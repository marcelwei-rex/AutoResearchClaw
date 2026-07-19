from __future__ import annotations

import json
import logging
import math
import re
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.hardware import HardwareProfile, detect_hardware, ensure_torch_available, is_metric_name
from researchclaw.llm import create_llm_client
from researchclaw.llm.client import LLMClient
from researchclaw.prompts import PromptManager
from researchclaw.pipeline.stages import (
    NEXT_STAGE,
    SKIP_FORBIDDEN_STAGES,
    Stage,
    StageStatus,
    TransitionEvent,
    TransitionOutcome,
    advance,
    gate_required,
)
from researchclaw.pipeline.contracts import CONTRACTS, StageContract
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.experiment.validator import (
    CodeValidation,
    format_issues_for_llm,
    validate_code,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from researchclaw.experiment_runtime.metric_authority import (
        MetricAuthoritySelection,
    )


class _DeferredLLMClient:
    """Construct an LLM client only if a legacy stage actually sends a chat."""

    def __init__(self, factory: Callable[[], LLMClient | None]) -> None:
        self._factory = factory
        self._client: LLMClient | None = None
        self._initialized = False

    def resolve_for_legacy(self) -> LLMClient | None:
        if not self._initialized:
            self._client = self._factory()
            self._initialized = True
        return self._client

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        client = self.resolve_for_legacy()
        if client is None:
            raise RuntimeError("LLM client is unavailable")
        return client.chat(*args, **kwargs)


def _create_configured_llm(config: RCConfig) -> LLMClient | None:
    try:
        if config.llm.provider == "acp":
            return create_llm_client(config)
        candidate = LLMClient.from_rc_config(config)
        if candidate.config.base_url and candidate.config.api_key:
            return candidate
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM client creation failed: %s", exc)
    return None


@dataclass
class _MissingAuthorityNamespace:
    """Record that no run namespace existed before an authority HITL wait."""

    run_dir: Path

    def assert_canonical(self) -> None:
        try:
            self.run_dir.lstat()
        except FileNotFoundError:
            return
        raise OSError("run directory appeared during authority HITL wait")

    def close(self) -> None:
        return None


def _select_output_files(contract, config) -> tuple[str, ...]:
    """Pick the contract's collider-mode outputs when running collider_agent."""
    if contract is None:
        return ()
    mode = getattr(getattr(config, "experiment", None), "mode", "") or ""
    alt = getattr(contract, "collider_output_files", ()) or ()
    if mode == "collider_agent" and alt:
        return tuple(alt)
    return tuple(contract.output_files)


def _domain_evaluator_output_contract(
    stage: Stage,
    run_dir: Path,
    config: RCConfig,
) -> tuple[str, ...] | None:
    """Derive fixed-evaluator outputs from replayed authority, never producer claims."""

    if stage is Stage.CODE_GENERATION:
        from researchclaw.pipeline.stage10_evaluator_capture import (
            load_stage10_contract,
        )

        _path, authority = load_stage10_contract(run_dir)
        if authority.schema_version != 3:
            return None
        return ("evaluator-capture-v1/", "selected_candidate_manifest.json")
    if stage is Stage.EXPERIMENT_RUN:
        from researchclaw.pipeline.canonical_experiment_evidence import (
            validate_selected_candidate_manifest,
        )

        authority = validate_selected_candidate_manifest(run_dir, config)
        if authority["schema_version"] != 3:
            return None
        return (
            "experiment_result_set.json",
            "execution_invocation_journal.jsonl",
            "evidence-v2/",
        )
    if stage is Stage.ITERATIVE_REFINE:
        from researchclaw.pipeline.canonical_experiment_evidence import (
            validate_experiment_result_set,
        )

        authority = validate_experiment_result_set(run_dir, config)
        if authority["result_set_type"] != "stage12_domain_evaluator":
            return None
        return ("refinement_result_set.json",)
    if stage is Stage.RESULT_ANALYSIS:
        from researchclaw.pipeline.canonical_experiment_evidence import (
            validate_canonical_experiment_manifest,
        )

        authority = validate_canonical_experiment_manifest(run_dir, config)
        if not (
            authority.get("schema_version") == 2
            and authority.get("generation_kind") == "domain_evaluator"
        ):
            return None
        manifest_path = authority["selected_candidate"]["manifest"]["path"]
        match = re.fullmatch(
            r"stage-14/(evidence_candidates/cand-[0-9a-f]{64}/"
            r"experiment_evidence_candidate\.json)",
            manifest_path,
        )
        if match is None:
            raise ValueError("invalid Stage 14 domain evaluator artifact path")
        return (match.group(1),)
    return None


def _invalidate_failed_domain_evaluator_authority(
    stage: Stage,
    run_dir: Path,
    lease: object,
) -> None:
    """Withdraw a canonical generation whose executor postconditions failed."""

    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    release_lock = require_active_writer_invalidation_epoch(run_dir, lease)
    errors: list[str] = []

    def attempt(label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    if stage is Stage.CODE_GENERATION:
        from researchclaw.pipeline.stage10_evaluator_capture import (
            clear_stage10_candidate_authority,
        )

        def clear_stage10() -> None:
            with release_lock.open_stage_namespace(
                "stage-10", create_stage=True
            ) as namespace:
                clear_stage10_candidate_authority(namespace)
                namespace.assert_canonical()

        attempt("Stage 10 authority", clear_stage10)
        attempt(
            "downstream commit points",
            release_lock.invalidate_experiment_commit_points,
        )
    elif stage is Stage.EXPERIMENT_RUN:
        from researchclaw.pipeline.stage12_domain_evaluator import _reset_stage12

        def clear_stage12() -> None:
            with release_lock.open_stage_namespace(
                "stage-12", create_stage=True
            ) as namespace:
                _reset_stage12(namespace)
                namespace.assert_canonical()

        attempt("Stage 12 authority", clear_stage12)
        attempt(
            "downstream commit points",
            release_lock.invalidate_experiment_commit_points,
        )
    elif stage is Stage.ITERATIVE_REFINE:
        def clear_stage13() -> None:
            with release_lock.open_stage_namespace(
                "stage-13", create_stage=True
            ) as namespace:
                namespace.quarantine_tree_entry("refinement_result_set.json")
                namespace.assert_canonical()

        attempt("Stage 13 authority", clear_stage13)
        attempt(
            "root commit points",
            lambda: release_lock.invalidate_experiment_commit_points(
                include_stage12=False
            ),
        )
    elif stage is Stage.RESULT_ANALYSIS:
        attempt(
            "root commit points",
            lambda: release_lock.invalidate_experiment_commit_points(
                include_stage12=False, include_stage13=False
            ),
        )

        def clear_stage14_candidates() -> None:
            with release_lock.open_stage_namespace(
                "stage-14", create_stage=True
            ) as namespace:
                namespace.quarantine_tree_entry("evidence_candidates")
                namespace.assert_canonical()

        attempt("Stage 14 candidates", clear_stage14_candidates)
    if errors:
        raise RuntimeError(
            "canonical authority invalidation incomplete: " + "; ".join(errors)
        )


def _append_authority_cleanup_error(
    result: StageResult,
    cleanup_error: Exception,
) -> StageResult:
    message = result.error or "Canonical executor postcondition failed"
    return StageResult(
        stage=result.stage,
        status=result.status,
        artifacts=result.artifacts,
        error=f"{message}; canonical authority cleanup failed: {cleanup_error}",
        decision=result.decision,
        evidence_refs=result.evidence_refs,
    )


def _finalize_fixed_stage9_authority(
    result: StageResult,
    run_dir: Path,
    config: RCConfig,
    lease: object,
    namespace: BoundOutputNamespace,
) -> StageResult:
    """Replay approved Stage 9 authority or withdraw every failed terminal state."""

    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )
    from researchclaw.pipeline.stage_impls._experiment_design import (
        _cleanup_stage9_outputs,
        _validate_stage9_publication,
    )

    _ = require_active_writer_invalidation_epoch(run_dir, lease)
    if namespace.run_dir != run_dir or namespace.stage_name != "stage-09":
        raise RuntimeError("fixed Stage 9 finalizer namespace mismatch")
    terminal = result
    if terminal.status is StageStatus.DONE:
        try:
            _validate_stage9_publication(namespace, run_dir, config)
            namespace.assert_canonical()
            return terminal
        except Exception as exc:  # noqa: BLE001
            terminal = StageResult(
                stage=result.stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Fixed Stage 9 terminal replay failed: {exc}",
                decision="abort",
                evidence_refs=(),
            )

    if terminal.artifacts or terminal.evidence_refs:
        terminal = StageResult(
            stage=terminal.stage,
            status=terminal.status,
            artifacts=(),
            error=terminal.error,
            decision=terminal.decision,
            evidence_refs=(),
        )

    try:
        _cleanup_stage9_outputs(namespace)
        namespace.assert_canonical()
    except Exception as cleanup_exc:  # noqa: BLE001
        return _append_authority_cleanup_error(terminal, cleanup_exc)
    return StageResult(
        stage=terminal.stage,
        status=terminal.status,
        artifacts=(),
        error=terminal.error,
        decision=terminal.decision,
        evidence_refs=(),
    )

# ---------------------------------------------------------------------------
# Domain detection (extracted to _domain.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline._domain import (  # noqa: E402
    _detect_domain,
    _is_ml_domain,
    _prompt_bank_domain_from_config,
)


# ---------------------------------------------------------------------------
# Shared helpers (extracted to _helpers.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline._helpers import (  # noqa: E402
    StageResult,
    _SANDBOX_SAFE_PACKAGES,
    _STOP_WORDS,
    _build_context_preamble,
    _build_fallback_queries,
    _chat_with_prompt,
    _collect_experiment_results,
    _collect_json_context,
    _default_hypotheses,
    _default_paper_outline,
    _default_quality_report,
    _detect_runtime_issues,
    _ensure_sandbox_deps,
    _extract_code_block,
    _extract_multi_file_blocks,
    _extract_paper_title,
    _extract_topic_keywords,
    _extract_yaml_block,
    _find_prior_file,
    _generate_framework_diagram_prompt,
    _generate_neurips_checklist,
    _get_evolution_overlay,
    _load_hardware_profile,
    _multi_perspective_generate,
    _parse_jsonl_rows,
    _parse_metrics_from_stdout,
    _read_prior_artifact,
    _safe_filename,
    _safe_json_loads,
    _synthesize_perspectives,
    _topic_constraint_block,
    _utcnow_iso,
    _write_jsonl,
    _write_stage_meta,
    reconcile_figure_refs,
)

# ---------------------------------------------------------------------------
# Stages 1-2 (extracted to stage_impls/_topic.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._topic import (  # noqa: E402
    _execute_topic_init,
    _execute_problem_decompose,
)

# ---------------------------------------------------------------------------
# Stages 3-6 (extracted to stage_impls/_literature.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._literature import (  # noqa: E402
    _execute_search_strategy,
    _execute_literature_collect,
    _execute_literature_screen,
    _execute_knowledge_extract,
    _expand_search_queries,
)

# ---------------------------------------------------------------------------
# Stages 7-8 (extracted to stage_impls/_synthesis.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._synthesis import (  # noqa: E402
    _execute_synthesis,
    _execute_hypothesis_gen,
)

# ---------------------------------------------------------------------------
# Stage 9 (extracted to stage_impls/_experiment_design.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._experiment_design import (  # noqa: E402
    _execute_experiment_design,
)

# ---------------------------------------------------------------------------
# Stage 10 (extracted to stage_impls/_code_generation.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._code_generation import (  # noqa: E402
    _execute_code_generation,
)

# ---------------------------------------------------------------------------
# Stages 11-13 (extracted to stage_impls/_execution.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._execution import (  # noqa: E402
    _execute_resource_planning,
    _execute_experiment_run,
    _execute_iterative_refine,
)

# ---------------------------------------------------------------------------
# Stages 14-15 (extracted to stage_impls/_analysis.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._analysis import (  # noqa: E402
    _execute_result_analysis,
    _parse_decision,
    _execute_research_decision,
)

# ---------------------------------------------------------------------------
# Stages 16-17 (extracted to stage_impls/_paper_writing.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._paper_writing import (  # noqa: E402
    _execute_paper_outline,
    _execute_paper_draft,
    _collect_raw_experiment_metrics,
    _write_paper_sections,
    _validate_draft_quality,
    _review_compiled_pdf,
    _check_ablation_effectiveness,
    _detect_result_contradictions,
    _BULLET_LENIENT_SECTIONS,
    _BALANCE_SECTIONS,
)

# ---------------------------------------------------------------------------
# Stages 18-23 (extracted to stage_impls/_review_publish.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._review_publish import (  # noqa: E402
    _execute_peer_review,
    _execute_paper_revision,
    _execute_quality_gate,
    _execute_knowledge_archive,
    _execute_export_publish,
    _execute_citation_verify,
    _sanitize_fabricated_data,
    _collect_experiment_evidence,
    _check_citation_relevance,
    _remove_bibtex_entries,
)

# ---------------------------------------------------------------------------
# Stages 24-25 (release audit, stage_impls/_release_audit.py)
# ---------------------------------------------------------------------------
from researchclaw.pipeline.stage_impls._release_audit import (  # noqa: E402
    _execute_truth_audit,
    _execute_deai_audit,
)


def _get_hitl_session(adapters: AdapterBundle) -> Any:
    """Retrieve the HITLSession from the adapter bundle (if any)."""
    return getattr(adapters, "hitl", None)


def _capture_hitl_authority_namespace(
    stage: Stage,
    run_dir: Path,
    *,
    fixed_stage9_authority: bool = False,
    release_lock: object | None = None,
) -> BoundOutputNamespace | _MissingAuthorityNamespace | None:
    """Hold deterministic authority identity across one HITL wait."""

    if fixed_stage9_authority:
        from researchclaw.pipeline.release_graph_lock import (
            require_active_writer_invalidation_epoch,
        )

        writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
        return writer.open_stage_namespace("stage-09")

    if stage is not Stage.DEAI_AUDIT:
        return None
    try:
        run_dir.lstat()
    except FileNotFoundError:
        return _MissingAuthorityNamespace(run_dir)
    return BoundOutputNamespace.open(
        run_dir,
        run_dir / "stage-25",
        "stage-25",
        create_stage=True,
    )


def _invalidate_hitl_authority_stage(
    stage: Stage,
    run_dir: Path,
    authority_namespace: BoundOutputNamespace | _MissingAuthorityNamespace | None = None,
) -> None:
    """Remove stale authority when HITL prevents an evidence stage commit."""

    if stage is Stage.DEAI_AUDIT and authority_namespace is None:
        raise OSError("Stage 25 HITL invalidation requires a held namespace")
    if isinstance(authority_namespace, _MissingAuthorityNamespace):
        authority_namespace.assert_canonical()
        return

    if authority_namespace is not None and stage is Stage.EXPERIMENT_DESIGN:
        from researchclaw.pipeline.stage_impls._experiment_design import (
            _cleanup_stage9_outputs,
        )

        _cleanup_stage9_outputs(authority_namespace)
        authority_namespace.assert_canonical()
        return

    if authority_namespace is not None and stage is Stage.DEAI_AUDIT:
        from researchclaw.pipeline.stage25_publication import _reset_namespace

        # Clean the held (possibly detached) inode before rejecting any live
        # path replacement. _reset_namespace performs the final identity check.
        _reset_namespace(authority_namespace)
        return

    def invalidate(namespace: BoundOutputNamespace) -> None:
        namespace.assert_canonical()
        if stage is Stage.TRUTH_AUDIT:
            from researchclaw.pipeline.stage24_publication import _reset_namespace

            _reset_namespace(namespace)
        else:
            namespace.reset_flat_namespace()
        namespace.assert_canonical()

    if authority_namespace is not None:
        invalidate(authority_namespace)
        return

    stage_name = f"stage-{int(stage):02d}"
    stage_dir = run_dir / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    with BoundOutputNamespace.open(run_dir, stage_dir, stage_name) as namespace:
        invalidate(namespace)


def _forbidden_hitl_result(
    stage: Stage,
    run_dir: Path,
    *,
    action: str,
    authority_namespace: BoundOutputNamespace | _MissingAuthorityNamespace | None = None,
) -> StageResult:
    try:
        _invalidate_hitl_authority_stage(stage, run_dir, authority_namespace)
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"HITL {action} is forbidden and authority invalidation failed: {exc}",
            decision="abort",
        )
    return StageResult(
        stage=stage,
        status=StageStatus.FAILED,
        artifacts=(),
        error=f"HITL {action} is forbidden for evidence-authority stage {stage.name}",
        decision="abort",
    )


def _guard_authority_human_input(
    stage: Stage,
    run_dir: Path,
    human_input: Any,
    authority_namespace: BoundOutputNamespace | _MissingAuthorityNamespace | None = None,
    *,
    fixed_stage9_authority: bool = False,
) -> StageResult | None:
    from researchclaw.hitl.intervention import HumanAction

    if stage is Stage.DEAI_AUDIT and authority_namespace is None:
        return _forbidden_hitl_result(
            stage,
            run_dir,
            action="missing held namespace",
            authority_namespace=None,
        )
    mutation_requested = bool(
        human_input.edited_files
        or (
            (stage is Stage.DEAI_AUDIT or fixed_stage9_authority)
            and human_input.guidance
        )
    )
    authority_hitl_forbidden = stage in SKIP_FORBIDDEN_STAGES or (
        stage is Stage.EXPERIMENT_DESIGN and fixed_stage9_authority
    )
    if authority_hitl_forbidden and (
        human_input.action != HumanAction.APPROVE or mutation_requested
    ):
        return _forbidden_hitl_result(
            stage,
            run_dir,
            action=f"post-stage {human_input.action.value.upper()}",
            authority_namespace=authority_namespace,
        )
    if (
        (stage is Stage.DEAI_AUDIT or fixed_stage9_authority)
        and authority_namespace is not None
    ):
        try:
            authority_namespace.assert_canonical()
        except OSError:
            return _forbidden_hitl_result(
                stage,
                run_dir,
                action="namespace replacement",
                authority_namespace=authority_namespace,
            )
    return None


def _run_hitl_pre_stage(
    stage: Stage, run_dir: Path, adapters: AdapterBundle,
    config: RCConfig | None = None,
    *,
    fixed_stage9_authority: bool = False,
    authority_namespace: BoundOutputNamespace | _MissingAuthorityNamespace | None = None,
) -> StageResult | None:
    """HITL pre-stage hook: pause before execution if policy requires.

    Returns a StageResult to skip the stage, or None to proceed normally.
    """
    session = _get_hitl_session(adapters)
    if session is None:
        return None

    stage_num = int(stage)
    if not session.should_pause_before(stage_num):
        return None

    from researchclaw.hitl.intervention import HumanAction, PauseReason

    # Collect output file names from contract (mode-aware: collider_agent → collider_plan.md)
    contract = CONTRACTS.get(stage)
    output_files = _select_output_files(contract, config)

    owns_authority_namespace = authority_namespace is None
    if owns_authority_namespace:
        try:
            authority_namespace = _capture_hitl_authority_namespace(stage, run_dir)
        except OSError as exc:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Cannot capture authority namespace before HITL wait: {exc}",
                decision="abort",
            )
    try:
        session.pause(
            stage_num,
            stage.name,
            PauseReason.PRE_STAGE,
            context_summary=f"About to execute {stage.name}",
            output_files=output_files,
        )
        human_input = session.wait_for_human()

        authority_guard = _guard_authority_human_input(
            stage,
            run_dir,
            human_input,
            authority_namespace,
            fixed_stage9_authority=fixed_stage9_authority,
        )
        if authority_guard is not None:
            return authority_guard

        if human_input.action == HumanAction.SKIP:
            return StageResult(
                stage=stage,
                status=StageStatus.DONE,
                artifacts=(),
                decision="proceed",
            )
        if human_input.action == HumanAction.ABORT:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error="Aborted by user",
                decision="abort",
            )

        # Inject guidance if provided. Stage 25 rejects guidance above because
        # its deterministic audit has no mutable prompt input.
        if human_input.guidance:
            if isinstance(authority_namespace, BoundOutputNamespace):
                authority_namespace.write_text_atomic(
                    "hitl_guidance.md", human_input.guidance
                )
            else:
                stage_dir = run_dir / f"stage-{stage_num:02d}"
                stage_dir.mkdir(parents=True, exist_ok=True)
                guidance_file = stage_dir / "hitl_guidance.md"
                guidance_file.write_text(human_input.guidance, encoding="utf-8")

        return None  # Proceed with execution
    finally:
        if owns_authority_namespace and authority_namespace is not None:
            authority_namespace.close()


def _run_hitl_post_stage(
    stage: Stage, result: StageResult, run_dir: Path, adapters: AdapterBundle,
    config: RCConfig | None = None,
    *,
    fixed_stage9_authority: bool = False,
    release_lock: object | None = None,
    stage9_namespace: BoundOutputNamespace | None = None,
) -> StageResult:
    """HITL post-stage hook: pause after execution for review.

    Returns the (possibly modified) StageResult.
    """
    session = _get_hitl_session(adapters)
    if session is None:
        return result

    # --- CostGuard: check budget thresholds ---
    stage_num = int(stage)
    try:
        from researchclaw.hitl.cost_guard import CostGuard

        budget = 0.0
        if hasattr(session, "config") and session.config:
            budget = getattr(session.config, "cost_budget_usd", 0.0) or 0.0
        guard = CostGuard(budget_usd=budget)
        if budget > 0 and guard.should_pause(run_dir):
            from researchclaw.hitl.intervention import HumanAction, PauseReason

            owns_authority_namespace = not (
                fixed_stage9_authority and stage9_namespace is not None
            )
            if owns_authority_namespace:
                try:
                    authority_namespace = _capture_hitl_authority_namespace(
                        stage,
                        run_dir,
                        fixed_stage9_authority=fixed_stage9_authority,
                        release_lock=release_lock,
                    )
                except OSError as exc:
                    return StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=(),
                        error=f"Cannot capture authority namespace before cost HITL: {exc}",
                        decision="abort",
                    )
            else:
                authority_namespace = stage9_namespace
            try:
                session.pause(
                    stage_num,
                    stage.name,
                    PauseReason.COST_BUDGET_EXCEEDED,
                    context_summary=f"Cost budget alert: {guard.format_display(run_dir)}",
                )
                human_input = session.wait_for_human()
                authority_result = _guard_authority_human_input(
                    stage,
                    run_dir,
                    human_input,
                    authority_namespace,
                    fixed_stage9_authority=fixed_stage9_authority,
                )
                if authority_result is not None:
                    return authority_result
                if human_input.action == HumanAction.ABORT:
                    return StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error="Aborted due to cost",
                        decision="abort",
                    )
            finally:
                if owns_authority_namespace and authority_namespace is not None:
                    authority_namespace.close()
    except Exception as _cg_exc:
        logger.debug("CostGuard check skipped: %s", _cg_exc)

    _smart_pause_triggered = False
    if not session.should_pause_after(stage_num):
        # Policy doesn't require pause, but SmartPause might
        try:
            from researchclaw.hitl.smart_pause import SmartPause

            sp = SmartPause(threshold=0.7, run_dir=run_dir)
            q_score = None
            stage_dir = run_dir / f"stage-{stage_num:02d}"
            prm_bytes = None
            if fixed_stage9_authority and stage9_namespace is not None:
                try:
                    prm_bytes = stage9_namespace.read_bytes("prm_score.json")
                except FileNotFoundError:
                    pass
            else:
                prm_file = stage_dir / "prm_score.json"
                if prm_file.exists():
                    prm_bytes = prm_file.read_bytes()
            if prm_bytes is not None:
                import json as _sp_json

                prm_data = _sp_json.loads(prm_bytes.decode("utf-8"))
                q_score = prm_data.get("prm_score")
            should_smart_pause, _signal = sp.should_pause(
                stage_num, stage.name, quality_score=q_score
            )
            if not should_smart_pause:
                return result
            # SmartPause triggered — fall through to pause logic
            _smart_pause_triggered = True
        except Exception as _sp_exc:
            logger.debug("SmartPause check skipped: %s", _sp_exc)
            return result

    from researchclaw.hitl.intervention import HumanAction, PauseReason

    # Determine pause reason
    reason = PauseReason.CONFIDENCE_LOW if _smart_pause_triggered else PauseReason.POST_STAGE
    policy = session.get_policy(stage_num)
    if policy.require_approval:
        reason = PauseReason.GATE_APPROVAL
    if (
        policy.min_quality_score > 0
        and result.status == StageStatus.DONE
    ):
        # Check quality score from stage health
        stage_dir = run_dir / f"stage-{stage_num:02d}"
        try:
            import json as _json_mod
            health_file = stage_dir / "stage_health.json"
            if health_file.exists():
                health = _json_mod.loads(health_file.read_text(encoding="utf-8"))
                prm_bytes = None
                if fixed_stage9_authority and stage9_namespace is not None:
                    try:
                        prm_bytes = stage9_namespace.read_bytes("prm_score.json")
                    except FileNotFoundError:
                        pass
                else:
                    prm_file = stage_dir / "prm_score.json"
                    if prm_file.exists():
                        prm_bytes = prm_file.read_bytes()
                if prm_bytes is not None:
                    prm = _json_mod.loads(prm_bytes.decode("utf-8"))
                    score = prm.get("prm_score", 1.0)
                    if score < policy.min_quality_score:
                        reason = PauseReason.QUALITY_BELOW_THRESHOLD
        except (OSError, ValueError):
            pass

    # Build context summary from stage artifacts
    contract = CONTRACTS.get(stage)
    output_files = _select_output_files(contract, config)
    context_lines = [
        f"Stage {stage_num} ({stage.name}) completed: {result.status.value}",
    ]
    if result.artifacts:
        context_lines.append(f"Artifacts: {', '.join(result.artifacts)}")
    if result.error:
        context_lines.append(f"Error: {result.error}")

    # Read first 500 chars of key output files for summary
    stage_dir = run_dir / f"stage-{stage_num:02d}"
    for fname in output_files[:3]:
        if fixed_stage9_authority and stage9_namespace is not None:
            try:
                text = stage9_namespace.read_bytes(fname).decode("utf-8")[:500]
                context_lines.append(f"\n--- {fname} ---\n{text}")
            except (OSError, UnicodeDecodeError, FileNotFoundError):
                pass
        else:
            fpath = stage_dir / fname
            if fpath.is_file():
                try:
                    text = fpath.read_text(encoding="utf-8")[:500]
                    context_lines.append(f"\n--- {fname} ---\n{text}")
                except (OSError, UnicodeDecodeError):
                    pass

    owns_authority_namespace = not (
        fixed_stage9_authority and stage9_namespace is not None
    )
    if owns_authority_namespace:
        try:
            authority_namespace = _capture_hitl_authority_namespace(
                stage,
                run_dir,
                fixed_stage9_authority=fixed_stage9_authority,
                release_lock=release_lock,
            )
        except OSError as exc:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Cannot capture authority namespace before post-stage HITL: {exc}",
                decision="abort",
            )
    else:
        authority_namespace = stage9_namespace
    try:
        session.pause(
            stage_num,
            stage.name,
            reason,
            context_summary="\n".join(context_lines),
            output_files=output_files,
        )
        human_input = session.wait_for_human()

        authority_result = _guard_authority_human_input(
            stage,
            run_dir,
            human_input,
            authority_namespace,
            fixed_stage9_authority=fixed_stage9_authority,
        )
        if authority_result is not None:
            return authority_result

        if human_input.action == HumanAction.APPROVE:
            return result

        if human_input.action == HumanAction.REJECT:
            return StageResult(
                stage=stage,
                status=StageStatus.REJECTED,
                artifacts=result.artifacts,
                error=human_input.message or "Rejected by human reviewer",
                decision="pivot",
                evidence_refs=result.evidence_refs,
            )

        if human_input.action == HumanAction.EDIT:
            # Human already edited files via the adapter
            return result

        if human_input.action == HumanAction.SKIP:
            return StageResult(
                stage=stage,
                status=StageStatus.DONE,
                artifacts=result.artifacts,
                decision="proceed",
                evidence_refs=result.evidence_refs,
            )

        if human_input.action == HumanAction.ABORT:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=result.artifacts,
                error="Aborted by user",
                decision="abort",
                evidence_refs=result.evidence_refs,
            )

        if human_input.action == HumanAction.COLLABORATE:
            session.enter_collaboration(stage_num, stage.name)
            try:
                result = _run_collaboration_loop(
                    stage, result, run_dir, adapters, session, config=config
                )
            except Exception as _collab_exc:
                logger.warning("Collaboration failed: %s", _collab_exc)
            session.exit_collaboration()
            return result

        if human_input.action == HumanAction.INJECT:
            # Save guidance for potential re-run
            if human_input.guidance:
                guidance_file = stage_dir / "hitl_guidance.md"
                guidance_file.write_text(human_input.guidance, encoding="utf-8")
            return result

        return result
    finally:
        if owns_authority_namespace and authority_namespace is not None:
            authority_namespace.close()


def _run_collaboration_loop(
    stage: Stage,
    result: StageResult,
    run_dir: Path,
    adapters: AdapterBundle,
    session: Any,
    *,
    config: RCConfig | None = None,
) -> StageResult:
    """Run an interactive collaboration loop for a stage.

    The human and AI take turns discussing and refining the stage output.
    The loop continues until the human approves or aborts.
    """
    from researchclaw.hitl.collaboration import CollaborationSession
    from researchclaw.hitl.intervention import HumanAction, PauseReason

    stage_num = int(stage)
    contract = CONTRACTS.get(stage)
    output_files = _select_output_files(contract, config)

    collab = CollaborationSession(run_dir=run_dir)

    # Try to get LLM client and topic
    llm_client = None
    topic = ""
    try:
        if config is not None:
            from researchclaw.llm import create_llm_client
            llm_client = create_llm_client(config)
            topic_obj = getattr(config, "research", None)
            topic = topic_obj.topic if topic_obj else "Research"
        else:
            topic = "Research"
    except Exception:
        topic = "Research"

    collab.initialize(
        stage_num, stage.name, topic, run_dir, artifacts=output_files,
    )

    print(f"\n  Entering collaboration mode for Stage {stage_num} ({stage.name})")
    print("  Commands: 'done' finalize | 'abort' cancel | 'show <file>' view | 'edit <file>' edit | 'files' list")
    print("  Or type a message to chat with AI.\n")

    # Simple collaboration loop via stdin
    while True:
        try:
            user_input = input("  You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        lower = user_input.lower()

        if lower in ("done", "approve", "finalize"):
            collab.finalize()
            print("  Collaboration finalized.")
            break

        if lower in ("abort", "quit", "cancel"):
            print("  Collaboration cancelled.")
            break

        # List available artifacts
        if lower == "files":
            for fname in collab.shared_artifacts:
                mod = " [modified]" if fname in collab._modified_artifacts else ""
                print(f"    {fname}{mod}")
            continue

        # Show artifact content
        if lower.startswith("show "):
            fname = user_input[5:].strip()
            if fname in collab.shared_artifacts:
                content = collab.shared_artifacts[fname]
                print(f"\n  --- {fname} ({len(content)} chars) ---")
                print(content[:3000])
                if len(content) > 3000:
                    print(f"  ... ({len(content) - 3000} chars truncated)")
                print(f"  --- end {fname} ---\n")
            else:
                print(f"  File not found: {fname}. Use 'files' to list available artifacts.")
            continue

        # Interactive edit: read from stdin until <<<END>>>
        if lower.startswith("edit "):
            fname = user_input[5:].strip()
            if fname not in collab.shared_artifacts:
                print(f"  File not found: {fname}. Use 'files' to list available artifacts.")
                continue
            print(f"  Editing {fname}. Paste new content, then type <<<END>>> on its own line:")
            lines = []
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() == "<<<END>>>":
                    break
                lines.append(line)
            new_content = "\n".join(lines)
            collab.human_edits_artifact(fname, new_content)
            print(f"  [{fname} updated — {len(new_content)} chars written]")
            continue

        # Regular chat message
        collab.human_says(user_input)

        # Get AI response
        if llm_client is not None:
            rev_before = len(collab.revision_history)
            response = collab.ai_responds(llm_client)
            print(f"\n  AI > {response}\n")
            # Report any artifact edits the AI made
            for rev in collab.revision_history[rev_before:]:
                if rev.get("action") == "ai_proposal":
                    print(f"  [AI edited: {rev['file']}]")
        else:
            print("  AI > [LLM not available for chat — your input is recorded]\n")

    return result


_STAGE_EXECUTORS: dict[Stage, Callable[..., StageResult]] = {
    Stage.TOPIC_INIT: _execute_topic_init,
    Stage.PROBLEM_DECOMPOSE: _execute_problem_decompose,
    Stage.SEARCH_STRATEGY: _execute_search_strategy,
    Stage.LITERATURE_COLLECT: _execute_literature_collect,
    Stage.LITERATURE_SCREEN: _execute_literature_screen,
    Stage.KNOWLEDGE_EXTRACT: _execute_knowledge_extract,
    Stage.SYNTHESIS: _execute_synthesis,
    Stage.HYPOTHESIS_GEN: _execute_hypothesis_gen,
    Stage.EXPERIMENT_DESIGN: _execute_experiment_design,
    Stage.CODE_GENERATION: _execute_code_generation,
    Stage.RESOURCE_PLANNING: _execute_resource_planning,
    Stage.EXPERIMENT_RUN: _execute_experiment_run,
    Stage.ITERATIVE_REFINE: _execute_iterative_refine,
    Stage.RESULT_ANALYSIS: _execute_result_analysis,
    Stage.RESEARCH_DECISION: _execute_research_decision,
    Stage.PAPER_OUTLINE: _execute_paper_outline,
    Stage.PAPER_DRAFT: _execute_paper_draft,
    Stage.PEER_REVIEW: _execute_peer_review,
    Stage.PAPER_REVISION: _execute_paper_revision,
    Stage.QUALITY_GATE: _execute_quality_gate,
    Stage.KNOWLEDGE_ARCHIVE: _execute_knowledge_archive,
    Stage.EXPORT_PUBLISH: _execute_export_publish,
    Stage.CITATION_VERIFY: _execute_citation_verify,
    Stage.TRUTH_AUDIT: _execute_truth_audit,
    Stage.DEAI_AUDIT: _execute_deai_audit,
}


_EXACT_AUTHORITY_NAMESPACE_STAGES = frozenset(
    {
        Stage.RESEARCH_DECISION,
        Stage.EXPORT_PUBLISH,
        Stage.CITATION_VERIFY,
        Stage.TRUTH_AUDIT,
        Stage.DEAI_AUDIT,
    }
)


def execute_stage(
    stage: Stage,
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
) -> StageResult:
    """Execute a stage inside the release writer epoch when it can alter authority."""

    if int(stage) < int(Stage.LITERATURE_COLLECT):
        return _execute_stage_under_release_scope(
            stage,
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adapters=adapters,
            auto_approve_gates=auto_approve_gates,
            release_lock=None,
        )
    from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock

    if int(stage) >= int(Stage.EXPERIMENT_RUN):
        from researchclaw.pipeline.canonical_evidence_capabilities import (
            require_canonical_evidence_capabilities,
        )

        require_canonical_evidence_capabilities(f"execute_stage.{stage.name}")
    with ReleaseGraphLock.acquire(
        run_dir, f"execute_stage.{stage.name}", mode="write"
    ) as release_lock:
        result = _execute_stage_under_release_scope(
            stage,
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adapters=adapters,
            auto_approve_gates=auto_approve_gates,
            release_lock=release_lock,
        )
        release_lock.assert_canonical()
        return result


def _execute_stage_under_release_scope(
    stage: Stage,
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
    release_lock: object | None = None,
) -> StageResult:
    """Bind fixed Stage 9 to one directory fd for its complete attempt."""

    if stage is not Stage.EXPERIMENT_DESIGN:
        return _execute_stage_under_release_scope_impl(
            stage,
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adapters=adapters,
            auto_approve_gates=auto_approve_gates,
            release_lock=release_lock,
        )

    from researchclaw.experiment_runtime.metric_authority import (
        select_metric_authority,
    )
    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )
    from researchclaw.pipeline.stage_impls._experiment_design import (
        _cleanup_stage9_outputs,
    )

    try:
        writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
        stage9_namespace = writer.open_stage_namespace(
            "stage-09", create_stage=True
        )
        try:
            _cleanup_stage9_outputs(stage9_namespace)
            stage9_namespace.assert_canonical()
            selection = select_metric_authority(
                config.research.topic, config.experiment.mode
            )
        except Exception:
            stage9_namespace.close()
            raise
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 9 attempt initialization failed: {exc}",
            decision="abort",
        )
    try:
        return _execute_stage_under_release_scope_impl(
            stage,
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adapters=adapters,
            auto_approve_gates=auto_approve_gates,
            release_lock=writer,
            fixed_stage9_authority=selection.schema_version == 2,
            stage9_namespace=stage9_namespace,
            stage9_authority_selection=selection,
        )
    finally:
        stage9_namespace.close()


def _execute_stage_under_release_scope_impl(
    stage: Stage,
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
    release_lock: object | None = None,
    fixed_stage9_authority: bool = False,
    stage9_namespace: BoundOutputNamespace | None = None,
    stage9_authority_selection: MetricAuthoritySelection | None = None,
) -> StageResult:
    """Execute one pipeline stage, validate outputs, and apply gate logic."""

    if int(stage) >= int(Stage.EXPERIMENT_RUN):
        from researchclaw.pipeline.canonical_evidence_capabilities import (
            require_canonical_evidence_capabilities,
        )

        require_canonical_evidence_capabilities(f"execute_stage.{stage.name}")

    if stage9_namespace is not None and stage9_authority_selection is None:
        return StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error="Stage 9 held namespace requires its captured authority selection",
            decision="abort",
        )

    # --- HITL pre-stage hook ---
    hitl_result = _run_hitl_pre_stage(
        stage,
        run_dir,
        adapters,
        config=config,
        fixed_stage9_authority=fixed_stage9_authority,
        authority_namespace=stage9_namespace,
    )
    if hitl_result is not None:
        return hitl_result

    stage_dir = (
        stage9_namespace.stage_dir
        if stage9_namespace is not None
        else run_dir / f"stage-{int(stage):02d}"
    )
    if stage9_namespace is None:
        if stage_dir.is_symlink():
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Stage output directory is a symlink: {stage_dir.name}",
            )
        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Cannot create stage output directory: {exc}",
            )
        if stage_dir.is_symlink() or not stage_dir.is_dir():
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Stage output directory is unsafe: {stage_dir.name}",
            )
    _t_health_start = _time.monotonic()
    contract: StageContract = CONTRACTS[stage]

    if contract.input_files:
        for input_file in contract.input_files:
            found = _read_prior_artifact(run_dir, input_file)
            if found is None:
                result = StageResult(
                    stage=stage,
                    status=StageStatus.FAILED,
                    artifacts=(),
                    error=f"Missing input: {input_file} (required by {stage.name})",
                    decision="retry",
                )
                _write_stage_meta(stage_dir, stage, run_id, result)
                return result

    bridge = config.openclaw_bridge
    if bridge.use_message and config.notifications.on_stage_start:
        adapters.message.notify(
            config.notifications.channel,
            f"stage-{int(stage):02d}-start",
            f"Starting {stage.name}",
        )
    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:running")

    # Stages 9-10 and 12-14 choose their canonical domain evaluator before any
    # model call. Fixed paths never need an LLM; legacy branches resolve this
    # proxy on their first chat.
    llm: LLMClient | _DeferredLLMClient | None
    if stage in {
        Stage.EXPERIMENT_DESIGN,
        Stage.CODE_GENERATION,
        Stage.EXPERIMENT_RUN,
        Stage.ITERATIVE_REFINE,
        Stage.RESULT_ANALYSIS,
    }:
        llm = _DeferredLLMClient(lambda: _create_configured_llm(config))
    else:
        llm = _create_configured_llm(config)

    try:
        _ = advance(stage, StageStatus.PENDING, TransitionEvent.START)
        executor = _STAGE_EXECUTORS[stage]
        prompts = PromptManager(
            config.prompts.custom_file or None,  # type: ignore[attr-defined]
            domain=_prompt_bank_domain_from_config(config),
            extra_prompts={
                stage_key: path_or_text
                for stage_key, path_or_text in getattr(config.prompts, "extra_prompts", ())  # type: ignore[attr-defined]
            } or None,
        )
        try:
            if stage9_namespace is not None:
                result = executor(
                    stage_dir,
                    run_dir,
                    config,
                    adapters,
                    llm=llm,
                    prompts=prompts,
                    namespace=stage9_namespace,
                    authority_selection=stage9_authority_selection,
                )
            else:
                result = executor(
                    stage_dir, run_dir, config, adapters, llm=llm, prompts=prompts
                )
        except TypeError as exc:
            if "unexpected keyword argument 'prompts'" not in str(exc):
                raise
            result = executor(stage_dir, run_dir, config, adapters, llm=llm)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stage %s failed", stage.name)
        result = StageResult(
            stage=stage,
            status=StageStatus.FAILED,
            artifacts=(),
            error=str(exc),
            decision="retry",
        )

    invalidate_domain_authority = False
    if result.status == StageStatus.DONE:
        output_files = _select_output_files(contract, config)
        try:
            domain_outputs = _domain_evaluator_output_contract(
                stage, run_dir, config
            )
        except Exception as exc:  # noqa: BLE001
            result = StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=result.artifacts,
                error=f"Canonical output contract replay failed: {exc}",
                decision="retry",
                evidence_refs=result.evidence_refs,
            )
            domain_outputs = None
            invalidate_domain_authority = stage in {
                Stage.CODE_GENERATION,
                Stage.EXPERIMENT_RUN,
                Stage.ITERATIVE_REFINE,
                Stage.RESULT_ANALYSIS,
            }
        if result.status == StageStatus.DONE and domain_outputs is not None:
            if result.artifacts != domain_outputs:
                result = StageResult(
                    stage=stage,
                    status=StageStatus.FAILED,
                    artifacts=result.artifacts,
                    error=(
                        "Canonical domain evaluator artifact contract mismatch: "
                        f"expected {domain_outputs!r}, got {result.artifacts!r}"
                    ),
                    decision="retry",
                    evidence_refs=result.evidence_refs,
                )
                invalidate_domain_authority = True
            else:
                output_files = domain_outputs
        if result.status != StageStatus.DONE:
            output_files = ()
        for output_file in output_files:
            if fixed_stage9_authority and stage9_namespace is not None:
                try:
                    if output_file.endswith("/"):
                        raise OSError(
                            "fixed Stage 9 output contract cannot contain directories"
                        )
                    if not stage9_namespace.read_bytes(output_file):
                        raise OSError("output is empty")
                except OSError:
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing or empty output: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    break
                continue
            if output_file.endswith("/"):
                path = stage_dir / output_file.rstrip("/")
                if not path.is_dir() or not any(path.iterdir()):
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing output directory: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    if domain_outputs is not None:
                        invalidate_domain_authority = True
                    break
            else:
                path = stage_dir / output_file
                if not path.exists() or path.stat().st_size == 0:
                    result = StageResult(
                        stage=stage,
                        status=StageStatus.FAILED,
                        artifacts=result.artifacts,
                        error=f"Missing or empty output: {output_file}",
                        decision="retry",
                        evidence_refs=result.evidence_refs,
                    )
                    if domain_outputs is not None:
                        invalidate_domain_authority = True
                    break
        if invalidate_domain_authority:
            try:
                if release_lock is None:
                    raise RuntimeError(
                        "canonical executor postcondition cleanup requires writer epoch"
                    )
                _invalidate_failed_domain_evaluator_authority(
                    stage, run_dir, release_lock
                )
            except Exception as cleanup_exc:  # noqa: BLE001
                result = _append_authority_cleanup_error(result, cleanup_exc)

    stage9_classification_failed = False
    if stage is Stage.EXPERIMENT_DESIGN and result.status is StageStatus.DONE:
        try:
            from researchclaw.experiment_runtime.contract import load_contract_bytes

            if stage9_namespace is None:
                raise RuntimeError("Stage 9 authority requires a held namespace")
            if stage9_authority_selection is None:
                raise RuntimeError("Stage 9 authority selection was not captured")
            published_contract = load_contract_bytes(
                stage9_namespace.read_bytes("experiment_contract.yaml"),
                authority_selection=stage9_authority_selection,
            )
            if (published_contract.schema_version == 3) != fixed_stage9_authority:
                raise RuntimeError("Stage 9 selector/contract schema mismatch")
            stage9_namespace.assert_canonical()
        except Exception as exc:  # noqa: BLE001
            stage9_classification_failed = True
            result = StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Stage 9 authority classification failed: {exc}",
                decision="abort",
                evidence_refs=(),
            )
    if stage9_classification_failed and stage9_namespace is not None:
        result = _finalize_fixed_stage9_authority(
            result, run_dir, config, release_lock, stage9_namespace
        )

    # --- MetaClaw PRM quality gate evaluation ---
    try:
        mc_bridge = getattr(config, "metaclaw_bridge", None)
        if (
            mc_bridge
            and getattr(mc_bridge, "enabled", False)
            and result.status == StageStatus.DONE
        ):
            mc_prm = getattr(mc_bridge, "prm", None)
            if mc_prm and getattr(mc_prm, "enabled", False):
                prm_stages = getattr(mc_prm, "gate_stages", (5, 9, 15, 20))
                if int(stage) in prm_stages:
                    from researchclaw.metaclaw_bridge.prm_gate import ResearchPRMGate

                    prm_gate = ResearchPRMGate.from_bridge_config(mc_prm)
                    if prm_gate is not None:
                        # Read stage output for PRM evaluation
                        output_text = ""
                        for art in result.artifacts:
                            if fixed_stage9_authority and stage9_namespace is not None:
                                try:
                                    output_text += stage9_namespace.read_bytes(
                                        art
                                    ).decode("utf-8")[:4000]
                                except (UnicodeDecodeError, OSError):
                                    pass
                            else:
                                art_path = stage_dir / art
                                if art_path.exists() and art_path.is_file():
                                    try:
                                        output_text += art_path.read_text(
                                            encoding="utf-8"
                                        )[:4000]
                                    except (UnicodeDecodeError, OSError):
                                        pass
                        if output_text:
                            prm_score = prm_gate.evaluate_stage(int(stage), output_text)
                            logger.info(
                                "MetaClaw PRM score for stage %d: %.1f",
                                int(stage),
                                prm_score,
                            )
                            # A completed PRM rejection is terminal evidence.
                            # Diagnostic publication below is best-effort and
                            # must never turn the rejected stage back to DONE.
                            if prm_score == -1.0:
                                logger.warning(
                                    "MetaClaw PRM rejected stage %d output",
                                    int(stage),
                                )
                                result = StageResult(
                                    stage=result.stage,
                                    status=StageStatus.FAILED,
                                    artifacts=result.artifacts,
                                    error="PRM quality gate: output below quality threshold",
                                    decision="retry",
                                    evidence_refs=result.evidence_refs,
                                )
                            # Write PRM score to stage health
                            import json as _prm_json

                            prm_report = {
                                "stage": int(stage),
                                "prm_score": prm_score,
                                "model": prm_gate.model,
                                "votes": prm_gate.votes,
                            }
                            prm_text = _prm_json.dumps(prm_report, indent=2)
                            if fixed_stage9_authority and stage9_namespace is not None:
                                stage9_namespace.write_text_atomic(
                                    "prm_score.json", prm_text
                                )
                            else:
                                (stage_dir / "prm_score.json").write_text(
                                    prm_text,
                                    encoding="utf-8",
                                )
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw PRM evaluation failed (non-blocking)")

    profile_name = (
        getattr(getattr(config, "project", None), "profile", None) or None
    )
    if gate_required(
        stage,
        config.security.hitl_required_stages,
        profile=profile_name,
    ):
        if auto_approve_gates:
            if bridge.use_memory:
                adapters.memory.append("gates", f"{run_id}:{int(stage)}:auto-approved")
        else:
            result = StageResult(
                stage=result.stage,
                status=StageStatus.BLOCKED_APPROVAL,
                artifacts=result.artifacts,
                error=result.error,
                decision="block",
                evidence_refs=result.evidence_refs,
            )
            if bridge.use_message and config.notifications.on_gate_required:
                adapters.message.notify(
                    config.notifications.channel,
                    f"gate-{int(stage):02d}",
                    f"Approval required for {stage.name}",
                )

    # A rejected fixed Stage 9 generation must not remain authoritative while
    # a later diagnostic/HITL path is waiting. The terminal guard repeats this
    # invalidation after HITL to reject any non-cooperative reintroduction.
    if (
        fixed_stage9_authority
        and result.status is not StageStatus.DONE
        and stage9_namespace is not None
    ):
        result = _finalize_fixed_stage9_authority(
            result, run_dir, config, release_lock, stage9_namespace
        )

    if bridge.use_memory:
        adapters.memory.append("stages", f"{run_id}:{int(stage)}:{result.status.value}")

    # Canonical postcondition failure has already withdrawn its authority under
    # the held writer epoch. Do not reopen the live stage path for diagnostics
    # or HITL; the outer epoch performs the final run-directory identity check.
    if invalidate_domain_authority:
        return result

    # These stages publish an exact, manifest-bound namespace. The v2 Stage 13
    # fixed evaluator is likewise manifest-only, while the legacy v1 branch
    # retains its historical diagnostics namespace.
    exact_authority_namespace = (
        stage in _EXACT_AUTHORITY_NAMESPACE_STAGES
        or (
            stage is Stage.EXPERIMENT_DESIGN
            and fixed_stage9_authority
        )
        or (
            stage is Stage.ITERATIVE_REFINE
            and result.status is StageStatus.DONE
            and result.artifacts == ("refinement_result_set.json",)
        )
        or (
            stage is Stage.RESULT_ANALYSIS
            and len(result.artifacts) == 1
            and result.artifacts[0].startswith("evidence_candidates/cand-")
            and result.artifacts[0].endswith("/experiment_evidence_candidate.json")
        )
    )
    if not exact_authority_namespace:
        _write_stage_meta(stage_dir, stage, run_id, result)

    _t_health_end = _time.monotonic()
    stage_health = {
        "stage_id": f"{int(stage):02d}-{stage.name.lower()}",
        "run_id": run_id,
        "duration_sec": round(_t_health_end - _t_health_start, 2),
        "status": result.status.value,
        "artifacts_count": len(result.artifacts),
        "error": result.error,
        "timestamp": _utcnow_iso(),
    }
    if not exact_authority_namespace:
        try:
            (stage_dir / "stage_health.json").write_text(
                json.dumps(stage_health, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # --- HITL post-stage hook ---
    result = _run_hitl_post_stage(
        stage,
        result,
        run_dir,
        adapters,
        config=config,
        fixed_stage9_authority=fixed_stage9_authority,
        release_lock=release_lock,
        stage9_namespace=stage9_namespace,
    )

    if fixed_stage9_authority:
        if stage9_namespace is None:
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                artifacts=(),
                error="Fixed Stage 9 terminal guard lost its held namespace",
                decision="abort",
            )
        result = _finalize_fixed_stage9_authority(
            result, run_dir, config, release_lock, stage9_namespace
        )

    return result
