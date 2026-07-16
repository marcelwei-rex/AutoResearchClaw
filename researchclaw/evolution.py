"""Self-evolution system for the ResearchClaw pipeline.

Publishes deterministic, canonical-release-bound diagnostic lessons. Policy v1
does not inject persistent lessons into production prompts.

Architecture
------------
* ``LessonCategory`` — 6 issue categories for classification.
* ``LessonEntry`` — single lesson (stage, category, severity, description, ts).
* ``EvolutionStore`` — manifest-bound current-generation lesson publication.
* ``extract_lessons()`` — deterministic derivation from release projection.
* ``build_overlay()`` — deny-only compatibility entrypoint.

Usage
-----
::

    from researchclaw.evolution import EvolutionStore, extract_lessons

    store = EvolutionStore(Path("evolution"))
    lessons = extract_lessons(results)
    store.append_many(lessons)
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

def _load_project_skills() -> list[str]:
    """Reject the legacy project-skill reader retained for API compatibility."""
    raise PermissionError("project skills are not a canonical prompt source")


class LessonCategory(str, Enum):
    """Issue classification for extracted lessons."""

    SYSTEM = "system"          # Environment / network / timeout
    EXPERIMENT = "experiment"  # Code validation, sandbox timeout
    WRITING = "writing"        # Paper quality issues
    ANALYSIS = "analysis"      # Weak analysis, missing comparison
    LITERATURE = "literature"  # Search / verification failures
    PIPELINE = "pipeline"      # Stage orchestration issues


@dataclass(frozen=True)
class LessonEntry:
    """A single lesson extracted from a pipeline run."""

    stage_name: str
    stage_num: int
    category: str
    severity: str  # "info", "warning", "error"
    description: str
    timestamp: str  # ISO 8601
    run_id: str = ""
    schema_version: int = 1
    lesson_kind: str = ""
    canonical_manifest_path: str = ""
    canonical_manifest_sha256: str = ""
    source_path: str = ""
    source_sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> LessonEntry:
        expected = {
            "stage_name", "stage_num", "category", "severity", "description",
            "timestamp", "run_id", "schema_version", "lesson_kind",
            "canonical_manifest_path", "canonical_manifest_sha256", "source_path",
            "source_sha256",
        }
        if set(data) != expected:
            raise ValueError("lesson schema mismatch")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("lesson schema version mismatch")
        if type(data.get("stage_num")) is not int:
            raise ValueError("lesson stage_num must be an integer")
        for field in expected - {"schema_version", "stage_num"}:
            if not isinstance(data.get(field), str):
                raise ValueError(f"lesson {field} must be a string")
        if (
            data["stage_name"] != "citation_verify"
            or data["stage_num"] != 23
            or data["category"] != LessonCategory.LITERATURE
            or data["severity"] != "warning"
            or data["description"] != _CANONICAL_CITATION_LESSON
            or data["timestamp"] != ""
            or data["run_id"] != ""
            or data["lesson_kind"] != "citation_verification_warning"
            or data["source_path"] != "stage-23/verification_report.json"
            or not data["canonical_manifest_path"]
            or not _SHA256_RE.fullmatch(data["canonical_manifest_sha256"])
            or not _SHA256_RE.fullmatch(data["source_sha256"])
        ):
            raise ValueError("lesson authority fields are invalid")
        return cls(
            stage_name=data["stage_name"],
            stage_num=data["stage_num"],
            category=data["category"],
            severity=data["severity"],
            description=data["description"],
            timestamp=data["timestamp"],
            run_id=data["run_id"],
            schema_version=data["schema_version"],
            lesson_kind=data["lesson_kind"],
            canonical_manifest_path=data["canonical_manifest_path"],
            canonical_manifest_sha256=data["canonical_manifest_sha256"],
            source_path=data["source_path"],
            source_sha256=data["source_sha256"],
        )


class CanonicalLessonError(ValueError):
    """Raised when persistent lesson provenance cannot be replayed."""


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_CITATION_LESSON = (
    "Canonical citation verification found unsupported or suspicious citations."
)


def _derive_canonical_lessons(
    projection: object,
    *,
    run_id: str,
    timestamp: str,
) -> list[LessonEntry]:
    """Derive the complete v1 lesson set from one immutable release projection."""
    del run_id, timestamp
    manifest_path = getattr(projection, "canonical_manifest_path", "")
    manifest_sha256 = getattr(projection, "canonical_manifest_sha256", "")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise CanonicalLessonError("canonical lesson manifest path is invalid")
    if not isinstance(manifest_sha256, str) or not _SHA256_RE.fullmatch(manifest_sha256):
        raise CanonicalLessonError("canonical lesson manifest hash is invalid")

    verification = getattr(projection, "verification_report", None)
    if not isinstance(verification, dict) and not hasattr(verification, "get"):
        raise CanonicalLessonError("canonical verification report is invalid")
    summary = verification.get("summary")
    if not isinstance(summary, dict) and not hasattr(summary, "get"):
        raise CanonicalLessonError("canonical verification summary is invalid")
    suspicious = summary.get("suspicious")
    hallucinated = summary.get("hallucinated")
    if type(suspicious) is not int or type(hallucinated) is not int:
        raise CanonicalLessonError("canonical verification counts are invalid")
    if suspicious < 0 or hallucinated < 0:
        raise CanonicalLessonError("canonical verification counts are invalid")
    if suspicious == 0 and hallucinated == 0:
        return []

    artifacts = getattr(projection, "authority_artifacts", ())
    matches = [
        artifact
        for artifact in artifacts
        if getattr(artifact, "path", "") == "stage-23/verification_report.json"
    ]
    if len(matches) != 1:
        raise CanonicalLessonError("Stage 23 verification authority is not unique")
    source_sha256 = getattr(matches[0], "sha256", "")
    if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
        raise CanonicalLessonError("Stage 23 verification authority hash is invalid")
    return [
        LessonEntry(
            stage_name="citation_verify",
            stage_num=23,
            category=LessonCategory.LITERATURE,
            severity="warning",
            description=_CANONICAL_CITATION_LESSON,
            timestamp="",
            run_id="",
            lesson_kind="citation_verification_warning",
            canonical_manifest_path=manifest_path,
            canonical_manifest_sha256=manifest_sha256,
            source_path="stage-23/verification_report.json",
            source_sha256=source_sha256,
        )
    ]


def validate_canonical_lessons(
    lessons: list[LessonEntry], projection: object
) -> None:
    """Require the complete lesson set to equal deterministic projection output."""
    expected = _derive_canonical_lessons(projection, run_id="", timestamp="")
    if lessons != expected:
        raise CanonicalLessonError("persistent lesson set is not projection-derived")


# ---------------------------------------------------------------------------
# Lesson classification keywords
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    LessonCategory.SYSTEM: [
        "timeout", "connection", "network", "oom", "memory",
        "permission", "ssh", "socket", "dns",
    ],
    LessonCategory.EXPERIMENT: [
        "sandbox", "validation", "import", "syntax", "subprocess",
        "experiment", "code", "execution",
    ],
    LessonCategory.WRITING: [
        "paper", "draft", "outline", "revision", "review",
        "template", "latex",
    ],
    LessonCategory.ANALYSIS: [
        "analysis", "metric", "statistic", "comparison", "baseline",
    ],
    LessonCategory.LITERATURE: [
        "search", "citation", "verify", "hallucin", "arxiv",
        "semantic_scholar", "literature", "collect",
    ],
}


def _classify_error(stage_name: str, error_text: str) -> str:
    """Classify an error into a LessonCategory based on keywords."""
    combined = f"{stage_name} {error_text}".lower()
    best_category = LessonCategory.PIPELINE
    best_score = 0
    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > best_score:
            best_score = score
            best_category = category
    return best_category


# ---------------------------------------------------------------------------
# Lesson extraction from pipeline results
# ---------------------------------------------------------------------------

def extract_lessons(
    results: list[object],
    run_id: str = "",
    run_dir: Path | None = None,
) -> list[LessonEntry]:
    """Derive persistent lessons only from immutable canonical release authority."""
    _require_evolution_capability("extract_lessons")
    del results
    if run_dir is None:
        return []
    from researchclaw.pipeline.external_release_projection import (
        load_external_release_projection,
    )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    projection = load_external_release_projection(run_dir)
    return _derive_canonical_lessons(projection, run_id=run_id, timestamp=now)


# ---------------------------------------------------------------------------
# Time-decay weighting
# ---------------------------------------------------------------------------

HALF_LIFE_DAYS: float = 30.0
MAX_AGE_DAYS: float = 90.0


def _time_weight(timestamp_iso: str) -> float:
    """Compute exponential decay weight for a lesson based on age.

    Uses 30-day half-life: weight = exp(-age_days * ln(2) / 30).
    Returns 0.0 for lessons older than 90 days.
    """
    try:
        ts = datetime.fromisoformat(timestamp_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        age_days = age.total_seconds() / 86400.0
        if age_days > MAX_AGE_DAYS:
            return 0.0
        return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Evolution store
# ---------------------------------------------------------------------------


class EvolutionStore:
    """JSONL-backed store for pipeline lessons."""

    def __init__(self, store_dir: Path) -> None:
        if store_dir.name != "evolution":
            raise ValueError("canonical evolution namespace must be run_dir/evolution")
        self._dir = store_dir
        self._lessons_path = self._dir / "lessons.jsonl"

    @property
    def lessons_path(self) -> Path:
        return self._lessons_path

    def append(self, lesson: LessonEntry) -> None:
        """Append a single lesson to the store."""
        self.append_many([lesson])

    def append_many(self, lessons: list[LessonEntry]) -> None:
        """Publish the exact current-generation lesson set atomically."""
        _require_evolution_capability("append_many")
        from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
        from researchclaw.pipeline.external_release_projection import (
            load_external_release_projection,
        )

        with ReleaseGraphLock.acquire(
            self._dir.parent, "EvolutionStore.append_many", mode="write"
        ) as release_lock:
            projection = load_external_release_projection(self._dir.parent)
            validate_canonical_lessons(lessons, projection)
            publication = _serialize_lessons(lessons)
            manifest = _serialize_lesson_manifest(projection, publication, len(lessons))
            release_lock.ensure_run_directory(self._dir.name)
            with release_lock.open_stage_namespace(self._dir.name) as namespace:
                previous: dict[str, bytes] = {}
                if set(namespace.direct_entries()) == {
                    "lessons.jsonl", "lessons_manifest.json"
                }:
                    previous = {
                        name: namespace.read_bytes(name)
                        for name in ("lessons.jsonl", "lessons_manifest.json")
                    }
                try:
                    namespace.invalidate(("lessons_manifest.json",))
                    namespace.reset_flat_namespace()
                    namespace.write_bytes_atomic("lessons.jsonl", publication)
                    namespace.write_bytes_atomic("lessons_manifest.json", manifest)
                    _read_lesson_publication(namespace, projection)
                    fresh_projection = load_external_release_projection(self._dir.parent)
                    if fresh_projection != projection:
                        raise CanonicalLessonError(
                            "canonical release changed during lesson publication"
                        )
                    _read_lesson_publication(namespace, fresh_projection)
                    namespace.assert_canonical()
                    release_lock.assert_canonical()
                except Exception as exc:
                    try:
                        namespace.invalidate(("lessons_manifest.json",))
                        namespace.reset_flat_namespace()
                        for name in ("lessons.jsonl", "lessons_manifest.json"):
                            if name in previous:
                                namespace.write_bytes_atomic(name, previous[name])
                    except Exception as cleanup_exc:  # noqa: BLE001
                        exc.add_note(f"lesson publication rollback failed: {cleanup_exc}")
                    raise
        logger.info("Published %d canonical lessons", len(lessons))

    def load_all(self) -> list[LessonEntry]:
        """Load all lessons from disk."""
        _require_evolution_capability("load_all")
        from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
        from researchclaw.pipeline.external_release_projection import (
            load_external_release_projection,
        )

        with ReleaseGraphLock.acquire(
            self._dir.parent, "EvolutionStore.load_all", mode="read"
        ) as release_lock:
            projection = load_external_release_projection(self._dir.parent)
            with release_lock.open_stage_namespace(self._dir.name) as namespace:
                lessons = _read_lesson_publication(namespace, projection)
                fresh_projection = load_external_release_projection(self._dir.parent)
                if fresh_projection != projection:
                    raise CanonicalLessonError(
                        "canonical release changed during lesson replay"
                    )
                if _read_lesson_publication(namespace, fresh_projection) != lessons:
                    raise CanonicalLessonError("lesson publication changed during replay")
                namespace.assert_canonical()
            release_lock.assert_canonical()
            return lessons

    def query_for_stage(
        self, stage_name: str, *, max_lessons: int = 5
    ) -> list[LessonEntry]:
        """Return the current canonical lessons relevant to a stage.

        Includes lessons that directly match the stage, plus high-severity
        lessons from related stages.
        """
        _require_evolution_capability("query_for_stage")
        all_lessons = self.load_all()
        return all_lessons[:max_lessons] if stage_name else []

    def build_overlay(
        self,
        stage_name: str,
        *,
        max_lessons: int = 5,
        skills_dir: str = "",
    ) -> str:
        """Return no prompt overlay; v1 persistent lessons are audit-only."""
        _require_evolution_capability("build_overlay")
        del stage_name, max_lessons, skills_dir
        return ""

    def count(self) -> int:
        """Return total number of stored lessons."""
        _require_evolution_capability("count")
        return len(self.load_all())

    def export_to_memory(self, memory_store: object) -> int:
        """Reject the legacy binding-losing memory export."""
        _require_evolution_capability("export_to_memory")
        del memory_store
        raise PermissionError("evolution lessons cannot be exported without bindings")

    def get_lessons_for_stage_with_memory(
        self,
        stage_name: str,
        memory_store: object,
        *,
        max_lessons: int = 5,
    ) -> str:
        """Combine evolution overlay with memory context for a stage.

        *memory_store* must expose a ``recall(query, category, max_results)`` method
        returning objects with a ``.content`` attribute.
        """
        _require_evolution_capability("get_lessons_for_stage_with_memory")
        del stage_name, memory_store, max_lessons
        return ""


def _require_evolution_capability(operation: str) -> None:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities(f"EvolutionStore.{operation}")


def _serialize_lessons(lessons: list[LessonEntry]) -> bytes:
    return b"".join(
        (
            json.dumps(
                lesson.to_dict(), sort_keys=True, ensure_ascii=False,
                allow_nan=False, separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for lesson in lessons
    )


def _serialize_lesson_manifest(
    projection: object, publication: bytes, count: int
) -> bytes:
    payload = {
        "schema_version": 1,
        "canonical_manifest_path": projection.canonical_manifest_path,
        "canonical_manifest_sha256": projection.canonical_manifest_sha256,
        "lessons_path": "evolution/lessons.jsonl",
        "lessons_sha256": hashlib.sha256(publication).hexdigest(),
        "lesson_count": count,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_lesson_publication(namespace: object, projection: object) -> list[LessonEntry]:
    expected_entries = ("lessons.jsonl", "lessons_manifest.json")
    if namespace.direct_entries() != expected_entries:
        raise CanonicalLessonError("lesson namespace closure mismatch")
    raw = namespace.read_bytes("lessons.jsonl")
    manifest_raw = namespace.read_bytes("lessons_manifest.json")
    try:
        manifest = json.loads(
            manifest_raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_lesson_keys,
            parse_constant=_reject_lesson_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CanonicalLessonError("lesson manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "canonical_manifest_path", "canonical_manifest_sha256",
        "lessons_path", "lessons_sha256", "lesson_count",
    }:
        raise CanonicalLessonError("lesson manifest schema mismatch")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise CanonicalLessonError("lesson manifest version mismatch")
    if type(manifest["lesson_count"]) is not int or manifest["lesson_count"] < 0:
        raise CanonicalLessonError("lesson manifest count is invalid")
    expected_manifest = _serialize_lesson_manifest(
        projection, raw, manifest["lesson_count"]
    )
    if manifest_raw != expected_manifest:
        raise CanonicalLessonError("lesson manifest replay mismatch")
    lessons = _parse_canonical_lessons(raw)
    if len(lessons) != manifest["lesson_count"]:
        raise CanonicalLessonError("lesson manifest count mismatch")
    validate_canonical_lessons(lessons, projection)
    return lessons


def _parse_canonical_lessons(raw: bytes) -> list[LessonEntry]:
    if raw and not raw.endswith(b"\n"):
        raise CanonicalLessonError("lesson JSONL is not canonical")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise CanonicalLessonError("persistent lesson store is not UTF-8") from exc
    if any(not line for line in lines):
        raise CanonicalLessonError("lesson JSONL contains a blank line")
    lessons: list[LessonEntry] = []
    for line in lines:
        try:
            data = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_lesson_keys,
                parse_constant=_reject_lesson_constant,
            )
            if not isinstance(data, dict):
                raise ValueError("lesson entry must be an object")
            lessons.append(LessonEntry.from_dict(data))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CanonicalLessonError("persistent lesson store is invalid") from exc
    if _serialize_lessons(lessons) != raw:
        raise CanonicalLessonError("lesson JSONL bytes are not canonical")
    return lessons


def _reject_duplicate_lesson_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate lesson key: {key}")
        result[key] = value
    return result


def _reject_lesson_constant(value: str) -> object:
    raise ValueError(f"non-finite lesson value is forbidden: {value}")


# ---------------------------------------------------------------------------
# Refine trajectory tracking
# ---------------------------------------------------------------------------


@dataclass
class RefinePoint:
    """Per-iteration metric record from one REFINE cycle."""

    run_id: str
    cycle: int
    iteration: int
    metric: float | None
    metric_key: str
    metric_direction: str
    timestamp: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RefinePoint:
        raw_metric = data.get("metric")
        return cls(
            run_id=str(data.get("run_id", "")),
            cycle=int(data.get("cycle", 1)),
            iteration=int(data.get("iteration", 0)),
            metric=float(raw_metric) if raw_metric is not None else None,
            metric_key=str(data.get("metric_key", "primary_metric")),
            metric_direction=str(data.get("metric_direction", "minimize")),
            timestamp=str(data.get("timestamp", "")),
        )


class TrajectoryStore:
    """Disabled legacy store for unbound refinement metrics."""

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._path = self._dir / "trajectory.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def append_many(self, points: list[RefinePoint]) -> None:
        del points
        raise PermissionError("unbound refinement trajectory persistence is disabled")

    def load_for_run(self, run_id: str) -> list[RefinePoint]:
        del run_id
        raise PermissionError("unbound refinement trajectory replay is disabled")


def record_refine_trajectory(
    store_dir: Path,
    run_id: str,
    refinement_log: dict[str, object],
    cycle: int = 1,
) -> list[RefinePoint]:
    """Reject the legacy unbound refinement-log persistence path."""
    del store_dir, run_id, refinement_log, cycle
    raise PermissionError("unbound refinement trajectory persistence is disabled")


def get_trajectory_signal(
    store_dir: Path,
    run_id: str,
    current_cycle: int,
) -> dict[str, object]:
    """Reject the legacy unbound trajectory signal path."""
    del store_dir, run_id, current_cycle
    raise PermissionError("unbound refinement trajectory replay is disabled")
