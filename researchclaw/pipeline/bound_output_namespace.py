"""Directory-fd-bound publication helpers for canonical stage outputs."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
        cls,
        run_dir: Path,
        stage_dir: Path,
        stage_name: str,
        *,
        create_stage: bool = False,
    ) -> "BoundOutputNamespace":
        if stage_dir != run_dir / stage_name:
            raise OSError(f"{stage_name} output directory is not the canonical path")
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("directory-fd-bound output publication is unsupported")
        # os.replace shares os.rename's dir-fd capability on CPython.
        required_dir_fd_functions = (
            os.open,
            os.stat,
            os.unlink,
            os.rename,
            os.mkdir,
            os.rmdir,
        )
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
            if create_stage:
                try:
                    os.mkdir(stage_name, 0o700, dir_fd=run_fd)
                except FileExistsError:
                    pass
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

    @classmethod
    def open_from_run_fd(
        cls,
        run_dir: Path,
        stage_dir: Path,
        stage_name: str,
        run_fd: int,
        *,
        create_stage: bool = False,
    ) -> "BoundOutputNamespace":
        """Take ownership of a held run fd and open the stage relative to it."""

        if stage_dir != run_dir / stage_name:
            os.close(run_fd)
            raise OSError(f"{stage_name} output directory is not the canonical path")
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            os.close(run_fd)
            raise OSError("directory-fd-bound output publication is unsupported")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            run_info = os.fstat(run_fd)
            if not stat.S_ISDIR(run_info.st_mode):
                raise OSError("held run namespace is not a directory")
            if create_stage:
                try:
                    os.mkdir(stage_name, 0o700, dir_fd=run_fd)
                except FileExistsError:
                    pass
            stage_fd = os.open(stage_name, directory_flags, dir_fd=run_fd)
            try:
                stage_info = os.fstat(stage_fd)
                stage_entry_info = os.stat(
                    stage_name, dir_fd=run_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(stage_info.st_mode) or _identity(
                    stage_info
                ) != _identity(stage_entry_info):
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

    def remove_flat_entries(self, names: tuple[str, ...]) -> None:
        """Remove owned direct entries without following their path targets."""

        for name in names:
            _require_child_name(name)
            self._remove_flat_entry(name)

    def remove_tree_entries(self, names: tuple[str, ...]) -> None:
        for name in names:
            _remove_tree_at(self._stage_fd, name)

    def quarantine_tree_entry(self, name: str) -> None:
        """Remove a tree name from authority before best-effort recursive cleanup."""

        _require_child_name(name)
        try:
            os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        quarantine = f".{name}.rejected-{uuid.uuid4().hex}"
        os.rename(
            name,
            quarantine,
            src_dir_fd=self._stage_fd,
            dst_dir_fd=self._stage_fd,
        )
        try:
            _remove_tree_at(self._stage_fd, quarantine)
        except OSError as exc:
            raise OSError(
                f"quarantined tree cleanup failed: {quarantine}: {exc}"
            ) from exc

    def ensure_directory(self, name: str) -> None:
        """Create or validate one direct directory without reopening its path."""

        _require_child_name(name)
        try:
            info = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=self._stage_fd)
            return
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"output directory entry is unsafe: {name}")

    def write_text_atomic(self, name: str, text: str) -> None:
        self.write_bytes_atomic(name, text.encode("utf-8"))

    def write_new_text_atomic(self, name: str, text: str) -> None:
        """Publish a complete new file and reject any target collision."""

        _require_child_name(name)
        if os.link not in os.supports_dir_fd:
            raise OSError("collision-safe output publication is unsupported")
        content = text.encode("utf-8")
        temporary = f"{name}.tmp"
        try:
            _write_new_file(self._stage_fd, temporary, content)
            os.link(
                temporary,
                name,
                src_dir_fd=self._stage_fd,
                dst_dir_fd=self._stage_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=self._stage_fd)
            temporary = ""
            os.fsync(self._stage_fd)
            self.assert_canonical()
        except Exception:
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=self._stage_fd)
                except FileNotFoundError:
                    pass
            raise

    def append_bytes(self, name: str, content: bytes) -> None:
        _require_child_name(name)
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=self._stage_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"append target is not a regular file: {name}")
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise OSError("append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_bytes_atomic(self, name: str, content: bytes) -> None:
        _require_child_name(name)
        temporary = f"{name}.tmp"
        self.invalidate((temporary,))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self._stage_fd)
        try:
            temporary_identity = _identity(os.fstat(descriptor))
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

    def publish_flat_directory(
        self, name: str, files: Mapping[str, bytes]
    ) -> None:
        """Publish one exact flat directory before its caller writes a commit point."""

        _require_child_name(name)
        if not files or any(not isinstance(content, bytes) for content in files.values()):
            raise OSError(f"flat output directory is empty or invalid: {name}")
        for child in files:
            _require_child_name(child)
        staging = f".{name}.tmp"
        quarantine = f".{name}.replaced"
        self._remove_flat_entry(staging)
        self._remove_flat_entry(quarantine)

        os.mkdir(staging, 0o700, dir_fd=self._stage_fd)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        staging_fd = os.open(staging, directory_flags, dir_fd=self._stage_fd)
        try:
            for child, content in sorted(files.items()):
                _write_new_file(staging_fd, child, content)
            os.fsync(staging_fd)
        except Exception:
            os.close(staging_fd)
            self._remove_flat_entry(staging)
            raise
        else:
            os.close(staging_fd)

        try:
            current = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            if stat.S_ISDIR(current.st_mode):
                os.rename(
                    name,
                    quarantine,
                    src_dir_fd=self._stage_fd,
                    dst_dir_fd=self._stage_fd,
                )
            else:
                os.unlink(name, dir_fd=self._stage_fd)
        os.rename(
            staging,
            name,
            src_dir_fd=self._stage_fd,
            dst_dir_fd=self._stage_fd,
        )
        self.assert_canonical()
        self._remove_flat_entry(quarantine)

    def publish_directory_tree(self, name: str, source: Path) -> None:
        """Publish an arbitrary regular-file tree through the held stage fd."""

        _require_child_name(name)
        source_fd = _open_source_directory(source)
        staging = f".{name}.tmp-{uuid.uuid4().hex}"
        quarantine = f".{name}.replaced-{uuid.uuid4().hex}"
        try:
            os.mkdir(staging, 0o700, dir_fd=self._stage_fd)
        except Exception:
            os.close(source_fd)
            raise
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        staging_fd = os.open(staging, flags, dir_fd=self._stage_fd)
        try:
            _copy_tree_fd_to_fd(source_fd, staging_fd)
            _assert_source_directory_unchanged(source, source_fd)
            os.fsync(staging_fd)
        except Exception:
            os.close(staging_fd)
            _remove_tree_at(self._stage_fd, staging)
            raise
        else:
            os.close(staging_fd)
        finally:
            os.close(source_fd)

        replaced = False
        try:
            current = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            if stat.S_ISDIR(current.st_mode):
                os.rename(
                    name,
                    quarantine,
                    src_dir_fd=self._stage_fd,
                    dst_dir_fd=self._stage_fd,
                )
                replaced = True
            else:
                os.unlink(name, dir_fd=self._stage_fd)
        published = False
        try:
            os.rename(
                staging,
                name,
                src_dir_fd=self._stage_fd,
                dst_dir_fd=self._stage_fd,
            )
            published = True
            self.assert_canonical()
        except Exception as exc:
            try:
                if published:
                    _remove_tree_at(self._stage_fd, name)
                else:
                    _remove_tree_at(self._stage_fd, staging)
                if replaced:
                    os.rename(
                        quarantine,
                        name,
                        src_dir_fd=self._stage_fd,
                        dst_dir_fd=self._stage_fd,
                    )
            except OSError as cleanup_exc:
                exc.add_note(f"tree publication rollback failed: {cleanup_exc}")
            raise
        if replaced:
            _remove_tree_at(self._stage_fd, quarantine)

    def publish_tree_child(self, parent: str, name: str, source: Path) -> None:
        """Publish one immutable tree below an existing held child directory."""

        _require_child_name(parent)
        _require_child_name(name)
        source_fd = _open_source_directory(source)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            parent_fd = os.open(parent, flags, dir_fd=self._stage_fd)
        except Exception:
            os.close(source_fd)
            raise
        staging = f".{name}.tmp-{uuid.uuid4().hex}"
        published = False
        try:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"nested publication already exists: {parent}/{name}")
            os.mkdir(staging, 0o700, dir_fd=parent_fd)
            staging_fd = os.open(staging, flags, dir_fd=parent_fd)
            try:
                _copy_tree_fd_to_fd(source_fd, staging_fd)
                _assert_source_directory_unchanged(source, source_fd)
                os.fsync(staging_fd)
            except Exception:
                os.close(staging_fd)
                _remove_tree_at(parent_fd, staging)
                raise
            else:
                os.close(staging_fd)
            os.rename(staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            published = True
            try:
                self.assert_canonical()
            except Exception as exc:
                try:
                    _remove_tree_at(parent_fd, name)
                except OSError as cleanup_exc:
                    exc.add_note(
                        f"nested tree publication rollback failed: {cleanup_exc}"
                    )
                raise
        except Exception:
            if not published:
                _remove_tree_at(parent_fd, staging)
            raise
        finally:
            os.close(source_fd)
            os.close(parent_fd)

    def remove_tree_child(self, parent: str, name: str) -> None:
        _require_child_name(parent)
        _require_child_name(name)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(parent, flags, dir_fd=self._stage_fd)
        try:
            _remove_tree_at(parent_fd, name)
        finally:
            os.close(parent_fd)

    def stage_flat_tree(
        self,
        name: str,
        *,
        direct_files: Mapping[str, bytes],
        flat_directories: Mapping[str, Mapping[str, bytes]],
    ) -> None:
        """Create one same-filesystem staging tree with exact flat children."""

        _require_child_name(name)
        self._remove_flat_entry(name)
        os.mkdir(name, 0o700, dir_fd=self._stage_fd)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        tree_fd = os.open(name, flags, dir_fd=self._stage_fd)
        try:
            for child, content in sorted(direct_files.items()):
                _require_child_name(child)
                _write_new_file(tree_fd, child, content)
            for directory, files in sorted(flat_directories.items()):
                _require_child_name(directory)
                os.mkdir(directory, 0o700, dir_fd=tree_fd)
                child_fd = os.open(directory, flags, dir_fd=tree_fd)
                try:
                    for child, content in sorted(files.items()):
                        _require_child_name(child)
                        _write_new_file(child_fd, child, content)
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
            os.fsync(tree_fd)
        except Exception:
            os.close(tree_fd)
            self._remove_flat_entry(name)
            raise
        else:
            os.close(tree_fd)

    def read_flat_tree(
        self, name: str
    ) -> tuple[dict[str, bytes], dict[str, dict[str, bytes]]]:
        """Read an exact staged tree without following any symlink."""

        _require_child_name(name)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        tree_fd = os.open(name, flags, dir_fd=self._stage_fd)
        direct: dict[str, bytes] = {}
        directories: dict[str, dict[str, bytes]] = {}
        try:
            for child in sorted(os.listdir(tree_fd)):
                _require_child_name(child)
                info = os.stat(child, dir_fd=tree_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    direct[child] = _read_regular_file(tree_fd, child)
                elif stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(child, flags, dir_fd=tree_fd)
                    try:
                        files: dict[str, bytes] = {}
                        for leaf in sorted(os.listdir(child_fd)):
                            _require_child_name(leaf)
                            files[leaf] = _read_regular_file(child_fd, leaf)
                        directories[child] = files
                    finally:
                        os.close(child_fd)
                else:
                    raise OSError(f"staged tree contains unsafe entry: {child}")
            return direct, directories
        finally:
            os.close(tree_fd)

    def publish_staged_tree(
        self,
        name: str,
        *,
        direct_names: tuple[str, ...],
        directory_names: tuple[str, ...],
    ) -> None:
        """Move a validated staged tree into the live namespace."""

        _require_child_name(name)
        expected = set(direct_names) | set(directory_names)
        direct, directories = self.read_flat_tree(name)
        if set(direct) != set(direct_names) or set(directories) != set(directory_names):
            raise OSError("staged tree namespace mismatch")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        tree_fd = os.open(name, flags, dir_fd=self._stage_fd)
        try:
            for child in sorted(expected):
                self._remove_flat_entry(child)
                os.rename(
                    child,
                    child,
                    src_dir_fd=tree_fd,
                    dst_dir_fd=self._stage_fd,
                )
        finally:
            os.close(tree_fd)
        os.rmdir(name, dir_fd=self._stage_fd)
        self.assert_canonical()

    def reset_flat_namespace(self) -> None:
        """Remove prior direct files and flat directories from this owned stage."""

        for name in os.listdir(self._stage_fd):
            _require_child_name(name)
            self._remove_flat_entry(name)

    def _remove_flat_entry(self, name: str) -> None:
        _require_child_name(name)
        try:
            info = os.stat(name, dir_fd=self._stage_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=self._stage_fd)
            return
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, directory_flags, dir_fd=self._stage_fd)
        try:
            for child in os.listdir(descriptor):
                _require_child_name(child)
                child_info = os.stat(
                    child, dir_fd=descriptor, follow_symlinks=False
                )
                if not stat.S_ISREG(child_info.st_mode):
                    raise OSError(
                        f"flat output directory contains unsafe entry: {name}/{child}"
                    )
                os.unlink(child, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=self._stage_fd)

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

    def read_run_file(self, relative_path: str) -> bytes:
        """Read one regular run-relative file through the held run fd."""

        parts = _relative_parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.dup(self._run_fd)
        opened = [descriptor]
        try:
            for part in parts[:-1]:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                opened.append(descriptor)
            return _read_regular_file(descriptor, parts[-1])
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)

    def invalidate_run_files(self, names: tuple[str, ...]) -> None:
        """Remove owned direct run files through the held run fd."""

        for name in names:
            _require_child_name(name)
            try:
                info = os.stat(name, dir_fd=self._run_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"run output collision is not a regular file: {name}")
            os.unlink(name, dir_fd=self._run_fd)

    def write_run_file_atomic(self, name: str, content: bytes) -> None:
        """Atomically replace one owned direct run file via the held run fd."""

        _require_child_name(name)
        temporary = f"{name}.tmp"
        try:
            _write_new_file(self._run_fd, temporary, content)
            try:
                current = os.stat(name, dir_fd=self._run_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and not stat.S_ISREG(current.st_mode):
                raise OSError(f"run output collision is not a regular file: {name}")
            os.replace(
                temporary,
                name,
                src_dir_fd=self._run_fd,
                dst_dir_fd=self._run_fd,
            )
            temporary = ""
            os.fsync(self._run_fd)
            self.assert_canonical()
        except Exception:
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=self._run_fd)
                except FileNotFoundError:
                    pass
            raise

    def run_entries(self) -> tuple[str, ...]:
        """List direct entries of the held run directory."""

        return tuple(sorted(os.listdir(self._run_fd)))

    def read_run_directory_entries(self, relative_path: str) -> tuple[str, ...]:
        """List a held run-relative directory without following symlinks."""

        parts = _relative_parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.dup(self._run_fd)
        opened = [descriptor]
        try:
            for part in parts:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                opened.append(descriptor)
            return tuple(sorted(os.listdir(descriptor)))
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)

    def direct_entries(self) -> tuple[str, ...]:
        return tuple(sorted(os.listdir(self._stage_fd)))

    def read_flat_directory(self, name: str) -> dict[str, bytes]:
        _require_child_name(name)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, directory_flags, dir_fd=self._stage_fd)
        try:
            result: dict[str, bytes] = {}
            for child in sorted(os.listdir(descriptor)):
                _require_child_name(child)
                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                child_fd = os.open(child, flags, dir_fd=descriptor)
                try:
                    info = os.fstat(child_fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError(
                            f"flat output directory contains non-file: {name}/{child}"
                        )
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(child_fd, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    result[child] = b"".join(chunks)
                finally:
                    os.close(child_fd)
            return result
        finally:
            os.close(descriptor)

    def read_directory_tree(self, name: str) -> dict[str, bytes]:
        """Read an exact regular-file tree relative to the held stage fd."""

        _require_child_name(name)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=self._stage_fd)
        try:
            return _read_directory_tree(descriptor, prefix="")
        finally:
            os.close(descriptor)

    def write_tree_file_atomic(
        self, tree: str, relative_path: str, content: bytes
    ) -> None:
        """Write a nested tree commit point without reopening the live path."""

        _require_child_name(tree)
        parts = _relative_parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(tree, flags, dir_fd=self._stage_fd)
        opened = [descriptor]
        temporary: str | None = None
        try:
            for part in parts[:-1]:
                descriptor = os.open(part, flags, dir_fd=descriptor)
                opened.append(descriptor)
            temporary = f".{parts[-1]}.tmp-{uuid.uuid4().hex}"
            _write_new_file(descriptor, temporary, content)
            os.replace(
                temporary,
                parts[-1],
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            os.fsync(descriptor)
            self.assert_canonical()
        except Exception:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            raise
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _require_child_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise OSError(f"output name is not a direct child: {name!r}")


def _write_new_file(directory_fd: int, name: str, content: bytes) -> None:
    if not isinstance(content, bytes):
        raise OSError(f"output content is not bytes: {name}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("staged output write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"staged output is not a regular file: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _read_directory_tree(directory_fd: int, *, prefix: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    initial_names = tuple(sorted(os.listdir(directory_fd)))
    for name in initial_names:
        _require_child_name(name)
        relative = f"{prefix}/{name}" if prefix else name
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            result[relative] = _read_regular_file(directory_fd, name)
        elif stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                result.update(_read_directory_tree(child_fd, prefix=relative))
            finally:
                os.close(child_fd)
        else:
            raise OSError(f"directory tree contains unsafe entry: {relative}")
    if tuple(sorted(os.listdir(directory_fd))) != initial_names:
        raise OSError("directory tree namespace changed while reading")
    return result


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OSError("tree path is unsafe")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise OSError("tree path is unsafe")
    return parts


def _open_source_directory(source: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(source, flags)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("tree publication source is not a directory")
    return descriptor


def _assert_source_directory_unchanged(source: Path, descriptor: int) -> None:
    held = os.fstat(descriptor)
    live = os.stat(source, follow_symlinks=False)
    if not stat.S_ISDIR(live.st_mode) or _identity(live) != _identity(held):
        raise OSError("tree publication source root changed during copy")


def _copy_tree_fd_to_fd(source_fd: int, destination_fd: int) -> None:
    initial_names = tuple(sorted(os.listdir(source_fd)))
    for name in initial_names:
        _require_child_name(name)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0)
        child_fd = os.open(name, flags, dir_fd=source_fd)
        try:
            source_info = os.fstat(child_fd)
            source_identity = _identity(source_info)
            if stat.S_ISREG(source_info.st_mode):
                _write_new_file(destination_fd, name, _read_all_fd(child_fd))
            elif stat.S_ISDIR(source_info.st_mode):
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                destination_child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=destination_fd,
                )
                try:
                    _copy_tree_fd_to_fd(child_fd, destination_child_fd)
                    os.fsync(destination_child_fd)
                finally:
                    os.close(destination_child_fd)
            else:
                raise OSError(f"tree publication source contains unsafe entry: {name}")
            current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if _identity(current) != source_identity or current.st_mode != source_info.st_mode:
                raise OSError(f"tree publication source changed during copy: {name}")
        finally:
            os.close(child_fd)
    if tuple(sorted(os.listdir(source_fd))) != initial_names:
        raise OSError("tree publication source namespace changed during copy")


def _read_all_fd(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    _require_child_name(name)
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        for child in os.listdir(descriptor):
            _remove_tree_at(descriptor, child)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)
