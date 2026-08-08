"""Tests for Stage 10 generic execution-spec consumption + runtime repair (E9).

ADJ-E9-01:
(a) The generic codegen prompt consumes only the contract-derived execution
    spec; the paper plan is narrative-only and never enters the prompt.
(b) One bounded runtime repair is allowed for candidate-Python tracebacks
    (returncode=1 with a Python traceback and no environmental markers);
    timeouts, OOM-style failures and non-traceback exits still hard-fail.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from researchclaw.adapters import AdapterBundle
from researchclaw.experiment_runtime.contract import (
    derive_execution_spec,
    derive_execution_spec_diagnostics,
    execution_spec_bytes,
    execution_spec_diagnostics_bytes,
    load_contract,
)
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
from tests.test_stage10_generic_provenance_repair_v1 import _misbound_main_py


def _crashing_main_py(contract: object) -> str:
    """A lawful main.py that compiles but dies with a KeyError traceback."""
    return _llm_main_py(contract).replace(
        'aggregate = dict(per_seed[0]["metrics"])',
        'aggregate = {k: per_seed[0][k] for k in ("accuracy",)}',
        1,
    )


def _sabotaged_main_py(contract: object, prelude: str) -> str:
    """A lawful main.py with *prelude* executed before the seed loop."""
    return _llm_main_py(contract).replace(
        "SEEDS = (7, 11, 13)", f"{prelude}\n\nSEEDS = (7, 11, 13)", 1
    )


def _journal(stage10: Path) -> dict[str, object]:
    return json.loads(
        (stage10 / "generic_llm_journal.json").read_text(encoding="utf-8")
    )


def test_runtime_crash_repair_succeeds(tmp_path: Path) -> None:
    """(b-i) rc=1 candidate traceback -> one runtime repair -> DONE."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM([_crashing_main_py(contract), _llm_main_py(contract)])

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.DONE
    assert len(llm.calls) == 2
    attempts = _journal(stage10)["attempts"]
    assert [e["kind"] for e in attempts] == ["initial", "runtime_repair"]
    repair = attempts[1]
    assert repair["trigger"] == "candidate_python_traceback_rc1"
    assert repair["code_sha256_before"] != repair["code_sha256_after"]
    # The repair prompt carries the crash evidence verbatim.
    assert "Traceback (most recent call last)" in llm.calls[1]
    assert "KeyError" in llm.calls[1]
    # Pre-repair evidence is preserved before the re-smoke overwrite.
    smoke = stage10 / "smoke"
    assert (smoke / "main_prerepair.py").is_file()
    assert (smoke / "smoke_report_prerepair.json").is_file()


def test_runtime_crash_repair_persists_fails(tmp_path: Path) -> None:
    """(b-ii) Repair output still crashes -> FAILED after exactly one repair."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM([_crashing_main_py(contract), _crashing_main_py(contract)])

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 2
    kinds = [e["kind"] for e in _journal(stage10)["attempts"]]
    assert kinds == ["initial", "runtime_repair"]


def test_runtime_exclusions_hard_fail_without_repair(tmp_path: Path) -> None:
    """(b-iii) timeout / MemoryError / rc!=1 exits: no repair, one LLM call."""

    def _run(case: str, code: str, *, budget: int = 60):  # noqa: ANN202
        root = tmp_path / case
        cfg = _cfg(root, topic=GENERIC_TOPIC)
        if budget != 60:  # ExperimentConfig is frozen; replace, don't mutate.
            cfg = dataclasses.replace(
                cfg,
                experiment=dataclasses.replace(
                    cfg.experiment, time_budget_sec=budget
                ),
            )
        run_dir = root / "run"
        _write_stage9(run_dir, cfg)
        stage10 = run_dir / "stage-10"
        llm = _SequenceLLM([code])
        result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]
        return result, llm, stage10

    cfg0 = _cfg(tmp_path / "probe", topic=GENERIC_TOPIC)
    probe_contract = load_contract(_write_stage9(tmp_path / "probe" / "run", cfg0))
    base = _llm_main_py(probe_contract)

    result, llm, _ = _run(
        "timeout", _sabotaged_main_py(probe_contract, "import time\n\ntime.sleep(30)"), budget=1
    )
    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 1

    result, llm, _ = _run(
        "oom", _sabotaged_main_py(probe_contract, 'raise MemoryError("simulated OOM")')
    )
    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 1

    result, llm, stage10 = _run(
        "exit2", _sabotaged_main_py(probe_contract, "import sys\n\nsys.exit(2)")
    )
    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 1
    assert "returncode=2" in (result.error or "")
    assert base  # fixture sanity


def test_provenance_repair_path_unchanged(tmp_path: Path) -> None:
    """(guard) Binding mismatch still routes to provenance_repair, not runtime."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM([_misbound_main_py(contract), _llm_main_py(contract)])

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.DONE
    kinds = [e["kind"] for e in _journal(stage10)["attempts"]]
    assert kinds == ["initial", "provenance_repair"]


def test_codegen_prompt_consumes_execution_spec_not_paper_plan(tmp_path: Path) -> None:
    """(a-i) Plan prose (CIFAR-10/170 GPU hours) never enters the prompt."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    (run_dir / "stage-09" / "exp_plan.yaml").write_text(
        "objectives:\n  - Train ResNet-50 on CIFAR-10 for 170 GPU hours\n"
        "hypothesis: CIFAR-10 accuracy exceeds 99 percent\n",
        encoding="utf-8",
    )
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM([_llm_main_py(contract)])

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.DONE
    prompt = llm.calls[0]
    assert "CIFAR" not in prompt
    assert "170 GPU hours" not in prompt
    spec = derive_execution_spec(contract)
    assert spec["spec_hash"] in prompt
    assert spec["contract_hash"] in prompt


@pytest.mark.parametrize("mutation", ["missing", "field", "bytes", "stale"])
def test_codegen_rejects_uncommitted_execution_spec_before_llm(
    tmp_path: Path, mutation: str
) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    contract = load_contract(contract_path)
    spec_path = run_dir / "stage-09/execution_spec.json"
    if mutation == "missing":
        spec_path.unlink()
    elif mutation == "field":
        spec = derive_execution_spec(contract)
        spec["unexpected"] = True
        spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    elif mutation == "bytes":
        spec_path.write_bytes(execution_spec_bytes(contract) + b" ")
    else:
        stale_cfg = _cfg(
            tmp_path / "stale", topic=GENERIC_TOPIC, metric_key="latency_ms"
        )
        stale_contract = load_contract(_write_stage9(tmp_path / "stale-run", stale_cfg))
        spec_path.write_bytes(execution_spec_bytes(stale_contract))
    llm = _SequenceLLM([_llm_main_py(contract)])

    result = _execute_code_generation(
        run_dir / "stage-10", run_dir, cfg, AdapterBundle(), llm=llm
    )  # type: ignore[arg-type]

    assert result.status == StageStatus.FAILED
    assert llm.calls == []
    assert "execution spec" in (result.error or "").lower()
    assert not (run_dir / "stage-10/selected_candidate_manifest.json").exists()


def test_codegen_prompt_hash_depends_on_spec_not_plan(tmp_path: Path) -> None:
    """(a-ii) plan change keeps prompt hash; contract change alters it."""

    def _prompt_hash(
        root: Path,
        *,
        metric_key: str = "accuracy",
        metric_direction: str = "maximize",
        plan: str,
    ) -> str:
        cfg = _cfg(
            root, topic=GENERIC_TOPIC,
            metric_key=metric_key, metric_direction=metric_direction,
        )
        run_dir = root / "run"
        contract_path = _write_stage9(run_dir, cfg)
        (run_dir / "stage-09" / "exp_plan.yaml").write_text(plan, encoding="utf-8")
        contract = load_contract(contract_path)
        code = _llm_main_py(contract)
        if metric_key != "accuracy":
            code = code.replace(
                '"key": "accuracy",\n            "value": aggregate["accuracy"],\n'
                '            "direction": "maximize",',
                f'"key": "{metric_key}",\n            "value": aggregate["{metric_key}"],\n'
                f'            "direction": "{metric_direction}",',
                1,
            )
        llm = _SequenceLLM([code])
        result = _execute_code_generation(run_dir / "stage-10", run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]
        assert result.status == StageStatus.DONE
        return hashlib.sha256(llm.calls[0].encode("utf-8")).hexdigest()

    base = _prompt_hash(tmp_path / "a", plan="objectives: []\n")
    plan_changed = _prompt_hash(
        tmp_path / "b", plan="objectives:\n  - CIFAR-10 with 170 GPU hours\n"
    )
    contract_changed = _prompt_hash(
        tmp_path / "c",
        metric_key="latency_ms",
        metric_direction="minimize",
        plan="objectives: []\n",
    )
    assert base == plan_changed
    assert base != contract_changed


def test_diagnostics_hash_changes_without_affecting_authority_or_prompt(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []
    spec_hashes: list[str] = []
    diagnostic_hashes: list[str] = []
    for index, diagnostics in enumerate(
        ([], [{"type": "plan_conflict", "field": "datasets"}])
    ):
        root = tmp_path / str(index)
        cfg = _cfg(root, topic=GENERIC_TOPIC)
        run_dir = root / "run"
        contract_path = _write_stage9(run_dir, cfg)
        contract = load_contract(contract_path)
        (run_dir / "stage-09/execution_spec_diagnostics.json").write_bytes(
            execution_spec_diagnostics_bytes(diagnostics)
        )
        llm = _SequenceLLM([_llm_main_py(contract)])
        result = _execute_code_generation(
            run_dir / "stage-10", run_dir, cfg, AdapterBundle(), llm=llm
        )  # type: ignore[arg-type]
        assert result.status == StageStatus.DONE
        prompts.append(llm.calls[0])
        spec_hashes.append(derive_execution_spec(contract)["spec_hash"])
        diagnostic_hashes.append(
            derive_execution_spec_diagnostics(diagnostics)["diagnostics_hash"]
        )

    assert prompts[0] == prompts[1]
    assert spec_hashes[0] == spec_hashes[1]
    assert diagnostic_hashes[0] != diagnostic_hashes[1]


def test_total_llm_call_budget_capped_at_three(tmp_path: Path) -> None:
    """(guard) syntax repair + one smoke-time repair = 3 calls, never more."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    contract = load_contract(contract_path)
    llm = _SequenceLLM(
        [
            "this is not python at all",
            _misbound_main_py(contract),
            _misbound_main_py(contract),
        ]
    )

    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]

    assert result.status == StageStatus.FAILED
    assert len(llm.calls) == 3
    kinds = [e["kind"] for e in _journal(stage10)["attempts"]]
    assert kinds == ["initial", "repair", "provenance_repair"]


def test_execution_spec_deterministic_and_self_hashed(tmp_path: Path) -> None:
    """(a-iii) derive_execution_spec is pure in the contract and self-hashed."""
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    contract = load_contract(_write_stage9(tmp_path / "run", cfg))

    spec = derive_execution_spec(contract)

    assert spec == derive_execution_spec(contract)
    body = {k: v for k, v in spec.items() if k != "spec_hash"}
    assert spec["spec_hash"] == hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert spec["execution_domain"] == "generic_sandbox"
    assert spec["paper_plan_role"].startswith("narrative_only")
    assert len(spec["seeds"]) == 3
    assert all(isinstance(seed, int) for seed in spec["seeds"])
    assert spec["allowed_metric_keys"] == sorted(contract.metric_units)
