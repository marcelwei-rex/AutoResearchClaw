"""Inactive structured Stage 17 producer, transaction, and disk replay."""

from __future__ import annotations

import hashlib
import os
import stat
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from researchclaw.literature.citation_plan import (
    build_citation_closure_from_texts,
    parse_citation_plan,
)
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.literature.experiment_fact_closure import (
    canonical_experiment_fact_json_text,
)
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline.bound_output_namespace import (
    BoundOutputNamespace,
    BoundRegularFile,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    canonical_authority_json_text,
    load_canonical_experiment_evidence,
)
from researchclaw.pipeline.canonical_fact_sheet import _is_domain_evaluator_v2
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_invalidation_epoch,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.scientific_claim_authority import (
    ScientificClaimGenerationBinding,
    build_scientific_claim_generation_binding,
    build_scientific_claim_registry,
    validate_scientific_claim_selection,
)
from researchclaw.pipeline.scientific_claim_publication import (
    SECTION_ORDER,
    build_scientific_claim_selection,
    build_stage17_scientific_claim_authority_manifest,
    build_structured_experiment_fact_closure,
    build_structured_paper_structure_report,
    parse_scientific_claim_registry,
    parse_scientific_evidence_facts,
    parse_stage17_scientific_claim_authority_manifest,
    replay_scientific_claim_selection_wrapper,
    replay_stage17_paper_related_artifacts,
)


STRUCTURED_STAGE17_OUTPUTS = (
    "scientific_evidence_facts.json",
    "scientific_claim_registry.json",
    "scientific_claim_selection.json",
    "paper_draft.md",
    "paper_structure_report.json",
    "experiment_fact_closure_report.json",
    "citation_closure_report.json",
)
STRUCTURED_STAGE17_MANIFEST = "scientific_claim_authority_manifest.json"
STRUCTURED_STAGE17_STAGING = ".stage17-structured-publication.staging"
_CITATION_PLAN_PATH = "stage-16/citation_plan.json"
_CITATION_ALLOWLIST_PATH = "stage-06/citation_allowlist.json"
_FINAL_NAMES = (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST)


class StructuredStage17PublicationError(RuntimeError):
    """Structured Stage 17 admission, generation, or replay failed."""


_CAPTURE_CONSTRUCTION_AUTHORITY = object()


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class StructuredStage17SourceCapture:
    evidence: CanonicalExperimentEvidence
    binding: ScientificClaimGenerationBinding
    files: tuple[tuple[str, str, bytes], ...]
    citation_plan: bytes
    citation_allowlist: bytes
    _writer_owner: ReleaseGraphLock
    _run_identity: tuple[int, int]
    _run_path: str
    _construction_token: object

    def __init__(
        self,
        *,
        evidence: CanonicalExperimentEvidence,
        binding: ScientificClaimGenerationBinding,
        files: tuple[tuple[str, str, bytes], ...],
        citation_plan: bytes,
        citation_allowlist: bytes,
        writer_owner: ReleaseGraphLock,
        run_identity: tuple[int, int],
        run_path: str,
        construction_token: object,
        authority: object,
    ) -> None:
        if authority is not _CAPTURE_CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 17 capture construction is private")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "citation_plan", citation_plan)
        object.__setattr__(self, "citation_allowlist", citation_allowlist)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_construction_token", construction_token)


_ISSUED_CAPTURES: weakref.WeakKeyDictionary[
    StructuredStage17SourceCapture, object
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class StructuredStage17Snapshot:
    files: tuple[tuple[str, BoundRegularFile], ...]
    sources: tuple[tuple[str, str, bytes], ...]
    namespace_identity: tuple[
        tuple[int, int], tuple[int, int], tuple[str, ...]
    ]

    def content(self, name: str) -> bytes:
        matches = tuple(item.content for key, item in self.files if key == name)
        if len(matches) != 1:
            raise StructuredStage17PublicationError(
                f"structured Stage 17 snapshot missing {name}"
            )
        return matches[0]


@dataclass(frozen=True)
class _StructuredStage17PublishedContext:
    capture: StructuredStage17SourceCapture
    snapshot: StructuredStage17Snapshot
    run_ctime_ns: int


def execute_structured_stage17_publication(
    run_dir: Path,
    stage_dir: Path,
    llm: LLMClient | None,
) -> StructuredStage17Snapshot:
    """Direct test entry; its complete guard is intentionally the first action."""

    capability.require_complete_structured_capability(
        "execute_structured_stage17_publication"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage17_publication", mode="write"
    ) as lease:
        evidence = load_canonical_experiment_evidence(run_dir)
        capture = capture_structured_stage17_sources(lease, evidence)
        return publish_structured_stage17_from_capture(
            lease,
            stage_dir=stage_dir,
            capture=capture,
            llm=llm,
        )


def capture_structured_stage17_sources(
    lease: ReleaseGraphLock,
    evidence: CanonicalExperimentEvidence,
) -> StructuredStage17SourceCapture:
    """Bind the full replayed source inventory through the held run fd."""

    capability.require_complete_structured_capability(
        "capture_structured_stage17_sources"
    )
    require_active_writer_epoch(lease.run_dir, lease)
    if not _is_domain_evaluator_v2(evidence):
        raise StructuredStage17PublicationError(
            "structured Stage 17 requires exact domain-evaluator-v2 evidence"
        )
    try:
        binding = build_scientific_claim_generation_binding(evidence)
    except Exception as exc:
        raise StructuredStage17PublicationError(
            f"structured generation binding failed: {exc}"
        ) from exc
    if binding.evidence is not evidence:
        raise StructuredStage17PublicationError(
            "structured generation binding changed evidence generation"
        )
    citation_plan = lease.read_run_file(_CITATION_PLAN_PATH)
    citation_allowlist = lease.read_run_file(_CITATION_ALLOWLIST_PATH)
    files = _capture_evidence_files(lease, evidence)
    owner = lease._require_active()
    construction_token = object()
    capture = StructuredStage17SourceCapture(
        evidence=evidence,
        binding=binding,
        files=files,
        citation_plan=citation_plan,
        citation_allowlist=citation_allowlist,
        writer_owner=owner,
        run_identity=owner._run_identity,
        run_path=str(owner.run_dir.absolute()),
        construction_token=construction_token,
        authority=_CAPTURE_CONSTRUCTION_AUTHORITY,
    )
    _ISSUED_CAPTURES[capture] = construction_token
    return capture


def _require_capture_provenance(
    lease: ReleaseGraphLock,
    capture: object,
) -> ReleaseGraphLock:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    if not isinstance(capture, StructuredStage17SourceCapture):
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture was not issued by its private constructor"
        )
    if _ISSUED_CAPTURES.get(capture) is not capture._construction_token:
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture construction is not issued"
        )
    if capture._writer_owner is not owner:
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture writer does not match active writer"
        )
    if capture._run_identity != owner._run_identity:
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture run identity does not match active run"
        )
    run_path = str(owner.run_dir.absolute())
    if capture._run_path != run_path or str(lease.run_dir.absolute()) != run_path:
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture run path does not match active run"
        )
    if capture.binding.evidence is not capture.evidence:
        raise StructuredStage17PublicationError(
            "structured Stage 17 capture mixes evidence generations"
        )
    return owner


def _assert_structured_transaction_canonical(
    namespace: BoundOutputNamespace,
    run_ctime_ns: int,
) -> None:
    namespace.assert_canonical()
    if os.fstat(namespace._run_fd).st_ctime_ns != run_ctime_ns:
        raise StructuredStage17PublicationError(
            "structured Stage 17 run namespace changed during transaction"
        )


def publish_structured_stage17_from_capture(
    lease: ReleaseGraphLock,
    *,
    stage_dir: Path,
    capture: StructuredStage17SourceCapture,
    llm: LLMClient | None,
) -> StructuredStage17Snapshot:
    """Publish one generation from the dispatcher's immutable capture."""

    capability.require_complete_structured_capability(
        "publish_structured_stage17_from_capture"
    )
    owner = _require_capture_provenance(lease, capture)
    _clear_published_context(owner)
    if stage_dir != lease.run_dir / "stage-17":
        raise StructuredStage17PublicationError(
            "structured Stage 17 directory is not canonical"
        )
    with lease.open_stage_namespace("stage-17") as namespace:
        require_namespace_owned_by_epoch(namespace, lease, "stage-17")
        run_ctime_ns = os.fstat(namespace._run_fd).st_ctime_ns

        def checkpoint() -> None:
            _assert_structured_transaction_canonical(namespace, run_ctime_ns)

        def verify_sources() -> None:
            _verify_source_fixpoint(namespace, capture)
            checkpoint()

        try:
            unsafe = _cleanup_owned_namespace(namespace)
            if unsafe:
                raise StructuredStage17PublicationError(
                    "unsafe prepublication namespace: " + "; ".join(unsafe)
                )
            checkpoint()
            extra = set(namespace.direct_entries()) - set(
                (*_FINAL_NAMES, STRUCTURED_STAGE17_STAGING)
            )
            if extra:
                raise StructuredStage17PublicationError(
                    f"structured Stage 17 extra namespace entries: {sorted(extra)}"
                )
            verify_sources()
            outputs = _produce_outputs(
                capture, llm, checkpoint=checkpoint
            )
            _semantic_replay(outputs, capture, manifest=None)
            namespace.stage_flat_files_new(
                STRUCTURED_STAGE17_STAGING, outputs
            )
            staged, directories = namespace.read_flat_tree(
                STRUCTURED_STAGE17_STAGING
            )
            if directories or tuple(sorted(staged)) != tuple(
                sorted(STRUCTURED_STAGE17_OUTPUTS)
            ):
                raise StructuredStage17PublicationError(
                    "staged structured Stage 17 namespace mismatch"
                )
            _semantic_replay(staged, capture, manifest=None)
            verify_sources()
            namespace.publish_staged_files_exclusive(
                STRUCTURED_STAGE17_STAGING,
                direct_names=STRUCTURED_STAGE17_OUTPUTS,
            )
            verify_sources()
            manifest = _build_manifest(outputs, capture)
            namespace.write_new_text_atomic(
                STRUCTURED_STAGE17_MANIFEST, manifest.decode("utf-8")
            )
            first = _capture_final(namespace, capture)
            checkpoint()
            verify_sources()
            second = _capture_final(namespace, capture)
            checkpoint()
            verify_sources()
            if first != second:
                raise StructuredStage17PublicationError(
                    "structured Stage 17 final capture is unstable"
                )
            _verify_final_snapshot_unchanged(namespace, second, capture)
            checkpoint()
            require_namespace_owned_by_epoch(namespace, lease, "stage-17")
            owner._structured_stage17_publication_context = (
                _StructuredStage17PublishedContext(
                    capture=capture,
                    snapshot=first,
                    run_ctime_ns=run_ctime_ns,
                )
            )
            return first
        except Exception as exc:
            try:
                _cleanup_owned_namespace(namespace)
            except Exception as cleanup_exc:
                exc.add_note(
                    f"structured Stage 17 cleanup also failed: {cleanup_exc}"
                )
            raise


def invalidate_structured_stage17_authority(
    lease: ReleaseGraphLock,
) -> tuple[str, ...]:
    """Manifest-first aggregate invalidation for dispatch/capture failures."""

    writer = require_active_writer_invalidation_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    try:
        namespace = lease.open_stage_namespace("stage-17")
    except FileNotFoundError:
        _clear_published_context(owner)
        return ()
    try:
        with namespace:
            return _cleanup_owned_namespace(namespace)
    finally:
        _clear_published_context(owner)


def has_structured_stage17_published_context(lease: object) -> bool:
    if not isinstance(lease, ReleaseGraphLock):
        return False
    try:
        owner = lease._require_active()
    except RuntimeError:
        return False
    return isinstance(
        getattr(owner, "_structured_stage17_publication_context", None),
        _StructuredStage17PublishedContext,
    )


def validate_structured_stage17_executor_postcondition(
    lease: ReleaseGraphLock,
    *,
    artifacts: tuple[str, ...],
) -> None:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    context = getattr(owner, "_structured_stage17_publication_context", None)
    if not isinstance(context, _StructuredStage17PublishedContext):
        raise StructuredStage17PublicationError(
            "structured Stage 17 postcondition lacks its published context"
        )
    expected_artifacts = (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST)
    if artifacts != expected_artifacts:
        raise StructuredStage17PublicationError(
            "structured Stage 17 postcondition artifact tuple mismatch"
        )
    with writer.open_stage_namespace("stage-17") as namespace:
        require_namespace_owned_by_epoch(namespace, writer, "stage-17")
        _assert_structured_transaction_canonical(
            namespace, context.run_ctime_ns
        )
        current = _capture_final(namespace, context.capture)
        _assert_structured_transaction_canonical(
            namespace, context.run_ctime_ns
        )
        if current != context.snapshot:
            raise StructuredStage17PublicationError(
                "structured Stage 17 postcondition snapshot mismatch"
            )


def clear_structured_stage17_published_context(
    lease: ReleaseGraphLock,
) -> None:
    writer = require_active_writer_invalidation_epoch(lease.run_dir, lease)
    _clear_published_context(writer._require_active())


def _clear_published_context(owner: ReleaseGraphLock) -> None:
    try:
        delattr(owner, "_structured_stage17_publication_context")
    except AttributeError:
        pass


def _produce_outputs(
    capture: StructuredStage17SourceCapture,
    llm: LLMClient | None,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, bytes]:
    if llm is None:
        raise StructuredStage17PublicationError(
            "structured Stage 17 selection provider is required"
        )
    registry = build_scientific_claim_registry(capture.binding)
    responses: list[bytes] = []
    for section_id in SECTION_ORDER:
        claims = [
            {
                "claim_id": claim.claim_id,
                "mandatory": claim.mandatory,
                "rendered_sentence": claim.rendered_sentence,
            }
            for claim in registry.claims
            if claim.section_id == section_id
        ]
        prompt = canonical_authority_json_text(
            {
                "section_id": section_id,
                "claims": claims,
                "response_fields": [
                    "selected_claim_ids",
                    "ordered_claim_ids",
                    "connector_template_ids",
                ],
            }
        )
        try:
            response = llm.chat(
                [{"role": "user", "content": prompt}],
                system=(
                    "Select only supplied claim IDs for the requested section. "
                    "Return one strict JSON object with exactly the three requested "
                    "fields. Return no prose, markdown, section ID, hashes, or authority."
                ),
                json_mode=True,
                max_tokens=512,
                strip_thinking=True,
            )
            content = response.content
        except Exception as exc:
            raise StructuredStage17PublicationError(
                f"structured selection provider failed for {section_id}: {exc}"
            ) from exc
        if not isinstance(content, str):
            raise StructuredStage17PublicationError(
                f"structured provider returned non-text for {section_id}"
            )
        if checkpoint is not None:
            checkpoint()
        response_bytes = content.encode("utf-8")
        try:
            validate_scientific_claim_selection(
                response_bytes,
                target_section=section_id,
                binding=capture.binding,
            )
        except Exception as exc:
            raise StructuredStage17PublicationError(
                f"structured selection is invalid for {section_id}: {exc}"
            ) from exc
        responses.append(response_bytes)

    selection = build_scientific_claim_selection(
        tuple(responses), binding=capture.binding
    )
    paper = _assemble_paper(
        selection,
        capture.binding,
        citation_plan=capture.citation_plan,
    )
    structure = build_structured_paper_structure_report(
        paper, selection, binding=capture.binding
    )
    experiment = build_structured_experiment_fact_closure(
        paper, binding=capture.binding
    )
    experiment_bytes = canonical_experiment_fact_json_text(experiment).encode("utf-8")
    citation = build_citation_closure_from_texts(
        paper_text=paper.decode("utf-8"),
        structure_report_text=structure.decode("utf-8"),
        experiment_fact_report_text=experiment_bytes.decode("utf-8"),
        citation_plan_text=capture.citation_plan.decode("utf-8"),
        citation_allowlist_text=capture.citation_allowlist.decode("utf-8"),
    )
    citation_bytes = canonical_json_text(citation).encode("utf-8")
    if experiment.get("valid") is not True or citation.get("valid") is not True:
        raise StructuredStage17PublicationError(
            "structured Stage 17 real closure is incomplete: "
            f"experiment_valid={experiment.get('valid')!r}, "
            f"citation_valid={citation.get('valid')!r}, "
            f"experiment_violations="
            f"{experiment.get('structured_fact_violations')!r}, "
            f"experiment_unknown={experiment.get('unknown_numeric_values')!r}, "
            f"dataset_violations={experiment.get('dataset_claim_violations')!r}, "
            f"citation_missing={citation.get('missing_planned_keys')!r}, "
            f"citation_misplaced={citation.get('misplaced_planned_keys')!r}"
        )
    return {
        "scientific_evidence_facts.json": registry.facts_bytes(),
        "scientific_claim_registry.json": registry.claims_bytes(),
        "scientific_claim_selection.json": selection,
        "paper_draft.md": paper,
        "paper_structure_report.json": structure,
        "experiment_fact_closure_report.json": experiment_bytes,
        "citation_closure_report.json": citation_bytes,
    }


def _assemble_paper(
    selection: bytes,
    binding: ScientificClaimGenerationBinding,
    *,
    citation_plan: bytes,
) -> bytes:
    parts: list[bytes] = []
    plan = parse_citation_plan(citation_plan.decode("utf-8"))
    citation_sections: dict[str, list[str]] = {}
    for claim in plan["claims"]:
        section = claim["section_path"][0]
        key = claim["planned_citations"][0]["cite_key"]
        citation_sections.setdefault(section, []).append(
            _bind_citation_marker(claim["claim_text"], key)
        )
    for section, sentences in citation_sections.items():
        parts.append(f"## {section}\n\n".encode("utf-8"))
        parts.append(("\n\n".join(sentences) + "\n\n").encode("utf-8"))
    for section_id, rendered in replay_scientific_claim_selection_wrapper(
        selection, binding=binding
    ):
        parts.extend(
            (
                f"## {section_id.title()}\n\n".encode("utf-8"),
                rendered,
                b"\n\n",
            )
        )
    return b"".join(parts)


def _bind_citation_marker(claim_text: str, key: str) -> str:
    if claim_text.endswith((".", "!", "?")):
        return f"{claim_text[:-1]} [{key}]{claim_text[-1]}"
    return f"{claim_text} [{key}]"


def _semantic_replay(
    outputs: Mapping[str, bytes],
    capture: StructuredStage17SourceCapture,
    *,
    manifest: bytes | None,
) -> None:
    if set(outputs) != set(STRUCTURED_STAGE17_OUTPUTS):
        raise StructuredStage17PublicationError(
            "structured Stage 17 semantic namespace mismatch"
        )
    binding = capture.binding
    parse_scientific_evidence_facts(
        outputs["scientific_evidence_facts.json"], binding=binding
    )
    parse_scientific_claim_registry(
        outputs["scientific_claim_registry.json"], binding=binding
    )
    replay_stage17_paper_related_artifacts(
        binding=binding,
        claim_selection_content=outputs["scientific_claim_selection.json"],
        paper_draft_content=outputs["paper_draft.md"],
        paper_structure_report_content=outputs["paper_structure_report.json"],
        experiment_fact_closure_report_content=outputs[
            "experiment_fact_closure_report.json"
        ],
        citation_closure_report_content=outputs["citation_closure_report.json"],
        citation_plan_content=capture.citation_plan,
        citation_allowlist_content=capture.citation_allowlist,
    )
    expected = _build_manifest(outputs, capture)
    if manifest is not None:
        parse_stage17_scientific_claim_authority_manifest(
            manifest,
            binding=binding,
            facts_content=outputs["scientific_evidence_facts.json"],
            claim_registry_content=outputs["scientific_claim_registry.json"],
            claim_selection_content=outputs["scientific_claim_selection.json"],
            paper_draft_content=outputs["paper_draft.md"],
            paper_structure_report_content=outputs["paper_structure_report.json"],
            experiment_fact_closure_report_content=outputs[
                "experiment_fact_closure_report.json"
            ],
            citation_closure_report_content=outputs[
                "citation_closure_report.json"
            ],
            citation_plan_content=capture.citation_plan,
            citation_allowlist_content=capture.citation_allowlist,
        )
        if manifest != expected:
            raise StructuredStage17PublicationError(
                "structured Stage 17 manifest rebuild mismatch"
            )


def _build_manifest(
    outputs: Mapping[str, bytes],
    capture: StructuredStage17SourceCapture,
) -> bytes:
    return build_stage17_scientific_claim_authority_manifest(
        binding=capture.binding,
        facts_content=outputs["scientific_evidence_facts.json"],
        claim_registry_content=outputs["scientific_claim_registry.json"],
        claim_selection_content=outputs["scientific_claim_selection.json"],
        paper_draft_content=outputs["paper_draft.md"],
        paper_structure_report_content=outputs["paper_structure_report.json"],
        experiment_fact_closure_report_content=outputs[
            "experiment_fact_closure_report.json"
        ],
        citation_closure_report_content=outputs["citation_closure_report.json"],
        citation_plan_content=capture.citation_plan,
        citation_allowlist_content=capture.citation_allowlist,
    )


def _capture_final(
    namespace: BoundOutputNamespace,
    capture: StructuredStage17SourceCapture,
) -> StructuredStage17Snapshot:
    if namespace.direct_entries() != tuple(sorted(_FINAL_NAMES)):
        raise StructuredStage17PublicationError(
            "structured Stage 17 final namespace is not exact"
        )
    files = tuple(
        (name, namespace.read_regular_snapshot(name))
        for name in sorted(_FINAL_NAMES)
    )
    outputs = {
        name: item.content
        for name, item in files
        if name != STRUCTURED_STAGE17_MANIFEST
    }
    manifest = next(
        item.content
        for name, item in files
        if name == STRUCTURED_STAGE17_MANIFEST
    )
    _semantic_replay(outputs, capture, manifest=manifest)
    namespace.assert_canonical()
    return StructuredStage17Snapshot(
        files=files,
        sources=_recapture_source_files(namespace, capture),
        namespace_identity=namespace.canonical_identity(),
    )


def _verify_final_snapshot_unchanged(
    namespace: BoundOutputNamespace,
    expected: StructuredStage17Snapshot,
    capture: StructuredStage17SourceCapture,
) -> None:
    current = _capture_final(namespace, capture)
    if current != expected:
        raise StructuredStage17PublicationError(
            "structured Stage 17 final snapshot changed after second capture"
        )


def _cleanup_owned_namespace(namespace: BoundOutputNamespace) -> tuple[str, ...]:
    unsafe: list[str] = []
    errors: list[str] = []
    for index, name in enumerate(
        (STRUCTURED_STAGE17_MANIFEST, *STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_STAGING)
    ):
        try:
            info = os.stat(name, dir_fd=namespace._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            index == len(_FINAL_NAMES)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            unsafe.append(f"{name}: unsafe collision")
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    try:
        namespace.assert_canonical()
    except OSError as exc:
        errors.append(f"namespace identity: {exc}")
    if errors:
        raise StructuredStage17PublicationError(
            "structured Stage 17 authority cleanup incomplete: " + "; ".join(errors)
        )
    return tuple(unsafe)


def _capture_evidence_files(
    reader: ReleaseGraphLock,
    evidence: CanonicalExperimentEvidence,
) -> tuple[tuple[str, str, bytes], ...]:
    expected: dict[str, str] = {
        evidence.manifest_path: evidence.manifest_sha256,
        evidence.candidate_manifest_path: evidence.candidate_manifest_sha256,
        evidence.selected_result_manifest_path: (
            evidence.selected_result_manifest_sha256
        ),
        evidence.experiment_contract_path: evidence.experiment_contract_sha256,
        evidence.run_config_path: evidence.run_config_sha256,
    }
    candidate_root = evidence.candidate_manifest_path.rsplit("/", 1)[0]
    for artifact in (
        *evidence.artifacts,
        *evidence.project_artifacts,
        evidence.selected_execution_artifact,
        evidence.execution_policy_artifact,
    ):
        if artifact is not None:
            path = getattr(artifact, "path", None) or getattr(
                artifact, "source_path", None
            )
            if isinstance(path, str):
                if "/" not in path and artifact in evidence.artifacts:
                    path = f"{candidate_root}/{path}"
                expected[path] = artifact.sha256
    _collect_file_refs(evidence.manifest, expected)
    _collect_file_refs(evidence.candidate, expected, base=candidate_root)
    _collect_file_refs(evidence.selected_result, expected)
    files: list[tuple[str, str, bytes]] = []
    for path, digest in sorted(expected.items()):
        content = reader.read_run_file(path)
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            raise StructuredStage17PublicationError(
                f"structured source hash mismatch: {path}"
            )
        files.append((path, digest, content))
    return tuple(files)


def _collect_file_refs(
    value: object,
    result: dict[str, str],
    *,
    base: str | None = None,
) -> None:
    if isinstance(value, Mapping):
        path = value.get("path")
        digest = value.get("sha256")
        if (
            isinstance(path, str)
            and isinstance(digest, str)
            and len(digest) == 64
        ):
            if base is not None and "/" not in path:
                path = f"{base}/{path}"
            if _is_captured_run_source_path(path):
                result[path] = digest
        for child in value.values():
            _collect_file_refs(child, result, base=base)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_file_refs(child, result, base=base)


def _is_captured_run_source_path(path: str) -> bool:
    return bool(
        path.startswith("stage-")
        or path
        in {
            "canonical_experiment_evidence.json",
            "experiment_summary_best.json",
            "analysis_best.md",
        }
    )


def _recapture_source_files(
    namespace: BoundOutputNamespace,
    capture: StructuredStage17SourceCapture,
) -> tuple[tuple[str, str, bytes], ...]:
    current: list[tuple[str, str, bytes]] = []
    for path, digest, _content in capture.files:
        content = namespace.read_run_file(path)
        if hashlib.sha256(content).hexdigest() != digest:
            raise StructuredStage17PublicationError(
                f"structured source changed: {path}"
            )
        current.append((path, digest, content))
    if namespace.read_run_file(_CITATION_PLAN_PATH) != capture.citation_plan:
        raise StructuredStage17PublicationError("citation plan changed")
    if namespace.read_run_file(_CITATION_ALLOWLIST_PATH) != capture.citation_allowlist:
        raise StructuredStage17PublicationError("citation allowlist changed")
    current.extend(
        (
            (
                _CITATION_PLAN_PATH,
                hashlib.sha256(capture.citation_plan).hexdigest(),
                capture.citation_plan,
            ),
            (
                _CITATION_ALLOWLIST_PATH,
                hashlib.sha256(capture.citation_allowlist).hexdigest(),
                capture.citation_allowlist,
            ),
        )
    )
    return tuple(sorted(current))


def _verify_source_fixpoint(
    namespace: BoundOutputNamespace,
    capture: StructuredStage17SourceCapture,
) -> None:
    current = _recapture_source_files(namespace, capture)
    expected = tuple(
        sorted(
            (
                *capture.files,
                (
                    _CITATION_PLAN_PATH,
                    hashlib.sha256(capture.citation_plan).hexdigest(),
                    capture.citation_plan,
                ),
                (
                    _CITATION_ALLOWLIST_PATH,
                    hashlib.sha256(capture.citation_allowlist).hexdigest(),
                    capture.citation_allowlist,
                ),
            )
        )
    )
    if current != expected:
        raise StructuredStage17PublicationError(
            "structured Stage 17 source fixpoint failed"
        )
    namespace.assert_canonical()
