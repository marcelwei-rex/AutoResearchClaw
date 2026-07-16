"""Experiment memory — records and retrieves experiment experiences."""

from __future__ import annotations

import json
import logging
import hashlib
from pathlib import Path
from typing import Any

from researchclaw.memory.retriever import MemoryRetriever
from researchclaw.memory.store import MemoryStore

logger = logging.getLogger(__name__)

CATEGORY = "experiment"


class ExperimentMemory:
    """Records and retrieves experiment experiences.

    Tracks hyperparameter configurations, model architectures, and
    training tricks that worked (or failed) in past runs.
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        retriever: MemoryRetriever | None = None,
        embed_fn: Any = None,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._embed_fn = embed_fn

    def record_hyperparams(
        self,
        task_type: str,
        hyperparams: dict[str, Any],
        metric: float,
        metric_name: str = "primary_metric",
        run_id: str = "",
    ) -> str:
        """Record an effective hyperparameter configuration.

        Args:
            task_type: Type of task (e.g., "image_classification").
            hyperparams: Dict of hyperparameter values.
            metric: Achieved metric value.
            metric_name: Name of the metric.
            run_id: Pipeline run identifier.

        Returns:
            The generated memory entry ID.
        """
        del task_type, hyperparams, metric, metric_name, run_id
        _reject_caller_supplied_authority("record_hyperparams")

    def record_architecture(
        self,
        task_type: str,
        architecture: str,
        metric: float,
        run_id: str = "",
    ) -> str:
        """Record a successful model architecture.

        Args:
            task_type: Type of task.
            architecture: Architecture description.
            metric: Achieved metric value.
            run_id: Pipeline run identifier.

        Returns:
            The generated memory entry ID.
        """
        del task_type, architecture, metric, run_id
        _reject_caller_supplied_authority("record_architecture")

    def record_training_trick(
        self,
        trick: str,
        improvement: float,
        context: str,
        run_id: str = "",
    ) -> str:
        """Record an effective training trick.

        Args:
            trick: Description of the trick.
            improvement: Relative improvement (e.g., 0.05 for 5%).
            context: When/where the trick was applied.
            run_id: Pipeline run identifier.

        Returns:
            The generated memory entry ID.
        """
        del trick, improvement, context, run_id
        _reject_caller_supplied_authority("record_training_trick")

    def record_release(
        self,
        run_dir: Path,
        *,
        task_type: str,
        run_id: str = "",
    ) -> str:
        """Persist exact metrics derived from one replayed canonical release."""
        _require_experiment_memory_capability("record_release")
        del task_type, run_id
        from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock

        with ReleaseGraphLock.acquire(
            run_dir, "ExperimentMemory.record_release", mode="write"
        ) as release_lock:
            projection = _load_projection(run_dir, "record_release")
            content = _serialize_memory_payload(_canonical_memory_payload(projection))
            manifest = _serialize_memory_manifest(projection, content)
            release_lock.ensure_run_directory("experiment_memory")
            with release_lock.open_stage_namespace("experiment_memory") as namespace:
                previous: dict[str, bytes] = {}
                if set(namespace.direct_entries()) == {
                    "canonical_release.json", "canonical_release_manifest.json"
                }:
                    previous = {
                        name: namespace.read_bytes(name)
                        for name in (
                            "canonical_release.json", "canonical_release_manifest.json"
                        )
                    }
                try:
                    namespace.invalidate(("canonical_release_manifest.json",))
                    namespace.reset_flat_namespace()
                    namespace.write_bytes_atomic("canonical_release.json", content)
                    namespace.write_bytes_atomic(
                        "canonical_release_manifest.json", manifest
                    )
                    _read_memory_publication(namespace, projection)
                    fresh_projection = _load_projection(run_dir, "record_release")
                    if fresh_projection != projection:
                        raise ValueError(
                            "canonical release changed during memory publication"
                        )
                    _read_memory_publication(namespace, fresh_projection)
                    namespace.assert_canonical()
                    release_lock.assert_canonical()
                except Exception as exc:
                    try:
                        namespace.invalidate(("canonical_release_manifest.json",))
                        namespace.reset_flat_namespace()
                        for name in (
                            "canonical_release.json", "canonical_release_manifest.json"
                        ):
                            if name in previous:
                                namespace.write_bytes_atomic(name, previous[name])
                    except Exception as cleanup_exc:  # noqa: BLE001
                        exc.add_note(f"memory publication rollback failed: {cleanup_exc}")
                    raise
        return hashlib.sha256(content).hexdigest()

    def recall_best_configs(
        self,
        task_type: str,
        top_k: int = 3,
        *,
        run_dir: Path | None = None,
    ) -> str:
        """Retrieve best configurations for a task type.

        Args:
            task_type: Description of the current task.
            top_k: Number of results.

        Returns:
            Formatted string of best configurations.
        """
        _require_experiment_memory_capability("recall_best_configs")
        if run_dir is None:
            raise ValueError("canonical experiment memory recall requires run_dir")
        del task_type, top_k
        from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock

        with ReleaseGraphLock.acquire(
            run_dir, "ExperimentMemory.recall_best_configs", mode="read"
        ) as release_lock:
            projection = _load_projection(run_dir, "recall_best_configs")
            with release_lock.open_stage_namespace("experiment_memory") as namespace:
                payload, raw = _read_memory_publication(namespace, projection)
                fresh_projection = _load_projection(run_dir, "recall_best_configs")
                if fresh_projection != projection:
                    raise ValueError("canonical release changed during memory replay")
                if _read_memory_publication(namespace, fresh_projection)[0] != payload:
                    raise ValueError("canonical memory changed during replay")
                namespace.assert_canonical()
            release_lock.assert_canonical()
        return "### Canonical Experiment Configuration\n" + raw.decode("utf-8").strip()


def _require_experiment_memory_capability(operation: str) -> None:
    from researchclaw.pipeline.canonical_evidence_capabilities import (
        require_canonical_evidence_capabilities,
    )

    require_canonical_evidence_capabilities(f"ExperimentMemory.{operation}")


def _reject_caller_supplied_authority(operation: str) -> None:
    _require_experiment_memory_capability(operation)
    raise PermissionError(
        "caller-supplied experiment authority is unsupported; use record_release"
    )


def _load_projection(run_dir: Path, operation: str):
    _require_experiment_memory_capability(operation)
    from researchclaw.pipeline.external_release_projection import (
        load_external_release_projection,
    )

    return load_external_release_projection(run_dir)


def _canonical_memory_payload(projection: Any) -> dict[str, Any]:
    from researchclaw.pipeline.external_release_projection import external_json_value

    return {
        "schema_version": 1,
        "metric_observations": external_json_value(projection.metric_observations),
        "binding": {
            "canonical_manifest_path": projection.canonical_manifest_path,
            "canonical_manifest_sha256": projection.canonical_manifest_sha256,
            "selected_result_manifest_path": projection.selected_result_manifest_path,
            "selected_result_manifest_sha256": projection.selected_result_manifest_sha256,
            "selected_execution_path": projection.selected_execution_path,
            "selected_execution_sha256": projection.selected_execution_sha256,
        },
    }


def _serialize_memory_payload(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _serialize_memory_manifest(projection: Any, content: bytes) -> bytes:
    payload = {
        "schema_version": 1,
        "canonical_manifest_path": projection.canonical_manifest_path,
        "canonical_manifest_sha256": projection.canonical_manifest_sha256,
        "memory_path": "experiment_memory/canonical_release.json",
        "memory_sha256": hashlib.sha256(content).hexdigest(),
    }
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _strict_memory_payload(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_memory_keys,
            parse_constant=_reject_memory_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("canonical experiment memory is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "metric_observations", "binding"
    }:
        raise ValueError("canonical experiment memory schema mismatch")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("canonical experiment memory version mismatch")
    if not isinstance(payload["metric_observations"], dict):
        raise ValueError("canonical experiment memory observations are invalid")
    binding = payload["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "canonical_manifest_path", "canonical_manifest_sha256",
        "selected_result_manifest_path", "selected_result_manifest_sha256",
        "selected_execution_path", "selected_execution_sha256",
    } or not all(isinstance(value, str) for value in binding.values()):
        raise ValueError("canonical experiment memory binding is invalid")
    if _serialize_memory_payload(payload) != raw:
        raise ValueError("canonical experiment memory bytes are not canonical")
    return payload


def _read_memory_publication(namespace: Any, projection: Any) -> tuple[dict[str, Any], bytes]:
    expected_entries = (
        "canonical_release.json", "canonical_release_manifest.json"
    )
    if namespace.direct_entries() != expected_entries:
        raise ValueError("canonical experiment memory namespace mismatch")
    raw = namespace.read_bytes("canonical_release.json")
    manifest_raw = namespace.read_bytes("canonical_release_manifest.json")
    payload = _strict_memory_payload(raw)
    expected_payload = _canonical_memory_payload(projection)
    if payload != expected_payload:
        raise ValueError("canonical experiment memory replay mismatch")
    expected_manifest = _serialize_memory_manifest(projection, raw)
    if manifest_raw != expected_manifest:
        raise ValueError("canonical experiment memory manifest mismatch")
    return payload, raw


def _reject_duplicate_memory_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate canonical memory key: {key}")
        result[key] = value
    return result


def _reject_memory_constant(value: str) -> Any:
    raise ValueError(f"non-finite canonical memory value is forbidden: {value}")
