"""Machine-readable experiment contract for Stages 9-12."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace

from researchclaw.experiment_runtime.metric_authority import (
    MetricAuthorityError,
    MetricAuthoritySelection,
    publish_metric_authority_snapshots,
    select_metric_authority,
)


CLAIM_SCOPES = {"pipeline_validation", "exploratory", "research_release"}
DATASET_ORIGINS = {"synthetic", "public", "local_hardware"}
METRIC_DIRECTIONS = {"maximize", "minimize"}


class ContractValidationError(ValueError):
    """Raised when an experiment contract is missing required invariants."""


@dataclass(frozen=True)
class ExperimentContract:
    schema_version: int
    topic: str
    claim_scope: str
    dataset_origin: str
    dataset_name: str | None
    primary_metric: dict[str, Any]
    smoke_budget_sec: int
    run_budget_sec: int
    allowed_inputs: list[dict[str, Any]]
    allowed_outputs: list[dict[str, Any]]
    evaluator: dict[str, Any]
    safety: dict[str, Any]
    sealing: dict[str, Any]
    metric_authority: dict[str, Any]
    metric_units: dict[str, str]
    metric_display_labels: dict[str, list[str]]
    evaluator_authority: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "topic": self.topic,
            "claim_scope": self.claim_scope,
            "dataset_origin": self.dataset_origin,
            "dataset_name": self.dataset_name,
            "primary_metric": dict(self.primary_metric),
            "smoke_budget_sec": self.smoke_budget_sec,
            "run_budget_sec": self.run_budget_sec,
            "allowed_inputs": list(self.allowed_inputs),
            "allowed_outputs": list(self.allowed_outputs),
            "evaluator": dict(self.evaluator),
            "safety": dict(self.safety),
            "sealing": dict(self.sealing),
            "metric_authority": dict(self.metric_authority),
            "metric_units": dict(self.metric_units),
            "metric_display_labels": {
                key: list(value) for key, value in self.metric_display_labels.items()
            },
        }
        if self.schema_version == 3:
            value["evaluator_authority"] = dict(self.evaluator_authority or {})
        return value


def _validate_evaluator_authority(
    value: dict[str, Any], errors: list[str]
) -> None:
    expected_fields = {
        "kind",
        "domain_id",
        "evaluator_id",
        "evaluator_schema",
        "package_manifest_package_path",
        "package_manifest_package_sha256",
        "package_manifest_snapshot_path",
        "package_manifest_snapshot_sha256",
        "execution_policy_package_path",
        "execution_policy_package_sha256",
        "execution_policy_snapshot_path",
        "execution_policy_snapshot_sha256",
        "input_capture_policy_version",
        "result_set_policy_version",
        "observation_replay_policy_version",
    }
    _require_exact_fields(value, expected_fields, "evaluator_authority", errors)
    version_fields = {
        "input_capture_policy_version",
        "result_set_policy_version",
        "observation_replay_policy_version",
    }
    for field in version_fields:
        if type(value.get(field)) is not int:
            errors.append(f"evaluator_authority.{field} must be a true integer")
    for field in expected_fields - version_fields:
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value:
            errors.append(f"evaluator_authority.{field} must be a nonempty string")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def contract_sha256(
    path: Path, *, namespace: BoundOutputNamespace | None = None
) -> str:
    return hashlib.sha256(_read_contract_bytes(path, namespace=namespace)).hexdigest()


def validate_contract_dict(
    data: dict[str, Any],
    *,
    authority_selection: MetricAuthoritySelection | None = None,
) -> ExperimentContract:
    errors: list[str] = []

    schema_version = data.get("schema_version")
    base_fields = {
        "schema_version", "topic", "claim_scope", "dataset_origin",
        "dataset_name", "primary_metric", "smoke_budget_sec",
        "run_budget_sec", "allowed_inputs", "allowed_outputs", "evaluator",
        "safety", "sealing", "metric_authority", "metric_units",
        "metric_display_labels",
    }
    expected_fields = base_fields | ({"evaluator_authority"} if schema_version == 3 else set())
    if set(data) != expected_fields:
        errors.append("contract fields must match the selected schema exactly")

    if type(schema_version) is not int or schema_version not in {2, 3}:
        errors.append("schema_version must be true integer 2 or 3")

    topic_value = data.get("topic")
    topic = topic_value if isinstance(topic_value, str) else ""
    if not topic.strip():
        errors.append("topic is required")

    claim_scope_value = data.get("claim_scope")
    claim_scope = claim_scope_value if isinstance(claim_scope_value, str) else ""
    if claim_scope not in CLAIM_SCOPES:
        errors.append(f"claim_scope must be one of {sorted(CLAIM_SCOPES)}")

    dataset_origin_value = data.get("dataset_origin")
    dataset_origin = dataset_origin_value if isinstance(dataset_origin_value, str) else ""
    if dataset_origin not in DATASET_ORIGINS:
        errors.append(f"dataset_origin must be one of {sorted(DATASET_ORIGINS)}")

    if claim_scope == "research_release" and dataset_origin == "synthetic":
        errors.append("research_release cannot use synthetic dataset_origin")

    primary_metric = data.get("primary_metric")
    if not isinstance(primary_metric, dict):
        errors.append("primary_metric must be an object")
        primary_metric = {}
    else:
        _require_exact_fields(
            primary_metric,
            {"key", "direction", "minimum_valid_value"},
            "primary_metric",
            errors,
        )
        if not isinstance(primary_metric.get("key"), str) or not primary_metric["key"]:
            errors.append("primary_metric.key is required")
        if primary_metric.get("direction") not in METRIC_DIRECTIONS:
            errors.append("primary_metric.direction must be maximize or minimize")
        minimum = primary_metric.get("minimum_valid_value")
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum):
            errors.append("primary_metric.minimum_valid_value must be finite")

    smoke_budget_sec = _int_value(data.get("smoke_budget_sec"))
    run_budget_sec = _int_value(data.get("run_budget_sec"))
    if smoke_budget_sec is None or smoke_budget_sec <= 0 or smoke_budget_sec > 120:
        errors.append("smoke_budget_sec must be in 1..120")
        smoke_budget_sec = 0
    if run_budget_sec is None or run_budget_sec <= 0:
        errors.append("run_budget_sec must be positive")
        run_budget_sec = 0
    if smoke_budget_sec and run_budget_sec and run_budget_sec < smoke_budget_sec:
        errors.append("run_budget_sec must be >= smoke_budget_sec")

    evaluator = data.get("evaluator")
    if not isinstance(evaluator, dict):
        errors.append("evaluator must be an object")
        evaluator = {}
    else:
        _require_exact_fields(
            evaluator,
            {"command", "owner", "timeout_sec", "required_result_keys"},
            "evaluator",
            errors,
        )
        expected_command = (
            "python evaluator/evaluator_main.py"
            if schema_version == 3
            else "python main.py"
        )
        expected_owner = "domain_evaluator" if schema_version == 3 else "scaffold"
        if evaluator.get("command") != expected_command:
            errors.append("evaluator.command does not match schema policy")
        if evaluator.get("owner") != expected_owner:
            errors.append("evaluator.owner does not match schema policy")
        timeout_sec = evaluator.get("timeout_sec")
        if type(timeout_sec) is not int or timeout_sec <= 0:
            errors.append("evaluator.timeout_sec must be a positive integer")
        required = evaluator.get("required_result_keys")
        if not isinstance(required, list):
            errors.append("evaluator.required_result_keys must be a list")
        else:
            expected_required = [] if schema_version == 3 else ["dataset_origin", "metrics"]
            if required != expected_required:
                errors.append("evaluator.required_result_keys must match policy exactly")

    allowed_inputs = data.get("allowed_inputs")
    allowed_outputs = data.get("allowed_outputs")
    if not isinstance(allowed_inputs, list):
        errors.append("allowed_inputs must be a list")
        allowed_inputs = []
    elif allowed_inputs:
        errors.append("allowed_inputs must be empty in contract policy v2")
    if not isinstance(allowed_outputs, list):
        errors.append("allowed_outputs must be a list")
        allowed_outputs = []
    elif len(allowed_outputs) != 1 or not isinstance(allowed_outputs[0], dict):
        errors.append("allowed_outputs must contain one result declaration")
    else:
        output = allowed_outputs[0]
        _require_exact_fields(output, {"path", "required"}, "allowed_outputs item", errors)
        expected_output = "score_evidence.jsonl" if schema_version == 3 else "results.json"
        if output.get("path") != expected_output or output.get("required") is not True:
            errors.append("allowed_outputs does not match schema policy")

    safety = data.get("safety")
    sealing = data.get("sealing")
    if not isinstance(safety, dict):
        errors.append("safety must be an object")
        safety = {}
    else:
        _require_exact_fields(
            safety, {"network", "env_policy", "evidence_policy"}, "safety", errors
        )
        if safety.get("network") != "none":
            errors.append("safety.network must be none")
        if safety.get("env_policy") != "allowlist":
            errors.append("safety.env_policy must be allowlist")
        expected_evidence_policy = (
            "stage12_independent_verifier_only"
            if schema_version == 3
            else "stage12_recomputed_only"
        )
        if safety.get("evidence_policy") != expected_evidence_policy:
            errors.append("safety.evidence_policy mismatch")
    if not isinstance(sealing, dict):
        errors.append("sealing must be an object")
        sealing = {}
    else:
        _require_exact_fields(
            sealing,
            {"candidate_manifest", "content_hash_algorithm"},
            "sealing",
            errors,
        )
        if sealing.get("candidate_manifest") != "selected_candidate_manifest.json":
            errors.append("sealing.candidate_manifest mismatch")
        if sealing.get("content_hash_algorithm") != "sha256":
            errors.append("sealing.content_hash_algorithm must be sha256")

    metric_authority = data.get("metric_authority")
    metric_units = data.get("metric_units")
    metric_display_labels = data.get("metric_display_labels")
    evaluator_authority = data.get("evaluator_authority")
    if not isinstance(metric_authority, dict):
        errors.append("metric_authority must be an object")
        metric_authority = {}
    if not isinstance(metric_units, dict):
        errors.append("metric_units must be an object")
        metric_units = {}
    if not isinstance(metric_display_labels, dict):
        errors.append("metric_display_labels must be an object")
        metric_display_labels = {}
    if schema_version == 3 and not isinstance(evaluator_authority, dict):
        errors.append("evaluator_authority must be an object for schema v3")
        evaluator_authority = {}
    elif schema_version != 3 and evaluator_authority is not None:
        errors.append("evaluator_authority is forbidden for schema v2")
    if schema_version == 3 and isinstance(evaluator_authority, dict):
        _validate_evaluator_authority(evaluator_authority, errors)

    if not errors:
        try:
            experiment_mode = metric_authority.get("experiment_mode")
            selected = authority_selection or select_metric_authority(
                topic, experiment_mode
            )
            if metric_authority != selected.contract_identity():
                errors.append("metric_authority does not match trusted selector result")
            if metric_units != selected.metric_units:
                errors.append("metric_units do not match trusted registry projection")
            if metric_display_labels != selected.metric_display_labels:
                errors.append(
                    "metric_display_labels do not match trusted registry projection"
                )
            metric_key = primary_metric.get("key")
            if metric_key not in selected.metric_units:
                errors.append("primary_metric.key is absent from metric authority")
            if schema_version != selected.schema_version + 1:
                errors.append("contract and metric authority schema rows are incompatible")
            if schema_version == 3 and evaluator_authority != selected.evaluator_authority:
                errors.append("evaluator_authority does not match trusted selector result")
        except MetricAuthorityError as exc:
            errors.append(f"metric authority is invalid: {exc}")

    if schema_version == 3:
        if claim_scope != "pipeline_validation":
            errors.append("contract v3 claim_scope must be pipeline_validation")
        if dataset_origin != "synthetic":
            errors.append("contract v3 dataset_origin must be synthetic")
        if data.get("dataset_name") != "controlled_synthetic_iscas85_trojan_localization_v1":
            errors.append("contract v3 dataset_name mismatch")
        if primary_metric != {
            "key": "auprc",
            "direction": "maximize",
            "minimum_valid_value": 0.0,
        }:
            errors.append("contract v3 primary metric mismatch")
        if sorted(metric_units) != [
            "accuracy", "auprc", "auroc", "f1", "fpr", "precision", "recall", "top_k_precision"
        ]:
            errors.append("contract v3 metric key set mismatch")

    if errors:
        raise ContractValidationError("; ".join(errors))

    dataset_name_raw = data.get("dataset_name")
    if dataset_name_raw is not None and (
        not isinstance(dataset_name_raw, str)
        or not dataset_name_raw
        or dataset_name_raw != dataset_name_raw.strip()
    ):
        raise ContractValidationError("dataset_name must be null or a trimmed nonempty string")
    dataset_name = dataset_name_raw
    return ExperimentContract(
        schema_version=int(schema_version),
        topic=topic,
        claim_scope=claim_scope,
        dataset_origin=dataset_origin,
        dataset_name=dataset_name,
        primary_metric=primary_metric,
        smoke_budget_sec=int(smoke_budget_sec),
        run_budget_sec=int(run_budget_sec),
        allowed_inputs=allowed_inputs,
        allowed_outputs=allowed_outputs,
        evaluator=evaluator,
        safety=safety,
        sealing=sealing,
        metric_authority=metric_authority,
        metric_units={str(key): str(value) for key, value in metric_units.items()},
        metric_display_labels={
            str(key): list(value) for key, value in metric_display_labels.items()
        },
        evaluator_authority=(
            dict(evaluator_authority) if isinstance(evaluator_authority, dict) else None
        ),
    )


def load_contract(
    path: Path, *, namespace: BoundOutputNamespace | None = None
) -> ExperimentContract:
    try:
        content = _read_contract_bytes(path, namespace=namespace)
    except OSError as exc:
        raise ContractValidationError(f"cannot read contract: {exc}") from exc
    return load_contract_bytes(content)


def parse_contract_bytes(content: bytes) -> dict[str, Any]:
    """Strictly parse captured contract bytes without applying authority policy."""

    try:
        raw = yaml.load(content.decode("utf-8"), Loader=_StrictLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractValidationError(f"cannot read contract: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractValidationError("contract root must be an object")
    return raw


def load_contract_bytes(
    content: bytes,
    *,
    authority_selection: MetricAuthoritySelection | None = None,
) -> ExperimentContract:
    """Strictly load captured contract bytes without reopening their path."""

    return validate_contract_dict(
        parse_contract_bytes(content),
        authority_selection=authority_selection,
    )


def dump_contract(
    contract: ExperimentContract,
    path: Path,
    *,
    namespace: BoundOutputNamespace | None = None,
) -> str:
    text = yaml.safe_dump(contract.to_dict(), sort_keys=False, allow_unicode=True)

    def publish(bound: BoundOutputNamespace) -> None:
        if bound.stage_dir != path.parent:
            raise OSError("contract namespace does not match output directory")
        bound.write_text_atomic(path.name, text)
        if bound.read_bytes(path.name) != text.encode("utf-8"):
            raise OSError("contract readback mismatch")
        bound.assert_canonical()

    try:
        if namespace is not None:
            publish(namespace)
        else:
            with BoundOutputNamespace.open(
                path.parent.parent, path.parent, path.parent.name
            ) as opened:
                publish(opened)
    except OSError as exc:
        raise ContractValidationError(f"cannot publish contract: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_contract(
    config: Any,
    plan: dict[str, Any] | None,
    *,
    stage_dir: Path | None = None,
    namespace: BoundOutputNamespace | None = None,
    authority_selection: MetricAuthoritySelection | None = None,
) -> ExperimentContract:
    experiment = getattr(config, "experiment", None)
    claim_scope = str(getattr(experiment, "claim_scope", "pipeline_validation") or "pipeline_validation")
    dataset_origin = str(getattr(experiment, "dataset_origin", "synthetic") or "synthetic")
    time_budget_sec = int(getattr(experiment, "time_budget_sec", 300) or 300)
    smoke_budget_sec = max(1, min(60, time_budget_sec))
    metric_key = str(getattr(experiment, "metric_key", "primary_metric") or "primary_metric")
    metric_direction = str(getattr(experiment, "metric_direction", "minimize") or "minimize")
    topic = str(getattr(getattr(config, "research", None), "topic", "") or "")
    experiment_mode = str(getattr(experiment, "mode", "") or "")
    try:
        authority = authority_selection or select_metric_authority(
            topic, experiment_mode
        )
        if stage_dir is not None:
            publish_metric_authority_snapshots(
                stage_dir, authority, namespace=namespace
            )
    except MetricAuthorityError as exc:
        raise ContractValidationError(f"metric authority selection failed: {exc}") from exc
    is_domain_evaluator = authority.schema_version == 2
    dataset_name = (
        "controlled_synthetic_iscas85_trojan_localization_v1"
        if is_domain_evaluator
        else (
            "synthetic_pipeline_validation_v1"
            if dataset_origin == "synthetic"
            else _first_dataset_name(plan)
        )
    )
    contract = ExperimentContract(
        schema_version=3 if is_domain_evaluator else 2,
        topic=topic,
        claim_scope=claim_scope,
        dataset_origin=dataset_origin,
        dataset_name=dataset_name,
        primary_metric={
            "key": "auprc" if is_domain_evaluator else metric_key,
            "direction": "maximize" if is_domain_evaluator else metric_direction,
            "minimum_valid_value": 0.0,
        },
        smoke_budget_sec=smoke_budget_sec,
        run_budget_sec=time_budget_sec,
        allowed_inputs=[],
        allowed_outputs=[{
            "path": "score_evidence.jsonl" if is_domain_evaluator else "results.json",
            "required": True,
        }],
        evaluator={
            "command": (
                "python evaluator/evaluator_main.py"
                if is_domain_evaluator
                else "python main.py"
            ),
            "owner": "domain_evaluator" if is_domain_evaluator else "scaffold",
            "timeout_sec": time_budget_sec,
            "required_result_keys": (
                [] if is_domain_evaluator else ["dataset_origin", "metrics"]
            ),
        },
        safety={
            "network": "none",
            "env_policy": getattr(getattr(experiment, "sandbox", None), "env_policy", "allowlist"),
            "evidence_policy": (
                "stage12_independent_verifier_only"
                if is_domain_evaluator
                else "stage12_recomputed_only"
            ),
        },
        sealing={
            "candidate_manifest": "selected_candidate_manifest.json",
            "content_hash_algorithm": "sha256",
        },
        metric_authority=authority.contract_identity(),
        metric_units=authority.metric_units,
        metric_display_labels=authority.metric_display_labels,
        evaluator_authority=authority.evaluator_authority,
    )
    return validate_contract_dict(
        contract.to_dict(), authority_selection=authority
    )


def find_stage09_contract(run_dir: Path) -> Path | None:
    direct = run_dir / "stage-09" / "experiment_contract.yaml"
    if _contract_exists_bound(direct):
        return direct
    if any(
        (direct.parent / marker).exists()
        for marker in ("decision.json", "plan_meta.json", "stage_health.json")
    ):
        return None
    candidates: list[Path] = []
    for path in run_dir.glob("stage-09_v*/experiment_contract.yaml"):
        if _contract_exists_bound(path):
            candidates.append(path)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: _stage09_version(p.parent.name), reverse=True)[0]


def _stage09_version(name: str) -> int:
    if "_v" not in name:
        return 0
    try:
        return int(name.rsplit("_v", 1)[1])
    except ValueError:
        return -1


def _int_value(value: Any) -> int | None:
    return value if type(value) is int else None


def _require_exact_fields(
    value: dict[str, Any],
    expected: set[str],
    label: str,
    errors: list[str],
) -> None:
    if set(value) != expected:
        errors.append(f"{label} fields must match policy exactly")


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.YAMLError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _read_contract_bytes(
    path: Path, *, namespace: BoundOutputNamespace | None = None
) -> bytes:
    if namespace is not None:
        if namespace.stage_dir != path.parent:
            raise ContractValidationError(
                "contract namespace does not match contract directory"
            )
        try:
            return namespace.read_bytes(path.name)
        except OSError as exc:
            raise ContractValidationError(
                f"contract path is missing or unsafe: {exc}"
            ) from exc
    try:
        with BoundOutputNamespace.open(
            path.parent.parent, path.parent, path.parent.name
        ) as namespace:
            content = namespace.read_bytes(path.name)
            namespace.assert_canonical()
            return content
    except OSError as exc:
        raise ContractValidationError(f"contract path is missing or unsafe: {exc}") from exc


def _contract_exists_bound(path: Path) -> bool:
    try:
        _read_contract_bytes(path)
    except ContractValidationError:
        return False
    return True


def _first_dataset_name(plan: dict[str, Any] | None) -> str | None:
    if not isinstance(plan, dict):
        return None
    datasets = plan.get("datasets")
    if isinstance(datasets, list) and datasets:
        first = datasets[0]
        if isinstance(first, dict):
            name = first.get("name") or first.get("dataset")
            return str(name).strip() if name else None
        return str(first).strip() or None
    if isinstance(datasets, dict) and datasets:
        key = next(iter(datasets.keys()))
        return str(key).strip() or None
    if isinstance(datasets, str):
        return datasets.strip() or None
    return None
