"""Strict Stage 15 critique-v2 publication and replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from researchclaw.pipeline.bound_output_namespace import BoundOutputNamespace
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    load_canonical_experiment_evidence,
)
from researchclaw.pipeline.release_graph_lock import (
    ReleaseGraphLock,
    require_active_writer_epoch,
)


CRITIQUE_SCHEMA_VERSION = 2
CRITIQUE_POLICY_VERSION = "stage15_critique_v2"
CRITIQUE_MANIFEST_SCHEMA_VERSION = 1
CRITIQUE_PATH = "stage-15/critique.json"
CRITIQUE_MANIFEST_PATH = "stage-15/stage15_critique_manifest.json"
EXTERNAL_STRUCTURED_PATH = "stage-15/external-review/structured.json"
EXTERNAL_PROSE_PATH = "stage-15/external-review/review.md"
PENDING_REQUEST_PATH = "stage-15/critique-pending/external_review_request.json"

SEVERITIES = frozenset({"P0", "P1", "P2"})
CATEGORIES = frozenset(
    {
        "methodology",
        "evidence",
        "statistics",
        "reproducibility",
        "validity",
        "scope",
        "reporting",
    }
)
UNAVAILABILITY_REASONS = frozenset(
    {"critic_not_configured", "critic_not_isolated", "critic_call_failed"}
)

_FINAL_ROOT_NAMES = ("critique.json", "stage15_critique_manifest.json")
_DECISION_ROOT_NAMES = ("decision.md", "decision_structured.json")
_OWNED_ROOT_NAMES = (
    "stage15_critique_manifest.json",
    "critique.json",
    "critique-pending",
)


class Stage15CritiqueError(ValueError):
    """Raised when critique authority is incomplete or inconsistent."""


@dataclass(frozen=True)
class Stage15CritiquePublication:
    state: str
    critique: Mapping[str, Any] | None
    manifest: Mapping[str, Any] | None
    artifacts: tuple[str, ...]


def canonical_identity_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Stage15CritiqueError(f"identity payload is not canonical JSON: {exc}") from exc


def canonical_json_text(value: object) -> str:
    return canonical_identity_bytes(value).decode("utf-8") + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare_stage15_critique_namespace(namespace: BoundOutputNamespace) -> None:
    """Invalidate old critique authority before any Stage 15 input is read."""

    with ReleaseGraphLock.acquire(
        namespace.run_dir, "prepare_stage15_critique_namespace"
    ) as release_lock:
        _prepare_stage15_critique_namespace_under_lock(
            namespace, writer_lease=release_lock
        )
        release_lock.assert_canonical()


def _prepare_stage15_critique_namespace_under_lock(
    namespace: BoundOutputNamespace,
    *,
    writer_lease: object,
) -> None:
    require_active_writer_epoch(namespace.run_dir, writer_lease)

    errors: list[str] = []
    for name in _OWNED_ROOT_NAMES:
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise Stage15CritiqueError(
            "Stage 15 critique cleanup was incomplete: " + "; ".join(errors)
        )
    namespace.assert_canonical()


def capture_decision_binding(
    namespace: BoundOutputNamespace,
    evidence: CanonicalExperimentEvidence,
) -> tuple[dict[str, str], dict[str, str]]:
    decision_text = namespace.read_bytes("decision.md")
    decision_structured = namespace.read_bytes("decision_structured.json")
    structured = _parse_json_object(decision_structured, "decision_structured.json")
    canonical = {
        "path": evidence.manifest_path,
        "sha256": evidence.manifest_sha256,
    }
    decision = {
        "text_path": "stage-15/decision.md",
        "text_sha256": sha256_bytes(decision_text),
        "structured_path": "stage-15/decision_structured.json",
        "structured_sha256": sha256_bytes(decision_structured),
    }
    if structured.get("canonical_experiment_evidence_path") != canonical["path"]:
        raise Stage15CritiqueError("structured decision canonical path mismatch")
    if structured.get("canonical_experiment_evidence_sha256") != canonical["sha256"]:
        raise Stage15CritiqueError("structured decision canonical hash mismatch")
    if structured.get("decision_path") != decision["text_path"]:
        raise Stage15CritiqueError("structured decision text path mismatch")
    if structured.get("decision_sha256") != decision["text_sha256"]:
        raise Stage15CritiqueError("structured decision text hash mismatch")
    return canonical, decision


def publish_model_or_none_critique(
    *,
    namespace: BoundOutputNamespace,
    canonical_evidence: Mapping[str, str],
    decision: Mapping[str, str],
    writer_model: str,
    critic_model: str,
    findings: list[Mapping[str, Any]] | None,
    unavailability_reason: str | None,
) -> Stage15CritiquePublication:
    with ReleaseGraphLock.acquire(
        namespace.run_dir, "publish_model_or_none_critique"
    ) as release_lock:
        result = _publish_model_or_none_critique_under_lock(
            namespace=namespace,
            canonical_evidence=canonical_evidence,
            decision=decision,
            writer_model=writer_model,
            critic_model=critic_model,
            findings=findings,
            unavailability_reason=unavailability_reason,
            writer_lease=release_lock,
        )
        release_lock.assert_canonical()
        return result


def _publish_model_or_none_critique_under_lock(
    *,
    namespace: BoundOutputNamespace,
    canonical_evidence: Mapping[str, str],
    decision: Mapping[str, str],
    writer_model: str,
    critic_model: str,
    findings: list[Mapping[str, Any]] | None,
    unavailability_reason: str | None,
    writer_lease: object,
) -> Stage15CritiquePublication:
    require_active_writer_epoch(namespace.run_dir, writer_lease)
    if findings is not None:
        critique: dict[str, Any] = {
            "schema_version": CRITIQUE_SCHEMA_VERSION,
            "policy_version": CRITIQUE_POLICY_VERSION,
            "state": "model_final",
            "recommend_only": True,
            "canonical_evidence": dict(canonical_evidence),
            "decision": dict(decision),
            "writer_model": writer_model,
            "critic_model": critic_model,
            "shared_context": False,
            "findings": [dict(item) for item in findings],
        }
    else:
        critique = {
            "schema_version": CRITIQUE_SCHEMA_VERSION,
            "policy_version": CRITIQUE_POLICY_VERSION,
            "state": "none_final",
            "recommend_only": True,
            "canonical_evidence": dict(canonical_evidence),
            "decision": dict(decision),
            "writer_model": writer_model,
            "unavailability_reason": unavailability_reason,
            "findings": [],
        }
    parsed = parse_critique(critique)
    return _publish_final(namespace, parsed)


def publish_external_critique_or_request(
    *,
    namespace: BoundOutputNamespace,
    canonical_evidence: Mapping[str, str],
    decision: Mapping[str, str],
    writer_model: str,
) -> Stage15CritiquePublication:
    with ReleaseGraphLock.acquire(
        namespace.run_dir, "publish_external_critique_or_request"
    ) as release_lock:
        result = _publish_external_critique_or_request_under_lock(
            namespace=namespace,
            canonical_evidence=canonical_evidence,
            decision=decision,
            writer_model=writer_model,
            writer_lease=release_lock,
        )
        release_lock.assert_canonical()
        return result


def _publish_external_critique_or_request_under_lock(
    *,
    namespace: BoundOutputNamespace,
    canonical_evidence: Mapping[str, str],
    decision: Mapping[str, str],
    writer_model: str,
    writer_lease: object,
) -> Stage15CritiquePublication:
    require_active_writer_epoch(namespace.run_dir, writer_lease)
    try:
        files = namespace.read_flat_directory("external-review")
    except FileNotFoundError:
        files = {}
    except OSError as exc:
        raise Stage15CritiqueError(f"external review namespace is unsafe: {exc}") from exc

    allowed = {"structured.json", "review.md"}
    if not set(files).issubset(allowed):
        raise Stage15CritiqueError("external review namespace has extra entries")
    if "structured.json" not in files:
        pending = {
            "schema_version": CRITIQUE_SCHEMA_VERSION,
            "policy_version": CRITIQUE_POLICY_VERSION,
            "state": "external_pending",
            "canonical_evidence": dict(canonical_evidence),
            "decision": dict(decision),
            "writer_model": writer_model,
            "structured_target_path": EXTERNAL_STRUCTURED_PATH,
            "prose_target_path": EXTERNAL_PROSE_PATH,
        }
        parse_critique(pending)
        namespace.publish_flat_directory(
            "critique-pending",
            {"external_review_request.json": canonical_identity_bytes(pending) + b"\n"},
        )
        expected_entries = set(_DECISION_ROOT_NAMES) | {"critique-pending"}
        if files:
            expected_entries.add("external-review")
        if set(namespace.direct_entries()) != expected_entries:
            raise Stage15CritiqueError(
                "external pending namespace has extra or missing entries"
            )
        namespace.assert_canonical()
        return Stage15CritiquePublication(
            state="external_pending",
            critique=None,
            manifest=None,
            artifacts=(PENDING_REQUEST_PATH,),
        )

    structured = parse_external_structured_review(files["structured.json"])
    if structured["target_canonical_evidence"] != dict(canonical_evidence):
        raise Stage15CritiqueError("external review canonical target mismatch")
    if structured["target_decision"] != dict(decision):
        raise Stage15CritiqueError("external review decision target mismatch")
    prose = files.get("review.md")
    external_prose = (
        {"path": EXTERNAL_PROSE_PATH, "sha256": sha256_bytes(prose)}
        if prose is not None
        else None
    )
    critique = {
        "schema_version": CRITIQUE_SCHEMA_VERSION,
        "policy_version": CRITIQUE_POLICY_VERSION,
        "state": "external_final",
        "recommend_only": True,
        "canonical_evidence": dict(canonical_evidence),
        "decision": dict(decision),
        "writer_model": writer_model,
        "reviewer": structured["reviewer"],
        "external_structured": {
            "path": EXTERNAL_STRUCTURED_PATH,
            "sha256": sha256_bytes(files["structured.json"]),
        },
        "external_prose": external_prose,
        "findings": structured["findings"],
    }
    parsed = parse_critique(critique)
    _publish_final(namespace, parsed)
    try:
        current = namespace.read_flat_directory("external-review")
        if current != files:
            raise Stage15CritiqueError("external review changed during publication")
        return load_stage15_critique_publication_from_namespace(namespace)
    except Exception as exc:
        try:
            _invalidate_final(namespace)
        except Stage15CritiqueError as cleanup_exc:
            exc.add_note(f"critique cleanup also failed: {cleanup_exc}")
        raise


def parse_model_findings_response(content: str) -> list[dict[str, str]]:
    root = _parse_json_object(content.encode("utf-8"), "critic response")
    _exact_fields(root, {"findings"}, "critic response")
    findings = root["findings"]
    if not isinstance(findings, list) or len(findings) > 12:
        raise Stage15CritiqueError("critic findings must be an array of at most 12")
    return _parse_findings(findings)


def parse_critique(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage15CritiqueError("critique must be an object")
    item = dict(value)
    state = item.get("state")
    expected: dict[str, set[str]] = {
        "model_final": {
            "schema_version", "policy_version", "state", "recommend_only",
            "canonical_evidence", "decision", "writer_model", "critic_model",
            "shared_context", "findings",
        },
        "none_final": {
            "schema_version", "policy_version", "state", "recommend_only",
            "canonical_evidence", "decision", "writer_model",
            "unavailability_reason", "findings",
        },
        "external_pending": {
            "schema_version", "policy_version", "state", "canonical_evidence",
            "decision", "writer_model", "structured_target_path",
            "prose_target_path",
        },
        "external_final": {
            "schema_version", "policy_version", "state", "recommend_only",
            "canonical_evidence", "decision", "writer_model", "reviewer",
            "external_structured", "external_prose", "findings",
        },
    }
    if state not in expected:
        raise Stage15CritiqueError("critique state is invalid")
    _exact_fields(item, expected[state], f"{state} critique")
    if type(item["schema_version"]) is not int or item["schema_version"] != CRITIQUE_SCHEMA_VERSION:
        raise Stage15CritiqueError("critique schema version mismatch")
    if item["policy_version"] != CRITIQUE_POLICY_VERSION:
        raise Stage15CritiqueError("critique policy version mismatch")
    item["canonical_evidence"] = _parse_path_hash(
        item["canonical_evidence"], "canonical_evidence"
    )
    item["decision"] = _parse_decision_binding(item["decision"])
    item["writer_model"] = _required_string(item["writer_model"], "writer_model")

    if state == "external_pending":
        if item["structured_target_path"] != EXTERNAL_STRUCTURED_PATH:
            raise Stage15CritiqueError("external structured target path mismatch")
        if item["prose_target_path"] not in {EXTERNAL_PROSE_PATH, None}:
            raise Stage15CritiqueError("external prose target path mismatch")
        return item

    if item["recommend_only"] is not True:
        raise Stage15CritiqueError("recommend_only must be true")
    item["findings"] = _parse_findings(item["findings"])
    if state == "model_final":
        if item["shared_context"] is not False:
            raise Stage15CritiqueError("shared_context must be false")
        item["critic_model"] = _required_string(item["critic_model"], "critic_model")
        if item["critic_model"] == item["writer_model"]:
            raise Stage15CritiqueError("critic and writer models must differ")
    elif state == "none_final":
        if item["findings"]:
            raise Stage15CritiqueError("none_final findings must be empty")
        if item["unavailability_reason"] not in UNAVAILABILITY_REASONS:
            raise Stage15CritiqueError("none_final unavailability reason is invalid")
    else:
        item["reviewer"] = _parse_reviewer(item["reviewer"])
        item["external_structured"] = _parse_path_hash(
            item["external_structured"], "external_structured"
        )
        if item["external_structured"]["path"] != EXTERNAL_STRUCTURED_PATH:
            raise Stage15CritiqueError("external structured path mismatch")
        if item["external_prose"] is not None:
            item["external_prose"] = _parse_path_hash(
                item["external_prose"], "external_prose"
            )
            if item["external_prose"]["path"] != EXTERNAL_PROSE_PATH:
                raise Stage15CritiqueError("external prose path mismatch")
    return item


def parse_external_structured_review(content: bytes) -> dict[str, Any]:
    item = _parse_json_object(content, "external structured review")
    _exact_fields(
        item,
        {
            "schema_version", "policy_version", "target_canonical_evidence",
            "target_decision", "reviewer", "findings",
        },
        "external structured review",
    )
    if type(item["schema_version"]) is not int or item["schema_version"] != CRITIQUE_SCHEMA_VERSION:
        raise Stage15CritiqueError("external review schema version mismatch")
    if item["policy_version"] != CRITIQUE_POLICY_VERSION:
        raise Stage15CritiqueError("external review policy version mismatch")
    item["target_canonical_evidence"] = _parse_path_hash(
        item["target_canonical_evidence"], "target_canonical_evidence"
    )
    item["target_decision"] = _parse_decision_binding(item["target_decision"])
    item["reviewer"] = _parse_reviewer(item["reviewer"])
    item["findings"] = _parse_findings(item["findings"])
    return item


def load_stage15_critique_publication(run_dir: Path) -> Stage15CritiquePublication:
    stage_dir = run_dir / "stage-15"
    try:
        with BoundOutputNamespace.open(run_dir, stage_dir, "stage-15") as namespace:
            publication = load_stage15_critique_publication_from_namespace(namespace)
    except OSError as exc:
        raise Stage15CritiqueError(f"Stage 15 critique namespace is unsafe: {exc}") from exc
    return publication


def _reconstruct_stage15_critique_from_verified_context(
    run_dir: Path,
    *,
    evidence: CanonicalExperimentEvidence,
    writer_model: str,
    critic_model: str,
    critic_source: str,
) -> Stage15CritiquePublication:
    """Rebuild Stage 15 after the caller independently verifies all authority."""
    stage_dir = run_dir / "stage-15"
    try:
        with BoundOutputNamespace.open(run_dir, stage_dir, "stage-15") as namespace:
            critique_bytes = namespace.read_bytes("critique.json")
            critique = parse_critique(
                _parse_json_object(critique_bytes, "critique.json")
            )
            canonical, decision = capture_decision_binding(namespace, evidence)
            _validate_reconstruction_config(
                critique,
                canonical=canonical,
                decision=decision,
                writer_model=writer_model,
                critic_model=critic_model,
                critic_source=critic_source,
            )
            expected_manifest = _build_expected_manifest(critique, critique_bytes)

            manifest_bytes = namespace.read_bytes("stage15_critique_manifest.json")
            manifest = _parse_manifest(manifest_bytes)
            if manifest != expected_manifest:
                raise Stage15CritiqueError(
                    "stored critique manifest differs from expected reconstruction"
                )
            _validate_actual_output_namespace(namespace, critique)
            if capture_decision_binding(namespace, evidence) != (canonical, decision):
                raise Stage15CritiqueError(
                    "critique decision inputs changed during reconstruction"
                )
            if namespace.read_bytes("critique.json") != critique_bytes:
                raise Stage15CritiqueError(
                    "critique source record changed during reconstruction"
                )
            if namespace.read_bytes("stage15_critique_manifest.json") != manifest_bytes:
                raise Stage15CritiqueError(
                    "critique manifest changed during reconstruction"
                )
            namespace.assert_canonical()
    except Stage15CritiqueError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stage15CritiqueError(
            f"Stage 15 independent reconstruction failed: {exc}"
        ) from exc
    return Stage15CritiquePublication(
        state=critique["state"],
        critique=_freeze(critique),
        manifest=_freeze(expected_manifest),
        artifacts=(CRITIQUE_PATH, CRITIQUE_MANIFEST_PATH),
    )


def _validate_reconstruction_config(
    critique: Mapping[str, Any],
    *,
    canonical: Mapping[str, str],
    decision: Mapping[str, str],
    writer_model: str,
    critic_model: str,
    critic_source: str,
) -> None:
    if critique["canonical_evidence"] != canonical:
        raise Stage15CritiqueError("critique canonical evidence mismatch")
    if critique["decision"] != decision:
        raise Stage15CritiqueError("critique decision binding mismatch")
    if critique["writer_model"] != writer_model or not writer_model:
        raise Stage15CritiqueError("critique writer model differs from active config")
    if critic_source == "external":
        if critique["state"] != "external_final":
            raise Stage15CritiqueError(
                "external critic config requires an external final source record"
            )
        return
    if critic_source not in {"", "model"}:
        raise Stage15CritiqueError("critique source is unsupported")
    if critique["state"] not in {"model_final", "none_final"}:
        raise Stage15CritiqueError("model critic config has incompatible critique state")
    if critique["state"] == "model_final":
        if not critic_model or critic_model == writer_model:
            raise Stage15CritiqueError("model critique is not independently configured")
        if critique["critic_model"] != critic_model:
            raise Stage15CritiqueError("critique model differs from active config")
        return
    expected_reason = (
        "critic_not_configured"
        if not critic_model
        else "critic_not_isolated"
        if critic_model == writer_model
        else "critic_call_failed"
    )
    if critique["unavailability_reason"] != expected_reason:
        raise Stage15CritiqueError("critique unavailability reason is not reproducible")


def _publish_final(
    namespace: BoundOutputNamespace, critique: Mapping[str, Any]
) -> Stage15CritiquePublication:
    critique_payload = dict(critique)
    critique_bytes = canonical_identity_bytes(critique_payload) + b"\n"
    manifest = _build_expected_manifest(critique_payload, critique_bytes)
    parsed_manifest = _parse_manifest(canonical_identity_bytes(manifest))
    try:
        namespace.remove_flat_entries(("critique-pending",))
        namespace.write_bytes_atomic("critique.json", critique_bytes)
        if namespace.read_bytes("critique.json") != critique_bytes:
            raise Stage15CritiqueError("critique readback mismatch")
        namespace.write_text_atomic(
            "stage15_critique_manifest.json", canonical_json_text(parsed_manifest)
        )
        namespace.assert_canonical()
        return load_stage15_critique_publication_from_namespace(namespace)
    except Exception as exc:
        try:
            _invalidate_final(namespace)
        except Stage15CritiqueError as cleanup_exc:
            exc.add_note(f"critique cleanup also failed: {cleanup_exc}")
        raise


def _build_expected_manifest(
    critique: Mapping[str, Any], critique_bytes: bytes
) -> dict[str, Any]:
    findings = critique["findings"]
    external_inputs: list[dict[str, str]] = []
    if critique["state"] == "external_final":
        external_inputs.append(dict(critique["external_structured"]))
        if critique["external_prose"] is not None:
            external_inputs.append(dict(critique["external_prose"]))
    return {
        "schema_version": CRITIQUE_MANIFEST_SCHEMA_VERSION,
        "policy_version": CRITIQUE_POLICY_VERSION,
        "state": critique["state"],
        "critique_path": CRITIQUE_PATH,
        "critique_sha256": sha256_bytes(critique_bytes),
        "canonical_evidence": dict(critique["canonical_evidence"]),
        "decision": dict(critique["decision"]),
        "external_inputs": external_inputs,
        "findings_sha256": sha256_bytes(
            canonical_identity_bytes({"findings": findings})
        ),
        "finding_count": len(findings),
        "output_namespace": list(_FINAL_ROOT_NAMES),
    }


def load_stage15_critique_publication_from_namespace(
    namespace: BoundOutputNamespace,
) -> Stage15CritiquePublication:
    manifest_bytes = namespace.read_bytes("stage15_critique_manifest.json")
    manifest = _parse_manifest(manifest_bytes)
    critique_bytes = namespace.read_bytes("critique.json")
    if sha256_bytes(critique_bytes) != manifest["critique_sha256"]:
        raise Stage15CritiqueError("critique hash mismatch")
    critique = parse_critique(_parse_json_object(critique_bytes, "critique.json"))
    _validate_manifest_against_critique(manifest, critique)
    _validate_actual_output_namespace(namespace, critique)
    _validate_source_fixpoint(namespace, critique)
    if namespace.read_bytes("stage15_critique_manifest.json") != manifest_bytes:
        raise Stage15CritiqueError("critique manifest changed during replay")
    if namespace.read_bytes("critique.json") != critique_bytes:
        raise Stage15CritiqueError("critique changed during replay")
    namespace.assert_canonical()
    return Stage15CritiquePublication(
        state=critique["state"],
        critique=_freeze(critique),
        manifest=_freeze(manifest),
        artifacts=(CRITIQUE_PATH, CRITIQUE_MANIFEST_PATH),
    )


def _parse_manifest(content: bytes) -> dict[str, Any]:
    item = _parse_json_object(content, "stage15 critique manifest")
    _exact_fields(
        item,
        {
            "schema_version", "policy_version", "state", "critique_path",
            "critique_sha256", "canonical_evidence", "decision",
            "external_inputs", "findings_sha256", "finding_count",
            "output_namespace",
        },
        "stage15 critique manifest",
    )
    if type(item["schema_version"]) is not int or item["schema_version"] != CRITIQUE_MANIFEST_SCHEMA_VERSION:
        raise Stage15CritiqueError("critique manifest schema version mismatch")
    if item["policy_version"] != CRITIQUE_POLICY_VERSION:
        raise Stage15CritiqueError("critique manifest policy version mismatch")
    if item["state"] not in {"model_final", "none_final", "external_final"}:
        raise Stage15CritiqueError("critique manifest state is not final")
    if item["critique_path"] != CRITIQUE_PATH:
        raise Stage15CritiqueError("critique manifest path mismatch")
    _sha256(item["critique_sha256"], "critique_sha256")
    item["canonical_evidence"] = _parse_path_hash(
        item["canonical_evidence"], "canonical_evidence"
    )
    item["decision"] = _parse_decision_binding(item["decision"])
    if not isinstance(item["external_inputs"], list):
        raise Stage15CritiqueError("external_inputs must be an array")
    item["external_inputs"] = [
        _parse_path_hash(value, "external input") for value in item["external_inputs"]
    ]
    _sha256(item["findings_sha256"], "findings_sha256")
    if type(item["finding_count"]) is not int or item["finding_count"] < 0:
        raise Stage15CritiqueError("finding_count must be a nonnegative integer")
    if item["output_namespace"] != list(_FINAL_ROOT_NAMES):
        raise Stage15CritiqueError("critique output namespace mismatch")
    return item


def _validate_manifest_against_critique(
    manifest: Mapping[str, Any], critique: Mapping[str, Any]
) -> None:
    if manifest["state"] != critique["state"]:
        raise Stage15CritiqueError("critique state mismatch")
    if manifest["canonical_evidence"] != critique["canonical_evidence"]:
        raise Stage15CritiqueError("critique canonical evidence mismatch")
    if manifest["decision"] != critique["decision"]:
        raise Stage15CritiqueError("critique decision binding mismatch")
    findings = critique["findings"]
    if manifest["finding_count"] != len(findings):
        raise Stage15CritiqueError("critique finding count mismatch")
    expected_findings_sha = sha256_bytes(
        canonical_identity_bytes({"findings": findings})
    )
    if manifest["findings_sha256"] != expected_findings_sha:
        raise Stage15CritiqueError("critique findings hash mismatch")
    expected_external: list[Mapping[str, str]] = []
    if critique["state"] == "external_final":
        expected_external.append(critique["external_structured"])
        if critique["external_prose"] is not None:
            expected_external.append(critique["external_prose"])
    if manifest["external_inputs"] != expected_external:
        raise Stage15CritiqueError("critique external input closure mismatch")


def _validate_actual_output_namespace(
    namespace: BoundOutputNamespace, critique: Mapping[str, Any]
) -> None:
    entries = set(namespace.direct_entries())
    expected = set(_DECISION_ROOT_NAMES) | set(_FINAL_ROOT_NAMES)
    if critique["state"] == "external_final":
        expected.add("external-review")
    if entries != expected:
        raise Stage15CritiqueError("critique authority namespace has extra or missing entries")
    if critique["state"] != "external_final":
        return
    try:
        files = namespace.read_flat_directory("external-review")
    except OSError as exc:
        raise Stage15CritiqueError(f"external review namespace is unsafe: {exc}") from exc
    expected_names = {"structured.json"}
    if critique["external_prose"] is not None:
        expected_names.add("review.md")
    if set(files) != expected_names:
        raise Stage15CritiqueError("external review namespace closure mismatch")
    if sha256_bytes(files["structured.json"]) != critique["external_structured"]["sha256"]:
        raise Stage15CritiqueError("external structured review hash mismatch")
    if critique["external_prose"] is not None and sha256_bytes(files["review.md"]) != critique["external_prose"]["sha256"]:
        raise Stage15CritiqueError("external prose review hash mismatch")
    structured = parse_external_structured_review(files["structured.json"])
    if structured["target_canonical_evidence"] != critique["canonical_evidence"]:
        raise Stage15CritiqueError("external structured canonical target mismatch")
    if structured["target_decision"] != critique["decision"]:
        raise Stage15CritiqueError("external structured decision target mismatch")
    if structured["reviewer"] != critique["reviewer"]:
        raise Stage15CritiqueError("external structured reviewer mismatch")
    if structured["findings"] != critique["findings"]:
        raise Stage15CritiqueError("external structured findings mismatch")


def _validate_source_fixpoint(
    namespace: BoundOutputNamespace, critique: Mapping[str, Any]
) -> None:
    """Re-read authority inputs after local replay without adopting new bytes."""

    try:
        evidence = load_canonical_experiment_evidence(namespace.run_dir)
        canonical, decision = capture_decision_binding(namespace, evidence)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stage15CritiqueError(f"critique source fixpoint failed: {exc}") from exc
    if canonical != critique["canonical_evidence"]:
        raise Stage15CritiqueError("critique canonical generation changed during publication")
    if decision != critique["decision"]:
        raise Stage15CritiqueError("critique decision generation changed during publication")


def _invalidate_final(namespace: BoundOutputNamespace) -> None:
    errors: list[str] = []
    for name in ("stage15_critique_manifest.json", "critique.json"):
        try:
            namespace.remove_flat_entries((name,))
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise Stage15CritiqueError("critique invalidation failed: " + "; ".join(errors))


def _parse_findings(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise Stage15CritiqueError("findings must be an array")
    result: list[dict[str, str]] = []
    ids: set[str] = set()
    fields = {
        "id", "severity", "category", "question", "finding",
        "falsification_criterion",
    }
    for raw in value:
        if not isinstance(raw, dict):
            raise Stage15CritiqueError("finding must be an object")
        _exact_fields(raw, fields, "finding")
        finding_id = _required_string(raw["id"], "finding id")
        if finding_id in ids:
            raise Stage15CritiqueError("finding IDs must be unique")
        ids.add(finding_id)
        severity = _required_string(raw["severity"], "finding severity")
        category = _required_string(raw["category"], "finding category")
        if severity not in SEVERITIES:
            raise Stage15CritiqueError("finding severity is invalid")
        if category not in CATEGORIES:
            raise Stage15CritiqueError("finding category is invalid")
        result.append(
            {
                "id": finding_id,
                "severity": severity,
                "category": category,
                "question": _required_string(raw["question"], "finding question"),
                "finding": _required_string(raw["finding"], "finding text"),
                "falsification_criterion": _required_string(
                    raw["falsification_criterion"], "falsification criterion"
                ),
            }
        )
    return result


def _parse_reviewer(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Stage15CritiqueError("reviewer must be an object")
    _exact_fields(value, {"reviewer_id", "reviewer_kind", "organization"}, "reviewer")
    result = {
        key: _required_string(value[key], f"reviewer {key}")
        for key in ("reviewer_id", "reviewer_kind", "organization")
    }
    if result["reviewer_kind"] not in {"human", "independent_agent"}:
        raise Stage15CritiqueError("reviewer kind is invalid")
    return result


def _parse_path_hash(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Stage15CritiqueError(f"{label} must be an object")
    _exact_fields(value, {"path", "sha256"}, label)
    path = _required_string(value["path"], f"{label} path")
    if path.startswith("/") or "\\" in path or "%" in path or ".." in Path(path).parts:
        raise Stage15CritiqueError(f"{label} path is unsafe")
    digest = _sha256(value["sha256"], f"{label} sha256")
    return {"path": path, "sha256": digest}


def _parse_decision_binding(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Stage15CritiqueError("decision binding must be an object")
    _exact_fields(
        value,
        {"text_path", "text_sha256", "structured_path", "structured_sha256"},
        "decision binding",
    )
    if value["text_path"] != "stage-15/decision.md":
        raise Stage15CritiqueError("decision text path mismatch")
    if value["structured_path"] != "stage-15/decision_structured.json":
        raise Stage15CritiqueError("decision structured path mismatch")
    return {
        "text_path": value["text_path"],
        "text_sha256": _sha256(value["text_sha256"], "decision text sha256"),
        "structured_path": value["structured_path"],
        "structured_sha256": _sha256(
            value["structured_sha256"], "decision structured sha256"
        ),
    }


def _parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Stage15CritiqueError(f"{label} has duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                Stage15CritiqueError(f"{label} has nonfinite number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Stage15CritiqueError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage15CritiqueError(f"{label} root must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise Stage15CritiqueError(f"{label} fields must match policy exactly")


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise Stage15CritiqueError(f"{label} must be a trimmed nonempty string")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise Stage15CritiqueError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise Stage15CritiqueError(f"{label} must be a SHA-256 hex digest") from exc
    if value != value.lower():
        raise Stage15CritiqueError(f"{label} must use lowercase hex")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value
