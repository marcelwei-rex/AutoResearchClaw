from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil

import pytest

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
