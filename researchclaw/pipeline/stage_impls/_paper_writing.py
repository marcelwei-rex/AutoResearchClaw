"""Stages 16-17: Paper outline and paper draft generation."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.literature.citation_policy import (
    CitationPolicyContractError,
    build_effective_citation_policy,
    load_effective_citation_policy,
)
from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    attribute_citation_keys_to_top_level_headings,
    build_heading_citation_writer_instructions_from_authority,
    build_citation_closure_report,
    build_citation_plan,
    # Stable monkeypatch seam for legacy executor fixtures.
    build_citation_writer_instruction,
    filter_strict_citation_markers,
    load_final_citation_plan,
    parse_strict_citation_occurrences,
    strict_citation_keys,
    strict_sentence_spans,
    strip_strict_citation_markers,
    validate_citation_closure_report,
    validate_citation_plan,
)
from researchclaw.literature.evidence_cards import canonical_json_text, load_validated_cards
from researchclaw.literature.experiment_fact_closure import (
    ExperimentFactClosureError,
    build_experiment_fact_closure_report,
    canonical_experiment_fact_json_text,
    remove_unsupported_experiment_fact_blocks,
    validate_experiment_fact_closure_report,
)
from researchclaw.pipeline._domain import (
    _detect_domain,
    _is_ml_domain,
    _prompt_bank_domain_from_config,
)
from researchclaw.pipeline._helpers import (
    StageResult,
    _build_context_preamble,
    _chat_with_prompt,
    _default_paper_outline,
    _extract_paper_title,
    _generate_framework_diagram_prompt,
    _generate_neurips_checklist,
    _safe_json_loads,
    _topic_constraint_block,
    _utcnow_iso,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    merge_manuscript,
    parse_manuscript,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    canonical_decimal,
    load_canonical_experiment_evidence,
)
from researchclaw.pipeline.canonical_fact_sheet import (
    CFSIntegrityError,
    build_canonical_fact_sheet,
    build_heading_grounding_contexts,
    fact_sheet_view_for_heading,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.stage15_decision_projection import (
    DECISION_POLICY_VERSION,
    DECISION_PROJECTION_SCHEMA_VERSION,
    Stage15DecisionProjectionError,
    build_stage15_decision_projection,
    uses_fixed_domain_decision_policy,
)
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)

_SECTION_OUTPUT_CONTRACT = """

SECTION OUTPUT CONTRACT (OVERRIDES ANY CONFLICTING FORMAT INSTRUCTIONS):
- Output only the sections requested in this call. Never repeat, summarize, or
  continue any section shown as prior context.
- Use exactly one `##` heading for each requested major section, with the exact
  requested section name. Do not renumber or rename those major headings.
- Optional `###` subsection titles must be unique within their `##` parent.
- Do not emit a title/preamble outside the requested `##` sections.
"""

_SECTION_REPAIR_SYSTEM = """You repair exactly one bounded manuscript part.
Output only that part as Markdown. Never output a complete paper.
The caller will reject every response whose CommonMark heading sequence is not exact."""

_SECTION_GENERATION_SCHEMA_VERSION = 1
_SECTION_OWNERSHIP_CONTRACT_VERSION = 1
_EXPERIMENT_FACT_INVALID_SCHEMA_VERSION = 1
_OUTLINE_BINDING_SCHEMA_VERSION = 1
_STAGE15_STANDARD_DECISION_FIELDS = frozenset(
    {
        "decision",
        "raw_text_excerpt",
        "quality_warnings",
        "generated",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "decision_path",
        "decision_sha256",
    }
)
_STAGE15_DOMAIN_DECISION_FIELDS = _STAGE15_STANDARD_DECISION_FIELDS | frozenset(
    {
        "decision_policy_version",
        "decision_projection_schema_version",
        "decision_projection_sha256",
    }
)
_STAGE15_AGENT_DECISION_FIELDS = frozenset(
    {
        "decision",
        "verdict",
        "retry_count",
        "rerun_triggered",
        "max_retries",
        "generated",
        "source",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "decision_path",
        "decision_sha256",
    }
)
_RESERVED_MAJOR_SECTION_NAMES = frozenset(
    {
        "abstract",
        "introduction",
        "related work",
        "method",
        "experiments",
        "results",
        "discussion",
        "limitations",
        "conclusion",
        "conclusions",
        "model / theoretical framework",
        "phenomenology / computational setup",
    }
)


def _read_regular_utf8(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or not a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc


def _strict_json_object(text: str, label: str) -> dict[str, Any]:
    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"duplicate {label} key: {key}")
            value[key] = child
        return value

    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_bound_stage15_decision(
    run_dir: Path,
    evidence: CanonicalExperimentEvidence,
) -> tuple[str, str]:
    decision_path = run_dir / "stage-15/decision.md"
    binding_path = run_dir / "stage-15/decision_structured.json"
    decision_text = _read_regular_utf8(decision_path, "Stage 15 decision")
    binding = _strict_json_object(
        _read_regular_utf8(binding_path, "Stage 15 decision binding"),
        "Stage 15 decision binding",
    )
    binding_fields = frozenset(binding)
    fixed_domain_policy = uses_fixed_domain_decision_policy(evidence)
    expected_standard_fields = (
        _STAGE15_DOMAIN_DECISION_FIELDS
        if fixed_domain_policy
        else _STAGE15_STANDARD_DECISION_FIELDS
    )
    if binding_fields == expected_standard_fields:
        if (
            not isinstance(binding["raw_text_excerpt"], str)
            or binding["raw_text_excerpt"] != decision_text[:500]
            or not isinstance(binding["quality_warnings"], list)
            or not all(isinstance(item, str) for item in binding["quality_warnings"])
            or not isinstance(binding["generated"], str)
            or not binding["generated"].strip()
        ):
            raise ValueError("Stage 15 standard decision binding is invalid")
        if fixed_domain_policy:
            try:
                projection = build_stage15_decision_projection(evidence)
            except Stage15DecisionProjectionError as exc:
                raise ValueError(f"Stage 15 decision projection is invalid: {exc}") from exc
            expected_projection = {
                "decision_policy_version": DECISION_POLICY_VERSION,
                "decision_projection_schema_version": DECISION_PROJECTION_SCHEMA_VERSION,
                "decision_projection_sha256": projection.sha256,
            }
            if type(binding["decision_projection_schema_version"]) is not int:
                raise ValueError(
                    "Stage 15 decision projection schema version must be an integer"
                )
            for field, value in expected_projection.items():
                if binding.get(field) != value:
                    raise ValueError(
                        f"Stage 15 decision projection binding mismatch: {field}"
                    )
    elif binding_fields == _STAGE15_AGENT_DECISION_FIELDS:
        raise ValueError(
            "Stage 15 agent requirements decisions are not authorized for "
            "Stage 16 in canonical evidence migration v1"
        )
    else:
        raise ValueError("Stage 15 decision binding fields mismatch")
    expected = {
        "decision_path": "stage-15/decision.md",
        "decision_sha256": _sha256_text(decision_text),
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise ValueError(f"Stage 15 decision binding mismatch: {field}")
    from researchclaw.pipeline.stage_impls._analysis import _parse_decision

    parsed_decision = _parse_decision(decision_text)
    if binding.get("decision") != "proceed" or parsed_decision != "proceed":
        raise ValueError("Stage 15 decision does not authorize paper writing")
    return decision_text, expected["decision_sha256"]


def _write_outline_binding(
    stage_dir: Path,
    *,
    outline: str,
    decision_sha256: str,
    evidence: CanonicalExperimentEvidence,
) -> str:
    payload = {
        "schema_version": _OUTLINE_BINDING_SCHEMA_VERSION,
        "outline_path": "stage-16/outline.md",
        "outline_sha256": _sha256_text(outline),
        "decision_path": "stage-15/decision.md",
        "decision_sha256": decision_sha256,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
    }
    text = canonical_json_text(payload)
    (stage_dir / "outline_binding.json").write_text(text, encoding="utf-8")
    return text


def _load_bound_stage16_outline(
    run_dir: Path,
    evidence: CanonicalExperimentEvidence,
) -> str:
    outline_path = run_dir / "stage-16/outline.md"
    binding_path = run_dir / "stage-16/outline_binding.json"
    outline = _read_regular_utf8(outline_path, "Stage 16 outline")
    binding = _strict_json_object(
        _read_regular_utf8(binding_path, "Stage 16 outline binding"),
        "Stage 16 outline binding",
    )
    expected_fields = {
        "schema_version",
        "outline_path",
        "outline_sha256",
        "decision_path",
        "decision_sha256",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
    }
    if set(binding) != expected_fields:
        raise ValueError("Stage 16 outline binding fields mismatch")
    expected = {
        "schema_version": _OUTLINE_BINDING_SCHEMA_VERSION,
        "outline_path": "stage-16/outline.md",
        "outline_sha256": _sha256_text(outline),
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise ValueError(f"Stage 16 outline binding mismatch: {field}")
    if binding.get("decision_path") != "stage-15/decision.md":
        raise ValueError("Stage 16 outline binding mismatch: decision_path")
    decision_sha256 = binding.get("decision_sha256")
    if not isinstance(decision_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", decision_sha256
    ) is None:
        raise ValueError("Stage 16 outline binding mismatch: decision_sha256")
    _decision, current_decision_sha256 = _load_bound_stage15_decision(
        run_dir, evidence
    )
    if decision_sha256 != current_decision_sha256:
        raise ValueError("Stage 16 outline binding mismatch: decision_sha256")
    return outline


class PaperSectionContractError(ValueError):
    """Raised when a Stage 17 LLM part violates section ownership."""

    def __init__(self, part_name: str, violations: tuple[str, ...], text: str):
        self.part_name = part_name
        self.violations = violations
        self.text = text
        super().__init__(f"{part_name} section contract failed: {', '.join(violations)}")


def _normalize_major_heading(title: str) -> str:
    return " ".join(title.strip().casefold().split())


def _validate_paper_part_sections(
    text: str,
    *,
    expected_major_sections: tuple[str, ...],
    title_slot: bool,
) -> tuple[str, ...]:
    """Validate one LLM part with the canonical CommonMark parser."""
    try:
        document = parse_manuscript(text, strict=False)
    except ManuscriptStructureError as exc:
        return tuple(sorted({issue.code for issue in exc.issues}))

    violations = {issue.code for issue in document.structure_issues}
    if document.preamble.strip():
        violations.add("section_part_preamble_forbidden")
    if any(section.level not in {2, 3} for section in document.sections):
        violations.add("section_part_heading_level_invalid")

    major = tuple(
        _normalize_major_heading(section.title)
        for section in document.sections
        if section.level == 2
    )
    expected = tuple(_normalize_major_heading(title) for title in expected_major_sections)
    if title_slot:
        if len(major) != len(expected) + 1 or major[1:] != expected:
            violations.add("section_part_major_sequence_mismatch")
        elif not major[0] or major[0] in _RESERVED_MAJOR_SECTION_NAMES:
            violations.add("section_part_title_invalid")
    elif major != expected:
        violations.add("section_part_major_sequence_mismatch")
    return tuple(sorted(violations))


def _observed_major_sections(text: str) -> tuple[str, ...]:
    """Return diagnostic h2 titles without creating a second validator."""
    try:
        document = parse_manuscript(text, strict=False)
    except ManuscriptStructureError:
        return ()
    return tuple(section.title for section in document.sections if section.level == 2)


def _persist_section_generation_report(
    stage_dir: Path | None, entries: list[dict[str, Any]]
) -> None:
    if stage_dir is None:
        return
    report = {
        "schema_version": _SECTION_GENERATION_SCHEMA_VERSION,
        "ownership_contract_version": _SECTION_OWNERSHIP_CONTRACT_VERSION,
        "_diagnostic": True,
        "parts": entries,
    }
    (stage_dir / "section_generation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_experiment_fact_invalid(
    stage_dir: Path, report: dict[str, Any]
) -> None:
    """Persist a diagnostic-only projection of a failed fact closure."""
    payload = {
        "schema_version": _EXPERIMENT_FACT_INVALID_SCHEMA_VERSION,
        "paper_sha256": report["paper_sha256"],
        "experiment_contract_path": report["experiment_contract_path"],
        "experiment_contract_sha256": report["experiment_contract_sha256"],
        "canonical_experiment_evidence_path": report[
            "canonical_experiment_evidence_path"
        ],
        "canonical_experiment_evidence_sha256": report[
            "canonical_experiment_evidence_sha256"
        ],
        "dataset_origin": report["dataset_origin"],
        "grounded_numeric_values": report["grounded_numeric_values"],
        "manuscript_numeric_values": report["manuscript_numeric_values"],
        "unknown_numeric_values": report["unknown_numeric_values"],
        "dataset_claim_violations": report["dataset_claim_violations"],
    }
    if report.get("schema_version") == 3:
        payload.update(
            canonical_fact_sheet_sha256=report["canonical_fact_sheet_sha256"],
            fact_sheet_schema_version=report["fact_sheet_schema_version"],
            structured_fact_violations=report["structured_fact_violations"],
            citation_bound_numeric_claims=report["citation_bound_numeric_claims"],
        )
    (stage_dir / "experiment_fact_closure_invalid.json").write_text(
        canonical_experiment_fact_json_text(payload), encoding="utf-8"
    )


def _fact_repair_added_numeric_authority(
    before: dict[str, Any], after: dict[str, Any]
) -> bool:
    """Return True unless the deterministic repair only removes numeric occurrences."""
    before_values = list(before["manuscript_numeric_values"])
    for value in after["manuscript_numeric_values"]:
        if value not in before_values:
            return True
        before_values.remove(value)
    return False


def _validate_or_regenerate_paper_part(
    *,
    llm: LLMClient,
    initial_text: str,
    part_name: str,
    expected_major_sections: tuple[str, ...],
    title_slot: bool,
    citation_repair_context: str,
    allowed_citation_keys: frozenset[str],
    max_tokens: int,
    report_entries: list[dict[str, Any]],
    stage_dir: Path | None,
    grounding_context: str = "",
) -> str:
    text = initial_text.strip()
    attempts: list[dict[str, Any]] = []
    for semantic_attempt in (1, 2):
        violations = _validate_paper_part_sections(
            text,
            expected_major_sections=expected_major_sections,
            title_slot=title_slot,
        )
        attempts.append(
            {
                "attempt": semantic_attempt,
                "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "valid": not violations,
                "violations": list(violations),
                "observed_major_sections": list(_observed_major_sections(text)),
            }
        )
        if not violations:
            report_entries.append(
                {
                    "part": part_name,
                    "title_slot": title_slot,
                    "expected_major_sections": list(expected_major_sections),
                    "attempts": attempts,
                }
            )
            _persist_section_generation_report(stage_dir, report_entries)
            return text
        if semantic_attempt == 2:
            report_entries.append(
                {
                    "part": part_name,
                    "title_slot": title_slot,
                    "expected_major_sections": list(expected_major_sections),
                    "attempts": attempts,
                }
            )
            _persist_section_generation_report(stage_dir, report_entries)
            raise PaperSectionContractError(part_name, violations, text)

        expected_lines = []
        if title_slot:
            expected_lines.append(
                "1. First h2: the actual concise paper title (not the literal word Title)"
            )
        start = len(expected_lines) + 1
        expected_lines.extend(
            f"{ordinal}. ## {heading}"
            for ordinal, heading in enumerate(expected_major_sections, start=start)
        )
        safe_previous_text = _filter_citation_markers(text, allowed_citation_keys)
        repair_prompt = f"""Repair one bounded manuscript part.

Part name: {part_name}
Exact h2 sequence:
{chr(10).join(expected_lines)}

Previous deterministic violations:
{chr(10).join(f'- {violation}' for violation in violations)}

Rules:
- Output only the h2 sequence above, in exactly that order.
- Do not output any other h2 section or a complete paper.
- Preserve only citation markers authorized for this part.
- Remove every unauthorized citation marker. Do not invent or replace citations.
- Optional h3 subsections may remain under their existing owned h2 parent.
- Do not add a preamble, explanation, code fence, or References section.

Validated citation claims assigned to this part:
{citation_repair_context}

Canonical fact authority assigned to this part:
{grounding_context or 'None (legacy generic path)'}

<previous_invalid_response>
{safe_previous_text}
</previous_invalid_response>
"""
        try:
            regenerated = _chat_with_prompt(
                llm,
                _SECTION_REPAIR_SYSTEM
                + (
                    "\n\nCANONICAL FACT AUTHORITY:\n" + grounding_context
                    if grounding_context
                    else ""
                ),
                repair_prompt,
                max_tokens=max_tokens,
                retries=1,
            )
            text = regenerated.content.strip()
        except Exception as exc:  # noqa: BLE001
            text = ""
            attempts.append(
                {
                    "attempt": 2,
                    "response_sha256": hashlib.sha256(b"").hexdigest(),
                    "valid": False,
                    "violations": [f"section_part_transport_error:{type(exc).__name__}"],
                    "observed_major_sections": [],
                }
            )
            report_entries.append(
                {
                    "part": part_name,
                    "title_slot": title_slot,
                    "expected_major_sections": list(expected_major_sections),
                    "attempts": attempts,
                }
            )
            _persist_section_generation_report(stage_dir, report_entries)
            raise PaperSectionContractError(
                part_name,
                ("section_part_transport_error",),
                text,
            ) from exc


def _raise_initial_part_transport_failure(
    *,
    part_name: str,
    expected_major_sections: tuple[str, ...],
    title_slot: bool,
    exc: Exception,
    report_entries: list[dict[str, Any]],
    stage_dir: Path | None,
) -> None:
    report_entries.append(
        {
            "part": part_name,
            "title_slot": title_slot,
            "expected_major_sections": list(expected_major_sections),
            "attempts": [
                {
                    "attempt": 1,
                    "response_sha256": hashlib.sha256(b"").hexdigest(),
                    "valid": False,
                    "violations": [
                        f"section_part_transport_error:{type(exc).__name__}"
                    ],
                    "observed_major_sections": [],
                }
            ],
        }
    )
    _persist_section_generation_report(stage_dir, report_entries)
    raise PaperSectionContractError(
        part_name, ("section_part_transport_error",), ""
    ) from exc


def _topic_is_literature_first(config: RCConfig) -> bool:
    """Return True when the topic is a survey/review or the project uses docs-first mode.

    Literature-first topics produce papers grounded in existing work rather
    than novel experiments, so the "all simulated" and "no real metrics"
    hard blocks should be bypassed.
    """
    topic_lower = config.research.topic.lower()
    if any(kw in topic_lower for kw in ("survey", "review", "meta-analysis", "literature review")):
        return True
    project_mode = getattr(config.research, "project_mode", None)
    if isinstance(project_mode, str) and project_mode.lower() == "docs-first":
        return True
    return False


def _execute_paper_outline(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    policy_path = stage_dir / "citation_policy_effective.json"
    preliminary_plan_path = stage_dir / "citation_plan.preliminary.json"
    final_plan_path = stage_dir / "citation_plan.json"
    outline_path = stage_dir / "outline.md"
    outline_binding_path = stage_dir / "outline_binding.json"
    try:
        policy_path.unlink(missing_ok=True)
        preliminary_plan_path.unlink(missing_ok=True)
        final_plan_path.unlink(missing_ok=True)
        outline_path.unlink(missing_ok=True)
        outline_binding_path.unlink(missing_ok=True)
        evidence = load_canonical_experiment_evidence(run_dir)
        decision, decision_sha256 = _load_bound_stage15_decision(run_dir, evidence)
        effective_policy = build_effective_citation_policy(run_dir, config)
        policy_path.write_text(
            canonical_json_text(effective_policy), encoding="utf-8"
        )
    except (
        CanonicalExperimentEvidenceError,
        CitationPolicyContractError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.PAPER_OUTLINE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Citation policy could not be closed: {exc}",
            decision="retry",
        )
    analysis = evidence.analysis_text
    preamble = _build_context_preamble(
        config,
        run_dir,
        canonical_evidence=evidence,
        bound_decision=decision,
        include_analysis=True,
        include_decision=True,
        include_experiment_data=True,
    )

    # WS-5.2: Read iteration feedback if available (multi-round iteration)
    feedback = ""
    iter_ctx_path = run_dir / "iteration_context.json"
    if iter_ctx_path.exists():
        try:
            ctx = json.loads(iter_ctx_path.read_text(encoding="utf-8"))
            iteration = ctx.get("iteration", 1)
            prev_score = ctx.get("quality_score")
            reviews_excerpt = ctx.get("reviews_excerpt", "")
            if iteration > 1 and reviews_excerpt:
                feedback = (
                    f"\n\n## Iteration {iteration} Feedback\n"
                    f"Previous quality score: {prev_score}/10\n"
                    f"Reviewer feedback to address:\n{reviews_excerpt[:2000]}\n"
                    f"\nYou MUST address these reviewer concerns in this revision.\n"
                )
        except (json.JSONDecodeError, KeyError):
            pass

    # Venue guidance is now carried natively by the active prompt bank
    # (ML bank -> NeurIPS/ICML, HEP bank -> JHEP/PRD). No adapter overlay.
    _outline_venue_guidance = ""

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-20: Pass academic style guide block for outline stage
        try:
            _asg = _pm.block("academic_style_guide")
        except (KeyError, Exception):
            _asg = ""
        sp = _pm.for_stage(
            "paper_outline",
            evolution_overlay="",
            preamble=preamble,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            feedback=feedback,
            analysis=analysis,
            decision=decision,
            academic_style_guide=_asg,
            venue_guidance=_outline_venue_guidance,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        outline = resp.content
        # Reasoning models may consume all tokens on CoT — retry with more
        if not outline.strip() and sp.max_tokens:
            logger.warning("Empty outline from LLM — retrying with 2x tokens")
            resp = _chat_with_prompt(
                llm,
                sp.system,
                sp.user,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens * 2,
            )
            outline = resp.content
        if not outline.strip():
            logger.warning("LLM returned empty outline — using default")
            outline = _default_paper_outline(config.research.topic)
    else:
        outline = _default_paper_outline(config.research.topic)
    try:
        preliminary = build_citation_plan(
            run_dir, config, plan_status="preliminary"
        )
        preliminary_text = canonical_json_text(preliminary)
        preliminary_plan_path.write_text(preliminary_text, encoding="utf-8")
        validate_citation_plan(
            run_dir, config, preliminary_text, plan_status="preliminary"
        )
        final_plan = build_citation_plan(run_dir, config, plan_status="final")
        final_plan_text = canonical_json_text(final_plan)
        final_plan_path.write_text(final_plan_text, encoding="utf-8")
        validate_citation_plan(
            run_dir, config, final_plan_text, plan_status="final"
        )
    except (CitationPlanContractError, OSError, UnicodeDecodeError) as exc:
        preliminary_plan_path.unlink(missing_ok=True)
        final_plan_path.unlink(missing_ok=True)
        outline_path.unlink(missing_ok=True)
        outline_binding_path.unlink(missing_ok=True)
        return StageResult(
            stage=Stage.PAPER_OUTLINE,
            status=StageStatus.FAILED,
            artifacts=("citation_policy_effective.json",),
            error=f"Citation plan could not be closed: {exc}",
            decision="retry",
            evidence_refs=("stage-16/citation_policy_effective.json",),
        )
    try:
        outline_path.write_text(outline, encoding="utf-8")
        _write_outline_binding(
            stage_dir,
            outline=outline,
            decision_sha256=decision_sha256,
            evidence=evidence,
        )
    except OSError as exc:
        outline_path.unlink(missing_ok=True)
        outline_binding_path.unlink(missing_ok=True)
        return StageResult(
            stage=Stage.PAPER_OUTLINE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Outline binding could not be published: {exc}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.PAPER_OUTLINE,
        status=StageStatus.DONE,
        artifacts=(
            "outline.md",
            "outline_binding.json",
            "citation_policy_effective.json",
            "citation_plan.preliminary.json",
            "citation_plan.json",
        ),
        evidence_refs=(
            "stage-16/outline.md",
            "stage-16/outline_binding.json",
            "stage-16/citation_policy_effective.json",
            "stage-16/citation_plan.preliminary.json",
            "stage-16/citation_plan.json",
        ),
    )


def _iter_authority_numbers(prefix: str, value: Any) -> list[tuple[str, Decimal]]:
    entries: list[tuple[str, Decimal]] = []
    if isinstance(value, bool):
        return entries
    if isinstance(value, (int, float, Decimal)):
        entries.append((prefix, Decimal(str(value))))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            entries.extend(_iter_authority_numbers(child_prefix, child))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            entries.extend(_iter_authority_numbers(f"{prefix}[{index}]", child))
    return entries


def _plain_evidence_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain_evidence_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_evidence_value(child) for child in value]
    return value


def _authority_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    return None


def _authority_number_text(value: Any) -> str:
    numeric = _authority_decimal(value)
    return canonical_decimal(numeric) if numeric is not None else str(value)


def _collect_raw_experiment_metrics(
    evidence: CanonicalExperimentEvidence | Path,
) -> tuple[str, bool]:
    """Render only accessor-selected observations for the paper writer."""
    if isinstance(evidence, Path):
        evidence = load_canonical_experiment_evidence(evidence)
    entries = _iter_authority_numbers("metric_observations", evidence.metric_observations)
    if not entries:
        return "", False
    lines = [f"  {key}: {value}" for key, value in entries[:200]]
    return (
        "\n\nCANONICAL EXPERIMENT DATA (use ONLY these selected observations):\n"
        "```\n" + "\n".join(lines) + "\n```\n"
        f"Evidence manifest: {evidence.manifest_path} ({evidence.manifest_sha256}).\n"
        "Do not invent, interpolate, or source values from diagnostic workspaces.\n"
    ), True


def _collect_grounded_metric_whitelist(
    evidence: CanonicalExperimentEvidence | Path,
) -> str:
    """Build the writer allowlist from the selected result set only."""
    if isinstance(evidence, Path):
        evidence = load_canonical_experiment_evidence(evidence)
    entries: list[tuple[str, str, Decimal]] = []
    seen: set[tuple[str, str]] = set()

    def _add(source: str, key: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return
        decimal_value = Decimal(str(value))
        item = (key, str(decimal_value))
        if item in seen:
            return
        seen.add(item)
        entries.append((source, key, decimal_value))

    def _walk_metrics(source: str, prefix: str, obj: Any) -> None:
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                _walk_metrics(source, next_prefix, value)
        elif isinstance(obj, (tuple, list)):
            for idx, value in enumerate(obj[:20]):
                _walk_metrics(source, f"{prefix}[{idx}]", value)
        else:
            _add(source, prefix, obj)

    source = evidence.selected_result_manifest_path
    _walk_metrics(source, "metric_observations", evidence.metric_observations)
    _walk_metrics(source, "structured_results", evidence.structured_results)

    if not entries:
        return ""

    lines = [
        "\n\n## GROUNDED METRIC VALUE WHITELIST (MANDATORY)",
        "The paper may report ONLY the numeric metric values listed below.",
        "Do NOT introduce any other decimal metric values in the Abstract, Experiments, Results, Discussion, or Conclusion.",
        "Do NOT convert these values to percentages unless that percentage value is explicitly listed here.",
        "If a desired number is not listed, write an em dash (---) or omit the numeric claim.",
    ]
    for source, key, value in entries[:160]:
        literal = str(value)
        lines.append(f"- {source} :: {key} = {literal}")
    return "\n".join(lines) + "\n"


def _compact_part_citation_context(
    claims: tuple[dict[str, str], ...],
    expected_major_sections: tuple[str, ...],
) -> str:
    """Render validated plan claims assigned to one bounded writer part."""
    expected = set(expected_major_sections)
    lines: list[str] = []
    for claim in claims:
        if claim["section"] not in expected:
            continue
        lines.extend(
            (
                f"- section: {claim['section']}",
                f"  bounded claim: {claim['claim_text']}",
                f"  required key: [{claim['cite_key']}]",
            )
        )
    return "\n".join(lines) if lines else "None. Do not add citation markers."


def _part_citation_instruction(
    claims: tuple[dict[str, str], ...],
    expected_major_sections: tuple[str, ...],
) -> str:
    """Render the exact citation authority available to one writer call."""

    context = _compact_part_citation_context(claims, expected_major_sections)
    return (
        "SECTION-SCOPED CITATION AUTHORITY:\n"
        f"{context}\n"
        "- Use only the exact required keys listed above for this part.\n"
        "- If the authority is None, do not add any citation marker in this part.\n"
        "- Do not invent a citation for a named method, foundation, dataset, or prior work.\n"
    )


def _citation_free_prior_context(text: str) -> str:
    """Remove citation markers before passing an earlier part to another writer call."""

    return _filter_citation_markers(text, frozenset())


def _filter_citation_markers(text: str, allowed_keys: frozenset[str]) -> str:
    """Keep only authorized citation keys while preserving non-citation brackets."""

    return filter_strict_citation_markers(text, allowed_keys)


def _citation_marker_free_bytes(text: str) -> bytes:
    """Remove only citation syntax and its conventional leading ASCII space."""

    return strip_strict_citation_markers(text).encode("utf-8")


def _section_scoped_writer_system(system: str, citation_instruction: str) -> str:
    """Append a same-layer citation override to a prompt-bank system instruction."""

    return (
        system.rstrip()
        + "\n\nSECTION-SCOPED CITATION OVERRIDE:\n"
        + citation_instruction.strip()
        + "\nThese section-scoped citation rules override every earlier general citation "
        "requirement in this system prompt. Do not emit a citation marker not authorized "
        "by this section-scoped authority.\n"
        + _SECTION_OUTPUT_CONTRACT.strip()
    )


def _write_paper_sections(
    *,
    llm: LLMClient,
    pm: PromptManager,
    run_dir: Path | None = None,
    preamble: str,
    topic_constraint: str,
    exp_metrics_instruction: str,
    citation_instruction: str,
    outline: str,
    model_name: str = "",
    venue_label: str = "NeurIPS/ICML",
    venue_guidance: str = "",
    is_hep: bool = False,
    stage_dir: Path | None = None,
    citation_repair_claims: tuple[dict[str, str], ...] = (),
    part_citation_instructions: Mapping[str, str] | None = None,
    heading_citation_instructions: Mapping[str, str] | None = None,
    canonical_fact_sheet: Mapping[str, Any] | None = None,
) -> str:
    """Write a conference-grade paper in 3 sequential LLM calls.

    ML path (default):
      Call 1: Title + Abstract + Introduction + Related Work
      Call 2: Method + Experiments
      Call 3: Results + Discussion + Limitations + Conclusion

    HEP path (when ``is_hep=True``, i.e. hep_ph domain detected):
      Call 1: Title + Abstract + Introduction
      Call 2: Model / Theoretical framework + Phenomenology / Computational setup
      Call 3: Results + Discussion + Conclusions (no Broader Impact, no Related Work block)
    """
    if heading_citation_instructions is not None:
        return _write_heading_scoped_paper_sections(
            llm=llm, pm=pm, preamble=preamble, topic_constraint=topic_constraint,
            exp_metrics_instruction=exp_metrics_instruction, outline=outline,
            model_name=model_name, is_hep=is_hep, stage_dir=stage_dir,
            citation_repair_claims=citation_repair_claims,
            heading_citation_instructions=heading_citation_instructions,
            canonical_fact_sheet=canonical_fact_sheet,
        )

    # Render writing_structure block for injection
    try:
        _writing_structure = pm.block("writing_structure")
    except (KeyError, Exception):  # noqa: BLE001
        _writing_structure = ""

    system = pm.for_stage(
        "paper_draft",
        evolution_overlay="",
        preamble=preamble,
        topic_constraint=topic_constraint,
        exp_metrics_instruction=exp_metrics_instruction,
        citation_instruction=citation_instruction,
        writing_structure=_writing_structure,
        outline="",
        venue_guidance=venue_guidance,
    ).system

    bounded_system = system.rstrip() + "\n\n" + _SECTION_OUTPUT_CONTRACT.strip()
    sections: list[str] = []
    section_generation_entries: list[dict[str, Any]] = []
    part1_sections = (
        ("Abstract", "Introduction")
        if is_hep
        else ("Abstract", "Introduction", "Related Work")
    )
    part2_sections = (
        ("Model / Theoretical Framework", "Phenomenology / Computational Setup")
        if is_hep
        else ("Method", "Experiments")
    )
    part3_sections = (
        ("Results", "Discussion", "Conclusions")
        if is_hep
        else ("Results", "Discussion", "Limitations", "Conclusion")
    )
    if part_citation_instructions is None:
        if citation_instruction:
            raise ValueError(
                "writer calls with citation authority require section-scoped instructions"
            )
        part_citation_instructions = {
            "part-1": _part_citation_instruction(citation_repair_claims, part1_sections),
            "part-2": _part_citation_instruction(citation_repair_claims, part2_sections),
            "part-3": _part_citation_instruction(citation_repair_claims, part3_sections),
        }
    if set(part_citation_instructions) != {"part-1", "part-2", "part-3"} or any(
        not isinstance(value, str) or not value.strip()
        for value in part_citation_instructions.values()
    ):
        raise ValueError("writer citation instructions must cover exactly three parts")
    part1_citation_instruction = part_citation_instructions["part-1"]
    part2_citation_instruction = part_citation_instructions["part-2"]
    part3_citation_instruction = part_citation_instructions["part-3"]
    part_allowed_citation_keys = {
        "part-1": frozenset(
            claim["cite_key"]
            for claim in citation_repair_claims
            if claim["section"] in part1_sections
        ),
        "part-2": frozenset(
            claim["cite_key"]
            for claim in citation_repair_claims
            if claim["section"] in part2_sections
        ),
        "part-3": frozenset(
            claim["cite_key"]
            for claim in citation_repair_claims
            if claim["section"] in part3_sections
        ),
    }
    part1_outline = _filter_citation_markers(outline, part_allowed_citation_keys["part-1"])
    part2_outline = _filter_citation_markers(outline, part_allowed_citation_keys["part-2"])
    part3_outline = _filter_citation_markers(outline, part_allowed_citation_keys["part-3"])
    part1_system = _section_scoped_writer_system(bounded_system, part1_citation_instruction)
    part2_system = _section_scoped_writer_system(bounded_system, part2_citation_instruction)
    part3_system = _section_scoped_writer_system(bounded_system, part3_citation_instruction)

    # --- R4-3: Title guidelines and abstract structure ---
    try:
        title_guidelines = pm.block("title_guidelines")
    except (KeyError, Exception):  # noqa: BLE001
        title_guidelines = ""
    try:
        abstract_structure = pm.block("abstract_structure")
    except (KeyError, Exception):  # noqa: BLE001
        abstract_structure = ""

    # IMP-20/25/31/24: Academic style, narrative, anti-hedging, anti-repetition
    try:
        academic_style_guide = pm.block("academic_style_guide")
    except (KeyError, Exception):  # noqa: BLE001
        academic_style_guide = ""
    try:
        narrative_writing_rules = pm.block("narrative_writing_rules")
    except (KeyError, Exception):  # noqa: BLE001
        narrative_writing_rules = ""
    try:
        anti_hedging_rules = pm.block("anti_hedging_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_hedging_rules = ""
    try:
        anti_repetition_rules = pm.block("anti_repetition_rules")
    except (KeyError, Exception):  # noqa: BLE001
        anti_repetition_rules = ""

    # --- Call 1: Title + Abstract + Introduction (+ Related Work for ML) ---
    if is_hep:
        call1_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{part1_citation_instruction}\n"
            f"{academic_style_guide}\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n"
            f"{anti_repetition_rules}\n\n"
            f"Write the following sections of a {venue_label}-quality HEP phenomenology "
            "paper in markdown. Use the JHEP/PRD convention — prior literature is woven "
            "into the Introduction (NO separate 'Related Work' section).\n\n"
            "1. **Title** (concise physics phrasing describing the model and the main "
            "observable; avoid ML-style catchy acronym+colon titles).\n"
            "2. **Abstract** (single paragraph, 150-250 words: motivation -> model -> "
            "method -> key numerical result in natural units -> implication for upcoming "
            "experiments). NO bullets.\n"
            "3. **Introduction** (800-1200 words): physics motivation, brief review of "
            "eligible literature under the effective citation policy (ATLAS/CMS/LZ/XENONnT/Fermi-LAT "
            "and recent JHEP/PRD theory work), statement of what the paper contributes. "
            "The review of prior work goes HERE; do NOT open a 'Related Work' section.\n\n"
            f"Outline:\n{part1_outline}\n\n"
            "Output markdown with ## headers. Do NOT include a References section.\n"
            "Start DIRECTLY with '## Title'. All equations must be LaTeX; all numerical "
            "quantities in natural units (GeV, pb, cm^2). Do NOT include 'Broader Impact' "
            "or 'Reproducibility Checklist' sections."
        )
    else:
        call1_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{part1_citation_instruction}\n"
            f"{title_guidelines}\n\n"
            f"{academic_style_guide}\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n"
            f"{anti_repetition_rules}\n\n"
            f"Write the following sections of a {venue_label}-quality paper in markdown. "
            "Follow the LENGTH REQUIREMENTS strictly:\n\n"
            "1. **Title** (HARD RULE: MUST be 14 words or fewer. Create a catchy method name "
            "first, then build the title: 'MethodName: Subtitle'. If your title exceeds 14 words, "
            "it will be automatically rejected. NEVER use 'Untitled Paper'.)\n"
            f"2. **Abstract** (150-220 words — HARD LIMIT. Do NOT exceed 220 words. "
            f"Do NOT include raw metric paths or 16-digit decimals.){abstract_structure}\n"
            "3. **Introduction** (800-1000 words): real-world motivation, problem statement, "
            "research gap analysis with citations, method overview, 3-4 contributions as bullet points, "
            "paper organization paragraph. Follow the effective citation policy.\n"
            "4. **Related Work** (600-800 words): organized into 3-4 thematic subsections, each discussing "
            "eligible prior work with proper citations. Compare approaches, identify limitations, position this work.\n\n"
            f"Outline:\n{part1_outline}\n\n"
            "Output markdown with ## headers. Do NOT include a References section.\n"
            "IMPORTANT: Start DIRECTLY with '## Title'. Do NOT include any preamble, "
            "data verification, condition listing, or metric enumeration before the title. "
            "The paper should read like a published manuscript, not a data report."
        )
    call1_user += _SECTION_OUTPUT_CONTRACT
    # R14-1: Higher token limit for reasoning models
    _paper_max_tokens = 12000
    if any(model_name.startswith(p) for p in ("gpt-5", "o3", "o4")):
        _paper_max_tokens = 24000

    # T3.5: Retry once on failure, use placeholder if still fails
    try:
        resp1 = _chat_with_prompt(llm, part1_system, call1_user, max_tokens=_paper_max_tokens, retries=1)
        part1 = resp1.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 17: Part 1 LLM call failed after transport retry")
        _raise_initial_part_transport_failure(
            part_name="part-1",
            expected_major_sections=("Abstract", "Introduction")
            if is_hep
            else ("Abstract", "Introduction", "Related Work"),
            title_slot=True,
            exc=exc,
            report_entries=section_generation_entries,
            stage_dir=stage_dir,
        )
    part1 = _validate_or_regenerate_paper_part(
        llm=llm,
        initial_text=part1,
        part_name="part-1",
        expected_major_sections=part1_sections,
        title_slot=True,
        citation_repair_context=_compact_part_citation_context(
            citation_repair_claims,
            part1_sections,
        ),
        allowed_citation_keys=part_allowed_citation_keys["part-1"],
        max_tokens=_paper_max_tokens,
        report_entries=section_generation_entries,
        stage_dir=stage_dir,
    )
    sections.append(part1)
    logger.info("Stage 17: Part 1 (Title+Abstract+Intro+Related Work) — %d chars", len(part1))
    part1_prior_context = _citation_free_prior_context(part1)

    # --- Call 2: Method + Experiments (ML)  OR  Model + Phenomenology (HEP) ---
    if is_hep:
        call2_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n\n"
            f"{part2_citation_instruction}\n"
            "You are continuing an HEP phenomenology paper. The sections written so far are:\n\n"
            f"---\n{part1_prior_context}\n---\n\n"
            "Now write the next sections:\n\n"
            "4. **Model / Theoretical framework** (1200-1800 words): the Lagrangian density "
            "(LaTeX, numbered equations), particle content, gauge structure, free parameters "
            "and their allowed ranges. Provide the Feynman rules or EFT operator coefficients "
            "relevant to the observables considered. Write as FLOWING PROSE with numbered "
            "equations — do NOT use bullet lists.\n"
            "5. **Phenomenology / Computational setup** (800-1200 words): the observables "
            "(cross sections, decay widths, relic density, direct-detection rates) and the "
            "formulas or tool-chain used to compute them. List every experimental constraint "
            "imposed with its explicit CL level and only an authorized reference. Units MUST be natural (GeV, pb, "
            "cm^2, Omega_h^2).\n\n"
            f"Outline:\n{part2_outline}\n\n"
            "Output markdown with ## headers. Continue from where Part 1 ended."
        )
    else:
        call2_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n\n"
            f"{part2_citation_instruction}\n"
            "You are continuing a paper. The sections written so far are:\n\n"
            f"---\n{part1_prior_context}\n---\n\n"
            "Now write the next sections, maintaining consistency with the above:\n\n"
            "5. **Method** (1000-1500 words): formal problem definition with mathematical notation "
            "($x$, $\\theta$, etc.), detailed algorithm description with equations, step-by-step procedure, "
            "complexity analysis, design rationale for key choices. Include algorithm pseudocode if applicable. "
            "Write as FLOWING PROSE — do NOT use bullet-point lists for method components.\n"
            "6. **Experiments** (800-1200 words): detailed experimental setup, datasets with statistics "
            "(size, splits, features), all baselines and their implementations, hyperparameter settings "
            "in a markdown table, evaluation metrics with mathematical definitions, hardware and runtime info.\n"
            "METHOD NAMES IN TABLES: Use SHORT abbreviations (4-8 chars) for method names "
            "in tables. Define abbreviation mappings in a footnote. "
            "NEVER put method names longer than 20 characters in table cells.\n\n"
            f"Outline:\n{part2_outline}\n\n"
            "Output markdown with ## headers. Continue from where Part 1 ended."
        )
    call2_user += _SECTION_OUTPUT_CONTRACT
    try:
        resp2 = _chat_with_prompt(llm, part2_system, call2_user, max_tokens=_paper_max_tokens, retries=1)
        part2 = resp2.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 17: Part 2 LLM call failed after transport retry")
        _raise_initial_part_transport_failure(
            part_name="part-2",
            expected_major_sections=part2_sections,
            title_slot=False,
            exc=exc,
            report_entries=section_generation_entries,
            stage_dir=stage_dir,
        )
    part2 = _validate_or_regenerate_paper_part(
        llm=llm,
        initial_text=part2,
        part_name="part-2",
        expected_major_sections=part2_sections,
        title_slot=False,
        citation_repair_context=_compact_part_citation_context(
            citation_repair_claims,
            part2_sections,
        ),
        allowed_citation_keys=part_allowed_citation_keys["part-2"],
        max_tokens=_paper_max_tokens,
        report_entries=section_generation_entries,
        stage_dir=stage_dir,
    )
    sections.append(part2)
    logger.info("Stage 17: Part 2 (Method+Experiments) — %d chars", len(part2))
    part2_prior_context = _citation_free_prior_context(part2)

    # --- Call 3: Results + Discussion + (Limitations) + Conclusion ---
    if is_hep:
        call3_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n"
            f"{anti_repetition_rules}\n\n"
            f"{part3_citation_instruction}\n"
            "You are completing an HEP phenomenology paper. Sections so far:\n\n"
            f"---\n{part1_prior_context}\n\n{part2_prior_context}\n---\n\n"
            "Now write the final sections:\n\n"
            "6. **Results** (800-1200 words): report parameter-space scans and 95% CL "
            "exclusion contours. Include tabulated predictions and a headline log-log "
            "exclusion plot (use ![Caption](charts/filename.png) markdown). Overlay "
            "current bounds and projected sensitivities. Discuss complementarity between "
            "direct, indirect, and collider probes.\n"
            "7. **Discussion** (400-800 words): comparison with earlier work, theoretical "
            "and experimental uncertainties (QCD scale, PDF, nuclear form factors, "
            "astrophysical J-factors), comment on the tension / consistency with "
            "independent constraints.\n"
            "8. **Conclusions** (200-400 words): summarise the main physical findings; "
            "state falsifiable predictions for HL-LHC / DARWIN / LZ / CTA and the "
            "timescale on which they can test the model.\n\n"
            "CRITICAL FORMATTING RULES:\n"
            "- Use FLOWING PROSE, not bullet lists, except for equation labels.\n"
            "- All numerical values in natural units; keep 3-4 significant figures.\n"
            "- Figures referenced with 'As shown in Fig. 1, ...' style.\n"
            "- Every table caption is descriptive (not 'Table 1').\n"
            "- Do NOT add 'Broader Impact', 'Reproducibility Checklist', 'Ethics', or "
            "'Societal Impact' sections.\n\n"
            "Output markdown with ## headers. Do NOT include a References section."
        )
    else:
        call3_user = (
            f"{preamble}\n\n"
            f"{topic_constraint}"
            f"{exp_metrics_instruction}\n\n"
            f"{narrative_writing_rules}\n"
            f"{anti_hedging_rules}\n"
            f"{anti_repetition_rules}\n\n"
            f"{part3_citation_instruction}\n"
            "You are completing a paper. The sections written so far are:\n\n"
            f"---\n{part1_prior_context}\n\n{part2_prior_context}\n---\n\n"
            "Now write the final sections, maintaining consistency:\n\n"
            "7. **Results** (600-800 words):\n"
            "   - START with an AGGREGATED results table (Table 1): rows = methods, columns = metrics.\n"
            "     Each cell = mean \u00b1 std across seeds. Bold the best value per column.\n"
            "     EVERY table MUST have a descriptive caption that allows understanding without "
            "     reading the main text. NEVER use just 'Table 1' as a caption.\n"
            "   - Follow with a PER-REGIME table (Table 2) breaking down by easy/hard regimes.\n"
            "   - Include a STATISTICAL COMPARISON table (Table 3): paired t-tests between key methods.\n"
            "   - NEVER dump raw per-seed numbers in the main text. Aggregate first, then discuss.\n"
            "   - MUST include at least 2 figures using markdown image syntax: ![Caption](charts/filename.png)\n"
            "     One figure MUST be a performance comparison chart. Figures MUST be referenced "
            "     in text: 'As shown in Figure 1, ...'\n"
            "8. **Discussion** (400-600 words): interpretation of key findings, unexpected results, "
            "comparison with prior work only when authorized by this section's citation authority, "
            "practical implications.\n"
            "9. **Limitations** (200-300 words): honest assessment of scope, dataset, methodology. "
            "ALL caveats consolidated HERE — nowhere else in the paper.\n"
            "10. **Conclusion** (100-200 words MAXIMUM — this is a HARD LIMIT): "
            "Summarize contributions in 2-3 sentences. State main finding in 1 sentence. "
            "Suggest 2-3 concrete future directions in 1-2 sentences. "
            "Do NOT repeat any specific numbers from Results. Do NOT restate the abstract. "
            "A good conclusion is SHORT and forward-looking.\n\n"
            "CRITICAL FORMATTING RULES FOR ALL SECTIONS:\n"
            "- Write as FLOWING PROSE paragraphs, NOT bullet-point lists\n"
            "- NEVER dump raw metric paths like 'config/method_name/seed_3/primary_metric'\n"
            "- All numbers must be rounded to 4 decimal places maximum\n"
            "- Every table MUST have a descriptive caption (not just 'Table 1')\n"
            "- Use \\begin{algorithm} or pseudocode notation, NOT \\begin{verbatim}\n\n"
            "Output markdown with ## headers. Do NOT include a References section."
        )
    call3_user += _SECTION_OUTPUT_CONTRACT
    try:
        resp3 = _chat_with_prompt(llm, part3_system, call3_user, max_tokens=_paper_max_tokens, retries=1)
        part3 = resp3.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Stage 17: Part 3 LLM call failed after transport retry")
        _raise_initial_part_transport_failure(
            part_name="part-3",
            expected_major_sections=part3_sections,
            title_slot=False,
            exc=exc,
            report_entries=section_generation_entries,
            stage_dir=stage_dir,
        )
    part3 = _validate_or_regenerate_paper_part(
        llm=llm,
        initial_text=part3,
        part_name="part-3",
        expected_major_sections=part3_sections,
        title_slot=False,
        citation_repair_context=_compact_part_citation_context(
            citation_repair_claims,
            part3_sections,
        ),
        allowed_citation_keys=part_allowed_citation_keys["part-3"],
        max_tokens=_paper_max_tokens,
        report_entries=section_generation_entries,
        stage_dir=stage_dir,
    )
    sections.append(part3)
    logger.info("Stage 17: Part 3 (Results+Discussion+Limitations+Conclusion) — %d chars", len(part3))

    # Combine all sections
    draft = "\n\n".join(sections)

    # R32: Strip data verification preamble that LLMs sometimes emit before
    # the actual paper.  The preamble typically starts with "## Tested Conditions"
    # or similar headings and ends before "## Title".
    import re as _re_strip
    _title_match = _re_strip.search(r"^## Title\b", draft, _re_strip.MULTILINE)
    if _title_match and _title_match.start() > 200:
        _stripped = draft[_title_match.start():]
        logger.info(
            "R32: Stripped %d-char preamble before '## Title'",
            _title_match.start(),
        )
        draft = _stripped

    total_words = len(draft.split())
    logger.info("Stage 17: Full draft — %d chars, ~%d words", len(draft), total_words)

    return draft


def _citation_safe_system(system: str, authority: str) -> str:
    """Remove inherited citation mandates before adding one heading authority."""

    retained = [
        line for line in system.splitlines()
        if "cite" not in line.casefold() and "citation" not in line.casefold()
    ]
    return _section_scoped_writer_system("\n".join(retained), authority)


def _heading_writer_groups(
    base_groups: tuple[tuple[str, ...], ...],
    claims: tuple[dict[str, str], ...],
) -> tuple[tuple[str, ...], ...]:
    """Keep zero-authority headings together; isolate every authority heading."""

    authority_headings = {claim["section"] for claim in claims}
    result: list[tuple[str, ...]] = []
    for base_group in base_groups:
        pending: list[str] = []
        for heading in base_group:
            if heading in authority_headings:
                if pending:
                    result.append(tuple(pending))
                    pending = []
                result.append((heading,))
            else:
                pending.append(heading)
        if pending:
            result.append(tuple(pending))
    return tuple(result)


def _write_heading_scoped_paper_sections(
    *,
    llm: LLMClient,
    pm: PromptManager,
    preamble: str,
    topic_constraint: str,
    exp_metrics_instruction: str,
    outline: str,
    model_name: str,
    is_hep: bool,
    stage_dir: Path | None,
    citation_repair_claims: tuple[dict[str, str], ...],
    heading_citation_instructions: Mapping[str, str],
    canonical_fact_sheet: Mapping[str, Any] | None,
) -> str:
    """Write only contiguous zero-authority or single authority-heading groups."""

    base_groups = (
        (("Abstract", "Introduction"),
         ("Model / Theoretical Framework", "Phenomenology / Computational Setup"),
         ("Results", "Discussion", "Conclusions"))
        if is_hep else
        (("Abstract", "Introduction", "Related Work"),
         ("Method", "Experiments"),
         ("Results", "Discussion", "Limitations", "Conclusion"))
    )
    headings = tuple(heading for group in base_groups for heading in group)
    if set(heading_citation_instructions) != set(headings):
        raise ValueError("heading citation authority must cover the active template")
    groups = _heading_writer_groups(base_groups, citation_repair_claims)
    if canonical_fact_sheet is not None:
        split_groups: list[tuple[str, ...]] = []
        for group in groups:
            pending: list[str] = []
            current_view = ""
            for heading in group:
                view = fact_sheet_view_for_heading(heading)
                if pending and view != current_view:
                    split_groups.append(tuple(pending))
                    pending = []
                pending.append(heading)
                current_view = view
            if pending:
                split_groups.append(tuple(pending))
        groups = tuple(split_groups)
    grounding_contexts = (
        build_heading_grounding_contexts(canonical_fact_sheet, headings)
        if canonical_fact_sheet is not None
        else None
    )
    system = pm.for_stage(
        "paper_draft", evolution_overlay="", preamble="", topic_constraint="",
        exp_metrics_instruction="", citation_instruction="", writing_structure="",
        outline="", venue_guidance="",
    ).system
    report_entries: list[dict[str, Any]] = []
    generated: list[str] = []
    for index, group in enumerate(groups):
        allowed = frozenset(
            claim["cite_key"] for claim in citation_repair_claims
            if claim["section"] in group
        )
        authority = "\n\n".join(heading_citation_instructions[heading] for heading in group)
        safe_outline = (
            "\n".join(f"## {heading}" for heading in group)
            if grounding_contexts is not None
            else _filter_citation_markers(outline, allowed)
        )
        safe_prior = (
            ""
            if grounding_contexts is not None
            else _citation_free_prior_context("\n\n".join(generated))
        )
        grounding = (
            "\n\n".join(
                dict.fromkeys(grounding_contexts[heading] for heading in group)
            )
            if grounding_contexts is not None
            else _filter_citation_markers(exp_metrics_instruction, allowed)
        )
        title_slot = index == 0
        requested = (("Title",) if title_slot else ()) + group
        heading_lines = "\n".join(f"- ## {heading}" for heading in requested)
        user = (
            f"{'' if grounding_contexts is not None else _filter_citation_markers(preamble, allowed)}\n"
            f"{'' if grounding_contexts is not None else _filter_citation_markers(topic_constraint, allowed)}\n"
            f"{grounding}\n\n"
            f"Write exactly these manuscript headings:\n{heading_lines}\n\n"
            f"{authority}\n\n"
            "Prior context is citation-free and may not be repeated:\n"
            f"---\n{safe_prior}\n---\n\n"
            f"Outline:\n{safe_outline}\n"
            "Do not output a References section."
            + _SECTION_OUTPUT_CONTRACT
        )
        response = _chat_with_prompt(
            llm,
            _citation_safe_system(
                system,
                authority
                + (
                    "\n\nCANONICAL FACT AUTHORITY:\n" + grounding
                    if grounding_contexts is not None
                    else ""
                ),
            ),
            user,
            max_tokens=24000 if model_name.startswith(("gpt-5", "o3", "o4")) else 12000,
            retries=1,
        ).content.strip()
        part = _validate_or_regenerate_paper_part(
            llm=llm, initial_text=response, part_name=f"heading-group-{index + 1}",
            expected_major_sections=group, title_slot=title_slot,
            citation_repair_context=_compact_part_citation_context(citation_repair_claims, group),
            allowed_citation_keys=allowed, max_tokens=12000,
            report_entries=report_entries, stage_dir=stage_dir,
            grounding_context=grounding,
        )
        generated.append(part)
    return "\n\n".join(generated)


def _heading_citation_state(
    paper_text: str, claims: tuple[dict[str, str], ...]
) -> dict[str, set[str]]:
    """Return the exact heading-local citation closure state for a draft."""

    assigned: dict[str, set[str]] = {}
    for claim in claims:
        assigned.setdefault(claim["section"], set()).add(claim["cite_key"])
    attributed = attribute_citation_keys_to_top_level_headings(paper_text)
    actual = {heading: set(keys) for heading, keys in attributed.items()}
    all_expected = set().union(*assigned.values()) if assigned else set()
    all_actual = set(strict_citation_keys(paper_text))
    missing = {
        heading: keys - actual.get(heading, set())
        for heading, keys in assigned.items()
        if keys - actual.get(heading, set())
    }
    misplaced = {
        heading: actual.get(heading, set()) - assigned.get(heading, set())
        for heading in actual
        if actual.get(heading, set()) - assigned.get(heading, set())
    }
    return {
        "missing": set().union(*missing.values()) if missing else set(),
        "misplaced": set().union(*misplaced.values()) if misplaced else set(),
        "unknown_or_unplanned": all_actual - all_expected,
        "missing_by_heading": missing,
        "actual": all_actual,
    }


def _top_level_heading_blocks(paper_text: str) -> dict[str, str]:
    document = parse_manuscript(paper_text, strict=True)
    lines = paper_text.splitlines(keepends=True)
    top_sections = [section for section in document.sections if len(section.path) == 1]
    blocks: dict[str, str] = {}
    for index, section in enumerate(top_sections):
        end = top_sections[index + 1].start_line if index + 1 < len(top_sections) else len(lines)
        blocks[section.title] = "".join(lines[section.start_line:end])
    return blocks


def _repair_heading_citation_closure(
    *,
    llm: LLMClient,
    run_dir: Path,
    stage_dir: Path,
    paper_text: str,
    evidence: CanonicalExperimentEvidence,
    claims: tuple[dict[str, str], ...],
    heading_citation_instructions: Mapping[str, str],
    system: str,
    canonical_fact_sheet: Mapping[str, Any] | None = None,
) -> str:
    """Insert missing plan markers deterministically; never rewrite prose."""

    del llm, run_dir, evidence, heading_citation_instructions, system, canonical_fact_sheet
    state = _heading_citation_state(paper_text, claims)
    if state["unknown_or_unplanned"] or state["misplaced"]:
        raise CitationPlanContractError(
            "existing citation occurrence is unknown or has a misplaced anchor"
        )
    claim_by_key = {claim["cite_key"]: claim for claim in claims}
    if len(claim_by_key) != len(claims):
        raise CitationPlanContractError("citation repair claims contain duplicate keys")

    blocks = _top_level_heading_blocks(paper_text)
    baseline_occurrences = parse_strict_citation_occurrences(paper_text)
    for occurrence in baseline_occurrences:
        for key in occurrence.keys:
            claim = claim_by_key.get(key)
            if claim is None:
                raise CitationPlanContractError("existing citation key lacks plan claim")
            sentence = paper_text[occurrence.sentence_start:occurrence.sentence_end]
            if strip_strict_citation_markers(sentence) != claim["claim_text"]:
                raise CitationPlanContractError(
                    "existing citation occurrence moved from its plan sentence anchor"
                )
            block = blocks.get(claim["section"])
            if block is None:
                raise CitationPlanContractError("existing citation heading is missing")
            anchor_matches = [
                (start, end)
                for start, end in strict_sentence_spans(block)
                if strip_strict_citation_markers(block[start:end])
                == claim["claim_text"]
            ]
            if len(anchor_matches) != 1:
                raise CitationPlanContractError(
                    "existing citation plan sentence anchor is not unique"
                )

    insertions: list[tuple[int, tuple[str, ...], str]] = []
    for heading, missing_keys in state["missing_by_heading"].items():
        block = blocks.get(heading)
        if block is None:
            raise CitationPlanContractError(f"required citation heading is missing: {heading}")
        block_start = paper_text.index(block)
        by_position: dict[int, list[str]] = {}
        for key in sorted(missing_keys):
            claim = claim_by_key[key]
            if claim["section"] != heading:
                raise CitationPlanContractError("citation repair heading binding mismatch")
            matches = [
                (start, end)
                for start, end in strict_sentence_spans(block)
                if strip_strict_citation_markers(block[start:end])
                == claim["claim_text"]
            ]
            if len(matches) != 1:
                raise CitationPlanContractError(
                    "citation repair claim sentence anchor is not unique"
                )
            _start, end = matches[0]
            insert_at = end - 1 if block[end - 1] in ".!?" else end
            by_position.setdefault(insert_at, []).append(key)
        for position, keys in by_position.items():
            insertions.append((block_start + position, tuple(sorted(keys)), heading))

    current = paper_text
    for position, keys, _heading in sorted(insertions, reverse=True):
        current = current[:position] + f" [{', '.join(keys)}]" + current[position:]
    _assert_marker_only_citation_delta(paper_text, current)

    repaired_occurrences = parse_strict_citation_occurrences(current)
    repaired_signatures = {
        (
            item.syntax,
            item.keys,
            item.sentence_sha256,
            item.sentence_ordinal,
            item.occurrence_ordinal,
        )
        for item in repaired_occurrences
    }
    for item in baseline_occurrences:
        signature = (
            item.syntax,
            item.keys,
            item.sentence_sha256,
            item.sentence_ordinal,
            item.occurrence_ordinal,
        )
        if signature not in repaired_signatures:
            raise CitationPlanContractError(
                "existing citation occurrence key, anchor, or ordinal changed"
            )
    operations = [
        {
            "kind": "deterministic_marker_insertion",
            "heading": heading,
            "missing_keys": list(keys),
        }
        for _position, keys, heading in sorted(insertions)
    ]
    state = _heading_citation_state(current, claims)
    if state["unknown_or_unplanned"] or state["misplaced"] or state["missing"]:
        raise CitationPlanContractError("heading-local citation repair did not close citations")
    (stage_dir / "citation_heading_repair_log.json").write_text(
        canonical_json_text({"schema_version": 1, "_diagnostic": True, "operations": operations}),
        encoding="utf-8",
    )
    return current


def _assert_marker_only_citation_delta(before: str, after: str) -> None:
    """Reject citation repair that changes any non-marker manuscript byte."""

    if _citation_marker_free_bytes(before) != _citation_marker_free_bytes(after):
        raise CitationPlanContractError("citation repair violated marker-only delta")


# ---------------------------------------------------------------------------
# Draft quality validation (section balance + bullet-point density)
# ---------------------------------------------------------------------------

# Sections where bullets/numbered lists are acceptable.
_BULLET_LENIENT_SECTIONS = frozenset({
    "introduction", "limitations", "limitation",
    "limitations and future work", "abstract",
})

# Main body sections used for balance ratio check.
_BALANCE_SECTIONS = frozenset({
    "introduction", "related work", "method", "experiments", "results",
    "discussion",
})


def _validate_draft_quality(
    draft: str,
    stage_dir: Path | None = None,
    citation_target: int = 15,
) -> dict[str, Any]:
    """Validate a paper draft for section balance and prose quality.

    Checks:
    1. Per-section word count vs ``SECTION_WORD_TARGETS``.
    2. Bullet-point / numbered-list density per section.
    3. Largest-to-smallest main-section word-count ratio.

    Returns a dict with ``section_analysis``, ``overall_warnings``, and
    ``revision_directives``.  Optionally writes ``draft_quality.json`` to
    *stage_dir*.
    """
    from researchclaw.prompts import SECTION_WORD_TARGETS, _SECTION_TARGET_ALIASES

    _heading_re = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    matches = list(_heading_re.finditer(draft))

    sections_data: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(draft)
        body = draft[start:end].strip()
        sections_data.append({
            "heading": heading,
            "heading_lower": heading.strip().lower(),
            "level": level,
            "body": body,
        })

    section_analysis: list[dict[str, Any]] = []
    overall_warnings: list[str] = []
    revision_directives: list[str] = []
    main_section_words: dict[str, int] = {}

    _bullet_re = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
    _numbered_re = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)

    # BUG-24: Accumulate subsection (H3+) word counts into parent H2 sections
    _subsection_words: dict[str, int] = {}
    _current_parent = ""
    for sec in sections_data:
        if sec["level"] <= 2:
            _current_parent = sec["heading_lower"]
            _subsection_words.setdefault(_current_parent, 0)
        else:
            # Add subsection words to parent
            _subsection_words[_current_parent] = (
                _subsection_words.get(_current_parent, 0) + len(sec["body"].split())
            )

    for sec in sections_data:
        if sec["level"] > 2:
            continue
        heading_lower: str = sec["heading_lower"]
        body: str = sec["body"]
        # BUG-24: Include subsection words in the parent's word count
        word_count = len(body.split()) + _subsection_words.get(heading_lower, 0)
        canon = heading_lower
        if canon not in SECTION_WORD_TARGETS:
            canon = _SECTION_TARGET_ALIASES.get(heading_lower, "")
        entry: dict[str, Any] = {
            "heading": sec["heading"],
            "word_count": word_count,
            "canonical": canon,
        }
        if canon and canon in SECTION_WORD_TARGETS:
            lo, hi = SECTION_WORD_TARGETS[canon]
            entry["target"] = [lo, hi]
            if word_count < int(lo * 0.7):
                overall_warnings.append(
                    f"{sec['heading']} is severely under target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"EXPAND {sec['heading']} from {word_count} to {lo}+ words. "
                    f"Add substantive content \u2014 do NOT pad with filler."
                )
                entry["status"] = "severely_short"
            elif word_count < lo:
                overall_warnings.append(
                    f"{sec['heading']} is under target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"Expand {sec['heading']} from {word_count} to {lo}+ words."
                )
                entry["status"] = "short"
            elif word_count > int(hi * 1.3):
                overall_warnings.append(
                    f"{sec['heading']} exceeds target "
                    f"({word_count} words, target {lo}-{hi})"
                )
                revision_directives.append(
                    f"Compress {sec['heading']} from {word_count} to {hi} words or fewer."
                )
                entry["status"] = "long"
            else:
                entry["status"] = "ok"
        if body:
            total_lines = len([ln for ln in body.splitlines() if ln.strip()])
            bullet_lines = len(_bullet_re.findall(body)) + len(_numbered_re.findall(body))
            density = bullet_lines / total_lines if total_lines > 0 else 0.0
            entry["bullet_density"] = round(density, 2)
            threshold = 0.50 if heading_lower in _BULLET_LENIENT_SECTIONS else 0.25
            if density > threshold and total_lines >= 4:
                overall_warnings.append(
                    f"{sec['heading']} has {bullet_lines}/{total_lines} "
                    f"bullet/numbered lines ({density:.0%} density, "
                    f"threshold {threshold:.0%})"
                )
                revision_directives.append(
                    f"REWRITE {sec['heading']} as flowing academic prose. "
                    f"Convert bullet points to narrative paragraphs."
                )
                entry["bullet_status"] = "high"
            else:
                entry["bullet_status"] = "ok"
        canon_balance = canon or heading_lower
        if canon_balance in _BALANCE_SECTIONS:
            main_section_words[canon_balance] = word_count
        section_analysis.append(entry)

    if len(main_section_words) >= 2:
        wc_values = list(main_section_words.values())
        max_wc = max(wc_values)
        min_wc = min(wc_values)
        if min_wc > 0 and max_wc / min_wc > 3.0:
            largest = max(main_section_words, key=main_section_words.get)  # type: ignore[arg-type]
            smallest = min(main_section_words, key=main_section_words.get)  # type: ignore[arg-type]
            overall_warnings.append(
                f"Section imbalance: {largest} ({max_wc} words) vs "
                f"{smallest} ({min_wc} words) \u2014 ratio {max_wc / min_wc:.1f}x"
            )
            revision_directives.append(
                f"Rebalance sections: expand {smallest} and/or compress {largest} "
                f"to achieve more even section lengths."
            )

    # --- C-4/C-5: Citation count and recency checks ---
    _cite_pattern = re.compile(r"\[([a-zA-Z][a-zA-Z0-9_-]*\d{4}[a-zA-Z0-9]*)\]")
    cited_keys = set(_cite_pattern.findall(draft))
    if cited_keys:
        n_citations = len(cited_keys)
        if n_citations < citation_target:
            overall_warnings.append(
                f"Only {n_citations} unique citations found "
                f"(effective target: >={citation_target})"
            )
            revision_directives.append(
                f"Use eligible references up to the effective target of "
                f"{citation_target}; currently {n_citations} unique citations."
            )
        # Check recency: count citations with year >= current_year - 2
        _year_pat = re.compile(r"(\d{4})")
        import datetime as _dt_cit
        _cur_year = _dt_cit.datetime.now().year
        recent_count = sum(
            1 for k in cited_keys
            for m in [_year_pat.search(k)]
            if m and int(m.group(1)) >= _cur_year - 2
        )
        recency_ratio = recent_count / n_citations if n_citations > 0 else 0.0
        if recency_ratio < 0.3 and n_citations >= 10:
            overall_warnings.append(
                f"Citation recency low: only {recent_count}/{n_citations} "
                f"({recency_ratio:.0%}) from last 3 years (target: >=30%%)"
            )

    # --- Abstract and Conclusion length enforcement ---
    for sec in sections_data:
        hl = sec["heading_lower"]
        body_text: str = sec["body"]
        wc = len(body_text.split())
        if hl == "abstract" and wc > 250:
            overall_warnings.append(
                f"Abstract is too long: {wc} words (target: 150-220 words)"
            )
            revision_directives.append(
                f"COMPRESS the Abstract from {wc} to 150-220 words. "
                f"Remove raw metric values, redundant context, and self-references."
            )
        if hl in ("conclusion", "conclusions", "conclusion and future work"):
            if wc > 300:
                overall_warnings.append(
                    f"Conclusion is too long: {wc} words (target: 100-200 words)"
                )
                revision_directives.append(
                    f"COMPRESS the Conclusion from {wc} to 100-200 words. "
                    f"Do NOT repeat specific metric values from Results. "
                    f"Summarize findings in 2-3 sentences, then 2-3 future directions."
                )

    # --- Raw metric path detection (log dumps in prose) ---
    _raw_path_re = re.compile(
        r"\\texttt\{[a-zA-Z0-9_/.-]+(?:/[a-zA-Z0-9_/.-]+){2,}",
    )
    raw_path_count = len(_raw_path_re.findall(draft))
    if raw_path_count > 3:
        overall_warnings.append(
            f"Raw metric paths in prose: {raw_path_count} instances of "
            f"\\texttt{{config/path/metric}} style dumps"
        )
        revision_directives.append(
            "REMOVE raw experiment log paths from prose. Replace "
            "\\texttt{config/metric/path} with human-readable metric names "
            "and summarize values in tables, not inline text."
        )

    # --- Writing quality lint ---
    _weasel_words = re.compile(
        r"\b(various|many|several|quite|fairly|really|very|rather|"
        r"somewhat|relatively|arguably|interestingly|importantly|"
        r"it is well known that|it is obvious that|clearly)\b",
        re.IGNORECASE,
    )
    _duplicate_words = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
    weasel_count = len(_weasel_words.findall(draft))
    dup_matches = _duplicate_words.findall(draft)
    dup_count = len([d for d in dup_matches if d.lower() not in ("that", "had")])
    if weasel_count > 20:
        overall_warnings.append(
            f"High weasel-word count: {weasel_count} instances "
            f"(consider replacing vague words with precise language)"
        )
        revision_directives.append(
            "Replace vague hedging words (various, several, quite, fairly, "
            "rather, somewhat) with precise quantities or remove them."
        )
    if dup_count > 0:
        overall_warnings.append(
            f"Duplicate adjacent words found: {dup_count} instance(s) "
            f"(e.g., 'the the', 'is is')"
        )
        revision_directives.append(
            "Fix duplicate adjacent words (likely typos)."
        )

    # --- AI-slop / boilerplate detection ---
    _BOILERPLATE_PHRASES = [
        "delves into", "delve into", "it is worth noting",
        "it should be noted", "it is important to note",
        "leverage the power of", "leverages the power of",
        "in this paper, we propose", "in this work, we propose",
        "to the best of our knowledge",
        "in the realm of", "in the landscape of",
        "plays a crucial role", "plays a pivotal role",
        "groundbreaking", "cutting-edge", "state-of-the-art",
        "game-changing", "paradigm shift",
        "a myriad of", "a plethora of",
        "aims to bridge the gap", "bridge the gap",
        "shed light on", "sheds light on",
        "pave the way", "paves the way",
        "the advent of", "with the advent of",
        "in recent years", "in recent times",
        "has gained significant attention",
        "has attracted considerable interest",
        "has emerged as a promising",
        "a comprehensive overview",
        "a holistic approach", "holistic understanding",
        "showcasing the efficacy", "demonstrate the efficacy",
        "multifaceted", "underscores the importance",
        "navigate the complexities",
        "harness the potential", "harnessing the power",
        "it is imperative to", "it is crucial to",
        "a nuanced understanding", "nuanced approach",
        "robust and scalable", "seamlessly integrates",
        "the intricacies of", "intricate interplay",
        "facilitate a deeper understanding",
        "a testament to",
    ]
    draft_lower = draft.lower()
    boilerplate_hits: list[str] = []
    for phrase in _BOILERPLATE_PHRASES:
        count = draft_lower.count(phrase)
        if count > 0:
            boilerplate_hits.extend([phrase] * count)
    if len(boilerplate_hits) > 5:
        unique_phrases = sorted(set(boilerplate_hits))[:5]
        overall_warnings.append(
            f"AI boilerplate detected: {len(boilerplate_hits)} instances "
            f"of generic LLM phrases (e.g., {', '.join(repr(p) for p in unique_phrases[:3])})"
        )
        revision_directives.append(
            "REWRITE sentences containing AI-generated boilerplate phrases. "
            "Replace generic language (e.g., 'delves into', 'it is worth noting', "
            "'leverages the power of', 'plays a crucial role', 'paves the way') "
            "with precise, specific academic language."
        )

    # --- Related work depth check ---
    _rw_headings = {"related work", "related works", "background", "literature review"}
    rw_body = ""
    for sec in sections_data:
        if sec["heading_lower"] in _rw_headings and sec["level"] <= 2:
            rw_body = sec["body"]
            break
    if rw_body and len(rw_body.split()) > 50:
        _comparative_pats = re.compile(
            r"\b(unlike|in contrast|whereas|while .+ focus|"
            r"however|differ(?:s|ent)|our (?:method|approach) .+ instead|"
            r"we (?:instead|differ)|compared to|as opposed to|"
            r"goes beyond|extends|improves upon|addresses the limitation)\b",
            re.IGNORECASE,
        )
        sentences = [s.strip() for s in re.split(r"[.!?]+", rw_body) if s.strip()]
        comparative_sents = sum(1 for s in sentences if _comparative_pats.search(s))
        ratio = comparative_sents / len(sentences) if sentences else 0.0
        if ratio < 0.15 and len(sentences) >= 5:
            overall_warnings.append(
                f"Related Work is purely descriptive: only {comparative_sents}/{len(sentences)} "
                f"sentences ({ratio:.0%}) contain comparative language (target: >=15%)"
            )
            revision_directives.append(
                "REWRITE Related Work to critically compare with prior methods. "
                "Use phrases like 'unlike X, our approach...', 'in contrast to...', "
                "'while X focuses on... we address...' for at least 20% of sentences."
            )

    # --- Statistical rigor check (result sections) ---
    _results_headings = {"results", "experiments", "experimental results", "evaluation"}
    results_body = ""
    for sec in sections_data:
        if sec["heading_lower"] in _results_headings and sec["level"] <= 2:
            results_body += sec["body"] + "\n"
    if results_body and len(results_body.split()) > 100:
        has_std = bool(re.search(r"\u00b1|\\pm|\bstd\b|\\std\b|standard deviation", results_body, re.IGNORECASE))
        has_ci = bool(re.search(r"confidence interval|\bCI\b|95%|p-value|p\s*<", results_body, re.IGNORECASE))
        has_seeds = bool(re.search(r"(?:seed|run|trial)s?\s*[:=]\s*\d|averaged?\s+over\s+\d+\s+(?:seed|run|trial)", results_body, re.IGNORECASE))
        if not has_std and not has_ci and not has_seeds:
            overall_warnings.append(
                "No statistical measures found in results (no std, CI, p-values, or multi-seed reporting)"
            )
            revision_directives.append(
                "ADD error bars (\u00b1std), confidence intervals, or note the number of "
                "random seeds used. Single-run results without variance reporting "
                "are insufficient for top venues."
            )

    result: dict[str, Any] = {
        "section_analysis": section_analysis,
        "overall_warnings": overall_warnings,
        "revision_directives": revision_directives,
    }
    if stage_dir is not None:
        (stage_dir / "draft_quality.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if overall_warnings:
            logger.warning(
                "Draft quality: %d warning(s) \u2014 %s",
                len(overall_warnings),
                "; ".join(overall_warnings[:3]),
            )
        else:
            logger.info("Draft quality: all checks passed")
    return result


def _review_compiled_pdf(
    pdf_path: Path,
    llm: LLMClient,
    topic: str,
) -> dict[str, Any]:
    """Multi-dimensional LLM review of compiled paper (AI-Scientist style).

    Scores the paper on 7 academic review dimensions (1-10 each),
    identifies specific strengths/weaknesses, and provides an overall
    accept/reject recommendation with confidence.

    Returns a dict with dimensional scores, issues, and decision.
    """
    if not pdf_path.exists():
        return {}

    # Use source-based review since not all models support vision
    tex_path = pdf_path.with_suffix(".tex")
    if not tex_path.exists():
        return {}

    tex_content = tex_path.read_text(encoding="utf-8")[:12000]

    review_prompt = (
        "You are a senior Area Chair at a top AI conference (NeurIPS/ICML/ICLR) "
        "reviewing a paper submission. Provide a rigorous, structured review.\n\n"
        f"PAPER TOPIC: {topic}\n\n"
        f"LaTeX source:\n```latex\n{tex_content}\n```\n\n"
        "REVIEW INSTRUCTIONS:\n"
        "Score each dimension 1-10 (1=unacceptable, 5=borderline, 8=strong accept, "
        "10=best paper candidate). Be critical but fair.\n\n"
        "DIMENSIONS:\n"
        "1. SOUNDNESS: Are claims well-supported? Is methodology correct? "
        "Are there logical gaps or unsupported claims?\n"
        "2. PRESENTATION: Is the writing clear, flowing, and professional? "
        "Are there grammar errors, bullet lists in prose sections, or "
        "boilerplate phrases? Is it free of AI-generated slop?\n"
        "3. CONTRIBUTION: Is the contribution significant? Does it advance "
        "the field beyond incremental improvement?\n"
        "4. ORIGINALITY: Is the approach novel? Does it differentiate clearly "
        "from prior work?\n"
        "5. CLARITY: Are the method and results easy to understand? Are figures "
        "and tables well-designed with descriptive captions?\n"
        "6. SIGNIFICANCE: Would the community benefit from this work? Does it "
        "open new research directions?\n"
        "7. REPRODUCIBILITY: Are experimental details sufficient to reproduce "
        "results? Are hyperparameters, datasets, and metrics clearly stated?\n\n"
        "Also evaluate:\n"
        "- Are all figures referenced in the text?\n"
        "- Are tables properly formatted (booktabs style, no vertical rules)?\n"
        "- Does the related work critically compare, not just list papers?\n"
        "- Are statistical measures (std, CI, multiple seeds) reported?\n"
        "- Is there a clear limitations section?\n\n"
        "Return a JSON object:\n"
        "{\n"
        '  "soundness": N,\n'
        '  "presentation": N,\n'
        '  "contribution": N,\n'
        '  "originality": N,\n'
        '  "clarity": N,\n'
        '  "significance": N,\n'
        '  "reproducibility": N,\n'
        '  "overall_score": N,\n'
        '  "confidence": N,\n'
        '  "decision": "accept" or "reject",\n'
        '  "strengths": ["strength1", "strength2", ...],\n'
        '  "weaknesses": ["weakness1", "weakness2", ...],\n'
        '  "critical_issues": ["issue requiring revision", ...],\n'
        '  "minor_issues": ["formatting/typo issues", ...],\n'
        '  "summary": "2-3 sentence overall assessment"\n'
        "}\n"
    )

    try:
        resp = llm.chat(
            messages=[{"role": "user", "content": review_prompt}],
            system=(
                "You are a meticulous, critical academic reviewer. "
                "You have reviewed 100+ papers at top venues. "
                "Score honestly — most papers deserve 4-6, not 7-9. "
                "Flag any sign of AI-generated boilerplate."
            ),
        )
        review_data = _safe_json_loads(resp.content, {})
        if isinstance(review_data, dict) and "overall_score" in review_data:
            # Compute weighted aggregate if individual scores present
            dim_scores = {
                k: review_data.get(k, 0)
                for k in (
                    "soundness", "presentation", "contribution",
                    "originality", "clarity", "significance",
                    "reproducibility",
                )
            }
            valid = {k: v for k, v in dim_scores.items() if isinstance(v, (int, float)) and v > 0}
            if valid:
                review_data["mean_score"] = round(sum(valid.values()) / len(valid), 2)
            return review_data
    except Exception as exc:  # noqa: BLE001
        logger.debug("PDF review LLM call failed: %s", exc)

    return {}


def _check_ablation_effectiveness(
    exp_summary: dict[str, Any],
    threshold: float = 0.02,
) -> list[str]:
    """P7: Check if ablation results are within *threshold* of baseline.

    Returns a list of warning strings for ineffective ablations.
    Threshold tightened from 5% to 2% (Improvement C) — ablations with
    < 2% relative difference AND < 1pp absolute difference are flagged
    as TRIVIAL.
    """
    warnings: list[str] = []
    cond_summaries = exp_summary.get("condition_summaries", {})
    if not isinstance(cond_summaries, dict) or not cond_summaries:
        return warnings

    # Find baseline/control condition
    baseline_name = None
    baseline_mean: Decimal | None = None
    decimal_threshold = Decimal(str(threshold))
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        name_lower = name.lower()
        if any(tag in name_lower for tag in ("baseline", "control", "vanilla", "standard")):
            metrics = data.get("metrics") or {}
            if not isinstance(metrics, dict):
                metrics = {}
            # Use the first metric that has a _mean suffix or the first available
            for mk, mv in metrics.items():
                if mk.endswith("_mean"):
                    baseline_name = name
                    baseline_mean = _authority_decimal(mv)
                    break
            if baseline_mean is None:
                for mk, mv in metrics.items():
                    try:
                        baseline_name = name
                        baseline_mean = _authority_decimal(mv)
                        if baseline_mean is None:
                            continue
                        break
                    except (TypeError, ValueError):
                        continue
            if baseline_name:
                break

    if baseline_name is None or baseline_mean is None:
        return warnings

    # Check each ablation condition
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        name_lower = name.lower()
        if name == baseline_name:
            continue
        if not any(tag in name_lower for tag in ("ablation", "no_", "without", "reduced")):
            continue
        metrics = data.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        for mk, mv in metrics.items():
            if not mk.endswith("_mean"):
                continue
            abl_val = _authority_decimal(mv)
            if abl_val is None:
                continue
            if baseline_mean != 0:
                rel_diff = abs(abl_val - baseline_mean) / abs(baseline_mean)
            else:
                rel_diff = abs(abl_val - baseline_mean)
            abs_diff = abs(abl_val - baseline_mean)
            # Improvement C: Tighter check — both relative < threshold
            # AND absolute < 1pp → TRIVIAL
            if rel_diff < decimal_threshold and abs_diff < Decimal(1):
                warnings.append(
                    f"TRIVIAL: Ablation '{name}' {mk}={abl_val:.4f} is within "
                    f"{rel_diff:.1%} (abs {abs_diff:.4f}pp) of baseline "
                    f"'{baseline_name}' {mk}={baseline_mean:.4f} — "
                    f"ablation is ineffective"
                )
            elif rel_diff < decimal_threshold:
                warnings.append(
                    f"Ablation '{name}' {mk}={abl_val:.4f} is within "
                    f"{rel_diff:.1%} of baseline '{baseline_name}' "
                    f"{mk}={baseline_mean:.4f} — ablation may be ineffective"
                )
            break  # Only check the first _mean metric per condition

    # Improvement C: Prepend CRITICAL summary if >50% trivial
    trivial_count = sum(1 for w in warnings if w.startswith("TRIVIAL:"))
    if trivial_count > 0 and len(warnings) > 0 and trivial_count / len(warnings) > 0.5:
        warnings.insert(0, (
            f"CRITICAL: {trivial_count}/{len(warnings)} ablations are trivially "
            f"similar to baseline (<{threshold:.0%} relative, <1pp absolute). "
            f"The ablation design is likely broken — components are not effectively removed."
        ))

    return warnings


def _detect_result_contradictions(
    exp_summary: dict[str, Any],
    metric_direction: str = "maximize",
) -> list[str]:
    """P10: Detect contradictions in experiment results before paper writing.

    Returns a list of advisory strings to inject into paper writing prompt.
    """
    advisories: list[str] = []
    cond_summaries = exp_summary.get("condition_summaries", {})
    if not isinstance(cond_summaries, dict) or not cond_summaries:
        return advisories

    # Collect primary metric means per condition
    means: dict[str, Decimal] = {}
    for name, data in cond_summaries.items():
        if not isinstance(data, dict):
            continue
        metrics = data.get("metrics", {})
        for mk, mv in metrics.items():
            if mk.endswith("_mean"):
                numeric = _authority_decimal(mv)
                if numeric is not None:
                    means[name] = numeric
                break

    if len(means) < 2:
        return advisories

    # Check 1: All methods within noise margin (2% relative spread)
    vals = list(means.values())
    val_range = max(vals) - min(vals)
    val_mean = sum(vals, Decimal(0)) / Decimal(len(vals))
    if val_mean != 0 and (val_range / abs(val_mean)) < Decimal("0.02"):
        advisories.append(
            "NULL RESULT: All methods produce nearly identical primary metric values "
            f"(range={val_range:.4f}, mean={val_mean:.4f}). Frame this as a null result — "
            "the methods are statistically indistinguishable. Do NOT claim any method "
            "is superior. Discuss possible explanations (task too easy/hard, metric "
            "insensitive, insufficient differentiation in methods)."
        )

    # Check 2: Control/simple baseline outperforms proposed method
    # BUG-P1: Respect metric_direction — "higher is better" vs "lower is better"
    _maximize = metric_direction == "maximize"
    baseline_val = None
    baseline_name = None
    proposed_val = None
    proposed_name = None
    for name, val in means.items():
        name_lower = name.lower()
        if any(tag in name_lower for tag in ("baseline", "control", "random", "vanilla")):
            if baseline_val is None or (_maximize and val > baseline_val) or (not _maximize and val < baseline_val):
                baseline_val = val
                baseline_name = name
        elif any(tag in name_lower for tag in ("proposed", "our", "novel", "method")):
            if proposed_val is None or (_maximize and val > proposed_val) or (not _maximize and val < proposed_val):
                proposed_val = val
                proposed_name = name

    if baseline_val is not None and proposed_val is not None:
        _baseline_wins = (baseline_val > proposed_val) if _maximize else (baseline_val < proposed_val)
        if _baseline_wins:
            advisories.append(
                f"NEGATIVE RESULT: Baseline '{baseline_name}' ({baseline_val:.4f}) "
                f"outperforms proposed method '{proposed_name}' ({proposed_val:.4f}). "
                "This is a NEGATIVE result. Do NOT claim the proposed method is superior. "
                "Frame as 'An Empirical Study of...' or 'When X Falls Short'. "
                "Discuss why the baseline won and what this implies for future work."
            )

    return advisories


def _validate_stage17_manuscript_structure(
    draft: str,
    *,
    stage_dir: Path,
) -> dict[str, Any]:
    """Write a deterministic structure report for the final Stage 17 draft."""

    try:
        document = parse_manuscript(draft, strict=False)
    except ManuscriptStructureError as exc:
        document = None
        structure_issues = exc.issues
    else:
        structure_issues = document.structure_issues
    issues = [
        {
            "code": issue.code,
            "message": issue.message,
            "ordinal": issue.ordinal,
        }
        for issue in structure_issues
    ]
    section_count = len(document.sections) if document is not None else 0
    if not section_count:
        issues.append(
            {
                "code": "manuscript_sections_empty",
                "message": "paper draft contains no CommonMark headings",
                "ordinal": None,
            }
        )
    report = {
        "schema_version": 1,
        "valid": not issues,
        "source_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "section_count": section_count,
        "issues": issues,
    }
    (stage_dir / "paper_structure_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def _execute_paper_draft(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    for owned_name in (
        "paper_draft.md",
        "paper_draft_invalid.md",
        "paper_structure_report.json",
        "section_generation_report.json",
        "citation_closure_report.json",
        "experiment_fact_closure_report.json",
        "experiment_fact_closure_invalid.json",
        "experiment_fact_closure_initial.json",
        "experiment_fact_closure_after.json",
        "experiment_fact_repair_log.json",
        "citation_heading_repair_log.json",
        "draft_quality.json",
        "paper_meta.json",
        "quality_warnings.json",
        "references_preverified.bib",
    ):
        (stage_dir / owned_name).unlink(missing_ok=True)
    try:
        evidence = load_canonical_experiment_evidence(run_dir)
        canonical_fact_sheet = build_canonical_fact_sheet(evidence)
        outline = _load_bound_stage16_outline(run_dir, evidence)
        effective_citation_policy = load_effective_citation_policy(run_dir, config)
    except (
        CanonicalExperimentEvidenceError,
        CFSIntegrityError,
        CitationPolicyContractError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Effective citation policy is invalid: {exc}",
            decision="retry",
        )
    citation_target = int(
        effective_citation_policy["effective_target_unique_sources"]
    )
    preamble = _build_context_preamble(
        config,
        run_dir,
        canonical_evidence=evidence,
        include_goal=True,
        include_hypotheses=True,
        include_analysis=canonical_fact_sheet is None,
        include_experiment_data=canonical_fact_sheet is None,
    )

    exp_summary_text = evidence.summary_bytes.decode("utf-8")
    exp_summary = _plain_evidence_value(evidence.summary)
    exp_metrics_instruction = ""
    has_real_metrics = False
    from researchclaw.pipeline.verified_registry import VerifiedRegistry
    _verified_registry = VerifiedRegistry.from_experiment(
        exp_summary,
        metric_direction=config.experiment.metric_direction,
    )
    if exp_summary.get("metrics_summary"):
        has_real_metrics = True
        exp_metrics_instruction = (
            "\n\nIMPORTANT: Use only the accessor-selected experiment results. "
            "All experiment numbers must be present in the canonical evidence bundle.\n"
        )

    # Collect raw experiment stdout metrics as hard constraint for the paper
    raw_metrics_block, _has_parsed_metrics = _collect_raw_experiment_metrics(evidence)
    if raw_metrics_block:
        # BUG-23: Raw stdout alone is not sufficient — require either
        # metrics_summary data, parsed metrics from run JSONs,
        # OR at least 3 condition= patterns in raw block
        _has_condition_pattern = len(re.findall(
            r"condition[=:]", raw_metrics_block, re.IGNORECASE
        )) >= 3
        if has_real_metrics or _has_parsed_metrics or _has_condition_pattern:
            has_real_metrics = True
        exp_metrics_instruction += raw_metrics_block

    grounded_metric_whitelist = _collect_grounded_metric_whitelist(evidence)
    if grounded_metric_whitelist:
        has_real_metrics = True
        exp_metrics_instruction += grounded_metric_whitelist

    # R18-1 + R19-6: Inject paired statistical comparisons AND condition summaries
    if exp_summary:
        exp_summary_parsed = exp_summary
        if isinstance(exp_summary_parsed, dict):
            # R19-6: Inject experiment scale header so LLM knows the data richness
            _total_conds = exp_summary_parsed.get("total_conditions")
            _total_mkeys = exp_summary_parsed.get("total_metric_keys")
            if _total_conds or _total_mkeys:
                scale_block = "\n\n## EXPERIMENT SCALE\n"
                if _total_conds:
                    scale_block += f"- Total conditions tested: {_total_conds}\n"
                if _total_mkeys:
                    scale_block += f"- Total metric keys collected: {_total_mkeys}\n"
                scale_block += (
                    "- This is a MULTI-SEED experiment. Report mean +/- std across seeds.\n"
                    "- Do NOT describe results as 'single run' or 'preliminary'.\n"
                )
                exp_metrics_instruction += scale_block

            # Improvement B: Inject seed insufficiency warnings
            _seed_warns = exp_summary_parsed.get("seed_insufficiency_warnings", [])
            if _seed_warns:
                _sw_block = (
                    "\n\n## SEED INSUFFICIENCY WARNINGS\n"
                    "Some conditions were run with fewer than 3 seeds. "
                    "Results for these conditions MUST be footnoted as preliminary.\n"
                    "All tables MUST show mean ± std format. Single-run values "
                    "MUST be footnoted with '†single seed — interpret with caution'.\n"
                )
                for _sw in _seed_warns:
                    _sw_block += f"- {_sw}\n"
                exp_metrics_instruction += _sw_block

            # R19-6 + R33: Inject condition summaries with CIs
            cond_summaries = exp_summary_parsed.get("condition_summaries", {})
            if isinstance(cond_summaries, dict) and cond_summaries:
                cond_block = "\n\n## PER-CONDITION SUMMARY (use in Results tables)\n"
                for cname, cdata in sorted(cond_summaries.items()):
                    cond_block += f"\n### {cname}\n"
                    if not isinstance(cdata, dict):
                        continue
                    sr = cdata.get("success_rate")
                    if sr is not None:
                        cond_block += (
                            f"- Success rate: {_authority_number_text(sr)}\n"
                        )
                    ns = cdata.get("n_seeds") or cdata.get("n_seed_metrics")
                    if ns:
                        cond_block += f"- Seeds: {ns}\n"
                    ci_lo = cdata.get("ci95_low")
                    ci_hi = cdata.get("ci95_high")
                    if ci_lo is not None and ci_hi is not None:
                        cond_block += (
                            "- Bootstrap 95% CI: "
                            f"[{_authority_number_text(ci_lo)}, "
                            f"{_authority_number_text(ci_hi)}]\n"
                        )
                    cm = cdata.get("metrics") or {}
                    if isinstance(cm, dict) and cm:
                        for mk, mv in sorted(cm.items()):
                            cond_block += f"- {mk}: {_authority_number_text(mv)}\n"
                exp_metrics_instruction += cond_block

            # R18-1: Inject paired statistical comparisons
            paired = exp_summary_parsed.get("paired_comparisons", [])
            if paired:
                paired_block = "\n\n## PAIRED STATISTICAL COMPARISONS (use these in Results)\n"
                paired_block += f"Total: {len(paired)} paired tests computed.\n"
                for pc in paired:
                    if not isinstance(pc, dict):
                        continue
                    method = pc.get("method", "?")
                    baseline = pc.get("baseline", "?")
                    regime = pc.get("regime", "all")
                    md = pc.get("mean_diff", "?")
                    sd = pc.get("std_diff", "?")
                    ts = pc.get("t_stat", "?")
                    pv = pc.get("p_value", "?")
                    ci_lo = pc.get("ci95_low")
                    ci_hi = pc.get("ci95_high")
                    ci_str = ""
                    if ci_lo is not None and ci_hi is not None:
                        ci_str = (
                            ", 95% CI "
                            f"[{_authority_number_text(ci_lo)}, "
                            f"{_authority_number_text(ci_hi)}]"
                        )
                    paired_block += (
                        f"- {method} vs {baseline} (regime={regime}): "
                        f"mean_diff={md}, std_diff={sd}, "
                        f"t={ts}, p={pv}{ci_str}\n"
                    )
                exp_metrics_instruction += paired_block

            # R24: Method naming map — translate generic condition labels
            _cond_names = list(cond_summaries.keys()) if isinstance(cond_summaries, dict) and cond_summaries else []
            if _cond_names:
                naming_block = (
                    "\n\n## METHOD NAMING (CRITICAL — do NOT use generic labels in the paper)\n"
                    "The condition labels below come from the experiment code. In the paper, "
                    "you MUST use DESCRIPTIVE algorithm names, not generic labels.\n"
                    "- If a condition name is already descriptive (e.g., 'random_search', "
                    "'bayesian_optimization', 'ppo_policy'), use it directly as a proper name.\n"
                    "- If a condition name is generic (e.g., 'baseline_1', 'method_variant_1'), "
                    "you MUST infer the algorithm from the experiment code/context and use the "
                    "real algorithm name (e.g., 'Random Search', 'Bayesian Optimization', "
                    "'PPO', 'Curiosity-Driven RL').\n"
                    "- NEVER write `baseline_1` or `method_variant_1` in the paper text.\n"
                    f"- Conditions to name: {_cond_names}\n"
                )
                exp_metrics_instruction += naming_block

            # IMP-8: Inject broken ablation warnings
            abl_warnings = exp_summary_parsed.get("ablation_warnings", [])
            if abl_warnings:
                broken_block = (
                    "\n\n## BROKEN ABLATIONS (DO NOT discuss as valid results)\n"
                    "The following ablation conditions produced IDENTICAL outputs, "
                    "indicating implementation bugs. Do NOT present their differences "
                    "as findings. Mention them ONLY in a 'Limitations' sub-section "
                    "as known implementation issues:\n"
                )
                for _aw in abl_warnings:
                    broken_block += f"- {_aw}\n"
                broken_block += (
                    "\nIf you reference these conditions, state explicitly: "
                    "'Due to an implementation defect, conditions X and Y produced "
                    "identical outputs; their comparison is therefore uninformative.'\n"
                )
                exp_metrics_instruction += broken_block

            # R25: Statistical table format requirement
            if paired:
                stat_table_block = (
                    "\n\n## STATISTICAL TABLE REQUIREMENT (MANDATORY in Results section)\n"
                    "The Results section MUST include a statistical comparison table with columns:\n"
                    "| Comparison | Mean Diff | Std Diff | t-statistic | p-value | Significance |\n"
                    "Use the PAIRED STATISTICAL COMPARISONS data above to fill this table.\n"
                    "Mark significance: *** (p<0.001), ** (p<0.01), * (p<0.05), n.s.\n"
                    "This is non-negotiable — a top-venue paper MUST have statistical tests.\n"
                )
                exp_metrics_instruction += stat_table_block

            # R26: Metric definition requirement
            exp_metrics_instruction += (
                "\n\n## METRIC DEFINITIONS (MANDATORY in Experiments section)\n"
                "The Experiments section MUST define each metric:\n"
                "- **Primary metric**: what it measures, how it is computed, range, direction "
                "(higher/lower is better), and units if applicable.\n"
                "- **Secondary metric**: same details.\n"
                "- For time-to-event metrics: explain the horizon, what constitutes success, "
                "and how failures are handled (e.g., set to max horizon).\n"
                "- These definitions MUST appear BEFORE any results tables.\n"
            )

            # R27: Multi-seed framing enforcement
            _any_seeds = any(
                (cond_summaries.get(c) or {}).get("n_seed_metrics", 0) > 1
                for c in _cond_names
            ) if _cond_names else False
            if _any_seeds:
                exp_metrics_instruction += (
                    "\n\n## MULTI-SEED EXPERIMENT FRAMING (CRITICAL)\n"
                    "This experiment uses MULTIPLE independent random seeds per condition.\n"
                    "- Report mean +/- std (or SE) for all metrics.\n"
                    "- NEVER describe this as 'a single run' or '1 benchmark-artifact run'.\n"
                    "- Frame as: 'We evaluate each method across N seeds per regime.'\n"
                    "- The seed-level data IS the evidence base — it is NOT a single observation.\n"
                    "- Include per-regime breakdowns (easy vs hard) as separate rows in tables.\n"
                )

    # BUG-003: Inject actual evaluated datasets as a hard constraint
    if exp_summary:
        _ds_parsed = exp_summary
        if isinstance(_ds_parsed, dict):
            _datasets: set[str] = set()
            # Extract from condition names (often contain dataset info)
            for _cname in (_ds_parsed.get("condition_summaries") or {}).keys():
                _datasets.add(str(_cname))
            # Extract from explicit "datasets" field if present
            for _ds in (_ds_parsed.get("datasets") or []):
                if isinstance(_ds, str):
                    _datasets.add(_ds)
            # Extract from "benchmark" or "dataset" fields
            for _key in ("benchmark", "dataset", "dataset_name"):
                _dv = _ds_parsed.get(_key)
                if isinstance(_dv, str) and _dv:
                    _datasets.add(_dv)
            if _datasets:
                exp_metrics_instruction += (
                    "\n\n## ACTUAL EVALUATED DATASETS (HARD CONSTRAINT)\n"
                    "The following datasets/conditions were ACTUALLY tested in experiments:\n"
                    + "".join(f"- {d}\n" for d in sorted(_datasets))
                    + "\nCRITICAL: Do NOT claim evaluation on any dataset not listed above.\n"
                    "Do NOT fabricate results for datasets you did not run experiments on.\n"
                    "If you reference other datasets, clearly state they are 'not evaluated "
                    "in this work' or are 'left for future work'.\n"
                )

    # P7: Ablation effectiveness check
    if exp_summary:
        _exp_parsed_p7 = exp_summary
        if isinstance(_exp_parsed_p7, dict):
            _abl_warnings = _check_ablation_effectiveness(_exp_parsed_p7)
            if _abl_warnings:
                _abl_block = (
                    "\n\n## ABLATION EFFECTIVENESS WARNINGS\n"
                    "The following ablations showed minimal effect (within 5% of baseline). "
                    "Discuss this honestly — it may indicate the ablated component is not "
                    "important, or the ablation was not properly implemented:\n"
                )
                for _aw in _abl_warnings:
                    _abl_block += f"- {_aw}\n"
                exp_metrics_instruction += _abl_block
                logger.warning("P7: Ablation effectiveness warnings: %s", _abl_warnings)

    # P10: Contradiction detection
    if exp_summary:
        _exp_parsed_p10 = exp_summary
        if isinstance(_exp_parsed_p10, dict):
            _contradictions = _detect_result_contradictions(
                _exp_parsed_p10, metric_direction=config.experiment.metric_direction
            )
            if _contradictions:
                _contra_block = (
                    "\n\n## RESULT INTERPRETATION ADVISORIES (CRITICAL — read before writing)\n"
                )
                for _ca in _contradictions:
                    _contra_block += f"- {_ca}\n"
                exp_metrics_instruction += _contra_block
                logger.warning("P10: Contradiction advisories: %s", _contradictions)

    _is_lit_first = _topic_is_literature_first(config)

    # R4-2: HARD BLOCK — refuse to write paper with no real data (ML/empirical domains)
    # For non-empirical domains (math proofs, theoretical economics), allow proceeding
    _domain_id, _domain_name, _domain_venues = _detect_domain(
        config.research.topic, config.research.domains
    )
    _empirical_domains = {"ml", "engineering", "biology", "chemistry"}
    if not has_real_metrics and not _is_lit_first:
        if _domain_id in _empirical_domains:
            logger.error(
                "BLOCKED: Cannot write paper — experiment produced NO metrics. "
                "The pipeline will not fabricate results."
            )
            (stage_dir / "paper_draft.md").write_text(
                "# Paper Draft Blocked\n\n"
                "**Reason**: Experiment stage produced no metrics (status: failed/timeout). "
                "Cannot write a paper without real experimental data.\n\n"
                "**Action Required**: Fix experiment execution or increase time_budget_sec.",
                encoding="utf-8",
            )
            (stage_dir / "paper_meta.json").write_text(
                json.dumps(
                    {
                        "outcome": "blocked_no_metrics",
                        "detected_by": (
                            "Stage 12/13 runs produced no real metrics "
                            "(has_real_metrics is False)"
                        ),
                        "domain_id": _domain_id,
                        "is_literature_first_topic": False,
                        "note": (
                            "Paper drafting refuses to fabricate results when "
                            "the experiment produced no metrics."
                        ),
                        "action_required": (
                            "Fix experiment execution or increase "
                            "time_budget_sec; re-run from "
                            "--from-stage EXPERIMENT_RUN."
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return StageResult(
                stage=Stage.PAPER_DRAFT,
                status=StageStatus.PAUSED,
                artifacts=("paper_draft.md", "paper_meta.json"),
                error=(
                    "Paper draft blocked: experiment produced no real metrics. "
                    "Fix execution or increase time budget."
                ),
                evidence_refs=(
                    "stage-17/paper_draft.md",
                    "stage-17/paper_meta.json",
                ),
                decision="blocked_no_metrics",
            )
        else:
            logger.warning(
                "No experiment metrics found, but domain '%s' may be non-empirical "
                "(theoretical/mathematical). Proceeding with paper draft.",
                _domain_name,
            )

    try:
        final_citation_plan = load_final_citation_plan(run_dir, config)
        # An empty final plan grants no citation authority, so no card bytes are
        # needed to render the corresponding no-citation heading instructions.
        citation_cards = (
            load_validated_cards(run_dir, config)
            if final_citation_plan["claims"]
            else {}
        )
    except CitationPlanContractError as exc:
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Final citation plan is invalid: {exc}",
            decision="retry",
        )
    citation_repair_claims = tuple(
        {
            "section": str(claim["section_path"][0]),
            "claim_text": str(claim["claim_text"]),
            "cite_key": str(claim["planned_citations"][0]["cite_key"]),
        }
        for claim in final_citation_plan["claims"]
    )
    heading_names = (
        ("Abstract", "Introduction", "Model / Theoretical Framework",
         "Phenomenology / Computational Setup", "Results", "Discussion", "Conclusions")
        if _prompt_bank_domain_from_config(config) == "hep_ph" else
        ("Abstract", "Introduction", "Related Work", "Method", "Experiments",
         "Results", "Discussion", "Limitations", "Conclusion")
    )
    heading_citation_instructions = (
        build_heading_citation_writer_instructions_from_authority(
            final_citation_plan, citation_cards, heading_names=heading_names
        )
    )

    # R11-5: Experiment quality minimum threshold before paper writing
    # Parse analysis.md for quality rating and condition completeness
    analysis_text = evidence.analysis_text
    _quality_warnings: list[str] = []

    # Check 1: Was the analysis quality rating very low?
    import re as _re_q
    _rating_match = _re_q.search(
        r"(?:quality\s+rating|result\s+quality)[:\s]*\**(\d+)\s*/\s*10",
        analysis_text,
        _re_q.IGNORECASE,
    )
    if _rating_match:
        _analysis_rating = int(_rating_match.group(1))
        if _analysis_rating <= 3:
            _quality_warnings.append(
                f"Analysis rated experiment quality {_analysis_rating}/10"
            )
        # BUG-23: If quality rating is ≤ 2, force has_real_metrics = False
        # to prevent fabricated results even if stdout had stray numbers.
        # R5-BUG-05: Skip override when _has_parsed_metrics is True — the
        # analysis.md may be stale (from pre-refinement Stage 14) while
        # Stage 13 refinement produced real parsed metrics.
        if _analysis_rating <= 2 and has_real_metrics and not _has_parsed_metrics:
            logger.warning(
                "BUG-23 guard: Analysis quality %d/10 \u2264 2 — "
                "overriding has_real_metrics to False (experiment likely failed)",
                _analysis_rating,
            )
            has_real_metrics = False

    # Check 2: Are baselines missing?
    _analysis_lower = analysis_text.lower()
    if "no" in _analysis_lower and "baseline" in _analysis_lower:
        if any(phrase in _analysis_lower for phrase in [
            "no baseline", "no bo", "no random", "baselines are missing",
            "missing baselines", "baseline coverage is missing",
        ]):
            _quality_warnings.append("Baselines appear to be missing from results")

    # Check 3: Is the metric undefined?
    if any(phrase in _analysis_lower for phrase in [
        "metric is undefined", "primary_metric is undefined",
        "undefined metric", "metric undefined",
    ]):
        _quality_warnings.append("Primary metric is undefined (direction/units/formula unknown)")

    # Check 4: Very few conditions completed
    _condition_count = len(_re_q.findall(
        r"condition[=:\s]+\w+.*?(?:mean|primary_metric)",
        raw_metrics_block or "",
        _re_q.IGNORECASE,
    ))

    if _quality_warnings:
        _warning_block = "\n".join(f"  - {w}" for w in _quality_warnings)
        logger.warning(
            "Stage 17: Experiment quality concerns detected before paper writing:\n%s",
            _warning_block,
        )
        # Inject quality warnings into the paper writing prompt so the LLM
        # writes an appropriately hedged paper
        exp_metrics_instruction += (
            "\n\n## EXPERIMENT QUALITY WARNINGS (address these honestly in the paper)\n"
            + "\n".join(f"- {w}" for w in _quality_warnings)
            + "\n\nBecause of these issues, the paper MUST:\n"
            "- Use hedged language ('preliminary', 'pilot', 'initial exploration')\n"
            "- NOT claim definitive comparisons between methods\n"
            "- Dedicate a substantial Limitations section to these gaps\n"
            "- Frame the contribution as methodology/framework, not empirical findings\n"
        )
        # Save warnings for tracking
        (stage_dir / "quality_warnings.json").write_text(
            json.dumps(_quality_warnings, indent=2), encoding="utf-8"
        )

    # Phase 1: Inject pre-built results tables from VerifiedRegistry
    if _verified_registry is not None:
        try:
            from researchclaw.templates.results_table_builder import (
                build_results_tables,
                build_condition_whitelist,
            )
            _prebuilt_tables = build_results_tables(
                _verified_registry,
                metric_direction=_verified_registry.metric_direction,
            )
            _condition_whitelist = build_condition_whitelist(_verified_registry)
            if _prebuilt_tables:
                _tables_block = "\n\n".join(t.latex_code for t in _prebuilt_tables)
                exp_metrics_instruction += (
                    "\n\n## PRE-BUILT RESULTS TABLES (MANDATORY — copy verbatim)\n"
                    "The tables below were AUTO-GENERATED from verified experiment data.\n"
                    "You MUST include these tables in the Results section EXACTLY as shown.\n"
                    "Do NOT modify any numbers. Do NOT add rows with fabricated data.\n"
                    "You MAY adjust formatting (bold, alignment) but NOT numerical values.\n\n"
                    + _tables_block
                )
                logger.info("Stage 17: Injected pre-built results tables into prompt")
            if _condition_whitelist:
                exp_metrics_instruction += (
                    "\n\n## VERIFIED CONDITIONS (ONLY mention these in the paper)\n"
                    + _condition_whitelist
                    + "\nDo NOT discuss conditions not in this list. Do NOT invent new conditions.\n"
                )
        except Exception as _tb_exc:
            logger.warning("Stage 17: Failed to build pre-built tables: %s", _tb_exc)

    # R4-2: Anti-fabrication data integrity instruction
    exp_metrics_instruction += (
        "\n\n## CRITICAL: Data Integrity Rules\n"
        "- You may ONLY report numbers that appear in the experiment data above\n"
        "- If the experiment data is incomplete (fewer conditions than planned), report\n"
        "  ONLY the conditions that were actually run\n"
        "- Do NOT extrapolate, interpolate, or 'fill in' missing cells in tables\n"
        "- Do NOT invent confidence intervals, p-values, or statistical tests unless\n"
        "  the actual data supports them\n"
        "- If only N conditions completed, simply report results for those N conditions\n"
        "  without repeating apologies or disclaimers about missing conditions\n"
        "- Any table cell without real data must show '\u2014' (not a plausible number)\n"
        "- FORBIDDEN: generating numbers that 'look right' based on your training data\n"
    )

    # IMP-6 + FA: Inject chart references into paper draft prompt
    # Candidate policy v1 authorizes a deterministic empty figure state. No
    # chart context is inferred from diagnostic or versioned directories.
    _hp_table = ""
    _hp = evidence.structured_results.get("hyperparameters")
    if isinstance(_hp, Mapping) and _hp:
        _hp_table = "\n\n## HYPERPARAMETERS (include as a table in the Method section)\n"
        _hp_table += "| Hyperparameter | Value |\n|---|---|\n"
        for _hk, _hv in sorted(_hp.items()):
            _hp_table += f"| {_hk} | {_hv} |\n"
        _hp_table += "\nInclude only these accessor-selected hyperparameters.\n"
    if _hp_table:
        exp_metrics_instruction += _hp_table

    # E5: citation_instruction was built from the sealed final plan and exact
    # retained excerpts. The free Stage 4 catalog and filtered bibliography
    # are intentionally outside the writer's authority surface.
    # Literature-first mode instruction for survey/review topics
    if _is_lit_first:
        exp_metrics_instruction += (
            "\n\n## LITERATURE-FIRST MODE\n"
            "This paper is a **survey / review / literature-first study**.\n"
            "- The contribution is the synthesis, taxonomy, and critical analysis of existing work.\n"
            "- Do NOT claim novel experimental results. Instead, summarize and compare findings\n"
            "  from the collected literature.\n"
            "- Structure the paper around themes, taxonomies, or chronological developments.\n"
            "- Include a comprehensive Related Work / Literature Review as the main body.\n"
            "- Tables should compare methods, datasets, and reported metrics FROM the literature.\n"
            "- The Conclusion should identify open problems and future directions.\n"
        )
        logger.info("Stage 17: Literature-first mode enabled for survey/review topic")

    # --- Venue label derived from the active prompt bank ---
    # Domain-specific venue prose now lives in the prompt bank itself; here we
    # only need the short label and the HEP flag for paper section structuring.
    if llm is not None:
        _pm = prompts or PromptManager()
        topic_constraint = _pm.block("topic_constraint", topic=config.research.topic)
        _paper_is_hep = _pm.domain == "hep_ph"
        _paper_venue_label = "JHEP" if _paper_is_hep else "NeurIPS/ICML"
        _paper_venue_guidance = ""

        # --- Section-by-section writing (3 calls) for conference-grade depth ---
        try:
            if canonical_fact_sheet is not None:
                exp_metrics_instruction = ""
            draft = _write_paper_sections(
                llm=llm,
                pm=_pm,
                run_dir=run_dir,
                preamble=preamble,
                topic_constraint=topic_constraint,
                exp_metrics_instruction=exp_metrics_instruction,
                citation_instruction="",
                outline=outline,
                model_name=config.llm.primary_model,
                venue_label=_paper_venue_label,
                venue_guidance=_paper_venue_guidance,
                is_hep=_paper_is_hep,
                stage_dir=stage_dir,
                citation_repair_claims=citation_repair_claims,
                heading_citation_instructions=heading_citation_instructions,
                canonical_fact_sheet=canonical_fact_sheet,
            )
        except PaperSectionContractError as exc:
            (stage_dir / "paper_draft_invalid.md").write_text(
                exc.text, encoding="utf-8"
            )
            _validate_stage17_manuscript_structure(exc.text, stage_dir=stage_dir)
            return StageResult(
                stage=Stage.PAPER_DRAFT,
                status=StageStatus.FAILED,
                artifacts=(
                    "paper_draft_invalid.md",
                    "paper_structure_report.json",
                    "section_generation_report.json",
                ),
                error=str(exc),
                evidence_refs=(
                    "stage-17/paper_draft_invalid.md",
                    "stage-17/paper_structure_report.json",
                    "stage-17/section_generation_report.json",
                ),
                decision="retry",
            )

        # R7: Strip LLM-generated References section — it often fabricates arXiv IDs.
        import re as _re_r7
        ref_pattern = _re_r7.compile(
            r'^(#{1,2}\s*References.*)', _re_r7.MULTILINE | _re_r7.DOTALL
        )
        ref_match = ref_pattern.search(draft)
        if ref_match:
            draft = draft[:ref_match.start()].rstrip()
            logger.info("Stage 17: Stripped LLM-generated References section (R7 fix)")
    else:
        # Build template with real data if available
        results_section = "Template results summary."
        if exp_summary:
            if isinstance(exp_summary, dict) and exp_summary.get("metrics_summary"):
                lines = ["Experiment results:"]
                for mk, mv in exp_summary["metrics_summary"].items():
                    if isinstance(mv, dict):
                        lines.append(
                            f"- {mk}: mean={mv.get('mean')}, min={mv.get('min')}, "
                            f"max={mv.get('max')}, n={mv.get('count')}"
                        )
                results_section = "\n".join(lines)

        draft = f"""# Draft Title

## Abstract
Template draft abstract.

## Introduction
Template introduction for {config.research.topic}.

## Related Work
Template related work.

## Method
Template method description.

## Experiments
Template experimental setup.

## Results
{results_section}

## Limitations
Template limitations.

## Conclusion
Template conclusion.

## References
Template references.

Generated: {_utcnow_iso()}
"""
        _persist_section_generation_report(stage_dir, [])
    # Validate draft quality (section balance + bullet density)
    _validate_draft_quality(
        draft, stage_dir=stage_dir, citation_target=citation_target
    )

    # --- HITL: Read human guidance for paper draft ---
    guidance_file = stage_dir / "hitl_guidance.md"
    if guidance_file.exists():
        try:
            guidance = guidance_file.read_text(encoding="utf-8").strip()
            if guidance and canonical_fact_sheet is not None:
                return StageResult(
                    stage=Stage.PAPER_DRAFT,
                    status=StageStatus.FAILED,
                    artifacts=(),
                    error=(
                        "Canonical domain-evaluator Stage 17 does not accept "
                        "unscoped HITL rewrite guidance"
                    ),
                    decision="retry",
                )
            if guidance and llm is not None:
                logger.info("Applying HITL guidance to paper draft")
                resp = llm.chat(
                    [{"role": "user", "content": (
                        f"The human researcher provided this guidance for the paper:\n\n"
                        f"{guidance}\n\n"
                        f"Apply these suggestions to improve the following draft. "
                        f"Preserve all existing content and citations. "
                        f"Only make changes that align with the guidance.\n\n"
                        f"## Current Draft\n{draft[:8000]}"
                    )}],
                    max_tokens=8192,
                )
                draft = resp.content
        except Exception:
            logger.debug("HITL guidance application to draft failed (non-blocking)")

    draft_path = stage_dir / "paper_draft.md"
    final_draft = draft
    structure_report = _validate_stage17_manuscript_structure(
        final_draft,
        stage_dir=stage_dir,
    )
    if not structure_report["valid"]:
        issue_codes = sorted(
            str(issue["code"])
            for issue in structure_report["issues"]
            if isinstance(issue, dict) and issue.get("code")
        )
        logger.error(
            "Stage 17: manuscript structure is ambiguous: %s",
            ", ".join(issue_codes),
        )
        (stage_dir / "paper_draft_invalid.md").write_text(
            final_draft, encoding="utf-8"
        )
        draft_path.unlink(missing_ok=True)
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=(
                "paper_draft_invalid.md",
                "paper_structure_report.json",
                "section_generation_report.json",
            ),
            error=(
                "Paper draft failed strict structure validation: "
                + ", ".join(issue_codes)
            ),
            evidence_refs=(
                "stage-17/paper_draft_invalid.md",
                "stage-17/paper_structure_report.json",
                "stage-17/section_generation_report.json",
            ),
        )

    if citation_repair_claims:
        repair_system = _pm.for_stage(
            "paper_draft", evolution_overlay="", preamble="", topic_constraint="",
            exp_metrics_instruction="", citation_instruction="", writing_structure="",
            outline="", venue_guidance="",
        ).system
        try:
            final_draft = _repair_heading_citation_closure(
                llm=llm,
                run_dir=run_dir,
                stage_dir=stage_dir,
                paper_text=final_draft,
                evidence=evidence,
                claims=citation_repair_claims,
                heading_citation_instructions=heading_citation_instructions,
                system=repair_system,
                canonical_fact_sheet=canonical_fact_sheet,
            )
            structure_report = _validate_stage17_manuscript_structure(
                final_draft, stage_dir=stage_dir
            )
            if not structure_report["valid"]:
                raise CitationPlanContractError(
                    "heading-local citation repair broke manuscript structure"
                )
        except (CitationPlanContractError, ManuscriptStructureError, OSError) as exc:
            (stage_dir / "paper_draft_invalid.md").write_text(final_draft, encoding="utf-8")
            return StageResult(
                stage=Stage.PAPER_DRAFT,
                status=StageStatus.FAILED,
                artifacts=("paper_draft_invalid.md", "paper_structure_report.json", "section_generation_report.json"),
                error=f"Paper draft heading citation closure failed: {exc}",
                decision="retry",
            )

    try:
        experiment_report = build_experiment_fact_closure_report(
            run_dir, paper_text=final_draft, evidence=evidence
        )
        if not experiment_report["valid"]:
            initial_experiment_report = experiment_report
            _write_experiment_fact_invalid(stage_dir, experiment_report)
            (stage_dir / "experiment_fact_closure_initial.json").write_text(
                canonical_experiment_fact_json_text(initial_experiment_report),
                encoding="utf-8",
            )
            try:
                final_draft, repair_log = remove_unsupported_experiment_fact_blocks(
                    final_draft,
                    grounded_numeric_values=experiment_report[
                        "grounded_numeric_values"
                    ],
                    dataset_origin=experiment_report["dataset_origin"],
                    structured_fact_violations=experiment_report.get(
                        "structured_fact_violations", []
                    ),
                    canonical_fact_sheet=canonical_fact_sheet,
                )
            except Exception as exc:  # noqa: BLE001
                raise ExperimentFactClosureError(
                    f"deterministic experiment-fact repair failed: {exc}"
                ) from exc
            (stage_dir / "experiment_fact_repair_log.json").write_text(
                canonical_experiment_fact_json_text(repair_log), encoding="utf-8"
            )
            structure_report = _validate_stage17_manuscript_structure(
                final_draft, stage_dir=stage_dir
            )
            if not structure_report["valid"]:
                raise ExperimentFactClosureError(
                    "deterministic experiment-fact repair broke manuscript structure"
                )
            experiment_report = build_experiment_fact_closure_report(
                run_dir, paper_text=final_draft, evidence=evidence
            )
            (stage_dir / "experiment_fact_closure_after.json").write_text(
                canonical_experiment_fact_json_text(experiment_report),
                encoding="utf-8",
            )
            if _fact_repair_added_numeric_authority(
                initial_experiment_report, experiment_report
            ):
                _write_experiment_fact_invalid(stage_dir, experiment_report)
                raise ExperimentFactClosureError(
                    "deterministic experiment-fact repair added numeric authority"
                )
            if not experiment_report["valid"]:
                _write_experiment_fact_invalid(stage_dir, experiment_report)
                raise ExperimentFactClosureError(
                    "deterministic experiment-fact repair did not close experiment facts"
                )
            (stage_dir / "experiment_fact_closure_invalid.json").unlink(
                missing_ok=True
            )
        experiment_report_text = canonical_experiment_fact_json_text(
            experiment_report
        )
        (stage_dir / "experiment_fact_closure_report.json").write_text(
            experiment_report_text, encoding="utf-8"
        )
        citation_report = build_citation_closure_report(
            run_dir,
            config,
            paper_text=final_draft,
            structure_report_text=(stage_dir / "paper_structure_report.json").read_text(
                encoding="utf-8"
            ),
            experiment_fact_report_text=experiment_report_text,
            evidence=evidence,
        )
        (stage_dir / "citation_closure_report.json").write_text(
            canonical_json_text(citation_report), encoding="utf-8"
        )
        draft_path.write_text(final_draft, encoding="utf-8")
        validate_experiment_fact_closure_report(run_dir, evidence=evidence)
        validate_citation_closure_report(run_dir, config, evidence=evidence)
    except (
        CitationPlanContractError,
        ExperimentFactClosureError,
        OSError,
        UnicodeDecodeError,
    ) as exc:
        (stage_dir / "paper_draft_invalid.md").write_text(
            final_draft, encoding="utf-8"
        )
        for failed_name in (
            "paper_draft.md",
            "experiment_fact_closure_report.json",
            "citation_closure_report.json",
        ):
            (stage_dir / failed_name).unlink(missing_ok=True)
        failure_artifacts = [
            "paper_draft_invalid.md",
            "paper_structure_report.json",
            "section_generation_report.json",
        ]
        failure_refs = [
            "stage-17/paper_draft_invalid.md",
            "stage-17/paper_structure_report.json",
            "stage-17/section_generation_report.json",
        ]
        if (stage_dir / "experiment_fact_closure_invalid.json").is_file():
            failure_artifacts.append("experiment_fact_closure_invalid.json")
            failure_refs.append("stage-17/experiment_fact_closure_invalid.json")
        for diagnostic_name in (
            "citation_heading_repair_log.json",
            "experiment_fact_closure_initial.json",
            "experiment_fact_closure_after.json",
            "experiment_fact_repair_log.json",
        ):
            if (stage_dir / diagnostic_name).is_file():
                failure_artifacts.append(diagnostic_name)
                failure_refs.append(f"stage-17/{diagnostic_name}")
        return StageResult(
            stage=Stage.PAPER_DRAFT,
            status=StageStatus.FAILED,
            artifacts=tuple(failure_artifacts),
            error=f"Paper draft closure failed: {exc}",
            evidence_refs=tuple(failure_refs),
            decision="retry",
        )

    # --- HITL: Paper Co-Writer data persistence ---
    try:
        from researchclaw.hitl.workshops.paper import PaperCoWriter

        writer = PaperCoWriter(run_dir, llm_client=llm)
        writer.load_outline()
        draft_path = stage_dir / "paper_draft.md"
        if draft_path.exists():
            draft_text = draft_path.read_text(encoding="utf-8")
            for section in writer.sections:
                # Extract section content from draft
                import re as _re_pw
                pattern = rf"(?:^|\n)##?\s*{_re_pw.escape(section.name)}.*?\n(.*?)(?=\n##?\s|\Z)"
                match = _re_pw.search(draft_text, _re_pw.DOTALL)
                if match:
                    section.content = match.group(1).strip()
                    section.status = "ai_draft"
        writer.save()
    except Exception:
        pass

    success_artifacts = [
        "paper_draft.md",
        "paper_structure_report.json",
        "section_generation_report.json",
        "experiment_fact_closure_report.json",
        "citation_closure_report.json",
    ]
    success_refs = [f"stage-17/{name}" for name in success_artifacts]
    for diagnostic_name in (
        "citation_heading_repair_log.json",
        "experiment_fact_closure_initial.json",
        "experiment_fact_closure_after.json",
        "experiment_fact_repair_log.json",
    ):
        if (stage_dir / diagnostic_name).is_file():
            success_artifacts.append(diagnostic_name)
    return StageResult(
        stage=Stage.PAPER_DRAFT,
        status=StageStatus.DONE,
        artifacts=tuple(success_artifacts),
        evidence_refs=tuple(success_refs),
    )
