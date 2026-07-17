from __future__ import annotations

import copy
import json
import math
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_trojnet_fixture import _assert_fixture_cache_free

from researchclaw.experiment_runtime.validation_fixtures.trojnet_iscas85_v1 import runner
from researchclaw.experiment_runtime.validation_fixtures.trojnet_iscas85_v1.runner import (
    CONDITIONS,
    EXPECTED_RUNTIME,
    FIXTURE_ID,
    METRIC_KEYS,
    PROFILES,
    SOURCE_BUNDLE_SHA256,
    FixtureIntegrityError,
    validate_result,
    verify_evaluator_policy,
    verify_source_bundle,
)


def _valid_smoke_result() -> dict[str, object]:
    profile = PROFILES["smoke"]
    observations: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for condition in CONDITIONS:
        for variant_number in (1, 2, 3):
            variant = f"c432_ht{variant_number}"
            observations.append(
                {
                    "condition": condition,
                    "seed": 0,
                    "circuit_family": "c432",
                    "circuit_variant": variant,
                    "n_total": 100,
                    "n_trojan": 5,
                    "metrics": {key: 0.5 for key in METRIC_KEYS},
                }
            )
            diagnostics.append(
                {
                    "condition": condition,
                    "seed": 0,
                    "circuit_variant": variant,
                    "runtime_sec": 0.0,
                }
            )
    per_seed = runner._build_per_seed(observations, profile)
    aggregate = runner._build_aggregate(per_seed)
    semantic: dict[str, object] = {
        "schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "profile": "smoke",
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "dataset_name": "controlled_synthetic_iscas85_trojan_localization_v1",
        "source_bundle_sha256": SOURCE_BUNDLE_SHA256,
        "evaluator_policy_sha256": verify_evaluator_policy(),
        "runtime_environment": copy.deepcopy(EXPECTED_RUNTIME),
        "primary_metric": {"key": "auprc", "direction": "maximize"},
        "conditions": list(CONDITIONS),
        "seeds": [0],
        "circuits": ["c432"],
        "epochs": 20,
        "observations": observations,
        "per_seed": per_seed,
        "aggregate": aggregate,
    }
    return {
        **semantic,
        "semantic_sha256": runner._semantic_sha256(semantic),
        "diagnostics": diagnostics,
    }


def _refresh_derived(result: dict[str, object]) -> None:
    profile = PROFILES[str(result["profile"])]
    observations = result["observations"]
    assert isinstance(observations, list)
    result["per_seed"] = runner._build_per_seed(observations, profile)
    result["aggregate"] = runner._build_aggregate(result["per_seed"])
    semantic = {
        key: value
        for key, value in result.items()
        if key not in {"semantic_sha256", "diagnostics"}
    }
    result["semantic_sha256"] = runner._semantic_sha256(semantic)


def test_trojnet_fixture_rejects_standard_bytecode_cache(
    tmp_path: Path, monkeypatch
) -> None:
    fixture_root = tmp_path / "fixture"
    shutil.copytree(
        runner.FIXTURE_ROOT,
        fixture_root,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    monkeypatch.setattr(runner, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(runner, "DATA_ROOT", fixture_root / "data" / "iscas85")
    py_compile.compile(
        str(fixture_root / "vendor" / "trojnet" / "model.py"),
        cfile=str(
            fixture_root
            / "vendor"
            / "trojnet"
            / "__pycache__"
            / f"model.{sys.implementation.cache_tag}.pyc"
        ),
        doraise=True,
    )
    with pytest.raises(FixtureIntegrityError, match="vendor namespace mismatch"):
        verify_source_bundle()


def test_strict_launcher_rejects_timestamp_valid_shadow_bytecode(
    tmp_path: Path, monkeypatch
) -> None:
    fixture_root = tmp_path / "fixture"
    shutil.copytree(
        runner.FIXTURE_ROOT,
        fixture_root,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    source = fixture_root / "vendor" / "trojnet" / "__init__.py"
    marker = tmp_path / "shadow-executed.txt"
    payload = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('shadow', encoding='utf-8')\n"
    ).encode("utf-8")
    source_size = source.stat().st_size
    assert len(payload) <= source_size
    evil_source = tmp_path / "evil.py"
    evil_source.write_bytes(payload + b"#" * (source_size - len(payload)))
    source_stat = source.stat()
    os.utime(evil_source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    cache_path = (
        source.parent
        / "__pycache__"
        / f"__init__.{sys.implementation.cache_tag}.pyc"
    )
    cache_path.parent.mkdir(parents=True)
    py_compile.compile(
        str(evil_source),
        cfile=str(cache_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )

    with pytest.raises(RuntimeError, match="executable bytecode cache"):
        _assert_fixture_cache_free(fixture_root)
    monkeypatch.setattr(runner, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(runner, "DATA_ROOT", fixture_root / "data" / "iscas85")
    with pytest.raises(FixtureIntegrityError, match="vendor namespace mismatch"):
        verify_source_bundle()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fixture_root / "vendor")
    env.pop("PYTHONPYCACHEPREFIX", None)
    completed = subprocess.run(
        [sys.executable, "-c", "import trojnet"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "shadow"


def test_trojnet_fixture_evaluator_policy_binds_runner(tmp_path: Path, monkeypatch) -> None:
    policy = json.loads(runner.POLICY_PATH.read_text(encoding="utf-8"))
    policy["runner_sha256"] = "0" * 64
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(runner, "POLICY_PATH", path)
    with pytest.raises(FixtureIntegrityError, match="runner/policy binding mismatch"):
        verify_evaluator_policy()


def test_trojnet_fixture_evaluator_policy_binds_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = tmp_path / "launcher.py"
    launcher.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "LAUNCHER_PATH", launcher)
    with pytest.raises(FixtureIntegrityError, match="launcher/policy binding mismatch"):
        verify_evaluator_policy()


def test_trojnet_fixture_runtime_dependency_check_is_not_a_skip(monkeypatch) -> None:
    real_version = runner.importlib.metadata.version

    def missing(name: str) -> str:
        if name == "torch-geometric":
            raise runner.importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(runner.importlib.metadata, "version", missing)
    with pytest.raises(FixtureIntegrityError, match="dependency is missing"):
        verify_evaluator_policy(require_runtime=True)


def test_trojnet_fixture_execution_requires_strict_launcher(monkeypatch) -> None:
    monkeypatch.delenv("RESEARCHCLAW_TROJNET_STRICT_CHILD", raising=False)
    with pytest.raises(FixtureIntegrityError, match="requires scripts/validate"):
        runner.run_fixture("smoke")


def test_trojnet_fixture_validator_accepts_exact_smoke_matrix() -> None:
    validate_result(_valid_smoke_result())


def test_trojnet_fixture_rejects_bool_runtime_policy_value() -> None:
    result = _valid_smoke_result()
    result["runtime_environment"]["torch_num_threads"] = True  # type: ignore[index]
    with pytest.raises(FixtureIntegrityError, match="runtime environment binding"):
        validate_result(result)


def test_trojnet_fixture_rejects_missing_observation() -> None:
    result = _valid_smoke_result()
    result["observations"].pop()  # type: ignore[union-attr]
    _refresh_derived(result)
    with pytest.raises(FixtureIntegrityError, match="observation count mismatch"):
        validate_result(result)


def test_trojnet_fixture_rejects_duplicate_observation() -> None:
    result = _valid_smoke_result()
    observations = result["observations"]
    assert isinstance(observations, list)
    observations[-1] = copy.deepcopy(observations[0])
    _refresh_derived(result)
    with pytest.raises(FixtureIntegrityError, match="duplicate identities"):
        validate_result(result)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_trojnet_fixture_rejects_nonfinite_metric(value: float) -> None:
    result = _valid_smoke_result()
    result["observations"][0]["metrics"]["auprc"] = value  # type: ignore[index]
    with pytest.raises(FixtureIntegrityError, match="metric auprc is invalid"):
        validate_result(result)


@pytest.mark.parametrize("location", ["per_seed", "aggregate"])
@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf, "1.0"])
def test_trojnet_fixture_rejects_invalid_derived_metric(
    location: str, value: object
) -> None:
    result = _valid_smoke_result()
    observations = result["observations"]
    assert isinstance(observations, list)
    for observation in observations:
        observation["metrics"]["auprc"] = 1.0
    _refresh_derived(result)
    if location == "per_seed":
        result["per_seed"][0]["metrics"]["auprc"] = value  # type: ignore[index]
        match = "per-seed metric auprc is not a finite float"
    else:
        result["aggregate"][0]["metrics"]["auprc"]["mean"] = value  # type: ignore[index]
        match = "aggregate metric auprc.mean is not a finite float"
    if value is True or isinstance(value, str):
        semantic = {
            key: item
            for key, item in result.items()
            if key not in {"semantic_sha256", "diagnostics"}
        }
        result["semantic_sha256"] = runner._semantic_sha256(semantic)
    with pytest.raises(FixtureIntegrityError, match=match):
        validate_result(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [("circuit_family", "c880"), ("circuit_variant", "c880_ht1")],
)
def test_trojnet_fixture_rejects_wrong_circuit_identity(field: str, value: str) -> None:
    result = _valid_smoke_result()
    result["observations"][0][field] = value  # type: ignore[index]
    with pytest.raises(FixtureIntegrityError, match="circuit identity mismatch"):
        validate_result(result)


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("location", ["top", "observation", "per_seed", "diagnostic"])
def test_trojnet_fixture_rejects_bool_seed(location: str, value: bool) -> None:
    result = _valid_smoke_result()
    if location == "top":
        result["seeds"][0] = value  # type: ignore[index]
    elif location == "observation":
        result["observations"][0]["seed"] = value  # type: ignore[index]
    elif location == "per_seed":
        result["per_seed"][0]["seed"] = value  # type: ignore[index]
    else:
        result["diagnostics"][0]["seed"] = value  # type: ignore[index]
    with pytest.raises(FixtureIntegrityError, match="seed"):
        validate_result(result)


def test_trojnet_fixture_rejects_synchronized_aggregate_tamper() -> None:
    result = _valid_smoke_result()
    result["observations"][0]["metrics"]["auprc"] = 0.99  # type: ignore[index]
    semantic = {
        key: value
        for key, value in result.items()
        if key not in {"semantic_sha256", "diagnostics"}
    }
    result["semantic_sha256"] = runner._semantic_sha256(semantic)
    with pytest.raises(FixtureIntegrityError, match="per-seed summaries do not replay"):
        validate_result(result)
