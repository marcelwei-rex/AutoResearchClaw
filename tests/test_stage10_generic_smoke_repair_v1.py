"""Tests for Stage 10 generic-channel unified smoke-time repair (TASKBOOK E8).

ADJ-E8-01: all smoke-time detectable contract deviations in the generic
channel — missing/non-finite primary metric, provenance mismatch, canonical
validator schema errors — route into ONE bounded repair (smoke-time budget 1)
whose prompt carries every diagnostic verbatim.  The generation-time syntax
repair keeps E3 semantics, so a run uses at most 2 repair LLM calls
(syntax <=1 + smoke-time <=1).  If deviations persist after the repair,
Stage 10 fails explicitly; the pipeline never rewrites results.json itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from researchclaw.adapters import AdapterBundle
from researchclaw.experiment_runtime.contract import load_contract
from researchclaw.pipeline.stage_impls._code_generation import (
    _execute_code_generation,
)
from researchclaw.pipeline.stages import StageStatus

from tests.test_stage10_generic_codegen_v1 import (
    GENERIC_TOPIC,
    _SequenceLLM,
    _cfg,
    _llm_main_py,
    _write_stage9,
)


def _nested_metrics_main_py(contract: object) -> str:
    """Lawful main.py whose results.json nests metrics under a condition."""
    return _llm_main_py(contract).replace(
        '"metrics": aggregate,',
        '"metrics": {"MockLLMGenericModel": aggregate},',
        1,
    )


def _double_fault_main_py(contract: object) -> str:
    """Nested metrics AND a misbound claim_scope in one results.json."""
    return _nested_metrics_main_py(contract).replace(
        "'pipeline_validation'", "'synthetic_pipeline_validation'", 1
    )


def _run(tmp_path: Path, renders: list[object]):
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM([render(contract) for render in renders])
    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]
    journal = json.loads(
        (stage10 / "generic_llm_journal.json").read_text(encoding="utf-8")
    )
    return result, llm, journal


def test_metric_deviation_repair_succeeds(tmp_path: Path) -> None:
    """(a) Nested/missing flat metric -> one smoke-time repair -> DONE."""
    result, llm, journal = _run(
        tmp_path, [_nested_metrics_main_py, _llm_main_py]
    )

    assert result.status == StageStatus.DONE
    assert len(llm.calls) == 2
    assert [entry["kind"] for entry in journal["attempts"]] == [
        "initial",
        "provenance_repair",
    ]
    assert [entry["outcome"] for entry in journal["attempts"]] == ["ok", "ok"]


def test_metric_repair_prompt_carries_blocker_diagnostic(tmp_path: Path) -> None:
    """(b) The smoke-time repair prompt embeds the metric blocker verbatim."""
    result, llm, journal = _run(
        tmp_path, [_nested_metrics_main_py, _llm_main_py]
    )

    assert result.status == StageStatus.DONE
    assert len(llm.calls) == 2
    assert "did not produce finite primary metric 'accuracy'" in llm.calls[1]


def test_provenance_and_metric_faults_single_repair(tmp_path: Path) -> None:
    """(c) Provenance + metric double fault -> one repair fixes both -> DONE."""
    result, llm, journal = _run(tmp_path, [_double_fault_main_py, _llm_main_py])

    assert result.status == StageStatus.DONE
    assert len(journal["attempts"]) <= 2  # syntax <=1 + smoke-time <=1
    assert len(llm.calls) == 2
    repair_prompt = llm.calls[1]
    assert "did not produce finite primary metric 'accuracy'" in repair_prompt
    assert "generic evaluator contract binding mismatch" in repair_prompt


def test_persistent_deviation_after_repair_fails(tmp_path: Path) -> None:
    """(d) Deviations surviving the bounded repair -> explicit FAILED."""
    result, llm, journal = _run(
        tmp_path, [_nested_metrics_main_py, _nested_metrics_main_py]
    )

    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 2
    assert "did not produce finite primary metric 'accuracy'" in (
        result.error or ""
    )
