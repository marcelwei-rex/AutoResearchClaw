"""Stages 11-13: Resource planning, experiment execution, and iterative refinement."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    load_contract,
    sha256_file,
)
from researchclaw.experiment_runtime.scaffold import (
    scaffold_sha256 as runtime_scaffold_sha256,
)
from researchclaw.llm.client import LLMClient
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.pipeline._helpers import (
    StageResult,
    _chat_with_prompt,
    _get_pipeline_evolution_overlay,
    _read_prior_artifact,
    _safe_json_loads,
    _utcnow_iso,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    build_refinement_execution_text,
    build_refinement_validation_report,
    build_stage12_evidence_texts,
    canonical_authority_json_text,
    canonical_decimal,
    invocation_generation_binding_sha256,
    parse_experiment_result_set,
    parse_invocation_result,
    parse_refinement_result_set,
    semantic_config_sha256,
    sha256_text,
    stage12_primary_metric,
    validate_experiment_result_set,
    validate_refinement_result_set,
    validate_selected_candidate_manifest,
)
from researchclaw.pipeline.canonical_execution_controller import (
    CanonicalExecutionController,
    CanonicalRefinementController,
    InvocationLease,
)
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)

_BANNED_SELECTED_FILES = frozenset(
    {"results.json", "smoke_results.json", "attempt.json", "experiment_summary.json", "metrics.json"}
)
_BANNED_SELECTED_DIRS = frozenset({"runs", "attempts", "candidates", ".smoke_sandbox"})


def _execute_resource_planning(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""
    schedule: dict[str, Any] | None = None
    schedule_source = "template"
    if llm is not None:
        _pm = prompts or PromptManager()
        _overlay = _get_pipeline_evolution_overlay(run_dir, "resource_planning")
        sp = _pm.for_stage("resource_planning", evolution_overlay=_overlay, exp_plan=exp_plan)
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        parsed = _safe_json_loads(resp.content, {})
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("tasks"), list)
            and parsed["tasks"]
        ):
            schedule = parsed
            schedule_source = "model"
        elif isinstance(parsed, dict):
            logger.warning(
                "Stage 11: model response missing/empty 'tasks' list "
                "(received keys: %s); falling back to template",
                sorted(parsed.keys()),
            )
    if schedule is None:
        schedule = {
            "tasks": [
                {
                    "id": "baseline",
                    "name": "Run baseline",
                    "depends_on": [],
                    "gpu_count": 1,
                    "estimated_minutes": 20,
                    "priority": "high",
                },
                {
                    "id": "proposed",
                    "name": "Run proposed method",
                    "depends_on": ["baseline"],
                    "gpu_count": 1,
                    "estimated_minutes": 30,
                    "priority": "high",
                },
            ],
            "total_gpu_budget": 1,
            "generated": _utcnow_iso(),
        }
    schedule.setdefault("generated", _utcnow_iso())
    schedule["_meta"] = {"source": schedule_source}
    (stage_dir / "schedule.json").write_text(
        json.dumps(schedule, indent=2), encoding="utf-8"
    )
    return StageResult(
        stage=Stage.RESOURCE_PLANNING,
        status=StageStatus.DONE,
        artifacts=("schedule.json",),
        evidence_refs=("stage-11/schedule.json",),
    )


def _estimate_stage12_footprint_bytes(run_dir: Path) -> int:
    """Sum the on-disk size of stage-12 and any stage-12_v* siblings."""
    total = 0
    for d in run_dir.glob("stage-12*"):
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    return total


def _scaffold_sha256() -> str:
    return runtime_scaffold_sha256()


def _load_sealed_candidate(run_dir: Path, config: RCConfig) -> Path:
    stage10_dir = run_dir / "stage-10"
    manifest_path = stage10_dir / "selected_candidate_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("sealed candidate manifest missing")
    selected_dir = stage10_dir / "selected_candidate"
    if not selected_dir.is_dir():
        raise RuntimeError("sealed selected_candidate directory missing")

    try:
        validate_selected_candidate_manifest(run_dir, config)
    except (CanonicalExperimentEvidenceError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"sealed candidate manifest invalid: {exc}") from exc

    return selected_dir


def _execute_legacy_experiment_run(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    del stage_dir, run_dir, config, adapters, llm, prompts
    raise PermissionError("legacy_stage12_execution_removed")


def _atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _execute_experiment_run(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """Publish the C1-A single-invocation canonical Stage 12 result set."""
    del adapters, llm, prompts
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities("stage12.execute_experiment_run")
    from researchclaw.experiment.factory import create_sandbox

    controller: CanonicalExecutionController | None = None
    publication_workspace: Path | None = None

    def finish(result: StageResult) -> StageResult:
        if publication_workspace is not None:
            shutil.rmtree(publication_workspace, ignore_errors=True)
        if controller is not None:
            controller.close()
        return result

    try:
        controller = CanonicalExecutionController.prepare_generation(
            run_dir, stage_dir
        )
    except (OSError, RuntimeError) as exc:
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Canonical Stage 12 generation preparation failed: {exc}",
        ))

    mode = config.experiment.mode
    if mode not in {"sandbox", "docker"}:
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Canonical Stage 12 does not support experiment mode: {mode}",
        ))

    try:
        selected_dir = _load_sealed_candidate(run_dir, config)
        seal_path = run_dir / "stage-10/selected_candidate_manifest.json"
        seal_text = seal_path.read_text(encoding="utf-8")
        seal = validate_selected_candidate_manifest(run_dir, config, seal_text)
        contract_path = run_dir / seal["contract_path"]
        contract = load_contract(contract_path)
    except (OSError, UnicodeDecodeError, RuntimeError, ContractValidationError) as exc:
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Canonical Stage 12 preflight failed: {exc}",
        ))

    try:
        evaluator_schema = "hpc_anomaly_detection_v1"
        seal_sha256 = sha256_text(seal_text)
        config_sha256 = semantic_config_sha256(config)
        if config_sha256 != seal["config_semantic_sha256"]:
            raise CanonicalExperimentEvidenceError(
                "active config does not match the sealed Stage 10 generation"
            )
        generation_binding = invocation_generation_binding_sha256(
            experiment_contract_sha256=seal["contract_sha256"],
            sealed_candidate_manifest_sha256=seal_sha256,
            config_semantic_sha256=config_sha256,
            experiment_mode=mode,
            evaluator_schema=evaluator_schema,
        )
    except Exception as exc:  # noqa: BLE001
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Canonical Stage 12 setup failed: {exc}",
        ))
    lease: InvocationLease | None = None
    publication_workspace = Path(tempfile.mkdtemp(prefix="researchclaw-stage12-publish-"))
    evidence_staging = publication_workspace / "evidence-v1"
    try:
        lease = controller.acquire(
            generation_binding_sha256=generation_binding,
            experiment_contract_sha256=seal["contract_sha256"],
            sealed_candidate_manifest_sha256=seal_sha256,
            config_semantic_sha256=config_sha256,
        )
        sandbox_root = controller.prepare_invocation_workspace(lease)
        sandbox = create_sandbox(
            config.experiment,
            sandbox_root,
            metadata_dir=controller.metadata_dir,
        )
        expected_backend = "subprocess" if mode == "sandbox" else "docker"
        if getattr(sandbox, "backend_kind", None) != expected_backend:
            raise CanonicalExperimentEvidenceError(
                "canonical sandbox backend does not match requested experiment mode"
            )
        result = controller.run_project(
            lease,
            sandbox,
            selected_dir,
            timeout_sec=config.experiment.time_budget_sec,
        )
        if result.returncode != 0 or result.timed_out:
            controller.fail(lease, failure_code="sandbox_execution_failed_v1")
            return finish(StageResult(
                stage=Stage.EXPERIMENT_RUN,
                status=StageStatus.FAILED,
                artifacts=("execution_invocation_journal.jsonl", "diagnostics/"),
                evidence_refs=(),
                error="Canonical Stage 12 sandbox invocation failed",
            ))
        output_dir = result.output_dir
        if (
            not isinstance(output_dir, Path)
            or output_dir.is_symlink()
            or not output_dir.is_dir()
        ):
            raise CanonicalExperimentEvidenceError(
                "sandbox did not return an exact output directory"
            )
        evaluator_path = output_dir / "results.json"
        if evaluator_path.is_symlink() or not evaluator_path.is_file():
            raise CanonicalExperimentEvidenceError(
                "sandbox did not publish evaluator results.json"
            )
        structured_text = evaluator_path.read_text(encoding="utf-8")
        invocation_text, aggregate_text = build_stage12_evidence_texts(
            structured_text,
            contract=contract,
            evaluator_schema=evaluator_schema,
        )
        evidence_staging.mkdir()
        run_path = evidence_staging / "run-1.json"
        aggregate_path = evidence_staging / "results.json"
        _atomic_write_text(run_path, invocation_text)
        _atomic_write_text(aggregate_path, aggregate_text)
        controller.publish_directory_tree("evidence-v1", evidence_staging)
        controller.complete(lease, result_sha256=sha256_text(invocation_text))
        lease = None
    except Exception as exc:  # noqa: BLE001
        cleanup_error = ""
        try:
            if evidence_staging.exists():
                shutil.rmtree(evidence_staging)
            controller.remove_tree_entries(("evidence-v1",))
        except OSError as cleanup_exc:
            cleanup_error = f"; partial evidence cleanup failed: {cleanup_exc}"
        if lease is not None:
            try:
                controller.fail(lease, failure_code="evaluator_publication_failed_v1")
            except Exception:  # noqa: BLE001
                pass
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=("execution_invocation_journal.jsonl", "diagnostics/"),
            evidence_refs=(),
            error=f"Canonical Stage 12 publication failed: {exc}{cleanup_error}",
        ))

    journal_text = controller.read_text("execution_invocation_journal.jsonl")
    result_set = {
        "schema_version": 1,
        "result_set_policy_version": 1,
        "result_set_type": "stage12_baseline",
        "experiment_mode": mode,
        "experiment_contract_path": seal["contract_path"],
        "experiment_contract_sha256": seal["contract_sha256"],
        "sealed_candidate_manifest_path": "stage-10/selected_candidate_manifest.json",
        "sealed_candidate_manifest_sha256": seal_sha256,
        "run_config_path": seal["run_config_path"],
        "run_config_sha256": seal["run_config_sha256"],
        "config_semantic_policy_version": seal["config_semantic_policy_version"],
        "config_semantic_sha256": seal["config_semantic_sha256"],
        "claim_scope": contract.claim_scope,
        "dataset_origin": contract.dataset_origin,
        "evaluator_schema": evaluator_schema,
        "metric_authority": contract.metric_authority,
        "invocation_journal": {
            "path": "stage-12/execution_invocation_journal.jsonl",
            "sha256": sha256_text(journal_text),
        },
        "execution_statuses": [{
            "ordinal": 1,
            "status": "completed",
            "result_path": "stage-12/evidence-v1/run-1.json",
            "failure_code": None,
        }],
        "evidence_files": [
            {
                "path": "stage-12/evidence-v1/results.json",
                "sha256": sha256_text(aggregate_text),
            },
            {
                "path": "stage-12/evidence-v1/run-1.json",
                "sha256": sha256_text(invocation_text),
            },
        ],
    }
    result_set_text = canonical_json_text(result_set)
    try:
        parse_experiment_result_set(result_set_text)
        validate_experiment_result_set(run_dir, config, result_set_text)
        controller.write_text_atomic("experiment_result_set.json", result_set_text)
        controller.assert_canonical()
        validate_experiment_result_set(run_dir, config)
    except Exception as exc:  # noqa: BLE001
        cleanup_error = ""
        try:
            controller.remove_tree_entries(
                ("experiment_result_set.json", "evidence-v1")
            )
        except OSError as cleanup_exc:
            cleanup_error = f"; canonical evidence cleanup failed: {cleanup_exc}"
        return finish(StageResult(
            stage=Stage.EXPERIMENT_RUN,
            status=StageStatus.FAILED,
            artifacts=("execution_invocation_journal.jsonl", "diagnostics/"),
            evidence_refs=(),
            error=f"Canonical Stage 12 manifest replay failed: {exc}{cleanup_error}",
        ))
    return finish(StageResult(
        stage=Stage.EXPERIMENT_RUN,
        status=StageStatus.DONE,
        artifacts=(
            "experiment_result_set.json",
            "execution_invocation_journal.jsonl",
            "evidence-v1/",
            "diagnostics/",
        ),
        evidence_refs=("stage-12/experiment_result_set.json",),
    ))


def _execute_iterative_refine(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """Publish the C1-B canonical Stage 13 refinement result set."""
    del adapters, prompts
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities("stage13.execute_iterative_refine")
    from researchclaw.experiment.factory import create_sandbox

    controller: CanonicalRefinementController | None = None
    publication_workspace: Path | None = None

    def finish(result: StageResult) -> StageResult:
        if publication_workspace is not None:
            shutil.rmtree(publication_workspace, ignore_errors=True)
        if controller is not None:
            controller.close()
        return result

    try:
        controller = CanonicalRefinementController.prepare_generation(run_dir, stage_dir)
    except (OSError, RuntimeError) as exc:
        return finish(StageResult(
            stage=Stage.ITERATIVE_REFINE,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=f"Canonical Stage 13 generation preparation failed: {exc}",
        ))

    publication_workspace = Path(
        tempfile.mkdtemp(prefix="researchclaw-stage13-publish-")
    ).resolve()
    stage_dir = publication_workspace

    evidence_dir = stage_dir / "evidence-v1"
    evidence_staging = stage_dir / ".evidence-v1.staging"
    diagnostics_dir = stage_dir / "diagnostics"
    refinement_log_path = stage_dir / "refinement_log.json"
    final_dir = stage_dir / "experiment_final"
    final_staging = stage_dir / ".experiment_final.staging"

    def fail(message: str) -> StageResult:
        cleanup_error = ""
        try:
            controller.remove_tree_entries(
                (
                    "refinement_result_set.json",
                    "evidence-v1",
                    "experiment_final",
                    "experiment_final.py",
                    "refinement_log.json",
                )
            )
        except OSError as exc:
            cleanup_error = f"; canonical cleanup failed: {exc}"
        failure_text = canonical_json_text({
            "error": message,
            "generated": _utcnow_iso(),
            "result_set_published": False,
        })
        try:
            controller.write_text_atomic("refinement_failure.json", failure_text)
        except OSError as exc:
            cleanup_error += f"; diagnostic write failed: {exc}"
        return finish(StageResult(
            stage=Stage.ITERATIVE_REFINE,
            status=StageStatus.FAILED,
            artifacts=("refinement_failure.json",),
            evidence_refs=(),
            error=f"{message}{cleanup_error}",
        ))

    if config.experiment.mode not in {"sandbox", "docker"}:
        return fail(
            f"Canonical Stage 13 does not support experiment mode: {config.experiment.mode}"
        )

    try:
        baseline_path = run_dir / "stage-12/experiment_result_set.json"
        baseline_text = baseline_path.read_text(encoding="utf-8")
        baseline = validate_experiment_result_set(run_dir, config, baseline_text)
        seal_path = run_dir / "stage-10/selected_candidate_manifest.json"
        seal_text = seal_path.read_text(encoding="utf-8")
        seal = validate_selected_candidate_manifest(run_dir, config, seal_text)
        contract = load_contract(run_dir / seal["contract_path"])
        if baseline["sealed_candidate_manifest_sha256"] != sha256_text(seal_text):
            raise CanonicalExperimentEvidenceError("Stage 12/13 seal binding mismatch")
        if baseline["config_semantic_sha256"] != semantic_config_sha256(config):
            raise CanonicalExperimentEvidenceError("active config differs from Stage 12 baseline")
        if baseline["evaluator_schema"] != "hpc_anomaly_detection_v1":
            raise CanonicalExperimentEvidenceError("unsupported Stage 13 evaluator schema")
        metric_key = contract.primary_metric.get("key")
        direction = contract.primary_metric.get("direction")
        if not isinstance(metric_key, str) or not metric_key:
            raise CanonicalExperimentEvidenceError("contract primary metric key is invalid")
        if direction not in {"maximize", "minimize"}:
            raise CanonicalExperimentEvidenceError("contract metric direction is invalid")
        if config.experiment.metric_key != metric_key or config.experiment.metric_direction != direction:
            raise CanonicalExperimentEvidenceError("Stage 13 metric policy differs from contract")
        baseline_value = stage12_primary_metric(run_dir, baseline, metric_key)
        selected_root = run_dir / "stage-10/selected_candidate"
        sealed_bytes: dict[str, bytes] = {}
        for name, metadata in sorted(seal["files"].items()):
            path = selected_root / name
            if path.is_symlink() or not path.is_file() or sha256_file(path) != metadata["sha256"]:
                raise CanonicalExperimentEvidenceError("Stage 10 selected project changed")
            sealed_bytes[name] = path.read_bytes()
        model_owned = frozenset(seal["plugin_files"])
        if not model_owned:
            raise CanonicalExperimentEvidenceError("Stage 13 has no model-owned files")
    except (
        OSError,
        UnicodeDecodeError,
        RuntimeError,
        ContractValidationError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        return fail(f"Canonical Stage 13 preflight failed: {exc}")

    best_files = dict(sealed_bytes)
    best_value = baseline_value
    selected_id: str | None = None
    iterations: list[dict[str, Any]] = []
    iteration_log: list[dict[str, Any]] = []
    no_improvement = 0
    requested_iterations = int(getattr(config.experiment, "max_iterations", 10) or 10)
    max_iterations = max(1, min(requested_iterations, 10)) if llm is not None else 0

    def write_project(root: Path, files: dict[str, bytes]) -> None:
        root.mkdir(parents=True, exist_ok=False)
        for name, payload in sorted(files.items()):
            path = root / name
            path.write_bytes(payload)

    def project_refs(iteration_id: str, files: dict[str, bytes]) -> list[dict[str, str]]:
        return [
            {
                "path": (
                    f"stage-13/evidence-v1/iterations/{iteration_id}/project/{name}"
                ),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files.items())
        ]

    def model_context(files: dict[str, bytes]) -> str:
        blocks: list[str] = []
        for name in sorted(model_owned):
            try:
                source = files[name].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CanonicalExperimentEvidenceError(
                    f"model-owned file is not UTF-8: {name}"
                ) from exc
            blocks.append(f"```filename:{name}\n{source}\n```")
        return "\n\n".join(blocks)

    def extract_proposal(response_text: str, files: dict[str, bytes]) -> dict[str, bytes]:
        pattern = re.compile(
            r"```(?:python\s+)?filename:([^\s`]+)\s*\n(.*?)```",
            flags=re.DOTALL,
        )
        matches = list(pattern.finditer(response_text))
        extracted: dict[str, str] = {}
        for match in matches:
            name = match.group(1)
            if name in extracted:
                raise CanonicalExperimentEvidenceError(
                    "Stage 13 response contains a duplicate filename"
                )
            extracted[name] = match.group(2).strip()
        residue = pattern.sub("", response_text).strip()
        if matches and residue:
            raise CanonicalExperimentEvidenceError(
                "Stage 13 response contains prose outside file fences"
            )
        if not extracted:
            raise CanonicalExperimentEvidenceError("Stage 13 response contains no project files")
        if not set(extracted).issubset(model_owned):
            raise CanonicalExperimentEvidenceError(
                "Stage 13 response attempted to modify a scaffold-owned or unknown file"
            )
        updated = dict(files)
        changed = False
        for name, source in extracted.items():
            if not isinstance(source, str) or not source.strip():
                raise CanonicalExperimentEvidenceError("Stage 13 returned an empty project file")
            payload = source.encode("utf-8")
            changed = changed or payload != updated[name]
            updated[name] = payload
        if not changed:
            raise CanonicalExperimentEvidenceError("Stage 13 proposal made no byte change")
        return updated

    try:
        evidence_staging.mkdir()
        diagnostics_dir.mkdir()
        for ordinal in range(1, max_iterations + 1):
            iteration_id = f"iter-{ordinal}"
            system = (
                "You improve only the model-owned files of a sealed experiment. "
                "Do not output main.py or any scaffold-owned file. Return only complete "
                "updated files using filename fences. Preserve the DetectorPlugin API. "
                "Do not emit prose outside the file fences."
            )
            user = (
                f"Primary metric: {metric_key}; direction: {direction}.\n"
                f"Current authoritative metric: {canonical_decimal(best_value)}.\n"
                f"Allowed files: {', '.join(sorted(model_owned))}.\n\n"
                f"Current model-owned files:\n{model_context(best_files)}"
            )
            response = _chat_with_prompt(
                llm,
                system,
                user,
                max_tokens=8192,
                retries=1,
            )
            candidate_files = extract_proposal(response.content, best_files)
            iteration_staging = evidence_staging / "iterations" / iteration_id
            project_root = iteration_staging / "project"
            write_project(project_root, candidate_files)
            refs = project_refs(iteration_id, candidate_files)
            validation = build_refinement_validation_report(project_root, refs)
            validation_text = canonical_json_text(validation)
            _atomic_write_text(iteration_staging / "validation_report.json", validation_text)
            if not validation["checks"]["python_syntax_valid"]:
                raise CanonicalExperimentEvidenceError(
                    f"{iteration_id} has invalid Python syntax"
                )

            sandbox_root = diagnostics_dir / iteration_id / "initial"
            sandbox_root.mkdir(parents=True)
            sandbox = create_sandbox(
                config.experiment,
                sandbox_root,
                metadata_dir=stage_dir,
            )
            expected_backend = (
                "subprocess" if config.experiment.mode == "sandbox" else "docker"
            )
            if getattr(sandbox, "backend_kind", None) != expected_backend:
                raise CanonicalExperimentEvidenceError(
                    "canonical Stage 13 sandbox backend mismatch"
                )
            result = sandbox.run_project(
                project_root,
                timeout_sec=config.experiment.time_budget_sec,
            )
            if result.returncode != 0 or result.timed_out:
                raise CanonicalExperimentEvidenceError(
                    f"{iteration_id} sandbox execution failed"
                )
            output_dir = getattr(result, "output_dir", None)
            if (
                not isinstance(output_dir, Path)
                or output_dir.is_symlink()
                or not output_dir.is_dir()
                or output_dir.parent != sandbox_root
                or output_dir.resolve().parent != sandbox_root.resolve()
                or set(sandbox_root.iterdir()) != {output_dir}
            ):
                raise CanonicalExperimentEvidenceError(
                    f"{iteration_id} sandbox output is not isolated"
                )
            evaluator_path = output_dir / "results.json"
            if evaluator_path.is_symlink() or not evaluator_path.is_file():
                raise CanonicalExperimentEvidenceError(
                    f"{iteration_id} evaluator results are missing"
                )
            execution_text = build_refinement_execution_text(
                evaluator_path.read_text(encoding="utf-8"),
                contract=contract,
                evaluator_schema=baseline["evaluator_schema"],
            )
            _atomic_write_text(iteration_staging / "initial_execution.json", execution_text)
            execution = parse_invocation_result(execution_text)
            observations = execution["metric_observations"].get(metric_key)
            if not isinstance(observations, list) or len(observations) != 1:
                raise CanonicalExperimentEvidenceError(
                    f"{iteration_id} primary metric is missing or nonsingular"
                )
            observed = Decimal(canonical_decimal(observations[0]))
            iteration = {
                "ordinal": ordinal,
                "iteration_id": iteration_id,
                "project_files": refs,
                "validation_report": {
                    "path": (
                        f"stage-13/evidence-v1/iterations/{iteration_id}/"
                        "validation_report.json"
                    ),
                    "sha256": sha256_text(validation_text),
                },
                "initial_execution": {
                    "path": (
                        f"stage-13/evidence-v1/iterations/{iteration_id}/"
                        "initial_execution.json"
                    ),
                    "sha256": sha256_text(execution_text),
                },
                "runtime_repair": None,
                "accepted": True,
                "rejection_codes": [],
                "primary_metric_observation": observed,
            }
            iterations.append(iteration)
            improved = observed > best_value if direction == "maximize" else observed < best_value
            iteration_log.append({
                "iteration_id": iteration_id,
                "accepted": True,
                "primary_metric_observation": canonical_decimal(observed),
                "improved": improved,
            })
            if improved:
                best_files = dict(candidate_files)
                best_value = observed
                selected_id = iteration_id
                no_improvement = 0
            else:
                no_improvement += 1
            if no_improvement >= 2:
                break

        refinement_log = {
            "schema_version": 1,
            "diagnostic_only": True,
            "metric_key": metric_key,
            "optimization_direction": direction,
            "baseline_metric": canonical_decimal(baseline_value),
            "iterations": iteration_log,
            "selected_result": {
                "type": "iteration" if selected_id is not None else "baseline",
                "iteration_id": selected_id,
            },
        }
        refinement_log_text = canonical_json_text(refinement_log)
        _atomic_write_text(refinement_log_path, refinement_log_text)
        if evidence_dir.exists() or evidence_dir.is_symlink():
            raise CanonicalExperimentEvidenceError(
                "canonical Stage 13 evidence namespace already exists"
            )
        os.replace(evidence_staging, evidence_dir)

        selected_files = best_files if selected_id is not None else sealed_bytes
        write_project(final_staging, selected_files)
        if final_dir.exists() or final_dir.is_symlink():
            raise CanonicalExperimentEvidenceError(
                "Stage 13 compatibility namespace already exists"
            )
        os.replace(final_staging, final_dir)
        if "main.py" in selected_files:
            _atomic_write_text(
                stage_dir / "experiment_final.py",
                selected_files["main.py"].decode("utf-8"),
            )

        result_set = {
            "schema_version": 1,
            "refinement_policy_version": 1,
            "result_set_type": "stage13_refinement",
            "baseline_manifest": {
                "path": "stage-12/experiment_result_set.json",
                "sha256": sha256_text(baseline_text),
            },
            "experiment_contract_path": baseline["experiment_contract_path"],
            "experiment_contract_sha256": baseline["experiment_contract_sha256"],
            "sealed_candidate_manifest_path": (
                baseline["sealed_candidate_manifest_path"]
            ),
            "sealed_candidate_manifest_sha256": (
                baseline["sealed_candidate_manifest_sha256"]
            ),
            "run_config_path": baseline["run_config_path"],
            "run_config_sha256": baseline["run_config_sha256"],
            "config_semantic_policy_version": (
                baseline["config_semantic_policy_version"]
            ),
            "config_semantic_sha256": baseline["config_semantic_sha256"],
            "claim_scope": baseline["claim_scope"],
            "dataset_origin": baseline["dataset_origin"],
            "evaluator_schema": baseline["evaluator_schema"],
            "metric_authority": baseline["metric_authority"],
            "primary_metric_key": metric_key,
            "optimization_direction": direction,
            "iterations": iterations,
            "refinement_log": {
                "path": "stage-13/refinement_log.json",
                "sha256": sha256_text(refinement_log_text),
            },
            "selected_result": {
                "type": "iteration" if selected_id is not None else "baseline",
                "iteration_id": selected_id,
            },
        }
        result_set_text = canonical_authority_json_text(result_set)
        parse_refinement_result_set(result_set_text)
        controller.publish_directory_tree("evidence-v1", evidence_dir)
        controller.publish_directory_tree("experiment_final", final_dir)
        controller.publish_directory_tree("diagnostics", diagnostics_dir)
        controller.write_text_atomic(
            "refinement_log.json", refinement_log_path.read_text(encoding="utf-8")
        )
        final_py = stage_dir / "experiment_final.py"
        if final_py.exists():
            controller.write_text_atomic(
                "experiment_final.py", final_py.read_text(encoding="utf-8")
            )
        controller.assert_canonical()
        validate_refinement_result_set(run_dir, config, result_set_text)
        controller.write_text_atomic("refinement_result_set.json", result_set_text)
        controller.assert_canonical()
        validate_refinement_result_set(run_dir, config)
    except Exception as exc:  # noqa: BLE001
        return fail(f"Canonical Stage 13 publication failed: {exc}")

    artifacts = (
        "refinement_result_set.json",
        "refinement_log.json",
        "evidence-v1/",
        "experiment_final/",
        "experiment_final.py",
        "diagnostics/",
    )
    return finish(StageResult(
        stage=Stage.ITERATIVE_REFINE,
        status=StageStatus.DONE,
        artifacts=artifacts,
        evidence_refs=("stage-13/refinement_result_set.json",),
    ))
