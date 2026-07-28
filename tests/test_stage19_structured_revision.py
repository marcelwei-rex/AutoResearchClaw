"""B4-A inactive structured Stage 19 revision and publication contracts."""

from __future__ import annotations

import inspect
import json
import copy
import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline import structured_scientific_claim_capabilities as capability
from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import stage19_structured_authority as authority
from researchclaw.pipeline import stage19_structured_publication as publication
from researchclaw.pipeline import stage19_structured_transport as transport
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.hitl.intervention import HumanAction, HumanInput
from researchclaw.pipeline.scientific_claim_authority import (
    build_scientific_claim_registry,
    scientific_claim_id,
)
from researchclaw.pipeline.scientific_claim_publication import (
    SECTION_ORDER,
    ScientificClaimSectionSelection,
    ScientificClaimSelectionArtifact,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)

from tests.test_stage17_structured_producer_integration import (
    _SelectionLLM,
    active_capture,
)
from researchclaw.pipeline import stage17_structured_publication as stage17
from researchclaw.pipeline.scientific_claim_publication import (
    parse_scientific_claim_selection_wrapper,
)


def _publish_verified(
    lease: ReleaseGraphLock,
    *,
    namespace: object,
    llm: LLMClient | None,
):
    snapshot, context = publication._capture_snapshot_a_and_issue_context(
        lease, namespace=namespace
    )
    return publication._publish_structured_stage19_from_context(
        lease,
        namespace=namespace,
        snapshot=snapshot,
        context=context,
        llm=llm,
    )


def test_b4_declares_capability_exactly_1110() -> None:
    assert capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }


def test_stage19_owns_exact_six_names_and_one_staging_entry() -> None:
    assert publication.STRUCTURED_STAGE19_OUTPUTS == (
        "scientific_claim_selection.json",
        "scientific_claim_paper_revised.md",
        "scientific_claim_paper_structure_report.json",
        "scientific_claim_experiment_fact_closure_report.json",
        "scientific_claim_citation_closure_report.json",
    )
    assert (
        publication.STRUCTURED_STAGE19_MANIFEST
        == "scientific_claim_authority_manifest.json"
    )
    assert (
        publication.STRUCTURED_STAGE19_STAGING
        == ".stage19-structured-publication.staging"
    )


def test_public_entry_has_no_caller_authority() -> None:
    parameters = inspect.signature(
        publication.execute_structured_stage19_revision
    ).parameters
    assert tuple(parameters) == ("run_dir", "stage_dir", "llm")
    assert "capabilities" not in parameters
    assert "context" not in parameters
    assert "evidence" not in parameters


def test_public_entry_rejects_1110_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_lock(*_args: object, **_kwargs: object) -> None:
        nonlocal touched
        touched = True
        raise AssertionError("structured Stage 19 touched I/O")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.execute_structured_stage19_revision(
            tmp_path / "missing-run",
            tmp_path / "missing-run" / "stage-19",
            None,
        )
    assert touched is False


def test_verified_context_constructor_is_private() -> None:
    with pytest.raises(TypeError, match="dispatch capture is private"):
        publication.StructuredStage19DispatchCapture(
            authority=object(),
            construction_token=object(),
            evidence=object(),  # type: ignore[arg-type]
            run_path="/forged",
            run_identity=(1, 2),
        )
    with pytest.raises(TypeError, match="construction is private"):
        publication.StructuredStage19VerifiedContext(
            authority=object(),
            construction_token=object(),
            writer_owner=object(),
            run_identity=(1, 2),
            run_path="/forged",
            stage_identity=(3, 4),
            generation_binding_sha256="0" * 64,
            stage17_manifest_sha256="1" * 64,
            snapshot_identity=(),
        )


def test_verified_context_rejects_fake_copy_reader_inactive_and_wrong_run(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        snapshot, context = publication._capture_snapshot_a_and_issue_context(
            active_capture.lease, namespace=namespace
        )
        assert all(
            item.size == len(item.content)
            and item.link_count == 1
            and item.mode > 0
            and len(item.identity) == 2
            for item in snapshot.files
        )
        assert tuple(item.path for item in snapshot.files) == (
            *(
                f"stage-17/{name}"
                for name in (
                    *stage17.STRUCTURED_STAGE17_OUTPUTS,
                    stage17.STRUCTURED_STAGE17_MANIFEST,
                )
            ),
            *sorted(
                {
                    snapshot.binding.canonical_experiment_evidence_path,
                    snapshot.binding.experiment_contract_path,
                    snapshot.binding.run_config_path,
                }
                - {
                    f"stage-17/{name}"
                    for name in (
                        *stage17.STRUCTURED_STAGE17_OUTPUTS,
                        stage17.STRUCTURED_STAGE17_MANIFEST,
                    )
                }
            ),
            "stage-16/citation_plan.json",
            "stage-06/citation_allowlist.json",
            "stage-18/reviews.md",
            "stage-18/review_structure_report.json",
        )
        assert (
            publication._require_verified_context(
                active_capture.lease, context, namespace=namespace
            )
            is active_capture.lease
        )
        for forged in (object(), copy.copy(context)):
            with pytest.raises(publication.StructuredStage19PublicationError):
                publication._require_verified_context(
                    active_capture.lease, forged, namespace=namespace
                )
        reader = ReleaseGraphLock.acquire(
            active_capture.run_dir, "reader", mode="read"
        )
        try:
            with pytest.raises(RuntimeError, match="writer_lease_required"):
                publication._require_verified_context(
                    reader, context, namespace=namespace
                )
        finally:
            reader.close()
        inactive = ReleaseGraphLock.acquire(
            active_capture.run_dir, "inactive", mode="write"
        )
        inactive.close()
        with pytest.raises(RuntimeError, match="lease_inactive"):
            publication._require_verified_context(
                inactive, context, namespace=namespace
            )
        run_b = tmp_path / "wrong-run"
        run_b.mkdir()
        with ReleaseGraphLock.acquire(run_b, "wrong", mode="write") as wrong:
            wrong.ensure_run_directory("stage-19")
            with wrong.open_stage_namespace("stage-19") as wrong_namespace:
                with pytest.raises(
                    publication.StructuredStage19PublicationError,
                    match="writer mismatch",
                ):
                    publication._require_verified_context(
                        wrong, context, namespace=wrong_namespace
                    )


def test_pre_activation_source_failure_revokes_stale_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_capture.lease.ensure_run_directory("stage-19")
    stale = b"stale-success-marker"
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        namespace.write_new_text_atomic(
            publication.STRUCTURED_STAGE19_MANIFEST,
            stale.decode(),
        )
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    monkeypatch.setattr(
        publication,
        "_capture_snapshot_a_and_issue_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            publication.StructuredStage19PublicationError(
                "context issuance stopped"
            )
        ),
    )
    with pytest.raises(
        publication.StructuredStage19PublicationError,
        match="context issuance stopped",
    ):
        publication.execute_structured_stage19_revision(
            active_capture.run_dir,
            active_capture.run_dir / "stage-19",
            None,
        )
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        assert namespace.direct_entries() == ()


def test_selection_parser_rejects_extra_authority_fields() -> None:
    with pytest.raises(authority.StructuredStage19AuthorityError):
        authority.parse_provider_selection(
            b'{"selected_claim_ids":[],"ordered_claim_ids":[],'
            b'"connector_template_ids":[],"section_id":"abstract"}\n'
        )


def _stage17_selection(binding) -> ScientificClaimSelectionArtifact:
    registry = build_scientific_claim_registry(binding)
    sections = []
    for section_id in SECTION_ORDER:
        ids = tuple(
            claim.claim_id
            for claim in registry.claims
            if claim.section_id == section_id
        )
        sections.append(
            ScientificClaimSectionSelection(
                section_id,
                ids,
                ids,
                ("NONE",) * max(len(ids) - 1, 0),
            )
        )
    return ScientificClaimSelectionArtifact(
        1,
        "structured-scientific-claim-v1",
        binding.generation_binding_sha256,
        registry.claims_sha256,
        tuple(sections),
    )


def test_stage19_selection_is_exact_descendant(active_capture) -> None:
    binding = active_capture.capture.binding
    source = _stage17_selection(binding)
    responses = authority.inherited_provider_responses(source)
    content = authority.build_stage19_selection(
        responses, source=source, binding=binding
    )
    payload = json.loads(content)
    assert [item["section_id"] for item in payload["sections"]] == list(SECTION_ORDER)


def test_stage19_selection_rejects_mandatory_deletion(active_capture) -> None:
    binding = active_capture.capture.binding
    source = _stage17_selection(binding)
    responses = list(authority.inherited_provider_responses(source))
    first = json.loads(responses[0])
    first["selected_claim_ids"] = []
    first["ordered_claim_ids"] = []
    responses[0] = (json.dumps(first, separators=(",", ":")) + "\n").encode()
    with pytest.raises(
        authority.StructuredStage19AuthorityError, match="mandatory"
    ):
        authority.build_stage19_selection(
            responses, source=source, binding=binding
        )


def test_noop_selection_independently_rerenders_stage17_bytes(active_capture) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    source = parse_scientific_claim_selection_wrapper(
        outputs["scientific_claim_selection.json"],
        binding=active_capture.capture.binding,
    )
    selection = authority.build_stage19_selection(
        authority.inherited_provider_responses(source),
        source=source,
        binding=active_capture.capture.binding,
    )
    revised = authority.rerender_stage19_paper(
        outputs["paper_draft.md"],
        selection,
        source_selection=source,
        binding=active_capture.capture.binding,
    )
    assert revised == outputs["paper_draft.md"]


def test_stage17_full_replay_rebuilds_all_eight_files(active_capture) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = stage17._build_manifest(
        outputs, active_capture.capture
    )
    replayed = authority.replay_stage17_authority(
        outputs,
        binding=active_capture.capture.binding,
        citation_plan=active_capture.capture.citation_plan,
        citation_allowlist=active_capture.capture.citation_allowlist,
        expected_capability_snapshot={
            key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES
        },
    )
    assert replayed.paper == outputs["paper_draft.md"]


def test_stage17_replay_rejects_nongoverned_paper_tamper(active_capture) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = stage17._build_manifest(
        outputs, active_capture.capture
    )
    paper = outputs["paper_draft.md"]
    governed_offset = paper.index(b"## Abstract\n")
    prefix = paper[:governed_offset]
    assert prefix
    outputs["paper_draft.md"] = prefix.replace(b" ", b"  ", 1) + paper[
        governed_offset:
    ]
    with pytest.raises(
        authority.StructuredStage19AuthorityError,
        match="paper differs from independent rerender",
    ):
        authority.replay_stage17_authority(
            outputs,
            binding=active_capture.capture.binding,
            citation_plan=active_capture.capture.citation_plan,
            citation_allowlist=active_capture.capture.citation_allowlist,
            expected_capability_snapshot={
                key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES
            },
        )


def test_stage17_full_replay_rejects_synchronized_registry_tamper(
    active_capture,
) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = stage17._build_manifest(
        outputs, active_capture.capture
    )
    claims = json.loads(outputs["scientific_claim_registry.json"])
    claims[0]["rendered_sentence"] += " forged"
    forged = json.dumps(
        claims, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode() + b"\n"
    outputs["scientific_claim_registry.json"] = forged
    manifest = json.loads(outputs[stage17.STRUCTURED_STAGE17_MANIFEST])
    manifest["claim_registry"]["file"]["sha256"] = __import__("hashlib").sha256(
        forged
    ).hexdigest()
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = (
        json.dumps(
            manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    with pytest.raises(
        authority.StructuredStage19AuthorityError, match="registry"
    ):
        authority.replay_stage17_authority(
            outputs,
            binding=active_capture.capture.binding,
            citation_plan=active_capture.capture.citation_plan,
            citation_allowlist=active_capture.capture.citation_allowlist,
            expected_capability_snapshot={
                key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES
            },
        )


def test_stage17_replay_rejects_synchronized_sentence_hash_and_claim_id_tamper(
    active_capture,
) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = stage17._build_manifest(
        outputs, active_capture.capture
    )
    claims = json.loads(outputs["scientific_claim_registry.json"])
    old_id = claims[0]["claim_id"]
    claims[0]["rendered_sentence"] += " forged"
    claims[0]["rendered_sentence_sha256"] = hashlib.sha256(
        claims[0]["rendered_sentence"].encode()
    ).hexdigest()
    identity_payload = dict(claims[0])
    identity_payload.pop("claim_id")
    claims[0]["claim_id"] = scientific_claim_id(identity_payload)
    registry_bytes = canonical_authority_json_text(claims).encode()
    outputs["scientific_claim_registry.json"] = registry_bytes
    selection = json.loads(outputs["scientific_claim_selection.json"])
    for section in selection["sections"]:
        section["selected_claim_ids"] = [
            claims[0]["claim_id"] if item == old_id else item
            for item in section["selected_claim_ids"]
        ]
        section["ordered_claim_ids"] = [
            claims[0]["claim_id"] if item == old_id else item
            for item in section["ordered_claim_ids"]
        ]
    selection["claim_registry_sha256"] = hashlib.sha256(registry_bytes).hexdigest()
    selection_bytes = canonical_authority_json_text(selection).encode()
    outputs["scientific_claim_selection.json"] = selection_bytes
    manifest = json.loads(outputs[stage17.STRUCTURED_STAGE17_MANIFEST])
    manifest["claim_registry"]["file"]["sha256"] = hashlib.sha256(
        registry_bytes
    ).hexdigest()
    manifest["claim_selection"]["file"]["sha256"] = hashlib.sha256(
        selection_bytes
    ).hexdigest()
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = (
        canonical_authority_json_text(manifest).encode()
    )
    with pytest.raises(
        authority.StructuredStage19AuthorityError, match="registry"
    ):
        authority.replay_stage17_authority(
            outputs,
            binding=active_capture.capture.binding,
            citation_plan=active_capture.capture.citation_plan,
            citation_allowlist=active_capture.capture.citation_allowlist,
            expected_capability_snapshot={
                key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES
            },
        )


def test_stage19_manifest_v2_has_exact_21_keys_and_15_file_refs(
    active_capture,
) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    stage17_manifest_bytes = stage17._build_manifest(
        outputs, active_capture.capture
    )
    stage17_manifest = json.loads(stage17_manifest_bytes)
    digests = authority.Stage19OutputDigests(
        *("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
    )
    content = authority.build_stage19_manifest(
        binding=active_capture.capture.binding,
        capability_snapshot={
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
            "stage24_and_release_integration": 0,
        },
        stage17_manifest=stage17_manifest,
        stage17_manifest_sha256=hashlib.sha256(stage17_manifest_bytes).hexdigest(),
        stage18_reviews_sha256="f" * 64,
        stage18_structure_report_sha256="0" * 64,
        stage18_comment_count=1,
        outputs=digests,
    )
    payload = authority.replay_stage19_manifest(content, expected=content)
    assert set(payload) == authority.STAGE19_MANIFEST_FIELDS

    def count_refs(value: object) -> int:
        if isinstance(value, dict):
            return int(set(value) == {"path", "sha256"}) + sum(
                count_refs(item) for item in value.values()
            )
        if isinstance(value, list):
            return sum(count_refs(item) for item in value)
        return 0

    assert count_refs(payload) == 15
    assert "scientific_claim_authority_manifest.json" not in {
        ref["path"]
        for ref in payload.values()
        if isinstance(ref, dict) and set(ref) == {"path", "sha256"}
        and ref["path"].startswith("stage-19/")
    }


def test_stage19_manifest_rejects_boolean_count(active_capture) -> None:
    outputs = stage17._produce_outputs(active_capture.capture, _SelectionLLM())
    stage17_manifest_bytes = stage17._build_manifest(
        outputs, active_capture.capture
    )
    content = authority.build_stage19_manifest(
        binding=active_capture.capture.binding,
        capability_snapshot={
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
            "stage24_and_release_integration": 0,
        },
        stage17_manifest=json.loads(stage17_manifest_bytes),
        stage17_manifest_sha256=hashlib.sha256(stage17_manifest_bytes).hexdigest(),
        stage18_reviews_sha256="f" * 64,
        stage18_structure_report_sha256="0" * 64,
        stage18_comment_count=1,
        outputs=authority.Stage19OutputDigests(
            *("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
        ),
    )
    payload = json.loads(content)
    payload["facts"]["record_count"] = True
    forged = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    with pytest.raises(
        authority.StructuredStage19AuthorityError, match="facts shape"
    ):
        authority.replay_stage19_manifest(forged, expected=forged)


def _prepare_stage19_sources(active_capture, monkeypatch, *, reviews: bytes = b""):
    run_dir = active_capture.run_dir
    capture = active_capture.capture
    outputs = stage17._produce_outputs(capture, _SelectionLLM())
    outputs[stage17.STRUCTURED_STAGE17_MANIFEST] = stage17._build_manifest(
        outputs, capture
    )
    for name, content in outputs.items():
        (run_dir / "stage-17" / name).write_bytes(content)
    evidence = capture.evidence
    for path, content in (
        (
            evidence.manifest_path,
            canonical_authority_json_text(evidence.manifest).encode(),
        ),
        (evidence.experiment_contract_path, evidence.experiment_contract_bytes),
        (evidence.run_config_path, evidence.run_config_bytes),
    ):
        target = run_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    stage18 = run_dir / "stage-18"
    stage18.mkdir()
    (stage18 / "reviews.md").write_bytes(reviews)
    report = {
        "schema_version": 2,
        "valid": True,
        "source_reviews_path": "stage-18/reviews.md",
        "source_reviews_sha256": hashlib.sha256(reviews).hexdigest(),
        "source_paper_path": "stage-17/paper_draft.md",
        "source_paper_sha256": hashlib.sha256(outputs["paper_draft.md"]).hexdigest(),
        "paper_structure_report_path": "stage-17/paper_structure_report.json",
        "paper_structure_report_sha256": hashlib.sha256(
            outputs["paper_structure_report.json"]
        ).hexdigest(),
        "experiment_fact_closure_report_path": (
            "stage-17/experiment_fact_closure_report.json"
        ),
        "experiment_fact_closure_report_sha256": hashlib.sha256(
            outputs["experiment_fact_closure_report.json"]
        ).hexdigest(),
        "citation_closure_report_path": "stage-17/citation_closure_report.json",
        "citation_closure_report_sha256": hashlib.sha256(
            outputs["citation_closure_report.json"]
        ).hexdigest(),
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "comment_count": len(
            publication.extract_review_ledger(
                reviews.decode(), source_path="stage-18/reviews.md"
            ).comments
        ),
        "issues": [],
    }
    (stage18 / "review_structure_report.json").write_bytes(
        canonical_authority_json_text(report).encode()
    )
    active_capture.lease.ensure_run_directory("stage-19")
    monkeypatch.setattr(
        publication, "load_canonical_experiment_evidence", lambda _run: evidence
    )
    monkeypatch.setattr(publication, "_capture_evidence_files", lambda *_args: ())
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
            "stage24_and_release_integration": 0,
        },
    )
    return outputs


def test_private_pre_activation_noop_publishes_exact_six(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        final = _publish_verified(
            active_capture.lease, namespace=namespace, llm=None
        )
        assert namespace.direct_entries() == tuple(
            sorted(
                (
                    *publication.STRUCTURED_STAGE19_OUTPUTS,
                    publication.STRUCTURED_STAGE19_MANIFEST,
                )
            )
        )
        manifest = json.loads(
            namespace.read_bytes(publication.STRUCTURED_STAGE19_MANIFEST)
        )
        assert manifest["structured_capability_snapshot"] == (
            capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES
        )
        assert len(final.files) == 6
        expected = (
            *publication.STRUCTURED_STAGE19_OUTPUTS,
            publication.STRUCTURED_STAGE19_MANIFEST,
        )
        publication.validate_structured_stage19_executor_postcondition(
            active_capture.lease,
            artifacts=expected,
            evidence_refs=tuple(f"stage-19/{name}" for name in expected),
        )


def test_verified_context_is_one_shot_before_namespace_io(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        snapshot, context = publication._capture_snapshot_a_and_issue_context(
            active_capture.lease,
            namespace=namespace,
        )
        publication._publish_structured_stage19_from_context(
            active_capture.lease,
            namespace=namespace,
            snapshot=snapshot,
            context=context,
            llm=None,
        )
        before = {
            name: namespace.read_bytes(name)
            for name in namespace.direct_entries()
        }
        monkeypatch.setattr(
            publication,
            "_produce_outputs",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("provider/output path reached")
            ),
        )
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="was not issued",
        ):
            publication._publish_structured_stage19_from_context(
                active_capture.lease,
                namespace=namespace,
                snapshot=snapshot,
                context=context,
                llm=None,
            )
        assert {
            name: namespace.read_bytes(name)
            for name in namespace.direct_entries()
        } == before


def test_context_binding_failure_expires_issuance(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        snapshot, context = publication._capture_snapshot_a_and_issue_context(
            active_capture.lease,
            namespace=namespace,
        )
        mismatched = replace(snapshot, run_identity=(-1, -1))
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="does not bind Snapshot A",
        ):
            publication._publish_structured_stage19_from_context(
                active_capture.lease,
                namespace=namespace,
                snapshot=mismatched,
                context=context,
                llm=None,
            )
        assert context not in publication._ISSUED_CONTEXTS
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="was not issued",
        ):
            publication._publish_structured_stage19_from_context(
                active_capture.lease,
                namespace=namespace,
                snapshot=snapshot,
                context=context,
                llm=None,
            )
        assert namespace.direct_entries() == ()


def test_executor_postcondition_mismatch_revokes_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        _publish_verified(
            active_capture.lease, namespace=namespace, llm=None
        )
    with pytest.raises(
        publication.StructuredStage19PublicationError, match="artifact tuple"
    ):
        publication.validate_structured_stage19_executor_postcondition(
            active_capture.lease,
            artifacts=(),
            evidence_refs=(),
        )
    publication.invalidate_structured_stage19_authority(active_capture.lease)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        assert namespace.direct_entries() == ()


def test_nonempty_ledger_with_no_llm_fails_before_publication(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviews = (
        b"## Reviewer A\n\n### Actionable Revisions\n\n"
        b"- Reorder the governed claims.\n"
    )
    _prepare_stage19_sources(
        active_capture, monkeypatch, reviews=reviews
    )
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="requires provider",
        ):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


class _PromptSelectionClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            fallback_url="",
            fallback_models=[],
            primary_model="fixture-model",
            base_url="https://fixture.invalid/v1",
        )
        self.calls = 0

    def _endpoint_url(self, base_url: str) -> str:
        return base_url + "/chat/completions"

    def _raw_call(
        self,
        _model,
        messages,
        _max_tokens,
        _temperature,
        _json_mode,
        **_options,
    ):
        self.calls += 1
        prompt = json.loads(messages[0]["content"])
        return LLMResponse(
            content=canonical_authority_json_text(
                prompt["source_selection"]
            ),
            model="fixture-model",
        )


def test_nonempty_ledger_makes_exactly_five_semantic_calls(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviews = (
        b"## Reviewer A\n\n### Actionable Revisions\n\n"
        b"- Reorder the governed claims.\n"
    )
    _prepare_stage19_sources(
        active_capture, monkeypatch, reviews=reviews
    )
    client = _PromptSelectionClient()
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        _publish_verified(
            active_capture.lease,
            namespace=namespace,
            llm=client,  # type: ignore[arg-type]
        )
    assert client.calls == 5


@pytest.mark.parametrize("mutate_both", [False, True])
def test_stage18_mutation_after_snapshot_a_rejects_before_publication(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    mutate_both: bool,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    original = publication._produce_outputs

    def mutate(snapshot, llm):
        outputs = original(snapshot, llm)
        (active_capture.run_dir / "stage-18" / "reviews.md").write_bytes(b"changed")
        if mutate_both:
            (
                active_capture.run_dir
                / "stage-18"
                / "review_structure_report.json"
            ).write_bytes(b"{}\n")
        return outputs

    monkeypatch.setattr(publication, "_produce_outputs", mutate)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="source fixpoint",
        ):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


def test_final_read_after_output_mutation_revokes_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    original = publication._capture_final
    calls = 0

    def mutate_after_first(namespace, snapshot, manifest):
        nonlocal calls
        result = original(namespace, snapshot, manifest)
        calls += 1
        if calls == 1:
            namespace.write_bytes_atomic(
                "scientific_claim_paper_revised.md", b"late mutation\n"
            )
        return result

    monkeypatch.setattr(publication, "_capture_final", mutate_after_first)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(Exception):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


@pytest.mark.parametrize("collision_kind", ["symlink", "hardlink", "fifo", "staging"])
def test_unsafe_owned_collision_fails_closed(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    collision_kind: str,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    stage19 = active_capture.run_dir / "stage-19"
    name = (
        publication.STRUCTURED_STAGE19_STAGING
        if collision_kind == "staging"
        else publication.STRUCTURED_STAGE19_OUTPUTS[0]
    )
    target = stage19 / name
    if collision_kind == "symlink":
        target.symlink_to(active_capture.run_dir / "stage-18" / "reviews.md")
    elif collision_kind == "hardlink":
        source = active_capture.run_dir / "hardlink-source"
        source.write_bytes(b"alias")
        os.link(source, target)
    elif collision_kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="cleanup failed",
        ):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert name not in namespace.direct_entries()


@pytest.mark.parametrize("source_kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_snapshot_source_rejects_before_provider(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    reviews = active_capture.run_dir / "stage-18" / "reviews.md"
    reviews.unlink()
    if source_kind == "symlink":
        reviews.symlink_to(
            active_capture.run_dir / "stage-18" / "review_structure_report.json"
        )
    elif source_kind == "hardlink":
        source = active_capture.run_dir / "source-alias"
        source.write_bytes(b"")
        os.link(source, reviews)
    else:
        os.mkfifo(reviews)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(Exception):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


def test_run_parent_replacement_is_external_zero_write_delete(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    original = publication._produce_outputs
    detached = active_capture.run_dir.with_name("detached-run")

    def replace_parent(snapshot, llm):
        outputs = original(snapshot, llm)
        active_capture.run_dir.rename(detached)
        active_capture.run_dir.mkdir()
        (active_capture.run_dir / "sentinel").write_bytes(b"external")
        return outputs

    monkeypatch.setattr(publication, "_produce_outputs", replace_parent)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(Exception):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
    assert (active_capture.run_dir / "sentinel").read_bytes() == b"external"
    assert sorted(path.name for path in active_capture.run_dir.iterdir()) == [
        "sentinel"
    ]
    assert not (
        detached / "stage-19" / publication.STRUCTURED_STAGE19_MANIFEST
    ).exists()


def test_stage_parent_replacement_is_external_zero_write_delete(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    original = publication._produce_outputs
    detached_stage = active_capture.run_dir / "stage-19-detached"

    def replace_stage(snapshot, llm):
        outputs = original(snapshot, llm)
        (active_capture.run_dir / "stage-19").rename(detached_stage)
        (active_capture.run_dir / "stage-19").mkdir()
        (active_capture.run_dir / "stage-19" / "sentinel").write_bytes(b"external")
        return outputs

    monkeypatch.setattr(publication, "_produce_outputs", replace_stage)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(Exception):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
    assert (
        active_capture.run_dir / "stage-19" / "sentinel"
    ).read_bytes() == b"external"
    assert sorted(
        path.name for path in (active_capture.run_dir / "stage-19").iterdir()
    ) == ["sentinel"]
    assert not (
        detached_stage / publication.STRUCTURED_STAGE19_MANIFEST
    ).exists()


def test_malformed_generated_manifest_cannot_leave_commit_point(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    monkeypatch.setattr(publication, "_build_manifest", lambda *_args: b"{}\n")
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(authority.StructuredStage19AuthorityError):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


def test_mixed_generation_stage17_selection_is_rejected(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    selection_path = (
        active_capture.run_dir / "stage-17" / "scientific_claim_selection.json"
    )
    selection = json.loads(selection_path.read_bytes())
    selection["generation_binding_sha256"] = "9" * 64
    forged_selection = canonical_authority_json_text(selection).encode()
    selection_path.write_bytes(forged_selection)
    manifest_path = (
        active_capture.run_dir
        / "stage-17"
        / stage17.STRUCTURED_STAGE17_MANIFEST
    )
    manifest = json.loads(manifest_path.read_bytes())
    manifest["claim_selection"]["file"]["sha256"] = hashlib.sha256(
        forged_selection
    ).hexdigest()
    manifest_path.write_bytes(canonical_authority_json_text(manifest).encode())
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        with pytest.raises(
            publication.StructuredStage19PublicationError,
            match="semantic replay",
        ):
            _publish_verified(
                active_capture.lease, namespace=namespace, llm=None
            )
        assert namespace.direct_entries() == ()


def test_executor_structured_dispatch_precedes_generic_live_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dispatch_capture = object()
    dispatch_evidence = SimpleNamespace(
        manifest={"schema_version": 2, "generation_kind": "domain_evaluator"},
        candidate={"schema_version": 2},
        selected_result={
            "schema_version": 2,
            "result_set_type": "stage13_refinement",
        },
    )
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    monkeypatch.setattr(
        publication,
        "issue_structured_stage19_dispatch_capture",
        lambda _run: dispatch_capture,
    )
    monkeypatch.setattr(
        publication,
        "replay_structured_stage19_dispatch_capture",
        lambda capture, _run: (
            dispatch_evidence
            if capture is dispatch_capture
            else (_ for _ in ()).throw(AssertionError("capture changed"))
        ),
    )
    hitl_namespaces = []

    def held_hitl(*_args, **kwargs):
        hitl_namespaces.append(kwargs["authority_namespace"])
        return None

    monkeypatch.setattr(
        pipeline_executor,
        "_run_hitl_pre_stage",
        held_hitl,
    )
    monkeypatch.setattr(
        pipeline_executor,
        "_read_prior_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic live input precheck ran")
        ),
    )
    seen = []

    def producer(
        _stage_dir,
        _run_dir,
        _config,
        _adapters,
        **kwargs,
    ):
        seen.append(kwargs)
        return pipeline_executor.StageResult(
            stage=Stage.PAPER_REVISION,
            status=StageStatus.FAILED,
            artifacts=(),
            error="producer stop",
            decision="structured-scientific-claim-v1",
        )

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS,
        Stage.PAPER_REVISION,
        producer,
    )
    config = RCConfig.load(
        Path(__file__).parent.parent / "config.researchclaw.example.yaml",
        check_paths=False,
    )
    result = pipeline_executor.execute_stage(
        Stage.PAPER_REVISION,
        run_dir=run_dir,
        run_id="b4a-first-io",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    assert result.status is StageStatus.FAILED
    assert result.error == "producer stop"
    assert len(seen) == 1
    assert len(hitl_namespaces) == 1
    assert getattr(hitl_namespaces[0], "stage_name", None) == "stage-19"
    assert seen[0]["prompts"] is None
    assert seen[0]["structured_dispatch_capture"] is dispatch_capture


@pytest.mark.parametrize(
    "human_input",
    [
        HumanInput(action=HumanAction.ABORT),
        HumanInput(action=HumanAction.APPROVE, guidance="rewrite"),
    ],
    ids=["abort", "guidance"],
)
def test_structured_stage19_pre_hitl_rejects_authority_changes(
    tmp_path: Path,
    human_input: HumanInput,
) -> None:
    class _Session:
        def should_pause_before(self, _stage_num):
            return True

        def pause(self, *_args, **_kwargs):
            return None

        def wait_for_human(self):
            return human_input

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapters = AdapterBundle(hitl=_Session())
    with ReleaseGraphLock.acquire(run_dir, "b4a-hitl", mode="write") as lease:
        lease.ensure_run_directory("stage-19")
        with lease.open_stage_namespace("stage-19") as namespace:
            result = pipeline_executor._run_hitl_pre_stage(
                Stage.PAPER_REVISION,
                run_dir,
                adapters,
                structured_stage19_authority=True,
                authority_namespace=namespace,
            )
            assert result is not None
            assert result.status is StageStatus.FAILED
            assert result.artifacts == ()
            assert result.evidence_refs == ()
            assert namespace.direct_entries() == ()


def test_structured_stage19_post_hitl_uses_held_namespace_and_clears_tuples(
    tmp_path: Path,
) -> None:
    class _Session:
        config = None

        def should_pause_after(self, _stage_num):
            return True

        def get_policy(self, _stage_num):
            return SimpleNamespace(
                require_approval=False,
                min_quality_score=0,
            )

        def pause(self, *_args, **_kwargs):
            return None

        def wait_for_human(self):
            return HumanInput(action=HumanAction.ABORT)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapters = AdapterBundle(hitl=_Session())
    with ReleaseGraphLock.acquire(run_dir, "b4a-post-hitl", mode="write") as lease:
        lease.ensure_run_directory("stage-19")
        with lease.open_stage_namespace("stage-19") as namespace:
            namespace.write_new_text_atomic("artifact.txt", "held")
            result = pipeline_executor._run_hitl_post_stage(
                Stage.PAPER_REVISION,
                pipeline_executor.StageResult(
                    stage=Stage.PAPER_REVISION,
                    status=StageStatus.DONE,
                    artifacts=("artifact.txt",),
                    evidence_refs=("stage-19/artifact.txt",),
                ),
                run_dir,
                adapters,
                structured_stage19_authority=True,
                structured_stage19_namespace=namespace,
            )
            assert result.status is StageStatus.FAILED
            assert result.artifacts == ()
            assert result.evidence_refs == ()
            assert namespace.direct_entries() == ()


@pytest.mark.parametrize(
    "outcome",
    ["stable", "missing", "failed-result", "terminal-missing"],
)
def test_executor_stage19_postconditions_are_terminal_and_fail_closed(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
    outcome: str,
) -> None:
    _prepare_stage19_sources(active_capture, monkeypatch)
    monkeypatch.setattr(
        publication,
        "load_dispatch_canonical_evidence",
        lambda _run: active_capture.capture.binding.evidence,
    )
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in capability.REQUIRED_STRUCTURED_CAPABILITIES},
    )
    expected = (
        *publication.STRUCTURED_STAGE19_OUTPUTS,
        publication.STRUCTURED_STAGE19_MANIFEST,
    )
    from researchclaw.pipeline.stage_impls._review_publish import StageResult

    def producer(
        stage_dir,
        run_dir,
        _config,
        _adapters,
        *,
        llm=None,
        prompts=None,
        structured_dispatch_capture=None,
    ):
        _ = (llm, prompts, structured_dispatch_capture)
        with ReleaseGraphLock.acquire(
            run_dir, "b4a-executor-producer", mode="write"
        ) as lease:
            with lease.open_stage_namespace("stage-19") as namespace:
                _publish_verified(
                    lease, namespace=namespace, llm=None
                )
        if outcome == "missing":
            (stage_dir / publication.STRUCTURED_STAGE19_OUTPUTS[0]).unlink()
        return StageResult(
            stage=Stage.PAPER_REVISION,
            status=(
                StageStatus.FAILED
                if outcome == "failed-result"
                else StageStatus.DONE
            ),
            artifacts=() if outcome == "failed-result" else expected,
            error=(
                "producer returned failure after publication"
                if outcome == "failed-result"
                else None
            ),
            decision=(
                "retry"
                if outcome == "failed-result"
                else "structured-scientific-claim-v1"
            ),
            evidence_refs=(
                ()
                if outcome == "failed-result"
                else tuple(f"stage-19/{name}" for name in expected)
            ),
        )

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS,
        Stage.PAPER_REVISION,
        producer,
    )
    immediate_cleanup_observed = []
    if outcome in {"failed-result", "terminal-missing"}:
        def observe_hitl(
            _stage,
            observed_result,
            observed_run_dir,
            _adapters,
            **_kwargs,
        ):
            if outcome == "terminal-missing":
                (
                    observed_run_dir
                    / "stage-19"
                    / publication.STRUCTURED_STAGE19_OUTPUTS[0]
                ).unlink()
            immediate_cleanup_observed.append(
                not (
                    observed_run_dir
                    / "stage-19"
                    / publication.STRUCTURED_STAGE19_MANIFEST
                ).exists()
            )
            return observed_result

        monkeypatch.setattr(
            pipeline_executor,
            "_run_hitl_post_stage",
            observe_hitl,
        )
    config = RCConfig.load(
        Path(__file__).parent.parent / "config.researchclaw.example.yaml",
        check_paths=False,
    )
    result = pipeline_executor.execute_stage(
        Stage.PAPER_REVISION,
        run_dir=active_capture.run_dir,
        run_id="b4a-executor",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    if outcome == "missing":
        assert result.status is StageStatus.FAILED
        assert "postcondition" in (result.error or "").lower()
        assert result.artifacts == ()
        assert result.evidence_refs == ()
        assert not (
            active_capture.stage_dir.parent
            / "stage-19"
            / publication.STRUCTURED_STAGE19_MANIFEST
        ).exists()
    elif outcome == "failed-result":
        assert result.status is StageStatus.FAILED
        assert result.error == "producer returned failure after publication"
        assert immediate_cleanup_observed == [True]
        assert not (
            active_capture.stage_dir.parent
            / "stage-19"
            / publication.STRUCTURED_STAGE19_MANIFEST
        ).exists()
    elif outcome == "terminal-missing":
        assert result.status is StageStatus.FAILED
        assert "terminal postcondition" in (result.error or "").lower()
        assert result.artifacts == ()
        assert result.evidence_refs == ()
        assert immediate_cleanup_observed == [False]
        assert not (
            active_capture.stage_dir.parent
            / "stage-19"
            / publication.STRUCTURED_STAGE19_MANIFEST
        ).exists()
    else:
        assert result.status is StageStatus.DONE
        assert result.artifacts == expected


@pytest.mark.parametrize(
    "capability_map",
    [
        {
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
            "stage24_and_release_integration": 0,
        },
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 0,
        },
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 1,
        },
        {"malformed": 1},
    ],
)
def test_generic_stage19_ignores_stale_structured_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability_map: dict[str, int],
) -> None:
    from researchclaw.pipeline.stage_impls import _review_publish

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-19"
    stage_dir.mkdir(parents=True)
    stale = {}
    for index, name in enumerate(
        (
            *publication.STRUCTURED_STAGE19_OUTPUTS,
            publication.STRUCTURED_STAGE19_MANIFEST,
        )
    ):
        content = f"stale-{index}".encode()
        (stage_dir / name).write_bytes(content)
        stale[name] = content
    monkeypatch.setattr(
        capability, "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES", capability_map
    )
    evidence_loads = []

    def load_evidence(_run):
        evidence_loads.append(_run)
        return SimpleNamespace(
            manifest={},
            candidate={},
            selected_result={},
        )

    monkeypatch.setattr(
        _review_publish,
        "load_canonical_experiment_evidence",
        load_evidence,
    )
    monkeypatch.setattr(
        publication,
        "load_dispatch_canonical_evidence",
        load_evidence,
    )
    monkeypatch.setattr(
        _review_publish,
        "_snapshot_claim_scope",
        lambda _evidence: (_ for _ in ()).throw(
            ValueError("generic replay stop")
        ),
    )
    result = _review_publish._execute_paper_revision(
        stage_dir,
        run_dir,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert {
        name: (stage_dir / name).read_bytes() for name in stale
    } == stale
    assert len(evidence_loads) == 1


def test_transport_bounds_are_code_owned() -> None:
    assert transport.SEMANTIC_CALL_LIMIT == 5
    assert transport.OUTBOUND_ATTEMPT_LIMIT_PER_CALL == 2
    assert transport.TOTAL_OUTBOUND_ATTEMPT_LIMIT == 10
    assert transport.STRUCTURED_STAGE19_MAX_TOKENS > 0


@dataclass
class _RawFixtureClient:
    outcomes: list[object]

    def __post_init__(self) -> None:
        self.config = SimpleNamespace(
            fallback_url="",
            fallback_models=[],
            primary_model="fixture-model",
            base_url="https://fixture.invalid/v1",
        )
        self.requests: list[tuple[object, ...]] = []

    def _endpoint_url(self, base_url: str) -> str:
        return base_url + "/chat/completions"

    def _raw_call(self, *request: object, **options: object) -> LLMResponse:
        self.requests.append((*request, tuple(sorted(options.items()))))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, LLMResponse)
        return outcome


def _response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "selected_claim_ids": [],
                "ordered_claim_ids": [],
                "connector_template_ids": [],
            },
            separators=(",", ":"),
        )
        + "\n",
        model="fixture-model",
    )


def _prompts() -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (section, ('{"target_section":"' + section + '"}\n').encode())
        for section in ("abstract", "results", "discussion", "limitations", "conclusion")
    )


def test_transport_performs_exactly_five_semantic_calls() -> None:
    client = _RawFixtureClient([_response() for _ in range(5)])
    responses, diagnostics = transport.execute_bounded_selection_calls(
        _prompts(),
        client,  # type: ignore[arg-type]
        validate=lambda _section, content: authority.parse_provider_selection(content),
    )
    assert len(responses) == 5
    assert len(client.requests) == 5
    assert [item.semantic_call_ordinal for item in diagnostics] == [1, 2, 3, 4, 5]
    assert dict(client.requests[0][-1])["exact_json_instruction"] is True
    expected_fingerprint = hashlib.sha256(
        b"\0".join(
            (
                b"fixture-model",
                b"https://fixture.invalid/v1/chat/completions",
                b"chat_completions",
                _prompts()[0][1],
                b"json_mode=true",
                b"exact_json_instruction=true",
                b"temperature=0",
                str(transport.STRUCTURED_STAGE19_MAX_TOKENS).encode("ascii"),
            )
        )
    ).hexdigest()
    assert diagnostics[0].request_fingerprint == expected_fingerprint


def test_transport_rejects_anthropic_endpoint_mutation_before_retry() -> None:
    class _MutatingAnthropicClient(_RawFixtureClient):
        def __post_init__(self) -> None:
            super().__post_init__()
            self._anthropic = SimpleNamespace(
                base_url="https://anthropic-primary.invalid"
            )

        def _raw_call(self, *request: object, **options: object) -> LLMResponse:
            self.requests.append((*request, tuple(sorted(options.items()))))
            self._anthropic.base_url = "https://anthropic-mutated.invalid"
            raise TimeoutError("retryable")

    client = _MutatingAnthropicClient([])
    with pytest.raises(
        transport.StructuredStage19TransportError,
        match="frozen request changed",
    ):
        transport.execute_bounded_selection_calls(
            _prompts(),
            client,  # type: ignore[arg-type]
            validate=lambda _section, content: authority.parse_provider_selection(
                content
            ),
        )
    assert len(client.requests) == 1


def test_transport_retries_only_once_with_identical_request() -> None:
    client = _RawFixtureClient([TimeoutError(), _response(), *[_response() for _ in range(4)]])
    _responses, diagnostics = transport.execute_bounded_selection_calls(
        _prompts(),
        client,  # type: ignore[arg-type]
        validate=lambda _section, content: authority.parse_provider_selection(content),
    )
    assert len(client.requests) == 6
    assert client.requests[0] == client.requests[1]
    assert diagnostics[0].request_fingerprint == diagnostics[1].request_fingerprint


def test_transport_semantic_error_is_not_retried() -> None:
    bad = LLMResponse(content='{"selected_claim_ids":[]}\n', model="fixture-model")
    client = _RawFixtureClient([bad, _response()])
    with pytest.raises(transport.StructuredStage19TransportError):
        transport.execute_bounded_selection_calls(
            _prompts(),
            client,  # type: ignore[arg-type]
            validate=lambda _section, content: authority.parse_provider_selection(content),
        )
    assert len(client.requests) == 1


def test_transport_rejects_hidden_fallback_before_outbound() -> None:
    client = _RawFixtureClient([_response()])
    client.config.fallback_models = ["fallback-model"]
    with pytest.raises(transport.StructuredStage19TransportError, match="fallback"):
        transport.execute_bounded_selection_calls(
            _prompts(),
            client,  # type: ignore[arg-type]
            validate=lambda _section, content: authority.parse_provider_selection(content),
        )
    assert client.requests == []


def test_transport_hard_stops_at_two_attempts_per_semantic_call() -> None:
    client = _RawFixtureClient([TimeoutError(), TimeoutError(), _response()])
    with pytest.raises(transport.StructuredStage19TransportError):
        transport.execute_bounded_selection_calls(
            _prompts(),
            client,  # type: ignore[arg-type]
            validate=lambda _section, content: authority.parse_provider_selection(content),
        )
    assert len(client.requests) == 2


def test_transport_total_outbound_bound_is_ten() -> None:
    outcomes = []
    for _ in range(5):
        outcomes.extend((TimeoutError(), _response()))
    client = _RawFixtureClient(outcomes)
    transport.execute_bounded_selection_calls(
        _prompts(),
        client,  # type: ignore[arg-type]
        validate=lambda _section, content: authority.parse_provider_selection(content),
    )
    assert len(client.requests) == 10


def test_transport_rejects_hidden_model_change_without_retry() -> None:
    response = _response()
    response.model = "fallback-model"
    client = _RawFixtureClient([response, _response()])
    with pytest.raises(transport.StructuredStage19TransportError, match="model"):
        transport.execute_bounded_selection_calls(
            _prompts(),
            client,  # type: ignore[arg-type]
            validate=lambda _section, content: authority.parse_provider_selection(content),
        )
    assert len(client.requests) == 1


def test_strict_raw_call_uses_exact_tokens_and_never_endpoint_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "gpt-5",
                    "choices": [
                        {
                            "message": {"content": "{}"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = LLMClient(
        LLMConfig(
            base_url="https://primary.invalid/v1",
            api_key="fixture",
            primary_model="gpt-5",
            fallback_models=[],
            fallback_url="https://fallback.invalid/v1",
        )
    )
    client._raw_call(
        "gpt-5",
        [{"role": "user", "content": "{}\n"}],
        512,
        0,
        True,
        allow_endpoint_fallback=False,
        exact_max_tokens=True,
        exact_temperature=True,
    )
    assert len(requests) == 1
    body = json.loads(requests[0][0].data)
    assert body["max_completion_tokens"] == 512
    assert requests[0][0].full_url.startswith("https://primary.invalid/")


def test_strict_raw_call_rejects_anthropic_temperature_rewrite() -> None:
    client = LLMClient(
        LLMConfig(
            base_url="https://api.anthropic.invalid",
            api_key="fixture",
            primary_model="claude-4-sonnet",
            fallback_models=[],
        )
    )
    client._anthropic = object()
    with pytest.raises(ValueError, match="rewrite exact temperature"):
        client._raw_call(
            "claude-4-sonnet",
            [{"role": "user", "content": "{}\n"}],
            512,
            0,
            True,
            allow_endpoint_fallback=False,
            exact_max_tokens=True,
            exact_temperature=True,
        )


def test_strict_raw_call_rejects_omitted_temperature() -> None:
    client = LLMClient(
        LLMConfig(
            base_url="https://api.openai.invalid/v1",
            api_key="fixture",
            primary_model="o3",
            fallback_models=[],
        )
    )
    with pytest.raises(ValueError, match="omit exact temperature"):
        client._raw_call(
            "o3",
            [{"role": "user", "content": "{}\n"}],
            512,
            0,
            True,
            allow_endpoint_fallback=False,
            exact_max_tokens=True,
            exact_temperature=True,
        )


def test_strict_responses_wire_includes_json_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "gpt-4.1",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "{}"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                    "status": "completed",
                }
            ).encode()

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = LLMClient(
        LLMConfig(
            base_url="https://responses.invalid/v1",
            api_key="fixture",
            wire_api="responses",
            primary_model="gpt-4.1",
            fallback_models=[],
        )
    )
    client._raw_call(
        "gpt-4.1",
        [{"role": "user", "content": "{}\n"}],
        512,
        0,
        True,
        allow_endpoint_fallback=False,
        exact_max_tokens=True,
        exact_temperature=True,
        exact_json_instruction=True,
    )
    body = json.loads(requests[0][0].data)
    assert body["input"][0] == {
        "role": "system",
        "content": [
            {
                "type": "input_text",
                "text": (
                    "You MUST respond with valid JSON only. "
                    "Do not include any text outside the JSON object."
                ),
            }
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "missing_order", "duplicate", "connector", "prose", "extra_hash"],
)
def test_provider_id_and_free_string_injections_fail(
    active_capture,
    mutation: str,
) -> None:
    binding = active_capture.capture.binding
    source = _stage17_selection(binding).sections[0]
    payload = source.provider_dict()
    if mutation == "unknown":
        payload["selected_claim_ids"] = ["f" * 64]
        payload["ordered_claim_ids"] = ["f" * 64]
    elif mutation == "missing_order":
        payload["ordered_claim_ids"] = []
    elif mutation == "duplicate":
        value = payload["selected_claim_ids"][0]
        payload["selected_claim_ids"] = [value, value]
        payload["ordered_claim_ids"] = [value, value]
        payload["connector_template_ids"] = ["NONE"]
    elif mutation == "connector":
        payload["connector_template_ids"] = ["because"]
    elif mutation == "extra_hash":
        payload["generation_binding_sha256"] = binding.generation_binding_sha256
    if mutation == "prose":
        content = b"Keep the abstract claims."
    else:
        content = canonical_authority_json_text(payload).encode()
    with pytest.raises(authority.StructuredStage19AuthorityError):
        authority.validate_descendant_selection(
            content,
            target_section="abstract",
            source=source,
            binding=binding,
        )
