from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal, getcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.llm.client import LLMClient, LLMConfig
from researchclaw.pipeline import bound_output_namespace as output_namespace_module
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
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
from researchclaw.pipeline.stage20_publication import (
    reconstruct_stage20_fabrication_state,
)
from researchclaw.pipeline import stage20_input_bundle as stage20_bundle_module
from researchclaw.pipeline.stage_impls import _review_publish
from researchclaw.pipeline.stages import StageStatus
from researchclaw.prompts import PromptManager


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
    llm_responses: list[object] | None = None,
    llm_call_log: list[dict[str, object]] | None = None,
    graceful_degradation: bool = False,
    replace_parent_during_chat: tuple[Path, Path] | None = None,
    replace_run_during_chat: tuple[Path, Path] | None = None,
    diagnostic_collision_target: Path | None = None,
    canonical_fact_sheet: object | None = None,
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
    monkeypatch.setattr(
        _review_publish,
        "build_canonical_fact_sheet",
        lambda _evidence: canonical_fact_sheet,
    )
    monkeypatch.setattr(_review_publish, "parse_config_snapshot_text", lambda *_a, **_k: config)
    monkeypatch.setattr(_review_publish, "semantic_config_sha256", lambda _: "same")
    monkeypatch.setattr(_review_publish, "_load_bound_stage19_inputs", lambda *_a: sources)
    monkeypatch.setattr(
        _review_publish,
        "_replay_citation_plan_provenance_from_evidence",
        lambda *_a, **_k: authority,
    )
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

    responses = list(llm_responses or [])
    def chat(*_args, **kwargs):
        if llm_call_log is not None:
            llm_call_log.append(dict(kwargs))
        if replace_parent_during_chat is not None:
            detached, outside = replace_parent_during_chat
            stage_dir.rename(detached)
            stage_dir.symlink_to(outside, target_is_directory=True)
        if replace_run_during_chat is not None:
            detached_run, replacement = replace_run_during_chat
            run_dir.rename(detached_run)
            run_dir.symlink_to(replacement, target_is_directory=True)
        diagnostic_path = stage_dir / "quality_gate_llm_diagnostics.json"
        if diagnostic_collision_target is not None and not diagnostic_path.exists():
            diagnostic_path.symlink_to(diagnostic_collision_target)
        response = responses.pop(0) if responses else llm_response
        if response is not None and not isinstance(response, str):
            return response
        return SimpleNamespace(
            content=response
            or json.dumps(
                {
                    "score_1_to_10": 8,
                    "verdict": "proceed",
                    "strengths": ["bounded"],
                    "weaknesses": [],
                    "required_actions": [],
                }
            ),
            model="test-model",
            finish_reason="stop",
            truncated=False,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )

    monkeypatch.setattr(_review_publish, "_chat_with_prompt", chat)
    monkeypatch.setattr(
        _review_publish,
        "_constrain_stage20_llm",
        lambda _value: SimpleNamespace(config=SimpleNamespace(max_tokens=4096)),
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
    manifest = json.loads((stage_dir / "quality_gate_manifest.json").read_text())
    assert manifest["outcome"] == "passed"


def test_stage20_prompt_includes_complete_cfs_contract_without_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfs = {
        "claim_scope": "pipeline_validation",
        "seeds": (0, 1, 2),
        "conditions": (
            {"id": "proposed", "role": "primary"},
            {"id": "baseline", "role": "comparator"},
        ),
    }
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda value, *, include_projection: (
            "COMPLETE_CFS_WITH_PROJECTION" if include_projection else "COMPLETE_CFS"
        ),
    )

    result, stage_dir, prompt = _run_quality_gate(
        tmp_path, monkeypatch, canonical_fact_sheet=cfs
    )

    assert result.status is StageStatus.DONE
    assert "COMPLETE_CFS_WITH_PROJECTION" in prompt
    assert "out_of_scope" in prompt
    assert "not alone justify reject" in prompt
    report = json.loads((stage_dir / "quality_report.json").read_text())
    manifest = json.loads((stage_dir / "quality_gate_manifest.json").read_text())
    assert "canonical_fact_sheet" not in report
    assert "canonical_fact_sheet_sha256" not in report
    assert "canonical_fact_sheet" not in manifest
    assert "canonical_fact_sheet_sha256" not in manifest


def test_stage20_repairs_out_of_scope_only_reject_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reject = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "reject",
                "strengths": [],
                "weaknesses": ["Only three seeds were used."],
                "required_actions": ["Re-run with ten seeds."],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    proceed = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "proceed",
                "strengths": ["Contract-bounded."],
                "weaknesses": [],
                "required_actions": [],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    calls: list[dict[str, object]] = []
    cfs = {
        "seeds": (0, 1, 2),
        "conditions": (
            {"id": "proposed", "role": "primary"},
            {"id": "baseline", "role": "comparator"},
        ),
        "variant_ids": ("c1355_v1",),
        "metric_keys": ("auprc",),
    }
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda _cfs, *, include_projection: "COMPLETE_CFS_WITH_PROJECTION",
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[reject, proceed],
        llm_call_log=calls,
        canonical_fact_sheet=cfs,
    )

    assert result.status is StageStatus.DONE
    assert len(calls) == 2
    diagnostic = json.loads(
        (stage_dir / "quality_gate_llm_diagnostics.json").read_text()
    )
    assert diagnostic[0]["error_category"] == "out_of_scope_only_reject"


def test_stage20_second_out_of_scope_only_reject_fails_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reject = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "reject",
                "strengths": [],
                "weaknesses": ["Only three seeds were used."],
                "required_actions": ["Re-run with 10 seeds."],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    calls: list[dict[str, object]] = []
    cfs = {
        "seeds": (0, 1, 2),
        "conditions": ({"id": "proposed", "role": "primary"},),
        "variant_ids": ("c1355_v1",),
        "metric_keys": ("auprc",),
    }
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda _cfs, *, include_projection: "COMPLETE_CFS_WITH_PROJECTION",
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[reject, reject],
        llm_call_log=calls,
        canonical_fact_sheet=cfs,
    )

    assert result.status is StageStatus.FAILED
    assert len(calls) == 2
    assert not (stage_dir / "quality_gate_manifest.json").exists()
    assert "out_of_scope_only_reject" in (result.error or "")


def test_stage20_mixed_real_defect_preserves_reject_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reject = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "reject",
                "strengths": [],
                "weaknesses": [
                    "Only three seeds were used.",
                    "The manuscript contradicts the observed AUPRC.",
                ],
                "required_actions": ["Correct the factual contradiction."],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    calls: list[dict[str, object]] = []
    cfs = {
        "seeds": (0, 1, 2),
        "conditions": ({"id": "proposed", "role": "primary"},),
        "variant_ids": ("c1355_v1",),
        "metric_keys": ("auprc",),
    }
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda _cfs, *, include_projection: "COMPLETE_CFS_WITH_PROJECTION",
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[reject],
        llm_call_log=calls,
        canonical_fact_sheet=cfs,
    )

    assert result.status is StageStatus.FAILED
    assert len(calls) == 1
    assert (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "quality_gate_manifest.json").exists()


@pytest.mark.parametrize(
    "weakness",
    (
        "Re-run with 10 seeds with AUPRC score 1,",
        "Re-run with 10 seeds with AUPRC score 99.9%,",
        "Re-run with 10 seeds with AUPRC score 1e-3,",
        "Re-run with 10 seeds with AUPRC score one,",
        "Re-run with 10 seeds with AUPRC score ten,",
        "Re-run with 10 seeds because the manuscript fabricates the observed AUPRC.",
    ),
    ids=(
        "integer",
        "percentage",
        "scientific",
        "word-one",
        "word-ten",
        "fabrication",
    ),
)
def test_stage20_numeric_metric_conflict_preserves_reject_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weakness: str
) -> None:
    reject = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 2,
                "verdict": "reject",
                "strengths": [],
                "weaknesses": [weakness],
                "required_actions": [],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    proceed = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "proceed",
                "strengths": ["bounded"],
                "weaknesses": [],
                "required_actions": [],
            }
        ),
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    calls: list[dict[str, object]] = []
    cfs = {
        "seeds": (0, 1, 2),
        "conditions": ({"id": "proposed", "role": "primary"},),
        "variant_ids": ("c1355_v1",),
        "metric_keys": ("auprc",),
    }
    monkeypatch.setattr(
        _review_publish,
        "render_complete_fact_sheet_text",
        lambda _cfs, *, include_projection: "COMPLETE_CFS_WITH_PROJECTION",
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[reject, proceed],
        llm_call_log=calls,
        canonical_fact_sheet=cfs,
    )

    assert result.status is StageStatus.FAILED
    assert len(calls) == 1
    assert (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "quality_gate_manifest.json").exists()


def test_stage20_fixpoint_failure_removes_success_named_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, stage_dir, _prompt = _run_quality_gate(
        tmp_path, monkeypatch, mutate_during_fixpoint=True
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "fabrication_flags.json").exists()
    assert not (stage_dir / "quality_gate_manifest.json").exists()


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
    assert not (stage_dir / "quality_gate_manifest.json").exists()


def test_stage20_failed_and_degraded_generations_have_distinct_commit_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = json.dumps(
        {
            "score_1_to_10": 2,
            "verdict": "revise",
            "strengths": ["bounded"],
            "weaknesses": ["weak"],
            "required_actions": ["repair"],
        }
    )
    failed, failed_dir, _ = _run_quality_gate(
        tmp_path / "failed",
        monkeypatch,
        llm_response=response,
        graceful_degradation=False,
    )
    assert failed.status is StageStatus.FAILED
    assert not (failed_dir / "quality_gate_manifest.json").exists()

    degraded, degraded_dir, _ = _run_quality_gate(
        tmp_path / "degraded",
        monkeypatch,
        llm_response=response,
        graceful_degradation=True,
    )
    assert degraded.status is StageStatus.DONE
    assert degraded.decision == "degraded"
    manifest = json.loads((degraded_dir / "quality_gate_manifest.json").read_text())
    assert manifest["outcome"] == "degraded"


def test_stage20_rejects_parent_symlink_without_touching_external_manifest(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external_manifest = outside / "quality_gate_manifest.json"
    external_manifest.write_text("keep", encoding="utf-8")
    stage_dir = run_dir / "stage-20"
    stage_dir.symlink_to(outside, target_is_directory=True)

    result = _review_publish._execute_quality_gate(
        stage_dir, run_dir, SimpleNamespace(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert external_manifest.read_text(encoding="utf-8") == "keep"


def test_stage20_parent_replacement_during_llm_cannot_touch_external_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    external = {
        name: outside / name
        for name in (
            "quality_report.json",
            "fabrication_flags.json",
            "quality_gate_manifest.json",
        )
    }
    for path in external.values():
        path.write_text("keep", encoding="utf-8")

    result, _stage_dir, _prompt = _run_quality_gate(
        tmp_path,
        monkeypatch,
        replace_parent_during_chat=(tmp_path / "detached-stage-20", outside),
    )

    assert result.status is StageStatus.FAILED
    assert all(path.read_text(encoding="utf-8") == "keep" for path in external.values())
    detached = tmp_path / "detached-stage-20"
    assert not (detached / "quality_report.json").exists()
    assert not (detached / "fabrication_flags.json").exists()
    assert not (detached / "quality_gate_manifest.json").exists()


def test_bound_output_write_preserves_original_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    output = BoundOutputNamespace.open(run_dir, stage_dir, "stage-20")
    original_invalidate = output.invalidate
    invalidation_calls = 0

    def fail_cleanup(names: tuple[str, ...]) -> None:
        nonlocal invalidation_calls
        invalidation_calls += 1
        if invalidation_calls == 1:
            original_invalidate(names)
            return
        raise OSError("cleanup failed")

    monkeypatch.setattr(output, "invalidate", fail_cleanup)
    monkeypatch.setattr(
        output_namespace_module.os,
        "write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    try:
        with pytest.raises(OSError, match="write failed") as raised:
            output.write_text_atomic("quality_report.json", "payload")
    finally:
        output.close()

    assert any(
        "temporary output cleanup also failed: cleanup failed" in note
        for note in getattr(raised.value, "__notes__", ())
    )


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
            state = reconstruct_stage20_fabrication_state(
                _evidence(summary),
                SimpleNamespace(experiment=SimpleNamespace(metric_direction="maximize")),
            )
            outputs.append(state.real_metric_values)
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


def test_stage20_repairs_invalid_verdict_once_with_bounded_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    invalid = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "APPROVE",
                "strengths": ["bounded"],
                "weaknesses": [],
                "required_actions": [],
            }
        ),
        model="deepseek-v4-flash",
        finish_reason="stop",
        truncated=False,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
    )
    valid = SimpleNamespace(
        content=json.dumps(
            {
                "score_1_to_10": 8,
                "verdict": "proceed",
                "strengths": ["bounded"],
                "weaknesses": [],
                "required_actions": [],
            }
        ),
        model="deepseek-v4-flash",
        finish_reason="stop",
        truncated=False,
        prompt_tokens=11,
        completion_tokens=21,
        total_tokens=32,
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[invalid, valid],
        llm_call_log=calls,
    )

    assert result.status is StageStatus.DONE
    assert len(calls) == 2
    diagnostics = json.loads(
        (stage_dir / "quality_gate_llm_diagnostics.json").read_text()
    )
    assert [row["error_category"] for row in diagnostics] == [
        "invalid_verdict",
        "success",
    ]
    expected_fields = {
        "schema_version",
        "call_index",
        "call_role",
        "model",
        "max_tokens",
        "finish_reason",
        "truncated",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "content_length",
        "error_category",
        "repair_attempted",
    }
    assert all(set(row) == expected_fields for row in diagnostics)
    serialized = json.dumps(diagnostics)
    assert "APPROVE" not in serialized
    assert "bounded" not in serialized
    assert "quality_gate_llm_diagnostics.json" not in result.artifacts


def test_stage20_second_invalid_verdict_fails_without_third_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    invalid = SimpleNamespace(
        content='{"score_1_to_10":8,"verdict":"APPROVE",'
        '"strengths":[],"weaknesses":[],"required_actions":[]}',
        model="deepseek-v4-flash",
        finish_reason="stop",
        truncated=False,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[invalid, invalid],
        llm_call_log=calls,
    )

    assert result.status is StageStatus.FAILED
    assert len(calls) == 2
    assert not (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "quality_gate_manifest.json").exists()
    diagnostics = json.loads(
        (stage_dir / "quality_gate_llm_diagnostics.json").read_text()
    )
    assert [row["call_role"] for row in diagnostics] == ["initial", "repair"]


def test_stage20_high_score_reject_cannot_publish_passed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = json.dumps(
        {
            "score_1_to_10": 8,
            "verdict": "reject",
            "strengths": [],
            "weaknesses": ["fatal"],
            "required_actions": ["reject"],
        }
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path, monkeypatch, llm_response=response
    )

    assert result.status is StageStatus.FAILED
    assert (stage_dir / "quality_report.json").exists()
    assert not (stage_dir / "quality_gate_manifest.json").exists()


@pytest.mark.parametrize(
    ("score", "verdict"),
    [(2, "proceed"), (8, "revise")],
)
def test_stage20_contradictory_score_and_verdict_cannot_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: int,
    verdict: str,
) -> None:
    response = json.dumps(
        {
            "score_1_to_10": score,
            "verdict": verdict,
            "strengths": [],
            "weaknesses": ["inconsistent"],
            "required_actions": ["repair"],
        }
    )

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_response=response,
        graceful_degradation=True,
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "quality_gate_manifest.json").exists()


@pytest.mark.parametrize(
    ("initial", "expected_category"),
    [
        (
            SimpleNamespace(
                content="",
                model="test",
                finish_reason="stop",
                truncated=False,
            ),
            "empty_response",
        ),
        (
            SimpleNamespace(
                content='{"score_1_to_10":',
                model="test",
                finish_reason="length",
                truncated=True,
            ),
            "truncated_response",
        ),
        (
            SimpleNamespace(
                content="not-json",
                model="test",
                finish_reason="stop",
                truncated=False,
            ),
            "malformed_json",
        ),
    ],
)
def test_stage20_repairs_each_response_class_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: SimpleNamespace,
    expected_category: str,
) -> None:
    valid = SimpleNamespace(
        content='{"score_1_to_10":8,"verdict":"proceed",'
        '"strengths":[],"weaknesses":[],"required_actions":[]}',
        model="test",
        finish_reason="stop",
        truncated=False,
    )
    calls: list[dict[str, object]] = []

    result, stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[initial, valid],
        llm_call_log=calls,
    )

    assert result.status is StageStatus.DONE
    assert len(calls) == 2
    diagnostics = json.loads(
        (stage_dir / "quality_gate_llm_diagnostics.json").read_text()
    )
    assert diagnostics[0]["error_category"] == expected_category


def test_stage20_constrained_client_disables_all_hidden_fallbacks() -> None:
    source = LLMClient(
        LLMConfig(
            base_url="https://example.invalid",
            api_key="secret",
            primary_model="primary",
            fallback_models=["fallback"],
            max_retries=4,
            fallback_url="https://fallback.invalid",
            fallback_api_key="fallback-secret",
        )
    )

    constrained = _review_publish._constrain_stage20_llm(source)

    assert constrained is not source
    assert constrained.config.max_retries == 1
    assert constrained.config.fallback_models == []
    assert constrained.config.fallback_url == ""
    assert constrained.config.fallback_api_key == ""
    assert constrained._model_chain == ["primary"]


def test_stage20_constrained_client_makes_one_outbound_attempt() -> None:
    source = LLMClient(
        LLMConfig(
            base_url="https://example.invalid",
            api_key="secret",
            primary_model="primary",
            fallback_models=["fallback"],
            max_retries=4,
            fallback_url="https://fallback.invalid",
        )
    )
    calls = 0

    def fail_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("injected timeout")

    source._raw_call = fail_once  # type: ignore[method-assign]
    constrained = _review_publish._constrain_stage20_llm(source)

    with pytest.raises(RuntimeError, match="All models failed"):
        constrained.chat([{"role": "user", "content": "quality"}])
    assert calls == 1


@pytest.mark.parametrize("domain", ["ml", "hep_ph"])
def test_stage20_repair_prompt_preserves_exact_verdict_contract(domain: str) -> None:
    prompt = PromptManager(domain=domain).for_stage(
        "quality_gate_repair",
        error_category="invalid_verdict",
        quality_threshold="6",
        revised="immutable-paper",
    )

    assert "proceed, revise, reject" in prompt.user
    assert "five fields and no others" in prompt.user
    assert "immutable-paper" in prompt.user


def test_stage20_degraded_parent_replacement_does_not_touch_external_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    external_signal = outside / "degradation_signal.json"
    external_signal.write_text("keep", encoding="utf-8")
    response = json.dumps(
        {
            "score_1_to_10": 2,
            "verdict": "revise",
            "strengths": [],
            "weaknesses": ["weak"],
            "required_actions": ["repair"],
        }
    )

    result, _stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_response=response,
        graceful_degradation=True,
        replace_run_during_chat=(tmp_path / "detached-run", outside),
    )

    assert result.status is StageStatus.FAILED
    assert external_signal.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "detached-run" / "degradation_signal.json").exists()


def test_stage20_diagnostic_directory_collision_fails_before_llm(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    (stage_dir / "quality_gate_llm_diagnostics.json").mkdir(parents=True)

    result = _review_publish._execute_quality_gate(
        stage_dir, run_dir, SimpleNamespace(), AdapterBundle(), llm=object()
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert "output namespace is unsafe" in (result.error or "")


def test_stage20_degradation_signal_symlink_collision_is_external_zero_write(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (run_dir / "degradation_signal.json").symlink_to(outside)

    result = _review_publish._execute_quality_gate(
        stage_dir, run_dir, SimpleNamespace(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_stage20_entry_removes_stale_degradation_signal_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    stale = run_dir / "degradation_signal.json.tmp"
    stale.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        _review_publish,
        "load_canonical_experiment_evidence",
        lambda _run_dir: (_ for _ in ()).throw(
            _review_publish.CanonicalExperimentEvidenceError("missing")
        ),
    )

    result = _review_publish._execute_quality_gate(
        stage_dir, run_dir, SimpleNamespace(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert not stale.exists()


def test_stage20_diagnostic_symlink_collision_fails_without_external_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    invalid = SimpleNamespace(
        content='{"score_1_to_10":8,"verdict":"APPROVE",'
        '"strengths":[],"weaknesses":[],"required_actions":[]}',
        model="test",
        finish_reason="stop",
        truncated=False,
    )

    result, _stage_dir, _ = _run_quality_gate(
        tmp_path,
        monkeypatch,
        llm_responses=[invalid, invalid],
        diagnostic_collision_target=outside,
    )

    assert result.status is StageStatus.FAILED
    assert "diagnostic publication also failed" in (result.error or "")
    assert outside.read_text(encoding="utf-8") == "keep"


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


def test_stage19_22_capability_is_activated_after_complete_migration() -> None:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        CAPABILITY_SCHEMA_VERSION,
        CANONICAL_EVIDENCE_CAPABILITIES,
    )

    assert (
        CANONICAL_EVIDENCE_CAPABILITIES["stage19_22_consumers"]
        == CAPABILITY_SCHEMA_VERSION
    )
