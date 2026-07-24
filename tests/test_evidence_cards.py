"""Stage 6 strict evidence-card and deterministic-renderer tests."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures(
    "canonical_evidence_migration_complete",
    "consumer_evidence_fixture",
)
import yaml

from researchclaw.literature import citation_plan as citation_plan_module
from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.domains.detector import set_forced_profile
from researchclaw.experiment_runtime.contract import derive_contract, dump_contract
from researchclaw.experiment_runtime.contract import find_stage09_contract, load_contract
from researchclaw.literature.citation_policy import (
    CitationPolicyContractError,
    load_effective_citation_policy,
    parse_citation_allowlist,
    parse_effective_citation_policy,
    resolve_active_config_snapshot,
    validate_citation_allowlist,
    write_active_config_binding,
)
from researchclaw.literature.citation_identity import seal_citation_collection
from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    build_citation_closure_report,
    build_citation_closure_from_texts,
    build_citation_plan_from_replayed_inputs,
    build_citation_writer_instruction,
    build_citation_writer_instruction_from_authority,
    capture_replayed_citation_authority,
    load_final_citation_plan,
    parse_citation_plan,
    parse_citation_closure_report,
    validate_citation_closure_report,
    validate_paper_citation_minimum,
    verify_captured_citation_authority_unchanged,
)
from researchclaw.literature.citation_support import (
    CitationSupportContractError,
    build_citation_support_closure,
    parse_citation_support_closure,
)
from researchclaw.literature.evidence_cards import (
    EvidenceCardContractError,
    build_cards_manifest,
    build_evidence_card,
    canonical_json_text,
    load_validated_cards,
    parse_card_batch_response,
    parse_evidence_card,
    render_evidence_card_markdown,
    validate_cards_artifacts,
)
from researchclaw.literature.experiment_fact_closure import (
    build_experiment_fact_closure_report,
    canonical_experiment_fact_json_text,
    parse_experiment_fact_closure_report,
    remove_unsupported_experiment_fact_blocks,
)
from researchclaw.literature.screening import SCREEN_BATCH_SIZE, sha256_text
from researchclaw.literature.verify import (
    CitationResult,
    VerificationReport,
    VerifyStatus,
    parse_bibtex_entries,
)
from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline.stage_impls._literature import (
    _execute_knowledge_extract,
    _execute_literature_screen,
)
from researchclaw.pipeline.stage_impls._paper_writing import _execute_paper_outline
from researchclaw.pipeline.stage_impls._paper_writing import _execute_paper_draft
from researchclaw.pipeline.stage_impls._review_publish import (
    _execute_citation_verify,
    _execute_export_publish,
    _execute_peer_review,
    _execute_quality_gate,
)
from researchclaw.pipeline.stage_impls._release_audit import _execute_truth_audit
from researchclaw.pipeline.citation_release_audit import (
    CitationAuditError,
    audit_citation_evidence,
)
from researchclaw.pipeline.stage19_input_bundle import BoundArtifact
from researchclaw.pipeline.stage22_publication import Stage22PublicationSnapshot
from researchclaw.pipeline.stage23_input_bundle import Stage23InputBundle
from researchclaw.pipeline._domain import _prompt_bank_domain_from_config
from researchclaw.pipeline.stage_impls._synthesis import _execute_synthesis
from researchclaw.pipeline.stages import StageStatus


class _SequenceLLM:
    def __init__(self, responses: list[str | LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.call_kwargs: list[dict[str, object]] = []

    def chat(
        self, messages: list[dict[str, str]], **_kwargs: object
    ) -> SimpleNamespace:
        self.calls.append(messages[0]["content"])
        self.call_kwargs.append(dict(_kwargs))
        if not self.responses:
            raise RuntimeError("unexpected extra LLM call")
        response = self.responses.pop(0)
        if isinstance(response, LLMResponse):
            return response  # type: ignore[return-value]
        return SimpleNamespace(
            content=response,
            model="fixture-model",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            finish_reason="stop",
            truncated=False,
        )


def _config(claim_scope: str = "pipeline_validation") -> SimpleNamespace:
    return SimpleNamespace(
        research=SimpleNamespace(
            topic="hardware runtime detection",
            domains=("hardware security",),
            quality_threshold=6.0,
        ),
        experiment=SimpleNamespace(claim_scope=claim_scope),
    )


def _candidate(index: int, *, abstract: str | None = None) -> dict[str, Any]:
    return {
        "paper_id": f"provider-{index}",
        "title": f"Hardware Detection Study {index}",
        "authors": [{"name": "Jane Smith"}],
        "year": 2024,
        "abstract": abstract
        or "Hardware runtime detection uses performance counters for attacks.",
        "venue": "Security Conference",
        "citation_count": 100 - index,
        "doi": f"10.1000/{index:03d}",
        "arxiv_id": "",
        "url": "",
        "source": "semantic_scholar",
    }


def _screen_response(
    source_ids: list[str], *, batch_id: str = "screen-batch-001"
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "batch_id": batch_id,
            "decisions": [
                {
                    "source_identity": source_id,
                    "decision": "keep",
                    "relevance_score": 0.9,
                    "quality_score": 0.8,
                    "reason": "directly relevant",
                }
                for source_id in source_ids
            ],
        }
    )


def _card_response(
    rows: list[dict[str, Any]], *, batch_id: str = "card-batch-001"
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "batch_id": batch_id,
            "cards": [
                {
                    "source_identity": row["source_identity"],
                    "summary_text": {
                        "problem": "Detect attacks at runtime.",
                        "method": "Use hardware counters.",
                        "data": "Retained abstract evidence.",
                        "metrics": "Detection performance.",
                        "findings": "Counters expose attack behavior.",
                        "limitations": "The abstract reports limited detail.",
                    },
                    "evidence_excerpt_texts": [row["abstract"]],
                }
                for row in rows
            ],
        },
        ensure_ascii=False,
    )


def _card_responses(rows: list[dict[str, Any]]) -> list[str]:
    return [
        _card_response([row], batch_id=f"card-batch-{index:03d}")
        for index, row in enumerate(rows, start=1)
    ]


def _prepare_stage5(
    run_dir: Path,
    candidates: list[dict[str, Any]],
    config: Any | None = None,
) -> list[dict[str, Any]]:
    sealed = seal_citation_collection(candidates)
    stage4 = run_dir / "stage-04"
    stage4.mkdir(parents=True)
    (stage4 / "candidates.jsonl").write_text(sealed.candidates_jsonl, encoding="utf-8")
    (stage4 / "references.bib").write_text(sealed.bibliography, encoding="utf-8")
    (stage4 / "cite_key_registry.json").write_text(
        canonical_json_text(sealed.registry), encoding="utf-8"
    )
    source_ids = [str(row["source_identity"]) for row in sealed.candidates]
    stage5 = run_dir / "stage-05"
    stage5.mkdir()
    result = _execute_literature_screen(
        stage5,
        run_dir,
        config or _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM(
            [
                _screen_response(
                    source_ids[start:start + SCREEN_BATCH_SIZE],
                    batch_id=f"screen-batch-{start // SCREEN_BATCH_SIZE + 1:03d}",
                )
                for start in range(0, len(source_ids), SCREEN_BATCH_SIZE)
            ]
        ),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    return [
        json.loads(line)
        for line in (stage5 / "shortlist.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _real_config_snapshot(
    run_dir: Path,
    *,
    claim_scope: str = "pipeline_validation",
    profile: str | None = None,
) -> RCConfig:
    run_dir.mkdir(parents=True, exist_ok=True)
    source = Path("config.deepseek.sectional-dry-run.yaml")
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["experiment"]["claim_scope"] = claim_scope
    if profile is not None:
        raw["project"]["profile"] = profile
    snapshot = run_dir / "config.yaml"
    snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    write_active_config_binding(run_dir, snapshot)
    return config


def _prepare_stage23_fixture(
    run_dir: Path,
    *,
    claim_scope: str = "pipeline_validation",
    fail_last_card: bool = False,
    profile: str | None = None,
) -> tuple[RCConfig, str, tuple[str, ...]]:
    config = _real_config_snapshot(
        run_dir, claim_scope=claim_scope, profile=profile
    )
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(
        derive_contract(config, {"datasets": [config.experiment.dataset_origin]}),
        stage9 / "experiment_contract.yaml",
    )
    candidate_count = 15 if claim_scope != "pipeline_validation" else 5
    shortlist = _prepare_stage5(
        run_dir,
        [_candidate(i) for i in range(1, candidate_count + 1)],
        config,
    )
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    card_responses = _card_responses(shortlist)
    if fail_last_card:
        card_responses[-1:] = ["{}", "{}"]
    extracted = _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM(card_responses),  # type: ignore[arg-type]
    )
    assert extracted.status is StageStatus.DONE, extracted.error
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    outlined = _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    )
    assert outlined.status is StageStatus.DONE, outlined.error
    plan = load_final_citation_plan(run_dir, config)
    planned_keys = tuple(
        citation["cite_key"]
        for claim in plan["claims"]
        for citation in claim["planned_citations"]
    )
    by_section: dict[str, list[str]] = {}
    for claim in plan["claims"]:
        section = str(claim["section_path"][0])
        key = str(claim["planned_citations"][0]["cite_key"])
        claim_text = str(claim["claim_text"])
        insertion = len(claim_text) - 1 if claim_text[-1] in ".!?" else len(claim_text)
        bound_sentence = claim_text[:insertion] + f" [{key}]" + claim_text[insertion:]
        by_section.setdefault(section, []).append(bound_sentence)
    paper_text = "\n\n".join(
        f"## {section}\n\n" + " ".join(sentences)
        for section, sentences in by_section.items()
    )
    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    (stage22 / "paper_final.md").write_text(paper_text, encoding="utf-8")
    return config, paper_text, planned_keys


def test_stage17_citation_authority_uses_captured_generation_and_rejects_late_plan(
    tmp_path: Path,
) -> None:
    config, _paper, _keys = _prepare_stage23_fixture(tmp_path)
    captured = capture_replayed_citation_authority(tmp_path, config)
    plan_path = tmp_path / "stage-16" / "citation_plan.json"
    original = plan_path.read_text(encoding="utf-8")
    tampered = json.loads(original)
    tampered["claims"][0]["claim_text"] += " changed"
    plan_path.write_text(canonical_json_text(tampered), encoding="utf-8")

    with pytest.raises(CitationPlanContractError, match="changed after"):
        verify_captured_citation_authority_unchanged(tmp_path, captured)
    assert captured.inputs.citation_plan_text == original
    assert captured.replayed.plan["claims"][0]["claim_text"] != tampered["claims"][0][
        "claim_text"
    ]


def test_stage17_citation_capture_rejects_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, _paper, _keys = _prepare_stage23_fixture(run_dir)
    plan_path = run_dir / "stage-16" / "citation_plan.json"
    plan_path.unlink()
    os.mkfifo(plan_path)

    def timeout_handler(_signum: int, _frame: object) -> None:
        raise TimeoutError("citation capture blocked on FIFO")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(CitationPlanContractError, match="cannot read"):
            capture_replayed_citation_authority(run_dir, config)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 0.5


def test_stage17_citation_capture_reads_held_inode_after_parent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    detached = tmp_path / "run-detached"
    config, _paper, _keys = _prepare_stage23_fixture(run_dir)
    original_read = citation_plan_module._CitationAuthorityReader.read_text
    replaced = False

    def replace_before_first_read(
        reader: object, relative_path: str
    ) -> str:
        nonlocal replaced
        if not replaced:
            replaced = True
            run_dir.rename(detached)
            replacement_stage = run_dir / "stage-04"
            replacement_stage.mkdir(parents=True)
            os.mkfifo(replacement_stage / "candidates.jsonl")
        return original_read(reader, relative_path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        citation_plan_module._CitationAuthorityReader,
        "read_text",
        replace_before_first_read,
    )
    with pytest.raises(RuntimeError, match="release_graph_run_directory_changed"):
        capture_replayed_citation_authority(run_dir, config)
    assert (run_dir / "stage-04" / "candidates.jsonl").is_fifo()


def _patch_stage23_canonical_input(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    config: RCConfig,
    paper_text: str,
    planned_keys: tuple[str, ...],
    *,
    bibliography_text: str | None = None,
) -> None:
    def bound(path: str, content: bytes) -> BoundArtifact:
        return BoundArtifact(path, sha256_text(content.decode("utf-8")), content)

    paper = bound("stage-22/paper_final.md", paper_text.encode("utf-8"))
    bib_bytes = (
        bibliography_text.encode("utf-8")
        if bibliography_text is not None
        else (run_dir / "stage-04/references.bib").read_bytes()
    )
    bibliography = bound("stage-22/references.bib", bib_bytes)
    latex = bound(
        "stage-22/paper.tex",
        (" ".join(f"\\cite{{{key}}}" for key in planned_keys) + "\n").encode(
            "utf-8"
        ),
    )
    manifest = bound("stage-22/stage22_export_manifest.json", b"sealed\n")
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
    )
    inputs = SimpleNamespace(
        evidence=evidence,
        canonical_config=config,
        claim_scope=config.experiment.claim_scope,
    )
    bundle = Stage23InputBundle(
        stage22_inputs=inputs,  # type: ignore[arg-type]
        publication=Stage22PublicationSnapshot(
            manifest=manifest,
            outputs=(paper, bibliography, latex),
        ),
        paper=paper,
        bibliography=bibliography,
        latex=latex,
        cited_keys=tuple(sorted(planned_keys)),
        claim_scope=config.experiment.claim_scope,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.load_stage23_input_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_stage23_input_bundle_unchanged",
        lambda *_args, **_kwargs: None,
    )


def _verification_report(
    keys: tuple[str, ...], *, status: VerifyStatus = VerifyStatus.VERIFIED
) -> VerificationReport:
    results = [
        CitationResult(
            cite_key=key,
            title=f"Verified title for {key}",
            status=status,
            confidence=0.95 if status is VerifyStatus.VERIFIED else 0.0,
            method="title_search" if status is not VerifyStatus.SKIPPED else "skipped",
        )
        for key in keys
    ]
    return VerificationReport(
        total=len(results),
        verified=sum(result.status is VerifyStatus.VERIFIED for result in results),
        suspicious=sum(result.status is VerifyStatus.SUSPICIOUS for result in results),
        hallucinated=sum(result.status is VerifyStatus.HALLUCINATED for result in results),
        skipped=sum(result.status is VerifyStatus.SKIPPED for result in results),
        results=results,
    )


def _run_stage23_verified(
    run_dir: Path,
    config: RCConfig,
    planned_keys: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper_text = (run_dir / "stage-22/paper_final.md").read_text(encoding="utf-8")
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(planned_keys),
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([json.dumps({key: 0.9 for key in planned_keys})]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE, result.error


def _prepare_e9_run(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str | None = None,
) -> tuple[RCConfig, tuple[str, ...]]:
    config, paper_text, planned_keys = _prepare_stage23_fixture(
        run_dir, profile=profile
    )
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"detection_f1": 0.95}}), encoding="utf-8"
    )
    stage17 = run_dir / "stage-17"
    stage17.mkdir()
    (stage17 / "paper_draft.md").write_text(paper_text, encoding="utf-8")
    structure_text = canonical_json_text(
        {
            "schema_version": 1,
            "valid": True,
            "source_sha256": sha256_text(paper_text),
            "section_count": 1,
            "issues": [],
        }
    )
    (stage17 / "paper_structure_report.json").write_text(structure_text)
    experiment_text = canonical_experiment_fact_json_text(
        build_experiment_fact_closure_report(run_dir, paper_text=paper_text)
    )
    (stage17 / "experiment_fact_closure_report.json").write_text(experiment_text)
    closure = build_citation_closure_report(
        run_dir,
        config,
        paper_text=paper_text,
        structure_report_text=structure_text,
        experiment_fact_report_text=experiment_text,
    )
    assert closure["valid"] is True
    (stage17 / "citation_closure_report.json").write_text(
        canonical_json_text(closure), encoding="utf-8"
    )
    _run_stage23_verified(run_dir, config, planned_keys, monkeypatch)
    support = build_citation_support_closure(
        run_dir,
        config,
        paper_text=paper_text,
        assessor=lambda _payload: {
            "verdict": "supported",
            "reason": "The retained excerpt supports the bound citation claim.",
        },
        critic_model=config.paper_revision.critic_model,
    )
    assert support["valid"] is True
    stage24 = run_dir / "stage-24"
    stage24.mkdir()
    support_text = canonical_json_text(support)
    (stage24 / "citation_support.json").write_text(support_text, encoding="utf-8")
    claims = {
        "schema_version": 2,
        "paper_path": "stage-23/paper_final_verified.md",
        "extraction_method": "static_e9_fixture",
        "claims": [
            {
                "id": row["claim_id"],
                "text": row["claim_text"],
                "type": "citation",
                "values": [],
                "cited_keys": [row["cite_key"]],
                "evidence": [],
                "status": "supported",
            }
            for row in support["instances"]
        ],
        "counts": {
            "total": len(support["instances"]),
            "unsupported": 0,
            "by_type": {
                "quantitative": 0,
                "comparative": 0,
                "result": 0,
                "citation": len(support["instances"]),
            },
        },
        "generated": "2026-01-01T00:00:00+00:00",
    }
    citations = {
        "schema_version": 2,
        "paper_path": "stage-23/paper_final_verified.md",
        "existence_report": "stage-23/verification_report.json",
        "support_report": "stage-24/citation_support.json",
        "instances": [
            {
                "instance_id": row["instance_id"],
                "cite_key": row["cite_key"],
                "role": "claim_support",
                "supported_claim_id": row["claim_id"],
                "support_excerpt": row["claim_text"],
                "context": row["claim_text"][:400],
            }
            for row in support["instances"]
        ],
        "counts": {
            "total": len(support["instances"]),
            "claim_support": len(support["instances"]),
            "background": 0,
            "unmapped": 0,
        },
        "generated": "2026-01-01T00:00:00+00:00",
    }
    truth = {
        "schema_version": 2,
        "paper_path": "stage-23/paper_final_verified.md",
        "paper_sha256": sha256_text(paper_text),
        "citation_support_path": "stage-24/citation_support.json",
        "citation_support_sha256": sha256_text(support_text),
        "citation_support_valid": True,
        "dataset_origin": support["dataset_origin"],
        "dataset_claim_violations": support["dataset_claim_violations"],
        "generated": "2026-01-01T00:00:00+00:00",
    }
    (stage24 / "claims.json").write_text(
        canonical_json_text(claims), encoding="utf-8"
    )
    (stage24 / "citations.json").write_text(
        canonical_json_text(citations), encoding="utf-8"
    )
    (stage24 / "critique_resolution.json").write_text(
        canonical_json_text(
            {
                "schema_version": 2,
                "critique_path": None,
                "resolutions": [],
                "generated": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (stage24 / "truth_audit.json").write_text(
        canonical_json_text(truth), encoding="utf-8"
    )
    return config, planned_keys


def test_card_batch_requires_exact_identity_closure() -> None:
    response = _card_response(
        [
            {
                "source_identity": "doi:one",
                "abstract": "Substantive retained abstract evidence.",
            }
        ]
    )
    with pytest.raises(EvidenceCardContractError, match="closure mismatch"):
        parse_card_batch_response(
            response,
            expected_batch_id="card-batch-001",
            expected_source_ids=("doi:one", "doi:two"),
        )


def test_card_batch_rejects_duplicate_json_keys() -> None:
    response = (
        '{"schema_version":1,"schema_version":1,'
        '"batch_id":"card-batch-001","cards":[]}'
    )
    with pytest.raises(EvidenceCardContractError, match="duplicate JSON key"):
        parse_card_batch_response(
            response,
            expected_batch_id="card-batch-001",
            expected_source_ids=(),
        )


@pytest.mark.parametrize(
    ("excerpt", "accepted"),
    [("x" * 24, False), ("x" * 25, True)],
)
def test_card_batch_enforces_minimum_excerpt_length(
    excerpt: str, accepted: bool
) -> None:
    candidate = {"source_identity": "doi:one", "abstract": excerpt}
    response = _card_response([candidate])
    if accepted:
        proposals = parse_card_batch_response(
            response,
            expected_batch_id="card-batch-001",
            expected_source_ids=("doi:one",),
        )
        assert proposals[0].excerpt_texts == (excerpt,)
    else:
        with pytest.raises(EvidenceCardContractError, match="at least 25"):
            parse_card_batch_response(
                response,
                expected_batch_id="card-batch-001",
                expected_source_ids=("doi:one",),
            )


def test_evidence_span_is_exact_for_unicode_text() -> None:
    candidate = _candidate(
        1, abstract="Alpha\u2028beta U0001f680 gamma provides retained evidence."
    )
    candidate.update(
        {"source_identity": "doi:10.1000/001", "cite_key": "smith2024alpha"}
    )
    proposal = parse_card_batch_response(
        _card_response([candidate]),
        expected_batch_id="card-batch-001",
        expected_source_ids=(candidate["source_identity"],),
    )[0]
    card = build_evidence_card(
        card_id="card-001",
        candidate=candidate,
        candidates_sha256="a" * 64,
        proposal=proposal,
    )
    excerpt = card["evidence_excerpts"][0]
    assert candidate["abstract"][excerpt["char_start"]:excerpt["char_end"]] == excerpt[
        "excerpt_text"
    ]
    parse_evidence_card(
        canonical_json_text(card),
        candidate=candidate,
        candidates_sha256="a" * 64,
    )


def test_evidence_card_replay_rejects_shortened_excerpt() -> None:
    candidate = _candidate(1, abstract="x" * 25)
    candidate.update(
        {"source_identity": "doi:10.1000/001", "cite_key": "smith2024alpha"}
    )
    proposal = parse_card_batch_response(
        _card_response([candidate]),
        expected_batch_id="card-batch-001",
        expected_source_ids=(candidate["source_identity"],),
    )[0]
    card = build_evidence_card(
        card_id="card-001",
        candidate=candidate,
        candidates_sha256="a" * 64,
        proposal=proposal,
    )
    card["evidence_excerpts"][0]["excerpt_text"] = "x" * 24
    card["evidence_excerpts"][0]["char_end"] = 24
    with pytest.raises(EvidenceCardContractError, match="shorter than 25"):
        parse_evidence_card(
            canonical_json_text(card),
            candidate=candidate,
            candidates_sha256="a" * 64,
        )


def test_fabricated_excerpt_produces_no_evidence() -> None:
    candidate = _candidate(1)
    candidate.update(
        {"source_identity": "doi:10.1000/001", "cite_key": "smith2024hardware"}
    )
    response = json.loads(_card_response([candidate]))
    response["cards"][0]["evidence_excerpt_texts"] = [
        "Fabricated evidence that is not retained."
    ]
    proposal = parse_card_batch_response(
        json.dumps(response),
        expected_batch_id="card-batch-001",
        expected_source_ids=(candidate["source_identity"],),
    )[0]
    card = build_evidence_card(
        card_id="card-001",
        candidate=candidate,
        candidates_sha256="a" * 64,
        proposal=proposal,
    )
    assert card["extraction_status"] == "fallback"
    assert card["evidence_excerpts"] == []


def test_stage6_writes_json_authority_and_deterministic_markdown(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1), _candidate(2)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM(_card_responses(shortlist)),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    assert result.artifacts == (
        "cards/",
        "cards_manifest.json",
        "citation_allowlist.json",
    )
    manifest = json.loads((stage6 / "cards_manifest.json").read_text())
    assert [entry["source_identity"] for entry in manifest["cards"]] == [
        row["source_identity"] for row in shortlist
    ]
    first_json = (stage6 / "cards" / "card-001.json").read_text()
    first_card = parse_evidence_card(
        first_json,
        candidate=shortlist[0],
        candidates_sha256=sha256_text(
            (run_dir / "stage-04" / "candidates.jsonl").read_text()
        ),
    )
    assert (stage6 / "cards" / "card-001.md").read_text() == render_evidence_card_markdown(
        first_card
    )
    assert "Template" not in first_json


def test_stage6_zero_evidence_fails_without_canonical_cards(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _prepare_stage5(run_dir, [_candidate(1)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    (stage6 / "cards").mkdir()
    (stage6 / "cards" / "stale.md").write_text("stale")
    (stage6 / "cards_manifest.json").write_text("stale")
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=None,
    )
    assert result.status is StageStatus.FAILED
    assert not (stage6 / "cards").exists()
    assert not (stage6 / "cards_manifest.json").exists()
    diagnostic = json.loads((stage6 / "card_extraction_failures.json").read_text())
    assert diagnostic["reason"] == "zero_eligible_evidence_cards"
    assert diagnostic["cards"][0]["evidence_excerpts"] == []


def test_stage6_truncated_initial_uses_one_larger_bounded_repair(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM(
        [
            LLMResponse(
                content='{"schema_version":1,"cards":[{"source_identity":"',
                model="fixture-model",
                prompt_tokens=300,
                completion_tokens=4096,
                total_tokens=4396,
                finish_reason="length",
                truncated=True,
            ),
            LLMResponse(
                content=_card_response(shortlist),
                model="fixture-model",
                prompt_tokens=320,
                completion_tokens=500,
                total_tokens=820,
                finish_reason="stop",
            ),
        ]
    )

    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.DONE
    assert [call["max_tokens"] for call in llm.call_kwargs] == [4096, 8192]
    assert len(llm.calls) == 2


def test_stage6_empty_repair_is_classified_without_raw_response(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    identity = shortlist[0]["source_identity"]
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="not json",
                model="fixture-model",
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                finish_reason="stop",
            ),
            LLMResponse(
                content="",
                model="fixture-model",
                prompt_tokens=120,
                completion_tokens=4096,
                total_tokens=4216,
                finish_reason="length",
                truncated=True,
            ),
        ]
    )

    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    diagnostic = json.loads((stage6 / "card_extraction_failures.json").read_text())
    assert diagnostic["llm_call_count"] == 2
    assert diagnostic["llm_call_limit"] == 2
    assert diagnostic["extraction_attempts"] == [
        {
            "attempt": "initial",
            "batch_id": "card-batch-001",
            "completion_tokens": 2,
            "content_length": 8,
            "error_category": "malformed_response",
            "finish_reason": "stop",
            "max_tokens": 4096,
            "prompt_tokens": 100,
            "source_identity": identity,
            "total_tokens": 102,
            "truncated": False,
        },
        {
            "attempt": "repair",
            "batch_id": "card-batch-001",
            "completion_tokens": 4096,
            "content_length": 0,
            "error_category": "empty_response",
            "finish_reason": "length",
            "max_tokens": 8192,
            "prompt_tokens": 120,
            "source_identity": identity,
            "total_tokens": 4216,
            "truncated": True,
        },
    ]
    assert "not json" not in json.dumps(diagnostic)


def test_stage6_second_truncated_response_fails_without_third_call(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    _prepare_stage5(run_dir, [_candidate(1)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="{\"schema_version\":",
                model="fixture-model",
                finish_reason="length",
                truncated=True,
            ),
            LLMResponse(
                content="{\"schema_version\":1,\"batch_id\":",
                model="fixture-model",
                finish_reason="length",
                truncated=True,
            ),
        ]
    )

    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert len(llm.calls) == 2
    assert not (stage6 / "cards").exists()
    assert not (stage6 / "cards_manifest.json").exists()
    diagnostic = json.loads((stage6 / "card_extraction_failures.json").read_text())
    assert [
        attempt["error_category"] for attempt in diagnostic["extraction_attempts"]
    ] == ["truncated_response", "truncated_response"]


def test_stage6_enforces_two_calls_per_shortlist_identity(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1), _candidate(2)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM(["{}", "", "{}", ""])

    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    diagnostic = json.loads((stage6 / "card_extraction_failures.json").read_text())
    assert len(llm.calls) == 4
    assert diagnostic["llm_call_count"] == 4
    assert diagnostic["llm_call_limit"] == 4
    assert {
        attempt["source_identity"] for attempt in diagnostic["extraction_attempts"]
    } == {row["source_identity"] for row in shortlist}


def test_stage6_mixed_success_keeps_failed_card_non_evidentiary(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1), _candidate(2)])
    responses = _card_responses(shortlist)
    response = json.loads(responses[1])
    response["cards"][0]["evidence_excerpt_texts"] = [
        "This substantive sentence is not in the abstract."
    ]
    responses[1] = json.dumps(response)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM(responses),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    second = json.loads((stage6 / "cards" / "card-002.json").read_text())
    assert second["extraction_status"] == "fallback"
    assert second["evidence_excerpts"] == []


def test_stage6_rejects_tampered_screening_report_before_llm(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    report_path = run_dir / "stage-05" / "screening_report.json"
    report = json.loads(report_path.read_text())
    report["selected_candidate_ids"] = []
    report_path.write_text(canonical_json_text(report), encoding="utf-8")
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM([_card_response(shortlist)])
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert "screening report" in (result.error or "")
    assert llm.calls == []
    assert not (stage6 / "cards").exists()


def test_stage6_replay_rejects_overlong_screening_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    shortlist[0]["keep_reason"] = "界" * 161
    shortlist_text = (
        json.dumps(shortlist[0], ensure_ascii=False, sort_keys=True) + "\n"
    )
    shortlist_path = run_dir / "stage-05" / "shortlist.jsonl"
    shortlist_path.write_text(shortlist_text, encoding="utf-8")
    report_path = run_dir / "stage-05" / "screening_report.json"
    report = json.loads(report_path.read_text())
    report["screening_output_sha256"] = sha256_text(shortlist_text)
    report_path.write_text(canonical_json_text(report), encoding="utf-8")

    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM([_card_response(shortlist)])
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )

    assert result.status is StageStatus.FAILED
    assert "keep_reason exceeds 160 Unicode code points" in (result.error or "")
    assert llm.calls == []


def test_cards_manifest_rejects_reordered_shortlist_identity(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1), _candidate(2)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM(_card_responses(shortlist)),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    manifest = json.loads((stage6 / "cards_manifest.json").read_text())
    manifest["cards"].reverse()
    with pytest.raises(EvidenceCardContractError, match="shortlist-order"):
        validate_cards_artifacts(
            stage_dir=stage6,
            manifest=manifest,
            shortlist_text=(run_dir / "stage-05" / "shortlist.jsonl").read_text(),
            screening_report_text=(
                run_dir / "stage-05" / "screening_report.json"
            ).read_text(),
            candidates_sha256=sha256_text(
                (run_dir / "stage-04" / "candidates.jsonl").read_text()
            ),
            shortlist=shortlist,
        )


def test_cards_artifacts_default_deny_extra_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    (stage6 / "cards" / "unmanifested.json").write_text("{}")
    manifest = json.loads((stage6 / "cards_manifest.json").read_text())
    with pytest.raises(EvidenceCardContractError, match="manifest closure"):
        validate_cards_artifacts(
            stage_dir=stage6,
            manifest=manifest,
            shortlist_text=(run_dir / "stage-05" / "shortlist.jsonl").read_text(),
            screening_report_text=(
                run_dir / "stage-05" / "screening_report.json"
            ).read_text(),
            candidates_sha256=sha256_text(
                (run_dir / "stage-04" / "candidates.jsonl").read_text()
            ),
            shortlist=shortlist,
        )


def test_stage7_replays_manifest_before_consuming_markdown(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1)])
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    (stage6 / "cards" / "card-001.md").write_text("tampered\n")
    stage7 = run_dir / "stage-07"
    stage7.mkdir()
    (stage7 / "synthesis.md").write_text("stale\n")

    synthesis = _execute_synthesis(
        stage7,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=None,
    )

    assert synthesis.status is StageStatus.FAILED
    assert "Markdown hash mismatch" in (synthesis.error or "")
    assert not (stage7 / "synthesis.md").exists()


def test_stage6_allowlist_is_recomputed_from_success_cards(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shortlist = _prepare_stage5(run_dir, [_candidate(1), _candidate(2)])
    responses = _card_responses(shortlist)
    response = json.loads(responses[1])
    response["cards"][0]["evidence_excerpt_texts"] = [
        "This substantive sentence is absent from the retained abstract."
    ]
    responses[1] = json.dumps(response)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        _config(),  # type: ignore[arg-type]
        AdapterBundle(),
        llm=_SequenceLLM(responses),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    allowlist_text = (stage6 / "citation_allowlist.json").read_text()
    allowlist = json.loads(allowlist_text)
    assert allowlist["eligible_keys"] == [shortlist[0]["cite_key"]]
    assert allowlist["ineligible"] == [
        {"cite_key": shortlist[1]["cite_key"], "reason_code": "card_fallback"}
    ]

    allowlist["eligible_keys"].append(shortlist[1]["cite_key"])
    allowlist["ineligible"] = []
    with pytest.raises(CitationPolicyContractError, match="replay mismatch"):
        validate_citation_allowlist(
            run_dir,
            _config(),  # type: ignore[arg-type]
            canonical_json_text(allowlist),
        )


def test_stage16_effective_policy_binds_run_local_config(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    outline = _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    )
    assert outline.status is StageStatus.DONE
    policy = load_effective_citation_policy(run_dir, config)
    assert policy["eligible_count"] == 1
    assert policy["effective_min_unique_sources"] == 1
    assert policy["effective_target_unique_sources"] == 1
    assert policy["config_source_path"] == "config.yaml"
    final_plan = load_final_citation_plan(run_dir, config)
    assert final_plan["plan_status"] == "final"
    assert [
        claim["planned_citations"][0]["cite_key"]
        for claim in final_plan["claims"]
    ] == [shortlist[0]["cite_key"]]
    instruction = build_citation_writer_instruction(run_dir, config)
    assert shortlist[0]["cite_key"] in instruction
    assert shortlist[0]["abstract"] in instruction
    assert "AVAILABLE REFERENCES" not in instruction

    cards = load_validated_cards(run_dir, config)
    related_work_instruction = build_citation_writer_instruction_from_authority(
        final_plan,
        cards,
        section_names=("Related Work",),
    )
    method_instruction = build_citation_writer_instruction_from_authority(
        final_plan,
        cards,
        section_names=("Method", "Experiments"),
    )
    assert shortlist[0]["cite_key"] in related_work_instruction
    assert shortlist[0]["abstract"] in related_work_instruction
    assert "Cite every required key above" in related_work_instruction
    assert shortlist[0]["cite_key"] not in method_instruction
    assert "Cite every required key above" not in method_instruction
    assert "Do not use any citation marker in this part." in method_instruction

    history_path = run_dir / "config_snapshot_history.jsonl"
    history_text = history_path.read_text(encoding="utf-8")
    history_path.write_text(history_text + "{}\n", encoding="utf-8")
    with pytest.raises(CitationPolicyContractError, match="history hash mismatch"):
        load_effective_citation_policy(run_dir, config)
    history_path.write_text(history_text, encoding="utf-8")

    (run_dir / "config.yaml").write_text("tampered: true\n", encoding="utf-8")
    with pytest.raises(CitationPolicyContractError, match="hash mismatch"):
        load_effective_citation_policy(run_dir, config)


def test_stage16_plan_selects_first_complete_standalone_excerpt() -> None:
    card = {
        "cite_key": "smith2024deep",
        "extraction_status": "success",
        "evidence_excerpts": [
            {
                "excerpt_id": "ev-multi",
                "excerpt_text": "First bounded sentence. Second bounded sentence.",
            },
            {
                "excerpt_id": "ev-single",
                "excerpt_text": "Single bounded sentence.",
            },
        ],
    }

    plan = build_citation_plan_from_replayed_inputs(
        config=_config(),  # type: ignore[arg-type]
        plan_status="final",
        allowlist={"eligible_keys": ["smith2024deep"]},
        allowlist_text="allowlist",
        cards_manifest_text="manifest",
        effective_policy={
            "effective_target_unique_sources": 1,
            "effective_min_unique_sources": 1,
        },
        effective_policy_text="policy",
        cards=(card,),
    )

    assert plan["claims"][0]["claim_text"] == "Single bounded sentence."
    assert plan["claims"][0]["planned_citations"][0]["evidence_excerpt_ids"] == [
        "ev-multi",
        "ev-single",
    ]


def test_stage16_plan_rejects_card_without_standalone_excerpt() -> None:
    card = {
        "cite_key": "smith2024deep",
        "extraction_status": "success",
        "evidence_excerpts": [
            {
                "excerpt_id": "ev-multi",
                "excerpt_text": "First bounded sentence. Second bounded sentence.",
            },
            {
                "excerpt_id": "ev-newline",
                "excerpt_text": "Line one.\nLine two.",
            },
        ],
    }

    with pytest.raises(CitationPlanContractError, match="standalone"):
        build_citation_plan_from_replayed_inputs(
            config=_config(),  # type: ignore[arg-type]
            plan_status="final",
            allowlist={"eligible_keys": ["smith2024deep"]},
            allowlist_text="allowlist",
            cards_manifest_text="manifest",
            effective_policy={
                "effective_target_unique_sources": 1,
                "effective_min_unique_sources": 1,
            },
            effective_policy_text="policy",
            cards=(card,),
        )


def _citation_usage_authority_fixture() -> dict[str, Any]:
    authority = {
        "schema_version": 1,
        "policy_version": 1,
        "canonical_fact_sheet_sha256": "a" * 64,
        "execution_policy_sha256": "b" * 64,
        "tokens": [
            {
                "usage_token": "dataset:synthetic",
                "usage_kind": "dataset",
                "section": "Experiments",
                "claim_type": "dataset_origin",
                "source_kind": "dataset",
                "source_identity": "synthetic",
                "evidence_terms": ["synthetic"],
            },
            {
                "usage_token": "method:graphsage",
                "usage_kind": "method",
                "section": "Method",
                "claim_type": "algorithm_definition",
                "source_kind": "condition",
                "source_identity": "trojnet_community_graphsage",
                "evidence_terms": ["GraphSAGE"],
            },
            {
                "usage_token": "benchmark:iscas85",
                "usage_kind": "benchmark",
                "section": "Experiments",
                "claim_type": "benchmark_definition",
                "source_kind": "benchmark",
                "source_identity": "iscas85",
                "evidence_terms": ["ISCAS-85", "ISCAS85"],
            },
            {
                "usage_token": "protocol:condition_seed_variant_mean_v1",
                "usage_kind": "evaluation_protocol",
                "section": "Experiments",
                "claim_type": "evaluation_protocol",
                "source_kind": "execution_policy",
                "source_identity": "condition_seed_variant_mean_v1",
                "evidence_terms": ["condition_seed_variant_mean_v1"],
            },
        ],
    }
    authority["tokens"].sort(key=lambda item: item["usage_token"])
    return authority


def _citation_plan_card(
    cite_key: str, excerpt: str, *, excerpt_id: str
) -> dict[str, Any]:
    return {
        "cite_key": cite_key,
        "extraction_status": "success",
        "evidence_excerpts": [
            {"excerpt_id": excerpt_id, "excerpt_text": excerpt}
        ],
    }


def _build_v3_plan(cards: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    keys = [card["cite_key"] for card in cards]
    return build_citation_plan_from_replayed_inputs(
        config=_config(),  # type: ignore[arg-type]
        plan_status="final",
        allowlist={"eligible_keys": keys},
        allowlist_text="allowlist",
        cards_manifest_text="manifest",
        effective_policy={
            "effective_target_unique_sources": len(keys),
            "effective_min_unique_sources": 1,
            "config_source_sha256": "c" * 64,
        },
        effective_policy_text="policy",
        cards=cards,
        citation_usage_authority=_citation_usage_authority_fixture(),
    )


def test_citation_plan_v3_allocates_exact_registered_usage_tokens() -> None:
    cards = (
        _citation_plan_card(
            "background2024work",
            "A bounded background statement.",
            excerpt_id="ev-background",
        ),
        _citation_plan_card(
            "graphsage2017inductive",
            "GraphSAGE is an inductive representation learning algorithm.",
            excerpt_id="ev-graphsage",
        ),
        _citation_plan_card(
            "iscas1985benchmark",
            "ISCAS-85 defines a benchmark family for circuit evaluation.",
            excerpt_id="ev-iscas85",
        ),
        _citation_plan_card(
            "synthetic2024dataset",
            "The synthetic dataset provides bounded evaluation inputs.",
            excerpt_id="ev-synthetic",
        ),
        _citation_plan_card(
            "protocol2024study",
            "The condition_seed_variant_mean_v1 protocol is deterministic.",
            excerpt_id="ev-protocol",
        ),
    )

    plan = _build_v3_plan(cards)

    assert plan["plan_version"] == 3
    by_key = {
        claim["planned_citations"][0]["cite_key"]: claim
        for claim in plan["claims"]
    }
    assert by_key["background2024work"]["section_path"] == ["Related Work"]
    assert by_key["background2024work"]["claim_type"] == "prior_work"
    assert by_key["background2024work"]["eligibility_binding"] is None
    assert by_key["graphsage2017inductive"]["section_path"] == ["Method"]
    assert by_key["graphsage2017inductive"]["claim_type"] == "algorithm_definition"
    assert by_key["graphsage2017inductive"]["eligibility_binding"][
        "usage_token"
    ] == "method:graphsage"
    assert by_key["iscas1985benchmark"]["section_path"] == ["Experiments"]
    assert by_key["iscas1985benchmark"]["claim_type"] == "benchmark_definition"
    assert by_key["synthetic2024dataset"]["claim_type"] == "dataset_origin"
    assert by_key["synthetic2024dataset"]["eligibility_binding"][
        "usage_token"
    ] == "dataset:synthetic"
    assert by_key["protocol2024study"]["claim_type"] == "evaluation_protocol"


def test_citation_plan_v3_does_not_infer_unregistered_method() -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "louvain2008communities",
                "Louvain community optimization identifies graph structure.",
                excerpt_id="ev-louvain",
            ),
        )
    )

    claim = plan["claims"][0]
    assert claim["section_path"] == ["Related Work"]
    assert claim["claim_type"] == "prior_work"
    assert claim["eligibility_binding"] is None


def test_citation_plan_v3_selects_first_exact_eligible_excerpt() -> None:
    card = {
        "cite_key": "graphsage2017inductive",
        "extraction_status": "success",
        "evidence_excerpts": [
            {
                "excerpt_id": "ev-background",
                "excerpt_text": "A bounded background statement.",
            },
            {
                "excerpt_id": "ev-graphsage",
                "excerpt_text": (
                    "GraphSAGE is an inductive representation learning algorithm."
                ),
            },
        ],
    }

    plan = _build_v3_plan((card,))

    claim = plan["claims"][0]
    assert claim["section_path"] == ["Method"]
    assert claim["claim_text"] == card["evidence_excerpts"][1]["excerpt_text"]
    assert claim["planned_citations"][0]["evidence_excerpt_ids"] == [
        "ev-graphsage"
    ]
    assert claim["eligibility_binding"]["evidence_excerpt_id"] == "ev-graphsage"


@pytest.mark.parametrize(
    ("section", "claim_type"),
    [
        ("Introduction", "background"),
        ("Related Work", "prior_work"),
        ("Method", "method_origin"),
        ("Method", "algorithm_definition"),
        ("Experiments", "dataset_origin"),
        ("Experiments", "benchmark_definition"),
        ("Experiments", "evaluation_protocol"),
    ],
)
def test_citation_plan_v3_closed_compatibility_matrix_accepts(
    section: str, claim_type: str
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "graphsage2017inductive",
                "GraphSAGE is an inductive representation learning algorithm.",
                excerpt_id="ev-graphsage",
            ),
        )
    )
    claim = plan["claims"][0]
    claim["section_path"] = [section]
    claim["claim_type"] = claim_type
    if section in {"Introduction", "Related Work"}:
        claim["eligibility_binding"] = None
    else:
        binding = claim["eligibility_binding"]
        usage_kind = {
            "method_origin": "method",
            "algorithm_definition": "method",
            "dataset_origin": "dataset",
            "benchmark_definition": "benchmark",
            "evaluation_protocol": "evaluation_protocol",
        }[claim_type]
        binding["usage_kind"] = usage_kind
        binding["usage_token"] = f"{usage_kind}:fixture"

    parsed = parse_citation_plan(canonical_json_text(plan))

    assert parsed["claims"][0]["section_path"] == [section]
    assert parsed["claims"][0]["claim_type"] == claim_type


@pytest.mark.parametrize(
    ("section", "claim_type"),
    [
        ("Results", "background"),
        ("Discussion", "prior_work"),
        ("Limitations", "benchmark_definition"),
        ("Conclusion", "method_origin"),
        ("Method", "background"),
        ("Experiments", "algorithm_definition"),
    ],
)
def test_citation_plan_v3_closed_compatibility_matrix_rejects(
    section: str, claim_type: str
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "graphsage2017inductive",
                "GraphSAGE is an inductive representation learning algorithm.",
                excerpt_id="ev-graphsage",
            ),
        )
    )
    plan["claims"][0]["section_path"] = [section]
    plan["claims"][0]["claim_type"] = claim_type

    with pytest.raises(CitationPlanContractError, match="compatibility"):
        parse_citation_plan(canonical_json_text(plan))


@pytest.mark.parametrize(
    "claim_text",
    [
        "GraphSAGE achieved AUPRC 0.999 in the current run.",
        "ISCAS-85 produced 162 observations in the current run.",
    ],
)
def test_citation_plan_v3_does_not_authorize_current_run_results(
    claim_text: str,
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "result2024claim",
                claim_text,
                excerpt_id="ev-result",
            ),
        )
    )

    assert plan["claims"][0]["section_path"] == ["Related Work"]
    assert plan["claims"][0]["eligibility_binding"] is None


@pytest.mark.parametrize(
    "claim_text",
    [
        "Our evaluation found that GraphSAGE outperformed the baseline.",
        "In the current run, GraphSAGE was the best-performing method.",
        "Our experiment used three seeds with GraphSAGE.",
        "GraphSAGE is the best performing method.",
        "GraphSAGE is the current run best method.",
        "GraphSAGE was introduced as the top performing method.",
    ],
)
def test_citation_plan_v3_only_authorizes_positive_method_definition_grammar(
    claim_text: str,
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "graphsage2017inductive",
                claim_text,
                excerpt_id="ev-graphsage",
            ),
        )
    )

    assert plan["claims"][0]["section_path"] == ["Related Work"]
    assert plan["claims"][0]["eligibility_binding"] is None


def test_citation_plan_v3_rejects_ambiguous_multi_usage_excerpt() -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "graphsage2017inductive",
                "GraphSAGE is evaluated on the ISCAS-85 benchmark.",
                excerpt_id="ev-ambiguous",
            ),
        )
    )

    assert plan["claims"][0]["section_path"] == ["Related Work"]
    assert plan["claims"][0]["eligibility_binding"] is None


@pytest.mark.parametrize(
    "claim_text",
    [
        "The synthetic dataset contains 162 observations in the current run.",
        "ISCAS-85 contains 162 observations in the current run.",
        (
            "The condition_seed_variant_mean_v1 protocol uses three seeds "
            "in the current run."
        ),
    ],
)
def test_citation_plan_v3_experiment_grammar_rejects_current_run_counts(
    claim_text: str,
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "experiment2024source",
                claim_text,
                excerpt_id="ev-experiment",
            ),
        )
    )

    assert plan["claims"][0]["section_path"] == ["Related Work"]
    assert plan["claims"][0]["eligibility_binding"] is None


def test_citation_plan_v3_rejects_noncanonical_usage_token_order() -> None:
    authority = _citation_usage_authority_fixture()
    authority["tokens"] = list(reversed(authority["tokens"]))

    with pytest.raises(CitationPlanContractError, match="canonical order"):
        build_citation_plan_from_replayed_inputs(
            config=_config(),  # type: ignore[arg-type]
            plan_status="final",
            allowlist={"eligible_keys": ["graphsage2017inductive"]},
            allowlist_text="allowlist",
            cards_manifest_text="manifest",
            effective_policy={
                "effective_target_unique_sources": 1,
                "effective_min_unique_sources": 1,
                "config_source_sha256": "c" * 64,
            },
            effective_policy_text="policy",
            cards=(
                _citation_plan_card(
                    "graphsage2017inductive",
                    "GraphSAGE is an inductive representation learning algorithm.",
                    excerpt_id="ev-graphsage",
                ),
            ),
            citation_usage_authority=authority,
        )


def test_citation_plan_v2_rejects_v3_eligibility_field() -> None:
    plan = build_citation_plan_from_replayed_inputs(
        config=_config(),  # type: ignore[arg-type]
        plan_status="final",
        allowlist={"eligible_keys": ["background2024work"]},
        allowlist_text="allowlist",
        cards_manifest_text="manifest",
        effective_policy={
            "effective_target_unique_sources": 1,
            "effective_min_unique_sources": 1,
        },
        effective_policy_text="policy",
        cards=(
            _citation_plan_card(
                "background2024work",
                "A bounded background statement.",
                excerpt_id="ev-background",
            ),
        ),
    )
    plan["claims"][0]["eligibility_binding"] = None

    with pytest.raises(CitationPlanContractError, match="fields mismatch"):
        parse_citation_plan(canonical_json_text(plan))


@pytest.mark.parametrize("plan_version", [2, 3])
def test_citation_plan_rejects_bool_schema_version(plan_version: int) -> None:
    if plan_version == 3:
        plan = _build_v3_plan(
            (
                _citation_plan_card(
                    "graphsage2017inductive",
                    "GraphSAGE is an inductive representation learning algorithm.",
                    excerpt_id="ev-graphsage",
                ),
            )
        )
    else:
        plan = build_citation_plan_from_replayed_inputs(
            config=_config(),  # type: ignore[arg-type]
            plan_status="final",
            allowlist={"eligible_keys": ["background2024work"]},
            allowlist_text="allowlist",
            cards_manifest_text="manifest",
            effective_policy={
                "effective_target_unique_sources": 1,
                "effective_min_unique_sources": 1,
            },
            effective_policy_text="policy",
            cards=(
                _citation_plan_card(
                    "background2024work",
                    "A bounded background statement.",
                    excerpt_id="ev-background",
                ),
            ),
        )
    plan["schema_version"] = True

    with pytest.raises(CitationPlanContractError, match="schema"):
        parse_citation_plan(canonical_json_text(plan))


@pytest.mark.parametrize(
    "mutation",
    ["missing", "bool_policy", "hash", "excerpt", "usage_kind"],
)
def test_citation_plan_v3_rejects_invalid_eligibility_binding(
    mutation: str,
) -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "graphsage2017inductive",
                "GraphSAGE is an inductive representation learning algorithm.",
                excerpt_id="ev-graphsage",
            ),
        )
    )
    binding = plan["claims"][0]["eligibility_binding"]
    if mutation == "missing":
        plan["claims"][0]["eligibility_binding"] = None
    elif mutation == "bool_policy":
        binding["policy_version"] = True
    elif mutation == "hash":
        binding["canonical_fact_sheet_sha256"] = "0" * 63
    elif mutation == "excerpt":
        binding["evidence_excerpt_id"] = "ev-foreign"
    else:
        binding["usage_kind"] = "benchmark"

    with pytest.raises(CitationPlanContractError):
        parse_citation_plan(canonical_json_text(plan))


def test_citation_plan_v3_rejects_binding_on_background_claim() -> None:
    plan = _build_v3_plan(
        (
            _citation_plan_card(
                "background2024work",
                "A bounded background statement.",
                excerpt_id="ev-background",
            ),
        )
    )
    plan["claims"][0]["eligibility_binding"] = {
        "policy_version": 1,
        "usage_token": "method:graphsage",
        "usage_kind": "method",
        "source_kind": "condition",
        "source_identity": "trojnet_community_graphsage",
        "canonical_fact_sheet_sha256": "a" * 64,
        "execution_policy_sha256": "b" * 64,
        "config_source_sha256": "c" * 64,
        "evidence_excerpt_id": "ev-background",
    }

    with pytest.raises(CitationPlanContractError, match="background claim"):
        parse_citation_plan(canonical_json_text(plan))


def test_research_release_fails_below_citation_minimum(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir, claim_scope="research_release")
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    result = _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    outline = _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    )
    assert outline.status is StageStatus.FAILED
    assert "below required minimum 15" in (outline.error or "")
    assert not (stage16 / "citation_policy_effective.json").exists()


def test_citation_policy_loaders_reject_duplicate_keys_and_boolean_counts() -> None:
    with pytest.raises(CitationPolicyContractError, match="duplicate JSON key"):
        parse_citation_allowlist(
            '{"schema_version":1,"schema_version":1}'
        )
    payload = {
        "schema_version": 1,
        "policy_version": 1,
        "claim_scope": "pipeline_validation",
        "eligible_count": True,
        "effective_min_unique_sources": 1,
        "effective_target_unique_sources": 1,
        "citation_allowlist_path": "stage-06/citation_allowlist.json",
        "citation_allowlist_sha256": "a" * 64,
        "config_source_path": "config.yaml",
        "config_source_sha256": "b" * 64,
    }
    with pytest.raises(CitationPolicyContractError, match="nonnegative integer"):
        parse_effective_citation_policy(canonical_json_text(payload))


def test_citation_plan_loader_rejects_unknown_fields_and_tampering(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    plan_path = stage16 / "citation_plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["claims"][0]["planned_citations"][0]["cite_key"] = "fake2024key"
    plan_path.write_text(canonical_json_text(payload), encoding="utf-8")
    with pytest.raises(CitationPlanContractError, match="replay mismatch"):
        load_final_citation_plan(run_dir, config)
    payload["unexpected"] = True
    with pytest.raises(CitationPlanContractError, match="fields mismatch"):
        parse_citation_plan(canonical_json_text(payload))


def test_citation_closure_rejects_key_outside_assigned_section(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    key = shortlist[0]["cite_key"]
    paper = f"## Introduction\n\nBackground.\n\n## Results\n\nResult [{key}].\n"
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"detection_f1": 0.95}}), encoding="utf-8"
    )
    structure = canonical_json_text(
        {
            "schema_version": 1,
            "valid": True,
            "source_sha256": sha256_text(paper),
            "section_count": 2,
            "issues": [],
        }
    )
    experiment = canonical_experiment_fact_json_text(
        build_experiment_fact_closure_report(run_dir, paper_text=paper)
    )
    report = build_citation_closure_report(
        run_dir,
        config,
        paper_text=paper,
        structure_report_text=structure,
        experiment_fact_report_text=experiment,
    )
    assert report["misplaced_planned_keys"] == [key]
    assert report["valid"] is False


def test_citation_closure_rejects_key_on_unbound_sentence_in_assigned_heading(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    key = shortlist[0]["cite_key"]
    paper = f"## Related Work\n\nFabricated unsupported prose [{key}].\n"
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"detection_f1": 0.95}}), encoding="utf-8"
    )
    structure = canonical_json_text(
        {
            "schema_version": 1,
            "valid": True,
            "source_sha256": sha256_text(paper),
            "section_count": 1,
            "issues": [],
        }
    )
    experiment = canonical_experiment_fact_json_text(
        build_experiment_fact_closure_report(run_dir, paper_text=paper)
    )

    report = build_citation_closure_report(
        run_dir,
        config,
        paper_text=paper,
        structure_report_text=structure,
        experiment_fact_report_text=experiment,
    )

    assert report["misplaced_planned_keys"] == [key]
    assert report["citation_occurrences"] == []
    assert report["valid"] is False
    stage17 = run_dir / "stage-17"
    stage17.mkdir()
    (stage17 / "paper_draft.md").write_text(paper, encoding="utf-8")
    (stage17 / "paper_structure_report.json").write_text(
        structure, encoding="utf-8"
    )
    (stage17 / "experiment_fact_closure_report.json").write_text(
        experiment, encoding="utf-8"
    )
    (stage17 / "citation_closure_report.json").write_text(
        canonical_json_text(report), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="replay failed"):
        validate_citation_closure_report(run_dir, config)


@pytest.mark.parametrize("separator", ["  ", "\t", "\N{NO-BREAK SPACE}"])
def test_citation_closure_requires_exact_plan_claim_bytes(
    tmp_path: Path, separator: str
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6, run_dir, config, AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    plan = json.loads((stage16 / "citation_plan.json").read_text(encoding="utf-8"))
    key = plan["claims"][0]["planned_citations"][0]["cite_key"]
    plan["claims"][0]["claim_text"] = f"A{separator}bounded claim."
    plan_text = canonical_json_text(plan)
    parsed_plan = parse_citation_plan(plan_text)
    allowlist_text = (run_dir / "stage-06/citation_allowlist.json").read_text(
        encoding="utf-8"
    )
    allowlist = parse_citation_allowlist(allowlist_text)
    paper = f"## Related Work\n\nA bounded claim [{key}].\n"
    structure = canonical_json_text(
        {"schema_version": 1, "valid": True, "source_sha256": sha256_text(paper),
         "section_count": 1, "issues": []}
    )
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12/runs/results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(json.dumps({"metrics": {"detection_f1": 0.95}}))
    experiment = canonical_experiment_fact_json_text(
        build_experiment_fact_closure_report(run_dir, paper_text=paper)
    )

    report = build_citation_closure_from_texts(
        paper_text=paper,
        structure_report_text=structure,
        experiment_fact_report_text=experiment,
        citation_plan_text=plan_text,
        citation_allowlist_text=allowlist_text,
        plan=parsed_plan,
        allowlist=allowlist,
    )

    assert report["misplaced_planned_keys"] == [key]
    assert report["citation_occurrences"] == []
    assert report["valid"] is False


def test_experiment_fact_closure_binds_metrics_and_synthetic_origin(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    run_path = run_dir / "stage-12" / "runs" / "run-001.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        json.dumps({"metrics": {"detection_rate": 0.95}}), encoding="utf-8"
    )
    paper = (
        "## Introduction\n\nPrior work reported 0.42 in another setting.\n\n"
        "## Results\n\nThe measured detection rate was 95%.\n"
    )
    report = build_experiment_fact_closure_report(run_dir, paper_text=paper)
    assert report["valid"] is True
    assert report["manuscript_numeric_values"] == [Decimal("0.95")]
    assert report["unknown_numeric_values"] == []

    contradicted = paper.replace(
        "The measured", "Using real-hardware measurements, the measured"
    )
    report = build_experiment_fact_closure_report(run_dir, paper_text=contradicted)
    assert report["valid"] is False
    assert report["dataset_claim_violations"]

    named_public_benchmark = paper.replace(
        "The measured", "Using SPEC CPU2006 traces, the measured"
    )
    report = build_experiment_fact_closure_report(
        run_dir, paper_text=named_public_benchmark
    )
    assert "SPEC CPU2006" in report["dataset_claim_violations"]


def test_experiment_fact_closure_rejects_unknown_metric_and_duplicate_json() -> None:
    payload = {
        "schema_version": 2,
        "paper_path": "stage-17/paper_draft.md",
        "paper_sha256": "a" * 64,
        "experiment_contract_path": "stage-09/experiment_contract.yaml",
        "experiment_contract_sha256": "b" * 64,
        "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
        "canonical_experiment_evidence_sha256": "d" * 64,
        "dataset_origin": "synthetic",
        "metric_sources": [{"path": "stage-12/runs/run.json", "sha256": "c" * 64}],
        "grounded_numeric_values": [0.95],
        "manuscript_numeric_values": [0.97],
        "unknown_numeric_values": [0.97],
        "dataset_claim_violations": [],
        "valid": False,
    }
    assert parse_experiment_fact_closure_report(canonical_json_text(payload))["valid"] is False
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_experiment_fact_closure_report('{"schema_version":1,"schema_version":1}')


def test_experiment_fact_closure_detects_integer_and_non_results_metrics(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"fps": 90, "latency_cycles": 64, "f1": 0.5}}),
        encoding="utf-8",
    )
    paper = (
        "## Abstract\n\nThe system achieves 92 FPS and 0.7 F1.\n\n"
        "## Results\n\nLatency was 128 cycles.\n\n"
        "## Discussion\n\nThe gain remained 3x.\n"
    )
    report = build_experiment_fact_closure_report(run_dir, paper_text=paper)
    assert report["valid"] is False
    assert report["unknown_numeric_values"] == [
        Decimal("92"),
        Decimal("0.7"),
        Decimal("128"),
        Decimal("3"),
    ]


def test_experiment_fact_closure_percent_normalization_is_token_explicit(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(json.dumps({"metrics": {"rate": 0.5}}), encoding="utf-8")
    percent = build_experiment_fact_closure_report(
        run_dir, paper_text="## Results\n\nThe rate was 50%.\n"
    )
    assert percent["valid"] is True
    unmarked = build_experiment_fact_closure_report(
        run_dir, paper_text="## Results\n\nThe rate was 50.0.\n"
    )
    assert unmarked["unknown_numeric_values"] == [Decimal("50.0")]


def test_experiment_fact_closure_rejects_display_rounding_v1(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"f1": 0.639877, "std": 0.010216}}),
        encoding="utf-8",
    )
    rounded = build_experiment_fact_closure_report(
        run_dir,
        paper_text="## Results\n\nF1 was 0.64 with standard deviation 0.0102.\n",
    )
    assert rounded["unknown_numeric_values"] == [
        Decimal("0.64"),
        Decimal("0.0102"),
    ]
    exact = build_experiment_fact_closure_report(
        run_dir,
        paper_text="## Results\n\nF1 was 0.639877 with standard deviation 0.010216.\n",
    )
    assert exact["valid"] is True


def test_experiment_fact_repair_removes_only_unsupported_sentence() -> None:
    paper = (
        "## Results\n\n"
        "The exact F1 was 0.639877. "
        "A rounded claim reported 0.64. "
        "The following qualitative sentence remains.\n\n"
        "## Conclusion\n\nThe exact result was 0.639877.\n"
    )
    repaired, log = remove_unsupported_experiment_fact_blocks(
        paper,
        grounded_numeric_values=[Decimal("0.639877")],
        dataset_origin="synthetic",
    )
    assert "The exact F1 was 0.639877." in repaired
    assert "A rounded claim reported 0.64." not in repaired
    assert "The following qualitative sentence remains." in repaired
    assert "The exact result was 0.639877." in repaired
    assert log["operations"][0]["block_type"] == "sentence"
    assert log["operations"][0]["unknown_numeric_values"] == [Decimal("0.64")]


@pytest.mark.parametrize(
    ("body", "block_type"),
    (
        ("| Method | F1 |\n| --- | --- |\n| ours | 0.64 |\n", "table_row"),
        ("$$\nF_1 = 0.64\n$$\n", "display_math"),
        ("**Figure 1:** Rounded F1 is 0.64.\n", "caption"),
        ("- Rounded F1 is 0.64.\n  Continuation text.\n", "list_item"),
    ),
)
def test_experiment_fact_repair_removes_complete_structural_block(
    body: str, block_type: str
) -> None:
    paper = f"## Results\n\n{body}\nSupported F1 is 0.639877.\n"
    repaired, log = remove_unsupported_experiment_fact_blocks(
        paper,
        grounded_numeric_values=[Decimal("0.639877")],
        dataset_origin="synthetic",
    )
    assert "0.64" not in repaired
    assert "Supported F1 is 0.639877." in repaired
    assert log["operations"][0]["block_type"] == block_type


def test_experiment_fact_repair_does_not_exempt_cited_or_conclusion_numbers() -> None:
    paper = (
        "## Discussion\n\n"
        "Our detector achieved 0.97, unlike prior work at 0.90 [key2020].\n\n"
        "## Conclusion\n\nOur method achieved 0.97.\n"
    )
    repaired, log = remove_unsupported_experiment_fact_blocks(
        paper,
        grounded_numeric_values=[Decimal("0.5")],
        dataset_origin="synthetic",
    )
    assert "0.97" not in repaired
    assert "0.90" not in repaired
    assert len(log["operations"]) == 2


@pytest.mark.parametrize("abbreviation", ("i.e.", "e.g.", "Fig.", "et al."))
def test_experiment_fact_repair_does_not_split_at_abbreviation(
    abbreviation: str,
) -> None:
    sentence = f"The method ({abbreviation} Z-score) achieved 0.64 F1. "
    retained = "The exact value was 0.5.\n"
    paper = f"## Results\n\n{sentence}{retained}"
    repaired, log = remove_unsupported_experiment_fact_blocks(
        paper,
        grounded_numeric_values=[Decimal("0.5")],
        dataset_origin="synthetic",
    )
    assert sentence not in repaired
    assert retained in repaired
    assert f"The method ({abbreviation}" not in repaired
    assert log["operations"][0]["block_type"] == "sentence"


def test_experiment_fact_repair_does_not_bridge_separate_math_blocks() -> None:
    paper = (
        "## Results\n\n"
        "$$\nF_1 = 0.5\n$$\n\n"
        "The unsupported prose value was 0.64.\n\n"
        "$$\nR = 0.5\n$$\n"
    )
    repaired, log = remove_unsupported_experiment_fact_blocks(
        paper,
        grounded_numeric_values=[Decimal("0.5")],
        dataset_origin="synthetic",
    )
    assert "unsupported prose" not in repaired
    assert repaired.count("$$") == 4
    assert "F_1 = 0.5" in repaired
    assert "R = 0.5" in repaired
    assert log["operations"][0]["block_type"] == "sentence"


@pytest.mark.parametrize(
    "body",
    ("$$\nF_1 = 0.64\n", "\\[\nF_1 = 0.64\n", "\\begin{equation}\nF_1 = 0.64\n"),
)
def test_experiment_fact_repair_rejects_unbalanced_math(body: str) -> None:
    with pytest.raises(ValueError, match="unbalanced"):
        remove_unsupported_experiment_fact_blocks(
            f"## Results\n\n{body}",
            grounded_numeric_values=[Decimal("0.5")],
            dataset_origin="synthetic",
        )


def test_experiment_fact_closure_ignores_shadow_metric_stage_and_flags_hardware_claim(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    direct = run_dir / "stage-12" / "runs" / "results.json"
    direct.parent.mkdir(parents=True)
    direct.write_text(json.dumps({"metrics": {"rate": 0.5}}), encoding="utf-8")
    shadow = run_dir / "stage-12b" / "runs" / "fake.json"
    shadow.parent.mkdir(parents=True)
    shadow.write_text(json.dumps({"metrics": {"rate": 0.97}}), encoding="utf-8")
    paper = (
        "## Abstract\n\nWe captured on our FPGA prototype board.\n\n"
        "## Results\n\nThe rate was 0.97.\n"
    )
    report = build_experiment_fact_closure_report(run_dir, paper_text=paper)
    assert report["unknown_numeric_values"] == [Decimal("0.97")]
    assert report["dataset_claim_violations"]


def test_stage17_uses_final_plan_only_and_writes_replayable_closure(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    run_path = run_dir / "stage-12" / "runs" / "results.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        json.dumps(
            {
                "claim_scope": "pipeline_validation",
                "dataset_origin": "synthetic",
                "evaluator_owner": "scaffold",
                "metrics": {"detection_f1": 0.95},
            }
        ),
        encoding="utf-8",
    )
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    plan = load_final_citation_plan(run_dir, config)
    assert {tuple(claim["section_path"]) for claim in plan["claims"]} == {
        ("Related Work",)
    }
    assert "section: Related Work" in build_citation_writer_instruction(
        run_dir, config
    )
    key = shortlist[0]["cite_key"]
    claim_text = plan["claims"][0]["claim_text"]
    llm = _SequenceLLM(
        [
            (
                "## Title\n\nBounded Study\n\n## Abstract\n\nAbstract.\n\n"
                "## Introduction\n\nIntroduction."
            ),
            f"## Related Work\n\n### Theme\n\n{claim_text}",
            "## Method\n\nMethod.\n\n## Experiments\n\nExperiment setup.",
            "## Results\n\nDetection F1 was 95%.\n\n"
            "## Discussion\n\nDiscussion.\n\n"
            "## Limitations\n\nLimitations.\n\n"
            "## Conclusion\n\nConclusion.",
        ]
    )
    stage17 = run_dir / "stage-17"
    stage17.mkdir()
    result = _execute_paper_draft(
        stage17,
        run_dir,
        config,
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE, result.error
    assert (stage17 / "citation_heading_repair_log.json").is_file()
    prompts = "\n".join(llm.calls)
    assert "FINAL CITATION PLAN" in prompts
    assert "AVAILABLE REFERENCES" not in prompts
    assert "Hardware Detection Study" not in prompts
    assert not (stage17 / "references_preverified.bib").exists()
    closure = json.loads((stage17 / "citation_closure_report.json").read_text())
    assert closure["valid"] is True
    assert closure["experiment_fact_closure_valid"] is True
    assert len(closure["citation_occurrences"]) == 1
    occurrence = closure["citation_occurrences"][0]
    assert occurrence["claim_id"] == plan["claims"][0]["claim_id"]
    assert occurrence["cite_key"] == key
    assert occurrence["sentence_text"] == claim_text
    assert occurrence["claim_text_sha256"] == sha256_text(claim_text)
    missing_occurrence = json.loads(json.dumps(closure))
    missing_occurrence["citation_occurrences"] = []
    with pytest.raises(ValueError, match="occurrence"):
        parse_citation_closure_report(canonical_json_text(missing_occurrence))
    original_closure = json.loads(json.dumps(closure))
    for field, value in (
        ("claim_id", "planned-claim-999"),
        ("claim_text_sha256", "f" * 64),
        ("citation_plan_sha256", "f" * 64),
        ("sentence_ordinal", 99),
        ("char_start", True),
    ):
        tampered = json.loads(json.dumps(original_closure))
        tampered["citation_occurrences"][0][field] = value
        (stage17 / "citation_closure_report.json").write_text(
            canonical_json_text(tampered), encoding="utf-8"
        )
        with pytest.raises(ValueError):
            validate_citation_closure_report(run_dir, config)
    synchronized_hash_tamper = json.loads(json.dumps(original_closure))
    synchronized_hash_tamper["citation_plan_sha256"] = "f" * 64
    synchronized_hash_tamper["citation_occurrences"][0][
        "citation_plan_sha256"
    ] = "f" * 64
    (stage17 / "citation_closure_report.json").write_text(
        canonical_json_text(synchronized_hash_tamper), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        validate_citation_closure_report(run_dir, config)
    (stage17 / "citation_closure_report.json").write_text(
        canonical_json_text(original_closure), encoding="utf-8"
    )
    draft_text = (stage17 / "paper_draft.md").read_text(encoding="utf-8")
    assert validate_paper_citation_minimum(
        run_dir, config, draft_text, minimum=1
    ) == (key,)
    stage19 = run_dir / "stage-19"
    stage19.mkdir()
    stage20 = run_dir / "stage-20"
    stage20.mkdir()
    (stage19 / "paper_revised.md").write_text(
        draft_text.replace(f"[{key}]", ""), encoding="utf-8"
    )
    quality = _execute_quality_gate(
        stage20, run_dir, config, AdapterBundle(), llm=None
    )
    assert quality.status is StageStatus.FAILED
    assert "citation minimum" in (quality.error or "").lower()
    assert "minimum=1" in (quality.error or "")
    closure["paper_sha256"] = "0" * 64
    (stage17 / "citation_closure_report.json").write_text(
        canonical_json_text(closure), encoding="utf-8"
    )
    stage18 = run_dir / "stage-18"
    stage18.mkdir()
    review = _execute_peer_review(
        stage18, run_dir, config, AdapterBundle(), llm=None
    )
    assert review.status is StageStatus.FAILED
    assert "closure" in (review.error or "").lower()


@pytest.mark.parametrize("repair_kind", ["foreign_key", "numeric_rewrite"])
def test_stage17_rejects_non_marker_heading_repair(
    tmp_path: Path, repair_kind: str,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage9 = run_dir / "stage-09"
    stage9.mkdir()
    dump_contract(derive_contract(config, None), stage9 / "experiment_contract.yaml")
    run_path = run_dir / "stage-12" / "runs" / "results.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        json.dumps(
            {
                "claim_scope": "pipeline_validation",
                "dataset_origin": "synthetic",
                "evaluator_owner": "scaffold",
                "metrics": {"detection_f1": 0.95},
            }
        ),
        encoding="utf-8",
    )
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    key = shortlist[0]["cite_key"]
    related_work = (
        "Prior work reported 91.5%."
        if repair_kind == "numeric_rewrite"
        else "Evidence-backed context."
    )
    repair = (
        f"## Related Work\n\nPrior work reported 92.5% [{key}]."
        if repair_kind == "numeric_rewrite"
        else "## Related Work\n\nForeign repair [foreign2024]."
    )
    llm = _SequenceLLM(
        [
            "## Title\n\nBounded Study\n\n## Abstract\n\nAbstract.\n\n"
            "## Introduction\n\nIntroduction.",
            f"## Related Work\n\n{related_work}",
            "## Method\n\nMethod.\n\n## Experiments\n\nExperiment setup.",
            "## Results\n\nDetection F1 was 95%.\n\n"
            "## Discussion\n\nDiscussion.\n\n"
            "## Limitations\n\nLimitations.\n\n"
            "## Conclusion\n\nConclusion.",
            repair,
        ]
    )
    stage17 = run_dir / "stage-17"
    stage17.mkdir()
    result = _execute_paper_draft(
        stage17,
        run_dir,
        config,
        AdapterBundle(),
        llm=llm,  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert "heading citation closure failed" in (result.error or "").lower()
    assert (stage17 / "paper_draft_invalid.md").is_file()
    assert not (stage17 / "paper_draft.md").exists()
    assert not (stage17 / "experiment_fact_closure_report.json").exists()
    assert not (stage17 / "citation_closure_report.json").exists()


def test_citation_plan_v2_assigns_hep_background_to_introduction(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir, profile="hep_ph")
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    plan = load_final_citation_plan(run_dir, config)
    assert {tuple(claim["section_path"]) for claim in plan["claims"]} == {
        ("Introduction",)
    }
    assert _prompt_bank_domain_from_config(config) == "hep_ph"
    assert "section: Introduction" in build_citation_writer_instruction(
        run_dir, config
    )


def test_prompt_bank_derivation_ignores_process_forced_profile(tmp_path: Path) -> None:
    config = _real_config_snapshot(tmp_path / "run", profile="")
    set_forced_profile("hep_ph")
    try:
        assert _prompt_bank_domain_from_config(config) == "ml"
    finally:
        set_forced_profile("")


def test_citation_plan_v2_rejects_legacy_and_noncanonical_sections(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, _paper, _keys = _prepare_stage23_fixture(run_dir)
    plan_path = run_dir / "stage-16" / "citation_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    legacy = {**plan, "plan_version": 1}
    with pytest.raises(CitationPlanContractError, match="plan version"):
        parse_citation_plan(canonical_json_text(legacy))

    for invalid_path in (
        ["Introduction", "Related Work"],
        ["Discussion"],
    ):
        tampered = json.loads(json.dumps(plan))
        tampered["claims"][0]["section_path"] = invalid_path
        with pytest.raises(CitationPlanContractError, match="section_path"):
            parse_citation_plan(canonical_json_text(tampered))


def test_stage20_22_and_23_reject_bibliography_key_outside_allowlist(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(i) for i in range(1, 6)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    llm = _SequenceLLM(_card_responses(shortlist[:4]) + ["{}", "{}"])
    assert _execute_knowledge_extract(
        stage6, run_dir, config, AdapterBundle(), llm=llm  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    planned = load_final_citation_plan(run_dir, config)
    planned_keys = [
        citation["cite_key"]
        for claim in planned["claims"]
        for citation in claim["planned_citations"]
    ]
    ineligible_key = shortlist[4]["cite_key"]
    final_text = "## Introduction\n\n" + " ".join(
        f"Evidence [{key}]." for key in planned_keys + [ineligible_key]
    )
    stage19 = run_dir / "stage-19"
    stage19.mkdir()
    (stage19 / "paper_revised.md").write_text(final_text, encoding="utf-8")
    stage20 = run_dir / "stage-20"
    stage20.mkdir()
    quality = _execute_quality_gate(
        stage20, run_dir, config, AdapterBundle(), llm=None
    )
    assert quality.status is StageStatus.FAILED
    assert "invalid=" in (quality.error or "")

    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    exported = _execute_export_publish(
        stage22, run_dir, config, AdapterBundle(), llm=None
    )
    assert exported.status is StageStatus.FAILED
    assert "canonical experiment evidence" in (exported.error or "")
    assert not (stage22 / "paper_final.md").exists()
    assert not (stage22 / "stage22_export_manifest.json").exists()

    (stage22 / "paper_final.md").write_text(final_text, encoding="utf-8")
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    verified = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert verified.status is StageStatus.FAILED
    assert "input replay" in (verified.error or "").lower()


def test_stage23_verifies_only_final_cited_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, paper_text, planned_keys = _prepare_stage23_fixture(
        run_dir, fail_last_card=True
    )
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    shadow = run_dir / "stage-99"
    shadow.mkdir()
    (shadow / "paper_final.md").write_text(
        "## Introduction\n\nShadow [fake2024].\n", encoding="utf-8"
    )
    captured_keys: set[str] = set()

    def _verify(bib_text: str, **_kwargs: object) -> VerificationReport:
        captured_keys.update(
            str(entry["key"]) for entry in parse_bibtex_entries(bib_text)
        )
        return _verification_report(planned_keys)

    monkeypatch.setattr("researchclaw.pipeline.stage23_verification.verify_citations", _verify)
    relevance = json.dumps({key: 0.9 for key in planned_keys})
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([relevance]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.DONE, result.error
    assert result.decision is None
    assert captured_keys == set(planned_keys)
    assert len(captured_keys) < 5
    assert (stage23 / "paper_final_verified.md").read_text() == paper_text
    report = json.loads((stage23 / "verification_report.json").read_text())
    assert report["summary"]["verification_complete"] is True
    assert report["summary"]["relevance_complete"] is True


def test_stage23_pipeline_validation_degrades_without_relevance_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, paper_text, planned_keys = _prepare_stage23_fixture(run_dir)
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(planned_keys),
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.DONE, result.error
    assert result.decision == "degraded"
    assert (stage23 / "paper_final_verified.md").read_text() == paper_text
    report = json.loads((stage23 / "verification_report.json").read_text())
    assert report["summary"]["unscored_keys"] == sorted(planned_keys)
    assert report["summary"]["fatal"] is False


def test_stage23_pipeline_validation_still_fails_hallucinated_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, paper_text, planned_keys = _prepare_stage23_fixture(run_dir)
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(
            planned_keys, status=VerifyStatus.HALLUCINATED
        ),
    )
    relevance = json.dumps({key: 0.9 for key in planned_keys})
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([relevance]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert list(stage23.iterdir()) == []


@pytest.mark.parametrize(
    "status",
    [VerifyStatus.SKIPPED, VerifyStatus.SUSPICIOUS, VerifyStatus.HALLUCINATED],
)
def test_stage23_strict_scope_rejects_incomplete_verification_without_editing_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: VerifyStatus,
) -> None:
    run_dir = tmp_path / "run"
    config, paper_text, planned_keys = _prepare_stage23_fixture(
        run_dir, claim_scope="exploratory"
    )
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(
            planned_keys, status=status
        ),
    )
    relevance = json.dumps({key: 0.9 for key in planned_keys})
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([relevance]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert result.decision == "retry"
    assert list(stage23.iterdir()) == []
    assert all(f"[{key}]" in paper_text for key in planned_keys)


def test_stage23_strict_scope_rejects_malformed_relevance_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, _paper_text, planned_keys = _prepare_stage23_fixture(
        run_dir, claim_scope="exploratory"
    )
    paper_text = (run_dir / "stage-22/paper_final.md").read_text(encoding="utf-8")
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(planned_keys),
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([json.dumps({planned_keys[0]: True})]),  # type: ignore[arg-type]
    )
    assert result.status is StageStatus.FAILED
    assert list(stage23.iterdir()) == []


def test_stage23_rejects_verification_result_closure_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, _paper_text, planned_keys = _prepare_stage23_fixture(run_dir)
    paper_text = (run_dir / "stage-22/paper_final.md").read_text(encoding="utf-8")
    _patch_stage23_canonical_input(
        monkeypatch, run_dir, config, paper_text, planned_keys
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage23_verification.verify_citations",
        lambda *_args, **_kwargs: _verification_report(planned_keys[:-1]),
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert "closure mismatch" in (result.error or "").lower()


def test_stage23_rejects_bounded_bibliography_closure_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, _paper_text, planned_keys = _prepare_stage23_fixture(run_dir)
    canonical_bib = (run_dir / "stage-04" / "references.bib").read_text()
    truncated_bib = canonical_bib.replace(
        next(
            entry
            for entry in canonical_bib.split("\n\n")
            if planned_keys[-1] in entry
        ),
        "",
    )
    paper_text = (run_dir / "stage-22/paper_final.md").read_text(encoding="utf-8")
    _patch_stage23_canonical_input(
        monkeypatch,
        run_dir,
        config,
        paper_text,
        planned_keys,
        bibliography_text=truncated_bib,
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert "bounded bibliography" in (result.error or "").lower()


def test_stage23_rejects_cited_paper_without_canonical_bibliography(
    tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    (stage22 / "paper_final.md").write_text(
        "## Introduction\n\nUnsupported [fake2024].\n", encoding="utf-8"
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert "input replay" in (result.error or "").lower()
    assert not (stage23 / "verification_report.json").exists()


def test_stage23_rejects_symlinked_canonical_paper(
    tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    outside = tmp_path / "outside.md"
    outside.write_text("## Introduction\n", encoding="utf-8")
    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    (stage22 / "paper_final.md").symlink_to(outside)
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert "input replay" in (result.error or "").lower()


def test_stage23_cleans_stale_verified_outputs_before_early_failure(
    tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    (stage22 / "paper_final.md").write_text(
        "## Introduction\n\nUnknown [fake2024].\n", encoding="utf-8"
    )
    stage23 = run_dir / "stage-23"
    stage23.mkdir()
    for name in (
        "verification_report.json",
        "references_verified.bib",
        "paper_final_verified.md",
    ):
        (stage23 / name).write_text("stale", encoding="utf-8")
    result = _execute_citation_verify(
        stage23, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert not any(
        (stage23 / name).exists()
        for name in (
            "verification_report.json",
            "references_verified.bib",
            "paper_final_verified.md",
        )
    )



def test_e9_replays_complete_citation_evidence_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    audit_citation_evidence(
        run_dir,
        contract_path=contract_path,
        contract=load_contract(contract_path),
    )


def test_e9_replays_profiled_hep_citation_plan_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    config, _planned_keys = _prepare_e9_run(
        run_dir, monkeypatch, profile="hep_ph"
    )
    plan = load_final_citation_plan(run_dir, config)
    assert {tuple(claim["section_path"]) for claim in plan["claims"]} == {
        ("Introduction",)
    }
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    audit_citation_evidence(
        run_dir,
        contract_path=contract_path,
        contract=load_contract(contract_path),
    )


def test_citation_closure_does_not_merge_discussion_h3_into_related_work(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config, _paper_text, planned_keys = _prepare_stage23_fixture(run_dir)
    metric_path = run_dir / "stage-12" / "runs" / "results.json"
    metric_path.parent.mkdir(parents=True)
    metric_path.write_text(
        json.dumps({"metrics": {"detection_f1": 0.95}}), encoding="utf-8"
    )
    key = planned_keys[0]
    paper = f"## Related Work\n\nNo citation.\n\n## Discussion\n\n### Theme\n\nEvidence [{key}].\n"
    structure_text = canonical_json_text(
        {
            "schema_version": 1,
            "valid": True,
            "source_sha256": sha256_text(paper),
            "section_count": 3,
            "issues": [],
        }
    )
    experiment_text = canonical_experiment_fact_json_text(
        build_experiment_fact_closure_report(run_dir, paper_text=paper)
    )
    closure = build_citation_closure_report(
        run_dir,
        config,
        paper_text=paper,
        structure_report_text=structure_text,
        experiment_fact_report_text=experiment_text,
    )
    assert key in closure["misplaced_planned_keys"]


def test_e9_rejects_citation_plan_v2_section_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    plan_path = run_dir / "stage-16" / "citation_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["claims"][0]["section_path"] == ["Related Work"]
    plan["claims"][0]["section_path"] = ["Introduction"]
    plan_path.write_text(canonical_json_text(plan), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "citation_plan_replay_failed"


def test_e9_rejects_support_artifact_single_point_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    support_path = run_dir / "stage-24" / "citation_support.json"
    support = json.loads(support_path.read_text())
    support["instances"][0]["claim_text"] += " tampered"
    support_path.write_text(json.dumps(support), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "citation_support_replay_failed"


def test_e9_replays_stage5_prefilter_instead_of_trusting_report_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    shortlist_path = run_dir / "stage-05" / "shortlist.jsonl"
    shortlist_lines = shortlist_path.read_text().splitlines()
    removed = json.loads(shortlist_lines.pop())
    shortlist_text = "\n".join(shortlist_lines) + "\n"
    shortlist_path.write_text(shortlist_text, encoding="utf-8")
    report_path = run_dir / "stage-05" / "screening_report.json"
    report = json.loads(report_path.read_text())
    identity = removed["source_identity"]
    report["screening_output_sha256"] = sha256_text(shortlist_text)
    report["selected_candidate_ids"].remove(identity)
    report["screened_candidate_ids"].remove(identity)
    report["prefilter_rejected_candidate_ids"].append(identity)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "literature_screening_replay_failed"
    assert "prefilter partition replay" in exc_info.value.message


def test_e9_rejects_stage5_policy_v1_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    report_path = run_dir / "stage-05" / "screening_report.json"
    report = json.loads(report_path.read_text())
    report["screening_policy_version"] = 1
    report["batch_size"] = 25
    report_path.write_text(canonical_json_text(report), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None

    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )

    assert exc_info.value.code == "literature_screening_replay_failed"
    assert "screening_policy_version" in exc_info.value.message


def test_e9_rejects_active_config_contract_scope_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    contract_text = contract_path.read_text().replace(
        "claim_scope: pipeline_validation", "claim_scope: exploratory"
    )
    contract_path.write_text(contract_text, encoding="utf-8")
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "citation_contract_scope_mismatch"


def test_e9_rejects_verification_summary_self_report_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    report_path = run_dir / "stage-23" / "verification_report.json"
    report = json.loads(report_path.read_text())
    report["summary"]["verified"] -= 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "citation_verification_replay_failed"


@pytest.mark.parametrize("artifact", ["claims", "citations", "truth"])
def test_e9_rejects_stage24_cross_artifact_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    run_dir = tmp_path / "run"
    _config_value, _planned_keys = _prepare_e9_run(run_dir, monkeypatch)
    if artifact == "claims":
        path = run_dir / "stage-24" / "claims.json"
        payload = json.loads(path.read_text())
        support_claim = next(
            row for row in payload["claims"] if row["id"].startswith("support-")
        )
        support_claim["status"] = "unsupported"
    elif artifact == "citations":
        path = run_dir / "stage-24" / "citations.json"
        payload = json.loads(path.read_text())
        payload["instances"] = list(reversed(payload["instances"]))
    else:
        path = run_dir / "stage-24" / "truth_audit.json"
        payload = json.loads(path.read_text())
        payload["citation_support_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    contract_path = find_stage09_contract(run_dir)
    assert contract_path is not None
    with pytest.raises(CitationAuditError) as exc_info:
        audit_citation_evidence(
            run_dir,
            contract_path=contract_path,
            contract=load_contract(contract_path),
        )
    assert exc_info.value.code == "citation_support_replay_failed"


def test_stage22_rejects_legacy_paper_when_canonical_bundle_is_missing(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    stage19 = run_dir / "stage-19"
    stage19.mkdir(parents=True)
    (stage19 / "paper_revised.md").write_text(
        "## Introduction\n\nPrior work [alpha2020, beta2021].\n",
        encoding="utf-8",
    )
    stage22 = run_dir / "stage-22"
    stage22.mkdir()
    result = _execute_export_publish(
        stage22, run_dir, config, AdapterBundle(), llm=None
    )
    assert result.status is StageStatus.FAILED
    assert "canonical experiment evidence" in (result.error or "")
    assert not (stage22 / "paper_final.md").exists()
    assert not (stage22 / "stage22_export_manifest.json").exists()


def test_resumed_config_without_active_pointer_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    (run_dir / "active_config_snapshot.json").unlink()
    (run_dir / "config_snapshot_history.jsonl").unlink()
    (run_dir / "config.resumed-20260711-120000.yaml").write_text(
        (run_dir / "config.yaml").read_text(), encoding="utf-8"
    )
    with pytest.raises(CitationPolicyContractError, match="without active config pointer"):
        resolve_active_config_snapshot(run_dir, config)


def test_config_history_cannot_be_truncated_before_append(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _real_config_snapshot(run_dir)
    base_text = (run_dir / "config.yaml").read_text(encoding="utf-8")
    resumed = run_dir / "config.resumed-20260711-120000.yaml"
    resumed.write_text(base_text, encoding="utf-8")
    write_active_config_binding(run_dir, resumed)
    history_path = run_dir / "config_snapshot_history.jsonl"
    history_lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(history_lines) == 2
    history_path.write_text(history_lines[0] + "\n", encoding="utf-8")
    third = run_dir / "config.resumed-20260711-120001.yaml"
    third.write_text(base_text, encoding="utf-8")
    with pytest.raises(CitationPolicyContractError, match="history hash mismatch"):
        write_active_config_binding(run_dir, third)


def test_active_config_binding_updates_and_replays_checkpoint(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "checkpoint.json").write_text(
        canonical_json_text({"last_completed_stage": 15, "run_id": "test"}),
        encoding="utf-8",
    )
    source = Path("config.deepseek.sectional-dry-run.yaml")
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    snapshot = run_dir / "config.yaml"
    snapshot.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    config = RCConfig.from_dict(raw, project_root=run_dir, check_paths=False)
    write_active_config_binding(run_dir, snapshot)
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    assert checkpoint["active_config_snapshot_path"] == "config.yaml"
    resolve_active_config_snapshot(run_dir, config)

    checkpoint["active_config_snapshot_sha256"] = "0" * 64
    checkpoint_path.write_text(canonical_json_text(checkpoint), encoding="utf-8")
    with pytest.raises(CitationPolicyContractError, match="checkpoint config binding"):
        resolve_active_config_snapshot(run_dir, config)


@pytest.mark.parametrize(
    ("stage_name", "executor"),
    [
        ("stage-17", _execute_paper_draft),
        ("stage-18", _execute_peer_review),
        ("stage-20", _execute_quality_gate),
    ],
)
def test_paper_consumers_reject_tampered_effective_policy(
    tmp_path: Path, stage_name: str, executor: Any
) -> None:
    run_dir = tmp_path / "run"
    config = _real_config_snapshot(run_dir)
    shortlist = _prepare_stage5(run_dir, [_candidate(1)], config)
    stage6 = run_dir / "stage-06"
    stage6.mkdir()
    assert _execute_knowledge_extract(
        stage6,
        run_dir,
        config,
        AdapterBundle(),
        llm=_SequenceLLM([_card_response(shortlist)]),  # type: ignore[arg-type]
    ).status is StageStatus.DONE
    stage16 = run_dir / "stage-16"
    stage16.mkdir()
    assert _execute_paper_outline(
        stage16, run_dir, config, AdapterBundle(), llm=None
    ).status is StageStatus.DONE
    policy_path = stage16 / "citation_policy_effective.json"
    policy = json.loads(policy_path.read_text())
    policy["eligible_count"] = 2
    policy_path.write_text(canonical_json_text(policy), encoding="utf-8")
    stage_dir = run_dir / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    result = executor(stage_dir, run_dir, config, AdapterBundle(), llm=None)
    assert result.status is StageStatus.FAILED
    assert "citation policy" in (result.error or "").lower()
