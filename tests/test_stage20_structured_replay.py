"""B4-B inactive structured Stage 20 replay and publication contracts."""

from __future__ import annotations

import copy
import hashlib
import http.client
import inspect
import json
import os
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMResponse
from researchclaw.llm import anthropic_adapter
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import stage20_structured_authority as authority
from researchclaw.pipeline import stage20_structured_publication as publication
from researchclaw.pipeline import stage20_structured_transport as transport
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stage20_publication import Stage20FabricationState
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage17_structured_producer_integration import active_capture
from tests.test_stage19_structured_revision import (
    _prepare_stage19_sources,
    _publish_verified,
)


def test_b4b_keeps_capability_exactly_1000() -> None:
    assert capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES == {
        "stage17_publication": 1,
        "stage19_revision": 0,
        "stage20_replay": 0,
        "stage24_and_release_integration": 0,
    }


def test_structured_stage20_owns_exact_success_names() -> None:
    assert publication.STRUCTURED_STAGE20_OUTPUTS == (
        "quality_report.json",
        "fabrication_flags.json",
    )
    assert publication.STRUCTURED_STAGE20_MANIFEST == "quality_gate_manifest.json"
    assert publication.STRUCTURED_STAGE20_STAGE_TEMPS == (
        "quality_report.json.tmp",
        "fabrication_flags.json.tmp",
        "quality_gate_manifest.json.tmp",
    )
    assert (
        publication.STRUCTURED_STAGE20_ROOT_TEMP
        == "degradation_signal.json.tmp"
    )
    assert publication.STRUCTURED_STAGE20_ARTIFACTS == (
        "quality_report.json",
        "fabrication_flags.json",
        "quality_gate_manifest.json",
    )
    assert publication.STRUCTURED_STAGE20_EVIDENCE_REFS == (
        "stage-20/quality_report.json",
        "stage-20/fabrication_flags.json",
        "stage-20/quality_gate_manifest.json",
    )


def test_public_entry_has_no_caller_authority() -> None:
    parameters = inspect.signature(
        publication.execute_structured_stage20_quality
    ).parameters
    assert tuple(parameters) == ("run_dir", "stage_dir", "llm")


def test_public_entry_rejects_1000_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_lock(*_args: object, **_kwargs: object) -> None:
        nonlocal touched
        touched = True
        raise AssertionError("structured Stage 20 touched I/O")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.execute_structured_stage20_quality(
            tmp_path / "missing-run",
            tmp_path / "missing-run" / "stage-20",
            None,
        )
    assert touched is False


@pytest.mark.parametrize(
    "malformed",
    (
        {
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": False,
            "stage24_and_release_integration": 0,
        },
        {
            "stage17_publication": 1,
            "stage19_revision": 0,
            "stage20_replay": 0,
        },
    ),
)
def test_public_entry_rejects_malformed_capability_before_io(
    malformed: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    touched = False

    def forbidden_lock(*_args: object, **_kwargs: object) -> None:
        nonlocal touched
        touched = True
        raise AssertionError("malformed capability touched I/O")

    monkeypatch.setattr(
        capability, "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES", malformed
    )
    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        publication.execute_structured_stage20_quality(
            tmp_path / "missing-run",
            tmp_path / "missing-run/stage-20",
            None,
        )
    assert touched is False


def test_structured_stage20_schema_versions_are_frozen() -> None:
    assert authority.QUALITY_REPORT_SCHEMA_VERSION == 1
    assert authority.FABRICATION_FLAGS_SCHEMA_VERSION == 3
    assert authority.QUALITY_GATE_MANIFEST_SCHEMA_VERSION == 2
    assert authority.DEGRADATION_SIGNAL_SCHEMA_VERSION == 1


class _QualityClient:
    def __init__(self, responses: list[object]) -> None:
        self.config = SimpleNamespace(
            fallback_url="",
            fallback_api_key="",
            fallback_models=[],
            primary_model="fixture-quality",
            base_url="https://fixture.invalid/v1",
            api_key="credential-a",
            extra_headers={},
            timeout_sec=30,
            user_agent="fixture-agent",
        )
        self.responses = responses
        self.requests: list[tuple[object, ...]] = []

    def _endpoint_url(self, base_url: str) -> str:
        return base_url + "/chat/completions"

    def _raw_call(self, *args: object, **kwargs: object) -> LLMResponse:
        self.requests.append((*args, tuple(sorted(kwargs.items()))))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, LLMResponse)
        return response


def _quality_response(*, score: float = 8.0, verdict: str = "proceed") -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "score_1_to_10": score,
                "verdict": verdict,
                "strengths": ["bounded evidence"],
                "weaknesses": ["limited scope"],
                "required_actions": ["retain the governed boundary"],
            },
            separators=(",", ":"),
        ),
        model="fixture-quality",
    )


def _prepare_stage20_source(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> RCConfig:
    _prepare_stage19_sources(active_capture, monkeypatch)
    with active_capture.lease.open_stage_namespace("stage-19") as namespace:
        _publish_verified(active_capture.lease, namespace=namespace, llm=None)
    config = RCConfig.load(
        Path(__file__).parent.parent / "config.researchclaw.example.yaml",
        check_paths=False,
    )
    monkeypatch.setattr(
        publication,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        publication,
        "reconstruct_stage20_fabrication_state",
        lambda *_args, **_kwargs: Stage20FabricationState(
            experiment_failed=False,
            real_metric_values=("1.25",),
            verified_values_count=1,
            verified_conditions=("primary",),
            has_real_data=True,
            fabrication_suspected=False,
        ),
    )
    active_capture.lease.ensure_run_directory("stage-20")
    return config


def _run_private(
    active_capture,
    *,
    llm,
):
    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        try:
            snapshot, context = (
                publication._capture_snapshot_a_and_issue_context(
                    active_capture.lease, namespace=namespace
                )
            )
        except publication.StructuredStage20PublicationError as exc:
            return StageResult(
                stage=Stage.QUALITY_GATE,
                status=StageStatus.FAILED,
                artifacts=(),
                evidence_refs=(),
                error=f"Structured Stage 20 capture failed: {exc}",
                decision="retry",
            )
        return publication._execute_structured_stage20_from_context(
            active_capture.lease,
            namespace=namespace,
            snapshot=snapshot,
            context=context,
            llm=llm,
        )


def test_private_passed_publication_has_exact_schemas_and_tuples(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    client = _QualityClient([_quality_response()])

    result = _run_private(active_capture, llm=client)

    assert result.status is StageStatus.DONE
    assert result.decision == "proceed"
    assert result.artifacts == publication.STRUCTURED_STAGE20_ARTIFACTS
    assert result.evidence_refs == publication.STRUCTURED_STAGE20_EVIDENCE_REFS
    assert len(client.requests) == 1
    stage20 = active_capture.run_dir / "stage-20"
    report = json.loads((stage20 / "quality_report.json").read_bytes())
    flags = json.loads((stage20 / "fabrication_flags.json").read_bytes())
    manifest = json.loads((stage20 / "quality_gate_manifest.json").read_bytes())
    assert report["schema_version"] == 1
    assert flags["schema_version"] == 3
    assert manifest["schema_version"] == 2
    assert manifest["outcome"] == "passed"
    assert manifest["degradation_signal"] is None
    assert not (active_capture.run_dir / "degradation_signal.json").exists()
    publication.validate_structured_stage20_executor_postcondition(
        active_capture.lease,
        artifacts=result.artifacts,
        evidence_refs=result.evidence_refs,
        decision=result.decision,
    )
    publication.clear_structured_stage20_published_context(active_capture.lease)


def test_llm_none_still_replays_then_fails_with_empty_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    stage20 = active_capture.run_dir / "stage-20"
    for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
        (stage20 / name).write_text("stale generic bytes", encoding="utf-8")
    (active_capture.run_dir / "degradation_signal.json").write_text(
        "stale generic signal", encoding="utf-8"
    )

    result = _run_private(active_capture, llm=None)

    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (active_capture.run_dir / "degradation_signal.json").exists()
    for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
        assert not (stage20 / name).exists()


def test_source_mutation_after_snapshot_has_zero_model_calls(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    client = _QualityClient([_quality_response()])
    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        snapshot, context = publication._capture_snapshot_a_and_issue_context(
            active_capture.lease, namespace=namespace
        )
        source = (
            active_capture.run_dir
            / "stage-19/scientific_claim_paper_revised.md"
        )
        source.write_bytes(source.read_bytes() + b"\nlate mutation\n")
        result = publication._execute_structured_stage20_from_context(
            active_capture.lease,
            namespace=namespace,
            snapshot=snapshot,
            context=context,
            llm=client,
        )
    assert result.status is StageStatus.FAILED
    assert client.requests == []
    assert result.artifacts == ()
    assert result.evidence_refs == ()


def test_degraded_signal_is_bound_but_excluded_from_tuples(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)
    assert config.research.graceful_degradation is True
    client = _QualityClient([_quality_response(score=3.0, verdict="revise")])

    result = _run_private(active_capture, llm=client)

    assert result.status is StageStatus.DONE
    assert result.decision == "degraded"
    assert result.artifacts == publication.STRUCTURED_STAGE20_ARTIFACTS
    signal = active_capture.run_dir / "degradation_signal.json"
    assert signal.is_file()
    manifest = json.loads(
        (
            active_capture.run_dir / "stage-20/quality_gate_manifest.json"
        ).read_bytes()
    )
    assert manifest["outcome"] == "degraded"
    assert manifest["degradation_signal"]["path"] == "degradation_signal.json"
    publication.clear_structured_stage20_published_context(active_capture.lease)


def test_degraded_root_signal_tmp_is_rejected(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original_capture = publication._capture_final
    injected = False

    def capture_with_tmp(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            (active_capture.run_dir / "degradation_signal.json.tmp").write_text(
                "late temp", encoding="utf-8"
            )
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(publication, "_capture_final", capture_with_tmp)
    result = _run_private(
        active_capture,
        llm=_QualityClient([_quality_response(score=3.0, verdict="revise")]),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "degradation_signal.json.tmp"
    ).exists()


def test_degraded_root_signal_late_collision_does_not_write_symlink_target(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original_publish = publication._create_formal_from_verified_temp
    external = tmp_path / "external-signal"
    external.write_text("external late collision", encoding="utf-8")
    injected = False

    def publish_after_collision(*args, **kwargs):
        nonlocal injected
        if kwargs["name"] == publication.STRUCTURED_STAGE20_SIGNAL and not injected:
            injected = True
            (active_capture.run_dir / publication.STRUCTURED_STAGE20_SIGNAL).symlink_to(
                external
            )
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        publication,
        "_create_formal_from_verified_temp",
        publish_after_collision,
    )
    result = _run_private(
        active_capture,
        llm=_QualityClient([_quality_response(score=3.0, verdict="revise")]),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert external.read_text(encoding="utf-8") == "external late collision"
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_zero_quality_threshold_is_not_replaced_by_default(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)
    zero_config = replace(
        config,
        research=replace(config.research, quality_threshold=0.0),
    )
    monkeypatch.setattr(
        publication,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: zero_config,
    )

    result = _run_private(
        active_capture,
        llm=_QualityClient([_quality_response(score=0.0, verdict="proceed")]),
    )

    assert result.status is StageStatus.DONE
    manifest = json.loads(
        (
            active_capture.run_dir / "stage-20/quality_gate_manifest.json"
        ).read_bytes()
    )
    assert manifest["quality_threshold"] == "0"
    publication.clear_structured_stage20_published_context(active_capture.lease)


def test_verified_context_constructor_and_forged_variants_fail_closed(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    with pytest.raises(TypeError, match="construction is private"):
        publication.StructuredStage20VerifiedContext(
            authority=object(),
            construction_token=object(),
            writer_owner=object(),
            run_path="/forged",
            run_identity=(1, 2),
            stage_identity=(3, 4),
            generation_binding_sha256="0" * 64,
            cfs_sha256="1" * 64,
            stage19_manifest_sha256="2" * 64,
            snapshot_identity=(),
        )
    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        snapshot, context = publication._capture_snapshot_a_and_issue_context(
            active_capture.lease, namespace=namespace
        )
        with pytest.raises(
            publication.StructuredStage20PublicationError,
            match="privately constructed",
        ):
            publication._require_verified_context(
                active_capture.lease, object(), namespace=namespace
            )
        copied = copy.copy(context)
        assert copied is not context
        with pytest.raises(
            publication.StructuredStage20PublicationError,
            match="was not issued",
        ):
            publication._require_verified_context(
                active_capture.lease, copied, namespace=namespace
            )
        reader = ReleaseGraphLock.acquire(
            active_capture.run_dir, "b4b-reader", mode="read"
        )
        try:
            with pytest.raises(RuntimeError, match="writer_lease_required"):
                publication._require_verified_context(
                    reader, context, namespace=namespace
                )
        finally:
            reader.close()
        inactive = ReleaseGraphLock.acquire(
            active_capture.run_dir, "b4b-inactive", mode="write"
        )
        inactive.close()
        with pytest.raises(RuntimeError, match="lease_inactive"):
            publication._require_verified_context(
                inactive, context, namespace=namespace
            )
        with active_capture.lease.open_stage_namespace("stage-19") as foreign:
            with pytest.raises(RuntimeError, match="namespace_stage_mismatch"):
                publication._require_verified_context(
                    active_capture.lease, context, namespace=foreign
                )
        wrong_run = tmp_path / "wrong-run"
        wrong_run.mkdir()
        with ReleaseGraphLock.acquire(
            wrong_run, "b4b-wrong-run", mode="write"
        ) as wrong:
            wrong.ensure_run_directory("stage-20")
            with wrong.open_stage_namespace("stage-20") as wrong_namespace:
                with pytest.raises(
                    publication.StructuredStage20PublicationError,
                    match="writer mismatch",
                ):
                    publication._require_verified_context(
                        wrong, context, namespace=wrong_namespace
                    )
        consumed = publication._execute_structured_stage20_from_context(
            active_capture.lease,
            namespace=namespace,
            snapshot=snapshot,
            context=context,
            llm=None,
        )
        assert consumed.status is StageStatus.FAILED
        with pytest.raises(
            publication.StructuredStage20PublicationError,
            match="was not issued",
        ):
            publication._execute_structured_stage20_from_context(
                active_capture.lease,
                namespace=namespace,
                snapshot=snapshot,
                context=context,
                llm=None,
            )


def test_schema_replay_rejects_boolean_version_and_synchronous_forgery() -> None:
    response = authority.parse_quality_response(
        _quality_response().content.encode("utf-8")
    )
    report = authority.build_quality_report(
        response,
        canonical_evidence_path="canonical_experiment_evidence.json",
        canonical_evidence_sha256="a" * 64,
        cfs_sha256="b" * 64,
        generation_binding_sha256="c" * 64,
        source_paper_sha256="d" * 64,
        stage19_manifest_sha256="e" * 64,
        generated="2026-07-27T00:00:00+00:00",
    )
    payload = json.loads(report)
    payload["schema_version"] = True
    forged = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(
        authority.StructuredStage20AuthorityError,
        match="header mismatch",
    ):
        authority.replay_quality_report(forged, expected=forged)

    payload = json.loads(report)
    payload["source_paper_sha256"] = "f" * 64
    synchronously_forged = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(
        authority.StructuredStage20AuthorityError,
        match="expected rebuild",
    ):
        authority.replay_quality_report(synchronously_forged, expected=report)


def test_manifest_null_file_ref_branch_is_strict() -> None:
    manifest = authority.build_quality_gate_manifest(
        outcome="passed",
        verdict="proceed",
        canonical_evidence_path="canonical_experiment_evidence.json",
        canonical_evidence_sha256="a" * 64,
        cfs_sha256="b" * 64,
        generation_binding_sha256="c" * 64,
        source_paper_sha256="d" * 64,
        stage19_manifest_sha256="e" * 64,
        quality_report_sha256="f" * 64,
        fabrication_flags_sha256="1" * 64,
        quality_score=8,
        quality_threshold=4,
        graceful_degradation=True,
        degradation_signal_sha256=None,
        generated="2026-07-27T00:00:00+00:00",
    )
    authority.replay_quality_gate_manifest(
        manifest, expected=manifest, signal_present=False
    )
    with pytest.raises(
        authority.StructuredStage20AuthorityError,
        match="branch mismatch",
    ):
        authority.replay_quality_gate_manifest(
            manifest, expected=manifest, signal_present=True
        )


def test_generated_fields_require_utc_timestamp() -> None:
    response = authority.parse_quality_response(
        _quality_response().content.encode("utf-8")
    )
    with pytest.raises(
        authority.StructuredStage20AuthorityError,
        match="UTC timestamp",
    ):
        authority.build_quality_report(
            response,
            canonical_evidence_path="canonical_experiment_evidence.json",
            canonical_evidence_sha256="a" * 64,
            cfs_sha256="b" * 64,
            generation_binding_sha256="c" * 64,
            source_paper_sha256="d" * 64,
            stage19_manifest_sha256="e" * 64,
            generated="not-a-utc-timestamp",
        )


def test_synchronized_three_output_forgery_cannot_self_prove() -> None:
    response = authority.parse_quality_response(
        _quality_response().content.encode("utf-8")
    )
    state = Stage20FabricationState(
        experiment_failed=False,
        real_metric_values=("1.25",),
        verified_values_count=1,
        verified_conditions=("primary",),
        has_real_data=True,
        fabrication_suspected=False,
    )
    common = {
        "canonical_evidence_path": "canonical_experiment_evidence.json",
        "canonical_evidence_sha256": "a" * 64,
        "cfs_sha256": "b" * 64,
        "generation_binding_sha256": "c" * 64,
        "source_paper_sha256": "d" * 64,
        "stage19_manifest_sha256": "e" * 64,
    }
    report = authority.build_quality_report(
        response,
        **common,
        generated="2026-07-27T00:00:00+00:00",
    )
    flags = authority.build_fabrication_flags(
        state,
        quality_score=8,
        **common,
    )
    manifest = authority.build_quality_gate_manifest(
        outcome="passed",
        verdict="proceed",
        **common,
        quality_report_sha256=hashlib.sha256(report).hexdigest(),
        fabrication_flags_sha256=hashlib.sha256(flags).hexdigest(),
        quality_score=8,
        quality_threshold=4,
        graceful_degradation=True,
        degradation_signal_sha256=None,
        generated="2026-07-27T00:00:00+00:00",
    )

    forged_report_payload = json.loads(report)
    forged_report_payload["source_paper_sha256"] = "f" * 64
    forged_report = (
        json.dumps(
            forged_report_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    forged_flags_payload = json.loads(flags)
    forged_flags_payload["source_paper_sha256"] = "f" * 64
    forged_flags = (
        json.dumps(
            forged_flags_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )
    forged_manifest_payload = json.loads(manifest)
    forged_manifest_payload["source_paper"]["sha256"] = "f" * 64
    forged_manifest_payload["quality_report"]["sha256"] = hashlib.sha256(
        forged_report
    ).hexdigest()
    forged_manifest_payload["fabrication_flags"]["sha256"] = hashlib.sha256(
        forged_flags
    ).hexdigest()
    forged_manifest = (
        json.dumps(
            forged_manifest_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )

    with pytest.raises(authority.StructuredStage20AuthorityError):
        authority.replay_quality_report(forged_report, expected=report)
    with pytest.raises(authority.StructuredStage20AuthorityError):
        authority.replay_fabrication_flags(forged_flags, expected=flags)
    with pytest.raises(authority.StructuredStage20AuthorityError):
        authority.replay_quality_gate_manifest(
            forged_manifest, expected=manifest, signal_present=False
        )


def test_manifest_post_publish_replacement_is_not_claimed_or_followed(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original_create = publication._create_formal_from_verified_temp
    external = tmp_path / "external-manifest-target"
    external.write_bytes(b"external manifest replacement")
    replaced = False

    def replace_after_return(*args, **kwargs):
        nonlocal replaced
        identity = original_create(*args, **kwargs)
        if (
            kwargs["name"] == publication.STRUCTURED_STAGE20_MANIFEST
            and not replaced
        ):
            replaced = True
            manifest = (
                active_capture.run_dir
                / "stage-20"
                / publication.STRUCTURED_STAGE20_MANIFEST
            )
            manifest.unlink()
            manifest.symlink_to(external)
        return identity

    monkeypatch.setattr(
        publication,
        "_create_formal_from_verified_temp",
        replace_after_return,
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.FAILED
    assert external.read_bytes() == b"external manifest replacement"
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_manifest_candidate_then_source_mutation_revokes_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original_capture = publication._capture_final
    mutated = False

    def mutate_source_after_manifest(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            source = (
                active_capture.run_dir
                / "stage-19/scientific_claim_paper_revised.md"
            )
            source.write_bytes(source.read_bytes() + b"\nlate source mutation\n")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(
        publication, "_capture_final", mutate_source_after_manifest
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_transport_bounds_retry_identity_and_no_hidden_fallback() -> None:
    assert transport.SEMANTIC_CALL_LIMIT == 2
    assert transport.OUTBOUND_ATTEMPT_LIMIT_PER_CALL == 2
    assert transport.TOTAL_OUTBOUND_ATTEMPT_LIMIT == 4
    client = _QualityClient(
        [
            TimeoutError("initial transport"),
            LLMResponse(content="{", model="fixture-quality"),
            TimeoutError("repair transport"),
            _quality_response(),
        ]
    )
    response, diagnostics = transport.execute_bounded_quality_calls(
        paper=b"immutable paper",
        quality_threshold="4",
        llm=client,  # type: ignore[arg-type]
    )
    assert response["verdict"] == "proceed"
    assert len(client.requests) == 4
    assert len(diagnostics) == 4
    assert diagnostics[0].request_fingerprint == diagnostics[1].request_fingerprint
    assert diagnostics[2].request_fingerprint == diagnostics[3].request_fingerprint

    hidden = _QualityClient([_quality_response()])
    hidden.config.fallback_models = ["hidden"]
    with pytest.raises(
        transport.StructuredStage20TransportError,
        match="forbids endpoint and model fallback",
    ):
        transport.execute_bounded_quality_calls(
            paper=b"immutable paper",
            quality_threshold="4",
            llm=hidden,  # type: ignore[arg-type]
        )
    assert hidden.requests == []


def test_transport_retry_rejects_credential_drift() -> None:
    client = _QualityClient(
        [TimeoutError("first transport"), _quality_response()]
    )
    original_raw_call = client._raw_call

    def mutate_credential(*args: object, **kwargs: object) -> LLMResponse:
        client.config.api_key = "credential-b"
        return original_raw_call(*args, **kwargs)

    client._raw_call = mutate_credential  # type: ignore[method-assign]
    with pytest.raises(
        transport.StructuredStage20TransportError,
        match="frozen request changed",
    ):
        transport.execute_bounded_quality_calls(
            paper=b"immutable paper",
            quality_threshold="4",
            llm=client,  # type: ignore[arg-type]
        )
    assert len(client.requests) == 1


def test_transport_does_not_retry_anthropic_http_body() -> None:
    response_error = urllib.error.HTTPError(
        "https://fixture.invalid/v1/messages",
        429,
        "rate limited: response body",
        {},
        None,
    )
    response_error._researchclaw_response_content_received = True
    client = _QualityClient([response_error, _quality_response()])

    with pytest.raises(
        transport.StructuredStage20TransportError,
        match="transport failed",
    ):
        transport.execute_bounded_quality_calls(
            paper=b"immutable paper",
            quality_threshold="4",
            llm=client,  # type: ignore[arg-type]
        )
    assert len(client.requests) == 1


def test_transport_does_not_retry_http_client_partial_body() -> None:
    partial = http.client.IncompleteRead(b"partial-response", 100)
    client = _QualityClient([partial, _quality_response()])

    with pytest.raises(
        transport.StructuredStage20TransportError,
        match="transport failed",
    ):
        transport.execute_bounded_quality_calls(
            paper=b"immutable paper",
            quality_threshold="4",
            llm=client,  # type: ignore[arg-type]
        )
    assert len(client.requests) == 1


def test_anthropic_uncertain_read_error_is_not_retryable() -> None:
    if not anthropic_adapter.HAS_HTTPX:
        pytest.skip("httpx is unavailable")
    import httpx

    class _FailingClient:
        def post(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError(
                "peer closed after response began",
                request=httpx.Request(
                    "POST", "https://fixture.invalid/v1/messages"
                ),
            )

    adapter = anthropic_adapter.AnthropicAdapter(
        "https://fixture.invalid", "credential"
    )
    adapter._client = _FailingClient()  # type: ignore[assignment]
    with pytest.raises(urllib.error.URLError) as raised:
        adapter.chat_completion(
            "claude-fixture",
            [{"role": "user", "content": "quality"}],
            1000,
            0,
            True,
        )
    retryable, _category = transport._retryable_without_content(raised.value)
    assert retryable is False


def test_independent_outcome_replay_rejects_synchronized_manifest_builder_forgery(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original = publication.build_quality_gate_manifest

    def contradictory_manifest(**kwargs):
        payload = json.loads(original(**kwargs))
        payload["quality_verdict"] = "reject"
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )

    monkeypatch.setattr(
        publication, "build_quality_gate_manifest", contradictory_manifest
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_executor_immediate_and_terminal_failures_revoke_authority(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)
    mode = {"value": "immediate"}

    def producer(
        _stage_dir,
        run_dir,
        _config,
        _adapters,
        **_kwargs,
    ):
        client = _QualityClient([_quality_response()])
        with ReleaseGraphLock.acquire(
            run_dir, "b4b-executor-producer", mode="write"
        ) as lease:
            with lease.open_stage_namespace("stage-20") as namespace:
                snapshot, context = (
                    publication._capture_snapshot_a_and_issue_context(
                        lease, namespace=namespace
                    )
                )
                result = publication._execute_structured_stage20_from_context(
                    lease,
                    namespace=namespace,
                    snapshot=snapshot,
                    context=context,
                    llm=(
                        None
                        if mode["value"] == "helper_failure"
                        else client  # type: ignore[arg-type]
                    ),
                )
        if mode["value"] == "immediate":
            return replace(result, artifacts=("wrong.json",))
        return result

    original_post = pipeline_executor._run_hitl_post_stage

    def post_hook(stage, result, run_dir, adapters, **kwargs):
        if mode["value"] == "terminal" and result.status is StageStatus.DONE:
            (run_dir / "stage-20/quality_report.json").unlink()
        return original_post(stage, result, run_dir, adapters, **kwargs)

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS, Stage.QUALITY_GATE, producer
    )
    monkeypatch.setattr(
        pipeline_executor, "_read_prior_artifact", lambda *_args: object()
    )
    monkeypatch.setattr(pipeline_executor, "_run_hitl_post_stage", post_hook)

    immediate = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-immediate",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    assert immediate.status is StageStatus.FAILED
    assert "postcondition" in (immediate.error or "").lower()
    assert immediate.artifacts == ()
    assert immediate.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()

    mode["value"] = "helper_failure"
    helper_failure = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-helper-failure",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    assert helper_failure.status is StageStatus.FAILED
    assert "requires a quality provider" in (helper_failure.error or "")
    assert helper_failure.artifacts == ()
    assert helper_failure.evidence_refs == ()
    assert not (active_capture.run_dir / "stage-20/stage_meta.json").exists()
    assert not (active_capture.run_dir / "stage-20/stage_health.json").exists()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()

    mode["value"] = "terminal"
    terminal = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-terminal",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )
    assert terminal.status is StageStatus.FAILED
    assert "terminal postcondition" in (terminal.error or "").lower()
    assert terminal.artifacts == ()
    assert terminal.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_executor_attempt_only_failure_cleans_late_reserved_manifest(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)

    def producer(
        _stage_dir,
        run_dir,
        _config,
        _adapters,
        **_kwargs,
    ):
        with ReleaseGraphLock.acquire(
            run_dir, "b4b-attempt-only-producer", mode="write"
        ) as lease:
            with lease.open_stage_namespace("stage-20") as namespace:
                snapshot, context = (
                    publication._capture_snapshot_a_and_issue_context(
                        lease, namespace=namespace
                    )
                )
                result = publication._execute_structured_stage20_from_context(
                    lease,
                    namespace=namespace,
                    snapshot=snapshot,
                    context=context,
                    llm=None,
                )
        (run_dir / "stage-20/quality_gate_manifest.json").write_bytes(
            b"late-reserved-collision"
        )
        return result

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS, Stage.QUALITY_GATE, producer
    )
    monkeypatch.setattr(
        pipeline_executor, "_read_prior_artifact", lambda *_args: object()
    )
    result = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-attempt-only",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_capture_replay_failure_is_structured_before_generic_metadata(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)
    stage20 = active_capture.run_dir / "stage-20"
    for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
        (stage20 / name).write_text("stale authority", encoding="utf-8")
    (active_capture.run_dir / "degradation_signal.json").write_text(
        "stale signal", encoding="utf-8"
    )

    def reject_replay(*_args, **_kwargs):
        for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
            assert not (stage20 / name).exists()
        assert not (
            active_capture.run_dir / "degradation_signal.json"
        ).exists()
        raise publication.StructuredStage20PublicationError(
            "forced source replay failure"
        )

    monkeypatch.setattr(publication.stage19, "_replay_outputs", reject_replay)

    def producer(_stage_dir, run_dir, _config, _adapters, **_kwargs):
        with ReleaseGraphLock.acquire(
            run_dir, "b4b-capture-failure", mode="write"
        ) as lease:
            with lease.open_stage_namespace("stage-20") as namespace:
                publication._capture_snapshot_a_and_issue_context(
                    lease, namespace=namespace
                )
        raise AssertionError("unreachable")

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS, Stage.QUALITY_GATE, producer
    )
    monkeypatch.setattr(
        pipeline_executor, "_read_prior_artifact", lambda *_args: object()
    )
    result = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-capture-failure",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (stage20 / "stage_meta.json").exists()
    assert not (stage20 / "stage_health.json").exists()


def test_executor_stage_parent_replacement_cleans_held_detached_namespace(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _prepare_stage20_source(active_capture, monkeypatch)
    detached = active_capture.run_dir / "stage-20-detached"

    def producer(_stage_dir, run_dir, _config, _adapters, **_kwargs):
        with ReleaseGraphLock.acquire(
            run_dir, "b4b-stage-replacement", mode="write"
        ) as lease:
            with lease.open_stage_namespace("stage-20") as namespace:
                snapshot, context = (
                    publication._capture_snapshot_a_and_issue_context(
                        lease, namespace=namespace
                    )
                )
                result = publication._execute_structured_stage20_from_context(
                    lease,
                    namespace=namespace,
                    snapshot=snapshot,
                    context=context,
                    llm=_QualityClient([_quality_response()]),  # type: ignore[arg-type]
                )
        (run_dir / "stage-20").rename(detached)
        (run_dir / "stage-20").mkdir()
        (run_dir / "stage-20/external-sentinel").write_text(
            "keep", encoding="utf-8"
        )
        return result

    monkeypatch.setitem(
        pipeline_executor._STAGE_EXECUTORS, Stage.QUALITY_GATE, producer
    )
    monkeypatch.setattr(
        pipeline_executor, "_read_prior_artifact", lambda *_args: object()
    )
    result = pipeline_executor.execute_stage(
        Stage.QUALITY_GATE,
        run_dir=active_capture.run_dir,
        run_id="b4b-stage-replacement",
        config=config,
        adapters=AdapterBundle(),
        auto_approve_gates=True,
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert (
        active_capture.run_dir / "stage-20/external-sentinel"
    ).read_text(encoding="utf-8") == "keep"
    for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
        assert not (detached / name).exists()


def test_lifecycle_hardlink_collision_late_mutation_and_parent_replacement(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    source = (
        active_capture.run_dir
        / "stage-19/scientific_claim_paper_revised.md"
    )
    alias = active_capture.run_dir / "paper-hardlink-alias"
    os.link(source, alias)
    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        with pytest.raises(
            publication.StructuredStage20PublicationError,
            match="unalias",
        ):
            publication._capture_snapshot_a_and_issue_context(
                active_capture.lease, namespace=namespace
            )
    alias.unlink()

    collision = active_capture.run_dir / "stage-20/quality_report.json"
    collision.mkdir()
    (collision / "sentinel").write_text("keep", encoding="utf-8")
    client = _QualityClient([_quality_response()])
    collision_result = _run_private(active_capture, llm=client)
    assert collision_result.status is StageStatus.FAILED
    assert client.requests == []
    assert (collision / "sentinel").read_text(encoding="utf-8") == "keep"
    (collision / "sentinel").unlink()
    collision.rmdir()

    original_capture_final = publication._capture_final
    capture_count = 0

    def mutate_after_first(*args, **kwargs):
        nonlocal capture_count
        final = original_capture_final(*args, **kwargs)
        capture_count += 1
        if capture_count == 1:
            flags = (
                active_capture.run_dir
                / "stage-20/fabrication_flags.json"
            )
            flags.write_bytes(flags.read_bytes() + b"\nlate mutation\n")
        return final

    with monkeypatch.context() as late_patch:
        late_patch.setattr(publication, "_capture_final", mutate_after_first)
        late_result = _run_private(
            active_capture, llm=_QualityClient([_quality_response()])
        )
    assert late_result.status is StageStatus.FAILED
    assert late_result.artifacts == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()

    original_fixpoint = publication._verify_source_fixpoint
    fixpoint_count = 0
    detached = tmp_path / "detached-run"

    def replace_parent_on_execution(lease, snapshot):
        nonlocal fixpoint_count
        fixpoint_count += 1
        if fixpoint_count == 2:
            active_capture.run_dir.rename(detached)
            active_capture.run_dir.mkdir()
            (active_capture.run_dir / "external-sentinel").write_text(
                "keep", encoding="utf-8"
            )
        return original_fixpoint(lease, snapshot)

    with monkeypatch.context() as parent_patch:
        parent_patch.setattr(
            publication, "_verify_source_fixpoint", replace_parent_on_execution
        )
        parent_result = _run_private(
            active_capture, llm=_QualityClient([_quality_response()])
        )
    assert parent_result.status is StageStatus.FAILED
    assert parent_result.artifacts == ()
    assert (
        active_capture.run_dir / "external-sentinel"
    ).read_text(encoding="utf-8") == "keep"
    assert not (active_capture.run_dir / "stage-20").exists()
    assert not (
        detached / "stage-20/quality_gate_manifest.json"
    ).exists()
