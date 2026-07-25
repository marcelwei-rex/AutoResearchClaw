"""Strict contracts and deterministic construction for scientific claims.

This B1-B2 module deliberately does not activate any pipeline stage.  It
validates canonical bytes, closed record shapes, trusted source/CFS/generation
bindings, self-excluding identities, code-owned construction and rendering,
and section-aware semantic selection.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    parse_contract_bytes,
    validate_contract_structure_dict,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalEvidenceArtifact,
    CanonicalExperimentEvidence,
    CanonicalExperimentEvidenceError,
    _parse_json_value,
    canonical_authority_json_text,
    canonical_decimal,
)
from researchclaw.pipeline.canonical_fact_sheet import (
    CFSIntegrityError,
    build_canonical_fact_sheet,
    canonical_fact_sheet_sha256,
)
from researchclaw.pipeline.independent_release_reconstruction import (
    IndependentReleaseReconstructionError,
    validate_release_authority_path,
)
from researchclaw.pipeline.stage13_domain_evaluator import (
    parse_domain_evaluator_refinement_result_set,
)
from researchclaw.pipeline.stage14_domain_evaluator import (
    parse_domain_evaluator_candidate,
    parse_domain_evaluator_canonical_manifest,
)


CLAIM_POLICY_ID = "structured-scientific-claim-v1"
SCHEMA_VERSION = 1
GENERATION_BINDING_POLICY_VERSION = 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REGISTRY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]*")
_OBJECT_KINDS = frozenset({"decimal", "string", "identifier", "boolean"})
_SECTION_IDS = frozenset(
    {"abstract", "results", "discussion", "limitations", "conclusion"}
)
_COMPLETE_CFS_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_origin",
        "claim_scope",
        "bound_labels",
        "conditions",
        "seeds",
        "circuit_families",
        "variants_per_family",
        "variant_ids",
        "counts",
        "metric_keys",
        "primary_metric",
        "condition_aggregates",
        "per_seed_aggregates",
        "scale",
        "runtime",
        "derived_facts",
        "provenance",
        "observation_rows",
    }
)
_PRIMARY_METRIC_FIELDS = frozenset(
    {"condition", "key", "aggregation", "observation_set", "value"}
)

_EVIDENCE_FACT_FIELDS = frozenset(
    {
        "schema_version",
        "fact_id",
        "fact_kind",
        "subject_id",
        "predicate_id",
        "object_kind",
        "object_value",
        "unit_id",
        "source_path",
        "source_sha256",
        "source_json_pointer",
        "cfs_schema_version",
        "cfs_sha256",
        "generation_binding_sha256",
    }
)
_SCIENTIFIC_CLAIM_FIELDS = frozenset(
    {
        "schema_version",
        "claim_id",
        "claim_kind",
        "section_id",
        "evidence_fact_ids",
        "renderer_template_id",
        "renderer_slot_fact_ids",
        "rendered_sentence",
        "rendered_sentence_sha256",
        "mandatory",
        "source_path",
        "source_sha256",
        "cfs_schema_version",
        "cfs_sha256",
        "generation_binding_sha256",
    }
)
_SELECTION_FIELDS = frozenset(
    {
        "selected_claim_ids",
        "ordered_claim_ids",
        "connector_template_ids",
    }
)
_RENDERED_CITATION_RE = re.compile(
    r"\[[^\]]+\]|\([^()]*\b(?:19|20)[0-9]{2}[a-z]?\b[^()]*\)"
)
_RENDERED_TERMINATOR_RE = re.compile(r"[.!?](?=$|\s)")


@dataclass(frozen=True)
class _EvidenceFactSpec:
    fact_kind: str
    subject_id: str
    predicate_id: str
    object_kind: str
    unit_id: str
    cfs_key: str
    source_json_pointer: str


@dataclass(frozen=True)
class _RendererTemplateSpec:
    template_id: str
    claim_kind: str
    section_id: str
    mandatory: bool
    slot_fact_kinds: tuple[str, ...]
    literal_parts: tuple[str, ...]


_EVIDENCE_FACT_REGISTRY = (
    _EvidenceFactSpec(
        "primary_condition",
        "primary_metric",
        "condition",
        "string",
        "NONE",
        "condition",
        "/primary_metric/condition",
    ),
    _EvidenceFactSpec(
        "primary_metric_key",
        "primary_metric",
        "metric_key",
        "string",
        "NONE",
        "key",
        "/primary_metric/key",
    ),
    _EvidenceFactSpec(
        "primary_aggregation",
        "primary_metric",
        "aggregation_policy",
        "string",
        "NONE",
        "aggregation",
        "/primary_metric/aggregation",
    ),
    _EvidenceFactSpec(
        "primary_observation_set",
        "primary_metric",
        "observation_set",
        "string",
        "NONE",
        "observation_set",
        "/primary_metric/observation_set",
    ),
    _EvidenceFactSpec(
        "primary_metric_value",
        "primary_metric",
        "value",
        "decimal",
        "NONE",
        "value",
        "/primary_metric/value",
    ),
)
_RENDERER_TEMPLATE_REGISTRY = (
    _RendererTemplateSpec(
        template_id="abstract.primary_metric.v1",
        claim_kind="primary_metric_summary",
        section_id="abstract",
        mandatory=True,
        slot_fact_kinds=(
            "primary_metric_key",
            "primary_metric_value",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        literal_parts=(
            "The governed evaluation reported ",
            " of ",
            " for primary condition ",
            " under ",
            " over ",
            ".",
        ),
    ),
    _RendererTemplateSpec(
        template_id="result.primary_metric.v1",
        claim_kind="primary_metric_result",
        section_id="results",
        mandatory=True,
        slot_fact_kinds=tuple(spec.fact_kind for spec in _EVIDENCE_FACT_REGISTRY),
        literal_parts=(
            "For primary condition ",
            ", ",
            " under ",
            " over ",
            " was ",
            ".",
        ),
    ),
    _RendererTemplateSpec(
        template_id="result.primary_metric_scope.v1",
        claim_kind="primary_metric_scope",
        section_id="results",
        mandatory=False,
        slot_fact_kinds=(
            "primary_aggregation",
            "primary_observation_set",
            "primary_condition",
        ),
        literal_parts=(
            "The primary result uses ",
            " over ",
            " for condition ",
            ".",
        ),
    ),
    _RendererTemplateSpec(
        template_id="discussion.primary_metric_scope.v1",
        claim_kind="primary_metric_scope_interpretation",
        section_id="discussion",
        mandatory=True,
        slot_fact_kinds=(
            "primary_metric_key",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        literal_parts=(
            "Interpretation of the primary ",
            " result for ",
            " is scoped to ",
            " over ",
            ".",
        ),
    ),
    _RendererTemplateSpec(
        template_id="limitation.primary_metric_scope.v1",
        claim_kind="primary_metric_scope_limitation",
        section_id="limitations",
        mandatory=True,
        slot_fact_kinds=(
            "primary_metric_key",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        literal_parts=(
            "The reported primary ",
            " result pertains to ",
            " under ",
            " over ",
            ".",
        ),
    ),
    _RendererTemplateSpec(
        template_id="conclusion.primary_metric.v1",
        claim_kind="primary_metric_conclusion",
        section_id="conclusion",
        mandatory=True,
        slot_fact_kinds=(
            "primary_metric_key",
            "primary_metric_value",
            "primary_condition",
            "primary_aggregation",
            "primary_observation_set",
        ),
        literal_parts=(
            "The governed evidence records ",
            " of ",
            " for ",
            " under ",
            " over ",
            ".",
        ),
    ),
)
_METRIC_DISPLAY_LABELS = (("auprc", "AUPRC"),)


class ScientificClaimAuthorityError(ValueError):
    """B1 scientific-claim contract or identity validation failed."""


@dataclass(frozen=True)
class ScientificClaimGenerationBinding:
    """Trusted binding rebuilt from one replayed evidence generation."""

    canonical_experiment_evidence_path: str
    canonical_experiment_evidence_sha256: str
    experiment_contract_path: str
    experiment_contract_sha256: str
    run_config_path: str
    run_config_sha256: str
    cfs_schema_version: int
    cfs_sha256: str
    generation_binding_sha256: str
    evidence: CanonicalExperimentEvidence = field(repr=False, compare=False)


@dataclass(frozen=True)
class ScientificClaimSource:
    """Exact bytes captured for one safe run-relative source artifact."""

    path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class EvidenceFact:
    schema_version: int
    fact_id: str
    fact_kind: str
    subject_id: str
    predicate_id: str
    object_kind: str
    object_value: str
    unit_id: str
    source_path: str
    source_sha256: str
    source_json_pointer: str
    cfs_schema_version: int
    cfs_sha256: str
    generation_binding_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fact_id": self.fact_id,
            "fact_kind": self.fact_kind,
            "subject_id": self.subject_id,
            "predicate_id": self.predicate_id,
            "object_kind": self.object_kind,
            "object_value": self.object_value,
            "unit_id": self.unit_id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_json_pointer": self.source_json_pointer,
            "cfs_schema_version": self.cfs_schema_version,
            "cfs_sha256": self.cfs_sha256,
            "generation_binding_sha256": self.generation_binding_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class ScientificClaimRecord:
    schema_version: int
    claim_id: str
    claim_kind: str
    section_id: str
    evidence_fact_ids: tuple[str, ...]
    renderer_template_id: str
    renderer_slot_fact_ids: tuple[str, ...]
    rendered_sentence: str
    rendered_sentence_sha256: str
    mandatory: bool
    source_path: str
    source_sha256: str
    cfs_schema_version: int
    cfs_sha256: str
    generation_binding_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "claim_kind": self.claim_kind,
            "section_id": self.section_id,
            "evidence_fact_ids": list(self.evidence_fact_ids),
            "renderer_template_id": self.renderer_template_id,
            "renderer_slot_fact_ids": list(self.renderer_slot_fact_ids),
            "rendered_sentence": self.rendered_sentence,
            "rendered_sentence_sha256": self.rendered_sentence_sha256,
            "mandatory": self.mandatory,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "cfs_schema_version": self.cfs_schema_version,
            "cfs_sha256": self.cfs_sha256,
            "generation_binding_sha256": self.generation_binding_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class ScientificClaimSelection:
    selected_claim_ids: tuple[str, ...]
    ordered_claim_ids: tuple[str, ...]
    connector_template_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_claim_ids": list(self.selected_claim_ids),
            "ordered_claim_ids": list(self.ordered_claim_ids),
            "connector_template_ids": list(self.connector_template_ids),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True)
class RenderedScientificClaim:
    """Exact sentence bytes and digest produced by a code-owned template."""

    sentence: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ScientificClaimAuthorityRegistry:
    """Immutable B2 fact and claim registries for one generation."""

    facts: tuple[EvidenceFact, ...]
    claims: tuple[ScientificClaimRecord, ...]

    def facts_bytes(self) -> bytes:
        return _canonical_bytes([fact.to_dict() for fact in self.facts])

    def claims_bytes(self) -> bytes:
        return _canonical_bytes([claim.to_dict() for claim in self.claims])

    @property
    def facts_sha256(self) -> str:
        return hashlib.sha256(self.facts_bytes()).hexdigest()

    @property
    def claims_sha256(self) -> str:
        return hashlib.sha256(self.claims_bytes()).hexdigest()


def bind_scientific_claim_source(
    binding: ScientificClaimGenerationBinding, path: str
) -> ScientificClaimSource:
    """Resolve source bytes only from the replayed evidence held by ``binding``."""

    rebuilt = _rebuild_generation_binding(binding)
    return _source_from_evidence(rebuilt.evidence, path)


def build_scientific_claim_generation_binding(
    evidence: CanonicalExperimentEvidence,
) -> ScientificClaimGenerationBinding:
    """Rebuild CFS and derive the complete generation binding without fallbacks."""

    if not isinstance(evidence, CanonicalExperimentEvidence):
        raise ScientificClaimAuthorityError(
            "generation binding requires replayed CanonicalExperimentEvidence"
        )
    try:
        cfs = build_canonical_fact_sheet(evidence)
    except (CFSIntegrityError, CanonicalExperimentEvidenceError) as exc:
        raise ScientificClaimAuthorityError(
            f"cannot rebuild scientific claim CFS: {exc}"
        ) from exc
    if cfs is None:
        raise ScientificClaimAuthorityError(
            "scientific claim binding requires exact domain-evaluator v2 CFS"
        )
    cfs = _require_complete_cfs(cfs)

    evidence_path = _safe_path(
        evidence.manifest_path, "canonical experiment evidence path"
    )
    contract_path = _safe_path(
        evidence.experiment_contract_path, "experiment contract path"
    )
    run_config_path = _safe_path(evidence.run_config_path, "run config path")
    if evidence_path != "canonical_experiment_evidence.json":
        raise ScientificClaimAuthorityError(
            "canonical experiment evidence path is not policy v1"
        )
    if contract_path != "stage-09/experiment_contract.yaml":
        raise ScientificClaimAuthorityError(
            "experiment contract path is not policy v1"
        )

    stored_evidence_sha256 = _sha256(
        evidence.manifest_sha256, "canonical experiment evidence sha256"
    )
    try:
        manifest_bytes = _canonical_bytes(evidence.manifest)
    except (UnicodeEncodeError, CanonicalExperimentEvidenceError) as exc:
        raise ScientificClaimAuthorityError(
            f"canonical experiment evidence manifest is not replayable: {exc}"
        ) from exc
    evidence_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if stored_evidence_sha256 != evidence_sha256:
        raise ScientificClaimAuthorityError(
            "canonical experiment evidence bytes/hash mismatch"
        )
    contract_sha256 = _sha256(
        evidence.experiment_contract_sha256, "experiment contract sha256"
    )
    run_config_sha256 = _sha256(evidence.run_config_sha256, "run config sha256")
    if hashlib.sha256(evidence.experiment_contract_bytes).hexdigest() != contract_sha256:
        raise ScientificClaimAuthorityError("experiment contract bytes/hash mismatch")
    if hashlib.sha256(evidence.run_config_bytes).hexdigest() != run_config_sha256:
        raise ScientificClaimAuthorityError("run config bytes/hash mismatch")
    _trusted_source_inventory(evidence)

    try:
        cfs_sha256 = canonical_fact_sheet_sha256(cfs)
    except CFSIntegrityError as exc:
        raise ScientificClaimAuthorityError(f"cannot hash rebuilt CFS: {exc}") from exc
    payload = {
        "binding_policy_version": GENERATION_BINDING_POLICY_VERSION,
        "canonical_experiment_evidence_path": evidence_path,
        "canonical_experiment_evidence_sha256": evidence_sha256,
        "experiment_contract_path": contract_path,
        "experiment_contract_sha256": contract_sha256,
        "run_config_path": run_config_path,
        "run_config_sha256": run_config_sha256,
        "cfs_schema_version": SCHEMA_VERSION,
        "cfs_sha256": cfs_sha256,
        "claim_policy_id": CLAIM_POLICY_ID,
    }
    generation_sha256 = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return ScientificClaimGenerationBinding(
        canonical_experiment_evidence_path=evidence_path,
        canonical_experiment_evidence_sha256=evidence_sha256,
        experiment_contract_path=contract_path,
        experiment_contract_sha256=contract_sha256,
        run_config_path=run_config_path,
        run_config_sha256=run_config_sha256,
        cfs_schema_version=SCHEMA_VERSION,
        cfs_sha256=cfs_sha256,
        generation_binding_sha256=generation_sha256,
        evidence=evidence,
    )


def evidence_fact_id(payload: Mapping[str, Any]) -> str:
    """Hash every EvidenceFact field except ``fact_id``."""

    return _self_excluding_identity(
        payload, fields=_EVIDENCE_FACT_FIELDS, id_field="fact_id", label="fact"
    )


def scientific_claim_id(payload: Mapping[str, Any]) -> str:
    """Hash every ScientificClaimRecord field except ``claim_id``."""

    return _self_excluding_identity(
        payload,
        fields=_SCIENTIFIC_CLAIM_FIELDS,
        id_field="claim_id",
        label="claim",
    )


def parse_evidence_fact(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
    source: ScientificClaimSource,
) -> EvidenceFact:
    """Parse and replay one canonical EvidenceFact under trusted bindings."""

    payload = _parse_canonical_object(
        content, fields=_EVIDENCE_FACT_FIELDS, label="EvidenceFact"
    )
    _require_binding(payload, binding)
    _require_source_binding(payload, source, binding)
    _true_int_one(payload["schema_version"], "schema_version")
    _true_int_one(payload["cfs_schema_version"], "cfs_schema_version")
    for field in (
        "fact_id",
        "source_sha256",
        "cfs_sha256",
        "generation_binding_sha256",
    ):
        _sha256(payload[field], field)
    for field in ("fact_kind", "subject_id", "predicate_id", "unit_id"):
        _registry_id(payload[field], field)

    object_kind = payload["object_kind"]
    if type(object_kind) is not str or object_kind not in _OBJECT_KINDS:
        raise ScientificClaimAuthorityError("object_kind is unsupported")
    if type(payload["object_value"]) is not str:
        raise ScientificClaimAuthorityError("object_value must be a string")
    _canonical_string(payload["object_value"], "object_value", allow_empty=True)
    if object_kind == "identifier":
        _registry_id(payload["object_value"], "object_value")
    elif object_kind == "boolean" and payload["object_value"] not in {"true", "false"}:
        raise ScientificClaimAuthorityError(
            "boolean object_value must be true or false"
        )
    elif object_kind == "decimal":
        try:
            normalized = canonical_decimal(payload["object_value"])
        except CanonicalExperimentEvidenceError as exc:
            raise ScientificClaimAuthorityError(
                f"decimal object_value is invalid: {exc}"
            ) from exc
        if normalized != payload["object_value"]:
            raise ScientificClaimAuthorityError(
                "decimal object_value is not canonical"
            )

    pointer = _json_pointer(payload["source_json_pointer"])
    source_value = _resolve_canonical_json_pointer(source.content, pointer)
    _require_fact_source_value(payload, source_value)
    if payload["fact_id"] != evidence_fact_id(payload):
        raise ScientificClaimAuthorityError("EvidenceFact identity mismatch")
    return EvidenceFact(**payload)


def parse_scientific_claim_record(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
    source: ScientificClaimSource,
) -> ScientificClaimRecord:
    """Parse B1 claim bytes without claiming B2 renderer semantic replay."""

    payload = _parse_canonical_object(
        content, fields=_SCIENTIFIC_CLAIM_FIELDS, label="ScientificClaimRecord"
    )
    _require_binding(payload, binding)
    _require_source_binding(payload, source, binding)
    _true_int_one(payload["schema_version"], "schema_version")
    _true_int_one(payload["cfs_schema_version"], "cfs_schema_version")
    for field in (
        "claim_id",
        "rendered_sentence_sha256",
        "source_sha256",
        "cfs_sha256",
        "generation_binding_sha256",
    ):
        _sha256(payload[field], field)
    _registry_id(payload["claim_kind"], "claim_kind")
    _registry_id(payload["renderer_template_id"], "renderer_template_id")
    if type(payload["section_id"]) is not str or payload["section_id"] not in _SECTION_IDS:
        raise ScientificClaimAuthorityError("section_id is unsupported")

    evidence_ids = _sha256_array(
        payload["evidence_fact_ids"], "evidence_fact_ids", nonempty=True
    )
    if tuple(sorted(evidence_ids)) != evidence_ids:
        raise ScientificClaimAuthorityError("evidence_fact_ids must be sorted")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ScientificClaimAuthorityError(
            "evidence_fact_ids must be duplicate free"
        )
    slot_ids = _sha256_array(
        payload["renderer_slot_fact_ids"],
        "renderer_slot_fact_ids",
        nonempty=True,
    )
    if len(set(slot_ids)) != len(slot_ids):
        raise ScientificClaimAuthorityError(
            "renderer_slot_fact_ids must be duplicate free"
        )
    if any(item not in evidence_ids for item in slot_ids):
        raise ScientificClaimAuthorityError(
            "renderer slot facts must belong to evidence_fact_ids"
        )

    sentence = _canonical_string(
        payload["rendered_sentence"], "rendered_sentence", allow_empty=False
    )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in sentence):
        raise ScientificClaimAuthorityError(
            "rendered_sentence contains a control character"
        )
    sentence_sha256 = hashlib.sha256(sentence.encode("utf-8")).hexdigest()
    if payload["rendered_sentence_sha256"] != sentence_sha256:
        raise ScientificClaimAuthorityError("rendered sentence/hash mismatch")
    if type(payload["mandatory"]) is not bool:
        raise ScientificClaimAuthorityError("mandatory must be a JSON boolean")
    if payload["claim_id"] != scientific_claim_id(payload):
        raise ScientificClaimAuthorityError("ScientificClaimRecord identity mismatch")

    normalized = dict(payload)
    normalized["evidence_fact_ids"] = evidence_ids
    normalized["renderer_slot_fact_ids"] = slot_ids
    return ScientificClaimRecord(**normalized)


def parse_scientific_claim_selection(content: bytes) -> ScientificClaimSelection:
    """Parse the exact B1 three-field structural selection response."""

    payload = _parse_canonical_object(
        content, fields=_SELECTION_FIELDS, label="scientific claim selection"
    )
    selected = _sha256_array(
        payload["selected_claim_ids"], "selected_claim_ids", nonempty=False
    )
    ordered = _sha256_array(
        payload["ordered_claim_ids"], "ordered_claim_ids", nonempty=False
    )
    if len(set(selected)) != len(selected):
        raise ScientificClaimAuthorityError(
            "selected_claim_ids must be duplicate free"
        )
    if len(set(ordered)) != len(ordered):
        raise ScientificClaimAuthorityError(
            "ordered_claim_ids must be duplicate free"
        )
    if len(ordered) != len(selected) or set(ordered) != set(selected):
        raise ScientificClaimAuthorityError(
            "ordered_claim_ids must be an exact selected_claim_ids permutation"
        )

    connectors = payload["connector_template_ids"]
    if not isinstance(connectors, list):
        raise ScientificClaimAuthorityError(
            "connector_template_ids must be an array"
        )
    if len(connectors) != max(len(ordered) - 1, 0):
        raise ScientificClaimAuthorityError("connector template count is invalid")
    if any(type(item) is not str or item != "NONE" for item in connectors):
        raise ScientificClaimAuthorityError(
            "connector policy v1 accepts only NONE IDs"
        )
    return ScientificClaimSelection(selected, ordered, tuple(connectors))


def build_scientific_claim_registry(
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimAuthorityRegistry:
    """Build the closed B2 fact and claim registries from captured authority."""

    _validate_code_owned_registries()
    binding = _rebuild_generation_binding(binding)
    try:
        cfs = build_canonical_fact_sheet(binding.evidence)
    except (CFSIntegrityError, CanonicalExperimentEvidenceError) as exc:
        raise ScientificClaimAuthorityError(
            f"cannot rebuild scientific claim CFS: {exc}"
        ) from exc
    cfs = _require_complete_cfs(cfs)
    primary_metric = cfs.get("primary_metric")

    source = bind_scientific_claim_source(
        binding, binding.canonical_experiment_evidence_path
    )
    facts: list[EvidenceFact] = []
    for spec in _EVIDENCE_FACT_REGISTRY:
        source_value = _resolve_canonical_json_pointer(
            source.content, spec.source_json_pointer
        )
        source_object_value = _fact_object_value(spec, source_value)
        cfs_object_value = _fact_object_value(
            spec, primary_metric[spec.cfs_key]
        )
        if source_object_value != cfs_object_value:
            raise ScientificClaimAuthorityError(
                f"CFS/source exact mismatch for {spec.fact_kind}"
            )
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "fact_id": "0" * 64,
            "fact_kind": spec.fact_kind,
            "subject_id": spec.subject_id,
            "predicate_id": spec.predicate_id,
            "object_kind": spec.object_kind,
            "object_value": source_object_value,
            "unit_id": spec.unit_id,
            "source_path": source.path,
            "source_sha256": source.sha256,
            "source_json_pointer": spec.source_json_pointer,
            "cfs_schema_version": binding.cfs_schema_version,
            "cfs_sha256": binding.cfs_sha256,
            "generation_binding_sha256": binding.generation_binding_sha256,
        }
        payload["fact_id"] = evidence_fact_id(payload)
        facts.append(
            parse_evidence_fact(
                _canonical_bytes(payload),
                binding=binding,
                source=source,
            )
        )

    fact_tuple = tuple(facts)
    claims: list[ScientificClaimRecord] = []
    for template in _RENDERER_TEMPLATE_REGISTRY:
        slot_facts = _slot_facts_for_template(template, fact_tuple)
        rendered = _render_template(template, slot_facts)
        evidence_fact_ids = tuple(sorted(fact.fact_id for fact in slot_facts))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "claim_id": "0" * 64,
            "claim_kind": template.claim_kind,
            "section_id": template.section_id,
            "evidence_fact_ids": list(evidence_fact_ids),
            "renderer_template_id": template.template_id,
            "renderer_slot_fact_ids": [
                fact.fact_id for fact in slot_facts
            ],
            "rendered_sentence": rendered.sentence,
            "rendered_sentence_sha256": rendered.sha256,
            "mandatory": template.mandatory,
            "source_path": source.path,
            "source_sha256": source.sha256,
            "cfs_schema_version": binding.cfs_schema_version,
            "cfs_sha256": binding.cfs_sha256,
            "generation_binding_sha256": binding.generation_binding_sha256,
        }
        payload["claim_id"] = scientific_claim_id(payload)
        claims.append(
            parse_scientific_claim_record(
                _canonical_bytes(payload),
                binding=binding,
                source=source,
            )
        )
    return ScientificClaimAuthorityRegistry(fact_tuple, tuple(claims))


def validate_scientific_claim_registry(
    registry: ScientificClaimAuthorityRegistry,
    *,
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimAuthorityRegistry:
    """Independently rebuild and semantically replay one in-memory registry."""

    if not isinstance(registry, ScientificClaimAuthorityRegistry):
        raise ScientificClaimAuthorityError(
            "scientific claim registry must be code-built"
        )
    expected = build_scientific_claim_registry(binding)
    source = bind_scientific_claim_source(
        binding, binding.canonical_experiment_evidence_path
    )
    if len(registry.facts) != len(_EVIDENCE_FACT_REGISTRY):
        raise ScientificClaimAuthorityError("fact registry cardinality mismatch")
    for index, (fact, spec) in enumerate(
        zip(registry.facts, _EVIDENCE_FACT_REGISTRY, strict=True)
    ):
        if not isinstance(fact, EvidenceFact):
            raise ScientificClaimAuthorityError(
                f"fact registry record {index} has the wrong type"
            )
        if not _fact_matches_spec(fact, spec):
            raise ScientificClaimAuthorityError(
                f"fact registry semantic mismatch at index {index}"
            )
        parse_evidence_fact(
            fact.canonical_bytes(),
            binding=binding,
            source=source,
        )
    if registry.facts != expected.facts:
        raise ScientificClaimAuthorityError(
            "fact registry differs from code-owned construction"
        )

    if len(registry.claims) != len(_RENDERER_TEMPLATE_REGISTRY):
        raise ScientificClaimAuthorityError("claim registry cardinality mismatch")
    fact_by_id = {fact.fact_id: fact for fact in registry.facts}
    for index, (claim, template) in enumerate(
        zip(registry.claims, _RENDERER_TEMPLATE_REGISTRY, strict=True)
    ):
        if not isinstance(claim, ScientificClaimRecord):
            raise ScientificClaimAuthorityError(
                f"claim registry record {index} has the wrong type"
            )
        if (
            claim.claim_kind != template.claim_kind
            or claim.section_id != template.section_id
            or claim.renderer_template_id != template.template_id
            or claim.mandatory is not template.mandatory
        ):
            raise ScientificClaimAuthorityError(
                f"claim registry semantic mismatch at index {index}"
            )
        try:
            slot_facts = tuple(
                fact_by_id[fact_id]
                for fact_id in claim.renderer_slot_fact_ids
            )
        except KeyError as exc:
            raise ScientificClaimAuthorityError(
                "claim registry references an unknown fact"
            ) from exc
        rendered = _render_template(template, slot_facts)
        if (
            rendered.content != claim.rendered_sentence.encode("utf-8")
            or rendered.sha256 != claim.rendered_sentence_sha256
        ):
            raise ScientificClaimAuthorityError(
                "claim rerender does not match stored sentence bytes"
            )
        parse_scientific_claim_record(
            claim.canonical_bytes(),
            binding=binding,
            source=source,
        )
    if registry.claims != expected.claims:
        raise ScientificClaimAuthorityError(
            "claim registry differs from code-owned construction"
        )
    if len({fact.fact_id for fact in registry.facts}) != len(registry.facts):
        raise ScientificClaimAuthorityError("fact registry contains duplicate IDs")
    if len({claim.claim_id for claim in registry.claims}) != len(registry.claims):
        raise ScientificClaimAuthorityError("claim registry contains duplicate IDs")
    return registry


def render_scientific_claim(
    template_id: str,
    renderer_slot_fact_ids: Sequence[str],
    *,
    registry: ScientificClaimAuthorityRegistry,
    binding: ScientificClaimGenerationBinding,
) -> RenderedScientificClaim:
    """Render one code-owned template from an independently replayed registry."""

    validate_scientific_claim_registry(registry, binding=binding)
    template = _template_for_id(template_id)
    if isinstance(renderer_slot_fact_ids, (str, bytes)) or not isinstance(
        renderer_slot_fact_ids, Sequence
    ):
        raise ScientificClaimAuthorityError(
            "renderer slot fact IDs must be an ordered sequence"
        )
    fact_by_id = {fact.fact_id: fact for fact in registry.facts}
    try:
        slot_facts = tuple(
            fact_by_id[_sha256(fact_id, "renderer slot fact ID")]
            for fact_id in renderer_slot_fact_ids
        )
    except KeyError as exc:
        raise ScientificClaimAuthorityError(
            "renderer references an unknown fact"
        ) from exc
    return _render_template(template, slot_facts)


def validate_scientific_claim_selection(
    content: bytes,
    *,
    target_section: str,
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimSelection:
    """Apply registry membership, section, and mandatory selection semantics."""

    if type(target_section) is not str or target_section not in _SECTION_IDS:
        raise ScientificClaimAuthorityError("target section is unsupported")
    selection = parse_scientific_claim_selection(content)
    registry = build_scientific_claim_registry(binding)
    claim_by_id = {claim.claim_id: claim for claim in registry.claims}
    unknown = [
        claim_id
        for claim_id in selection.selected_claim_ids
        if claim_id not in claim_by_id
    ]
    if unknown:
        raise ScientificClaimAuthorityError(
            "selection contains an unknown claim ID"
        )
    if any(
        claim_by_id[claim_id].section_id != target_section
        for claim_id in selection.selected_claim_ids
    ):
        raise ScientificClaimAuthorityError(
            "selection contains a claim from another section"
        )
    mandatory = {
        claim.claim_id
        for claim in registry.claims
        if claim.section_id == target_section and claim.mandatory
    }
    if not mandatory.issubset(selection.selected_claim_ids):
        raise ScientificClaimAuthorityError(
            "selection omits a mandatory claim"
        )
    return selection


def render_scientific_claim_selection(
    content: bytes,
    *,
    target_section: str,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Render one validated section selection without paths, files, or LLMs."""

    selection = validate_scientific_claim_selection(
        content,
        target_section=target_section,
        binding=binding,
    )
    registry = build_scientific_claim_registry(binding)
    claim_by_id = {claim.claim_id: claim for claim in registry.claims}
    sentences = tuple(
        claim_by_id[claim_id].rendered_sentence.encode("utf-8")
        for claim_id in selection.ordered_claim_ids
    )
    return _join_rendered_sentences(
        sentences, selection.connector_template_ids
    )


def render_connector(template_id: object) -> bytes:
    """Render connector policy v1; ``NONE`` itself emits no bytes."""

    if type(template_id) is not str or template_id != "NONE":
        raise ScientificClaimAuthorityError(
            "connector policy v1 accepts only NONE without parameters"
        )
    return b""


def _join_rendered_sentences(
    sentences: Sequence[bytes], connector_template_ids: Sequence[str]
) -> bytes:
    if isinstance(sentences, (str, bytes)) or not isinstance(sentences, Sequence):
        raise ScientificClaimAuthorityError("rendered sentences must be byte records")
    if isinstance(connector_template_ids, (str, bytes)) or not isinstance(
        connector_template_ids, Sequence
    ):
        raise ScientificClaimAuthorityError("connector IDs must be a sequence")
    if len(connector_template_ids) != max(len(sentences) - 1, 0):
        raise ScientificClaimAuthorityError("connector template count is invalid")
    validated: list[bytes] = []
    for sentence in sentences:
        if type(sentence) is not bytes:
            raise ScientificClaimAuthorityError(
                "rendered sentence content must be exact bytes"
            )
        try:
            text = sentence.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScientificClaimAuthorityError(
                "rendered sentence bytes are not UTF-8"
            ) from exc
        _validate_rendered_sentence(text)
        if text.encode("utf-8") != sentence:
            raise ScientificClaimAuthorityError(
                "rendered sentence byte replay mismatch"
            )
        validated.append(sentence)
    for connector in connector_template_ids:
        if render_connector(connector) != b"":
            raise ScientificClaimAuthorityError(
                "NONE connector emitted unexpected bytes"
            )
    return b" ".join(validated)


def _validate_code_owned_registries() -> None:
    fact_kinds = tuple(spec.fact_kind for spec in _EVIDENCE_FACT_REGISTRY)
    if len(fact_kinds) != len(set(fact_kinds)):
        raise ScientificClaimAuthorityError(
            "code-owned fact registry contains duplicates"
        )
    template_ids = tuple(
        template.template_id for template in _RENDERER_TEMPLATE_REGISTRY
    )
    if len(template_ids) != len(set(template_ids)):
        raise ScientificClaimAuthorityError(
            "code-owned template registry contains duplicates"
        )
    claim_keys = tuple(
        (template.claim_kind, template.section_id)
        for template in _RENDERER_TEMPLATE_REGISTRY
    )
    if len(claim_keys) != len(set(claim_keys)):
        raise ScientificClaimAuthorityError(
            "code-owned claim registry contains duplicates"
        )
    known_fact_kinds = set(fact_kinds)
    for template in _RENDERER_TEMPLATE_REGISTRY:
        if (
            len(template.literal_parts) != len(template.slot_fact_kinds) + 1
            or len(template.slot_fact_kinds)
            != len(set(template.slot_fact_kinds))
            or any(
                fact_kind not in known_fact_kinds
                for fact_kind in template.slot_fact_kinds
            )
        ):
            raise ScientificClaimAuthorityError(
                f"code-owned template registry is invalid: {template.template_id}"
            )


def _require_complete_cfs(cfs: object) -> Mapping[str, Any]:
    if not isinstance(cfs, Mapping) or set(cfs) != _COMPLETE_CFS_FIELDS:
        raise ScientificClaimAuthorityError(
            "rebuilt complete CFS schema mismatch"
        )
    _true_int_one(cfs.get("schema_version"), "CFS schema_version")
    primary_metric = cfs.get("primary_metric")
    if (
        not isinstance(primary_metric, Mapping)
        or set(primary_metric) != _PRIMARY_METRIC_FIELDS
    ):
        raise ScientificClaimAuthorityError(
            "rebuilt complete CFS primary_metric schema mismatch"
        )
    return cfs


def _fact_object_value(spec: _EvidenceFactSpec, value: object) -> str:
    if spec.object_kind == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ScientificClaimAuthorityError(
                f"{spec.fact_kind} must resolve to a finite decimal"
            )
        try:
            return canonical_decimal(value)
        except CanonicalExperimentEvidenceError as exc:
            raise ScientificClaimAuthorityError(
                f"{spec.fact_kind} decimal is invalid: {exc}"
            ) from exc
    if spec.object_kind == "string":
        if (
            type(value) is not str
            or len(value) > 128
            or _REGISTRY_ID_RE.fullmatch(value) is None
        ):
            raise ScientificClaimAuthorityError(
                f"{spec.fact_kind} is not an admitted data string"
            )
        return value
    raise ScientificClaimAuthorityError(
        f"unsupported code-owned fact object kind: {spec.object_kind}"
    )


def _fact_matches_spec(fact: EvidenceFact, spec: _EvidenceFactSpec) -> bool:
    return (
        fact.fact_kind == spec.fact_kind
        and fact.subject_id == spec.subject_id
        and fact.predicate_id == spec.predicate_id
        and fact.object_kind == spec.object_kind
        and fact.unit_id == spec.unit_id
        and fact.source_json_pointer == spec.source_json_pointer
    )


def _template_for_id(template_id: object) -> _RendererTemplateSpec:
    if type(template_id) is not str:
        raise ScientificClaimAuthorityError(
            "renderer template ID must be a string"
        )
    _validate_code_owned_registries()
    for template in _RENDERER_TEMPLATE_REGISTRY:
        if template.template_id == template_id:
            return template
    raise ScientificClaimAuthorityError("renderer template ID is unknown")


def _slot_facts_for_template(
    template: _RendererTemplateSpec, facts: Sequence[EvidenceFact]
) -> tuple[EvidenceFact, ...]:
    by_kind = {fact.fact_kind: fact for fact in facts}
    if len(by_kind) != len(facts):
        raise ScientificClaimAuthorityError(
            "fact registry contains duplicate semantic kinds"
        )
    try:
        return tuple(by_kind[kind] for kind in template.slot_fact_kinds)
    except KeyError as exc:
        raise ScientificClaimAuthorityError(
            "template requires an unavailable fact kind"
        ) from exc


def _render_template(
    template: _RendererTemplateSpec,
    slot_facts: Sequence[EvidenceFact],
) -> RenderedScientificClaim:
    if len(slot_facts) != len(template.slot_fact_kinds):
        raise ScientificClaimAuthorityError("renderer template arity mismatch")
    values: list[str] = []
    spec_by_kind = {
        spec.fact_kind: spec for spec in _EVIDENCE_FACT_REGISTRY
    }
    for index, (fact, expected_kind) in enumerate(
        zip(slot_facts, template.slot_fact_kinds, strict=True)
    ):
        spec = spec_by_kind[expected_kind]
        if not isinstance(fact, EvidenceFact) or not _fact_matches_spec(fact, spec):
            raise ScientificClaimAuthorityError(
                f"renderer slot semantic mismatch at index {index}"
            )
        value = _fact_object_value(spec, _fact_typed_value(fact))
        if fact.fact_kind == "primary_metric_key":
            labels = dict(_METRIC_DISPLAY_LABELS)
            if value not in labels:
                raise ScientificClaimAuthorityError(
                    "primary metric has no code-owned display label"
                )
            value = labels[value]
        values.append(value)

    pieces: list[str] = [template.literal_parts[0]]
    for value, literal in zip(
        values, template.literal_parts[1:], strict=True
    ):
        pieces.extend((value, literal))
    sentence = "".join(pieces)
    _validate_rendered_sentence(sentence)
    content = sentence.encode("utf-8")
    return RenderedScientificClaim(
        sentence=sentence,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fact_typed_value(fact: EvidenceFact) -> object:
    if fact.object_kind == "decimal":
        try:
            return Decimal(fact.object_value)
        except Exception as exc:
            raise ScientificClaimAuthorityError(
                "renderer decimal fact is invalid"
            ) from exc
    if fact.object_kind == "string":
        return fact.object_value
    raise ScientificClaimAuthorityError(
        "renderer fact object kind is unsupported"
    )


def _validate_rendered_sentence(sentence: object) -> str:
    sentence = _canonical_string(
        sentence, "rendered sentence", allow_empty=False
    )
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in sentence
    ):
        raise ScientificClaimAuthorityError(
            "rendered sentence contains a control character"
        )
    if sentence.lstrip().startswith("#") or _RENDERED_CITATION_RE.search(sentence):
        raise ScientificClaimAuthorityError(
            "rendered sentence contains forbidden markup or citation syntax"
        )
    if sentence[-1] not in ".!?":
        raise ScientificClaimAuthorityError(
            "rendered sentence lacks terminal punctuation"
        )
    for index, character in enumerate(sentence[:-1]):
        if character not in ".!?":
            continue
        if (
            character == "."
            and index > 0
            and sentence[index - 1].isascii()
            and sentence[index - 1].isdigit()
            and sentence[index + 1].isascii()
            and sentence[index + 1].isdigit()
        ):
            continue
        raise ScientificClaimAuthorityError(
            "renderer must emit exactly one terminally punctuated sentence"
        )
    if len(_RENDERED_TERMINATOR_RE.findall(sentence)) != 1:
        raise ScientificClaimAuthorityError(
            "renderer must emit exactly one terminally punctuated sentence"
        )
    return sentence


def _parse_canonical_object(
    content: bytes, *, fields: frozenset[str], label: str
) -> dict[str, Any]:
    if type(content) is not bytes:
        raise ScientificClaimAuthorityError(f"{label} content must be exact bytes")
    try:
        text = content.decode("utf-8")
        value = _parse_json_value(text, label)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        raise ScientificClaimAuthorityError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ScientificClaimAuthorityError(f"{label} root must be an object")
    if set(value) != fields:
        raise ScientificClaimAuthorityError(
            f"{label} fields mismatch: "
            f"missing={sorted(fields - set(value))}, "
            f"extra={sorted(set(value) - fields)}"
        )
    try:
        canonical = _canonical_bytes(value)
    except (UnicodeEncodeError, CanonicalExperimentEvidenceError) as exc:
        raise ScientificClaimAuthorityError(
            f"{label} is not canonical authority JSON: {exc}"
        ) from exc
    if canonical != content:
        raise ScientificClaimAuthorityError(f"{label} bytes are not canonical")
    return value


def _canonical_bytes(value: object) -> bytes:
    return canonical_authority_json_text(value).encode("utf-8")


def _self_excluding_identity(
    payload: Mapping[str, Any],
    *,
    fields: frozenset[str],
    id_field: str,
    label: str,
) -> str:
    if not isinstance(payload, Mapping):
        raise ScientificClaimAuthorityError(f"{label} identity payload must be an object")
    keys = set(payload)
    allowed = (fields, fields - {id_field})
    if keys not in allowed:
        raise ScientificClaimAuthorityError(
            f"{label} identity fields mismatch"
        )
    identity = {key: value for key, value in payload.items() if key != id_field}
    try:
        content = _canonical_bytes(identity)
    except CanonicalExperimentEvidenceError as exc:
        raise ScientificClaimAuthorityError(
            f"{label} identity is not canonical: {exc}"
        ) from exc
    return hashlib.sha256(content).hexdigest()


def _true_int_one(value: object, field: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ScientificClaimAuthorityError(f"{field} must be the true integer 1")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ScientificClaimAuthorityError(f"{field} must be lowercase SHA-256")
    return value


def _registry_id(value: object, field: str) -> str:
    value = _canonical_string(value, field, allow_empty=False)
    if _REGISTRY_ID_RE.fullmatch(value) is None:
        raise ScientificClaimAuthorityError(f"{field} is not a canonical registry ID")
    return value


def _canonical_string(value: object, field: str, *, allow_empty: bool) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ScientificClaimAuthorityError(f"{field} must be a string")
    return value


def _safe_path(value: object, field: str) -> str:
    if type(value) is not str:
        raise ScientificClaimAuthorityError(f"{field} must be a string")
    if value != value.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ScientificClaimAuthorityError(f"{field} is unsafe")
    try:
        return validate_release_authority_path(value)
    except IndependentReleaseReconstructionError as exc:
        raise ScientificClaimAuthorityError(f"{field} is unsafe: {exc}") from exc


def _sha256_array(value: object, field: str, *, nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a nonempty" if nonempty else "an"
        raise ScientificClaimAuthorityError(f"{field} must be {qualifier} array")
    return tuple(_sha256(item, f"{field} item") for item in value)


def _require_binding(
    payload: Mapping[str, Any], binding: ScientificClaimGenerationBinding
) -> None:
    binding = _rebuild_generation_binding(binding)
    if (
        payload.get("cfs_schema_version") != binding.cfs_schema_version
        or payload.get("cfs_sha256") != binding.cfs_sha256
        or payload.get("generation_binding_sha256")
        != binding.generation_binding_sha256
    ):
        raise ScientificClaimAuthorityError(
            "record CFS/generation binding mismatch"
        )


def _require_source_binding(
    payload: Mapping[str, Any],
    source: ScientificClaimSource,
    binding: ScientificClaimGenerationBinding,
) -> None:
    if not isinstance(source, ScientificClaimSource):
        raise ScientificClaimAuthorityError(
            "record validation requires captured source bytes"
        )
    _safe_path(payload.get("source_path"), "source_path")
    _sha256(payload.get("source_sha256"), "source_sha256")
    trusted_source = _source_from_evidence(
        _rebuild_generation_binding(binding).evidence,
        payload["source_path"],
    )
    if (
        source != trusted_source
        or payload.get("source_path") != trusted_source.path
        or payload.get("source_sha256") != trusted_source.sha256
        or hashlib.sha256(trusted_source.content).hexdigest()
        != trusted_source.sha256
    ):
        raise ScientificClaimAuthorityError("record source binding mismatch")


def _rebuild_generation_binding(
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimGenerationBinding:
    if not isinstance(binding, ScientificClaimGenerationBinding):
        raise ScientificClaimAuthorityError(
            "record validation requires a rebuilt generation binding"
        )
    rebuilt = build_scientific_claim_generation_binding(binding.evidence)
    if rebuilt != binding:
        raise ScientificClaimAuthorityError(
            "caller generation binding differs from independent rebuild"
        )
    return rebuilt


def _source_from_evidence(
    evidence: CanonicalExperimentEvidence, path: str
) -> ScientificClaimSource:
    canonical_path = _safe_path(path, "source path")
    source = _trusted_source_inventory(evidence).get(canonical_path)
    if source is None:
        raise ScientificClaimAuthorityError(
            "source path is not present in the trusted evidence inventory"
        )
    return source


def _trusted_source_inventory(
    evidence: CanonicalExperimentEvidence,
) -> dict[str, ScientificClaimSource]:
    """Rebuild the domain-v2 source namespace from code-owned bindings."""

    inventory: dict[str, ScientificClaimSource] = {}

    def add(path: object, digest: object, content: object, label: str) -> None:
        canonical_path = _safe_path(path, f"{label} path")
        stored_sha256 = _sha256(digest, f"{label} sha256")
        if type(content) is not bytes:
            raise ScientificClaimAuthorityError(f"{label} content must be exact bytes")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != stored_sha256:
            raise ScientificClaimAuthorityError(f"{label} bytes/hash mismatch")
        if canonical_path in inventory:
            raise ScientificClaimAuthorityError(
                f"duplicate trusted source path: {canonical_path}"
            )
        inventory[canonical_path] = ScientificClaimSource(
            canonical_path, actual_sha256, content
        )

    try:
        manifest_bytes = _canonical_bytes(evidence.manifest)
        candidate_bytes = _canonical_bytes(evidence.candidate)
        selected_result_bytes = _canonical_bytes(evidence.selected_result)
        manifest = parse_domain_evaluator_canonical_manifest(
            manifest_bytes.decode("utf-8")
        )
        candidate = parse_domain_evaluator_candidate(
            candidate_bytes.decode("utf-8")
        )
        selected_result = parse_domain_evaluator_refinement_result_set(
            selected_result_bytes.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        raise ScientificClaimAuthorityError(
            f"domain-v2 source authority replay failed: {exc}"
        ) from exc

    add(
        evidence.manifest_path,
        evidence.manifest_sha256,
        manifest_bytes,
        "canonical evidence manifest",
    )
    candidate_ref = _required_mapping(
        _required_mapping(
            manifest.get("selected_candidate"), "root selected_candidate"
        ).get("manifest"),
        "root selected_candidate.manifest",
    )
    if (
        candidate_ref["path"] != evidence.candidate_manifest_path
        or candidate_ref["sha256"] != evidence.candidate_manifest_sha256
        or candidate_ref["size"] != len(candidate_bytes)
    ):
        raise ScientificClaimAuthorityError("candidate manifest root binding mismatch")
    add(
        evidence.candidate_manifest_path,
        evidence.candidate_manifest_sha256,
        candidate_bytes,
        "candidate manifest",
    )
    if candidate_ref["sha256"] != hashlib.sha256(candidate_bytes).hexdigest():
        raise ScientificClaimAuthorityError("candidate manifest hash binding mismatch")

    bindings = _required_mapping(manifest.get("bindings"), "root bindings")
    selected_ref = _required_mapping(
        bindings.get("stage13_refinement"), "root Stage 13 refinement binding"
    )
    if (
        selected_ref["path"] != evidence.selected_result_manifest_path
        or selected_ref["sha256"] != evidence.selected_result_manifest_sha256
        or selected_ref["size"] != len(selected_result_bytes)
    ):
        raise ScientificClaimAuthorityError("selected result root binding mismatch")
    add(
        evidence.selected_result_manifest_path,
        evidence.selected_result_manifest_sha256,
        selected_result_bytes,
        "selected result manifest",
    )
    if selected_ref["sha256"] != hashlib.sha256(selected_result_bytes).hexdigest():
        raise ScientificClaimAuthorityError("selected result hash binding mismatch")

    if (
        candidate.get("bindings") != bindings
        or candidate.get("observation_authority")
        != manifest.get("observation_authority")
        or candidate.get("primary_metric") != manifest.get("primary_metric")
        or candidate.get("selected_result") != manifest.get("selected_result")
    ):
        raise ScientificClaimAuthorityError("candidate/root authority binding mismatch")
    selected_candidate = _required_mapping(
        manifest.get("selected_candidate"), "root selected_candidate"
    )
    if candidate.get("candidate_id") != selected_candidate.get("candidate_id"):
        raise ScientificClaimAuthorityError("candidate identity/root binding mismatch")

    contract_ref = _required_mapping(
        bindings.get("experiment_contract"), "contract binding"
    )
    config_ref = _required_mapping(bindings.get("run_config"), "run config binding")
    if (
        contract_ref["path"] != evidence.experiment_contract_path
        or contract_ref["sha256"] != evidence.experiment_contract_sha256
        or contract_ref["size"] != len(evidence.experiment_contract_bytes)
        or config_ref["path"] != evidence.run_config_path
        or config_ref["sha256"] != evidence.run_config_sha256
        or config_ref["size"] != len(evidence.run_config_bytes)
    ):
        raise ScientificClaimAuthorityError("contract/config binding mismatch")
    add(
        evidence.experiment_contract_path,
        evidence.experiment_contract_sha256,
        evidence.experiment_contract_bytes,
        "experiment contract",
    )
    add(
        evidence.run_config_path,
        evidence.run_config_sha256,
        evidence.run_config_bytes,
        "run config",
    )

    artifact_refs = candidate["artifacts"]
    if len(evidence.artifacts) != len(artifact_refs):
        raise ScientificClaimAuthorityError("candidate artifact closure mismatch")

    candidate_parent = PurePosixPath(evidence.candidate_manifest_path).parent
    seen_logical_paths: set[str] = set()
    role_sources: dict[str, list[ScientificClaimSource]] = {}
    for index, (ref, snapshot) in enumerate(
        zip(artifact_refs, evidence.artifacts, strict=True)
    ):
        role = _canonical_string(ref["role"], "candidate artifact role", allow_empty=False)
        logical_path = _safe_path(ref["path"], "candidate artifact logical path")
        if logical_path in seen_logical_paths:
            raise ScientificClaimAuthorityError(
                f"duplicate candidate artifact logical path: {logical_path}"
            )
        seen_logical_paths.add(logical_path)
        if not isinstance(snapshot, CanonicalEvidenceArtifact) or (
            snapshot.role != role
            or snapshot.path != logical_path
            or snapshot.sha256 != ref["sha256"]
            or len(snapshot.content) != ref["size"]
        ):
            raise ScientificClaimAuthorityError(
                f"candidate artifact closure mismatch at index {index}"
            )
        run_relative_path = _safe_path(
            (candidate_parent / PurePosixPath(logical_path)).as_posix(),
            "candidate artifact run-relative path",
        )
        add(
            run_relative_path,
            ref["sha256"],
            snapshot.content,
            f"candidate artifact {logical_path}",
        )
        role_sources.setdefault(role, []).append(inventory[run_relative_path])

    for root_field, role in (
        ("selected_summary", "summary"),
        ("selected_analysis", "analysis"),
    ):
        root_ref = _required_mapping(
            _required_mapping(manifest.get(root_field), root_field).get("source"),
            f"{root_field}.source",
        )
        sources = role_sources.get(role, [])
        if (
            len(sources) != 1
            or root_ref.get("path") != sources[0].path
            or root_ref.get("sha256") != sources[0].sha256
            or root_ref.get("size") != len(sources[0].content)
        ):
            raise ScientificClaimAuthorityError(
                f"{root_field} candidate artifact binding mismatch"
            )
    if evidence.summary_bytes != role_sources["summary"][0].content:
        raise ScientificClaimAuthorityError("candidate summary snapshot mismatch")
    if evidence.analysis_bytes != role_sources["analysis"][0].content:
        raise ScientificClaimAuthorityError("candidate analysis snapshot mismatch")

    execution_ref = _required_mapping(
        manifest["observation_authority"].get("observations"),
        "root observation authority",
    )
    selected_binding_fields = {
        "baseline_manifest": "stage12_result_set",
        "experiment_contract": "experiment_contract",
        "sealed_candidate_manifest": "sealed_candidate_manifest",
        "capture_manifest": "capture_manifest",
        "execution_policy": "execution_policy",
        "run_config": "run_config",
    }
    if any(
        selected_result.get(selected_field) != bindings[binding_field]
        for selected_field, binding_field in selected_binding_fields.items()
    ) or selected_result.get("observations") != execution_ref:
        raise ScientificClaimAuthorityError("selected result authority binding mismatch")
    for field in (
        "config_semantic_policy_version",
        "config_semantic_sha256",
        "claim_scope",
        "dataset_origin",
        "dataset_name",
        "evaluator_schema",
        "metric_authority",
    ):
        if selected_result.get(field) != bindings.get(field):
            raise ScientificClaimAuthorityError(
                "selected result authority binding mismatch"
            )
    if selected_result.get("primary_metric") != manifest["primary_metric"]:
        raise ScientificClaimAuthorityError("selected result authority binding mismatch")
    execution = evidence.selected_execution_artifact
    if (
        not isinstance(execution, CanonicalEvidenceArtifact)
        or execution.role != "observations"
        or execution.path != execution_ref["path"]
        or execution.sha256 != execution_ref["sha256"]
        or len(execution.content) != execution_ref["size"]
    ):
        raise ScientificClaimAuthorityError("selected execution binding mismatch")
    add(
        execution.path,
        execution.sha256,
        execution.content,
        "selected execution",
    )

    try:
        contract = parse_contract_bytes(evidence.experiment_contract_bytes)
        validate_contract_structure_dict(contract)
    except (ContractValidationError, UnicodeDecodeError, ValueError) as exc:
        raise ScientificClaimAuthorityError(
            f"experiment contract replay failed: {exc}"
        ) from exc
    evaluator_authority = _required_mapping(
        contract.get("evaluator_authority"), "contract evaluator_authority"
    )
    for field in ("claim_scope", "dataset_origin", "dataset_name", "metric_authority"):
        if contract.get(field) != bindings.get(field):
            raise ScientificClaimAuthorityError(
                "experiment contract authority binding mismatch"
            )
    if evaluator_authority.get("evaluator_schema") != bindings.get("evaluator_schema"):
        raise ScientificClaimAuthorityError(
            "experiment contract authority binding mismatch"
        )
    policy_ref = _required_mapping(bindings.get("execution_policy"), "policy binding")
    policy = evidence.execution_policy_artifact
    if (
        not isinstance(policy, CanonicalEvidenceArtifact)
        or policy.role != "execution_policy"
        or policy.path != policy_ref["path"]
        or policy.sha256 != policy_ref["sha256"]
        or len(policy.content) != policy_ref["size"]
        or policy.path != evaluator_authority.get("execution_policy_snapshot_path")
        or policy.sha256 != evaluator_authority.get(
            "execution_policy_snapshot_sha256"
        )
    ):
        raise ScientificClaimAuthorityError("execution policy binding mismatch")
    add(policy.path, policy.sha256, policy.content, "execution policy")
    return inventory


def _required_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScientificClaimAuthorityError(f"{field} must be an object")
    return value


def _json_pointer(value: object) -> str:
    pointer = _canonical_string(value, "source_json_pointer", allow_empty=True)
    if pointer and not pointer.startswith("/"):
        raise ScientificClaimAuthorityError(
            "source JSON pointer must be empty or start with /"
        )
    for token in pointer.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    raise ScientificClaimAuthorityError(
                        "source JSON pointer has an invalid escape"
                    )
                index += 2
            else:
                index += 1
    return pointer


def _resolve_canonical_json_pointer(content: bytes, pointer: str) -> object:
    try:
        text = content.decode("utf-8")
        value = _parse_json_value(text, "EvidenceFact source")
        if _canonical_bytes(value) != content:
            raise ScientificClaimAuthorityError(
                "EvidenceFact source JSON bytes are not canonical"
            )
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        raise ScientificClaimAuthorityError(
            f"EvidenceFact source is not strict JSON: {exc}"
        ) from exc
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ScientificClaimAuthorityError(
                    "source JSON pointer does not resolve"
                )
            current = current[token]
        elif isinstance(current, list):
            if (
                token == "-"
                or not token.isdigit()
                or (len(token) > 1 and token.startswith("0"))
            ):
                raise ScientificClaimAuthorityError(
                    "source JSON pointer array index is noncanonical"
                )
            index = int(token)
            if index >= len(current):
                raise ScientificClaimAuthorityError(
                    "source JSON pointer does not resolve"
                )
            current = current[index]
        else:
            raise ScientificClaimAuthorityError(
                "source JSON pointer does not resolve"
            )
    return current


def _require_fact_source_value(payload: Mapping[str, Any], source_value: object) -> None:
    kind = payload["object_kind"]
    stored = payload["object_value"]
    if kind == "decimal":
        if isinstance(source_value, bool) or not isinstance(
            source_value, (int, Decimal)
        ):
            raise ScientificClaimAuthorityError(
                "decimal fact source value has the wrong type"
            )
        try:
            replayed = canonical_decimal(source_value)
        except CanonicalExperimentEvidenceError as exc:
            raise ScientificClaimAuthorityError(
                f"decimal fact source value is invalid: {exc}"
            ) from exc
        if stored != replayed:
            raise ScientificClaimAuthorityError(
                "decimal fact source pointer/value mismatch"
            )
    elif kind == "boolean":
        if type(source_value) is not bool:
            raise ScientificClaimAuthorityError(
                "boolean fact source value has the wrong type"
            )
        if stored != ("true" if source_value else "false"):
            raise ScientificClaimAuthorityError(
                "boolean fact source pointer/value mismatch"
            )
    elif kind in {"string", "identifier"}:
        if type(source_value) is not str or stored != source_value:
            raise ScientificClaimAuthorityError(
                f"{kind} fact source pointer/value mismatch"
            )
