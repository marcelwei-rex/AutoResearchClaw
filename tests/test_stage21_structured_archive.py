"""B5-A1 inactive structured Stage 21 deterministic archive contracts."""

from __future__ import annotations

import copy
import gc
import hashlib
import inspect
import json
import os
import pickle
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from researchclaw.pipeline import executor as pipeline_executor
from researchclaw.pipeline import (
    stage21_structured_authority as authority,
)
from researchclaw.pipeline import (
    stage21_structured_publication as publication,
)
from researchclaw.pipeline import (
    structured_scientific_claim_capabilities as capability,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_stage17_structured_producer_integration import active_capture
from tests.test_stage20_structured_replay import (
    _QualityClient,
    _prepare_stage20_source,
    _quality_response,
    _run_private,
)


def _authority_inputs(
    outcome: str = "passed",
) -> authority.Stage21AuthorityInputs:
    roles = [
        ("canonical_experiment_evidence", "canonical_experiment_evidence.json"),
        (
            "source_stage19_manifest",
            "stage-19/scientific_claim_authority_manifest.json",
        ),
        ("source_paper", "stage-19/scientific_claim_paper_revised.md"),
        ("stage20_quality_report", "stage-20/quality_report.json"),
        (
            "stage20_fabrication_flags",
            "stage-20/fabrication_flags.json",
        ),
    ]
    if outcome == "degraded":
        roles.append(
            ("stage20_degradation_signal", "degradation_signal.json")
        )
    roles.append(
        ("source_stage20_manifest", "stage-20/quality_gate_manifest.json")
    )
    return authority.Stage21AuthorityInputs(
        "a" * 64,
        "b" * 64,
        outcome,
        "2026-07-28T00:00:00+00:00",
        tuple(
            authority.BoundSource(role, path, f"{index:064x}", index + 1)
            for index, (role, path) in enumerate(roles, start=1)
        ),
    )


@pytest.fixture
def passed_stage20(active_capture, monkeypatch: pytest.MonkeyPatch):
    _prepare_stage20_source(active_capture, monkeypatch)
    result = _run_private(
        active_capture,
        llm=_QualityClient([_quality_response()]),
    )
    assert result.status is StageStatus.DONE
    from researchclaw.pipeline.stage20_structured_publication import (
        clear_structured_stage20_published_context,
    )

    clear_structured_stage20_published_context(active_capture.lease)
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
    return active_capture


@pytest.fixture
def degraded_stage20(active_capture, monkeypatch: pytest.MonkeyPatch):
    _prepare_stage20_source(active_capture, monkeypatch)
    result = _run_private(
        active_capture,
        llm=_QualityClient(
            [_quality_response(score=3.0, verdict="revise")]
        ),
    )
    assert result.status is StageStatus.DONE
    from researchclaw.pipeline.stage20_structured_publication import (
        clear_structured_stage20_published_context,
    )

    clear_structured_stage20_published_context(active_capture.lease)
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
    return active_capture


def _publish(capture):
    pre = publication.issue_stage21_pre_admission_context(capture.lease)
    return pipeline_executor._execute_structured_stage21_private(
        capture.lease,
        pre,
    )


def test_b5a1_keeps_capability_exactly_1110() -> None:
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }


def test_public_direct_guard_is_first_and_has_no_caller_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_lock(*_args: object, **_kwargs: object) -> None:
        nonlocal touched
        touched = True
        raise AssertionError("structured Stage 21 touched I/O")

    assert tuple(
        inspect.signature(
            publication.execute_structured_stage21_archive
        ).parameters
    ) == ("run_dir", "stage_dir")
    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(
        capability.StructuredScientificClaimCapabilityIncomplete
    ):
        publication.execute_structured_stage21_archive(
            tmp_path / "missing",
            tmp_path / "missing/stage-21",
        )
    assert touched is False


@pytest.mark.parametrize(
    "malformed",
    (
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": True,
            "stage24_and_release_integration": 0,
        },
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
        },
        {
            "stage17_publication": 1,
            "stage19_revision": 1,
            "stage20_replay": 1,
            "stage24_and_release_integration": 0,
            "extra": 0,
        },
    ),
)
def test_public_guard_rejects_malformed_map_before_lock(
    malformed: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_lock(*_args, **_kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("malformed capability reached lock")

    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        malformed,
    )
    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        publication.execute_structured_stage21_archive(
            tmp_path / "missing",
            tmp_path / "missing/stage-21",
        )
    assert touched is False


def test_public_entry_rejects_caller_supplied_1111_before_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = False

    def forbidden_lock(*_args, **_kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("caller authority reached lock")

    monkeypatch.setattr(publication.ReleaseGraphLock, "acquire", forbidden_lock)
    with pytest.raises(TypeError):
        publication.execute_structured_stage21_archive(
            tmp_path / "missing",
            tmp_path / "missing/stage-21",
            capability_snapshot=(1, 1, 1, 1),  # type: ignore[call-arg]
        )
    assert touched is False


def test_pre_admission_has_zero_stage21_io_and_no_stage21_identity(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = passed_stage20.run_dir
    assert not (run_dir / "stage-21").exists()
    opened: list[str] = []
    real_open = passed_stage20.lease.open_stage_namespace

    def watched_open(stage_name: str, **kwargs):
        opened.append(stage_name)
        return real_open(stage_name, **kwargs)

    monkeypatch.setattr(
        passed_stage20.lease, "open_stage_namespace", watched_open
    )
    pre = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    assert "stage-21" not in opened
    assert not (run_dir / "stage-21").exists()
    forbidden = {
        "_stage_identity",
        "_stage_fd",
        "stage_identity",
        "stage_fd",
        "stage_exists",
    }
    assert forbidden.isdisjoint(dir(pre))
    assert pre.logical_stage_id == "stage21"


def test_pre_to_attempt_transition_is_single_use_and_forgery_rejects(
    passed_stage20,
) -> None:
    pre = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    attempt = publication.transition_stage21_pre_admission_context(
        passed_stage20.lease,
        pre,
    )
    assert publication.context_phase(attempt) == "namespace_bound"
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.transition_stage21_pre_admission_context(
            passed_stage20.lease,
            pre,
        )
    forged = object.__new__(publication.Stage21AttemptContext)
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.produce_structured_stage21(
            passed_stage20.lease,
            forged,
        )


def test_contexts_are_noncopyable_nonserializable(passed_stage20) -> None:
    pre = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    with pytest.raises(TypeError):
        copy.copy(pre)
    with pytest.raises(TypeError):
        copy.deepcopy(pre)
    with pytest.raises(TypeError):
        pickle.dumps(pre)
    record = publication._PRE_CONTEXTS[pre]
    descriptors = (
        record.capture.root_parent_fd,
        record.capture.root_signal_parent_fd,
    )
    del pre
    gc.collect()
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_pre_context_plain_expired_wrong_stage_run_and_inactive_reject(
    passed_stage20,
    tmp_path: Path,
) -> None:
    stage21 = passed_stage20.run_dir / "stage-21"
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.transition_stage21_pre_admission_context(
            passed_stage20.lease,
            object(),
        )
    assert not stage21.exists()

    expired = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    expired_record = publication._PRE_CONTEXTS.pop(expired)
    expired_record.finalizer()
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.transition_stage21_pre_admission_context(
            passed_stage20.lease,
            expired,
        )
    assert not stage21.exists()

    wrong_stage = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    object.__setattr__(wrong_stage, "logical_stage_id", "stage20")
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.transition_stage21_pre_admission_context(
            passed_stage20.lease,
            wrong_stage,
        )
    wrong_stage_record = publication._PRE_CONTEXTS.pop(wrong_stage)
    wrong_stage_record.finalizer()
    assert not stage21.exists()

    wrong_owner = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    object.__setattr__(wrong_owner, "_writer_owner", object())
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.transition_stage21_pre_admission_context(
            passed_stage20.lease,
            wrong_owner,
        )
    wrong_owner_record = publication._PRE_CONTEXTS.pop(wrong_owner)
    wrong_owner_record.finalizer()
    assert not stage21.exists()

    wrong_run_context = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    other_run = tmp_path / "other-run"
    other_run.mkdir()
    with passed_stage20.lease.__class__.acquire(
        other_run, "stage21-wrong-run", mode="write"
    ) as wrong_run:
        with pytest.raises(publication.StructuredStage21PublicationError):
            publication.transition_stage21_pre_admission_context(
                wrong_run,
                wrong_run_context,
            )
    wrong_run_record = publication._PRE_CONTEXTS.pop(wrong_run_context)
    wrong_run_record.finalizer()

    inactive_context = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    inactive = passed_stage20.lease.__class__.acquire(
        passed_stage20.run_dir,
        "stage21-inactive",
        mode="write",
    )
    inactive.close()
    with pytest.raises((RuntimeError, publication.StructuredStage21PublicationError)):
        publication.transition_stage21_pre_admission_context(
            inactive,
            inactive_context,
        )
    inactive_record = publication._PRE_CONTEXTS.pop(inactive_context)
    inactive_record.finalizer()
    assert not stage21.exists()


def test_wrong_attempt_identity_rejects_before_cleanup_or_write(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    attempt = publication.transition_stage21_pre_admission_context(
        passed_stage20.lease,
        pre,
    )
    original_identity = attempt._stage_identity
    object.__setattr__(attempt, "_stage_identity", (-1, -1))
    touched = False

    def forbidden_cleanup(_namespace) -> tuple[str, ...]:
        nonlocal touched
        touched = True
        raise AssertionError("wrong context reached cleanup")

    monkeypatch.setattr(
        publication, "_invalidate_owned_namespace", forbidden_cleanup
    )
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.produce_structured_stage21(
            passed_stage20.lease,
            attempt,
        )
    assert touched is False
    object.__setattr__(attempt, "_stage_identity", original_identity)
    assert publication.fail_structured_stage21_attempt(
        passed_stage20.lease,
        attempt,
    ) == ()


@pytest.mark.parametrize(
    ("fixture_name", "outcome", "count"),
    (
        ("passed_stage20", "passed", 7),
        ("degraded_stage20", "degraded", 8),
    ),
)
def test_real_publication_exact_archive_and_17_key_manifest(
    request: pytest.FixtureRequest,
    fixture_name: str,
    outcome: str,
    count: int,
) -> None:
    capture = request.getfixturevalue(fixture_name)
    result = _publish(capture)
    assert result.status is StageStatus.DONE
    assert result.artifacts == ("archive.md", "bundle_index.json")
    assert result.evidence_refs == (
        "stage-21/archive.md",
        "stage-21/bundle_index.json",
    )
    stage21 = capture.run_dir / "stage-21"
    archive_bytes = (stage21 / "archive.md").read_bytes()
    index_bytes = (stage21 / "bundle_index.json").read_bytes()
    parsed = authority.parse_bundle_index_v2(index_bytes)
    assert len(parsed) == 17
    assert parsed["schema_version"] == 2
    assert parsed["quality_outcome"] == outcome
    assert parsed["artifact_count"] == count
    assert archive_bytes == authority.render_archive_from_manifest(parsed)
    assert archive_bytes.endswith(b"\n")
    assert b"\r" not in archive_bytes
    assert not (stage21 / "archive.md.tmp").exists()
    assert not (stage21 / "bundle_index.json.tmp").exists()


def test_manifest_parser_rejects_boolean_integer_and_extra_key() -> None:
    inputs = _authority_inputs()
    archive = authority.render_archive(inputs)
    index = authority.build_bundle_index_v2(
        inputs, archive_bytes=archive
    )
    payload = json.loads(index)
    payload["artifact_count"] = True
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
    payload["artifact_count"] = 7
    payload["extra"] = "forged"
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("schema_version",), True),
        (("publication_mode",), "generic-v1"),
        (("structured_capability_schema_version",), False),
        (
            (
                "structured_capability_snapshot",
                "stage20_replay",
            ),
            True,
        ),
        (("generation_binding_sha256",), "A" * 64),
        (("cfs", "schema_version"), True),
        (
            ("canonical_experiment_evidence", "path"),
            "../canonical_experiment_evidence.json",
        ),
        (("quality_outcome",), "unknown"),
        (("artifact_count",), 8),
        (("artifacts", 0, "role"), "source_paper"),
        (("artifacts", 0, "path"), "/unsafe"),
        (("artifacts", 0, "sha256"), "f" * 63),
        (("artifacts", 0, "size"), True),
        (("generated",), "2026-07-28T00:00:00Z"),
        (("generated",), "2026-07-28T00:00:00.1+00:00"),
    ),
)
def test_manifest_parser_rejects_nested_schema_attacks(
    path: tuple[object, ...],
    value: object,
) -> None:
    inputs = _authority_inputs()
    archive = authority.render_archive(inputs)
    payload = json.loads(
        authority.build_bundle_index_v2(inputs, archive_bytes=archive)
    )
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    attacked = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(attacked)


def test_manifest_parser_rejects_duplicate_missing_and_crossed_branch() -> None:
    inputs = _authority_inputs()
    archive = authority.render_archive(inputs)
    index = authority.build_bundle_index_v2(
        inputs, archive_bytes=archive
    )
    duplicate = index.replace(
        b'{"archive":',
        b'{"schema_version":2,"archive":',
        1,
    )
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(duplicate)
    payload = json.loads(index)
    del payload["cfs"]
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
    payload = json.loads(index)
    payload["degradation_signal"] = {
        "path": "degradation_signal.json",
        "sha256": "f" * 64,
    }
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.parse_bundle_index_v2(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )


def test_manifest_replay_rejects_changed_inherited_generation() -> None:
    inputs = _authority_inputs()
    archive = authority.render_archive(inputs)
    index = authority.build_bundle_index_v2(
        inputs, archive_bytes=archive
    )
    payload = json.loads(index)
    payload["generated"] = "2026-07-28T00:00:01+00:00"
    forged = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.replay_bundle_index_v2(
            forged,
            archive_bytes=archive,
            expected=index,
        )


def test_synchronized_archive_index_tamper_rejects() -> None:
    inputs = _authority_inputs()
    original_archive = authority.render_archive(inputs)
    original_index = authority.build_bundle_index_v2(
        inputs, archive_bytes=original_archive
    )
    archive = original_archive.replace(
        b"deterministic operational projection",
        b"synchronized forged projection",
    )
    payload = json.loads(original_index)
    payload["archive"]["sha256"] = hashlib.sha256(archive).hexdigest()
    for row in payload["artifacts"]:
        if row["role"] == "stage21_archive":
            row["sha256"] = hashlib.sha256(archive).hexdigest()
            row["size"] = len(archive)
    forged_index = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(authority.StructuredStage21AuthorityError):
        authority.replay_bundle_index_v2(
            forged_index,
            archive_bytes=archive,
            expected=original_index,
        )


@pytest.mark.parametrize(
    "collision",
    ("regular", "symlink", "hardlink", "fifo", "socket", "directory"),
)
def test_preexisting_temp_collision_fails_closed(
    passed_stage20,
    tmp_path: Path,
    collision: str,
) -> None:
    stage21 = passed_stage20.run_dir / "stage-21"
    stage21.mkdir()
    target = stage21 / "archive.md.tmp"
    outside: Path | None = None
    unix_socket: socket.socket | None = None
    if collision == "regular":
        target.write_text("collision", encoding="utf-8")
    elif collision == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside", encoding="utf-8")
        target.symlink_to(outside)
    elif collision == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text("outside", encoding="utf-8")
        os.link(outside, target)
    elif collision == "fifo":
        os.mkfifo(target)
    elif collision == "socket":
        unix_socket = socket.socket(socket.AF_UNIX)
        previous_cwd = Path.cwd()
        try:
            os.chdir(stage21)
            try:
                unix_socket.bind(target.name)
            except PermissionError:
                unix_socket.close()
                unix_socket = None
                pytest.skip("sandbox forbids AF_UNIX socket creation")
        finally:
            os.chdir(previous_cwd)
    else:
        target.mkdir()
    try:
        result = _publish(passed_stage20)
    finally:
        if unix_socket is not None:
            unix_socket.close()
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (stage21 / "bundle_index.json").exists()
    if outside is not None:
        assert outside.read_text(encoding="utf-8") == "outside"


def test_preexisting_formal_aliases_are_invalidated_without_target_write(
    passed_stage20,
    tmp_path: Path,
) -> None:
    stage21 = passed_stage20.run_dir / "stage-21"
    stage21.mkdir()
    archive_target = tmp_path / "archive-target"
    index_target = tmp_path / "index-target"
    archive_target.write_text("archive target", encoding="utf-8")
    index_target.write_text("index target", encoding="utf-8")
    os.link(archive_target, stage21 / "archive.md")
    (stage21 / "bundle_index.json").symlink_to(index_target)
    result = _publish(passed_stage20)
    assert result.status is StageStatus.DONE
    assert archive_target.read_text(encoding="utf-8") == "archive target"
    assert index_target.read_text(encoding="utf-8") == "index target"


def test_late_formal_collision_fails_without_target_write(
    passed_stage20,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    original = publication._create_held_file
    injected = False

    def collide(directory_fd: int, name: str, content: bytes):
        nonlocal injected
        if name == "archive.md" and not injected:
            injected = True
            os.symlink(outside, name, dir_fd=directory_fd)
        return original(directory_fd, name, content)

    monkeypatch.setattr(publication, "_create_held_file", collide)
    result = _publish(passed_stage20)
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert not (
        passed_stage20.run_dir / "stage-21/bundle_index.json"
    ).exists()


def test_stage21_parent_replacement_cleanup_uses_held_original(
    passed_stage20,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = publication._invalidate_owned_namespace
    detached = tmp_path / "detached-stage-21"
    replaced = False

    def replace_parent(namespace):
        nonlocal replaced
        if not replaced:
            replaced = True
            os.rename(
                "stage-21",
                detached,
                src_dir_fd=namespace._run_fd,
            )
            os.mkdir("stage-21", dir_fd=namespace._run_fd)
            replacement_fd = os.open(
                "stage-21",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=namespace._run_fd,
            )
            try:
                marker = os.open(
                    "replacement-sentinel",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=replacement_fd,
                )
                os.write(marker, b"replacement")
                os.close(marker)
            finally:
                os.close(replacement_fd)
        return original(namespace)

    monkeypatch.setattr(
        publication, "_invalidate_owned_namespace", replace_parent
    )
    result = _publish(passed_stage20)
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert (
        passed_stage20.run_dir
        / "stage-21/replacement-sentinel"
    ).read_bytes() == b"replacement"
    assert not (detached / "bundle_index.json").exists()


def test_source_same_bytes_inode_replacement_fails_fixpoint(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = publication._capture_upstream
    calls = 0

    def replace_before_snapshot_a(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            source = (
                passed_stage20.run_dir
                / "stage-20/quality_report.json"
            )
            content = source.read_bytes()
            source.unlink()
            source.write_bytes(content)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication, "_capture_upstream", replace_before_snapshot_a
    )
    result = _publish(passed_stage20)
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert not (passed_stage20.run_dir / "stage-21").exists()


@pytest.mark.parametrize("capture_call", (3, 4, 5, 6, 7, 8))
def test_source_replacement_at_each_stage21_capture_boundary_fails(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
    capture_call: int,
) -> None:
    original = publication._capture_upstream
    calls = 0

    def replace_at_boundary(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == capture_call:
            source = (
                passed_stage20.run_dir
                / "stage-20/quality_report.json"
            )
            content = source.read_bytes()
            source.unlink()
            source.write_bytes(content)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication, "_capture_upstream", replace_at_boundary
    )
    result = _publish(passed_stage20)
    assert calls >= capture_call
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        passed_stage20.run_dir / "stage-21/bundle_index.json"
    ).exists()


def test_root_run_stage20_and_signal_parent_identity_drift_precedes_stage21_io(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for field in (
        "root_parent_identity",
        "run_identity",
        "stage20_identity",
        "root_signal_parent_identity",
    ):
        pre = publication.issue_stage21_pre_admission_context(
            passed_stage20.lease
        )
        original = publication._capture_upstream

        def drifted_capture(*args, **kwargs):
            captured = original(*args, **kwargs)
            captured.snapshot = replace(
                captured.snapshot,
                **{field: (-1, -1)},
            )
            return captured

        with monkeypatch.context() as scoped:
            scoped.setattr(
                publication, "_capture_upstream", drifted_capture
            )
            result = pipeline_executor._execute_structured_stage21_private(
                passed_stage20.lease,
                pre,
            )
        assert result.status is StageStatus.FAILED
        assert result.artifacts == ()
        assert result.evidence_refs == ()
        assert not (passed_stage20.run_dir / "stage-21").exists()


def test_malformed_stage20_rejects_before_any_stage21_io(
    passed_stage20,
) -> None:
    from researchclaw.pipeline.stage20_structured_authority import (
        StructuredStage20AuthorityError,
    )

    report = passed_stage20.run_dir / "stage-20/quality_report.json"
    payload = json.loads(report.read_bytes())
    payload["forged"] = True
    report.write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    with pytest.raises(StructuredStage20AuthorityError):
        publication.issue_stage21_pre_admission_context(
            passed_stage20.lease
        )
    assert not (passed_stage20.run_dir / "stage-21").exists()


def test_late_stage20_manifest_mutation_withdraws_outputs(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = publication._capture_final_state
    injected = False

    def mutate_before_final_capture(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            manifest = (
                passed_stage20.run_dir
                / "stage-20/quality_gate_manifest.json"
            )
            manifest.write_bytes(manifest.read_bytes() + b"\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        publication, "_capture_final_state", mutate_before_final_capture
    )
    result = _publish(passed_stage20)
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        passed_stage20.run_dir / "stage-21/bundle_index.json"
    ).exists()


def test_creating_fd_name_identity_drift_fails_closed(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = publication._read_held_file
    injected = False

    def replace_temp_name(held):
        nonlocal injected
        if held.name == "archive.md.tmp" and not injected:
            injected = True
            os.unlink(held.name, dir_fd=held.directory_fd)
            replacement = os.open(
                held.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=held.directory_fd,
            )
            os.write(replacement, held.expected_bytes)
            os.close(replacement)
        return original(held)

    monkeypatch.setattr(publication, "_read_held_file", replace_temp_name)
    result = _publish(passed_stage20)
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        passed_stage20.run_dir / "stage-21/bundle_index.json"
    ).exists()


def test_immediate_and_terminal_tuple_or_context_mutation_fails(
    passed_stage20,
) -> None:
    pre = publication.issue_stage21_pre_admission_context(
        passed_stage20.lease
    )
    attempt = publication.transition_stage21_pre_admission_context(
        passed_stage20.lease,
        pre,
    )
    provisional = publication.produce_structured_stage21(
        passed_stage20.lease,
        attempt,
    )
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.validate_structured_stage21_immediate_postcondition(
            passed_stage20.lease,
            provisional,
            artifacts=("bundle_index.json", "archive.md"),
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    forged_evidence = replace(
        provisional,
        evidence_refs=tuple(reversed(provisional.evidence_refs)),
    )
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.validate_structured_stage21_immediate_postcondition(
            passed_stage20.lease,
            forged_evidence,
            artifacts=provisional.artifacts,
            evidence_refs=forged_evidence.evidence_refs,
            context=attempt,
        )
    forged_status = replace(provisional, status=StageStatus.FAILED)
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.validate_structured_stage21_immediate_postcondition(
            passed_stage20.lease,
            forged_status,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    publication.validate_structured_stage21_immediate_postcondition(
        passed_stage20.lease,
        provisional,
        artifacts=provisional.artifacts,
        evidence_refs=provisional.evidence_refs,
        context=attempt,
    )
    forged_context = object.__new__(publication.Stage21AttemptContext)
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.validate_structured_stage21_terminal_postcondition(
            passed_stage20.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=forged_context,
        )
    record = publication._ATTEMPT_CONTEXTS[attempt]
    assert record.formal_archive is not None
    os.pwrite(record.formal_archive.descriptor, b"X", 0)
    os.fsync(record.formal_archive.descriptor)
    with pytest.raises(publication.StructuredStage21PublicationError):
        publication.validate_structured_stage21_terminal_postcondition(
            passed_stage20.lease,
            provisional,
            artifacts=provisional.artifacts,
            evidence_refs=provisional.evidence_refs,
            context=attempt,
        )
    assert publication.fail_structured_stage21_attempt(
        passed_stage20.lease,
        attempt,
    ) == ()


def test_raising_provider_prompt_hitl_prm_and_clock_spies_are_unused(
    passed_stage20,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from researchclaw.metaclaw_bridge import prm_gate
    from researchclaw.pipeline import experiment_repair
    from researchclaw.pipeline.stage_impls import _review_publish

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("structured Stage 21 invoked a forbidden hook")

    monkeypatch.setattr(
        pipeline_executor, "_create_configured_llm", forbidden
    )
    monkeypatch.setattr(pipeline_executor, "PromptManager", forbidden)
    monkeypatch.setattr(pipeline_executor, "_run_hitl_pre_stage", forbidden)
    monkeypatch.setattr(pipeline_executor, "_run_hitl_post_stage", forbidden)
    monkeypatch.setattr(pipeline_executor, "_run_collaboration_loop", forbidden)
    monkeypatch.setattr(_review_publish, "_chat_with_prompt", forbidden)
    monkeypatch.setattr(_review_publish, "_utcnow_iso", forbidden)
    monkeypatch.setattr(experiment_repair, "run_repair_loop", forbidden)
    monkeypatch.setattr(
        prm_gate.ResearchPRMGate,
        "from_bridge_config",
        forbidden,
    )
    result = _publish(passed_stage20)
    assert result.status is StageStatus.DONE
    assert calls == []
    producer_source = inspect.getsource(
        publication.produce_structured_stage21
    )
    assert "_utcnow" not in producer_source
    assert "datetime.now" not in producer_source
    assert "time." not in producer_source


def test_private_executor_orders_both_postconditions_and_cleans_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    events: list[str] = []
    attempt = object()
    provisional = SimpleNamespace(
        artifacts=("archive.md", "bundle_index.json"),
        evidence_refs=(
            "stage-21/archive.md",
            "stage-21/bundle_index.json",
        ),
    )

    monkeypatch.setattr(
        publication,
        "transition_stage21_pre_admission_context",
        lambda *_args: events.append("transition") or attempt,
    )
    monkeypatch.setattr(
        publication,
        "produce_structured_stage21",
        lambda *_args: events.append("produce") or provisional,
    )
    monkeypatch.setattr(
        publication,
        "validate_structured_stage21_immediate_postcondition",
        lambda *_args, **_kwargs: events.append("immediate"),
    )

    def terminal_failure(*_args, **_kwargs):
        events.append("terminal")
        raise RuntimeError("terminal mutation")

    monkeypatch.setattr(
        publication,
        "validate_structured_stage21_terminal_postcondition",
        terminal_failure,
    )
    monkeypatch.setattr(
        publication,
        "fail_structured_stage21_attempt",
        lambda *_args: events.append("cleanup") or (),
    )
    monkeypatch.setattr(
        publication,
        "clear_structured_stage21_context",
        lambda *_args: events.append("clear"),
    )
    failed = pipeline_executor._execute_structured_stage21_private(
        object(),
        object(),
    )
    assert failed.status is StageStatus.FAILED
    assert failed.artifacts == ()
    assert failed.evidence_refs == ()
    assert events == [
        "transition",
        "produce",
        "immediate",
        "terminal",
        "cleanup",
    ]

    events.clear()
    monkeypatch.setattr(
        publication,
        "validate_structured_stage21_terminal_postcondition",
        lambda *_args, **_kwargs: events.append("terminal"),
    )
    done = pipeline_executor._execute_structured_stage21_private(
        object(),
        object(),
    )
    assert done.status is StageStatus.DONE
    assert done.artifacts == ("archive.md", "bundle_index.json")
    assert events == [
        "transition",
        "produce",
        "immediate",
        "terminal",
        "clear",
    ]


def test_structured_private_api_has_no_llm_prompt_config_or_hook_inputs() -> None:
    assert tuple(
        inspect.signature(
            pipeline_executor._execute_structured_stage21_private
        ).parameters
    ) == ("release_lock", "pre_admission_context")
    assert tuple(
        inspect.signature(publication.produce_structured_stage21).parameters
    ) == ("lease", "context")
    assert not {
        "result",
        "provisional",
        "artifacts",
        "evidence_refs",
    }.intersection(publication.Stage21AttemptContext.__slots__)


def test_generic_stage21_implementation_remains_schema_v1() -> None:
    from researchclaw.pipeline.stage_impls import _review_publish

    source = inspect.getsource(_review_publish._build_stage21_bundle_index)
    assert '"schema_version": 1' in source
    assert "_utcnow_iso()" in source
    assert (
        pipeline_executor._STAGE_EXECUTORS[Stage.KNOWLEDGE_ARCHIVE]
        is _review_publish._execute_knowledge_archive
    )


def test_generic_stage21_ignores_stale_structured_v2_formals_and_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.stage20_input_bundle import Stage20InputBundle
    from researchclaw.pipeline.stage21_input_bundle import Stage21InputBundle
    from researchclaw.pipeline.stage_impls import _review_publish
    from tests.test_stage21_canonical_archive import (
        _bound,
        _config,
        _inputs,
    )

    run_dir = tmp_path / "generic-run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    stale_inputs = _authority_inputs()
    stale_archive = authority.render_archive(stale_inputs)
    stale_index = authority.build_bundle_index_v2(
        stale_inputs, archive_bytes=stale_archive
    )
    (stage_dir / "archive.md").write_bytes(stale_archive)
    (stage_dir / "bundle_index.json").write_bytes(stale_index)
    (stage_dir / "archive.md.tmp").write_bytes(stale_archive)
    (stage_dir / "bundle_index.json.tmp").write_bytes(stale_index)

    evidence, base_stage20, _ = _inputs()
    config = _config(graceful=True)
    stage19 = SimpleNamespace(
        artifacts=(_bound("stage-17/paper_draft.md", "draft"),)
    )
    stage20 = Stage20InputBundle(
        stage19_inputs=stage19,
        revised_paper=base_stage20.revised_paper,
        publication_binding=base_stage20.publication_binding,
        publication_mode=base_stage20.publication_mode,
    )
    stage21 = Stage21InputBundle(
        stage20_inputs=stage20,
        quality_report=_bound("stage-20/quality_report.json", "quality"),
        fabrication_flags=_bound(
            "stage-20/fabrication_flags.json", "flags"
        ),
        quality_gate_manifest=_bound(
            "stage-20/quality_gate_manifest.json", "manifest"
        ),
        quality_gate_outcome="passed",
    )
    monkeypatch.setattr(
        _review_publish,
        "load_canonical_experiment_evidence",
        lambda _run: evidence,
    )
    monkeypatch.setattr(
        _review_publish,
        "parse_config_snapshot_text",
        lambda *_args, **_kwargs: config,
    )
    monkeypatch.setattr(
        _review_publish, "semantic_config_sha256", lambda _config: "same"
    )
    monkeypatch.setattr(
        _review_publish,
        "_load_bound_stage19_inputs",
        lambda *_args: stage19,
    )
    monkeypatch.setattr(
        _review_publish,
        "_snapshot_claim_scope",
        lambda _evidence: "pipeline_validation",
    )
    monkeypatch.setattr(
        _review_publish,
        "load_stage20_input_bundle",
        lambda *_args, **_kwargs: stage20,
    )
    monkeypatch.setattr(
        _review_publish,
        "load_stage21_input_bundle",
        lambda *_args, **_kwargs: stage21,
    )
    monkeypatch.setattr(
        _review_publish,
        "_verify_stage21_sources",
        lambda *_args, **_kwargs: None,
    )

    result = _review_publish._execute_knowledge_archive(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=None,
        prompts=None,
    )
    assert result.status is StageStatus.DONE
    generic_index = json.loads(
        (stage_dir / "bundle_index.json").read_bytes()
    )
    assert generic_index["schema_version"] == 1
    assert (
        stage_dir / "archive.md"
    ).read_text(encoding="utf-8").startswith("# Knowledge Archive\n")
    assert not (stage_dir / "archive.md.tmp").exists()
    assert not (stage_dir / "bundle_index.json.tmp").exists()
