"""B5-A2 inactive structured Stage 22 deterministic export contracts."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import pickle
import socket
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import (
    stage22_structured_publication as publication,
)
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.stage22_semantics import Stage22DeterministicOutputs
from tests.test_stage17_structured_producer_integration import active_capture
from tests.test_stage21_structured_archive import passed_stage20
from tests.test_stage21_structured_archive import degraded_stage20
from tests.test_stage21_structured_archive import _publish as publish_stage21


def _patch_stage22_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from researchclaw.pipeline import stage22_semantics

    monkeypatch.setattr(
        stage22_semantics,
        "verify_paper",
        lambda *_args, **_kwargs: SimpleNamespace(
            passed=True,
            severity="PASS",
            total_numbers_checked=0,
            total_numbers_verified=0,
            strict_violations=0,
            lenient_violations=0,
            unverified_numbers=(),
            fabricated_conditions=(),
            config_warnings=(),
            summary="fixture verified",
        ),
    )


@pytest.fixture
def passed_stage21(passed_stage20, monkeypatch: pytest.MonkeyPatch):
    _patch_stage22_verifier(monkeypatch)
    result = publish_stage21(passed_stage20)
    assert result.status is StageStatus.DONE
    return passed_stage20


@pytest.fixture
def degraded_stage21(degraded_stage20, monkeypatch: pytest.MonkeyPatch):
    _patch_stage22_verifier(monkeypatch)
    result = publish_stage21(degraded_stage20)
    assert result.status is StageStatus.DONE
    return degraded_stage20


def _valid_pdf() -> bytes:
    prefix = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    xref = len(prefix)
    return (
        prefix
        + b"xref\n0 2\n0000000000 65535 f \n0000000009 00000 n \n"
        + b"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode("ascii")
        + b"\n%%EOF\n"
    )


def _successful_compile(tex_path: Path, *, max_attempts: int):
    assert max_attempts == 2
    (tex_path.parent / "paper.pdf").write_bytes(_valid_pdf())
    return SimpleNamespace(success=True, attempts=1, errors=())


def _failed_compile(_tex_path: Path, *, max_attempts: int):
    assert max_attempts == 2
    return SimpleNamespace(
        success=False,
        attempts=2,
        errors=("first failure", "second failure"),
    )


def _publish(capture, monkeypatch: pytest.MonkeyPatch, compiler=_successful_compile):
    monkeypatch.setattr(publication, "compile_latex", compiler)
    pre = publication.issue_stage22_pre_admission_context(capture.lease)
    return pipeline_executor._execute_structured_stage22_private(
        capture.lease,
        pre,
    )


def test_b5a2_keeps_capability_exactly_1110() -> None:
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }


def test_public_direct_guard_is_first_and_ordinary_stage22_api_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden(*_args, **_kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("structured Stage 22 touched lock or filesystem")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden)
    with pytest.raises(
        capability.StructuredScientificClaimCapabilityIncomplete,
        match="structured_scientific_claim_capability_incomplete",
    ):
        publication.execute_structured_stage22_export(
            tmp_path,
            tmp_path / "stage-22",
        )
    assert touched is False
    from researchclaw.pipeline.stage_impls._review_publish import (
        _execute_export_publish,
    )

    assert tuple(inspect.signature(_execute_export_publish).parameters) == (
        "stage_dir",
        "run_dir",
        "config",
        "adapters",
        "llm",
        "prompts",
    )


def test_contexts_are_private_noncopyable_nonserializable_and_single_use(
    passed_stage21,
) -> None:
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    assert publication.context_phase(pre) == "pre_admission"
    for operation in (
        lambda: copy.copy(pre),
        lambda: copy.deepcopy(pre),
        lambda: pickle.dumps(pre),
    ):
        with pytest.raises(TypeError):
            operation()
    attempt = publication.transition_stage22_pre_admission_context(
        passed_stage21.lease,
        pre,
    )
    assert publication.context_phase(attempt) == "namespace_bound"
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.transition_stage22_pre_admission_context(
            passed_stage21.lease,
            pre,
        )
    publication.fail_structured_stage22_attempt(passed_stage21.lease, attempt)


def test_stage_namespace_acquisition_rejects_every_preexisting_entry_unchanged(
    passed_stage21,
) -> None:
    run_dir = passed_stage21.run_dir
    stage_path = run_dir / "stage-22"

    stage_path.mkdir()
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    with pytest.raises(
        publication.StructuredStage22PublicationError,
        match="namespace transition failed",
    ):
        publication.transition_stage22_pre_admission_context(
            passed_stage21.lease, pre
        )
    assert stage_path.is_dir()
    assert tuple(stage_path.iterdir()) == ()
    stage_path.rmdir()

    stage_path.mkdir()
    marker = stage_path / "prior-authority"
    marker.write_bytes(b"preserve")
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.transition_stage22_pre_admission_context(
            passed_stage21.lease, pre
        )
    assert marker.read_bytes() == b"preserve"
    marker.unlink()
    stage_path.rmdir()

    outside = run_dir / "outside-stage22-target"
    outside.mkdir()
    stage_path.symlink_to(outside, target_is_directory=True)
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.transition_stage22_pre_admission_context(
            passed_stage21.lease, pre
        )
    assert stage_path.is_symlink()
    assert tuple(outside.iterdir()) == ()
    stage_path.unlink()

    os.mkfifo(stage_path)
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.transition_stage22_pre_admission_context(
            passed_stage21.lease, pre
        )
    assert stat.S_ISFIFO(stage_path.lstat().st_mode)
    stage_path.unlink()


def test_new_stage_is_strictly_empty_and_failed_attempt_removes_owned_stage(
    passed_stage21,
) -> None:
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    attempt = publication.transition_stage22_pre_admission_context(
        passed_stage21.lease, pre
    )
    record = publication._ATTEMPT_CONTEXTS[attempt]
    assert record.namespace.direct_entries() == ()
    assert os.listdir(record.namespace._stage_fd) == []
    run_info = os.fstat(record.namespace._run_fd)
    stage_info = os.fstat(record.namespace._stage_fd)
    assert stage_info.st_dev == run_info.st_dev
    assert stage_info.st_uid == os.geteuid()
    assert stat.S_IMODE(stage_info.st_mode) == 0o700
    assert stage_info.st_nlink >= 2
    assert os.get_inheritable(record.namespace._stage_fd) is False
    assert publication.fail_structured_stage22_attempt(
        passed_stage21.lease, attempt
    ) == ()
    assert not (passed_stage21.run_dir / "stage-22").exists()


@pytest.mark.skip(reason="no supported cross-device mount fixture in the sandbox")
def test_new_stage_rejects_wrong_device() -> None:
    """The same-device branch is asserted above; no portable cross-device fixture."""


def test_nested_directory_must_be_empty_before_first_child_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_open = publication.os.open

    def open_with_injected_child(name, flags, *args, **kwargs):
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == "code":
            child = original_open(
                "injected",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
            os.close(child)
        return descriptor

    monkeypatch.setattr(publication.os, "open", open_with_injected_child)
    try:
        with pytest.raises(
            publication.StructuredStage22PublicationError,
            match="directory",
        ):
            publication._create_held_directory(
                parent_fd,
                "code",
                logical_path="stage-22/code",
            )
        assert (tmp_path / "code/injected").read_bytes() == b""
    finally:
        os.close(parent_fd)


def test_publishable_branches_have_exact_tuple_manifest_schema_and_order(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _successful_compile
    result = _publish(passed_stage21, monkeypatch, compiler)
    assert result == pipeline_executor.StageResult(
        stage=Stage.EXPORT_PUBLISH,
        status=StageStatus.DONE,
        artifacts=(
            "paper_final.md",
            "code/",
            "stage22_export_manifest.json",
        ),
        evidence_refs=(
            "stage-22/paper_final.md",
            "stage-22/code/",
            "stage-22/stage22_export_manifest.json",
        ),
    ), result.error
    manifest = json.loads(
        (passed_stage21.run_dir / "stage-22/stage22_export_manifest.json").read_text()
    )
    assert set(manifest) == set(publication.STRUCTURED_STAGE22_MANIFEST_FIELDS)
    assert len(manifest) == 23
    assert manifest["schema_version"] == 2
    assert manifest["structured_capability_snapshot"] == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }
    assert all(
        set(entry) == {"role", "logical_name", "path", "sha256", "size"}
        and "kind" not in entry
        for entry in manifest["outputs"]
    )
    roles = [entry["role"] for entry in manifest["outputs"]]
    assert roles[:8] == list(publication.STRUCTURED_STAGE22_FIXED_ROLES)
    pdf_present = compiler is _successful_compile
    assert ("paper_pdf" in roles) is pdf_present
    if pdf_present:
        assert roles.index("paper_pdf") == 8
    assert roles[-2:] == ["code_support_file", "code_support_file"]
    assert [entry["logical_name"] for entry in manifest["outputs"][-2:]] == [
        "README.md",
        "requirements.txt",
    ]
    assert manifest["output_count"] == len(manifest["outputs"])
    assert (
        "paper.pdf"
        in {
            path.name
            for path in (passed_stage21.run_dir / "stage-22").iterdir()
        }
    ) is pdf_present


@pytest.mark.parametrize(
    ("fixture_name", "quality_outcome", "compiler"),
    [
        ("passed_stage21", "passed", _successful_compile),
        ("passed_stage21", "passed", _failed_compile),
        ("degraded_stage21", "degraded", _successful_compile),
        ("degraded_stage21", "degraded", _failed_compile),
    ],
)
def test_exact_four_branch_quality_and_compiler_matrix(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    quality_outcome: str,
    compiler,
) -> None:
    capture = request.getfixturevalue(fixture_name)
    result = _publish(capture, monkeypatch, compiler)
    assert result.status is StageStatus.DONE, result.error
    assert result.artifacts == publication.STRUCTURED_STAGE22_ARTIFACTS
    assert result.evidence_refs == publication.STRUCTURED_STAGE22_EVIDENCE_REFS
    manifest = json.loads(
        (capture.run_dir / "stage-22/stage22_export_manifest.json").read_text()
    )
    assert manifest["quality_outcome"] == quality_outcome
    if quality_outcome == "passed":
        assert manifest["degradation_signal"] is None
    else:
        assert set(manifest["degradation_signal"]) == {"path", "sha256", "size"}
        assert manifest["degradation_signal"]["path"] == "degradation_signal.json"
    assert manifest["compile"]["outcome"] == (
        "compiler-success"
        if compiler is _successful_compile
        else "compiler-failure"
    )


def test_manifest_parser_rejects_wrong_root_fileref_kind_and_order() -> None:
    base = {
        field: None for field in publication.STRUCTURED_STAGE22_MANIFEST_FIELDS
    }
    base.update(
        {
            "schema_version": 2,
            "publication_stage_id": "stage22",
            "publication_mode": "structured-scientific-claim-v1",
            "structured_capability_schema_version": 1,
            "structured_capability_snapshot": {
                "stage17_publication": 1,
                "stage19_revision": 1,
                "stage20_replay": 1,
                "stage24_and_release_integration": 0,
            },
            "generation_binding_sha256": "a" * 64,
            "canonical_experiment_evidence": {
                "path": "canonical_experiment_evidence.json",
                "sha256": "b" * 64,
                "size": 1,
            },
            "cfs": {"schema_version": 1, "sha256": "c" * 64},
            "selected_result_manifest": {
                "path": "stage-12/selected_result_manifest.json",
                "sha256": "d" * 64,
                "size": 1,
            },
            "source_stage19_manifest": {
                "path": "stage-19/scientific_claim_authority_manifest.json",
                "sha256": "e" * 64,
                "size": 1,
            },
            "source_stage20_manifest": {
                "path": "stage-20/quality_gate_manifest.json",
                "sha256": "f" * 64,
                "size": 1,
            },
            "source_stage21_manifest": {
                "path": "stage-21/bundle_index.json",
                "sha256": "1" * 64,
                "size": 1,
            },
            "source_stage21_archive": {
                "path": "stage-21/archive.md",
                "sha256": "2" * 64,
                "size": 1,
            },
            "source_paper": {
                "path": "stage-19/scientific_claim_paper_revised.md",
                "sha256": "3" * 64,
                "size": 1,
            },
            "quality_outcome": "passed",
            "degradation_signal": None,
            "bibliography_source": {
                "path": "stage-4/references.bib",
                "sha256": "4" * 64,
                "size": 1,
            },
            "template": {"name": "template", "files": []},
            "project_files": [],
            "compile": None,
            "output_count": 0,
            "outputs": [],
            "generated": "2026-07-28T00:00:00+00:00",
        }
    )
    wrong_root = dict(base, withdrawn=True)
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.replay_structured_stage22_manifest_v2(
            publication._canonical_json(wrong_root),
        )
    two_key = copy.deepcopy(base)
    del two_key["source_stage21_manifest"]["size"]
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.replay_structured_stage22_manifest_v2(
            publication._canonical_json(two_key),
        )
    with_kind = copy.deepcopy(base)
    with_kind["outputs"] = [
        {
            "role": "paper_markdown",
            "logical_name": None,
            "path": "stage-22/paper_final.md",
            "sha256": "5" * 64,
            "size": 1,
            "kind": "file",
        }
    ]
    with_kind["output_count"] = 1
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.replay_structured_stage22_manifest_v2(
            publication._canonical_json(with_kind),
        )


def test_immediate_and_terminal_mutation_withdraws_authority(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    attempt = publication.transition_stage22_pre_admission_context(
        passed_stage21.lease,
        pre,
    )
    provisional = publication.produce_structured_stage22(
        passed_stage21.lease,
        attempt,
    )
    publication.validate_structured_stage22_immediate_postcondition(
        passed_stage21.lease,
        provisional,
        artifacts=provisional.artifacts,
        evidence_refs=provisional.evidence_refs,
        context=attempt,
    )
    (passed_stage21.run_dir / "stage-22/paper_final.md").write_bytes(b"mutated")
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.validate_structured_stage22_terminal_postcondition(
            passed_stage21.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    publication.fail_structured_stage22_attempt(passed_stage21.lease, attempt)
    assert not (
        passed_stage21.run_dir / "stage-22/stage22_export_manifest.json"
    ).exists()


def test_private_executor_failure_is_exact_retry_and_empty_tuples(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_success(tex_path: Path, *, max_attempts: int):
        assert max_attempts == 2
        (tex_path.parent / "paper.pdf").write_bytes(b"not a pdf")
        return SimpleNamespace(success=True, attempts=1, errors=())

    result = _publish(passed_stage21, monkeypatch, invalid_success)
    assert result.stage is Stage.EXPORT_PUBLISH
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        passed_stage21.run_dir / "stage-22/stage22_export_manifest.json"
    ).exists()


def _assert_failed_retry(result) -> None:
    assert result.stage is Stage.EXPORT_PUBLISH
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert result.artifacts == ()
    assert result.evidence_refs == ()


def test_post_capture_stage_replacement_never_writes_replacement(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)
    original = publication._compile_deterministic

    def replace_stage_after_compile(*args, **kwargs):
        result = original(*args, **kwargs)
        run_dir = passed_stage21.run_dir
        (run_dir / "stage-22").rename(run_dir / "stage-22-detached")
        (run_dir / "stage-22").mkdir()
        return result

    monkeypatch.setattr(publication, "_compile_deterministic", replace_stage_after_compile)
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    result = pipeline_executor._execute_structured_stage22_private(
        passed_stage21.lease, pre
    )
    _assert_failed_retry(result)
    replacement = passed_stage21.run_dir / "stage-22"
    assert replacement.is_dir()
    assert tuple(replacement.iterdir()) == ()


def test_post_capture_code_replacement_never_writes_replacement(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)
    original = publication._create_held_directory

    def replace_code_after_capture(parent_fd, name, *, logical_path):
        held = original(parent_fd, name, logical_path=logical_path)
        if logical_path == "stage-22/code":
            stage = passed_stage21.run_dir / "stage-22"
            (stage / "code").rename(stage / "code-detached")
            (stage / "code").mkdir()
        return held

    monkeypatch.setattr(
        publication, "_create_held_directory", replace_code_after_capture
    )
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    result = pipeline_executor._execute_structured_stage22_private(
        passed_stage21.lease, pre
    )
    _assert_failed_retry(result)
    replacement = passed_stage21.run_dir / "stage-22/code"
    assert replacement.is_dir()
    assert tuple(replacement.iterdir()) == ()


def test_post_capture_nested_replacement_blocks_first_child_write(
    tmp_path: Path,
) -> None:
    stage_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    code = publication._create_held_directory(
        stage_fd, "code", logical_path="stage-22/code"
    )
    nested = publication._create_held_directory(
        code.descriptor, "pkg", logical_path="stage-22/code/pkg"
    )
    try:
        assert os.get_inheritable(code.descriptor) is False
        assert os.get_inheritable(nested.descriptor) is False
        (tmp_path / "code/pkg").rename(tmp_path / "code/pkg-detached")
        (tmp_path / "code/pkg").mkdir()
        with pytest.raises(
            publication.StructuredStage22PublicationError,
            match="directory identity",
        ):
            publication._require_directory_identity(nested)
        assert tuple((tmp_path / "code/pkg").iterdir()) == ()
        assert not (tmp_path / "code/pkg/main.py").exists()
    finally:
        os.close(nested.descriptor)
        os.close(code.descriptor)
        os.close(stage_fd)


@pytest.mark.parametrize("fixpoint_call", [1, 2])
def test_upstream_withdrawal_at_each_fixpoint_fails_closed(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
    fixpoint_call: int,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)
    original = publication._verify_upstream_fixpoint
    calls = 0

    def withdraw_before_fixpoint(lease, expected):
        nonlocal calls
        calls += 1
        if calls == fixpoint_call:
            path = passed_stage21.run_dir / "stage-21/bundle_index.json"
            path.write_bytes(path.read_bytes() + b"\n")
        return original(lease, expected)

    monkeypatch.setattr(
        publication, "_verify_upstream_fixpoint", withdraw_before_fixpoint
    )
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    result = pipeline_executor._execute_structured_stage22_private(
        passed_stage21.lease, pre
    )
    _assert_failed_retry(result)
    assert not (
        passed_stage21.run_dir / "stage-22/stage22_export_manifest.json"
    ).exists()


def test_synchronized_payload_and_stored_manifest_forgery_is_rejected(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    attempt = publication.transition_stage22_pre_admission_context(
        passed_stage21.lease, pre
    )
    provisional = publication.produce_structured_stage22(
        passed_stage21.lease, attempt
    )
    publication.validate_structured_stage22_immediate_postcondition(
        passed_stage21.lease,
        provisional,
        artifacts=provisional.artifacts,
        evidence_refs=provisional.evidence_refs,
        context=attempt,
    )
    forged = b"forged but self-consistent stored bytes"
    stage = passed_stage21.run_dir / "stage-22"
    (stage / "paper_final.md").write_bytes(forged)
    stored = json.loads((stage / "stage22_export_manifest.json").read_text())
    paper_entry = next(
        entry for entry in stored["outputs"] if entry["role"] == "paper_markdown"
    )
    paper_entry["sha256"] = hashlib.sha256(forged).hexdigest()
    paper_entry["size"] = len(forged)
    (stage / "stage22_export_manifest.json").write_bytes(
        publication._canonical_json(stored)
    )
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication.validate_structured_stage22_terminal_postcondition(
            passed_stage21.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    publication.fail_structured_stage22_attempt(passed_stage21.lease, attempt)
    assert not (stage / "stage22_export_manifest.json").exists()


def test_primary_failure_precedes_real_cleanup_collision(
    passed_stage21,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publication, "compile_latex", _successful_compile)

    def collide_then_fail(*_args, **_kwargs):
        manifest = (
            passed_stage21.run_dir / "stage-22/stage22_export_manifest.json"
        )
        manifest.unlink()
        manifest.mkdir()
        raise RuntimeError("primary-failure")

    monkeypatch.setattr(
        publication,
        "validate_structured_stage22_terminal_postcondition",
        collide_then_fail,
    )
    pre = publication.issue_stage22_pre_admission_context(passed_stage21.lease)
    result = pipeline_executor._execute_structured_stage22_private(
        passed_stage21.lease, pre
    )
    _assert_failed_retry(result)
    assert result.error.startswith("Structured Stage 22 failed: primary-failure")
    assert "structured Stage 22 cleanup also failed:" in result.error
    assert "file identity collision" in result.error


def test_preexisting_expected_leaf_is_preserved_by_exclusive_creation(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "main.py"
    existing.write_bytes(b"prior")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            publication._create_held_file(
                parent_fd,
                "main.py",
                b"new",
                logical_path="stage-22/code/main.py",
            )
    finally:
        os.close(parent_fd)
    assert existing.read_bytes() == b"prior"


@pytest.mark.parametrize(
    "paths",
    [
        ("pkg", "pkg/main.py"),
        ("A.py", "a.py"),
        ("\u00e9.py", "e\u0301.py"),
        ("README.md", "README.md"),
    ],
)
def test_nested_code_paths_reject_prefix_case_nfc_and_duplicates(paths) -> None:
    with pytest.raises(publication.StructuredStage22PublicationError):
        publication._validate_logical_paths(paths)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "socket", "device", "empty"]
)
def test_existing_nested_tree_rejects_special_hardlink_and_empty_directory(
    tmp_path: Path,
    kind: str,
) -> None:
    del tmp_path
    with tempfile.TemporaryDirectory(
        prefix="b5a2-tree-", dir="/private/tmp"
    ) as temporary:
        root = Path(temporary)
        code = root / "code"
        if kind == "symlink":
            code.symlink_to(root / "outside")
        elif kind == "hardlink":
            outside = root / "outside"
            outside.write_bytes(b"x")
            os.link(outside, code)
        elif kind == "fifo":
            os.mkfifo(code)
        elif kind == "socket":
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.bind(str(code))
            except PermissionError:
                sock.close()
                pytest.skip("sandbox forbids AF_UNIX fixture creation")
        elif kind == "device":
            try:
                os.mknod(code, stat.S_IFCHR | 0o600, os.makedev(0, 0))
            except (AttributeError, PermissionError, OSError):
                pytest.skip("environment cannot create a device-node fixture")
        else:
            code.mkdir()
        stage_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(
                publication.StructuredStage22PublicationError,
                match="already exists",
            ):
                publication._create_held_directory(
                    stage_fd,
                    "code",
                    logical_path="stage-22/code",
                )
            assert code.exists() or code.is_symlink()
        finally:
            os.close(stage_fd)
        if kind == "socket":
            sock.close()


def _compiler_deterministic() -> Stage22DeterministicOutputs:
    return Stage22DeterministicOutputs(
        direct_files={
            "paper.tex": b"paper",
            "references.bib": b"refs",
        },
        code_files={},
        template_name="fixture",
        template_files=(),
    )


def test_compiler_rejects_second_semantic_call_third_attempt_and_input_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        publication.StructuredStage22PublicationError,
        match="already consumed",
    ):
        publication._compile_deterministic(
            _compiler_deterministic(),
            SimpleNamespace(semantic_calls=1),
        )

    monkeypatch.setattr(
        publication,
        "compile_latex",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=False, attempts=3, errors=("failure",)
        ),
    )
    with pytest.raises(
        publication.StructuredStage22PublicationError,
        match="attempts mismatch",
    ):
        publication._compile_deterministic(
            _compiler_deterministic(),
            SimpleNamespace(semantic_calls=0),
        )

    def mutating(tex_path: Path, *, max_attempts: int):
        assert max_attempts == 2
        tex_path.write_bytes(b"changed")
        return SimpleNamespace(success=False, attempts=1, errors=("failure",))

    monkeypatch.setattr(publication, "compile_latex", mutating)
    with pytest.raises(
        publication.StructuredStage22PublicationError,
        match="changed deterministic input",
    ):
        publication._compile_deterministic(
            _compiler_deterministic(),
            SimpleNamespace(semantic_calls=0),
        )


def test_compiler_failure_discards_residual_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_with_pdf(tex_path: Path, *, max_attempts: int):
        assert max_attempts == 2
        (tex_path.parent / "paper.pdf").write_bytes(_valid_pdf())
        return SimpleNamespace(success=False, attempts=1, errors=("failure",))

    monkeypatch.setattr(publication, "compile_latex", failing_with_pdf)
    outputs, status = publication._compile_deterministic(
        _compiler_deterministic(),
        SimpleNamespace(semantic_calls=0),
    )
    assert outputs == {}
    assert status["paper_pdf"] is None
    assert status["status"] == "latex_error"


def test_tuple_slashes_and_order_are_normative() -> None:
    assert publication.STRUCTURED_STAGE22_ARTIFACTS == (
        "paper_final.md",
        "code/",
        "stage22_export_manifest.json",
    )
    assert publication.STRUCTURED_STAGE22_EVIDENCE_REFS == (
        "stage-22/paper_final.md",
        "stage-22/code/",
        "stage-22/stage22_export_manifest.json",
    )
    for wrong in (
        ("paper_final.md", "code", "stage22_export_manifest.json"),
        ("paper_final.md", "paper.tex", "stage22_export_manifest.json"),
        ("code/", "paper_final.md", "stage22_export_manifest.json"),
    ):
        assert wrong != publication.STRUCTURED_STAGE22_ARTIFACTS
