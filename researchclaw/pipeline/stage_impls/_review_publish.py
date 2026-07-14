"""Stages 18-23: Peer review, paper revision, quality gate, knowledge archive, export/publish, and citation verify."""

from __future__ import annotations

import json
import logging
import math
import re
import hashlib
from collections.abc import Mapping
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml  # noqa: F401 — available for downstream use

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    validate_contract_dict,
)
from researchclaw.llm.client import LLMClient
from researchclaw.literature.citation_policy import (
    CitationPolicyContractError,
    load_effective_citation_policy,
    parse_config_snapshot_text,
)
from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    load_canonical_bibliography,
    replay_citation_plan_provenance,
    replay_citation_closure,
    validate_final_paper_citations,
    validate_paper_citation_minimum,
    validate_paper_citation_minimum_from_authority,
    validate_citation_closure_report,
)
from researchclaw.literature.experiment_fact_closure import (
    ExperimentFactClosureError,
    replay_experiment_fact_closure,
    validate_experiment_fact_closure_report,
)
from researchclaw.pipeline._domain import _detect_domain  # noqa: F401
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    canonical_decimal,
    load_canonical_experiment_evidence,
    semantic_config_sha256,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
    Stage19InputBundle,
    Stage19InputBundleError,
    load_stage19_input_bundle,
    parse_stage18_review_structure_report,
    verify_stage19_input_bundle_unchanged,
)
from researchclaw.pipeline.stage20_input_bundle import (
    Stage20InputBundle,
    Stage20InputBundleError,
    load_stage20_input_bundle,
    parse_revision_evidence_binding,
    verify_stage20_input_bundle_unchanged,
)
from researchclaw.pipeline.stage20_publication import (
    reconstruct_stage20_fabrication_state,
    thaw_canonical_summary,
)
from researchclaw.pipeline.stage21_input_bundle import (
    Stage21InputBundle,
    Stage21InputBundleError,
    load_stage21_input_bundle,
    replay_stage21_input_bundle,
    verify_stage21_input_bundle_unchanged,
    verify_stage21_output_artifacts,
)
from researchclaw.pipeline.stage22_export import export_canonical_stage22
from researchclaw.pipeline.stage22_input_bundle import (
    load_stage22_input_bundle,
)
from researchclaw.pipeline._helpers import (
    StageResult,
    _build_context_preamble,
    _chat_with_prompt,
    _collect_experiment_results,  # noqa: F401
    _default_quality_report,
    _extract_paper_title,
    _generate_framework_diagram_prompt,
    _generate_neurips_checklist,
    _get_evolution_overlay,
    _read_best_analysis,
    _read_prior_artifact,
    _safe_json_loads,
    _topic_constraint_block,  # noqa: F401
    _utcnow_iso,
    reconcile_figure_refs,
)
from researchclaw.pipeline.sectional_revision import (
    SectionalRevisionContractError,
    extract_review_ledger,
)
from researchclaw.pipeline.sectional_validation import extract_citation_keys
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers imported from executor.py (not yet moved to _helpers.py).
# Lazy-imported inside functions to avoid circular import when executor.py
# imports this module.
# ---------------------------------------------------------------------------


def _get_collect_raw_experiment_metrics():
    from researchclaw.pipeline.stage_impls._paper_writing import _collect_raw_experiment_metrics
    return _collect_raw_experiment_metrics


def _get_review_compiled_pdf():
    from researchclaw.pipeline.stage_impls._paper_writing import _review_compiled_pdf
    return _review_compiled_pdf


def _snapshot_claim_scope(evidence: CanonicalExperimentEvidence) -> str:
    try:
        payload = yaml.safe_load(evidence.experiment_contract_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("canonical experiment contract root is not an object")
        return validate_contract_dict(payload).claim_scope
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"canonical snapshot contract is invalid: {exc}") from exc


# ---------------------------------------------------------------------------
# _collect_experiment_evidence
# ---------------------------------------------------------------------------

def _collect_experiment_evidence(evidence: CanonicalExperimentEvidence) -> str:
    """Render only the accessor-selected immutable experiment snapshot."""
    metrics_summary = evidence.summary.get("metrics_summary")
    parts = [
        "## Actual Experiment Evidence",
        "Use only the accessor-selected evidence below to verify methodology and "
        "quantitative claims. Do not infer results beyond these records.",
        "### Canonical Evidence Identity\n"
        f"- Manifest path: `{evidence.manifest_path}`\n"
        f"- Manifest SHA-256: `{evidence.manifest_sha256}`",
        "### Selected Result\n```json\n"
        + canonical_authority_json_text(evidence.selected_result).rstrip()
        + "\n```",
        "### Metric Observations\n```json\n"
        + canonical_authority_json_text(evidence.metric_observations).rstrip()
        + "\n```",
        "### Structured Results\n```json\n"
        + canonical_authority_json_text(evidence.structured_results).rstrip()
        + "\n```",
    ]
    if isinstance(metrics_summary, Mapping):
        parts.append(
            "### Summary Metrics\n```json\n"
            + canonical_authority_json_text(metrics_summary).rstrip()
            + "\n```"
        )
    if evidence.analysis_text.strip():
        parts.append("### Canonical Analysis\n" + evidence.analysis_text)
    return "\n\n" + "\n\n".join(parts)


def _review_structure_report(
    evidence: CanonicalExperimentEvidence,
    *,
    stage17_binding: Mapping[str, str],
    **fields: Any,
) -> dict[str, Any]:
    fields.setdefault("comment_count", 0)
    fields.setdefault("issues", [])
    return {
        "schema_version": 2,
        "source_reviews_path": "stage-18/reviews.md",
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        **stage17_binding,
        **fields,
    }


def _read_bound_stage17_draft(run_dir: Path) -> tuple[str, str]:
    """Read one regular Stage 17 draft whose bytes will be closure-bound."""
    stage17_dir = run_dir / "stage-17"
    if stage17_dir.is_symlink() or not stage17_dir.is_dir():
        raise OSError("Stage 17 directory is missing, unsafe, or not a directory")
    draft_path = stage17_dir / "paper_draft.md"
    if draft_path.is_symlink() or not draft_path.is_file():
        raise OSError("Stage 17 paper draft is missing or not a regular file")
    draft_bytes = draft_path.read_bytes()
    try:
        return draft_bytes.decode("utf-8"), hashlib.sha256(draft_bytes).hexdigest()
    except UnicodeDecodeError as exc:
        raise OSError(f"Stage 17 paper draft is not UTF-8: {exc}") from exc


def _load_bound_stage19_inputs(
    run_dir: Path,
    config: RCConfig,
    evidence: CanonicalExperimentEvidence,
) -> Stage19InputBundle:
    """Capture and replay Stage 17/18 inputs before either Stage 19 path."""
    bundle = load_stage19_input_bundle(run_dir)
    try:
        canonical_config_text = evidence.run_config_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("canonical experiment config snapshot is not UTF-8") from exc
    canonical_config = parse_config_snapshot_text(
        canonical_config_text,
        project_root=run_dir,
        label="canonical experiment config snapshot",
    )
    if semantic_config_sha256(config) != semantic_config_sha256(canonical_config):
        raise OSError(
            "runtime config semantic generation differs from canonical experiment evidence"
        )
    citation_authority = replay_citation_plan_provenance(
        bundle.citation_replay_inputs(), canonical_config, project_root=run_dir
    )
    fact_report = replay_experiment_fact_closure(
        paper_bytes=bundle.paper.content,
        stored_report_bytes=bundle.experiment_fact_closure_report.content,
        evidence=evidence,
    )
    citation_report = replay_citation_closure(
        paper_bytes=bundle.paper.content,
        structure_report_bytes=bundle.paper_structure_report.content,
        experiment_fact_report_bytes=bundle.experiment_fact_closure_report.content,
        citation_closure_report_bytes=bundle.citation_closure_report.content,
        citation_plan_bytes=bundle.citation_plan.content,
        citation_allowlist_bytes=bundle.citation_allowlist.content,
        citation_authority=citation_authority,
        evidence=evidence,
    )
    if (
        fact_report["paper_sha256"] != bundle.paper.sha256
        or citation_report["paper_sha256"] != bundle.paper.sha256
        or fact_report["canonical_experiment_evidence_path"] != evidence.manifest_path
        or fact_report["canonical_experiment_evidence_sha256"] != evidence.manifest_sha256
    ):
        raise OSError("Stage 17 closure reports do not bind the current draft")
    reviews = bundle.reviews.text()
    report = parse_stage18_review_structure_report(
        bundle.review_structure_report.text(),
        bundle=bundle,
        canonical_evidence_path=evidence.manifest_path,
        canonical_evidence_sha256=evidence.manifest_sha256,
    )
    if report["valid"] is not True:
        raise OSError("Stage 18 review structure report is not valid")
    return bundle


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Stage 18: Peer Review
# ---------------------------------------------------------------------------

_STAGE18_REVIEW_FORMAT_CONTRACT = """

STAGE 18 OUTPUT CONTRACT (OVERRIDES ALL EARLIER FORMAT INSTRUCTIONS):
- Use only `## Reviewer A`, `## Reviewer B`, and `## Reviewer C` reviewer headings.
- Under each reviewer, use only these exact `###` subsections: `Strengths`,
  `Weaknesses`, and `Actionable Revisions`.
- Every Actionable Revisions item must be a markdown list item. Put every issue
  that requires a manuscript change in that subsection; do not invent headings
  such as Summary, Required Revisions, Additional Rigor Issues, Recommendation,
  or Score.
- After Reviewer C, optionally add exactly one
  `### General Comments (Applicable to All Reviewers)` subsection. Its comments
  must also be markdown list items.
- Do not emit any other markdown heading at any level.
"""

_CITATION_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-five": 25, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60,
}
_COUNT_TOKEN = r"(?P<count>\d+|[a-z]+(?:[-\s][a-z]+)?)"
_CITATION_COUNT_REQUIREMENT_RES = (
    re.compile(
        r"\b(?:at\s+least|minimum(?:\s+of)?|ensure(?:\s+at\s+least)?|"
        r"include(?:\s+at\s+least)?|cite(?:\s+at\s+least)?|add(?:\s+at\s+least)?)"
        rf"\s+{_COUNT_TOKEN}\s+"
        r"(?:(?:relevant|unique)\s+)*(?:citations?|references?|sources?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:increase|expand|raise|grow)\s+(?:the\s+)?"
        r"(?:(?:number|count)\s+of\s+)?"
        r"(?:citations?|references?|sources?|bibliography|citation\s+count|reference\s+count)"
        rf"\s+(?:to|above|beyond)\s+{_COUNT_TOKEN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_COUNT_TOKEN}\s+(?:or\s+more\s+)?"
        r"(?:citations?|references?|sources?)\s+"
        r"(?:(?:are|is|should\s+be|must\s+be)\s+)?"
        r"(?:required|needed|necessary|included|the\s+minimum)\b",
        re.IGNORECASE,
    ),
)


def _citation_count_policy_violations(ledger: Any, target: int) -> list[str]:
    violations: list[str] = []
    for comment in ledger.comments:
        if comment.category != "actionable_revision":
            continue
        for pattern in _CITATION_COUNT_REQUIREMENT_RES:
            for match in pattern.finditer(comment.exact_text):
                raw = match.group("count").casefold()
                normalized = raw.replace(" ", "-")
                count = (
                    int(raw)
                    if raw.isdigit()
                    else _CITATION_COUNT_WORDS.get(normalized)
                )
                if count is None and "-" in normalized:
                    count = _CITATION_COUNT_WORDS.get(normalized.split("-", 1)[0])
                if count is not None and count > target:
                    violations.append(comment.comment_id)
                    break
            if comment.comment_id in violations:
                break
    return violations

def _execute_peer_review(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    for owned_name in ("reviews.md", "review_structure_report.json"):
        (stage_dir / owned_name).unlink(missing_ok=True)
    try:
        evidence = load_canonical_experiment_evidence(run_dir)
    except (CanonicalExperimentEvidenceError, RuntimeError) as exc:
        return StageResult(
            stage=Stage.PEER_REVIEW,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Canonical experiment evidence is invalid or unavailable: {exc}",
            decision="retry",
        )
    try:
        draft, draft_sha256 = _read_bound_stage17_draft(run_dir)
        effective_citation_policy = load_effective_citation_policy(run_dir, config)
        experiment_closure = validate_experiment_fact_closure_report(
            run_dir, evidence=evidence
        )
        citation_closure = validate_citation_closure_report(
            run_dir, config, evidence=evidence
        )
        if (
            experiment_closure["paper_sha256"] != draft_sha256
            or citation_closure["paper_sha256"] != draft_sha256
            or experiment_closure["canonical_experiment_evidence_path"]
            != evidence.manifest_path
            or experiment_closure["canonical_experiment_evidence_sha256"]
            != evidence.manifest_sha256
        ):
            raise ValueError("Stage 17 draft differs from closure-bound paper")
        citation_report_path = run_dir / "stage-17" / "citation_closure_report.json"
        citation_report_bytes = citation_report_path.read_bytes()
        stage17_binding = {
            "source_paper_path": "stage-17/paper_draft.md",
            "source_paper_sha256": draft_sha256,
            "paper_structure_report_path": "stage-17/paper_structure_report.json",
            "paper_structure_report_sha256": citation_closure[
                "structure_report_sha256"
            ],
            "experiment_fact_closure_report_path": (
                "stage-17/experiment_fact_closure_report.json"
            ),
            "experiment_fact_closure_report_sha256": citation_closure[
                "experiment_fact_closure_report_sha256"
            ],
            "citation_closure_report_path": "stage-17/citation_closure_report.json",
            "citation_closure_report_sha256": hashlib.sha256(
                citation_report_bytes
            ).hexdigest(),
        }
    except (
        CitationPolicyContractError,
        CitationPlanContractError,
        ExperimentFactClosureError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.PEER_REVIEW,
            status=StageStatus.FAILED,
            artifacts=(),
            error=(
                "Effective citation policy is invalid or Stage 17 evidence "
                f"closure failed: {exc}"
            ),
            decision="retry",
        )
    experiment_evidence = _collect_experiment_evidence(evidence)

    if llm is not None:
        _pm = prompts or PromptManager()
        sp = _pm.for_stage(
            "peer_review",
            # Persistent experiment lessons migrate separately. Stage 18 v1
            # receives experiment context only from the accessor snapshot.
            evolution_overlay="",
            topic=config.research.topic,
            draft=draft,
            experiment_evidence=experiment_evidence,
        )

        # Reviewer persona and rubric are carried natively by the active
        # prompt bank (ML bank -> NeurIPS/ICML referees, HEP bank -> HEP
        # theorist/phenomenologist/experimentalist). No adapter overlay.
        _review_system = sp.system
        _citation_policy_suffix = (
            "\n\nCITATION COUNT POLICY:\n"
            f"- Effective minimum unique sources: "
            f"{effective_citation_policy['effective_min_unique_sources']}.\n"
            f"- Effective target unique sources: "
            f"{effective_citation_policy['effective_target_unique_sources']}.\n"
            "- Do not require a citation count above the effective target.\n"
        )
        _review_user = (
            sp.user
            + _citation_policy_suffix
            + _STAGE18_REVIEW_FORMAT_CONTRACT
        )
        resp = _chat_with_prompt(
            llm,
            _review_system,
            _review_user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        reviews = resp.content
    else:
        reviews = """## Reviewer A

### Strengths
Clear problem statement.

### Weaknesses
Limited ablation details.

### Actionable Revisions
1. Add uncertainty analysis and stronger baselines.

## Reviewer B

### Strengths
Reproducibility focus.

### Weaknesses
Discussion underdeveloped.

### Actionable Revisions
1. Expand limitations and broader impact.

## Reviewer C

### Strengths
The evaluation criteria are stated clearly.

### Weaknesses
Statistical reporting is incomplete.

### Actionable Revisions
1. Report uncertainty and the number of independent seeds.

### General Comments (Applicable to All Reviewers)
1. Keep every quantitative claim tied to experiment evidence.
"""
    (stage_dir / "reviews.md").write_text(reviews, encoding="utf-8")
    try:
        ledger = extract_review_ledger(
            reviews,
            source_path="stage-18/reviews.md",
        )
    except SectionalRevisionContractError as exc:
        report = _review_structure_report(
            evidence,
            stage17_binding=stage17_binding,
            valid=False,
            source_reviews_sha256=hashlib.sha256(
                reviews.encode("utf-8")
            ).hexdigest(),
            issues=[
                {
                    "code": issue.code,
                    "message": issue.message,
                    "line": issue.line,
                }
                for issue in exc.issues
            ],
        )
        (stage_dir / "review_structure_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        issue_codes = sorted({issue.code for issue in exc.issues})
        return StageResult(
            stage=Stage.PEER_REVIEW,
            status=StageStatus.FAILED,
            artifacts=("reviews.md", "review_structure_report.json"),
            error=(
                "Peer review failed strict structure validation: "
                + ", ".join(issue_codes)
            ),
            evidence_refs=(
                "stage-18/reviews.md",
                "stage-18/review_structure_report.json",
            ),
        )
    citation_target = int(
        effective_citation_policy["effective_target_unique_sources"]
    )
    policy_violations = _citation_count_policy_violations(ledger, citation_target)
    if policy_violations and llm is not None:
        repair_prompt = (
            _review_user
            + "\n\nYour previous review imposed a citation-count requirement above "
            + f"the effective target of {citation_target}. Regenerate the complete "
            + "review once, preserving the exact heading contract and lowering any "
            + "citation-count requirement to the effective target or below."
        )
        try:
            repaired = _chat_with_prompt(
                llm,
                _review_system,
                repair_prompt,
                json_mode=sp.json_mode,
                max_tokens=sp.max_tokens,
                retries=0,
            )
        except Exception as exc:  # noqa: BLE001 - preserve no-artifact failure state.
            for owned_name in ("reviews.md", "review_structure_report.json"):
                (stage_dir / owned_name).unlink(missing_ok=True)
            return StageResult(
                stage=Stage.PEER_REVIEW,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Citation-policy review repair failed: {exc}",
                decision="retry",
            )
        reviews = repaired.content
        (stage_dir / "reviews.md").write_text(reviews, encoding="utf-8")
        try:
            ledger = extract_review_ledger(reviews, source_path="stage-18/reviews.md")
        except SectionalRevisionContractError as exc:
            report = _review_structure_report(
                evidence,
                stage17_binding=stage17_binding,
                valid=False,
                source_reviews_sha256=hashlib.sha256(
                    reviews.encode("utf-8")
                ).hexdigest(),
                issues=[
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "line": issue.line,
                    }
                    for issue in exc.issues
                ],
            )
            (stage_dir / "review_structure_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            issue_codes = sorted({issue.code for issue in exc.issues})
            return StageResult(
                stage=Stage.PEER_REVIEW,
                status=StageStatus.FAILED,
                artifacts=("reviews.md", "review_structure_report.json"),
                error=(
                    "Citation-policy repair broke review structure: "
                    + ", ".join(issue_codes)
                ),
                evidence_refs=(
                    "stage-18/reviews.md",
                    "stage-18/review_structure_report.json",
                ),
            )
        policy_violations = _citation_count_policy_violations(
            ledger, citation_target
        )
    if policy_violations:
        report = _review_structure_report(
            evidence,
            stage17_binding=stage17_binding,
            valid=False,
            source_reviews_sha256=ledger.source_reviews_sha256,
            issues=[
                {
                    "code": "citation_count_policy_exceeded",
                    "message": (
                        f"Actionable revision exceeds effective target {citation_target}: "
                        + ", ".join(policy_violations)
                    ),
                    "line": None,
                }
            ],
        )
        (stage_dir / "review_structure_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return StageResult(
            stage=Stage.PEER_REVIEW,
            status=StageStatus.FAILED,
            artifacts=("reviews.md", "review_structure_report.json"),
            error="Peer review exceeds the effective citation target",
            evidence_refs=(
                "stage-18/reviews.md",
                "stage-18/review_structure_report.json",
            ),
        )
    report = _review_structure_report(
        evidence,
        stage17_binding=stage17_binding,
        valid=True,
        source_reviews_sha256=ledger.source_reviews_sha256,
        comment_count=len(ledger.comments),
        issues=[],
    )
    (stage_dir / "review_structure_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return StageResult(
        stage=Stage.PEER_REVIEW,
        status=StageStatus.DONE,
        artifacts=("reviews.md", "review_structure_report.json"),
        evidence_refs=(
            "stage-18/reviews.md",
            "stage-18/review_structure_report.json",
        ),
    )


# ---------------------------------------------------------------------------
# Stage 19: Paper Revision
# ---------------------------------------------------------------------------

def _execute_paper_revision(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
    sectional_provider: object | None = None,
) -> StageResult:
    from researchclaw.pipeline.sectional_execution import clean_sectional_outputs

    clean_sectional_outputs(stage_dir)
    for artifact_name in (
        "paper_revised.md",
        "revision_notes_internal.md",
        "revision_retry_failure.json",
        "revision_evidence_binding.json",
    ):
        (stage_dir / artifact_name).unlink(missing_ok=True)
    try:
        evidence = load_canonical_experiment_evidence(run_dir)
    except (CanonicalExperimentEvidenceError, RuntimeError) as exc:
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Canonical experiment evidence is invalid or unavailable: {exc}",
            decision="retry",
        )
    try:
        claim_scope = _snapshot_claim_scope(evidence)
    except ValueError as exc:
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Canonical experiment contract is invalid or unavailable: {exc}",
            decision="retry",
        )
    try:
        stage19_inputs = _load_bound_stage19_inputs(run_dir, config, evidence)
        draft = stage19_inputs.paper.text()
        reviews = stage19_inputs.reviews.text()
        _draft_sha256 = stage19_inputs.paper.sha256
        _reviews_sha256 = stage19_inputs.reviews.sha256
    except (
        CitationPlanContractError,
        ExperimentFactClosureError,
        Stage19InputBundleError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 17/18 revision inputs are invalid: {exc}",
            decision="retry",
        )
    if config.paper_revision.sectional_enabled:
        from researchclaw.pipeline.sectional_execution import (
            execute_sectional_revision,
        )
        from researchclaw.pipeline.sectional_llm import (
            LLMSectionalRevisionProvider,
        )

        try:
            if sectional_provider is None and llm is not None:
                sectional_provider = LLMSectionalRevisionProvider(
                    llm=llm,
                    writer_model=config.llm.primary_model,
                    critic_model=config.paper_revision.critic_model,
                )
            outcome = execute_sectional_revision(
                stage_dir=stage_dir,
                run_dir=run_dir,
                config=config.paper_revision,
                claim_scope=claim_scope,
                provider=sectional_provider,  # type: ignore[arg-type]
                evidence=evidence,
                paper_text=draft,
                reviews_text=reviews,
                review_structure_report_text=stage19_inputs.review_structure_report.text(),
                bibliography_text=stage19_inputs.bibliography.text(),
                bibliography_sha256=stage19_inputs.bibliography.sha256,
            )
        except Exception as exc:  # deterministic sectional stage boundary
            logger.error("Stage 19 sectional revision failed: %s", exc)
            return StageResult(
                stage=Stage.PAPER_REVISION,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Sectional revision failed: {exc}",
            )
        try:
            verify_stage19_input_bundle_unchanged(run_dir, stage19_inputs)
        except Stage19InputBundleError as exc:
            clean_sectional_outputs(stage_dir)
            return StageResult(
                stage=Stage.PAPER_REVISION,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Stage 17/18 revision inputs changed during Stage 19: {exc}",
                decision="retry",
            )
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=(StageStatus.DONE if outcome.completed else StageStatus.FAILED),
            artifacts=outcome.artifacts,
            error=outcome.error,
            evidence_refs=tuple(f"stage-19/{name}" for name in outcome.artifacts),
            decision="sectional" if outcome.completed else "sectional_incomplete",
        )

    # A resumed run may reuse the same stage directory. Remove outputs owned by
    # this stage before any LLM call so a failed retry cannot expose artifacts
    # from an earlier attempt as current output.
    draft_word_count = len(draft.split())

    def _save_revision_notes(text: str) -> None:
        revision_words = text.split()
        revision_summary = (
            " ".join(revision_words[:500]) + "\n\n*(Revision summary truncated)*"
            if len(revision_words) > 500
            else text
        )
        if revision_summary.strip():
            (stage_dir / "revision_notes_internal.md").write_text(
                revision_summary, encoding="utf-8"
            )

    # R4-2: Collect real metrics for anti-fabrication guard in revision
    # BUG-47: _collect_raw_experiment_metrics returns tuple[str, bool], must unpack
    raw_metrics_revision = canonical_authority_json_text(
        {
            "metric_observations": evidence.metric_observations,
            "structured_results": evidence.structured_results,
            "summary": evidence.summary,
        }
    )
    data_integrity_revision = ""
    if raw_metrics_revision:
        data_integrity_revision = (
            raw_metrics_revision
            + "\nDATA INTEGRITY: Do NOT add new numbers that are not in the "
            "experiment data above. If a reviewer asks for additional results "
            "you do not have, state 'Due to computational constraints, "
            "this analysis was not conducted' instead of fabricating data.\n"
        )

    if llm is not None:
        _pm = prompts or PromptManager()
        try:
            _ws_revision = _pm.block("writing_structure")
        except (KeyError, Exception):  # noqa: BLE001
            _ws_revision = ""
        # IMP-20/25/31/24: Load style blocks for revision prompt
        _rev_blocks: dict[str, str] = {}
        for _bname in ("academic_style_guide", "narrative_writing_rules",
                        "anti_hedging_rules", "anti_repetition_rules"):
            try:
                _rev_blocks[_bname] = _pm.block(_bname)
            except (KeyError, Exception):  # noqa: BLE001
                _rev_blocks[_bname] = ""
        _overlay = _get_evolution_overlay(run_dir, "paper_revision")
        sp = _pm.for_stage(
            "paper_revision",
            evolution_overlay=_overlay,
            topic_constraint=_pm.block("topic_constraint", topic=config.research.topic),
            writing_structure=_ws_revision,
            draft=draft,
            reviews=reviews + data_integrity_revision,
            **_rev_blocks,
        )

        # Revision system prompt and rules are carried natively by the active
        # prompt bank; no adapter overlay.
        _revision_system = sp.system
        _revision_user = sp.user
        # R10-Fix2: Ensure max_tokens is sufficient for full paper revision
        revision_max_tokens = sp.max_tokens
        if revision_max_tokens and draft_word_count > 0:
            # ~1.5 tokens per word, 20% headroom
            min_tokens_needed = int(draft_word_count * 1.5 * 1.2)
            if revision_max_tokens < min_tokens_needed:
                revision_max_tokens = min_tokens_needed
                logger.info(
                    "Stage 19: Increased max_tokens from %d to %d to fit full paper revision",
                    sp.max_tokens,
                    revision_max_tokens,
                )

        # R10-Fix4: Retry on timeout for paper revision (critical stage)
        resp = _chat_with_prompt(
            llm,
            _revision_system,
            _revision_user,
            json_mode=sp.json_mode,
            max_tokens=revision_max_tokens,
            retries=2,
        )
        revised = resp.content
        revised_word_count = len(revised.split())
        # Length guard: if revision is shorter than 80% of draft, retry once
        if draft_word_count > 500 and revised_word_count < int(draft_word_count * 0.8):
            logger.warning(
                "Paper revision (%d words) is shorter than draft (%d words). "
                "Retrying with stronger length enforcement.",
                revised_word_count,
                draft_word_count,
            )
            retry_user = (
                f"CRITICAL LENGTH REQUIREMENT: The draft is {draft_word_count} words. "
                f"Your revision MUST be at least {draft_word_count} words — ideally longer. "
                f"Do NOT summarize or condense ANY section. Copy each section verbatim "
                f"and ONLY make targeted improvements to address reviewer comments. "
                f"If a section has no reviewer comments, include it UNCHANGED.\n\n"
                + _revision_user
            )
            try:
                resp2 = _chat_with_prompt(
                    llm, _revision_system, retry_user,
                    json_mode=sp.json_mode, max_tokens=revision_max_tokens,
                    retries=2,
                )
            except RuntimeError as exc:
                if claim_scope != "pipeline_validation":
                    raise
                cause = exc.__cause__
                logger.warning(
                    "Length-enforcement retry failed after a usable but short "
                    "revision (%d/%d words): %s. Falling back to the full "
                    "original draft for Stage 20 quality review.",
                    revised_word_count,
                    draft_word_count,
                    exc,
                )
                _save_revision_notes(revised)
                (stage_dir / "revision_retry_failure.json").write_text(
                    json.dumps(
                        {
                            "fallback": "full_original_draft",
                            "draft_word_count": draft_word_count,
                            "first_revision_word_count": revised_word_count,
                            "error_type": type(exc).__name__,
                            "cause_type": type(cause).__name__ if cause else None,
                            "error": str(exc),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                revised = draft
            else:
                revised2 = resp2.content
                revised2_word_count = len(revised2.split())
                if revised2_word_count >= int(draft_word_count * 0.8):
                    revised = revised2
                elif revised2_word_count > revised_word_count:
                    # Retry improved but still not enough — use the longer version
                    revised = revised2
                    logger.warning(
                        "Retry improved (%d → %d words) but still shorter than draft (%d).",
                        revised_word_count,
                        revised2_word_count,
                        draft_word_count,
                    )
                else:
                    # Both attempts produced short output — preserve full original draft
                    logger.warning(
                        "Retry also produced short output (%d words). "
                        "Falling back to FULL ORIGINAL DRAFT to prevent content loss.",
                        revised2_word_count,
                    )
                    _save_revision_notes(revised)
                    revised = draft
    else:
        revised = draft
    revised_path = stage_dir / "paper_revised.md"
    binding = {
        "schema_version": 1,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": "stage-17/paper_draft.md",
        "source_paper_sha256": _draft_sha256,
        "source_reviews_path": "stage-18/reviews.md",
        "source_reviews_sha256": _reviews_sha256,
        "revised_paper_path": "stage-19/paper_revised.md",
        "revised_paper_sha256": hashlib.sha256(revised.encode("utf-8")).hexdigest(),
    }
    binding_path = stage_dir / "revision_evidence_binding.json"
    try:
        _write_text_atomic(revised_path, revised)
        _write_text_atomic(
            binding_path,
            json.dumps(binding, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        )
        stored_revised_sha256 = hashlib.sha256(revised_path.read_bytes()).hexdigest()
        parse_revision_evidence_binding(
            binding_path.read_text(encoding="utf-8"),
            evidence=evidence,
            draft_sha256=_draft_sha256,
            reviews_sha256=_reviews_sha256,
            revised_sha256=stored_revised_sha256,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        revised_path.unlink(missing_ok=True)
        binding_path.unlink(missing_ok=True)
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 19 revision evidence binding failed: {exc}",
            decision="retry",
        )
    artifacts = ["paper_revised.md", "revision_evidence_binding.json"]
    for diagnostic_name in (
        "revision_notes_internal.md",
        "revision_retry_failure.json",
    ):
        if (stage_dir / diagnostic_name).is_file():
            artifacts.append(diagnostic_name)
    try:
        verify_stage19_input_bundle_unchanged(run_dir, stage19_inputs)
    except Stage19InputBundleError as exc:
        revised_path.unlink(missing_ok=True)
        binding_path.unlink(missing_ok=True)
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 17/18 revision inputs changed during Stage 19: {exc}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.PAPER_REVISION,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=(
            "stage-19/paper_revised.md",
            "stage-19/revision_evidence_binding.json",
        ),
    )


# ---------------------------------------------------------------------------
# Stage 20: Quality Gate
# ---------------------------------------------------------------------------


_STAGE20_OWNED_OUTPUTS = (
    "quality_gate_manifest.json",
    "quality_report.json",
    "fabrication_flags.json",
    "quality_gate_manifest.json.tmp",
    "quality_report.json.tmp",
    "fabrication_flags.json.tmp",
)


def _bound_output_artifact(
    output: BoundOutputNamespace, stage_name: str, name: str
) -> BoundArtifact:
    content = output.read_bytes(name)
    return BoundArtifact(
        path=f"{stage_name}/{name}",
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _output_invalidation_suffix(
    output: BoundOutputNamespace, names: tuple[str, ...]
) -> str:
    try:
        output.invalidate(names)
    except OSError as exc:
        return f"; output invalidation also failed: {exc}"
    return ""


def _normalize_quality_score(value: object) -> float:
    """Map malformed or nonfinite LLM scores to the fail-closed floor."""

    if isinstance(value, bool):
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score) or score < 0 or score > 10:
        return 0.0
    return score


_QUALITY_RESPONSE_FIELDS = frozenset(
    {"score_1_to_10", "verdict", "strengths", "weaknesses", "required_actions"}
)


def _parse_quality_gate_response(text: str) -> dict[str, Any]:
    """Parse the sole supported LLM quality schema without JSON ambiguity."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate quality response key {key!r}")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        raise ValueError(f"nonfinite quality response number {token}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("quality response is not strict JSON") from exc
    if not isinstance(value, dict) or set(value) != _QUALITY_RESPONSE_FIELDS:
        raise ValueError("quality response fields mismatch")
    score = value["score_1_to_10"]
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0 <= score <= 10
    ):
        raise ValueError("quality response score must be a finite number in [0, 10]")
    if value["verdict"] not in {"proceed", "revise", "reject"}:
        raise ValueError("quality response verdict is invalid")
    for field in ("strengths", "weaknesses", "required_actions"):
        items = value[field]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise ValueError(f"quality response {field} must be a string array")
    return value

def _execute_quality_gate(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    output: BoundOutputNamespace | None = None
    try:
        output = BoundOutputNamespace.open(run_dir, stage_dir, "stage-20")
        output.invalidate(_STAGE20_OWNED_OUTPUTS)
    except OSError as exc:
        if output is not None:
            output.close()
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 20 output namespace is unsafe: {exc}",
            decision="retry",
        )
    assert output is not None
    try:
        return _execute_quality_gate_bound(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,
            prompts=prompts,
            output=output,
        )
    finally:
        output.close()


def _execute_quality_gate_bound(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None,
    prompts: PromptManager | None,
    output: BoundOutputNamespace,
) -> StageResult:
    (run_dir / "degradation_signal.json").unlink(missing_ok=True)
    try:
        evidence = load_canonical_experiment_evidence(run_dir)
        canonical_config_text = evidence.run_config_bytes.decode("utf-8")
        canonical_config = parse_config_snapshot_text(
            canonical_config_text,
            project_root=run_dir,
            label="canonical experiment config snapshot",
        )
        if semantic_config_sha256(config) != semantic_config_sha256(canonical_config):
            raise ValueError(
                "runtime config semantic generation differs from canonical experiment evidence"
            )
        stage19_inputs = _load_bound_stage19_inputs(run_dir, config, evidence)
        citation_authority = replay_citation_plan_provenance(
            stage19_inputs.citation_replay_inputs(),
            canonical_config,
            project_root=run_dir,
        )
        claim_scope = _snapshot_claim_scope(evidence)
        stage20_inputs = load_stage20_input_bundle(
            run_dir,
            stage19_inputs=stage19_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
        revised = stage20_inputs.revised_paper.text()
        citation_minimum = int(
            citation_authority.effective_policy["effective_min_unique_sources"]
        )
        validate_paper_citation_minimum_from_authority(
            revised,
            minimum=citation_minimum,
            allowlist=citation_authority.allowlist,
            plan=citation_authority.plan,
        )
        experiment_summary = thaw_canonical_summary(evidence)
        fabrication_state = reconstruct_stage20_fabrication_state(
            evidence, canonical_config
        )
    except (
        CanonicalExperimentEvidenceError,
        CitationPlanContractError,
        CitationPolicyContractError,
        Stage19InputBundleError,
        Stage20InputBundleError,
        UnicodeDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 20 canonical input validation failed: {exc}",
            decision="retry",
        )
    report: dict[str, Any] | None = None
    experiment_failed = fabrication_state.experiment_failed

    if llm is not None:
        _pm = prompts or PromptManager()
        # IMP-33: Evaluate the full paper instead of truncating to 12K chars.
        # Split into chunks if very long, but prefer sending the full text.
        paper_for_eval = revised[:40000] if len(revised) > 40000 else revised

        # BUG-25: Inject experiment status into quality gate prompt
        _exp_context = ""
        if experiment_summary:
            _exp_status_keys = {
                k: experiment_summary.get(k) for k in (
                    "total_conditions", "total_metric_keys",
                    "metrics_summary",
                ) if experiment_summary.get(k) is not None
            }
            _cond_summ = experiment_summary.get("condition_summaries", {})
            if isinstance(_cond_summ, dict) and _cond_summ:
                _exp_status_keys["completed_conditions"] = len(_cond_summ)
                _exp_status_keys["condition_names"] = list(_cond_summ.keys())[:20]
            if _best_run := experiment_summary.get("best_run"):
                _exp_status_keys["best_run_status"] = (
                    _best_run.get("status") if isinstance(_best_run, dict) else str(_best_run)
                )
            _exp_context = (
                "\n\nExperiment summary (for cross-checking reported numbers):\n"
                + json.dumps(_exp_status_keys, indent=2, default=str)[:4000]
                + "\n\nCross-check: If the experiment status is 'failed' with "
                "empty metrics, any numerical results in tables constitute "
                "fabrication. Penalize severely.\n"
            )

        sp = _pm.for_stage(
            "quality_gate",
            evolution_overlay=None,
            quality_threshold=str(config.research.quality_threshold),
            revised=(
                paper_for_eval
                + _exp_context
                + "\n\nCitation policy: require at least "
                + str(citation_authority.effective_policy["effective_min_unique_sources"])
                + " unique eligible sources and target "
                + str(citation_authority.effective_policy["effective_target_unique_sources"])
                + ". Do not penalize the paper for not exceeding that target.\n"
            ),
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        try:
            report = _parse_quality_gate_response(resp.content)
        except ValueError as exc:
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Stage 20 quality response is invalid: {exc}",
                decision="retry",
            )
    # BUG-25: If experiment failed with no metrics, cap the quality score
    if report is not None and experiment_failed:
        _orig_score = report.get("score_1_to_10", 5)
        if isinstance(_orig_score, (int, float)) and _orig_score > 3:
            report["score_1_to_10"] = min(_orig_score, 3.0)
            report.setdefault("weaknesses", []).append(
                "Experiment failed with no metrics — any reported numerical "
                "results are unsupported and likely fabricated."
            )
            logger.warning(
                "BUG-25: Experiment failed — capping quality score from %.1f to 3.0",
                _orig_score,
            )
    if report is None:
        report = _default_quality_report(config.research.quality_threshold)
    report["generated"] = report.get("generated") or _utcnow_iso()
    report["canonical_experiment_evidence_path"] = evidence.manifest_path
    report["canonical_experiment_evidence_sha256"] = evidence.manifest_sha256
    report["source_paper_path"] = stage20_inputs.revised_paper.path
    report["source_paper_sha256"] = stage20_inputs.revised_paper.sha256
    report["stage19_publication_mode"] = stage20_inputs.publication_mode
    report["stage19_publication_binding_path"] = (
        stage20_inputs.publication_binding.path
    )
    report["stage19_publication_binding_sha256"] = (
        stage20_inputs.publication_binding.sha256
    )

    # T2.1: Enforce quality gate — fail if score below threshold
    score = _normalize_quality_score(report.get("score_1_to_10", 0))
    report["score_1_to_10"] = score
    verdict = report.get("verdict", "proceed")
    threshold = config.research.quality_threshold or 5.0

    # --- Fabrication flag: collect real metrics for Stage 22 sanitization ---
    _fabrication_info: dict[str, Any] = {
        "schema_version": 2,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage20_inputs.revised_paper.path,
        "source_paper_sha256": stage20_inputs.revised_paper.sha256,
        "stage19_publication_mode": stage20_inputs.publication_mode,
        "stage19_publication_binding_path": stage20_inputs.publication_binding.path,
        "stage19_publication_binding_sha256": stage20_inputs.publication_binding.sha256,
        "quality_score": score,
        **fabrication_state.to_dict(),
    }
    # --- Hard guard: block if VerifiedRegistry has zero real experiment values ---
    # Even if the LLM gives a passing score, a paper with no verified
    # experiment data must not proceed to export. This prevents the exact
    # failure in issue #165 where a 3.46s "experiment" produced a fabricated
    # paper that passed the quality gate.
    _vr_zero_values = not fabrication_state.has_real_data
    quality_report_text = (
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    fabrication_flags_text = (
        json.dumps(
            _fabrication_info,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )

    try:
        current_evidence = load_canonical_experiment_evidence(run_dir)
        if current_evidence != evidence:
            raise Stage20InputBundleError(
                "canonical experiment evidence changed during Stage 20"
            )
        verify_stage20_input_bundle_unchanged(
            run_dir,
            stage20_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
        output.write_text_atomic("quality_report.json", quality_report_text)
        output.write_text_atomic("fabrication_flags.json", fabrication_flags_text)
    except (
        CanonicalExperimentEvidenceError,
        Stage19InputBundleError,
        Stage20InputBundleError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        cleanup_suffix = _output_invalidation_suffix(
            output, ("quality_report.json", "fabrication_flags.json")
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=(
                f"Stage 20 inputs changed during evaluation: {exc}"
                + cleanup_suffix
            ),
            decision="retry",
        )
    if _vr_zero_values:
        logger.error(
            "Stage 20 BLOCKED: VerifiedRegistry has zero real experiment values "
            "and experiment summary reports failure. The paper's numerical results "
            "cannot be grounded in real data. Refusing to proceed."
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=("quality_report.json", "fabrication_flags.json"),
            evidence_refs=("stage-20/quality_report.json",),
            error=(
                "Quality gate BLOCKED: VerifiedRegistry has zero real experiment "
                "values and experiment failed. Paper contains no grounded data. "
                "Pipeline must not proceed to export."
            ),
        )
    if isinstance(score, (int, float)) and score < threshold:
        if config.research.graceful_degradation:
            logger.warning(
                "Quality gate DEGRADED: score %.1f < threshold %.1f — "
                "continuing with sanitization (graceful_degradation=True)",
                score, threshold,
            )
            # Write degradation signal for downstream stages
            signal = {
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "weaknesses": report.get("weaknesses", []),
                "generated": _utcnow_iso(),
            }
            (run_dir / "degradation_signal.json").write_text(
                json.dumps(signal, indent=2), encoding="utf-8"
            )
            try:
                _publish_stage20_gate_manifest(
                    run_dir=run_dir,
                    output=output,
                    outcome="degraded",
                    evidence=evidence,
                    stage20_inputs=stage20_inputs,
                    canonical_config=canonical_config,
                    claim_scope=claim_scope,
                    quality_report_text=quality_report_text,
                    fabrication_flags_text=fabrication_flags_text,
                    score=score,
                    threshold=threshold,
                )
            except (OSError, TypeError, ValueError) as exc:
                cleanup_suffix = _output_invalidation_suffix(
                    output, _STAGE20_OWNED_OUTPUTS
                )
                (run_dir / "degradation_signal.json").unlink(missing_ok=True)
                return StageResult(
                    stage=Stage.QUALITY_GATE,
                    status=StageStatus.FAILED,
                    artifacts=("quality_report.json", "fabrication_flags.json"),
                    error=f"Stage 20 gate publication failed: {exc}{cleanup_suffix}",
                    decision="retry",
                )
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.DONE,
                artifacts=(
                    "quality_report.json",
                    "fabrication_flags.json",
                    "quality_gate_manifest.json",
                ),
                evidence_refs=("stage-20/quality_report.json",),
                decision="degraded",
            )
        logger.warning(
            "Quality gate FAILED: score %.1f < threshold %.1f (verdict=%s)",
            score, threshold, verdict,
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=("quality_report.json", "fabrication_flags.json"),
            evidence_refs=("stage-20/quality_report.json",),
            error=f"Quality score {score:.1f}/10 below threshold {threshold:.1f}. "
                  f"Paper needs revision before export.",
        )

    logger.info(
        "Quality gate PASSED: score %.1f >= threshold %.1f",
        score, threshold,
    )
    try:
        _publish_stage20_gate_manifest(
            run_dir=run_dir,
            output=output,
            outcome="passed",
            evidence=evidence,
            stage20_inputs=stage20_inputs,
            canonical_config=canonical_config,
            claim_scope=claim_scope,
            quality_report_text=quality_report_text,
            fabrication_flags_text=fabrication_flags_text,
            score=score,
            threshold=threshold,
        )
    except (OSError, TypeError, ValueError) as exc:
        cleanup_suffix = _output_invalidation_suffix(output, _STAGE20_OWNED_OUTPUTS)
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=("quality_report.json", "fabrication_flags.json"),
            error=f"Stage 20 gate publication failed: {exc}{cleanup_suffix}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.QUALITY_GATE,
        status=StageStatus.DONE,
        artifacts=(
            "quality_report.json",
            "fabrication_flags.json",
            "quality_gate_manifest.json",
        ),
        evidence_refs=("stage-20/quality_report.json",),
    )


def _publish_stage20_gate_manifest(
    *,
    run_dir: Path,
    output: BoundOutputNamespace,
    outcome: str,
    evidence: CanonicalExperimentEvidence,
    stage20_inputs: Stage20InputBundle,
    canonical_config: RCConfig,
    claim_scope: str,
    quality_report_text: str,
    fabrication_flags_text: str,
    score: float,
    threshold: float,
) -> None:
    current_evidence = load_canonical_experiment_evidence(run_dir)
    if current_evidence != evidence:
        raise Stage20InputBundleError(
            "canonical experiment evidence changed before Stage 20 commit"
        )
    verify_stage20_input_bundle_unchanged(
        run_dir,
        stage20_inputs,
        evidence=current_evidence,
        claim_scope=claim_scope,
    )
    payload = {
        "schema_version": 1,
        "outcome": outcome,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage20_inputs.revised_paper.path,
        "source_paper_sha256": stage20_inputs.revised_paper.sha256,
        "stage19_publication_mode": stage20_inputs.publication_mode,
        "stage19_publication_binding_path": stage20_inputs.publication_binding.path,
        "stage19_publication_binding_sha256": stage20_inputs.publication_binding.sha256,
        "quality_report_path": "stage-20/quality_report.json",
        "quality_report_sha256": hashlib.sha256(
            quality_report_text.encode("utf-8")
        ).hexdigest(),
        "fabrication_flags_path": "stage-20/fabrication_flags.json",
        "fabrication_flags_sha256": hashlib.sha256(
            fabrication_flags_text.encode("utf-8")
        ).hexdigest(),
        "quality_threshold": canonical_decimal(Decimal(str(threshold))),
        "graceful_degradation": canonical_config.research.graceful_degradation,
        "quality_score": canonical_decimal(Decimal(str(score))),
        "generated": _utcnow_iso(),
    }
    output.write_text_atomic(
        "quality_gate_manifest.json",
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )
    final_evidence = load_canonical_experiment_evidence(run_dir)
    if final_evidence != evidence:
        raise Stage20InputBundleError(
            "canonical experiment evidence changed during Stage 20 commit"
        )
    verify_stage20_input_bundle_unchanged(
        run_dir,
        stage20_inputs,
        evidence=final_evidence,
        claim_scope=claim_scope,
    )
    output.assert_canonical()
    replay_stage21_input_bundle(
        quality_report=_bound_output_artifact(
            output, "stage-20", "quality_report.json"
        ),
        fabrication_flags=_bound_output_artifact(
            output, "stage-20", "fabrication_flags.json"
        ),
        quality_gate_manifest=_bound_output_artifact(
            output, "stage-20", "quality_gate_manifest.json"
        ),
        stage20_inputs=stage20_inputs,
        evidence=final_evidence,
        canonical_config=canonical_config,
    )
    output.assert_canonical()


# ---------------------------------------------------------------------------
# Stage 21: Knowledge Archive
# ---------------------------------------------------------------------------


_STAGE21_OWNED_OUTPUTS = (
    "bundle_index.json",
    "archive.md",
    "bundle_index.json.tmp",
    "archive.md.tmp",
)

def _execute_knowledge_archive(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    output: BoundOutputNamespace | None = None
    try:
        output = BoundOutputNamespace.open(run_dir, stage_dir, "stage-21")
        output.invalidate(_STAGE21_OWNED_OUTPUTS)
    except OSError as exc:
        if output is not None:
            output.close()
        return StageResult(
            stage=Stage.KNOWLEDGE_ARCHIVE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 21 output namespace is unsafe: {exc}",
            decision="retry",
        )
    assert output is not None
    try:
        return _execute_knowledge_archive_bound(
            stage_dir,
            run_dir,
            config,
            adapters,
            llm=llm,
            prompts=prompts,
            output=output,
        )
    finally:
        output.close()


def _execute_knowledge_archive_bound(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None,
    prompts: PromptManager | None,
    output: BoundOutputNamespace,
) -> StageResult:
    try:
        evidence = load_canonical_experiment_evidence(run_dir)
        canonical_config = parse_config_snapshot_text(
            evidence.run_config_bytes.decode("utf-8"),
            project_root=run_dir,
            label="canonical experiment config snapshot",
        )
        if semantic_config_sha256(config) != semantic_config_sha256(canonical_config):
            raise ValueError(
                "runtime config semantic generation differs from canonical experiment evidence"
            )
        stage19_inputs = _load_bound_stage19_inputs(run_dir, config, evidence)
        claim_scope = _snapshot_claim_scope(evidence)
        stage20_inputs = load_stage20_input_bundle(
            run_dir,
            stage19_inputs=stage19_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
        stage21_inputs = load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20_inputs,
            evidence=evidence,
            canonical_config=canonical_config,
        )
        revised = stage20_inputs.revised_paper.text()
    except (
        CanonicalExperimentEvidenceError,
        CitationPlanContractError,
        CitationPolicyContractError,
        Stage19InputBundleError,
        Stage20InputBundleError,
        Stage21InputBundleError,
        OSError,
        UnicodeDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        return StageResult(
            stage=Stage.KNOWLEDGE_ARCHIVE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Stage 21 canonical input validation failed: {exc}",
            decision="retry",
        )
    if llm is not None:
        _pm = prompts or PromptManager()
        sp = _pm.for_stage(
            "knowledge_archive",
            evolution_overlay=None,
            preamble=(
                "Research topic from the canonical runtime configuration:\n"
                + config.research.topic
            ),
            decision=(
                "PASSED (validated by the canonical Stage 20 gate)"
                if stage21_inputs.quality_gate_outcome == "passed"
                else "DEGRADED (explicitly authorized by the canonical Stage 20 gate)"
            ),
            analysis=evidence.analysis_text,
            revised=revised[:15000],
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        archive = resp.content
    else:
        archive = f"""# Knowledge Archive

## Lessons Learned
- Preserve strict metric reporting protocol.
- Keep refinement logs aligned with code changes.

## Reproducibility
- Include exact experiment script and schedule.
- Capture run-level JSON metrics.

## Future Work
- Extend robustness and external validity checks.

Generated: {_utcnow_iso()}
"""
    try:
        if not isinstance(archive, str) or not archive.strip():
            raise Stage21InputBundleError("Stage 21 archive response is empty")
        _verify_stage21_sources(
            run_dir,
            config=config,
            evidence=evidence,
            stage19_inputs=stage19_inputs,
            stage20_inputs=stage20_inputs,
            stage21_inputs=stage21_inputs,
            claim_scope=claim_scope,
            canonical_config=canonical_config,
        )
        archive_sha256 = hashlib.sha256(archive.encode("utf-8")).hexdigest()
        index = _build_stage21_bundle_index(
            run_dir,
            evidence=evidence,
            stage19_inputs=stage19_inputs,
            stage20_inputs=stage20_inputs,
            stage21_inputs=stage21_inputs,
            archive_sha256=archive_sha256,
        )
        output.write_text_atomic("archive.md", archive)
        _verify_stage21_sources(
            run_dir,
            config=config,
            evidence=evidence,
            stage19_inputs=stage19_inputs,
            stage20_inputs=stage20_inputs,
            stage21_inputs=stage21_inputs,
            claim_scope=claim_scope,
            canonical_config=canonical_config,
        )
        output.write_text_atomic(
            "bundle_index.json",
            json.dumps(index, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        )
        _verify_stage21_sources(
            run_dir,
            config=config,
            evidence=evidence,
            stage19_inputs=stage19_inputs,
            stage20_inputs=stage20_inputs,
            stage21_inputs=stage21_inputs,
            claim_scope=claim_scope,
            canonical_config=canonical_config,
        )
        output.assert_canonical()
        verify_stage21_output_artifacts(
            archive=_bound_output_artifact(output, "stage-21", "archive.md"),
            index=_bound_output_artifact(output, "stage-21", "bundle_index.json"),
            archive_text=archive,
            expected_index=index,
        )
        output.assert_canonical()
    except (
        CanonicalExperimentEvidenceError,
        CitationPlanContractError,
        CitationPolicyContractError,
        Stage19InputBundleError,
        Stage20InputBundleError,
        Stage21InputBundleError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        cleanup_error = None
        try:
            output.invalidate(_STAGE21_OWNED_OUTPUTS)
        except OSError as cleanup_exc:
            cleanup_error = cleanup_exc
        return StageResult(
            stage=Stage.KNOWLEDGE_ARCHIVE,
            status=StageStatus.FAILED,
            artifacts=(),
            error=(
                f"Stage 21 inputs changed during archival: {exc}"
                + (
                    f"; output invalidation also failed: {cleanup_error}"
                    if cleanup_error is not None
                    else ""
                )
            ),
            decision="retry",
        )
    return StageResult(
        stage=Stage.KNOWLEDGE_ARCHIVE,
        status=StageStatus.DONE,
        artifacts=("archive.md", "bundle_index.json"),
        evidence_refs=("stage-21/archive.md", "stage-21/bundle_index.json"),
    )


def _verify_stage21_sources(
    run_dir: Path,
    *,
    config: RCConfig,
    evidence: CanonicalExperimentEvidence,
    stage19_inputs: Stage19InputBundle,
    stage20_inputs: Stage20InputBundle,
    stage21_inputs: Stage21InputBundle,
    claim_scope: str,
    canonical_config: RCConfig,
) -> None:
    current_evidence = load_canonical_experiment_evidence(run_dir)
    if current_evidence != evidence:
        raise Stage21InputBundleError(
            "canonical experiment evidence changed during Stage 21"
        )
    current_stage19 = _load_bound_stage19_inputs(run_dir, config, current_evidence)
    if current_stage19 != stage19_inputs:
        raise Stage21InputBundleError("Stage 04-18 inputs changed during Stage 21")
    verify_stage20_input_bundle_unchanged(
        run_dir,
        stage20_inputs,
        evidence=current_evidence,
        claim_scope=claim_scope,
    )
    verify_stage21_input_bundle_unchanged(
        run_dir,
        stage21_inputs,
        stage20_inputs=stage20_inputs,
        evidence=current_evidence,
        canonical_config=canonical_config,
    )


def _build_stage21_bundle_index(
    run_dir: Path,
    *,
    evidence: CanonicalExperimentEvidence,
    stage19_inputs: Stage19InputBundle,
    stage20_inputs: Stage20InputBundle,
    stage21_inputs: Stage21InputBundle,
    archive_sha256: str,
) -> dict[str, Any]:
    artifacts: dict[str, str] = {
        evidence.manifest_path: evidence.manifest_sha256,
        stage20_inputs.revised_paper.path: stage20_inputs.revised_paper.sha256,
        stage20_inputs.publication_binding.path: stage20_inputs.publication_binding.sha256,
        stage21_inputs.quality_report.path: stage21_inputs.quality_report.sha256,
        stage21_inputs.fabrication_flags.path: stage21_inputs.fabrication_flags.sha256,
        stage21_inputs.quality_gate_manifest.path: (
            stage21_inputs.quality_gate_manifest.sha256
        ),
        "stage-21/archive.md": archive_sha256,
    }
    for artifact in stage19_inputs.artifacts:
        previous = artifacts.setdefault(artifact.path, artifact.sha256)
        if previous != artifact.sha256:
            raise Stage21InputBundleError(
                f"conflicting Stage 21 artifact identity: {artifact.path}"
            )
    artifact_entries = [
        {"path": path, "sha256": artifacts[path]} for path in sorted(artifacts)
    ]
    return {
        "schema_version": 1,
        "run_id": run_dir.name,
        "generated": _utcnow_iso(),
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage20_inputs.revised_paper.path,
        "source_paper_sha256": stage20_inputs.revised_paper.sha256,
        "stage19_publication_mode": stage20_inputs.publication_mode,
        "stage19_publication_binding_path": stage20_inputs.publication_binding.path,
        "stage19_publication_binding_sha256": stage20_inputs.publication_binding.sha256,
        "quality_report_path": stage21_inputs.quality_report.path,
        "quality_report_sha256": stage21_inputs.quality_report.sha256,
        "fabrication_flags_path": stage21_inputs.fabrication_flags.path,
        "fabrication_flags_sha256": stage21_inputs.fabrication_flags.sha256,
        "quality_gate_manifest_path": stage21_inputs.quality_gate_manifest.path,
        "quality_gate_manifest_sha256": stage21_inputs.quality_gate_manifest.sha256,
        "archive_path": "stage-21/archive.md",
        "archive_sha256": archive_sha256,
        "artifact_count": len(artifact_entries),
        "artifacts": artifact_entries,
    }


# ---------------------------------------------------------------------------
# _sanitize_fabricated_data helper
# ---------------------------------------------------------------------------

def _sanitize_fabricated_data(
    paper: str,
    run_dir: Path,
) -> tuple[str, dict[str, Any]]:
    """Replace unverified numerical data in markdown tables with '---'.

    Loads experiment_summary.json as ground truth, extracts all verified
    metric values, then scans markdown tables in Results/Experiment sections.
    Numbers not matching any verified value (within 1% relative tolerance)
    are replaced with ``---``.

    Returns (sanitized_paper, sanitization_report).
    """
    import re as _re_san

    # --- 1. Build verified values set from experiment_summary.json ---
    # BUG-222: After REFINE cycles, merging ALL stage-14* data creates a
    # permissive registry that validates fabricated numbers from regressed
    # iterations.  Use ONLY the promoted best data as ground truth.
    # experiment_summary_best.json is written by _promote_best_stage14() and
    # contains the single best iteration's data.
    verified_values: set[float] = set()

    def _richness(path: Path) -> int:
        """Score an experiment_summary.json by how many conditions it has."""
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return -1
        if not isinstance(d, dict):
            return -1
        conds = d.get("condition_summaries", {})
        metrics = d.get("metrics_summary", {})
        return len(conds) + len(metrics)

    # BUG-222: Prefer experiment_summary_best.json (promoted best iteration).
    # Only fall back to "richest stage-14*" scanning if best.json is missing
    # (single-iteration runs without REFINE).
    _root_best = run_dir / "experiment_summary_best.json"
    if _root_best.exists() and _richness(_root_best) > 0:
        exp_path = _root_best
    else:
        _candidates = list(run_dir.glob("stage-14*/experiment_summary.json"))
        exp_path = max(_candidates, key=_richness) if _candidates else run_dir / "stage-14" / "experiment_summary.json"

    if exp_path.exists():
        try:
            exp_data = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            exp_data = {}

        def _collect_numbers(obj: Any, depth: int = 0) -> None:
            if depth > 10:
                return
            if isinstance(obj, (int, float)) and not isinstance(obj, bool):
                import math as _math_vv
                if _math_vv.isfinite(float(obj)):
                    verified_values.add(float(obj))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect_numbers(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    _collect_numbers(v, depth + 1)

        # Extract from well-known keys
        for key in (
            "metrics_summary", "condition_summaries", "best_run",
            "condition_metrics", "conditions", "ablation_results",
        ):
            if key in exp_data:
                _collect_numbers(exp_data[key])

    # BUG-222: Removed BUG-206 refinement_log scanning.  The original BUG-206
    # rationale was "Stage 17 injects sandbox metrics, so the sanitizer must
    # recognise them".  But that created a loophole: after REFINE regression,
    # the LLM would cite regressed iteration numbers and the sanitizer would
    # pass them because they were in the refinement log.  Now that Stage 17
    # also uses only the promoted best data (BUG-222), there is no need to
    # whitelist all sandbox metrics here.

    if not verified_values:
        report: dict[str, Any] = {
            "sanitized": False,
            "reason": "no verified values found in experiment_summary.json",
            "tables_processed": 0,
            "numbers_replaced": 0,
        }
        return paper, report

    def _is_verified(num: float) -> bool:
        """Check if num matches any verified value within 1% relative tolerance.

        BUG-R5-20: Also checks percentage/decimal cross-matching
        (e.g., 73.42 in paper vs 0.7342 in experiment, or vice versa).
        """
        for v in verified_values:
            if v == 0.0:
                if abs(num) < 1e-9:
                    return True
            elif abs(num - v) / abs(v) <= 0.01:
                return True
            # Cross-match: num might be percentage form of v (or vice versa)
            elif v != 0.0 and abs(num / 100.0 - v) / abs(v) <= 0.01:
                return True
            elif v != 0.0 and abs(num - v * 100.0) / abs(v * 100.0) <= 0.01:
                return True
        return False

    # --- 2. Find and sanitize markdown tables ---
    # BUG-175: Always-allowed set — common constants, hyperparameters, and
    # structural values that should never be sanitized (matches paper_verifier.py).
    _SANITIZER_ALWAYS_ALLOWED: set[float] = {
        0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0,
        0.5, 0.01, 0.001, 0.0001, 0.1, 0.05, 0.95, 0.99,
        2024.0, 2025.0, 2026.0, 2027.0,
        8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0,
        224.0, 299.0, 384.0,  # Common image sizes
        # BUG-192: Common hyperparameter values
        0.0003, 3e-4, 0.0005, 5e-4, 0.002, 2e-3,  # learning rates
        0.2, 0.3, 0.25, 0.7, 0.6, 0.8,  # clip epsilon, dropout, gradient clip, GCE q, common HP
        0.9, 0.999, 0.9999,  # Adam betas, momentum
        0.02, 0.03,  # weight init std
        1e-5, 1e-6, 1e-8,  # epsilon, weight decay
        300.0, 400.0, 500.0,  # epochs
        4096.0, 8192.0,  # larger batch sizes / hidden dims
    }
    # Match markdown table blocks (header + separator + data rows)
    table_pat = _re_san.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)+"  # one or more pipe-delimited lines
        r")",
        _re_san.MULTILINE,
    )
    # Match numbers in table cells (integers, decimals, percentages, scientific)
    # BUG-175: Also exclude hyphen in lookaround to protect method names like
    # "Cos-200", "StepLR-100" from partial number extraction.
    # BUG-206: Include Unicode hyphens (U+2010 hyphen, U+2011 non-breaking
    # hyphen, U+2013 en-dash) — LLMs frequently emit these instead of ASCII
    # hyphens in model names like "ResNet‑34".
    # BUG-206: Unicode hyphens placed before escaped ASCII hyphen (\\-)
    # to avoid creating unintended character ranges in the class.
    _HYPH = "\u2010\u2011\u2013\\-"  # U+2010 + U+2011 + U+2013 + ASCII hyphen
    num_pat = _re_san.compile(
        f"(?<![a-zA-Z_{_HYPH}])"  # not preceded by letter/underscore/any-hyphen
        r"(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
        r"(%?)"  # optional percent
        f"(?![a-zA-Z_{_HYPH}])"  # not followed by letter/underscore/any-hyphen
    )

    numbers_replaced = 0
    numbers_kept = 0
    tables_processed = 0
    replaced_values: list[str] = []

    # Shared helper — verifies a single number match, replaces if unverified.
    # Used by both markdown-table and LaTeX-tabular sanitizers.
    def _replace_num(m: _re_san.Match[str]) -> str:
        nonlocal numbers_replaced, numbers_kept
        num_str = m.group(1)
        pct = m.group(2)
        try:
            val = float(num_str)
        except ValueError:
            return m.group(0)
        # BUG-175: Always allow common constants / hyperparameters
        if val in _SANITIZER_ALWAYS_ALLOWED:
            numbers_kept += 1
            return m.group(0)
        # BUG-175: Small integer exemption — counts, indices,
        # epoch numbers, etc. (≤ 20 auto-pass)
        if val == int(val) and abs(val) <= 20:
            numbers_kept += 1
            return m.group(0)
        if _is_verified(val):
            numbers_kept += 1
            return m.group(0)
        numbers_replaced += 1
        replaced_values.append(num_str + pct)
        return "---"

    def _sanitize_table(match: _re_san.Match[str]) -> str:
        nonlocal numbers_replaced, numbers_kept, tables_processed
        table_text = match.group(0)
        lines = table_text.split("\n")

        # Check if this looks like a results/experiment table
        # (heuristic: has a separator row with dashes)
        has_separator = any(
            _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            for line in lines
        )
        if not has_separator:
            return table_text

        # BUG-192: Detect hyperparameter/config tables and SKIP sanitization.
        # These tables contain design choices, not experimental results.
        _HP_TABLE_KW = {
            "hyperparameter", "hyper-parameter", "configuration", "config",
            "setting", "parameter", "learning rate", "lr", "batch size",
            "optimizer", "architecture", "schedule", "warmup", "decay",
            "dropout", "weight decay", "momentum", "epsilon", "clip",
        }
        # BUG-224: Statistical analysis tables contain derived values
        # (t-statistics, p-values, effect sizes) that are computed from
        # the experiment data but never appear in experiment_summary.json.
        # These tables should NOT be sanitized.
        _STAT_TABLE_KW = {
            "t-statistic", "t-stat", "t statistic", "p-value", "p value",
            "paired", "cohen", "effect size", "wilcoxon", "mann-whitney",
            "statistical", "significance", "confidence interval",
        }
        _RESULT_TABLE_KW = {
            "accuracy", "acc", "loss", "f1", "auroc", "auc", "precision",
            "recall", "bleu", "rouge", "reward", "return", "rmse", "mae",
            "mse", "error", "score", "metric", "performance", "improvement",
            "top-1", "top1", "top-5", "top5",
        }
        _header_lower = lines[0].lower() if lines else ""
        _is_hp_table = any(kw in _header_lower for kw in _HP_TABLE_KW)
        _is_result_table = any(kw in _header_lower for kw in _RESULT_TABLE_KW)
        # BUG-224: Statistical analysis tables (t-tests, p-values) contain
        # derived values that are never in experiment_summary.json.
        _is_stat_table = any(kw in _header_lower for kw in _STAT_TABLE_KW)
        if _is_hp_table and not _is_result_table:
            return table_text  # Skip sanitization for HP/config tables
        if _is_stat_table:
            return table_text  # Skip sanitization for statistical test tables

        # BUG-184: Per-column HP detection — classify each column header
        # as HP-type (skip sanitization) or result-type (sanitize).
        # This handles mixed tables like "| Method | LR | Acc | F1 |"
        # where LR should be preserved but Acc/F1 are verified.
        _HP_COL_KW = {
            "lr", "learning rate", "batch", "epoch", "optimizer",
            "schedule", "warmup", "decay", "dropout", "momentum",
            "clip", "epsilon", "eps", "beta", "alpha", "gamma",
            "lambda", "weight decay", "wd", "temperature", "temp",
            "hidden", "dim", "layers", "heads", "steps", "iterations",
            "seed", "patience", "#param", "params", "size", "depth",
            "width", "channels", "kernel", "stride", "padding",
            # BUG-224: Statistical test columns (derived, not in experiment data)
            "t-stat", "t stat", "p-value", "p value", "p-val",
            "cohen", "effect", "ci lower", "ci upper", "difference",
        }
        _hp_cols: set[int] = set()  # column indices that are HP columns
        if lines:
            _hdr_cells = lines[0].split("|")
            for _ci, _hc in enumerate(_hdr_cells):
                _hc_low = _hc.strip().lower()
                if any(kw in _hc_low for kw in _HP_COL_KW):
                    _hp_cols.add(_ci)

        tables_processed += 1
        sanitized_lines: list[str] = []
        for i, line in enumerate(lines):
            # Skip header row and separator row
            is_separator = bool(
                _re_san.match(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", line)
            )
            is_header = i == 0  # first line is typically the header
            if is_separator or is_header:
                sanitized_lines.append(line)
                continue

            # BUG-175: Split by pipe and only sanitize cells after
            # the first data column (which typically contains method
            # names, condition labels, etc.)
            cells = line.split("|")
            sanitized_cells: list[str] = []

            for ci, cell in enumerate(cells):
                # Skip first non-empty cell (method/label column),
                # empty edge cells, and BUG-184 HP-classified columns
                if ci <= 1 or not cell.strip() or ci in _hp_cols:
                    sanitized_cells.append(cell)
                else:
                    sanitized_cells.append(
                        num_pat.sub(_replace_num, cell)
                    )
            sanitized_lines.append("|".join(sanitized_cells))
        return "\n".join(sanitized_lines)

    sanitized = table_pat.sub(_sanitize_table, paper)

    # --- BUG-211: LaTeX tabular sanitization ---
    # LLMs sometimes write results in LaTeX \begin{tabular} format inside
    # the markdown paper (often within ```latex fences).  The markdown
    # table regex above misses these entirely, allowing fabricated numbers
    # to pass through unchecked.
    latex_tab_pat = _re_san.compile(
        r"(\\begin\{tabular\}.*?\\end\{tabular\})",
        _re_san.DOTALL,
    )

    # Keywords for HP-table vs result-table classification (reuse from above)
    _LTX_HP_KW = {
        "hyperparameter", "hyper-parameter", "configuration", "config",
        "setting", "learning rate", "lr", "batch size", "optimizer",
    }
    _LTX_RESULT_KW = {
        "accuracy", "acc", "loss", "f1", "auroc", "auc", "precision",
        "recall", "reward", "score", "metric", "performance", "result",
    }
    # BUG-224: Statistical analysis LaTeX tables — derived values
    _LTX_STAT_KW = {
        "t-statistic", "t-stat", "t statistic", "p-value", "p value",
        "paired", "cohen", "effect size", "statistical", "significance",
    }

    def _sanitize_latex_table(match: _re_san.Match[str]) -> str:
        nonlocal tables_processed
        block = match.group(0)

        # Heuristic: look at the first ~300 chars (column spec + header row)
        # to decide HP vs result table.  Also check preceding \caption if
        # the match is part of a \begin{table} environment — we can look
        # backwards a bit in the full text for the caption.
        _start = match.start()
        _context = sanitized[max(0, _start - 300):_start + 300].lower()
        _is_hp = any(kw in _context for kw in _LTX_HP_KW)
        _is_res = any(kw in _context for kw in _LTX_RESULT_KW)
        # BUG-224: Statistical test tables — derived values not in experiment data
        _is_stat = any(kw in _context for kw in _LTX_STAT_KW)
        if _is_hp and not _is_res:
            return block  # HP/config table — skip
        if _is_stat:
            return block  # Statistical analysis table — skip

        tables_processed += 1

        # Split into rows by \\ (LaTeX row separator).
        # We split on \\ but keep the delimiter so we can reconstruct.
        parts = _re_san.split(r"(\\\\)", block)
        result_parts: list[str] = []
        _seen_midrule = False

        for part in parts:
            # Preserve row separators as-is
            if part == "\\\\":
                result_parts.append(part)
                continue

            _stripped = part.strip()
            # Rule lines — no numbers to sanitize
            if _re_san.search(
                r"\\(hline|toprule|midrule|bottomrule|cline|cmidrule)",
                _stripped,
            ):
                if "midrule" in _stripped or "hline" in _stripped:
                    _seen_midrule = True
                result_parts.append(part)
                continue

            # Column spec line (contains \begin{tabular}{...})
            if r"\begin{tabular}" in part:
                result_parts.append(part)
                continue

            # End line
            if r"\end{tabular}" in part:
                result_parts.append(part)
                continue

            # Header row: rows before the first \midrule/\hline
            if not _seen_midrule:
                result_parts.append(part)
                continue

            # Data row — split by & and sanitize cells after the first
            cells = part.split("&")
            sanitized_cells: list[str] = []
            for ci, cell in enumerate(cells):
                if ci == 0:
                    # First cell is method/condition name — preserve
                    sanitized_cells.append(cell)
                else:
                    sanitized_cells.append(num_pat.sub(_replace_num, cell))
            result_parts.append("&".join(sanitized_cells))

        return "".join(result_parts)

    sanitized = latex_tab_pat.sub(_sanitize_latex_table, sanitized)

    # --- Improvement F: Prose-level anti-fabrication ---
    # Scan Results/Experiments sections for inline numeric claims like
    # "achieved 94.2% accuracy" or "obtained an AUROC of 0.87".
    # Replace unverified numbers with "[value removed]".
    prose_numbers_replaced = 0
    _prose_pattern = _re_san.compile(
        r"(?:achiev|obtain|reach|attain|yield|report|record|produc|demonstrat|show|observ)"
        r"(?:ed|es|ing|s)?\s+"
        r"(?:an?\s+)?(?:\w+\s+)?(?:of\s+)?"
        r"(\d+\.?\d*)\s*"
        r"(%|\\%)?",
        _re_san.IGNORECASE,
    )
    # Only process lines in Results/Experiments sections
    _in_results_section = False
    _results_headers = _re_san.compile(
        r"^#{1,3}\s*(Results|Experiments|Experimental|Evaluation|Ablation)",
        _re_san.IGNORECASE,
    )
    _any_header = _re_san.compile(r"^#{1,3}\s+")
    _sanitized_lines = []
    for _line in sanitized.split("\n"):
        if _results_headers.match(_line):
            _in_results_section = True
        elif _any_header.match(_line) and _in_results_section:
            # Check if we're leaving Results for a different top-level section
            _header_text = _line.lstrip("#").strip().lower()
            if _header_text and not any(kw in _header_text for kw in
                    ("result", "experiment", "ablation", "evaluation", "comparison")):
                _in_results_section = False
        if _in_results_section and "|" not in _line:  # skip table rows
            def _replace_prose_num(m: _re_san.Match[str]) -> str:
                nonlocal prose_numbers_replaced
                num_str = m.group(1)
                try:
                    val = float(num_str)
                except ValueError:
                    return m.group(0)
                # Skip common constants / small integers
                if val in _SANITIZER_ALWAYS_ALLOWED:
                    return m.group(0)
                if val == int(val) and abs(val) <= 20:
                    return m.group(0)
                if _is_verified(val):
                    return m.group(0)
                prose_numbers_replaced += 1
                return m.group(0).replace(num_str + (m.group(2) or ""), "[value removed]")
            _line = _prose_pattern.sub(_replace_prose_num, _line)
        _sanitized_lines.append(_line)
    sanitized = "\n".join(_sanitized_lines)

    report = {
        "sanitized": numbers_replaced > 0 or prose_numbers_replaced > 0,
        "tables_processed": tables_processed,
        "numbers_replaced": numbers_replaced,
        "numbers_kept": numbers_kept,
        "prose_numbers_replaced": prose_numbers_replaced,
        "verified_values_count": len(verified_values),
        "replaced_samples": replaced_values[:20],
        "generated": _utcnow_iso(),
    }
    return sanitized, report


# ---------------------------------------------------------------------------
# BUG-176: Missing citation resolution
# BUG-194: Validate search results to avoid replacing correct entries with
#           garbage.  Previous code searched by cite-key fragments (e.g.
#           "he 2016 deep") which returned completely unrelated papers.
#           Fix: (1) consult seminal_papers.yaml first, (2) require title-
#           similarity validation for API results, (3) build better queries.
# ---------------------------------------------------------------------------

# Minimum title-similarity between search result and expected title/query
# for a result to be accepted.  Prevents "Jokowi and the New Developmentalism"
# from replacing "Deep Residual Learning for Image Recognition".
_CITATION_RESOLVE_MIN_SIMILARITY = 0.30


def _load_seminal_papers_by_key() -> dict[str, dict]:
    """Load seminal_papers.yaml and index by cite_key.

    Returns dict like::

        {"he2016deep": {"title": "Deep Residual Learning...", "authors": "He et al.", ...}, ...}

    Returns empty dict on any failure (missing file, bad YAML, etc.).
    """
    try:
        from researchclaw.data import _load_all as _load_seminal_all
        all_papers = _load_seminal_all()
        return {p["cite_key"]: p for p in all_papers if "cite_key" in p}
    except Exception:  # noqa: BLE001
        return {}


def _seminal_to_bibtex(paper: dict, cite_key: str) -> str:
    """Convert a seminal_papers.yaml entry dict to a BibTeX string."""
    title = paper.get("title", "Unknown")
    authors = paper.get("authors", "Unknown")
    year = paper.get("year", "")
    venue = paper.get("venue", "")

    # Decide entry type
    venue_lower = (venue or "").lower()
    is_conf = any(kw in venue_lower for kw in (
        "neurips", "nips", "icml", "iclr", "cvpr", "eccv", "iccv",
        "aaai", "acl", "emnlp", "naacl", "sigir", "kdd", "www",
        "ijcai", "conference", "proc", "workshop",
    ))
    if is_conf:
        return (
            f"@inproceedings{{{cite_key},\n"
            f"  title = {{{title}}},\n"
            f"  author = {{{authors}}},\n"
            f"  year = {{{year}}},\n"
            f"  booktitle = {{{venue}}},\n"
            f"}}"
        )
    return (
        f"@article{{{cite_key},\n"
        f"  title = {{{title}}},\n"
        f"  author = {{{authors}}},\n"
        f"  year = {{{year}}},\n"
        f"  journal = {{{venue}}},\n"
        f"}}"
    )


def _resolve_missing_citations(
    missing_keys: set[str],
    existing_bib: str,
) -> tuple[set[str], list[str]]:
    """Try to find BibTeX entries for citation keys not in references.bib.

    Parses each cite_key (e.g. ``hendrycks2017baseline``) into an author name
    and year, then searches academic APIs.  Returns ``(resolved_keys,
    new_bib_entries)`` where each entry is a complete BibTeX string.

    BUG-194 fix: Three-layer resolution strategy:
      1. **Seminal lookup** — check seminal_papers.yaml (zero API calls, exact match)
      2. **API search with validation** — search Semantic Scholar / arXiv, but ONLY
         accept results whose title has ≥ 30% word overlap with query terms.
         Previously any year-matching result was blindly accepted, causing
         foundational papers to be replaced with garbage.
      3. **Skip** — if no confident match, leave the citation unresolved rather
         than inject a wrong paper.

    Gracefully returns empty results on any network failure.
    """
    import re as _re176
    import time as _time176

    resolved: set[str] = set()
    new_entries: list[str] = []

    def _parse_cite_key(key: str) -> tuple[str, str, str]:
        """Extract (author, year, keyword_hint) from a citation key.

        Common patterns:
          ``he2016deep``       → ("he", "2016", "deep")
          ``vaswani2017attention`` → ("vaswani", "2017", "attention")
          ``goodfellow2014generative`` → ("goodfellow", "2014", "generative")
        """
        m = _re176.match(r"([a-zA-Z]+?)(\d{4})(.*)", key)
        if m:
            return m.group(1), m.group(2), m.group(3)
        return key, "", ""

    def _title_word_overlap(title: str, query_words: list[str]) -> float:
        """Word-overlap score between a paper title and query keywords.

        Returns fraction of query words found in the title (0.0–1.0).
        Used to validate that a search result is actually relevant.
        """
        if not query_words:
            return 0.0
        title_lower = set(
            _re176.sub(r"[^a-z0-9\s]", "", title.lower()).split()
        ) - {""}
        if not title_lower:
            return 0.0
        matched = sum(1 for w in query_words if w.lower() in title_lower)
        return matched / len(query_words)

    # --- Layer 1: Seminal papers lookup (no API calls) ---
    seminal_by_key = _load_seminal_papers_by_key()

    for key in sorted(missing_keys):
        if key in seminal_by_key and key not in existing_bib:
            sp = seminal_by_key[key]
            bib_entry = _seminal_to_bibtex(sp, key)
            new_entries.append(bib_entry)
            resolved.add(key)
            logger.info(
                "BUG-194: Resolved %r via seminal_papers.yaml → %r (%s)",
                key, sp.get("title", "")[:60], sp.get("year", ""),
            )

    # Remaining keys that weren't in the seminal database AND aren't already
    # present in the existing bib (no point re-resolving keys we already have).
    remaining = sorted(
        k for k in (missing_keys - resolved) if k not in existing_bib
    )
    if not remaining:
        return resolved, new_entries

    # --- Layer 2: API search with title-similarity validation ---
    try:
        from researchclaw.literature.search import search_papers
    except ImportError:
        logger.debug("BUG-176: literature.search not available, skipping resolution")
        return resolved, new_entries

    for key in remaining:
        author, year, hint = _parse_cite_key(key)
        if not author or not year:
            continue

        # BUG-194: Build a better search query.
        # Instead of "he 2016 deep", use "he deep residual learning 2016" or
        # at minimum, split camelCase hints into separate words.
        # Split hint on word boundaries (camelCase or underscore).
        hint_words = _re176.findall(r"[a-zA-Z]+", hint) if hint else []
        # The query words used for validation
        query_words = [author] + hint_words

        # Build search query: author + hint words + year (year helps but isn't
        # the primary discriminator anymore)
        query_parts = [author] + hint_words + [year]
        query = " ".join(query_parts)

        try:
            results = search_papers(query, limit=5, deduplicate=True)
        except Exception as exc:
            logger.debug("BUG-176: Search failed for %r: %s", key, exc)
            continue

        if not results:
            logger.debug(
                "BUG-194: No search results for %r (query=%r), skipping",
                key, query,
            )
            continue

        # BUG-194: Find best match by title-word-overlap AND year match.
        # Previously the code just took the first year-matching result.
        best = None
        best_score = -1.0
        for paper in results:
            overlap = _title_word_overlap(paper.title, query_words)
            year_bonus = 0.2 if str(paper.year) == year else 0.0
            # Also give bonus for author name appearing in paper.authors
            author_bonus = 0.0
            if any(author.lower() in a.name.lower() for a in paper.authors):
                author_bonus = 0.2
            score = overlap + year_bonus + author_bonus
            if score > best_score:
                best_score = score
                best = paper

        if best is None:
            continue

        # BUG-194: Validate the result — require minimum similarity.
        # This is the KEY fix: previously ANY result was accepted blindly.
        overlap = _title_word_overlap(best.title, query_words)
        if overlap < _CITATION_RESOLVE_MIN_SIMILARITY:
            logger.info(
                "BUG-194: Rejecting search result for %r — title %r has "
                "too-low overlap (%.2f < %.2f) with query words %r",
                key, best.title[:60], overlap,
                _CITATION_RESOLVE_MIN_SIMILARITY, query_words,
            )
            continue

        # Year must also match (or be within 1 year — sometimes conferences
        # vs arXiv preprint have different years)
        if year and best.year:
            year_diff = abs(int(year) - int(best.year))
            if year_diff > 1:
                logger.info(
                    "BUG-194: Rejecting search result for %r — year mismatch "
                    "(%s vs %s, diff=%d)",
                    key, year, best.year, year_diff,
                )
                continue

        # Generate BibTeX with the ORIGINAL cite_key (so \cite{key} works)
        bib_entry = best.to_bibtex()
        # Replace the auto-generated cite_key with the one used in the paper
        orig_key_match = _re176.match(r"@(\w+)\{([^,]+),", bib_entry)
        if orig_key_match:
            bib_entry = bib_entry.replace(
                f"@{orig_key_match.group(1)}{{{orig_key_match.group(2)},",
                f"@{orig_key_match.group(1)}{{{key},",
                1,
            )

        # Verify entry doesn't duplicate an existing key
        if key not in existing_bib:
            new_entries.append(bib_entry)
            resolved.add(key)
            logger.info(
                "BUG-194: Resolved %r via API → %r (%s, overlap=%.2f)",
                key, best.title[:60], best.year, overlap,
            )
        else:
            logger.debug(
                "BUG-194: Key %r already in bib, skipping API result", key,
            )

        # Rate limit: 0.5s between API calls
        _time176.sleep(0.5)

    return resolved, new_entries


# ---------------------------------------------------------------------------
# Stage 22: Export & Publish
# ---------------------------------------------------------------------------

def _execute_export_publish(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    """Publish Stage 22 solely from replayed canonical inputs."""

    del adapters, llm, prompts
    try:
        with BoundOutputNamespace.open(run_dir, stage_dir, "stage-22") as namespace:
            namespace.invalidate(("stage22_export_manifest.json",))
            namespace.reset_flat_namespace()
        bundle = load_stage22_input_bundle(run_dir, config)
        artifacts = export_canonical_stage22(
            run_dir,
            stage_dir,
            bundle=bundle,
            runtime_config=config,
            generated=_utcnow_iso(),
        )
    except Exception as exc:  # noqa: BLE001
        try:
            with BoundOutputNamespace.open(
                run_dir, stage_dir, "stage-22"
            ) as namespace:
                namespace.invalidate(("stage22_export_manifest.json",))
                namespace.reset_flat_namespace()
        except OSError as cleanup_error:
            exc.add_note(f"Stage 22 output cleanup also failed: {cleanup_error}")
        return StageResult(
            stage=Stage.EXPORT_PUBLISH,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Canonical Stage 22 export failed: {exc}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.EXPORT_PUBLISH,
        status=StageStatus.DONE,
        artifacts=artifacts,
        evidence_refs=tuple(
            f"stage-22/{artifact}" for artifact in artifacts
        ),
    )


def _execute_export_publish_legacy_disabled(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    raise PermissionError(
        "legacy Stage 22 producer is permanently disabled; use canonical export"
    )


def _check_citation_relevance(
    llm: Any,
    topic: str,
    results: list[Any],
) -> dict[str, float | None]:
    """Use LLM to assess relevance of each citation to the research topic.

    Returns a dict mapping cite_key → relevance score (0.0–1.0).
    Processes citations in batches of 30 to handle large bibliographies.
    """
    citation_lines = []
    for cr in results:
        citation_lines.append(f"- [{cr.cite_key}] \"{cr.title}\"")
    if not citation_lines:
        return {}

    all_scores: dict[str, float] = {}
    _BATCH_SIZE = 30

    for batch_start in range(0, len(citation_lines), _BATCH_SIZE):
        batch = citation_lines[batch_start:batch_start + _BATCH_SIZE]
        citations_text = "\n".join(batch)

        prompt = (
            f"Research topic: {topic}\n\n"
            f"Rate the relevance of each citation to the research topic "
            f"on a scale of 0.0 to 1.0.\n"
            f"Return ONLY a JSON object mapping cite_key to relevance score.\n"
            f"Example: {{\"smith2020\": 0.9, \"jones2019\": 0.2}}\n\n"
            f"Citations:\n{citations_text}"
        )

        resp = llm.chat(
            [{"role": "user", "content": prompt}],
            system="You assess citation relevance. Return only valid JSON.",
            json_mode=True,
        )
        try:
            parsed = json.loads(
                resp.content,
                object_pairs_hook=_reject_duplicate_relevance_keys,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"citation relevance response is invalid for batch {batch_start // _BATCH_SIZE + 1}: {exc}"
            ) from exc
        expected_keys = {
            result.cite_key
            for result in results[batch_start:batch_start + _BATCH_SIZE]
        }
        if not isinstance(parsed, dict) or set(parsed) != expected_keys:
            raise ValueError(
                "citation relevance response key closure mismatch: "
                f"expected={sorted(expected_keys)}, actual={sorted(parsed) if isinstance(parsed, dict) else []}"
            )
        for key, value in parsed.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"invalid relevance score for {key}")
            all_scores[key] = float(value)

    return all_scores


def _reject_duplicate_relevance_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate relevance key: {key}")
        result[key] = value
    return result


def _remove_bibtex_entries(bib_text: str, keys_to_remove: set[str]) -> str:
    """Remove BibTeX entries whose keys are in *keys_to_remove*."""
    kept: list[str] = []
    for m in re.finditer(r"@\w+\{([^,]+),", bib_text):
        key = m.group(1).strip()
        if key in keys_to_remove:
            continue
        # Find the full entry (from @ to the next @ or end)
        start = m.start()
        # Find balanced braces
        depth = 0
        end = start
        for i in range(start, len(bib_text)):
            if bib_text[i] == "{":
                depth += 1
            elif bib_text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            kept.append(bib_text[start:end])
    return "\n\n".join(kept) + "\n" if kept else ""


# ---------------------------------------------------------------------------
# Stage 23: Citation Verify
# ---------------------------------------------------------------------------

def _execute_citation_verify(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    from researchclaw.literature.verify import (
        VerifyStatus,
        filter_verified_bibtex,
        verify_citations,
    )

    try:
        for owned_name in (
            "verification_report.json",
            "references_verified.bib",
            "paper_final_verified.md",
            "bib_strip_warning.json",
        ):
            owned_path = stage_dir / owned_name
            if owned_path.is_symlink() or owned_path.exists():
                owned_path.unlink()
    except OSError as exc:
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Could not clean stale Stage 23 outputs: {exc}",
            decision="retry",
        )

    paper_path = run_dir / "stage-22" / "paper_final.md"
    try:
        if paper_path.is_symlink() or not paper_path.is_file():
            raise OSError("canonical stage-22/paper_final.md is missing or unsafe")
        paper_text = paper_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=(),
            error=f"Could not read canonical final paper: {exc}",
            decision="retry",
        )
    cited_keys = extract_citation_keys(paper_text)
    has_plan = (run_dir / "stage-16" / "citation_plan.json").is_file()
    try:
        bib_text = load_canonical_bibliography(run_dir)
    except CitationPlanContractError as exc:
        if cited_keys or has_plan:
            return StageResult(
                stage=Stage.CITATION_VERIFY,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Canonical bibliography is invalid: {exc}",
                decision="retry",
            )
        bib_text = ""
    if cited_keys or has_plan:
        try:
            validate_final_paper_citations(run_dir, config, paper_text)
        except CitationPlanContractError as exc:
            return StageResult(
                stage=Stage.CITATION_VERIFY,
                status=StageStatus.FAILED,
                artifacts=(),
                error=f"Evidence-bound final citation closure failed: {exc}",
                decision="retry",
            )

    if not bib_text.strip():
        # v2 fail-closed: if the paper cites keys but there is no bib,
        # verification CANNOT succeed. Previously this wrote
        # integrity_score=1.0 and returned DONE — a fail-open path.
        _cited_keys_no_bib = set(cited_keys)
        has_citations = bool(_cited_keys_no_bib)
        report_data = {
            "summary": {
                "total": 0,
                "verified": 0,
                "suspicious": 0,
                "hallucinated": 0,
                "skipped": 0,
                # No bib + citations in paper = zero integrity, not perfect.
                "integrity_score": 0.0 if has_citations else 1.0,
                "missing_bib": True,
                "cited_keys_without_bib": sorted(_cited_keys_no_bib)[:50],
            },
            "results": [],
            "note": (
                "No references.bib found, but the paper cites "
                f"{len(_cited_keys_no_bib)} key(s) — verification impossible."
                if has_citations
                else "No references.bib found and no citations in paper."
            ),
        }
        (stage_dir / "verification_report.json").write_text(
            json.dumps(report_data, indent=2), encoding="utf-8"
        )
        (stage_dir / "references_verified.bib").write_text(
            "% No references to verify\n", encoding="utf-8"
        )
        # Always write paper_final_verified.md so deliverables packaging gets
        # the latest paper (not a stale copy from a previous run)
        if paper_text.strip():
            (stage_dir / "paper_final_verified.md").write_text(
                paper_text, encoding="utf-8"
            )
        if has_citations:
            return StageResult(
                stage=Stage.CITATION_VERIFY,
                status=StageStatus.FAILED,
                artifacts=("verification_report.json", "references_verified.bib"),
                evidence_refs=("stage-23/verification_report.json",),
                error=(
                    f"Paper cites {len(_cited_keys_no_bib)} key(s) but no "
                    "references.bib exists — citations cannot be verified."
                ),
                decision="retry",
            )
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.DONE,
            artifacts=("verification_report.json", "references_verified.bib"),
            evidence_refs=(
                "stage-23/verification_report.json",
                "stage-23/references_verified.bib",
            ),
        )

    from researchclaw.literature.verify import parse_bibtex_entries
    all_bib_keys = {
        str(entry.get("key") or "").strip()
        for entry in parse_bibtex_entries(bib_text)
        if str(entry.get("key") or "").strip()
    }
    bounded_bib = _remove_bibtex_entries(bib_text, all_bib_keys - set(cited_keys))
    bounded_entries = parse_bibtex_entries(bounded_bib)
    bounded_keys = {
        str(entry.get("key") or "").strip()
        for entry in bounded_entries
        if str(entry.get("key") or "").strip()
    }
    if bounded_keys != set(cited_keys) or len(bounded_entries) != len(cited_keys):
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=(),
            error="Bounded bibliography does not exactly match final cited keys",
            decision="retry",
        )

    s2_api_key = getattr(config.llm, "s2_api_key", "") or ""
    _n_entries = len(bounded_entries)
    logger.info(
        "[citation-verify] Verifying %d references "
        "(DOI→CrossRef > OpenAlex > arXiv > S2)…",
        _n_entries,
    )
    report = verify_citations(bounded_bib, s2_api_key=s2_api_key)
    logger.info(
        "[citation-verify] Done: %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity: %.0f%%)",
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    result_keys = {result.cite_key for result in report.results}
    status_counts = {
        VerifyStatus.VERIFIED: sum(
            result.status == VerifyStatus.VERIFIED for result in report.results
        ),
        VerifyStatus.SUSPICIOUS: sum(
            result.status == VerifyStatus.SUSPICIOUS for result in report.results
        ),
        VerifyStatus.HALLUCINATED: sum(
            result.status == VerifyStatus.HALLUCINATED for result in report.results
        ),
        VerifyStatus.SKIPPED: sum(
            result.status == VerifyStatus.SKIPPED for result in report.results
        ),
    }
    if (
        report.total != len(cited_keys)
        or len(report.results) != len(cited_keys)
        or result_keys != set(cited_keys)
        or report.verified != status_counts[VerifyStatus.VERIFIED]
        or report.suspicious != status_counts[VerifyStatus.SUSPICIOUS]
        or report.hallucinated != status_counts[VerifyStatus.HALLUCINATED]
        or report.skipped != status_counts[VerifyStatus.SKIPPED]
    ):
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=(),
            error="Citation verification result closure mismatch",
            decision="retry",
        )

    relevance_error = ""
    if llm is not None and report.results:
        try:
            relevance_scores = _check_citation_relevance(
                llm, config.research.topic, report.results
            )
        except (RuntimeError, ValueError) as exc:
            relevance_error = str(exc)
        else:
            for result in report.results:
                result.relevance_score = relevance_scores[result.cite_key]

    relevance_threshold = 0.5
    hallucinated_keys = sorted(
        result.cite_key
        for result in report.results
        if result.status == VerifyStatus.HALLUCINATED
    )
    suspicious_keys = sorted(
        result.cite_key
        for result in report.results
        if result.status == VerifyStatus.SUSPICIOUS
    )
    skipped_keys = sorted(
        result.cite_key
        for result in report.results
        if result.status == VerifyStatus.SKIPPED
    )
    unscored_keys = sorted(
        result.cite_key
        for result in report.results
        if result.relevance_score is None
    )
    low_relevance_keys = sorted(
        result.cite_key
        for result in report.results
        if result.relevance_score is not None
        and result.relevance_score < relevance_threshold
    )
    strict_scope = config.experiment.claim_scope != "pipeline_validation"
    fatal = bool(hallucinated_keys) or (
        strict_scope
        and bool(
            suspicious_keys
            or skipped_keys
            or unscored_keys
            or low_relevance_keys
            or relevance_error
        )
    )
    degraded = not fatal and bool(
        suspicious_keys
        or skipped_keys
        or unscored_keys
        or low_relevance_keys
        or relevance_error
    )
    report_payload = report.to_dict()
    summary = report_payload["summary"]
    if isinstance(summary, dict):
        summary.update(
            {
                "claim_scope": config.experiment.claim_scope,
                "cited_keys": sorted(cited_keys),
                "verification_complete": not (
                    hallucinated_keys or suspicious_keys or skipped_keys
                ),
                "relevance_complete": not unscored_keys and not relevance_error,
                "relevance_threshold": relevance_threshold,
                "hallucinated_keys": hallucinated_keys,
                "suspicious_keys": suspicious_keys,
                "skipped_keys": skipped_keys,
                "unscored_keys": unscored_keys,
                "low_relevance_keys": low_relevance_keys,
                "relevance_error": relevance_error or None,
                "degraded": degraded,
                "fatal": fatal,
            }
        )
    (stage_dir / "verification_report.json").write_text(
        json.dumps(report_payload, indent=2), encoding="utf-8"
    )

    verified_bib = filter_verified_bibtex(
        bounded_bib, report, include_suspicious=True
    )

    # v2 (supersedes BUG-26): NEVER fall back to the unverified original bib.
    # Restoring bib_text wholesale re-admits every hallucinated/suspicious
    # entry that verification just removed — the exact fail-open path a
    # release gate exists to prevent. Heavy stripping (e.g. rate limiting)
    # is recorded as evidence instead, and the missing-verified-entries gate
    # in release_check blocks the run.
    original_count = len(re.findall(r"@\w+\{", bounded_bib))
    verified_count = len(re.findall(r"@\w+\{", verified_bib))
    if original_count > 0 and verified_count < original_count * 0.5:
        logger.warning(
            "Stage 23: Verification stripped %d→%d entries (>50%% loss). "
            "NOT restoring the unverified original bib; run is not "
            "release-eligible until verification succeeds.",
            original_count, verified_count,
        )
        try:
            (stage_dir / "bib_strip_warning.json").write_text(
                json.dumps(
                    {
                        "original_entries": original_count,
                        "verified_entries": verified_count,
                        "strip_ratio": round(
                            1 - verified_count / max(original_count, 1), 3
                        ),
                        "note": (
                            "More than 50% of bib entries failed verification "
                            "or were rate-limited. Original bib was NOT restored."
                        ),
                        "generated": _utcnow_iso(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # IMP-1: Also prune uncited entries from verified bib
    # BUG-182: Also scan LaTeX paper.tex (not just Markdown) for \cite{} keys.
    # The Markdown version may use [key] notation while LaTeX uses \cite{key}.
    if paper_text.strip():
        _vbib_keys = set(re.findall(r"@\w+\{([^,]+),", verified_bib))
        _cited_in_paper: set[str] = set()
        _cited_in_paper.update(
            re.findall(r"\[([a-zA-Z]+\d{4}[a-zA-Z0-9_-]*)\]", paper_text)
        )
        for _cm in re.finditer(r"\\cite\{([^}]+)\}", paper_text):
            _cited_in_paper.update(
                k.strip() for k in _cm.group(1).split(",")
            )
        # BUG-182: Also read stage-22/paper.tex for \cite{} keys
        _latex_paper = stage_dir.parent / "stage-22" / "paper.tex"
        if not _latex_paper.is_symlink() and _latex_paper.is_file():
            try:
                _latex_text = _latex_paper.read_text(encoding="utf-8")
                for _cm in re.finditer(r"\\cite[pt]?\{([^}]+)\}", _latex_text):
                    _cited_in_paper.update(
                        k.strip() for k in _cm.group(1).split(",")
                    )
            except OSError:
                pass
        _uncited_vbib = _vbib_keys - _cited_in_paper
        if _uncited_vbib:
            verified_bib = _remove_bibtex_entries(verified_bib, _uncited_vbib)
            logger.info(
                "Stage 23: Pruned %d uncited entries from verified bib "
                "(kept %d)",
                len(_uncited_vbib),
                len(_vbib_keys) - len(_uncited_vbib),
            )

    # BUG-100: If all entries were filtered out (low-relevance + uncited pruning),
    # write a comment instead of an empty file to avoid "Missing or empty output" error.
    if not verified_bib.strip():
        verified_bib = "% All citations were filtered out during verification\n"
        logger.warning(
            "Stage 23: All BibTeX entries filtered out — writing placeholder"
        )

    (stage_dir / "references_verified.bib").write_text(verified_bib, encoding="utf-8")

    artifacts = ["verification_report.json", "references_verified.bib"]

    if paper_text.strip():
        # Stage 23 is an audit boundary, not a manuscript repair stage.  Keep
        # the exact Stage 22 text so a failed citation cannot be hidden by
        # deleting its marker or annotating the claim after verification.
        (stage_dir / "paper_final_verified.md").write_text(
            paper_text, encoding="utf-8"
        )
        artifacts.append("paper_final_verified.md")

    logger.info(
        "Stage 23 citation verify: %d total, %d verified, %d suspicious, "
        "%d hallucinated, %d skipped (integrity=%.1f%%)",
        report.total,
        report.verified,
        report.suspicious,
        report.hallucinated,
        report.skipped,
        report.integrity_score * 100,
    )

    evidence_refs = tuple(f"stage-23/{a}" for a in artifacts)
    if fatal:
        blockers = sorted(
            set(
                hallucinated_keys
                + suspicious_keys
                + skipped_keys
                + unscored_keys
                + low_relevance_keys
            )
        )
        detail = relevance_error or ", ".join(blockers[:20]) or "unknown"
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=tuple(artifacts),
            evidence_refs=evidence_refs,
            error=f"Citation verification is incomplete or invalid: {detail}",
            decision="retry",
        )
    return StageResult(
        stage=Stage.CITATION_VERIFY,
        status=StageStatus.DONE,
        artifacts=tuple(artifacts),
        evidence_refs=evidence_refs,
        decision="degraded" if degraded else None,
    )
