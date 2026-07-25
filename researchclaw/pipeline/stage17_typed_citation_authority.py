"""Pure typed citation authority projected from one captured release generation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    parse_contract_bytes,
    validate_contract_structure_dict,
)
from researchclaw.literature.citation_plan import (
    CITATION_PLAN_DOMAIN_VERSION,
    CitationPlanContractError,
    CitationPlanReplayInputs,
    ReplayedCitationAuthority,
    _replay_citation_plan_provenance_from_evidence,
    parse_citation_plan,
)
from researchclaw.literature.citation_policy import (
    CitationPolicyContractError,
    replay_active_config_snapshot,
)
from researchclaw.literature.evidence_cards import (
    EvidenceCardContractError,
    canonical_json_text,
    parse_cards_manifest,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
)


EVIDENCE_ANCHOR_POLICY_VERSION = "stage17-evidence-anchor-v1"
MANUSCRIPT_CLAIM_POLICY_VERSION = "stage17-manuscript-claim-v1"
VERBATIM_VALIDATION_POLICY_VERSION = "stage17-verbatim-validation-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_ANCHOR_FIELDS = {
    "schema_version",
    "policy_version",
    "citation_plan_path",
    "citation_plan_sha256",
    "claim_id",
    "section_path",
    "claim_type",
    "cite_key",
    "evidence_card_path",
    "evidence_card_sha256",
    "evidence_excerpt_id",
    "evidence_excerpt_sha256",
    "support_status",
    "claim_scope",
    "canonical_experiment_evidence_path",
    "canonical_experiment_evidence_sha256",
    "config_source_path",
    "config_source_sha256",
}
_MANUSCRIPT_CLAIM_FIELDS = {
    "schema_version",
    "policy_version",
    "evidence_anchor_id",
    "claim_scope",
    "provenance",
    "claim_text_sha256",
    "validation_policy_version",
    "validation_report_sha256",
}
_VERBATIM_VALIDATION_FIELDS = {
    "schema_version",
    "policy_version",
    "evidence_anchor_id",
    "claim_scope",
    "provenance",
    "claim_text_sha256",
    "evidence_excerpt_sha256",
    "exact_byte_match",
    "valid",
}


class CitationTypedAuthorityError(ValueError):
    """Raised when typed Stage 17 citation authority cannot be reconstructed."""


@dataclass(frozen=True)
class EvidenceAnchor:
    evidence_anchor_id: str
    identity_bytes: bytes
    excerpt_bytes: bytes


@dataclass(frozen=True)
class ManuscriptClaim:
    manuscript_claim_id: str
    identity_bytes: bytes
    evidence_anchor_id: str
    claim_id: str
    heading: str
    cite_key: str
    claim_scope: str
    provenance: str
    claim_text_bytes: bytes
    validation_report_bytes: bytes


@dataclass(frozen=True)
class TypedCitationAuthority:
    claim_scope: str
    evidence_anchors: tuple[EvidenceAnchor, ...]
    manuscript_claims: tuple[ManuscriptClaim, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CitationTypedAuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CitationTypedAuthorityError(f"non-finite JSON value: {value}")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CitationTypedAuthorityError(
            f"typed citation identity is not canonical JSON: {exc}"
        ) from exc


def _parse_canonical_object(
    content: bytes, *, label: str, fields: set[str]
) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CitationTypedAuthorityError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CitationTypedAuthorityError(f"{label} root must be an object")
    if set(payload) != fields:
        raise CitationTypedAuthorityError(
            f"{label} fields mismatch: "
            f"missing={sorted(fields - set(payload))}, "
            f"extra={sorted(set(payload) - fields)}"
        )
    if _canonical_json_bytes(payload) != content:
        raise CitationTypedAuthorityError(f"{label} bytes are not canonical")
    return payload


def _required_true_version(payload: Mapping[str, Any], value: int = 1) -> None:
    if type(payload.get("schema_version")) is not int:
        raise CitationTypedAuthorityError("schema_version must be a true integer")
    if payload["schema_version"] != value:
        raise CitationTypedAuthorityError("unsupported schema_version")


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
    ):
        raise CitationTypedAuthorityError(f"{field} must be a canonical string")
    return value


def _required_sha256(payload: Mapping[str, Any], field: str) -> str:
    value = _required_string(payload, field)
    if not _SHA256_RE.fullmatch(value):
        raise CitationTypedAuthorityError(f"{field} must be lowercase SHA-256")
    return value


def _safe_run_relative_path(value: object, field: str) -> str:
    if type(value) is not str:
        raise CitationTypedAuthorityError(f"{field} must be a string")
    if (
        not value
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or "\\" in value
        or "%" in value
        or "//" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise CitationTypedAuthorityError(f"{field} is not a safe canonical path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CitationTypedAuthorityError(f"{field} is not a safe canonical path")
    return value


def parse_evidence_anchor_identity(content: bytes) -> dict[str, Any]:
    payload = _parse_canonical_object(
        content,
        label="evidence anchor identity",
        fields=_EVIDENCE_ANCHOR_FIELDS,
    )
    _required_true_version(payload)
    if payload["policy_version"] != EVIDENCE_ANCHOR_POLICY_VERSION:
        raise CitationTypedAuthorityError("unsupported evidence anchor policy")
    for field in (
        "citation_plan_path",
        "evidence_card_path",
        "canonical_experiment_evidence_path",
        "config_source_path",
    ):
        _safe_run_relative_path(payload[field], field)
    for field in (
        "citation_plan_sha256",
        "evidence_card_sha256",
        "evidence_excerpt_sha256",
        "canonical_experiment_evidence_sha256",
        "config_source_sha256",
    ):
        _required_sha256(payload, field)
    for field in (
        "claim_id",
        "claim_type",
        "cite_key",
        "evidence_excerpt_id",
        "support_status",
    ):
        _required_string(payload, field)
    if payload["citation_plan_path"] != "stage-16/citation_plan.json":
        raise CitationTypedAuthorityError("noncanonical citation_plan_path")
    if not payload["evidence_card_path"].startswith("stage-06/cards/"):
        raise CitationTypedAuthorityError("noncanonical evidence_card_path")
    if payload["support_status"] != "abstract_sufficient":
        raise CitationTypedAuthorityError("unsupported evidence support_status")
    section_path = payload["section_path"]
    if (
        not isinstance(section_path, list)
        or not section_path
        or any(
            type(item) is not str
            or not item
            or item != item.strip()
            or unicodedata.normalize("NFC", item) != item
            for item in section_path
        )
    ):
        raise CitationTypedAuthorityError("section_path must be a nonempty string array")
    if payload["claim_scope"] not in {
        "pipeline_validation",
        "exploratory",
        "research_release",
    }:
        raise CitationTypedAuthorityError("invalid claim_scope")
    return payload


def parse_manuscript_claim_identity(content: bytes) -> dict[str, Any]:
    payload = _parse_canonical_object(
        content,
        label="manuscript claim identity",
        fields=_MANUSCRIPT_CLAIM_FIELDS,
    )
    _required_true_version(payload)
    if payload["policy_version"] != MANUSCRIPT_CLAIM_POLICY_VERSION:
        raise CitationTypedAuthorityError("unsupported manuscript claim policy")
    _required_sha256(payload, "evidence_anchor_id")
    _required_sha256(payload, "claim_text_sha256")
    _required_sha256(payload, "validation_report_sha256")
    if payload["claim_scope"] not in {
        "pipeline_validation",
        "exploratory",
        "research_release",
    }:
        raise CitationTypedAuthorityError("invalid claim_scope")
    if payload["provenance"] not in {"verbatim", "paraphrased"}:
        raise CitationTypedAuthorityError("invalid manuscript claim provenance")
    if payload["validation_policy_version"] != VERBATIM_VALIDATION_POLICY_VERSION:
        raise CitationTypedAuthorityError("unsupported validation policy")
    if payload["provenance"] != "verbatim":
        raise CitationTypedAuthorityError(
            "paraphrased manuscript claims are not supported before C3"
        )
    return payload


def _parse_verbatim_validation_report(content: bytes) -> dict[str, Any]:
    payload = _parse_canonical_object(
        content,
        label="verbatim validation report",
        fields=_VERBATIM_VALIDATION_FIELDS,
    )
    _required_true_version(payload)
    if payload["policy_version"] != VERBATIM_VALIDATION_POLICY_VERSION:
        raise CitationTypedAuthorityError("unsupported verbatim validation policy")
    _required_sha256(payload, "evidence_anchor_id")
    _required_sha256(payload, "claim_text_sha256")
    _required_sha256(payload, "evidence_excerpt_sha256")
    if payload["claim_scope"] not in {"pipeline_validation", "exploratory"}:
        raise CitationTypedAuthorityError("verbatim validation has forbidden claim_scope")
    if payload["provenance"] != "verbatim":
        raise CitationTypedAuthorityError("verbatim validation provenance mismatch")
    if payload["exact_byte_match"] is not True or payload["valid"] is not True:
        raise CitationTypedAuthorityError("verbatim validation is not valid")
    return payload


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _identity_id(content: bytes) -> str:
    return _sha256(content)


def _card_entry_by_key(
    manifest: Mapping[str, Any], cite_key: str
) -> Mapping[str, Any]:
    entries = [
        entry for entry in manifest["cards"] if entry["cite_key"] == cite_key
    ]
    if len(entries) != 1:
        raise CitationTypedAuthorityError(
            "cite key does not bind exactly one evidence card"
        )
    return entries[0]


def project_typed_citation_authority(
    *,
    inputs: CitationPlanReplayInputs,
    citation_authority: ReplayedCitationAuthority,
    evidence: CanonicalExperimentEvidence,
    project_root: Path,
) -> TypedCitationAuthority:
    """Derive C2 authority only from captured Stage 4-16 and evidence bytes."""

    try:
        snapshot_config, config_path, config_sha256 = replay_active_config_snapshot(
            inputs.active_config,
            None,
            project_root=project_root,
        )
    except (
        CitationPolicyContractError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise CitationTypedAuthorityError(
            f"cannot replay typed citation inputs: {exc}"
        ) from exc

    try:
        plan = parse_citation_plan(inputs.citation_plan_text)
    except CitationPlanContractError as exc:
        raise CitationTypedAuthorityError(
            f"cannot replay typed citation plan: {exc}"
        ) from exc
    if plan != citation_authority.plan:
        raise CitationTypedAuthorityError(
            "caller citation authority differs from captured citation plan"
        )
    if type(plan.get("plan_version")) is not int or (
        plan["plan_version"] != CITATION_PLAN_DOMAIN_VERSION
    ):
        raise CitationTypedAuthorityError(
            "typed citation authority requires domain citation plan v3"
        )
    if inputs.citation_plan_text.encode("utf-8") != _canonical_plan_bytes(plan):
        raise CitationTypedAuthorityError("captured citation plan bytes mismatch")
    claim_scope = snapshot_config.experiment.claim_scope
    if plan["claim_scope"] != claim_scope:
        raise CitationTypedAuthorityError("typed citation claim_scope mismatch")
    config_bytes = inputs.active_config.config_source_text.encode("utf-8")
    if (
        evidence.run_config_path != config_path
        or evidence.run_config_sha256 != config_sha256
        or evidence.run_config_bytes != config_bytes
    ):
        raise CitationTypedAuthorityError(
            "canonical experiment config generation mismatch"
        )
    if claim_scope == "research_release":
        raise CitationTypedAuthorityError(
            "research_release requires validated paraphrase"
        )
    try:
        expected_authority = _replay_citation_plan_provenance_from_evidence(
            inputs,
            None,
            project_root=project_root,
            evidence=evidence,
        )
    except (CitationPlanContractError, ValueError) as exc:
        raise CitationTypedAuthorityError(
            f"cannot replay typed citation provenance: {exc}"
        ) from exc
    if expected_authority != citation_authority:
        raise CitationTypedAuthorityError(
            "caller citation authority differs from independently replayed authority"
        )
    citation_authority = expected_authority
    plan = citation_authority.plan
    try:
        contract = validate_contract_structure_dict(
            parse_contract_bytes(evidence.experiment_contract_bytes)
        )
        manifest = parse_cards_manifest(inputs.cards_manifest_text)
    except (
        ContractValidationError,
        EvidenceCardContractError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise CitationTypedAuthorityError(
            f"cannot replay typed citation inputs: {exc}"
        ) from exc
    if contract.claim_scope != claim_scope:
        raise CitationTypedAuthorityError("typed citation claim_scope mismatch")
    if (
        evidence.experiment_contract_sha256
        != _sha256(evidence.experiment_contract_bytes)
    ):
        raise CitationTypedAuthorityError("experiment contract hash mismatch")
    if plan["cards_manifest_sha256"] != _sha256(
        inputs.cards_manifest_text.encode("utf-8")
    ):
        raise CitationTypedAuthorityError("citation plan cards manifest mismatch")
    _safe_run_relative_path(evidence.manifest_path, "canonical evidence path")
    if not _SHA256_RE.fullmatch(evidence.manifest_sha256):
        raise CitationTypedAuthorityError("canonical evidence hash is invalid")

    cards_by_key = {
        str(card["cite_key"]): card for card in citation_authority.cards
    }
    if len(cards_by_key) != len(citation_authority.cards):
        raise CitationTypedAuthorityError("duplicate replayed evidence card key")

    evidence_anchors: list[EvidenceAnchor] = []
    manuscript_claims: list[ManuscriptClaim] = []
    seen_anchor_ids: set[str] = set()
    seen_claim_ids: set[str] = set()
    citation_plan_bytes = inputs.citation_plan_text.encode("utf-8")
    for claim in plan["claims"]:
        citation = claim["planned_citations"][0]
        excerpt_ids = citation["evidence_excerpt_ids"]
        if len(excerpt_ids) != 1:
            raise CitationTypedAuthorityError(
                "domain citation claim must bind one evidence excerpt"
            )
        cite_key = citation["cite_key"]
        card = cards_by_key.get(cite_key)
        if card is None:
            raise CitationTypedAuthorityError("planned cite key has no replayed card")
        entry = _card_entry_by_key(manifest, cite_key)
        if entry["card_id"] != card.get("card_id"):
            raise CitationTypedAuthorityError("evidence card identity mismatch")
        card_path = _safe_run_relative_path(
            entry["json_path"], "evidence_card_path"
        )
        card_text = inputs.card_texts.get(card_path)
        if card_text is None:
            raise CitationTypedAuthorityError("captured evidence card bytes are missing")
        card_bytes = card_text.encode("utf-8")
        if entry["json_sha256"] != _sha256(card_bytes):
            raise CitationTypedAuthorityError("captured evidence card hash mismatch")
        if card_text != canonical_json_text(card):
            raise CitationTypedAuthorityError(
                "replayed evidence card differs from captured card bytes"
            )
        excerpts = [
            excerpt
            for excerpt in card["evidence_excerpts"]
            if excerpt["excerpt_id"] == excerpt_ids[0]
        ]
        if len(excerpts) != 1:
            raise CitationTypedAuthorityError(
                "planned excerpt does not bind exactly one captured excerpt"
            )
        excerpt = excerpts[0]
        excerpt_bytes = excerpt["excerpt_text"].encode("utf-8")
        if excerpt["excerpt_sha256"] != _sha256(excerpt_bytes):
            raise CitationTypedAuthorityError("evidence excerpt hash mismatch")

        anchor_payload = {
            "schema_version": 1,
            "policy_version": EVIDENCE_ANCHOR_POLICY_VERSION,
            "citation_plan_path": "stage-16/citation_plan.json",
            "citation_plan_sha256": _sha256(citation_plan_bytes),
            "claim_id": claim["claim_id"],
            "section_path": list(claim["section_path"]),
            "claim_type": claim["claim_type"],
            "cite_key": cite_key,
            "evidence_card_path": card_path,
            "evidence_card_sha256": entry["json_sha256"],
            "evidence_excerpt_id": excerpt["excerpt_id"],
            "evidence_excerpt_sha256": excerpt["excerpt_sha256"],
            "support_status": citation["support_status"],
            "claim_scope": claim_scope,
            "canonical_experiment_evidence_path": evidence.manifest_path,
            "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
            "config_source_path": config_path,
            "config_source_sha256": config_sha256,
        }
        anchor_bytes = _canonical_json_bytes(anchor_payload)
        parse_evidence_anchor_identity(anchor_bytes)
        anchor_id = _identity_id(anchor_bytes)
        if anchor_id in seen_anchor_ids:
            raise CitationTypedAuthorityError("duplicate evidence anchor identity")
        seen_anchor_ids.add(anchor_id)
        evidence_anchors.append(
            EvidenceAnchor(
                evidence_anchor_id=anchor_id,
                identity_bytes=anchor_bytes,
                excerpt_bytes=excerpt_bytes,
            )
        )

        claim_text_bytes = claim["claim_text"].encode("utf-8")
        validation_payload = {
            "schema_version": 1,
            "policy_version": VERBATIM_VALIDATION_POLICY_VERSION,
            "evidence_anchor_id": anchor_id,
            "claim_scope": claim_scope,
            "provenance": "verbatim",
            "claim_text_sha256": _sha256(claim_text_bytes),
            "evidence_excerpt_sha256": _sha256(excerpt_bytes),
            "exact_byte_match": claim_text_bytes == excerpt_bytes,
            "valid": claim_text_bytes == excerpt_bytes,
        }
        validation_bytes = _canonical_json_bytes(validation_payload)
        _parse_verbatim_validation_report(validation_bytes)
        claim_payload = {
            "schema_version": 1,
            "policy_version": MANUSCRIPT_CLAIM_POLICY_VERSION,
            "evidence_anchor_id": anchor_id,
            "claim_scope": claim_scope,
            "provenance": "verbatim",
            "claim_text_sha256": _sha256(claim_text_bytes),
            "validation_policy_version": VERBATIM_VALIDATION_POLICY_VERSION,
            "validation_report_sha256": _sha256(validation_bytes),
        }
        claim_identity_bytes = _canonical_json_bytes(claim_payload)
        parse_manuscript_claim_identity(claim_identity_bytes)
        manuscript_claim_id = _identity_id(claim_identity_bytes)
        if manuscript_claim_id in seen_claim_ids:
            raise CitationTypedAuthorityError("duplicate manuscript claim identity")
        seen_claim_ids.add(manuscript_claim_id)
        manuscript_claims.append(
            ManuscriptClaim(
                manuscript_claim_id=manuscript_claim_id,
                identity_bytes=claim_identity_bytes,
                evidence_anchor_id=anchor_id,
                claim_id=claim["claim_id"],
                heading=claim["section_path"][0],
                cite_key=cite_key,
                claim_scope=claim_scope,
                provenance="verbatim",
                claim_text_bytes=claim_text_bytes,
                validation_report_bytes=validation_bytes,
            )
        )

    return TypedCitationAuthority(
        claim_scope=claim_scope,
        evidence_anchors=tuple(evidence_anchors),
        manuscript_claims=tuple(manuscript_claims),
    )


def _canonical_plan_bytes(plan: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
