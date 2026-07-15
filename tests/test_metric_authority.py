from __future__ import annotations

import json
from pathlib import Path

import pytest

from researchclaw.experiment_runtime import metric_authority as authority_module
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace

from researchclaw.experiment_runtime.metric_authority import (
    DOMAIN_SELECTOR_PACKAGE_PATH,
    MetricAuthorityError,
    normalize_topic,
    publish_metric_authority_snapshots,
    replay_metric_authority,
    select_metric_authority,
)


TOPIC = "Hardware-performance-counter detection of Spectre attacks"


def test_topic_selector_is_nfkc_casefold_and_whitespace_deterministic() -> None:
    variants = (
        TOPIC,
        "  HARDWARE-PERFORMANCE-COUNTER\tDETECTION OF SPECTRE ATTACKS  ",
        "Ｈａｒｄｗａｒｅ-performance-counter detection of Spectre attacks",
    )
    assert {select_metric_authority(topic, "sandbox").domain_id for topic in variants} == {
        "security_detection"
    }
    assert normalize_topic(variants[1]) == normalize_topic(TOPIC)


def test_domain_selector_rejects_no_match_instead_of_using_generic_or_llm() -> None:
    with pytest.raises(MetricAuthorityError, match="matched no canonical profile"):
        select_metric_authority("unclassified topic without a governed rule", "sandbox")


def test_metric_selector_rejects_unsupported_mode() -> None:
    with pytest.raises(MetricAuthorityError, match="unsupported"):
        select_metric_authority(TOPIC, "simulated")


def test_stage9_snapshots_are_byte_identical_and_replayable(tmp_path: Path) -> None:
    stage9 = tmp_path / "stage-09"
    stage9.mkdir()
    selection = select_metric_authority(TOPIC, "sandbox")
    publish_metric_authority_snapshots(stage9, selection)

    replayed = replay_metric_authority(
        run_dir=tmp_path,
        topic=TOPIC,
        experiment_mode="sandbox",
        stored_identity=selection.contract_identity(),
        metric_units=selection.metric_units,
        metric_display_labels=selection.metric_display_labels,
    )
    assert replayed == selection
    assert (stage9 / "domain_selector_policy.json").read_bytes() == (
        DOMAIN_SELECTOR_PACKAGE_PATH.read_bytes()
    )


def test_coherent_run_local_domain_policy_substitution_is_not_an_oracle(
    tmp_path: Path,
) -> None:
    stage9 = tmp_path / "stage-09"
    stage9.mkdir()
    selection = select_metric_authority(TOPIC, "sandbox")
    publish_metric_authority_snapshots(stage9, selection)

    substituted = json.loads((stage9 / "domain_selector_policy.json").read_text())
    for rule in substituted["rules"]:
        rule["domain_id"] = "attacker_selected_domain"
    (stage9 / "domain_selector_policy.json").write_text(
        json.dumps(substituted, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    forged_identity = selection.contract_identity()
    forged_identity["domain_id"] = "attacker_selected_domain"
    with pytest.raises(MetricAuthorityError):
        replay_metric_authority(
            run_dir=tmp_path,
            topic=TOPIC,
            experiment_mode="sandbox",
            stored_identity=forged_identity,
            metric_units=selection.metric_units,
            metric_display_labels=selection.metric_display_labels,
        )


def test_stage9_parent_symlink_cannot_write_external_authority(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    external = tmp_path / "external"
    run_dir.mkdir()
    external.mkdir()
    (external / "sentinel").write_text("unchanged", encoding="utf-8")
    (run_dir / "stage-09").symlink_to(external, target_is_directory=True)

    selection = select_metric_authority(TOPIC, "sandbox")
    with pytest.raises(MetricAuthorityError, match="namespace is unsafe"):
        publish_metric_authority_snapshots(run_dir / "stage-09", selection)

    assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
    assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"


def test_package_schema_version_rejects_json_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = json.loads(DOMAIN_SELECTOR_PACKAGE_PATH.read_text(encoding="utf-8"))
    policy["schema_version"] = True
    path = tmp_path / "domain-selector-v1.json"
    path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
    monkeypatch.setattr(authority_module, "DOMAIN_SELECTOR_PACKAGE_PATH", path)

    with pytest.raises(MetricAuthorityError, match="version mismatch"):
        select_metric_authority(TOPIC, "sandbox")


def test_stage9_parent_replacement_during_publication_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    stage9 = run_dir / "stage-09"
    external = tmp_path / "external"
    stage9.mkdir(parents=True)
    external.mkdir()
    (external / "sentinel").write_text("unchanged", encoding="utf-8")
    original = BoundOutputNamespace.write_bytes_atomic
    calls = 0

    def replace_parent(
        namespace: BoundOutputNamespace, name: str, content: bytes
    ) -> None:
        nonlocal calls
        original(namespace, name, content)
        calls += 1
        if calls == 1:
            stage9.rename(run_dir / "stage-09-moved")
            stage9.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(BoundOutputNamespace, "write_bytes_atomic", replace_parent)
    with pytest.raises(MetricAuthorityError, match="namespace is unsafe"):
        publish_metric_authority_snapshots(
            stage9, select_metric_authority(TOPIC, "sandbox")
        )

    assert sorted(path.name for path in external.iterdir()) == ["sentinel"]
    assert (external / "sentinel").read_text(encoding="utf-8") == "unchanged"


def test_snapshot_leaf_symlink_is_removed_without_touching_target(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage9 = run_dir / "stage-09"
    external = tmp_path / "external-policy.json"
    stage9.mkdir(parents=True)
    external.write_text("unchanged", encoding="utf-8")
    leaf = stage9 / "domain_selector_policy.json"
    leaf.symlink_to(external)

    selection = select_metric_authority(TOPIC, "sandbox")
    publish_metric_authority_snapshots(stage9, selection)

    assert external.read_text(encoding="utf-8") == "unchanged"
    assert not leaf.is_symlink()
    assert leaf.read_bytes() == DOMAIN_SELECTOR_PACKAGE_PATH.read_bytes()


def test_profile_schema_version_rejects_json_boolean(tmp_path: Path) -> None:
    payload = {
        "schema_version": True,
        "selector_policy_version": "domain_selector_v1",
        "domain_id": "security_detection",
        "owner": "scaffold",
        "supported_experiment_modes": ["sandbox"],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(MetricAuthorityError, match="version mismatch"):
        authority_module._load_domain_profile(path, "security_detection")


def test_index_schema_version_rejects_json_boolean(tmp_path: Path) -> None:
    payload = {
        "schema_version": True,
        "selector_policy_version": "metric_selector_v1",
        "entries": [],
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(MetricAuthorityError, match="version mismatch"):
        authority_module._load_selector_index(path)


def test_registry_versions_reject_json_boolean(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "policy_version": True,
        "domain_id": "security_detection",
        "evaluator_id": "hpc_anomaly_detection",
        "owner": "scaffold",
        "metrics": [],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(MetricAuthorityError, match="version mismatch"):
        authority_module._load_metric_registry(
            path,
            domain_id="security_detection",
            evaluator_id="hpc_anomaly_detection",
        )


def test_selector_word_boundary_priority_and_tie_are_deterministic() -> None:
    whole_word = {
        "rules": [{
            "rule_id": "word",
            "pattern_kind": "whole_word",
            "normalized_pattern": "spectre",
            "domain_id": "security_detection",
            "priority": 1,
        }]
    }
    assert authority_module._select_domain_id("spectre-like", whole_word) == (
        "security_detection"
    )
    with pytest.raises(MetricAuthorityError, match="matched no"):
        authority_module._select_domain_id("spectrometer", whole_word)

    priority = {
        "rules": [
            {**whole_word["rules"][0], "domain_id": "low", "priority": 1},
            {**whole_word["rules"][0], "rule_id": "high", "domain_id": "high", "priority": 2},
        ]
    }
    assert authority_module._select_domain_id("spectre", priority) == "high"
    priority["rules"][0]["priority"] = 2
    with pytest.raises(MetricAuthorityError, match="ambiguous"):
        authority_module._select_domain_id("spectre", priority)
