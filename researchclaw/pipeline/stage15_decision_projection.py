"""Deterministic Stage 15 decision projection for fixed domain evaluators."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import yaml

from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
)


DECISION_PROJECTION_SCHEMA_VERSION = 1
DECISION_POLICY_VERSION = "domain_evaluator_decision_v1"

_METRIC_KEYS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)


class Stage15DecisionProjectionError(ValueError):
    """Raised when a fixed-domain decision projection cannot be reconstructed."""


@dataclass(frozen=True)
class Stage15DecisionProjection:
    schema_version: int
    policy_version: str
    content: bytes
    sha256: str

    @property
    def prompt_text(self) -> str:
        return self.content.decode("utf-8")


def uses_fixed_domain_decision_policy(evidence: CanonicalExperimentEvidence) -> bool:
    return (
        evidence.manifest.get("schema_version") == 2
        and evidence.manifest.get("generation_kind") == "domain_evaluator"
    )


def build_stage15_decision_projection(
    evidence: CanonicalExperimentEvidence,
) -> Stage15DecisionProjection:
    """Build one bounded projection using only an immutable accessor snapshot."""

    if not uses_fixed_domain_decision_policy(evidence):
        raise Stage15DecisionProjectionError(
            "fixed domain decision projection requires canonical schema v2"
        )
    root = evidence.manifest
    bindings = root.get("bindings")
    primary_metric = root.get("primary_metric")
    if not isinstance(bindings, Mapping) or not isinstance(primary_metric, Mapping):
        raise Stage15DecisionProjectionError("canonical domain bindings are invalid")

    observations = _parse_json_object(
        evidence.selected_execution_artifact.content,
        "canonical domain observations",
    )
    if (
        hashlib.sha256(evidence.selected_execution_artifact.content).hexdigest()
        != evidence.selected_execution_artifact.sha256
    ):
        raise Stage15DecisionProjectionError("observation artifact hash mismatch")
    expected_observation_fields = {
        "schema_version",
        "observation_policy_version",
        "dataset_capture_sha256",
        "score_evidence_sha256",
        "metric_keys",
        "observations",
        "per_seed",
        "aggregate",
        "primary_metric",
    }
    if set(observations) != expected_observation_fields:
        raise Stage15DecisionProjectionError(
            "canonical domain observation fields mismatch"
        )

    metric_keys = observations["metric_keys"]
    rows = observations["observations"]
    per_seed = observations["per_seed"]
    aggregate = observations["aggregate"]
    if (
        not isinstance(metric_keys, list)
        or tuple(metric_keys) != _METRIC_KEYS
        or not isinstance(rows, list)
        or not isinstance(per_seed, list)
        or not isinstance(aggregate, list)
    ):
        raise Stage15DecisionProjectionError(
            "canonical domain observation layout mismatch"
        )

    contract = _parse_contract(evidence.experiment_contract_bytes)
    metric_units = contract.get("metric_units")
    contract_primary = contract.get("primary_metric")
    if (
        not isinstance(metric_units, dict)
        or set(metric_units) != set(metric_keys)
        or any(not isinstance(metric_units[key], str) or not metric_units[key] for key in metric_keys)
        or contract.get("claim_scope") != bindings.get("claim_scope")
        or contract.get("dataset_origin") != bindings.get("dataset_origin")
        or not isinstance(contract_primary, dict)
    ):
        raise Stage15DecisionProjectionError("contract decision projection is invalid")

    condition_values: set[str] = set()
    seed_values: set[int] = set()
    variant_values: set[tuple[str, str]] = set()
    group_counts: dict[tuple[str, int], int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise Stage15DecisionProjectionError("observation row must be an object")
        condition = row.get("condition")
        seed = row.get("seed")
        family = row.get("circuit_family")
        variant = row.get("circuit_variant")
        metrics = row.get("metrics")
        if (
            not isinstance(condition, str)
            or not condition
            or type(seed) is not int
            or not isinstance(family, str)
            or not family
            or not isinstance(variant, str)
            or not variant
            or not isinstance(metrics, dict)
            or set(metrics) != set(metric_keys)
        ):
            raise Stage15DecisionProjectionError("observation row identity is invalid")
        _require_metric_values(metrics, metric_keys, "observation row")
        condition_values.add(condition)
        seed_values.add(seed)
        variant_values.add((family, variant))
        group = (condition, seed)
        group_counts[group] = group_counts.get(group, 0) + 1

    condition_order = sorted(condition_values)
    seeds = sorted(seed_values)
    variants = sorted(variant_values)
    expected_groups = {(condition, seed) for condition in condition_order for seed in seeds}
    if (
        len(condition_order) != 3
        or seeds != [0, 1, 2]
        or len(variants) != 18
        or len(rows) != 162
        or set(group_counts) != expected_groups
        or set(group_counts.values()) != {18}
        or len(per_seed) != 9
        or len(aggregate) != 3
    ):
        raise Stage15DecisionProjectionError(
            "condition, seed, or variant closure is incomplete"
        )

    primary_condition = primary_metric.get("condition")
    primary_key = primary_metric.get("key")
    primary_direction = contract_primary.get("direction")
    if (
        primary_condition not in condition_order
        or primary_key not in metric_keys
        or dict(primary_metric) != observations["primary_metric"]
        or contract_primary.get("key") != primary_key
        or primary_direction not in {"maximize", "minimize"}
    ):
        raise Stage15DecisionProjectionError("primary metric binding is invalid")
    conditions = [
        {
            "condition_id": condition,
            "role": "proposed" if condition == primary_condition else "baseline",
        }
        for condition in condition_order
    ]
    if sum(item["role"] == "baseline" for item in conditions) != 2:
        raise Stage15DecisionProjectionError("baseline/proposed role closure mismatch")

    normalized_per_seed = _normalize_per_seed(
        per_seed, condition_order, seeds, metric_keys
    )
    normalized_aggregate = _normalize_aggregates(
        aggregate, condition_order, metric_keys
    )
    primary_values_by_seed = {
        seed: [
            item["metrics"][primary_key]
            for item in normalized_per_seed
            if item["seed"] == seed
        ]
        for seed in seeds
    }
    cross_condition_integrity = all(
        len(set(values)) == len(condition_order)
        for values in primary_values_by_seed.values()
    )
    if not cross_condition_integrity:
        raise Stage15DecisionProjectionError(
            "primary metric cross-condition integrity failed"
        )

    payload = {
        "schema_version": DECISION_PROJECTION_SCHEMA_VERSION,
        "policy_version": DECISION_POLICY_VERSION,
        "canonical_evidence": {
            "path": evidence.manifest_path,
            "sha256": evidence.manifest_sha256,
        },
        "claim_scope": bindings.get("claim_scope"),
        "dataset_origin": bindings.get("dataset_origin"),
        "evaluator_schema": bindings.get("evaluator_schema"),
        "conditions": conditions,
        "seeds": seeds,
        "variants": [
            {"circuit_family": family, "circuit_variant": variant}
            for family, variant in variants
        ],
        "metric_policy": [
            {
                "key": key,
                "unit": metric_units[key],
            }
            for key in metric_keys
        ],
        "per_seed": normalized_per_seed,
        "condition_aggregates": normalized_aggregate,
        "primary_metric": {
            **dict(primary_metric),
            "direction": primary_direction,
        },
        "observation_closure": {
            "path": evidence.selected_execution_artifact.path,
            "sha256": evidence.selected_execution_artifact.sha256,
            "observation_count": len(rows),
            "condition_seed_group_count": len(group_counts),
            "rows_per_group": 18,
        },
        "deterministic_gates": {
            "evidence_completeness": True,
            "condition_role_closure": True,
            "seed_closure": True,
            "variant_closure": True,
            "metric_registry_closure": True,
            "cross_condition_primary_metric_integrity": True,
        },
    }
    for field in ("claim_scope", "dataset_origin", "evaluator_schema"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise Stage15DecisionProjectionError(f"{field} is invalid")
    try:
        content = canonical_authority_json_text(payload).encode("utf-8")
    except CanonicalExperimentEvidenceError as exc:
        raise Stage15DecisionProjectionError(str(exc)) from exc
    return Stage15DecisionProjection(
        schema_version=DECISION_PROJECTION_SCHEMA_VERSION,
        policy_version=DECISION_POLICY_VERSION,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def fixed_domain_decision_prompt(projection: Stage15DecisionProjection) -> tuple[str, str]:
    system = (
        "You are a research program lead evaluating a fixed, independently replayed "
        "domain-evaluator evidence projection."
    )
    user = (
        "Choose exactly one decision: PROCEED, PIVOT, or REFINE.\n\n"
        "For PROCEED, all deterministic_gates in the projection must be true, there "
        "must be exactly two baseline conditions and one proposed condition, each "
        "condition must contain three seeds over the same 18 variants, and all eight "
        "metric policies and bounded aggregates must be present. Do not require or "
        "invent a subjective analysis-quality score.\n\n"
        "Output markdown with sections ## Decision, ## Justification, ## Evidence, "
        "and ## Next Actions.\n\nCanonical decision projection:\n"
        + projection.prompt_text
    )
    return system, user


def _parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise Stage15DecisionProjectionError(f"duplicate {label} key: {key}")
            value[key] = child
        return value

    try:
        value = json.loads(
            content.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=lambda token: (_ for _ in ()).throw(
                Stage15DecisionProjectionError(f"non-finite {label} number: {token}")
            ),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage15DecisionProjectionError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage15DecisionProjectionError(f"{label} root must be an object")
    return value


def _parse_contract(content: bytes) -> dict[str, Any]:
    try:
        value = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise Stage15DecisionProjectionError(f"invalid experiment contract: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage15DecisionProjectionError("experiment contract root must be an object")
    return value


def _require_metric_values(metrics: dict[str, Any], keys: list[str], label: str) -> None:
    for key in keys:
        value = metrics[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, Decimal))
            or isinstance(value, Decimal) and not value.is_finite()
        ):
            raise Stage15DecisionProjectionError(f"{label} metric is not finite numeric")


def _normalize_per_seed(
    values: list[Any], conditions: list[str], seeds: list[int], metric_keys: list[str]
) -> list[dict[str, Any]]:
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"condition", "seed", "n_variants", "metrics"}:
            raise Stage15DecisionProjectionError("per-seed aggregate fields mismatch")
        condition = item["condition"]
        seed = item["seed"]
        metrics = item["metrics"]
        if (
            condition not in conditions
            or type(seed) is not int
            or seed not in seeds
            or type(item["n_variants"]) is not int
            or item["n_variants"] != 18
            or not isinstance(metrics, dict)
            or set(metrics) != set(metric_keys)
        ):
            raise Stage15DecisionProjectionError("per-seed aggregate is invalid")
        _require_metric_values(metrics, metric_keys, "per-seed aggregate")
        identity = (condition, seed)
        if identity in indexed:
            raise Stage15DecisionProjectionError("duplicate per-seed aggregate")
        indexed[identity] = item
    expected = {(condition, seed) for condition in conditions for seed in seeds}
    if set(indexed) != expected:
        raise Stage15DecisionProjectionError("per-seed aggregate closure mismatch")
    return [indexed[(condition, seed)] for condition in conditions for seed in seeds]


def _normalize_aggregates(
    values: list[Any], conditions: list[str], metric_keys: list[str]
) -> list[dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    summary_fields = {"mean", "std", "min", "max"}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"condition", "n_seeds", "metrics"}:
            raise Stage15DecisionProjectionError("condition aggregate fields mismatch")
        condition = item["condition"]
        metrics = item["metrics"]
        if (
            condition not in conditions
            or condition in indexed
            or type(item["n_seeds"]) is not int
            or item["n_seeds"] != 3
            or not isinstance(metrics, dict)
            or set(metrics) != set(metric_keys)
        ):
            raise Stage15DecisionProjectionError("condition aggregate is invalid")
        for key in metric_keys:
            summary = metrics[key]
            if not isinstance(summary, dict) or set(summary) != summary_fields:
                raise Stage15DecisionProjectionError("metric aggregate fields mismatch")
            _require_metric_values(summary, list(summary_fields), "condition aggregate")
        indexed[condition] = item
    if set(indexed) != set(conditions):
        raise Stage15DecisionProjectionError("condition aggregate closure mismatch")
    return [indexed[condition] for condition in conditions]
