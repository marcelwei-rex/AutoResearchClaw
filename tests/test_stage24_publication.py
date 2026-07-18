from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.config import RCConfig
from researchclaw.pipeline.stage15_critique import Stage15CritiquePublication
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage23_verification import Stage23PublicationSnapshot
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundle,
    Stage24ModelProjection,
)
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationError,
    _citation_inputs,
    _comparison_status,
    _load_stage24_truth_publication_from_namespace,
    _load_stage24_truth_publication,
    _numeric_support,
    _publish_after_invalidation,
    _publish_stage24_truth,
    _transform_value,
    execute_stage24_truth,
)
from researchclaw.pipeline import executor
from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.stages import Stage
from researchclaw.pipeline.stage24_obligations import (
    build_claim_obligation_inventory,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CAPABILITY_SCHEMA_VERSION,
    CanonicalEvidenceMigrationIncomplete,
)


@pytest.fixture(autouse=True)
def _enable_complete_capability_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: CAPABILITY_SCHEMA_VERSION
            for name in CANONICAL_EVIDENCE_CAPABILITIES
        },
    )


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


def _execution_artifact(
    observations: dict[str, tuple[str, ...]],
) -> BoundArtifact:
    payload = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_observations": {
            metric: [float(value) for value in values]
            for metric, values in observations.items()
        },
        "structured_results": {},
    }
    return _bound(
        "stage-12/evidence-v1/run-1.json",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def _bundle() -> Stage24InputBundle:
    paper = _bound(
        "stage-23/paper_final_verified.md",
        b"## Introduction\n\nBackground [smith2024].\n",
    )
    abstract = "The retained source supports background."
    excerpt_hash = hashlib.sha256(abstract.encode()).hexdigest()
    candidate_value = {
        "source_identity": "source:smith2024",
        "abstract": abstract,
    }
    candidates = _bound(
        "stage-04/candidates.jsonl",
        (json.dumps(candidate_value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    card_value = {
        "card_id": "card-001",
        "source_identity": "source:smith2024",
        "cite_key": "smith2024",
        "evidence_excerpts": [
            {
                "excerpt_id": "excerpt-001",
                "excerpt_sha256": excerpt_hash,
                "source_type": "abstract",
                "source_artifact_path": candidates.path,
                "source_artifact_sha256": candidates.sha256,
                "source_record_id": "source:smith2024",
                "json_pointer": "/abstract",
                "char_start": 0,
                "char_end": len(abstract),
                "excerpt_text": abstract,
            }
        ],
    }
    card = _bound(
        "stage-06/cards/card-001.json",
        (json.dumps(card_value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    canonical = _bound("canonical_experiment_evidence.json", b"{}\n")
    contract = _bound("stage-09/experiment_contract.yaml", b"schema_version: 2\n")
    critique = _bound("stage-15/critique.json", b"{}\n")
    critique_manifest = _bound("stage-15/stage15_critique_manifest.json", b"{}\n")
    stage23_manifest = _bound("stage-23/stage23_verification_manifest.json", b"{}\n")
    default_observations = {"detection_f1": ("0.5",)}
    selected_execution = _execution_artifact(default_observations)
    evidence = SimpleNamespace(
        metric_observations=MappingProxyType(default_observations),
        selected_result_manifest_path="stage-12/evidence-v1/result_set_manifest.json",
        selected_result_manifest_sha256="b" * 64,
        selected_execution_artifact=selected_execution,
    )
    stage23_inputs = SimpleNamespace(
        stage22_inputs=SimpleNamespace(
            evidence=evidence,
            stage19_inputs=SimpleNamespace(candidates=candidates),
        )
    )
    publication = Stage23PublicationSnapshot(
        manifest=stage23_manifest,
        outputs=(paper,),
    )
    critique_publication = Stage15CritiquePublication(
        state="model_final",
        critique=MappingProxyType({"state": "model_final", "findings": ()}),
        manifest=MappingProxyType({"state": "model_final"}),
        artifacts=(critique.path, critique_manifest.path),
    )
    return Stage24InputBundle(
        stage23_inputs=stage23_inputs,  # type: ignore[arg-type]
        stage23_publication=publication,
        critique_publication=critique_publication,
        critique=critique,
        critique_manifest=critique_manifest,
        canonical_manifest=canonical,
        experiment_contract=contract,
        paper=paper,
        verification_report=_bound("stage-23/verification_report.json", b"{}\n"),
        citation_plan=MappingProxyType(
            {
                "claims": (
                    MappingProxyType(
                        {
                            "planned_citations": (
                                MappingProxyType(
                                    {
                                        "cite_key": "smith2024",
                                        "evidence_excerpt_ids": ("excerpt-001",),
                                    }
                                ),
                            )
                        }
                    ),
                )
            }
        ),
        effective_policy=MappingProxyType({}),
        evidence_cards=(MappingProxyType(card_value),),
        verification=MappingProxyType(
            {
                "results": (
                    MappingProxyType(
                        {"cite_key": "smith2024", "status": "verified"}
                    ),
                )
            }
        ),
        dataset_origin="synthetic",
        model_projection=Stage24ModelProjection(
            writer_model="writer",
            citation_assessment_model="citation-critic",
            generic_support_model="generic-critic",
            resolution_assessment_model="resolution-critic",
        ),
        entries=(SimpleNamespace(artifact=card),),  # type: ignore[arg-type]
        identity_sha256="c" * 64,
    )


def _numeric_bundle(
    paper_bytes: bytes,
    observations: dict[str, tuple[str, ...]],
) -> Stage24InputBundle:
    bundle = _bundle()
    paper = _bound("stage-23/paper_final_verified.md", paper_bytes)
    evidence = SimpleNamespace(
        metric_observations=MappingProxyType(observations),
        selected_result_manifest_path="stage-12/experiment_result_set.json",
        selected_result_manifest_sha256="b" * 64,
        selected_execution_artifact=_execution_artifact(observations),
    )
    return replace(
        bundle,
        paper=paper,
        stage23_publication=replace(bundle.stage23_publication, outputs=(paper,)),
        stage23_inputs=SimpleNamespace(
            stage22_inputs=SimpleNamespace(
                evidence=evidence,
                stage19_inputs=bundle.stage23_inputs.stage22_inputs.stage19_inputs,
            )
        ),  # type: ignore[arg-type]
        citation_plan=MappingProxyType({"claims": ()}),
        evidence_cards=(),
        verification=MappingProxyType({"results": ()}),
        entries=(),
    )


def _domain_numeric_bundle(
    paper_bytes: bytes,
) -> Stage24InputBundle:
    bundle = _bundle()
    paper = _bound("stage-23/paper_final_verified.md", paper_bytes)
    rows = [
        {
            "circuit_family": f"c{variant:03d}",
            "circuit_variant": f"c{variant:03d}_ht1",
            "condition": condition,
            "metrics": {"auprc": 0.5},
            "n_total": 10,
            "n_trojan": 1,
            "seed": seed,
        }
        for condition in (
            "raw_cc1",
            "scoap_isolation_forest",
            "trojnet_community_graphsage",
        )
        for seed in (0, 1, 2)
        for variant in range(1, 19)
    ]
    rows[0]["metrics"]["auprc"] = 0.75
    observations_payload = {
        "schema_version": 2,
        "observation_policy_version": 1,
        "dataset_capture_sha256": "a" * 64,
        "score_evidence_sha256": "b" * 64,
        "metric_keys": ["auprc"],
        "observations": rows,
        "per_seed": [],
        "aggregate": [],
        "primary_metric": {
            "aggregation": "mean_variants_then_mean_seeds_v1",
            "condition": "trojnet_community_graphsage",
            "key": "auprc",
            "observation_set": "exact_18_variants_per_seed",
            "value": 0.75,
        },
    }
    content = (
        json.dumps(
            observations_payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    evidence = SimpleNamespace(
        manifest=MappingProxyType(
            {"schema_version": 2, "generation_kind": "domain_evaluator"}
        ),
        metric_observations=MappingProxyType(
            {"auprc": (Decimal("0.75"),) + (Decimal("0.5"),) * 161}
        ),
        selected_result_manifest_path="stage-13/refinement_result_set.json",
        selected_result_manifest_sha256="c" * 64,
        selected_execution_artifact=_bound(
            "stage-12/evidence-v2/observations.json", content
        ),
    )
    return replace(
        bundle,
        paper=paper,
        stage23_publication=replace(bundle.stage23_publication, outputs=(paper,)),
        stage23_inputs=SimpleNamespace(
            stage22_inputs=SimpleNamespace(
                evidence=evidence,
                stage19_inputs=bundle.stage23_inputs.stage22_inputs.stage19_inputs,
            )
        ),  # type: ignore[arg-type]
        citation_plan=MappingProxyType({"claims": ()}),
        evidence_cards=(),
        verification=MappingProxyType({"results": ()}),
        entries=(),
    )
class _SchemaLLM:
    calls = 0

    def chat(self, messages, **_kwargs):
        self.calls += 1
        request = json.loads(messages[0]["content"])
        response = dict(request["response_schema"])
        if "verdict" in response:
            response["verdict"] = "supported"
            response["reason"] = "The retained excerpt directly supports the claim."
        else:
            response["resolution"] = "fixed"
            response["note"] = "The paper addresses the finding."
        return SimpleNamespace(content=json.dumps(response))


def test_stage24_public_entrypoints_guard_before_namespace_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {**CANONICAL_EVIDENCE_CAPABILITIES, "independent_release_reconstruction": 0},
    )
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    stale = stage_dir / "truth_audit.json"
    stale.write_text("stale", encoding="utf-8")
    bundle = _bundle()
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            llm=_SchemaLLM(),  # type: ignore[arg-type]
        )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _load_stage24_truth_publication(run_dir, bundle=bundle)
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        execute_stage24_truth(
            run_dir,
            stage_dir,
            runtime_config=config,
            llm=_SchemaLLM(),  # type: ignore[arg-type]
        )

    assert stale.read_text(encoding="utf-8") == "stale"


def test_stage24_internal_entrypoints_guard_before_namespace_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {**CANONICAL_EVIDENCE_CAPABILITIES, "independent_release_reconstruction": 0},
    )

    class NamespaceSpy:
        calls = 0
        run_dir = Path("/must-not-be-read")

        def __getattr__(self, _name: str):
            self.calls += 1
            raise AssertionError("namespace must not be used before capability guard")

    namespace = NamespaceSpy()
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _publish_after_invalidation(
            namespace,  # type: ignore[arg-type]
            bundle=_bundle(),
            runtime_config=config,
            llm=_SchemaLLM(),  # type: ignore[arg-type]
        )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _load_stage24_truth_publication_from_namespace(
            namespace, bundle=_bundle()  # type: ignore[arg-type]
        )

    assert namespace.calls == 0


def test_execute_stage24_guard_precedes_hitl_directory_and_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {**CANONICAL_EVIDENCE_CAPABILITIES, "independent_release_reconstruction": 0},
    )
    calls = {"hitl": 0, "producer": 0}

    class HITLSpy:
        def should_pause_before(self, _stage: int) -> bool:
            calls["hitl"] += 1
            return False

    def producer(*_args: object, **_kwargs: object):
        calls["producer"] += 1
        raise AssertionError("producer must not run before capability activation")

    monkeypatch.setitem(executor._STAGE_EXECUTORS, Stage.TRUTH_AUDIT, producer)
    run_dir = tmp_path / "run"
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        executor.execute_stage(
            Stage.TRUTH_AUDIT,
            run_dir=run_dir,
            run_id="guarded-stage24",
            config=config,
            adapters=AdapterBundle(hitl=HITLSpy()),  # type: ignore[arg-type]
        )

    assert calls == {"hitl": 0, "producer": 0}
    assert not run_dir.exists()


def test_stage24_publishes_manifest_last_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )

    snapshot = _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )

    assert snapshot.manifest.path == "stage-24/stage24_truth_manifest.json"
    assert json.loads((stage_dir / "truth_audit.json").read_text())["stage24_success"] is True
    assert _load_stage24_truth_publication(run_dir, bundle=bundle) == snapshot


def test_stage24_assessment_tamper_rejects_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )
    _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )
    record = next((stage_dir / "citation-assessments").iterdir())
    record.write_text("{}\n", encoding="utf-8")

    with pytest.raises((Stage24PublicationError, ValueError)):
        _load_stage24_truth_publication(run_dir, bundle=bundle)


def test_stage24_failed_assessment_removes_stale_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    for name in ("stage24_truth_manifest.json", "truth_audit.json"):
        (stage_dir / name).write_text("stale", encoding="utf-8")
    bundle = _bundle()
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )

    with pytest.raises(Stage24PublicationError, match="requires an isolated"):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            llm=None,
        )
    assert tuple(stage_dir.iterdir()) == ()


def test_stage24_cleanup_collision_cannot_preserve_manifest(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    (stage_dir / "citation-assessments" / "nested").mkdir(parents=True)
    manifest = stage_dir / "stage24_truth_manifest.json"
    manifest.write_text("stale", encoding="utf-8")
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )

    with pytest.raises(Stage24PublicationError, match="cleanup was incomplete"):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=_bundle(),
            runtime_config=config,
            llm=None,
        )

    assert not manifest.exists()


def test_stage24_rejects_plan_key_divergence_before_first_assessment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = replace(_bundle(), citation_plan=MappingProxyType({"claims": ()}))
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )
    llm = _SchemaLLM()

    with pytest.raises(Stage24PublicationError, match="plan key closure"):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            llm=llm,  # type: ignore[arg-type]
        )
    assert llm.calls == 0
    assert tuple(stage_dir.iterdir()) == ()


def test_stage24_generic_support_is_obligation_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    paper = _bound(
        "stage-23/paper_final_verified.md",
        b"## Results\n\nBackground support is retained [smith2024].\n",
    )
    bundle = replace(
        bundle,
        paper=paper,
        stage23_publication=replace(bundle.stage23_publication, outputs=(paper,)),
    )
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )

    snapshot = _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )

    generic = tuple(
        item for item in snapshot.assessment_files
        if "/generic-support-assessments/" in item.path
    )
    assert len(generic) == 1
    claims = json.loads((stage_dir / "claims.json").read_text())
    assert claims["counts"]["unsupported"] == 0


def test_stage24_citation_excerpt_uses_utf8_byte_span() -> None:
    bundle = _bundle()
    abstract = "µ-prefix. The retained source supports background."
    excerpt_text = "The retained source supports background."
    char_start = abstract.index(excerpt_text)
    char_end = char_start + len(excerpt_text)
    candidate = {
        "source_identity": "source:smith2024",
        "abstract": abstract,
    }
    candidates = _bound(
        "stage-04/candidates.jsonl",
        (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    card = dict(bundle.evidence_cards[0])
    card["evidence_excerpts"] = [
        {
            "excerpt_id": "excerpt-001",
            "excerpt_sha256": hashlib.sha256(excerpt_text.encode()).hexdigest(),
            "source_type": "abstract",
            "source_artifact_path": candidates.path,
            "source_artifact_sha256": candidates.sha256,
            "source_record_id": "source:smith2024",
            "json_pointer": "/abstract",
            "char_start": char_start,
            "char_end": char_end,
            "excerpt_text": excerpt_text,
        }
    ]
    card_bytes = (json.dumps(card, sort_keys=True, separators=(",", ":")) + "\n").encode()
    bundle = replace(
        bundle,
        evidence_cards=(MappingProxyType(card),),
        entries=(SimpleNamespace(artifact=_bound("stage-06/cards/card-001.json", card_bytes)),),  # type: ignore[arg-type]
        stage23_inputs=SimpleNamespace(
            stage22_inputs=SimpleNamespace(
                evidence=bundle.stage23_inputs.stage22_inputs.evidence,
                stage19_inputs=SimpleNamespace(candidates=candidates),
            )
        ),  # type: ignore[arg-type]
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)

    assessment = _citation_inputs(bundle, obligations)[0]
    evidence = assessment.evidence_records[0]

    assert evidence.byte_start == len(abstract[:char_start].encode("utf-8"))
    assert evidence.byte_end == len(abstract[:char_end].encode("utf-8"))
    assert evidence.byte_start > char_start


def test_stage24_numeric_support_uses_exact_metric_unit_and_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    paper = _bound(
        "stage-23/paper_final_verified.md",
        b"## Results\n\nF1 was 0.9.\n",
    )
    evidence = SimpleNamespace(
        metric_observations=MappingProxyType({"f1": ("0.9",)}),
        selected_result_manifest_path="stage-12/evidence-v1/result_set_manifest.json",
        selected_result_manifest_sha256="b" * 64,
        selected_execution_artifact=_execution_artifact({"f1": ("0.9",)}),
    )
    bundle = replace(
        bundle,
        paper=paper,
        stage23_publication=replace(bundle.stage23_publication, outputs=(paper,)),
        stage23_inputs=SimpleNamespace(
            stage22_inputs=SimpleNamespace(
                evidence=evidence,
                stage19_inputs=bundle.stage23_inputs.stage22_inputs.stage19_inputs,
            )
        ),  # type: ignore[arg-type]
        citation_plan=MappingProxyType({"claims": ()}),
        evidence_cards=(),
        verification=MappingProxyType({"results": ()}),
        entries=(),
    )
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio"},
            metric_display_labels={"f1": ["F1"]},
        ),
    )

    obligations = build_claim_obligation_inventory(bundle.paper.content)
    numeric_support = _numeric_support(bundle, obligations)
    supported = next(
        item for item in numeric_support.values() if item["status"] == "supported"
    )
    execution_payload = json.loads(
        evidence.selected_execution_artifact.content.decode("utf-8")
    )
    assert supported["authority_path"] == "stage-12/evidence-v1/run-1.json"
    assert supported["authority_sha256"] == evidence.selected_execution_artifact.sha256
    assert supported["semantic_pointer"] == "/metric_observations/f1/0"
    assert execution_payload["metric_observations"]["f1"][0] == 0.9

    _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )

    claims = json.loads((stage_dir / "claims.json").read_text())
    assert claims["counts"] == {
        "not_required": 0,
        "supported": 2,
        "total": 2,
        "unsupported": 0,
    }


def test_stage24_numeric_support_replays_domain_observation_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _domain_numeric_bundle(b"## Results\n\nAUPRC was 0.75.\n")
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"auprc": "ratio"},
            metric_display_labels={"auprc": ["AUPRC"]},
        ),
    )

    support = _numeric_support(
        bundle, build_claim_obligation_inventory(bundle.paper.content)
    )
    supported = next(item for item in support.values() if item["status"] == "supported")

    assert supported["invocation_ordinal"] is None
    assert supported["authority_path"] == "stage-12/evidence-v2/observations.json"
    assert supported["semantic_pointer"] == "/observations/0/metrics/auprc"


def test_stage24_numeric_label_requires_lexical_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _numeric_bundle(b"## Results\n\nF10 was 0.9.\n", {"f1": ("0.9",)})
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio"},
            metric_display_labels={"f1": ["F1"]},
        ),
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)

    support = _numeric_support(bundle, obligations)

    numeric = next(row for row in obligations if row.kind == "numeric_token")
    assert support[numeric.obligation_id]["status"] == "unsupported"


def test_stage24_numeric_label_binds_each_local_operand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _numeric_bundle(
        b"## Results\n\nF1 was 0.9 and Accuracy was 0.8.\n",
        {"f1": ("0.8",), "accuracy": ("0.9",)},
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio", "accuracy": "ratio"},
            metric_display_labels={"f1": ["F1"], "accuracy": ["Accuracy"]},
        ),
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)

    support = _numeric_support(bundle, obligations)

    assert {
        row["status"] for row in support.values()
    } == {"unsupported"}


def test_stage24_percentage_transform_is_global_context_independent() -> None:
    outputs = []
    for precision in (7, 28, 80):
        with localcontext() as context:
            context.prec = precision
            outputs.append(
                _transform_value(
                    Decimal("12.34567890123456789"), "%", "ratio"
                )
            )

    assert outputs == [Decimal("0.1234567890123456789")] * 3


def test_stage24_percentage_does_not_collapse_adjacent_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = "0.1234567890123456790"
    bundle = _numeric_bundle(
        b"## Results\n\nF1 was 12.34567890123456789%.\n",
        {"f1": (authority,)},
    )
    raw = (
        '{"evaluator_schema":"hpc_anomaly_detection_v1",'
        '"invocation_policy_version":1,"metric_observations":{"f1":['
        + authority
        + ']},"ordinal":1,"schema_version":1,"status":"completed",'
        '"structured_results":{}}\n'
    ).encode()
    evidence = SimpleNamespace(
        metric_observations=MappingProxyType({"f1": (authority,)}),
        selected_result_manifest_path="stage-12/experiment_result_set.json",
        selected_result_manifest_sha256="b" * 64,
        selected_execution_artifact=_bound(
            "stage-12/evidence-v1/run-1.json", raw
        ),
    )
    bundle = replace(
        bundle,
        stage23_inputs=SimpleNamespace(
            stage22_inputs=SimpleNamespace(
                evidence=evidence,
                stage19_inputs=bundle.stage23_inputs.stage22_inputs.stage19_inputs,
            )
        ),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio"},
            metric_display_labels={"f1": ["F1"]},
        ),
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)

    support = _numeric_support(bundle, obligations)

    numeric = next(row for row in obligations if row.kind == "numeric_token")
    assert support[numeric.obligation_id]["status"] == "unsupported"


@pytest.mark.parametrize(
    "paper_bytes",
    (
        b"## Results\n\nF1 0.9 is not higher than F1 0.8.\n",
        b"## Results\n\nIf F1 0.9 is higher than F1 0.8.\n",
        b"## Results\n\nF1 0.9 is higher and lower than F1 0.8.\n",
        b"## Results\n\nF1 0.9 is bounded higher than F1 0.8.\n",
    ),
)
def test_stage24_comparison_v1_rejects_nonexact_grammar(
    paper_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _numeric_bundle(paper_bytes, {"f1": ("0.9", "0.8")})
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio"},
            metric_display_labels={"f1": ["F1"]},
        ),
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)
    numeric = _numeric_support(bundle, obligations)
    comparison = next(row for row in obligations if row.kind == "comparative_sentence")

    assert _comparison_status(
        bundle.paper.content, comparison, obligations, numeric
    ) == "unsupported"


def test_stage24_comparison_v1_accepts_exact_positive_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _numeric_bundle(
        b"## Results\n\nF1 0.9 is higher than F1 0.8.\n",
        {"f1": ("0.9", "0.8")},
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(
            metric_units={"f1": "ratio"},
            metric_display_labels={"f1": ["F1"]},
        ),
    )
    obligations = build_claim_obligation_inventory(bundle.paper.content)
    numeric = _numeric_support(bundle, obligations)
    comparison = next(row for row in obligations if row.kind == "comparative_sentence")

    assert _comparison_status(
        bundle.paper.content, comparison, obligations, numeric
    ) == "supported"


def test_stage24_resolution_assessment_closes_each_serious_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    finding = MappingProxyType(
        {
            "id": "finding-1",
            "severity": "P1",
            "category": "evidence",
            "question": "Is support visible?",
            "finding": "The support must be visible.",
            "falsification_criterion": "Show the retained citation.",
        }
    )
    critique_publication = replace(
        bundle.critique_publication,
        critique=MappingProxyType(
            {"state": "model_final", "findings": (finding,)}
        ),
    )
    bundle = replace(bundle, critique_publication=critique_publication)
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )

    _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )

    resolution = json.loads((stage_dir / "critique_resolution.json").read_text())
    assert resolution["counts"] == {
        "fixed": 1,
        "rebutted": 0,
        "total": 1,
        "unresolved": 0,
    }


@pytest.mark.parametrize(
    "target",
    (
        "claims.json",
        "citations.json",
        "citation_support.json",
        "critique_resolution.json",
        "truth_audit.json",
        "obligation_inventory.json",
        "stage24_truth_manifest.json",
        "citation-assessments",
        "generic-support-assessments",
        "resolution-assessments",
    ),
)
def test_stage24_replay_rejects_every_bound_output_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    bundle = _bundle()
    config = RCConfig.load(Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )
    _publish_stage24_truth(
        run_dir,
        stage_dir,
        bundle=bundle,
        runtime_config=config,
        llm=_SchemaLLM(),  # type: ignore[arg-type]
    )
    path = stage_dir / target
    if path.is_dir():
        (path / "shadow.json").write_text("{}\n", encoding="utf-8")
    else:
        path.write_text("{}\n", encoding="utf-8")

    with pytest.raises((Stage24PublicationError, ValueError, OSError)):
        _load_stage24_truth_publication(run_dir, bundle=bundle)


def test_stage24_final_output_replay_cannot_hide_late_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    source = run_dir / "stage-23" / "source.txt"
    source.parent.mkdir()
    source.write_text("captured", encoding="utf-8")
    bundle = _bundle()
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )

    def verify_source(*_args):
        if source.read_text(encoding="utf-8") != "captured":
            raise Stage24PublicationError("captured source changed")

    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        verify_source,
    )
    from researchclaw.pipeline import stage24_publication as publication

    original = publication._load_stage24_truth_publication_from_namespace
    calls = 0

    def mutate_during_final_output_replay(*args, **kwargs):
        nonlocal calls
        calls += 1
        snapshot = original(*args, **kwargs)
        if calls == 2:
            source.write_text("mutated", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(
        publication,
        "_load_stage24_truth_publication_from_namespace",
        mutate_during_final_output_replay,
    )

    with pytest.raises(Stage24PublicationError, match="captured source changed"):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            llm=_SchemaLLM(),  # type: ignore[arg-type]
        )

    assert tuple(stage_dir.iterdir()) == ()


@pytest.mark.parametrize("replacement_point", ("llm", "publication"))
def test_stage24_parent_replacement_never_writes_external_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_point: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    moved = run_dir / "stage-24-moved"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    bundle = _bundle()
    config = RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.validate_contract_dict",
        lambda *_args: SimpleNamespace(metric_units={}, metric_display_labels={}),
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_publication.verify_stage24_input_bundle_unchanged",
        lambda *_args: None,
    )
    replaced = False

    def replace_parent() -> None:
        nonlocal replaced
        if replaced:
            return
        stage_dir.rename(moved)
        stage_dir.symlink_to(external, target_is_directory=True)
        replaced = True

    llm = _SchemaLLM()
    if replacement_point == "llm":
        original_chat = llm.chat

        def replacing_chat(*args, **kwargs):
            replace_parent()
            return original_chat(*args, **kwargs)

        llm.chat = replacing_chat  # type: ignore[method-assign]
    else:
        from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace

        original_publish = BoundOutputNamespace.publish_staged_tree

        def replacing_publish(namespace, *args, **kwargs):
            replace_parent()
            return original_publish(namespace, *args, **kwargs)

        monkeypatch.setattr(
            BoundOutputNamespace, "publish_staged_tree", replacing_publish
        )

    with pytest.raises((Stage24PublicationError, OSError, ValueError)):
        _publish_stage24_truth(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            llm=llm,  # type: ignore[arg-type]
        )

    assert sentinel.read_text(encoding="utf-8") == "external"
    assert set(external.iterdir()) == {sentinel}
    assert tuple(moved.iterdir()) == ()
