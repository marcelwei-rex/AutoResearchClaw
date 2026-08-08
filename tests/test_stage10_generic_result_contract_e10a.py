from __future__ import annotations
import json
from pathlib import Path
import pytest
from researchclaw.adapters import AdapterBundle
from researchclaw.experiment_runtime.contract import derive_contract, derive_execution_spec, load_contract
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    build_stage12_evidence_texts,
)
from researchclaw.pipeline.stage_impls._code_generation import _execute_code_generation
from researchclaw.pipeline.stages import StageStatus
from tests.test_stage10_generic_codegen_v1 import (
    GENERIC_TOPIC, _SequenceLLM, _cfg, _generic_structured_result, _llm_main_py, _write_stage9,
)
_CONTRACT_MARKERS = (
    "exactly these top-level keys", "keys {seed, status, metrics, error}",
    'status="ok" requires flat metrics and error=null', "failed/negative requires metrics=null and a nonempty error",
    "At least one per_seed entry must have status=\"ok\"", "aggregate metrics value MUST equal the exact mean",
    "schema_version is int and equals 1", 'entry_point="main.py"',
    "metrics is an object", "seeds is a nonempty list of fixed ints",
    "conditions is a nonempty string list", "runtime_sec is a nonnegative number",
    'evaluator_owner="model"', 'status is only "ok", "failed", or "negative"',
)
def _error_main_py(contract: object, error_source: str) -> str:
    code = _llm_main_py(contract)
    assert '"error": None' in code
    return code.replace('"error": None', f'"error": {error_source}', 1)
def _run(tmp_path: Path, responses: list[str]):
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    run_dir = tmp_path / "run"
    contract_path = _write_stage9(run_dir, cfg)
    stage10 = run_dir / "stage-10"
    (stage10 / "selected_candidate").mkdir(parents=True)
    (stage10 / "selected_candidate_manifest.json").write_text("stale")
    llm = _SequenceLLM(responses)
    result = _execute_code_generation(stage10, run_dir, cfg, AdapterBundle(), llm=llm)  # type: ignore[arg-type]
    journal = json.loads((stage10 / "generic_llm_journal.json").read_text())
    return result, llm, journal, stage10, load_contract(contract_path)
def test_shared_complete_contract_in_initial_syntax_and_smoke_repair(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    contract = derive_contract(cfg, {"datasets": ["synthetic"]})
    result, llm, _journal, _stage10, _ = _run(tmp_path / "syntax", ["```python\ndef broken(:\n```", _llm_main_py(contract)])
    assert result.status == StageStatus.DONE
    for prompt in llm.calls:
        assert all(marker in prompt for marker in _CONTRACT_MARKERS)

    bad = _error_main_py(contract, "''")
    result, llm, journal, stage10, _ = _run(tmp_path / "smoke", [bad, _error_main_py(contract, "None")])
    assert result.status == StageStatus.DONE
    assert all(marker in llm.calls[1] for marker in _CONTRACT_MARKERS)
    repair = journal["attempts"][1]
    assert repair["call_role"] == "repair"
    assert repair["trigger"] == "smoke_contract_validation"
    assert repair["diagnostics"] == ["ok generic run must not carry an error"]
    assert repair["code_sha256_before"] != repair["code_sha256_after"]
    assert (stage10 / "smoke/smoke_results_prerepair.json").is_file()
def test_persistent_empty_error_fails_without_any_seal(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    contract = derive_contract(cfg, {"datasets": ["synthetic"]})
    bad = _error_main_py(contract, "''")
    run_root = tmp_path / "persistent"
    result, llm, _journal, stage10, _ = _run(run_root, [bad, bad])
    assert result.status == StageStatus.FAILED and len(llm.calls) == 2
    assert not (stage10 / "selected_candidate").exists() and not (stage10 / "selected_candidate_manifest.json").exists()
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("error"), "fields mismatch"), (lambda row: row.update(error="bad"), "ok generic run must not carry an error"),
        (lambda row: row.update(status="failed", metrics=None, error=""), "nonempty string"), (lambda row: row.update(status="failed", error="bad"), "must not carry metrics"),
    ],
)
def test_existing_row_validator_remains_fail_closed(tmp_path: Path, mutation, message: str) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    structured = _generic_structured_result()
    mutation(structured["per_seed"][0])
    with pytest.raises(CanonicalExperimentEvidenceError, match=message):
        build_stage12_evidence_texts(json.dumps(structured), contract=derive_contract(cfg, {"datasets": ["synthetic"]}), evaluator_schema="sandbox_generic_v1")
@pytest.mark.parametrize("faults", [
    {"entry_point": "other.py"}, {"evaluator_owner": "scaffold"},
    {"seeds": [7, 11, 13]}, {"conditions": ["other"]},
    {"dataset_origin": ""}, {"dataset_origin": "wrong"},
    {"dataset_origin": "", "evaluator_owner": "scaffold"},
])
def test_stage10_repairs_exact_model_spec_binding(tmp_path: Path, faults: dict[str, object]) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    contract = derive_contract(cfg, {"datasets": ["synthetic"]})
    good = _llm_main_py(contract)
    spec = derive_execution_spec(contract)
    bad_code = good
    for field, bad in faults.items():
        source = {"entry_point": '"main.py"', "evaluator_owner": '"model"', "conditions": repr(spec["conditions"]), "dataset_origin": repr(contract.dataset_origin)}.get(field)
        bad_code = bad_code.replace(f'SEEDS = {tuple(spec["seeds"])!r}', f'SEEDS = {tuple(bad)!r}', 1) if field == "seeds" else bad_code.replace(f'"{field}": {source}', f'"{field}": {bad!r}', 1)
    assert bad_code != good
    result, llm, journal, stage10, _ = _run(tmp_path, [bad_code, good])
    assert result.status == StageStatus.DONE
    assert len(llm.calls) == 2 and journal["attempts"][1]["kind"] == "provenance_repair"
    assert (stage10 / "selected_candidate_manifest.json").is_file()
    result, llm, _, stage10, _ = _run(tmp_path / "persistent", [bad_code, bad_code])
    assert result.status == StageStatus.FAILED and len(llm.calls) == 2
    assert not (stage10 / "selected_candidate").exists() and not (stage10 / "selected_candidate_manifest.json").exists()
def test_memoryerror_with_binding_fault_is_not_repaired(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, topic=GENERIC_TOPIC)
    good = _llm_main_py(derive_contract(cfg, {"datasets": ["synthetic"]}))
    bad = good.replace('def run():', 'def run():\n    raise MemoryError("oom")', 1).replace('"evaluator_owner": "model"', '"evaluator_owner": "scaffold"', 1)
    result, llm, _, stage10, _ = _run(tmp_path, [bad])
    assert result.status == StageStatus.FAILED and len(llm.calls) == 1
    assert not (stage10 / "selected_candidate").exists() and not (stage10 / "selected_candidate_manifest.json").exists()
