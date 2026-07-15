"""Immutable, replay-validated input graph for canonical Stage 24."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import validate_contract_dict
from researchclaw.pipeline.canonical_experiment_evidence import (
    semantic_config_sha256,
)
from researchclaw.pipeline.stage15_critique import (
    Stage15CritiquePublication,
    load_stage15_critique_publication,
)
from researchclaw.pipeline.stage19_input_bundle import (
    BoundArtifact,
    Stage19InputBundleError,
    _read_regular_file,
)
from researchclaw.pipeline.stage23_input_bundle import (
    Stage23InputBundle,
    load_stage23_input_bundle,
    verify_stage23_input_bundle_unchanged,
)
from researchclaw.pipeline.stage23_verification import (
    Stage23PublicationSnapshot,
    load_stage23_verification_publication,
    parse_stage23_verification_report,
)


STAGE24_INPUT_BUNDLE_POLICY_VERSION = "stage24_input_bundle_v1"


class Stage24InputBundleError(ValueError):
    """Raised when the Stage 24 authority graph is incomplete or changes."""


@dataclass(frozen=True)
class Stage24ModelProjection:
    writer_model: str
    citation_assessment_model: str
    generic_support_model: str
    resolution_assessment_model: str


@dataclass(frozen=True)
class Stage24InputEntry:
    role: str
    artifact: BoundArtifact


@dataclass(frozen=True)
class Stage24InputBundle:
    stage23_inputs: Stage23InputBundle
    stage23_publication: Stage23PublicationSnapshot
    critique_publication: Stage15CritiquePublication
    critique: BoundArtifact
    critique_manifest: BoundArtifact
    canonical_manifest: BoundArtifact
    experiment_contract: BoundArtifact
    paper: BoundArtifact
    verification_report: BoundArtifact
    citation_plan: Mapping[str, Any]
    effective_policy: Mapping[str, Any]
    evidence_cards: tuple[Mapping[str, Any], ...]
    verification: Mapping[str, Any]
    dataset_origin: str
    model_projection: Stage24ModelProjection
    entries: tuple[Stage24InputEntry, ...]
    identity_sha256: str

def load_stage24_input_bundle(
    run_dir: Path,
    runtime_config: RCConfig,
) -> Stage24InputBundle:
    """Capture the complete Stage 04-23 authority graph before Stage 24 calls."""

    try:
        stage23_inputs = load_stage23_input_bundle(run_dir, runtime_config)
        stage23_publication = load_stage23_verification_publication(
            run_dir, stage23_inputs
        )
        critique = _read_regular_file(run_dir, "stage-15/critique.json")
        critique_manifest = _read_regular_file(
            run_dir, "stage-15/stage15_critique_manifest.json"
        )
        critique_publication = load_stage15_critique_publication(run_dir)
        if critique_publication.state == "external_pending":
            raise Stage24InputBundleError("pending critique is not Stage 24 authority")
        if critique_publication.critique is None or critique_publication.manifest is None:
            raise Stage24InputBundleError("final critique publication is incomplete")
        if critique.sha256 != critique_publication.manifest["critique_sha256"]:
            raise Stage24InputBundleError("critique bytes differ from replayed publication")
        if _read_regular_file(run_dir, critique.path) != critique:
            raise Stage24InputBundleError("critique bytes changed during capture")
        if _read_regular_file(run_dir, critique_manifest.path) != critique_manifest:
            raise Stage24InputBundleError("critique manifest changed during capture")

        evidence = stage23_inputs.stage22_inputs.evidence
        canonical_manifest = _read_regular_file(run_dir, evidence.manifest_path)
        if canonical_manifest.sha256 != evidence.manifest_sha256:
            raise Stage24InputBundleError("canonical manifest bytes changed after replay")
        experiment_contract = BoundArtifact(
            evidence.experiment_contract_path,
            evidence.experiment_contract_sha256,
            evidence.experiment_contract_bytes,
        )
        paper = stage23_publication.require_output("paper_final_verified.md")
        verification_report = stage23_publication.require_output(
            "verification_report.json"
        )
        citation_authority = stage23_inputs.stage22_inputs.citation_authority
        citation_plan = _freeze(citation_authority.plan)
        effective_policy = _freeze(citation_authority.effective_policy)
        evidence_cards = tuple(
            _freeze(_strict_json_object(artifact.content, artifact.path))
            for artifact in stage23_inputs.stage22_inputs.stage19_inputs.card_artifacts
            if artifact.path.endswith(".json")
        )
        verification = _freeze(
            parse_stage23_verification_report(verification_report.text())
        )
        contract_value = yaml.safe_load(experiment_contract.text())
        if not isinstance(contract_value, dict):
            raise Stage24InputBundleError("experiment contract root is not an object")
        dataset_origin = validate_contract_dict(contract_value).dataset_origin
        projection = _model_projection(
            stage23_inputs.stage22_inputs.canonical_config
        )
        entries = _build_entries(
            stage23_inputs=stage23_inputs,
            stage23_publication=stage23_publication,
            critique=critique,
            critique_manifest=critique_manifest,
            canonical_manifest=canonical_manifest,
            experiment_contract=experiment_contract,
        )
        identity = _bundle_identity(
            entries=entries,
            canonical_manifest=canonical_manifest,
            stage23_publication=stage23_publication,
            paper=paper,
            critique_manifest=critique_manifest,
            semantic_config_hash=semantic_config_sha256(
                stage23_inputs.stage22_inputs.canonical_config
            ),
            projection=projection,
        )
        # The second critique replay is a comparison-only fixpoint. Its bytes
        # never replace the artifacts captured above.
        if load_stage15_critique_publication(run_dir) != critique_publication:
            raise Stage24InputBundleError("critique publication changed during capture")
        if _read_regular_file(run_dir, critique.path) != critique:
            raise Stage24InputBundleError("critique bytes changed after final replay")
        if _read_regular_file(run_dir, critique_manifest.path) != critique_manifest:
            raise Stage24InputBundleError(
                "critique manifest changed after final replay"
            )
        verify_stage23_input_bundle_unchanged(
            run_dir, runtime_config, stage23_inputs
        )
        if (
            load_stage23_verification_publication(run_dir, stage23_inputs)
            != stage23_publication
        ):
            raise Stage24InputBundleError(
                "Stage 23 publication changed during bundle capture"
            )
    except Stage24InputBundleError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stage24InputBundleError(f"Stage 24 input replay failed: {exc}") from exc
    return Stage24InputBundle(
        stage23_inputs=stage23_inputs,
        stage23_publication=stage23_publication,
        critique_publication=critique_publication,
        critique=critique,
        critique_manifest=critique_manifest,
        canonical_manifest=canonical_manifest,
        experiment_contract=experiment_contract,
        paper=paper,
        verification_report=verification_report,
        citation_plan=citation_plan,
        effective_policy=effective_policy,
        evidence_cards=evidence_cards,
        verification=verification,
        dataset_origin=dataset_origin,
        model_projection=projection,
        entries=entries,
        identity_sha256=identity,
    )


def verify_stage24_input_bundle_unchanged(
    run_dir: Path,
    runtime_config: RCConfig,
    bundle: Stage24InputBundle,
) -> None:
    """Rediscover the complete graph and compare without adopting fresh bytes."""

    current = load_stage24_input_bundle(run_dir, runtime_config)
    if current != bundle:
        raise Stage24InputBundleError("Stage 24 input bundle changed after capture")


def _model_projection(config: RCConfig) -> Stage24ModelProjection:
    projection = Stage24ModelProjection(
        writer_model=(config.llm.primary_model or "").strip(),
        citation_assessment_model=(config.paper_revision.critic_model or "").strip(),
        generic_support_model=(config.paper_revision.critic_model or "").strip(),
        resolution_assessment_model=(config.llm.critic_model or "").strip(),
    )
    critics = (
        projection.citation_assessment_model,
        projection.generic_support_model,
        projection.resolution_assessment_model,
    )
    if not projection.writer_model or any(not model for model in critics):
        raise Stage24InputBundleError("Stage 24 model projection is incomplete")
    if any(model == projection.writer_model for model in critics):
        raise Stage24InputBundleError(
            "Stage 24 assessment models must differ from the writer model"
        )
    return projection


def _build_entries(
    *,
    stage23_inputs: Stage23InputBundle,
    stage23_publication: Stage23PublicationSnapshot,
    critique: BoundArtifact,
    critique_manifest: BoundArtifact,
    canonical_manifest: BoundArtifact,
    experiment_contract: BoundArtifact,
) -> tuple[Stage24InputEntry, ...]:
    stage22 = stage23_inputs.stage22_inputs
    stage19 = stage22.stage19_inputs
    artifacts: list[tuple[str, BoundArtifact]] = [
        ("canonical_manifest", canonical_manifest),
        ("experiment_contract", experiment_contract),
        *[("stage04_18_source", item) for item in stage19.artifacts],
        ("stage19_revised_paper", stage22.stage20_inputs.revised_paper),
        ("stage19_publication_binding", stage22.stage20_inputs.publication_binding),
        ("stage20_quality_report", stage22.stage21_inputs.quality_report),
        ("stage20_fabrication_flags", stage22.stage21_inputs.fabrication_flags),
        ("stage20_manifest", stage22.stage21_inputs.quality_gate_manifest),
        ("stage22_manifest", stage23_inputs.publication.manifest),
        *[("stage22_output", item) for item in stage23_inputs.publication.outputs],
        ("stage23_manifest", stage23_publication.manifest),
        *[("stage23_output", item) for item in stage23_publication.outputs],
        ("stage15_critique", critique),
        ("stage15_critique_manifest", critique_manifest),
    ]
    seen: dict[str, BoundArtifact] = {}
    entries: list[Stage24InputEntry] = []
    for role, artifact in artifacts:
        previous = seen.get(artifact.path)
        if previous is not None:
            raise Stage24InputBundleError(
                f"Stage 24 source path has multiple logical roles: {artifact.path}"
            )
        seen[artifact.path] = artifact
        entries.append(Stage24InputEntry(role=role, artifact=artifact))
    return tuple(entries)


def _bundle_identity(
    *,
    entries: tuple[Stage24InputEntry, ...],
    canonical_manifest: BoundArtifact,
    stage23_publication: Stage23PublicationSnapshot,
    paper: BoundArtifact,
    critique_manifest: BoundArtifact,
    semantic_config_hash: str,
    projection: Stage24ModelProjection,
) -> str:
    payload = {
        "policy_version": STAGE24_INPUT_BUNDLE_POLICY_VERSION,
        "entries": [
            {
                "role": entry.role,
                "normalized_run_relative_path": entry.artifact.path,
                "raw_sha256": entry.artifact.sha256,
            }
            for entry in entries
        ],
        "canonical_manifest": {
            "path": canonical_manifest.path,
            "sha256": canonical_manifest.sha256,
        },
        "stage23_publication": {
            "path": stage23_publication.manifest.path,
            "sha256": stage23_publication.manifest.sha256,
        },
        "paper": {"path": paper.path, "sha256": paper.sha256},
        "critique_publication": {
            "path": critique_manifest.path,
            "sha256": critique_manifest.sha256,
        },
        "active_config_semantic_sha256": semantic_config_hash,
        "models": {
            "writer_model": projection.writer_model,
            "citation_assessment_model": projection.citation_assessment_model,
            "generic_support_model": projection.generic_support_model,
            "resolution_assessment_model": projection.resolution_assessment_model,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _strict_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Stage24InputBundleError(f"invalid captured JSON {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise Stage24InputBundleError(f"captured JSON root is not an object: {label}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value
