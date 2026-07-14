"""Controller-owned Stage 12 single-invocation lease and journal."""

from __future__ import annotations

import json
import fcntl
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)


@dataclass(frozen=True)
class InvocationLease:
    """Opaque controller-issued lease required by the C1 sandbox wrapper."""

    ordinal: int
    invocation_token: str
    generation_binding_sha256: str


class CanonicalExecutionController:
    """Own generation rollover, one-acquire enforcement, and journal writes."""

    def __init__(self, stage_dir: Path, lock_descriptor: int | None = None) -> None:
        self.stage_dir = stage_dir
        self.journal_path = stage_dir / "execution_invocation_journal.jsonl"
        self._lease: InvocationLease | None = None
        self._invocation_consumed = False
        self._sandbox_root: Path | None = None
        self._lock_descriptor = lock_descriptor

    @classmethod
    def prepare_generation(cls, run_dir: Path, stage_dir: Path) -> CanonicalExecutionController:
        """Invalidate downstream authority and atomically roll an old generation."""
        require_canonical_evidence_capabilities("CanonicalExecutionController.acquire")
        if stage_dir != run_dir / "stage-12":
            raise RuntimeError("canonical_stage12_directory_mismatch")
        lock_path = run_dir / ".canonical_experiment_evidence.lock"
        if lock_path.is_symlink():
            raise RuntimeError("canonical_evidence_lock_unsafe")
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            os.close(lock_descriptor)
            raise RuntimeError("canonical_evidence_lock_unsafe")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_descriptor)
            raise RuntimeError("canonical_evidence_generation_locked") from exc
        controller = cls(stage_dir, lock_descriptor)
        try:
            if stage_dir.is_symlink():
                raise RuntimeError("canonical_stage12_directory_unsafe")
            invalidation_unsafe = False
            for path in (
                run_dir / "canonical_experiment_evidence.json",
                stage_dir / "experiment_result_set.json",
                run_dir / "experiment_summary_best.json",
                run_dir / "analysis_best.md",
                run_dir / "stage-13/refinement_result_set.json",
            ):
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    if not path.is_file():
                        invalidation_unsafe = True
                    else:
                        path.unlink()
            if invalidation_unsafe:
                raise RuntimeError("canonical_evidence_invalidation_unsafe")
            if stage_dir.exists() and any(stage_dir.iterdir()):
                versions = []
                for path in run_dir.iterdir():
                    match = re.fullmatch(r"stage-12_v([1-9]\d*)", path.name)
                    if match:
                        if path.is_symlink() or not path.is_dir():
                            raise RuntimeError("canonical_stage12_history_unsafe")
                        versions.append(int(match.group(1)))
                archive = run_dir / f"stage-12_v{max(versions, default=0) + 1}"
                os.replace(stage_dir, archive)
            stage_dir.mkdir(parents=True, exist_ok=True)
            return controller
        except Exception:
            controller.close()
            raise

    def close(self) -> None:
        if self._lock_descriptor is None:
            return
        fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        os.close(self._lock_descriptor)
        self._lock_descriptor = None

    def acquire(
        self,
        *,
        generation_binding_sha256: str,
        experiment_contract_sha256: str,
        sealed_candidate_manifest_sha256: str,
        config_semantic_sha256: str,
    ) -> InvocationLease:
        require_canonical_evidence_capabilities("CanonicalExecutionController.acquire")
        if self._lease is not None or self.journal_path.exists():
            raise RuntimeError("canonical_stage12_invocation_already_acquired")
        lease = InvocationLease(
            ordinal=1,
            invocation_token=secrets.token_hex(32),
            generation_binding_sha256=generation_binding_sha256,
        )
        started = {
            "schema_version": 1,
            "event": "started",
            "ordinal": 1,
            "invocation_token": lease.invocation_token,
            "generation_binding_sha256": generation_binding_sha256,
            "experiment_contract_sha256": experiment_contract_sha256,
            "sealed_candidate_manifest_sha256": sealed_candidate_manifest_sha256,
            "config_semantic_sha256": config_semantic_sha256,
        }
        self._write_started(started)
        self._lease = lease
        return lease

    def prepare_invocation_workspace(self, lease: InvocationLease) -> Path:
        """Create the empty controller-owned sandbox root for this lease."""
        self._require_active_lease(lease)
        if self._invocation_consumed or self._sandbox_root is not None:
            raise RuntimeError("canonical_stage12_invocation_workspace_already_prepared")
        diagnostics = self.stage_dir / "diagnostics"
        invocation_root = diagnostics / "invocation-1"
        sandbox_root = invocation_root / "sandbox"
        if (
            diagnostics.is_symlink()
            or invocation_root.is_symlink()
            or sandbox_root.is_symlink()
        ):
            raise RuntimeError("canonical_stage12_invocation_workspace_unsafe")
        sandbox_root.mkdir(parents=True, exist_ok=False)
        self._sandbox_root = sandbox_root
        return sandbox_root

    def run_project(
        self,
        lease: InvocationLease,
        sandbox: Any,
        project_dir: Path,
        *,
        timeout_sec: int,
    ) -> Any:
        """Invoke the mode-specific sandbox exactly once under the active lease."""
        self._require_active_lease(lease)
        if self._invocation_consumed:
            raise RuntimeError("canonical_stage12_invocation_already_consumed")
        if (
            self._sandbox_root is None
            or not self._workspace_paths_safe()
            or any(self._sandbox_root.iterdir())
        ):
            raise RuntimeError("canonical_stage12_invocation_workspace_invalid")
        self._invocation_consumed = True
        result = sandbox.run_project(project_dir, timeout_sec=timeout_sec)
        output_dir = getattr(result, "output_dir", None)
        if (
            not self._workspace_paths_safe()
            or not isinstance(output_dir, Path)
            or output_dir.is_symlink()
            or not output_dir.is_dir()
            or output_dir.parent != self._sandbox_root
            or output_dir.resolve().parent != self._sandbox_root.resolve()
        ):
            raise RuntimeError("canonical_stage12_output_directory_unbound")
        if set(self._sandbox_root.iterdir()) != {output_dir}:
            raise RuntimeError("canonical_stage12_output_namespace_invalid")
        return result

    def _workspace_paths_safe(self) -> bool:
        if self._sandbox_root is None:
            return False
        return all(
            path.is_dir() and not path.is_symlink()
            for path in (
                self.stage_dir / "diagnostics",
                self.stage_dir / "diagnostics/invocation-1",
                self._sandbox_root,
            )
        )

    def complete(self, lease: InvocationLease, *, result_sha256: str) -> None:
        self._require_active_lease(lease)
        if not self._invocation_consumed:
            raise RuntimeError("canonical_stage12_invocation_not_consumed")
        self._append_terminal(
            {
                "schema_version": 1,
                "event": "terminal",
                "ordinal": 1,
                "invocation_token": lease.invocation_token,
                "status": "completed",
                "result_path": "stage-12/evidence-v1/run-1.json",
                "result_sha256": result_sha256,
                "failure_code": None,
            }
        )

    def fail(self, lease: InvocationLease, *, failure_code: str) -> None:
        self._require_active_lease(lease)
        if not failure_code:
            raise ValueError("canonical failure code must be nonempty")
        self._append_terminal(
            {
                "schema_version": 1,
                "event": "terminal",
                "ordinal": 1,
                "invocation_token": lease.invocation_token,
                "status": "failed",
                "result_path": None,
                "result_sha256": None,
                "failure_code": failure_code,
            }
        )

    def _require_active_lease(self, lease: InvocationLease) -> None:
        require_controller_lease(lease)
        if lease is not self._lease:
            raise PermissionError("canonical_stage12_invocation_lease_mismatch")
        from researchclaw.pipeline.canonical_experiment_evidence import (
            parse_execution_invocation_journal,
        )

        records = parse_execution_invocation_journal(
            self.journal_path.read_text(encoding="utf-8")
        )
        if len(records) != 1 or records[0]["invocation_token"] != lease.invocation_token:
            raise PermissionError("canonical_stage12_invocation_lease_not_active")

    def _write_started(self, record: dict[str, object]) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(self.journal_path, flags, 0o600)
        try:
            self._write_all(descriptor, text.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_terminal(self, record: dict[str, object]) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(self.journal_path, os.O_WRONLY | os.O_APPEND)
        try:
            self._write_all(descriptor, text.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._lease = None

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("canonical invocation journal write made no progress")
            offset += written


def require_controller_lease(lease: object) -> InvocationLease:
    """Reject direct canonical Stage 12 sandbox calls without a real lease."""
    if not isinstance(lease, InvocationLease):
        raise PermissionError("canonical_stage12_invocation_lease_required")
    if lease.ordinal != 1:
        raise PermissionError("canonical_stage12_invocation_ordinal_invalid")
    return lease


class CanonicalRefinementController:
    """Own the Stage 13 generation lock, rollover, and authority invalidation."""

    def __init__(self, stage_dir: Path, lock_descriptor: int) -> None:
        self.stage_dir = stage_dir
        self._lock_descriptor: int | None = lock_descriptor

    @classmethod
    def prepare_generation(
        cls,
        run_dir: Path,
        stage_dir: Path,
    ) -> CanonicalRefinementController:
        require_canonical_evidence_capabilities("CanonicalRefinementController.prepare")
        if stage_dir != run_dir / "stage-13":
            raise RuntimeError("canonical_stage13_directory_mismatch")
        lock_path = run_dir / ".canonical_experiment_evidence.lock"
        if lock_path.is_symlink():
            raise RuntimeError("canonical_evidence_lock_unsafe")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError("canonical_evidence_lock_unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("canonical_evidence_generation_locked") from exc

        controller = cls(stage_dir, descriptor)
        try:
            if stage_dir.is_symlink():
                raise RuntimeError("canonical_stage13_directory_unsafe")
            if stage_dir.exists() and not stage_dir.is_dir():
                raise RuntimeError("canonical_stage13_directory_unsafe")
            invalidation_unsafe = False
            for path in (
                run_dir / "canonical_experiment_evidence.json",
                run_dir / "experiment_summary_best.json",
                run_dir / "analysis_best.md",
                run_dir / "stage-13/refinement_result_set.json",
            ):
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    if not path.is_file():
                        invalidation_unsafe = True
                    else:
                        path.unlink()
            if invalidation_unsafe:
                raise RuntimeError("canonical_evidence_invalidation_unsafe")
            if stage_dir.exists() and any(stage_dir.iterdir()):
                versions: list[int] = []
                for path in run_dir.iterdir():
                    match = re.fullmatch(r"stage-13_v([1-9]\d*)", path.name)
                    if match:
                        if path.is_symlink() or not path.is_dir():
                            raise RuntimeError("canonical_stage13_history_unsafe")
                        versions.append(int(match.group(1)))
                archive = run_dir / f"stage-13_v{max(versions, default=0) + 1}"
                os.replace(stage_dir, archive)
            stage_dir.mkdir(parents=True, exist_ok=True)
            return controller
        except Exception:
            controller.close()
            raise

    def close(self) -> None:
        if self._lock_descriptor is None:
            return
        fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        os.close(self._lock_descriptor)
        self._lock_descriptor = None
