from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.experiment_runtime import metric_authority
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    parse_experiment_result_set,
    validate_experiment_result_set,
)
from researchclaw.pipeline.stage12_domain_evaluator import (
    Stage12DomainEvaluatorError,
    parse_domain_evaluator_journal,
)
from researchclaw.pipeline import stage12_domain_evaluator
from researchclaw.pipeline import canonical_evidence_capabilities
from researchclaw.pipeline.stage10_evaluator_capture import (
    _capture_domain_evaluator_candidate_under_lock,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.stage_impls._execution import _execute_experiment_run
from researchclaw.pipeline.stages import StageStatus
from tests.test_stage10_evaluator_capture import _execute_capture, _prepare_run


VERIFIER_PATH = Path(
    "researchclaw/experiment_runtime/domain_evaluators/"
    "trojnet_iscas85_v1/verifier_main.py"
)


def _verifier_module():
    spec = importlib.util.spec_from_file_location("trojnet_verifier_test", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@pytest.mark.parametrize(
    ("scores", "labels", "expected"),
    [
        (
            ("1", "1", "0", "0"),
            (1, 0, 1, 0),
            {
                "accuracy": Fraction(1, 2),
                "auprc": Fraction(1, 2),
                "auroc": Fraction(1, 2),
                "f1": Fraction(0),
                "fpr": Fraction(0),
                "precision": Fraction(0),
                "recall": Fraction(0),
                "top_k_precision": Fraction(1, 2),
            },
        ),
        (
            ("1", "1", "1", "1"),
            (1, 0, 1, 0),
            {
                "accuracy": Fraction(1, 2),
                "auprc": Fraction(1, 2),
                "auroc": Fraction(1, 2),
                "f1": Fraction(0),
                "fpr": Fraction(0),
                "precision": Fraction(0),
                "recall": Fraction(0),
                "top_k_precision": Fraction(1, 2),
            },
        ),
        (
            ("0.9", "0.5", "0.5", "0.1"),
            (1, 1, 0, 0),
            {
                "accuracy": Fraction(3, 4),
                "auprc": Fraction(5, 6),
                "auroc": Fraction(7, 8),
                "f1": Fraction(2, 3),
                "fpr": Fraction(0),
                "precision": Fraction(1),
                "recall": Fraction(1, 2),
                "top_k_precision": Fraction(3, 4),
            },
        ),
        (
            ("0", "0", "0"),
            (1, 0, 0),
            {
                "accuracy": Fraction(2, 3),
                "auprc": Fraction(1, 3),
                "auroc": Fraction(1, 2),
                "f1": Fraction(0),
                "fpr": Fraction(0),
                "precision": Fraction(0),
                "recall": Fraction(0),
                "top_k_precision": Fraction(1, 3),
            },
        ),
    ],
)
def test_independent_metric_oracle_golden_vectors(
    scores: tuple[str, ...],
    labels: tuple[int, ...],
    expected: dict[str, Fraction],
) -> None:
    verifier = _verifier_module()

    actual = verifier._metrics([Decimal(value) for value in scores], list(labels))

    assert actual == expected


def test_independent_metric_oracle_uses_fixed_nonterminating_decimal_policy() -> None:
    verifier = _verifier_module()

    assert verifier._decimal_text(Fraction(1, 3)) == (
        "0.33333333333333333333333333333333333333333333333333"
    )
    assert verifier._decimal_text(Fraction(2, 3)) == (
        "0.66666666666666666666666666666666666666666666666667"
    )
    assert verifier._sqrt_text(Fraction(1, 3)) == (
        "0.57735026918962576450914878050195745564760175127012"
    )


def test_verifier_import_closure_is_standard_library_only() -> None:
    tree = ast.parse(VERIFIER_PATH.read_text(encoding="utf-8"))
    allowed = {
        "__future__", "decimal", "fractions", "hashlib", "json", "pathlib",
        "re", "sys", "unicodedata",
    }
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert imports <= allowed
    forbidden_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"eval", "exec", "compile", "__import__"} & forbidden_calls)


def test_stage12_domain_evaluator_runs_twice_from_capture_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE

    def reject_live_source_recapture(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Stage 12 attempted to recapture trusted live roots")

    monkeypatch.setattr(
        metric_authority,
        "_capture_exact_package_sources",
        reject_live_source_recapture,
    )

    result = _execute_experiment_run(
        run / "stage-12", run, config, AdapterBundle()
    )

    assert result.status == StageStatus.DONE, result.error
    assert result.artifacts == (
        "experiment_result_set.json",
        "execution_invocation_journal.jsonl",
        "evidence-v2/",
    )
    first = run / "stage-12/evidence-v2/invocation-1/score_evidence.jsonl"
    second = run / "stage-12/evidence-v2/invocation-2/score_evidence.jsonl"
    assert first.read_bytes() == second.read_bytes()
    assert len(first.read_bytes().splitlines()) == 162
    assert (
        run / "stage-12/evidence-v2/verification-1.json"
    ).read_bytes() == (
        run / "stage-12/evidence-v2/verification-2.json"
    ).read_bytes()
    assert validate_experiment_result_set(run, config)["result_set_type"] == (
        "stage12_domain_evaluator"
    )
    journal = parse_domain_evaluator_journal(
        (run / "stage-12/execution_invocation_journal.jsonl").read_bytes()
    )
    assert [(item["event"], item["ordinal"]) for item in journal] == [
        ("started", Decimal(1)),
        ("terminal", Decimal(1)),
        ("started", Decimal(2)),
        ("terminal", Decimal(2)),
    ]

    original = first.read_bytes()
    first.write_bytes(original.replace(b'"scores":["', b'"scores":["0', 1))
    try:
        with pytest.raises(
            Stage12DomainEvaluatorError,
            match=(
                "evidence namespace mismatch|raw score evidence mismatch|"
                "score decimal grammar mismatch"
            ),
        ):
            validate_experiment_result_set(run, config)
    finally:
        first.write_bytes(original)
    assert validate_experiment_result_set(run, config)["result_set_type"] == (
        "stage12_domain_evaluator"
    )

    manifest_path = run / "stage-12/experiment_result_set.json"
    manifest_bytes = manifest_path.read_bytes()
    original_reader = stage12_domain_evaluator.BoundOutputNamespace.read_bytes
    switched = False

    def switch_after_discriminator(namespace, name: str) -> bytes:
        nonlocal switched
        value = original_reader(namespace, name)
        if name == "experiment_result_set.json" and not switched:
            switched = True
            manifest_path.write_text("{}\n", encoding="utf-8")
        return value

    monkeypatch.setattr(
        stage12_domain_evaluator.BoundOutputNamespace,
        "read_bytes",
        switch_after_discriminator,
    )
    try:
        with pytest.raises(Stage12DomainEvaluatorError):
            validate_experiment_result_set(run, config)
    finally:
        manifest_path.write_bytes(manifest_bytes)
        monkeypatch.setattr(
            stage12_domain_evaluator.BoundOutputNamespace,
            "read_bytes",
            original_reader,
        )

    journal_path = run / "stage-12/execution_invocation_journal.jsonl"
    journal_bytes = journal_path.read_bytes()
    original_verifier = stage12_domain_evaluator._run_verifier
    mutated = False

    def mutate_journal_during_verifier(**kwargs):
        nonlocal mutated
        value = original_verifier(**kwargs)
        if not mutated:
            mutated = True
            journal_path.write_bytes(journal_bytes + b"{}\n")
        return value

    monkeypatch.setattr(
        stage12_domain_evaluator, "_run_verifier", mutate_journal_during_verifier
    )
    try:
        with pytest.raises(
            Stage12DomainEvaluatorError,
            match="authority changed during semantic replay",
        ):
            validate_experiment_result_set(run, config)
    finally:
        journal_path.write_bytes(journal_bytes)
        monkeypatch.setattr(
            stage12_domain_evaluator, "_run_verifier", original_verifier
        )


def test_stage12_different_raw_scores_publish_no_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run, config = _prepare_run(tmp_path)
    assert _execute_capture(run, config).status == StageStatus.DONE
    policy = json.loads(
        (
            run
            / "stage-10/evaluator-capture-v1/policy/execution-policy-v1.json"
        ).read_text(encoding="utf-8")
    )
    calls = 0

    def score_bytes(value: str) -> bytes:
        rows = []
        for seed in policy["seeds"]:
            for family in policy["circuit_families"]:
                for variant in range(1, policy["variants_per_family"] + 1):
                    for condition in policy["conditions"]:
                        rows.append(
                            json.dumps(
                                {
                                    "circuit_family": family,
                                    "circuit_variant": f"{family}_ht{variant}",
                                    "condition": condition,
                                    "node_ids": ["n"],
                                    "schema_version": 1,
                                    "scores": [value],
                                    "seed": seed,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            + b"\n"
                        )
        return b"".join(rows)

    def fake_evaluator(**kwargs):
        nonlocal calls
        del kwargs
        calls += 1
        return score_bytes("0" if calls == 1 else "1"), "0" * 64

    monkeypatch.setattr(stage12_domain_evaluator, "_run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        stage12_domain_evaluator,
        "_run_verifier",
        lambda **kwargs: b"{}\n",
    )

    result = _execute_experiment_run(
        run / "stage-12", run, config, AdapterBundle()
    )

    assert result.status == StageStatus.FAILED
    assert "different raw score evidence" in (result.error or "")
    assert not (run / "stage-12/experiment_result_set.json").exists()
    assert not (run / "stage-12/evidence-v2").exists()


def test_stage12_v2_parser_rejects_v1_field_mixing() -> None:
    mixed = {
        "schema_version": 2,
        "result_set_policy_version": 1,
        "result_set_type": "stage12_baseline",
    }

    with pytest.raises(
        (CanonicalExperimentEvidenceError, Stage12DomainEvaluatorError),
        match="schema mismatch",
    ):
        parse_experiment_result_set(
            json.dumps(mixed, sort_keys=True, separators=(",", ":")) + "\n"
        )


@pytest.mark.parametrize(
    "entrypoint",
    [
        lambda run, config: validate_experiment_result_set(run, config),
        lambda run, config: stage12_domain_evaluator.stage12_uses_domain_evaluator(run),
        lambda run, config: stage12_domain_evaluator.execute_domain_evaluator_stage12(
            run_dir=run, config=config
        ),
        lambda run, config: stage12_domain_evaluator.validate_domain_evaluator_result_set(
            run, config
        ),
    ],
)
def test_stage12_domain_public_entries_are_capability_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint,
) -> None:
    capabilities = {
        name: canonical_evidence_capabilities.CAPABILITY_SCHEMA_VERSION
        for name in canonical_evidence_capabilities.REQUIRED_CAPABILITIES
    }
    capabilities["domain_evaluator_authority"] = 0
    monkeypatch.setattr(
        canonical_evidence_capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        capabilities,
    )
    run = tmp_path / "must-not-exist"
    config = _prepare_run  # The guard must reject before config is observed.

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        entrypoint(run, config)

    assert not run.exists()


@pytest.mark.parametrize(
    "content",
    [
        b'{"device":"cpu","packages":{},"python_major_minor":"3.11",'
        b'"torch_deterministic_algorithms":true,"torch_num_threads":true}\n',
        b'{"device":"cpu","packages":{},"python_major_minor":"3.11",'
        b'"torch_deterministic_algorithms":1,"torch_num_threads":1}\n',
        b'{"device":"cpu","packages":{},"python_major_minor":"3.11",'
        b'"torch_deterministic_algorithms":true,"torch_num_threads":1, '
        b'"torch_num_threads":1}\n',
        b'{"device":"cpu","packages":{},"python_major_minor":"3.11",'
        b'"torch_deterministic_algorithms":true,"torch_num_threads":NaN}\n',
        b' {"device":"cpu","packages":{},"python_major_minor":"3.11",'
        b'"torch_deterministic_algorithms":true,"torch_num_threads":1}\n',
    ],
)
def test_runtime_attestation_rejects_noncanonical_or_wrong_types(
    content: bytes,
) -> None:
    with pytest.raises(Stage12DomainEvaluatorError):
        stage12_domain_evaluator._strict_runtime_attestation(content)


@pytest.mark.parametrize(
    "token",
    [True, False, "", "a" * 63, "A" * 64, "g" * 64],
)
def test_stage12_journal_rejects_noncanonical_invocation_token(token) -> None:
    sha = "0" * 64
    records = []
    for ordinal in (1, 2):
        current = token if ordinal == 1 else "1" * 64
        records.extend(
            [
                {
                    "schema_version": 2,
                    "event": "started",
                    "ordinal": ordinal,
                    "invocation_token": current,
                    "generation_binding_sha256": sha,
                    "experiment_contract_sha256": sha,
                    "sealed_candidate_manifest_sha256": sha,
                    "capture_manifest_sha256": sha,
                    "execution_policy_sha256": sha,
                    "runtime_attestation_sha256": sha,
                    "config_semantic_sha256": sha,
                    "evaluator_schema": "x",
                },
                {
                    "schema_version": 2,
                    "event": "terminal",
                    "ordinal": ordinal,
                    "invocation_token": current,
                    "status": "completed",
                    "score_evidence_path": (
                        f"stage-12/evidence-v2/invocation-{ordinal}/"
                        "score_evidence.jsonl"
                    ),
                    "score_evidence_sha256": sha,
                    "execution_meta_path": (
                        f"stage-12/evidence-v2/invocation-{ordinal}/"
                        "execution_meta.json"
                    ),
                    "execution_meta_sha256": sha,
                    "failure_code": None,
                },
            ]
        )
    content = b"".join(
        (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for item in records
    )

    with pytest.raises(Stage12DomainEvaluatorError, match="token"):
        parse_domain_evaluator_journal(content)


def test_bound_python_rejects_loaded_image_swap_and_restore(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run, config = _prepare_run(tmp_path)
    configured = tmp_path / "python"
    configured.symlink_to(Path(sys.executable).resolve())
    config = replace(
        config,
        experiment=replace(
            config.experiment,
            sandbox=replace(
                config.experiment.sandbox,
                python_path=configured.as_posix(),
            ),
        ),
    )
    malicious = tmp_path / "malicious"
    malicious.write_text("#!/bin/sh\necho MALICIOUS\n", encoding="utf-8")
    malicious.chmod(0o700)

    if sys.platform != "darwin":
        pytest.skip("Darwin suspended-image adversarial test")
    original_cdhash = stage12_domain_evaluator._darwin_process_cdhash

    with stage12_domain_evaluator._bound_python_executable(config) as bound:
        configured.unlink()
        configured.symlink_to(malicious)

        def restore_before_proof(pid: int) -> bytes:
            if pid != os.getpid():
                configured.unlink()
                configured.symlink_to(Path(sys.executable).resolve())
            return original_cdhash(pid)

        monkeypatch.setattr(
            stage12_domain_evaluator,
            "_darwin_process_cdhash",
            restore_before_proof,
        )
        with pytest.raises(Stage12DomainEvaluatorError, match="image differs"):
            stage12_domain_evaluator._run_process(
                [configured.as_posix(), "-I", "-c", "print('BOUND')"],
                bound_python=bound,
                cwd=tmp_path,
                timeout=10,
                capture_output=True,
            )
        bound.assert_bound()


def test_bound_python_executes_kernel_verified_image(
    tmp_path: Path,
    canonical_evidence_migration_complete: None,
) -> None:
    _run, config = _prepare_run(tmp_path)
    with stage12_domain_evaluator._bound_python_executable(config) as bound:
        result = stage12_domain_evaluator._run_process(
            [bound.configured_path, "-I", "-c", "print('BOUND')"],
            bound_python=bound,
            cwd=tmp_path,
            timeout=10,
            capture_output=True,
        )
    assert result.stdout == b"BOUND\n"
    assert result.stderr == b""


def test_private_stage10_capture_rejects_untrusted_lease(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()

    with pytest.raises(RuntimeError, match="release_graph_lease_required"):
        _capture_domain_evaluator_candidate_under_lock(
            object(),
            run_dir=run,
            lease=object(),
            expected_stage="stage-10",
        )


@pytest.mark.parametrize("stage_name", ["stage-10", "stage-12"])
def test_private_capture_rejects_namespace_from_another_epoch(
    tmp_path: Path,
    stage_name: str,
) -> None:
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    for run in (run_a, run_b):
        (run / stage_name).mkdir(parents=True)
    with ReleaseGraphLock.acquire(run_a, "test", mode="read") as lease:
        with BoundOutputNamespace.open(
            run_b, run_b / stage_name, stage_name
        ) as namespace:
            with pytest.raises(RuntimeError, match="run_mismatch|epoch_mismatch"):
                _capture_domain_evaluator_candidate_under_lock(
                    namespace,
                    run_dir=run_a,
                    lease=lease,
                    expected_stage=stage_name,
                )


def test_private_capture_rejects_inactive_real_lease(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "stage-10").mkdir(parents=True)
    lease = ReleaseGraphLock.acquire(run, "test", mode="read")
    lease.close()
    with BoundOutputNamespace.open(
        run, run / "stage-10", "stage-10"
    ) as namespace:
        with pytest.raises(RuntimeError, match="lease_inactive"):
            _capture_domain_evaluator_candidate_under_lock(
                namespace,
                run_dir=run,
                lease=lease,
                expected_stage="stage-10",
            )


def test_private_stage12_capture_rejects_namespace_from_another_epoch(
    tmp_path: Path,
) -> None:
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    for run in (run_a, run_b):
        (run / "stage-12").mkdir(parents=True)
    with ReleaseGraphLock.acquire(run_a, "test", mode="read") as lease:
        with BoundOutputNamespace.open(
            run_b, run_b / "stage-12", "stage-12"
        ) as namespace:
            with pytest.raises(RuntimeError, match="run_mismatch"):
                stage12_domain_evaluator._capture_stage12_v2_authority(
                    namespace,
                    run_dir=run_a,
                    lease=lease,
                )


def test_private_capture_rejects_wrong_stage_namespace(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "stage-12").mkdir(parents=True)
    with ReleaseGraphLock.acquire(run, "test", mode="read") as lease:
        with BoundOutputNamespace.open(
            run, run / "stage-12", "stage-12"
        ) as namespace:
            with pytest.raises(RuntimeError, match="stage_mismatch"):
                _capture_domain_evaluator_candidate_under_lock(
                    namespace,
                    run_dir=run,
                    lease=lease,
                    expected_stage="stage-10",
                )
