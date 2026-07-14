"""Directory-fd-bound publication helpers for canonical stage outputs."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BoundOutputNamespace:
    """Hold a canonical stage directory open across its publication lifecycle."""

    run_dir: Path
    stage_dir: Path
    stage_name: str
    _run_fd: int
    _stage_fd: int
    _run_identity: tuple[int, int]
    _stage_identity: tuple[int, int]

    @classmethod
    def open(
        cls, run_dir: Path, stage_dir: Path, stage_name: str
    ) -> "BoundOutputNamespace":
        if stage_dir != run_dir / stage_name:
            raise OSError(f"{stage_name} output directory is not the canonical path")
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("directory-fd-bound output publication is unsupported")
        # os.replace shares os.rename's dir-fd capability on CPython.
        required_dir_fd_functions = (os.open, os.stat, os.unlink, os.rename)
        if any(function not in os.supports_dir_fd for function in required_dir_fd_functions):
            raise OSError("directory-fd-bound output operations are unsupported")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        run_fd = os.open(run_dir, directory_flags)
        try:
            run_info = os.fstat(run_fd)
            run_path_info = os.stat(run_dir, follow_symlinks=False)
            if not stat.S_ISDIR(run_info.st_mode) or _identity(run_info) != _identity(
                run_path_info
            ):
                raise OSError("run directory changed while opening output namespace")
            stage_fd = os.open(stage_name, directory_flags, dir_fd=run_fd)
            try:
                stage_info = os.fstat(stage_fd)
                stage_path_info = os.stat(
                    stage_name, dir_fd=run_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(stage_info.st_mode) or _identity(
                    stage_info
                ) != _identity(stage_path_info):
                    raise OSError(
                        f"{stage_name} directory changed while opening output namespace"
                    )
            except Exception:
                os.close(stage_fd)
                raise
        except Exception:
            os.close(run_fd)
            raise
        return cls(
            run_dir=run_dir,
            stage_dir=stage_dir,
            stage_name=stage_name,
            _run_fd=run_fd,
            _stage_fd=stage_fd,
            _run_identity=_identity(run_info),
            _stage_identity=_identity(stage_info),
        )

    def close(self) -> None:
        if self._stage_fd >= 0:
            os.close(self._stage_fd)
            self._stage_fd = -1
        if self._run_fd >= 0:
            os.close(self._run_fd)
            self._run_fd = -1

    def __enter__(self) -> "BoundOutputNamespace":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def assert_canonical(self) -> None:
        """Require both live paths to still name the held directories."""

        run_info = os.stat(self.run_dir, follow_symlinks=False)
        if not stat.S_ISDIR(run_info.st_mode) or _identity(run_info) != self._run_identity:
            raise OSError("run directory changed during output publication")
        stage_info = os.stat(
            self.stage_name, dir_fd=self._run_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(stage_info.st_mode)
            or _identity(stage_info) != self._stage_identity
        ):
            raise OSError(
                f"{self.stage_name} directory changed during output publication"
            )

    def invalidate(self, names: tuple[str, ...]) -> None:
        for name in names:
            _require_child_name(name)
            try:
                info = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                raise OSError(f"output collision is a directory: {name}")
            os.unlink(name, dir_fd=self._stage_fd)

    def write_text_atomic(self, name: str, text: str) -> None:
        _require_child_name(name)
        temporary = f"{name}.tmp"
        self.invalidate((temporary,))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self._stage_fd)
        try:
            temporary_identity = _identity(os.fstat(descriptor))
            content = text.encode("utf-8")
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("atomic output write made no progress")
                offset += written
            os.fsync(descriptor)
        except Exception as exc:
            os.close(descriptor)
            try:
                self.invalidate((temporary,))
            except OSError as cleanup_exc:
                exc.add_note(f"temporary output cleanup also failed: {cleanup_exc}")
            raise
        else:
            os.close(descriptor)
        current_temporary = os.stat(
            temporary, dir_fd=self._stage_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(current_temporary.st_mode)
            or _identity(current_temporary) != temporary_identity
        ):
            mismatch = OSError(f"atomic output temporary changed before replace: {name}")
            try:
                self.invalidate((temporary,))
            except OSError as cleanup_exc:
                mismatch.add_note(
                    f"temporary output cleanup also failed: {cleanup_exc}"
                )
            raise mismatch
        os.replace(
            temporary,
            name,
            src_dir_fd=self._stage_fd,
            dst_dir_fd=self._stage_fd,
        )

    def read_bytes(self, name: str) -> bytes:
        _require_child_name(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=self._stage_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"output is not a regular file: {name}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _require_child_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError(f"output name is not a direct child: {name!r}")
