"""B5-A5 structured Stage 25 schema, verdict, and lifecycle boundaries."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline import executor
from researchclaw.pipeline import stage24_structured_publication as stage24
from researchclaw.pipeline import stage24_structured_authority as stage24_authority
from researchclaw.pipeline import stage24_structured_transport as transport
from researchclaw.pipeline import stage25_structured_authority as authority
from researchclaw.pipeline import stage25_structured_publication as publication
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.stages import StageStatus
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from tests.test_stage17_structured_producer_integration import active_capture
from tests.test_stage24_structured_publication import (
    _assessment_http,
    _client_factory,
    _complete_stage24_upstream,
)


def _ref(path: str) -> dict[str, object]:
    return {"path": path, "sha256": "a" * 64, "size": 1}


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(
        path,
        __import__("hashlib").sha256(content).hexdigest(),
        content,
    )


def test_stage25_authority_exact_roots_and_mechanical_verdict() -> None:
    audit = authority.example_audit_v2()
    assert tuple(audit) == authority.STRUCTURED_STAGE25_AUDIT_ROOTS
    authority.validate_stage25_audit_v2(audit)

    manifest = authority.example_manifest_v2()
    assert tuple(manifest) == authority.STRUCTURED_STAGE25_MANIFEST_ROOTS
    assert len(manifest) == 26
    authority.validate_stage25_manifest_v2(manifest)
    assert manifest["release_verdict"] == {"verdict": "eligible", "reasons": []}

    reasons = authority.derive_release_verdict(
        stage24_outcome="degraded",
        quality_outcome="degraded",
        stage23_outcome="degraded",
        degradation_signal=_ref("degradation_signal.json"),
        claim_scope="pipeline_validation",
        compiler_outcome="compiler-failure",
        pdf_valid=False,
    )
    assert reasons == {
        "verdict": "blocked",
        "reasons": list(authority.RELEASE_BLOCK_REASONS),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("output_count", 1.0),
        ("stage24_output_count", False),
    ),
)
def test_stage25_manifest_rejects_bool_float_and_exact_root_drift(
    field: str,
    value: object,
) -> None:
    manifest = authority.example_manifest_v2()
    manifest[field] = value
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)

    manifest = authority.example_manifest_v2()
    manifest["extra"] = None
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)


def test_stage25_manifest_rejects_output_reorder_duplicate_and_verdict_drift() -> None:
    manifest = authority.example_manifest_v2()
    manifest["stage24_outputs"] = list(
        reversed(manifest["stage24_outputs"])
    )
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)

    manifest = authority.example_manifest_v2()
    row = {
        "role": "citation_assessment",
        "logical_name": "a" * 64,
        "path": f"stage-24/citation-assessments/{'a' * 64}.json",
        "sha256": "a" * 64,
        "size": 1,
    }
    manifest["stage24_outputs"].extend((row, dict(row)))
    manifest["stage24_output_count"] += 2
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)

    manifest = authority.example_manifest_v2()
    manifest["release_verdict"] = {
        "verdict": "eligible",
        "reasons": ["quality_not_passed"],
    }
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)


def test_stage25_manifest_rejects_incomplete_stage24_closure_bad_dynamic_path_and_local_false_eligible() -> None:
    manifest = authority.example_manifest_v2()
    manifest["stage24_outputs"] = []
    manifest["stage24_output_count"] = 0
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)

    manifest = authority.example_manifest_v2()
    manifest["stage24_outputs"].append(
        {
            "role": "citation_assessment",
            "logical_name": "not-a-digest",
            "path": "wrong/place.json",
            "sha256": "a" * 64,
            "size": 1,
        }
    )
    manifest["stage24_output_count"] += 1
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)

    manifest = authority.example_manifest_v2()
    manifest["quality_outcome"] = "degraded"
    manifest["degradation_signal"] = _ref("degradation_signal.json")
    manifest["claim_scope"] = "exploratory"
    manifest["stage24_outcome"] = "degraded"
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)


@pytest.mark.parametrize(
    "forged_reason",
    (
        "claim_scope_not_research_release",
        "degradation_present",
        "quality_not_passed",
        "stage24_not_passed",
    ),
)
def test_stage25_manifest_rejects_false_local_blocker(
    forged_reason: str,
) -> None:
    manifest = authority.example_manifest_v2()
    manifest["release_verdict"] = {
        "verdict": "blocked",
        "reasons": [forged_reason],
    }
    with pytest.raises(authority.StructuredStage25AuthorityError):
        authority.validate_stage25_manifest_v2(manifest)


def test_stage25_audit_synchronized_payload_hash_cannot_replace_expected_bytes() -> None:
    capture = SimpleNamespace(
        paper=_bound(
            "stage-23/paper_final_verified.md",
            b"Plain deterministic paper.\n",
        ),
        manifest=_bound(
            "stage-24/stage24_truth_manifest.json",
            b'{"authority":"fixture"}\n',
        ),
    )
    original = publication._build_audit(capture)
    value = json.loads(original)
    value["suggestions"] = [
        {
            "source": "heuristic",
            "span": "Forged prose",
            "issue": "forged issue",
            "suggested_rewrite": "",
            "risk": "style_only",
        }
    ]
    value["counts"] = {"total": 1, "touches_claim": 0}
    forged = stage24_authority.global_canonical_json_bytes(value)
    authority.validate_stage25_audit_v2(value)
    with pytest.raises(
        publication.StructuredStage25PublicationError,
        match="independent replay mismatch",
    ):
        publication._replay_audit(capture, forged)


@pytest.mark.parametrize("collision_kind", ("regular", "directory", "symlink", "fifo"))
def test_stage25_absent_only_namespace_rejects_preexisting_collision(
    tmp_path: Path,
    collision_kind: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "stage-25"
    if collision_kind == "regular":
        target.write_text("collision", encoding="utf-8")
    elif collision_kind == "directory":
        target.mkdir()
    elif collision_kind == "symlink":
        external = tmp_path / "external"
        external.mkdir()
        target.symlink_to(external, target_is_directory=True)
    else:
        os.mkfifo(target)
    with ReleaseGraphLock.acquire(
        run_dir, "test_stage25_absent_namespace", mode="write"
    ) as lease:
        with pytest.raises(
            publication.StructuredStage25PublicationError,
            match="already exists",
        ):
            publication._acquire_absent_stage25_namespace(lease)
    assert target.exists() or target.is_symlink()


@pytest.mark.parametrize("source_kind", ("hardlink", "symlink", "fifo"))
def test_current_stage24_held_file_rejects_special_or_aliased_source(
    tmp_path: Path,
    source_kind: str,
) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source = tmp_path / "source.json"
    if source_kind == "hardlink":
        original = tmp_path / "original.json"
        original.write_text("{}", encoding="utf-8")
        os.link(original, source)
    elif source_kind == "symlink":
        external = tmp_path / "external.json"
        external.write_text("{}", encoding="utf-8")
        source.symlink_to(external)
    else:
        os.mkfifo(source)
    try:
        with pytest.raises((OSError, stage24.StructuredStage24PublicationError)):
            stage24._hold_existing_stage24_file(
                parent_fd,
                source.name,
                logical_path=f"stage-24/{source.name}",
            )
    finally:
        os.close(parent_fd)


def test_current_stage24_held_file_rejects_parent_name_replacement(
    tmp_path: Path,
) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    held = stage24._hold_existing_stage24_file(
        parent_fd,
        source.name,
        logical_path=f"stage-24/{source.name}",
    )
    try:
        source.rename(tmp_path / "detached.json")
        source.write_text("{}", encoding="utf-8")
        with pytest.raises(
            stage24.StructuredStage24PublicationError,
            match="identity changed",
        ):
            stage24._read_held_file(held)
    finally:
        os.close(held.descriptor)
        os.close(parent_fd)


def test_b5a5_keeps_1110_generic_and_release_boundaries() -> None:
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }
    assert publication.STRUCTURED_STAGE25_ARTIFACTS == (
        "deai_audit.json",
        "stage25_deai_manifest.json",
    )
    assert publication.STRUCTURED_STAGE25_EVIDENCE_REFS == (
        "stage-25/deai_audit.json",
        "stage-25/stage25_deai_manifest.json",
    )
    assert tuple(
        inspect.signature(executor._execute_structured_stage25_private).parameters
    ) == ("release_lock", "pre_admission_context")


def test_public_direct_guard_precedes_lock_path_or_stage25_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def forbidden(*_args, **_kwargs):
        touched.append("io")
        raise AssertionError("guard was not first")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden)
    with pytest.raises(capability.StructuredScientificClaimCapabilityIncomplete):
        publication.execute_structured_stage25_verdict(
            tmp_path,
            tmp_path / "stage-25",
        )
    assert touched == []


def test_pre_admission_failure_is_failed_retry_with_empty_tuples(
    tmp_path,
) -> None:
    class Lease:
        run_dir = tmp_path

    result = executor._execute_structured_stage25_with_pre_admission_private(
        Lease(),
        canonical_stage_dir=False,
    )
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()


def _publish_stage24(active_capture, monkeypatch: pytest.MonkeyPatch) -> None:
    _complete_stage24_upstream(active_capture, monkeypatch)
    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        lambda _client, request: _assessment_http(request),
    )
    pre = stage24.issue_stage24_pre_admission_context(
        active_capture.lease,
        clients={role: _client_factory for role in transport.ROLE_ORDER},
    )
    result = executor._execute_structured_stage24_private(
        active_capture.lease,
        pre,
    )
    assert result.status is StageStatus.DONE, result.error


def test_private_stage25_real_blocked_publication_and_executor_postconditions(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_stage24(active_capture, monkeypatch)
    lifecycle: list[str] = []
    original_immediate = (
        publication.validate_structured_stage25_immediate_postcondition
    )
    original_terminal = (
        publication.validate_structured_stage25_terminal_postcondition
    )

    def observe_immediate(*args, **kwargs):
        lifecycle.append("immediate")
        return original_immediate(*args, **kwargs)

    def observe_terminal(*args, **kwargs):
        lifecycle.append("terminal")
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(
        publication,
        "validate_structured_stage25_immediate_postcondition",
        observe_immediate,
    )
    monkeypatch.setattr(
        publication,
        "validate_structured_stage25_terminal_postcondition",
        observe_terminal,
    )
    pre = publication.issue_stage25_pre_admission_context(active_capture.lease)
    result = executor._execute_structured_stage25_private(
        active_capture.lease,
        pre,
    )
    assert result.status is StageStatus.DONE, result.error
    assert result.artifacts == publication.STRUCTURED_STAGE25_ARTIFACTS
    assert result.evidence_refs == publication.STRUCTURED_STAGE25_EVIDENCE_REFS
    assert lifecycle == ["immediate", "terminal"]
    stage_dir = active_capture.run_dir / "stage-25"
    assert tuple(sorted(path.name for path in stage_dir.iterdir())) == (
        "deai_audit.json",
        "stage25_deai_manifest.json",
    )
    audit = json.loads((stage_dir / "deai_audit.json").read_bytes())
    manifest = json.loads(
        (stage_dir / "stage25_deai_manifest.json").read_bytes()
    )
    assert set(audit) == set(authority.STRUCTURED_STAGE25_AUDIT_ROOTS)
    assert len(audit) == len(authority.STRUCTURED_STAGE25_AUDIT_ROOTS)
    assert set(manifest) == set(authority.STRUCTURED_STAGE25_MANIFEST_ROOTS)
    assert len(manifest) == len(authority.STRUCTURED_STAGE25_MANIFEST_ROOTS)
    assert manifest["release_verdict"] == {
        "verdict": "blocked",
        "reasons": [
            "claim_scope_not_research_release",
            "stage23_not_passed",
        ],
    }
    assert manifest["stage24_output_count"] == len(
        manifest["stage24_outputs"]
    )
    assert manifest["outputs"] == [
        {
            "role": "deai_audit",
            "logical_name": None,
            "path": "stage-25/deai_audit.json",
            "sha256": __import__("hashlib").sha256(
                (stage_dir / "deai_audit.json").read_bytes()
            ).hexdigest(),
            "size": len((stage_dir / "deai_audit.json").read_bytes()),
        }
    ]


def test_stage24_replay_failure_has_zero_stage25_namespace_io(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    touched: list[str] = []
    monkeypatch.setattr(
        stage24,
        "capture_current_structured_stage24",
        lambda _lease: (_ for _ in ()).throw(
            stage24.StructuredStage24PublicationError(
                "forged current Stage 24 manifest"
            )
        ),
    )
    monkeypatch.setattr(
        publication,
        "_acquire_absent_stage25_namespace",
        lambda _lease: touched.append("stage25_io"),
    )
    with ReleaseGraphLock.acquire(
        run_dir, "test_stage24_replay_failure", mode="write"
    ) as lease:
        result = executor._execute_structured_stage25_with_pre_admission_private(
            lease,
        )

    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert touched == []
    assert not (run_dir / "stage-25").exists()


def test_stage25_terminal_mutation_withdraws_manifest_and_clears_tuples(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_stage24(active_capture, monkeypatch)
    original_terminal = (
        publication.validate_structured_stage25_terminal_postcondition
    )

    def mutate_before_terminal(lease, provisional, **kwargs):
        audit_path = active_capture.run_dir / "stage-25/deai_audit.json"
        audit_path.write_bytes(audit_path.read_bytes() + b" ")
        return original_terminal(lease, provisional, **kwargs)

    monkeypatch.setattr(
        publication,
        "validate_structured_stage25_terminal_postcondition",
        mutate_before_terminal,
    )
    pre = publication.issue_stage25_pre_admission_context(active_capture.lease)
    result = executor._execute_structured_stage25_private(
        active_capture.lease,
        pre,
    )

    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-25/stage25_deai_manifest.json"
    ).exists()
    assert not (active_capture.run_dir / "stage-25").exists()
