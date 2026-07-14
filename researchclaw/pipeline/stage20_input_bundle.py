"""Immutable Stage 19 publication consumed by the Stage 20 quality gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
)
from researchclaw.pipeline.sectional_validation import SectionRevisionManifest
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
    Stage19InputBundle,
    Stage19InputBundleError,
    _read_regular_file,
    load_stage19_input_bundle,
)


class Stage20InputBundleError(ValueError):
    """Raised when the Stage 19 publication is absent, ambiguous, or invalid."""


@dataclass(frozen=True)
class Stage20InputBundle:
    stage19_inputs: Stage19InputBundle
    revised_paper: BoundArtifact
    publication_binding: BoundArtifact
    publication_mode: str


_LEGACY_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_experiment_evidence_path",
        "canonical_experiment_evidence_sha256",
        "source_paper_path",
        "source_paper_sha256",
        "source_reviews_path",
        "source_reviews_sha256",
        "revised_paper_path",
        "revised_paper_sha256",
    }
)


def load_stage20_input_bundle(
    run_dir: Path,
    *,
    stage19_inputs: Stage19InputBundle,
    evidence: CanonicalExperimentEvidence,
    claim_scope: str,
) -> Stage20InputBundle:
    """Capture and validate the sole canonical Stage 19 publication."""

    revised = _read_regular_file(run_dir, "stage-19/paper_revised.md")
    legacy = _read_optional(run_dir, "stage-19/revision_evidence_binding.json")
    sectional = _read_optional(run_dir, "stage-19/section_revision_manifest.json")
    if (legacy is None) == (sectional is None):
        raise Stage20InputBundleError(
            "Stage 19 must publish exactly one revision binding"
        )
    if legacy is not None:
        parse_revision_evidence_binding(
            legacy.text(),
            evidence=evidence,
            draft_sha256=stage19_inputs.paper.sha256,
            reviews_sha256=stage19_inputs.reviews.sha256,
            revised_sha256=revised.sha256,
        )
        binding = legacy
        mode = "legacy"
    else:
        assert sectional is not None
        _parse_sectional_publication(
            sectional,
            revised=revised,
            stage19_inputs=stage19_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
        binding = sectional
        mode = "sectional"
    return Stage20InputBundle(
        stage19_inputs=stage19_inputs,
        revised_paper=revised,
        publication_binding=binding,
        publication_mode=mode,
    )


def verify_stage20_input_bundle_unchanged(
    run_dir: Path,
    bundle: Stage20InputBundle,
    *,
    evidence: CanonicalExperimentEvidence,
    claim_scope: str,
) -> None:
    """Reload the Stage 19 namespace and require an exact fixpoint."""

    try:
        fresh_stage19_inputs = load_stage19_input_bundle(run_dir)
        if fresh_stage19_inputs != bundle.stage19_inputs:
            raise Stage20InputBundleError(
                "Stage 04-18 input bundle changed after capture"
            )
        current = load_stage20_input_bundle(
            run_dir,
            stage19_inputs=fresh_stage19_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
    except (Stage19InputBundleError, Stage20InputBundleError) as exc:
        raise Stage20InputBundleError(
            f"Stage 20 input namespace changed after capture: {exc}"
        ) from exc
    if current != bundle:
        raise Stage20InputBundleError("Stage 20 input bundle changed after capture")


def parse_revision_evidence_binding(
    text: str,
    *,
    evidence: CanonicalExperimentEvidence,
    draft_sha256: str,
    reviews_sha256: str,
    revised_sha256: str,
) -> dict[str, object]:
    """Strictly replay the legacy Stage 19 publication binding."""

    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise Stage20InputBundleError("invalid revision evidence binding JSON") from exc
    if not isinstance(value, dict) or set(value) != _LEGACY_BINDING_FIELDS:
        raise Stage20InputBundleError("revision evidence binding fields mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Stage20InputBundleError(
            "revision evidence binding schema_version must be integer 1"
        )
    for field in (
        "canonical_experiment_evidence_sha256",
        "source_paper_sha256",
        "source_reviews_sha256",
        "revised_paper_sha256",
    ):
        _require_sha256(value[field], field)
    expected: dict[str, object] = {
        "schema_version": 1,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": "stage-17/paper_draft.md",
        "source_paper_sha256": draft_sha256,
        "source_reviews_path": "stage-18/reviews.md",
        "source_reviews_sha256": reviews_sha256,
        "revised_paper_path": "stage-19/paper_revised.md",
        "revised_paper_sha256": revised_sha256,
    }
    if value != expected:
        raise Stage20InputBundleError("revision evidence binding does not replay")
    return value


def _parse_sectional_publication(
    artifact: BoundArtifact,
    *,
    revised: BoundArtifact,
    stage19_inputs: Stage19InputBundle,
    evidence: CanonicalExperimentEvidence,
    claim_scope: str,
) -> SectionRevisionManifest:
    try:
        value = json.loads(artifact.text(), object_pairs_hook=_reject_duplicate_keys)
        manifest = SectionRevisionManifest.from_dict(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise Stage20InputBundleError(
            f"invalid sectional revision manifest: {exc}"
        ) from exc
    expected = {
        "completed": True,
        "claim_scope": claim_scope,
        "experiment_contract_path": evidence.experiment_contract_path,
        "experiment_contract_sha256": evidence.experiment_contract_sha256,
        "canonical_experiment_evidence_path": evidence.manifest_path,
        "canonical_experiment_evidence_sha256": evidence.manifest_sha256,
        "source_paper_path": stage19_inputs.paper.path,
        "source_paper_sha256": stage19_inputs.paper.sha256,
        "source_reviews_path": stage19_inputs.reviews.path,
        "source_reviews_sha256": stage19_inputs.reviews.sha256,
        "merged_paper_sha256": revised.sha256,
    }
    for field, expected_value in expected.items():
        if getattr(manifest, field) != expected_value:
            raise Stage20InputBundleError(
                f"sectional revision manifest {field} mismatch"
            )
    return manifest


def _read_optional(run_dir: Path, relative_path: str) -> BoundArtifact | None:
    path = run_dir / relative_path
    if path.is_symlink():
        raise Stage20InputBundleError(f"unsafe Stage 19 binding: {relative_path}")
    if not path.exists():
        return None
    try:
        return _read_regular_file(run_dir, relative_path)
    except Stage19InputBundleError as exc:
        raise Stage20InputBundleError(str(exc)) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Stage20InputBundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Stage20InputBundleError(f"{field} is not a SHA-256")
    return value
