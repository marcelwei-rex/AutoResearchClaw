from __future__ import annotations

import json
import importlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time as _time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.budget_ledger import BudgetUnverifiable, TransportBudgetLedger, transport_budgeted
from researchclaw.evolution import (
    EvolutionStore,
    extract_lessons,
)
from researchclaw.knowledge.base import write_stage_to_kb
from researchclaw.pipeline.contracts import CONTRACTS
from researchclaw.pipeline.executor import StageResult, execute_stage
from researchclaw.pipeline.stages import (
    DECISION_ROLLBACK,
    MAX_DECISION_PIVOTS,
    NONCRITICAL_STAGES,
    SKIP_FORBIDDEN_STAGES,
    STAGE_SEQUENCE,
    Stage,
    StageStatus,
)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _should_start(stage: Stage, from_stage: Stage, started: bool) -> bool:
    if started:
        return True
    return stage == from_stage


def _artifact_path(run_dir: Path, artifact_key: str) -> Path:
    normalized = artifact_key.replace("\\", "/").lstrip("/")
    if not normalized or ".." in normalized.split("/"):
        raise ValueError(f"Invalid injected artifact path: {artifact_key!r}")
    return run_dir / normalized


def _apply_injected_artifacts(run_dir: Path, config: RCConfig) -> list[str]:
    """Write configured partial-run artifacts before stage execution."""
    injected: list[str] = []
    for rel_path, source in config.runtime.inject_artifacts.items():
        dest = _artifact_path(run_dir, rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        source_is_path = "\n" not in source and "\r" not in source and len(source) < 240
        if source_is_path:
            source_path = Path(source).expanduser()
            try:
                if source_path.exists() and source_path.is_file():
                    shutil.copy2(source_path, dest)
                    injected.append(rel_path.replace("\\", "/"))
                    continue
            except OSError:
                pass
            if "/" in source or "\\" in source or source_path.suffix:
                logger.warning(
                    "Injected artifact source looks like a path but was not readable: %s",
                    source,
                )
        dest.write_text(source, encoding="utf-8")
        injected.append(rel_path.replace("\\", "/"))
    if injected:
        (run_dir / "injected_artifacts.json").write_text(
            json.dumps(
                {"artifacts": injected, "generated": _utcnow_iso()},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return injected


def _write_skipped_stage_outputs(
    run_dir: Path, stage: Stage, run_id: str
) -> tuple[str, ...]:
    """Create minimal contract outputs for a deliberately skipped stage."""
    if stage in SKIP_FORBIDDEN_STAGES:
        raise ValueError(
            f"cannot create skipped outputs for evidence-authority stage: {int(stage)}"
        )
    stage_dir = run_dir / f"stage-{int(stage):02d}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[str] = []
    contract = CONTRACTS[stage]
    for output in contract.output_files:
        target = stage_dir / output.rstrip("/")
        if output.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            marker = target / "SKIPPED.md"
            if not marker.exists():
                marker.write_text(
                    f"# Skipped Stage\n\n"
                    f"Stage {int(stage)} {stage.name} was skipped by "
                    "runtime.skip_stages.\n",
                    encoding="utf-8",
                )
            artifacts.append(output)
            continue
        if target.exists() and target.stat().st_size > 0:
            artifacts.append(output)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if output.endswith(".json"):
            target.write_text(
                json.dumps(
                    {
                        "skipped": True,
                        "stage": int(stage),
                        "stage_name": stage.name,
                        "run_id": run_id,
                        "generated": _utcnow_iso(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        elif output.endswith((".yaml", ".yml")):
            target.write_text(
                "skipped: true\n"
                f"stage: {int(stage)}\n"
                f"stage_name: {stage.name}\n"
                f"run_id: {run_id}\n",
                encoding="utf-8",
            )
        else:
            target.write_text(
                f"# Skipped Stage\n\n"
                f"Stage {int(stage)} {stage.name} was skipped by "
                "runtime.skip_stages.\n",
                encoding="utf-8",
            )
        artifacts.append(output)
    return tuple(artifacts)


def _write_skipped_stage_outputs_bound(
    run_dir: Path,
    stage: Stage,
    run_id: str,
    release_lock: object,
) -> tuple[str, ...]:
    """Publish generic skip contracts through the active writer's held run fd."""

    if stage in SKIP_FORBIDDEN_STAGES:
        raise ValueError(
            f"cannot create skipped outputs for evidence-authority stage: {int(stage)}"
        )
    from researchclaw.pipeline.release_graph_lock import require_active_writer_epoch

    writer = require_active_writer_epoch(run_dir, release_lock)
    artifacts: list[str] = []
    with writer.open_stage_namespace(
        f"stage-{int(stage):02d}", create_stage=True
    ) as namespace:
        for output in CONTRACTS[stage].output_files:
            if output.endswith("/"):
                namespace.publish_flat_directory(
                    output[:-1],
                    {
                        "SKIPPED.md": (
                            f"# Skipped Stage\n\nStage {int(stage)} {stage.name} "
                            "was skipped by runtime.skip_stages.\n"
                        ).encode("utf-8")
                    },
                )
            elif output.endswith(".json"):
                namespace.write_text_atomic(
                    output,
                    json.dumps(
                        {
                            "skipped": True,
                            "stage": int(stage),
                            "stage_name": stage.name,
                            "run_id": run_id,
                            "generated": _utcnow_iso(),
                        },
                        indent=2,
                    ),
                )
            elif output.endswith((".yaml", ".yml")):
                namespace.write_text_atomic(
                    output,
                    "skipped: true\n"
                    f"stage: {int(stage)}\n"
                    f"stage_name: {stage.name}\n"
                    f"run_id: {run_id}\n",
                )
            else:
                namespace.write_text_atomic(
                    output,
                    f"# Skipped Stage\n\nStage {int(stage)} {stage.name} "
                    "was skipped by runtime.skip_stages.\n",
                )
            artifacts.append(output)
        namespace.assert_canonical()
    writer.assert_canonical()
    return tuple(artifacts)


def _build_pipeline_summary(
    *,
    run_id: str,
    results: list[StageResult],
    from_stage: Stage,
    run_dir: Path | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "run_id": run_id,
        "stages_executed": len(results),
        "stages_done": sum(1 for item in results if item.status == StageStatus.DONE),
        "stages_paused": sum(
            1 for item in results if item.status == StageStatus.PAUSED
        ),
        "stages_blocked": sum(
            1 for item in results if item.status == StageStatus.BLOCKED_APPROVAL
        ),
        "stages_failed": sum(
            1 for item in results if item.status == StageStatus.FAILED
        ),
        "degraded": any(
            r.status is StageStatus.DONE and r.decision == "degraded" for r in results
        ),
        "from_stage": int(from_stage),
        "final_stage": int(results[-1].stage) if results else int(from_stage),
        "final_status": results[-1].status.value if results else "no_stages",
        "final_terminal_action": results[-1].terminal_action if results else "stop",
        "generated": _utcnow_iso(),
        "content_metrics": _collect_content_metrics(run_dir),
    }
    if results:
        if results[-1].error:
            summary["final_error"] = results[-1].error
        if results[-1].persisted_decision:
            summary["final_decision"] = results[-1].persisted_decision
    return summary


def _write_pipeline_summary(run_dir: Path, summary: dict[str, object]) -> None:
    (run_dir / "pipeline_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )


def _write_checkpoint(
    run_dir: Path, stage: Stage, run_id: str,
    adapters: "AdapterBundle | None" = None,
) -> None:
    """Write checkpoint atomically via temp file + rename to prevent corruption."""
    checkpoint: dict[str, object] = {
        "last_completed_stage": int(stage),
        "last_completed_name": stage.name,
        "run_id": run_id,
        "timestamp": _utcnow_iso(),
    }
    from researchclaw.literature.citation_policy import (
        active_config_checkpoint_fields,
    )

    checkpoint.update(active_config_checkpoint_fields(run_dir))

    # Embed HITL session data if available
    if adapters is not None:
        hitl_session = getattr(adapters, "hitl", None)
        if hitl_session is not None:
            try:
                checkpoint["hitl"] = hitl_session.hitl_checkpoint_data()
            except Exception:
                pass
    target = run_dir / "checkpoint.json"
    fd, tmp_path = tempfile.mkstemp(dir=run_dir, suffix=".tmp", prefix="checkpoint_")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(checkpoint, indent=2))
        Path(tmp_path).replace(target)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _write_heartbeat(run_dir: Path, stage: Stage, run_id: str) -> None:
    """Write heartbeat file for sentinel watchdog monitoring."""
    import os

    heartbeat = {
        "pid": os.getpid(),
        "last_stage": int(stage),
        "last_stage_name": stage.name,
        "run_id": run_id,
        "timestamp": _utcnow_iso(),
    }
    (run_dir / "heartbeat.json").write_text(
        json.dumps(heartbeat, indent=2), encoding="utf-8"
    )


def read_checkpoint(run_dir: Path) -> Stage | None:
    """Read checkpoint and return the NEXT stage to execute, or None if no checkpoint."""
    cp_path = run_dir / "checkpoint.json"
    if not cp_path.exists():
        return None
    try:
        data = json.loads(cp_path.read_text(encoding="utf-8"))
        last_num = data.get("last_completed_stage")
        if last_num is None:
            return None
        for i, stage in enumerate(STAGE_SEQUENCE):
            if int(stage) == last_num:
                if i + 1 < len(STAGE_SEQUENCE):
                    return STAGE_SEQUENCE[i + 1]
                return None
        return None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def resume_from_checkpoint(
    run_dir: Path, default_stage: Stage = Stage.TOPIC_INIT
) -> Stage:
    """Resolve the stage to resume from using checkpoint metadata."""
    next_stage = read_checkpoint(run_dir)
    return next_stage if next_stage is not None else default_stage


def _collect_content_metrics(run_dir: Path | None) -> dict[str, object]:
    """Collect content authenticity metrics from stage outputs."""
    metrics: dict[str, object] = {
        "template_ratio": None,
        "citation_verify_score": None,
        "total_citations": None,
        "verified_citations": None,
        "degraded_sources": [],
    }
    if run_dir is None:
        return metrics

    draft_path = run_dir / "stage-17" / "paper_draft.md"
    if draft_path.exists():
        try:
            quality_module = importlib.import_module("researchclaw.quality")
            compute_template_ratio = quality_module.compute_template_ratio
            text = draft_path.read_text(encoding="utf-8")
            metrics["template_ratio"] = round(compute_template_ratio(text), 4)
        except (
            AttributeError,
            ModuleNotFoundError,
            UnicodeDecodeError,
            OSError,
            ValueError,
            TypeError,
        ):
            pass

    verify_path = run_dir / "stage-23" / "verification_report.json"
    if verify_path.exists():
        try:
            vdata = json.loads(verify_path.read_text(encoding="utf-8"))
            if isinstance(vdata, dict):
                summary = vdata.get("summary", vdata)
                total = summary.get("total", 0) if isinstance(summary, dict) else None
                verified = summary.get("verified", 0) if isinstance(summary, dict) else None
                if isinstance(total, int | float) and isinstance(verified, int | float):
                    total_num = int(total)
                    verified_num = int(verified)
                    metrics["total_citations"] = total_num
                    metrics["verified_citations"] = verified_num
                    if total_num > 0:
                        metrics["citation_verify_score"] = round(
                            verified_num / total_num, 4
                        )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    return metrics


logger = logging.getLogger(__name__)


def _run_experiment_diagnosis(run_dir: Path, config: RCConfig, run_id: str) -> None:
    """Reject the legacy shadow-scanning diagnosis path."""
    del run_dir, config, run_id
    raise PermissionError("legacy experiment diagnosis is disabled by canonical policy")


def _run_experiment_repair(run_dir: Path, config: RCConfig, run_id: str) -> None:
    """Reject the legacy shadow-scanning repair path."""
    del run_dir, config, run_id
    raise PermissionError("legacy experiment repair is disabled by canonical policy")


@transport_budgeted
def execute_pipeline(
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    from_stage: Stage = Stage.TOPIC_INIT,
    to_stage: Stage | None = None,
    auto_approve_gates: bool = False,
    stop_on_gate: bool = False,
    skip_noncritical: bool = False,
    kb_root: Path | None = None,
    cancel_event: "threading.Event | None" = None,
    _release_lock: object | None = None,
    _internal_rollback: bool = False,
) -> list[StageResult]:
    """Execute pipeline stages sequentially from *from_stage* to *to_stage* (inclusive)."""

    from researchclaw.pipeline.canonical_evidence_capabilities import (
        requested_range_requires_canonical_evidence,
        require_canonical_evidence_capabilities,
    )

    if requested_range_requires_canonical_evidence(from_stage, to_stage):
        require_canonical_evidence_capabilities("execute_pipeline")
    if _internal_rollback and _release_lock is None:
        raise RuntimeError("internal rollback requires an active writer epoch")
    if _release_lock is not None:
        from researchclaw.pipeline.release_graph_lock import (
            require_active_writer_epoch,
        )

        require_active_writer_epoch(run_dir, _release_lock)

    results: list[StageResult] = []
    started = False
    total_stages = len(STAGE_SEQUENCE)
    skip_stage_nums = frozenset(config.runtime.skip_stages)
    forbidden_skips = sorted(
        skip_stage_nums & {int(stage) for stage in SKIP_FORBIDDEN_STAGES}
    )
    if forbidden_skips:
        raise ValueError(
            "runtime.skip_stages cannot skip evidence-authority stages: "
            + ", ".join(str(stage) for stage in forbidden_skips)
        )

    if not _internal_rollback and not (run_dir / "injected_artifacts.json").exists():
        injected = _apply_injected_artifacts(run_dir, config)
        if injected:
            print(f"[{run_id}] Injected artifacts: {', '.join(injected)}")

    # Force the domain detector to honor a deployed profile (if any) so
    # every stage picks the same adapter.  Safe no-op when empty.
    try:
        from researchclaw.domains.detector import set_forced_profile
        forced = getattr(config.project, "profile", "") or ""
        set_forced_profile(forced)
    except Exception:  # noqa: BLE001
        pass

    # ── Integration hooks: EventLog, CostTracker ──
    event_log = None
    if not _internal_rollback:
        try:
            from researchclaw.pipeline.event_log import EventLog, EventType, create_event
            event_log = EventLog(log_dir=run_dir)
            event_log.append(create_event(
                EventType.PIPELINE_START, run_id=run_id,
                stages=total_stages, from_stage=int(from_stage),
            ))
        except Exception:
            logger.debug("Event log initialisation skipped")

    for stage in STAGE_SEQUENCE:
        started = _should_start(stage, from_stage, started)
        if not started:
            continue

        # ── Check for cancellation before each stage ──
        if cancel_event is not None and cancel_event.is_set():
            logger.info("[%s] Pipeline cancelled before stage %s", run_id, stage.name)
            print(f"[{run_id}] Pipeline cancelled by user.")
            break

        stage_num = int(stage)
        prefix = f"[{run_id}] Stage {stage_num:02d}/{total_stages}"

        if stage_num in skip_stage_nums:
            if event_log:
                try:
                    event_log.append(
                        create_event(
                            EventType.STAGE_START,
                            run_id=run_id,
                            stage=stage.name,
                            skipped_by_config=True,
                        )
                    )
                except Exception:
                    pass
            if _internal_rollback:
                artifacts = _write_skipped_stage_outputs_bound(
                    run_dir, stage, run_id, _release_lock
                )
            else:
                artifacts = _write_skipped_stage_outputs(run_dir, stage, run_id)
            result = StageResult(
                stage=stage,
                status=StageStatus.DONE,
                artifacts=artifacts,
                decision="skipped",
                evidence_refs=tuple(f"stage-{stage_num:02d}/{a}" for a in artifacts),
            )
            results.append(result)
            if event_log:
                try:
                    event_log.append(
                        create_event(
                            EventType.STAGE_END,
                            run_id=run_id,
                            stage=stage.name,
                            status=result.status.value,
                            elapsed_sec=0.0,
                            decision=result.decision,
                            skipped_by_config=True,
                        )
                    )
                except Exception:
                    pass
            arts = ", ".join(artifacts) if artifacts else "none"
            print(f"{prefix} {stage.name} — skipped by config → {arts}")
            if not _internal_rollback:
                _write_checkpoint(run_dir, stage, run_id, adapters=adapters)
                _write_heartbeat(run_dir, stage, run_id)
            if to_stage is not None and stage == to_stage:
                logger.info("[%s] Reached --to-stage %s, stopping.", run_id, stage.name)
                print(f"[{run_id}] Reached --to-stage {stage.name}, stopping pipeline.")
                break
            continue

        print(f"{prefix} {stage.name} — running...")

        # ── Event log: stage start ──
        if event_log:
            try:
                event_log.append(create_event(
                    EventType.STAGE_START, run_id=run_id, stage=stage.name,
                ))
            except Exception:
                pass

        # BUG-218: Ensure the best stage-14 experiment data is promoted
        # BEFORE paper writing begins.  Without this, the recursive REFINE
        # path writes the paper using the latest (potentially worse)
        # iteration's data, because the post-recursion promotion at line
        # ~547 runs only after the recursive call—i.e. after the paper
        # has already been written.
        if stage == Stage.PAPER_OUTLINE:
            _promote_best_stage14(run_dir, config)

        t0 = _time.monotonic()

        stage15_pivot_count: int | None = None
        stage15_pivot_results: list[StageResult] | None = None
        if stage is Stage.RESEARCH_DECISION:
            from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock

            with ReleaseGraphLock.acquire(
                run_dir, "runner.research_decision_terminalization", mode="write"
            ) as decision_epoch:
                with ExitStack() as namespace_stack:
                    exhaustion_namespaces = (
                        _capture_refinement_exhaustion_namespaces(
                            run_dir, decision_epoch, namespace_stack
                        )
                    )
                    result = execute_stage(
                        stage,
                        run_dir=run_dir,
                        run_id=run_id,
                        config=config,
                        adapters=adapters,
                        auto_approve_gates=auto_approve_gates,
                    )
                    if (
                        result.status is StageStatus.DONE
                        and result.decision in DECISION_ROLLBACK
                    ):
                        _invalidate_decision_resume_pointers(
                            run_dir, decision_epoch
                        )
                        stage15_pivot_count = _read_pivot_count_bound(
                            run_dir, decision_epoch
                        )
                        exhaustion_reason: str | None = None
                        if stage15_pivot_count > 0 and _consecutive_empty_metrics_bound(
                            run_dir, stage15_pivot_count, decision_epoch
                        ):
                            exhaustion_reason = "consecutive_empty_metrics"
                            logger.warning(
                                "Consecutive REFINE cycles produced empty metrics — "
                                "failing closed"
                            )
                            print(
                                f"[{run_id}] Consecutive empty metrics across REFINE "
                                "cycles — refinement exhausted"
                            )
                        elif stage15_pivot_count >= MAX_DECISION_PIVOTS:
                            exhaustion_reason = "max_refinement_attempts"
                            logger.warning(
                                "Max pivot attempts (%d) reached — refinement exhausted",
                                MAX_DECISION_PIVOTS,
                            )
                            print(
                                f"[{run_id}] Max pivot attempts reached — "
                                "refinement exhausted"
                            )
                        if exhaustion_reason is not None:
                            result = _finalize_refinement_exhausted(
                                run_dir,
                                result,
                                reason=exhaustion_reason,
                                release_lock=decision_epoch,
                                namespaces=exhaustion_namespaces,
                            )
                        elif to_stage is not Stage.RESEARCH_DECISION:
                            rollback_target = DECISION_ROLLBACK[result.decision]
                            if (
                                config.experiment.mode
                                in ("collider_agent", "biology_agent", "stat_agent")
                                and result.decision == "refine"
                            ):
                                rollback_target = Stage.EXPERIMENT_RUN
                            _record_decision_history_bound(
                                run_dir,
                                result.decision,
                                rollback_target,
                                stage15_pivot_count + 1,
                                decision_epoch,
                            )
                            decision_epoch.assert_canonical()
                            _append_rollback_attempt_bound(
                                run_dir,
                                run_id,
                                rollback_target,
                                result.decision,
                                decision_epoch,
                            )
                            decision_epoch.assert_canonical()
                            logger.info(
                                "Decision %s: rolling back to %s (attempt %d/%d)",
                                result.decision.upper(),
                                rollback_target.name,
                                stage15_pivot_count + 1,
                                MAX_DECISION_PIVOTS,
                            )
                            print(
                                f"[{run_id}] Decision: {result.decision.upper()} → "
                                f"rollback to {rollback_target.name} "
                                f"(attempt {stage15_pivot_count + 1}/"
                                f"{MAX_DECISION_PIVOTS})"
                            )
                            agent_refine = (
                                config.experiment.mode
                                in ("collider_agent", "biology_agent", "stat_agent")
                                and result.decision == "refine"
                            )
                            _version_rollback_stages_bound(
                                run_dir,
                                rollback_target,
                                stage15_pivot_count + 1,
                                decision_epoch,
                                incremental=agent_refine,
                            )
                            decision_epoch.assert_canonical()
                            stage15_pivot_results = execute_pipeline(
                                run_dir=run_dir,
                                run_id=run_id,
                                config=config,
                                adapters=adapters,
                                from_stage=rollback_target,
                                auto_approve_gates=auto_approve_gates,
                                stop_on_gate=stop_on_gate,
                                skip_noncritical=skip_noncritical,
                                kb_root=kb_root,
                                cancel_event=cancel_event,
                                _release_lock=decision_epoch,
                                _internal_rollback=True,
                            )
                            decision_epoch.assert_canonical()
                            refinement_exhausted = bool(
                                stage15_pivot_results
                                and stage15_pivot_results[-1].stage
                                is Stage.RESEARCH_DECISION
                                and stage15_pivot_results[-1].status
                                is StageStatus.FAILED
                                and stage15_pivot_results[-1].decision
                                == "refinement_exhausted"
                            )
                            if not refinement_exhausted:
                                _promote_best_stage14(run_dir, config)
                            decision_epoch.assert_canonical()
                    # No path-based diagnostics may run after a parent replacement.
                    decision_epoch.assert_canonical()
        else:
            result = execute_stage(
                stage,
                run_dir=run_dir,
                run_id=run_id,
                config=config,
                adapters=adapters,
                auto_approve_gates=auto_approve_gates,
            )
        elapsed = _time.monotonic() - t0

        # ── v2: append-only attempt log + per-stage cost entry ──
        # Failed/degraded attempts are first-class records; never deleted.
        if not _internal_rollback:
            try:
                from researchclaw.pipeline import release_artifacts as _ra

                _attempt = _ra.append_attempt(
                    run_dir,
                    run_id=run_id,
                    stage=stage_num,
                    stage_name=stage.name,
                    status=result.status.value,
                    terminal_action=result.terminal_action,
                    decision=result.persisted_decision or "",
                    error=result.error,
                    elapsed_sec=elapsed,
                    artifacts=result.artifacts,
                )
                _cumulative = TransportBudgetLedger(run_dir, config.llm).cumulative_cost()
                _prev = _ra.last_cumulative_cost(run_dir)
                _delta = max(0.0, round(_cumulative - _prev, 6))
                _ra.append_cost_entry(
                    run_dir,
                    stage=stage_num,
                    stage_name=stage.name,
                    model=config.llm.primary_model or "",
                    attempt_id=_attempt["attempt_id"],
                    cost_usd=_delta,
                    cumulative_usd=_cumulative,
                    elapsed_sec=elapsed,
                )
            except BudgetUnverifiable:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("Attempt/cost log append failed") from exc

        # ── Event log: stage end ──
        if event_log:
            try:
                etype = EventType.STAGE_END if result.status == StageStatus.DONE else EventType.STAGE_FAIL
                event_log.append(create_event(
                    etype, run_id=run_id, stage=stage.name,
                    status=result.status.value, elapsed_sec=round(elapsed, 1),
                    error=result.error,
                ))
            except Exception:
                pass

        # ── ExperimentSpec: generate after design, validate after analysis ──
        if (
            not _internal_rollback
            and stage == Stage.EXPERIMENT_DESIGN
            and result.status == StageStatus.DONE
        ):
            try:
                from researchclaw.pipeline.experiment_spec import ExperimentSpec, MetricDef, generate_spec
                spec_text = generate_spec(config.research.topic, "")
                spec_path = run_dir / f"stage-{int(stage):02d}" / "experiment_spec.md"
                spec_path.write_text(spec_text, encoding="utf-8")
                logger.info("Experiment spec generated: %s", spec_path)
            except Exception:
                logger.debug("Experiment spec generation skipped")

        # ── Pitfall detection after code generation / experiment run ──
        if (
            not _internal_rollback
            and stage in (Stage.CODE_GENERATION, Stage.EXPERIMENT_RUN)
            and result.status == StageStatus.DONE
        ):
            try:
                from researchclaw.pipeline.pitfall_detector import PitfallDetector
                detector = PitfallDetector()
                code_path = run_dir / f"stage-{int(stage):02d}"
                code_files = list(code_path.rglob("*.py"))
                code_text = "\n".join(f.read_text(errors="ignore") for f in code_files[:5])
                pitfalls = detector.detect_all(code=code_text, results={}, experiment_config={})
                if pitfalls:
                    critical = [p for p in pitfalls if p.severity == "critical"]
                    if critical:
                        logger.warning("CRITICAL pitfalls detected: %s", [p.description for p in critical])
                    pitfall_report = [{"type": p.type.value, "severity": p.severity, "description": p.description} for p in pitfalls]
                    (run_dir / f"stage-{int(stage):02d}" / "pitfall_report.json").write_text(
                        json.dumps(pitfall_report, indent=2), encoding="utf-8"
                    )
            except Exception:
                logger.debug("Pitfall detection skipped")

        if result.status == StageStatus.DONE:
            arts = ", ".join(result.artifacts) if result.artifacts else "none"
            if result.decision == "degraded":
                print(
                    f"{prefix} {stage.name} — DEGRADED ({elapsed:.1f}s) "
                    f"— continuing with sanitization → {arts}"
                )
            else:
                print(f"{prefix} {stage.name} — done ({elapsed:.1f}s) → {arts}")
        elif result.status == StageStatus.FAILED:
            err = result.error or "unknown error"
            print(f"{prefix} {stage.name} — FAILED ({elapsed:.1f}s) — {err}")
        elif result.status == StageStatus.BLOCKED_APPROVAL:
            print(f"{prefix} {stage.name} — blocked (awaiting approval)")
        elif result.status == StageStatus.PAUSED:
            err = result.error or "paused"
            print(f"{prefix} {stage.name} -- PAUSED ({elapsed:.1f}s) -- {err}")
        results.append(result)

        if (
            not _internal_rollback
            and kb_root is not None
            and result.status == StageStatus.DONE
        ):
            try:
                stage_dir = run_dir / f"stage-{int(stage):02d}"
                write_stage_to_kb(
                    kb_root,
                    stage_id=int(stage),
                    stage_name=stage.name.lower(),
                    run_id=run_id,
                    artifacts=list(result.artifacts),
                    stage_dir=stage_dir,
                    backend=config.knowledge_base.backend,
                    topic=config.research.topic,
                )
            except Exception:  # noqa: BLE001
                pass

        decision_rolls_back = (
            stage is Stage.RESEARCH_DECISION
            and result.status is StageStatus.DONE
            and result.decision in DECISION_ROLLBACK
        )
        if (
            not _internal_rollback
            and result.status == StageStatus.DONE
            and not decision_rolls_back
        ):
            _write_checkpoint(run_dir, stage, run_id, adapters=adapters)

        # ── Stop after to_stage if specified ──
        if to_stage is not None and stage == to_stage:
            logger.info("[%s] Reached --to-stage %s, stopping.", run_id, stage.name)
            print(f"[{run_id}] Reached --to-stage {stage.name}, stopping pipeline.")
            break

        # --- Heartbeat for sentinel watchdog ---
        if (
            not _internal_rollback
            and result.status == StageStatus.DONE
            and not decision_rolls_back
        ):
            _write_heartbeat(run_dir, stage, run_id)

        # --- PIVOT/REFINE decision handling ---
        if (
            stage == Stage.RESEARCH_DECISION
            and result.status == StageStatus.DONE
            and result.decision in DECISION_ROLLBACK
        ):
            if stage15_pivot_count is None:
                raise RuntimeError("Stage 15 decision classification was not captured")
            if stage15_pivot_count < MAX_DECISION_PIVOTS:
                if to_stage is not Stage.RESEARCH_DECISION:
                    if stage15_pivot_results is None:
                        raise RuntimeError("Stage 15 rollback was not terminalized")
                    results.extend(stage15_pivot_results)
                break  # Exit current loop; recursive call handles the rest
            else:  # pragma: no cover - terminalized under the held writer epoch
                raise RuntimeError("Stage 15 exhaustion escaped terminalization")

        # --- HITL: Handle abort decision ---
        if result.status is StageStatus.DONE and result.decision == "abort":
            logger.info("[%s] Pipeline aborted by user at stage %s", run_id, stage.name)
            print(f"[{run_id}] Pipeline aborted by user at {stage.name}")
            break

        if result.status == StageStatus.FAILED and skip_noncritical and stage in NONCRITICAL_STAGES:
            logger.warning("Noncritical stage %s failed; skip requested but terminal failure stops", stage.name)
        if result.status == StageStatus.PAUSED:
            logger.warning(
                "[%s] Pipeline paused at %s: %s",
                run_id,
                stage.name,
                result.error or result.decision,
            )

        # --- HITL: Handle rejected stage (from HITL review) ---
        if result.status == StageStatus.REJECTED:
            logger.info(
                "[%s] Stage %s rejected by reviewer — pipeline stopped",
                run_id, stage.name,
            )
            print(f"[{run_id}] Stage {stage.name} rejected — pipeline stopped")

        if result.terminal_action != "advance":
            break

    if _internal_rollback:
        from researchclaw.pipeline.release_graph_lock import require_active_writer_epoch

        require_active_writer_epoch(run_dir, _release_lock).assert_canonical()
        return results

    summary = _build_pipeline_summary(
        run_id=run_id,
        results=results,
        from_stage=from_stage,
        run_dir=run_dir,
    )
    _write_pipeline_summary(run_dir, summary)

    # ── v2: run manifest (reviewer isolation + manifest-driven final stage) ──
    try:
        from researchclaw import __version__ as _rc_version
        from researchclaw.pipeline import release_artifacts as _ra
        from researchclaw.pipeline.stages import FINAL_STAGE as _FINAL

        _sandbox_meta = (
            _ra.read_json(run_dir / "stage-12" / "sandbox_metadata.json")
            or _ra.read_json(run_dir / "sandbox_metadata.json")
            or {}
        )
        _env_meta = (
            _ra.read_json(run_dir / "stage-12" / "environment_policy.json")
            or _ra.read_json(run_dir / "environment_policy.json")
            or {}
        )
        _writer_model = config.llm.primary_model or ""
        _critic_model = getattr(config.llm, "critic_model", "") or ""
        _ext_path = getattr(config.llm, "external_review_path", "") or ""
        # Precedence: explicit config > what stage 15 actually recorded > derived.
        _critic_source = (getattr(config.llm, "critic_source", "") or "").strip()
        if not _critic_source:
            _critique = _ra.read_json(run_dir / "stage-15" / "critique.json") or {}
            _critic_source = str(_critique.get("critic_source", "")) or (
                "model" if _critic_model and _critic_model != _writer_model else "none"
            )
        _ra.write_run_manifest(
            run_dir,
            run_id=run_id,
            pipeline_version=_rc_version,
            expected_final_stage=int(_FINAL),
            writer_model=_writer_model,
            critic_model=_critic_model,
            critic_source=_critic_source,
            sectional_writer_model=(
                _writer_model if config.paper_revision.sectional_enabled else ""
            ),
            sectional_critic_model=(
                config.paper_revision.critic_model
                if config.paper_revision.sectional_enabled
                else ""
            ),
            external_review_path=_ext_path,
            sandbox=_sandbox_meta,
            environment_policy=_env_meta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_manifest.json write failed", exc_info=True)
        if config.paper_revision.sectional_enabled:
            raise RuntimeError(
                "sectional run requires a complete run_manifest.json"
            ) from exc

    # ── Event log: pipeline end ──
    if event_log:
        try:
            done_count = sum(1 for r in results if r.status == StageStatus.DONE)
            failed_count = sum(1 for r in results if r.status == StageStatus.FAILED)
            event_log.append(create_event(
                EventType.PIPELINE_END, run_id=run_id,
                stages_done=done_count, stages_failed=failed_count,
            ))
        except Exception:
            pass

    # --- Evolution: extract and store lessons ---
    lessons: list[object] = []
    try:
        lessons = extract_lessons(results, run_id=run_id, run_dir=run_dir)
        store = EvolutionStore(run_dir / "evolution")
        store.append_many(lessons)
        if lessons:
            logger.info("Extracted %d lessons from pipeline run", len(lessons))
    except Exception:  # noqa: BLE001
        logger.warning("Evolution lesson extraction failed (non-blocking)")

    # Experiment memory is published only after complete release reconstruction.
    try:
        from researchclaw.memory.experiment_memory import ExperimentMemory

        ExperimentMemory().record_release(
            run_dir,
            task_type=config.research.topic,
            run_id=run_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Canonical experiment memory recording failed (non-blocking)")

    # --- MetaClaw bridge: convert high-severity lessons to skills ---
    try:
        _metaclaw_post_pipeline(config, results, lessons, run_id, run_dir)
    except Exception:  # noqa: BLE001
        logger.warning("MetaClaw post-pipeline hook failed (non-blocking)")

    # --- Package deliverables into a single folder ---
    try:
        deliverables_dir = _package_deliverables(run_dir, run_id, config)
        if deliverables_dir is not None:
            print(f"[{run_id}] Deliverables packaged → {deliverables_dir}")
    except Exception:  # noqa: BLE001
        logger.warning("Deliverables packaging failed (non-blocking)")

    # --- HITL: Finalize session state ---
    try:
        hitl_session = getattr(adapters, "hitl", None)
        if hitl_session is not None:
            has_abort = any(
                r.status is StageStatus.DONE and r.decision == "abort" for r in results
            )
            has_failure = any(
                r.status == StageStatus.FAILED for r in results
            )
            if has_abort:
                hitl_session.abort()
            elif has_failure:
                hitl_session.abort()
            else:
                hitl_session.complete()
    except Exception:  # noqa: BLE001
        logger.debug("HITL session finalization failed (non-blocking)")

    return results


def _package_deliverables(
    run_dir: Path,
    run_id: str,
    config: RCConfig,
) -> Path | None:
    """Collect all final user-facing deliverables into a single ``deliverables/`` folder.

    Returns the deliverables directory path, or None if nothing was packaged.

    Packaged artifacts (best-available version selected automatically):
    - paper_final.md          — Final paper (Markdown)
    - paper.tex               — Conference-ready LaTeX
    - references.bib          — BibTeX bibliography
    - code/                   — Experiment code package
    - verification_report.json — Citation verification report (if available)
    """
    dest = run_dir / "deliverables"
    dest.mkdir(parents=True, exist_ok=True)

    packaged: list[str] = []

    # --- 0. Resolve effective conference template ---
    # Mirrors the stage-22 domain-aware override: when the topic belongs to a
    # non-ML domain (hep_ph, etc.) and the user has left the default
    # neurips_2025, swap in the domain's preferred physics template so the
    # bundled .sty, regenerated .tex, and manifest are all consistent.
    effective_conf = config.export.target_conference
    try:
        from researchclaw.domains.detector import detect_domain as _dd_detect
        from researchclaw.domains.prompt_adapter import get_adapter as _dd_adapter

        _dd_dom = _dd_detect(topic=config.research.topic)
        _dd_blocks = _dd_adapter(_dd_dom).get_export_publish_blocks(
            {"topic": config.research.topic}
        )
        _pref_tpl = (_dd_blocks.preferred_template or "").strip()
        if _pref_tpl and effective_conf == "neurips_2025":
            effective_conf = _pref_tpl
            logger.info(
                "Deliverables: domain=%s — overriding target_conference "
                "'neurips_2025' → '%s'.",
                getattr(_dd_dom, "domain_id", "?"),
                effective_conf,
            )
    except Exception:  # noqa: BLE001
        logger.debug("Deliverables: domain-aware template override skipped")

    # --- 1. Final paper (Markdown) ---
    # Prefer verified version (stage 23) over base version (stage 22)
    paper_md = None
    for candidate in [
        run_dir / "stage-23" / "paper_final_verified.md",
        run_dir / "stage-22" / "paper_final.md",
    ]:
        if candidate.exists() and candidate.stat().st_size > 0:
            paper_md = candidate
            break
    if paper_md is not None:
        shutil.copy2(paper_md, dest / "paper_final.md")
        packaged.append("paper_final.md")

    # --- 2. LaTeX paper ---
    # BUG-183: Stage 22's paper.tex has been sanitized (fabricated numbers
    # replaced with ---).  Regenerating from Markdown would undo this because
    # the Markdown was never sanitized.  Prefer Stage-22 paper.tex when a
    # sanitization report exists.  Only regenerate from verified Markdown if
    # no sanitization was performed (i.e., the run was clean).
    tex_regenerated = False
    _sanitization_report = run_dir / "stage-22" / "sanitization_report.json"
    _was_sanitized = _sanitization_report.exists()
    verified_md = run_dir / "stage-23" / "paper_final_verified.md"
    if (
        not _was_sanitized
        and paper_md is not None
        and paper_md == verified_md
        and verified_md.exists()
        and verified_md.stat().st_size > 0
    ):
        try:
            from researchclaw.templates import get_template, markdown_to_latex
            from researchclaw.pipeline.executor import _extract_paper_title

            tpl = get_template(effective_conf)
            v_text = verified_md.read_text(encoding="utf-8")
            tex_content = markdown_to_latex(
                v_text,
                tpl,
                title=_extract_paper_title(v_text),
                authors=config.export.authors,
                bib_file=config.export.bib_file,
            )
            # IMP-17: Quality check — ensure regenerated LaTeX has
            # proper structure (abstract, multiple sections)
            _has_abstract = (
                "\\begin{abstract}" in tex_content
                and tex_content.split("\\begin{abstract}")[1]
                .split("\\end{abstract}")[0]
                .strip()
            )
            _section_count = tex_content.count("\\section{")
            if _has_abstract and _section_count >= 3:
                (dest / "paper.tex").write_text(tex_content, encoding="utf-8")
                packaged.append("paper.tex")
                tex_regenerated = True
                logger.info(
                    "Deliverables: regenerated paper.tex from verified markdown"
                )
            else:
                logger.warning(
                    "Regenerated paper.tex has poor structure "
                    "(abstract=%s, sections=%d) — using Stage 22 version",
                    bool(_has_abstract),
                    _section_count,
                )
        except Exception:  # noqa: BLE001
            logger.debug("paper.tex regeneration from verified md failed")
    elif _was_sanitized:
        logger.info(
            "Deliverables: using Stage 22 paper.tex (sanitized) — "
            "skipping markdown regeneration to preserve sanitization"
        )

    if not tex_regenerated:
        tex_src = run_dir / "stage-22" / "paper.tex"
        if tex_src.exists() and tex_src.stat().st_size > 0:
            shutil.copy2(tex_src, dest / "paper.tex")
            packaged.append("paper.tex")

    # --- 3. References (BibTeX) ---
    # Prefer verified bib (stage 23) over base bib (stage 22)
    bib_src = None
    for candidate in [
        run_dir / "stage-23" / "references_verified.bib",
        run_dir / "stage-22" / "references.bib",
    ]:
        if candidate.exists() and candidate.stat().st_size > 0:
            bib_src = candidate
            break
    if bib_src is not None:
        shutil.copy2(bib_src, dest / "references.bib")
        packaged.append("references.bib")

    # --- 4. Experiment code package ---
    code_src = run_dir / "stage-22" / "code"
    if code_src.is_dir():
        code_dest = dest / "code"
        if code_dest.exists():
            shutil.rmtree(code_dest)
        shutil.copytree(code_src, code_dest)
        packaged.append("code/")

    # --- 5. Verification report (optional) ---
    verify_src = run_dir / "stage-23" / "verification_report.json"
    if verify_src.exists() and verify_src.stat().st_size > 0:
        shutil.copy2(verify_src, dest / "verification_report.json")
        packaged.append("verification_report.json")

    # --- 5b. Sanitization report (degraded mode) ---
    san_src = run_dir / "stage-22" / "sanitization_report.json"
    if san_src.exists() and san_src.stat().st_size > 0:
        shutil.copy2(san_src, dest / "sanitization_report.json")
        packaged.append("sanitization_report.json")

    # --- 6. Charts (optional) ---
    charts_src = run_dir / "stage-22" / "charts"
    if charts_src.is_dir() and any(charts_src.iterdir()):
        charts_dest = dest / "charts"
        if charts_dest.exists():
            shutil.rmtree(charts_dest)
        shutil.copytree(charts_src, charts_dest)
        packaged.append("charts/")

    # --- 7. Conference style files (.sty, .bst) ---
    try:
        from researchclaw.templates import get_template

        tpl = get_template(effective_conf)
        style_files = tpl.get_style_files()
        for sf in style_files:
            shutil.copy2(sf, dest / sf.name)
            packaged.append(sf.name)
        if style_files:
            logger.info(
                "Deliverables: bundled %d style files for %s",
                len(style_files),
                tpl.display_name,
            )
    except Exception:  # noqa: BLE001
        logger.debug("Style file bundling skipped (template lookup failed)")

    # --- 8. Verify & repair cite key coverage (IMP-12 + IMP-14) ---
    tex_path = dest / "paper.tex"
    bib_path = dest / "references.bib"
    if tex_path.exists() and bib_path.exists():
        try:
            tex_text = tex_path.read_text(encoding="utf-8")
            bib_text = bib_path.read_text(encoding="utf-8")
            import re as _re

            # IMP-15: Deduplicate .bib entries
            _seen_bib_keys: set[str] = set()
            _deduped_entries: list[str] = []
            for _bm in _re.finditer(
                r"(@\w+\{([^,]+),.*?\n\})", bib_text, _re.DOTALL
            ):
                _bkey = _bm.group(2).strip()
                if _bkey not in _seen_bib_keys:
                    _seen_bib_keys.add(_bkey)
                    _deduped_entries.append(_bm.group(1))
            if len(_deduped_entries) < len(
                list(_re.finditer(r"@\w+\{", bib_text))
            ):
                bib_text = "\n\n".join(_deduped_entries) + "\n"
                bib_path.write_text(bib_text, encoding="utf-8")
                logger.info(
                    "Deliverables: deduplicated .bib → %d entries",
                    len(_deduped_entries),
                )

            # Collect all cite keys from \cite{key1, key2}
            all_cite_keys: set[str] = set()
            for cm in _re.finditer(r"\\cite\{([^}]+)\}", tex_text):
                all_cite_keys.update(k.strip() for k in cm.group(1).split(","))
            bib_keys = set(_re.findall(r"@\w+\{([^,]+),", bib_text))
            missing = all_cite_keys - bib_keys

            # IMP-14: Strip orphaned \cite{key} from paper.tex
            if missing:
                logger.warning(
                    "Deliverables: stripping %d orphaned cite keys from "
                    "paper.tex: %s",
                    len(missing),
                    sorted(missing)[:10],
                )

                def _filter_cite(m: _re.Match[str]) -> str:
                    keys = [k.strip() for k in m.group(1).split(",")]
                    kept = [k for k in keys if k not in missing]
                    if not kept:
                        return ""
                    return "\\cite{" + ", ".join(kept) + "}"

                tex_text = _re.sub(r"\\cite\{([^}]+)\}", _filter_cite, tex_text)
                # Clean up whitespace artifacts: double spaces, space before period
                tex_text = _re.sub(r"  +", " ", tex_text)
                tex_text = _re.sub(r" ([.,;:)])", r"\1", tex_text)
                tex_path.write_text(tex_text, encoding="utf-8")
                logger.info(
                    "Deliverables: paper.tex repaired — all remaining cite "
                    "keys verified"
                )
            else:
                logger.info(
                    "Deliverables: all %d cite keys verified in references.bib",
                    len(all_cite_keys),
                )
        except Exception:  # noqa: BLE001
            logger.debug("Cite key verification/repair skipped")

    # --- 9. IMP-18: Compile LaTeX to verify paper.tex ---
    if tex_path.exists() and bib_path.exists():
        try:
            from researchclaw.templates.compiler import compile_latex

            compile_result = compile_latex(tex_path, max_attempts=3, timeout=120)
            if compile_result.success:
                logger.info("IMP-18: paper.tex compiles successfully")
                # Keep the generated PDF
                pdf_path = dest / tex_path.stem
                pdf_file = dest / (tex_path.stem + ".pdf")
                if pdf_file.exists():
                    packaged.append(f"{tex_path.stem}.pdf")
            else:
                logger.warning(
                    "IMP-18: paper.tex compilation failed after %d attempts: %s",
                    compile_result.attempts,
                    compile_result.errors[:3],
                )
            if compile_result.fixes_applied:
                logger.info(
                    "IMP-18: Applied %d auto-fixes: %s",
                    len(compile_result.fixes_applied),
                    compile_result.fixes_applied,
                )
        except Exception:  # noqa: BLE001
            logger.debug("IMP-18: LaTeX compilation skipped (non-blocking)")

    if not packaged:
        # Nothing to package — remove empty dir
        dest.rmdir()
        return None

    # --- Determine release status (never advertise a failed/degraded/
    #     incomplete run as release-ready) ---
    not_release_ready = False
    release_blockers: list[str] = []
    try:
        from researchclaw.pipeline.stages import FINAL_STAGE as _FINAL_STG

        if (run_dir / "degradation_signal.json").exists():
            not_release_ready = True
            release_blockers.append("degradation_signal_present")
        _summary_path = run_dir / "pipeline_summary.json"
        if _summary_path.exists():
            try:
                _sm = json.loads(_summary_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _sm = {}
            if bool(_sm.get("degraded")):
                not_release_ready = True
                release_blockers.append("summary_degraded")
            if _sm.get("stages_failed") not in (0, None):
                not_release_ready = True
                release_blockers.append("stages_failed")
            if _sm.get("stages_blocked") not in (0, None):
                not_release_ready = True
                release_blockers.append("stages_blocked")
            if _sm.get("final_stage") != int(_FINAL_STG) or _sm.get("final_status") != "done":
                not_release_ready = True
                release_blockers.append("incomplete_run")
        else:
            not_release_ready = True
            release_blockers.append("no_pipeline_summary")
    except Exception:  # noqa: BLE001
        not_release_ready = True
        release_blockers.append("release_status_undetermined")

    # --- Write manifest ---
    manifest = {
        "run_id": run_id,
        "target_conference": effective_conf,
        "files": packaged,
        "generated": _utcnow_iso(),
        # Explicit, machine-readable release status. release_check treats a
        # missing/false-but-should-be-true marker as a blocker: deliverables
        # for a non-passing run must NOT look release-ready.
        "release_ready": not not_release_ready,
        "not_release_ready": not_release_ready,
        "release_blockers": release_blockers,
        "release_authority": "scripts/release_check.py is authoritative; this flag is advisory.",
        "notes": {
            "paper_final.md": "Final paper in Markdown format",
            "paper.tex": f"Conference-ready LaTeX ({effective_conf})",
            "references.bib": "BibTeX bibliography (verified citations only)",
            "code/": "Experiment source code with requirements.txt",
            "verification_report.json": "Citation integrity & relevance verification",
            "charts/": "Result visualizations",
        },
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    logger.info(
        "Deliverables packaged: %s (%d items)",
        dest,
        len(packaged),
    )
    return dest


_REFINEMENT_EXHAUSTION_COMMIT_POINTS: dict[str, tuple[str, ...]] = {
    "stage-16": (
        "outline_binding.json",
        "outline.md",
        "citation_plan.json",
        "citation_plan.preliminary.json",
        "citation_policy_effective.json",
    ),
    "stage-17": (
        "paper_meta.json",
        "paper_draft.md",
        "paper_structure_report.json",
        "experiment_fact_closure_report.json",
        "citation_closure_report.json",
        "references_preverified.bib",
    ),
    "stage-18": ("review_structure_report.json", "reviews.md"),
    "stage-19": (
        "section_revision_manifest.json",
        "revision_evidence_binding.json",
        "paper_revised.md",
    ),
    "stage-20": (
        "quality_gate_manifest.json",
        "quality_report.json",
        "fabrication_flags.json",
    ),
    "stage-21": ("bundle_index.json", "archive.md"),
    "stage-22": ("stage22_export_manifest.json",),
    "stage-23": ("stage23_verification_manifest.json",),
    "stage-24": ("stage24_truth_manifest.json",),
    "stage-25": ("stage25_deai_manifest.json",),
}


def _read_run_regular_file_bound(
    run_dir: Path, release_lock: object, relative_path: str
) -> bytes | None:
    """Read one run-relative regular file through the held writer inode."""

    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
    parts = tuple(relative_path.split("/"))
    if not parts or any(
        not part or part in {".", ".."} or "\\" in part for part in parts
    ):
        raise OSError(f"invalid run-relative path: {relative_path}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = writer.duplicate_run_fd()
    opened = [descriptor]
    try:
        for part in parts[:-1]:
            try:
                descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            opened.append(descriptor)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=descriptor)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"run input is not a regular file: {relative_path}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise OSError(f"run input changed while reading: {relative_path}")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        for opened_fd in reversed(opened):
            os.close(opened_fd)


def _read_pivot_count_bound(run_dir: Path, release_lock: object) -> int:
    raw = _read_run_regular_file_bound(
        run_dir, release_lock, "decision_history.json"
    )
    if raw is None:
        return 0
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def _record_decision_history_bound(
    run_dir: Path,
    decision: str,
    rollback_target: Stage,
    attempt: int,
    release_lock: object,
) -> None:
    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
    raw = _read_run_regular_file_bound(
        run_dir, release_lock, "decision_history.json"
    )
    history: list[dict[str, object]] = []
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            history = data
    history.append(
        {
            "decision": decision,
            "rollback_target": rollback_target.name,
            "rollback_stage_num": int(rollback_target),
            "attempt": attempt,
            "timestamp": _utcnow_iso(),
        }
    )
    writer.write_run_bytes_atomic(
        "decision_history.json", json.dumps(history, indent=2).encode("utf-8")
    )


def _consecutive_empty_metrics_bound(
    run_dir: Path, pivot_count: int, release_lock: object
) -> bool:
    current_relative = "stage-14/experiment_summary.json"
    if _read_run_regular_file_bound(run_dir, release_lock, current_relative) is None:
        for version in range(pivot_count + 1, 0, -1):
            candidate = f"stage-14_v{version}/experiment_summary.json"
            if _read_run_regular_file_bound(run_dir, release_lock, candidate) is not None:
                current_relative = candidate
                break
    previous_relative = f"stage-14_v{pivot_count}/experiment_summary.json"
    for relative in (current_relative, previous_relative):
        raw = _read_run_regular_file_bound(run_dir, release_lock, relative)
        if raw is None:
            return False
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        metrics_summary = data.get("metrics_summary", {})
        best_run = data.get("best_run", {})
        if (isinstance(metrics_summary, dict) and metrics_summary) or (
            isinstance(best_run, dict) and best_run.get("metrics")
        ):
            return False
    return True


def _invalidate_decision_resume_pointers(
    run_dir: Path, release_lock: object
) -> None:
    from researchclaw.pipeline.release_graph_lock import require_active_writer_epoch

    writer = require_active_writer_epoch(run_dir, release_lock)
    errors: list[str] = []
    for name in ("checkpoint.json", "heartbeat.json"):
        try:
            writer.remove_run_files((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise OSError("decision resume pointer invalidation failed: " + "; ".join(errors))


def _append_rollback_attempt_bound(
    run_dir: Path,
    run_id: str,
    rollback_target: Stage,
    decision: str,
    release_lock: object,
) -> None:
    from researchclaw.pipeline import release_artifacts
    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
    run_fd = writer.duplicate_run_fd()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        try:
            attempts_info = os.stat(
                "attempts", dir_fd=run_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            os.mkdir("attempts", 0o700, dir_fd=run_fd)
        else:
            if not stat.S_ISDIR(attempts_info.st_mode):
                raise OSError("attempt log namespace is unsafe")
        attempts_fd = os.open("attempts", directory_flags, dir_fd=run_fd)
        try:
            try:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                flags |= getattr(os, "O_CLOEXEC", 0)
                log_fd = os.open("attempt_log.jsonl", flags, dir_fd=attempts_fd)
            except FileNotFoundError:
                existing = b""
            else:
                try:
                    log_info = os.fstat(log_fd)
                    if not stat.S_ISREG(log_info.st_mode):
                        raise OSError("attempt log is not a regular file")
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(log_fd, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    existing = b"".join(chunks)
                finally:
                    os.close(log_fd)
            prior = 0
            try:
                lines = existing.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise OSError("attempt log is not UTF-8") from exc
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("stage") == int(rollback_target)
                    and item.get("kind") == "decision_rollback"
                ):
                    prior += 1
            entry = {
                "schema_version": release_artifacts.SCHEMA_VERSION,
                "kind": "decision_rollback",
                "run_id": run_id,
                "stage": int(rollback_target),
                "stage_name": rollback_target.name,
                "attempt": prior + 1,
                "attempt_id": f"stage{int(rollback_target):02d}-a{prior + 1}",
                "status": "rolled_back_to",
                "decision": decision,
                "error": None,
                "elapsed_sec": None,
                "artifacts": [],
                "timestamp": release_artifacts.utcnow_iso(),
            }
            prefix = existing
            if prefix and not prefix.endswith(b"\n"):
                prefix += b"\n"
            content = prefix + json.dumps(
                entry, ensure_ascii=False, default=str
            ).encode("utf-8") + b"\n"
            temporary = f".attempt_log.jsonl.tmp-{uuid.uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            temporary_fd = os.open(temporary, flags, 0o600, dir_fd=attempts_fd)
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(temporary_fd, content[offset:])
                    if written <= 0:
                        raise OSError("attempt log write made no progress")
                    offset += written
                os.fsync(temporary_fd)
            except Exception:
                try:
                    os.unlink(temporary, dir_fd=attempts_fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(temporary_fd)
            os.replace(
                temporary,
                "attempt_log.jsonl",
                src_dir_fd=attempts_fd,
                dst_dir_fd=attempts_fd,
            )
            os.fsync(attempts_fd)
        finally:
            os.close(attempts_fd)
    finally:
        os.close(run_fd)


def _version_rollback_stages_bound(
    run_dir: Path,
    rollback_target: Stage,
    attempt: int,
    release_lock: object,
    *,
    incremental: bool = False,
) -> None:
    from researchclaw.pipeline.bound_output_namespace import (
        _copy_tree_fd_to_fd,
        _remove_tree_at,
    )
    from researchclaw.pipeline.release_graph_lock import require_active_writer_epoch

    writer = require_active_writer_epoch(run_dir, release_lock)
    run_fd = writer.duplicate_run_fd()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        for stage_num in range(int(rollback_target), int(Stage.RESEARCH_DECISION) + 1):
            stage_name = f"stage-{stage_num:02d}"
            archive_name = f"{stage_name}_v{attempt}"
            try:
                source_info = os.stat(
                    stage_name, dir_fd=run_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(source_info.st_mode):
                raise OSError(f"rollback stage is unsafe: {stage_name}")
            _remove_tree_at(run_fd, archive_name)
            if incremental and stage_num >= int(Stage.EXPERIMENT_RUN):
                os.mkdir(archive_name, 0o700, dir_fd=run_fd)
                source_fd = os.open(stage_name, directory_flags, dir_fd=run_fd)
                try:
                    destination_fd = os.open(
                        archive_name, directory_flags, dir_fd=run_fd
                    )
                    try:
                        _copy_tree_fd_to_fd(source_fd, destination_fd)
                        source_after = os.fstat(source_fd)
                        source_entry = os.stat(
                            stage_name, dir_fd=run_fd, follow_symlinks=False
                        )
                        if (source_after.st_dev, source_after.st_ino) != (
                            source_entry.st_dev,
                            source_entry.st_ino,
                        ):
                            raise OSError(
                                f"rollback stage changed while copying: {stage_name}"
                            )
                    finally:
                        os.close(destination_fd)
                except Exception:
                    _remove_tree_at(run_fd, archive_name)
                    raise
                finally:
                    os.close(source_fd)
            else:
                os.rename(
                    stage_name,
                    archive_name,
                    src_dir_fd=run_fd,
                    dst_dir_fd=run_fd,
                )
    finally:
        os.close(run_fd)
    writer.assert_canonical()


def _capture_refinement_exhaustion_namespaces(
    run_dir: Path,
    release_lock: object,
    stack: ExitStack,
) -> dict[str, Any | None]:
    """Capture existing downstream stage inodes before Stage 15 executes."""

    from researchclaw.pipeline.release_graph_lock import require_active_writer_epoch

    writer = require_active_writer_epoch(run_dir, release_lock)
    namespaces: dict[str, Any | None] = {}
    for stage_name in _REFINEMENT_EXHAUSTION_COMMIT_POINTS:
        try:
            namespace = writer.open_stage_namespace(stage_name)
        except FileNotFoundError:
            namespaces[stage_name] = None
            continue
        namespaces[stage_name] = stack.enter_context(namespace)
    return namespaces


def _invalidate_refinement_exhaustion_authority(
    run_dir: Path,
    release_lock: object,
    namespaces: dict[str, Any | None],
) -> None:
    """Withdraw every downstream commit point without following live symlinks."""

    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    require_active_writer_invalidation_epoch(run_dir, release_lock)
    if set(namespaces) != set(_REFINEMENT_EXHAUSTION_COMMIT_POINTS):
        raise RuntimeError("refinement exhaustion namespace set mismatch")
    # Validate the complete namespace set before withdrawing any authority.
    # A foreign or wrong-stage entry must not cause partial deletion.
    for stage_name, names in _REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        namespace = namespaces[stage_name]
        if namespace is None:
            continue
        _require_detached_namespace_owned_by_writer(
            run_dir, release_lock, namespace, stage_name
        )
    errors: list[str] = []
    for stage_name, names in _REFINEMENT_EXHAUSTION_COMMIT_POINTS.items():
        namespace = namespaces[stage_name]
        if namespace is None:
            continue
        for name in names:
            try:
                namespace.remove_flat_entries((name,))
            except OSError as exc:
                errors.append(f"{stage_name}/{name}: {exc}")
        try:
            namespace.assert_canonical()
        except OSError as exc:
            errors.append(f"{stage_name} identity: {exc}")
    if errors:
        raise OSError(
            "refinement exhaustion authority cleanup was incomplete: "
            + "; ".join(errors)
        )


def _require_detached_namespace_owned_by_writer(
    run_dir: Path,
    release_lock: object,
    namespace: Any,
    expected_stage: str,
) -> None:
    """Bind a captured stage fd to a writer without resolving the live path."""

    from researchclaw.pipeline.release_graph_lock import (
        require_active_writer_invalidation_epoch,
    )

    writer = require_active_writer_invalidation_epoch(run_dir, release_lock)
    if (
        getattr(namespace, "stage_name", None) != expected_stage
        or str(getattr(namespace, "run_dir", Path()).absolute())
        != str(run_dir.absolute())
        or getattr(namespace, "stage_dir", None) != run_dir / expected_stage
    ):
        raise RuntimeError("refinement exhaustion namespace binding mismatch")
    writer_fd = writer.duplicate_run_fd()
    try:
        writer_info = os.fstat(writer_fd)
        namespace_run_info = os.fstat(namespace._run_fd)
        namespace_stage_info = os.fstat(namespace._stage_fd)
    except (AttributeError, OSError) as exc:
        raise RuntimeError("refinement exhaustion namespace is inactive") from exc
    finally:
        os.close(writer_fd)
    if (
        not stat.S_ISDIR(namespace_run_info.st_mode)
        or not stat.S_ISDIR(namespace_stage_info.st_mode)
        or (writer_info.st_dev, writer_info.st_ino)
        != (namespace_run_info.st_dev, namespace_run_info.st_ino)
        or getattr(namespace, "_run_identity", None)
        != (namespace_run_info.st_dev, namespace_run_info.st_ino)
        or getattr(namespace, "_stage_identity", None)
        != (namespace_stage_info.st_dev, namespace_stage_info.st_ino)
    ):
        raise RuntimeError("refinement exhaustion namespace epoch mismatch")


def _finalize_refinement_exhausted(
    run_dir: Path,
    decision_result: StageResult,
    *,
    reason: str,
    release_lock: object,
    namespaces: dict[str, Any | None],
) -> StageResult:
    error = f"refinement_exhausted: {reason}"
    try:
        _invalidate_refinement_exhaustion_authority(
            run_dir, release_lock, namespaces
        )
    except Exception as exc:  # noqa: BLE001
        error += f"; downstream authority cleanup failed: {exc}"
    return StageResult(
        stage=Stage.RESEARCH_DECISION,
        status=StageStatus.FAILED,
        artifacts=decision_result.artifacts,
        evidence_refs=decision_result.evidence_refs,
        error=error,
        decision="refinement_exhausted",
    )


def _version_rollback_stages(
    run_dir: Path,
    rollback_target: Stage,
    attempt: int,
    *,
    incremental: bool = False,
) -> None:
    """Snapshot stage directories that will be re-executed by a PIVOT/REFINE
    or by an explicit incremental re-entry.

    Default behavior renames ``stage-NN/`` to ``stage-NN_v{attempt}/`` so the
    next run starts from a clean slate.

    When ``incremental=True``, directories whose number is >= EXPERIMENT_RUN (12)
    are *copied* via ``shutil.copytree`` instead of renamed, so the live
    stage-12 workspace persists across re-entries. Stages before EXPERIMENT_RUN
    in the rollback range are still renamed.
    """
    import shutil

    rollback_num = int(rollback_target)
    decision_num = int(Stage.RESEARCH_DECISION)
    exp_run_num = int(Stage.EXPERIMENT_RUN)

    for stage_num in range(rollback_num, decision_num + 1):
        stage_dir = run_dir / f"stage-{stage_num:02d}"
        if not stage_dir.exists():
            continue
        version_dir = run_dir / f"stage-{stage_num:02d}_v{attempt}"
        if version_dir.exists():
            shutil.rmtree(version_dir)
        if incremental and stage_num >= exp_run_num:
            shutil.copytree(stage_dir, version_dir, symlinks=False)
            logger.debug(
                "Snapshotted (copytree) %s → %s (incremental)",
                stage_dir.name,
                version_dir.name,
            )
        else:
            stage_dir.rename(version_dir)
            logger.debug(
                "Versioned (rename) %s → %s", stage_dir.name, version_dir.name
            )


def _consecutive_empty_metrics(run_dir: Path, pivot_count: int) -> bool:
    """R6-4: Check if the current and previous REFINE cycles both produced empty metrics."""
    # Check the most recent experiment_summary.json (stage-14) and its versioned predecessor.
    # BUG-215: When stage-14/ doesn't exist (renamed to stage-14_v{N} without
    # promotion), fall back to the latest versioned directory as "current".
    current = run_dir / "stage-14" / "experiment_summary.json"
    if not current.exists():
        # Try the latest versioned directory
        for _v in range(pivot_count + 1, 0, -1):
            alt = run_dir / f"stage-14_v{_v}" / "experiment_summary.json"
            if alt.exists():
                current = alt
                break
    prev = run_dir / f"stage-14_v{pivot_count}" / "experiment_summary.json"
    for path in (current, prev):
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Check all possible metric locations
            has_metrics = False
            ms = data.get("metrics_summary", {})
            if isinstance(ms, dict) and ms:
                has_metrics = True
            br = data.get("best_run", {})
            if isinstance(br, dict) and br.get("metrics"):
                has_metrics = True
            if has_metrics:
                return False  # At least one cycle had real metrics
        except (json.JSONDecodeError, OSError, AttributeError):
            return False
    return True  # Both cycles had empty metrics


def _promote_best_stage14(run_dir: Path, config: RCConfig) -> None:
    """Replay immutable candidates and publish the deterministic winner under lock."""
    from researchclaw.pipeline.canonical_execution_controller import (
        CanonicalAnalysisController,
    )
    from researchclaw.pipeline.canonical_experiment_evidence import (
        _publish_canonical_experiment_manifest_under_controller,
    )

    controller = CanonicalAnalysisController.acquire_promotion(run_dir)
    try:
        _publish_canonical_experiment_manifest_under_controller(
            controller, run_dir, config
        )
    finally:
        controller.close()


def _read_pivot_count(run_dir: Path) -> int:
    """Read how many PIVOT/REFINE decisions have been made so far."""
    history_path = run_dir / "decision_history.json"
    if not history_path.exists():
        return 0
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _record_decision_history(
    run_dir: Path, decision: str, rollback_target: Stage, attempt: int
) -> None:
    """Append a decision event to the history log."""
    history_path = run_dir / "decision_history.json"
    history: list[dict[str, object]] = []
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
        except (json.JSONDecodeError, OSError):
            pass
    history.append({
        "decision": decision,
        "rollback_target": rollback_target.name,
        "rollback_stage_num": int(rollback_target),
        "attempt": attempt,
        "timestamp": _utcnow_iso(),
    })
    history_path.write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )



def _read_quality_score(run_dir: Path) -> float | None:
    """Extract quality score from the most recent quality_report.json."""
    report_path = run_dir / "stage-20" / "quality_report.json"
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Try common keys: score_1_to_10, score, quality_score
            for key in ("score_1_to_10", "score", "quality_score", "overall_score"):
                if key in data:
                    return float(data[key])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _write_iteration_context(
    run_dir: Path, iteration: int, reviews: str, quality_score: float | None
) -> None:
    """Write iteration feedback file so next round can read it."""
    ctx = {
        "iteration": iteration,
        "quality_score": quality_score,
        "reviews_excerpt": reviews[:3000] if reviews else "",
        "generated": _utcnow_iso(),
    }
    (run_dir / "iteration_context.json").write_text(
        json.dumps(ctx, indent=2), encoding="utf-8"
    )


def execute_iterative_pipeline(
    *,
    run_dir: Path,
    run_id: str,
    config: RCConfig,
    adapters: AdapterBundle,
    auto_approve_gates: bool = False,
    kb_root: Path | None = None,
    max_iterations: int = 3,
    quality_threshold: float = 7.0,
    convergence_rounds: int = 2,
) -> dict[str, object]:
    """Run the full pipeline with iterative quality improvement.

    After the first full pass (stages 1-22), if the quality gate score is below
    *quality_threshold*, re-run stages 16-22 (paper writing + finalization) with
    review feedback injected.  Stop when:
      - Score >= quality_threshold, OR
      - Score hasn't improved for *convergence_rounds* consecutive iterations, OR
      - *max_iterations* reached.

    Returns a summary dict with iteration history.
    """
    iteration_scores: list[float | None] = []
    all_results: list[list[StageResult]] = []

    # --- First full pass ---
    logger.info("Iteration 1/%d: running full pipeline (stages 1-22)", max_iterations)
    results = execute_pipeline(
        run_dir=run_dir,
        run_id=f"{run_id}-iter1",
        config=config,
        adapters=adapters,
        auto_approve_gates=auto_approve_gates,
        kb_root=kb_root,
    )
    all_results.append(results)
    score = _read_quality_score(run_dir)
    iteration_scores.append(score)
    logger.info("Iteration 1 score: %s", score)

    # --- Iterative improvement ---
    for iteration in range(2, max_iterations + 1):
        # Check if we've met quality threshold
        if score is not None and score >= quality_threshold:
            logger.info(
                "Quality threshold %.1f met (score=%.1f). Stopping.",
                quality_threshold,
                score,
            )
            break

        # Check convergence (score hasn't improved)
        if len(iteration_scores) >= convergence_rounds:
            recent = iteration_scores[-convergence_rounds:]
            if all(s is not None for s in recent):
                recent_scores = [float(s) for s in recent if s is not None]
                if max(recent_scores) - min(recent_scores) < 0.5:
                    logger.info(
                        "Convergence detected: scores %s unchanged for %d rounds. Stopping.",
                        recent,
                        convergence_rounds,
                    )
                    break

        # Write iteration context with feedback from reviews
        reviews_text = ""
        reviews_path = run_dir / "stage-18" / "reviews.md"
        if reviews_path.exists():
            reviews_text = reviews_path.read_text(encoding="utf-8")
        _write_iteration_context(run_dir, iteration, reviews_text, score)

        # Re-run from PAPER_OUTLINE (stage 16) through EXPORT_PUBLISH (stage 22)
        logger.info(
            "Iteration %d/%d: re-running stages 16-22 with feedback",
            iteration,
            max_iterations,
        )
        results = execute_pipeline(
            run_dir=run_dir,
            run_id=f"{run_id}-iter{iteration}",
            config=config,
            adapters=adapters,
            from_stage=Stage.PAPER_OUTLINE,
            auto_approve_gates=auto_approve_gates,
            kb_root=kb_root,
        )
        all_results.append(results)
        score = _read_quality_score(run_dir)
        iteration_scores.append(score)
        logger.info("Iteration %d score: %s", iteration, score)

    # --- Build iterative summary ---
    converged = False
    if len(iteration_scores) >= convergence_rounds:
        recent_window = iteration_scores[-convergence_rounds:]
        if all(s is not None for s in recent_window):
            recent_scores = [float(s) for s in recent_window if s is not None]
            converged = max(recent_scores) - min(recent_scores) < 0.5

    summary: dict[str, object] = {
        "run_id": run_id,
        "total_iterations": len(iteration_scores),
        "iteration_scores": iteration_scores,
        "quality_threshold": quality_threshold,
        "converged": converged,
        "final_score": iteration_scores[-1] if iteration_scores else None,
        "met_threshold": score is not None and score >= quality_threshold,
        "stages_per_iteration": [len(r) for r in all_results],
        "generated": _utcnow_iso(),
    }
    (run_dir / "iteration_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    # --- Package deliverables into a single folder ---
    try:
        deliverables_dir = _package_deliverables(run_dir, run_id, config)
        if deliverables_dir is not None:
            print(f"[{run_id}] Deliverables packaged →{deliverables_dir}")
    except Exception:  # noqa: BLE001
        logger.warning("Deliverables packaging failed (non-blocking)")

    return summary


def _metaclaw_post_pipeline(
    config: RCConfig,
    results: list[StageResult],
    lessons: list[object],
    run_id: str,
    run_dir: Path,
) -> None:
    """MetaClaw bridge: post-pipeline hook.

    Canonical policy v1 permits only a session-end notification. Durable lesson
    conversion and StageResult-derived skill feedback are not authoritative.
    """
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities("metaclaw_post_pipeline")
    bridge = getattr(config, "metaclaw_bridge", None)
    if not bridge or not getattr(bridge, "enabled", False):
        return

    del results, lessons, run_dir

    # Signal session end (fire-and-forget).
    try:
        from researchclaw.metaclaw_bridge.session import MetaClawSession
        import json as _json
        import urllib.request as _urllib_req

        session = MetaClawSession(run_id)
        end_headers = session.end()
        # Send a minimal request to signal session end
        proxy_url = getattr(bridge, "proxy_url", "http://localhost:30000")
        url = f"{proxy_url.rstrip('/')}/v1/chat/completions"
        body = _json.dumps({
            "model": "session-end",
            "messages": [{"role": "user", "content": "session complete"}],
            "max_tokens": 1,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(end_headers)
        req = _urllib_req.Request(url, data=body, headers=headers)
        try:
            _urllib_req.urlopen(req, timeout=5)
        except Exception:  # noqa: BLE001
            pass  # Best-effort signal
    except Exception:  # noqa: BLE001
        pass
