"""Independent reconstruction of canonical Stage 15 and Stage 23-25 releases."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable

from researchclaw.literature.citation_policy import parse_config_snapshot_text
from researchclaw.pipeline.canonical_evidence_capabilities import (
    require_canonical_evidence_capabilities,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    parse_experiment_result_set,
    parse_refinement_result_set,
    reconstruct_expected_canonical_evidence,
    semantic_config_sha256,
)
from researchclaw.pipeline.stage15_critique import (
    Stage15CritiquePublication,
    _reconstruct_stage15_critique_from_verified_context,
)
from researchclaw.pipeline.release_graph_lock import ReleaseGraphLock
from researchclaw.pipeline.stage19_input_bundle import _read_regular_file
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
class IndependentReleaseArtifact:
    role: str
    path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class IndependentReleaseReconstruction:
    evidence: CanonicalExperimentEvidence
    critique: Stage15CritiquePublication
    stage24_inputs: Stage24InputBundle
    stage23: Stage23PublicationSnapshot
    stage24: Stage24PublicationSnapshot
    stage25: Stage25PublicationSnapshot
    authority_artifacts: tuple[IndependentReleaseArtifact, ...]
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
    authority_artifacts = _capture_release_authority_artifacts(
        run_dir,
        evidence=evidence,
        stage24_inputs=stage24_inputs,
        stage24=stage24,
        stage25=stage25,
    )
    return IndependentReleaseReconstruction(
        evidence=evidence,
        critique=critique,
        stage24_inputs=stage24_inputs,
        stage23=stage23,
        stage24=stage24,
        stage25=stage25,
        authority_artifacts=authority_artifacts,
        config_semantic_sha256=semantic_config_sha256(config),
    )


def validate_release_authority_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path != unicodedata.normalize("NFC", path)
        or "\\" in path
        or "%" in path
        or path.startswith("/")
        or path.endswith("/")
    ):
        raise IndependentReleaseReconstructionError(
            f"noncanonical release authority path: {path!r}"
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise IndependentReleaseReconstructionError(
            f"noncanonical release authority path: {path!r}"
        )
    if PurePosixPath(path).as_posix() != path:
        raise IndependentReleaseReconstructionError(
            f"noncanonical release authority path: {path!r}"
        )
    return path


def _capture_release_authority_artifacts(
    run_dir: Path,
    *,
    evidence: CanonicalExperimentEvidence,
    stage24_inputs: Stage24InputBundle,
    stage24: Stage24PublicationSnapshot,
    stage25: Stage25PublicationSnapshot,
) -> tuple[IndependentReleaseArtifact, ...]:
    rows: list[tuple[str, str, bytes]] = []
    primary_entries = {
        entry.artifact.path: entry.artifact.content for entry in stage24_inputs.entries
    }

    def add(role: str, path: str, content: bytes) -> None:
        rows.append((role, validate_release_authority_path(path), content))

    def add_disk(role: str, path: str) -> bytes:
        artifact = _read_regular_file(run_dir, validate_release_authority_path(path))
        add(role, artifact.path, artifact.content)
        return artifact.content

    def add_supplemental_disk(role: str, path: str) -> bytes:
        artifact = _read_regular_file(run_dir, validate_release_authority_path(path))
        primary_content = primary_entries.get(artifact.path)
        if primary_content is not None:
            if artifact.content != primary_content:
                raise IndependentReleaseReconstructionError(
                    f"release authority bytes diverged for primary path: {artifact.path}"
                )
            return artifact.content
        add(role, artifact.path, artifact.content)
        return artifact.content

    for entry in stage24_inputs.entries:
        add(entry.role, entry.artifact.path, entry.artifact.content)
    for artifact in stage24.outputs:
        add("stage24_output", artifact.path, artifact.content)
    for artifact in stage24.assessment_files:
        add("stage24_assessment", artifact.path, artifact.content)
    add("stage24_manifest", stage24.manifest.path, stage24.manifest.content)
    add("stage25_audit", stage25.audit.path, stage25.audit.content)
    add("stage25_manifest", stage25.manifest.path, stage25.manifest.content)

    for field, content in (
        ("selected_summary", evidence.summary_bytes),
        ("selected_analysis", evidence.analysis_bytes),
    ):
        ref = evidence.manifest[field]
        add(field, ref["canonical_path"], content)

    for name in (
        "experiment_contract.sha256",
        "domain_selector_policy.json",
        "domain_profile.json",
        "metric_authority_index.json",
        "metric_authority.json",
    ):
        add_supplemental_disk("stage09_authority", f"stage-09/{name}")
    add_supplemental_disk(
        "stage10_seal", "stage-10/selected_candidate_manifest.json"
    )
    _capture_flat_directory(
        run_dir,
        "stage-10/selected_candidate",
        role="stage10_project",
        add_disk=add_supplemental_disk,
    )

    baseline_text = add_supplemental_disk(
        "stage12_manifest", "stage-12/experiment_result_set.json"
    )
    baseline = parse_experiment_result_set(baseline_text.decode("utf-8"))
    add_supplemental_disk(
        "stage12_journal", baseline["invocation_journal"]["path"]
    )
    for ref in baseline["evidence_files"]:
        add_supplemental_disk("stage12_evidence", ref["path"])

    if evidence.selected_result_manifest_path == "stage-13/refinement_result_set.json":
        refinement_text = add_supplemental_disk(
            "selected_result_manifest", evidence.selected_result_manifest_path
        )
        refinement = parse_refinement_result_set(refinement_text.decode("utf-8"))
        add_supplemental_disk(
            "stage13_refinement_log", refinement["refinement_log"]["path"]
        )
        for iteration in refinement["iterations"]:
            for ref in (
                *iteration["project_files"],
                iteration["validation_report"],
                iteration["initial_execution"],
            ):
                add_supplemental_disk("stage13_evidence", ref["path"])
        _capture_flat_directory(
            run_dir,
            "stage-13/experiment_final",
            role="stage13_compatibility",
            add_disk=add_supplemental_disk,
        )
        add_supplemental_disk(
            "stage13_compatibility", "stage-13/experiment_final.py"
        )
    elif evidence.selected_result_manifest_path != "stage-12/experiment_result_set.json":
        raise IndependentReleaseReconstructionError(
            "selected result manifest has a noncanonical path"
        )

    _capture_stage14_candidates(run_dir, add_disk=add_supplemental_disk)

    return _build_exact_release_authority_artifacts(rows)


def _build_exact_release_authority_artifacts(
    rows: list[tuple[str, str, bytes]],
) -> tuple[IndependentReleaseArtifact, ...]:
    captured: dict[str, IndependentReleaseArtifact] = {}
    for role, path, content in rows:
        canonical_path = validate_release_authority_path(path)
        if canonical_path in captured:
            raise IndependentReleaseReconstructionError(
                f"duplicate release authority path: {canonical_path}"
            )
        captured[canonical_path] = IndependentReleaseArtifact(
            role=role,
            path=canonical_path,
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
    return tuple(captured[path] for path in sorted(captured))


def _capture_flat_directory(
    run_dir: Path,
    relative_root: str,
    *,
    role: str,
    add_disk: Callable[[str, str], bytes],
) -> None:
    root = run_dir / relative_root
    if root.is_symlink() or not root.is_dir():
        raise IndependentReleaseReconstructionError(
            f"release authority directory is missing or unsafe: {relative_root}"
        )
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child.is_symlink() or not child.is_file():
            raise IndependentReleaseReconstructionError(
                f"release authority directory is not flat: {relative_root}"
            )
        add_disk(role, f"{relative_root}/{child.name}")


def _capture_stage14_candidates(
    run_dir: Path, *, add_disk: Callable[[str, str], bytes]
) -> None:
    stage_pattern = re.compile(r"stage-14(?:_v[1-9][0-9]*)?")
    for stage_root in sorted(run_dir.iterdir(), key=lambda path: path.name):
        if stage_pattern.fullmatch(stage_root.name) is None:
            continue
        if stage_root.is_symlink() or not stage_root.is_dir():
            raise IndependentReleaseReconstructionError(
                "Stage 14 authority directory is unsafe"
            )
        candidates = stage_root / "evidence_candidates"
        if not candidates.exists():
            continue
        if candidates.is_symlink() or not candidates.is_dir():
            raise IndependentReleaseReconstructionError(
                "Stage 14 candidate collection is unsafe"
            )
        for candidate in sorted(candidates.iterdir(), key=lambda path: path.name):
            relative = candidate.relative_to(run_dir).as_posix()
            _capture_flat_directory(
                run_dir,
                relative,
                role="stage14_candidate",
                add_disk=add_disk,
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
