"""Strict deterministic citation plans for Stage 16 and Stage 17."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from researchclaw.config import RCConfig
from researchclaw.literature.citation_policy import (
    ActiveConfigSnapshotInputs,
    CitationPolicyContractError,
    build_citation_allowlist_from_replayed_inputs,
    build_effective_citation_policy_from_replayed_inputs,
    load_effective_citation_policy,
    parse_citation_allowlist,
    parse_effective_citation_policy,
    replay_active_config_snapshot,
    validate_citation_allowlist,
)
from researchclaw.literature.evidence_cards import (
    canonical_json_text,
    load_validated_cards,
    parse_cards_manifest,
    validate_card_inputs_from_texts,
    validate_cards_artifacts_from_texts,
)
from researchclaw.literature.experiment_fact_closure import (
    ExperimentFactClosureError,
    build_experiment_fact_closure_report,
    parse_experiment_fact_closure_report,
    replay_experiment_fact_closure,
)
from researchclaw.literature.citation_identity import (
    CitationIdentityError,
    parse_cite_key_registry,
    validate_registry_artifacts,
)
from researchclaw.literature.screening import sha256_text
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    parse_manuscript,
)
from researchclaw.pipeline._domain import _prompt_bank_domain_from_config
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_release_graph_epoch,
)


CITATION_PLAN_SCHEMA_VERSION = 1
CITATION_PLAN_VERSION = 2
DOMAIN_V2_CITATION_BATCH_MAX_ANCHORS = 1
DOMAIN_V2_CITATION_BATCH_MAX_PROMPT_UTF8_BYTES = 16_384
DOMAIN_V2_CITATION_FRAGMENT_MAX_UTF8_BYTES = 8_192
_STRICT_CITE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*")
_MARKDOWN_CITATION_CANDIDATE_RE = re.compile(r"\[([^\[\]\n]+)\]")
_LATEX_CITATION_CANDIDATE_RE = re.compile(
    r"\\(?:cite|citep|citet)\*?(?:\[[^\]\n]*\]){0,2}\{([^{}\n]+)\}"
)
_CITATION_LIKE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*")
_LATEX_CITATION_LIKE_RE = re.compile(
    r"\\+[A-Za-z]*cite[A-Za-z]*\*?", re.IGNORECASE
)
_AUTHOR_YEAR_CITATION_RE = re.compile(
    r"\b[A-Za-z][A-Za-z'_-]*(?:\s+et\s+al\.)?"
    r"[^]\n]{0,32}\b(?:18|19|20|21)\d{2}[a-z]?\b",
    re.IGNORECASE,
)
_BARE_AUTHOR_YEAR_CITATION_RE = re.compile(
    r"""
    (?:\(\s*|\b)
    [A-Za-z][A-Za-z'_-]*
    (?:
        \s+et\s+al\.
        |
        \s+(?:and|&)\s+[A-Za-z][A-Za-z'_-]*
    )?
    \s*
    (?:
        ,\s*(?:18|19|20|21)\d{2}[a-z]?
        |
        \(\s*(?:18|19|20|21)\d{2}[a-z]?
    )
    (?:
        \s*\)
        |
        (?=\s*(?:[.;:!?\n]|$))
        |
        (?=\s+(?:reported|showed|found|proposed|demonstrated)\b)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NUMERIC_CITATION_RE = re.compile(
    r"\s*\d+(?:\s*(?:[-–,;]\s*)\d+)*\s*"
)
_AT_CITATION_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_:-]*")
_HTML_HEADING_RE = re.compile(r"</?h[1-6]\b[^>]*>", re.IGNORECASE)


class CitationPlanContractError(ValueError):
    """Raised when a citation plan is not closed over retained evidence."""


@dataclass(frozen=True)
class CitationOccurrence:
    """One strict, unescaped citation marker bound to its prose sentence."""

    syntax: str
    keys: tuple[str, ...]
    char_start: int
    char_end: int
    sentence_start: int
    sentence_end: int
    sentence_sha256: str
    sentence_ordinal: int
    occurrence_ordinal: int


@dataclass(frozen=True)
class CitationAnchor:
    """Code-owned standalone-line projection of one final-plan claim."""

    claim_id: str
    heading: str
    claim_text: str
    cite_key: str
    boundary_kind: str = "standalone_line"


@dataclass(frozen=True)
class CitationAnchorBatch:
    """One precomputed provider-visible prompt for contiguous plan anchors."""

    ordinal: int
    anchors: tuple[CitationAnchor, ...]
    system_prompt: str
    user_prompt: str
    prompt_utf8_bytes: int


def _is_standalone_citation_claim(text: object) -> bool:
    return (
        type(text) is str
        and bool(text)
        and "\x00" not in text
        and "\r" not in text
        and "\n" not in text
        and text == text.strip()
        and strict_sentence_spans(text) == ((0, len(text)),)
    )


def project_citation_anchors(plan: Mapping[str, Any]) -> tuple[CitationAnchor, ...]:
    """Derive strict sentence anchors without changing final-plan bytes."""

    parsed = parse_citation_plan(canonical_json_text(plan))
    anchors: list[CitationAnchor] = []
    seen_texts: set[str] = set()
    for claim in parsed["claims"]:
        text = claim["claim_text"]
        if not _is_standalone_citation_claim(text):
            raise CitationPlanContractError(
                "citation claim cannot form one exact standalone-line anchor"
            )
        if text in seen_texts:
            raise CitationPlanContractError("duplicate citation claim anchor text")
        seen_texts.add(text)
        anchors.append(
            CitationAnchor(
                claim_id=claim["claim_id"],
                heading=claim["section_path"][0],
                claim_text=text,
                cite_key=claim["planned_citations"][0]["cite_key"],
            )
        )
    return tuple(anchors)


def project_contiguous_citation_anchor_batches(
    anchors: tuple[CitationAnchor, ...],
    *,
    render_prompt: Callable[[tuple[CitationAnchor, ...]], tuple[str, str]],
    max_anchors: int = DOMAIN_V2_CITATION_BATCH_MAX_ANCHORS,
    max_prompt_utf8_bytes: int = DOMAIN_V2_CITATION_BATCH_MAX_PROMPT_UTF8_BYTES,
) -> tuple[CitationAnchorBatch, ...]:
    """Freeze single-anchor calls using final provider-visible bytes."""

    if type(max_anchors) is not int or max_anchors <= 0:
        raise CitationPlanContractError("citation batch anchor budget is invalid")
    if max_anchors != 1:
        raise CitationPlanContractError(
            "domain-v2 citation projection requires single-anchor batches"
        )
    if type(max_prompt_utf8_bytes) is not int or max_prompt_utf8_bytes <= 0:
        raise CitationPlanContractError("citation batch prompt budget is invalid")
    if not anchors:
        return ()
    heading = anchors[0].heading
    if any(anchor.heading != heading for anchor in anchors):
        raise CitationPlanContractError(
            "citation anchor batch cannot span multiple headings"
        )

    frozen: list[CitationAnchorBatch] = []
    pending: tuple[CitationAnchor, ...] = ()
    pending_prompts: tuple[str, str] | None = None
    pending_bytes = 0

    def render(candidate: tuple[CitationAnchor, ...]) -> tuple[str, str, int]:
        prompts = render_prompt(candidate)
        if (
            not isinstance(prompts, tuple)
            or len(prompts) != 2
            or any(type(prompt) is not str for prompt in prompts)
        ):
            raise CitationPlanContractError(
                "citation batch prompt renderer returned invalid prompts"
            )
        size = sum(len(prompt.encode("utf-8")) for prompt in prompts)
        return prompts[0], prompts[1], size

    def append_pending() -> None:
        nonlocal pending, pending_prompts, pending_bytes
        if not pending or pending_prompts is None:
            return
        frozen.append(
            CitationAnchorBatch(
                ordinal=len(frozen) + 1,
                anchors=pending,
                system_prompt=pending_prompts[0],
                user_prompt=pending_prompts[1],
                prompt_utf8_bytes=pending_bytes,
            )
        )
        pending = ()
        pending_prompts = None
        pending_bytes = 0

    for anchor in anchors:
        candidate = pending + (anchor,)
        if len(candidate) > max_anchors:
            append_pending()
            candidate = (anchor,)
        system_prompt, user_prompt, size = render(candidate)
        if size > max_prompt_utf8_bytes:
            if pending:
                append_pending()
                candidate = (anchor,)
                system_prompt, user_prompt, size = render(candidate)
            if size > max_prompt_utf8_bytes:
                raise CitationPlanContractError(
                    "single citation anchor exceeds prompt budget"
                )
        pending = candidate
        pending_prompts = (system_prompt, user_prompt)
        pending_bytes = size
    append_pending()
    return tuple(frozen)


def validate_citation_free_anchor_fragment(
    text: str,
    *,
    anchors: tuple[CitationAnchor, ...],
    all_anchors: tuple[CitationAnchor, ...],
) -> None:
    """Validate one headingless domain-v2 fragment against a contiguous batch."""

    if len(text.encode("utf-8")) > DOMAIN_V2_CITATION_FRAGMENT_MAX_UTF8_BYTES:
        raise CitationPlanContractError(
            "domain-v2 citation fragment exceeds response budget"
        )
    if not anchors:
        raise CitationPlanContractError("citation fragment has no active anchors")
    if len(set(all_anchors)) != len(all_anchors):
        raise CitationPlanContractError(
            "full citation anchor authority contains duplicates"
        )
    matches = tuple(
        start
        for start in range(0, len(all_anchors) - len(anchors) + 1)
        if all_anchors[start:start + len(anchors)] == anchors
    )
    if len(matches) != 1:
        raise CitationPlanContractError(
            "citation fragment anchors are not one exact contiguous authority slice"
        )
    if "\r" in text:
        raise CitationPlanContractError(
            "domain-v2 citation fragment must use LF newlines"
        )
    require_citation_candidate_free(text)
    if _HTML_HEADING_RE.search(text):
        raise CitationPlanContractError(
            "domain-v2 citation fragment contains an HTML heading"
        )
    document = parse_manuscript(text, strict=False)
    if document.sections:
        raise CitationPlanContractError("domain-v2 citation fragment contains a heading")
    if re.search(r"(?m)^[ \t]{0,3}(?:`{3,}|~{3,})", text):
        raise CitationPlanContractError(
            "domain-v2 citation fragment contains a code fence"
        )

    active = set(anchors)
    positions: list[int] = []
    for anchor in all_anchors:
        if anchor.claim_id in text:
            raise CitationPlanContractError(
                "domain-v2 citation fragment exposes a claim id literal"
            )
        if anchor.cite_key in text:
            raise CitationPlanContractError(
                "domain-v2 citation fragment exposes a citation key literal"
            )
        if anchor not in active:
            if anchor.claim_text in text:
                raise CitationPlanContractError(
                    "domain-v2 citation fragment exposes a foreign claim anchor"
                )
            continue
        exact_lines = [line for line in text.split("\n") if line == anchor.claim_text]
        if len(exact_lines) != 1 or text.count(anchor.claim_text) != 1:
            raise CitationPlanContractError(
                "domain-v2 citation anchor is missing, changed, or not unique"
            )
        positions.append(text.index(anchor.claim_text))
    if positions != sorted(positions):
        raise CitationPlanContractError(
            "domain-v2 citation anchors are out of plan order"
        )


def require_citation_candidate_free(text: str) -> None:
    """Reject every citation-like candidate, including escaped or malformed forms."""

    if _LATEX_CITATION_LIKE_RE.search(text):
        raise CitationPlanContractError("initial domain-v2 draft contains citation candidate")
    if _BARE_AUTHOR_YEAR_CITATION_RE.search(text):
        raise CitationPlanContractError(
            "initial domain-v2 draft contains citation candidate"
        )
    for line in text.splitlines():
        bracket = line.find("[")
        while bracket >= 0:
            close = line.find("]", bracket + 1)
            candidate = line[bracket + 1 :] if close < 0 else line[bracket + 1 : close]
            if (
                _CITATION_LIKE_KEY_RE.search(candidate)
                or _AUTHOR_YEAR_CITATION_RE.search(candidate)
                or _NUMERIC_CITATION_RE.fullmatch(candidate)
                or _AT_CITATION_RE.search(candidate)
            ):
                raise CitationPlanContractError(
                    "initial domain-v2 draft contains citation candidate"
                )
            bracket = line.find("[", bracket + 1 if close < 0 else close + 1)


def validate_citation_free_anchor_draft(
    text: str,
    *,
    anchors: tuple[CitationAnchor, ...],
    active_headings: tuple[str, ...],
) -> None:
    """Validate one domain-v2 writer response before any free-form repair."""

    if "\r" in text:
        raise CitationPlanContractError("domain-v2 draft must use LF newlines")
    require_citation_candidate_free(text)
    document = parse_manuscript(text, strict=True)
    sections = {
        section.path[0]: section
        for section in document.sections
        if len(section.path) == 1
    }
    active = set(active_headings)
    positions: list[int] = []
    for anchor in anchors:
        if anchor.cite_key in text:
            raise CitationPlanContractError("domain-v2 draft exposes a citation key literal")
        if anchor.heading not in active:
            if anchor.claim_text in text:
                raise CitationPlanContractError("domain-v2 draft exposes a foreign claim anchor")
            continue
        section = sections.get(anchor.heading)
        if section is None:
            raise CitationPlanContractError("domain-v2 citation heading is missing")
        exact_lines = [
            line for line in section.body.split("\n") if line == anchor.claim_text
        ]
        if len(exact_lines) != 1 or text.count(anchor.claim_text) != 1:
            raise CitationPlanContractError(
                "domain-v2 citation anchor is missing, changed, or not unique"
            )
        positions.append(text.index(anchor.claim_text))
    if positions != sorted(positions):
        raise CitationPlanContractError("domain-v2 citation anchors are out of plan order")


def strict_sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return deterministic non-empty prose sentence spans."""

    spans: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(text):
        boundary = character == "\n" or (
            character in ".!?"
            and (index + 1 == len(text) or text[index + 1].isspace())
        )
        if not boundary:
            continue
        end = index if character == "\n" else index + 1
        left, right = _trim_span(text, start, end)
        if left < right:
            spans.append((left, right))
        start = index + 1
    left, right = _trim_span(text, start, len(text))
    if left < right:
        spans.append((left, right))
    return tuple(spans)


def parse_strict_citation_occurrences(text: str) -> tuple[CitationOccurrence, ...]:
    """Parse only grammar-valid, unescaped Markdown and LaTeX citations."""

    candidates: list[tuple[int, int, str, tuple[str, ...]]] = []
    for syntax, pattern in (
        ("markdown", _MARKDOWN_CITATION_CANDIDATE_RE),
        ("latex", _LATEX_CITATION_CANDIDATE_RE),
    ):
        for match in pattern.finditer(text):
            if _is_escaped_marker(text, match.start(), syntax):
                continue
            keys = _parse_marker_keys(match.group(1))
            if keys is None:
                continue
            candidates.append((match.start(), match.end(), syntax, keys))
    candidates.sort(key=lambda item: (item[0], item[1]))
    if any(left[1] > right[0] for left, right in zip(candidates, candidates[1:])):
        raise CitationPlanContractError("overlapping citation occurrences")

    sentence_spans = strict_sentence_spans(text)
    ordinals: dict[tuple[int, int], int] = {}
    sentence_hash_ordinals: dict[str, int] = {}
    occurrences: list[CitationOccurrence] = []
    for start, end, syntax, keys in candidates:
        sentence = next(
            (
                span for span in sentence_spans
                if span[0] <= start and end <= span[1]
            ),
            None,
        )
        if sentence is None:
            raise CitationPlanContractError("citation occurrence lacks sentence anchor")
        ordinal = ordinals.get(sentence, 0)
        ordinals[sentence] = ordinal + 1
        sentence_markers = [
            item for item in candidates
            if sentence[0] <= item[0] and item[1] <= sentence[1]
        ]
        anchor = _remove_spans(
            text[sentence[0]:sentence[1]],
            [
                (item[0] - sentence[0], item[1] - sentence[0])
                for item in sentence_markers
            ],
            remove_leading_space=True,
        )
        sentence_sha256 = hashlib.sha256(anchor.encode("utf-8")).hexdigest()
        sentence_ordinal = sentence_hash_ordinals.get(sentence_sha256, 0)
        if ordinal == 0:
            sentence_hash_ordinals[sentence_sha256] = sentence_ordinal + 1
        occurrences.append(
            CitationOccurrence(
                syntax=syntax,
                keys=keys,
                char_start=start,
                char_end=end,
                sentence_start=sentence[0],
                sentence_end=sentence[1],
                sentence_sha256=sentence_sha256,
                sentence_ordinal=sentence_ordinal,
                occurrence_ordinal=ordinal,
            )
        )
    return tuple(occurrences)


def strict_citation_keys(text: str) -> frozenset[str]:
    return frozenset(
        key for occurrence in parse_strict_citation_occurrences(text)
        for key in occurrence.keys
    )


def extract_citation_keys(text: str) -> frozenset[str]:
    """Compatibility entry point backed by the strict shared parser."""

    return strict_citation_keys(text)


def filter_strict_citation_markers(
    text: str, allowed_keys: frozenset[str]
) -> str:
    """Filter only strict citation occurrences; preserve all other brackets."""

    replacements: list[tuple[int, int, str]] = []
    for occurrence in parse_strict_citation_occurrences(text):
        retained = tuple(key for key in occurrence.keys if key in allowed_keys)
        if retained == occurrence.keys:
            continue
        if not retained:
            replacement = ""
        elif occurrence.syntax == "markdown":
            replacement = f"[{', '.join(retained)}]"
        else:
            marker = text[occurrence.char_start:occurrence.char_end]
            replacement = marker[:marker.index("{") + 1] + ",".join(retained) + "}"
        replacements.append((occurrence.char_start, occurrence.char_end, replacement))
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text


def strip_strict_citation_markers(text: str) -> str:
    occurrences = parse_strict_citation_occurrences(text)
    return _remove_spans(
        text,
        [(item.char_start, item.char_end) for item in occurrences],
        remove_leading_space=True,
    )


def _parse_marker_keys(value: str) -> tuple[str, ...] | None:
    parts = tuple(item.strip() for item in re.split(r"[,;]", value))
    if (
        not parts
        or any(not item or _STRICT_CITE_KEY_RE.fullmatch(item) is None for item in parts)
        or len(parts) != len(set(parts))
    ):
        return None
    return parts


def _is_escaped_marker(text: str, start: int, syntax: str) -> bool:
    slash_count = 0
    index = start - 1 if syntax == "markdown" else start
    while index >= 0 and text[index] == "\\":
        slash_count += 1
        index -= 1
    return slash_count % 2 == 1 if syntax == "markdown" else slash_count % 2 == 0


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _remove_spans(
    text: str, spans: list[tuple[int, int]], *, remove_leading_space: bool
) -> str:
    for start, end in sorted(spans, reverse=True):
        if remove_leading_space and start > 0 and text[start - 1] == " ":
            start -= 1
        text = text[:start] + text[end:]
    return text


@dataclass(frozen=True)
class CitationPlanReplayInputs:
    """Complete captured source set for Stage 16 citation-plan replay."""

    candidates_text: str
    registry_text: str
    bibliography_text: str
    shortlist_text: str
    screening_report_text: str
    cards_manifest_text: str
    card_texts: Mapping[str, str]
    citation_allowlist_text: str
    effective_policy_text: str
    citation_plan_text: str
    active_config: ActiveConfigSnapshotInputs


@dataclass(frozen=True)
class ReplayedCitationAuthority:
    """Source-recomputed citation authority passed to Stage 17/19 replay."""

    allowlist: Mapping[str, Any]
    effective_policy: Mapping[str, Any]
    plan: Mapping[str, Any]
    cards: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CapturedCitationAuthority:
    """Immutable Stage 17 citation generation captured before its first LLM call."""

    inputs: CitationPlanReplayInputs
    replayed: ReplayedCitationAuthority
    run_identity: tuple[int, int]


class _CitationAuthorityReader:
    """Read one citation generation only through a held run-directory fd."""

    def __init__(self, run_fd: int) -> None:
        self._run_fd = run_fd
        info = os.fstat(run_fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(run_fd)
            self._run_fd = -1
            raise CitationPlanContractError("citation authority run fd is not a directory")
        self.run_identity = (info.st_dev, info.st_ino)

    def close(self) -> None:
        if self._run_fd >= 0:
            os.close(self._run_fd)
            self._run_fd = -1

    def __enter__(self) -> "_CitationAuthorityReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def read_text(self, relative_path: str) -> str:
        try:
            content = self._read_bytes(relative_path)
        except OSError as exc:
            raise CitationPlanContractError(
                f"cannot read citation authority: {relative_path}"
            ) from exc
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CitationPlanContractError(
                f"citation authority is not UTF-8: {relative_path}"
            ) from exc

    def read_optional_text(self, relative_path: str) -> str | None:
        try:
            content = self._read_bytes(relative_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CitationPlanContractError(
                f"cannot read citation authority: {relative_path}"
            ) from exc
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CitationPlanContractError(
                f"citation authority is not UTF-8: {relative_path}"
            ) from exc

    def directory_entries(self, relative_path: str) -> tuple[str, ...]:
        parts = _citation_relative_parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.dup(self._run_fd)
        opened = [descriptor]
        try:
            for part in parts:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                opened.append(descriptor)
            return tuple(sorted(os.listdir(descriptor)))
        except OSError as exc:
            raise CitationPlanContractError(
                f"cannot enumerate citation authority: {relative_path}"
            ) from exc
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)

    def _read_bytes(self, relative_path: str) -> bytes:
        parts = _citation_relative_parts(relative_path)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.dup(self._run_fd)
        opened = [descriptor]
        try:
            for part in parts[:-1]:
                descriptor = os.open(part, directory_flags, dir_fd=descriptor)
                opened.append(descriptor)
            file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            file_flags |= getattr(os, "O_CLOEXEC", 0)
            leaf = os.open(parts[-1], file_flags, dir_fd=descriptor)
            try:
                info = os.fstat(leaf)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError(
                        f"citation authority is not a regular file: {relative_path}"
                    )
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(leaf, 1024 * 1024)
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)
            finally:
                os.close(leaf)
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)


def _citation_relative_parts(relative_path: str) -> tuple[str, ...]:
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise CitationPlanContractError("noncanonical citation authority path")
    return relative.parts


def _citation_section_for_config(config: RCConfig) -> str:
    """Return the sole background-evidence section owned by the prompt bank."""
    return (
        "Introduction"
        if _prompt_bank_domain_from_config(config) == "hep_ph"
        else "Related Work"
    )


def build_citation_plan(
    run_dir: Path, config: RCConfig, *, plan_status: str
) -> dict[str, Any]:
    if plan_status not in {"preliminary", "final"}:
        raise CitationPlanContractError("invalid citation plan status")
    allowlist_path = run_dir / "stage-06" / "citation_allowlist.json"
    manifest_path = run_dir / "stage-06" / "cards_manifest.json"
    policy_path = run_dir / "stage-16" / "citation_policy_effective.json"
    try:
        allowlist_text = allowlist_path.read_text(encoding="utf-8")
        manifest_text = manifest_path.read_text(encoding="utf-8")
        policy_text = policy_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CitationPlanContractError(f"cannot read citation-plan source: {exc}") from exc
    try:
        allowlist = validate_citation_allowlist(run_dir, config, allowlist_text)
        policy = load_effective_citation_policy(run_dir, config)
        cards = load_validated_cards(run_dir, config)
    except (CitationPolicyContractError, ValueError) as exc:
        raise CitationPlanContractError(f"invalid citation-plan source: {exc}") from exc

    return build_citation_plan_from_replayed_inputs(
        config=config,
        plan_status=plan_status,
        allowlist=allowlist,
        allowlist_text=allowlist_text,
        cards_manifest_text=manifest_text,
        effective_policy=policy,
        effective_policy_text=policy_text,
        cards=cards,
    )


def build_citation_plan_from_replayed_inputs(
    *,
    config: RCConfig,
    plan_status: str,
    allowlist: Mapping[str, Any],
    allowlist_text: str,
    cards_manifest_text: str,
    effective_policy: Mapping[str, Any],
    effective_policy_text: str,
    cards: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Build a plan solely from already replayed citation source artifacts."""
    if plan_status not in {"preliminary", "final"}:
        raise CitationPlanContractError("invalid citation plan status")

    cards_by_key = {str(card["cite_key"]): card for card in cards}
    target = int(effective_policy["effective_target_unique_sources"])
    selected_keys = list(allowlist["eligible_keys"])[:target]
    if len(selected_keys) < int(effective_policy["effective_min_unique_sources"]):
        raise CitationPlanContractError("citation plan cannot meet effective minimum")
    claims: list[dict[str, Any]] = []
    citation_section = _citation_section_for_config(config)
    for ordinal, cite_key in enumerate(selected_keys, start=1):
        card = cards_by_key.get(cite_key)
        if card is None or card["extraction_status"] != "success":
            raise CitationPlanContractError("eligible key lacks successful card")
        excerpts = card["evidence_excerpts"]
        if not excerpts:
            raise CitationPlanContractError("eligible key lacks retained excerpt")
        claim_text = next(
            (
                excerpt["excerpt_text"]
                for excerpt in excerpts
                if _is_standalone_citation_claim(excerpt["excerpt_text"])
            ),
            None,
        )
        if claim_text is None:
            raise CitationPlanContractError(
                "eligible key lacks standalone citation excerpt"
            )
        claims.append(
            {
                "claim_id": f"planned-claim-{ordinal:03d}",
                "section_path": [citation_section],
                "claim_text": claim_text,
                "claim_type": "background",
                "planned_citations": [
                    {
                        "cite_key": cite_key,
                        "evidence_excerpt_ids": [
                            excerpt["excerpt_id"] for excerpt in excerpts
                        ],
                        "support_status": "abstract_sufficient",
                    }
                ],
            }
        )
    payload = {
        "schema_version": CITATION_PLAN_SCHEMA_VERSION,
        "plan_version": CITATION_PLAN_VERSION,
        "plan_status": plan_status,
        "claim_scope": config.experiment.claim_scope,
        "citation_allowlist_path": "stage-06/citation_allowlist.json",
        "citation_allowlist_sha256": sha256_text(allowlist_text),
        "cards_manifest_path": "stage-06/cards_manifest.json",
        "cards_manifest_sha256": sha256_text(cards_manifest_text),
        "effective_policy_path": "stage-16/citation_policy_effective.json",
        "effective_policy_sha256": sha256_text(effective_policy_text),
        "claims": claims,
    }
    return parse_citation_plan(canonical_json_text(payload))


def replay_citation_plan_provenance(
    inputs: CitationPlanReplayInputs,
    runtime_config: RCConfig | None,
    *,
    project_root: Path,
) -> ReplayedCitationAuthority:
    """Rebuild Stage 4-6 eligibility and Stage 16 policy/plan from captured bytes."""
    try:
        snapshot_config, config_path, config_sha256 = replay_active_config_snapshot(
            inputs.active_config, runtime_config, project_root=project_root
        )
        card_inputs = validate_card_inputs_from_texts(
            candidates_text=inputs.candidates_text,
            registry_text=inputs.registry_text,
            bibliography_text=inputs.bibliography_text,
            shortlist_text=inputs.shortlist_text,
            screening_report_text=inputs.screening_report_text,
            config=snapshot_config,
        )
        cards = validate_cards_artifacts_from_texts(
            manifest_text=inputs.cards_manifest_text,
            card_texts=inputs.card_texts,
            shortlist_text=card_inputs.shortlist_text,
            screening_report_text=card_inputs.screening_report_text,
            candidates_sha256=sha256_text(card_inputs.candidates_text),
            shortlist=card_inputs.shortlist,
        )
        expected_allowlist = build_citation_allowlist_from_replayed_inputs(
            card_inputs=card_inputs,
            cards=cards,
            cards_manifest_text=inputs.cards_manifest_text,
            references_text=inputs.bibliography_text,
        )
        stored_allowlist = parse_citation_allowlist(inputs.citation_allowlist_text)
        if stored_allowlist != expected_allowlist:
            raise CitationPlanContractError("citation allowlist replay mismatch")
        expected_policy = build_effective_citation_policy_from_replayed_inputs(
            allowlist=stored_allowlist,
            allowlist_text=inputs.citation_allowlist_text,
            snapshot_config=snapshot_config,
            config_source_path=config_path,
            config_source_sha256=config_sha256,
        )
        stored_policy = parse_effective_citation_policy(inputs.effective_policy_text)
        if stored_policy != expected_policy:
            raise CitationPlanContractError("effective citation policy replay mismatch")
        expected_plan = build_citation_plan_from_replayed_inputs(
            config=snapshot_config,
            plan_status="final",
            allowlist=stored_allowlist,
            allowlist_text=inputs.citation_allowlist_text,
            cards_manifest_text=inputs.cards_manifest_text,
            effective_policy=stored_policy,
            effective_policy_text=inputs.effective_policy_text,
            cards=cards,
        )
        stored_plan = parse_citation_plan(inputs.citation_plan_text)
    except (CitationPolicyContractError, CitationIdentityError, ValueError) as exc:
        raise CitationPlanContractError(f"citation provenance replay failed: {exc}") from exc
    if stored_plan != expected_plan:
        raise CitationPlanContractError("citation plan replay mismatch")
    return ReplayedCitationAuthority(
        allowlist=stored_allowlist,
        effective_policy=stored_policy,
        plan=stored_plan,
        cards=cards,
    )


def capture_replayed_citation_authority(
    run_dir: Path, config: RCConfig, *, release_lock: object | None = None
) -> CapturedCitationAuthority:
    """Capture A, replay only A, then require an immediate identical snapshot B."""

    if release_lock is None:
        with ReleaseGraphLock.acquire(
            run_dir, "capture_replayed_citation_authority", mode="read"
        ) as owned_epoch:
            return capture_replayed_citation_authority(
                run_dir, config, release_lock=owned_epoch
            )
    epoch = require_active_release_graph_epoch(run_dir, release_lock)
    with _CitationAuthorityReader(epoch.duplicate_run_fd()) as reader:
        first = _capture_citation_plan_inputs(reader)
        replayed = replay_citation_plan_provenance(
            first, config, project_root=run_dir
        )
        if _capture_citation_plan_inputs(reader) != first:
            raise CitationPlanContractError(
                "citation authority changed during initial capture replay"
            )
        epoch.assert_canonical()
        return CapturedCitationAuthority(
            inputs=first,
            replayed=replayed,
            run_identity=reader.run_identity,
        )


def verify_captured_citation_authority_unchanged(
    run_dir: Path,
    captured: CapturedCitationAuthority,
    *,
    release_lock: object | None = None,
) -> None:
    """Compare fresh bytes without replacing the captured generation."""

    if release_lock is None:
        with ReleaseGraphLock.acquire(
            run_dir, "verify_captured_citation_authority_unchanged", mode="read"
        ) as owned_epoch:
            verify_captured_citation_authority_unchanged(
                run_dir, captured, release_lock=owned_epoch
            )
            return
    epoch = require_active_release_graph_epoch(run_dir, release_lock)
    with _CitationAuthorityReader(epoch.duplicate_run_fd()) as reader:
        if reader.run_identity != captured.run_identity:
            raise CitationPlanContractError(
                "citation authority run identity changed after capture"
            )
        if _capture_citation_plan_inputs(reader) != captured.inputs:
            raise CitationPlanContractError(
                "citation authority changed after Stage 17 generation"
            )
        epoch.assert_canonical()


def _capture_citation_plan_inputs(
    reader: _CitationAuthorityReader,
) -> CitationPlanReplayInputs:
    paths = {
        "candidates_text": "stage-04/candidates.jsonl",
        "registry_text": "stage-04/cite_key_registry.json",
        "bibliography_text": "stage-04/references.bib",
        "shortlist_text": "stage-05/shortlist.jsonl",
        "screening_report_text": "stage-05/screening_report.json",
        "cards_manifest_text": "stage-06/cards_manifest.json",
        "citation_allowlist_text": "stage-06/citation_allowlist.json",
        "effective_policy_text": "stage-16/citation_policy_effective.json",
        "citation_plan_text": "stage-16/citation_plan.json",
    }
    texts = {
        field: reader.read_text(path)
        for field, path in paths.items()
    }
    manifest = parse_cards_manifest(texts["cards_manifest_text"])
    card_paths = sorted(
        str(entry[field])
        for entry in manifest["cards"]
        for field in ("json_path", "markdown_path")
    )
    actual_card_paths = [
        f"stage-06/cards/{entry}"
        for entry in reader.directory_entries("stage-06/cards")
    ]
    if actual_card_paths != card_paths:
        raise CitationPlanContractError("canonical cards namespace is not exact")
    card_texts = {
        path: reader.read_text(path) for path in card_paths
    }

    pointer_text = reader.read_optional_text("active_config_snapshot.json")
    history_text = reader.read_optional_text("config_snapshot_history.jsonl")
    checkpoint_text = reader.read_optional_text("checkpoint.json")
    if pointer_text is None:
        config_path = "config.yaml"
    else:
        pointer = _parse_object(pointer_text, "active config pointer")
        config_path = pointer.get("config_source_path")
        if not isinstance(config_path, str):
            raise CitationPlanContractError("active config pointer has no source path")
    config_text = reader.read_text(config_path)
    return CitationPlanReplayInputs(
        **texts,
        card_texts=card_texts,
        active_config=ActiveConfigSnapshotInputs(
            config_source_path=config_path,
            config_source_text=config_text,
            pointer_text=pointer_text,
            history_text=history_text,
            checkpoint_text=checkpoint_text,
        ),
    )


def parse_citation_plan(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "citation plan")
    _exact_keys(
        payload,
        {
            "schema_version", "plan_version", "plan_status", "claim_scope",
            "citation_allowlist_path", "citation_allowlist_sha256",
            "cards_manifest_path", "cards_manifest_sha256",
            "effective_policy_path", "effective_policy_sha256", "claims",
        },
        "citation plan",
    )
    if payload["schema_version"] != CITATION_PLAN_SCHEMA_VERSION:
        raise CitationPlanContractError("unsupported citation plan schema")
    if payload["plan_version"] != CITATION_PLAN_VERSION:
        raise CitationPlanContractError("unsupported citation plan version")
    if payload["plan_status"] not in {"preliminary", "final"}:
        raise CitationPlanContractError("invalid citation plan status")
    if payload["claim_scope"] not in {"pipeline_validation", "exploratory", "research_release"}:
        raise CitationPlanContractError("invalid citation plan claim_scope")
    expected_paths = {
        "citation_allowlist_path": "stage-06/citation_allowlist.json",
        "cards_manifest_path": "stage-06/cards_manifest.json",
        "effective_policy_path": "stage-16/citation_policy_effective.json",
    }
    for field, expected in expected_paths.items():
        if payload[field] != expected:
            raise CitationPlanContractError(f"noncanonical {field}")
    for field in (
        "citation_allowlist_sha256", "cards_manifest_sha256",
        "effective_policy_sha256",
    ):
        _sha256_field(payload, field)
    if not isinstance(payload["claims"], list) or not payload["claims"]:
        raise CitationPlanContractError("citation plan claims must not be empty")
    claim_ids: set[str] = set()
    planned_keys: set[str] = set()
    for ordinal, claim in enumerate(payload["claims"], start=1):
        if not isinstance(claim, dict):
            raise CitationPlanContractError("planned claim must be an object")
        _exact_keys(
            claim,
            {"claim_id", "section_path", "claim_text", "claim_type", "planned_citations"},
            "planned claim",
        )
        claim_id = _required_string(claim, "claim_id")
        if claim_id != f"planned-claim-{ordinal:03d}" or claim_id in claim_ids:
            raise CitationPlanContractError("planned claim ID sequence mismatch")
        claim_ids.add(claim_id)
        if claim["section_path"] not in (["Introduction"], ["Related Work"]):
            raise CitationPlanContractError("unsupported v2 section_path")
        _required_string(claim, "claim_text")
        if claim["claim_type"] != "background":
            raise CitationPlanContractError("unsupported v2 claim_type")
        citations = claim["planned_citations"]
        if not isinstance(citations, list) or len(citations) != 1:
            raise CitationPlanContractError("v2 claim requires one planned citation")
        citation = citations[0]
        if not isinstance(citation, dict):
            raise CitationPlanContractError("planned citation must be an object")
        _exact_keys(
            citation,
            {"cite_key", "evidence_excerpt_ids", "support_status"},
            "planned citation",
        )
        key = _required_string(citation, "cite_key")
        if key in planned_keys:
            raise CitationPlanContractError("duplicate planned cite_key")
        planned_keys.add(key)
        ids = citation["evidence_excerpt_ids"]
        if not isinstance(ids, list) or not ids or any(
            not isinstance(item, str) or not item.strip() for item in ids
        ) or len(ids) != len(set(ids)):
            raise CitationPlanContractError("invalid evidence_excerpt_ids")
        if citation["support_status"] != "abstract_sufficient":
            raise CitationPlanContractError("final v2 plan requires abstract_sufficient")
    return payload


def validate_citation_plan(
    run_dir: Path, config: RCConfig, text: str, *, plan_status: str
) -> dict[str, Any]:
    stored = parse_citation_plan(text)
    expected = build_citation_plan(run_dir, config, plan_status=plan_status)
    if stored != expected:
        raise CitationPlanContractError("citation plan replay mismatch")
    return stored


def load_final_citation_plan(run_dir: Path, config: RCConfig) -> dict[str, Any]:
    path = run_dir / "stage-16" / "citation_plan.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise CitationPlanContractError("final citation plan is missing or unsafe")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CitationPlanContractError(f"cannot read final citation plan: {exc}") from exc
    return validate_citation_plan(run_dir, config, text, plan_status="final")


def build_citation_writer_instruction(run_dir: Path, config: RCConfig) -> str:
    """Render the bounded final-plan citation surface for Stage 17."""

    plan = load_final_citation_plan(run_dir, config)
    cards = load_validated_cards(run_dir, config)
    return build_citation_writer_instruction_from_authority(plan, cards)


def build_citation_writer_instruction_from_authority(
    plan: Mapping[str, Any],
    cards: list[Mapping[str, Any]],
    *,
    section_names: tuple[str, ...] | None = None,
) -> str:
    """Render exact citation authority for the requested manuscript sections."""

    allowed_sections = None if section_names is None else set(section_names)
    cards_by_key = {str(card["cite_key"]): card for card in cards}
    blocks: list[str] = []
    for claim in plan["claims"]:
        if (
            allowed_sections is not None
            and str(claim["section_path"][0]) not in allowed_sections
        ):
            continue
        citation = claim["planned_citations"][0]
        cite_key = citation["cite_key"]
        card = cards_by_key.get(cite_key)
        if card is None or card["extraction_status"] != "success":
            raise CitationPlanContractError(
                f"planned key lacks a successful evidence card: {cite_key}"
            )
        excerpts_by_id = {
            excerpt["excerpt_id"]: excerpt
            for excerpt in card["evidence_excerpts"]
        }
        excerpt_lines: list[str] = []
        for excerpt_id in citation["evidence_excerpt_ids"]:
            excerpt = excerpts_by_id.get(excerpt_id)
            if excerpt is None:
                raise CitationPlanContractError(
                    f"planned excerpt is absent from its card: {excerpt_id}"
                )
            excerpt_lines.append(
                f'  - {excerpt_id}: "{excerpt["excerpt_text"]}"'
            )
        blocks.append(
            "\n".join(
                (
                    f"- CLAIM {claim['claim_id']} "
                    f"(section: {claim['section_path'][0]})",
                    f"  Allowed wording ceiling: {claim['claim_text']}",
                    f"  Required citation key: [{cite_key}]",
                    "  Retained abstract evidence:",
                    *excerpt_lines,
                )
            )
        )
    if not blocks:
        if allowed_sections is None:
            raise CitationPlanContractError("final citation plan has no writable claims")
        return (
            "\n\nFINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
            "No citation authority is assigned to this writing part.\n"
            "\nCITATION RULES:\n"
            "- Do not use any citation marker in this part.\n"
            "- Do not write a References section; it is generated from the canonical bibliography.\n"
        )
    return (
        "\n\nFINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
        + "\n\n".join(blocks)
        + "\n\nCITATION RULES:\n"
        "- Cite every required key above at least once using exact [cite_key] syntax.\n"
        "- Do not use any citation key that is not listed above.\n"
        "- Do not strengthen a claim beyond its retained abstract evidence.\n"
        "- Do not write a References section; it is generated from the canonical bibliography.\n"
        "- If the evidence does not support a stronger sentence, keep the bounded wording.\n"
    )


def build_heading_citation_writer_instructions_from_authority(
    plan: Mapping[str, Any],
    cards: list[Mapping[str, Any]],
    *,
    heading_names: tuple[str, ...],
) -> dict[str, str]:
    """Render exact final-plan authority separately for each top-level heading."""

    if len(heading_names) != len(set(heading_names)) or any(
        not isinstance(name, str) or not name for name in heading_names
    ):
        raise CitationPlanContractError("heading authority names must be unique strings")
    return {
        heading: build_citation_writer_instruction_from_authority(
            plan,
            cards,
            section_names=(heading,),
        )
        for heading in heading_names
    }


def attribute_citation_keys_to_top_level_headings(
    paper_text: str,
) -> dict[str, frozenset[str]]:
    """Strictly attribute Markdown citation markers to their top-level heading."""

    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError as exc:
        raise CitationPlanContractError(
            f"cannot attribute citations from an ambiguous manuscript: {exc}"
        ) from exc
    attributed: dict[str, set[str]] = {}
    for section in document.sections:
        top_level = section.path[0]
        attributed.setdefault(top_level, set()).update(strict_citation_keys(section.body))
    return {
        heading: frozenset(keys)
        for heading, keys in attributed.items()
    }


def load_canonical_bibliography(run_dir: Path) -> str:
    """Load and replay the immutable registry-bound Stage 4 bibliography."""

    stage04 = run_dir / "stage-04"
    candidates_path = stage04 / "candidates.jsonl"
    registry_path = stage04 / "cite_key_registry.json"
    bibliography_path = stage04 / "references.bib"
    try:
        for path in (candidates_path, registry_path, bibliography_path):
            if path.is_symlink() or not path.is_file():
                raise CitationPlanContractError(
                    f"canonical bibliography source is missing or unsafe: {path.name}"
                )
        candidates_text = candidates_path.read_text(encoding="utf-8")
        registry_text = registry_path.read_text(encoding="utf-8")
        bibliography = bibliography_path.read_text(encoding="utf-8")
        registry = parse_cite_key_registry(registry_text)
        validate_registry_artifacts(registry, candidates_text, bibliography)
    except (OSError, UnicodeDecodeError, CitationIdentityError) as exc:
        raise CitationPlanContractError(f"canonical bibliography is invalid: {exc}") from exc
    return bibliography


def validate_final_paper_citations(
    run_dir: Path, config: RCConfig, paper_text: str
) -> tuple[str, ...]:
    """Replay the final paper against the evidence-bound allowlist and plan."""

    allowlist_path = run_dir / "stage-06" / "citation_allowlist.json"
    try:
        if allowlist_path.is_symlink() or not allowlist_path.is_file():
            raise CitationPlanContractError("citation allowlist is missing or unsafe")
        allowlist_text = allowlist_path.read_text(encoding="utf-8")
        allowlist = validate_citation_allowlist(run_dir, config, allowlist_text)
        plan = load_final_citation_plan(run_dir, config)
    except (OSError, UnicodeDecodeError, CitationPolicyContractError) as exc:
        raise CitationPlanContractError(
            f"cannot validate final paper citations: {exc}"
        ) from exc
    cited = set(strict_citation_keys(paper_text))
    eligible = set(allowlist["eligible_keys"])
    planned = {
        citation["cite_key"]
        for claim in plan["claims"]
        for citation in claim["planned_citations"]
    }
    unknown = sorted(cited - eligible)
    unplanned = sorted(cited - planned)
    missing = sorted(planned - cited)
    if unknown or unplanned or missing:
        raise CitationPlanContractError(
            "final paper citation closure failed: "
            f"unknown={unknown}, unplanned={unplanned}, missing={missing}"
        )
    return tuple(sorted(cited))


def validate_paper_citation_minimum(
    run_dir: Path,
    config: RCConfig,
    paper_text: str,
    *,
    minimum: int,
) -> tuple[str, ...]:
    """Require the current paper to meet policy using eligible planned keys."""

    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise CitationPlanContractError("citation minimum must be a nonnegative integer")
    allowlist_path = run_dir / "stage-06" / "citation_allowlist.json"
    try:
        if allowlist_path.is_symlink() or not allowlist_path.is_file():
            raise CitationPlanContractError("citation allowlist is missing or unsafe")
        allowlist_text = allowlist_path.read_text(encoding="utf-8")
        allowlist = validate_citation_allowlist(run_dir, config, allowlist_text)
        plan = load_final_citation_plan(run_dir, config)
    except (OSError, UnicodeDecodeError, CitationPolicyContractError) as exc:
        raise CitationPlanContractError(
            f"cannot validate paper citation minimum: {exc}"
        ) from exc
    return validate_paper_citation_minimum_from_authority(
        paper_text,
        minimum=minimum,
        allowlist=allowlist,
        plan=plan,
    )


def validate_paper_citation_minimum_from_authority(
    paper_text: str,
    *,
    minimum: int,
    allowlist: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate a paper using already replayed citation authority."""

    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise CitationPlanContractError("citation minimum must be a nonnegative integer")
    cited = set(strict_citation_keys(paper_text))
    eligible = set(allowlist["eligible_keys"])
    planned = {
        citation["cite_key"]
        for claim in plan["claims"]
        for citation in claim["planned_citations"]
    }
    invalid = sorted(cited - eligible)
    unplanned = sorted(cited - planned)
    counted = sorted(cited & eligible & planned)
    if invalid or unplanned or len(counted) < minimum:
        raise CitationPlanContractError(
            "paper citation minimum failed: "
            f"eligible_count={len(counted)}, minimum={minimum}, "
            f"invalid={invalid}, unplanned={unplanned}"
        )
    return tuple(counted)


def build_citation_closure_report(
    run_dir: Path,
    config: RCConfig,
    *,
    paper_text: str,
    structure_report_text: str,
    experiment_fact_report_text: str,
    evidence: CanonicalExperimentEvidence | None = None,
) -> dict[str, Any]:
    plan_path = run_dir / "stage-16" / "citation_plan.json"
    allowlist_path = run_dir / "stage-06" / "citation_allowlist.json"
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
        allowlist_text = allowlist_path.read_text(encoding="utf-8")
        plan = load_final_citation_plan(run_dir, config)
        experiment = parse_experiment_fact_closure_report(
            experiment_fact_report_text
        )
        expected_experiment = build_experiment_fact_closure_report(
            run_dir, paper_text=paper_text, evidence=evidence
        )
        if experiment != expected_experiment:
            raise CitationPlanContractError("experiment fact closure replay mismatch")
        allowlist = validate_citation_allowlist(run_dir, config, allowlist_text)
    except (
        OSError,
        UnicodeDecodeError,
        CitationPlanContractError,
        CitationPolicyContractError,
        ExperimentFactClosureError,
    ) as exc:
        raise CitationPlanContractError(f"cannot build citation closure: {exc}") from exc
    return build_citation_closure_from_texts(
        paper_text=paper_text,
        structure_report_text=structure_report_text,
        experiment_fact_report_text=experiment_fact_report_text,
        citation_plan_text=plan_text,
        citation_allowlist_text=allowlist_text,
        plan=plan,
        allowlist=allowlist,
    )


def build_citation_closure_from_texts(
    *,
    paper_text: str,
    structure_report_text: str,
    experiment_fact_report_text: str,
    citation_plan_text: str,
    citation_allowlist_text: str,
    plan: Mapping[str, Any] | None = None,
    allowlist: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build closure from captured authority texts without reopening run paths."""

    try:
        plan = dict(plan) if plan is not None else parse_citation_plan(citation_plan_text)
        allowlist = (
            dict(allowlist)
            if allowlist is not None
            else parse_citation_allowlist(citation_allowlist_text)
        )
        structure = _parse_object(structure_report_text, "paper structure report")
        experiment = parse_experiment_fact_closure_report(experiment_fact_report_text)
    except (CitationPlanContractError, ExperimentFactClosureError) as exc:
        raise CitationPlanContractError(f"cannot build citation closure: {exc}") from exc
    if plan["citation_allowlist_sha256"] != sha256_text(citation_allowlist_text):
        raise CitationPlanContractError("citation plan allowlist hash mismatch")
    planned = [
        citation["cite_key"]
        for claim in plan["claims"]
        for citation in claim["planned_citations"]
    ]
    cited = sorted(strict_citation_keys(paper_text))
    unknown = sorted(set(cited) - set(allowlist["eligible_keys"]))
    unplanned = sorted(set(cited) - set(planned))
    missing = sorted(set(planned) - set(cited))
    structure_valid = False
    experiment_valid = experiment.get("valid") is True
    misplaced: list[str] = []
    citation_occurrences: list[dict[str, Any]] = []
    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError:
        document = None
    if document is not None:
        structure_valid = (
            set(structure) == {
                "schema_version", "valid", "source_sha256", "section_count", "issues"
            }
            and structure.get("schema_version") == 1
            and structure.get("valid") is True
            and structure.get("source_sha256") == sha256_text(paper_text)
            and structure.get("section_count") == len(document.sections)
            and structure.get("issues") == []
        )
        if structure_valid:
            citation_occurrences, misplaced = _bind_plan_citation_occurrences(
                document=document,
                plan=plan,
                citation_plan_sha256=sha256_text(citation_plan_text),
            )
    misplaced = sorted(set(misplaced))
    payload = {
        "schema_version": 2,
        "paper_path": "stage-17/paper_draft.md",
        "paper_sha256": sha256_text(paper_text),
        "citation_plan_path": "stage-16/citation_plan.json",
        "citation_plan_sha256": sha256_text(citation_plan_text),
        "cited_keys": cited,
        "unknown_keys": unknown,
        "unplanned_keys": unplanned,
        "missing_planned_keys": missing,
        "misplaced_planned_keys": misplaced,
        "citation_occurrences": citation_occurrences,
        "structure_report_path": "stage-17/paper_structure_report.json",
        "structure_report_sha256": sha256_text(structure_report_text),
        "structure_valid": structure_valid,
        "experiment_fact_closure_report_path": "stage-17/experiment_fact_closure_report.json",
        "experiment_fact_closure_report_sha256": sha256_text(experiment_fact_report_text),
        "experiment_fact_closure_valid": experiment_valid,
        "valid": (
            not unknown and not unplanned and not missing and not misplaced
            and structure_valid and experiment_valid
        ),
    }
    return parse_citation_closure_report(canonical_json_text(payload))


def replay_citation_closure(
    *,
    paper_bytes: bytes,
    structure_report_bytes: bytes,
    experiment_fact_report_bytes: bytes,
    citation_closure_report_bytes: bytes,
    citation_plan_bytes: bytes,
    citation_allowlist_bytes: bytes,
    citation_authority: ReplayedCitationAuthority,
    evidence: CanonicalExperimentEvidence,
) -> dict[str, Any]:
    """Replay captured Stage 17 closure bytes without reopening run inputs."""

    try:
        paper_text = paper_bytes.decode("utf-8")
        structure_text = structure_report_bytes.decode("utf-8")
        fact_text = experiment_fact_report_bytes.decode("utf-8")
        closure_text = citation_closure_report_bytes.decode("utf-8")
        plan_text = citation_plan_bytes.decode("utf-8")
        allowlist_text = citation_allowlist_bytes.decode("utf-8")
        replay_experiment_fact_closure(
            paper_bytes=paper_bytes,
            stored_report_bytes=experiment_fact_report_bytes,
            evidence=evidence,
        )
        stored = parse_citation_closure_report(closure_text)
        if parse_citation_plan(plan_text) != citation_authority.plan:
            raise CitationPlanContractError("citation closure plan is not provenance-replayed")
        if parse_citation_allowlist(allowlist_text) != citation_authority.allowlist:
            raise CitationPlanContractError(
                "citation closure allowlist is not provenance-replayed"
            )
        expected = build_citation_closure_from_texts(
            paper_text=paper_text,
            structure_report_text=structure_text,
            experiment_fact_report_text=fact_text,
            citation_plan_text=plan_text,
            citation_allowlist_text=allowlist_text,
            plan=citation_authority.plan,
            allowlist=citation_authority.allowlist,
        )
    except (UnicodeDecodeError, CitationPlanContractError, ExperimentFactClosureError) as exc:
        raise CitationPlanContractError(f"cannot replay citation closure: {exc}") from exc
    if stored != expected or not stored["valid"]:
        raise CitationPlanContractError("citation closure replay failed")
    return stored


def parse_citation_closure_report(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "citation closure report")
    _exact_keys(
        payload,
        {
            "schema_version", "paper_path", "paper_sha256",
            "citation_plan_path", "citation_plan_sha256", "cited_keys",
            "unknown_keys", "unplanned_keys", "missing_planned_keys",
            "misplaced_planned_keys", "citation_occurrences",
            "structure_report_path", "structure_report_sha256",
            "structure_valid", "experiment_fact_closure_report_path",
            "experiment_fact_closure_report_sha256",
            "experiment_fact_closure_valid", "valid",
        },
        "citation closure report",
    )
    if payload["schema_version"] != 2:
        raise CitationPlanContractError("unsupported citation closure schema")
    expected_paths = {
        "paper_path": "stage-17/paper_draft.md",
        "citation_plan_path": "stage-16/citation_plan.json",
        "structure_report_path": "stage-17/paper_structure_report.json",
        "experiment_fact_closure_report_path": "stage-17/experiment_fact_closure_report.json",
    }
    for field, expected in expected_paths.items():
        if payload[field] != expected:
            raise CitationPlanContractError(f"noncanonical closure {field}")
    for field in (
        "paper_sha256", "citation_plan_sha256", "structure_report_sha256",
        "experiment_fact_closure_report_sha256",
    ):
        _sha256_field(payload, field)
    for field in (
        "cited_keys", "unknown_keys", "unplanned_keys", "missing_planned_keys",
        "misplaced_planned_keys",
    ):
        value = payload[field]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ) or value != sorted(set(value)):
            raise CitationPlanContractError(f"invalid closure {field}")
    occurrences = payload["citation_occurrences"]
    if not isinstance(occurrences, list):
        raise CitationPlanContractError("citation_occurrences must be a list")
    parsed_occurrences = [
        _parse_citation_occurrence_record(
            item,
            citation_plan_sha256=payload["citation_plan_sha256"],
        )
        for item in occurrences
    ]
    if parsed_occurrences != sorted(
        parsed_occurrences,
        key=lambda item: (item["claim_id"], item["cite_key"]),
    ):
        raise CitationPlanContractError("citation occurrence records are not canonical")
    if len({item["claim_id"] for item in parsed_occurrences}) != len(
        parsed_occurrences
    ):
        raise CitationPlanContractError("duplicate citation occurrence claim binding")
    if (
        not isinstance(payload["structure_valid"], bool)
        or not isinstance(payload["experiment_fact_closure_valid"], bool)
        or not isinstance(payload["valid"], bool)
    ):
        raise CitationPlanContractError("closure validity fields must be booleans")
    if payload["valid"]:
        occurrence_keys = sorted(item["cite_key"] for item in parsed_occurrences)
        if not parsed_occurrences or occurrence_keys != payload["cited_keys"]:
            raise CitationPlanContractError(
                "valid citation closure occurrence keys do not match cited_keys"
            )
        expected_claim_ids = [
            f"planned-claim-{index:03d}"
            for index in range(1, len(parsed_occurrences) + 1)
        ]
        if [item["claim_id"] for item in parsed_occurrences] != expected_claim_ids:
            raise CitationPlanContractError(
                "valid citation closure claim IDs are not canonical"
            )
    expected_valid = (
        payload["structure_valid"]
        and payload["experiment_fact_closure_valid"]
        and not payload["unknown_keys"]
        and not payload["unplanned_keys"]
        and not payload["missing_planned_keys"]
        and not payload["misplaced_planned_keys"]
    )
    if payload["valid"] is not expected_valid:
        raise CitationPlanContractError("citation closure valid mismatch")
    return payload


def _bind_plan_citation_occurrences(
    *,
    document: Any,
    plan: Mapping[str, Any],
    citation_plan_sha256: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    indexed: dict[str, list[tuple[Any, CitationOccurrence, str]]] = {}
    for section in document.sections:
        for occurrence in parse_strict_citation_occurrences(section.body):
            sentence = section.body[
                occurrence.sentence_start:occurrence.sentence_end
            ]
            marker_free_sentence = strip_strict_citation_markers(sentence)
            for key in occurrence.keys:
                indexed.setdefault(key, []).append(
                    (section, occurrence, marker_free_sentence)
                )

    records: list[dict[str, Any]] = []
    misplaced: list[str] = []
    for claim in plan["claims"]:
        citation = claim["planned_citations"][0]
        key = citation["cite_key"]
        assigned = claim["section_path"][-1].casefold()
        candidates = indexed.get(key, [])
        exact = [
            item
            for item in candidates
            if item[0].path[0].casefold() == assigned
            and item[2] == claim["claim_text"]
        ]
        if len(candidates) != 1 or len(exact) != 1:
            misplaced.append(key)
            continue
        section, occurrence, sentence = exact[0]
        records.append(
            {
                "claim_id": claim["claim_id"],
                "claim_text_sha256": sha256_text(claim["claim_text"]),
                "cite_key": key,
                "heading": section.path[0],
                "section_id": section.section_id,
                "syntax": occurrence.syntax,
                "marker_keys": list(occurrence.keys),
                "char_start": occurrence.char_start,
                "char_end": occurrence.char_end,
                "sentence_start": occurrence.sentence_start,
                "sentence_end": occurrence.sentence_end,
                "sentence_text": sentence,
                "sentence_sha256": sha256_text(sentence),
                "sentence_ordinal": occurrence.sentence_ordinal,
                "occurrence_ordinal": occurrence.occurrence_ordinal,
                "citation_plan_path": "stage-16/citation_plan.json",
                "citation_plan_sha256": citation_plan_sha256,
            }
        )
    return sorted(records, key=lambda item: (item["claim_id"], item["cite_key"])), sorted(
        set(misplaced)
    )


def _parse_citation_occurrence_record(
    value: Any, *, citation_plan_sha256: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CitationPlanContractError("citation occurrence must be an object")
    expected = {
        "claim_id", "claim_text_sha256", "cite_key", "heading", "section_id",
        "syntax", "marker_keys",
        "char_start", "char_end", "sentence_start", "sentence_end", "sentence_text",
        "sentence_sha256", "sentence_ordinal", "occurrence_ordinal",
        "citation_plan_path", "citation_plan_sha256",
    }
    _exact_keys(value, expected, "citation occurrence")
    for field in ("claim_id", "cite_key", "heading", "section_id", "sentence_text"):
        _required_string(value, field)
    if _STRICT_CITE_KEY_RE.fullmatch(value["cite_key"]) is None:
        raise CitationPlanContractError("invalid citation occurrence cite_key")
    _sha256_field(value, "claim_text_sha256")
    if value["syntax"] not in {"markdown", "latex"}:
        raise CitationPlanContractError("invalid citation occurrence syntax")
    marker_keys = value["marker_keys"]
    if (
        not isinstance(marker_keys, list)
        or not marker_keys
        or any(
            not isinstance(key, str) or _STRICT_CITE_KEY_RE.fullmatch(key) is None
            for key in marker_keys
        )
        or len(marker_keys) != len(set(marker_keys))
        or value["cite_key"] not in marker_keys
    ):
        raise CitationPlanContractError("invalid citation occurrence marker_keys")
    for field in (
        "char_start", "char_end", "sentence_start", "sentence_end",
        "sentence_ordinal", "occurrence_ordinal",
    ):
        if type(value[field]) is not int or value[field] < 0:
            raise CitationPlanContractError(f"invalid citation occurrence {field}")
    if not (
        value["sentence_start"] <= value["char_start"]
        < value["char_end"] <= value["sentence_end"]
    ):
        raise CitationPlanContractError("invalid citation occurrence spans")
    if value["citation_plan_path"] != "stage-16/citation_plan.json":
        raise CitationPlanContractError("invalid citation occurrence plan path")
    _sha256_field(value, "sentence_sha256")
    _sha256_field(value, "citation_plan_sha256")
    if value["sentence_sha256"] != sha256_text(value["sentence_text"]):
        raise CitationPlanContractError("citation occurrence sentence hash mismatch")
    if value["citation_plan_sha256"] != citation_plan_sha256:
        raise CitationPlanContractError("citation occurrence plan hash mismatch")
    return value


def validate_citation_closure_report(
    run_dir: Path,
    config: RCConfig,
    *,
    evidence: CanonicalExperimentEvidence | None = None,
) -> dict[str, Any]:
    paper_path = run_dir / "stage-17" / "paper_draft.md"
    structure_path = run_dir / "stage-17" / "paper_structure_report.json"
    experiment_path = run_dir / "stage-17" / "experiment_fact_closure_report.json"
    report_path = run_dir / "stage-17" / "citation_closure_report.json"
    try:
        paper_text = paper_path.read_text(encoding="utf-8")
        structure_text = structure_path.read_text(encoding="utf-8")
        experiment_text = experiment_path.read_text(encoding="utf-8")
        stored = parse_citation_closure_report(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise CitationPlanContractError(f"cannot read citation closure artifacts: {exc}") from exc
    expected = build_citation_closure_report(
        run_dir,
        config,
        paper_text=paper_text,
        structure_report_text=structure_text,
        experiment_fact_report_text=experiment_text,
        evidence=evidence,
    )
    if stored != expected or not stored["valid"]:
        raise CitationPlanContractError("citation closure replay failed")
    return stored


def _parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CitationPlanContractError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CitationPlanContractError(f"{label} root must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CitationPlanContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise CitationPlanContractError(
            f"{label} fields mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CitationPlanContractError(f"{field} must be a nonempty string")
    return value


def _sha256_field(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CitationPlanContractError(f"invalid {field}")
    return value
