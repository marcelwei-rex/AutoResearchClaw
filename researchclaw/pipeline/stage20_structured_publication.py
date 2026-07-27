"""Held-fd independent replay and publication for structured Stage 20."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import weakref
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from researchclaw.llm.client import LLMClient
from researchclaw.literature.citation_policy import parse_config_snapshot_text
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import canonical_decimal
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_active_writer_invalidation_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.stage19_structured_publication import (
    RichFileSnapshot,
    StructuredStage19SnapshotA,
)
from researchclaw.pipeline import stage19_structured_publication as stage19
from researchclaw.pipeline.stage20_publication import (
    Stage20FabricationState,
    reconstruct_stage20_fabrication_state,
)
from researchclaw.pipeline.stage20_structured_authority import (
    build_degradation_signal,
    build_fabrication_flags,
    build_quality_gate_manifest,
    build_quality_report,
    derive_outcome,
    replay_degradation_signal,
    replay_fabrication_flags,
    replay_quality_gate_manifest,
    replay_quality_report,
)
from researchclaw.pipeline.stage20_structured_transport import (
    TransportDiagnostic,
    execute_bounded_quality_calls,
)
from researchclaw.pipeline.stage_impls._review_publish import _utcnow_iso
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus


STRUCTURED_STAGE20_OUTPUTS = (
    "quality_report.json",
    "fabrication_flags.json",
)
STRUCTURED_STAGE20_MANIFEST = "quality_gate_manifest.json"
STRUCTURED_STAGE20_STAGE_TEMPS = (
    "quality_report.json.tmp",
    "fabrication_flags.json.tmp",
    "quality_gate_manifest.json.tmp",
)
STRUCTURED_STAGE20_ROOT_TEMP = "degradation_signal.json.tmp"
STRUCTURED_STAGE20_ARTIFACTS = (
    *STRUCTURED_STAGE20_OUTPUTS,
    STRUCTURED_STAGE20_MANIFEST,
)
STRUCTURED_STAGE20_EVIDENCE_REFS = tuple(
    f"stage-20/{name}" for name in STRUCTURED_STAGE20_ARTIFACTS
)
STRUCTURED_STAGE20_DIAGNOSTIC = "quality_gate_llm_diagnostics.json"
STRUCTURED_STAGE20_DIAGNOSTIC_TEMP = (
    "quality_gate_llm_diagnostics.json.tmp"
)
STRUCTURED_STAGE20_SIGNAL = "degradation_signal.json"
_STAGE19_NAMES = (
    *stage19.STRUCTURED_STAGE19_OUTPUTS,
    stage19.STRUCTURED_STAGE19_MANIFEST,
)
_CONTEXT_CONSTRUCTION_AUTHORITY = object()
_CONTEXT_ISSUANCE_AUTHORITY = object()


class StructuredStage20PublicationError(RuntimeError):
    """Structured Stage 20 admission, replay, or publication failed."""


@dataclass(frozen=True)
class StructuredStage20SnapshotA:
    sources: tuple[RichFileSnapshot, ...]
    stage19_snapshot: StructuredStage19SnapshotA
    stage19_files: tuple[RichFileSnapshot, ...]
    stage19_manifest: Mapping[str, object]
    canonical_config: object
    fabrication_state: Stage20FabricationState
    root_parent_fd: int
    root_parent_path: str
    root_parent_identity: tuple[int, int]
    run_name: str
    run_identity: tuple[int, int]
    stage_identity: tuple[int, int]

    def source(self, path: str) -> RichFileSnapshot:
        matches = tuple(item for item in self.sources if item.path == path)
        if len(matches) != 1:
            raise StructuredStage20PublicationError(
                f"Snapshot A missing exact source: {path}"
            )
        return matches[0]

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return tuple(item.identity_tuple() for item in self.sources)


@dataclass(frozen=True)
class StructuredStage20FinalSnapshot:
    files: tuple[RichFileSnapshot, ...]
    signal: RichFileSnapshot | None
    namespace_identity: tuple[
        tuple[int, int], tuple[int, int], tuple[str, ...]
    ]
    root_parent_identity: tuple[int, int]
    run_identity: tuple[int, int]


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class StructuredStage20VerifiedContext:
    _construction_token: object
    _writer_owner: object
    _run_path: str
    _run_identity: tuple[int, int]
    _stage_identity: tuple[int, int]
    generation_binding_sha256: str
    cfs_sha256: str
    stage19_manifest_sha256: str
    snapshot_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority: object,
        construction_token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        stage_identity: tuple[int, int],
        generation_binding_sha256: str,
        cfs_sha256: str,
        stage19_manifest_sha256: str,
        snapshot_identity: tuple[object, ...],
    ) -> None:
        if authority is not _CONTEXT_CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 20 context construction is private")
        object.__setattr__(self, "_construction_token", construction_token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(self, "cfs_sha256", cfs_sha256)
        object.__setattr__(
            self, "stage19_manifest_sha256", stage19_manifest_sha256
        )
        object.__setattr__(self, "snapshot_identity", snapshot_identity)


@dataclass
class _IssuedContext:
    token: object
    owner: ReleaseGraphLock
    run_path: str
    run_identity: tuple[int, int]
    stage_identity: tuple[int, int]
    generation_binding_sha256: str
    cfs_sha256: str
    stage19_manifest_sha256: str
    snapshot_identity: tuple[object, ...]
    phase: str = "issued"


@dataclass(frozen=True)
class _PublishedContext:
    snapshot_a: StructuredStage20SnapshotA
    final_snapshot: StructuredStage20FinalSnapshot
    verified_context: StructuredStage20VerifiedContext
    expected_report: bytes
    expected_flags: bytes
    expected_manifest: bytes
    expected_signal: bytes | None
    expected_stage_identities: dict[str, tuple[int, int]]
    expected_signal_identity: tuple[int, int] | None
    decision: str
    held_namespace: BoundOutputNamespace


@dataclass(frozen=True)
class _HeldTemp:
    directory_fd: int
    name: str
    descriptor: int
    identity: tuple[int, int]
    expected_bytes: bytes


@dataclass(frozen=True)
class _VerifiedTemp:
    held: _HeldTemp
    verified_bytes: bytes


_ISSUED_CONTEXTS: weakref.WeakKeyDictionary[
    StructuredStage20VerifiedContext, _IssuedContext
] = weakref.WeakKeyDictionary()


def _capture_snapshot_a_and_issue_context(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
) -> tuple[StructuredStage20SnapshotA, StructuredStage20VerifiedContext]:
    """Capture and independently replay the sole structured Stage 19 source."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    require_namespace_owned_by_epoch(namespace, writer, "stage-20")  # type: ignore[arg-type]
    _clear_published_context(owner)
    admission_context = _issue_admission_context(
        writer, namespace=namespace
    )
    _require_verified_context(
        writer,
        admission_context,
        namespace=namespace,
        allowed_phases=("admitted",),
    )
    _mark_attempt(owner, namespace)
    cleanup_errors = _cleanup_owned_namespace(owner, namespace)
    if cleanup_errors:
        _ISSUED_CONTEXTS.pop(admission_context, None)
        raise StructuredStage20PublicationError(
            "structured Stage 20 admission cleanup failed: "
            + "; ".join(cleanup_errors)
        )
    parent_fd = -1
    try:
        parent_fd = _open_root_parent(owner)
        parent_info = os.fstat(parent_fd)
        root_parent_path = str(owner.run_dir.parent.absolute())
        root_parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _verify_root_parent(
            parent_fd,
            root_parent_path=root_parent_path,
            root_parent_identity=root_parent_identity,
            run_name=owner.run_dir.name,
            run_identity=owner._run_identity,
        )
        with writer.open_stage_namespace("stage-19") as stage19_namespace:
            require_namespace_owned_by_epoch(
                stage19_namespace, writer, "stage-19"
            )
            if set(stage19_namespace.direct_entries()) != set(_STAGE19_NAMES):
                raise StructuredStage20PublicationError(
                    "Stage 19 exact six-file namespace mismatch"
                )
            base, stage19_context = stage19._capture_snapshot_a_and_issue_context(
                writer, namespace=stage19_namespace
            )
            stage19._ISSUED_CONTEXTS.pop(stage19_context, None)
            stage19_files = tuple(
                stage19._read_rich_run_snapshot(writer, f"stage-19/{name}")
                for name in _STAGE19_NAMES
            )
        output_map = {
            item.path.removeprefix("stage-19/"): item.content
            for item in stage19_files
            if item.path
            != f"stage-19/{stage19.STRUCTURED_STAGE19_MANIFEST}"
        }
        stage19._replay_outputs(output_map, base)
        expected_manifest = stage19._build_manifest(output_map, base)
        manifest_file = next(
            item
            for item in stage19_files
            if item.path
            == f"stage-19/{stage19.STRUCTURED_STAGE19_MANIFEST}"
        )
        manifest = stage19.replay_stage19_manifest(
            manifest_file.content, expected=expected_manifest
        )
        sources = (*base.files, *stage19_files)
        if len({item.path for item in sources}) != len(sources):
            raise StructuredStage20PublicationError(
                "Stage 20 source closure contains duplicate paths"
            )
        canonical_config = parse_config_snapshot_text(
            base.binding.evidence.run_config_bytes.decode("utf-8"),
            project_root=owner.run_dir,
            label="structured Stage 20 canonical config",
        )
        fabrication_state = reconstruct_stage20_fabrication_state(
            base.binding.evidence, canonical_config
        )
        snapshot = StructuredStage20SnapshotA(
            tuple(sources),
            base,
            stage19_files,
            manifest,
            canonical_config,
            fabrication_state,
            parent_fd,
            root_parent_path,
            root_parent_identity,
            owner.run_dir.name,
            owner._run_identity,
            getattr(namespace, "_stage_identity"),
        )
        _verify_source_fixpoint(writer, snapshot)
        context = _issue_verified_context(
            writer, snapshot=snapshot, authority=_CONTEXT_ISSUANCE_AUTHORITY
        )
        _ISSUED_CONTEXTS.pop(admission_context, None)
        return snapshot, context
    except Exception as exc:
        _ISSUED_CONTEXTS.pop(admission_context, None)
        if parent_fd >= 0:
            os.close(parent_fd)
        if isinstance(exc, StructuredStage20PublicationError):
            raise
        raise StructuredStage20PublicationError(
            f"structured Stage 19 independent replay failed: {exc}"
        ) from exc


def _issue_admission_context(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
) -> StructuredStage20VerifiedContext:
    """Issue the live non-forgeable pre-I/O admission context."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    require_namespace_owned_by_epoch(namespace, writer, "stage-20")  # type: ignore[arg-type]
    stage_identity = getattr(namespace, "_stage_identity", None)
    if not isinstance(stage_identity, tuple) or len(stage_identity) != 2:
        raise StructuredStage20PublicationError(
            "structured Stage 20 admission namespace identity is invalid"
        )
    token = object()
    placeholder = "0" * 64
    context = StructuredStage20VerifiedContext(
        authority=_CONTEXT_CONSTRUCTION_AUTHORITY,
        construction_token=token,
        writer_owner=owner,
        run_path=str(owner.run_dir.absolute()),
        run_identity=owner._run_identity,
        stage_identity=stage_identity,
        generation_binding_sha256=placeholder,
        cfs_sha256=placeholder,
        stage19_manifest_sha256=placeholder,
        snapshot_identity=(),
    )
    _ISSUED_CONTEXTS[context] = _IssuedContext(
        token,
        owner,
        str(owner.run_dir.absolute()),
        owner._run_identity,
        stage_identity,
        placeholder,
        placeholder,
        placeholder,
        (),
        phase="admitted",
    )
    return context


def _open_root_parent(owner: ReleaseGraphLock) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(owner.run_dir.parent, flags)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise StructuredStage20PublicationError(
            "structured Stage 20 root parent is unsafe"
        )
    return descriptor


def _issue_verified_context(
    lease: ReleaseGraphLock,
    *,
    snapshot: StructuredStage20SnapshotA,
    authority: object,
) -> StructuredStage20VerifiedContext:
    if authority is not _CONTEXT_ISSUANCE_AUTHORITY:
        raise TypeError("structured Stage 20 context issuance is private")
    owner = require_active_writer_epoch(lease.run_dir, lease)._require_active()
    if (
        snapshot.run_identity != owner._run_identity
        or snapshot.stage_identity is None
        or not snapshot.sources
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 context requires verified Snapshot A"
        )
    token = object()
    manifest_digest = snapshot.source(
        "stage-19/scientific_claim_authority_manifest.json"
    ).sha256
    context = StructuredStage20VerifiedContext(
        authority=_CONTEXT_CONSTRUCTION_AUTHORITY,
        construction_token=token,
        writer_owner=owner,
        run_path=str(owner.run_dir.absolute()),
        run_identity=owner._run_identity,
        stage_identity=snapshot.stage_identity,
        generation_binding_sha256=(
            snapshot.stage19_snapshot.binding.generation_binding_sha256
        ),
        cfs_sha256=snapshot.stage19_snapshot.binding.cfs_sha256,
        stage19_manifest_sha256=manifest_digest,
        snapshot_identity=snapshot.identity_tuple,
    )
    _ISSUED_CONTEXTS[context] = _IssuedContext(
        token,
        owner,
        str(owner.run_dir.absolute()),
        owner._run_identity,
        snapshot.stage_identity,
        snapshot.stage19_snapshot.binding.generation_binding_sha256,
        snapshot.stage19_snapshot.binding.cfs_sha256,
        manifest_digest,
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
    """Reject forged/stale private authority before source or output reads."""

    if not isinstance(context, StructuredStage20VerifiedContext):
        raise StructuredStage20PublicationError(
            "structured Stage 20 context was not privately constructed"
        )
    issued = _ISSUED_CONTEXTS.get(context)
    if (
        issued is None
        or issued.phase not in allowed_phases
        or issued.token is not context._construction_token
        or issued.snapshot_identity != context.snapshot_identity
        or issued.generation_binding_sha256
        != context.generation_binding_sha256
        or issued.cfs_sha256 != context.cfs_sha256
        or issued.stage19_manifest_sha256 != context.stage19_manifest_sha256
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 context was not issued"
        )
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    if owner is not issued.owner or context._writer_owner is not owner:
        raise StructuredStage20PublicationError(
            "structured Stage 20 context writer mismatch"
        )
    if (
        context._run_path != str(owner.run_dir.absolute())
        or issued.run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or issued.run_identity != owner._run_identity
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 context run mismatch"
        )
    require_namespace_owned_by_epoch(namespace, writer, "stage-20")  # type: ignore[arg-type]
    if (
        getattr(namespace, "_stage_identity", None) != context._stage_identity
        or issued.stage_identity != context._stage_identity
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 context namespace mismatch"
        )
    return owner


def _execute_structured_stage20_from_context(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
    snapshot: StructuredStage20SnapshotA,
    context: StructuredStage20VerifiedContext,
    llm: LLMClient | None,
) -> StageResult:
    """Consume one verified context and publish exact Stage 20 authority."""

    try:
        owner = _require_verified_context(
            lease, context, namespace=namespace, allowed_phases=("issued",)
        )
    except Exception as exc:
        issued = (
            _ISSUED_CONTEXTS.get(context)
            if isinstance(context, StructuredStage20VerifiedContext)
            else None
        )
        try:
            active_owner = (
                lease._require_active()
                if isinstance(lease, ReleaseGraphLock) and lease._mode == "write"
                else None
            )
        except RuntimeError:
            active_owner = None
        same_detached_epoch = bool(
            issued is not None
            and active_owner is issued.owner
            and getattr(namespace, "stage_name", None) == "stage-20"
            and getattr(namespace, "_run_identity", None) == issued.run_identity
            and getattr(namespace, "_stage_identity", None)
            == issued.stage_identity
        )
        if not same_detached_epoch:
            raise
        _mark_attempt(issued.owner, namespace)
        _ISSUED_CONTEXTS.pop(context, None)
        _close_snapshot(snapshot)
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=(
                f"Structured Stage 20 admission failed: {exc}"
            ),
            decision="retry",
        )
    issued = _ISSUED_CONTEXTS[context]
    if (
        snapshot.identity_tuple != context.snapshot_identity
        or snapshot.run_identity != owner._run_identity
        or snapshot.stage_identity != getattr(namespace, "_stage_identity", None)
    ):
        _drop_context(context, snapshot)
        raise StructuredStage20PublicationError(
            "structured Stage 20 context does not bind Snapshot A"
        )
    issued.phase = "publishing"
    _clear_published_context(owner)
    _mark_attempt(owner, namespace)
    diagnostics: tuple[TransportDiagnostic, ...] = ()
    held_temps: list[_HeldTemp] = []
    try:
        _verify_source_fixpoint(lease, snapshot)
        if llm is None:
            raise StructuredStage20PublicationError(
                "structured Stage 20 requires a quality provider"
            )
        configured_threshold = (
            snapshot.canonical_config.research.quality_threshold
        )
        threshold = (
            5.0 if configured_threshold is None else configured_threshold
        )
        response, diagnostics = execute_bounded_quality_calls(
            paper=snapshot.source(
                "stage-19/scientific_claim_paper_revised.md"
            ).content,
            quality_threshold=canonical_decimal(Decimal(str(threshold))),
            llm=llm,
        )
        report_generated = _utcnow_iso()
        manifest_generated = _utcnow_iso()
        signal_generated = _utcnow_iso()
        binding = snapshot.stage19_snapshot.binding
        paper = snapshot.source(
            "stage-19/scientific_claim_paper_revised.md"
        )
        stage19_manifest = snapshot.source(
            "stage-19/scientific_claim_authority_manifest.json"
        )
        report = build_quality_report(
            response,
            canonical_evidence_path=binding.canonical_experiment_evidence_path,
            canonical_evidence_sha256=(
                binding.canonical_experiment_evidence_sha256
            ),
            cfs_sha256=binding.cfs_sha256,
            generation_binding_sha256=binding.generation_binding_sha256,
            source_paper_sha256=paper.sha256,
            stage19_manifest_sha256=stage19_manifest.sha256,
            generated=report_generated,
        )
        score = response["score_1_to_10"]
        flags = build_fabrication_flags(
            snapshot.fabrication_state,
            quality_score=score,
            canonical_evidence_path=binding.canonical_experiment_evidence_path,
            canonical_evidence_sha256=(
                binding.canonical_experiment_evidence_sha256
            ),
            cfs_sha256=binding.cfs_sha256,
            generation_binding_sha256=binding.generation_binding_sha256,
            source_paper_sha256=paper.sha256,
            stage19_manifest_sha256=stage19_manifest.sha256,
        )
        outcome = derive_outcome(
            verdict=str(response["verdict"]),
            score=score,
            threshold=threshold,
            graceful_degradation=(
                snapshot.canonical_config.research.graceful_degradation
            ),
            state=snapshot.fabrication_state,
        )
        if outcome is None:
            raise StructuredStage20PublicationError(
                "quality verdict/score does not authorize structured publication"
            )
        stage_fd = getattr(namespace, "_stage_fd")
        report_temp = _create_held_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_STAGE_TEMPS[0],
            content=report,
        )
        held_temps.append(report_temp)
        flags_temp = _create_held_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_STAGE_TEMPS[1],
            content=flags,
        )
        held_temps.append(flags_temp)
        verified_report = _VerifiedTemp(
            report_temp, _verify_held_temp(report_temp)
        )
        verified_flags = _VerifiedTemp(
            flags_temp, _verify_held_temp(flags_temp)
        )
        report_payload = replay_quality_report(
            verified_report.verified_bytes, expected=report
        )
        flags_payload = replay_fabrication_flags(
            verified_flags.verified_bytes, expected=flags
        )
        report_sha = hashlib.sha256(
            verified_report.verified_bytes
        ).hexdigest()
        flags_sha = hashlib.sha256(
            verified_flags.verified_bytes
        ).hexdigest()
        signal: bytes | None = None
        signal_sha: str | None = None
        verified_signal: _VerifiedTemp | None = None
        if outcome == "degraded":
            signal = build_degradation_signal(
                canonical_evidence_path=binding.canonical_experiment_evidence_path,
                canonical_evidence_sha256=(
                    binding.canonical_experiment_evidence_sha256
                ),
                cfs_sha256=binding.cfs_sha256,
                generation_binding_sha256=binding.generation_binding_sha256,
                source_paper_sha256=paper.sha256,
                stage19_manifest_sha256=stage19_manifest.sha256,
                quality_report_sha256=report_sha,
                fabrication_flags_sha256=flags_sha,
                quality_score=score,
                quality_threshold=threshold,
                weaknesses=response["weaknesses"],  # type: ignore[arg-type]
                generated=signal_generated,
            )
            signal_temp = _create_held_temp(
                directory_fd=owner._run_fd,
                name=STRUCTURED_STAGE20_ROOT_TEMP,
                content=signal,
            )
            held_temps.append(signal_temp)
            verified_signal = _VerifiedTemp(
                signal_temp, _verify_held_temp(signal_temp)
            )
            signal_payload = replay_degradation_signal(
                verified_signal.verified_bytes, expected=signal
            )
            signal_sha = hashlib.sha256(
                verified_signal.verified_bytes
            ).hexdigest()
        else:
            signal_payload = None
            _require_root_signal_absent(namespace)
        _verify_source_fixpoint(lease, snapshot)
        manifest = build_quality_gate_manifest(
            outcome=outcome,
            verdict=str(response["verdict"]),
            canonical_evidence_path=binding.canonical_experiment_evidence_path,
            canonical_evidence_sha256=(
                binding.canonical_experiment_evidence_sha256
            ),
            cfs_sha256=binding.cfs_sha256,
            generation_binding_sha256=binding.generation_binding_sha256,
            source_paper_sha256=paper.sha256,
            stage19_manifest_sha256=stage19_manifest.sha256,
            quality_report_sha256=report_sha,
            fabrication_flags_sha256=flags_sha,
            quality_score=score,
            quality_threshold=threshold,
            graceful_degradation=(
                snapshot.canonical_config.research.graceful_degradation
            ),
            degradation_signal_sha256=signal_sha,
            generated=manifest_generated,
        )
        manifest_temp = _create_held_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_STAGE_TEMPS[2],
            content=manifest,
        )
        held_temps.append(manifest_temp)
        verified_manifest = _VerifiedTemp(
            manifest_temp, _verify_held_temp(manifest_temp)
        )
        manifest_payload = replay_quality_gate_manifest(
            verified_manifest.verified_bytes,
            expected=manifest,
            signal_present=verified_signal is not None,
        )
        _require_independent_bundle_semantics(
            snapshot,
            report_payload=report_payload,
            flags_payload=flags_payload,
            manifest_payload=manifest_payload,
            signal_payload=signal_payload,
            report_bytes=verified_report.verified_bytes,
            flags_bytes=verified_flags.verified_bytes,
            signal_bytes=(
                None
                if verified_signal is None
                else verified_signal.verified_bytes
            ),
        )
        for verified in (
            verified_report,
            verified_flags,
            *(() if verified_signal is None else (verified_signal,)),
            verified_manifest,
        ):
            _require_held_temp_identity(verified.held)
        formal_identities: dict[str, tuple[int, int]] = {}
        formal_identities[STRUCTURED_STAGE20_OUTPUTS[0]] = (
            _create_formal_from_verified_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_OUTPUTS[0],
            temp=verified_report,
            )
        )
        formal_identities[STRUCTURED_STAGE20_OUTPUTS[1]] = (
            _create_formal_from_verified_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_OUTPUTS[1],
            temp=verified_flags,
            )
        )
        signal_identity: tuple[int, int] | None = None
        if verified_signal is not None:
            signal_identity = _create_formal_from_verified_temp(
                directory_fd=owner._run_fd,
                name=STRUCTURED_STAGE20_SIGNAL,
                temp=verified_signal,
            )
        formal_identities[STRUCTURED_STAGE20_MANIFEST] = (
            _create_formal_from_verified_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_MANIFEST,
            temp=verified_manifest,
            )
        )
        temp_cleanup_errors = _cleanup_temporary_names(owner, namespace)
        if temp_cleanup_errors:
            raise StructuredStage20PublicationError(
                "structured Stage 20 temporary cleanup failed: "
                + "; ".join(temp_cleanup_errors)
            )
        first = _capture_final(
            lease,
            namespace,
            snapshot,
            expected_report=report,
            expected_flags=flags,
            expected_manifest=manifest,
            expected_signal=signal,
            expected_stage_identities=formal_identities,
            expected_signal_identity=signal_identity,
        )
        _verify_source_fixpoint(lease, snapshot)
        second = _capture_final(
            lease,
            namespace,
            snapshot,
            expected_report=report,
            expected_flags=flags,
            expected_manifest=manifest,
            expected_signal=signal,
            expected_stage_identities=formal_identities,
            expected_signal_identity=signal_identity,
        )
        _verify_source_fixpoint(lease, snapshot)
        if first != second:
            raise StructuredStage20PublicationError(
                "structured Stage 20 final snapshots differ"
            )
        held_namespace = _retain_namespace(namespace)
        _clear_attempt_namespace(owner)
        owner._structured_stage20_publication_context = _PublishedContext(
            snapshot,
            first,
            context,
            report,
            flags,
            manifest,
            signal,
            dict(formal_identities),
            signal_identity,
            "proceed" if outcome == "passed" else "degraded",
            held_namespace,
        )
        issued.phase = "published"
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.DONE,
            artifacts=STRUCTURED_STAGE20_ARTIFACTS,
            evidence_refs=STRUCTURED_STAGE20_EVIDENCE_REFS,
            decision="proceed" if outcome == "passed" else "degraded",
        )
    except Exception as exc:
        diagnostics = tuple(getattr(exc, "diagnostics", diagnostics))
        cleanup_errors = _cleanup_attempt_outputs(owner, namespace)
        cleanup_suffix = (
            "; structured Stage 20 cleanup also failed: "
            + "; ".join(cleanup_errors)
            if cleanup_errors
            else ""
        )
        _clear_published_authority_context(owner)
        _drop_context(context, snapshot)
        diagnostic_errors: tuple[str, ...] = ()
        if diagnostics:
            diagnostic_errors = _publish_failure_diagnostics(
                namespace, diagnostics, exc
            )
        diagnostic_suffix = (
            "; structured Stage 20 diagnostic publication also failed: "
            + "; ".join(diagnostic_errors)
            if diagnostic_errors
            else ""
        )
        return StageResult(
            stage=Stage.QUALITY_GATE,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error=(
                f"Structured Stage 20 failed: {exc}"
                f"{cleanup_suffix}{diagnostic_suffix}"
            ),
            decision="retry",
        )
    finally:
        _close_held_temps(held_temps)


def _capture_final(
    lease: ReleaseGraphLock,
    namespace: object,
    snapshot: StructuredStage20SnapshotA,
    *,
    expected_report: bytes,
    expected_flags: bytes,
    expected_manifest: bytes,
    expected_signal: bytes | None,
    expected_stage_identities: dict[str, tuple[int, int]],
    expected_signal_identity: tuple[int, int] | None,
) -> StructuredStage20FinalSnapshot:
    entries = namespace.direct_entries()  # type: ignore[attr-defined]
    if set(entries) != set(STRUCTURED_STAGE20_ARTIFACTS):
        raise StructuredStage20PublicationError(
            "structured Stage 20 final namespace mismatch"
        )
    files = tuple(
        stage19._read_rich_stage_snapshot(namespace, name)
        for name in STRUCTURED_STAGE20_ARTIFACTS
    )
    if set(expected_stage_identities) != set(STRUCTURED_STAGE20_ARTIFACTS):
        raise StructuredStage20PublicationError(
            "structured Stage 20 expected formal identity set mismatch"
        )
    for item in files:
        if item.identity != expected_stage_identities[item.path]:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 formal identity changed: {item.path}"
            )
    current = {item.path: item.content for item in files}
    report_payload = replay_quality_report(
        current["quality_report.json"], expected=expected_report
    )
    flags_payload = replay_fabrication_flags(
        current["fabrication_flags.json"], expected=expected_flags
    )
    signal_snapshot: RichFileSnapshot | None = None
    signal_payload: Mapping[str, object] | None = None
    if STRUCTURED_STAGE20_ROOT_TEMP in namespace.run_entries():  # type: ignore[attr-defined]
        raise StructuredStage20PublicationError(
            "structured Stage 20 degradation signal temporary remains"
        )
    if expected_signal is None:
        if expected_signal_identity is not None:
            raise StructuredStage20PublicationError(
                "structured Stage 20 unexpected signal identity"
            )
        _require_root_signal_absent(namespace)
    else:
        if expected_signal_identity is None:
            raise StructuredStage20PublicationError(
                "structured Stage 20 missing expected signal identity"
            )
        signal_snapshot = stage19._read_rich_run_snapshot(
            lease,
            STRUCTURED_STAGE20_SIGNAL,
        )
        if signal_snapshot.identity != expected_signal_identity:
            raise StructuredStage20PublicationError(
                "structured Stage 20 degradation signal identity changed"
            )
        if signal_snapshot.content != expected_signal:
            raise StructuredStage20PublicationError(
                "structured Stage 20 degradation signal mismatch"
            )
        signal_payload = replay_degradation_signal(
            signal_snapshot.content, expected=expected_signal
        )
    manifest_payload = replay_quality_gate_manifest(
        current[STRUCTURED_STAGE20_MANIFEST],
        expected=expected_manifest,
        signal_present=signal_snapshot is not None,
    )
    _require_independent_bundle_semantics(
        snapshot,
        report_payload=report_payload,
        flags_payload=flags_payload,
        manifest_payload=manifest_payload,
        signal_payload=signal_payload,
        report_bytes=current["quality_report.json"],
        flags_bytes=current["fabrication_flags.json"],
        signal_bytes=(
            None if signal_snapshot is None else signal_snapshot.content
        ),
    )
    if any(name in entries for name in STRUCTURED_STAGE20_STAGE_TEMPS):
        raise StructuredStage20PublicationError(
            "structured Stage 20 temporary entry remains"
        )
    if (
        STRUCTURED_STAGE20_DIAGNOSTIC in entries
        or STRUCTURED_STAGE20_DIAGNOSTIC_TEMP in entries
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 diagnostic entry remains on success"
        )
    _verify_root_parent_snapshot(snapshot)
    return StructuredStage20FinalSnapshot(
        files,
        signal_snapshot,
        namespace.canonical_identity(),  # type: ignore[attr-defined]
        snapshot.root_parent_identity,
        snapshot.run_identity,
    )


def _require_independent_bundle_semantics(
    snapshot: StructuredStage20SnapshotA,
    *,
    report_payload: Mapping[str, object],
    flags_payload: Mapping[str, object],
    manifest_payload: Mapping[str, object],
    signal_payload: Mapping[str, object] | None,
    report_bytes: bytes,
    flags_bytes: bytes,
    signal_bytes: bytes | None,
) -> None:
    binding = snapshot.stage19_snapshot.binding
    paper = snapshot.source("stage-19/scientific_claim_paper_revised.md")
    stage19_manifest = snapshot.source(
        "stage-19/scientific_claim_authority_manifest.json"
    )
    canonical_ref = {
        "path": binding.canonical_experiment_evidence_path,
        "sha256": binding.canonical_experiment_evidence_sha256,
    }
    cfs = {"schema_version": 1, "sha256": binding.cfs_sha256}
    flat_common = {
        "canonical_experiment_evidence": canonical_ref,
        "cfs": cfs,
        "generation_binding_sha256": binding.generation_binding_sha256,
        "source_paper_path": (
            "stage-19/scientific_claim_paper_revised.md"
        ),
        "source_paper_sha256": paper.sha256,
        "stage19_publication_mode": "structured-scientific-claim-v1",
        "stage19_publication_binding_path": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "stage19_publication_binding_sha256": stage19_manifest.sha256,
    }
    for label, payload in (
        ("quality report", report_payload),
        ("fabrication flags", flags_payload),
    ):
        for field, expected in flat_common.items():
            if payload.get(field) != expected:
                raise StructuredStage20PublicationError(
                    f"structured Stage 20 {label} source binding mismatch: {field}"
                )

    state = snapshot.fabrication_state
    expected_flags = {
        "experiment_failed": state.experiment_failed,
        "real_metric_values": list(state.real_metric_values),
        "verified_values_count": state.verified_values_count,
        "verified_conditions": list(state.verified_conditions),
        "has_real_data": state.has_real_data,
        "fabrication_suspected": state.fabrication_suspected,
    }
    for field, expected in expected_flags.items():
        if flags_payload.get(field) != expected:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 fabrication rebuild mismatch: {field}"
            )

    report_score = report_payload["score_1_to_10"]
    if flags_payload.get("quality_score") != report_score:
        raise StructuredStage20PublicationError(
            "structured Stage 20 report/flags score mismatch"
        )
    configured_threshold = snapshot.canonical_config.research.quality_threshold
    threshold = 5.0 if configured_threshold is None else configured_threshold
    expected_outcome = derive_outcome(
        verdict=str(report_payload["verdict"]),
        score=report_score,
        threshold=threshold,
        graceful_degradation=(
            snapshot.canonical_config.research.graceful_degradation
        ),
        state=state,
    )
    if expected_outcome is None:
        raise StructuredStage20PublicationError(
            "structured Stage 20 independently rebuilt outcome is unpublished"
        )

    expected_manifest = {
        "stage19_publication_mode": "structured-scientific-claim-v1",
        "canonical_experiment_evidence": canonical_ref,
        "cfs": cfs,
        "generation_binding_sha256": binding.generation_binding_sha256,
        "source_paper": {
            "path": "stage-19/scientific_claim_paper_revised.md",
            "sha256": paper.sha256,
        },
        "stage19_authority_manifest": {
            "path": "stage-19/scientific_claim_authority_manifest.json",
            "sha256": stage19_manifest.sha256,
        },
        "quality_report": {
            "path": "stage-20/quality_report.json",
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
        },
        "fabrication_flags": {
            "path": "stage-20/fabrication_flags.json",
            "sha256": hashlib.sha256(flags_bytes).hexdigest(),
        },
        "outcome": expected_outcome,
        "quality_verdict": report_payload["verdict"],
        "quality_score": canonical_decimal(Decimal(str(report_score))),
        "quality_threshold": canonical_decimal(Decimal(str(threshold))),
        "graceful_degradation": (
            snapshot.canonical_config.research.graceful_degradation
        ),
    }
    for field, expected in expected_manifest.items():
        if manifest_payload.get(field) != expected:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 manifest independent rebuild mismatch: {field}"
            )

    if expected_outcome == "passed":
        if (
            manifest_payload.get("degradation_signal") is not None
            or signal_payload is not None
            or signal_bytes is not None
        ):
            raise StructuredStage20PublicationError(
                "structured Stage 20 passed signal branch mismatch"
            )
        return

    if signal_payload is None or signal_bytes is None:
        raise StructuredStage20PublicationError(
            "structured Stage 20 degraded signal is missing"
        )
    expected_signal_ref = {
        "path": "degradation_signal.json",
        "sha256": hashlib.sha256(signal_bytes).hexdigest(),
    }
    if manifest_payload.get("degradation_signal") != expected_signal_ref:
        raise StructuredStage20PublicationError(
            "structured Stage 20 degradation signal ref mismatch"
        )
    expected_signal = {
        "outcome": "degraded",
        "stage19_publication_mode": "structured-scientific-claim-v1",
        "canonical_experiment_evidence": canonical_ref,
        "cfs": cfs,
        "generation_binding_sha256": binding.generation_binding_sha256,
        "source_paper": expected_manifest["source_paper"],
        "stage19_authority_manifest": expected_manifest[
            "stage19_authority_manifest"
        ],
        "quality_report": expected_manifest["quality_report"],
        "fabrication_flags": expected_manifest["fabrication_flags"],
        "quality_verdict": "revise",
        "quality_score": expected_manifest["quality_score"],
        "quality_threshold": expected_manifest["quality_threshold"],
        "weaknesses": report_payload["weaknesses"],
    }
    for field, expected in expected_signal.items():
        if signal_payload.get(field) != expected:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 signal independent rebuild mismatch: {field}"
            )


def _retain_namespace(namespace: object) -> BoundOutputNamespace:
    """Duplicate the exact held namespace across executor postconditions."""

    run_fd = os.dup(getattr(namespace, "_run_fd"))
    try:
        stage_fd = os.dup(getattr(namespace, "_stage_fd"))
    except Exception:
        os.close(run_fd)
        raise
    retained = BoundOutputNamespace(
        run_dir=getattr(namespace, "run_dir"),
        stage_dir=getattr(namespace, "stage_dir"),
        stage_name=getattr(namespace, "stage_name"),
        _run_fd=run_fd,
        _stage_fd=stage_fd,
        _run_identity=getattr(namespace, "_run_identity"),
        _stage_identity=getattr(namespace, "_stage_identity"),
    )
    if (
        (os.fstat(run_fd).st_dev, os.fstat(run_fd).st_ino)
        != retained._run_identity
        or (os.fstat(stage_fd).st_dev, os.fstat(stage_fd).st_ino)
        != retained._stage_identity
    ):
        retained.close()
        raise StructuredStage20PublicationError(
            "retained Stage 20 namespace identity mismatch"
        )
    return retained


def _verify_source_fixpoint(
    lease: ReleaseGraphLock, snapshot: StructuredStage20SnapshotA
) -> None:
    current = tuple(
        stage19._read_rich_run_snapshot(lease, item.path)
        for item in snapshot.sources
    )
    if current != snapshot.sources:
        raise StructuredStage20PublicationError(
            "structured Stage 20 source fixpoint changed"
        )
    _verify_root_parent_snapshot(snapshot)
    lease.assert_canonical()


def _verify_root_parent_snapshot(snapshot: StructuredStage20SnapshotA) -> None:
    _verify_root_parent(
        snapshot.root_parent_fd,
        root_parent_path=snapshot.root_parent_path,
        root_parent_identity=snapshot.root_parent_identity,
        run_name=snapshot.run_name,
        run_identity=snapshot.run_identity,
    )


def _verify_root_parent(
    parent_fd: int,
    *,
    root_parent_path: str,
    root_parent_identity: tuple[int, int],
    run_name: str,
    run_identity: tuple[int, int],
) -> None:
    held = os.fstat(parent_fd)
    live_parent = os.stat(root_parent_path, follow_symlinks=False)
    live_run = os.stat(run_name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(held.st_mode)
        or (held.st_dev, held.st_ino) != root_parent_identity
        or not stat.S_ISDIR(live_parent.st_mode)
        or (live_parent.st_dev, live_parent.st_ino) != root_parent_identity
        or not stat.S_ISDIR(live_run.st_mode)
        or (live_run.st_dev, live_run.st_ino) != run_identity
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 root parent or run changed"
        )


def _require_root_signal_absent(namespace: object) -> None:
    entries = set(namespace.run_entries())  # type: ignore[attr-defined]
    if (
        STRUCTURED_STAGE20_SIGNAL in entries
        or STRUCTURED_STAGE20_ROOT_TEMP in entries
    ):
        raise StructuredStage20PublicationError(
            "passed outcome has a degradation signal"
        )


def _create_held_temp(
    *, directory_fd: int, name: str, content: bytes
) -> _HeldTemp:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 created unsafe temporary: {name}"
            )
        _write_all(descriptor, content)
        os.fsync(descriptor)
        return _HeldTemp(
            directory_fd,
            name,
            descriptor,
            (info.st_dev, info.st_ino),
            bytes(content),
        )
    except Exception:
        os.close(descriptor)
        raise


def _require_held_temp_identity(record: _HeldTemp) -> None:
    held = os.fstat(record.descriptor)
    current = os.stat(
        record.name,
        dir_fd=record.directory_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (held.st_dev, held.st_ino) != record.identity
        or (current.st_dev, current.st_ino) != record.identity
    ):
        raise StructuredStage20PublicationError(
            f"structured Stage 20 temporary identity drift: {record.name}"
        )


def _verify_held_temp(record: _HeldTemp) -> bytes:
    _require_held_temp_identity(record)
    size = os.fstat(record.descriptor).st_size
    content = b""
    offset = 0
    while offset < size:
        chunk = os.pread(record.descriptor, size - offset, offset)
        if not chunk:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 temporary read stalled: {record.name}"
            )
        content += chunk
        offset += len(chunk)
    if content != record.expected_bytes:
        raise StructuredStage20PublicationError(
            f"structured Stage 20 temporary bytes changed: {record.name}"
        )
    return content


def _create_formal_from_verified_temp(
    *, directory_fd: int, name: str, temp: _VerifiedTemp
) -> tuple[int, int]:
    _require_held_temp_identity(temp.held)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StructuredStage20PublicationError(
                f"structured Stage 20 created unsafe formal file: {name}"
            )
        identity = (info.st_dev, info.st_ino)
        _write_all(descriptor, temp.verified_bytes)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != identity
    ):
        raise StructuredStage20PublicationError(
            f"structured Stage 20 formal identity drift: {name}"
        )
    return identity


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("structured Stage 20 write made no progress")
        offset += written


def _close_held_temps(records: list[_HeldTemp]) -> None:
    for record in records:
        try:
            os.close(record.descriptor)
        except OSError:
            pass


def _unlink_reserved(
    directory_fd: int,
    name: str,
    errors: list[str],
    *,
    collision_is_error: bool = False,
) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if collision_is_error:
        errors.append(f"{name}: preexisting reserved temporary collision")
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except Exception as exc:
        errors.append(f"{name}: {exc}")


def _cleanup_temporary_names(
    owner: ReleaseGraphLock, namespace: object
) -> tuple[str, ...]:
    errors: list[str] = []
    stage_fd = getattr(namespace, "_stage_fd")
    for name in STRUCTURED_STAGE20_STAGE_TEMPS:
        _unlink_reserved(stage_fd, name, errors)
    _unlink_reserved(owner._run_fd, STRUCTURED_STAGE20_ROOT_TEMP, errors)
    return tuple(errors)


def _cleanup_attempt_outputs(
    owner: ReleaseGraphLock, namespace: object
) -> tuple[str, ...]:
    errors: list[str] = []
    stage_fd = getattr(namespace, "_stage_fd")
    for name in (
        STRUCTURED_STAGE20_MANIFEST,
        *STRUCTURED_STAGE20_OUTPUTS,
        *STRUCTURED_STAGE20_STAGE_TEMPS,
    ):
        _unlink_reserved(stage_fd, name, errors)
    for name in (
        STRUCTURED_STAGE20_SIGNAL,
        STRUCTURED_STAGE20_ROOT_TEMP,
    ):
        _unlink_reserved(owner._run_fd, name, errors)
    for name in (
        STRUCTURED_STAGE20_DIAGNOSTIC,
        STRUCTURED_STAGE20_DIAGNOSTIC_TEMP,
    ):
        _unlink_reserved(stage_fd, name, errors)
    return tuple(errors)


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _cleanup_owned_namespace(
    owner: ReleaseGraphLock, namespace: object
) -> tuple[str, ...]:
    errors: list[str] = []
    stage_fd = getattr(namespace, "_stage_fd")
    for name in (
        STRUCTURED_STAGE20_MANIFEST,
        *STRUCTURED_STAGE20_OUTPUTS,
    ):
        _unlink_reserved(stage_fd, name, errors)
    for name in STRUCTURED_STAGE20_STAGE_TEMPS:
        _unlink_reserved(
            stage_fd, name, errors, collision_is_error=True
        )
    for name in (
        STRUCTURED_STAGE20_SIGNAL,
    ):
        _unlink_reserved(owner._run_fd, name, errors)
    for name in (
        STRUCTURED_STAGE20_ROOT_TEMP,
    ):
        _unlink_reserved(
            owner._run_fd, name, errors, collision_is_error=True
        )
    for name in (
        STRUCTURED_STAGE20_DIAGNOSTIC,
        STRUCTURED_STAGE20_DIAGNOSTIC_TEMP,
    ):
        _unlink_reserved(stage_fd, name, errors)
    return tuple(errors)


def _publish_failure_diagnostics(
    namespace: object,
    diagnostics: tuple[TransportDiagnostic, ...],
    original: Exception,
) -> tuple[str, ...]:
    held: _HeldTemp | None = None
    errors: list[str] = []
    try:
        payload = [item.to_dict() for item in diagnostics]
        content = (
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8")
        stage_fd = getattr(namespace, "_stage_fd")
        held = _create_held_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_DIAGNOSTIC_TEMP,
            content=content,
        )
        verified = _VerifiedTemp(held, _verify_held_temp(held))
        _create_formal_from_verified_temp(
            directory_fd=stage_fd,
            name=STRUCTURED_STAGE20_DIAGNOSTIC,
            temp=verified,
        )
        errors: list[str] = []
        _unlink_reserved(
            stage_fd, STRUCTURED_STAGE20_DIAGNOSTIC_TEMP, errors
        )
        if errors:
            raise StructuredStage20PublicationError("; ".join(errors))
    except Exception as exc:
        errors.append(str(exc))
        original.add_note(
            f"structured Stage 20 diagnostic publication also failed: {exc}"
        )
    finally:
        if held is not None:
            _close_held_temps([held])
    return tuple(errors)


def has_structured_stage20_published_context(lease: object) -> bool:
    if not isinstance(lease, ReleaseGraphLock):
        return False
    try:
        owner = lease._require_active()
    except RuntimeError:
        return False
    return isinstance(
        getattr(owner, "_structured_stage20_publication_context", None),
        _PublishedContext,
    )


def has_structured_stage20_attempt_context(lease: object) -> bool:
    if not isinstance(lease, ReleaseGraphLock):
        return False
    try:
        owner = lease._require_active()
    except RuntimeError:
        return False
    return getattr(owner, "_structured_stage20_attempted", None) is True


def validate_structured_stage20_executor_postcondition(
    lease: ReleaseGraphLock,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    decision: str | None,
) -> None:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    context = getattr(owner, "_structured_stage20_publication_context", None)
    if not isinstance(context, _PublishedContext):
        raise StructuredStage20PublicationError(
            "structured Stage 20 postcondition lacks published context"
        )
    if (
        artifacts != STRUCTURED_STAGE20_ARTIFACTS
        or evidence_refs != STRUCTURED_STAGE20_EVIDENCE_REFS
        or decision != context.decision
    ):
        raise StructuredStage20PublicationError(
            "structured Stage 20 postcondition result tuple mismatch"
        )
    namespace = context.held_namespace
    _require_verified_context(
        writer,
        context.verified_context,
        namespace=namespace,
        allowed_phases=("published",),
    )
    _verify_source_fixpoint(writer, context.snapshot_a)
    current = _capture_final(
        writer,
        namespace,
        context.snapshot_a,
        expected_report=context.expected_report,
        expected_flags=context.expected_flags,
        expected_manifest=context.expected_manifest,
        expected_signal=context.expected_signal,
        expected_stage_identities=context.expected_stage_identities,
        expected_signal_identity=context.expected_signal_identity,
    )
    if current != context.final_snapshot:
        raise StructuredStage20PublicationError(
            "structured Stage 20 postcondition snapshot mismatch"
        )


def invalidate_structured_stage20_authority(
    lease: ReleaseGraphLock,
) -> tuple[str, ...]:
    writer = require_active_writer_invalidation_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    published = getattr(
        owner, "_structured_stage20_publication_context", None
    )
    if isinstance(published, _PublishedContext):
        namespace = published.held_namespace
    else:
        namespace = getattr(
            owner, "_structured_stage20_attempt_namespace", None
        )
    errors = (
        _cleanup_attempt_outputs(owner, namespace)
        if isinstance(namespace, BoundOutputNamespace)
        else ()
    )
    _clear_published_context(owner)
    return errors


def clear_structured_stage20_published_context(lease: ReleaseGraphLock) -> None:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    _clear_published_context(writer._require_active())


def _clear_published_context(owner: ReleaseGraphLock) -> None:
    _clear_published_authority_context(owner)
    _clear_attempt_namespace(owner)
    try:
        delattr(owner, "_structured_stage20_attempted")
    except AttributeError:
        pass


def _clear_published_authority_context(owner: ReleaseGraphLock) -> None:
    published = getattr(owner, "_structured_stage20_publication_context", None)
    if isinstance(published, _PublishedContext):
        _ISSUED_CONTEXTS.pop(published.verified_context, None)
        _close_snapshot(published.snapshot_a)
        published.held_namespace.close()
    try:
        delattr(owner, "_structured_stage20_publication_context")
    except AttributeError:
        pass


def _clear_attempt_namespace(owner: ReleaseGraphLock) -> None:
    namespace = getattr(owner, "_structured_stage20_attempt_namespace", None)
    if isinstance(namespace, BoundOutputNamespace):
        namespace.close()
    try:
        delattr(owner, "_structured_stage20_attempt_namespace")
    except AttributeError:
        pass


def _mark_attempt(owner: ReleaseGraphLock, namespace: object) -> None:
    _clear_attempt_namespace(owner)
    owner._structured_stage20_attempt_namespace = _retain_namespace(namespace)
    owner._structured_stage20_attempted = True


def _drop_context(
    context: StructuredStage20VerifiedContext,
    snapshot: StructuredStage20SnapshotA,
) -> None:
    _ISSUED_CONTEXTS.pop(context, None)
    _close_snapshot(snapshot)


def _close_snapshot(snapshot: StructuredStage20SnapshotA) -> None:
    if snapshot.root_parent_fd >= 0:
        try:
            os.close(snapshot.root_parent_fd)
        except OSError:
            pass


def execute_structured_stage20_quality(
    run_dir: Path,
    stage_dir: Path,
    llm: LLMClient | None,
) -> StageResult:
    """Public/direct entry; complete-capability guard is the first operation."""

    capability.require_complete_structured_capability(
        "execute_structured_stage20_quality"
    )
    with ReleaseGraphLock.acquire(
        run_dir, "execute_structured_stage20_quality", mode="write"
    ) as lease:
        if stage_dir != run_dir / "stage-20":
            raise StructuredStage20PublicationError(
                "structured Stage 20 directory is not canonical"
            )
        lease.ensure_run_directory("stage-20")
        with lease.open_stage_namespace("stage-20") as namespace:
            try:
                snapshot, context = _capture_snapshot_a_and_issue_context(
                    lease, namespace=namespace
                )
            except Exception as exc:
                owner = lease._require_active()
                cleanup_errors = _cleanup_attempt_outputs(owner, namespace)
                _clear_published_context(owner)
                cleanup_suffix = (
                    "; structured Stage 20 cleanup also failed: "
                    + "; ".join(cleanup_errors)
                    if cleanup_errors
                    else ""
                )
                return StageResult(
                    stage=Stage.QUALITY_GATE,
                    status=StageStatus.FAILED,
                    artifacts=(),
                    evidence_refs=(),
                    error=(
                        f"Structured Stage 20 capture failed: {exc}"
                        f"{cleanup_suffix}"
                    ),
                    decision="retry",
                )
            return _execute_structured_stage20_from_context(
                lease,
                namespace=namespace,
                snapshot=snapshot,
                context=context,
                llm=llm,
            )
