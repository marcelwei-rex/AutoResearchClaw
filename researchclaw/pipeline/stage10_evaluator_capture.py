"""Immutable Stage 10 capture for code-owned domain evaluators."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import (
    ContractValidationError,
    ExperimentContract,
    derive_contract,
    load_contract_bytes,
    parse_contract_bytes,
)
from researchclaw.experiment_runtime.metric_authority import (
    DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH,
    DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH,
    DomainEvaluatorCapturePlan,
    MetricAuthorityError,
    build_domain_evaluator_capture_plan,
    replay_captured_domain_evaluator_authority,
    select_metric_authority,
)
from researchclaw.literature.citation_policy import (
    ConfigSnapshotNamespaceInputs,
    replay_config_snapshot_namespace,
)
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline import release_capture_profile as _profile
from researchclaw.pipeline.canonical_experiment_evidence import (
    CONFIG_SEMANTIC_POLICY_VERSION,
    parse_selected_candidate_manifest,
    semantic_config_sha256,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_release_graph_epoch,
    require_namespace_owned_by_epoch,
)


CAPTURE_DIRECTORY = "evaluator-capture-v1"
CAPTURE_MANIFEST = "capture-manifest.json"
DOMAIN_SEAL_SCHEMA_VERSION = 3
DOMAIN_SEAL_POLICY_VERSION = 2
@dataclass(frozen=True)
class CapturedEvaluatorMember:
    role: str; path: str; sha256: str
    size: int; content: bytes


@dataclass(frozen=True)
class _SourceAuthoritySnapshot:
    contract_path: str
    contract_bytes: bytes
    config_entries: tuple[tuple[str, bytes], ...]
    package_manifest_bytes: bytes
    execution_policy_bytes: bytes


@dataclass(frozen=True)
class _CandidateAuthoritySnapshot:
    source: _SourceAuthoritySnapshot
    seal_bytes: bytes
    capture_entries: tuple[tuple[str, bytes], ...]


class Stage10EvaluatorCaptureError(RuntimeError):
    """Raised when the immutable evaluator capture cannot be published."""


def load_stage10_contract(
    run_dir: Path,
) -> tuple[Path, ExperimentContract]:
    """Select and strictly load the Stage 9 contract through the held run fd."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage10.contract_load", mode="write"
    ) as release_lock:
        with release_lock.open_stage_namespace("stage-10", create_stage=True) as namespace:
            relative = _select_contract_relative(namespace)
            content = namespace.read_run_file(relative)
            contract = load_contract_bytes(content)
            namespace.assert_canonical()
            return run_dir / relative, contract


def publish_domain_evaluator_candidate(
    *,
    run_dir: Path,
    config: RCConfig,
) -> tuple[str, str]:
    """Capture trusted evaluator bytes and publish a manifest-last Stage 10 seal."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage10.domain_evaluator_capture", mode="write"
    ) as release_lock:
        release_lock.invalidate_experiment_commit_points()
        with release_lock.open_stage_namespace("stage-10", create_stage=True) as namespace:
            _reset_authority(namespace)
            try:
                source = _capture_source_authority(namespace)
                captured_config, contract, config_path, config_bytes = (
                    _replay_source_authority(
                        source,
                        runtime_config=config,
                        project_root=run_dir,
                    )
                )
                plan = build_domain_evaluator_capture_plan(
                    select_metric_authority(
                        captured_config.research.topic,
                        captured_config.experiment.mode,
                    )
                )
                manifest_bytes = _capture_manifest_bytes(contract, plan)
                _publish_capture_tree(namespace, plan, manifest_bytes)
                seal = _build_seal(
                    source=source,
                    config=captured_config,
                    config_path=config_path,
                    config_bytes=config_bytes,
                    contract=contract,
                    plan=plan,
                    capture_manifest_bytes=manifest_bytes,
                )
                seal_bytes = _canonical_json_bytes(seal)
                namespace.write_bytes_atomic(
                    "selected_candidate_manifest.json", seal_bytes
                )
                if parse_selected_candidate_manifest(seal_bytes.decode("utf-8")) != seal:
                    raise Stage10EvaluatorCaptureError(
                        "Stage 10 selected candidate seal grammar mismatch"
                    )
                first = _capture_candidate_authority(namespace)
                if _replay_candidate_snapshot(
                    first,
                    runtime_config=config,
                    project_root=run_dir,
                ) != seal:
                    raise Stage10EvaluatorCaptureError(
                        "Stage 10 selected candidate semantic replay mismatch"
                    )
                second = _capture_candidate_authority(namespace)
                if second != first:
                    raise Stage10EvaluatorCaptureError(
                        "Stage 10 authority changed during publication replay"
                    )
                namespace.assert_canonical()
            except Exception:
                _reset_authority(namespace)
                raise
    return f"{CAPTURE_DIRECTORY}/", "selected_candidate_manifest.json"


def invalidate_stage10_candidate_authority(run_dir: Path) -> None:
    """Invalidate both Stage 10 schema rows before any new candidate attempt."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage10.candidate_invalidation", mode="write"
    ) as release_lock:
        release_lock.invalidate_experiment_commit_points()
        with release_lock.open_stage_namespace("stage-10", create_stage=True) as namespace:
            _reset_authority(namespace)
            namespace.assert_canonical()


def clear_stage10_candidate_authority(namespace: BoundOutputNamespace) -> None:
    """Clear both Stage 10 schema rows through an already held namespace."""

    _reset_authority(namespace)


def replay_domain_evaluator_candidate(
    run_dir: Path, config: RCConfig
) -> dict[str, Any]:
    """Replay pre-activation M1 authority without authorizing Stage 12 use."""

    with ReleaseGraphLock.acquire(
        run_dir, "stage10.domain_evaluator_replay", mode="read"
    ) as release_lock:
        with release_lock.open_stage_namespace("stage-10") as namespace:
            first = _capture_candidate_authority(namespace)
            seal = _replay_domain_evaluator_candidate_snapshot(
                first,
                runtime_config=config, project_root=run_dir
            )
            second = _capture_candidate_authority(namespace)
            if second != first:
                raise Stage10EvaluatorCaptureError(
                    "Stage 10 authority changed during replay"
                )
            namespace.assert_canonical()
            return seal


def _capture_domain_evaluator_candidate_under_lock(
    namespace: BoundOutputNamespace,
    *,
    run_dir: Path,
    lease: object,
    expected_stage: str,
) -> _CandidateAuthoritySnapshot:
    """Capture Stage 10 authority only under a validated same-run graph epoch."""

    require_active_release_graph_epoch(run_dir, lease)
    if not isinstance(namespace, BoundOutputNamespace):
        raise RuntimeError("release_graph_namespace_required")
    if namespace.run_dir != run_dir:
        raise RuntimeError("release_graph_namespace_run_mismatch")
    require_namespace_owned_by_epoch(namespace, lease, expected_stage)
    seal_bytes = namespace.read_run_file(
        "stage-10/selected_candidate_manifest.json"
    )
    try:
        seal = parse_selected_candidate_manifest(seal_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Stage10EvaluatorCaptureError("Stage 10 seal is not UTF-8") from exc
    if seal.get("schema_version") != DOMAIN_SEAL_SCHEMA_VERSION:
        raise Stage10EvaluatorCaptureError(
            "domain evaluator Stage 10 seal v3 is required"
        )
    contract_path = seal["experiment_contract"]["path"]
    source = _SourceAuthoritySnapshot(
        contract_path=contract_path,
        contract_bytes=namespace.read_run_file(contract_path),
        config_entries=_capture_config_namespace(namespace),
        package_manifest_bytes=namespace.read_run_file(
            DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH
        ),
        execution_policy_bytes=namespace.read_run_file(
            DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH
        ),
    )
    return _CandidateAuthoritySnapshot(
        source=source,
        seal_bytes=seal_bytes,
        capture_entries=tuple(
            sorted(
                _capture_run_tree(
                    namespace, f"stage-10/{CAPTURE_DIRECTORY}"
                ).items()
            )
        ),
    )


def _replay_domain_evaluator_candidate_snapshot(
    snapshot: _CandidateAuthoritySnapshot,
    *,
    runtime_config: RCConfig,
    project_root: Path,
) -> dict[str, Any]:
    """Semantically replay one immutable Stage 10 authority snapshot."""

    return _replay_candidate_snapshot(
        snapshot,
        runtime_config=runtime_config,
        project_root=project_root,
    )


def _capture_run_tree(
    namespace: BoundOutputNamespace,
    root: str,
) -> dict[str, bytes]:
    """Capture one regular-file tree through the namespace's held run fd."""

    result: dict[str, bytes] = {}

    def walk(relative_dir: str, output_prefix: str) -> None:
        entries = namespace.read_run_directory_entries(relative_dir)
        for name in entries:
            relative = f"{relative_dir}/{name}"
            output = f"{output_prefix}/{name}" if output_prefix else name
            try:
                children = namespace.read_run_directory_entries(relative)
            except (NotADirectoryError, OSError):
                result[output] = namespace.read_run_file(relative)
            else:
                del children
                walk(relative, output)

    walk(root, "")
    return result


def _reset_authority(namespace: BoundOutputNamespace) -> None:
    errors: list[str] = []
    try:
        namespace.remove_flat_entries(("selected_candidate_manifest.json",))
    except OSError as exc:
        errors.append(str(exc))
    for tree in (CAPTURE_DIRECTORY, "selected_candidate"):
        try:
            namespace.quarantine_tree_entry(tree)
        except OSError as exc:
            errors.append(f"{tree}: {exc}")
    if errors:
        raise Stage10EvaluatorCaptureError(
            "Stage 10 evaluator authority cleanup failed: " + "; ".join(errors)
        )


def _capture_candidate_authority(
    namespace: BoundOutputNamespace,
) -> _CandidateAuthoritySnapshot:
    seal_bytes = namespace.read_bytes("selected_candidate_manifest.json")
    source = _capture_source_authority(namespace)
    capture_entries = tuple(
        sorted(namespace.read_directory_tree(CAPTURE_DIRECTORY).items())
    )
    return _CandidateAuthoritySnapshot(
        source=source,
        seal_bytes=seal_bytes,
        capture_entries=capture_entries,
    )


def _capture_source_authority(
    namespace: BoundOutputNamespace,
) -> _SourceAuthoritySnapshot:
    contract_path = _select_contract_relative(namespace)
    contract_bytes = namespace.read_run_file(contract_path)
    config_entries = _capture_config_namespace(namespace)
    package_manifest_bytes = namespace.read_run_file(
        DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH
    )
    execution_policy_bytes = namespace.read_run_file(
        DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH
    )
    return _SourceAuthoritySnapshot(
        contract_path=contract_path,
        contract_bytes=contract_bytes,
        config_entries=config_entries,
        package_manifest_bytes=package_manifest_bytes,
        execution_policy_bytes=execution_policy_bytes,
    )


def _select_contract_relative(namespace: BoundOutputNamespace) -> str:
    try:
        direct_entries = namespace.read_run_directory_entries("stage-09")
    except FileNotFoundError:
        direct_entries = ()
    if "experiment_contract.yaml" in direct_entries:
        return "stage-09/experiment_contract.yaml"
    if any(
        marker in direct_entries
        for marker in ("decision.json", "plan_meta.json", "stage_health.json")
    ):
        raise Stage10EvaluatorCaptureError(
            "canonical Stage 9 contract is missing from the live generation"
        )

    candidates: list[tuple[int, str]] = []
    for entry in namespace.run_entries():
        match = re.fullmatch(r"stage-09_v([1-9]\d*)", entry)
        if match is None:
            continue
        if "experiment_contract.yaml" in namespace.read_run_directory_entries(entry):
            candidates.append((int(match.group(1)), entry))
    if not candidates:
        raise Stage10EvaluatorCaptureError("canonical Stage 9 contract is missing")
    _version, stage_name = max(candidates)
    return f"{stage_name}/experiment_contract.yaml"


def _capture_config_namespace(
    namespace: BoundOutputNamespace,
) -> tuple[tuple[str, bytes], ...]:
    before = namespace.run_entries()
    _reject_noncanonical_resumed_configs(before)
    names = tuple(
        name
        for name in before
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
        or name
        in {
            "active_config_snapshot.json",
            "config_snapshot_history.jsonl",
            "checkpoint.json",
        }
    )
    entries = tuple((name, namespace.read_run_file(name)) for name in names)
    after = namespace.run_entries()
    _reject_noncanonical_resumed_configs(after)
    after_names = tuple(
        name
        for name in after
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
        or name
        in {
            "active_config_snapshot.json",
            "config_snapshot_history.jsonl",
            "checkpoint.json",
        }
    )
    if after_names != names:
        raise Stage10EvaluatorCaptureError(
            "active config namespace changed during capture"
        )
    return entries


def _reject_noncanonical_resumed_configs(entries: tuple[str, ...]) -> None:
    for name in entries:
        if name.startswith("config.resumed-") and re.fullmatch(
            r"config\.resumed-\d{8}-\d{6}\.yaml", name
        ) is None:
            raise Stage10EvaluatorCaptureError(
                f"noncanonical resumed config entry: {name}"
            )


def _replay_source_authority(
    source: _SourceAuthoritySnapshot,
    *,
    runtime_config: RCConfig,
    project_root: Path,
) -> tuple[RCConfig, ExperimentContract, str, bytes]:
    entry_map = dict(source.config_entries)
    if len(entry_map) != len(source.config_entries):
        raise Stage10EvaluatorCaptureError("duplicate active config namespace entry")

    def text(name: str) -> str | None:
        content = entry_map.get(name)
        if content is None:
            return None
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Stage10EvaluatorCaptureError(
                f"active config entry is not UTF-8: {name}"
            ) from exc

    snapshots = tuple(
        (name, value.decode("utf-8"))
        for name, value in source.config_entries
        if name == "config.yaml"
        or re.fullmatch(r"config\.resumed-\d{8}-\d{6}\.yaml", name)
    )
    try:
        captured_config, config_path, config_text, _digest, _snapshots = (
            replay_config_snapshot_namespace(
                ConfigSnapshotNamespaceInputs(
                    snapshots=snapshots,
                    pointer_text=text("active_config_snapshot.json"),
                    history_text=text("config_snapshot_history.jsonl"),
                    checkpoint_text=text("checkpoint.json"),
                ),
                project_root=project_root,
            )
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise Stage10EvaluatorCaptureError(
            f"active config reconstruction failed: {exc}"
        ) from exc
    if semantic_config_sha256(captured_config) != semantic_config_sha256(runtime_config):
        raise Stage10EvaluatorCaptureError(
            "active config differs from the Stage 10 runtime config"
        )
    try:
        raw_contract = parse_contract_bytes(source.contract_bytes)
        replayed_authority = replay_captured_domain_evaluator_authority(
            topic=captured_config.research.topic,
            experiment_mode=captured_config.experiment.mode,
            package_manifest_bytes=source.package_manifest_bytes,
            execution_policy_bytes=source.execution_policy_bytes,
            stored_identity=raw_contract.get("metric_authority", {}),
            metric_units=raw_contract.get("metric_units", {}),
            metric_display_labels=raw_contract.get("metric_display_labels", {}),
            evaluator_authority=raw_contract.get("evaluator_authority", {}),
        )
        contract = load_contract_bytes(
            source.contract_bytes,
            authority_selection=replayed_authority,
        )
        expected_contract = derive_contract(
            captured_config,
            None,
            authority_selection=replayed_authority,
        )
    except (ContractValidationError, MetricAuthorityError) as exc:
        raise Stage10EvaluatorCaptureError(
            f"captured Stage 9 contract is invalid: {exc}"
        ) from exc
    if contract.to_dict() != expected_contract.to_dict():
        raise Stage10EvaluatorCaptureError(
            "captured Stage 9 contract differs from deterministic derivation"
        )
    authority = contract.evaluator_authority
    if contract.schema_version != 3 or authority is None:
        raise Stage10EvaluatorCaptureError("domain evaluator contract v3 is required")
    if (
        authority["package_manifest_snapshot_path"]
        != DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH
        or authority["execution_policy_snapshot_path"]
        != DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH
    ):
        raise Stage10EvaluatorCaptureError("Stage 9 evaluator snapshot path mismatch")
    return captured_config, contract, config_path, config_text.encode("utf-8")


def _replay_candidate_snapshot(
    snapshot: _CandidateAuthoritySnapshot,
    *,
    runtime_config: RCConfig,
    project_root: Path,
) -> dict[str, Any]:
    try:
        seal = parse_selected_candidate_manifest(snapshot.seal_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Stage10EvaluatorCaptureError("Stage 10 seal is not UTF-8") from exc
    if snapshot.seal_bytes != _canonical_json_bytes(seal):
        raise Stage10EvaluatorCaptureError(
            "Stage 10 selected candidate seal is not canonical JSON"
        )
    if seal.get("schema_version") != DOMAIN_SEAL_SCHEMA_VERSION:
        raise Stage10EvaluatorCaptureError("Stage 10 seal is not domain evaluator v3")
    captured_config, contract, config_path, config_bytes = _replay_source_authority(
        snapshot.source,
        runtime_config=runtime_config,
        project_root=project_root,
    )
    if seal["experiment_contract"]["path"] != snapshot.source.contract_path:
        raise Stage10EvaluatorCaptureError("Stage 10 contract path mismatch")
    _require_ref_bytes(seal["experiment_contract"], snapshot.source.contract_bytes)
    if seal["run_config"]["path"] != config_path:
        raise Stage10EvaluatorCaptureError("Stage 10 active config path mismatch")
    _require_ref_bytes(seal["run_config"], config_bytes)
    authority = contract.evaluator_authority or {}
    if seal["package_manifest"]["path"] != authority[
        "package_manifest_snapshot_path"
    ]:
        raise Stage10EvaluatorCaptureError("Stage 10 package snapshot path mismatch")
    if seal["execution_policy"]["path"] != authority[
        "execution_policy_snapshot_path"
    ]:
        raise Stage10EvaluatorCaptureError("Stage 10 execution snapshot path mismatch")
    if seal["capture_manifest"]["path"] != (
        f"stage-10/{CAPTURE_DIRECTORY}/{CAPTURE_MANIFEST}"
    ):
        raise Stage10EvaluatorCaptureError("Stage 10 capture manifest path mismatch")
    if seal["config_semantic_sha256"] != semantic_config_sha256(captured_config):
        raise Stage10EvaluatorCaptureError("Stage 10 config semantic mismatch")
    _replay_candidate_from_captured_authority(
        capture_tree=dict(snapshot.capture_entries),
        seal=seal,
        contract=contract.to_dict(),
        package_manifest_bytes=snapshot.source.package_manifest_bytes,
        execution_policy_bytes=snapshot.source.execution_policy_bytes,
    )
    return seal


def _publish_capture_tree(
    namespace: BoundOutputNamespace,
    plan: DomainEvaluatorCapturePlan,
    manifest_bytes: bytes,
) -> None:
    with tempfile.TemporaryDirectory(prefix="researchclaw-stage10-capture-") as raw:
        source = Path(raw)
        for relative, content in sorted(plan.contents.items()):
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        namespace.publish_directory_tree(CAPTURE_DIRECTORY, source)
    if namespace.read_directory_tree(CAPTURE_DIRECTORY) != dict(plan.contents):
        raise Stage10EvaluatorCaptureError("published evaluator capture bytes mismatch")
    namespace.write_tree_file_atomic(
        CAPTURE_DIRECTORY, CAPTURE_MANIFEST, manifest_bytes
    )
    _replay_published_capture(namespace, plan, manifest_bytes)


def _replay_published_capture(
    namespace: BoundOutputNamespace,
    plan: DomainEvaluatorCapturePlan,
    manifest_bytes: bytes,
) -> None:
    actual = namespace.read_directory_tree(CAPTURE_DIRECTORY)
    expected = dict(plan.contents)
    expected[CAPTURE_MANIFEST] = manifest_bytes
    if actual != expected:
        raise Stage10EvaluatorCaptureError("evaluator capture namespace replay mismatch")
    parsed = _parse_capture_manifest(manifest_bytes)
    if parsed != json.loads(manifest_bytes):
        raise Stage10EvaluatorCaptureError("capture manifest replay mismatch")


def resolve_captured_evaluator_membership(
    *,
    package_manifest_bytes: bytes,
    execution_policy_bytes: bytes,
    capture_entries: tuple[tuple[str, bytes], ...],
) -> tuple[CapturedEvaluatorMember, ...]:
    """Resolve evaluator members from captured bytes without live authority."""

    if (type(package_manifest_bytes) is not bytes
            or type(execution_policy_bytes) is not bytes
            or type(capture_entries) is not tuple):
        raise Stage10EvaluatorCaptureError("captured evaluator inputs are invalid")
    capture_tree: dict[str, bytes] = {}
    paths: list[str] = []
    for entry in capture_entries:
        if (type(entry) is not tuple or len(entry) != 2
                or type(entry[0]) is not str or type(entry[1]) is not bytes):
            raise Stage10EvaluatorCaptureError("capture entry identity mismatch")
        path, content = entry
        paths.append(path)
        capture_tree[path] = content
    try:
        if _profile.canonical_order(tuple(paths)) != tuple(paths):
            raise Stage10EvaluatorCaptureError("capture entries are not canonical")
    except _profile.ReleaseProfileError as exc:
        raise Stage10EvaluatorCaptureError("capture entry identity mismatch") from exc

    package = _parse_captured_package_manifest(package_manifest_bytes)
    package_paths = tuple(item["capture_path"] for item in package["files"])
    try:
        if _profile.canonical_order(package_paths) != package_paths:
            raise Stage10EvaluatorCaptureError("package capture paths are not canonical")
    except _profile.ReleaseProfileError as exc:
        raise Stage10EvaluatorCaptureError("package capture path identity mismatch") from exc
    if (hashlib.sha256(execution_policy_bytes).hexdigest()
            != package["execution_policy_sha256"]):
        raise Stage10EvaluatorCaptureError("captured execution policy hash mismatch")
    try:
        capture_manifest_bytes = capture_tree.pop(CAPTURE_MANIFEST)
    except KeyError as exc:
        raise Stage10EvaluatorCaptureError("capture manifest is missing") from exc
    capture_manifest = _parse_capture_manifest(capture_manifest_bytes)
    expected_package_ref = _file_ref(
        DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH, package_manifest_bytes)
    expected_execution_ref = _file_ref(
        DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH, execution_policy_bytes)
    if capture_manifest["package_manifest"] != expected_package_ref:
        raise Stage10EvaluatorCaptureError("capture package manifest binding mismatch")
    if capture_manifest["execution_policy"] != expected_execution_ref:
        raise Stage10EvaluatorCaptureError("capture execution policy binding mismatch")
    if capture_manifest["evaluator_schema"] != package["evaluator_schema"]:
        raise Stage10EvaluatorCaptureError("capture evaluator schema mismatch")

    expected_files: dict[str, bytes] = {}
    expected_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    members: list[CapturedEvaluatorMember] = []
    for package_item in package["files"]:
        capture_path = package_item["capture_path"]
        try:
            content = capture_tree[capture_path]
        except KeyError as exc:
            raise Stage10EvaluatorCaptureError(
                f"captured evaluator file is missing: {capture_path}"
            ) from exc
        if (
            len(content) != package_item["size"]
            or hashlib.sha256(content).hexdigest() != package_item["sha256"]
        ):
            raise Stage10EvaluatorCaptureError(
                f"captured evaluator file bytes mismatch: {capture_path}"
            )
        expected_files[capture_path] = content
        package_entry = dict(package_item)
        expected_rows.append({
            "role": package_item["role"], "path": capture_path,
            "sha256": package_item["sha256"], "size": package_item["size"],
            "package_entry_sha256": hashlib.sha256(
                _canonical_json_bytes(package_entry)
            ).hexdigest(),
        })
        source_rows.append({
            "source_root": package_item["source_root"],
            "source_path": package_item["source_path"],
            "role": package_item["role"], "sha256": package_item["sha256"],
            "size": package_item["size"],
        })
        members.append(CapturedEvaluatorMember(
            package_item["role"], capture_path, package_item["sha256"],
            package_item["size"], content))
    if capture_tree != expected_files:
        raise Stage10EvaluatorCaptureError("capture contains an undeclared entry")
    if capture_manifest["files"] != expected_rows:
        raise Stage10EvaluatorCaptureError("capture file manifest mismatch")
    expected_namespace_sha = hashlib.sha256(
        _canonical_json_bytes(source_rows)
    ).hexdigest()
    if capture_manifest["source_namespace_sha256"] != expected_namespace_sha:
        raise Stage10EvaluatorCaptureError("capture source namespace hash mismatch")
    return tuple(members)


def _replay_candidate_from_captured_authority(
    *,
    capture_tree: dict[str, bytes],
    seal: dict[str, Any],
    contract: dict[str, Any],
    package_manifest_bytes: bytes,
    execution_policy_bytes: bytes,
) -> None:
    if contract["schema_version"] != 3:
        raise Stage10EvaluatorCaptureError("captured authority requires contract v3")
    evaluator_authority = contract["evaluator_authority"]
    if seal["metric_authority"] != contract["metric_authority"]:
        raise Stage10EvaluatorCaptureError("Stage 10 metric authority mismatch")
    if seal["evaluator_schema"] != evaluator_authority["evaluator_schema"]:
        raise Stage10EvaluatorCaptureError("Stage 10 evaluator schema mismatch")
    _require_ref_bytes(seal["package_manifest"], package_manifest_bytes)
    _require_ref_bytes(seal["execution_policy"], execution_policy_bytes)
    package = _parse_captured_package_manifest(package_manifest_bytes)
    if hashlib.sha256(execution_policy_bytes).hexdigest() != package[
        "execution_policy_sha256"
    ]:
        raise Stage10EvaluatorCaptureError("captured execution policy hash mismatch")
    try:
        capture_manifest_bytes = capture_tree.pop(CAPTURE_MANIFEST)
    except KeyError as exc:
        raise Stage10EvaluatorCaptureError("capture manifest is missing") from exc
    _require_ref_bytes(seal["capture_manifest"], capture_manifest_bytes)
    capture_manifest = _parse_capture_manifest(capture_manifest_bytes)
    if capture_manifest["package_manifest"] != seal["package_manifest"]:
        raise Stage10EvaluatorCaptureError("capture package manifest binding mismatch")
    if capture_manifest["execution_policy"] != seal["execution_policy"]:
        raise Stage10EvaluatorCaptureError("capture execution policy binding mismatch")
    if capture_manifest["evaluator_schema"] != seal["evaluator_schema"]:
        raise Stage10EvaluatorCaptureError("capture evaluator schema mismatch")

    resolve_captured_evaluator_membership(
        package_manifest_bytes=package_manifest_bytes,
        execution_policy_bytes=execution_policy_bytes,
        capture_entries=tuple(sorted(
            ((CAPTURE_MANIFEST, capture_manifest_bytes), *capture_tree.items())
        )),
    )


def _capture_manifest_bytes(
    contract: ExperimentContract, plan: DomainEvaluatorCapturePlan
) -> bytes:
    authority = contract.evaluator_authority or {}
    value = {
        "schema_version": 1,
        "capture_policy_version": 1,
        "package_manifest": {
            "path": DOMAIN_EVALUATOR_PACKAGE_MANIFEST_SNAPSHOT_PATH,
            "sha256": authority["package_manifest_snapshot_sha256"],
            "size": len(plan.package_manifest_bytes),
        },
        "execution_policy": {
            "path": DOMAIN_EVALUATOR_EXECUTION_POLICY_SNAPSHOT_PATH,
            "sha256": authority["execution_policy_snapshot_sha256"],
            "size": len(plan.execution_policy_bytes),
        },
        "evaluator_schema": authority["evaluator_schema"],
        "source_namespace_sha256": plan.source_namespace_sha256,
        "files": list(plan.files),
    }
    return _canonical_json_bytes(value)


def _build_seal(
    *,
    source: _SourceAuthoritySnapshot,
    config: RCConfig,
    config_path: str,
    config_bytes: bytes,
    contract: ExperimentContract,
    plan: DomainEvaluatorCapturePlan,
    capture_manifest_bytes: bytes,
) -> dict[str, Any]:
    authority = contract.evaluator_authority or {}
    return {
        "schema_version": DOMAIN_SEAL_SCHEMA_VERSION,
        "seal_policy_version": DOMAIN_SEAL_POLICY_VERSION,
        "candidate_kind": "domain_evaluator",
        "experiment_contract": _file_ref(
            source.contract_path,
            source.contract_bytes,
        ),
        "run_config": _file_ref(config_path, config_bytes),
        "config_semantic_policy_version": CONFIG_SEMANTIC_POLICY_VERSION,
        "config_semantic_sha256": semantic_config_sha256(config),
        "metric_authority": contract.metric_authority,
        "package_manifest": _file_ref(
            authority["package_manifest_snapshot_path"],
            plan.package_manifest_bytes,
            expected_sha=authority["package_manifest_snapshot_sha256"],
        ),
        "execution_policy": _file_ref(
            authority["execution_policy_snapshot_path"],
            plan.execution_policy_bytes,
            expected_sha=authority["execution_policy_snapshot_sha256"],
        ),
        "capture_manifest": _file_ref(
            f"stage-10/{CAPTURE_DIRECTORY}/{CAPTURE_MANIFEST}",
            capture_manifest_bytes,
        ),
        "evaluator_schema": authority["evaluator_schema"],
    }


def _parse_capture_manifest(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage10EvaluatorCaptureError(f"capture manifest is invalid: {exc}") from exc
    expected = {
        "schema_version", "capture_policy_version", "package_manifest",
        "execution_policy", "evaluator_schema", "source_namespace_sha256", "files",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise Stage10EvaluatorCaptureError("capture manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage10EvaluatorCaptureError("capture manifest schema mismatch")
    if type(value["capture_policy_version"]) is not int or value["capture_policy_version"] != 1:
        raise Stage10EvaluatorCaptureError("capture policy version mismatch")
    _parse_file_ref(value["package_manifest"], "package_manifest")
    _parse_file_ref(value["execution_policy"], "execution_policy")
    _sha256(value["source_namespace_sha256"], "source_namespace_sha256")
    if not isinstance(value["evaluator_schema"], str) or not value["evaluator_schema"]:
        raise Stage10EvaluatorCaptureError("capture evaluator_schema mismatch")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise Stage10EvaluatorCaptureError("capture files must be nonempty")
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "role", "path", "sha256", "size", "package_entry_sha256"
        }:
            raise Stage10EvaluatorCaptureError("capture file fields mismatch")
        if not isinstance(item["role"], str) or not item["role"]:
            raise Stage10EvaluatorCaptureError("capture file role mismatch")
        _safe_path(item["path"], "capture file path")
        _sha256(item["sha256"], "capture file sha256")
        _sha256(item["package_entry_sha256"], "package_entry_sha256")
        if type(item["size"]) is not int or item["size"] <= 0:
            raise Stage10EvaluatorCaptureError("capture file size mismatch")
        paths.append(item["path"])
    if paths != sorted(set(paths)):
        raise Stage10EvaluatorCaptureError("capture files are not sorted and unique")
    return value


def _parse_captured_package_manifest(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage10EvaluatorCaptureError(f"package manifest is invalid: {exc}") from exc
    expected = {
        "schema_version", "package_policy_version", "domain_id", "evaluator_id",
        "evaluator_schema", "execution_policy_path", "execution_policy_sha256",
        "source_roots", "files",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise Stage10EvaluatorCaptureError("package manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage10EvaluatorCaptureError("package manifest schema mismatch")
    if type(value["package_policy_version"]) is not int or value["package_policy_version"] != 1:
        raise Stage10EvaluatorCaptureError("package policy version mismatch")
    _sha256(value["execution_policy_sha256"], "execution_policy_sha256")
    if not isinstance(value["files"], list) or not value["files"]:
        raise Stage10EvaluatorCaptureError("package manifest files mismatch")
    capture_paths: list[str] = []
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {
            "source_root", "source_path", "capture_path", "role", "sha256", "size"
        }:
            raise Stage10EvaluatorCaptureError("package manifest file fields mismatch")
        for field in ("source_root", "source_path", "capture_path", "role"):
            if not isinstance(item[field], str) or not item[field]:
                raise Stage10EvaluatorCaptureError("package manifest file identity mismatch")
        _safe_path(item["source_path"], "package source_path")
        _safe_path(item["capture_path"], "package capture_path")
        _sha256(item["sha256"], "package file sha256")
        if type(item["size"]) is not int or item["size"] <= 0:
            raise Stage10EvaluatorCaptureError("package file size mismatch")
        capture_paths.append(item["capture_path"])
    if capture_paths != sorted(set(capture_paths)):
        raise Stage10EvaluatorCaptureError("package capture paths mismatch")
    return value


def _file_ref(path: str, content: bytes, *, expected_sha: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha is not None and digest != expected_sha:
        raise Stage10EvaluatorCaptureError(f"file reference hash mismatch: {path}")
    return {"path": path, "sha256": digest, "size": len(content)}


def _parse_file_ref(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise Stage10EvaluatorCaptureError(f"{label} file reference mismatch")
    _safe_path(value["path"], f"{label}.path")
    _sha256(value["sha256"], f"{label}.sha256")
    if type(value["size"]) is not int or value["size"] <= 0:
        raise Stage10EvaluatorCaptureError(f"{label}.size mismatch")


def _safe_path(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Stage10EvaluatorCaptureError(f"{label} is unsafe")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise Stage10EvaluatorCaptureError(f"{label} is unsafe")


def _sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage10EvaluatorCaptureError(f"{label} mismatch")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _require_ref_bytes(reference: dict[str, Any], content: bytes) -> None:
    if reference["size"] != len(content) or reference["sha256"] != hashlib.sha256(content).hexdigest():
        raise Stage10EvaluatorCaptureError(
            f"file reference bytes mismatch: {reference['path']}"
        )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
