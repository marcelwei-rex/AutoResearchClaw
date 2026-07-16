"""Tests for the collaboration system (15+ tests).

Covers:
- ResearchRepository (publish, search, list)
- ArtifactPublisher (extraction from run dirs)
- ArtifactSubscriber (queries)
- Deduplication (content_hash, deduplicate_artifacts)
"""

from __future__ import annotations

import json
import hashlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("canonical_evidence_migration_complete")

from researchclaw.collaboration.repository import (
    RepositoryPublicationError,
    ResearchRepository,
)
from researchclaw.collaboration.publisher import ArtifactPublisher
from researchclaw.collaboration.subscriber import ArtifactSubscriber
from researchclaw.collaboration.dedup import content_hash, deduplicate_artifacts
from researchclaw.collaboration import repository as repository_module


# ── Fixtures ─────────────────────────────────────────────────────────


_MANIFEST_PATH = "canonical_experiment_evidence.json"
_MANIFEST_SHA = "a" * 64


def _publish(
    repo: ResearchRepository, run_id: str, artifacts: dict[str, object]
) -> int:
    return repo.publish(
        run_id,
        artifacts,
        canonical_manifest_path=_MANIFEST_PATH,
        canonical_manifest_sha256=_MANIFEST_SHA,
    )


@pytest.fixture
def repo(tmp_path: Path) -> ResearchRepository:
    return ResearchRepository(repo_dir=tmp_path / "shared_repo")


@pytest.fixture
def populated_repo(repo: ResearchRepository) -> ResearchRepository:
    _publish(
        repo,
        "run-001",
        {
            "literature_summary": {"papers": ["Paper A on transformer", "Paper B on vision"]},
            "experiment_results": {"accuracy": "0.95", "model": "ResNet50"},
        },
    )
    _publish(
        repo,
        "run-002",
        {
            "literature_summary": {"papers": ["Paper C on nlp transformer"]},
            "code_template": "import torch\nmodel = ResNet()\n# pytorch training",
        },
    )
    return repo


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Create a fake pipeline run directory with stage outputs."""
    d = tmp_path / "run-test"
    d.mkdir()

    # Stage 07 — literature synthesis
    s07 = d / "stage-07-literature_synthesis"
    s07.mkdir()
    (s07 / "synthesis.json").write_text(
        json.dumps({"papers": [{"title": "Test Paper", "year": 2024}]}),
        encoding="utf-8",
    )

    # Stage 10 — code generation
    s10 = d / "stage-10-code_generation"
    s10.mkdir()
    (s10 / "main.py").write_text("print('hello')", encoding="utf-8")

    # Stage 14 — result analysis
    s14 = d / "stage-14-result_analysis"
    s14.mkdir()
    (s14 / "experiment_summary.json").write_text(
        json.dumps({"accuracy": 0.92}), encoding="utf-8"
    )

    # Stage 18 — peer review
    s18 = d / "stage-18-peer_review"
    s18.mkdir()
    (s18 / "review.md").write_text("Good paper overall.", encoding="utf-8")

    return d


@pytest.fixture
def canonical_projection(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projection = SimpleNamespace(
        canonical_manifest_path="canonical_experiment_evidence.json",
        canonical_manifest_sha256="a" * 64,
        candidate_id="cand-" + "b" * 64,
        selected_result_manifest_path="stage-12/experiment_result_set.json",
        selected_result_manifest_sha256="c" * 64,
        selected_execution_path="stage-12/evidence-v1/run-1.json",
        selected_execution_sha256="d" * 64,
        metric_observations={"accuracy": ("0.95",)},
        structured_results={"conditions": ()},
        summary={"primary_metric": "accuracy"},
        literature_text="Canonical literature synthesis.",
        review_text="Canonical review.",
        project_files=(("main.py", "print('canonical')\n"),),
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.external_release_projection."
        "load_external_release_projection",
        lambda _run_dir: projection,
    )
    return projection


# ── Repository Tests ─────────────────────────────────────────────────


class TestResearchRepository:
    def test_publish(self, repo: ResearchRepository) -> None:
        count = _publish(
            repo,
            "run-001",
            {"literature_summary": {"papers": ["P1"]}},
        )
        assert count == 1

    def test_publish_creates_dirs(self, repo: ResearchRepository) -> None:
        _publish(repo, "run-new", {"code_template": "print('hi')"})
        assert (repo.repo_dir / "run-new").is_dir()

    def test_publish_unknown_type_rejected(self, repo: ResearchRepository) -> None:
        with pytest.raises(RepositoryPublicationError):
            _publish(repo, "run-bad", {"unknown_type": "data"})

    @pytest.mark.parametrize("run_id", ["../escape", "..", "/absolute", "a/b"])
    def test_publish_rejects_unsafe_run_id(
        self, repo: ResearchRepository, run_id: str
    ) -> None:
        with pytest.raises(RepositoryPublicationError, match="run_id"):
            _publish(repo, run_id, {"code_template": "safe"})
        assert not repo.repo_dir.exists()

    def test_partial_publication_has_no_pointer_or_generation(
        self, repo: ResearchRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = repository_module._write_new_regular_at
        calls = 0

        def fail_second(directory_fd: int, name: str, content: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            original(directory_fd, name, content)

        monkeypatch.setattr(repository_module, "_write_new_regular_at", fail_second)
        with pytest.raises(OSError, match="injected"):
            _publish(
                repo,
                "run-partial",
                {"code_template": "safe", "literature_summary": "bounded"},
            )
        run_root = repo.repo_dir / "run-partial"
        assert not (run_root / "publication.json").exists()
        assert list((run_root / "generations").iterdir()) == []
        assert not any(path.name.startswith(".publication-staging-") for path in run_root.iterdir())

    def test_new_generation_does_not_expose_stale_artifacts(
        self, repo: ResearchRepository
    ) -> None:
        _publish(
            repo,
            "run-republish",
            {"code_template": "old", "review_feedback": "stale"},
        )
        repo.publish(
            "run-republish",
            {"code_template": "new"},
            canonical_manifest_path=_MANIFEST_PATH,
            canonical_manifest_sha256="b" * 64,
        )
        assert repo.get_run_artifacts("run-republish") == {
            "code_template": "new"
        }
        generations = repo.repo_dir / "run-republish" / "generations"
        assert len(list(generations.iterdir())) == 2

    def test_failed_republish_invalidates_previous_pointer(
        self, repo: ResearchRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _publish(repo, "run-republish-failure", {"code_template": "old"})
        run_root = repo.repo_dir / "run-republish-failure"
        old_generations = tuple((run_root / "generations").iterdir())

        def fail_write(_directory_fd: int, _name: str, _content: bytes) -> None:
            raise OSError("injected republish failure")

        monkeypatch.setattr(repository_module, "_write_new_regular_at", fail_write)
        with pytest.raises(OSError, match="injected republish failure"):
            repo.publish(
                "run-republish-failure",
                {"code_template": "new"},
                canonical_manifest_path=_MANIFEST_PATH,
                canonical_manifest_sha256="b" * 64,
            )

        assert not (run_root / "publication.json").exists()
        assert tuple((run_root / "generations").iterdir()) == old_generations
        with pytest.raises(RepositoryPublicationError, match="namespace mismatch"):
            repo.get_run_artifacts("run-republish-failure")

    def test_reader_rejects_extra_generation_file(
        self, repo: ResearchRepository
    ) -> None:
        _publish(repo, "run-tamper", {"code_template": "safe"})
        run_root = repo.repo_dir / "run-tamper"
        pointer = json.loads((run_root / "publication.json").read_text())
        generation = run_root / pointer["generation"]
        (generation / "shadow.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(RepositoryPublicationError, match="file closure"):
            repo.get_run_artifacts("run-tamper")

    @pytest.mark.parametrize("replacement", ["run", "repository"])
    def test_publish_parent_replacement_has_external_zero_write_and_delete(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        replacement: str,
    ) -> None:
        repo = ResearchRepository(tmp_path / "shared")
        external = tmp_path / "external"
        external.mkdir()
        (external / "sentinel").write_text("KEEP", encoding="utf-8")
        detached = tmp_path / f"detached-{replacement}"
        original = repository_module._write_pointer_atomic_fd

        def replace_after_pointer(
            run_fd: int, pointer: dict[str, object]
        ) -> None:
            original(run_fd, pointer)
            run_root = repo.repo_dir / "run-parent"
            if replacement == "run":
                run_root.rename(detached)
                run_root.symlink_to(external, target_is_directory=True)
            else:
                repo.repo_dir.rename(detached)
                repo.repo_dir.symlink_to(external, target_is_directory=True)

        monkeypatch.setattr(
            repository_module, "_write_pointer_atomic_fd", replace_after_pointer
        )
        with pytest.raises(RepositoryPublicationError, match="identity changed"):
            _publish(repo, "run-parent", {"code_template": "safe"})

        assert [path.name for path in external.iterdir()] == ["sentinel"]
        detached_run = detached if replacement == "run" else detached / "run-parent"
        assert not (detached_run / "publication.json").exists()
        assert list((detached_run / "generations").iterdir()) == []

    def test_reader_rejects_run_replacement_before_pointer_read(
        self,
        repo: ResearchRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _publish(repo, "run-reader", {"code_template": "SAFE"})
        run_root = repo.repo_dir / "run-reader"
        detached = tmp_path / "run-reader-detached"
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        original = repository_module._read_regular_at
        replaced = False

        def replace_before_read(directory_fd: int, name: str) -> bytes:
            nonlocal replaced
            if name == "publication.json" and not replaced:
                replaced = True
                run_root.rename(detached)
                run_root.symlink_to(attacker, target_is_directory=True)
            return original(directory_fd, name)

        monkeypatch.setattr(repository_module, "_read_regular_at", replace_before_read)
        with pytest.raises(RepositoryPublicationError, match="identity changed"):
            repo.get_run_artifacts("run-reader")
        assert list(attacker.iterdir()) == []
        assert (detached / "publication.json").is_file()

    def test_reader_rejects_selected_generation_replacement_during_replay(
        self,
        repo: ResearchRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _publish(repo, "run-generation", {"code_template": "SAFE"})
        run_root = repo.repo_dir / "run-generation"
        pointer = json.loads((run_root / "publication.json").read_text())
        generation = run_root / pointer["generation"]
        detached = tmp_path / "generation-detached"
        original = repository_module._replay_generation_fd
        replaced = False

        def replace_after_replay(
            generation_fd: int, *, expected_run_id: str
        ) -> dict[str, object]:
            nonlocal replaced
            result = original(generation_fd, expected_run_id=expected_run_id)
            if not replaced:
                replaced = True
                generation.rename(detached)
                generation.mkdir()
            return result

        monkeypatch.setattr(
            repository_module, "_replay_generation_fd", replace_after_replay
        )
        with pytest.raises(
            RepositoryPublicationError, match="selected generation identity changed"
        ):
            repo.get_run_artifacts("run-generation")
        assert list(generation.iterdir()) == []
        assert (detached / "bundle_manifest.json").is_file()

    def test_same_run_publications_are_serialized(
        self,
        repo: ResearchRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        original = repository_module._write_new_regular_at

        def pause_first(directory_fd: int, name: str, content: bytes) -> None:
            if b'"content":"old"' in content and not entered.is_set():
                entered.set()
                assert release.wait(timeout=5)
            original(directory_fd, name, content)

        monkeypatch.setattr(repository_module, "_write_new_regular_at", pause_first)
        errors: list[BaseException] = []

        def publish(content: str, manifest_hash: str) -> None:
            try:
                repo.publish(
                    "run-concurrent",
                    {"code_template": content},
                    canonical_manifest_path=_MANIFEST_PATH,
                    canonical_manifest_sha256=manifest_hash,
                )
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        first = threading.Thread(target=publish, args=("old", "a" * 64))
        second = threading.Thread(target=publish, args=("new", "b" * 64))
        first.start()
        assert entered.wait(timeout=5)
        second.start()
        assert second.is_alive()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert errors == []
        assert repo.get_run_artifacts("run-concurrent") == {"code_template": "new"}
        assert (repo.repo_dir / "run-concurrent" / "publication.json").is_file()

    def test_failed_run_namespace_construction_closes_generations_fd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = ResearchRepository(tmp_path / "shared")
        captured_fd: int | None = None
        original_open = repository_module._open_child_directory
        original_assert = repository_module._assert_child_identity

        def capture_open(parent_fd: int, name: str, *, create: bool) -> int:
            nonlocal captured_fd
            fd = original_open(parent_fd, name, create=create)
            if name == "generations":
                captured_fd = fd
            return fd

        def fail_generations_identity(
            parent_fd: int,
            name: str,
            expected: tuple[int, int],
            label: str,
        ) -> None:
            if label == "repository generations":
                raise RepositoryPublicationError("injected generations failure")
            original_assert(parent_fd, name, expected, label)

        monkeypatch.setattr(
            repository_module, "_open_child_directory", capture_open
        )
        monkeypatch.setattr(
            repository_module, "_assert_child_identity", fail_generations_identity
        )
        with pytest.raises(RepositoryPublicationError, match="injected generations"):
            _publish(repo, "run-fd-close", {"code_template": "safe"})

        assert captured_fd is not None
        with pytest.raises(OSError):
            os.fstat(captured_fd)

    @pytest.mark.parametrize("target", ["pointer", "manifest", "artifact"])
    @pytest.mark.parametrize("invalid", [True, 1.0, "1", None])
    def test_replay_rejects_non_integer_schema_versions(
        self,
        repo: ResearchRepository,
        target: str,
        invalid: object,
    ) -> None:
        _publish(repo, "run-schema", {"code_template": "safe"})
        _rewrite_repository_generation(repo, "run-schema", target, invalid)
        with pytest.raises(RepositoryPublicationError):
            repo.get_run_artifacts("run-schema")

    def test_replay_rejects_reordered_artifact_refs(
        self, repo: ResearchRepository
    ) -> None:
        _publish(
            repo,
            "run-order",
            {"code_template": "safe", "literature_summary": "bounded"},
        )
        run_root = repo.repo_dir / "run-order"
        pointer = json.loads((run_root / "publication.json").read_text())
        generation = run_root / pointer["generation"]
        manifest = json.loads((generation / "bundle_manifest.json").read_text())
        manifest["artifacts"].reverse()
        _replace_generation_manifest(run_root, generation, pointer, manifest)
        with pytest.raises(RepositoryPublicationError, match="not sorted"):
            repo.get_run_artifacts("run-order")

    def test_search_by_query(self, populated_repo: ResearchRepository) -> None:
        results = populated_repo.search("transformer")
        assert len(results) >= 2

    def test_search_by_type(self, populated_repo: ResearchRepository) -> None:
        results = populated_repo.search(
            "paper", artifact_type="literature_summary"
        )
        assert len(results) >= 1

    def test_search_no_results(self, populated_repo: ResearchRepository) -> None:
        results = populated_repo.search("quantum_nonexistent_xyz")
        assert len(results) == 0

    def test_search_empty_repo(self, repo: ResearchRepository) -> None:
        results = repo.search("anything")
        assert results == []

    def test_list_runs(self, populated_repo: ResearchRepository) -> None:
        runs = populated_repo.list_runs()
        assert "run-001" in runs
        assert "run-002" in runs

    def test_list_runs_empty(self, repo: ResearchRepository) -> None:
        runs = repo.list_runs()
        assert runs == []

    def test_get_run_artifacts(self, populated_repo: ResearchRepository) -> None:
        artifacts = populated_repo.get_run_artifacts("run-001")
        assert "literature_summary" in artifacts
        assert "experiment_results" in artifacts

    def test_get_run_artifacts_missing(self, populated_repo: ResearchRepository) -> None:
        artifacts = populated_repo.get_run_artifacts("run-999")
        assert artifacts == {}

    def test_import_literature(self, populated_repo: ResearchRepository) -> None:
        lit = populated_repo.import_literature("run-001")
        assert isinstance(lit, list)
        assert len(lit) >= 1

    def test_import_literature_missing_run(self, populated_repo: ResearchRepository) -> None:
        lit = populated_repo.import_literature("run-999")
        assert lit == []

    def test_import_code_template(self, populated_repo: ResearchRepository) -> None:
        code = populated_repo.import_code_template("run-002", "pytorch")
        assert code is not None
        assert "torch" in code

    def test_import_code_template_no_match(self, populated_repo: ResearchRepository) -> None:
        code = populated_repo.import_code_template("run-002", "tensorflow_xyz")
        assert code is None


# ── Publisher Tests ──────────────────────────────────────────────────


class TestArtifactPublisher:
    def test_publish_from_run_dir(
        self,
        run_dir: Path,
        tmp_path: Path,
        canonical_projection: SimpleNamespace,
    ) -> None:
        repo = ResearchRepository(repo_dir=tmp_path / "pub_repo")
        publisher = ArtifactPublisher(repo)
        count = publisher.publish_from_run_dir("test-run", run_dir)
        assert count == 4
        published = repo.get_run_artifacts("test-run")["experiment_results"]
        assert published["canonical_manifest_sha256"] == "a" * 64
        assert published["selected_execution_sha256"] == "d" * 64

    def test_publish_reconstruction_failure_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = ResearchRepository(repo_dir=tmp_path / "pub_repo2")
        publisher = ArtifactPublisher(repo)
        monkeypatch.setattr(
            "researchclaw.pipeline.external_release_projection."
            "load_external_release_projection",
            lambda _run_dir: (_ for _ in ()).throw(ValueError("invalid release")),
        )
        with pytest.raises(ValueError, match="invalid release"):
            publisher.publish_from_run_dir("invalid", tmp_path / "legacy-run")
        assert not repo.repo_dir.exists()

    def test_direct_experiment_extract_uses_canonical_projection(
        self, tmp_path: Path, canonical_projection: SimpleNamespace
    ) -> None:
        publisher = ArtifactPublisher.__new__(ArtifactPublisher)
        result = publisher._extract_experiments(tmp_path / "run")
        assert result["candidate_id"] == "cand-" + "b" * 64
        assert result["metric_observations"] == {"accuracy": ["0.95"]}


# ── Subscriber Tests ─────────────────────────────────────────────────


class TestArtifactSubscriber:
    def test_find_relevant_literature(self, populated_repo: ResearchRepository) -> None:
        sub = ArtifactSubscriber(populated_repo)
        results = sub.find_relevant_literature("transformer")
        assert len(results) >= 1

    def test_find_similar_experiments(self, populated_repo: ResearchRepository) -> None:
        sub = ArtifactSubscriber(populated_repo)
        results = sub.find_similar_experiments("resnet")
        assert len(results) >= 1

    def test_find_code_templates(self, populated_repo: ResearchRepository) -> None:
        sub = ArtifactSubscriber(populated_repo)
        results = sub.find_code_templates("pytorch")
        assert len(results) >= 1

    def test_import_best_practices(self, populated_repo: ResearchRepository) -> None:
        sub = ArtifactSubscriber(populated_repo)
        practices = sub.import_best_practices("transformer")
        assert isinstance(practices, str)

    def test_import_best_practices_empty(self, repo: ResearchRepository) -> None:
        sub = ArtifactSubscriber(repo)
        practices = sub.import_best_practices("nonexistent")
        assert practices == ""


# ── Dedup Tests ──────────────────────────────────────────────────────


class TestDedup:
    def test_content_hash_deterministic(self) -> None:
        h1 = content_hash({"a": 1, "b": 2})
        h2 = content_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_content_hash_different(self) -> None:
        h1 = content_hash({"a": 1})
        h2 = content_hash({"a": 2})
        assert h1 != h2

    def test_deduplicate_artifacts(self) -> None:
        artifacts = [
            {"content": {"x": 1}, "tags": ["a"]},
            {"content": {"x": 1}, "tags": ["b"]},  # duplicate content
            {"content": {"y": 2}, "tags": ["c"]},
        ]
        unique = deduplicate_artifacts(artifacts)
        assert len(unique) == 2

    def test_deduplicate_empty(self) -> None:
        assert deduplicate_artifacts([]) == []


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _rewrite_repository_generation(
    repo: ResearchRepository, run_id: str, target: str, invalid: object
) -> None:
    run_root = repo.repo_dir / run_id
    pointer_path = run_root / "publication.json"
    pointer = json.loads(pointer_path.read_text())
    generation = run_root / pointer["generation"]
    manifest_path = generation / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    artifact_ref = manifest["artifacts"][0]
    artifact_path = generation / artifact_ref["path"]

    if target == "artifact":
        artifact = json.loads(artifact_path.read_text())
        artifact["schema_version"] = invalid
        artifact_bytes = _canonical_bytes(artifact)
        artifact_path.write_bytes(artifact_bytes)
        artifact_ref["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    elif target == "manifest":
        manifest["schema_version"] = invalid

    _replace_generation_manifest(run_root, generation, pointer, manifest)
    if target == "pointer":
        updated = json.loads(pointer_path.read_text())
        updated["schema_version"] = invalid
        pointer_path.write_bytes(_canonical_bytes(updated))


def _replace_generation_manifest(
    run_root: Path,
    generation: Path,
    pointer: dict[str, object],
    manifest: dict[str, object],
) -> None:
    manifest_bytes = _canonical_bytes(manifest)
    (generation / "bundle_manifest.json").write_bytes(manifest_bytes)
    generation_hash = hashlib.sha256(manifest_bytes).hexdigest()
    renamed = generation.parent / f"gen-{generation_hash}"
    generation.rename(renamed)
    pointer["generation"] = f"generations/{renamed.name}"
    pointer["manifest_sha256"] = generation_hash
    (run_root / "publication.json").write_bytes(_canonical_bytes(pointer))
