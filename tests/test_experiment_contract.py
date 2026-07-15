from __future__ import annotations

import copy
from pathlib import Path

import pytest

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    derive_contract,
    dump_contract,
    find_stage09_contract,
    load_contract,
    validate_contract_dict,
)
from researchclaw.experiment_runtime.metric_authority import select_metric_authority


def _valid_contract() -> dict:
    topic = "Hardware-performance-counter detection of Spectre attacks"
    authority = select_metric_authority(topic, "sandbox")
    return {
        "schema_version": 2,
        "topic": topic,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "dataset_name": None,
        "primary_metric": {
            "key": "detection_f1",
            "direction": "maximize",
            "minimum_valid_value": 0.0,
        },
        "smoke_budget_sec": 60,
        "run_budget_sec": 300,
        "allowed_inputs": [],
        "allowed_outputs": [{"path": "results.json", "required": True}],
        "evaluator": {
            "command": "python main.py",
            "owner": "scaffold",
            "timeout_sec": 300,
            "required_result_keys": ["dataset_origin", "metrics"],
        },
        "safety": {
            "network": "none",
            "env_policy": "allowlist",
            "evidence_policy": "stage12_recomputed_only",
        },
        "sealing": {
            "candidate_manifest": "selected_candidate_manifest.json",
            "content_hash_algorithm": "sha256",
        },
        "metric_authority": authority.contract_identity(),
        "metric_units": authority.metric_units,
        "metric_display_labels": authority.metric_display_labels,
    }


def test_valid_contract_parses() -> None:
    contract = validate_contract_dict(_valid_contract())
    assert contract.claim_scope == "pipeline_validation"
    assert contract.dataset_origin == "synthetic"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", None),
        ("claim_scope", None),
        ("dataset_origin", "unknown"),
    ],
)
def test_contract_rejects_missing_or_unknown_required_fields(field: str, value) -> None:
    data = _valid_contract()
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    with pytest.raises(ContractValidationError):
        validate_contract_dict(data)


def test_contract_rejects_synthetic_research_release() -> None:
    data = _valid_contract()
    data["claim_scope"] = "research_release"
    data["dataset_origin"] = "synthetic"
    with pytest.raises(ContractValidationError, match="research_release"):
        validate_contract_dict(data)


def test_contract_rejects_non_scaffold_evaluator() -> None:
    data = _valid_contract()
    data["evaluator"] = copy.deepcopy(data["evaluator"])
    data["evaluator"]["owner"] = "model"
    with pytest.raises(ContractValidationError, match="evaluator.owner"):
        validate_contract_dict(data)


def test_contract_rejects_smoke_budget_over_limit() -> None:
    data = _valid_contract()
    data["smoke_budget_sec"] = 121
    with pytest.raises(ContractValidationError, match="smoke_budget_sec"):
        validate_contract_dict(data)


def test_contract_rejects_empty_primary_metric_key() -> None:
    data = _valid_contract()
    data["primary_metric"] = copy.deepcopy(data["primary_metric"])
    data["primary_metric"]["key"] = ""
    with pytest.raises(ContractValidationError, match="primary_metric.key"):
        validate_contract_dict(data)


@pytest.mark.parametrize("value", [True, 60.5, "60"])
def test_contract_rejects_non_integer_budget_types(value: object) -> None:
    data = _valid_contract()
    data["smoke_budget_sec"] = value
    with pytest.raises(ContractValidationError, match="smoke_budget_sec"):
        validate_contract_dict(data)


def test_contract_rejects_unknown_nested_field() -> None:
    data = _valid_contract()
    data["primary_metric"] = copy.deepcopy(data["primary_metric"])
    data["primary_metric"]["extra"] = "not-authority"
    with pytest.raises(ContractValidationError, match="primary_metric fields"):
        validate_contract_dict(data)


def test_contract_loader_rejects_duplicate_yaml_key(tmp_path: Path) -> None:
    stage9 = tmp_path / "stage-09"
    stage9.mkdir()
    path = stage9 / "experiment_contract.yaml"
    text = dump_contract(validate_contract_dict(_valid_contract()), path)
    assert text
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "schema_version: 2\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="duplicate YAML key"):
        load_contract(path)


def test_contract_publication_rejects_stage9_parent_symlink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    external = tmp_path / "external"
    run_dir.mkdir()
    external.mkdir()
    (external / "sentinel").write_text("unchanged", encoding="utf-8")
    (run_dir / "stage-09").symlink_to(external, target_is_directory=True)

    with pytest.raises(ContractValidationError, match="cannot publish contract"):
        dump_contract(
            validate_contract_dict(_valid_contract()),
            run_dir / "stage-09" / "experiment_contract.yaml",
        )

    assert sorted(path.name for path in external.iterdir()) == ["sentinel"]


def test_contract_sha256_is_deterministic(tmp_path: Path) -> None:
    contract = validate_contract_dict(_valid_contract())
    p1 = tmp_path / "c1.yaml"
    p2 = tmp_path / "c2.yaml"
    assert dump_contract(contract, p1) == dump_contract(contract, p2)
    assert load_contract(p1).primary_metric["key"] == "detection_f1"


def test_direct_paused_stage9_blocks_fallback_to_versioned_contract(
    tmp_path: Path,
) -> None:
    versioned = tmp_path / "stage-09_v2"
    versioned.mkdir()
    (versioned / "experiment_contract.yaml").write_text(
        "stale pre-pivot contract\n", encoding="utf-8"
    )
    direct = tmp_path / "stage-09"
    direct.mkdir()
    (direct / "plan_meta.json").write_text("{}\n", encoding="utf-8")

    assert find_stage09_contract(tmp_path) is None


def test_stage_health_marker_alone_blocks_versioned_contract_fallback(
    tmp_path: Path,
) -> None:
    versioned = tmp_path / "stage-09_v2"
    versioned.mkdir()
    (versioned / "experiment_contract.yaml").write_text(
        "stale pre-pivot contract\n", encoding="utf-8"
    )
    direct = tmp_path / "stage-09"
    direct.mkdir()
    (direct / "stage_health.json").write_text(
        '{"status":"paused"}\n', encoding="utf-8"
    )

    assert find_stage09_contract(tmp_path) is None


def test_direct_contract_wins_even_when_attempt_markers_exist(tmp_path: Path) -> None:
    direct = tmp_path / "stage-09"
    direct.mkdir()
    contract = direct / "experiment_contract.yaml"
    contract.write_text("new direct contract\n", encoding="utf-8")
    (direct / "stage_health.json").write_text(
        '{"status":"done"}\n', encoding="utf-8"
    )

    assert find_stage09_contract(tmp_path) == contract


def test_highest_versioned_contract_remains_legacy_fallback(tmp_path: Path) -> None:
    for version in (2, 10):
        directory = tmp_path / f"stage-09_v{version}"
        directory.mkdir()
        (directory / "experiment_contract.yaml").write_text(
            f"version {version}\n", encoding="utf-8"
        )

    selected = find_stage09_contract(tmp_path)
    assert selected == tmp_path / "stage-09_v10" / "experiment_contract.yaml"


def test_derive_contract_splits_smoke_and_run_budgets(tmp_path: Path) -> None:
    cfg = RCConfig.from_dict(
        {
            "project": {"name": "rc-test", "mode": "docs-first"},
                "research": {
                    "topic": "Hardware-performance-counter detection of Spectre attacks",
                    "domains": ["security"],
                },
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "none"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 300,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )
    contract = derive_contract(cfg, {"datasets": ["UCI Adult"]})
    assert contract.smoke_budget_sec == 60
    assert contract.run_budget_sec == 300
    assert contract.dataset_origin == "synthetic"
    assert contract.dataset_name == "synthetic_pipeline_validation_v1"


def test_derive_contract_keeps_named_public_dataset(tmp_path: Path) -> None:
    cfg = RCConfig.from_dict(
        {
            "project": {"name": "rc-test", "mode": "docs-first"},
                "research": {
                    "topic": "Hardware-performance-counter detection of Spectre attacks",
                    "domains": ["security"],
                },
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "none"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "fake-model",
                "fallback_models": [],
            },
            "experiment": {
                "mode": "sandbox",
                "time_budget_sec": 300,
                "metric_key": "detection_f1",
                "metric_direction": "maximize",
                "dataset_origin": "public",
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )
    contract = derive_contract(cfg, {"datasets": ["UCI Adult"]})
    assert contract.dataset_origin == "public"
    assert contract.dataset_name == "UCI Adult"
