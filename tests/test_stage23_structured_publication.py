"""B5-A3 inactive structured Stage 23 held-fd publication lifecycle."""

from __future__ import annotations

import copy
from dataclasses import replace
import gc
import hashlib
import inspect
import json
import os
import pickle
from pathlib import Path
import stat
from types import SimpleNamespace
import weakref

import pytest

from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import stage22_structured_publication as stage22
from researchclaw.pipeline import stage23_structured_authority as authority
from researchclaw.pipeline import stage23_structured_publication as publication
from researchclaw.pipeline import stage23_structured_transport as transport
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage22_structured_export import (
    _patch_stage22_verifier,
    _successful_compile,
)
from tests.test_stage21_structured_archive import _publish as publish_stage21
from tests.test_stage21_structured_archive import passed_stage20
from tests.test_stage17_structured_producer_integration import active_capture


@pytest.fixture
def passed_stage22(passed_stage20, monkeypatch: pytest.MonkeyPatch):
    _patch_stage22_verifier(monkeypatch)
    assert publish_stage21(passed_stage20).status is StageStatus.DONE
    monkeypatch.setattr(stage22, "compile_latex", _successful_compile)
    pre = stage22.issue_stage22_pre_admission_context(passed_stage20.lease)
    result = pipeline_executor._execute_structured_stage22_private(
        passed_stage20.lease, pre
    )
    assert result.status is StageStatus.DONE
    return passed_stage20


def _metadata_outbound(request: transport.WireRequest):
    assert request.origin == "https://api.openalex.org"
    body = json.dumps(
        {
            "meta": {"count": 1},
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "doi": None,
                    "title": "Fixture reference",
                    "publication_year": 2024,
                    "ids": {},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body,
    )


def _publish(capture):
    pre = publication.issue_stage23_pre_admission_context(
        capture.lease,
        llm=None,
    )
    return pipeline_executor._execute_structured_stage23_private(
        capture.lease,
        pre,
        metadata_outbound=_metadata_outbound,
        relevance_outbound=None,
    )


def test_b5a3_keeps_capability_exactly_1110() -> None:
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }


def test_public_direct_guard_is_before_lock_path_provider_and_generic_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def forbidden(*_args, **_kwargs):
        touched.append("io")
        raise AssertionError("guard was not first")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden)
    monkeypatch.setattr(publication, "_capture_upstream", forbidden)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.execute_structured_stage23_verification(
            tmp_path,
            tmp_path / "stage-23",
            llm=None,
            metadata_outbound=forbidden,
            relevance_outbound=forbidden,
        )
    assert touched == []
    from researchclaw.pipeline.stage_impls._review_publish import (
        _execute_citation_verify,
    )

    assert tuple(inspect.signature(_execute_citation_verify).parameters) == (
        "stage_dir",
        "run_dir",
        "config",
        "adapters",
        "llm",
        "prompts",
    )


def test_contexts_are_code_issued_noncopyable_and_single_use(passed_stage22) -> None:
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease,
        llm=None,
    )
    assert publication.context_phase(pre) == "pre_admission"
    for operation in (
        lambda: copy.copy(pre),
        lambda: copy.deepcopy(pre),
        lambda: pickle.dumps(pre),
    ):
        with pytest.raises(TypeError):
            operation()
    attempt = publication.transition_stage23_pre_admission_context(
        passed_stage22.lease,
        pre,
    )
    assert publication.context_phase(attempt) == "namespace_bound"
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease,
            pre,
        )
    publication.fail_structured_stage23_attempt(passed_stage22.lease, attempt)


def test_active_client_factory_copy_wrong_epoch_and_config_fail_closed(
    passed_stage22,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = stage22._capture_upstream(passed_stage22.lease)
    try:
        original = captured.semantic_inputs.canonical_config
    finally:
        captured.close()
    monkeypatch.setenv(original.llm.api_key_env, "fake-env-credential")
    with pytest.raises(
        publication.StructuredStage23PublicationError,
        match="canonical Stage 23 credential is invalid",
    ):
        publication.create_and_register_stage23_active_client(
            passed_stage22.lease
        )
    canonical = replace(
        original,
        llm=replace(
            original.llm,
            api_key="fake-stage23-credential",
            fallback_models=(),
        ),
    )
    parsed_keys: list[str] = []

    def fresh_config(*_args, **_kwargs):
        key = canonical.llm.api_key.encode("ascii").decode("ascii")
        parsed_keys.append(key)
        return replace(canonical, llm=replace(canonical.llm, api_key=key))

    monkeypatch.setattr(
        stage22.stage21.stage20,
        "parse_config_snapshot_text",
        fresh_config,
    )
    client = publication.create_and_register_stage23_active_client(
        passed_stage22.lease
    )
    fake = LLMClient(client.config)
    failed = pipeline_executor._execute_structured_stage23_with_pre_admission_private(
        passed_stage22.lease,
        llm=fake,
        metadata_outbound=_metadata_outbound,
        relevance_outbound=None,
    )
    assert failed.status is StageStatus.FAILED
    assert failed.decision == "retry"
    assert failed.artifacts == failed.evidence_refs == ()
    assert "fake-stage23-credential" not in (failed.error or "")
    assert not (passed_stage22.run_dir / "stage-23").exists()

    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=client
    )
    assert len(parsed_keys) >= 2
    assert parsed_keys[-1] is not parsed_keys[-2]
    attempt = publication.transition_stage23_pre_admission_context(
        passed_stage22.lease, pre
    )

    for rejected in (fake, copy.copy(client)):
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="registration mismatch",
        ):
            publication.issue_stage23_pre_admission_context(
                passed_stage22.lease, llm=rejected
            )

    registration = publication._ACTIVE_CLIENTS[client]
    wrong_registrations = (
        replace(registration, run_path=registration.run_path + "-wrong"),
        replace(
            registration,
            run_identity=(
                registration.run_identity[0],
                registration.run_identity[1] + 1,
            ),
        ),
        replace(registration, owner=object()),
    )
    for wrong in wrong_registrations:
        publication._ACTIVE_CLIENTS[client] = wrong
        try:
            with pytest.raises(
                publication.StructuredStage23PublicationError,
                match="registration mismatch",
            ):
                publication.issue_stage23_pre_admission_context(
                    passed_stage22.lease, llm=client
                )
        finally:
            publication._ACTIVE_CLIENTS[client] = registration

    original_model = client.config.primary_model
    client.config.primary_model = "wrong-model"
    try:
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="configuration mismatch",
        ):
            publication.issue_stage23_pre_admission_context(
                passed_stage22.lease, llm=client
            )
    finally:
        client.config.primary_model = original_model

    original_key = client.config.api_key
    client.config.api_key = "fake-replaced-credential"
    try:
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="registration mismatch",
        ):
            publication.issue_stage23_pre_admission_context(
                passed_stage22.lease, llm=client
            )
    finally:
        client.config.api_key = original_key
    publication.fail_structured_stage23_attempt(passed_stage22.lease, attempt)


def test_active_client_duplicate_factory_and_attempt_clear_release_registration(
    passed_stage22,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = stage22._capture_upstream(passed_stage22.lease)
    try:
        original = captured.semantic_inputs.canonical_config
    finally:
        captured.close()
    canonical = replace(
        original,
        llm=replace(
            original.llm,
            api_key="fake-stage23-credential",
            fallback_models=(),
        ),
    )
    monkeypatch.setattr(
        stage22.stage21.stage20,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: canonical,
    )
    client = publication.create_and_register_stage23_active_client(
        passed_stage22.lease
    )
    with pytest.raises(
        publication.StructuredStage23PublicationError,
        match="already registered",
    ):
        publication.create_and_register_stage23_active_client(
            passed_stage22.lease
        )
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=client
    )
    spec = publication._PRE_CONTEXTS[pre].capture.relevance_spec
    assert spec is not None
    assert publication._client_for_spec(spec, passed_stage22.lease) is client
    unbound = transport.Stage23RelevanceTransportSpec.issue_for_test(
        provider=spec.provider,
        model=spec.model,
        base_url=spec.base_url,
        credential=b"fake-stage23-credential",
    )
    with pytest.raises(
        publication.StructuredStage23PublicationError,
        match="registration",
    ):
        publication._client_for_spec(unbound, passed_stage22.lease)
    attempt = publication.transition_stage23_pre_admission_context(
        passed_stage22.lease, pre
    )
    publication.fail_structured_stage23_attempt(passed_stage22.lease, attempt)
    assert client not in publication._ACTIVE_CLIENTS
    assert not publication._ACTIVE_REGISTRATIONS
    assert not publication._SPEC_REGISTRATIONS


def test_active_client_registry_does_not_keep_client_or_credential_alive(
    passed_stage22,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = stage22._capture_upstream(passed_stage22.lease)
    try:
        original = captured.semantic_inputs.canonical_config
    finally:
        captured.close()
    canonical = replace(
        original,
        llm=replace(
            original.llm,
            api_key="fake-stage23-credential",
            fallback_models=(),
        ),
    )
    monkeypatch.setattr(
        stage22.stage21.stage20,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: canonical,
    )
    client = publication.create_and_register_stage23_active_client(
        passed_stage22.lease
    )
    client_ref = weakref.ref(client)
    del client
    gc.collect()
    assert client_ref() is None
    assert not publication._ACTIVE_CLIENTS
    assert not publication._ACTIVE_REGISTRATIONS


def test_same_config_cross_run_specs_bind_exact_registration(tmp_path: Path) -> None:
    class FakeOwner:
        def __init__(self, path: Path, identity: tuple[int, int]) -> None:
            self.run_dir = path
            self._run_identity = identity
            self._active = object()

        def _require_active(self):
            return self._active

    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openai",
            base_url="https://api.example/v1",
            wire_api="chat_completions",
            primary_model="model-1",
            fallback_models=(),
        )
    )
    owner_a = FakeOwner(tmp_path / "run-a", (1, 11))
    owner_b = FakeOwner(tmp_path / "run-b", (1, 12))
    client_a = LLMClient(
        LLMConfig(
            base_url=config.llm.base_url,
            api_key="fake-a",
            primary_model=config.llm.primary_model,
            fallback_models=[],
        )
    )
    client_b = LLMClient(
        LLMConfig(
            base_url=config.llm.base_url,
            api_key="fake-b",
            primary_model=config.llm.primary_model,
            fallback_models=[],
        )
    )
    publication._register_active_client(
        owner_a, client_a, config, credential=b"fake-a"
    )
    publication._register_active_client(
        owner_b, client_b, config, credential=b"fake-b"
    )
    record_a = publication._ACTIVE_CLIENTS[client_a]
    record_b = publication._ACTIVE_CLIENTS[client_b]
    spec_a = transport.Stage23RelevanceTransportSpec.issue_for_test(
        provider="openai",
        model="model-1",
        base_url="https://api.example/v1",
        credential=b"fake-a",
    )
    spec_b = transport.Stage23RelevanceTransportSpec.issue_for_test(
        provider="openai",
        model="model-1",
        base_url="https://api.example/v1",
        credential=b"fake-b",
    )
    publication._SPEC_REGISTRATIONS[spec_a] = record_a.registration_identity
    publication._SPEC_REGISTRATIONS[spec_b] = record_b.registration_identity
    try:
        assert publication._client_for_spec(spec_a, owner_a) is client_a
        assert publication._client_for_spec(spec_b, owner_b) is client_b
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="registration mismatch",
        ):
            publication._client_for_spec(spec_a, owner_b)
        copied_spec = copy.copy(spec_a)
        assert copied_spec is not spec_a
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="registration mismatch",
        ):
            publication._client_for_spec(copied_spec, owner_a)
        active_epoch = owner_a._active
        owner_a._active = object()
        try:
            with pytest.raises(
                publication.StructuredStage23PublicationError,
                match="registration mismatch",
            ):
                publication._client_for_spec(spec_a, owner_a)
        finally:
            owner_a._active = active_epoch
        owner_a._active = object()
        publication._require_no_active_registration(owner_a)
        assert client_a not in publication._ACTIVE_CLIENTS
        with pytest.raises(
            publication.StructuredStage23PublicationError,
            match="registration mismatch",
        ):
            publication._client_for_spec(spec_a, owner_a)
        replacement_client = LLMClient(
            LLMConfig(
                base_url=config.llm.base_url,
                api_key="fake-replacement",
                primary_model=config.llm.primary_model,
                fallback_models=[],
            )
        )
        publication._register_active_client(
            owner_a,
            replacement_client,
            config,
            credential=b"fake-replacement",
        )
        replacement_record = publication._ACTIVE_CLIENTS[replacement_client]
        replaced_run = FakeOwner(owner_a.run_dir, (1, 99))
        publication._require_no_active_registration(replaced_run)
        assert replacement_client not in publication._ACTIVE_CLIENTS
        assert (
            replacement_record.registration_identity
            not in publication._ACTIVE_REGISTRATIONS
        )
    finally:
        publication._drop_active_registration(record_a.registration_identity)
        publication._drop_active_registration(record_b.registration_identity)


def test_stage23_initially_absent_rejects_directory_symlink_fifo_and_preserves(
    passed_stage22,
) -> None:
    run_dir = passed_stage22.run_dir
    path = run_dir / "stage-23"
    path.mkdir()
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease, pre
        )
    assert path.is_dir() and tuple(path.iterdir()) == ()
    path.rmdir()

    outside = run_dir / "outside-stage23"
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease, pre
        )
    assert path.is_symlink() and tuple(outside.iterdir()) == ()
    path.unlink()

    os.mkfifo(path)
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease, pre
        )
    assert stat.S_ISFIFO(path.lstat().st_mode)
    path.unlink()

    source = run_dir / "preexisting-special-source"
    source.write_bytes(b"preserve")
    os.link(source, path)
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease, pre
        )
    assert path.read_bytes() == source.read_bytes() == b"preserve"
    path.unlink()
    source.unlink()

    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    moved = run_dir.with_name(run_dir.name + "-replaced")
    run_dir.rename(moved)
    run_dir.mkdir()
    try:
        with pytest.raises(Exception, match="changed|mismatch"):
            publication.transition_stage23_pre_admission_context(
                passed_stage22.lease, pre
            )
    finally:
        run_dir.rmdir()
        moved.rename(run_dir)

    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    archive = run_dir / "stage-21/archive.md"
    archive.write_bytes(archive.read_bytes() + b"\nwithdrawn generation\n")
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.transition_stage23_pre_admission_context(
            passed_stage22.lease, pre
        )
    assert not (run_dir / "stage-23").exists()


def test_pipeline_validation_missing_relevance_publishes_exact_degraded_four_tuple(
    passed_stage22,
) -> None:
    result = _publish(passed_stage22)
    assert result.status is StageStatus.DONE
    assert result.decision == "structured-scientific-claim-v1"
    assert result.artifacts == authority.STRUCTURED_STAGE23_ARTIFACTS
    assert result.evidence_refs == authority.STRUCTURED_STAGE23_EVIDENCE_REFS
    assert result.degraded is True
    stage_dir = passed_stage22.run_dir / "stage-23"
    assert tuple(sorted(path.name for path in stage_dir.iterdir())) == tuple(
        sorted(authority.STRUCTURED_STAGE23_ARTIFACTS)
    )
    manifest = json.loads(
        (stage_dir / "stage23_verification_manifest.json").read_text()
    )
    assert tuple(manifest) == authority.MANIFEST_FIELDS
    assert manifest["schema_version"] == 2
    assert manifest["output_count"] == 3
    assert [row["role"] for row in manifest["outputs"]] == [
        "verification_report",
        "verified_bibliography",
        "verified_paper",
    ]
    report = json.loads((stage_dir / "verification_report.json").read_text())
    assert tuple(report) == authority.REPORT_FIELDS
    assert tuple(report["relevance"]) == authority.RELEVANCE_FIELDS
    assert report["relevance"]["status"] == "missing"
    assert report["outcome"] == "degraded"
    assert (stage_dir / "paper_final_verified.md").read_bytes() == (
        passed_stage22.run_dir / "stage-22/paper_final.md"
    ).read_bytes()


def test_immediate_and_terminal_late_mutation_withdraw_authority(
    passed_stage22,
) -> None:
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    attempt = publication.transition_stage23_pre_admission_context(
        passed_stage22.lease, pre
    )
    provisional = publication.produce_structured_stage23(
        passed_stage22.lease,
        attempt,
        metadata_outbound=_metadata_outbound,
        relevance_outbound=None,
    )
    report = passed_stage22.run_dir / "stage-23/verification_report.json"
    report.write_bytes(report.read_bytes() + b" ")
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.validate_structured_stage23_immediate_postcondition(
            passed_stage22.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    assert publication.fail_structured_stage23_attempt(
        passed_stage22.lease, attempt
    ) == ()
    assert not (passed_stage22.run_dir / "stage-23").exists()

    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )
    attempt = publication.transition_stage23_pre_admission_context(
        passed_stage22.lease, pre
    )
    provisional = publication.produce_structured_stage23(
        passed_stage22.lease,
        attempt,
        metadata_outbound=_metadata_outbound,
        relevance_outbound=None,
    )
    publication.validate_structured_stage23_immediate_postcondition(
        passed_stage22.lease,
        provisional,
        artifacts=provisional.artifacts,
        evidence_refs=provisional.evidence_refs,
        context=attempt,
    )
    report = passed_stage22.run_dir / "stage-23/verification_report.json"
    report.unlink()
    report.write_bytes(b"replacement-collision")
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.validate_structured_stage23_terminal_postcondition(
            passed_stage22.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    cleanup_errors = publication.fail_structured_stage23_attempt(
        passed_stage22.lease, attempt
    )
    assert cleanup_errors
    assert report.read_bytes() == b"replacement-collision"


def test_private_executor_failure_is_retry_empty_and_manifest_first_cleanup(
    passed_stage22,
) -> None:
    pre = publication.issue_stage23_pre_admission_context(
        passed_stage22.lease, llm=None
    )

    def failing(_request):
        raise transport.PureTransportError("offline")

    result = pipeline_executor._execute_structured_stage23_private(
        passed_stage22.lease,
        pre,
        metadata_outbound=failing,
        relevance_outbound=None,
    )
    assert result.stage is Stage.CITATION_VERIFY
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (passed_stage22.run_dir / "stage-23").exists()


def test_pre_admission_rejects_cited_closure_before_stage23_path_io(
    passed_stage22,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden(*_args, **_kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("Stage 23 path I/O occurred")

    monkeypatch.setattr(publication, "_acquire_structured_stage23_namespace", forbidden)
    paper = passed_stage22.run_dir / "stage-22/paper_final.md"
    original = paper.read_bytes()
    assert b"[Smith2024]" in original
    paper.write_bytes(original.replace(b"[Smith2024]", b""))
    with pytest.raises(publication.StructuredStage23PublicationError):
        publication.issue_stage23_pre_admission_context(
            passed_stage22.lease, llm=None
        )
    assert touched is False

    paper.write_bytes(original + b"\nforged synchronized payload\n")
    manifest_path = (
        passed_stage22.run_dir / "stage-22/stage22_export_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    for row in manifest["outputs"]:
        if row["path"] == "stage-22/paper_final.md":
            forged = paper.read_bytes()
            row["sha256"] = hashlib.sha256(forged).hexdigest()
            row["size"] = len(forged)
    manifest_path.write_bytes(stage22._canonical_json(manifest))
    with pytest.raises(
        publication.StructuredStage23PublicationError,
        match="independently rebuilt output changed",
    ):
        publication.issue_stage23_pre_admission_context(
            passed_stage22.lease, llm=None
        )
    assert touched is False
