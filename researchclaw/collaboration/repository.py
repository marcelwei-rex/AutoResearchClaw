"""Manifest-bound shared repository for canonical release artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping

ARTIFACT_TYPES = (
    "literature_summary",
    "experiment_results",
    "code_template",
    "review_feedback",
)
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_RE = re.compile(r"gen-([0-9a-f]{64})\Z")
_STAGING_RE = re.compile(r"\.publication-staging-[0-9a-f]{32}\Z")
_SCHEMA_VERSION = 1
_POINTER_NAME = "publication.json"
_MANIFEST_NAME = "bundle_manifest.json"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_DIRECTORY_FLAGS |= getattr(os, "O_CLOEXEC", 0)
_FILE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_FILE_READ_FLAGS |= getattr(os, "O_CLOEXEC", 0)


class RepositoryPublicationError(ValueError):
    """Raised when a repository publication is unsafe or not replayable."""


class ResearchRepository:
    """Publish and retrieve immutable, canonical-manifest-bound generations."""

    def __init__(self, repo_dir: str | Path = ".researchclaw/shared") -> None:
        self._repo_dir = Path(repo_dir)

    @property
    def repo_dir(self) -> Path:
        return self._repo_dir

    def publish(
        self,
        run_id: str,
        artifacts: dict[str, Any],
        *,
        canonical_manifest_path: str,
        canonical_manifest_sha256: str,
    ) -> int:
        """Publish one immutable generation and select it atomically."""

        _require_repository_capability("publish")
        validated_run_id = _validate_run_id(run_id)
        _validate_manifest_identity(
            canonical_manifest_path, canonical_manifest_sha256
        )
        if not artifacts or not set(artifacts).issubset(ARTIFACT_TYPES):
            raise RepositoryPublicationError("repository artifact types are invalid")
        for content in artifacts.values():
            _reject_noncanonical_numbers(content)

        with _RepositoryRoot(self._repo_dir, create=True) as root:
            with root.open_run(validated_run_id, create=True, exclusive=True) as run:
                return _publish_locked(
                    run,
                    artifacts,
                    canonical_manifest_path=canonical_manifest_path,
                    canonical_manifest_sha256=canonical_manifest_sha256,
                )

    def search(
        self,
        query: str,
        artifact_type: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        _require_repository_capability("search")
        if artifact_type is not None and artifact_type not in ARTIFACT_TYPES:
            raise RepositoryPublicationError("unknown repository artifact type")
        if type(max_results) is not int or max_results < 1:
            raise RepositoryPublicationError("max_results must be positive")
        try:
            root = _RepositoryRoot(self._repo_dir, create=False)
        except FileNotFoundError:
            return []

        results: list[dict[str, Any]] = []
        query_lower = query.casefold()
        with root:
            for run_id in root.run_ids():
                with root.open_run(run_id, create=False, exclusive=False) as run:
                    publication = _load_run_publication_fd(run)
                for item in publication["artifacts"]:
                    if artifact_type is not None and item["type"] != artifact_type:
                        continue
                    rendered = json.dumps(
                        item["content"], ensure_ascii=False, sort_keys=True
                    ).casefold()
                    if query_lower in rendered:
                        results.append(item)
                        if len(results) >= max_results:
                            root.assert_canonical()
                            return results
            root.assert_canonical()
        return results

    def list_runs(self) -> list[str]:
        _require_repository_capability("list_runs")
        try:
            root = _RepositoryRoot(self._repo_dir, create=False)
        except FileNotFoundError:
            return []
        with root:
            result = root.run_ids()
            for run_id in result:
                with root.open_run(run_id, create=False, exclusive=False) as run:
                    _load_run_publication_fd(run)
            root.assert_canonical()
            return result

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        _require_repository_capability("get_run_artifacts")
        validated = _validate_run_id(run_id)
        try:
            root = _RepositoryRoot(self._repo_dir, create=False)
        except FileNotFoundError:
            return {}
        with root:
            try:
                run = root.open_run(validated, create=False, exclusive=False)
            except FileNotFoundError:
                return {}
            with run:
                publication = _load_run_publication_fd(run)
            root.assert_canonical()
        return {item["type"]: item["content"] for item in publication["artifacts"]}

    def import_literature(self, source_run_id: str) -> list[dict[str, Any]]:
        _require_repository_capability("import_literature")
        content = self.get_run_artifacts(source_run_id).get("literature_summary")
        if content is None:
            return []
        if isinstance(content, list):
            return content
        return [content]

    def import_code_template(
        self, source_run_id: str, pattern: str
    ) -> str | None:
        _require_repository_capability("import_code_template")
        content = self.get_run_artifacts(source_run_id).get("code_template")
        if content is None:
            return None
        content_str = str(content)
        return content_str if pattern.casefold() in content_str.casefold() else None


class _RepositoryRoot:
    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = Path(os.path.abspath(path))
        self.fd = _open_directory_path(self.path, create=create)
        self._identity = _directory_identity(self.fd)
        try:
            self.assert_canonical()
        except Exception:
            os.close(self.fd)
            raise

    def __enter__(self) -> _RepositoryRoot:
        return self

    def __exit__(self, *_args: object) -> None:
        os.close(self.fd)

    def assert_canonical(self) -> None:
        _assert_path_identity(self.path, self._identity, "repository root")

    def run_ids(self) -> list[str]:
        names = sorted(os.listdir(self.fd))
        result: list[str] = []
        for name in names:
            if name.startswith("."):
                raise RepositoryPublicationError("repository root namespace mismatch")
            result.append(_validate_run_id(name))
        return result

    def open_run(
        self, run_id: str, *, create: bool, exclusive: bool
    ) -> _RepositoryRun:
        return _RepositoryRun(self, run_id, create=create, exclusive=exclusive)


class _RepositoryRun:
    def __init__(
        self,
        root: _RepositoryRoot,
        run_id: str,
        *,
        create: bool,
        exclusive: bool,
    ) -> None:
        self.root = root
        self.run_id = _validate_run_id(run_id)
        self.fd = _open_child_directory(root.fd, self.run_id, create=create)
        self._identity = _directory_identity(self.fd)
        self._closed = False
        self.generations_fd: int | None = None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            self.generations_fd = _open_child_directory(
                self.fd, "generations", create=create
            )
            self._generations_identity = _directory_identity(self.generations_fd)
            self.assert_canonical()
        except Exception:
            if self.generations_fd is not None:
                os.close(self.generations_fd)
            os.close(self.fd)
            raise

    def __enter__(self) -> _RepositoryRun:
        return self

    def __exit__(self, *_args: object) -> None:
        if not self._closed:
            if self.generations_fd is None:
                raise RepositoryPublicationError(
                    "repository generations namespace is unavailable"
                )
            os.close(self.generations_fd)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self._closed = True

    def assert_canonical(self) -> None:
        if self.generations_fd is None:
            raise RepositoryPublicationError(
                "repository generations namespace is unavailable"
            )
        self.root.assert_canonical()
        _assert_child_identity(
            self.root.fd, self.run_id, self._identity, "repository run"
        )
        _assert_child_identity(
            self.fd,
            "generations",
            self._generations_identity,
            "repository generations",
        )


def _publish_locked(
    run: _RepositoryRun,
    artifacts: Mapping[str, Any],
    *,
    canonical_manifest_path: str,
    canonical_manifest_sha256: str,
) -> int:
    _invalidate_repository_pointer_fd(run.fd)
    _cleanup_repository_staging_fd(run.fd)
    staging_name = f".publication-staging-{uuid.uuid4().hex}"
    os.mkdir(staging_name, mode=0o700, dir_fd=run.fd)
    staging_fd = _open_child_directory(run.fd, staging_name, create=False)
    created_generation: str | None = None
    selected_generation_identity: tuple[int, int] | None = None
    try:
        refs: list[dict[str, str]] = []
        for artifact_type in sorted(artifacts):
            filename = f"{artifact_type}.json"
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "run_id": run.run_id,
                "artifact_type": artifact_type,
                "canonical_manifest_path": canonical_manifest_path,
                "canonical_manifest_sha256": canonical_manifest_sha256,
                "content": artifacts[artifact_type],
            }
            content = _canonical_json_bytes(payload)
            _write_new_regular_at(staging_fd, filename, content)
            refs.append(
                {
                    "artifact_type": artifact_type,
                    "path": filename,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": run.run_id,
            "canonical_manifest_path": canonical_manifest_path,
            "canonical_manifest_sha256": canonical_manifest_sha256,
            "artifacts": refs,
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        _write_new_regular_at(staging_fd, _MANIFEST_NAME, manifest_bytes)
        staged = _replay_generation_fd(staging_fd, expected_run_id=run.run_id)
        if staged["manifest_bytes"] != manifest_bytes:
            raise RepositoryPublicationError("staged repository manifest changed")

        generation_hash = hashlib.sha256(manifest_bytes).hexdigest()
        generation_name = f"gen-{generation_hash}"
        if _entry_exists(run.generations_fd, generation_name):
            existing_fd = _open_child_directory(
                run.generations_fd, generation_name, create=False
            )
            try:
                selected_generation_identity = _directory_identity(existing_fd)
                existing = _replay_generation_fd(
                    existing_fd, expected_run_id=run.run_id
                )
            finally:
                os.close(existing_fd)
            if existing["manifest_bytes"] != manifest_bytes:
                raise RepositoryPublicationError("repository generation ID collision")
            os.close(staging_fd)
            staging_fd = -1
            _remove_tree_at(run.fd, staging_name)
            staging_name = ""
        else:
            os.rename(
                staging_name,
                generation_name,
                src_dir_fd=run.fd,
                dst_dir_fd=run.generations_fd,
            )
            created_generation = generation_name
            staging_name = ""
            selected_generation_identity = _directory_identity(staging_fd)
            published = _replay_generation_fd(
                staging_fd, expected_run_id=run.run_id
            )
            if published["manifest_bytes"] != manifest_bytes:
                raise RepositoryPublicationError("published repository manifest changed")

        pointer = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": run.run_id,
            "generation": f"generations/{generation_name}",
            "manifest_sha256": generation_hash,
        }
        _write_pointer_atomic_fd(run.fd, pointer)
        selected = _load_run_publication_fd(run)
        if selected["pointer"] != pointer:
            raise RepositoryPublicationError("repository pointer replay mismatch")
        if selected_generation_identity is None:
            raise RepositoryPublicationError("repository generation identity is missing")
        _assert_child_identity(
            run.generations_fd,
            generation_name,
            selected_generation_identity,
            "repository selected generation",
        )
        run.assert_canonical()
        return len(refs)
    except Exception:
        _invalidate_repository_pointer_fd(run.fd)
        if created_generation is not None:
            _remove_tree_at(run.generations_fd, created_generation)
        if staging_name:
            _remove_tree_at(run.fd, staging_name)
        raise
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)


def _load_run_publication_fd(run: _RepositoryRun) -> dict[str, Any]:
    _require_repository_capability("_load_run_publication_fd")
    if set(os.listdir(run.fd)) != {"generations", _POINTER_NAME}:
        raise RepositoryPublicationError("repository run namespace mismatch")
    pointer_bytes = _read_regular_at(run.fd, _POINTER_NAME)
    pointer = _parse_exact_json(pointer_bytes, "repository pointer")
    if not isinstance(pointer, dict) or set(pointer) != {
        "schema_version",
        "run_id",
        "generation",
        "manifest_sha256",
    }:
        raise RepositoryPublicationError("repository pointer fields mismatch")
    if not _schema_is_v1(pointer["schema_version"]) or pointer["run_id"] != run.run_id:
        raise RepositoryPublicationError("repository pointer identity mismatch")
    generation = pointer["generation"]
    if not isinstance(generation, str) or not generation.startswith("generations/"):
        raise RepositoryPublicationError("repository generation path is invalid")
    generation_name = generation.removeprefix("generations/")
    match = _GENERATION_RE.fullmatch(generation_name)
    if match is None or pointer["manifest_sha256"] != match.group(1):
        raise RepositoryPublicationError("repository generation hash mismatch")
    generation_fd = _open_child_directory(
        run.generations_fd, generation_name, create=False
    )
    try:
        generation_identity = _directory_identity(generation_fd)
        replayed = _replay_generation_fd(
            generation_fd, expected_run_id=run.run_id
        )
    finally:
        os.close(generation_fd)
    if hashlib.sha256(replayed["manifest_bytes"]).hexdigest() != match.group(1):
        raise RepositoryPublicationError("repository manifest hash mismatch")
    if pointer_bytes != _canonical_json_bytes(pointer):
        raise RepositoryPublicationError("repository pointer is noncanonical")
    _assert_child_identity(
        run.generations_fd,
        generation_name,
        generation_identity,
        "repository selected generation",
    )
    run.assert_canonical()
    if _read_regular_at(run.fd, _POINTER_NAME) != pointer_bytes:
        raise RepositoryPublicationError("repository pointer changed during replay")
    return {"pointer": pointer, "artifacts": replayed["artifacts"]}


def _replay_generation_fd(
    generation_fd: int, *, expected_run_id: str
) -> dict[str, Any]:
    _require_repository_capability("_replay_generation_fd")
    entries = tuple(sorted(os.listdir(generation_fd)))
    manifest_bytes = _read_regular_at(generation_fd, _MANIFEST_NAME)
    manifest = _parse_exact_json(manifest_bytes, "repository manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "run_id",
        "canonical_manifest_path",
        "canonical_manifest_sha256",
        "artifacts",
    }:
        raise RepositoryPublicationError("repository manifest fields mismatch")
    if (
        not _schema_is_v1(manifest["schema_version"])
        or manifest["run_id"] != expected_run_id
    ):
        raise RepositoryPublicationError("repository manifest identity mismatch")
    _validate_manifest_identity(
        manifest["canonical_manifest_path"],
        manifest["canonical_manifest_sha256"],
    )
    refs = manifest["artifacts"]
    if not isinstance(refs, list) or not refs:
        raise RepositoryPublicationError("repository artifact closure is empty")
    expected_files = {_MANIFEST_NAME}
    parsed_artifacts: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    ordered_types: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {
            "artifact_type",
            "path",
            "sha256",
        }:
            raise RepositoryPublicationError("repository artifact ref is invalid")
        artifact_type = ref["artifact_type"]
        path = ref["path"]
        if (
            artifact_type not in ARTIFACT_TYPES
            or artifact_type in seen_types
            or path != f"{artifact_type}.json"
            or not isinstance(ref["sha256"], str)
            or _SHA_RE.fullmatch(ref["sha256"]) is None
        ):
            raise RepositoryPublicationError("repository artifact ref mismatch")
        seen_types.add(artifact_type)
        ordered_types.append(artifact_type)
        expected_files.add(path)
        content_bytes = _read_regular_at(generation_fd, path)
        if hashlib.sha256(content_bytes).hexdigest() != ref["sha256"]:
            raise RepositoryPublicationError("repository artifact hash mismatch")
        payload = _parse_exact_json(content_bytes, "repository artifact")
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "run_id",
            "artifact_type",
            "canonical_manifest_path",
            "canonical_manifest_sha256",
            "content",
        }:
            raise RepositoryPublicationError("repository artifact fields mismatch")
        if (
            not _schema_is_v1(payload["schema_version"])
            or payload["run_id"] != expected_run_id
            or payload["artifact_type"] != artifact_type
            or payload["canonical_manifest_path"]
            != manifest["canonical_manifest_path"]
            or payload["canonical_manifest_sha256"]
            != manifest["canonical_manifest_sha256"]
        ):
            raise RepositoryPublicationError("repository artifact binding mismatch")
        parsed_artifacts.append(
            {
                "run_id": expected_run_id,
                "type": artifact_type,
                "content": payload["content"],
                "canonical_manifest_path": manifest["canonical_manifest_path"],
                "canonical_manifest_sha256": manifest[
                    "canonical_manifest_sha256"
                ],
            }
        )
    if ordered_types != sorted(ordered_types):
        raise RepositoryPublicationError("repository artifact refs are not sorted")
    if set(entries) != expected_files:
        raise RepositoryPublicationError("repository generation file closure mismatch")
    if manifest_bytes != _canonical_json_bytes(manifest):
        raise RepositoryPublicationError("repository manifest is noncanonical")
    if tuple(sorted(os.listdir(generation_fd))) != entries:
        raise RepositoryPublicationError("repository generation changed during replay")
    return {"manifest_bytes": manifest_bytes, "artifacts": parsed_artifacts}


def _write_pointer_atomic_fd(run_fd: int, pointer: Mapping[str, Any]) -> None:
    temporary = f".{_POINTER_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        _write_new_regular_at(run_fd, temporary, _canonical_json_bytes(pointer))
        os.rename(temporary, _POINTER_NAME, src_dir_fd=run_fd, dst_dir_fd=run_fd)
        os.fsync(run_fd)
    finally:
        _unlink_if_present(run_fd, temporary)


def _open_directory_path(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    fd = os.open(absolute.anchor or ".", _DIRECTORY_FLAGS)
    try:
        start = 1 if absolute.anchor else 0
        for part in parts[start:]:
            if part in {"", ".", ".."}:
                raise RepositoryPublicationError("repository path is invalid")
            next_fd = _open_child_directory(fd, part, create=create)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise RepositoryPublicationError(
            f"repository directory is unsafe: {name}"
        ) from exc


def _write_new_regular_at(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written < 1:
                raise OSError("repository write made no progress")
            view = view[written:]
        os.fsync(fd)
        written_stat = os.fstat(fd)
        if not stat.S_ISREG(written_stat.st_mode) or written_stat.st_nlink != 1:
            raise RepositoryPublicationError("repository output is unsafe")
    finally:
        os.close(fd)


def _read_regular_at(directory_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        raise RepositoryPublicationError(
            f"repository artifact read failed: {name}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RepositoryPublicationError(
                f"repository artifact is unsafe: {name}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after) or before.st_size != after.st_size:
            raise RepositoryPublicationError(
                f"repository artifact changed during read: {name}"
            )
        return b"".join(chunks)
    finally:
        os.close(fd)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(child_fd):
            _remove_tree_at(child_fd, child)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _invalidate_repository_pointer_fd(run_fd: int) -> None:
    try:
        entry = os.stat(_POINTER_NAME, dir_fd=run_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(entry.st_mode):
        raise RepositoryPublicationError("repository pointer is unsafe")
    os.unlink(_POINTER_NAME, dir_fd=run_fd)


def _cleanup_repository_staging_fd(run_fd: int) -> None:
    for name in os.listdir(run_fd):
        if _STAGING_RE.fullmatch(name):
            _remove_tree_at(run_fd, name)


def _unlink_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _directory_identity(fd: int) -> tuple[int, int]:
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode):
        raise RepositoryPublicationError("repository namespace is not a directory")
    return (current.st_dev, current.st_ino)


def _assert_path_identity(
    path: Path, expected: tuple[int, int], label: str
) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RepositoryPublicationError(f"{label} identity changed") from exc
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != expected:
        raise RepositoryPublicationError(f"{label} identity changed")


def _assert_child_identity(
    parent_fd: int,
    name: str,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise RepositoryPublicationError(f"{label} identity changed") from exc
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != expected:
        raise RepositoryPublicationError(f"{label} identity changed")


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)


def _schema_is_v1(value: Any) -> bool:
    return type(value) is int and value == _SCHEMA_VERSION


def _parse_exact_json(content: bytes, label: str) -> Any:
    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_raise_json_constant(token)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryPublicationError(f"{label} is invalid JSON") from exc
    _reject_noncanonical_numbers(value)
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    _reject_noncanonical_numbers(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryPublicationError("repository value is not canonical JSON") from exc
    return (text + "\n").encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepositoryPublicationError(f"duplicate repository JSON key: {key}")
        result[key] = value
    return result


def _raise_json_constant(token: str) -> Any:
    raise RepositoryPublicationError(f"invalid repository JSON constant: {token}")


def _reject_noncanonical_numbers(value: Any) -> None:
    if isinstance(value, float):
        raise RepositoryPublicationError("repository JSON must not contain float")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_noncanonical_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_noncanonical_numbers(item)


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise RepositoryPublicationError("repository run_id is invalid")
    if run_id in {".", ".."}:
        raise RepositoryPublicationError("repository run_id is invalid")
    return run_id


def _validate_manifest_identity(path: Any, sha256: Any) -> None:
    if path != "canonical_experiment_evidence.json":
        raise RepositoryPublicationError("canonical manifest path is invalid")
    if not isinstance(sha256, str) or _SHA_RE.fullmatch(sha256) is None:
        raise RepositoryPublicationError("canonical manifest hash is invalid")


def _require_repository_capability(operation: str) -> None:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities(f"ResearchRepository.{operation}")
