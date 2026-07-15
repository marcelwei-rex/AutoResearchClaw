from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.pipeline.canonical_experiment_evidence import canonical_decimal
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage20_input_bundle import Stage20InputBundle
from researchclaw.pipeline.stage20_publication import (
    reconstruct_stage20_fabrication_state,
)
from researchclaw.pipeline.stage21_input_bundle import (
    Stage21InputBundle,
    Stage21InputBundleError,
    load_stage21_input_bundle,
    parse_stage21_bundle_index,
    verify_stage21_input_bundle_unchanged,
)
from researchclaw.pipeline.stage_impls import _review_publish
from researchclaw.pipeline.stages import StageStatus


def _bound(path: str, text: str) -> BoundArtifact:
    content = text.encode("utf-8")
    return BoundArtifact(path, hashlib.sha256(content).hexdigest(), content)


def _config(*, graceful: bool = False, threshold: float = 6.0) -> SimpleNamespace:
    return SimpleNamespace(
        research=SimpleNamespace(
            topic="canonical topic",
            quality_threshold=threshold,
            graceful_degradation=graceful,
        ),
        experiment=SimpleNamespace(metric_direction="maximize"),
    )


def _inputs() -> tuple[SimpleNamespace, Stage20InputBundle, SimpleNamespace]:
    config = _config()
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
        analysis_text="canonical analysis only",
        run_config_bytes=b"config",
        summary=MappingProxyType(
            {
                "best_run": MappingProxyType(
                    {
                        "status": "success",
                        "metrics": MappingProxyType({"primary_metric": 0.125}),
                    }
                ),
                "condition_summaries": MappingProxyType(
                    {
                        "baseline": MappingProxyType(
                            {"metrics": MappingProxyType({"f1": 0.125})}
                        )
                    }
                ),
                "metrics_summary": MappingProxyType({}),
            }
        ),
    )
    stage20 = Stage20InputBundle(
        stage19_inputs=SimpleNamespace(artifacts=()),
        revised_paper=_bound("stage-19/paper_revised.md", "canonical revised paper"),
        publication_binding=_bound(
            "stage-19/revision_evidence_binding.json", "binding"
        ),
        publication_mode="legacy",
    )
    return evidence, stage20, config


def _write_stage20_outputs(
    run_dir: Path,
    evidence: SimpleNamespace,
    stage20: Stage20InputBundle,
    config: SimpleNamespace,
    *,
    outcome: str = "passed",
    score: float = 8,
) -> None:
    stage_dir = run_dir / "stage-20"
    stage_dir.mkdir(parents=True)
    binding = {
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage20.revised_paper.path,
        "source_paper_sha256": stage20.revised_paper.sha256,
        "stage19_publication_mode": stage20.publication_mode,
        "stage19_publication_binding_path": stage20.publication_binding.path,
        "stage19_publication_binding_sha256": stage20.publication_binding.sha256,
    }
    quality = {
        "score_1_to_10": score,
        "verdict": "proceed",
        "strengths": ["bounded"],
        "weaknesses": [],
        "required_actions": [],
        "generated": "2026-07-14T00:00:00+00:00",
        **binding,
    }
    state = reconstruct_stage20_fabrication_state(evidence, config)
    flags = {
        "schema_version": 2,
        **binding,
        "quality_score": score,
        **state.to_dict(),
    }
    quality_text = json.dumps(quality)
    flags_text = json.dumps(flags)
    (stage_dir / "quality_report.json").write_text(quality_text, encoding="utf-8")
    (stage_dir / "fabrication_flags.json").write_text(flags_text, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "outcome": outcome,
        **binding,
        "quality_report_path": "stage-20/quality_report.json",
        "quality_report_sha256": hashlib.sha256(quality_text.encode()).hexdigest(),
        "fabrication_flags_path": "stage-20/fabrication_flags.json",
        "fabrication_flags_sha256": hashlib.sha256(flags_text.encode()).hexdigest(),
        "quality_threshold": canonical_decimal(
            Decimal(str(config.research.quality_threshold))
        ),
        "graceful_degradation": config.research.graceful_degradation,
        "quality_score": canonical_decimal(Decimal(str(score))),
        "generated": "2026-07-14T00:00:00+00:00",
    }
    (stage_dir / "quality_gate_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_stage21_bundle_replays_stage20_bindings_and_fixpoint(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)

    bundle = load_stage21_input_bundle(
        run_dir, stage20_inputs=stage20, evidence=evidence, canonical_config=config
    )
    assert bundle.quality_report.path == "stage-20/quality_report.json"
    assert bundle.fabrication_flags.path == "stage-20/fabrication_flags.json"

    quality_path = run_dir / "stage-20/quality_report.json"
    quality = json.loads(quality_path.read_text())
    quality["source_paper_sha256"] = "0" * 64
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    with pytest.raises(Stage21InputBundleError, match="source_paper_sha256"):
        verify_stage21_input_bundle_unchanged(
            run_dir,
            bundle,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


@pytest.mark.parametrize(
    ("filename", "replacement", "message"),
    [
        (
            "quality_report.json",
            '{"score_1_to_10":8,"score_1_to_10":9}',
            "duplicate",
        ),
        ("quality_report.json", '{"score_1_to_10":NaN}', "nonfinite"),
        ("fabrication_flags.json", "{}", "fields mismatch"),
    ],
)
def test_stage21_bundle_rejects_malformed_stage20_outputs(
    tmp_path: Path, filename: str, replacement: str, message: str
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)
    (run_dir / "stage-20" / filename).write_text(replacement, encoding="utf-8")

    with pytest.raises(Stage21InputBundleError, match=message):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_bundle_rejects_symlink_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)
    quality_path = run_dir / "stage-20/quality_report.json"
    quality_path.unlink()
    quality_path.symlink_to(tmp_path / "outside.json")

    with pytest.raises(Stage21InputBundleError, match="cannot open"):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_bundle_rejects_cross_report_score_mismatch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)
    flags_path = run_dir / "stage-20/fabrication_flags.json"
    flags = json.loads(flags_path.read_text())
    flags["quality_score"] = 7
    flags_path.write_text(json.dumps(flags), encoding="utf-8")

    with pytest.raises(Stage21InputBundleError, match="score mismatch"):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_requires_stage20_commit_point(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)
    (run_dir / "stage-20/quality_gate_manifest.json").unlink()

    with pytest.raises(Stage21InputBundleError, match="cannot open"):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_recomputes_fabrication_state_from_canonical_evidence(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    _write_stage20_outputs(run_dir, evidence, stage20, config)
    flags_path = run_dir / "stage-20/fabrication_flags.json"
    flags = json.loads(flags_path.read_text())
    flags.update(
        {
            "verified_values_count": 999,
            "real_metric_values": ["9"],
            "verified_conditions": ["forged"],
            "has_real_data": True,
            "experiment_failed": False,
            "fabrication_suspected": False,
        }
    )
    flags_text = json.dumps(flags)
    flags_path.write_text(flags_text, encoding="utf-8")
    manifest_path = run_dir / "stage-20/quality_gate_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["fabrication_flags_sha256"] = hashlib.sha256(
        flags_text.encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Stage21InputBundleError, match="do not replay"):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_rejects_zero_data_even_with_forged_pass_manifest(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, config = _inputs()
    evidence.summary = MappingProxyType(
        {
            "best_run": MappingProxyType({"status": "failed", "metrics": MappingProxyType({})}),
            "condition_summaries": MappingProxyType({}),
            "metrics_summary": MappingProxyType({}),
        }
    )
    _write_stage20_outputs(run_dir, evidence, stage20, config)

    with pytest.raises(Stage21InputBundleError, match="zero-data"):
        load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20,
            evidence=evidence,
            canonical_config=config,
        )


def test_stage21_accepts_only_explicit_legal_degraded_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence, stage20, _ = _inputs()
    config = _config(graceful=True)
    _write_stage20_outputs(
        run_dir, evidence, stage20, config, outcome="degraded", score=2
    )

    bundle = load_stage21_input_bundle(
        run_dir,
        stage20_inputs=stage20,
        evidence=evidence,
        canonical_config=config,
    )
    assert bundle.quality_gate_outcome == "degraded"


def test_stage21_uses_only_captured_authority_and_hash_indexes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    evidence, stage20, _ = _inputs()
    config = _config(graceful=True)
    artifact = _bound("stage-17/paper_draft.md", "draft")
    stage19 = SimpleNamespace(artifacts=(artifact,))
    stage20 = Stage20InputBundle(
        stage19_inputs=stage19,
        revised_paper=stage20.revised_paper,
        publication_binding=stage20.publication_binding,
        publication_mode=stage20.publication_mode,
    )
    stage21 = Stage21InputBundle(
        stage20_inputs=stage20,
        quality_report=_bound("stage-20/quality_report.json", "quality"),
        fabrication_flags=_bound("stage-20/fabrication_flags.json", "flags"),
        quality_gate_manifest=_bound(
            "stage-20/quality_gate_manifest.json", "manifest"
        ),
        quality_gate_outcome="degraded",
    )
    prompt_values: dict[str, object] = {}

    monkeypatch.setattr(
        _review_publish, "load_canonical_experiment_evidence", lambda _run: evidence
    )
    monkeypatch.setattr(
        _review_publish, "parse_config_snapshot_text", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(_review_publish, "semantic_config_sha256", lambda _config: "same")
    monkeypatch.setattr(
        _review_publish, "_load_bound_stage19_inputs", lambda *_args: stage19
    )
    monkeypatch.setattr(
        _review_publish, "_snapshot_claim_scope", lambda _evidence: "pipeline_validation"
    )
    monkeypatch.setattr(
        _review_publish, "load_stage20_input_bundle", lambda *_args, **_kwargs: stage20
    )
    monkeypatch.setattr(
        _review_publish, "load_stage21_input_bundle", lambda *_args, **_kwargs: stage21
    )
    monkeypatch.setattr(
        _review_publish, "_verify_stage21_sources", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        _review_publish,
        "_read_prior_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy read")),
    )
    monkeypatch.setattr(
        _review_publish,
        "_read_best_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy analysis")),
    )

    class Prompts:
        def for_stage(self, _stage: str, **kwargs):
            prompt_values.update(kwargs)
            return SimpleNamespace(system="system", user="user", json_mode=False, max_tokens=10)

    monkeypatch.setattr(
        _review_publish,
        "_chat_with_prompt",
        lambda *_args, **_kwargs: SimpleNamespace(content="# Archive\n"),
    )
    shadow = run_dir / "stage-99"
    shadow.mkdir()
    (shadow / "poison.txt").write_text("poison", encoding="utf-8")

    result = _review_publish._execute_knowledge_archive(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=object(),
        prompts=Prompts(),
    )

    assert result.status is StageStatus.DONE
    assert prompt_values["analysis"] == evidence.analysis_text
    assert prompt_values["revised"] == stage20.revised_paper.text()
    assert prompt_values["evolution_overlay"] is None
    assert str(prompt_values["decision"]).startswith("DEGRADED")
    index = json.loads((stage_dir / "bundle_index.json").read_text())
    indexed = {item["path"]: item["sha256"] for item in index["artifacts"]}
    assert "stage-99/poison.txt" not in indexed
    assert indexed["stage-21/archive.md"] == hashlib.sha256(
        (stage_dir / "archive.md").read_bytes()
    ).hexdigest()
    assert index["artifact_count"] == len(indexed)
    unsafe = dict(index)
    unsafe["artifacts"] = [dict(item) for item in index["artifacts"]]
    unsafe["artifacts"][0]["path"] = "../outside"
    with pytest.raises(Stage21InputBundleError, match="artifact path"):
        parse_stage21_bundle_index(json.dumps(unsafe))


def test_stage21_fixpoint_failure_removes_success_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    evidence, stage20, config = _inputs()
    stage19 = SimpleNamespace(artifacts=())
    stage21 = Stage21InputBundle(
        stage20_inputs=stage20,
        quality_report=_bound("stage-20/quality_report.json", "quality"),
        fabrication_flags=_bound("stage-20/fabrication_flags.json", "flags"),
        quality_gate_manifest=_bound(
            "stage-20/quality_gate_manifest.json", "manifest"
        ),
        quality_gate_outcome="passed",
    )
    calls = 0

    monkeypatch.setattr(
        _review_publish, "load_canonical_experiment_evidence", lambda _run: evidence
    )
    monkeypatch.setattr(
        _review_publish, "parse_config_snapshot_text", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(_review_publish, "semantic_config_sha256", lambda _config: "same")
    monkeypatch.setattr(
        _review_publish, "_load_bound_stage19_inputs", lambda *_args: stage19
    )
    monkeypatch.setattr(
        _review_publish, "_snapshot_claim_scope", lambda _evidence: "pipeline_validation"
    )
    monkeypatch.setattr(
        _review_publish, "load_stage20_input_bundle", lambda *_args, **_kwargs: stage20
    )
    monkeypatch.setattr(
        _review_publish, "load_stage21_input_bundle", lambda *_args, **_kwargs: stage21
    )

    def fail_second(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Stage21InputBundleError("changed")

    monkeypatch.setattr(_review_publish, "_verify_stage21_sources", fail_second)

    result = _review_publish._execute_knowledge_archive(
        stage_dir, run_dir, config, AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "archive.md").exists()
    assert not (stage_dir / "bundle_index.json").exists()


def test_stage21_rejects_parent_symlink_without_touching_external_files(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external_archive = outside / "archive.md"
    external_archive.write_text("keep", encoding="utf-8")
    stage_dir = run_dir / "stage-21"
    stage_dir.symlink_to(outside, target_is_directory=True)

    result = _review_publish._execute_knowledge_archive(
        stage_dir, run_dir, _config(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert external_archive.read_text(encoding="utf-8") == "keep"


def test_stage21_parent_replacement_during_llm_cannot_touch_external_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    detached = tmp_path / "detached-stage-21"
    outside = tmp_path / "outside"
    outside.mkdir()
    external_archive = outside / "archive.md"
    external_index = outside / "bundle_index.json"
    external_archive.write_text("keep-archive", encoding="utf-8")
    external_index.write_text("keep-index", encoding="utf-8")
    evidence, stage20, config = _inputs()
    stage19 = SimpleNamespace(artifacts=())
    stage21 = Stage21InputBundle(
        stage20_inputs=stage20,
        quality_report=_bound("stage-20/quality_report.json", "quality"),
        fabrication_flags=_bound("stage-20/fabrication_flags.json", "flags"),
        quality_gate_manifest=_bound(
            "stage-20/quality_gate_manifest.json", "manifest"
        ),
        quality_gate_outcome="passed",
    )
    monkeypatch.setattr(
        _review_publish, "load_canonical_experiment_evidence", lambda _run: evidence
    )
    monkeypatch.setattr(
        _review_publish, "parse_config_snapshot_text", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(_review_publish, "semantic_config_sha256", lambda _config: "same")
    monkeypatch.setattr(
        _review_publish, "_load_bound_stage19_inputs", lambda *_args: stage19
    )
    monkeypatch.setattr(
        _review_publish, "_snapshot_claim_scope", lambda _evidence: "pipeline_validation"
    )
    monkeypatch.setattr(
        _review_publish, "load_stage20_input_bundle", lambda *_args, **_kwargs: stage20
    )
    monkeypatch.setattr(
        _review_publish, "load_stage21_input_bundle", lambda *_args, **_kwargs: stage21
    )
    monkeypatch.setattr(
        _review_publish, "_verify_stage21_sources", lambda *_args, **_kwargs: None
    )

    class Prompts:
        def for_stage(self, _stage: str, **_kwargs):
            return SimpleNamespace(
                system="system", user="user", json_mode=False, max_tokens=10
            )

    def replace_parent(*_args, **_kwargs):
        stage_dir.rename(detached)
        stage_dir.symlink_to(outside, target_is_directory=True)
        return SimpleNamespace(content="# Archive\n")

    monkeypatch.setattr(_review_publish, "_chat_with_prompt", replace_parent)

    result = _review_publish._execute_knowledge_archive(
        stage_dir,
        run_dir,
        config,
        AdapterBundle(),
        llm=object(),
        prompts=Prompts(),
    )

    assert result.status is StageStatus.FAILED
    assert external_archive.read_text(encoding="utf-8") == "keep-archive"
    assert external_index.read_text(encoding="utf-8") == "keep-index"
    assert not (detached / "archive.md").exists()
    assert not (detached / "bundle_index.json").exists()


def test_stage21_invalidates_index_before_archive_collision(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    (stage_dir / "bundle_index.json").write_text("stale", encoding="utf-8")
    (stage_dir / "archive.md").mkdir()

    result = _review_publish._execute_knowledge_archive(
        stage_dir, run_dir, _config(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "bundle_index.json").exists()
    assert (stage_dir / "archive.md").is_dir()


def test_stage21_preflight_unlinks_fifo_after_commit_point(
    tmp_path: Path, canonical_evidence_migration_complete: None
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-21"
    stage_dir.mkdir(parents=True)
    (stage_dir / "bundle_index.json").write_text("stale", encoding="utf-8")
    fifo = stage_dir / "archive.md"
    os.mkfifo(fifo)

    result = _review_publish._execute_knowledge_archive(
        stage_dir, run_dir, _config(), AdapterBundle(), llm=None
    )

    assert result.status is StageStatus.FAILED
    assert not (stage_dir / "bundle_index.json").exists()
    assert not fifo.exists()
