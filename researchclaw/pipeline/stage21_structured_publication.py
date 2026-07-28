"""Private pre-activation publication for deterministic structured Stage 21."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from researchclaw.pipeline import (
    stage19_structured_publication as stage19,
)
from researchclaw.pipeline import (
    stage20_structured_publication as stage20,
)
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.stage20_structured_authority import (
    replay_degradation_signal,
    replay_fabrication_flags,
    replay_quality_gate_manifest,
    replay_quality_report,
)
from researchclaw.pipeline.stage21_structured_authority import (
    BoundSource,
    Stage21AuthorityInputs,
    build_bundle_index_v2,
    render_archive,
    replay_bundle_index_v2,
)
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus


STRUCTURED_STAGE21_OUTPUTS = ("archive.md", "bundle_index.json")
STRUCTURED_STAGE21_TEMPS = ("archive.md.tmp", "bundle_index.json.tmp")
STRUCTURED_STAGE21_NAMESPACE = (
    *STRUCTURED_STAGE21_OUTPUTS,
    *STRUCTURED_STAGE21_TEMPS,
)
STRUCTURED_STAGE21_ARTIFACTS = STRUCTURED_STAGE21_OUTPUTS
STRUCTURED_STAGE21_EVIDENCE_REFS = (
    "stage-21/archive.md",
    "stage-21/bundle_index.json",
)
_STAGE19_NAMES = (
    *stage19.STRUCTURED_STAGE19_OUTPUTS,
    stage19.STRUCTURED_STAGE19_MANIFEST,
)
_STAGE20_NAMES = (
    *stage20.STRUCTURED_STAGE20_OUTPUTS,
    stage20.STRUCTURED_STAGE20_MANIFEST,
)
_CONSTRUCTION_AUTHORITY = object()


class StructuredStage21PublicationError(RuntimeError):
    """Private structured Stage 21 admission or publication failed."""


class _NonTransferableContext:
    def __copy__(self):
        raise TypeError("structured Stage 21 contexts cannot be copied")

    def __deepcopy__(self, _memo):
        raise TypeError("structured Stage 21 contexts cannot be copied")

    def __reduce__(self):
        raise TypeError("structured Stage 21 contexts cannot be serialized")


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class PreAdmissionContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_path: str
    _run_identity: tuple[int, int]
    _root_parent_identity: tuple[int, int]
    _stage20_identity: tuple[int, int]
    _root_signal_parent_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_schema_version: int
    structured_capability_snapshot: tuple[int, int, int, int]
    generation_binding_sha256: str
    cfs_sha256: str
    stage19_manifest_sha256: str
    stage20_manifest_sha256: str
    upstream_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority: object,
        token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        root_parent_identity: tuple[int, int],
        stage20_identity: tuple[int, int],
        root_signal_parent_identity: tuple[int, int],
        generation_binding_sha256: str,
        cfs_sha256: str,
        stage19_manifest_sha256: str,
        stage20_manifest_sha256: str,
        upstream_identity: tuple[object, ...],
    ) -> None:
        if authority is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 21 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(
            self, "_root_parent_identity", root_parent_identity
        )
        object.__setattr__(self, "_stage20_identity", stage20_identity)
        object.__setattr__(
            self,
            "_root_signal_parent_identity",
            root_signal_parent_identity,
        )
        object.__setattr__(self, "logical_stage_id", "stage21")
        object.__setattr__(
            self,
            "structured_capability_schema_version",
            capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION,
        )
        object.__setattr__(
            self,
            "structured_capability_snapshot",
            _capability_tuple(),
        )
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(self, "cfs_sha256", cfs_sha256)
        object.__setattr__(
            self, "stage19_manifest_sha256", stage19_manifest_sha256
        )
        object.__setattr__(
            self, "stage20_manifest_sha256", stage20_manifest_sha256
        )
        object.__setattr__(self, "upstream_identity", upstream_identity)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage21AttemptContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_path: str
    _run_identity: tuple[int, int]
    _stage_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_snapshot: tuple[int, int, int, int]
    upstream_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority: object,
        token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        stage_identity: tuple[int, int],
        upstream_identity: tuple[object, ...],
    ) -> None:
        if authority is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 21 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(self, "logical_stage_id", "stage21")
        object.__setattr__(
            self,
            "structured_capability_snapshot",
            _capability_tuple(),
        )
        object.__setattr__(self, "upstream_identity", upstream_identity)


@dataclass(frozen=True)
class _UpstreamSnapshot:
    sources: tuple[stage19.RichFileSnapshot, ...]
    stage19_snapshot: stage19.StructuredStage19SnapshotA
    authority_inputs: Stage21AuthorityInputs
    report_payload: Mapping[str, object]
    flags_payload: Mapping[str, object]
    manifest_payload: Mapping[str, object]
    signal_payload: Mapping[str, object] | None
    root_parent_identity: tuple[int, int]
    run_identity: tuple[int, int]
    stage20_identity: tuple[int, int]
    root_signal_parent_identity: tuple[int, int]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return (
            tuple(item.identity_tuple() for item in self.sources),
            self.root_parent_identity,
            self.run_identity,
            self.stage20_identity,
            self.root_signal_parent_identity,
            self.authority_inputs,
        )


@dataclass
class _CapturedUpstream:
    snapshot: _UpstreamSnapshot
    stage20_namespace: BoundOutputNamespace
    root_parent_fd: int
    root_signal_parent_fd: int

    def close(self) -> None:
        self.stage20_namespace.close()
        descriptors = (self.root_parent_fd, self.root_signal_parent_fd)
        self.root_parent_fd = -1
        self.root_signal_parent_fd = -1
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass
class _PreRecord:
    token: object
    owner: ReleaseGraphLock
    run_path: str
    capture: _CapturedUpstream
    finalizer: weakref.finalize
    phase: str = "pre_admission"


@dataclass(frozen=True)
class _HeldFile:
    directory_fd: int
    name: str
    descriptor: int
    identity: tuple[int, int]
    expected_bytes: bytes


@dataclass
class _AttemptRecord:
    token: object
    owner: ReleaseGraphLock
    run_path: str
    initial: _CapturedUpstream
    namespace: BoundOutputNamespace
    phase: str = "namespace_bound"
    archive: bytes | None = None
    index: bytes | None = None
    formal_archive: _HeldFile | None = None
    formal_index: _HeldFile | None = None
    final_snapshot: tuple[object, ...] | None = None


@dataclass(frozen=True)
class ProvisionalStage21Result:
    status: StageStatus
    artifacts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    context: Stage21AttemptContext


_PRE_CONTEXTS: weakref.WeakKeyDictionary[
    PreAdmissionContext, _PreRecord
] = weakref.WeakKeyDictionary()
_ATTEMPT_CONTEXTS: weakref.WeakKeyDictionary[
    Stage21AttemptContext, _AttemptRecord
] = weakref.WeakKeyDictionary()


def issue_stage21_pre_admission_context(
    lease: ReleaseGraphLock,
) -> PreAdmissionContext:
    """Capture/replay all upstream authority before any Stage 21 access."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_private_capability()
    capture = _capture_upstream(writer)
    token = object()
    snapshot = capture.snapshot
    context = PreAdmissionContext(
        authority=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=owner,
        run_path=str(owner.run_dir.absolute()),
        run_identity=snapshot.run_identity,
        root_parent_identity=snapshot.root_parent_identity,
        stage20_identity=snapshot.stage20_identity,
        root_signal_parent_identity=snapshot.root_signal_parent_identity,
        generation_binding_sha256=(
            snapshot.authority_inputs.generation_binding_sha256
        ),
        cfs_sha256=snapshot.authority_inputs.cfs_sha256,
        stage19_manifest_sha256=snapshot.authority_inputs.source(
            "source_stage19_manifest"
        ).sha256,
        stage20_manifest_sha256=snapshot.authority_inputs.source(
            "source_stage20_manifest"
        ).sha256,
        upstream_identity=snapshot.identity_tuple,
    )
    finalizer = weakref.finalize(context, capture.close)
    _PRE_CONTEXTS[context] = _PreRecord(
        token,
        owner,
        str(owner.run_dir.absolute()),
        capture,
        finalizer,
    )
    return context


def transition_stage21_pre_admission_context(
    lease: ReleaseGraphLock,
    context: object,
) -> Stage21AttemptContext:
    """Consume one pre-context and perform the first Stage 21 open/create."""

    record = _require_pre_context(lease, context)
    assert isinstance(context, PreAdmissionContext)
    owner = record.owner
    try:
        current = _capture_upstream(lease)
        try:
            _require_same_upstream(
                record.capture.snapshot,
                current.snapshot,
            )
        finally:
            current.close()
    except Exception as exc:
        _PRE_CONTEXTS.pop(context, None)
        record.finalizer.detach()
        record.phase = "expired"
        record.capture.close()
        raise StructuredStage21PublicationError(
            f"structured Stage 21 pre-admission replay failed: {exc}"
        ) from exc
    _PRE_CONTEXTS.pop(context, None)
    record.finalizer.detach()
    record.phase = "consumed"
    namespace: BoundOutputNamespace | None = None
    try:
        namespace = lease.open_stage_namespace(
            "stage-21", create_stage=True
        )
        require_namespace_owned_by_epoch(namespace, lease, "stage-21")
        token = object()
        attempt = Stage21AttemptContext(
            authority=_CONSTRUCTION_AUTHORITY,
            token=token,
            writer_owner=owner,
            run_path=str(owner.run_dir.absolute()),
            run_identity=owner._run_identity,
            stage_identity=namespace._stage_identity,
            upstream_identity=record.capture.snapshot.identity_tuple,
        )
        _ATTEMPT_CONTEXTS[attempt] = _AttemptRecord(
            token,
            owner,
            str(owner.run_dir.absolute()),
            record.capture,
            namespace,
        )
        return attempt
    except Exception as exc:
        if namespace is not None:
            namespace.close()
        record.capture.close()
        raise StructuredStage21PublicationError(
            f"structured Stage 21 namespace transition failed: {exc}"
        ) from exc


def produce_structured_stage21(
    lease: ReleaseGraphLock,
    context: object,
) -> ProvisionalStage21Result:
    """Publish a commit candidate and return only an internal provisional."""

    record = _require_attempt_context(
        lease, context, allowed_phases=("namespace_bound",)
    )
    assert isinstance(context, Stage21AttemptContext)
    held_temps: list[_HeldFile] = []
    try:
        errors = _invalidate_owned_namespace(record.namespace)
        if errors:
            raise StructuredStage21PublicationError(
                "manifest-first invalidation failed: " + "; ".join(errors)
            )
        _advance(record, "namespace_bound", "invalidated")
        captured_a = _capture_upstream(lease)
        try:
            _require_same_upstream(
                record.initial.snapshot, captured_a.snapshot
            )
        finally:
            captured_a.close()
        _advance(record, "invalidated", "snapshot_a")
        inputs = record.initial.snapshot.authority_inputs
        archive = render_archive(inputs)
        index = build_bundle_index_v2(inputs, archive_bytes=archive)
        archive_temp = _create_held_file(
            record.namespace._stage_fd,
            "archive.md.tmp",
            archive,
        )
        held_temps.append(archive_temp)
        index_temp = _create_held_file(
            record.namespace._stage_fd,
            "bundle_index.json.tmp",
            index,
        )
        held_temps.append(index_temp)
        staged_archive = _read_held_file(archive_temp)
        staged_index = _read_held_file(index_temp)
        replay_bundle_index_v2(
            staged_index,
            archive_bytes=staged_archive,
            expected=index,
        )
        _advance(record, "snapshot_a", "staged")
        _verify_upstream_fixpoint(lease, record.initial.snapshot)
        record.formal_archive = _create_held_file(
            record.namespace._stage_fd,
            "archive.md",
            staged_archive,
        )
        record.formal_index = _create_held_file(
            record.namespace._stage_fd,
            "bundle_index.json",
            staged_index,
        )
        temp_errors = _remove_temporaries(record.namespace)
        if temp_errors:
            raise StructuredStage21PublicationError(
                "temporary cleanup failed: " + "; ".join(temp_errors)
            )
        if tuple(sorted(record.namespace.direct_entries())) != tuple(
            sorted(STRUCTURED_STAGE21_OUTPUTS)
        ):
            raise StructuredStage21PublicationError(
                "structured Stage 21 final namespace mismatch"
            )
        _verify_formal_outputs(record, inputs)
        first = _capture_final_state(lease, record)
        second = _capture_final_state(lease, record)
        if first != second:
            raise StructuredStage21PublicationError(
                "structured Stage 21 final snapshots differ"
            )
        record.archive = archive
        record.index = index
        record.final_snapshot = first
        _advance(record, "staged", "published")
        _advance(record, "published", "provisional_done")
        return ProvisionalStage21Result(
            StageStatus.DONE,
            STRUCTURED_STAGE21_ARTIFACTS,
            STRUCTURED_STAGE21_EVIDENCE_REFS,
            context,
        )
    finally:
        for held in held_temps:
            _close_held(held)


def validate_structured_stage21_immediate_postcondition(
    lease: ReleaseGraphLock,
    provisional: object,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: object,
) -> None:
    record = _validate_provisional(
        lease,
        provisional,
        artifacts=artifacts,
        evidence_refs=evidence_refs,
        context=context,
        phase="provisional_done",
    )
    _verify_postcondition_state(lease, record)
    _advance(record, "provisional_done", "immediate_validated")


def validate_structured_stage21_terminal_postcondition(
    lease: ReleaseGraphLock,
    provisional: object,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: object,
) -> None:
    record = _validate_provisional(
        lease,
        provisional,
        artifacts=artifacts,
        evidence_refs=evidence_refs,
        context=context,
        phase="immediate_validated",
    )
    _verify_postcondition_state(lease, record)
    _advance(record, "immediate_validated", "terminal_validated")


def clear_structured_stage21_context(
    lease: ReleaseGraphLock,
    context: object,
) -> None:
    record = _require_attempt_context(
        lease, context, allowed_phases=("terminal_validated",)
    )
    assert isinstance(context, Stage21AttemptContext)
    _advance(record, "terminal_validated", "cleared")
    _ATTEMPT_CONTEXTS.pop(context, None)
    _close_attempt_record(record)


def fail_structured_stage21_attempt(
    lease: ReleaseGraphLock,
    context: object,
) -> tuple[str, ...]:
    """Withdraw a current attempt through held manifest-first cleanup."""

    if not isinstance(context, Stage21AttemptContext):
        return ()
    record = _ATTEMPT_CONTEXTS.get(context)
    if record is None:
        return ()
    writer = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if writer is not record.owner:
        raise StructuredStage21PublicationError(
            "structured Stage 21 cleanup writer mismatch"
        )
    record.phase = "failed_cleanup"
    errors = _cleanup_attempt_outputs(record.namespace)
    record.phase = "cleared"
    _ATTEMPT_CONTEXTS.pop(context, None)
    _close_attempt_record(record)
    return errors


def context_phase(context: object) -> str:
    if isinstance(context, PreAdmissionContext):
        record = _PRE_CONTEXTS.get(context)
    elif isinstance(context, Stage21AttemptContext):
        record = _ATTEMPT_CONTEXTS.get(context)
    else:
        record = None
    if record is None:
        raise StructuredStage21PublicationError(
            "structured Stage 21 context is not live"
        )
    return record.phase


def execute_structured_stage21_archive(
    run_dir: Path,
    stage_dir: Path,
) -> StageResult:
    """Public/direct entry; complete-capability guard is the first action."""

    capability.require_complete_structured_capability(
        "execute_structured_stage21_archive"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage21_archive", mode="write"
    ) as lease:
        if stage_dir != run_dir / "stage-21":
            raise StructuredStage21PublicationError(
                "structured Stage 21 directory is not canonical"
            )
        from researchclaw.pipeline.executor import (
            _execute_structured_stage21_private,
        )

        pre = issue_stage21_pre_admission_context(lease)
        return _execute_structured_stage21_private(lease, pre)


def _capture_upstream(lease: ReleaseGraphLock) -> _CapturedUpstream:
    """Capture and independently replay Stage 20 through held descriptors."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    root_parent_fd = stage20._open_root_parent(owner)
    root_signal_parent_fd = os.dup(owner._run_fd)
    stage20_namespace: BoundOutputNamespace | None = None
    try:
        root_parent_info = os.fstat(root_parent_fd)
        signal_parent_info = os.fstat(root_signal_parent_fd)
        stage20_namespace = writer.open_stage_namespace("stage-20")
        require_namespace_owned_by_epoch(
            stage20_namespace, writer, "stage-20"
        )
        if set(stage20_namespace.direct_entries()) != set(_STAGE20_NAMES):
            raise StructuredStage21PublicationError(
                "Stage 20 exact success namespace mismatch"
            )
        with writer.open_stage_namespace("stage-19") as stage19_namespace:
            require_namespace_owned_by_epoch(
                stage19_namespace, writer, "stage-19"
            )
            if set(stage19_namespace.direct_entries()) != set(_STAGE19_NAMES):
                raise StructuredStage21PublicationError(
                    "Stage 19 exact six-file namespace mismatch"
                )
            base, stage19_context = (
                stage19._capture_snapshot_a_and_issue_context(
                    writer, namespace=stage19_namespace
                )
            )
            stage19._ISSUED_CONTEXTS.pop(stage19_context, None)
            stage19_files = tuple(
                stage19._read_rich_run_snapshot(
                    writer, f"stage-19/{name}"
                )
                for name in _STAGE19_NAMES
            )
        output_map = {
            item.path.removeprefix("stage-19/"): item.content
            for item in stage19_files
            if item.path
            != f"stage-19/{stage19.STRUCTURED_STAGE19_MANIFEST}"
        }
        stage19._replay_outputs(output_map, base)
        stage19_manifest = next(
            item
            for item in stage19_files
            if item.path
            == f"stage-19/{stage19.STRUCTURED_STAGE19_MANIFEST}"
        )
        stored_stage19_manifest = _strict_object(
            stage19_manifest.content, "Stage 19 manifest"
        )
        digests = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in output_map.items()
        }
        expected_stage19_manifest = stage19.build_stage19_manifest(
            binding=base.binding,
            capability_snapshot=stored_stage19_manifest[
                "structured_capability_snapshot"
            ],
            stage17_manifest=base.stage17.manifest,
            stage17_manifest_sha256=base.file(
                f"stage-17/{stage19.STRUCTURED_STAGE17_MANIFEST}"
            ).sha256,
            stage18_reviews_sha256=base.file(
                "stage-18/reviews.md"
            ).sha256,
            stage18_structure_report_sha256=base.file(
                "stage-18/review_structure_report.json"
            ).sha256,
            stage18_comment_count=len(base.review_ledger.comments),
            outputs=stage19.Stage19OutputDigests(
                digests["scientific_claim_selection.json"],
                digests["scientific_claim_paper_revised.md"],
                digests[
                    "scientific_claim_paper_structure_report.json"
                ],
                digests[
                    "scientific_claim_experiment_fact_closure_report.json"
                ],
                digests[
                    "scientific_claim_citation_closure_report.json"
                ],
            ),
        )
        replayed_stage19_manifest = stage19.replay_stage19_manifest(
            stage19_manifest.content, expected=expected_stage19_manifest
        )
        stage20_files = tuple(
            stage19._read_rich_run_snapshot(
                writer, f"stage-20/{name}"
            )
            for name in _STAGE20_NAMES
        )
        file_map = {item.path: item for item in stage20_files}
        manifest_file = file_map[
            "stage-20/quality_gate_manifest.json"
        ]
        raw_manifest = _strict_object(
            manifest_file.content, "Stage 20 manifest"
        )
        raw_outcome = raw_manifest.get("outcome")
        if raw_outcome not in {"passed", "degraded"}:
            raise StructuredStage21PublicationError(
                "Stage 20 manifest outcome is invalid"
            )
        run_entries = set(stage20_namespace.run_entries())
        if stage20.STRUCTURED_STAGE20_ROOT_TEMP in run_entries:
            raise StructuredStage21PublicationError(
                "Stage 20 root signal temporary remains"
            )
        signal_file: stage19.RichFileSnapshot | None = None
        if raw_outcome == "passed":
            if stage20.STRUCTURED_STAGE20_SIGNAL in run_entries:
                raise StructuredStage21PublicationError(
                    "passed Stage 20 has a degradation signal"
                )
        else:
            signal_file = stage19._read_rich_run_snapshot(
                writer, stage20.STRUCTURED_STAGE20_SIGNAL
            )
        canonical_config = stage20.parse_config_snapshot_text(
            base.binding.evidence.run_config_bytes.decode("utf-8"),
            project_root=owner.run_dir,
            label="structured Stage 21 canonical config",
        )
        fabrication_state = stage20.reconstruct_stage20_fabrication_state(
            base.binding.evidence, canonical_config
        )
        stage20_source_snapshot = stage20.StructuredStage20SnapshotA(
            (*base.files, *stage19_files),
            base,
            stage19_files,
            replayed_stage19_manifest,
            canonical_config,
            fabrication_state,
            root_parent_fd,
            str(owner.run_dir.parent.absolute()),
            (root_parent_info.st_dev, root_parent_info.st_ino),
            owner.run_dir.name,
            owner._run_identity,
            stage20_namespace._stage_identity,
        )
        report_file = file_map["stage-20/quality_report.json"]
        flags_file = file_map["stage-20/fabrication_flags.json"]
        report_payload = replay_quality_report(
            report_file.content, expected=report_file.content
        )
        flags_payload = replay_fabrication_flags(
            flags_file.content, expected=flags_file.content
        )
        signal_payload = (
            None
            if signal_file is None
            else replay_degradation_signal(
                signal_file.content, expected=signal_file.content
            )
        )
        manifest_payload = replay_quality_gate_manifest(
            manifest_file.content,
            expected=manifest_file.content,
            signal_present=signal_file is not None,
        )
        stage20._require_independent_bundle_semantics(
            stage20_source_snapshot,
            report_payload=report_payload,
            flags_payload=flags_payload,
            manifest_payload=manifest_payload,
            signal_payload=signal_payload,
            report_bytes=report_file.content,
            flags_bytes=flags_file.content,
            signal_bytes=(
                None if signal_file is None else signal_file.content
            ),
        )
        current_manifest = stage19._read_rich_run_snapshot(
            writer, "stage-20/quality_gate_manifest.json"
        )
        if current_manifest != manifest_file:
            raise StructuredStage21PublicationError(
                "current Stage 20 manifest name changed"
            )
        sources = (
            *base.files,
            *stage19_files,
            *stage20_files,
            *(() if signal_file is None else (signal_file,)),
        )
        if len({item.path for item in sources}) != len(sources):
            raise StructuredStage21PublicationError(
                "Stage 21 upstream closure has duplicate paths"
            )
        evidence = next(
            item
            for item in sources
            if item.path
            == base.binding.canonical_experiment_evidence_path
        )
        paper = next(
            item
            for item in stage19_files
            if item.path
            == "stage-19/scientific_claim_paper_revised.md"
        )
        bound_sources = (
            _bound("canonical_experiment_evidence", evidence),
            _bound("source_stage19_manifest", stage19_manifest),
            _bound("source_paper", paper),
            _bound("stage20_quality_report", report_file),
            _bound("stage20_fabrication_flags", flags_file),
            *(
                ()
                if signal_file is None
                else (_bound("stage20_degradation_signal", signal_file),)
            ),
            _bound("source_stage20_manifest", manifest_file),
        )
        inputs = Stage21AuthorityInputs(
            base.binding.generation_binding_sha256,
            base.binding.cfs_sha256,
            str(manifest_payload["outcome"]),
            str(manifest_payload["generated"]),
            tuple(bound_sources),
        )
        render_archive(inputs)
        snapshot = _UpstreamSnapshot(
            tuple(sources),
            base,
            inputs,
            report_payload,
            flags_payload,
            manifest_payload,
            signal_payload,
            (root_parent_info.st_dev, root_parent_info.st_ino),
            owner._run_identity,
            stage20_namespace._stage_identity,
            (signal_parent_info.st_dev, signal_parent_info.st_ino),
        )
        stage20._verify_root_parent(
            root_parent_fd,
            root_parent_path=str(owner.run_dir.parent.absolute()),
            root_parent_identity=snapshot.root_parent_identity,
            run_name=owner.run_dir.name,
            run_identity=owner._run_identity,
        )
        return _CapturedUpstream(
            snapshot,
            stage20_namespace,
            root_parent_fd,
            root_signal_parent_fd,
        )
    except Exception:
        if stage20_namespace is not None:
            stage20_namespace.close()
        os.close(root_parent_fd)
        os.close(root_signal_parent_fd)
        raise


def _require_pre_context(
    lease: ReleaseGraphLock, context: object
) -> _PreRecord:
    if not isinstance(context, PreAdmissionContext):
        raise StructuredStage21PublicationError(
            "Stage 21 pre-admission context was not privately constructed"
        )
    record = _PRE_CONTEXTS.get(context)
    if (
        record is None
        or record.phase != "pre_admission"
        or record.token is not context._token
        or context.logical_stage_id != "stage21"
        or context.structured_capability_schema_version != 1
        or context.structured_capability_snapshot != _capability_tuple()
        or context.upstream_identity
        != record.capture.snapshot.identity_tuple
    ):
        raise StructuredStage21PublicationError(
            "Stage 21 pre-admission context was not issued"
        )
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    snapshot = record.capture.snapshot
    if (
        owner is not record.owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or record.run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or snapshot.run_identity != owner._run_identity
        or context._root_parent_identity != snapshot.root_parent_identity
        or context._stage20_identity != snapshot.stage20_identity
        or context._root_signal_parent_identity
        != snapshot.root_signal_parent_identity
        or context.generation_binding_sha256
        != snapshot.authority_inputs.generation_binding_sha256
        or context.cfs_sha256 != snapshot.authority_inputs.cfs_sha256
    ):
        raise StructuredStage21PublicationError(
            "Stage 21 pre-admission context binding mismatch"
        )
    return record


def _require_attempt_context(
    lease: ReleaseGraphLock,
    context: object,
    *,
    allowed_phases: tuple[str, ...],
) -> _AttemptRecord:
    if not isinstance(context, Stage21AttemptContext):
        raise StructuredStage21PublicationError(
            "Stage 21 attempt context was not privately constructed"
        )
    record = _ATTEMPT_CONTEXTS.get(context)
    if (
        record is None
        or record.phase not in allowed_phases
        or record.token is not context._token
        or context.logical_stage_id != "stage21"
        or context.structured_capability_snapshot != _capability_tuple()
        or context.upstream_identity
        != record.initial.snapshot.identity_tuple
    ):
        raise StructuredStage21PublicationError(
            "Stage 21 attempt context was not issued"
        )
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if (
        owner is not record.owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or record.namespace._run_identity != owner._run_identity
        or context._stage_identity != record.namespace._stage_identity
    ):
        raise StructuredStage21PublicationError(
            "Stage 21 attempt context binding mismatch"
        )
    require_namespace_owned_by_epoch(
        record.namespace, lease, "stage-21"
    )
    return record


def _validate_provisional(
    lease: ReleaseGraphLock,
    provisional: object,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: object,
    phase: str,
) -> _AttemptRecord:
    if (
        not isinstance(provisional, ProvisionalStage21Result)
        or provisional.status is not StageStatus.DONE
        or provisional.artifacts != STRUCTURED_STAGE21_ARTIFACTS
        or provisional.evidence_refs != STRUCTURED_STAGE21_EVIDENCE_REFS
        or provisional.context is not context
        or artifacts != STRUCTURED_STAGE21_ARTIFACTS
        or evidence_refs != STRUCTURED_STAGE21_EVIDENCE_REFS
    ):
        raise StructuredStage21PublicationError(
            "structured Stage 21 provisional handoff mismatch"
        )
    return _require_attempt_context(
        lease, context, allowed_phases=(phase,)
    )


def _verify_postcondition_state(
    lease: ReleaseGraphLock, record: _AttemptRecord
) -> None:
    if record.final_snapshot is None:
        raise StructuredStage21PublicationError(
            "structured Stage 21 final snapshot is missing"
        )
    _verify_formal_outputs(
        record, record.initial.snapshot.authority_inputs
    )
    current = _capture_final_state(lease, record)
    if current != record.final_snapshot:
        raise StructuredStage21PublicationError(
            "structured Stage 21 postcondition snapshot mismatch"
        )


def _capture_final_state(
    lease: ReleaseGraphLock, record: _AttemptRecord
) -> tuple[object, ...]:
    _verify_upstream_fixpoint(lease, record.initial.snapshot)
    if record.formal_archive is None or record.formal_index is None:
        raise StructuredStage21PublicationError(
            "structured Stage 21 formal descriptors are missing"
        )
    archive = _read_held_file(record.formal_archive)
    index = _read_held_file(record.formal_index)
    return (
        record.initial.snapshot.identity_tuple,
        _held_identity_tuple(record.formal_archive),
        _held_identity_tuple(record.formal_index),
        hashlib.sha256(archive).hexdigest(),
        len(archive),
        hashlib.sha256(index).hexdigest(),
        len(index),
        record.namespace.canonical_identity(),
        tuple(sorted(record.namespace.direct_entries())),
    )


def _verify_formal_outputs(
    record: _AttemptRecord, inputs: Stage21AuthorityInputs
) -> None:
    if record.formal_archive is None or record.formal_index is None:
        raise StructuredStage21PublicationError(
            "structured Stage 21 formal outputs are missing"
        )
    archive = _read_held_file(record.formal_archive)
    index = _read_held_file(record.formal_index)
    expected_archive = render_archive(inputs)
    expected_index = build_bundle_index_v2(
        inputs, archive_bytes=expected_archive
    )
    if archive != expected_archive:
        raise StructuredStage21PublicationError(
            "structured Stage 21 archive changed"
        )
    replay_bundle_index_v2(
        index, archive_bytes=archive, expected=expected_index
    )


def _verify_upstream_fixpoint(
    lease: ReleaseGraphLock, expected: _UpstreamSnapshot
) -> None:
    current = _capture_upstream(lease)
    try:
        _require_same_upstream(expected, current.snapshot)
    finally:
        current.close()


def _require_same_upstream(
    expected: _UpstreamSnapshot, current: _UpstreamSnapshot
) -> None:
    if current != expected:
        raise StructuredStage21PublicationError(
            "structured Stage 21 upstream snapshot changed"
        )


def _invalidate_owned_namespace(
    namespace: BoundOutputNamespace,
) -> tuple[str, ...]:
    errors: list[str] = []
    for name in (
        "bundle_index.json",
        "archive.md",
        "bundle_index.json.tmp",
        "archive.md.tmp",
    ):
        _unlink_collision(
            namespace._stage_fd,
            name,
            errors,
            temp=name.endswith(".tmp"),
        )
    return tuple(errors)


def _cleanup_attempt_outputs(
    namespace: BoundOutputNamespace,
) -> tuple[str, ...]:
    errors: list[str] = []
    for name in (
        "bundle_index.json",
        "archive.md",
        "bundle_index.json.tmp",
        "archive.md.tmp",
    ):
        _unlink_collision(
            namespace._stage_fd, name, errors, temp=False
        )
    return tuple(errors)


def _remove_temporaries(
    namespace: BoundOutputNamespace,
) -> tuple[str, ...]:
    errors: list[str] = []
    for name in ("bundle_index.json.tmp", "archive.md.tmp"):
        try:
            os.unlink(name, dir_fd=namespace._stage_fd)
        except FileNotFoundError:
            errors.append(f"{name}: temporary is missing")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    for name in ("bundle_index.json.tmp", "archive.md.tmp"):
        try:
            os.stat(
                name,
                dir_fd=namespace._stage_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        errors.append(f"{name}: temporary remains")
    return tuple(errors)


def _unlink_collision(
    directory_fd: int,
    name: str,
    errors: list[str],
    *,
    temp: bool,
) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if temp:
        errors.append(f"{name}: preexisting reserved temporary collision")
    if stat.S_ISDIR(info.st_mode) or (
        not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    ):
        errors.append(f"{name}: unsafe collision retained")
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except Exception as exc:
        errors.append(f"{name}: {exc}")


def _create_held_file(
    directory_fd: int, name: str, content: bytes
) -> _HeldFile:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StructuredStage21PublicationError(
                f"created Stage 21 file is unsafe: {name}"
            )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("structured Stage 21 write made no progress")
            offset += written
        os.fsync(descriptor)
        held = _HeldFile(
            directory_fd,
            name,
            descriptor,
            (info.st_dev, info.st_ino),
            bytes(content),
        )
        _read_held_file(held)
        return held
    except Exception:
        os.close(descriptor)
        raise


def _read_held_file(held: _HeldFile) -> bytes:
    info = os.fstat(held.descriptor)
    current = os.stat(
        held.name, dir_fd=held.directory_fd, follow_symlinks=False
    )
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (info.st_dev, info.st_ino) != held.identity
        or (current.st_dev, current.st_ino) != held.identity
    ):
        raise StructuredStage21PublicationError(
            f"Stage 21 held file identity changed: {held.name}"
        )
    content = b""
    offset = 0
    while offset < info.st_size:
        chunk = os.pread(held.descriptor, info.st_size - offset, offset)
        if not chunk:
            raise StructuredStage21PublicationError(
                f"Stage 21 held read stalled: {held.name}"
            )
        content += chunk
        offset += len(chunk)
    if content != held.expected_bytes:
        raise StructuredStage21PublicationError(
            f"Stage 21 held bytes changed: {held.name}"
        )
    return content


def _held_identity_tuple(held: _HeldFile) -> tuple[object, ...]:
    info = os.fstat(held.descriptor)
    return (
        held.name,
        held.identity,
        info.st_mode,
        info.st_nlink,
        info.st_size,
    )


def _close_attempt_record(record: _AttemptRecord) -> None:
    for held in (record.formal_archive, record.formal_index):
        if held is not None:
            _close_held(held)
    record.namespace.close()
    record.initial.close()


def _close_held(held: _HeldFile) -> None:
    try:
        os.close(held.descriptor)
    except OSError:
        pass


def _advance(
    record: _AttemptRecord, expected: str, target: str
) -> None:
    if record.phase != expected:
        raise StructuredStage21PublicationError(
            f"Stage 21 phase mismatch: {record.phase} != {expected}"
        )
    record.phase = target


def _bound(
    role: str, snapshot: stage19.RichFileSnapshot
) -> BoundSource:
    return BoundSource(role, snapshot.path, snapshot.sha256, snapshot.size)


def _strict_object(content: bytes, label: str) -> dict[str, object]:
    def reject(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise StructuredStage21PublicationError(
                    f"{label} has duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=reject
        )
    except StructuredStage21PublicationError:
        raise
    except Exception as exc:
        raise StructuredStage21PublicationError(
            f"{label} is not strict JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise StructuredStage21PublicationError(
            f"{label} root must be an object"
        )
    return payload


def _capability_tuple() -> tuple[int, int, int, int]:
    snapshot = capability.code_owned_structured_capability_snapshot()
    return tuple(
        snapshot[name] for name in capability.REQUIRED_STRUCTURED_CAPABILITIES
    )  # type: ignore[return-value]


def _require_private_capability() -> None:
    if _capability_tuple() != (1, 1, 1, 0):
        raise StructuredStage21PublicationError(
            "private structured Stage 21 requires exact capability 1110"
        )
