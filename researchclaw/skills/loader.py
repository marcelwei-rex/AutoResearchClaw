"""Skill file loader — supports YAML, JSON, and SKILL.md (agentskills.io)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
import yaml

from researchclaw.skills.schema import Skill

logger = logging.getLogger(__name__)


def _read_regular_utf8(path: Path) -> str:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"skill source is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks).decode("utf-8")


def _read_regular_fd(parent_fd: int, name: str, display_path: Path) -> bytes:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"skill source is not a regular file: {display_path}")
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"skill source is not a regular file: {display_path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError(f"skill source changed while opening: {display_path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise OSError(f"skill source changed while reading: {display_path}")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise OSError(f"skill source changed while reading: {display_path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _collect_skill_sources(directory: Path) -> list[tuple[Path, bytes]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(directory, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OSError(f"skill root is not a regular directory: {directory}") from exc

    root_identity = os.fstat(root_fd)

    def walk(parent_fd: int, relative: Path) -> list[tuple[Path, bytes]]:
        initial_names = tuple(sorted(os.listdir(parent_fd)))
        sources: list[tuple[Path, bytes]] = []
        for name in initial_names:
            display = directory / relative / name
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, flags, dir_fd=parent_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError(f"skill directory changed while opening: {display}")
                    sources.extend(walk(child_fd, relative / name))
                    after = os.fstat(child_fd)
                    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                        raise OSError(f"skill directory changed while reading: {display}")
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(current.st_mode) or (
                        current.st_dev,
                        current.st_ino,
                    ) != (opened.st_dev, opened.st_ino):
                        raise OSError(f"skill directory changed while reading: {display}")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                sources.append(
                    (relative / name, _read_regular_fd(parent_fd, name, display))
                )
            else:
                raise OSError(f"skill namespace contains an unsafe entry: {display}")
        if tuple(sorted(os.listdir(parent_fd))) != initial_names:
            raise OSError(f"skill directory changed while reading: {directory / relative}")
        return sources

    try:
        sources = walk(root_fd, Path())
        try:
            final_root = os.stat(directory, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise OSError("skill root changed while reading") from exc
        if not stat.S_ISDIR(final_root.st_mode) or (
            final_root.st_dev,
            final_root.st_ino,
        ) != (root_identity.st_dev, root_identity.st_ino):
            raise OSError("skill root changed while reading")
        return sources
    finally:
        os.close(root_fd)


# ── SKILL.md loader ──────────────────────────────────────────────────


def load_skill_from_skillmd(path: Path) -> Skill | None:
    """Load a skill from a ``SKILL.md`` file (agentskills.io format).

    Expected layout::

        ---
        name: kebab-case-id
        description: one-liner
        metadata:
          category: domain
          trigger-keywords: "kw1,kw2"
        ---

        Markdown body here ...

    Args:
        path: Path to the SKILL.md file.

    Returns:
        Parsed :class:`Skill`, or *None* on failure.
    """
    try:
        text = _read_regular_utf8(path)
    except Exception as exc:
        logger.warning("Failed to read SKILL.md at %s: %s", path, exc)
        return None

    return _parse_skillmd_text(text, path)


def _parse_skillmd_text(text: str, path: Path) -> Skill | None:
    # Split on YAML frontmatter markers
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.warning("SKILL.md missing frontmatter delimiters: %s", path)
        return None

    try:
        header = yaml.safe_load(parts[1])
    except Exception as exc:
        logger.warning("Invalid YAML frontmatter in %s: %s", path, exc)
        return None

    if not isinstance(header, dict):
        logger.warning("Frontmatter is not a dict in %s", path)
        return None

    # Support `enabled: false` to let users disable a recommended skill
    if header.get("enabled") is False or str(header.get("enabled", "")).lower() == "false":
        logger.debug("Skill disabled via frontmatter: %s", path)
        return None

    name = str(header.get("name", ""))
    if not name:
        logger.warning("SKILL.md missing 'name' field: %s", path)
        return None

    description = str(header.get("description", ""))
    body = parts[2].strip()

    # Build metadata — flatten nested 'metadata' dict from frontmatter
    metadata: dict[str, str] = {}
    raw_meta = header.get("metadata")
    if isinstance(raw_meta, dict):
        for k, v in raw_meta.items():
            metadata[str(k)] = str(v)

    # Also pull top-level keys that map to metadata
    for key in ("category", "license", "compatibility", "version", "author"):
        if key in header and key not in metadata:
            metadata[key] = str(header[key])

    skill_license = str(header.get("license", ""))
    compatibility = str(header.get("compatibility", ""))

    return Skill(
        name=name,
        description=description,
        body=body,
        license=skill_license,
        compatibility=compatibility,
        metadata=metadata,
        source_dir=path.parent,
        source_format="skillmd",
    )


def load_skillmd_from_directory(directory: Path) -> list[Skill]:
    """Scan *directory* for ``*/SKILL.md`` sub-directories.

    Each immediate sub-directory containing a ``SKILL.md`` file is
    treated as a single skill.
    """
    skills: list[Skill] = []
    for relative, raw in _collect_skill_sources(directory):
        if relative.name != "SKILL.md":
            continue
        path = directory / relative
        try:
            skill = _parse_skillmd_text(raw.decode("utf-8"), path)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load SKILL.md at %s: %s", path, exc)
            continue
        if skill:
            skills.append(skill)
    return skills


# ── Legacy YAML / JSON loader ────────────────────────────────────────


def load_skill_file(path: Path) -> Skill | None:
    """Load a single skill from a YAML or JSON file.

    Args:
        path: Path to the skill file.

    Returns:
        Parsed Skill object, or None if loading fails.
    """
    try:
        text = _read_regular_utf8(path)
        return _parse_skill_file_text(text, path)
    except Exception as exc:
        logger.warning("Failed to load skill from %s: %s", path, exc)
        return None


def _parse_skill_file_text(text: str, path: Path) -> Skill | None:
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif path.suffix == ".json":
        data = json.loads(text)
    else:
        logger.warning("Unsupported skill file format: %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Skill file is not a dict: %s", path)
        return None
    skill = Skill.from_dict(data)
    if not skill.name:
        logger.warning("Skill missing name/id: %s", path)
        return None
    return skill


def load_skills_from_directory(directory: Path) -> list[Skill]:
    """Recursively load all skills from a directory.

    Supports both ``SKILL.md`` (agentskills.io) and legacy YAML/JSON.
    When both formats exist for the same skill name, SKILL.md wins.

    Args:
        directory: Root directory to scan.

    Returns:
        List of successfully loaded Skill objects.
    """
    skills_by_name: dict[str, Skill] = {}
    sources = _collect_skill_sources(directory)
    for relative, raw in sources:
        if relative.name != "SKILL.md":
            continue
        try:
            skill = _parse_skillmd_text(raw.decode("utf-8"), directory / relative)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            logger.warning("Failed to load SKILL.md at %s: %s", directory / relative, exc)
            continue
        if skill:
            skills_by_name[skill.name] = skill

    for relative, raw in sources:
        if relative.suffix not in (".yaml", ".yml", ".json"):
            continue
        try:
            skill = _parse_skill_file_text(raw.decode("utf-8"), directory / relative)
        except (UnicodeDecodeError, ValueError, TypeError, yaml.YAMLError) as exc:
            logger.warning("Failed to load skill from %s: %s", directory / relative, exc)
            continue
        if skill and skill.name not in skills_by_name:
            skills_by_name[skill.name] = skill

    skills = list(skills_by_name.values())
    logger.info("Loaded %d skills from %s", len(skills), directory)
    return skills
