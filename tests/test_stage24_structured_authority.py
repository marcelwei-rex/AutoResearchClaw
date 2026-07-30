"""B5-A4 frozen structured Stage 24 authority contracts."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib

import pytest

from researchclaw.pipeline import stage24_structured_authority as authority
from researchclaw.pipeline.stage24_obligations import (
    build_claim_obligation_inventory,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _numeric_binding(
    *,
    value: str,
    label: str,
    pointer: str,
    unit: str = "ratio",
) -> dict[str, object]:
    return {
        "cfs": {"schema_version": 1, "sha256": "a" * 64},
        "metric": "detection_f1",
        "display_label": label,
        "semantic_pointer": pointer,
        "semantic_value_sha256": _sha(value.encode()),
        "canonical_value": value,
        "unit": unit,
        "transform": "unitless-identity-v1",
    }


def test_exact_namespace_manifest_roots_and_kind_rank_are_frozen() -> None:
    assert authority.STRUCTURED_STAGE24_COARSE_ARTIFACTS == (
        "obligation_inventory.json",
        "claims.json",
        "citations.json",
        "citation_support.json",
        "critique_resolution.json",
        "truth_audit.json",
        "citation-assessments/",
        "generic-support-assessments/",
        "resolution-assessments/",
        "stage24_truth_manifest.json",
    )
    assert len(authority.STRUCTURED_STAGE24_MANIFEST_ROOTS) == 29
    assert authority.STRUCTURED_STAGE24_MANIFEST_ROOTS[:6] == (
        "schema_version",
        "publication_stage_id",
        "publication_mode",
        "structured_capability_schema_version",
        "structured_capability_snapshot",
        "generation_binding_sha256",
    )
    assert authority.KIND_RANK == {
        "numeric_token": 0,
        "citation_instance": 1,
        "comparative_sentence": 2,
        "declarative_sentence": 3,
    }
    assert "comparison_support" not in authority.STRUCTURED_STAGE24_MANIFEST_ROOTS


@pytest.mark.parametrize(
    ("op", "left", "right", "expected"),
    (
        ("higher", "0.9", "0.8", "supported"),
        ("greater", "0.9", "0.8", "supported"),
        ("lower", "0.8", "0.9", "supported"),
        ("less", "0.8", "0.9", "supported"),
        ("higher", "0.8", "0.9", "unsupported"),
        ("lower", "0.9", "0.8", "unsupported"),
        ("greater", "0.8", "0.8", "unsupported"),
        ("less", "0.8", "0.8", "unsupported"),
    ),
)
def test_comparative_truth_is_code_owned_exact_decimal(
    op: str,
    left: str,
    right: str,
    expected: str,
) -> None:
    assert authority.decimal_relation_status(
        operator=op,
        left=left,
        right=right,
    ) == expected


def test_comparative_byte_grammar_accepts_only_exact_two_token_sentence() -> None:
    paper = b"# Study\n\n## Results\n\nF1 90% is higher than baseline F1 80%.\n"
    inventory = build_claim_obligation_inventory(paper)
    comparison = next(row for row in inventory if row.kind == "comparative_sentence")
    numeric = tuple(
        row
        for row in inventory
        if row.kind == "numeric_token"
        and row.byte_start >= comparison.byte_start
        and row.byte_end <= comparison.byte_end
    )
    assert len(numeric) == 2
    bindings = {
        numeric[0].obligation_id: _numeric_binding(
            value="0.9", label="F1", pointer="/left"
        ),
        numeric[1].obligation_id: _numeric_binding(
            value="0.8", label="baseline F1", pointer="/right"
        ),
    }
    result = authority.evaluate_comparative_truth(
        paper=paper,
        comparison=comparison,
        numeric_children=numeric,
        numeric_bindings=bindings,
    )
    assert result == {
        "status": "supported",
        "support_record_id": None,
    }

    malformed = paper.replace(b" higher than ", b" higher than if ")
    malformed_inventory = build_claim_obligation_inventory(malformed)
    malformed_comparison = next(
        row for row in malformed_inventory if row.kind == "comparative_sentence"
    )
    malformed_numeric = tuple(
        row
        for row in malformed_inventory
        if row.kind == "numeric_token"
        and row.byte_start >= malformed_comparison.byte_start
        and row.byte_end <= malformed_comparison.byte_end
    )
    malformed_bindings = {
        malformed_numeric[0].obligation_id: _numeric_binding(
            value="0.9", label="F1", pointer="/left"
        ),
        malformed_numeric[1].obligation_id: _numeric_binding(
            value="0.8", label="baseline F1", pointer="/right"
        ),
    }
    assert authority.evaluate_comparative_truth(
        paper=malformed,
        comparison=malformed_comparison,
        numeric_children=malformed_numeric,
        numeric_bindings=malformed_bindings,
    )["status"] == "unsupported"

    forged_term = replace(
        comparison,
        kind_payload={
            **comparison.kind_payload,
            "matched_terms": ["hiµgher"],
        },
    )
    assert authority.evaluate_comparative_truth(
        paper=paper,
        comparison=forged_term,
        numeric_children=numeric,
        numeric_bindings=bindings,
    )["status"] == "unsupported"

    assert authority.evaluate_comparative_truth(
        paper=paper,
        comparison=comparison,
        numeric_children=(*numeric, numeric[1]),
        numeric_bindings=bindings,
    )["status"] == "unsupported"


def test_numeric_authority_uses_exact_decimal_and_rejects_ambiguity() -> None:
    records = (
        {
            "metric": "detection_f1",
            "display_label": "F1",
            "semantic_pointer": "/results/f1",
            "canonical_value": "0.95",
            "unit": "ratio",
        },
    )
    supported = authority.resolve_cfs_numeric_support(
        number_lexeme="95",
        unit_lexeme="%",
        cfs={"schema_version": 1, "sha256": "a" * 64},
        authority_records=records,
    )
    assert supported["canonical_value"] == "0.95"
    assert supported["transform"] == "percent-to-ratio-v1"

    with pytest.raises(authority.StructuredStage24AuthorityError):
        authority.resolve_cfs_numeric_support(
            number_lexeme="95",
            unit_lexeme="%",
            cfs={"schema_version": 1, "sha256": "a" * 64},
            authority_records=records + records,
        )


def test_candidate_universe_is_occurrence_based_and_exact_subsequence() -> None:
    paper = (
        b"# Study\n\n## Results\n\n"
        b"F1 is 90% [same2024].\n\nF1 is 90% [same2024].\n"
    )
    inventory = build_claim_obligation_inventory(paper)
    numeric_ids = frozenset(
        row.obligation_id
        for row in inventory
        if row.kind == "numeric_token"
        and row.kind_payload["numeric_role"] == "claim_numeric"
    )
    universe = authority.derive_generic_candidate_universe(
        inventory,
        supported_numeric_obligation_ids=numeric_ids,
        structurally_closed_citation_obligation_ids=frozenset(
            row.obligation_id
            for row in inventory
            if row.kind == "citation_instance"
        ),
    )
    assert len(universe) == 2
    assert universe[0].obligation_id != universe[1].obligation_id
    assert authority.citation_occurrence_count(inventory) == 2
    with pytest.raises(authority.StructuredStage24AuthorityError):
        authority.validate_assessment_counts(citation=129, generic=0, resolution=0)
    assert authority.validate_assessment_counts(
        citation=2, generic=2, resolution=0
    ) == 4


def test_manifest_v2_rejects_unknown_root_and_comparison_support() -> None:
    manifest = authority.example_manifest_v2()
    assert tuple(manifest) == authority.STRUCTURED_STAGE24_MANIFEST_ROOTS
    authority.validate_stage24_manifest_v2(manifest)
    forged = dict(manifest)
    forged["comparison_support"] = []
    with pytest.raises(authority.StructuredStage24AuthorityError):
        authority.validate_stage24_manifest_v2(forged)

    inconsistent = authority.example_manifest_v2()
    inconsistent["degradation_signal"] = {
        "path": "degradation_signal.json",
        "sha256": "f" * 64,
        "size": 1,
    }
    inconsistent["outcome"] = "degraded"
    with pytest.raises(authority.StructuredStage24AuthorityError):
        authority.validate_stage24_manifest_v2(inconsistent)


def test_manifest_v2_accepts_its_global_canonical_round_trip() -> None:
    content = authority.global_canonical_json_bytes(authority.example_manifest_v2())
    replayed = authority.strict_json_object(content, label="Stage 24 manifest")
    authority.validate_stage24_manifest_v2(replayed)
