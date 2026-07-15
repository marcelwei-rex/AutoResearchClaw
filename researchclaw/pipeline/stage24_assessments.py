"""Strict source-record contracts for future canonical Stage 24 assessments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

CITATION_ASSESSMENT_SCHEMA_VERSION = 1
CITATION_ASSESSMENT_POLICY_VERSION = "citation_assessment_v1"
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
