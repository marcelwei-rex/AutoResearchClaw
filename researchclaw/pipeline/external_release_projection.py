"""Immutable projections for external canonical-release consumers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.independent_release_reconstruction import (
    IndependentReleaseArtifact,
    reconstruct_expected_release_publications,
)
from researchclaw.pipeline.stage23_verification import (
    parse_stage23_verification_report,
)


class ExternalReleaseProjectionError(ValueError):
    """Raised when an external projection cannot be derived unambiguously."""


@dataclass(frozen=True)
class ExternalReleaseProjection:
    """One replay-validated, immutable release view for external consumers."""

    canonical_manifest_path: str
    canonical_manifest_sha256: str
    candidate_id: str
    selected_result_manifest_path: str
    selected_result_manifest_sha256: str
    selected_execution_path: str
    selected_execution_sha256: str
    metric_observations: Mapping[str, Any]
    structured_results: Mapping[str, Any]
    summary: Mapping[str, Any]
    analysis_text: str
    paper_text: str
    latex_text: str
    verification_report: Mapping[str, Any]
    literature_text: str | None
    review_text: str | None
    project_files: tuple[tuple[str, str], ...]
    authority_artifacts: tuple[IndependentReleaseArtifact, ...]


def load_external_release_projection(run_dir: Path) -> ExternalReleaseProjection:
    """Reconstruct a release before exposing any experiment-derived value."""

    require_canonical_evidence_capabilities("load_external_release_projection")
    reconstructed = reconstruct_expected_release_publications(run_dir)
    evidence = reconstructed.evidence
    artifacts = {artifact.path: artifact for artifact in reconstructed.authority_artifacts}
    if len(artifacts) != len(reconstructed.authority_artifacts):
        raise ExternalReleaseProjectionError("release authority paths are not unique")

    try:
        paper_text = reconstructed.stage24_inputs.paper.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalReleaseProjectionError(
            "canonical release paper is not UTF-8"
        ) from exc
    latex_text = _required_utf8(artifacts, "stage-22/paper.tex")
    verification_artifact = reconstructed.stage23.require_output(
        "verification_report.json"
    )
    try:
        verification = _required_mapping(
            parse_stage23_verification_report(
                verification_artifact.content.decode("utf-8")
            ),
            "Stage 23 verification report",
        )
    except UnicodeDecodeError as exc:
        raise ExternalReleaseProjectionError(
            "Stage 23 verification report is not UTF-8"
        ) from exc
    literature_text = _optional_utf8(artifacts, "stage-07/synthesis.md")
    review_text = _optional_utf8(artifacts, "stage-18/reviews.md")
    project_files = tuple(
        (artifact.logical_name, artifact.content.decode("utf-8"))
        for artifact in evidence.project_artifacts
    )

    candidate_id = evidence.candidate.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ExternalReleaseProjectionError("canonical candidate ID is invalid")
    return ExternalReleaseProjection(
        canonical_manifest_path=evidence.manifest_path,
        canonical_manifest_sha256=evidence.manifest_sha256,
        candidate_id=candidate_id,
        selected_result_manifest_path=evidence.selected_result_manifest_path,
        selected_result_manifest_sha256=evidence.selected_result_manifest_sha256,
        selected_execution_path=evidence.selected_execution_artifact.path,
        selected_execution_sha256=evidence.selected_execution_artifact.sha256,
        metric_observations=evidence.metric_observations,
        structured_results=evidence.structured_results,
        summary=evidence.summary,
        analysis_text=evidence.analysis_text,
        paper_text=paper_text,
        latex_text=latex_text,
        verification_report=verification,
        literature_text=literature_text,
        review_text=review_text,
        project_files=project_files,
        authority_artifacts=reconstructed.authority_artifacts,
    )


def external_json_value(value: Any) -> Any:
    """Convert immutable authority values to JSON-compatible exact values."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): external_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [external_json_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ExternalReleaseProjectionError(
        f"external projection contains unsupported value: {type(value).__name__}"
    )


def _required_utf8(
    artifacts: Mapping[str, IndependentReleaseArtifact], path: str
) -> str:
    artifact = artifacts.get(path)
    if artifact is None:
        raise ExternalReleaseProjectionError(
            f"required release authority artifact is missing: {path}"
        )
    try:
        return artifact.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalReleaseProjectionError(
            f"release authority artifact is not UTF-8: {path}"
        ) from exc


def _optional_utf8(
    artifacts: Mapping[str, IndependentReleaseArtifact], path: str
) -> str | None:
    artifact = artifacts.get(path)
    if artifact is None:
        return None
    try:
        return artifact.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalReleaseProjectionError(
            f"release authority artifact is not UTF-8: {path}"
        ) from exc


def _required_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalReleaseProjectionError(f"{label} is not a mapping")
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        raise ExternalReleaseProjectionError(f"{label} is not a mapping")
    return frozen


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
