"""Immutable Stage 20 publication consumed by the Stage 21 archive."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from researchclaw.config import RCConfig
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    canonical_decimal,
)
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
    Stage19InputBundleError,
    _read_regular_file,
)
from researchclaw.pipeline.stage20_input_bundle import Stage20InputBundle
from researchclaw.pipeline.stage20_publication import (
    reconstruct_stage20_fabrication_state,
)


class Stage21InputBundleError(ValueError):
    """Raised when Stage 20 outputs do not replay against their authority."""


@dataclass(frozen=True)
class Stage21InputBundle:
    stage20_inputs: Stage20InputBundle
    quality_report: BoundArtifact
    fabrication_flags: BoundArtifact
    quality_gate_manifest: BoundArtifact
    quality_gate_outcome: str


_BINDING_FIELDS = frozenset(
    {
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage19_publication_mode",
        "stage19_publication_binding_path",
        "stage19_publication_binding_sha256",
    }
)
_QUALITY_BASE_FIELDS = frozenset(
    {
        "score_1_to_10",
        "verdict",
        "strengths",
        "weaknesses",
        "required_actions",
        "generated",
    }
)
_QUALITY_CRITERIA_FIELDS = frozenset(
    {"novelty", "methodological_rigor", "clarity", "reproducibility"}
)
_FABRICATION_FIELDS = frozenset(
    {
        "schema_version",
        *_BINDING_FIELDS,
        "experiment_failed",
        "quality_score",
        "real_metric_values",
        "verified_values_count",
        "verified_conditions",
        "has_real_data",
        "fabrication_suspected",
    }
)
_GATE_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "outcome",
        *_BINDING_FIELDS,
        "quality_report_path",
        "quality_report_sha256",
        "fabrication_flags_path",
        "fabrication_flags_sha256",
        "quality_threshold",
        "graceful_degradation",
        "quality_score",
        "generated",
    }
)
_CANONICAL_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")


def load_stage21_input_bundle(
    run_dir: Path,
    *,
    stage20_inputs: Stage20InputBundle,
    evidence: CanonicalExperimentEvidence,
    canonical_config: RCConfig,
) -> Stage21InputBundle:
    """Capture and replay the exact Stage 20 success-named outputs."""

    try:
        quality_report = _read_regular_file(run_dir, "stage-20/quality_report.json")
        fabrication_flags = _read_regular_file(
            run_dir, "stage-20/fabrication_flags.json"
        )
        quality_gate_manifest = _read_regular_file(
            run_dir, "stage-20/quality_gate_manifest.json"
        )
    except Stage19InputBundleError as exc:
        raise Stage21InputBundleError(str(exc)) from exc
    return replay_stage21_input_bundle(
        quality_report=quality_report,
        fabrication_flags=fabrication_flags,
        quality_gate_manifest=quality_gate_manifest,
        stage20_inputs=stage20_inputs,
        evidence=evidence,
        canonical_config=canonical_config,
    )


def replay_stage21_input_bundle(
    *,
    quality_report: BoundArtifact,
    fabrication_flags: BoundArtifact,
    quality_gate_manifest: BoundArtifact,
    stage20_inputs: Stage20InputBundle,
    evidence: CanonicalExperimentEvidence,
    canonical_config: RCConfig,
) -> Stage21InputBundle:
    """Replay captured Stage 20 bytes without reopening their filesystem paths."""

    expected_binding = _expected_binding(stage20_inputs, evidence)
    quality = _parse_quality_report(
        quality_report.text(), expected_binding=expected_binding
    )
    flags = _parse_fabrication_flags(
        fabrication_flags.text(), expected_binding=expected_binding
    )
    if Decimal(str(quality["score_1_to_10"])) != Decimal(
        str(flags["quality_score"])
    ):
        raise Stage21InputBundleError(
            "quality report and fabrication flags score mismatch"
        )
    expected_state = reconstruct_stage20_fabrication_state(
        evidence, canonical_config
    ).to_dict()
    actual_state = {field: flags[field] for field in expected_state}
    if actual_state != expected_state:
        raise Stage21InputBundleError(
            "fabrication flags do not replay from canonical evidence"
        )
    gate_manifest = _parse_quality_gate_manifest(
        quality_gate_manifest.text(),
        expected_binding=expected_binding,
        quality_report=quality_report,
        fabrication_flags=fabrication_flags,
        quality_score=quality["score_1_to_10"],
        canonical_config=canonical_config,
        has_real_data=bool(expected_state["has_real_data"]),
    )
    return Stage21InputBundle(
        stage20_inputs=stage20_inputs,
        quality_report=quality_report,
        fabrication_flags=fabrication_flags,
        quality_gate_manifest=quality_gate_manifest,
        quality_gate_outcome=str(gate_manifest["outcome"]),
    )


def verify_stage21_input_bundle_unchanged(
    run_dir: Path,
    bundle: Stage21InputBundle,
    *,
    stage20_inputs: Stage20InputBundle,
    evidence: CanonicalExperimentEvidence,
    canonical_config: RCConfig,
) -> None:
    """Reload Stage 20 outputs and require an exact fixpoint."""

    current = load_stage21_input_bundle(
        run_dir,
        stage20_inputs=stage20_inputs,
        evidence=evidence,
        canonical_config=canonical_config,
    )
    if current != bundle:
        raise Stage21InputBundleError("Stage 21 input bundle changed after capture")


def verify_stage21_outputs(
    run_dir: Path,
    *,
    archive_text: str,
    expected_index: dict[str, object],
) -> None:
    """Replay both Stage 21 success outputs from their on-disk bytes."""

    try:
        archive = _read_regular_file(run_dir, "stage-21/archive.md")
        index = _read_regular_file(run_dir, "stage-21/bundle_index.json")
    except Stage19InputBundleError as exc:
        raise Stage21InputBundleError(str(exc)) from exc
    verify_stage21_output_artifacts(
        archive=archive,
        index=index,
        archive_text=archive_text,
        expected_index=expected_index,
    )


def verify_stage21_output_artifacts(
    *,
    archive: BoundArtifact,
    index: BoundArtifact,
    archive_text: str,
    expected_index: dict[str, object],
) -> None:
    """Replay captured Stage 21 output bytes without reopening their paths."""

    if archive.content != archive_text.encode("utf-8"):
        raise Stage21InputBundleError("Stage 21 archive changed during publication")
    parsed_index = parse_stage21_bundle_index(index.text())
    if parsed_index != expected_index:
        raise Stage21InputBundleError("Stage 21 bundle index does not replay")


def parse_stage21_bundle_index(text: str) -> dict[str, object]:
    """Strictly parse the deterministic Stage 21 authority inventory."""

    value = _parse_object(text, "Stage 21 bundle index")
    expected_fields = {
        "schema_version",
        "run_id",
        "generated",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "stage19_publication_mode",
        "stage19_publication_binding_path",
        "stage19_publication_binding_sha256",
        "quality_report_path",
        "quality_report_sha256",
        "fabrication_flags_path",
        "fabrication_flags_sha256",
        "quality_gate_manifest_path",
        "quality_gate_manifest_sha256",
        "archive_path",
        "archive_sha256",
        "artifact_count",
        "artifacts",
    }
    if set(value) != expected_fields:
        raise Stage21InputBundleError("Stage 21 bundle index fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage21InputBundleError("Stage 21 bundle index schema is invalid")
    for field in (
        "run_id",
        "generated",
        "stage19_publication_mode",
    ):
        if not isinstance(value[field], str) or not value[field].strip():
            raise Stage21InputBundleError(
                f"Stage 21 bundle index {field} is invalid"
            )
    for field in (
        "canonical_experiment_evidence_path",
        "source_paper_path",
        "stage19_publication_binding_path",
        "quality_report_path",
        "fabrication_flags_path",
        "quality_gate_manifest_path",
        "archive_path",
    ):
        _require_canonical_path(value[field], f"Stage 21 bundle index {field}")
    for field in (
        "canonical_experiment_evidence_sha256",
        "source_paper_sha256",
        "stage19_publication_binding_sha256",
        "quality_report_sha256",
        "fabrication_flags_sha256",
        "quality_gate_manifest_sha256",
        "archive_sha256",
    ):
        _require_sha256(value[field], f"Stage 21 bundle index {field}")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list):
        raise Stage21InputBundleError("Stage 21 bundle artifacts must be an array")
    paths: list[str] = []
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise Stage21InputBundleError("Stage 21 bundle artifact fields mismatch")
        path = entry["path"]
        _require_canonical_path(path, "Stage 21 bundle artifact path")
        _require_sha256(entry["sha256"], "Stage 21 bundle artifact sha256")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise Stage21InputBundleError("Stage 21 bundle artifact paths are not canonical")
    if type(value["artifact_count"]) is not int or value["artifact_count"] != len(
        artifacts
    ):
        raise Stage21InputBundleError("Stage 21 bundle artifact count mismatch")
    return value


def _expected_binding(
    stage20_inputs: Stage20InputBundle,
    evidence: CanonicalExperimentEvidence,
) -> dict[str, str]:
    return {
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage20_inputs.revised_paper.path,
        "source_paper_sha256": stage20_inputs.revised_paper.sha256,
        "stage19_publication_mode": stage20_inputs.publication_mode,
        "stage19_publication_binding_path": stage20_inputs.publication_binding.path,
        "stage19_publication_binding_sha256": stage20_inputs.publication_binding.sha256,
    }


def _parse_quality_report(
    text: str, *, expected_binding: dict[str, str]
) -> dict[str, object]:
    value = _parse_object(text, "quality report")
    fields = frozenset(value)
    allowed = _QUALITY_BASE_FIELDS | _BINDING_FIELDS
    if fields not in {allowed, allowed | {"criteria"}}:
        raise Stage21InputBundleError("quality report fields mismatch")
    _validate_binding(value, expected_binding, "quality report")
    _finite_score(value["score_1_to_10"], "quality report score")
    if value["verdict"] not in {"proceed", "revise", "reject"}:
        raise Stage21InputBundleError("quality report verdict is invalid")
    for field in ("strengths", "weaknesses", "required_actions"):
        _string_array(value[field], f"quality report {field}")
    if not isinstance(value["generated"], str) or not value["generated"].strip():
        raise Stage21InputBundleError("quality report generated is invalid")
    if "criteria" in value:
        criteria = value["criteria"]
        if not isinstance(criteria, dict) or set(criteria) != _QUALITY_CRITERIA_FIELDS:
            raise Stage21InputBundleError("quality report criteria fields mismatch")
        for field, score in criteria.items():
            _finite_score(score, f"quality report criteria {field}")
    return value


def _parse_fabrication_flags(
    text: str, *, expected_binding: dict[str, str]
) -> dict[str, object]:
    value = _parse_object(text, "fabrication flags")
    if set(value) != _FABRICATION_FIELDS:
        raise Stage21InputBundleError("fabrication flags fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise Stage21InputBundleError("fabrication flags schema_version is invalid")
    _validate_binding(value, expected_binding, "fabrication flags")
    _finite_score(value["quality_score"], "fabrication flags quality_score")
    for field in ("experiment_failed", "has_real_data", "fabrication_suspected"):
        if not isinstance(value[field], bool):
            raise Stage21InputBundleError(f"fabrication flags {field} is invalid")
    if (
        type(value["verified_values_count"]) is not int
        or value["verified_values_count"] < 0
    ):
        raise Stage21InputBundleError(
            "fabrication flags verified_values_count is invalid"
        )
    metrics = value["real_metric_values"]
    if not isinstance(metrics, list) or any(not isinstance(item, str) for item in metrics):
        raise Stage21InputBundleError("fabrication flags metric values are invalid")
    if metrics != sorted(set(metrics)):
        raise Stage21InputBundleError("fabrication flags metric values are not canonical")
    for token in metrics:
        try:
            parsed = Decimal(token)
        except InvalidOperation as exc:
            raise Stage21InputBundleError(
                "fabrication flags metric value is invalid"
            ) from exc
        if not parsed.is_finite() or canonical_decimal(parsed) != token:
            raise Stage21InputBundleError(
                "fabrication flags metric value is not canonical"
            )
    conditions = value["verified_conditions"]
    _string_array(conditions, "fabrication flags verified_conditions")
    if conditions != sorted(set(conditions)):
        raise Stage21InputBundleError(
            "fabrication flags verified_conditions are not canonical"
        )
    has_values = value["verified_values_count"] > 0
    if value["has_real_data"] is not has_values:
        raise Stage21InputBundleError("fabrication flags real-data state is inconsistent")
    if value["experiment_failed"] is has_values:
        raise Stage21InputBundleError("fabrication flags experiment state is inconsistent")
    if value["fabrication_suspected"] is not value["experiment_failed"]:
        raise Stage21InputBundleError(
            "fabrication flags suspicion state is inconsistent"
        )
    return value


def _parse_quality_gate_manifest(
    text: str,
    *,
    expected_binding: dict[str, str],
    quality_report: BoundArtifact,
    fabrication_flags: BoundArtifact,
    quality_score: object,
    canonical_config: RCConfig,
    has_real_data: bool,
) -> dict[str, object]:
    value = _parse_object(text, "quality gate manifest")
    if set(value) != _GATE_MANIFEST_FIELDS:
        raise Stage21InputBundleError("quality gate manifest fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage21InputBundleError("quality gate manifest schema is invalid")
    _validate_binding(value, expected_binding, "quality gate manifest")
    expected_artifacts = {
        "quality_report_path": quality_report.path,
        "quality_report_sha256": quality_report.sha256,
        "fabrication_flags_path": fabrication_flags.path,
        "fabrication_flags_sha256": fabrication_flags.sha256,
    }
    for field, expected in expected_artifacts.items():
        if value[field] != expected:
            raise Stage21InputBundleError(f"quality gate manifest {field} mismatch")
    threshold = canonical_config.research.quality_threshold or 5.0
    expected_threshold = canonical_decimal(Decimal(str(threshold)))
    expected_score = canonical_decimal(Decimal(str(quality_score)))
    if value["quality_threshold"] != expected_threshold:
        raise Stage21InputBundleError("quality gate manifest threshold mismatch")
    if value["quality_score"] != expected_score:
        raise Stage21InputBundleError("quality gate manifest score mismatch")
    graceful = canonical_config.research.graceful_degradation
    if value["graceful_degradation"] is not graceful:
        raise Stage21InputBundleError(
            "quality gate manifest graceful-degradation mismatch"
        )
    score_decimal = Decimal(expected_score)
    threshold_decimal = Decimal(expected_threshold)
    if not has_real_data:
        raise Stage21InputBundleError(
            "quality gate manifest cannot authorize zero-data evidence"
        )
    expected_outcome = (
        "passed"
        if score_decimal >= threshold_decimal
        else "degraded" if graceful else None
    )
    if expected_outcome is None or value["outcome"] != expected_outcome:
        raise Stage21InputBundleError("quality gate manifest outcome mismatch")
    if not isinstance(value["generated"], str) or not value["generated"].strip():
        raise Stage21InputBundleError("quality gate manifest generated is invalid")
    return value


def _parse_object(text: str, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, child in pairs:
            if key in result:
                raise Stage21InputBundleError(f"duplicate {label} key: {key}")
            result[key] = child
        return result

    def reject_constant(token: str) -> None:
        raise Stage21InputBundleError(f"nonfinite {label} number: {token}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise Stage21InputBundleError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise Stage21InputBundleError(f"{label} root must be an object")
    return value


def _validate_binding(
    value: dict[str, object], expected: dict[str, str], label: str
) -> None:
    for field, expected_value in expected.items():
        if value.get(field) != expected_value:
            raise Stage21InputBundleError(f"{label} {field} mismatch")


def _finite_score(value: object, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= value <= 10
    ):
        raise Stage21InputBundleError(f"{label} is invalid")


def _string_array(value: object, label: str) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise Stage21InputBundleError(f"{label} must be a string array")


def _require_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage21InputBundleError(f"{label} is invalid")


def _require_canonical_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or _CANONICAL_PATH_RE.fullmatch(value) is None
    ):
        raise Stage21InputBundleError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise Stage21InputBundleError(f"{label} is invalid")
    return value
