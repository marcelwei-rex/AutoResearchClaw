"""B5-A4 frozen structured Stage 24 authority contracts."""

from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
import hashlib
import inspect

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


def test_staged_plan_api_has_no_forbidden_reverse_imports() -> None:
    tree = ast.parse(inspect.getsource(authority))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported & {
        "stage24_structured_transport",
        "stage24_structured_publication",
        "stage24_publication",
        "stage24_assessments",
    }


def test_citation_plan_identity_context_and_evidence_order_golden() -> None:
    paper = b"# Study\n\n## Results\n\nClaim [Smith2024].\n"
    obligations = build_claim_obligation_inventory(paper)
    target = next(row for row in obligations if row.kind == "citation_instance")
    first = {"planned_id": "p2", "excerpt_id": "e2", "rank": 2}
    second = {"planned_id": "p1", "excerpt_id": "e1", "rank": 1}
    plan_input = authority.CitationAssessmentPlanInput(
        canonical_manifest_sha256="2" * 64,
        paper_sha256="3" * 64,
        obligation_id=target.obligation_id,
        byte_start=target.byte_start,
        byte_end=target.byte_end,
        source_sha256=target.source_sha256,
        instance_id=f"cit:{target.obligation_id}",
        cite_key="Smith2024",
        stage23_verification_record_sha256="5" * 64,
        evidence_records=(first, second),
        paper=paper,
        obligations=obligations,
        evidence_cards=(
            {
                "cite_key": "Smith2024",
                "evidence_excerpts": (
                    {"excerpt_id": "e1", "excerpt_text": "first"},
                    {"excerpt_id": "e2", "excerpt_text": "second"},
                ),
            },
        ),
    )
    plan = authority.derive_citation_assessment_plan(
        plan_input,
        client_binding_sha256="1" * 64,
        critic_model="critic-c",
    )
    assert plan.role == "citation_assessment"
    assert plan.assessment_id == (
        "9b7b13a64d7e7e58e684a028644745b12f96dd84bebdfc3457fcba7f0467a07a"
    )
    assert plan.identity["evidence_records"] == [first, second]
    assert plan.bound_context == {
        "manuscript_context": "Claim [Smith2024].",
        "retained_excerpts": [
            {"excerpt_id": "e2", "excerpt_text": "second"},
            {"excerpt_id": "e1", "excerpt_text": "first"},
        ],
    }
    assert plan.obligation_id == target.obligation_id
    assert plan.finding_content_sha256 is None
    assert plan == authority.derive_citation_assessment_plan(
        plan_input,
        client_binding_sha256="1" * 64,
        critic_model="critic-c",
    )


@pytest.mark.parametrize(
    ("target_key", "card_keys"),
    (
        ("Smith2024", ("Smith2024", "Smith2024")),
        ("Smith2024", (None,)),
        ("Smith2024", ("",)),
        (None, ("Smith2024",)),
        ("", ("Smith2024",)),
    ),
)
def test_citation_plan_rejects_invalid_or_duplicate_card_keys(
    target_key: object,
    card_keys: tuple[object, ...],
) -> None:
    paper = b"# Study\n\n## Results\n\nClaim [Smith2024].\n"
    obligations = build_claim_obligation_inventory(paper)
    target = next(row for row in obligations if row.kind == "citation_instance")
    plan_input = authority.CitationAssessmentPlanInput(
        canonical_manifest_sha256="2" * 64,
        paper_sha256="3" * 64,
        obligation_id=target.obligation_id,
        byte_start=target.byte_start,
        byte_end=target.byte_end,
        source_sha256=target.source_sha256,
        instance_id=f"cit:{target.obligation_id}",
        cite_key=target_key,  # type: ignore[arg-type]
        stage23_verification_record_sha256="5" * 64,
        evidence_records=(),
        paper=paper,
        obligations=obligations,
        evidence_cards=tuple(
            {"cite_key": key, "evidence_excerpts": ()} for key in card_keys
        ),
    )
    with pytest.raises(authority.StructuredStage24AuthorityError):
        authority.derive_citation_assessment_plan(
            plan_input,
            client_binding_sha256="1" * 64,
            critic_model="critic-c",
        )


def test_generic_plan_deduplicates_sorts_skips_empty_and_has_no_aliases() -> None:
    paper = b"# Study\n\n## Results\n\nF1 is 90% [Smith2024].\n"
    obligations = build_claim_obligation_inventory(paper)
    sentence = next(row for row in obligations if row.kind == "declarative_sentence")
    numeric = next(row for row in obligations if row.kind == "numeric_token")
    citation = next(row for row in obligations if row.kind == "citation_instance")
    numeric_authority = {
        "cfs": {"schema_version": 1, "sha256": "a" * 64},
        "metric": "f1",
        "display_label": "F1",
        "semantic_pointer": "/results/f1",
        "semantic_value_sha256": "b" * 64,
        "canonical_value": "0.9",
        "unit": "ratio",
        "transform": "percent-to-ratio-v1",
    }
    citation_id = "f" * 64
    citation_content = b'{"verdict":"supported"}\n'
    empty = authority.GenericAssessmentPlanInput(
        canonical_manifest_sha256="7" * 64,
        paper_sha256="8" * 64,
        obligation_id="skip",
        byte_start=0,
        byte_end=1,
        source_sha256="0" * 64,
        paper=b"\xff",
        obligations=(),
        numeric_support={},
        citation_records={},
        citation_record_bytes={},
    )
    assert authority.derive_generic_assessment_plan(
        empty,
        client_binding_sha256="6" * 64,
        critic_model="critic-g",
    ) is None
    plan_input = authority.GenericAssessmentPlanInput(
        canonical_manifest_sha256="7" * 64,
        paper_sha256="8" * 64,
        obligation_id=sentence.obligation_id,
        byte_start=sentence.byte_start,
        byte_end=sentence.byte_end,
        source_sha256=sentence.source_sha256,
        paper=paper,
        obligations=(*obligations, numeric),
        numeric_support={
            numeric.obligation_id: {
                "status": "supported",
                "authority": numeric_authority,
            }
        },
        citation_records={
            citation.obligation_id: {
                "verdict": "supported",
                "assessment_id": citation_id,
            }
        },
        citation_record_bytes={f"{citation_id}.json": citation_content},
    )
    plan = authority.derive_generic_assessment_plan(
        plan_input,
        client_binding_sha256="6" * 64,
        critic_model="critic-g",
    )
    assert plan is not None
    assert plan.assessment_id == (
        "b76d37115fa1bef2af175b129b0d0f0cf39857b409cc4657a24380fad88ef717"
    )
    citation_row = {
        "evidence_kind": "citation_support",
        "authority": {
            "authority_kind": "file",
            "file": {
                "path": f"stage-24/citation-assessments/{citation_id}.json",
                "sha256": _sha(citation_content),
                "size": len(citation_content),
            },
        },
        "semantic_pointer": "/verdict",
        "semantic_value_sha256": _sha(b"supported"),
    }
    numeric_row = {
        "evidence_kind": "numeric_support",
        "authority": numeric_authority,
        "semantic_pointer": "/results/f1",
        "semantic_value_sha256": "b" * 64,
    }
    assert plan.identity["evidence_records"] == [citation_row, numeric_row]
    assert plan.bound_context == {
        "manuscript_sentence": "F1 is 90% [Smith2024].",
        "evidence_records": [citation_row, numeric_row],
    }
    assert plan.identity["evidence_records"] is not plan.bound_context[
        "evidence_records"
    ]
    numeric_authority["semantic_pointer"] = "/mutated"
    assert plan.identity["evidence_records"][1]["semantic_pointer"] == "/results/f1"
    assert plan.bound_context["evidence_records"][1]["semantic_pointer"] == "/results/f1"


def test_resolution_plan_filters_orders_limits_and_matches_golden() -> None:
    p2 = {
        "id": "f-skip",
        "severity": "P2",
        "category": "style",
        "question": "skip?",
        "finding": "skip.",
        "falsification_criterion": "skip.",
    }
    p1 = {
        "id": "f-1",
        "severity": "P1",
        "category": "evidence",
        "question": "Q?",
        "finding": "F.",
        "falsification_criterion": "C.",
    }
    p0 = {**p1, "id": "f-0", "severity": "P0"}
    assert authority.derive_resolution_assessment_plan(
        authority.ResolutionAssessmentPlanInput(
            critique_sha256="d" * 64,
            raw_paper_sha256="e" * 64,
            finding=p2,
            paper=b"\xff",
        ),
        client_binding_sha256="c" * 64,
        critic_model="critic-r",
    ) is None
    plans = tuple(
        plan
        for finding in (p2, p1, p0)
        if (
            plan := authority.derive_resolution_assessment_plan(
                authority.ResolutionAssessmentPlanInput(
                    critique_sha256="d" * 64,
                    raw_paper_sha256="e" * 64,
                    finding=finding,
                    paper=b"Paper.",
                ),
                client_binding_sha256="c" * 64,
                critic_model="critic-r",
            )
        )
        is not None
    )
    assert tuple(plan.bound_context["finding"]["id"] for plan in plans) == (
        "f-1",
        "f-0",
    )
    assert plans[0].assessment_id == (
        "7f74007add7062d34cfc951469cb60b8aa7bb3463d2829e055f6edc75ea1f8f6"
    )
    assert plans[0].finding_content_sha256 == (
        "812037841eb2f2d40e48e7fe2252750ce6f78b4f41caaf479f6e79e2198b6249"
    )
    assert plans[0].obligation_id is None
    assert plans[0].bound_context == {"finding": p1, "paper": "Paper."}
    with pytest.raises(
        authority.StructuredStage24AuthorityError,
        match="resolution assessment count is invalid",
    ):
        authority.validate_assessment_counts(
            citation=0,
            generic=0,
            resolution=13,
        )
