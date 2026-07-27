"""Inactive held-namespace publication boundary for structured Stage 19."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat
import weakref
from pathlib import Path
from typing import Mapping

from researchclaw.llm.client import LLMClient
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline.stage14_domain_evaluator import (
    load_domain_evaluator_canonical_evidence as load_canonical_experiment_evidence,
)
from researchclaw.pipeline.scientific_claim_authority import (
    ScientificClaimGenerationBinding,
    build_scientific_claim_generation_binding,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    load_canonical_experiment_evidence as load_dispatch_canonical_evidence,
)
from researchclaw.pipeline.sectional_revision import (
    ReviewLedger,
    extract_review_ledger,
)
from researchclaw.pipeline.stage17_structured_publication import (
    STRUCTURED_STAGE17_MANIFEST,
    STRUCTURED_STAGE17_OUTPUTS,
    _capture_evidence_files,
)
from researchclaw.pipeline.stage19_structured_authority import (
    ReplayedStage17Authority,
    Stage19OutputDigests,
    build_stage19_closures,
    build_stage19_manifest,
    build_stage19_selection,
    inherited_provider_responses,
    replay_stage17_authority,
    replay_stage19_manifest,
    replay_stage19_outputs,
    rerender_stage19_paper,
    validate_descendant_selection,
)
from researchclaw.pipeline.stage19_structured_transport import (
    build_selection_prompts,
    execute_bounded_selection_calls,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)


STRUCTURED_STAGE19_OUTPUTS = (
    "scientific_claim_selection.json",
    "scientific_claim_paper_revised.md",
    "scientific_claim_paper_structure_report.json",
    "scientific_claim_experiment_fact_closure_report.json",
    "scientific_claim_citation_closure_report.json",
)
STRUCTURED_STAGE19_MANIFEST = "scientific_claim_authority_manifest.json"
STRUCTURED_STAGE19_STAGING = ".stage19-structured-publication.staging"

_CONTEXT_CONSTRUCTION_AUTHORITY = object()
_CONTEXT_ISSUANCE_AUTHORITY = object()
_DISPATCH_CAPTURE_AUTHORITY = object()
_STAGE17_CAPABILITY_SNAPSHOT = {
    key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES
}
_STAGE18_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "valid",
        "source_reviews_path",
        "source_reviews_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "paper_structure_report_path",
        "paper_structure_report_sha256",
        "experiment_fact_closure_report_path",
        "experiment_fact_closure_report_sha256",
        "citation_closure_report_path",
        "citation_closure_report_sha256",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "comment_count",
        "issues",
    }
)


class StructuredStage19PublicationError(RuntimeError):
    """Structured Stage 19 admission, replay, or publication failed."""


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class StructuredStage19DispatchCapture:
    _construction_token: object
    _evidence: CanonicalExperimentEvidence
    _run_path: str
    _run_identity: tuple[int, int]

    def __init__(
        self,
        *,
        authority: object,
        construction_token: object,
        evidence: CanonicalExperimentEvidence,
        run_path: str,
        run_identity: tuple[int, int],
    ) -> None:
        if authority is not _DISPATCH_CAPTURE_AUTHORITY:
            raise TypeError("structured Stage 19 dispatch capture is private")
        object.__setattr__(self, "_construction_token", construction_token)
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)


@dataclass(frozen=True)
class _IssuedDispatchCapture:
    token: object
    run_path: str
    run_identity: tuple[int, int]
    evidence: CanonicalExperimentEvidence


@dataclass(frozen=True)
class RichFileSnapshot:
    path: str
    content: bytes
    sha256: str
    size: int
    mode: int
    link_count: int
    identity: tuple[int, int]

    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.path,
            self.content,
            self.sha256,
            self.size,
            self.mode,
            self.link_count,
            self.identity,
        )


@dataclass(frozen=True)
class StructuredStage19SnapshotA:
    files: tuple[RichFileSnapshot, ...]
    binding: ScientificClaimGenerationBinding
    stage17: ReplayedStage17Authority
    review_ledger: ReviewLedger
    review_structure_report: Mapping[str, object]
    run_identity: tuple[int, int]
    stage_identity: tuple[int, int]

    def file(self, path: str) -> RichFileSnapshot:
        matches = tuple(item for item in self.files if item.path == path)
        if len(matches) != 1:
            raise StructuredStage19PublicationError(
                f"Snapshot A missing exact source: {path}"
            )
        return matches[0]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return tuple(item.identity_tuple() for item in self.files)


@dataclass(frozen=True)
class StructuredStage19FinalSnapshot:
    files: tuple[RichFileSnapshot, ...]
    namespace_identity: tuple[
        tuple[int, int], tuple[int, int], tuple[str, ...]
    ]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return tuple(item.identity_tuple() for item in self.files)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class StructuredStage19VerifiedContext:
    _construction_token: object
    _writer_owner: object
    _run_identity: tuple[int, int]
    _run_path: str
    _stage_identity: tuple[int, int]
    generation_binding_sha256: str
    stage17_manifest_sha256: str
    snapshot_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority: object,
        construction_token: object,
        writer_owner: object,
        run_identity: tuple[int, int],
        run_path: str,
        stage_identity: tuple[int, int],
        generation_binding_sha256: str,
        stage17_manifest_sha256: str,
        snapshot_identity: tuple[object, ...],
    ) -> None:
        if authority is not _CONTEXT_CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 19 context construction is private")
        object.__setattr__(self, "_construction_token", construction_token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(
            self, "stage17_manifest_sha256", stage17_manifest_sha256
        )
        object.__setattr__(self, "snapshot_identity", snapshot_identity)


@dataclass
class _IssuedContext:
    token: object
    owner: ReleaseGraphLock
    run_identity: tuple[int, int]
    run_path: str
    stage_identity: tuple[int, int]
    generation_binding_sha256: str
    stage17_manifest_sha256: str
    snapshot_identity: tuple[object, ...]
    phase: str = "issued"


@dataclass(frozen=True)
class _PublishedContext:
    snapshot_a: StructuredStage19SnapshotA
    final_snapshot: StructuredStage19FinalSnapshot
    verified_context: StructuredStage19VerifiedContext


_ISSUED_CONTEXTS: weakref.WeakKeyDictionary[
    StructuredStage19VerifiedContext, _IssuedContext
] = weakref.WeakKeyDictionary()
_ISSUED_DISPATCH_CAPTURES: weakref.WeakKeyDictionary[
    StructuredStage19DispatchCapture, _IssuedDispatchCapture
] = weakref.WeakKeyDictionary()


def issue_structured_stage19_dispatch_capture(
    run_dir: Path,
) -> StructuredStage19DispatchCapture:
    """Strictly load and code-issue the one routing/consumer evidence capture."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage19_dispatch_capture", mode="read"
    ) as lease:
        owner = lease._require_active()
        evidence = load_dispatch_canonical_evidence(run_dir)
        lease.assert_canonical()
        token = object()
        run_path = str(owner.run_dir.absolute())
        capture = StructuredStage19DispatchCapture(
            authority=_DISPATCH_CAPTURE_AUTHORITY,
            construction_token=token,
            evidence=evidence,
            run_path=run_path,
            run_identity=owner._run_identity,
        )
        _ISSUED_DISPATCH_CAPTURES[capture] = _IssuedDispatchCapture(
            token, run_path, owner._run_identity, evidence
        )
        return capture


def replay_structured_stage19_dispatch_capture(
    capture: object,
    run_dir: Path,
) -> CanonicalExperimentEvidence:
    """Validate a code-issued capture against the current canonical run."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage19_dispatch_replay", mode="read"
    ) as lease:
        return _evidence_from_dispatch_capture(capture, lease)


def _evidence_from_dispatch_capture(
    capture: object,
    lease: ReleaseGraphLock,
) -> CanonicalExperimentEvidence:
    if not isinstance(capture, StructuredStage19DispatchCapture):
        raise StructuredStage19PublicationError(
            "structured Stage 19 dispatch capture type mismatch"
        )
    issued = _ISSUED_DISPATCH_CAPTURES.get(capture)
    owner = lease._require_active()
    run_path = str(owner.run_dir.absolute())
    if (
        issued is None
        or issued.token is not capture._construction_token
        or issued.evidence is not capture._evidence
        or issued.run_path != capture._run_path
        or issued.run_identity != capture._run_identity
        or capture._run_path != run_path
        or capture._run_identity != owner._run_identity
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 dispatch capture was not issued for this run"
        )
    lease.assert_canonical()
    return issued.evidence


def _capture_snapshot_a_and_issue_context(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
    dispatch_capture: StructuredStage19DispatchCapture | None = None,
) -> tuple[StructuredStage19SnapshotA, StructuredStage19VerifiedContext]:
    """Capture and fully replay all Stage 17/18 sources under one writer epoch."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    require_namespace_owned_by_epoch(namespace, writer, "stage-19")  # type: ignore[arg-type]
    try:
        evidence = (
            _evidence_from_dispatch_capture(dispatch_capture, writer)
            if dispatch_capture is not None
            else load_canonical_experiment_evidence(writer.run_dir)
        )
        writer.assert_canonical()
        binding = build_scientific_claim_generation_binding(evidence)
        evidence_inventory = _capture_evidence_files(writer, evidence)
        writer.assert_canonical()
    except Exception as exc:
        raise StructuredStage19PublicationError(
            f"Snapshot A canonical evidence replay failed: {exc}"
        ) from exc

    ordered_paths: list[str] = [
        *(f"stage-17/{name}" for name in STRUCTURED_STAGE17_OUTPUTS),
        f"stage-17/{STRUCTURED_STAGE17_MANIFEST}",
    ]
    source_closure_digests = {
        binding.canonical_experiment_evidence_path: (
            binding.canonical_experiment_evidence_sha256
        ),
        binding.experiment_contract_path: binding.experiment_contract_sha256,
        binding.run_config_path: binding.run_config_sha256,
    }
    for path, digest, _content in evidence_inventory:
        previous = source_closure_digests.setdefault(path, digest)
        if previous != digest:
            raise StructuredStage19PublicationError(
                f"Snapshot A source closure digest mismatch: {path}"
            )
    for path in sorted(source_closure_digests):
        if path not in ordered_paths:
            ordered_paths.append(path)
    for path in (
        "stage-16/citation_plan.json",
        "stage-06/citation_allowlist.json",
        "stage-18/reviews.md",
        "stage-18/review_structure_report.json",
    ):
        if path not in ordered_paths:
            ordered_paths.append(path)
    snapshots = tuple(_read_rich_run_snapshot(writer, path) for path in ordered_paths)
    if len({item.path for item in snapshots}) != len(snapshots):
        raise StructuredStage19PublicationError("Snapshot A contains duplicate paths")
    snapshot_map = {item.path: item for item in snapshots}
    for path, digest, content in evidence_inventory:
        captured = snapshot_map.get(path)
        if (
            captured is None
            or captured.content != content
            or captured.sha256 != digest
        ):
            raise StructuredStage19PublicationError(
                f"Snapshot A evidence inventory mismatch: {path}"
            )
    stage17_files = {
        name: snapshot_map[f"stage-17/{name}"].content
        for name in (*STRUCTURED_STAGE17_OUTPUTS, STRUCTURED_STAGE17_MANIFEST)
    }
    try:
        stage17 = replay_stage17_authority(
            stage17_files,
            binding=binding,
            citation_plan=snapshot_map["stage-16/citation_plan.json"].content,
            citation_allowlist=snapshot_map[
                "stage-06/citation_allowlist.json"
            ].content,
            expected_capability_snapshot=_STAGE17_CAPABILITY_SNAPSHOT,
        )
        ledger = extract_review_ledger(
            snapshot_map["stage-18/reviews.md"].content.decode("utf-8"),
            source_path="stage-18/reviews.md",
        )
        review_report = _replay_stage18_report(snapshot_map, binding, ledger)
    except Exception as exc:
        raise StructuredStage19PublicationError(
            f"Snapshot A Stage 17/18 semantic replay failed: {exc}"
        ) from exc
    writer.assert_canonical()
    snapshot = StructuredStage19SnapshotA(
        snapshots,
        binding,
        stage17,
        ledger,
        review_report,
        writer._require_active()._run_identity,
        getattr(namespace, "_stage_identity"),
    )
    context = _issue_verified_context(
        writer, snapshot=snapshot, authority=_CONTEXT_ISSUANCE_AUTHORITY
    )
    return snapshot, context


def _read_rich_run_snapshot(
    lease: ReleaseGraphLock,
    relative_path: str,
) -> RichFileSnapshot:
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    parts = tuple(relative_path.split("/"))
    if (
        not parts
        or any(not part or part in {".", ".."} for part in parts)
        or "\\" in relative_path
    ):
        raise StructuredStage19PublicationError("Snapshot A path is unsafe")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.dup(owner._run_fd)
    opened = [descriptor]
    try:
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            opened.append(descriptor)
        entry = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise StructuredStage19PublicationError(
                f"Snapshot A source is not an unaliased regular file: {relative_path}"
            )
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(parts[-1], flags, dir_fd=descriptor)
        try:
            before = os.fstat(file_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (before.st_dev, before.st_ino)
                != (entry.st_dev, entry.st_ino)
            ):
                raise StructuredStage19PublicationError(
                    f"Snapshot A source is not an unaliased regular file: {relative_path}"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_fd)
            content = b"".join(chunks)
            identity = (before.st_dev, before.st_ino)
            if (
                identity != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mode != after.st_mode
                or before.st_nlink != after.st_nlink
                or after.st_nlink != 1
                or len(content) != before.st_size
            ):
                raise StructuredStage19PublicationError(
                    f"Snapshot A source changed during read: {relative_path}"
                )
            return RichFileSnapshot(
                relative_path,
                content,
                hashlib.sha256(content).hexdigest(),
                before.st_size,
                before.st_mode,
                before.st_nlink,
                identity,
            )
        finally:
            os.close(file_fd)
    finally:
        for opened_fd in reversed(opened):
            os.close(opened_fd)


def _replay_stage18_report(
    files: Mapping[str, RichFileSnapshot],
    binding: ScientificClaimGenerationBinding,
    ledger: ReviewLedger,
) -> Mapping[str, object]:
    report_file = files["stage-18/review_structure_report.json"]
    try:
        report = __import__("json").loads(
            report_file.content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except Exception as exc:
        raise StructuredStage19PublicationError(
            "Stage 18 structure report is not strict JSON"
        ) from exc
    if not isinstance(report, dict) or set(report) != _STAGE18_REPORT_FIELDS:
        raise StructuredStage19PublicationError(
            "Stage 18 structure report fields mismatch"
        )
    from researchclaw.pipeline.canonical_experiment_evidence import (
        canonical_authority_json_text,
    )

    if canonical_authority_json_text(report).encode("utf-8") != report_file.content:
        raise StructuredStage19PublicationError(
            "Stage 18 structure report is not canonical"
        )
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != 2
        or report["valid"] is not True
        or report["issues"] != []
        or type(report["comment_count"]) is not int
        or report["comment_count"] != len(ledger.comments)
    ):
        raise StructuredStage19PublicationError(
            "Stage 18 structure report authority mismatch"
        )
    expected = {
        "source_reviews": "stage-18/reviews.md",
        "source_paper": "stage-17/paper_draft.md",
        "paper_structure_report": "stage-17/paper_structure_report.json",
        "experiment_fact_closure_report": (
            "stage-17/experiment_fact_closure_report.json"
        ),
        "citation_closure_report": "stage-17/citation_closure_report.json",
        "canonical_experiment_evidence": (
            binding.canonical_experiment_evidence_path
        ),
    }
    for prefix, path in expected.items():
        snap = files[path]
        if (
            report[f"{prefix}_path"] != path
            or report[f"{prefix}_sha256"] != snap.sha256
        ):
            raise StructuredStage19PublicationError(
                f"Stage 18 report does not bind {path}"
            )
    return report


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredStage19PublicationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _issue_verified_context(
    lease: ReleaseGraphLock,
    *,
    snapshot: StructuredStage19SnapshotA,
    authority: object,
) -> StructuredStage19VerifiedContext:
    if authority is not _CONTEXT_ISSUANCE_AUTHORITY:
        raise TypeError("structured Stage 19 context issuance is private")
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if (
        snapshot.run_identity != owner._run_identity
        or not isinstance(snapshot.stage17, ReplayedStage17Authority)
        or not isinstance(snapshot.review_ledger, ReviewLedger)
        or not snapshot.files
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 context requires a verified Snapshot A"
        )
    token = object()
    run_path = str(owner.run_dir.absolute())
    stage17_manifest_sha256 = snapshot.file(
        f"stage-17/{STRUCTURED_STAGE17_MANIFEST}"
    ).sha256
    context = StructuredStage19VerifiedContext(
        authority=_CONTEXT_CONSTRUCTION_AUTHORITY,
        construction_token=token,
        writer_owner=owner,
        run_identity=owner._run_identity,
        run_path=run_path,
        stage_identity=snapshot.stage_identity,
        generation_binding_sha256=snapshot.binding.generation_binding_sha256,
        stage17_manifest_sha256=stage17_manifest_sha256,
        snapshot_identity=snapshot.identity_tuple,
    )
    _ISSUED_CONTEXTS[context] = _IssuedContext(
        token,
        owner,
        owner._run_identity,
        run_path,
        snapshot.stage_identity,
        snapshot.binding.generation_binding_sha256,
        stage17_manifest_sha256,
        snapshot.identity_tuple,
    )
    return context


def _require_verified_context(
    lease: ReleaseGraphLock,
    context: object,
    *,
    namespace: object,
    allowed_phases: tuple[str, ...] = ("issued", "publishing", "published"),
) -> ReleaseGraphLock:
    """Reject forged or stale pre-activation authority before output I/O."""

    if not isinstance(context, StructuredStage19VerifiedContext):
        raise StructuredStage19PublicationError(
            "structured Stage 19 context was not privately constructed"
        )
    issued = _ISSUED_CONTEXTS.get(context)
    if (
        issued is None
        or issued.phase not in allowed_phases
        or issued.token is not context._construction_token
        or issued.snapshot_identity != context.snapshot_identity
        or issued.generation_binding_sha256 != context.generation_binding_sha256
        or issued.stage17_manifest_sha256 != context.stage17_manifest_sha256
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 context was not issued"
        )
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    if owner is not issued.owner or context._writer_owner is not owner:
        raise StructuredStage19PublicationError(
            "structured Stage 19 context writer mismatch"
        )
    run_path = str(owner.run_dir.absolute())
    if (
        context._run_identity != owner._run_identity
        or issued.run_identity != owner._run_identity
        or context._run_path != run_path
        or issued.run_path != run_path
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 context run mismatch"
        )
    require_namespace_owned_by_epoch(namespace, writer, "stage-19")  # type: ignore[arg-type]
    if (
        getattr(namespace, "_stage_identity", None) != context._stage_identity
        or issued.stage_identity != context._stage_identity
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 context namespace mismatch"
        )
    return owner


def _publish_structured_stage19_from_context(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
    snapshot: StructuredStage19SnapshotA,
    context: StructuredStage19VerifiedContext,
    llm: LLMClient | None,
) -> StructuredStage19FinalSnapshot:
    """Run the frozen transaction after validating the live issued context."""

    owner = _require_verified_context(
        lease,
        context,
        namespace=namespace,
        allowed_phases=("issued",),
    )
    issued = _ISSUED_CONTEXTS[context]
    if (
        snapshot.identity_tuple != context.snapshot_identity
        or snapshot.binding.generation_binding_sha256
        != context.generation_binding_sha256
        or snapshot.run_identity != owner._run_identity
        or snapshot.stage_identity != getattr(namespace, "_stage_identity", None)
    ):
        _ISSUED_CONTEXTS.pop(context, None)
        raise StructuredStage19PublicationError(
            "structured Stage 19 context does not bind Snapshot A"
        )
    issued.phase = "publishing"
    _clear_published_context(owner)
    try:
        errors = _cleanup_owned_namespace(namespace)
        if errors:
            raise StructuredStage19PublicationError(
                "structured Stage 19 prepublication cleanup failed: "
                + "; ".join(errors)
            )
        extras = set(namespace.direct_entries()) - {  # type: ignore[attr-defined]
            *STRUCTURED_STAGE19_OUTPUTS,
            STRUCTURED_STAGE19_MANIFEST,
            STRUCTURED_STAGE19_STAGING,
        }
        if extras:
            raise StructuredStage19PublicationError(
                f"structured Stage 19 extra namespace entries: {sorted(extras)}"
            )
        outputs = _produce_outputs(snapshot, llm)
        _replay_outputs(outputs, snapshot)
        namespace.stage_flat_files_new(  # type: ignore[attr-defined]
            STRUCTURED_STAGE19_STAGING, outputs
        )
        staged, directories = namespace.read_flat_tree(  # type: ignore[attr-defined]
            STRUCTURED_STAGE19_STAGING
        )
        if directories or set(staged) != set(STRUCTURED_STAGE19_OUTPUTS):
            raise StructuredStage19PublicationError(
                "structured Stage 19 staged namespace mismatch"
            )
        _replay_outputs(staged, snapshot)
        _verify_source_fixpoint(lease, snapshot)
        namespace.publish_staged_files_exclusive(  # type: ignore[attr-defined]
            STRUCTURED_STAGE19_STAGING,
            direct_names=STRUCTURED_STAGE19_OUTPUTS,
        )
        if STRUCTURED_STAGE19_STAGING in namespace.direct_entries():  # type: ignore[attr-defined]
            raise StructuredStage19PublicationError(
                "structured Stage 19 staging remains after publication"
            )
        manifest = _build_manifest(outputs, snapshot)
        namespace.write_new_text_atomic(  # type: ignore[attr-defined]
            STRUCTURED_STAGE19_MANIFEST, manifest.decode("utf-8")
        )
        first = _capture_final(namespace, snapshot, manifest)
        _verify_source_fixpoint(lease, snapshot)
        second = _capture_final(namespace, snapshot, manifest)
        _verify_source_fixpoint(lease, snapshot)
        if first != second:
            raise StructuredStage19PublicationError(
                "structured Stage 19 final namespace is unstable"
            )
        _verify_final_snapshot_unchanged(namespace, second)
        owner._structured_stage19_publication_context = _PublishedContext(
            snapshot, first, context
        )
        issued.phase = "published"
        return first
    except Exception as exc:
        errors = _cleanup_owned_namespace(namespace)
        if errors:
            exc.add_note(
                "structured Stage 19 cleanup also failed: " + "; ".join(errors)
            )
        _clear_published_context(owner)
        _ISSUED_CONTEXTS.pop(context, None)
        raise


def _produce_outputs(
    snapshot: StructuredStage19SnapshotA,
    llm: LLMClient | None,
) -> dict[str, bytes]:
    source = snapshot.stage17.selection
    if not snapshot.review_ledger.comments:
        responses = inherited_provider_responses(source)
    else:
        if llm is None:
            raise StructuredStage19PublicationError(
                "structured Stage 19 nonempty review ledger requires provider"
            )
        prompts = build_selection_prompts(
            source_selection=source,
            binding=snapshot.binding,
            ledger=snapshot.review_ledger,
        )
        source_by_section = {
            section.section_id: section for section in source.sections
        }
        responses, _diagnostics = execute_bounded_selection_calls(
            prompts,
            llm,
            validate=lambda section_id, content: validate_descendant_selection(
                content,
                target_section=section_id,
                source=source_by_section[section_id],
                binding=snapshot.binding,
            ),
        )
    selection = build_stage19_selection(
        responses, source=source, binding=snapshot.binding
    )
    paper = rerender_stage19_paper(
        snapshot.stage17.paper,
        selection,
        source_selection=source,
        binding=snapshot.binding,
    )
    structure, experiment, citation = build_stage19_closures(
        paper,
        selection,
        source_selection=source,
        binding=snapshot.binding,
        citation_plan=snapshot.file("stage-16/citation_plan.json").content,
        citation_allowlist=snapshot.file(
            "stage-06/citation_allowlist.json"
        ).content,
    )
    return {
        "scientific_claim_selection.json": selection,
        "scientific_claim_paper_revised.md": paper,
        "scientific_claim_paper_structure_report.json": structure,
        "scientific_claim_experiment_fact_closure_report.json": experiment,
        "scientific_claim_citation_closure_report.json": citation,
    }


def _replay_outputs(
    outputs: Mapping[str, bytes],
    snapshot: StructuredStage19SnapshotA,
) -> None:
    replay_stage19_outputs(
        outputs,
        source_paper=snapshot.stage17.paper,
        source_selection=snapshot.stage17.selection,
        binding=snapshot.binding,
        citation_plan=snapshot.file("stage-16/citation_plan.json").content,
        citation_allowlist=snapshot.file(
            "stage-06/citation_allowlist.json"
        ).content,
    )


def _build_manifest(
    outputs: Mapping[str, bytes],
    snapshot: StructuredStage19SnapshotA,
) -> bytes:
    digests = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in outputs.items()
    }
    return build_stage19_manifest(
        binding=snapshot.binding,
        capability_snapshot=capability.code_owned_structured_capability_snapshot(),
        stage17_manifest=snapshot.stage17.manifest,
        stage17_manifest_sha256=snapshot.file(
            f"stage-17/{STRUCTURED_STAGE17_MANIFEST}"
        ).sha256,
        stage18_reviews_sha256=snapshot.file("stage-18/reviews.md").sha256,
        stage18_structure_report_sha256=snapshot.file(
            "stage-18/review_structure_report.json"
        ).sha256,
        stage18_comment_count=len(snapshot.review_ledger.comments),
        outputs=Stage19OutputDigests(
            digests["scientific_claim_selection.json"],
            digests["scientific_claim_paper_revised.md"],
            digests["scientific_claim_paper_structure_report.json"],
            digests["scientific_claim_experiment_fact_closure_report.json"],
            digests["scientific_claim_citation_closure_report.json"],
        ),
    )


def _verify_source_fixpoint(
    lease: ReleaseGraphLock,
    snapshot: StructuredStage19SnapshotA,
) -> None:
    current = tuple(_read_rich_run_snapshot(lease, item.path) for item in snapshot.files)
    if current != snapshot.files:
        raise StructuredStage19PublicationError("Snapshot A source fixpoint changed")
    lease.assert_canonical()


def _capture_final(
    namespace: object,
    snapshot: StructuredStage19SnapshotA,
    expected_manifest: bytes,
) -> StructuredStage19FinalSnapshot:
    expected_names = (*STRUCTURED_STAGE19_OUTPUTS, STRUCTURED_STAGE19_MANIFEST)
    entries = namespace.direct_entries()  # type: ignore[attr-defined]
    if set(entries) != set(expected_names) or STRUCTURED_STAGE19_STAGING in entries:
        raise StructuredStage19PublicationError(
            "structured Stage 19 final namespace mismatch"
        )
    files = tuple(
        _read_rich_stage_snapshot(namespace, name) for name in expected_names
    )
    output_map = {
        item.path: item.content
        for item in files
        if item.path != STRUCTURED_STAGE19_MANIFEST
    }
    _replay_outputs(output_map, snapshot)
    manifest = next(
        item.content for item in files if item.path == STRUCTURED_STAGE19_MANIFEST
    )
    replay_stage19_manifest(manifest, expected=expected_manifest)
    return StructuredStage19FinalSnapshot(
        files, namespace.canonical_identity()  # type: ignore[attr-defined]
    )


def _read_rich_stage_snapshot(
    namespace: object,
    name: str,
) -> RichFileSnapshot:
    file = namespace.read_regular_snapshot(name)  # type: ignore[attr-defined]
    info = os.stat(
        name,
        dir_fd=getattr(namespace, "_stage_fd"),
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino) != file.identity
        or info.st_size != file.size
    ):
        raise StructuredStage19PublicationError(
            f"structured Stage 19 final file is unsafe: {name}"
        )
    return RichFileSnapshot(
        name,
        file.content,
        hashlib.sha256(file.content).hexdigest(),
        info.st_size,
        info.st_mode,
        info.st_nlink,
        file.identity,
    )


def _verify_final_snapshot_unchanged(
    namespace: object,
    expected: StructuredStage19FinalSnapshot,
) -> None:
    names = (*STRUCTURED_STAGE19_OUTPUTS, STRUCTURED_STAGE19_MANIFEST)
    current = StructuredStage19FinalSnapshot(
        tuple(_read_rich_stage_snapshot(namespace, name) for name in names),
        namespace.canonical_identity(),  # type: ignore[attr-defined]
    )
    if current != expected:
        raise StructuredStage19PublicationError(
            "structured Stage 19 final snapshot changed after replay"
        )


def _cleanup_owned_namespace(namespace: object) -> tuple[str, ...]:
    errors: list[str] = []
    names = (
        STRUCTURED_STAGE19_MANIFEST,
        *STRUCTURED_STAGE19_OUTPUTS,
        STRUCTURED_STAGE19_STAGING,
    )
    for name in names:
        try:
            info = os.stat(
                name,
                dir_fd=getattr(namespace, "_stage_fd"),
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (
            name == STRUCTURED_STAGE19_STAGING
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            errors.append(f"{name}: unsafe collision")
        try:
            namespace.remove_flat_entries((name,))  # type: ignore[attr-defined]
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return tuple(errors)


def _clear_published_context(owner: ReleaseGraphLock) -> None:
    published = getattr(owner, "_structured_stage19_publication_context", None)
    if isinstance(published, _PublishedContext):
        _ISSUED_CONTEXTS.pop(published.verified_context, None)
    try:
        delattr(owner, "_structured_stage19_publication_context")
    except AttributeError:
        pass


def has_structured_stage19_published_context(lease: object) -> bool:
    if not isinstance(lease, ReleaseGraphLock):
        return False
    try:
        owner = lease._require_active()
    except RuntimeError:
        return False
    return isinstance(
        getattr(owner, "_structured_stage19_publication_context", None),
        _PublishedContext,
    )


def validate_structured_stage19_executor_postcondition(
    lease: ReleaseGraphLock,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> None:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    context = getattr(owner, "_structured_stage19_publication_context", None)
    if not isinstance(context, _PublishedContext):
        raise StructuredStage19PublicationError(
            "structured Stage 19 postcondition lacks published context"
        )
    expected = (*STRUCTURED_STAGE19_OUTPUTS, STRUCTURED_STAGE19_MANIFEST)
    if artifacts != expected or evidence_refs != tuple(
        f"stage-19/{name}" for name in expected
    ):
        raise StructuredStage19PublicationError(
            "structured Stage 19 postcondition artifact tuple mismatch"
        )
    with writer.open_stage_namespace("stage-19") as namespace:
        require_namespace_owned_by_epoch(namespace, writer, "stage-19")
        _require_verified_context(
            writer,
            context.verified_context,
            namespace=namespace,
            allowed_phases=("published",),
        )
        _verify_source_fixpoint(writer, context.snapshot_a)
        current = _capture_final(
            namespace,
            context.snapshot_a,
            next(
                item.content
                for item in context.final_snapshot.files
                if item.path == STRUCTURED_STAGE19_MANIFEST
            ),
        )
        if current != context.final_snapshot:
            raise StructuredStage19PublicationError(
                "structured Stage 19 postcondition snapshot mismatch"
            )
def clear_structured_stage19_published_context(lease: ReleaseGraphLock) -> None:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    _clear_published_context(writer._require_active())


def invalidate_structured_stage19_authority(
    lease: ReleaseGraphLock,
) -> tuple[str, ...]:
    owner = lease._require_active()
    if lease._mode != "write":
        raise RuntimeError("release_graph_writer_lease_required")
    try:
        namespace = lease.open_stage_namespace("stage-19")
    except FileNotFoundError:
        _clear_published_context(owner)
        return ()
    try:
        with namespace:
            return _cleanup_owned_namespace(namespace)
    finally:
        _clear_published_context(owner)


def execute_structured_stage19_revision(
    run_dir: Path,
    stage_dir: Path,
    llm: LLMClient | None,
) -> StructuredStage19FinalSnapshot:
    """Public/direct entry; capability guard is intentionally its first action."""

    capability.require_complete_structured_capability(
        "execute_structured_stage19_revision"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage19_revision", mode="write"
    ) as lease:
        if stage_dir != run_dir / "stage-19":
            raise StructuredStage19PublicationError(
                "structured Stage 19 directory is not canonical"
            )
        lease.ensure_run_directory("stage-19")
        with lease.open_stage_namespace("stage-19") as namespace:
            try:
                errors = _cleanup_owned_namespace(namespace)
                if errors:
                    raise StructuredStage19PublicationError(
                        "structured Stage 19 admission cleanup failed: "
                        + "; ".join(errors)
                    )
                extras = set(namespace.direct_entries()) - {
                    *STRUCTURED_STAGE19_OUTPUTS,
                    STRUCTURED_STAGE19_MANIFEST,
                    STRUCTURED_STAGE19_STAGING,
                }
                if extras:
                    raise StructuredStage19PublicationError(
                        "structured Stage 19 extra namespace entries: "
                        f"{sorted(extras)}"
                    )
                snapshot, context = _capture_snapshot_a_and_issue_context(
                    lease, namespace=namespace
                )
            except Exception as exc:
                cleanup_errors = _cleanup_owned_namespace(namespace)
                if cleanup_errors:
                    exc.add_note(
                        "structured Stage 19 capture cleanup also failed: "
                        + "; ".join(cleanup_errors)
                    )
                raise
            return _publish_structured_stage19_from_context(
                lease,
                namespace=namespace,
                snapshot=snapshot,
                context=context,
                llm=llm,
            )
