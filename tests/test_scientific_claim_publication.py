"""B3-A structured Stage 17 in-memory publication contracts and replay."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from researchclaw.pipeline import scientific_claim_authority as authority
from researchclaw.pipeline import scientific_claim_publication as publication
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_scientific_claim_authority import _evidence


SECTIONS = ("abstract", "results", "discussion", "limitations", "conclusion")


def _complete_cfs() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_origin": "Trust-HUB",
        "claim_scope": "fixture scope",
        "bound_labels": {},
        "conditions": (),
        "seeds": (),
        "circuit_families": (),
        "variants_per_family": {},
        "variant_ids": (),
        "counts": {},
        "metric_keys": ("auprc",),
        "primary_metric": {
            "condition": "trojnet_community_graphsage",
            "key": "auprc",
            "aggregation": "mean_variants_then_mean_seeds_v1",
            "observation_set": "exact_18_variants_per_seed",
            "value": Decimal("1.25"),
        },
        "condition_aggregates": (),
        "per_seed_aggregates": (),
        "scale": {},
        "runtime": {},
        "derived_facts": {},
        "provenance": {},
        "observation_rows": (),
    }


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(authority, "build_canonical_fact_sheet", lambda _evidence: _complete_cfs())
    return authority.build_scientific_claim_generation_binding(_evidence())


@pytest.fixture(autouse=True)
def complete_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    monkeypatch.setattr(
        publication,
        "replay_experiment_fact_closure",
        lambda **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        publication,
        "build_citation_closure_from_texts",
        lambda **_kwargs: {"valid": True},
    )
    monkeypatch.setattr(
        publication,
        "parse_citation_closure_report",
        lambda text: json.loads(text),
    )


def _provider_responses(binding) -> tuple[bytes, ...]:
    registry = authority.build_scientific_claim_registry(binding)
    responses = []
    for section in SECTIONS:
        ids = [
            claim.claim_id
            for claim in registry.claims
            if claim.section_id == section
        ]
        responses.append(
            canonical_authority_json_text(
                {
                    "selected_claim_ids": ids,
                    "ordered_claim_ids": ids,
                    "connector_template_ids": ["NONE"] * max(len(ids) - 1, 0),
                }
            ).encode("utf-8")
        )
    return tuple(responses)


def _artifacts(binding):
    registry = authority.build_scientific_claim_registry(binding)
    selection = publication.build_scientific_claim_selection(
        _provider_responses(binding),
        binding=binding,
    )
    return registry.facts_bytes(), registry.claims_bytes(), selection


def _manifest_kwargs(binding) -> dict[str, bytes]:
    facts, claims, selection = _artifacts(binding)
    rendered = publication.replay_scientific_claim_selection_wrapper(
        selection,
        binding=binding,
    )
    paper_parts = [b"# Test Paper\n\n", b"## Introduction\n\nUngoverned prose.\n\n"]
    for section_id, section_content in rendered:
        heading = section_id.title()
        paper_parts.append(
            f"## {heading}\n\n".encode("utf-8") + section_content + b"\n\n"
        )
    paper = b"".join(paper_parts)
    structure = publication.build_structured_paper_structure_report(
        paper,
        selection,
        binding=binding,
    )
    return {
        "facts_content": facts,
        "claim_registry_content": claims,
        "claim_selection_content": selection,
        "paper_draft_content": paper,
        "paper_structure_report_content": structure,
        "experiment_fact_closure_report_content": b'{"valid":true}\n',
        "citation_closure_report_content": b'{\n  "valid": true\n}\n',
        "citation_plan_content": b'{"fixture":"plan"}\n',
        "citation_allowlist_content": b'{"fixture":"allowlist"}\n',
    }


def test_facts_and_registry_are_direct_ordered_exact_replays(binding) -> None:
    facts, claims, _selection = _artifacts(binding)
    assert len(publication.parse_scientific_evidence_facts(facts, binding=binding)) == 5
    assert len(publication.parse_scientific_claim_registry(claims, binding=binding)) == 6

    facts_value = publication.parse_canonical_publication_json(facts, label="facts")
    claims_value = publication.parse_canonical_publication_json(claims, label="claims")
    for changed in (
        facts_value[:-1],
        facts_value + [facts_value[-1]],
        [facts_value[1], facts_value[0], *facts_value[2:]],
    ):
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.parse_scientific_evidence_facts(
                canonical_authority_json_text(changed).encode("utf-8"),
                binding=binding,
            )
    for changed in (
        claims_value[:-1],
        claims_value + [claims_value[-1]],
        [claims_value[1], claims_value[0], *claims_value[2:]],
    ):
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.parse_scientific_claim_registry(
                canonical_authority_json_text(changed).encode("utf-8"),
                binding=binding,
            )


def test_provider_cannot_supply_section_or_root_bindings(binding) -> None:
    responses = list(_provider_responses(binding))
    value = publication.parse_canonical_publication_json(
        responses[0], label="provider response"
    )
    for field, supplied in (
        ("section_id", "abstract"),
        ("schema_version", 1),
        ("claim_registry_sha256", "0" * 64),
    ):
        forged = {**value, field: supplied}
        responses[0] = canonical_authority_json_text(forged).encode("utf-8")
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.build_scientific_claim_selection(responses, binding=binding)
        responses[0] = _provider_responses(binding)[0]


def test_selection_wrapper_rejects_reordered_missing_duplicate_and_mixed(binding) -> None:
    _facts, _claims, selection = _artifacts(binding)
    parsed = publication.parse_canonical_publication_json(selection, label="selection")
    publication.parse_scientific_claim_selection_wrapper(selection, binding=binding)
    mutations = []
    reordered = copy.deepcopy(parsed)
    reordered["sections"][0], reordered["sections"][1] = (
        reordered["sections"][1],
        reordered["sections"][0],
    )
    mutations.append(reordered)
    missing = copy.deepcopy(parsed)
    missing["sections"].pop()
    mutations.append(missing)
    duplicate = copy.deepcopy(parsed)
    duplicate["sections"][-1] = copy.deepcopy(duplicate["sections"][0])
    mutations.append(duplicate)
    unknown = copy.deepcopy(parsed)
    unknown["sections"][-1]["section_id"] = "methods"
    mutations.append(unknown)
    mixed = copy.deepcopy(parsed)
    mixed["generation_binding_sha256"] = "f" * 64
    mutations.append(mixed)
    for changed in mutations:
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.parse_scientific_claim_selection_wrapper(
                canonical_authority_json_text(changed).encode("utf-8"),
                binding=binding,
            )


def test_duplicate_keys_unknown_missing_and_bool_versions_reject(binding) -> None:
    _facts, _claims, selection = _artifacts(binding)
    duplicate = selection.replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(publication.ScientificClaimPublicationError):
        publication.parse_scientific_claim_selection_wrapper(duplicate, binding=binding)
    parsed = publication.parse_canonical_publication_json(selection, label="selection")
    for changed in (
        {**parsed, "unknown": None},
        {key: value for key, value in parsed.items() if key != "claim_policy_id"},
        {**parsed, "schema_version": True},
    ):
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.parse_scientific_claim_selection_wrapper(
                canonical_authority_json_text(changed).encode("utf-8"),
                binding=binding,
            )


def test_stage17_manifest_is_exact_guarded_and_independently_rebuilt(
    binding,
    complete_capability,
) -> None:
    kwargs = _manifest_kwargs(binding)
    manifest = publication.build_stage17_scientific_claim_authority_manifest(
        binding=binding,
        **kwargs,
    )
    parsed = publication.parse_stage17_scientific_claim_authority_manifest(
        manifest,
        binding=binding,
        **kwargs,
    )
    assert parsed["publication_stage_id"] == "stage17"
    assert parsed["source_authority_manifest"] is None
    assert "manifest_path" not in parsed
    assert "manifest_sha256" not in parsed
    assert parsed["facts"]["record_count"] == 5
    assert type(parsed["facts"]["record_count"]) is int


def test_manifest_rejects_forged_snapshot_self_hash_null_fileref_and_bool_count(
    binding,
    complete_capability,
) -> None:
    kwargs = _manifest_kwargs(binding)
    manifest = publication.build_stage17_scientific_claim_authority_manifest(
        binding=binding,
        **kwargs,
    )
    parsed = publication.parse_canonical_publication_json(manifest, label="manifest")
    mutations = []
    forged = copy.deepcopy(parsed)
    forged["structured_capability_snapshot"]["stage19_revision"] = 0
    mutations.append(forged)
    self_hash = {**parsed, "manifest_sha256": hashlib.sha256(manifest).hexdigest()}
    mutations.append(self_hash)
    source_ref = copy.deepcopy(parsed)
    source_ref["source_authority_manifest"] = {
        "path": "stage-17/old.json",
        "sha256": "0" * 64,
    }
    mutations.append(source_ref)
    bool_count = copy.deepcopy(parsed)
    bool_count["facts"]["record_count"] = True
    mutations.append(bool_count)
    extra_ref = copy.deepcopy(parsed)
    extra_ref["facts"]["file"]["size"] = 1
    mutations.append(extra_ref)
    for changed in mutations:
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.parse_stage17_scientific_claim_authority_manifest(
                canonical_authority_json_text(changed).encode("utf-8"),
                binding=binding,
                **kwargs,
            )


def test_direct_entries_reject_before_parsing_when_inactive(
    binding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _manifest_kwargs(binding)
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 0 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.parse_stage17_scientific_claim_authority_manifest(
            b"not json",
            binding=binding,
            **kwargs,
        )
    direct_calls = (
        lambda: publication.parse_scientific_evidence_facts(
            b"not json", binding=binding
        ),
        lambda: publication.parse_scientific_claim_registry(
            b"not json", binding=binding
        ),
        lambda: publication.build_scientific_claim_selection(
            (), binding=binding
        ),
        lambda: publication.parse_scientific_claim_selection_wrapper(
            b"not json", binding=binding
        ),
        lambda: publication.replay_scientific_claim_selection_wrapper(
            b"not json", binding=binding
        ),
    )
    for call in direct_calls:
        with pytest.raises(
            capability.StructuredScientificClaimCapabilityIncomplete
        ):
            call()


def test_manifest_apis_accept_no_caller_capability_authority() -> None:
    for function in (
        publication.build_stage17_scientific_claim_authority_manifest,
        publication.parse_stage17_scientific_claim_authority_manifest,
    ):
        parameters = inspect.signature(function).parameters
        assert "capabilities" not in parameters
        assert "capability_snapshot" not in parameters
        assert "structured_capability_schema_version" not in parameters


def test_synchronously_rehashed_claim_graph_still_fails_code_owned_rebuild(
    binding,
    complete_capability,
) -> None:
    kwargs = _manifest_kwargs(binding)
    claims = publication.parse_canonical_publication_json(
        kwargs["claim_registry_content"],
        label="claim registry",
    )
    selections = publication.parse_canonical_publication_json(
        kwargs["claim_selection_content"],
        label="claim selection",
    )
    old_id = claims[0]["claim_id"]
    claims[0]["rendered_sentence"] = "Forged but internally rehashed."
    claims[0]["rendered_sentence_sha256"] = hashlib.sha256(
        claims[0]["rendered_sentence"].encode("utf-8")
    ).hexdigest()
    claims[0]["claim_id"] = authority.scientific_claim_id(claims[0])
    new_id = claims[0]["claim_id"]
    section = selections["sections"][0]
    section["selected_claim_ids"] = [
        new_id if item == old_id else item for item in section["selected_claim_ids"]
    ]
    section["ordered_claim_ids"] = [
        new_id if item == old_id else item for item in section["ordered_claim_ids"]
    ]
    forged_claims = canonical_authority_json_text(claims).encode("utf-8")
    selections["claim_registry_sha256"] = hashlib.sha256(forged_claims).hexdigest()
    forged_selection = canonical_authority_json_text(selections).encode("utf-8")
    forged_kwargs = {
        **kwargs,
        "claim_registry_content": forged_claims,
        "claim_selection_content": forged_selection,
    }
    with pytest.raises(
        publication.ScientificClaimPublicationError,
        match="claim registry replay failed|code-owned ordered construction",
    ):
        publication.build_stage17_scientific_claim_authority_manifest(
            binding=binding,
            **forged_kwargs,
        )


def test_synchronously_rehashed_paper_and_manifest_still_fail_reassembly(
    binding,
) -> None:
    kwargs = _manifest_kwargs(binding)
    forged = {
        **kwargs,
        "paper_draft_content": b"forged paper",
    }
    with pytest.raises(
        publication.ScientificClaimPublicationError,
        match="structured paper|governed",
    ):
        publication.build_stage17_scientific_claim_authority_manifest(
            binding=binding,
            **forged,
        )


def test_paper_replay_rejects_governed_reorder_and_blank_line_alias(
    binding,
) -> None:
    kwargs = _manifest_kwargs(binding)
    paper = kwargs["paper_draft_content"]
    abstract_start = paper.index(b"## Abstract\n")
    results_start = paper.index(b"## Results\n")
    discussion_start = paper.index(b"## Discussion\n")
    reordered = (
        paper[:abstract_start]
        + paper[results_start:discussion_start]
        + paper[abstract_start:results_start]
        + paper[discussion_start:]
    )
    extra_blank = paper.replace(b"## Abstract\n\n", b"## Abstract\n\n\n", 1)
    for changed in (reordered, extra_blank):
        with pytest.raises(publication.ScientificClaimPublicationError):
            publication.replay_structured_scientific_claim_paper(
                changed,
                kwargs["claim_selection_content"],
                binding=binding,
            )


def test_experiment_closure_requires_existing_canonical_exact_bytes(binding) -> None:
    kwargs = _manifest_kwargs(binding)
    forged = {
        **kwargs,
        "experiment_fact_closure_report_content": b'{ "valid": true }\n',
    }
    with pytest.raises(
        publication.ScientificClaimPublicationError,
        match="experiment fact closure bytes are not canonical",
    ):
        publication.build_stage17_scientific_claim_authority_manifest(
            binding=binding,
            **forged,
        )
