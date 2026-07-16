from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

import researchclaw.pipeline.canonical_execution_controller as controller_module
import researchclaw.pipeline.release_graph_lock as release_graph_lock_module
from researchclaw.pipeline.stage_impls import _execution
from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    derive_contract,
    dump_contract,
    load_contract,
    sha256_file,
)
from researchclaw.experiment_runtime.scaffold import render_main_py
from researchclaw.pipeline.stage_impls._execution import (
    _execute_experiment_run,
    _execute_legacy_experiment_run,
    _load_sealed_candidate,
    _scaffold_sha256,
)
from researchclaw.pipeline.stage_impls._code_generation import (
    _execute_code_generation,
    _seal_selected_candidate,
)
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.contracts import CONTRACTS
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalExecutionController,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    parse_execution_invocation_journal,
    validate_experiment_result_set,
)


def _cfg(tmp_path: Path) -> RCConfig:
    return RCConfig.from_dict(
        {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {
                "topic": "Hardware-performance-counter detection of Spectre attacks",
                "domains": ["security"],
            },
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "none"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 30,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "sandbox": {"python_path": sys.executable},
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )


def _write_contract(run: Path, cfg: RCConfig) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    snapshot = run / "config.yaml"
    if not snapshot.exists():
        raw = json.loads(json.dumps(cfg.to_dict()))
        snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        write_active_config_binding(run, snapshot)
    path = run / "stage-09" / "experiment_contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    contract = derive_contract(
        cfg,
        {"datasets": ["synthetic traces"]},
        stage_dir=path.parent,
    )
    dump_contract(contract, path)
    return path


def _write_selected_candidate(run: Path, cfg: RCConfig) -> Path:
    contract_path = _write_contract(run, cfg)
    experiment = run / "stage-10" / "experiment"
    experiment.mkdir(parents=True, exist_ok=True)
    (experiment / "main.py").write_text(
        render_main_py(load_contract(contract_path)),
        encoding="utf-8",
    )
    (experiment / "detector_plugin.py").write_text(
        "def fit(X, y):\n    return None\n\ndef predict(X):\n    return [0] * len(X)\n",
        encoding="utf-8",
    )
    _seal_selected_candidate(run / "stage-10", experiment, contract_path, cfg)
    return run / "stage-10" / "selected_candidate"


def test_stage12_rejects_when_manifest_missing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-10" / "selected_candidate").mkdir(parents=True)

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _execute_experiment_run(run / "stage-12", run, cfg, AdapterBundle())


def test_canonical_stage12_contract_has_no_generic_inputs_or_retries() -> None:
    contract = CONTRACTS[Stage.EXPERIMENT_RUN]
    assert contract.input_files == ()
    assert contract.max_retries == 0


def test_stage12_loads_valid_sealed_candidate(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)

    assert _load_sealed_candidate(run, cfg) == selected


def test_stage12_rejects_contract_hash_mismatch(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_selected_candidate(run, cfg)
    manifest_path = run / "stage-10" / "selected_candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contract_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="contract hash mismatch"):
        _load_sealed_candidate(run, cfg)


def test_stage12_rejects_scaffold_hash_mismatch(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_selected_candidate(run, cfg)
    manifest_path = run / "stage-10" / "selected_candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scaffold_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="scaffold hash mismatch"):
        _load_sealed_candidate(run, cfg)


def test_stage12_rejects_file_hash_mismatch(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)
    (selected / "main.py").write_text("print('tampered')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        _load_sealed_candidate(run, cfg)


def test_stage12_rejects_unmanifested_file(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)
    (selected / "helper.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="file-set mismatch"):
        _load_sealed_candidate(run, cfg)


@pytest.mark.parametrize("name", ["results.json", "smoke_results.json", "metrics.json"])
def test_stage12_rejects_banned_files(tmp_path: Path, name: str) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)
    path = selected / name
    path.write_text("{}", encoding="utf-8")
    manifest_path = run / "stage-10" / "selected_candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][name] = {"sha256": sha256_file(path)}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Python files|owner maps"):
        _load_sealed_candidate(run, cfg)


@pytest.mark.parametrize("name", ["runs", "attempts", "candidates", ".smoke_sandbox"])
def test_stage12_rejects_directories(tmp_path: Path, name: str) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)
    (selected / name).mkdir()

    with pytest.raises(RuntimeError, match="flat regular-file"):
        _load_sealed_candidate(run, cfg)


def test_stage12_rejects_owner_sets_that_do_not_match_files(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_selected_candidate(run, cfg)
    manifest_path = run / "stage-10" / "selected_candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["scaffold_files"] = {}
    manifest["plugin_files"] = {
        "detector_plugin.py": {"sha256": "0" * 64, "owner": "model"}
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="owner maps"):
        _load_sealed_candidate(run, cfg)


def test_stage12_rejects_invalid_scaffold_owner(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    selected = _write_selected_candidate(run, cfg)
    plugin = selected / "detector_plugin.py"
    plugin.write_text(
        "class DetectorPlugin:\n"
        "    def fit(self, X_train, y_train): return self\n"
        "    def predict(self, X_test): return [0] * len(X_test)\n"
        "    def describe(self): return {}\n",
        encoding="utf-8",
    )
    manifest_path = run / "stage-10" / "selected_candidate_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["detector_plugin.py"] = {"sha256": sha256_file(plugin)}
    manifest["scaffold_files"] = {
        "main.py": {"sha256": manifest["files"]["main.py"]["sha256"], "owner": "model"}
    }
    manifest["plugin_files"] = {
        "detector_plugin.py": {"sha256": sha256_file(plugin), "owner": "model"}
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid owner"):
        _load_sealed_candidate(run, cfg)


def test_stage10_scaffold_candidate_runs_in_stage12(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09" / "exp_plan.yaml").write_text(
        "objectives: []\n", encoding="utf-8"
    )

    stage10 = _execute_code_generation(
        run / "stage-10",
        run,
        cfg,
        AdapterBundle(),
        llm=None,
    )
    assert stage10.status == StageStatus.DONE

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _execute_experiment_run(
            run / "stage-12",
            run,
            cfg,
            AdapterBundle(),
        )


def test_canonical_stage12_publishes_and_replays_single_invocation(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    stage10 = _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    )
    assert stage10.status == StageStatus.DONE

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.DONE
    manifest = validate_experiment_result_set(run, cfg)
    assert manifest["execution_statuses"] == [{
        "ordinal": 1,
        "status": "completed",
        "result_path": "stage-12/evidence-v1/run-1.json",
        "failure_code": None,
    }]
    journal = parse_execution_invocation_journal(
        (run / "stage-12/execution_invocation_journal.jsonl").read_text(encoding="utf-8")
    )
    assert [record["event"] for record in journal] == ["started", "terminal"]
    assert journal[1]["status"] == "completed"


def test_canonical_stage12_failed_invocation_publishes_no_manifest(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    contract_path = _write_contract(run, cfg)
    experiment = run / "stage-10/experiment"
    experiment.mkdir(parents=True)
    (experiment / "main.py").write_text(
        render_main_py(load_contract(contract_path)), encoding="utf-8"
    )
    (experiment / "detector_plugin.py").write_text(
        """import numpy as np

class DetectorPlugin:
    def fit(self, X, y):
        return self

    def predict(self, X):
        return np.full(len(X), np.nan)
""",
        encoding="utf-8",
    )
    _seal_selected_candidate(run / "stage-10", experiment, contract_path, cfg)

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-12/experiment_result_set.json").exists()
    assert not (run / "stage-12/evidence-v1").exists()
    journal = parse_execution_invocation_journal(
        (run / "stage-12/execution_invocation_journal.jsonl").read_text(encoding="utf-8")
    )
    assert journal[-1]["status"] == "failed"


def test_controller_rejects_hidden_retry_after_failed_invocation(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    controller = CanonicalExecutionController.prepare_generation(
        run, run / "stage-12"
    )
    lease = controller.acquire(
        generation_binding_sha256="1" * 64,
        experiment_contract_sha256="2" * 64,
        sealed_candidate_manifest_sha256="3" * 64,
        config_semantic_sha256="4" * 64,
    )
    sandbox_root = controller.prepare_invocation_workspace(lease)

    class FailingSandbox:
        backend_kind = "subprocess"
        calls = 0

        def run_project(self, project_dir: Path, *, timeout_sec: int) -> object:
            del project_dir, timeout_sec
            self.calls += 1
            output = sandbox_root / "_project_1"
            output.mkdir()
            return SimpleNamespace(returncode=1, output_dir=output)

    sandbox = FailingSandbox()
    controller.run_project(lease, sandbox, tmp_path, timeout_sec=1)
    with pytest.raises(RuntimeError, match="already_consumed"):
        controller.run_project(lease, sandbox, tmp_path, timeout_sec=1)
    controller.fail(lease, failure_code="sandbox_execution_failed_v1")
    with pytest.raises(RuntimeError, match="already_acquired"):
        controller.acquire(
            generation_binding_sha256="1" * 64,
            experiment_contract_sha256="2" * 64,
            sealed_candidate_manifest_sha256="3" * 64,
            config_semantic_sha256="4" * 64,
        )
    assert sandbox.calls == 1
    controller.close()


def test_controller_rejects_output_from_archived_generation(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    archived_output = run / "stage-12_v1/diagnostics/invocation-1/sandbox/_project_1"
    archived_output.mkdir(parents=True)
    controller = CanonicalExecutionController.prepare_generation(
        run, run / "stage-12"
    )
    lease = controller.acquire(
        generation_binding_sha256="1" * 64,
        experiment_contract_sha256="2" * 64,
        sealed_candidate_manifest_sha256="3" * 64,
        config_semantic_sha256="4" * 64,
    )
    controller.prepare_invocation_workspace(lease)

    class ArchivedOutputSandbox:
        backend_kind = "subprocess"

        def run_project(self, project_dir: Path, *, timeout_sec: int) -> object:
            del project_dir, timeout_sec
            return SimpleNamespace(returncode=0, output_dir=archived_output)

    with pytest.raises(RuntimeError, match="output_directory_unbound"):
        controller.run_project(
            lease, ArchivedOutputSandbox(), tmp_path, timeout_sec=1
        )
    controller.fail(lease, failure_code="output_directory_unbound_v1")
    controller.close()


def test_legacy_stage12_entrypoint_is_unconditionally_removed(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="legacy_stage12_execution_removed"):
        _execute_legacy_experiment_run(
            tmp_path / "stage-12",
            tmp_path,
            _cfg(tmp_path),
            AdapterBundle(),
        )


def test_canonical_stage12_rejects_runner_output_outside_invocation_workspace(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    assert _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    ).status == StageStatus.DONE
    evaluator_text = next(
        (run / "stage-12/diagnostics/invocation-1/sandbox").glob("*/results.json")
    ).read_text(encoding="utf-8")
    exact_output = tmp_path / "exact-output"
    newer_sibling = tmp_path / "newer-sibling"
    exact_output.mkdir()
    newer_sibling.mkdir()
    (exact_output / "results.json").write_text(evaluator_text, encoding="utf-8")
    (newer_sibling / "results.json").write_text("{}\n", encoding="utf-8")

    class ExactOutputSandbox:
        backend_kind = "subprocess"

        def run_project(self, project_dir: Path, *, timeout_sec: int) -> object:
            del project_dir, timeout_sec
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                output_dir=exact_output,
            )

    monkeypatch.setattr(
        "researchclaw.experiment.factory.create_sandbox",
        lambda *args, **kwargs: ExactOutputSandbox(),
    )

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert "output_directory_unbound" in (result.error or "")
    assert not (run / "stage-12/experiment_result_set.json").exists()


def test_canonical_stage12_rejects_docker_to_subprocess_fallback(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(tmp_path)
    cfg = replace(
        cfg,
        experiment=replace(
            cfg.experiment,
            mode="docker",
            sandbox=replace(cfg.experiment.sandbox, allow_docker_fallback=True),
        ),
    )
    run = tmp_path / "run"
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    monkeypatch.setattr(
        "researchclaw.experiment.docker_sandbox.DockerSandbox.check_docker_available",
        lambda: False,
    )

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert "backend does not match" in (result.error or "")
    assert not (run / "stage-12/experiment_result_set.json").exists()
    journal = parse_execution_invocation_journal(
        (run / "stage-12/execution_invocation_journal.jsonl").read_text(encoding="utf-8")
    )
    assert journal[-1]["status"] == "failed"


def test_stage12_partial_evidence_write_never_enters_canonical_namespace(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    original_write = _execution._atomic_write_text

    def fail_second_evidence_write(path: Path, text: str) -> None:
        if path.name == "results.json" and path.parent.name == "evidence-v1":
            raise OSError("simulated aggregate write interruption")
        original_write(path, text)

    monkeypatch.setattr(_execution, "_atomic_write_text", fail_second_evidence_write)

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-12/evidence-v1").exists()
    assert not (run / "stage-12/.evidence-v1.staging").exists()
    assert not (run / "stage-12/experiment_result_set.json").exists()


def test_stage12_full_replay_precedes_manifest_publication(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    original_validate = _execution.validate_experiment_result_set

    def fail_prepublication_replay(
        replay_run: Path, replay_config: RCConfig, text: str | None = None
    ) -> dict[str, object]:
        if text is not None:
            raise RuntimeError("simulated prepublication replay interruption")
        return original_validate(replay_run, replay_config, text)

    monkeypatch.setattr(
        _execution, "validate_experiment_result_set", fail_prepublication_replay
    )

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-12/experiment_result_set.json").exists()
    assert not (run / "stage-12/evidence-v1").exists()


def test_execute_stage_invalidates_old_authority_before_input_preflight(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    stage12 = run / "stage-12"
    stage12.mkdir(parents=True)
    (stage12 / "experiment_result_set.json").write_text("stale\n", encoding="utf-8")
    (run / "canonical_experiment_evidence.json").write_text("stale\n", encoding="utf-8")

    result = execute_stage(
        Stage.EXPERIMENT_RUN,
        run_dir=run,
        run_id="rerun",
        config=cfg,
        adapters=AdapterBundle(),
    )

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-12/experiment_result_set.json").exists()
    assert not (run / "canonical_experiment_evidence.json").exists()
    assert not (run / "stage-12_v1/experiment_result_set.json").exists()


def test_stage12_rejects_symlink_lock_path(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    target = tmp_path / "lock-target"
    target.write_text("do not touch\n", encoding="utf-8")
    (run / ".canonical_experiment_evidence.lock").symlink_to(target)

    with pytest.raises(RuntimeError, match="lock_unsafe"):
        CanonicalExecutionController.prepare_generation(run, run / "stage-12")

    assert target.read_text(encoding="utf-8") == "do not touch\n"


def test_stage12_rejects_concurrent_generation_controller(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    first = CanonicalExecutionController.prepare_generation(run, run / "stage-12")
    try:
        with pytest.raises(RuntimeError, match="generation_locked"):
            CanonicalExecutionController.prepare_generation(run, run / "stage-12")
    finally:
        first.close()


def test_canonical_stage12_manifest_write_failure_leaves_no_manifest(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    original_write = CanonicalExecutionController.write_text_atomic

    def fail_manifest_write(
        self: CanonicalExecutionController, name: str, text: str
    ) -> None:
        if name == "experiment_result_set.json":
            raise OSError("simulated manifest publication failure")
        original_write(self, name, text)

    monkeypatch.setattr(
        CanonicalExecutionController, "write_text_atomic", fail_manifest_write
    )

    result = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-12/experiment_result_set.json").exists()
    journal = parse_execution_invocation_journal(
        (run / "stage-12/execution_invocation_journal.jsonl").read_text(encoding="utf-8")
    )
    assert journal[-1]["status"] == "completed"


def test_stage12_archive_interruption_invalidates_manifest_before_rollover(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    stage12 = run / "stage-12"
    stage12.mkdir(parents=True)
    manifest = stage12 / "experiment_result_set.json"
    manifest.write_text("stale\n", encoding="utf-8")
    journal = stage12 / "execution_invocation_journal.jsonl"
    journal.write_text("diagnostic history\n", encoding="utf-8")
    root_manifest = run / "canonical_experiment_evidence.json"
    root_manifest.write_text("stale\n", encoding="utf-8")

    def fail_rollover(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        del source, destination, src_dir_fd, dst_dir_fd
        raise OSError("simulated archive interruption")

    monkeypatch.setattr(release_graph_lock_module.os, "rename", fail_rollover)

    with pytest.raises(OSError, match="archive interruption"):
        CanonicalExecutionController.prepare_generation(run, stage12)

    assert not manifest.exists()
    assert journal.exists()
    assert not root_manifest.exists()
    assert not (run / "stage-12_v1").exists()


def test_canonical_stage12_rerun_archives_prior_generation_and_invalidates_root(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    run = tmp_path / "run"
    cfg = _cfg(tmp_path)
    _write_contract(run, cfg)
    (run / "stage-09/exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    assert _execute_code_generation(
        run / "stage-10", run, cfg, AdapterBundle(), llm=None
    ).status == StageStatus.DONE
    assert _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    ).status == StageStatus.DONE
    first_journal = (run / "stage-12/execution_invocation_journal.jsonl").read_text(
        encoding="utf-8"
    )
    (run / "canonical_experiment_evidence.json").write_text("stale\n", encoding="utf-8")
    (run / "experiment_summary_best.json").write_text("stale\n", encoding="utf-8")
    stage13 = run / "stage-13"
    stage13.mkdir()
    (stage13 / "refinement_result_set.json").write_text("stale\n", encoding="utf-8")

    rerun = _execute_experiment_run(
        run / "stage-12", run, cfg, AdapterBundle()
    )

    assert rerun.status == StageStatus.DONE
    assert (run / "stage-12_v1/execution_invocation_journal.jsonl").read_text(
        encoding="utf-8"
    ) == first_journal
    assert validate_experiment_result_set(run, cfg)
    assert not (run / "canonical_experiment_evidence.json").exists()
    assert not (run / "experiment_summary_best.json").exists()
    assert not (stage13 / "refinement_result_set.json").exists()
