from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CANONICAL_EVIDENCE_CAPABILITIES,
    CAPABILITY_SCHEMA_VERSION,
    CanonicalEvidenceMigrationIncomplete,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage24_publication import Stage24PublicationSnapshot
from researchclaw.pipeline.stage25_publication import (
    STAGE25_MANIFEST_PATH,
    STAGE25_OUTPUT_PATH,
    Stage25PublicationError,
    _build_manifest,
    _load_stage25_from_namespace,
    _publish_after_invalidation,
    execute_stage25_deai,
    load_stage25_publication,
    parse_stage25_audit,
    parse_stage25_manifest,
)
from researchclaw.pipeline import executor, runner
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage_impls._release_audit import _execute_deai_audit
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline.stages import SKIP_FORBIDDEN_STAGES, Stage, StageStatus
from researchclaw.hitl.intervention import HumanAction, HumanInput


@pytest.fixture(autouse=True)
def _enable_complete_capability_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: CAPABILITY_SCHEMA_VERSION
            for name in CANONICAL_EVIDENCE_CAPABILITIES
        },
    )


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


def _source(paper: bytes = b"## Discussion\n\nMoreover, results remain bounded.\n") -> Stage24PublicationSnapshot:
    return Stage24PublicationSnapshot(
        manifest=_bound("stage-24/stage24_truth_manifest.json", b'{"sealed":true}\n'),
        paper=_bound("stage-23/paper_final_verified.md", paper),
        outputs=(
            _bound("stage-24/claims.json", b'{"claims":[]}\n'),
            _bound("stage-24/truth_audit.json", b'{"stage24_success":true}\n'),
        ),
        assessment_files=(
            _bound("stage-24/citation-assessments/a.json", b'{"verdict":"supported"}\n'),
        ),
    )


def _config() -> RCConfig:
    return RCConfig.load(
        Path("config.deepseek.sectional-dry-run.yaml"), check_paths=False
    )


def test_stage25_publishes_manifest_last_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    source = _source()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        lambda *_args: source,
    )

    snapshot = execute_stage25_deai(
        run_dir, stage_dir, runtime_config=_config(), llm=None
    )

    assert set(path.name for path in stage_dir.iterdir()) == {
        STAGE25_OUTPUT_PATH,
        STAGE25_MANIFEST_PATH,
    }
    audit = parse_stage25_audit(snapshot.audit.content)
    assert audit["paper_sha256"] == source.paper.sha256
    assert audit["recommend_only"] is True
    manifest = parse_stage25_manifest(snapshot.manifest.content)
    assert manifest["stage24_manifest_sha256"] == source.manifest.sha256
    assert load_stage25_publication(run_dir, _config()) == snapshot


def test_stage25_direct_entries_guard_before_namespace_or_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        dict(CANONICAL_EVIDENCE_CAPABILITIES),
    )
    run_dir = tmp_path / "missing-run"
    source = _source()

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        execute_stage25_deai(
            run_dir,
            run_dir / "stage-25",
            runtime_config=_config(),
            llm=None,
        )

    class NamespaceSpy:
        def __init__(self, path: Path) -> None:
            self.run_dir = path

        def direct_entries(self) -> tuple[str, ...]:
            raise AssertionError("namespace accessed before capability guard")

    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _load_stage25_from_namespace(
            NamespaceSpy(run_dir), source=source  # type: ignore[arg-type]
        )
    with pytest.raises(CanonicalEvidenceMigrationIncomplete):
        _publish_after_invalidation(
            NamespaceSpy(run_dir),  # type: ignore[arg-type]
            source=source,
            runtime_config=_config(),
            llm=None,
        )

    assert not run_dir.exists()


def test_stage25_source_change_at_fixpoint_removes_old_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    (stage_dir / STAGE25_OUTPUT_PATH).write_text("stale", encoding="utf-8")
    (stage_dir / STAGE25_MANIFEST_PATH).write_text("stale", encoding="utf-8")
    source = _source()
    changed = replace(
        source,
        paper=_bound("stage-23/paper_final_verified.md", source.paper.content + b"changed"),
    )
    calls = 0

    def load(*_args: object) -> Stage24PublicationSnapshot:
        nonlocal calls
        calls += 1
        return source if calls == 1 else changed

    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        load,
    )

    with pytest.raises(Stage25PublicationError, match="changed"):
        execute_stage25_deai(run_dir, stage_dir, runtime_config=_config(), llm=None)

    assert tuple(stage_dir.iterdir()) == ()


def test_stage25_v1_never_calls_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    source = _source()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        lambda *_args: source,
    )

    class ForbiddenLLM:
        def chat(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Stage 25 v1 must not call an LLM")

    snapshot = execute_stage25_deai(
        run_dir,
        stage_dir,
        runtime_config=_config(),
        llm=ForbiddenLLM(),  # type: ignore[arg-type]
    )

    assert parse_stage25_audit(snapshot.audit.content)["counts"]["total"] == 1


def test_stage25_rejects_semantic_tamper_with_synchronized_manifest_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    source = _source()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        lambda *_args: source,
    )

    execute_stage25_deai(run_dir, stage_dir, runtime_config=_config(), llm=None)
    value = json.loads((stage_dir / STAGE25_OUTPUT_PATH).read_text(encoding="utf-8"))
    value["suggestions"] = []
    value["counts"] = {"total": 0, "touches_claim": 0}
    tampered = canonical_authority_json_text(value).encode("utf-8")
    (stage_dir / STAGE25_OUTPUT_PATH).write_bytes(tampered)
    manifest = _build_manifest(source, tampered)
    (stage_dir / STAGE25_MANIFEST_PATH).write_text(
        canonical_authority_json_text(manifest), encoding="utf-8"
    )

    with pytest.raises(Stage25PublicationError, match="semantic replay mismatch"):
        load_stage25_publication(run_dir, _config())


def test_stage25_replay_rejects_output_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    source = _source()
    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        lambda *_args: source,
    )
    execute_stage25_deai(run_dir, stage_dir, runtime_config=_config(), llm=None)
    value = json.loads((stage_dir / STAGE25_OUTPUT_PATH).read_text(encoding="utf-8"))
    value["applied"] = True
    (stage_dir / STAGE25_OUTPUT_PATH).write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(Stage25PublicationError):
        load_stage25_publication(run_dir, _config())


def test_stage25_wrapper_maps_canonical_producer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    (stage_dir / STAGE25_OUTPUT_PATH).write_text("stale", encoding="utf-8")
    (stage_dir / STAGE25_MANIFEST_PATH).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._release_audit.execute_stage25_deai",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Stage25PublicationError("source invalid")
        ),
    )

    result = _execute_deai_audit(
        stage_dir,
        run_dir,
        _config(),
        AdapterBundle(),
        llm=None,
    )

    assert result.status is StageStatus.FAILED
    assert "source invalid" in (result.error or "")


class _HITLSession:
    def __init__(
        self,
        action: HumanAction,
        *,
        edited: bool = False,
        on_wait: object | None = None,
        cost_budget: float = 0.0,
        pause_after: bool = True,
    ) -> None:
        self.action = action
        self.edited = edited
        self.on_wait = on_wait
        self.pause_after = pause_after
        self.config = SimpleNamespace(cost_budget_usd=cost_budget)

    def should_pause_before(self, _stage: int) -> bool:
        return True

    def should_pause_after(self, _stage: int) -> bool:
        return self.pause_after

    def pause(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_human(self) -> HumanInput:
        if callable(self.on_wait):
            self.on_wait()
        return HumanInput(
            action=self.action,
            edited_files={"deai_audit.json": "changed"} if self.edited else {},
        )

    def get_policy(self, _stage: int) -> SimpleNamespace:
        return SimpleNamespace(require_approval=False, min_quality_score=0.0)


def _seed_stage25_authority(run_dir: Path, external: Path) -> Path:
    stage_dir = run_dir / "stage-25"
    staging = stage_dir / ".stage25-publication.staging"
    staging.mkdir(parents=True)
    (staging / STAGE25_OUTPUT_PATH).write_text("staged", encoding="utf-8")
    (stage_dir / STAGE25_OUTPUT_PATH).write_text("output", encoding="utf-8")
    (stage_dir / STAGE25_MANIFEST_PATH).write_text("manifest", encoding="utf-8")
    (stage_dir / "diagnostic.txt").write_text("retain", encoding="utf-8")
    (stage_dir / "diagnostic-link").symlink_to(external)
    return stage_dir


def test_stage25_generic_skip_is_structurally_forbidden(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    assert Stage.DEAI_AUDIT in SKIP_FORBIDDEN_STAGES
    with pytest.raises(ValueError, match="evidence-authority stage: 25"):
        runner._write_skipped_stage_outputs(run_dir, Stage.DEAI_AUDIT, "run")
    assert not (run_dir / "stage-25").exists()


@pytest.mark.parametrize(
    "action,edited",
    tuple((action, False) for action in HumanAction if action is not HumanAction.APPROVE)
    + ((HumanAction.APPROVE, True),),
)
@pytest.mark.parametrize("hook", ("pre", "post"))
def test_stage25_hitl_only_accepts_unedited_approval_and_preserves_diagnostics(
    tmp_path: Path,
    action: HumanAction,
    edited: bool,
    hook: str,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("external", encoding="utf-8")
    stage_dir = _seed_stage25_authority(run_dir, external)
    adapters = AdapterBundle(hitl=_HITLSession(action, edited=edited))
    if hook == "pre":
        result = executor._run_hitl_pre_stage(
            Stage.DEAI_AUDIT, run_dir, adapters, config=_config()
        )
        assert result is not None
    else:
        result = executor._run_hitl_post_stage(
            Stage.DEAI_AUDIT,
            StageResult(
                stage=Stage.DEAI_AUDIT,
                status=StageStatus.DONE,
                artifacts=(STAGE25_OUTPUT_PATH, STAGE25_MANIFEST_PATH),
            ),
            run_dir,
            adapters,
            config=_config(),
        )
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert not (stage_dir / STAGE25_MANIFEST_PATH).exists()
    assert not (stage_dir / STAGE25_OUTPUT_PATH).exists()
    assert not (stage_dir / ".stage25-publication.staging").exists()
    assert (stage_dir / "diagnostic.txt").read_text(encoding="utf-8") == "retain"
    assert (stage_dir / "diagnostic-link").is_symlink()
    assert external.read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("hook", ("pre", "post"))
@pytest.mark.parametrize("replacement", ("run", "stage"))
@pytest.mark.parametrize("action", (HumanAction.SKIP, HumanAction.APPROVE))
def test_stage25_hitl_holds_namespace_across_parent_replacement(
    tmp_path: Path,
    hook: str,
    replacement: str,
    action: HumanAction,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    stage_dir = _seed_stage25_authority(run_dir, sentinel)
    moved_run = tmp_path / "run-moved"
    moved_stage = run_dir / "stage-25-moved"

    def replace() -> None:
        if replacement == "run":
            run_dir.rename(moved_run)
            run_dir.symlink_to(external, target_is_directory=True)
        else:
            stage_dir.rename(moved_stage)
            stage_dir.symlink_to(external, target_is_directory=True)

    adapters = AdapterBundle(hitl=_HITLSession(action, on_wait=replace))
    if hook == "pre":
        result = executor._run_hitl_pre_stage(
            Stage.DEAI_AUDIT, run_dir, adapters, config=_config()
        )
        assert result is not None
    else:
        result = executor._run_hitl_post_stage(
            Stage.DEAI_AUDIT,
            StageResult(
                stage=Stage.DEAI_AUDIT,
                status=StageStatus.DONE,
                artifacts=(STAGE25_OUTPUT_PATH, STAGE25_MANIFEST_PATH),
            ),
            run_dir,
            adapters,
            config=_config(),
        )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert sentinel.read_text(encoding="utf-8") == "external"
    assert set(external.iterdir()) == {sentinel}

    if replacement == "run":
        run_dir.unlink()
        moved_run.rename(run_dir)
        restored_stage = run_dir / "stage-25"
    else:
        stage_dir.unlink()
        moved_stage.rename(stage_dir)
        restored_stage = stage_dir
    assert not (restored_stage / STAGE25_MANIFEST_PATH).exists()
    assert not (restored_stage / STAGE25_OUTPUT_PATH).exists()
    assert not (restored_stage / ".stage25-publication.staging").exists()
    assert (restored_stage / "diagnostic.txt").is_file()


def test_stage25_hitl_missing_run_state_does_not_touch_appearing_external_path(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")

    def replace() -> None:
        run_dir.symlink_to(external, target_is_directory=True)

    result = executor._run_hitl_pre_stage(
        Stage.DEAI_AUDIT,
        run_dir,
        AdapterBundle(
            hitl=_HITLSession(HumanAction.SKIP, on_wait=replace)
        ),
        config=_config(),
    )

    assert result is not None
    assert result.status is StageStatus.FAILED
    assert set(external.iterdir()) == {sentinel}
    assert sentinel.read_text(encoding="utf-8") == "external"


def test_stage25_hitl_safely_creates_missing_stage_under_held_run_fd(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = executor._run_hitl_pre_stage(
        Stage.DEAI_AUDIT,
        run_dir,
        AdapterBundle(hitl=_HITLSession(HumanAction.APPROVE)),
        config=_config(),
    )

    assert result is None
    assert (run_dir / "stage-25").is_dir()
    assert tuple((run_dir / "stage-25").iterdir()) == ()


def test_stage25_direct_guard_and_invalidation_require_held_namespace(
    tmp_path: Path,
) -> None:
    live_run = tmp_path / "run"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    live_run.symlink_to(external, target_is_directory=True)
    human_input = HumanInput(action=HumanAction.SKIP)

    result = executor._guard_authority_human_input(
        Stage.DEAI_AUDIT, live_run, human_input
    )
    assert result is not None
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    with pytest.raises(OSError, match="requires a held namespace"):
        executor._invalidate_hitl_authority_stage(Stage.DEAI_AUDIT, live_run)

    assert set(external.iterdir()) == {sentinel}
    assert sentinel.read_text(encoding="utf-8") == "external"


def test_stage25_cost_guard_holds_namespace_across_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researchclaw.hitl.cost_guard import CostGuard

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    _seed_stage25_authority(run_dir, sentinel)
    moved_run = tmp_path / "run-moved"

    def replace() -> None:
        run_dir.rename(moved_run)
        run_dir.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(CostGuard, "should_pause", lambda *_args: True)
    monkeypatch.setattr(CostGuard, "format_display", lambda *_args: "over budget")
    result = executor._run_hitl_post_stage(
        Stage.DEAI_AUDIT,
        StageResult(
            stage=Stage.DEAI_AUDIT,
            status=StageStatus.DONE,
            artifacts=(STAGE25_OUTPUT_PATH, STAGE25_MANIFEST_PATH),
        ),
        run_dir,
        AdapterBundle(
            hitl=_HITLSession(
                HumanAction.SKIP,
                on_wait=replace,
                cost_budget=1.0,
                pause_after=False,
            )
        ),
        config=_config(),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert set(external.iterdir()) == {sentinel}
    assert sentinel.read_text(encoding="utf-8") == "external"
    run_dir.unlink()
    moved_run.rename(run_dir)
    assert tuple(
        path.name
        for path in (run_dir / "stage-25").iterdir()
        if path.name in {
            STAGE25_OUTPUT_PATH,
            STAGE25_MANIFEST_PATH,
            ".stage25-publication.staging",
        }
    ) == ()


def test_stage25_parsers_reject_duplicate_keys_and_noncanonical_artifacts() -> None:
    with pytest.raises(Stage25PublicationError):
        parse_stage25_audit(b'{"schema_version":1,"schema_version":1}\n')
    source = _source()
    manifest = {
        "schema_version": 1,
        "publication_policy_version": "stage25_deai_v1",
        "stage24_manifest_path": source.manifest.path,
        "stage24_manifest_sha256": source.manifest.sha256,
        "source_paper_path": source.paper.path,
        "source_paper_sha256": source.paper.sha256,
        "stage24_artifacts": [
            {"path": source.outputs[0].path, "sha256": source.outputs[0].sha256},
            {"path": source.outputs[0].path, "sha256": source.outputs[0].sha256},
        ],
        "output_path": "stage-25/deai_audit.json",
        "output_sha256": "0" * 64,
    }
    with pytest.raises(Stage25PublicationError, match="not canonical"):
        parse_stage25_manifest(json.dumps(manifest).encode("utf-8"))


@pytest.mark.parametrize(
    "collision_name",
    (STAGE25_MANIFEST_PATH, STAGE25_OUTPUT_PATH, ".stage25-publication.staging"),
)
def test_stage25_cleanup_invalidates_other_authority_despite_unsafe_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision_name: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    for name in (
        STAGE25_MANIFEST_PATH,
        STAGE25_OUTPUT_PATH,
        ".stage25-publication.staging",
    ):
        if name == collision_name:
            (stage_dir / name / "nested").mkdir(parents=True)
        else:
            (stage_dir / name).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        lambda *_args: _source(),
    )

    with pytest.raises(OSError, match="cleanup failed"):
        execute_stage25_deai(run_dir, stage_dir, runtime_config=_config(), llm=None)

    for name in (
        STAGE25_MANIFEST_PATH,
        STAGE25_OUTPUT_PATH,
        ".stage25-publication.staging",
    ):
        if name != collision_name:
            assert not (stage_dir / name).exists()
    if collision_name != STAGE25_MANIFEST_PATH:
        assert not (stage_dir / STAGE25_MANIFEST_PATH).exists()


def test_stage25_parent_replacement_never_writes_external_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-25"
    stage_dir.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external", encoding="utf-8")
    detached = run_dir / "stage-25-moved"
    source = _source()
    calls = 0

    def replace_parent(*_args: object) -> Stage24PublicationSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            stage_dir.rename(detached)
            stage_dir.symlink_to(external, target_is_directory=True)
        return source

    monkeypatch.setattr(
        "researchclaw.pipeline.stage25_publication.load_stage24_publication_snapshot",
        replace_parent,
    )

    with pytest.raises(OSError, match="stage-25 directory changed"):
        execute_stage25_deai(run_dir, stage_dir, runtime_config=_config(), llm=None)

    assert sentinel.read_text(encoding="utf-8") == "external"
    assert set(external.iterdir()) == {sentinel}
    assert tuple(detached.iterdir()) == ()
