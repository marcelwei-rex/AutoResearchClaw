"""Generate human-readable run reports from pipeline artifacts."""

# pyright: basic
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path
from typing import Any


def generate_report(run_dir: Path) -> str:
    """Generate a Markdown report from a pipeline run directory.

    Args:
        run_dir: Path to the run artifacts directory (e.g., artifacts/rc-xxx/)

    Returns:
        Markdown string with the report content.

    Raises:
        ValueError: If the canonical release graph cannot be reconstructed.
    """
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities("report.generate_report")
    from researchclaw.pipeline.external_release_projection import (
        load_external_release_projection,
    )

    projection = load_external_release_projection(run_dir)

    return render_report(projection, run_dir)


def render_report(projection: Any, run_dir: Path) -> str:
    """Render one already reconstructed immutable projection."""

    sections = []
    sections.append(_header(projection, run_dir))
    sections.append(_paper_section(projection))
    sections.append(_experiment_section(projection))
    sections.append(_citation_section(projection))

    return "\n\n".join(section for section in sections if section)


def _header(projection: Any, run_dir: Path) -> str:
    lines = [
        "# ResearchClaw Run Report",
        "",
        f"**Run ID**: {run_dir.name}",
        "**Status**: canonical release reconstructed",
        f"**Artifacts**: `{run_dir}`",
        f"**Canonical manifest**: `{projection.canonical_manifest_path}`",
        f"**Canonical SHA-256**: `{projection.canonical_manifest_sha256}`",
    ]
    return "\n".join(lines)


def _paper_section(projection: Any) -> str:
    word_count = len(projection.paper_text.split())
    return "\n".join(
        [
            "## Paper",
            f"- Final: canonical verified paper (~{word_count} words)",
            f"- Candidate: `{projection.candidate_id}`",
        ]
    )


def _experiment_section(projection: Any) -> str:
    lines = ["## Experiments"]
    lines.append(
        f"- Selected result: `{projection.selected_result_manifest_path}`"
    )
    lines.append(f"- Selected execution: `{projection.selected_execution_path}`")
    lines.append(f"- Metric observations: {len(projection.metric_observations)}")
    lines.append("- Analysis: canonical selected candidate")

    return "\n".join(lines)


def _citation_section(projection: Any) -> str:
    lines = ["## Citations"]
    summary = projection.verification_report["summary"]
    total = summary["total"]
    verified = summary["verified"]
    pct = f"{verified / total * 100:.1f}%" if total else "N/A"
    lines.append(f"- Verified: {verified}/{total} ({pct})")
    lines.append(f"- Suspicious: {summary['suspicious']}")
    lines.append(f"- Hallucinated: {summary['hallucinated']}")

    return "\n".join(lines)


def print_report(run_dir: Path) -> None:
    print(generate_report(run_dir))


def write_report(run_dir: Path, output_path: Path) -> str:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities("report.write_report")
    from researchclaw.pipeline.external_release_projection import (
        load_external_release_projection,
    )

    projection = load_external_release_projection(run_dir)
    report = render_report(projection, run_dir)
    _write_external_report(run_dir, output_path, report.encode("utf-8"))
    return report


def _write_external_report(run_dir: Path, output_path: Path, content: bytes) -> None:
    if output_path.name in {"", ".", ".."}:
        raise ValueError("report output path is invalid")
    run_root = run_dir.resolve(strict=True)
    parent = output_path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("report output parent must be a safe existing directory")
    parent_root = parent.resolve(strict=True)
    if parent_root == run_root or parent_root.is_relative_to(run_root):
        raise ValueError("report output must be outside the run directory")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(parent, directory_flags)
    temporary = f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    backup = f".{output_path.name}.{uuid.uuid4().hex}.bak"
    temporary_active = False
    backup_active = False
    published = False
    try:
        opened_parent = os.fstat(parent_fd)
        live_parent = os.stat(parent, follow_symlinks=False)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            live_parent.st_dev,
            live_parent.st_ino,
        ):
            raise ValueError("report output parent changed before write")
        try:
            existing = os.stat(
                output_path.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise ValueError("report output must be a singly linked regular file")

        temporary_active = True
        _write_report_temp(parent_fd, temporary, content)
        if _read_report_file(parent_fd, temporary) != content:
            raise OSError("report temporary output replay mismatch")
        if existing is not None:
            os.rename(
                output_path.name,
                backup,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            backup_active = True
        os.rename(
            temporary,
            output_path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_active = False
        published = True
        os.fsync(parent_fd)
        if _read_report_file(parent_fd, output_path.name) != content:
            raise OSError("report output replay mismatch")
        final_parent = os.stat(parent, follow_symlinks=False)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            final_parent.st_dev,
            final_parent.st_ino,
        ):
            raise ValueError("report output parent changed during write")
        if backup_active:
            _unlink_report_entry(parent_fd, backup)
            backup_active = False
    except Exception as exc:
        cleanup_errors: list[BaseException] = []
        if backup_active:
            try:
                os.rename(
                    backup,
                    output_path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                backup_active = False
                published = False
            except BaseException as cleanup_exc:  # pragma: no cover - OS failure
                cleanup_errors.append(cleanup_exc)
        elif published:
            try:
                _unlink_report_entry(parent_fd, output_path.name)
                published = False
            except BaseException as cleanup_exc:  # pragma: no cover - OS failure
                cleanup_errors.append(cleanup_exc)
        if temporary_active:
            try:
                _unlink_report_entry(parent_fd, temporary)
                temporary_active = False
            except BaseException as cleanup_exc:  # pragma: no cover - OS failure
                cleanup_errors.append(cleanup_exc)
        for cleanup_exc in cleanup_errors:
            exc.add_note(f"report cleanup failed: {cleanup_exc}")
        raise
    finally:
        if temporary_active:
            try:
                _unlink_report_entry(parent_fd, temporary)
            except OSError:
                pass
        os.close(parent_fd)


def _write_report_temp(directory_fd: int, name: str, content: bytes) -> None:
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    output_fd = os.open(name, file_flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(output_fd, view)
            if written < 1:
                raise OSError("report output write made no progress")
            view = view[written:]
        os.fsync(output_fd)
        output_stat = os.fstat(output_fd)
        if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_nlink != 1:
            raise ValueError("report output must be a singly linked regular file")
    finally:
        os.close(output_fd)


def _read_report_file(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("report output must be a singly linked regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("report output changed during replay")
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _unlink_report_entry(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
