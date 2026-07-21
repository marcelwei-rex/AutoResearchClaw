"""Fixed Stage 14 candidate and root authority for domain evaluators."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from researchclaw.config import RCConfig
from researchclaw.literature.citation_policy import (
    ConfigSnapshotNamespaceInputs,
    replay_config_snapshot_namespace,
)
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalAnalysisController,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalEvidenceArtifact,
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    CanonicalProjectArtifact,
    _CanonicalPublicationPlan,
    _exact_keys,
    _file_ref_v2,
    _freeze_authority_value,
    _parse_json_value,
    _parse_object,
    _required_string,
    _sha256,
    canonical_authority_json_text,
    canonical_decimal,
    parse_experiment_result_set,
    semantic_config_sha256,
    sha256_text,
    validate_experiment_result_set,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stage13_domain_evaluator import (
    Stage13DomainEvaluatorError,
    parse_domain_evaluator_refinement_result_set,
    validate_domain_evaluator_refinement_result_set,
)
from researchclaw.pipeline.stages import Stage, StageStatus


class Stage14DomainEvaluatorError(RuntimeError):
    """Raised when a domain-evaluator Stage 14 publication is not replayable."""


_CANDIDATE_FIELDS = {
    "schema_version",
    "candidate_policy_version",
    "candidate_id",
    "bindings",
    "selected_result",
    "observation_authority",
    "primary_metric",
    "artifacts",
}
_ROOT_FIELDS = {
    "schema_version",
    "selection_policy_version",
    "generation_kind",
    "bindings",
    "selected_result",
    "observation_authority",
    "primary_metric",
    "selected_candidate",
    "selected_summary",
    "selected_analysis",
}
_BINDING_REFS = (
    "experiment_contract",
    "sealed_candidate_manifest",
    "capture_manifest",
    "package_manifest",
    "execution_policy",
    "stage12_result_set",
    "stage13_refinement",
    "run_config",
)
_BINDING_FIELDS = {
    *_BINDING_REFS,
    "config_semantic_policy_version",
    "config_semantic_sha256",
    "claim_scope",
    "dataset_origin",
    "dataset_name",
    "evaluator_schema",
    "metric_authority",
}
_OBSERVATION_REFS = (
    "invocation_journal",
    "score_evidence_1",
    "score_evidence_2",
    "observations",
    "results",
)
_OBSERVATION_FIELDS = {*_OBSERVATION_REFS, "observation_policy_version"}
_ARTIFACT_LAYOUT = (
    ("analysis", "analysis.md"),
    ("figure_plan", "figure_plan.json"),
    ("results_table", "results_table.tex"),
    ("summary", "experiment_summary.json"),
)
_ROOT_OWNED = (
    "canonical_experiment_evidence.json",
    "experiment_summary_best.json",
    "analysis_best.md",
)
_DOMAIN_CONDITIONS = (
    "raw_cc1",
    "scoap_isolation_forest",
    "trojnet_community_graphsage",
)
_DOMAIN_SEEDS = (0, 1, 2)
_DOMAIN_FAMILIES = ("c1355", "c1908", "c3540", "c432", "c6288", "c880")
_DOMAIN_VARIANTS = tuple(
    (family, f"{family}_ht{variant}")
    for family in _DOMAIN_FAMILIES
    for variant in (1, 2, 3)
)
_DOMAIN_METRIC_KEYS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)
_DOMAIN_OBSERVATION_IDENTITIES = {
    (family, variant, condition, seed)
    for condition in _DOMAIN_CONDITIONS
    for seed in _DOMAIN_SEEDS
    for family, variant in _DOMAIN_VARIANTS
}


@dataclass(frozen=True)
class _DomainStage14Snapshot:
    stage_entries: tuple[str, ...]
    candidate_tree: tuple[tuple[str, bytes], ...]
    candidate_collections: tuple[tuple[str, tuple[tuple[str, bytes], ...]], ...]
    root_entries: tuple[str, ...]
    root_manifest: bytes | None
    summary_copy: bytes | None
    analysis_copy: bytes | None
    stage12_manifest: bytes
    stage13_manifest: bytes
    stage10_capture_tree: tuple[tuple[str, bytes], ...]
    run_files: tuple[tuple[str, bytes], ...]
    config_entries: tuple[tuple[str, bytes], ...]


def _stage14_uses_domain_evaluator_under_controller(
    controller: CanonicalAnalysisController,
) -> bool:
    """Dispatch on Stage 12, then require the matching Stage 13 generation."""

    try:
        baseline_text = controller.read_run_file(
            "stage-12/experiment_result_set.json"
        ).decode(
            "utf-8"
        )
    except (FileNotFoundError, UnicodeDecodeError, OSError) as exc:
        raise Stage14DomainEvaluatorError(
            "Stage 12 result set is missing or not UTF-8"
        ) from exc
    baseline = parse_experiment_result_set(baseline_text)
    if baseline["schema_version"] != Decimal(2):
        controller.assert_canonical()
        return False
    try:
        text = controller.read_run_file("stage-13/refinement_result_set.json").decode(
            "utf-8"
        )
    except (FileNotFoundError, UnicodeDecodeError, OSError) as exc:
        raise Stage14DomainEvaluatorError(
            "Stage 13 refinement result set is missing or not UTF-8"
        ) from exc
    payload = _parse_object(text, "Stage 13 refinement result set")
    controller.assert_canonical()
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise Stage14DomainEvaluatorError("Stage 12/13 domain schema mismatch")
    return True


def execute_domain_evaluator_stage14(
    *,
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
) -> StageResult:
    """Public fixed-evaluator Stage 14 producer."""

    require_canonical_evidence_capabilities("stage14.domain_evaluator_execute")
    controller: CanonicalAnalysisController | None = None
    try:
        controller = CanonicalAnalysisController.prepare_generation(run_dir, stage_dir)
        return _execute_domain_evaluator_stage14_under_controller(
            controller=controller, run_dir=run_dir, config=config
        )
    finally:
        if controller is not None:
            controller.close()


def _execute_domain_evaluator_stage14_under_controller(
    *,
    controller: CanonicalAnalysisController,
    run_dir: Path,
    config: RCConfig,
) -> StageResult:
    """Publish v2 candidate and root authority under one writer epoch."""

    candidate_id: str | None = None
    published_candidate = False
    try:
        source = _load_domain_stage14_source(
            run_dir, config, controller=controller
        )
        staging = controller.create_candidate_staging()
        try:
            artifact_bytes = _render_fixed_artifacts(source["primary_metric"])
            for _role, relative in _ARTIFACT_LAYOUT:
                (staging / relative).write_bytes(artifact_bytes[relative])
            payload = _build_candidate_payload(source, artifact_bytes)
            candidate_id = payload["candidate_id"]
            (staging / "experiment_evidence_candidate.json").write_text(
                canonical_authority_json_text(payload), encoding="utf-8"
            )
            staged_tree = _tree_from_directory(staging)
            _validate_candidate_tree(payload, staged_tree)
            if not _candidate_matches_source(payload, source):
                raise Stage14DomainEvaluatorError("Stage 14 candidate source mismatch")
            existing = _candidate_tree_by_id(
                controller.read_candidate_tree(), candidate_id
            )
            if existing is None:
                controller.publish_candidate_tree(candidate_id, staging)
                published_candidate = True
            else:
                _validate_candidate_tree(payload, existing)
                if existing != staged_tree:
                    raise Stage14DomainEvaluatorError(
                        "Stage 14 domain candidate ID collision during publication"
                    )
        finally:
            _remove_staging_tree(staging)

        _publish_domain_evaluator_canonical_manifest_under_generation_controller(
            controller, run_dir, config, source=source
        )
        validate_domain_evaluator_canonical_manifest(run_dir, config)
        controller.assert_canonical()
        return StageResult(
            stage=Stage.RESULT_ANALYSIS,
            status=StageStatus.DONE,
            artifacts=(
                "evidence_candidates/"
                + candidate_id
                + "/experiment_evidence_candidate.json",
            ),
            evidence_refs=(
                "stage-14/evidence_candidates/"
                + candidate_id
                + "/experiment_evidence_candidate.json",
                "canonical_experiment_evidence.json",
            ),
            decision=f"canonical_domain_candidate:{candidate_id}",
        )
    except Exception as exc:  # noqa: BLE001
        cleanup_errors: list[str] = []
        if candidate_id is not None and published_candidate:
            try:
                controller.remove_candidate_tree(candidate_id)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"candidate cleanup failed: {cleanup_exc}")
        try:
            controller.remove_run_files(_ROOT_OWNED)
        except OSError as cleanup_exc:
            cleanup_errors.append(f"root cleanup failed: {cleanup_exc}")
        suffix = f"; {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        return StageResult(
            stage=Stage.RESULT_ANALYSIS,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Fixed-evaluator Stage 14 publication failed: {exc}{suffix}",
        )


def parse_domain_evaluator_candidate(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "Stage 14 domain evidence candidate")
    _exact_keys(payload, _CANDIDATE_FIELDS, "Stage 14 domain evidence candidate")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
        raise CanonicalExperimentEvidenceError("Stage 14 domain candidate schema mismatch")
    if (
        type(payload["candidate_policy_version"]) is not int
        or payload["candidate_policy_version"] != 2
    ):
        raise CanonicalExperimentEvidenceError("Stage 14 domain candidate policy mismatch")
    _candidate_id(payload["candidate_id"])
    _validate_bindings(payload["bindings"])
    if payload["selected_result"] != {"type": "baseline", "iteration_id": None}:
        raise CanonicalExperimentEvidenceError("Stage 14 domain candidate selection mismatch")
    _validate_observation_authority(payload["observation_authority"])
    _validate_primary_metric(payload["primary_metric"])
    _validate_artifact_refs(payload["artifacts"])
    if _candidate_id_for_payload(payload) != payload["candidate_id"]:
        raise CanonicalExperimentEvidenceError("Stage 14 domain candidate identity mismatch")
    return payload


def parse_domain_evaluator_canonical_manifest(text: str) -> dict[str, Any]:
    payload = _parse_object(text, "canonical domain experiment evidence manifest")
    _exact_keys(payload, _ROOT_FIELDS, "canonical domain experiment evidence manifest")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
        raise CanonicalExperimentEvidenceError("canonical domain root schema mismatch")
    if (
        type(payload["selection_policy_version"]) is not int
        or payload["selection_policy_version"] != 2
        or payload["generation_kind"] != "domain_evaluator"
    ):
        raise CanonicalExperimentEvidenceError("canonical domain root policy mismatch")
    _validate_bindings(payload["bindings"])
    if payload["selected_result"] != {"type": "baseline", "iteration_id": None}:
        raise CanonicalExperimentEvidenceError("canonical domain root selection mismatch")
    _validate_observation_authority(payload["observation_authority"])
    _validate_primary_metric(payload["primary_metric"])
    candidate = payload["selected_candidate"]
    if not isinstance(candidate, dict):
        raise CanonicalExperimentEvidenceError("selected_candidate must be an object")
    _exact_keys(candidate, {"candidate_id", "manifest"}, "selected_candidate")
    _candidate_id(candidate["candidate_id"])
    _file_ref_v2(candidate["manifest"], "selected_candidate.manifest")
    for field, expected_source, expected_copy in (
        ("selected_summary", "experiment_summary.json", "experiment_summary_best.json"),
        ("selected_analysis", "analysis.md", "analysis_best.md"),
    ):
        value = payload[field]
        if not isinstance(value, dict):
            raise CanonicalExperimentEvidenceError(f"{field} must be an object")
        _exact_keys(value, {"source", "canonical_copy"}, field)
        source = _file_ref_v2(value["source"], f"{field}.source")
        copy = _file_ref_v2(value["canonical_copy"], f"{field}.canonical_copy")
        if source["path"].rsplit("/", 1)[-1] != expected_source:
            raise CanonicalExperimentEvidenceError(f"invalid {field} source path")
        if copy["path"] != expected_copy:
            raise CanonicalExperimentEvidenceError(f"invalid {field} compatibility path")
    return payload


def validate_domain_evaluator_candidate(candidate_root: Path) -> dict[str, Any]:
    """Replay one stored v2 candidate from disk without a text-override path."""

    require_canonical_evidence_capabilities(
        "stage14.domain_evaluator_candidate_replay"
    )
    root = candidate_root.absolute()
    if root.parent.name != "evidence_candidates":
        raise CanonicalExperimentEvidenceError(
            "Stage 14 domain candidate is outside evidence_candidates"
        )
    stage_dir = root.parent.parent
    if re.fullmatch(r"stage-14(?:_v[1-9]\d*)?", stage_dir.name) is None:
        raise CanonicalExperimentEvidenceError(
            "Stage 14 domain candidate has an invalid generation path"
        )
    run_dir = stage_dir.parent
    candidate_id = root.name
    with ReleaseGraphLock.acquire(
        run_dir, "stage14.domain_evaluator_candidate_replay", mode="read"
    ) as lease:
        with lease.open_stage_namespace(stage_dir.name) as namespace:
            first_entries, first_tree = _capture_single_candidate_collection(namespace)
            candidates = _candidates_from_tree(first_tree)
            try:
                payload, _tree = candidates[candidate_id]
            except KeyError as exc:
                raise CanonicalExperimentEvidenceError(
                    "Stage 14 domain candidate is missing from held namespace"
                ) from exc
            if payload["candidate_id"] != candidate_id:
                raise CanonicalExperimentEvidenceError(
                    "Stage 14 domain candidate directory identity mismatch"
                )
            second_entries, second_tree = _capture_single_candidate_collection(namespace)
            if (second_entries, second_tree) != (first_entries, first_tree):
                raise Stage14DomainEvaluatorError(
                    "Stage 14 domain candidate changed during replay"
                )
            namespace.assert_canonical()
            lease.assert_canonical()
            return payload


def _capture_single_candidate_collection(
    namespace,
) -> tuple[tuple[str, ...], tuple[tuple[str, bytes], ...]]:
    entries = namespace.direct_entries()
    if entries != ("evidence_candidates",):
        raise Stage14DomainEvaluatorError(
            "Stage 14 domain candidate namespace is not exact"
        )
    return entries, tuple(sorted(namespace.read_directory_tree("evidence_candidates").items()))


def validate_domain_evaluator_canonical_manifest(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Replay the complete v2 Stage 12-14 authority graph from disk snapshots."""

    require_canonical_evidence_capabilities("stage14.domain_evaluator_replay")
    with ReleaseGraphLock.acquire(
        run_dir, "stage14.domain_evaluator_replay", mode="read"
    ) as lease:
        with lease.open_stage_namespace("stage-14") as namespace:
            first = _capture_domain_stage14_snapshot(namespace, lease=lease)
            if first.root_manifest is None:
                raise Stage14DomainEvaluatorError("canonical domain root is missing")
            source = _load_domain_stage14_source(
                run_dir,
                config,
                controller=None,
                baseline_bytes=first.stage12_manifest,
                refinement_bytes=first.stage13_manifest,
                run_files=dict(first.run_files),
                config_entries=first.config_entries,
            )
            payload = parse_domain_evaluator_canonical_manifest(
                first.root_manifest.decode("utf-8")
            )
            _validate_root_snapshot(payload, first, source)
            second = _capture_domain_stage14_snapshot(namespace, lease=lease)
            if second != first:
                raise Stage14DomainEvaluatorError(
                    "Stage 12-14 authority changed during semantic replay"
                )
            namespace.assert_canonical()
            lease.assert_canonical()
            return payload


def load_domain_evaluator_canonical_evidence(
    run_dir: Path,
) -> CanonicalExperimentEvidence:
    """Return the immutable shared-accessor snapshot for a validated v2 root."""

    require_canonical_evidence_capabilities("stage14.domain_evaluator_accessor")
    with ReleaseGraphLock.acquire(
        run_dir, "stage14.domain_evaluator_accessor", mode="read"
    ) as lease:
        with lease.open_stage_namespace("stage-14") as namespace:
            first = _capture_domain_stage14_snapshot(namespace, lease=lease)
            if first.root_manifest is None:
                raise Stage14DomainEvaluatorError("canonical domain root is missing")
            root = parse_domain_evaluator_canonical_manifest(
                first.root_manifest.decode("utf-8")
            )
            config_bytes = dict(first.run_files)[root["bindings"]["run_config"]["path"]]
            config = _config_from_bytes(config_bytes, run_dir)
            source = _load_domain_stage14_source(
                run_dir,
                config,
                controller=None,
                baseline_bytes=first.stage12_manifest,
                refinement_bytes=first.stage13_manifest,
                run_files=dict(first.run_files),
                config_entries=first.config_entries,
            )
            _validate_root_snapshot(root, first, source)
            evidence = _snapshot_accessor(root, first, source, config_bytes)
            second = _capture_domain_stage14_snapshot(namespace, lease=lease)
            if second != first:
                raise Stage14DomainEvaluatorError(
                    "canonical domain bundle changed during access"
                )
            namespace.assert_canonical()
            lease.assert_canonical()
            return evidence


def build_expected_domain_evaluator_manifest(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Derive the v2 root without reading the stored root manifest."""

    return dict(build_domain_evaluator_publication_plan(run_dir, config).manifest)


def build_domain_evaluator_publication_plan(
    run_dir: Path,
    config: RCConfig,
) -> _CanonicalPublicationPlan:
    """Build the immutable v2 root plan without reading the stored root."""

    require_canonical_evidence_capabilities("stage14.domain_evaluator_expected")
    with ReleaseGraphLock.acquire(
        run_dir, "stage14.domain_evaluator_expected", mode="read"
    ) as lease:
        with lease.open_stage_namespace("stage-14") as namespace:
            snapshot = _capture_domain_stage14_snapshot(
                namespace, lease=lease, include_root=False
            )
            source = _load_domain_stage14_source(
                run_dir,
                config,
                controller=None,
                baseline_bytes=snapshot.stage12_manifest,
                refinement_bytes=snapshot.stage13_manifest,
                run_files=dict(snapshot.run_files),
                config_entries=snapshot.config_entries,
            )
            plan = _build_root_plan_from_snapshot(snapshot, source)
            second = _capture_domain_stage14_snapshot(
                namespace, lease=lease, include_root=False
            )
            if second != snapshot:
                raise Stage14DomainEvaluatorError(
                    "Stage 12-14 authority changed during expected-plan replay"
                )
            namespace.assert_canonical()
            lease.assert_canonical()
            return _CanonicalPublicationPlan(
                manifest=_freeze_authority_value(_parse_root_bytes(plan["manifest"])),
                summary_bytes=plan["summary"],
                analysis_bytes=plan["analysis"],
            )


def publish_domain_evaluator_canonical_manifest(
    run_dir: Path,
    config: RCConfig,
) -> dict[str, Any]:
    """Publish the v2 root under a dedicated Stage 14 writer epoch."""

    controller = CanonicalAnalysisController.acquire_promotion(run_dir)
    try:
        return _publish_domain_evaluator_canonical_manifest_under_controller(
            controller, run_dir, config
        )
    finally:
        controller.close()


def _publish_domain_evaluator_canonical_manifest_under_controller(
    controller: CanonicalAnalysisController,
    run_dir: Path,
    config: RCConfig,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_canonical_evidence_capabilities(
        "stage14.domain_evaluator_publish_under_controller"
    )
    if type(controller) is not CanonicalAnalysisController:
        raise Stage14DomainEvaluatorError(
            "untrusted Stage 14 promotion controller"
        )
    controller.require_active_promotion(run_dir)
    return _publish_domain_evaluator_canonical_manifest_core(
        controller, run_dir, config, source=source
    )


def _publish_domain_evaluator_canonical_manifest_under_generation_controller(
    controller: CanonicalAnalysisController,
    run_dir: Path,
    config: RCConfig,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_canonical_evidence_capabilities(
        "stage14.domain_evaluator_publish_under_generation_controller"
    )
    if type(controller) is not CanonicalAnalysisController:
        raise Stage14DomainEvaluatorError(
            "untrusted Stage 14 generation controller"
        )
    controller.require_active_generation(run_dir)
    return _publish_domain_evaluator_canonical_manifest_core(
        controller, run_dir, config, source=source
    )


def _publish_domain_evaluator_canonical_manifest_core(
    controller: CanonicalAnalysisController,
    run_dir: Path,
    config: RCConfig,
    *,
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    try:
        _clear_root_authority(controller)
        source = source or _load_domain_stage14_source(
            run_dir, config, controller=controller
        )
        plan = _build_root_plan_from_source(controller, source)
        controller.write_run_bytes_atomic(
            "experiment_summary_best.json", plan["summary"]
        )
        controller.write_run_bytes_atomic("analysis_best.md", plan["analysis"])
        controller.write_run_bytes_atomic(
            "canonical_experiment_evidence.json", plan["manifest"]
        )
        controller.assert_canonical()
        return validate_domain_evaluator_canonical_manifest(run_dir, config)
    except Exception as exc:
        try:
            _clear_root_authority(controller)
        except OSError as cleanup_exc:
            exc.add_note(f"Stage 14 root cleanup failed: {cleanup_exc}")
        raise


def _clear_root_authority(controller: CanonicalAnalysisController) -> None:
    """Invalidate every root commit point without short-circuiting cleanup."""

    errors: list[str] = []
    for name in _ROOT_OWNED:
        try:
            controller.remove_run_files((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise OSError("Stage 14 root invalidation incomplete: " + "; ".join(errors))


def _load_domain_stage14_source(
    run_dir: Path,
    config: RCConfig,
    *,
    controller: CanonicalAnalysisController | None,
    baseline_bytes: bytes | None = None,
    refinement_bytes: bytes | None = None,
    run_files: Mapping[str, bytes] | None = None,
    config_entries: tuple[tuple[str, bytes], ...] | None = None,
) -> dict[str, Any]:
    def read(path: str) -> bytes:
        if controller is not None:
            return controller.read_run_file(path)
        with ReleaseGraphLock.acquire(
            run_dir, "stage14.domain_evaluator_source", mode="read"
        ) as lease:
            with lease.open_stage_namespace("stage-14") as namespace:
                return namespace.read_run_file(path)

    def run_entries() -> tuple[str, ...]:
        if controller is not None:
            return controller.run_entries()
        with ReleaseGraphLock.acquire(
            run_dir, "stage14.domain_evaluator_source", mode="read"
        ) as lease:
            with lease.open_stage_namespace("stage-14") as namespace:
                return namespace.run_entries()

    baseline_bytes = baseline_bytes or read("stage-12/experiment_result_set.json")
    refinement_bytes = refinement_bytes or read("stage-13/refinement_result_set.json")
    try:
        baseline_text = baseline_bytes.decode("utf-8")
        refinement_text = refinement_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage14DomainEvaluatorError("Stage 12/13 authority is not UTF-8") from exc
    captured_baseline = parse_experiment_result_set(baseline_text)
    baseline = validate_experiment_result_set(run_dir, config, baseline_text)
    if baseline != captured_baseline:
        raise Stage14DomainEvaluatorError(
            "Stage 12 replay differs from captured result set"
        )
    if baseline.get("schema_version") != Decimal(2):
        raise Stage14DomainEvaluatorError("Stage 14 domain result requires Stage 12 v2")
    refinement = validate_domain_evaluator_refinement_result_set(run_dir, config)
    parsed_refinement = parse_domain_evaluator_refinement_result_set(refinement_text)
    if refinement != parsed_refinement:
        raise Stage14DomainEvaluatorError("Stage 13 replay differs from captured manifest")
    if refinement["baseline_manifest"] != _ref(
        "stage-12/experiment_result_set.json", baseline_bytes
    ):
        raise Stage14DomainEvaluatorError("Stage 13 baseline binding mismatch")
    if refinement["primary_metric"] != baseline["primary_metric"]:
        raise Stage14DomainEvaluatorError("Stage 13 primary metric binding mismatch")
    expected = _expected_bindings(baseline, baseline_bytes, refinement_bytes)
    captured = dict(run_files or _capture_source_files(baseline, read))
    _validate_captured_source_files(baseline, refinement, captured)
    if config_entries is None:
        config_entries = _capture_active_config_namespace(
            run_entries,
            read,
        )
    _validate_active_config_namespace(
        config_entries,
        baseline=baseline,
        runtime_config=config,
        run_dir=run_dir,
    )
    return {
        "baseline": baseline,
        "baseline_bytes": baseline_bytes,
        "refinement": refinement,
        "refinement_bytes": refinement_bytes,
        "bindings": expected,
        "observation_authority": _expected_observation_authority(baseline),
        "primary_metric": baseline["primary_metric"],
        "run_files": captured,
        "config_entries": config_entries,
    }


def _capture_active_config_namespace(
    list_entries: Callable[[], tuple[str, ...]],
    read: Callable[[str], bytes],
) -> tuple[tuple[str, bytes], ...]:
    """Capture the complete active-config namespace for the Stage 14 A/B view."""

    before = list_entries()
    _reject_noncanonical_resumed_configs(before)
    names = tuple(
        name
        for name in before
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
        or name
        in {
            "active_config_snapshot.json",
            "config_snapshot_history.jsonl",
            "checkpoint.json",
        }
    )
    entries = tuple((name, read(name)) for name in names)
    after = list_entries()
    _reject_noncanonical_resumed_configs(after)
    after_names = tuple(
        name
        for name in after
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
        or name
        in {
            "active_config_snapshot.json",
            "config_snapshot_history.jsonl",
            "checkpoint.json",
        }
    )
    if after_names != names:
        raise Stage14DomainEvaluatorError(
            "active config namespace changed during Stage 14 capture"
        )
    return entries


def _reject_noncanonical_resumed_configs(entries: tuple[str, ...]) -> None:
    for name in entries:
        if name.startswith("config.resumed-") and re.fullmatch(
            r"config\.resumed-\d{8}-\d{6}\.yaml", name
        ) is None:
            raise Stage14DomainEvaluatorError(
                f"noncanonical resumed config entry: {name}"
            )


def _validate_active_config_namespace(
    entries: tuple[tuple[str, bytes], ...],
    *,
    baseline: Mapping[str, Any],
    runtime_config: RCConfig,
    run_dir: Path,
) -> None:
    values = dict(entries)
    if len(values) != len(entries):
        raise Stage14DomainEvaluatorError("duplicate active config namespace entry")

    def text(name: str) -> str | None:
        value = values.get(name)
        if value is None:
            return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stage14DomainEvaluatorError(
                f"active config entry is not UTF-8: {name}"
            ) from exc

    snapshots = tuple(
        (name, value.decode("utf-8"))
        for name, value in entries
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
    )
    try:
        captured, path, raw, digest, _snapshots = replay_config_snapshot_namespace(
            ConfigSnapshotNamespaceInputs(
                snapshots=snapshots,
                pointer_text=text("active_config_snapshot.json"),
                history_text=text("config_snapshot_history.jsonl"),
                checkpoint_text=text("checkpoint.json"),
            ),
            project_root=run_dir,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage14DomainEvaluatorError(
            f"active config reconstruction failed: {exc}"
        ) from exc
    expected_ref = _ref(path, raw.encode("utf-8"))
    if baseline["run_config"] != expected_ref:
        raise Stage14DomainEvaluatorError("Stage 12 active config binding mismatch")
    if baseline["config_semantic_sha256"] != semantic_config_sha256(captured):
        raise Stage14DomainEvaluatorError("Stage 12 active config semantic mismatch")
    if digest != baseline["run_config"]["sha256"]:
        raise Stage14DomainEvaluatorError("Stage 12 active config digest mismatch")
    if semantic_config_sha256(captured) != semantic_config_sha256(runtime_config):
        raise Stage14DomainEvaluatorError(
            "active config differs from Stage 14 runtime config"
        )


def _build_candidate_payload(
    source: Mapping[str, Any], artifact_bytes: Mapping[str, bytes]
) -> dict[str, Any]:
    artifacts = [
        {"role": role, **_ref(path, artifact_bytes[path])}
        for role, path in _ARTIFACT_LAYOUT
    ]
    payload: dict[str, Any] = {
        "schema_version": 2,
        "candidate_policy_version": 2,
        "candidate_id": "",
        "bindings": source["bindings"],
        "selected_result": {"type": "baseline", "iteration_id": None},
        "observation_authority": source["observation_authority"],
        "primary_metric": source["primary_metric"],
        "artifacts": artifacts,
    }
    payload["candidate_id"] = _candidate_id_for_payload(payload)
    return payload


def _build_root_plan_from_source(
    controller: CanonicalAnalysisController,
    source: Mapping[str, Any],
) -> dict[str, bytes]:
    assert controller is not None
    collections = _capture_candidate_collections_from_controller(controller)
    snapshot = _DomainStage14Snapshot(
        stage_entries=(),
        candidate_tree=tuple(sorted(controller.read_candidate_tree().items())),
        candidate_collections=collections,
        root_entries=(),
        root_manifest=None,
        summary_copy=None,
        analysis_copy=None,
        stage12_manifest=source["baseline_bytes"],
        stage13_manifest=source["refinement_bytes"],
        stage10_capture_tree=(),
        run_files=tuple(sorted(source["run_files"].items())),
        config_entries=source["config_entries"],
    )
    return _build_root_plan_from_snapshot(snapshot, source)


def _build_root_plan_from_snapshot(
    snapshot: _DomainStage14Snapshot,
    source: Mapping[str, Any],
) -> dict[str, bytes]:
    candidates = _candidates_from_collections(snapshot.candidate_collections, source)
    if not candidates:
        raise Stage14DomainEvaluatorError("no eligible Stage 14 domain candidate")
    winner_id = min(candidates)
    stage_name, winner, tree = candidates[winner_id]
    artifacts = {item["role"]: item for item in winner["artifacts"]}
    summary = tree[artifacts["summary"]["path"]]
    analysis = tree[artifacts["analysis"]["path"]]
    manifest = {
        "schema_version": 2,
        "selection_policy_version": 2,
        "generation_kind": "domain_evaluator",
        "bindings": source["bindings"],
        "selected_result": {"type": "baseline", "iteration_id": None},
        "observation_authority": source["observation_authority"],
        "primary_metric": source["primary_metric"],
        "selected_candidate": {
            "candidate_id": winner_id,
            "manifest": _ref(
                f"{stage_name}/evidence_candidates/{winner_id}/"
                "experiment_evidence_candidate.json",
                tree["experiment_evidence_candidate.json"],
            ),
        },
        "selected_summary": {
            "source": _ref(
                f"{stage_name}/evidence_candidates/{winner_id}/"
                "experiment_summary.json",
                summary,
            ),
            "canonical_copy": _ref("experiment_summary_best.json", summary),
        },
        "selected_analysis": {
            "source": _ref(
                f"{stage_name}/evidence_candidates/{winner_id}/analysis.md", analysis
            ),
            "canonical_copy": _ref("analysis_best.md", analysis),
        },
    }
    manifest_bytes = canonical_authority_json_text(manifest).encode("utf-8")
    _parse_root_bytes(manifest_bytes)
    return {"manifest": manifest_bytes, "summary": summary, "analysis": analysis}


def _validate_root_snapshot(
    root: Mapping[str, Any],
    snapshot: _DomainStage14Snapshot,
    source: Mapping[str, Any],
) -> None:
    _assert_root_owned_namespace(snapshot.root_entries)
    if snapshot.summary_copy is None or snapshot.analysis_copy is None:
        raise Stage14DomainEvaluatorError("canonical domain compatibility copy is missing")
    if root["bindings"] != source["bindings"]:
        raise Stage14DomainEvaluatorError("canonical domain root bindings mismatch")
    if root["observation_authority"] != source["observation_authority"]:
        raise Stage14DomainEvaluatorError("canonical domain root observation mismatch")
    if root["primary_metric"] != source["primary_metric"]:
        raise Stage14DomainEvaluatorError("canonical domain root primary metric mismatch")
    expected = _build_root_plan_from_snapshot(snapshot, source)
    if snapshot.root_manifest != expected["manifest"]:
        raise Stage14DomainEvaluatorError("canonical domain root differs from deterministic promotion")
    if snapshot.summary_copy != expected["summary"]:
        raise Stage14DomainEvaluatorError("canonical domain summary compatibility copy mismatch")
    if snapshot.analysis_copy != expected["analysis"]:
        raise Stage14DomainEvaluatorError("canonical domain analysis compatibility copy mismatch")


def _capture_domain_stage14_snapshot(
    namespace,
    *,
    lease: ReleaseGraphLock,
    include_root: bool = True,
) -> _DomainStage14Snapshot:
    entries = namespace.direct_entries()
    if entries != ("evidence_candidates",):
        raise Stage14DomainEvaluatorError("Stage 14 v2 namespace is not exact")
    candidate_tree = tuple(sorted(namespace.read_directory_tree("evidence_candidates").items()))
    candidate_collections = _capture_candidate_collections(namespace, lease)
    root_entries = namespace.run_entries()
    root_manifest = None
    summary_copy = None
    analysis_copy = None
    if include_root:
        root_manifest = namespace.read_run_file("canonical_experiment_evidence.json")
        summary_copy = namespace.read_run_file("experiment_summary_best.json")
        analysis_copy = namespace.read_run_file("analysis_best.md")
    stage12_manifest = namespace.read_run_file("stage-12/experiment_result_set.json")
    stage13_manifest = namespace.read_run_file("stage-13/refinement_result_set.json")
    with lease.open_stage_namespace("stage-10") as stage10_namespace:
        stage10_capture_tree = tuple(
            sorted(
                stage10_namespace.read_directory_tree(
                    "evaluator-capture-v1"
                ).items()
            )
        )
        stage10_namespace.assert_canonical()
    try:
        baseline = parse_experiment_result_set(stage12_manifest.decode("utf-8"))
        refinement = parse_domain_evaluator_refinement_result_set(
            stage13_manifest.decode("utf-8")
        )
    except UnicodeDecodeError as exc:
        raise Stage14DomainEvaluatorError("Stage 12/13 authority is not UTF-8") from exc
    run_files = _capture_source_files(baseline, namespace.read_run_file)
    _validate_captured_source_files(baseline, refinement, run_files)
    config_entries = _capture_active_config_namespace(
        namespace.run_entries, namespace.read_run_file
    )
    return _DomainStage14Snapshot(
        stage_entries=entries,
        candidate_tree=candidate_tree,
        candidate_collections=candidate_collections,
        root_entries=root_entries,
        root_manifest=root_manifest,
        summary_copy=summary_copy,
        analysis_copy=analysis_copy,
        stage12_manifest=stage12_manifest,
        stage13_manifest=stage13_manifest,
        stage10_capture_tree=stage10_capture_tree,
        run_files=tuple(sorted(run_files.items())),
        config_entries=config_entries,
    )


def _capture_candidate_collections(
    namespace,
    lease: ReleaseGraphLock,
) -> tuple[tuple[str, tuple[tuple[str, bytes], ...]], ...]:
    names = _stage14_generation_names(namespace.run_entries())
    captured: list[tuple[str, tuple[tuple[str, bytes], ...]]] = []
    for stage_name in names:
        if stage_name == "stage-14":
            stage_namespace = namespace
            if stage_namespace.direct_entries() != ("evidence_candidates",):
                raise Stage14DomainEvaluatorError(
                    "Stage 14 v2 candidate history namespace is not exact"
                )
            tree = stage_namespace.read_directory_tree("evidence_candidates")
        else:
            with lease.open_stage_namespace(stage_name) as stage_namespace:
                if stage_namespace.direct_entries() != ("evidence_candidates",):
                    raise Stage14DomainEvaluatorError(
                        "Stage 14 v2 candidate history namespace is not exact"
                    )
                tree = stage_namespace.read_directory_tree("evidence_candidates")
                stage_namespace.assert_canonical()
        captured.append((stage_name, tuple(sorted(tree.items()))))
    return tuple(captured)


def _capture_candidate_collections_from_controller(
    controller: CanonicalAnalysisController,
) -> tuple[tuple[str, tuple[tuple[str, bytes], ...]], ...]:
    names = _stage14_generation_names(controller.run_entries())
    captured: list[tuple[str, tuple[tuple[str, bytes], ...]]] = []
    for stage_name in names:
        if controller.read_stage_entries(stage_name) != ("evidence_candidates",):
            raise Stage14DomainEvaluatorError(
                "Stage 14 v2 candidate history namespace is not exact"
            )
        tree = controller.read_stage_candidate_tree(stage_name)
        captured.append((stage_name, tuple(sorted(tree.items()))))
    return tuple(captured)


def _stage14_generation_names(entries: tuple[str, ...]) -> tuple[str, ...]:
    versions: list[tuple[int, str]] = []
    for entry in entries:
        match = re.fullmatch(r"stage-14_v([1-9]\d*)", entry)
        if entry.startswith("stage-14_v") and match is None:
            raise Stage14DomainEvaluatorError(
                "invalid Stage 14 generation shadow"
            )
        if match is not None:
            versions.append((int(match.group(1)), entry))
    if "stage-14" not in entries:
        raise Stage14DomainEvaluatorError("live Stage 14 generation is missing")
    return ("stage-14", *(name for _version, name in sorted(versions)))


def _candidates_from_tree(
    entries: tuple[tuple[str, bytes], ...],
) -> dict[str, tuple[dict[str, Any], dict[str, bytes]]]:
    grouped: dict[str, dict[str, bytes]] = {}
    for relative, content in entries:
        parts = relative.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise Stage14DomainEvaluatorError("Stage 14 candidate tree path is invalid")
        grouped.setdefault(parts[0], {})[parts[1]] = content
    candidates: dict[str, tuple[dict[str, Any], dict[str, bytes]]] = {}
    for candidate_id, tree in grouped.items():
        manifest = tree.get("experiment_evidence_candidate.json")
        if manifest is None:
            raise Stage14DomainEvaluatorError("Stage 14 candidate manifest is missing")
        try:
            payload = parse_domain_evaluator_candidate(manifest.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise Stage14DomainEvaluatorError("Stage 14 candidate manifest is not UTF-8") from exc
        if payload["candidate_id"] != candidate_id:
            raise Stage14DomainEvaluatorError("Stage 14 candidate directory identity mismatch")
        _validate_candidate_tree(payload, tree)
        candidates[candidate_id] = (payload, tree)
    return candidates


def _candidates_from_collections(
    collections: tuple[tuple[str, tuple[tuple[str, bytes], ...]], ...],
    source: Mapping[str, Any],
) -> dict[str, tuple[str, dict[str, Any], dict[str, bytes]]]:
    candidates: dict[str, tuple[str, dict[str, Any], dict[str, bytes]]] = {}
    for stage_name, entries in collections:
        for candidate_id, (payload, tree) in _candidates_from_tree(entries).items():
            existing = candidates.get(candidate_id)
            if existing is not None:
                _existing_stage, _existing_payload, existing_tree = existing
                if existing_tree != tree:
                    raise Stage14DomainEvaluatorError(
                        "Stage 14 domain candidate ID collision across generations"
                    )
                continue
            if _candidate_matches_source(payload, source):
                candidates[candidate_id] = (stage_name, payload, tree)
    return candidates


def _candidate_matches_source(
    payload: Mapping[str, Any], source: Mapping[str, Any]
) -> bool:
    return (
        payload["bindings"] == source["bindings"]
        and payload["observation_authority"] == source["observation_authority"]
        and payload["primary_metric"] == source["primary_metric"]
    )


def _candidate_tree_by_id(
    entries: Mapping[str, bytes], candidate_id: str
) -> dict[str, bytes] | None:
    prefix = candidate_id + "/"
    selected = {
        relative.removeprefix(prefix): content
        for relative, content in entries.items()
        if relative.startswith(prefix)
    }
    return selected or None


def _validate_candidate_tree(
    payload: Mapping[str, Any],
    tree: Mapping[str, bytes],
) -> None:
    expected_paths = {"experiment_evidence_candidate.json"}
    expected_paths.update(path for _role, path in _ARTIFACT_LAYOUT)
    if set(tree) != expected_paths:
        raise Stage14DomainEvaluatorError("Stage 14 domain candidate namespace is not exact")
    artifacts = {item["role"]: item for item in payload["artifacts"]}
    expected_artifacts = _render_fixed_artifacts(payload["primary_metric"])
    for role, relative in _ARTIFACT_LAYOUT:
        ref = artifacts[role]
        content = tree[relative]
        if ref != {"role": role, **_ref(relative, content)}:
            raise Stage14DomainEvaluatorError(f"Stage 14 candidate artifact mismatch: {role}")
        if content != expected_artifacts[relative]:
            raise Stage14DomainEvaluatorError(f"Stage 14 candidate artifact is not deterministic: {role}")


def _expected_bindings(
    baseline: Mapping[str, Any], baseline_bytes: bytes, refinement_bytes: bytes
) -> dict[str, Any]:
    return {
        "experiment_contract": baseline["experiment_contract"],
        "sealed_candidate_manifest": baseline["sealed_candidate_manifest"],
        "capture_manifest": baseline["capture_manifest"],
        "package_manifest": baseline["package_manifest"],
        "execution_policy": baseline["execution_policy"],
        "stage12_result_set": _ref("stage-12/experiment_result_set.json", baseline_bytes),
        "stage13_refinement": _ref("stage-13/refinement_result_set.json", refinement_bytes),
        "run_config": baseline["run_config"],
        "config_semantic_policy_version": baseline["config_semantic_policy_version"],
        "config_semantic_sha256": baseline["config_semantic_sha256"],
        "claim_scope": baseline["claim_scope"],
        "dataset_origin": baseline["dataset_origin"],
        "dataset_name": baseline["dataset_name"],
        "evaluator_schema": baseline["evaluator_schema"],
        "metric_authority": baseline["metric_authority"],
    }


def _expected_observation_authority(baseline: Mapping[str, Any]) -> dict[str, Any]:
    evidence = {item["path"]: item for item in baseline["evidence_files"]}
    required = {
        "score_evidence_1": "stage-12/evidence-v2/invocation-1/score_evidence.jsonl",
        "score_evidence_2": "stage-12/evidence-v2/invocation-2/score_evidence.jsonl",
    }
    try:
        return {
            "invocation_journal": baseline["invocation_journal"],
            **{field: evidence[path] for field, path in required.items()},
            "observations": baseline["observations"],
            "results": baseline["results"],
            "observation_policy_version": 2,
        }
    except KeyError as exc:
        raise Stage14DomainEvaluatorError("Stage 12 observation evidence is incomplete") from exc


def _validate_bindings(value: object) -> None:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("bindings must be an object")
    _exact_keys(value, _BINDING_FIELDS, "bindings")
    for field in _BINDING_REFS:
        _file_ref_v2(value[field], f"bindings.{field}")
    expected_paths = {
        "experiment_contract": "stage-09/experiment_contract.yaml",
        "sealed_candidate_manifest": "stage-10/selected_candidate_manifest.json",
        "capture_manifest": "stage-10/evaluator-capture-v1/capture-manifest.json",
        "execution_policy": "stage-09/domain_evaluator_execution_policy.json",
        "stage12_result_set": "stage-12/experiment_result_set.json",
        "stage13_refinement": "stage-13/refinement_result_set.json",
    }
    for field, path in expected_paths.items():
        if value[field]["path"] != path:
            raise CanonicalExperimentEvidenceError(f"noncanonical {field} binding path")
    if type(value["config_semantic_policy_version"]) is not int or value[
        "config_semantic_policy_version"
    ] != 1:
        raise CanonicalExperimentEvidenceError("unsupported config semantic policy")
    _sha256(value["config_semantic_sha256"], "config_semantic_sha256")
    for field in ("claim_scope", "dataset_origin", "dataset_name", "evaluator_schema"):
        _required_string(value[field], field)
    if not isinstance(value["metric_authority"], dict):
        raise CanonicalExperimentEvidenceError("metric_authority must be an object")


def _validate_observation_authority(value: object) -> None:
    if not isinstance(value, dict):
        raise CanonicalExperimentEvidenceError("observation_authority must be an object")
    _exact_keys(value, _OBSERVATION_FIELDS, "observation_authority")
    for field in _OBSERVATION_REFS:
        _file_ref_v2(value[field], f"observation_authority.{field}")
    expected_paths = {
        "invocation_journal": "stage-12/execution_invocation_journal.jsonl",
        "score_evidence_1": "stage-12/evidence-v2/invocation-1/score_evidence.jsonl",
        "score_evidence_2": "stage-12/evidence-v2/invocation-2/score_evidence.jsonl",
        "observations": "stage-12/evidence-v2/observations.json",
        "results": "stage-12/evidence-v2/results.json",
    }
    for field, path in expected_paths.items():
        if value[field]["path"] != path:
            raise CanonicalExperimentEvidenceError(f"noncanonical {field} observation path")
    if type(value["observation_policy_version"]) is not int or value[
        "observation_policy_version"
    ] != 2:
        raise CanonicalExperimentEvidenceError("unsupported observation policy")


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
    metric = value["value"]
    if isinstance(metric, bool) or not isinstance(metric, (int, Decimal)):
        raise CanonicalExperimentEvidenceError("primary_metric.value must be numeric")
    if isinstance(metric, Decimal) and not metric.is_finite():
        raise CanonicalExperimentEvidenceError("primary_metric.value must be finite")


def _validate_artifact_refs(value: object) -> None:
    if not isinstance(value, list) or len(value) != len(_ARTIFACT_LAYOUT):
        raise CanonicalExperimentEvidenceError("Stage 14 candidate artifacts are invalid")
    expected = []
    for role, path in _ARTIFACT_LAYOUT:
        expected.append((role, path))
    actual = []
    for item in value:
        if not isinstance(item, dict):
            raise CanonicalExperimentEvidenceError("candidate artifact must be an object")
        _exact_keys(item, {"role", "path", "sha256", "size"}, "candidate artifact")
        role = _required_string(item["role"], "candidate artifact role")
        ref = _file_ref_v2(
            {key: item[key] for key in ("path", "sha256", "size")},
            "candidate artifact",
        )
        actual.append((role, ref["path"]))
    if actual != expected:
        raise CanonicalExperimentEvidenceError("Stage 14 candidate artifact layout mismatch")


def _candidate_id(value: object) -> str:
    candidate = _required_string(value, "candidate_id")
    if len(candidate) != 69 or not candidate.startswith("cand-"):
        raise CanonicalExperimentEvidenceError("invalid candidate_id")
    _sha256(candidate[5:], "candidate_id")
    return candidate


def _candidate_id_for_payload(payload: Mapping[str, Any]) -> str:
    identity = {
        "schema_version": 2,
        "candidate_policy_version": 2,
        "bindings": payload["bindings"],
        "selected_result": payload["selected_result"],
        "observation_authority": payload["observation_authority"],
        "primary_metric": payload["primary_metric"],
        "artifacts": payload["artifacts"],
    }
    return "cand-" + sha256_text(canonical_authority_json_text(identity))


def _render_fixed_artifacts(primary_metric: Mapping[str, Any]) -> dict[str, bytes]:
    value = canonical_decimal(primary_metric["value"])
    key = primary_metric["key"]
    summary = {
        "schema_version": 2,
        "summary_policy_version": 2,
        "primary_metric": primary_metric,
        "metrics_summary": {key: {"mean": primary_metric["value"]}},
    }
    figure_plan = {
        "schema_version": 2,
        "generator": "canonical_stage14_domain_evaluator_v2",
        "figures": [],
    }
    return {
        "analysis.md": (
            "# Canonical Domain Evaluator Analysis\n\n"
            f"Primary metric `{key}`: {value}.\n"
        ).encode("utf-8"),
        "experiment_summary.json": canonical_authority_json_text(summary).encode("utf-8"),
        "results_table.tex": (
            "\\begin{table}[h]\n\\centering\n"
            "\\caption{Canonical Domain Evaluator Results}\n"
            "\\begin{tabular}{lr}\n\\hline\n"
            f"Metric & Value \\\\" + "\n\\hline\n"
            + f"{key} & {value} \\\\" + "\n"
            "\\hline\n\\end{tabular}\n\\end{table}\n"
        ).encode("utf-8"),
        "figure_plan.json": canonical_authority_json_text(figure_plan).encode("utf-8"),
    }


def _ref(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _parse_root_bytes(content: bytes) -> dict[str, Any]:
    try:
        return parse_domain_evaluator_canonical_manifest(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Stage14DomainEvaluatorError("canonical domain root is not UTF-8") from exc


def _assert_root_owned_namespace(entries: tuple[str, ...]) -> None:
    present = set(entries)
    for name in _ROOT_OWNED:
        if name not in present:
            raise Stage14DomainEvaluatorError("canonical domain root namespace is incomplete")
    for name in entries:
        if name.startswith("canonical_experiment_evidence") and name != _ROOT_OWNED[0]:
            raise Stage14DomainEvaluatorError("canonical domain root has extra manifest authority")
        if name.startswith("experiment_summary_best") and name != _ROOT_OWNED[1]:
            raise Stage14DomainEvaluatorError("canonical domain root has extra summary authority")
        if name.startswith("analysis_best") and name != _ROOT_OWNED[2]:
            raise Stage14DomainEvaluatorError("canonical domain root has extra analysis authority")


def _tree_from_directory(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise Stage14DomainEvaluatorError("Stage 14 staging tree is unsafe")
    tree: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or path.is_dir() or not path.is_file():
            if path.is_dir():
                continue
            raise Stage14DomainEvaluatorError("Stage 14 staging tree contains unsafe entry")
        tree[path.relative_to(root).as_posix()] = path.read_bytes()
    return tree


def _remove_staging_tree(root: Path) -> None:
    if root.exists() or root.is_symlink():
        import shutil

        if root.is_symlink():
            root.unlink()
        else:
            shutil.rmtree(root)


def _config_from_bytes(content: bytes, run_dir: Path) -> RCConfig:
    import yaml

    try:
        raw = yaml.safe_load(content.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be a mapping")
        return RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    except Exception as exc:  # noqa: BLE001
        raise Stage14DomainEvaluatorError("canonical domain run config is invalid") from exc


def _snapshot_accessor(
    root: Mapping[str, Any],
    snapshot: _DomainStage14Snapshot,
    source: Mapping[str, Any],
    config_bytes: bytes,
) -> CanonicalExperimentEvidence:
    candidates = _candidates_from_collections(snapshot.candidate_collections, source)
    candidate_id = root["selected_candidate"]["candidate_id"]
    _stage_name, candidate, tree = candidates[candidate_id]
    artifacts = tuple(
        CanonicalEvidenceArtifact(
            role=item["role"],
            path=item["path"],
            sha256=item["sha256"],
            content=tree[item["path"]],
        )
        for item in candidate["artifacts"]
    )
    artifact_map = {artifact.role: artifact for artifact in artifacts}
    summary = _parse_json_value(
        artifact_map["summary"].content.decode("utf-8"),
        "canonical domain selected summary",
    )
    if not isinstance(summary, dict):
        raise Stage14DomainEvaluatorError("canonical domain summary root is invalid")
    results = _parse_json_value(
        _bytes_from_ref(source, source["baseline"]["results"]).decode("utf-8"),
        "canonical domain results",
    )
    observations = _parse_json_value(
        _bytes_from_ref(source, source["baseline"]["observations"]).decode("utf-8"),
        "canonical domain observations",
    )
    if not isinstance(results, dict) or not isinstance(observations, dict):
        raise Stage14DomainEvaluatorError("canonical domain observation payload is invalid")
    metric_observations = _project_metric_observations(observations)
    project_artifacts = _domain_project_artifacts(snapshot, source)
    candidate_manifest = tree["experiment_evidence_candidate.json"]
    contract = _bytes_from_ref(source, source["baseline"]["experiment_contract"])
    execution_policy_ref = source["baseline"]["execution_policy"]
    execution_policy = _bytes_from_ref(source, execution_policy_ref)
    return CanonicalExperimentEvidence(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256=hashlib.sha256(snapshot.root_manifest or b"").hexdigest(),
        manifest=_freeze_authority_value(root),
        candidate_manifest_path=root["selected_candidate"]["manifest"]["path"],
        candidate_manifest_sha256=hashlib.sha256(candidate_manifest).hexdigest(),
        candidate=_freeze_authority_value(candidate),
        selected_result_manifest_path="stage-13/refinement_result_set.json",
        selected_result_manifest_sha256=hashlib.sha256(snapshot.stage13_manifest).hexdigest(),
        selected_result=_freeze_authority_value(source["refinement"]),
        selected_execution_artifact=CanonicalEvidenceArtifact(
            role="observations",
            path=source["baseline"]["observations"]["path"],
            sha256=source["baseline"]["observations"]["sha256"],
            content=_bytes_from_ref(source, source["baseline"]["observations"]),
        ),
        experiment_contract_path=source["bindings"]["experiment_contract"]["path"],
        experiment_contract_sha256=source["bindings"]["experiment_contract"]["sha256"],
        experiment_contract_bytes=contract,
        run_config_path=source["bindings"]["run_config"]["path"],
        run_config_sha256=source["bindings"]["run_config"]["sha256"],
        run_config_bytes=config_bytes,
        summary_bytes=artifact_map["summary"].content,
        summary=_freeze_authority_value(summary),
        analysis_bytes=artifact_map["analysis"].content,
        analysis_text=artifact_map["analysis"].content.decode("utf-8"),
        metric_observations=_freeze_authority_value(metric_observations),
        structured_results=_freeze_authority_value(results),
        artifacts=artifacts,
        project_artifacts=project_artifacts,
        execution_policy_artifact=CanonicalEvidenceArtifact(
            role="execution_policy",
            path=execution_policy_ref["path"],
            sha256=execution_policy_ref["sha256"],
            content=execution_policy,
        ),
    )


def _project_metric_observations(
    observations: Mapping[str, Any],
) -> dict[str, list[int | Decimal]]:
    """Project replayed domain rows onto the legacy metric-series interface."""

    metric_keys = observations.get("metric_keys")
    rows = observations.get("observations")
    if (
        not isinstance(metric_keys, list)
        or tuple(metric_keys) != _DOMAIN_METRIC_KEYS
        or not isinstance(rows, list)
        or len(rows) != len(_DOMAIN_OBSERVATION_IDENTITIES)
    ):
        raise Stage14DomainEvaluatorError(
            "canonical domain metric observation projection is invalid"
        )
    projected: dict[str, list[int | Decimal]] = {key: [] for key in metric_keys}
    expected_keys = set(metric_keys)
    identities: set[tuple[str, str, str, int]] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "circuit_family",
                "circuit_variant",
                "condition",
                "metrics",
                "n_total",
                "n_trojan",
                "seed",
            }
            or not isinstance(row.get("metrics"), dict)
        ):
            raise Stage14DomainEvaluatorError(
                "canonical domain observation row is invalid"
            )
        family = row["circuit_family"]
        variant = row["circuit_variant"]
        condition = row["condition"]
        seed = row["seed"]
        if (
            not isinstance(family, str)
            or not family
            or not isinstance(variant, str)
            or not variant
            or not isinstance(condition, str)
            or not condition
            or type(seed) is not int
            or type(row["n_total"]) is not int
            or type(row["n_trojan"]) is not int
        ):
            raise Stage14DomainEvaluatorError(
                "canonical domain observation identity is invalid"
            )
        identity = (family, variant, condition, seed)
        if identity in identities:
            raise Stage14DomainEvaluatorError(
                "canonical domain observation identity is duplicated"
            )
        identities.add(identity)
        metrics = row["metrics"]
        if set(metrics) != expected_keys:
            raise Stage14DomainEvaluatorError(
                "canonical domain observation metric layout mismatch"
            )
        for key in metric_keys:
            value = metrics[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, Decimal))
                or isinstance(value, Decimal)
                and not value.is_finite()
            ):
                raise Stage14DomainEvaluatorError(
                    "canonical domain metric observation must be finite numeric"
                )
            projected[key].append(value)
    if identities != _DOMAIN_OBSERVATION_IDENTITIES:
        raise Stage14DomainEvaluatorError(
            "canonical domain observation condition/seed closure mismatch"
        )
    return projected


def project_domain_metric_observations(content: bytes) -> dict[str, list[int | Decimal]]:
    """Strictly replay the v2 observation artifact used by release consumers."""

    try:
        value = _parse_json_value(content.decode("utf-8"), "domain observations")
    except UnicodeDecodeError as exc:
        raise Stage14DomainEvaluatorError(
            "canonical domain observations are not UTF-8"
        ) from exc
    expected = {
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
    if not isinstance(value, dict) or set(value) != expected:
        raise Stage14DomainEvaluatorError(
            "canonical domain observation fields mismatch"
        )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["observation_policy_version"]) is not int
        or value["observation_policy_version"] != 1
    ):
        raise Stage14DomainEvaluatorError(
            "canonical domain observation policy mismatch"
        )
    _sha256(value["dataset_capture_sha256"], "dataset_capture_sha256")
    _sha256(value["score_evidence_sha256"], "score_evidence_sha256")
    if canonical_authority_json_text(value).encode("utf-8") != content:
        raise Stage14DomainEvaluatorError(
            "canonical domain observations are not canonical JSON"
        )
    return _project_metric_observations(value)


def _domain_project_artifacts(
    snapshot: _DomainStage14Snapshot,
    source: Mapping[str, Any],
) -> tuple[CanonicalProjectArtifact, ...]:
    """Expose the exact Stage 10 evaluator capture as the release code tree."""

    tree = dict(snapshot.stage10_capture_tree)
    manifest_path = "capture-manifest.json"
    expected_manifest = _bytes_from_ref(source, source["baseline"]["capture_manifest"])
    if tree.get(manifest_path) != expected_manifest:
        raise Stage14DomainEvaluatorError("Stage 10 capture manifest snapshot mismatch")
    manifest = _parse_json_value(
        expected_manifest.decode("utf-8"), "Stage 10 capture manifest"
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise Stage14DomainEvaluatorError("Stage 10 capture manifest is invalid")
    expected_paths = {manifest_path}
    artifacts: list[CanonicalProjectArtifact] = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {
            "role",
            "path",
            "sha256",
            "size",
            "package_entry_sha256",
        }:
            raise Stage14DomainEvaluatorError("Stage 10 capture file entry is invalid")
        path = item["path"]
        if not isinstance(path, str) or path in expected_paths:
            raise Stage14DomainEvaluatorError("Stage 10 capture file path is invalid")
        expected_paths.add(path)
        try:
            content = tree[path]
        except KeyError as exc:
            raise Stage14DomainEvaluatorError(
                f"Stage 10 capture project file is missing: {path}"
            ) from exc
        if (
            type(item["size"]) is not int
            or len(content) != item["size"]
            or hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise Stage14DomainEvaluatorError(
                f"Stage 10 capture project file binding mismatch: {path}"
            )
        logical_name = _domain_project_logical_name(path, item["role"])
        artifacts.append(
            CanonicalProjectArtifact(
                logical_name=logical_name,
                source_path=f"stage-10/evaluator-capture-v1/{path}",
                sha256=item["sha256"],
                content=content,
            )
        )
    if set(tree) != expected_paths:
        raise Stage14DomainEvaluatorError("Stage 10 capture project namespace mismatch")
    names = [artifact.logical_name for artifact in artifacts]
    if len(names) != len(set(names)) or "main.py" not in names:
        raise Stage14DomainEvaluatorError("domain evaluator release project is invalid")
    return tuple(sorted(artifacts, key=lambda artifact: artifact.logical_name))


def _domain_project_logical_name(path: str, role: object) -> str:
    if role == "evaluator" and path == "evaluator/evaluator_main.py":
        return "main.py"
    if role == "verifier" and path == "verifier/verifier_main.py":
        return "verifier_main.py"
    if role == "vendor" and path.startswith("vendor/"):
        return "trojnet/" + path.removeprefix("vendor/")
    if role == "policy" and path == "policy/execution-policy-v1.json":
        return "execution-policy-v1.json"
    if role == "data" and path.startswith("data/"):
        return path
    raise Stage14DomainEvaluatorError(
        f"unsupported domain evaluator release project entry: {path}"
    )


def _bytes_from_ref(source: Mapping[str, Any], ref: Mapping[str, Any]) -> bytes:
    path = ref["path"]
    try:
        content = source["run_files"][path]
    except KeyError as exc:
        raise Stage14DomainEvaluatorError(f"captured Stage 12 reference is missing: {path}") from exc
    if hashlib.sha256(content).hexdigest() != ref["sha256"] or len(content) != ref["size"]:
        raise Stage14DomainEvaluatorError("captured Stage 12 reference mismatch")
    return content


def _capture_source_files(
    baseline: Mapping[str, Any], read: Any
) -> dict[str, bytes]:
    refs = [
        baseline["experiment_contract"],
        baseline["sealed_candidate_manifest"],
        baseline["capture_manifest"],
        baseline["package_manifest"],
        baseline["execution_policy"],
        baseline["run_config"],
        baseline["invocation_journal"],
        baseline["observations"],
        baseline["results"],
        *baseline["evidence_files"],
    ]
    captured: dict[str, bytes] = {}
    for ref in refs:
        path = ref["path"]
        if path in captured:
            continue
        captured[path] = read(path)
    return captured


def _validate_captured_source_files(
    baseline: Mapping[str, Any],
    refinement: Mapping[str, Any],
    captured: Mapping[str, bytes],
) -> None:
    refs = [
        baseline["experiment_contract"],
        baseline["sealed_candidate_manifest"],
        baseline["capture_manifest"],
        baseline["package_manifest"],
        baseline["execution_policy"],
        baseline["run_config"],
        baseline["invocation_journal"],
        baseline["observations"],
        baseline["results"],
        *baseline["evidence_files"],
    ]
    for ref in refs:
        path = ref["path"]
        try:
            content = captured[path]
        except KeyError as exc:
            raise Stage14DomainEvaluatorError(
                f"captured domain authority is missing: {path}"
            ) from exc
        if hashlib.sha256(content).hexdigest() != ref["sha256"] or len(content) != ref["size"]:
            raise Stage14DomainEvaluatorError(
                f"captured domain authority binding mismatch: {path}"
            )
    if refinement["run_config"] != baseline["run_config"]:
        raise Stage14DomainEvaluatorError("Stage 13 run config binding mismatch")
