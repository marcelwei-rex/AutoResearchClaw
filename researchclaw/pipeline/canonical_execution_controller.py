"""Controller-owned Stage 12 single-invocation lease and journal."""

from __future__ import annotations

import json
import fcntl
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock


_ANALYSIS_CONTROLLER_AUTHORITY = object()


@dataclass(frozen=True)
class InvocationLease:
    """Opaque controller-issued lease required by the C1 sandbox wrapper."""

    ordinal: int
    invocation_token: str
    generation_binding_sha256: str


def _acquire_canonical_lock(release_lock: ReleaseGraphLock) -> int:
    run_fd = release_lock.duplicate_run_fd()
    try:
        try:
            descriptor = os.open(
                ".canonical_experiment_evidence.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=run_fd,
            )
        except OSError as exc:
            raise RuntimeError("canonical_evidence_lock_unsafe") from exc
    finally:
        os.close(run_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeError("canonical_evidence_lock_unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RuntimeError("canonical_evidence_generation_locked") from exc
    return descriptor


def _close_controller_locks(
    descriptor: int | None,
    release_lock: ReleaseGraphLock | None,
) -> None:
    identity_error: Exception | None = None
    if release_lock is not None:
        try:
            release_lock.assert_canonical()
        except Exception as exc:  # noqa: BLE001
            identity_error = exc
            try:
                release_lock.invalidate_experiment_commit_points()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(
                    f"detached experiment authority cleanup also failed: {cleanup_exc}"
                )
    if descriptor is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    if release_lock is not None:
        release_lock.close()
    if identity_error is not None:
        raise RuntimeError("canonical_generation_run_identity_changed") from identity_error


class CanonicalExecutionController:
    """Own generation rollover, one-acquire enforcement, and journal writes."""

    def __init__(
        self,
        stage_dir: Path,
        lock_descriptor: int | None = None,
        release_lock: ReleaseGraphLock | None = None,
    ) -> None:
        self.stage_dir = stage_dir
        self.journal_path = stage_dir / "execution_invocation_journal.jsonl"
        self._lease: InvocationLease | None = None
        self._invocation_consumed = False
        self._sandbox_root: Path | None = None
        self._workspace_parent: Path | None = None
        self._diagnostics_published = False
        self._lock_descriptor = lock_descriptor
        self._release_lock = release_lock
        self._namespace: BoundOutputNamespace | None = None

    @classmethod
    def prepare_generation(cls, run_dir: Path, stage_dir: Path) -> CanonicalExecutionController:
        """Invalidate downstream authority and atomically roll an old generation."""
        require_canonical_evidence_capabilities("CanonicalExecutionController.acquire")
        if stage_dir != run_dir / "stage-12":
            raise RuntimeError("canonical_stage12_directory_mismatch")
        release_lock = ReleaseGraphLock.acquire(
            run_dir, "CanonicalExecutionController.prepare"
        )
        try:
            lock_descriptor = _acquire_canonical_lock(release_lock)
        except Exception:
            release_lock.close()
            raise
        controller = cls(stage_dir, lock_descriptor, release_lock)
        try:
            try:
                release_lock.ensure_run_directory("stage-12")
            except OSError as exc:
                raise RuntimeError("canonical_stage12_directory_unsafe") from exc
            release_lock.invalidate_experiment_commit_points()
            release_lock.roll_stage_generation("stage-12")
            controller._namespace = release_lock.open_stage_namespace("stage-12")
            controller.assert_canonical()
            return controller
        except Exception:
            controller.close()
            raise

    def close(self) -> None:
        workspace_parent, self._workspace_parent = self._workspace_parent, None
        if workspace_parent is not None:
            shutil.rmtree(workspace_parent, ignore_errors=True)
        namespace, self._namespace = self._namespace, None
        if namespace is not None:
            namespace.close()
        descriptor, self._lock_descriptor = self._lock_descriptor, None
        release_lock, self._release_lock = self._release_lock, None
        _close_controller_locks(descriptor, release_lock)

    def assert_canonical(self) -> None:
        if self._release_lock is None or self._namespace is None:
            raise RuntimeError("canonical_stage12_controller_closed")
        self._release_lock.assert_canonical()
        self._namespace.assert_canonical()

    def read_text(self, name: str) -> str:
        self.assert_canonical()
        assert self._namespace is not None
        return self._namespace.read_bytes(name).decode("utf-8")

    def write_text_atomic(self, name: str, text: str) -> None:
        self.assert_canonical()
        assert self._namespace is not None
        self._namespace.write_text_atomic(name, text)

    def publish_directory_tree(self, name: str, source: Path) -> None:
        self.assert_canonical()
        assert self._namespace is not None
        self._namespace.publish_directory_tree(name, source)

    def remove_tree_entries(self, names: tuple[str, ...]) -> None:
        assert self._namespace is not None
        self._namespace.remove_tree_entries(names)

    def acquire(
        self,
        *,
        generation_binding_sha256: str,
        experiment_contract_sha256: str,
        sealed_candidate_manifest_sha256: str,
        config_semantic_sha256: str,
    ) -> InvocationLease:
        require_canonical_evidence_capabilities("CanonicalExecutionController.acquire")
        self.assert_canonical()
        if self._lease is not None:
            raise RuntimeError("canonical_stage12_invocation_already_acquired")
        assert self._namespace is not None
        if "execution_invocation_journal.jsonl" in self._namespace.direct_entries():
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
        self.assert_canonical()
        if self._invocation_consumed or self._sandbox_root is not None:
            raise RuntimeError("canonical_stage12_invocation_workspace_already_prepared")
        workspace_parent = Path(
            tempfile.mkdtemp(prefix="researchclaw-stage12-invocation-")
        ).resolve()
        diagnostics = workspace_parent / "diagnostics"
        invocation_root = diagnostics / "invocation-1"
        sandbox_root = invocation_root / "sandbox"
        sandbox_root.mkdir(parents=True, exist_ok=False)
        self._workspace_parent = workspace_parent
        self._sandbox_root = sandbox_root
        return sandbox_root

    @property
    def metadata_dir(self) -> Path:
        if self._workspace_parent is None:
            raise RuntimeError("canonical_stage12_invocation_workspace_not_prepared")
        return self._workspace_parent

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
        self.assert_canonical()
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
        self.assert_canonical()
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
                self._workspace_parent / "diagnostics",
                self._workspace_parent / "diagnostics/invocation-1",
                self._sandbox_root,
            )
        )

    def complete(self, lease: InvocationLease, *, result_sha256: str) -> None:
        self._require_active_lease(lease)
        self.assert_canonical()
        if not self._invocation_consumed:
            raise RuntimeError("canonical_stage12_invocation_not_consumed")
        self._publish_diagnostics()
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
        self.assert_canonical()
        if not failure_code:
            raise ValueError("canonical failure code must be nonempty")
        self._publish_diagnostics()
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
        self.assert_canonical()
        require_controller_lease(lease)
        if lease is not self._lease:
            raise PermissionError("canonical_stage12_invocation_lease_mismatch")
        from researchclaw.pipeline.canonical_experiment_evidence import (
            parse_execution_invocation_journal,
        )

        records = parse_execution_invocation_journal(
            self.read_text("execution_invocation_journal.jsonl")
        )
        if len(records) != 1 or records[0]["invocation_token"] != lease.invocation_token:
            raise PermissionError("canonical_stage12_invocation_lease_not_active")

    def _write_started(self, record: dict[str, object]) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        self.write_text_atomic("execution_invocation_journal.jsonl", text)

    def _append_terminal(self, record: dict[str, object]) -> None:
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        assert self._namespace is not None
        self._namespace.append_bytes(
            "execution_invocation_journal.jsonl", text.encode("utf-8")
        )
        self._lease = None

    def _publish_diagnostics(self) -> None:
        if self._diagnostics_published:
            return
        if self._workspace_parent is None:
            raise RuntimeError("canonical_stage12_diagnostics_missing")
        source = self._workspace_parent / "diagnostics"
        self.publish_directory_tree("diagnostics", source)
        self._diagnostics_published = True


def require_controller_lease(lease: object) -> InvocationLease:
    """Reject direct canonical Stage 12 sandbox calls without a real lease."""
    if not isinstance(lease, InvocationLease):
        raise PermissionError("canonical_stage12_invocation_lease_required")
    if lease.ordinal != 1:
        raise PermissionError("canonical_stage12_invocation_ordinal_invalid")
    return lease


class CanonicalRefinementController:
    """Own the Stage 13 generation lock, rollover, and authority invalidation."""

    def __init__(
        self,
        stage_dir: Path,
        lock_descriptor: int,
        release_lock: ReleaseGraphLock,
    ) -> None:
        self.stage_dir = stage_dir
        self._lock_descriptor: int | None = lock_descriptor
        self._release_lock: ReleaseGraphLock | None = release_lock
        self._namespace: BoundOutputNamespace | None = None

    @classmethod
    def prepare_generation(
        cls,
        run_dir: Path,
        stage_dir: Path,
    ) -> CanonicalRefinementController:
        require_canonical_evidence_capabilities("CanonicalRefinementController.prepare")
        if stage_dir != run_dir / "stage-13":
            raise RuntimeError("canonical_stage13_directory_mismatch")
        release_lock = ReleaseGraphLock.acquire(
            run_dir, "CanonicalRefinementController.prepare"
        )
        try:
            descriptor = _acquire_canonical_lock(release_lock)
        except Exception:
            release_lock.close()
            raise

        controller = cls(stage_dir, descriptor, release_lock)
        try:
            try:
                release_lock.ensure_run_directory("stage-13")
            except OSError as exc:
                raise RuntimeError("canonical_stage13_directory_unsafe") from exc
            release_lock.invalidate_experiment_commit_points(include_stage12=False)
            release_lock.roll_stage_generation("stage-13")
            controller._namespace = release_lock.open_stage_namespace("stage-13")
            controller.assert_canonical()
            return controller
        except Exception:
            controller.close()
            raise

    def close(self) -> None:
        namespace, self._namespace = self._namespace, None
        if namespace is not None:
            namespace.close()
        descriptor, self._lock_descriptor = self._lock_descriptor, None
        release_lock, self._release_lock = self._release_lock, None
        _close_controller_locks(descriptor, release_lock)

    def assert_canonical(self) -> None:
        if self._release_lock is None or self._namespace is None:
            raise RuntimeError("canonical_stage13_controller_closed")
        self._release_lock.assert_canonical()
        self._namespace.assert_canonical()

    def write_text_atomic(self, name: str, text: str) -> None:
        self.assert_canonical()
        assert self._namespace is not None
        self._namespace.write_text_atomic(name, text)

    def read_run_file(self, relative_path: str) -> bytes:
        """Read one upstream authority file through the held Stage 13 epoch."""
        self.assert_canonical()
        assert self._namespace is not None
        return self._namespace.read_run_file(relative_path)

    def publish_directory_tree(self, name: str, source: Path) -> None:
        self.assert_canonical()
        assert self._namespace is not None
        self._namespace.publish_directory_tree(name, source)

    def remove_tree_entries(self, names: tuple[str, ...]) -> None:
        assert self._namespace is not None
        self._namespace.remove_tree_entries(names)


class CanonicalAnalysisController:
    """Own Stage 14 invalidation, candidate staging, and promotion locking."""

    def __init__(
        self,
        *,
        authority: object,
        run_dir: Path,
        stage_dir: Path,
        lock_descriptor: int,
        release_lock: ReleaseGraphLock | None,
        purpose: str,
    ) -> None:
        if authority is not _ANALYSIS_CONTROLLER_AUTHORITY:
            raise RuntimeError("canonical_stage14_controller_untrusted")
        if purpose not in {"generation", "promotion", "reader"}:
            raise RuntimeError("canonical_stage14_controller_purpose_invalid")
        self.run_dir = run_dir
        self.stage_dir = stage_dir
        self._purpose = purpose
        self._lock_descriptor: int | None = lock_descriptor
        self._release_lock: ReleaseGraphLock | None = release_lock
        self._namespace: BoundOutputNamespace | None = None

    @classmethod
    def prepare_generation(
        cls,
        run_dir: Path,
        stage_dir: Path,
    ) -> CanonicalAnalysisController:
        controller = cls._acquire(
            run_dir,
            stage_dir,
            "CanonicalAnalysisController.prepare",
            release_mode="write",
            create_stage=True,
            purpose="generation",
        )
        try:
            if stage_dir != run_dir / "stage-14":
                raise RuntimeError("canonical_stage14_directory_mismatch")
            controller._invalidate_root_authority()
            assert controller._namespace is not None
            controller._namespace.remove_tree_entries((
                "analysis.md",
                "experiment_summary.json",
                "results_table.tex",
                "figure_plan.json",
                "figure_plan_final.json",
                "charts",
                "perspectives",
            ))
            transient = tuple(
                name
                for name in controller._namespace.direct_entries()
                if name.startswith((".candidate-staging-", ".candidate-rejected-"))
            )
            controller._namespace.remove_tree_entries(transient)
            controller._namespace.ensure_directory("evidence_candidates")
            controller.assert_canonical()
            return controller
        except Exception:
            controller.close()
            raise

    @classmethod
    def acquire_promotion(cls, run_dir: Path) -> CanonicalAnalysisController:
        return cls._acquire(
            run_dir,
            run_dir / "stage-14",
            "CanonicalAnalysisController.promote",
            release_mode="write",
            purpose="promotion",
        )

    @classmethod
    def acquire_reader(cls, run_dir: Path) -> CanonicalAnalysisController:
        """Hold the publication lock while one consumer snapshots the bundle."""
        return cls._acquire(
            run_dir,
            run_dir / "stage-14",
            "load_canonical_experiment_evidence",
            release_mode="read",
            open_namespace=False,
            purpose="reader",
        )

    @classmethod
    def _acquire(
        cls,
        run_dir: Path,
        stage_dir: Path,
        entrypoint: str,
        *,
        release_mode: str,
        create_stage: bool = False,
        open_namespace: bool = True,
        purpose: str,
    ) -> CanonicalAnalysisController:
        require_canonical_evidence_capabilities(entrypoint)
        release_lock = ReleaseGraphLock.acquire(
            run_dir, entrypoint, mode=release_mode
        )
        descriptor: int | None = None
        try:
            descriptor = _acquire_canonical_lock(release_lock)
            namespace = (
                release_lock.open_stage_namespace(
                    "stage-14", create_stage=create_stage
                )
                if open_namespace
                else None
            )
        except Exception as exc:
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            release_lock.close()
            if create_stage:
                raise RuntimeError("canonical_stage14_directory_unsafe") from exc
            raise
        controller = cls(
            authority=_ANALYSIS_CONTROLLER_AUTHORITY,
            run_dir=run_dir,
            stage_dir=stage_dir,
            lock_descriptor=descriptor,
            release_lock=release_lock,
            purpose=purpose,
        )
        controller._namespace = namespace
        return controller

    def require_active_promotion(self, run_dir: Path) -> None:
        """Prove this is the live Stage 14 writer created for promotion."""

        self._require_active_writer_purpose(run_dir, "promotion")

    def require_active_generation(self, run_dir: Path) -> None:
        """Prove this is the live Stage 14 writer created for generation."""

        self._require_active_writer_purpose(run_dir, "generation")

    def _require_active_writer_purpose(
        self, run_dir: Path, expected_purpose: str
    ) -> None:
        """Bind a trusted controller purpose to its live writer epoch."""

        if type(self) is not CanonicalAnalysisController:
            raise RuntimeError("canonical_stage14_promotion_controller_untrusted")
        if self._purpose != expected_purpose:
            raise RuntimeError(
                f"canonical_stage14_{expected_purpose}_controller_required"
            )
        if self.run_dir.absolute() != run_dir.absolute():
            raise RuntimeError(
                f"canonical_stage14_{expected_purpose}_controller_run_mismatch"
            )
        if self.stage_dir != self.run_dir / "stage-14":
            raise RuntimeError(
                f"canonical_stage14_{expected_purpose}_controller_stage_mismatch"
            )
        if self._release_lock is None or self._namespace is None:
            raise RuntimeError(
                f"canonical_stage14_{expected_purpose}_controller_inactive"
            )
        from researchclaw.pipeline.release_graph_lock import (
            require_active_writer_epoch,
            require_namespace_owned_by_epoch,
        )

        require_active_writer_epoch(run_dir, self._release_lock)
        require_namespace_owned_by_epoch(
            self._namespace, self._release_lock, "stage-14"
        )
        self.assert_canonical()

    def create_candidate_staging(self) -> Path:
        self.assert_canonical()
        if self._lock_descriptor is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        staging = Path(
            tempfile.mkdtemp(prefix="researchclaw-stage14-candidate-")
        ).resolve()
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError("canonical_stage14_staging_unsafe")
        return staging

    def assert_canonical(self) -> None:
        if self._release_lock is None or self._lock_descriptor is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        self._release_lock.assert_canonical()
        if self._namespace is not None:
            self._namespace.assert_canonical()

    def publish_candidate_tree(self, candidate_id: str, source: Path) -> None:
        self.assert_canonical()
        assert self._namespace is not None
        self._namespace.publish_tree_child("evidence_candidates", candidate_id, source)

    def remove_candidate_tree(self, candidate_id: str) -> None:
        assert self._namespace is not None
        self._namespace.remove_tree_child("evidence_candidates", candidate_id)

    def read_candidate_tree(self) -> dict[str, bytes]:
        """Capture the complete immutable candidate collection under Stage 14."""
        self.assert_canonical()
        assert self._namespace is not None
        return self._namespace.read_directory_tree("evidence_candidates")

    def run_entries(self) -> tuple[str, ...]:
        """List direct run entries through the held Stage 14 writer epoch."""
        self.assert_canonical()
        if self._namespace is not None:
            return self._namespace.run_entries()
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        with self._release_lock.open_stage_namespace("stage-14") as namespace:
            return namespace.run_entries()

    def read_stage_entries(self, stage_name: str) -> tuple[str, ...]:
        """Read one Stage 14 generation without reopening the live run path."""
        self.assert_canonical()
        if stage_name == "stage-14" and self._namespace is not None:
            return self._namespace.direct_entries()
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        with self._release_lock.open_stage_namespace(stage_name) as namespace:
            return namespace.direct_entries()

    def read_stage_candidate_tree(self, stage_name: str) -> dict[str, bytes]:
        """Capture a candidate tree from one held Stage 14 generation."""
        self.assert_canonical()
        if stage_name == "stage-14" and self._namespace is not None:
            return self._namespace.read_directory_tree("evidence_candidates")
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        with self._release_lock.open_stage_namespace(stage_name) as namespace:
            return namespace.read_directory_tree("evidence_candidates")

    def read_run_file(self, relative_path: str) -> bytes:
        """Read one upstream authority file through the held Stage 14 epoch."""
        self.assert_canonical()
        if self._namespace is not None:
            return self._namespace.read_run_file(relative_path)
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        with self._release_lock.open_stage_namespace("stage-14") as namespace:
            return namespace.read_run_file(relative_path)

    def write_run_bytes_atomic(self, name: str, content: bytes) -> None:
        """Publish one root compatibility file through the held run fd."""
        self.assert_canonical()
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        self._release_lock.write_run_bytes_atomic(name, content)

    def remove_run_files(self, names: tuple[str, ...]) -> None:
        """Invalidate root authority without reopening the live run path."""
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        self._release_lock.remove_run_files(names)

    def _invalidate_root_authority(self) -> None:
        if self._release_lock is None:
            raise RuntimeError("canonical_stage14_controller_closed")
        self._release_lock.remove_run_files(
            (
                "canonical_experiment_evidence.json",
                "experiment_summary_best.json",
                "analysis_best.md",
            )
        )

    def close(self) -> None:
        namespace, self._namespace = self._namespace, None
        if namespace is not None:
            namespace.close()
        descriptor, self._lock_descriptor = self._lock_descriptor, None
        release_lock, self._release_lock = self._release_lock, None
        _close_controller_locks(descriptor, release_lock)
