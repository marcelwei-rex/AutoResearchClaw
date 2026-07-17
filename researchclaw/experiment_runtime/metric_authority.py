"""Code-owned metric authority selection and run-local snapshot replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace


DOMAIN_SELECTOR_SCHEMA_VERSION = 1
DOMAIN_SELECTOR_POLICY_VERSION = "domain_selector_v1"
METRIC_SELECTOR_POLICY_VERSION = "metric_selector_v1"
METRIC_AUTHORITY_POLICY_VERSION = 1
NORMALIZATION_POLICY = "nfkc_casefold_space_v1"

DOMAIN_SELECTOR_SCHEMA_VERSION_V2 = 2
DOMAIN_SELECTOR_POLICY_VERSION_V2 = 2
METRIC_SELECTOR_POLICY_VERSION_V2 = 2
METRIC_AUTHORITY_POLICY_VERSION_V2 = 2

PACKAGE_ROOT = Path(__file__).with_name("metric_authority")
DOMAIN_SELECTOR_PACKAGE_PATH = PACKAGE_ROOT / "domain-selector-v1.json"
METRIC_SELECTOR_INDEX_PATH = PACKAGE_ROOT / "index-v1.json"
DOMAIN_SELECTOR_PACKAGE_PATH_V2 = PACKAGE_ROOT / "domain-selector-v2.json"
METRIC_SELECTOR_INDEX_PATH_V2 = PACKAGE_ROOT / "index-v2.json"

DOMAIN_SELECTOR_SNAPSHOT_PATH = "stage-09/domain_selector_policy.json"
DOMAIN_PROFILE_SNAPSHOT_PATH = "stage-09/domain_profile.json"
METRIC_SELECTOR_INDEX_SNAPSHOT_PATH = "stage-09/metric_authority_index.json"
METRIC_AUTHORITY_SNAPSHOT_PATH = "stage-09/metric_authority.json"
DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH = (
    "stage-09/domain_evaluator_package_manifest.json"
)
DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH = (
    "stage-09/domain_evaluator_execution_policy.json"
)

TROJNET_DOMAIN_ID = "hardware_trojan_localization"
TROJNET_EVALUATOR_ID = "trojnet_iscas85_graphsage_localization"
TROJNET_EVALUATOR_SCHEMA = "trojnet_iscas85_graphsage_localization_v1"
TROJNET_PACKAGE_MANIFEST_PATH = Path(
    "researchclaw/experiment_runtime/domain_evaluators/"
    "trojnet_iscas85_v1/package-manifest-v1.json"
)
TROJNET_REGISTRY_PATH = Path(
    "researchclaw/experiment_runtime/metric_authority/"
    "trojnet_iscas85_graphsage_localization-v2.json"
)
TRUSTED_SOURCE_BASE = Path(__file__).parents[2]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_METRIC_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ASCII_ID_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")


class MetricAuthorityError(ValueError):
    """Raised when metric authority cannot be selected or replayed exactly."""


@dataclass(frozen=True)
class MetricAuthoritySelection:
    domain_id: str
    evaluator_id: str
    experiment_mode: str
    evaluator_kind: str
    domain_selector_package_path: str
    domain_selector_package_sha256: str
    domain_selector_snapshot_path: str
    domain_selector_snapshot_sha256: str
    domain_profile_path: str
    domain_profile_sha256: str
    selector_index_path: str
    selector_index_sha256: str
    authority_path: str
    authority_sha256: str
    selector_input_sha256: str
    selector_policy_version: str | int
    metric_units: dict[str, str]
    metric_display_labels: dict[str, list[str]]
    schema_version: int = 1
    identity_v2: dict[str, Any] | None = None
    evaluator_authority: dict[str, Any] | None = None

    def contract_identity(self) -> dict[str, Any]:
        if self.schema_version == 2:
            if self.identity_v2 is None:
                raise MetricAuthorityError("metric authority v2 identity is missing")
            return deepcopy(self.identity_v2)
        return {
            "path": self.authority_path,
            "sha256": self.authority_sha256,
            "policy_version": METRIC_AUTHORITY_POLICY_VERSION,
            "domain_id": self.domain_id,
            "evaluator_id": self.evaluator_id,
            "domain_profile_path": self.domain_profile_path,
            "domain_profile_sha256": self.domain_profile_sha256,
            "selector_index_path": self.selector_index_path,
            "selector_index_sha256": self.selector_index_sha256,
            "domain_selector_package_path": self.domain_selector_package_path,
            "domain_selector_package_sha256": self.domain_selector_package_sha256,
            "domain_selector_snapshot_path": self.domain_selector_snapshot_path,
            "domain_selector_snapshot_sha256": self.domain_selector_snapshot_sha256,
            "selector_input_sha256": self.selector_input_sha256,
            "selector_policy_version": self.selector_policy_version,
            "experiment_mode": self.experiment_mode,
            "evaluator_kind": self.evaluator_kind,
        }


@dataclass(frozen=True)
class DomainEvaluatorCapturePlan:
    selection: MetricAuthoritySelection
    package_manifest_bytes: bytes
    execution_policy_bytes: bytes
    source_namespace_sha256: str
    files: tuple[dict[str, Any], ...]
    contents: Mapping[str, bytes]


def build_domain_evaluator_capture_plan(
    topic: str, experiment_mode: str
) -> DomainEvaluatorCapturePlan:
    """Capture one exact trusted package plan without authorizing live paths."""

    selection = select_metric_authority(topic, experiment_mode)
    if selection.schema_version != 2 or selection.evaluator_authority is None:
        raise MetricAuthorityError("selected authority is not a domain evaluator")
    manifest_path = TRUSTED_SOURCE_BASE / TROJNET_PACKAGE_MANIFEST_PATH
    manifest_bytes = _read_regular_bytes(
        manifest_path, "domain evaluator package manifest"
    )
    if (
        _sha256_bytes(manifest_bytes)
        != selection.evaluator_authority["package_manifest_package_sha256"]
    ):
        raise MetricAuthorityError("domain evaluator package manifest hash mismatch")
    manifest, sources = _load_package_manifest_and_sources(manifest_bytes)
    execution_policy_bytes = sources[("package", manifest["execution_policy_path"])]
    _load_execution_policy_bytes(execution_policy_bytes)
    if (
        _sha256_bytes(execution_policy_bytes)
        != selection.evaluator_authority["execution_policy_package_sha256"]
    ):
        raise MetricAuthorityError("domain evaluator execution policy hash mismatch")

    ordered_entries: list[dict[str, Any]] = []
    contents: dict[str, bytes] = {}
    namespace_rows: list[dict[str, Any]] = []
    for item in manifest["files"]:
        package_entry = {
            "source_root": item["source_root"],
            "source_path": item["source_path"],
            "capture_path": item["capture_path"],
            "role": item["role"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
        ordered_entries.append(
            {
                "role": item["role"],
                "path": item["capture_path"],
                "sha256": item["sha256"],
                "size": item["size"],
                "package_entry_sha256": _sha256_bytes(
                    _canonical_json_bytes(package_entry)
                ),
            }
        )
        contents[item["capture_path"]] = sources[
            (item["source_root"], item["source_path"])
        ]
        namespace_rows.append(
            {
                "source_root": item["source_root"],
                "source_path": item["source_path"],
                "role": item["role"],
                "sha256": item["sha256"],
                "size": item["size"],
            }
        )
    return DomainEvaluatorCapturePlan(
        selection=selection,
        package_manifest_bytes=manifest_bytes,
        execution_policy_bytes=execution_policy_bytes,
        source_namespace_sha256=_sha256_bytes(
            _canonical_json_bytes(namespace_rows)
        ),
        files=tuple(ordered_entries),
        contents=deepcopy(contents),
    )


def replay_captured_domain_evaluator_authority(
    *,
    topic: str,
    experiment_mode: str,
    package_manifest_bytes: bytes,
    execution_policy_bytes: bytes,
    stored_identity: Mapping[str, Any],
    metric_units: Mapping[str, str],
    metric_display_labels: Mapping[str, list[str]],
    evaluator_authority: Mapping[str, Any],
) -> MetricAuthoritySelection:
    """Replay trusted selector metadata without reopening captured source roots."""

    policy_bytes, policy = _load_domain_selector_policy_v2(
        DOMAIN_SELECTOR_PACKAGE_PATH_V2
    )
    normalized_topic = normalize_topic(topic)
    domain_id = _select_domain_id(normalized_topic, policy)
    if domain_id != TROJNET_DOMAIN_ID:
        raise MetricAuthorityError("captured evaluator domain selection mismatch")
    profile_path = PACKAGE_ROOT / "profiles" / f"{domain_id}-v2.json"
    profile_bytes, profile = _load_domain_profile_v2(profile_path, domain_id)
    if experiment_mode not in profile["supported_experiment_modes"]:
        raise MetricAuthorityError("captured evaluator experiment mode mismatch")
    index_bytes, index = _load_selector_index_v2(METRIC_SELECTOR_INDEX_PATH_V2)
    matches = [
        item
        for item in index["entries"]
        if item["domain_id"] == domain_id
        and item["experiment_mode"] == experiment_mode
        and item["evaluator_kind"] == "domain_evaluator"
    ]
    if len(matches) != 1:
        raise MetricAuthorityError("captured evaluator selector match mismatch")
    entry = matches[0]
    if (
        entry["evaluator_id"] != TROJNET_EVALUATOR_ID
        or entry["package_manifest_path"] != TROJNET_PACKAGE_MANIFEST_PATH.as_posix()
        or entry["metric_registry_path"] != TROJNET_REGISTRY_PATH.as_posix()
        or _sha256_bytes(package_manifest_bytes) != entry["package_manifest_sha256"]
    ):
        raise MetricAuthorityError("captured evaluator package authority mismatch")
    manifest = _parse_package_manifest_metadata_bytes(package_manifest_bytes)
    if (
        _sha256_bytes(execution_policy_bytes) != manifest["execution_policy_sha256"]
    ):
        raise MetricAuthorityError("captured execution policy authority mismatch")
    _load_execution_policy_bytes(execution_policy_bytes)
    registry_path = Path(__file__).parents[2] / entry["metric_registry_path"]
    registry_bytes = _read_regular_bytes(registry_path, "domain evaluator registry")
    if _sha256_bytes(registry_bytes) != entry["metric_registry_sha256"]:
        raise MetricAuthorityError("captured evaluator registry hash mismatch")
    registry = _load_metric_registry_v2_bytes(
        registry_bytes, domain_id=domain_id, evaluator_id=entry["evaluator_id"]
    )

    policy_sha = _sha256_bytes(policy_bytes)
    profile_sha = _sha256_bytes(profile_bytes)
    index_sha = _sha256_bytes(index_bytes)
    registry_sha = _sha256_bytes(registry_bytes)
    manifest_sha = _sha256_bytes(package_manifest_bytes)
    execution_sha = _sha256_bytes(execution_policy_bytes)
    selector_input = {
        "domain_id": domain_id,
        "domain_profile_sha256": profile_sha,
        "domain_selector_package_sha256": policy_sha,
        "evaluator_kind": "domain_evaluator",
        "experiment_mode": experiment_mode,
        "package_manifest_sha256": manifest_sha,
        "registry_sha256": registry_sha,
        "selector_index_sha256": index_sha,
        "selector_policy_version": METRIC_SELECTOR_POLICY_VERSION_V2,
        "topic_normalized_sha256": _sha256_bytes(normalized_topic.encode("utf-8")),
        "topic_raw_sha256": _sha256_bytes(topic.encode("utf-8")),
    }
    expected_identity = {
        "schema_version": 2,
        "policy_version": 2,
        "domain_id": domain_id,
        "evaluator_id": entry["evaluator_id"],
        "experiment_mode": experiment_mode,
        "evaluator_kind": "domain_evaluator",
        "selector_policy_version": 2,
        "selector_input_sha256": _sha256_bytes(_canonical_json_bytes(selector_input)),
        "domain_selector_package": {
            "path": _repo_relative(DOMAIN_SELECTOR_PACKAGE_PATH_V2),
            "sha256": policy_sha,
        },
        "domain_selector_snapshot": {
            "path": DOMAIN_SELECTOR_SNAPSHOT_PATH,
            "sha256": policy_sha,
        },
        "domain_profile_snapshot": {
            "path": DOMAIN_PROFILE_SNAPSHOT_PATH,
            "sha256": profile_sha,
        },
        "selector_index_package": {
            "path": _repo_relative(METRIC_SELECTOR_INDEX_PATH_V2),
            "sha256": index_sha,
        },
        "selector_index_snapshot": {
            "path": METRIC_SELECTOR_INDEX_SNAPSHOT_PATH,
            "sha256": index_sha,
        },
        "registry_snapshot": {
            "path": METRIC_AUTHORITY_SNAPSHOT_PATH,
            "sha256": registry_sha,
        },
    }
    expected_evaluator_authority = {
        "kind": "domain_evaluator",
        "domain_id": domain_id,
        "evaluator_id": entry["evaluator_id"],
        "evaluator_schema": manifest["evaluator_schema"],
        "package_manifest_package_path": entry["package_manifest_path"],
        "package_manifest_package_sha256": manifest_sha,
        "package_manifest_snapshot_path": DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH,
        "package_manifest_snapshot_sha256": manifest_sha,
        "execution_policy_package_path": (
            Path(entry["package_manifest_path"]).parent
            / manifest["execution_policy_path"]
        ).as_posix(),
        "execution_policy_package_sha256": execution_sha,
        "execution_policy_snapshot_path": DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH,
        "execution_policy_snapshot_sha256": execution_sha,
        "input_capture_policy_version": 1,
        "result_set_policy_version": 2,
        "observation_replay_policy_version": 1,
    }
    expected_units = {item["key"]: item["unit"] for item in registry["metrics"]}
    expected_labels = {
        item["key"]: list(item["display_labels"]) for item in registry["metrics"]
    }
    if dict(stored_identity) != expected_identity:
        raise MetricAuthorityError("captured metric authority identity mismatch")
    if dict(evaluator_authority) != expected_evaluator_authority:
        raise MetricAuthorityError("captured evaluator authority identity mismatch")
    if dict(metric_units) != expected_units or dict(metric_display_labels) != expected_labels:
        raise MetricAuthorityError("captured metric registry projection mismatch")
    return MetricAuthoritySelection(
        domain_id=domain_id,
        evaluator_id=entry["evaluator_id"],
        experiment_mode=experiment_mode,
        evaluator_kind="domain_evaluator",
        domain_selector_package_path=_repo_relative(DOMAIN_SELECTOR_PACKAGE_PATH_V2),
        domain_selector_package_sha256=policy_sha,
        domain_selector_snapshot_path=DOMAIN_SELECTOR_SNAPSHOT_PATH,
        domain_selector_snapshot_sha256=policy_sha,
        domain_profile_path=DOMAIN_PROFILE_SNAPSHOT_PATH,
        domain_profile_sha256=profile_sha,
        selector_index_path=METRIC_SELECTOR_INDEX_SNAPSHOT_PATH,
        selector_index_sha256=index_sha,
        authority_path=METRIC_AUTHORITY_SNAPSHOT_PATH,
        authority_sha256=registry_sha,
        selector_input_sha256=expected_identity["selector_input_sha256"],
        selector_policy_version=2,
        metric_units=expected_units,
        metric_display_labels=expected_labels,
        schema_version=2,
        identity_v2=expected_identity,
        evaluator_authority=expected_evaluator_authority,
    )


def _parse_package_manifest_metadata_bytes(data: bytes) -> dict[str, Any]:
    value = _parse_json_object_bytes(data, "captured domain evaluator package manifest")
    _exact_keys(
        value,
        {
            "schema_version", "package_policy_version", "domain_id", "evaluator_id",
            "evaluator_schema", "execution_policy_path", "execution_policy_sha256",
            "source_roots", "files",
        },
        "captured domain evaluator package manifest",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["package_policy_version"]) is not int
        or value["package_policy_version"] != 1
        or value["domain_id"] != TROJNET_DOMAIN_ID
        or value["evaluator_id"] != TROJNET_EVALUATOR_ID
        or value["evaluator_schema"] != TROJNET_EVALUATOR_SCHEMA
        or value["execution_policy_path"] != "execution-policy-v1.json"
        or not isinstance(value["execution_policy_sha256"], str)
        or _SHA256_RE.fullmatch(value["execution_policy_sha256"]) is None
    ):
        raise MetricAuthorityError("captured package manifest identity mismatch")
    return value


def normalize_topic(topic: str) -> str:
    if not isinstance(topic, str):
        raise MetricAuthorityError("research topic must be a string")
    normalized = unicodedata.normalize("NFKC", topic).casefold().strip()
    return " ".join(normalized.split())


def select_metric_authority(topic: str, experiment_mode: str) -> MetricAuthoritySelection:
    """Select additive v2 authority or preserve the exact v1 selection path."""
    policy_bytes, policy = _load_domain_selector_policy_v2(
        DOMAIN_SELECTOR_PACKAGE_PATH_V2
    )
    normalized_topic = normalize_topic(topic)
    try:
        domain_id = _select_domain_id(normalized_topic, policy)
    except MetricAuthorityError as exc:
        if "matched no canonical profile" not in str(exc):
            raise
        return _select_metric_authority_v1(topic, experiment_mode)
    if domain_id != TROJNET_DOMAIN_ID:
        raise MetricAuthorityError("domain selector v2 selected an unsupported domain")
    return _select_metric_authority_v2(
        topic=topic,
        normalized_topic=normalized_topic,
        experiment_mode=experiment_mode,
        policy_bytes=policy_bytes,
        domain_id=domain_id,
    )


def _select_metric_authority_v1(
    topic: str, experiment_mode: str
) -> MetricAuthoritySelection:
    """Run the unchanged scaffold v1 selection path."""
    policy_bytes, policy = _load_domain_selector_policy(DOMAIN_SELECTOR_PACKAGE_PATH)
    normalized_topic = normalize_topic(topic)
    domain_id = _select_domain_id(normalized_topic, policy)

    profile_path = PACKAGE_ROOT / "profiles" / f"{domain_id}-v1.json"
    profile_bytes, profile = _load_domain_profile(profile_path, domain_id)
    supported_modes = profile["supported_experiment_modes"]
    if experiment_mode not in supported_modes:
        raise MetricAuthorityError(
            f"experiment mode {experiment_mode!r} is unsupported for domain {domain_id}"
        )
    evaluator_kind = _evaluator_kind(experiment_mode)

    index_bytes, index = _load_selector_index(METRIC_SELECTOR_INDEX_PATH)
    matches = [
        item for item in index["entries"]
        if item["domain_id"] == domain_id
        and item["experiment_mode"] == experiment_mode
        and item["evaluator_kind"] == evaluator_kind
    ]
    if len(matches) != 1:
        raise MetricAuthorityError("metric authority selector must match exactly one evaluator")
    evaluator_id = matches[0]["evaluator_id"]
    authority_path = PACKAGE_ROOT / f"{evaluator_id}-v1.json"
    authority_bytes, authority = _load_metric_registry(
        authority_path, domain_id=domain_id, evaluator_id=evaluator_id
    )

    package_policy_sha = _sha256_bytes(policy_bytes)
    profile_sha = _sha256_bytes(profile_bytes)
    index_sha = _sha256_bytes(index_bytes)
    authority_sha = _sha256_bytes(authority_bytes)
    profile_identity = {
        "domain_id": domain_id,
        "package_profile_path": _package_relative(profile_path),
        "package_profile_sha256": profile_sha,
        "topic_raw_sha256": _sha256_bytes(topic.encode("utf-8")),
        "topic_normalized_sha256": _sha256_bytes(normalized_topic.encode("utf-8")),
        "domain_selector_package_path": _package_relative(DOMAIN_SELECTOR_PACKAGE_PATH),
        "domain_selector_package_sha256": package_policy_sha,
        "selector_policy_version": DOMAIN_SELECTOR_POLICY_VERSION,
    }
    selector_input = {
        "canonical_domain_profile_identity": profile_identity,
        "experiment_mode": experiment_mode,
        "evaluator_kind": evaluator_kind,
        "selector_policy_version": METRIC_SELECTOR_POLICY_VERSION,
        "domain_selector_package_sha256": package_policy_sha,
        "selector_index_sha256": index_sha,
    }
    units = {item["key"]: item["unit"] for item in authority["metrics"]}
    labels = {item["key"]: list(item["display_labels"]) for item in authority["metrics"]}
    return MetricAuthoritySelection(
        domain_id=domain_id,
        evaluator_id=evaluator_id,
        experiment_mode=experiment_mode,
        evaluator_kind=evaluator_kind,
        domain_selector_package_path=_package_relative(DOMAIN_SELECTOR_PACKAGE_PATH),
        domain_selector_package_sha256=package_policy_sha,
        domain_selector_snapshot_path=DOMAIN_SELECTOR_SNAPSHOT_PATH,
        domain_selector_snapshot_sha256=package_policy_sha,
        domain_profile_path=DOMAIN_PROFILE_SNAPSHOT_PATH,
        domain_profile_sha256=profile_sha,
        selector_index_path=METRIC_SELECTOR_INDEX_SNAPSHOT_PATH,
        selector_index_sha256=index_sha,
        authority_path=METRIC_AUTHORITY_SNAPSHOT_PATH,
        authority_sha256=authority_sha,
        selector_input_sha256=_sha256_bytes(_canonical_json_bytes(selector_input)),
        selector_policy_version=METRIC_SELECTOR_POLICY_VERSION,
        metric_units=units,
        metric_display_labels=labels,
    )


def _select_metric_authority_v2(
    *,
    topic: str,
    normalized_topic: str,
    experiment_mode: str,
    policy_bytes: bytes,
    domain_id: str,
) -> MetricAuthoritySelection:
    profile_path = PACKAGE_ROOT / "profiles" / f"{domain_id}-v2.json"
    profile_bytes, profile = _load_domain_profile_v2(profile_path, domain_id)
    if experiment_mode not in profile["supported_experiment_modes"]:
        raise MetricAuthorityError(
            f"experiment mode {experiment_mode!r} is unsupported for domain {domain_id}"
        )
    evaluator_kind = "domain_evaluator"
    index_bytes, index = _load_selector_index_v2(METRIC_SELECTOR_INDEX_PATH_V2)
    matches = [
        item
        for item in index["entries"]
        if item["domain_id"] == domain_id
        and item["experiment_mode"] == experiment_mode
        and item["evaluator_kind"] == evaluator_kind
    ]
    if len(matches) != 1:
        raise MetricAuthorityError(
            "domain evaluator selector must match exactly one evaluator"
        )
    entry = matches[0]
    if (
        entry["evaluator_id"] != TROJNET_EVALUATOR_ID
        or entry["package_manifest_path"] != TROJNET_PACKAGE_MANIFEST_PATH.as_posix()
        or entry["metric_registry_path"] != TROJNET_REGISTRY_PATH.as_posix()
    ):
        raise MetricAuthorityError("domain evaluator index path allowlist mismatch")

    manifest_path = Path(__file__).parents[2] / entry["package_manifest_path"]
    manifest_bytes = _read_regular_bytes(manifest_path, "domain evaluator package manifest")
    if _sha256_bytes(manifest_bytes) != entry["package_manifest_sha256"]:
        raise MetricAuthorityError("domain evaluator package manifest hash mismatch")
    manifest = _load_package_manifest_bytes(manifest_bytes)

    registry_path = Path(__file__).parents[2] / entry["metric_registry_path"]
    registry_bytes = _read_regular_bytes(registry_path, "domain evaluator registry")
    if _sha256_bytes(registry_bytes) != entry["metric_registry_sha256"]:
        raise MetricAuthorityError("domain evaluator registry hash mismatch")
    registry = _load_metric_registry_v2_bytes(
        registry_bytes, domain_id=domain_id, evaluator_id=entry["evaluator_id"]
    )

    execution_policy_path = manifest_path.parent / manifest["execution_policy_path"]
    execution_policy_bytes = _read_regular_bytes(
        execution_policy_path, "domain evaluator execution policy"
    )
    if _sha256_bytes(execution_policy_bytes) != manifest["execution_policy_sha256"]:
        raise MetricAuthorityError("domain evaluator execution policy hash mismatch")
    _load_execution_policy_bytes(execution_policy_bytes)

    policy_sha = _sha256_bytes(policy_bytes)
    profile_sha = _sha256_bytes(profile_bytes)
    index_sha = _sha256_bytes(index_bytes)
    registry_sha = _sha256_bytes(registry_bytes)
    manifest_sha = _sha256_bytes(manifest_bytes)
    execution_policy_sha = _sha256_bytes(execution_policy_bytes)
    selector_input = {
        "domain_id": domain_id,
        "domain_profile_sha256": profile_sha,
        "domain_selector_package_sha256": policy_sha,
        "evaluator_kind": evaluator_kind,
        "experiment_mode": experiment_mode,
        "package_manifest_sha256": manifest_sha,
        "registry_sha256": registry_sha,
        "selector_index_sha256": index_sha,
        "selector_policy_version": METRIC_SELECTOR_POLICY_VERSION_V2,
        "topic_normalized_sha256": _sha256_bytes(normalized_topic.encode("utf-8")),
        "topic_raw_sha256": _sha256_bytes(topic.encode("utf-8")),
    }
    identity = {
        "schema_version": 2,
        "policy_version": 2,
        "domain_id": domain_id,
        "evaluator_id": entry["evaluator_id"],
        "experiment_mode": experiment_mode,
        "evaluator_kind": evaluator_kind,
        "selector_policy_version": 2,
        "selector_input_sha256": _sha256_bytes(
            _canonical_json_bytes(selector_input)
        ),
        "domain_selector_package": {
            "path": _repo_relative(DOMAIN_SELECTOR_PACKAGE_PATH_V2),
            "sha256": policy_sha,
        },
        "domain_selector_snapshot": {
            "path": DOMAIN_SELECTOR_SNAPSHOT_PATH,
            "sha256": policy_sha,
        },
        "domain_profile_snapshot": {
            "path": DOMAIN_PROFILE_SNAPSHOT_PATH,
            "sha256": profile_sha,
        },
        "selector_index_package": {
            "path": _repo_relative(METRIC_SELECTOR_INDEX_PATH_V2),
            "sha256": index_sha,
        },
        "selector_index_snapshot": {
            "path": METRIC_SELECTOR_INDEX_SNAPSHOT_PATH,
            "sha256": index_sha,
        },
        "registry_snapshot": {
            "path": METRIC_AUTHORITY_SNAPSHOT_PATH,
            "sha256": registry_sha,
        },
    }
    evaluator_authority = {
        "kind": "domain_evaluator",
        "domain_id": domain_id,
        "evaluator_id": entry["evaluator_id"],
        "evaluator_schema": manifest["evaluator_schema"],
        "package_manifest_package_path": entry["package_manifest_path"],
        "package_manifest_package_sha256": manifest_sha,
        "package_manifest_snapshot_path": DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH,
        "package_manifest_snapshot_sha256": manifest_sha,
        "execution_policy_package_path": _repo_relative(execution_policy_path),
        "execution_policy_package_sha256": execution_policy_sha,
        "execution_policy_snapshot_path": DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH,
        "execution_policy_snapshot_sha256": execution_policy_sha,
        "input_capture_policy_version": 1,
        "result_set_policy_version": 2,
        "observation_replay_policy_version": 1,
    }
    units = {item["key"]: item["unit"] for item in registry["metrics"]}
    labels = {
        item["key"]: list(item["display_labels"])
        for item in registry["metrics"]
    }
    return MetricAuthoritySelection(
        domain_id=domain_id,
        evaluator_id=entry["evaluator_id"],
        experiment_mode=experiment_mode,
        evaluator_kind=evaluator_kind,
        domain_selector_package_path=_repo_relative(DOMAIN_SELECTOR_PACKAGE_PATH_V2),
        domain_selector_package_sha256=policy_sha,
        domain_selector_snapshot_path=DOMAIN_SELECTOR_SNAPSHOT_PATH,
        domain_selector_snapshot_sha256=policy_sha,
        domain_profile_path=DOMAIN_PROFILE_SNAPSHOT_PATH,
        domain_profile_sha256=profile_sha,
        selector_index_path=METRIC_SELECTOR_INDEX_SNAPSHOT_PATH,
        selector_index_sha256=index_sha,
        authority_path=METRIC_AUTHORITY_SNAPSHOT_PATH,
        authority_sha256=registry_sha,
        selector_input_sha256=identity["selector_input_sha256"],
        selector_policy_version=2,
        metric_units=units,
        metric_display_labels=labels,
        schema_version=2,
        identity_v2=identity,
        evaluator_authority=evaluator_authority,
    )


def _snapshot_sources(selection: MetricAuthoritySelection) -> dict[str, Path]:
    if selection.schema_version == 1:
        return {
            "domain_selector_policy.json": DOMAIN_SELECTOR_PACKAGE_PATH,
            "domain_profile.json": PACKAGE_ROOT / "profiles" / f"{selection.domain_id}-v1.json",
            "metric_authority_index.json": METRIC_SELECTOR_INDEX_PATH,
            "metric_authority.json": PACKAGE_ROOT / f"{selection.evaluator_id}-v1.json",
        }
    if selection.evaluator_authority is None:
        raise MetricAuthorityError("domain evaluator authority is missing")
    manifest_path = Path(__file__).parents[2] / TROJNET_PACKAGE_MANIFEST_PATH
    return {
        "domain_selector_policy.json": DOMAIN_SELECTOR_PACKAGE_PATH_V2,
        "domain_profile.json": PACKAGE_ROOT / "profiles" / f"{selection.domain_id}-v2.json",
        "metric_authority_index.json": METRIC_SELECTOR_INDEX_PATH_V2,
        "metric_authority.json": Path(__file__).parents[2] / TROJNET_REGISTRY_PATH,
        "domain_evaluator_package_manifest.json": manifest_path,
        "domain_evaluator_execution_policy.json": manifest_path.parent / "execution-policy-v1.json",
    }


def publish_metric_authority_snapshots(
    stage_dir: Path,
    selection: MetricAuthoritySelection,
    *,
    namespace: BoundOutputNamespace | None = None,
) -> None:
    """Copy trusted package bytes into the fixed Stage 9 snapshot paths."""
    if stage_dir.name != "stage-09":
        raise MetricAuthorityError("metric authority snapshots require canonical stage-09")
    sources = _snapshot_sources(selection)
    def publish(bound: BoundOutputNamespace) -> None:
        if bound.stage_dir != stage_dir:
            raise OSError("metric authority namespace does not match stage-09")
        for name, source in sources.items():
            data = _read_regular_bytes(
                source, f"trusted metric authority source {name}"
            )
            bound.write_bytes_atomic(name, data)
            if bound.read_bytes(name) != data:
                raise MetricAuthorityError(
                    f"metric authority snapshot publication failed: {name}"
                )
        bound.assert_canonical()

    try:
        if namespace is not None:
            publish(namespace)
        else:
            with BoundOutputNamespace.open(
                stage_dir.parent, stage_dir, "stage-09"
            ) as opened:
                publish(opened)
    except OSError as exc:
        raise MetricAuthorityError(
            f"metric authority snapshot namespace is unsafe: {exc}"
        ) from exc


def replay_metric_authority(
    *,
    run_dir: Path,
    topic: str,
    experiment_mode: str,
    stored_identity: Mapping[str, Any],
    metric_units: Mapping[str, Any],
    metric_display_labels: Mapping[str, Any],
    evaluator_authority: Mapping[str, Any] | None = None,
    namespace: BoundOutputNamespace | None = None,
) -> MetricAuthoritySelection:
    """Rerun both trusted selectors, then compare all run-local snapshots."""
    selection = select_metric_authority(topic, experiment_mode)
    expected_identity = selection.contract_identity()
    if dict(stored_identity) != expected_identity:
        raise MetricAuthorityError("stored metric authority identity mismatch")
    if dict(metric_units) != selection.metric_units:
        raise MetricAuthorityError("stored metric unit projection mismatch")
    if {key: list(value) for key, value in metric_display_labels.items()} != selection.metric_display_labels:
        raise MetricAuthorityError("stored metric display-label projection mismatch")
    if selection.evaluator_authority is None:
        if evaluator_authority is not None:
            raise MetricAuthorityError("scaffold authority cannot include evaluator_authority")
    elif dict(evaluator_authority or {}) != selection.evaluator_authority:
        raise MetricAuthorityError("stored domain evaluator authority mismatch")
    snapshot_sources = {
        f"stage-09/{name}": source
        for name, source in _snapshot_sources(selection).items()
    }
    def replay(bound: BoundOutputNamespace) -> None:
        if bound.run_dir != run_dir or bound.stage_dir != run_dir / "stage-09":
            raise OSError("metric authority namespace does not match run stage-09")
        for relative, source in snapshot_sources.items():
            name = Path(relative).name
            snapshot = bound.read_bytes(name)
            package = _read_regular_bytes(
                source, f"trusted metric authority package {source.name}"
            )
            if snapshot != package:
                raise MetricAuthorityError(
                    f"run-local metric snapshot mismatch: {relative}"
                )
        bound.assert_canonical()

    try:
        if namespace is not None:
            replay(namespace)
        else:
            with BoundOutputNamespace.open(
                run_dir, run_dir / "stage-09", "stage-09"
            ) as opened:
                replay(opened)
    except OSError as exc:
        raise MetricAuthorityError(
            f"run-local metric snapshot namespace is unsafe: {exc}"
        ) from exc
    return selection


def _select_domain_id(normalized_topic: str, policy: Mapping[str, Any]) -> str:
    matches: list[Mapping[str, Any]] = []
    for rule in policy["rules"]:
        pattern = rule["normalized_pattern"]
        if rule["pattern_kind"] == "substring":
            matched = pattern in normalized_topic
        else:
            matched = _contains_whole_word(normalized_topic, pattern)
        if matched:
            matches.append(rule)
    if not matches:
        raise MetricAuthorityError("domain selector matched no canonical profile")
    maximum = max(item["priority"] for item in matches)
    winners = {item["domain_id"] for item in matches if item["priority"] == maximum}
    if len(winners) != 1:
        raise MetricAuthorityError("domain selector has an ambiguous maximum-priority tie")
    return next(iter(winners))


def _contains_whole_word(text: str, pattern: str) -> bool:
    start = 0
    while True:
        index = text.find(pattern, start)
        if index < 0:
            return False
        before = text[index - 1] if index else None
        after_index = index + len(pattern)
        after = text[after_index] if after_index < len(text) else None
        if (before is None or not _word_codepoint(before)) and (
            after is None or not _word_codepoint(after)
        ):
            return True
        start = index + 1


def _word_codepoint(value: str) -> bool:
    return value == "_" or unicodedata.category(value)[0] in {"L", "N"}


def _evaluator_kind(experiment_mode: str) -> str:
    if experiment_mode in {"sandbox", "docker"}:
        return "scaffold"
    raise MetricAuthorityError(f"unsupported canonical experiment mode: {experiment_mode}")


def _load_domain_selector_policy_v2(path: Path) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "domain selector policy v2")
    _exact_keys(
        value,
        {"schema_version", "selector_policy_version", "normalization", "no_match", "rules"},
        "domain selector policy v2",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != DOMAIN_SELECTOR_SCHEMA_VERSION_V2
        or type(value["selector_policy_version"]) is not int
        or value["selector_policy_version"] != DOMAIN_SELECTOR_POLICY_VERSION_V2
        or value["normalization"] != NORMALIZATION_POLICY
        or value["no_match"] != "delegate_v1"
    ):
        raise MetricAuthorityError("domain selector policy v2 semantics mismatch")
    _validate_selector_rules(value["rules"], expected_domain=TROJNET_DOMAIN_ID)
    return data, value


def _validate_selector_rules(rules: object, *, expected_domain: str) -> None:
    if not isinstance(rules, list) or not rules:
        raise MetricAuthorityError("domain selector v2 rules must be nonempty")
    seen: set[str] = set()
    order: list[tuple[int, str]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise MetricAuthorityError("domain selector v2 rule must be an object")
        _exact_keys(
            rule,
            {"rule_id", "pattern_kind", "normalized_pattern", "domain_id", "priority"},
            "domain selector v2 rule",
        )
        rule_id = _ascii_id(rule["rule_id"], "domain selector v2 rule_id")
        if rule_id in seen:
            raise MetricAuthorityError("duplicate domain selector v2 rule_id")
        seen.add(rule_id)
        if rule["pattern_kind"] not in {"whole_word", "substring"}:
            raise MetricAuthorityError("invalid domain selector v2 pattern_kind")
        pattern = rule["normalized_pattern"]
        if not isinstance(pattern, str) or not pattern or normalize_topic(pattern) != pattern:
            raise MetricAuthorityError("domain selector v2 pattern is not normalized")
        if rule["domain_id"] != expected_domain:
            raise MetricAuthorityError("domain selector v2 domain mismatch")
        priority = rule["priority"]
        if type(priority) is not int or not 0 <= priority <= 1000:
            raise MetricAuthorityError("domain selector v2 priority is invalid")
        order.append((-priority, rule_id))
    if order != sorted(order):
        raise MetricAuthorityError("domain selector v2 rules are not sorted")


def _load_domain_profile_v2(
    path: Path, expected_domain: str
) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "canonical domain profile v2")
    _exact_keys(
        value,
        {"schema_version", "selector_policy_version", "domain_id", "owner", "supported_experiment_modes"},
        "canonical domain profile v2",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["selector_policy_version"]) is not int
        or value["selector_policy_version"] != 2
        or value["domain_id"] != expected_domain
        or value["owner"] != "domain_evaluator"
        or value["supported_experiment_modes"] != ["sandbox"]
    ):
        raise MetricAuthorityError("canonical domain profile v2 mismatch")
    return data, value


def _load_selector_index_v2(path: Path) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "metric selector index v2")
    _exact_keys(value, {"schema_version", "selector_policy_version", "entries"}, "metric selector index v2")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["selector_policy_version"]) is not int
        or value["selector_policy_version"] != 2
    ):
        raise MetricAuthorityError("metric selector index v2 version mismatch")
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise MetricAuthorityError("metric selector index v2 entries must be nonempty")
    rows: list[tuple[str, str, str, str]] = []
    selectors: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MetricAuthorityError("metric selector index v2 entry must be an object")
        _exact_keys(
            entry,
            {
                "domain_id", "experiment_mode", "evaluator_kind", "evaluator_id",
                "package_policy_version", "package_manifest_path",
                "package_manifest_sha256", "metric_registry_path", "metric_registry_sha256",
            },
            "metric selector index v2 entry",
        )
        row = tuple(
            _ascii_id(entry[key], f"metric selector v2 {key}")
            for key in ("domain_id", "experiment_mode", "evaluator_kind", "evaluator_id")
        )
        if row[:3] in selectors:
            raise MetricAuthorityError("duplicate metric selector v2 tuple")
        selectors.add(row[:3])
        rows.append(row)
        if row != (
            TROJNET_DOMAIN_ID,
            "sandbox",
            "domain_evaluator",
            TROJNET_EVALUATOR_ID,
        ):
            raise MetricAuthorityError("metric selector index v2 entry is not allowlisted")
        if type(entry["package_policy_version"]) is not int or entry["package_policy_version"] != 1:
            raise MetricAuthorityError("package policy version mismatch")
        for key in ("package_manifest_sha256", "metric_registry_sha256"):
            if not isinstance(entry[key], str) or _SHA256_RE.fullmatch(entry[key]) is None:
                raise MetricAuthorityError(f"metric selector v2 {key} is invalid")
        for key in ("package_manifest_path", "metric_registry_path"):
            _safe_relative_posix(entry[key], f"metric selector v2 {key}")
        if (
            entry["package_manifest_path"] != TROJNET_PACKAGE_MANIFEST_PATH.as_posix()
            or entry["metric_registry_path"] != TROJNET_REGISTRY_PATH.as_posix()
        ):
            raise MetricAuthorityError("metric selector index v2 package path mismatch")
    if rows != sorted(rows):
        raise MetricAuthorityError("metric selector index v2 entries are not sorted")
    return data, value


def _load_metric_registry_v2_bytes(
    data: bytes, *, domain_id: str, evaluator_id: str
) -> dict[str, Any]:
    value = _parse_json_object_bytes(data, "metric authority registry v2")
    _exact_keys(
        value,
        {"schema_version", "policy_version", "domain_id", "evaluator_id", "owner", "metrics"},
        "metric authority registry v2",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["policy_version"]) is not int
        or value["policy_version"] != 2
        or value["domain_id"] != domain_id
        or value["evaluator_id"] != evaluator_id
        or value["owner"] != "domain_evaluator"
    ):
        raise MetricAuthorityError("metric authority registry v2 identity mismatch")
    metrics = value["metrics"]
    if not isinstance(metrics, list) or len(metrics) != 8:
        raise MetricAuthorityError("metric authority registry v2 must contain eight metrics")
    keys: list[str] = []
    normalized_labels: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            raise MetricAuthorityError("metric authority registry v2 entry must be an object")
        _exact_keys(metric, {"key", "unit", "display_labels"}, "metric authority registry v2 entry")
        key = metric["key"]
        if not isinstance(key, str) or _METRIC_KEY_RE.fullmatch(key) is None or metric["unit"] != "ratio":
            raise MetricAuthorityError("metric authority registry v2 metric mismatch")
        labels = metric["display_labels"]
        if not isinstance(labels, list) or not 1 <= len(labels) <= 8:
            raise MetricAuthorityError("metric authority registry v2 labels mismatch")
        for label in labels:
            if not isinstance(label, str) or not label or label != label.strip():
                raise MetricAuthorityError("metric authority registry v2 label is invalid")
            normalized = normalize_topic(label)
            if normalized in normalized_labels:
                raise MetricAuthorityError("duplicate metric authority v2 label")
            normalized_labels.add(normalized)
        keys.append(key)
    expected = ["accuracy", "auprc", "auroc", "f1", "fpr", "precision", "recall", "top_k_precision"]
    if keys != expected:
        raise MetricAuthorityError("metric authority registry v2 key set mismatch")
    return value


def _load_package_manifest_bytes(data: bytes) -> dict[str, Any]:
    value, _sources = _load_package_manifest_and_sources(data)
    return value


def _load_package_manifest_and_sources(
    data: bytes,
) -> tuple[dict[str, Any], dict[tuple[str, str], bytes]]:
    value = _parse_json_object_bytes(data, "domain evaluator package manifest")
    _exact_keys(
        value,
        {
            "schema_version", "package_policy_version", "domain_id", "evaluator_id",
            "evaluator_schema", "execution_policy_path", "execution_policy_sha256",
            "source_roots", "files",
        },
        "domain evaluator package manifest",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["package_policy_version"]) is not int
        or value["package_policy_version"] != 1
        or value["domain_id"] != TROJNET_DOMAIN_ID
        or value["evaluator_id"] != TROJNET_EVALUATOR_ID
        or value["evaluator_schema"] != TROJNET_EVALUATOR_SCHEMA
        or value["execution_policy_path"] != "execution-policy-v1.json"
        or not isinstance(value["execution_policy_sha256"], str)
        or _SHA256_RE.fullmatch(value["execution_policy_sha256"]) is None
    ):
        raise MetricAuthorityError("domain evaluator package manifest identity mismatch")
    expected_roots = [
        {
            "root_id": "data",
            "trusted_path": "researchclaw/experiment_runtime/validation_fixtures/trojnet_iscas85_v1/data/iscas85",
            "reserved_control_files": [],
        },
        {
            "root_id": "package",
            "trusted_path": "researchclaw/experiment_runtime/domain_evaluators/trojnet_iscas85_v1",
            "reserved_control_files": ["package-manifest-v1.json"],
        },
        {
            "root_id": "vendor",
            "trusted_path": "researchclaw/experiment_runtime/validation_fixtures/trojnet_iscas85_v1/vendor/trojnet",
            "reserved_control_files": [],
        },
    ]
    if value["source_roots"] != expected_roots:
        raise MetricAuthorityError("domain evaluator source roots mismatch")
    files = value["files"]
    if not isinstance(files, list) or len(files) != 46:
        raise MetricAuthorityError("domain evaluator package must contain 46 files")
    capture_paths: list[str] = []
    source_pairs: set[tuple[str, str]] = set()
    roles: list[str] = []
    root_paths = {item["root_id"]: item["trusted_path"] for item in expected_roots}
    declared_by_root: dict[str, set[str]] = {
        root_id: set() for root_id in root_paths
    }
    for item in files:
        if not isinstance(item, dict):
            raise MetricAuthorityError("domain evaluator package file must be an object")
        _exact_keys(item, {"source_root", "source_path", "capture_path", "role", "sha256", "size"}, "domain evaluator package file")
        source_root = item["source_root"]
        if source_root not in root_paths:
            raise MetricAuthorityError("domain evaluator package source root mismatch")
        source_path = _safe_relative_posix(item["source_path"], "package source_path")
        capture_path = _safe_relative_posix(item["capture_path"], "package capture_path")
        pair = (source_root, source_path)
        if pair in source_pairs:
            raise MetricAuthorityError("duplicate domain evaluator source file")
        source_pairs.add(pair)
        declared_by_root[source_root].add(source_path)
        if item["role"] not in {"evaluator", "verifier", "vendor", "data", "policy"}:
            raise MetricAuthorityError("domain evaluator package role mismatch")
        expected_prefix = {
            "evaluator": ("package", "evaluator_main.py", "evaluator/evaluator_main.py"),
            "verifier": ("package", "verifier_main.py", "verifier/verifier_main.py"),
            "policy": ("package", "execution-policy-v1.json", "policy/execution-policy-v1.json"),
        }
        role = item["role"]
        if role in expected_prefix and (
            source_root,
            source_path,
            capture_path,
        ) != expected_prefix[role]:
            raise MetricAuthorityError("domain evaluator control-file mapping mismatch")
        if role == "vendor" and (
            source_root != "vendor" or capture_path != f"vendor/{source_path}"
        ):
            raise MetricAuthorityError("domain evaluator vendor mapping mismatch")
        if role == "data" and (
            source_root != "data" or capture_path != f"data/{source_path}"
        ):
            raise MetricAuthorityError("domain evaluator data mapping mismatch")
        if not isinstance(item["sha256"], str) or _SHA256_RE.fullmatch(item["sha256"]) is None:
            raise MetricAuthorityError("domain evaluator package file hash mismatch")
        if type(item["size"]) is not int or item["size"] <= 0:
            raise MetricAuthorityError("domain evaluator package file size mismatch")
        capture_paths.append(capture_path)
        roles.append(item["role"])
    if capture_paths != sorted(set(capture_paths)):
        raise MetricAuthorityError("domain evaluator capture paths are not sorted and unique")
    if (
        roles.count("evaluator") != 1
        or roles.count("verifier") != 1
        or roles.count("policy") != 1
        or roles.count("vendor") != 7
        or roles.count("data") != 36
    ):
        raise MetricAuthorityError("domain evaluator package role cardinality mismatch")
    source_bytes = _capture_exact_package_sources(
        source_roots=expected_roots,
        declared_by_root=declared_by_root,
    )
    for item in files:
        content = source_bytes[(item["source_root"], item["source_path"])]
        if len(content) != item["size"] or _sha256_bytes(content) != item["sha256"]:
            raise MetricAuthorityError("domain evaluator package source bytes mismatch")
    reserved_manifest = source_bytes[("package", "package-manifest-v1.json")]
    if reserved_manifest != data:
        raise MetricAuthorityError("domain evaluator package manifest source mismatch")
    return value, source_bytes


def _capture_exact_package_sources(
    *,
    source_roots: list[dict[str, Any]],
    declared_by_root: Mapping[str, set[str]],
) -> dict[tuple[str, str], bytes]:
    root_paths = [item["trusted_path"] for item in source_roots]
    for index, left in enumerate(root_paths):
        left_parts = tuple(Path(left).parts)
        for right in root_paths[index + 1 :]:
            right_parts = tuple(Path(right).parts)
            if (
                left_parts == right_parts
                or left_parts == right_parts[: len(left_parts)]
                or right_parts == left_parts[: len(right_parts)]
            ):
                raise MetricAuthorityError("domain evaluator source roots overlap")

    base_fd = -1
    root_fds: list[tuple[str, str, int]] = []
    root_identities: set[tuple[int, int]] = set()
    file_identities: set[tuple[int, int]] = set()
    captured: dict[tuple[str, str], bytes] = {}
    try:
        base_fd = os.open(
            TRUSTED_SOURCE_BASE,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        for root in source_roots:
            root_id = root["root_id"]
            trusted_path = root["trusted_path"]
            root_fd = os.dup(base_fd)
            try:
                for segment in Path(trusted_path).parts:
                    next_fd = os.open(
                        segment,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=root_fd,
                    )
                    os.close(root_fd)
                    root_fd = next_fd
                identity = _fd_identity(root_fd)
                if identity in root_identities:
                    raise MetricAuthorityError("domain evaluator source root inode alias")
                root_identities.add(identity)
                root_fds.append((root_id, trusted_path, root_fd))
            except Exception:
                os.close(root_fd)
                raise

        for root, (root_id, trusted_path, root_fd) in zip(
            source_roots, root_fds, strict=True
        ):
            expected_files = set(declared_by_root[root_id]) | set(
                root["reserved_control_files"]
            )
            expected_dirs = {
                Path(path).parent.as_posix()
                for path in expected_files
                if Path(path).parent.as_posix() != "."
            }
            expected_dirs |= {
                parent.as_posix()
                for path in expected_files
                for parent in Path(path).parents
                if parent.as_posix() != "."
            }
            _walk_exact_source_root(
                root_id=root_id,
                directory_fd=root_fd,
                prefix="",
                expected_files=expected_files,
                expected_dirs=expected_dirs,
                file_identities=file_identities,
                captured=captured,
            )
            path_stat = os.stat(
                TRUSTED_SOURCE_BASE / trusted_path, follow_symlinks=False
            )
            if not stat.S_ISDIR(path_stat.st_mode) or (
                path_stat.st_dev,
                path_stat.st_ino,
            ) != _fd_identity(root_fd):
                raise MetricAuthorityError("domain evaluator source root identity changed")
        return captured
    except (OSError, ValueError) as exc:
        if isinstance(exc, MetricAuthorityError):
            raise
        raise MetricAuthorityError(
            f"domain evaluator source namespace is unsafe: {exc}"
        ) from exc
    finally:
        for _, _, root_fd in root_fds:
            os.close(root_fd)
        if base_fd >= 0:
            os.close(base_fd)


def _walk_exact_source_root(
    *,
    root_id: str,
    directory_fd: int,
    prefix: str,
    expected_files: set[str],
    expected_dirs: set[str],
    file_identities: set[tuple[int, int]],
    captured: dict[tuple[str, str], bytes],
) -> None:
    actual_entries = sorted(os.listdir(directory_fd))
    expected_entries = {
        path.split("/", 1)[0]
        for path in expected_files | expected_dirs
        if not prefix or path.startswith(f"{prefix}/")
        for path in [path[len(prefix) + 1 :] if prefix else path]
    }
    if set(actual_entries) != expected_entries:
        raise MetricAuthorityError("domain evaluator source namespace mismatch")

    for name in actual_entries:
        relative = f"{prefix}/{name}" if prefix else name
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if relative in expected_dirs:
            if not stat.S_ISDIR(entry_stat.st_mode):
                raise MetricAuthorityError("domain evaluator source directory is unsafe")
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                if _fd_identity(child_fd) != (entry_stat.st_dev, entry_stat.st_ino):
                    raise MetricAuthorityError("domain evaluator source directory changed")
                _walk_exact_source_root(
                    root_id=root_id,
                    directory_fd=child_fd,
                    prefix=relative,
                    expected_files=expected_files,
                    expected_dirs=expected_dirs,
                    file_identities=file_identities,
                    captured=captured,
                )
            finally:
                os.close(child_fd)
            continue
        if relative not in expected_files or not stat.S_ISREG(entry_stat.st_mode):
            raise MetricAuthorityError("domain evaluator source file is unsafe")
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        try:
            opened_stat = os.fstat(file_fd)
            identity = (opened_stat.st_dev, opened_stat.st_ino)
            if not stat.S_ISREG(opened_stat.st_mode) or identity != (
                entry_stat.st_dev,
                entry_stat.st_ino,
            ):
                raise MetricAuthorityError("domain evaluator source file changed")
            if identity in file_identities:
                raise MetricAuthorityError("domain evaluator source file inode alias")
            file_identities.add(identity)
            chunks: list[bytes] = []
            while chunk := os.read(file_fd, 1024 * 1024):
                chunks.append(chunk)
            final_stat = os.fstat(file_fd)
            if _stat_signature(final_stat) != _stat_signature(opened_stat):
                raise MetricAuthorityError("domain evaluator source file changed")
            captured[(root_id, relative)] = b"".join(chunks)
        finally:
            os.close(file_fd)
    if sorted(os.listdir(directory_fd)) != actual_entries:
        raise MetricAuthorityError("domain evaluator source namespace changed")


def _fd_identity(fd: int) -> tuple[int, int]:
    value = os.fstat(fd)
    return value.st_dev, value.st_ino


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _load_execution_policy_bytes(data: bytes) -> dict[str, Any]:
    value = _parse_json_object_bytes(data, "domain evaluator execution policy")
    expected_keys = {
        "schema_version", "execution_policy_version", "invocation_count", "seeds",
        "circuit_families", "variants_per_family", "conditions", "metric_keys",
        "primary_condition", "primary_metric_key", "primary_observation_set",
        "primary_aggregation", "execution_backend_policy", "raw_evidence_policy",
        "observation_policy", "aggregation_policy", "runtime_projection",
    }
    _exact_keys(value, expected_keys, "domain evaluator execution policy")
    expected = {
        "schema_version": 1,
        "execution_policy_version": 1,
        "invocation_count": 2,
        "seeds": [0, 1, 2],
        "circuit_families": ["c1355", "c1908", "c3540", "c432", "c6288", "c880"],
        "variants_per_family": 3,
        "conditions": ["raw_cc1", "scoap_isolation_forest", "trojnet_community_graphsage"],
        "metric_keys": ["accuracy", "auprc", "auroc", "f1", "fpr", "precision", "recall", "top_k_precision"],
        "primary_condition": "trojnet_community_graphsage",
        "primary_metric_key": "auprc",
        "primary_observation_set": "exact_18_variants_per_seed",
        "primary_aggregation": "mean_variants_then_mean_seeds_v1",
        "execution_backend_policy": "host_subprocess_trusted_consistency_v1",
        "raw_evidence_policy": "node_score_evidence_v1",
        "observation_policy": "trojnet_localization_metrics_v1",
        "aggregation_policy": "condition_seed_variant_mean_v1",
        "runtime_projection": {
            "python_major_minor": "3.11",
            "packages": {
                "networkx": "3.6.1", "numpy": "2.4.6", "scikit-learn": "1.9.0",
                "scipy": "1.17.1", "torch": "2.12.1", "torch-geometric": "2.8.0",
            },
            "device": "cpu",
            "torch_deterministic_algorithms": True,
            "torch_num_threads": 1,
        },
    }
    integer_fields = {
        "schema_version", "execution_policy_version", "invocation_count",
        "variants_per_family",
    }
    if any(type(value[field]) is not int for field in integer_fields):
        raise MetricAuthorityError("domain evaluator execution policy integer type mismatch")
    if not isinstance(value["seeds"], list) or any(
        type(seed) is not int for seed in value["seeds"]
    ):
        raise MetricAuthorityError("domain evaluator execution policy seed type mismatch")
    runtime_projection = value.get("runtime_projection")
    if not isinstance(runtime_projection, dict):
        raise MetricAuthorityError("domain evaluator runtime projection mismatch")
    if type(runtime_projection.get("torch_num_threads")) is not int:
        raise MetricAuthorityError("domain evaluator runtime thread type mismatch")
    if type(runtime_projection.get("torch_deterministic_algorithms")) is not bool:
        raise MetricAuthorityError("domain evaluator deterministic flag type mismatch")
    if value != expected:
        raise MetricAuthorityError("domain evaluator execution policy mismatch")
    return value


def _load_domain_selector_policy(path: Path) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "domain selector policy")
    _exact_keys(value, {"schema_version", "selector_policy_version", "normalization", "no_match", "rules"}, "domain selector policy")
    if type(value["schema_version"]) is not int or value["schema_version"] != DOMAIN_SELECTOR_SCHEMA_VERSION or value["selector_policy_version"] != DOMAIN_SELECTOR_POLICY_VERSION:
        raise MetricAuthorityError("domain selector policy version mismatch")
    if value["normalization"] != NORMALIZATION_POLICY or value["no_match"] != "error":
        raise MetricAuthorityError("domain selector policy semantics mismatch")
    rules = value["rules"]
    if not isinstance(rules, list) or not rules:
        raise MetricAuthorityError("domain selector rules must be a nonempty array")
    seen: set[str] = set()
    canonical_order: list[tuple[int, str]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise MetricAuthorityError("domain selector rule must be an object")
        _exact_keys(rule, {"rule_id", "pattern_kind", "normalized_pattern", "domain_id", "priority"}, "domain selector rule")
        rule_id = _ascii_id(rule["rule_id"], "domain selector rule_id")
        if rule_id in seen:
            raise MetricAuthorityError("duplicate domain selector rule_id")
        seen.add(rule_id)
        if rule["pattern_kind"] not in {"whole_word", "substring"}:
            raise MetricAuthorityError("invalid domain selector pattern_kind")
        pattern = rule["normalized_pattern"]
        if not isinstance(pattern, str) or not pattern or normalize_topic(pattern) != pattern:
            raise MetricAuthorityError("domain selector pattern is not normalized")
        _ascii_id(rule["domain_id"], "domain selector domain_id")
        priority = rule["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
            raise MetricAuthorityError("domain selector priority must be an integer in 0..1000")
        canonical_order.append((-priority, rule_id))
    if canonical_order != sorted(canonical_order):
        raise MetricAuthorityError("domain selector rules are not canonically ordered")
    return data, value


def _load_domain_profile(path: Path, expected_domain: str) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "canonical domain profile")
    _exact_keys(value, {"schema_version", "selector_policy_version", "domain_id", "owner", "supported_experiment_modes"}, "canonical domain profile")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["selector_policy_version"] != DOMAIN_SELECTOR_POLICY_VERSION:
        raise MetricAuthorityError("canonical domain profile version mismatch")
    if value["domain_id"] != expected_domain or value["owner"] != "scaffold":
        raise MetricAuthorityError("canonical domain profile identity mismatch")
    modes = value["supported_experiment_modes"]
    if not isinstance(modes, list) or not modes or modes != sorted(set(modes)) or not all(isinstance(item, str) and item for item in modes):
        raise MetricAuthorityError("canonical domain profile modes are invalid")
    return data, value


def _load_selector_index(path: Path) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "metric selector index")
    _exact_keys(value, {"schema_version", "selector_policy_version", "entries"}, "metric selector index")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["selector_policy_version"] != METRIC_SELECTOR_POLICY_VERSION:
        raise MetricAuthorityError("metric selector index version mismatch")
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise MetricAuthorityError("metric selector entries must be a nonempty array")
    tuples: list[tuple[str, str, str, str]] = []
    selectors: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MetricAuthorityError("metric selector entry must be an object")
        _exact_keys(entry, {"domain_id", "experiment_mode", "evaluator_kind", "evaluator_id"}, "metric selector entry")
        row = tuple(_ascii_id(entry[key], f"metric selector {key}") for key in ("domain_id", "experiment_mode", "evaluator_kind", "evaluator_id"))
        selector = row[:3]
        if selector in selectors:
            raise MetricAuthorityError("duplicate metric selector tuple")
        selectors.add(selector)
        tuples.append(row)
    if tuples != sorted(tuples):
        raise MetricAuthorityError("metric selector entries are not sorted")
    return data, value


def _load_metric_registry(path: Path, *, domain_id: str, evaluator_id: str) -> tuple[bytes, dict[str, Any]]:
    data, value = _load_json_object(path, "metric authority registry")
    _exact_keys(value, {"schema_version", "policy_version", "domain_id", "evaluator_id", "owner", "metrics"}, "metric authority registry")
    if type(value["schema_version"]) is not int or type(value["policy_version"]) is not int or value["schema_version"] != 1 or value["policy_version"] != METRIC_AUTHORITY_POLICY_VERSION:
        raise MetricAuthorityError("metric authority registry version mismatch")
    if value["domain_id"] != domain_id or value["evaluator_id"] != evaluator_id or value["owner"] != "scaffold":
        raise MetricAuthorityError("metric authority registry identity mismatch")
    metrics = value["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise MetricAuthorityError("metric authority metrics must be nonempty")
    keys: list[str] = []
    labels_seen: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            raise MetricAuthorityError("metric authority entry must be an object")
        _exact_keys(metric, {"key", "unit", "display_labels"}, "metric authority entry")
        key = metric["key"]
        if not isinstance(key, str) or _METRIC_KEY_RE.fullmatch(key) is None:
            raise MetricAuthorityError("invalid metric authority key")
        unit = metric["unit"]
        if unit not in {"ratio", "milliseconds", "seconds", "count"}:
            raise MetricAuthorityError("invalid metric authority unit")
        labels = metric["display_labels"]
        if not isinstance(labels, list) or not 1 <= len(labels) <= 8:
            raise MetricAuthorityError("metric display_labels must contain 1..8 items")
        for label in labels:
            if not isinstance(label, str) or not label or label != label.strip():
                raise MetricAuthorityError("invalid metric display label")
            normalized = normalize_topic(label)
            if normalized in labels_seen:
                raise MetricAuthorityError("duplicate normalized metric display label")
            labels_seen.add(normalized)
        keys.append(key)
    if keys != sorted(set(keys)):
        raise MetricAuthorityError("metric authority entries are not key-sorted")
    return data, value


def _load_json_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    data = _read_regular_bytes(path, label)
    return data, _parse_json_object_bytes(data, label)


def _parse_json_object_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MetricAuthorityError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise MetricAuthorityError(f"{label} root must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise MetricAuthorityError(f"{label} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MetricAuthorityError(f"cannot read {label}: {exc}") from exc


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise MetricAuthorityError(f"{label} fields mismatch")


def _ascii_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or _ASCII_ID_RE.fullmatch(value) is None:
        raise MetricAuthorityError(f"{label} must be a nonempty ASCII identifier")
    return value


def _package_relative(path: Path) -> str:
    return path.relative_to(Path(__file__).parents[1]).as_posix()


def _repo_relative(path: Path) -> str:
    return path.relative_to(Path(__file__).parents[2]).as_posix()


def _safe_relative_posix(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MetricAuthorityError(f"{label} must be a relative POSIX path")
    path = Path(value)
    if path.is_absolute() or path.as_posix() != value or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise MetricAuthorityError(f"{label} must be a normalized relative POSIX path")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
