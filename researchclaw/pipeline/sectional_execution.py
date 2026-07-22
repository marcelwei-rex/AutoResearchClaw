"""Feature-flagged deterministic execution shell for sectional Stage 19."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import yaml

from researchclaw.config import PaperRevisionConfig
from researchclaw.experiment_runtime.contract import (
    sha256_file,
    validate_contract_dict,
)
from researchclaw.literature.verify import parse_bibtex_entries
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptDocument,
    ManuscriptSection,
    merge_manuscript,
    parse_manuscript,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    canonical_decimal,
)
from researchclaw.pipeline.canonical_fact_sheet import classify_out_of_scope_request
from researchclaw.pipeline.sectional_revision import (
    ReviewComment,
    ReviewLedger,
    RevisionPlan,
    SectionalRevisionContractError,
    extract_review_ledger,
    make_attempt_id,
    validate_review_ledger,
    validate_revision_plan,
)
from researchclaw.pipeline.sectional_validation import (
    ResolutionAssessmentRecord,
    SectionAttemptRecord,
    SectionManifestMetadata,
    SectionRevisionManifest,
    SectionValidationContext,
    ValidatedSectionReplacement,
    build_section_revision_manifest,
    build_unresolved_comments_artifact,
    extract_citation_keys,
    merge_validated_sections,
    parse_resolution_assessments_jsonl,
    parse_section_attempts_jsonl,
    validate_section_candidate,
    validate_section_revision_manifest,
)


_OWNED_FILES = (
    "paper_revised.md",
    "revision_notes_internal.md",
    "revision_retry_failure.json",
    "revision_plan.json",
    "review_comment_ledger.json",
    "section_attempts.jsonl",
    "resolution_assessments.jsonl",
    "section_revision_manifest.json",
    "unresolved_comments.json",
    "consistency_audit.json",
    "validation_context.json",
    "sectional_llm_diagnostics.json",
)
_OWNED_DIRS = ("sections", "section_validation")


class SectionalExecutionError(RuntimeError):
    """Raised when the B2 execution shell cannot close its contracts."""


@dataclass(frozen=True)
class SectionProposal:
    section_id: str
    revised_body: str
    resolution_comment_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionAssessment:
    comment_id: str
    section_id: str
    attempt_id: str
    critic_model: str
    context_isolated: bool
    verdict: str
    reason: str


class SectionalRevisionProvider(Protocol):
    writer_model: str
    critic_model: str
    diagnostic_records: tuple[dict[str, Any], ...]

    def build_plan(
        self,
        *,
        ledger: ReviewLedger,
        document: ManuscriptDocument,
    ) -> object: ...

    def propose(
        self,
        *,
        section: ManuscriptSection,
        comments: tuple[ReviewComment, ...],
        attempt: int,
        context: SectionValidationContext,
    ) -> SectionProposal: ...

    def assess(
        self,
        *,
        comment: ReviewComment,
        section: ManuscriptSection,
        original_body: str,
        revised_body: str,
        attempt_id: str,
        validator_codes: tuple[str, ...],
    ) -> ResolutionAssessment: ...


@dataclass(frozen=True)
class SectionalExecutionResult:
    completed: bool
    paper_text: str | None
    error: str | None
    artifacts: tuple[str, ...]


@dataclass(frozen=True)
class _ContextBundle:
    allowed_citation_keys: frozenset[str]
    grounded_numeric_values: tuple[Decimal, ...]
    canonical_experiment_evidence_path: str
    canonical_experiment_evidence_sha256: str
    text: str


def execute_sectional_revision(
    *,
    stage_dir: Path,
    run_dir: Path,
    config: PaperRevisionConfig,
    claim_scope: str,
    provider: SectionalRevisionProvider | None,
    evidence: CanonicalExperimentEvidence,
    paper_text: str,
    reviews_text: str,
    review_structure_report_text: str,
    bibliography_text: str,
    bibliography_sha256: str,
    canonical_fact_sheet: Mapping[str, Any] | None = None,
) -> SectionalExecutionResult:
    """Execute B2 using an explicitly injected provider and deterministic gates."""

    clean_sectional_outputs(stage_dir)
    if provider is None:
        raise SectionalExecutionError(
            "sectional revision is enabled but no reviewed provider is configured"
        )
    writer_model = str(provider.writer_model or "").strip()
    critic_model = str(provider.critic_model or "").strip()
    if not writer_model or not critic_model or writer_model == critic_model:
        raise SectionalExecutionError(
            "sectional provider requires distinct nonempty writer and critic models"
        )
    if not config.critic_model.strip() or config.critic_model.strip() != critic_model:
        raise SectionalExecutionError(
            "sectional provider critic model does not match paper_revision.critic_model"
        )

    try:
        contract_data = yaml.safe_load(evidence.experiment_contract_bytes.decode("utf-8"))
        if not isinstance(contract_data, dict):
            raise ValueError("canonical contract root is not an object")
        contract = validate_contract_dict(contract_data)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise SectionalExecutionError(
            f"canonical snapshot contract is invalid: {exc}"
        ) from exc
    if contract.claim_scope != claim_scope:
        raise SectionalExecutionError(
            "Stage 19 claim scope does not match the canonical Stage 9 contract"
        )
    contract_rel = evidence.experiment_contract_path
    contract_sha = evidence.experiment_contract_sha256

    # The Stage 19 boundary passes these only after replaying the Stage 17/18
    # closures. This function must not create a second authority read.
    _validate_stage18_review_binding(
        reviews_text,
        review_structure_report_text,
        evidence,
    )
    document = parse_manuscript(paper_text, strict=True)
    ledger = extract_review_ledger(reviews_text, source_path="stage-18/reviews.md")
    llm_call_limit = 2
    try:
        plan = validate_revision_plan(
            provider.build_plan(ledger=ledger, document=document),
            ledger,
            document,
            reviews=reviews_text,
        )
        if canonical_fact_sheet is not None:
            comments_by_id = {comment.comment_id: comment for comment in ledger.comments}
            assignments = []
            for assignment in plan.assignments:
                codes = classify_out_of_scope_request(
                    comments_by_id[assignment.comment_id].exact_text,
                    canonical_fact_sheet,
                )
                if assignment.disposition == "assigned" and codes:
                    assignment = replace(
                        assignment,
                        target_section_ids=(),
                        disposition="not_actionable_with_reason",
                        reason="Canonical contract boundary: " + ", ".join(codes),
                    )
                assignments.append(assignment)
            plan = replace(plan, assignments=tuple(assignments))
            validate_revision_plan(plan, ledger, document, reviews=reviews_text)
    except RuntimeError:
        _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)
        raise
    _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)

    context_bundle = build_validation_context(
        document=document,
        config=config,
        evidence=evidence,
        bibliography_text=bibliography_text,
        bibliography_sha256=bibliography_sha256,
    )
    _write_text_atomic(stage_dir / "validation_context.json", context_bundle.text)
    _write_json_atomic(stage_dir / "review_comment_ledger.json", ledger.to_dict())
    _write_json_atomic(stage_dir / "revision_plan.json", plan.to_dict())

    comments_by_id = {comment.comment_id: comment for comment in ledger.comments}
    assigned_by_section: dict[str, list[ReviewComment]] = {}
    for assignment in plan.assignments:
        if assignment.disposition != "assigned":
            continue
        for section_id in assignment.target_section_ids:
            assigned_by_section.setdefault(section_id, []).append(
                comments_by_id[assignment.comment_id]
            )
    llm_call_limit = _sectional_llm_call_limit(
        assigned_by_section=assigned_by_section,
        max_section_retries=config.max_section_retries,
    )
    _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)

    sections_dir = stage_dir / "sections"
    validation_dir = stage_dir / "section_validation"
    sections_dir.mkdir(parents=True, exist_ok=True)
    validation_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    replacements: dict[str, ValidatedSectionReplacement] = {}
    metadata: dict[str, SectionManifestMetadata] = {}
    accepted_comment_sections: set[tuple[str, str]] = set()
    attempts_by_section: dict[str, list[str]] = {}

    section_lookup = {section.section_id: section for section in document.sections}
    for section_id, assigned_comments_list in assigned_by_section.items():
        section = section_lookup[section_id]
        assigned_comments = tuple(assigned_comments_list)
        required_ids = tuple(
            comment.comment_id for comment in assigned_comments if comment.required
        )
        accepted_replacement: ValidatedSectionReplacement | None = None
        for attempt in range(1, config.max_section_retries + 2):
            attempt_id = make_attempt_id(section_id, attempt)
            attempts_by_section.setdefault(section_id, []).append(attempt_id)
            context = SectionValidationContext(
                document=document,
                section_id=section_id,
                attempt=attempt,
                allowed_citation_keys=context_bundle.allowed_citation_keys,
                grounded_numeric_values=context_bundle.grounded_numeric_values,
                required_comment_ids=required_ids,
                resolution_comment_ids=(),
                min_length_ratio=config.min_length_ratio,
                max_length_ratio=config.max_length_ratio,
            )
            try:
                proposal = provider.propose(
                    section=section,
                    comments=assigned_comments,
                    attempt=attempt,
                    context=context,
                )
            except RuntimeError as exc:  # provider transport boundary
                _write_sectional_llm_diagnostics(
                    stage_dir, provider, llm_call_limit
                )
                attempts.append(
                    _transport_failure_attempt(
                        attempt_id=attempt_id,
                        section_id=section_id,
                        comment_ids=tuple(c.comment_id for c in assigned_comments),
                        writer_model=writer_model,
                        attempt=attempt,
                        source_section_sha256=section.original_sha256,
                        canonical_experiment_evidence_path=evidence.manifest_path,
                        canonical_experiment_evidence_sha256=evidence.manifest_sha256,
                        exc=exc,
                    )
                )
                continue
            _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)
            _validate_proposal(proposal, section_id, assigned_comments)
            context = replace(
                context,
                resolution_comment_ids=proposal.resolution_comment_ids,
            )
            candidate_path = sections_dir / f"{section_id}.attempt-{attempt}.md"
            _write_text_atomic(candidate_path, proposal.revised_body)
            validation = validate_section_candidate(context, proposal.revised_body)
            validation_rel = (
                f"stage-19/section_validation/{section_id}.attempt-{attempt}.json"
            )
            validation_path = validation_dir / f"{section_id}.attempt-{attempt}.json"
            validation_text = _canonical_json_text(validation.to_dict())
            _write_text_atomic(validation_path, validation_text)
            failed_codes = tuple(
                check.code for check in validation.checks if check.status == "failed"
            )
            attempt_assessments: list[ResolutionAssessment] = []
            assessment_error: Exception | None = None
            if validation.accepted and proposal.revised_body != section.body:
                try:
                    for comment in assigned_comments:
                        assessment = provider.assess(
                            comment=comment,
                            section=section,
                            original_body=section.body,
                            revised_body=proposal.revised_body,
                            attempt_id=attempt_id,
                            validator_codes=failed_codes,
                        )
                        _write_sectional_llm_diagnostics(
                            stage_dir, provider, llm_call_limit
                        )
                        _validate_assessment(
                            assessment,
                            comment=comment,
                            section_id=section_id,
                            attempt_id=attempt_id,
                            expected_critic_model=critic_model,
                            writer_model=writer_model,
                        )
                        attempt_assessments.append(assessment)
                except RuntimeError as exc:  # isolated critic boundary
                    _write_sectional_llm_diagnostics(
                        stage_dir, provider, llm_call_limit
                    )
                    assessment_error = exc
                    attempt_assessments = []
            all_resolved = bool(attempt_assessments) and all(
                assessment.verdict == "resolved"
                for assessment in attempt_assessments
            )
            status = "accepted" if validation.accepted and all_resolved else "rejected"
            attempts.append(
                SectionAttemptRecord.from_dict(
                    SectionAttemptRecord(
                        schema_version=1,
                        attempt_id=attempt_id,
                        section_id=section_id,
                        source_section_sha256=section.original_sha256,
                        canonical_experiment_evidence_path=evidence.manifest_path,
                        canonical_experiment_evidence_sha256=evidence.manifest_sha256,
                        comment_ids=tuple(c.comment_id for c in assigned_comments),
                        resolution_comment_ids=proposal.resolution_comment_ids,
                        writer_model=writer_model,
                        attempt=attempt,
                        status=status,
                        candidate_path=(
                            f"stage-19/sections/{section_id}.attempt-{attempt}.md"
                        ),
                        candidate_body_sha256=_sha256(proposal.revised_body),
                        validation_report_path=validation_rel,
                        validation_report_sha256=_sha256(validation_text),
                        validator_codes=failed_codes,
                        error_type=(
                            type(assessment_error).__name__
                            if assessment_error
                            else None
                        ),
                        error=(
                            None
                            if status == "accepted"
                            else str(assessment_error or "candidate not accepted")
                        ),
                        timestamp=_utcnow(),
                    ).to_dict()
                ).to_dict()
            )
            for assessment in attempt_assessments:
                payload = _assessment_payload(
                    assessment,
                    canonical_experiment_evidence_path=evidence.manifest_path,
                    canonical_experiment_evidence_sha256=evidence.manifest_sha256,
                )
                assessments.append(payload)
                if status == "accepted" and assessment.verdict == "resolved":
                    accepted_comment_sections.add(
                        (assessment.comment_id, assessment.section_id)
                    )
            if status == "accepted":
                accepted_replacement = ValidatedSectionReplacement(
                    section_id=section_id,
                    body=proposal.revised_body,
                    validation=validation,
                    context=context,
                )
                replacements[section_id] = accepted_replacement
                metadata[section_id] = SectionManifestMetadata(
                    comment_ids=tuple(c.comment_id for c in assigned_comments),
                    attempt_ids=tuple(attempts_by_section[section_id]),
                    final_status="accepted",
                    validation_result=validation,
                    validation_context=context,
                )
                break
        if accepted_replacement is None:
            metadata[section_id] = SectionManifestMetadata(
                comment_ids=tuple(c.comment_id for c in assigned_comments),
                attempt_ids=tuple(attempts_by_section[section_id]),
                final_status="unresolved_original_preserved",
                validation_result=None,
                validation_context=None,
            )

    _verify_input_immutability(
        run_dir=run_dir,
        document=document,
        ledger=ledger,
        context_text=context_bundle.text,
        config=config,
        evidence=evidence,
    )
    final_ledger = _finalize_ledger(
        ledger=ledger,
        plan=plan,
        attempts_by_section=attempts_by_section,
        accepted_comment_sections=accepted_comment_sections,
    )
    validate_review_ledger(final_ledger, reviews=reviews_text, require_final=True)
    merge_result = merge_validated_sections(document, replacements)
    assessments_text = _jsonl_text(assessments)
    attempts_text = _jsonl_text(attempts)
    parse_section_attempts_jsonl(attempts_text)
    parse_resolution_assessments_jsonl(assessments_text)
    unresolved_text = _pretty_json_text(
        build_unresolved_comments_artifact(final_ledger)
    )
    _write_json_atomic(stage_dir / "review_comment_ledger.json", final_ledger.to_dict())
    _write_text_atomic(stage_dir / "section_attempts.jsonl", attempts_text)
    _write_text_atomic(stage_dir / "resolution_assessments.jsonl", assessments_text)
    _write_text_atomic(stage_dir / "unresolved_comments.json", unresolved_text)

    completed = True
    try:
        manifest = build_section_revision_manifest(
            document=document,
            merge_result=merge_result,
            ledger=final_ledger,
            plan=plan,
            reviews=reviews_text,
            claim_scope=claim_scope,
            experiment_contract_path=contract_rel,
            experiment_contract_sha256=contract_sha,
            canonical_experiment_evidence_path=evidence.manifest_path,
            canonical_experiment_evidence_sha256=evidence.manifest_sha256,
            writer_model=writer_model,
            critic_model=critic_model,
            source_paper_path="stage-17/paper_draft.md",
            section_metadata=metadata,
            attempts_text=attempts_text,
            assessments_text=assessments_text,
            unresolved_comments_text=unresolved_text,
            completed=True,
            validation_context_text=context_bundle.text,
        )
    except SectionalRevisionContractError as exc:
        if claim_scope == "pipeline_validation":
            raise
        completed = False
        manifest = build_section_revision_manifest(
            document=document,
            merge_result=merge_result,
            ledger=final_ledger,
            plan=plan,
            reviews=reviews_text,
            claim_scope=claim_scope,
            experiment_contract_path=contract_rel,
            experiment_contract_sha256=contract_sha,
            canonical_experiment_evidence_path=evidence.manifest_path,
            canonical_experiment_evidence_sha256=evidence.manifest_sha256,
            writer_model=writer_model,
            critic_model=critic_model,
            source_paper_path="stage-17/paper_draft.md",
            section_metadata=metadata,
            attempts_text=attempts_text,
            assessments_text=assessments_text,
            unresolved_comments_text=unresolved_text,
            completed=False,
            validation_context_text=context_bundle.text,
        )
        error = str(exc)
    else:
        error = None
    validate_section_revision_manifest(
        manifest,
        document=document,
        merge_result=merge_result,
        ledger=final_ledger,
        plan=plan,
        reviews=reviews_text,
        claim_scope=claim_scope,
        experiment_contract_path=contract_rel,
        experiment_contract_sha256=contract_sha,
        canonical_experiment_evidence_path=evidence.manifest_path,
        canonical_experiment_evidence_sha256=evidence.manifest_sha256,
        writer_model=writer_model,
        critic_model=critic_model,
        source_paper_path="stage-17/paper_draft.md",
        section_metadata=metadata,
        attempts_text=attempts_text,
        assessments_text=assessments_text,
        unresolved_comments_text=unresolved_text,
        completed=completed,
        validation_context_text=context_bundle.text,
    )
    artifacts = (
        "review_comment_ledger.json",
        "revision_plan.json",
        "validation_context.json",
        "section_attempts.jsonl",
        "resolution_assessments.jsonl",
        "unresolved_comments.json",
        "section_revision_manifest.json",
    )
    if not completed:
        _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)
        _write_json_atomic(stage_dir / "section_revision_manifest.json", manifest.to_dict())
        return SectionalExecutionResult(False, None, error, artifacts)
    try:
        _write_text_atomic(stage_dir / "paper_revised.md", merge_result.merged_text)
        _write_json_atomic(stage_dir / "section_revision_manifest.json", manifest.to_dict())
        _replay_published_sectional_bundle(
            stage_dir=stage_dir,
            run_dir=run_dir,
            document=document,
            merge_result=merge_result,
            ledger=final_ledger,
            plan=plan,
            reviews_text=reviews_text,
            claim_scope=claim_scope,
            experiment_contract_path=contract_rel,
            experiment_contract_sha256=contract_sha,
            canonical_experiment_evidence_path=evidence.manifest_path,
            canonical_experiment_evidence_sha256=evidence.manifest_sha256,
            writer_model=writer_model,
            critic_model=critic_model,
            source_paper_path="stage-17/paper_draft.md",
            section_metadata=metadata,
            attempts_text=attempts_text,
            assessments_text=assessments_text,
            unresolved_comments_text=unresolved_text,
            validation_context_text=context_bundle.text,
        )
    except Exception:
        clean_sectional_outputs(stage_dir)
        _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)
        raise
    _write_sectional_llm_diagnostics(stage_dir, provider, llm_call_limit)
    return SectionalExecutionResult(
        True,
        merge_result.merged_text,
        None,
        ("paper_revised.md",) + artifacts,
    )


def build_validation_context(
    *,
    document: ManuscriptDocument,
    config: PaperRevisionConfig,
    evidence: CanonicalExperimentEvidence,
    bibliography_text: str,
    bibliography_sha256: str,
) -> _ContextBundle:
    """Build and source-bind the B2 citation and metric validation context."""

    sources: list[dict[str, str]] = []
    original_citations = extract_citation_keys(merge_manuscript(document))
    citation_keys: set[str] = set()
    citation_keys.update(
        str(entry.get("key") or "").strip()
        for entry in parse_bibtex_entries(bibliography_text)
        if str(entry.get("key") or "").strip()
    )
    if not re.fullmatch(r"[0-9a-f]{64}", bibliography_sha256):
        raise SectionalExecutionError("canonical bibliography hash is invalid")
    if _sha256(bibliography_text) != bibliography_sha256:
        raise SectionalExecutionError("canonical bibliography hash does not match captured bytes")
    sources.append(
        {
            "kind": "citations",
            "path": "stage-04/references.bib",
            "sha256": bibliography_sha256,
        }
    )
    missing_citations = sorted(original_citations - citation_keys)
    if missing_citations:
        raise SectionalExecutionError(
            "canonical references.bib is missing draft citation keys: "
            + ", ".join(missing_citations)
        )

    numeric_values: list[Decimal] = []
    numeric_seen: set[Decimal] = set()
    for value in (
        evidence.metric_observations,
        evidence.structured_results,
        evidence.summary,
    ):
        _collect_numbers(value, numeric_values, numeric_seen)
    if numeric_values:
        sources.append(
            {
                "kind": "canonical_evidence",
                "path": evidence.manifest_path,
                "sha256": evidence.manifest_sha256,
            }
        )

    config_payload = {
        "max_section_retries": config.max_section_retries,
        "min_length_ratio": config.min_length_ratio,
        "max_length_ratio": config.max_length_ratio,
    }
    config_text = _canonical_json_text(config_payload)
    sources.append(
        {
            "kind": "config",
            "path": "config.paper_revision",
            "sha256": _sha256(config_text),
        }
    )
    payload = {
        "schema_version": 2,
        "numeric_policy_version": "stage19_decimal_v1",
        "source_paper_sha256": document.source_sha256,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "allowed_citation_keys": sorted(citation_keys),
        "grounded_numeric_values": [canonical_decimal(value) for value in numeric_values],
        "max_section_retries": config.max_section_retries,
        "min_length_ratio": config.min_length_ratio,
        "max_length_ratio": config.max_length_ratio,
        "sources": sorted(sources, key=lambda item: (item["kind"], item["path"])),
    }
    text = _pretty_json_text(payload)
    return _ContextBundle(
        frozenset(citation_keys),
        tuple(numeric_values),
        evidence.manifest_path,
        evidence.manifest_sha256,
        text,
    )


def _finalize_ledger(
    *,
    ledger: ReviewLedger,
    plan: RevisionPlan,
    attempts_by_section: dict[str, list[str]],
    accepted_comment_sections: set[tuple[str, str]],
) -> ReviewLedger:
    assignments = {assignment.comment_id: assignment for assignment in plan.assignments}
    comments: list[ReviewComment] = []
    for comment in ledger.comments:
        assignment = assignments[comment.comment_id]
        if assignment.disposition != "assigned":
            comments.append(
                replace(
                    comment,
                    final_status=assignment.disposition,
                    resolution_reason=assignment.reason,
                )
            )
            continue
        attempt_ids = tuple(
            attempt_id
            for section_id in assignment.target_section_ids
            for attempt_id in attempts_by_section.get(section_id, ())
        )
        resolved = all(
            (comment.comment_id, section_id) in accepted_comment_sections
            for section_id in assignment.target_section_ids
        )
        comments.append(
            replace(
                comment,
                working_status="assigned",
                target_section_ids=assignment.target_section_ids,
                final_status="resolved" if resolved else "unresolved",
                resolution_reason=(
                    "All assigned sections passed deterministic validation "
                    "and isolated assessment."
                    if resolved
                    else "One or more assigned sections did not close after bounded attempts."
                ),
                attempt_ids=attempt_ids,
            )
        )
    return replace(ledger, comments=tuple(comments))


def _verify_input_immutability(
    *,
    run_dir: Path,
    document: ManuscriptDocument,
    ledger: ReviewLedger,
    context_text: str,
    config: PaperRevisionConfig,
    evidence: CanonicalExperimentEvidence,
) -> None:
    evidence_path = run_dir / evidence.manifest_path
    if (
        evidence.manifest_path != "canonical_experiment_evidence.json"
        or not evidence_path.is_file()
        or sha256_file(evidence_path) != evidence.manifest_sha256
    ):
        raise SectionalExecutionError("canonical experiment evidence changed during revision")
    payload = json.loads(context_text)
    for source in payload["sources"]:
        kind = source["kind"]
        if kind == "config":
            config_payload = {
                "max_section_retries": config.max_section_retries,
                "min_length_ratio": config.min_length_ratio,
                "max_length_ratio": config.max_length_ratio,
            }
            actual = _sha256(_canonical_json_text(config_payload))
        elif kind == "canonical_evidence":
            actual = evidence.manifest_sha256
        else:
            path = run_dir / source["path"]
            if not path.is_file():
                raise SectionalExecutionError(
                    f"validation context source disappeared: {source['path']}"
                )
            actual = sha256_file(path)
        if actual != source["sha256"]:
            raise SectionalExecutionError(
                f"validation context source changed: {source['path']}"
            )


def _validate_proposal(
    proposal: SectionProposal,
    section_id: str,
    comments: tuple[ReviewComment, ...],
) -> None:
    if not isinstance(proposal, SectionProposal):
        raise SectionalExecutionError("provider returned an invalid proposal type")
    if proposal.section_id != section_id or not isinstance(proposal.revised_body, str):
        raise SectionalExecutionError("proposal section identity is invalid")
    known = {comment.comment_id for comment in comments}
    if len(set(proposal.resolution_comment_ids)) != len(proposal.resolution_comment_ids):
        raise SectionalExecutionError("proposal contains duplicate resolution IDs")
    if not set(proposal.resolution_comment_ids).issubset(known):
        raise SectionalExecutionError("proposal contains unknown resolution IDs")


def _validate_assessment(
    assessment: ResolutionAssessment,
    *,
    comment: ReviewComment,
    section_id: str,
    attempt_id: str,
    expected_critic_model: str,
    writer_model: str,
) -> None:
    if not isinstance(assessment, ResolutionAssessment):
        raise SectionalExecutionError("provider returned an invalid assessment type")
    if (
        assessment.comment_id != comment.comment_id
        or assessment.section_id != section_id
        or assessment.attempt_id != attempt_id
    ):
        raise SectionalExecutionError("assessment identity does not match its attempt")
    if (
        assessment.critic_model != expected_critic_model
        or assessment.critic_model == writer_model
        or not assessment.context_isolated
    ):
        raise SectionalExecutionError("assessment critic isolation is invalid")
    if assessment.verdict not in {"resolved", "unresolved"}:
        raise SectionalExecutionError("assessment verdict is invalid")
    if not assessment.reason.strip():
        raise SectionalExecutionError("assessment reason is required")


def _assessment_payload(
    assessment: ResolutionAssessment,
    *,
    canonical_experiment_evidence_path: str,
    canonical_experiment_evidence_sha256: str,
) -> dict[str, Any]:
    return ResolutionAssessmentRecord.from_dict(
        ResolutionAssessmentRecord(
            schema_version=1,
            assessment_id=f"ra-{assessment.comment_id}-{assessment.attempt_id}",
            comment_id=assessment.comment_id,
            section_id=assessment.section_id,
            attempt_id=assessment.attempt_id,
            canonical_experiment_evidence_path=canonical_experiment_evidence_path,
            canonical_experiment_evidence_sha256=canonical_experiment_evidence_sha256,
            critic_model=assessment.critic_model,
            context_isolated=assessment.context_isolated,
            verdict=assessment.verdict,
            reason=assessment.reason,
            timestamp=_utcnow(),
        ).to_dict()
    ).to_dict()


def _transport_failure_attempt(
    *,
    attempt_id: str,
    section_id: str,
    comment_ids: tuple[str, ...],
    writer_model: str,
    attempt: int,
    source_section_sha256: str,
    canonical_experiment_evidence_path: str,
    canonical_experiment_evidence_sha256: str,
    exc: Exception,
) -> dict[str, Any]:
    return SectionAttemptRecord.from_dict(
        SectionAttemptRecord(
            schema_version=1,
            attempt_id=attempt_id,
            section_id=section_id,
            source_section_sha256=source_section_sha256,
            canonical_experiment_evidence_path=canonical_experiment_evidence_path,
            canonical_experiment_evidence_sha256=canonical_experiment_evidence_sha256,
            comment_ids=comment_ids,
            resolution_comment_ids=(),
            writer_model=writer_model,
            attempt=attempt,
            status="transport_failed",
            candidate_path=None,
            candidate_body_sha256=None,
            validation_report_path=None,
            validation_report_sha256=None,
            validator_codes=(),
            error_type=type(exc).__name__,
            error=str(exc),
            timestamp=_utcnow(),
        ).to_dict()
    ).to_dict()


def clean_sectional_outputs(stage_dir: Path) -> None:
    """Remove only artifacts owned by the current Stage 19 attempt."""

    for name in _OWNED_FILES:
        (stage_dir / name).unlink(missing_ok=True)
        (stage_dir / f"{name}.tmp").unlink(missing_ok=True)
    for name in _OWNED_DIRS:
        path = stage_dir / name
        if path.exists():
            shutil.rmtree(path)


def _sectional_llm_call_limit(
    *,
    assigned_by_section: Mapping[str, list[ReviewComment]],
    max_section_retries: int,
) -> int:
    attempts = max_section_retries + 1
    writer_calls = 2 * len(assigned_by_section) * attempts
    critic_calls = 2 * sum(map(len, assigned_by_section.values())) * attempts
    return 2 + writer_calls + critic_calls


def _write_sectional_llm_diagnostics(
    stage_dir: Path,
    provider: SectionalRevisionProvider,
    call_limit: int,
) -> None:
    records_obj = getattr(provider, "diagnostic_records", ())
    if not isinstance(records_obj, (tuple, list)):
        raise SectionalExecutionError("sectional LLM diagnostics must be a sequence")
    records: list[dict[str, Any]] = []
    has_failure = False
    for index, item in enumerate(records_obj):
        if not isinstance(item, dict):
            raise SectionalExecutionError(
                f"sectional LLM diagnostic {index} must be an object"
            )
        if "raw" in item or "content" in item:
            raise SectionalExecutionError(
                "sectional LLM diagnostics must not contain raw response text"
            )
        record = dict(item)
        category = record.get("error_category")
        if not isinstance(category, str) or not category:
            raise SectionalExecutionError(
                f"sectional LLM diagnostic {index} has invalid error_category"
            )
        has_failure = has_failure or category != "success"
        records.append(record)
    if len(records) > call_limit:
        raise SectionalExecutionError(
            "sectional LLM call limit exceeded: "
            f"calls={len(records)}, limit={call_limit}"
        )
    diagnostic_path = stage_dir / "sectional_llm_diagnostics.json"
    if not has_failure:
        diagnostic_path.unlink(missing_ok=True)
        return
    _write_json_atomic(
        diagnostic_path,
        {
            "schema_version": 1,
            "llm_call_count": len(records),
            "llm_call_limit": call_limit,
            "calls": records,
        },
    )


def _source_record(run_dir: Path, path: Path, kind: str) -> dict[str, str]:
    return {
        "kind": kind,
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": sha256_file(path),
    }


def _validate_stage18_review_binding(
    reviews: str,
    report_text: str,
    evidence: CanonicalExperimentEvidence,
) -> None:
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as exc:
        raise SectionalExecutionError(
            f"canonical Stage 18 review structure report is invalid: {exc}"
        ) from exc
    if not isinstance(report, dict) or report.get("valid") is not True:
        raise SectionalExecutionError(
            "canonical Stage 18 review structure report is not valid"
        )
    if report.get("source_reviews_sha256") != _sha256(reviews):
        raise SectionalExecutionError(
            "canonical Stage 18 review structure report does not match reviews"
        )
    if (
        report.get("canonical_experiment_evidence_path") != evidence.manifest_path
        or report.get("canonical_experiment_evidence_sha256") != evidence.manifest_sha256
    ):
        raise SectionalExecutionError(
            "canonical Stage 18 reviews bind different experiment evidence"
        )


def _collect_numbers(value: Any, output: list[Decimal], seen: set[Decimal]) -> None:
    if isinstance(value, Mapping):
        for nested in value.values():
            _collect_numbers(nested, output, seen)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value[:100]:
            _collect_numbers(nested, output, seen)
    elif not isinstance(value, bool) and isinstance(value, float):
        raise SectionalExecutionError(
            "canonical evidence must not expose binary-float numeric authority"
        )
    elif not isinstance(value, bool) and isinstance(value, (int, Decimal)):
        try:
            number = Decimal(canonical_decimal(value))
        except Exception as exc:
            raise SectionalExecutionError("canonical evidence numeric authority is invalid") from exc
        if number not in seen:
            seen.add(number)
            output.append(number)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_text_atomic(path, _pretty_json_text(value))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _load_strict_json_object(text: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    value = json.loads(text, object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _replay_published_sectional_bundle(
    *,
    stage_dir: Path,
    run_dir: Path,
    document: ManuscriptDocument,
    merge_result: Any,
    ledger: ReviewLedger,
    plan: RevisionPlan,
    reviews_text: str,
    claim_scope: str,
    experiment_contract_path: str,
    experiment_contract_sha256: str,
    canonical_experiment_evidence_path: str,
    canonical_experiment_evidence_sha256: str,
    writer_model: str,
    critic_model: str,
    source_paper_path: str,
    section_metadata: Mapping[str, SectionManifestMetadata],
    attempts_text: str,
    assessments_text: str,
    unresolved_comments_text: str,
    validation_context_text: str,
) -> None:
    """Reopen every published Stage 19 authority artifact before success."""
    expected_texts = {
        "paper_revised.md": merge_result.merged_text,
        "review_comment_ledger.json": _pretty_json_text(ledger.to_dict()),
        "revision_plan.json": _pretty_json_text(plan.to_dict()),
        "validation_context.json": validation_context_text,
        "section_attempts.jsonl": attempts_text,
        "resolution_assessments.jsonl": assessments_text,
        "unresolved_comments.json": unresolved_comments_text,
    }
    stored: dict[str, str] = {}
    for name, expected in expected_texts.items():
        path = stage_dir / name
        if path.is_symlink() or not path.is_file():
            raise SectionalExecutionError(f"published Stage 19 artifact is unsafe: {name}")
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            raise SectionalExecutionError(f"published Stage 19 artifact changed: {name}")
        stored[name] = actual

    attempts = parse_section_attempts_jsonl(stored["section_attempts.jsonl"])
    parse_resolution_assessments_jsonl(stored["resolution_assessments.jsonl"])
    for attempt in attempts:
        if attempt.candidate_path is None:
            continue
        for path_text, expected_sha256 in (
            (attempt.candidate_path, attempt.candidate_body_sha256),
            (attempt.validation_report_path, attempt.validation_report_sha256),
        ):
            if path_text is None or expected_sha256 is None:
                raise SectionalExecutionError("published attempt artifact binding is incomplete")
            relative = Path(path_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise SectionalExecutionError("published attempt artifact path is unsafe")
            path = run_dir / relative
            if path.is_symlink() or not path.is_file():
                raise SectionalExecutionError("published attempt artifact is unsafe")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise SectionalExecutionError("published attempt artifact hash mismatch")

    manifest_path = stage_dir / "section_revision_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SectionalExecutionError("published Stage 19 manifest is unsafe")
    stored_manifest = SectionRevisionManifest.from_dict(
        _load_strict_json_object(manifest_path.read_text(encoding="utf-8"))
    )
    validate_section_revision_manifest(
        stored_manifest,
        document=document,
        merge_result=merge_result,
        ledger=ledger,
        plan=plan,
        reviews=reviews_text,
        claim_scope=claim_scope,
        experiment_contract_path=experiment_contract_path,
        experiment_contract_sha256=experiment_contract_sha256,
        canonical_experiment_evidence_path=canonical_experiment_evidence_path,
        canonical_experiment_evidence_sha256=canonical_experiment_evidence_sha256,
        writer_model=writer_model,
        critic_model=critic_model,
        source_paper_path=source_paper_path,
        section_metadata=section_metadata,
        attempts_text=stored["section_attempts.jsonl"],
        assessments_text=stored["resolution_assessments.jsonl"],
        unresolved_comments_text=stored["unresolved_comments.json"],
        completed=True,
        validation_context_text=stored["validation_context.json"],
    )


def _canonical_json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _pretty_json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(_canonical_json_text(row) + "\n" for row in rows)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()
