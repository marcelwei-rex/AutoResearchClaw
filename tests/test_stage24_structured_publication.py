"""B5-A4 structured Stage 24 private/public and executor boundaries."""

from __future__ import annotations

import inspect
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.config import RCConfig
from researchclaw.pipeline import executor
from researchclaw.pipeline import scientific_claim_authority
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.stage15_critique import publish_model_or_none_critique
from researchclaw.pipeline import stage24_structured_publication as publication
from researchclaw.pipeline import stage24_structured_transport as transport
from researchclaw.pipeline.stage24_obligations import (
    build_claim_obligation_inventory,
)
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage23_structured_publication import (
    _publish as publish_stage23,
    passed_stage22,
)
from tests.test_stage21_structured_archive import passed_stage20
from tests.test_stage17_structured_producer_integration import active_capture


def test_renderer_slot_projection_preserves_every_registry_sentence_byte(
    active_capture,
) -> None:
    binding = scientific_claim_authority.build_scientific_claim_generation_binding(
        active_capture.capture.evidence
    )
    registry = scientific_claim_authority.build_scientific_claim_registry(binding)
    facts = {fact.fact_id: fact for fact in registry.facts}
    for claim in registry.claims:
        projection = scientific_claim_authority.render_scientific_claim_with_slots(
            claim.renderer_template_id,
            claim.renderer_slot_fact_ids,
            registry=registry,
            binding=binding,
        )
        assert projection.rendered.sentence == claim.rendered_sentence
        assert projection.rendered.content == claim.rendered_sentence.encode("utf-8")
        assert projection.rendered.sha256 == claim.rendered_sentence_sha256
        assert tuple(slot.slot_ordinal for slot in projection.slots) == tuple(
            range(len(claim.renderer_slot_fact_ids))
        )
        assert tuple(slot.fact_id for slot in projection.slots) == (
            claim.renderer_slot_fact_ids
        )
        assert tuple(slot.fact_kind for slot in projection.slots) == tuple(
            facts[fact_id].fact_kind
            for fact_id in claim.renderer_slot_fact_ids
        )
        for slot in projection.slots:
            rendered_value = projection.rendered.content[
                slot.byte_start : slot.byte_end
            ].decode("utf-8")
            expected_value = facts[slot.fact_id].object_value
            if slot.fact_kind == "primary_metric_key":
                expected_value = "AUPRC"
            assert rendered_value == expected_value


@pytest.mark.parametrize(
    ("in_d", "in_u", "generic_verdict", "expected"),
    (
        (True, False, None, ("supported", None)),
        (True, True, "supported", ("supported", "assessment")),
        (True, True, "unsupported", ("unsupported", "assessment")),
        (True, True, None, ("unsupported", None)),
        (False, True, "supported", ("supported", "assessment")),
        (False, True, None, ("unsupported", None)),
        (False, False, None, ("unsupported", None)),
    ),
)
def test_declarative_ledger_precedence_is_exact(
    in_d: bool,
    in_u: bool,
    generic_verdict: str | None,
    expected: tuple[str, str | None],
) -> None:
    generic_record = (
        None
        if generic_verdict is None
        else {
            "verdict": generic_verdict,
            "assessment_id": "assessment",
        }
    )
    assert publication._declarative_ledger_state(
        in_d=in_d,
        in_u=in_u,
        generic_record=generic_record,
    ) == expected


def _renderer_occurrence(
    sentence: bytes,
    *,
    byte_start: int,
    section_id: str = "results",
    claim_id: str = "claim-a",
) -> publication._RendererOccurrence:
    digest = hashlib.sha256(sentence).hexdigest()
    return publication._RendererOccurrence(
        generation_binding_sha256="a" * 64,
        cfs_sha256="b" * 64,
        registry_content=b"[]\n",
        registry_sha256=hashlib.sha256(b"[]\n").hexdigest(),
        section_id=section_id,
        selection_ordinal=0,
        claim_id=claim_id,
        renderer_template_id=f"{section_id}.fixture.v1",
        renderer_slot_fact_ids=(),
        sentence_content=sentence,
        sentence_sha256=digest,
        stage19_paper_sha256="c" * 64,
        stage19_byte_start=byte_start,
        stage19_byte_end=byte_start + len(sentence),
        stage22_paper_sha256="d" * 64,
        final_paper_path="stage-23/paper_final_verified.md",
        final_paper_sha256="e" * 64,
        final_paper_identity=(1, 2),
        byte_start=byte_start,
        byte_end=byte_start + len(sentence),
        slots=(),
    )


def test_d_consumption_is_bidirectional_and_nonrenderer_prose_stays_out() -> None:
    paper = b"## Results\n\nRendered statement. Nonrenderer statement.\n"
    obligations = build_claim_obligation_inventory(paper)
    declaratives = tuple(
        item for item in obligations if item.kind == "declarative_sentence"
    )
    assert len(declaratives) == 2
    rendered = declaratives[0]
    occurrence = _renderer_occurrence(
        paper[rendered.byte_start : rendered.byte_end],
        byte_start=rendered.byte_start,
    )
    assert publication._deterministic_declarative_ids(
        (occurrence,), obligations
    ) == (rendered.obligation_id,)
    assert declaratives[1].obligation_id not in publication._deterministic_declarative_ids(
        (occurrence,), obligations
    )

    missing = replace(
        occurrence,
        byte_start=occurrence.byte_start + 1,
        byte_end=occurrence.byte_end + 1,
    )
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="match is ambiguous",
    ):
        publication._deterministic_declarative_ids((missing,), obligations)

    duplicate_obligation = replace(
        rendered,
        obligation_id="obl-" + "f" * 64,
    )
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="match is ambiguous",
    ):
        publication._deterministic_declarative_ids(
            (occurrence,),
            (*obligations, duplicate_obligation),
        )
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="identity is duplicated",
    ):
        publication._deterministic_declarative_ids(
            (occurrence, occurrence), obligations
        )


def test_d_consumption_handles_cross_section_duplicates_reorder_and_deletion() -> None:
    sentence = b"Repeated renderer sentence."
    paper = (
        b"## Results\n\n"
        + sentence
        + b"\n\n## Discussion\n\n"
        + sentence
        + b"\n"
    )
    obligations = build_claim_obligation_inventory(paper)
    declaratives = tuple(
        item for item in obligations if item.kind == "declarative_sentence"
    )
    assert len(declaratives) == 2
    occurrences = tuple(
        _renderer_occurrence(
            sentence,
            byte_start=item.byte_start,
            section_id=("results" if index == 0 else "discussion"),
            claim_id=f"claim-{1 - index}",
        )
        for index, item in enumerate(declaratives)
    )

    assert publication._deterministic_declarative_ids(
        occurrences, obligations
    ) == tuple(item.obligation_id for item in declaratives)
    assert publication._deterministic_declarative_ids(
        occurrences[:1], obligations
    ) == (declaratives[0].obligation_id,)
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="occurrences overlap|order mismatch",
    ):
        publication._deterministic_declarative_ids(
            tuple(reversed(occurrences)), obligations
        )


def test_renderer_occurrence_identity_binds_every_authority_layer_and_slot() -> None:
    sentence = b"Primary AUPRC is 0.50."
    base = _renderer_occurrence(sentence, byte_start=20)
    slot = publication._RendererSlotOccurrence(
        section_id="results",
        selection_ordinal=0,
        claim_id="claim-a",
        slot_ordinal=0,
        fact_kind="primary_metric_value",
        fact_id="fact-a",
        stage19_byte_start=37,
        stage19_byte_end=41,
        byte_start=37,
        byte_end=41,
    )
    base = replace(
        base,
        renderer_slot_fact_ids=("fact-a",),
        slots=(slot,),
    )
    mutations = (
        {"generation_binding_sha256": "f" * 64},
        {"cfs_sha256": "f" * 64},
        {"registry_content": b"[{}]\n"},
        {"registry_sha256": "f" * 64},
        {"claim_id": "claim-b"},
        {"renderer_template_id": "results.other.v1"},
        {"renderer_slot_fact_ids": ("fact-b",)},
        {"section_id": "discussion"},
        {"selection_ordinal": 1},
        {"sentence_content": b"Primary AUPRC is 0.60."},
        {"sentence_sha256": "f" * 64},
        {"stage19_paper_sha256": "f" * 64},
        {"stage19_byte_start": 21},
        {"stage19_byte_end": 45},
        {"stage22_paper_sha256": "f" * 64},
        {"final_paper_path": "stage-23/other.md"},
        {"final_paper_sha256": "f" * 64},
        {"final_paper_identity": (2, 3)},
        {"byte_start": 21},
        {"byte_end": 45},
        {"slots": (replace(slot, slot_ordinal=1),)},
        {"slots": (replace(slot, fact_id="fact-b"),)},
        {"slots": (replace(slot, fact_kind="condition"),)},
        {"slots": (replace(slot, byte_start=38),)},
    )
    for changes in mutations:
        assert replace(base, **changes).identity_tuple != base.identity_tuple


def test_renderer_numeric_mapping_uses_ordered_slots_when_condition_equals_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = b"## Results\n\nAUPRC 0.5 under 0.5 condition.\n"
    obligations = build_claim_obligation_inventory(paper)
    numeric = tuple(
        item
        for item in obligations
        if item.kind == "numeric_token"
        and item.kind_payload["numeric_role"] == "claim_numeric"
    )
    assert len(numeric) == 2
    metric_start = paper.index(b"AUPRC")
    condition_start = numeric[0].byte_start
    value_start = numeric[1].byte_start
    slots = (
        publication._RendererSlotOccurrence(
            "results", 0, "claim-a", 0, "primary_metric_key", "metric",
            metric_start, metric_start + 5, metric_start, metric_start + 5,
        ),
        publication._RendererSlotOccurrence(
            "results", 0, "claim-a", 1, "primary_condition", "condition",
            condition_start, condition_start + 3, condition_start,
            condition_start + 3,
        ),
        publication._RendererSlotOccurrence(
            "results", 0, "claim-a", 2, "primary_metric_value", "value",
            value_start, value_start + 3, value_start, value_start + 3,
        ),
    )
    occurrence = replace(
        _renderer_occurrence(
            paper[numeric[0].byte_start - 6 : numeric[1].byte_end + 11],
            byte_start=numeric[0].byte_start - 6,
        ),
        renderer_slot_fact_ids=("metric", "condition", "value"),
        slots=slots,
    )
    facts = (
        SimpleNamespace(
            fact_id="metric",
            object_value="detection_auprc",
        ),
        SimpleNamespace(fact_id="condition", object_value="0.5"),
        SimpleNamespace(fact_id="value", object_value="0.5"),
    )
    monkeypatch.setattr(
        publication.claim_authority,
        "build_scientific_claim_registry",
        lambda _binding: SimpleNamespace(facts=facts),
    )
    pointer = (
        "/derived/canonical_fact_sheet/v1/condition_aggregates/0/"
        "metrics/detection_auprc/mean"
    )
    result, replayed_slots = publication._renderer_numeric_bindings(
        SimpleNamespace(paper=SimpleNamespace(content=paper)),
        obligations,
        structured_base=SimpleNamespace(
            base=SimpleNamespace(
                base=SimpleNamespace(
                    snapshot=SimpleNamespace(
                        stage19_snapshot=SimpleNamespace(binding=object())
                    )
                )
            )
        ),
        occurrences=(occurrence,),
        cfs={
            "condition_aggregates": (
                {
                    "condition": "0.5",
                    "metrics": {"detection_auprc": {"mean": "0.5"}},
                },
            )
        },
        records=({"pointer": pointer, "value": "0.5"},),
        contract=SimpleNamespace(
            metric_display_labels={"detection_auprc": ("AUPRC",)},
            metric_units={"detection_auprc": "ratio"},
        ),
        cfs_ref={"schema_version": 1, "sha256": "a" * 64},
    )
    assert tuple(result) == (numeric[1].obligation_id,)
    assert numeric[0].obligation_id not in result
    assert replayed_slots == slots

    without_value = replace(occurrence, slots=slots[:2])
    assert publication._renderer_numeric_bindings(
        SimpleNamespace(paper=SimpleNamespace(content=paper)),
        obligations,
        structured_base=SimpleNamespace(
            base=SimpleNamespace(
                base=SimpleNamespace(
                    snapshot=SimpleNamespace(
                        stage19_snapshot=SimpleNamespace(binding=object())
                    )
                )
            )
        ),
        occurrences=(without_value,),
        cfs={"condition_aggregates": ()},
        records=(),
        contract=SimpleNamespace(),
        cfs_ref={"schema_version": 1, "sha256": "a" * 64},
    )[0] == {}
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="value slot is ambiguous",
    ):
        publication._renderer_numeric_bindings(
            SimpleNamespace(paper=SimpleNamespace(content=paper)),
            obligations,
            structured_base=SimpleNamespace(
                base=SimpleNamespace(
                    base=SimpleNamespace(
                        snapshot=SimpleNamespace(
                            stage19_snapshot=SimpleNamespace(binding=object())
                        )
                    )
                )
            ),
            occurrences=(
                replace(
                    occurrence,
                    slots=(
                        *slots,
                        replace(slots[-1], slot_ordinal=3),
                    ),
                ),
            ),
            cfs={"condition_aggregates": ()},
            records=(),
            contract=SimpleNamespace(),
            cfs_ref={"schema_version": 1, "sha256": "a" * 64},
        )


def test_numeric_support_never_raw_falls_back_from_nonvalue_renderer_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = b"## Results\n\nAUPRC 0.5 is reported.\n"
    obligations = build_claim_obligation_inventory(paper)
    numeric = next(
        item
        for item in obligations
        if item.kind == "numeric_token"
        and item.kind_payload["numeric_role"] == "claim_numeric"
    )
    nonvalue_slot = publication._RendererSlotOccurrence(
        section_id="results",
        selection_ordinal=0,
        claim_id="claim-a",
        slot_ordinal=0,
        fact_kind="primary_condition",
        fact_id="condition",
        stage19_byte_start=numeric.byte_start,
        stage19_byte_end=numeric.byte_end,
        byte_start=numeric.byte_start,
        byte_end=numeric.byte_end,
    )
    monkeypatch.setattr(
        publication,
        "build_canonical_fact_sheet",
        lambda _evidence: {"condition_aggregates": ()},
    )
    monkeypatch.setattr(
        publication,
        "fact_sheet_numeric_authority_records",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        publication,
        "validate_contract_dict",
        lambda _raw: SimpleNamespace(metric_display_labels={}, metric_units={}),
    )
    monkeypatch.setattr(
        publication,
        "_renderer_numeric_bindings",
        lambda *_args, **_kwargs: ({}, (nonvalue_slot,)),
    )
    monkeypatch.setattr(
        publication.generic_stage24,
        "_metric_label_binding",
        lambda *_args, **_kwargs: pytest.fail(
            "non-value renderer slot reached raw numeric fallback"
        ),
    )
    evidence = SimpleNamespace(cfs_sha256="a" * 64)
    result, _cfs_ref, _records = publication._numeric_support(
        SimpleNamespace(
            stage23_inputs=SimpleNamespace(
                stage22_inputs=SimpleNamespace(evidence=evidence)
            ),
            experiment_contract=SimpleNamespace(content=b"{}"),
            paper=SimpleNamespace(content=paper),
        ),
        obligations,
        structured_base=object(),
        renderer_occurrences=(),
    )
    assert result[numeric.obligation_id] == {"status": "unsupported"}


@pytest.mark.parametrize("quality_outcome", ("passed", "degraded"))
def test_stage22_transform_carries_complete_renderer_sentence_span(
    quality_outcome: str,
) -> None:
    sentence = b"Rendered result statement."
    paper = (
        b"## Abstract\n\nAbstract statement.\n\n"
        b"![plot](charts/result.png)\n\n"
        b"## Results\n\n"
        + sentence
        + b"\n\n"
    )
    start = paper.index(sentence)
    occurrence = _renderer_occurrence(sentence, byte_start=start)
    text = paper.decode()
    if quality_outcome == "degraded":
        text = publication.stage22_semantics._insert_degradation_notice(text)
    text = publication.stage22_semantics._remove_unavailable_markdown_figures(text)
    expected = text.encode()

    shifted = publication._carry_renderer_occurrences_through_stage22(
        paper,
        (occurrence,),
        quality_outcome=quality_outcome,
        expected=expected,
    )

    assert len(shifted) == 1
    assert expected[shifted[0].byte_start : shifted[0].byte_end] == sentence
    assert shifted[0].stage19_byte_start == start
    assert shifted[0].stage22_paper_sha256 == hashlib.sha256(expected).hexdigest()


def test_stage22_transform_rejects_incomplete_output_and_overlap() -> None:
    sentence = b"Rendered result statement."
    paper = (
        b"## Abstract\n\nAbstract statement.\n\n"
        b"![plot](charts/result.png)\n\n"
        b"## Results\n\n"
        + sentence
        + b"\n"
    )
    start = paper.index(sentence)
    occurrence = _renderer_occurrence(sentence, byte_start=start)
    expected = publication.stage22_semantics._remove_unavailable_markdown_figures(
        paper.decode()
    ).encode()
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="paper replay mismatch",
    ):
        publication._carry_renderer_occurrences_through_stage22(
            paper,
            (occurrence,),
            quality_outcome="passed",
            expected=expected + b"late mutation",
        )

    figure_start = paper.index(b"![plot]")
    overlapping = _renderer_occurrence(
        paper[figure_start : paper.index(b"## Results")],
        byte_start=figure_start,
    )
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="overlaps renderer sentence",
    ):
        publication._carry_renderer_occurrences_through_stage22(
            paper,
            (overlapping,),
            quality_outcome="passed",
            expected=expected,
        )


def test_b5a4_keeps_capability_exactly_1110_and_generic_dispatch_unchanged() -> None:
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }
    from researchclaw.pipeline.stage_impls._release_audit import (
        _execute_truth_audit,
    )

    assert executor._STAGE_EXECUTORS[Stage.TRUTH_AUDIT] is _execute_truth_audit


def test_public_direct_guard_precedes_lock_path_provider_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def forbidden(*_args, **_kwargs):
        touched.append("io")
        raise AssertionError("guard was not first")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.execute_structured_stage24_truth(
            tmp_path,
            tmp_path / "stage-24",
            clients={},
        )
    assert touched == []


def test_private_executor_has_no_generic_llm_or_prompt_hook() -> None:
    parameters = tuple(
        inspect.signature(executor._execute_structured_stage24_private).parameters
    )
    assert parameters == (
        "release_lock",
        "pre_admission_context",
    )
    assert "LLMClient.chat" not in inspect.getsource(
        executor._execute_structured_stage24_private
    )


def test_model_projection_rejects_writer_as_any_critic() -> None:
    config = SimpleNamespace(
        llm=SimpleNamespace(
            primary_model="same-model",
            critic_model="same-model",
        ),
        paper_revision=SimpleNamespace(critic_model="same-model"),
    )
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="not isolated",
    ):
        publication._model_projection(config)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("registry_authority_token", object()),
        ("registry_identity", object()),
        ("release_graph_owner_object", object()),
        ("run_fd_identity", (9, 9)),
        ("writer_epoch_object", object()),
        ("stage24_attempt_context_object", object()),
        ("canonical_config_object", object()),
        ("semantic_config_sha256", "b" * 64),
        ("role", "resolution_assessment"),
        ("binding", object()),
        ("active_client_exact_type", object),
        ("underlying_client_binding_sha256", "c" * 64),
        ("credential_source_identity", object()),
        ("credential_identity", object()),
        ("credential_bytes", b"different-credential"),
    ),
)
def test_live_client_record_drift_is_rejected_and_cleanup_is_complete(
    field: str,
    replacement: object,
) -> None:
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    config = RCConfig.load(
        Path(__file__).parent.parent / "config.researchclaw.example.yaml",
        check_paths=False,
    )
    digest = publication.semantic_config_sha256(config)
    epoch = object()

    class Owner:
        _run_identity = (1, 2)

        def _require_active(self):
            return epoch

    class Context:
        pass

    owner = Owner()
    context = Context()
    registry = transport._issue_client_registry(
        owner_identity=epoch,
        run_identity=(1, 2),
        semantic_config_sha256=digest,
        factories={"citation_assessment": _client_factory},
    )
    capture = SimpleNamespace(
        client_registry=registry,
        config_digest=digest,
        release_graph_owner=owner,
        writer_epoch_object=epoch,
        canonical_config_object=config,
        attempt_context_object=context,
    )
    publication._ATTEMPT_CONTEXTS[context] = SimpleNamespace(initial=capture)
    active = publication._activate_registered_client(
        capture,
        "citation_assessment",
        binding,
    )
    record = publication._ACTIVE_CLIENTS[active.client]
    if field == "registry_authority_token":
        copied = replace(record)
        assert copied is not record and copied == record
        publication._ACTIVE_REGISTRATIONS[
            record.registration_identity
        ] = copied
        with pytest.raises(
            publication.StructuredStage24PublicationError,
            match="registration mismatch",
        ):
            publication._activate_registered_client(
                capture,
                "citation_assessment",
                binding,
            )
        publication._ACTIVE_REGISTRATIONS[
            record.registration_identity
        ] = record

        wrong_registry = transport._issue_client_registry(
            owner_identity=object(),
            run_identity=(1, 2),
            semantic_config_sha256="b" * 64,
            factories={"citation_assessment": _client_factory},
        )
        capture.client_registry = wrong_registry
        with pytest.raises(
            publication.StructuredStage24PublicationError,
            match="epoch/context mismatch",
        ):
            publication._activate_registered_client(
                capture,
                "citation_assessment",
                binding,
            )
        capture.client_registry = registry
    forged = replace(record, **{field: replacement})
    publication._ACTIVE_CLIENTS[active.client] = forged
    publication._ACTIVE_REGISTRATIONS[record.registration_identity] = forged
    try:
        with pytest.raises(
            publication.StructuredStage24PublicationError,
            match="registration mismatch",
        ):
            publication._activate_registered_client(
                capture,
                "citation_assessment",
                binding,
            )
    finally:
        class Closer:
            def close(self):
                pass

        publication._CapturedUpstream.close(
            SimpleNamespace(
                client_registry=registry,
                source_files=(),
                stage15_namespace=Closer(),
                stage23_namespace=Closer(),
                base=Closer(),
            )
        )
        publication._ATTEMPT_CONTEXTS.pop(context, None)
    assert active.client not in publication._ACTIVE_CLIENTS
    assert record.registration_identity not in publication._ACTIVE_REGISTRATIONS


def test_live_client_subclass_is_rejected() -> None:
    class DerivedClient(transport.Stage24AssessmentTransport):
        pass

    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    registry = transport._issue_client_registry(
        owner_identity=object(),
        run_identity=(1, 2),
        semantic_config_sha256="a" * 64,
        factories={
            "citation_assessment": lambda value: DerivedClient(
                value,
                credential_source_identity=object(),
                credential_identity=object(),
                credential_bytes=b"credential",
            )
        },
    )
    with pytest.raises(transport.Stage24TransportError):
        registry.activate(role="citation_assessment", binding=binding)


def test_pre_admission_failure_is_failed_retry_with_empty_tuples(
    tmp_path: Path,
) -> None:
    class BrokenLease:
        run_dir = tmp_path

    result = executor._execute_structured_stage24_with_pre_admission_private(
        BrokenLease(),
        clients={},
    )
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()


def test_structured_stage24_success_tuple_is_exact_coarse_namespace() -> None:
    assert publication.STRUCTURED_STAGE24_ARTIFACTS == (
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
    assert publication.STRUCTURED_STAGE24_EVIDENCE_REFS == tuple(
        f"stage-24/{name}" for name in publication.STRUCTURED_STAGE24_ARTIFACTS
    )


def _assessment_http(request: transport.AssessmentRequest) -> bytes:
    if request.role == "resolution_assessment":
        decision = {"resolution": "fixed", "note": "The finding is fixed."}
    else:
        decision = {"verdict": "supported", "reason": "Exact bound support."}
    body = __import__("json").dumps(
        {
            "model": request.binding.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": __import__("json").dumps(
                            decision, separators=(",", ":")
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _client_factory(binding: transport.UnderlyingClientBinding):
    return transport.Stage24AssessmentTransport(
        binding,
        credential_source_identity=object(),
        credential_identity=object(),
        credential_bytes=b"fixture-stage24-credential",
    )


def _publish_stage15_none(capture, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = capture.capture.evidence
    monkeypatch.setattr(
        "researchclaw.pipeline.stage15_critique.load_canonical_experiment_evidence",
        lambda _run_dir: SimpleNamespace(
            manifest_path=evidence.manifest_path,
            manifest_sha256=evidence.manifest_sha256,
        ),
    )
    stage = capture.run_dir / "stage-15"
    stage.mkdir()
    decision_text = b"PROCEED\n"
    structured = json.dumps(
        {
            "canonical_experiment_evidence_path": evidence.manifest_path,
            "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
            "decision_path": "stage-15/decision.md",
            "decision_sha256": hashlib.sha256(decision_text).hexdigest(),
        },
        sort_keys=True,
    ).encode()
    (stage / "decision.md").write_bytes(decision_text)
    (stage / "decision_structured.json").write_bytes(structured)
    with BoundOutputNamespace.open(
        capture.run_dir, stage, "stage-15"
    ) as namespace:
        publish_model_or_none_critique(
            namespace=namespace,
            canonical_evidence={
                "path": evidence.manifest_path,
                "sha256": evidence.manifest_sha256,
            },
            decision={
                "text_path": "stage-15/decision.md",
                "text_sha256": hashlib.sha256(decision_text).hexdigest(),
                "structured_path": "stage-15/decision_structured.json",
                "structured_sha256": hashlib.sha256(structured).hexdigest(),
            },
            writer_model="writer",
            critic_model="",
            findings=None,
            unavailability_reason="critic_not_configured",
        )


def _complete_stage24_upstream(
    capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from researchclaw.literature.evidence_cards import build_cards_manifest
    from researchclaw.literature import experiment_fact_closure
    from researchclaw.pipeline import scientific_claim_authority
    from researchclaw.pipeline import stage17_structured_publication as stage17
    from researchclaw.pipeline import stage20_structured_publication as stage20
    from researchclaw.pipeline import stage22_structured_publication as stage22
    from tests.test_stage20_structured_replay import (
        _QualityClient,
        _prepare_stage20_source,
        _quality_response,
        _run_private,
    )
    from tests.test_stage21_structured_archive import _publish as publish_stage21
    from tests.test_stage22_structured_export import (
        _patch_stage22_verifier,
        _successful_compile,
    )
    from tests.test_stage17_structured_producer_integration import _complete_cfs

    run_dir = capture.run_dir
    abstract = (
        "Prior work supplies exact governed evidence for the retained "
        "background statement in this fixture."
    )
    candidate = {
        "source_identity": "fixture-source-001",
        "abstract": abstract,
    }
    candidate_bytes = (
        json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    excerpt_text = abstract
    excerpt_sha256 = hashlib.sha256(excerpt_text.encode()).hexdigest()
    card = {
        "card_id": "card-001",
        "source_identity": candidate["source_identity"],
        "cite_key": "Smith2024",
        "evidence_excerpts": [
            {
                "excerpt_id": "excerpt-1",
                "source_type": "abstract",
                "source_artifact_path": "stage-04/candidates.jsonl",
                "source_artifact_sha256": hashlib.sha256(
                    candidate_bytes
                ).hexdigest(),
                "source_record_id": candidate["source_identity"],
                "json_pointer": "/abstract",
                "char_start": 0,
                "char_end": len(excerpt_text),
                "excerpt_text": excerpt_text,
                "excerpt_sha256": excerpt_sha256,
            }
        ],
    }
    card_bytes = publication.authority.global_canonical_json_bytes(card)
    card_markdown = b"# Fixture evidence card\n"
    cards_manifest = build_cards_manifest(
        shortlist_sha256="1" * 64,
        screening_report_sha256="2" * 64,
        cards=((card, card_bytes.decode(), card_markdown.decode()),),
    )
    cards_manifest_bytes = publication.authority.global_canonical_json_bytes(
        cards_manifest
    )
    cards_manifest_sha256 = hashlib.sha256(cards_manifest_bytes).hexdigest()

    plan_path = run_dir / "stage-16/citation_plan.json"
    allowlist_path = run_dir / "stage-06/citation_allowlist.json"
    plan = json.loads(plan_path.read_bytes())
    allowlist = json.loads(allowlist_path.read_bytes())
    plan["cards_manifest_sha256"] = cards_manifest_sha256
    allowlist["cards_manifest_sha256"] = cards_manifest_sha256
    allowlist_bytes = publication.authority.global_canonical_json_bytes(
        allowlist
    )
    plan["citation_allowlist_sha256"] = hashlib.sha256(
        allowlist_bytes
    ).hexdigest()
    plan_bytes = publication.authority.global_canonical_json_bytes(plan)
    plan_path.write_bytes(plan_bytes)
    allowlist_path.write_bytes(allowlist_bytes)
    (run_dir / "stage-04/candidates.jsonl").write_bytes(candidate_bytes)
    cards_dir = run_dir / "stage-06/cards"
    cards_dir.mkdir()
    (cards_dir / "card-001.json").write_bytes(card_bytes)
    (cards_dir / "card-001.md").write_bytes(card_markdown)
    (run_dir / "stage-06/cards_manifest.json").write_bytes(
        cards_manifest_bytes
    )

    original_capture = capture.capture
    cfs = _complete_cfs()
    cfs["bound_labels"] = {
        "benchmark_tokens": (),
        "dataset": "Trust-HUB",
        "evaluator_schema": "fixture-evaluator",
        "evaluator_id": "fixture-evaluator",
    }
    cfs["runtime"] = {
        "python": "3.11",
        "device": "cpu",
        "packages": {},
    }
    cfs["condition_aggregates"] = (
        {
            "condition": "trojnet_community_graphsage",
            "metrics": {
                "auprc": {
                    "mean": Decimal("1.25"),
                    "std": Decimal("0"),
                    "min": Decimal("1.20"),
                    "max": Decimal("1.30"),
                }
            },
        },
    )
    monkeypatch.setattr(
        scientific_claim_authority,
        "build_canonical_fact_sheet",
        lambda evidence: (
            cfs
            if evidence is original_capture.evidence
            else pytest.fail("Stage 24 fixture changed claim CFS evidence")
        ),
    )
    monkeypatch.setattr(
        experiment_fact_closure,
        "build_canonical_fact_sheet",
        lambda evidence: (
            cfs
            if evidence is original_capture.evidence
            else pytest.fail("Stage 24 fixture changed closure CFS evidence")
        ),
    )
    binding = scientific_claim_authority.build_scientific_claim_generation_binding(
        original_capture.evidence
    )
    object.__setattr__(
        original_capture.evidence,
        "cfs_sha256",
        binding.cfs_sha256,
    )
    with monkeypatch.context() as recapture_patch:
        recapture_patch.setattr(
            stage17,
            "build_scientific_claim_generation_binding",
            lambda received: (
                binding
                if received is original_capture.evidence
                else pytest.fail("Stage 24 fixture changed evidence generation")
            ),
        )
        recapture_patch.setattr(
            stage17,
            "_capture_evidence_files",
            lambda reader, received: (
                ()
                if reader is capture.lease
                and received is original_capture.evidence
                else pytest.fail("Stage 24 fixture changed writer epoch")
            ),
        )
        capture.capture = stage17.capture_structured_stage17_sources(
            capture.lease,
            original_capture.evidence,
        )

    _prepare_stage20_source(capture, monkeypatch)
    stage20_result = _run_private(
        capture,
        llm=_QualityClient([_quality_response()]),
    )
    assert stage20_result.status is StageStatus.DONE
    stage20.clear_structured_stage20_published_context(capture.lease)
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 0,
        },
    )
    _patch_stage22_verifier(monkeypatch)
    assert publish_stage21(capture).status is StageStatus.DONE
    monkeypatch.setattr(stage22, "compile_latex", _successful_compile)
    stage22_pre = stage22.issue_stage22_pre_admission_context(capture.lease)
    assert executor._execute_structured_stage22_private(
        capture.lease,
        stage22_pre,
    ).status is StageStatus.DONE

    config = stage20.parse_config_snapshot_text(
        "",
        project_root=run_dir,
        label="structured Stage 24 complete fixture config",
    )
    monkeypatch.setattr(
        stage20,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: replace(
            config,
            llm=replace(config.llm, critic_model="fixture-resolution"),
            paper_revision=replace(
                config.paper_revision,
                critic_model="fixture-assessment",
            ),
        ),
    )
    monkeypatch.setattr(
        publication,
        "build_canonical_fact_sheet",
        lambda evidence: (
            cfs
            if evidence is capture.capture.evidence
            else pytest.fail("Stage 24 fixture changed CFS evidence")
        ),
    )
    _publish_stage15_none(capture, monkeypatch)
    assert publish_stage23(capture).status is StageStatus.DONE


def test_private_stage24_real_success_and_mutation_cleanup(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_stage24_upstream(active_capture, monkeypatch)
    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        lambda _client, request: _assessment_http(request),
    )
    lifecycle: list[str] = []
    replay_count = 0
    created: list[str] = []
    original_create = publication._create_held_file
    original_replay = publication._replay_complete_authority
    original_build_fixed = publication._build_fixed_payloads
    original_immediate = (
        publication.validate_structured_stage24_immediate_postcondition
    )
    original_terminal = (
        publication.validate_structured_stage24_terminal_postcondition
    )

    def observe_create(*args, **kwargs):
        created.append(args[1])
        return original_create(*args, **kwargs)

    def observe_replay(*args, **kwargs):
        nonlocal replay_count
        replay_count += 1
        return original_replay(*args, **kwargs)

    observed_fixed = []

    def observe_build_fixed(*args, **kwargs):
        result = original_build_fixed(*args, **kwargs)
        observed_fixed.append(result)
        return result

    def observe_immediate(*args, **kwargs):
        lifecycle.append("immediate")
        return original_immediate(*args, **kwargs)

    def observe_terminal(*args, **kwargs):
        lifecycle.append("terminal")
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(publication, "_create_held_file", observe_create)
    monkeypatch.setattr(publication, "_replay_complete_authority", observe_replay)
    monkeypatch.setattr(publication, "_build_fixed_payloads", observe_build_fixed)
    monkeypatch.setattr(
        publication,
        "validate_structured_stage24_immediate_postcondition",
        observe_immediate,
    )
    monkeypatch.setattr(
        publication,
        "validate_structured_stage24_terminal_postcondition",
        observe_terminal,
    )
    clients = {role: _client_factory for role in transport.ROLE_ORDER}
    pre = publication.issue_stage24_pre_admission_context(
        active_capture.lease,
        clients=clients,
    )
    success = executor._execute_structured_stage24_private(
        active_capture.lease,
        pre,
    )
    assert success.status is StageStatus.DONE, (
        json.dumps(
            [
                row
                for row in json.loads(observed_fixed[-1][0]["claims.json"])[
                    "claims"
                ]
                if row["status"] == "unsupported"
            ],
            sort_keys=True,
        )
        if observed_fixed
        else success.error
    )
    assert success.artifacts == publication.STRUCTURED_STAGE24_ARTIFACTS
    assert success.evidence_refs == publication.STRUCTURED_STAGE24_EVIDENCE_REFS
    assert success.decision == "structured-scientific-claim-v1"
    assert created[-1] == "stage24_truth_manifest.json"
    assert lifecycle == ["immediate", "terminal"]
    assert replay_count >= 3
    claims = json.loads(observed_fixed[-1][0]["claims.json"])
    paper = (
        active_capture.run_dir / "stage-23/paper_final_verified.md"
    ).read_bytes()
    rows_by_text = {
        paper[row["byte_start"] : row["byte_end"]].decode(): row
        for row in claims["claims"]
        if row["kind"] == "declarative_sentence"
    }
    for prefix in (
        "The primary result uses ",
        "Interpretation of the primary AUPRC result ",
    ):
        row = next(
            value
            for text, value in rows_by_text.items()
            if text.startswith(prefix)
        )
        assert row["support_required"] is True
        assert row["status"] == "supported"
        assert row["support_record_id"] is None
    overlap = next(
        value
        for text, value in rows_by_text.items()
        if text.startswith("For primary condition ")
    )
    assert overlap["status"] == "supported"
    assert isinstance(overlap["support_record_id"], str)
    assert claims["counts"]["unsupported"] == 0
    manifest_payload = json.loads(
        (
            active_capture.run_dir
            / "stage-24/stage24_truth_manifest.json"
        ).read_bytes()
    )
    assert manifest_payload["assessment_counts"][
        "generic_support_assessment"
    ] == 1

    original_terminal_for_failure = (
        publication.validate_structured_stage24_terminal_postcondition
    )

    def mutate_before_terminal(lease, provisional, **kwargs):
        claims_path = active_capture.run_dir / "stage-24/claims.json"
        claims_path.write_bytes(claims_path.read_bytes() + b" ")
        return original_terminal_for_failure(lease, provisional, **kwargs)

    monkeypatch.setattr(
        publication,
        "validate_structured_stage24_terminal_postcondition",
        mutate_before_terminal,
    )
    pre = publication.issue_stage24_pre_admission_context(
        active_capture.lease,
        clients=clients,
    )
    failed = executor._execute_structured_stage24_private(
        active_capture.lease,
        pre,
    )
    assert failed.status is StageStatus.FAILED
    assert failed.decision == "retry"
    assert failed.artifacts == ()
    assert failed.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-24/stage24_truth_manifest.json"
    ).exists()
    assert not (active_capture.run_dir / "stage-24").exists()


def test_comparison_glue_ignores_identifier_metadata_numeric_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = (
        b"# Study\n\n## Results\n\n"
        b'F1 90% is higher than baseline '
        b'<span data-rank="2"></span>F1 80%.\n'
    )
    obligations = build_claim_obligation_inventory(paper)
    comparison = next(
        item for item in obligations if item.kind == "comparative_sentence"
    )
    numeric = tuple(
        item
        for item in obligations
        if item.kind == "numeric_token"
        and comparison.byte_start <= item.byte_start
        and item.byte_end <= comparison.byte_end
    )
    claim_numeric = tuple(
        item
        for item in numeric
        if item.kind_payload["numeric_role"] == "claim_numeric"
    )
    identifier = next(
        item
        for item in numeric
        if item.kind_payload["numeric_role"] == "identifier_metadata"
    )
    assert len(claim_numeric) == 2
    numeric_state = {}
    for item, value, label in zip(
        claim_numeric,
        ("0.9", "0.8"),
        ("F1", 'baseline <span data-rank="2"></span>F1'),
        strict=True,
    ):
        numeric_state[item.obligation_id] = {
            "status": "supported",
            "authority": {
                "cfs": {"schema_version": 1, "sha256": "a" * 64},
                "metric": "detection_f1",
                "display_label": label,
                "semantic_pointer": f"/{item.obligation_id}",
                "semantic_value_sha256": hashlib.sha256(
                    value.encode()
                ).hexdigest(),
                "canonical_value": value,
                "unit": "ratio",
                "transform": "unitless-identity-v1",
            },
        }
    numeric_state[identifier.obligation_id] = {"status": "not_required"}
    generic_records = {
        item.obligation_id: {
            "verdict": "supported",
            "assessment_id": hashlib.sha256(
                item.obligation_id.encode()
            ).hexdigest(),
        }
        for item in obligations
        if item.kind == "declarative_sentence"
    }
    observed_children = ()
    original = publication.authority.evaluate_comparative_truth

    def observe_children(**kwargs):
        nonlocal observed_children
        observed_children = kwargs["numeric_children"]
        return original(**kwargs)

    monkeypatch.setattr(
        publication.authority,
        "evaluate_comparative_truth",
        observe_children,
    )
    monkeypatch.setattr(
        publication,
        "_renderer_occurrences",
        lambda *_args, **_kwargs: (),
    )
    paper_artifact = SimpleNamespace(
        path="stage-23/paper_final_verified.md",
        content=paper,
        sha256=hashlib.sha256(paper).hexdigest(),
        text=lambda: paper.decode(),
    )
    capture = SimpleNamespace(
        generic_bundle=SimpleNamespace(
            paper=paper_artifact,
            dataset_origin="public",
            critique_publication=SimpleNamespace(
                critique={"findings": []},
            ),
            critique=SimpleNamespace(path="stage-15/critique.json", sha256="a" * 64),
        ),
        obligations=obligations,
        source_files=(
            SimpleNamespace(
                logical_path="stage-23/paper_final_verified.md",
                identity=(1, 2),
            ),
        ),
        base=object(),
        renderer_occurrences=(),
        deterministic_declarative_ids=(),
        generic_universe=tuple(
            item for item in obligations if item.kind == "declarative_sentence"
        ),
        numeric=numeric_state,
        stage23_manifest={
            "quality_outcome": "passed",
            "generation_binding_sha256": "b" * 64,
            "cfs": {"schema_version": 1, "sha256": "c" * 64},
            "degradation_signal": None,
            "claim_scope": "pipeline_validation",
        },
        source_map={
            "stage-23/stage23_verification_manifest.json": SimpleNamespace(
                path="stage-23/stage23_verification_manifest.json",
                sha256="d" * 64,
                content=b"{}",
            )
        },
        input_bundle_sha256="e" * 64,
    )
    fixed, success, _outcome = publication._build_fixed_payloads(
        capture,
        citation_records={},
        generic_records=generic_records,
        resolution_records={},
    )
    claims = json.loads(fixed["claims.json"])
    comparison_row = next(
        row
        for row in claims["claims"]
        if row["obligation_id"] == comparison.obligation_id
    )
    assert tuple(item.obligation_id for item in observed_children) == tuple(
        item.obligation_id for item in claim_numeric
    )
    assert comparison_row["status"] == "supported"
    assert comparison_row["support_record_id"] is None
    assert success is True

    parent = next(
        item for item in obligations if item.kind == "declarative_sentence"
    )
    deterministic = _renderer_occurrence(
        paper[parent.byte_start : parent.byte_end],
        byte_start=parent.byte_start,
    )
    monkeypatch.setattr(
        publication,
        "_renderer_occurrences",
        lambda *_args, **_kwargs: (deterministic,),
    )
    failed_numeric = dict(numeric_state)
    failed_numeric[claim_numeric[0].obligation_id] = {"status": "unsupported"}
    capture_fields = vars(capture).copy()
    capture_fields.update(
        numeric=failed_numeric,
        renderer_occurrences=(deterministic,),
        deterministic_declarative_ids=(parent.obligation_id,),
        generic_universe=(),
    )
    failed_capture = SimpleNamespace(**capture_fields)
    failed_fixed, failed_success, _failed_outcome = publication._build_fixed_payloads(
        failed_capture,
        citation_records={},
        generic_records={},
        resolution_records={},
    )
    failed_claims = json.loads(failed_fixed["claims.json"])
    failed_rows = {
        row["obligation_id"]: row for row in failed_claims["claims"]
    }
    assert failed_rows[parent.obligation_id]["status"] == "supported"
    assert failed_rows[claim_numeric[0].obligation_id]["status"] == "unsupported"
    assert failed_rows[comparison.obligation_id]["status"] == "unsupported"
    assert failed_claims["counts"]["unsupported"] >= 2
    assert failed_success is False


def test_private_stage24_rejects_incomplete_legacy_fixture_before_namespace(
    passed_stage22,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.pipeline import stage20_structured_publication as stage20

    config = stage20.parse_config_snapshot_text(
        "",
        project_root=passed_stage22.run_dir,
        label="structured Stage 24 fixture config",
    )
    monkeypatch.setattr(
        stage20,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: replace(
            config,
            llm=replace(config.llm, critic_model="fixture-resolution"),
            paper_revision=replace(
                config.paper_revision,
                critic_model="fixture-assessment",
            ),
        ),
    )
    _publish_stage15_none(passed_stage22, monkeypatch)
    assert publish_stage23(passed_stage22).status is StageStatus.DONE
    clients = {role: _client_factory for role in transport.ROLE_ORDER}
    with pytest.raises(
        publication.StructuredStage24PublicationError,
        match="stage-04/candidates.jsonl",
    ):
        publication.issue_stage24_pre_admission_context(
            passed_stage22.lease,
            clients=clients,
        )
    assert not (passed_stage22.run_dir / "stage-24").exists()
