"""Strict schemas and replay primitives for canonical experiment evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tokenize
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    ExperimentContract,
    contract_sha256,
    find_stage09_contract,
    load_contract,
    sha256_file,
)
from researchclaw.experiment_runtime.scaffold import render_main_py, scaffold_sha256
from researchclaw.experiment_runtime.metric_authority import (
    MetricAuthorityError,
    replay_metric_authority,
)
from researchclaw.literature.citation_policy import (
    ConfigSnapshotNamespaceInputs,
    replay_config_snapshot_namespace,
    resolve_active_config_snapshot,
)
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock


STAGE10_SEAL_SCHEMA_VERSION = 2
SEAL_POLICY_VERSION = 1
CONFIG_SEMANTIC_POLICY_VERSION = 1
RESULT_SET_SCHEMA_VERSION = 1
REFINEMENT_SCHEMA_VERSION = 1
REFINEMENT_VALIDATION_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
CANONICAL_MANIFEST_SCHEMA_VERSION = 1
INVOCATION_JOURNAL_SCHEMA_VERSION = 1
EVALUATOR_DECIMAL_PRECISION = 50

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")
_ITERATION_ID_RE = re.compile(r"iter-([1-9]\d*)\Z")
_CANDIDATE_ID_RE = re.compile(r"cand-([0-9a-f]{64})\Z")
_STAGE14_DIR_RE = re.compile(r"stage-14(?:_v[1-9]\d*)?\Z")
_CONFIG_SNAPSHOT_RE = re.compile(r"config(?:\.resumed-[A-Za-z0-9_.-]+)?\.yaml\Z")
_MANDATORY_CANDIDATE_ROLES = {
    "analysis": "analysis.md",
    "figure_plan": "figure_plan.json",
    "results_table": "results_table.tex",
    "summary": "experiment_summary.json",
}
_FIGURE_AGENT_INTERMEDIATE_NAMES = frozenset(
    {
        "figure_decisions.json",
        "figure_plan_code.json",
        "figure_plan_final.json",
        "nano_banana_results.json",
    }
)


class CanonicalExperimentEvidenceError(ValueError):
    """Raised when canonical experiment evidence is not strictly replayable."""


@dataclass(frozen=True)
class CanonicalEvidenceArtifact:
    """One immutable in-memory artifact from the selected Stage 14 candidate."""

    role: str
    path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class CanonicalProjectArtifact:
    """One immutable selected Stage 10/13 project file."""

    logical_name: str
    source_path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class CanonicalExperimentEvidence:
    """A lock-consistent, replay-validated snapshot for downstream consumers."""

    manifest_path: str
    manifest_sha256: str
    manifest: Mapping[str, Any]
    candidate_manifest_path: str
    candidate_manifest_sha256: str
    candidate: Mapping[str, Any]
    selected_result_manifest_path: str
    selected_result_manifest_sha256: str
    selected_result: Mapping[str, Any]
    selected_execution_artifact: CanonicalEvidenceArtifact
    experiment_contract_path: str
    experiment_contract_sha256: str
    experiment_contract_bytes: bytes
    run_config_path: str
    run_config_sha256: str
    run_config_bytes: bytes
    summary_bytes: bytes
    summary: Mapping[str, Any]
    analysis_bytes: bytes
    analysis_text: str
    metric_observations: Mapping[str, Any]
    structured_results: Mapping[str, Any]
    artifacts: tuple[CanonicalEvidenceArtifact, ...]
    project_artifacts: tuple[CanonicalProjectArtifact, ...]


@dataclass(frozen=True)
class _CanonicalPublicationPlan:
    manifest: Mapping[str, Any]
    summary_bytes: bytes
    analysis_bytes: bytes


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_config_sha256(config: RCConfig) -> str:
    """Hash the complete parsed configuration under semantic policy v1."""
    return sha256_text(canonical_json_text(config.to_dict()))


def canonical_decimal(value: object) -> str:
    """Return the finite canonical Decimal string accepted by selection policy v1."""
    if isinstance(value, bool):
        raise CanonicalExperimentEvidenceError("metric value must not be bool")
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            raise CanonicalExperimentEvidenceError("metric value must be finite")
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raise CanonicalExperimentEvidenceError("metric value must be numeric")
    if not _DECIMAL_RE.fullmatch(raw):
        raise CanonicalExperimentEvidenceError("metric value has noncanonical grammar")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise CanonicalExperimentEvidenceError("metric value is invalid") from exc
    if not number.is_finite():
        raise CanonicalExperimentEvidenceError("metric value must be finite")
    if number == 0:
        return "0"
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def invocation_generation_binding_sha256(
    *,
    experiment_contract_sha256: str,
    sealed_candidate_manifest_sha256: str,
    config_semantic_sha256: str,
    experiment_mode: str,
    evaluator_schema: str,
) -> str:
    """Build the immutable single-invocation generation identity."""
    for field, value in (
        ("experiment_contract_sha256", experiment_contract_sha256),
        ("sealed_candidate_manifest_sha256", sealed_candidate_manifest_sha256),
        ("config_semantic_sha256", config_semantic_sha256),
    ):
        _sha256(value, field)
    if experiment_mode not in {"sandbox", "docker"}:
        raise CanonicalExperimentEvidenceError("unsupported canonical experiment mode")
    _required_string(evaluator_schema, "evaluator_schema")
    payload = {
        "config_semantic_sha256": config_semantic_sha256,
        "evaluator_schema": evaluator_schema,
        "experiment_contract_sha256": experiment_contract_sha256,
        "experiment_mode": experiment_mode,
        "invocation_policy_version": 1,
        "sealed_candidate_manifest_sha256": sealed_candidate_manifest_sha256,
    }
    return sha256_text(canonical_json_text(payload))


def parse_invocation_result(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 12 invocation result")
    _exact_keys(
        payload,
        {
            "schema_version", "invocation_policy_version", "ordinal", "status",
            "evaluator_schema", "metric_observations", "structured_results",
        },
        "Stage 12 invocation result",
    )
    _require_equal(payload, "schema_version", 1)
    _require_equal(payload, "invocation_policy_version", 1)
    _require_equal(payload, "ordinal", 1)
    _require_equal(payload, "status", "completed")
    _required_string(payload["evaluator_schema"], "evaluator_schema")
    _metric_observations(payload["metric_observations"])
    _structured_results(payload["structured_results"])
    return payload


def parse_aggregate_results(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 12 aggregate results")
    _exact_keys(
        payload,
        {
            "schema_version", "aggregation_policy_version", "evaluator_schema",
            "source_ordinals", "metric_observations", "structured_results",
        },
        "Stage 12 aggregate results",
    )
    _require_equal(payload, "schema_version", 1)
    _require_equal(payload, "aggregation_policy_version", 1)
    _required_string(payload["evaluator_schema"], "evaluator_schema")
    if (
        not isinstance(payload["source_ordinals"], list)
        or len(payload["source_ordinals"]) != 1
        or _strict_int(payload["source_ordinals"][0], "source ordinal") != 1
    ):
        raise CanonicalExperimentEvidenceError("source_ordinals must equal [1]")
    _metric_observations(payload["metric_observations"])
    _structured_results(payload["structured_results"])
    return payload


def validate_single_invocation_aggregate(
    invocation_text: str,
    aggregate_text: str,
    *,
    contract: ExperimentContract,
) -> tuple[dict[str, Any], dict[str, Any]]:
    invocation = parse_invocation_result(invocation_text)
    aggregate = parse_aggregate_results(aggregate_text)
    if invocation["evaluator_schema"] != aggregate["evaluator_schema"]:
        raise CanonicalExperimentEvidenceError("aggregate evaluator schema mismatch")
    if invocation["metric_observations"] != aggregate["metric_observations"]:
        raise CanonicalExperimentEvidenceError("aggregate metric observations mismatch")
    if invocation["structured_results"] != aggregate["structured_results"]:
        raise CanonicalExperimentEvidenceError("aggregate structured results mismatch")
    expected_observations = _validate_evaluator_result(
        invocation["structured_results"], contract, invocation["evaluator_schema"]
    )
    if invocation["metric_observations"] != expected_observations:
        raise CanonicalExperimentEvidenceError("normalized metrics differ from evaluator result")
    return invocation, aggregate


def build_stage12_evidence_texts(
    structured_results_text: str,
    *,
    contract: ExperimentContract,
    evaluator_schema: str,
) -> tuple[str, str]:
    """Normalize one evaluator result into replayable Stage 12 evidence."""
    structured_results = _parse_object(structured_results_text, "evaluator results")
    observations = _validate_evaluator_result(
        structured_results, contract, evaluator_schema
    )
    invocation = {
        "schema_version": 1,
        "invocation_policy_version": 1,
        "ordinal": 1,
        "status": "completed",
        "evaluator_schema": evaluator_schema,
        "metric_observations": observations,
        "structured_results": structured_results,
    }
    aggregate = {
        "schema_version": 1,
        "aggregation_policy_version": 1,
        "evaluator_schema": evaluator_schema,
        "source_ordinals": [1],
        "metric_observations": observations,
        "structured_results": structured_results,
    }
    invocation_text = canonical_authority_json_text(invocation)
    aggregate_text = canonical_authority_json_text(aggregate)
    validate_single_invocation_aggregate(
        invocation_text, aggregate_text, contract=contract
    )
    return invocation_text, aggregate_text


def build_refinement_execution_text(
    structured_results_text: str,
    *,
    contract: ExperimentContract,
    evaluator_schema: str,
) -> str:
    """Normalize one Stage 13 evaluator result with the Stage 12 authority grammar."""
    invocation_text, _aggregate_text = build_stage12_evidence_texts(
        structured_results_text,
        contract=contract,
        evaluator_schema=evaluator_schema,
    )
    return invocation_text


def build_refinement_validation_report(
    project_root: Path,
    project_files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the replayable Stage 13 syntax report from staged project bytes."""
    canonical_refs = sorted(project_files, key=lambda item: item["path"])
    if project_files != canonical_refs:
        raise CanonicalExperimentEvidenceError(
            "Stage 13 project files are not canonically sorted"
        )
    syntax_valid = True
    for ref in canonical_refs:
        path = ref.get("path")
        digest = ref.get("sha256")
        if not isinstance(path, str) or "/project/" not in path:
            raise CanonicalExperimentEvidenceError("noncanonical Stage 13 project path")
        logical_name = path.rsplit("/project/", 1)[1]
        if not logical_name or "/" in logical_name or "\\" in logical_name:
            raise CanonicalExperimentEvidenceError(
                "Stage 13 project must preserve flat sealed filenames"
            )
        source_path = project_root / logical_name
        if source_path.is_symlink() or not source_path.is_file():
            raise CanonicalExperimentEvidenceError("Stage 13 staged project file is unsafe")
        if sha256_file(source_path) != digest:
            raise CanonicalExperimentEvidenceError("Stage 13 staged project hash mismatch")
        if not logical_name.endswith(".py"):
            continue
        raw = source_path.read_bytes()
        try:
            encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
            source = raw.decode(encoding)
            compile(source, path, "exec")
        except (LookupError, SyntaxError, UnicodeDecodeError, ValueError):
            syntax_valid = False
    return {
        "schema_version": REFINEMENT_VALIDATION_SCHEMA_VERSION,
        "validation_policy_version": 1,
        "project_files_sha256": sha256_text(canonical_json_text(canonical_refs)),
        "checks": {"python_syntax_valid": syntax_valid},
    }


def stage12_primary_metric(
    run_dir: Path,
    baseline: Mapping[str, Any],
    metric_key: str,
) -> Decimal:
    """Return the independently replayed Stage 12 primary metric."""
    return _stage12_primary_metric(run_dir, baseline, metric_key)


def canonical_authority_json_text(value: object) -> str:
    """Serialize strict authority JSON without a Decimal-to-float conversion."""
    return _authority_json_value(value) + "\n"


def _authority_json_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, float):
        raise CanonicalExperimentEvidenceError(
            "authority JSON must not contain binary float values"
        )
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_authority_json_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalExperimentEvidenceError(
                "authority JSON object keys must be strings"
            )
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _authority_json_value(value[key])
            for key in sorted(value)
        ) + "}"
    raise CanonicalExperimentEvidenceError(
        f"unsupported authority JSON type: {type(value).__name__}"
    )


def _validate_evaluator_result(
    value: object,
    contract: ExperimentContract,
    evaluator_schema: str,
) -> dict[str, list[int | float | Decimal]]:
    if evaluator_schema != "hpc_anomaly_detection_v1":
        raise CanonicalExperimentEvidenceError("unsupported evaluator schema")
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("HPC evaluator result must be an object")
    _exact_keys(
        value,
        {
            "schema_version", "claim_scope", "dataset_origin", "dataset_name",
            "primary_metric", "metrics", "seeds", "conditions", "per_seed",
            "runtime_sec", "evaluator_owner",
        },
        "HPC evaluator result",
    )
    _require_equal(value, "schema_version", 1)
    if (
        value["claim_scope"] != contract.claim_scope
        or value["dataset_origin"] != contract.dataset_origin
        or value["dataset_name"] != contract.dataset_name
    ):
        raise CanonicalExperimentEvidenceError("HPC evaluator contract binding mismatch")
    if value["evaluator_owner"] != "scaffold":
        raise CanonicalExperimentEvidenceError("HPC evaluator owner mismatch")
    if value["seeds"] != [42, 123, 256]:
        raise CanonicalExperimentEvidenceError("HPC evaluator seeds mismatch")
    if value["conditions"] != ["DetectorPlugin"]:
        raise CanonicalExperimentEvidenceError("HPC evaluator conditions mismatch")
    runtime_sec = _finite_json_number(value["runtime_sec"], "runtime_sec")
    if runtime_sec < 0:
        raise CanonicalExperimentEvidenceError("runtime_sec must be nonnegative")

    metric_keys = {
        "accuracy", "detection_f1", "precision", "tpr", "tnr", "fpr", "latency_ms",
    }
    metrics = _hpc_metric_map(value["metrics"], metric_keys, "HPC aggregate metrics")
    per_seed = value["per_seed"]
    if not isinstance(per_seed, list) or len(per_seed) != 3:
        raise CanonicalExperimentEvidenceError("HPC per_seed must have three entries")
    seed_metrics: list[dict[str, int | float | Decimal]] = []
    for expected_seed, item in zip((42, 123, 256), per_seed, strict=True):
        if not isinstance(item, dict):
            raise CanonicalExperimentEvidenceError("HPC per_seed entry must be an object")
        _exact_keys(item, {"seed", "metrics"}, "HPC per_seed entry")
        if _strict_int(item["seed"], "HPC seed") != expected_seed:
            raise CanonicalExperimentEvidenceError("HPC per_seed order mismatch")
        seed_metrics.append(_hpc_metric_map(item["metrics"], metric_keys, "HPC per-seed metrics"))
    for key in sorted(metric_keys):
        expected = _decimal_mean(item[key] for item in seed_metrics)
        if Decimal(canonical_decimal(metrics[key])) != Decimal(canonical_decimal(expected)):
            raise CanonicalExperimentEvidenceError(f"HPC aggregate metric mismatch: {key}")

    primary = value["primary_metric"]
    if not isinstance(primary, dict):
        raise CanonicalExperimentEvidenceError("HPC primary_metric must be an object")
    _exact_keys(primary, {"key", "value", "direction"}, "HPC primary_metric")
    metric_key = _required_string(contract.primary_metric.get("key"), "contract primary metric")
    direction = contract.primary_metric.get("direction")
    if primary["key"] != metric_key or primary["direction"] != direction:
        raise CanonicalExperimentEvidenceError("HPC primary metric policy mismatch")
    if metric_key not in metrics:
        raise CanonicalExperimentEvidenceError("HPC primary metric key is absent from metrics")
    if Decimal(canonical_decimal(primary["value"])) != Decimal(canonical_decimal(metrics[metric_key])):
        raise CanonicalExperimentEvidenceError("HPC primary metric value mismatch")
    return {metric_key: [metrics[metric_key]]}


def _hpc_metric_map(
    value: object,
    expected_keys: set[str],
    label: str,
) -> dict[str, int | float | Decimal]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError(f"{label} must be an object")
    _exact_keys(value, expected_keys, label)
    result: dict[str, int | float | Decimal] = {}
    for key in sorted(expected_keys):
        number = _finite_json_number(value[key], f"{label}.{key}")
        if key == "latency_ms":
            if number < 0:
                raise CanonicalExperimentEvidenceError("latency_ms must be nonnegative")
        elif number < 0 or number > 1:
            raise CanonicalExperimentEvidenceError(f"{label}.{key} must be in 0..1")
        result[key] = value[key]
    return result


def _finite_json_number(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CanonicalExperimentEvidenceError(f"{field} must be a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalExperimentEvidenceError(f"{field} must be finite")
    if isinstance(value, Decimal) and not value.is_finite():
        raise CanonicalExperimentEvidenceError(f"{field} must be finite")
    return Decimal(canonical_decimal(value))


def _decimal_mean(values: object) -> Decimal:
    numbers = [Decimal(canonical_decimal(value)) for value in values]
    if not numbers:
        raise CanonicalExperimentEvidenceError("cannot aggregate an empty metric sequence")
    with localcontext() as context:
        context.prec = EVALUATOR_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return sum(numbers, Decimal(0)) / Decimal(len(numbers))


def parse_selected_candidate_manifest(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "selected candidate manifest")
    _exact_keys(
        payload,
        {
            "schema_version", "seal_policy_version", "producer_input_type",
            "contract_path", "contract_sha256", "run_config_path",
            "run_config_sha256", "config_semantic_policy_version",
            "config_semantic_sha256", "scaffold_sha256", "entry_point",
            "metric_authority", "files", "scaffold_files", "plugin_files",
        },
        "selected candidate manifest",
    )
    _require_equal(payload, "schema_version", STAGE10_SEAL_SCHEMA_VERSION)
    _require_equal(payload, "seal_policy_version", SEAL_POLICY_VERSION)
    _require_equal(payload, "producer_input_type", "sealed_python_candidate")
    _safe_relative_path(payload["contract_path"], "contract_path")
    _safe_relative_path(payload["run_config_path"], "run_config_path")
    for field in (
        "contract_sha256", "run_config_sha256", "config_semantic_sha256",
        "scaffold_sha256",
    ):
        _sha256(payload[field], field)
    _require_equal(
        payload,
        "config_semantic_policy_version",
        CONFIG_SEMANTIC_POLICY_VERSION,
    )
    _metric_authority_identity(payload["metric_authority"])
    if payload["entry_point"] != "main.py":
        raise CanonicalExperimentEvidenceError("entry_point must be main.py")
    files = _file_map(payload["files"], "files", owner=None, nonempty=True)
    scaffold = _file_map(payload["scaffold_files"], "scaffold_files", owner="scaffold")
    plugins = _file_map(payload["plugin_files"], "plugin_files", owner="model")
    if set(scaffold) & set(plugins) or set(scaffold) | set(plugins) != set(files):
        raise CanonicalExperimentEvidenceError("owner maps must be disjoint and cover files")
    for name, metadata in {**scaffold, **plugins}.items():
        if metadata["sha256"] != files[name]["sha256"]:
            raise CanonicalExperimentEvidenceError(f"owner hash mismatch for {name}")
    if "main.py" not in files:
        raise CanonicalExperimentEvidenceError("selected candidate is missing main.py")
    return payload


def validate_selected_candidate_manifest(
    run_dir: Path,
    config: RCConfig,
    text: str | None = None,
) -> dict[str, Any]:
    """Replay Stage 10 seal identity, config, ownership, and exact file closure."""
    manifest_path = run_dir / "stage-10" / "selected_candidate_manifest.json"
    if text is None:
        text = _read_regular_file(manifest_path, "selected candidate manifest")
    payload = parse_selected_candidate_manifest(text)

    contract_path = find_stage09_contract(run_dir)
    if contract_path is None:
        raise CanonicalExperimentEvidenceError("canonical experiment contract is missing")
    try:
        contract_relative = contract_path.relative_to(run_dir).as_posix()
        contract = load_contract(contract_path)
    except (ValueError, ContractValidationError) as exc:
        raise CanonicalExperimentEvidenceError(f"canonical contract is invalid: {exc}") from exc
    if payload["contract_path"] != contract_relative:
        raise CanonicalExperimentEvidenceError("contract_path does not match canonical selector")
    if contract_sha256(contract_path) != payload["contract_sha256"]:
        raise CanonicalExperimentEvidenceError("contract hash mismatch")

    try:
        replay_metric_authority(
            run_dir=run_dir,
            topic=config.research.topic,
            experiment_mode=config.experiment.mode,
            stored_identity=contract.metric_authority,
            metric_units=contract.metric_units,
            metric_display_labels=contract.metric_display_labels,
        )
    except MetricAuthorityError as exc:
        raise CanonicalExperimentEvidenceError(
            f"metric authority replay failed: {exc}"
        ) from exc
    if payload["metric_authority"] != contract.metric_authority:
        raise CanonicalExperimentEvidenceError("Stage 10 metric authority mismatch")

    config_text, config_hash = _resolve_producer_config_identity(
        run_dir, config, payload["run_config_path"], payload["run_config_sha256"]
    )
    if payload["config_semantic_sha256"] != semantic_config_sha256(config):
        raise CanonicalExperimentEvidenceError("config semantic hash mismatch")
    if sha256_text(config_text) != config_hash:
        raise CanonicalExperimentEvidenceError("run config exact hash mismatch")
    if payload["scaffold_sha256"] != scaffold_sha256():
        raise CanonicalExperimentEvidenceError("scaffold hash mismatch")

    selected_dir = run_dir / "stage-10" / "selected_candidate"
    if selected_dir.is_symlink() or not selected_dir.is_dir():
        raise CanonicalExperimentEvidenceError("selected candidate directory is missing or unsafe")
    entries = list(selected_dir.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise CanonicalExperimentEvidenceError("selected candidate must be a flat regular-file set")
    actual = {entry.name for entry in entries}
    if actual != set(payload["files"]):
        raise CanonicalExperimentEvidenceError("selected candidate file-set mismatch")
    for name, metadata in payload["files"].items():
        if sha256_file(selected_dir / name) != metadata["sha256"]:
            raise CanonicalExperimentEvidenceError(f"selected candidate hash mismatch: {name}")
    canonical_scaffold = (
        contract.claim_scope == "pipeline_validation"
        or not config.experiment.allow_legacy_experiment_path
    )
    expected_scaffold = {"main.py"} if canonical_scaffold else set()
    if set(payload["scaffold_files"]) != expected_scaffold:
        raise CanonicalExperimentEvidenceError("scaffold ownership differs from producer policy")
    if set(payload["plugin_files"]) != set(payload["files"]) - expected_scaffold:
        raise CanonicalExperimentEvidenceError("plugin ownership differs from producer policy")
    if canonical_scaffold:
        if set(payload["files"]) != {"main.py", "detector_plugin.py"}:
            raise CanonicalExperimentEvidenceError("invalid selected candidate file closure")
        if (selected_dir / "main.py").read_bytes() != render_main_py(contract).encode("utf-8"):
            raise CanonicalExperimentEvidenceError("scaffold-owned main.py replay mismatch")
    return payload


def parse_execution_invocation_journal(text: str) -> tuple[dict[str, Any], ...]:
    if not text or not text.endswith("\n"):
        raise CanonicalExperimentEvidenceError("invocation journal must end with one newline")
    lines = text.split("\n")[:-1]
    if len(lines) not in {1, 2} or any(not line for line in lines):
        raise CanonicalExperimentEvidenceError("invocation journal has invalid record count")
    records = tuple(_parse_object(line, "invocation journal record") for line in lines)
    started = records[0]
    _exact_keys(
        started,
        {
            "schema_version", "event", "ordinal", "invocation_token",
            "generation_binding_sha256", "experiment_contract_sha256",
            "sealed_candidate_manifest_sha256", "config_semantic_sha256",
        },
        "invocation started record",
    )
    _require_equal(started, "schema_version", INVOCATION_JOURNAL_SCHEMA_VERSION)
    _require_equal(started, "event", "started")
    _require_equal(started, "ordinal", 1)
    _token(started["invocation_token"], "invocation_token")
    for field in (
        "generation_binding_sha256", "experiment_contract_sha256",
        "sealed_candidate_manifest_sha256", "config_semantic_sha256",
    ):
        _sha256(started[field], field)
    if len(records) == 2:
        terminal = records[1]
        _exact_keys(
            terminal,
            {
                "schema_version", "event", "ordinal", "invocation_token",
                "status", "result_path", "result_sha256", "failure_code",
            },
            "invocation terminal record",
        )
        _require_equal(terminal, "schema_version", INVOCATION_JOURNAL_SCHEMA_VERSION)
        _require_equal(terminal, "event", "terminal")
        _require_equal(terminal, "ordinal", 1)
        if terminal["invocation_token"] != started["invocation_token"]:
            raise CanonicalExperimentEvidenceError("invocation token mismatch")
        if terminal["status"] == "completed":
            if terminal["result_path"] != "stage-12/evidence-v1/run-1.json":
                raise CanonicalExperimentEvidenceError("noncanonical completed result path")
            _sha256(terminal["result_sha256"], "result_sha256")
            if terminal["failure_code"] is not None:
                raise CanonicalExperimentEvidenceError("completed invocation has failure_code")
        elif terminal["status"] == "failed":
            if terminal["result_path"] is not None or terminal["result_sha256"] is not None:
                raise CanonicalExperimentEvidenceError("failed invocation has result authority")
            _required_string(terminal["failure_code"], "failure_code")
        else:
            raise CanonicalExperimentEvidenceError("invalid invocation terminal status")
    return records


def parse_experiment_result_set(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 12 result set")
    _exact_keys(
        payload,
        {
            "schema_version", "result_set_policy_version", "result_set_type",
            "experiment_mode", "experiment_contract_path", "experiment_contract_sha256",
            "sealed_candidate_manifest_path", "sealed_candidate_manifest_sha256",
            "run_config_path", "run_config_sha256", "config_semantic_policy_version",
            "config_semantic_sha256", "claim_scope", "dataset_origin",
            "evaluator_schema", "metric_authority", "invocation_journal", "execution_statuses",
            "evidence_files",
        },
        "Stage 12 result set",
    )
    _require_equal(payload, "schema_version", RESULT_SET_SCHEMA_VERSION)
    _require_equal(payload, "result_set_policy_version", 1)
    _require_equal(payload, "result_set_type", "stage12_baseline")
    if payload["experiment_mode"] not in {"sandbox", "docker"}:
        raise CanonicalExperimentEvidenceError("unsupported canonical experiment mode")
    _common_producer_bindings(payload)
    journal = _file_ref(payload["invocation_journal"], "invocation_journal")
    if journal["path"] != "stage-12/execution_invocation_journal.jsonl":
        raise CanonicalExperimentEvidenceError("noncanonical invocation journal path")
    statuses = payload["execution_statuses"]
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise CanonicalExperimentEvidenceError("Stage 12 requires one execution status")
    status = statuses[0]
    _exact_keys(status, {"ordinal", "status", "result_path", "failure_code"}, "execution status")
    if (
        _strict_int(status["ordinal"], "execution ordinal") != 1
        or status["status"] != "completed"
        or status["result_path"] != "stage-12/evidence-v1/run-1.json"
        or status["failure_code"] is not None
    ):
        raise CanonicalExperimentEvidenceError("invalid Stage 12 execution status")
    evidence = _file_ref_list(payload["evidence_files"], "evidence_files")
    if [item["path"] for item in evidence] != [
        "stage-12/evidence-v1/results.json",
        "stage-12/evidence-v1/run-1.json",
    ]:
        raise CanonicalExperimentEvidenceError("invalid Stage 12 evidence file closure")
    return payload


def validate_experiment_result_set(
    run_dir: Path,
    config: RCConfig,
    text: str | None = None,
) -> dict[str, Any]:
    """Replay the complete Stage 12 journal, evidence namespace, and bindings."""
    manifest_path = run_dir / "stage-12" / "experiment_result_set.json"
    if text is None:
        text = _read_regular_file(manifest_path, "Stage 12 result set")
    payload = parse_experiment_result_set(text)
    seal, contract = _validate_common_run_bindings(run_dir, config, payload)

    journal_path = run_dir / payload["invocation_journal"]["path"]
    journal_text = _read_regular_file(journal_path, "execution invocation journal")
    if sha256_text(journal_text) != payload["invocation_journal"]["sha256"]:
        raise CanonicalExperimentEvidenceError("invocation journal hash mismatch")
    journal = parse_execution_invocation_journal(journal_text)
    if len(journal) != 2 or journal[1]["status"] != "completed":
        raise CanonicalExperimentEvidenceError("published result set requires completed journal")
    started, terminal = journal
    expected_binding = invocation_generation_binding_sha256(
        experiment_contract_sha256=payload["experiment_contract_sha256"],
        sealed_candidate_manifest_sha256=payload["sealed_candidate_manifest_sha256"],
        config_semantic_sha256=payload["config_semantic_sha256"],
        experiment_mode=payload["experiment_mode"],
        evaluator_schema=payload["evaluator_schema"],
    )
    if (
        started["generation_binding_sha256"] != expected_binding
        or started["experiment_contract_sha256"] != payload["experiment_contract_sha256"]
        or started["sealed_candidate_manifest_sha256"] != payload["sealed_candidate_manifest_sha256"]
        or started["config_semantic_sha256"] != payload["config_semantic_sha256"]
    ):
        raise CanonicalExperimentEvidenceError("invocation journal generation binding mismatch")

    evidence_dir = run_dir / "stage-12" / "evidence-v1"
    expected_paths = {item["path"] for item in payload["evidence_files"]}
    _validate_exact_namespace(run_dir, evidence_dir, expected_paths, "Stage 12 evidence")
    evidence_by_path = {item["path"]: item for item in payload["evidence_files"]}
    for relative, ref in evidence_by_path.items():
        path = run_dir / relative
        if sha256_file(path) != ref["sha256"]:
            raise CanonicalExperimentEvidenceError(f"Stage 12 evidence hash mismatch: {relative}")
    run_ref = evidence_by_path["stage-12/evidence-v1/run-1.json"]
    if terminal["result_path"] != run_ref["path"] or terminal["result_sha256"] != run_ref["sha256"]:
        raise CanonicalExperimentEvidenceError("journal terminal result binding mismatch")
    invocation, aggregate = validate_single_invocation_aggregate(
        _read_regular_file(run_dir / run_ref["path"], "Stage 12 invocation result"),
        _read_regular_file(
            run_dir / "stage-12/evidence-v1/results.json", "Stage 12 aggregate results"
        ),
        contract=contract,
    )
    if invocation["evaluator_schema"] != payload["evaluator_schema"]:
        raise CanonicalExperimentEvidenceError("result-set evaluator schema mismatch")
    if seal["config_semantic_sha256"] != payload["config_semantic_sha256"]:
        raise CanonicalExperimentEvidenceError("Stage 10/12 config binding mismatch")
    del aggregate, contract
    return payload


def parse_refinement_result_set(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 13 refinement result set")
    _exact_keys(
        payload,
        {
            "schema_version", "refinement_policy_version", "result_set_type",
            "baseline_manifest", "experiment_contract_path", "experiment_contract_sha256",
            "sealed_candidate_manifest_path", "sealed_candidate_manifest_sha256",
            "run_config_path", "run_config_sha256", "config_semantic_policy_version",
            "config_semantic_sha256", "claim_scope", "dataset_origin", "evaluator_schema",
            "metric_authority",
            "primary_metric_key", "optimization_direction", "iterations",
            "refinement_log", "selected_result",
        },
        "Stage 13 refinement result set",
    )
    _require_equal(payload, "schema_version", REFINEMENT_SCHEMA_VERSION)
    _require_equal(payload, "refinement_policy_version", 1)
    _require_equal(payload, "result_set_type", "stage13_refinement")
    _common_producer_bindings(payload)
    if _file_ref(payload["baseline_manifest"], "baseline_manifest")["path"] != "stage-12/experiment_result_set.json":
        raise CanonicalExperimentEvidenceError("noncanonical Stage 12 baseline path")
    _required_string(payload["primary_metric_key"], "primary_metric_key")
    if payload["optimization_direction"] not in {"maximize", "minimize"}:
        raise CanonicalExperimentEvidenceError("invalid optimization_direction")
    iterations = payload["iterations"]
    if not isinstance(iterations, list):
        raise CanonicalExperimentEvidenceError("iterations must be a list")
    iteration_ids: set[str] = set()
    for index, item in enumerate(iterations, start=1):
        _parse_refinement_iteration(item, index, iteration_ids)
    if _file_ref(payload["refinement_log"], "refinement_log")["path"] != "stage-13/refinement_log.json":
        raise CanonicalExperimentEvidenceError("noncanonical refinement log path")
    selected = payload["selected_result"]
    _exact_keys(selected, {"type", "iteration_id"}, "selected_result")
    if selected["type"] == "baseline":
        if selected["iteration_id"] is not None:
            raise CanonicalExperimentEvidenceError("baseline selection has iteration_id")
    elif selected["type"] == "iteration":
        if selected["iteration_id"] not in iteration_ids:
            raise CanonicalExperimentEvidenceError("selected iteration is missing")
    else:
        raise CanonicalExperimentEvidenceError("invalid selected_result type")
    return payload


def parse_refinement_validation_report(text: str) -> dict[str, Any]:
    """Parse the deterministic Stage 13 project-validation replay record."""
    payload = _parse_object(text, "Stage 13 validation report")
    _exact_keys(
        payload,
        {
            "schema_version", "validation_policy_version",
            "project_files_sha256", "checks",
        },
        "Stage 13 validation report",
    )
    _require_equal(payload, "schema_version", REFINEMENT_VALIDATION_SCHEMA_VERSION)
    _require_equal(payload, "validation_policy_version", 1)
    _sha256(payload["project_files_sha256"], "project_files_sha256")
    checks = payload["checks"]
    if not isinstance(checks, dict):
        raise CanonicalExperimentEvidenceError("validation checks must be an object")
    _exact_keys(checks, {"python_syntax_valid"}, "Stage 13 validation checks")
    if not isinstance(checks["python_syntax_valid"], bool):
        raise CanonicalExperimentEvidenceError("python_syntax_valid must be bool")
    return payload


def validate_refinement_result_set(
    run_dir: Path,
    config: RCConfig,
    text: str | None = None,
) -> dict[str, Any]:
    """Replay Stage 13 baseline, ordered iterations, file closure, and selection."""
    manifest_path = run_dir / "stage-13" / "refinement_result_set.json"
    if text is None:
        text = _read_regular_file(manifest_path, "Stage 13 refinement result set")
    payload = parse_refinement_result_set(text)
    baseline_text = _read_regular_file(
        run_dir / "stage-12/experiment_result_set.json", "Stage 12 result set"
    )
    if sha256_text(baseline_text) != payload["baseline_manifest"]["sha256"]:
        raise CanonicalExperimentEvidenceError("Stage 13 baseline manifest hash mismatch")
    baseline = validate_experiment_result_set(run_dir, config, baseline_text)
    seal, contract = _validate_common_run_bindings(run_dir, config, payload)
    _require_common_binding_equality(payload, baseline, "Stage 12/13")
    contract_metric_key = _required_string(
        contract.primary_metric.get("key"), "contract primary metric key"
    )
    contract_direction = contract.primary_metric.get("direction")
    if (
        payload["primary_metric_key"] != contract_metric_key
        or config.experiment.metric_key != contract_metric_key
    ):
        raise CanonicalExperimentEvidenceError(
            "Stage 13 primary metric differs from contract/config"
        )
    if (
        contract_direction not in {"maximize", "minimize"}
        or payload["optimization_direction"] != contract_direction
        or config.experiment.metric_direction != contract_direction
    ):
        raise CanonicalExperimentEvidenceError(
            "Stage 13 optimization direction differs from contract/config"
        )

    expected_evidence: set[str] = set()
    accepted_values: dict[str, Decimal] = {}
    for item in payload["iterations"]:
        prefix = f"stage-13/evidence-v1/iterations/{item['iteration_id']}/"
        refs = list(item["project_files"]) + [item["validation_report"], item["initial_execution"]]
        relative_paths = [ref["path"] for ref in refs]
        if len(relative_paths) != len(set(relative_paths)):
            raise CanonicalExperimentEvidenceError("Stage 13 cross-role path alias")
        for ref in refs:
            relative = ref["path"]
            if not relative.startswith(prefix):
                raise CanonicalExperimentEvidenceError("Stage 13 file escapes iteration namespace")
            path = run_dir / relative
            if sha256_file(_require_regular_path(path, "Stage 13 evidence")) != ref["sha256"]:
                raise CanonicalExperimentEvidenceError(f"Stage 13 evidence hash mismatch: {relative}")
            expected_evidence.add(relative)
        initial_outcome = _replay_refinement_attempt(
            run_dir,
            project_files=item["project_files"],
            validation_ref=item["validation_report"],
            execution_ref=item["initial_execution"],
            evaluator_schema=payload["evaluator_schema"],
            primary_metric_key=payload["primary_metric_key"],
            seal=seal,
            contract=contract,
        )
        accepted, rejection_codes, observed = initial_outcome
        if not accepted or rejection_codes or observed is None:
            raise CanonicalExperimentEvidenceError(
                "Stage 13 policy v1 iteration did not replay as accepted"
            )
        stored = _finite_json_number(
            item["primary_metric_observation"], "primary_metric_observation"
        )
        if observed != stored:
            raise CanonicalExperimentEvidenceError("accepted iteration metric mismatch")
        accepted_values[item["iteration_id"]] = observed
    _validate_exact_namespace(
        run_dir,
        run_dir / "stage-13/evidence-v1",
        expected_evidence,
        "Stage 13 evidence",
    )
    refinement_log = run_dir / payload["refinement_log"]["path"]
    if sha256_file(_require_regular_path(refinement_log, "refinement log")) != payload["refinement_log"]["sha256"]:
        raise CanonicalExperimentEvidenceError("refinement log hash mismatch")

    baseline_value = _stage12_primary_metric(run_dir, baseline, payload["primary_metric_key"])
    best_type = "baseline"
    best_id: str | None = None
    best_value = baseline_value
    for iteration_id, value in accepted_values.items():
        improved = value > best_value if payload["optimization_direction"] == "maximize" else value < best_value
        if improved:
            best_type, best_id, best_value = "iteration", iteration_id, value
    if payload["selected_result"] != {"type": best_type, "iteration_id": best_id}:
        raise CanonicalExperimentEvidenceError("Stage 13 selected result replay mismatch")
    _validate_refinement_compatibility_copies(run_dir, payload, seal)
    return payload


def _validate_refinement_compatibility_copies(
    run_dir: Path,
    refinement: Mapping[str, Any],
    seal: Mapping[str, Any],
) -> None:
    selected = refinement["selected_result"]
    selected_sources: dict[str, Path] = {}
    if selected["type"] == "baseline":
        for name in sorted(seal["files"]):
            selected_sources[name] = run_dir / "stage-10/selected_candidate" / name
    else:
        item = next(
            candidate
            for candidate in refinement["iterations"]
            if candidate["iteration_id"] == selected["iteration_id"]
        )
        for ref in item["project_files"]:
            name = ref["path"].rsplit("/project/", 1)[1]
            selected_sources[name] = run_dir / ref["path"]

    final_root = run_dir / "stage-13/experiment_final"
    expected = {
        f"stage-13/experiment_final/{name}" for name in selected_sources
    }
    _validate_exact_namespace(
        run_dir,
        final_root,
        expected,
        "Stage 13 compatibility project",
    )
    for name, source_path in selected_sources.items():
        source = _require_regular_path(source_path, "Stage 13 selected project file")
        final = _require_regular_path(
            final_root / name, "Stage 13 compatibility project file"
        )
        if source.read_bytes() != final.read_bytes():
            raise CanonicalExperimentEvidenceError(
                f"Stage 13 compatibility copy mismatch: {name}"
            )
    selected_main = _require_regular_path(
        selected_sources["main.py"], "Stage 13 selected main.py"
    )
    final_main = _require_regular_path(
        run_dir / "stage-13/experiment_final.py",
        "Stage 13 compatibility main.py",
    )
    if selected_main.read_bytes() != final_main.read_bytes():
        raise CanonicalExperimentEvidenceError(
            "Stage 13 compatibility copy mismatch: experiment_final.py"
        )


def parse_experiment_evidence_candidate(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 14 evidence candidate")
    _exact_keys(
        payload,
        {
            "schema_version", "candidate_policy_version", "candidate_id",
            "identity_payload", "identity_payload_sha256", "selected_result",
            "experiment_contract_path", "experiment_contract_sha256",
            "sealed_candidate_manifest_path", "sealed_candidate_manifest_sha256",
            "run_config_path", "run_config_sha256", "config_semantic_policy_version",
            "config_semantic_sha256", "claim_scope", "dataset_origin", "evaluator_schema",
            "metric_authority",
            "primary_metric_key", "optimization_direction", "primary_metric_value",
            "artifacts",
        },
        "Stage 14 evidence candidate",
    )
    _require_equal(payload, "schema_version", CANDIDATE_SCHEMA_VERSION)
    _require_equal(payload, "candidate_policy_version", 1)
    candidate_match = _CANDIDATE_ID_RE.fullmatch(_required_string(payload["candidate_id"], "candidate_id"))
    if candidate_match is None:
        raise CanonicalExperimentEvidenceError("invalid candidate_id")
    _common_producer_bindings(payload)
    identity = _parse_candidate_identity(payload["identity_payload"])
    identity_sha = sha256_text(canonical_json_text(identity))
    if payload["identity_payload_sha256"] != identity_sha or candidate_match.group(1) != identity_sha:
        raise CanonicalExperimentEvidenceError("candidate identity mismatch")
    selected = _selected_result_ref(payload["selected_result"])
    if (
        identity["selected_result_type"] != selected["result_set_type"]
        or identity["selected_result_manifest_sha256"] != selected["manifest_sha256"]
        or identity["experiment_contract_sha256"] != payload["experiment_contract_sha256"]
        or identity["config_semantic_sha256"] != payload["config_semantic_sha256"]
        or identity["metric_authority_selector_input_sha256"]
        != payload["metric_authority"]["selector_input_sha256"]
        or identity["primary_metric_key"] != payload["primary_metric_key"]
        or identity["optimization_direction"] != payload["optimization_direction"]
    ):
        raise CanonicalExperimentEvidenceError("candidate identity binding mismatch")
    if payload["optimization_direction"] not in {"maximize", "minimize"}:
        raise CanonicalExperimentEvidenceError("invalid optimization_direction")
    if payload["primary_metric_value"] != canonical_decimal(payload["primary_metric_value"]):
        raise CanonicalExperimentEvidenceError("primary_metric_value is not canonical")
    outer = _artifact_list(payload["artifacts"], "artifacts", path_field="path")
    inner = identity["artifacts"]
    if [(x["role"], x["path"], x["sha256"]) for x in outer] != [
        (x["role"], x["logical_name"], x["sha256"]) for x in inner
    ]:
        raise CanonicalExperimentEvidenceError("candidate artifact identity mismatch")
    return payload


def validate_experiment_evidence_candidate(
    candidate_root: Path,
    text: str | None = None,
) -> dict[str, Any]:
    """Replay a Stage 14 candidate's deterministic identity and file closure."""
    manifest_path = candidate_root / "experiment_evidence_candidate.json"
    if text is None:
        text = _read_regular_file(manifest_path, "experiment evidence candidate")
    payload = parse_experiment_evidence_candidate(text)
    if candidate_root.parent.name == "evidence_candidates" and candidate_root.name != payload["candidate_id"]:
        raise CanonicalExperimentEvidenceError("candidate directory identity mismatch")
    expected = {"experiment_evidence_candidate.json"}
    for artifact in payload["artifacts"]:
        expected.add(artifact["path"])
    allowed_dirs = {
        parent.as_posix()
        for relative in expected
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    actual: set[str] = set()
    for path in candidate_root.rglob("*"):
        if path.is_symlink():
            raise CanonicalExperimentEvidenceError("candidate contains a symlink")
        relative = path.relative_to(candidate_root).as_posix()
        if path.is_dir():
            if relative not in allowed_dirs:
                raise CanonicalExperimentEvidenceError("candidate contains an unmanifested directory")
            continue
        if not path.is_file():
            raise CanonicalExperimentEvidenceError("candidate contains a non-regular entry")
        actual.add(relative)
    if actual != expected:
        raise CanonicalExperimentEvidenceError("candidate file-set mismatch")
    forbidden = (
        payload["candidate_id"].encode("ascii"),
        payload["identity_payload_sha256"].encode("ascii"),
        sha256_text(text).encode("ascii"),
        b"canonical_experiment_evidence.json",
        b"/evidence_candidates/",
    )
    for artifact in payload["artifacts"]:
        path = candidate_root / artifact["path"]
        if sha256_file(path) != artifact["sha256"]:
            raise CanonicalExperimentEvidenceError(f"candidate artifact hash mismatch: {artifact['path']}")
        raw = path.read_bytes()
        if any(token in raw for token in forbidden):
            raise CanonicalExperimentEvidenceError("candidate artifact contains forbidden identity data")
        if path.suffix.lower() == ".json":
            structured = _parse_json_value(raw.decode("utf-8"), artifact["path"])
            if artifact["role"] == "figure_plan" and structured != {
                "schema_version": 1,
                "generator": "canonical_stage14_v1",
                "figures": [],
            }:
                raise CanonicalExperimentEvidenceError(
                    "candidate policy v1 requires the deterministic empty figure plan"
                )
            decoded_forbidden = tuple(token.decode("ascii") for token in forbidden)
            _reject_decoded_identity_strings(structured, decoded_forbidden)
    return payload


def load_selected_result_for_analysis(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Replay the selected Stage 12/13 result and expose its bounded analysis input."""
    selected, upstream, metric_key, direction, metric_value = _derive_selected_result(
        run_dir, config
    )
    if selected["result_set_type"] == "stage12_baseline":
        execution = parse_aggregate_results(
            _read_regular_file(
                run_dir / "stage-12/evidence-v1/results.json",
                "Stage 12 aggregate results",
            )
        )
    else:
        chosen = upstream["selected_result"]
        if chosen["type"] == "baseline":
            execution = parse_aggregate_results(
                _read_regular_file(
                    run_dir / "stage-12/evidence-v1/results.json",
                    "Stage 12 aggregate results",
                )
            )
        else:
            iteration = next(
                item
                for item in upstream["iterations"]
                if item["iteration_id"] == chosen["iteration_id"]
            )
            execution = parse_invocation_result(
                _read_regular_file(
                    run_dir / iteration["initial_execution"]["path"],
                    "selected Stage 13 execution result",
                )
            )
    if execution["evaluator_schema"] != upstream["evaluator_schema"]:
        raise CanonicalExperimentEvidenceError("selected analysis evaluator mismatch")
    observations = execution["metric_observations"].get(metric_key)
    if not isinstance(observations, list) or not observations:
        raise CanonicalExperimentEvidenceError("selected analysis metric is missing")
    if _decimal_mean(observations) != metric_value:
        raise CanonicalExperimentEvidenceError("selected analysis metric replay mismatch")
    return {
        "selected_result": selected,
        "upstream": upstream,
        "primary_metric_key": metric_key,
        "optimization_direction": direction,
        "primary_metric_value": metric_value,
        "metric_observations": execution["metric_observations"],
        "structured_results": execution["structured_results"],
    }


def publish_experiment_evidence_candidate(
    run_dir: Path,
    stage_dir: Path,
    staging_root: Path,
    config: RCConfig,
) -> tuple[Path, str, dict[str, Any]]:
    """Seal, replay, and atomically publish one complete Stage 14 candidate."""
    with ReleaseGraphLock.acquire(
        run_dir, "publish_experiment_evidence_candidate", mode="write"
    ) as release_lock:
        with release_lock.open_stage_namespace("stage-14") as namespace:
            return _publish_experiment_evidence_candidate_under_lock(
                run_dir, stage_dir, staging_root, config, namespace
            )


def _publish_experiment_evidence_candidate_under_lock(
    run_dir: Path,
    stage_dir: Path,
    staging_root: Path,
    config: RCConfig,
    namespace: BoundOutputNamespace,
) -> tuple[Path, str, dict[str, Any]]:
    if stage_dir != run_dir / "stage-14":
        raise CanonicalExperimentEvidenceError("canonical Stage 14 directory mismatch")
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise CanonicalExperimentEvidenceError("Stage 14 staging directory is unsafe")

    selected_data = load_selected_result_for_analysis(run_dir, config)
    selected = selected_data["selected_result"]
    upstream = selected_data["upstream"]
    metric_key = selected_data["primary_metric_key"]
    direction = selected_data["optimization_direction"]
    metric_value = selected_data["primary_metric_value"]

    artifacts: list[dict[str, str]] = []
    mandatory_by_path = {
        logical_name: role for role, logical_name in _MANDATORY_CANDIDATE_ROLES.items()
    }
    for path in sorted(staging_root.rglob("*"), key=lambda item: item.relative_to(staging_root).as_posix()):
        if path.is_symlink():
            raise CanonicalExperimentEvidenceError("Stage 14 staging contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CanonicalExperimentEvidenceError("Stage 14 staging contains a non-regular entry")
        relative = path.relative_to(staging_root).as_posix()
        if relative == "experiment_evidence_candidate.json":
            raise CanonicalExperimentEvidenceError("Stage 14 staging contains a premature manifest")
        if relative in mandatory_by_path:
            role = mandatory_by_path[relative]
        elif relative.startswith("charts/"):
            role = "chart"
        else:
            role = "auxiliary"
        artifacts.append({"role": role, "path": relative, "sha256": sha256_file(path)})
    artifacts.sort(key=lambda item: (item["role"], item["path"]))
    _artifact_list(artifacts, "candidate artifacts", path_field="path")

    identity_artifacts = [
        {"role": item["role"], "logical_name": item["path"], "sha256": item["sha256"]}
        for item in artifacts
    ]
    identity = {
        "candidate_identity_policy_version": 1,
        "selected_result_type": selected["result_set_type"],
        "selected_result_manifest_sha256": selected["manifest_sha256"],
        "experiment_contract_sha256": upstream["experiment_contract_sha256"],
        "config_semantic_sha256": upstream["config_semantic_sha256"],
        "metric_authority_selector_input_sha256": upstream["metric_authority"]["selector_input_sha256"],
        "primary_metric_key": metric_key,
        "optimization_direction": direction,
        "artifacts": identity_artifacts,
    }
    identity_sha = sha256_text(canonical_json_text(identity))
    candidate_id = "cand-" + identity_sha
    payload = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_policy_version": 1,
        "candidate_id": candidate_id,
        "identity_payload": identity,
        "identity_payload_sha256": identity_sha,
        "selected_result": selected,
        "experiment_contract_path": upstream["experiment_contract_path"],
        "experiment_contract_sha256": upstream["experiment_contract_sha256"],
        "sealed_candidate_manifest_path": upstream["sealed_candidate_manifest_path"],
        "sealed_candidate_manifest_sha256": upstream["sealed_candidate_manifest_sha256"],
        "run_config_path": upstream["run_config_path"],
        "run_config_sha256": upstream["run_config_sha256"],
        "config_semantic_policy_version": upstream["config_semantic_policy_version"],
        "config_semantic_sha256": upstream["config_semantic_sha256"],
        "claim_scope": upstream["claim_scope"],
        "dataset_origin": upstream["dataset_origin"],
        "evaluator_schema": upstream["evaluator_schema"],
        "metric_authority": upstream["metric_authority"],
        "primary_metric_key": metric_key,
        "optimization_direction": direction,
        "primary_metric_value": canonical_decimal(metric_value),
        "artifacts": artifacts,
    }
    candidate_text = canonical_json_text(payload)
    (staging_root / "experiment_evidence_candidate.json").write_text(
        candidate_text, encoding="utf-8"
    )
    validate_experiment_evidence_candidate(staging_root)
    if _candidate_summary_metric(staging_root, payload, metric_key) != metric_value:
        raise CanonicalExperimentEvidenceError("candidate summary primary metric mismatch")

    candidates_root = stage_dir / "evidence_candidates"
    if candidates_root.is_symlink() or not candidates_root.is_dir():
        raise CanonicalExperimentEvidenceError("candidate collection is unsafe")
    destination = candidates_root / candidate_id
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise CanonicalExperimentEvidenceError("candidate destination is unsafe")
        existing_text = _read_regular_file(
            destination / "experiment_evidence_candidate.json",
            "existing experiment evidence candidate",
        )
        validate_experiment_evidence_candidate(destination)
        if (
            existing_text != candidate_text
            or _candidate_directory_digest(destination) != _candidate_directory_digest(staging_root)
        ):
            raise CanonicalExperimentEvidenceError("candidate ID collision during publication")
        return destination, existing_text, payload
    try:
        namespace.publish_tree_child("evidence_candidates", candidate_id, staging_root)
        namespace.assert_canonical()
        published = validate_experiment_evidence_candidate(destination)
    except Exception:
        try:
            namespace.remove_tree_child("evidence_candidates", candidate_id)
        except OSError as cleanup_error:
            raise CanonicalExperimentEvidenceError(
                "failed to remove invalid published Stage 14 candidate"
            ) from cleanup_error
        raise
    return destination, candidate_text, published


def _derive_selected_result(
    run_dir: Path,
    config: RCConfig,
) -> tuple[dict[str, str], dict[str, Any], str, str, Decimal]:
    baseline_path = run_dir / "stage-12/experiment_result_set.json"
    baseline_text = _read_regular_file(baseline_path, "Stage 12 result set")
    baseline = validate_experiment_result_set(run_dir, config, baseline_text)
    contract_path = find_stage09_contract(run_dir)
    if contract_path is None:
        raise CanonicalExperimentEvidenceError("canonical experiment contract is missing")
    try:
        contract = load_contract(contract_path)
    except ContractValidationError as exc:
        raise CanonicalExperimentEvidenceError(f"canonical contract is invalid: {exc}") from exc
    metric_key = _required_string(contract.primary_metric.get("key"), "contract primary metric")
    direction = contract.primary_metric.get("direction")
    if direction not in {"maximize", "minimize"}:
        raise CanonicalExperimentEvidenceError("invalid contract optimization direction")

    refinement_path = run_dir / "stage-13/refinement_result_set.json"
    if refinement_path.is_symlink():
        raise CanonicalExperimentEvidenceError("Stage 13 refinement manifest is unsafe")
    if refinement_path.exists():
        refinement_text = _read_regular_file(refinement_path, "Stage 13 refinement result set")
        refinement = validate_refinement_result_set(run_dir, config, refinement_text)
        if (
            refinement["primary_metric_key"] != metric_key
            or refinement["optimization_direction"] != direction
        ):
            raise CanonicalExperimentEvidenceError("Stage 13 metric policy differs from contract")
        selected = {
            "result_set_type": "stage13_refinement",
            "manifest_path": "stage-13/refinement_result_set.json",
            "manifest_sha256": sha256_text(refinement_text),
        }
        return selected, refinement, metric_key, direction, _selected_refinement_metric(
            run_dir, refinement
        )

    selected = {
        "result_set_type": "stage12_baseline",
        "manifest_path": "stage-12/experiment_result_set.json",
        "manifest_sha256": sha256_text(baseline_text),
    }
    return selected, baseline, metric_key, direction, _stage12_primary_metric(
        run_dir, baseline, metric_key
    )


def _candidate_directory_digest(candidate_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate_root.rglob("*"), key=lambda item: item.relative_to(candidate_root).as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(candidate_root).as_posix().encode("utf-8")
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _candidate_summary_metric(
    candidate_root: Path,
    candidate: Mapping[str, Any],
    metric_key: str,
) -> Decimal:
    artifact_map = {item["role"]: item for item in candidate["artifacts"]}
    summary_payload = _parse_json_value(
        _read_regular_file(
            candidate_root / artifact_map["summary"]["path"],
            "candidate experiment summary",
        ),
        "candidate experiment summary",
    )
    if not isinstance(summary_payload, dict):
        raise CanonicalExperimentEvidenceError("candidate summary root must be an object")
    metrics_summary = summary_payload.get("metrics_summary")
    metric_summary = metrics_summary.get(metric_key) if isinstance(metrics_summary, dict) else None
    mirrored_mean = metric_summary.get("mean") if isinstance(metric_summary, dict) else None
    return Decimal(canonical_decimal(mirrored_mean))


def _select_canonical_candidate(
    run_dir: Path,
    *,
    selected_result: Mapping[str, Any],
    upstream: Mapping[str, Any],
    primary_metric_key: str,
    optimization_direction: str,
    metric_value: Decimal,
) -> tuple[Path, str, dict[str, Any]]:
    seen_by_id: dict[str, tuple[str, str]] = {}
    eligible_by_id: dict[str, tuple[Path, str, dict[str, Any], Decimal]] = {}
    stage_dirs: list[Path] = []
    for entry in run_dir.iterdir():
        if _STAGE14_DIR_RE.fullmatch(entry.name) is None:
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise CanonicalExperimentEvidenceError("canonical Stage 14 directory is unsafe")
        stage_dirs.append(entry)

    for stage_dir in sorted(stage_dirs, key=lambda path: path.name):
        candidates_dir = stage_dir / "evidence_candidates"
        if candidates_dir.is_symlink():
            raise CanonicalExperimentEvidenceError("candidate collection is unsafe")
        if not candidates_dir.exists():
            continue
        if not candidates_dir.is_dir():
            raise CanonicalExperimentEvidenceError("candidate collection is not a directory")
        for candidate_root in sorted(candidates_dir.iterdir(), key=lambda path: path.name):
            if (
                candidate_root.is_symlink()
                or not candidate_root.is_dir()
                or _CANDIDATE_ID_RE.fullmatch(candidate_root.name) is None
            ):
                raise CanonicalExperimentEvidenceError("candidate collection contains a noncanonical entry")
            manifest_path = candidate_root / "experiment_evidence_candidate.json"
            candidate_text = _read_regular_file(manifest_path, "experiment evidence candidate")
            candidate = validate_experiment_evidence_candidate(candidate_root, candidate_text)
            directory_digest = _candidate_directory_digest(candidate_root)
            existing = seen_by_id.get(candidate["candidate_id"])
            if existing is not None:
                if existing != (candidate_text, directory_digest):
                    raise CanonicalExperimentEvidenceError("candidate ID collision across Stage 14 attempts")
                continue
            seen_by_id[candidate["candidate_id"]] = (candidate_text, directory_digest)
            if candidate["selected_result"] != selected_result:
                continue
            if not _common_bindings_equal(candidate, upstream):
                continue
            if (
                candidate["primary_metric_key"] != primary_metric_key
                or candidate["optimization_direction"] != optimization_direction
            ):
                raise CanonicalExperimentEvidenceError("candidate metric policy mismatch")
            score = Decimal(canonical_decimal(candidate["primary_metric_value"]))
            if score != metric_value:
                raise CanonicalExperimentEvidenceError("candidate primary metric replay mismatch")
            if _candidate_summary_metric(candidate_root, candidate, primary_metric_key) != score:
                raise CanonicalExperimentEvidenceError("candidate summary primary metric mismatch")
            eligible_by_id[candidate["candidate_id"]] = (
                manifest_path, candidate_text, candidate, score
            )

    if not eligible_by_id:
        raise CanonicalExperimentEvidenceError("no eligible canonical experiment candidate")
    scores = [record[3] for record in eligible_by_id.values()]
    best_score = max(scores) if optimization_direction == "maximize" else min(scores)
    winner_id = min(
        candidate_id
        for candidate_id, record in eligible_by_id.items()
        if record[3] == best_score
    )
    winner_path, winner_text, winner, _score = eligible_by_id[winner_id]
    return winner_path, winner_text, winner


def parse_canonical_experiment_manifest(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "canonical experiment evidence manifest")
    _exact_keys(
        payload,
        {
            "schema_version", "selection_policy_version", "selected_result",
            "experiment_contract_path", "experiment_contract_sha256",
            "sealed_candidate_manifest_path", "sealed_candidate_manifest_sha256",
            "run_config_path", "run_config_sha256", "config_semantic_policy_version",
            "config_semantic_sha256", "claim_scope", "dataset_origin", "evaluator_schema",
            "metric_authority",
            "primary_metric", "optimization_direction", "selected_candidate",
            "selected_summary", "selected_analysis",
        },
        "canonical experiment evidence manifest",
    )
    _require_equal(payload, "schema_version", CANONICAL_MANIFEST_SCHEMA_VERSION)
    _require_equal(payload, "selection_policy_version", 1)
    _common_producer_bindings(payload)
    _selected_result_ref(payload["selected_result"])
    _required_string(payload["primary_metric"], "primary_metric")
    if payload["optimization_direction"] not in {"maximize", "minimize"}:
        raise CanonicalExperimentEvidenceError("invalid optimization_direction")
    candidate = payload["selected_candidate"]
    _exact_keys(candidate, {"candidate_id", "path", "sha256"}, "selected_candidate")
    candidate_id = _required_string(candidate["candidate_id"], "candidate_id")
    if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise CanonicalExperimentEvidenceError("invalid selected candidate_id")
    _safe_relative_path(candidate["path"], "selected_candidate.path")
    _sha256(candidate["sha256"], "selected_candidate.sha256")
    for field, filename, canonical_name in (
        ("selected_summary", "experiment_summary.json", "experiment_summary_best.json"),
        ("selected_analysis", "analysis.md", "analysis_best.md"),
    ):
        ref = payload[field]
        _exact_keys(ref, {"source_path", "source_sha256", "canonical_path", "canonical_sha256"}, field)
        _safe_relative_path(ref["source_path"], f"{field}.source_path")
        if ref["canonical_path"] != canonical_name:
            raise CanonicalExperimentEvidenceError(f"noncanonical {field} output path")
        if not ref["source_path"].endswith("/" + filename):
            raise CanonicalExperimentEvidenceError(f"invalid {field} source")
        _sha256(ref["source_sha256"], f"{field}.source_sha256")
        _sha256(ref["canonical_sha256"], f"{field}.canonical_sha256")
        if ref["source_sha256"] != ref["canonical_sha256"]:
            raise CanonicalExperimentEvidenceError(f"{field} copy hash mismatch")
    return payload


def validate_canonical_experiment_manifest(
    run_dir: Path,
    config: RCConfig,
    text: str | None = None,
) -> dict[str, Any]:
    """Replay the selected result, immutable candidate, and root compatibility copies."""
    manifest_path = run_dir / "canonical_experiment_evidence.json"
    if text is None:
        text = _read_regular_file(manifest_path, "canonical experiment evidence manifest")
    payload = parse_canonical_experiment_manifest(text)
    selected, upstream, metric_key, direction, metric_value = _derive_selected_result(
        run_dir, config
    )
    if payload["selected_result"] != selected:
        raise CanonicalExperimentEvidenceError("root selected result replay mismatch")
    if payload["primary_metric"] != metric_key or payload["optimization_direction"] != direction:
        raise CanonicalExperimentEvidenceError("root metric policy replay mismatch")
    _validate_common_run_bindings(run_dir, config, payload)
    _require_common_binding_equality(payload, upstream, "selected result/root")

    winner_path, candidate_text, candidate = _select_canonical_candidate(
        run_dir,
        selected_result=selected,
        upstream=upstream,
        primary_metric_key=metric_key,
        optimization_direction=direction,
        metric_value=metric_value,
    )
    candidate_ref = payload["selected_candidate"]
    winner_relative = winner_path.relative_to(run_dir).as_posix()
    expected_candidate_ref = {
        "candidate_id": candidate["candidate_id"],
        "path": winner_relative,
        "sha256": sha256_text(candidate_text),
    }
    if candidate_ref != expected_candidate_ref:
        raise CanonicalExperimentEvidenceError("root selected candidate is not deterministic winner")
    candidate_path = winner_path
    _require_common_binding_equality(payload, candidate, "candidate/root")

    artifact_map = {item["role"]: item for item in candidate["artifacts"]}
    summary_payload = _parse_json_value(
        _read_regular_file(
            candidate_path.parent / artifact_map["summary"]["path"],
            "candidate experiment summary",
        ),
        "candidate experiment summary",
    )
    if not isinstance(summary_payload, dict):
        raise CanonicalExperimentEvidenceError("candidate summary root must be an object")
    metrics_summary = summary_payload.get("metrics_summary")
    metric_summary = metrics_summary.get(payload["primary_metric"]) if isinstance(metrics_summary, dict) else None
    mirrored_mean = metric_summary.get("mean") if isinstance(metric_summary, dict) else None
    if Decimal(canonical_decimal(mirrored_mean)) != metric_value:
        raise CanonicalExperimentEvidenceError("candidate summary primary metric mismatch")
    for field, role in (("selected_summary", "summary"), ("selected_analysis", "analysis")):
        ref = payload[field]
        expected_source = (candidate_path.parent / artifact_map[role]["path"]).relative_to(run_dir).as_posix()
        if ref["source_path"] != expected_source or ref["source_sha256"] != artifact_map[role]["sha256"]:
            raise CanonicalExperimentEvidenceError(f"{field} source binding mismatch")
        source = _require_regular_path(run_dir / ref["source_path"], f"{field} source")
        canonical = _require_regular_path(run_dir / ref["canonical_path"], f"{field} canonical copy")
        if source.read_bytes() != canonical.read_bytes() or sha256_file(canonical) != ref["canonical_sha256"]:
            raise CanonicalExperimentEvidenceError(f"{field} compatibility copy mismatch")
    return payload


def reconstruct_expected_stage9_14_metric_authority(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Independently replay metric authority through every Stage 9-14 layer."""
    contract_path = find_stage09_contract(run_dir)
    if contract_path is None:
        raise CanonicalExperimentEvidenceError("canonical experiment contract is missing")
    try:
        contract = load_contract(contract_path)
        selection = replay_metric_authority(
            run_dir=run_dir,
            topic=config.research.topic,
            experiment_mode=config.experiment.mode,
            stored_identity=contract.metric_authority,
            metric_units=contract.metric_units,
            metric_display_labels=contract.metric_display_labels,
        )
    except (ContractValidationError, MetricAuthorityError) as exc:
        raise CanonicalExperimentEvidenceError(
            f"expected metric authority reconstruction failed: {exc}"
        ) from exc

    seal = validate_selected_candidate_manifest(run_dir, config)
    if seal["metric_authority"] != selection.contract_identity():
        raise CanonicalExperimentEvidenceError("Stage 10 metric authority replay mismatch")
    baseline = validate_experiment_result_set(run_dir, config)
    if baseline["metric_authority"] != selection.contract_identity():
        raise CanonicalExperimentEvidenceError("Stage 12 metric authority replay mismatch")

    refinement_path = run_dir / "stage-13/refinement_result_set.json"
    if refinement_path.exists() or refinement_path.is_symlink():
        refinement = validate_refinement_result_set(run_dir, config)
        if refinement["metric_authority"] != selection.contract_identity():
            raise CanonicalExperimentEvidenceError(
                "Stage 13 metric authority replay mismatch"
            )

    root = validate_canonical_experiment_manifest(run_dir, config)
    if root["metric_authority"] != selection.contract_identity():
        raise CanonicalExperimentEvidenceError(
            "Stage 14 root metric authority replay mismatch"
        )
    return {
        "schema_version": 1,
        "metric_authority": selection.contract_identity(),
        "metric_units": selection.metric_units,
        "metric_display_labels": selection.metric_display_labels,
    }


def load_canonical_experiment_evidence(run_dir: Path) -> CanonicalExperimentEvidence:
    """Replay and snapshot the complete canonical bundle under its publication lock."""
    from researchclaw.pipeline.canonical_execution_controller import (
        CanonicalAnalysisController,
    )

    controller = CanonicalAnalysisController.acquire_reader(run_dir)
    try:
        return _load_canonical_experiment_evidence_under_lock(run_dir)
    finally:
        controller.close()


def _load_canonical_experiment_evidence_under_lock(
    run_dir: Path,
) -> CanonicalExperimentEvidence:
    """Snapshot the canonical bundle while the caller owns the reader lock."""

    def snapshot() -> CanonicalExperimentEvidence:
        manifest_relative = "canonical_experiment_evidence.json"
        manifest_text = _read_regular_file(
            run_dir / manifest_relative,
            "canonical experiment evidence manifest",
        )
        preview = parse_canonical_experiment_manifest(manifest_text)
        config_relative = preview["run_config_path"]
        config_text = _read_regular_file(
            run_dir / config_relative,
            "canonical producer config snapshot",
        )
        try:
            config_data = yaml.safe_load(config_text)
            if not isinstance(config_data, dict):
                raise ValueError("config root must be a mapping")
            config = RCConfig.from_dict(
                config_data,
                project_root=run_dir,
                check_paths=False,
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise CanonicalExperimentEvidenceError(
                f"canonical producer config snapshot is invalid: {exc}"
            ) from exc

        manifest = validate_canonical_experiment_manifest(run_dir, config, manifest_text)
        selected_data = load_selected_result_for_analysis(run_dir, config)
        project_artifacts = _snapshot_selected_project(
            run_dir,
            config,
            selected_data["upstream"],
        )
        selected_result_relative = manifest["selected_result"]["manifest_path"]
        selected_result_text = _read_regular_file(
            run_dir / selected_result_relative,
            "canonical selected result manifest",
        )
        if sha256_text(selected_result_text) != manifest["selected_result"]["manifest_sha256"]:
            raise CanonicalExperimentEvidenceError("selected result manifest changed during access")
        selected_execution_artifact = _snapshot_selected_execution_artifact(
            run_dir,
            config,
            selected_data["upstream"],
            selected_data["metric_observations"],
        )

        candidate_relative = manifest["selected_candidate"]["path"]
        candidate_manifest_path = run_dir / candidate_relative
        candidate_root = candidate_manifest_path.parent
        candidate_text = _read_regular_file(
            candidate_manifest_path,
            "selected experiment evidence candidate",
        )
        candidate = validate_experiment_evidence_candidate(candidate_root, candidate_text)
        if sha256_text(candidate_text) != manifest["selected_candidate"]["sha256"]:
            raise CanonicalExperimentEvidenceError("selected candidate changed during access")

        artifact_snapshots: list[CanonicalEvidenceArtifact] = []
        artifact_content: dict[str, bytes] = {}
        for artifact in candidate["artifacts"]:
            content = _read_regular_bytes(
                candidate_root / artifact["path"],
                f"selected candidate artifact {artifact['path']}",
            )
            if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                raise CanonicalExperimentEvidenceError(
                    f"selected candidate artifact changed during access: {artifact['path']}"
                )
            artifact_content[artifact["role"]] = content
            artifact_snapshots.append(
                CanonicalEvidenceArtifact(
                    role=artifact["role"],
                    path=artifact["path"],
                    sha256=artifact["sha256"],
                    content=content,
                )
            )

        summary_bytes = artifact_content["summary"]
        try:
            summary_value = _parse_json_value(
                summary_bytes.decode("utf-8"),
                "canonical selected experiment summary",
            )
        except UnicodeDecodeError as exc:
            raise CanonicalExperimentEvidenceError(
                "canonical selected experiment summary is not UTF-8"
            ) from exc
        if not isinstance(summary_value, dict):
            raise CanonicalExperimentEvidenceError(
                "canonical selected experiment summary root must be an object"
            )
        analysis_bytes = artifact_content["analysis"]
        try:
            analysis_text = analysis_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalExperimentEvidenceError(
                "canonical selected analysis is not UTF-8"
            ) from exc

        contract_relative = manifest["experiment_contract_path"]
        contract_bytes = _read_regular_bytes(
            run_dir / contract_relative,
            "canonical experiment contract",
        )
        if hashlib.sha256(contract_bytes).hexdigest() != manifest["experiment_contract_sha256"]:
            raise CanonicalExperimentEvidenceError("experiment contract changed during access")
        config_bytes = config_text.encode("utf-8")
        if hashlib.sha256(config_bytes).hexdigest() != manifest["run_config_sha256"]:
            raise CanonicalExperimentEvidenceError("producer config changed during access")

        final_manifest = validate_canonical_experiment_manifest(run_dir, config)
        if final_manifest != manifest:
            raise CanonicalExperimentEvidenceError("canonical bundle changed during access")
        if _read_regular_file(
            run_dir / manifest_relative,
            "canonical experiment evidence manifest",
        ) != manifest_text:
            raise CanonicalExperimentEvidenceError("canonical manifest changed during access")

        return CanonicalExperimentEvidence(
            manifest_path=manifest_relative,
            manifest_sha256=sha256_text(manifest_text),
            manifest=_freeze_authority_value(manifest),
            candidate_manifest_path=candidate_relative,
            candidate_manifest_sha256=sha256_text(candidate_text),
            candidate=_freeze_authority_value(candidate),
            selected_result_manifest_path=selected_result_relative,
            selected_result_manifest_sha256=sha256_text(selected_result_text),
            selected_result=_freeze_authority_value(selected_data["upstream"]),
            selected_execution_artifact=selected_execution_artifact,
            experiment_contract_path=contract_relative,
            experiment_contract_sha256=manifest["experiment_contract_sha256"],
            experiment_contract_bytes=contract_bytes,
            run_config_path=config_relative,
            run_config_sha256=manifest["run_config_sha256"],
            run_config_bytes=config_bytes,
            summary_bytes=summary_bytes,
            summary=_freeze_authority_value(summary_value),
            analysis_bytes=analysis_bytes,
            analysis_text=analysis_text,
            metric_observations=_freeze_authority_value(
                selected_data["metric_observations"]
            ),
            structured_results=_freeze_authority_value(
                selected_data["structured_results"]
            ),
            artifacts=tuple(artifact_snapshots),
            project_artifacts=project_artifacts,
        )

    return snapshot()

def _load_active_config_for_reconstruction(
    run_dir: Path,
) -> tuple[RCConfig, str, str, str, tuple[tuple[str, str], ...]]:
    """Select and replay the active config without consulting the root manifest."""

    config_snapshots = tuple(
        (
            path.name,
            _read_regular_file(path, f"config snapshot {path.name}"),
        )
        for path in sorted(
            (run_dir / "config.yaml", *run_dir.glob("config.resumed-*.yaml")),
            key=lambda item: item.name,
        )
    )
    pointer_path = run_dir / "active_config_snapshot.json"
    pointer_text: str | None = None
    if pointer_path.exists() or pointer_path.is_symlink():
        pointer_text = _read_regular_file(
            pointer_path, "active config snapshot pointer"
        )

    history_path = run_dir / "config_snapshot_history.jsonl"
    checkpoint_path = run_dir / "checkpoint.json"
    history_text = (
        _read_regular_file(history_path, "config snapshot history")
        if history_path.exists() or history_path.is_symlink()
        else None
    )
    checkpoint_text = (
        _read_regular_file(checkpoint_path, "checkpoint")
        if checkpoint_path.exists() or checkpoint_path.is_symlink()
        else None
    )
    try:
        return replay_config_snapshot_namespace(
            ConfigSnapshotNamespaceInputs(
                snapshots=config_snapshots,
                pointer_text=pointer_text,
                history_text=history_text,
                checkpoint_text=checkpoint_text,
            ),
            project_root=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        raise CanonicalExperimentEvidenceError(
            f"active config reconstruction failed: {exc}"
        ) from exc


def reconstruct_expected_canonical_evidence(
    run_dir: Path,
) -> CanonicalExperimentEvidence:
    """Reconstruct the expected Stage 9-14 authority before trusting its root."""

    require_canonical_evidence_capabilities(
        "reconstruct_expected_canonical_evidence"
    )
    from researchclaw.pipeline.canonical_execution_controller import (
        CanonicalAnalysisController,
    )

    controller = CanonicalAnalysisController.acquire_reader(run_dir)
    try:
        return _reconstruct_expected_canonical_evidence_under_lock(run_dir)
    finally:
        controller.close()


def _reconstruct_expected_canonical_evidence_under_lock(
    run_dir: Path,
) -> CanonicalExperimentEvidence:
    """Reconstruct while the caller holds the canonical publication lock."""

    config, config_relative, config_text, config_sha256, config_snapshots = (
        _load_active_config_for_reconstruction(run_dir)
    )
    expected = _build_expected_canonical_experiment_manifest(run_dir, config)

    manifest_text = _read_regular_file(
        run_dir / "canonical_experiment_evidence.json",
        "canonical experiment evidence manifest",
    )
    stored = parse_canonical_experiment_manifest(manifest_text)
    if stored != expected:
        raise CanonicalExperimentEvidenceError(
            "stored canonical experiment manifest differs from expected reconstruction"
        )

    evidence = _load_canonical_experiment_evidence_under_lock(run_dir)
    if dict(evidence.manifest) != expected:
        raise CanonicalExperimentEvidenceError(
            "canonical experiment evidence changed during reconstruction"
        )
    if (
        evidence.run_config_path != config_relative
        or evidence.run_config_sha256 != config_sha256
        or evidence.run_config_bytes != config_text.encode("utf-8")
    ):
        raise CanonicalExperimentEvidenceError(
            "canonical experiment config differs from active reconstruction input"
        )

    (
        final_config,
        final_relative,
        final_text,
        final_sha256,
        final_config_snapshots,
    ) = (
        _load_active_config_for_reconstruction(run_dir)
    )
    final_expected = _build_expected_canonical_experiment_manifest(
        run_dir, final_config
    )
    if (
        final_expected != expected
        or final_relative != config_relative
        or final_text != config_text
        or final_sha256 != config_sha256
        or final_config_snapshots != config_snapshots
    ):
        raise CanonicalExperimentEvidenceError(
            "canonical reconstruction inputs changed during replay"
        )
    final_evidence = _load_canonical_experiment_evidence_under_lock(run_dir)
    if final_evidence != evidence:
        raise CanonicalExperimentEvidenceError(
            "canonical experiment evidence changed during final reconstruction"
        )
    if _read_regular_file(
        run_dir / "canonical_experiment_evidence.json",
        "canonical experiment evidence manifest",
    ) != manifest_text:
        raise CanonicalExperimentEvidenceError(
            "canonical manifest changed during release reconstruction"
        )
    for field, expected_bytes in (
        ("selected_summary", evidence.summary_bytes),
        ("selected_analysis", evidence.analysis_bytes),
    ):
        relative = expected[field]["canonical_path"]
        if _read_regular_bytes(
            run_dir / relative, f"{field} compatibility copy"
        ) != expected_bytes:
            raise CanonicalExperimentEvidenceError(
                f"{field} compatibility copy changed during release reconstruction"
            )
    (
        settled_config,
        settled_relative,
        settled_text,
        settled_sha256,
        settled_config_snapshots,
    ) = _load_active_config_for_reconstruction(run_dir)
    if (
        canonical_json_text(settled_config.to_dict())
        != canonical_json_text(config.to_dict())
        or settled_relative != config_relative
        or settled_text != config_text
        or settled_sha256 != config_sha256
        or settled_config_snapshots != config_snapshots
    ):
        raise CanonicalExperimentEvidenceError(
            "active config namespace changed during final reconstruction"
        )
    return evidence


def _snapshot_selected_execution_artifact(
    run_dir: Path,
    config: RCConfig,
    upstream: Mapping[str, Any],
    expected_observations: Mapping[str, Any],
) -> CanonicalEvidenceArtifact:
    """Capture the invocation JSON that actually owns selected observations."""

    if upstream.get("result_set_type") == "stage12_baseline":
        result_set = upstream
        ref = next(
            (
                item
                for item in result_set["evidence_files"]
                if item["path"] == "stage-12/evidence-v1/run-1.json"
            ),
            None,
        )
    elif upstream.get("result_set_type") == "stage13_refinement":
        selected = upstream["selected_result"]
        if selected["type"] == "baseline":
            result_set = validate_experiment_result_set(run_dir, config)
            ref = next(
                (
                    item
                    for item in result_set["evidence_files"]
                    if item["path"] == "stage-12/evidence-v1/run-1.json"
                ),
                None,
            )
        else:
            iteration = next(
                (
                    item
                    for item in upstream["iterations"]
                    if item["iteration_id"] == selected["iteration_id"]
                ),
                None,
            )
            if not isinstance(iteration, Mapping):
                raise CanonicalExperimentEvidenceError(
                    "selected refinement iteration is missing"
                )
            ref = iteration["initial_execution"]
    else:
        raise CanonicalExperimentEvidenceError("unsupported selected result set type")
    if not isinstance(ref, Mapping):
        raise CanonicalExperimentEvidenceError("selected execution artifact is missing")
    path = _required_string(ref.get("path"), "selected execution path")
    expected_sha = _required_string(ref.get("sha256"), "selected execution sha256")
    content = _read_regular_bytes(run_dir / path, "selected execution artifact")
    actual_sha = hashlib.sha256(content).hexdigest()
    if actual_sha != expected_sha:
        raise CanonicalExperimentEvidenceError("selected execution artifact hash mismatch")
    try:
        invocation = parse_invocation_result(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CanonicalExperimentEvidenceError(
            "selected execution artifact is not UTF-8"
        ) from exc
    if invocation["metric_observations"] != expected_observations:
        raise CanonicalExperimentEvidenceError(
            "selected execution observations differ from canonical snapshot"
        )
    return CanonicalEvidenceArtifact(
        role="selected_execution",
        path=path,
        sha256=actual_sha,
        content=content,
    )


def _snapshot_selected_project(
    run_dir: Path,
    config: RCConfig,
    upstream: Mapping[str, Any],
) -> tuple[CanonicalProjectArtifact, ...]:
    """Capture the replay-selected flat project while the publication lock is held."""

    seal_text = _read_regular_file(
        run_dir / "stage-10/selected_candidate_manifest.json",
        "selected candidate manifest",
    )
    seal = validate_selected_candidate_manifest(run_dir, config, seal_text)
    expected_seal_sha = upstream.get("sealed_candidate_manifest_sha256")
    if sha256_text(seal_text) != expected_seal_sha:
        raise CanonicalExperimentEvidenceError(
            "selected project seal differs from selected result"
        )

    selected_sources: dict[str, tuple[str, str]] = {}
    if upstream.get("result_set_type") == "stage12_baseline":
        for logical_name, metadata in seal["files"].items():
            selected_sources[logical_name] = (
                f"stage-10/selected_candidate/{logical_name}",
                metadata["sha256"],
            )
    elif upstream.get("result_set_type") == "stage13_refinement":
        selected = upstream.get("selected_result")
        if selected == {"type": "baseline", "iteration_id": None}:
            for logical_name, metadata in seal["files"].items():
                selected_sources[logical_name] = (
                    f"stage-10/selected_candidate/{logical_name}",
                    metadata["sha256"],
                )
        elif isinstance(selected, Mapping) and selected.get("type") == "iteration":
            iteration_id = selected.get("iteration_id")
            iteration = next(
                (
                    item
                    for item in upstream.get("iterations", ())
                    if isinstance(item, Mapping)
                    and item.get("iteration_id") == iteration_id
                ),
                None,
            )
            if iteration is None:
                raise CanonicalExperimentEvidenceError(
                    "selected refinement project is missing"
                )
            for ref in iteration["project_files"]:
                source_path = ref["path"]
                marker = "/project/"
                if marker not in source_path:
                    raise CanonicalExperimentEvidenceError(
                        "selected refinement project path is invalid"
                    )
                logical_name = source_path.rsplit(marker, 1)[1]
                selected_sources[logical_name] = (source_path, ref["sha256"])
        else:
            raise CanonicalExperimentEvidenceError(
                "selected refinement project identity is invalid"
            )
    else:
        raise CanonicalExperimentEvidenceError("selected result type is invalid")

    if set(selected_sources) != set(seal["files"]):
        raise CanonicalExperimentEvidenceError(
            "selected project file closure differs from Stage 10 seal"
        )
    snapshots: list[CanonicalProjectArtifact] = []
    for logical_name in sorted(selected_sources):
        source_path, expected_sha = selected_sources[logical_name]
        content = _read_regular_bytes(
            run_dir / source_path,
            f"selected project file {logical_name}",
        )
        actual_sha = hashlib.sha256(content).hexdigest()
        if actual_sha != expected_sha:
            raise CanonicalExperimentEvidenceError(
                f"selected project file changed during access: {logical_name}"
            )
        snapshots.append(
            CanonicalProjectArtifact(
                logical_name=logical_name,
                source_path=source_path,
                sha256=actual_sha,
                content=content,
            )
        )
    return tuple(snapshots)


def _build_canonical_publication_plan(
    run_dir: Path,
    config: RCConfig,
) -> _CanonicalPublicationPlan:
    """Select and capture one immutable Stage 14 publication plan."""

    selected, upstream, metric_key, direction, metric_value = _derive_selected_result(
        run_dir, config
    )
    winner_path, candidate_text, candidate = _select_canonical_candidate(
        run_dir,
        selected_result=selected,
        upstream=upstream,
        primary_metric_key=metric_key,
        optimization_direction=direction,
        metric_value=metric_value,
    )
    artifact_map = {item["role"]: item for item in candidate["artifacts"]}
    summary_source = _require_regular_path(
        winner_path.parent / artifact_map["summary"]["path"],
        "selected candidate summary",
    )
    analysis_source = _require_regular_path(
        winner_path.parent / artifact_map["analysis"]["path"],
        "selected candidate analysis",
    )
    winner_relative = winner_path.relative_to(run_dir).as_posix()
    summary_relative = summary_source.relative_to(run_dir).as_posix()
    analysis_relative = analysis_source.relative_to(run_dir).as_posix()
    payload = {
        "schema_version": CANONICAL_MANIFEST_SCHEMA_VERSION,
        "selection_policy_version": 1,
        "selected_result": selected,
        "experiment_contract_path": upstream["experiment_contract_path"],
        "experiment_contract_sha256": upstream["experiment_contract_sha256"],
        "sealed_candidate_manifest_path": upstream["sealed_candidate_manifest_path"],
        "sealed_candidate_manifest_sha256": upstream["sealed_candidate_manifest_sha256"],
        "run_config_path": upstream["run_config_path"],
        "run_config_sha256": upstream["run_config_sha256"],
        "config_semantic_policy_version": upstream["config_semantic_policy_version"],
        "config_semantic_sha256": upstream["config_semantic_sha256"],
        "claim_scope": upstream["claim_scope"],
        "dataset_origin": upstream["dataset_origin"],
        "evaluator_schema": upstream["evaluator_schema"],
        "metric_authority": upstream["metric_authority"],
        "primary_metric": metric_key,
        "optimization_direction": direction,
        "selected_candidate": {
            "candidate_id": candidate["candidate_id"],
            "path": winner_relative,
            "sha256": sha256_text(candidate_text),
        },
        "selected_summary": {
            "source_path": summary_relative,
            "source_sha256": artifact_map["summary"]["sha256"],
            "canonical_path": "experiment_summary_best.json",
            "canonical_sha256": artifact_map["summary"]["sha256"],
        },
        "selected_analysis": {
            "source_path": analysis_relative,
            "source_sha256": artifact_map["analysis"]["sha256"],
            "canonical_path": "analysis_best.md",
            "canonical_sha256": artifact_map["analysis"]["sha256"],
        },
    }
    manifest = parse_canonical_experiment_manifest(canonical_json_text(payload))
    summary_bytes = summary_source.read_bytes()
    analysis_bytes = analysis_source.read_bytes()
    if hashlib.sha256(summary_bytes).hexdigest() != artifact_map["summary"]["sha256"]:
        raise CanonicalExperimentEvidenceError(
            "selected candidate summary changed during publication planning"
        )
    if hashlib.sha256(analysis_bytes).hexdigest() != artifact_map["analysis"]["sha256"]:
        raise CanonicalExperimentEvidenceError(
            "selected candidate analysis changed during publication planning"
        )
    return _CanonicalPublicationPlan(
        manifest=_freeze_authority_value(manifest),
        summary_bytes=summary_bytes,
        analysis_bytes=analysis_bytes,
    )


def _build_expected_canonical_experiment_manifest(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Derive the complete root manifest without reading or writing that root."""

    return _thaw_authority_value(
        _build_canonical_publication_plan(run_dir, config).manifest
    )


def publish_canonical_experiment_manifest(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Deterministically select Stage 14 evidence and publish the root pointer last."""
    with ReleaseGraphLock.acquire(
        run_dir, "publish_canonical_experiment_manifest", mode="write"
    ) as release_lock:
        owned = (
            "canonical_experiment_evidence.json",
            "experiment_summary_best.json",
            "analysis_best.md",
        )
        release_lock.remove_run_files(owned)
        plan = _build_canonical_publication_plan(run_dir, config)
        if _build_canonical_publication_plan(run_dir, config) != plan:
            raise CanonicalExperimentEvidenceError(
                "canonical publication sources changed before write"
            )
        payload = _thaw_authority_value(plan.manifest)
        manifest_text = canonical_json_text(payload)
        release_lock.assert_canonical()
        release_lock.write_run_bytes_atomic(
            "experiment_summary_best.json", plan.summary_bytes
        )
        release_lock.write_run_bytes_atomic("analysis_best.md", plan.analysis_bytes)
        release_lock.write_run_bytes_atomic(
            "canonical_experiment_evidence.json", manifest_text.encode("utf-8")
        )
        try:
            release_lock.assert_canonical()
            return validate_canonical_experiment_manifest(run_dir, config)
        except Exception:
            release_lock.remove_run_files(owned)
            raise


def _resolve_full_config_identity(run_dir: Path, config: RCConfig) -> tuple[str, str, str]:
    try:
        path, text, digest = resolve_active_config_snapshot(run_dir, config)
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError("config root must be a mapping")
        parsed = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    except Exception as exc:  # noqa: BLE001
        raise CanonicalExperimentEvidenceError(f"run config identity is invalid: {exc}") from exc
    if canonical_json_text(parsed.to_dict()) != canonical_json_text(config.to_dict()):
        raise CanonicalExperimentEvidenceError("active config differs from runtime config")
    return path, text, digest


def _resolve_producer_config_identity(
    run_dir: Path,
    config: RCConfig,
    recorded_path: object,
    recorded_sha256: object,
) -> tuple[str, str]:
    """Replay the immutable producer snapshot and current active semantic identity."""
    active_path, _active_text, _active_digest = _resolve_full_config_identity(run_dir, config)
    producer_relative = _required_string(recorded_path, "run_config_path")
    if _CONFIG_SNAPSHOT_RE.fullmatch(producer_relative) is None:
        raise CanonicalExperimentEvidenceError("noncanonical producer config path")
    expected_digest = _sha256(recorded_sha256, "run_config_sha256")
    producer_path = run_dir / producer_relative
    producer_text = _read_regular_file(producer_path, "producer config snapshot")
    if sha256_text(producer_text) != expected_digest:
        raise CanonicalExperimentEvidenceError("producer config exact hash mismatch")
    try:
        raw = yaml.safe_load(producer_text)
        if not isinstance(raw, dict):
            raise ValueError("config root must be a mapping")
        producer_config = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    except (ValueError, yaml.YAMLError) as exc:
        raise CanonicalExperimentEvidenceError(f"producer config is invalid: {exc}") from exc
    if semantic_config_sha256(producer_config) != semantic_config_sha256(config):
        raise CanonicalExperimentEvidenceError("producer and active config semantics differ")
    history_path = run_dir / "config_snapshot_history.jsonl"
    if history_path.exists():
        history_text = _read_regular_file(history_path, "config snapshot history")
        events = [_parse_object(line, "config history event") for line in history_text.split("\n") if line]
        if not any(
            event.get("config_source_path") == producer_relative
            and event.get("config_source_sha256") == expected_digest
            for event in events
        ):
            raise CanonicalExperimentEvidenceError("producer config is absent from run history")
    elif producer_relative != active_path:
        raise CanonicalExperimentEvidenceError("producer config is not the active initial snapshot")
    return producer_text, expected_digest


def _validate_common_run_bindings(
    run_dir: Path,
    config: RCConfig,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    contract_path = find_stage09_contract(run_dir)
    if contract_path is None:
        raise CanonicalExperimentEvidenceError("canonical experiment contract is missing")
    try:
        relative = contract_path.relative_to(run_dir).as_posix()
        contract = load_contract(contract_path)
    except (ValueError, ContractValidationError) as exc:
        raise CanonicalExperimentEvidenceError(f"canonical contract is invalid: {exc}") from exc
    if payload["experiment_contract_path"] != relative:
        raise CanonicalExperimentEvidenceError("experiment contract path mismatch")
    if contract_sha256(contract_path) != payload["experiment_contract_sha256"]:
        raise CanonicalExperimentEvidenceError("experiment contract hash mismatch")
    if payload["claim_scope"] != contract.claim_scope or payload["dataset_origin"] != contract.dataset_origin:
        raise CanonicalExperimentEvidenceError("contract scope/origin binding mismatch")
    if payload["evaluator_schema"] != "hpc_anomaly_detection_v1":
        raise CanonicalExperimentEvidenceError("unsupported evaluator schema")
    try:
        replay_metric_authority(
            run_dir=run_dir,
            topic=config.research.topic,
            experiment_mode=config.experiment.mode,
            stored_identity=contract.metric_authority,
            metric_units=contract.metric_units,
            metric_display_labels=contract.metric_display_labels,
        )
    except MetricAuthorityError as exc:
        raise CanonicalExperimentEvidenceError(
            f"metric authority replay failed: {exc}"
        ) from exc
    if payload["metric_authority"] != contract.metric_authority:
        raise CanonicalExperimentEvidenceError("contract metric authority binding mismatch")

    seal_path = run_dir / "stage-10/selected_candidate_manifest.json"
    seal_text = _read_regular_file(seal_path, "selected candidate manifest")
    if sha256_text(seal_text) != payload["sealed_candidate_manifest_sha256"]:
        raise CanonicalExperimentEvidenceError("sealed candidate manifest hash mismatch")
    seal = validate_selected_candidate_manifest(run_dir, config, seal_text)
    if payload["sealed_candidate_manifest_path"] != "stage-10/selected_candidate_manifest.json":
        raise CanonicalExperimentEvidenceError("sealed candidate manifest path mismatch")
    if (
        seal["contract_sha256"] != payload["experiment_contract_sha256"]
        or seal["config_semantic_policy_version"] != payload["config_semantic_policy_version"]
        or seal["config_semantic_sha256"] != payload["config_semantic_sha256"]
        or seal["metric_authority"] != payload["metric_authority"]
    ):
        raise CanonicalExperimentEvidenceError("producer binding differs from Stage 10 seal")
    _resolve_producer_config_identity(
        run_dir, config, payload["run_config_path"], payload["run_config_sha256"]
    )
    return seal, contract


def _require_common_binding_equality(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    label: str,
) -> None:
    if not _common_bindings_equal(left, right):
        raise CanonicalExperimentEvidenceError(f"{label} binding mismatch")


def _common_bindings_equal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    fields = (
        "experiment_contract_path", "experiment_contract_sha256",
        "sealed_candidate_manifest_path", "sealed_candidate_manifest_sha256",
        "config_semantic_policy_version", "config_semantic_sha256",
        "claim_scope", "dataset_origin", "evaluator_schema",
        "metric_authority",
    )
    return all(left[field] == right[field] for field in fields)


def _require_regular_path(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise CanonicalExperimentEvidenceError(f"{label} is missing or unsafe")
    return path


def _validate_exact_namespace(
    run_dir: Path,
    namespace: Path,
    expected_run_relative_paths: set[str],
    label: str,
) -> None:
    if namespace.is_symlink() or not namespace.is_dir():
        raise CanonicalExperimentEvidenceError(f"{label} namespace is missing or unsafe")
    namespace_relative = namespace.relative_to(run_dir)
    allowed_dirs = {
        parent.as_posix()
        for relative in expected_run_relative_paths
        for parent in Path(relative).parents
        if parent != Path(".") and parent != namespace_relative
        and namespace_relative in parent.parents
    }
    actual: set[str] = set()
    for path in namespace.rglob("*"):
        if path.is_symlink():
            raise CanonicalExperimentEvidenceError(f"{label} contains a symlink")
        relative = path.relative_to(run_dir).as_posix()
        if path.is_dir():
            if relative not in allowed_dirs:
                raise CanonicalExperimentEvidenceError(f"{label} contains an unmanifested directory")
            continue
        if not path.is_file():
            raise CanonicalExperimentEvidenceError(f"{label} contains a non-regular entry")
        actual.add(relative)
    if actual != expected_run_relative_paths:
        raise CanonicalExperimentEvidenceError(f"{label} file-set mismatch")


def _stage12_primary_metric(
    run_dir: Path,
    result_set: Mapping[str, Any],
    metric_key: str,
) -> Decimal:
    aggregate = parse_aggregate_results(
        _read_regular_file(run_dir / "stage-12/evidence-v1/results.json", "Stage 12 aggregate")
    )
    if aggregate["evaluator_schema"] != result_set["evaluator_schema"]:
        raise CanonicalExperimentEvidenceError("Stage 12 aggregate evaluator mismatch")
    observations = aggregate["metric_observations"].get(metric_key)
    if not isinstance(observations, list) or not observations:
        raise CanonicalExperimentEvidenceError("primary metric observations are missing")
    return _decimal_mean(observations)


def _recompute_refinement_validation_report(
    run_dir: Path,
    project_files: list[dict[str, Any]],
) -> dict[str, Any]:
    canonical_refs = sorted(project_files, key=lambda item: item["path"])
    if project_files != canonical_refs:
        raise CanonicalExperimentEvidenceError("Stage 13 project files are not canonically sorted")
    syntax_valid = True
    for ref in canonical_refs:
        if not ref["path"].endswith(".py"):
            continue
        raw = _require_regular_path(run_dir / ref["path"], "Stage 13 project file").read_bytes()
        try:
            encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
            source = raw.decode(encoding)
            compile(source, ref["path"], "exec")
        except (LookupError, SyntaxError, UnicodeDecodeError, ValueError):
            syntax_valid = False
    return {
        "schema_version": REFINEMENT_VALIDATION_SCHEMA_VERSION,
        "validation_policy_version": 1,
        "project_files_sha256": sha256_text(canonical_json_text(canonical_refs)),
        "checks": {"python_syntax_valid": syntax_valid},
    }


def _replay_refinement_attempt(
    run_dir: Path,
    *,
    project_files: list[dict[str, Any]],
    validation_ref: Mapping[str, Any],
    execution_ref: Mapping[str, Any],
    evaluator_schema: str,
    primary_metric_key: str,
    seal: Mapping[str, Any],
    contract: ExperimentContract,
) -> tuple[bool, list[str], Decimal | None]:
    _validate_refinement_project_ownership(run_dir, project_files, seal, contract)
    validation_report = parse_refinement_validation_report(
        _read_regular_file(run_dir / validation_ref["path"], "Stage 13 validation report")
    )
    expected_validation = _recompute_refinement_validation_report(run_dir, project_files)
    if validation_report != expected_validation:
        raise CanonicalExperimentEvidenceError("Stage 13 validation report replay mismatch")
    execution = parse_invocation_result(
        _read_regular_file(run_dir / execution_ref["path"], "Stage 13 execution result")
    )
    if execution["evaluator_schema"] != evaluator_schema:
        raise CanonicalExperimentEvidenceError("Stage 13 execution evaluator mismatch")
    expected_observations = _validate_evaluator_result(
        execution["structured_results"], contract, evaluator_schema
    )
    if execution["metric_observations"] != expected_observations:
        raise CanonicalExperimentEvidenceError("normalized metrics differ from evaluator result")
    observations = expected_observations.get(primary_metric_key)
    syntax_valid = validation_report["checks"]["python_syntax_valid"]
    metric_valid = isinstance(observations, list) and len(observations) == 1
    rejection_codes: list[str] = []
    if not syntax_valid:
        rejection_codes.append("python_syntax_invalid")
    if not metric_valid:
        rejection_codes.append("primary_metric_missing_or_nonsingular")
    accepted = syntax_valid and metric_valid
    observed = Decimal(canonical_decimal(observations[0])) if accepted else None
    return accepted, rejection_codes, observed


def _validate_refinement_project_ownership(
    run_dir: Path,
    project_files: list[dict[str, Any]],
    seal: Mapping[str, Any],
    contract: ExperimentContract,
) -> None:
    logical_files: dict[str, dict[str, Any]] = {}
    for ref in project_files:
        marker = "/project/"
        if marker not in ref["path"]:
            raise CanonicalExperimentEvidenceError("Stage 13 project path lacks ownership root")
        logical_name = ref["path"].rsplit(marker, 1)[1]
        if not logical_name or "/" in logical_name or "\\" in logical_name:
            raise CanonicalExperimentEvidenceError("Stage 13 project must preserve flat sealed filenames")
        logical_files[logical_name] = ref
    if set(logical_files) != set(seal["files"]):
        raise CanonicalExperimentEvidenceError("Stage 13 project file closure differs from Stage 10 seal")
    if set(seal["scaffold_files"]) != {"main.py"}:
        raise CanonicalExperimentEvidenceError("HPC evaluator requires sealed scaffold-owned main.py")
    stage10_root = run_dir / "stage-10/selected_candidate"
    for name, metadata in seal["scaffold_files"].items():
        refined = run_dir / logical_files[name]["path"]
        sealed = stage10_root / name
        if (
            logical_files[name]["sha256"] != metadata["sha256"]
            or refined.read_bytes() != sealed.read_bytes()
        ):
            raise CanonicalExperimentEvidenceError(
                f"Stage 13 replaced scaffold-owned evaluator file: {name}"
            )
    if (stage10_root / "main.py").read_bytes() != render_main_py(contract).encode("utf-8"):
        raise CanonicalExperimentEvidenceError("sealed evaluator differs from canonical renderer")


def _selected_refinement_metric(run_dir: Path, refinement: Mapping[str, Any]) -> Decimal:
    selected = refinement["selected_result"]
    if selected["type"] == "baseline":
        baseline = parse_experiment_result_set(
            _read_regular_file(run_dir / "stage-12/experiment_result_set.json", "Stage 12 result set")
        )
        return _stage12_primary_metric(run_dir, baseline, refinement["primary_metric_key"])
    for item in refinement["iterations"]:
        if item["iteration_id"] == selected["iteration_id"]:
            return Decimal(canonical_decimal(item["primary_metric_observation"]))
    raise CanonicalExperimentEvidenceError("selected refinement iteration is missing")


def _parse_refinement_iteration(item: object, index: int, iteration_ids: set[str]) -> None:
    if not isinstance(item, dict):
        raise CanonicalExperimentEvidenceError("iteration must be an object")
    _exact_keys(
        item,
        {
            "ordinal", "iteration_id", "project_files", "validation_report",
            "initial_execution", "runtime_repair", "accepted", "rejection_codes",
            "primary_metric_observation",
        },
        "refinement iteration",
    )
    if _strict_int(item["ordinal"], "iteration ordinal") != index:
        raise CanonicalExperimentEvidenceError("iteration ordinal mismatch")
    if item["iteration_id"] != f"iter-{index}":
        raise CanonicalExperimentEvidenceError("iteration_id mismatch")
    iteration_id = item["iteration_id"]
    iteration_ids.add(iteration_id)
    prefix = f"stage-13/evidence-v1/iterations/{iteration_id}/"
    project_prefix = prefix + "project/"
    project_files = _file_ref_list(item["project_files"], "project_files", nonempty=True)
    project_paths = [ref["path"] for ref in project_files]
    if project_paths != sorted(project_paths):
        raise CanonicalExperimentEvidenceError("project_files are not canonically sorted")
    if any(not path.startswith(project_prefix) for path in project_paths):
        raise CanonicalExperimentEvidenceError("noncanonical Stage 13 project path")
    if project_prefix + "main.py" not in project_paths:
        raise CanonicalExperimentEvidenceError("Stage 13 project is missing main.py")
    validation_report = _file_ref(item["validation_report"], "validation_report")
    initial_execution = _file_ref(item["initial_execution"], "initial_execution")
    if validation_report["path"] != prefix + "validation_report.json":
        raise CanonicalExperimentEvidenceError("noncanonical Stage 13 validation report path")
    if initial_execution["path"] != prefix + "initial_execution.json":
        raise CanonicalExperimentEvidenceError("noncanonical Stage 13 initial execution path")
    all_refs = list(project_files) + [validation_report, initial_execution]
    if item["runtime_repair"] is not None:
        raise CanonicalExperimentEvidenceError(
            "runtime_repair must be null under refinement policy v1"
        )
    all_paths = [ref["path"] for ref in all_refs]
    if len(all_paths) != len(set(all_paths)):
        raise CanonicalExperimentEvidenceError("Stage 13 cross-role path alias")
    if item["accepted"] is not True:
        raise CanonicalExperimentEvidenceError(
            "accepted must be true under refinement policy v1"
        )
    rejection_codes = item["rejection_codes"]
    if rejection_codes != []:
        raise CanonicalExperimentEvidenceError(
            "rejection_codes must be empty under refinement policy v1"
        )
    _finite_json_number(item["primary_metric_observation"], "primary_metric_observation")


def _parse_candidate_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("identity_payload must be an object")
    _exact_keys(
        value,
        {
            "candidate_identity_policy_version", "selected_result_type",
            "selected_result_manifest_sha256", "experiment_contract_sha256",
            "config_semantic_sha256", "metric_authority_selector_input_sha256",
            "primary_metric_key", "optimization_direction",
            "artifacts",
        },
        "candidate identity payload",
    )
    _require_equal(value, "candidate_identity_policy_version", 1)
    if value["selected_result_type"] not in {"stage12_baseline", "stage13_refinement"}:
        raise CanonicalExperimentEvidenceError("invalid selected_result_type")
    for field in (
        "selected_result_manifest_sha256", "experiment_contract_sha256",
        "config_semantic_sha256", "metric_authority_selector_input_sha256",
    ):
        _sha256(value[field], field)
    _required_string(value["primary_metric_key"], "primary_metric_key")
    if value["optimization_direction"] not in {"maximize", "minimize"}:
        raise CanonicalExperimentEvidenceError("invalid optimization_direction")
    artifacts = _artifact_list(value["artifacts"], "identity artifacts", path_field="logical_name")
    if artifacts != sorted(artifacts, key=lambda x: (x["role"], x["logical_name"])):
        raise CanonicalExperimentEvidenceError("identity artifacts are not canonically sorted")
    return value


def _selected_result_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("selected_result must be an object")
    _exact_keys(value, {"result_set_type", "manifest_path", "manifest_sha256"}, "selected_result")
    expected = {
        "stage12_baseline": "stage-12/experiment_result_set.json",
        "stage13_refinement": "stage-13/refinement_result_set.json",
    }
    result_type = value["result_set_type"]
    if result_type not in expected or value["manifest_path"] != expected[result_type]:
        raise CanonicalExperimentEvidenceError("selected_result type/path mismatch")
    _sha256(value["manifest_sha256"], "selected_result.manifest_sha256")
    return value


def _common_producer_bindings(payload: Mapping[str, Any]) -> None:
    expected_paths = {
        "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
    }
    for field, expected in expected_paths.items():
        if payload[field] != expected:
            raise CanonicalExperimentEvidenceError(f"noncanonical {field}")
    _safe_relative_path(payload["experiment_contract_path"], "experiment_contract_path")
    _safe_relative_path(payload["run_config_path"], "run_config_path")
    for field in (
        "experiment_contract_sha256", "sealed_candidate_manifest_sha256",
        "run_config_sha256", "config_semantic_sha256",
    ):
        _sha256(payload[field], field)
    if _strict_int(
        payload["config_semantic_policy_version"], "config semantic policy version"
    ) != CONFIG_SEMANTIC_POLICY_VERSION:
        raise CanonicalExperimentEvidenceError("unsupported config semantic policy")
    for field in ("claim_scope", "dataset_origin", "evaluator_schema"):
        _required_string(payload[field], field)
    _metric_authority_identity(payload["metric_authority"])


def _metric_authority_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("metric_authority must be an object")
    _exact_keys(
        value,
        {
            "path", "sha256", "policy_version", "domain_id", "evaluator_id",
            "domain_profile_path", "domain_profile_sha256",
            "selector_index_path", "selector_index_sha256",
            "domain_selector_package_path", "domain_selector_package_sha256",
            "domain_selector_snapshot_path", "domain_selector_snapshot_sha256",
            "selector_input_sha256", "selector_policy_version",
            "experiment_mode", "evaluator_kind",
        },
        "metric_authority",
    )
    expected_paths = {
        "path": "stage-09/metric_authority.json",
        "domain_profile_path": "stage-09/domain_profile.json",
        "selector_index_path": "stage-09/metric_authority_index.json",
        "domain_selector_snapshot_path": "stage-09/domain_selector_policy.json",
        "domain_selector_package_path": (
            "experiment_runtime/metric_authority/domain-selector-v1.json"
        ),
    }
    for field, expected in expected_paths.items():
        if value[field] != expected:
            raise CanonicalExperimentEvidenceError(
                f"noncanonical metric_authority.{field}"
            )
    for field in (
        "sha256", "domain_profile_sha256", "selector_index_sha256",
        "domain_selector_package_sha256", "domain_selector_snapshot_sha256",
        "selector_input_sha256",
    ):
        _sha256(value[field], f"metric_authority.{field}")
    if value["domain_selector_package_sha256"] != value["domain_selector_snapshot_sha256"]:
        raise CanonicalExperimentEvidenceError("domain selector snapshot hash mismatch")
    if value["policy_version"] != 1:
        raise CanonicalExperimentEvidenceError("unsupported metric authority policy")
    if value["selector_policy_version"] != "metric_selector_v1":
        raise CanonicalExperimentEvidenceError("unsupported metric selector policy")
    for field in ("domain_id", "evaluator_id", "experiment_mode", "evaluator_kind"):
        _required_string(value[field], f"metric_authority.{field}")
    return value


def _artifact_list(value: object, label: str, *, path_field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CanonicalExperimentEvidenceError(f"{label} must be a nonempty list")
    result: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_roles: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            raise CanonicalExperimentEvidenceError(f"{label} entry must be an object")
        _exact_keys(item, {"role", path_field, "sha256"}, f"{label} entry")
        role = _required_string(item["role"], "role")
        path = _safe_relative_path(item[path_field], path_field)
        _sha256(item["sha256"], "sha256")
        if path in seen_paths:
            raise CanonicalExperimentEvidenceError(f"duplicate {label} path")
        seen_paths.add(path)
        seen_roles[role] = seen_roles.get(role, 0) + 1
        result.append(item)
    if "candidate" in label or "identity artifact" in label:
        for role, expected_path in _MANDATORY_CANDIDATE_ROLES.items():
            matches = [item for item in result if item["role"] == role]
            if len(matches) != 1 or matches[0][path_field] != expected_path:
                raise CanonicalExperimentEvidenceError(f"missing or invalid mandatory role: {role}")
        for item in result:
            role = item["role"]
            path = item[path_field]
            if role in _MANDATORY_CANDIDATE_ROLES:
                continue
            if role == "auxiliary" and not _candidate_policy_v1_forbids_artifact(path):
                continue
            raise CanonicalExperimentEvidenceError("invalid candidate auxiliary role/path")
    return result


def _candidate_policy_v1_forbids_artifact(path: str) -> bool:
    """Reject FigureAgent output outside the sole canonical empty figure plan."""
    if "charts" in Path(path).parts:
        return True
    name = Path(path).name
    if name in _FIGURE_AGENT_INTERMEDIATE_NAMES:
        return True
    if name.startswith("figure_plan") and path != "figure_plan.json":
        return True
    return (
        name.endswith(".json")
        and (name.startswith("scripts_") or name.startswith("reviews_"))
    )


def _metric_observations(value: object) -> dict[str, list[int | float | Decimal]]:
    if not isinstance(value, dict) or not value:
        raise CanonicalExperimentEvidenceError("metric_observations must be a nonempty object")
    for key, observations in value.items():
        _required_string(key, "metric key")
        if not isinstance(observations, list) or not observations:
            raise CanonicalExperimentEvidenceError("metric observation array must be nonempty")
        for observation in observations:
            if isinstance(observation, bool) or not isinstance(
                observation, (int, float, Decimal)
            ):
                raise CanonicalExperimentEvidenceError("metric observation must be a JSON number")
            if isinstance(observation, float) and not math.isfinite(observation):
                raise CanonicalExperimentEvidenceError("metric observation must be finite")
            if isinstance(observation, Decimal) and not observation.is_finite():
                raise CanonicalExperimentEvidenceError("metric observation must be finite")
    return value


def _structured_results(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("structured_results must be an object")
    _reject_nonfinite_numbers(value, "structured_results")
    return value


def _file_ref_list(value: object, label: str, *, nonempty: bool = True) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CanonicalExperimentEvidenceError(f"{label} must be a list")
    refs = [_file_ref(item, f"{label} entry") for item in value]
    paths = [item["path"] for item in refs]
    if len(paths) != len(set(paths)):
        raise CanonicalExperimentEvidenceError(f"duplicate {label} path")
    return refs


def _file_ref(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError(f"{label} must be an object")
    _exact_keys(value, {"path", "sha256"}, label)
    _safe_relative_path(value["path"], f"{label}.path")
    _sha256(value["sha256"], f"{label}.sha256")
    return value


def _file_map(value: object, label: str, *, owner: str | None, nonempty: bool = False) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or (nonempty and not value):
        raise CanonicalExperimentEvidenceError(f"{label} must be an object")
    for name, metadata in value.items():
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise CanonicalExperimentEvidenceError(f"unsafe {label} filename")
        if not name.endswith(".py"):
            raise CanonicalExperimentEvidenceError(f"{label} may contain only Python files")
        if not isinstance(metadata, dict):
            raise CanonicalExperimentEvidenceError(f"{label} metadata must be an object")
        expected = {"sha256"} if owner is None else {"sha256", "owner"}
        _exact_keys(metadata, expected, f"{label}.{name}")
        _sha256(metadata["sha256"], f"{label}.{name}.sha256")
        if owner is not None and metadata["owner"] != owner:
            raise CanonicalExperimentEvidenceError(f"invalid owner for {name}")
    return value


def _parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, CanonicalExperimentEvidenceError) as exc:
        raise CanonicalExperimentEvidenceError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError(f"{label} root must be an object")
    _reject_nonfinite_numbers(value, label)
    return value


def _parse_json_value(text: str, label: str) -> object:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, CanonicalExperimentEvidenceError) as exc:
        raise CanonicalExperimentEvidenceError(f"invalid structured artifact {label}: {exc}") from exc
    _reject_nonfinite_numbers(value, label)
    return value


def _reject_decoded_identity_strings(value: object, forbidden: tuple[str, ...]) -> None:
    if isinstance(value, str) and any(token in value for token in forbidden):
        raise CanonicalExperimentEvidenceError("structured artifact contains forbidden identity data")
    if isinstance(value, list):
        for item in value:
            _reject_decoded_identity_strings(item, forbidden)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_decoded_identity_strings(key, forbidden)
            _reject_decoded_identity_strings(item, forbidden)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalExperimentEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise CanonicalExperimentEvidenceError(f"nonfinite JSON constant: {value}")


def _reject_nonfinite_numbers(value: object, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalExperimentEvidenceError(f"{label} contains a nonfinite number")
    if isinstance(value, Decimal) and not value.is_finite():
        raise CanonicalExperimentEvidenceError(f"{label} contains a nonfinite number")
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite_numbers(item, label)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite_numbers(item, label)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CanonicalExperimentEvidenceError(
            f"{label} fields mismatch: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _require_equal(payload: Mapping[str, Any], field: str, expected: object) -> None:
    if payload[field] != expected or (
        isinstance(expected, int) and type(payload[field]) is not int
    ):
        raise CanonicalExperimentEvidenceError(f"invalid {field}")


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalExperimentEvidenceError(f"{field} must be an integer")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalExperimentEvidenceError(f"{field} must be a nonempty string")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CanonicalExperimentEvidenceError(f"{field} must be lowercase sha256")
    return value


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise CanonicalExperimentEvidenceError(f"{field} must be 64 lowercase hex")
    return value


def _safe_relative_path(value: object, field: str) -> str:
    path = _required_string(value, field)
    if "\\" in path or path.startswith("/") or "//" in path:
        raise CanonicalExperimentEvidenceError(f"{field} is not a normalized relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CanonicalExperimentEvidenceError(f"{field} is not a normalized relative path")
    if Path(path).as_posix() != path:
        raise CanonicalExperimentEvidenceError(f"{field} is not canonical")
    return path


def _freeze_authority_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_authority_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_authority_value(item) for item in value)
    return value


def _thaw_authority_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_authority_value(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_authority_value(item) for item in value]
    return value


def _read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise CanonicalExperimentEvidenceError(f"{label} is missing or unsafe")
        return path.read_bytes()
    except OSError as exc:
        raise CanonicalExperimentEvidenceError(f"cannot read {label}: {exc}") from exc


def _read_regular_file(path: Path, label: str) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise CanonicalExperimentEvidenceError(f"{label} is missing or unsafe")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CanonicalExperimentEvidenceError(f"cannot read {label}: {exc}") from exc
