"""B2-B governed-section scientific claim registries."""

from __future__ import annotations

import copy
import hashlib
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from researchclaw.pipeline import scientific_claim_authority as authority
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_scientific_claim_authority import _evidence


PRIMARY_METRIC = {
    "condition": "trojnet_community_graphsage",
    "key": "auprc",
    "aggregation": "mean_variants_then_mean_seeds_v1",
    "observation_set": "exact_18_variants_per_seed",
    "value": Decimal("1.25"),
}
COMPLETE_CFS_FIELDS = {
    "schema_version",
    "dataset_origin",
    "claim_scope",
    "bound_labels",
    "conditions",
    "seeds",
    "circuit_families",
    "variants_per_family",
    "variant_ids",
    "counts",
    "metric_keys",
    "primary_metric",
    "condition_aggregates",
    "per_seed_aggregates",
    "scale",
    "runtime",
    "derived_facts",
    "provenance",
    "observation_rows",
}
EXPECTED_TEMPLATE_MATRIX = (
    (
        "abstract.primary_metric.v1",
        "primary_metric_summary",
        "abstract",
        True,
        (
            "primary_metric_key",
            "primary_metric_value",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        "The governed evaluation reported AUPRC of 1.25 for primary condition "
        "trojnet_community_graphsage under mean_variants_then_mean_seeds_v1 "
        "over exact_18_variants_per_seed.",
    ),
    (
        "result.primary_metric.v1",
        "primary_metric_result",
        "results",
        True,
        (
            "primary_condition",
            "primary_metric_key",
            "primary_aggregation",
            "primary_observation_set",
            "primary_metric_value",
        ),
        "For primary condition trojnet_community_graphsage, AUPRC under "
        "mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed "
        "was 1.25.",
    ),
    (
        "result.primary_metric_scope.v1",
        "primary_metric_scope",
        "results",
        False,
        (
            "primary_aggregation",
            "primary_observation_set",
            "primary_condition",
        ),
        "The primary result uses mean_variants_then_mean_seeds_v1 over "
        "exact_18_variants_per_seed for condition trojnet_community_graphsage.",
    ),
    (
        "discussion.primary_metric_scope.v1",
        "primary_metric_scope_interpretation",
        "discussion",
        True,
        (
            "primary_metric_key",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        "Interpretation of the primary AUPRC result for "
        "trojnet_community_graphsage is scoped to "
        "mean_variants_then_mean_seeds_v1 over exact_18_variants_per_seed.",
    ),
    (
        "limitation.primary_metric_scope.v1",
        "primary_metric_scope_limitation",
        "limitations",
        True,
        (
            "primary_metric_key",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        "The reported primary AUPRC result pertains to "
        "trojnet_community_graphsage under mean_variants_then_mean_seeds_v1 "
        "over exact_18_variants_per_seed.",
    ),
    (
        "conclusion.primary_metric.v1",
        "primary_metric_conclusion",
        "conclusion",
        True,
        (
            "primary_metric_key",
            "primary_metric_value",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        "The governed evidence records AUPRC of 1.25 for "
        "trojnet_community_graphsage under mean_variants_then_mean_seeds_v1 "
        "over exact_18_variants_per_seed.",
    ),
)


def _complete_cfs(**primary_overrides: Any) -> dict[str, Any]:
    cfs = {
        "schema_version": 1,
        "dataset_origin": "Trust-HUB",
        "claim_scope": "fixture scope",
        "bound_labels": {},
        "conditions": (
            {"id": "trojnet_community_graphsage", "role": "primary"},
            {"id": "scoap_isolation_forest", "role": "comparator"},
        ),
        "seeds": (),
        "circuit_families": (),
        "variants_per_family": {},
        "variant_ids": (),
        "counts": {},
        "metric_keys": ("auprc",),
        "primary_metric": {**PRIMARY_METRIC, **primary_overrides},
        "condition_aggregates": (),
        "per_seed_aggregates": (),
        "scale": {},
        "runtime": {},
        "derived_facts": {},
        "provenance": {},
        "observation_rows": (),
    }
    assert set(cfs) == COMPLETE_CFS_FIELDS
    return cfs


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: _complete_cfs(),
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


def test_complete_registry_freezes_all_section_template_semantics(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    fact_kind_by_id = {fact.fact_id: fact.fact_kind for fact in registry.facts}

    actual = tuple(
        (
            claim.renderer_template_id,
            claim.claim_kind,
            claim.section_id,
            claim.mandatory,
            tuple(
                fact_kind_by_id[fact_id]
                for fact_id in claim.renderer_slot_fact_ids
            ),
            claim.rendered_sentence,
        )
        for claim in registry.claims
    )
    assert actual == EXPECTED_TEMPLATE_MATRIX
    assert [fact.fact_kind for fact in registry.facts] == [
        "primary_condition",
        "primary_metric_key",
        "primary_aggregation",
        "primary_observation_set",
        "primary_metric_value",
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


def test_every_governed_section_has_mandatory_selection(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    for section in (
        "abstract",
        "results",
        "discussion",
        "limitations",
        "conclusion",
    ):
        mandatory = [
            claim.claim_id
            for claim in registry.claims
            if claim.section_id == section and claim.mandatory
        ]
        assert len(mandatory) == 1
        selection = authority.validate_scientific_claim_selection(
            _selection(mandatory),
            target_section=section,
            binding=binding,
        )
        assert selection.ordered_claim_ids == tuple(mandatory)


def test_limitation_is_nonexclusive_when_complete_cfs_has_comparator(
    binding,
) -> None:
    cfs = _complete_cfs()
    assert {condition["role"] for condition in cfs["conditions"]} == {
        "primary",
        "comparator",
    }
    registry = authority.build_scientific_claim_registry(binding)
    limitation = next(
        claim
        for claim in registry.claims
        if claim.renderer_template_id == "limitation.primary_metric_scope.v1"
    )
    assert limitation.rendered_sentence == (
        "The reported primary AUPRC result pertains to "
        "trojnet_community_graphsage under mean_variants_then_mean_seeds_v1 "
        "over exact_18_variants_per_seed."
    )
    assert "evidence is limited to" not in limitation.rendered_sentence


def test_results_two_claim_reverse_reorder_none_and_optional_delete(binding) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    results = [claim for claim in registry.claims if claim.section_id == "results"]
    assert len(results) == 2
    assert [claim.mandatory for claim in results] == [True, False]

    ordered = [results[1].claim_id, results[0].claim_id]
    rendered = authority.render_scientific_claim_selection(
        _selection(
            [results[0].claim_id, results[1].claim_id],
            ordered=ordered,
            connectors=["NONE"],
        ),
        target_section="results",
        binding=binding,
    )
    assert rendered == (
        results[1].rendered_sentence.encode("utf-8")
        + b" "
        + results[0].rendered_sentence.encode("utf-8")
    )
    assert authority.render_scientific_claim_selection(
        _selection([results[0].claim_id]),
        target_section="results",
        binding=binding,
    ) == results[0].rendered_sentence.encode("utf-8")


@pytest.mark.parametrize("view", ("results", "limitations"))
def test_generation_binding_rejects_scoped_cfs_view(
    monkeypatch: pytest.MonkeyPatch, view: str
) -> None:
    complete = _complete_cfs()
    if view == "results":
        scoped = {
            key: complete[key]
            for key in (
                "schema_version",
                "claim_scope",
                "dataset_origin",
                "bound_labels",
                "conditions",
                "primary_metric",
                "condition_aggregates",
                "per_seed_aggregates",
            )
        }
    else:
        scoped = {
            key: complete[key]
            for key in (
                "schema_version",
                "claim_scope",
                "dataset_origin",
                "bound_labels",
                "conditions",
                "primary_metric",
                "counts",
                "seeds",
                "circuit_families",
                "variant_ids",
                "variants_per_family",
                "scale",
                "derived_facts",
            )
        }
    scoped["authority_availability"] = {
        "top_level_omission_semantics": "withheld_not_absent"
    }
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: scoped,
    )
    with pytest.raises(
        authority.ScientificClaimAuthorityError,
        match="complete CFS",
    ):
        authority.build_scientific_claim_generation_binding(_evidence())


@pytest.mark.parametrize("claim_index", range(len(EXPECTED_TEMPLATE_MATRIX)))
def test_every_claim_rejects_synchronized_sentence_hash_and_id_tamper(
    binding, claim_index: int
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    claim = registry.claims[claim_index]
    sentence = f"Changed {claim.rendered_sentence}"
    forged = _rehash_claim(
        claim,
        rendered_sentence=sentence,
        rendered_sentence_sha256=hashlib.sha256(
            sentence.encode("utf-8")
        ).hexdigest(),
    )
    claims = list(registry.claims)
    claims[claim_index] = forged
    with pytest.raises(
        authority.ScientificClaimAuthorityError,
        match="rerender",
    ):
        authority.validate_scientific_claim_registry(
            replace(registry, claims=tuple(claims)),
            binding=binding,
        )


@pytest.mark.parametrize("claim_index", range(len(EXPECTED_TEMPLATE_MATRIX)))
def test_every_template_rejects_wrong_slot_arity_and_order(
    binding, claim_index: int
) -> None:
    registry = authority.build_scientific_claim_registry(binding)
    claim = registry.claims[claim_index]
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.render_scientific_claim(
            claim.renderer_template_id,
            claim.renderer_slot_fact_ids[:-1],
            registry=registry,
            binding=binding,
        )
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.render_scientific_claim(
            claim.renderer_template_id,
            tuple(reversed(claim.renderer_slot_fact_ids)),
            registry=registry,
            binding=binding,
        )
    with pytest.raises(authority.ScientificClaimAuthorityError, match="unknown"):
        authority.render_scientific_claim(
            f"{claim.renderer_template_id}.unknown",
            claim.renderer_slot_fact_ids,
            registry=registry,
            binding=binding,
        )


def test_late_evidence_mutation_with_synchronized_surface_hash_is_rejected(
    binding,
) -> None:
    manifest = copy.deepcopy(binding.evidence.manifest)
    manifest["primary_metric"]["value"] = Decimal("9.5")
    manifest_sha256 = hashlib.sha256(
        canonical_authority_json_text(manifest).encode("utf-8")
    ).hexdigest()
    mutated_evidence = replace(
        binding.evidence,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )
    forged_binding = replace(binding, evidence=mutated_evidence)
    with pytest.raises(authority.ScientificClaimAuthorityError):
        authority.build_scientific_claim_registry(forged_binding)


def test_registry_rejects_binary_float_primary_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: _complete_cfs(value=1.25),
    )
    with pytest.raises(authority.ScientificClaimAuthorityError):
        binding = authority.build_scientific_claim_generation_binding(_evidence())
        authority.build_scientific_claim_registry(binding)
