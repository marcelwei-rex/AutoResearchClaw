from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")

from researchclaw.pipeline.external_release_projection import (
    ExternalReleaseProjection,
)
from researchclaw.report import generate_report, write_report
from researchclaw import cli as rc_cli


def _projection() -> ExternalReleaseProjection:
    return ExternalReleaseProjection(
        canonical_manifest_path="canonical_experiment_evidence.json",
        canonical_manifest_sha256="a" * 64,
        candidate_id="cand-" + "b" * 64,
        selected_result_manifest_path="stage-13/refinement_result_set.json",
        selected_result_manifest_sha256="c" * 64,
        selected_execution_path="stage-13/evidence-v1/iter-1/run-1.json",
        selected_execution_sha256="d" * 64,
        metric_observations=MappingProxyType({"accuracy": ("0.9",)}),
        structured_results=MappingProxyType({"conditions": ()}),
        summary=MappingProxyType({"primary_metric": "accuracy"}),
        analysis_text="Canonical analysis.",
        paper_text="Canonical final paper with five bounded words.",
        latex_text="\\documentclass{article}\n",
        verification_report=MappingProxyType(
            {
                "summary": MappingProxyType(
                    {
                        "total": 10,
                        "verified": 8,
                        "suspicious": 1,
                        "hallucinated": 1,
                    }
                ),
                "results": (),
            }
        ),
        literature_text="Canonical literature synthesis.",
        review_text="Canonical peer review.",
        project_files=(("main.py", "print('canonical')\n"),),
        authority_artifacts=(),
    )


def test_report_renders_only_external_release_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run-that-need-not-exist"
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda captured: _projection() if captured == run_dir else None,
    )

    report = generate_report(run_dir)

    assert "canonical release reconstructed" in report
    assert "canonical_experiment_evidence.json" in report
    assert "cand-" + "b" * 64 in report
    assert "stage-13/refinement_result_set.json" in report
    assert "Metric observations: 1" in report
    assert "Verified: 8/10 (80.0%)" in report
    assert "Suspicious: 1" in report
    assert "Hallucinated: 1" in report


def test_write_report_reconstructs_before_persistent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: calls.append("reconstruct") or _projection(),
    )
    (tmp_path / "run").mkdir()
    output = tmp_path / "report.md"

    write_report(tmp_path / "run", output)

    assert calls == ["reconstruct"]
    assert output.read_text(encoding="utf-8").startswith("# ResearchClaw Run Report")


def test_report_rejects_run_tree_symlink_and_hardlink_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    authority = run_dir / "canonical_experiment_evidence.json"
    authority.write_text("AUTHORITY", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: _projection(),
    )
    with pytest.raises(ValueError, match="outside the run"):
        write_report(run_dir, run_dir / "report.md")

    symlink = tmp_path / "report-symlink.md"
    symlink.symlink_to(authority)
    with pytest.raises(ValueError, match="singly linked regular file"):
        write_report(run_dir, symlink)
    assert authority.read_text(encoding="utf-8") == "AUTHORITY"

    hardlink = tmp_path / "report-hardlink.md"
    hardlink.hardlink_to(authority)
    with pytest.raises(ValueError, match="regular file"):
        write_report(run_dir, hardlink)
    assert authority.read_text(encoding="utf-8") == "AUTHORITY"


def test_report_cli_uses_one_projection_for_stdout_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output = tmp_path / "report.md"
    calls: list[Path] = []
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda path: calls.append(path) or _projection(),
    )
    result = rc_cli.cmd_report(
        SimpleNamespace(run_dir=str(run_dir), output=str(output))
    )
    assert result == 0
    assert calls == [run_dir]
    assert output.read_text(encoding="utf-8") in capsys.readouterr().out


def test_report_parent_replacement_fails_and_cleans_detached_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    parent = tmp_path / "output"
    parent.mkdir()
    (parent / "report.md").write_text("OLD", encoding="utf-8")
    detached = tmp_path / "output-detached"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_text("KEEP", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: _projection(),
    )
    from researchclaw import report as report_module

    original = report_module._write_report_temp

    def replace_parent(directory_fd: int, name: str, content: bytes) -> None:
        original(directory_fd, name, content)
        parent.rename(detached)
        parent.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(report_module, "_write_report_temp", replace_parent)
    with pytest.raises(ValueError, match="parent changed during write"):
        write_report(run_dir, parent / "report.md")

    assert [path.name for path in external.iterdir()] == ["sentinel"]
    assert (detached / "report.md").read_text(encoding="utf-8") == "OLD"
    assert [path.name for path in detached.iterdir()] == ["report.md"]


def test_report_post_rename_replay_failure_restores_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output = tmp_path / "report.md"
    output.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: _projection(),
    )
    from researchclaw import report as report_module

    original = report_module._read_report_file

    def fail_published_replay(directory_fd: int, name: str) -> bytes:
        content = original(directory_fd, name)
        if name == "report.md":
            raise OSError("injected late replay failure")
        return content

    monkeypatch.setattr(
        report_module, "_read_report_file", fail_published_replay
    )
    with pytest.raises(OSError, match="injected late replay"):
        write_report(run_dir, output)

    assert output.read_text(encoding="utf-8") == "OLD"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.md", "run"]


def test_report_directory_fsync_failure_restores_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output = tmp_path / "report.md"
    output.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: _projection(),
    )
    from researchclaw import report as report_module

    original = report_module.os.fsync
    calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original(fd)

    monkeypatch.setattr(report_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="injected directory fsync"):
        write_report(run_dir, output)

    assert calls == 2
    assert output.read_text(encoding="utf-8") == "OLD"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.md", "run"]


def test_report_partial_write_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output = tmp_path / "report.md"
    output.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: _projection(),
    )
    from researchclaw import report as report_module

    def fail_partial(directory_fd: int, name: str, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            os.write(fd, content[:10])
        finally:
            os.close(fd)
        raise OSError("injected partial report write")

    monkeypatch.setattr(report_module, "_write_report_temp", fail_partial)
    with pytest.raises(OSError, match="injected partial"):
        write_report(run_dir, output)

    assert output.read_text(encoding="utf-8") == "OLD"
    assert not any(path.name.endswith(".tmp") for path in tmp_path.iterdir())


def test_report_propagates_reconstruction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_run_dir: Path) -> ExternalReleaseProjection:
        raise ValueError("canonical graph invalid")

    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        fail,
    )
    with pytest.raises(ValueError, match="canonical graph invalid"):
        generate_report(tmp_path / "legacy-only-run")


def test_report_has_no_legacy_authority_path_reads() -> None:
    source = inspect.getsource(__import__("researchclaw.report", fromlist=["*"]))
    for forbidden in (
        "pipeline_summary.json",
        "experiment_results.json",
        "stage-14*",
        ".glob(",
        ".read_text(",
    ):
        assert forbidden not in source
