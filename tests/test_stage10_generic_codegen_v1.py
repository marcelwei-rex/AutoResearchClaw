"""Tests for the Stage 10 generic_sandbox LLM full-codegen channel (TASKBOOK E3b).

For ``domain_id == "generic_sandbox"`` Stage 10 must generate ``main.py``
through the configured LLM (one bounded repair, no silent fallback to any
deterministic baseline), seal it bound to the ``sandbox_generic_v1``
evaluator schema, and record an auditable LLM journal.  The
``security_detection`` (HPC) domain keeps the scaffold-owned channel
byte-identical.  All tests run offline with mock LLMs.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    derive_contract,
    derive_execution_spec,
    dump_contract,
    execution_spec_bytes,
    load_contract,
)
from researchclaw.literature.citation_policy import write_active_config_binding
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    build_stage12_evidence_texts,
    parse_experiment_result_set,
    parse_invocation_result,
    validate_selected_candidate_manifest,
)
from researchclaw.pipeline.stage_impls._code_generation import (
    _execute_code_generation,
    _seal_selected_candidate,
)
from researchclaw.pipeline.stage_impls._execution import (
    _execute_experiment_run,
    _execute_iterative_refine,
)
from researchclaw.pipeline.stages import StageStatus

GENERIC_TOPIC = "structural health monitoring for smart buildings"
SECURITY_TOPIC = "Hardware-performance-counter detection of Spectre attacks"


class _SequenceLLM:
    """Chat-sequence mock aligned with tests/test_literature_screening_contract."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def chat(
        self, messages: list[dict[str, str]], **_kwargs: object
    ) -> SimpleNamespace:
        self.calls.append(messages[0]["content"])
        if not self.responses:
            raise RuntimeError("unexpected extra codegen call")
        return SimpleNamespace(content=self.responses.pop(0))


class _ExplodingLLM:
    """Mock whose chat always raises (provider outage)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def chat(
        self, messages: list[dict[str, str]], **_kwargs: object
    ) -> SimpleNamespace:
        self.calls += 1
        raise self.exc


def _llm_main_py(contract: object) -> str:
    """A lawful model-owned main.py as an LLM would return it (fenced block)."""
    spec = derive_execution_spec(contract)  # type: ignore[arg-type]
    claim_scope = contract.claim_scope  # type: ignore[attr-defined]
    dataset_origin = contract.dataset_origin  # type: ignore[attr-defined]
    dataset_name = contract.dataset_name  # type: ignore[attr-defined]
    return f'''```python
"""LLM-generated generic sandbox experiment (mock fixture)."""

import json

SEEDS = (7, 11, 13)
SEEDS = {tuple(spec["seeds"])!r}
SAMPLE_COUNT = 100


def _evaluate(seed):
    # Deterministic per-seed metrics, identical across seeds so the
    # aggregate mean equals the shared value exactly.
    return {{"accuracy": 0.8, "latency_ms": 1.5, "sample_count": SAMPLE_COUNT}}


def run():
    per_seed = [
        {{"seed": seed, "status": "ok", "metrics": _evaluate(seed), "error": None}}
        for seed in SEEDS
    ]
    aggregate = dict(per_seed[0]["metrics"])
    results = {{
        "schema_version": 1,
        "claim_scope": {claim_scope!r},
        "dataset_origin": {dataset_origin!r},
        "dataset_name": {dataset_name!r},
        "entry_point": "main.py",
        "primary_metric": {{
            "key": "accuracy",
            "value": aggregate["accuracy"],
            "direction": "maximize",
        }},
        "metrics": aggregate,
        "seeds": list(SEEDS),
        "conditions": {spec["conditions"]!r},
        "per_seed": per_seed,
        "runtime_sec": 0.01,
        "evaluator_owner": "model",
    }}
    with open("results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh)


if __name__ == "__main__":
    run()
```
'''


def _cfg(
    tmp_path: Path,
    *,
    topic: str,
    metric_key: str = "accuracy",
    metric_direction: str = "maximize",
) -> RCConfig:
    return RCConfig.from_dict(
        {
            "project": {"name": "rc-test", "mode": "docs-first"},
            "research": {
                "topic": topic,
                "domains": ["ml"],
                "daily_paper_count": 2,
                "quality_threshold": 8.2,
            },
            "runtime": {"timezone": "UTC"},
            "notifications": {
                "channel": "local",
                "on_stage_start": True,
                "on_stage_fail": False,
                "on_gate_required": True,
            },
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "openclaw_bridge": {"use_memory": True, "use_message": True},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "security": {"hitl_required_stages": [5, 9, 20]},
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 60,
                "metric_key": metric_key,
                "metric_direction": metric_direction,
                "sandbox": {
                    "python_path": sys.executable,
                    "gpu_required": False,
                    "max_memory_mb": 1024,
                },
                "code_agent": {"enabled": False},
                "opencode": {"enabled": False},
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )


def _write_stage9(run_dir: Path, cfg: RCConfig) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "config.yaml"
    raw = json.loads(json.dumps(cfg.to_dict()))
    snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    write_active_config_binding(run_dir, snapshot)
    stage9 = run_dir / "stage-09"
    stage9.mkdir(parents=True, exist_ok=True)
    contract = derive_contract(cfg, {"datasets": ["synthetic"]}, stage_dir=stage9)
    contract_path = stage9 / "experiment_contract.yaml"
    dump_contract(contract, contract_path)
    (stage9 / "execution_spec.json").write_bytes(execution_spec_bytes(contract))
    (stage9 / "exp_plan.yaml").write_text("objectives: []\n", encoding="utf-8")
    return contract_path


def _generic_structured_result() -> dict[str, object]:
    ok_metrics = {"accuracy": 0.75, "latency_ms": 2.5}
    return {
        "schema_version": 1,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "dataset_name": "synthetic_pipeline_validation_v1",
        "entry_point": "main.py",
        "primary_metric": {"key": "accuracy", "value": 0.75, "direction": "maximize"},
        "metrics": dict(ok_metrics),
        "seeds": [7, 11],
        "conditions": ["BaselineModel", "FailedVariant"],
        "per_seed": [
            {"seed": 7, "status": "ok", "metrics": dict(ok_metrics), "error": None},
            {
                "seed": 11,
                "status": "failed",
                "metrics": None,
                "error": "fit diverged: NaN loss",
            },
        ],
        "runtime_sec": 1.5,
        "evaluator_owner": "model",
    }


def test_generic_domain_llm_generates_seals_and_stage12_consumes(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    llm = _SequenceLLM([_llm_main_py(load_contract(contract_path))])

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.DONE
    meta = json.loads((stage10 / "generic_full.json").read_text(encoding="utf-8"))
    assert meta["generator"]["source"] == "llm"
    journal = json.loads(
        (stage10 / "generic_llm_journal.json").read_text(encoding="utf-8")
    )
    assert [entry["outcome"] for entry in journal["attempts"]] == ["ok"]
    assert all(entry["response_sha256"] for entry in journal["attempts"])
    main_text = (stage10 / "experiment" / "main.py").read_text(encoding="utf-8")
    assert "DeterministicGenericBaseline" not in main_text

    manifest = json.loads(
        (stage10 / "selected_candidate_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["evaluator_schema"] == "sandbox_generic_v1"
    assert manifest["entry_point"] == "main.py"
    assert manifest["scaffold_files"] == {}
    assert set(manifest["plugin_files"]) == set(manifest["files"]) == {"main.py"}
    assert all(
        metadata["owner"] == "model"
        for metadata in manifest["plugin_files"].values()
    )
    # The seal replays against disk authority for the generic domain as well.
    validate_selected_candidate_manifest(run_dir, cfg)

    # Stage 12 consumes the LLM-generated main.py under sandbox_generic_v1.
    (run_dir / "stage-11").mkdir()
    (run_dir / "stage-11" / "schedule.json").write_text("{}", encoding="utf-8")
    stage12 = run_dir / "stage-12"
    stage12.mkdir()
    run_result = _execute_experiment_run(stage12, run_dir, cfg, AdapterBundle())
    assert run_result.status == StageStatus.DONE
    result_set = parse_experiment_result_set(
        (stage12 / "experiment_result_set.json").read_text(encoding="utf-8")
    )
    assert result_set["evaluator_schema"] == "sandbox_generic_v1"
    invocation = parse_invocation_result(
        (stage12 / "evidence-v1" / "run-1.json").read_text(encoding="utf-8")
    )
    assert invocation["evaluator_schema"] == "sandbox_generic_v1"
    assert list(invocation["metric_observations"]) == ["accuracy"]
    assert invocation["structured_results"]["evaluator_owner"] == "model"


def test_generic_llm_garbage_after_bounded_repair_fails(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    llm = _SequenceLLM(
        ["this is not python at all", "```python\ndef broken(:\n```"]
    )

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 2  # initial attempt + one bounded repair
    # No silent fallback: nothing resembling the removed deterministic
    # baseline may appear in any Stage 10 artifact.
    for path in stage10.rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            assert b"DeterministicGenericBaseline" not in blob
            assert b"deterministic_baseline" not in blob
    journal = json.loads(
        (stage10 / "generic_llm_journal.json").read_text(encoding="utf-8")
    )
    assert [entry["outcome"] for entry in journal["attempts"]] == [
        "invalid",
        "invalid",
    ]


def test_generic_llm_exception_fails_without_fallback(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    llm = _ExplodingLLM(RuntimeError("provider unavailable"))

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.FAILED
    assert llm.calls == 1  # hard exceptions are not retried
    assert "provider unavailable" in (result.error or "")
    main_py = stage10 / "experiment" / "main.py"
    if main_py.exists():
        assert "DeterministicGenericBaseline" not in main_py.read_text(
            encoding="utf-8"
        )
    journal = json.loads(
        (stage10 / "generic_llm_journal.json").read_text(encoding="utf-8")
    )
    assert [entry["outcome"] for entry in journal["attempts"]] == ["error"]


def test_generic_stage13_baseline_passthrough_with_llm(tmp_path: Path) -> None:
    """ADJ-E4-01: generic_sandbox Stage 13 is baseline-passthrough."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    gen_llm = _SequenceLLM([_llm_main_py(load_contract(contract_path))])
    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=gen_llm)  # type: ignore[arg-type]
    assert result.status == StageStatus.DONE

    (run_dir / "stage-11").mkdir()
    (run_dir / "stage-11" / "schedule.json").write_text("{}")
    stage12 = run_dir / "stage-12"
    stage12.mkdir()
    assert _execute_experiment_run(
        stage12, run_dir, cfg, AdapterBundle()
    ).status == StageStatus.DONE

    refine_llm = _SequenceLLM(["```filename:main.py\n# never requested\n```"])
    stage13 = run_dir / "stage-13"
    stage13.mkdir()
    refine = _execute_iterative_refine(stage13, run_dir, cfg, AdapterBundle(), llm=refine_llm)  # type: ignore[arg-type]

    assert refine.status == StageStatus.DONE
    assert refine_llm.calls == []  # passthrough: the LLM is never consulted
    result_set = json.loads(
        (stage13 / "refinement_result_set.json").read_text(encoding="utf-8")
    )
    assert result_set["iterations"] == []
    assert result_set["selected_result"] == {
        "type": "baseline",
        "iteration_id": None,
    }
    assert result_set["evaluator_schema"] == "sandbox_generic_v1"


def test_hpc_domain_keeps_scaffold_owned_main_and_rejects_model_owned(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, topic=SECURITY_TOPIC, metric_key="detection_f1")
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=None)

    assert result.status == StageStatus.DONE
    manifest = json.loads(
        (stage10 / "selected_candidate_manifest.json").read_text(encoding="utf-8")
    )
    assert "evaluator_schema" not in manifest
    assert sorted(manifest["scaffold_files"]) == ["main.py"]
    assert manifest["scaffold_files"]["main.py"]["owner"] == "scaffold"
    assert sorted(manifest["plugin_files"]) == ["detector_plugin.py"]

    # A model-owned main.py remains a sealing failure under the HPC domain.
    exp_dir = stage10 / "experiment"
    (exp_dir / "main.py").write_text(
        "import json\n"
        "if __name__ == '__main__':\n"
        "    json.dump({'metrics': {'detection_f1': 0.99}}, open('results.json', 'w'))\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="scaffold-owned main.py"):
        _seal_selected_candidate(stage10, exp_dir, contract_path, cfg)


def test_generic_aggregate_mean_tampering_is_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract = derive_contract(cfg, {"datasets": ["synthetic"]})
    structured = _generic_structured_result()
    structured["metrics"]["accuracy"] = 0.9  # no longer the mean of ok runs
    with pytest.raises(
        CanonicalExperimentEvidenceError,
        match="generic aggregate metric mismatch",
    ):
        build_stage12_evidence_texts(
            json.dumps(structured),
            contract=contract,
            evaluator_schema="sandbox_generic_v1",
        )


@pytest.mark.parametrize(
    "entry_point",
    ["../escape.py", "/abs/main.py", "nested\\main.py"],
)
def test_generic_entry_point_path_escape_is_rejected(
    tmp_path: Path, entry_point: str
) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    contract = derive_contract(cfg, {"datasets": ["synthetic"]})
    structured = _generic_structured_result()
    structured["entry_point"] = entry_point
    with pytest.raises(CanonicalExperimentEvidenceError, match="entry_point"):
        build_stage12_evidence_texts(
            json.dumps(structured),
            contract=contract,
            evaluator_schema="sandbox_generic_v1",
        )
