from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.literature.verify import (
    CitationResult,
    VerificationReport,
    VerifyStatus,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CANONICAL_EVIDENCE_CAPABILITIES,
    incomplete_canonical_evidence_capabilities,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage22_publication import Stage22PublicationSnapshot
from researchclaw.pipeline.stage23_input_bundle import Stage23InputBundle
from researchclaw.pipeline.stage23_verification import (
    Stage23VerificationError,
    execute_canonical_stage23,
    parse_stage23_verification_manifest,
    parse_stage23_verification_report,
    publish_stage23_verification,
    load_stage23_verification_publication,
    validate_stage23_verification_publication,
)
from researchclaw.pipeline._helpers import StageResult
from researchclaw.pipeline import executor
from researchclaw.pipeline import stage23_verification as stage23_module
from researchclaw.pipeline.stage_impls import _review_publish
from researchclaw.pipeline.stages import SKIP_FORBIDDEN_STAGES, Stage, StageStatus
from researchclaw.hitl.intervention import HumanAction, HumanInput


@pytest.fixture
def canonical_config() -> RCConfig:
    return RCConfig.load(
        Path(__file__).parents[1] / "config.deepseek.sectional-dry-run.yaml",
        check_paths=False,
    )


def _bound(path: str, content: bytes) -> BoundArtifact:
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


def _bundle(config: RCConfig, *, claim_scope: str = "pipeline_validation") -> Stage23InputBundle:
    paper = _bound(
        "stage-22/paper_final.md",
        b"## Introduction\n\nEvidence [smith2024test].\n",
    )
    bibliography = _bound(
        "stage-22/references.bib",
        b"@article{smith2024test,\n  title={Test},\n  year={2024}\n}\n",
    )
    latex = _bound(
        "stage-22/paper.tex",
        b"Evidence \\cite{smith2024test}.\n",
    )
    manifest = _bound(
        "stage-22/stage22_export_manifest.json", b'{"sealed":true}\n'
    )
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
    )
    stage22_inputs = SimpleNamespace(
        evidence=evidence,
        canonical_config=config,
        claim_scope=claim_scope,
    )
    publication = Stage22PublicationSnapshot(
        manifest=manifest,
        outputs=(paper, bibliography, latex),
    )
    return Stage23InputBundle(
        stage22_inputs=stage22_inputs,  # type: ignore[arg-type]
        publication=publication,
        paper=paper,
        bibliography=bibliography,
        latex=latex,
        cited_keys=("smith2024test",),
        claim_scope=claim_scope,
    )


def _report(status: VerifyStatus = VerifyStatus.VERIFIED) -> VerificationReport:
    result = CitationResult(
        cite_key="smith2024test",
        title="Verified title",
        status=status,
        confidence=0.95,
        method="title_search",
    )
    return VerificationReport(
        total=1,
        verified=int(status is VerifyStatus.VERIFIED),
        suspicious=int(status is VerifyStatus.SUSPICIOUS),
        hallucinated=int(status is VerifyStatus.HALLUCINATED),
        skipped=int(status is VerifyStatus.SKIPPED),
        results=[result],
    )


def _patch_inputs(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Stage23InputBundle,
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.load_stage23_input_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_stage23_input_bundle_unchanged",
        lambda *_args, **_kwargs: None,
    )


def test_stage23_publishes_manifest_last_and_replays(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )

    outcome = execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
    )

    assert outcome.degraded is False
    assert set(outcome.artifacts) == {
        "paper_final_verified.md",
        "references_verified.bib",
        "verification_report.json",
        "stage23_verification_manifest.json",
    }
    assert validate_stage23_verification_publication(run_dir, bundle)
    snapshot = load_stage23_verification_publication(run_dir, bundle)
    paper = snapshot.require_output("paper_final_verified.md")
    assert paper.content == bundle.paper.content
    assert paper.sha256 == hashlib.sha256(bundle.paper.content).hexdigest()


@pytest.mark.parametrize("mutation", ("extra", "delete", "symlink"))
def test_stage23_snapshot_rejects_namespace_mutation(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )
    execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
    )
    paper = stage_dir / "paper_final_verified.md"
    if mutation == "extra":
        (stage_dir / "shadow.tmp").write_text("shadow", encoding="utf-8")
    elif mutation == "delete":
        paper.unlink()
    else:
        target = tmp_path / "external-paper.md"
        target.write_bytes(paper.read_bytes())
        paper.unlink()
        paper.symlink_to(target)

    with pytest.raises(Stage23VerificationError):
        load_stage23_verification_publication(run_dir, bundle)


@pytest.mark.parametrize(
    "mutation",
    ("manifest", "output", "extra", "delete", "parent"),
)
def test_stage23_snapshot_rejects_mutation_after_initial_semantic_replay(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )
    execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
    )
    original = stage23_module._verify_publication
    mutated = False
    external = tmp_path / "external-stage-23"

    def mutate_after_first_replay(namespace, manifest, captured_bundle):
        nonlocal mutated
        original(namespace, manifest, captured_bundle)
        if mutated:
            return
        mutated = True
        if mutation == "manifest":
            (stage_dir / "stage23_verification_manifest.json").write_text(
                "{}", encoding="utf-8"
            )
        elif mutation == "output":
            (stage_dir / "paper_final_verified.md").write_text(
                "changed", encoding="utf-8"
            )
        elif mutation == "extra":
            (stage_dir / "shadow.tmp").write_text("shadow", encoding="utf-8")
        elif mutation == "delete":
            (stage_dir / "paper_final_verified.md").unlink()
        else:
            moved = run_dir / "stage-23-moved"
            stage_dir.rename(moved)
            external.mkdir()
            (external / "sentinel").write_text("external", encoding="utf-8")
            stage_dir.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(stage23_module, "_verify_publication", mutate_after_first_replay)

    with pytest.raises(Stage23VerificationError):
        load_stage23_verification_publication(run_dir, bundle)
    if mutation == "parent":
        assert (external / "sentinel").read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize(
    "status,claim_scope",
    (
        (VerifyStatus.HALLUCINATED, "pipeline_validation"),
        (VerifyStatus.SUSPICIOUS, "research_release"),
        (VerifyStatus.SKIPPED, "exploratory"),
    ),
)
def test_stage23_fatal_outcome_leaves_no_success_outputs(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    status: VerifyStatus,
    claim_scope: str,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    for name in (
        "verification_report.json",
        "references_verified.bib",
        "paper_final_verified.md",
        "stage23_verification_manifest.json",
    ):
        (stage_dir / name).write_text("stale", encoding="utf-8")
    bundle = _bundle(canonical_config, claim_scope=claim_scope)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(status),
    )

    with pytest.raises(Stage23VerificationError, match="incomplete or invalid"):
        execute_canonical_stage23(
            run_dir,
            stage_dir,
            canonical_config,
            relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
        )

    assert list(stage_dir.iterdir()) == []


def test_stage23_pipeline_validation_can_publish_degraded_result(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(VerifyStatus.SUSPICIOUS),
    )

    outcome = execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=None,
    )

    assert outcome.degraded is True
    manifest = validate_stage23_verification_publication(run_dir, bundle)
    assert manifest["claim_scope"] == "pipeline_validation"
    assert "smith2024test" not in (
        stage_dir / "references_verified.bib"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "score,degraded",
    (
        (Decimal("0.499"), True),
        (Decimal("0.500"), False),
        (Decimal("0.501"), False),
    ),
)
def test_stage23_relevance_boundary_uses_stored_exact_decimal(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    score: Decimal,
    degraded: bool,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )

    outcome = execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=lambda _results: {"smith2024test": score},
    )

    report = parse_stage23_verification_report(
        (stage_dir / "verification_report.json").read_text(encoding="utf-8")
    )
    assert report["results"][0]["relevance_score"] == score
    assert outcome.degraded is degraded


def test_stage23_late_input_change_removes_published_namespace(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.load_stage23_input_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )
    checks = 0

    def changed(*_args: object, **_kwargs: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("late Stage 22 change")

    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_stage23_input_bundle_unchanged",
        changed,
    )

    with pytest.raises(ValueError, match="late Stage 22 change"):
        execute_canonical_stage23(
            run_dir,
            stage_dir,
            canonical_config,
            relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
        )

    assert list(stage_dir.iterdir()) == []


def test_stage23_replay_rejects_synchronized_output_forgery(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config)
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )
    execute_canonical_stage23(
        run_dir,
        stage_dir,
        canonical_config,
        relevance_checker=lambda _results: {"smith2024test": Decimal("0.9")},
    )
    forged = b"## Forged\n"
    (stage_dir / "paper_final_verified.md").write_bytes(forged)
    manifest_path = stage_dir / "stage23_verification_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["outputs"]:
        if entry["path"] == "paper_final_verified.md":
            entry["sha256"] = hashlib.sha256(forged).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Stage23VerificationError, match="verified paper differs"):
        validate_stage23_verification_publication(run_dir, bundle)


@pytest.mark.parametrize(
    "payload",
    (
        '{"summary":{},"summary":{},"results":[]}',
        '{"summary":{"total":NaN},"results":[]}',
    ),
)
def test_stage23_report_parser_rejects_noncanonical_json(payload: str) -> None:
    with pytest.raises(Stage23VerificationError):
        parse_stage23_verification_report(payload)


def test_stage23_manifest_rejects_unknown_field() -> None:
    with pytest.raises(Stage23VerificationError, match="fields mismatch"):
        parse_stage23_verification_manifest('{"unknown":1}')


def test_stage23_publisher_invalidates_stale_namespace_before_output_validation(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage23_verification_manifest.json").write_text(
        "stale", encoding="utf-8"
    )
    (stage_dir / "paper_final_verified.md").write_text("stale", encoding="utf-8")

    with pytest.raises(Stage23VerificationError, match="namespace mismatch"):
        publish_stage23_verification(
            run_dir,
            stage_dir,
            bundle=_bundle(canonical_config),
            outputs={},
            precommit_check=lambda: None,
        )

    assert list(stage_dir.iterdir()) == []


class _HITLActionSession:
    def __init__(
        self,
        action: HumanAction,
        *,
        edited: bool = False,
        cost_budget: float = 0.0,
        pause_after: bool = True,
    ) -> None:
        self.action = action
        self.edited = edited
        self.pause_after = pause_after
        self.config = SimpleNamespace(cost_budget_usd=cost_budget)

    def should_pause_before(self, _stage: int) -> bool:
        return True

    def should_pause_after(self, _stage: int) -> bool:
        return self.pause_after

    def pause(self, *_args: object, **_kwargs: object) -> None:
        return None

    def wait_for_human(self) -> HumanInput:
        return HumanInput(
            action=self.action,
            edited_files={"paper.md": "changed"}
            if self.action is HumanAction.EDIT or self.edited
            else {},
        )

    def get_policy(self, _stage: int) -> SimpleNamespace:
        return SimpleNamespace(require_approval=False, min_quality_score=0.0)


@pytest.mark.parametrize("stage", tuple(Stage(number) for number in range(18, 25)))
@pytest.mark.parametrize("action", (HumanAction.SKIP, HumanAction.ABORT))
def test_authority_hitl_pre_stage_stop_fails_and_clears_stale_outputs(
    tmp_path: Path,
    canonical_config: RCConfig,
    stage: Stage,
    action: HumanAction,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / f"stage-{int(stage):02d}"
    stage_dir.mkdir(parents=True)
    authority_name = (
        "stage24_truth_manifest.json"
        if stage is Stage.TRUTH_AUDIT
        else "stale-authority.json"
    )
    (stage_dir / authority_name).write_text("stale", encoding="utf-8")
    adapters = AdapterBundle(hitl=_HITLActionSession(action))

    result = executor._run_hitl_pre_stage(
        stage, run_dir, adapters, config=canonical_config
    )

    assert result is not None
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert list(stage_dir.iterdir()) == []


@pytest.mark.parametrize(
    "action,edited",
    tuple((action, False) for action in HumanAction if action is not HumanAction.APPROVE)
    + ((HumanAction.APPROVE, True),),
)
def test_stage24_hitl_pre_stage_only_accepts_unedited_approval(
    tmp_path: Path,
    canonical_config: RCConfig,
    action: HumanAction,
    edited: bool,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage24_truth_manifest.json").write_text(
        "stale", encoding="utf-8"
    )
    diagnostic = stage_dir / "diagnostic.txt"
    diagnostic.write_text("retain", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("external", encoding="utf-8")
    (stage_dir / "diagnostic-link").symlink_to(external)

    result = executor._run_hitl_pre_stage(
        Stage.TRUTH_AUDIT,
        run_dir,
        AdapterBundle(hitl=_HITLActionSession(action, edited=edited)),
        config=canonical_config,
    )

    assert result is not None
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert not (stage_dir / "stage24_truth_manifest.json").exists()
    assert diagnostic.read_text(encoding="utf-8") == "retain"
    assert (stage_dir / "diagnostic-link").is_symlink()
    assert external.read_text(encoding="utf-8") == "external"


def test_stage24_hitl_pre_stage_unedited_approval_proceeds(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    result = executor._run_hitl_pre_stage(
        Stage.TRUTH_AUDIT,
        tmp_path / "run",
        AdapterBundle(hitl=_HITLActionSession(HumanAction.APPROVE)),
        config=canonical_config,
    )

    assert result is None
    assert not (tmp_path / "run").exists()


def test_execute_stage_hitl_skip_cannot_reuse_stale_stage23_authority(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.canonical_evidence_capabilities.CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: CAPABILITY_SCHEMA_VERSION
            for name in CANONICAL_EVIDENCE_CAPABILITIES
        },
    )
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage23_verification_manifest.json").write_text(
        "stale", encoding="utf-8"
    )
    (stage_dir / "paper_final_verified.md").write_text("stale", encoding="utf-8")
    called = False

    def producer(*_args: object, **_kwargs: object) -> StageResult:
        nonlocal called
        called = True
        raise AssertionError("Stage 23 producer must not run after HITL SKIP")

    monkeypatch.setitem(executor._STAGE_EXECUTORS, Stage.CITATION_VERIFY, producer)
    result = executor.execute_stage(
        Stage.CITATION_VERIFY,
        run_dir=run_dir,
        run_id="hitl-skip",
        config=canonical_config,
        adapters=AdapterBundle(hitl=_HITLActionSession(HumanAction.SKIP)),
    )

    assert called is False
    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert list(stage_dir.iterdir()) == []


def test_stage24_hitl_collision_still_invalidates_manifest(
    tmp_path: Path,
    canonical_config: RCConfig,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-24"
    (stage_dir / "claims.json" / "nested").mkdir(parents=True)
    manifest = stage_dir / "stage24_truth_manifest.json"
    manifest.write_text("stale", encoding="utf-8")
    adapters = AdapterBundle(
        hitl=_HITLActionSession(HumanAction.SKIP)
    )

    result = executor._run_hitl_pre_stage(
        Stage.TRUTH_AUDIT, run_dir, adapters, config=canonical_config
    )

    assert result is not None
    assert result.status is StageStatus.FAILED
    assert not manifest.exists()


@pytest.mark.parametrize("stage", tuple(Stage(number) for number in range(18, 25)))
@pytest.mark.parametrize(
    "action,edited",
    tuple((action, False) for action in HumanAction if action is not HumanAction.APPROVE)
    + ((HumanAction.APPROVE, True),),
)
def test_authority_hitl_post_stage_mutation_fails_and_clears_publication(
    tmp_path: Path,
    canonical_config: RCConfig,
    stage: Stage,
    action: HumanAction,
    edited: bool,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / f"stage-{int(stage):02d}"
    stage_dir.mkdir(parents=True)
    authority_name = (
        "stage24_truth_manifest.json"
        if stage is Stage.TRUTH_AUDIT
        else "committed-authority.json"
    )
    (stage_dir / authority_name).write_text("valid", encoding="utf-8")
    adapters = AdapterBundle(
        hitl=_HITLActionSession(action, edited=edited)
    )
    completed = StageResult(
        stage=stage,
        status=StageStatus.DONE,
        artifacts=(authority_name,),
    )

    result = executor._run_hitl_post_stage(
        stage, completed, run_dir, adapters, config=canonical_config
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert list(stage_dir.iterdir()) == []


@pytest.mark.parametrize(
    "action,edited",
    tuple((action, False) for action in HumanAction if action is not HumanAction.APPROVE)
    + ((HumanAction.APPROVE, True),),
)
def test_authority_cost_guard_rejects_mutating_or_nonapprove_actions(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    action: HumanAction,
    edited: bool,
) -> None:
    from researchclaw.hitl.cost_guard import CostGuard

    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage23_verification_manifest.json").write_text(
        "valid", encoding="utf-8"
    )
    (stage_dir / "paper_final_verified.md").write_text("valid", encoding="utf-8")
    monkeypatch.setattr(CostGuard, "should_pause", lambda *_args: True)
    monkeypatch.setattr(CostGuard, "format_display", lambda *_args: "over budget")
    session = _HITLActionSession(
        action,
        edited=edited,
        cost_budget=1.0,
        pause_after=False,
    )
    completed = StageResult(
        stage=Stage.CITATION_VERIFY,
        status=StageStatus.DONE,
        artifacts=("stage23_verification_manifest.json",),
    )

    result = executor._run_hitl_post_stage(
        Stage.CITATION_VERIFY,
        completed,
        run_dir,
        AdapterBundle(hitl=session),
        config=canonical_config,
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert list(stage_dir.iterdir()) == []


@pytest.mark.parametrize(
    "token,expected",
    (
        ("0.49999999999999999", Decimal("0.49999999999999999")),
        ("0.50000000000000001", Decimal("0.50000000000000001")),
    ),
)
@pytest.mark.parametrize("precision", (7, 28, 80))
def test_relevance_llm_json_preserves_decimal_token(
    token: str,
    expected: Decimal,
    precision: int,
) -> None:
    class RelevanceLLM:
        def chat(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(content=f'{{"smith2024test": {token}}}')

    with localcontext() as context:
        context.prec = precision
        scores = _review_publish._check_citation_relevance(
            RelevanceLLM(), "topic", _report().results
        )

    assert scores == {"smith2024test": expected}


@pytest.mark.parametrize(
    "score,should_fail",
    (
        (Decimal("0.49999999999999999"), True),
        (Decimal("0.50000000000000001"), False),
    ),
)
def test_research_release_uses_exact_decimal_relevance_boundary(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
    score: Decimal,
    should_fail: bool,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    bundle = _bundle(canonical_config, claim_scope="research_release")
    _patch_inputs(monkeypatch, bundle)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _report(),
    )

    if should_fail:
        with pytest.raises(Stage23VerificationError, match="incomplete or invalid"):
            execute_canonical_stage23(
                run_dir,
                stage_dir,
                canonical_config,
                relevance_checker=lambda _results: {"smith2024test": score},
            )
        assert list(stage_dir.iterdir()) == []
    else:
        outcome = execute_canonical_stage23(
            run_dir,
            stage_dir,
            canonical_config,
            relevance_checker=lambda _results: {"smith2024test": score},
        )
        assert outcome.degraded is False
        assert validate_stage23_verification_publication(run_dir, bundle)


def test_stage23_wrapper_returns_failed_without_artifacts(
    tmp_path: Path,
    canonical_config: RCConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-23"
    stage_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.execute_canonical_stage23",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Stage23VerificationError("closed")
        ),
    )

    result = _review_publish._execute_citation_verify(
        stage_dir,
        run_dir,
        canonical_config,
        AdapterBundle(),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()


def test_stage18_23_authority_functions_have_no_legacy_selectors() -> None:
    from researchclaw.pipeline import executor
    from researchclaw.pipeline import stage23_input_bundle, stage23_verification

    functions = (
        _review_publish._execute_peer_review,
        _review_publish._execute_paper_revision,
        _review_publish._execute_quality_gate,
        _review_publish._execute_knowledge_archive,
        _review_publish._execute_export_publish,
        _review_publish._execute_citation_verify,
    )
    forbidden = (
        "_read_prior_artifact",
        "load_canonical_bibliography",
        "validate_final_paper_citations",
        "stage-14*",
        "experiment_final",
        "_get_evolution_overlay",
        ".glob(",
        ".rglob(",
    )
    for function in functions:
        source = inspect.getsource(function)
        assert not any(token in source for token in forbidden), function.__name__

    for module in (stage23_input_bundle, stage23_verification):
        source = inspect.getsource(module)
        assert "_read_prior_artifact" not in source
        assert "load_canonical_bibliography" not in source
        assert "validate_final_paper_citations" not in source
        assert 'run_dir / "stage-22"' not in source
        assert ".glob(" not in source
        assert ".rglob(" not in source

    assert executor._STAGE_EXECUTORS[Stage.PEER_REVIEW] is functions[0]
    assert executor._STAGE_EXECUTORS[Stage.PAPER_REVISION] is functions[1]
    assert executor._STAGE_EXECUTORS[Stage.QUALITY_GATE] is functions[2]
    assert executor._STAGE_EXECUTORS[Stage.KNOWLEDGE_ARCHIVE] is functions[3]
    assert executor._STAGE_EXECUTORS[Stage.EXPORT_PUBLISH] is functions[4]
    assert executor._STAGE_EXECUTORS[Stage.CITATION_VERIFY] is functions[5]

    assert (
        CANONICAL_EVIDENCE_CAPABILITIES["stage19_22_consumers"]
        == CAPABILITY_SCHEMA_VERSION
    )
    assert (
        CANONICAL_EVIDENCE_CAPABILITIES["stage24_release_consumers"]
        == CAPABILITY_SCHEMA_VERSION
    )
    assert incomplete_canonical_evidence_capabilities() == (
        "external_and_persistent_consumers",
        "independent_release_reconstruction",
    )
    assert {
        Stage.PAPER_DRAFT,
        Stage.PEER_REVIEW,
        Stage.PAPER_REVISION,
        Stage.QUALITY_GATE,
        Stage.KNOWLEDGE_ARCHIVE,
        Stage.EXPORT_PUBLISH,
        Stage.CITATION_VERIFY,
    } <= SKIP_FORBIDDEN_STAGES
