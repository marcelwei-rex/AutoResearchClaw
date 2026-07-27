"""B4-D3 portable Stage 20 publication and cleanup threat boundary."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline import stage20_structured_publication as publication
from researchclaw.pipeline.stages import StageStatus
from tests.test_stage17_structured_producer_integration import active_capture
from tests.test_stage20_structured_replay import (
    _QualityClient,
    _prepare_stage20_source,
    _quality_response,
    _run_private,
)


def test_stage20_uses_only_four_fixed_temporary_files() -> None:
    assert publication.STRUCTURED_STAGE20_STAGE_TEMPS == (
        "quality_report.json.tmp",
        "fabrication_flags.json.tmp",
        "quality_gate_manifest.json.tmp",
    )
    assert (
        publication.STRUCTURED_STAGE20_ROOT_TEMP
        == "degradation_signal.json.tmp"
    )
    assert not hasattr(publication, "STRUCTURED_STAGE20_STAGING")


@pytest.mark.parametrize(
    ("cleanup_name", "expected"),
    (
        (
            "_cleanup_attempt_outputs",
            (
                "quality_gate_manifest.json",
                "quality_report.json",
                "fabrication_flags.json",
                "quality_report.json.tmp",
                "fabrication_flags.json.tmp",
                "quality_gate_manifest.json.tmp",
                "degradation_signal.json",
                "degradation_signal.json.tmp",
                "quality_gate_llm_diagnostics.json",
                "quality_gate_llm_diagnostics.json.tmp",
            ),
        ),
        (
            "_cleanup_owned_namespace",
            (
                "quality_gate_manifest.json",
                "quality_report.json",
                "fabrication_flags.json",
                "quality_report.json.tmp",
                "fabrication_flags.json.tmp",
                "quality_gate_manifest.json.tmp",
                "degradation_signal.json",
                "degradation_signal.json.tmp",
                "quality_gate_llm_diagnostics.json",
                "quality_gate_llm_diagnostics.json.tmp",
            ),
        ),
    ),
)
def test_cleanup_uses_frozen_manifest_formal_temp_signal_diagnostic_order(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_name: str,
    expected: tuple[str, ...],
) -> None:
    observed: list[str] = []

    def record(_fd, name, _errors, **_kwargs):
        observed.append(name)

    monkeypatch.setattr(publication, "_unlink_reserved", record)
    cleanup = getattr(publication, cleanup_name)
    assert cleanup(
        SimpleNamespace(_run_fd=101), SimpleNamespace(_stage_fd=202)
    ) == ()
    assert tuple(observed) == expected


def test_success_has_no_staging_directory_or_temporary_file(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)

    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.DONE
    stage20 = active_capture.run_dir / "stage-20"
    assert not (stage20 / ".stage20-structured-publication.staging").exists()
    for name in publication.STRUCTURED_STAGE20_STAGE_TEMPS:
        assert not (stage20 / name).exists()
    assert not (
        active_capture.run_dir / publication.STRUCTURED_STAGE20_ROOT_TEMP
    ).exists()


def _install_collision(path: Path, kind: str, target: Path) -> socket.socket | None:
    if kind == "regular":
        path.write_bytes(b"preexisting-reserved-entry")
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
        (path / "sentinel").write_bytes(b"keep-directory-content")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        descriptor, alias_name = tempfile.mkstemp(
            prefix="b4b-s20-", dir="/tmp"
        )
        os.close(descriptor)
        os.unlink(alias_name)
        alias = Path(alias_name)
        try:
            alias.symlink_to(path.parent, target_is_directory=True)
            try:
                handle.bind(str(alias / path.name))
            except PermissionError:
                handle.close()
                pytest.skip("sandbox forbids AF_UNIX socket creation")
        finally:
            alias.unlink(missing_ok=True)
        return handle
    else:  # pragma: no cover - test construction guard
        raise AssertionError(kind)
    return None


@pytest.mark.parametrize(
    "kind", ("regular", "symlink", "directory", "fifo", "socket")
)
def test_preexisting_temp_collision_fails_before_model_or_content_write(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    target = tmp_path / "external-target"
    target.write_bytes(b"external-target-bytes")
    temp = active_capture.run_dir / "stage-20/quality_report.json.tmp"
    handle = _install_collision(temp, kind, target)
    client = _QualityClient([_quality_response()])
    try:
        result = _run_private(active_capture, llm=client)
    finally:
        if handle is not None:
            handle.close()

    assert result.status is StageStatus.FAILED
    assert client.requests == []
    assert target.read_bytes() == b"external-target-bytes"
    if kind == "directory":
        assert (temp / "sentinel").read_bytes() == b"keep-directory-content"
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_temp_replacement_blocks_publication_without_writing_replacement(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    external = tmp_path / "replacement-target"
    external.write_bytes(b"external-bytes")
    detached = tmp_path / "detached-created-temp"
    original_verify = publication._verify_held_temp
    replaced = False

    def replace_before_verify(record):
        nonlocal replaced
        if not replaced and record.name == "quality_report.json.tmp":
            replaced = True
            temp = active_capture.run_dir / "stage-20" / record.name
            temp.rename(detached)
            temp.symlink_to(external)
        return original_verify(record)

    monkeypatch.setattr(
        publication, "_verify_held_temp", replace_before_verify
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.FAILED
    assert external.read_bytes() == b"external-bytes"
    assert detached.read_bytes() != b"external-bytes"
    assert not (
        active_capture.run_dir / "stage-20/quality_report.json"
    ).exists()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_cleanup_window_replacement_unlinks_reserved_entry_not_target(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    stage20 = active_capture.run_dir / "stage-20"
    for name in publication.STRUCTURED_STAGE20_ARTIFACTS:
        (stage20 / name).write_bytes(b"stale")
    collision_dir = stage20 / "fabrication_flags.json.tmp"
    collision_dir.mkdir()
    (collision_dir / "sentinel").write_bytes(b"keep")
    external = tmp_path / "symlink-target"
    external.write_bytes(b"external")
    original_unlink = publication.os.unlink
    replaced = False

    def replace_after_check(name, *, dir_fd=None):
        nonlocal replaced
        if name == "quality_report.json" and not replaced:
            replaced = True
            original_unlink(name, dir_fd=dir_fd)
            os.symlink(external, name, dir_fd=dir_fd)
        return original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(publication.os, "unlink", replace_after_check)
    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        errors = publication._cleanup_owned_namespace(
            active_capture.lease, namespace
        )

    assert external.read_bytes() == b"external"
    assert (collision_dir / "sentinel").read_bytes() == b"keep"
    assert not (stage20 / "quality_gate_manifest.json").exists()
    assert not (stage20 / "quality_report.json").exists()
    assert any("fabrication_flags.json.tmp" in item for item in errors)


def test_late_formal_symlink_collision_receives_zero_content_write(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    external = tmp_path / "late-formal-target"
    external.write_bytes(b"external-formal-bytes")
    original_create = publication._create_formal_from_verified_temp
    injected = False

    def inject_collision(*args, **kwargs):
        nonlocal injected
        name = kwargs["name"]
        if name == "quality_report.json" and not injected:
            injected = True
            (
                active_capture.run_dir / "stage-20" / name
            ).symlink_to(external)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(
        publication, "_create_formal_from_verified_temp", inject_collision
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.FAILED
    assert external.read_bytes() == b"external-formal-bytes"
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_exact_bytes_regular_formal_replacement_is_not_adopted(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    original_create = publication._create_formal_from_verified_temp
    identities: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def replace_report_after_create(*args, **kwargs):
        created_identity = original_create(*args, **kwargs)
        if kwargs["name"] == "quality_report.json":
            report = active_capture.run_dir / "stage-20/quality_report.json"
            exact_bytes = report.read_bytes()
            report.unlink()
            report.write_bytes(exact_bytes)
            replacement = report.stat()
            identities.append(
                (
                    created_identity,
                    (replacement.st_dev, replacement.st_ino),
                )
            )
        return created_identity

    monkeypatch.setattr(
        publication,
        "_create_formal_from_verified_temp",
        replace_report_after_create,
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert identities and identities[0][0] != identities[0][1]
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    assert not (
        active_capture.run_dir / "stage-20/quality_gate_manifest.json"
    ).exists()


def test_manifest_formal_create_is_strictly_last(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    created: list[tuple[str, str]] = []
    original_temp = publication._create_held_temp
    original_formal = publication._create_formal_from_verified_temp

    def record_temp(*args, **kwargs):
        created.append(("temp", kwargs["name"]))
        return original_temp(*args, **kwargs)

    def record_formal(*args, **kwargs):
        created.append(("formal", kwargs["name"]))
        return original_formal(*args, **kwargs)

    monkeypatch.setattr(publication, "_create_held_temp", record_temp)
    monkeypatch.setattr(
        publication, "_create_formal_from_verified_temp", record_formal
    )
    result = _run_private(
        active_capture, llm=_QualityClient([_quality_response()])
    )

    assert result.status is StageStatus.DONE
    assert created == [
        ("temp", "quality_report.json.tmp"),
        ("temp", "fabrication_flags.json.tmp"),
        ("temp", "quality_gate_manifest.json.tmp"),
        ("formal", "quality_report.json"),
        ("formal", "fabrication_flags.json"),
        ("formal", "quality_gate_manifest.json"),
    ]


def test_unremovable_directory_collision_does_not_hide_other_invalidation(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    stage20 = active_capture.run_dir / "stage-20"
    (stage20 / "quality_gate_manifest.json").write_bytes(b"stale-manifest")
    (stage20 / "quality_report.json").write_bytes(b"stale-report")
    directory = stage20 / "fabrication_flags.json"
    directory.mkdir()
    (directory / "sentinel").write_bytes(b"keep")

    with active_capture.lease.open_stage_namespace("stage-20") as namespace:
        errors = publication._cleanup_owned_namespace(
            active_capture.lease, namespace
        )

    assert not (stage20 / "quality_gate_manifest.json").exists()
    assert not (stage20 / "quality_report.json").exists()
    assert (directory / "sentinel").read_bytes() == b"keep"
    assert any("fabrication_flags.json" in item for item in errors)


def test_diagnostic_directory_collision_blocks_success_before_model(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    diagnostic = (
        active_capture.run_dir
        / "stage-20/quality_gate_llm_diagnostics.json"
    )
    diagnostic.mkdir()
    (diagnostic / "sentinel").write_bytes(b"keep")
    client = _QualityClient([_quality_response()])

    result = _run_private(active_capture, llm=client)

    assert result.status is StageStatus.FAILED
    assert client.requests == []
    assert (diagnostic / "sentinel").read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ("regular", "symlink", "fifo"))
def test_diagnostic_safe_reserved_collision_is_unlinked_without_target_io(
    tmp_path: Path,
    kind: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    target = tmp_path / "diagnostic-target"
    target.write_bytes(b"external")
    collision = stage_dir / publication.STRUCTURED_STAGE20_DIAGNOSTIC
    handle = _install_collision(collision, kind, target)
    run_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage_fd = os.open(
        stage_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        errors = publication._cleanup_owned_namespace(
            SimpleNamespace(_run_fd=run_fd),
            SimpleNamespace(_stage_fd=stage_fd),
        )
    finally:
        os.close(stage_fd)
        os.close(run_fd)
        if handle is not None:
            handle.close()

    assert errors == ()
    assert not collision.exists()
    assert target.read_bytes() == b"external"


def test_failure_diagnostic_is_non_authority_and_has_no_temp(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)
    client = _QualityClient([ValueError("non-transport quality failure")])

    result = _run_private(active_capture, llm=client)

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert result.evidence_refs == ()
    stage20 = active_capture.run_dir / "stage-20"
    diagnostic = stage20 / "quality_gate_llm_diagnostics.json"
    assert isinstance(json.loads(diagnostic.read_bytes()), list)
    assert not (stage20 / "quality_gate_llm_diagnostics.json.tmp").exists()
    assert not (stage20 / "quality_gate_manifest.json").exists()


def test_diagnostic_publication_error_preserves_original_failure(
    active_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_stage20_source(active_capture, monkeypatch)

    def fail_after_injecting_diagnostic(*_args, **_kwargs):
        diagnostic = (
            active_capture.run_dir
            / "stage-20/quality_gate_llm_diagnostics.json"
        )
        diagnostic.mkdir()
        error = publication.StructuredStage20PublicationError(
            "primary quality failure"
        )
        error.diagnostics = (
            publication.TransportDiagnostic(
                1,
                "initial",
                1,
                "0" * 64,
                "semantic_rejected",
                "malformed_response",
            ),
        )
        raise error

    monkeypatch.setattr(
        publication, "execute_bounded_quality_calls", fail_after_injecting_diagnostic
    )
    result = _run_private(active_capture, llm=_QualityClient([]))

    assert result.status is StageStatus.FAILED
    assert "primary quality failure" in (result.error or "")
    assert "diagnostic publication also failed" in (result.error or "")
    assert result.artifacts == ()
    assert result.evidence_refs == ()
