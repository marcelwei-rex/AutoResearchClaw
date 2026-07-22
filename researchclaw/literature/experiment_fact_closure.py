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
from researchclaw.pipeline.canonical_fact_sheet import (
    CFSIntegrityError,
    build_canonical_fact_sheet,
    canonical_fact_sheet_sha256,
    fact_sheet_numeric_authority,
    fact_sheet_view_for_heading,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    merge_manuscript,
    parse_manuscript,
)


EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION = 3
_LEGACY_EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION = 2


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
_COUNT_CLAIM_RE = re.compile(
    r"(?<![\w.])(?P<number>\d+)\s+"
    r"(?P<prefix>(?:[A-Za-z][A-Za-z-]*\s+){0,2})"
    r"(?P<unit>runs?|seeds?|circuits?|families?|observations?|conditions?|"
    r"variants?|baselines?|comparators?|invocations?|trials?|iterations?|"
    r"epochs?|samples?)\b",
    re.I,
)
_VERSION_RE = re.compile(r"\b(?:PyTorch|torch|Python)\s+v?(?P<version>\d+(?:\.\d+)+)\b", re.I)
_RUNTIME_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s+(?:seconds?|minutes?|hours?)\b", re.I)
_COMPARATOR_ABSENCE_RE = re.compile(
    r"\b(?:comparators?|baselines?|comparison results?)\b[^.!?\n]{0,80}"
    r"\b(?:not executed|not run|left for future work|deferred|unavailable)\b",
    re.I,
)
_SOURCE_PREFIX_RE = re.compile(
    r"\b(?:evaluated(?: (?:on|with))?|sourced from|obtained from|results on|data came from)"
    r"\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*(?:\s*-\s*[A-Za-z0-9]+)?)"
    r"(?:\s+(?P<source_noun>dataset|benchmark(?:\s+suite|\s+circuits)?|suite|corpus|"
    r"data|samples|traces|measurements))?\b",
    re.I,
)
_SOURCE_USAGE_RE = re.compile(
    r"\b(?:used|using|trained (?:on|with)|drew on|leveraged)\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*(?:\s*-\s*[A-Za-z0-9]+)?)\s+"
    r"(?P<source_noun>dataset|benchmark(?:\s+suite|\s+circuits)?|suite|corpus|data|samples|"
    r"traces|measurements)\b",
    re.I,
)
_BARE_NAMED_SOURCE_USAGE_RE = re.compile(
    r"\b(?:used|using|trained (?:on|with)|drew on|leveraged)\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_-]+)\b",
    re.I,
)
_SOURCE_SAMPLES_FROM_RE = re.compile(
    r"\b(?:sourced|obtained)\s+(?P<source_noun>samples|data|traces|measurements)"
    r"\s+from\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*(?:\s*-\s*[A-Za-z0-9]+)?)\b",
    re.I,
)
_SOURCE_SUFFIX_RE = re.compile(
    r"\b(?P<name>[A-Za-z][A-Za-z0-9_-]*(?:\s*-\s*[A-Za-z0-9]+)?)"
    r"(?:\s+as\s+the)?\s+(?P<source_noun>dataset|benchmark(?:\s+suite|\s+circuits)?|suite|"
    r"corpus|traces|measurements)\b",
    re.I,
)
_GENERIC_SOURCE_NAMES = frozenset(
    {"a", "an", "the", "this", "our", "synthetic", "public", "local", "benchmark"}
)
_COMPLETE_TABLE_CLAIM_RE = re.compile(
    r"\b(?:lists?|reports?|shows?)\s+(?:all|the complete set of)\s+"
    r"(?:per[- ]?(?:run|observation)|individual)\b",
    re.I,
)
_CONDITION_NAME_RE = re.compile(
    r"\b(?P<name>[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*){0,3})"
    r"\s+condition\b",
    re.I,
)
_SCALE_RANGE_RE = re.compile(
    r"\bnodes?\s+(?P<minimum>\d[\d,]*)\s+(?:to|through|-)\s+"
    r"(?P<maximum>\d[\d,]*)\b",
    re.I,
)
_OWN_SOURCE_USAGE_RE = re.compile(
    r"\b(?:our method|our evaluation|our experiment|our study|our model|this work|we)"
    r"\b[^.!?\n]{0,40}\b(?:used|using)\s+(?:the\s+)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_-]+)",
    re.I,
)
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
        CFSIntegrityError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise ExperimentFactClosureError(f"cannot load experiment contract: {exc}") from exc

    try:
        return build_experiment_fact_closure_from_text(
            paper_text=paper_text,
            evidence=evidence,
            contract=contract,
        )
    except CFSIntegrityError as exc:
        raise ExperimentFactClosureError(
            f"canonical fact sheet is invalid: {exc}"
        ) from exc


def build_experiment_fact_closure_from_text(
    *,
    paper_text: str,
    evidence: CanonicalExperimentEvidence,
    contract: Any,
) -> dict[str, Any]:
    """Build closure from already-bound paper bytes and canonical evidence."""

    cfs = build_canonical_fact_sheet(evidence)
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

    if cfs is None:
        manuscript_literals = _extract_experiment_metric_literals(paper_text)
        unknown_values = _unknown_metric_values(manuscript_literals, grounded)
    else:
        manuscript_literals, unknown_values = _extract_view_scoped_metric_literals(
            paper_text, cfs
        )
    citation_bound_numeric_claims = (
        _citation_bound_numeric_claims(paper_text) if cfs is not None else []
    )
    manuscript_values = [value for value, _is_percent in manuscript_literals]
    dataset_violations = list(
        find_dataset_claim_violations(paper_text, contract.dataset_origin)
    )
    structured_violations = (
        _structured_fact_violations(paper_text, cfs) if cfs is not None else []
    )
    payload = {
        "schema_version": (
            EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION
            if cfs is not None
            else _LEGACY_EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION
        ),
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
        "valid": not unknown_values and not dataset_violations and not structured_violations,
    }
    if cfs is not None:
        payload.update(
            canonical_fact_sheet_sha256=canonical_fact_sheet_sha256(cfs),
            fact_sheet_schema_version=cfs["schema_version"],
            structured_fact_violations=structured_violations,
            citation_bound_numeric_claims=citation_bound_numeric_claims,
        )
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
        CFSIntegrityError,
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
    legacy_expected = {
        "schema_version", "paper_path", "paper_sha256", "experiment_contract_path",
        "experiment_contract_sha256", "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256", "dataset_origin", "metric_sources",
        "grounded_numeric_values", "manuscript_numeric_values",
        "unknown_numeric_values", "dataset_claim_violations", "valid",
    }
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise ExperimentFactClosureError("unsupported experiment closure schema")
    expected = set(legacy_expected)
    if schema_version == EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION:
        expected.update(
            {
                "canonical_fact_sheet_sha256",
                "fact_sheet_schema_version",
                "structured_fact_violations",
                "citation_bound_numeric_claims",
            }
        )
    elif schema_version != _LEGACY_EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION:
        raise ExperimentFactClosureError("unsupported experiment closure schema")
    if set(payload) != expected:
        raise ExperimentFactClosureError("experiment closure fields mismatch")
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
    structured: list[dict[str, Any]] = []
    if schema_version == EXPERIMENT_FACT_CLOSURE_SCHEMA_VERSION:
        if (
            type(payload["fact_sheet_schema_version"]) is not int
            or payload["fact_sheet_schema_version"] != 1
            or not isinstance(payload["canonical_fact_sheet_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["canonical_fact_sheet_sha256"])
            is None
        ):
            raise ExperimentFactClosureError("invalid canonical fact sheet binding")
        raw_structured = payload["structured_fact_violations"]
        if not isinstance(raw_structured, list):
            raise ExperimentFactClosureError("invalid structured fact violations")
        for item in raw_structured:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "kind", "section", "section_body_sha256", "char_start",
                    "char_end", "claim_sha256", "claim", "section_id",
                }
            ):
                raise ExperimentFactClosureError("invalid structured fact violation")
            if any(
                not isinstance(item[key], str) or not item[key]
                for key in ("kind", "section", "section_id", "claim")
            ) or any(
                not isinstance(item[key], str)
                or re.fullmatch(r"[0-9a-f]{64}", item[key]) is None
                for key in ("section_body_sha256", "claim_sha256")
            ):
                raise ExperimentFactClosureError("invalid structured fact violation")
            if (
                type(item["char_start"]) is not int
                or type(item["char_end"]) is not int
                or item["char_start"] < 0
                or item["char_end"] <= item["char_start"]
                or _sha256(item["claim"]) != item["claim_sha256"]
            ):
                raise ExperimentFactClosureError("invalid structured fact occurrence")
            structured.append(item)
        if structured != sorted(
            structured,
            key=lambda item: (
                item["section_id"], item["char_start"], item["char_end"], item["kind"]
            ),
        ):
            raise ExperimentFactClosureError("structured fact violations are not canonical")
        citation_bound = payload["citation_bound_numeric_claims"]
        if not isinstance(citation_bound, list):
            raise ExperimentFactClosureError("invalid citation-bound numeric claims")
        for item in citation_bound:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "section", "section_id", "section_body_sha256", "char_start", "char_end",
                    "claim_sha256", "numeric_values",
                }
                or not isinstance(item["section"], str)
                or not item["section"]
                or not isinstance(item["section_id"], str)
                or not item["section_id"]
                or type(item["char_start"]) is not int
                or type(item["char_end"]) is not int
                or item["char_start"] < 0
                or item["char_end"] <= item["char_start"]
                or any(
                    not isinstance(item[key], str)
                    or re.fullmatch(r"[0-9a-f]{64}", item[key]) is None
                    for key in ("section_body_sha256", "claim_sha256")
                )
                or not isinstance(item["numeric_values"], list)
                or not item["numeric_values"]
                or any(
                    not _is_finite_decimal_number(value)
                    for value in item["numeric_values"]
                )
            ):
                raise ExperimentFactClosureError("invalid citation-bound numeric claim")
        if citation_bound != sorted(
            citation_bound,
            key=lambda item: (item["section_id"], item["char_start"], item["char_end"]),
        ):
            raise ExperimentFactClosureError(
                "citation-bound numeric claims are not canonical"
            )
    expected_valid = (
        not payload["unknown_numeric_values"] and not violations and not structured
    )
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
    from researchclaw.literature.citation_plan import strip_strict_citation_markers

    prose = strip_strict_citation_markers(text)
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


def _extract_experiment_metric_literals(
    text: str, *, include_all_sections: bool = False
) -> list[tuple[Decimal, bool]]:
    try:
        document = parse_manuscript(text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for experiment closure: {exc}"
        ) from exc
    section_texts = []
    for section in document.sections:
        if not include_all_sections and not _is_experiment_fact_section(section.title):
            continue
        body = section.body
        if include_all_sections:
            citation_spans = _citation_bound_sentence_spans(section.path[0], body)
            body = _mask_spans(
                body,
                [match.span() for match in _COUNT_CLAIM_RE.finditer(body)]
                + [match.span() for match in _VERSION_RE.finditer(body)]
                + citation_spans,
            )
        section_texts.append(body)
    return _extract_metric_literals("\n".join(section_texts))


def _unknown_metric_values(
    literals: list[tuple[Decimal, bool]], grounded: list[Decimal]
) -> list[Decimal]:
    return [
        value
        for value, is_percent in literals
        if not any(
            _numeric_equivalent(value, expected, is_percent=is_percent)
            for expected in grounded
        )
    ]


def _extract_view_scoped_metric_literals(
    paper_text: str, cfs: Mapping[str, Any]
) -> tuple[list[tuple[Decimal, bool]], list[Decimal]]:
    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for experiment closure: {exc}"
        ) from exc
    literals: list[tuple[Decimal, bool]] = []
    unknown: list[Decimal] = []
    for section in document.sections:
        body = _mask_spans(
            section.body,
            [match.span() for match in _COUNT_CLAIM_RE.finditer(section.body)]
            + [match.span() for match in _VERSION_RE.finditer(section.body)],
        )
        section_literals = _extract_metric_literals(body)
        allowed = [
            Decimal(value)
            for value in fact_sheet_numeric_authority(
                cfs, view=fact_sheet_view_for_heading(section.path[0])
            )
        ]
        literals.extend(section_literals)
        unknown.extend(_unknown_metric_values(section_literals, allowed))
    return literals, unknown


def _citation_bound_sentence_spans(
    top_level_heading: str, body: str
) -> list[tuple[int, int]]:
    if top_level_heading.casefold() not in {"introduction", "related work"}:
        return []
    from researchclaw.literature.citation_plan import parse_strict_citation_occurrences

    spans = {
        (occurrence.sentence_start, occurrence.sentence_end)
        for occurrence in parse_strict_citation_occurrences(body)
    }
    return sorted(spans)


def _citation_bound_numeric_claims(paper_text: str) -> list[dict[str, Any]]:
    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for citation-bound numeric closure: {exc}"
        ) from exc
    claims: list[dict[str, Any]] = []
    for section in document.sections:
        for start, end in _citation_bound_sentence_spans(section.path[0], section.body):
            sentence = section.body[start:end]
            values = [value for value, _percent in _extract_metric_literals(sentence)]
            if not values:
                continue
            claims.append(
                {
                    "section": section.path[0],
                    "section_id": section.section_id,
                    "section_body_sha256": _sha256(section.body),
                    "char_start": start,
                    "char_end": end,
                    "claim_sha256": _sha256(sentence),
                    "numeric_values": values,
                }
            )
    return sorted(
        claims,
        key=lambda item: (item["section_id"], item["char_start"], item["char_end"]),
    )


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _structured_fact_violations(
    paper_text: str, cfs: Mapping[str, Any]
) -> list[dict[str, Any]]:
    try:
        document = parse_manuscript(paper_text, strict=True)
    except ManuscriptStructureError as exc:
        raise ExperimentFactClosureError(
            f"paper structure is invalid for structured fact closure: {exc}"
        ) from exc

    violations: list[dict[str, Any]] = []
    for section in document.sections:
        for match in _COUNT_CLAIM_RE.finditer(section.body):
            kind = _count_violation_kind(match, section.body, cfs)
            if kind is not None:
                violations.append(
                    _violation(
                        kind, section.title, section.section_id, section.body,
                        match.start(), match.end(),
                    )
                )
        violations.extend(
            _runtime_violations(section.title, section.section_id, section.body, cfs)
        )
        violations.extend(
            _provenance_violations(section.title, section.section_id, section.body, cfs)
        )
        violations.extend(
            _condition_violations(section.title, section.section_id, section.body, cfs)
        )
        for match in _SCALE_RANGE_RE.finditer(section.body):
            observed = (
                int(match.group("minimum").replace(",", "")),
                int(match.group("maximum").replace(",", "")),
            )
            expected = (cfs["scale"]["n_total_min"], cfs["scale"]["n_total_max"])
            if observed != expected:
                violations.append(
                    _violation(
                        "scale_violation", section.title, section.section_id, section.body,
                        match.start(), match.end(),
                    )
                )
        for match in _COMPARATOR_ABSENCE_RE.finditer(section.body):
            if any(item["role"] == "comparator" for item in cfs["conditions"]):
                violations.append(
                    _violation(
                        "comparator_presence_violation",
                        section.title,
                        section.section_id,
                        section.body,
                        match.start(),
                        match.end(),
                    )
                )
        for match in _COMPLETE_TABLE_CLAIM_RE.finditer(section.body):
            violations.append(
                _violation(
                    "table_completeness_violation",
                    section.title,
                    section.section_id,
                    section.body,
                    match.start(),
                    match.end(),
                )
            )
    return sorted(
        {tuple(item.items()): item for item in violations}.values(),
        key=lambda item: (
            item["section_id"], item["char_start"], item["char_end"], item["kind"]
        ),
    )


def _violation(
    kind: str, section: str, section_id: str, body: str, start: int, end: int
) -> dict[str, Any]:
    claim = body[start:end]
    if not claim or start < 0 or end <= start or end > len(body):
        raise ExperimentFactClosureError("invalid structured fact occurrence")
    return {
        "kind": kind,
        "section": section,
        "section_id": section_id,
        "section_body_sha256": _sha256(body),
        "char_start": start,
        "char_end": end,
        "claim_sha256": _sha256(claim),
        "claim": claim,
    }


def _count_violation_kind(
    match: re.Match[str], body: str, cfs: Mapping[str, Any]
) -> str | None:
    number = int(match.group("number"))
    raw_unit = match.group("unit").casefold()
    unit = "family" if raw_unit in {"family", "families"} else raw_unit.rstrip("s")
    qualifiers = tuple(match.group("prefix").casefold().split())
    suffix = body[match.end():]
    clause_suffix = re.split(r"[.;!?\n]", suffix, maxsplit=1)[0].strip().casefold()
    predicate = re.match(
        r"^,?\s*(?:all|each|both|the)?\s*"
        r"(?:passed|succeeded|failed|failures|successes|flawless|outperformed|"
        r"outperforming|achieved)\b",
        clause_suffix,
    )
    if predicate is not None:
        return "unsupported_derived_count"
    if clause_suffix.startswith(","):
        metric_names = "|".join(re.escape(str(key)) for key in cfs["metric_keys"])
        if clause_suffix == ", the evaluation was repeated":
            immediate = ""
        elif re.match(
            rf"^,\s*(?:the\s+)?(?:{metric_names})\s+"
            rf"(?:reached|was|averaged|achieved|measured)\b",
            clause_suffix,
            re.I,
        ) is None:
            return "unsupported_derived_count"
        else:
            immediate = ""
    else:
        immediate = clause_suffix
    neutral_suffixes = {
        "", "in total", "total", "overall", "across the complete evaluation",
        "per family", "per seed", "were evaluated",
        "were used", "were included", "were analyzed", "were analysed",
    }
    allowed_qualifiers = {
        "seed": {(), ("total",), ("all",)},
        "condition": {(), ("total",), ("all",)},
        "family": {(), ("total",), ("all",)},
        "invocation": {(), ("total",), ("all",)},
        "comparator": {(), ("total",), ("all",)},
        "baseline": {(), ("total",), ("all",)},
        "variant": {(), ("circuit",), ("total",), ("all",)},
        "observation": {
            (), ("total",), ("all",), ("overall",), ("complete",),
        },
        "run": {()},
    }
    if unit == "circuit" and immediate in neutral_suffixes:
        return "ambiguous_count_claim"
    if unit == "run" and qualifiers in {(), ("experimental",)} and immediate in neutral_suffixes:
        return "ambiguous_count_claim"
    if unit == "observation" and qualifiers == ("individual",) and immediate in neutral_suffixes:
        return "ambiguous_count_claim"
    if immediate not in neutral_suffixes or qualifiers not in allowed_qualifiers.get(unit, set()):
        return "unsupported_derived_count"
    if unit == "circuit":
        return "ambiguous_count_claim"
    if unit in {"run", "observation"}:
        global_qualified = bool(
            {"total", "all", "overall", "complete"}.intersection(qualifiers)
            or immediate in {"in total", "total", "overall", "across the complete evaluation"}
        )
        if unit == "observation" and (global_qualified or qualifiers == ()):
            return None if number == cfs["counts"]["observations"] else "count_mismatch"
        return "ambiguous_count_claim"
    if unit == "variant":
        if immediate == "per family":
            expected = cfs["variants_per_family"]
        elif immediate == "per seed":
            expected = cfs["counts"]["observations_per_condition_seed"]
        elif "circuit" in qualifiers:
            expected = len(cfs["variant_ids"])
        else:
            expected = len(cfs["variant_ids"])
            if number != expected:
                return "ambiguous_count_claim"
        return None if number == expected else "count_mismatch"
    expected_by_unit = {
        "seed": len(cfs["seeds"]),
        "condition": len(cfs["conditions"]),
        "family": len(cfs["circuit_families"]),
        "invocation": cfs["counts"]["invocations"],
        "comparator": sum(
            item["role"] == "comparator" for item in cfs["conditions"]
        ),
    }
    expected = expected_by_unit.get(unit)
    if expected is None:
        return "ambiguous_count_claim"
    return None if number == expected else "count_mismatch"


def _runtime_violations(
    section: str, section_id: str, body: str, cfs: Mapping[str, Any]
) -> list[dict[str, Any]]:
    runtime = cfs["runtime"]
    packages = runtime["packages"]
    violations: list[dict[str, Any]] = []
    for match in _VERSION_RE.finditer(body):
        product = match.group(0).split()[0].casefold()
        actual = match.group("version")
        expected = runtime["python"] if product == "python" else packages.get("torch")
        if expected != actual:
            violations.append(
                _violation(
                    "runtime_violation", section, section_id, body,
                    match.start(), match.end(),
                )
            )
    for match in re.finditer(r"\b(?:CPU|GPU|MPS|CUDA)\b", body, re.I):
        sentence_start, sentence_end, _kind = _safe_fact_block_span(
            body, match.start()
        )
        sentence = body[sentence_start:sentence_end]
        if re.search(r"\bno\b[^.;]*\b(?:was|were)\s+used\b", sentence, re.I):
            continue
        if match.group(0).casefold() != runtime["device"].casefold():
            violations.append(
                _violation(
                    "runtime_violation", section, section_id, body,
                    match.start(), match.end(),
                )
            )
    for match in _RUNTIME_DURATION_RE.finditer(body):
        violations.append(
            _violation(
                "runtime_violation", section, section_id, body,
                match.start(), match.end(),
            )
        )
    return violations


def _provenance_violations(
    section: str, section_id: str, body: str, cfs: Mapping[str, Any]
) -> list[dict[str, Any]]:
    labels = cfs["bound_labels"]
    allowed = set(labels["benchmark_tokens"])
    allowed.update(
        _normalize_source_name(labels[key])
        for key in ("dataset", "evaluator_schema", "evaluator_id")
    )
    metric_aliases = {
        _normalize_source_name(str(key)) for key in cfs["metric_keys"]
    }
    if any("f1" in alias for alias in metric_aliases):
        metric_aliases.add("f1")
    violations: list[dict[str, Any]] = []
    matches = (
        list(_SOURCE_PREFIX_RE.finditer(body))
        + list(_SOURCE_USAGE_RE.finditer(body))
        + list(_BARE_NAMED_SOURCE_USAGE_RE.finditer(body))
        + list(_SOURCE_SAMPLES_FROM_RE.finditer(body))
        + list(_SOURCE_SUFFIX_RE.finditer(body))
        + list(_OWN_SOURCE_USAGE_RE.finditer(body))
    )
    for match in matches:
        name = match.group("name")
        if _normalize_source_name(name) in _GENERIC_SOURCE_NAMES:
            continue
        source_noun = match.groupdict().get("source_noun")
        named_token = bool(re.search(r"[-_0-9]", name))
        if source_noun is None and _normalize_source_name(name) in metric_aliases:
            continue
        if source_noun is None and not named_token:
            continue
        if _normalize_source_name(name) not in allowed:
            violations.append(
                _violation(
                    "provenance_violation", section, section_id, body,
                    match.start(), match.end(),
                )
            )
    return violations


def _condition_violations(
    section: str, section_id: str, body: str, cfs: Mapping[str, Any]
) -> list[dict[str, Any]]:
    allowed = {
        _normalize_source_name(item["id"])
        for item in cfs["conditions"]
    }
    generic = {"primary", "comparator", "baseline", "experimental", "control"}
    violations: list[dict[str, Any]] = []
    for match in _CONDITION_NAME_RE.finditer(body):
        words = match.group("name").split()
        while words and words[0].casefold() in {"the", "a", "an"}:
            words.pop(0)
        name = " ".join(words)
        normalized = _normalize_source_name(name)
        if normalized in generic:
            continue
        if normalized not in allowed:
            violations.append(
                _violation(
                    "condition_violation", section, section_id, body,
                    match.start(), match.end(),
                )
            )
    return violations


def _normalize_source_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def remove_unsupported_experiment_fact_blocks(
    paper_text: str,
    *,
    grounded_numeric_values: list[Decimal],
    dataset_origin: str,
    structured_fact_violations: list[dict[str, Any]] | None = None,
    canonical_fact_sheet: Mapping[str, Any] | None = None,
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
    structured_fact_violations = structured_fact_violations or []
    if structured_fact_violations:
        sections_by_id = {section.section_id: section for section in document.sections}
        for violation in structured_fact_violations:
            section_id = violation.get("section_id")
            section = sections_by_id.get(section_id) if isinstance(section_id, str) else None
            claim = violation.get("claim")
            start = violation.get("char_start")
            end = violation.get("char_end")
            if (
                section is None
                or not isinstance(claim, str)
                or not claim
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(section.body)
                or violation.get("section_body_sha256") != _sha256(section.body)
                or section.body[start:end] != claim
                or violation.get("claim_sha256") != _sha256(claim)
            ):
                raise ExperimentFactClosureError("invalid structured repair claim")
        if canonical_fact_sheet is None:
            raise ExperimentFactClosureError(
                "canonical fact sheet is required for structured repair"
            )
        fresh = _structured_fact_violations(paper_text, canonical_fact_sheet)
        if structured_fact_violations != fresh:
            raise ExperimentFactClosureError(
                "structured repair violations do not match fresh replay"
            )
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
        for violation in structured_fact_violations:
            if violation.get("section_id") != section.section_id:
                continue
            claim = violation.get("claim")
            start = violation.get("char_start")
            end = violation.get("char_end")
            if (
                not isinstance(claim, str)
                or not claim
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(body)
                or violation.get("section_body_sha256") != _sha256(body)
                or body[start:end] != claim
                or violation.get("claim_sha256") != _sha256(claim)
            ):
                raise ExperimentFactClosureError("invalid structured repair claim")
            targets.append((start, violation.get("kind", "structured_fact"), None))
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
