"""Artifact publisher — extracts and publishes research artifacts from pipeline runs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from researchclaw.collaboration.repository import ResearchRepository

logger = logging.getLogger(__name__)


class ArtifactPublisher:
    """Extracts artifacts from pipeline run directories and publishes them.

    Scans stage output directories for relevant files and publishes
    structured summaries to the shared repository.
    """

    def __init__(self, repository: ResearchRepository) -> None:
        self._repo = repository

    def publish_from_run_dir(
        self,
        run_id: str,
        run_dir: Path,
    ) -> int:
        """Extract and publish all artifacts from a pipeline run directory.

        Args:
            run_id: Unique run identifier.
            run_dir: Path to the pipeline run output directory.

        Returns:
            Number of artifacts published.
        """
        from researchclaw.pipeline.canonical_evidence_capabilities import (
            require_canonical_evidence_capabilities,
        )

        require_canonical_evidence_capabilities("ArtifactPublisher.publish_from_run_dir")
        from researchclaw.pipeline.external_release_projection import (
            load_external_release_projection,
        )

        projection = load_external_release_projection(run_dir)
        artifacts: dict[str, Any] = {}

        if projection.literature_text:
            artifacts["literature_summary"] = projection.literature_text[:5000]
        artifacts["experiment_results"] = self._render_experiment_projection(
            projection
        )
        code_template = next(
            (
                content
                for logical_name, content in projection.project_files
                if logical_name == "main.py"
            ),
            None,
        )
        if code_template is not None:
            artifacts["code_template"] = code_template[:10000]
        if projection.review_text:
            artifacts["review_feedback"] = projection.review_text[:5000]

        if not artifacts:
            logger.info("No artifacts found in run dir: %s", run_dir)
            return 0

        return self._repo.publish(
            run_id,
            artifacts,
            canonical_manifest_path=projection.canonical_manifest_path,
            canonical_manifest_sha256=projection.canonical_manifest_sha256,
        )

    def _extract_experiments(self, run_dir: Path) -> Any:
        """Extract only independently reconstructed experiment results."""
        from researchclaw.pipeline.canonical_evidence_capabilities import (
            require_canonical_evidence_capabilities,
        )
        from researchclaw.pipeline.external_release_projection import (
            load_external_release_projection,
        )

        require_canonical_evidence_capabilities("ArtifactPublisher._extract_experiments")
        return self._render_experiment_projection(
            load_external_release_projection(run_dir)
        )

    @staticmethod
    def _render_experiment_projection(projection: Any) -> dict[str, Any]:
        from researchclaw.pipeline.external_release_projection import (
            external_json_value,
        )

        return {
            "canonical_manifest_path": projection.canonical_manifest_path,
            "canonical_manifest_sha256": projection.canonical_manifest_sha256,
            "candidate_id": projection.candidate_id,
            "selected_result_manifest_path": projection.selected_result_manifest_path,
            "selected_result_manifest_sha256": (
                projection.selected_result_manifest_sha256
            ),
            "selected_execution_path": projection.selected_execution_path,
            "selected_execution_sha256": projection.selected_execution_sha256,
            "metric_observations": external_json_value(
                projection.metric_observations
            ),
            "structured_results": external_json_value(projection.structured_results),
            "summary": external_json_value(projection.summary),
        }
