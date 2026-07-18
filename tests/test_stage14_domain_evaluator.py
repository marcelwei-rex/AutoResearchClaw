from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.literature.citation_policy import (
    _write_active_config_binding_under_lock,
)
from researchclaw.pipeline import runner as pipeline_runner
from researchclaw.pipeline import canonical_experiment_evidence
from researchclaw.pipeline import stage14_domain_evaluator
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalAnalysisController,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    load_canonical_experiment_evidence,
    reconstruct_expected_canonical_evidence,
    reconstruct_expected_stage9_14_metric_authority,
    validate_experiment_evidence_candidate,
    validate_canonical_experiment_manifest,
)
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.stage_impls._execution import _execute_experiment_run
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage10_evaluator_capture import _execute_capture, _prepare_run


_DOMAIN_METRICS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)


def _domain_observation_payload() -> dict[str, object]:
    rows = [
        {
            "circuit_family": family,
            "circuit_variant": f"{family}_ht{variant}",
            "condition": condition,
            "metrics": {metric: 1 for metric in _DOMAIN_METRICS},
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
        for family in ("c1355", "c1908", "c3540", "c432", "c6288", "c880")
        for variant in (1, 2, 3)
    ]
    return {
        "schema_version": 2,
        "observation_policy_version": 1,
        "dataset_capture_sha256": "a" * 64,
        "score_evidence_sha256": "b" * 64,
        "metric_keys": list(_DOMAIN_METRICS),
        "observations": rows,
        "per_seed": [],
        "aggregate": [],
        "primary_metric": {
            "aggregation": "mean_variants_then_mean_seeds_v1",
            "condition": "trojnet_community_graphsage",
            "key": "auprc",
            "observation_set": "exact_18_variants_per_seed",
            "value": 1,
        },
    }


def _ref(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _replace_bound_ref(value: object, path: str, content: bytes) -> None:
    if isinstance(value, dict):
        if value.get("path") == path and {"sha256", "size"}.issubset(value):
            value.update(_ref(path, content))
        for child in value.values():
            _replace_bound_ref(child, path, content)
    elif isinstance(value, list):
        for child in value:
            _replace_bound_ref(child, path, content)


def _rewrite_stage9_domain_snapshots_through_root(run: Path) -> None:
    def load_json(path: Path) -> dict[str, object]:
        value = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        assert isinstance(value, dict)
        return value

    stage9 = run / "stage-09"
    package_path = stage9 / "domain_evaluator_package_manifest.json"
    policy_path = stage9 / "domain_evaluator_execution_policy.json"
    package = load_json(package_path)
    policy = load_json(policy_path)
    policy["primary_metric_key"] = "f1"
    policy_bytes = canonical_authority_json_text(policy).encode("utf-8")
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    package["execution_policy_sha256"] = policy_sha
    for item in package["files"]:
        if item["capture_path"] == "policy/execution-policy-v1.json":
            item["sha256"] = policy_sha
            item["size"] = len(policy_bytes)
            break
    else:
        raise AssertionError("package policy entry is missing")
    package_bytes = canonical_authority_json_text(package).encode("utf-8")
    package_path.write_bytes(package_bytes)
    policy_path.write_bytes(policy_bytes)

    contract_path = stage9 / "experiment_contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    authority = contract["evaluator_authority"]
    package_sha = hashlib.sha256(package_bytes).hexdigest()
    authority["package_manifest_package_sha256"] = package_sha
    authority["package_manifest_snapshot_sha256"] = package_sha
    authority["execution_policy_package_sha256"] = policy_sha
    authority["execution_policy_snapshot_sha256"] = policy_sha
    contract_bytes = yaml.safe_dump(
        contract, sort_keys=False, allow_unicode=True
    ).encode("utf-8")
    contract_path.write_bytes(contract_bytes)
    (stage9 / "experiment_contract.sha256").write_text(
        hashlib.sha256(contract_bytes).hexdigest() + "\n", encoding="ascii"
    )

    capture_root = run / "stage-10/evaluator-capture-v1"
    capture_policy = capture_root / "policy/execution-policy-v1.json"
    capture_policy.write_bytes(policy_bytes)
    capture_manifest_path = capture_root / "capture-manifest.json"
    capture_manifest = load_json(capture_manifest_path)
    _replace_bound_ref(
        capture_manifest,
        "stage-09/domain_evaluator_package_manifest.json",
        package_bytes,
    )
    _replace_bound_ref(
        capture_manifest,
        "stage-09/domain_evaluator_execution_policy.json",
        policy_bytes,
    )
    source_rows = []
    for package_item in package["files"]:
        package_entry = {
            key: package_item[key]
            for key in (
                "source_root",
                "source_path",
                "capture_path",
                "role",
                "sha256",
                "size",
            )
        }
        source_rows.append(
            {
                key: package_item[key]
                for key in ("source_root", "source_path", "role", "sha256", "size")
            }
        )
        for captured in capture_manifest["files"]:
            if captured["path"] == package_item["capture_path"]:
                captured["sha256"] = package_item["sha256"]
                captured["size"] = package_item["size"]
                captured["package_entry_sha256"] = hashlib.sha256(
                    canonical_authority_json_text(package_entry).encode("utf-8")
                ).hexdigest()
                break
    capture_manifest["source_namespace_sha256"] = hashlib.sha256(
        canonical_authority_json_text(source_rows).encode("utf-8")
    ).hexdigest()
    capture_manifest_bytes = canonical_authority_json_text(capture_manifest).encode(
        "utf-8"
    )
    capture_manifest_path.write_bytes(capture_manifest_bytes)

    seal_path = run / "stage-10/selected_candidate_manifest.json"
    seal = load_json(seal_path)
    for path, content in (
        ("stage-09/experiment_contract.yaml", contract_bytes),
        ("stage-09/domain_evaluator_package_manifest.json", package_bytes),
        ("stage-09/domain_evaluator_execution_policy.json", policy_bytes),
        ("stage-10/evaluator-capture-v1/capture-manifest.json", capture_manifest_bytes),
    ):
        _replace_bound_ref(seal, path, content)
    seal_bytes = canonical_authority_json_text(seal).encode("utf-8")
    seal_path.write_bytes(seal_bytes)

    baseline_path = run / "stage-12/experiment_result_set.json"
    baseline = load_json(baseline_path)
    for path, content in (
        ("stage-09/experiment_contract.yaml", contract_bytes),
        ("stage-09/domain_evaluator_package_manifest.json", package_bytes),
        ("stage-09/domain_evaluator_execution_policy.json", policy_bytes),
        ("stage-10/evaluator-capture-v1/capture-manifest.json", capture_manifest_bytes),
        ("stage-10/selected_candidate_manifest.json", seal_bytes),
    ):
        _replace_bound_ref(baseline, path, content)
    baseline_bytes = canonical_authority_json_text(baseline).encode("utf-8")
    baseline_path.write_bytes(baseline_bytes)

    refinement_path = run / "stage-13/refinement_result_set.json"
    refinement = load_json(refinement_path)
    for path, content in (
        ("stage-09/experiment_contract.yaml", contract_bytes),
        ("stage-09/domain_evaluator_execution_policy.json", policy_bytes),
        ("stage-10/evaluator-capture-v1/capture-manifest.json", capture_manifest_bytes),
        ("stage-10/selected_candidate_manifest.json", seal_bytes),
        ("stage-12/experiment_result_set.json", baseline_bytes),
    ):
        _replace_bound_ref(refinement, path, content)
    refinement_bytes = canonical_authority_json_text(refinement).encode("utf-8")
    refinement_path.write_bytes(refinement_bytes)

    candidate_path = next(
        (run / "stage-14/evidence_candidates").glob(
            "cand-*/experiment_evidence_candidate.json"
        )
    )
    candidate = load_json(candidate_path)
    for path, content in (
        ("stage-09/experiment_contract.yaml", contract_bytes),
        ("stage-09/domain_evaluator_package_manifest.json", package_bytes),
        ("stage-09/domain_evaluator_execution_policy.json", policy_bytes),
        ("stage-10/evaluator-capture-v1/capture-manifest.json", capture_manifest_bytes),
        ("stage-10/selected_candidate_manifest.json", seal_bytes),
        ("stage-12/experiment_result_set.json", baseline_bytes),
        ("stage-13/refinement_result_set.json", refinement_bytes),
    ):
        _replace_bound_ref(candidate, path, content)
    candidate["candidate_id"] = stage14_domain_evaluator._candidate_id_for_payload(
        candidate
    )
    candidate_bytes = canonical_authority_json_text(candidate).encode("utf-8")
    old_candidate_root = candidate_path.parent
    new_candidate_root = old_candidate_root.with_name(candidate["candidate_id"])
    old_candidate_root.rename(new_candidate_root)
    candidate_path = new_candidate_root / candidate_path.name
    candidate_path.write_bytes(candidate_bytes)

    root_path = run / "canonical_experiment_evidence.json"
    root = load_json(root_path)
    for path, content in (
        ("stage-09/experiment_contract.yaml", contract_bytes),
        ("stage-09/domain_evaluator_package_manifest.json", package_bytes),
        ("stage-09/domain_evaluator_execution_policy.json", policy_bytes),
        ("stage-10/evaluator-capture-v1/capture-manifest.json", capture_manifest_bytes),
        ("stage-10/selected_candidate_manifest.json", seal_bytes),
        ("stage-12/experiment_result_set.json", baseline_bytes),
        ("stage-13/refinement_result_set.json", refinement_bytes),
    ):
        _replace_bound_ref(root, path, content)
    old_id = old_candidate_root.name
    new_id = new_candidate_root.name
    root["selected_candidate"]["candidate_id"] = new_id
    root["selected_candidate"]["manifest"] = _ref(
        f"stage-14/evidence_candidates/{new_id}/experiment_evidence_candidate.json",
        candidate_bytes,
    )
    for field, name in (
        ("selected_summary", "experiment_summary.json"),
        ("selected_analysis", "analysis.md"),
    ):
        source = root[field]["source"]
        source["path"] = source["path"].replace(old_id, new_id)
    root_path.write_bytes(canonical_authority_json_text(root).encode("utf-8"))


def _prepare_domain_stage14(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> tuple[Path, object]:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status is StageStatus.DONE
    assert _execute_experiment_run(
        run / "stage-12", run, config, AdapterBundle()
    ).status is StageStatus.DONE
    assert execute_stage(
        Stage.ITERATIVE_REFINE,
        run_dir=run,
        run_id="stage13-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE
    return run, config


def test_stage14_domain_results_table_uses_latex_row_breaks() -> None:
    table = stage14_domain_evaluator._render_fixed_artifacts(
        {
            "condition": "trojnet_localization",
            "key": "auprc",
            "observation_set": "per_seed",
            "aggregation": "mean",
            "value": 1,
        }
    )["results_table.tex"]

    assert b"Metric & Value " + b"\\\\" + b"\n\\hline\n" in table
    assert b"\\\\n" not in table


def test_domain_observation_projection_requires_exact_162_row_closure() -> None:
    payload = _domain_observation_payload()
    projected = stage14_domain_evaluator.project_domain_metric_observations(
        canonical_authority_json_text(payload).encode("utf-8")
    )

    assert tuple(projected) == _DOMAIN_METRICS
    assert {len(values) for values in projected.values()} == {162}


@pytest.mark.parametrize(
    "mutation",
    (
        "short",
        "long",
        "duplicate",
        "bool",
        "condition",
        "seed",
        "variant",
        "metric_keys",
    ),
)
def test_domain_observation_projection_rejects_nonproduction_shape(
    mutation: str,
) -> None:
    payload = _domain_observation_payload()
    rows = payload["observations"]
    assert isinstance(rows, list)
    if mutation == "short":
        rows.pop()
    elif mutation == "long":
        rows.append(deepcopy(rows[-1]))
        rows[-1]["circuit_variant"] = "extra_ht1"
    elif mutation == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif mutation == "bool":
        rows[0]["metrics"]["auprc"] = True
    elif mutation == "condition":
        for row in rows:
            if row["condition"] == "raw_cc1":
                row["condition"] = "shadow_condition"
    elif mutation == "seed":
        for row in rows:
            if row["seed"] == 0:
                row["seed"] = 3
    elif mutation == "variant":
        rows[0]["circuit_family"] = "shadow"
        rows[0]["circuit_variant"] = "shadow_ht1"
    else:
        metric_keys = payload["metric_keys"]
        assert isinstance(metric_keys, list)
        metric_keys[0] = "shadow_metric"
        for row in rows:
            metrics = row["metrics"]
            metrics["shadow_metric"] = metrics.pop("accuracy")

    with pytest.raises(
        stage14_domain_evaluator.Stage14DomainEvaluatorError
    ):
        stage14_domain_evaluator.project_domain_metric_observations(
            canonical_authority_json_text(payload).encode("utf-8")
        )


def test_stage14_domain_public_replay_is_capability_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (
        tmp_path
        / "run/stage-14/evidence_candidates"
        / ("cand-" + "0" * 64)
    )

    def unexpected_lock(*_args, **_kwargs):
        raise AssertionError("candidate replay acquired a lock before capability guard")

    monkeypatch.setattr(
        stage14_domain_evaluator.ReleaseGraphLock, "acquire", unexpected_lock
    )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        stage14_domain_evaluator.validate_domain_evaluator_candidate(candidate)
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        validate_experiment_evidence_candidate(candidate)


def test_stage14_domain_under_controller_entry_is_default_deny(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeController:
        def read_run_file(self, name: str) -> bytes:
            calls.append(("read", name))
            return b"{}"

        def remove_run_files(self, names: tuple[str, ...]) -> None:
            calls.append(("remove", names))

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        canonical_experiment_evidence._publish_canonical_experiment_manifest_under_controller(
            FakeController(), tmp_path / "missing-run", object()
        )
    assert calls == []

    with pytest.raises(TypeError, match="controller"):
        canonical_experiment_evidence.publish_canonical_experiment_manifest(
            tmp_path / "missing-run", object(), controller=FakeController()
        )


def test_stage14_domain_under_controller_rejects_untrusted_states(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    for run in (run_a, run_b):
        run.mkdir()
        (run / "stage-14").mkdir()

    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="untrusted Stage 14 promotion controller",
    ):
        canonical_experiment_evidence._publish_canonical_experiment_manifest_under_controller(
            object(), run_a, object()
        )

    reader = CanonicalAnalysisController.acquire_reader(run_a)
    try:
        with pytest.raises(
            RuntimeError, match="promotion_controller_required"
        ):
            canonical_experiment_evidence._publish_canonical_experiment_manifest_under_controller(
                reader, run_a, object()
            )
    finally:
        reader.close()

    promotion = CanonicalAnalysisController.acquire_promotion(run_a)
    promotion.close()
    with pytest.raises(RuntimeError, match="promotion_controller_inactive"):
        canonical_experiment_evidence._publish_canonical_experiment_manifest_under_controller(
            promotion, run_a, object()
        )

    promotion = CanonicalAnalysisController.acquire_promotion(run_a)
    try:
        with pytest.raises(RuntimeError, match="promotion_controller_run_mismatch"):
            canonical_experiment_evidence._publish_canonical_experiment_manifest_under_controller(
                promotion, run_b, object()
            )
    finally:
        promotion.close()


def test_stage14_domain_evaluator_publishes_fixed_candidate_and_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    llm_factory_calls = 0

    def counted_llm_factory(*_args, **_kwargs):
        nonlocal llm_factory_calls
        llm_factory_calls += 1
        return None

    monkeypatch.setattr(
        "researchclaw.pipeline.executor._create_configured_llm", counted_llm_factory
    )
    result = execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.DONE, result.error
    assert llm_factory_calls == 0
    stage14 = run / "stage-14"
    assert tuple(sorted(path.name for path in stage14.iterdir())) == (
        "evidence_candidates",
    )
    candidates = tuple((stage14 / "evidence_candidates").iterdir())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert tuple(sorted(path.name for path in candidate.iterdir())) == (
        "analysis.md",
        "experiment_evidence_candidate.json",
        "experiment_summary.json",
        "figure_plan.json",
        "results_table.tex",
    )
    root = validate_canonical_experiment_manifest(run, config)
    assert root["schema_version"] == 2
    assert root["generation_kind"] == "domain_evaluator"
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="does not accept a manifest override",
    ):
        validate_canonical_experiment_manifest(
            run,
            config,
            (run / "canonical_experiment_evidence.json").read_text(encoding="utf-8"),
        )
    evidence = load_canonical_experiment_evidence(run)
    assert evidence.manifest["schema_version"] == 2
    assert evidence.selected_result["schema_version"] == 2
    assert tuple(evidence.metric_observations) == (
        "accuracy",
        "auprc",
        "auroc",
        "f1",
        "fpr",
        "precision",
        "recall",
        "top_k_precision",
    )
    assert {len(values) for values in evidence.metric_observations.values()} == {162}
    project = {artifact.logical_name: artifact for artifact in evidence.project_artifacts}
    assert len(project) == 46
    assert project["main.py"].source_path == (
        "stage-10/evaluator-capture-v1/evaluator/evaluator_main.py"
    )
    assert "verifier_main.py" in project
    assert "trojnet/anomaly.py" in project
    assert "data/c1355/c1355_ht1.bench" in project
    assert {artifact.role for artifact in evidence.artifacts} == {
        "analysis",
        "figure_plan",
        "results_table",
        "summary",
    }
    assert validate_experiment_evidence_candidate(candidate)["schema_version"] == 2
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="does not accept a text override",
    ):
        validate_experiment_evidence_candidate(
            candidate,
            (candidate / "experiment_evidence_candidate.json").read_text(
                encoding="utf-8"
            ),
        )
    expected_metric_authority = reconstruct_expected_stage9_14_metric_authority(
        run, config
    )
    assert expected_metric_authority["metric_authority"] == root["bindings"][
        "metric_authority"
    ]

    # The real runner holds one promotion controller and must not reacquire it.
    pipeline_runner._promote_best_stage14(run, config)
    assert validate_canonical_experiment_manifest(run, config) == root

    original_plan_builder = stage14_domain_evaluator._build_root_plan_from_snapshot
    analysis_path = candidate / "analysis.md"
    original_analysis = analysis_path.read_bytes()
    changed = False

    def mutate_after_expected_plan(*args, **kwargs):
        nonlocal changed
        plan = original_plan_builder(*args, **kwargs)
        if not changed:
            changed = True
            analysis_path.write_bytes(original_analysis + b"late mutation\n")
        return plan

    monkeypatch.setattr(
        stage14_domain_evaluator,
        "_build_root_plan_from_snapshot",
        mutate_after_expected_plan,
    )
    try:
        with pytest.raises(
            stage14_domain_evaluator.Stage14DomainEvaluatorError,
            match="expected-plan replay",
        ):
            stage14_domain_evaluator.build_domain_evaluator_publication_plan(
                run, config
            )
    finally:
        analysis_path.write_bytes(original_analysis)
        monkeypatch.setattr(
            stage14_domain_evaluator,
            "_build_root_plan_from_snapshot",
            original_plan_builder,
        )

    rerun = execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator-rerun",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    assert rerun.status is StageStatus.DONE, rerun.error
    assert len(tuple((stage14 / "evidence_candidates").iterdir())) == 1

    _rewrite_stage9_domain_snapshots_through_root(run)
    forged_package = (run / "stage-09/domain_evaluator_package_manifest.json").read_bytes()
    forged_policy = (run / "stage-09/domain_evaluator_execution_policy.json").read_bytes()
    forged_root = json.loads(
        (run / "canonical_experiment_evidence.json").read_text(encoding="utf-8"),
        parse_float=Decimal,
    )
    assert forged_root["bindings"]["package_manifest"] == _ref(
        "stage-09/domain_evaluator_package_manifest.json", forged_package
    )
    assert forged_root["bindings"]["execution_policy"] == _ref(
        "stage-09/domain_evaluator_execution_policy.json", forged_policy
    )
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="expected metric authority reconstruction failed",
    ):
        reconstruct_expected_stage9_14_metric_authority(run, config)


def test_stage14_domain_publication_clears_root_on_plan_and_replay_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    assert execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE

    def assert_no_root() -> None:
        assert not any(
            (run / name).exists()
            for name in (
                "canonical_experiment_evidence.json",
                "experiment_summary_best.json",
                "analysis_best.md",
            )
        )

    original_source = stage14_domain_evaluator._load_domain_stage14_source
    monkeypatch.setattr(
        stage14_domain_evaluator,
        "_load_domain_stage14_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("plan failure")),
    )
    with pytest.raises(RuntimeError, match="plan failure"):
        stage14_domain_evaluator.publish_domain_evaluator_canonical_manifest(run, config)
    assert_no_root()
    monkeypatch.setattr(
        stage14_domain_evaluator, "_load_domain_stage14_source", original_source
    )
    stage14_domain_evaluator.publish_domain_evaluator_canonical_manifest(run, config)

    original_replay = stage14_domain_evaluator.validate_domain_evaluator_canonical_manifest
    monkeypatch.setattr(
        stage14_domain_evaluator,
        "validate_domain_evaluator_canonical_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("replay failure")),
    )
    with pytest.raises(RuntimeError, match="replay failure"):
        stage14_domain_evaluator.publish_domain_evaluator_canonical_manifest(run, config)
    assert_no_root()
    monkeypatch.setattr(
        stage14_domain_evaluator,
        "validate_domain_evaluator_canonical_manifest",
        original_replay,
    )


def test_stage14_domain_replay_closes_config_and_versioned_candidate_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    assert execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE
    stage14 = run / "stage-14"
    history = run / "stage-14_v99"
    shutil.copytree(stage14, history)
    root = validate_canonical_experiment_manifest(run, config)
    assert root["selected_candidate"]["manifest"]["path"].startswith("stage-14/")
    (history / "SHADOW_POISON").write_text("poison\n", encoding="utf-8")
    with pytest.raises(stage14_domain_evaluator.Stage14DomainEvaluatorError, match="history namespace"):
        validate_canonical_experiment_manifest(run, config)
    (history / "SHADOW_POISON").unlink()

    for shadow_name in ("stage-14_v0", "stage-14_v01", "stage-14_vEVIL"):
        shadow = run / shadow_name
        shadow.mkdir()
        try:
            with pytest.raises(
                stage14_domain_evaluator.Stage14DomainEvaluatorError,
                match="generation shadow",
            ):
                validate_canonical_experiment_manifest(run, config)
        finally:
            shadow.rmdir()

    original_source = stage14_domain_evaluator._load_domain_stage14_source
    changed = False

    def switch_active_config(*args, **kwargs):
        nonlocal changed
        source = original_source(*args, **kwargs)
        if not changed:
            changed = True
            resumed = run / "config.resumed-20260718-120000.yaml"
            resumed.write_bytes((run / "config.yaml").read_bytes())
            # Model a non-cooperating late filesystem mutation after semantic replay.
            _write_active_config_binding_under_lock(run, resumed)
        return source

    monkeypatch.setattr(
        stage14_domain_evaluator, "_load_domain_stage14_source", switch_active_config
    )
    with pytest.raises(stage14_domain_evaluator.Stage14DomainEvaluatorError, match="changed"):
        validate_canonical_experiment_manifest(run, config)


def test_stage14_domain_replay_rejects_shadow_and_late_root_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    assert execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE
    stage14 = run / "stage-14"
    shadow = stage14 / "SHADOW_POISON"
    shadow.write_text("poison\n", encoding="utf-8")
    with pytest.raises(stage14_domain_evaluator.Stage14DomainEvaluatorError, match="namespace"):
        validate_canonical_experiment_manifest(run, config)
    shadow.unlink()

    root_path = run / "canonical_experiment_evidence.json"
    original_root = root_path.read_bytes()
    original_validate = stage14_domain_evaluator._load_domain_stage14_source
    changed = False

    def mutate_after_source(*args, **kwargs):
        nonlocal changed
        source = original_validate(*args, **kwargs)
        if not changed:
            changed = True
            root_path.write_text("{}\n", encoding="utf-8")
        return source

    monkeypatch.setattr(
        stage14_domain_evaluator,
        "_load_domain_stage14_source",
        mutate_after_source,
    )
    try:
        with pytest.raises((CanonicalExperimentEvidenceError, stage14_domain_evaluator.Stage14DomainEvaluatorError)):
            validate_canonical_experiment_manifest(run, config)
    finally:
        root_path.write_bytes(original_root)


def test_stage14_domain_candidate_rejects_mixed_schema_binding(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    assert execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE
    candidate_path = next((run / "stage-14/evidence_candidates").glob("*/experiment_evidence_candidate.json"))
    original = candidate_path.read_bytes()
    payload = stage14_domain_evaluator.parse_domain_evaluator_candidate(
        original.decode("utf-8")
    )
    forged = deepcopy(payload)
    forged["bindings"]["stage13_refinement"]["size"] = True
    candidate_path.write_text(canonical_authority_json_text(forged), encoding="utf-8")
    with pytest.raises((CanonicalExperimentEvidenceError, stage14_domain_evaluator.Stage14DomainEvaluatorError)):
        validate_canonical_experiment_manifest(run, config)
    candidate_path.write_bytes(original)


def test_stage14_domain_independent_reconstruction_replays_v2_root(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_domain_stage14(
        tmp_path, canonical_evidence_migration_complete
    )
    assert execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-domain-evaluator",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    ).status is StageStatus.DONE

    evidence = reconstruct_expected_canonical_evidence(run)
    assert evidence.manifest["schema_version"] == 2
    assert evidence.manifest["generation_kind"] == "domain_evaluator"


def test_stage14_domain_invalidates_old_root_before_missing_stage13_preflight(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_run(tmp_path)
    stage14 = run / "stage-14"
    stage14.mkdir()
    (run / "canonical_experiment_evidence.json").write_text("stale\n", encoding="utf-8")
    (run / "experiment_summary_best.json").write_text("stale\n", encoding="utf-8")
    (run / "analysis_best.md").write_text("stale\n", encoding="utf-8")

    result = execute_stage(
        Stage.RESULT_ANALYSIS,
        run_dir=run,
        run_id="stage14-missing-stage13",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.FAILED
    assert not any((run / name).exists() for name in (
        "canonical_experiment_evidence.json",
        "experiment_summary_best.json",
        "analysis_best.md",
    ))
