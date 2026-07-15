"""Canonical Stage 24 truth publication and disk replay."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import validate_contract_dict
from researchclaw.literature.experiment_fact_closure import (
    find_dataset_claim_violations,
)
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    parse_invocation_result,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage24_assessments import (
    CitationAssessmentInput,
    CitationEvidenceRecord,
    GenericEvidenceRecord,
    GenericSupportInput,
    ResolutionAssessmentInput,
    Stage24AssessmentError,
    build_citation_assessment_input,
    build_generic_support_input,
    build_resolution_assessment_input,
    parse_citation_assessment_record,
    parse_generic_support_record,
    parse_resolution_assessment_record,
)
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundle,
    load_stage24_input_bundle,
    verify_stage24_input_bundle_unchanged,
)
from researchclaw.pipeline.stage24_obligations import (
    ClaimObligation,
    build_claim_obligation_inventory,
    canonical_obligation_inventory_bytes,
    parse_claim_obligation_inventory,
)


STAGE24_PUBLICATION_POLICY_VERSION = "stage24_truth_v1"
STAGE24_MANIFEST_PATH = "stage24_truth_manifest.json"
_STAGING_NAME = ".stage24-publication.staging"
_ASSESSMENT_DIRECTORIES = (
    "citation-assessments",
    "generic-support-assessments",
    "resolution-assessments",
)
_OUTPUT_NAMES = (
    "obligation_inventory.json",
    "claims.json",
    "citations.json",
    "citation_support.json",
    "critique_resolution.json",
    "truth_audit.json",
)
_SUCCESS_NAMES = (*_OUTPUT_NAMES, *_ASSESSMENT_DIRECTORIES)


class Stage24PublicationError(ValueError):
    """Raised when Stage 24 cannot create or replay one truth generation."""


@dataclass(frozen=True)
class Stage24PublicationSnapshot:
    manifest: BoundArtifact
    outputs: tuple[BoundArtifact, ...]
    assessment_files: tuple[BoundArtifact, ...]


def _publish_stage24_truth(
    run_dir: Path,
    stage_dir: Path,
    *,
    bundle: Stage24InputBundle,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage24PublicationSnapshot:
    """Build, stage, replay, and commit one canonical Stage 24 generation."""

    require_canonical_evidence_capabilities("_publish_stage24_truth_test_fixture")
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-24") as namespace:
        try:
            _reset_namespace(namespace)
            return _publish_after_invalidation(
                namespace,
                bundle=bundle,
                runtime_config=runtime_config,
                llm=llm,
            )
        except Exception as exc:
            try:
                _reset_namespace(namespace)
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 24 cleanup also failed: {cleanup_exc}")
            raise


def execute_stage24_truth(
    run_dir: Path,
    stage_dir: Path,
    *,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage24PublicationSnapshot:
    """Invalidate old authority before capturing any canonical source input."""

    require_canonical_evidence_capabilities("execute_stage24_truth")
    with BoundOutputNamespace.open(run_dir, stage_dir, "stage-24") as namespace:
        try:
            _reset_namespace(namespace)
            bundle = load_stage24_input_bundle(run_dir, runtime_config)
            return _publish_after_invalidation(
                namespace,
                bundle=bundle,
                runtime_config=runtime_config,
                llm=llm,
            )
        except Exception as exc:
            try:
                _reset_namespace(namespace)
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Stage 24 cleanup also failed: {cleanup_exc}")
            raise


def _publish_after_invalidation(
    namespace: BoundOutputNamespace,
    *,
    bundle: Stage24InputBundle,
    runtime_config: RCConfig,
    llm: LLMClient | None,
) -> Stage24PublicationSnapshot:
    require_canonical_evidence_capabilities("_publish_after_invalidation")
    run_dir = namespace.run_dir
    obligations = build_claim_obligation_inventory(bundle.paper.content)
    if _has_audited_prose(bundle.paper.content) and not obligations:
        raise Stage24PublicationError("audited paper produced no obligations")
    directories = _produce_assessment_records(bundle, obligations, llm)
    outputs, success = _derive_publication(bundle, obligations, directories)
    if not success:
        raise Stage24PublicationError("Stage 24 support closure is incomplete")
    _replay_staged(bundle, outputs, directories)
    namespace.stage_flat_tree(
        _STAGING_NAME,
        direct_files=outputs,
        flat_directories=directories,
    )
    staged_outputs, staged_directories = namespace.read_flat_tree(_STAGING_NAME)
    _replay_staged(bundle, staged_outputs, staged_directories)
    verify_stage24_input_bundle_unchanged(run_dir, runtime_config, bundle)
    namespace.publish_staged_tree(
        _STAGING_NAME,
        direct_names=_OUTPUT_NAMES,
        directory_names=_ASSESSMENT_DIRECTORIES,
    )
    manifest = _build_manifest(bundle, outputs, directories)
    namespace.write_bytes_atomic(
        STAGE24_MANIFEST_PATH, _canonical_json_bytes(manifest)
    )
    snapshot = _load_stage24_truth_publication_from_namespace(
        namespace, bundle=bundle
    )
    verify_stage24_input_bundle_unchanged(run_dir, runtime_config, bundle)
    final = _load_stage24_truth_publication_from_namespace(
        namespace, bundle=bundle
    )
    if final != snapshot:
        raise Stage24PublicationError(
            "Stage 24 publication changed after final source fixpoint"
        )
    verify_stage24_input_bundle_unchanged(run_dir, runtime_config, bundle)
    return snapshot


def _load_stage24_truth_publication(
    run_dir: Path,
    *,
    bundle: Stage24InputBundle,
) -> Stage24PublicationSnapshot:
    require_canonical_evidence_capabilities("_load_stage24_truth_publication_test_fixture")
    with BoundOutputNamespace.open(
        run_dir, run_dir / "stage-24", "stage-24"
    ) as namespace:
        return _load_stage24_truth_publication_from_namespace(
            namespace, bundle=bundle
        )


def _load_stage24_truth_publication_from_namespace(
    namespace: BoundOutputNamespace,
    *,
    bundle: Stage24InputBundle,
) -> Stage24PublicationSnapshot:
    require_canonical_evidence_capabilities(
        "_load_stage24_truth_publication_from_namespace"
    )
    manifest_bytes = namespace.read_bytes(STAGE24_MANIFEST_PATH)
    manifest = _parse_manifest(manifest_bytes)
    if set(namespace.direct_entries()) != {STAGE24_MANIFEST_PATH, *_SUCCESS_NAMES}:
        raise Stage24PublicationError("Stage 24 output namespace mismatch")
    outputs = {name: namespace.read_bytes(name) for name in _OUTPUT_NAMES}
    directories = {
        name: namespace.read_flat_directory(name) for name in _ASSESSMENT_DIRECTORIES
    }
    _verify_manifest(manifest, bundle, outputs, directories)
    _replay_staged(bundle, outputs, directories)
    if namespace.read_bytes(STAGE24_MANIFEST_PATH) != manifest_bytes:
        raise Stage24PublicationError("Stage 24 manifest changed during replay")
    for name, content in outputs.items():
        if namespace.read_bytes(name) != content:
            raise Stage24PublicationError(f"Stage 24 output changed during replay: {name}")
    for name, files in directories.items():
        if namespace.read_flat_directory(name) != files:
            raise Stage24PublicationError(
                f"Stage 24 assessment namespace changed during replay: {name}"
            )
    namespace.assert_canonical()
    return Stage24PublicationSnapshot(
        manifest=BoundArtifact(
            f"stage-24/{STAGE24_MANIFEST_PATH}",
            _sha256(manifest_bytes),
            manifest_bytes,
        ),
        outputs=tuple(
            BoundArtifact(f"stage-24/{name}", _sha256(content), content)
            for name, content in sorted(outputs.items())
        ),
        assessment_files=tuple(
            BoundArtifact(
                f"stage-24/{directory}/{name}", _sha256(content), content
            )
            for directory, files in sorted(directories.items())
            for name, content in sorted(files.items())
        ),
    )


def _produce_assessment_records(
    bundle: Stage24InputBundle,
    obligations: tuple[ClaimObligation, ...],
    llm: LLMClient | None,
) -> dict[str, dict[str, bytes]]:
    citation_inputs = _citation_inputs(bundle, obligations)
    citation_files = {
        f"{item.assessment_id}.json": _call_assessment(
            llm,
            model=item.critic_model,
            system=(
                "Judge only whether the supplied retained excerpts support the "
                "bound citation claim. Return the exact JSON source-record schema."
            ),
            assessment_input=item,
            assessment_context=_citation_prompt_context(bundle, obligations, item),
            verdict_field="verdict",
            explanation_field="reason",
        )
        for item in citation_inputs
    }
    citation_records = _parse_citation_records(bundle, citation_inputs, citation_files)
    numeric = _numeric_support(bundle, obligations)
    generic_inputs = _generic_inputs(
        bundle,
        obligations,
        numeric=numeric,
        citation_records=citation_records,
        citation_files=citation_files,
    )
    generic_files = {
        f"{item.assessment_id}.json": _call_assessment(
            llm,
            model=item.critic_model,
            system=(
                "Judge only whether the listed canonical evidence supports the "
                "bound residual manuscript sentence. Return the exact JSON source-record schema."
            ),
            assessment_input=item,
            assessment_context=_generic_prompt_context(bundle, obligations, item),
            verdict_field="verdict",
            explanation_field="reason",
        )
        for item in generic_inputs
    }
    resolution_inputs = _resolution_inputs(bundle)
    resolution_files = {
        f"{item.assessment_id}.json": _call_assessment(
            llm,
            model=item.critic_model,
            system=(
                "Judge whether the bound paper fixes or explicitly rebuts the bound "
                "critique finding. Return fixed, rebutted, or unresolved in the exact schema."
            ),
            assessment_input=item,
            assessment_context=_resolution_prompt_context(bundle, item),
            verdict_field="resolution",
            explanation_field="note",
        )
        for item in resolution_inputs
    }
    return {
        "citation-assessments": citation_files,
        "generic-support-assessments": generic_files,
        "resolution-assessments": resolution_files,
    }


def _call_assessment(
    llm: LLMClient | None,
    *,
    model: str,
    system: str,
    assessment_input: object,
    assessment_context: Mapping[str, Any],
    verdict_field: str,
    explanation_field: str,
) -> bytes:
    if llm is None:
        raise Stage24PublicationError("Stage 24 requires an isolated assessment client")
    payload = _plain(asdict(assessment_input))
    response_schema = {
        "schema_version": 1,
        "assessment_id": payload["assessment_id"],
        "assessment_input_sha256": payload["assessment_input_sha256"],
        "critic_model": model,
        "policy_version": payload["policy_version"],
        verdict_field: (
            "fixed|rebutted|unresolved"
            if verdict_field == "resolution"
            else "supported|unsupported"
        ),
        explanation_field: "<one bounded explanation>",
    }
    response = llm.chat(
        [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "assessment_input": payload,
                        "bound_context": _plain(assessment_context),
                        "response_schema": response_schema,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            }
        ],
        system=system,
        json_mode=True,
        model=model,
        strip_thinking=True,
    )
    content = response.content
    if not isinstance(content, str):
        raise Stage24PublicationError("assessment response is not text")
    return content.encode("utf-8")


def _derive_publication(
    bundle: Stage24InputBundle,
    obligations: tuple[ClaimObligation, ...],
    directories: Mapping[str, Mapping[str, bytes]],
) -> tuple[dict[str, bytes], bool]:
    citation_inputs = _citation_inputs(bundle, obligations)
    citation_records = _parse_citation_records(
        bundle, citation_inputs, directories["citation-assessments"]
    )
    numeric = _numeric_support(bundle, obligations)
    generic_inputs = _generic_inputs(
        bundle,
        obligations,
        numeric=numeric,
        citation_records=citation_records,
        citation_files=directories["citation-assessments"],
    )
    generic_records = _parse_generic_records(
        bundle, generic_inputs, directories["generic-support-assessments"]
    )
    resolution_inputs = _resolution_inputs(bundle)
    resolution_records = _parse_resolution_records(
        bundle, resolution_inputs, directories["resolution-assessments"]
    )
    claims = _claims_ledger(
        bundle.paper.content,
        obligations,
        numeric=numeric,
        citation_records=citation_records,
        generic_records=generic_records,
    )
    citations = _citation_mapping(obligations, citation_records)
    citation_support = _citation_support(bundle, obligations, citation_records)
    critique_resolution = _critique_resolution(bundle, resolution_records)
    violations = list(
        find_dataset_claim_violations(bundle.paper.text(), bundle.dataset_origin)
    )
    unsupported = sum(row["status"] == "unsupported" for row in claims["claims"])
    critique_ok = (
        bundle.critique_publication.state in {"model_final", "external_final"}
        and critique_resolution["counts"]["unresolved"] == 0
    )
    success = (
        unsupported == 0
        and citation_support["valid"] is True
        and critique_ok
        and not violations
    )
    truth = {
        "schema_version": 1,
        "policy_version": STAGE24_PUBLICATION_POLICY_VERSION,
        "stage24_input_bundle_sha256": bundle.identity_sha256,
        "paper_path": bundle.paper.path,
        "paper_sha256": bundle.paper.sha256,
        "critique_state": bundle.critique_publication.state,
        "unsupported_count": unsupported,
        "dataset_claim_violations": violations,
        "citation_support_valid": citation_support["valid"],
        "critique_resolution_valid": critique_ok,
        "stage24_success": success,
    }
    outputs = {
        "obligation_inventory.json": canonical_obligation_inventory_bytes(obligations),
        "claims.json": _canonical_json_bytes(claims),
        "citations.json": _canonical_json_bytes(citations),
        "citation_support.json": _canonical_json_bytes(citation_support),
        "critique_resolution.json": _canonical_json_bytes(critique_resolution),
        "truth_audit.json": _canonical_json_bytes(truth),
    }
    return outputs, success


def _citation_inputs(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
) -> tuple[CitationAssessmentInput, ...]:
    cards = _cards_by_key(bundle)
    candidates_artifact, abstracts = _candidate_abstracts(bundle)
    verification = _verification_by_key(bundle)
    planned_excerpts = _planned_excerpts_by_key(bundle)
    result: list[CitationAssessmentInput] = []
    cited_keys = [
        str(obligation.kind_payload["cite_key"])
        for obligation in obligations
        if obligation.kind == "citation_instance"
    ]
    if set(cited_keys) != set(planned_excerpts):
        raise Stage24PublicationError("citation plan key closure mismatch")
    if set(cited_keys) != set(verification):
        raise Stage24PublicationError("Stage 23 verification key closure mismatch")
    for obligation in obligations:
        if obligation.kind != "citation_instance":
            continue
        cite_key = str(obligation.kind_payload["cite_key"])
        card, card_hash = cards.get(cite_key, (None, None))
        row = verification.get(cite_key)
        if card is None or row is None or str(row.get("status", "")).lower() != "verified":
            raise Stage24PublicationError(
                f"citation obligation lacks verified retained evidence: {cite_key}"
            )
        excerpts = card.get("evidence_excerpts")
        if not isinstance(excerpts, (list, tuple)) or not excerpts:
            raise Stage24PublicationError(f"citation evidence is empty: {cite_key}")
        excerpts_by_id = {
            str(item.get("excerpt_id")): item
            for item in excerpts
            if isinstance(item, Mapping)
            and isinstance(item.get("excerpt_id"), str)
        }
        planned_ids = planned_excerpts.get(cite_key)
        if planned_ids is None or any(item not in excerpts_by_id for item in planned_ids):
            raise Stage24PublicationError(
                f"citation plan evidence closure is incomplete: {cite_key}"
            )
        source_identity = card.get("source_identity")
        abstract = abstracts.get(source_identity)
        if not isinstance(source_identity, str) or abstract is None:
            raise Stage24PublicationError(
                f"citation card source identity is missing: {cite_key}"
            )
        evidence = []
        for excerpt_id in planned_ids:
            item = excerpts_by_id[excerpt_id]
            evidence.append(
                _citation_evidence_record(
                    card=card,
                    card_hash=str(card_hash),
                    excerpt=item,
                    abstract=abstract,
                    candidates_artifact=candidates_artifact,
                )
            )
        result.append(
            build_citation_assessment_input(
                canonical_manifest_sha256=bundle.canonical_manifest.sha256,
                paper_sha256=bundle.paper.sha256,
                obligation_id=obligation.obligation_id,
                byte_start=obligation.byte_start,
                byte_end=obligation.byte_end,
                source_sha256=obligation.source_sha256,
                instance_id=f"cit:{obligation.obligation_id}",
                cite_key=cite_key,
                stage23_verification_record_sha256=_identity_sha256(row),
                evidence_records=evidence,
                critic_model=bundle.model_projection.citation_assessment_model,
            )
        )
    return tuple(result)


def _candidate_abstracts(
    bundle: Stage24InputBundle,
) -> tuple[BoundArtifact, dict[str, str]]:
    artifact = bundle.stage23_inputs.stage22_inputs.stage19_inputs.candidates
    if artifact.path != "stage-04/candidates.jsonl":
        raise Stage24PublicationError("noncanonical Stage 4 candidates path")
    if not artifact.content or not artifact.content.endswith(b"\n"):
        raise Stage24PublicationError("Stage 4 candidates JSONL is incomplete")
    result: dict[str, str] = {}
    for raw_line in artifact.content.splitlines():
        if not raw_line:
            raise Stage24PublicationError("Stage 4 candidates contains an empty line")
        candidate = _parse_json_object(raw_line, "Stage 4 candidate")
        source_identity = candidate.get("source_identity")
        abstract = candidate.get("abstract")
        if (
            not isinstance(source_identity, str)
            or not source_identity
            or source_identity in result
            or not isinstance(abstract, str)
        ):
            raise Stage24PublicationError("Stage 4 candidate identity is invalid")
        result[source_identity] = abstract
    return artifact, result


def _citation_evidence_record(
    *,
    card: Mapping[str, Any],
    card_hash: str,
    excerpt: Mapping[str, Any],
    abstract: str,
    candidates_artifact: BoundArtifact,
) -> CitationEvidenceRecord:
    if (
        excerpt.get("source_type") != "abstract"
        or excerpt.get("source_artifact_path") != candidates_artifact.path
        or excerpt.get("source_artifact_sha256") != candidates_artifact.sha256
        or excerpt.get("source_record_id") != card.get("source_identity")
        or excerpt.get("json_pointer") != "/abstract"
    ):
        raise Stage24PublicationError("citation excerpt provenance is invalid")
    char_start = excerpt.get("char_start")
    char_end = excerpt.get("char_end")
    excerpt_text = excerpt.get("excerpt_text")
    if (
        type(char_start) is not int
        or type(char_end) is not int
        or not 0 <= char_start < char_end <= len(abstract)
        or not isinstance(excerpt_text, str)
        or abstract[char_start:char_end] != excerpt_text
        or _sha256(excerpt_text.encode("utf-8")) != excerpt.get("excerpt_sha256")
    ):
        raise Stage24PublicationError("citation excerpt span is invalid")
    abstract_bytes = abstract.encode("utf-8")
    byte_start = len(abstract[:char_start].encode("utf-8"))
    byte_end = len(abstract[:char_end].encode("utf-8"))
    if abstract_bytes[byte_start:byte_end].decode("utf-8") != excerpt_text:
        raise Stage24PublicationError("citation excerpt byte replay mismatch")
    return CitationEvidenceRecord(
        card_id=str(card["card_id"]),
        card_sha256=card_hash,
        excerpt_id=str(excerpt["excerpt_id"]),
        excerpt_sha256=str(excerpt["excerpt_sha256"]),
        byte_start=byte_start,
        byte_end=byte_end,
    )


def _parse_citation_records(
    bundle: Stage24InputBundle,
    inputs: Sequence[CitationAssessmentInput],
    files: Mapping[str, bytes],
) -> dict[str, Mapping[str, Any]]:
    expected = {f"{item.assessment_id}.json": item for item in inputs}
    if set(files) != set(expected):
        raise Stage24PublicationError("citation assessment namespace mismatch")
    records: dict[str, Mapping[str, Any]] = {}
    for name, item in sorted(expected.items()):
        record = parse_citation_assessment_record(
            _decode(files[name]),
            expected_input=item,
            writer_model=bundle.model_projection.writer_model,
        )
        records[item.obligation_id] = _plain(asdict(record))
    return records


def _generic_inputs(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    *,
    numeric: Mapping[str, Mapping[str, Any]],
    citation_records: Mapping[str, Mapping[str, Any]],
    citation_files: Mapping[str, bytes],
) -> tuple[GenericSupportInput, ...]:
    result: list[GenericSupportInput] = []
    for obligation in obligations:
        if obligation.kind != "declarative_sentence":
            continue
        evidence: list[GenericEvidenceRecord] = []
        for child in obligations:
            if child.byte_start < obligation.byte_start or child.byte_end > obligation.byte_end:
                continue
            if child.obligation_id in numeric and numeric[child.obligation_id]["status"] == "supported":
                support = numeric[child.obligation_id]
                evidence.append(
                    GenericEvidenceRecord(
                        evidence_kind="numeric_support",
                        authority_path=str(support["authority_path"]),
                        authority_sha256=str(support["authority_sha256"]),
                        semantic_pointer=str(support["semantic_pointer"]),
                        semantic_value_sha256=str(support["semantic_value_sha256"]),
                    )
                )
            record = citation_records.get(child.obligation_id)
            if record is not None and record["verdict"] == "supported":
                filename = f"{record['assessment_id']}.json"
                source_bytes = citation_files.get(filename)
                if source_bytes is None:
                    raise Stage24PublicationError(
                        "generic support citation source record is missing"
                    )
                evidence.append(
                    GenericEvidenceRecord(
                        evidence_kind="citation_support",
                        authority_path=f"stage-24/citation-assessments/{record['assessment_id']}.json",
                        authority_sha256=_sha256(source_bytes),
                        semantic_pointer="/verdict",
                        semantic_value_sha256=_sha256(b"supported"),
                    )
                )
        if not evidence:
            continue
        normalized = list(evidence)
        normalized.sort(
            key=lambda item: (
                item.evidence_kind, item.authority_path, item.authority_sha256,
                item.semantic_pointer, item.semantic_value_sha256,
            )
        )
        result.append(
            build_generic_support_input(
                canonical_manifest_sha256=bundle.canonical_manifest.sha256,
                paper_sha256=bundle.paper.sha256,
                obligation_id=obligation.obligation_id,
                byte_start=obligation.byte_start,
                byte_end=obligation.byte_end,
                source_sha256=obligation.source_sha256,
                evidence_records=normalized,
                critic_model=bundle.model_projection.generic_support_model,
            )
        )
    return tuple(result)


def _parse_generic_records(
    bundle: Stage24InputBundle,
    inputs: Sequence[GenericSupportInput],
    files: Mapping[str, bytes],
) -> dict[str, Mapping[str, Any]]:
    expected = {f"{item.assessment_id}.json": item for item in inputs}
    if set(files) != set(expected):
        raise Stage24PublicationError("generic assessment namespace mismatch")
    result: dict[str, Mapping[str, Any]] = {}
    for name, item in sorted(expected.items()):
        record = parse_generic_support_record(
            _decode(files[name]),
            expected_input=item,
            writer_model=bundle.model_projection.writer_model,
        )
        result[item.obligation_id] = _plain(asdict(record))
    return result


def _resolution_inputs(bundle: Stage24InputBundle) -> tuple[ResolutionAssessmentInput, ...]:
    critique = bundle.critique_publication.critique
    if critique is None:
        return ()
    findings = critique.get("findings")
    if not isinstance(findings, (list, tuple)):
        raise Stage24PublicationError("critique findings are invalid")
    return tuple(
        build_resolution_assessment_input(
            critique_sha256=bundle.critique.sha256,
            finding=finding,
            raw_paper_sha256=bundle.paper.sha256,
            critic_model=bundle.model_projection.resolution_assessment_model,
        )
        for finding in findings
        if isinstance(finding, Mapping) and finding.get("severity") in {"P0", "P1"}
    )


def _parse_resolution_records(
    bundle: Stage24InputBundle,
    inputs: Sequence[ResolutionAssessmentInput],
    files: Mapping[str, bytes],
) -> dict[str, Mapping[str, Any]]:
    expected = {f"{item.assessment_id}.json": item for item in inputs}
    if set(files) != set(expected):
        raise Stage24PublicationError("resolution assessment namespace mismatch")
    result: dict[str, Mapping[str, Any]] = {}
    for name, item in sorted(expected.items()):
        record = parse_resolution_assessment_record(
            _decode(files[name]),
            expected_input=item,
            writer_model=bundle.model_projection.writer_model,
        )
        result[item.finding_content_sha256] = _plain(asdict(record))
    return result


def _numeric_support(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
) -> dict[str, Mapping[str, Any]]:
    evidence = bundle.stage23_inputs.stage22_inputs.evidence
    contract_raw = yaml.safe_load(bundle.experiment_contract.text())
    if not isinstance(contract_raw, dict):
        raise Stage24PublicationError("experiment contract is invalid")
    contract = validate_contract_dict(contract_raw)
    execution_artifact = evidence.selected_execution_artifact
    try:
        execution_payload = parse_invocation_result(
            execution_artifact.content.decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage24PublicationError(
            f"selected execution artifact replay failed: {exc}"
        ) from exc
    execution_observations = execution_payload.get("metric_observations")
    if not isinstance(execution_observations, dict):
        raise Stage24PublicationError(
            "selected execution metric observations are missing"
        )
    if not _metric_observations_equal(
        execution_observations, evidence.metric_observations
    ):
        raise Stage24PublicationError(
            "selected execution metric observations differ from canonical evidence"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for obligation in obligations:
        if obligation.kind != "numeric_token":
            continue
        if obligation.kind_payload["numeric_role"] == "identifier_metadata":
            result[obligation.obligation_id] = {"status": "not_required"}
            continue
        try:
            value = Decimal(str(obligation.kind_payload["number_lexeme"]).replace(",", ""))
        except InvalidOperation as exc:
            raise Stage24PublicationError("numeric obligation is invalid") from exc
        unit_lexeme = obligation.kind_payload["unit_lexeme"]
        sentence = _containing_sentence(obligations, obligation)
        sentence_bytes = bundle.paper.content[sentence.byte_start:sentence.byte_end]
        label_binding = _metric_label_binding(
            sentence_bytes,
            sentence_start=sentence.byte_start,
            child=obligation,
            display_labels=contract.metric_display_labels,
        )
        candidates: list[tuple[str, str, int, Decimal]] = []
        if label_binding is not None:
            bound_metric, bound_label = label_binding
            raw_values = evidence.metric_observations.get(bound_metric, ())
            unit = contract.metric_units.get(bound_metric)
            transformed = _transform_value(value, unit_lexeme, unit)
            if transformed is not None:
                for ordinal, raw_value in enumerate(raw_values):
                    canonical = _decimal(raw_value)
                    if canonical == transformed:
                        candidates.append(
                            (bound_metric, bound_label, ordinal, canonical)
                        )
        if len(candidates) != 1:
            result[obligation.obligation_id] = {"status": "unsupported"}
            continue
        metric, display_label, ordinal, canonical = candidates[0]
        canonical_text = _canonical_decimal_text(canonical)
        result[obligation.obligation_id] = {
            "status": "supported",
            "metric": metric,
            "display_label": display_label,
            "invocation_ordinal": execution_payload.get("ordinal"),
            "observation_ordinal": ordinal,
            "canonical_value": canonical_text,
            "authority_path": execution_artifact.path,
            "authority_sha256": execution_artifact.sha256,
            "semantic_pointer": f"/metric_observations/{metric}/{ordinal}",
            "semantic_value_sha256": _sha256(canonical_text.encode("utf-8")),
        }
    return result


def _claims_ledger(
    paper: bytes,
    obligations: Sequence[ClaimObligation],
    *,
    numeric: Mapping[str, Mapping[str, Any]],
    citation_records: Mapping[str, Mapping[str, Any]],
    generic_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for obligation in obligations:
        support_required = not (
            obligation.kind == "numeric_token"
            and obligation.kind_payload["numeric_role"] == "identifier_metadata"
        )
        if not support_required:
            status, record_id = "not_required", None
        elif obligation.kind == "numeric_token":
            status = str(numeric[obligation.obligation_id]["status"])
            record_id = None
        elif obligation.kind == "citation_instance":
            record = citation_records[obligation.obligation_id]
            status = str(record["verdict"])
            record_id = record["assessment_id"]
        elif obligation.kind == "comparative_sentence":
            status, record_id = _comparison_status(
                paper, obligation, obligations, numeric
            ), None
        else:
            record = generic_records.get(obligation.obligation_id)
            status = "supported" if record and record["verdict"] == "supported" else "unsupported"
            record_id = record["assessment_id"] if record else None
        rows.append(
            {
                "obligation_id": obligation.obligation_id,
                "kind": obligation.kind,
                "claim_class": {
                    "numeric_token": "quantitative",
                    "citation_instance": "citation",
                    "comparative_sentence": "comparative",
                    "declarative_sentence": "result",
                }[obligation.kind],
                "byte_start": obligation.byte_start,
                "byte_end": obligation.byte_end,
                "source_sha256": obligation.source_sha256,
                "support_required": support_required,
                "status": status,
                "support_record_id": record_id,
            }
        )
    return {
        "schema_version": 1,
        "policy_version": "claim_ledger_v1",
        "claims": rows,
        "counts": {
            "total": len(rows),
            "supported": sum(row["status"] == "supported" for row in rows),
            "unsupported": sum(row["status"] == "unsupported" for row in rows),
            "not_required": sum(row["status"] == "not_required" for row in rows),
        },
    }


def _citation_mapping(
    obligations: Sequence[ClaimObligation],
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    for obligation in obligations:
        if obligation.kind != "citation_instance":
            continue
        record = records[obligation.obligation_id]
        rows.append(
            {
                "instance_id": f"cit:{obligation.obligation_id}",
                "obligation_id": obligation.obligation_id,
                "cite_key": obligation.kind_payload["cite_key"],
                "assessment_id": record["assessment_id"],
                "status": record["verdict"],
            }
        )
    return {"schema_version": 1, "policy_version": "citation_mapping_v1", "instances": rows}


def _citation_support(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows = []
    for obligation in obligations:
        if obligation.kind != "citation_instance":
            continue
        record = records[obligation.obligation_id]
        rows.append(
            {
                "obligation_id": obligation.obligation_id,
                "cite_key": obligation.kind_payload["cite_key"],
                "assessment_id": record["assessment_id"],
                "verdict": record["verdict"],
            }
        )
    violations = list(find_dataset_claim_violations(bundle.paper.text(), bundle.dataset_origin))
    return {
        "schema_version": 1,
        "policy_version": "citation_support_v2",
        "paper_sha256": bundle.paper.sha256,
        "dataset_origin": bundle.dataset_origin,
        "dataset_claim_violations": violations,
        "instances": rows,
        "counts": {
            "total": len(rows),
            "supported": sum(row["verdict"] == "supported" for row in rows),
            "unsupported": sum(row["verdict"] == "unsupported" for row in rows),
        },
        "valid": all(row["verdict"] == "supported" for row in rows) and not violations,
    }


def _critique_resolution(
    bundle: Stage24InputBundle,
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    critique = bundle.critique_publication.critique
    findings = critique.get("findings", ()) if critique is not None else ()
    rows = []
    for finding in findings:
        if finding["severity"] not in {"P0", "P1"}:
            continue
        expected = build_resolution_assessment_input(
            critique_sha256=bundle.critique.sha256,
            finding=finding,
            raw_paper_sha256=bundle.paper.sha256,
            critic_model=bundle.model_projection.resolution_assessment_model,
        )
        record = records[expected.finding_content_sha256]
        rows.append(
            {
                "finding_id": finding["id"],
                "severity": finding["severity"],
                "finding_content_sha256": expected.finding_content_sha256,
                "assessment_id": record["assessment_id"],
                "resolution": record["resolution"],
            }
        )
    return {
        "schema_version": 1,
        "policy_version": "critique_resolution_v1",
        "critique_path": bundle.critique.path,
        "critique_sha256": bundle.critique.sha256,
        "resolutions": rows,
        "counts": {
            "total": len(rows),
            "fixed": sum(row["resolution"] == "fixed" for row in rows),
            "rebutted": sum(row["resolution"] == "rebutted" for row in rows),
            "unresolved": sum(row["resolution"] == "unresolved" for row in rows),
        },
    }


def _replay_staged(
    bundle: Stage24InputBundle,
    outputs: Mapping[str, bytes],
    directories: Mapping[str, Mapping[str, bytes]],
) -> None:
    if set(outputs) != set(_OUTPUT_NAMES) or set(directories) != set(_ASSESSMENT_DIRECTORIES):
        raise Stage24PublicationError("Stage 24 staged namespace mismatch")
    obligations = parse_claim_obligation_inventory(
        outputs["obligation_inventory.json"], paper_bytes=bundle.paper.content
    )
    expected, success = _derive_publication(bundle, obligations, directories)
    if not success or dict(outputs) != expected:
        raise Stage24PublicationError("Stage 24 staged semantic replay mismatch")


def _build_manifest(
    bundle: Stage24InputBundle,
    outputs: Mapping[str, bytes],
    directories: Mapping[str, Mapping[str, bytes]],
) -> dict[str, Any]:
    files = [
        {"path": name, "sha256": _sha256(content)}
        for name, content in sorted(outputs.items())
    ]
    assessment_files = [
        {"path": f"{directory}/{name}", "sha256": _sha256(content)}
        for directory, rows in sorted(directories.items())
        for name, content in sorted(rows.items())
    ]
    return {
        "schema_version": 1,
        "policy_version": STAGE24_PUBLICATION_POLICY_VERSION,
        "stage24_input_bundle_sha256": bundle.identity_sha256,
        "canonical_manifest": {
            "path": bundle.canonical_manifest.path,
            "sha256": bundle.canonical_manifest.sha256,
        },
        "stage23_publication": {
            "path": bundle.stage23_publication.manifest.path,
            "sha256": bundle.stage23_publication.manifest.sha256,
        },
        "critique_publication": {
            "path": bundle.critique_manifest.path,
            "sha256": bundle.critique_manifest.sha256,
        },
        "paper": {"path": bundle.paper.path, "sha256": bundle.paper.sha256},
        "outputs": files,
        "assessment_files": assessment_files,
        "output_namespace": [STAGE24_MANIFEST_PATH, *_SUCCESS_NAMES],
    }


def _parse_manifest(content: bytes) -> dict[str, Any]:
    value = _parse_json_object(content, "Stage 24 manifest")
    expected = {
        "schema_version", "policy_version", "stage24_input_bundle_sha256",
        "canonical_manifest", "stage23_publication", "critique_publication",
        "paper", "outputs", "assessment_files", "output_namespace",
    }
    if set(value) != expected:
        raise Stage24PublicationError("Stage 24 manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage24PublicationError("Stage 24 manifest schema mismatch")
    if value["policy_version"] != STAGE24_PUBLICATION_POLICY_VERSION:
        raise Stage24PublicationError("Stage 24 manifest policy mismatch")
    if value["output_namespace"] != [STAGE24_MANIFEST_PATH, *_SUCCESS_NAMES]:
        raise Stage24PublicationError("Stage 24 manifest namespace mismatch")
    return value


def _verify_manifest(
    manifest: Mapping[str, Any],
    bundle: Stage24InputBundle,
    outputs: Mapping[str, bytes],
    directories: Mapping[str, Mapping[str, bytes]],
) -> None:
    expected = _build_manifest(bundle, outputs, directories)
    if dict(manifest) != expected:
        raise Stage24PublicationError("Stage 24 manifest replay mismatch")


def _cards_by_key(bundle: Stage24InputBundle) -> dict[str, tuple[Mapping[str, Any], str]]:
    hashes: dict[str, str] = {}
    for entry in bundle.entries:
        if entry.artifact.path.startswith("stage-06/cards/") and entry.artifact.path.endswith(".json"):
            value = _parse_json_object(entry.artifact.content, entry.artifact.path)
            key = value.get("cite_key")
            if isinstance(key, str):
                hashes[key] = entry.artifact.sha256
    result: dict[str, tuple[Mapping[str, Any], str]] = {}
    for card in bundle.evidence_cards:
        key = card.get("cite_key")
        if not isinstance(key, str) or key in result or key not in hashes:
            raise Stage24PublicationError("evidence-card key/hash closure mismatch")
        result[key] = (card, hashes[key])
    return result


def _citation_prompt_context(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    item: CitationAssessmentInput,
) -> Mapping[str, Any]:
    obligation = next(
        row for row in obligations if row.obligation_id == item.obligation_id
    )
    card, _card_hash = _cards_by_key(bundle)[item.cite_key]
    excerpts = card.get("evidence_excerpts")
    excerpts_by_id = {
        row.get("excerpt_id"): row
        for row in excerpts
        if isinstance(row, Mapping) and isinstance(row.get("excerpt_id"), str)
    }
    return {
        "manuscript_context": bundle.paper.content[
            _containing_sentence(obligations, obligation).byte_start:
            _containing_sentence(obligations, obligation).byte_end
        ].decode("utf-8"),
        "retained_excerpts": [
            {
                "excerpt_id": row["excerpt_id"],
                "excerpt_text": row["excerpt_text"],
            }
            for record in item.evidence_records
            for row in (excerpts_by_id[record.excerpt_id],)
        ],
    }


def _generic_prompt_context(
    bundle: Stage24InputBundle,
    obligations: Sequence[ClaimObligation],
    item: GenericSupportInput,
) -> Mapping[str, Any]:
    obligation = next(
        row for row in obligations if row.obligation_id == item.obligation_id
    )
    return {
        "manuscript_sentence": bundle.paper.content[
            obligation.byte_start:obligation.byte_end
        ].decode("utf-8"),
        "evidence_records": [_plain(asdict(row)) for row in item.evidence_records],
    }


def _resolution_prompt_context(
    bundle: Stage24InputBundle,
    item: ResolutionAssessmentInput,
) -> Mapping[str, Any]:
    critique = bundle.critique_publication.critique
    findings = critique.get("findings", ()) if critique is not None else ()
    for finding in findings:
        candidate = build_resolution_assessment_input(
            critique_sha256=bundle.critique.sha256,
            finding=finding,
            raw_paper_sha256=bundle.paper.sha256,
            critic_model=bundle.model_projection.resolution_assessment_model,
        )
        if candidate.assessment_id == item.assessment_id:
            return {"finding": _plain(finding), "paper": bundle.paper.text()}
    raise Stage24PublicationError("resolution assessment finding is missing")


def _verification_by_key(bundle: Stage24InputBundle) -> dict[str, Mapping[str, Any]]:
    rows = bundle.verification.get("results")
    if not isinstance(rows, (list, tuple)):
        raise Stage24PublicationError("Stage 23 verification results are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("cite_key"), str):
            raise Stage24PublicationError("Stage 23 verification row is invalid")
        key = str(row["cite_key"])
        if key in result:
            raise Stage24PublicationError("duplicate Stage 23 verification key")
        result[key] = row
    return result


def _planned_excerpts_by_key(bundle: Stage24InputBundle) -> dict[str, tuple[str, ...]]:
    claims = bundle.citation_plan.get("claims")
    if not isinstance(claims, (list, tuple)):
        raise Stage24PublicationError("citation plan claims are missing")
    result: dict[str, tuple[str, ...]] = {}
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise Stage24PublicationError("citation plan claim is invalid")
        citations = claim.get("planned_citations")
        if not isinstance(citations, (list, tuple)):
            raise Stage24PublicationError("planned citations are missing")
        for citation in citations:
            if not isinstance(citation, Mapping):
                raise Stage24PublicationError("planned citation is invalid")
            key = citation.get("cite_key")
            excerpt_ids = citation.get("evidence_excerpt_ids")
            if (
                not isinstance(key, str)
                or not key
                or key in result
                or not isinstance(excerpt_ids, (list, tuple))
                or not excerpt_ids
                or any(not isinstance(item, str) or not item for item in excerpt_ids)
                or len(set(excerpt_ids)) != len(excerpt_ids)
            ):
                raise Stage24PublicationError("citation plan evidence binding is invalid")
            result[key] = tuple(excerpt_ids)
    return result


def _comparison_status(
    paper: bytes,
    obligation: ClaimObligation,
    obligations: Sequence[ClaimObligation],
    numeric: Mapping[str, Mapping[str, Any]],
) -> str:
    numbers = [
        child
        for child in obligations
        if child.kind == "numeric_token"
        and child.byte_start >= obligation.byte_start
        and child.byte_end <= obligation.byte_end
        and numeric[child.obligation_id]["status"] == "supported"
    ]
    if len(numbers) != 2:
        return "unsupported"
    first_support = numeric[numbers[0].obligation_id]
    second_support = numeric[numbers[1].obligation_id]
    first_label = str(first_support["display_label"])
    second_label = str(second_support["display_label"])
    first_raw = paper[numbers[0].byte_start:numbers[0].byte_end].decode("utf-8")
    second_raw = paper[numbers[1].byte_start:numbers[1].byte_end].decode("utf-8")
    sentence = paper[obligation.byte_start:obligation.byte_end].decode("utf-8")
    relation_pattern = re.compile(
        rf"\s*{re.escape(first_label)}\s*(?:(?:was|is)\s+|[:=]\s*)?"
        rf"{re.escape(first_raw)}\s+(?:is|was)\s+"
        rf"(higher|greater|lower|less)\s+than\s+"
        rf"{re.escape(second_label)}\s*(?:(?:was|is)\s+|[:=]\s*)?"
        rf"{re.escape(second_raw)}\s*[.!?]?\s*",
        re.IGNORECASE,
    )
    match = relation_pattern.fullmatch(sentence)
    if match is None:
        return "unsupported"
    operator = match.group(1).casefold()
    first = Decimal(str(first_support["canonical_value"]))
    second = Decimal(str(second_support["canonical_value"]))
    if operator in {"higher", "greater"}:
        return "supported" if first > second else "unsupported"
    if operator in {"lower", "less"}:
        return "supported" if first < second else "unsupported"
    return "unsupported"


def _containing_sentence(
    obligations: Sequence[ClaimObligation],
    child: ClaimObligation,
) -> ClaimObligation:
    containers = [
        item
        for item in obligations
        if item.kind in {"comparative_sentence", "declarative_sentence"}
        and item.byte_start <= child.byte_start
        and item.byte_end >= child.byte_end
    ]
    if not containers:
        return child
    return min(containers, key=lambda item: item.byte_end - item.byte_start)


def _metric_label_binding(
    sentence: bytes,
    *,
    sentence_start: int,
    child: ClaimObligation,
    display_labels: Mapping[str, Sequence[str]],
) -> tuple[str, str] | None:
    try:
        text = sentence.decode("utf-8")
        local_start = child.byte_start - sentence_start
        local_end = child.byte_end - sentence_start
        number_start = len(sentence[:local_start].decode("utf-8"))
        number_end = len(sentence[:local_end].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Stage24PublicationError("numeric sentence is not UTF-8") from exc
    candidates: set[tuple[str, str]] = set()
    for metric, labels in display_labels.items():
        for label in labels:
            if not isinstance(label, str) or not label:
                raise Stage24PublicationError("metric display label is invalid")
            left = (
                r"(?<![\w-])" if (label[0].isalnum() or label[0] == "_") else ""
            )
            right = (
                r"(?![\w-])" if (label[-1].isalnum() or label[-1] == "_") else ""
            )
            pattern = re.compile(left + re.escape(label) + right, re.IGNORECASE)
            for match in pattern.finditer(text):
                if match.end() <= number_start and re.fullmatch(
                    r"\s*(?:(?:was|is)\s+|[:=]\s*)?",
                    text[match.end():number_start],
                    re.IGNORECASE,
                ):
                    candidates.add((str(metric), label))
                elif number_end <= match.start() and re.fullmatch(
                    r"\s+", text[number_end:match.start()]
                ):
                    candidates.add((str(metric), label))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _transform_value(value: Decimal, unit_lexeme: Any, authority_unit: str | None) -> Decimal | None:
    lexeme = str(unit_lexeme or "").strip()
    if lexeme == "%":
        if authority_unit == "ratio":
            with localcontext() as context:
                context.prec = 50
                context.rounding = ROUND_HALF_EVEN
                return value / Decimal("100")
        return value if authority_unit == "percent" else None
    units = {
        "ns": "nanoseconds", "us": "microseconds", "µs": "microseconds",
        "ms": "milliseconds", "s": "seconds", "bytes": "bytes",
    }
    if lexeme:
        return value if units.get(lexeme) == authority_unit else None
    return value if authority_unit in {"ratio", "count", "unitless"} else None


def _metric_observations_equal(
    stored: Mapping[str, Any], canonical: Mapping[str, Any]
) -> bool:
    if set(stored) != set(canonical):
        return False
    for metric, values in canonical.items():
        stored_values = stored.get(metric)
        if not isinstance(stored_values, list) or not isinstance(values, (list, tuple)):
            return False
        if len(stored_values) != len(values):
            return False
        if any(_decimal(left) != _decimal(right) for left, right in zip(stored_values, values)):
            return False
    return True


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise Stage24PublicationError("boolean metric observation is invalid")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise Stage24PublicationError("nonfinite metric observation is invalid")
    return Decimal(0) if result == 0 else result


def _canonical_decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _has_audited_prose(paper: bytes) -> bool:
    return bool(paper.strip())


def _reset_namespace(namespace: BoundOutputNamespace) -> None:
    errors: list[str] = []
    for name in (STAGE24_MANIFEST_PATH, *_SUCCESS_NAMES, _STAGING_NAME):
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise Stage24PublicationError(
            "Stage 24 authority cleanup was incomplete: " + "; ".join(errors)
        )
    namespace.assert_canonical()


def _parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage24PublicationError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage24PublicationError(f"{label} root is not an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
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


def _identity_sha256(value: object) -> str:
    return _sha256(_canonical_json_bytes(value).rstrip(b"\n"))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return _canonical_decimal_text(value)
    return value


def _decode(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage24PublicationError("assessment source record is not UTF-8") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
