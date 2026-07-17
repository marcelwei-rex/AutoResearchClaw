"""Canonical Stage 12 execution for immutable domain evaluators."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import fcntl
import ctypes
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import parse_contract_bytes
from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidenceError,
    canonical_authority_json_text,
    semantic_config_sha256,
    parse_selected_candidate_manifest,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_release_graph_epoch,
    require_namespace_owned_by_epoch,
)
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.stage10_evaluator_capture import (
    _CandidateAuthoritySnapshot,
    _capture_domain_evaluator_candidate_under_lock,
    _replay_domain_evaluator_candidate_snapshot,
)


SCORE_LIMIT = 128 * 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
SCORE_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
EXPECTED_METRICS = (
    "accuracy",
    "auprc",
    "auroc",
    "f1",
    "fpr",
    "precision",
    "recall",
    "top_k_precision",
)


class Stage12DomainEvaluatorError(RuntimeError):
    """Raised when trusted evaluator execution or replay fails closed."""


@dataclass(frozen=True)
class _Stage12AuthoritySnapshot:
    manifest_bytes: bytes
    journal_bytes: bytes
    evidence_entries: tuple[tuple[str, bytes], ...]
    stage10: _CandidateAuthoritySnapshot


@dataclass
class _BoundPythonExecutable:
    configured_path: str
    resolved_path: str
    execution_path: str
    descriptor: int
    size: int
    sha256: str
    pass_fds: tuple[int, ...]
    binding_policy: str
    darwin_cdhash: bytes | None = None
    cleanup_root: Path | None = None
    immutable_path: Path | None = None

    def assert_bound(self) -> None:
        info = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != self.size
            or _sha256_fd(self.descriptor, self.size) != self.sha256
        ):
            raise Stage12DomainEvaluatorError("bound Python executable changed")
        if self.immutable_path is not None:
            live = os.stat(self.immutable_path, follow_symlinks=False)
            if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
                raise Stage12DomainEvaluatorError(
                    "bound Python executable path changed"
                )


def stage12_uses_domain_evaluator(run_dir: Path) -> bool:
    """Invalidate stale authority, then dispatch only on the strict seal schema."""

    require_canonical_evidence_capabilities("stage12.domain_evaluator_dispatch")
    with ReleaseGraphLock.acquire(
        run_dir, "stage12.candidate_dispatch", mode="write"
    ) as release_lock:
        release_lock.invalidate_experiment_commit_points()
        release_lock.ensure_run_directory("stage-12")
        with release_lock.open_stage_namespace("stage-12") as namespace:
            try:
                content = namespace.read_run_file(
                    "stage-10/selected_candidate_manifest.json"
                )
            except (FileNotFoundError, OSError) as exc:
                raise Stage12DomainEvaluatorError(
                    "sealed candidate manifest missing"
                ) from exc
            try:
                manifest = parse_selected_candidate_manifest(content.decode("utf-8"))
            except (UnicodeDecodeError, CanonicalExperimentEvidenceError) as exc:
                raise Stage12DomainEvaluatorError(
                    f"sealed candidate manifest invalid: {exc}"
                ) from exc
            namespace.assert_canonical()
            return manifest["schema_version"] == 3


def execute_domain_evaluator_stage12(
    *,
    run_dir: Path,
    config: RCConfig,
) -> tuple[str, ...]:
    """Run the captured evaluator twice and publish verifier-derived evidence."""

    require_canonical_evidence_capabilities("stage12.domain_evaluator_execute")
    with ReleaseGraphLock.acquire(
        run_dir, "stage12.domain_evaluator", mode="write"
    ) as release_lock:
        release_lock.invalidate_experiment_commit_points()
        release_lock.roll_stage_generation("stage-12")
        with release_lock.open_stage_namespace("stage-12") as namespace:
            _reset_stage12(namespace)
            try:
                stage10 = _capture_domain_evaluator_candidate_under_lock(
                    namespace,
                    run_dir=run_dir,
                    lease=release_lock,
                    expected_stage="stage-12",
                )
                seal = _replay_domain_evaluator_candidate_snapshot(
                    stage10, runtime_config=config, project_root=run_dir
                )
                artifacts = _produce_under_namespace(
                    namespace=namespace,
                    run_dir=run_dir,
                    config=config,
                    lease=release_lock,
                    stage10=stage10,
                    seal=seal,
                    capture=dict(stage10.capture_entries),
                )
                namespace.assert_canonical()
                return artifacts
            except Exception:
                _reset_stage12(namespace)
                raise


def _produce_under_namespace(
    *,
    namespace: BoundOutputNamespace,
    run_dir: Path,
    config: RCConfig,
    lease: object,
    stage10: _CandidateAuthoritySnapshot,
    seal: dict[str, Any],
    capture: dict[str, bytes],
) -> tuple[str, ...]:
    require_active_release_graph_epoch(run_dir, lease)
    contract_bytes = stage10.source.contract_bytes
    contract = parse_contract_bytes(contract_bytes)
    if contract.get("schema_version") != 3 or not isinstance(
        contract.get("evaluator_authority"), dict
    ):
        raise Stage12DomainEvaluatorError("domain evaluator contract v3 is required")
    policy_bytes = capture["policy/execution-policy-v1.json"]
    policy = _strict_json_object(policy_bytes, "execution policy")
    _validate_execution_policy(policy)
    capture_manifest_bytes = capture["capture-manifest.json"]
    capture_sha = hashlib.sha256(capture_manifest_bytes).hexdigest()
    seal_sha = hashlib.sha256(stage10.seal_bytes).hexdigest()
    reference_runtime: dict[str, Any] | None = None

    journal: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []
    verification_bytes: list[bytes] = []
    publication_root = Path(tempfile.mkdtemp(prefix="researchclaw-stage12-v2-publish-"))
    evidence_root = publication_root / "evidence-v2"
    evidence_root.mkdir()
    try:
        with _bound_python_executable(config) as bound_python:
          for ordinal in (1, 2):
            runtime = _capture_runtime_attestation(bound_python, policy)
            if reference_runtime is None:
                reference_runtime = runtime
            elif runtime != reference_runtime:
                raise Stage12DomainEvaluatorError(
                    "runtime projection changed across evaluator invocations"
                )
            runtime_bytes = _canonical_json_bytes(runtime)
            runtime_sha = hashlib.sha256(runtime_bytes).hexdigest()
            token = secrets.token_hex(32)
            generation_binding = _generation_binding_sha256(
                ordinal=ordinal,
                invocation_token=token,
                seal=seal,
                contract_sha256=hashlib.sha256(contract_bytes).hexdigest(),
                sealed_candidate_manifest_sha256=seal_sha,
                runtime_attestation_sha256=runtime_sha,
            )
            started = {
                "schema_version": 2,
                "event": "started",
                "ordinal": ordinal,
                "invocation_token": token,
                "generation_binding_sha256": generation_binding,
                "experiment_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
                "sealed_candidate_manifest_sha256": seal_sha,
                "capture_manifest_sha256": capture_sha,
                "execution_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                "runtime_attestation_sha256": runtime_sha,
                "config_semantic_sha256": semantic_config_sha256(config),
                "evaluator_schema": seal["evaluator_schema"],
            }
            journal.append(started)
            _write_journal(namespace, journal)
            try:
                invocation_dir = evidence_root / f"invocation-{ordinal}"
                invocation_dir.mkdir()
                score_bytes, command_sha = _run_evaluator(
                    bound_python=bound_python,
                    capture=capture,
                    output_dir=invocation_dir,
                )
                if _capture_runtime_attestation(bound_python, policy) != runtime:
                    raise Stage12DomainEvaluatorError(
                        "runtime projection changed during evaluator invocation"
                    )
                _validate_score_evidence(score_bytes, policy)
                meta = {
                    "schema_version": 2,
                    "execution_meta_policy_version": 1,
                    "ordinal": ordinal,
                    "invocation_token": token,
                    "evaluator_command_sha256": command_sha,
                    "backend_policy": policy["execution_backend_policy"],
                    "runtime_attestation": runtime,
                    "capture_manifest_sha256": capture_sha,
                    "execution_policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                }
                meta_bytes = _canonical_json_bytes(meta)
                (invocation_dir / "execution_meta.json").write_bytes(meta_bytes)
                verification = _run_verifier(
                    bound_python=bound_python,
                    capture=capture,
                    score_bytes=score_bytes,
                    dataset_capture_sha256=capture_sha,
                    ordinal=ordinal,
                )
                verification_bytes.append(verification)
                terminal = {
                    "schema_version": 2,
                    "event": "terminal",
                    "ordinal": ordinal,
                    "invocation_token": token,
                    "status": "completed",
                    "score_evidence_path": (
                        f"stage-12/evidence-v2/invocation-{ordinal}/score_evidence.jsonl"
                    ),
                    "score_evidence_sha256": hashlib.sha256(score_bytes).hexdigest(),
                    "execution_meta_path": (
                        f"stage-12/evidence-v2/invocation-{ordinal}/execution_meta.json"
                    ),
                    "execution_meta_sha256": hashlib.sha256(meta_bytes).hexdigest(),
                    "failure_code": None,
                }
                journal.append(terminal)
                _write_journal(namespace, journal)
                invocations.append(
                    {
                        "ordinal": ordinal,
                        "token": token,
                        "generation_binding_sha256": generation_binding,
                        "score_bytes": score_bytes,
                        "meta_bytes": meta_bytes,
                    }
                )
            except Exception as exc:
                journal.append(
                    {
                        "schema_version": 2,
                        "event": "terminal",
                        "ordinal": ordinal,
                        "invocation_token": token,
                        "status": "failed",
                        "score_evidence_path": None,
                        "score_evidence_sha256": None,
                        "execution_meta_path": None,
                        "execution_meta_sha256": None,
                        "failure_code": type(exc).__name__,
                    }
                )
                _write_journal(namespace, journal)
                raise

        if invocations[0]["score_bytes"] != invocations[1]["score_bytes"]:
            raise Stage12DomainEvaluatorError(
                "two evaluator invocations produced different raw score evidence"
            )
        if verification_bytes[0] != verification_bytes[1]:
            raise Stage12DomainEvaluatorError(
                "independent verifier outputs differ across invocations"
            )
        for ordinal, content in enumerate(verification_bytes, start=1):
            (evidence_root / f"verification-{ordinal}.json").write_bytes(content)
        observations_bytes = verification_bytes[0]
        (evidence_root / "observations.json").write_bytes(observations_bytes)
        observations = _strict_authority_json(observations_bytes, "observations")
        run_refs: list[dict[str, Any]] = []
        for invocation in invocations:
            ordinal = invocation["ordinal"]
            run_payload = {
                "schema_version": 2,
                "run_policy_version": 2,
                "ordinal": ordinal,
                "invocation_token": invocation["token"],
                "generation_binding_sha256": invocation["generation_binding_sha256"],
                "capture_manifest": seal["capture_manifest"],
                "score_evidence": _ref(
                    f"stage-12/evidence-v2/invocation-{ordinal}/score_evidence.jsonl",
                    invocation["score_bytes"],
                ),
                "execution_meta": _ref(
                    f"stage-12/evidence-v2/invocation-{ordinal}/execution_meta.json",
                    invocation["meta_bytes"],
                ),
                "observations": _ref(
                    "stage-12/evidence-v2/observations.json", observations_bytes
                ),
                "primary_metric": observations["primary_metric"],
            }
            run_bytes = _authority_json_bytes(run_payload)
            (evidence_root / f"run-{ordinal}.json").write_bytes(run_bytes)
            run_refs.append(
                _ref(f"stage-12/evidence-v2/run-{ordinal}.json", run_bytes)
            )
        results_payload = {
            "schema_version": 2,
            "results_policy_version": 2,
            "runs": run_refs,
            "observations": _ref(
                "stage-12/evidence-v2/observations.json", observations_bytes
            ),
            "structured_results": observations["aggregate"],
            "primary_metric": observations["primary_metric"],
        }
        results_bytes = _authority_json_bytes(results_payload)
        (evidence_root / "results.json").write_bytes(results_bytes)

        namespace.publish_directory_tree("evidence-v2", evidence_root)
        manifest = _build_result_set_manifest(
            config=config,
            contract=contract,
            seal=seal,
            stage10_seal_bytes=stage10.seal_bytes,
            journal=journal,
            evidence_root=evidence_root,
            observations_bytes=observations_bytes,
            results_bytes=results_bytes,
            observations=observations,
        )
        manifest_text = _authority_json_text(manifest)
        _validate_domain_evaluator_result_set_under_lock(
            run_dir, config, namespace=namespace, lease=lease,
            manifest_override=manifest_text.encode("utf-8"),
        )
        namespace.write_text_atomic("experiment_result_set.json", manifest_text)
        _validate_domain_evaluator_result_set_under_lock(
            run_dir, config, namespace=namespace, lease=lease
        )
        return (
            "experiment_result_set.json",
            "execution_invocation_journal.jsonl",
            "evidence-v2/",
        )
    finally:
        shutil.rmtree(publication_root, ignore_errors=True)


def validate_domain_evaluator_result_set(
    run_dir: Path,
    config: RCConfig,
    *,
    text: str | None = None,
) -> dict[str, Any]:
    """Public replay that always owns the reader epoch and disk snapshot."""

    require_canonical_evidence_capabilities("stage12.domain_evaluator_replay")
    with ReleaseGraphLock.acquire(
        run_dir, "stage12.domain_evaluator_replay", mode="read"
    ) as release_lock:
        with release_lock.open_stage_namespace("stage-12") as namespace:
            return _validate_domain_evaluator_result_set_under_lock(
                run_dir,
                config,
                namespace=namespace,
                lease=release_lock,
                expected_manifest=(text.encode("utf-8") if text is not None else None),
            )


def _capture_stage12_v2_authority(
    namespace: BoundOutputNamespace,
    *,
    run_dir: Path,
    lease: object,
    manifest_override: bytes | None = None,
) -> _Stage12AuthoritySnapshot:
    require_namespace_owned_by_epoch(namespace, lease, "stage-12")
    manifest_bytes = (
        manifest_override
        if manifest_override is not None
        else namespace.read_bytes("experiment_result_set.json")
    )
    journal_bytes = namespace.read_bytes("execution_invocation_journal.jsonl")
    evidence_entries = tuple(
        sorted(namespace.read_directory_tree("evidence-v2").items())
    )
    stage10 = _capture_domain_evaluator_candidate_under_lock(
        namespace,
        run_dir=run_dir,
        lease=lease,
        expected_stage="stage-12",
    )
    return _Stage12AuthoritySnapshot(
        manifest_bytes=manifest_bytes,
        journal_bytes=journal_bytes,
        evidence_entries=evidence_entries,
        stage10=stage10,
    )


def _validate_domain_evaluator_result_set_under_lock(
    run_dir: Path,
    config: RCConfig,
    *,
    namespace: BoundOutputNamespace,
    lease: object,
    manifest_override: bytes | None = None,
    expected_manifest: bytes | None = None,
) -> dict[str, Any]:
    """A/B replay under a validated release-graph reader or writer epoch."""

    require_namespace_owned_by_epoch(namespace, lease, "stage-12")
    first = _capture_stage12_v2_authority(
        namespace,
        run_dir=run_dir,
        lease=lease,
        manifest_override=manifest_override,
    )
    if expected_manifest is not None and first.manifest_bytes != expected_manifest:
        raise Stage12DomainEvaluatorError("Stage 12 manifest text/disk mismatch")
    with _bound_python_executable(config) as bound_python:
        payload = _replay_domain_evaluator_snapshot(
            first, run_dir=run_dir, config=config, bound_python=bound_python
        )
    second = _capture_stage12_v2_authority(
        namespace,
        run_dir=run_dir,
        lease=lease,
        manifest_override=manifest_override,
    )
    if second != first:
        raise Stage12DomainEvaluatorError(
            "Stage 10-12 authority changed during semantic replay"
        )
    require_namespace_owned_by_epoch(namespace, lease, "stage-12")
    return payload


def _replay_domain_evaluator_snapshot(
    snapshot: _Stage12AuthoritySnapshot,
    *,
    run_dir: Path,
    config: RCConfig,
    bound_python: _BoundPythonExecutable,
) -> dict[str, Any]:
    """Replay only bytes captured in one immutable Stage 10-12 snapshot."""

    try:
        manifest_text = snapshot.manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Stage12DomainEvaluatorError("Stage 12 manifest is not UTF-8") from exc
    payload = parse_domain_evaluator_result_set(manifest_text)
    seal = _replay_domain_evaluator_candidate_snapshot(
        snapshot.stage10, runtime_config=config, project_root=run_dir
    )
    capture = dict(snapshot.stage10.capture_entries)
    if payload["sealed_candidate_manifest"] != _ref(
        "stage-10/selected_candidate_manifest.json",
        snapshot.stage10.seal_bytes,
    ):
        raise Stage12DomainEvaluatorError("Stage 10 seal binding mismatch")
    for field in (
        "experiment_contract", "capture_manifest", "package_manifest",
        "execution_policy", "run_config",
    ):
        expected = seal[field]
        if payload[field] != expected:
            raise Stage12DomainEvaluatorError(f"Stage 12 {field} binding mismatch")
    if payload["metric_authority"] != seal["metric_authority"]:
        raise Stage12DomainEvaluatorError("Stage 12 metric authority mismatch")
    contract = parse_contract_bytes(snapshot.stage10.source.contract_bytes)
    if (
        payload["experiment_mode"] != config.experiment.mode
        or payload["config_semantic_policy_version"]
        != Decimal(seal["config_semantic_policy_version"])
        or payload["config_semantic_sha256"] != seal["config_semantic_sha256"]
        or payload["claim_scope"] != contract["claim_scope"]
        or payload["dataset_origin"] != contract["dataset_origin"]
        or payload["dataset_name"] != contract["dataset_name"]
        or payload["evaluator_schema"] != seal["evaluator_schema"]
    ):
        raise Stage12DomainEvaluatorError("Stage 12 producer binding mismatch")
    journal_bytes = snapshot.journal_bytes
    journal = parse_domain_evaluator_journal(journal_bytes)
    if payload["invocation_journal"] != _ref(
        "stage-12/execution_invocation_journal.jsonl", journal_bytes
    ):
        raise Stage12DomainEvaluatorError("Stage 12 journal binding mismatch")
    tree = dict(snapshot.evidence_entries)
    expected_tree_paths = {
        "invocation-1/score_evidence.jsonl",
        "invocation-1/execution_meta.json",
        "invocation-2/score_evidence.jsonl",
        "invocation-2/execution_meta.json",
        "verification-1.json",
        "verification-2.json",
        "observations.json",
        "run-1.json",
        "run-2.json",
        "results.json",
    }
    if set(tree) != expected_tree_paths:
        raise Stage12DomainEvaluatorError("Stage 12 evidence exact closure mismatch")
    refs = [_ref(f"stage-12/evidence-v2/{path}", content) for path, content in sorted(tree.items())]
    if payload["evidence_files"] != refs:
        raise Stage12DomainEvaluatorError("Stage 12 evidence namespace mismatch")
    scores = [tree[f"invocation-{ordinal}/score_evidence.jsonl"] for ordinal in (1, 2)]
    if scores[0] != scores[1]:
        raise Stage12DomainEvaluatorError("Stage 12 raw score evidence mismatch")
    policy = _strict_json_object(capture["policy/execution-policy-v1.json"], "execution policy")
    _validate_execution_policy(policy)
    runtime = _capture_runtime_attestation(bound_python, policy)
    runtime_sha = hashlib.sha256(_canonical_json_bytes(runtime)).hexdigest()
    contract_bytes = snapshot.stage10.source.contract_bytes
    contract_sha = hashlib.sha256(contract_bytes).hexdigest()
    seal_sha = hashlib.sha256(snapshot.stage10.seal_bytes).hexdigest()
    for ordinal, started, terminal in (
        (1, journal[0], journal[1]),
        (2, journal[2], journal[3]),
    ):
        expected_binding = _generation_binding_sha256(
            ordinal=ordinal,
            invocation_token=started["invocation_token"],
            seal=seal,
            contract_sha256=contract_sha,
            sealed_candidate_manifest_sha256=seal_sha,
            runtime_attestation_sha256=runtime_sha,
        )
        if started != {
            "schema_version": Decimal(2),
            "event": "started",
            "ordinal": Decimal(ordinal),
            "invocation_token": started["invocation_token"],
            "generation_binding_sha256": expected_binding,
            "experiment_contract_sha256": contract_sha,
            "sealed_candidate_manifest_sha256": seal_sha,
            "capture_manifest_sha256": seal["capture_manifest"]["sha256"],
            "execution_policy_sha256": seal["execution_policy"]["sha256"],
            "runtime_attestation_sha256": runtime_sha,
            "config_semantic_sha256": seal["config_semantic_sha256"],
            "evaluator_schema": seal["evaluator_schema"],
        }:
            raise Stage12DomainEvaluatorError("Stage 12 started binding mismatch")
        expected_score_path = f"stage-12/evidence-v2/invocation-{ordinal}/score_evidence.jsonl"
        expected_meta_path = f"stage-12/evidence-v2/invocation-{ordinal}/execution_meta.json"
        if terminal["score_evidence_path"] != expected_score_path or terminal["execution_meta_path"] != expected_meta_path:
            raise Stage12DomainEvaluatorError("Stage 12 terminal path mismatch")
        score_content = tree[f"invocation-{ordinal}/score_evidence.jsonl"]
        meta_content = tree[f"invocation-{ordinal}/execution_meta.json"]
        if terminal["score_evidence_sha256"] != hashlib.sha256(score_content).hexdigest() or terminal["execution_meta_sha256"] != hashlib.sha256(meta_content).hexdigest():
            raise Stage12DomainEvaluatorError("Stage 12 terminal hash mismatch")
        _validate_execution_meta(
            _strict_authority_json(meta_content, "execution meta"),
            ordinal=ordinal,
            token=started["invocation_token"],
            runtime=runtime,
            capture_sha=seal["capture_manifest"]["sha256"],
            policy_sha=seal["execution_policy"]["sha256"],
            backend_policy=policy["execution_backend_policy"],
            evaluator_command_sha256=_expected_evaluator_command_sha(
                bound_python, capture
            ),
        )
    for score in scores:
        _validate_score_evidence(score, policy)
    capture_sha = hashlib.sha256(capture["capture-manifest.json"]).hexdigest()
    expected_verification = [
        _run_verifier(
            bound_python=bound_python,
            capture=capture,
            score_bytes=scores[index - 1],
            dataset_capture_sha256=capture_sha,
            ordinal=index,
        )
        for index in (1, 2)
    ]
    if expected_verification[0] != expected_verification[1]:
        raise Stage12DomainEvaluatorError("Stage 12 verifier replay mismatch")
    for ordinal in (1, 2):
        if tree[f"verification-{ordinal}.json"] != expected_verification[ordinal - 1]:
            raise Stage12DomainEvaluatorError("stored verifier output mismatch")
    if tree["observations.json"] != expected_verification[0]:
        raise Stage12DomainEvaluatorError("stored observations mismatch")
    observations = _strict_authority_json(tree["observations.json"], "observations")
    for ordinal, started, terminal in (
        (1, journal[0], journal[1]),
        (2, journal[2], journal[3]),
    ):
        run_bytes = tree[f"run-{ordinal}.json"]
        run_payload = _strict_authority_json(run_bytes, f"run-{ordinal}")
        _validate_run_payload(
            run_payload,
            ordinal,
            started,
            terminal,
            observations,
            tree,
            seal["capture_manifest"],
        )
    results = _strict_authority_json(tree["results.json"], "results")
    _exact(
        results,
        {"schema_version", "results_policy_version", "runs", "observations", "structured_results", "primary_metric"},
        "Stage 12 results",
    )
    if results["schema_version"] != Decimal(2) or results["results_policy_version"] != Decimal(2):
        raise Stage12DomainEvaluatorError("Stage 12 results policy mismatch")
    expected_run_refs = [
        _ref(f"stage-12/evidence-v2/run-{ordinal}.json", tree[f"run-{ordinal}.json"])
        for ordinal in (1, 2)
    ]
    if results["runs"] != expected_run_refs or results["observations"] != _ref(
        "stage-12/evidence-v2/observations.json", tree["observations.json"]
    ):
        raise Stage12DomainEvaluatorError("Stage 12 results references mismatch")
    if results["structured_results"] != observations["aggregate"] or results["primary_metric"] != observations["primary_metric"]:
        raise Stage12DomainEvaluatorError("Stage 12 results derivation mismatch")
    if payload["observations"] != _ref(
        "stage-12/evidence-v2/observations.json", tree["observations.json"]
    ) or payload["results"] != _ref(
        "stage-12/evidence-v2/results.json", tree["results.json"]
    ):
        raise Stage12DomainEvaluatorError("Stage 12 result references mismatch")
    if payload["primary_metric"] != observations["primary_metric"]:
        raise Stage12DomainEvaluatorError("Stage 12 primary metric mismatch")
    return payload


def parse_domain_evaluator_result_set(text: str) -> dict[str, Any]:
    payload = _strict_authority_json(text.encode("utf-8"), "Stage 12 domain result set")
    expected = {
        "schema_version", "result_set_policy_version", "result_set_type",
        "experiment_mode", "experiment_contract", "sealed_candidate_manifest",
        "capture_manifest", "package_manifest", "execution_policy", "run_config",
        "config_semantic_policy_version", "config_semantic_sha256", "claim_scope",
        "dataset_origin", "dataset_name", "evaluator_schema", "metric_authority",
        "invocation_journal", "execution_statuses", "evidence_files", "observations",
        "results", "primary_metric",
    }
    _exact(payload, expected, "Stage 12 domain result set")
    if payload["schema_version"] != Decimal(2) or payload["result_set_policy_version"] != Decimal(2):
        raise Stage12DomainEvaluatorError("Stage 12 v2 schema mismatch")
    if payload["result_set_type"] != "stage12_domain_evaluator":
        raise Stage12DomainEvaluatorError("Stage 12 result-set type mismatch")
    for field in (
        "experiment_contract", "sealed_candidate_manifest", "capture_manifest",
        "package_manifest", "execution_policy", "run_config", "invocation_journal",
        "observations", "results",
    ):
        _validate_ref(payload[field], field)
    statuses = payload["execution_statuses"]
    if not isinstance(statuses, list) or len(statuses) != 2:
        raise Stage12DomainEvaluatorError("Stage 12 requires two completed statuses")
    for ordinal, status in enumerate(statuses, start=1):
        _exact(status, {"ordinal", "status", "run", "failure_code"}, "execution status")
        if status != {
            "ordinal": Decimal(ordinal),
            "status": "completed",
            "run": f"stage-12/evidence-v2/run-{ordinal}.json",
            "failure_code": None,
        }:
            raise Stage12DomainEvaluatorError("invalid Stage 12 execution status")
    refs = payload["evidence_files"]
    if not isinstance(refs, list) or not refs:
        raise Stage12DomainEvaluatorError("Stage 12 evidence_files must be nonempty")
    for ref in refs:
        _validate_ref(ref, "evidence file")
    if [ref["path"] for ref in refs] != sorted(ref["path"] for ref in refs):
        raise Stage12DomainEvaluatorError("Stage 12 evidence refs are not sorted")
    _validate_primary_metric(payload["primary_metric"])
    return payload


def parse_domain_evaluator_journal(content: bytes) -> list[dict[str, Any]]:
    if not content.endswith(b"\n") or b"\r" in content or content.count(b"\n") != 4:
        raise Stage12DomainEvaluatorError("Stage 12 v2 journal bytes are invalid")
    records = [
        _strict_authority_json(line + b"\n", "journal record")
        for line in content.splitlines()
    ]
    for ordinal, started, terminal in ((1, records[0], records[1]), (2, records[2], records[3])):
        _exact(
            started,
            {
                "schema_version", "event", "ordinal", "invocation_token",
                "generation_binding_sha256", "experiment_contract_sha256",
                "sealed_candidate_manifest_sha256", "capture_manifest_sha256",
                "execution_policy_sha256", "runtime_attestation_sha256",
                "config_semantic_sha256", "evaluator_schema",
            },
            "started journal record",
        )
        _exact(
            terminal,
            {
                "schema_version", "event", "ordinal", "invocation_token", "status",
                "score_evidence_path", "score_evidence_sha256", "execution_meta_path",
                "execution_meta_sha256", "failure_code",
            },
            "terminal journal record",
        )
        if started["schema_version"] != Decimal(2) or terminal["schema_version"] != Decimal(2):
            raise Stage12DomainEvaluatorError("journal schema mismatch")
        if started["event"] != "started" or terminal["event"] != "terminal":
            raise Stage12DomainEvaluatorError("journal event order mismatch")
        if started["ordinal"] != Decimal(ordinal) or terminal["ordinal"] != Decimal(ordinal):
            raise Stage12DomainEvaluatorError("journal ordinal mismatch")
        if started["invocation_token"] != terminal["invocation_token"]:
            raise Stage12DomainEvaluatorError("journal token mismatch")
        _require_invocation_token(started["invocation_token"])
        if terminal["status"] != "completed" or terminal["failure_code"] is not None:
            raise Stage12DomainEvaluatorError("published journal is not completed")
        if terminal["score_evidence_path"] != (
            f"stage-12/evidence-v2/invocation-{ordinal}/score_evidence.jsonl"
        ) or terminal["execution_meta_path"] != (
            f"stage-12/evidence-v2/invocation-{ordinal}/execution_meta.json"
        ):
            raise Stage12DomainEvaluatorError("journal authority path mismatch")
        for field in (
            "generation_binding_sha256", "experiment_contract_sha256",
            "sealed_candidate_manifest_sha256", "capture_manifest_sha256",
            "execution_policy_sha256", "runtime_attestation_sha256",
            "config_semantic_sha256",
        ):
            _require_sha(started[field], field)
        _require_sha(terminal["score_evidence_sha256"], "score evidence hash")
        _require_sha(terminal["execution_meta_sha256"], "execution meta hash")
    if records[0]["invocation_token"] == records[2]["invocation_token"]:
        raise Stage12DomainEvaluatorError("invocation tokens must be unique")
    return records


def _run_evaluator(
    *,
    bound_python: _BoundPythonExecutable,
    capture: Mapping[str, bytes],
    output_dir: Path,
) -> tuple[bytes, str]:
    workspace = output_dir.parent.parent / f"workspace-{output_dir.name}"
    _write_capture_workspace(workspace, capture, include_evaluator=True)
    output = output_dir / "score_evidence.jsonl"
    pycache = workspace / ".pycache-external"
    command = [
        bound_python.configured_path,
        "-I",
        "-X",
        f"pycache_prefix={pycache}",
        str(workspace / "evaluator_main.py"),
        "--vendor-root",
        str(workspace / "trojnet"),
        "--data-root",
        str(workspace / "data"),
        "--policy",
        str(workspace / "execution-policy-v1.json"),
        "--output",
        str(output),
    ]
    command_sha = _expected_evaluator_command_sha(bound_python, capture)
    _run_process(command, bound_python=bound_python, cwd=workspace, timeout=1800)
    bound_python.assert_bound()
    actual = set(output_dir.iterdir())
    if actual != {output}:
        raise Stage12DomainEvaluatorError("evaluator output namespace mismatch")
    content = output.read_bytes()
    return content, command_sha


def _run_verifier(
    *,
    bound_python: _BoundPythonExecutable,
    capture: Mapping[str, bytes],
    score_bytes: bytes,
    dataset_capture_sha256: str,
    ordinal: int,
) -> bytes:
    root = Path(tempfile.mkdtemp(prefix=f"researchclaw-verifier-{ordinal}-"))
    try:
        _write_capture_workspace(root, capture, include_verifier=True)
        scores = root / "score_evidence.jsonl"
        output = root / "verification.json"
        scores.write_bytes(score_bytes)
        command = [
            bound_python.configured_path,
            "-I",
            "-S",
            "-X",
            f"pycache_prefix={root / '.pycache-external'}",
            str(root / "verifier_main.py"),
            "--data-root",
            str(root / "data"),
            "--scores",
            str(scores),
            "--output",
            str(output),
            "--dataset-capture-sha256",
            dataset_capture_sha256,
        ]
        _run_process(
            command, bound_python=bound_python, cwd=root, timeout=300
        )
        content = output.read_bytes()
        _strict_authority_json(content, "verifier output")
        return content
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _capture_runtime_attestation(
    bound_python: _BoundPythonExecutable, policy: dict[str, Any]
) -> dict[str, Any]:
    projection = policy["runtime_projection"]
    script = (
        "import importlib.metadata,json,platform,torch;"
        "torch.use_deterministic_algorithms(True);torch.set_num_threads(1);"
        "p={n:importlib.metadata.version(n) for n in "
        + repr(sorted(projection["packages"]))
        + "};print(json.dumps({'python_major_minor':platform.python_version_tuple()[0]+'.'+platform.python_version_tuple()[1],"
        "'packages':p,'device':'cpu','torch_deterministic_algorithms':torch.are_deterministic_algorithms_enabled(),"
        "'torch_num_threads':torch.get_num_threads()},sort_keys=True,separators=(',',':')))"
    )
    result = _run_process(
        [bound_python.configured_path, "-I", "-c", script],
        bound_python=bound_python,
        cwd=Path(tempfile.gettempdir()),
        timeout=60,
        capture_output=True,
    )
    if result.stderr:
        raise Stage12DomainEvaluatorError(
            "runtime attestation emitted unexpected stderr"
        )
    actual = _strict_runtime_attestation(result.stdout)
    _validate_runtime_projection(projection)
    if actual != projection or result.stdout != _canonical_json_bytes(actual):
        raise Stage12DomainEvaluatorError("runtime projection mismatch")
    return actual


def _write_capture_workspace(
    root: Path,
    capture: Mapping[str, bytes],
    *,
    include_evaluator: bool = False,
    include_verifier: bool = False,
) -> None:
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise Stage12DomainEvaluatorError("invocation workspace is not empty")
    else:
        root.mkdir(parents=True, exist_ok=False)
    for relative, content in sorted(capture.items()):
        destination: Path | None = None
        if relative.startswith("data/"):
            destination = root / relative
        elif include_evaluator and relative.startswith("vendor/"):
            destination = root / "trojnet" / relative.removeprefix("vendor/")
        elif include_evaluator and relative == "evaluator/evaluator_main.py":
            destination = root / "evaluator_main.py"
        elif include_evaluator and relative == "policy/execution-policy-v1.json":
            destination = root / "execution-policy-v1.json"
        elif include_verifier and relative == "verifier/verifier_main.py":
            destination = root / "verifier_main.py"
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)


def _run_process(
    command: list[str],
    *,
    bound_python: _BoundPythonExecutable,
    cwd: Path,
    timeout: int,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "HOME": tempfile.gettempdir(),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    bound_python.assert_bound()
    if bound_python.darwin_cdhash is not None:
        result = _run_darwin_suspended(
            command,
            executable=bound_python.execution_path,
            expected_cdhash=bound_python.darwin_cdhash,
            cwd=cwd,
            environment=environment,
            timeout=timeout,
        )
    else:
        result = subprocess.run(
            command,
            executable=bound_python.execution_path,
            pass_fds=bound_python.pass_fds,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    bound_python.assert_bound()
    if result.returncode != 0:
        raise Stage12DomainEvaluatorError(
            f"trusted subprocess failed ({result.returncode}): "
            + result.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    if not capture_output and result.stdout:
        raise Stage12DomainEvaluatorError("trusted subprocess emitted unexpected stdout")
    return result


def _darwin_process_cdhash(pid: int) -> bytes:
    """Read the kernel's CodeDirectory hash for an already loaded image."""

    if sys.platform != "darwin":
        raise Stage12DomainEvaluatorError("Darwin executable proof is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    csops = libc.csops
    csops.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
    csops.restype = ctypes.c_int
    output = (ctypes.c_ubyte * 20)()
    if csops(pid, 5, output, len(output)) != 0:  # CS_OPS_CDHASH
        error = ctypes.get_errno()
        raise Stage12DomainEvaluatorError(
            f"cannot inspect loaded executable image: {os.strerror(error)}"
        )
    digest = bytes(output)
    if digest == b"\0" * len(digest):
        raise Stage12DomainEvaluatorError("loaded executable has no code hash")
    return digest


def _darwin_code_directory_hash(content: bytes) -> bytes:
    """Derive the SHA-256 CodeDirectory hash from captured thin Mach-O bytes."""

    if len(content) < 32 or struct.unpack_from("<I", content)[0] != 0xFEEDFACF:
        raise Stage12DomainEvaluatorError(
            "configured evaluator Python is not a supported thin Mach-O image"
        )
    command_count = struct.unpack_from("<I", content, 16)[0]
    offset = 32
    signature: bytes | None = None
    for _index in range(command_count):
        if offset + 8 > len(content):
            raise Stage12DomainEvaluatorError("Mach-O load commands are truncated")
        command, command_size = struct.unpack_from("<II", content, offset)
        if command_size < 8 or offset + command_size > len(content):
            raise Stage12DomainEvaluatorError("Mach-O load command is invalid")
        if command == 0x1D:  # LC_CODE_SIGNATURE
            if command_size < 16 or signature is not None:
                raise Stage12DomainEvaluatorError("Mach-O signature command is invalid")
            data_offset, data_size = struct.unpack_from("<II", content, offset + 8)
            if data_size == 0 or data_offset + data_size > len(content):
                raise Stage12DomainEvaluatorError("Mach-O signature is truncated")
            signature = content[data_offset : data_offset + data_size]
        offset += command_size
    if signature is None or len(signature) < 12:
        raise Stage12DomainEvaluatorError("Mach-O code signature is missing")
    magic, total_length, count = struct.unpack_from(">III", signature)
    if magic != 0xFADE0CC0 or total_length > len(signature) or total_length < 12 + count * 8:
        raise Stage12DomainEvaluatorError("Mach-O signature superblob is invalid")
    candidates: list[bytes] = []
    for index in range(count):
        _slot, blob_offset = struct.unpack_from(">II", signature, 12 + index * 8)
        if blob_offset + 8 > total_length:
            raise Stage12DomainEvaluatorError("Mach-O signature blob is truncated")
        blob_magic, blob_length = struct.unpack_from(">II", signature, blob_offset)
        if blob_magic != 0xFADE0C02:
            continue
        if (
            blob_length < 38
            or blob_offset + 38 > total_length
            or blob_offset + blob_length > total_length
        ):
            raise Stage12DomainEvaluatorError("Mach-O CodeDirectory is invalid")
        code_directory = signature[blob_offset : blob_offset + blob_length]
        if code_directory[37] == 2:  # CS_HASHTYPE_SHA256
            candidates.append(hashlib.sha256(code_directory).digest()[:20])
    if len(set(candidates)) != 1:
        raise Stage12DomainEvaluatorError(
            "Mach-O must contain one unambiguous SHA-256 CodeDirectory"
        )
    return candidates[0]


def _run_darwin_suspended(
    command: list[str],
    *,
    executable: str,
    expected_cdhash: bytes,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    """Spawn suspended, prove the loaded image in-kernel, then allow execution."""

    libc = ctypes.CDLL(None, use_errno=True)
    opaque = ctypes.c_void_p
    opaque_pointer = ctypes.POINTER(opaque)
    functions = {
        "posix_spawnattr_init": [opaque_pointer],
        "posix_spawnattr_setflags": [opaque_pointer, ctypes.c_short],
        "posix_spawnattr_destroy": [opaque_pointer],
        "posix_spawn_file_actions_init": [opaque_pointer],
        "posix_spawn_file_actions_adddup2": [opaque_pointer, ctypes.c_int, ctypes.c_int],
        "posix_spawn_file_actions_addchdir_np": [opaque_pointer, ctypes.c_char_p],
        "posix_spawn_file_actions_destroy": [opaque_pointer],
    }
    for name, argument_types in functions.items():
        function = getattr(libc, name, None)
        if function is None:
            raise Stage12DomainEvaluatorError(
                "Darwin suspended executable binding is unsupported"
            )
        function.argtypes = argument_types
        function.restype = ctypes.c_int
    libc.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        opaque_pointer,
        opaque_pointer,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    libc.posix_spawn.restype = ctypes.c_int

    attributes = opaque()
    actions = opaque()
    child_pid: int | None = None

    def checked(result: int, operation: str) -> None:
        if result:
            raise Stage12DomainEvaluatorError(
                f"{operation} failed: {os.strerror(result)}"
            )

    with (
        open(os.devnull, "rb", buffering=0) as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        checked(libc.posix_spawnattr_init(ctypes.byref(attributes)), "spawn attr init")
        try:
            checked(
                libc.posix_spawnattr_setflags(
                    ctypes.byref(attributes), ctypes.c_short(0x0080)
                ),
                "spawn suspended flag",
            )
            checked(
                libc.posix_spawn_file_actions_init(ctypes.byref(actions)),
                "spawn actions init",
            )
            try:
                for source, destination in (
                    (stdin_file.fileno(), 0),
                    (stdout_file.fileno(), 1),
                    (stderr_file.fileno(), 2),
                ):
                    checked(
                        libc.posix_spawn_file_actions_adddup2(
                            ctypes.byref(actions), source, destination
                        ),
                        "spawn fd binding",
                    )
                checked(
                    libc.posix_spawn_file_actions_addchdir_np(
                        ctypes.byref(actions), os.fsencode(cwd)
                    ),
                    "spawn cwd binding",
                )
                argument_bytes = [os.fsencode(item) for item in command]
                arguments = (ctypes.c_char_p * (len(argument_bytes) + 1))(
                    *argument_bytes, None
                )
                environment_bytes = [
                    os.fsencode(key) + b"=" + os.fsencode(value)
                    for key, value in sorted(environment.items())
                ]
                environment_array = (
                    ctypes.c_char_p * (len(environment_bytes) + 1)
                )(*environment_bytes, None)
                pid = ctypes.c_int()
                checked(
                    libc.posix_spawn(
                        ctypes.byref(pid),
                        os.fsencode(executable),
                        ctypes.byref(actions),
                        ctypes.byref(attributes),
                        arguments,
                        environment_array,
                    ),
                    "suspended executable spawn",
                )
                child_pid = pid.value
            finally:
                libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        finally:
            libc.posix_spawnattr_destroy(ctypes.byref(attributes))

        try:
            if _darwin_process_cdhash(child_pid) != expected_cdhash:
                raise Stage12DomainEvaluatorError(
                    "spawned executable image differs from bound Python"
                )
            os.kill(child_pid, signal.SIGCONT)
            deadline = time.monotonic() + timeout
            while True:
                waited, status = os.waitpid(child_pid, os.WNOHANG)
                if waited == child_pid:
                    child_pid = None
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.01)
        except Exception:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
                child_pid = None
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        returncode = (
            os.waitstatus_to_exitcode(status)
            if hasattr(os, "waitstatus_to_exitcode")
            else os.WEXITSTATUS(status)
        )
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout_file.read(),
            stderr_file.read(),
        )


def _validate_execution_policy(policy: dict[str, Any]) -> None:
    if (
        type(policy.get("schema_version")) is not int
        or policy["schema_version"] != 1
        or type(policy.get("execution_policy_version")) is not int
        or policy["execution_policy_version"] != 1
    ):
        raise Stage12DomainEvaluatorError("execution policy version mismatch")
    if type(policy.get("invocation_count")) is not int or policy["invocation_count"] != 2:
        raise Stage12DomainEvaluatorError("execution policy must require two invocations")
    if tuple(policy.get("metric_keys", ())) != EXPECTED_METRICS:
        raise Stage12DomainEvaluatorError("execution metric key policy mismatch")
    if policy.get("execution_backend_policy") != "host_subprocess_trusted_consistency_v1":
        raise Stage12DomainEvaluatorError("unsupported evaluator backend policy")


def _validate_execution_meta(
    payload: dict[str, Any],
    *,
    ordinal: int,
    token: str,
    runtime: dict[str, Any],
    capture_sha: str,
    policy_sha: str,
    backend_policy: str,
    evaluator_command_sha256: str,
) -> None:
    _exact(
        payload,
        {
            "schema_version", "execution_meta_policy_version", "ordinal",
            "invocation_token", "evaluator_command_sha256", "backend_policy",
            "runtime_attestation", "capture_manifest_sha256",
            "execution_policy_sha256",
        },
        "execution meta",
    )
    if payload["schema_version"] != Decimal(2) or payload["execution_meta_policy_version"] != Decimal(1):
        raise Stage12DomainEvaluatorError("execution meta policy mismatch")
    _require_invocation_token(payload["invocation_token"])
    if payload["ordinal"] != Decimal(ordinal) or payload["invocation_token"] != token:
        raise Stage12DomainEvaluatorError("execution meta invocation mismatch")
    if payload["evaluator_command_sha256"] != evaluator_command_sha256:
        raise Stage12DomainEvaluatorError("execution meta command binding mismatch")
    _validate_stored_runtime_projection(payload["runtime_attestation"])
    if payload["backend_policy"] != backend_policy or payload["runtime_attestation"] != runtime:
        raise Stage12DomainEvaluatorError("execution meta runtime mismatch")
    if payload["capture_manifest_sha256"] != capture_sha or payload["execution_policy_sha256"] != policy_sha:
        raise Stage12DomainEvaluatorError("execution meta authority binding mismatch")


def _expected_evaluator_command_sha(
    bound_python: _BoundPythonExecutable,
    capture: Mapping[str, bytes],
) -> str:
    descriptor = {
        "policy_version": 2,
        "execution_binding_policy": bound_python.binding_policy,
        "python_executable": bound_python.configured_path,
        "python_resolved_path": bound_python.resolved_path,
        "python_executable_size": bound_python.size,
        "python_executable_sha256": bound_python.sha256,
        "python_flags": ["-I", "-X", "pycache_prefix=<controller-temp>"],
        "evaluator_sha256": hashlib.sha256(
            capture["evaluator/evaluator_main.py"]
        ).hexdigest(),
        "execution_policy_sha256": hashlib.sha256(
            capture["policy/execution-policy-v1.json"]
        ).hexdigest(),
        "argv": [
            "--vendor-root", "<captured-vendor>",
            "--data-root", "<captured-data>",
            "--policy", "<captured-policy>",
            "--output", "<controller-output>/score_evidence.jsonl",
        ],
    }
    return hashlib.sha256(_canonical_json_bytes(descriptor)).hexdigest()


def _capture_executable_bytes(path: Path) -> tuple[bytes, int, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise Stage12DomainEvaluatorError("configured evaluator Python is unsafe")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        signature = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if signature(after) != signature(before):
            raise Stage12DomainEvaluatorError(
                "configured evaluator Python changed during capture"
            )
        return b"".join(chunks), before.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


@contextmanager
def _bound_python_executable(config: RCConfig):
    """Bind execution to a sealed fd or a suspended kernel-verified image."""

    configured = Path(_python_executable(config))
    resolved = configured.resolve(strict=True)
    content, size, digest = _capture_executable_bytes(resolved)
    bound: _BoundPythonExecutable | None = None
    if sys.platform.startswith("linux") and hasattr(os, "memfd_create"):
        flags = getattr(os, "MFD_CLOEXEC", 0) | getattr(
            os, "MFD_ALLOW_SEALING", 0
        )
        descriptor = os.memfd_create("researchclaw-python", flags)
        try:
            _write_all_fd(descriptor, content)
            os.fchmod(descriptor, 0o500)
            seals = (
                getattr(fcntl, "F_SEAL_SEAL", 0)
                | getattr(fcntl, "F_SEAL_SHRINK", 0)
                | getattr(fcntl, "F_SEAL_GROW", 0)
                | getattr(fcntl, "F_SEAL_WRITE", 0)
            )
            if not seals or not hasattr(fcntl, "F_ADD_SEALS"):
                raise Stage12DomainEvaluatorError(
                    "sealed executable fd is unsupported"
                )
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
            os.lseek(descriptor, 0, os.SEEK_SET)
            bound = _BoundPythonExecutable(
                configured.as_posix(), resolved.as_posix(),
                f"/proc/self/fd/{descriptor}", descriptor, size, digest,
                (descriptor,), "linux_sealed_memfd_v1",
            )
            yield bound
        finally:
            os.close(descriptor)
        return

    if sys.platform != "darwin":
        raise Stage12DomainEvaluatorError(
            "immutable executable binding is unsupported on this platform"
        )
    current = Path(sys.executable).resolve(strict=True)
    current_content, current_size, current_digest = _capture_executable_bytes(current)
    del current_content
    if (current_size, current_digest) != (size, digest):
        raise Stage12DomainEvaluatorError(
            "configured evaluator Python is not the running controller image"
        )
    descriptor = os.open(
        resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    try:
        if _sha256_fd(descriptor, size) != digest:
            raise Stage12DomainEvaluatorError(
                "configured evaluator Python changed before binding"
            )
        cdhash = _darwin_code_directory_hash(content)
        bound = _BoundPythonExecutable(
            configured.as_posix(),
            resolved.as_posix(),
            configured.as_posix(),
            descriptor,
            size,
            digest,
            (),
            "darwin_suspended_cdhash_v1",
            cdhash,
        )
        bound.assert_bound()
        yield bound
    finally:
        os.close(descriptor)


def _write_all_fd(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("executable copy made no progress")
        offset += written


def _sha256_fd(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise Stage12DomainEvaluatorError(
                "bound Python executable ended unexpectedly"
            )
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _validate_score_evidence(content: bytes, policy: Mapping[str, Any]) -> None:
    if (
        not content
        or len(content) > SCORE_LIMIT
        or not content.endswith(b"\n")
        or b"\r" in content
        or content.startswith(b"\xef\xbb\xbf")
        or content.count(b"\n") != 162
    ):
        raise Stage12DomainEvaluatorError("score evidence bytes are noncanonical")
    expected = [
        (condition, seed, family, f"{family}_ht{variant}")
        for seed in policy["seeds"]
        for family in policy["circuit_families"]
        for variant in range(1, policy["variants_per_family"] + 1)
        for condition in policy["conditions"]
    ]
    for ordinal, raw in enumerate(content.splitlines()):
        row = _strict_json_object(raw, "score row")
        _exact(
            row,
            {"schema_version", "condition", "seed", "circuit_family", "circuit_variant", "node_ids", "scores"},
            "score row",
        )
        if type(row["schema_version"]) is not int or row["schema_version"] != 1 or type(row["seed"]) is not int:
            raise Stage12DomainEvaluatorError("score row structural integer mismatch")
        if (row["condition"], row["seed"], row["circuit_family"], row["circuit_variant"]) != expected[ordinal]:
            raise Stage12DomainEvaluatorError("score row order mismatch")
        nodes, scores = row["node_ids"], row["scores"]
        if not isinstance(nodes, list) or not nodes or len(nodes) != len(set(nodes)):
            raise Stage12DomainEvaluatorError("score node namespace mismatch")
        if not isinstance(scores, list) or len(scores) != len(nodes):
            raise Stage12DomainEvaluatorError("score vector size mismatch")
        if not all(isinstance(item, str) and item != "-0" and SCORE_RE.fullmatch(item) for item in scores):
            raise Stage12DomainEvaluatorError("score decimal grammar mismatch")
        if _canonical_json_bytes(row).rstrip(b"\n") != raw:
            raise Stage12DomainEvaluatorError("score row JSON is not canonical")


def _build_result_set_manifest(
    *,
    config: RCConfig,
    contract: Any,
    seal: dict[str, Any],
    stage10_seal_bytes: bytes,
    journal: list[dict[str, Any]],
    evidence_root: Path,
    observations_bytes: bytes,
    results_bytes: bytes,
    observations: dict[str, Any],
) -> dict[str, Any]:
    journal_bytes = _journal_bytes(journal)
    evidence_refs = [
        _ref(
            f"stage-12/evidence-v2/{path.relative_to(evidence_root).as_posix()}",
            path.read_bytes(),
        )
        for path in sorted(evidence_root.rglob("*"))
        if path.is_file()
    ]
    return {
        "schema_version": 2,
        "result_set_policy_version": 2,
        "result_set_type": "stage12_domain_evaluator",
        "experiment_mode": config.experiment.mode,
        "experiment_contract": seal["experiment_contract"],
        "sealed_candidate_manifest": _ref(
            "stage-10/selected_candidate_manifest.json",
            stage10_seal_bytes,
        ),
        "capture_manifest": seal["capture_manifest"],
        "package_manifest": seal["package_manifest"],
        "execution_policy": seal["execution_policy"],
        "run_config": seal["run_config"],
        "config_semantic_policy_version": seal["config_semantic_policy_version"],
        "config_semantic_sha256": seal["config_semantic_sha256"],
        "claim_scope": contract["claim_scope"],
        "dataset_origin": contract["dataset_origin"],
        "dataset_name": contract["dataset_name"],
        "evaluator_schema": seal["evaluator_schema"],
        "metric_authority": seal["metric_authority"],
        "invocation_journal": _ref(
            "stage-12/execution_invocation_journal.jsonl", journal_bytes
        ),
        "execution_statuses": [
            {
                "ordinal": ordinal,
                "status": "completed",
                "run": f"stage-12/evidence-v2/run-{ordinal}.json",
                "failure_code": None,
            }
            for ordinal in (1, 2)
        ],
        "evidence_files": evidence_refs,
        "observations": _ref(
            "stage-12/evidence-v2/observations.json", observations_bytes
        ),
        "results": _ref("stage-12/evidence-v2/results.json", results_bytes),
        "primary_metric": observations["primary_metric"],
    }


def _validate_run_payload(
    payload: dict[str, Any],
    ordinal: int,
    started: dict[str, Any],
    terminal: dict[str, Any],
    observations: dict[str, Any],
    tree: Mapping[str, bytes],
    capture_manifest: dict[str, Any],
) -> None:
    _exact(
        payload,
        {
            "schema_version", "run_policy_version", "ordinal", "invocation_token",
            "generation_binding_sha256", "capture_manifest", "score_evidence",
            "execution_meta", "observations", "primary_metric",
        },
        "Stage 12 run",
    )
    _require_invocation_token(payload["invocation_token"])
    expected = {
        "schema_version": Decimal(2),
        "run_policy_version": Decimal(2),
        "ordinal": Decimal(ordinal),
        "invocation_token": started["invocation_token"],
        "generation_binding_sha256": started["generation_binding_sha256"],
        "capture_manifest": capture_manifest,
        "score_evidence": _ref(
            terminal["score_evidence_path"],
            tree[f"invocation-{ordinal}/score_evidence.jsonl"],
        ),
        "execution_meta": _ref(
            terminal["execution_meta_path"],
            tree[f"invocation-{ordinal}/execution_meta.json"],
        ),
        "observations": _ref(
            "stage-12/evidence-v2/observations.json", tree["observations.json"]
        ),
        "primary_metric": observations["primary_metric"],
    }
    if payload != expected:
        raise Stage12DomainEvaluatorError("Stage 12 run replay mismatch")


def _reset_stage12(namespace: BoundOutputNamespace) -> None:
    errors: list[str] = []
    for name in ("experiment_result_set.json", "execution_invocation_journal.jsonl"):
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    for name in ("evidence-v2", ".evidence-v2.staging"):
        try:
            namespace.quarantine_tree_entry(name)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise Stage12DomainEvaluatorError("Stage 12 cleanup failed: " + "; ".join(errors))


def _write_journal(namespace: BoundOutputNamespace, records: list[dict[str, Any]]) -> None:
    namespace.write_bytes_atomic("execution_invocation_journal.jsonl", _journal_bytes(records))


def _journal_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(record) for record in records)


def _generation_binding_sha256(
    *,
    ordinal: int,
    invocation_token: str,
    seal: Mapping[str, Any],
    contract_sha256: str,
    sealed_candidate_manifest_sha256: str,
    runtime_attestation_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "ordinal": ordinal,
                "invocation_token": invocation_token,
                "experiment_contract_sha256": contract_sha256,
                "sealed_candidate_manifest_sha256": sealed_candidate_manifest_sha256,
                "capture_manifest_sha256": seal["capture_manifest"]["sha256"],
                "execution_policy_sha256": seal["execution_policy"]["sha256"],
                "runtime_attestation_sha256": runtime_attestation_sha256,
                "config_semantic_sha256": seal["config_semantic_sha256"],
                "evaluator_schema": seal["evaluator_schema"],
            }
        )
    ).hexdigest()


def _python_executable(config: RCConfig) -> str:
    value = Path(config.experiment.sandbox.python_path)
    if not value.is_absolute():
        value = Path.cwd() / value
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise Stage12DomainEvaluatorError(
            "configured evaluator Python is unavailable"
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise Stage12DomainEvaluatorError("configured evaluator Python is unavailable")
    return str(value)


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_strict_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage12DomainEvaluatorError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError(f"{label} must be an object")
    return value


def _strict_runtime_attestation(content: bytes) -> dict[str, Any]:
    if (
        not content.endswith(b"\n")
        or content.count(b"\n") != 1
        or b"\r" in content
        or b"\x00" in content
        or content.startswith(b"\xef\xbb\xbf")
    ):
        raise Stage12DomainEvaluatorError(
            "runtime attestation has noncanonical bytes"
        )
    payload = _strict_json_object(content, "runtime attestation")
    _validate_runtime_projection(payload)
    if _canonical_json_bytes(payload) != content:
        raise Stage12DomainEvaluatorError(
            "runtime attestation is not canonical JSON"
        )
    return payload


def _validate_runtime_projection(value: object) -> None:
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError("runtime projection must be an object")
    _exact(
        value,
        {
            "python_major_minor", "packages", "device",
            "torch_deterministic_algorithms", "torch_num_threads",
        },
        "runtime projection",
    )
    if (
        not isinstance(value["python_major_minor"], str)
        or not isinstance(value["device"], str)
        or type(value["torch_deterministic_algorithms"]) is not bool
        or type(value["torch_num_threads"]) is not int
        or value["torch_num_threads"] <= 0
        or not isinstance(value["packages"], dict)
        or not all(
            isinstance(key, str) and key and isinstance(item, str) and item
            for key, item in value["packages"].items()
        )
    ):
        raise Stage12DomainEvaluatorError("runtime projection type mismatch")


def _validate_stored_runtime_projection(value: object) -> None:
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError("stored runtime projection must be an object")
    _exact(
        value,
        {
            "python_major_minor", "packages", "device",
            "torch_deterministic_algorithms", "torch_num_threads",
        },
        "stored runtime projection",
    )
    threads = value["torch_num_threads"]
    if (
        not isinstance(value["python_major_minor"], str)
        or not isinstance(value["device"], str)
        or type(value["torch_deterministic_algorithms"]) is not bool
        or not isinstance(threads, Decimal)
        or not threads.is_finite()
        or threads <= 0
        or threads != threads.to_integral_value()
        or not isinstance(value["packages"], dict)
        or not all(
            isinstance(key, str) and key and isinstance(item, str) and item
            for key, item in value["packages"].items()
        )
    ):
        raise Stage12DomainEvaluatorError("stored runtime projection type mismatch")


def _strict_authority_json(content: bytes, label: str) -> dict[str, Any]:
    if not content.endswith(b"\n") or b"\r" in content or content.startswith(b"\xef\xbb\xbf"):
        raise Stage12DomainEvaluatorError(f"{label} has noncanonical bytes")
    try:
        value = json.loads(
            content,
            object_pairs_hook=_strict_pairs,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage12DomainEvaluatorError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError(f"{label} must be an object")
    if _authority_json_bytes(value) != content:
        raise Stage12DomainEvaluatorError(f"{label} is not canonical JSON")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _authority_json_text(value: object) -> str:
    text = canonical_authority_json_text(value)
    return text if text.endswith("\n") else text + "\n"


def _authority_json_bytes(value: object) -> bytes:
    return _authority_json_text(value).encode("utf-8")


def _ref(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _validate_ref(value: object, label: str) -> None:
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError(f"{label} must be an object")
    _exact(value, {"path", "sha256", "size"}, label)
    if (
        not isinstance(value["path"], str)
        or unicodedata.normalize("NFC", value["path"]) != value["path"]
        or value["path"].startswith("/")
        or "\\" in value["path"]
        or "%" in value["path"]
        or any(part in {"", ".", ".."} for part in value["path"].split("/"))
    ):
        raise Stage12DomainEvaluatorError(f"{label} path is invalid")
    _require_sha(value["sha256"], f"{label} sha256")
    if not isinstance(value["size"], Decimal) or value["size"] <= 0 or value["size"] != value["size"].to_integral_value():
        raise Stage12DomainEvaluatorError(f"{label} size is invalid")


def _validate_primary_metric(value: object) -> None:
    if not isinstance(value, dict):
        raise Stage12DomainEvaluatorError("primary_metric must be an object")
    _exact(value, {"condition", "key", "observation_set", "aggregation", "value"}, "primary_metric")
    if value["condition"] != "trojnet_community_graphsage" or value["key"] != "auprc":
        raise Stage12DomainEvaluatorError("primary metric identity mismatch")
    if not isinstance(value["value"], Decimal) or not value["value"].is_finite():
        raise Stage12DomainEvaluatorError("primary metric value is invalid")


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise Stage12DomainEvaluatorError(f"{label} is invalid")


def _require_invocation_token(value: object) -> None:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise Stage12DomainEvaluatorError("invocation token is invalid")


def _exact(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise Stage12DomainEvaluatorError(f"{label} schema mismatch")
