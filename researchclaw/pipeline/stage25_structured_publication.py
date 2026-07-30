"""Private pre-activation structured Stage 25 verdict publication."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from researchclaw.pipeline import stage22_structured_publication as stage22
from researchclaw.pipeline import stage23_structured_authority as stage23_authority
from researchclaw.pipeline import stage24_structured_authority as stage24_authority
from researchclaw.pipeline import stage24_structured_publication as stage24
from researchclaw.pipeline import stage25_structured_authority as authority
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage25_publication import (
    STAGE25_PUBLICATION_POLICY_VERSION,
    _heuristic_suggestions,
)
from researchclaw.pipeline.stages import StageStatus


class StructuredStage25PublicationError(RuntimeError):
    """Structured Stage 25 admission, publication, or replay failed."""


STRUCTURED_STAGE25_ARTIFACTS = authority.STRUCTURED_STAGE25_ARTIFACTS
STRUCTURED_STAGE25_EVIDENCE_REFS = authority.STRUCTURED_STAGE25_EVIDENCE_REFS
_STAGE_NAME = "stage-25"
_AUDIT_NAME = "deai_audit.json"
_MANIFEST_NAME = "stage25_deai_manifest.json"
_CONSTRUCTION_AUTHORITY = object()


class _NonTransferableContext:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("structured Stage 25 context cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("structured Stage 25 context cannot be copied")

    def __reduce__(self):
        raise TypeError("structured Stage 25 context cannot be serialized")


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage25PreAdmissionContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_snapshot: tuple[int, int, int, int]
    stage24_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority_token: object,
        token: object,
        writer_owner: object,
        run_identity: tuple[int, int],
        stage24_identity: tuple[object, ...],
    ) -> None:
        if authority_token is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 25 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "logical_stage_id", "stage25")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(self, "stage24_identity", stage24_identity)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage25AttemptContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_identity: tuple[int, int]
    _stage_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_snapshot: tuple[int, int, int, int]

    def __init__(
        self,
        *,
        authority_token: object,
        token: object,
        writer_owner: object,
        run_identity: tuple[int, int],
        stage_identity: tuple[int, int],
    ) -> None:
        if authority_token is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 25 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(self, "logical_stage_id", "stage25")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )


@dataclass(frozen=True)
class _HeldFile:
    parent_fd: int
    name: str
    descriptor: int
    identity: tuple[int, int]
    logical_path: str
    expected_bytes: bytes


@dataclass
class _PreRecord:
    token: object
    owner: ReleaseGraphLock
    capture: stage24.CurrentStructuredStage24Capture
    finalizer: weakref.finalize
    phase: str = "pre_admission"


@dataclass
class _AttemptRecord:
    token: object
    owner: ReleaseGraphLock
    initial: stage24.CurrentStructuredStage24Capture
    namespace: BoundOutputNamespace
    phase: str = "namespace_bound"
    audit: _HeldFile | None = None
    manifest: _HeldFile | None = None
    verdict: str | None = None
    final_snapshot: tuple[object, ...] | None = None


@dataclass(frozen=True)
class ProvisionalStage25Result:
    status: StageStatus
    artifacts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    context: Stage25AttemptContext
    verdict: str


_PRE_CONTEXTS: weakref.WeakKeyDictionary[
    Stage25PreAdmissionContext, _PreRecord
] = weakref.WeakKeyDictionary()
_ATTEMPT_CONTEXTS: weakref.WeakKeyDictionary[
    Stage25AttemptContext, _AttemptRecord
] = weakref.WeakKeyDictionary()


def issue_stage25_pre_admission_context(
    lease: ReleaseGraphLock,
) -> Stage25PreAdmissionContext:
    """Replay current Stage 24 before any Stage 25 pathname access."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_private_capability()
    capture = stage24.capture_current_structured_stage24(writer)
    stage24.verify_current_structured_stage24(capture)
    token = object()
    context = Stage25PreAdmissionContext(
        authority_token=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=owner,
        run_identity=owner._run_identity,
        stage24_identity=capture.identity_tuple,
    )
    finalizer = weakref.finalize(context, capture.close)
    _PRE_CONTEXTS[context] = _PreRecord(token, owner, capture, finalizer)
    return context


def transition_stage25_pre_admission_context(
    lease: ReleaseGraphLock,
    context: Stage25PreAdmissionContext,
) -> Stage25AttemptContext:
    pre = _require_pre_context(lease, context)
    try:
        stage24.verify_current_structured_stage24(pre.capture)
        namespace = _acquire_absent_stage25_namespace(pre.owner)
    except Exception:
        pre.finalizer.detach()
        _PRE_CONTEXTS.pop(context, None)
        pre.phase = "cleared"
        pre.capture.close()
        raise
    token = object()
    attempt = Stage25AttemptContext(
        authority_token=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=pre.owner,
        run_identity=pre.owner._run_identity,
        stage_identity=namespace._stage_identity,
    )
    pre.finalizer.detach()
    _PRE_CONTEXTS.pop(context, None)
    pre.phase = "transitioned"
    _ATTEMPT_CONTEXTS[attempt] = _AttemptRecord(
        token,
        pre.owner,
        pre.capture,
        namespace,
    )
    return attempt


def produce_structured_stage25(
    lease: ReleaseGraphLock,
    context: Stage25AttemptContext,
) -> ProvisionalStage25Result:
    record = _require_attempt_context(
        lease, context, allowed_phases=("namespace_bound",)
    )
    stage24.verify_current_structured_stage24(record.initial)
    record.phase = "snapshot_a"
    audit_bytes = _build_audit(record.initial)
    _replay_audit(record.initial, audit_bytes)
    record.audit = _create_held_file(
        record.namespace._stage_fd,
        _AUDIT_NAME,
        audit_bytes,
        logical_path=f"{_STAGE_NAME}/{_AUDIT_NAME}",
    )
    stage24.verify_current_structured_stage24(record.initial)
    manifest_value = _build_manifest(record.initial, audit_bytes)
    authority.validate_stage25_manifest_v2(manifest_value)
    manifest_bytes = stage24_authority.global_canonical_json_bytes(
        manifest_value
    )
    record.manifest = _create_held_file(
        record.namespace._stage_fd,
        _MANIFEST_NAME,
        manifest_bytes,
        logical_path=f"{_STAGE_NAME}/{_MANIFEST_NAME}",
    )
    record.verdict = manifest_value["release_verdict"]["verdict"]
    _verify_complete_publication(record)
    stage24.verify_current_structured_stage24(record.initial)
    snapshot = _capture_final_state(record)
    if _capture_final_state(record) != snapshot:
        raise StructuredStage25PublicationError(
            "Stage 25 second final capture mismatch"
        )
    record.final_snapshot = snapshot
    record.phase = "provisional_done"
    return ProvisionalStage25Result(
        StageStatus.DONE,
        STRUCTURED_STAGE25_ARTIFACTS,
        STRUCTURED_STAGE25_EVIDENCE_REFS,
        context,
        record.verdict,
    )


def validate_structured_stage25_immediate_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage25Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage25AttemptContext,
) -> None:
    record = _require_attempt_context(
        lease, context, allowed_phases=("provisional_done",)
    )
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    stage24.verify_current_structured_stage24(record.initial)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage25PublicationError(
            "Stage 25 immediate snapshot changed"
        )
    record.phase = "immediate_validated"


def validate_structured_stage25_terminal_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage25Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage25AttemptContext,
) -> None:
    record = _require_attempt_context(
        lease, context, allowed_phases=("immediate_validated",)
    )
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    stage24.verify_current_structured_stage24(record.initial)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage25PublicationError(
            "Stage 25 terminal snapshot changed"
        )
    record.phase = "terminal_validated"


def clear_structured_stage25_context(
    lease: ReleaseGraphLock,
    context: Stage25AttemptContext,
) -> None:
    record = _require_attempt_context(
        lease, context, allowed_phases=("terminal_validated",)
    )
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt(record)


def fail_structured_stage25_attempt(
    lease: ReleaseGraphLock,
    context: Stage25AttemptContext,
) -> tuple[str, ...]:
    record = _require_attempt_context(
        lease,
        context,
        allowed_phases=(
            "namespace_bound",
            "snapshot_a",
            "provisional_done",
            "immediate_validated",
            "terminal_validated",
        ),
    )
    errors = _cleanup_attempt_outputs(record)
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt(record)
    return errors


def context_phase(context: object) -> str:
    if isinstance(context, Stage25PreAdmissionContext):
        record = _PRE_CONTEXTS.get(context)
    elif isinstance(context, Stage25AttemptContext):
        record = _ATTEMPT_CONTEXTS.get(context)
    else:
        raise StructuredStage25PublicationError("unknown Stage 25 context")
    return "cleared" if record is None else record.phase


def execute_structured_stage25_verdict(
    run_dir: Path,
    stage_dir: Path,
):
    """Public/direct structured entry remains blocked until exact 1111."""

    capability.require_complete_structured_capability(
        "execute_structured_stage25_verdict"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage25_verdict", mode="write"
    ) as lease:
        if stage_dir != run_dir / _STAGE_NAME:
            raise StructuredStage25PublicationError(
                "structured Stage 25 directory is not canonical"
            )
        from researchclaw.pipeline.executor import (
            _execute_structured_stage25_with_pre_admission_private,
        )

        return _execute_structured_stage25_with_pre_admission_private(
            lease,
            canonical_stage_dir=True,
        )


def _build_audit(
    capture: stage24.CurrentStructuredStage24Capture,
) -> bytes:
    try:
        paper_text = capture.paper.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredStage25PublicationError(
            "Stage 25 paper is not UTF-8"
        ) from exc
    suggestions = _heuristic_suggestions(paper_text)
    value = {
        "schema_version": 2,
        "publication_policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
        "recommend_only": True,
        "applied": False,
        "paper": _ref(capture.paper),
        "source_stage24_manifest": _ref(capture.manifest),
        "suggestions": suggestions,
        "counts": {
            "total": len(suggestions),
            "touches_claim": sum(
                item["risk"] == "touches_claim" for item in suggestions
            ),
        },
        "rework_rule": (
            "If suggestions are adopted, rerun the canonical citation and truth "
            "stages required by the affected claim spans; never edit automatically."
        ),
    }
    authority.validate_stage25_audit_v2(value)
    return stage24_authority.global_canonical_json_bytes(value)


def _replay_audit(
    capture: stage24.CurrentStructuredStage24Capture,
    content: bytes,
) -> None:
    value = stage24_authority.strict_json_object(
        content, label="Stage 25 de-AI audit"
    )
    authority.validate_stage25_audit_v2(value)
    if content != _build_audit(capture):
        raise StructuredStage25PublicationError(
            "Stage 25 audit independent replay mismatch"
        )


def _build_manifest(
    capture: stage24.CurrentStructuredStage24Capture,
    audit_bytes: bytes,
) -> dict[str, object]:
    stage24_manifest = capture.manifest_value
    stage22_manifest = stage22.replay_structured_stage22_manifest_v2(
        capture.stage22_manifest.content
    )
    stage23_manifest = stage23_authority.parse_verification_manifest_v2(
        capture.stage23_manifest.content
    )
    pdf_valid = _pdf_is_valid(capture, stage22_manifest)
    verdict = authority.derive_release_verdict(
        stage24_outcome=stage24_manifest["outcome"],
        quality_outcome=stage24_manifest["quality_outcome"],
        stage23_outcome=stage23_manifest["outcome"],
        degradation_signal=stage24_manifest["degradation_signal"],
        claim_scope=stage24_manifest["claim_scope"],
        compiler_outcome=stage22_manifest["compile"]["outcome"],
        pdf_valid=pdf_valid,
    )
    return {
        "schema_version": 2,
        "publication_stage_id": "stage25",
        "publication_mode": authority.PUBLICATION_MODE,
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": _capability_dict(),
        "generation_binding_sha256": stage24_manifest[
            "generation_binding_sha256"
        ],
        "canonical_experiment_evidence": copy.deepcopy(
            stage24_manifest["canonical_experiment_evidence"]
        ),
        "cfs": copy.deepcopy(stage24_manifest["cfs"]),
        "source_stage19_manifest": copy.deepcopy(
            stage24_manifest["source_stage19_manifest"]
        ),
        "source_stage20_manifest": copy.deepcopy(
            stage24_manifest["source_stage20_manifest"]
        ),
        "source_stage21_manifest": copy.deepcopy(
            stage24_manifest["source_stage21_manifest"]
        ),
        "source_stage22_manifest": copy.deepcopy(
            stage24_manifest["source_stage22_manifest"]
        ),
        "source_stage23_manifest": copy.deepcopy(
            stage24_manifest["source_stage23_manifest"]
        ),
        "source_stage24_manifest": _ref(capture.manifest),
        "source_paper": _ref(capture.paper),
        "quality_outcome": stage24_manifest["quality_outcome"],
        "degradation_signal": copy.deepcopy(
            stage24_manifest["degradation_signal"]
        ),
        "claim_scope": stage24_manifest["claim_scope"],
        "stage24_outcome": stage24_manifest["outcome"],
        "stage24_output_count": stage24_manifest["output_count"],
        "stage24_outputs": copy.deepcopy(stage24_manifest["outputs"]),
        "deai_policy": {
            "policy_version": STAGE25_PUBLICATION_POLICY_VERSION,
            "recommend_only": True,
            "applied": False,
            "provider_calls": 0,
        },
        "output_count": 1,
        "outputs": [
            {
                "role": "deai_audit",
                "logical_name": None,
                "path": f"{_STAGE_NAME}/{_AUDIT_NAME}",
                "sha256": hashlib.sha256(audit_bytes).hexdigest(),
                "size": len(audit_bytes),
            }
        ],
        "release_verdict": verdict,
        "generated": stage24_manifest["generated"],
    }


def _pdf_is_valid(
    capture: stage24.CurrentStructuredStage24Capture,
    stage22_manifest: Mapping[str, object],
) -> bool:
    compile_value = stage22_manifest["compile"]
    pdf_ref = compile_value["paper_pdf"]
    if compile_value["outcome"] != "compiler-success" or type(pdf_ref) is not dict:
        return False
    matches = tuple(
        item
        for item in capture._record.initial.base.stage22_outputs
        if item.path == "stage-22/paper.pdf"
    )
    if len(matches) != 1:
        return False
    item = matches[0]
    return (
        pdf_ref
        == {"path": item.path, "sha256": item.sha256, "size": item.size}
        and item.content.startswith(b"%PDF-")
        and b"%%EOF" in item.content[-4096:]
    )


def _verify_complete_publication(record: _AttemptRecord) -> None:
    if record.audit is None or record.manifest is None:
        raise StructuredStage25PublicationError(
            "Stage 25 publication is incomplete"
        )
    record.namespace.assert_canonical()
    if tuple(record.namespace.direct_entries()) != (
        _AUDIT_NAME,
        _MANIFEST_NAME,
    ):
        raise StructuredStage25PublicationError(
            "Stage 25 exact namespace mismatch"
        )
    audit_bytes = _read_held_file(record.audit)
    manifest_bytes = _read_held_file(record.manifest)
    _replay_audit(record.initial, audit_bytes)
    value = stage24_authority.strict_json_object(
        manifest_bytes, label="Stage 25 verdict manifest"
    )
    authority.validate_stage25_manifest_v2(value)
    expected = _build_manifest(record.initial, audit_bytes)
    expected_bytes = stage24_authority.global_canonical_json_bytes(expected)
    if value != expected or manifest_bytes != expected_bytes:
        raise StructuredStage25PublicationError(
            "Stage 25 manifest independent replay mismatch"
        )
    if record.verdict is not None and record.verdict != value[
        "release_verdict"
    ]["verdict"]:
        raise StructuredStage25PublicationError(
            "Stage 25 verdict context mismatch"
        )


def _capture_final_state(record: _AttemptRecord) -> tuple[object, ...]:
    _verify_complete_publication(record)
    assert record.audit is not None and record.manifest is not None
    return (
        record.initial.identity_tuple,
        record.namespace._run_identity,
        record.namespace._stage_identity,
        (
            record.audit.identity,
            hashlib.sha256(_read_held_file(record.audit)).hexdigest(),
            len(_read_held_file(record.audit)),
        ),
        (
            record.manifest.identity,
            hashlib.sha256(_read_held_file(record.manifest)).hexdigest(),
            len(_read_held_file(record.manifest)),
        ),
        record.verdict,
    )


def _validate_provisional(
    record: _AttemptRecord,
    provisional: ProvisionalStage25Result,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> None:
    if (
        provisional.status is not StageStatus.DONE
        or provisional.context not in _ATTEMPT_CONTEXTS
        or provisional.artifacts != STRUCTURED_STAGE25_ARTIFACTS
        or provisional.evidence_refs != STRUCTURED_STAGE25_EVIDENCE_REFS
        or artifacts != STRUCTURED_STAGE25_ARTIFACTS
        or evidence_refs != STRUCTURED_STAGE25_EVIDENCE_REFS
        or provisional.verdict != record.verdict
        or provisional.verdict not in {"eligible", "blocked"}
    ):
        raise StructuredStage25PublicationError(
            "Stage 25 provisional tuple/status mismatch"
        )


def _require_pre_context(
    lease: ReleaseGraphLock,
    context: Stage25PreAdmissionContext,
) -> _PreRecord:
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if type(context) is not Stage25PreAdmissionContext:
        raise StructuredStage25PublicationError(
            "Stage 25 pre-admission context type mismatch"
        )
    record = _PRE_CONTEXTS.get(context)
    if (
        record is None
        or context._token is not record.token
        or context._writer_owner is not owner
        or record.owner is not owner
        or context._run_identity != owner._run_identity
        or context.logical_stage_id != "stage25"
        or context.structured_capability_snapshot != (1, 1, 1, 0)
        or context.stage24_identity != record.capture.identity_tuple
    ):
        raise StructuredStage25PublicationError(
            "Stage 25 pre-admission context identity mismatch"
        )
    return record


def _require_attempt_context(
    lease: ReleaseGraphLock,
    context: Stage25AttemptContext,
    *,
    allowed_phases: tuple[str, ...],
) -> _AttemptRecord:
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if type(context) is not Stage25AttemptContext:
        raise StructuredStage25PublicationError(
            "Stage 25 attempt context type mismatch"
        )
    record = _ATTEMPT_CONTEXTS.get(context)
    if (
        record is None
        or context._token is not record.token
        or context._writer_owner is not owner
        or record.owner is not owner
        or context._run_identity != owner._run_identity
        or context._stage_identity != record.namespace._stage_identity
        or context.logical_stage_id != "stage25"
        or context.structured_capability_snapshot != (1, 1, 1, 0)
        or record.phase not in allowed_phases
    ):
        raise StructuredStage25PublicationError(
            "Stage 25 attempt context identity mismatch"
        )
    require_namespace_owned_by_epoch(record.namespace, lease, _STAGE_NAME)
    return record


def _acquire_absent_stage25_namespace(
    owner: ReleaseGraphLock,
) -> BoundOutputNamespace:
    run_fd = os.dup(owner._run_fd)
    stage_fd = -1
    created_identity: tuple[int, int] | None = None
    try:
        run_info = os.fstat(run_fd)
        if (
            not stat.S_ISDIR(run_info.st_mode)
            or (run_info.st_dev, run_info.st_ino) != owner._run_identity
        ):
            raise StructuredStage25PublicationError(
                "Stage 25 held run parent mismatch"
            )
        try:
            os.stat(_STAGE_NAME, dir_fd=run_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StructuredStage25PublicationError(
                "Stage 25 canonical namespace already exists"
            )
        os.mkdir(_STAGE_NAME, mode=0o700, dir_fd=run_fd)
        created = os.stat(_STAGE_NAME, dir_fd=run_fd, follow_symlinks=False)
        created_identity = (created.st_dev, created.st_ino)
        stage_fd = os.open(
            _STAGE_NAME,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=run_fd,
        )
        info = os.fstat(stage_fd)
        named = os.stat(_STAGE_NAME, dir_fd=run_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != created_identity
            or created_identity != (named.st_dev, named.st_ino)
            or info.st_nlink < 2
            or os.listdir(stage_fd)
        ):
            raise StructuredStage25PublicationError(
                "Stage 25 created namespace mismatch"
            )
        return BoundOutputNamespace(
            run_dir=owner.run_dir,
            stage_dir=owner.run_dir / _STAGE_NAME,
            stage_name=_STAGE_NAME,
            _run_fd=run_fd,
            _stage_fd=stage_fd,
            _run_identity=owner._run_identity,
            _stage_identity=created_identity,
        )
    except Exception:
        if stage_fd >= 0:
            os.close(stage_fd)
        if created_identity is not None:
            try:
                named = os.stat(
                    _STAGE_NAME, dir_fd=run_fd, follow_symlinks=False
                )
                if (
                    stat.S_ISDIR(named.st_mode)
                    and (named.st_dev, named.st_ino) == created_identity
                ):
                    os.rmdir(_STAGE_NAME, dir_fd=run_fd)
            except OSError:
                pass
        os.close(run_fd)
        raise


def _create_held_file(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    logical_path: str,
) -> _HeldFile:
    descriptor = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short Stage 25 write")
            view = view[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(content)
            or identity != (named.st_dev, named.st_ino)
        ):
            raise StructuredStage25PublicationError(
                f"Stage 25 created file mismatch: {logical_path}"
            )
        return _HeldFile(
            parent_fd,
            name,
            descriptor,
            identity,
            logical_path,
            content,
        )
    except Exception:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(descriptor)
        raise


def _read_held_file(item: _HeldFile) -> bytes:
    info = os.fstat(item.descriptor)
    named = os.stat(item.name, dir_fd=item.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino) != item.identity
        or (named.st_dev, named.st_ino) != item.identity
    ):
        raise StructuredStage25PublicationError(
            f"Stage 25 held file identity changed: {item.logical_path}"
        )
    os.lseek(item.descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(item.descriptor, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    content = b"".join(chunks)
    after = os.fstat(item.descriptor)
    if (
        len(content) != after.st_size
        or (after.st_dev, after.st_ino) != item.identity
    ):
        raise StructuredStage25PublicationError(
            f"Stage 25 held file changed: {item.logical_path}"
        )
    return content


def _cleanup_attempt_outputs(record: _AttemptRecord) -> tuple[str, ...]:
    errors: list[str] = []
    for held in (record.manifest, record.audit):
        if held is None:
            continue
        try:
            named = os.stat(
                held.name, dir_fd=held.parent_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or (named.st_dev, named.st_ino) != held.identity
            ):
                errors.append(f"{held.logical_path}: identity collision")
                continue
            os.unlink(held.name, dir_fd=held.parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{held.logical_path}: {exc}")
    try:
        named = os.stat(
            _STAGE_NAME,
            dir_fd=record.namespace._run_fd,
            follow_symlinks=False,
        )
        if (named.st_dev, named.st_ino) != record.namespace._stage_identity:
            errors.append("stage-25: canonical name identity collision")
        elif os.listdir(record.namespace._stage_fd):
            errors.append("stage-25: owned namespace remains nonempty")
        else:
            os.rmdir(_STAGE_NAME, dir_fd=record.namespace._run_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"stage-25: {exc}")
    return tuple(errors)


def _close_attempt(record: _AttemptRecord) -> None:
    for held in (record.audit, record.manifest):
        if held is not None:
            try:
                os.close(held.descriptor)
            except OSError:
                pass
    record.namespace.close()
    record.initial.close()


def _ref(item: BoundArtifact) -> dict[str, object]:
    return {
        "path": item.path,
        "sha256": item.sha256,
        "size": len(item.content),
    }


def _capability_tuple() -> tuple[int, int, int, int]:
    snapshot = capability.code_owned_structured_capability_snapshot()
    return tuple(
        snapshot[name] for name in capability.REQUIRED_STRUCTURED_CAPABILITIES
    )


def _capability_dict() -> dict[str, int]:
    return capability.code_owned_structured_capability_snapshot()


def _require_private_capability() -> None:
    if (
        capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION != 1
        or _capability_tuple() != (1, 1, 1, 0)
    ):
        raise StructuredStage25PublicationError(
            "private structured Stage 25 requires exact capability 1110"
        )
