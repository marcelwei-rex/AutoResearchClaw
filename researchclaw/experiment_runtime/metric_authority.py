"""Code-owned metric authority selection and run-local snapshot replay."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace


DOMAIN_SELECTOR_SCHEMA_VERSION = 1
DOMAIN_SELECTOR_POLICY_VERSION = "domain_selector_v1"
METRIC_SELECTOR_POLICY_VERSION = "metric_selector_v1"
METRIC_AUTHORITY_POLICY_VERSION = 1
NORMALIZATION_POLICY = "nfkc_casefold_space_v1"

PACKAGE_ROOT = Path(__file__).with_name("metric_authority")
DOMAIN_SELECTOR_PACKAGE_PATH = PACKAGE_ROOT / "domain-selector-v1.json"
METRIC_SELECTOR_INDEX_PATH = PACKAGE_ROOT / "index-v1.json"

DOMAIN_SELECTOR_SNAPSHOT_PATH = "stage-09/domain_selector_policy.json"
DOMAIN_PROFILE_SNAPSHOT_PATH = "stage-09/domain_profile.json"
METRIC_SELECTOR_INDEX_SNAPSHOT_PATH = "stage-09/metric_authority_index.json"
METRIC_AUTHORITY_SNAPSHOT_PATH = "stage-09/metric_authority.json"

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
    selector_policy_version: str
    metric_units: dict[str, str]
    metric_display_labels: dict[str, list[str]]

    def contract_identity(self) -> dict[str, Any]:
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


def normalize_topic(topic: str) -> str:
    if not isinstance(topic, str):
        raise MetricAuthorityError("research topic must be a string")
    normalized = unicodedata.normalize("NFKC", topic).casefold().strip()
    return " ".join(normalized.split())


def select_metric_authority(topic: str, experiment_mode: str) -> MetricAuthoritySelection:
    """Select both authority levels exclusively from trusted package policy."""
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


def publish_metric_authority_snapshots(
    stage_dir: Path,
    selection: MetricAuthoritySelection,
    *,
    namespace: BoundOutputNamespace | None = None,
) -> None:
    """Copy trusted package bytes into the four fixed Stage 9 snapshot paths."""
    if stage_dir.name != "stage-09":
        raise MetricAuthorityError("metric authority snapshots require canonical stage-09")
    sources = {
        "domain_selector_policy.json": DOMAIN_SELECTOR_PACKAGE_PATH,
        "domain_profile.json": PACKAGE_ROOT / "profiles" / f"{selection.domain_id}-v1.json",
        "metric_authority_index.json": METRIC_SELECTOR_INDEX_PATH,
        "metric_authority.json": PACKAGE_ROOT / f"{selection.evaluator_id}-v1.json",
    }
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
    snapshot_sources = {
        selection.domain_selector_snapshot_path: DOMAIN_SELECTOR_PACKAGE_PATH,
        selection.domain_profile_path: PACKAGE_ROOT / "profiles" / f"{selection.domain_id}-v1.json",
        selection.selector_index_path: METRIC_SELECTOR_INDEX_PATH,
        selection.authority_path: PACKAGE_ROOT / f"{selection.evaluator_id}-v1.json",
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
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MetricAuthorityError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise MetricAuthorityError(f"{label} root must be an object")
    return data, value


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
