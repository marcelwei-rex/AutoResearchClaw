from __future__ import annotations

import ast
import json
import os
import shutil
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
TROJNET_TOPIC = "TrojNet hardware Trojan localization on ISCAS-85 circuits"


def _copy_domain_evaluator_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    trusted = tmp_path / "trusted"
    manifest_path = Path(
        "researchclaw/experiment_runtime/domain_evaluators/"
        "trojnet_iscas85_v1/package-manifest-v1.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for root in manifest["source_roots"]:
        source = Path(root["trusted_path"])
        destination = trusted / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    monkeypatch.setattr(authority_module, "TRUSTED_SOURCE_BASE", trusted)
    return trusted


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


def test_domain_evaluator_selector_is_additive_and_v1_identity_is_unchanged() -> None:
    scaffold = select_metric_authority(TOPIC, "sandbox")
    trojnet = select_metric_authority(TROJNET_TOPIC, "sandbox")

    assert scaffold == authority_module._select_metric_authority_v1(TOPIC, "sandbox")
    assert scaffold.schema_version == 1
    assert scaffold.contract_identity()["selector_policy_version"] == "metric_selector_v1"
    assert trojnet.schema_version == 2
    assert trojnet.domain_id == "hardware_trojan_localization"
    assert trojnet.evaluator_id == "trojnet_iscas85_graphsage_localization"
    assert trojnet.contract_identity()["schema_version"] == 2
    assert trojnet.evaluator_authority is not None
    assert trojnet.evaluator_authority["kind"] == "domain_evaluator"
    assert (
        trojnet.evaluator_authority["evaluator_schema"]
        == "trojnet_iscas85_graphsage_localization_v1"
    )


def test_domain_evaluator_stage9_snapshots_are_six_file_exact_replay(
    tmp_path: Path,
) -> None:
    stage9 = tmp_path / "stage-09"
    stage9.mkdir()
    selection = select_metric_authority(TROJNET_TOPIC, "sandbox")
    publish_metric_authority_snapshots(stage9, selection)

    assert {path.name for path in stage9.iterdir()} == {
        "domain_selector_policy.json",
        "domain_profile.json",
        "metric_authority_index.json",
        "metric_authority.json",
        "domain_evaluator_package_manifest.json",
        "domain_evaluator_execution_policy.json",
    }
    assert replay_metric_authority(
        run_dir=tmp_path,
        topic=TROJNET_TOPIC,
        experiment_mode="sandbox",
        stored_identity=selection.contract_identity(),
        metric_units=selection.metric_units,
        metric_display_labels=selection.metric_display_labels,
        evaluator_authority=selection.evaluator_authority,
    ) == selection


def test_domain_evaluator_snapshot_and_stored_authority_are_comparison_only(
    tmp_path: Path,
) -> None:
    stage9 = tmp_path / "stage-09"
    stage9.mkdir()
    selection = select_metric_authority(TROJNET_TOPIC, "sandbox")
    publish_metric_authority_snapshots(stage9, selection)
    manifest = stage9 / "domain_evaluator_package_manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")

    forged = dict(selection.evaluator_authority or {})
    forged["package_manifest_snapshot_sha256"] = "0" * 64
    with pytest.raises(MetricAuthorityError):
        replay_metric_authority(
            run_dir=tmp_path,
            topic=TROJNET_TOPIC,
            experiment_mode="sandbox",
            stored_identity=selection.contract_identity(),
            metric_units=selection.metric_units,
            metric_display_labels=selection.metric_display_labels,
            evaluator_authority=forged,
        )


def test_domain_evaluator_package_manifest_rejects_boolean_size() -> None:
    selection = select_metric_authority(TROJNET_TOPIC, "sandbox")
    path = Path(selection.evaluator_authority["package_manifest_package_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["files"][0]["size"] = True
    with pytest.raises(MetricAuthorityError, match="size"):
        authority_module._load_package_manifest_bytes(
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        )


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
@pytest.mark.parametrize(
    ("path", "message"),
    [
        (("schema_version",), "integer type"),
        (("execution_policy_version",), "integer type"),
        (("invocation_count",), "integer type"),
        (("variants_per_family",), "integer type"),
        (("seeds", 0), "seed type"),
        (("runtime_projection", "torch_num_threads"), "thread type"),
    ],
)
def test_domain_evaluator_execution_policy_rejects_type_confusion(
    path: tuple[object, ...], value: object, message: str
) -> None:
    policy_path = Path(
        "researchclaw/experiment_runtime/domain_evaluators/"
        "trojnet_iscas85_v1/execution-policy-v1.json"
    )
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    target: object = payload
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(MetricAuthorityError, match=message):
        authority_module._load_execution_policy_bytes(
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        )


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_domain_evaluator_execution_policy_requires_true_boolean_flag(
    value: object,
) -> None:
    policy_path = Path(
        "researchclaw/experiment_runtime/domain_evaluators/"
        "trojnet_iscas85_v1/execution-policy-v1.json"
    )
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["runtime_projection"]["torch_deterministic_algorithms"] = value
    with pytest.raises(MetricAuthorityError, match="deterministic flag type"):
        authority_module._load_execution_policy_bytes(
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        )


@pytest.mark.parametrize("extra", ["shadow.py", "__pycache__/shadow.py"])
def test_domain_evaluator_package_rejects_undeclared_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    trusted = _copy_domain_evaluator_sources(tmp_path, monkeypatch)
    vendor = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/vendor/trojnet"
    )
    extra_path = vendor / extra
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("shadow = True\n", encoding="utf-8")

    with pytest.raises(MetricAuthorityError, match="namespace mismatch"):
        select_metric_authority(TROJNET_TOPIC, "sandbox")


def test_domain_evaluator_package_rejects_source_root_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _copy_domain_evaluator_sources(tmp_path, monkeypatch)
    vendor = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/vendor/trojnet"
    )
    external = tmp_path / "external-vendor"
    vendor.rename(external)
    vendor.symlink_to(external, target_is_directory=True)

    with pytest.raises(MetricAuthorityError, match="unsafe"):
        select_metric_authority(TROJNET_TOPIC, "sandbox")


def test_domain_evaluator_package_rejects_hardlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _copy_domain_evaluator_sources(tmp_path, monkeypatch)
    data = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/data/iscas85/c1355"
    )
    first = data / "c1355_ht1_trojan_nodes.txt"
    second = data / "c1355_ht2_trojan_nodes.txt"
    second.unlink()
    os.link(first, second)

    with pytest.raises(MetricAuthorityError, match="inode alias"):
        select_metric_authority(TROJNET_TOPIC, "sandbox")


def test_domain_evaluator_package_rejects_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _copy_domain_evaluator_sources(tmp_path, monkeypatch)
    vendor = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/vendor/trojnet"
    )
    os.mkfifo(vendor / "shadow.fifo")

    with pytest.raises(MetricAuthorityError, match="namespace mismatch"):
        select_metric_authority(TROJNET_TOPIC, "sandbox")


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_domain_evaluator_package_rejects_declared_unsafe_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    trusted = _copy_domain_evaluator_sources(tmp_path, monkeypatch)
    vendor = trusted / (
        "researchclaw/experiment_runtime/validation_fixtures/"
        "trojnet_iscas85_v1/vendor/trojnet"
    )
    leaf = vendor / "train.py"
    leaf.unlink()
    if replacement == "symlink":
        external = tmp_path / "external.py"
        external.write_text("shadow = True\n", encoding="utf-8")
        leaf.symlink_to(external)
    else:
        os.mkfifo(leaf)

    with pytest.raises(MetricAuthorityError, match="unsafe"):
        select_metric_authority(TROJNET_TOPIC, "sandbox")


def test_malformed_v2_policy_does_not_fall_back_to_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "domain-selector-v2.json"
    policy.write_text('{"schema_version":2}\n', encoding="utf-8")
    monkeypatch.setattr(authority_module, "DOMAIN_SELECTOR_PACKAGE_PATH_V2", policy)

    with pytest.raises(MetricAuthorityError, match="fields mismatch"):
        select_metric_authority(TOPIC, "sandbox")


def test_domain_evaluator_package_is_complete_and_verifier_imports_are_closed() -> None:
    selection = select_metric_authority(TROJNET_TOPIC, "sandbox")
    manifest_path = Path(
        selection.evaluator_authority["package_manifest_package_path"]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 46
    assert [item["role"] for item in manifest["files"]].count("evaluator") == 1
    assert [item["role"] for item in manifest["files"]].count("verifier") == 1
    assert [item["role"] for item in manifest["files"]].count("policy") == 1
    assert not any(path.name == "__pycache__" for path in manifest_path.parent.rglob("*"))

    verifier = manifest_path.parent / "verifier_main.py"
    tree = ast.parse(verifier.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    assert imports <= {
        "decimal", "fractions", "hashlib", "json", "pathlib", "re", "sys"
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"__import__", "compile", "eval", "exec"}
        for node in ast.walk(tree)
    )


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
