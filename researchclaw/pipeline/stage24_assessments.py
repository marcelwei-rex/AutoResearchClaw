"""Strict source-record contracts for future canonical Stage 24 assessments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

CITATION_ASSESSMENT_SCHEMA_VERSION = 1
CITATION_ASSESSMENT_POLICY_VERSION = "citation_assessment_v1"
GENERIC_SUPPORT_POLICY_VERSION = "generic_support_v1"
RESOLUTION_ASSESSMENT_POLICY_VERSION = "resolution_assessment_v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OBLIGATION_ID_RE = re.compile(r"obl-[0-9a-f]{64}")
_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_CITE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d{4}[A-Za-z0-9_-]*")


class Stage24AssessmentError(ValueError):
    """Raised when an assessment input or source record is not canonical."""


@dataclass(frozen=True)
class CitationEvidenceRecord:
    card_id: str
    card_sha256: str
    excerpt_id: str
    excerpt_sha256: str
    byte_start: int
    byte_end: int


@dataclass(frozen=True)
class CitationAssessmentInput:
    schema_version: int
    policy_version: str
    canonical_manifest_sha256: str
    paper_sha256: str
    obligation_id: str
    byte_start: int
    byte_end: int
    source_sha256: str
    instance_id: str
    cite_key: str
    stage23_verification_record_sha256: str
    evidence_records: tuple[CitationEvidenceRecord, ...]
    critic_model: str
    assessment_input_sha256: str
    assessment_id: str


@dataclass(frozen=True)
class CitationAssessmentRecord:
    schema_version: int
    assessment_id: str
    assessment_input_sha256: str
    critic_model: str
    policy_version: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class GenericEvidenceRecord:
    evidence_kind: str
    authority_path: str
    authority_sha256: str
    semantic_pointer: str
    semantic_value_sha256: str


@dataclass(frozen=True)
class GenericSupportInput:
    schema_version: int
    policy_version: str
    canonical_manifest_sha256: str
    paper_sha256: str
    obligation_id: str
    byte_start: int
    byte_end: int
    source_sha256: str
    evidence_records: tuple[GenericEvidenceRecord, ...]
    critic_model: str
    assessment_input_sha256: str
    assessment_id: str


@dataclass(frozen=True)
class GenericSupportRecord:
    schema_version: int
    assessment_id: str
    assessment_input_sha256: str
    critic_model: str
    policy_version: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class ResolutionAssessmentInput:
    schema_version: int
    policy_version: str
    critique_sha256: str
    finding_content_sha256: str
    raw_paper_sha256: str
    critic_model: str
    assessment_input_sha256: str
    assessment_id: str


@dataclass(frozen=True)
class ResolutionAssessmentRecord:
    schema_version: int
    assessment_id: str
    assessment_input_sha256: str
    critic_model: str
    policy_version: str
    resolution: str
    note: str


def build_citation_assessment_input(
    *,
    canonical_manifest_sha256: str,
    paper_sha256: str,
    obligation_id: str,
    byte_start: int,
    byte_end: int,
    source_sha256: str,
    instance_id: str,
    cite_key: str,
    stage23_verification_record_sha256: str,
    evidence_records: Sequence[Mapping[str, Any] | CitationEvidenceRecord],
    critic_model: str,
) -> CitationAssessmentInput:
    """Build one identity-bound input after C4-A1 supplies byte obligations."""

    evidence = tuple(_parse_evidence_record(item) for item in evidence_records)
    _require_sha256(canonical_manifest_sha256, "canonical manifest hash")
    _require_sha256(paper_sha256, "paper hash")
    _require_sha256(source_sha256, "source hash")
    _require_sha256(
        stage23_verification_record_sha256,
        "Stage 23 verification record hash",
    )
    _require_obligation_id(obligation_id)
    _require_pattern(instance_id, "instance ID", _INSTANCE_ID_RE)
    _require_pattern(cite_key, "cite key", _CITE_KEY_RE)
    _require_string(critic_model, "critic model")
    _require_span(byte_start, byte_end, "citation assessment")
    if not evidence:
        raise Stage24AssessmentError("citation assessment evidence is empty")
    if len({item.excerpt_id for item in evidence}) != len(evidence):
        raise Stage24AssessmentError("citation assessment evidence is duplicated")
    payload = {
        "schema_version": CITATION_ASSESSMENT_SCHEMA_VERSION,
        "policy_version": CITATION_ASSESSMENT_POLICY_VERSION,
        "canonical_manifest_sha256": canonical_manifest_sha256,
        "paper_sha256": paper_sha256,
        "obligation_id": obligation_id,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "source_sha256": source_sha256,
        "instance_id": instance_id,
        "cite_key": cite_key,
        "stage23_verification_record_sha256": stage23_verification_record_sha256,
        "evidence_records": [
            {
                "card_id": item.card_id,
                "card_sha256": item.card_sha256,
                "excerpt_id": item.excerpt_id,
                "excerpt_sha256": item.excerpt_sha256,
                "byte_start": item.byte_start,
                "byte_end": item.byte_end,
            }
            for item in evidence
        ],
        "critic_model": critic_model,
    }
    input_sha256 = _identity_sha256(payload)
    # The full input hash is the v1 assessment ID; aliases and truncation are
    # therefore impossible and the filename can be derived without prose.
    assessment_id = input_sha256
    return CitationAssessmentInput(
        schema_version=CITATION_ASSESSMENT_SCHEMA_VERSION,
        policy_version=CITATION_ASSESSMENT_POLICY_VERSION,
        canonical_manifest_sha256=canonical_manifest_sha256,
        paper_sha256=paper_sha256,
        obligation_id=obligation_id,
        byte_start=byte_start,
        byte_end=byte_end,
        source_sha256=source_sha256,
        instance_id=instance_id,
        cite_key=cite_key,
        stage23_verification_record_sha256=stage23_verification_record_sha256,
        evidence_records=evidence,
        critic_model=critic_model,
        assessment_input_sha256=input_sha256,
        assessment_id=assessment_id,
    )


def parse_citation_assessment_record(
    text: str,
    *,
    expected_input: CitationAssessmentInput,
    writer_model: str,
) -> CitationAssessmentRecord:
    """Strictly replay a raw LLM record against an independently built input."""

    value = _parse_object(text)
    expected_fields = {
        "schema_version",
        "assessment_id",
        "assessment_input_sha256",
        "critic_model",
        "policy_version",
        "verdict",
        "reason",
    }
    if set(value) != expected_fields:
        raise Stage24AssessmentError("citation assessment record fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage24AssessmentError("citation assessment schema is invalid")
    for field in (
        "assessment_id",
        "assessment_input_sha256",
        "critic_model",
        "policy_version",
        "verdict",
        "reason",
    ):
        _require_string(value[field], field)
    if value["assessment_id"] != expected_input.assessment_id:
        raise Stage24AssessmentError("citation assessment ID mismatch")
    if value["assessment_input_sha256"] != expected_input.assessment_input_sha256:
        raise Stage24AssessmentError("citation assessment input hash mismatch")
    if value["policy_version"] != expected_input.policy_version:
        raise Stage24AssessmentError("citation assessment policy mismatch")
    if value["critic_model"] != expected_input.critic_model:
        raise Stage24AssessmentError("citation assessment critic mismatch")
    if (
        not isinstance(writer_model, str)
        or not writer_model.strip()
        or value["critic_model"] == writer_model.strip()
    ):
        raise Stage24AssessmentError("citation assessment critic is not isolated")
    if value["verdict"] not in {"supported", "unsupported"}:
        raise Stage24AssessmentError("citation assessment verdict is invalid")
    return CitationAssessmentRecord(
        schema_version=1,
        assessment_id=str(value["assessment_id"]),
        assessment_input_sha256=str(value["assessment_input_sha256"]),
        critic_model=str(value["critic_model"]),
        policy_version=str(value["policy_version"]),
        verdict=str(value["verdict"]),
        reason=str(value["reason"]).strip(),
    )


def build_generic_support_input(
    *,
    canonical_manifest_sha256: str,
    paper_sha256: str,
    obligation_id: str,
    byte_start: int,
    byte_end: int,
    source_sha256: str,
    evidence_records: Sequence[Mapping[str, Any] | GenericEvidenceRecord],
    critic_model: str,
) -> GenericSupportInput:
    evidence = tuple(_parse_generic_evidence_record(item) for item in evidence_records)
    _require_sha256(canonical_manifest_sha256, "canonical manifest hash")
    _require_sha256(paper_sha256, "paper hash")
    _require_obligation_id(obligation_id)
    _require_span(byte_start, byte_end, "generic support")
    _require_sha256(source_sha256, "source hash")
    _require_string(critic_model, "critic model")
    if not evidence:
        raise Stage24AssessmentError("generic support evidence is empty")
    ordered = tuple(
        sorted(
            evidence,
            key=lambda item: (
                item.evidence_kind,
                item.authority_path,
                item.authority_sha256,
                item.semantic_pointer,
                item.semantic_value_sha256,
            ),
        )
    )
    if ordered != evidence or len(set(ordered)) != len(ordered):
        raise Stage24AssessmentError("generic support evidence closure is not canonical")
    payload = {
        "schema_version": 1,
        "policy_version": GENERIC_SUPPORT_POLICY_VERSION,
        "canonical_manifest_sha256": canonical_manifest_sha256,
        "paper_sha256": paper_sha256,
        "obligation_id": obligation_id,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "source_sha256": source_sha256,
        "evidence_records": [
            {
                "evidence_kind": item.evidence_kind,
                "authority_path": item.authority_path,
                "authority_sha256": item.authority_sha256,
                "semantic_pointer": item.semantic_pointer,
                "semantic_value_sha256": item.semantic_value_sha256,
            }
            for item in evidence
        ],
        "critic_model": critic_model,
    }
    input_sha256 = _identity_sha256(payload)
    return GenericSupportInput(
        schema_version=1,
        policy_version=GENERIC_SUPPORT_POLICY_VERSION,
        canonical_manifest_sha256=canonical_manifest_sha256,
        paper_sha256=paper_sha256,
        obligation_id=obligation_id,
        byte_start=byte_start,
        byte_end=byte_end,
        source_sha256=source_sha256,
        evidence_records=evidence,
        critic_model=critic_model,
        assessment_input_sha256=input_sha256,
        assessment_id=input_sha256,
    )


def parse_generic_support_record(
    text: str,
    *,
    expected_input: GenericSupportInput,
    writer_model: str,
) -> GenericSupportRecord:
    value = _parse_source_record(text, label="generic support")
    _validate_common_source_record(
        value,
        expected_input=expected_input,
        writer_model=writer_model,
        verdict_field="verdict",
        allowed_values={"supported", "unsupported"},
        explanation_field="reason",
    )
    return GenericSupportRecord(
        schema_version=1,
        assessment_id=value["assessment_id"],
        assessment_input_sha256=value["assessment_input_sha256"],
        critic_model=value["critic_model"],
        policy_version=value["policy_version"],
        verdict=value["verdict"],
        reason=value["reason"],
    )


def build_resolution_assessment_input(
    *,
    critique_sha256: str,
    finding: Mapping[str, Any],
    raw_paper_sha256: str,
    critic_model: str,
) -> ResolutionAssessmentInput:
    expected_fields = {
        "id", "severity", "category", "question", "finding",
        "falsification_criterion",
    }
    if set(finding) != expected_fields:
        raise Stage24AssessmentError("critique finding fields mismatch")
    for field in expected_fields:
        _require_string(finding[field], f"finding {field}")
    finding_payload = {
        "finding_id": finding["id"],
        "severity": finding["severity"],
        "category": finding["category"],
        "question": finding["question"],
        "finding": finding["finding"],
        "falsification_criterion": finding["falsification_criterion"],
    }
    finding_hash = _identity_sha256(finding_payload)
    _require_sha256(critique_sha256, "critique hash")
    _require_sha256(raw_paper_sha256, "paper hash")
    _require_string(critic_model, "critic model")
    payload = {
        "schema_version": 1,
        "policy_version": RESOLUTION_ASSESSMENT_POLICY_VERSION,
        "critique_sha256": critique_sha256,
        "finding_content_sha256": finding_hash,
        "raw_paper_sha256": raw_paper_sha256,
        "critic_model": critic_model,
    }
    input_sha256 = _identity_sha256(payload)
    return ResolutionAssessmentInput(
        schema_version=1,
        policy_version=RESOLUTION_ASSESSMENT_POLICY_VERSION,
        critique_sha256=critique_sha256,
        finding_content_sha256=finding_hash,
        raw_paper_sha256=raw_paper_sha256,
        critic_model=critic_model,
        assessment_input_sha256=input_sha256,
        assessment_id=input_sha256,
    )


def parse_resolution_assessment_record(
    text: str,
    *,
    expected_input: ResolutionAssessmentInput,
    writer_model: str,
) -> ResolutionAssessmentRecord:
    value = _parse_source_record(
        text,
        label="resolution assessment",
        verdict_field="resolution",
        explanation_field="note",
    )
    _validate_common_source_record(
        value,
        expected_input=expected_input,
        writer_model=writer_model,
        verdict_field="resolution",
        allowed_values={"fixed", "rebutted", "unresolved"},
        explanation_field="note",
    )
    return ResolutionAssessmentRecord(
        schema_version=1,
        assessment_id=value["assessment_id"],
        assessment_input_sha256=value["assessment_input_sha256"],
        critic_model=value["critic_model"],
        policy_version=value["policy_version"],
        resolution=value["resolution"],
        note=value["note"],
    )


def _parse_generic_evidence_record(
    value: Mapping[str, Any] | GenericEvidenceRecord,
) -> GenericEvidenceRecord:
    fields = {
        "evidence_kind", "authority_path", "authority_sha256",
        "semantic_pointer", "semantic_value_sha256",
    }
    if isinstance(value, GenericEvidenceRecord):
        result = value
    elif isinstance(value, Mapping) and set(value) == fields:
        result = GenericEvidenceRecord(
            evidence_kind=_require_string(value["evidence_kind"], "evidence kind"),
            authority_path=_require_string(value["authority_path"], "authority path"),
            authority_sha256=_require_sha256(value["authority_sha256"], "authority hash"),
            semantic_pointer=_require_string(value["semantic_pointer"], "semantic pointer"),
            semantic_value_sha256=_require_sha256(
                value["semantic_value_sha256"], "semantic value hash"
            ),
        )
    else:
        raise Stage24AssessmentError("generic evidence fields mismatch")
    if result.evidence_kind not in {
        "metric_observation", "numeric_support", "citation_support",
        "comparison_support",
    }:
        raise Stage24AssessmentError("generic evidence kind is invalid")
    _require_string(result.authority_path, "authority path")
    if result.authority_path.startswith("/") or ".." in result.authority_path.split("/"):
        raise Stage24AssessmentError("generic evidence authority path is unsafe")
    _require_sha256(result.authority_sha256, "authority hash")
    _require_string(result.semantic_pointer, "semantic pointer")
    _require_sha256(result.semantic_value_sha256, "semantic value hash")
    return result


def _parse_source_record(
    text: str,
    *,
    label: str,
    verdict_field: str = "verdict",
    explanation_field: str = "reason",
) -> dict[str, str | int]:
    value = _parse_object(text)
    expected = {
        "schema_version", "assessment_id", "assessment_input_sha256",
        "critic_model", "policy_version", verdict_field, explanation_field,
    }
    if set(value) != expected:
        raise Stage24AssessmentError(f"{label} record fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage24AssessmentError(f"{label} schema is invalid")
    for field in expected - {"schema_version"}:
        _require_string(value[field], f"{label} {field}")
    return value


def _validate_common_source_record(
    value: Mapping[str, Any],
    *,
    expected_input: GenericSupportInput | ResolutionAssessmentInput,
    writer_model: str,
    verdict_field: str,
    allowed_values: set[str],
    explanation_field: str,
) -> None:
    if value["assessment_id"] != expected_input.assessment_id:
        raise Stage24AssessmentError("assessment ID mismatch")
    if value["assessment_input_sha256"] != expected_input.assessment_input_sha256:
        raise Stage24AssessmentError("assessment input hash mismatch")
    if value["policy_version"] != expected_input.policy_version:
        raise Stage24AssessmentError("assessment policy mismatch")
    if value["critic_model"] != expected_input.critic_model:
        raise Stage24AssessmentError("assessment critic mismatch")
    if not isinstance(writer_model, str) or not writer_model.strip() or value["critic_model"] == writer_model.strip():
        raise Stage24AssessmentError("assessment critic is not isolated")
    if value[verdict_field] not in allowed_values:
        raise Stage24AssessmentError(f"assessment {verdict_field} is invalid")
    _require_string(value[explanation_field], explanation_field)


def _parse_evidence_record(
    value: Mapping[str, Any] | CitationEvidenceRecord,
) -> CitationEvidenceRecord:
    if isinstance(value, CitationEvidenceRecord):
        result = value
    else:
        fields = {
            "card_id",
            "card_sha256",
            "excerpt_id",
            "excerpt_sha256",
            "byte_start",
            "byte_end",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise Stage24AssessmentError("citation evidence fields mismatch")
        result = CitationEvidenceRecord(
            card_id=_require_string(value["card_id"], "card ID"),
            card_sha256=_require_sha256(value["card_sha256"], "card hash"),
            excerpt_id=_require_string(value["excerpt_id"], "excerpt ID"),
            excerpt_sha256=_require_sha256(
                value["excerpt_sha256"], "excerpt hash"
            ),
            byte_start=value["byte_start"],
            byte_end=value["byte_end"],
        )
    _require_string(result.card_id, "card ID")
    _require_sha256(result.card_sha256, "card hash")
    _require_string(result.excerpt_id, "excerpt ID")
    _require_sha256(result.excerpt_sha256, "excerpt hash")
    _require_span(result.byte_start, result.byte_end, "citation evidence")
    return result


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise Stage24AssessmentError(f"invalid citation assessment JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage24AssessmentError("citation assessment root is not an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Stage24AssessmentError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_string(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise Stage24AssessmentError(f"{label} is invalid")
    if label in {"reason", "note", "generic support reason", "resolution assessment note"} and len(value) > 1000:
        raise Stage24AssessmentError(f"{label} exceeds 1000 Unicode code points")
    return value


def _require_obligation_id(value: Any) -> str:
    return _require_pattern(value, "obligation ID", _OBLIGATION_ID_RE)


def _require_pattern(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    canonical = _require_string(value, label)
    if pattern.fullmatch(canonical) is None:
        raise Stage24AssessmentError(f"{label} is invalid")
    return canonical


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise Stage24AssessmentError(f"{label} is invalid")
    return value


def _require_span(start: Any, end: Any, label: str) -> None:
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
    ):
        raise Stage24AssessmentError(f"{label} byte span is invalid")


def _identity_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
