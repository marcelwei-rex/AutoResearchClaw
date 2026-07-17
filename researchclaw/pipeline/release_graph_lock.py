"""Run-local lock that linearizes canonical release graph readers and writers."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import uuid
from contextvars import ContextVar, Token
from functools import wraps
from pathlib import Path
from typing import Callable, ParamSpec, TypeVar


_LOCK_AUTHORITY = object()
_HELD_LOCK: ContextVar[tuple[str, str, "ReleaseGraphLock"] | None] = ContextVar(
    "canonical_release_graph_lock", default=None
)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class ReleaseGraphLock:
    """An active OS lock owner or a lifetime-bound lease borrowed from one."""

    def __init__(
        self,
        *,
        authority: object,
        run_dir: Path,
        run_fd: int,
        lock_fd: int,
        run_identity: tuple[int, int],
        mode: str,
        owner: "ReleaseGraphLock | None" = None,
    ) -> None:
        if authority is not _LOCK_AUTHORITY:
            raise RuntimeError("release_graph_lease_untrusted")
        self.run_dir = run_dir
        self._run_fd = run_fd
        self._lock_fd = lock_fd
        self._run_identity = run_identity
        self._mode = mode
        self._owner = owner
        self._context_token: Token[
            tuple[str, str, "ReleaseGraphLock"] | None
        ] | None = None
        self._active = True
        self._borrow_count = 0
        self._closing = False

    @classmethod
    def acquire(
        cls, run_dir: Path, entrypoint: str, *, mode: str = "write"
    ) -> "ReleaseGraphLock":
        if mode not in {"read", "write"}:
            raise ValueError("invalid release graph lock mode")
        run_key = str(run_dir.absolute())
        held = _HELD_LOCK.get()
        if held is not None and held[0] == run_key:
            owner = held[2]
            if owner._active:
                owner.assert_canonical()
                if held[1] == "read" and mode == "write":
                    raise RuntimeError(
                        f"release_graph_generation_locked:{entrypoint}"
                    )
                owner._borrow_count += 1
                return cls(
                    authority=_LOCK_AUTHORITY,
                    run_dir=run_dir,
                    run_fd=owner._run_fd,
                    lock_fd=owner._lock_fd,
                    run_identity=owner._run_identity,
                    mode=mode,
                    owner=owner,
                )
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("release_graph_lock_unsupported")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        run_fd = os.open(run_dir, directory_flags)
        try:
            run_info = os.fstat(run_fd)
            live_info = os.stat(run_dir, follow_symlinks=False)
            identity = (run_info.st_dev, run_info.st_ino)
            if not stat.S_ISDIR(run_info.st_mode) or identity != (
                live_info.st_dev,
                live_info.st_ino,
            ):
                raise RuntimeError("release_graph_run_directory_changed")
            # Lock the held run-directory inode itself. A named lock file can be
            # unlinked and recreated while an existing flock still protects the
            # detached inode, creating two independent writer epochs.
            lock_fd = os.dup(run_fd)
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        f"release_graph_generation_locked:{entrypoint}"
                    ) from exc
            except Exception:
                os.close(lock_fd)
                raise
        except Exception:
            os.close(run_fd)
            raise
        result = cls(
            authority=_LOCK_AUTHORITY,
            run_dir=run_dir,
            run_fd=run_fd,
            lock_fd=lock_fd,
            run_identity=identity,
            mode=mode,
        )
        result._context_token = _HELD_LOCK.set((run_key, mode, result))
        return result

    def _require_active(self) -> "ReleaseGraphLock":
        owner = self._owner or self
        if not self._active or not owner._active or owner._closing:
            raise RuntimeError("release_graph_lease_inactive")
        if owner._run_fd < 0 or owner._lock_fd < 0:
            raise RuntimeError("release_graph_lease_inactive")
        try:
            run_info = os.fstat(owner._run_fd)
            lock_info = os.fstat(owner._lock_fd)
        except OSError as exc:
            raise RuntimeError("release_graph_lease_inactive") from exc
        if not stat.S_ISDIR(run_info.st_mode) or not stat.S_ISDIR(lock_info.st_mode):
            raise RuntimeError("release_graph_lease_inactive")
        if (run_info.st_dev, run_info.st_ino) != owner._run_identity:
            raise RuntimeError("release_graph_lease_inactive")
        return owner

    def assert_canonical(self) -> None:
        owner = self._require_active()
        live_info = os.stat(owner.run_dir, follow_symlinks=False)
        if not stat.S_ISDIR(live_info.st_mode) or (
            live_info.st_dev,
            live_info.st_ino,
        ) != owner._run_identity:
            raise RuntimeError("release_graph_run_directory_changed")

    def invalidate_experiment_commit_points(
        self,
        *,
        include_stage12: bool = True,
        include_stage13: bool = True,
    ) -> None:
        """Invalidate detached Stage 12-14 authority without reopening live paths."""

        owner = self._require_active()
        errors: list[str] = []
        relatives = [
            "canonical_experiment_evidence.json",
            "experiment_summary_best.json",
            "analysis_best.md",
        ]
        if include_stage12:
            relatives.append("stage-12/experiment_result_set.json")
        if include_stage13:
            relatives.append("stage-13/refinement_result_set.json")
        for relative in relatives:
            parts = relative.split("/")
            parent_fd = owner._run_fd
            opened_fd: int | None = None
            try:
                if len(parts) == 2:
                    try:
                        opened_fd = os.open(
                            parts[0],
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=owner._run_fd,
                        )
                    except FileNotFoundError:
                        continue
                    parent_fd = opened_fd
                try:
                    info = os.stat(
                        parts[-1], dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    errors.append(f"{relative}: commit point is a directory")
                else:
                    os.unlink(parts[-1], dir_fd=parent_fd)
            except OSError as exc:
                errors.append(f"{relative}: {exc}")
            finally:
                if opened_fd is not None:
                    os.close(opened_fd)
        if errors:
            raise RuntimeError(
                "release_graph_commit_point_invalidation_incomplete: "
                + "; ".join(errors)
            )

    def remove_run_files(self, names: tuple[str, ...]) -> None:
        owner = self._require_active()
        for name in names:
            _require_run_child(name)
            try:
                info = os.stat(name, dir_fd=owner._run_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                raise OSError(f"run output collision is a directory: {name}")
            os.unlink(name, dir_fd=owner._run_fd)

    def duplicate_run_fd(self) -> int:
        owner = self._require_active()
        return os.dup(owner._run_fd)

    def ensure_run_directory(self, name: str) -> None:
        owner = self._require_active()
        _require_run_child(name)
        try:
            info = os.stat(name, dir_fd=owner._run_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=owner._run_fd)
            return
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"run directory entry is unsafe: {name}")

    def roll_stage_generation(self, stage_name: str) -> None:
        """Archive one nonempty stage and create a new live directory via run fd."""

        owner = self._require_active()
        _require_run_child(stage_name)
        try:
            info = os.stat(stage_name, dir_fd=owner._run_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(stage_name, 0o700, dir_fd=owner._run_fd)
            return
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"stage generation is unsafe: {stage_name}")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        stage_fd = os.open(stage_name, flags, dir_fd=owner._run_fd)
        try:
            nonempty = bool(os.listdir(stage_fd))
        finally:
            os.close(stage_fd)
        if not nonempty:
            return
        versions: list[int] = []
        pattern = re.compile(re.escape(stage_name) + r"_v([1-9]\d*)")
        for entry in os.listdir(owner._run_fd):
            match = pattern.fullmatch(entry)
            if match is None:
                continue
            version_info = os.stat(
                entry, dir_fd=owner._run_fd, follow_symlinks=False
            )
            if not stat.S_ISDIR(version_info.st_mode):
                raise OSError(f"stage generation history is unsafe: {entry}")
            versions.append(int(match.group(1)))
        archive = f"{stage_name}_v{max(versions, default=0) + 1}"
        os.rename(
            stage_name,
            archive,
            src_dir_fd=owner._run_fd,
            dst_dir_fd=owner._run_fd,
        )
        os.mkdir(stage_name, 0o700, dir_fd=owner._run_fd)

    def open_stage_namespace(
        self, stage_name: str, *, create_stage: bool = False
    ) -> "BoundOutputNamespace":
        owner = self._require_active()
        from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace

        return BoundOutputNamespace.open_from_run_fd(
            owner.run_dir,
            owner.run_dir / stage_name,
            stage_name,
            os.dup(owner._run_fd),
            create_stage=create_stage,
        )

    def write_run_bytes_atomic(self, name: str, content: bytes) -> None:
        owner = self._require_active()
        _require_run_child(name)
        temporary = f".{name}.tmp-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=owner._run_fd)
        try:
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("run output write made no progress")
                offset += written
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=owner._run_fd)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(descriptor)
        os.replace(
            temporary,
            name,
            src_dir_fd=owner._run_fd,
            dst_dir_fd=owner._run_fd,
        )

    def close(self) -> None:
        if not self._active:
            return
        if self._owner is not None:
            self._active = False
            self._owner._borrow_count -= 1
            if self._owner._borrow_count == 0 and self._owner._closing:
                self._owner._finalize_close()
            return
        if self._borrow_count:
            self._closing = True
            self._active = False
            if self._context_token is not None:
                _HELD_LOCK.reset(self._context_token)
                self._context_token = None
            return
        self._active = False
        self._finalize_close()

    def _finalize_close(self) -> None:
        """Release one owner after its last lifetime-bound borrower closes."""

        if self._lock_fd < 0 and self._run_fd < 0:
            return
        self._active = False
        if self._context_token is not None:
            _HELD_LOCK.reset(self._context_token)
            self._context_token = None
        if self._lock_fd >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = -1
        if self._run_fd >= 0:
            os.close(self._run_fd)
            self._run_fd = -1

    def __enter__(self) -> "ReleaseGraphLock":
        self._require_active()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def require_active_writer_epoch(run_dir: Path, lease: object) -> ReleaseGraphLock:
    """Validate that a private writer helper received a live trusted lease."""

    if not isinstance(lease, ReleaseGraphLock) or lease._mode != "write":
        raise RuntimeError("release_graph_writer_lease_required")
    owner = lease._require_active()
    if str(owner.run_dir.absolute()) != str(run_dir.absolute()):
        raise RuntimeError("release_graph_writer_lease_run_mismatch")
    lease.assert_canonical()
    return lease


def require_active_release_graph_epoch(
    run_dir: Path, lease: object
) -> ReleaseGraphLock:
    """Validate a live trusted reader or writer lease for private replay helpers."""

    if not isinstance(lease, ReleaseGraphLock) or lease._mode not in {"read", "write"}:
        raise RuntimeError("release_graph_lease_required")
    owner = lease._require_active()
    if str(owner.run_dir.absolute()) != str(run_dir.absolute()):
        raise RuntimeError("release_graph_lease_run_mismatch")
    lease.assert_canonical()
    return lease


def require_namespace_owned_by_epoch(
    namespace: "BoundOutputNamespace",
    lease: object,
    expected_stage: str,
) -> ReleaseGraphLock:
    """Bind one held stage namespace to the same live release-graph epoch."""

    owner = require_active_release_graph_epoch(namespace.run_dir, lease)
    if namespace.stage_name != expected_stage:
        raise RuntimeError("release_graph_namespace_stage_mismatch")
    if namespace.stage_dir != owner.run_dir / expected_stage:
        raise RuntimeError("release_graph_namespace_path_mismatch")
    if str(namespace.run_dir.absolute()) != str(owner.run_dir.absolute()):
        raise RuntimeError("release_graph_namespace_run_mismatch")
    try:
        run_info = os.fstat(namespace._run_fd)
        stage_info = os.fstat(namespace._stage_fd)
    except OSError as exc:
        raise RuntimeError("release_graph_namespace_inactive") from exc
    if (
        not stat.S_ISDIR(run_info.st_mode)
        or (run_info.st_dev, run_info.st_ino) != owner._run_identity
        or (run_info.st_dev, run_info.st_ino) != namespace._run_identity
        or not stat.S_ISDIR(stage_info.st_mode)
        or (stage_info.st_dev, stage_info.st_ino) != namespace._stage_identity
    ):
        raise RuntimeError("release_graph_namespace_epoch_mismatch")
    namespace.assert_canonical()
    owner.assert_canonical()
    return owner


def release_graph_writer(
    function: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Require a real writer epoch for a public function whose first arg is run_dir."""

    @wraps(function)
    def wrapped(run_dir: Path, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with ReleaseGraphLock.acquire(
            run_dir, function.__name__, mode="write"
        ) as lease:
            result = function(run_dir, *args, **kwargs)
            require_active_writer_epoch(run_dir, lease)
            return result

    return wrapped  # type: ignore[return-value]


def _require_run_child(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError(f"run output name is not a direct child: {name!r}")
