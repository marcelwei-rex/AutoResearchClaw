# pyright: reportPrivateUsage=false
"""Tests for the evolution (self-learning) system."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")

from researchclaw.evolution import (
    CanonicalLessonError,
    EvolutionStore,
    LessonCategory,
    LessonEntry,
    TrajectoryStore,
    extract_lessons,
    get_trajectory_signal,
    record_refine_trajectory,
    _classify_error,
    _time_weight,
)


# ── LessonEntry tests ──


class TestLessonEntry:
    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        entry = LessonEntry(
            stage_name="citation_verify",
            stage_num=23,
            category=LessonCategory.LITERATURE,
            severity="warning",
            description=(
                "Canonical citation verification found unsupported or suspicious citations."
            ),
            timestamp="",
            lesson_kind="citation_verification_warning",
            canonical_manifest_path="canonical_experiment_evidence.json",
            canonical_manifest_sha256="a" * 64,
            source_path="stage-23/verification_report.json",
            source_sha256="b" * 64,
        )
        data = entry.to_dict()
        restored = LessonEntry.from_dict(data)
        assert restored.stage_name == "citation_verify"
        assert restored.stage_num == 23
        assert restored.category == LessonCategory.LITERATURE
        assert restored.severity == "warning"

    def test_from_dict_rejects_missing_fields(self) -> None:
        with pytest.raises(ValueError, match="schema mismatch"):
            LessonEntry.from_dict({})


# ── Classification tests ──


class TestClassifyError:
    def test_timeout_classified_as_system(self) -> None:
        assert _classify_error("experiment_run", "Connection timeout after 30s") == "system"

    def test_validation_classified_as_experiment(self) -> None:
        assert _classify_error("code_generation", "Syntax error in code") == "experiment"

    def test_citation_classified_as_literature(self) -> None:
        assert _classify_error("citation_verify", "Hallucinated reference") == "literature"

    def test_paper_classified_as_writing(self) -> None:
        assert _classify_error("paper_draft", "Draft quality too low") == "writing"

    def test_unknown_defaults_to_pipeline(self) -> None:
        assert _classify_error("unknown_stage", "something random") == "pipeline"


# ── Time weight tests ──


class TestTimeWeight:
    def test_recent_lesson_has_high_weight(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        assert _time_weight(now) > 0.9

    def test_30_day_old_has_half_weight(self) -> None:
        ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        weight = _time_weight(ts)
        assert 0.4 < weight < 0.6  # Should be ~0.5

    def test_90_day_old_returns_zero(self) -> None:
        ts = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(timespec="seconds")
        assert _time_weight(ts) == 0.0

    def test_invalid_timestamp_returns_zero(self) -> None:
        assert _time_weight("not-a-date") == 0.0

    def test_empty_timestamp_returns_zero(self) -> None:
        assert _time_weight("") == 0.0


# ── Extract lessons tests ──


def _projection(*, manifest_sha256: str = "a" * 64, suspicious: int = 1):
    return SimpleNamespace(
        canonical_manifest_path="canonical_experiment_evidence.json",
        canonical_manifest_sha256=manifest_sha256,
        verification_report=MappingProxyType(
            {
                "summary": MappingProxyType(
                    {"suspicious": suspicious, "hallucinated": 0}
                )
            }
        ),
        authority_artifacts=(
            SimpleNamespace(
                path="stage-23/verification_report.json", sha256="b" * 64
            ),
        ),
    )


class TestExtractLessons:
    def test_unbound_stage_results_do_not_create_lessons(self) -> None:
        poison = SimpleNamespace(
            stage=15, status="failed", error="SHADOW_POISON", decision="pivot"
        )
        assert extract_lessons([poison], run_id="test-run") == []

    def test_derives_only_projection_bound_lesson(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projection = _projection()
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: projection,
        )
        poison = tmp_path / "run/stage-15_v99/decision_structured.json"
        poison.parent.mkdir(parents=True)
        poison.write_text('{"rationale":"SHADOW_POISON"}', encoding="utf-8")
        runtime = tmp_path / "run/stage-99/runs/run-poison.json"
        runtime.parent.mkdir(parents=True)
        runtime.write_text('{"stderr":"SHADOW_RUNTIME_POISON"}', encoding="utf-8")

        lessons = extract_lessons([], run_id="run-1", run_dir=tmp_path / "run")

        assert len(lessons) == 1
        assert lessons[0].canonical_manifest_sha256 == "a" * 64
        assert lessons[0].source_path == "stage-23/verification_report.json"
        assert "SHADOW" not in lessons[0].description

    def test_clean_projection_produces_no_lesson(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: _projection(suspicious=0),
        )
        assert extract_lessons([], run_dir=tmp_path / "run") == []


# ── EvolutionStore tests ──


class TestEvolutionStore:
    @pytest.fixture(autouse=True)
    def _canonical_projection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: _projection(),
        )

    @staticmethod
    def _lesson(tmp_path: Path) -> LessonEntry:
        return extract_lessons([], run_id="run-1", run_dir=tmp_path)[0]

    def test_append_and_load(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].stage_name == "citation_verify"

    @pytest.mark.parametrize("name", ("stage-15", "shadow-lessons", "evo"))
    def test_rejects_noncanonical_namespace_before_access(
        self, tmp_path: Path, name: str
    ) -> None:
        wrong = tmp_path / name
        wrong.mkdir()
        sentinel = wrong / "sentinel"
        sentinel.write_text("KEEP", encoding="utf-8")

        with pytest.raises(ValueError, match="run_dir/evolution"):
            EvolutionStore(wrong)

        assert sentinel.read_text(encoding="utf-8") == "KEEP"

    def test_append_many(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append_many([self._lesson(tmp_path)])
        assert store.count() == 1

    def test_append_many_empty_publishes_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: _projection(suspicious=0),
        )
        store = EvolutionStore(tmp_path / "evolution")
        store.append_many([])
        assert store.count() == 0
        assert set(path.name for path in store.lessons_path.parent.iterdir()) == {
            "lessons.jsonl", "lessons_manifest.json"
        }

    def test_load_all_missing_publication_fails_closed(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        with pytest.raises(FileNotFoundError):
            store.load_all()

    def test_query_for_stage_returns_relevant_lessons(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        result = store.query_for_stage("citation_verify", max_lessons=5)
        assert len(result) == 1

    def test_query_respects_max_lessons(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        assert len(store.query_for_stage("citation_verify", max_lessons=1)) == 1

    def test_build_overlay_returns_empty_for_no_lessons(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        assert store.build_overlay("hypothesis_gen") == ""

    def test_persistent_lessons_are_not_a_prompt_overlay(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        assert store.build_overlay("citation_verify") == ""

    def test_overlay_rejects_stale_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: _projection(manifest_sha256="c" * 64),
        )
        with pytest.raises(CanonicalLessonError, match="manifest replay mismatch"):
            store.query_for_stage("citation_verify")

    def test_overlay_rejects_tampered_and_duplicate_key_lessons(
        self, tmp_path: Path
    ) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        lesson = self._lesson(tmp_path).to_dict()
        lesson["description"] = "SHADOW_POISON"
        store.lessons_path.write_text(json.dumps(lesson) + "\n", encoding="utf-8")
        with pytest.raises(CanonicalLessonError):
            store.query_for_stage("citation_verify")

        raw = json.dumps(self._lesson(tmp_path).to_dict())
        store.lessons_path.write_text(
            raw[:-1] + ',"description":"SHADOW_POISON"}\n', encoding="utf-8"
        )
        with pytest.raises(CanonicalLessonError):
            store.query_for_stage("citation_verify")

    def test_clean_generation_replaces_prior_lesson_with_explicit_empty_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: _projection(manifest_sha256="c" * 64, suspicious=0),
        )
        store.append_many([])
        assert store.load_all() == []
        assert store.lessons_path.read_bytes() == b""

    def test_extra_namespace_entry_is_rejected(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        store.append(self._lesson(tmp_path))
        (store.lessons_path.parent / "shadow.json").write_text("{}", encoding="utf-8")
        with pytest.raises(CanonicalLessonError, match="namespace closure"):
            store.load_all()

    def test_post_write_mutation_rolls_back_new_lesson_authority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchclaw import evolution

        store = EvolutionStore(tmp_path / "evolution")
        original = evolution._read_lesson_publication
        calls = 0

        def mutate_after_first_replay(namespace, projection):
            nonlocal calls
            result = original(namespace, projection)
            calls += 1
            if calls == 1:
                store.lessons_path.write_bytes(b"{}\n")
            return result

        monkeypatch.setattr(evolution, "_read_lesson_publication", mutate_after_first_replay)
        with pytest.raises(CanonicalLessonError):
            store.append(self._lesson(tmp_path))
        assert not store.lessons_path.exists()
        assert not (store.lessons_path.parent / "lessons_manifest.json").exists()

    def test_metaclaw_skill_directory_is_not_an_overlay_source(
        self, tmp_path: Path
    ) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        skills = tmp_path / "skills/arc-poison"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("SHADOW_SKILL_POISON", encoding="utf-8")
        assert "SHADOW_SKILL_POISON" not in store.build_overlay(
            "citation_verify", skills_dir=str(skills.parent)
        )

    def test_append_parent_replacement_has_external_zero_write_and_rolls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        detached = tmp_path / "run-detached"
        external = tmp_path / "external"
        external.mkdir()
        (external / "sentinel").write_text("KEEP", encoding="utf-8")
        lesson = extract_lessons([], run_id="run-1", run_dir=run_dir)[0]

        replaced = False

        def replace_parent(_run_dir: Path):
            nonlocal replaced
            if not replaced:
                run_dir.rename(detached)
                run_dir.symlink_to(external, target_is_directory=True)
                replaced = True
            return _projection()

        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            replace_parent,
        )
        with pytest.raises(OSError, match="directory changed|changed during"):
            EvolutionStore(run_dir / "evolution").append(lesson)

        assert [path.name for path in external.iterdir()] == ["sentinel"]
        assert not (detached / "evolution/lessons.jsonl").exists()

    def test_canonical_lesson_timestamp_cannot_be_reweighted(self, tmp_path: Path) -> None:
        store = EvolutionStore(tmp_path / "evolution")
        lesson = self._lesson(tmp_path)
        altered = replace(
            lesson,
            timestamp=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),
        )
        with pytest.raises(CanonicalLessonError, match="not projection-derived"):
            store.append(altered)

    def test_duplicate_canonical_lesson_cannot_inflate_overlay(
        self, tmp_path: Path
    ) -> None:
        lesson = self._lesson(tmp_path)
        with pytest.raises(CanonicalLessonError, match="not projection-derived"):
            EvolutionStore(tmp_path / "evolution").append_many([lesson, lesson])

    def test_creates_directory_if_not_exists(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "nested" / "evolution"
        EvolutionStore(store_dir)
        assert not store_dir.exists()


class TestTrajectoryStore:
    def test_unbound_trajectory_persistence_and_replay_are_disabled(
        self, tmp_path: Path
    ) -> None:
        refinement_log = {
            "metric_key": "loss",
            "metric_direction": "minimize",
            "iterations": [{"metric": 1.0}, {"metric": 0.9}],
        }
        poison = tmp_path / "refinement_log.json"
        poison.write_text(json.dumps(refinement_log), encoding="utf-8")

        with pytest.raises(PermissionError, match="trajectory persistence"):
            record_refine_trajectory(tmp_path, "run-a", refinement_log, cycle=1)
        with pytest.raises(PermissionError, match="trajectory replay"):
            get_trajectory_signal(tmp_path, "run-a", current_cycle=1)
        with pytest.raises(PermissionError, match="trajectory persistence"):
            TrajectoryStore(tmp_path).append_many([])
        with pytest.raises(PermissionError, match="trajectory replay"):
            TrajectoryStore(tmp_path).load_for_run("run-a")

        assert not (tmp_path / "trajectory.jsonl").exists()


# ── PromptManager evolution overlay integration ──


class TestPromptManagerEvolutionOverlay:
    def test_overlay_appended_to_user_prompt(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        overlay = "## Lessons\n1. Avoid timeout errors."
        sp = pm.for_stage(
            "topic_init",
            evolution_overlay=overlay,
            topic="test",
            domains="ml",
            project_name="p1",
            quality_threshold="8.0",
        )
        assert "Avoid timeout errors" in sp.user

    def test_no_overlay_when_empty(self) -> None:
        from researchclaw.prompts import PromptManager

        pm = PromptManager()
        sp1 = pm.for_stage(
            "topic_init",
            topic="test",
            domains="ml",
            project_name="p1",
            quality_threshold="8.0",
        )
        sp2 = pm.for_stage(
            "topic_init",
            evolution_overlay="",
            topic="test",
            domains="ml",
            project_name="p1",
            quality_threshold="8.0",
        )
        assert sp1.user == sp2.user
