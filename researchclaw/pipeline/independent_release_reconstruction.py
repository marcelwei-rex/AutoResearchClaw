"""Independent reconstruction of canonical Stage 15 and Stage 23-25 releases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from researchclaw.literature.citation_policy import parse_config_snapshot_text
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    reconstruct_expected_canonical_evidence,
    semantic_config_sha256,
)
from researchclaw.pipeline.stage15_critique import (
    Stage15CritiquePublication,
    _reconstruct_stage15_critique_from_verified_context,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stage23_verification import Stage23PublicationSnapshot
from researchclaw.pipeline.stage24_input_bundle import (
    Stage24InputBundle,
    load_stage24_input_bundle,
)
from researchclaw.pipeline.stage24_publication import (
    Stage24PublicationSnapshot,
    _parse_manifest as _parse_stage24_manifest,
    load_stage24_publication_snapshot,
)
from researchclaw.pipeline.stage25_publication import (
    Stage25PublicationSnapshot,
    load_stage25_publication,
    parse_stage25_manifest,
)


class IndependentReleaseReconstructionError(ValueError):
    """Raised when run-local release authority cannot be independently rebuilt."""


@dataclass(frozen=True)
class IndependentReleaseReconstruction:
    evidence: CanonicalExperimentEvidence
    critique: Stage15CritiquePublication
    stage23: Stage23PublicationSnapshot
    stage24: Stage24PublicationSnapshot
    stage25: Stage25PublicationSnapshot
    config_semantic_sha256: str


def reconstruct_expected_release_publications(
    run_dir: Path,
) -> IndependentReleaseReconstruction:
    """Rebuild release publications twice without using stored manifest choices."""

    require_canonical_evidence_capabilities(
        "reconstruct_expected_release_publications"
    )
    try:
        with ReleaseGraphLock.acquire(
            run_dir, "reconstruct_expected_release_publications", mode="read"
        ) as release_lock:
            first = _capture_expected_release_publications(run_dir)
            second = _capture_expected_release_publications(run_dir)
            if second != first:
                raise IndependentReleaseReconstructionError(
                    "release publication graph changed during independent reconstruction"
                )
            release_lock.assert_canonical()
    except IndependentReleaseReconstructionError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise IndependentReleaseReconstructionError(
            f"independent release reconstruction failed: {exc}"
        ) from exc
    return first


def _capture_expected_release_publications(
    run_dir: Path,
) -> IndependentReleaseReconstruction:
    evidence = reconstruct_expected_canonical_evidence(run_dir)
    try:
        config = parse_config_snapshot_text(
            evidence.run_config_bytes.decode("utf-8"),
            project_root=run_dir,
            label="independent release config snapshot",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise IndependentReleaseReconstructionError(
            f"canonical release config is invalid: {exc}"
        ) from exc

    writer_model = (config.llm.primary_model or "").strip()
    critic_model = (config.llm.critic_model or "").strip()
    critic_source = (config.llm.critic_source or "").strip()
    critique = _reconstruct_stage15_critique_from_verified_context(
        run_dir,
        evidence=evidence,
        writer_model=writer_model,
        critic_model=critic_model,
        critic_source=critic_source,
    )

    stage24_inputs = load_stage24_input_bundle(run_dir, config)
    captured_evidence = stage24_inputs.stage23_inputs.stage22_inputs.evidence
    if captured_evidence != evidence:
        raise IndependentReleaseReconstructionError(
            "downstream release graph selected a different experiment generation"
        )
    if stage24_inputs.critique_publication != critique:
        raise IndependentReleaseReconstructionError(
            "downstream release graph selected a different critique generation"
        )
    canonical_config = stage24_inputs.stage23_inputs.stage22_inputs.canonical_config
    if semantic_config_sha256(canonical_config) != semantic_config_sha256(config):
        raise IndependentReleaseReconstructionError(
            "downstream release graph selected a different config generation"
        )

    stage23 = stage24_inputs.stage23_publication
    stage24 = load_stage24_publication_snapshot(run_dir, config)
    stage25 = load_stage25_publication(run_dir, config)
    _require_release_generation_bindings(
        evidence=evidence,
        stage24_inputs=stage24_inputs,
        stage23=stage23,
        stage24=stage24,
        stage25=stage25,
    )
    return IndependentReleaseReconstruction(
        evidence=evidence,
        critique=critique,
        stage23=stage23,
        stage24=stage24,
        stage25=stage25,
        config_semantic_sha256=semantic_config_sha256(config),
    )


def _require_release_generation_bindings(
    *,
    evidence: CanonicalExperimentEvidence,
    stage24_inputs: Stage24InputBundle,
    stage23: Stage23PublicationSnapshot,
    stage24: Stage24PublicationSnapshot,
    stage25: Stage25PublicationSnapshot,
) -> None:
    stage24_manifest = _parse_stage24_manifest(stage24.manifest.content)
    expected_stage24_bindings = {
        "stage24_input_bundle_sha256": stage24_inputs.identity_sha256,
        "canonical_manifest": {
            "path": evidence.manifest_path,
            "sha256": evidence.manifest_sha256,
        },
        "stage23_publication": {
            "path": stage23.manifest.path,
            "sha256": stage23.manifest.sha256,
        },
        "critique_publication": {
            "path": stage24_inputs.critique_manifest.path,
            "sha256": stage24_inputs.critique_manifest.sha256,
        },
    }
    for field, expected in expected_stage24_bindings.items():
        if stage24_manifest[field] != expected:
            raise IndependentReleaseReconstructionError(
                f"Stage 24 publication selected a different {field} generation"
            )
    stage25_manifest = parse_stage25_manifest(stage25.manifest.content)
    if stage25.manifest.path != "stage-25/stage25_deai_manifest.json":
        raise IndependentReleaseReconstructionError(
            "Stage 25 reconstruction selected a noncanonical manifest"
        )
    if (
        stage25_manifest["stage24_manifest_path"] != stage24.manifest.path
        or stage25_manifest["stage24_manifest_sha256"] != stage24.manifest.sha256
        or stage25_manifest["source_paper_path"] != stage24.paper.path
        or stage25_manifest["source_paper_sha256"] != stage24.paper.sha256
    ):
        raise IndependentReleaseReconstructionError(
            "Stage 25 publication selected a different Stage 24 generation"
        )
