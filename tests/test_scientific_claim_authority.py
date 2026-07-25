"""B1 contracts and identity for structured scientific claims."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from researchclaw.experiment_runtime.contract import parse_contract_bytes
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CAPABILITY_SCHEMA_VERSION,
    REQUIRED_CAPABILITIES,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalEvidenceArtifact,
    CanonicalExperimentEvidence,
    canonical_authority_json_text,
)
from researchclaw.pipeline.scientific_claim_authority import (
    CLAIM_POLICY_ID,
    EvidenceFact,
    ScientificClaimAuthorityError,
    ScientificClaimRecord,
    bind_scientific_claim_source,
    build_scientific_claim_generation_binding,
    evidence_fact_id,
    parse_evidence_fact,
    parse_scientific_claim_record,
    parse_scientific_claim_selection,
    scientific_claim_id,
)


SHA_A = "a" * 64
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "canonical_fact_sheet"
CONTRACT_BYTES = (FIXTURE_DIR / "experiment_contract.yaml").read_bytes()
EXECUTION_POLICY_BYTES = (FIXTURE_DIR / "execution_policy.json").read_bytes()
CONFIG_BYTES = b"config\n"
SOURCE_CONTENT = canonical_authority_json_text(
    {"flag": True, "label": "alpha", "value": Decimal("1.25")}
).encode("utf-8")
SOURCE_SHA256 = hashlib.sha256(SOURCE_CONTENT).hexdigest()
ANALYSIS_CONTENT = b"Canonical analysis.\n"
SELECTED_EXECUTION_CONTENT = b'{"selected":"execution"}\n'
SELECTED_EXECUTION_PATH = "stage-12/evidence-v2/observations.json"
SELECTED_RESULT_PATH = "stage-13/refinement_result_set.json"
EXECUTION_POLICY_PATH = "stage-09/domain_evaluator_execution_policy.json"
SHA_B = hashlib.sha256(CONTRACT_BYTES).hexdigest()
SHA_C = hashlib.sha256(CONFIG_BYTES).hexdigest()
FACT_A = "1" * 64
FACT_B = "2" * 64


def _evidence(**overrides: Any) -> CanonicalExperimentEvidence:
    summary_content = overrides.pop("_summary_content", SOURCE_CONTENT)
    contract = parse_contract_bytes(CONTRACT_BYTES)

    def ref(path: str, content: bytes) -> dict[str, Any]:
        return {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }

    candidate_contents = {
        "analysis.md": ANALYSIS_CONTENT,
        "figure_plan.json": b'{"figures":[]}\n',
        "results_table.tex": b"table\n",
        "experiment_summary.json": summary_content,
    }
    candidate_refs = [
        {"role": role, **ref(path, candidate_contents[path])}
        for role, path in (
            ("analysis", "analysis.md"),
            ("figure_plan", "figure_plan.json"),
            ("results_table", "results_table.tex"),
            ("summary", "experiment_summary.json"),
        )
    ]
    contract_ref = ref("stage-09/experiment_contract.yaml", CONTRACT_BYTES)
    policy_ref = ref(EXECUTION_POLICY_PATH, EXECUTION_POLICY_BYTES)
    config_ref = ref("config.snapshot.yaml", CONFIG_BYTES)
    stage12_ref = ref("stage-12/experiment_result_set.json", b"baseline\n")
    execution_ref = ref(SELECTED_EXECUTION_PATH, SELECTED_EXECUTION_CONTENT)
    other_refs = {
        "sealed_candidate_manifest": ref(
            "stage-10/selected_candidate_manifest.json", b"seal\n"
        ),
        "capture_manifest": ref(
            "stage-10/evaluator-capture-v1/capture-manifest.json", b"capture\n"
        ),
        "package_manifest": ref(
            "stage-09/domain_evaluator_package_manifest.json", b"package\n"
        ),
    }
    primary_metric = {
        "condition": "trojnet_community_graphsage",
        "key": "auprc",
        "observation_set": "exact_18_variants_per_seed",
        "aggregation": "mean_variants_then_mean_seeds_v1",
        "value": Decimal("1.25"),
    }
    common = {
        "config_semantic_policy_version": 1,
        "config_semantic_sha256": "e" * 64,
        "claim_scope": contract["claim_scope"],
        "dataset_origin": contract["dataset_origin"],
        "dataset_name": contract["dataset_name"],
        "evaluator_schema": contract["evaluator_authority"]["evaluator_schema"],
        "metric_authority": contract["metric_authority"],
    }
    selected_result = {
        "schema_version": 2,
        "refinement_policy_version": 2,
        "result_set_type": "stage13_refinement",
        "baseline_manifest": stage12_ref,
        "experiment_contract": contract_ref,
        "sealed_candidate_manifest": other_refs["sealed_candidate_manifest"],
        "capture_manifest": other_refs["capture_manifest"],
        "execution_policy": policy_ref,
        "observations": execution_ref,
        "run_config": config_ref,
        **common,
        "refinement_mode": "fixed_evaluator_no_refine",
        "primary_metric": primary_metric,
        "iterations": [],
        "selected_result": {"type": "baseline", "iteration_id": None},
    }
    selected_result_bytes = canonical_authority_json_text(selected_result).encode("utf-8")
    selected_result_ref = ref(SELECTED_RESULT_PATH, selected_result_bytes)
    bindings = {
        "experiment_contract": contract_ref,
        **other_refs,
        "execution_policy": policy_ref,
        "stage12_result_set": stage12_ref,
        "stage13_refinement": selected_result_ref,
        "run_config": config_ref,
        **common,
    }
    observation_authority = {
        "invocation_journal": ref(
            "stage-12/execution_invocation_journal.jsonl", b"journal\n"
        ),
        "score_evidence_1": ref(
            "stage-12/evidence-v2/invocation-1/score_evidence.jsonl", b"score1\n"
        ),
        "score_evidence_2": ref(
            "stage-12/evidence-v2/invocation-2/score_evidence.jsonl", b"score2\n"
        ),
        "observations": execution_ref,
        "results": ref("stage-12/evidence-v2/results.json", b"results\n"),
        "observation_policy_version": 2,
    }
    candidate_identity = {
        "schema_version": 2,
        "candidate_policy_version": 2,
        "bindings": bindings,
        "selected_result": {"type": "baseline", "iteration_id": None},
        "observation_authority": observation_authority,
        "primary_metric": primary_metric,
        "artifacts": candidate_refs,
    }
    candidate_id = "cand-" + hashlib.sha256(
        canonical_authority_json_text(candidate_identity).encode("utf-8")
    ).hexdigest()
    candidate = {"candidate_id": candidate_id, **candidate_identity}
    candidate_bytes = canonical_authority_json_text(candidate).encode("utf-8")
    candidate_manifest_path = (
        f"stage-14/evidence_candidates/{candidate_id}/"
        "experiment_evidence_candidate.json"
    )
    candidate_ref = ref(candidate_manifest_path, candidate_bytes)
    candidate_root = candidate_manifest_path.rsplit("/", 1)[0]
    manifest = {
        "schema_version": 2,
        "selection_policy_version": 2,
        "generation_kind": "domain_evaluator",
        "bindings": bindings,
        "selected_result": {"type": "baseline", "iteration_id": None},
        "observation_authority": observation_authority,
        "primary_metric": primary_metric,
        "selected_candidate": {
            "candidate_id": candidate_id,
            "manifest": candidate_ref,
        },
        "selected_summary": {
            "source": ref(
                f"{candidate_root}/experiment_summary.json", summary_content
            ),
            "canonical_copy": ref("experiment_summary_best.json", summary_content),
        },
        "selected_analysis": {
            "source": ref(f"{candidate_root}/analysis.md", ANALYSIS_CONTENT),
            "canonical_copy": ref("analysis_best.md", ANALYSIS_CONTENT),
        },
    }
    manifest_sha256 = hashlib.sha256(
        canonical_authority_json_text(manifest).encode("utf-8")
    ).hexdigest()
    selected_execution = CanonicalEvidenceArtifact(
        role="observations",
        path=SELECTED_EXECUTION_PATH,
        sha256=execution_ref["sha256"],
        content=SELECTED_EXECUTION_CONTENT,
    )
    candidate_artifacts = tuple(
        CanonicalEvidenceArtifact(
            role=item["role"],
            path=item["path"],
            sha256=item["sha256"],
            content=candidate_contents[item["path"]],
        )
        for item in candidate_refs
    )
    execution_policy = CanonicalEvidenceArtifact(
        role="execution_policy",
        path=EXECUTION_POLICY_PATH,
        sha256=policy_ref["sha256"],
        content=EXECUTION_POLICY_BYTES,
    )
    fields: dict[str, Any] = {
        "manifest_path": "canonical_experiment_evidence.json",
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "candidate_manifest_path": candidate_manifest_path,
        "candidate_manifest_sha256": candidate_ref["sha256"],
        "candidate": candidate,
        "selected_result_manifest_path": SELECTED_RESULT_PATH,
        "selected_result_manifest_sha256": selected_result_ref["sha256"],
        "selected_result": selected_result,
        "selected_execution_artifact": selected_execution,
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": contract_ref["sha256"],
        "experiment_contract_bytes": CONTRACT_BYTES,
        "run_config_path": "config.snapshot.yaml",
        "run_config_sha256": config_ref["sha256"],
        "run_config_bytes": CONFIG_BYTES,
        "summary_bytes": summary_content,
        "summary": {},
        "analysis_bytes": ANALYSIS_CONTENT,
        "analysis_text": ANALYSIS_CONTENT.decode("utf-8"),
        "metric_observations": {},
        "structured_results": {},
        "artifacts": candidate_artifacts,
        "project_artifacts": (),
        "execution_policy_artifact": execution_policy,
    }
    fields.update(overrides)
    return CanonicalExperimentEvidence(**fields)


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch):
    from researchclaw.pipeline import scientific_claim_authority as authority

    cfs = {"schema_version": 1, "value": Decimal("1.25")}
    monkeypatch.setattr(authority, "build_canonical_fact_sheet", lambda _evidence: cfs)
    built = build_scientific_claim_generation_binding(_evidence())
    expected_payload = {
        "binding_policy_version": 1,
        "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
        "canonical_experiment_evidence_sha256": built.evidence.manifest_sha256,
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": SHA_B,
        "run_config_path": "config.snapshot.yaml",
        "run_config_sha256": SHA_C,
        "cfs_schema_version": 1,
        "cfs_sha256": hashlib.sha256(
            canonical_authority_json_text(cfs).encode("utf-8")
        ).hexdigest(),
        "claim_policy_id": CLAIM_POLICY_ID,
    }
    assert built.generation_binding_sha256 == hashlib.sha256(
        canonical_authority_json_text(expected_payload).encode("utf-8")
    ).hexdigest()
    return built


@pytest.fixture
def source(binding):
    candidate_root = binding.evidence.candidate_manifest_path.rsplit("/", 1)[0]
    return bind_scientific_claim_source(
        binding, f"{candidate_root}/experiment_summary.json"
    )


def _fact_payload(binding, source, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "fact_id": "0" * 64,
        "fact_kind": "metric_value",
        "subject_id": "condition:primary",
        "predicate_id": "metric:auprc",
        "object_kind": "decimal",
        "object_value": "1.25",
        "unit_id": "NONE",
        "source_path": source.path,
        "source_sha256": source.sha256,
        "source_json_pointer": "/value",
        "cfs_schema_version": 1,
        "cfs_sha256": binding.cfs_sha256,
        "generation_binding_sha256": binding.generation_binding_sha256,
    }
    payload.update(overrides)
    payload["fact_id"] = evidence_fact_id(payload)
    return payload


def _claim_payload(binding, source, **overrides: Any) -> dict[str, Any]:
    sentence = "The primary AUCPR was 1.25."
    payload: dict[str, Any] = {
        "schema_version": 1,
        "claim_id": "0" * 64,
        "claim_kind": "primary_metric_result",
        "section_id": "results",
        "evidence_fact_ids": [FACT_A, FACT_B],
        "renderer_template_id": "result.primary_metric.v1",
        "renderer_slot_fact_ids": [FACT_B, FACT_A],
        "rendered_sentence": sentence,
        "rendered_sentence_sha256": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
        "mandatory": True,
        "source_path": source.path,
        "source_sha256": source.sha256,
        "cfs_schema_version": 1,
        "cfs_sha256": binding.cfs_sha256,
        "generation_binding_sha256": binding.generation_binding_sha256,
    }
    payload.update(overrides)
    payload["claim_id"] = scientific_claim_id(payload)
    return payload


def _bytes(payload: dict[str, Any]) -> bytes:
    return canonical_authority_json_text(payload).encode("utf-8")


def _replace_raw(content: bytes, old: bytes, new: bytes) -> bytes:
    assert old in content
    return content.replace(old, new, 1)


def test_evidence_fact_and_claim_canonical_round_trip(binding, source) -> None:
    fact_payload = _fact_payload(binding, source)
    fact = parse_evidence_fact(_bytes(fact_payload), binding=binding, source=source)
    assert isinstance(fact, EvidenceFact)
    assert fact.to_dict() == fact_payload
    assert fact.canonical_bytes() == _bytes(fact_payload)

    claim_payload = _claim_payload(binding, source)
    claim = parse_scientific_claim_record(
        _bytes(claim_payload), binding=binding, source=source
    )
    assert isinstance(claim, ScientificClaimRecord)
    assert claim.to_dict() == claim_payload
    assert claim.canonical_bytes() == _bytes(claim_payload)


@pytest.mark.parametrize("kind", ["fact", "claim"])
@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "missing"])
def test_record_exact_schema_rejects_duplicate_unknown_and_missing(
    binding, source, kind: str, mutation: str
) -> None:
    payload = (
        _fact_payload(binding, source)
        if kind == "fact"
        else _claim_payload(binding, source)
    )
    parser = parse_evidence_fact if kind == "fact" else parse_scientific_claim_record
    kwargs = {"binding": binding, "source": source}
    if mutation == "unknown":
        payload["extra"] = "forbidden"
        content = _bytes(payload)
    elif mutation == "missing":
        payload.pop("source_sha256")
        content = _bytes(payload)
    else:
        content = _replace_raw(
            _bytes(payload),
            b'{"',
            b'{"schema_version":1,',
        )
    with pytest.raises(ScientificClaimAuthorityError):
        parser(content, **kwargs)


@pytest.mark.parametrize(
    "field,raw",
    [
        ("schema_version", b"true"),
        ("schema_version", b"1.0"),
        ("schema_version", b'"1"'),
        ("schema_version", b"null"),
        ("cfs_schema_version", b"false"),
        ("cfs_schema_version", b"1.0"),
        ("cfs_schema_version", b'"1"'),
        ("cfs_schema_version", b"null"),
    ],
)
@pytest.mark.parametrize("kind", ["fact", "claim"])
def test_versions_require_true_integer_one(
    binding, source, field: str, raw: bytes, kind: str
) -> None:
    payload = (
        _fact_payload(binding, source)
        if kind == "fact"
        else _claim_payload(binding, source)
    )
    content = _replace_raw(_bytes(payload), f'"{field}":1'.encode(), b'"' + field.encode() + b'":' + raw)
    with pytest.raises(ScientificClaimAuthorityError):
        if kind == "fact":
            parse_evidence_fact(content, binding=binding, source=source)
        else:
            parse_scientific_claim_record(content, binding=binding, source=source)


def test_identity_excludes_only_its_own_id(binding, source) -> None:
    fact = _fact_payload(binding, source)
    fact_identity = {key: value for key, value in fact.items() if key != "fact_id"}
    assert fact["fact_id"] == hashlib.sha256(_bytes(fact_identity)).hexdigest()
    fact["fact_id"] = SHA_C
    assert evidence_fact_id(fact) == hashlib.sha256(_bytes(fact_identity)).hexdigest()

    claim = _claim_payload(binding, source)
    claim_identity = {key: value for key, value in claim.items() if key != "claim_id"}
    assert claim["claim_id"] == hashlib.sha256(_bytes(claim_identity)).hexdigest()
    claim["claim_id"] = SHA_C
    assert scientific_claim_id(claim) == hashlib.sha256(_bytes(claim_identity)).hexdigest()


@pytest.mark.parametrize("kind", ["fact", "claim"])
def test_stored_self_including_id_cannot_be_an_identity_oracle(
    binding, source, kind: str
) -> None:
    payload = (
        _fact_payload(binding, source)
        if kind == "fact"
        else _claim_payload(binding, source)
    )
    id_field = "fact_id" if kind == "fact" else "claim_id"
    parser = parse_evidence_fact if kind == "fact" else parse_scientific_claim_record
    payload[id_field] = hashlib.sha256(_bytes(payload)).hexdigest()
    with pytest.raises(ScientificClaimAuthorityError, match="identity"):
        parser(_bytes(payload), binding=binding, source=source)


@pytest.mark.parametrize(
    "path",
    [
        "../observations.json",
        "/stage-12/observations.json",
        "stage-12\\observations.json",
        "stage-12//observations.json",
        "stage-12/%2e%2e/observations.json",
        "stage-12/e\u0301.json",
        " stage-12/observations.json",
        "stage-12/control\u0001.json",
    ],
)
def test_source_path_must_be_safe_and_canonical(binding, path: str) -> None:
    with pytest.raises(ScientificClaimAuthorityError):
        bind_scientific_claim_source(binding, path)


@pytest.mark.parametrize("field", ["source_sha256", "cfs_sha256", "generation_binding_sha256"])
def test_hashes_are_exact_lowercase_sha256(binding, source, field: str) -> None:
    payload = _fact_payload(binding, source, **{field: "A" * 64})
    with pytest.raises(ScientificClaimAuthorityError):
        parse_evidence_fact(_bytes(payload), binding=binding, source=source)


@pytest.mark.parametrize("pointer", ["value", "/missing", "/bad~2escape", "/items/01"])
def test_json_pointer_is_strict_and_must_resolve(binding, source, pointer: str) -> None:
    payload = _fact_payload(binding, source, source_json_pointer=pointer)
    with pytest.raises(ScientificClaimAuthorityError, match="pointer"):
        parse_evidence_fact(_bytes(payload), binding=binding, source=source)


@pytest.mark.parametrize(
    "object_kind,object_value,pointer",
    [
        ("decimal", "-0", "/value"),
        ("decimal", "1e0", "/value"),
        ("decimal", "1.250", "/value"),
        ("decimal", 1.25, "/value"),
        ("decimal", True, "/value"),
        ("decimal", None, "/value"),
        ("boolean", "false", "/flag"),
        ("boolean", True, "/flag"),
        ("string", "other", "/label"),
    ],
)
def test_fact_object_value_type_and_canonical_value_are_closed(
    binding, source, object_kind: str, object_value: Any, pointer: str
) -> None:
    if isinstance(object_value, float):
        content = _replace_raw(
            _bytes(_fact_payload(binding, source)),
            b'"object_value":"1.25"',
            b'"object_value":1.25',
        )
        with pytest.raises(ScientificClaimAuthorityError):
            parse_evidence_fact(content, binding=binding, source=source)
        return
    payload = _fact_payload(
        binding,
        source,
        object_kind=object_kind,
        object_value=object_value,
        source_json_pointer=pointer,
    )
    with pytest.raises(ScientificClaimAuthorityError):
        parse_evidence_fact(_bytes(payload), binding=binding, source=source)


@pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"-Infinity", b"-0", b"1e0", b"1.00"])
def test_external_numeric_aliases_and_nonfinite_tokens_are_rejected(
    binding, source, raw: bytes
) -> None:
    content = _replace_raw(_bytes(_fact_payload(binding, source)), b'"schema_version":1', b'"schema_version":' + raw)
    with pytest.raises(ScientificClaimAuthorityError):
        parse_evidence_fact(content, binding=binding, source=source)


@pytest.mark.parametrize(
    "ids",
    [
        [],
        [FACT_B, FACT_A],
        [FACT_A, FACT_A],
    ],
)
def test_claim_evidence_ids_are_nonempty_sorted_and_unique(binding, source, ids) -> None:
    payload = _claim_payload(binding, source, evidence_fact_ids=ids)
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_record(
            _bytes(payload), binding=binding, source=source
        )


@pytest.mark.parametrize(
    "slots",
    [
        [],
        [FACT_A, FACT_A],
        [FACT_A, "3" * 64],
    ],
)
def test_claim_slots_are_nonempty_unique_and_members_of_evidence(
    binding, source, slots
) -> None:
    payload = _claim_payload(binding, source, renderer_slot_fact_ids=slots)
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_record(
            _bytes(payload), binding=binding, source=source
        )


def test_claim_slot_order_is_identity_bound_without_claiming_renderer_replay(
    binding, source
) -> None:
    payload = _claim_payload(binding, source)
    payload["renderer_slot_fact_ids"] = list(reversed(payload["renderer_slot_fact_ids"]))
    with pytest.raises(ScientificClaimAuthorityError, match="identity"):
        parse_scientific_claim_record(
            _bytes(payload), binding=binding, source=source
        )


def test_rendered_sentence_and_full_sha256_cannot_diverge(binding, source) -> None:
    payload = _claim_payload(binding, source)
    payload["rendered_sentence"] += " "
    payload["claim_id"] = scientific_claim_id(payload)
    with pytest.raises(ScientificClaimAuthorityError, match="sentence"):
        parse_scientific_claim_record(
            _bytes(payload), binding=binding, source=source
        )


@pytest.mark.parametrize("mandatory", [0, 1, "true", None])
def test_claim_mandatory_requires_json_boolean(binding, source, mandatory) -> None:
    payload = _claim_payload(binding, source, mandatory=mandatory)
    with pytest.raises(ScientificClaimAuthorityError, match="mandatory"):
        parse_scientific_claim_record(
            _bytes(payload), binding=binding, source=source
        )


@pytest.mark.parametrize("kind", ["fact", "claim"])
@pytest.mark.parametrize("field", ["cfs_sha256", "generation_binding_sha256"])
def test_record_cfs_and_generation_must_match_trusted_rebuild(
    binding, source, kind: str, field: str
) -> None:
    payload = (
        _fact_payload(binding, source, **{field: SHA_C})
        if kind == "fact"
        else _claim_payload(binding, source, **{field: SHA_C})
    )
    parser = parse_evidence_fact if kind == "fact" else parse_scientific_claim_record
    with pytest.raises(ScientificClaimAuthorityError, match="binding"):
        parser(_bytes(payload), binding=binding, source=source)


@pytest.mark.parametrize("kind", ["fact", "claim"])
def test_source_path_and_hash_must_match_trusted_source(
    binding, source, kind: str
) -> None:
    other = replace(source, path="stage-12/other.json")
    payload = (
        _fact_payload(binding, source)
        if kind == "fact"
        else _claim_payload(binding, source)
    )
    parser = parse_evidence_fact if kind == "fact" else parse_scientific_claim_record
    with pytest.raises(ScientificClaimAuthorityError, match="source binding"):
        parser(_bytes(payload), binding=binding, source=other)


def test_generation_binding_rebuild_has_no_caller_hash_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import scientific_claim_authority as authority

    cfs = {"schema_version": 1, "value": Decimal("2.00")}
    monkeypatch.setattr(authority, "build_canonical_fact_sheet", lambda _evidence: cfs)
    first = build_scientific_claim_generation_binding(_evidence())
    second = build_scientific_claim_generation_binding(
        _evidence(
            _summary_content=canonical_authority_json_text(
                {"flag": True, "label": "beta", "value": Decimal("1.25")}
            ).encode("utf-8")
        )
    )
    assert first.cfs_sha256 == second.cfs_sha256
    assert first.generation_binding_sha256 != second.generation_binding_sha256


def test_untrusted_caller_binding_and_source_handles_are_rejected(
    binding, source
) -> None:
    payload = _fact_payload(binding, source)
    with pytest.raises(ScientificClaimAuthorityError, match="independent rebuild"):
        parse_evidence_fact(
            _bytes(payload),
            binding=replace(binding, cfs_sha256=SHA_A),
            source=source,
        )
    with pytest.raises(ScientificClaimAuthorityError, match="source binding"):
        parse_evidence_fact(
            _bytes(payload),
            binding=binding,
            source=replace(source, content=b"forged\n"),
        )


def test_generation_binding_rejects_non_v1_or_missing_cfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import scientific_claim_authority as authority

    for cfs in (None, {"schema_version": True}, {"schema_version": 2}):
        monkeypatch.setattr(authority, "build_canonical_fact_sheet", lambda _e, value=cfs: value)
        with pytest.raises(ScientificClaimAuthorityError):
            build_scientific_claim_generation_binding(_evidence())


def test_generation_binding_rejects_stored_manifest_hash_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import scientific_claim_authority as authority

    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: {"schema_version": 1},
    )
    with pytest.raises(ScientificClaimAuthorityError, match="evidence bytes/hash"):
        build_scientific_claim_generation_binding(
            _evidence(manifest_sha256=SHA_A)
        )


def test_synchronized_caller_source_hash_and_fact_id_cannot_self_authorize(
    binding, source
) -> None:
    forged_content = canonical_authority_json_text(
        {"flag": True, "label": "alpha", "value": Decimal("9.5")}
    ).encode("utf-8")
    forged_source = replace(
        source,
        sha256=hashlib.sha256(forged_content).hexdigest(),
        content=forged_content,
    )
    payload = _fact_payload(binding, forged_source, object_value="9.5")
    with pytest.raises(ScientificClaimAuthorityError, match="source binding"):
        parse_evidence_fact(
            _bytes(payload), binding=binding, source=forged_source
        )


def _unbound_artifact(
    *,
    path: str = "stage-99/unbound.json",
    role: str = "poison",
    content: bytes = b'{"poison":"UNBOUND_SCIENTIFIC_FACT"}\n',
) -> CanonicalEvidenceArtifact:
    return CanonicalEvidenceArtifact(
        role=role,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def test_unbound_appended_artifact_never_enters_source_inventory(binding) -> None:
    injected = _unbound_artifact()
    mutated_evidence = replace(
        binding.evidence,
        artifacts=(*binding.evidence.artifacts, injected),
    )
    with pytest.raises(ScientificClaimAuthorityError, match="artifact closure"):
        build_scientific_claim_generation_binding(mutated_evidence)
    forged_binding = replace(binding, evidence=mutated_evidence)
    with pytest.raises(ScientificClaimAuthorityError):
        bind_scientific_claim_source(forged_binding, injected.path)


def test_synchronized_unbound_artifact_source_hash_and_fact_id_still_reject(
    binding,
) -> None:
    injected = _unbound_artifact()
    mutated_evidence = replace(
        binding.evidence,
        artifacts=(*binding.evidence.artifacts, injected),
    )
    forged_binding = replace(binding, evidence=mutated_evidence)
    with pytest.raises(ScientificClaimAuthorityError):
        source = bind_scientific_claim_source(forged_binding, injected.path)
        payload = _fact_payload(
            forged_binding,
            source,
            object_kind="string",
            object_value="UNBOUND_SCIENTIFIC_FACT",
            source_json_pointer="/poison",
        )
        parse_evidence_fact(
            _bytes(payload), binding=forged_binding, source=source
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "duplicate",
        "role",
        "path",
        "hash",
    ],
)
def test_candidate_manifest_and_accessor_artifacts_require_exact_closure(
    binding, mutation: str
) -> None:
    artifacts = list(binding.evidence.artifacts)
    if mutation == "extra":
        artifacts.append(_unbound_artifact(path="extra.json"))
    elif mutation == "missing":
        artifacts.pop()
    elif mutation == "duplicate":
        artifacts.append(artifacts[-1])
    elif mutation == "role":
        artifacts[0] = replace(artifacts[0], role="summary")
    elif mutation == "path":
        artifacts[0] = replace(artifacts[0], path="other.md")
    else:
        artifacts[0] = replace(artifacts[0], sha256=SHA_A)
    with pytest.raises(ScientificClaimAuthorityError, match="artifact closure"):
        build_scientific_claim_generation_binding(
            replace(binding.evidence, artifacts=tuple(artifacts))
        )


@pytest.mark.parametrize("field", ["selected_execution_artifact", "execution_policy_artifact"])
def test_stored_ref_rejects_replaced_selected_execution_or_policy(
    binding, field: str
) -> None:
    replacement = _unbound_artifact(
        path=(
            "stage-12/evidence-v2/other-observations.json"
            if field == "selected_execution_artifact"
            else "stage-09/other-policy.json"
        ),
        role=("observations" if field == "selected_execution_artifact" else "execution_policy"),
    )
    with pytest.raises(ScientificClaimAuthorityError, match="binding"):
        build_scientific_claim_generation_binding(
            replace(binding.evidence, **{field: replacement})
        )


def test_candidate_source_uses_full_run_relative_path_only(binding) -> None:
    candidate_root = binding.evidence.candidate_manifest_path.rsplit("/", 1)[0]
    full_path = f"{candidate_root}/analysis.md"
    source = bind_scientific_claim_source(binding, full_path)
    assert source.path == full_path
    with pytest.raises(ScientificClaimAuthorityError):
        bind_scientific_claim_source(binding, "analysis.md")


def test_exact_non_nfc_source_string_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import scientific_claim_authority as authority

    exact_value = "e\u0301"
    content = canonical_authority_json_text({"label": exact_value}).encode("utf-8")
    monkeypatch.setattr(
        authority,
        "build_canonical_fact_sheet",
        lambda _evidence: {"schema_version": 1},
    )
    binding = build_scientific_claim_generation_binding(
        _evidence(_summary_content=content)
    )
    candidate_root = binding.evidence.candidate_manifest_path.rsplit("/", 1)[0]
    source = bind_scientific_claim_source(
        binding, f"{candidate_root}/experiment_summary.json"
    )
    payload = _fact_payload(
        binding,
        source,
        object_kind="string",
        object_value=exact_value,
        source_json_pointer="/label",
    )
    record = parse_evidence_fact(
        _bytes(payload), binding=binding, source=source
    )
    assert record.object_value == exact_value
    assert record.canonical_bytes() == _bytes(payload)


def _selection(
    selected: list[str], ordered: list[str] | None = None, connectors: list[Any] | None = None
) -> bytes:
    ordered_value = list(selected) if ordered is None else ordered
    connector_value = ["NONE"] * max(len(ordered_value) - 1, 0) if connectors is None else connectors
    return _bytes(
        {
            "selected_claim_ids": selected,
            "ordered_claim_ids": ordered_value,
            "connector_template_ids": connector_value,
        }
    )


def test_selection_exact_schema_and_canonical_round_trip() -> None:
    content = _selection([SHA_A, SHA_B], [SHA_B, SHA_A], ["NONE"])
    selection = parse_scientific_claim_selection(content)
    assert selection.selected_claim_ids == (SHA_A, SHA_B)
    assert selection.ordered_claim_ids == (SHA_B, SHA_A)
    assert selection.connector_template_ids == ("NONE",)
    assert selection.canonical_bytes() == content
    assert parse_scientific_claim_selection(_selection([], [], [])).ordered_claim_ids == ()


@pytest.mark.parametrize(
    "content",
    [
        b'{"selection":{"selected_claim_ids":[],"ordered_claim_ids":[],"connector_template_ids":[]}}\n',
        b"```json\n{}\n```\n",
        b'{"selected_claim_ids":[],"ordered_claim_ids":[],"connector_template_ids":[],"schema_version":1}\n',
        b'{"selected_claim_ids":[],"ordered_claim_ids":[],"connector_template_ids":[],"explanation":"x"}\n',
        b'{"selected_claim_ids":[],"selected_claim_ids":[],"ordered_claim_ids":[],"connector_template_ids":[]}\n',
        b'{"ordered_claim_ids":[],"connector_template_ids":[]}\n',
        b'{"selected_claim_ids":[],"connector_template_ids":[]}\n',
        b'{"selected_claim_ids":[],"ordered_claim_ids":[]}\n',
    ],
)
def test_selection_rejects_wrapper_markdown_extra_and_duplicate_key(content: bytes) -> None:
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_selection(content)


@pytest.mark.parametrize(
    "selected,ordered",
    [
        ([SHA_A, SHA_B], [SHA_A]),
        ([SHA_A, SHA_B], [SHA_A, SHA_A]),
        ([SHA_A], [SHA_A, SHA_B]),
        ([SHA_A, SHA_A], [SHA_A, SHA_A]),
    ],
)
def test_selection_ordered_is_exact_duplicate_free_permutation(selected, ordered) -> None:
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_selection(_selection(selected, ordered, []))


@pytest.mark.parametrize(
    "connectors",
    [
        [],
        ["NONE", "NONE"],
        ["AND"],
        [{"template_id": "NONE"}],
        ["NONE(x)"],
        ["because"],
    ],
)
def test_selection_connector_count_and_closed_none_channel(connectors) -> None:
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_selection(_selection([SHA_A, SHA_B], connectors=connectors))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.replace(b",", b", ", 1),
        lambda b: b.replace(b'{"', b'{\n"', 1),
        lambda b: b[:-1],
        lambda b: b + b"\n",
        lambda b: b.replace(b'"connector_template_ids"', b'"ordered_claim_ids"', 1),
        lambda b: b.replace(SHA_A.encode(), b"\\ud800", 1),
    ],
)
def test_semantically_similar_noncanonical_json_bytes_are_rejected(mutate) -> None:
    content = _selection([SHA_A])
    with pytest.raises(ScientificClaimAuthorityError):
        parse_scientific_claim_selection(mutate(content))


def test_generic_v1_capability_map_is_unchanged() -> None:
    assert REQUIRED_CAPABILITIES == (
        "stage10_sealed_input",
        "stage12_result_set",
        "stage13_refinement_set",
        "stage14_candidate_and_promotion",
        "metric_contract_authority",
        "domain_evaluator_authority",
        "shared_accessor",
        "stage15_17_consumers",
        "stage19_22_consumers",
        "stage24_release_consumers",
        "external_and_persistent_consumers",
        "independent_release_reconstruction",
    )
    assert CANONICAL_EVIDENCE_CAPABILITIES == {
        name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES
    }
