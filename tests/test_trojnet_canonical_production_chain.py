from __future__ import annotations

import json
from pathlib import Path

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.llm.client import LLMClient
from researchclaw.literature.verify import parse_bibtex_entries
from researchclaw.pipeline.canonical_experiment_evidence import (
    validate_canonical_experiment_manifest,
)
from researchclaw.pipeline.executor import execute_stage
from researchclaw.pipeline.independent_release_reconstruction import (
    reconstruct_expected_release_publications,
)
from researchclaw.pipeline.stages import Stage, StageStatus
from tests.test_canonical_production_chain import (
    _LLM_STAGES,
    _NoLLM,
    _ProductionChainLLM,
    _config,
    _papers,
    _verified_report,
)


TROJNET_TOPIC = "TrojNet hardware Trojan localization on ISCAS-85 circuits"
_SCOPING_LLM_STAGES = frozenset(
    {Stage.TOPIC_INIT, Stage.PROBLEM_DECOMPOSE, Stage.SEARCH_STRATEGY}
)


def test_stage01_through_stage25_trojnet_pipeline_validation_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_evidence_migration_complete: None,
) -> None:
    """Run the complete domain-evaluator production graph before activation."""

    run_dir = tmp_path / "run"
    config = _config(
        run_dir,
        claim_scope="pipeline_validation",
        topic=TROJNET_TOPIC,
    )
    current_stage: list[Stage] = [Stage.TOPIC_INIT]
    llm = _ProductionChainLLM(run_dir)

    def llm_factory(_config):
        if current_stage[0] in _LLM_STAGES | _SCOPING_LLM_STAGES:
            return llm
        return _NoLLM()

    monkeypatch.setattr(LLMClient, "from_rc_config", staticmethod(llm_factory))
    monkeypatch.setattr(
        "researchclaw.literature.search.search_papers_multi_query",
        lambda *_args, **_kwargs: _papers(3),
    )
    monkeypatch.setattr("researchclaw.data.load_seminal_papers", lambda *_args: [])
    monkeypatch.setattr(
        "researchclaw.literature.novelty.check_novelty",
        lambda **_kwargs: {
            "novelty_score": 1.0,
            "assessment": "bounded TrojNet pipeline-validation input",
            "recommendation": "continue",
        },
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda bib, **_kwargs: _verified_report(
            tuple(str(entry["key"]) for entry in parse_bibtex_entries(bib))
        ),
    )

    results = []
    for stage in Stage:
        current_stage[0] = stage
        result = execute_stage(
            stage,
            run_dir=run_dir,
            run_id="trojnet-canonical-production-chain",
            config=config,
            adapters=AdapterBundle(),
            auto_approve_gates=True,
        )
        results.append(result)
        assert result.status is StageStatus.DONE, (
            f"{stage.name} failed: {result.error}; artifacts={result.artifacts}"
        )

    assert [result.stage for result in results] == list(Stage)
    baseline = json.loads(
        (run_dir / "stage-12/experiment_result_set.json").read_text(
            encoding="utf-8"
        )
    )
    assert baseline["schema_version"] == 2
    assert baseline["result_set_type"] == "stage12_domain_evaluator"
    first_scores = (
        run_dir / "stage-12/evidence-v2/invocation-1/score_evidence.jsonl"
    ).read_bytes()
    second_scores = (
        run_dir / "stage-12/evidence-v2/invocation-2/score_evidence.jsonl"
    ).read_bytes()
    assert first_scores == second_scores
    assert len(first_scores.splitlines()) == 162

    root = validate_canonical_experiment_manifest(run_dir, config)
    assert root["schema_version"] == 2
    assert root["generation_kind"] == "domain_evaluator"
    reconstructed = reconstruct_expected_release_publications(run_dir)
    assert reconstructed.evidence.manifest["schema_version"] == 2
    assert {len(values) for values in reconstructed.evidence.metric_observations.values()} == {
        162
    }
    assert reconstructed.stage24.manifest.path == (
        "stage-24/stage24_truth_manifest.json"
    )
    assert reconstructed.stage25.manifest.path == (
        "stage-25/stage25_deai_manifest.json"
    )
    authority_roles = {
        artifact.path: artifact.role
        for artifact in reconstructed.authority_artifacts
    }
    assert len(authority_roles) == len(reconstructed.authority_artifacts)
    expected_roles = {
        "stage-09/domain_evaluator_package_manifest.json": (
            "stage09_domain_authority"
        ),
        "stage-09/domain_evaluator_execution_policy.json": (
            "stage09_domain_authority"
        ),
        "stage-10/evaluator-capture-v1/capture-manifest.json": (
            "stage10_domain_capture"
        ),
        "stage-10/evaluator-capture-v1/evaluator/evaluator_main.py": (
            "stage10_domain_capture"
        ),
        "stage-10/evaluator-capture-v1/verifier/verifier_main.py": (
            "stage10_domain_capture"
        ),
        "stage-10/evaluator-capture-v1/vendor/anomaly.py": (
            "stage10_domain_capture"
        ),
        "stage-10/evaluator-capture-v1/data/c1355/c1355_ht1.bench": (
            "stage10_domain_capture"
        ),
    }
    assert {
        path: authority_roles[path] for path in expected_roles
    } == expected_roles
    assert "stage-22/code/main.py" in authority_roles
    assert "stage-22/code/verifier_main.py" in authority_roles
    assert "stage-22/code/trojnet/anomaly.py" in authority_roles
    assert "stage-22/code/data/c1355/c1355_ht1.bench" in authority_roles
