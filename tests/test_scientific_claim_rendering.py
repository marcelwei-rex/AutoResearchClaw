"""B2 deterministic scientific-claim construction and rendering."""

from __future__ import annotations

import copy
import hashlib
import sys
from dataclasses import replace
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Decimal,
    getcontext,
    localcontext,
)
from typing import Any
from pathlib import Path

import pytest

from researchclaw.pipeline import scientific_claim_authority as authority
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_scientific_claim_authority import _evidence


SHA_UNKNOWN = "f" * 64
PRIMARY_METRIC = {
    "condition": "trojnet_community_graphsage",
    "key": "auprc",
    "aggregation": "mean_variants_then_mean_seeds_v1",
    "observation_set": "exact_18_variants_per_seed",
    "value": Decimal("1.25"),
}


def _cfs(**primary_overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "primary_metric": {**PRIMARY_METRIC, **primary_overrides},
    }


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: _cfs(),
    )
    return authority.build_scientific_claim_generation_binding(_evidence())


def _selection(
    selected: list[str],
    *,
    ordered: list[str] | None = None,
    connectors: list[Any] | None = None,
) -> bytes:
    ordered_value = list(selected) if ordered is None else ordered
    connector_value = (
        ["NONE"] * max(len(ordered_value) - 1, 0)
        if connectors is None
        else connectors
    )
    return canonical_authority_json_text(
        {
            "selected_claim_ids": selected,
            "ordered_claim_ids": ordered_value,
            "connector_template_ids": connector_value,
        }
    ).encode("utf-8")


def _rehash_claim(claim, **changes: Any):
    changed = replace(claim, **changes)
    payload = changed.to_dict()
    payload["claim_id"] = authority.scientific_claim_id(payload)
    return replace(changed, claim_id=payload["claim_id"])


def test_repeated_construction_is_byte_and_identity_deterministic(binding) -> None:
    first = authority.build_scientific_claim_registry(binding)
    second = authority.build_scientific_claim_registry(binding)

    assert first == second
    assert first.facts_bytes() == second.facts_bytes()
    assert first.claims_bytes() == second.claims_bytes()
    assert first.facts_sha256 == hashlib.sha256(first.facts_bytes()).hexdigest()
    assert first.claims_sha256 == hashlib.sha256(first.claims_bytes()).hexdigest()
    assert [fact.fact_id for fact in first.facts] == [
        fact.fact_id for fact in second.facts
    ]
    assert [claim.claim_id for claim in first.claims] == [
        claim.claim_id for claim in second.claims
    ]


def test_global_decimal_context_does_not_change_registry(binding) -> None:
    original = getcontext().copy()
    try:
        getcontext().prec = 3
        getcontext().rounding = ROUND_CEILING
        first = authority.build_scientific_claim_registry(binding)
        getcontext().prec = 50
        getcontext().rounding = ROUND_FLOOR
        second = authority.build_scientific_claim_registry(binding)
    finally:
        getcontext().prec = original.prec
        getcontext().rounding = original.rounding

    assert first.facts_bytes() == second.facts_bytes()
    assert first.claims_bytes() == second.claims_bytes()


def test_fact_registry_has_code_owned_total_order_and_exact_sources(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    assert [
        (fact.fact_kind, fact.subject_id, fact.predicate_id, fact.unit_id)
        for fact in registry.facts
    ] == [
        ("primary_condition", "primary_metric", "condition", "NONE"),
        ("primary_metric_key", "primary_metric", "metric_key", "NONE"),
        (
            "primary_aggregation",
            "primary_metric",
            "aggregation_policy",
            "NONE",
        ),
        (
            "primary_observation_set",
            "primary_metric",
            "observation_set",
            "NONE",
        ),
        ("primary_metric_value", "primary_metric", "value", "NONE"),
    ]
    assert [fact.source_json_pointer for fact in registry.facts] == [
        "/primary_metric/condition",
        "/primary_metric/key",
        "/primary_metric/aggregation",
        "/primary_metric/observation_set",
        "/primary_metric/value",
    ]
    assert {fact.source_path for fact in registry.facts} == {
        "canonical_experiment_evidence.json"
    }
    assert [fact.object_value for fact in registry.facts] == [
        "trojnet_community_graphsage",
        "auprc",
        "mean_variants_then_mean_seeds_v1",
        "exact_18_variants_per_seed",
        "1.25",
    ]


def test_cfs_and_source_pointer_must_be_exact_not_tolerance_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: _cfs(value=Decimal("1.25000000000000000000000000000000000000000000000001")),
    )
    mismatched = authority.build_scientific_claim_generation_binding(_evidence())
    with pytest.raises(authority.ScientificClaimAuthorityError, match="exact"):
        authority.build_scientific_claim_registry(mismatched)


@pytest.mark.parametrize("field", ["fact_kind", "subject_id", "predicate_id", "unit_id"])
def test_fact_registry_rejects_unknown_closed_registry_value(
    binding, field: str
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    forged = replace(registry.facts[0], **{field: "unknown"})
    tampered = replace(registry, facts=(forged, *registry.facts[1:]))
    with pytest.raises(authority.ScientificClaimAuthorityError, match="fact registry"):
        authority.validate_scientific_claim_registry(tampered, binding=binding)


def test_fact_registry_rejects_duplicate_and_reordered_records(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    for facts in (
        (registry.facts[0], registry.facts[0], *registry.facts[2:]),
        (registry.facts[1], registry.facts[0], *registry.facts[2:]),
    ):
        with pytest.raises(authority.ScientificClaimAuthorityError, match="fact registry"):
            authority.validate_scientific_claim_registry(
                replace(registry, facts=facts),
                binding=binding,
            )


def test_claim_registry_is_closed_before_selection(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    assert len(registry.claims) == 1
    claim = registry.claims[0]
    assert claim.claim_kind == "primary_metric_result"
    assert claim.section_id == "results"
    assert claim.renderer_template_id == "result.primary_metric.v1"
    assert claim.mandatory is True
    assert claim.evidence_fact_ids == tuple(
        sorted(fact.fact_id for fact in registry.facts)
    )
    assert claim.renderer_slot_fact_ids == tuple(
        fact.fact_id for fact in registry.facts
    )


def test_claim_registry_rejects_unknown_fact_and_mixed_generation(
    binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    unknown_claim = _rehash_claim(
        registry.claims[0],
        evidence_fact_ids=tuple(sorted((*registry.claims[0].evidence_fact_ids[:-1], SHA_UNKNOWN))),
        renderer_slot_fact_ids=(
            *registry.claims[0].renderer_slot_fact_ids[:-1],
            SHA_UNKNOWN,
        ),
    )
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.validate_scientific_claim_registry(
            replace(registry, claims=(unknown_claim,)),
            binding=binding,
        )

    other_evidence = _evidence(
        _summary_content=canonical_authority_json_text(
            {"flag": True, "label": "other", "value": Decimal("1.25")}
        ).encode("utf-8")
    )
    other_binding = authority.build_scientific_claim_generation_binding(other_evidence)
    other_registry = authority.build_scientific_claim_registry(other_binding)
    mixed = replace(
        registry,
        facts=(*registry.facts[:-1], other_registry.facts[-1]),
    )
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.validate_scientific_claim_registry(mixed, binding=binding)


@pytest.mark.parametrize(
    "template_id,slot_indexes",
    [
        ("unknown.template", (0, 1, 2, 3, 4)),
        ("result.primary_metric.v1", (0, 1, 2, 3)),
        ("result.primary_metric.v1", (0, 1, 2, 3, 4, 4)),
        ("result.primary_metric.v1", (0, 1, 2, 2, 4)),
        ("result.primary_metric.v1", (1, 0, 2, 3, 4)),
    ],
)
def test_renderer_rejects_unknown_template_and_wrong_slots(
    binding, template_id: str, slot_indexes: tuple[int, ...]
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    slot_ids = tuple(registry.facts[index].fact_id for index in slot_indexes)
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.render_scientific_claim(
            template_id,
            slot_ids,
            registry=registry,
            binding=binding,
        )


def test_renderer_uses_canonical_values_without_float_conversion(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    rendered = authority.render_scientific_claim(
        registry.claims[0].renderer_template_id,
        registry.claims[0].renderer_slot_fact_ids,
        registry=registry,
        binding=binding,
    )
    assert rendered.content == (
        b"For primary condition trojnet_community_graphsage, AUPRC under "
        b"mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed "
        b"was 1.25."
    )
    assert rendered.sentence == rendered.content.decode("utf-8")
    assert rendered.sha256 == hashlib.sha256(rendered.content).hexdigest()


@pytest.mark.parametrize(
    "sentence",
    [
        "Changed sentence.",
        "For primary condition trojnet_community_graphsage, AUPRC under "
        "mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed "
        "was 1.25!",
        "For primary condition  trojnet_community_graphsage, AUPRC under "
        "mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed "
        "was 1.25.",
        "For primary condition trojnet_community_graphsage, AUPRC under "
        "mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed "
        "was １.25.",
    ],
)
def test_claim_rerender_rejects_sentence_hash_and_id_synchronized_tamper(
    binding, sentence: str
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    forged = _rehash_claim(
        registry.claims[0],
        rendered_sentence=sentence,
        rendered_sentence_sha256=hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
    )
    with pytest.raises(authority.ScientificClaimAuthorityError, match="rerender"):
        authority.validate_scientific_claim_registry(
            replace(registry, claims=(forged,)),
            binding=binding,
        )


@pytest.mark.parametrize(
    "candidate",
    [
        "line one\nline two.",
        "line one\rline two.",
        "bad\u0001control.",
        "# Heading.",
        "Value [1].",
        "Value (Smith, 2024).",
        "First. Second.",
        "No terminal punctuation",
    ],
)
def test_rendered_sentence_policy_rejects_forbidden_output(candidate: str) -> None:
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority._validate_rendered_sentence(candidate)


def test_selection_semantics_reject_unknown_other_section_and_mandatory_omission(
    binding,
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    claim_id = registry.claims[0].claim_id
    with pytest.raises(authority.ScientificClaimAuthorityError, match="unknown"):
        authority.validate_scientific_claim_selection(
            _selection([SHA_UNKNOWN]),
            target_section="results",
            binding=binding,
        )
    with pytest.raises(authority.ScientificClaimAuthorityError, match="section"):
        authority.validate_scientific_claim_selection(
            _selection([claim_id]),
            target_section="abstract",
            binding=binding,
        )
    with pytest.raises(authority.ScientificClaimAuthorityError, match="mandatory"):
        authority.validate_scientific_claim_selection(
            _selection([]),
            target_section="results",
            binding=binding,
        )


def test_selection_preserves_exact_permutation_and_none_connector_policy(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    claim_id = registry.claims[0].claim_id
    selection = authority.validate_scientific_claim_selection(
        _selection([claim_id]),
        target_section="results",
        binding=binding,
    )
    assert selection.selected_claim_ids == (claim_id,)
    assert selection.ordered_claim_ids == (claim_id,)
    assert selection.connector_template_ids == ()

    for content in (
        _selection([claim_id], ordered=[]),
        _selection([claim_id], ordered=[claim_id, claim_id], connectors=["NONE"]),
        _selection([claim_id], connectors=["NONE"]),
        _selection([claim_id], connectors=[{"id": "NONE"}]),
        _selection([claim_id], connectors=["because"]),
    ):
        with pytest.raises(authority.ScientificClaimAuthorityError):
            authority.validate_scientific_claim_selection(
                content,
                target_section="results",
                binding=binding,
            )


def test_none_connector_emits_zero_bytes_and_join_adds_one_ascii_space() -> None:
    assert authority.render_connector("NONE") == b""
    assert authority._join_rendered_sentences(
        (b"Method A had AUPRC 0.80.", b"Method B had AUPRC 0.70."),
        ("NONE",),
    ) == b"Method A had AUPRC 0.80. Method B had AUPRC 0.70."
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.render_connector("NONE ")
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.render_connector({"id": "NONE"})


def test_selection_rendering_uses_only_code_built_registry(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    claim_id = registry.claims[0].claim_id
    rendered = authority.render_scientific_claim_selection(
        _selection([claim_id]),
        target_section="results",
        binding=binding,
    )
    assert rendered == registry.claims[0].rendered_sentence.encode("utf-8")
    with pytest.raises(TypeError):
        authority.render_scientific_claim_selection(
            _selection([claim_id]),
            target_section="results",
            binding=binding,
            template="caller {prose}",
        )


def test_source_inventory_late_mutation_is_rejected(
    binding,
) -> None:
    mutated_manifest = copy.deepcopy(binding.evidence.manifest)
    mutated_manifest["primary_metric"]["value"] = Decimal("9.5")
    binding.evidence.manifest.clear()
    binding.evidence.manifest.update(mutated_manifest)
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.build_scientific_claim_registry(binding)


def test_b2_apis_do_not_write_artifacts(binding, tmp_path) -> None:
    before = tuple(tmp_path.iterdir())
    registry = authority.build_scientific_claim_registry(binding)
    claim_id = registry.claims[0].claim_id
    authority.render_scientific_claim_selection(
        _selection([claim_id]),
        target_section="results",
        binding=binding,
    )
    assert tuple(tmp_path.iterdir()) == before


def test_decimal_context_guard_covers_local_context_changes(binding) -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_FLOOR
        low = authority.build_scientific_claim_registry(binding)
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_CEILING
        high = authority.build_scientific_claim_registry(binding)
    assert low == high
