"""Inactive held-fd structured Stage 24 truth publication.

Ordinary dispatch remains generic-v1 while the code-owned capability is 1110.
Only the registry-issued private seam in this module may exercise the
pre-activation implementation.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence
import weakref

import yaml

from researchclaw.experiment_runtime.contract import validate_contract_dict
from researchclaw.literature.citation_plan import parse_citation_plan
from researchclaw.literature.evidence_cards import (
    EvidenceCardContractError,
    parse_cards_manifest,
)
from researchclaw.literature.experiment_fact_closure import (
    find_dataset_claim_violations,
)
from researchclaw.pipeline import stage23_structured_publication as stage23
from researchclaw.pipeline import stage23_structured_authority as stage23_authority
from researchclaw.pipeline import stage24_publication as generic_stage24
from researchclaw.pipeline import stage24_structured_authority as authority
from researchclaw.pipeline import stage24_structured_transport as transport
from researchclaw.pipeline import stage24_obligations as obligation_authority
from researchclaw.pipeline import scientific_claim_authority as claim_authority
from researchclaw.pipeline import stage19_structured_authority as stage19_authority
from researchclaw.pipeline import stage22_semantics
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_fact_sheet import (
    build_canonical_fact_sheet,
    fact_sheet_numeric_authority_records,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.stage15_critique import (
    Stage15CritiquePublication,
    _parse_manifest as _parse_stage15_manifest,
    parse_critique,
)
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    semantic_config_sha256,
)
from researchclaw.pipeline.stage23_verification import (
    Stage23PublicationSnapshot,
)
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundle,
    Stage24InputEntry,
    Stage24ModelProjection,
)
from researchclaw.pipeline.stage24_obligations import (
    ClaimObligation,
    build_claim_obligation_inventory,
    canonical_obligation_inventory_bytes,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    parse_manuscript,
)
from researchclaw.pipeline.scientific_claim_publication import SECTION_ORDER
from researchclaw.pipeline.stages import StageStatus


class StructuredStage24PublicationError(RuntimeError):
    """Structured Stage 24 admission, publication, or replay failed."""


STRUCTURED_STAGE24_ARTIFACTS = authority.STRUCTURED_STAGE24_COARSE_ARTIFACTS
STRUCTURED_STAGE24_EVIDENCE_REFS = authority.STRUCTURED_STAGE24_EVIDENCE_REFS
_STAGE_NAME = "stage-24"
_MANIFEST_NAME = "stage24_truth_manifest.json"
_DIRECT_NAMES = authority.STRUCTURED_STAGE24_COARSE_ARTIFACTS[:6]
_DIRECT_ROLES = authority.DIRECT_OUTPUT_ROLES
_DIRECTORIES = (
    "citation-assessments",
    "generic-support-assessments",
    "resolution-assessments",
)
_CONSTRUCTION_AUTHORITY = object()


class _NonTransferableContext:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("structured Stage 24 context cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("structured Stage 24 context cannot be copied")

    def __reduce__(self):
        raise TypeError("structured Stage 24 context cannot be serialized")


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage24PreAdmissionContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_snapshot: tuple[int, int, int, int]
    generation_binding_sha256: str
    source_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority_token: object,
        token: object,
        writer_owner: object,
        run_identity: tuple[int, int],
        generation_binding_sha256: str,
        source_identity: tuple[object, ...],
    ) -> None:
        if authority_token is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 24 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "logical_stage_id", "stage24")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(self, "source_identity", source_identity)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage24AttemptContext(_NonTransferableContext):
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
            raise TypeError("structured Stage 24 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(self, "logical_stage_id", "stage24")
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
class _CapturedUpstream:
    base: stage23._CapturedUpstream
    source_files: tuple[_HeldFile, ...]
    source_map: Mapping[str, BoundArtifact]
    stage23_namespace: BoundOutputNamespace
    stage15_namespace: BoundOutputNamespace
    generic_bundle: Stage24InputBundle
    stage23_manifest: Mapping[str, object]
    cfs: Mapping[str, object]
    cfs_records: tuple[Mapping[str, object], ...]
    config_digest: str
    release_graph_owner: ReleaseGraphLock
    writer_epoch_object: object
    canonical_config_object: object
    attempt_context_object: Stage24AttemptContext | None
    role_bindings: Mapping[str, transport.UnderlyingClientBinding]
    client_registry: transport.Stage24ClientRegistry
    obligations: tuple[ClaimObligation, ...]
    renderer_occurrences: tuple["_RendererOccurrence", ...]
    deterministic_declarative_ids: tuple[str, ...]
    numeric: Mapping[str, Mapping[str, object]]
    citation_inputs: tuple["_AssessmentSpec", ...]
    generic_universe: tuple[ClaimObligation, ...]
    resolution_inputs: tuple["_AssessmentSpec", ...]
    input_bundle_sha256: str

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.base.identity_tuple,
            tuple(
                (
                    item.logical_path,
                    item.identity,
                    hashlib.sha256(item.expected_bytes).hexdigest(),
                    len(item.expected_bytes),
                )
                for item in self.source_files
            ),
            self.stage23_namespace._stage_identity,
            self.stage15_namespace._stage_identity,
            self.input_bundle_sha256,
            self.client_registry.identity_tuple,
            tuple(
                (
                    role,
                    binding.underlying_client_binding_sha256,
                    binding.client_binding_sha256,
                )
                for role, binding in self.role_bindings.items()
            ),
            tuple(
                occurrence.identity_tuple
                for occurrence in self.renderer_occurrences
            ),
            self.deterministic_declarative_ids,
        )

    def close(self) -> None:
        _drop_active_clients(self.client_registry)
        for item in self.source_files:
            for descriptor in (item.descriptor, item.parent_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.stage15_namespace.close()
        self.stage23_namespace.close()
        self.base.close()


@dataclass(frozen=True)
class _AssessmentSpec:
    role: str
    input: Mapping[str, object]
    assessment_id: str
    context: Mapping[str, object]
    obligation_id: str | None
    finding_content_sha256: str | None


@dataclass(frozen=True)
class _RendererSlotOccurrence:
    section_id: str
    selection_ordinal: int
    claim_id: str
    slot_ordinal: int
    fact_kind: str
    fact_id: str
    stage19_byte_start: int
    stage19_byte_end: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True)
class _RendererOccurrence:
    generation_binding_sha256: str
    cfs_sha256: str
    registry_content: bytes
    registry_sha256: str
    section_id: str
    selection_ordinal: int
    claim_id: str
    renderer_template_id: str
    renderer_slot_fact_ids: tuple[str, ...]
    sentence_content: bytes
    sentence_sha256: str
    stage19_paper_sha256: str
    stage19_byte_start: int
    stage19_byte_end: int
    stage22_paper_sha256: str
    final_paper_path: str
    final_paper_sha256: str
    final_paper_identity: tuple[int, int]
    byte_start: int
    byte_end: int
    slots: tuple[_RendererSlotOccurrence, ...]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.generation_binding_sha256,
            self.cfs_sha256,
            self.registry_content,
            self.registry_sha256,
            self.claim_id,
            self.renderer_template_id,
            self.renderer_slot_fact_ids,
            self.section_id,
            self.selection_ordinal,
            self.sentence_content,
            self.sentence_sha256,
            self.stage19_paper_sha256,
            self.stage19_byte_start,
            self.stage19_byte_end,
            self.stage22_paper_sha256,
            self.final_paper_path,
            self.final_paper_sha256,
            self.final_paper_identity,
            self.byte_start,
            self.byte_end,
            tuple(
                (
                    slot.slot_ordinal,
                    slot.fact_kind,
                    slot.fact_id,
                    slot.stage19_byte_start,
                    slot.stage19_byte_end,
                    slot.byte_start,
                    slot.byte_end,
                )
                for slot in self.slots
            ),
        )


@dataclass
class _PreRecord:
    token: object
    owner: ReleaseGraphLock
    capture: _CapturedUpstream
    finalizer: weakref.finalize
    phase: str = "pre_admission"


@dataclass
class _AttemptRecord:
    token: object
    owner: ReleaseGraphLock
    initial: _CapturedUpstream
    namespace: BoundOutputNamespace
    phase: str = "namespace_bound"
    held_directories: dict[str, tuple[int, tuple[int, int]]] | None = None
    held_files: dict[str, _HeldFile] | None = None
    manifest: _HeldFile | None = None
    final_snapshot: tuple[object, ...] | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class _ActiveClientRecord:
    registry_authority_token: object
    registration_identity: object
    registry_identity: object
    release_graph_owner_object: ReleaseGraphLock
    run_fd_identity: tuple[int, int]
    writer_epoch_object: object
    stage24_attempt_context_object: Stage24AttemptContext
    canonical_config_object: object
    semantic_config_sha256: str
    role: str
    binding: transport.UnderlyingClientBinding
    client_ref: weakref.ReferenceType[
        transport.Stage24AssessmentTransport
    ]
    active_client_exact_type: type
    underlying_client_binding_sha256: str
    credential_source_identity: object
    credential_identity: object
    credential_bytes: bytes


@dataclass(frozen=True)
class ProvisionalStage24Result:
    status: StageStatus
    artifacts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    degraded: bool
    context: Stage24AttemptContext


@dataclass
class CurrentStructuredStage24Capture:
    """Held, independently replayed current Stage 24 authority for Stage 25."""

    _record: _AttemptRecord
    manifest_value: Mapping[str, object]
    manifest: BoundArtifact
    outputs: tuple[BoundArtifact, ...]
    paper: BoundArtifact
    stage22_manifest: BoundArtifact
    stage23_manifest: BoundArtifact

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return _capture_final_state(self._record)

    def close(self) -> None:
        _close_attempt(self._record)


_PRE_CONTEXTS: weakref.WeakKeyDictionary[
    Stage24PreAdmissionContext, _PreRecord
] = weakref.WeakKeyDictionary()
_ATTEMPT_CONTEXTS: weakref.WeakKeyDictionary[
    Stage24AttemptContext, _AttemptRecord
] = weakref.WeakKeyDictionary()
_ACTIVE_CLIENTS: weakref.WeakKeyDictionary[
    transport.Stage24AssessmentTransport,
    _ActiveClientRecord,
] = weakref.WeakKeyDictionary()
_ACTIVE_REGISTRATIONS: dict[object, _ActiveClientRecord] = {}


def issue_stage24_pre_admission_context(
    lease: ReleaseGraphLock,
    *,
    clients: Mapping[
        str,
        Callable[
            [transport.UnderlyingClientBinding],
            transport.Stage24AssessmentTransport,
        ],
    ],
) -> Stage24PreAdmissionContext:
    """Capture/replay all upstream bytes before Stage 24 pathname access."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_private_capability()
    if type(clients) is not dict or not set(clients).issubset(
        set(transport.ROLE_ORDER)
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 client factory map is invalid"
        )
    capture = _capture_upstream(writer, clients=clients)
    token = object()
    context = Stage24PreAdmissionContext(
        authority_token=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=owner,
        run_identity=owner._run_identity,
        generation_binding_sha256=(
            capture.base.base.base.snapshot.authority_inputs.generation_binding_sha256
        ),
        source_identity=capture.identity_tuple,
    )
    finalizer = weakref.finalize(context, capture.close)
    _PRE_CONTEXTS[context] = _PreRecord(token, owner, capture, finalizer)
    return context


def capture_current_structured_stage24(
    lease: ReleaseGraphLock,
) -> CurrentStructuredStage24Capture:
    """Capture and independently replay current Stage 24 through held fds."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    _require_private_capability()
    initial = _capture_upstream(writer, clients={})
    namespace: BoundOutputNamespace | None = None
    held_directories: dict[str, tuple[int, tuple[int, int]]] = {}
    held_files: dict[str, _HeldFile] = {}
    manifest: _HeldFile | None = None
    try:
        namespace = writer.open_stage_namespace(_STAGE_NAME)
        require_namespace_owned_by_epoch(namespace, writer, _STAGE_NAME)
        if set(namespace.direct_entries()) != {
            *_DIRECT_NAMES,
            *_DIRECTORIES,
            _MANIFEST_NAME,
        }:
            raise StructuredStage24PublicationError(
                "current Stage 24 exact namespace mismatch"
            )
        for directory in _DIRECTORIES:
            descriptor = os.open(
                directory,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=namespace._stage_fd,
            )
            info = os.fstat(descriptor)
            named = os.stat(
                directory,
                dir_fd=namespace._stage_fd,
                follow_symlinks=False,
            )
            identity = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISDIR(info.st_mode)
                or identity != (named.st_dev, named.st_ino)
            ):
                os.close(descriptor)
                raise StructuredStage24PublicationError(
                    f"current Stage 24 directory mismatch: {directory}"
                )
            held_directories[directory] = (descriptor, identity)
        for name in _DIRECT_NAMES:
            held_files[name] = _hold_existing_stage24_file(
                namespace._stage_fd,
                name,
                logical_path=f"stage-24/{name}",
            )
        for directory in _DIRECTORIES:
            descriptor, _identity = held_directories[directory]
            for name in sorted(os.listdir(descriptor)):
                if not name.endswith(".json"):
                    raise StructuredStage24PublicationError(
                        "current Stage 24 assessment name mismatch"
                    )
                key = f"{directory}/{name}"
                held_files[key] = _hold_existing_stage24_file(
                    descriptor,
                    name,
                    logical_path=f"stage-24/{key}",
                )
        manifest = _hold_existing_stage24_file(
            namespace._stage_fd,
            _MANIFEST_NAME,
            logical_path=f"stage-24/{_MANIFEST_NAME}",
        )
        record = _AttemptRecord(
            object(),
            writer._require_active(),
            initial,
            namespace,
            phase="current_replay",
            held_directories=held_directories,
            held_files=held_files,
            manifest=manifest,
        )
        value = authority.strict_json_object(
            manifest.expected_bytes,
            label="current Stage 24 truth manifest",
        )
        record.outcome = value["outcome"]
        _verify_complete_publication(record)
        _verify_source_snapshot(initial)
        snapshot = _capture_final_state(record)
        if _capture_final_state(record) != snapshot:
            raise StructuredStage24PublicationError(
                "current Stage 24 second capture mismatch"
            )
        record.final_snapshot = snapshot
        output_by_path = {
            item.logical_path: BoundArtifact(
                item.logical_path,
                hashlib.sha256(item.expected_bytes).hexdigest(),
                item.expected_bytes,
            )
            for item in held_files.values()
        }
        source = initial.source_map
        return CurrentStructuredStage24Capture(
            record,
            value,
            BoundArtifact(
                f"stage-24/{_MANIFEST_NAME}",
                hashlib.sha256(manifest.expected_bytes).hexdigest(),
                manifest.expected_bytes,
            ),
            tuple(output_by_path[row["path"]] for row in value["outputs"]),
            source["stage-23/paper_final_verified.md"],
            source["stage-22/stage22_export_manifest.json"],
            source["stage-23/stage23_verification_manifest.json"],
        )
    except Exception:
        if namespace is not None:
            record = _AttemptRecord(
                object(),
                writer._require_active(),
                initial,
                namespace,
                held_directories=held_directories,
                held_files=held_files,
                manifest=manifest,
            )
            _close_attempt(record)
        else:
            initial.close()
        raise


def verify_current_structured_stage24(
    capture: CurrentStructuredStage24Capture,
) -> None:
    if type(capture) is not CurrentStructuredStage24Capture:
        raise StructuredStage24PublicationError(
            "current Stage 24 capture type mismatch"
        )
    _verify_complete_publication(capture._record)
    _verify_source_snapshot(capture._record.initial)
    if _capture_final_state(capture._record) != capture._record.final_snapshot:
        raise StructuredStage24PublicationError(
            "current Stage 24 capture changed"
        )


def transition_stage24_pre_admission_context(
    lease: ReleaseGraphLock,
    context: Stage24PreAdmissionContext,
) -> Stage24AttemptContext:
    """Bind/invalidate the Stage 24 namespace before any credential read."""

    pre = _require_pre_context(lease, context)
    if pre.phase != "pre_admission":
        raise StructuredStage24PublicationError(
            "Stage 24 pre-admission context phase mismatch"
        )
    namespace = _acquire_stage24_namespace(lease)
    try:
        _withdraw_namespace(namespace)
        held_directories = _create_assessment_directories(namespace)
    except Exception:
        try:
            _withdraw_namespace(namespace)
        finally:
            namespace.close()
            pre.finalizer.detach()
            _PRE_CONTEXTS.pop(context, None)
            pre.phase = "cleared"
            pre.capture.close()
        raise
    token = object()
    attempt = Stage24AttemptContext(
        authority_token=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=pre.owner,
        run_identity=pre.owner._run_identity,
        stage_identity=namespace._stage_identity,
    )
    pre.finalizer.detach()
    _PRE_CONTEXTS.pop(context, None)
    pre.phase = "transitioned"
    pre.capture.attempt_context_object = attempt
    _ATTEMPT_CONTEXTS[attempt] = _AttemptRecord(
        token,
        pre.owner,
        pre.capture,
        namespace,
        phase="namespace_bound",
        held_directories=held_directories,
        held_files={},
    )
    return attempt


def produce_structured_stage24(
    lease: ReleaseGraphLock,
    context: Stage24AttemptContext,
) -> ProvisionalStage24Result:
    record = _require_attempt_context(lease, context)
    if record.phase != "namespace_bound":
        raise StructuredStage24PublicationError("Stage 24 attempt phase mismatch")
    capture = record.initial
    _verify_source_snapshot(capture)
    record.phase = "snapshot_a"
    for specs in (
        capture.citation_inputs,
        capture.resolution_inputs,
    ):
        if specs:
            role = specs[0].role
            _activate_registered_client(
                capture,
                role,
                capture.role_bindings[role],
            )

    citation_records, citation_bytes, ordinal = _run_specs(
        capture,
        capture.citation_inputs,
        ordinal_start=1,
    )
    generic_specs = _derive_generic_specs(
        capture,
        citation_records=citation_records,
        citation_bytes=citation_bytes,
    )
    if len(generic_specs) > 128:
        raise StructuredStage24PublicationError("Stage 24 G exceeds 128")
    generic_records, generic_bytes, ordinal = _run_specs(
        capture,
        generic_specs,
        ordinal_start=ordinal,
    )
    resolution_records, resolution_bytes, ordinal = _run_specs(
        capture,
        capture.resolution_inputs,
        ordinal_start=ordinal,
    )
    total = authority.validate_assessment_counts(
        citation=len(capture.citation_inputs),
        generic=len(generic_specs),
        resolution=len(capture.resolution_inputs),
    )
    if ordinal != total + 1:
        raise StructuredStage24PublicationError(
            "Stage 24 semantic call ordinal closure mismatch"
        )
    outbound_total = sum(
        item["transport_receipt"]["outbound_attempts"]
        for item in (
            *citation_records.values(),
            *generic_records.values(),
            *resolution_records.values(),
        )
    )
    if outbound_total > 536:
        raise StructuredStage24PublicationError(
            "Stage 24 outbound attempt bound exceeded"
        )

    fixed, success, outcome = _build_fixed_payloads(
        capture,
        citation_records=citation_records,
        generic_records=generic_records,
        resolution_records=resolution_records,
    )
    if not success:
        raise StructuredStage24PublicationError(
            "Stage 24 truth closure is incomplete"
        )
    assessment_groups = {
        "citation-assessments": citation_bytes,
        "generic-support-assessments": generic_bytes,
        "resolution-assessments": resolution_bytes,
    }
    _publish_payloads(record, fixed, assessment_groups)
    _verify_payloads(record)
    _verify_source_snapshot(capture)
    manifest = _build_manifest(
        record,
        fixed=fixed,
        assessment_groups=assessment_groups,
        outcome=outcome,
    )
    authority.validate_stage24_manifest_v2(manifest)
    record.manifest = _create_held_file(
        record.namespace._stage_fd,
        _MANIFEST_NAME,
        authority.global_canonical_json_bytes(manifest),
        logical_path=f"stage-24/{_MANIFEST_NAME}",
    )
    record.outcome = outcome
    _verify_complete_publication(record)
    _verify_source_snapshot(capture)
    snapshot = _capture_final_state(record)
    if _capture_final_state(record) != snapshot:
        raise StructuredStage24PublicationError(
            "Stage 24 second final capture mismatch"
        )
    record.final_snapshot = snapshot
    record.phase = "provisional_done"
    return ProvisionalStage24Result(
        StageStatus.DONE,
        STRUCTURED_STAGE24_ARTIFACTS,
        STRUCTURED_STAGE24_EVIDENCE_REFS,
        outcome == "degraded",
        context,
    )


def validate_structured_stage24_immediate_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage24Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage24AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    _verify_source_snapshot(record.initial)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage24PublicationError(
            "Stage 24 immediate snapshot changed"
        )
    record.phase = "immediate_validated"


def validate_structured_stage24_terminal_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage24Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage24AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    if record.phase != "immediate_validated":
        raise StructuredStage24PublicationError(
            "Stage 24 terminal postcondition bypassed immediate validation"
        )
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    _verify_source_snapshot(record.initial)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage24PublicationError(
            "Stage 24 terminal snapshot changed"
        )
    record.phase = "terminal_validated"


def clear_structured_stage24_context(
    lease: ReleaseGraphLock,
    context: Stage24AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    if record.phase != "terminal_validated":
        raise StructuredStage24PublicationError(
            "Stage 24 context is not terminal validated"
        )
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt(record)


def fail_structured_stage24_attempt(
    lease: ReleaseGraphLock,
    context: Stage24AttemptContext,
) -> tuple[str, ...]:
    record = _require_attempt_context(lease, context)
    errors = _cleanup_attempt_outputs(record)
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt(record)
    return errors


def context_phase(context: object) -> str:
    if isinstance(context, Stage24PreAdmissionContext):
        record = _PRE_CONTEXTS.get(context)
    elif isinstance(context, Stage24AttemptContext):
        record = _ATTEMPT_CONTEXTS.get(context)
    else:
        raise StructuredStage24PublicationError("unknown Stage 24 context")
    return "cleared" if record is None else record.phase


def execute_structured_stage24_truth(
    run_dir: Path,
    stage_dir: Path,
    *,
    clients: Mapping[
        str,
        Callable[
            [transport.UnderlyingClientBinding],
            transport.Stage24AssessmentTransport,
        ],
    ],
):
    """Public/direct entry; incomplete-capability guard is the first action."""

    capability.require_complete_structured_capability(
        "execute_structured_stage24_truth"
    )
    try:
        with ReleaseGraphLock.acquire(
            run_dir, "execute_structured_stage24_truth", mode="write"
        ) as lease:
            from researchclaw.pipeline.executor import (
                _execute_structured_stage24_with_pre_admission_private,
            )

            return _execute_structured_stage24_with_pre_admission_private(
                lease,
                clients=clients,
                canonical_stage_dir=stage_dir == run_dir / _STAGE_NAME,
            )
    except Exception:
        from researchclaw.pipeline._helpers import StageResult
        from researchclaw.pipeline.stages import Stage

        return StageResult(
            stage=Stage.TRUTH_AUDIT,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error="Structured Stage 24 failed before attempt",
            decision="retry",
        )


def _capture_upstream(
    writer: ReleaseGraphLock,
    *,
    clients: Mapping[
        str,
        Callable[
            [transport.UnderlyingClientBinding],
            transport.Stage24AssessmentTransport,
        ],
    ],
) -> _CapturedUpstream:
    base = stage23._capture_upstream(writer, llm=None)
    stage23_namespace: BoundOutputNamespace | None = None
    stage15_namespace: BoundOutputNamespace | None = None
    held: list[_HeldFile] = []
    try:
        stage23_namespace = writer.open_stage_namespace("stage-23")
        require_namespace_owned_by_epoch(stage23_namespace, writer, "stage-23")
        if set(stage23_namespace.direct_entries()) != {
            "verification_report.json",
            "references_verified.bib",
            "paper_final_verified.md",
            "stage23_verification_manifest.json",
        }:
            raise StructuredStage24PublicationError(
                "Stage 23 exact four-file namespace mismatch"
            )
        stage15_namespace = writer.open_stage_namespace("stage-15")
        require_namespace_owned_by_epoch(stage15_namespace, writer, "stage-15")
        stage15_entries = set(stage15_namespace.direct_entries())
        if stage15_entries not in (
            {
                "critique.json",
                "stage15_critique_manifest.json",
            },
            {
                "decision.md",
                "decision_structured.json",
                "critique.json",
                "stage15_critique_manifest.json",
            },
        ):
            raise StructuredStage24PublicationError(
                "Stage 15 exact critique namespace mismatch"
            )

        expected = _expected_source_snapshots(base)
        source_paths = tuple(
            dict.fromkeys(
                (
                    *expected,
                    "stage-04/candidates.jsonl",
                    "stage-06/cards_manifest.json",
                    "stage-15/critique.json",
                    "stage-15/stage15_critique_manifest.json",
                    "stage-23/verification_report.json",
                    "stage-23/references_verified.bib",
                    "stage-23/paper_final_verified.md",
                    "stage-23/stage23_verification_manifest.json",
                )
            )
        )
        for path in source_paths:
            try:
                item = _hold_run_file(writer, path)
            except (FileNotFoundError, OSError) as exc:
                raise StructuredStage24PublicationError(
                    f"required Stage 24 source is unavailable: {path}"
                ) from exc
            captured = expected.get(path)
            if captured is not None and (
                captured.content != item.expected_bytes
                or captured.sha256
                != hashlib.sha256(item.expected_bytes).hexdigest()
                or captured.size != len(item.expected_bytes)
                or captured.identity != item.identity
            ):
                raise StructuredStage24PublicationError(
                    f"held source differs from upstream capture: {path}"
                )
            held.append(item)
        cards_manifest_item = next(
            item
            for item in held
            if item.logical_path == "stage-06/cards_manifest.json"
        )
        try:
            cards_manifest = parse_cards_manifest(
                cards_manifest_item.expected_bytes.decode("utf-8")
            )
        except (UnicodeDecodeError, EvidenceCardContractError) as exc:
            raise StructuredStage24PublicationError(
                "Stage 24 cards manifest replay failed"
            ) from exc
        card_paths = tuple(
            sorted(
                {
                    str(row[field])
                    for row in cards_manifest["cards"]
                    for field in ("json_path", "markdown_path")
                }
            )
        )
        for path in card_paths:
            try:
                held.append(_hold_run_file(writer, path))
            except (FileNotFoundError, OSError) as exc:
                raise StructuredStage24PublicationError(
                    f"required Stage 24 evidence card is unavailable: {path}"
                ) from exc
        source_map = {
            item.logical_path: BoundArtifact(
                item.logical_path,
                hashlib.sha256(item.expected_bytes).hexdigest(),
                item.expected_bytes,
            )
            for item in held
        }
        stage23_manifest = stage23_authority.parse_verification_manifest_v2(
            source_map[
                "stage-23/stage23_verification_manifest.json"
            ].content
        )
        _verify_stage23_outputs(source_map, stage23_manifest)
        critique_value = _strict_object(
            source_map["stage-15/critique.json"].content, "Stage 15 critique"
        )
        critique = parse_critique(critique_value)
        critique_manifest = _parse_stage15_manifest(
            source_map[
                "stage-15/stage15_critique_manifest.json"
            ].content
        )
        if (
            critique_manifest["critique_sha256"]
            != source_map["stage-15/critique.json"].sha256
            or critique_manifest["finding_count"] != len(critique["findings"])
        ):
            raise StructuredStage24PublicationError(
                "Stage 15 critique manifest closure mismatch"
            )
        config = base.base.semantic_inputs.canonical_config
        config_digest = semantic_config_sha256(config)
        models = _model_projection(config)
        prospective = {
            role: transport.issue_role_client_binding(
                role=role,
                underlying=transport.issue_underlying_client_binding(
                    semantic_config_sha256=config_digest,
                    provider=config.llm.provider,
                    base_url=config.llm.base_url,
                    wire_api=config.llm.wire_api,
                    model=models[role],
                ),
            )
            for role in transport.ROLE_ORDER
        }
        generic_bundle = _build_generic_bundle(
            base,
            source_map=source_map,
            critique=critique,
            critique_manifest=critique_manifest,
            models=models,
        )
        obligations = build_claim_obligation_inventory(
            generic_bundle.paper.content
        )
        final_paper_held = next(
            item
            for item in held
            if item.logical_path == "stage-23/paper_final_verified.md"
        )
        renderer_occurrences = _renderer_occurrences(
            base,
            final_paper=generic_bundle.paper,
            final_paper_identity=final_paper_held.identity,
        )
        deterministic_declarative_ids = _deterministic_declarative_ids(
            renderer_occurrences,
            obligations,
        )
        numeric, cfs, cfs_records = _numeric_support(
            generic_bundle,
            obligations,
            structured_base=base,
            renderer_occurrences=renderer_occurrences,
        )
        generic_citation_inputs = generic_stage24._citation_inputs(
            generic_bundle, obligations
        )
        citation_specs = tuple(
            _citation_spec(
                generic_bundle,
                obligations,
                item,
                binding=prospective["citation_assessment"],
            )
            for item in generic_citation_inputs
        )
        closed_citations = frozenset(
            item.obligation_id for item in generic_citation_inputs
        )
        universe = authority.derive_generic_candidate_universe(
            obligations,
            supported_numeric_obligation_ids=frozenset(
                key
                for key, value in numeric.items()
                if value["status"] == "supported"
            ),
            structurally_closed_citation_obligation_ids=closed_citations,
        )
        resolution_specs = _resolution_specs(
            generic_bundle,
            binding=prospective["resolution_assessment"],
        )
        authority.validate_assessment_counts(
            citation=len(citation_specs),
            generic=len(universe),
            resolution=len(resolution_specs),
        )
        input_identity = authority.global_identity_sha256(
            {
                "policy_version": "stage24_input_bundle_structured_v2",
                "sources": [
                    {
                        "path": item.logical_path,
                        "sha256": hashlib.sha256(
                            item.expected_bytes
                        ).hexdigest(),
                        "size": len(item.expected_bytes),
                    }
                    for item in held
                ],
                "semantic_config_sha256": config_digest,
                "role_bindings": {
                    role: prospective[role].client_binding_sha256
                    for role in transport.ROLE_ORDER
                },
            }
        )
        capture = _CapturedUpstream(
            base,
            tuple(held),
            source_map,
            stage23_namespace,
            stage15_namespace,
            generic_bundle,
            stage23_manifest,
            cfs,
            cfs_records,
            config_digest,
            writer,
            writer._require_active(),
            config,
            None,
            prospective,
            transport._issue_client_registry(
                owner_identity=writer._require_active(),
                run_identity=writer._run_identity,
                semantic_config_sha256=config_digest,
                factories=clients,
            ),
            obligations,
            renderer_occurrences,
            deterministic_declarative_ids,
            numeric,
            citation_specs,
            universe,
            resolution_specs,
            input_identity,
        )
        _verify_source_snapshot(capture)
        return capture
    except Exception:
        for item in held:
            for descriptor in (item.descriptor, item.parent_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if stage15_namespace is not None:
            stage15_namespace.close()
        if stage23_namespace is not None:
            stage23_namespace.close()
        base.close()
        raise


def _build_generic_bundle(
    base: stage23._CapturedUpstream,
    *,
    source_map: Mapping[str, BoundArtifact],
    critique: Mapping[str, object],
    critique_manifest: Mapping[str, object],
    models: Mapping[str, str],
) -> Stage24InputBundle:
    evidence = base.base.semantic_inputs.evidence
    canonical = source_map[evidence.manifest_path]
    contract = source_map[evidence.experiment_contract_path]
    paper = source_map["stage-23/paper_final_verified.md"]
    report = source_map["stage-23/verification_report.json"]
    stage23_manifest = source_map[
        "stage-23/stage23_verification_manifest.json"
    ]
    bibliography = source_map["stage-23/references_verified.bib"]
    plan_artifact = source_map["stage-16/citation_plan.json"]
    plan = parse_citation_plan(plan_artifact.content.decode("utf-8"))
    cards_manifest = source_map["stage-06/cards_manifest.json"]
    if (
        plan.get("cards_manifest_path") != cards_manifest.path
        or plan.get("cards_manifest_sha256") != cards_manifest.sha256
    ):
        raise StructuredStage24PublicationError(
            "citation plan cards-manifest closure mismatch"
        )
    cards = tuple(
        _freeze(_strict_object(item.content, item.path))
        for path, item in source_map.items()
        if path.startswith("stage-06/cards/") and path.endswith(".json")
    )
    report_value = stage23_authority.parse_verification_report_v2(report.content)
    verification = {
        "results": tuple(_freeze(row) for row in report_value["citations"])
    }
    contract_value = yaml.safe_load(contract.content.decode("utf-8"))
    if type(contract_value) is not dict:
        raise StructuredStage24PublicationError(
            "experiment contract root is invalid"
        )
    dataset_origin = validate_contract_dict(contract_value).dataset_origin
    critique_publication = Stage15CritiquePublication(
        state=critique["state"],
        critique=_freeze(critique),
        manifest=_freeze(critique_manifest),
        artifacts=(
            "stage-15/critique.json",
            "stage-15/stage15_critique_manifest.json",
        ),
    )
    publication = Stage23PublicationSnapshot(
        manifest=stage23_manifest,
        outputs=(report, bibliography, paper),
    )
    entries = tuple(
        Stage24InputEntry(role="held_source", artifact=item)
        for item in source_map.values()
    )
    stage23_inputs = SimpleNamespace(
        stage22_inputs=SimpleNamespace(
            evidence=evidence,
            canonical_config=base.base.semantic_inputs.canonical_config,
            stage19_inputs=SimpleNamespace(
                candidates=source_map["stage-04/candidates.jsonl"],
                card_artifacts=tuple(
                    item
                    for path, item in source_map.items()
                    if path.startswith("stage-06/cards/")
                    and path.endswith(".json")
                ),
            ),
        )
    )
    return Stage24InputBundle(
        stage23_inputs=stage23_inputs,  # type: ignore[arg-type]
        stage23_publication=publication,
        critique_publication=critique_publication,
        critique=source_map["stage-15/critique.json"],
        critique_manifest=source_map[
            "stage-15/stage15_critique_manifest.json"
        ],
        canonical_manifest=canonical,
        experiment_contract=contract,
        paper=paper,
        verification_report=report,
        citation_plan=_freeze(plan),
        effective_policy={},
        evidence_cards=cards,
        verification=_freeze(verification),
        dataset_origin=dataset_origin,
        model_projection=Stage24ModelProjection(
            writer_model=models["writer_model"],
            citation_assessment_model=models["citation_assessment"],
            generic_support_model=models["generic_support_assessment"],
            resolution_assessment_model=models["resolution_assessment"],
        ),
        entries=entries,
        identity_sha256="0" * 64,
    )


def _renderer_occurrences(
    base: stage23._CapturedUpstream,
    *,
    final_paper: BoundArtifact,
    final_paper_identity: tuple[int, int],
) -> tuple[_RendererOccurrence, ...]:
    stage19_snapshot = base.base.base.snapshot.stage19_snapshot
    binding = stage19_snapshot.binding
    source = stage19_snapshot.stage17
    selection_content = base.stage19_selection.content
    stage19_paper = base.base.semantic_inputs.paper_content
    try:
        replayed = stage19_authority.rerender_stage19_paper(
            source.paper,
            selection_content,
            source_selection=source.selection,
            binding=binding,
        )
    except Exception as exc:
        raise StructuredStage24PublicationError(
            "Stage 24 Stage 19 paper replay failed"
        ) from exc
    if replayed != stage19_paper:
        raise StructuredStage24PublicationError(
            "Stage 24 Stage 19 paper replay mismatch"
        )
    selection = _strict_object(selection_content, "Stage 19 selection")
    raw_sections = selection.get("sections")
    if not isinstance(raw_sections, list) or len(raw_sections) != len(
        SECTION_ORDER
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 Stage 19 selection section count mismatch"
        )
    section_selection: dict[str, Mapping[str, object]] = {}
    for section_id, item in zip(SECTION_ORDER, raw_sections, strict=True):
        if (
            not isinstance(item, Mapping)
            or item.get("section_id") != section_id
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 Stage 19 selection order mismatch"
            )
        section_selection[section_id] = item

    registry = claim_authority.build_scientific_claim_registry(binding)
    registry_source = stage19_snapshot.file(
        "stage-17/scientific_claim_registry.json"
    )
    if (
        registry_source.content != registry.claims_bytes()
        or registry_source.sha256 != registry.claims_sha256
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 held registry canonical bytes mismatch"
        )
    claim_by_id = {claim.claim_id: claim for claim in registry.claims}
    try:
        text = stage19_paper.decode("utf-8")
        document = parse_manuscript(text, strict=True)
    except (UnicodeDecodeError, ManuscriptStructureError) as exc:
        raise StructuredStage24PublicationError(
            "Stage 24 Stage 19 paper is not strict UTF-8"
        ) from exc
    if (
        document.preamble
        + "".join(section.source for section in document.sections)
        != text
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 Stage 19 manuscript round trip mismatch"
        )

    occurrences: list[_RendererOccurrence] = []
    governed: list[str] = []
    cursor = len(document.preamble.encode("utf-8"))
    for section in document.sections:
        section_id = next(
            (
                item
                for item in SECTION_ORDER
                if section.title == item.title()
            ),
            None,
        )
        section_bytes = section.source.encode("utf-8")
        if section_id is not None:
            governed.append(section_id)
            selected = section_selection[section_id]
            ordered = selected.get("ordered_claim_ids")
            if (
                not isinstance(ordered, list)
                or any(type(item) is not str for item in ordered)
            ):
                raise StructuredStage24PublicationError(
                    "Stage 24 ordered claim IDs are invalid"
                )
            projections: list[
                tuple[
                    str,
                    claim_authority.RenderedScientificClaimWithSlots,
                ]
            ] = []
            for claim_id in ordered:
                claim = claim_by_id.get(claim_id)
                if claim is None or claim.section_id != section_id:
                    raise StructuredStage24PublicationError(
                        "Stage 24 selected claim membership mismatch"
                    )
                projection = (
                    claim_authority.render_scientific_claim_with_slots(
                        claim.renderer_template_id,
                        claim.renderer_slot_fact_ids,
                        registry=registry,
                        binding=binding,
                    )
                )
                if projection.rendered.sentence != claim.rendered_sentence:
                    raise StructuredStage24PublicationError(
                        "Stage 24 renderer sentence parity mismatch"
                    )
                projections.append((claim_id, projection))
            rendered = b" ".join(
                projection.rendered.content
                for _claim_id, projection in projections
            )
            expected_body = b"\n" + rendered + b"\n\n"
            if section.body.encode("utf-8") != expected_body:
                raise StructuredStage24PublicationError(
                    "Stage 24 governed section framing mismatch"
                )
            body_start = (
                cursor + len(section.heading_source.encode("utf-8")) + 1
            )
            sentence_cursor = body_start
            for ordinal, (claim_id, projection) in enumerate(projections):
                claim = claim_by_id[claim_id]
                projected_slots = tuple(
                    _RendererSlotOccurrence(
                            section_id=section_id,
                            selection_ordinal=ordinal,
                            claim_id=claim_id,
                            slot_ordinal=slot.slot_ordinal,
                            fact_kind=slot.fact_kind,
                            fact_id=slot.fact_id,
                            stage19_byte_start=sentence_cursor + slot.byte_start,
                            stage19_byte_end=sentence_cursor + slot.byte_end,
                            byte_start=sentence_cursor + slot.byte_start,
                            byte_end=sentence_cursor + slot.byte_end,
                        )
                    for slot in projection.slots
                )
                sentence_end = sentence_cursor + len(
                    projection.rendered.content
                )
                occurrences.append(
                    _RendererOccurrence(
                        generation_binding_sha256=(
                            binding.generation_binding_sha256
                        ),
                        cfs_sha256=binding.cfs_sha256,
                        registry_content=registry_source.content,
                        registry_sha256=registry_source.sha256,
                        section_id=section_id,
                        selection_ordinal=ordinal,
                        claim_id=claim_id,
                        renderer_template_id=claim.renderer_template_id,
                        renderer_slot_fact_ids=claim.renderer_slot_fact_ids,
                        sentence_content=projection.rendered.content,
                        sentence_sha256=projection.rendered.sha256,
                        stage19_paper_sha256=hashlib.sha256(
                            stage19_paper
                        ).hexdigest(),
                        stage19_byte_start=sentence_cursor,
                        stage19_byte_end=sentence_end,
                        stage22_paper_sha256="0" * 64,
                        final_paper_path=final_paper.path,
                        final_paper_sha256=final_paper.sha256,
                        final_paper_identity=final_paper_identity,
                        byte_start=sentence_cursor,
                        byte_end=sentence_end,
                        slots=projected_slots,
                    )
                )
                sentence_cursor = sentence_end
                if ordinal + 1 < len(projections):
                    sentence_cursor += 1
            if sentence_cursor != body_start + len(rendered):
                raise StructuredStage24PublicationError(
                    "Stage 24 governed sentence cursor mismatch"
                )
        cursor += len(section_bytes)
    if tuple(governed) != SECTION_ORDER or cursor != len(stage19_paper):
        raise StructuredStage24PublicationError(
            "Stage 24 governed section lifecycle mismatch"
        )
    transformed = _carry_renderer_occurrences_through_stage22(
        stage19_paper,
        tuple(occurrences),
        quality_outcome=base.base.semantic_inputs.quality_outcome,
        expected=base.paper.content,
    )
    if (
        final_paper.content != base.paper.content
        or final_paper.sha256 != hashlib.sha256(base.paper.content).hexdigest()
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 final held Stage 23 paper differs from Stage 22"
        )
    for occurrence in transformed:
        if (
            final_paper.content[
                occurrence.byte_start : occurrence.byte_end
            ]
            != occurrence.sentence_content
            or hashlib.sha256(occurrence.sentence_content).hexdigest()
            != occurrence.sentence_sha256
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 final renderer sentence identity mismatch"
            )
    return transformed


def _carry_renderer_occurrences_through_stage22(
    paper: bytes,
    occurrences: tuple[_RendererOccurrence, ...],
    *,
    quality_outcome: str,
    expected: bytes,
) -> tuple[_RendererOccurrence, ...]:
    current = paper
    projected = occurrences
    if quality_outcome == "degraded":
        try:
            text = current.decode("utf-8")
            document = parse_manuscript(text, strict=True)
        except (UnicodeDecodeError, ManuscriptStructureError) as exc:
            raise StructuredStage24PublicationError(
                "Stage 24 degradation source is not strict UTF-8"
            ) from exc
        starts: list[tuple[str, int]] = []
        cursor = len(document.preamble.encode("utf-8"))
        for section in document.sections:
            starts.append((section.title, cursor))
            cursor += len(section.source.encode("utf-8"))
        abstract = next(
            (index for index, (title, _offset) in enumerate(starts) if title == "Abstract"),
            None,
        )
        insertion = 0
        if abstract is not None and abstract + 1 < len(starts):
            insertion = starts[abstract + 1][1] - 1
            if insertion < 0 or current[insertion : insertion + 1] != b"\n":
                raise StructuredStage24PublicationError(
                    "Stage 24 degradation insertion boundary mismatch"
                )
        transformed = stage22_semantics._insert_degradation_notice(
            text
        ).encode("utf-8")
        delta = len(transformed) - len(current)
        if delta <= 0:
            raise StructuredStage24PublicationError(
                "Stage 24 degradation insertion is not additive"
            )
        inserted = transformed[insertion : insertion + delta]
        if (
            transformed
            != current[:insertion] + inserted + current[insertion:]
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 degradation insertion replay mismatch"
            )
        shifted_occurrences: list[_RendererOccurrence] = []
        for occurrence in projected:
            if occurrence.byte_start < insertion < occurrence.byte_end:
                raise StructuredStage24PublicationError(
                    "Stage 22 insertion overlaps renderer sentence"
                )
            shifted_occurrences.append(
                replace(
                    occurrence,
                    byte_start=occurrence.byte_start + (
                        delta if occurrence.byte_start >= insertion else 0
                    ),
                    byte_end=occurrence.byte_end + (
                        delta if occurrence.byte_end > insertion else 0
                    ),
                    slots=tuple(
                        replace(
                            slot,
                            byte_start=slot.byte_start + (
                                delta if slot.byte_start >= insertion else 0
                            ),
                            byte_end=slot.byte_end + (
                                delta if slot.byte_end > insertion else 0
                            ),
                        )
                        for slot in occurrence.slots
                    ),
                )
            )
        projected = tuple(shifted_occurrences)
        current = transformed

    try:
        current_text = current.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredStage24PublicationError(
            "Stage 24 figure transform source is not UTF-8"
        ) from exc
    deletion_chars = tuple(
        match.span()
        for match in re.finditer(
            r"(?m)^\s*!\[[^\]]*\]\(charts/[^)]+\)\s*\n?",
            current_text,
        )
    )
    deletions = tuple(
        (
            len(current_text[:start].encode("utf-8")),
            len(current_text[:end].encode("utf-8")),
        )
        for start, end in deletion_chars
    )
    output = bytearray()
    cursor = 0
    for start, end in deletions:
        if start < cursor or not start < end:
            raise StructuredStage24PublicationError(
                "Stage 22 figure deletion intervals are invalid"
            )
        output.extend(current[cursor:start])
        cursor = end
    output.extend(current[cursor:])
    transformed = bytes(output)
    if transformed != stage22_semantics._remove_unavailable_markdown_figures(
        current_text
    ).encode("utf-8"):
        raise StructuredStage24PublicationError(
            "Stage 24 figure deletion replay mismatch"
        )
    shifted: list[_RendererOccurrence] = []
    for occurrence in projected:
        if any(
            occurrence.byte_start < end and start < occurrence.byte_end
            for start, end in deletions
        ):
            raise StructuredStage24PublicationError(
                "Stage 22 figure deletion overlaps renderer sentence"
            )
        removed_before = sum(
            end - start
            for start, end in deletions
            if end <= occurrence.byte_start
        )
        shifted.append(
            replace(
                occurrence,
                byte_start=occurrence.byte_start - removed_before,
                byte_end=occurrence.byte_end - removed_before,
                slots=tuple(
                    replace(
                        slot,
                        byte_start=slot.byte_start - removed_before,
                        byte_end=slot.byte_end - removed_before,
                    )
                    for slot in occurrence.slots
                ),
            )
        )
    if transformed != expected:
        raise StructuredStage24PublicationError(
            "Stage 24 held Stage 22 paper replay mismatch"
        )
    stage22_sha256 = hashlib.sha256(transformed).hexdigest()
    return tuple(
        replace(
            occurrence,
            stage22_paper_sha256=stage22_sha256,
        )
        for occurrence in shifted
    )


def _renderer_numeric_bindings(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    *,
    structured_base: stage23._CapturedUpstream,
    occurrences: Sequence[_RendererOccurrence],
    cfs: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    contract: object,
    cfs_ref: Mapping[str, object],
) -> tuple[
    dict[str, Mapping[str, object]],
    tuple[_RendererSlotOccurrence, ...],
]:
    slots = tuple(
        slot
        for occurrence in occurrences
        for slot in occurrence.slots
    )
    numeric = tuple(item for item in obligations if item.kind == "numeric_token")
    binding = structured_base.base.base.snapshot.stage19_snapshot.binding
    registry = claim_authority.build_scientific_claim_registry(binding)
    fact_by_id = {fact.fact_id: fact for fact in registry.facts}
    grouped: dict[
        tuple[str, int, str],
        list[_RendererSlotOccurrence],
    ] = {}
    for slot in slots:
        grouped.setdefault(
            (slot.section_id, slot.selection_ordinal, slot.claim_id), []
        ).append(slot)

    result: dict[str, Mapping[str, object]] = {}
    consumed: set[str] = set()
    for occurrence_slots in grouped.values():
        by_kind: dict[str, list[_RendererSlotOccurrence]] = {}
        for slot in occurrence_slots:
            by_kind.setdefault(slot.fact_kind, []).append(slot)
        value_slots = by_kind.get("primary_metric_value", [])
        if not value_slots:
            continue
        if len(value_slots) != 1:
            raise StructuredStage24PublicationError(
                "Stage 24 renderer value slot is ambiguous"
            )
        value_slot = value_slots[0]
        matches = tuple(
            item
            for item in numeric
            if item.kind_payload.get("numeric_role") == "claim_numeric"
            and item.byte_start == value_slot.byte_start
            and item.byte_end == value_slot.byte_end
        )
        if len(matches) != 1 or matches[0].obligation_id in consumed:
            raise StructuredStage24PublicationError(
                "Stage 24 renderer value obligation mapping is ambiguous"
            )
        obligation = matches[0]
        consumed.add(obligation.obligation_id)

        required = {
            name: by_kind.get(name, [])
            for name in (
                "primary_metric_key",
                "primary_condition",
                "primary_metric_value",
            )
        }
        if any(len(items) != 1 for items in required.values()):
            raise StructuredStage24PublicationError(
                "Stage 24 renderer primary fact binding is ambiguous"
            )
        metric_slot = required["primary_metric_key"][0]
        metric_fact = fact_by_id[metric_slot.fact_id]
        condition_fact = fact_by_id[required["primary_condition"][0].fact_id]
        value_fact = fact_by_id[value_slot.fact_id]
        metric = metric_fact.object_value
        condition = condition_fact.object_value
        rendered_label = bundle.paper.content[
            metric_slot.byte_start : metric_slot.byte_end
        ].decode("utf-8")
        display_labels = getattr(contract, "metric_display_labels", {}).get(
            metric
        )
        if (
            isinstance(display_labels, (str, bytes))
            or not isinstance(display_labels, Sequence)
            or sum(label == rendered_label for label in display_labels) != 1
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 code-owned metric display label mismatch: "
                f"{rendered_label!r}"
            )

        condition_rows = tuple(
            (index, row)
            for index, row in enumerate(cfs.get("condition_aggregates", ()))
            if isinstance(row, Mapping) and row.get("condition") == condition
        )
        if len(condition_rows) != 1:
            raise StructuredStage24PublicationError(
                "Stage 24 primary CFS condition is ambiguous"
            )
        condition_index, condition_row = condition_rows[0]
        metrics = condition_row.get("metrics")
        summary = metrics.get(metric) if isinstance(metrics, Mapping) else None
        if not isinstance(summary, Mapping) or "mean" not in summary:
            raise StructuredStage24PublicationError(
                "Stage 24 primary CFS metric mean is unavailable"
            )
        pointer = (
            "/derived/canonical_fact_sheet/v1/"
            f"condition_aggregates/{condition_index}/metrics/{metric}/mean"
        )
        candidates = tuple(row for row in records if row.get("pointer") == pointer)
        if len(candidates) != 1:
            raise StructuredStage24PublicationError(
                "Stage 24 primary CFS mean record is ambiguous"
            )
        row = candidates[0]
        try:
            if (
                Decimal(value_fact.object_value) != Decimal(str(summary["mean"]))
                or Decimal(str(row["value"])) != Decimal(value_fact.object_value)
            ):
                raise StructuredStage24PublicationError(
                    "Stage 24 renderer value differs from primary CFS mean"
                )
        except Exception as exc:
            if isinstance(exc, StructuredStage24PublicationError):
                raise
            raise StructuredStage24PublicationError(
                "Stage 24 primary numeric authority is not Decimal"
            ) from exc
        candidate = {
            "metric": metric,
            "display_label": rendered_label,
            "semantic_pointer": pointer,
            "semantic_value_sha256": hashlib.sha256(
                str(row["value"]).encode("utf-8")
            ).hexdigest(),
            "canonical_value": str(row["value"]),
            "unit": getattr(contract, "metric_units", {}).get(metric),
        }
        try:
            resolved = authority.resolve_cfs_numeric_support(
                number_lexeme=obligation.kind_payload["number_lexeme"],
                unit_lexeme=obligation.kind_payload["unit_lexeme"],
                cfs=cfs_ref,
                authority_records=(candidate,),
            )
        except authority.StructuredStage24AuthorityError as exc:
            raise StructuredStage24PublicationError(
                "Stage 24 renderer value did not resolve to its bound CFS mean"
            ) from exc
        result[obligation.obligation_id] = resolved
    return result, slots


def _deterministic_declarative_ids(
    occurrences: Sequence[_RendererOccurrence],
    obligations: Sequence[ClaimObligation],
) -> tuple[str, ...]:
    if len({item.identity_tuple for item in occurrences}) != len(occurrences):
        raise StructuredStage24PublicationError(
            "Stage 24 renderer occurrence identity is duplicated"
        )
    declaratives = tuple(
        item for item in obligations if item.kind == "declarative_sentence"
    )
    obligation_index = {
        item.obligation_id: index for index, item in enumerate(obligations)
    }
    candidates = tuple(
        occurrence
        for occurrence in occurrences
        if obligation_authority._selected_section_class(
            (occurrence.section_id.title(),)
        )
        is not None
    )
    for left, right in zip(candidates, candidates[1:]):
        if left.byte_end > right.byte_start:
            raise StructuredStage24PublicationError(
                "Stage 24 renderer sentence occurrences overlap"
            )
    consumed: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for occurrence in candidates:
        matches = tuple(
            item
            for item in declaratives
            if item.byte_start == occurrence.byte_start
            and item.byte_end == occurrence.byte_end
        )
        if len(matches) != 1:
            raise StructuredStage24PublicationError(
                "Stage 24 deterministic declarative match is ambiguous"
            )
        obligation = matches[0]
        if (
            obligation.obligation_id in consumed
            or obligation.source_sha256 != occurrence.sentence_sha256
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 deterministic declarative consumption mismatch"
            )
        exact = occurrence.sentence_content
        if (
            hashlib.sha256(exact).hexdigest() != obligation.source_sha256
            or occurrence.byte_end - occurrence.byte_start != len(exact)
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 deterministic declarative sentence hash mismatch"
            )
        consumed.add(obligation.obligation_id)
        ordered.append(
            (obligation_index[obligation.obligation_id], obligation.obligation_id)
        )
    if [index for index, _item in ordered] != sorted(
        index for index, _item in ordered
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 deterministic declarative order mismatch"
        )
    return tuple(item for _index, item in ordered)


def _declarative_ledger_state(
    *,
    in_d: bool,
    in_u: bool,
    generic_record: Mapping[str, object] | None,
) -> tuple[str, str | None]:
    if generic_record is not None:
        if not in_u:
            raise StructuredStage24PublicationError(
                "generic declarative record is outside U"
            )
        verdict = generic_record.get("verdict")
        assessment_id = generic_record.get("assessment_id")
        if (
            verdict not in {"supported", "unsupported"}
            or not isinstance(assessment_id, str)
        ):
            raise StructuredStage24PublicationError(
                "generic declarative record is invalid"
            )
        return verdict, assessment_id
    if in_d and not in_u:
        return "supported", None
    return "unsupported", None


def _numeric_support(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    *,
    structured_base: stage23._CapturedUpstream,
    renderer_occurrences: Sequence[_RendererOccurrence],
) -> tuple[
    dict[str, Mapping[str, object]],
    Mapping[str, object],
    tuple[Mapping[str, object], ...],
]:
    evidence = bundle.stage23_inputs.stage22_inputs.evidence
    cfs = build_canonical_fact_sheet(evidence)
    if cfs is None:
        raise StructuredStage24PublicationError(
            "domain-v2 Stage 24 requires exact CFS"
        )
    records = fact_sheet_numeric_authority_records(cfs, view="results")
    contract_raw = yaml.safe_load(bundle.experiment_contract.content.decode("utf-8"))
    if type(contract_raw) is not dict:
        raise StructuredStage24PublicationError("experiment contract is invalid")
    contract = validate_contract_dict(contract_raw)
    cfs_ref = {
        "schema_version": 1,
        "sha256": (
            bundle.stage23_inputs.stage22_inputs.evidence.cfs_sha256
            if hasattr(bundle.stage23_inputs.stage22_inputs.evidence, "cfs_sha256")
            else bundle.stage23_inputs.stage22_inputs.evidence.manifest.get(
                "cfs", {}
            ).get("sha256")
        ),
    }
    if not isinstance(cfs_ref["sha256"], str):
        # The code-owned Stage 20 binding remains the authority for the CFS id.
        cfs_ref["sha256"] = hashlib.sha256(
            authority.global_canonical_json_bytes(cfs)
        ).hexdigest()
    renderer_bindings, renderer_slots = _renderer_numeric_bindings(
        bundle,
        obligations,
        structured_base=structured_base,
        occurrences=renderer_occurrences,
        cfs=cfs,
        records=records,
        contract=contract,
        cfs_ref=cfs_ref,
    )
    renderer_slot_spans = {
        (slot.byte_start, slot.byte_end)
        for slot in renderer_slots
    }
    result: dict[str, Mapping[str, object]] = {}
    for item in obligations:
        if item.kind != "numeric_token":
            continue
        if item.kind_payload["numeric_role"] == "identifier_metadata":
            result[item.obligation_id] = {"status": "not_required"}
            continue
        renderer = renderer_bindings.get(item.obligation_id)
        if renderer is not None:
            result[item.obligation_id] = {
                "status": "supported",
                "authority": renderer,
            }
            continue
        if (item.byte_start, item.byte_end) in renderer_slot_spans:
            result[item.obligation_id] = {"status": "unsupported"}
            continue
        sentence = _containing_sentence(obligations, item)
        label = generic_stage24._metric_label_binding(
            bundle.paper.content[sentence.byte_start : sentence.byte_end],
            sentence_start=sentence.byte_start,
            child=item,
            display_labels=contract.metric_display_labels,
        )
        if label is None:
            result[item.obligation_id] = {"status": "unsupported"}
            continue
        metric, display_label = label
        candidates = tuple(
            {
                "metric": row["metric"],
                "display_label": display_label,
                "semantic_pointer": row["pointer"],
                "semantic_value_sha256": hashlib.sha256(
                    str(row["value"]).encode("utf-8")
                ).hexdigest(),
                "canonical_value": str(row["value"]),
                "unit": contract.metric_units.get(metric),
            }
            for row in records
            if row["metric"] == metric
        )
        try:
            binding = authority.resolve_cfs_numeric_support(
                number_lexeme=item.kind_payload["number_lexeme"],
                unit_lexeme=item.kind_payload["unit_lexeme"],
                cfs=cfs_ref,
                authority_records=candidates,
            )
        except authority.StructuredStage24AuthorityError:
            result[item.obligation_id] = {"status": "unsupported"}
        else:
            result[item.obligation_id] = {
                "status": "supported",
                "authority": binding,
            }
    return result, cfs, tuple(records)


def _citation_spec(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    item,
    *,
    binding: transport.UnderlyingClientBinding,
) -> _AssessmentSpec:
    identity = {
        "schema_version": 2,
        "policy_version": "citation_assessment_v2",
        "assessment_role": "citation_assessment",
        "client_binding_sha256": binding.client_binding_sha256,
        "canonical_manifest_sha256": item.canonical_manifest_sha256,
        "paper_sha256": item.paper_sha256,
        "obligation_id": item.obligation_id,
        "byte_start": item.byte_start,
        "byte_end": item.byte_end,
        "source_sha256": item.source_sha256,
        "instance_id": item.instance_id,
        "cite_key": item.cite_key,
        "stage23_verification_record_sha256": (
            item.stage23_verification_record_sha256
        ),
        "evidence_records": [asdict(row) for row in item.evidence_records],
        "critic_model": binding.model,
    }
    assessment_id = authority.global_identity_sha256(identity)
    obligation = next(
        row for row in obligations if row.obligation_id == item.obligation_id
    )
    container = _containing_sentence(obligations, obligation)
    cards = generic_stage24._cards_by_key(bundle)
    card = cards[item.cite_key][0]
    excerpts = {
        row["excerpt_id"]: row
        for row in card["evidence_excerpts"]
        if isinstance(row, Mapping)
    }
    context = {
        "manuscript_context": bundle.paper.content[
            container.byte_start : container.byte_end
        ].decode("utf-8", errors="strict"),
        "retained_excerpts": [
            {
                "excerpt_id": record.excerpt_id,
                "excerpt_text": excerpts[record.excerpt_id]["excerpt_text"],
            }
            for record in item.evidence_records
        ],
    }
    transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=identity,
        bound_context=context,
    )
    return _AssessmentSpec(
        "citation_assessment",
        identity,
        assessment_id,
        context,
        item.obligation_id,
        None,
    )


def _resolution_specs(
    bundle: Stage24InputBundle,
    *,
    binding: transport.UnderlyingClientBinding,
) -> tuple[_AssessmentSpec, ...]:
    critique = bundle.critique_publication.critique
    findings = () if critique is None else critique["findings"]
    result: list[_AssessmentSpec] = []
    for finding in findings:
        if finding["severity"] not in {"P0", "P1"}:
            continue
        content = {
            key: finding[key]
            for key in (
                "id",
                "severity",
                "category",
                "question",
                "finding",
                "falsification_criterion",
            )
        }
        finding_hash = authority.global_identity_sha256(content)
        identity = {
            "schema_version": 2,
            "policy_version": "resolution_assessment_v2",
            "assessment_role": "resolution_assessment",
            "client_binding_sha256": binding.client_binding_sha256,
            "critique_sha256": bundle.critique.sha256,
            "finding_content_sha256": finding_hash,
            "raw_paper_sha256": bundle.paper.sha256,
            "critic_model": binding.model,
        }
        assessment_id = authority.global_identity_sha256(identity)
        context = {"finding": content, "paper": bundle.paper.text()}
        transport.build_assessment_request(
            role="resolution_assessment",
            binding=binding,
            assessment_input=identity,
            bound_context=context,
        )
        result.append(
            _AssessmentSpec(
                "resolution_assessment",
                identity,
                assessment_id,
                context,
                None,
                finding_hash,
            )
        )
    if len(result) > 12:
        raise StructuredStage24PublicationError(
            "Stage 24 resolution count exceeds 12"
        )
    return tuple(result)


def _derive_generic_specs(
    capture: _CapturedUpstream,
    *,
    citation_records: Mapping[str, Mapping[str, object]],
    citation_bytes: Mapping[str, bytes],
) -> tuple[_AssessmentSpec, ...]:
    result: list[_AssessmentSpec] = []
    binding = capture.role_bindings["generic_support_assessment"]
    for sentence in capture.generic_universe:
        evidence_rows: list[dict[str, object]] = []
        for child in capture.obligations:
            if (
                child.byte_start < sentence.byte_start
                or child.byte_end > sentence.byte_end
            ):
                continue
            numeric = capture.numeric.get(child.obligation_id)
            if numeric is not None and numeric["status"] == "supported":
                numeric_binding = copy.deepcopy(numeric["authority"])
                evidence_rows.append(
                    {
                        "evidence_kind": "numeric_support",
                        "authority": numeric_binding,
                        "semantic_pointer": numeric_binding[
                            "semantic_pointer"
                        ],
                        "semantic_value_sha256": numeric_binding[
                            "semantic_value_sha256"
                        ],
                    }
                )
            citation = citation_records.get(child.obligation_id)
            if citation is not None and citation["verdict"] == "supported":
                assessment_id = citation["assessment_id"]
                content = citation_bytes[f"{assessment_id}.json"]
                evidence_rows.append(
                    {
                        "evidence_kind": "citation_support",
                        "authority": {
                            "authority_kind": "file",
                            "file": {
                                "path": (
                                    "stage-24/citation-assessments/"
                                    f"{assessment_id}.json"
                                ),
                                "sha256": hashlib.sha256(content).hexdigest(),
                                "size": len(content),
                            },
                        },
                        "semantic_pointer": "/verdict",
                        "semantic_value_sha256": hashlib.sha256(
                            b"supported"
                        ).hexdigest(),
                    }
                )
        unique = {
            authority.global_canonical_json_bytes(row): row
            for row in evidence_rows
        }
        rows = [
            unique[key]
            for key in sorted(unique)
        ]
        if not rows:
            continue
        identity = {
            "schema_version": 3,
            "policy_version": "generic_support_structured_v3",
            "assessment_role": "generic_support_assessment",
            "client_binding_sha256": binding.client_binding_sha256,
            "canonical_manifest_sha256": (
                capture.generic_bundle.canonical_manifest.sha256
            ),
            "paper_sha256": capture.generic_bundle.paper.sha256,
            "obligation_id": sentence.obligation_id,
            "byte_start": sentence.byte_start,
            "byte_end": sentence.byte_end,
            "source_sha256": sentence.source_sha256,
            "evidence_records": rows,
            "critic_model": binding.model,
        }
        assessment_id = authority.global_identity_sha256(identity)
        context = {
            "manuscript_sentence": capture.generic_bundle.paper.content[
                sentence.byte_start : sentence.byte_end
            ].decode("utf-8", errors="strict"),
            "evidence_records": copy.deepcopy(rows),
        }
        transport.build_assessment_request(
            role="generic_support_assessment",
            binding=binding,
            assessment_input=identity,
            bound_context=context,
        )
        result.append(
            _AssessmentSpec(
                "generic_support_assessment",
                identity,
                assessment_id,
                context,
                sentence.obligation_id,
                None,
            )
        )
    return tuple(result)


def _run_specs(
    capture: _CapturedUpstream,
    specs: Sequence[_AssessmentSpec],
    *,
    ordinal_start: int,
) -> tuple[
    dict[str, Mapping[str, object]],
    dict[str, bytes],
    int,
]:
    if not specs:
        return {}, {}, ordinal_start
    role = specs[0].role
    if any(item.role != role for item in specs):
        raise StructuredStage24PublicationError("mixed assessment role group")
    binding = capture.role_bindings[role]
    active_client = _activate_registered_client(capture, role, binding)
    records: dict[str, Mapping[str, object]] = {}
    files: dict[str, bytes] = {}
    ordinal = ordinal_start
    for spec in specs:
        active_client = _activate_registered_client(capture, role, binding)
        request = transport.build_assessment_request(
            role=role,
            binding=binding,
            assessment_input=spec.input,
            bound_context=spec.context,
        )
        outcome = transport.execute_assessment(
            request,
            role=role,
            binding=binding,
            active_client=active_client,
            semantic_call_ordinal=ordinal,
        )
        record = _assessment_record(spec, outcome)
        content = authority.global_canonical_json_bytes(record)
        records[
            spec.obligation_id or spec.finding_content_sha256 or spec.assessment_id
        ] = record
        files[f"{spec.assessment_id}.json"] = content
        ordinal += 1
    return records, files, ordinal


def _activate_registered_client(
    capture: _CapturedUpstream,
    role: str,
    binding: transport.UnderlyingClientBinding,
) -> transport.ActiveAssessmentClient:
    attempt_context = capture.attempt_context_object
    attempt_record = (
        None
        if attempt_context is None
        else _ATTEMPT_CONTEXTS.get(attempt_context)
    )
    if (
        attempt_context is None
        or attempt_record is None
        or attempt_record.initial is not capture
        or capture.release_graph_owner._require_active()
        is not capture.writer_epoch_object
        or capture.release_graph_owner._run_identity
        != capture.client_registry.identity_tuple[2]
        or capture.client_registry.identity_tuple[1]
        is not capture.writer_epoch_object
        or capture.client_registry.identity_tuple[3]
        != capture.config_digest
        or semantic_config_sha256(capture.canonical_config_object)
        != capture.config_digest
    ):
        raise StructuredStage24PublicationError(
            "active Stage 24 registry epoch/context mismatch"
        )
    active = capture.client_registry.activate(role=role, binding=binding)
    client = active.client
    registration = _ACTIVE_CLIENTS.get(client)
    if registration is None:
        registration_identity = object()

        def release(_reference) -> None:
            _drop_active_registration(registration_identity)

        registration = _ActiveClientRecord(
            registry_authority_token=(
                capture.client_registry.registry_authority_token
            ),
            registration_identity=registration_identity,
            registry_identity=capture.client_registry.registry_identity,
            release_graph_owner_object=capture.release_graph_owner,
            run_fd_identity=capture.release_graph_owner._run_identity,
            writer_epoch_object=capture.writer_epoch_object,
            stage24_attempt_context_object=attempt_context,
            canonical_config_object=capture.canonical_config_object,
            semantic_config_sha256=capture.config_digest,
            role=role,
            binding=binding,
            client_ref=weakref.ref(client, release),
            active_client_exact_type=transport.Stage24AssessmentTransport,
            underlying_client_binding_sha256=(
                binding.underlying_client_binding_sha256
            ),
            credential_source_identity=client.credential_source_identity,
            credential_identity=client.credential_identity,
            credential_bytes=client.credential_bytes,
        )
        _ACTIVE_CLIENTS[client] = registration
        _ACTIVE_REGISTRATIONS[registration_identity] = registration
    expected_live_identity = (
        capture.client_registry.registry_authority_token,
        registration,
        capture.release_graph_owner,
        capture.release_graph_owner._run_identity,
        capture.writer_epoch_object,
        attempt_context,
        capture.canonical_config_object,
        capture.config_digest,
        client,
        transport.Stage24AssessmentTransport,
        binding.underlying_client_binding_sha256,
        client.credential_source_identity,
        client.credential_identity,
        client.credential_bytes,
    )
    actual_live_identity = (
        registration.registry_authority_token,
        _ACTIVE_REGISTRATIONS.get(registration.registration_identity),
        registration.release_graph_owner_object,
        registration.run_fd_identity,
        registration.writer_epoch_object,
        registration.stage24_attempt_context_object,
        registration.canonical_config_object,
        registration.semantic_config_sha256,
        registration.client_ref(),
        registration.active_client_exact_type,
        registration.underlying_client_binding_sha256,
        registration.credential_source_identity,
        registration.credential_identity,
        registration.credential_bytes,
    )
    if (
        _ACTIVE_REGISTRATIONS.get(registration.registration_identity)
        is not registration
        or actual_live_identity != expected_live_identity
        or registration.registry_identity
        is not capture.client_registry.registry_identity
        or registration.role != role
        or registration.binding is not binding
        or _ACTIVE_CLIENTS.get(client) is not registration
        or type(client) is not transport.Stage24AssessmentTransport
    ):
        raise StructuredStage24PublicationError(
            "active Stage 24 client registration mismatch"
        )
    return active


def _drop_active_registration(registration_identity: object) -> None:
    record = _ACTIVE_REGISTRATIONS.pop(registration_identity, None)
    if record is None:
        return
    client = record.client_ref()
    if client is not None and _ACTIVE_CLIENTS.get(client) is record:
        _ACTIVE_CLIENTS.pop(client, None)


def _drop_active_clients(
    registry: transport.Stage24ClientRegistry,
) -> None:
    active_clients = set(registry.active_clients)
    for identity, record in tuple(_ACTIVE_REGISTRATIONS.items()):
        if (
            record.registry_identity is registry.registry_identity
            or record.client_ref() in active_clients
        ):
            _drop_active_registration(identity)


def _assessment_record(
    spec: _AssessmentSpec,
    outcome: transport.AssessmentOutcome,
) -> dict[str, object]:
    common = {
        "schema_version": spec.input["schema_version"],
        "assessment_id": spec.assessment_id,
        "assessment_input_sha256": spec.assessment_id,
        "critic_model": spec.input["critic_model"],
        "policy_version": spec.input["policy_version"],
    }
    if spec.role == "resolution_assessment":
        return {
            **common,
            "resolution": outcome.decision["resolution"],
            "note": outcome.decision["note"],
            "transport_receipt": copy.deepcopy(outcome.receipt),
        }
    return {
        **common,
        "verdict": outcome.decision["verdict"],
        "reason": outcome.decision["reason"],
        "transport_receipt": copy.deepcopy(outcome.receipt),
    }


def _build_fixed_payloads(
    capture: _CapturedUpstream,
    *,
    citation_records: Mapping[str, Mapping[str, object]],
    generic_records: Mapping[str, Mapping[str, object]],
    resolution_records: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, bytes], bool, str]:
    bundle = capture.generic_bundle
    final_paper_held = next(
        item
        for item in capture.source_files
        if item.logical_path == "stage-23/paper_final_verified.md"
    )
    replayed_occurrences = _renderer_occurrences(
        capture.base,
        final_paper=bundle.paper,
        final_paper_identity=final_paper_held.identity,
    )
    replayed_d = _deterministic_declarative_ids(
        replayed_occurrences,
        capture.obligations,
    )
    if (
        replayed_occurrences != capture.renderer_occurrences
        or replayed_d != capture.deterministic_declarative_ids
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 deterministic declarative independent replay mismatch"
        )
    d_ids = frozenset(replayed_d)
    u_ids = frozenset(
        item.obligation_id for item in capture.generic_universe
    )
    if not set(generic_records).issubset(u_ids):
        raise StructuredStage24PublicationError(
            "Stage 24 generic declarative records exceed U"
        )
    claims_rows: list[dict[str, object]] = []
    for item in capture.obligations:
        support_required = not (
            item.kind == "numeric_token"
            and item.kind_payload["numeric_role"] == "identifier_metadata"
        )
        record_id: object = None
        if not support_required:
            status = "not_required"
        elif item.kind == "numeric_token":
            status = capture.numeric[item.obligation_id]["status"]
        elif item.kind == "citation_instance":
            record = citation_records[item.obligation_id]
            status = record["verdict"]
            record_id = record["assessment_id"]
        elif item.kind == "comparative_sentence":
            children = tuple(
                child
                for child in capture.obligations
                if child.kind == "numeric_token"
                and child.kind_payload.get("numeric_role") == "claim_numeric"
                and child.byte_start >= item.byte_start
                and child.byte_end <= item.byte_end
            )
            bindings = {
                child.obligation_id: capture.numeric[
                    child.obligation_id
                ]["authority"]
                for child in children
                if capture.numeric.get(child.obligation_id, {}).get("status")
                == "supported"
            }
            comparison = authority.evaluate_comparative_truth(
                paper=bundle.paper.content,
                comparison=item,
                numeric_children=children,
                numeric_bindings=bindings,
            )
            status = comparison["status"]
        else:
            record = generic_records.get(item.obligation_id)
            status, record_id = _declarative_ledger_state(
                in_d=item.obligation_id in d_ids,
                in_u=item.obligation_id in u_ids,
                generic_record=record,
            )
        claims_rows.append(
            {
                "obligation_id": item.obligation_id,
                "kind": item.kind,
                "claim_class": {
                    "numeric_token": "quantitative",
                    "citation_instance": "citation",
                    "comparative_sentence": "comparative",
                    "declarative_sentence": "result",
                }[item.kind],
                "byte_start": item.byte_start,
                "byte_end": item.byte_end,
                "source_sha256": item.source_sha256,
                "support_required": support_required,
                "status": status,
                "support_record_id": record_id,
            }
        )
    claims = {
        "schema_version": 1,
        "policy_version": "claim_ledger_v1",
        "claims": claims_rows,
        "counts": {
            "total": len(claims_rows),
            "supported": sum(row["status"] == "supported" for row in claims_rows),
            "unsupported": sum(
                row["status"] == "unsupported" for row in claims_rows
            ),
            "not_required": sum(
                row["status"] == "not_required" for row in claims_rows
            ),
        },
    }
    citation_rows = []
    support_rows = []
    for item in capture.obligations:
        if item.kind != "citation_instance":
            continue
        record = citation_records[item.obligation_id]
        citation_rows.append(
            {
                "instance_id": f"cit:{item.obligation_id}",
                "obligation_id": item.obligation_id,
                "cite_key": item.kind_payload["cite_key"],
                "assessment_id": record["assessment_id"],
                "status": record["verdict"],
            }
        )
        support_rows.append(
            {
                "obligation_id": item.obligation_id,
                "cite_key": item.kind_payload["cite_key"],
                "assessment_id": record["assessment_id"],
                "verdict": record["verdict"],
            }
        )
    citations = {
        "schema_version": 1,
        "policy_version": "citation_mapping_v1",
        "instances": citation_rows,
    }
    violations = list(
        find_dataset_claim_violations(
            bundle.paper.text(), bundle.dataset_origin
        )
    )
    citation_support = {
        "schema_version": 1,
        "policy_version": "citation_support_v2",
        "paper_sha256": bundle.paper.sha256,
        "dataset_origin": bundle.dataset_origin,
        "dataset_claim_violations": violations,
        "instances": support_rows,
        "counts": {
            "total": len(support_rows),
            "supported": sum(
                row["verdict"] == "supported" for row in support_rows
            ),
            "unsupported": sum(
                row["verdict"] == "unsupported" for row in support_rows
            ),
        },
        "valid": all(
            row["verdict"] == "supported" for row in support_rows
        )
        and not violations,
    }
    critique_rows: list[dict[str, object]] = []
    critique = bundle.critique_publication.critique
    for finding in (() if critique is None else critique["findings"]):
        if finding["severity"] not in {"P0", "P1"}:
            continue
        finding_content = {
            key: finding[key]
            for key in (
                "id",
                "severity",
                "category",
                "question",
                "finding",
                "falsification_criterion",
            )
        }
        finding_hash = authority.global_identity_sha256(finding_content)
        record = resolution_records[finding_hash]
        critique_rows.append(
            {
                "finding_id": finding["id"],
                "severity": finding["severity"],
                "finding_content_sha256": finding_hash,
                "assessment_id": record["assessment_id"],
                "resolution": record["resolution"],
            }
        )
    critique_resolution = {
        "schema_version": 1,
        "policy_version": "critique_resolution_v1",
        "critique_path": bundle.critique.path,
        "critique_sha256": bundle.critique.sha256,
        "resolutions": critique_rows,
        "counts": {
            "total": len(critique_rows),
            "fixed": sum(row["resolution"] == "fixed" for row in critique_rows),
            "rebutted": sum(
                row["resolution"] == "rebutted" for row in critique_rows
            ),
            "unresolved": sum(
                row["resolution"] == "unresolved" for row in critique_rows
            ),
        },
    }
    unsupported = claims["counts"]["unsupported"]
    resolution_valid = critique_resolution["counts"]["unresolved"] == 0
    success = (
        unsupported == 0
        and not violations
        and citation_support["valid"] is True
        and resolution_valid
    )
    quality = capture.stage23_manifest["quality_outcome"]
    outcome = "degraded" if quality == "degraded" else "passed"
    truth = {
        "schema_version": 2,
        "policy_version": "stage24_truth_structured_v2",
        "generation_binding_sha256": capture.stage23_manifest[
            "generation_binding_sha256"
        ],
        "cfs": copy.deepcopy(capture.stage23_manifest["cfs"]),
        "source_stage23_manifest": _ref(
            capture.source_map[
                "stage-23/stage23_verification_manifest.json"
            ]
        ),
        "paper": _ref(bundle.paper),
        "quality_outcome": quality,
        "degradation_signal": copy.deepcopy(
            capture.stage23_manifest["degradation_signal"]
        ),
        "claim_scope": capture.stage23_manifest["claim_scope"],
        "stage24_input_bundle_sha256": capture.input_bundle_sha256,
        "unsupported_count": unsupported,
        "dataset_claim_violations": violations,
        "citation_support_valid": citation_support["valid"],
        "critique_resolution_valid": resolution_valid,
        "outcome": outcome,
    }
    fixed_values = (
        [
            item.to_dict()
            for item in capture.obligations
        ],
        claims,
        citations,
        citation_support,
        critique_resolution,
        truth,
    )
    fixed = {
        name: (
            canonical_obligation_inventory_bytes(capture.obligations)
            if index == 0
            else authority.global_canonical_json_bytes(fixed_values[index])
        )
        for index, name in enumerate(_DIRECT_NAMES)
    }
    return fixed, success, outcome


def _build_manifest(
    record: _AttemptRecord,
    *,
    fixed: Mapping[str, bytes],
    assessment_groups: Mapping[str, Mapping[str, bytes]],
    outcome: str,
) -> dict[str, object]:
    capture = record.initial
    source = capture.source_map
    stage23_manifest = capture.stage23_manifest
    direct_rows = [
        {
            "role": role,
            "logical_name": None,
            "path": f"stage-24/{name}",
            "sha256": hashlib.sha256(fixed[name]).hexdigest(),
            "size": len(fixed[name]),
        }
        for role, name in zip(_DIRECT_ROLES, _DIRECT_NAMES, strict=True)
    ]
    assessment_rows: list[dict[str, object]] = []
    group_roles = {
        "citation-assessments": "citation_assessment",
        "generic-support-assessments": "generic_support_assessment",
        "resolution-assessments": "resolution_assessment",
    }
    for directory in _DIRECTORIES:
        for name, content in sorted(assessment_groups[directory].items()):
            logical = name.removesuffix(".json")
            assessment_rows.append(
                {
                    "role": group_roles[directory],
                    "logical_name": logical,
                    "path": f"stage-24/{directory}/{name}",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )
    counts = {
        role: sum(row["role"] == role for row in assessment_rows)
        for role in transport.ROLE_ORDER
    }
    return {
        "schema_version": 2,
        "publication_stage_id": "stage24",
        "publication_mode": authority.PUBLICATION_MODE,
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": _capability_dict(),
        "generation_binding_sha256": stage23_manifest[
            "generation_binding_sha256"
        ],
        "canonical_experiment_evidence": copy.deepcopy(
            stage23_manifest["canonical_experiment_evidence"]
        ),
        "cfs": copy.deepcopy(stage23_manifest["cfs"]),
        "selected_result_manifest": _ref(
            source[
                capture.base.base.semantic_inputs.evidence.selected_result_manifest_path
            ]
        ),
        "source_stage17_manifest": _ref(
            source[
                "stage-17/scientific_claim_authority_manifest.json"
            ]
        ),
        "source_stage19_manifest": copy.deepcopy(
            stage23_manifest["source_stage19_manifest"]
        ),
        "source_stage20_manifest": copy.deepcopy(
            stage23_manifest["source_stage20_manifest"]
        ),
        "source_stage21_manifest": copy.deepcopy(
            stage23_manifest["source_stage21_manifest"]
        ),
        "source_stage22_manifest": copy.deepcopy(
            stage23_manifest["source_stage22_manifest"]
        ),
        "source_stage23_manifest": _ref(
            source[
                "stage-23/stage23_verification_manifest.json"
            ]
        ),
        "source_paper": _ref(
            source["stage-23/paper_final_verified.md"]
        ),
        "source_bibliography": _ref(
            source["stage-23/references_verified.bib"]
        ),
        "source_verification_report": _ref(
            source["stage-23/verification_report.json"]
        ),
        "quality_outcome": stage23_manifest["quality_outcome"],
        "degradation_signal": copy.deepcopy(
            stage23_manifest["degradation_signal"]
        ),
        "claim_scope": stage23_manifest["claim_scope"],
        "stage24_input_bundle_sha256": capture.input_bundle_sha256,
        "numeric_support_policy": {
            "policy_version": "cfs-results-exact-decimal-v1",
            "cfs_view": "results",
            "equality": "exact-decimal",
            "tolerance": False,
            "raw_execution_fallback": False,
        },
        "outcome": outcome,
        "direct_output_count": 6,
        "assessment_counts": counts,
        "output_count": 6 + len(assessment_rows),
        "outputs": [*direct_rows, *assessment_rows],
        "generated": stage23_manifest["generated"],
    }


def _publish_payloads(
    record: _AttemptRecord,
    fixed: Mapping[str, bytes],
    groups: Mapping[str, Mapping[str, bytes]],
) -> None:
    assert record.held_files is not None
    assert record.held_directories is not None
    for name in _DIRECT_NAMES:
        record.held_files[name] = _create_held_file(
            record.namespace._stage_fd,
            name,
            fixed[name],
            logical_path=f"stage-24/{name}",
        )
    for directory in _DIRECTORIES:
        directory_fd, _identity = record.held_directories[directory]
        for name, content in sorted(groups[directory].items()):
            key = f"{directory}/{name}"
            record.held_files[key] = _create_held_file(
                directory_fd,
                name,
                content,
                logical_path=f"stage-24/{key}",
            )


def _verify_payloads(record: _AttemptRecord) -> None:
    assert record.held_files is not None
    assert record.held_directories is not None
    record.namespace.assert_canonical()
    for held in record.held_files.values():
        if _read_held_file(held) != held.expected_bytes:
            raise StructuredStage24PublicationError(
                f"Stage 24 payload changed: {held.logical_path}"
            )
    direct = set(record.namespace.direct_entries())
    expected = {*_DIRECT_NAMES, *_DIRECTORIES}
    if record.manifest is not None:
        expected.add(_MANIFEST_NAME)
    if direct != expected:
        raise StructuredStage24PublicationError(
            "Stage 24 direct namespace mismatch"
        )
    for name, (descriptor, identity) in record.held_directories.items():
        info = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=record.namespace._stage_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != identity
            or (named.st_dev, named.st_ino) != identity
        ):
            raise StructuredStage24PublicationError(
                f"Stage 24 directory identity changed: {name}"
            )


def _verify_complete_publication(record: _AttemptRecord) -> None:
    if record.manifest is None:
        raise StructuredStage24PublicationError("Stage 24 manifest is absent")
    _verify_payloads(record)
    manifest_bytes = _read_held_file(record.manifest)
    if manifest_bytes != record.manifest.expected_bytes:
        raise StructuredStage24PublicationError(
            "Stage 24 manifest changed after publication"
        )
    manifest = authority.strict_json_object(
        manifest_bytes, label="Stage 24 truth manifest"
    )
    authority.validate_stage24_manifest_v2(manifest)
    expected = {
        row["path"]: (row["sha256"], row["size"])
        for row in manifest["outputs"]
    }
    actual = {
        held.logical_path: (
            hashlib.sha256(_read_held_file(held)).hexdigest(),
            len(_read_held_file(held)),
        )
        for held in record.held_files.values()
    }
    if actual != expected:
        raise StructuredStage24PublicationError(
            "Stage 24 manifest output closure mismatch"
        )
    _replay_complete_authority(record, manifest)


def _replay_complete_authority(
    record: _AttemptRecord,
    manifest: Mapping[str, object],
) -> None:
    capture = record.initial
    citation, citation_bytes, ordinal = _replay_assessment_records(
        record,
        capture.citation_inputs,
        ordinal_start=1,
    )
    generic_specs = _derive_generic_specs(
        capture,
        citation_records=citation,
        citation_bytes=citation_bytes,
    )
    generic, generic_bytes, ordinal = _replay_assessment_records(
        record,
        generic_specs,
        ordinal_start=ordinal,
    )
    resolution, resolution_bytes, ordinal = _replay_assessment_records(
        record,
        capture.resolution_inputs,
        ordinal_start=ordinal,
    )
    total = authority.validate_assessment_counts(
        citation=len(capture.citation_inputs),
        generic=len(generic_specs),
        resolution=len(capture.resolution_inputs),
    )
    if ordinal != total + 1:
        raise StructuredStage24PublicationError(
            "replayed semantic call ordinal closure mismatch"
        )
    fixed, success, outcome = _build_fixed_payloads(
        capture,
        citation_records=citation,
        generic_records=generic,
        resolution_records=resolution,
    )
    if not success:
        raise StructuredStage24PublicationError(
            "replayed Stage 24 truth closure is incomplete"
        )
    groups = {
        "citation-assessments": citation_bytes,
        "generic-support-assessments": generic_bytes,
        "resolution-assessments": resolution_bytes,
    }
    assert record.held_files is not None
    expected_keys = set(_DIRECT_NAMES)
    expected_keys.update(
        f"{directory}/{name}"
        for directory, values in groups.items()
        for name in values
    )
    if set(record.held_files) != expected_keys:
        raise StructuredStage24PublicationError(
            "replayed Stage 24 held-file set mismatch"
        )
    for name, content in fixed.items():
        held = record.held_files[name]
        if held.expected_bytes != content or _read_held_file(held) != content:
            raise StructuredStage24PublicationError(
                f"replayed Stage 24 fixed payload mismatch: {name}"
            )
    expected_manifest = _build_manifest(
        record,
        fixed=fixed,
        assessment_groups=groups,
        outcome=outcome,
    )
    expected_manifest_bytes = authority.global_canonical_json_bytes(
        expected_manifest
    )
    if (
        dict(manifest) != expected_manifest
        or record.manifest is None
        or record.manifest.expected_bytes != expected_manifest_bytes
        or _read_held_file(record.manifest) != expected_manifest_bytes
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 manifest independent replay mismatch"
        )


def _replay_assessment_records(
    record: _AttemptRecord,
    specs: Sequence[_AssessmentSpec],
    *,
    ordinal_start: int,
) -> tuple[
    dict[str, Mapping[str, object]],
    dict[str, bytes],
    int,
]:
    if not specs:
        return {}, {}, ordinal_start
    role = specs[0].role
    if any(spec.role != role for spec in specs):
        raise StructuredStage24PublicationError(
            "replayed assessment group mixes roles"
        )
    directory = {
        "citation_assessment": "citation-assessments",
        "generic_support_assessment": "generic-support-assessments",
        "resolution_assessment": "resolution-assessments",
    }[role]
    binding = record.initial.role_bindings[role]
    records: dict[str, Mapping[str, object]] = {}
    files: dict[str, bytes] = {}
    ordinal = ordinal_start
    assert record.held_files is not None
    for spec in specs:
        name = f"{spec.assessment_id}.json"
        held = record.held_files.get(f"{directory}/{name}")
        if held is None:
            raise StructuredStage24PublicationError(
                "replayed assessment file is missing"
            )
        content = _read_held_file(held)
        if content != held.expected_bytes:
            raise StructuredStage24PublicationError(
                "assessment bytes changed after publication"
            )
        value = authority.strict_json_object(
            content, label=f"Stage 24 {role} record"
        )
        if role == "resolution_assessment":
            decision = {
                "resolution": value.get("resolution"),
                "note": value.get("note"),
            }
        else:
            decision = {
                "verdict": value.get("verdict"),
                "reason": value.get("reason"),
            }
        transport.validate_persisted_decision(role, decision)
        request = transport.build_assessment_request(
            role=role,
            binding=binding,
            assessment_input=spec.input,
            bound_context=spec.context,
        )
        receipt = value.get("transport_receipt")
        if type(receipt) is not dict:
            raise StructuredStage24PublicationError(
                "assessment transport receipt is missing"
            )
        authority.validate_transport_receipt(
            receipt,
            expected_role=role,
            expected_client_binding_sha256=(
                binding.client_binding_sha256 or ""
            ),
        )
        expected_receipt = transport.build_transport_receipt(
            role=role,
            client_binding_sha256=binding.client_binding_sha256 or "",
            semantic_call_ordinal=ordinal,
            outbound_attempts=receipt["outbound_attempts"],
            request_sha256=request.request_sha256,
            response_sha256=receipt["response_sha256"],
            origin=binding.origin,
            target=binding.target,
            decision=decision,
        )
        if receipt != expected_receipt:
            raise StructuredStage24PublicationError(
                "assessment transport receipt replay mismatch"
            )
        expected_record = _assessment_record(
            spec,
            transport.AssessmentOutcome(decision, receipt),
        )
        if value != expected_record:
            raise StructuredStage24PublicationError(
                "assessment record independent replay mismatch"
            )
        key = (
            spec.obligation_id
            or spec.finding_content_sha256
            or spec.assessment_id
        )
        records[key] = value
        files[name] = content
        ordinal += 1
    return records, files, ordinal


def _capture_final_state(record: _AttemptRecord) -> tuple[object, ...]:
    assert record.held_files is not None
    assert record.manifest is not None
    return (
        record.namespace._run_identity,
        record.namespace._stage_identity,
        tuple(record.namespace.direct_entries()),
        tuple(
            (
                key,
                held.identity,
                hashlib.sha256(_read_held_file(held)).hexdigest(),
                len(_read_held_file(held)),
            )
            for key, held in sorted(record.held_files.items())
        ),
        (
            record.manifest.identity,
            hashlib.sha256(_read_held_file(record.manifest)).hexdigest(),
            len(_read_held_file(record.manifest)),
        ),
        record.initial.identity_tuple,
    )


def _verify_source_snapshot(capture: _CapturedUpstream) -> None:
    for item in capture.source_files:
        if _read_held_file(item) != item.expected_bytes:
            raise StructuredStage24PublicationError(
                f"Stage 24 source changed: {item.logical_path}"
            )
    capture.base.stage22_namespace.assert_canonical()
    capture.base.base.stage21_namespace.assert_canonical()
    capture.base.base.base.stage20_namespace.assert_canonical()
    capture.stage23_namespace.assert_canonical()
    capture.stage15_namespace.assert_canonical()


def _expected_source_snapshots(
    base: stage23._CapturedUpstream,
) -> dict[str, object]:
    values = (
        *base.base.base.snapshot.sources,
        *base.base.stage21_files,
        *base.base.extra_sources,
        base.stage22_manifest,
        *base.stage22_outputs,
    )
    result: dict[str, object] = {}
    for item in values:
        previous = result.setdefault(item.path, item)
        if previous != item:
            raise StructuredStage24PublicationError(
                f"upstream source path is ambiguous: {item.path}"
            )
    return result


def _verify_stage23_outputs(
    source: Mapping[str, BoundArtifact],
    manifest: Mapping[str, object],
) -> None:
    for row in manifest["outputs"]:
        item = source.get(row["path"])
        if (
            item is None
            or item.sha256 != row["sha256"]
            or len(item.content) != row["size"]
        ):
            raise StructuredStage24PublicationError(
                "Stage 23 output FileRef mismatch"
            )


def _hold_run_file(writer: ReleaseGraphLock, path: str) -> _HeldFile:
    parts = path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise StructuredStage24PublicationError("unsafe source path")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent = writer.duplicate_run_fd()
    opened = [parent]
    try:
        for part in parts[:-1]:
            parent = os.open(part, directory_flags, dir_fd=parent)
            opened.append(parent)
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(descriptor)
            raise StructuredStage24PublicationError(
                f"source is not one unaliased regular file: {path}"
            )
        content = _read_descriptor(descriptor)
        return _HeldFile(
            os.dup(parent),
            parts[-1],
            descriptor,
            (info.st_dev, info.st_ino),
            path,
            content,
        )
    finally:
        for item in reversed(opened):
            os.close(item)


def _hold_existing_stage24_file(
    parent_fd: int,
    name: str,
    *,
    logical_path: str,
) -> _HeldFile:
    if "/" in name or name in {"", ".", ".."}:
        raise StructuredStage24PublicationError(
            "unsafe current Stage 24 leaf name"
        )
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_NONBLOCK
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or identity != (named.st_dev, named.st_ino)
        ):
            raise StructuredStage24PublicationError(
                f"current Stage 24 file mismatch: {logical_path}"
            )
        content = _read_descriptor(descriptor)
        return _HeldFile(
            parent_fd,
            name,
            descriptor,
            identity,
            logical_path,
            content,
        )
    except Exception:
        os.close(descriptor)
        raise


def _create_held_file(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    logical_path: str,
) -> _HeldFile:
    if "/" in name or name in {"", ".", ".."}:
        raise StructuredStage24PublicationError("unsafe Stage 24 leaf name")
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
                raise OSError("short Stage 24 write")
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
            raise StructuredStage24PublicationError(
                f"created file identity mismatch: {logical_path}"
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
        raise StructuredStage24PublicationError(
            f"held file identity changed: {item.logical_path}"
        )
    content = _read_descriptor(item.descriptor)
    after = os.fstat(item.descriptor)
    if (
        len(content) != after.st_size
        or (after.st_dev, after.st_ino) != item.identity
    ):
        raise StructuredStage24PublicationError(
            f"held file changed during read: {item.logical_path}"
        )
    return content


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _acquire_stage24_namespace(
    writer: ReleaseGraphLock,
) -> BoundOutputNamespace:
    try:
        namespace = writer.open_stage_namespace("stage-24", create_stage=True)
        require_namespace_owned_by_epoch(namespace, writer, "stage-24")
        return namespace
    except Exception as exc:
        raise StructuredStage24PublicationError(
            "Stage 24 namespace binding failed"
        ) from exc


def _withdraw_namespace(namespace: BoundOutputNamespace) -> None:
    entries = tuple(namespace.direct_entries())
    allowed = {*_DIRECT_NAMES, *_DIRECTORIES, _MANIFEST_NAME}
    if not set(entries).issubset(allowed):
        raise StructuredStage24PublicationError(
            "Stage 24 namespace contains an unknown collision"
        )
    if _MANIFEST_NAME in entries:
        os.unlink(_MANIFEST_NAME, dir_fd=namespace._stage_fd)
    for name in _DIRECT_NAMES:
        try:
            info = os.stat(name, dir_fd=namespace._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StructuredStage24PublicationError(
                f"Stage 24 stale file collision: {name}"
            )
        os.unlink(name, dir_fd=namespace._stage_fd)
    for name in _DIRECTORIES:
        try:
            info = os.stat(name, dir_fd=namespace._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise StructuredStage24PublicationError(
                f"Stage 24 stale directory collision: {name}"
            )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=namespace._stage_fd,
        )
        try:
            for leaf in os.listdir(descriptor):
                leaf_info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not leaf.endswith(".json")
                    or not stat.S_ISREG(leaf_info.st_mode)
                    or leaf_info.st_nlink != 1
                ):
                    raise StructuredStage24PublicationError(
                        f"Stage 24 stale assessment collision: {name}/{leaf}"
                    )
                os.unlink(leaf, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=namespace._stage_fd)
    if namespace.direct_entries():
        raise StructuredStage24PublicationError(
            "Stage 24 namespace invalidation incomplete"
        )


def _create_assessment_directories(
    namespace: BoundOutputNamespace,
) -> dict[str, tuple[int, tuple[int, int]]]:
    result: dict[str, tuple[int, tuple[int, int]]] = {}
    try:
        for name in _DIRECTORIES:
            os.mkdir(name, 0o700, dir_fd=namespace._stage_fd)
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=namespace._stage_fd,
            )
            info = os.fstat(descriptor)
            result[name] = (descriptor, (info.st_dev, info.st_ino))
        return result
    except Exception:
        for descriptor, _identity in result.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _cleanup_attempt_outputs(record: _AttemptRecord) -> tuple[str, ...]:
    errors: list[str] = []
    ordered = (
        (record.manifest,)
        + tuple((record.held_files or {}).values())
    )
    for held in ordered:
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
    for name, value in (record.held_directories or {}).items():
        descriptor, identity = value
        try:
            named = os.stat(
                name,
                dir_fd=record.namespace._stage_fd,
                follow_symlinks=False,
            )
            if (named.st_dev, named.st_ino) != identity:
                errors.append(f"stage-24/{name}: identity collision")
            elif os.listdir(descriptor):
                errors.append(f"stage-24/{name}: directory remains nonempty")
            else:
                os.rmdir(name, dir_fd=record.namespace._stage_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"stage-24/{name}: {exc}")
    try:
        if record.namespace.direct_entries():
            errors.append("stage-24: owned namespace remains nonempty")
        else:
            named = os.stat(
                _STAGE_NAME,
                dir_fd=record.namespace._run_fd,
                follow_symlinks=False,
            )
            if (named.st_dev, named.st_ino) != record.namespace._stage_identity:
                errors.append("stage-24: canonical name identity collision")
            else:
                os.rmdir(_STAGE_NAME, dir_fd=record.namespace._run_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"stage-24: {exc}")
    return tuple(errors)


def _close_attempt(record: _AttemptRecord) -> None:
    descriptors = [
        item.descriptor for item in (record.held_files or {}).values()
    ]
    if record.manifest is not None:
        descriptors.append(record.manifest.descriptor)
    descriptors.extend(
        descriptor
        for descriptor, _identity in (record.held_directories or {}).values()
    )
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass
    record.namespace.close()
    record.initial.close()


def _validate_provisional(
    record: _AttemptRecord,
    provisional: ProvisionalStage24Result,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> None:
    if (
        provisional.context not in _ATTEMPT_CONTEXTS
        or provisional.context._token is not record.token
        or provisional.status is not StageStatus.DONE
        or provisional.artifacts != STRUCTURED_STAGE24_ARTIFACTS
        or provisional.evidence_refs != STRUCTURED_STAGE24_EVIDENCE_REFS
        or artifacts != STRUCTURED_STAGE24_ARTIFACTS
        or evidence_refs != STRUCTURED_STAGE24_EVIDENCE_REFS
        or provisional.degraded != (record.outcome == "degraded")
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 provisional result mismatch"
        )


def _require_pre_context(
    lease: ReleaseGraphLock,
    context: Stage24PreAdmissionContext,
) -> _PreRecord:
    writer = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if type(context) is not Stage24PreAdmissionContext:
        raise StructuredStage24PublicationError(
            "Stage 24 pre-admission context type mismatch"
        )
    record = _PRE_CONTEXTS.get(context)
    if (
        record is None
        or context._token is not record.token
        or context._writer_owner is not writer
        or record.owner is not writer
        or context._run_identity != writer._run_identity
        or context.structured_capability_snapshot != (1, 1, 1, 0)
        or context.source_identity != record.capture.identity_tuple
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 pre-admission context identity mismatch"
        )
    return record


def _require_attempt_context(
    lease: ReleaseGraphLock,
    context: Stage24AttemptContext,
) -> _AttemptRecord:
    writer = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if type(context) is not Stage24AttemptContext:
        raise StructuredStage24PublicationError(
            "Stage 24 attempt context type mismatch"
        )
    record = _ATTEMPT_CONTEXTS.get(context)
    if (
        record is None
        or context._token is not record.token
        or context._writer_owner is not writer
        or record.owner is not writer
        or context._run_identity != writer._run_identity
        or context._stage_identity != record.namespace._stage_identity
        or context.structured_capability_snapshot != (1, 1, 1, 0)
    ):
        raise StructuredStage24PublicationError(
            "Stage 24 attempt context identity mismatch"
        )
    record.namespace.assert_canonical()
    return record


def _model_projection(config) -> dict[str, str]:
    values = {
        "writer_model": config.llm.primary_model,
        "citation_assessment": config.paper_revision.critic_model,
        "generic_support_assessment": config.paper_revision.critic_model,
        "resolution_assessment": config.llm.critic_model,
    }
    for value in values.values():
        if (
            type(value) is not str
            or value != value.strip()
            or not 1 <= len(value.encode("ascii", errors="strict")) <= 256
            or any(
                byte < 0x21 or byte > 0x7E
                for byte in value.encode("ascii")
            )
        ):
            raise StructuredStage24PublicationError(
                "Stage 24 model projection is invalid"
            )
    if any(
        values[role] == values["writer_model"]
        for role in transport.ROLE_ORDER
    ):
        raise StructuredStage24PublicationError(
            "writer and Stage 24 critics are not isolated"
        )
    return values


def _containing_sentence(
    obligations: Sequence[ClaimObligation],
    child: ClaimObligation,
) -> ClaimObligation:
    candidates = tuple(
        item
        for item in obligations
        if item.kind in {"comparative_sentence", "declarative_sentence"}
        and item.byte_start <= child.byte_start
        and item.byte_end >= child.byte_end
    )
    if not candidates:
        return child
    return min(
        candidates,
        key=lambda item: (
            item.byte_end - item.byte_start,
            item.byte_start,
            item.byte_end,
            authority.KIND_RANK[item.kind],
            item.obligation_id,
        ),
    )


def _strict_object(content: bytes, label: str) -> dict[str, object]:
    return authority.strict_json_object(content, label=label)


def _ref(item: BoundArtifact) -> dict[str, object]:
    return {
        "path": item.path,
        "sha256": item.sha256,
        "size": len(item.content),
    }


def _freeze(value):
    if isinstance(value, Mapping):
        return {key: _freeze(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


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
        raise StructuredStage24PublicationError(
            "private structured Stage 24 requires exact capability 1110"
        )
