"""Deterministic structured Stage 20 quality authority schemas."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_decimal,
)
from researchclaw.pipeline.stage20_publication import Stage20FabricationState


QUALITY_REPORT_SCHEMA_VERSION = 1
FABRICATION_FLAGS_SCHEMA_VERSION = 3
QUALITY_GATE_MANIFEST_SCHEMA_VERSION = 2
DEGRADATION_SIGNAL_SCHEMA_VERSION = 1
STRUCTURED_STAGE20_MODE = "structured-scientific-claim-v1"

_QUALITY_RESPONSE_FIELDS = frozenset(
    {"score_1_to_10", "verdict", "strengths", "weaknesses", "required_actions"}
)
_QUALITY_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_experiment_evidence",
        "cfs",
        "generation_binding_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage19_publication_mode",
        "stage19_publication_binding_path",
        "stage19_publication_binding_sha256",
        "score_1_to_10",
        "verdict",
        "strengths",
        "weaknesses",
        "required_actions",
        "generated",
    }
)
_FABRICATION_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_experiment_evidence",
        "cfs",
        "generation_binding_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage19_publication_mode",
        "stage19_publication_binding_path",
        "stage19_publication_binding_sha256",
        "experiment_failed",
        "quality_score",
        "real_metric_values",
        "verified_values_count",
        "verified_conditions",
        "has_real_data",
        "fabrication_suspected",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "publication_stage_id",
        "stage19_publication_mode",
        "canonical_experiment_evidence",
        "cfs",
        "generation_binding_sha256",
        "source_paper",
        "stage19_authority_manifest",
        "quality_report",
        "fabrication_flags",
        "outcome",
        "quality_verdict",
        "quality_score",
        "quality_threshold",
        "graceful_degradation",
        "degradation_signal",
        "generated",
    }
)
_SIGNAL_FIELDS = frozenset(
    {
        "schema_version",
        "publication_stage_id",
        "outcome",
        "stage19_publication_mode",
        "canonical_experiment_evidence",
        "cfs",
        "generation_binding_sha256",
        "source_paper",
        "stage19_authority_manifest",
        "quality_report",
        "fabrication_flags",
        "quality_verdict",
        "quality_score",
        "quality_threshold",
        "weaknesses",
        "generated",
    }
)


class StructuredStage20AuthorityError(ValueError):
    """A structured Stage 20 object failed strict independent replay."""


def parse_quality_response(content: bytes) -> Mapping[str, Any]:
    payload = _canonical_or_plain_object(content, "quality response")
    if set(payload) != _QUALITY_RESPONSE_FIELDS:
        raise StructuredStage20AuthorityError("quality response fields mismatch")
    _quality_score(payload["score_1_to_10"])
    if payload["verdict"] not in {"proceed", "revise", "reject"}:
        raise StructuredStage20AuthorityError("quality response verdict is invalid")
    for field in ("strengths", "weaknesses", "required_actions"):
        _string_array(payload[field], field)
    return payload


def build_quality_report(
    response: Mapping[str, Any],
    *,
    canonical_evidence_path: str,
    canonical_evidence_sha256: str,
    cfs_sha256: str,
    generation_binding_sha256: str,
    source_paper_sha256: str,
    stage19_manifest_sha256: str,
    generated: str,
) -> bytes:
    if set(response) != _QUALITY_RESPONSE_FIELDS:
        raise StructuredStage20AuthorityError("quality response fields mismatch")
    payload = {
        "schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "canonical_experiment_evidence": _ref(
            canonical_evidence_path, canonical_evidence_sha256
        ),
        "cfs": {"schema_version": 1, "sha256": _digest(cfs_sha256)},
        "generation_binding_sha256": _digest(generation_binding_sha256),
        "source_paper_path": "stage-19/scientific_claim_paper_revised.md",
        "source_paper_sha256": _digest(source_paper_sha256),
        "stage19_publication_mode": STRUCTURED_STAGE20_MODE,
        "stage19_publication_binding_path": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "stage19_publication_binding_sha256": _digest(stage19_manifest_sha256),
        "score_1_to_10": _quality_score(response["score_1_to_10"]),
        "verdict": response["verdict"],
        "strengths": list(_string_array(response["strengths"], "strengths")),
        "weaknesses": list(_string_array(response["weaknesses"], "weaknesses")),
        "required_actions": list(
            _string_array(response["required_actions"], "required_actions")
        ),
        "generated": _generated(generated),
    }
    return _canonical(payload)


def replay_quality_report(content: bytes, *, expected: bytes) -> Mapping[str, Any]:
    payload = _strict_canonical_object(content, "quality report")
    if set(payload) != _QUALITY_REPORT_FIELDS:
        raise StructuredStage20AuthorityError("quality report fields mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != QUALITY_REPORT_SCHEMA_VERSION
        or payload["stage19_publication_mode"] != STRUCTURED_STAGE20_MODE
        or payload["source_paper_path"]
        != "stage-19/scientific_claim_paper_revised.md"
        or payload["stage19_publication_binding_path"]
        != "stage-19/scientific_claim_authority_manifest.json"
    ):
        raise StructuredStage20AuthorityError("quality report header mismatch")
    _ref_value(payload["canonical_experiment_evidence"], "canonical evidence")
    _cfs(payload["cfs"])
    for field in (
        "generation_binding_sha256",
        "source_paper_sha256",
        "stage19_publication_binding_sha256",
    ):
        _digest(payload[field])
    _quality_score(payload["score_1_to_10"])
    if payload["verdict"] not in {"proceed", "revise", "reject"}:
        raise StructuredStage20AuthorityError("quality report verdict is invalid")
    for field in ("strengths", "weaknesses", "required_actions"):
        _string_array(payload[field], field)
    _generated(payload["generated"])
    _require_expected(content, expected, "quality report")
    return payload


def build_fabrication_flags(
    state: Stage20FabricationState,
    *,
    quality_score: object,
    canonical_evidence_path: str,
    canonical_evidence_sha256: str,
    cfs_sha256: str,
    generation_binding_sha256: str,
    source_paper_sha256: str,
    stage19_manifest_sha256: str,
) -> bytes:
    payload = {
        "schema_version": FABRICATION_FLAGS_SCHEMA_VERSION,
        "canonical_experiment_evidence": _ref(
            canonical_evidence_path, canonical_evidence_sha256
        ),
        "cfs": {"schema_version": 1, "sha256": _digest(cfs_sha256)},
        "generation_binding_sha256": _digest(generation_binding_sha256),
        "source_paper_path": "stage-19/scientific_claim_paper_revised.md",
        "source_paper_sha256": _digest(source_paper_sha256),
        "stage19_publication_mode": STRUCTURED_STAGE20_MODE,
        "stage19_publication_binding_path": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "stage19_publication_binding_sha256": _digest(stage19_manifest_sha256),
        "experiment_failed": state.experiment_failed,
        "quality_score": _quality_score(quality_score),
        "real_metric_values": list(state.real_metric_values),
        "verified_values_count": state.verified_values_count,
        "verified_conditions": list(state.verified_conditions),
        "has_real_data": state.has_real_data,
        "fabrication_suspected": state.fabrication_suspected,
    }
    _validate_fabrication_payload(payload)
    return _canonical(payload)


def replay_fabrication_flags(
    content: bytes, *, expected: bytes
) -> Mapping[str, Any]:
    payload = _strict_canonical_object(content, "fabrication flags")
    _validate_fabrication_payload(payload)
    _require_expected(content, expected, "fabrication flags")
    return payload


def derive_outcome(
    *,
    verdict: str,
    score: object,
    threshold: object,
    graceful_degradation: bool,
    state: Stage20FabricationState,
) -> str | None:
    quality_score = Decimal(str(_quality_score(score)))
    quality_threshold = Decimal(str(_quality_score(threshold)))
    if not state.has_real_data or state.fabrication_suspected:
        return None
    if verdict == "proceed" and quality_score >= quality_threshold:
        return "passed"
    if (
        verdict == "revise"
        and quality_score < quality_threshold
        and graceful_degradation is True
    ):
        return "degraded"
    return None


def build_degradation_signal(
    *,
    canonical_evidence_path: str,
    canonical_evidence_sha256: str,
    cfs_sha256: str,
    generation_binding_sha256: str,
    source_paper_sha256: str,
    stage19_manifest_sha256: str,
    quality_report_sha256: str,
    fabrication_flags_sha256: str,
    quality_score: object,
    quality_threshold: object,
    weaknesses: Sequence[str],
    generated: str,
) -> bytes:
    payload = {
        "schema_version": DEGRADATION_SIGNAL_SCHEMA_VERSION,
        "publication_stage_id": "stage20",
        "outcome": "degraded",
        "stage19_publication_mode": STRUCTURED_STAGE20_MODE,
        "canonical_experiment_evidence": _ref(
            canonical_evidence_path, canonical_evidence_sha256
        ),
        "cfs": {"schema_version": 1, "sha256": _digest(cfs_sha256)},
        "generation_binding_sha256": _digest(generation_binding_sha256),
        "source_paper": _ref(
            "stage-19/scientific_claim_paper_revised.md", source_paper_sha256
        ),
        "stage19_authority_manifest": _ref(
            "stage-19/scientific_claim_authority_manifest.json",
            stage19_manifest_sha256,
        ),
        "quality_report": _ref(
            "stage-20/quality_report.json", quality_report_sha256
        ),
        "fabrication_flags": _ref(
            "stage-20/fabrication_flags.json", fabrication_flags_sha256
        ),
        "quality_verdict": "revise",
        "quality_score": _decimal_string(quality_score),
        "quality_threshold": _decimal_string(quality_threshold),
        "weaknesses": list(_string_array(weaknesses, "weaknesses")),
        "generated": _generated(generated),
    }
    return _canonical(payload)


def replay_degradation_signal(
    content: bytes, *, expected: bytes
) -> Mapping[str, Any]:
    payload = _strict_canonical_object(content, "degradation signal")
    if set(payload) != _SIGNAL_FIELDS:
        raise StructuredStage20AuthorityError(
            "degradation signal fields mismatch"
        )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != DEGRADATION_SIGNAL_SCHEMA_VERSION
        or payload["publication_stage_id"] != "stage20"
        or payload["outcome"] != "degraded"
        or payload["stage19_publication_mode"] != STRUCTURED_STAGE20_MODE
        or payload["quality_verdict"] != "revise"
    ):
        raise StructuredStage20AuthorityError(
            "degradation signal header mismatch"
        )
    _validate_common_nested(payload)
    for field in (
        "source_paper",
        "stage19_authority_manifest",
        "quality_report",
        "fabrication_flags",
    ):
        _ref_value(payload[field], field)
    _decimal_string(payload["quality_score"])
    _decimal_string(payload["quality_threshold"])
    _string_array(payload["weaknesses"], "weaknesses")
    _generated(payload["generated"])
    _require_expected(content, expected, "degradation signal")
    return payload


def build_quality_gate_manifest(
    *,
    outcome: str,
    verdict: str,
    canonical_evidence_path: str,
    canonical_evidence_sha256: str,
    cfs_sha256: str,
    generation_binding_sha256: str,
    source_paper_sha256: str,
    stage19_manifest_sha256: str,
    quality_report_sha256: str,
    fabrication_flags_sha256: str,
    quality_score: object,
    quality_threshold: object,
    graceful_degradation: bool,
    degradation_signal_sha256: str | None,
    generated: str,
) -> bytes:
    if outcome not in {"passed", "degraded"}:
        raise StructuredStage20AuthorityError("manifest outcome is invalid")
    if type(graceful_degradation) is not bool:
        raise StructuredStage20AuthorityError(
            "manifest graceful_degradation must be boolean"
        )
    signal = (
        None
        if outcome == "passed"
        else _ref("degradation_signal.json", degradation_signal_sha256)
    )
    if outcome == "passed" and degradation_signal_sha256 is not None:
        raise StructuredStage20AuthorityError("passed outcome forbids signal")
    payload = {
        "schema_version": QUALITY_GATE_MANIFEST_SCHEMA_VERSION,
        "publication_stage_id": "stage20",
        "stage19_publication_mode": STRUCTURED_STAGE20_MODE,
        "canonical_experiment_evidence": _ref(
            canonical_evidence_path, canonical_evidence_sha256
        ),
        "cfs": {"schema_version": 1, "sha256": _digest(cfs_sha256)},
        "generation_binding_sha256": _digest(generation_binding_sha256),
        "source_paper": _ref(
            "stage-19/scientific_claim_paper_revised.md", source_paper_sha256
        ),
        "stage19_authority_manifest": _ref(
            "stage-19/scientific_claim_authority_manifest.json",
            stage19_manifest_sha256,
        ),
        "quality_report": _ref(
            "stage-20/quality_report.json", quality_report_sha256
        ),
        "fabrication_flags": _ref(
            "stage-20/fabrication_flags.json", fabrication_flags_sha256
        ),
        "outcome": outcome,
        "quality_verdict": verdict,
        "quality_score": _decimal_string(quality_score),
        "quality_threshold": _decimal_string(quality_threshold),
        "graceful_degradation": graceful_degradation,
        "degradation_signal": signal,
        "generated": _generated(generated),
    }
    return _canonical(payload)


def replay_quality_gate_manifest(
    content: bytes, *, expected: bytes, signal_present: bool
) -> Mapping[str, Any]:
    payload = _strict_canonical_object(content, "quality gate manifest")
    if set(payload) != _MANIFEST_FIELDS:
        raise StructuredStage20AuthorityError(
            "quality gate manifest fields mismatch"
        )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != QUALITY_GATE_MANIFEST_SCHEMA_VERSION
        or payload["publication_stage_id"] != "stage20"
        or payload["stage19_publication_mode"] != STRUCTURED_STAGE20_MODE
        or payload["outcome"] not in {"passed", "degraded"}
        or payload["quality_verdict"] not in {"proceed", "revise", "reject"}
        or type(payload["graceful_degradation"]) is not bool
    ):
        raise StructuredStage20AuthorityError(
            "quality gate manifest header mismatch"
        )
    _validate_common_nested(payload)
    for field in (
        "source_paper",
        "stage19_authority_manifest",
        "quality_report",
        "fabrication_flags",
    ):
        _ref_value(payload[field], field)
    _decimal_string(payload["quality_score"])
    _decimal_string(payload["quality_threshold"])
    _generated(payload["generated"])
    signal = payload["degradation_signal"]
    if payload["outcome"] == "passed":
        if signal is not None or signal_present:
            raise StructuredStage20AuthorityError(
                "passed degradation signal branch mismatch"
            )
    else:
        path, _digest_value = _ref_value(signal, "degradation signal")
        if path != "degradation_signal.json" or not signal_present:
            raise StructuredStage20AuthorityError(
                "degraded degradation signal branch mismatch"
            )
    _require_expected(content, expected, "quality gate manifest")
    return payload


def _validate_fabrication_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != _FABRICATION_FIELDS:
        raise StructuredStage20AuthorityError("fabrication flags fields mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != FABRICATION_FLAGS_SCHEMA_VERSION
        or payload["stage19_publication_mode"] != STRUCTURED_STAGE20_MODE
        or payload["source_paper_path"]
        != "stage-19/scientific_claim_paper_revised.md"
        or payload["stage19_publication_binding_path"]
        != "stage-19/scientific_claim_authority_manifest.json"
    ):
        raise StructuredStage20AuthorityError("fabrication flags header mismatch")
    _ref_value(payload["canonical_experiment_evidence"], "canonical evidence")
    _cfs(payload["cfs"])
    for field in (
        "generation_binding_sha256",
        "source_paper_sha256",
        "stage19_publication_binding_sha256",
    ):
        _digest(payload[field])
    _quality_score(payload["quality_score"])
    for field in ("experiment_failed", "has_real_data", "fabrication_suspected"):
        if type(payload[field]) is not bool:
            raise StructuredStage20AuthorityError(
                f"fabrication flags {field} must be boolean"
            )
    count = payload["verified_values_count"]
    if type(count) is not int or count < 0:
        raise StructuredStage20AuthorityError(
            "verified_values_count must be a nonnegative integer"
        )
    values = _string_array(payload["real_metric_values"], "real_metric_values")
    conditions = _string_array(
        payload["verified_conditions"], "verified_conditions"
    )
    if tuple(sorted(set(values))) != values:
        raise StructuredStage20AuthorityError(
            "real_metric_values must be sorted and unique"
        )
    if tuple(sorted(set(conditions))) != conditions:
        raise StructuredStage20AuthorityError(
            "verified_conditions must be sorted and unique"
        )
    for value in values:
        if canonical_decimal(Decimal(value)) != value:
            raise StructuredStage20AuthorityError(
                "real_metric_values contains a noncanonical decimal"
            )
    has_data = count > 0
    if (
        payload["has_real_data"] is not has_data
        or payload["experiment_failed"] is not (not has_data)
        or payload["fabrication_suspected"] is not (not has_data)
    ):
        raise StructuredStage20AuthorityError(
            "fabrication flags deterministic relation mismatch"
        )


def _validate_common_nested(payload: Mapping[str, Any]) -> None:
    _ref_value(payload["canonical_experiment_evidence"], "canonical evidence")
    _cfs(payload["cfs"])
    _digest(payload["generation_binding_sha256"])


def _canonical(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StructuredStage20AuthorityError(
            "structured Stage 20 object is not canonical JSON"
        ) from exc


def _canonical_or_plain_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredStage20AuthorityError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise StructuredStage20AuthorityError(f"{label} must be an object")
    return value


def _strict_canonical_object(content: bytes, label: str) -> dict[str, Any]:
    value = _canonical_or_plain_object(content, label)
    if _canonical(value) != content:
        raise StructuredStage20AuthorityError(f"{label} is not canonical")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredStage20AuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise StructuredStage20AuthorityError(f"nonfinite JSON number: {token}")


def _quality_score(value: object) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 10
    ):
        raise StructuredStage20AuthorityError(
            "quality score must be a finite JSON number in [0, 10]"
        )
    return value


def _decimal_string(value: object) -> str:
    if isinstance(value, bool):
        raise StructuredStage20AuthorityError("decimal value cannot be boolean")
    try:
        result = canonical_decimal(Decimal(str(value)))
    except Exception as exc:
        raise StructuredStage20AuthorityError("decimal value is invalid") from exc
    if not result or result in {"NaN", "Infinity", "-Infinity"}:
        raise StructuredStage20AuthorityError("decimal value is nonfinite")
    return result


def _string_array(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise StructuredStage20AuthorityError(
            f"{field} must be an array of nonempty strings"
        )
    return tuple(value)


def _generated(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructuredStage20AuthorityError(
            "generated must be a nonempty UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StructuredStage20AuthorityError(
            "generated must be a UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StructuredStage20AuthorityError(
            "generated must be a UTC timestamp"
        )
    return value


def _ref(path: str, digest: object) -> dict[str, str]:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or ".." in path.split("/")
    ):
        raise StructuredStage20AuthorityError("FileRef path is unsafe")
    return {"path": path, "sha256": _digest(digest)}


def _ref_value(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise StructuredStage20AuthorityError(f"{label} FileRef shape mismatch")
    ref = _ref(value["path"], value["sha256"])
    return ref["path"], ref["sha256"]


def _cfs(value: object) -> tuple[int, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "sha256"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise StructuredStage20AuthorityError("CFS binding shape mismatch")
    return value["schema_version"], _digest(value["sha256"])


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StructuredStage20AuthorityError("value is not a SHA-256")
    return value


def _require_expected(content: bytes, expected: bytes, label: str) -> None:
    if content != expected:
        raise StructuredStage20AuthorityError(
            f"{label} differs from independent expected rebuild"
        )
