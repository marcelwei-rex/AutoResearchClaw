from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal, getcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
    Stage19InputBundleError,
)
from researchclaw.pipeline.sectional_validation import SectionRevisionManifest
from researchclaw.pipeline.stage20_input_bundle import (
    Stage20InputBundle,
    Stage20InputBundleError,
    load_stage20_input_bundle,
    verify_stage20_input_bundle_unchanged,
)
from researchclaw.pipeline import stage20_input_bundle as stage20_bundle_module
from researchclaw.pipeline.stage_impls import _review_publish
from researchclaw.pipeline.stages import StageStatus


def _bound(path: str, text: str) -> BoundArtifact:
    content = text.encode("utf-8")
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


def _evidence(summary: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
        experiment_contract_path="stage-09/experiment_contract.yaml",
        experiment_contract_sha256="b" * 64,
        experiment_contract_bytes=b"claim_scope: pipeline_validation\n",
        run_config_bytes=b"config\n",
        summary=MappingProxyType(summary or {}),
    )


def _stage19_sources() -> SimpleNamespace:
    return SimpleNamespace(
        paper=_bound("stage-17/paper_draft.md", "draft"),
        reviews=_bound("stage-18/reviews.md", "reviews"),
    )


def test_stage20_legacy_bundle_binds_exact_stage19_publication(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "stage-19").mkdir(parents=True)
    evidence = _evidence()
    sources = _stage19_sources()
    revised = "paper [key]."
    (run_dir / "stage-19/paper_revised.md").write_text(revised, encoding="utf-8")
    binding = {
        "schema_version": 1,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": sources.paper.path,
        "source_paper_sha256": sources.paper.sha256,
        "source_reviews_path": sources.reviews.path,
        "source_reviews_sha256": sources.reviews.sha256,
        "revised_paper_path": "stage-19/paper_revised.md",
        "revised_paper_sha256": hashlib.sha256(revised.encode()).hexdigest(),
    }
    (run_dir / "stage-19/revision_evidence_binding.json").write_text(
        json.dumps(binding), encoding="utf-8"
    )

    bundle = load_stage20_input_bundle(
        run_dir,
        stage19_inputs=sources,
        evidence=evidence,
        claim_scope="pipeline_validation",
    )

    assert bundle.publication_mode == "legacy"
    assert bundle.revised_paper.text() == revised
    (run_dir / "stage-19/section_revision_manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(Stage20InputBundleError, match="exactly one"):
        load_stage20_input_bundle(
            run_dir,
            stage19_inputs=sources,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )


def test_stage20_sectional_bundle_binds_paper_and_canonical_evidence(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage19 = run_dir / "stage-19"
    stage19.mkdir(parents=True)
    evidence = _evidence()
    sources = _stage19_sources()
    revised = "sectional paper"
    (stage19 / "paper_revised.md").write_text(revised, encoding="utf-8")
    manifest = SectionRevisionManifest(
        schema_version=1,
        mode="sectional",
        claim_scope="pipeline_validation",
        experiment_contract_path=evidence.experiment_contract_path,
        experiment_contract_sha256=evidence.experiment_contract_sha256,
        canonical_experiment_evidence_path=evidence.manifest_path,
        canonical_experiment_evidence_sha256=evidence.manifest_sha256,
        writer_model="writer-model",
        critic_model="critic-model",
        source_paper_path=sources.paper.path,
        source_paper_sha256=sources.paper.sha256,
        source_reviews_path=sources.reviews.path,
        source_reviews_sha256=sources.reviews.sha256,
        ledger_sha256="c" * 64,
        plan_sha256="d" * 64,
        attempts_sha256="e" * 64,
        assessments_sha256="f" * 64,
        unresolved_comments_sha256="1" * 64,
        validation_context_path="stage-19/validation_context.json",
        validation_context_sha256="2" * 64,
        sections=(),
        comment_counts=(
            ("input", 0),
            ("resolved", 0),
            ("unresolved", 0),
            ("not_actionable_with_reason", 0),
        ),
        merged_paper_sha256=hashlib.sha256(revised.encode()).hexdigest(),
        completed=True,
    )
    (stage19 / "section_revision_manifest.json").write_text(
        json.dumps(manifest.to_dict()), encoding="utf-8"
    )

    bundle = load_stage20_input_bundle(
        run_dir,
        stage19_inputs=sources,
        evidence=evidence,
        claim_scope="pipeline_validation",
    )

    assert bundle.publication_mode == "sectional"
    assert bundle.revised_paper.sha256 == manifest.merged_paper_sha256

    manifest = manifest.to_dict()
    manifest["merged_paper_sha256"] = "0" * 64
    (stage19 / "section_revision_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(Stage20InputBundleError, match="merged_paper_sha256"):
        load_stage20_input_bundle(
            run_dir,
            stage19_inputs=sources,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )


def test_stage20_bundle_rejects_missing_duplicate_and_symlink_inputs(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage19 = run_dir / "stage-19"
    stage19.mkdir(parents=True)
    evidence = _evidence()
    sources = _stage19_sources()
    revised = "paper"
    paper_path = stage19 / "paper_revised.md"
    paper_path.write_text(revised, encoding="utf-8")

    with pytest.raises(Stage20InputBundleError, match="exactly one"):
        load_stage20_input_bundle(
            run_dir,
            stage19_inputs=sources,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )

    duplicate_binding = (
        '{"schema_version":1,"schema_version":1,'
        '"canonical_experiment_evidence_path":"canonical_experiment_evidence.json"}'
    )
    (stage19 / "revision_evidence_binding.json").write_text(
        duplicate_binding, encoding="utf-8"
    )
    with pytest.raises(Stage20InputBundleError, match="duplicate"):
        load_stage20_input_bundle(
            run_dir,
            stage19_inputs=sources,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )

    (stage19 / "revision_evidence_binding.json").unlink()
    paper_path.unlink()
    paper_path.symlink_to(tmp_path / "outside.md")
    with pytest.raises(Stage19InputBundleError, match="cannot open|not a regular"):
        load_stage20_input_bundle(
            run_dir,
            stage19_inputs=sources,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )


def test_stage20_ignores_versioned_shadow_paper(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage19 = run_dir / "stage-19"
    shadow = run_dir / "stage-19_v99"
    stage19.mkdir(parents=True)
    shadow.mkdir()
    evidence = _evidence()
    sources = _stage19_sources()
    revised = "canonical paper"
    (stage19 / "paper_revised.md").write_text(revised, encoding="utf-8")
    (shadow / "paper_revised.md").write_text("shadow poison", encoding="utf-8")
    binding = {
        "schema_version": 1,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": sources.paper.path,
        "source_paper_sha256": sources.paper.sha256,
        "source_reviews_path": sources.reviews.path,
        "source_reviews_sha256": sources.reviews.sha256,
        "revised_paper_path": "stage-19/paper_revised.md",
        "revised_paper_sha256": hashlib.sha256(revised.encode()).hexdigest(),
    }
    (stage19 / "revision_evidence_binding.json").write_text(
        json.dumps(binding), encoding="utf-8"
    )

    bundle = load_stage20_input_bundle(
        run_dir,
        stage19_inputs=sources,
        evidence=evidence,
        claim_scope="pipeline_validation",
    )

    assert bundle.revised_paper.text() == revised


def _run_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_during_fixpoint: bool = False,
    summary_override: dict | None = None,
    llm_response: str | None = None,
    graceful_degradation: bool = False,
) -> tuple[object, Path, str]:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    revised = "## Results\n\nCanonical metric [smith2024]."
    summary = {
        "best_run": {
            "status": "success",
            "metrics": {"primary_metric": Decimal("0.123456789012345678")},
        },
        "condition_summaries": {
            "canonical-condition": {
                "metrics": {"f1": Decimal("0.123456789012345678")}
            }
        },
        "metrics_summary": {
            "f1": {
                "min": Decimal("0.123456789012345678"),
                "max": Decimal("0.123456789012345678"),
                "mean": Decimal("0.123456789012345678"),
            }
        },
    }
    evidence = _evidence(summary_override if summary_override is not None else summary)
    config = SimpleNamespace(
        research=SimpleNamespace(
            quality_threshold=6.0,
            graceful_degradation=graceful_degradation,
        ),
        experiment=SimpleNamespace(metric_direction="maximize"),
    )
    sources = SimpleNamespace(citation_replay_inputs=lambda: object())
    stage20_inputs = Stage20InputBundle(
        stage19_inputs=sources,
        revised_paper=_bound("stage-19/paper_revised.md", revised),
        publication_binding=_bound("stage-19/revision_evidence_binding.json", "binding"),
        publication_mode="legacy",
    )
    authority = SimpleNamespace(
        effective_policy={
            "effective_min_unique_sources": 1,
            "effective_target_unique_sources": 1,
        },
        allowlist={"eligible_keys": ["smith2024"]},
        plan={
            "claims": [
                {"planned_citations": [{"cite_key": "smith2024"}]}
            ]
        },
    )
    monkeypatch.setattr(_review_publish, "load_canonical_experiment_evidence", lambda _: evidence)
    monkeypatch.setattr(_review_publish, "parse_config_snapshot_text", lambda *_a, **_k: config)
    monkeypatch.setattr(_review_publish, "semantic_config_sha256", lambda _: "same")
    monkeypatch.setattr(_review_publish, "_load_bound_stage19_inputs", lambda *_a: sources)
    monkeypatch.setattr(_review_publish, "replay_citation_plan_provenance", lambda *_a, **_k: authority)
    monkeypatch.setattr(_review_publish, "_snapshot_claim_scope", lambda _: "pipeline_validation")
    monkeypatch.setattr(_review_publish, "load_stage20_input_bundle", lambda *_a, **_k: stage20_inputs)
    monkeypatch.setattr(_review_publish, "verify_stage19_input_bundle_unchanged", lambda *_a: None)
    if mutate_during_fixpoint:
        monkeypatch.setattr(
            stage20_bundle_module,
            "load_stage19_input_bundle",
            lambda _run_dir: SimpleNamespace(
                citation_replay_inputs=sources.citation_replay_inputs,
                changed=True,
            ),
        )
    else:
        monkeypatch.setattr(
            _review_publish,
            "verify_stage20_input_bundle_unchanged",
            lambda *_a, **_k: None,
        )
    from researchclaw.pipeline.verified_registry import VerifiedRegistry

    monkeypatch.setattr(
        VerifiedRegistry,
        "from_run_dir",
        classmethod(lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy scan"))),
    )
    prompts: list[str] = []

    class _Prompts:
        def for_stage(self, _stage: str, **kwargs):
            prompts.append(kwargs["revised"])
            return SimpleNamespace(system="system", user="user", json_mode=True, max_tokens=100)

    monkeypatch.setattr(
        _review_publish,
        "_chat_with_prompt",
        lambda *_a, **_k: SimpleNamespace(
            content=llm_response
            or json.dumps(
                {
                    "score_1_to_10": 8,
                    "verdict": "proceed",
                    "strengths": ["bounded"],
                    "weaknesses": [],
                    "required_actions": [],
                }
            )
        ),
    )
    (run_dir / "stage-14_v99").mkdir()
    (run_dir / "stage-14_v99/experiment_summary.json").write_text(
        json.dumps({"metrics_summary": {"shadow": {"mean": 999}}}),
        encoding="utf-8",
    )
    (run_dir / "experiment_summary_best.json").write_text(
        json.dumps({"metrics_summary": {"shadow": {"mean": 777}}}),
        encoding="utf-8",
    )
    result = _review_publish._execute_quality_gate(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=object(),
        prompts=_Prompts(),
    )
    return result, stage_dir, "\n".join(prompts)


def test_stage20_uses_only_canonical_summary_and_preserves_decimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, stage_dir, prompt = _run_quality_gate(tmp_path, monkeypatch)

    assert result.status is StageStatus.DONE
    assert "canonical-condition" in prompt
    assert "999" not in prompt and "777" not in prompt
    flags = json.loads((stage_dir / "fabrication_flags.json").read_text())
    assert "0.123456789012345678" in flags["real_metric_values"]
    assert flags["canonical_experiment_evidence_sha256"] == "a" * 64
    report = json.loads((stage_dir / "quality_report.json").read_text())
    assert report["source_paper_sha256"] == hashlib.sha256(
        b"## Results\n\nCanonical metric [smith2024]."
    ).hexdigest()


def test_stage20_fixpoint_failure_removes_success_named_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, stage_dir, _prompt = _run_quality_gate(
        tmp_path, monkeypatch, mutate_during_fixpoint=True
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "fabrication_flags.json").exists()


def test_stage20_blocks_failed_condition_with_zero_metrics_despite_high_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = {
        "best_run": {"status": "failed", "metrics": {}},
        "metrics_summary": {},
        "condition_summaries": {
            "failed-condition": {"status": "failed", "metrics": {}}
        },
    }
    result, stage_dir, _prompt = _run_quality_gate(
        tmp_path, monkeypatch, summary_override=summary
    )

    assert result.status is StageStatus.FAILED
    flags = json.loads((stage_dir / "fabrication_flags.json").read_text())
    assert flags["experiment_failed"] is True
    assert flags["has_real_data"] is False
    assert flags["verified_values_count"] == 0


def test_stage20_registry_is_independent_of_global_decimal_precision() -> None:
    summary = {
        "best_run": {
            "status": "success",
            "metrics": {
                "A/0/f1": Decimal("0.123456789012345678"),
                "A/1/f1": Decimal("0.123456789012345679"),
            },
        },
        "condition_summaries": {},
        "metrics_summary": {},
    }
    original_precision = getcontext().prec
    outputs: list[tuple[str, ...]] = []
    try:
        for precision in (7, 28, 80):
            getcontext().prec = precision
            registry = _review_publish._build_stage20_registry(
                summary, metric_direction="maximize"
            )
            outputs.append(
                tuple(sorted(_review_publish.canonical_decimal(v) for v in registry.values))
            )
    finally:
        getcontext().prec = original_precision
    assert outputs[0] == outputs[1] == outputs[2]
    assert "0.1234567890123456785" in outputs[0]


def test_stage20_fixpoint_rediscovers_stage19_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _evidence()
    captured_sources = _stage19_sources()
    changed_sources = SimpleNamespace(
        paper=_bound("stage-17/paper_draft.md", "changed"),
        reviews=captured_sources.reviews,
    )
    bundle = Stage20InputBundle(
        stage19_inputs=captured_sources,
        revised_paper=_bound("stage-19/paper_revised.md", "paper"),
        publication_binding=_bound("stage-19/revision_evidence_binding.json", "binding"),
        publication_mode="legacy",
    )
    monkeypatch.setattr(
        stage20_bundle_module,
        "load_stage19_input_bundle",
        lambda _run_dir: changed_sources,
    )

    with pytest.raises(Stage20InputBundleError, match="Stage 04-18"):
        verify_stage20_input_bundle_unchanged(
            tmp_path,
            bundle,
            evidence=evidence,
            claim_scope="pipeline_validation",
        )


@pytest.mark.parametrize(
    "response",
    [
        '{"score_1_to_10":0,"score_1_to_10":8,"verdict":"proceed",'
        '"strengths":[],"weaknesses":[],"required_actions":[]}',
        '{"score_1_to_10":8,"verdict":"proceed","strengths":[],'
        '"weaknesses":[NaN],"required_actions":[]}',
    ],
)
def test_stage20_rejects_ambiguous_or_nonfinite_llm_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    result, stage_dir, _prompt = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_response=response,
        graceful_degradation=True,
    )

    assert result.status is StageStatus.FAILED
    assert "quality response is invalid" in (result.error or "")
    assert not (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "fabrication_flags.json").exists()


@pytest.mark.parametrize(
    "value",
    [True, float("nan"), float("inf"), float("-inf"), -1, 11, "invalid"],
)
def test_stage20_quality_score_is_fail_closed(value: object) -> None:
    assert _review_publish._normalize_quality_score(value) == 0.0


def test_stage20_consumer_has_no_legacy_experiment_selector() -> None:
    source = inspect.getsource(_review_publish._execute_quality_gate)
    assert "_read_prior_artifact" not in source
    assert "VerifiedRegistry.from_run_dir" not in source
    assert "stage-14*" not in source
    assert "experiment_summary_best.json" not in source
    assert "_get_evolution_overlay" not in source


def test_stage19_22_capability_remains_blocked() -> None:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        CANONICAL_EVIDENCE_CAPABILITIES,
    )

    assert CANONICAL_EVIDENCE_CAPABILITIES["stage19_22_consumers"] == 0
