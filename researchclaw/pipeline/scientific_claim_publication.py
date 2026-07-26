"""B3-A in-memory contracts for structured Stage 17 publication.

No function in this module reads or writes a Stage 17 namespace, calls a
provider, or changes ordinary Stage 17 dispatch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    build_citation_closure_from_texts,
    parse_citation_closure_report,
)
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.literature.experiment_fact_closure import (
    ExperimentFactClosureError,
    _contract_from_evidence,
    build_experiment_fact_closure_from_text,
    canonical_experiment_fact_json_text,
    parse_experiment_fact_closure_report,
    replay_experiment_fact_closure,
)
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    _parse_json_value,
    canonical_authority_json_text,
)
from researchclaw.pipeline.independent_release_reconstruction import (
    IndependentReleaseReconstructionError,
    validate_release_authority_path,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    parse_manuscript,
)
from researchclaw.pipeline.scientific_claim_authority import (
    CLAIM_POLICY_ID,
    EvidenceFact,
    ScientificClaimAuthorityError,
    ScientificClaimAuthorityRegistry,
    ScientificClaimGenerationBinding,
    ScientificClaimRecord,
    bind_scientific_claim_source,
    build_scientific_claim_registry,
    parse_evidence_fact,
    parse_scientific_claim_record,
    render_scientific_claim_selection,
    validate_scientific_claim_registry,
    validate_scientific_claim_selection,
)


PUBLICATION_SCHEMA_VERSION = 1
PUBLICATION_STAGE_ID = "stage17"
SECTION_ORDER = (
    "abstract",
    "results",
    "discussion",
    "limitations",
    "conclusion",
)
FACT_RECORD_COUNT = 5
CLAIM_RECORD_COUNT = 6

_SELECTION_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "claim_policy_id",
        "generation_binding_sha256",
        "claim_registry_sha256",
        "sections",
    }
)
_SELECTION_SECTION_FIELDS = frozenset(
    {
        "section_id",
        "selected_claim_ids",
        "ordered_claim_ids",
        "connector_template_ids",
    }
)
_PROVIDER_SELECTION_FIELDS = frozenset(
    {"selected_claim_ids", "ordered_claim_ids", "connector_template_ids"}
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "publication_stage_id",
        "claim_policy_id",
        "structured_capability_schema_version",
        "structured_capability_snapshot",
        "generation_binding_sha256",
        "canonical_experiment_evidence",
        "experiment_contract",
        "run_config",
        "cfs",
        "facts",
        "claim_registry",
        "claim_selection",
        "paper_draft",
        "paper_structure_report",
        "experiment_fact_closure_report",
        "citation_closure_report",
        "source_authority_manifest",
    }
)
_FILE_REF_FIELDS = frozenset({"path", "sha256"})
_SHA256_LENGTH = 64

_FACTS_PATH = "stage-17/scientific_evidence_facts.json"
_REGISTRY_PATH = "stage-17/scientific_claim_registry.json"
_SELECTION_PATH = "stage-17/scientific_claim_selection.json"
_PAPER_PATH = "stage-17/paper_draft.md"
_STRUCTURE_REPORT_PATH = "stage-17/paper_structure_report.json"
_FACT_CLOSURE_PATH = "stage-17/experiment_fact_closure_report.json"
_CITATION_CLOSURE_PATH = "stage-17/citation_closure_report.json"
_SECTION_HEADINGS = {
    "abstract": "Abstract",
    "results": "Results",
    "discussion": "Discussion",
    "limitations": "Limitations",
    "conclusion": "Conclusion",
}


class ScientificClaimPublicationError(ValueError):
    """A B3-A publication contract or semantic replay failed."""


@dataclass(frozen=True)
class ScientificClaimSectionSelection:
    section_id: str
    selected_claim_ids: tuple[str, ...]
    ordered_claim_ids: tuple[str, ...]
    connector_template_ids: tuple[str, ...]

    def provider_dict(self) -> dict[str, Any]:
        return {
            "selected_claim_ids": list(self.selected_claim_ids),
            "ordered_claim_ids": list(self.ordered_claim_ids),
            "connector_template_ids": list(self.connector_template_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"section_id": self.section_id, **self.provider_dict()}


@dataclass(frozen=True)
class ScientificClaimSelectionArtifact:
    schema_version: int
    claim_policy_id: str
    generation_binding_sha256: str
    claim_registry_sha256: str
    sections: tuple[ScientificClaimSectionSelection, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_policy_id": self.claim_policy_id,
            "generation_binding_sha256": self.generation_binding_sha256,
            "claim_registry_sha256": self.claim_registry_sha256,
            "sections": [section.to_dict() for section in self.sections],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


def parse_canonical_publication_json(content: bytes, *, label: str) -> object:
    """Parse strict canonical authority JSON with duplicate-key rejection."""

    if type(content) is not bytes:
        raise ScientificClaimPublicationError(f"{label} content must be exact bytes")
    try:
        value = _parse_json_value(content.decode("utf-8"), label)
        canonical = _canonical_bytes(value)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        CanonicalExperimentEvidenceError,
    ) as exc:
        raise ScientificClaimPublicationError(f"invalid {label} JSON: {exc}") from exc
    if canonical != content:
        raise ScientificClaimPublicationError(f"{label} bytes are not canonical")
    return value


def build_scientific_evidence_facts(
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Build the v1 direct-array facts artifact from code-owned registries."""

    _guard("build_scientific_evidence_facts")
    registry = build_scientific_claim_registry(binding)
    if len(registry.facts) != FACT_RECORD_COUNT:
        raise ScientificClaimPublicationError("facts code-owned count mismatch")
    return registry.facts_bytes()


def parse_scientific_evidence_facts(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> tuple[EvidenceFact, ...]:
    """Parse and independently replay the ordered five-record facts array."""

    _guard("parse_scientific_evidence_facts")
    value = parse_canonical_publication_json(content, label="scientific evidence facts")
    if not isinstance(value, list):
        raise ScientificClaimPublicationError("facts root must be a direct array")
    if len(value) != FACT_RECORD_COUNT:
        raise ScientificClaimPublicationError("facts record count must equal 5")
    expected = build_scientific_claim_registry(binding)
    source = bind_scientific_claim_source(
        binding, binding.canonical_experiment_evidence_path
    )
    records: list[EvidenceFact] = []
    try:
        for item in value:
            records.append(
                parse_evidence_fact(
                    _canonical_bytes(item),
                    binding=binding,
                    source=source,
                )
            )
    except ScientificClaimAuthorityError as exc:
        raise ScientificClaimPublicationError(f"facts replay failed: {exc}") from exc
    if content != expected.facts_bytes() or tuple(records) != expected.facts:
        raise ScientificClaimPublicationError(
            "facts differ from code-owned ordered construction"
        )
    return tuple(records)


def build_scientific_claim_registry_artifact(
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Build the v1 direct-array claim registry artifact."""

    _guard("build_scientific_claim_registry_artifact")
    registry = build_scientific_claim_registry(binding)
    if len(registry.claims) != CLAIM_RECORD_COUNT:
        raise ScientificClaimPublicationError("claim code-owned count mismatch")
    return registry.claims_bytes()


def parse_scientific_claim_registry(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> tuple[ScientificClaimRecord, ...]:
    """Parse the ordered six-record registry and independently rerender it."""

    _guard("parse_scientific_claim_registry")
    value = parse_canonical_publication_json(content, label="scientific claim registry")
    if not isinstance(value, list):
        raise ScientificClaimPublicationError("claim registry root must be a direct array")
    if len(value) != CLAIM_RECORD_COUNT:
        raise ScientificClaimPublicationError("claim registry count must equal 6")
    expected = build_scientific_claim_registry(binding)
    source = bind_scientific_claim_source(
        binding, binding.canonical_experiment_evidence_path
    )
    records: list[ScientificClaimRecord] = []
    try:
        for item in value:
            records.append(
                parse_scientific_claim_record(
                    _canonical_bytes(item),
                    binding=binding,
                    source=source,
                )
            )
        parsed_registry = ScientificClaimAuthorityRegistry(
            expected.facts, tuple(records)
        )
        validate_scientific_claim_registry(parsed_registry, binding=binding)
    except ScientificClaimAuthorityError as exc:
        raise ScientificClaimPublicationError(
            f"claim registry replay failed: {exc}"
        ) from exc
    if content != expected.claims_bytes() or tuple(records) != expected.claims:
        raise ScientificClaimPublicationError(
            "claim registry differs from code-owned ordered construction"
        )
    return tuple(records)


def build_scientific_claim_selection(
    provider_responses: Sequence[bytes],
    *,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Inject section/root authority around five provider three-field responses."""

    _guard("build_scientific_claim_selection")
    if isinstance(provider_responses, (str, bytes)) or not isinstance(
        provider_responses, Sequence
    ):
        raise ScientificClaimPublicationError(
            "provider responses must be an ordered sequence"
        )
    if len(provider_responses) != len(SECTION_ORDER):
        raise ScientificClaimPublicationError("provider response count must equal 5")
    registry = build_scientific_claim_registry(binding)
    sections: list[ScientificClaimSectionSelection] = []
    for section_id, response in zip(SECTION_ORDER, provider_responses, strict=True):
        parsed = _parse_exact_object(
            response,
            fields=_PROVIDER_SELECTION_FIELDS,
            label=f"{section_id} provider selection",
        )
        try:
            selection = validate_scientific_claim_selection(
                _canonical_bytes(parsed),
                target_section=section_id,
                binding=binding,
            )
        except ScientificClaimAuthorityError as exc:
            raise ScientificClaimPublicationError(
                f"{section_id} provider selection failed: {exc}"
            ) from exc
        sections.append(
            ScientificClaimSectionSelection(
                section_id,
                selection.selected_claim_ids,
                selection.ordered_claim_ids,
                selection.connector_template_ids,
            )
        )
    artifact = ScientificClaimSelectionArtifact(
        PUBLICATION_SCHEMA_VERSION,
        CLAIM_POLICY_ID,
        binding.generation_binding_sha256,
        registry.claims_sha256,
        tuple(sections),
    )
    return artifact.canonical_bytes()


def parse_scientific_claim_selection_wrapper(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimSelectionArtifact:
    """Parse and semantically replay the fixed-order five-section wrapper."""

    _guard("parse_scientific_claim_selection_wrapper")
    payload = _parse_exact_object(
        content,
        fields=_SELECTION_ROOT_FIELDS,
        label="scientific claim selection wrapper",
    )
    _true_int_one(payload["schema_version"], "selection schema_version")
    if payload["claim_policy_id"] != CLAIM_POLICY_ID:
        raise ScientificClaimPublicationError("selection claim policy mismatch")
    registry = build_scientific_claim_registry(binding)
    if payload["generation_binding_sha256"] != binding.generation_binding_sha256:
        raise ScientificClaimPublicationError("selection generation binding mismatch")
    if payload["claim_registry_sha256"] != registry.claims_sha256:
        raise ScientificClaimPublicationError("selection registry binding mismatch")
    values = payload["sections"]
    if not isinstance(values, list) or len(values) != len(SECTION_ORDER):
        raise ScientificClaimPublicationError("selection sections must have length 5")

    sections: list[ScientificClaimSectionSelection] = []
    for index, section_id in enumerate(SECTION_ORDER):
        item = values[index]
        if not isinstance(item, dict) or set(item) != _SELECTION_SECTION_FIELDS:
            raise ScientificClaimPublicationError(
                f"selection section {index} fields mismatch"
            )
        if item["section_id"] != section_id:
            raise ScientificClaimPublicationError(
                f"selection section order mismatch at {section_id}"
            )
        provider_payload = {
            field: item[field] for field in _PROVIDER_SELECTION_FIELDS
        }
        try:
            selection = validate_scientific_claim_selection(
                _canonical_bytes(provider_payload),
                target_section=section_id,
                binding=binding,
            )
        except ScientificClaimAuthorityError as exc:
            raise ScientificClaimPublicationError(
                f"{section_id} selection replay failed: {exc}"
            ) from exc
        sections.append(
            ScientificClaimSectionSelection(
                section_id,
                selection.selected_claim_ids,
                selection.ordered_claim_ids,
                selection.connector_template_ids,
            )
        )
    artifact = ScientificClaimSelectionArtifact(
        PUBLICATION_SCHEMA_VERSION,
        CLAIM_POLICY_ID,
        binding.generation_binding_sha256,
        registry.claims_sha256,
        tuple(sections),
    )
    if artifact.canonical_bytes() != content:
        raise ScientificClaimPublicationError("selection wrapper replay mismatch")
    return artifact


def replay_scientific_claim_selection_wrapper(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> tuple[tuple[str, bytes], ...]:
    """Rerender all five sections from the code-owned registry."""

    _guard("replay_scientific_claim_selection_wrapper")
    artifact = parse_scientific_claim_selection_wrapper(content, binding=binding)
    rendered: list[tuple[str, bytes]] = []
    for section in artifact.sections:
        try:
            section_bytes = render_scientific_claim_selection(
                _canonical_bytes(section.provider_dict()),
                target_section=section.section_id,
                binding=binding,
            )
        except ScientificClaimAuthorityError as exc:
            raise ScientificClaimPublicationError(
                f"{section.section_id} rerender failed: {exc}"
            ) from exc
        rendered.append((section.section_id, section_bytes))
    return tuple(rendered)


def replay_structured_scientific_claim_paper(
    paper_content: bytes,
    claim_selection_content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Require exact code-rerendered bodies for all five governed sections."""

    _guard("replay_structured_scientific_claim_paper")
    paper = _exact_bytes(paper_content, "paper draft")
    try:
        document = parse_manuscript(paper.decode("utf-8"), strict=True)
    except (UnicodeDecodeError, ManuscriptStructureError, TypeError) as exc:
        raise ScientificClaimPublicationError(
            f"structured paper is not a strict manuscript: {exc}"
        ) from exc
    rendered = dict(
        replay_scientific_claim_selection_wrapper(
            claim_selection_content,
            binding=binding,
        )
    )
    governed: list[tuple[str, str, str]] = []
    for section in document.sections:
        matching = [
            section_id
            for section_id in SECTION_ORDER
            if _SECTION_HEADINGS[section_id] == section.title
        ]
        if matching:
            governed.append((matching[0], section.heading_source, section.body))
    if tuple(section_id for section_id, _heading, _body in governed) != SECTION_ORDER:
        raise ScientificClaimPublicationError(
            "paper governed sections are missing, duplicated, or reordered"
        )
    for section_id, heading_source, body in governed:
        heading = _SECTION_HEADINGS[section_id]
        if heading_source != f"## {heading}\n":
            raise ScientificClaimPublicationError(
                f"paper governed {heading} heading bytes are not deterministic"
            )
        expected_body = rendered[section_id].decode("utf-8")
        if body != f"\n{expected_body}\n\n":
            raise ScientificClaimPublicationError(
                f"paper governed {heading} body bytes differ from code rerender"
            )
    return paper


def build_structured_paper_structure_report(
    paper_content: bytes,
    claim_selection_content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Rebuild the existing Stage 17 structure-report schema in memory."""

    _guard("build_structured_paper_structure_report")
    paper = replay_structured_scientific_claim_paper(
        paper_content,
        claim_selection_content,
        binding=binding,
    )
    document = parse_manuscript(paper.decode("utf-8"), strict=True)
    return _canonical_bytes(
        {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "valid": True,
            "source_sha256": _sha256_bytes(paper),
            "section_count": len(document.sections),
            "issues": [],
        }
    )


def replay_stage17_paper_related_artifacts(
    *,
    binding: ScientificClaimGenerationBinding,
    claim_selection_content: bytes,
    paper_draft_content: bytes,
    paper_structure_report_content: bytes,
    experiment_fact_closure_report_content: bytes,
    citation_closure_report_content: bytes,
    citation_plan_content: bytes,
    citation_allowlist_content: bytes,
) -> None:
    """Replay the paper and existing closure schemas without filesystem I/O."""

    _guard("replay_stage17_paper_related_artifacts")
    paper = replay_structured_scientific_claim_paper(
        paper_draft_content,
        claim_selection_content,
        binding=binding,
    )
    expected_structure = build_structured_paper_structure_report(
        paper,
        claim_selection_content,
        binding=binding,
    )
    if paper_structure_report_content != expected_structure:
        raise ScientificClaimPublicationError(
            "paper structure report differs from independent rebuild"
        )
    try:
        try:
            experiment = replay_experiment_fact_closure(
                paper_bytes=paper,
                stored_report_bytes=experiment_fact_closure_report_content,
                evidence=binding.evidence,
            )
        except ExperimentFactClosureError:
            experiment = build_structured_experiment_fact_closure(
                paper,
                binding=binding,
            )
        expected_citation = build_citation_closure_from_texts(
            paper_text=paper.decode("utf-8"),
            structure_report_text=expected_structure.decode("utf-8"),
            experiment_fact_report_text=(
                experiment_fact_closure_report_content.decode("utf-8")
            ),
            citation_plan_text=citation_plan_content.decode("utf-8"),
            citation_allowlist_text=citation_allowlist_content.decode("utf-8"),
        )
        stored_citation = parse_citation_closure_report(
            citation_closure_report_content.decode("utf-8")
        )
    except (
        UnicodeDecodeError,
        ExperimentFactClosureError,
        CitationPlanContractError,
    ) as exc:
        raise ScientificClaimPublicationError(
            f"paper closure replay failed: {exc}"
        ) from exc
    if experiment.get("valid") is not True:
        raise ScientificClaimPublicationError(
            "experiment fact closure is not valid"
        )
    expected_experiment_bytes = canonical_experiment_fact_json_text(
        experiment
    ).encode("utf-8")
    if experiment_fact_closure_report_content != expected_experiment_bytes:
        raise ScientificClaimPublicationError(
            "experiment fact closure bytes are not canonical"
        )
    expected_citation_bytes = canonical_json_text(expected_citation).encode("utf-8")
    if (
        stored_citation != expected_citation
        or citation_closure_report_content != expected_citation_bytes
        or stored_citation.get("valid") is not True
    ):
        raise ScientificClaimPublicationError("citation closure replay mismatch")


def build_structured_experiment_fact_closure(
    paper_content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> dict[str, Any]:
    """Build the real closure after byte-proving code-owned governed prose."""

    paper = _exact_bytes(paper_content, "paper draft")
    try:
        document = parse_manuscript(paper.decode("utf-8"), strict=True)
        contract = _contract_from_evidence(binding.evidence)
        report = build_experiment_fact_closure_from_text(
            paper_text=paper.decode("utf-8"),
            evidence=binding.evidence,
            contract=contract,
        )
        ungoverned_paper = document.preamble + "".join(
            section.heading_source
            + ("\n" if section.title in _SECTION_HEADINGS.values() else section.body)
            for section in document.sections
        )
        ungoverned_report = build_experiment_fact_closure_from_text(
            paper_text=ungoverned_paper,
            evidence=binding.evidence,
            contract=contract,
        )
    except (
        UnicodeDecodeError,
        ManuscriptStructureError,
        ExperimentFactClosureError,
        ValueError,
    ) as exc:
        raise ScientificClaimPublicationError(
            f"structured experiment closure build failed: {exc}"
        ) from exc
    violations = ungoverned_report.get("structured_fact_violations")
    if not isinstance(violations, list):
        raise ScientificClaimPublicationError(
            "structured experiment closure lacks violation inventory"
        )
    report["structured_fact_violations"] = violations
    report["unknown_numeric_values"] = ungoverned_report[
        "unknown_numeric_values"
    ]
    report["valid"] = not (
        report.get("unknown_numeric_values")
        or report.get("dataset_claim_violations")
        or report["structured_fact_violations"]
    )
    try:
        return parse_experiment_fact_closure_report(
            canonical_experiment_fact_json_text(report)
        )
    except ExperimentFactClosureError as exc:
        raise ScientificClaimPublicationError(
            f"structured experiment closure replay failed: {exc}"
        ) from exc


def build_stage17_scientific_claim_authority_manifest(
    *,
    binding: ScientificClaimGenerationBinding,
    facts_content: bytes,
    claim_registry_content: bytes,
    claim_selection_content: bytes,
    paper_draft_content: bytes,
    paper_structure_report_content: bytes,
    experiment_fact_closure_report_content: bytes,
    citation_closure_report_content: bytes,
    citation_plan_content: bytes,
    citation_allowlist_content: bytes,
) -> bytes:
    """Build an admitted Stage 17 manifest entirely from captured bytes."""

    capability.require_complete_structured_capability(
        "build_stage17_scientific_claim_authority_manifest"
    )
    facts = parse_scientific_evidence_facts(facts_content, binding=binding)
    claims = parse_scientific_claim_registry(claim_registry_content, binding=binding)
    selection = parse_scientific_claim_selection_wrapper(
        claim_selection_content, binding=binding
    )
    replay_scientific_claim_selection_wrapper(
        claim_selection_content, binding=binding
    )
    replay_stage17_paper_related_artifacts(
        binding=binding,
        claim_selection_content=claim_selection_content,
        paper_draft_content=paper_draft_content,
        paper_structure_report_content=paper_structure_report_content,
        experiment_fact_closure_report_content=experiment_fact_closure_report_content,
        citation_closure_report_content=citation_closure_report_content,
        citation_plan_content=citation_plan_content,
        citation_allowlist_content=citation_allowlist_content,
    )

    snapshot = capability.code_owned_structured_capability_snapshot()
    payload = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "publication_stage_id": PUBLICATION_STAGE_ID,
        "claim_policy_id": CLAIM_POLICY_ID,
        "structured_capability_schema_version": (
            capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION
        ),
        "structured_capability_snapshot": snapshot,
        "generation_binding_sha256": binding.generation_binding_sha256,
        "canonical_experiment_evidence": _file_ref(
            binding.canonical_experiment_evidence_path,
            binding.canonical_experiment_evidence_sha256,
        ),
        "experiment_contract": _file_ref(
            binding.experiment_contract_path,
            binding.experiment_contract_sha256,
        ),
        "run_config": _file_ref(
            binding.run_config_path,
            binding.run_config_sha256,
        ),
        "cfs": {
            "schema_version": binding.cfs_schema_version,
            "sha256": binding.cfs_sha256,
        },
        "facts": {
            "file": _file_ref(_FACTS_PATH, _sha256_bytes(facts_content)),
            "record_count": len(facts),
        },
        "claim_registry": {
            "file": _file_ref(_REGISTRY_PATH, _sha256_bytes(claim_registry_content)),
            "record_count": len(claims),
        },
        "claim_selection": {
            "file": _file_ref(_SELECTION_PATH, _sha256_bytes(claim_selection_content)),
            "section_count": len(selection.sections),
        },
        "paper_draft": _file_ref(_PAPER_PATH, _sha256_bytes(paper_draft_content)),
        "paper_structure_report": _file_ref(
            _STRUCTURE_REPORT_PATH, _sha256_bytes(paper_structure_report_content)
        ),
        "experiment_fact_closure_report": _file_ref(
            _FACT_CLOSURE_PATH,
            _sha256_bytes(experiment_fact_closure_report_content),
        ),
        "citation_closure_report": _file_ref(
            _CITATION_CLOSURE_PATH,
            _sha256_bytes(citation_closure_report_content),
        ),
        "source_authority_manifest": None,
    }
    return _canonical_bytes(payload)


def parse_stage17_scientific_claim_authority_manifest(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
    facts_content: bytes,
    claim_registry_content: bytes,
    claim_selection_content: bytes,
    paper_draft_content: bytes,
    paper_structure_report_content: bytes,
    experiment_fact_closure_report_content: bytes,
    citation_closure_report_content: bytes,
    citation_plan_content: bytes,
    citation_allowlist_content: bytes,
) -> Mapping[str, Any]:
    """Guard, parse, and independently rebuild a Stage 17 manifest."""

    capability.require_complete_structured_capability(
        "parse_stage17_scientific_claim_authority_manifest"
    )
    payload = _parse_exact_object(
        content,
        fields=_MANIFEST_FIELDS,
        label="Stage 17 scientific claim authority manifest",
    )
    _validate_stage17_manifest_shape(payload)
    expected = build_stage17_scientific_claim_authority_manifest(
        binding=binding,
        facts_content=facts_content,
        claim_registry_content=claim_registry_content,
        claim_selection_content=claim_selection_content,
        paper_draft_content=paper_draft_content,
        paper_structure_report_content=paper_structure_report_content,
        experiment_fact_closure_report_content=experiment_fact_closure_report_content,
        citation_closure_report_content=citation_closure_report_content,
        citation_plan_content=citation_plan_content,
        citation_allowlist_content=citation_allowlist_content,
    )
    if content != expected:
        raise ScientificClaimPublicationError(
            "Stage 17 manifest differs from independent code-owned rebuild"
        )
    return payload


def _validate_stage17_manifest_shape(payload: Mapping[str, Any]) -> None:
    _true_int_one(payload["schema_version"], "manifest schema_version")
    _true_int_one(
        payload["structured_capability_schema_version"],
        "structured capability schema version",
    )
    if payload["publication_stage_id"] != PUBLICATION_STAGE_ID:
        raise ScientificClaimPublicationError("publication_stage_id must be stage17")
    if payload["claim_policy_id"] != CLAIM_POLICY_ID:
        raise ScientificClaimPublicationError("manifest claim policy mismatch")
    _sha256(payload["generation_binding_sha256"], "generation binding")

    snapshot = payload["structured_capability_snapshot"]
    expected_snapshot = capability.code_owned_structured_capability_snapshot()
    if type(snapshot) is not dict or snapshot != expected_snapshot:
        raise ScientificClaimPublicationError("capability snapshot is not code-owned")
    if not capability.structured_publication_is_eligible():
        raise ScientificClaimPublicationError("capability snapshot is incomplete")

    for field in (
        "canonical_experiment_evidence",
        "experiment_contract",
        "run_config",
        "paper_draft",
        "paper_structure_report",
        "experiment_fact_closure_report",
        "citation_closure_report",
    ):
        _parse_file_ref(payload[field], field)
    cfs = payload["cfs"]
    if not isinstance(cfs, dict) or set(cfs) != {"schema_version", "sha256"}:
        raise ScientificClaimPublicationError("cfs manifest binding fields mismatch")
    _true_int_one(cfs["schema_version"], "cfs schema_version")
    _sha256(cfs["sha256"], "cfs sha256")
    for field, count_field, expected_count in (
        ("facts", "record_count", FACT_RECORD_COUNT),
        ("claim_registry", "record_count", CLAIM_RECORD_COUNT),
        ("claim_selection", "section_count", len(SECTION_ORDER)),
    ):
        item = payload[field]
        if not isinstance(item, dict) or set(item) != {"file", count_field}:
            raise ScientificClaimPublicationError(f"{field} binding fields mismatch")
        _parse_file_ref(item["file"], f"{field}.file")
        if type(item[count_field]) is not int or item[count_field] != expected_count:
            raise ScientificClaimPublicationError(
                f"{field}.{count_field} must be the true integer {expected_count}"
            )
    if payload["source_authority_manifest"] is not None:
        raise ScientificClaimPublicationError(
            "Stage 17 source_authority_manifest must be present JSON null"
        )


def _guard(entrypoint: str) -> None:
    capability.require_complete_structured_capability(entrypoint)


def _parse_exact_object(
    content: bytes,
    *,
    fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    value = parse_canonical_publication_json(content, label=label)
    if not isinstance(value, dict):
        raise ScientificClaimPublicationError(f"{label} root must be an object")
    if set(value) != fields:
        raise ScientificClaimPublicationError(
            f"{label} fields mismatch: "
            f"missing={sorted(fields - set(value))}, "
            f"extra={sorted(set(value) - fields)}"
        )
    return value


def _parse_file_ref(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != _FILE_REF_FIELDS:
        raise ScientificClaimPublicationError(f"{label} must be an exact FileRef")
    path = _safe_path(value["path"], f"{label}.path")
    digest = _sha256(value["sha256"], f"{label}.sha256")
    return path, digest


def _file_ref(path: str, digest: str) -> dict[str, str]:
    return {
        "path": _safe_path(path, "FileRef.path"),
        "sha256": _sha256(digest, "FileRef.sha256"),
    }


def _safe_path(value: object, label: str) -> str:
    if type(value) is not str or value != value.strip():
        raise ScientificClaimPublicationError(f"{label} is unsafe")
    try:
        return validate_release_authority_path(value)
    except IndependentReleaseReconstructionError as exc:
        raise ScientificClaimPublicationError(f"{label} is unsafe: {exc}") from exc


def _true_int_one(value: object, label: str) -> int:
    if type(value) is not int or value != PUBLICATION_SCHEMA_VERSION:
        raise ScientificClaimPublicationError(f"{label} must be the true integer 1")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ScientificClaimPublicationError(f"{label} must be lowercase SHA-256")
    return value


def _exact_bytes(value: object, label: str) -> bytes:
    if type(value) is not bytes:
        raise ScientificClaimPublicationError(f"{label} must be exact bytes")
    return value


def _sha256_bytes(value: object) -> str:
    return hashlib.sha256(_exact_bytes(value, "artifact content")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return canonical_authority_json_text(value).encode("utf-8")
    except (UnicodeEncodeError, CanonicalExperimentEvidenceError) as exc:
        raise ScientificClaimPublicationError(
            f"value is not canonical authority JSON: {exc}"
        ) from exc
