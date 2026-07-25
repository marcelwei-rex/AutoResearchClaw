from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.experiment_runtime.contract import derive_contract
from researchclaw.literature.citation_plan import (
    CITATION_PLAN_DOMAIN_VERSION,
    CitationPlanReplayInputs,
    ReplayedCitationAuthority,
    _build_citation_plan_from_evidence,
    _replay_citation_plan_provenance_from_evidence,
)
from researchclaw.literature.citation_policy import (
    ActiveConfigSnapshotInputs,
    build_effective_citation_policy,
    write_active_config_binding,
)
from researchclaw.literature.evidence_cards import (
    canonical_json_text,
)
from researchclaw.pipeline.canonical_experiment_evidence import (
    CanonicalExperimentEvidence,
)
from researchclaw.pipeline.stage17_typed_citation_authority import (
    CitationTypedAuthorityError,
    parse_evidence_anchor_identity,
    parse_manuscript_claim_identity,
    project_typed_citation_authority,
)
from researchclaw.pipeline.stage_impls._literature import _execute_knowledge_extract
from researchclaw.pipeline.stages import StageStatus
from tests.test_canonical_fact_sheet import _make_evidence
from tests.test_evidence_cards import (
    _SequenceLLM,
    _candidate,
    _card_responses,
    _prepare_stage5,
)


TOPIC = "TrojNet hardware Trojan localization on ISCAS-85 circuits"
EXCERPT = "A bounded prior-work statement is supported by retained evidence."


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _config(tmp_path: Path, *, claim_scope: str) -> RCConfig:
    return RCConfig.from_dict(
        {
            "project": {"name": "typed-citation", "mode": "docs-first"},
            "research": {"topic": TOPIC, "domains": ["security"]},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "none"},
            "knowledge_base": {"backend": "markdown", "root": str(tmp_path / "kb")},
            "llm": {
                "provider": "openai-compatible",
                "base_url": "http://localhost:1234/v1",
                "api_key_env": "RC_TEST_KEY",
                "api_key": "inline-test-key",
                "primary_model": "must-not-be-called",
                "fallback_models": [],
            },
            "experiment": {
                "mode": "sandbox",
                "claim_scope": claim_scope,
                "dataset_origin": "synthetic",
                "time_budget_sec": 30,
                "metric_key": "auprc",
                "metric_direction": "maximize",
                "sandbox": {"python_path": sys.executable},
            },
        },
        project_root=tmp_path,
        check_paths=False,
    )


def _fixture(
    tmp_path: Path, *, claim_scope: str = "pipeline_validation"
) -> tuple[
    CitationPlanReplayInputs,
    ReplayedCitationAuthority,
    CanonicalExperimentEvidence,
]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = _config(tmp_path, claim_scope="pipeline_validation")
    config_text = yaml.safe_dump(config.to_dict(), sort_keys=False)
    config_bytes = config_text.encode("utf-8")
    config_path = run_dir / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    write_active_config_binding(run_dir, config_path)
    contract_payload = derive_contract(config, {}).to_dict()
    contract_bytes = yaml.safe_dump(
        contract_payload, sort_keys=False, allow_unicode=True
    ).encode("utf-8")
    evidence = _make_evidence(
        experiment_contract_sha256=_sha256(contract_bytes),
        experiment_contract_bytes=contract_bytes,
        run_config_path="config.yaml",
        run_config_sha256=_sha256(config_bytes),
        run_config_bytes=config_bytes,
    )
    shortlist = _prepare_stage5(
        run_dir,
        [_candidate(1, abstract=EXCERPT)],
        config,
    )
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    extracted = _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM(_card_responses(shortlist)),  # type: ignore[arg-type]
    )
    assert extracted.status is StageStatus.DONE, extracted.error
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    policy = build_effective_citation_policy(run_dir, config)
    (stage16 / "citation_policy_effective.json").write_text(
        canonical_json_text(policy), encoding="utf-8"
    )
    plan = _build_citation_plan_from_evidence(
        run_dir,
        config,
        plan_status="final",
        evidence=evidence,
    )
    (stage16 / "citation_plan.json").write_text(
        canonical_json_text(plan), encoding="utf-8"
    )
    assert plan["plan_version"] == CITATION_PLAN_DOMAIN_VERSION
    manifest_text = (stage6 / "cards_manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    card_paths = sorted(
        str(entry[field])
        for entry in manifest["cards"]
        for field in ("json_path", "markdown_path")
    )
    pointer_text = (run_dir / "active_config_snapshot.json").read_text(
        encoding="utf-8"
    )
    history_text = (run_dir / "config_snapshot_history.jsonl").read_text(
        encoding="utf-8"
    )
    inputs = CitationPlanReplayInputs(
        candidates_text=(run_dir / "stage-04/candidates.jsonl").read_text(
            encoding="utf-8"
        ),
        registry_text=(run_dir / "stage-04/cite_key_registry.json").read_text(
            encoding="utf-8"
        ),
        bibliography_text=(run_dir / "stage-04/references.bib").read_text(
            encoding="utf-8"
        ),
        shortlist_text=(run_dir / "stage-05/shortlist.jsonl").read_text(
            encoding="utf-8"
        ),
        screening_report_text=(run_dir / "stage-05/screening_report.json").read_text(
            encoding="utf-8"
        ),
        cards_manifest_text=manifest_text,
        card_texts={
            path: (run_dir / path).read_text(encoding="utf-8")
            for path in card_paths
        },
        citation_allowlist_text=(stage6 / "citation_allowlist.json").read_text(
            encoding="utf-8"
        ),
        effective_policy_text=(stage16 / "citation_policy_effective.json").read_text(
            encoding="utf-8"
        ),
        citation_plan_text=canonical_json_text(plan),
        active_config=ActiveConfigSnapshotInputs(
            config_source_path="config.yaml",
            config_source_text=config_text,
            pointer_text=pointer_text,
            history_text=history_text,
            checkpoint_text=(run_dir / "checkpoint.json").read_text(encoding="utf-8")
            if (run_dir / "checkpoint.json").exists()
            else None,
        ),
    )
    authority = _replay_citation_plan_provenance_from_evidence(
        inputs,
        None,
        project_root=run_dir,
        evidence=evidence,
    )
    if claim_scope == "research_release":
        release_config = _config(tmp_path, claim_scope="research_release")
        release_config_text = yaml.safe_dump(release_config.to_dict(), sort_keys=False)
        release_config_bytes = release_config_text.encode("utf-8")
        release_plan = json.loads(inputs.citation_plan_text)
        release_plan["claim_scope"] = "research_release"
        inputs = replace(
            inputs,
            citation_plan_text=canonical_json_text(release_plan),
            active_config=replace(
                inputs.active_config,
                config_source_text=release_config_text,
                pointer_text=None,
                history_text=None,
                checkpoint_text=None,
            ),
        )
        authority = replace(authority, plan=release_plan)
        evidence = replace(
            evidence,
            run_config_sha256=_sha256(release_config_bytes),
            run_config_bytes=release_config_bytes,
        )
    return inputs, authority, evidence


def test_typed_projection_binds_exact_anchor_and_manuscript_claim(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(tmp_path)

    projection = project_typed_citation_authority(
        inputs=inputs,
        citation_authority=authority,
        evidence=evidence,
        project_root=tmp_path,
    )

    assert projection.claim_scope == "pipeline_validation"
    assert len(projection.evidence_anchors) == len(projection.manuscript_claims) == 1
    anchor = projection.evidence_anchors[0]
    claim = projection.manuscript_claims[0]
    assert anchor.evidence_anchor_id == _sha256(anchor.identity_bytes)
    assert claim.manuscript_claim_id == _sha256(claim.identity_bytes)
    assert anchor.excerpt_bytes == EXCERPT.encode("utf-8")
    assert claim.claim_text_bytes == anchor.excerpt_bytes
    assert claim.provenance == "verbatim"
    assert claim.evidence_anchor_id == anchor.evidence_anchor_id
    assert parse_evidence_anchor_identity(anchor.identity_bytes)[
        "evidence_excerpt_sha256"
    ] == _sha256(anchor.excerpt_bytes)
    assert parse_manuscript_claim_identity(claim.identity_bytes)[
        "validation_report_sha256"
    ] == _sha256(claim.validation_report_bytes)


def test_typed_projection_rejects_research_release_verbatim_before_render(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(
        tmp_path, claim_scope="research_release"
    )

    with pytest.raises(
        CitationTypedAuthorityError,
        match="research_release requires validated paraphrase",
    ):
        project_typed_citation_authority(
            inputs=inputs,
            citation_authority=authority,
            evidence=evidence,
            project_root=tmp_path,
        )


def test_typed_projection_rejects_scope_and_generation_mismatch(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(tmp_path)
    bad_plan = json.loads(inputs.citation_plan_text)
    bad_plan["claim_scope"] = "exploratory"
    bad_inputs = replace(inputs, citation_plan_text=canonical_json_text(bad_plan))

    with pytest.raises(CitationTypedAuthorityError, match="claim_scope"):
        project_typed_citation_authority(
            inputs=bad_inputs,
            citation_authority=replace(authority, plan=bad_plan),
            evidence=evidence,
            project_root=tmp_path,
        )

    with pytest.raises(CitationTypedAuthorityError, match="config"):
        project_typed_citation_authority(
            inputs=inputs,
            citation_authority=authority,
            evidence=replace(evidence, run_config_sha256="f" * 64),
            project_root=tmp_path,
        )


def test_typed_projection_rejects_card_bytes_and_unsafe_paths(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(tmp_path)
    card_texts = dict(inputs.card_texts)
    card_texts["stage-06/cards/card-001.json"] += " "

    with pytest.raises(CitationTypedAuthorityError, match="card JSON hash"):
        project_typed_citation_authority(
            inputs=replace(inputs, card_texts=card_texts),
            citation_authority=authority,
            evidence=evidence,
            project_root=tmp_path,
        )

    for unsafe in (
        "stage-14/../root.json",
        "./canonical_experiment_evidence.json",
        "canonical_experiment_evidence.json/",
        "stage-14/./root.json",
        "canonical\x00evidence.json",
        "canonical\nevidence.json",
        "canonical\revidence.json",
        "canonical\tevidence.json",
        "canonical\x7fevidence.json",
    ):
        with pytest.raises(
            CitationTypedAuthorityError, match="canonical evidence path"
        ):
            project_typed_citation_authority(
                inputs=inputs,
                citation_authority=authority,
                evidence=replace(evidence, manifest_path=unsafe),
                project_root=tmp_path,
            )


def test_typed_projection_rejects_claim_excerpt_drift(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(tmp_path)
    plan = json.loads(inputs.citation_plan_text)
    plan["claims"][0]["claim_text"] = "A rewritten claim."
    plan_text = canonical_json_text(plan)

    with pytest.raises(CitationTypedAuthorityError, match="plan replay mismatch"):
        project_typed_citation_authority(
            inputs=replace(inputs, citation_plan_text=plan_text),
            citation_authority=replace(authority, plan=plan),
            evidence=evidence,
            project_root=tmp_path,
        )


def test_typed_projection_rejects_synchronized_fake_card_authority(
    tmp_path: Path,
) -> None:
    inputs, authority, evidence = _fixture(tmp_path)
    card_path = next(
        path for path in inputs.card_texts if path.endswith(".json")
    )
    card = json.loads(inputs.card_texts[card_path])
    excerpt = card["evidence_excerpts"][0]
    forged_text = "Forged synchronized statement remains outside captured candidates."
    forged_hash = _sha256(forged_text.encode("utf-8"))
    excerpt["excerpt_text"] = forged_text
    excerpt["excerpt_sha256"] = forged_hash
    excerpt["char_end"] = excerpt["char_start"] + len(forged_text)
    excerpt["excerpt_id"] = "ev-" + hashlib.sha256(
        (
            f"{card['source_identity']}\n{excerpt['char_start']}\n"
            f"{excerpt['char_end']}\n{forged_hash}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    card_text = canonical_json_text(card)

    manifest = json.loads(inputs.cards_manifest_text)
    manifest["cards"][0]["json_sha256"] = _sha256(card_text.encode("utf-8"))
    manifest_text = canonical_json_text(manifest)
    plan = json.loads(inputs.citation_plan_text)
    plan["cards_manifest_sha256"] = _sha256(manifest_text.encode("utf-8"))
    plan["claims"][0]["claim_text"] = forged_text
    plan["claims"][0]["planned_citations"][0]["evidence_excerpt_ids"] = [
        excerpt["excerpt_id"]
    ]
    plan_text = canonical_json_text(plan)
    forged_inputs = replace(
        inputs,
        cards_manifest_text=manifest_text,
        card_texts={**inputs.card_texts, card_path: card_text},
        citation_plan_text=plan_text,
    )
    forged_authority = replace(authority, plan=plan, cards=(card,))

    with pytest.raises(
        CitationTypedAuthorityError,
        match="typed citation provenance",
    ):
        project_typed_citation_authority(
            inputs=forged_inputs,
            citation_authority=forged_authority,
            evidence=evidence,
            project_root=tmp_path,
        )


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_typed_identity_rejects_non_true_integer_schema(value: object) -> None:
    payload = {
        "schema_version": value,
        "policy_version": "stage17-evidence-anchor-v1",
        "citation_plan_path": "stage-16/citation_plan.json",
        "citation_plan_sha256": "0" * 64,
        "claim_id": "planned-claim-001",
        "section_path": ["Related Work"],
        "claim_type": "prior_work",
        "cite_key": "smith2024bounded",
        "evidence_card_path": "stage-06/cards/card-001.json",
        "evidence_card_sha256": "1" * 64,
        "evidence_excerpt_id": "ev-bounded",
        "evidence_excerpt_sha256": "2" * 64,
        "support_status": "abstract_sufficient",
        "claim_scope": "pipeline_validation",
        "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
        "canonical_experiment_evidence_sha256": "3" * 64,
        "config_source_path": "config.yaml",
        "config_source_sha256": "4" * 64,
    }
    data = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    with pytest.raises(CitationTypedAuthorityError, match="schema_version"):
        parse_evidence_anchor_identity(data)


def test_typed_identity_rejects_duplicate_unknown_and_noncanonical_json() -> None:
    duplicate = (
        b'{"schema_version":1,"schema_version":1,'
        b'"policy_version":"stage17-evidence-anchor-v1"}'
    )
    with pytest.raises(CitationTypedAuthorityError, match="duplicate JSON key"):
        parse_evidence_anchor_identity(duplicate)

    inputs = (
        b'{"claim_scope":"pipeline_validation","evidence_anchor_id":"'
        + b"a" * 64
        + b'","extra":1,"policy_version":"stage17-manuscript-claim-v1",'
        b'"provenance":"verbatim","schema_version":1,'
        b'"claim_text_sha256":"'
        + b"b" * 64
        + b'","validation_policy_version":"stage17-verbatim-validation-v1",'
        b'"validation_report_sha256":"'
        + b"c" * 64
        + b'"}'
    )
    with pytest.raises(CitationTypedAuthorityError, match="fields"):
        parse_manuscript_claim_identity(inputs)


def test_c2_rejects_paraphrased_claim_with_verbatim_validation_policy() -> None:
    payload = {
        "schema_version": 1,
        "policy_version": "stage17-manuscript-claim-v1",
        "evidence_anchor_id": "a" * 64,
        "claim_scope": "pipeline_validation",
        "provenance": "paraphrased",
        "claim_text_sha256": "b" * 64,
        "validation_policy_version": "stage17-verbatim-validation-v1",
        "validation_report_sha256": "c" * 64,
    }
    content = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    with pytest.raises(CitationTypedAuthorityError, match="before C3"):
        parse_manuscript_claim_identity(content)
