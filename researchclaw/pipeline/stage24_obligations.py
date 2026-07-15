"""Deterministic UTF-8 claim-obligation inventory for canonical Stage 24."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import re
import unicodedata
from typing import Any, Mapping

from markdown_it import MarkdownIt
from markdown_it.token import Token

from researchclaw.pipeline.manuscript_sections import (
    ManuscriptSection,
    ManuscriptStructureError,
    parse_manuscript,
)


CLAIM_OBLIGATION_SCHEMA_VERSION = 1
CLAIM_OBLIGATION_POLICY_VERSION = "claim_obligation_v1"
MARKDOWN_IT_VERSION = "4.2.0"

_KIND_RANK = {
    "numeric_token": 0,
    "citation_instance": 1,
    "comparative_sentence": 2,
    "declarative_sentence": 3,
}
_CITE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*")
_BRACKET_CITATION_RE = re.compile(
    r"\[(?P<body>[ \t]*[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*"
    r"(?:[ \t]*[,;][ \t]*[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*)*[ \t]*)\]"
)
_LATEX_CITATION_RE = re.compile(
    r"\\cite\{"
    r"(?P<body>[ \t]*[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*"
    r"(?:[ \t]*,[ \t]*[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*)*[ \t]*)\}"
)
_COMPARISON_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:better|worse|higher|lower|greater|less|"
    r"outperform|outperforms|outperformed|improve|improves|improved|"
    r"reduce|reduces|reduced|increase|increases|increased|decrease|"
    r"decreases|decreased|versus|vs\.|compared[ \t]+with|"
    r"compared[ \t]+to)(?![A-Za-z0-9_])"
)
_ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "et al.",
    "fig.",
    "eq.",
    "sec.",
    "dr.",
    "mr.",
    "ms.",
    "vs.",
)
_SELECTED_SECTION_ALIASES = {
    "results": frozenset(
        {"results", "experimental results", "evaluation results", "findings"}
    ),
    "discussion": frozenset(
        {"discussion", "results and discussion", "discussion and implications"}
    ),
    "contributions": frozenset(
        {"contributions", "main contributions", "contributions and impact"}
    ),
}
_NUMERIC_CORE_RE = re.compile(
    rb"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    rb"(?:[eE][-+]?\d+)?"
)
_NUMERIC_RUN_BYTES = frozenset(b"0123456789,+.eE-")
_ASCII_WORD_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)
_UNITS = (
    (b"bytes", "bytes"),
    (b"ms", "milliseconds"),
    (b"us", "microseconds"),
    (b"\xc2\xb5s", "microseconds"),
    (b"ns", "nanoseconds"),
    (b"s", "seconds"),
)
_OBLIGATION_ID_RE = re.compile(r"obl-[0-9a-f]{64}")


class ClaimObligationError(ValueError):
    """Raised when claim-obligation bytes or replay are not canonical."""


@dataclass(frozen=True)
class SectionIdentity:
    ordinal: int
    level: int
    normalized_path: tuple[str, ...]
    heading_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "level": self.level,
            "normalized_path": list(self.normalized_path),
            "heading_sha256": self.heading_sha256,
        }


@dataclass(frozen=True)
class ClaimObligation:
    schema_version: int
    policy_version: str
    paper_sha256: str
    obligation_id: str
    kind: str
    section_identity: str | SectionIdentity
    byte_start: int
    byte_end: int
    source_sha256: str
    occurrence_rank: int
    kind_payload: Mapping[str, Any]

    def identity_dict(self) -> dict[str, Any]:
        section = (
            self.section_identity
            if isinstance(self.section_identity, str)
            else self.section_identity.to_dict()
        )
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "paper_sha256": self.paper_sha256,
            "kind": self.kind,
            "section_identity": section,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "source_sha256": self.source_sha256,
            "occurrence_rank": self.occurrence_rank,
            "kind_payload": _plain(self.kind_payload),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"obligation_id": self.obligation_id, **self.identity_dict()}


@dataclass(frozen=True)
class _SourceMaps:
    raw: bytes
    text: str
    char_to_byte: tuple[int, ...]
    line_char_starts: tuple[int, ...]
    line_byte_starts: tuple[int, ...]


@dataclass(frozen=True)
class _Sentence:
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    section: str | SectionIdentity
    selected_class: str | None
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class _ProtectedRanges:
    metadata: tuple[tuple[int, int], ...]
    citation: tuple[tuple[int, int], ...]
    bracket_citation: tuple[tuple[int, int], ...]
    sentence_excluded: tuple[tuple[int, int], ...]


def build_claim_obligation_inventory(paper_bytes: bytes) -> tuple[ClaimObligation, ...]:
    """Build the complete policy-v1 obligation inventory from raw paper bytes."""

    maps = _source_maps(paper_bytes)
    parser = _require_markdown_profile()
    markdown_env: dict[str, Any] = {}
    markdown_tokens = tuple(parser.parse(maps.text, markdown_env))
    try:
        document = parse_manuscript(maps.text, strict=True)
    except ManuscriptStructureError as exc:
        raise ClaimObligationError(f"invalid manuscript structure: {exc}") from exc
    sections = _section_ranges(document.sections, maps)
    protected = _protected_byte_ranges(
        maps,
        markdown_tokens,
        markdown_env,
        parser,
    )
    citations = _citation_instances(maps, protected)
    citation_markers = tuple(
        (marker_start, marker_end)
        for _start, _end, marker_start, marker_end, _key, _rank in citations
    )
    metadata_ranges = tuple(sorted(set((*protected.metadata, *citation_markers))))
    protected = _ProtectedRanges(
        metadata=metadata_ranges,
        citation=protected.citation,
        bracket_citation=protected.bracket_citation,
        sentence_excluded=protected.sentence_excluded,
    )
    paper_sha = _sha256(paper_bytes)
    rows: list[ClaimObligation] = []

    for rank, token in enumerate(_scan_numeric_tokens(maps.raw)):
        start, end, lexeme, unit_lexeme = token
        role = (
            "identifier_metadata"
            if _overlaps_any(start, end, metadata_ranges)
            else "claim_numeric"
        )
        rows.append(
            _obligation(
                paper_sha=paper_sha,
                raw=maps.raw,
                kind="numeric_token",
                section=_section_for_span(start, sections),
                byte_start=start,
                byte_end=end,
                occurrence_rank=rank,
                kind_payload={
                    "numeric_role": role,
                    "number_lexeme": lexeme,
                    "unit_lexeme": unit_lexeme,
                },
            )
        )

    for rank, citation in enumerate(citations):
        start, end, marker_start, marker_end, cite_key, key_rank = citation
        rows.append(
            _obligation(
                paper_sha=paper_sha,
                raw=maps.raw,
                kind="citation_instance",
                section=_section_for_span(start, sections),
                byte_start=start,
                byte_end=end,
                occurrence_rank=rank,
                kind_payload={
                    "cite_key": cite_key,
                    "marker_byte_start": marker_start,
                    "marker_byte_end": marker_end,
                    "marker_sha256": _sha256(maps.raw[marker_start:marker_end]),
                    "key_rank": key_rank,
                },
            )
        )

    sentences = _sentences(maps, document.sections, protected, markdown_tokens)
    comparative_rank = 0
    declarative_rank = 0
    for sentence in sentences:
        if sentence.matched_terms:
            rows.append(
                _obligation(
                    paper_sha=paper_sha,
                    raw=maps.raw,
                    kind="comparative_sentence",
                    section=sentence.section,
                    byte_start=sentence.byte_start,
                    byte_end=sentence.byte_end,
                    occurrence_rank=comparative_rank,
                    kind_payload={"matched_terms": list(sentence.matched_terms)},
                )
            )
            comparative_rank += 1
        if sentence.selected_class is not None:
            rows.append(
                _obligation(
                    paper_sha=paper_sha,
                    raw=maps.raw,
                    kind="declarative_sentence",
                    section=sentence.section,
                    byte_start=sentence.byte_start,
                    byte_end=sentence.byte_end,
                    occurrence_rank=declarative_rank,
                    kind_payload={"selected_section_class": sentence.selected_class},
                )
            )
            declarative_rank += 1

    rows.sort(
        key=lambda row: (
            row.byte_start,
            row.byte_end,
            _KIND_RANK[row.kind],
            row.occurrence_rank,
        )
    )
    seen: set[tuple[str, int, int]] = set()
    for row in rows:
        key = (row.kind, row.byte_start, row.byte_end)
        if key in seen:
            raise ClaimObligationError("duplicate obligation kind/span")
        seen.add(key)
    return tuple(rows)


def parse_claim_obligation_inventory(
    data: bytes,
    *,
    paper_bytes: bytes,
) -> tuple[ClaimObligation, ...]:
    """Parse held-fd bytes and compare them with full canonical reconstruction."""

    text = _strict_utf8_artifact(data, "obligation inventory")
    value = _parse_json(text)
    if not isinstance(value, list):
        raise ClaimObligationError("obligation inventory root is not an array")
    parsed = tuple(_parse_obligation(row, paper_bytes) for row in value)
    expected = build_claim_obligation_inventory(paper_bytes)
    if parsed != expected:
        raise ClaimObligationError("obligation inventory replay mismatch")
    if data != canonical_obligation_inventory_bytes(expected):
        raise ClaimObligationError("obligation inventory bytes are not canonical")
    return parsed


def canonical_obligation_inventory_bytes(
    obligations: tuple[ClaimObligation, ...],
) -> bytes:
    text = json.dumps(
        [row.to_dict() for row in obligations],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    return text.encode("utf-8")


def _strict_utf8_artifact(data: bytes, label: str) -> str:
    if type(data) is not bytes:
        raise ClaimObligationError(f"{label} is not bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ClaimObligationError(f"{label} has a UTF-8 BOM")
    if b"\r" in data:
        raise ClaimObligationError(f"{label} contains CR bytes")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ClaimObligationError(f"{label} is not strict UTF-8") from exc


def _source_maps(raw: bytes) -> _SourceMaps:
    if not isinstance(raw, bytes):
        raise ClaimObligationError("paper source is not bytes")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ClaimObligationError("UTF-8 BOM is forbidden")
    for index, value in enumerate(raw):
        if value == 13 and (index + 1 >= len(raw) or raw[index + 1] != 10):
            raise ClaimObligationError("bare CR is forbidden")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ClaimObligationError("paper is not strict UTF-8") from exc
    char_to_byte = [0]
    for char in text:
        char_to_byte.append(char_to_byte[-1] + len(char.encode("utf-8")))
    line_char_starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            line_char_starts.append(index + 1)
    line_byte_starts = [0]
    line_byte_starts.extend(index + 1 for index, value in enumerate(raw) if value == 10)
    return _SourceMaps(
        raw=raw,
        text=text,
        char_to_byte=tuple(char_to_byte),
        line_char_starts=tuple(line_char_starts),
        line_byte_starts=tuple(line_byte_starts),
    )


def _require_markdown_profile() -> MarkdownIt:
    try:
        installed_version = package_version("markdown-it-py")
    except PackageNotFoundError as exc:
        raise ClaimObligationError("markdown-it-py is not installed") from exc
    if installed_version != MARKDOWN_IT_VERSION:
        raise ClaimObligationError("markdown-it-py version mismatch")
    parser = MarkdownIt("commonmark")
    if parser.options.get("html") is not True or parser.options.get("linkify") is not False:
        raise ClaimObligationError("CommonMark parser profile mismatch")
    return parser


def _section_ranges(
    sections: tuple[ManuscriptSection, ...],
    maps: _SourceMaps,
) -> tuple[tuple[int, int, SectionIdentity], ...]:
    result: list[tuple[int, int, SectionIdentity]] = []
    for section in sections:
        start = maps.line_byte_starts[section.start_line]
        end = (
            maps.line_byte_starts[section.end_line]
            if section.end_line < len(maps.line_byte_starts)
            else len(maps.raw)
        )
        identity = SectionIdentity(
            ordinal=section.ordinal,
            level=section.level,
            normalized_path=tuple(_normalize(component) for component in section.path),
            heading_sha256=_sha256(section.heading_source.encode("utf-8")),
        )
        result.append((start, end, identity))
    return tuple(result)


def _section_for_span(
    start: int,
    sections: tuple[tuple[int, int, SectionIdentity], ...],
) -> str | SectionIdentity:
    for section_start, section_end, identity in sections:
        if section_start <= start < section_end:
            return identity
    return "preamble"


def _protected_byte_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    markdown_env: Mapping[str, Any],
    parser: MarkdownIt,
) -> _ProtectedRanges:
    block_ranges: list[tuple[int, int]] = []
    heading_ranges: list[tuple[int, int]] = []
    for token in tokens:
        if token.map is None:
            continue
        if token.type in {"fence", "code_block", "html_block", "heading_open"}:
            start_line, end_line = token.map
            start = maps.line_byte_starts[start_line]
            end = (
                maps.line_byte_starts[end_line]
                if end_line < len(maps.line_byte_starts)
                else len(maps.raw)
            )
            target = heading_ranges if token.type == "heading_open" else block_ranges
            target.append((start, end))
    inline_code = _inline_code_ranges(maps, tokens, parser)
    html_tags = _html_inline_ranges(
        maps,
        tokens,
        inline_code,
    )
    autolinks = _autolink_ranges(
        maps,
        tokens,
        tuple((*inline_code, *html_tags)),
    )
    bibliography = _bibtex_ranges(maps.raw)
    references, reference_labels = _reference_definitions(maps, markdown_env)
    link_ranges, link_destinations = _commonmark_link_ranges(
        maps,
        tokens,
        parser,
        markdown_env,
        reference_labels,
        tuple((*inline_code, *html_tags, *autolinks)),
    )
    identifiers = _identifier_ranges(maps.text, maps.char_to_byte)
    common_citation = tuple(
        sorted(
            set(
                (
                    *block_ranges,
                    *heading_ranges,
                    *inline_code,
                    *html_tags,
                    *autolinks,
                    *bibliography,
                    *link_ranges,
                    *references,
                )
            )
        )
    )
    latex_commands = _latex_command_ranges(maps, tokens, common_citation)
    return _ProtectedRanges(
        metadata=tuple(
            sorted(
                set(
                    (
                        *block_ranges,
                        *heading_ranges,
                        *inline_code,
                        *link_destinations,
                        *html_tags,
                        *autolinks,
                        *bibliography,
                        *identifiers,
                    )
                )
            )
        ),
        citation=common_citation,
        bracket_citation=tuple(sorted(set((*common_citation, *latex_commands)))),
        sentence_excluded=tuple(sorted(set((*block_ranges, *bibliography)))),
    )


def _inline_code_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    parser: MarkdownIt,
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        if token.type != "inline" or token.map is None:
            continue
        expected = [
            (child.markup, child.content)
            for child in (token.children or ())
            if child.type == "code_inline"
        ]
        start, end = _token_byte_bounds(token, maps)
        candidates = _raw_code_span_candidates(maps.raw, start, end)
        confirmed: list[tuple[tuple[int, int], tuple[str, str]]] = []
        for candidate in candidates:
            source = maps.raw[candidate[0] : candidate[1]].decode("utf-8")
            parsed = parser.parseInline(source, {})
            children = parsed[0].children if len(parsed) == 1 else None
            if (
                children is not None
                and len(children) == 1
                and children[0].type == "code_inline"
            ):
                confirmed.append(
                    (candidate, (children[0].markup, children[0].content))
                )
        if [semantic for _candidate, semantic in confirmed] != expected:
            raise ClaimObligationError("CommonMark inline code source closure mismatch")
        ranges.extend(candidate for candidate, _semantic in confirmed)
    return tuple(ranges)


def _raw_code_span_candidates(
    raw: bytes,
    start: int,
    end: int,
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    index = start
    while index < end:
        if raw[index] != 96 or _is_escaped(raw, index):
            index += 1
            continue
        run_end = index
        while run_end < end and raw[run_end] == 96:
            run_end += 1
        width = run_end - index
        cursor = run_end
        close = None
        while cursor < end:
            if raw[cursor] != 96:
                cursor += 1
                continue
            candidate_end = cursor
            while candidate_end < end and raw[candidate_end] == 96:
                candidate_end += 1
            if candidate_end - cursor == width:
                close = candidate_end
                break
            cursor = candidate_end
        if close is None:
            index = run_end
        else:
            ranges.append((index, close))
            index = close
    return tuple(ranges)


def _raw_inline_link_candidates(
    raw: bytes,
    start: int,
    end: int,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    links: list[tuple[int, int]] = []
    destinations: list[tuple[int, int]] = []
    index = start
    while index < end:
        if raw[index] != 91 or _is_escaped(raw, index):
            index += 1
            continue
        cursor = index + 1
        bracket_depth = 1
        close_label: int | None = None
        while cursor < end:
            if raw[cursor] == 92:
                cursor += 2
                continue
            if raw[cursor] == 91:
                bracket_depth += 1
            elif raw[cursor] == 93:
                bracket_depth -= 1
                if bracket_depth == 0:
                    close_label = cursor
                    break
            cursor += 1
        if close_label is None or raw[close_label + 1 : close_label + 2] != b"(":
            index += 1
            continue
        destination_start = close_label + 2
        cursor = destination_start
        depth = 1
        while cursor < end:
            if raw[cursor] == 92:
                cursor += 2
                continue
            if raw[cursor] == 40:
                depth += 1
            elif raw[cursor] == 41:
                depth -= 1
                if depth == 0:
                    links.append((index, cursor + 1))
                    destinations.append((destination_start, cursor))
                    index = cursor + 1
                    break
            cursor += 1
        else:
            index += 1
    return tuple(links), tuple(destinations)


def _reference_definitions(
    maps: _SourceMaps,
    markdown_env: Mapping[str, Any],
) -> tuple[tuple[tuple[int, int], ...], frozenset[str]]:
    references = markdown_env.get("references", {})
    if not isinstance(references, dict):
        raise ClaimObligationError("CommonMark reference environment is invalid")
    ranges: list[tuple[int, int]] = []
    labels: set[str] = set()
    for label, record in references.items():
        if not isinstance(label, str) or not isinstance(record, dict):
            raise ClaimObligationError("CommonMark reference record is invalid")
        line_map = record.get("map")
        href = record.get("href")
        if (
            not isinstance(line_map, list)
            or len(line_map) != 2
            or any(type(item) is not int for item in line_map)
            or not 0 <= line_map[0] < line_map[1] <= len(maps.line_byte_starts)
            or not isinstance(href, str)
            or not href
        ):
            raise ClaimObligationError("CommonMark reference record is invalid")
        start_line, end_line = line_map
        start = maps.line_byte_starts[start_line]
        end = (
            maps.line_byte_starts[end_line]
            if end_line < len(maps.line_byte_starts)
            else len(maps.raw)
        )
        ranges.append((start, end))
        labels.add(_normalize_reference_label(label))
    return tuple(ranges), frozenset(labels)


def _commonmark_link_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    parser: MarkdownIt,
    markdown_env: Mapping[str, Any],
    defined_labels: frozenset[str],
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    ranges: list[tuple[int, int]] = []
    destinations: list[tuple[int, int]] = []
    references = markdown_env.get("references", {})
    if not isinstance(references, dict):
        raise ClaimObligationError("CommonMark reference environment is invalid")
    for token in tokens:
        if token.type != "inline" or token.map is None:
            continue
        expected_hrefs = [
            child.attrs.get("src") if child.type == "image" else child.attrs.get("href")
            for child in (token.children or ())
            if child.type == "image"
            or (child.type == "link_open" and child.markup != "autolink")
        ]
        start, end = _token_byte_bounds(token, maps)
        direct, direct_destinations = _raw_inline_link_candidates(
            maps.raw,
            start,
            end,
        )
        references_candidates = _raw_reference_link_candidates(
            maps.raw,
            start,
            end,
            defined_labels,
        )
        candidate_destinations = {
            candidate: destination
            for candidate, destination in zip(direct, direct_destinations, strict=True)
        }
        candidates = sorted(set((*direct, *references_candidates)))
        confirmed: list[tuple[tuple[int, int], str]] = []
        for candidate in candidates:
            if _contained_by_any(candidate[0], candidate[1], excluded):
                continue
            source = maps.raw[candidate[0] : candidate[1]].decode("utf-8")
            href = _parse_single_link_fragment(parser, source, references)
            if href is not None:
                confirmed.append((candidate, href))
        if [href for _candidate, href in confirmed] != expected_hrefs:
            raise ClaimObligationError("CommonMark inline link source closure mismatch")
        ranges.extend(candidate for candidate, _href in confirmed)
        destinations.extend(
            candidate_destinations[candidate]
            for candidate, _href in confirmed
            if candidate in candidate_destinations
        )
    return tuple(ranges), tuple(destinations)


def _raw_reference_link_candidates(
    raw: bytes,
    start: int,
    end: int,
    defined_labels: frozenset[str],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    pattern = re.compile(rb"\[(?P<label>[^\]\r\n]+)\](?:\[(?P<target>[^\]\r\n]*)\])?")
    for match in pattern.finditer(raw, start, end):
        if _is_escaped(raw, match.start()):
            continue
        if match.end() < end and raw[match.end()] == 40:
            continue
        label = _normalize_reference_label(match.group("label").decode("utf-8"))
        target_bytes = match.group("target")
        if target_bytes is None:
            target = label
        elif target_bytes:
            target = _normalize_reference_label(target_bytes.decode("utf-8"))
        else:
            target = label
        if target in defined_labels:
            ranges.append(match.span())
    return tuple(ranges)


def _parse_single_link_fragment(
    parser: MarkdownIt,
    source: str,
    references: Mapping[str, Any],
) -> str | None:
    parsed = parser.parseInline(source, {"references": dict(references)})
    if len(parsed) != 1:
        return None
    children = parsed[0].children or ()
    if len(children) == 1 and children[0].type == "image":
        src = children[0].attrs.get("src")
        return src if isinstance(src, str) else None
    if (
        len(children) < 2
        or children[0].type != "link_open"
        or children[0].markup == "autolink"
        or children[-1].type != "link_close"
        or sum(child.type == "link_open" for child in children) != 1
        or sum(child.type == "link_close" for child in children) != 1
    ):
        return None
    href = children[0].attrs.get("href")
    return href if isinstance(href, str) else None


def _normalize_reference_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _latex_command_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        if token.type != "inline" or token.map is None:
            continue
        start, end = _token_byte_bounds(token, maps)
        ranges.extend(_latex_command_ranges_in_block(maps.raw, start, end, excluded))
    return tuple(ranges)


def _latex_command_ranges_in_block(
    raw: bytes,
    start: int,
    end: int,
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    index = start
    while index < end:
        if raw[index] != 92:
            index += 1
            continue
        if _overlaps_any(index, index + 1, excluded):
            index += 1
            continue
        cursor = index + 1
        while cursor < end and (
            65 <= raw[cursor] <= 90 or 97 <= raw[cursor] <= 122
        ):
            cursor += 1
        if cursor == index + 1:
            index += 1
            continue
        while cursor < end and raw[cursor] in b"[{":
            opener = raw[cursor]
            closer = 93 if opener == 91 else 125
            depth = 1
            cursor += 1
            while cursor < end and depth:
                if raw[cursor] == 92:
                    cursor += 2
                    continue
                if raw[cursor] == opener:
                    depth += 1
                elif raw[cursor] == closer:
                    depth -= 1
                cursor += 1
            if depth:
                raise ClaimObligationError("unclosed LaTeX command group")
        ranges.append((index, cursor))
        index = max(cursor, index + 1)
    return tuple(ranges)


def _token_byte_bounds(token: Token, maps: _SourceMaps) -> tuple[int, int]:
    if token.map is None:
        raise ClaimObligationError("CommonMark inline token has no source map")
    start_line, end_line = token.map
    start = maps.line_byte_starts[start_line]
    end = (
        maps.line_byte_starts[end_line]
        if end_line < len(maps.line_byte_starts)
        else len(maps.raw)
    )
    return start, end


def _is_escaped(raw: bytes, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and raw[cursor] == 92:
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _html_inline_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        expected = [
            child.content
            for child in (token.children or ())
            if child.type == "html_inline"
        ]
        if not expected:
            continue
        if token.type != "inline" or token.map is None:
            raise ClaimObligationError("CommonMark inline HTML token has no source map")
        start_line, end_line = token.map
        block_start = maps.line_byte_starts[start_line]
        block_end = (
            maps.line_byte_starts[end_line]
            if end_line < len(maps.line_byte_starts)
            else len(maps.raw)
        )
        candidates = _raw_html_candidates(
            maps.raw,
            block_start,
            block_end,
            excluded,
        )
        candidate_index = 0
        for expected_source in expected:
            while candidate_index < len(candidates):
                candidate = candidates[candidate_index]
                candidate_index += 1
                actual_source = maps.raw[candidate[0] : candidate[1]].decode("utf-8")
                if actual_source == expected_source:
                    ranges.append(candidate)
                    break
            else:
                raise ClaimObligationError(
                    "CommonMark inline HTML source range could not be reconstructed"
                )
    return tuple(ranges)


def _autolink_ranges(
    maps: _SourceMaps,
    tokens: tuple[Token, ...],
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        children = token.children or ()
        expected: list[str] = []
        child_index = 0
        while child_index < len(children):
            child = children[child_index]
            if child.type != "link_open" or child.markup != "autolink":
                child_index += 1
                continue
            if (
                child_index + 2 >= len(children)
                or children[child_index + 1].type != "text"
                or children[child_index + 2].type != "link_close"
                or children[child_index + 2].markup != "autolink"
            ):
                raise ClaimObligationError("CommonMark autolink token sequence is invalid")
            expected.append("<" + children[child_index + 1].content + ">")
            child_index += 3
        if not expected:
            continue
        if token.type != "inline" or token.map is None:
            raise ClaimObligationError("CommonMark autolink token has no source map")
        start_line, end_line = token.map
        block_start = maps.line_byte_starts[start_line]
        block_end = (
            maps.line_byte_starts[end_line]
            if end_line < len(maps.line_byte_starts)
            else len(maps.raw)
        )
        candidates = _raw_html_candidates(
            maps.raw,
            block_start,
            block_end,
            excluded,
        )
        candidate_index = 0
        for expected_source in expected:
            while candidate_index < len(candidates):
                candidate = candidates[candidate_index]
                candidate_index += 1
                actual_source = maps.raw[candidate[0] : candidate[1]].decode("utf-8")
                if actual_source == expected_source:
                    ranges.append(candidate)
                    break
            else:
                raise ClaimObligationError(
                    "CommonMark autolink source range could not be reconstructed"
                )
    return tuple(ranges)


def _raw_html_candidates(
    raw: bytes,
    start: int,
    end: int,
    excluded: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    index = start
    while index < end:
        if raw[index] != 60 or _is_escaped(raw, index):
            index += 1
            continue
        close = _raw_html_candidate_end(raw, index, end)
        if close is None:
            index += 1
            continue
        if not _overlaps_any(index, close, excluded):
            result.append((index, close))
        index = close
    return tuple(result)


def _raw_html_candidate_end(raw: bytes, start: int, end: int) -> int | None:
    terminator: bytes | None = None
    if raw[start : start + 4] == b"<!--":
        terminator = b"-->"
    elif raw[start : start + 9] == b"<![CDATA[":
        terminator = b"]]>"
    elif raw[start : start + 2] == b"<?":
        terminator = b"?>"
    if terminator is not None:
        cursor = start + 2
        while cursor + len(terminator) <= end:
            if raw[cursor : cursor + len(terminator)] == terminator:
                return cursor + len(terminator)
            cursor += 1
        return None

    quote: int | None = None
    cursor = start + 1
    while cursor < end:
        value = raw[cursor]
        if quote is not None:
            if value == quote:
                quote = None
        elif value in (34, 39):
            quote = value
        elif value == 62:
            return cursor + 1
        cursor += 1
    return None


def _bibtex_ranges(raw: bytes) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(raw):
        if raw[index] != 64 or (index > 0 and raw[index - 1] not in b"\r\n"):
            index += 1
            continue
        header = re.match(rb"@[A-Za-z]+[ \t]*\{", raw[index:])
        if header is None:
            index += 1
            continue
        cursor = index + header.end()
        depth = 1
        while cursor < len(raw) and depth:
            if raw[cursor] == 92:
                cursor += 2
                continue
            if raw[cursor] == 123:
                depth += 1
            elif raw[cursor] == 125:
                depth -= 1
            cursor += 1
        if depth:
            raise ClaimObligationError("unterminated bibliography entry")
        ranges.append((index, cursor))
        index = cursor
    return tuple(ranges)


def _identifier_ranges(
    text: str,
    char_to_byte: tuple[int, ...],
) -> tuple[tuple[int, int], ...]:
    patterns = (
        re.compile(r"(?i)\b(?:doi:|https?://doi\.org/)10\.\d{4,9}/\S+"),
        re.compile(r"(?i)\barxiv:\s*\d{4}\.\d{4,5}(?:v\d+)?\b"),
        re.compile(r"(?i)\b(?:figure|fig\.|equation|eq\.|table)\s+\d+\b"),
    )
    result: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            result.append((char_to_byte[match.start()], char_to_byte[match.end()]))
    return tuple(result)


def _scan_numeric_tokens(
    raw: bytes,
) -> tuple[tuple[int, int, str, str | None], ...]:
    result: list[tuple[int, int, str, str | None]] = []
    index = 0
    while index < len(raw):
        if not _numeric_candidate_start(raw, index):
            index += 1
            continue
        start = index
        cursor = index
        while cursor < len(raw) and raw[cursor] in _NUMERIC_RUN_BYTES:
            cursor += 1
        run_end = cursor
        core_end = run_end
        if (
            core_end > start
            and raw[core_end - 1] in b",."
            and (core_end == len(raw) or raw[core_end] in b" \t\r\n")
            and _NUMERIC_CORE_RE.fullmatch(raw[start : core_end - 1]) is not None
        ):
            core_end -= 1
        core = raw[start:core_end]
        if _NUMERIC_CORE_RE.fullmatch(core) is None:
            raise ClaimObligationError(
                f"malformed numeric token at byte {start}: {raw[start:run_end]!r}"
            )
        cursor = core_end
        unit_lexeme: str | None = None
        if cursor < len(raw) and raw[cursor] == 37:
            cursor += 1
            unit_lexeme = "%"
        else:
            unit_start = cursor
            if cursor < len(raw) and raw[cursor] in b" \t":
                cursor += 1
            for suffix, _unit in _UNITS:
                if raw[cursor : cursor + len(suffix)] == suffix:
                    cursor += len(suffix)
                    unit_lexeme = raw[unit_start:cursor].decode("utf-8")
                    break
            else:
                cursor = core_end
        if cursor < len(raw) and raw[cursor] in _ASCII_WORD_BYTES:
            raise ClaimObligationError(f"numeric token has invalid boundary at byte {start}")
        lexeme = core.decode("ascii")
        try:
            Decimal(lexeme.replace(",", ""))
        except InvalidOperation as exc:
            raise ClaimObligationError("numeric token is not finite Decimal") from exc
        result.append((start, cursor, lexeme, unit_lexeme))
        index = max(cursor, run_end)
    return tuple(result)


def _numeric_candidate_start(raw: bytes, index: int) -> bool:
    if index > 0 and raw[index - 1] in _ASCII_WORD_BYTES:
        return False
    value = raw[index]
    if 48 <= value <= 57:
        return True
    if value in b"+-":
        return index + 1 < len(raw) and (
            48 <= raw[index + 1] <= 57
            or (
                raw[index + 1] == 46
                and index + 2 < len(raw)
                and 48 <= raw[index + 2] <= 57
            )
        )
    return (
        value == 46
        and index + 1 < len(raw)
        and 48 <= raw[index + 1] <= 57
    )


def _citation_instances(
    maps: _SourceMaps,
    protected: _ProtectedRanges,
) -> tuple[tuple[int, int, int, int, str, int], ...]:
    result: list[tuple[int, int, int, int, str, int]] = []
    for pattern, excluded in (
        (_LATEX_CITATION_RE, protected.citation),
        (_BRACKET_CITATION_RE, protected.bracket_citation),
    ):
        for marker in pattern.finditer(maps.text):
            marker_start_byte = maps.char_to_byte[marker.start()]
            marker_end_byte = maps.char_to_byte[marker.end()]
            if _is_escaped(maps.raw, marker_start_byte) or _overlaps_any(
                marker_start_byte,
                marker_end_byte,
                excluded,
            ):
                continue
            body_start, body_end = marker.span("body")
            body = maps.text[body_start:body_end]
            key_rank = 0
            for key_match in _CITE_KEY_RE.finditer(body):
                start_char = body_start + key_match.start()
                end_char = body_start + key_match.end()
                result.append(
                    (
                        maps.char_to_byte[start_char],
                        maps.char_to_byte[end_char],
                        maps.char_to_byte[marker.start()],
                        maps.char_to_byte[marker.end()],
                        key_match.group(0),
                        key_rank,
                    )
                )
                key_rank += 1
    result.sort(key=lambda item: (item[0], item[1], item[5]))
    return tuple(result)


def _sentences(
    maps: _SourceMaps,
    sections: tuple[ManuscriptSection, ...],
    protected: _ProtectedRanges,
    tokens: tuple[Token, ...],
) -> tuple[_Sentence, ...]:
    section_by_line: list[tuple[int, int, SectionIdentity, str | None]] = []
    for section in sections:
        identity = SectionIdentity(
            ordinal=section.ordinal,
            level=section.level,
            normalized_path=tuple(_normalize(component) for component in section.path),
            heading_sha256=_sha256(section.heading_source.encode("utf-8")),
        )
        selected = _selected_section_class(section.path)
        heading_lines = section.heading_source.count("\n") or 1
        section_by_line.append(
            (section.start_line + heading_lines, section.end_line, identity, selected)
        )
    result: list[_Sentence] = []
    for token in tokens:
        if token.type != "paragraph_open" or token.map is None:
            continue
        start_line, end_line = token.map
        section: str | SectionIdentity = "preamble"
        selected: str | None = None
        for body_start, body_end, identity, section_class in section_by_line:
            if body_start <= start_line < body_end:
                section = identity
                selected = section_class
                break
        char_start = maps.line_char_starts[start_line]
        char_end = (
            maps.line_char_starts[end_line]
            if end_line < len(maps.line_char_starts)
            else len(maps.text)
        )
        block = maps.text[char_start:char_end]
        marker = re.match(r"[ \t]*(?:[-+*]|\d+[.)])[ \t]+", block)
        if marker is not None:
            char_start += marker.end()
            block = block[marker.end() :]
        for local_start, local_end in _sentence_char_spans(block):
            absolute_start = char_start + local_start
            absolute_end = char_start + local_end
            sentence_text = maps.text[absolute_start:absolute_end]
            byte_start = maps.char_to_byte[absolute_start]
            byte_end = maps.char_to_byte[absolute_end]
            if _contained_by_any(
                byte_start,
                byte_end,
                protected.sentence_excluded,
            ) or _contained_by_any(byte_start, byte_end, protected.metadata):
                continue
            terms = tuple(
                match.group(0)
                for match in _COMPARISON_RE.finditer(sentence_text)
                if not _overlaps_any(
                    maps.char_to_byte[absolute_start + match.start()],
                    maps.char_to_byte[absolute_start + match.end()],
                    protected.metadata,
                )
            )
            result.append(
                _Sentence(
                    char_start=absolute_start,
                    char_end=absolute_end,
                    byte_start=byte_start,
                    byte_end=byte_end,
                    section=section,
                    selected_class=selected,
                    matched_terms=terms,
                )
            )
    result.sort(key=lambda row: (row.byte_start, row.byte_end))
    return tuple(result)


def _sentence_char_spans(text: str) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in ".!?":
            index += 1
            continue
        end = index + 1
        if end < len(text) and not text[end].isspace():
            index += 1
            continue
        prefix = re.sub(r"[ \t]+", " ", text[start:end]).lower()
        if any(prefix.endswith(abbreviation) for abbreviation in _ABBREVIATIONS):
            index += 1
            continue
        span = _trim_span(text, start, end)
        if span is not None:
            result.append(span)
        while end < len(text) and text[end].isspace():
            end += 1
        start = end
        index = end
    span = _trim_span(text, start, len(text))
    if span is not None:
        result.append(span)
    return tuple(result)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _selected_section_class(path: tuple[str, ...]) -> str | None:
    """Apply policy v1 nearest-matching-node inheritance over a section path."""

    for title in reversed(path):
        normalized = _normalize(title)
        for section_class, aliases in _SELECTED_SECTION_ALIASES.items():
            if normalized in aliases:
                return section_class
    return None


def _obligation(
    *,
    paper_sha: str,
    raw: bytes,
    kind: str,
    section: str | SectionIdentity,
    byte_start: int,
    byte_end: int,
    occurrence_rank: int,
    kind_payload: Mapping[str, Any],
) -> ClaimObligation:
    base = ClaimObligation(
        schema_version=CLAIM_OBLIGATION_SCHEMA_VERSION,
        policy_version=CLAIM_OBLIGATION_POLICY_VERSION,
        paper_sha256=paper_sha,
        obligation_id="",
        kind=kind,
        section_identity=section,
        byte_start=byte_start,
        byte_end=byte_end,
        source_sha256=_sha256(raw[byte_start:byte_end]),
        occurrence_rank=occurrence_rank,
        kind_payload=dict(kind_payload),
    )
    return ClaimObligation(
        **{
            **base.__dict__,
            "obligation_id": "obl-" + _identity_sha256(base.identity_dict()),
        }
    )


def _parse_obligation(value: Any, paper_bytes: bytes) -> ClaimObligation:
    fields = {
        "obligation_id",
        "schema_version",
        "policy_version",
        "paper_sha256",
        "kind",
        "section_identity",
        "byte_start",
        "byte_end",
        "source_sha256",
        "occurrence_rank",
        "kind_payload",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ClaimObligationError("obligation fields mismatch")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != CLAIM_OBLIGATION_SCHEMA_VERSION
    ):
        raise ClaimObligationError("obligation schema version mismatch")
    if (
        type(value["policy_version"]) is not str
        or value["policy_version"] != CLAIM_OBLIGATION_POLICY_VERSION
    ):
        raise ClaimObligationError("obligation policy version mismatch")
    if type(value["kind"]) is not str or value["kind"] not in _KIND_RANK:
        raise ClaimObligationError("obligation kind is invalid")
    if type(value["byte_start"]) is not int or type(value["byte_end"]) is not int:
        raise ClaimObligationError("obligation byte span is invalid")
    if (
        value["byte_start"] < 0
        or value["byte_end"] <= value["byte_start"]
        or value["byte_end"] > len(paper_bytes)
    ):
        raise ClaimObligationError("obligation byte span is invalid")
    if type(value["occurrence_rank"]) is not int or value["occurrence_rank"] < 0:
        raise ClaimObligationError("obligation occurrence rank is invalid")
    section = _parse_section_identity(value["section_identity"])
    payload = _parse_kind_payload(value["kind"], value["kind_payload"])
    result = ClaimObligation(
        schema_version=1,
        policy_version=CLAIM_OBLIGATION_POLICY_VERSION,
        paper_sha256=_require_sha(value["paper_sha256"], "paper hash"),
        obligation_id=_require_obligation_id(value["obligation_id"]),
        kind=value["kind"],
        section_identity=section,
        byte_start=value["byte_start"],
        byte_end=value["byte_end"],
        source_sha256=_require_sha(value["source_sha256"], "source hash"),
        occurrence_rank=value["occurrence_rank"],
        kind_payload=payload,
    )
    if result.paper_sha256 != _sha256(paper_bytes):
        raise ClaimObligationError("obligation paper hash mismatch")
    if result.source_sha256 != _sha256(paper_bytes[result.byte_start : result.byte_end]):
        raise ClaimObligationError("obligation source hash mismatch")
    expected_id = "obl-" + _identity_sha256(result.identity_dict())
    if result.obligation_id != expected_id:
        raise ClaimObligationError("obligation ID mismatch")
    return result


def _parse_section_identity(value: Any) -> str | SectionIdentity:
    if value == "preamble":
        return "preamble"
    fields = {"ordinal", "level", "normalized_path", "heading_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ClaimObligationError("section identity fields mismatch")
    if type(value["ordinal"]) is not int or value["ordinal"] < 0:
        raise ClaimObligationError("section ordinal is invalid")
    if type(value["level"]) is not int or not 1 <= value["level"] <= 6:
        raise ClaimObligationError("section level is invalid")
    path = value["normalized_path"]
    if not isinstance(path, list) or not path:
        raise ClaimObligationError("section normalized path is invalid")
    normalized = tuple(_require_canonical_string(item, "section path") for item in path)
    if tuple(_normalize(item) for item in normalized) != normalized:
        raise ClaimObligationError("section normalized path is not canonical")
    return SectionIdentity(
        ordinal=value["ordinal"],
        level=value["level"],
        normalized_path=normalized,
        heading_sha256=_require_sha(value["heading_sha256"], "heading hash"),
    )


def _parse_kind_payload(kind: str, value: Any) -> Mapping[str, Any]:
    expected = {
        "numeric_token": {
            "numeric_role",
            "number_lexeme",
            "unit_lexeme",
        },
        "citation_instance": {
            "cite_key",
            "marker_byte_start",
            "marker_byte_end",
            "marker_sha256",
            "key_rank",
        },
        "comparative_sentence": {"matched_terms"},
        "declarative_sentence": {"selected_section_class"},
    }[kind]
    if not isinstance(value, dict) or set(value) != expected:
        raise ClaimObligationError("obligation kind payload fields mismatch")
    if kind == "numeric_token":
        role = value["numeric_role"]
        if type(role) is not str or role not in {
            "claim_numeric",
            "identifier_metadata",
        }:
            raise ClaimObligationError("numeric role is invalid")
        lexeme = _require_canonical_string(value["number_lexeme"], "number lexeme")
        try:
            encoded_lexeme = lexeme.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ClaimObligationError("number lexeme is invalid") from exc
        if _NUMERIC_CORE_RE.fullmatch(encoded_lexeme) is None:
            raise ClaimObligationError("number lexeme is invalid")
        unit = value["unit_lexeme"]
        valid_units: set[str | None] = {None, "%"}
        for suffix, _normalized in _UNITS:
            decoded = suffix.decode("utf-8")
            valid_units.update({decoded, " " + decoded, "\t" + decoded})
        if unit is not None and type(unit) is not str:
            raise ClaimObligationError("numeric unit lexeme is invalid")
        if unit not in valid_units:
            raise ClaimObligationError("numeric unit lexeme is invalid")
    elif kind == "citation_instance":
        cite_key = _require_canonical_string(value["cite_key"], "citation key")
        if _CITE_KEY_RE.fullmatch(cite_key) is None:
            raise ClaimObligationError("citation key is invalid")
        for field in ("marker_byte_start", "marker_byte_end", "key_rank"):
            if type(value[field]) is not int or value[field] < 0:
                raise ClaimObligationError(f"citation {field} is invalid")
        if value["marker_byte_end"] <= value["marker_byte_start"]:
            raise ClaimObligationError("citation marker span is invalid")
        _require_sha(value["marker_sha256"], "citation marker hash")
    elif kind == "comparative_sentence":
        terms = value["matched_terms"]
        if not isinstance(terms, list) or not terms:
            raise ClaimObligationError("comparison terms are invalid")
        for term in terms:
            canonical = _require_canonical_string(term, "comparison term")
            if _COMPARISON_RE.fullmatch(canonical) is None:
                raise ClaimObligationError("comparison term is invalid")
    else:
        selected = value["selected_section_class"]
        if type(selected) is not str or selected not in _SELECTED_SECTION_ALIASES:
            raise ClaimObligationError("selected section class is invalid")
    return dict(value)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ClaimObligationError(f"invalid obligation JSON: {exc}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClaimObligationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ClaimObligationError(f"{label} is invalid")
    return value


def _require_obligation_id(value: Any) -> str:
    if not isinstance(value, str) or _OBLIGATION_ID_RE.fullmatch(value) is None:
        raise ClaimObligationError("obligation ID is invalid")
    return value


def _require_canonical_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ClaimObligationError(f"{label} is invalid")
    return value


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _overlaps_any(
    start: int,
    end: int,
    ranges: tuple[tuple[int, int], ...],
) -> bool:
    return any(start < range_end and end > range_start for range_start, range_end in ranges)


def _contained_by_any(
    start: int,
    end: int,
    ranges: tuple[tuple[int, int], ...],
) -> bool:
    return any(range_start <= start and end <= range_end for range_start, range_end in ranges)


def _identity_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(
            _plain(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
