from __future__ import annotations

import ast
import hashlib
import json
import inspect
import os
import signal
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

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
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.stages import Stage, StageStatus


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


def test_stage10_executor_accepts_exact_domain_capture_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_run(tmp_path)
    (run / "stage-09/exp_plan.yaml").write_text(
        "mode: fixed_domain_evaluator\n", encoding="utf-8"
    )

    def unexpected_llm(*_args, **_kwargs):
        raise AssertionError("fixed Stage 10 attempted to construct an LLM")

    monkeypatch.setattr(
        "researchclaw.pipeline.executor._create_configured_llm", unexpected_llm
    )
    result = execute_stage(
        Stage.CODE_GENERATION,
        run_dir=run,
        run_id="stage10-domain-executor-contract",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.DONE, result.error
    assert result.artifacts == (
        "evaluator-capture-v1/",
        "selected_candidate_manifest.json",
    )


def test_captured_authority_rejects_coherent_package_hash_rewrite() -> None:
    plan = metric_authority.build_domain_evaluator_capture_plan(
        metric_authority.select_metric_authority(TOPIC, "sandbox")
    )
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


def _r1_stage10_case():
    plan = metric_authority.build_domain_evaluator_capture_plan(
        metric_authority.select_metric_authority(TOPIC, "sandbox")
    )
    authority = plan.selection.evaluator_authority or {}
    manifest = {
        "schema_version": 1,
        "capture_policy_version": 1,
        "package_manifest": {
            "path": metric_authority.DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH,
            "sha256": hashlib.sha256(plan.package_manifest_bytes).hexdigest(),
            "size": len(plan.package_manifest_bytes),
        },
        "execution_policy": {
            "path": metric_authority.DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH,
            "sha256": hashlib.sha256(plan.execution_policy_bytes).hexdigest(),
            "size": len(plan.execution_policy_bytes),
        },
        "evaluator_schema": authority["evaluator_schema"],
        "source_namespace_sha256": plan.source_namespace_sha256,
        "files": list(plan.files),
    }
    manifest_bytes = (json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode()
    entries = dict(plan.contents)
    entries[stage10_evaluator_capture.CAPTURE_MANIFEST] = manifest_bytes
    return plan, tuple(sorted(entries.items()))


def _r1_stage10_resolve(plan, entries):
    api = getattr(
        stage10_evaluator_capture, "resolve_captured_evaluator_membership", None
    )
    assert callable(api), "missing resolve_captured_evaluator_membership"
    return api(
        package_manifest_bytes=plan.package_manifest_bytes,
        execution_policy_bytes=plan.execution_policy_bytes,
        capture_entries=entries,
    )


def test_r1_07_17_shared_membership_preserves_nested_frozen_order() -> None:
    plan, entries = _r1_stage10_case()
    before = (plan.package_manifest_bytes, plan.execution_policy_bytes, entries)
    first = _r1_stage10_resolve(plan, entries)
    second = _r1_stage10_resolve(plan, entries)
    assert type(first) is tuple and first == second and first is not second
    paths = tuple(member.path for member in first)
    assert "data/c1355/c1355_ht1.bench" in paths
    assert "vendor/train.py" in paths
    assert paths == tuple(item["capture_path"] for item in json.loads(
        plan.package_manifest_bytes
    )["files"])
    rows = json.loads(plan.package_manifest_bytes)["files"]
    actual = dict(entries)
    member_type = getattr(stage10_evaluator_capture, "CapturedEvaluatorMember")
    assert all(type(member) is member_type for member in first)
    assert member_type.__dataclass_params__.frozen
    assert tuple(field.name for field in member_type.__dataclass_fields__.values()) == (
        "role", "path", "sha256", "size", "content")
    assert tuple((item.role, item.path, item.sha256, item.size, item.content)
        for item in first) == tuple((row["role"], row["capture_path"], row["sha256"],
        row["size"], actual[row["capture_path"]]) for row in rows)
    with pytest.raises((AttributeError, TypeError)):
        first[0].path = "flat.py"
    assert (plan.package_manifest_bytes, plan.execution_policy_bytes, entries) == before


@pytest.mark.parametrize("attack", ("missing", "extra", "bytes"))
def test_r1_09_10_actual_capture_closure_and_bytes_reject(attack: str) -> None:
    plan, entries = _r1_stage10_case()
    tree = dict(entries)
    target = next(path for path in tree if path != "capture-manifest.json")
    if attack == "missing":
        tree.pop(target)
    elif attack == "extra":
        tree["vendor/evil.py"] = b"evil\n"
    else:
        tree[target] += b"drift"
    with pytest.raises(Stage10EvaluatorCaptureError):
        _r1_stage10_resolve(plan, tuple(sorted(tree.items())))


@pytest.mark.parametrize(
    "attack", ("role", "path", "sha256", "size", "entry_sha", "order",
               "row_add", "row_delete", "source_sha", "package_ref_path",
               "package_ref_sha256", "package_ref_size", "execution_ref_path",
               "execution_ref_sha256", "execution_ref_size", "schema")
)
def test_r1_11_12_13_capture_manifest_is_comparison_only(attack: str) -> None:
    plan, entries = _r1_stage10_case()
    tree = dict(entries)
    manifest = json.loads(tree["capture-manifest.json"])
    if attack in {"role", "path", "sha256", "size", "entry_sha"}:
        field = {"entry_sha": "package_entry_sha256"}.get(attack, attack)
        manifest["files"][0][field] = (
            manifest["files"][0][field] + "x"
            if isinstance(manifest["files"][0][field], str)
            else manifest["files"][0][field] + 1
        )
    elif attack == "order":
        manifest["files"][:2] = reversed(manifest["files"][:2])
    elif attack == "row_add":
        manifest["files"].append(dict(manifest["files"][-1], path="vendor/evil.py"))
        manifest["files"].sort(key=lambda row: row["path"])
    elif attack == "row_delete":
        manifest["files"].pop()
    elif attack == "source_sha":
        manifest["source_namespace_sha256"] = "0" * 64
    elif attack.startswith(("package_ref_", "execution_ref_")):
        owner, field = attack.rsplit("_", 1)
        ref = manifest[{"package_ref": "package_manifest", "execution_ref": "execution_policy"}[owner]]
        ref[field] = {"path": "wrong.json", "sha256": "0" * 64, "size": ref["size"] + 1}[field]
    else:
        manifest["evaluator_schema"] += "-forged"
    tree["capture-manifest.json"] = (json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode()
    with pytest.raises(Stage10EvaluatorCaptureError):
        _r1_stage10_resolve(plan, tuple(sorted(tree.items())))


@pytest.mark.parametrize("attack", ("missing", "duplicate", "unsafe", "prefix"))
def test_r1_08_package_member_identity_rejects(attack: str) -> None:
    plan, entries = _r1_stage10_case()
    package = json.loads(plan.package_manifest_bytes)
    if attack == "missing":
        package["files"].pop()
    else:
        package["files"][1]["capture_path"] = {
            "duplicate": package["files"][0]["capture_path"],
            "unsafe": "../evil.py",
            "prefix": package["files"][0]["capture_path"] + "/child.py",
        }[attack]
    forged = (json.dumps(package, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if attack in {"missing", "prefix"}:
        tree = dict(entries)
        manifest = json.loads(tree["capture-manifest.json"])
        if attack == "prefix":
            old, new = json.loads(plan.package_manifest_bytes)["files"][1]["capture_path"], package["files"][1]["capture_path"]
            tree[new] = tree.pop(old)
            row = next(item for item in manifest["files"] if item["path"] == old)
            row["path"] = new
            row["package_entry_sha256"] = hashlib.sha256((json.dumps(
                package["files"][1], sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        manifest["package_manifest"].update(
            sha256=hashlib.sha256(forged).hexdigest(), size=len(forged))
        manifest["files"].sort(key=lambda item: item["path"])
        tree["capture-manifest.json"] = (json.dumps(
            manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        entries = tuple(sorted(tree.items()))
        package_paths = tuple(item["capture_path"] for item in package["files"])
        row_paths = tuple(item["path"] for item in manifest["files"])
        actual_paths = set(tree) - {"capture-manifest.json"}
        assert manifest["package_manifest"]["sha256"] == hashlib.sha256(forged).hexdigest()
        assert manifest["package_manifest"]["size"] == len(forged)
        if attack == "prefix":
            assert set(package_paths) == actual_paths == set(row_paths) and row_paths == tuple(sorted(row_paths))
            assert any(right.startswith(left + "/") for left in package_paths for right in package_paths)
        else:
            removed = json.loads(plan.package_manifest_bytes)["files"][-1]["capture_path"]
            assert removed not in package_paths and removed in actual_paths and removed in row_paths
    plan = SimpleNamespace(
        package_manifest_bytes=forged,
        execution_policy_bytes=plan.execution_policy_bytes,
    )
    with pytest.raises(Stage10EvaluatorCaptureError):
        _r1_stage10_resolve(plan, entries)


def test_r1_14_producer_replay_delegates_shared_membership_helper() -> None:
    source = inspect.getsource(
        stage10_evaluator_capture._replay_candidate_from_captured_authority
    )
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_captured_evaluator_membership"]
    assert len(calls) == 1


def test_r1_10_capture_bytes_reject_after_outer_metadata_can_rebind() -> None:
    plan, entries = _r1_stage10_case()
    tree = dict(entries)
    target = next(path for path in tree if path != "capture-manifest.json")
    tree[target] += b"coherent outer record drift"
    with pytest.raises(Stage10EvaluatorCaptureError):
        _r1_stage10_resolve(plan, tuple(sorted(tree.items())))


def _r1_producer_replay_inputs():
    plan, entries = _r1_stage10_case()
    tree = dict(entries)
    ref = lambda path, content: {"path": path,
        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    authority = plan.selection.evaluator_authority or {}
    contract = {"schema_version": 3, "metric_authority": plan.selection.contract_identity(),
        "evaluator_authority": {"evaluator_schema": authority["evaluator_schema"]}}
    seal = {"metric_authority": contract["metric_authority"],
        "evaluator_schema": authority["evaluator_schema"],
        "package_manifest": ref(metric_authority.DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH, plan.package_manifest_bytes),
        "execution_policy": ref(metric_authority.DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH, plan.execution_policy_bytes),
        "capture_manifest": ref("stage-10/evaluator-capture-v1/capture-manifest.json", tree["capture-manifest.json"])}
    return plan, tree, seal, contract


@pytest.mark.parametrize("attack", ("contract", "metric", "package", "execution", "capture",
    "capture_package", "capture_execution", "capture_schema", "package_execution"))
def test_r1_14_producer_prebindings_precede_shared_helper(
    monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    api = getattr(stage10_evaluator_capture, "resolve_captured_evaluator_membership", None)
    assert callable(api), "missing resolve_captured_evaluator_membership"
    plan, tree, seal, contract = _r1_producer_replay_inputs()
    package_bytes = plan.package_manifest_bytes
    calls = []
    def counting(**kwargs):
        calls.append(kwargs)
        return api(**kwargs)
    monkeypatch.setattr(stage10_evaluator_capture, "resolve_captured_evaluator_membership", counting)
    if attack == "contract": contract["schema_version"] = 2
    elif attack == "metric": seal["metric_authority"] = {}
    elif attack in {"package", "execution", "capture"}:
        seal[{"package": "package_manifest", "execution": "execution_policy",
            "capture": "capture_manifest"}[attack]]["sha256"] = "0" * 64
    elif attack.startswith("capture_"):
        manifest = json.loads(tree["capture-manifest.json"])
        field = attack.removeprefix("capture_")
        if field == "schema": manifest["evaluator_schema"] += "-wrong"
        else: manifest[{"package": "package_manifest", "execution": "execution_policy"}[field]]["sha256"] = "0" * 64
        tree["capture-manifest.json"] = (json.dumps(
            manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        seal["capture_manifest"].update(sha256=hashlib.sha256(
            tree["capture-manifest.json"]).hexdigest(), size=len(tree["capture-manifest.json"]))
    else:
        package = json.loads(package_bytes); package["execution_policy_sha256"] = "0" * 64
        package_bytes = (json.dumps(package, sort_keys=True, separators=(",", ":")) + "\n").encode()
        seal["package_manifest"].update(sha256=hashlib.sha256(package_bytes).hexdigest(), size=len(package_bytes))
    with pytest.raises(Stage10EvaluatorCaptureError):
        stage10_evaluator_capture._replay_candidate_from_captured_authority(
            capture_tree=tree, seal=seal, contract=contract,
            package_manifest_bytes=package_bytes,
            execution_policy_bytes=plan.execution_policy_bytes)
    assert calls == []


def test_r1_14_producer_calls_shared_helper_once_after_prebindings(monkeypatch) -> None:
    api = getattr(stage10_evaluator_capture, "resolve_captured_evaluator_membership", None)
    assert callable(api), "missing resolve_captured_evaluator_membership"
    plan, tree, seal, contract = _r1_producer_replay_inputs()
    calls = []
    def counting(**kwargs):
        calls.append(kwargs)
        return api(**kwargs)
    monkeypatch.setattr(stage10_evaluator_capture, "resolve_captured_evaluator_membership", counting)
    stage10_evaluator_capture._replay_candidate_from_captured_authority(
        capture_tree=tree, seal=seal, contract=contract,
        package_manifest_bytes=plan.package_manifest_bytes,
        execution_policy_bytes=plan.execution_policy_bytes)
    assert len(calls) == 1
