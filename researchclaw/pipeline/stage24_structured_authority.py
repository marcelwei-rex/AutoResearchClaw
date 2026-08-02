"""Frozen deterministic authority for private structured Stage 24.

This module is deliberately separate from the generic-v1 Stage 24 modules.
It owns only the v2 schemas and deterministic, provider-independent replay
specified by ``SCIENTIFIC_CLAIM_AUTHORITY_DESIGN.md`` section 18.15.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from researchclaw.pipeline.stage24_obligations import ClaimObligation


class StructuredStage24AuthorityError(ValueError):
    """A structured Stage 24 schema or deterministic replay did not close."""


PUBLICATION_MODE = "structured-scientific-claim-v1"
STAGE24_MANIFEST_POLICY_VERSION = "stage24_truth_structured_v2"
STRUCTURED_STAGE24_COARSE_ARTIFACTS = (
    "obligation_inventory.json",
    "claims.json",
    "citations.json",
    "citation_support.json",
    "critique_resolution.json",
    "truth_audit.json",
    "citation-assessments/",
    "generic-support-assessments/",
    "resolution-assessments/",
    "stage24_truth_manifest.json",
)
STRUCTURED_STAGE24_EVIDENCE_REFS = tuple(
    f"stage-24/{name}" for name in STRUCTURED_STAGE24_COARSE_ARTIFACTS
)
DIRECT_OUTPUT_ROLES = (
    "obligation_inventory",
    "claims",
    "citations",
    "citation_support",
    "critique_resolution",
    "truth_audit",
)
ASSESSMENT_ROLES = (
    "citation_assessment",
    "generic_support_assessment",
    "resolution_assessment",
)
KIND_RANK = {
    "numeric_token": 0,
    "citation_instance": 1,
    "comparative_sentence": 2,
    "declarative_sentence": 3,
}
STRUCTURED_STAGE24_MANIFEST_ROOTS = (
    "schema_version",
    "publication_stage_id",
    "publication_mode",
    "structured_capability_schema_version",
    "structured_capability_snapshot",
    "generation_binding_sha256",
    "canonical_experiment_evidence",
    "cfs",
    "selected_result_manifest",
    "source_stage17_manifest",
    "source_stage19_manifest",
    "source_stage20_manifest",
    "source_stage21_manifest",
    "source_stage22_manifest",
    "source_stage23_manifest",
    "source_paper",
    "source_bibliography",
    "source_verification_report",
    "quality_outcome",
    "degradation_signal",
    "claim_scope",
    "stage24_input_bundle_sha256",
    "numeric_support_policy",
    "outcome",
    "direct_output_count",
    "assessment_counts",
    "output_count",
    "outputs",
    "generated",
)
TRANSPORT_RECEIPT_ROOTS = (
    "schema_version",
    "policy_version",
    "assessment_role",
    "client_binding_sha256",
    "semantic_call_ordinal",
    "outbound_attempts",
    "request_sha256",
    "response_sha256",
    "retry_fingerprint",
    "origin_sha256",
    "target_sha256",
    "response_content_sha256",
    "response_content_size",
    "finish_reason",
    "outcome",
)
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_ASSESSMENT_ID_RE = _SHA_RE
_ALLOWED_TRANSFORMS = frozenset(
    {
        "percent-to-ratio-v1",
        "percent-identity-v1",
        "unit-exact-v1",
        "unitless-identity-v1",
    }
)
_UNIT_EXACT = frozenset({"ns", "us", "µs", "ms", "s", "bytes"})
_UNITLESS = frozenset({"ratio", "count", "unitless"})


def global_canonical_json_bytes(value: object) -> bytes:
    """Return the existing authority JSON domain: sorted keys and one LF."""

    try:
        return (
            json.dumps(
                _plain(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StructuredStage24AuthorityError(
            "value is not canonical authority JSON"
        ) from exc


def global_identity_sha256(value: object) -> str:
    return hashlib.sha256(global_canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class AssessmentPlanItem:
    role: str
    identity: Mapping[str, object]
    assessment_id: str
    bound_context: Mapping[str, object]
    obligation_id: str | None
    finding_content_sha256: str | None


@dataclass(frozen=True)
class CitationAssessmentPlanInput:
    canonical_manifest_sha256: str
    paper_sha256: str
    obligation_id: str
    byte_start: int
    byte_end: int
    source_sha256: str
    instance_id: str
    cite_key: str
    stage23_verification_record_sha256: str
    evidence_records: tuple[Mapping[str, object], ...]
    paper: bytes
    obligations: tuple[ClaimObligation, ...]
    evidence_cards: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class GenericAssessmentPlanInput:
    canonical_manifest_sha256: str
    paper_sha256: str
    obligation_id: str
    byte_start: int
    byte_end: int
    source_sha256: str
    paper: bytes
    obligations: tuple[ClaimObligation, ...]
    numeric_support: Mapping[str, Mapping[str, object]]
    citation_records: Mapping[str, Mapping[str, object]]
    citation_record_bytes: Mapping[str, bytes]


@dataclass(frozen=True)
class ResolutionAssessmentPlanInput:
    critique_sha256: str
    raw_paper_sha256: str
    finding: Mapping[str, object]
    paper: bytes


def derive_citation_assessment_plan(
    inputs: CitationAssessmentPlanInput,
    *,
    client_binding_sha256: str,
    critic_model: str,
) -> AssessmentPlanItem:
    target = next(
        row for row in inputs.obligations if row.obligation_id == inputs.obligation_id
    )
    container = containing_sentence(inputs.obligations, target)
    if type(inputs.cite_key) is not str or not inputs.cite_key:
        raise StructuredStage24AuthorityError("citation card cite_key closure mismatch")
    cards: dict[str, Mapping[str, object]] = {}
    for row in inputs.evidence_cards:
        key = row.get("cite_key")
        if type(key) is not str or not key or key in cards:
            raise StructuredStage24AuthorityError(
                "citation card cite_key closure mismatch"
            )
        cards[key] = row
    if inputs.cite_key not in cards:
        raise StructuredStage24AuthorityError("citation card cite_key closure mismatch")
    excerpts = {
        row["excerpt_id"]: row
        for row in cards[inputs.cite_key]["evidence_excerpts"]
        if isinstance(row, Mapping)
    }
    evidence = [_detached_mapping(row) for row in inputs.evidence_records]
    identity = {
        "schema_version": 2,
        "policy_version": "citation_assessment_v2",
        "assessment_role": "citation_assessment",
        "client_binding_sha256": client_binding_sha256,
        "canonical_manifest_sha256": inputs.canonical_manifest_sha256,
        "paper_sha256": inputs.paper_sha256,
        "obligation_id": inputs.obligation_id,
        "byte_start": inputs.byte_start,
        "byte_end": inputs.byte_end,
        "source_sha256": inputs.source_sha256,
        "instance_id": inputs.instance_id,
        "cite_key": inputs.cite_key,
        "stage23_verification_record_sha256": (
            inputs.stage23_verification_record_sha256
        ),
        "evidence_records": evidence,
        "critic_model": critic_model,
    }
    context = {
        "manuscript_context": inputs.paper[
            container.byte_start : container.byte_end
        ].decode("utf-8", errors="strict"),
        "retained_excerpts": [
            {
                "excerpt_id": row["excerpt_id"],
                "excerpt_text": excerpts[row["excerpt_id"]]["excerpt_text"],
            }
            for row in inputs.evidence_records
        ],
    }
    return AssessmentPlanItem(
        "citation_assessment",
        identity,
        global_identity_sha256(identity),
        context,
        inputs.obligation_id,
        None,
    )


def derive_generic_assessment_plan(
    inputs: GenericAssessmentPlanInput,
    *,
    client_binding_sha256: str,
    critic_model: str,
) -> AssessmentPlanItem | None:
    evidence_rows: list[dict[str, object]] = []
    for child in inputs.obligations:
        if child.byte_start < inputs.byte_start or child.byte_end > inputs.byte_end:
            continue
        numeric = inputs.numeric_support.get(child.obligation_id)
        if numeric is not None and numeric["status"] == "supported":
            binding = _detached_mapping(numeric["authority"])  # type: ignore[arg-type]
            evidence_rows.append(
                {
                    "evidence_kind": "numeric_support",
                    "authority": binding,
                    "semantic_pointer": binding["semantic_pointer"],
                    "semantic_value_sha256": binding["semantic_value_sha256"],
                }
            )
        citation = inputs.citation_records.get(child.obligation_id)
        if citation is not None and citation["verdict"] == "supported":
            assessment_id = citation["assessment_id"]
            content = inputs.citation_record_bytes[f"{assessment_id}.json"]
            evidence_rows.append(
                {
                    "evidence_kind": "citation_support",
                    "authority": {
                        "authority_kind": "file",
                        "file": {
                            "path": "stage-24/citation-assessments/"
                            f"{assessment_id}.json",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        },
                    },
                    "semantic_pointer": "/verdict",
                    "semantic_value_sha256": hashlib.sha256(b"supported").hexdigest(),
                }
            )
    unique = {
        global_canonical_json_bytes(row): _detached_mapping(row)
        for row in evidence_rows
    }
    rows = [unique[key] for key in sorted(unique)]
    if not rows:
        return None
    identity_rows = [_detached_mapping(row) for row in rows]
    identity = {
        "schema_version": 3,
        "policy_version": "generic_support_structured_v3",
        "assessment_role": "generic_support_assessment",
        "client_binding_sha256": client_binding_sha256,
        "canonical_manifest_sha256": inputs.canonical_manifest_sha256,
        "paper_sha256": inputs.paper_sha256,
        "obligation_id": inputs.obligation_id,
        "byte_start": inputs.byte_start,
        "byte_end": inputs.byte_end,
        "source_sha256": inputs.source_sha256,
        "evidence_records": identity_rows,
        "critic_model": critic_model,
    }
    context = {
        "manuscript_sentence": inputs.paper[
            inputs.byte_start : inputs.byte_end
        ].decode(
            "utf-8", errors="strict"
        ),
        "evidence_records": [_detached_mapping(row) for row in rows],
    }
    return AssessmentPlanItem(
        "generic_support_assessment",
        identity,
        global_identity_sha256(identity),
        context,
        inputs.obligation_id,
        None,
    )


def derive_resolution_assessment_plan(
    inputs: ResolutionAssessmentPlanInput,
    *,
    client_binding_sha256: str,
    critic_model: str,
) -> AssessmentPlanItem | None:
    if inputs.finding["severity"] not in {"P0", "P1"}:
        return None
    content = {
        key: _plain(inputs.finding[key])
        for key in (
            "id",
            "severity",
            "category",
            "question",
            "finding",
            "falsification_criterion",
        )
    }
    finding_hash = global_identity_sha256(content)
    identity = {
        "schema_version": 2,
        "policy_version": "resolution_assessment_v2",
        "assessment_role": "resolution_assessment",
        "client_binding_sha256": client_binding_sha256,
        "critique_sha256": inputs.critique_sha256,
        "finding_content_sha256": finding_hash,
        "raw_paper_sha256": inputs.raw_paper_sha256,
        "critic_model": critic_model,
    }
    context = {
        "finding": _detached_mapping(content),
        "paper": inputs.paper.decode("utf-8", errors="strict"),
    }
    return AssessmentPlanItem(
        "resolution_assessment",
        identity,
        global_identity_sha256(identity),
        context,
        None,
        finding_hash,
    )


def containing_sentence(
    obligations: Sequence[ClaimObligation], child: ClaimObligation
) -> ClaimObligation:
    candidates = tuple(
        item
        for item in obligations
        if item.kind in {"comparative_sentence", "declarative_sentence"}
        and item.byte_start <= child.byte_start
        and item.byte_end >= child.byte_end
    )
    return child if not candidates else min(
        candidates,
        key=lambda item: (
            item.byte_end - item.byte_start,
            item.byte_start,
            item.byte_end,
            KIND_RANK[item.kind],
            item.obligation_id,
        ),
    )


def strict_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    """Parse strict UTF-8 JSON while rejecting duplicate object keys."""

    try:
        text = content.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StructuredStage24AuthorityError(f"{label} is malformed JSON") from exc
    if type(value) is not dict:
        raise StructuredStage24AuthorityError(f"{label} root is not an object")
    _validate_json_domain(value)
    return value


def decimal_relation_status(*, operator: str, left: str, right: str) -> str:
    """Apply the frozen exact Decimal comparison predicate."""

    if operator not in {"higher", "greater", "lower", "less"}:
        raise StructuredStage24AuthorityError("comparison operator is invalid")
    try:
        lhs = Decimal(left)
        rhs = Decimal(right)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StructuredStage24AuthorityError(
            "comparison canonical Decimal is invalid"
        ) from exc
    if not lhs.is_finite() or not rhs.is_finite():
        raise StructuredStage24AuthorityError(
            "comparison canonical Decimal is nonfinite"
        )
    supported = lhs > rhs if operator in {"higher", "greater"} else lhs < rhs
    return "supported" if supported else "unsupported"


def resolve_cfs_numeric_support(
    *,
    number_lexeme: str,
    unit_lexeme: str | None,
    cfs: Mapping[str, object],
    authority_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Resolve exactly one CFS results-view record by exact Decimal equality."""

    _validate_cfs_ref(cfs)
    if type(number_lexeme) is not str or type(unit_lexeme) not in {str, type(None)}:
        raise StructuredStage24AuthorityError("numeric lexeme is invalid")
    try:
        manuscript_value = Decimal(number_lexeme.replace(",", ""))
    except InvalidOperation as exc:
        raise StructuredStage24AuthorityError("numeric lexeme is invalid") from exc
    if not manuscript_value.is_finite():
        raise StructuredStage24AuthorityError("numeric lexeme is nonfinite")
    matches: list[tuple[Mapping[str, object], str, Decimal]] = []
    for record in authority_records:
        _validate_numeric_authority_source(record)
        canonical = _decimal(record["canonical_value"], "canonical CFS value")
        sealed_unit = record["unit"]
        transform: str | None = None
        candidate: Decimal | None = None
        if unit_lexeme == "%" and sealed_unit == "ratio":
            transform = "percent-to-ratio-v1"
            candidate = manuscript_value / Decimal(100)
        elif unit_lexeme == "%" and sealed_unit == "percent":
            transform = "percent-identity-v1"
            candidate = manuscript_value
        elif unit_lexeme is not None and unit_lexeme.strip(" \t") in _UNIT_EXACT:
            exact_unit = unit_lexeme.strip(" \t")
            if exact_unit == sealed_unit:
                transform = "unit-exact-v1"
                candidate = manuscript_value
        elif unit_lexeme is None and sealed_unit in _UNITLESS:
            transform = "unitless-identity-v1"
            candidate = manuscript_value
        if transform is not None and candidate == canonical:
            matches.append((record, transform, canonical))
    if len(matches) != 1:
        raise StructuredStage24AuthorityError(
            "numeric support did not resolve exactly one CFS authority record"
        )
    record, transform, canonical = matches[0]
    canonical_text = _canonical_decimal_text(canonical)
    pointer = _require_string(record["semantic_pointer"], "semantic pointer")
    value_hash = record.get("semantic_value_sha256")
    if value_hash is None:
        value_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    _sha(value_hash, "semantic value hash")
    return {
        "cfs": {"schema_version": 1, "sha256": cfs["sha256"]},
        "metric": _require_string(record["metric"], "metric"),
        "display_label": _require_string(record["display_label"], "display label"),
        "semantic_pointer": pointer,
        "semantic_value_sha256": value_hash,
        "canonical_value": canonical_text,
        "unit": record["unit"],
        "transform": transform,
    }


def evaluate_comparative_truth(
    *,
    paper: bytes,
    comparison: ClaimObligation,
    numeric_children: Sequence[ClaimObligation],
    numeric_bindings: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Evaluate the code-owned exact comparative byte grammar."""

    if comparison.kind != "comparative_sentence":
        raise StructuredStage24AuthorityError("comparison obligation kind mismatch")
    if len(numeric_children) != 2:
        return {"status": "unsupported", "support_record_id": None}
    children = tuple(
        sorted(
            numeric_children,
            key=lambda item: (item.byte_start, item.byte_end, item.obligation_id),
        )
    )
    if len({child.obligation_id for child in children}) != 2:
        return {"status": "unsupported", "support_record_id": None}
    if any(
        child.kind != "numeric_token"
        or child.kind_payload.get("numeric_role") != "claim_numeric"
        or child.byte_start < comparison.byte_start
        or child.byte_end > comparison.byte_end
        for child in children
    ):
        return {"status": "unsupported", "support_record_id": None}
    if children[0].byte_end > children[1].byte_start:
        return {"status": "unsupported", "support_record_id": None}
    try:
        left_binding = numeric_bindings[children[0].obligation_id]
        right_binding = numeric_bindings[children[1].obligation_id]
        _validate_numeric_binding(left_binding)
        _validate_numeric_binding(right_binding)
        span = paper[comparison.byte_start : comparison.byte_end]
        if hashlib.sha256(span).hexdigest() != comparison.source_sha256:
            raise StructuredStage24AuthorityError("comparison source hash mismatch")
        left_token = paper[children[0].byte_start : children[0].byte_end]
        right_token = paper[children[1].byte_start : children[1].byte_end]
        if (
            hashlib.sha256(left_token).hexdigest() != children[0].source_sha256
            or hashlib.sha256(right_token).hexdigest() != children[1].source_sha256
        ):
            raise StructuredStage24AuthorityError("comparison token hash mismatch")
        left_label = _require_string(
            left_binding["display_label"], "left display label"
        ).encode("utf-8")
        right_label = _require_string(
            right_binding["display_label"], "right display label"
        ).encode("utf-8")
    except (KeyError, UnicodeEncodeError, StructuredStage24AuthorityError):
        return {"status": "unsupported", "support_record_id": None}
    op = _fullmatch_comparative_grammar(
        span,
        left_label=left_label,
        left_token=left_token,
        right_label=right_label,
        right_token=right_token,
    )
    terms = comparison.kind_payload.get("matched_terms")
    try:
        exact_term = (
            terms[0].encode("ascii").lower()
            if type(terms) in {tuple, list}
            and len(terms) == 1
            and type(terms[0]) is str
            else None
        )
    except UnicodeEncodeError:
        exact_term = None
    if op is None or exact_term != op:
        return {"status": "unsupported", "support_record_id": None}
    try:
        status = decimal_relation_status(
            operator=op.decode("ascii"),
            left=_require_string(left_binding["canonical_value"], "left value"),
            right=_require_string(right_binding["canonical_value"], "right value"),
        )
    except StructuredStage24AuthorityError:
        status = "unsupported"
    return {"status": status, "support_record_id": None}


def citation_occurrence_count(inventory: Sequence[ClaimObligation]) -> int:
    count = sum(row.kind == "citation_instance" for row in inventory)
    if count > 128:
        raise StructuredStage24AuthorityError("citation occurrence count exceeds 128")
    return count


def derive_generic_candidate_universe(
    inventory: Sequence[ClaimObligation],
    *,
    supported_numeric_obligation_ids: frozenset[str],
    structurally_closed_citation_obligation_ids: frozenset[str],
) -> tuple[ClaimObligation, ...]:
    """Build U without text, span, or evidence deduplication."""

    eligible = tuple(
        row
        for row in inventory
        if (
            row.kind == "numeric_token"
            and row.kind_payload.get("numeric_role") == "claim_numeric"
            and row.obligation_id in supported_numeric_obligation_ids
        )
        or (
            row.kind == "citation_instance"
            and row.obligation_id
            in structurally_closed_citation_obligation_ids
        )
    )
    universe = tuple(
        sentence
        for sentence in inventory
        if sentence.kind == "declarative_sentence"
        and any(
            child.byte_start >= sentence.byte_start
            and child.byte_end <= sentence.byte_end
            for child in eligible
        )
    )
    if len(universe) > 128:
        raise StructuredStage24AuthorityError(
            "generic candidate universe exceeds 128"
        )
    return universe


def validate_assessment_counts(
    *, citation: int, generic: int, resolution: int
) -> int:
    for value, maximum, label in (
        (citation, 128, "citation"),
        (generic, 128, "generic"),
        (resolution, 12, "resolution"),
    ):
        if type(value) is not int or value < 0 or value > maximum:
            raise StructuredStage24AuthorityError(
                f"{label} assessment count is invalid"
            )
    total = citation + generic + resolution
    if total > 268:
        raise StructuredStage24AuthorityError(
            "Stage 24 semantic call count exceeds 268"
        )
    return total


def example_manifest_v2() -> dict[str, object]:
    """Return a count-consistent schema example with no assessment rows."""

    ref = {"path": "placeholder", "sha256": "a" * 64, "size": 1}
    outputs = [
        {
            "role": role,
            "logical_name": None,
            "path": f"stage-24/{name}",
            "sha256": "a" * 64,
            "size": 1,
        }
        for role, name in zip(
            DIRECT_OUTPUT_ROLES, STRUCTURED_STAGE24_COARSE_ARTIFACTS[:6], strict=True
        )
    ]
    return {
        "schema_version": 2,
        "publication_stage_id": "stage24",
        "publication_mode": PUBLICATION_MODE,
        "structured_capability_schema_version": 1,
        "structured_capability_snapshot": {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 0,
        },
        "generation_binding_sha256": "a" * 64,
        "canonical_experiment_evidence": dict(ref),
        "cfs": {"schema_version": 1, "sha256": "a" * 64},
        "selected_result_manifest": dict(ref),
        "source_stage17_manifest": dict(ref),
        "source_stage19_manifest": dict(ref),
        "source_stage20_manifest": dict(ref),
        "source_stage21_manifest": dict(ref),
        "source_stage22_manifest": dict(ref),
        "source_stage23_manifest": dict(ref),
        "source_paper": dict(ref),
        "source_bibliography": dict(ref),
        "source_verification_report": dict(ref),
        "quality_outcome": "passed",
        "degradation_signal": None,
        "claim_scope": "research_release",
        "stage24_input_bundle_sha256": "a" * 64,
        "numeric_support_policy": {
            "policy_version": "cfs-results-exact-decimal-v1",
            "cfs_view": "results",
            "equality": "exact-decimal",
            "tolerance": False,
            "raw_execution_fallback": False,
        },
        "outcome": "passed",
        "direct_output_count": 6,
        "assessment_counts": {
            "citation_assessment": 0,
            "generic_support_assessment": 0,
            "resolution_assessment": 0,
        },
        "output_count": 6,
        "outputs": outputs,
        "generated": "2026-07-28T00:00:00+00:00",
    }


def validate_stage24_manifest_v2(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    """Strictly replay root schema, counts, output order, and 1110 binding."""

    if type(value) is not dict or set(value) != set(
        STRUCTURED_STAGE24_MANIFEST_ROOTS
    ):
        raise StructuredStage24AuthorityError("Stage 24 manifest roots mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise StructuredStage24AuthorityError("Stage 24 manifest schema mismatch")
    if value["publication_stage_id"] != "stage24" or value[
        "publication_mode"
    ] != PUBLICATION_MODE:
        raise StructuredStage24AuthorityError("Stage 24 publication identity mismatch")
    if (
        type(value["structured_capability_schema_version"]) is not int
        or value["structured_capability_schema_version"] != 1
        or value["structured_capability_snapshot"]
        != {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 0,
        }
    ):
        raise StructuredStage24AuthorityError("Stage 24 capability is not exact 1110")
    _sha(value["generation_binding_sha256"], "generation binding")
    for field in (
        "canonical_experiment_evidence",
        "selected_result_manifest",
        "source_stage17_manifest",
        "source_stage19_manifest",
        "source_stage20_manifest",
        "source_stage21_manifest",
        "source_stage22_manifest",
        "source_stage23_manifest",
        "source_paper",
        "source_bibliography",
        "source_verification_report",
    ):
        _validate_file_ref(value[field], field)
    _validate_cfs_ref(value["cfs"])
    if value["quality_outcome"] not in {"passed", "degraded"}:
        raise StructuredStage24AuthorityError("quality outcome is invalid")
    if value["degradation_signal"] is not None:
        _validate_file_ref(value["degradation_signal"], "degradation signal")
    if value["claim_scope"] not in {
        "research_release",
        "pipeline_validation",
        "exploratory",
    }:
        raise StructuredStage24AuthorityError("claim scope is invalid")
    _sha(value["stage24_input_bundle_sha256"], "input bundle identity")
    if value["numeric_support_policy"] != {
        "policy_version": "cfs-results-exact-decimal-v1",
        "cfs_view": "results",
        "equality": "exact-decimal",
        "tolerance": False,
        "raw_execution_fallback": False,
    }:
        raise StructuredStage24AuthorityError("numeric support policy mismatch")
    if value["outcome"] not in {"passed", "degraded"}:
        raise StructuredStage24AuthorityError("Stage 24 outcome is invalid")
    if (
        value["outcome"] != value["quality_outcome"]
        or (
            value["quality_outcome"] == "passed"
            and value["degradation_signal"] is not None
        )
        or (
            value["quality_outcome"] == "degraded"
            and value["degradation_signal"] is None
        )
    ):
        raise StructuredStage24AuthorityError(
            "quality, degradation signal, and outcome mismatch"
        )
    if type(value["direct_output_count"]) is not int or value[
        "direct_output_count"
    ] != 6:
        raise StructuredStage24AuthorityError("direct output count mismatch")
    counts = value["assessment_counts"]
    if type(counts) is not dict or set(counts) != set(ASSESSMENT_ROLES):
        raise StructuredStage24AuthorityError("assessment counts roots mismatch")
    total = validate_assessment_counts(
        citation=counts["citation_assessment"],
        generic=counts["generic_support_assessment"],
        resolution=counts["resolution_assessment"],
    )
    if (
        type(value["output_count"]) is not int
        or value["output_count"] != 6 + total
    ):
        raise StructuredStage24AuthorityError("output count mismatch")
    outputs = value["outputs"]
    if type(outputs) is not list or len(outputs) != value["output_count"]:
        raise StructuredStage24AuthorityError("outputs length mismatch")
    _validate_output_rows(outputs, counts)
    _require_string(value["generated"], "generated")
    return value


def validate_transport_receipt(
    value: Mapping[str, object],
    *,
    expected_role: str,
    expected_client_binding_sha256: str,
) -> None:
    if type(value) is not dict or set(value) != set(TRANSPORT_RECEIPT_ROOTS):
        raise StructuredStage24AuthorityError("transport receipt roots mismatch")
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
        raise StructuredStage24AuthorityError("transport receipt schema mismatch")
    if value["policy_version"] != "stage24_assessment_transport_v1":
        raise StructuredStage24AuthorityError("transport receipt policy mismatch")
    if (
        value["assessment_role"] != expected_role
        or value["client_binding_sha256"] != expected_client_binding_sha256
    ):
        raise StructuredStage24AuthorityError("transport receipt binding mismatch")
    for field in (
        "request_sha256",
        "response_sha256",
        "origin_sha256",
        "target_sha256",
        "response_content_sha256",
    ):
        _sha(value[field], field)
    ordinal = value["semantic_call_ordinal"]
    outbound = value["outbound_attempts"]
    size = value["response_content_size"]
    if (
        type(ordinal) is not int
        or not 1 <= ordinal <= 268
        or type(outbound) is not int
        or outbound not in {1, 2}
        or type(size) is not int
        or size < 0
    ):
        raise StructuredStage24AuthorityError("transport receipt integer invalid")
    expected_retry = value["request_sha256"] if outbound == 2 else None
    if value["retry_fingerprint"] != expected_retry:
        raise StructuredStage24AuthorityError("retry fingerprint mismatch")
    if value["finish_reason"] != "stop" or value["outcome"] != "complete":
        raise StructuredStage24AuthorityError("transport receipt outcome mismatch")


def _fullmatch_comparative_grammar(
    span: bytes,
    *,
    left_label: bytes,
    left_token: bytes,
    right_label: bytes,
    right_token: bytes,
) -> bytes | None:
    ows = rb"[ \t]*"
    rws = rb"[ \t]+"
    cop = rb"(?:is|was)"
    op = rb"(higher|greater|lower|less)"
    prefix = rb"(?:(?:is|was)[ \t]+|[:=][ \t]*)"
    pattern = (
        rb"\A"
        + ows
        + re.escape(left_label)
        + ows
        + rb"(?:"
        + prefix
        + rb")?"
        + re.escape(left_token)
        + rws
        + cop
        + rws
        + op
        + rws
        + rb"than"
        + rws
        + re.escape(right_label)
        + ows
        + rb"(?:"
        + prefix
        + rb")?"
        + re.escape(right_token)
        + ows
        + rb"[.!?]?"
        + ows
        + rb"\Z"
    )
    match = re.fullmatch(pattern, span, flags=re.IGNORECASE | re.ASCII)
    return None if match is None else match.group(1).lower()


def _validate_numeric_authority_source(value: Mapping[str, object]) -> None:
    if type(value) is not dict:
        raise StructuredStage24AuthorityError("numeric authority row is invalid")
    required = {
        "metric",
        "display_label",
        "semantic_pointer",
        "canonical_value",
        "unit",
    }
    if not required.issubset(value):
        raise StructuredStage24AuthorityError("numeric authority row is incomplete")
    _require_string(value["metric"], "metric")
    _require_string(value["display_label"], "display label")
    _require_string(value["semantic_pointer"], "semantic pointer")
    _require_string(value["canonical_value"], "canonical value")
    if value["unit"] not in _UNIT_EXACT | _UNITLESS | {"percent"}:
        raise StructuredStage24AuthorityError("numeric authority unit is invalid")
    if "semantic_value_sha256" in value:
        _sha(value["semantic_value_sha256"], "semantic value hash")


def _validate_numeric_binding(value: Mapping[str, object]) -> None:
    roots = (
        "cfs",
        "metric",
        "display_label",
        "semantic_pointer",
        "semantic_value_sha256",
        "canonical_value",
        "unit",
        "transform",
    )
    if type(value) is not dict or tuple(value) != roots:
        raise StructuredStage24AuthorityError("numeric binding roots mismatch")
    _validate_cfs_ref(value["cfs"])
    for field in (
        "metric",
        "display_label",
        "semantic_pointer",
        "canonical_value",
    ):
        _require_string(value[field], field)
    _sha(value["semantic_value_sha256"], "semantic value hash")
    if value["transform"] not in _ALLOWED_TRANSFORMS:
        raise StructuredStage24AuthorityError("numeric transform is invalid")
    _decimal(value["canonical_value"], "canonical numeric binding")


def _validate_output_rows(
    rows: list[object], counts: Mapping[str, int]
) -> None:
    fixed_names = STRUCTURED_STAGE24_COARSE_ARTIFACTS[:6]
    for index, (role, name) in enumerate(
        zip(DIRECT_OUTPUT_ROLES, fixed_names, strict=True)
    ):
        expected = {
            "role": role,
            "logical_name": None,
            "path": f"stage-24/{name}",
        }
        _validate_output_row(rows[index], expected=expected)
    offset = 6
    directories = {
        "citation_assessment": "citation-assessments",
        "generic_support_assessment": "generic-support-assessments",
        "resolution_assessment": "resolution-assessments",
    }
    for role in ASSESSMENT_ROLES:
        group = rows[offset : offset + counts[role]]
        logical_names: list[str] = []
        for row in group:
            if type(row) is not dict:
                raise StructuredStage24AuthorityError("output row is invalid")
            logical = row.get("logical_name")
            _sha(logical, "assessment logical name")
            _validate_output_row(
                row,
                expected={
                    "role": role,
                    "logical_name": logical,
                    "path": f"stage-24/{directories[role]}/{logical}.json",
                },
            )
            logical_names.append(logical)
        if logical_names != sorted(logical_names) or len(set(logical_names)) != len(
            logical_names
        ):
            raise StructuredStage24AuthorityError(
                "assessment outputs are not unique bytewise order"
            )
        offset += counts[role]


def _validate_output_row(
    value: object, *, expected: Mapping[str, object]
) -> None:
    if type(value) is not dict or set(value) != {
        "role",
        "logical_name",
        "path",
        "sha256",
        "size",
    }:
        raise StructuredStage24AuthorityError("output row roots mismatch")
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise StructuredStage24AuthorityError(f"output row {field} mismatch")
    _sha(value["sha256"], "output digest")
    if type(value["size"]) is not int or value["size"] < 0:
        raise StructuredStage24AuthorityError("output size is invalid")


def _validate_file_ref(value: object, label: str) -> None:
    if type(value) is not dict or tuple(value) != ("path", "sha256", "size"):
        raise StructuredStage24AuthorityError(f"{label} FileRef roots mismatch")
    path = _require_string(value["path"], f"{label} path")
    if (
        path.startswith("/")
        or "\\" in path
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise StructuredStage24AuthorityError(f"{label} path is unsafe")
    _sha(value["sha256"], f"{label} digest")
    if type(value["size"]) is not int or value["size"] < 0:
        raise StructuredStage24AuthorityError(f"{label} size is invalid")


def _validate_cfs_ref(value: object) -> None:
    if type(value) is not dict or tuple(value) != ("schema_version", "sha256"):
        raise StructuredStage24AuthorityError("CFS ref roots mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise StructuredStage24AuthorityError("CFS schema version mismatch")
    _sha(value["sha256"], "CFS digest")


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise StructuredStage24AuthorityError("Decimal value is nonfinite")
    if value == 0:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is not str:
        raise StructuredStage24AuthorityError(f"{label} is not canonical text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise StructuredStage24AuthorityError(f"{label} is invalid") from exc
    if not parsed.is_finite() or _canonical_decimal_text(parsed) != value:
        raise StructuredStage24AuthorityError(f"{label} is not canonical")
    return parsed


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA_RE.fullmatch(value) is None:
        raise StructuredStage24AuthorityError(f"{label} is not lowercase SHA-256")
    return value


def _require_string(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise StructuredStage24AuthorityError(f"{label} is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StructuredStage24AuthorityError(f"{label} is not scalar UTF-8") from exc
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _validate_json_domain(value: object) -> None:
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise StructuredStage24AuthorityError(
                    "JSON string is not scalar UTF-8"
                ) from exc
        return
    if type(value) is list:
        for item in value:
            _validate_json_domain(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise StructuredStage24AuthorityError("JSON key is invalid")
            _validate_json_domain(item)
        return
    raise StructuredStage24AuthorityError("unsupported JSON value")


def _plain(value: object) -> object:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _detached_mapping(value: Mapping[str, object]) -> dict[str, object]:
    detached = _plain(value)
    if type(detached) is not dict:
        raise StructuredStage24AuthorityError("plan input is not a JSON object")
    return detached
