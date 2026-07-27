"""Pure authority contracts for structured Stage 19 revision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from researchclaw.literature.citation_plan import (
    build_citation_closure_from_texts,
    parse_citation_plan,
    parse_citation_closure_report,
)
from researchclaw.literature.evidence_cards import canonical_json_text
from researchclaw.literature.experiment_fact_closure import (
    canonical_experiment_fact_json_text,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.scientific_claim_authority import (
    CLAIM_POLICY_ID,
    ScientificClaimGenerationBinding,
    build_scientific_claim_registry,
    render_scientific_claim_selection,
    validate_scientific_claim_selection,
)
from researchclaw.pipeline.manuscript_sections import (
    ManuscriptStructureError,
    parse_manuscript,
)
from researchclaw.pipeline.scientific_claim_publication import (
    SECTION_ORDER,
    ScientificClaimSectionSelection,
    ScientificClaimSelectionArtifact,
    build_structured_experiment_fact_closure,
)


_PROVIDER_FIELDS = frozenset(
    {
        "selected_claim_ids",
        "ordered_claim_ids",
        "connector_template_ids",
    }
)
_SECTION_HEADINGS = {
    "abstract": "Abstract",
    "results": "Results",
    "discussion": "Discussion",
    "limitations": "Limitations",
    "conclusion": "Conclusion",
}
_STAGE17_MANIFEST_FIELDS = frozenset(
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
STAGE19_MANIFEST_FIELDS = frozenset(
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
        "source_claim_selection",
        "claim_selection",
        "source_paper_draft",
        "paper_revised",
        "paper_structure_report",
        "experiment_fact_closure_report",
        "citation_closure_report",
        "stage18_review",
        "source_authority_manifest",
    }
)


class StructuredStage19AuthorityError(ValueError):
    """Structured Stage 19 authority is malformed or semantically invalid."""


@dataclass(frozen=True)
class ProviderSelection:
    selected_claim_ids: tuple[str, ...]
    ordered_claim_ids: tuple[str, ...]
    connector_template_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReplayedStage17Authority:
    selection: ScientificClaimSelectionArtifact
    manifest: Mapping[str, Any]
    paper: bytes
    structure_report: bytes
    experiment_fact_closure_report: bytes
    citation_closure_report: bytes


@dataclass(frozen=True)
class Stage19OutputDigests:
    selection: str
    paper: str
    structure_report: str
    experiment_fact_closure_report: str
    citation_closure_report: str


def parse_provider_selection(content: bytes) -> ProviderSelection:
    """Parse the exact three-field, ID-only provider response."""

    try:
        text = content.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredStage19AuthorityError(
            "structured Stage 19 provider response is not strict JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != _PROVIDER_FIELDS:
        raise StructuredStage19AuthorityError(
            "structured Stage 19 provider fields mismatch"
        )
    return ProviderSelection(
        selected_claim_ids=_string_tuple(
            value["selected_claim_ids"], "selected_claim_ids"
        ),
        ordered_claim_ids=_string_tuple(
            value["ordered_claim_ids"], "ordered_claim_ids"
        ),
        connector_template_ids=_string_tuple(
            value["connector_template_ids"], "connector_template_ids"
        ),
    )


def validate_descendant_selection(
    content: bytes,
    *,
    target_section: str,
    source: ScientificClaimSectionSelection,
    binding: ScientificClaimGenerationBinding,
) -> ProviderSelection:
    """Require one selection to be an ID-only descendant of Stage 17."""

    selection = parse_provider_selection(content)
    if source.section_id != target_section or target_section not in SECTION_ORDER:
        raise StructuredStage19AuthorityError("selection target section mismatch")
    selected = selection.selected_claim_ids
    ordered = selection.ordered_claim_ids
    connectors = selection.connector_template_ids
    if len(selected) != len(set(selected)):
        raise StructuredStage19AuthorityError("selected claim IDs contain duplicates")
    source_ids = frozenset(source.selected_claim_ids)
    if any(claim_id not in source_ids for claim_id in selected):
        raise StructuredStage19AuthorityError(
            "selection is not a descendant of Stage 17"
        )
    registry = build_scientific_claim_registry(binding)
    claims = {claim.claim_id: claim for claim in registry.claims}
    mandatory = {
        claim.claim_id
        for claim in registry.claims
        if claim.section_id == target_section and claim.mandatory
    }
    if not mandatory.issubset(selected):
        raise StructuredStage19AuthorityError("mandatory claim was deleted")
    if any(
        claim_id not in claims or claims[claim_id].section_id != target_section
        for claim_id in selected
    ):
        raise StructuredStage19AuthorityError("claim section membership mismatch")
    if len(ordered) != len(set(ordered)) or set(ordered) != set(selected):
        raise StructuredStage19AuthorityError(
            "ordered claim IDs are not an exact permutation"
        )
    if len(connectors) != max(len(ordered) - 1, 0) or any(
        item != "NONE" for item in connectors
    ):
        raise StructuredStage19AuthorityError("connector policy mismatch")
    return selection


def build_stage19_selection(
    responses: Sequence[bytes],
    *,
    source: ScientificClaimSelectionArtifact,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Build the canonical five-section Stage 19 selection wrapper."""

    if len(responses) != len(SECTION_ORDER) or len(source.sections) != len(
        SECTION_ORDER
    ):
        raise StructuredStage19AuthorityError("selection section count mismatch")
    sections: list[dict[str, object]] = []
    for section_id, source_section, response in zip(
        SECTION_ORDER, source.sections, responses, strict=True
    ):
        parsed = validate_descendant_selection(
            response,
            target_section=section_id,
            source=source_section,
            binding=binding,
        )
        sections.append(
            {
                "section_id": section_id,
                "selected_claim_ids": list(parsed.selected_claim_ids),
                "ordered_claim_ids": list(parsed.ordered_claim_ids),
                "connector_template_ids": list(parsed.connector_template_ids),
            }
        )
    registry = build_scientific_claim_registry(binding)
    return canonical_authority_json_text(
        {
            "schema_version": 1,
            "claim_policy_id": CLAIM_POLICY_ID,
            "generation_binding_sha256": binding.generation_binding_sha256,
            "claim_registry_sha256": registry.claims_sha256,
            "sections": sections,
        }
    ).encode("utf-8")


def inherited_provider_responses(
    source: ScientificClaimSelectionArtifact,
) -> tuple[bytes, ...]:
    """Return exact provider-shaped bytes for the strict no-op branch."""

    return tuple(
        canonical_authority_json_text(section.provider_dict()).encode("utf-8")
        for section in source.sections
    )


def rerender_stage19_paper(
    source_paper: bytes,
    selection_content: bytes,
    *,
    source_selection: ScientificClaimSelectionArtifact,
    binding: ScientificClaimGenerationBinding,
) -> bytes:
    """Splice only five governed bodies into the captured Stage 17 paper."""

    payload = _canonical_object(selection_content, "Stage 19 selection")
    expected_root = {
        "schema_version",
        "claim_policy_id",
        "generation_binding_sha256",
        "claim_registry_sha256",
        "sections",
    }
    if set(payload) != expected_root:
        raise StructuredStage19AuthorityError("Stage 19 selection fields mismatch")
    sections = payload["sections"]
    if not isinstance(sections, list) or len(sections) != len(SECTION_ORDER):
        raise StructuredStage19AuthorityError("Stage 19 section count mismatch")
    rendered: dict[str, str] = {}
    for section_id, source_section, item in zip(
        SECTION_ORDER, source_selection.sections, sections, strict=True
    ):
        if not isinstance(item, dict) or item.get("section_id") != section_id:
            raise StructuredStage19AuthorityError("Stage 19 section order mismatch")
        provider = {
            name: item.get(name)
            for name in (
                "selected_claim_ids",
                "ordered_claim_ids",
                "connector_template_ids",
            )
        }
        response = canonical_authority_json_text(provider).encode("utf-8")
        validate_descendant_selection(
            response,
            target_section=section_id,
            source=source_section,
            binding=binding,
        )
        rendered[section_id] = render_scientific_claim_selection(
            response, target_section=section_id, binding=binding
        ).decode("utf-8")
    try:
        source_text = source_paper.decode("utf-8")
        document = parse_manuscript(source_text, strict=True)
    except (UnicodeDecodeError, ManuscriptStructureError) as exc:
        raise StructuredStage19AuthorityError("source paper is not strict") from exc
    governed: list[str] = []
    pieces = [document.preamble]
    for section in document.sections:
        matches = [
            section_id
            for section_id, title in _SECTION_HEADINGS.items()
            if section.title == title
        ]
        if matches:
            section_id = matches[0]
            governed.append(section_id)
            if section.heading_source != f"## {_SECTION_HEADINGS[section_id]}\n":
                raise StructuredStage19AuthorityError(
                    "governed heading bytes are not deterministic"
                )
            pieces.append(section.heading_source + "\n" + rendered[section_id] + "\n\n")
        else:
            pieces.append(section.source)
    if tuple(governed) != SECTION_ORDER:
        raise StructuredStage19AuthorityError(
            "governed sections are missing, duplicated, or reordered"
        )
    return "".join(pieces).encode("utf-8")


def replay_stage17_authority(
    files: Mapping[str, bytes],
    *,
    binding: ScientificClaimGenerationBinding,
    citation_plan: bytes,
    citation_allowlist: bytes,
    expected_capability_snapshot: Mapping[str, int],
) -> ReplayedStage17Authority:
    """Independently rebuild the complete eight-file Stage 17 authority."""

    names = {
        "facts": "scientific_evidence_facts.json",
        "registry": "scientific_claim_registry.json",
        "selection": "scientific_claim_selection.json",
        "paper": "paper_draft.md",
        "structure": "paper_structure_report.json",
        "experiment": "experiment_fact_closure_report.json",
        "citation": "citation_closure_report.json",
        "manifest": "scientific_claim_authority_manifest.json",
    }
    if set(files) != set(names.values()):
        raise StructuredStage19AuthorityError("Stage 17 authority namespace mismatch")
    registry = build_scientific_claim_registry(binding)
    if files[names["facts"]] != registry.facts_bytes():
        raise StructuredStage19AuthorityError(
            "Stage 17 facts differ from independent rebuild"
        )
    if files[names["registry"]] != registry.claims_bytes():
        raise StructuredStage19AuthorityError(
            "Stage 17 registry differs from independent rebuild"
        )
    selection = _parse_stage17_selection(
        files[names["selection"]], binding=binding
    )
    expected_paper = _assemble_stage17_paper(
        files[names["selection"]],
        binding=binding,
        citation_plan=citation_plan,
    )
    if expected_paper != files[names["paper"]]:
        raise StructuredStage19AuthorityError(
            "Stage 17 paper differs from independent rerender"
        )
    structure = _build_structure_report(expected_paper)
    if files[names["structure"]] != structure:
        raise StructuredStage19AuthorityError(
            "Stage 17 structure report differs from independent rebuild"
        )
    experiment = build_structured_experiment_fact_closure(
        expected_paper, binding=binding
    )
    experiment_bytes = canonical_experiment_fact_json_text(experiment).encode(
        "utf-8"
    )
    if files[names["experiment"]] != experiment_bytes:
        raise StructuredStage19AuthorityError(
            "Stage 17 experiment closure differs from independent rebuild"
        )
    try:
        citation = build_citation_closure_from_texts(
            paper_text=expected_paper.decode("utf-8"),
            structure_report_text=structure.decode("utf-8"),
            experiment_fact_report_text=experiment_bytes.decode("utf-8"),
            citation_plan_text=citation_plan.decode("utf-8"),
            citation_allowlist_text=citation_allowlist.decode("utf-8"),
        )
        stored_citation = parse_citation_closure_report(
            files[names["citation"]].decode("utf-8")
        )
    except Exception as exc:
        raise StructuredStage19AuthorityError(
            "Stage 17 citation closure replay failed"
        ) from exc
    citation_bytes = canonical_json_text(citation).encode("utf-8")
    if (
        files[names["citation"]] != citation_bytes
        or stored_citation != citation
        or citation.get("valid") is not True
    ):
        raise StructuredStage19AuthorityError(
            "Stage 17 citation closure differs from independent rebuild"
        )
    manifest = _canonical_object(files[names["manifest"]], "Stage 17 manifest")
    _validate_stage17_manifest(
        manifest,
        files=files,
        binding=binding,
        expected_capability_snapshot=expected_capability_snapshot,
    )
    return ReplayedStage17Authority(
        selection,
        manifest,
        expected_paper,
        structure,
        experiment_bytes,
        citation_bytes,
    )


def build_stage19_closures(
    paper: bytes,
    selection: bytes,
    *,
    source_selection: ScientificClaimSelectionArtifact,
    binding: ScientificClaimGenerationBinding,
    citation_plan: bytes,
    citation_allowlist: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Rebuild all three Stage 19 closures from the deterministic revised paper."""

    replayed_paper = rerender_stage19_paper(
        paper,
        selection,
        source_selection=source_selection,
        binding=binding,
    )
    if replayed_paper != paper:
        raise StructuredStage19AuthorityError("Stage 19 paper replay mismatch")
    structure = _build_structure_report(paper)
    experiment = build_structured_experiment_fact_closure(paper, binding=binding)
    experiment_bytes = canonical_experiment_fact_json_text(experiment).encode(
        "utf-8"
    )
    try:
        citation = build_citation_closure_from_texts(
            paper_text=paper.decode("utf-8"),
            structure_report_text=structure.decode("utf-8"),
            experiment_fact_report_text=experiment_bytes.decode("utf-8"),
            citation_plan_text=citation_plan.decode("utf-8"),
            citation_allowlist_text=citation_allowlist.decode("utf-8"),
        )
    except Exception as exc:
        raise StructuredStage19AuthorityError(
            "Stage 19 citation closure build failed"
        ) from exc
    citation_bytes = canonical_json_text(citation).encode("utf-8")
    if experiment.get("valid") is not True or citation.get("valid") is not True:
        raise StructuredStage19AuthorityError("Stage 19 closure is not valid")
    return structure, experiment_bytes, citation_bytes


def replay_stage19_outputs(
    outputs: Mapping[str, bytes],
    *,
    source_paper: bytes,
    source_selection: ScientificClaimSelectionArtifact,
    binding: ScientificClaimGenerationBinding,
    citation_plan: bytes,
    citation_allowlist: bytes,
) -> None:
    """Independently rebuild all five non-manifest Stage 19 outputs."""

    expected_names = {
        "scientific_claim_selection.json",
        "scientific_claim_paper_revised.md",
        "scientific_claim_paper_structure_report.json",
        "scientific_claim_experiment_fact_closure_report.json",
        "scientific_claim_citation_closure_report.json",
    }
    if set(outputs) != expected_names:
        raise StructuredStage19AuthorityError("Stage 19 output namespace mismatch")
    selection = outputs["scientific_claim_selection.json"]
    payload = _canonical_object(selection, "Stage 19 selection")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        raise StructuredStage19AuthorityError("Stage 19 selection sections missing")
    responses: list[bytes] = []
    for section_id, item in zip(SECTION_ORDER, raw_sections, strict=True):
        if not isinstance(item, dict) or item.get("section_id") != section_id:
            raise StructuredStage19AuthorityError("Stage 19 section order mismatch")
        responses.append(
            canonical_authority_json_text(
                {
                    key: item.get(key)
                    for key in (
                        "selected_claim_ids",
                        "ordered_claim_ids",
                        "connector_template_ids",
                    )
                }
            ).encode("utf-8")
        )
    expected_selection = build_stage19_selection(
        responses, source=source_selection, binding=binding
    )
    if selection != expected_selection:
        raise StructuredStage19AuthorityError(
            "Stage 19 selection differs from independent rebuild"
        )
    expected_paper = rerender_stage19_paper(
        source_paper,
        expected_selection,
        source_selection=source_selection,
        binding=binding,
    )
    if outputs["scientific_claim_paper_revised.md"] != expected_paper:
        raise StructuredStage19AuthorityError(
            "Stage 19 paper differs from independent rerender"
        )
    structure, experiment, citation = build_stage19_closures(
        expected_paper,
        expected_selection,
        source_selection=source_selection,
        binding=binding,
        citation_plan=citation_plan,
        citation_allowlist=citation_allowlist,
    )
    expected_reports = {
        "scientific_claim_paper_structure_report.json": structure,
        "scientific_claim_experiment_fact_closure_report.json": experiment,
        "scientific_claim_citation_closure_report.json": citation,
    }
    for name, expected in expected_reports.items():
        if outputs[name] != expected:
            raise StructuredStage19AuthorityError(
                f"{name} differs from independent rebuild"
            )


def build_stage19_manifest(
    *,
    binding: ScientificClaimGenerationBinding,
    capability_snapshot: Mapping[str, int],
    stage17_manifest: Mapping[str, Any],
    stage17_manifest_sha256: str,
    stage18_reviews_sha256: str,
    stage18_structure_report_sha256: str,
    stage18_comment_count: int,
    outputs: Stage19OutputDigests,
) -> bytes:
    """Build the exact schema-v2 manifest without a self-reference."""

    _validate_capability_snapshot(capability_snapshot)
    if set(stage17_manifest) != _STAGE17_MANIFEST_FIELDS:
        raise StructuredStage19AuthorityError(
            "source Stage 17 manifest fields mismatch"
        )
    if type(stage18_comment_count) is not int or stage18_comment_count < 0:
        raise StructuredStage19AuthorityError("Stage 18 comment count is invalid")
    for digest in (
        stage17_manifest_sha256,
        stage18_reviews_sha256,
        stage18_structure_report_sha256,
        outputs.selection,
        outputs.paper,
        outputs.structure_report,
        outputs.experiment_fact_closure_report,
        outputs.citation_closure_report,
    ):
        _digest(digest)
    payload = {
        "schema_version": 2,
        "publication_stage_id": "stage19",
        "claim_policy_id": CLAIM_POLICY_ID,
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": dict(capability_snapshot),
        "generation_binding_sha256": binding.generation_binding_sha256,
        "canonical_experiment_evidence": _copy_ref(
            stage17_manifest["canonical_experiment_evidence"]
        ),
        "experiment_contract": _copy_ref(stage17_manifest["experiment_contract"]),
        "run_config": _copy_ref(stage17_manifest["run_config"]),
        "cfs": {
            "schema_version": binding.cfs_schema_version,
            "sha256": binding.cfs_sha256,
        },
        "facts": {
            "file": _copy_ref(stage17_manifest["facts"]["file"]),
            "record_count": 5,
        },
        "claim_registry": {
            "file": _copy_ref(stage17_manifest["claim_registry"]["file"]),
            "record_count": 6,
        },
        "source_claim_selection": {
            "file": _copy_ref(stage17_manifest["claim_selection"]["file"]),
            "section_count": 5,
        },
        "claim_selection": {
            "file": _ref(
                "stage-19/scientific_claim_selection.json", outputs.selection
            ),
            "section_count": 5,
        },
        "source_paper_draft": _copy_ref(stage17_manifest["paper_draft"]),
        "paper_revised": _ref(
            "stage-19/scientific_claim_paper_revised.md", outputs.paper
        ),
        "paper_structure_report": _ref(
            "stage-19/scientific_claim_paper_structure_report.json",
            outputs.structure_report,
        ),
        "experiment_fact_closure_report": _ref(
            "stage-19/scientific_claim_experiment_fact_closure_report.json",
            outputs.experiment_fact_closure_report,
        ),
        "citation_closure_report": _ref(
            "stage-19/scientific_claim_citation_closure_report.json",
            outputs.citation_closure_report,
        ),
        "stage18_review": {
            "reviews": _ref("stage-18/reviews.md", stage18_reviews_sha256),
            "structure_report": _ref(
                "stage-18/review_structure_report.json",
                stage18_structure_report_sha256,
            ),
            "structure_schema_version": 2,
            "comment_count": stage18_comment_count,
        },
        "source_authority_manifest": _ref(
            "stage-17/scientific_claim_authority_manifest.json",
            stage17_manifest_sha256,
        ),
    }
    if set(payload) != STAGE19_MANIFEST_FIELDS:
        raise StructuredStage19AuthorityError("Stage 19 manifest builder drift")
    return canonical_authority_json_text(payload).encode("utf-8")


def replay_stage19_manifest(
    content: bytes,
    *,
    expected: bytes,
) -> Mapping[str, Any]:
    """Strictly parse schema v2 and require independent expected bytes."""

    payload = _canonical_object(content, "Stage 19 manifest")
    if set(payload) != STAGE19_MANIFEST_FIELDS:
        raise StructuredStage19AuthorityError("Stage 19 manifest fields mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 2
        or payload["publication_stage_id"] != "stage19"
        or payload["claim_policy_id"] != CLAIM_POLICY_ID
        or type(payload["structured_capability_schema_version"]) is not int
        or payload["structured_capability_schema_version"] != 1
    ):
        raise StructuredStage19AuthorityError("Stage 19 manifest header mismatch")
    _validate_capability_snapshot(payload["structured_capability_snapshot"])
    _validate_stage19_manifest_nested(payload)
    if content != expected:
        raise StructuredStage19AuthorityError(
            "Stage 19 manifest differs from independent expected rebuild"
        )
    return payload


def _validate_stage19_manifest_nested(payload: Mapping[str, Any]) -> None:
    for field in (
        "canonical_experiment_evidence",
        "experiment_contract",
        "run_config",
        "source_paper_draft",
        "paper_revised",
        "paper_structure_report",
        "experiment_fact_closure_report",
        "citation_closure_report",
        "source_authority_manifest",
    ):
        _parse_ref(payload[field], field)
    cfs = payload["cfs"]
    if (
        not isinstance(cfs, dict)
        or set(cfs) != {"schema_version", "sha256"}
        or type(cfs["schema_version"]) is not int
        or cfs["schema_version"] != 1
    ):
        raise StructuredStage19AuthorityError("Stage 19 CFS shape mismatch")
    _digest(cfs["sha256"])
    for field, count_field, count in (
        ("facts", "record_count", 5),
        ("claim_registry", "record_count", 6),
        ("source_claim_selection", "section_count", 5),
        ("claim_selection", "section_count", 5),
    ):
        value = payload[field]
        if (
            not isinstance(value, dict)
            or set(value) != {"file", count_field}
            or type(value[count_field]) is not int
            or value[count_field] != count
        ):
            raise StructuredStage19AuthorityError(
                f"Stage 19 manifest {field} shape mismatch"
            )
        _parse_ref(value["file"], field)
    review = payload["stage18_review"]
    if (
        not isinstance(review, dict)
        or set(review)
        != {
            "reviews",
            "structure_report",
            "structure_schema_version",
            "comment_count",
        }
        or type(review["structure_schema_version"]) is not int
        or review["structure_schema_version"] != 2
        or type(review["comment_count"]) is not int
        or review["comment_count"] < 0
    ):
        raise StructuredStage19AuthorityError("Stage 18 manifest binding mismatch")
    _parse_ref(review["reviews"], "Stage 18 reviews")
    _parse_ref(review["structure_report"], "Stage 18 structure report")


def _validate_capability_snapshot(value: object) -> None:
    keys = {
        "stage17_publication",
        "stage19_revision",
        "stage20_replay",
        "stage24_and_release_integration",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StructuredStage19AuthorityError("capability snapshot fields mismatch")
    if any(type(value[key]) is not int or value[key] not in {0, 1} for key in keys):
        raise StructuredStage19AuthorityError("capability snapshot values mismatch")
    states = tuple(value[key] for key in (
        "stage17_publication",
        "stage19_revision",
        "stage20_replay",
        "stage24_and_release_integration",
    ))
    if states not in {(1, 0, 0, 0), (1, 1, 1, 1)}:
        raise StructuredStage19AuthorityError(
            "Stage 19 manifest capability state is ineligible"
        )


def _copy_ref(value: object) -> dict[str, str]:
    path, digest = _parse_ref(value, "source FileRef")
    return _ref(path, digest)


def _parse_ref(value: object, label: str) -> tuple[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or not isinstance(value["path"], str)
        or not value["path"]
        or value["path"].startswith("/")
        or ".." in value["path"].split("/")
    ):
        raise StructuredStage19AuthorityError(f"{label} FileRef shape mismatch")
    return value["path"], _digest(value["sha256"])


def _ref(path: str, digest: str) -> dict[str, str]:
    return {"path": path, "sha256": _digest(digest)}


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StructuredStage19AuthorityError("SHA-256 digest is invalid")
    return value


def _parse_stage17_selection(
    content: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
) -> ScientificClaimSelectionArtifact:
    payload = _canonical_object(content, "Stage 17 selection")
    expected = {
        "schema_version",
        "claim_policy_id",
        "generation_binding_sha256",
        "claim_registry_sha256",
        "sections",
    }
    registry = build_scientific_claim_registry(binding)
    if (
        set(payload) != expected
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["claim_policy_id"] != CLAIM_POLICY_ID
        or payload["generation_binding_sha256"]
        != binding.generation_binding_sha256
        or payload["claim_registry_sha256"] != registry.claims_sha256
    ):
        raise StructuredStage19AuthorityError("Stage 17 selection root mismatch")
    raw_sections = payload["sections"]
    if not isinstance(raw_sections, list) or len(raw_sections) != len(SECTION_ORDER):
        raise StructuredStage19AuthorityError("Stage 17 selection count mismatch")
    sections: list[ScientificClaimSectionSelection] = []
    for section_id, item in zip(SECTION_ORDER, raw_sections, strict=True):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "section_id",
                "selected_claim_ids",
                "ordered_claim_ids",
                "connector_template_ids",
            }
            or item["section_id"] != section_id
        ):
            raise StructuredStage19AuthorityError(
                "Stage 17 selection section mismatch"
            )
        provider = {
            key: item[key]
            for key in (
                "selected_claim_ids",
                "ordered_claim_ids",
                "connector_template_ids",
            )
        }
        response = canonical_authority_json_text(provider).encode("utf-8")
        try:
            parsed = validate_scientific_claim_selection(
                response, target_section=section_id, binding=binding
            )
        except Exception as exc:
            raise StructuredStage19AuthorityError(
                f"Stage 17 {section_id} selection replay failed"
            ) from exc
        sections.append(
            ScientificClaimSectionSelection(
                section_id,
                parsed.selected_claim_ids,
                parsed.ordered_claim_ids,
                parsed.connector_template_ids,
            )
        )
    return ScientificClaimSelectionArtifact(
        1,
        CLAIM_POLICY_ID,
        binding.generation_binding_sha256,
        registry.claims_sha256,
        tuple(sections),
    )


def _build_structure_report(paper: bytes) -> bytes:
    try:
        document = parse_manuscript(paper.decode("utf-8"), strict=True)
    except (UnicodeDecodeError, ManuscriptStructureError) as exc:
        raise StructuredStage19AuthorityError("paper structure replay failed") from exc
    return canonical_authority_json_text(
        {
            "schema_version": 1,
            "valid": True,
            "source_sha256": hashlib.sha256(paper).hexdigest(),
            "section_count": len(document.sections),
            "issues": [],
        }
    ).encode("utf-8")


def _assemble_stage17_paper(
    selection: bytes,
    *,
    binding: ScientificClaimGenerationBinding,
    citation_plan: bytes,
) -> bytes:
    """Independently rebuild the complete deterministic Stage 17 paper."""

    try:
        plan = parse_citation_plan(citation_plan.decode("utf-8"))
    except Exception as exc:
        raise StructuredStage19AuthorityError(
            "Stage 17 citation plan replay failed"
        ) from exc
    citation_sections: dict[str, list[str]] = {}
    for claim in plan["claims"]:
        section = claim["section_path"][0]
        text = claim["claim_text"]
        key = claim["planned_citations"][0]["cite_key"]
        if text.endswith((".", "!", "?")):
            rendered = f"{text[:-1]} [{key}]{text[-1]}"
        else:
            rendered = f"{text} [{key}]"
        citation_sections.setdefault(section, []).append(rendered)
    parts: list[bytes] = []
    for section, sentences in citation_sections.items():
        parts.append(f"## {section}\n\n".encode("utf-8"))
        parts.append(("\n\n".join(sentences) + "\n\n").encode("utf-8"))
    source = _parse_stage17_selection(selection, binding=binding)
    for section in source.sections:
        response = canonical_authority_json_text(section.provider_dict()).encode(
            "utf-8"
        )
        rendered = render_scientific_claim_selection(
            response,
            target_section=section.section_id,
            binding=binding,
        )
        parts.extend(
            (
                f"## {section.section_id.title()}\n\n".encode("utf-8"),
                rendered,
                b"\n\n",
            )
        )
    return b"".join(parts)


def _validate_stage17_manifest(
    manifest: Mapping[str, Any],
    *,
    files: Mapping[str, bytes],
    binding: ScientificClaimGenerationBinding,
    expected_capability_snapshot: Mapping[str, int],
) -> None:
    if set(manifest) != _STAGE17_MANIFEST_FIELDS:
        raise StructuredStage19AuthorityError("Stage 17 manifest fields mismatch")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["publication_stage_id"] != "stage17"
        or manifest["claim_policy_id"] != CLAIM_POLICY_ID
        or type(manifest["structured_capability_schema_version"]) is not int
        or manifest["structured_capability_schema_version"] != 1
        or manifest["structured_capability_snapshot"]
        != dict(expected_capability_snapshot)
        or manifest["generation_binding_sha256"]
        != binding.generation_binding_sha256
        or manifest["source_authority_manifest"] is not None
    ):
        raise StructuredStage19AuthorityError("Stage 17 manifest authority mismatch")
    for field, path, digest in (
        (
            "canonical_experiment_evidence",
            binding.canonical_experiment_evidence_path,
            binding.canonical_experiment_evidence_sha256,
        ),
        (
            "experiment_contract",
            binding.experiment_contract_path,
            binding.experiment_contract_sha256,
        ),
        ("run_config", binding.run_config_path, binding.run_config_sha256),
    ):
        _require_ref(manifest[field], path, digest, field)
    if manifest["cfs"] != {
        "schema_version": binding.cfs_schema_version,
        "sha256": binding.cfs_sha256,
    }:
        raise StructuredStage19AuthorityError("Stage 17 manifest CFS mismatch")
    refs = {
        "facts": ("scientific_evidence_facts.json", 5, "record_count"),
        "claim_registry": ("scientific_claim_registry.json", 6, "record_count"),
        "claim_selection": ("scientific_claim_selection.json", 5, "section_count"),
    }
    for field, (name, count, count_field) in refs.items():
        value = manifest[field]
        if (
            not isinstance(value, dict)
            or set(value) != {"file", count_field}
            or type(value[count_field]) is not int
            or value[count_field] != count
        ):
            raise StructuredStage19AuthorityError(
                f"Stage 17 manifest {field} mismatch"
            )
        _require_ref(
            value["file"],
            f"stage-17/{name}",
            hashlib.sha256(files[name]).hexdigest(),
            field,
        )
    for field, name in (
        ("paper_draft", "paper_draft.md"),
        ("paper_structure_report", "paper_structure_report.json"),
        ("experiment_fact_closure_report", "experiment_fact_closure_report.json"),
        ("citation_closure_report", "citation_closure_report.json"),
    ):
        _require_ref(
            manifest[field],
            f"stage-17/{name}",
            hashlib.sha256(files[name]).hexdigest(),
            field,
        )


def _require_ref(value: object, path: str, digest: str, label: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or value["path"] != path
        or value["sha256"] != digest
    ):
        raise StructuredStage19AuthorityError(f"{label} FileRef mismatch")


def _canonical_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredStage19AuthorityError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise StructuredStage19AuthorityError(f"{label} must be an object")
    if canonical_authority_json_text(value).encode("utf-8") != content:
        raise StructuredStage19AuthorityError(f"{label} is not canonical")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StructuredStage19AuthorityError(f"{field} must be an array of strings")
    return tuple(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredStage19AuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
