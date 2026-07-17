"""Run the frozen TrojNet ISCAS-85 validation fixture.

This module is deliberately outside the canonical Stage 9-14 evaluator path.
It validates the domain fixture before a separately reviewed evaluator
migration. The paper workspace is never read at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


FIXTURE_ID = "trojnet_iscas85_graphsage_localization_v1"
FIXTURE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = FIXTURE_ROOT / "data" / "iscas85"
POLICY_PATH = FIXTURE_ROOT / "policy.json"
LAUNCHER_PATH = FIXTURE_ROOT.parents[3] / "scripts" / "validate_trojnet_fixture.py"
SOURCE_BUNDLE_SHA256 = "121db3fefe7a096b4beea9019c069b51027e1956eec7b744d9a2d19f5fe41d19"
CIRCUITS = ("c432", "c880", "c1355", "c1908", "c3540", "c6288")
SEEDS = (0, 1, 2)
VENDOR_FILES = (
    "__init__.py",
    "anomaly.py",
    "data.py",
    "eval.py",
    "model.py",
    "partition.py",
    "train.py",
)
DATA_FILES = tuple(
    relative
    for circuit in CIRCUITS
    for variant in (1, 2, 3)
    for relative in (
        f"{circuit}/{circuit}_ht{variant}.bench",
        f"{circuit}/{circuit}_ht{variant}_trojan_nodes.txt",
    )
)
METRIC_KEYS = (
    "auroc",
    "auprc",
    "top_k_precision",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "fpr",
)
CONDITIONS = (
    "raw_cc1",
    "scoap_isolation_forest",
    "trojnet_community_graphsage",
)


class FixtureIntegrityError(ValueError):
    """Raised when fixture source, data, or results are not exact."""


@dataclass(frozen=True)
class FixtureProfile:
    name: str
    circuits: tuple[str, ...]
    seeds: tuple[int, ...]
    epochs: int


PROFILES = {
    "smoke": FixtureProfile("smoke", ("c432",), (0,), 20),
    "validation": FixtureProfile("validation", CIRCUITS, SEEDS, 20),
}

EXPECTED_RUNTIME = {
    "python_major_minor": "3.11",
    "packages": {
        "networkx": "3.6.1",
        "numpy": "2.4.6",
        "scikit-learn": "1.9.0",
        "scipy": "1.17.1",
        "torch": "2.12.1",
        "torch-geometric": "2.8.0",
    },
    "deterministic_algorithms": True,
    "torch_num_threads": 1,
}


def _source_paths() -> list[Path]:
    paths = [
        FIXTURE_ROOT / "provenance.json",
        *(FIXTURE_ROOT / "vendor" / "trojnet" / name for name in VENDOR_FILES),
        *(DATA_ROOT / relative for relative in DATA_FILES),
    ]
    return sorted(paths, key=lambda path: path.relative_to(FIXTURE_ROOT).as_posix())


def _validate_source_namespace() -> None:
    vendor_root = FIXTURE_ROOT / "vendor" / "trojnet"
    vendor_entries = {path.name: path for path in vendor_root.iterdir()}
    if set(vendor_entries) != set(VENDOR_FILES):
        raise FixtureIntegrityError("fixture vendor namespace mismatch")
    for name in VENDOR_FILES:
        path = vendor_entries.get(name)
        if path is None or path.is_symlink() or not path.is_file():
            raise FixtureIntegrityError(f"fixture vendor source is invalid: {name}")
    data_entries = {path.name: path for path in DATA_ROOT.iterdir()}
    if set(data_entries) != set(CIRCUITS):
        raise FixtureIntegrityError("fixture data family namespace mismatch")
    for circuit, directory in data_entries.items():
        if directory.is_symlink() or not directory.is_dir():
            raise FixtureIntegrityError(f"fixture data family is invalid: {circuit}")
        expected = {
            Path(relative).name
            for relative in DATA_FILES
            if relative.startswith(f"{circuit}/")
        }
        actual = {path.name: path for path in directory.iterdir()}
        if set(actual) != expected:
            raise FixtureIntegrityError(
                f"fixture data namespace mismatch for family: {circuit}"
            )
        for name, path in actual.items():
            if path.is_symlink() or not path.is_file():
                raise FixtureIntegrityError(f"fixture data source is invalid: {name}")


def source_bundle_sha256() -> str:
    digest = hashlib.sha256()
    for path in _source_paths():
        if path.is_symlink():
            raise FixtureIntegrityError(f"fixture source may not be a symlink: {path}")
        relative = path.relative_to(FIXTURE_ROOT).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureIntegrityError(f"duplicate provenance key: {key}")
        result[key] = value
    return result


def _exact_value(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _exact_value(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _exact_value(item, value) for item, value in zip(actual, expected)
        )
    return actual == expected


def verify_source_bundle() -> str:
    _validate_source_namespace()
    paths = _source_paths()
    vendor = [path for path in paths if "/vendor/trojnet/" in f"/{path.as_posix()}"]
    data = [path for path in paths if "/data/iscas85/" in f"/{path.as_posix()}"]
    if len(vendor) != 7 or len(data) != 36 or len(paths) != 44:
        raise FixtureIntegrityError(
            f"fixture namespace mismatch: vendor={len(vendor)} data={len(data)} total={len(paths)}"
        )
    try:
        provenance = json.loads(
            (FIXTURE_ROOT / "provenance.json").read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureIntegrityError(f"fixture provenance is invalid: {exc}") from exc
    if set(provenance) != {
        "dataset_origin",
        "fixture_id",
        "paper_workspace_source",
        "source_code_sha256",
        "source_data_file_count",
        "source_data_sha256",
        "source_result_paths",
        "use_boundary",
    }:
        raise FixtureIntegrityError("fixture provenance schema mismatch")
    if (
        provenance["fixture_id"] != FIXTURE_ID
        or provenance["dataset_origin"] != "synthetic"
        or provenance["use_boundary"] != "pipeline_validation_only"
        or provenance["source_data_file_count"] != 36
    ):
        raise FixtureIntegrityError("fixture provenance policy mismatch")
    source_hashes = provenance["source_code_sha256"]
    if not isinstance(source_hashes, dict) or set(source_hashes) != {
        f"trojnet/{path.name}" for path in vendor
    }:
        raise FixtureIntegrityError("fixture source-code provenance mismatch")
    for relative, expected in source_hashes.items():
        path = FIXTURE_ROOT / "vendor" / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise FixtureIntegrityError(f"fixture source-code hash mismatch: {relative}")
    if _tree_sha256(DATA_ROOT) != provenance["source_data_sha256"]:
        raise FixtureIntegrityError("fixture source-data hash mismatch")
    actual = source_bundle_sha256()
    if actual != SOURCE_BUNDLE_SHA256:
        raise FixtureIntegrityError(
            f"fixture source digest mismatch: expected {SOURCE_BUNDLE_SHA256}, got {actual}"
        )
    return actual


def _policy_projection() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "profiles": {
            name: {
                "circuits": list(profile.circuits),
                "seeds": list(profile.seeds),
                "epochs": profile.epochs,
            }
            for name, profile in sorted(PROFILES.items())
        },
        "conditions": list(CONDITIONS),
        "metric_keys": list(METRIC_KEYS),
        "primary_metric": {"key": "auprc", "direction": "maximize"},
        "runtime": EXPECTED_RUNTIME,
    }


def _runtime_projection() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for name, expected in EXPECTED_RUNTIME["packages"].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise FixtureIntegrityError(
                f"required trojnet-validation dependency is missing: {name}"
            ) from exc
        if actual != expected:
            raise FixtureIntegrityError(
                f"trojnet-validation dependency mismatch: {name}={actual}, expected {expected}"
            )
        packages[name] = actual
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != EXPECTED_RUNTIME["python_major_minor"]:
        raise FixtureIntegrityError(
            f"Python minor mismatch: {actual_python}, expected {EXPECTED_RUNTIME['python_major_minor']}"
        )
    return {
        "python_major_minor": actual_python,
        "packages": packages,
        "deterministic_algorithms": True,
        "torch_num_threads": 1,
    }


def verify_evaluator_policy(*, require_runtime: bool = False) -> str:
    try:
        raw = POLICY_PATH.read_bytes()
        policy = json.loads(raw, object_pairs_hook=_strict_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureIntegrityError(f"fixture evaluator policy is invalid: {exc}") from exc
    if not isinstance(policy, dict) or set(policy) != {
        "schema_version",
        "fixture_id",
        "launcher_sha256",
        "runner_sha256",
        "profiles",
        "conditions",
        "metric_keys",
        "primary_metric",
        "runtime",
    }:
        raise FixtureIntegrityError("fixture evaluator policy schema mismatch")
    expected = _policy_projection()
    for key, value in expected.items():
        if not _exact_value(policy[key], value):
            raise FixtureIntegrityError(f"fixture evaluator policy mismatch: {key}")
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if policy["runner_sha256"] != runner_sha256:
        raise FixtureIntegrityError("fixture runner/policy binding mismatch")
    launcher_sha256 = hashlib.sha256(LAUNCHER_PATH.read_bytes()).hexdigest()
    if policy["launcher_sha256"] != launcher_sha256:
        raise FixtureIntegrityError("fixture launcher/policy binding mismatch")
    if require_runtime:
        _runtime_projection()
    return hashlib.sha256(raw).hexdigest()


def _normalise(values: Any) -> Any:
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    low = float(array.min())
    high = float(array.max())
    if high <= low:
        return np.zeros_like(array)
    return (array - low) / (high - low)


def _score_raw_cc1(data: Any, seed: int) -> Any:
    del seed
    import torch

    return torch.tensor(_normalise(data.x[:, 14].numpy()), dtype=torch.float32)


def _score_scoap_if(data: Any, seed: int) -> Any:
    import numpy as np
    import torch
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    features = data.x[:, [13, 14, 15]].numpy().astype(np.float64)
    if float(features.std(axis=0).max()) < 1e-8:
        return torch.zeros(data.num_nodes, dtype=torch.float32)
    scaled = StandardScaler().fit_transform(features)
    model = IsolationForest(
        n_estimators=200,
        contamination=0.05,
        random_state=seed,
        n_jobs=1,
    )
    model.fit(scaled)
    return torch.tensor(
        _normalise(-model.decision_function(scaled)), dtype=torch.float32
    )


def _score_trojnet(data: Any, seed: int, epochs: int) -> Any:
    import numpy as np
    import torch

    from .vendor.trojnet.anomaly import AnomalyScorer
    from .vendor.trojnet.partition import LouvainPartition
    from .vendor.trojnet.train import TrainConfig, TrojNetTrainer

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    config = TrainConfig(
        gnn_type="GraphSAGE",
        in_dim=data.x.size(1),
        hidden_dim=128,
        out_dim=64,
        proj_dim=32,
        epochs=epochs,
        lr=1e-3,
        temperature=0.5,
        p_feat=0.3,
        p_edge=0.4,
        device="cpu",
        seed=seed,
        log_every=epochs,
    )
    partitioner = LouvainPartition()
    _, data_with_ids = partitioner.partition(data)
    trainer = TrojNetTrainer(config=config, partitioner=partitioner)
    trainer.fit([data_with_ids])
    embeddings = trainer.embed(data_with_ids)
    return AnomalyScorer(
        contamination=0.05,
        random_state=seed,
        n_jobs=1,
    ).fit_score(embeddings, data_with_ids)


def _condition_scorers(epochs: int) -> dict[str, Callable[[Any, int], Any]]:
    return {
        "raw_cc1": _score_raw_cc1,
        "scoap_isolation_forest": _score_scoap_if,
        "trojnet_community_graphsage": (
            lambda data, seed: _score_trojnet(data, seed, epochs)
        ),
    }


def _metric_values(metrics: dict[str, Any]) -> dict[str, float]:
    aliases = {"accuracy": "acc"}
    values: dict[str, float] = {}
    for key in METRIC_KEYS:
        value = float(metrics[aliases.get(key, key)])
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise FixtureIntegrityError(f"invalid {key} metric: {value!r}")
        values[key] = value
    return values


def _summary(values: list[float]) -> dict[str, float]:
    import numpy as np

    if not values:
        raise FixtureIntegrityError("cannot summarize an empty metric sequence")
    array = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(np.mean(array)),
        "std": float(np.std(array, ddof=1)) if len(array) > 1 else 0.0,
        "min": float(np.min(array)),
        "max": float(np.max(array)),
    }


def _semantic_sha256(semantic: dict[str, Any]) -> str:
    text = json.dumps(
        semantic,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_per_seed(
    observations: list[dict[str, Any]], profile: FixtureProfile
) -> list[dict[str, Any]]:
    per_seed: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        for seed in profile.seeds:
            subset = [
                item
                for item in observations
                if item["condition"] == condition and item["seed"] == seed
            ]
            per_seed.append(
                {
                    "condition": condition,
                    "seed": seed,
                    "n_variants": len(subset),
                    "metrics": {
                        key: _summary([item["metrics"][key] for item in subset])["mean"]
                        for key in METRIC_KEYS
                    },
                }
            )
    return per_seed


def _build_aggregate(per_seed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregate: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        rows = [item for item in per_seed if item["condition"] == condition]
        aggregate.append(
            {
                "condition": condition,
                "n_seeds": len(rows),
                "metrics": {
                    key: _summary([item["metrics"][key] for item in rows])
                    for key in METRIC_KEYS
                },
            }
        )
    return aggregate


def run_fixture(profile_name: str = "smoke") -> dict[str, Any]:
    if os.environ.get("RESEARCHCLAW_TROJNET_STRICT_CHILD") != "1":
        raise FixtureIntegrityError(
            "fixture execution requires scripts/validate_trojnet_fixture.py"
        )
    if sys.pycache_prefix is None:
        raise FixtureIntegrityError("fixture execution requires an external pycache prefix")
    cache_prefix = Path(sys.pycache_prefix).resolve()
    if cache_prefix == FIXTURE_ROOT or FIXTURE_ROOT in cache_prefix.parents:
        raise FixtureIntegrityError("fixture pycache prefix must be outside the fixture")
    if any(path.name == "__pycache__" for path in FIXTURE_ROOT.rglob("__pycache__")):
        raise FixtureIntegrityError("fixture source tree contains a bytecode cache")
    bundle_sha256 = verify_source_bundle()
    try:
        profile = PROFILES[profile_name]
    except KeyError as exc:
        raise FixtureIntegrityError(f"unknown fixture profile: {profile_name}") from exc
    policy_sha256 = verify_evaluator_policy(require_runtime=True)

    from .vendor.trojnet.data import load_all_circuits
    from .vendor.trojnet.eval import evaluate_circuit
    import torch

    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(EXPECTED_RUNTIME["torch_num_threads"])

    data_list = load_all_circuits(DATA_ROOT, circuits=list(profile.circuits))
    expected_variants = len(profile.circuits) * 3
    if len(data_list) != expected_variants:
        raise FixtureIntegrityError(
            f"expected {expected_variants} circuit variants, got {len(data_list)}"
        )

    observations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    scorers = _condition_scorers(profile.epochs)
    for seed in profile.seeds:
        for data in data_list:
            family = str(data.circuit_name)
            variant = str(data.circuit_variant)
            n_trojan = int(data.y.sum().item())
            if n_trojan <= 0:
                raise FixtureIntegrityError(f"variant has no Trojan labels: {variant}")
            for condition in CONDITIONS:
                start_ns = time.perf_counter_ns()
                scores = scorers[condition](data, seed)
                elapsed_ns = time.perf_counter_ns() - start_ns
                metrics = evaluate_circuit(scores, data.y, top_k=n_trojan)
                observations.append(
                    {
                        "condition": condition,
                        "seed": seed,
                        "circuit_family": family,
                        "circuit_variant": variant,
                        "n_total": int(metrics["n_total"]),
                        "n_trojan": int(metrics["n_trojan"]),
                        "metrics": _metric_values(metrics),
                    }
                )
                diagnostics.append(
                    {
                        "condition": condition,
                        "seed": seed,
                        "circuit_variant": variant,
                        "runtime_sec": elapsed_ns / 1_000_000_000,
                    }
                )

    per_seed = _build_per_seed(observations, profile)
    aggregate = _build_aggregate(per_seed)

    semantic = {
        "schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "profile": profile.name,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "dataset_name": "controlled_synthetic_iscas85_trojan_localization_v1",
        "source_bundle_sha256": bundle_sha256,
        "evaluator_policy_sha256": policy_sha256,
        "runtime_environment": _runtime_projection(),
        "primary_metric": {
            "key": "auprc",
            "direction": "maximize",
        },
        "conditions": list(CONDITIONS),
        "seeds": list(profile.seeds),
        "circuits": list(profile.circuits),
        "epochs": profile.epochs,
        "observations": observations,
        "per_seed": per_seed,
        "aggregate": aggregate,
    }
    result = {
        **semantic,
        "semantic_sha256": _semantic_sha256(semantic),
        "diagnostics": diagnostics,
    }
    validate_result(result)
    return result


def validate_result(result: dict[str, Any]) -> None:
    verify_source_bundle()
    policy_sha256 = verify_evaluator_policy()
    expected_fields = {
        "schema_version",
        "fixture_id",
        "profile",
        "claim_scope",
        "dataset_origin",
        "dataset_name",
        "source_bundle_sha256",
        "evaluator_policy_sha256",
        "runtime_environment",
        "primary_metric",
        "conditions",
        "seeds",
        "circuits",
        "epochs",
        "observations",
        "per_seed",
        "aggregate",
        "semantic_sha256",
        "diagnostics",
    }
    if set(result) != expected_fields:
        raise FixtureIntegrityError("fixture result fields do not match schema v1")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise FixtureIntegrityError("fixture result schema_version must be integer 1")
    if result["fixture_id"] != FIXTURE_ID:
        raise FixtureIntegrityError("fixture result identity mismatch")
    if result["claim_scope"] != "pipeline_validation":
        raise FixtureIntegrityError("fixture may only use pipeline_validation scope")
    if result["dataset_origin"] != "synthetic":
        raise FixtureIntegrityError("fixture dataset_origin must be synthetic")
    if result["dataset_name"] != "controlled_synthetic_iscas85_trojan_localization_v1":
        raise FixtureIntegrityError("fixture dataset identity mismatch")
    if result["source_bundle_sha256"] != SOURCE_BUNDLE_SHA256:
        raise FixtureIntegrityError("fixture source bundle binding mismatch")
    if result["evaluator_policy_sha256"] != policy_sha256:
        raise FixtureIntegrityError("fixture evaluator policy binding mismatch")
    if not _exact_value(result["runtime_environment"], EXPECTED_RUNTIME):
        raise FixtureIntegrityError("fixture runtime environment binding mismatch")
    if result["primary_metric"] != {"key": "auprc", "direction": "maximize"}:
        raise FixtureIntegrityError("fixture primary metric mismatch")
    if result["conditions"] != list(CONDITIONS):
        raise FixtureIntegrityError("fixture condition sequence mismatch")
    profile = PROFILES.get(result["profile"])
    if profile is None:
        raise FixtureIntegrityError("fixture profile is invalid")
    if not isinstance(result["seeds"], list) or any(
        type(seed) is not int for seed in result["seeds"]
    ):
        raise FixtureIntegrityError("fixture profile seeds must be integers")
    if (
        result["seeds"] != list(profile.seeds)
        or result["circuits"] != list(profile.circuits)
        or result["epochs"] != profile.epochs
    ):
        raise FixtureIntegrityError("fixture profile projection mismatch")
    expected_observations = (
        len(profile.seeds) * len(profile.circuits) * 3 * len(CONDITIONS)
    )
    if len(result["observations"]) != expected_observations:
        raise FixtureIntegrityError("fixture observation count mismatch")
    expected_keys = {
        "condition",
        "seed",
        "circuit_family",
        "circuit_variant",
        "n_total",
        "n_trojan",
        "metrics",
    }
    identities: list[tuple[str, int, str]] = []
    for observation in result["observations"]:
        if not isinstance(observation, dict) or set(observation) != expected_keys:
            raise FixtureIntegrityError("fixture observation schema mismatch")
        condition = observation["condition"]
        seed = observation["seed"]
        family = observation["circuit_family"]
        variant = observation["circuit_variant"]
        if (
            condition not in CONDITIONS
            or type(seed) is not int
            or seed not in profile.seeds
        ):
            raise FixtureIntegrityError("fixture observation condition or seed mismatch")
        if (
            family not in profile.circuits
            or not isinstance(variant, str)
            or not variant.startswith(f"{family}_ht")
        ):
            raise FixtureIntegrityError("fixture observation circuit identity mismatch")
        if (
            type(observation["n_total"]) is not int
            or type(observation["n_trojan"]) is not int
            or not 0 < observation["n_trojan"] < observation["n_total"]
        ):
            raise FixtureIntegrityError("fixture observation counts are invalid")
        metrics = observation["metrics"]
        if not isinstance(metrics, dict) or set(metrics) != set(METRIC_KEYS):
            raise FixtureIntegrityError("fixture observation metric schema mismatch")
        for key, value in metrics.items():
            if type(value) is not float:
                raise FixtureIntegrityError(f"fixture metric {key} is not a float")
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise FixtureIntegrityError(f"fixture metric {key} is invalid")
        identities.append((condition, seed, variant))
    if len(identities) != len(set(identities)):
        raise FixtureIntegrityError("fixture observations contain duplicate identities")
    expected_identity_set = {
        (condition, seed, f"{circuit}_ht{variant}")
        for condition in CONDITIONS
        for seed in profile.seeds
        for circuit in profile.circuits
        for variant in (1, 2, 3)
    }
    if set(identities) != expected_identity_set:
        raise FixtureIntegrityError("fixture observation matrix is incomplete")
    expected_per_seed = _build_per_seed(result["observations"], profile)
    if not isinstance(result["per_seed"], list):
        raise FixtureIntegrityError("fixture per-seed summaries are invalid")
    for row in result["per_seed"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"condition", "seed", "n_variants", "metrics"}
            or type(row["seed"]) is not int
            or type(row["n_variants"]) is not int
        ):
            raise FixtureIntegrityError("fixture per-seed summary schema mismatch")
        metrics = row["metrics"]
        if not isinstance(metrics, dict) or set(metrics) != set(METRIC_KEYS):
            raise FixtureIntegrityError("fixture per-seed metric schema mismatch")
        for key, value in metrics.items():
            if type(value) is not float or not math.isfinite(value):
                raise FixtureIntegrityError(
                    f"fixture per-seed metric {key} is not a finite float"
                )
            if not 0.0 <= value <= 1.0:
                raise FixtureIntegrityError(f"fixture per-seed metric {key} is invalid")
    if result["per_seed"] != expected_per_seed:
        raise FixtureIntegrityError("fixture per-seed summaries do not replay")
    expected_aggregate = _build_aggregate(expected_per_seed)
    if not isinstance(result["aggregate"], list):
        raise FixtureIntegrityError("fixture aggregate summaries are invalid")
    for row in result["aggregate"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"condition", "n_seeds", "metrics"}
            or type(row["n_seeds"]) is not int
        ):
            raise FixtureIntegrityError("fixture aggregate summary schema mismatch")
        metrics = row["metrics"]
        if not isinstance(metrics, dict) or set(metrics) != set(METRIC_KEYS):
            raise FixtureIntegrityError("fixture aggregate metric schema mismatch")
        for key, summary in metrics.items():
            if not isinstance(summary, dict) or set(summary) != {
                "mean",
                "std",
                "min",
                "max",
            }:
                raise FixtureIntegrityError(
                    f"fixture aggregate metric {key} summary schema mismatch"
                )
            for statistic, value in summary.items():
                if type(value) is not float or not math.isfinite(value):
                    raise FixtureIntegrityError(
                        f"fixture aggregate metric {key}.{statistic} is not a finite float"
                    )
                if statistic == "std":
                    if value < 0.0:
                        raise FixtureIntegrityError(
                            f"fixture aggregate metric {key}.std is invalid"
                        )
                elif not 0.0 <= value <= 1.0:
                    raise FixtureIntegrityError(
                        f"fixture aggregate metric {key}.{statistic} is invalid"
                    )
    if result["aggregate"] != expected_aggregate:
        raise FixtureIntegrityError("fixture aggregate summaries do not replay")
    diagnostics = result["diagnostics"]
    if not isinstance(diagnostics, list) or len(diagnostics) != expected_observations:
        raise FixtureIntegrityError("fixture diagnostic count mismatch")
    diagnostic_identities: list[tuple[str, int, str]] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict) or set(diagnostic) != {
            "condition",
            "seed",
            "circuit_variant",
            "runtime_sec",
        }:
            raise FixtureIntegrityError("fixture diagnostic schema mismatch")
        runtime = diagnostic["runtime_sec"]
        if type(diagnostic["seed"]) is not int:
            raise FixtureIntegrityError("fixture diagnostic seed is invalid")
        if isinstance(runtime, bool) or not isinstance(runtime, (int, float)):
            raise FixtureIntegrityError("fixture runtime is not numeric")
        if not math.isfinite(runtime) or runtime < 0:
            raise FixtureIntegrityError("fixture runtime is invalid")
        diagnostic_identities.append(
            (diagnostic["condition"], diagnostic["seed"], diagnostic["circuit_variant"])
        )
    if diagnostic_identities != identities:
        raise FixtureIntegrityError("fixture diagnostic identities do not match observations")
    semantic = {key: value for key, value in result.items() if key not in {"semantic_sha256", "diagnostics"}}
    if result["semantic_sha256"] != _semantic_sha256(semantic):
        raise FixtureIntegrityError("fixture semantic digest mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_fixture(args.profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"fixture={FIXTURE_ID} profile={args.profile} semantic_sha256={result['semantic_sha256']}")


if __name__ == "__main__":
    main()
