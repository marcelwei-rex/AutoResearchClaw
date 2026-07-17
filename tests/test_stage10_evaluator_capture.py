from __future__ import annotations

import hashlib
import json
import inspect
import os
import signal
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime import metric_authority
from researchclaw.experiment_runtime.contract import derive_contract, dump_contract
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.pipeline import bound_output_namespace
from researchclaw.pipeline import stage10_evaluator_capture
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    parse_selected_candidate_manifest,
    validate_selected_candidate_manifest,
)
from researchclaw.pipeline.stage10_evaluator_capture import (
    Stage10EvaluatorCaptureError,
    invalidate_stage10_candidate_authority,
    replay_domain_evaluator_candidate,
)
from researchclaw.pipeline.stage_impls._code_generation import _execute_code_generation
from researchclaw.pipeline.stage_impls import _execution
from researchclaw.pipeline.stages import StageStatus


TOPIC = "TrojNet hardware Trojan localization on ISCAS-85 circuits"


def _config(tmp_path: Path) -> RCConfig:
    return RCConfig.from_dict(
        {
            "project": {"name": "trojnet-capture", "mode": "docs-first"},
            "research": {"topic": TOPIC, "domains": ["security"]},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "none"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "must-not-be-called",
                "fallback_models": [],
            },
            "experiment": {
                "mode": "sandbox",
                "claim_scope": "pipeline_validation",
                "dataset_origin": "synthetic",
                "time_budget_sec": 30,
                "metric_key": "auprc",
                "metric_direction": "maximize",
                "sandbox": {"python_path": sys.executable},
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )


def _prepare_run(tmp_path: Path) -> tuple[Path, RCConfig]:
    run = tmp_path / "run"
    run.mkdir()
    config = _config(tmp_path)
    config_path = run / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(json.loads(json.dumps(config.to_dict())), sort_keys=False),
        encoding="utf-8",
    )
    write_active_config_binding(run, config_path)
    stage9 = run / "stage-09"
    stage9.mkdir()
    contract = derive_contract(config, {}, stage_dir=stage9)
    dump_contract(contract, stage9 / "experiment_contract.yaml")
    (run / "stage-10").mkdir()
    return run, config


def _execute_capture(run: Path, config: RCConfig):
    return _execute_code_generation(
        run / "stage-10", run, config, AdapterBundle(), llm=None, prompts=None
    )


def _copy_trusted_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    trusted = tmp_path / "trusted"
    manifest_path = Path(
        "researchclaw/experiment_runtime/domain_evaluators/"
        "trojnet_iscas85_v1/package-manifest-v1.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for root in manifest["source_roots"]:
        source = Path(root["trusted_path"])
        destination = trusted / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    monkeypatch.setattr(metric_authority, "TRUSTED_SOURCE_BASE", trusted)
    return trusted


def test_stage10_domain_evaluator_capture_is_exact_and_replayable(
    tmp_path: Path,
) -> None:
    run, config = _prepare_run(tmp_path)
    stale = run / "stage-10/selected_candidate"
    stale.mkdir()
    (stale / "main.py").write_text("stale = True\n", encoding="utf-8")

    result = _execute_capture(run, config)

    assert result.status == StageStatus.DONE
    assert result.artifacts == (
        "evaluator-capture-v1/",
        "selected_candidate_manifest.json",
    )
    assert not (run / "stage-10/selected_candidate").exists()
    seal = replay_domain_evaluator_candidate(run, config)
    assert seal["schema_version"] == 3
    assert validate_selected_candidate_manifest(run, config) == seal
    capture_files = {
        path.relative_to(run / "stage-10/evaluator-capture-v1").as_posix()
        for path in (run / "stage-10/evaluator-capture-v1").rglob("*")
        if path.is_file()
    }
    assert len(capture_files) == 47
    assert "capture-manifest.json" in capture_files


def test_captured_authority_rejects_coherent_package_hash_rewrite() -> None:
    plan = metric_authority.build_domain_evaluator_capture_plan(TOPIC, "sandbox")
    forged_manifest = plan.package_manifest_bytes + b" "
    forged_authority = dict(plan.selection.evaluator_authority or {})
    forged_sha = hashlib.sha256(forged_manifest).hexdigest()
    forged_authority["package_manifest_package_sha256"] = forged_sha
    forged_authority["package_manifest_snapshot_sha256"] = forged_sha

    with pytest.raises(
        metric_authority.MetricAuthorityError,
        match="package authority mismatch",
    ):
        metric_authority.replay_captured_domain_evaluator_authority(
            topic=TOPIC,
            experiment_mode="sandbox",
            package_manifest_bytes=forged_manifest,
            execution_policy_bytes=plan.execution_policy_bytes,
            stored_identity=plan.selection.contract_identity(),
            metric_units=plan.selection.metric_units,
            metric_display_labels=plan.selection.metric_display_labels,
            evaluator_authority=forged_authority,
        )


def test_stage10_replay_never_reopens_package_sources_after_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _copy_trusted_sources(tmp_path, monkeypatch)
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    source = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/vendor/trojnet/train.py"
    )
    source.write_text("changed_after_capture = True\n", encoding="utf-8")

    assert replay_domain_evaluator_candidate(run, config)["schema_version"] == 3


def test_stage10_capture_tamper_is_rejected(tmp_path: Path) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    target = run / "stage-10/evaluator-capture-v1/verifier/verifier_main.py"
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(Stage10EvaluatorCaptureError, match="bytes mismatch"):
        replay_domain_evaluator_candidate(run, config)


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_stage10_replay_rejects_unsafe_captured_leaf(
    tmp_path: Path, replacement: str
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    target = run / "stage-10/evaluator-capture-v1/verifier/verifier_main.py"
    target.unlink()
    if replacement == "symlink":
        external = tmp_path / "external-verifier.py"
        external.write_text("external = True\n", encoding="utf-8")
        target.symlink_to(external)
    else:
        os.mkfifo(target)

    with pytest.raises(OSError, match="unsafe entry"):
        replay_domain_evaluator_candidate(run, config)


def test_stage10_capture_rejects_intermediate_source_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _copy_trusted_sources(tmp_path, monkeypatch)
    run, config = _prepare_run(tmp_path)
    circuit = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/data/iscas85/c1355"
    )
    external = tmp_path / "external-c1355"
    circuit.rename(external)
    circuit.symlink_to(external, target_is_directory=True)

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


@pytest.mark.parametrize("value", [True, 3.0, "3", None])
def test_stage10_seal_schema_dispatch_requires_true_integer(value: object) -> None:
    payload = {
        "schema_version": value,
        "candidate_kind": "domain_evaluator",
    }
    with pytest.raises(CanonicalExperimentEvidenceError):
        parse_selected_candidate_manifest(json.dumps(payload))


def test_stage10_capture_failure_removes_both_commit_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, config = _prepare_run(tmp_path)
    original = bound_output_namespace.BoundOutputNamespace.write_tree_file_atomic

    def fail_manifest(self, tree, relative_path, content):
        if relative_path == "capture-manifest.json":
            raise OSError("injected capture manifest failure")
        return original(self, tree, relative_path, content)

    monkeypatch.setattr(
        bound_output_namespace.BoundOutputNamespace,
        "write_tree_file_atomic",
        fail_manifest,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


@pytest.mark.parametrize("contract_state", ["missing", "invalid"])
def test_stage10_attempt_invalidates_stale_authority_before_contract_load(
    tmp_path: Path, contract_state: str
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    stale_v2 = run / "stage-10/selected_candidate"
    stale_v2.mkdir()
    (stale_v2 / "main.py").write_text("stale = True\n", encoding="utf-8")
    contract_path = run / "stage-09/experiment_contract.yaml"
    if contract_state == "missing":
        contract_path.unlink()
    else:
        contract_path.write_text("schema_version: [invalid\n", encoding="utf-8")

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not stale_v2.exists()


def test_stage10_capture_parent_replacement_is_external_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, config = _prepare_run(tmp_path)
    moved = tmp_path / "run-moved"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    original = bound_output_namespace.BoundOutputNamespace.publish_directory_tree

    def replace_parent(self, name, source):
        run.rename(moved)
        run.symlink_to(external, target_is_directory=True)
        return original(self, name, source)

    monkeypatch.setattr(
        bound_output_namespace.BoundOutputNamespace,
        "publish_directory_tree",
        replace_parent,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert {path.name for path in external.iterdir()} == {"sentinel"}
    assert not (moved / "stage-10/evaluator-capture-v1").exists()
    assert not (moved / "stage-10/selected_candidate_manifest.json").exists()


@pytest.mark.parametrize("field", ["topic", "budget", "evaluator", "safety"])
def test_stage10_rejects_contract_replacement_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    run, config = _prepare_run(tmp_path)
    original = bound_output_namespace.BoundOutputNamespace.publish_directory_tree

    def replace_contract(self, name, source):
        result = original(self, name, source)
        contract_path = run / "stage-09/experiment_contract.yaml"
        payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if field == "topic":
            payload["topic"] = "FORGED TOPIC"
        elif field == "budget":
            payload["run_budget_sec"] += 1
        elif field == "evaluator":
            payload["evaluator"]["timeout_sec"] += 1
        else:
            payload["safety"]["network"] = "forged"
        contract_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        bound_output_namespace.BoundOutputNamespace,
        "publish_directory_tree",
        replace_contract,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


def _mutate_stage10_authority(run: Path, target: str) -> None:
    paths = {
        "seal": run / "stage-10/selected_candidate_manifest.json",
        "capture": run / "stage-10/evaluator-capture-v1/verifier/verifier_main.py",
        "capture_manifest": (
            run / "stage-10/evaluator-capture-v1/capture-manifest.json"
        ),
        "contract": run / "stage-09/experiment_contract.yaml",
        "config": run / "config.yaml",
        "config_pointer": run / "active_config_snapshot.json",
        "config_history": run / "config_snapshot_history.jsonl",
        "package_snapshot": run / "stage-09/domain_evaluator_package_manifest.json",
        "execution_snapshot": run / "stage-09/domain_evaluator_execution_policy.json",
    }
    path = paths[target]
    path.write_bytes(path.read_bytes() + b"\n")


@pytest.mark.parametrize(
    "target",
    [
        "seal",
        "capture",
        "capture_manifest",
        "contract",
        "config",
        "config_pointer",
        "config_history",
        "package_snapshot",
        "execution_snapshot",
    ],
)
def test_stage10_producer_rejects_late_authority_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    run, config = _prepare_run(tmp_path)
    original = stage10_evaluator_capture._replay_candidate_snapshot
    calls = 0

    def mutate_after_replay(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            _mutate_stage10_authority(run, target)
        return result

    monkeypatch.setattr(
        stage10_evaluator_capture,
        "_replay_candidate_snapshot",
        mutate_after_replay,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


@pytest.mark.parametrize(
    "target",
    [
        "seal",
        "capture",
        "capture_manifest",
        "contract",
        "config",
        "config_pointer",
        "config_history",
        "package_snapshot",
        "execution_snapshot",
    ],
)
def test_stage10_public_replay_rejects_late_authority_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    original = stage10_evaluator_capture._replay_candidate_snapshot
    calls = 0

    def mutate_after_replay(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            _mutate_stage10_authority(run, target)
        return result

    monkeypatch.setattr(
        stage10_evaluator_capture,
        "_replay_candidate_snapshot",
        mutate_after_replay,
    )

    with pytest.raises(Stage10EvaluatorCaptureError, match="changed during replay"):
        replay_domain_evaluator_candidate(run, config)


def test_stage10_cleanup_collision_does_not_preserve_legacy_tree(
    tmp_path: Path,
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    (run / "stage-10/evaluator-capture-v1/bad\\name").write_text(
        "collision", encoding="utf-8"
    )
    legacy = run / "stage-10/selected_candidate"
    legacy.mkdir()
    (legacy / "main.py").write_text("stale = True\n", encoding="utf-8")

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()
    assert not legacy.exists()


@pytest.mark.parametrize("collision", ["malformed", "symlink", "fifo"])
def test_stage10_cleanup_entries_are_independent_and_external_safe(
    tmp_path: Path, collision: str
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    capture = run / "stage-10/evaluator-capture-v1"
    external = tmp_path / "cleanup-external"
    external.write_text("keep", encoding="utf-8")
    if collision == "malformed":
        (capture / "bad\\name").write_text("collision", encoding="utf-8")
    elif collision == "symlink":
        (capture / "external-link").symlink_to(external)
    else:
        os.mkfifo(capture / "fifo")
    legacy = run / "stage-10/selected_candidate"
    legacy.mkdir()
    (legacy / "main.py").write_text("stale = True\n", encoding="utf-8")

    if collision == "malformed":
        with pytest.raises(Stage10EvaluatorCaptureError, match="cleanup failed"):
            invalidate_stage10_candidate_authority(run)
    else:
        invalidate_stage10_candidate_authority(run)

    assert not (run / "stage-10/selected_candidate_manifest.json").exists()
    assert not legacy.exists()
    assert external.read_text(encoding="utf-8") == "keep"
    assert not capture.exists()


def test_stage10_injected_capture_cleanup_failure_still_removes_legacy_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    legacy = run / "stage-10/selected_candidate"
    legacy.mkdir()
    (legacy / "main.py").write_text("stale = True\n", encoding="utf-8")
    original = bound_output_namespace._remove_tree_at

    def fail_capture(parent_fd, name):
        if name.startswith(".evaluator-capture-v1.rejected-"):
            raise OSError("injected capture cleanup failure")
        return original(parent_fd, name)

    monkeypatch.setattr(
        bound_output_namespace,
        "_remove_tree_at",
        fail_capture,
    )

    with pytest.raises(Stage10EvaluatorCaptureError, match="injected"):
        invalidate_stage10_candidate_authority(run)

    assert not (run / "stage-10/selected_candidate_manifest.json").exists()
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not legacy.exists()


@pytest.mark.parametrize(
    "name", ["config.resumed-shadow.yaml", "config.resumed-EVIL.yaml"]
)
def test_stage10_rejects_noncanonical_resumed_config_shadow(
    tmp_path: Path, name: str
) -> None:
    run, config = _prepare_run(tmp_path)
    (run / name).write_bytes((run / "config.yaml").read_bytes())

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "fifo"])
def test_stage10_rejects_unsafe_canonical_resumed_config_without_blocking(
    tmp_path: Path, entry_kind: str
) -> None:
    run, config = _prepare_run(tmp_path)
    resumed = run / "config.resumed-20260717-120000.yaml"
    external = tmp_path / "external-resumed-config.yaml"
    external.write_text("keep", encoding="utf-8")
    if entry_kind == "symlink":
        resumed.symlink_to(external)
    elif entry_kind == "directory":
        resumed.mkdir()
    else:
        os.mkfifo(resumed)

    def timeout_handler(_signum, _frame):
        raise TimeoutError("Stage 10 blocked on unsafe resumed config")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        result = _execute_capture(run, config)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()
    assert external.read_text(encoding="utf-8") == "keep"


def test_stage10_fixpoint_rejects_new_noncanonical_resumed_config_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, config = _prepare_run(tmp_path)
    original = stage10_evaluator_capture._replay_candidate_snapshot
    calls = 0

    def add_shadow_after_replay(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            (run / "config.resumed-shadow.yaml").write_bytes(
                (run / "config.yaml").read_bytes()
            )
        return result

    monkeypatch.setattr(
        stage10_evaluator_capture,
        "_replay_candidate_snapshot",
        add_shadow_after_replay,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert not (run / "stage-10/evaluator-capture-v1").exists()
    assert not (run / "stage-10/selected_candidate_manifest.json").exists()


def test_stage10_source_discovery_parent_replacement_is_external_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, config = _prepare_run(tmp_path)
    moved = tmp_path / "run-moved-discovery"
    external = tmp_path / "external-discovery"
    external.mkdir()
    (external / "sentinel").write_text("keep", encoding="utf-8")
    original = stage10_evaluator_capture._capture_source_authority
    replaced = False

    def replace_before_discovery(namespace):
        nonlocal replaced
        if not replaced:
            replaced = True
            run.rename(moved)
            run.symlink_to(external, target_is_directory=True)
        return original(namespace)

    monkeypatch.setattr(
        stage10_evaluator_capture,
        "_capture_source_authority",
        replace_before_discovery,
    )

    result = _execute_capture(run, config)

    assert result.status == StageStatus.FAILED
    assert {path.name for path in external.iterdir()} == {"sentinel"}
    assert not (moved / "stage-10/evaluator-capture-v1").exists()
    assert not (moved / "stage-10/selected_candidate_manifest.json").exists()


def test_stage12_has_no_live_package_or_fixture_consumer() -> None:
    source = inspect.getsource(_execution)
    assert "domain_evaluators" not in source
    assert "validation_fixtures" not in source
    assert "TRUSTED_SOURCE_BASE" not in source
