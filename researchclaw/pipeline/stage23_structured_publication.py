"""Inactive held-fd structured Stage 23 citation verification publication."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Callable, Iterable, Mapping
import weakref

from researchclaw.literature.citation_plan import parse_citation_plan
from researchclaw.literature.verify import parse_bibtex_entries
from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.pipeline import stage19_structured_publication as stage19
from researchclaw.pipeline import stage22_structured_publication as stage22
from researchclaw.pipeline import stage23_structured_authority as authority
from researchclaw.pipeline import stage23_structured_transport as transport
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.scientific_claim_authority import (
    build_scientific_claim_registry,
)
from researchclaw.pipeline.sectional_validation import extract_citation_keys
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus


class StructuredStage23PublicationError(RuntimeError):
    """Structured Stage 23 admission, transport, or publication failed."""


_CONSTRUCTION_AUTHORITY = object()
_STAGE_NAME = "stage-23"
_MANIFEST_NAME = "stage23_verification_manifest.json"
_PAYLOAD_NAMES = (
    "verification_report.json",
    "references_verified.bib",
    "paper_final_verified.md",
)
_FORMAL_NAMES = (*_PAYLOAD_NAMES, _MANIFEST_NAME)


class _NonTransferableContext:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("structured Stage 23 context cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("structured Stage 23 context cannot be copied")

    def __reduce__(self):
        raise TypeError("structured Stage 23 context cannot be serialized")


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage23PreAdmissionContext(_NonTransferableContext):
    _token: object
    _writer_owner: object
    _run_path: str
    _run_identity: tuple[int, int]
    logical_stage_id: str
    structured_capability_snapshot: tuple[int, int, int, int]
    generation_binding_sha256: str
    stage22_manifest_sha256: str
    cited_keys: tuple[str, ...]
    upstream_identity: tuple[object, ...]

    def __init__(
        self,
        *,
        authority_token: object,
        token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        generation_binding_sha256: str,
        stage22_manifest_sha256: str,
        cited_keys: tuple[str, ...],
        upstream_identity: tuple[object, ...],
    ) -> None:
        if authority_token is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 23 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "logical_stage_id", "stage23")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(
            self, "generation_binding_sha256", generation_binding_sha256
        )
        object.__setattr__(
            self, "stage22_manifest_sha256", stage22_manifest_sha256
        )
        object.__setattr__(self, "cited_keys", cited_keys)
        object.__setattr__(self, "upstream_identity", upstream_identity)


@dataclass(frozen=True, eq=False, init=False, slots=True, weakref_slot=True)
class Stage23AttemptContext(_NonTransferableContext):
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
        authority_token: object,
        token: object,
        writer_owner: object,
        run_path: str,
        run_identity: tuple[int, int],
        stage_identity: tuple[int, int],
        upstream_identity: tuple[object, ...],
    ) -> None:
        if authority_token is not _CONSTRUCTION_AUTHORITY:
            raise TypeError("structured Stage 23 context construction is private")
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_writer_owner", writer_owner)
        object.__setattr__(self, "_run_path", run_path)
        object.__setattr__(self, "_run_identity", run_identity)
        object.__setattr__(self, "_stage_identity", stage_identity)
        object.__setattr__(self, "logical_stage_id", "stage23")
        object.__setattr__(
            self, "structured_capability_snapshot", _capability_tuple()
        )
        object.__setattr__(self, "upstream_identity", upstream_identity)


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
    base: stage22._CapturedUpstream
    stage22_namespace: BoundOutputNamespace
    stage22_manifest: stage19.RichFileSnapshot
    stage22_manifest_payload: Mapping[str, object]
    stage22_outputs: tuple[stage19.RichFileSnapshot, ...]
    paper: stage19.RichFileSnapshot
    bibliography: stage19.RichFileSnapshot
    latex: stage19.RichFileSnapshot
    stage19_selection: stage19.RichFileSnapshot
    cited_keys: tuple[str, ...]
    bibliography_entries: tuple[Mapping[str, str], ...]
    verified_bibliography: bytes
    claim_projection: Mapping[str, object]
    relevance_spec: transport.Stage23RelevanceTransportSpec | None

    @property
    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.base.identity_tuple,
            self.stage22_namespace._stage_identity,
            self.stage22_manifest.identity_tuple(),
            tuple(item.identity_tuple() for item in self.stage22_outputs),
            self.stage19_selection.identity_tuple(),
            self.cited_keys,
            authority.canonical_json_bytes(self.claim_projection),
            (
                None
                if self.relevance_spec is None
                else (
                    self.relevance_spec.provider,
                    self.relevance_spec.model,
                    self.relevance_spec.base_url,
                    self.relevance_spec.origin,
                    self.relevance_spec.endpoint_path,
                    self.relevance_spec.wire_api,
                    self.relevance_spec.wire_policy_id,
                    id(self.relevance_spec.credential_identity),
                )
            ),
        )

    def close(self) -> None:
        self.stage22_namespace.close()
        self.base.close()


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
    held_files: dict[str, _HeldFile] | None = None
    manifest: _HeldFile | None = None
    metadata: tuple[Mapping[str, object], ...] | None = None
    relevance: Mapping[str, object] | None = None
    outcome: str | None = None
    final_snapshot: tuple[object, ...] | None = None


@dataclass(frozen=True)
class ProvisionalStage23Result:
    status: StageStatus
    artifacts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    degraded: bool
    context: Stage23AttemptContext


@dataclass(frozen=True)
class _ActiveClientRecord:
    registration_identity: object
    run_path: str
    run_identity: tuple[int, int]
    owner: object
    client_ref: weakref.ReferenceType[LLMClient]
    provider: str
    base_url: str
    wire_api: str
    primary_model: str
    fallback_models: tuple[str, ...]
    credential: bytes = field(repr=False)
    client_credential_source_identity: int


_PRE_CONTEXTS: weakref.WeakKeyDictionary[
    Stage23PreAdmissionContext, _PreRecord
] = weakref.WeakKeyDictionary()
_ATTEMPT_CONTEXTS: weakref.WeakKeyDictionary[
    Stage23AttemptContext, _AttemptRecord
] = weakref.WeakKeyDictionary()
_ACTIVE_CLIENTS: weakref.WeakKeyDictionary[
    LLMClient, _ActiveClientRecord
] = weakref.WeakKeyDictionary()
_ACTIVE_REGISTRATIONS: dict[object, _ActiveClientRecord] = {}
_SPEC_REGISTRATIONS: weakref.WeakKeyDictionary[
    transport.Stage23RelevanceTransportSpec, object
] = weakref.WeakKeyDictionary()


def create_and_register_stage23_active_client(
    lease: ReleaseGraphLock,
) -> LLMClient:
    """Create the sole current-run Stage 23 client from replayed config."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_no_active_registration(owner)
    capture = stage22._capture_upstream(writer)
    try:
        config = capture.semantic_inputs.canonical_config
        canonical = config.llm
        canonical_credential = canonical.api_key
        if type(canonical_credential) is not str:
            raise StructuredStage23PublicationError(
                "canonical Stage 23 credential is invalid"
            )
        try:
            credential = canonical_credential.encode("ascii")
        except UnicodeEncodeError as exc:
            raise StructuredStage23PublicationError(
                "canonical Stage 23 credential is invalid"
            ) from exc
        if not credential or any(
            byte < 0x21 or byte > 0x7E for byte in credential
        ):
            raise StructuredStage23PublicationError(
                "canonical Stage 23 credential is invalid"
            )
        client = LLMClient(
            LLMConfig(
                base_url=canonical.base_url,
                api_key=canonical_credential,
                wire_api=canonical.wire_api,
                primary_model=canonical.primary_model,
                fallback_models=list(canonical.fallback_models),
                max_retries=canonical.max_retries,
                retry_base_delay=canonical.retry_base_delay,
                timeout_sec=canonical.timeout_sec,
            )
        )
        _register_active_client(owner, client, config, credential=credential)
        return client
    finally:
        capture.close()


def _register_active_client(
    owner: ReleaseGraphLock,
    client: LLMClient,
    config,
    *,
    credential: bytes,
) -> None:
    if type(client) is not LLMClient:
        raise StructuredStage23PublicationError("active LLMClient type mismatch")
    active_owner = owner._require_active()
    run_path = str(owner.run_dir.absolute())
    _require_no_active_registration(owner)
    canonical = config.llm
    registration_identity = object()

    def release_registration(_reference) -> None:
        _drop_active_registration(registration_identity)

    record = _ActiveClientRecord(
        registration_identity,
        run_path,
        owner._run_identity,
        active_owner,
        weakref.ref(client, release_registration),
        canonical.provider,
        canonical.base_url,
        canonical.wire_api,
        canonical.primary_model,
        tuple(canonical.fallback_models),
        credential,
        id(client.config.api_key),
    )
    _ACTIVE_CLIENTS[client] = record
    _ACTIVE_REGISTRATIONS[registration_identity] = record


def _require_no_active_registration(owner: ReleaseGraphLock) -> None:
    active_owner = owner._require_active()
    run_path = str(owner.run_dir.absolute())
    for record in tuple(_ACTIVE_REGISTRATIONS.values()):
        if record.run_path != run_path:
            continue
        if (
            record.run_identity != owner._run_identity
            or record.client_ref() is None
            or record.owner is not active_owner
        ):
            _drop_active_registration(record.registration_identity)
            continue
        if record.owner is active_owner:
            raise StructuredStage23PublicationError(
                "active Stage 23 client is already registered"
            )


def _drop_active_registration(registration_identity: object) -> None:
    record = _ACTIVE_REGISTRATIONS.pop(registration_identity, None)
    if record is None:
        return
    client = record.client_ref()
    if client is not None and _ACTIVE_CLIENTS.get(client) is record:
        _ACTIVE_CLIENTS.pop(client, None)
    for spec, bound_identity in tuple(_SPEC_REGISTRATIONS.items()):
        if bound_identity is registration_identity:
            _SPEC_REGISTRATIONS.pop(spec, None)


def _drop_spec_registration(
    spec: transport.Stage23RelevanceTransportSpec | None,
) -> None:
    if spec is None:
        return
    registration_identity = _SPEC_REGISTRATIONS.pop(spec, None)
    if registration_identity is not None:
        _drop_active_registration(registration_identity)


def issue_stage23_pre_admission_context(
    lease: ReleaseGraphLock,
    *,
    llm: LLMClient | None,
) -> Stage23PreAdmissionContext:
    """Fully replay Stage 22→17/CFS and relevance admission before Stage 23 I/O."""

    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    _require_private_capability()
    try:
        capture = _capture_upstream(writer, llm=llm)
    except Exception as exc:
        if isinstance(exc, StructuredStage23PublicationError):
            raise
        raise StructuredStage23PublicationError(
            f"structured Stage 23 pre-admission replay failed: {exc}"
        ) from exc
    token = object()
    context = Stage23PreAdmissionContext(
        authority_token=_CONSTRUCTION_AUTHORITY,
        token=token,
        writer_owner=owner,
        run_path=str(owner.run_dir.absolute()),
        run_identity=owner._run_identity,
        generation_binding_sha256=(
            capture.base.base.snapshot.authority_inputs.generation_binding_sha256
        ),
        stage22_manifest_sha256=capture.stage22_manifest.sha256,
        cited_keys=capture.cited_keys,
        upstream_identity=capture.identity_tuple,
    )
    finalizer = weakref.finalize(
        context,
        _finalize_pre_admission_capture,
        capture,
    )
    _PRE_CONTEXTS[context] = _PreRecord(token, owner, capture, finalizer)
    return context


def _capture_upstream(
    lease: ReleaseGraphLock,
    *,
    llm: LLMClient | None,
    held_spec: transport.Stage23RelevanceTransportSpec | None = None,
) -> _CapturedUpstream:
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    base = stage22._capture_upstream(writer)
    namespace: BoundOutputNamespace | None = None
    try:
        namespace = writer.open_stage_namespace("stage-22")
        require_namespace_owned_by_epoch(namespace, writer, "stage-22")
        manifest = stage19._read_rich_run_snapshot(
            writer, "stage-22/stage22_export_manifest.json"
        )
        manifest_payload = stage22.replay_structured_stage22_manifest_v2(
            manifest.content
        )
        output_rows = manifest_payload["outputs"]
        if not isinstance(output_rows, list):
            raise StructuredStage23PublicationError("Stage 22 outputs are invalid")
        snapshots = tuple(
            stage19._read_rich_run_snapshot(writer, row["path"])
            for row in output_rows
        )
        if len(snapshots) != manifest_payload["output_count"]:
            raise StructuredStage23PublicationError("Stage 22 output count mismatch")
        for row, snapshot in zip(output_rows, snapshots, strict=True):
            if (
                row["path"] != snapshot.path
                or row["sha256"] != snapshot.sha256
                or row["size"] != len(snapshot.content)
            ):
                raise StructuredStage23PublicationError(
                    "Stage 22 output FileRef mismatch"
                )
        direct_expected = {"stage22_export_manifest.json"}
        for snapshot in snapshots:
            relative = snapshot.path.removeprefix("stage-22/")
            direct_expected.add(relative.split("/", 1)[0])
        if set(namespace.direct_entries()) != direct_expected:
            raise StructuredStage23PublicationError(
                "Stage 22 exact namespace mismatch"
            )
        by_path = {item.path: item for item in snapshots}
        _verify_stage22_independent_rebuild(base, manifest_payload, by_path)
        try:
            paper = by_path["stage-22/paper_final.md"]
            bibliography = by_path["stage-22/references.bib"]
            latex = by_path["stage-22/paper.tex"]
        except KeyError as exc:
            raise StructuredStage23PublicationError(
                "Stage 22 Stage 23 source output missing"
            ) from exc
        cited_keys, entries, selected_bib = _citation_closure(
            base, paper.content, latex.content, bibliography.content
        )
        selection = stage19._read_rich_run_snapshot(
            writer, "stage-19/scientific_claim_selection.json"
        )
        projection = _abstract_projection(
            base,
            selection,
            paper_sha256=paper.sha256,
        )
        relevance_spec = _admit_relevance(
            owner,
            base,
            llm,
            held_spec=held_spec,
        )
        return _CapturedUpstream(
            base,
            namespace,
            manifest,
            manifest_payload,
            snapshots,
            paper,
            bibliography,
            latex,
            selection,
            cited_keys,
            entries,
            selected_bib,
            projection,
            relevance_spec,
        )
    except Exception:
        if namespace is not None:
            namespace.close()
        base.close()
        raise


def _verify_stage22_independent_rebuild(
    capture: stage22._CapturedUpstream,
    manifest: Mapping[str, object],
    by_path: Mapping[str, stage19.RichFileSnapshot],
) -> None:
    """Reject a self-consistent replacement of the stored Stage 22 namespace."""

    deterministic = stage22.build_stage22_outputs_from_inputs(
        capture.semantic_inputs,
        generated=stage22._generated(capture),
    )
    expected_code = stage22._expected_code_files(deterministic)
    expected_noncompiler = {
        **{
            f"stage-22/{name}": content
            for name, content in deterministic.direct_files.items()
        },
        **{
            f"stage-22/code/{name}": content
            for name, content in expected_code.items()
        },
    }
    actual_noncompiler_paths = set(by_path) - {
        "stage-22/compile_status.json",
        "stage-22/paper.pdf",
    }
    if actual_noncompiler_paths != set(expected_noncompiler):
        raise StructuredStage23PublicationError(
            "Stage 22 independently rebuilt output set mismatch"
        )
    for path, expected in expected_noncompiler.items():
        if by_path[path].content != expected:
            raise StructuredStage23PublicationError(
                f"Stage 22 independently rebuilt output changed: {path}"
            )
    _verify_stage22_compile_branch(manifest, by_path)

    inputs = capture.base.snapshot.authority_inputs
    binding = capture.base.snapshot.stage19_snapshot.binding
    expected_roots = {
        "generation_binding_sha256": inputs.generation_binding_sha256,
        "canonical_experiment_evidence": stage22._rich_ref(
            stage22._source(
                capture, binding.canonical_experiment_evidence_path
            )
        ),
        "cfs": {"schema_version": 1, "sha256": inputs.cfs_sha256},
        "selected_result_manifest": stage22._rich_ref(
            stage22._source(
                capture,
                capture.semantic_inputs.evidence.selected_result_manifest_path,
            )
        ),
        "source_stage19_manifest": stage22._rich_ref(
            stage22._source(
                capture, "stage-19/scientific_claim_authority_manifest.json"
            )
        ),
        "source_stage20_manifest": stage22._rich_ref(
            stage22._source(capture, "stage-20/quality_gate_manifest.json")
        ),
        "source_stage21_manifest": stage22._rich_ref(
            stage22._source(capture, "stage-21/bundle_index.json")
        ),
        "source_stage21_archive": stage22._rich_ref(
            stage22._source(capture, "stage-21/archive.md")
        ),
        "source_paper": stage22._rich_ref(
            stage22._source(
                capture, "stage-19/scientific_claim_paper_revised.md"
            )
        ),
        "quality_outcome": inputs.quality_outcome,
        "bibliography_source": stage22._rich_ref(capture.bibliography),
        "generated": inputs.generated,
    }
    for name, expected in expected_roots.items():
        if manifest.get(name) != expected:
            raise StructuredStage23PublicationError(
                f"Stage 22 independent manifest root mismatch: {name}"
            )
    degradation = (
        None
        if inputs.quality_outcome == "passed"
        else stage22._rich_ref(
            stage22._source(capture, "degradation_signal.json")
        )
    )
    if manifest.get("degradation_signal") != degradation:
        raise StructuredStage23PublicationError(
            "Stage 22 independent degradation root mismatch"
        )
    template = manifest.get("template")
    if (
        not isinstance(template, dict)
        or template.get("name") != deterministic.template_name
    ):
        raise StructuredStage23PublicationError(
            "Stage 22 independent template mismatch"
        )


def _verify_stage22_compile_branch(
    manifest: Mapping[str, object],
    by_path: Mapping[str, stage19.RichFileSnapshot],
) -> None:
    status_file = by_path.get("stage-22/compile_status.json")
    if status_file is None:
        raise StructuredStage23PublicationError(
            "Stage 22 compile status is absent"
        )
    try:
        status = stage22._strict_object(
            status_file.content, "Stage 22 held compile status"
        )
        compile_manifest = stage22._compile_manifest(manifest.get("compile"))
    except Exception as exc:
        raise StructuredStage23PublicationError(
            "Stage 22 compile branch replay failed"
        ) from exc
    status_fields = {
        "schema_version",
        "semantic_calls",
        "max_attempts",
        "attempts",
        "status",
        "tooling_available",
        "errors",
        "paper_pdf",
    }
    if set(status) != status_fields or status.get("schema_version") != 2:
        raise StructuredStage23PublicationError(
            "Stage 22 compile status fields mismatch"
        )
    for name in (
        "semantic_calls",
        "max_attempts",
        "attempts",
        "status",
        "tooling_available",
        "errors",
        "paper_pdf",
    ):
        if status.get(name) != compile_manifest.get(name):
            raise StructuredStage23PublicationError(
                f"Stage 22 compile branch differs: {name}"
            )
    if compile_manifest["status_file"] != stage22._bytes_ref(
        "stage-22/compile_status.json", status_file.content
    ):
        raise StructuredStage23PublicationError(
            "Stage 22 compile status FileRef mismatch"
        )
    pdf = by_path.get("stage-22/paper.pdf")
    if status["status"] == "success":
        if pdf is None or status["paper_pdf"] != stage22._bytes_ref(
            "stage-22/paper.pdf", pdf.content
        ):
            raise StructuredStage23PublicationError(
                "Stage 22 compiler-success PDF mismatch"
            )
        try:
            stage22._validate_pdf(pdf.content)
        except Exception as exc:
            raise StructuredStage23PublicationError(
                "Stage 22 compiler-success PDF is invalid"
            ) from exc
    elif pdf is not None or status["paper_pdf"] is not None:
        raise StructuredStage23PublicationError(
            "Stage 22 compiler-failure published PDF"
        )


def _citation_closure(
    capture: stage22._CapturedUpstream,
    paper: bytes,
    latex: bytes,
    bibliography: bytes,
) -> tuple[tuple[str, ...], tuple[Mapping[str, str], ...], bytes]:
    try:
        markdown_text = paper.decode("utf-8")
        latex_text = latex.decode("utf-8")
        bibliography_text = bibliography.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredStage23PublicationError(
            "Stage 23 sources are not UTF-8"
        ) from exc
    markdown = set(extract_citation_keys(markdown_text))
    latex_keys: set[str] = set()
    for match in re.finditer(r"\\cite[a-zA-Z*]*(?:\[[^\]]*\])*\{([^}]+)\}", latex_text):
        latex_keys.update(
            key.strip() for key in match.group(1).split(",") if key.strip()
        )
    if markdown != latex_keys:
        raise StructuredStage23PublicationError(
            "Markdown/LaTeX citation closure mismatch"
        )
    cited = authority.validate_cited_keys(
        tuple(sorted(markdown, key=lambda item: item.encode("utf-8")))
    )
    parsed = parse_bibtex_entries(bibliography_text)
    keys = [str(entry.get("key") or "").strip() for entry in parsed]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise StructuredStage23PublicationError(
            "bibliography keys are empty or duplicated"
        )
    if not set(cited).issubset(keys):
        raise StructuredStage23PublicationError("bibliography cited key missing")
    plan_file = stage22._source_from_base(capture.base, "stage-16/citation_plan.json")
    try:
        plan = parse_citation_plan(plan_file.content.decode("utf-8"))
        planned = {
            item["cite_key"]
            for claim in plan["claims"]
            for item in claim["planned_citations"]
        }
    except Exception as exc:
        raise StructuredStage23PublicationError(
            "Stage 17 citation plan replay failed"
        ) from exc
    if not set(cited).issubset(planned):
        raise StructuredStage23PublicationError("unplanned cited key")
    entry_by_key = {str(entry["key"]).strip(): entry for entry in parsed}
    selected_entries = tuple(entry_by_key[key] for key in cited)
    selected_bib = _select_exact_bibtex_entries(bibliography_text, set(cited))
    selected_check = parse_bibtex_entries(selected_bib.decode("utf-8"))
    if [entry["key"] for entry in selected_check] != [
        key for key in keys if key in set(cited)
    ]:
        raise StructuredStage23PublicationError(
            "verified bibliography source order mismatch"
        )
    return cited, selected_entries, selected_bib


def _select_exact_bibtex_entries(text: str, cited: set[str]) -> bytes:
    spans: list[tuple[str, str]] = []
    cursor = 0
    pattern = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")
    while True:
        match = pattern.search(text, cursor)
        if match is None:
            break
        depth = 0
        end = None
        for index in range(match.start(), len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise StructuredStage23PublicationError("BibTeX entry is unclosed")
        spans.append((match.group(1).strip(), text[match.start() : end]))
        cursor = end
    selected = [entry for key, entry in spans if key in cited]
    if len(selected) != len(cited):
        raise StructuredStage23PublicationError(
            "exact cited bibliography selection failed"
        )
    return (("\n\n".join(selected) + "\n").encode("utf-8"))


def _abstract_projection(
    capture: stage22._CapturedUpstream,
    selection_file: stage19.RichFileSnapshot,
    *,
    paper_sha256: str,
) -> Mapping[str, object]:
    snapshot = capture.base.snapshot.stage19_snapshot
    try:
        value = json.loads(
            selection_file.content.decode("utf-8"),
            object_pairs_hook=authority._reject_duplicate_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredStage23PublicationError(
            "Stage 19 selection is malformed"
        ) from exc
    sections = value.get("sections") if isinstance(value, dict) else None
    abstract = (
        next(
            (
                item
                for item in sections
                if isinstance(item, dict) and item.get("section_id") == "abstract"
            ),
            None,
        )
        if isinstance(sections, list)
        else None
    )
    if not isinstance(abstract, dict):
        raise StructuredStage23PublicationError("Stage 19 abstract selection missing")
    claim_ids = abstract.get("ordered_claim_ids")
    if (
        not isinstance(claim_ids, list)
        or not claim_ids
        or len(claim_ids) != len(set(claim_ids))
        or any(type(item) is not str for item in claim_ids)
    ):
        raise StructuredStage23PublicationError(
            "Stage 19 abstract claim closure invalid"
        )
    registry = build_scientific_claim_registry(snapshot.binding)
    by_id = {claim.claim_id: claim for claim in registry.claims}
    sentences: list[str] = []
    for claim_id in claim_ids:
        claim = by_id.get(claim_id)
        if claim is None or claim.section_id != "abstract":
            raise StructuredStage23PublicationError(
                "Stage 19 abstract claim membership mismatch"
            )
        sentences.append(claim.rendered_sentence)
    projection = {
        "schema_version": 1,
        "paper_sha256": paper_sha256,
        "abstract_claim_ids": claim_ids,
        "abstract_sentences": sentences,
    }
    if len(authority.canonical_json_bytes(projection)) > 16_384:
        raise StructuredStage23PublicationError(
            "abstract claim projection exceeds cap"
        )
    return projection


def _admit_relevance(
    owner: ReleaseGraphLock,
    capture: stage22._CapturedUpstream,
    llm: LLMClient | None,
    *,
    held_spec: transport.Stage23RelevanceTransportSpec | None,
) -> transport.Stage23RelevanceTransportSpec | None:
    if llm is None:
        if held_spec is not None:
            raise StructuredStage23PublicationError(
                "held relevance spec lost its active client"
            )
        return None
    if type(llm) is not LLMClient:
        raise StructuredStage23PublicationError("active LLMClient type mismatch")
    registration = _ACTIVE_CLIENTS.get(llm)
    current_owner = owner._require_active()
    canonical = capture.semantic_inputs.canonical_config
    llm_config = canonical.llm
    expected = (
        str(owner.run_dir.absolute()),
        owner._run_identity,
        current_owner,
        llm,
        llm_config.provider,
        llm_config.base_url,
        llm_config.wire_api,
        llm_config.primary_model,
        tuple(llm_config.fallback_models),
        id(llm.config.api_key),
    )
    actual = (
        None
        if registration is None
        else (
            registration.run_path,
            registration.run_identity,
            registration.owner,
            registration.client_ref(),
            registration.provider,
            registration.base_url,
            registration.wire_api,
            registration.primary_model,
            registration.fallback_models,
            registration.client_credential_source_identity,
        )
    )
    if actual != expected:
        raise StructuredStage23PublicationError(
            "active LLMClient registration mismatch"
        )
    client_config = llm.config
    if (
        client_config.base_url != llm_config.base_url
        or client_config.primary_model != llm_config.primary_model
        or client_config.wire_api != llm_config.wire_api
        or tuple(client_config.fallback_models) != tuple(llm_config.fallback_models)
        or client_config.fallback_url
        or client_config.fallback_api_key
        or client_config.extra_headers
        or getattr(llm, "_anthropic", None) is not None
    ):
        raise StructuredStage23PublicationError(
            "active LLMClient canonical configuration mismatch"
        )
    if held_spec is not None:
        if (
            registration is None
            or _SPEC_REGISTRATIONS.get(held_spec)
            is not registration.registration_identity
        ):
            raise StructuredStage23PublicationError(
                "held relevance transport registration changed"
            )
        if (
            held_spec.provider != llm_config.provider
            or held_spec.model != llm_config.primary_model
            or held_spec.base_url != llm_config.base_url
            or held_spec.wire_api != llm_config.wire_api
            or held_spec.wire_policy_id != transport.WIRE_POLICY_ID
        ):
            raise StructuredStage23PublicationError(
                "held relevance transport spec changed"
            )
        return held_spec
    bridge = canonical.metaclaw_bridge
    try:
        spec = transport.issue_relevance_transport_spec(
            provider=llm_config.provider,
            model=llm_config.primary_model,
            base_url=llm_config.base_url,
            wire_api=llm_config.wire_api,
            fallback_models=tuple(llm_config.fallback_models),
            bridge_enabled=bridge.enabled,
            extra_headers=client_config.extra_headers,
            fallback_url=client_config.fallback_url,
            anthropic_adapter=getattr(llm, "_anthropic", None),
            credential=registration.credential,
        )
        _SPEC_REGISTRATIONS[spec] = registration.registration_identity
        return spec
    except transport.Stage23TransportError as exc:
        raise StructuredStage23PublicationError(
            f"structured relevance admission failed: {exc}"
        ) from exc


def transition_stage23_pre_admission_context(
    lease: ReleaseGraphLock,
    context: object,
) -> Stage23AttemptContext:
    record = _require_pre_context(lease, context)
    assert isinstance(context, Stage23PreAdmissionContext)
    try:
        current = _capture_upstream(
            lease,
            llm=(
                None
                if record.capture.relevance_spec is None
                else _client_for_spec(record.capture.relevance_spec, record.owner)
            ),
            held_spec=record.capture.relevance_spec,
        )
        try:
            _require_same_upstream(record.capture, current)
        finally:
            current.close()
    except Exception as exc:
        _expire_pre_context(context, record)
        raise StructuredStage23PublicationError(
            f"structured Stage 23 pre-admission replay failed: {exc}"
        ) from exc
    _PRE_CONTEXTS.pop(context, None)
    record.finalizer.detach()
    record.phase = "consumed"
    namespace: BoundOutputNamespace | None = None
    try:
        namespace = _acquire_structured_stage23_namespace(record.owner)
        require_namespace_owned_by_epoch(namespace, lease, _STAGE_NAME)
        attempt = Stage23AttemptContext(
            authority_token=_CONSTRUCTION_AUTHORITY,
            token=object(),
            writer_owner=record.owner,
            run_path=str(record.owner.run_dir.absolute()),
            run_identity=record.owner._run_identity,
            stage_identity=namespace._stage_identity,
            upstream_identity=record.capture.identity_tuple,
        )
        _ATTEMPT_CONTEXTS[attempt] = _AttemptRecord(
            attempt._token, record.owner, record.capture, namespace
        )
        return attempt
    except Exception as exc:
        if namespace is not None:
            namespace.close()
        _drop_spec_registration(record.capture.relevance_spec)
        record.capture.close()
        raise StructuredStage23PublicationError(
            f"structured Stage 23 namespace transition failed: {exc}"
        ) from exc


def _client_for_spec(
    spec: transport.Stage23RelevanceTransportSpec,
    owner: ReleaseGraphLock,
) -> LLMClient:
    registration_identity = _SPEC_REGISTRATIONS.get(spec)
    registration = (
        None
        if registration_identity is None
        else _ACTIVE_REGISTRATIONS.get(registration_identity)
    )
    client = None if registration is None else registration.client_ref()
    active_owner = owner._require_active()
    if (
        registration is None
        or client is None
        or registration.registration_identity is not registration_identity
        or _ACTIVE_CLIENTS.get(client) is not registration
        or registration.run_path != str(owner.run_dir.absolute())
        or registration.run_identity != owner._run_identity
        or registration.owner is not active_owner
        or registration.provider != spec.provider
        or registration.base_url != spec.base_url
        or registration.wire_api != spec.wire_api
        or registration.primary_model != spec.model
        or registration.fallback_models
        or registration.credential != spec._credential
    ):
        raise StructuredStage23PublicationError(
            "active relevance client registration mismatch"
        )
    return client


def _acquire_structured_stage23_namespace(
    owner: ReleaseGraphLock,
) -> BoundOutputNamespace:
    run_fd = os.dup(owner._run_fd)
    stage_fd = -1
    created: tuple[int, int] | None = None
    try:
        parent = os.fstat(run_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or (parent.st_dev, parent.st_ino) != owner._run_identity
        ):
            raise StructuredStage23PublicationError("held run parent mismatch")
        try:
            os.stat(_STAGE_NAME, dir_fd=run_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StructuredStage23PublicationError(
                "canonical Stage 23 namespace already exists"
            )
        os.mkdir(_STAGE_NAME, 0o700, dir_fd=run_fd)
        named = os.stat(_STAGE_NAME, dir_fd=run_fd, follow_symlinks=False)
        created = (named.st_dev, named.st_ino)
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        stage_fd = os.open(_STAGE_NAME, flags, dir_fd=run_fd)
        held = os.fstat(stage_fd)
        if (
            not stat.S_ISDIR(held.st_mode)
            or (held.st_dev, held.st_ino) != created
            or held.st_dev != parent.st_dev
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o700
            or held.st_nlink < 2
            or os.listdir(stage_fd)
        ):
            raise StructuredStage23PublicationError(
                "new Stage 23 directory validation failed"
            )
        return BoundOutputNamespace(
            owner.run_dir,
            owner.run_dir / _STAGE_NAME,
            _STAGE_NAME,
            run_fd,
            stage_fd,
            owner._run_identity,
            created,
        )
    except Exception:
        if stage_fd >= 0:
            os.close(stage_fd)
        if created is not None:
            check_fd = -1
            try:
                current = os.stat(
                    _STAGE_NAME, dir_fd=run_fd, follow_symlinks=False
                )
                check_fd = os.open(
                    _STAGE_NAME,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=run_fd,
                )
                if (current.st_dev, current.st_ino) == created and not os.listdir(
                    check_fd
                ):
                    os.rmdir(_STAGE_NAME, dir_fd=run_fd)
            except OSError:
                pass
            finally:
                if check_fd >= 0:
                    os.close(check_fd)
        os.close(run_fd)
        raise


def produce_structured_stage23(
    lease: ReleaseGraphLock,
    context: Stage23AttemptContext,
    *,
    metadata_outbound: Callable[[transport.WireRequest], Iterable[bytes]],
    relevance_outbound: (
        Callable[[transport.WireRequest], Iterable[bytes]] | None
    ),
) -> ProvisionalStage23Result:
    record = _require_attempt_context(lease, context)
    if record.phase != "namespace_bound":
        raise StructuredStage23PublicationError("Stage 23 attempt phase mismatch")
    _verify_manifest_first_absence(record)
    _advance(record, "namespace_bound", "invalidated")
    current = _capture_current(record)
    try:
        _require_same_upstream(record.initial, current)
    finally:
        current.close()
    _advance(record, "invalidated", "snapshot_a")
    metadata: list[Mapping[str, object]] = []
    for key, entry in zip(
        record.initial.cited_keys,
        record.initial.bibliography_entries,
        strict=True,
    ):
        try:
            metadata.append(
                transport.verify_metadata(
                    cite_key=key,
                    entry=entry,
                    outbound=metadata_outbound,
                )
            )
        except Exception as exc:
            raise StructuredStage23PublicationError(
                f"citation metadata verification failed: {exc}"
            ) from exc
    record.metadata = tuple(metadata)
    _advance(record, "snapshot_a", "metadata_verified")
    if record.initial.relevance_spec is None:
        relevance = authority.relevance_fixture(
            "missing", cited_keys=record.initial.cited_keys
        )
    else:
        if relevance_outbound is None:
            raise StructuredStage23PublicationError(
                "relevance outbound seam is missing"
            )
        relevance = transport.execute_relevance(
            spec=record.initial.relevance_spec,
            paper=_ref(record.initial.paper),
            claim_projection=record.initial.claim_projection,
            cited_keys=record.initial.cited_keys,
            metadata=record.metadata,
            outbound=relevance_outbound,
        )
    record.relevance = authority.validate_relevance(
        relevance, record.initial.cited_keys
    )
    _advance(record, "metadata_verified", "relevance_complete")
    quality = record.initial.base.base.snapshot.authority_inputs.quality_outcome
    claim_scope = _claim_scope(record.initial)
    publishable, outcome = authority.derive_outcome(
        claim_scope=claim_scope,
        upstream_quality=quality,
        relevance=record.relevance,
        cited_keys=record.initial.cited_keys,
    )
    if not publishable or outcome is None:
        raise StructuredStage23PublicationError(
            "claim-scope outcome is non-publishable"
        )
    record.outcome = outcome
    report = _build_report(record, claim_scope)
    payloads = {
        "verification_report.json": authority.canonical_json_bytes(report),
        "references_verified.bib": record.initial.verified_bibliography,
        "paper_final_verified.md": record.initial.paper.content,
    }
    record.held_files = {}
    for name in _PAYLOAD_NAMES:
        record.held_files[name] = _create_held_file(
            record.namespace._stage_fd,
            name,
            payloads[name],
            logical_path=f"stage-23/{name}",
        )
    _advance(record, "relevance_complete", "payloads_published")
    _verify_payloads(record)
    current = _capture_current(record)
    try:
        _require_same_upstream(record.initial, current)
    finally:
        current.close()
    _advance(record, "payloads_published", "source_fixpoint")
    manifest_payload = _build_manifest(record, report)
    record.manifest = _create_held_file(
        record.namespace._stage_fd,
        _MANIFEST_NAME,
        authority.canonical_json_bytes(manifest_payload),
        logical_path=f"stage-23/{_MANIFEST_NAME}",
    )
    _advance(record, "source_fixpoint", "manifest_published")
    _verify_complete_publication(record)
    snapshot = _capture_final_state(record)
    if _capture_final_state(record) != snapshot:
        raise StructuredStage23PublicationError(
            "Stage 23 second final capture mismatch"
        )
    record.final_snapshot = snapshot
    _advance(record, "manifest_published", "provisional_done")
    return ProvisionalStage23Result(
        StageStatus.DONE,
        authority.STRUCTURED_STAGE23_ARTIFACTS,
        authority.STRUCTURED_STAGE23_EVIDENCE_REFS,
        outcome == "degraded",
        context,
    )


def _build_report(record: _AttemptRecord, claim_scope: str) -> Mapping[str, object]:
    assert record.metadata is not None
    assert record.relevance is not None
    assert record.outcome is not None
    return {
        "schema_version": 2,
        "publication_stage_id": "stage23",
        "paper": _ref(record.initial.paper),
        "bibliography": _ref(record.initial.bibliography),
        "cited_count": len(record.initial.cited_keys),
        "cited_keys": list(record.initial.cited_keys),
        "claim_scope": claim_scope,
        "provider_policy": copy.deepcopy(authority.PROVIDER_POLICY),
        "relevance_policy": copy.deepcopy(authority.RELEVANCE_POLICY),
        "citations": [dict(item) for item in record.metadata],
        "relevance": dict(record.relevance),
        "outcome": record.outcome,
        "generated": record.initial.stage22_manifest_payload["generated"],
    }


def _build_manifest(
    record: _AttemptRecord,
    report: Mapping[str, object],
) -> Mapping[str, object]:
    assert record.held_files is not None
    assert record.outcome is not None
    capture = record.initial
    inputs = capture.base.base.snapshot.authority_inputs
    stage19_manifest = stage22._source(
        capture.base, "stage-19/scientific_claim_authority_manifest.json"
    )
    stage20_manifest = stage22._source(
        capture.base, "stage-20/quality_gate_manifest.json"
    )
    stage21_manifest = stage22._source(capture.base, "stage-21/bundle_index.json")
    evidence = stage22._source(
        capture.base,
        capture.base.base.snapshot.stage19_snapshot.binding.canonical_experiment_evidence_path,
    )
    degradation = (
        None
        if inputs.quality_outcome == "passed"
        else _ref(stage22._source(capture.base, "degradation_signal.json"))
    )
    refs = {
        name: _bytes_ref(f"stage-23/{name}", _read_held_file(held))
        for name, held in record.held_files.items()
    }
    outputs = [
        {
            "role": role,
            "logical_name": None,
            **refs[name],
        }
        for role, name in (
            ("verification_report", "verification_report.json"),
            ("verified_bibliography", "references_verified.bib"),
            ("verified_paper", "paper_final_verified.md"),
        )
    ]
    return {
        "schema_version": 2,
        "publication_stage_id": "stage23",
        "publication_mode": "structured-scientific-claim-v1",
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": _capability_dict(),
        "generation_binding_sha256": inputs.generation_binding_sha256,
        "canonical_experiment_evidence": _ref(evidence),
        "cfs": {"schema_version": 1, "sha256": inputs.cfs_sha256},
        "source_stage19_manifest": _ref(stage19_manifest),
        "source_stage20_manifest": _ref(stage20_manifest),
        "source_stage21_manifest": _ref(stage21_manifest),
        "source_stage22_manifest": _ref(capture.stage22_manifest),
        "source_paper": _ref(capture.paper),
        "source_bibliography": _ref(capture.bibliography),
        "source_latex": _ref(capture.latex),
        "quality_outcome": inputs.quality_outcome,
        "degradation_signal": degradation,
        "claim_scope": report["claim_scope"],
        "verification_policy": copy.deepcopy(authority.VERIFICATION_POLICY),
        "outcome": record.outcome,
        "verification_report": refs["verification_report.json"],
        "verified_bibliography": refs["references_verified.bib"],
        "verified_paper": refs["paper_final_verified.md"],
        "output_count": 3,
        "outputs": outputs,
        "generated": capture.stage22_manifest_payload["generated"],
    }


def validate_structured_stage23_immediate_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage23Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage23AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage23PublicationError(
            "Stage 23 immediate snapshot changed"
        )
    current = _capture_current(record)
    try:
        _require_same_upstream(record.initial, current)
    finally:
        current.close()
    _advance(record, "provisional_done", "immediate_validated")


def validate_structured_stage23_terminal_postcondition(
    lease: ReleaseGraphLock,
    provisional: ProvisionalStage23Result,
    *,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    context: Stage23AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    _validate_provisional(record, provisional, artifacts, evidence_refs)
    _verify_complete_publication(record)
    if _capture_final_state(record) != record.final_snapshot:
        raise StructuredStage23PublicationError(
            "Stage 23 terminal snapshot changed"
        )
    current = _capture_current(record)
    try:
        _require_same_upstream(record.initial, current)
    finally:
        current.close()
    _advance(record, "immediate_validated", "terminal_validated")


def clear_structured_stage23_context(
    lease: ReleaseGraphLock,
    context: Stage23AttemptContext,
) -> None:
    record = _require_attempt_context(lease, context)
    if record.phase != "terminal_validated":
        raise StructuredStage23PublicationError(
            "Stage 23 context is not terminal validated"
        )
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt_record(record)


def fail_structured_stage23_attempt(
    lease: ReleaseGraphLock,
    context: Stage23AttemptContext,
) -> tuple[str, ...]:
    record = _require_attempt_context(lease, context)
    if record.phase == "relevance_complete":
        _advance(record, "relevance_complete", "failed_cleanup")
    errors = _cleanup_attempt_outputs(record)
    _ATTEMPT_CONTEXTS.pop(context, None)
    record.phase = "cleared"
    _close_attempt_record(record)
    return errors


def context_phase(context: object) -> str:
    if isinstance(context, Stage23PreAdmissionContext):
        record = _PRE_CONTEXTS.get(context)
    elif isinstance(context, Stage23AttemptContext):
        record = _ATTEMPT_CONTEXTS.get(context)
    else:
        raise StructuredStage23PublicationError("unknown Stage 23 context")
    if record is None:
        return "cleared"
    return record.phase


def execute_structured_stage23_verification(
    run_dir: Path,
    stage_dir: Path,
    *,
    llm: LLMClient | None,
    metadata_outbound: Callable[[transport.WireRequest], Iterable[bytes]],
    relevance_outbound: (
        Callable[[transport.WireRequest], Iterable[bytes]] | None
    ),
):
    """Public/direct entry; the incomplete-capability guard is first."""

    capability.require_complete_structured_capability(
        "execute_structured_stage23_verification"
    )
    try:
        with ReleaseGraphLock.acquire(
            run_dir, "execute_structured_stage23_verification", mode="write"
        ) as lease:
            from researchclaw.pipeline.executor import (
                _execute_structured_stage23_with_pre_admission_private,
            )

            return _execute_structured_stage23_with_pre_admission_private(
                lease,
                llm=llm,
                metadata_outbound=metadata_outbound,
                relevance_outbound=relevance_outbound,
                canonical_stage_dir=stage_dir == run_dir / _STAGE_NAME,
            )
    except Exception:  # noqa: BLE001
        return StageResult(
            stage=Stage.CITATION_VERIFY,
            status=StageStatus.FAILED,
            artifacts=(),
            evidence_refs=(),
            error="Structured Stage 23 failed before attempt",
            decision="retry",
        )


def _verify_manifest_first_absence(record: _AttemptRecord) -> None:
    record.namespace.assert_canonical()
    for name in (_MANIFEST_NAME, *_PAYLOAD_NAMES):
        try:
            os.stat(name, dir_fd=record.namespace._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise StructuredStage23PublicationError(
            f"post-acquisition Stage 23 collision: {name}"
        )
    if record.namespace.direct_entries() or os.listdir(record.namespace._stage_fd):
        raise StructuredStage23PublicationError(
            "post-acquisition Stage 23 namespace is not empty"
        )


def _create_held_file(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    logical_path: str,
) -> _HeldFile:
    if "/" in name or name in {"", ".", ".."}:
        raise StructuredStage23PublicationError("unsafe Stage 23 leaf name")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o644, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short Stage 23 write")
            view = view[written:]
        os.fsync(descriptor)
        held_info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (held_info.st_dev, held_info.st_ino)
        if (
            not stat.S_ISREG(held_info.st_mode)
            or held_info.st_nlink != 1
            or held_info.st_size != len(content)
            or identity != (named.st_dev, named.st_ino)
        ):
            raise StructuredStage23PublicationError(
                f"created file identity mismatch: {logical_path}"
            )
        held = _HeldFile(
            parent_fd, name, descriptor, identity, logical_path, content
        )
        if _read_held_file(held) != content:
            raise StructuredStage23PublicationError(
                f"created file readback mismatch: {logical_path}"
            )
        return held
    except Exception:
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            held_info = os.fstat(descriptor)
            if (named.st_dev, named.st_ino) == (
                held_info.st_dev,
                held_info.st_ino,
            ):
                os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(descriptor)
        raise


def _read_held_file(held: _HeldFile) -> bytes:
    before = os.fstat(held.descriptor)
    named = os.stat(held.name, dir_fd=held.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_dev, before.st_ino) != held.identity
        or (named.st_dev, named.st_ino) != held.identity
    ):
        raise StructuredStage23PublicationError(
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
        (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or len(content) != after.st_size
    ):
        raise StructuredStage23PublicationError(
            f"held file changed during read: {held.logical_path}"
        )
    return content


def _verify_payloads(record: _AttemptRecord) -> None:
    assert record.held_files is not None
    if set(record.namespace.direct_entries()) != set(_PAYLOAD_NAMES):
        raise StructuredStage23PublicationError(
            "Stage 23 payload namespace mismatch"
        )
    for name, held in record.held_files.items():
        if _read_held_file(held) != held.expected_bytes:
            raise StructuredStage23PublicationError(
                f"Stage 23 payload changed: {name}"
            )
    if (
        record.held_files["paper_final_verified.md"].expected_bytes
        != record.initial.paper.content
    ):
        raise StructuredStage23PublicationError("verified paper byte mismatch")
    if (
        record.held_files["references_verified.bib"].expected_bytes
        != record.initial.verified_bibliography
    ):
        raise StructuredStage23PublicationError(
            "verified bibliography byte mismatch"
        )


def _verify_complete_publication(record: _AttemptRecord) -> None:
    _verify_payloads_with_manifest(record)
    assert record.manifest is not None
    manifest_bytes = _read_held_file(record.manifest)
    try:
        manifest = authority.parse_verification_manifest_v2(manifest_bytes)
    except authority.StructuredStage23AuthorityError as exc:
        raise StructuredStage23PublicationError(
            "Stage 23 manifest is malformed"
        ) from exc
    report_bytes = _read_held_file(
        record.held_files["verification_report.json"]  # type: ignore[index]
    )
    try:
        report = authority.parse_verification_report_v2(report_bytes)
    except authority.StructuredStage23AuthorityError as exc:
        raise StructuredStage23PublicationError(
            "Stage 23 verification report is malformed"
        ) from exc
    expected_report = _build_report(record, _claim_scope(record.initial))
    if report != expected_report:
        raise StructuredStage23PublicationError(
            "Stage 23 report differs from held authority state"
        )
    assert record.metadata is not None and record.relevance is not None
    for key, entry, telemetry in zip(
        record.initial.cited_keys,
        record.initial.bibliography_entries,
        record.metadata,
        strict=True,
    ):
        route, endpoint, identity = authority.select_route(entry)
        request = transport.build_metadata_request(route, identity)
        if (
            telemetry.get("cite_key") != key
            or telemetry.get("route_class") != route
            or telemetry.get("endpoint_class") != endpoint
            or telemetry.get("request_sha256") != request.request_sha256
            or telemetry.get("outbound_count") != 1
            or telemetry.get("status") != "verified"
        ):
            raise StructuredStage23PublicationError(
                f"Stage 23 metadata route replay mismatch: {key}"
            )
    if record.initial.relevance_spec is not None:
        relevance_request = transport._build_relevance_request(
            spec=record.initial.relevance_spec,
            paper=_ref(record.initial.paper),
            claim_projection=record.initial.claim_projection,
            cited_keys=record.initial.cited_keys,
            metadata=record.metadata,
        )
        if record.relevance.get("request_sha256") != relevance_request.request_sha256:
            raise StructuredStage23PublicationError(
                "Stage 23 relevance request replay mismatch"
            )
    publishable, outcome = authority.derive_outcome(
        claim_scope=report["claim_scope"],
        upstream_quality=record.initial.base.base.snapshot.authority_inputs.quality_outcome,
        relevance=report["relevance"],
        cited_keys=record.initial.cited_keys,
    )
    if not publishable or outcome != report["outcome"] or outcome != record.outcome:
        raise StructuredStage23PublicationError(
            "Stage 23 report outcome matrix mismatch"
        )
    rebuilt = _build_manifest(record, report)
    if manifest != rebuilt:
        raise StructuredStage23PublicationError(
            "Stage 23 manifest differs from independent rebuild"
        )


def _verify_payloads_with_manifest(record: _AttemptRecord) -> None:
    assert record.held_files is not None and record.manifest is not None
    if set(record.namespace.direct_entries()) != set(_FORMAL_NAMES):
        raise StructuredStage23PublicationError(
            "Stage 23 exact four-file namespace mismatch"
        )
    for held in (*record.held_files.values(), record.manifest):
        if _read_held_file(held) != held.expected_bytes:
            raise StructuredStage23PublicationError(
                f"Stage 23 stored bytes changed: {held.logical_path}"
            )


def _capture_final_state(record: _AttemptRecord) -> tuple[object, ...]:
    _verify_complete_publication(record)
    assert record.held_files is not None and record.manifest is not None
    return (
        record.initial.identity_tuple,
        record.namespace._run_identity,
        record.namespace._stage_identity,
        tuple(
            (
                name,
                held.identity,
                hashlib.sha256(_read_held_file(held)).hexdigest(),
                len(_read_held_file(held)),
            )
            for name, held in record.held_files.items()
        ),
        (
            record.manifest.identity,
            hashlib.sha256(_read_held_file(record.manifest)).hexdigest(),
            len(_read_held_file(record.manifest)),
        ),
        record.outcome,
    )


def _validate_provisional(
    record: _AttemptRecord,
    provisional: ProvisionalStage23Result,
    artifacts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> None:
    if (
        provisional.status is not StageStatus.DONE
        or provisional.context not in _ATTEMPT_CONTEXTS
        or provisional.artifacts != authority.STRUCTURED_STAGE23_ARTIFACTS
        or provisional.evidence_refs != authority.STRUCTURED_STAGE23_EVIDENCE_REFS
        or artifacts != authority.STRUCTURED_STAGE23_ARTIFACTS
        or evidence_refs != authority.STRUCTURED_STAGE23_EVIDENCE_REFS
        or provisional.degraded is not (record.outcome == "degraded")
    ):
        raise StructuredStage23PublicationError(
            "Stage 23 provisional tuple/status mismatch"
        )


def _cleanup_attempt_outputs(record: _AttemptRecord) -> tuple[str, ...]:
    errors: list[str] = []
    ordered = (
        (record.manifest,)
        + tuple((record.held_files or {}).get(name) for name in _PAYLOAD_NAMES)
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
                errors.append(f"{held.logical_path}: file identity collision")
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
            errors.append("stage-23: canonical name identity collision")
        elif os.listdir(record.namespace._stage_fd):
            errors.append("stage-23: owned namespace remains nonempty")
        else:
            os.rmdir(_STAGE_NAME, dir_fd=record.namespace._run_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        errors.append(f"stage-23: {exc}")
    return tuple(errors)


def _capture_current(record: _AttemptRecord) -> _CapturedUpstream:
    return _capture_upstream(
        record.owner,
        llm=(
            None
            if record.initial.relevance_spec is None
            else _client_for_spec(record.initial.relevance_spec, record.owner)
        ),
        held_spec=record.initial.relevance_spec,
    )


def _claim_scope(capture: _CapturedUpstream) -> str:
    from researchclaw.experiment_runtime.contract import load_contract_bytes

    evidence = capture.base.base.snapshot.stage19_snapshot.binding.evidence
    return load_contract_bytes(evidence.experiment_contract_bytes).claim_scope


def _ref(item: stage19.RichFileSnapshot) -> dict[str, object]:
    return {"path": item.path, "sha256": item.sha256, "size": len(item.content)}


def _bytes_ref(path: str, content: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _require_pre_context(
    lease: ReleaseGraphLock,
    context: object,
) -> _PreRecord:
    if type(context) is not Stage23PreAdmissionContext:
        raise StructuredStage23PublicationError(
            "Stage 23 pre-admission context type mismatch"
        )
    record = _PRE_CONTEXTS.get(context)
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    if (
        record is None
        or record.token is not context._token
        or record.owner is not owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or context.logical_stage_id != "stage23"
        or context.structured_capability_snapshot != (1, 1, 1, 0)
        or context.upstream_identity != record.capture.identity_tuple
        or record.phase != "pre_admission"
    ):
        raise StructuredStage23PublicationError(
            "Stage 23 pre-admission context is stale or forged"
        )
    return record


def _require_attempt_context(
    lease: ReleaseGraphLock,
    context: object,
) -> _AttemptRecord:
    if type(context) is not Stage23AttemptContext:
        raise StructuredStage23PublicationError("Stage 23 attempt context type mismatch")
    record = _ATTEMPT_CONTEXTS.get(context)
    writer = require_active_writer_epoch(lease.run_dir, lease)
    owner = writer._require_active()
    if (
        record is None
        or record.token is not context._token
        or record.owner is not owner
        or context._writer_owner is not owner
        or context._run_path != str(owner.run_dir.absolute())
        or context._run_identity != owner._run_identity
        or context._stage_identity != record.namespace._stage_identity
        or context.logical_stage_id != "stage23"
        or context.structured_capability_snapshot != (1, 1, 1, 0)
        or context.upstream_identity != record.initial.identity_tuple
    ):
        raise StructuredStage23PublicationError(
            "Stage 23 attempt context is stale or forged"
        )
    record.namespace.assert_canonical()
    require_namespace_owned_by_epoch(record.namespace, lease, _STAGE_NAME)
    return record


def _require_same_upstream(
    expected: _CapturedUpstream,
    current: _CapturedUpstream,
) -> None:
    if expected.identity_tuple != current.identity_tuple:
        raise StructuredStage23PublicationError(
            "structured Stage 23 upstream generation changed"
        )


def _finalize_pre_admission_capture(capture: _CapturedUpstream) -> None:
    _drop_spec_registration(capture.relevance_spec)
    capture.close()


def _expire_pre_context(
    context: Stage23PreAdmissionContext,
    record: _PreRecord,
) -> None:
    _PRE_CONTEXTS.pop(context, None)
    record.finalizer.detach()
    record.phase = "expired"
    _drop_spec_registration(record.capture.relevance_spec)
    record.capture.close()


def _advance(record: _AttemptRecord, current: str, target: str) -> None:
    if record.phase != current:
        raise StructuredStage23PublicationError(
            f"Stage 23 phase mismatch: expected {current}, got {record.phase}"
        )
    record.phase = target


def _close_attempt_record(record: _AttemptRecord) -> None:
    held = tuple((record.held_files or {}).values()) + (
        (record.manifest,) if record.manifest is not None else ()
    )
    for item in held:
        try:
            os.close(item.descriptor)
        except OSError:
            pass
    record.namespace.close()
    _drop_spec_registration(record.initial.relevance_spec)
    record.initial.close()


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
        raise StructuredStage23PublicationError(
            "private structured Stage 23 requires exact capability 1110"
        )
