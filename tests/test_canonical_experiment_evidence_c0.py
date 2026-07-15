from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_EVEN, getcontext, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw import cli as rc_cli
from researchclaw.collaboration.publisher import ArtifactPublisher
from researchclaw.collaboration.subscriber import ArtifactSubscriber
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import derive_contract, dump_contract, sha256_file
from researchclaw.experiment_runtime.scaffold import render_main_py
from researchclaw.experiment_runtime.metric_authority import normalize_topic, select_metric_authority
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.literature.experiment_fact_closure import (
    ExperimentFactClosureError,
    build_experiment_fact_closure_report,
    canonical_experiment_fact_json_text,
    validate_experiment_fact_closure_report,
)
from researchclaw.mcp.server import ResearchClawMCPServer
from researchclaw.memory.experiment_memory import ExperimentMemory
from researchclaw.evolution import extract_lessons
from researchclaw.pipeline._helpers import (
    _get_evolution_overlay,
    _get_pipeline_evolution_overlay,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CAPABILITY_SCHEMA_VERSION,
    REQUIRED_CAPABILITIES,
    CanonicalEvidenceMigrationIncomplete,
    incomplete_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalAnalysisController,
    CanonicalExecutionController,
    CanonicalRefinementController,
    require_controller_lease,
)
from researchclaw.pipeline.experiment_repair import (
    _run_experiment_in_sandbox,
    run_repair_loop,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    canonical_decimal,
    invocation_generation_binding_sha256,
    load_canonical_experiment_evidence,
    reconstruct_expected_stage9_14_metric_authority,
    parse_aggregate_results,
    parse_canonical_experiment_manifest,
    parse_execution_invocation_journal,
    parse_experiment_evidence_candidate,
    parse_experiment_result_set,
    parse_invocation_result,
    parse_refinement_result_set,
    parse_refinement_validation_report,
    parse_selected_candidate_manifest,
    semantic_config_sha256,
    sha256_text,
    validate_selected_candidate_manifest,
    validate_experiment_evidence_candidate,
    validate_canonical_experiment_manifest,
    validate_experiment_result_set,
    validate_refinement_result_set,
    validate_single_invocation_aggregate,
    publish_canonical_experiment_manifest,
    _stage12_primary_metric,
)
from researchclaw.pipeline.stage_impls._analysis import _execute_research_decision
from researchclaw.pipeline.stage_impls._paper_writing import (
    _collect_grounded_metric_whitelist,
    _collect_raw_experiment_metrics,
    _execute_paper_draft,
    _execute_paper_outline,
    _load_bound_stage15_decision,
    _load_bound_stage16_outline,
    _write_outline_binding,
)
from researchclaw.pipeline.runner import execute_pipeline
from researchclaw.pipeline import runner as pipeline_runner
from researchclaw.pipeline.runner import _metaclaw_post_pipeline
from researchclaw.pipeline.executor import StageResult, execute_stage
from researchclaw.pipeline.stage_impls._code_generation import _seal_selected_candidate
from researchclaw.pipeline.stage_impls._execution import (
    _execute_experiment_run,
    _execute_iterative_refine,
)
from researchclaw.pipeline.stage_impls._analysis import _execute_result_analysis
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.report import generate_report


SHA = "1" * 64
METRIC_AUTHORITY = select_metric_authority(
    "Hardware-performance-counter detection of Spectre attacks",
    "sandbox",
).contract_identity()
EMPTY_FIGURE_PLAN_TEXT = canonical_authority_json_text(
    {
        "schema_version": 1,
        "generator": "canonical_stage14_v1",
        "figures": [],
    }
)


def _enable_complete_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES},
    )


def _config(run_dir: Path) -> RCConfig:
    raw = yaml.safe_load(
        Path("config.deepseek.sectional-dry-run.yaml").read_text(encoding="utf-8")
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "config.yaml"
    snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    write_active_config_binding(run_dir, snapshot)
    return config


def _hpc_structured_result(metric: float = 0.5) -> dict[str, object]:
    per_seed_metrics = {
        "accuracy": 0.5,
        "detection_f1": metric,
        "precision": 0.5,
        "tpr": 0.5,
        "tnr": 0.5,
        "fpr": 0.5,
        "latency_ms": 1.0,
    }
    return {
        "schema_version": 1,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "dataset_name": "synthetic_pipeline_validation_v1",
        "primary_metric": {
            "key": "detection_f1",
            "value": metric,
            "direction": "maximize",
        },
        "metrics": dict(per_seed_metrics),
        "seeds": [42, 123, 256],
        "conditions": ["DetectorPlugin"],
        "per_seed": [
            {"seed": seed, "metrics": dict(per_seed_metrics)}
            for seed in (42, 123, 256)
        ],
        "runtime_sec": 1.0,
        "evaluator_owner": "scaffold",
    }


def _normalized_hpc_metrics(result: dict[str, object]) -> dict[str, list[float]]:
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    return {"detection_f1": [metrics["detection_f1"]]}


def _sealed_candidate(run_dir: Path) -> tuple[RCConfig, dict[str, object]]:
    config = _config(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    contract_path = stage9 / "experiment_contract.yaml"
    contract = derive_contract(
        config,
        {"datasets": ["synthetic"]},
        stage_dir=stage9,
    )
    dump_contract(contract, contract_path)
    stage10 = run_dir / "stage-10"
    experiment = stage10 / "experiment"
    experiment.mkdir(parents=True)
    (experiment / "main.py").write_text(render_main_py(contract), encoding="utf-8")
    (experiment / "detector_plugin.py").write_text(
        "def fit(X, y):\n    return None\n\ndef predict(X):\n    return [0] * len(X)\n",
        encoding="utf-8",
    )
    _seal_selected_candidate(stage10, experiment, contract_path, config)
    text = (stage10 / "selected_candidate_manifest.json").read_text(encoding="utf-8")
    return config, json.loads(text)


def _write_canonical_bundle(run_dir: Path) -> tuple[RCConfig, dict[str, object]]:
    config, seal = _sealed_candidate(run_dir)
    seal_text = (run_dir / "stage-10/selected_candidate_manifest.json").read_text(encoding="utf-8")
    common = {
        "experiment_contract_path": seal["contract_path"],
        "experiment_contract_sha256": seal["contract_sha256"],
        "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
        "sealed_candidate_manifest_sha256": sha256_text(seal_text),
        "run_config_path": seal["run_config_path"],
        "run_config_sha256": seal["run_config_sha256"],
        "config_semantic_policy_version": 1,
        "config_semantic_sha256": seal["config_semantic_sha256"],
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_authority": seal["metric_authority"],
    }
    stage12 = run_dir / "stage-12"
    evidence12 = stage12 / "evidence-v1"
    evidence12.mkdir(parents=True)
    structured_result = _hpc_structured_result()
    normalized_metrics = _normalized_hpc_metrics(structured_result)
    invocation = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": common["evaluator_schema"],
        "metric_observations": normalized_metrics,
        "structured_results": structured_result,
    }
    aggregate = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": common["evaluator_schema"],
        "source_ordinals": [1],
        "metric_observations": normalized_metrics,
        "structured_results": structured_result,
    }
    run_text = canonical_json_text(invocation)
    aggregate_text = canonical_json_text(aggregate)
    (evidence12 / "run-1.json").write_text(run_text, encoding="utf-8")
    (evidence12 / "results.json").write_text(aggregate_text, encoding="utf-8")
    binding = invocation_generation_binding_sha256(
        experiment_contract_sha256=common["experiment_contract_sha256"],
        sealed_candidate_manifest_sha256=common["sealed_candidate_manifest_sha256"],
        config_semantic_sha256=common["config_semantic_sha256"],
        experiment_mode="sandbox",
        evaluator_schema=common["evaluator_schema"],
    )
    token = "2" * 64
    journal_text = "".join(
        json.dumps(row, sort_keys=True) + "\n"
        for row in (
            {
                "schema_version": 1,
                "event": "started",
                "ordinal": 1,
                "invocation_token": token,
                "generation_binding_sha256": binding,
                "experiment_contract_sha256": common["experiment_contract_sha256"],
                "sealed_candidate_manifest_sha256": common["sealed_candidate_manifest_sha256"],
                "config_semantic_sha256": common["config_semantic_sha256"],
            },
            {
                "schema_version": 1,
                "event": "terminal",
                "ordinal": 1,
                "invocation_token": token,
                "status": "completed",
                "result_path": "stage-12/evidence-v1/run-1.json",
                "result_sha256": sha256_text(run_text),
                "failure_code": None,
            },
        )
    )
    (stage12 / "execution_invocation_journal.jsonl").write_text(journal_text, encoding="utf-8")
    result_set = {
        "schema_version": 1,
        "result_set_policy_version": 1,
        "result_set_type": "stage12_baseline",
        "experiment_mode": "sandbox",
        **common,
        "invocation_journal": {
            "path": "stage-12/execution_invocation_journal.jsonl",
            "sha256": sha256_text(journal_text),
        },
        "execution_statuses": [{
            "ordinal": 1,
            "status": "completed",
            "result_path": "stage-12/evidence-v1/run-1.json",
            "failure_code": None,
        }],
        "evidence_files": [
            {"path": "stage-12/evidence-v1/results.json", "sha256": sha256_text(aggregate_text)},
            {"path": "stage-12/evidence-v1/run-1.json", "sha256": sha256_text(run_text)},
        ],
    }
    result_set_text = canonical_json_text(result_set)
    (stage12 / "experiment_result_set.json").write_text(result_set_text, encoding="utf-8")

    stage13 = run_dir / "stage-13"
    (stage13 / "evidence-v1").mkdir(parents=True)
    final13 = stage13 / "experiment_final"
    final13.mkdir()
    for name in ("detector_plugin.py", "main.py"):
        payload = (run_dir / "stage-10/selected_candidate" / name).read_bytes()
        (final13 / name).write_bytes(payload)
    (stage13 / "experiment_final.py").write_bytes(
        (run_dir / "stage-10/selected_candidate/main.py").read_bytes()
    )
    refinement_log_text = "{}\n"
    (stage13 / "refinement_log.json").write_text(refinement_log_text, encoding="utf-8")
    refinement = {
        "schema_version": 1,
        "refinement_policy_version": 1,
        "result_set_type": "stage13_refinement",
        "baseline_manifest": {
            "path": "stage-12/experiment_result_set.json",
            "sha256": sha256_text(result_set_text),
        },
        **common,
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "iterations": [],
        "refinement_log": {
            "path": "stage-13/refinement_log.json",
            "sha256": sha256_text(refinement_log_text),
        },
        "selected_result": {"type": "baseline", "iteration_id": None},
    }
    refinement_text = canonical_json_text(refinement)
    (stage13 / "refinement_result_set.json").write_text(refinement_text, encoding="utf-8")

    stage14 = run_dir / "stage-14/evidence_candidates"
    artifacts = {
        "analysis.md": "Analysis.\n",
        "figure_plan.json": EMPTY_FIGURE_PLAN_TEXT,
        "results_table.tex": "table\n",
        "experiment_summary.json": json.dumps(
            {"metrics_summary": {"detection_f1": {"mean": 0.5}}}, sort_keys=True
        ) + "\n",
    }
    role_paths = {
        "analysis": "analysis.md",
        "figure_plan": "figure_plan.json",
        "results_table": "results_table.tex",
        "summary": "experiment_summary.json",
    }
    identity_artifacts = [
        {"role": role, "logical_name": path, "sha256": sha256_text(artifacts[path])}
        for role, path in sorted(role_paths.items())
    ]
    identity = {
        "candidate_identity_policy_version": 1,
        "selected_result_type": "stage13_refinement",
        "selected_result_manifest_sha256": sha256_text(refinement_text),
        "experiment_contract_sha256": common["experiment_contract_sha256"],
        "config_semantic_sha256": common["config_semantic_sha256"],
        "metric_authority_selector_input_sha256": common["metric_authority"]["selector_input_sha256"],
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "artifacts": identity_artifacts,
    }
    identity_sha = sha256_text(canonical_json_text(identity))
    candidate_id = "cand-" + identity_sha
    candidate_dir = stage14 / candidate_id
    candidate_dir.mkdir(parents=True)
    for path, content in artifacts.items():
        (candidate_dir / path).write_text(content, encoding="utf-8")
    selected_result = {
        "result_set_type": "stage13_refinement",
        "manifest_path": "stage-13/refinement_result_set.json",
        "manifest_sha256": sha256_text(refinement_text),
    }
    candidate = {
        "schema_version": 1,
        "candidate_policy_version": 1,
        "candidate_id": candidate_id,
        "identity_payload": identity,
        "identity_payload_sha256": identity_sha,
        "selected_result": selected_result,
        **common,
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "primary_metric_value": "0.5",
        "artifacts": [
            {"role": role, "path": path, "sha256": sha256_text(artifacts[path])}
            for role, path in sorted(role_paths.items())
        ],
    }
    candidate_text = canonical_json_text(candidate)
    candidate_manifest = candidate_dir / "experiment_evidence_candidate.json"
    candidate_manifest.write_text(candidate_text, encoding="utf-8")
    (run_dir / "experiment_summary_best.json").write_text(
        artifacts["experiment_summary.json"], encoding="utf-8"
    )
    (run_dir / "analysis_best.md").write_text(artifacts["analysis.md"], encoding="utf-8")
    root = {
        "schema_version": 1,
        "selection_policy_version": 1,
        "selected_result": selected_result,
        **common,
        "primary_metric": "detection_f1",
        "optimization_direction": "maximize",
        "selected_candidate": {
            "candidate_id": candidate_id,
            "path": candidate_manifest.relative_to(run_dir).as_posix(),
            "sha256": sha256_text(candidate_text),
        },
        "selected_summary": {
            "source_path": (candidate_dir / "experiment_summary.json").relative_to(run_dir).as_posix(),
            "source_sha256": sha256_text(artifacts["experiment_summary.json"]),
            "canonical_path": "experiment_summary_best.json",
            "canonical_sha256": sha256_text(artifacts["experiment_summary.json"]),
        },
        "selected_analysis": {
            "source_path": (candidate_dir / "analysis.md").relative_to(run_dir).as_posix(),
            "source_sha256": sha256_text(artifacts["analysis.md"]),
            "canonical_path": "analysis_best.md",
            "canonical_sha256": sha256_text(artifacts["analysis.md"]),
        },
    }
    (run_dir / "canonical_experiment_evidence.json").write_text(
        canonical_json_text(root), encoding="utf-8"
    )
    return config, root


def test_accessor_snapshots_selected_project_bytes_under_canonical_lock(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _config, _manifest = _write_canonical_bundle(run_dir)

    evidence = load_canonical_experiment_evidence(run_dir)

    assert [item.logical_name for item in evidence.project_artifacts] == [
        "detector_plugin.py",
        "main.py",
    ]
    for item in evidence.project_artifacts:
        assert hashlib.sha256(item.content).hexdigest() == item.sha256
        assert item.source_path.startswith("stage-10/selected_candidate/")
    captured = evidence.project_artifacts[0].content
    source = run_dir / evidence.project_artifacts[0].source_path
    source.write_text("tampered after snapshot\n", encoding="utf-8")
    assert evidence.project_artifacts[0].content == captured
    with pytest.raises(CanonicalExperimentEvidenceError):
        load_canonical_experiment_evidence(run_dir)


def test_u0_expected_helper_replays_metric_authority_through_stage14(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)

    expected = reconstruct_expected_stage9_14_metric_authority(run_dir, config)

    assert expected["schema_version"] == 1
    assert expected["metric_authority"]["domain_id"] == "security_detection"
    assert expected["metric_authority"]["evaluator_id"] == "hpc_anomaly_detection"
    assert expected["metric_units"]["detection_f1"] == "ratio"


def test_u0_expected_helper_rejects_run_local_domain_selector_as_oracle(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)
    snapshot_path = run_dir / "stage-09" / "domain_selector_policy.json"
    substituted = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for rule in substituted["rules"]:
        rule["domain_id"] = "attacker_selected_domain"
    snapshot_path.write_text(
        json.dumps(substituted, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="expected metric authority reconstruction failed",
    ):
        reconstruct_expected_stage9_14_metric_authority(run_dir, config)


def test_u0_expected_helper_rejects_root_metric_authority_tamper(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)
    root_path = run_dir / "canonical_experiment_evidence.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    root["metric_authority"]["evaluator_id"] = "attacker_selected_evaluator"
    root_path.write_text(canonical_json_text(root), encoding="utf-8")

    with pytest.raises(CanonicalExperimentEvidenceError):
        reconstruct_expected_stage9_14_metric_authority(run_dir, config)


def test_u0_expected_helper_replays_versioned_stage14_collections(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)
    poisoned = run_dir / "stage-14_v2" / "evidence_candidates" / "not-canonical"
    poisoned.mkdir(parents=True)

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="noncanonical entry",
    ):
        reconstruct_expected_stage9_14_metric_authority(run_dir, config)


def test_u0_expected_helper_rejects_versioned_mixed_authority_collision(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)
    source = next((run_dir / "stage-14/evidence_candidates").glob("cand-*"))
    duplicate = run_dir / "stage-14_v2/evidence_candidates" / source.name
    shutil.copytree(source, duplicate)
    manifest_path = duplicate / "experiment_evidence_candidate.json"
    candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate["metric_authority"]["evaluator_id"] = "mixed_authority"
    manifest_path.write_text(canonical_json_text(candidate), encoding="utf-8")

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="candidate ID collision",
    ):
        reconstruct_expected_stage9_14_metric_authority(run_dir, config)


def test_u0_expected_helper_rejects_coherent_run_local_b_authority_chain(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config, _manifest = _write_canonical_bundle(run_dir)
    stage9 = run_dir / "stage-09"

    policy = json.loads((stage9 / "domain_selector_policy.json").read_text())
    for rule in policy["rules"]:
        rule["domain_id"] = "attacker_domain"
    profile = {
        "schema_version": 1,
        "selector_policy_version": "domain_selector_v1",
        "domain_id": "attacker_domain",
        "owner": "scaffold",
        "supported_experiment_modes": ["sandbox"],
    }
    index = {
        "schema_version": 1,
        "selector_policy_version": "metric_selector_v1",
        "entries": [{
            "domain_id": "attacker_domain",
            "experiment_mode": "sandbox",
            "evaluator_kind": "scaffold",
            "evaluator_id": "attacker_evaluator",
        }],
    }
    registry = {
        "schema_version": 1,
        "policy_version": 1,
        "domain_id": "attacker_domain",
        "evaluator_id": "attacker_evaluator",
        "owner": "scaffold",
        "metrics": [{
            "key": "detection_f1",
            "unit": "ratio",
            "display_labels": ["attacker F1"],
        }],
    }
    snapshot_payloads = {
        "domain_selector_policy.json": policy,
        "domain_profile.json": profile,
        "metric_authority_index.json": index,
        "metric_authority.json": registry,
    }
    snapshot_hashes: dict[str, str] = {}
    for name, payload in snapshot_payloads.items():
        text = canonical_json_text(payload)
        (stage9 / name).write_text(text, encoding="utf-8")
        snapshot_hashes[name] = sha256_text(text)

    normalized = normalize_topic(config.research.topic)
    profile_identity = {
        "domain_id": "attacker_domain",
        "package_profile_path": (
            "experiment_runtime/metric_authority/profiles/attacker_domain-v1.json"
        ),
        "package_profile_sha256": snapshot_hashes["domain_profile.json"],
        "topic_raw_sha256": hashlib.sha256(
            config.research.topic.encode("utf-8")
        ).hexdigest(),
        "topic_normalized_sha256": hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest(),
        "domain_selector_package_path": (
            "experiment_runtime/metric_authority/domain-selector-v1.json"
        ),
        "domain_selector_package_sha256": snapshot_hashes[
            "domain_selector_policy.json"
        ],
        "selector_policy_version": "domain_selector_v1",
    }
    selector_input = {
        "canonical_domain_profile_identity": profile_identity,
        "experiment_mode": "sandbox",
        "evaluator_kind": "scaffold",
        "selector_policy_version": "metric_selector_v1",
        "domain_selector_package_sha256": snapshot_hashes[
            "domain_selector_policy.json"
        ],
        "selector_index_sha256": snapshot_hashes["metric_authority_index.json"],
    }
    selector_input_sha = hashlib.sha256(
        json.dumps(
            selector_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    forged = {
        "path": "stage-09/metric_authority.json",
        "sha256": snapshot_hashes["metric_authority.json"],
        "policy_version": 1,
        "domain_id": "attacker_domain",
        "evaluator_id": "attacker_evaluator",
        "domain_profile_path": "stage-09/domain_profile.json",
        "domain_profile_sha256": snapshot_hashes["domain_profile.json"],
        "selector_index_path": "stage-09/metric_authority_index.json",
        "selector_index_sha256": snapshot_hashes["metric_authority_index.json"],
        "domain_selector_package_path": (
            "experiment_runtime/metric_authority/domain-selector-v1.json"
        ),
        "domain_selector_package_sha256": snapshot_hashes[
            "domain_selector_policy.json"
        ],
        "domain_selector_snapshot_path": "stage-09/domain_selector_policy.json",
        "domain_selector_snapshot_sha256": snapshot_hashes[
            "domain_selector_policy.json"
        ],
        "selector_input_sha256": selector_input_sha,
        "selector_policy_version": "metric_selector_v1",
        "experiment_mode": "sandbox",
        "evaluator_kind": "scaffold",
    }

    contract_path = stage9 / "experiment_contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["metric_authority"] = forged
    contract["metric_units"] = {"detection_f1": "ratio"}
    contract["metric_display_labels"] = {"detection_f1": ["attacker F1"]}
    contract_path.write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
    )
    contract_sha = sha256_file(contract_path)

    seal_path = run_dir / "stage-10/selected_candidate_manifest.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["contract_sha256"] = contract_sha
    seal["metric_authority"] = forged
    seal_text = canonical_json_text(seal)
    seal_path.write_text(seal_text, encoding="utf-8")
    seal_sha = sha256_text(seal_text)

    result_path = run_dir / "stage-12/experiment_result_set.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["experiment_contract_sha256"] = contract_sha
    result["sealed_candidate_manifest_sha256"] = seal_sha
    result["metric_authority"] = forged
    journal_path = run_dir / "stage-12/execution_invocation_journal.jsonl"
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    binding = invocation_generation_binding_sha256(
        experiment_contract_sha256=contract_sha,
        sealed_candidate_manifest_sha256=seal_sha,
        config_semantic_sha256=result["config_semantic_sha256"],
        experiment_mode="sandbox",
        evaluator_schema=result["evaluator_schema"],
    )
    journal[0]["experiment_contract_sha256"] = contract_sha
    journal[0]["sealed_candidate_manifest_sha256"] = seal_sha
    journal[0]["generation_binding_sha256"] = binding
    journal_text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in journal)
    journal_path.write_text(journal_text, encoding="utf-8")
    result["invocation_journal"]["sha256"] = sha256_text(journal_text)
    result_text = canonical_json_text(result)
    result_path.write_text(result_text, encoding="utf-8")

    refinement_path = run_dir / "stage-13/refinement_result_set.json"
    refinement = json.loads(refinement_path.read_text(encoding="utf-8"))
    refinement["experiment_contract_sha256"] = contract_sha
    refinement["sealed_candidate_manifest_sha256"] = seal_sha
    refinement["metric_authority"] = forged
    refinement["baseline_manifest"]["sha256"] = sha256_text(result_text)
    refinement_text = canonical_json_text(refinement)
    refinement_path.write_text(refinement_text, encoding="utf-8")

    candidate_path = next(
        (run_dir / "stage-14/evidence_candidates").glob(
            "cand-*/experiment_evidence_candidate.json"
        )
    )
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["experiment_contract_sha256"] = contract_sha
    candidate["sealed_candidate_manifest_sha256"] = seal_sha
    candidate["metric_authority"] = forged
    candidate["selected_result"]["manifest_sha256"] = sha256_text(refinement_text)
    identity = candidate["identity_payload"]
    identity["experiment_contract_sha256"] = contract_sha
    identity["selected_result_manifest_sha256"] = sha256_text(refinement_text)
    identity["metric_authority_selector_input_sha256"] = selector_input_sha
    identity_sha = sha256_text(canonical_json_text(identity))
    candidate_id = "cand-" + identity_sha
    candidate["candidate_id"] = candidate_id
    candidate["identity_payload_sha256"] = identity_sha
    candidate_text = canonical_json_text(candidate)
    old_dir = candidate_path.parent
    new_dir = old_dir.parent / candidate_id
    old_dir.rename(new_dir)
    candidate_path = new_dir / candidate_path.name
    candidate_path.write_text(candidate_text, encoding="utf-8")

    root_path = run_dir / "canonical_experiment_evidence.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    root["experiment_contract_sha256"] = contract_sha
    root["sealed_candidate_manifest_sha256"] = seal_sha
    root["metric_authority"] = forged
    root["selected_result"]["manifest_sha256"] = sha256_text(refinement_text)
    root["selected_candidate"] = {
        "candidate_id": candidate_id,
        "path": candidate_path.relative_to(run_dir).as_posix(),
        "sha256": sha256_text(candidate_text),
    }
    root_path.write_text(canonical_json_text(root), encoding="utf-8")

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="expected metric authority reconstruction failed",
    ):
        reconstruct_expected_stage9_14_metric_authority(run_dir, config)


def _prepare_stage14_upstream(run_dir: Path) -> RCConfig:
    config, _root = _write_canonical_bundle(run_dir)
    shutil.rmtree(run_dir / "stage-14")
    for name in (
        "canonical_experiment_evidence.json",
        "experiment_summary_best.json",
        "analysis_best.md",
    ):
        (run_dir / name).unlink()
    (run_dir / "stage-14").mkdir()
    return config


def _add_refinement_iteration(
    run_dir: Path,
    *,
    metric: float,
    accepted: bool,
    rejection_codes: list[str],
    primary_metric_observation: float | None,
) -> None:
    manifest_path = run_dir / "stage-13/refinement_result_set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    iteration_root = run_dir / "stage-13/evidence-v1/iterations/iter-1"
    project_root = iteration_root / "project"
    project_root.mkdir(parents=True)
    for name in ("main.py", "detector_plugin.py"):
        (project_root / name).write_bytes(
            (run_dir / "stage-10/selected_candidate" / name).read_bytes()
        )
    project_refs = [
        {
            "path": f"stage-13/evidence-v1/iterations/iter-1/project/{name}",
            "sha256": sha256_file(project_root / name),
        }
        for name in ("detector_plugin.py", "main.py")
    ]
    validation = {
        "schema_version": 1,
        "validation_policy_version": 1,
        "project_files_sha256": sha256_text(canonical_json_text(project_refs)),
        "checks": {"python_syntax_valid": True},
    }
    validation_text = canonical_json_text(validation)
    validation_path = iteration_root / "validation_report.json"
    validation_path.write_text(validation_text, encoding="utf-8")
    structured_result = _hpc_structured_result(metric)
    execution = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": manifest["evaluator_schema"],
        "metric_observations": _normalized_hpc_metrics(structured_result),
        "structured_results": structured_result,
    }
    execution_text = canonical_json_text(execution)
    execution_path = iteration_root / "initial_execution.json"
    execution_path.write_text(execution_text, encoding="utf-8")
    manifest["iterations"] = [{
        "ordinal": 1,
        "iteration_id": "iter-1",
        "project_files": project_refs,
        "validation_report": {
            "path": "stage-13/evidence-v1/iterations/iter-1/validation_report.json",
            "sha256": sha256_text(validation_text),
        },
        "initial_execution": {
            "path": "stage-13/evidence-v1/iterations/iter-1/initial_execution.json",
            "sha256": sha256_text(execution_text),
        },
        "runtime_repair": None,
        "accepted": accepted,
        "rejection_codes": rejection_codes,
        "primary_metric_observation": primary_metric_observation,
    }]
    manifest_path.write_text(canonical_json_text(manifest), encoding="utf-8")


def _clone_candidate(
    run_dir: Path,
    root: dict[str, object],
    *,
    analysis_text: str,
) -> tuple[dict[str, object], Path, str]:
    source_manifest = run_dir / root["selected_candidate"]["path"]
    source_root = source_manifest.parent
    candidate = json.loads(source_manifest.read_text(encoding="utf-8"))
    analysis_sha = sha256_text(analysis_text)
    for artifact in candidate["identity_payload"]["artifacts"]:
        if artifact["role"] == "analysis":
            artifact["sha256"] = analysis_sha
    identity_sha = sha256_text(canonical_json_text(candidate["identity_payload"]))
    candidate["identity_payload_sha256"] = identity_sha
    candidate["candidate_id"] = "cand-" + identity_sha
    for artifact in candidate["artifacts"]:
        if artifact["role"] == "analysis":
            artifact["sha256"] = analysis_sha
    target_root = run_dir / "stage-14_v1/evidence_candidates" / candidate["candidate_id"]
    target_root.mkdir(parents=True)
    for source in source_root.iterdir():
        if source.name == "experiment_evidence_candidate.json":
            continue
        (target_root / source.name).write_bytes(source.read_bytes())
    (target_root / "analysis.md").write_text(analysis_text, encoding="utf-8")
    candidate_text = canonical_json_text(candidate)
    target_manifest = target_root / "experiment_evidence_candidate.json"
    target_manifest.write_text(candidate_text, encoding="utf-8")
    return candidate, target_manifest, candidate_text


def test_stage10_v2_producer_round_trips_through_strict_replay(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, manifest = _sealed_candidate(run_dir)

    replayed = validate_selected_candidate_manifest(run_dir, config)

    assert replayed == manifest
    assert manifest["schema_version"] == 2
    assert manifest["run_config_path"] == "config.yaml"
    assert manifest["config_semantic_sha256"] == semantic_config_sha256(config)
    assert set(manifest["scaffold_files"]) == {"main.py"}
    assert set(manifest["plugin_files"]) == {"detector_plugin.py"}
    assert "generated" not in manifest


def test_stage10_loader_rejects_duplicate_keys_and_unmanifested_symlink(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, manifest = _sealed_candidate(run_dir)
    text = canonical_json_text(manifest).replace(
        '"schema_version": 2,', '"schema_version": 2,\n  "schema_version": 2,', 1
    )
    with pytest.raises(CanonicalExperimentEvidenceError, match="duplicate JSON key"):
        parse_selected_candidate_manifest(text)

    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    (run_dir / "stage-10" / "selected_candidate" / "extra.py").symlink_to(outside)
    with pytest.raises(CanonicalExperimentEvidenceError, match="flat regular-file"):
        validate_selected_candidate_manifest(run_dir, config)


def test_stage10_loader_rejects_semantically_different_active_config(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _manifest = _sealed_candidate(run_dir)
    changed = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    changed["experiment"]["time_budget_sec"] += 1
    changed_config = RCConfig.from_dict(changed, project_root=run_dir, check_paths=False)

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="active config semantic generation differs",
    ):
        validate_selected_candidate_manifest(run_dir, changed_config)
    assert semantic_config_sha256(changed_config) != semantic_config_sha256(config)


def test_stage10_failed_reseal_removes_stale_canonical_pair(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config = _config(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    contract_path = stage9 / "experiment_contract.yaml"
    dump_contract(derive_contract(config, {"datasets": ["synthetic"]}), contract_path)
    stage10 = run_dir / "stage-10"
    selected = stage10 / "selected_candidate"
    selected.mkdir(parents=True)
    (selected / "stale.py").write_text("stale = True\n", encoding="utf-8")
    manifest = stage10 / "selected_candidate_manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    experiment = stage10 / "experiment"
    experiment.mkdir()
    (experiment / "helper.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing required main.py"):
        _seal_selected_candidate(stage10, experiment, contract_path, config)
    assert not selected.exists()
    assert not manifest.exists()


def test_invocation_journal_accepts_only_single_started_terminal_pair() -> None:
    started = {
        "schema_version": 1,
        "event": "started",
        "ordinal": 1,
        "invocation_token": "2" * 64,
        "generation_binding_sha256": SHA,
        "experiment_contract_sha256": SHA,
        "sealed_candidate_manifest_sha256": SHA,
        "config_semantic_sha256": SHA,
    }
    terminal = {
        "schema_version": 1,
        "event": "terminal",
        "ordinal": 1,
        "invocation_token": "2" * 64,
        "status": "completed",
        "result_path": "stage-12/evidence-v1/run-1.json",
        "result_sha256": SHA,
        "failure_code": None,
    }
    text = json.dumps(started) + "\n" + json.dumps(terminal) + "\n"
    assert len(parse_execution_invocation_journal(text)) == 2

    with pytest.raises(CanonicalExperimentEvidenceError, match="record count"):
        parse_execution_invocation_journal(text + json.dumps(terminal) + "\n")
    with pytest.raises(CanonicalExperimentEvidenceError, match="end with one newline"):
        parse_execution_invocation_journal(text.rstrip("\n"))


def test_generation_binding_and_single_invocation_aggregate_are_replayable(
    tmp_path: Path,
) -> None:
    binding = invocation_generation_binding_sha256(
        experiment_contract_sha256=SHA,
        sealed_candidate_manifest_sha256="2" * 64,
        config_semantic_sha256="3" * 64,
        experiment_mode="sandbox",
        evaluator_schema="hpc_anomaly_detection_v1",
    )
    assert len(binding) == 64
    config = _config(tmp_path / "config-run")
    contract = derive_contract(config, {"datasets": ["synthetic"]})
    structured_result = _hpc_structured_result()
    normalized_metrics = _normalized_hpc_metrics(structured_result)
    invocation = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_observations": normalized_metrics,
        "structured_results": structured_result,
    }
    aggregate = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "source_ordinals": [1],
        "metric_observations": normalized_metrics,
        "structured_results": structured_result,
    }
    invocation_text = canonical_json_text(invocation)
    aggregate_text = canonical_json_text(aggregate)
    assert parse_invocation_result(invocation_text) == invocation
    assert parse_aggregate_results(aggregate_text) == aggregate
    assert validate_single_invocation_aggregate(
        invocation_text, aggregate_text, contract=contract
    ) == (
        invocation,
        aggregate,
    )
    aggregate["metric_observations"]["detection_f1"] = [0.6]
    with pytest.raises(CanonicalExperimentEvidenceError, match="aggregate metric"):
        validate_single_invocation_aggregate(
            invocation_text, canonical_json_text(aggregate), contract=contract
        )


def test_stage12_stage13_candidate_and_root_schemas_round_trip() -> None:
    common = {
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": SHA,
        "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
        "sealed_candidate_manifest_sha256": SHA,
        "run_config_path": "config.yaml",
        "run_config_sha256": SHA,
        "config_semantic_policy_version": 1,
        "config_semantic_sha256": SHA,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_authority": METRIC_AUTHORITY,
    }
    stage12 = {
        "schema_version": 1,
        "result_set_policy_version": 1,
        "result_set_type": "stage12_baseline",
        "experiment_mode": "sandbox",
        **common,
        "invocation_journal": {
            "path": "stage-12/execution_invocation_journal.jsonl",
            "sha256": SHA,
        },
        "execution_statuses": [{
            "ordinal": 1,
            "status": "completed",
            "result_path": "stage-12/evidence-v1/run-1.json",
            "failure_code": None,
        }],
        "evidence_files": [
            {"path": "stage-12/evidence-v1/results.json", "sha256": SHA},
            {"path": "stage-12/evidence-v1/run-1.json", "sha256": SHA},
        ],
    }
    assert parse_experiment_result_set(canonical_json_text(stage12)) == stage12

    stage13 = {
        "schema_version": 1,
        "refinement_policy_version": 1,
        "result_set_type": "stage13_refinement",
        "baseline_manifest": {"path": "stage-12/experiment_result_set.json", "sha256": SHA},
        **common,
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "iterations": [],
        "refinement_log": {"path": "stage-13/refinement_log.json", "sha256": SHA},
        "selected_result": {"type": "baseline", "iteration_id": None},
    }
    assert parse_refinement_result_set(canonical_json_text(stage13)) == stage13

    identity = {
        "candidate_identity_policy_version": 1,
        "selected_result_type": "stage12_baseline",
        "selected_result_manifest_sha256": SHA,
        "experiment_contract_sha256": SHA,
        "config_semantic_sha256": SHA,
        "metric_authority_selector_input_sha256": METRIC_AUTHORITY["selector_input_sha256"],
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "artifacts": [
            {"role": "analysis", "logical_name": "analysis.md", "sha256": SHA},
            {"role": "figure_plan", "logical_name": "figure_plan.json", "sha256": SHA},
            {"role": "results_table", "logical_name": "results_table.tex", "sha256": SHA},
            {"role": "summary", "logical_name": "experiment_summary.json", "sha256": SHA},
        ],
    }
    identity_sha = sha256_text(canonical_json_text(identity))
    candidate = {
        "schema_version": 1,
        "candidate_policy_version": 1,
        "candidate_id": "cand-" + identity_sha,
        "identity_payload": identity,
        "identity_payload_sha256": identity_sha,
        "selected_result": {
            "result_set_type": "stage12_baseline",
            "manifest_path": "stage-12/experiment_result_set.json",
            "manifest_sha256": SHA,
        },
        **common,
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "primary_metric_value": "0.8",
        "artifacts": [
            {"role": "analysis", "path": "analysis.md", "sha256": SHA},
            {"role": "figure_plan", "path": "figure_plan.json", "sha256": SHA},
            {"role": "results_table", "path": "results_table.tex", "sha256": SHA},
            {"role": "summary", "path": "experiment_summary.json", "sha256": SHA},
        ],
    }
    assert parse_experiment_evidence_candidate(canonical_json_text(candidate)) == candidate

    root = {
        "schema_version": 1,
        "selection_policy_version": 1,
        "selected_result": candidate["selected_result"],
        **common,
        "primary_metric": "detection_f1",
        "optimization_direction": "maximize",
        "selected_candidate": {
            "candidate_id": candidate["candidate_id"],
            "path": f"stage-14/evidence_candidates/{candidate['candidate_id']}/experiment_evidence_candidate.json",
            "sha256": SHA,
        },
        "selected_summary": {
            "source_path": f"stage-14/evidence_candidates/{candidate['candidate_id']}/experiment_summary.json",
            "source_sha256": SHA,
            "canonical_path": "experiment_summary_best.json",
            "canonical_sha256": SHA,
        },
        "selected_analysis": {
            "source_path": f"stage-14/evidence_candidates/{candidate['candidate_id']}/analysis.md",
            "source_sha256": SHA,
            "canonical_path": "analysis_best.md",
            "canonical_sha256": SHA,
        },
    }
    assert parse_canonical_experiment_manifest(canonical_json_text(root)) == root


def test_run_level_replay_closes_stage12_stage13_and_root_bundle(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, root = _write_canonical_bundle(run_dir)

    assert validate_experiment_result_set(run_dir, config)["result_set_type"] == "stage12_baseline"
    assert validate_refinement_result_set(run_dir, config)["selected_result"]["type"] == "baseline"
    assert validate_canonical_experiment_manifest(run_dir, config) == root

    (run_dir / "stage-12/evidence-v1/run-1.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(CanonicalExperimentEvidenceError, match="hash mismatch"):
        validate_experiment_result_set(run_dir, config)


def test_stage13_producer_publishes_replayable_baseline_without_llm(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)

    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=None,
    )

    assert result.status is StageStatus.DONE
    manifest = validate_refinement_result_set(run_dir, config)
    assert manifest["iterations"] == []
    assert manifest["selected_result"] == {"type": "baseline", "iteration_id": None}
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert (run_dir / "stage-13_v1").is_dir()
    assert not (run_dir / "stage-13_v1/refinement_result_set.json").exists()
    assert (run_dir / "stage-13/experiment_final/main.py").read_bytes() == (
        run_dir / "stage-10/selected_candidate/main.py"
    ).read_bytes()


def test_stage13_producer_selects_replayed_improving_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)

    class SequenceLLM:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            self.calls += 1
            return SimpleNamespace(content=(
                "```filename:detector_plugin.py\n"
                "class DetectorPlugin:\n"
                "    def fit(self, X, y):\n"
                "        return self\n"
                "    def predict(self, X):\n"
                f"        # iteration {self.calls}\n"
                f"        return [{self.calls % 2}] * len(X)\n"
                "```"
            ))

    metrics = iter((0.8, 0.7, 0.6))

    class FakeSandbox:
        backend_kind = "subprocess"

        def __init__(self, root: Path) -> None:
            self.root = root

        def run_project(self, project_dir: Path, *, timeout_sec: int) -> SimpleNamespace:
            del project_dir, timeout_sec
            output = self.root / "run-output"
            output.mkdir()
            (output / "results.json").write_text(
                canonical_json_text(_hpc_structured_result(next(metrics))),
                encoding="utf-8",
            )
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                output_dir=output,
            )

    from researchclaw.experiment import factory

    monkeypatch.setattr(
        factory,
        "create_sandbox",
        lambda _config, root, metadata_dir=None: FakeSandbox(root),
    )
    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=SequenceLLM(),
    )

    assert result.status is StageStatus.DONE
    manifest = validate_refinement_result_set(run_dir, config)
    assert [item["iteration_id"] for item in manifest["iterations"]] == [
        "iter-1", "iter-2", "iter-3"
    ]
    assert manifest["selected_result"] == {
        "type": "iteration",
        "iteration_id": "iter-1",
    }
    assert manifest["iterations"][0]["primary_metric_observation"] == Decimal("0.8")
    assert (run_dir / "stage-13/experiment_final/detector_plugin.py").read_text(
        encoding="utf-8"
    ).endswith("return [1] * len(X)")


def test_stage13_rejects_model_attempt_to_replace_scaffold_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)

    class MainReplacingLLM:
        def chat(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            return SimpleNamespace(content="```filename:main.py\nprint('replacement')\n```")

    from researchclaw.experiment import factory

    def forbidden_sandbox(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("sandbox must not run for an ownership violation")

    monkeypatch.setattr(factory, "create_sandbox", forbidden_sandbox)
    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=MainReplacingLLM(),
    )

    assert result.status is StageStatus.FAILED
    assert "scaffold-owned" in (result.error or "")
    assert not (run_dir / "stage-13/refinement_result_set.json").exists()
    assert not (run_dir / "stage-13/evidence-v1").exists()
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_stage13_prepublication_replay_failure_leaves_no_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    from researchclaw.pipeline.stage_impls import _execution as execution_impl

    monkeypatch.setattr(
        execution_impl,
        "validate_refinement_result_set",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CanonicalExperimentEvidenceError("forced replay failure")
        ),
    )
    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=None,
    )

    assert result.status is StageStatus.FAILED
    assert "forced replay failure" in (result.error or "")
    assert not (run_dir / "stage-13/refinement_result_set.json").exists()
    assert not (run_dir / "stage-13/evidence-v1").exists()
    assert not (run_dir / "stage-13/experiment_final").exists()


def test_stage13_invalidates_old_authority_before_baseline_preflight(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    (run_dir / "stage-12/experiment_result_set.json").unlink()

    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=None,
    )

    assert result.status is StageStatus.FAILED
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert not (run_dir / "experiment_summary_best.json").exists()
    assert not (run_dir / "analysis_best.md").exists()
    assert not (run_dir / "stage-13/refinement_result_set.json").exists()


def test_stage13_generation_uses_shared_experiment_evidence_lock(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stage13 = run_dir / "stage-13"
    first = CanonicalRefinementController.prepare_generation(run_dir, stage13)
    try:
        with pytest.raises(RuntimeError, match="generation_locked"):
            CanonicalRefinementController.prepare_generation(run_dir, stage13)
    finally:
        first.close()


def test_stage13_execute_stage_invalidates_before_missing_baseline_preflight(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    (run_dir / "stage-12/experiment_result_set.json").unlink()

    result = execute_stage(
        Stage.ITERATIVE_REFINE,
        run_dir=run_dir,
        run_id="stage13-missing-baseline",
        config=config,
        adapters=AdapterBundle(),
    )

    assert result.status is StageStatus.FAILED
    assert "preflight failed" in (result.error or "")
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert not (run_dir / "stage-13/refinement_result_set.json").exists()


def test_stage13_generation_rejects_symlink_without_touching_target(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    victim = external / "refinement_result_set.json"
    victim.write_text("victim\n", encoding="utf-8")
    (run_dir / "stage-13").symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="stage13_directory_unsafe"):
        CanonicalRefinementController.prepare_generation(
            run_dir, run_dir / "stage-13"
        )

    assert victim.read_text(encoding="utf-8") == "victim\n"


def test_stage13_replay_rejects_forged_direction_and_winner(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    _add_refinement_iteration(
        run_dir,
        metric=0.9,
        accepted=True,
        rejection_codes=[],
        primary_metric_observation=0.9,
    )
    manifest_path = run_dir / "stage-13/refinement_result_set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["optimization_direction"] = "minimize"
    manifest["selected_result"] = {"type": "baseline", "iteration_id": None}
    manifest_path.write_text(canonical_json_text(manifest), encoding="utf-8")

    with pytest.raises(CanonicalExperimentEvidenceError, match="direction differs"):
        validate_refinement_result_set(run_dir, config)


@pytest.mark.parametrize(
    "response",
    [
        "```python\nclass DetectorPlugin:\n    pass\n```",
        "class DetectorPlugin:\n    pass\n",
        (
            "```python\nclass DetectorPlugin:\n    pass\n```\n"
            "```python\nprint('second')\n```"
        ),
        (
            "PROSE OUTSIDE FENCE\n"
            "```filename:detector_plugin.py\nclass DetectorPlugin:\n    pass\n```"
        ),
    ],
)
def test_stage13_rejects_noncanonical_model_response_grammar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
    response: str,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)

    class InvalidLLM:
        def chat(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            return SimpleNamespace(content=response)

    from researchclaw.experiment import factory

    def forbidden_sandbox(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("invalid response must not reach sandbox")

    monkeypatch.setattr(factory, "create_sandbox", forbidden_sandbox)
    result = _execute_iterative_refine(
        run_dir / "stage-13",
        run_dir,
        config,
        AdapterBundle(),
        llm=InvalidLLM(),
    )

    assert result.status is StageStatus.FAILED
    assert not (run_dir / "stage-13/refinement_result_set.json").exists()
    assert not (run_dir / "stage-13/evidence-v1").exists()


def test_stage13_replay_rejects_tampered_compatibility_copy(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    (run_dir / "stage-13/experiment_final/main.py").write_text(
        "print('tampered')\n", encoding="utf-8"
    )

    with pytest.raises(CanonicalExperimentEvidenceError, match="compatibility copy mismatch"):
        validate_refinement_result_set(run_dir, config)


def test_stage14_publishes_deterministic_immutable_candidate_and_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"

    first = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert first.status is StageStatus.DONE
    root = validate_canonical_experiment_manifest(run_dir, config)
    candidate_id = root["selected_candidate"]["candidate_id"]
    candidate_root = stage_dir / "evidence_candidates" / candidate_id
    assert validate_experiment_evidence_candidate(candidate_root)["candidate_id"] == candidate_id
    assert not (stage_dir / "analysis.md").exists()
    assert not (stage_dir / "experiment_summary.json").exists()
    assert (candidate_root / "figure_plan.json").is_file()
    assert not list(stage_dir.glob(".candidate-staging-*"))

    second = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert second.status is StageStatus.DONE
    replayed = validate_canonical_experiment_manifest(run_dir, config)
    assert replayed["selected_candidate"]["candidate_id"] == candidate_id
    assert [path.name for path in (stage_dir / "evidence_candidates").iterdir()] == [candidate_id]


def test_shared_accessor_returns_lock_consistent_immutable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    _config, root = _write_canonical_bundle(run_dir)

    evidence = load_canonical_experiment_evidence(run_dir)

    assert evidence.manifest_sha256 == sha256_file(
        run_dir / "canonical_experiment_evidence.json"
    )
    assert evidence.candidate_manifest_sha256 == root["selected_candidate"]["sha256"]
    assert evidence.selected_result_manifest_sha256 == root["selected_result"][
        "manifest_sha256"
    ]
    assert evidence.summary["metrics_summary"]["detection_f1"]["mean"] == Decimal("0.5")
    assert evidence.analysis_text == "Analysis.\n"
    assert {artifact.role for artifact in evidence.artifacts} == {
        "analysis",
        "figure_plan",
        "results_table",
        "summary",
    }
    with pytest.raises(TypeError):
        evidence.summary["new"] = "forbidden"  # type: ignore[index]
    with pytest.raises(TypeError):
        evidence.summary["metrics_summary"]["detection_f1"]["mean"] = Decimal("1")  # type: ignore[index]

    summary_path = run_dir / root["selected_summary"]["source_path"]
    summary_path.write_text("{}\n", encoding="utf-8")
    assert evidence.summary_bytes != summary_path.read_bytes()
    assert evidence.summary["metrics_summary"]["detection_f1"]["mean"] == Decimal("0.5")


def test_shared_accessor_refuses_to_read_during_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    _write_canonical_bundle(run_dir)
    publisher = CanonicalAnalysisController.acquire_promotion(run_dir)
    try:
        with pytest.raises(RuntimeError, match="canonical_evidence_generation_locked"):
            load_canonical_experiment_evidence(run_dir)
    finally:
        publisher.close()


@pytest.mark.parametrize(
    "target_name",
    [
        "root_manifest",
        "candidate_manifest",
        "candidate_summary",
        "selected_result_manifest",
        "selected_execution",
        "experiment_contract",
        "run_config",
        "summary_compatibility_copy",
        "analysis_compatibility_copy",
    ],
)
def test_shared_accessor_rejects_bundle_change_during_final_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    _config, root = _write_canonical_bundle(run_dir)
    from researchclaw.pipeline import canonical_experiment_evidence as evidence_impl

    targets = {
        "root_manifest": run_dir / "canonical_experiment_evidence.json",
        "candidate_manifest": run_dir / root["selected_candidate"]["path"],
        "candidate_summary": run_dir / root["selected_summary"]["source_path"],
        "selected_result_manifest": run_dir / root["selected_result"]["manifest_path"],
        "selected_execution": run_dir / "stage-12/evidence-v1/results.json",
        "experiment_contract": run_dir / root["experiment_contract_path"],
        "run_config": run_dir / root["run_config_path"],
        "summary_compatibility_copy": run_dir / root["selected_summary"]["canonical_path"],
        "analysis_compatibility_copy": run_dir / root["selected_analysis"]["canonical_path"],
    }
    target = targets[target_name]
    original = target.read_bytes()
    real_validate = evidence_impl.validate_canonical_experiment_manifest
    calls = 0

    def mutate_before_final_replay(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_bytes(b"{}\n")
        return real_validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        evidence_impl,
        "validate_canonical_experiment_manifest",
        mutate_before_final_replay,
    )
    with pytest.raises(CanonicalExperimentEvidenceError):
        load_canonical_experiment_evidence(run_dir)
    assert calls == 2

    target.write_bytes(original)
    recovered = load_canonical_experiment_evidence(run_dir)
    assert recovered.manifest_sha256 == sha256_file(
        run_dir / "canonical_experiment_evidence.json"
    )


def test_stage14_llm_perspectives_are_bound_into_candidate_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    object.__setattr__(config.experiment.figure_agent, "enabled", True)
    stage_dir = run_dir / "stage-14"

    class AnalysisLLM:
        def chat(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            return SimpleNamespace(content="# Bound analysis\nCanonical result discussion.\n")

    result = _execute_result_analysis(
        stage_dir,
        run_dir,
        config,
        None,  # type: ignore[arg-type]
        llm=AnalysisLLM(),  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.DONE
    root = validate_canonical_experiment_manifest(run_dir, config)
    candidate_root = run_dir / Path(root["selected_candidate"]["path"]).parent
    candidate = validate_experiment_evidence_candidate(candidate_root)
    perspective_paths = {
        item["path"]
        for item in candidate["artifacts"]
        if item["path"].startswith("perspectives/")
    }
    assert perspective_paths == {
        "perspectives/methodologist.md",
        "perspectives/optimist.md",
        "perspectives/skeptic.md",
    }
    assert all(
        item["role"] == "auxiliary"
        for item in candidate["artifacts"]
        if item["path"] in perspective_paths
    )
    assert not (stage_dir / "perspectives").exists()
    assert not (candidate_root / "charts").exists()
    assert (candidate_root / "figure_plan.json").read_text(
        encoding="utf-8"
    ) == EMPTY_FIGURE_PLAN_TEXT


def test_stage14_rejects_partial_chart_output_instead_of_sealing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    from researchclaw.pipeline.stage_impls import _analysis as analysis_impl

    def partial_renderer(
        staging: Path,
        *_args: object,
        **_kwargs: object,
    ) -> StageResult:
        (staging / "analysis.md").write_text("analysis\n", encoding="utf-8")
        (staging / "experiment_summary.json").write_text("{}\n", encoding="utf-8")
        (staging / "results_table.tex").write_text("table\n", encoding="utf-8")
        charts = staging / "charts"
        charts.mkdir()
        (charts / "unreviewed.png").write_bytes(b"partial")
        return StageResult(
            stage=Stage.RESULT_ANALYSIS,
            status=StageStatus.DONE,
            artifacts=(),
        )

    monkeypatch.setattr(
        analysis_impl,
        "_render_result_analysis_candidate",
        partial_renderer,
    )
    result = analysis_impl._execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "forbids producer-supplied charts" in (result.error or "")
    assert not list((stage_dir / "evidence_candidates").iterdir())
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_stage14_invalidates_old_root_before_missing_upstream_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-14"
    stage_dir.mkdir(parents=True)
    for name in (
        "canonical_experiment_evidence.json",
        "experiment_summary_best.json",
        "analysis_best.md",
    ):
        (run_dir / name).write_text("stale\n", encoding="utf-8")

    result = _execute_result_analysis(
        stage_dir, run_dir, _config(run_dir), None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "Stage 12 result set is missing" in (result.error or "")
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert not (run_dir / "experiment_summary_best.json").exists()
    assert not (run_dir / "analysis_best.md").exists()


def test_stage14_promotion_rejects_tampered_candidate_without_root_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    root = validate_canonical_experiment_manifest(run_dir, config)
    candidate_root = run_dir / Path(root["selected_candidate"]["path"]).parent
    (candidate_root / "analysis.md").write_text("tampered\n", encoding="utf-8")
    (run_dir / "canonical_experiment_evidence.json").unlink()

    controller = CanonicalAnalysisController.acquire_promotion(run_dir)
    try:
        with pytest.raises(CanonicalExperimentEvidenceError, match="artifact hash mismatch"):
            publish_canonical_experiment_manifest(run_dir, config)
    finally:
        controller.close()
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_runner_stage14_promotion_reconstructs_root_from_canonical_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    result = _execute_result_analysis(
        run_dir / "stage-14",
        run_dir,
        config,
        None,
        llm=None,  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    expected = validate_canonical_experiment_manifest(run_dir, config)
    for name in (
        "canonical_experiment_evidence.json",
        "experiment_summary_best.json",
        "analysis_best.md",
    ):
        (run_dir / name).unlink()

    pipeline_runner._promote_best_stage14(run_dir, config)

    assert validate_canonical_experiment_manifest(run_dir, config) == expected


def test_stage14_candidate_publication_rejects_cross_device_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    real_replace = os.replace

    def cross_device_replace(source: object, destination: object) -> None:
        source_path = Path(source)  # type: ignore[arg-type]
        destination_path = Path(destination)  # type: ignore[arg-type]
        if (
            source_path.name.startswith(".candidate-staging-")
            and destination_path.parent == stage_dir / "evidence_candidates"
        ):
            raise OSError(errno.EXDEV, "forced cross-device publication")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", cross_device_replace)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "cross-device Stage 14 candidate publication is forbidden" in (
        result.error or ""
    )
    assert not list((stage_dir / "evidence_candidates").iterdir())
    assert not list(stage_dir.glob(".candidate-staging-*"))
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_stage14_root_publication_interruption_leaves_no_manifest_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    real_replace = os.replace

    def interrupt_analysis_copy(source: object, destination: object) -> None:
        destination_path = Path(destination)  # type: ignore[arg-type]
        if destination_path == run_dir / "analysis_best.md":
            raise OSError("forced compatibility-copy interruption")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", interrupt_analysis_copy)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert len(list((stage_dir / "evidence_candidates").iterdir())) == 1
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="canonical experiment evidence manifest is missing",
    ):
        validate_canonical_experiment_manifest(run_dir, config)


def test_stage14_candidate_post_rename_replay_reads_disk_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    real_replace = os.replace
    corrupt_once = True

    def corrupt_published_candidate(source: object, destination: object) -> None:
        nonlocal corrupt_once
        destination_path = Path(destination)  # type: ignore[arg-type]
        real_replace(source, destination)
        if corrupt_once and destination_path.parent == stage_dir / "evidence_candidates":
            corrupt_once = False
            (destination_path / "experiment_evidence_candidate.json").write_text(
                "{}\n", encoding="utf-8"
            )

    monkeypatch.setattr(os, "replace", corrupt_published_candidate)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "Stage 14 evidence candidate fields mismatch" in (result.error or "")
    assert not (run_dir / "canonical_experiment_evidence.json").exists()
    assert not list((stage_dir / "evidence_candidates").iterdir())

    recovered = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert recovered.status is StageStatus.DONE
    assert len(list((stage_dir / "evidence_candidates").iterdir())) == 1
    validate_canonical_experiment_manifest(run_dir, config)


def test_stage14_candidate_pre_rename_replay_reads_disk_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    real_write_text = Path.write_text

    def corrupt_staged_manifest(
        path: Path,
        data: str,
        *args: object,
        **kwargs: object,
    ) -> int:
        written = real_write_text(path, data, *args, **kwargs)  # type: ignore[arg-type]
        if (
            path.name == "experiment_evidence_candidate.json"
            and path.parent.name.startswith(".candidate-staging-")
        ):
            real_write_text(path, "{}\n", encoding="utf-8")
        return written

    monkeypatch.setattr(Path, "write_text", corrupt_staged_manifest)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "Stage 14 evidence candidate fields mismatch" in (result.error or "")
    assert not list((stage_dir / "evidence_candidates").iterdir())
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_stage14_root_post_replace_replay_reads_disk_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config = _prepare_stage14_upstream(run_dir)
    stage_dir = run_dir / "stage-14"
    real_replace = os.replace

    def corrupt_published_root(source: object, destination: object) -> None:
        destination_path = Path(destination)  # type: ignore[arg-type]
        real_replace(source, destination)
        if destination_path == run_dir / "canonical_experiment_evidence.json":
            destination_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(os, "replace", corrupt_published_root)
    result = _execute_result_analysis(
        stage_dir, run_dir, config, None, llm=None  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "canonical experiment evidence manifest fields mismatch" in (
        result.error or ""
    )
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


def test_stage14_symlinked_generation_does_not_touch_external_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external-stage14"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("external\n", encoding="utf-8")
    (run_dir / "stage-14").symlink_to(external, target_is_directory=True)

    result = _execute_result_analysis(
        run_dir / "stage-14",
        run_dir,
        _config(run_dir),
        None,
        llm=None,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "canonical_stage14_directory_unsafe" in (result.error or "")
    assert marker.read_text(encoding="utf-8") == "external\n"


def test_run_level_replay_rejects_stage13_and_root_copy_tampering(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    (run_dir / "stage-13/refinement_log.json").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(CanonicalExperimentEvidenceError, match="refinement log hash mismatch"):
        validate_refinement_result_set(run_dir, config)

    run_dir = tmp_path / "run-copy"
    config, _root = _write_canonical_bundle(run_dir)
    (run_dir / "analysis_best.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(CanonicalExperimentEvidenceError, match="compatibility copy mismatch"):
        validate_canonical_experiment_manifest(run_dir, config)


def test_stage13_policy_v1_parser_rejects_rejected_iteration(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    _add_refinement_iteration(
        run_dir,
        metric=0.9,
        accepted=False,
        rejection_codes=["producer_says_no"],
        primary_metric_observation=None,
    )

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="accepted must be true under refinement policy v1",
    ):
        validate_refinement_result_set(run_dir, config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "rejection_codes",
            ["producer_says_no"],
            "rejection_codes must be empty under refinement policy v1",
        ),
        (
            "primary_metric_observation",
            "0.9",
            "primary_metric_observation must be a JSON number",
        ),
    ],
)
def test_stage13_policy_v1_parser_rejects_noncanonical_accepted_state(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    run_dir = tmp_path / field
    _config, _root = _write_canonical_bundle(run_dir)
    _add_refinement_iteration(
        run_dir,
        metric=0.9,
        accepted=True,
        rejection_codes=[],
        primary_metric_observation=0.9,
    )
    manifest = json.loads(
        (run_dir / "stage-13/refinement_result_set.json").read_text(encoding="utf-8")
    )
    manifest["iterations"][0][field] = value

    with pytest.raises(CanonicalExperimentEvidenceError, match=message):
        parse_refinement_result_set(canonical_json_text(manifest))


def test_stage13_rejects_replacement_of_scaffold_owned_evaluator(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    _add_refinement_iteration(
        run_dir,
        metric=0.9,
        accepted=True,
        rejection_codes=[],
        primary_metric_observation=0.9,
    )
    manifest_path = run_dir / "stage-13/refinement_result_set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["iterations"][0]
    main_path = run_dir / "stage-13/evidence-v1/iterations/iter-1/project/main.py"
    main_path.write_text("print('refined evaluator')\n", encoding="utf-8")
    next(ref for ref in item["project_files"] if ref["path"].endswith("/main.py"))[
        "sha256"
    ] = sha256_file(main_path)
    validation_path = run_dir / item["validation_report"]["path"]
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["project_files_sha256"] = sha256_text(canonical_json_text(item["project_files"]))
    validation_text = canonical_json_text(validation)
    validation_path.write_text(validation_text, encoding="utf-8")
    item["validation_report"]["sha256"] = sha256_text(validation_text)
    manifest["selected_result"] = {"type": "iteration", "iteration_id": "iter-1"}
    manifest_path.write_text(canonical_json_text(manifest), encoding="utf-8")

    with pytest.raises(CanonicalExperimentEvidenceError, match="scaffold-owned evaluator"):
        validate_refinement_result_set(run_dir, config)


def test_stage12_rejects_normalized_metric_divergent_from_evaluator_result(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    run_path = run_dir / "stage-12/evidence-v1/run-1.json"
    aggregate_path = run_dir / "stage-12/evidence-v1/results.json"
    run_payload = json.loads(run_path.read_text(encoding="utf-8"))
    evaluator = run_payload["structured_results"]
    evaluator["primary_metric"]["value"] = 0.0
    evaluator["metrics"]["detection_f1"] = 0.0
    for per_seed in evaluator["per_seed"]:
        per_seed["metrics"]["detection_f1"] = 0.0
    run_payload["metric_observations"]["detection_f1"] = [0.9]
    aggregate_payload = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": run_payload["evaluator_schema"],
        "source_ordinals": [1],
        "metric_observations": run_payload["metric_observations"],
        "structured_results": evaluator,
    }
    run_text = canonical_json_text(run_payload)
    aggregate_text = canonical_json_text(aggregate_payload)
    run_path.write_text(run_text, encoding="utf-8")
    aggregate_path.write_text(aggregate_text, encoding="utf-8")

    journal_path = run_dir / "stage-12/execution_invocation_journal.jsonl"
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    records[1]["result_sha256"] = sha256_text(run_text)
    journal_text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in records)
    journal_path.write_text(journal_text, encoding="utf-8")
    result_set_path = run_dir / "stage-12/experiment_result_set.json"
    result_set = json.loads(result_set_path.read_text(encoding="utf-8"))
    result_set["invocation_journal"]["sha256"] = sha256_text(journal_text)
    refs = {ref["path"]: ref for ref in result_set["evidence_files"]}
    refs["stage-12/evidence-v1/run-1.json"]["sha256"] = sha256_text(run_text)
    refs["stage-12/evidence-v1/results.json"]["sha256"] = sha256_text(aggregate_text)
    result_set_path.write_text(canonical_json_text(result_set), encoding="utf-8")

    with pytest.raises(CanonicalExperimentEvidenceError, match="normalized metrics differ"):
        validate_experiment_result_set(run_dir, config)


def test_stage12_decimal_authority_does_not_collapse_adjacent_values(tmp_path: Path) -> None:
    config = _config(tmp_path / "run")
    contract = derive_contract(config, {"datasets": ["synthetic"]})
    evaluator = _hpc_structured_result()
    for item in evaluator["per_seed"]:
        item["metrics"]["detection_f1"] = "__EVALUATOR_DECIMAL__"
    evaluator["metrics"]["detection_f1"] = "__EVALUATOR_DECIMAL__"
    evaluator["primary_metric"]["value"] = "__EVALUATOR_DECIMAL__"
    invocation = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_observations": {"detection_f1": ["__WRAPPER_DECIMAL__"]},
        "structured_results": evaluator,
    }
    aggregate = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": invocation["evaluator_schema"],
        "source_ordinals": [1],
        "metric_observations": invocation["metric_observations"],
        "structured_results": evaluator,
    }

    def authority_text(payload: object) -> str:
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            .replace('"__EVALUATOR_DECIMAL__"', "0.123456789012345678")
            .replace('"__WRAPPER_DECIMAL__"', "0.123456789012345679")
        )

    with pytest.raises(CanonicalExperimentEvidenceError, match="normalized metrics differ"):
        validate_single_invocation_aggregate(
            authority_text(invocation), authority_text(aggregate), contract=contract
        )


def test_stage12_primary_metric_is_independent_of_global_decimal_context(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    evidence = run_dir / "stage-12/evidence-v1"
    evidence.mkdir(parents=True)
    observation = "0.12345678901234567890123456789012345678901234567890"
    aggregate_text = (
        '{"schema_version":1,"aggregation_policy_version":1,'
        '"evaluator_schema":"hpc_anomaly_detection_v1","source_ordinals":[1],'
        f'"metric_observations":{{"detection_f1":[{observation}]}},'
        '"structured_results":{}}'
    )
    (evidence / "results.json").write_text(aggregate_text, encoding="utf-8")
    result_set = {"evaluator_schema": "hpc_anomaly_detection_v1"}
    original_precision = getcontext().prec
    try:
        results = []
        for precision in (7, 28, 80):
            getcontext().prec = precision
            results.append(_stage12_primary_metric(run_dir, result_set, "detection_f1"))
    finally:
        getcontext().prec = original_precision

    assert results == [Decimal(observation)] * 3


def test_scaffold_decimal_output_replays_under_authority_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "run")
    contract = derive_contract(config, {"datasets": ["synthetic"]})
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(render_main_py(contract), encoding="utf-8")
    (project / "detector_plugin.py").write_text(
        """import numpy as np

class DetectorPlugin:
    def fit(self, X, y):
        return self

    def predict(self, X):
        return (np.asarray(X)[:, 2] > 0.8).astype(int)
""",
        encoding="utf-8",
    )
    source = (project / "main.py").read_text(encoding="utf-8")
    assert "time.perf_counter_ns()" in source
    assert "time.perf_counter()" not in source
    monkeypatch.syspath_prepend(str(project))
    spec = importlib.util.spec_from_file_location("decimal_scaffold_main", project / "main.py")
    assert spec is not None and spec.loader is not None
    scaffold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scaffold)
    score = scaffold._score([1, 0, 0], [1, 1, 1])
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        expected_precision = Decimal(1) / Decimal(3)
    assert score["precision"] == expected_precision
    assert isinstance(score["precision"], Decimal)
    with pytest.raises(ValueError, match="predict returned non-finite values"):
        scaffold._as_binary_predictions(
            [float("nan"), float("inf"), float("-inf")], 3
        )
    with pytest.raises(TypeError, match="unsupported JSON value: float"):
        scaffold._json_text(0.5)

    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    structured_text = (project / "results.json").read_text(encoding="utf-8").strip()
    structured = json.loads(structured_text, parse_float=Decimal)
    metric = format(structured["metrics"]["detection_f1"], "f")
    invocation_text = (
        '{"schema_version":1,"invocation_policy_version":1,"ordinal":1,'
        '"status":"completed","evaluator_schema":"hpc_anomaly_detection_v1",'
        f'"metric_observations":{{"detection_f1":[{metric}]}},'
        f'"structured_results":{structured_text}}}'
    )
    aggregate_text = (
        '{"schema_version":1,"aggregation_policy_version":1,'
        '"evaluator_schema":"hpc_anomaly_detection_v1","source_ordinals":[1],'
        f'"metric_observations":{{"detection_f1":[{metric}]}},'
        f'"structured_results":{structured_text}}}'
    )

    validate_single_invocation_aggregate(
        invocation_text, aggregate_text, contract=contract
    )


def test_scaffold_nonfinite_plugin_predictions_publish_no_results(tmp_path: Path) -> None:
    config = _config(tmp_path / "run")
    contract = derive_contract(config, {"datasets": ["synthetic"]})
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(render_main_py(contract), encoding="utf-8")
    (project / "detector_plugin.py").write_text(
        """import numpy as np

class DetectorPlugin:
    def fit(self, X, y):
        return self

    def predict(self, X):
        values = np.zeros(len(X), dtype=float)
        values[:3] = [np.nan, np.inf, -np.inf]
        return values
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "predict returned non-finite values" in completed.stderr
    assert not (project / "results.json").exists()


def test_stage13_validation_report_is_strict_and_cross_role_alias_is_rejected(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": 1,
        "validation_policy_version": 1,
        "project_files_sha256": SHA,
        "checks": {"python_syntax_valid": True},
    }
    assert parse_refinement_validation_report(canonical_json_text(report)) == report
    report["valid"] = True
    with pytest.raises(CanonicalExperimentEvidenceError, match="fields mismatch"):
        parse_refinement_validation_report(canonical_json_text(report))

    run_dir = tmp_path / "alias"
    _config, _root = _write_canonical_bundle(run_dir)
    manifest_path = run_dir / "stage-13/refinement_result_set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shared = {
        "path": "stage-13/evidence-v1/iterations/iter-1/validation_report.json",
        "sha256": SHA,
    }
    manifest["iterations"] = [{
        "ordinal": 1,
        "iteration_id": "iter-1",
        "project_files": [shared],
        "validation_report": shared,
        "initial_execution": {
            "path": "stage-13/evidence-v1/iterations/iter-1/initial_execution.json",
            "sha256": SHA,
        },
        "runtime_repair": None,
        "accepted": False,
        "rejection_codes": ["python_syntax_invalid"],
        "primary_metric_observation": None,
    }]
    with pytest.raises(CanonicalExperimentEvidenceError, match="project path|path alias"):
        parse_refinement_result_set(canonical_json_text(manifest))


def test_stage13_policy_v1_parser_rejects_runtime_repair(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    _add_refinement_iteration(
        run_dir,
        metric=0.9,
        accepted=True,
        rejection_codes=[],
        primary_metric_observation=0.9,
    )
    manifest_path = run_dir / "stage-13/refinement_result_set.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = manifest["iterations"][0]
    repair_root = run_dir / "stage-13/evidence-v1/iterations/iter-1/runtime_repair"
    repair_project_root = repair_root / "project"
    repair_project_root.mkdir(parents=True)
    for name in ("main.py", "detector_plugin.py"):
        (repair_project_root / name).write_bytes(
            (run_dir / "stage-10/selected_candidate" / name).read_bytes()
        )
    repair_refs = [
        {
            "path": f"stage-13/evidence-v1/iterations/iter-1/runtime_repair/project/{name}",
            "sha256": sha256_file(repair_project_root / name),
        }
        for name in ("detector_plugin.py", "main.py")
    ]
    repair_validation = {
        "schema_version": 1,
        "validation_policy_version": 1,
        "project_files_sha256": sha256_text(canonical_json_text(repair_refs)),
        "checks": {"python_syntax_valid": True},
    }
    repair_validation_text = canonical_json_text(repair_validation)
    repair_validation_path = repair_root / "validation_report.json"
    repair_validation_path.write_text(repair_validation_text, encoding="utf-8")
    initial_execution = run_dir / item["initial_execution"]["path"]
    repair_execution_path = repair_root / "execution_result.json"
    repair_execution_path.write_bytes(initial_execution.read_bytes())
    item["runtime_repair"] = {
        "project_files": repair_refs,
        "execution_result": {
            "path": "stage-13/evidence-v1/iterations/iter-1/runtime_repair/execution_result.json",
            "sha256": sha256_file(repair_execution_path),
        },
        "validation_report": {
            "path": "stage-13/evidence-v1/iterations/iter-1/runtime_repair/validation_report.json",
            "sha256": sha256_text(repair_validation_text),
        },
    }
    manifest_path.write_text(canonical_json_text(manifest), encoding="utf-8")

    manifest_text = canonical_json_text(manifest)
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="runtime_repair must be null under refinement policy v1",
    ):
        parse_refinement_result_set(manifest_text)
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="runtime_repair must be null under refinement policy v1",
    ):
        validate_refinement_result_set(run_dir, config, manifest_text)


def test_root_replay_rejects_stored_candidate_that_is_not_tie_break_winner(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, root = _write_canonical_bundle(run_dir)
    clone, clone_manifest, clone_text = _clone_candidate(
        run_dir, root, analysis_text="Alternative analysis.\n"
    )
    original_manifest = run_dir / root["selected_candidate"]["path"]
    original = json.loads(original_manifest.read_text(encoding="utf-8"))
    records = {
        original["candidate_id"]: (original, original_manifest, original_manifest.read_text(encoding="utf-8")),
        clone["candidate_id"]: (clone, clone_manifest, clone_text),
    }
    loser_id = max(records)
    loser, loser_manifest, loser_text = records[loser_id]
    loser_root = loser_manifest.parent
    artifact_map = {item["role"]: item for item in loser["artifacts"]}
    root["selected_candidate"] = {
        "candidate_id": loser_id,
        "path": loser_manifest.relative_to(run_dir).as_posix(),
        "sha256": sha256_text(loser_text),
    }
    for field, role, canonical_name in (
        ("selected_summary", "summary", "experiment_summary_best.json"),
        ("selected_analysis", "analysis", "analysis_best.md"),
    ):
        source = loser_root / artifact_map[role]["path"]
        raw = source.read_bytes()
        (run_dir / canonical_name).write_bytes(raw)
        root[field] = {
            "source_path": source.relative_to(run_dir).as_posix(),
            "source_sha256": artifact_map[role]["sha256"],
            "canonical_path": canonical_name,
            "canonical_sha256": artifact_map[role]["sha256"],
        }
    (run_dir / "canonical_experiment_evidence.json").write_text(
        canonical_json_text(root), encoding="utf-8"
    )

    with pytest.raises(CanonicalExperimentEvidenceError, match="not deterministic winner"):
        validate_canonical_experiment_manifest(run_dir, config)


def test_root_excludes_well_formed_stale_candidate_but_rejects_malformed_entry(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, root = _write_canonical_bundle(run_dir)
    stale, stale_manifest, _stale_text = _clone_candidate(
        run_dir, root, analysis_text="Stale generation analysis.\n"
    )
    baseline_text = (run_dir / "stage-12/experiment_result_set.json").read_text(encoding="utf-8")
    stale["selected_result"] = {
        "result_set_type": "stage12_baseline",
        "manifest_path": "stage-12/experiment_result_set.json",
        "manifest_sha256": sha256_text(baseline_text),
    }
    stale["identity_payload"]["selected_result_type"] = "stage12_baseline"
    stale["identity_payload"]["selected_result_manifest_sha256"] = sha256_text(baseline_text)
    identity_sha = sha256_text(canonical_json_text(stale["identity_payload"]))
    stale["identity_payload_sha256"] = identity_sha
    stale["candidate_id"] = "cand-" + identity_sha
    stale_root = stale_manifest.parent
    canonical_stale_root = stale_root.parent / stale["candidate_id"]
    stale_root.rename(canonical_stale_root)
    (canonical_stale_root / "experiment_evidence_candidate.json").write_text(
        canonical_json_text(stale), encoding="utf-8"
    )

    assert validate_canonical_experiment_manifest(run_dir, config) == root

    (run_dir / "stage-14_v1/evidence_candidates/not-a-candidate").mkdir()
    with pytest.raises(CanonicalExperimentEvidenceError, match="noncanonical entry"):
        validate_canonical_experiment_manifest(run_dir, config)


def test_stage10_rejects_arbitrary_main_claimed_by_scaffold(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config = _config(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    contract = derive_contract(config, {"datasets": ["synthetic"]})
    contract_path = stage9 / "experiment_contract.yaml"
    dump_contract(contract, contract_path)
    experiment = run_dir / "stage-10/experiment"
    experiment.mkdir(parents=True)
    (experiment / "main.py").write_text("print('model evaluator')\n", encoding="utf-8")
    (experiment / "detector_plugin.py").write_text("def predict(X): return []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs from canonical renderer"):
        _seal_selected_candidate(run_dir / "stage-10", experiment, contract_path, config)


def test_stage10_replays_original_snapshot_after_same_semantic_resume(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config, _manifest = _sealed_candidate(run_dir)
    resumed = run_dir / "config.resumed-20260713-120000.yaml"
    resumed.write_text((run_dir / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    write_active_config_binding(run_dir, resumed)

    replayed = validate_selected_candidate_manifest(run_dir, config)
    assert replayed["run_config_path"] == "config.yaml"


def test_candidate_rejects_missing_role_and_same_path_for_different_roles(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _config, root = _write_canonical_bundle(run_dir)
    candidate_path = run_dir / root["selected_candidate"]["path"]
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))

    missing = json.loads(json.dumps(payload))
    missing["identity_payload"]["artifacts"] = [
        item for item in missing["identity_payload"]["artifacts"]
        if item["role"] != "figure_plan"
    ]
    with pytest.raises(CanonicalExperimentEvidenceError, match="mandatory role"):
        parse_experiment_evidence_candidate(canonical_json_text(missing))

    duplicate = json.loads(json.dumps(payload))
    artifacts = duplicate["identity_payload"]["artifacts"]
    summary_path = next(item["logical_name"] for item in artifacts if item["role"] == "summary")
    next(item for item in artifacts if item["role"] == "analysis")["logical_name"] = summary_path
    with pytest.raises(CanonicalExperimentEvidenceError, match="duplicate identity artifacts path"):
        parse_experiment_evidence_candidate(canonical_json_text(duplicate))


@pytest.mark.parametrize(
    ("role", "path"),
    [
        ("chart", "charts/shadow.png"),
        ("auxiliary", "figure_plan_final.json"),
        ("auxiliary", "intermediate/figure_plan.json"),
        ("auxiliary", "intermediate/charts/shadow.png"),
    ],
)
def test_candidate_policy_v1_loader_rejects_figure_agent_artifacts(
    tmp_path: Path,
    role: str,
    path: str,
) -> None:
    run_dir = tmp_path / "run"
    _config, root = _write_canonical_bundle(run_dir)
    candidate_path = run_dir / root["selected_candidate"]["path"]
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    artifact_sha = "a" * 64
    payload["identity_payload"]["artifacts"].append(
        {"role": role, "logical_name": path, "sha256": artifact_sha}
    )
    payload["identity_payload"]["artifacts"].sort(
        key=lambda item: (item["role"], item["logical_name"])
    )
    payload["artifacts"].append({"role": role, "path": path, "sha256": artifact_sha})
    payload["artifacts"].sort(key=lambda item: (item["role"], item["path"]))

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="invalid candidate auxiliary role/path",
    ):
        parse_experiment_evidence_candidate(canonical_json_text(payload))


def test_stage14_promotion_rejects_manifest_bound_chart_under_policy_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_complete_capabilities(monkeypatch)
    run_dir = tmp_path / "run"
    config, root = _write_canonical_bundle(run_dir)
    manifest_path = run_dir / root["selected_candidate"]["path"]
    candidate_root = manifest_path.parent
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    charts = candidate_root / "charts"
    charts.mkdir()
    chart = charts / "shadow.png"
    chart.write_bytes(b"shadow chart")
    chart_sha = sha256_file(chart)
    payload["identity_payload"]["artifacts"].append(
        {"role": "chart", "logical_name": "charts/shadow.png", "sha256": chart_sha}
    )
    payload["identity_payload"]["artifacts"].sort(
        key=lambda item: (item["role"], item["logical_name"])
    )
    payload["artifacts"].append(
        {"role": "chart", "path": "charts/shadow.png", "sha256": chart_sha}
    )
    payload["artifacts"].sort(key=lambda item: (item["role"], item["path"]))
    identity_sha = sha256_text(canonical_json_text(payload["identity_payload"]))
    payload["identity_payload_sha256"] = identity_sha
    payload["candidate_id"] = "cand-" + identity_sha
    manifest_path.write_text(canonical_json_text(payload), encoding="utf-8")
    renamed_root = candidate_root.with_name(payload["candidate_id"])
    candidate_root.rename(renamed_root)
    (run_dir / "canonical_experiment_evidence.json").unlink()

    controller = CanonicalAnalysisController.acquire_promotion(run_dir)
    try:
        with pytest.raises(
            CanonicalExperimentEvidenceError,
            match="invalid candidate auxiliary role/path",
        ):
            publish_canonical_experiment_manifest(run_dir, config)
    finally:
        controller.close()
    assert not (run_dir / "canonical_experiment_evidence.json").exists()


@pytest.mark.parametrize("bad", [True, False, 1.0, "1"])
def test_integer_schema_fields_reject_bool_float_and_string(bad: object) -> None:
    aggregate = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "source_ordinals": [bad],
        "metric_observations": {"detection_f1": [0.5]},
        "structured_results": {},
    }
    with pytest.raises(CanonicalExperimentEvidenceError):
        parse_aggregate_results(canonical_json_text(aggregate))


def test_candidate_file_closure_and_identity_tokens_are_default_deny(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    artifact = root / "experiment_summary.json"
    artifact.write_text("{}\n", encoding="utf-8")
    artifact_sha = sha256_text("{}\n")
    (root / "analysis.md").write_text("{}\n", encoding="utf-8")
    (root / "figure_plan.json").write_text(
        EMPTY_FIGURE_PLAN_TEXT, encoding="utf-8"
    )
    (root / "results_table.tex").write_text("{}\n", encoding="utf-8")
    figure_plan_sha = sha256_text(EMPTY_FIGURE_PLAN_TEXT)
    identity = {
        "candidate_identity_policy_version": 1,
        "selected_result_type": "stage12_baseline",
        "selected_result_manifest_sha256": SHA,
        "experiment_contract_sha256": SHA,
        "config_semantic_sha256": SHA,
        "metric_authority_selector_input_sha256": METRIC_AUTHORITY["selector_input_sha256"],
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "artifacts": [
            {"role": "analysis", "logical_name": "analysis.md", "sha256": artifact_sha},
            {"role": "figure_plan", "logical_name": "figure_plan.json", "sha256": figure_plan_sha},
            {"role": "results_table", "logical_name": "results_table.tex", "sha256": artifact_sha},
            {"role": "summary", "logical_name": "experiment_summary.json", "sha256": artifact_sha},
        ],
    }
    identity_sha = sha256_text(canonical_json_text(identity))
    payload = {
        "schema_version": 1,
        "candidate_policy_version": 1,
        "candidate_id": "cand-" + identity_sha,
        "identity_payload": identity,
        "identity_payload_sha256": identity_sha,
        "selected_result": {
            "result_set_type": "stage12_baseline",
            "manifest_path": "stage-12/experiment_result_set.json",
            "manifest_sha256": SHA,
        },
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": SHA,
        "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
        "sealed_candidate_manifest_sha256": SHA,
        "run_config_path": "config.yaml",
        "run_config_sha256": SHA,
        "config_semantic_policy_version": 1,
        "config_semantic_sha256": SHA,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "evaluator_schema": "hpc_anomaly_detection_v1",
        "metric_authority": METRIC_AUTHORITY,
        "primary_metric_key": "detection_f1",
        "optimization_direction": "maximize",
        "primary_metric_value": "0.5",
        "artifacts": [
            {"role": "analysis", "path": "analysis.md", "sha256": artifact_sha},
            {"role": "figure_plan", "path": "figure_plan.json", "sha256": figure_plan_sha},
            {"role": "results_table", "path": "results_table.tex", "sha256": artifact_sha},
            {"role": "summary", "path": "experiment_summary.json", "sha256": artifact_sha},
        ],
    }
    (root / "experiment_evidence_candidate.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )
    assert validate_experiment_evidence_candidate(root) == payload

    escaped = '{"ref":"stage-14\\/evidence_candidates\\/shadow"}\n'
    (root / "figure_plan.json").write_text(escaped, encoding="utf-8")
    escaped_sha = sha256_text(escaped)
    for item in identity["artifacts"]:
        if item["role"] == "figure_plan":
            item["sha256"] = escaped_sha
    identity_sha = sha256_text(canonical_json_text(identity))
    payload["candidate_id"] = "cand-" + identity_sha
    payload["identity_payload_sha256"] = identity_sha
    for item in payload["artifacts"]:
        if item["role"] == "figure_plan":
            item["sha256"] = escaped_sha
    (root / "experiment_evidence_candidate.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="deterministic empty figure plan",
    ):
        validate_experiment_evidence_candidate(root)

    (root / "figure_plan.json").write_text(
        EMPTY_FIGURE_PLAN_TEXT, encoding="utf-8"
    )
    for item in identity["artifacts"]:
        if item["role"] == "figure_plan":
            item["sha256"] = figure_plan_sha
    identity_sha = sha256_text(canonical_json_text(identity))
    payload["candidate_id"] = "cand-" + identity_sha
    payload["identity_payload_sha256"] = identity_sha
    for item in payload["artifacts"]:
        if item["role"] == "figure_plan":
            item["sha256"] = figure_plan_sha
    (root / "experiment_evidence_candidate.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )

    (root / "extra.txt").write_text("unmanifested\n", encoding="utf-8")
    with pytest.raises(CanonicalExperimentEvidenceError, match="file-set mismatch"):
        validate_experiment_evidence_candidate(root)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.8000", "0.8"), ("-0.0", "0"), ("1e-3", "0.001"), (2, "2")],
)
def test_canonical_decimal_grammar(raw: object, expected: str) -> None:
    assert canonical_decimal(raw) == expected


@pytest.mark.parametrize("raw", [True, "NaN", "Infinity", "+1", "01", float("nan")])
def test_canonical_decimal_rejects_ambiguous_or_nonfinite_values(raw: object) -> None:
    with pytest.raises(CanonicalExperimentEvidenceError):
        canonical_decimal(raw)


def test_every_partial_capability_map_is_blocked() -> None:
    complete = {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES}
    assert incomplete_canonical_evidence_capabilities(complete) == ()
    for name in REQUIRED_CAPABILITIES:
        partial = dict(complete)
        partial[name] = 0
        assert name in incomplete_canonical_evidence_capabilities(partial)
    assert incomplete_canonical_evidence_capabilities(CANONICAL_EVIDENCE_CAPABILITIES)


@pytest.mark.parametrize("missing_component", REQUIRED_CAPABILITIES)
def test_every_partial_map_blocks_all_external_and_persistent_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_component: str,
) -> None:
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    mapping = {name: CAPABILITY_SCHEMA_VERSION for name in REQUIRED_CAPABILITIES}
    mapping[missing_component] = 0
    monkeypatch.setattr(capabilities, "CANONICAL_EVIDENCE_CAPABILITIES", mapping)
    config = _config(tmp_path / "config-run")
    missing = tmp_path / "missing-run"

    calls = (
        lambda: generate_report(missing),
        lambda: ArtifactPublisher.__new__(ArtifactPublisher)._extract_experiments(missing),
        lambda: ArtifactSubscriber.__new__(ArtifactSubscriber).find_similar_experiments("q"),
        lambda: ExperimentMemory.__new__(ExperimentMemory).recall_best_configs("q"),
        lambda: extract_lessons([], run_dir=missing),
        lambda: _get_evolution_overlay(missing, "topic_init"),
        lambda: _metaclaw_post_pipeline(config, [], [], "blocked", missing),
    )
    for call in calls:
        with pytest.raises(CanonicalEvidenceMigrationIncomplete):
            call()
    server = ResearchClawMCPServer.__new__(ResearchClawMCPServer)
    payload = asyncio.run(server._handle_get_results({"run_id": "missing"}))
    assert payload["error_code"] == "canonical_evidence_migration_incomplete"
    assert not missing.exists()


def test_python_pipeline_blocks_before_creating_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "not-created"
    config = _config(tmp_path / "config-run")
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        execute_pipeline(
            run_dir=run_dir,
            run_id="blocked",
            config=config,
            adapters=AdapterBundle(),
            from_stage=Stage.TOPIC_INIT,
            to_stage=Stage.EXPERIMENT_RUN,
        )
    assert not run_dir.exists()


def test_cli_blocks_before_preflight_or_run_directory_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "blocked-cli-run"
    code = rc_cli.main(
        [
            "run",
            "--config",
            "config.deepseek.sectional-dry-run.yaml",
            "--output",
            str(output),
            "--skip-preflight",
            "--to-stage",
            "EXPERIMENT_RUN",
        ]
    )
    assert code == 1
    assert "canonical_evidence_migration_incomplete" in capsys.readouterr().err
    assert not output.exists()


def test_pipeline_ending_before_stage12_remains_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "config-run")
    run_dir = tmp_path / "pre-stage12"
    monkeypatch.setattr(
        pipeline_runner,
        "execute_stage",
        lambda stage, **kwargs: StageResult(
            stage=stage,
            status=StageStatus.DONE,
            artifacts=(),
        ),
    )
    results = execute_pipeline(
        run_dir=run_dir,
        run_id="pre-stage12",
        config=config,
        adapters=AdapterBundle(),
        to_stage=Stage.RESOURCE_PLANNING,
    )
    assert results[-1].stage is Stage.RESOURCE_PLANNING


def test_pre_stage12_overlay_adapter_returns_empty_without_reading_legacy_lessons(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "pre-stage12"
    lesson_dir = run_dir / "evolution"
    lesson_dir.mkdir(parents=True)
    (lesson_dir / "lessons.jsonl").write_text(
        '{"description":"UNBOUND_EXPERIMENT_LESSON"}\n', encoding="utf-8"
    )
    assert _get_pipeline_evolution_overlay(run_dir, "topic_init") == ""


def test_external_and_direct_stage12_entrypoints_block_before_reads(tmp_path: Path) -> None:
    missing = tmp_path / "missing-run"
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        generate_report(missing)
    publisher = ArtifactPublisher.__new__(ArtifactPublisher)
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        publisher._extract_experiments(missing)
    subscriber = ArtifactSubscriber.__new__(ArtifactSubscriber)
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        subscriber.find_similar_experiments("query")
    memory = ExperimentMemory.__new__(ExperimentMemory)
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        memory.recall_best_configs("task")
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        CanonicalExecutionController(missing / "stage-12").acquire(
            generation_binding_sha256=SHA,
            experiment_contract_sha256=SHA,
            sealed_candidate_manifest_sha256=SHA,
            config_semantic_sha256=SHA,
        )
    with pytest.raises(PermissionError, match="lease_required"):
        require_controller_lease(None)

    config = _config(tmp_path / "config-run")
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _execute_experiment_run(missing / "stage-12", missing, config, AdapterBundle())
    assert not missing.exists()


def test_stage12_guard_precedes_sandbox_import_and_artifact_reads() -> None:
    from researchclaw.pipeline.stage_impls import _execution as execution_impl

    source = inspect.getsource(_execute_experiment_run)
    guard = source.index('require_canonical_evidence_capabilities("stage12.execute_experiment_run")')
    sandbox_import = source.index("from researchclaw.experiment.factory import create_sandbox")
    sealed_artifact_read = source.index("_load_sealed_candidate")
    assert guard < sandbox_import < sealed_artifact_read
    assert "sandbox.run_project(" not in source
    assert "_ensure_sandbox_deps" not in source
    assert source.index("controller.acquire(") < source.index("sandbox = create_sandbox(")
    assert source.index(
        "validate_experiment_result_set(run_dir, config, result_set_text)"
    ) < source.index(
        '_atomic_write_text(stage_dir / "experiment_result_set.json", result_set_text)'
    )
    assert "def _latest_sandbox_project_results" not in inspect.getsource(execution_impl)
    legacy = inspect.getsource(execution_impl._execute_legacy_experiment_run)
    assert "legacy_stage12_execution_removed" in legacy
    assert "run_project(" not in legacy


def test_stage13_and_repair_guards_precede_sandbox_or_artifact_access(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "config-run")
    missing = tmp_path / "missing-run"

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _execute_iterative_refine(
            missing / "stage-13", missing, config, AdapterBundle()
        )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        run_repair_loop(missing, config, "blocked")
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _run_experiment_in_sandbox(
            missing / "experiment", config, missing / "repair-work"
        )
    assert not missing.exists()

    stage13_source = inspect.getsource(_execute_iterative_refine)
    assert stage13_source.index(
        'require_canonical_evidence_capabilities("stage13.execute_iterative_refine")'
    ) < stage13_source.index(
        "from researchclaw.experiment.factory import create_sandbox"
    )

    repair_source = inspect.getsource(run_repair_loop)
    assert repair_source.index(
        'require_canonical_evidence_capabilities("experiment_repair.run_repair_loop")'
    ) < repair_source.index("_load_experiment_summary")

    sandbox_source = inspect.getsource(_run_experiment_in_sandbox)
    assert sandbox_source.index(
        'require_canonical_evidence_capabilities('
    ) < sandbox_source.index("sandbox_dir = work_dir")


def test_mcp_returns_structured_migration_error_without_reading_run(tmp_path: Path) -> None:
    server = ResearchClawMCPServer.__new__(ResearchClawMCPServer)
    payload = asyncio.run(server._handle_get_results({"run_id": "does-not-exist"}))
    assert payload["success"] is False
    assert payload["error_code"] == "canonical_evidence_migration_incomplete"
    assert not tmp_path.joinpath("does-not-exist").exists()


def test_stage15_consumes_and_binds_only_canonical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    shadow = run_dir / "stage-14_v99"
    shadow.mkdir()
    (shadow / "analysis.md").write_text("POISON ANALYSIS", encoding="utf-8")
    stage15 = run_dir / "stage-15"
    stage15.mkdir()
    captured: dict[str, str] = {}

    class _Prompts:
        def for_stage(self, _name: str, **kwargs: object) -> SimpleNamespace:
            captured["analysis"] = str(kwargs["analysis"])
            return SimpleNamespace(system="system", user=str(kwargs["analysis"]))

    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._analysis._chat_with_prompt",
        lambda *_args, **_kwargs: SimpleNamespace(
            content="PROCEED: baseline, seed, and metric evidence are bounded."
        ),
    )
    result = _execute_research_decision(
        stage15,
        run_dir,
        config,
        AdapterBundle(),
        llm=SimpleNamespace(),
        prompts=_Prompts(),  # type: ignore[arg-type]
    )
    assert result.status.value == "done"
    assert captured["analysis"] == "Analysis.\n"
    payload = json.loads((stage15 / "decision_structured.json").read_text())
    evidence = load_canonical_experiment_evidence(run_dir)
    assert payload["canonical_experiment_evidence_path"] == evidence.manifest_path
    assert payload["canonical_experiment_evidence_sha256"] == evidence.manifest_sha256
    critique = json.loads((stage15 / "critique.json").read_text())
    assert critique["canonical_evidence"]["sha256"] == evidence.manifest_sha256
    assert (stage15 / "stage15_critique_manifest.json").is_file()
    from researchclaw.pipeline.stage15_critique import (
        Stage15CritiqueError,
        load_stage15_critique_publication,
    )

    replayed = load_stage15_critique_publication(run_dir)
    assert replayed.manifest["canonical_evidence"]["sha256"] == evidence.manifest_sha256
    (stage15 / "decision.md").write_text("PIVOT\n", encoding="utf-8")
    with pytest.raises(Stage15CritiqueError, match="source fixpoint"):
        load_stage15_critique_publication(run_dir)


@pytest.mark.parametrize(
    "mutation_target",
    ["decision.md", "decision_structured.json", "canonical_experiment_evidence.json"],
)
def test_stage15_rejects_source_change_during_critic_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
    mutation_target: str,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    stage15 = run_dir / "stage-15"
    stage15.mkdir()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._analysis._chat_with_prompt",
        lambda *_args, **_kwargs: SimpleNamespace(
            content="PROCEED: baseline, seed, and metric evidence are bounded."
        ),
    )

    class _MutatingCritic:
        def chat(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            target = (
                run_dir / mutation_target
                if mutation_target == "canonical_experiment_evidence.json"
                else stage15 / mutation_target
            )
            target.write_text("{}\n", encoding="utf-8")
            return SimpleNamespace(content=json.dumps({"findings": []}))

    result = _execute_research_decision(
        stage15,
        run_dir,
        config,
        AdapterBundle(),
        llm=_MutatingCritic(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert "source fixpoint" in (result.error or "")
    assert not (stage15 / "critique.json").exists()
    assert not (stage15 / "stage15_critique_manifest.json").exists()
    assert not (stage15 / "decision.md").exists()
    assert not (stage15 / "decision_structured.json").exists()


def test_stage15_agent_mode_fails_before_llm_or_decision_write(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    config = replace(
        config,
        experiment=replace(config.experiment, mode="collider_agent"),
    )
    stage15 = run_dir / "stage-15"
    stage15.mkdir()

    class _LLMSpy:
        calls = 0

        def chat(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            return SimpleNamespace(content="PROCEED")

    llm = _LLMSpy()
    result = _execute_research_decision(
        stage15,
        run_dir,
        config,
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert "canonical_critique_mode_unsupported" in (result.error or "")
    assert llm.calls == 0
    assert not (stage15 / "decision.md").exists()
    assert not (stage15 / "decision_structured.json").exists()


def test_stage15_external_review_resume_preserves_bound_decision_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    config = replace(
        config,
        llm=replace(
            config.llm,
            critic_source="external",
            external_review_path="ignored/by/canonical/v2",
        ),
    )
    stage15 = run_dir / "stage-15"
    stage15.mkdir()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._analysis._chat_with_prompt",
        lambda *_args, **_kwargs: SimpleNamespace(
            content="PROCEED: baseline, seed, and metric evidence are bounded."
        ),
    )

    pending = _execute_research_decision(
        stage15, run_dir, config, AdapterBundle(), llm=SimpleNamespace()
    )
    assert pending.status.value == "paused"
    request = json.loads(
        (stage15 / "critique-pending" / "external_review_request.json").read_text()
    )
    decision_before = (stage15 / "decision.md").read_bytes()
    structured_before = (stage15 / "decision_structured.json").read_bytes()

    external = stage15 / "external-review"
    external.mkdir()
    review = {
        "schema_version": request["schema_version"],
        "policy_version": request["policy_version"],
        "target_canonical_evidence": request["canonical_evidence"],
        "target_decision": request["decision"],
        "reviewer": {
            "reviewer_id": "reviewer-1",
            "reviewer_kind": "independent_agent",
            "organization": "independent",
        },
        "findings": [
            {
                "id": "external-01",
                "severity": "P1",
                "category": "evidence",
                "question": "Which bound observation supports the conclusion?",
                "finding": "The conclusion needs an explicit evidence binding.",
                "falsification_criterion": "A bound replicate contradicts it.",
            }
        ],
    }
    (external / "structured.json").write_text(
        canonical_json_text(review), encoding="utf-8"
    )

    finalized = _execute_research_decision(
        stage15, run_dir, config, AdapterBundle(), llm=None
    )
    assert finalized.status.value == "done"
    assert (stage15 / "decision.md").read_bytes() == decision_before
    assert (stage15 / "decision_structured.json").read_bytes() == structured_before
    critique = json.loads((stage15 / "critique.json").read_text())
    assert critique["state"] == "external_final"
    assert critique["findings"] == review["findings"]


def test_stage17_fact_closure_binds_canonical_manifest_and_rejects_change(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    paper = "# Paper\n\n## Results\n\nThe selected detection F1 is 0.5.\n"
    stage17 = run_dir / "stage-17"
    stage17.mkdir()
    (stage17 / "paper_draft.md").write_text(paper, encoding="utf-8")
    report = build_experiment_fact_closure_report(
        run_dir, paper_text=paper, evidence=evidence
    )
    assert report["valid"] is True
    assert report["canonical_experiment_evidence_sha256"] == evidence.manifest_sha256
    (stage17 / "experiment_fact_closure_report.json").write_text(
        canonical_experiment_fact_json_text(report), encoding="utf-8"
    )
    validate_experiment_fact_closure_report(run_dir)

    (run_dir / "canonical_experiment_evidence.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ExperimentFactClosureError, match="canonical experiment evidence"):
        validate_experiment_fact_closure_report(run_dir)


def _write_stage15_decision_binding(
    run_dir: Path,
    evidence: object,
    *,
    decision_text: str,
    structured_decision: object,
    parse_failed: bool = False,
) -> None:
    stage15 = run_dir / "stage-15"
    stage15.mkdir(exist_ok=True)
    (stage15 / "decision.md").write_text(decision_text, encoding="utf-8")
    payload: dict[str, object] = {
        "decision": structured_decision,
        "raw_text_excerpt": decision_text[:500],
        "generated": "2026-07-14T00:00:00+00:00",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "decision_path": "stage-15/decision.md",
        "decision_sha256": sha256_text(decision_text),
    }
    if parse_failed:
        payload["decision_parse_failed"] = True
        payload["note"] = "No recognized decision."
    else:
        payload["quality_warnings"] = []
    (stage15 / "decision_structured.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("decision_text", "structured_decision", "parse_failed"),
    (
        ("## Decision\nREFINE\n", "refine", False),
        ("## Decision\nPIVOT\n", "pivot", False),
        ("## Decision\nREFINE\n", "proceed", False),
        ("No decision token.\n", None, True),
    ),
)
def test_bound_stage15_decision_requires_reparsed_proceed(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    decision_text: str,
    structured_decision: object,
    parse_failed: bool,
) -> None:
    run_dir = tmp_path / "run"
    _config, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    _write_stage15_decision_binding(
        run_dir,
        evidence,
        decision_text=decision_text,
        structured_decision=structured_decision,
        parse_failed=parse_failed,
    )

    with pytest.raises(ValueError, match="Stage 15 decision"):
        _load_bound_stage15_decision(run_dir, evidence)


def test_bound_stage15_decision_rejects_agent_requirements_tag(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    _config, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    decision_text = "## Decision\nPROCEED\n"
    stage15 = run_dir / "stage-15"
    stage15.mkdir(exist_ok=True)
    (stage15 / "decision.md").write_text(decision_text, encoding="utf-8")
    payload = {
        "decision": "proceed",
        "verdict": {},
        "retry_count": 999,
        "rerun_triggered": True,
        "max_retries": 0,
        "generated": "2026-07-14T00:00:00+00:00",
        "source": "agent_requirements_gate",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "decision_path": "stage-15/decision.md",
        "decision_sha256": sha256_text(decision_text),
    }
    (stage15 / "decision_structured.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="agent requirements decisions"):
        _load_bound_stage15_decision(run_dir, evidence)


def test_stage16_rejects_agent_requirements_binding_before_llm(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    decision_text = "## Decision\nPROCEED\n"
    stage15 = run_dir / "stage-15"
    stage15.mkdir(exist_ok=True)
    (stage15 / "decision.md").write_text(decision_text, encoding="utf-8")
    payload = {
        "decision": "proceed",
        "verdict": {},
        "retry_count": 0,
        "rerun_triggered": False,
        "max_retries": 0,
        "generated": "2026-07-14T00:00:00+00:00",
        "source": "agent_requirements_gate",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "decision_path": "stage-15/decision.md",
        "decision_sha256": sha256_text(decision_text),
    }
    (stage15 / "decision_structured.json").write_text(
        canonical_json_text(payload), encoding="utf-8"
    )
    stage16 = run_dir / "stage-16"
    stage16.mkdir()

    class _UnexpectedLLM:
        calls = 0

        def chat(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("Stage 16 must reject before invoking the LLM")

    llm = _UnexpectedLLM()
    result = _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=llm  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == 0


def test_stage16_rejects_paused_stage15_binding_before_llm(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    _write_stage15_decision_binding(
        run_dir,
        evidence,
        decision_text="No decision token.\n",
        structured_decision=None,
        parse_failed=True,
    )
    stage16 = run_dir / "stage-16"
    stage16.mkdir()

    class _UnexpectedLLM:
        calls = 0

        def chat(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("Stage 16 must reject before invoking the LLM")

    llm = _UnexpectedLLM()
    result = _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=llm  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == 0


def test_stage15_16_bindings_ignore_shadow_stage_paths(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    _config, _root = _write_canonical_bundle(run_dir)
    evidence = load_canonical_experiment_evidence(run_dir)
    decision_text = "## Decision\nPROCEED\n"
    _write_stage15_decision_binding(
        run_dir,
        evidence,
        decision_text=decision_text,
        structured_decision="proceed",
    )
    outline = "# Outline\n\n## Abstract\n"
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    (stage16 / "outline.md").write_text(outline, encoding="utf-8")
    _write_outline_binding(
        stage16,
        outline=outline,
        decision_sha256=sha256_text(decision_text),
        evidence=evidence,
    )
    shadow = run_dir / "stage-99"
    shadow.mkdir()
    (shadow / "decision.md").write_text("## Decision\nPIVOT\n", encoding="utf-8")
    (shadow / "outline.md").write_text("# Poison outline\n", encoding="utf-8")

    assert _load_bound_stage15_decision(run_dir, evidence)[0] == decision_text
    assert _load_bound_stage16_outline(run_dir, evidence) == outline


@pytest.mark.parametrize(
    ("stage_name", "execute", "owned_names"),
    (
        (
            "stage-15",
            _execute_research_decision,
            ("decision.md", "decision_structured.json", "critique.json"),
        ),
        (
            "stage-16",
            _execute_paper_outline,
            (
                "outline.md",
                "citation_policy_effective.json",
                "citation_plan.preliminary.json",
                "citation_plan.json",
            ),
        ),
        (
            "stage-17",
            _execute_paper_draft,
            (
                "paper_draft.md",
                "paper_draft_invalid.md",
                "paper_structure_report.json",
                "section_generation_report.json",
                "citation_closure_report.json",
                "experiment_fact_closure_report.json",
                "paper_meta.json",
                "quality_warnings.json",
            ),
        ),
    ),
)
def test_stage15_17_reject_invalid_canonical_evidence_without_stale_outputs(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    stage_name: str,
    execute: object,
    owned_names: tuple[str, ...],
) -> None:
    run_dir = tmp_path / "run"
    config, _root = _write_canonical_bundle(run_dir)
    stage_dir = run_dir / stage_name
    stage_dir.mkdir()
    for name in owned_names:
        (stage_dir / name).write_text("stale\n", encoding="utf-8")
    (run_dir / "canonical_experiment_evidence.json").write_text("{}\n", encoding="utf-8")

    result = execute(  # type: ignore[operator]
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert all(not (stage_dir / name).exists() for name in owned_names)


def test_stage15_17_authoritative_functions_have_no_legacy_selectors() -> None:
    forbidden = (
        "experiment_summary_best",
        "stage-14*",
        "stage-12*/runs",
        "stage-13*/refinement",
        "_read_best_analysis",
    )
    functions = (
        _execute_research_decision,
        _execute_paper_outline,
        _execute_paper_draft,
        _collect_raw_experiment_metrics,
        _collect_grounded_metric_whitelist,
        build_experiment_fact_closure_report,
        validate_experiment_fact_closure_report,
    )
    for function in functions:
        source = inspect.getsource(function)
        assert all(pattern not in source for pattern in forbidden), function.__name__
