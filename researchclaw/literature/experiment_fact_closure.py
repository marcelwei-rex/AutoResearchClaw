"""Deterministic Stage 17 experiment-fact closure over canonical artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from researchclaw.experiment_runtime.contract import validate_contract_dict
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    load_canonical_experiment_evidence,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    merge_manuscript,
    parse_manuscript,
)


EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION = 2


class ExperimentFactClosureError(ValueError):
    """Raised when manuscript experiment facts cannot be replayed."""


_DECIMAL_METRIC_RE = re.compile(
    r"(?<![\w.])[-+]?\d+\.\d+(?:[eE][-+]?\d+)?%?"
    r"|(?<![\w.])[-+]?\d+[eE][-+]?\d+%?"
    r"|(?<![\w.])[-+]?\d+%"
)
_INTEGER_UNIT_RE = re.compile(
    r"(?<![\w.])(?P<number>[-+]?\d+)(?="
    r"\s*(?:fps|cycles?|milliseconds?|ms|seconds?|s|samples?|windows?|"
    r"runs?|trials?|seeds?|iterations?|epochs?)\b|x\b)",
    re.I,
)
_CITATION_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*\]")
_SYNTHETIC_CONTRADICTIONS = (
    re.compile(r"\b(?:measured|collected|captured|recorded)\s+(?:on|from)\s+(?:real|physical)\s+hardware\b", re.I),
    re.compile(r"\breal[- ]hardware\s+(?:measurements?|traces?|counters?|experiments?)\b", re.I),
    re.compile(r"\bpublic\s+(?:hpc\s+)?dataset(?:s)?\s+(?:was|were)\s+used\b", re.I),
    re.compile(r"\bcaptured\s+on\s+(?:our|a|the)\s+(?:fpga|prototype|physical)\b", re.I),
    re.compile(r"\bcollected\s+[^.\n]{0,60}\bfrom\s+(?:a|an|the)?\s*physical\b", re.I),
    re.compile(r"\bpublic\s+(?:hpc\s+)?benchmark\s+suite\b", re.I),
    re.compile(r"\bpublic\s+benchmark(?:s|\s+dataset|\s+suite)?\b", re.I),
    re.compile(r"\bSPEC\s*CPU\s*2006\b", re.I),
)
_PUBLIC_CONTRADICTIONS = (
    re.compile(r"\b(?:our|this)\s+(?:fpga|cpu|gpu|prototype|device)\s+(?:measurements?|traces?)\b", re.I),
    re.compile(r"\bcollected\s+[^.\n]{0,60}\bfrom\s+(?:our|a|the)\s+(?:physical|local)\b", re.I),
)
_LOCAL_HARDWARE_CONTRADICTIONS = (
    re.compile(r"\bpublic\s+(?:hpc\s+)?dataset(?:s)?\s+(?:was|were)\s+used\b", re.I),
)


def find_dataset_claim_violations(
    paper_text: str, dataset_origin: str
) -> tuple[str, ...]:
    """Return deterministic dataset-origin contradictions in manuscript text."""

    patterns = {
        "synthetic": _SYNTHETIC_CONTRADICTIONS,
        "public": _PUBLIC_CONTRADICTIONS,
        "local_hardware": _LOCAL_HARDWARE_CONTRADICTIONS,
    }.get(dataset_origin)
    if patterns is None:
        raise ExperimentFactClosureError("invalid dataset_origin")
    violations: list[str] = []
    for pattern in patterns:
        violations.extend(match.group(0) for match in pattern.finditer(paper_text))
    return tuple(sorted(set(violations)))


def build_experiment_fact_closure_report(
    run_dir: Path,
    *,
    paper_text: str,
    evidence: CanonicalExperimentEvidence | None = None,
) -> dict[str, Any]:
    try:
        evidence = evidence or load_canonical_experiment_evidence(run_dir)
        contract_text = evidence.experiment_contract_bytes.decode("utf-8")
        contract_data = yaml.safe_load(contract_text)
        if not isinstance(contract_data, dict):
            raise ValueError("contract root must be an object")
        contract = validate_contract_dict(contract_data)
    except (
        CanonicalExperimentEvidenceError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise ExperimentFactClosureError(f"cannot load experiment contract: {exc}") from exc

    return build_experiment_fact_closure_from_text(
        paper_text=paper_text,
        evidence=evidence,
        contract=contract,
    )


def build_experiment_fact_closure_from_text(
    *,
    paper_text: str,
    evidence: CanonicalExperimentEvidence,
    contract: Any,
) -> dict[str, Any]:
    """Build closure from already-bound paper bytes and canonical evidence."""

    grounded: list[Decimal] = []
    _collect_numbers(evidence.metric_observations, grounded)
    _collect_numbers(evidence.structured_results, grounded)
    source_records = [
        {
            "path": evidence.selected_result_manifest_path,
            "sha256": evidence.selected_result_manifest_sha256,
        },
        {
            "path": evidence.candidate_manifest_path,
            "sha256": evidence.candidate_manifest_sha256,
        },
    ]
    if not grounded:
        raise ExperimentFactClosureError("no grounded metric values were found")

    manuscript_literals = _extract_experiment_metric_literals(paper_text)
    manuscript_values = [value for value, _is_percent in manuscript_literals]
    unknown_values = [
        value
        for value, is_percent in manuscript_literals
        if not any(
            _numeric_equivalent(value, expected, is_percent=is_percent)
            for expected in grounded
        )
    ]
    dataset_violations = list(
        find_dataset_claim_violations(paper_text, contract.dataset_origin)
    )
    payload = {
        "schema_version": EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION,
        "paper_path": "stage-17/paper_draft.md",
        "paper_sha256": _sha256(paper_text),
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "experiment_contract_path": evidence.experiment_contract_path,
        "experiment_contract_sha256": evidence.experiment_contract_sha256,
        "dataset_origin": contract.dataset_origin,
        "metric_sources": source_records,
        "grounded_numeric_values": sorted(set(grounded)),
        "manuscript_numeric_values": manuscript_values,
        "unknown_numeric_values": unknown_values,
        "dataset_claim_violations": sorted(set(dataset_violations)),
        "valid": not unknown_values and not dataset_violations,
    }
    return parse_experiment_fact_closure_report(
        canonical_experiment_fact_json_text(payload)
    )


def replay_experiment_fact_closure(
    *,
    paper_bytes: bytes,
    stored_report_bytes: bytes,
    evidence: CanonicalExperimentEvidence,
) -> dict[str, Any]:
    """Replay a captured closure report without reopening any run artifact."""

    try:
        paper_text = paper_bytes.decode("utf-8")
        stored_text = stored_report_bytes.decode("utf-8")
        contract_text = evidence.experiment_contract_bytes.decode("utf-8")
        contract_data = yaml.safe_load(contract_text)
        if not isinstance(contract_data, dict):
            raise ValueError("contract root must be an object")
        contract = validate_contract_dict(contract_data)
        stored = parse_experiment_fact_closure_report(stored_text)
        expected = build_experiment_fact_closure_from_text(
            paper_text=paper_text,
            evidence=evidence,
            contract=contract,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        yaml.YAMLError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        raise ExperimentFactClosureError(f"cannot replay experiment closure: {exc}") from exc
    if stored != expected or not stored["valid"]:
        raise ExperimentFactClosureError("experiment fact closure replay failed")
    return stored


def parse_experiment_fact_closure_report(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
        raise ExperimentFactClosureError(f"invalid experiment closure JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExperimentFactClosureError("experiment closure root must be an object")
    expected = {
        "schema_version", "paper_path", "paper_sha256", "experiment_contract_path",
        "experiment_contract_sha256", "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256", "dataset_origin", "metric_sources",
        "grounded_numeric_values", "manuscript_numeric_values",
        "unknown_numeric_values", "dataset_claim_violations", "valid",
    }
    if set(payload) != expected:
        raise ExperimentFactClosureError("experiment closure fields mismatch")
    if payload["schema_version"] != EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION:
        raise ExperimentFactClosureError("unsupported experiment closure schema")
    if payload["paper_path"] != "stage-17/paper_draft.md":
        raise ExperimentFactClosureError("noncanonical experiment closure paper path")
    _safe_relative_path(payload["experiment_contract_path"])
    if payload["canonical_experiment_evidence_path"] != "canonical_experiment_evidence.json":
        raise ExperimentFactClosureError("noncanonical evidence manifest path")
    for field in (
        "paper_sha256",
        "experiment_contract_sha256",
        "canonical_experiment_evidence_sha256",
    ):
        if not isinstance(payload[field], str) or re.fullmatch(r"[0-9a-f]{64}", payload[field]) is None:
            raise ExperimentFactClosureError(f"invalid {field}")
    if payload["dataset_origin"] not in {"synthetic", "public", "local_hardware"}:
        raise ExperimentFactClosureError("invalid dataset_origin")
    if not isinstance(payload["metric_sources"], list) or not payload["metric_sources"]:
        raise ExperimentFactClosureError("metric_sources must not be empty")
    for source in payload["metric_sources"]:
        if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
            raise ExperimentFactClosureError("invalid metric source record")
        _safe_relative_path(source["path"])
        if re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"])) is None:
            raise ExperimentFactClosureError("invalid metric source hash")
    if payload["metric_sources"] != sorted(payload["metric_sources"], key=lambda row: row["path"]):
        raise ExperimentFactClosureError("metric sources are not canonical")
    for field in ("grounded_numeric_values", "manuscript_numeric_values", "unknown_numeric_values"):
        values = payload[field]
        if not isinstance(values, list) or any(
            not _is_finite_decimal_number(value)
            for value in values
        ):
            raise ExperimentFactClosureError(f"invalid {field}")
    violations = payload["dataset_claim_violations"]
    if not isinstance(violations, list) or any(not isinstance(item, str) or not item for item in violations):
        raise ExperimentFactClosureError("invalid dataset_claim_violations")
    if not isinstance(payload["valid"], bool):
        raise ExperimentFactClosureError("valid must be boolean")
    expected_valid = not payload["unknown_numeric_values"] and not violations
    if payload["valid"] is not expected_valid:
        raise ExperimentFactClosureError("experiment closure valid mismatch")
    return payload


def validate_experiment_fact_closure_report(
    run_dir: Path,
    *,
    evidence: CanonicalExperimentEvidence | None = None,
) -> dict[str, Any]:
    paper_path = run_dir / "stage-17" / "paper_draft.md"
    report_path = run_dir / "stage-17" / "experiment_fact_closure_report.json"
    try:
        paper_bytes = paper_path.read_bytes()
        report_bytes = report_path.read_bytes()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExperimentFactClosureError(f"cannot read experiment closure artifacts: {exc}") from exc
    if evidence is None:
        try:
            evidence = load_canonical_experiment_evidence(run_dir)
        except CanonicalExperimentEvidenceError as exc:
            raise ExperimentFactClosureError(
                f"canonical experiment evidence is invalid: {exc}"
            ) from exc
    return replay_experiment_fact_closure(
        paper_bytes=paper_bytes,
        stored_report_bytes=report_bytes,
        evidence=evidence,
    )


def _collect_numbers(value: Any, output: list[Decimal]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExperimentFactClosureError(
                "canonical experiment evidence contains nonfinite authority"
            )
        output.append(value)
    elif isinstance(value, int):
        output.append(Decimal(value))
    elif isinstance(value, float):
        raise ExperimentFactClosureError(
            "canonical experiment evidence contains binary float authority"
        )
    elif isinstance(value, Mapping):
        for child in value.values():
            _collect_numbers(child, output)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _collect_numbers(child, output)


def _extract_metric_literals(text: str) -> list[tuple[Decimal, bool]]:
    prose = _CITATION_RE.sub("", text)
    values: list[tuple[int, Decimal, bool]] = []
    occupied: list[tuple[int, int]] = []
    for match in _DECIMAL_METRIC_RE.finditer(prose):
        token = match.group(0)
        percent = token.endswith("%")
        value = _decimal_token(token[:-1] if percent else token)
        values.append(
            (match.start(), value / Decimal(100) if percent else value, percent)
        )
        occupied.append(match.span())
    for match in _INTEGER_UNIT_RE.finditer(prose):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        values.append((match.start(), _decimal_token(match.group("number")), False))
    return [(value, percent) for _position, value, percent in sorted(values)]


def _extract_experiment_metric_literals(text: str) -> list[tuple[Decimal, bool]]:
    try:
        document = parse_manuscript(text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for experiment closure: {exc}"
        ) from exc
    section_texts = [
        section.body
        for section in document.sections
        if any(
            token in section.title.casefold()
            for token in (
                "abstract", "result", "experiment", "ablation", "evaluation",
                "discussion", "conclusion",
            )
        )
    ]
    return _extract_metric_literals("\n".join(section_texts))


def remove_unsupported_experiment_fact_blocks(
    paper_text: str,
    *,
    grounded_numeric_values: list[Decimal],
    dataset_origin: str,
) -> tuple[str, dict[str, Any]]:
    """Remove the smallest deterministic blocks containing unsupported facts.

    The repair never invents replacement prose. Text outside the reported body
    spans is preserved byte-for-byte; headings are never eligible for removal.
    """
    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for experiment repair: {exc}"
        ) from exc
    patterns = {
        "synthetic": _SYNTHETIC_CONTRADICTIONS,
        "public": _PUBLIC_CONTRADICTIONS,
        "local_hardware": _LOCAL_HARDWARE_CONTRADICTIONS,
    }.get(dataset_origin)
    if patterns is None:
        raise ExperimentFactClosureError("invalid dataset_origin")

    replacements: dict[str, str] = {}
    operations: list[dict[str, Any]] = []
    for section in document.sections:
        body = section.body
        targets: list[tuple[int, str, Decimal | None]] = []
        if _is_experiment_fact_section(section.title):
            for start, _end, value, is_percent in _metric_literal_spans(body):
                if not any(
                    _numeric_equivalent(value, expected, is_percent=is_percent)
                    for expected in grounded_numeric_values
                ):
                    targets.append((start, "unsupported_numeric", value))
        for pattern in patterns:
            targets.extend(
                (match.start(), "dataset_origin_contradiction", None)
                for match in pattern.finditer(body)
            )
        if not targets:
            continue

        spans: list[tuple[int, int, str, list[Decimal]]] = []
        for position, reason, value in targets:
            start, end, block_type = _safe_fact_block_span(body, position)
            values = [] if value is None else [value]
            spans.append((start, end, block_type, values))
        merged = _merge_repair_spans(spans)
        repaired = body
        for start, end, block_type, values in reversed(merged):
            removed = repaired[start:end]
            repaired = repaired[:start] + repaired[end:]
            operations.append(
                {
                    "section_id": section.section_id,
                    "block_type": block_type,
                    "body_char_start": start,
                    "body_char_end": end,
                    "removed_sha256": _sha256(removed),
                    "unknown_numeric_values": sorted(set(values)),
                }
            )
        replacements[section.section_id] = repaired

    repaired_text = merge_manuscript(document, replacements)
    log = {
        "schema_version": 1,
        "_diagnostic": True,
        "strategy": "deterministic_block_removal",
        "source_sha256": _sha256(paper_text),
        "repaired_sha256": _sha256(repaired_text),
        "operations": sorted(
            operations,
            key=lambda row: (row["section_id"], row["body_char_start"]),
        ),
    }
    return repaired_text, log


def _is_experiment_fact_section(title: str) -> bool:
    return any(
        token in title.casefold()
        for token in (
            "abstract", "result", "experiment", "ablation", "evaluation",
            "discussion", "conclusion",
        )
    )


def _metric_literal_spans(text: str) -> list[tuple[int, int, Decimal, bool]]:
    spans: list[tuple[int, int, Decimal, bool]] = []
    occupied: list[tuple[int, int]] = []
    for match in _DECIMAL_METRIC_RE.finditer(text):
        token = match.group(0)
        percent = token.endswith("%")
        value = _decimal_token(token[:-1] if percent else token)
        spans.append(
            (
                match.start(),
                match.end(),
                value / Decimal(100) if percent else value,
                percent,
            )
        )
        occupied.append(match.span())
    for match in _INTEGER_UNIT_RE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        spans.append(
            (
                match.start(),
                match.end(),
                _decimal_token(match.group("number")),
                False,
            )
        )
    return sorted(spans)


def _safe_fact_block_span(text: str, position: int) -> tuple[int, int, str]:
    line_start = text.rfind("\n", 0, position) + 1
    line_end_marker = text.find("\n", position)
    line_end = len(text) if line_end_marker < 0 else line_end_marker + 1
    line = text[line_start:line_end]
    stripped = line.strip()

    display = _enclosing_display_math_span(text, position)
    if display is not None:
        return display[0], display[1], "display_math"
    if (
        stripped.startswith("|")
        or ("&" in line and stripped.endswith(r"\\"))
    ):
        return line_start, line_end, "table_row"
    if stripped.startswith("**Figure") or stripped.startswith("**Table") or "\\caption{" in line:
        return line_start, line_end, "caption"
    if re.match(r"\s*(?:[-+*]|\d+[.)])\s+", line):
        end = line_end
        while end < len(text):
            next_end_marker = text.find("\n", end)
            next_end = len(text) if next_end_marker < 0 else next_end_marker + 1
            next_line = text[end:next_end]
            if not next_line.strip() or re.match(r"\s*(?:[-+*]|\d+[.)])\s+", next_line):
                break
            end = next_end
        return line_start, end, "list_item"

    paragraph_start = text.rfind("\n\n", 0, position) + 2
    paragraph_end_marker = text.find("\n\n", position)
    paragraph_end = len(text) if paragraph_end_marker < 0 else paragraph_end_marker
    sentence = _sentence_span(text, position, paragraph_start, paragraph_end)
    if sentence is not None:
        return sentence[0], sentence[1], "sentence"
    return paragraph_start, paragraph_end, "paragraph"


def _enclosing_display_math_span(text: str, position: int) -> tuple[int, int] | None:
    for opener, closer in (("$$", "$$"), (r"\[", r"\]")):
        starts = [match.start() for match in re.finditer(re.escape(opener), text[:position])]
        if opener == closer:
            start = starts[-1] if len(starts) % 2 == 1 else -1
        else:
            last_close = text.rfind(closer, 0, position)
            start = starts[-1] if starts and starts[-1] > last_close else -1
        if start >= 0:
            end_marker = text.find(closer, position)
            if end_marker >= position:
                return start, end_marker + len(closer)
            raise ExperimentFactClosureError(
                "unsupported fact is inside unbalanced display math"
            )
    begin = text.rfind(r"\begin{equation}", 0, position + 1)
    prior_end = text.rfind(r"\end{equation}", 0, position)
    if begin >= 0 and begin > prior_end:
        end_marker = text.find(r"\end{equation}", position)
        if end_marker >= position:
            return begin, end_marker + len(r"\end{equation}")
        raise ExperimentFactClosureError(
            "unsupported fact is inside unbalanced equation environment"
        )
    return None


def _sentence_span(
    text: str, position: int, paragraph_start: int, paragraph_end: int
) -> tuple[int, int] | None:
    paragraph = text[paragraph_start:paragraph_end]
    relative = position - paragraph_start
    boundaries = [0]
    boundaries.extend(
        match.end()
        for match in re.finditer(r"[.!?][\"')\]]*(?:[ \t]+|\n+)", paragraph)
        if not _is_abbreviation_boundary(paragraph, match.start())
    )
    boundaries.append(len(paragraph))
    for start, end in zip(boundaries, boundaries[1:], strict=True):
        if start <= relative < end:
            while start < end and paragraph[start].isspace():
                start += 1
            if start < end:
                return paragraph_start + start, paragraph_start + end
    return None


def _is_abbreviation_boundary(paragraph: str, punctuation_start: int) -> bool:
    prefix = paragraph[: punctuation_start + 1]
    return any(
        pattern.search(prefix) is not None
        for pattern in (
            re.compile(r"(?:\b[A-Za-z]\.){2,}$"),
            re.compile(r"\b[A-Z]\.$"),
            re.compile(
                r"\b(?:et\s+al|vs|Dr|Mr|Mrs|Ms|Prof|Fig|Eq|Ref|Sec|No)\.$",
                re.I,
            ),
        )
    )


def _merge_repair_spans(
    spans: list[tuple[int, int, str, list[Decimal]]]
) -> list[tuple[int, int, str, list[Decimal]]]:
    merged: list[tuple[int, int, str, list[Decimal]]] = []
    for start, end, block_type, values in sorted(spans):
        if merged and start < merged[-1][1]:
            prior_start, prior_end, prior_type, prior_values = merged[-1]
            merged[-1] = (
                prior_start,
                max(prior_end, end),
                prior_type if prior_type == block_type else "overlapping_blocks",
                prior_values + values,
            )
        else:
            merged.append((start, end, block_type, values))
    return merged


def _numeric_equivalent(
    value: Decimal, expected: Decimal, *, is_percent: bool
) -> bool:
    if value == expected:
        return True
    return is_percent and value * Decimal(100) == expected


def _decimal_token(token: str) -> Decimal:
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise ExperimentFactClosureError("invalid manuscript numeric token") from exc
    if not value.is_finite():
        raise ExperimentFactClosureError("manuscript numeric token must be finite")
    return value


def _is_finite_decimal_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and (
            isinstance(value, int)
            or (isinstance(value, Decimal) and value.is_finite())
        )
    )


def _reject_nonfinite_constant(value: str) -> None:
    raise ExperimentFactClosureError(f"nonfinite JSON number: {value}")


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ExperimentFactClosureError("unsafe relative path")
    path = Path(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ExperimentFactClosureError("unsafe relative path")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExperimentFactClosureError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_experiment_fact_json_text(value: Mapping[str, Any]) -> str:
    try:
        return canonical_authority_json_text(value)
    except CanonicalExperimentEvidenceError as exc:
        raise ExperimentFactClosureError(
            f"invalid experiment closure authority JSON: {exc}"
        ) from exc
