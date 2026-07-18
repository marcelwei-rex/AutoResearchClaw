"""Fixed-evaluator Stage 13 authority for Stage 12 domain results."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from researchclaw.config import RCConfig
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalRefinementController,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    _exact_keys,
    _file_ref_v2,
    _parse_object,
    _required_string,
    _sha256,
    canonical_authority_json_text,
    parse_experiment_result_set,
    validate_experiment_result_set,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stages import Stage, StageStatus


class Stage13DomainEvaluatorError(RuntimeError):
    """Raised when a fixed-evaluator refinement manifest is not replayable."""


_V2_REFINEMENT_FIELDS = {
    "schema_version",
    "refinement_policy_version",
    "result_set_type",
    "baseline_manifest",
    "experiment_contract",
    "sealed_candidate_manifest",
    "capture_manifest",
    "execution_policy",
    "observations",
    "run_config",
    "config_semantic_policy_version",
    "config_semantic_sha256",
    "claim_scope",
    "dataset_origin",
    "dataset_name",
    "evaluator_schema",
    "metric_authority",
    "refinement_mode",
    "primary_metric",
    "iterations",
    "selected_result",
}


def _stage13_uses_domain_evaluator_under_controller(
    controller: CanonicalRefinementController,
) -> bool:
    """Read the Stage 12 discriminator only after Stage 13 invalidation."""

    try:
        text = controller.read_run_file("stage-12/experiment_result_set.json").decode(
            "utf-8"
        )
    except (FileNotFoundError, UnicodeDecodeError, OSError) as exc:
        raise Stage13DomainEvaluatorError(
            "Stage 12 result set is missing or not UTF-8"
        ) from exc
    manifest = parse_experiment_result_set(text)
    controller.assert_canonical()
    return manifest["schema_version"] == Decimal(2)


def execute_domain_evaluator_stage13(
    *,
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
) -> StageResult:
    """Publish the no-refinement Stage 13 result for a fixed evaluator."""

    require_canonical_evidence_capabilities("stage13.domain_evaluator_execute")
    controller: CanonicalRefinementController | None = None
    try:
        controller = CanonicalRefinementController.prepare_generation(run_dir, stage_dir)
        return _execute_domain_evaluator_stage13_under_controller(
            controller=controller, run_dir=run_dir, config=config
        )
    finally:
        if controller is not None:
            controller.close()


def _execute_domain_evaluator_stage13_under_controller(
    *,
    controller: CanonicalRefinementController,
    run_dir: Path,
    config: RCConfig,
) -> StageResult:
    """Publish under a writer epoch already invalidated by Stage 13 dispatch."""

    try:
        baseline_bytes = controller.read_run_file("stage-12/experiment_result_set.json")
        try:
            baseline_text = baseline_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stage13DomainEvaluatorError("Stage 12 result set is not UTF-8") from exc
        baseline = validate_experiment_result_set(run_dir, config, baseline_text)
        if baseline["schema_version"] != Decimal(2):
            raise Stage13DomainEvaluatorError("Stage 12 is not a domain evaluator result")

        payload = _build_refinement_payload(controller, baseline_bytes, baseline)
        text = canonical_authority_json_text(payload)
        _validate_domain_evaluator_refinement_payload(
            text, baseline_bytes=baseline_bytes, baseline=baseline
        )
        controller.write_text_atomic("refinement_result_set.json", text)
        validate_domain_evaluator_refinement_result_set(run_dir, config)
        controller.assert_canonical()
        return StageResult(
            stage=Stage.ITERATIVE_REFINE,
            status=StageStatus.DONE,
            artifacts=("refinement_result_set.json",),
            evidence_refs=("stage-13/refinement_result_set.json",),
        )
    except Exception as exc:  # noqa: BLE001
        cleanup_error = ""
        try:
            controller.remove_tree_entries(("refinement_result_set.json",))
        except OSError as cleanup_exc:
            cleanup_error = f"; canonical cleanup failed: {cleanup_exc}"
        return StageResult(
            stage=Stage.ITERATIVE_REFINE,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Fixed-evaluator Stage 13 publication failed: {exc}{cleanup_error}",
        )


def parse_domain_evaluator_refinement_result_set(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 13 domain refinement result set")
    _exact_keys(payload, _V2_REFINEMENT_FIELDS, "Stage 13 domain refinement result set")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
        raise CanonicalExperimentEvidenceError("Stage 13 domain schema mismatch")
    if (
        type(payload["refinement_policy_version"]) is not int
        or payload["refinement_policy_version"] != 2
        or payload["result_set_type"] != "stage13_refinement"
    ):
        raise CanonicalExperimentEvidenceError("Stage 13 domain policy mismatch")
    for field in (
        "baseline_manifest",
        "experiment_contract",
        "sealed_candidate_manifest",
        "capture_manifest",
        "execution_policy",
        "observations",
        "run_config",
    ):
        _file_ref_v2(payload[field], field)
    if payload["baseline_manifest"]["path"] != "stage-12/experiment_result_set.json":
        raise CanonicalExperimentEvidenceError("noncanonical Stage 12 baseline path")
    expected_paths = {
        "experiment_contract": "stage-09/experiment_contract.yaml",
        "sealed_candidate_manifest": "stage-10/selected_candidate_manifest.json",
        "capture_manifest": "stage-10/evaluator-capture-v1/capture-manifest.json",
        "execution_policy": "stage-09/domain_evaluator_execution_policy.json",
        "observations": "stage-12/evidence-v2/observations.json",
    }
    for field, expected in expected_paths.items():
        if payload[field]["path"] != expected:
            raise CanonicalExperimentEvidenceError(f"noncanonical Stage 13 {field} path")
    if type(payload["config_semantic_policy_version"]) is not int or payload[
        "config_semantic_policy_version"
    ] != 1:
        raise CanonicalExperimentEvidenceError("unsupported config semantic policy")
    _sha256(payload["config_semantic_sha256"], "config_semantic_sha256")
    for field in ("claim_scope", "dataset_origin", "dataset_name", "evaluator_schema"):
        _required_string(payload[field], field)
    if not isinstance(payload["metric_authority"], dict):
        raise CanonicalExperimentEvidenceError("metric_authority must be an object")
    if payload["refinement_mode"] != "fixed_evaluator_no_refine":
        raise CanonicalExperimentEvidenceError("Stage 13 must use fixed evaluator mode")
    _validate_primary_metric(payload["primary_metric"])
    if payload["iterations"] != []:
        raise CanonicalExperimentEvidenceError("fixed evaluator Stage 13 has iterations")
    if payload["selected_result"] != {"type": "baseline", "iteration_id": None}:
        raise CanonicalExperimentEvidenceError("fixed evaluator Stage 13 selection mismatch")
    return payload


def validate_domain_evaluator_refinement_result_set(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Replay a fixed-evaluator Stage 13 manifest under one reader epoch."""

    require_canonical_evidence_capabilities("stage13.domain_evaluator_replay")
    with ReleaseGraphLock.acquire(
        run_dir, "stage13.domain_evaluator_replay", mode="read"
    ) as lease:
        with lease.open_stage_namespace("stage-13") as namespace:
            first = _capture_stage13_v2_authority(namespace)
            payload = parse_domain_evaluator_refinement_result_set(
                first["manifest"].decode("utf-8")
            )
            baseline_bytes = first["baseline"]
            try:
                baseline_text = baseline_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise Stage13DomainEvaluatorError("Stage 12 result set is not UTF-8") from exc
            baseline = validate_experiment_result_set(run_dir, config, baseline_text)
            if baseline["schema_version"] != Decimal(2):
                raise Stage13DomainEvaluatorError("Stage 13 domain result requires Stage 12 v2")
            _validate_payload_against_baseline(payload, baseline_bytes, baseline)
            second = _capture_stage13_v2_authority(namespace)
            if second != first:
                raise Stage13DomainEvaluatorError(
                    "Stage 12-13 authority changed during semantic replay"
                )
            namespace.assert_canonical()
            lease.assert_canonical()
            return payload


def is_domain_evaluator_refinement_on_disk(run_dir: Path) -> bool:
    """Read only the in-epoch discriminator for public replay dispatch.

    This performs no authority replay and returns no artifact data.  The v2
    branch immediately enters ``validate_domain_evaluator_refinement_result_set``
    where the capability guard runs before its complete semantic replay.
    """

    with ReleaseGraphLock.acquire(
        run_dir, "stage13.domain_evaluator_replay_dispatch", mode="read"
    ) as lease:
        with lease.open_stage_namespace("stage-13") as namespace:
            text = namespace.read_bytes("refinement_result_set.json").decode("utf-8")
            payload = _parse_object(text, "Stage 13 refinement result set")
            namespace.assert_canonical()
            lease.assert_canonical()
            return type(payload.get("schema_version")) is int and payload[
                "schema_version"
            ] == 2


def _capture_stage13_v2_authority(
    namespace: BoundOutputNamespace,
) -> dict[str, object]:
    entries = namespace.direct_entries()
    if entries != ("refinement_result_set.json",):
        raise Stage13DomainEvaluatorError("Stage 13 v2 namespace is not exact")
    return {
        "entries": entries,
        "manifest": namespace.read_bytes("refinement_result_set.json"),
        "baseline": namespace.read_run_file("stage-12/experiment_result_set.json"),
    }


def _validate_domain_evaluator_refinement_payload(
    text: str,
    *,
    baseline_bytes: bytes,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    payload = parse_domain_evaluator_refinement_result_set(text)
    _validate_payload_against_baseline(payload, baseline_bytes, baseline)
    return payload


def _build_refinement_payload(
    controller: CanonicalRefinementController,
    baseline_bytes: bytes,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    def ref(path: str) -> dict[str, Any]:
        content = controller.read_run_file(path)
        return {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }

    return {
        "schema_version": 2,
        "refinement_policy_version": 2,
        "result_set_type": "stage13_refinement",
        "baseline_manifest": {
            "path": "stage-12/experiment_result_set.json",
            "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "size": len(baseline_bytes),
        },
        "experiment_contract": ref(baseline["experiment_contract"]["path"]),
        "sealed_candidate_manifest": ref(
            baseline["sealed_candidate_manifest"]["path"]
        ),
        "capture_manifest": ref(baseline["capture_manifest"]["path"]),
        "execution_policy": ref(baseline["execution_policy"]["path"]),
        "observations": ref(baseline["observations"]["path"]),
        "run_config": ref(baseline["run_config"]["path"]),
        "config_semantic_policy_version": 1,
        "config_semantic_sha256": baseline["config_semantic_sha256"],
        "claim_scope": baseline["claim_scope"],
        "dataset_origin": baseline["dataset_origin"],
        "dataset_name": baseline["dataset_name"],
        "evaluator_schema": baseline["evaluator_schema"],
        "metric_authority": baseline["metric_authority"],
        "refinement_mode": "fixed_evaluator_no_refine",
        "primary_metric": baseline["primary_metric"],
        "iterations": [],
        "selected_result": {"type": "baseline", "iteration_id": None},
    }


def _validate_payload_against_baseline(
    payload: Mapping[str, Any],
    baseline_bytes: bytes,
    baseline: Mapping[str, Any],
) -> None:
    expected_refs = {
        "baseline_manifest": {
            "path": "stage-12/experiment_result_set.json",
            "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "size": len(baseline_bytes),
        },
        "experiment_contract": baseline["experiment_contract"],
        "sealed_candidate_manifest": baseline["sealed_candidate_manifest"],
        "capture_manifest": baseline["capture_manifest"],
        "execution_policy": baseline["execution_policy"],
        "observations": baseline["observations"],
        "run_config": baseline["run_config"],
    }
    for field, expected in expected_refs.items():
        if not _same_ref(payload[field], expected):
            raise Stage13DomainEvaluatorError(f"Stage 13 {field} binding mismatch")
    for field in (
        "config_semantic_policy_version",
        "config_semantic_sha256",
        "claim_scope",
        "dataset_origin",
        "dataset_name",
        "evaluator_schema",
    ):
        if payload[field] != baseline[field]:
            raise Stage13DomainEvaluatorError(f"Stage 13 {field} binding mismatch")
    if canonical_authority_json_text(payload["metric_authority"]) != canonical_authority_json_text(
        baseline["metric_authority"]
    ):
        raise Stage13DomainEvaluatorError("Stage 13 metric authority binding mismatch")
    if canonical_authority_json_text(payload["primary_metric"]) != canonical_authority_json_text(
        baseline["primary_metric"]
    ):
        raise Stage13DomainEvaluatorError("Stage 13 primary metric binding mismatch")


def _same_ref(actual: object, expected: object) -> bool:
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        return False
    try:
        return (
            actual["path"] == expected["path"]
            and actual["sha256"] == expected["sha256"]
            and actual["size"] == expected["size"]
        )
    except KeyError:
        return False


def _validate_primary_metric(value: object) -> None:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("primary_metric must be an object")
    _exact_keys(
        value,
        {"condition", "key", "observation_set", "aggregation", "value"},
        "primary_metric",
    )
    for field in ("condition", "key", "observation_set", "aggregation"):
        _required_string(value[field], f"primary_metric.{field}")
    metric_value = value["value"]
    if isinstance(metric_value, bool) or not isinstance(metric_value, (int, Decimal)):
        raise CanonicalExperimentEvidenceError("primary_metric.value must be numeric")
    if isinstance(metric_value, Decimal) and not metric_value.is_finite():
        raise CanonicalExperimentEvidenceError("primary_metric.value must be finite")
