# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportAttributeAccessIssue=false, reportUnknownLambdaType=false
from __future__ import annotations

import dataclasses
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline import runner as rc_runner
from researchclaw.pipeline.executor import StageResult
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stages import STAGE_SEQUENCE, Stage, StageStatus


@pytest.fixture()
def rc_config(tmp_path: Path) -> RCConfig:
    data = {
        "project": {"name": "rc-runner-test", "mode": "docs-first"},
        "research": {"topic": "pipeline testing"},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "local"},
        "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
        "openclaw_bridge": {},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "RC_TEST_KEY",
            "api_key": "inline",
        },
    }
    return RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)


@pytest.fixture()
def adapters() -> AdapterBundle:
    return AdapterBundle()


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _isolate_runner_control_flow_from_canonical_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner tests mock stage execution, so they must also mock Stage 14 authority."""
    monkeypatch.setattr(rc_runner, "_promote_best_stage14", lambda *_args, **_kwargs: None)


def _done(stage: Stage, artifacts: tuple[str, ...] = ("out.md",)) -> StageResult:
    return StageResult(stage=stage, status=StageStatus.DONE, artifacts=artifacts)


def _failed(stage: Stage, msg: str = "boom") -> StageResult:
    return StageResult(stage=stage, status=StageStatus.FAILED, artifacts=(), error=msg)


def _paused(stage: Stage, msg: str = "resume needed") -> StageResult:
    return StageResult(
        stage=stage,
        status=StageStatus.PAUSED,
        artifacts=("refinement_log.json",),
        error=msg,
        decision="resume",
    )


def _blocked(stage: Stage) -> StageResult:
    return StageResult(
        stage=stage,
        status=StageStatus.BLOCKED_APPROVAL,
        artifacts=("gate.md",),
        decision="block",
    )


def test_legacy_experiment_diagnosis_and_repair_are_mechanically_disabled(
    tmp_path: Path, rc_config: RCConfig
) -> None:
    with pytest.raises(PermissionError, match="diagnosis is disabled"):
        rc_runner._run_experiment_diagnosis(tmp_path, rc_config, "run")
    with pytest.raises(PermissionError, match="repair is disabled"):
        rc_runner._run_experiment_repair(tmp_path, rc_config, "run")

    import inspect

    pipeline_source = inspect.getsource(rc_runner.execute_pipeline)
    assert "_run_experiment_diagnosis" not in pipeline_source
    assert "_run_experiment_repair" not in pipeline_source

    shadow_summary = tmp_path / "stage-14_v99/experiment_summary.json"
    shadow_run = tmp_path / "stage-13_v99/runs/run-poison.json"
    shadow_summary.parent.mkdir(parents=True)
    shadow_run.parent.mkdir(parents=True)
    shadow_summary.write_text('{"quality":"SHADOW_POISON"}', encoding="utf-8")
    shadow_run.write_text('{"stderr":"SHADOW_RUNTIME_POISON"}', encoding="utf-8")
    assert not (tmp_path / "experiment_diagnosis.json").exists()
    assert not (tmp_path / "repair_prompt.txt").exists()


def test_execute_pipeline_runs_stages_in_sequence(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-seq",
        config=rc_config,
        adapters=adapters,
    )
    assert seen == list(STAGE_SEQUENCE)
    assert len(results) == 25
    assert all(r.status == StageStatus.DONE for r in results)


def test_execute_pipeline_invokes_canonical_promotion_before_paper_outline(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    events: list[tuple[str, object]] = []

    def record_execution(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        events.append(("execute", stage))
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", record_execution)
    monkeypatch.setattr(
        rc_runner,
        "_promote_best_stage14",
        lambda target, _config: events.append(("promote", target)),
    )

    rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-canonical-promotion-spy",
        config=rc_config,
        adapters=adapters,
        to_stage=Stage.PAPER_OUTLINE,
    )

    promotion = events.index(("promote", run_dir))
    outline = events.index(("execute", Stage.PAPER_OUTLINE))
    assert promotion < outline


def test_sectional_run_manifest_records_role_specific_models(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    monkeypatch.setattr(
        rc_runner,
        "execute_stage",
        lambda stage, **kwargs: _done(stage),
    )
    config = dataclasses.replace(
        rc_config,
        llm=dataclasses.replace(
            rc_config.llm,
            primary_model="section-writer",
            critic_model="stage15-critic",
        ),
        paper_revision=dataclasses.replace(
            rc_config.paper_revision,
            sectional_enabled=True,
            critic_model="section-critic",
        ),
    )

    rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-sectional-manifest",
        config=config,
        adapters=adapters,
    )

    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    reviewer = manifest["reviewer"]
    assert reviewer["writer_model"] == "section-writer"
    assert reviewer["critic_model"] == "stage15-critic"
    assert reviewer["sectional_writer_model"] == "section-writer"
    assert reviewer["sectional_critic_model"] == "section-critic"


def test_sectional_run_manifest_write_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    from researchclaw.pipeline import release_artifacts

    monkeypatch.setattr(
        rc_runner,
        "execute_stage",
        lambda stage, **kwargs: _done(stage),
    )

    def fail_manifest(*args, **kwargs):
        raise OSError("manifest write failed")

    monkeypatch.setattr(release_artifacts, "write_run_manifest", fail_manifest)
    config = dataclasses.replace(
        rc_config,
        paper_revision=dataclasses.replace(
            rc_config.paper_revision,
            sectional_enabled=True,
            critic_model="section-critic",
        ),
    )

    with pytest.raises(RuntimeError, match="requires a complete run_manifest"):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="run-sectional-manifest-failure",
            config=config,
            adapters=adapters,
        )


def test_execute_pipeline_stops_on_failed_stage(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fail_stage = Stage.SEARCH_STRATEGY

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        if stage == fail_stage:
            return StageResult(stage, StageStatus.FAILED, (), "forced failure", "abort")
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-fail",
        config=rc_config,
        adapters=adapters,
    )
    assert results[-1].stage == fail_stage
    assert results[-1].status == StageStatus.FAILED
    assert len(results) == int(fail_stage)
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    assert summary["final_terminal_action"] == "stop"
    assert summary["final_decision"] == "abort"
    assert "Pipeline aborted by user" not in capsys.readouterr().out
    attempt = json.loads((run_dir / "attempts/attempt_log.jsonl").read_text().splitlines()[-1])
    assert (attempt["status"], attempt["decision"]) == ("failed", "abort")


def test_execute_pipeline_honors_done_abort(
    monkeypatch, run_dir, rc_config, adapters, capsys,
) -> None:
    monkeypatch.setattr(rc_runner, "execute_stage", lambda stage, **_: StageResult(
        stage, StageStatus.DONE, (), decision="abort"))
    results = rc_runner.execute_pipeline(
        run_dir=run_dir, run_id="run-abort", config=rc_config, adapters=adapters)
    assert (len(results), "Pipeline aborted by user" in capsys.readouterr().out) == (1, True)


def test_execute_pipeline_stops_on_paused_stage(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    pause_stage = Stage.ITERATIVE_REFINE

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        if stage == pause_stage:
            return _paused(stage, "ACP prompt timed out after 1800s")
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-paused",
        config=rc_config,
        adapters=adapters,
    )
    assert results[-1].stage == pause_stage
    assert results[-1].status == StageStatus.PAUSED
    assert len(results) == int(pause_stage)
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["last_completed_stage"] == int(Stage.EXPERIMENT_RUN)
    summary = json.loads((run_dir / "pipeline_summary.json").read_text(encoding="utf-8"))
    assert summary["stages_paused"] == 1
    assert summary["final_status"] == "paused"


def test_execute_pipeline_stops_on_gate_when_stop_on_gate_enabled(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    gate_stage = Stage.LITERATURE_SCREEN

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        if stage == gate_stage:
            return _blocked(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-gate-stop",
        config=rc_config,
        adapters=adapters,
        stop_on_gate=True,
    )
    assert results[-1].stage == gate_stage
    assert results[-1].status == StageStatus.BLOCKED_APPROVAL
    assert len(results) == int(gate_stage)


def test_execute_pipeline_injects_artifacts_and_skips_configured_stages(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    cfg = dataclasses.replace(
        rc_config,
        runtime=dataclasses.replace(
            rc_config.runtime,
            skip_stages=(9,),
            inject_artifacts={"stage-07/synthesis.md": "Injected synthesis"},
        ),
    )
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs: Any) -> StageResult:
        _ = kwargs
        seen.append(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-partial",
        config=cfg,
        adapters=adapters,
        from_stage=Stage.HYPOTHESIS_GEN,
    )

    assert (
        run_dir / "stage-07" / "synthesis.md"
    ).read_text(encoding="utf-8") == "Injected synthesis"
    assert Stage.EXPERIMENT_DESIGN not in seen
    skipped = next(r for r in results if r.stage == Stage.EXPERIMENT_DESIGN)
    assert skipped.decision == "skipped"
    assert (run_dir / "stage-09" / "exp_plan.yaml").exists()


@pytest.mark.parametrize("stage_num", [4, 5, 24, 25])
def test_execute_pipeline_rejects_programmatic_evidence_stage_skip(
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    stage_num: int,
) -> None:
    cfg = dataclasses.replace(
        rc_config,
        runtime=dataclasses.replace(rc_config.runtime, skip_stages=(stage_num,)),
    )

    with pytest.raises(
        ValueError,
        match=f"cannot skip evidence-authority stages: {stage_num}",
    ):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="run-forbidden-skip",
            config=cfg,
            adapters=adapters,
        )

    assert not (run_dir / f"stage-{stage_num:02d}").exists()


def test_generic_skip_writer_rejects_stage24_without_creating_artifacts(
    run_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="evidence-authority stage: 24"):
        rc_runner._write_skipped_stage_outputs(
            run_dir, Stage.TRUTH_AUDIT, "run-stage24-skip"
        )

    assert not (run_dir / "stage-24").exists()


def test_execute_pipeline_does_not_consume_raw_refinement_trajectory(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def mock_execute_stage(stage: Stage, **kwargs: Any) -> StageResult:
        run_path = cast(Path, kwargs["run_dir"])
        if stage == Stage.ITERATIVE_REFINE:
            stage_dir = run_path / "stage-13"
            stage_dir.mkdir(parents=True, exist_ok=True)
            (stage_dir / "refinement_log.json").write_text(
                json.dumps(
                    {
                        "metric_key": "loss",
                        "metric_direction": "minimize",
                        "iterations": [{"metric": 1.0}, {"metric": 0.9}],
                    }
                ),
                encoding="utf-8",
            )
            return _done(stage, ("refinement_log.json",))
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    _ = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-trajectory",
        config=rc_config,
        adapters=adapters,
        from_stage=Stage.ITERATIVE_REFINE,
        to_stage=Stage.RESEARCH_DECISION,
    )

    assert not (run_dir / "evolution" / "trajectory.jsonl").exists()
    assert not (run_dir / "trajectory_signal.json").exists()


def test_execute_pipeline_stops_after_gate_even_when_stop_on_gate_disabled(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    gate_stage = Stage.LITERATURE_SCREEN

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        if stage == gate_stage:
            return _blocked(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-gate-continue",
        config=rc_config,
        adapters=adapters,
        stop_on_gate=False,
    )
    assert len(results) == int(gate_stage)
    assert results[-1].status is StageStatus.BLOCKED_APPROVAL
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    assert (summary["final_status"], summary["final_terminal_action"]) == ("blocked_approval", "block")
    assert summary["final_decision"] == "block"


def test_execute_pipeline_writes_pipeline_summary_json(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-summary",
        config=rc_config,
        adapters=adapters,
    )
    summary_path = run_dir / "pipeline_summary.json"
    assert summary_path.exists()


def test_pipeline_summary_has_expected_fields_and_values(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        if stage == Stage.LITERATURE_SCREEN:
            return _blocked(stage)
        if stage == Stage.HYPOTHESIS_GEN:
            return _failed(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-summary-fields",
        config=rc_config,
        adapters=adapters,
    )
    summary = cast(
        dict[str, Any],
        json.loads((run_dir / "pipeline_summary.json").read_text(encoding="utf-8")),
    )
    assert summary["run_id"] == "run-summary-fields"
    assert summary["stages_executed"] == len(results)
    assert summary["stages_done"] == sum(
        1 for r in results if r.status == StageStatus.DONE
    )
    assert summary["stages_paused"] == 0
    assert summary["stages_blocked"] == 1
    assert summary["stages_failed"] == 0
    assert summary["from_stage"] == 1
    assert summary["final_stage"] == int(Stage.LITERATURE_SCREEN)
    assert summary["final_status"] == "blocked_approval"
    assert "generated" in summary


def test_execute_pipeline_from_stage_skips_earlier_stages(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-from-stage",
        config=rc_config,
        adapters=adapters,
        from_stage=Stage.PAPER_OUTLINE,
    )
    assert seen[0] == Stage.PAPER_OUTLINE
    assert len(seen) == len(STAGE_SEQUENCE) - (int(Stage.PAPER_OUTLINE) - 1)
    assert len(results) == len(seen)


def test_execute_pipeline_writes_kb_entries_when_kb_root_provided(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, str, str]] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        stage_dir = run_dir / f"stage-{int(stage):02d}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "out.md").write_text(f"stage {int(stage)}", encoding="utf-8")
        return _done(stage)

    def mock_write_stage_to_kb(
        kb_root: Path,
        stage_id: int,
        stage_name: str,
        run_id: str,
        artifacts: list[str],
        stage_dir: Path,
        **kwargs,
    ):
        _ = kb_root, artifacts, stage_dir, kwargs
        calls.append((stage_id, stage_name, run_id))
        return []

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(rc_runner, "write_stage_to_kb", mock_write_stage_to_kb)

    kb_root = tmp_path / "kb-out"
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-kb",
        config=rc_config,
        adapters=adapters,
        kb_root=kb_root,
    )
    assert len(results) == 25
    assert len(calls) == 25
    assert calls[0] == (1, "topic_init", "run-kb")


def test_execute_pipeline_passes_auto_approve_flag_to_execute_stage(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    received: list[bool] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        received.append(kwargs["auto_approve_gates"])
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-auto-approve",
        config=rc_config,
        adapters=adapters,
        auto_approve_gates=True,
    )
    assert received
    assert all(received)


@pytest.mark.parametrize(
    ("stage", "started", "expected"),
    [
        (Stage.TOPIC_INIT, False, True),
        (Stage.PROBLEM_DECOMPOSE, False, False),
        (Stage.PAPER_DRAFT, True, True),
    ],
)
def test_should_start_logic(stage: Stage, started: bool, expected: bool) -> None:
    assert rc_runner._should_start(stage, Stage.TOPIC_INIT, started) is expected


@pytest.mark.parametrize(
    ("results", "expected_status", "expected_final_stage"),
    [
        ([], "no_stages", int(Stage.TOPIC_INIT)),
        ([_done(Stage.TOPIC_INIT)], "done", int(Stage.TOPIC_INIT)),
        (
            [_done(Stage.TOPIC_INIT), _paused(Stage.PROBLEM_DECOMPOSE)],
            "paused",
            int(Stage.PROBLEM_DECOMPOSE),
        ),
        (
            [_done(Stage.TOPIC_INIT), _failed(Stage.PROBLEM_DECOMPOSE)],
            "failed",
            int(Stage.PROBLEM_DECOMPOSE),
        ),
    ],
)
def test_build_pipeline_summary_core_fields(
    results, expected_status: str, expected_final_stage: int
) -> None:
    summary = rc_runner._build_pipeline_summary(
        run_id="run-core",
        results=results,
        from_stage=Stage.TOPIC_INIT,
    )
    assert summary["run_id"] == "run-core"
    assert summary["final_status"] == expected_status
    assert summary["final_stage"] == expected_final_stage


@pytest.mark.parametrize("status", [StageStatus.PENDING, StageStatus.RUNNING,
                                    StageStatus.APPROVED, StageStatus.RETRYING])
def test_execute_pipeline_stops_on_every_unexpected_nonadvance_status(
    monkeypatch, run_dir, rc_config, adapters, status) -> None:
    calls = []
    monkeypatch.setattr(rc_runner, "execute_stage", lambda stage, **_:
                        calls.append(stage) or StageResult(stage, status, ()))
    results = rc_runner.execute_pipeline(
        run_dir=run_dir, run_id=f"run-{status.value}", config=rc_config,
        adapters=adapters, to_stage=Stage.PROBLEM_DECOMPOSE)
    assert (calls, len(results), results[0].terminal_action) == (
        [Stage.TOPIC_INIT], 1, "stop")
    attempt = json.loads((run_dir / "attempts/attempt_log.jsonl").read_text())
    assert (attempt["status"], attempt["terminal_action"]) == (status.value, "stop")


def test_failed_noncritical_stage_cannot_advance_when_skip_requested(
    monkeypatch, run_dir, rc_config, adapters) -> None:
    monkeypatch.setattr(rc_runner, "execute_stage", lambda stage, **_: _failed(stage))
    results = rc_runner.execute_pipeline(
        run_dir=run_dir, run_id="run-skip-failed", config=rc_config,
        adapters=adapters, from_stage=Stage.KNOWLEDGE_ARCHIVE,
        to_stage=Stage.EXPORT_PUBLISH, skip_noncritical=True)
    assert [(r.stage, r.status) for r in results] == [
        (Stage.KNOWLEDGE_ARCHIVE, StageStatus.FAILED)]


def test_pipeline_prints_stage_progress(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_results = [
        StageResult(
            stage=Stage.TOPIC_INIT, status=StageStatus.DONE, artifacts=("topic.json",)
        ),
        StageResult(
            stage=Stage.PROBLEM_DECOMPOSE,
            status=StageStatus.DONE,
            artifacts=("tree.json",),
        ),
        StageResult(
            stage=Stage.SEARCH_STRATEGY,
            status=StageStatus.FAILED,
            artifacts=(),
            error="LLM timeout",
        ),
    ]

    call_idx = 0

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = stage, kwargs
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        return mock_results[min(idx, len(mock_results) - 1)]

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(rc_runner, "write_stage_to_kb", lambda *args, **kwargs: [])

    _ = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="rc-test-001",
        config=rc_config,
        adapters=adapters,
    )

    captured = capsys.readouterr()
    assert "TOPIC_INIT — running..." in captured.out
    assert "TOPIC_INIT — done" in captured.out
    assert "SEARCH_STRATEGY — FAILED" in captured.out
    assert "LLM timeout" in captured.out


def test_pipeline_prints_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_result = StageResult(
        stage=Stage.TOPIC_INIT,
        status=StageStatus.DONE,
        artifacts=("topic.json",),
    )
    mock_fail = StageResult(
        stage=Stage.PROBLEM_DECOMPOSE,
        status=StageStatus.FAILED,
        artifacts=(),
        error="test",
    )
    results_iter = iter([mock_result, mock_fail])

    monkeypatch.setattr(
        rc_runner, "execute_stage", lambda *args, **kwargs: next(results_iter)
    )
    monkeypatch.setattr(rc_runner, "write_stage_to_kb", lambda *args, **kwargs: [])

    _ = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="rc-test-002",
        config=rc_config,
        adapters=adapters,
    )

    captured = capsys.readouterr()
    import re

    assert re.search(r"\d+\.\d+s\)", captured.out), (
        f"No elapsed time found in: {captured.out}"
    )


# ── PIVOT/PROCEED/REFINE decision loop tests ──


def _pivot_result(stage: Stage) -> StageResult:
    return StageResult(
        stage=stage, status=StageStatus.DONE, artifacts=("decision.md",), decision="pivot"
    )


def _refine_result(stage: Stage) -> StageResult:
    return StageResult(
        stage=stage, status=StageStatus.DONE, artifacts=("decision.md",), decision="refine"
    )


def test_pivot_decision_triggers_rollback_to_hypothesis_gen(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []
    pivot_count = 0

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        nonlocal pivot_count
        if stage == Stage.RESEARCH_DECISION and pivot_count == 0:
            pivot_count += 1
            return _pivot_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-pivot",
        config=rc_config,
        adapters=adapters,
    )
    # Should have seen HYPOTHESIS_GEN at least twice (original + rollback)
    hyp_gen_count = sum(1 for s in seen if s == Stage.HYPOTHESIS_GEN)
    assert hyp_gen_count >= 2
    # Decision history should be recorded
    history_path = run_dir / "decision_history.json"
    assert history_path.exists()
    history = json.loads(history_path.read_text())
    assert len(history) == 1
    assert history[0]["decision"] == "pivot"


def test_refine_decision_triggers_rollback_to_iterative_refine(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []
    refine_count = 0

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        nonlocal refine_count
        if stage == Stage.RESEARCH_DECISION and refine_count == 0:
            refine_count += 1
            return _refine_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-refine",
        config=rc_config,
        adapters=adapters,
    )
    # Should have seen ITERATIVE_REFINE at least twice
    refine_stage_count = sum(1 for s in seen if s == Stage.ITERATIVE_REFINE)
    assert refine_stage_count >= 2


def test_max_pivot_count_prevents_infinite_loop(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []
    promotion_calls = 0

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        # Always PIVOT — should be limited by MAX_DECISION_PIVOTS
        if stage == Stage.RESEARCH_DECISION:
            return _pivot_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)

    def _promotion_spy(*_args: object, **_kwargs: object) -> None:
        nonlocal promotion_calls
        promotion_calls += 1

    monkeypatch.setattr(rc_runner, "_promote_best_stage14", _promotion_spy)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-max-pivot",
        config=rc_config,
        adapters=adapters,
    )
    # RESEARCH_DECISION should appear at most MAX_DECISION_PIVOTS + 1 times
    from researchclaw.pipeline.stages import MAX_DECISION_PIVOTS
    decision_count = sum(1 for s in seen if s == Stage.RESEARCH_DECISION)
    assert decision_count <= MAX_DECISION_PIVOTS + 1
    assert Stage.PAPER_OUTLINE not in seen
    assert results[-1].stage is Stage.RESEARCH_DECISION
    assert results[-1].status is StageStatus.FAILED
    assert results[-1].decision == "refinement_exhausted"
    assert "max_refinement_attempts" in (results[-1].error or "")
    assert promotion_calls == 0
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    assert summary["final_stage"] == int(Stage.RESEARCH_DECISION)
    assert summary["final_status"] == "failed"
    assert summary["final_decision"] == "refinement_exhausted"
    assert "max_refinement_attempts" in summary["final_error"]
    assert rc_runner.read_checkpoint(run_dir) is None
    assert not (run_dir / "heartbeat.json").exists()


def test_refinement_exhaustion_invalidates_downstream_commit_points(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external-manifest"
    external.write_text("EXTERNAL", encoding="utf-8")
    for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        stage_dir = run_dir / stage_name
        stage_dir.mkdir()
        (stage_dir / "diagnostic.txt").write_text("keep", encoding="utf-8")
        for name in names:
            (stage_dir / name).write_text("authority", encoding="utf-8")
    stage24_manifest = run_dir / "stage-24" / "stage24_truth_manifest.json"
    stage24_manifest.unlink()
    stage24_manifest.symlink_to(external)

    with ReleaseGraphLock.acquire(run_dir, "test-exhaustion-cleanup") as release_lock:
        with ExitStack() as stack:
            namespaces = rc_runner._capture_refinement_exhaustion_namespaces(
                run_dir, release_lock, stack
            )
            rc_runner._invalidate_refinement_exhaustion_authority(
                run_dir, release_lock, namespaces
            )

    for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        stage_dir = run_dir / stage_name
        assert (stage_dir / "diagnostic.txt").read_text(encoding="utf-8") == "keep"
        assert all(not (stage_dir / name).exists() for name in names)
    assert external.read_text(encoding="utf-8") == "EXTERNAL"


def test_consecutive_empty_metrics_fail_before_stage16(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        seen.append(stage)
        if stage is Stage.RESEARCH_DECISION:
            return _refine_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(
        rc_runner,
        "_read_pivot_count_bound",
        lambda _run_dir, _release_lock: 1,
    )
    monkeypatch.setattr(
        rc_runner,
        "_consecutive_empty_metrics_bound",
        lambda _run_dir, _count, _release_lock: True,
    )
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-empty-refine",
        config=rc_config,
        adapters=adapters,
    )

    assert Stage.PAPER_OUTLINE not in seen
    assert results[-1].status is StageStatus.FAILED
    assert results[-1].decision == "refinement_exhausted"
    assert "consecutive_empty_metrics" in (results[-1].error or "")
    assert rc_runner.read_checkpoint(run_dir) is None
    assert not (run_dir / "heartbeat.json").exists()


def test_refinement_exhaustion_cleanup_continues_after_collision(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        stage_dir = run_dir / stage_name
        stage_dir.mkdir()
        for name in names:
            (stage_dir / name).write_text("authority", encoding="utf-8")
    collision = run_dir / "stage-16" / "outline_binding.json"
    collision.unlink()
    collision.mkdir()
    (collision / "nested").mkdir()

    with ReleaseGraphLock.acquire(run_dir, "test-exhaustion-collision") as release_lock:
        with ExitStack() as stack:
            namespaces = rc_runner._capture_refinement_exhaustion_namespaces(
                run_dir, release_lock, stack
            )
            with pytest.raises(OSError, match="cleanup was incomplete"):
                rc_runner._invalidate_refinement_exhaustion_authority(
                    run_dir, release_lock, namespaces
                )

    assert collision.is_dir()
    assert not (run_dir / "stage-16" / "outline.md").exists()
    for stage_name in tuple(rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS)[1:]:
        names = rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS[stage_name]
        assert all(not (run_dir / stage_name / name).exists() for name in names)


def _write_refinement_exhaustion_authority(run_dir: Path) -> None:
    for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        stage_dir = run_dir / stage_name
        stage_dir.mkdir(exist_ok=True)
        (stage_dir / "diagnostic.txt").write_text("keep", encoding="utf-8")
        for name in names:
            (stage_dir / name).write_text("authority", encoding="utf-8")


def test_refinement_exhaustion_run_parent_replacement_cleans_detached_only(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    _write_refinement_exhaustion_authority(run_dir)
    moved = run_dir.with_name("run-moved")
    replacement_files: dict[Path, bytes] = {}

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            return _refine_result(stage)
        return _done(stage)

    def replace_run(_run_dir: Path, _release_lock: object) -> int:
        run_dir.rename(moved)
        run_dir.mkdir()
        for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
            stage_dir = run_dir / stage_name
            stage_dir.mkdir()
            sentinel = stage_dir / "sentinel.txt"
            authority = stage_dir / names[0]
            sentinel.write_text("EXTERNAL", encoding="utf-8")
            authority.write_text("EXTERNAL-AUTHORITY", encoding="utf-8")
            replacement_files[sentinel] = sentinel.read_bytes()
            replacement_files[authority] = authority.read_bytes()
        return rc_runner.MAX_DECISION_PIVOTS

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(rc_runner, "_read_pivot_count_bound", replace_run)

    with pytest.raises(RuntimeError, match="release_graph_run_directory_changed"):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="run-parent-replaced",
            config=rc_config,
            adapters=adapters,
        )

    assert all(path.read_bytes() == content for path, content in replacement_files.items())
    for stage_name, names in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        detached_stage = moved / stage_name
        assert all(not (detached_stage / name).exists() for name in names)
        assert (detached_stage / "diagnostic.txt").read_text(encoding="utf-8") == "keep"


def test_refinement_exhaustion_stage_parent_replacement_cleans_detached_only(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    _write_refinement_exhaustion_authority(run_dir)
    stage16 = run_dir / "stage-16"
    detached = run_dir / "stage-16-moved"

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            return _refine_result(stage)
        return _done(stage)

    def replace_stage(_run_dir: Path, _release_lock: object) -> int:
        stage16.rename(detached)
        stage16.mkdir()
        (stage16 / "sentinel.txt").write_text("EXTERNAL", encoding="utf-8")
        (stage16 / "outline_binding.json").write_text(
            "EXTERNAL-AUTHORITY", encoding="utf-8"
        )
        return rc_runner.MAX_DECISION_PIVOTS

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(rc_runner, "_read_pivot_count_bound", replace_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="stage-parent-replaced",
        config=rc_config,
        adapters=adapters,
    )

    assert results[-1].status is StageStatus.FAILED
    assert results[-1].decision == "refinement_exhausted"
    assert "cleanup failed" in (results[-1].error or "")
    assert (stage16 / "sentinel.txt").read_text(encoding="utf-8") == "EXTERNAL"
    assert (stage16 / "outline_binding.json").read_text(encoding="utf-8") == (
        "EXTERNAL-AUTHORITY"
    )
    assert not (detached / "outline_binding.json").exists()
    assert (detached / "diagnostic.txt").read_text(encoding="utf-8") == "keep"


def test_nonexhausted_refine_parent_replacement_rejects_before_live_writes(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    moved = run_dir.with_name("run-moved")
    replacement = run_dir
    real_record = rc_runner._record_decision_history_bound

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            return _refine_result(stage)
        return _done(stage)

    def replace_before_history(*args: object, **kwargs: object) -> None:
        run_dir.rename(moved)
        replacement.mkdir()
        (replacement / "sentinel.txt").write_text("EXTERNAL", encoding="utf-8")
        (replacement / "decision_history.json").write_text(
            "EXTERNAL-HISTORY", encoding="utf-8"
        )
        real_record(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(
        rc_runner, "_record_decision_history_bound", replace_before_history
    )

    with pytest.raises(RuntimeError, match="release_graph_run_directory_changed"):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="nonexhausted-parent-replaced",
            config=rc_config,
            adapters=adapters,
        )

    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == "EXTERNAL"
    assert (replacement / "decision_history.json").read_text(encoding="utf-8") == (
        "EXTERNAL-HISTORY"
    )


def test_internal_rollback_suppresses_runner_path_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    from researchclaw.pipeline import release_artifacts

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("internal rollback reopened a runner-level path")

    monkeypatch.setattr(rc_runner, "execute_stage", lambda stage, **_kwargs: _done(stage))
    monkeypatch.setattr(rc_runner, "_apply_injected_artifacts", forbidden)
    monkeypatch.setattr(rc_runner, "_write_checkpoint", forbidden)
    monkeypatch.setattr(rc_runner, "_write_heartbeat", forbidden)
    monkeypatch.setattr(rc_runner, "_build_pipeline_summary", forbidden)
    monkeypatch.setattr(rc_runner, "_write_pipeline_summary", forbidden)
    monkeypatch.setattr(release_artifacts, "append_attempt", forbidden)
    monkeypatch.setattr(release_artifacts, "append_cost_entry", forbidden)

    with ReleaseGraphLock.acquire(run_dir, "internal-rollback") as release_lock:
        results = rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="internal-rollback",
            config=rc_config,
            adapters=adapters,
            from_stage=Stage.RESEARCH_DECISION,
            to_stage=Stage.RESEARCH_DECISION,
            _release_lock=release_lock,
            _internal_rollback=True,
        )

    assert [result.stage for result in results] == [Stage.RESEARCH_DECISION]
    assert not (run_dir / "injected_artifacts.json").exists()
    assert not (run_dir / "checkpoint.json").exists()
    assert not (run_dir / "heartbeat.json").exists()
    assert not (run_dir / "pipeline_summary.json").exists()


def test_generic_pivot_internal_rollback_publishes_allowed_skip_contract(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    cfg = dataclasses.replace(
        rc_config,
        runtime=dataclasses.replace(rc_config.runtime, skip_stages=(10,)),
    )
    decision_count = 0

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        nonlocal decision_count
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            decision_count += 1
            if decision_count == 1:
                return _pivot_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="pivot-skip-contract",
        config=cfg,
        adapters=adapters,
        from_stage=Stage.RESEARCH_DECISION,
    )

    skipped_marker = run_dir / "stage-10" / "experiment" / "SKIPPED.md"
    assert "was skipped by runtime.skip_stages" in skipped_marker.read_text(
        encoding="utf-8"
    )
    assert (run_dir / "stage-10" / "experiment_spec.md").is_file()
    skipped = [
        result
        for result in results
        if result.stage is Stage.CODE_GENERATION and result.decision == "skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].artifacts == ("experiment/", "experiment_spec.md")


def test_internal_skip_publisher_uses_held_inode_after_parent_replacement(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    cfg = dataclasses.replace(
        rc_config,
        runtime=dataclasses.replace(rc_config.runtime, skip_stages=(10,)),
    )
    moved = run_dir.with_name("run-moved")
    decision_count = 0
    real_publish = rc_runner._write_skipped_stage_outputs_bound

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        nonlocal decision_count
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            decision_count += 1
            if decision_count == 1:
                return _pivot_result(stage)
        return _done(stage)

    def replace_before_skip(*args: object, **kwargs: object) -> tuple[str, ...]:
        run_dir.rename(moved)
        run_dir.mkdir()
        (run_dir / "sentinel.txt").write_text("EXTERNAL", encoding="utf-8")
        return real_publish(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(
        rc_runner, "_write_skipped_stage_outputs_bound", replace_before_skip
    )

    with pytest.raises(RuntimeError, match="release_graph_run_directory_changed"):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="pivot-skip-replaced",
            config=cfg,
            adapters=adapters,
            from_stage=Stage.RESEARCH_DECISION,
        )

    assert {path.name for path in run_dir.iterdir()} == {"sentinel.txt"}
    assert (run_dir / "sentinel.txt").read_text(encoding="utf-8") == "EXTERNAL"
    assert not (moved / "stage-10" / "experiment" / "SKIPPED.md").exists()


def test_recursive_rollback_parent_replacement_has_zero_replacement_writes(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    moved = run_dir.with_name("run-moved")
    decision_count = 0

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        nonlocal decision_count
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            decision_count += 1
            if decision_count == 1:
                return _refine_result(stage)
        if stage is Stage.DEAI_AUDIT:
            run_dir.rename(moved)
            run_dir.mkdir()
            (run_dir / "sentinel.txt").write_text("EXTERNAL", encoding="utf-8")
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)

    with pytest.raises(RuntimeError, match="release_graph_run_directory_changed"):
        rc_runner.execute_pipeline(
            run_dir=run_dir,
            run_id="recursive-parent-replaced",
            config=rc_config,
            adapters=adapters,
            from_stage=Stage.RESEARCH_DECISION,
        )

    assert {path.name for path in run_dir.iterdir()} == {"sentinel.txt"}
    assert (run_dir / "sentinel.txt").read_text(encoding="utf-8") == "EXTERNAL"


def test_pivot_count_uses_held_inode_during_a_b_a_replacement(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    moved = run_dir.with_name("run-moved")
    replacement = run_dir.with_name("replacement-moved")
    real_read = rc_runner._read_run_regular_file_bound
    switched = False

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            return _pivot_result(stage)
        return _done(stage)

    def switch_around_read(
        current_run: Path, release_lock: object, relative_path: str
    ) -> bytes | None:
        nonlocal switched
        if relative_path == "decision_history.json" and not switched:
            switched = True
            run_dir.rename(moved)
            run_dir.mkdir()
            (run_dir / "decision_history.json").write_text(
                json.dumps([{}, {}]), encoding="utf-8"
            )
            run_dir.rename(replacement)
            moved.rename(run_dir)
        return real_read(current_run, release_lock, relative_path)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    monkeypatch.setattr(
        rc_runner, "_read_run_regular_file_bound", switch_around_read
    )
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="a-b-a-pivot",
        config=rc_config,
        adapters=adapters,
        to_stage=Stage.RESEARCH_DECISION,
    )

    assert results[-1].status is StageStatus.DONE
    assert results[-1].decision == "pivot"
    assert rc_runner.read_checkpoint(run_dir) is None
    assert json.loads((replacement / "decision_history.json").read_text()) == [{}, {}]


@pytest.mark.parametrize("decision", ("refine", "pivot"))
def test_stage15_rollback_invalidates_stale_stage16_resume_pointers(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    decision: str,
) -> None:
    rc_runner._write_checkpoint(run_dir, Stage.RESEARCH_DECISION, "stale")
    rc_runner._write_heartbeat(run_dir, Stage.RESEARCH_DECISION, "stale")

    def mock_execute_stage(stage: Stage, **kwargs: object) -> StageResult:
        del kwargs
        if stage is Stage.RESEARCH_DECISION:
            return _refine_result(stage) if decision == "refine" else _pivot_result(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id=f"stale-{decision}",
        config=rc_config,
        adapters=adapters,
        from_stage=Stage.RESEARCH_DECISION,
        to_stage=Stage.RESEARCH_DECISION,
    )

    assert results[-1].decision == decision
    assert rc_runner.read_checkpoint(run_dir) is None
    assert not (run_dir / "checkpoint.json").exists()
    assert not (run_dir / "heartbeat.json").exists()


def test_exhaustion_invalidator_rejects_foreign_namespace(
    tmp_path: Path,
) -> None:
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    run_a.mkdir()
    run_b.mkdir()
    stage_b = run_b / "stage-16"
    stage_b.mkdir()
    authority = stage_b / "outline_binding.json"
    authority.write_text("B", encoding="utf-8")

    with ReleaseGraphLock.acquire(run_b, "capture-foreign") as lock_b:
        namespace_b = lock_b.open_stage_namespace("stage-16")
    namespaces: dict[str, object | None] = {
        name: None for name in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS
    }
    namespaces["stage-16"] = namespace_b
    try:
        with ReleaseGraphLock.acquire(run_a, "reject-foreign") as lock_a:
            with pytest.raises(RuntimeError, match="namespace binding mismatch"):
                rc_runner._invalidate_refinement_exhaustion_authority(
                    run_a, lock_a, namespaces  # type: ignore[arg-type]
                )
    finally:
        namespace_b.close()

    assert authority.read_text(encoding="utf-8") == "B"


@pytest.mark.parametrize("foreign_stage", ("stage-17", "stage-25"))
def test_exhaustion_invalidator_validates_all_before_deleting_foreign_namespace(
    tmp_path: Path,
    foreign_stage: str,
) -> None:
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    run_a.mkdir()
    run_b.mkdir()
    stage16_a = run_a / "stage-16"
    stage16_a.mkdir()
    authority_a = stage16_a / "outline_binding.json"
    authority_a.write_text("A", encoding="utf-8")
    (run_b / foreign_stage).mkdir()

    with ReleaseGraphLock.acquire(run_a, "validate-first-a") as lock_a:
        with ReleaseGraphLock.acquire(run_b, "validate-first-b") as lock_b:
            with ExitStack() as stack:
                namespaces: dict[str, object | None] = {
                    name: None
                    for name in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS
                }
                namespaces["stage-16"] = stack.enter_context(
                    lock_a.open_stage_namespace("stage-16")
                )
                namespaces[foreign_stage] = stack.enter_context(
                    lock_b.open_stage_namespace(foreign_stage)
                )
                with pytest.raises(RuntimeError, match="namespace binding mismatch"):
                    rc_runner._invalidate_refinement_exhaustion_authority(
                        run_a, lock_a, namespaces  # type: ignore[arg-type]
                    )

    assert authority_a.read_text(encoding="utf-8") == "A"


def test_exhaustion_invalidator_validates_all_before_deleting_wrong_stage(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for stage_name in ("stage-16", "stage-18"):
        (run_dir / stage_name).mkdir()
    authority = run_dir / "stage-16" / "outline_binding.json"
    authority.write_text("A", encoding="utf-8")

    with ReleaseGraphLock.acquire(run_dir, "validate-wrong-stage") as release_lock:
        with ExitStack() as stack:
            namespaces: dict[str, object | None] = {
                name: None
                for name in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS
            }
            namespaces["stage-16"] = stack.enter_context(
                release_lock.open_stage_namespace("stage-16")
            )
            namespaces["stage-17"] = stack.enter_context(
                release_lock.open_stage_namespace("stage-18")
            )
            with pytest.raises(RuntimeError, match="namespace binding mismatch"):
                rc_runner._invalidate_refinement_exhaustion_authority(
                    run_dir, release_lock, namespaces  # type: ignore[arg-type]
                )

    assert authority.read_text(encoding="utf-8") == "A"


@pytest.mark.parametrize("incremental", (False, True))
def test_bound_rollback_versioning_uses_held_run_inode(
    tmp_path: Path, incremental: bool
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for stage_num in range(int(Stage.ITERATIVE_REFINE), int(Stage.RESEARCH_DECISION) + 1):
        stage_dir = run_dir / f"stage-{stage_num:02d}"
        stage_dir.mkdir()
        (stage_dir / "payload.txt").write_text(
            f"stage-{stage_num}", encoding="utf-8"
        )

    with ReleaseGraphLock.acquire(run_dir, "bound-versioning") as release_lock:
        rc_runner._version_rollback_stages_bound(
            run_dir,
            Stage.ITERATIVE_REFINE,
            1,
            release_lock,
            incremental=incremental,
        )

    for stage_num in range(int(Stage.ITERATIVE_REFINE), int(Stage.RESEARCH_DECISION) + 1):
        stage_name = f"stage-{stage_num:02d}"
        archived = run_dir / f"{stage_name}_v1" / "payload.txt"
        assert archived.read_text(encoding="utf-8") == f"stage-{stage_num}"
        live = run_dir / stage_name / "payload.txt"
        if incremental:
            assert live.read_text(encoding="utf-8") == f"stage-{stage_num}"
        else:
            assert not live.exists()


@pytest.mark.parametrize("lease_kind", ("fake", "reader", "inactive"))
def test_exhaustion_invalidator_rejects_untrusted_writer_lease(
    tmp_path: Path, lease_kind: str
) -> None:
    run_dir = tmp_path / lease_kind
    run_dir.mkdir()
    namespaces = {
        name: None for name in rc_runner._REFINEMENT_EXHAUSTION_COMMIT_POINTS
    }
    if lease_kind == "fake":
        lease: object = object()
    else:
        acquired = ReleaseGraphLock.acquire(
            run_dir, lease_kind, mode="read" if lease_kind == "reader" else "write"
        )
        lease = acquired
        if lease_kind == "inactive":
            acquired.close()
    try:
        with pytest.raises(RuntimeError, match="writer_lease_required|lease_inactive"):
            rc_runner._invalidate_refinement_exhaustion_authority(
                run_dir, lease, namespaces
            )
    finally:
        if lease_kind == "reader":
            acquired.close()


def test_proceed_decision_does_not_trigger_rollback(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
) -> None:
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-proceed",
        config=rc_config,
        adapters=adapters,
    )
    # Should be exactly 25 stages, no rollback
    assert len(seen) == 25
    assert not (run_dir / "decision_history.json").exists()


def test_read_pivot_count_returns_zero_for_no_history(run_dir: Path) -> None:
    assert rc_runner._read_pivot_count(run_dir) == 0


def test_record_decision_history_appends(run_dir: Path) -> None:
    rc_runner._record_decision_history(run_dir, "pivot", Stage.HYPOTHESIS_GEN, 1)
    rc_runner._record_decision_history(run_dir, "refine", Stage.ITERATIVE_REFINE, 2)
    history = json.loads((run_dir / "decision_history.json").read_text())
    assert len(history) == 2
    assert history[0]["decision"] == "pivot"
    assert history[1]["decision"] == "refine"


# ── Deliverables packaging tests ──


def _setup_stage_artifacts(run_dir: Path) -> None:
    """Create typical stage-22 and stage-23 output files for testing."""
    s22 = run_dir / "stage-22"
    s22.mkdir(parents=True, exist_ok=True)
    (s22 / "paper_final.md").write_text("# My Paper\nContent here.", encoding="utf-8")
    (s22 / "paper.tex").write_text("\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}", encoding="utf-8")
    (s22 / "references.bib").write_text("@article{smith2024,\n  title={Test}\n}", encoding="utf-8")
    code_dir = s22 / "code"
    code_dir.mkdir()
    (code_dir / "main.py").write_text("print('hello')", encoding="utf-8")
    (code_dir / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    (code_dir / "README.md").write_text("# Code\n", encoding="utf-8")

    s23 = run_dir / "stage-23"
    s23.mkdir(parents=True, exist_ok=True)
    (s23 / "paper_final_verified.md").write_text("# My Paper (verified)\nContent.", encoding="utf-8")
    (s23 / "references_verified.bib").write_text("@article{smith2024,\n  title={Test}\n}", encoding="utf-8")
    (s23 / "verification_report.json").write_text(
        json.dumps({"summary": {"total": 5, "verified": 4}}), encoding="utf-8"
    )


def test_package_deliverables_collects_all_artifacts(
    run_dir: Path, rc_config: RCConfig
) -> None:
    _setup_stage_artifacts(run_dir)
    (run_dir / "pipeline_summary.json").write_text(json.dumps({
        "degraded": False, "stages_failed": 0, "stages_blocked": 1,
        "final_stage": 25, "final_status": "done",
    }))
    dest = rc_runner._package_deliverables(run_dir, "run-pkg-test", rc_config)
    assert dest is not None
    assert dest == run_dir / "deliverables"
    assert (dest / "paper_final.md").exists()
    assert (dest / "paper.tex").exists()
    assert (dest / "references.bib").exists()
    assert (dest / "code" / "main.py").exists()
    assert (dest / "verification_report.json").exists()
    assert (dest / "manifest.json").exists()
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["run_id"] == "run-pkg-test"
    assert "paper_final.md" in manifest["files"]
    assert (manifest["release_ready"], "stages_blocked" in manifest["release_blockers"]) == (False, True)


def test_package_deliverables_prefers_verified_versions(
    run_dir: Path, rc_config: RCConfig
) -> None:
    _setup_stage_artifacts(run_dir)
    rc_runner._package_deliverables(run_dir, "run-verified", rc_config)
    dest = run_dir / "deliverables"
    # Should contain verified content (from stage 23), not base (from stage 22)
    paper = (dest / "paper_final.md").read_text(encoding="utf-8")
    assert "verified" in paper
    bib = (dest / "references.bib").read_text(encoding="utf-8")
    assert "smith2024" in bib


def test_package_deliverables_falls_back_to_stage22(
    run_dir: Path, rc_config: RCConfig
) -> None:
    """When stage 23 outputs are missing, falls back to stage 22 versions."""
    s22 = run_dir / "stage-22"
    s22.mkdir(parents=True, exist_ok=True)
    (s22 / "paper_final.md").write_text("# Base Paper", encoding="utf-8")
    (s22 / "references.bib").write_text("@article{a,title={A}}", encoding="utf-8")

    dest = rc_runner._package_deliverables(run_dir, "run-fallback", rc_config)
    assert dest is not None
    paper = (dest / "paper_final.md").read_text(encoding="utf-8")
    assert "Base Paper" in paper


def test_package_deliverables_returns_none_when_no_stage_artifacts(
    run_dir: Path, tmp_path: Path,
) -> None:
    """Returns None when no stage artifacts exist and no style files found."""
    # Use a config with an unknown conference so style files aren't bundled
    data = {
        "project": {"name": "empty-test", "mode": "docs-first"},
        "research": {"topic": "empty"},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "local"},
        "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
        "openclaw_bridge": {},
        "llm": {
            "provider": "openai-compatible",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "RC_TEST_KEY",
            "api_key": "inline",
        },
        "export": {"target_conference": "unknown_conf_9999"},
    }
    cfg = RCConfig.from_dict(data, project_root=tmp_path, check_paths=False)
    result = rc_runner._package_deliverables(run_dir, "run-empty", cfg)
    assert result is None
    assert not (run_dir / "deliverables").exists()


def test_package_deliverables_includes_style_files(
    run_dir: Path, rc_config: RCConfig
) -> None:
    """Style files (.sty, .bst) for the target conference are bundled."""
    _setup_stage_artifacts(run_dir)
    dest = rc_runner._package_deliverables(run_dir, "run-styles", rc_config)
    assert dest is not None
    # Default config uses neurips_2025 → should have neurips_2025.sty
    assert (dest / "neurips_2025.sty").exists()
    manifest = json.loads((dest / "manifest.json").read_text())
    assert "neurips_2025.sty" in manifest["files"]


# ── Atomic checkpoint write tests ──


def test_write_checkpoint_uses_atomic_rename(run_dir: Path) -> None:
    """Checkpoint must be written via temp file + rename, not direct write"""
    rc_runner._write_checkpoint(run_dir, Stage.TOPIC_INIT, "run-atomic")
    cp = run_dir / "checkpoint.json"
    assert cp.exists()
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["last_completed_stage"] == int(Stage.TOPIC_INIT)
    assert data["run_id"] == "run-atomic"


def test_write_checkpoint_leaves_no_temp_files(run_dir: Path) -> None:
    """Atomic write must clean up temp files on success"""
    rc_runner._write_checkpoint(run_dir, Stage.TOPIC_INIT, "run-clean")
    temps = list(run_dir.glob("*.tmp"))
    assert temps == [], f"Leftover temp files: {temps}"


def test_write_checkpoint_preserves_old_on_write_failure(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the temp-file write fails, the existing checkpoint must survive"""
    import builtins

    rc_runner._write_checkpoint(run_dir, Stage.TOPIC_INIT, "run-ok")

    original_open = builtins.open

    def _exploding_open(path, *args, **kwargs):
        # After os.close(fd), _write_checkpoint opens via path string —
        # intercept temp-file opens (checkpoint_*.tmp)
        if isinstance(path, (str, Path)) and "checkpoint_" in str(path):
            raise OSError("disk full")
        if isinstance(path, int):
            raise OSError("disk full")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _exploding_open)
    with pytest.raises(OSError):
        rc_runner._write_checkpoint(run_dir, Stage.PROBLEM_DECOMPOSE, "run-ok")

    # Original checkpoint must be intact
    data = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert data["last_completed_stage"] == int(Stage.TOPIC_INIT)
    # Temp file must be cleaned up
    assert list(run_dir.glob("checkpoint_*.tmp")) == []


def test_write_checkpoint_overwrites_previous(run_dir: Path) -> None:
    """A second checkpoint call must fully replace the first"""
    rc_runner._write_checkpoint(run_dir, Stage.TOPIC_INIT, "run-1")
    rc_runner._write_checkpoint(run_dir, Stage.PROBLEM_DECOMPOSE, "run-1")
    data = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert data["last_completed_stage"] == int(Stage.PROBLEM_DECOMPOSE)
    assert data["last_completed_name"] == Stage.PROBLEM_DECOMPOSE.name


def _degraded(stage: Stage) -> StageResult:
    return StageResult(
        stage=stage,
        status=StageStatus.DONE,
        artifacts=("quality_report.json",),
        decision="degraded",
    )


def test_degraded_quality_gate_continues_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When quality gate returns decision='degraded', pipeline continues to completion."""
    seen: list[Stage] = []

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        _ = kwargs
        seen.append(stage)
        if stage == Stage.QUALITY_GATE:
            return _degraded(stage)
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    results = rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-degraded",
        config=rc_config,
        adapters=adapters,
    )
    # All 25 stages should execute (not stopped at quality gate)
    assert len(results) == 25
    assert seen == list(STAGE_SEQUENCE)
    # Quality gate result should have decision="degraded"
    qg_result = [r for r in results if r.stage == Stage.QUALITY_GATE][0]
    assert qg_result.decision == "degraded"
    assert qg_result.status == StageStatus.DONE
    # Pipeline summary should have degraded=True
    summary = json.loads((run_dir / "pipeline_summary.json").read_text())
    assert summary["degraded"] is True
    # Output should show DEGRADED message
    captured = capsys.readouterr()
    assert "DEGRADED" in captured.out


def test_package_deliverables_called_after_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    rc_config: RCConfig,
    adapters: AdapterBundle,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deliverables packaging is called at end of execute_pipeline."""
    _setup_stage_artifacts(run_dir)

    def mock_execute_stage(stage: Stage, **kwargs) -> StageResult:
        return _done(stage)

    monkeypatch.setattr(rc_runner, "execute_stage", mock_execute_stage)
    rc_runner.execute_pipeline(
        run_dir=run_dir,
        run_id="run-with-deliverables",
        config=rc_config,
        adapters=adapters,
    )
    captured = capsys.readouterr()
    assert "Deliverables packaged" in captured.out
    assert (run_dir / "deliverables" / "manifest.json").exists()
