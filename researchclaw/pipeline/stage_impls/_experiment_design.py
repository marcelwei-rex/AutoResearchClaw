"""Stage 9: Experiment design."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    contract_sha256,
    derive_contract,
    derive_execution_spec,
    dump_contract,
    execution_spec_bytes,
    execution_spec_diagnostics_bytes,
    load_contract_bytes,
)
from researchclaw.experiment_runtime.metric_authority import (
    _replay_metric_authority_selection,
    MetricAuthorityError,
    MetricAuthoritySelection,
    build_domain_evaluator_capture_plan,
    config_allows_domain_evaluator_capture,
    derive_domain_evaluator_experiment_plan,
    select_metric_authority,
)
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline._helpers import (
    StageResult,
    _build_context_preamble,
    _chat_with_prompt,
    _extract_yaml_block,
    _get_pipeline_evolution_overlay,
    _load_hardware_profile,
    _read_prior_artifact,
    _safe_json_loads,
    _utcnow_iso,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


_STAGE9_OWNED_OUTPUTS = (
    "benchmark_agent",
    "benchmark_plan.json",
    "domain_profile.json",
    "domain_selector_policy.json",
    "domain_evaluator_execution_policy.json",
    "domain_evaluator_package_manifest.json",
    "exp_plan.yaml",
    "experiment_contract.sha256",
    "experiment_contract.yaml",
    "execution_spec.json",
    "execution_spec_diagnostics.json",
    "metric_authority.json",
    "metric_authority_index.json",
    "plan_meta.json",
    "prompt_domain_profile.json",
)

_STAGE9_AUTHORITY_OUTPUTS = (
    "experiment_contract.yaml",
    "experiment_contract.sha256",
    "execution_spec.json",
    "domain_selector_policy.json",
    "domain_profile.json",
    "metric_authority_index.json",
    "metric_authority.json",
    "domain_evaluator_package_manifest.json",
    "domain_evaluator_execution_policy.json",
)

_STAGE9_DIAGNOSTIC_OUTPUTS = tuple(
    name for name in _STAGE9_OWNED_OUTPUTS if name not in _STAGE9_AUTHORITY_OUTPUTS
)


def _cleanup_stage9_outputs(
    namespace: BoundOutputNamespace,
    *,
    preserve_diagnostics: tuple[str, ...] = (),
) -> None:
    """Invalidate authority first and never let one collision stop cleanup."""

    preserved = set(preserve_diagnostics)
    if not preserved.issubset(_STAGE9_DIAGNOSTIC_OUTPUTS):
        raise OSError("Stage 9 cleanup may preserve diagnostic outputs only")
    errors: list[str] = []
    for name in _STAGE9_AUTHORITY_OUTPUTS:
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    for name in _STAGE9_DIAGNOSTIC_OUTPUTS:
        if name in preserved:
            continue
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise OSError("Stage 9 cleanup was incomplete: " + "; ".join(errors))


def _normalize_plan_field(value: Any) -> list:
    """Normalize a plan field (baselines, proposed_methods, ablations, datasets)
    from any shape the LLM might produce into a flat list of items.

    Handles: list[str], list[dict], dict[str, Any], str, None.
    When the input is a dict, we preserve the full structure by converting each
    key-value pair into a dict item (with at least a 'name' key), rather than
    discarding either keys or values.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        result = []
        for k, v in value.items():
            if isinstance(v, dict):
                # e.g. {"baseline_1": {"params": ...}} -> {"name": "baseline_1", "params": ...}
                item = dict(v)
                item.setdefault("name", str(k))
                result.append(item)
            else:
                # e.g. {"baseline_1": "description"} -> {"name": "baseline_1", "description": str(v)}
                result.append({"name": str(k), "description": str(v) if v else ""})
        return result
    if isinstance(value, list):
        return list(value)
    return [value]


def _plan_field_names(items: list) -> list[str]:
    """Extract string names from a normalized plan field for display/dedup."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(item.get("name", str(item)))
        else:
            result.append(str(item))
    return result


def _execute_experiment_design(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
    namespace: BoundOutputNamespace | None = None,
    authority_selection: MetricAuthoritySelection | None = None,
) -> StageResult:
    if namespace is not None:
        if namespace.run_dir != run_dir or namespace.stage_dir != stage_dir:
            return StageResult(
                stage=Stage.EXPERIMENT_DESIGN,
                status=StageStatus.FAILED,
                artifacts=(),
                error="Stage 9 held namespace does not match producer paths",
                decision="retry",
            )
        return _execute_experiment_design_in_namespace(
            stage_dir,
            run_dir,
            config,
            adapters,
            namespace=namespace,
            authority_selection=authority_selection,
            llm=llm,
            prompts=prompts,
        )
    try:
        with BoundOutputNamespace.open(
            run_dir, stage_dir, "stage-09"
        ) as namespace:
            return _execute_experiment_design_in_namespace(
                stage_dir,
                run_dir,
                config,
                adapters,
                namespace=namespace,
                authority_selection=authority_selection,
                llm=llm,
                prompts=prompts,
            )
    except OSError as exc:
        return StageResult(
            stage=Stage.EXPERIMENT_DESIGN,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 9 output namespace is unsafe: {exc}",
            decision="retry",
        )


def _execute_experiment_design_in_namespace(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    namespace: BoundOutputNamespace,
    authority_selection: MetricAuthoritySelection | None,
    llm: LLMClient | None,
    prompts: PromptManager | None,
) -> StageResult:
    try:
        _cleanup_stage9_outputs(namespace)
        namespace.assert_canonical()
        if authority_selection is None:
            authority_selection = select_metric_authority(
                config.research.topic,
                config.experiment.mode,
                allow_domain_evaluator_capture=config_allows_domain_evaluator_capture(config),
            )
        result = _execute_experiment_design_bound(
            stage_dir,
            run_dir,
            config,
            adapters,
            namespace=namespace,
            authority_selection=authority_selection,
            llm=llm,
            prompts=prompts,
        )
        if result.status == StageStatus.DONE:
            _validate_stage9_publication(namespace, run_dir, config)
        else:
            preserved = tuple(
                name
                for name in result.artifacts
                if name in _STAGE9_DIAGNOSTIC_OUTPUTS
            )
            _cleanup_stage9_outputs(
                namespace, preserve_diagnostics=preserved
            )
        namespace.assert_canonical()
        return result
    except Exception as exc:  # noqa: BLE001
        try:
            _cleanup_stage9_outputs(namespace)
        except OSError as cleanup_exc:
            exc.add_note(f"Stage 9 fd-bound cleanup also failed: {cleanup_exc}")
        return StageResult(
            stage=Stage.EXPERIMENT_DESIGN,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 9 publication failed: {exc}",
            decision="retry",
        )


def _execute_experiment_design_bound(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    namespace: BoundOutputNamespace,
    authority_selection: MetricAuthoritySelection,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    # Keep executor-owned decision.json/stage_health.json. Contract selection
    # uses them to distinguish an authoritative incomplete direct attempt from
    # a legacy run that may safely fall back to a versioned Stage 9 contract.
    hypotheses = _read_prior_artifact(run_dir, "hypotheses.md") or ""
    preamble = _build_context_preamble(
        config, run_dir, include_goal=True, include_hypotheses=True
    )
    plan: dict[str, Any] | None = None
    domain_capture_plan = None
    selected_authority = authority_selection
    if selected_authority.schema_version == 2:
        domain_capture_plan = build_domain_evaluator_capture_plan(
            selected_authority
        )
        plan = derive_domain_evaluator_experiment_plan(domain_capture_plan)

    # ── Domain detection ──────────────────────────────────────────────────
    # Detect the research domain early so we can adapt experiment design
    # and code generation. For ML domains, existing behavior is unchanged.
    _domain_profile = None
    try:
        from researchclaw.domains.detector import detect_domain as _detect_domain_adv
        _domain_profile = _detect_domain_adv(
            topic=config.research.topic,
            hypotheses=hypotheses,
        )
        logger.info(
            "Domain detected: %s (%s)",
            _domain_profile.display_name,
            _domain_profile.domain_id,
        )
        # Persist domain profile for Stage 10
        import json as _json_dd
        namespace.write_text_atomic(
            "prompt_domain_profile.json",
            _json_dd.dumps({
                "domain_id": _domain_profile.domain_id,
                "display_name": _domain_profile.display_name,
                "experiment_paradigm": _domain_profile.experiment_paradigm,
                "core_libraries": _domain_profile.core_libraries,
                "gpu_required": _domain_profile.gpu_required,
            }, indent=2),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Domain detection unavailable", exc_info=True)

    # --- Domain-specific experiment design context (YAML-driven overlay) ---
    # For ML and HEP, the active prompt bank is already domain-native so we
    # leave this empty. For other profiles (biology, physics, economics, …)
    # the GenericPromptAdapter injects YAML-defined guidance here.
    _domain_design_context = ""
    if _domain_profile is not None:
        try:
            from researchclaw.domains.prompt_adapter import get_adapter as _get_prompt_adapter
            _adapter = _get_prompt_adapter(_domain_profile)
            _design_blocks = _adapter.get_experiment_design_blocks(
                {"topic": config.research.topic}
            )
            if _design_blocks.experiment_design_context:
                _domain_design_context = (
                    "## Domain-Specific Experiment Guidelines\n"
                    + _design_blocks.experiment_design_context
                    + "\n\n"
                )
                if _design_blocks.statistical_test_guidance:
                    _domain_design_context += (
                        "## Statistical Analysis Guidance\n"
                        + _design_blocks.statistical_test_guidance + "\n\n"
                    )
                logger.info(
                    "ExperimentDesign: injecting YAML-driven domain context for %s",
                    _domain_profile.domain_id,
                )
        except Exception:  # noqa: BLE001
            logger.debug("Domain experiment design context unavailable", exc_info=True)

    if llm is not None and domain_capture_plan is None:
        _pm = prompts or PromptManager()
        # Pass dataset_guidance block for experiment design
        try:
            _dg_block = _pm.block("dataset_guidance")
        except (KeyError, Exception):  # noqa: BLE001
            _dg_block = ""
        # I-08: Inject RL step guidance for RL topics
        _rl_kws = ("reinforcement learning", "ppo", "sac", "td3", "ddpg",
                    "dqn", "mujoco", "continuous control", "actor-critic",
                    "policy gradient", "exploration bonus")
        _is_rl_topic = any(kw in config.research.topic.lower() for kw in _rl_kws)
        if _is_rl_topic:
            try:
                _dg_block += _pm.block("rl_step_guidance")
            except Exception:  # noqa: BLE001
                pass
            # Improvement G: For RL with short budget, constrain to classic control
            if config.experiment.time_budget_sec <= 3600:
                _dg_block += (
                    "\n\n## RL TIME CONSTRAINT (MANDATORY):\n"
                    f"Your time budget is {config.experiment.time_budget_sec}s (≤ 3600s).\n"
                    "You MUST use ONLY classic control environments: "
                    "CartPole-v1, Pendulum-v1, MountainCar-v0, Acrobot-v1, LunarLander-v3.\n"
                    "Do NOT use MuJoCo (HalfCheetah, Hopper, Walker2d, Ant, Humanoid) — "
                    "they require >5000s for meaningful training.\n"
                )
            if config.experiment.time_budget_sec <= 1800:
                _dg_block += (
                    "Time budget ≤ 1800s: use ONLY CartPole-v1 or Pendulum-v1 "
                    "(the simplest environments).\n"
                )
        # F-01: Inject framework docs for experiment design
        try:
            from researchclaw.data import detect_frameworks, load_framework_docs
            _fw_ids = detect_frameworks(config.research.topic, hypotheses)
            if _fw_ids:
                _fw_docs = load_framework_docs(_fw_ids, max_chars=4000)
                if _fw_docs:
                    _dg_block += _fw_docs
        except Exception:  # noqa: BLE001
            pass
        # Improvement A: Compute hardware profile + per-condition budget
        _hw_profile_str = (
            "- GPU: NVIDIA RTX 6000 Ada (49140 MB VRAM)\n"
            "- GPU count: 1\n"
            "- CPU: shared server"
        )
        _per_condition_sec = int(config.experiment.time_budget_sec * 0.7 / 6)
        _tier1 = "CIFAR-10, CIFAR-100, MNIST, FashionMNIST, STL-10, SVHN"

        _overlay = _get_pipeline_evolution_overlay(run_dir, "experiment_design")
        sp = _pm.for_stage(
            "experiment_design",
            evolution_overlay=_overlay,
            preamble=preamble,
            hypotheses=hypotheses,
            dataset_guidance=_dg_block,
            domain_design_context=_domain_design_context,
            time_budget_sec=config.experiment.time_budget_sec,
            metric_key=config.experiment.metric_key,
            metric_direction=config.experiment.metric_direction,
            hardware_profile=_hw_profile_str,
            per_condition_budget_sec=_per_condition_sec,
            available_tier1_datasets=_tier1,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        raw_yaml = _extract_yaml_block(resp.content)
        try:
            parsed = yaml.safe_load(raw_yaml)
        except yaml.YAMLError:
            parsed = None
        # Fallback: reasoning models sometimes emit the YAML without fences
        # or wrapped in prose. Try parsing the whole response as YAML.
        if not isinstance(parsed, dict):
            try:
                parsed = yaml.safe_load(resp.content)
            except yaml.YAMLError:
                pass
        # Last fallback: try to find any YAML-like dict in the response
        if not isinstance(parsed, dict):
            import re as _re_yaml

            # Look for lines starting with known keys
            _yaml_lines = []
            _capturing = False
            for line in resp.content.splitlines():
                if _re_yaml.match(
                    r"^(baselines|proposed_methods|ablations|datasets|"
                    r"metrics|objectives|risks|compute_budget)\s*:",
                    line,
                ):
                    _capturing = True
                if _capturing:
                    if line.strip() == "" or line.startswith("```"):
                        continue
                    if line.startswith("#") or line.startswith("**"):
                        continue
                    _yaml_lines.append(line)
            if _yaml_lines:
                try:
                    parsed = yaml.safe_load("\n".join(_yaml_lines))
                except yaml.YAMLError:
                    pass
        if isinstance(parsed, dict):
            plan = parsed
        else:
            logger.warning(
                "Stage 09: LLM response could not be parsed as YAML "
                "(len=%d, first 200 chars: %s). Content extraction method "
                "returned: %s",
                len(resp.content),
                resp.content[:200],
                raw_yaml[:200] if raw_yaml else "<empty>",
            )
            # BUG-12: Retry with a stricter, shorter prompt
            if llm is not None:
                logger.info("Stage 09: Retrying with strict YAML-only prompt...")
                _retry_prompt = (
                    "Output ONLY valid YAML. No prose, no markdown fences, no explanation.\n"
                    f"Topic: {config.research.topic}\n"
                    "Required keys: baselines, proposed_methods, ablations, "
                    "datasets, metrics, objectives, risks, compute_budget.\n"
                    "Each key maps to a list of strings."
                )
                _retry_resp = _chat_with_prompt(
                    llm,
                    "You output ONLY valid YAML. Nothing else.",
                    _retry_prompt,
                    max_tokens=4096,
                )
                try:
                    _retry_parsed = yaml.safe_load(_retry_resp.content)
                    if isinstance(_retry_parsed, dict):
                        plan = _retry_parsed
                        logger.info("Stage 09: Strict YAML retry succeeded.")
                except yaml.YAMLError:
                    pass

    if (
        isinstance(plan, dict)
        and set(plan) == {"experiment_plan"}
        and isinstance(plan["experiment_plan"], dict)
    ):
        plan = dict(plan["experiment_plan"])

    # BUG-12: Fallback 4 — extract method/baseline names from Stage 8 hypotheses
    if plan is None:
        _hyp_text = _read_prior_artifact(run_dir, "hypotheses.md") or ""
        if _hyp_text:
            import re as _re_hyp
            # Extract method-like names from hypothesis text
            _method_candidates = _re_hyp.findall(
                r"(?:proposed|our|novel|new)\s+(?:method|approach|algorithm|framework|model)[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            _baseline_candidates = _re_hyp.findall(
                r"(?:baseline|compare|existing|standard|traditional)\s+(?:method|approach|model)?[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            if _method_candidates or _baseline_candidates:
                logger.info(
                    "Stage 09: Extracted names from hypotheses: methods=%s, baselines=%s",
                    _method_candidates[:3], _baseline_candidates[:3],
                )
                plan = {
                    "topic": config.research.topic,
                    "generated": _utcnow_iso(),
                    "objectives": ["Evaluate hypotheses with controlled experiments"],
                    "datasets": ["primary_dataset"],
                    "baselines": _baseline_candidates[:3] or ["baseline_1", "baseline_2"],
                    "proposed_methods": _method_candidates[:3] or ["proposed_method"],
                    "ablations": ["without_key_component", "simplified_version"],
                    "metrics": [config.experiment.metric_key, "secondary_metric"],
                    "risks": ["validity threats", "confounding variables"],
                    "compute_budget": {"max_gpu": 1, "max_hours": 4},
                }

    if plan is None:
        # BUG-12: Use domain-aware names instead of fully generic placeholders
        _topic_prefix = config.research.topic.split()[0] if config.research.topic else "method"
        logger.warning(
            "Stage 09: LLM failed to produce valid experiment plan YAML. "
            "Using topic-derived fallback."
        )
        plan = {
            "topic": config.research.topic,
            "generated": _utcnow_iso(),
            "objectives": ["Evaluate hypotheses with controlled experiments"],
            "datasets": ["primary_dataset", "secondary_dataset"],
            "baselines": [f"{_topic_prefix}_baseline_1", f"{_topic_prefix}_baseline_2"],
            "proposed_methods": [f"{_topic_prefix}_proposed", f"{_topic_prefix}_variant"],
            "ablations": ["without_key_component", "simplified_version"],
            "metrics": [config.experiment.metric_key, "secondary_metric"],
            "risks": ["validity threats", "confounding variables"],
            "compute_budget": {"max_gpu": 1, "max_hours": 4},
        }

    # Schema-deficit guard: when the LLM returned a parseable dict that
    # bypassed every fallback cascade (because plan was never None) but
    # lacks any actual experiment content, pause rather than silently
    # advancing a content-empty plan to code generation.  Use
    # _normalize_plan_field so the guard accepts every shape the rest of
    # this file already supports (str, dict, list[str], list[dict]).
    _required_any = ("baselines", "proposed_methods", "ablations")
    _normalized = {k: _normalize_plan_field(plan.get(k)) for k in _required_any}
    if not any(_normalized.values()):
        namespace.write_text_atomic(
            "plan_meta.json",
            json.dumps(
                {
                    "outcome": "model_response_schema_deficient",
                    "missing_required_keys": [
                        k for k in _required_any if not _normalized[k]
                    ],
                    "received_keys": sorted(plan.keys()),
                    "note": (
                        "Experiment plan parsed but lacked baselines, proposed_methods, "
                        "and ablations. Pipeline paused; refine the prompt or rerun stage."
                    ),
                },
                indent=2,
            ),
        )
        logger.warning(
            "Stage 9: model plan parsed but missing required content keys — pausing pipeline"
        )
        return StageResult(
            stage=Stage.EXPERIMENT_DESIGN,
            status=StageStatus.PAUSED,
            artifacts=("plan_meta.json",),
            error="Experiment plan missing baselines/proposed_methods/ablations",
            evidence_refs=("stage-09/plan_meta.json",),
            decision="schema_deficient",
        )
    # ── BA: BenchmarkAgent — intelligent dataset/baseline selection ──────
    _benchmark_plan = None
    _spec_diagnostics: list[dict[str, object]] = []
    # BUG-40: Skip BenchmarkAgent for non-ML domains — it has no relevant
    # benchmarks for physics/chemistry/mathematics/etc. and would inject
    # wrong datasets (e.g., CIFAR-10 for PDE topics).
    _ba_domain_profile = _domain_profile
    if _ba_domain_profile is None:
        try:
            from researchclaw.domains.detector import detect_domain as _detect_domain_adv
            _ba_domain_profile = _detect_domain_adv(
                topic=config.research.topic,
                hypotheses=hypotheses,
            )
        except Exception:  # noqa: BLE001
            logger.debug("BenchmarkAgent domain detection unavailable", exc_info=True)
    _ba_domain_id = (
        _ba_domain_profile.domain_id
        if _ba_domain_profile is not None
        else "generic"
    )
    _ba_domain_ok = (
        domain_capture_plan is None and _ba_domain_id.startswith("ml_")
    )
    if not _ba_domain_ok:
        logger.info(
            "BenchmarkAgent skipped: domain profile '%s' is not an ML profile (topic: %s)",
            _ba_domain_id, config.research.topic[:80],
        )
    if (
        _ba_domain_ok
        and config.experiment.benchmark_agent.enabled
        and config.experiment.mode in ("sandbox", "docker")
        and llm is not None
    ):
        try:
            from researchclaw.agents.benchmark_agent import BenchmarkOrchestrator
            from researchclaw.agents.benchmark_agent.orchestrator import (
                BenchmarkAgentConfig as _BACfg,
            )

            _ba_cfg_raw = config.experiment.benchmark_agent
            _ba_cfg = _BACfg(
                enabled=_ba_cfg_raw.enabled,
                enable_hf_search=_ba_cfg_raw.enable_hf_search,
                max_hf_results=_ba_cfg_raw.max_hf_results,
                enable_web_search=_ba_cfg_raw.enable_web_search,
                max_web_results=_ba_cfg_raw.max_web_results,
                web_search_min_local=_ba_cfg_raw.web_search_min_local,
                tier_limit=_ba_cfg_raw.tier_limit,
                min_benchmarks=_ba_cfg_raw.min_benchmarks,
                min_baselines=_ba_cfg_raw.min_baselines,
                prefer_cached=_ba_cfg_raw.prefer_cached,
                max_iterations=_ba_cfg_raw.max_iterations,
            )

            _hw = _load_hardware_profile(run_dir)
            with tempfile.TemporaryDirectory(prefix="researchclaw-stage09-ba-") as work:
                _ba = BenchmarkOrchestrator(
                    llm,
                    config=_ba_cfg,
                    gpu_memory_mb=(
                        _hw.get("gpu_memory_mb", 49000) if _hw else 49000
                    ),
                    time_budget_sec=config.experiment.time_budget_sec,
                    network_policy=(
                        config.experiment.docker.network_policy
                        if config.experiment.mode == "docker"
                        else "full"
                    ),
                    stage_dir=Path(work),
                )
                _benchmark_plan = _ba.orchestrate({
                    "topic": config.research.topic,
                    "hypothesis": hypotheses,
                    "experiment_plan": plan.get("objectives", "") if isinstance(plan, dict) else "",
                })

            # Inject BenchmarkAgent selections into experiment plan
            if (
                isinstance(plan, dict)
                and _benchmark_plan.selected_benchmarks
                and _benchmark_plan.validation_passed
            ):
                plan["datasets"] = [
                    b.get("name", "Unknown") for b in _benchmark_plan.selected_benchmarks
                ]
                # Normalize existing baselines — LLM may emit dict, list of
                # dicts, or list of strings.
                _baselines_from_plan = _plan_field_names(
                    _normalize_plan_field(plan.get("baselines", []))
                )
                plan["baselines"] = [
                    bl.get("name", "Unknown") for bl in _benchmark_plan.selected_baselines
                ] + _baselines_from_plan
                # Deduplicate baselines
                plan["baselines"] = list(dict.fromkeys(plan["baselines"]))
            if (
                _benchmark_plan.selected_benchmarks
                and not _benchmark_plan.validation_passed
            ):
                # ADJ-E9-01: fail-closed — an unvalidated BenchmarkAgent
                # product is rejected and the rejection is recorded
                # structurally for the execution-spec artifact.
                _spec_diagnostics.append(
                    {
                        "type": "benchmark_agent_product_rejected",
                        "validation_passed": False,
                        "selected_benchmarks": [
                            str(b.get("name", "Unknown"))
                            for b in _benchmark_plan.selected_benchmarks
                        ],
                    }
                )

            logger.info(
                "BenchmarkAgent: %d benchmarks, %d baselines selected (%d LLM calls, %.1fs)",
                len(_benchmark_plan.selected_benchmarks),
                len(_benchmark_plan.selected_baselines),
                _benchmark_plan.total_llm_calls,
                _benchmark_plan.elapsed_sec,
            )
        except Exception as _ba_exc:
            logger.warning("BenchmarkAgent failed (non-fatal): %s", _ba_exc)

    # Save benchmark plan for code_generation stage
    if _benchmark_plan is not None:
        try:
            namespace.write_text_atomic(
                "benchmark_plan.json",
                json.dumps(_benchmark_plan.to_dict(), indent=2, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001
            pass

    if domain_capture_plan is None:
        plan.setdefault("topic", config.research.topic)

    # BUG-R41-09: Enforce condition count limit based on time budget.
    # Too many conditions (30+) guarantee timeouts and wasted compute.
    _time_budget = getattr(
        getattr(config, "experiment", None), "time_budget_sec", 3600
    )
    _max_conditions = 8  # default for budgets ≤ 3600s
    if _time_budget > 3600:
        _max_conditions = 12
    if _time_budget > 7200:
        _max_conditions = 20

    _baselines = _normalize_plan_field(plan.get("baselines", []))
    _proposed = _normalize_plan_field(plan.get("proposed_methods", []))
    _ablations = _normalize_plan_field(plan.get("ablations", []))
    _total = len(_baselines) + len(_proposed) + len(_ablations)

    if _total > _max_conditions:
        logger.warning(
            "Stage 9: Plan has %d conditions (limit %d for %ds budget). "
            "Trimming to fit.",
            _total, _max_conditions, _time_budget,
        )
        # Keep all proposed methods (up to max), trim baselines and ablations
        _proposed_count = min(len(_proposed), max(1, _max_conditions - 4))
        _remaining = max(0, _max_conditions - _proposed_count)
        _baseline_budget = max(1, _remaining // 2)
        _ablation_budget = max(0, _remaining - _baseline_budget)
        if len(_proposed) > _proposed_count:
            plan["proposed_methods"] = _proposed[:_proposed_count]
            logger.info(
                "Stage 9: Trimmed proposed methods %d → %d",
                len(_proposed), _proposed_count,
            )

        if len(_baselines) > _baseline_budget:
            plan["baselines"] = _baselines[:_baseline_budget]
            logger.info(
                "Stage 9: Trimmed baselines %d → %d",
                len(_baselines), _baseline_budget,
            )
        if len(_ablations) > _ablation_budget:
            plan["ablations"] = _ablations[:_ablation_budget]
            logger.info(
                "Stage 9: Trimmed ablations %d → %d",
                len(_ablations), _ablation_budget,
            )

    # --- HITL: Read human guidance if available ---
    try:
        guidance = namespace.read_bytes("hitl_guidance.md").decode("utf-8").strip()
    except FileNotFoundError:
        guidance = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractValidationError(f"HITL guidance is unsafe: {exc}") from exc
    if guidance:
        try:
            if (
                guidance
                and domain_capture_plan is None
                and llm is not None
                and isinstance(plan, dict)
            ):
                logger.info("Applying HITL guidance to experiment design")
                resp = llm.chat(
                    [{"role": "user", "content": (
                        f"The human researcher provided this guidance for "
                        f"the experiment design:\n\n{guidance}\n\n"
                        f"Current experiment plan:\n"
                        f"```yaml\n{yaml.dump(plan, default_flow_style=False)}\n```\n\n"
                        f"Update the YAML plan to incorporate the guidance. "
                        f"Return ONLY the updated YAML."
                    )}],
                    max_tokens=4096,
                )
                updated = _extract_yaml_block(resp.content)
                try:
                    parsed_update = yaml.safe_load(updated)
                    if isinstance(parsed_update, dict):
                        plan = parsed_update
                except yaml.YAMLError:
                    pass
        except Exception:  # noqa: BLE001
            logger.debug("HITL guidance application failed (non-blocking)")

    # --- HITL: Baseline Navigator data persistence ---
    if domain_capture_plan is None:
        try:
            from researchclaw.hitl.workshops.baseline import (
                BaselineCandidate,
                BaselineNavigator,
            )

            nav = BaselineNavigator(run_dir, llm_client=llm)
            if isinstance(plan, dict):
                baselines = plan.get("baselines", [])
                if isinstance(baselines, list):
                    for b in baselines:
                        if isinstance(b, dict):
                            nav.baselines.append(BaselineCandidate(
                                name=b.get("name", str(b)),
                                description=b.get("description", ""),
                            ))
                        elif isinstance(b, str):
                            nav.baselines.append(BaselineCandidate(name=b))
                metrics = plan.get("metrics", [])
                if isinstance(metrics, list):
                    nav.metrics = [str(m) for m in metrics]
            nav.save()
        except Exception:
            pass

    namespace.write_text_atomic(
        "exp_plan.yaml",
        yaml.dump(plan, default_flow_style=False, allow_unicode=True),
    )
    try:
        contract = derive_contract(
            config,
            plan,
            stage_dir=stage_dir,
            namespace=namespace,
            authority_selection=(
                domain_capture_plan.selection
                if domain_capture_plan is not None
                else selected_authority
            ),
        )
        contract_sha = dump_contract(
            contract,
            stage_dir / "experiment_contract.yaml",
            namespace=namespace,
        )
        replayed_contract = load_contract_bytes(
            namespace.read_bytes("experiment_contract.yaml"),
            authority_selection=selected_authority,
        )
        if replayed_contract != contract:
            raise ContractValidationError("contract pre-sidecar replay mismatch")
        _replay_metric_authority_selection(
            selection=selected_authority,
            run_dir=run_dir,
            stored_identity=contract.metric_authority,
            metric_units=contract.metric_units,
            metric_display_labels=contract.metric_display_labels,
            evaluator_authority=contract.evaluator_authority,
            namespace=namespace,
        )
        namespace.write_text_atomic("experiment_contract.sha256", contract_sha + "\n")
        if contract.metric_authority.get("domain_id") == "generic_sandbox":
            namespace.write_bytes_atomic(
                "execution_spec.json", execution_spec_bytes(contract)
            )
            namespace.write_bytes_atomic(
                "execution_spec_diagnostics.json",
                execution_spec_diagnostics_bytes(_spec_diagnostics),
            )
    except (ContractValidationError, MetricAuthorityError) as exc:
        error = f"Experiment contract invalid: {exc}"
        logger.error("Stage 9: %s", error)
        return StageResult(
            stage=Stage.EXPERIMENT_DESIGN,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=error,
        )
    authority_artifacts = (
        "domain_selector_policy.json",
        "domain_profile.json",
        "metric_authority_index.json",
        "metric_authority.json",
    )
    if contract.schema_version == 3:
        authority_artifacts += (
            "domain_evaluator_package_manifest.json",
            "domain_evaluator_execution_policy.json",
        )
    return StageResult(
        stage=Stage.EXPERIMENT_DESIGN,
        status=StageStatus.DONE,
        artifacts=(
            "exp_plan.yaml",
            "experiment_contract.yaml",
            "experiment_contract.sha256",
            *(
                ("execution_spec.json", "execution_spec_diagnostics.json")
                if contract.metric_authority.get("domain_id") == "generic_sandbox"
                else ()
            ),
            *authority_artifacts,
        ),
        evidence_refs=(
            "stage-09/exp_plan.yaml",
            "stage-09/experiment_contract.yaml",
            *(
                ("stage-09/execution_spec.json",)
                if contract.metric_authority.get("domain_id") == "generic_sandbox"
                else ()
            ),
        ),
    )


def _validate_stage9_publication(
    namespace: BoundOutputNamespace,
    run_dir: Path,
    config: RCConfig,
) -> None:
    """Replay the Stage 9 commit point through the held directory identity."""

    try:
        plan = yaml.safe_load(namespace.read_bytes("exp_plan.yaml").decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractValidationError(f"experiment plan replay failed: {exc}") from exc
    if not isinstance(plan, dict):
        raise ContractValidationError("experiment plan replay root must be an object")

    contract_path = namespace.stage_dir / "experiment_contract.yaml"
    replayed_selection = select_metric_authority(
        config.research.topic,
        config.experiment.mode,
        allow_domain_evaluator_capture=config_allows_domain_evaluator_capture(config),
    )
    contract = load_contract_bytes(
        namespace.read_bytes("experiment_contract.yaml"),
        authority_selection=replayed_selection,
    )
    digest = contract_sha256(contract_path, namespace=namespace)
    try:
        sidecar = namespace.read_bytes("experiment_contract.sha256").decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractValidationError(f"contract sidecar replay failed: {exc}") from exc
    if sidecar != digest + "\n":
        raise ContractValidationError("contract sidecar does not bind contract bytes")

    _replay_metric_authority_selection(
        selection=replayed_selection,
        run_dir=run_dir,
        stored_identity=contract.metric_authority,
        metric_units=contract.metric_units,
        metric_display_labels=contract.metric_display_labels,
        evaluator_authority=contract.evaluator_authority,
        namespace=namespace,
    )
    if contract.schema_version == 3:
        expected_plan = derive_domain_evaluator_experiment_plan(
            build_domain_evaluator_capture_plan(replayed_selection)
        )
        if plan != expected_plan:
            raise ContractValidationError(
                "fixed domain evaluator experiment plan mismatch"
            )
    elif contract.metric_authority.get("domain_id") == "generic_sandbox":
        expected = derive_execution_spec(contract)
        try:
            raw_spec = namespace.read_bytes("execution_spec.json")
            published = json.loads(raw_spec)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractValidationError(
                f"execution spec semantic replay failed: {exc}"
            ) from exc
        if published != expected:
            raise ContractValidationError("execution spec semantic replay mismatch")
        if raw_spec != execution_spec_bytes(contract):
            raise ContractValidationError("execution spec byte replay mismatch")
    namespace.assert_canonical()
