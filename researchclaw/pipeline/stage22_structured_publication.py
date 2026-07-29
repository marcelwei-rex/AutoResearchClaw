"""Private pre-activation deterministic structured Stage 22 publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import weakref
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from researchclaw.pipeline import stage19_structured_publication as stage19
from researchclaw.pipeline import stage21_structured_publication as stage21
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.stage21_structured_authority import (
    build_bundle_index_v2,
    render_archive,
    replay_bundle_index_v2,
)
from researchclaw.pipeline.stage22_semantics import (
    Stage22DeterministicOutputs,
    Stage22SemanticInputs,
    build_stage22_outputs_from_inputs,
)
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.templates.compiler import compile_latex


STRUCTURED_STAGE22_ARTIFACTS = (
    "paper_final.md",
    "code/",
    "stage22_export_manifest.json",
)
STRUCTURED_STAGE22_EVIDENCE_REFS = (
    "stage-22/paper_final.md",
    "stage-22/code/",
    "stage-22/stage22_export_manifest.json",
)
STRUCTURED_STAGE22_FIXED_ROLES = (
    "paper_markdown",
    "paper_latex_markdown",
    "bibliography",
    "paper_latex",
    "compile_status",
    "paper_verification",
    "sanitization_report",
    "canonical_source",
)
STRUCTURED_STAGE22_FIXED_FILES = (
    "paper_final.md",
    "paper_final_latex.md",
    "references.bib",
    "paper.tex",
    "compile_status.json",
    "paper_verification.json",
    "sanitization_report.json",
    "canonical_source.json",
)
STRUCTURED_STAGE22_MANIFEST = "stage22_export_manifest.json"
STRUCTURED_STAGE22_MANIFEST_FIELDS = (
    "schema_version",
    "publication_stage_id",
    "publication_mode",
    "structured_capability_schema_version",
    "structured_capability_snapshot",
    "generation_binding_sha256",
    "canonical_experiment_evidence",
    "cfs",
    "selected_result_manifest",
    "source_stage19_manifest",
    "source_stage20_manifest",
    "source_stage21_manifest",
    "source_stage21_archive",
    "source_paper",
    "quality_outcome",
    "degradation_signal",
    "bibliography_source",
    "template",
    "project_files",
    "compile",
    "output_count",
    "outputs",
    "generated",
)
_FIXED_ROLE_BY_NAME = dict(
    zip(STRUCTURED_STAGE22_FIXED_FILES, STRUCTURED_STAGE22_FIXED_ROLES)
)
_STAGE21_NAMES = set(stage21.STRUCTURED_STAGE21_OUTPUTS)
_CONSTRUCTION_AUTHORITY = object()


class StructuredStage22PublicationError(RuntimeError):
    """Structured Stage 22 admission, publication, or replay failed."""


class _NonTransferableContext:
    def __copy__(self):
        raise TypeError("structured Stage 22 contexts cannot be copied")

    def __deepcopy__(self, _memo):
        raise TypeError("structured Stage 22 contexts cannot be copied")

    def __reduce__(self):
        raise TypeError("structured Stage 22 contexts cannot be serialized")


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage22PreAdmissionContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_path: str
    _run_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_schema_version: int
    structured_capability_snapshot: tuple[int, int, int, int]
    generation_binding_sha256: str
    stage20_manifest_sha256: str
    stage21_manifest_sha256: str
    upstream_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority: object,
        token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        generation_binding_sha256: str,
        stage20_manifest_sha256: str,
        stage21_manifest_sha256: str,
        upstream_identity: tuple[object, ...],
    ) -> None:
        if authority is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 22 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "logical_stage_id", "stage22")
        object.__setattr__(
            self,
            "structured_capability_schema_version",
            capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION,
        )
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(
            self, "stage20_manifest_sha256", stage20_manifest_sha256
        )
        object.__setattr__(
            self, "stage21_manifest_sha256", stage21_manifest_sha256
        )
        object.__setattr__(self, "upstream_identity", upstream_identity)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage22AttemptContext(_NonTransferableContext):
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
            raise TypeError("structured Stage 22 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(self, "logical_stage_id", "stage22")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(self, "upstream_identity", upstream_identity)


@dataclass
class _CapturedUpstream:
    base: stage21._CapturedUpstream
    stage21_namespace: BoundOutputNamespace
    stage21_files: tuple[stage19.RichFileSnapshot, ...]
    semantic_inputs: Stage22SemanticInputs
    bibliography: stage19.RichFileSnapshot
    extra_sources: tuple[stage19.RichFileSnapshot, ...]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.base.snapshot.identity_tuple,
            tuple(item.identity_tuple() for item in self.stage21_files),
            self.stage21_namespace._stage_identity,
            self.semantic_inputs.paper_path,
            self.semantic_inputs.paper_sha256,
            self.semantic_inputs.quality_outcome,
            self.bibliography.identity_tuple(),
            tuple(item.identity_tuple() for item in self.extra_sources),
        )

    def close(self) -> None:
        self.stage21_namespace.close()
        self.base.close()


@dataclass
class _PreRecord:
    token: object
    owner: ReleaseGraphLock
    capture: _CapturedUpstream
    finalizer: weakref.finalize
    phase: str = "pre_admission"


@dataclass(frozen=True)
class _HeldDirectory:
    parent_fd: int
    name: str
    descriptor: int
    identity: tuple[int, int]
    logical_path: str


@dataclass(frozen=True)
class _HeldFile:
    parent_fd: int
    name: str
    descriptor: int
    identity: tuple[int, int]
    logical_path: str
    expected_bytes: bytes


@dataclass
class _AttemptRecord:
    token: object
    owner: ReleaseGraphLock
    initial: _CapturedUpstream
    namespace: BoundOutputNamespace
    phase: str = "namespace_bound"
    deterministic: Stage22DeterministicOutputs | None = None
    payloads: dict[str, bytes] | None = None
    held_directories: dict[str, _HeldDirectory] | None = None
    held_files: dict[str, _HeldFile] | None = None
    manifest: _HeldFile | None = None
    manifest_payload: dict[str, object] | None = None
    final_snapshot: tuple[object, ...] | None = None
    semantic_calls: int = 0


@dataclass(frozen=True)
class ProvisionalStage22Result:
    status: StageStatus
    artifacts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    context: Stage22AttemptContext


_PRE_CONTEXTS: weakref.WeakKeyDictionary[
    Stage22PreAdmissionContext, _PreRecord
] = weakref.WeakKeyDictionary()
_ATTEMPT_CONTEXTS: weakref.WeakKeyDictionary[
    Stage22AttemptContext, _AttemptRecord
] = weakref.WeakKeyDictionary()


def issue_stage22_pre_admission_context(
    lease: ReleaseGraphLock,
) -> Stage22PreAdmissionContext:
    """Replay Stage 21 and its full closure before any Stage 22 access."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_private_capability()
    capture = _capture_upstream(writer)
    token = object()
    stage20_manifest = _source(
        capture, "stage-20/quality_gate_manifest.json"
    )
    stage21_manifest = _source(capture, "stage-21/bundle_index.json")
    context = Stage22PreAdmissionContext(
        authority=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=owner,
        run_path=str(owner.run_dir.absolute()),
        run_identity=owner._run_identity,
        generation_binding_sha256=(
            capture.base.snapshot.authority_inputs.generation_binding_sha256
        ),
        stage20_manifest_sha256=stage20_manifest.sha256,
        stage21_manifest_sha256=stage21_manifest.sha256,
        upstream_identity=capture.identity_tuple,
    )
    finalizer = weakref.finalize(context, capture.close)
    _PRE_CONTEXTS[context] = _PreRecord(token, owner, capture, finalizer)
    return context


def _acquire_structured_stage22_namespace(
    owner: ReleaseGraphLock,
) -> BoundOutputNamespace:
    """Create and bind a new, strictly absent Stage 22 namespace."""

    stage_name = "stage-22"
    run_fd = os.dup(owner._run_fd)
    stage_fd = -1
    created_identity: tuple[int, int] | None = None
    try:
        run_info = os.fstat(run_fd)
        if (
            not stat.S_ISDIR(run_info.st_mode)
            or (run_info.st_dev, run_info.st_ino) != owner._run_identity
        ):
            raise StructuredStage22PublicationError(
                "structured Stage 22 held run parent mismatch"
            )
        try:
            os.stat(stage_name, dir_fd=run_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StructuredStage22PublicationError(
                "structured Stage 22 canonical namespace already exists"
            )
        os.mkdir(stage_name, mode=0o700, dir_fd=run_fd)
        created = os.stat(stage_name, dir_fd=run_fd, follow_symlinks=False)
        created_identity = (created.st_dev, created.st_ino)
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        stage_fd = os.open(stage_name, flags, dir_fd=run_fd)
        stage_info = _validate_new_directory(
            parent_fd=run_fd,
            parent_identity=owner._run_identity,
            name=stage_name,
            descriptor=stage_fd,
            expected_mode=0o700,
            logical_path="stage-22",
        )
        return BoundOutputNamespace(
            run_dir=owner.run_dir,
            stage_dir=owner.run_dir / stage_name,
            stage_name=stage_name,
            _run_fd=run_fd,
            _stage_fd=stage_fd,
            _run_identity=owner._run_identity,
            _stage_identity=(stage_info.st_dev, stage_info.st_ino),
        )
    except Exception:
        if stage_fd >= 0:
            _close_fd(stage_fd)
        if created_identity is not None:
            _remove_new_empty_directory(
                run_fd,
                stage_name,
                created_identity=created_identity,
            )
        _close_fd(run_fd)
        raise


def transition_stage22_pre_admission_context(
    lease: ReleaseGraphLock,
    context: object,
) -> Stage22AttemptContext:
    """Consume pre-admission and perform the first Stage 22 namespace access."""

    record = _require_pre_context(lease, context)
    assert isinstance(context, Stage22PreAdmissionContext)
    try:
        current = _capture_upstream(lease)
        try:
            _require_same_upstream(record.capture, current)
        finally:
            current.close()
    except Exception as exc:
        _PRE_CONTEXTS.pop(context, None)
        record.finalizer.detach()
        record.capture.close()
        record.phase = "expired"
        raise StructuredStage22PublicationError(
            f"structured Stage 22 pre-admission replay failed: {exc}"
        ) from exc
    _PRE_CONTEXTS.pop(context, None)
    record.finalizer.detach()
    record.phase = "consumed"
    namespace: BoundOutputNamespace | None = None
    try:
        namespace = _acquire_structured_stage22_namespace(record.owner)
        require_namespace_owned_by_epoch(namespace, lease, "stage-22")
        token = object()
        attempt = Stage22AttemptContext(
            authority=_CONSTRUCTION_AUTHORITY,
            token=token,
            writer_owner=record.owner,
            run_path=str(record.owner.run_dir.absolute()),
            run_identity=record.owner._run_identity,
            stage_identity=namespace._stage_identity,
            upstream_identity=record.capture.identity_tuple,
        )
        _ATTEMPT_CONTEXTS[attempt] = _AttemptRecord(
            token, record.owner, record.capture, namespace
        )
        return attempt
    except Exception as exc:
        if namespace is not None:
            namespace.close()
        record.capture.close()
        raise StructuredStage22PublicationError(
            f"structured Stage 22 namespace transition failed: {exc}"
        ) from exc


def produce_structured_stage22(
    lease: ReleaseGraphLock,
    context: object,
) -> ProvisionalStage22Result:
    """Publish exact payloads and manifest, returning only a provisional."""

    record = _require_attempt_context(
        lease, context, allowed_phases=("namespace_bound",)
    )
    assert isinstance(context, Stage22AttemptContext)
    deterministic = build_stage22_outputs_from_inputs(
        record.initial.semantic_inputs,
        generated=_generated(record.initial),
    )
    record.deterministic = deterministic
    expected_code = _expected_code_files(deterministic)
    template_names = tuple(name for name, _digest in deterministic.template_files)
    _validate_template_names(template_names)
    errors = _verify_empty_namespace(record.namespace)
    if errors:
        raise StructuredStage22PublicationError(
            "manifest-first invalidation failed: " + "; ".join(errors)
        )
    _advance(record, "namespace_bound", "invalidated")
    _verify_upstream_fixpoint(lease, record.initial)
    _advance(record, "invalidated", "snapshot_a")
    _advance(record, "snapshot_a", "built")
    compile_payloads, compile_status = _compile_deterministic(
        deterministic,
        record,
    )
    _advance(record, "built", "compiler_complete")
    payloads = dict(deterministic.direct_files)
    payloads["compile_status.json"] = _canonical_json(compile_status)
    payloads.update(compile_payloads)
    record.payloads = {
        **payloads,
        **{f"code/{name}": content for name, content in expected_code.items()},
    }
    held_dirs, held_files = _publish_payloads(
        record.namespace,
        record=record,
        payloads=payloads,
        code_files=expected_code,
        template_names=template_names,
    )
    record.held_directories = held_dirs
    record.held_files = held_files
    _advance(record, "compiler_complete", "payloads_published")
    _verify_payload_state(record)
    _verify_upstream_fixpoint(lease, record.initial)
    _advance(record, "payloads_published", "source_fixpoint")
    manifest_payload = _build_manifest(record, compile_status)
    manifest_bytes = _canonical_json(manifest_payload)
    replay_structured_stage22_manifest_v2(
        manifest_bytes, expected=manifest_payload
    )
    record.manifest = _create_held_file(
        record.namespace._stage_fd,
        STRUCTURED_STAGE22_MANIFEST,
        manifest_bytes,
        logical_path=f"stage-22/{STRUCTURED_STAGE22_MANIFEST}",
    )
    record.manifest_payload = manifest_payload
    _advance(record, "source_fixpoint", "manifest_published")
    first = _capture_final_state(lease, record)
    second = _capture_final_state(lease, record)
    if first != second:
        raise StructuredStage22PublicationError(
            "structured Stage 22 final snapshots differ"
        )
    record.final_snapshot = first
    _advance(record, "manifest_published", "provisional_done")
    return ProvisionalStage22Result(
        StageStatus.DONE,
        STRUCTURED_STAGE22_ARTIFACTS,
        STRUCTURED_STAGE22_EVIDENCE_REFS,
        context,
    )


def validate_structured_stage22_immediate_postcondition(
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


def validate_structured_stage22_terminal_postcondition(
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


def clear_structured_stage22_context(
    lease: ReleaseGraphLock,
    context: object,
) -> None:
    record = _require_attempt_context(
        lease, context, allowed_phases=("terminal_validated",)
    )
    assert isinstance(context, Stage22AttemptContext)
    _advance(record, "terminal_validated", "cleared")
    _ATTEMPT_CONTEXTS.pop(context, None)
    _close_attempt_record(record)


def fail_structured_stage22_attempt(
    lease: ReleaseGraphLock,
    context: object,
) -> tuple[str, ...]:
    """Withdraw the current attempt through held manifest-first cleanup."""

    if not isinstance(context, Stage22AttemptContext):
        return ()
    record = _ATTEMPT_CONTEXTS.get(context)
    if record is None:
        return ()
    writer = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if writer is not record.owner:
        raise StructuredStage22PublicationError(
            "structured Stage 22 cleanup writer mismatch"
        )
    record.phase = "failed_cleanup"
    errors = _cleanup_attempt_outputs(record)
    record.phase = "cleared"
    _ATTEMPT_CONTEXTS.pop(context, None)
    _close_attempt_record(record)
    return errors


def context_phase(context: object) -> str:
    if isinstance(context, Stage22PreAdmissionContext):
        record = _PRE_CONTEXTS.get(context)
    elif isinstance(context, Stage22AttemptContext):
        record = _ATTEMPT_CONTEXTS.get(context)
    else:
        record = None
    if record is None:
        raise StructuredStage22PublicationError(
            "structured Stage 22 context is not live"
        )
    return record.phase


def execute_structured_stage22_export(
    run_dir: Path,
    stage_dir: Path,
) -> StageResult:
    """Public/direct entry; capability guard is mechanically first."""

    capability.require_complete_structured_capability(
        "execute_structured_stage22_export"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage22_export", mode="write"
    ) as lease:
        if stage_dir != run_dir / "stage-22":
            raise StructuredStage22PublicationError(
                "structured Stage 22 directory is not canonical"
            )
        from researchclaw.pipeline.executor import (
            _execute_structured_stage22_private,
        )

        pre = issue_stage22_pre_admission_context(lease)
        return _execute_structured_stage22_private(lease, pre)


def replay_structured_stage22_manifest_v2(
    content: bytes,
    *,
    expected: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Strictly parse the exact 23-root Stage 22 authority manifest."""

    value = _strict_object(content, "Stage 22 manifest")
    if set(value) != set(STRUCTURED_STAGE22_MANIFEST_FIELDS) or len(value) != 23:
        raise StructuredStage22PublicationError(
            "structured Stage 22 manifest root fields mismatch"
        )
    _true_int(value["schema_version"], 2, "manifest schema_version")
    if value["publication_stage_id"] != "stage22":
        raise StructuredStage22PublicationError("Stage 22 publication_stage_id mismatch")
    if value["publication_mode"] != "structured-scientific-claim-v1":
        raise StructuredStage22PublicationError("Stage 22 publication_mode mismatch")
    _true_int(
        value["structured_capability_schema_version"],
        1,
        "structured capability schema version",
    )
    if value["structured_capability_snapshot"] != _capability_dict():
        raise StructuredStage22PublicationError("Stage 22 capability snapshot mismatch")
    _sha(value["generation_binding_sha256"], "generation binding")
    for name in (
        "canonical_experiment_evidence",
        "selected_result_manifest",
        "source_stage19_manifest",
        "source_stage20_manifest",
        "source_stage21_manifest",
        "source_stage21_archive",
        "source_paper",
        "bibliography_source",
    ):
        _file_ref(value[name], name)
    exact_source_paths = {
        "source_stage19_manifest": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "source_stage20_manifest": "stage-20/quality_gate_manifest.json",
        "source_stage21_manifest": "stage-21/bundle_index.json",
        "source_stage21_archive": "stage-21/archive.md",
        "source_paper": "stage-19/scientific_claim_paper_revised.md",
    }
    for name, path in exact_source_paths.items():
        if value[name]["path"] != path:
            raise StructuredStage22PublicationError(
                f"Stage 22 {name} path mismatch"
            )
    cfs = value["cfs"]
    if not isinstance(cfs, dict) or set(cfs) != {"schema_version", "sha256"}:
        raise StructuredStage22PublicationError("Stage 22 CFS fields mismatch")
    _true_int(cfs["schema_version"], 1, "CFS schema_version")
    _sha(cfs["sha256"], "CFS sha256")
    if value["quality_outcome"] not in {"passed", "degraded"}:
        raise StructuredStage22PublicationError("Stage 22 quality outcome mismatch")
    if value["quality_outcome"] == "passed":
        if value["degradation_signal"] is not None:
            raise StructuredStage22PublicationError(
                "passed Stage 22 has degradation signal"
            )
    else:
        _file_ref(value["degradation_signal"], "degradation signal")
    template = value["template"]
    if not isinstance(template, dict) or set(template) != {"name", "files"}:
        raise StructuredStage22PublicationError("Stage 22 template fields mismatch")
    if not isinstance(template["name"], str) or not template["name"]:
        raise StructuredStage22PublicationError("Stage 22 template name mismatch")
    template_files = _output_entries(
        template["files"], allowed_roles={"template_file"}
    )
    _validate_template_names(
        tuple(str(entry["logical_name"]) for entry in template_files)
    )
    project_files = _output_entries(
        value["project_files"], allowed_roles={"project_file"}, nonempty=True
    )
    _validate_logical_paths(
        (
            *(str(entry["logical_name"]) for entry in project_files),
            "README.md",
            "requirements.txt",
        )
    )
    compile_value = _compile_manifest(value["compile"])
    outputs = _output_entries(value["outputs"], allowed_roles=None, nonempty=True)
    _validate_output_order(outputs, compile_value)
    if template_files != [
        entry for entry in outputs if entry["role"] == "template_file"
    ]:
        raise StructuredStage22PublicationError("Stage 22 template output mismatch")
    for entry in template_files:
        if entry["path"] != f"stage-22/{entry['logical_name']}":
            raise StructuredStage22PublicationError(
                "Stage 22 template logical path mismatch"
            )
    if project_files != [
        entry for entry in outputs if entry["role"] == "project_file"
    ]:
        raise StructuredStage22PublicationError("Stage 22 project output mismatch")
    for entry in project_files:
        if entry["path"] != f"stage-22/code/{entry['logical_name']}":
            raise StructuredStage22PublicationError(
                "Stage 22 project logical path mismatch"
            )
    _true_positive_int(value["output_count"], "output_count")
    if value["output_count"] != len(outputs):
        raise StructuredStage22PublicationError("Stage 22 output_count mismatch")
    pdf_present = compile_value["paper_pdf"] is not None
    if value["output_count"] != (
        10 + len(template_files) + len(project_files) + int(pdf_present)
    ):
        raise StructuredStage22PublicationError("Stage 22 output_count formula mismatch")
    if not isinstance(value["generated"], str) or not value["generated"]:
        raise StructuredStage22PublicationError("Stage 22 generated mismatch")
    if expected is not None and value != dict(expected):
        raise StructuredStage22PublicationError(
            "Stage 22 manifest differs from independent rebuild"
        )
    if _canonical_json(value) != content:
        raise StructuredStage22PublicationError(
            "Stage 22 manifest is not canonical JSON"
        )
    return value


def _capture_upstream(lease: ReleaseGraphLock) -> _CapturedUpstream:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    base = stage21._capture_upstream(writer)
    namespace: BoundOutputNamespace | None = None
    try:
        namespace = writer.open_stage_namespace("stage-21")
        require_namespace_owned_by_epoch(namespace, writer, "stage-21")
        if set(namespace.direct_entries()) != _STAGE21_NAMES:
            raise StructuredStage22PublicationError(
                "Stage 21 exact two-file namespace mismatch"
            )
        files = tuple(
            stage19._read_rich_run_snapshot(writer, f"stage-21/{name}")
            for name in stage21.STRUCTURED_STAGE21_OUTPUTS
        )
        file_map = {item.path: item for item in files}
        archive = file_map["stage-21/archive.md"]
        index = file_map["stage-21/bundle_index.json"]
        expected_archive = render_archive(base.snapshot.authority_inputs)
        expected_index = build_bundle_index_v2(
            base.snapshot.authority_inputs,
            archive_bytes=expected_archive,
        )
        if archive.content != expected_archive:
            raise StructuredStage22PublicationError(
                "Stage 21 archive differs from reconstruction"
            )
        replay_bundle_index_v2(
            index.content,
            archive_bytes=archive.content,
            expected=expected_index,
        )
        if stage19._read_rich_run_snapshot(
            writer, "stage-21/bundle_index.json"
        ) != index:
            raise StructuredStage22PublicationError(
                "current Stage 21 manifest name changed"
            )
        binding = base.snapshot.stage19_snapshot.binding
        config = stage21.stage20.parse_config_snapshot_text(
            binding.evidence.run_config_bytes.decode("utf-8"),
            project_root=owner.run_dir,
            label="structured Stage 22 canonical config",
        )
        paper = _source_from_base(
            base, "stage-19/scientific_claim_paper_revised.md"
        )
        allowlist = _source_from_base(
            base, "stage-06/citation_allowlist.json"
        )
        allowlist_payload = _strict_object(
            allowlist.content, "Stage 17 citation allowlist"
        )
        bibliography_path = allowlist_payload.get("references_path")
        bibliography_sha256 = allowlist_payload.get("references_sha256")
        if not isinstance(bibliography_path, str):
            raise StructuredStage22PublicationError(
                "structured Stage 22 bibliography path is invalid"
            )
        _safe_relative(bibliography_path)
        _sha(bibliography_sha256, "bibliography source sha256")
        bibliography = stage19._read_rich_run_snapshot(
            writer, bibliography_path
        )
        if bibliography.sha256 != bibliography_sha256:
            raise StructuredStage22PublicationError(
                "structured Stage 22 bibliography hash mismatch"
            )
        evidence_file = stage19._read_rich_run_snapshot(
            writer, binding.canonical_experiment_evidence_path
        )
        if evidence_file.sha256 != binding.canonical_experiment_evidence_sha256:
            raise StructuredStage22PublicationError(
                "structured Stage 22 canonical evidence hash mismatch"
            )
        selected_file = stage19._read_rich_run_snapshot(
            writer, binding.evidence.selected_result_manifest_path
        )
        if (
            selected_file.sha256
            != binding.evidence.selected_result_manifest_sha256
        ):
            raise StructuredStage22PublicationError(
                "structured Stage 22 selected-result hash mismatch"
            )
        semantic_inputs = Stage22SemanticInputs(
            evidence=binding.evidence,
            canonical_config=config,
            paper_path=paper.path,
            paper_sha256=paper.sha256,
            paper_content=paper.content,
            bibliography_content=bibliography.content,
            quality_outcome=base.snapshot.authority_inputs.quality_outcome,
        )
        return _CapturedUpstream(
            base,
            namespace,
            files,
            semantic_inputs,
            bibliography,
            (evidence_file, selected_file, bibliography),
        )
    except Exception:
        if namespace is not None:
            namespace.close()
        base.close()
        raise


def _compile_deterministic(
    deterministic: Stage22DeterministicOutputs,
    record: _AttemptRecord,
) -> tuple[dict[str, bytes], dict[str, object]]:
    if record.semantic_calls != 0:
        raise StructuredStage22PublicationError(
            "structured Stage 22 compiler semantic call already consumed"
        )
    direct = deterministic.direct_files
    original = {
        "paper.tex": direct["paper.tex"],
        "references.bib": direct["references.bib"],
        **{
            name: direct[name]
            for name, _digest in deterministic.template_files
        },
    }
    with tempfile.TemporaryDirectory(
        prefix="researchclaw-structured-stage22-"
    ) as temporary:
        root = Path(temporary)
        for name, content in original.items():
            (root / name).write_bytes(content)
        record.semantic_calls += 1
        result = compile_latex(root / "paper.tex", max_attempts=2)
        if record.semantic_calls != 1:
            raise StructuredStage22PublicationError(
                "structured Stage 22 compiler semantic call count mismatch"
            )
        for name, content in original.items():
            if (root / name).read_bytes() != content:
                raise StructuredStage22PublicationError(
                    f"compiler changed deterministic input: {name}"
                )
        attempts = getattr(result, "attempts", None)
        if type(attempts) is not int or not 0 <= attempts <= 2:
            raise StructuredStage22PublicationError(
                "structured Stage 22 compiler attempts mismatch"
            )
        success = getattr(result, "success", None)
        if type(success) is not bool:
            raise StructuredStage22PublicationError(
                "structured Stage 22 compiler success flag mismatch"
            )
        raw_errors = tuple(getattr(result, "errors", ()) or ())
        pdf_bytes: bytes | None = None
        if success:
            if attempts not in {1, 2} or raw_errors:
                raise StructuredStage22PublicationError(
                    "successful Stage 22 compiler result is inconsistent"
                )
            pdf_path = root / "paper.pdf"
            if not pdf_path.is_file() or pdf_path.is_symlink():
                raise StructuredStage22PublicationError(
                    "compiler reported success without paper.pdf"
                )
            pdf_bytes = pdf_path.read_bytes()
            _validate_pdf(pdf_bytes)
            status = "success"
            tooling_available = True
            errors: list[str] = []
        else:
            missing = any(
                "not installed" in str(item).lower()
                or "not found" in str(item).lower()
                for item in raw_errors
            )
            if missing:
                if attempts != 0:
                    raise StructuredStage22PublicationError(
                        "toolchain-missing compiler attempted compilation"
                    )
                status = "toolchain_missing"
                tooling_available = False
                errors = ["toolchain_missing"]
            else:
                if attempts not in {1, 2}:
                    raise StructuredStage22PublicationError(
                        "latex-error compiler attempts mismatch"
                    )
                status = "latex_error"
                tooling_available = True
                errors = ["compiler_attempt_1_failed"]
                if attempts == 2:
                    errors.append("compiler_attempt_2_failed")
        pdf_ref = (
            None
            if pdf_bytes is None
            else _bytes_ref("stage-22/paper.pdf", pdf_bytes)
        )
        status_payload: dict[str, object] = {
            "schema_version": 2,
            "semantic_calls": 1,
            "max_attempts": 2,
            "attempts": attempts,
            "status": status,
            "tooling_available": tooling_available,
            "errors": errors,
            "paper_pdf": pdf_ref,
        }
        return (
            {} if pdf_bytes is None else {"paper.pdf": pdf_bytes},
            status_payload,
        )


def _publish_payloads(
    namespace: BoundOutputNamespace,
    *,
    record: _AttemptRecord,
    payloads: Mapping[str, bytes],
    code_files: Mapping[str, bytes],
    template_names: tuple[str, ...],
) -> tuple[dict[str, _HeldDirectory], dict[str, _HeldFile]]:
    held_files: dict[str, _HeldFile] = {}
    held_dirs: dict[str, _HeldDirectory] = {}
    record.held_files = held_files
    record.held_directories = held_dirs
    direct_order = (
        *STRUCTURED_STAGE22_FIXED_FILES,
        *(("paper.pdf",) if "paper.pdf" in payloads else ()),
        *template_names,
    )
    try:
        if namespace.direct_entries() or os.listdir(namespace._stage_fd):
            raise StructuredStage22PublicationError(
                "structured Stage 22 namespace changed before first payload"
            )
        for name in direct_order:
            namespace.assert_canonical()
            held_files[f"stage-22/{name}"] = _create_held_file(
                namespace._stage_fd,
                name,
                payloads[name],
                logical_path=f"stage-22/{name}",
            )
        code_root = _create_held_directory(
            namespace._stage_fd, "code", logical_path="stage-22/code"
        )
        held_dirs["code"] = code_root
        started_parents: set[str] = set()
        directory_paths = sorted(
            {
                parent.as_posix()
                for name in code_files
                for parent in PurePosixPath(name).parents
                if parent.as_posix() not in {".", ""}
            },
            key=lambda item: (item.count("/"), item),
        )
        for relative in directory_paths:
            parent_path, _, leaf = relative.rpartition("/")
            parent_key = parent_path or "code"
            parent = held_dirs[parent_key]
            _require_directory_identity(parent)
            if parent_key not in started_parents:
                _require_directory_empty_before_first_child(parent)
                started_parents.add(parent_key)
            logical = f"stage-22/code/{relative}"
            held_dirs[relative] = _create_held_directory(
                parent.descriptor, leaf or relative, logical_path=logical
            )
        for relative, content in code_files.items():
            parent_path, _, leaf = relative.rpartition("/")
            parent_key = parent_path or "code"
            parent = held_dirs[parent_key]
            _require_directory_identity(parent)
            if parent_key not in started_parents:
                _require_directory_empty_before_first_child(parent)
                started_parents.add(parent_key)
            logical = f"stage-22/code/{relative}"
            held_files[logical] = _create_held_file(
                parent.descriptor, leaf or relative, content, logical_path=logical
            )
        return held_dirs, held_files
    except Exception:
        # The executor owns manifest-first withdrawal. Keep creating fds live
        # and the partial maps attached so cleanup can address only identities
        # created by this attempt.
        raise


def _build_manifest(
    record: _AttemptRecord,
    compile_status: Mapping[str, object],
) -> dict[str, object]:
    if record.payloads is None or record.deterministic is None:
        raise StructuredStage22PublicationError("Stage 22 payload state missing")
    capture = record.initial
    template_names = tuple(
        name for name, _digest in record.deterministic.template_files
    )
    project_names = tuple(
        sorted(
            artifact.logical_name
            for artifact in capture.semantic_inputs.evidence.project_artifacts
        )
    )
    outputs: list[dict[str, object]] = []
    for name, role in _FIXED_ROLE_BY_NAME.items():
        outputs.append(_output_entry(role, None, f"stage-22/{name}", record.payloads[name]))
    if "paper.pdf" in record.payloads:
        outputs.append(
            _output_entry(
                "paper_pdf", None, "stage-22/paper.pdf", record.payloads["paper.pdf"]
            )
        )
    for name in template_names:
        outputs.append(
            _output_entry(
                "template_file", name, f"stage-22/{name}", record.payloads[name]
            )
        )
    for name in project_names:
        path = f"code/{name}"
        outputs.append(
            _output_entry(
                "project_file",
                name,
                f"stage-22/{path}",
                record.payloads[path],
            )
        )
    for name in ("README.md", "requirements.txt"):
        path = f"code/{name}"
        outputs.append(
            _output_entry(
                "code_support_file",
                name,
                f"stage-22/{path}",
                record.payloads[path],
            )
        )
    inputs = capture.base.snapshot.authority_inputs
    degradation = (
        None
        if inputs.quality_outcome == "passed"
        else _rich_ref(_source(capture, "degradation_signal.json"))
    )
    status_bytes = record.payloads["compile_status.json"]
    pdf_ref = (
        None
        if "paper.pdf" not in record.payloads
        else _bytes_ref("stage-22/paper.pdf", record.payloads["paper.pdf"])
    )
    compile_manifest = {
        "outcome": (
            "compiler-success"
            if compile_status["status"] == "success"
            else "compiler-failure"
        ),
        "semantic_calls": 1,
        "max_attempts": 2,
        "attempts": compile_status["attempts"],
        "status": compile_status["status"],
        "tooling_available": compile_status["tooling_available"],
        "errors": compile_status["errors"],
        "status_file": _bytes_ref(
            "stage-22/compile_status.json", status_bytes
        ),
        "paper_pdf": pdf_ref,
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "publication_stage_id": "stage22",
        "publication_mode": "structured-scientific-claim-v1",
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": _capability_dict(),
        "generation_binding_sha256": inputs.generation_binding_sha256,
        "canonical_experiment_evidence": _rich_ref(
            _source(
                capture,
                capture.base.snapshot.stage19_snapshot.binding.canonical_experiment_evidence_path,
            )
        ),
        "cfs": {"schema_version": 1, "sha256": inputs.cfs_sha256},
        "selected_result_manifest": _rich_ref(
            _source(
                capture,
                capture.semantic_inputs.evidence.selected_result_manifest_path,
            )
        ),
        "source_stage19_manifest": _rich_ref(
            _source(
                capture,
                "stage-19/scientific_claim_authority_manifest.json",
            )
        ),
        "source_stage20_manifest": _rich_ref(
            _source(capture, "stage-20/quality_gate_manifest.json")
        ),
        "source_stage21_manifest": _rich_ref(
            _source(capture, "stage-21/bundle_index.json")
        ),
        "source_stage21_archive": _rich_ref(
            _source(capture, "stage-21/archive.md")
        ),
        "source_paper": _rich_ref(
            _source(
                capture, "stage-19/scientific_claim_paper_revised.md"
            )
        ),
        "quality_outcome": inputs.quality_outcome,
        "degradation_signal": degradation,
        "bibliography_source": _rich_ref(capture.bibliography),
        "template": {
            "name": record.deterministic.template_name,
            "files": [
                entry for entry in outputs if entry["role"] == "template_file"
            ],
        },
        "project_files": [
            entry for entry in outputs if entry["role"] == "project_file"
        ],
        "compile": compile_manifest,
        "output_count": len(outputs),
        "outputs": outputs,
        "generated": inputs.generated,
    }
    return payload


def _validate_provisional(
    lease: ReleaseGraphLock,
    provisional: object,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: object,
    phase: str,
) -> _AttemptRecord:
    record = _require_attempt_context(
        lease, context, allowed_phases=(phase,)
    )
    if (
        not isinstance(provisional, ProvisionalStage22Result)
        or provisional.status is not StageStatus.DONE
        or provisional.context is not context
        or provisional.artifacts != STRUCTURED_STAGE22_ARTIFACTS
        or provisional.evidence_refs != STRUCTURED_STAGE22_EVIDENCE_REFS
        or artifacts != STRUCTURED_STAGE22_ARTIFACTS
        or evidence_refs != STRUCTURED_STAGE22_EVIDENCE_REFS
    ):
        raise StructuredStage22PublicationError(
            "structured Stage 22 provisional result mismatch"
        )
    return record


def _verify_postcondition_state(
    lease: ReleaseGraphLock, record: _AttemptRecord
) -> None:
    current = _capture_final_state(lease, record)
    if record.final_snapshot is None or current != record.final_snapshot:
        raise StructuredStage22PublicationError(
            "structured Stage 22 postcondition snapshot mismatch"
        )


def _capture_final_state(
    lease: ReleaseGraphLock,
    record: _AttemptRecord,
) -> tuple[object, ...]:
    _verify_upstream_fixpoint(lease, record.initial)
    _verify_payload_state(record)
    if record.manifest is None or record.manifest_payload is None:
        raise StructuredStage22PublicationError("Stage 22 manifest state missing")
    manifest_bytes = _read_held_file(record.manifest)
    replay_structured_stage22_manifest_v2(
        manifest_bytes, expected=record.manifest_payload
    )
    _require_name_identity(record.manifest)
    expected_direct = {
        "code",
        STRUCTURED_STAGE22_MANIFEST,
        *(
            path.removeprefix("stage-22/")
            for path in (record.held_files or {})
            if not path.startswith("stage-22/code/")
        ),
    }
    if set(record.namespace.direct_entries()) != expected_direct:
        raise StructuredStage22PublicationError(
            "structured Stage 22 direct namespace mismatch"
        )
    return (
        record.initial.identity_tuple,
        tuple(
            (
                path,
                held.identity,
                _read_held_file(held),
            )
            for path, held in sorted((record.held_files or {}).items())
        ),
        (
            record.manifest.identity,
            manifest_bytes,
        ),
        tuple(
            (path, held.identity)
            for path, held in sorted((record.held_directories or {}).items())
        ),
        record.semantic_calls,
    )


def _verify_payload_state(record: _AttemptRecord) -> None:
    if (
        record.payloads is None
        or record.held_files is None
        or record.held_directories is None
        or record.deterministic is None
    ):
        raise StructuredStage22PublicationError("Stage 22 payload capture missing")
    expected_paths = {f"stage-22/{path}" for path in record.payloads}
    if set(record.held_files) != expected_paths:
        raise StructuredStage22PublicationError("Stage 22 held file set mismatch")
    for path, held in record.held_files.items():
        _require_name_identity(held)
        logical = path.removeprefix("stage-22/")
        if _read_held_file(held) != record.payloads[logical]:
            raise StructuredStage22PublicationError(
                f"Stage 22 payload changed: {path}"
            )
    _validate_code_tree(record)
    rebuilt = build_stage22_outputs_from_inputs(
        record.initial.semantic_inputs,
        generated=_generated(record.initial),
    )
    if (
        rebuilt.direct_files != record.deterministic.direct_files
        or rebuilt.code_files != record.deterministic.code_files
        or rebuilt.template_name != record.deterministic.template_name
        or rebuilt.template_files != record.deterministic.template_files
    ):
        raise StructuredStage22PublicationError(
            "Stage 22 deterministic rebuild changed"
        )
    _replay_compile_status(record)


def _replay_compile_status(record: _AttemptRecord) -> dict[str, object]:
    assert record.payloads is not None
    value = _strict_object(
        record.payloads["compile_status.json"], "Stage 22 compile status"
    )
    fields = (
        "schema_version",
        "semantic_calls",
        "max_attempts",
        "attempts",
        "status",
        "tooling_available",
        "errors",
        "paper_pdf",
    )
    if set(value) != set(fields):
        raise StructuredStage22PublicationError("compile status fields mismatch")
    _true_int(value["schema_version"], 2, "compile schema")
    _true_int(value["semantic_calls"], 1, "compile semantic calls")
    _true_int(value["max_attempts"], 2, "compile max attempts")
    if type(value["attempts"]) is not int or not 0 <= value["attempts"] <= 2:
        raise StructuredStage22PublicationError("compile attempts mismatch")
    if type(value["tooling_available"]) is not bool:
        raise StructuredStage22PublicationError("compile tooling flag mismatch")
    if not isinstance(value["errors"], list):
        raise StructuredStage22PublicationError("compile errors mismatch")
    if value["status"] == "success":
        if (
            value["tooling_available"] is not True
            or value["attempts"] not in {1, 2}
            or value["errors"] != []
            or "paper.pdf" not in record.payloads
        ):
            raise StructuredStage22PublicationError("compile success mismatch")
        _file_ref(value["paper_pdf"], "compile PDF")
        if value["paper_pdf"] != _bytes_ref(
            "stage-22/paper.pdf", record.payloads["paper.pdf"]
        ):
            raise StructuredStage22PublicationError("compile PDF ref mismatch")
        _validate_pdf(record.payloads["paper.pdf"])
    elif value["status"] == "toolchain_missing":
        if (
            value["tooling_available"] is not False
            or value["attempts"] != 0
            or value["errors"] != ["toolchain_missing"]
            or value["paper_pdf"] is not None
        ):
            raise StructuredStage22PublicationError(
                "compile toolchain-missing mismatch"
            )
    elif value["status"] == "latex_error":
        expected_errors = ["compiler_attempt_1_failed"]
        if value["attempts"] == 2:
            expected_errors.append("compiler_attempt_2_failed")
        if (
            value["tooling_available"] is not True
            or value["attempts"] not in {1, 2}
            or value["errors"] != expected_errors
            or value["paper_pdf"] is not None
        ):
            raise StructuredStage22PublicationError("compile latex-error mismatch")
    else:
        raise StructuredStage22PublicationError("compile status mismatch")
    if record.semantic_calls != 1:
        raise StructuredStage22PublicationError("compiler semantic call count drift")
    return value


def _validate_code_tree(record: _AttemptRecord) -> None:
    assert record.held_directories is not None
    assert record.held_files is not None
    code = record.held_directories.get("code")
    if code is None:
        raise StructuredStage22PublicationError("Stage 22 code root missing")
    expected_children: dict[str, set[str]] = {
        path: set() for path in record.held_directories
    }
    for path in record.held_directories:
        if path == "code":
            continue
        parent, _, leaf = path.rpartition("/")
        expected_children[parent or "code"].add(leaf or path)
    for logical_path in record.held_files:
        if not logical_path.startswith("stage-22/code/"):
            continue
        relative = logical_path.removeprefix("stage-22/code/")
        parent, _, leaf = relative.rpartition("/")
        expected_children[parent or "code"].add(leaf or relative)
    for path, held in record.held_directories.items():
        _require_directory_identity(held)
        entries = set(os.listdir(held.descriptor))
        if entries != expected_children[path] or not entries:
            raise StructuredStage22PublicationError(
                f"Stage 22 code directory mismatch: {path}"
            )


def _verify_empty_namespace(
    namespace: BoundOutputNamespace,
) -> tuple[str, ...]:
    """Verify the newly acquired stage remains empty; never adopt or clean."""

    errors: list[str] = []
    try:
        namespace.assert_canonical()
    except OSError as exc:
        errors.append(f"namespace identity: {exc}")
    try:
        entries = namespace.direct_entries()
        if entries or os.listdir(namespace._stage_fd):
            errors.append(
                f"post-acquisition Stage 22 collision: {sorted(entries)}"
            )
    except OSError as exc:
        errors.append(f"namespace enumeration: {exc}")
    return tuple(errors)


def _cleanup_attempt_outputs(record: _AttemptRecord) -> tuple[str, ...]:
    errors: list[str] = []
    if record.manifest is not None:
        _unlink_held(record.manifest, errors)
    direct = tuple(
        held
        for path, held in (record.held_files or {}).items()
        if not path.startswith("stage-22/code/")
    )
    for held in direct:
        _unlink_held(held, errors)
    for held in tuple((record.held_files or {}).values()):
        if held.logical_path.startswith("stage-22/code/"):
            _unlink_held(held, errors)
    for _path, held in sorted(
        (record.held_directories or {}).items(),
        key=lambda item: (item[0].count("/"), item[0]),
        reverse=True,
    ):
        try:
            info = os.stat(
                held.name, dir_fd=held.parent_fd, follow_symlinks=False
            )
            if (info.st_dev, info.st_ino) != held.identity:
                errors.append(f"{held.logical_path}: directory identity collision")
                continue
            os.rmdir(held.name, dir_fd=held.parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{held.logical_path}: {exc}")
    try:
        named = os.stat(
            record.namespace.stage_name,
            dir_fd=record.namespace._run_fd,
            follow_symlinks=False,
        )
        if (named.st_dev, named.st_ino) != record.namespace._stage_identity:
            errors.append("stage-22: canonical name identity collision")
        elif os.listdir(record.namespace._stage_fd):
            errors.append("stage-22: owned namespace remains nonempty")
        else:
            os.rmdir(
                record.namespace.stage_name,
                dir_fd=record.namespace._run_fd,
            )
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"stage-22: {exc}")
    return tuple(errors)


def _unlink_held(held: _HeldFile, errors: list[str]) -> None:
    try:
        info = os.stat(held.name, dir_fd=held.parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != held.identity
        ):
            errors.append(f"{held.logical_path}: file identity collision")
            return
        os.unlink(held.name, dir_fd=held.parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"{held.logical_path}: {exc}")


def _validate_new_directory(
    *,
    parent_fd: int,
    parent_identity: tuple[int, int],
    name: str,
    descriptor: int,
    expected_mode: int,
    logical_path: str,
) -> os.stat_result:
    parent_before = os.fstat(parent_fd)
    info_before = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    entries = os.listdir(descriptor)
    info_after = os.fstat(descriptor)
    parent_after = os.fstat(parent_fd)
    identity = (info_before.st_dev, info_before.st_ino)
    if (
        not stat.S_ISDIR(parent_before.st_mode)
        or (parent_before.st_dev, parent_before.st_ino) != parent_identity
        or (parent_after.st_dev, parent_after.st_ino) != parent_identity
        or not stat.S_ISDIR(info_before.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or identity != (named.st_dev, named.st_ino)
        or identity != (info_after.st_dev, info_after.st_ino)
        or info_before.st_dev != parent_before.st_dev
        or info_before.st_uid != os.geteuid()
        or stat.S_IMODE(info_before.st_mode) != expected_mode
        or info_before.st_nlink < 2
        or entries
    ):
        raise StructuredStage22PublicationError(
            f"created directory contract mismatch: {logical_path}"
        )
    return info_before


def _remove_new_empty_directory(
    parent_fd: int,
    name: str,
    *,
    created_identity: tuple[int, int],
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISDIR(named.st_mode)
            and (named.st_dev, named.st_ino) == created_identity
        ):
            os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


def _create_held_directory(
    parent_fd: int, name: str, *, logical_path: str
) -> _HeldDirectory:
    _safe_component(name)
    parent = os.fstat(parent_fd)
    parent_identity = (parent.st_dev, parent.st_ino)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise StructuredStage22PublicationError(
            f"created directory name already exists: {logical_path}"
        )
    os.mkdir(name, mode=0o755, dir_fd=parent_fd)
    created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    created_identity = (created.st_dev, created.st_ino)
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        info = _validate_new_directory(
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            name=name,
            descriptor=descriptor,
            expected_mode=0o755,
            logical_path=logical_path,
        )
        return _HeldDirectory(
            parent_fd,
            name,
            descriptor,
            (info.st_dev, info.st_ino),
            logical_path,
        )
    except Exception:
        if descriptor >= 0:
            _close_fd(descriptor)
        _remove_new_empty_directory(
            parent_fd,
            name,
            created_identity=created_identity,
        )
        raise


def _create_held_file(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    logical_path: str,
) -> _HeldFile:
    _safe_component(name)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, 0o644, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(content)
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise StructuredStage22PublicationError(
                f"created file identity mismatch: {logical_path}"
            )
        held = _HeldFile(
            parent_fd,
            name,
            descriptor,
            (info.st_dev, info.st_ino),
            logical_path,
            content,
        )
        if _read_held_file(held) != content:
            raise StructuredStage22PublicationError(
                f"created file readback mismatch: {logical_path}"
            )
        return held
    except Exception:
        try:
            created = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (created.st_dev, created.st_ino) == (
                named.st_dev,
                named.st_ino,
            ):
                os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        _close_fd(descriptor)
        raise


def _read_held_file(held: _HeldFile) -> bytes:
    before = os.fstat(held.descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_dev, before.st_ino) != held.identity
    ):
        raise StructuredStage22PublicationError(
            f"held file identity changed: {held.logical_path}"
        )
    os.lseek(held.descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(held.descriptor, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    content = b"".join(chunks)
    after = os.fstat(held.descriptor)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or len(content) != after.st_size
    ):
        raise StructuredStage22PublicationError(
            f"held file changed during read: {held.logical_path}"
        )
    return content


def _require_name_identity(held: _HeldFile) -> None:
    info = os.stat(held.name, dir_fd=held.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino) != held.identity
    ):
        raise StructuredStage22PublicationError(
            f"held file name identity changed: {held.logical_path}"
        )


def _require_directory_identity(held: _HeldDirectory) -> None:
    info = os.fstat(held.descriptor)
    named = os.stat(held.name, dir_fd=held.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != held.identity
        or (named.st_dev, named.st_ino) != held.identity
    ):
        raise StructuredStage22PublicationError(
            f"held directory identity changed: {held.logical_path}"
        )


def _require_directory_empty_before_first_child(
    held: _HeldDirectory,
) -> None:
    parent = os.fstat(held.parent_fd)
    _validate_new_directory(
        parent_fd=held.parent_fd,
        parent_identity=(parent.st_dev, parent.st_ino),
        name=held.name,
        descriptor=held.descriptor,
        expected_mode=0o755,
        logical_path=held.logical_path,
    )


def _require_pre_context(
    lease: ReleaseGraphLock, context: object
) -> _PreRecord:
    if not isinstance(context, Stage22PreAdmissionContext):
        raise StructuredStage22PublicationError(
            "structured Stage 22 pre-admission context type mismatch"
        )
    record = _PRE_CONTEXTS.get(context)
    if record is None or record.token is not context._token:
        raise StructuredStage22PublicationError(
            "structured Stage 22 pre-admission context is not live"
        )
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if (
        owner is not record.owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or context.logical_stage_id != "stage22"
        or context.structured_capability_schema_version != 1
        or context.structured_capability_snapshot != _capability_tuple()
        or context.generation_binding_sha256
        != record.capture.base.snapshot.authority_inputs.generation_binding_sha256
        or context.stage20_manifest_sha256
        != _source(
            record.capture, "stage-20/quality_gate_manifest.json"
        ).sha256
        or context.stage21_manifest_sha256
        != _source(record.capture, "stage-21/bundle_index.json").sha256
        or context.upstream_identity != record.capture.identity_tuple
    ):
        raise StructuredStage22PublicationError(
            "structured Stage 22 pre-admission context binding mismatch"
        )
    return record


def _require_attempt_context(
    lease: ReleaseGraphLock,
    context: object,
    *,
    allowed_phases: tuple[str, ...],
) -> _AttemptRecord:
    if not isinstance(context, Stage22AttemptContext):
        raise StructuredStage22PublicationError(
            "structured Stage 22 attempt context type mismatch"
        )
    record = _ATTEMPT_CONTEXTS.get(context)
    if record is None or record.token is not context._token:
        raise StructuredStage22PublicationError(
            "structured Stage 22 attempt context is not live"
        )
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if (
        owner is not record.owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or context._stage_identity != record.namespace._stage_identity
        or context.logical_stage_id != "stage22"
        or context.structured_capability_snapshot != _capability_tuple()
        or context.upstream_identity != record.initial.identity_tuple
        or record.phase not in allowed_phases
    ):
        raise StructuredStage22PublicationError(
            "structured Stage 22 attempt context binding mismatch"
        )
    require_namespace_owned_by_epoch(record.namespace, lease, "stage-22")
    return record


def _verify_upstream_fixpoint(
    lease: ReleaseGraphLock, expected: _CapturedUpstream
) -> None:
    current = _capture_upstream(lease)
    try:
        _require_same_upstream(expected, current)
    finally:
        current.close()


def _require_same_upstream(
    expected: _CapturedUpstream, current: _CapturedUpstream
) -> None:
    if expected.identity_tuple != current.identity_tuple:
        raise StructuredStage22PublicationError(
            "structured Stage 22 upstream closure changed"
        )


def _source(capture: _CapturedUpstream, path: str) -> stage19.RichFileSnapshot:
    matches = tuple(
        item
        for item in (
            *capture.base.snapshot.sources,
            *capture.stage21_files,
            *capture.extra_sources,
        )
        if item.path == path
    )
    if not matches or any(item != matches[0] for item in matches[1:]):
        raise StructuredStage22PublicationError(
            f"structured Stage 22 source is not unique: {path}"
        )
    return matches[0]


def _source_from_base(
    capture: stage21._CapturedUpstream, path: str
) -> stage19.RichFileSnapshot:
    matches = tuple(item for item in capture.snapshot.sources if item.path == path)
    if len(matches) != 1:
        raise StructuredStage22PublicationError(
            f"structured Stage 22 base source is not unique: {path}"
        )
    return matches[0]


def _expected_code_files(
    deterministic: Stage22DeterministicOutputs,
) -> dict[str, bytes]:
    result = dict(deterministic.code_files)
    if not result or "README.md" not in result or "requirements.txt" not in result:
        raise StructuredStage22PublicationError(
            "structured Stage 22 code support files missing"
        )
    project = {
        name: content
        for name, content in result.items()
        if name not in {"README.md", "requirements.txt"}
    }
    if not project:
        raise StructuredStage22PublicationError(
            "structured Stage 22 project file set is empty"
        )
    _validate_logical_paths(tuple(result))
    return {
        **{name: project[name] for name in sorted(project)},
        "README.md": result["README.md"],
        "requirements.txt": result["requirements.txt"],
    }


def _validate_logical_paths(paths: tuple[str, ...]) -> None:
    aliases: set[str] = set()
    file_set = set(paths)
    if len(file_set) != len(paths):
        raise StructuredStage22PublicationError("duplicate Stage 22 code path")
    for value in paths:
        _safe_relative(value)
        alias = unicodedata.normalize("NFC", value).casefold()
        if alias in aliases:
            raise StructuredStage22PublicationError(
                "Stage 22 code path case/NFC alias"
            )
        aliases.add(alias)
        path = PurePosixPath(value)
        for parent in path.parents:
            if parent.as_posix() in file_set:
                raise StructuredStage22PublicationError(
                    "Stage 22 code file/directory prefix collision"
                )


def _validate_template_names(names: tuple[str, ...]) -> None:
    reserved = {
        *STRUCTURED_STAGE22_FIXED_FILES,
        "paper.pdf",
        "code",
        STRUCTURED_STAGE22_MANIFEST,
    }
    aliases: set[str] = set()
    reserved_aliases = {
        unicodedata.normalize("NFC", item).casefold() for item in reserved
    }
    for name in names:
        _safe_component(name)
        alias = unicodedata.normalize("NFC", name).casefold()
        if alias in aliases or any(
            reserved_alias.startswith(alias)
            for reserved_alias in reserved_aliases
        ):
            raise StructuredStage22PublicationError(
                "Stage 22 template name collision"
            )
        aliases.add(alias)


def _validate_output_order(
    outputs: list[dict[str, object]],
    compile_value: Mapping[str, object],
) -> None:
    fixed = outputs[:8]
    if [entry["role"] for entry in fixed] != list(
        STRUCTURED_STAGE22_FIXED_ROLES
    ):
        raise StructuredStage22PublicationError("Stage 22 fixed output order mismatch")
    if [entry["path"] for entry in fixed] != [
        f"stage-22/{name}" for name in STRUCTURED_STAGE22_FIXED_FILES
    ] or any(entry["logical_name"] is not None for entry in fixed):
        raise StructuredStage22PublicationError("Stage 22 fixed output binding mismatch")
    cursor = 8
    pdf_expected = compile_value["paper_pdf"] is not None
    if pdf_expected:
        if (
            cursor >= len(outputs)
            or outputs[cursor]["role"] != "paper_pdf"
            or outputs[cursor]["logical_name"] is not None
            or outputs[cursor]["path"] != "stage-22/paper.pdf"
        ):
            raise StructuredStage22PublicationError("Stage 22 PDF order mismatch")
        cursor += 1
    groups: list[tuple[str, list[dict[str, object]]]] = []
    for role in ("template_file", "project_file"):
        start = cursor
        while cursor < len(outputs) and outputs[cursor]["role"] == role:
            cursor += 1
        group = outputs[start:cursor]
        names = [entry["logical_name"] for entry in group]
        if names != sorted(names) or len(names) != len(set(names)):
            raise StructuredStage22PublicationError(
                f"Stage 22 {role} order mismatch"
            )
        if any(not isinstance(name, str) or not name for name in names):
            raise StructuredStage22PublicationError(
                f"Stage 22 {role} logical name mismatch"
            )
        groups.append((role, group))
    support = outputs[cursor:]
    if (
        len(support) != 2
        or [entry["role"] for entry in support]
        != ["code_support_file", "code_support_file"]
        or [entry["logical_name"] for entry in support]
        != ["README.md", "requirements.txt"]
        or [entry["path"] for entry in support]
        != [
            "stage-22/code/README.md",
            "stage-22/code/requirements.txt",
        ]
    ):
        raise StructuredStage22PublicationError(
            "Stage 22 code support order mismatch"
        )


def _output_entries(
    value: object,
    *,
    allowed_roles: set[str] | None,
    nonempty: bool = False,
) -> list[dict[str, object]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise StructuredStage22PublicationError("Stage 22 output list mismatch")
    result: list[dict[str, object]] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "role",
            "logical_name",
            "path",
            "sha256",
            "size",
        }:
            raise StructuredStage22PublicationError(
                "Stage 22 output entry fields mismatch"
            )
        if not isinstance(entry["role"], str) or (
            allowed_roles is not None and entry["role"] not in allowed_roles
        ):
            raise StructuredStage22PublicationError("Stage 22 output role mismatch")
        if allowed_roles is not None and (
            not isinstance(entry["logical_name"], str)
            or not entry["logical_name"]
        ):
            raise StructuredStage22PublicationError(
                "Stage 22 output logical name mismatch"
            )
        _safe_relative(entry["path"])
        _sha(entry["sha256"], "output sha256")
        _nonnegative_int(entry["size"], "output size")
        result.append(entry)
    return result


def _compile_manifest(value: object) -> dict[str, object]:
    fields = (
        "outcome",
        "semantic_calls",
        "max_attempts",
        "attempts",
        "status",
        "tooling_available",
        "errors",
        "status_file",
        "paper_pdf",
    )
    if not isinstance(value, dict) or set(value) != set(fields):
        raise StructuredStage22PublicationError("Stage 22 compile fields mismatch")
    _true_int(value["semantic_calls"], 1, "compile semantic calls")
    _true_int(value["max_attempts"], 2, "compile max attempts")
    if type(value["attempts"]) is not int or not 0 <= value["attempts"] <= 2:
        raise StructuredStage22PublicationError("compile attempts mismatch")
    if type(value["tooling_available"]) is not bool:
        raise StructuredStage22PublicationError("compile tooling mismatch")
    if not isinstance(value["errors"], list):
        raise StructuredStage22PublicationError("compile errors mismatch")
    _file_ref(value["status_file"], "compile status file")
    if value["status_file"]["path"] != "stage-22/compile_status.json":
        raise StructuredStage22PublicationError("compile status path mismatch")
    if value["outcome"] == "compiler-success":
        if (
            value["status"] != "success"
            or value["tooling_available"] is not True
            or value["attempts"] not in {1, 2}
            or value["errors"] != []
        ):
            raise StructuredStage22PublicationError("compile success branch mismatch")
        _file_ref(value["paper_pdf"], "compile PDF")
        if value["paper_pdf"]["path"] != "stage-22/paper.pdf":
            raise StructuredStage22PublicationError("compile PDF path mismatch")
    elif value["outcome"] == "compiler-failure":
        if (
            value["paper_pdf"] is not None
            or (
                value["status"] == "toolchain_missing"
                and (
                    value["tooling_available"] is not False
                    or value["attempts"] != 0
                    or value["errors"] != ["toolchain_missing"]
                )
            )
            or (
                value["status"] == "latex_error"
                and (
                    value["tooling_available"] is not True
                    or value["attempts"] not in {1, 2}
                    or value["errors"]
                    != (
                        ["compiler_attempt_1_failed"]
                        if value["attempts"] == 1
                        else [
                            "compiler_attempt_1_failed",
                            "compiler_attempt_2_failed",
                        ]
                    )
                )
            )
            or value["status"] not in {"toolchain_missing", "latex_error"}
        ):
            raise StructuredStage22PublicationError("compile failure has PDF")
    else:
        raise StructuredStage22PublicationError("compile outcome mismatch")
    return value


def _file_ref(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise StructuredStage22PublicationError(f"{label} FileRef fields mismatch")
    _safe_relative(value["path"])
    _sha(value["sha256"], f"{label} sha256")
    _nonnegative_int(value["size"], f"{label} size")
    return value


def _output_entry(
    role: str, logical_name: str | None, path: str, content: bytes
) -> dict[str, object]:
    return {
        "role": role,
        "logical_name": logical_name,
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _rich_ref(item: stage19.RichFileSnapshot) -> dict[str, object]:
    return {"path": item.path, "sha256": item.sha256, "size": item.size}


def _bytes_ref(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _validate_pdf(content: bytes) -> None:
    eof = content.rfind(b"%%EOF")
    startxref = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*\Z", content)
    valid_xref = False
    if startxref is not None:
        offset = int(startxref.group(1))
        valid_xref = 0 <= offset < len(content) and (
            content[offset:].startswith(b"xref")
            or b"/Type /XRef" in content[offset : offset + 512]
        )
    if (
        len(content) <= 32
        or not re.match(rb"%PDF-1\.[0-9]\r?\n", content)
        or eof < len(content) - 1024
        or startxref is None
        or not valid_xref
        or b" obj" not in content
        or b"endobj" not in content
        or (b"trailer" not in content and b"/Type /XRef" not in content)
    ):
        raise StructuredStage22PublicationError(
            "structured Stage 22 compiler produced invalid PDF"
        )


def _strict_object(content: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StructuredStage22PublicationError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise StructuredStage22PublicationError(f"{label} root is not an object")
    return value


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredStage22PublicationError(
                f"duplicate Stage 22 JSON key: {key}"
            )
        result[key] = value
    return result


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return canonical_authority_json_text(dict(value)).encode("utf-8")


def _safe_relative(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or any(
            ord(character) < 32
            or ord(character) == 127
            or unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    ):
        raise StructuredStage22PublicationError("unsafe Stage 22 path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StructuredStage22PublicationError("unsafe Stage 22 path")
    windows_devices = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            part.endswith((" ", "."))
            or ":" in part
            or stem in windows_devices
        ):
            raise StructuredStage22PublicationError(
                "unsafe Stage 22 platform path alias"
            )
    return value


def _safe_component(value: str) -> str:
    if "/" in value:
        raise StructuredStage22PublicationError("unsafe Stage 22 component")
    return _safe_relative(value)


def _sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StructuredStage22PublicationError(f"{label} mismatch")
    return value


def _true_int(value: object, expected: int, label: str) -> None:
    if type(value) is not int or value != expected:
        raise StructuredStage22PublicationError(f"{label} mismatch")


def _nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise StructuredStage22PublicationError(f"{label} mismatch")


def _true_positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise StructuredStage22PublicationError(f"{label} mismatch")


def _generated(capture: _CapturedUpstream) -> str:
    return capture.base.snapshot.authority_inputs.generated


def _capability_tuple() -> tuple[int, int, int, int]:
    snapshot = capability.code_owned_structured_capability_snapshot()
    return tuple(
        snapshot[name] for name in capability.REQUIRED_STRUCTURED_CAPABILITIES
    )  # type: ignore[return-value]


def _capability_dict() -> dict[str, int]:
    return capability.code_owned_structured_capability_snapshot()


def _require_private_capability() -> None:
    if (
        capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION != 1
        or _capability_tuple() != (1, 1, 1, 0)
    ):
        raise StructuredStage22PublicationError(
            "private structured Stage 22 requires exact capability 1110"
        )


def _advance(record: _AttemptRecord, current: str, target: str) -> None:
    if record.phase != current:
        raise StructuredStage22PublicationError(
            f"structured Stage 22 phase mismatch: {record.phase} != {current}"
        )
    record.phase = target


def _close_attempt_record(record: _AttemptRecord) -> None:
    if record.manifest is not None:
        _close_fd(record.manifest.descriptor)
    for held in (record.held_files or {}).values():
        _close_fd(held.descriptor)
    for held in reversed(tuple((record.held_directories or {}).values())):
        _close_fd(held.descriptor)
    record.namespace.close()
    record.initial.close()


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
