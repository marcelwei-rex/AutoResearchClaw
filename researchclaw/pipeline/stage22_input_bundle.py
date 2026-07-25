"""Immutable canonical inputs consumed by Stage 22 export publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import validate_contract_dict
from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    ReplayedCitationAuthority,
    replay_citation_closure,
    _replay_citation_plan_provenance_from_evidence,
)
from researchclaw.literature.citation_policy import parse_config_snapshot_text
from researchclaw.literature.experiment_fact_closure import (
    replay_experiment_fact_closure,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
    load_canonical_experiment_evidence,
    semantic_config_sha256,
)
from researchclaw.pipeline.sectional_validation import extract_citation_keys
from researchclaw.pipeline.stage19_input_bundle import (
    Stage19InputBundle,
    load_stage19_input_bundle,
    parse_stage18_review_structure_report,
)
from researchclaw.pipeline.stage20_input_bundle import (
    Stage20InputBundle,
    load_stage20_input_bundle,
)
from researchclaw.pipeline.stage21_input_bundle import (
    Stage21InputBundle,
    load_stage21_input_bundle,
)


class Stage22InputBundleError(ValueError):
    """Raised when Stage 22 cannot replay one canonical input generation."""


@dataclass(frozen=True)
class Stage22InputBundle:
    evidence: CanonicalExperimentEvidence
    canonical_config: RCConfig
    stage19_inputs: Stage19InputBundle
    stage20_inputs: Stage20InputBundle
    stage21_inputs: Stage21InputBundle
    citation_authority: ReplayedCitationAuthority
    claim_scope: str


def load_stage22_input_bundle(
    run_dir: Path,
    runtime_config: RCConfig,
) -> Stage22InputBundle:
    """Capture and replay all Stage 22 authority before producing output."""

    try:
        evidence = load_canonical_experiment_evidence(run_dir)
        canonical_config = parse_config_snapshot_text(
            evidence.run_config_bytes.decode("utf-8"),
            project_root=run_dir,
            label="canonical experiment config snapshot",
        )
        if semantic_config_sha256(runtime_config) != semantic_config_sha256(
            canonical_config
        ):
            raise Stage22InputBundleError(
                "runtime config differs from canonical experiment generation"
            )
        contract_value = yaml.safe_load(
            evidence.experiment_contract_bytes.decode("utf-8")
        )
        if not isinstance(contract_value, dict):
            raise Stage22InputBundleError(
                "canonical experiment contract root is not an object"
            )
        claim_scope = validate_contract_dict(contract_value).claim_scope

        stage19_inputs = load_stage19_input_bundle(run_dir)
        citation_authority = _replay_citation_plan_provenance_from_evidence(
            stage19_inputs.citation_replay_inputs(),
            canonical_config,
            project_root=run_dir,
            evidence=evidence,
        )
        fact_report = replay_experiment_fact_closure(
            paper_bytes=stage19_inputs.paper.content,
            stored_report_bytes=stage19_inputs.experiment_fact_closure_report.content,
            evidence=evidence,
        )
        citation_report = replay_citation_closure(
            paper_bytes=stage19_inputs.paper.content,
            structure_report_bytes=stage19_inputs.paper_structure_report.content,
            experiment_fact_report_bytes=stage19_inputs.experiment_fact_closure_report.content,
            citation_closure_report_bytes=stage19_inputs.citation_closure_report.content,
            citation_plan_bytes=stage19_inputs.citation_plan.content,
            citation_allowlist_bytes=stage19_inputs.citation_allowlist.content,
            citation_authority=citation_authority,
            evidence=evidence,
            citation_inputs=stage19_inputs.citation_replay_inputs(),
            project_root=run_dir,
        )
        if (
            fact_report["paper_sha256"] != stage19_inputs.paper.sha256
            or citation_report["paper_sha256"] != stage19_inputs.paper.sha256
        ):
            raise Stage22InputBundleError(
                "Stage 17 closure reports do not bind the captured draft"
            )
        review_report = parse_stage18_review_structure_report(
            stage19_inputs.review_structure_report.text(),
            bundle=stage19_inputs,
            canonical_evidence_path=evidence.manifest_path,
            canonical_evidence_sha256=evidence.manifest_sha256,
        )
        if review_report["valid"] is not True:
            raise Stage22InputBundleError("Stage 18 review publication is invalid")

        stage20_inputs = load_stage20_input_bundle(
            run_dir,
            stage19_inputs=stage19_inputs,
            evidence=evidence,
            claim_scope=claim_scope,
        )
        _validate_revised_citations(
            stage20_inputs.revised_paper.text(), citation_authority
        )
        stage21_inputs = load_stage21_input_bundle(
            run_dir,
            stage20_inputs=stage20_inputs,
            evidence=evidence,
            canonical_config=canonical_config,
        )
    except Stage22InputBundleError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise Stage22InputBundleError(
            f"canonical experiment evidence input replay failed: {exc}"
        ) from exc
    return Stage22InputBundle(
        evidence=evidence,
        canonical_config=canonical_config,
        stage19_inputs=stage19_inputs,
        stage20_inputs=stage20_inputs,
        stage21_inputs=stage21_inputs,
        citation_authority=citation_authority,
        claim_scope=claim_scope,
    )


def verify_stage22_input_bundle_unchanged(
    run_dir: Path,
    runtime_config: RCConfig,
    bundle: Stage22InputBundle,
) -> None:
    """Reload the full authority graph and require an exact fixpoint."""

    current = load_stage22_input_bundle(run_dir, runtime_config)
    if current != bundle:
        raise Stage22InputBundleError("Stage 22 input bundle changed after capture")


def _validate_revised_citations(
    paper_text: str,
    authority: ReplayedCitationAuthority,
) -> None:
    cited = set(extract_citation_keys(paper_text))
    eligible = set(authority.allowlist["eligible_keys"])
    planned = {
        citation["cite_key"]
        for claim in authority.plan["claims"]
        for citation in claim["planned_citations"]
    }
    unknown = sorted(cited - eligible)
    unplanned = sorted(cited - planned)
    missing = sorted(planned - cited)
    if unknown or unplanned or missing:
        raise CitationPlanContractError(
            "Stage 19 revised citation closure failed: "
            f"unknown={unknown}, unplanned={unplanned}, missing={missing}"
        )
