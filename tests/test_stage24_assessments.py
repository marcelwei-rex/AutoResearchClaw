from __future__ import annotations

import json
import hashlib

import pytest

from researchclaw.pipeline.stage24_assessments import (
    Stage24AssessmentError,
    build_citation_assessment_input,
    parse_citation_assessment_record,
)


def _input(
    *,
    paper_sha256: str = "2" * 64,
    source_sha256: str = "3" * 64,
    obligation_id: str = "obl-" + "a" * 64,
    instance_id: str = "citation-instance-0001",
    cite_key: str = "smith2024study",
    critic_model: str = "critic-model",
):
    return build_citation_assessment_input(
        canonical_manifest_sha256="1" * 64,
        paper_sha256=paper_sha256,
        obligation_id=obligation_id,
        byte_start=10,
        byte_end=35,
        source_sha256=source_sha256,
        instance_id=instance_id,
        cite_key=cite_key,
        stage23_verification_record_sha256="4" * 64,
        evidence_records=(
            {
                "card_id": "card-001",
                "card_sha256": "5" * 64,
                "excerpt_id": "excerpt-001",
                "excerpt_sha256": "6" * 64,
                "byte_start": 100,
                "byte_end": 160,
            },
        ),
        critic_model=critic_model,
    )


def _record(value) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "assessment_id": value.assessment_id,
            "assessment_input_sha256": value.assessment_input_sha256,
            "critic_model": value.critic_model,
            "policy_version": value.policy_version,
            "verdict": "supported",
            "reason": "The bound excerpt supports the exact citation claim.",
        }
    )


def test_citation_assessment_record_binds_exact_input_identity() -> None:
    value = _input()

    parsed = parse_citation_assessment_record(
        _record(value), expected_input=value, writer_model="writer-model"
    )

    assert parsed.assessment_id == value.assessment_input_sha256
    assert parsed.verdict == "supported"


def test_citation_assessment_identity_uses_json_bytes_without_newline() -> None:
    value = _input()
    payload = {
        "schema_version": value.schema_version,
        "policy_version": value.policy_version,
        "canonical_manifest_sha256": value.canonical_manifest_sha256,
        "paper_sha256": value.paper_sha256,
        "obligation_id": value.obligation_id,
        "byte_start": value.byte_start,
        "byte_end": value.byte_end,
        "source_sha256": value.source_sha256,
        "instance_id": value.instance_id,
        "cite_key": value.cite_key,
        "stage23_verification_record_sha256": (
            value.stage23_verification_record_sha256
        ),
        "evidence_records": [
            {
                "card_id": item.card_id,
                "card_sha256": item.card_sha256,
                "excerpt_id": item.excerpt_id,
                "excerpt_sha256": item.excerpt_sha256,
                "byte_start": item.byte_start,
                "byte_end": item.byte_end,
            }
            for item in value.evidence_records
        ],
        "critic_model": value.critic_model,
    }
    expected = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()

    assert value.assessment_input_sha256 == expected


def test_generation_a_record_cannot_replay_for_changed_claim_bytes() -> None:
    generation_a = _input()
    generation_b = _input(paper_sha256="7" * 64, source_sha256="8" * 64)

    with pytest.raises(Stage24AssessmentError, match="ID mismatch"):
        parse_citation_assessment_record(
            _record(generation_a),
            expected_input=generation_b,
            writer_model="writer-model",
        )


def test_citation_assessment_record_rejects_duplicate_key() -> None:
    value = _input()
    text = _record(value).replace(
        '"schema_version": 1,',
        '"schema_version": 1, "schema_version": 1,',
    )

    with pytest.raises(Stage24AssessmentError, match="duplicate JSON key"):
        parse_citation_assessment_record(
            text, expected_input=value, writer_model="writer-model"
        )


def test_citation_assessment_record_rejects_writer_model() -> None:
    value = _input()

    with pytest.raises(Stage24AssessmentError, match="not isolated"):
        parse_citation_assessment_record(
            _record(value), expected_input=value, writer_model="critic-model"
        )


def test_citation_assessment_input_rejects_bool_span() -> None:
    with pytest.raises(Stage24AssessmentError, match="byte span"):
        build_citation_assessment_input(
            canonical_manifest_sha256="1" * 64,
            paper_sha256="2" * 64,
            obligation_id="obl-" + "a" * 64,
            byte_start=True,
            byte_end=35,
            source_sha256="3" * 64,
            instance_id="citation-instance-0001",
            cite_key="smith2024study",
            stage23_verification_record_sha256="4" * 64,
            evidence_records=(
                {
                    "card_id": "card-001",
                    "card_sha256": "5" * 64,
                    "excerpt_id": "excerpt-001",
                    "excerpt_sha256": "6" * 64,
                    "byte_start": 100,
                    "byte_end": 160,
                },
            ),
            critic_model="critic-model",
        )


@pytest.mark.parametrize(
    "obligation_id",
    (
        "obl-citation-0001",
        "obl-" + "a" * 63,
        "obl-" + "A" * 64,
        " obl-" + "a" * 64,
        "obl-" + "a" * 64 + " ",
    ),
)
def test_citation_assessment_input_rejects_noncanonical_obligation_id(
    obligation_id: str,
) -> None:
    with pytest.raises(Stage24AssessmentError, match="obligation ID"):
        _input(obligation_id=obligation_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("instance_id", " citation-instance-0001"),
        ("instance_id", "citation instance 0001"),
        ("cite_key", "smith2024study "),
        ("cite_key", "not-a-canonical-key"),
        ("critic_model", " critic-model"),
    ),
)
def test_citation_assessment_input_rejects_string_aliases(
    field: str,
    value: str,
) -> None:
    with pytest.raises(
        Stage24AssessmentError,
        match="(?i)" + field.replace("_", " "),
    ):
        _input(**{field: value})
