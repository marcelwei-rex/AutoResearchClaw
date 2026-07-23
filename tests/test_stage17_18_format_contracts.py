from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from researchclaw.literature import citation_plan as citation_plan_module
pytestmark = pytest.mark.usefixtures(
    "canonical_evidence_migration_complete",
    "consumer_evidence_fixture",
)

from researchclaw.pipeline.stage_impls import _paper_writing, _review_publish
from researchclaw.pipeline.stage_impls._paper_writing import (
    PaperSectionContractError,
    _repair_heading_citation_closure,
    _assert_marker_only_citation_delta,
    _validate_paper_part_sections,
    _validate_stage17_manuscript_structure,
    _write_paper_sections,
)
from researchclaw.literature.citation_plan import (
    CitationPlanContractError,
    build_heading_citation_writer_instructions_from_authority,
    project_citation_anchors,
    require_citation_candidate_free,
    validate_citation_free_anchor_draft,
)
from researchclaw.pipeline.stage_impls._review_publish import (
    _citation_count_policy_violations,
    _execute_peer_review,
)
from researchclaw.prompts import PromptManager
from researchclaw.pipeline.stages import StageStatus


@pytest.fixture(autouse=True)
def _stub_effective_citation_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _review_publish,
        "load_effective_citation_policy",
        lambda *_args: {
            "effective_min_unique_sources": 1,
            "effective_target_unique_sources": 15,
        },
    )
    def closure_for_current_draft(
        run_dir: Path,
        *_args: object,
        evidence: object | None = None,
        **_kwargs: object,
    ) -> dict[str, str]:
        draft_bytes = (run_dir / "stage-17" / "paper_draft.md").read_bytes()
        payload = {
            "paper_sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "canonical_experiment_evidence_path": str(
                getattr(evidence, "manifest_path", "canonical_experiment_evidence.json")
            ),
            "canonical_experiment_evidence_sha256": str(
                getattr(
                    evidence,
                    "manifest_sha256",
                    hashlib.sha256(b"consumer-test-evidence").hexdigest(),
                )
            ),
        }
        structure = run_dir / "stage-17" / "paper_structure_report.json"
        fact = run_dir / "stage-17" / "experiment_fact_closure_report.json"
        if structure.is_file() and fact.is_file():
            payload["structure_report_sha256"] = hashlib.sha256(
                structure.read_bytes()
            ).hexdigest()
            payload["experiment_fact_closure_report_sha256"] = hashlib.sha256(
                fact.read_bytes()
            ).hexdigest()
        return payload

    monkeypatch.setattr(
        _review_publish, "validate_experiment_fact_closure_report", closure_for_current_draft
    )
    monkeypatch.setattr(
        _review_publish, "validate_citation_closure_report", closure_for_current_draft
    )


class _PromptManagerStub:
    def block(self, _name: str, **_kwargs: object) -> str:
        return ""

    def for_stage(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            system="system",
            user="review the paper",
            json_mode=False,
            max_tokens=8192,
        )


class _SequentialLLM:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []
        self.systems: list[str] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: object) -> SimpleNamespace:
        self.calls.append(messages)
        self.systems.append(str(kwargs.get("system", "")))
        return SimpleNamespace(content=self.responses.pop(0))


def test_strict_citation_occurrence_parser_handles_syntax_matrix() -> None:
    text = (
        "Prior [important context] \\[escaped2024key] "
        "[smith2024deep] [smith2024deep, jones2023graph] "
        "[smith2024deep;  jones2023graph] "
        "[smith2024deep, smith2024deep] "
        r"\cite{smith2024deep,jones2023graph} "
        r"\\cite{escaped2024key}."
    )

    occurrences = citation_plan_module.parse_strict_citation_occurrences(text)

    assert [item.syntax for item in occurrences] == [
        "markdown", "markdown", "markdown", "latex"
    ]
    assert occurrences[0].keys == ("smith2024deep",)
    assert occurrences[1].keys == ("smith2024deep", "jones2023graph")
    assert occurrences[2].keys == ("smith2024deep", "jones2023graph")
    assert occurrences[3].keys == ("smith2024deep", "jones2023graph")
    assert all(item.char_start < item.char_end for item in occurrences)
    assert all(item.sentence_start < item.sentence_end for item in occurrences)
    assert all(len(item.sentence_sha256) == 64 for item in occurrences)


def test_strict_parser_preserves_noncitation_and_escaped_brackets() -> None:
    text = r"Keep [important context] and \[smith2024deep]."
    assert citation_plan_module.parse_strict_citation_occurrences(text) == ()
    assert citation_plan_module.filter_strict_citation_markers(
        text, frozenset()
    ) == text


def _anchor_plan(claim_text: str = "Exact bounded claim.") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_version": 2,
        "plan_status": "final",
        "claim_scope": "pipeline_validation",
        "citation_allowlist_path": "stage-06/citation_allowlist.json",
        "citation_allowlist_sha256": "a" * 64,
        "cards_manifest_path": "stage-06/cards_manifest.json",
        "cards_manifest_sha256": "b" * 64,
        "effective_policy_path": "stage-16/citation_policy_effective.json",
        "effective_policy_sha256": "c" * 64,
        "claims": [
            {
                "claim_id": "planned-claim-001",
                "section_path": ["Related Work"],
                "claim_text": claim_text,
                "claim_type": "background",
                "planned_citations": [
                    {
                        "cite_key": "smith2024deep",
                        "evidence_excerpt_ids": ["ev-1"],
                        "support_status": "abstract_sufficient",
                    }
                ],
            }
        ],
    }


def _many_anchor_plan(count: int) -> dict[str, Any]:
    plan = _anchor_plan("Exact bounded claim 001.")
    claims = []
    for ordinal in range(1, count + 1):
        claim = json.loads(json.dumps(plan["claims"][0]))
        claim["claim_id"] = f"planned-claim-{ordinal:03d}"
        claim["claim_text"] = f"Exact bounded claim {ordinal:03d}."
        claim["planned_citations"][0]["cite_key"] = f"source{ordinal:04d}key"
        claims.append(claim)
    plan["claims"] = claims
    return plan


def _domain_v2_heading_instructions() -> dict[str, str]:
    return {
        heading: "No citation authority."
        for heading in (
            "Abstract", "Introduction", "Related Work", "Method", "Experiments",
            "Results", "Discussion", "Limitations", "Conclusion",
        )
    }


def _domain_v2_zero_authority_responses() -> list[str]:
    return [
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.",
        "## Method\n\nM.\n\n## Experiments\n\nE.",
        "## Results\n\nR.\n\n## Discussion\n\nD.",
        "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
    ]


def test_domain_v2_anchor_batches_are_contiguous_and_bounded() -> None:
    anchors = project_citation_anchors(_many_anchor_plan(11))

    batches = citation_plan_module.project_contiguous_citation_anchor_batches(
        anchors,
        render_prompt=lambda batch: (
            "system",
            "\n".join(anchor.claim_text for anchor in batch),
        ),
    )

    assert [tuple(anchor.claim_id for anchor in batch.anchors) for batch in batches] == [
        tuple(f"planned-claim-{ordinal:03d}" for ordinal in range(1, 6)),
        tuple(f"planned-claim-{ordinal:03d}" for ordinal in range(6, 11)),
        ("planned-claim-011",),
    ]
    assert all(batch.prompt_utf8_bytes <= 16_384 for batch in batches)
    assert all(len(batch.anchors) <= 5 for batch in batches)


def test_domain_v2_anchor_batch_oversize_fails_before_writer_call() -> None:
    anchors = project_citation_anchors(_anchor_plan())
    render_calls = 0

    def render(batch: tuple[object, ...]) -> tuple[str, str]:
        nonlocal render_calls
        render_calls += 1
        return ("s", "x" * 16_384)

    with pytest.raises(CitationPlanContractError, match="prompt budget"):
        citation_plan_module.project_contiguous_citation_anchor_batches(
            anchors,
            render_prompt=render,
        )
    assert render_calls == 1


def test_domain_v2_anchor_batch_byte_boundary_is_exact() -> None:
    anchors = project_citation_anchors(_many_anchor_plan(2))

    exact = citation_plan_module.project_contiguous_citation_anchor_batches(
        anchors,
        render_prompt=lambda batch: ("", "x" * (5 if len(batch) == 2 else 2)),
        max_anchors=5,
        max_prompt_utf8_bytes=5,
    )
    split = citation_plan_module.project_contiguous_citation_anchor_batches(
        anchors,
        render_prompt=lambda batch: ("", "x" * (6 if len(batch) == 2 else 2)),
        max_anchors=5,
        max_prompt_utf8_bytes=5,
    )

    assert [len(batch.anchors) for batch in exact] == [2]
    assert [len(batch.anchors) for batch in split] == [1, 1]


def test_domain_v2_headingless_fragment_rejects_heading_and_foreign_anchor() -> None:
    anchors = project_citation_anchors(_many_anchor_plan(2))
    citation_plan_module.validate_citation_free_anchor_fragment(
        "Exact bounded claim 001.\n",
        anchors=anchors[:1],
        all_anchors=anchors,
    )
    for invalid in (
        "## Related Work\n\nExact bounded claim 001.\n",
        "Exact bounded claim 001.\nExact bounded claim 002.\n",
    ):
        with pytest.raises(CitationPlanContractError):
            citation_plan_module.validate_citation_free_anchor_fragment(
                invalid,
                anchors=anchors[:1],
                all_anchors=anchors,
            )


def test_domain_v2_fragment_batch_must_be_exact_contiguous_authority_slice() -> None:
    anchors = project_citation_anchors(_many_anchor_plan(3))
    forged = type(anchors[0])(
        claim_id="planned-claim-999",
        heading="Related Work",
        claim_text="Forged exact claim.",
        cite_key="forged2024key",
    )
    cases = (
        (
            "Forged exact claim.\n",
            (forged,),
            anchors,
        ),
        (
            "Exact bounded claim 001.\nExact bounded claim 003.\n",
            (anchors[0], anchors[2]),
            anchors,
        ),
        (
            "Exact bounded claim 001.\n",
            (anchors[0],),
            anchors + (anchors[0],),
        ),
    )
    for text, batch, full in cases:
        with pytest.raises(CitationPlanContractError):
            citation_plan_module.validate_citation_free_anchor_fragment(
                text,
                anchors=batch,
                all_anchors=full,
            )


@pytest.mark.parametrize(
    "candidate",
    (
        "(Smith et al., 2024) reported this.",
        "smith et al. (2024a) reported this.",
        "Smith et al., 2024 reported this.",
        "(Smith et al., 2024 reported this.",
        "Smith (2024) reported this.",
        "Smith and Jones (2024a) reported this.",
        "Smith & Jones (2024) reported this.",
        "Smith, 2024 reported this.",
        "Smith (2024 reported this.",
        "Smith and Jones (2024a reported this.",
    ),
)
def test_domain_v2_fragment_rejects_bare_author_year_citations(
    candidate: str,
) -> None:
    anchors = project_citation_anchors(_anchor_plan())
    with pytest.raises(CitationPlanContractError, match="citation candidate"):
        citation_plan_module.validate_citation_free_anchor_fragment(
            f"{candidate}\nExact bounded claim.\n",
            anchors=anchors,
            all_anchors=anchors,
        )


@pytest.mark.parametrize(
    "heading",
    (
        "<h2>Related Work</h2>",
        "<H3>Theme</H3>",
        '<h4 class="theme">Theme</h4>',
    ),
)
def test_domain_v2_fragment_rejects_html_headings(heading: str) -> None:
    anchors = project_citation_anchors(_anchor_plan())
    with pytest.raises(CitationPlanContractError, match="heading"):
        citation_plan_module.validate_citation_free_anchor_fragment(
            f"{heading}\nExact bounded claim.\n",
            anchors=anchors,
            all_anchors=anchors,
        )


def test_domain_v2_fragment_allows_plain_year_prose() -> None:
    anchors = project_citation_anchors(_anchor_plan())
    for prose in (
        "Several methods were published in 2024.",
        "In 2024, several methods were published.",
        "The report, 2024 edition, remains available.",
        "The dataset, 2024 release, contains 100 rows.",
        "The report (2024 edition) remains available.",
        "The dataset (2024 release) remains available.",
        "The specification (2024 version) was used.",
    ):
        citation_plan_module.validate_citation_free_anchor_fragment(
            f"{prose}\nExact bounded claim.\n",
            anchors=anchors,
            all_anchors=anchors,
        )


def test_domain_v2_related_work_uses_isolated_contiguous_batches(
    tmp_path: Path,
) -> None:
    anchors = project_citation_anchors(_many_anchor_plan(6))
    responses = _domain_v2_zero_authority_responses()
    responses[1:1] = [
        "\n".join(anchor.claim_text for anchor in anchors[:5]),
        anchors[5].claim_text,
    ]
    llm = _SequentialLLM(responses)

    draft = _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
        stage_dir=tmp_path,
        citation_repair_claims=tuple(
            {
                "section": anchor.heading,
                "claim_text": anchor.claim_text,
                "cite_key": anchor.cite_key,
            }
            for anchor in anchors
        ),
        heading_citation_instructions=_domain_v2_heading_instructions(),
        canonical_fact_sheet=_minimal_cfs(),
        citation_anchors=anchors,
    )

    assert draft.count("## Related Work") == 1
    positions = [draft.index(anchor.claim_text) for anchor in anchors]
    assert positions == sorted(positions)
    assert len(llm.calls) == 6
    batch_prompts = [
        "\n".join(message["content"] for message in llm.calls[index])
        for index in (1, 2)
    ]
    assert all(anchor.claim_id in batch_prompts[0] for anchor in anchors[:5])
    assert all(anchor.claim_id not in batch_prompts[0] for anchor in anchors[5:])
    assert anchors[5].claim_id in batch_prompts[1]
    assert all(anchor.claim_id not in batch_prompts[1] for anchor in anchors[:5])
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    related = next(item for item in report["parts"] if item["part"] == "heading-group-2")
    assert [batch["anchor_count"] for batch in related["batches"]] == [5, 1]
    assert all("prompt" not in batch and "response" not in batch for batch in related["batches"])


def test_domain_v2_precomputes_every_batch_before_first_llm_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchors = project_citation_anchors(_many_anchor_plan(6))
    llm = _SequentialLLM([])

    monkeypatch.setattr(
        _paper_writing,
        "project_contiguous_citation_anchor_batches",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CitationPlanContractError("later batch exceeds prompt budget")
        ),
    )

    with pytest.raises(CitationPlanContractError, match="later batch"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
            citation_repair_claims=tuple(
                {
                    "section": anchor.heading,
                    "claim_text": anchor.claim_text,
                    "cite_key": anchor.cite_key,
                }
                for anchor in anchors
            ),
            heading_citation_instructions=_domain_v2_heading_instructions(),
            canonical_fact_sheet=_minimal_cfs(),
            citation_anchors=anchors,
        )

    assert llm.calls == []


def test_domain_v2_full_replay_rejects_cross_batch_missing_and_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchors = project_citation_anchors(_many_anchor_plan(6))
    responses = _domain_v2_zero_authority_responses()
    responses[1:1] = [
        "\n".join(anchor.claim_text for anchor in anchors[:5]),
        anchors[4].claim_text,
    ]
    llm = _SequentialLLM(responses)
    monkeypatch.setattr(
        _paper_writing,
        "validate_citation_free_anchor_fragment",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(PaperSectionContractError, match="citation_anchor"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
            citation_repair_claims=tuple(
                {
                    "section": anchor.heading,
                    "claim_text": anchor.claim_text,
                    "cite_key": anchor.cite_key,
                }
                for anchor in anchors
            ),
            heading_citation_instructions=_domain_v2_heading_instructions(),
            canonical_fact_sheet=_minimal_cfs(),
            citation_anchors=anchors,
        )

    assert len(llm.calls) == 3
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    related = next(item for item in report["parts"] if item["part"] == "heading-group-2")
    assert related["full_replay_valid"] is False


def test_domain_v2_failed_batch_stops_before_later_batches(
    tmp_path: Path,
) -> None:
    anchors = project_citation_anchors(_many_anchor_plan(11))
    llm = _SequentialLLM(
        [
            _domain_v2_zero_authority_responses()[0],
            "\n".join(anchor.claim_text for anchor in anchors[:5]),
            "Foreign response.",
            "\n".join(anchor.claim_text for anchor in anchors[10:]),
        ]
    )

    with pytest.raises(PaperSectionContractError, match="citation_anchor"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
            citation_repair_claims=tuple(
                {
                    "section": anchor.heading,
                    "claim_text": anchor.claim_text,
                    "cite_key": anchor.cite_key,
                }
                for anchor in anchors
            ),
            heading_citation_instructions=_domain_v2_heading_instructions(),
            canonical_fact_sheet=_minimal_cfs(),
            citation_anchors=anchors,
        )

    assert len(llm.calls) == 3
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    related = next(item for item in report["parts"] if item["part"] == "heading-group-2")
    assert [batch["valid"] for batch in related["batches"]] == [True, False]


def test_domain_v2_realistic_25_anchor_plan_forms_five_batches(
    tmp_path: Path,
) -> None:
    anchors = project_citation_anchors(_many_anchor_plan(25))
    responses = [_domain_v2_zero_authority_responses()[0]]
    responses.extend(
        "\n".join(anchor.claim_text for anchor in anchors[start:start + 5])
        for start in range(0, 25, 5)
    )
    responses.extend(_domain_v2_zero_authority_responses()[1:])
    llm = _SequentialLLM(responses)

    draft = _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
        stage_dir=tmp_path,
        citation_repair_claims=tuple(
            {
                "section": anchor.heading,
                "claim_text": anchor.claim_text,
                "cite_key": anchor.cite_key,
            }
            for anchor in anchors
        ),
        heading_citation_instructions=_domain_v2_heading_instructions(),
        canonical_fact_sheet=_minimal_cfs(),
        citation_anchors=anchors,
    )

    assert draft.count("## Related Work") == 1
    assert len(llm.calls) == 9
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    related = next(item for item in report["parts"] if item["part"] == "heading-group-2")
    assert [
        (batch["first_claim_id"], batch["last_claim_id"])
        for batch in related["batches"]
    ] == [
        (f"planned-claim-{start:03d}", f"planned-claim-{start + 4:03d}")
        for start in range(1, 26, 5)
    ]
    assert all(batch["prompt_utf8_bytes"] <= 16_384 for batch in related["batches"])


@pytest.mark.parametrize(
    "candidate",
    [
        "Claim [smith2024deep].",
        r"Claim \[smith2024deep].",
        "Claim [smith2024deep, jones2023graph].",
        "Claim [smith2024deep, ].",
        "Claim [smith2024deep",
        r"Claim \cite{smith2024deep}.",
        r"Claim \\cite{smith2024deep}.",
        r"Claim \cite{smith2024deep.",
        "Claim [Smith et al., 2024].",
        "Claim [smith et al., 2024].",
        "Claim [Smith et al., 2024a].",
        "Claim [1].",
        "Claim [1, 3-5].",
        "Claim [@smith2024deep].",
        "Claim [Smith et al., 2024",
        "Claim [smith et al., 2024a",
        r"Claim \parencite{foreign2024paper}.",
        r"Claim \autocite{foreign2024paper}.",
    ],
)
def test_domain_v2_initial_draft_rejects_every_citation_candidate(
    candidate: str,
) -> None:
    with pytest.raises(CitationPlanContractError, match="citation candidate"):
        require_citation_candidate_free(candidate)


def test_domain_v2_initial_draft_preserves_noncitation_brackets() -> None:
    require_citation_candidate_free("Keep [important context] in the prose.")


def test_domain_v2_noncitation_bracket_does_not_absorb_later_text() -> None:
    require_citation_candidate_free(
        "Keep [important context] before the literal smith2024deep token."
    )


def test_citation_anchor_without_terminal_punctuation_requires_own_line() -> None:
    anchors = project_citation_anchors(_anchor_plan("No terminator"))
    validate_citation_free_anchor_draft(
        "## Related Work\n\nNo terminator\n",
        anchors=anchors,
        active_headings=("Related Work",),
    )
    with pytest.raises(CitationPlanContractError, match="missing, changed"):
        validate_citation_free_anchor_draft(
            "## Related Work\n\nNo terminator Next sentence.\n",
            anchors=anchors,
            active_headings=("Related Work",),
        )


@pytest.mark.parametrize(
    "claim_text",
    [" leading", "trailing ", "line\nbreak", "carriage\rreturn", "nul\x00byte"],
)
def test_citation_anchor_projection_rejects_unsafe_boundaries(
    claim_text: str,
) -> None:
    with pytest.raises(CitationPlanContractError, match="standalone-line"):
        project_citation_anchors(_anchor_plan(claim_text))


def test_domain_v2_anchor_draft_requires_exact_unique_ordered_claims() -> None:
    plan = _anchor_plan("First exact claim.")
    second = json.loads(json.dumps(plan["claims"][0]))
    second["claim_id"] = "planned-claim-002"
    second["claim_text"] = "Second exact claim."
    second["planned_citations"][0]["cite_key"] = "jones2023graph"
    plan["claims"].append(second)
    anchors = project_citation_anchors(plan)

    validate_citation_free_anchor_draft(
        "## Related Work\n\nFirst exact claim.\nSecond exact claim.\n",
        anchors=anchors,
        active_headings=("Related Work",),
    )
    for invalid in (
        "## Related Work\n\nFirst paraphrased claim.\nSecond exact claim.\n",
        "## Related Work\n\nFirst exact claim.\nFirst exact claim.\nSecond exact claim.\n",
        "## Related Work\n\nSecond exact claim.\nFirst exact claim.\n",
        "## Related Work\n\nFirst exact claim.\nSecond exact claim.\nsmith2024deep\n",
    ):
        with pytest.raises(CitationPlanContractError):
            validate_citation_free_anchor_draft(
                invalid,
                anchors=anchors,
                active_headings=("Related Work",),
            )


@pytest.mark.parametrize(
    "foreign",
    ("Exact bounded claim.", "smith2024deep"),
)
def test_domain_v2_zero_authority_heading_rejects_foreign_anchor_or_key(
    foreign: str,
) -> None:
    anchors = project_citation_anchors(_anchor_plan())
    with pytest.raises(CitationPlanContractError):
        validate_citation_free_anchor_draft(
            f"## Method\n\n{foreign}\n",
            anchors=anchors,
            active_headings=("Method",),
        )


def test_domain_v2_empty_plan_still_rejects_citation_candidate() -> None:
    with pytest.raises(CitationPlanContractError, match="citation candidate"):
        validate_citation_free_anchor_draft(
            "## Method\n\nClaim [1].\n",
            anchors=(),
            active_headings=("Method",),
        )


def test_domain_v2_paraphrased_anchor_fails_before_free_structure_repair(
    tmp_path: Path,
) -> None:
    anchors = project_citation_anchors(_anchor_plan())
    headings = (
        "Abstract", "Introduction", "Related Work", "Method", "Experiments",
        "Results", "Discussion", "Limitations", "Conclusion",
    )
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.",
            "## Related Work\n\nA paraphrase of the bounded claim.",
            "## Related Work\n\nExact bounded claim.",
        ]
    )

    with pytest.raises(PaperSectionContractError, match="citation_anchor"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
            citation_repair_claims=(
                {
                    "section": "Related Work",
                    "claim_text": "Exact bounded claim.",
                    "cite_key": "smith2024deep",
                },
            ),
            heading_citation_instructions={
                heading: "No citation authority." for heading in headings
            },
            canonical_fact_sheet=_minimal_cfs(),
            citation_anchors=anchors,
        )

    assert len(llm.calls) == 2


def test_domain_v2_structure_repair_cannot_reintroduce_citation_marker(
    tmp_path: Path,
) -> None:
    anchors = project_citation_anchors(_anchor_plan())
    headings = (
        "Abstract", "Introduction", "Related Work", "Method", "Experiments",
        "Results", "Discussion", "Limitations", "Conclusion",
    )
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.",
            (
                "## Related Work\n\nExact bounded claim.\n\n"
                "## Foreign Heading\n\nInvalid structure."
            ),
            "## Related Work\n\nExact bounded claim [smith2024deep].",
        ]
    )

    with pytest.raises(PaperSectionContractError, match="citation_anchor"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
            citation_repair_claims=(
                {
                    "section": "Related Work",
                    "claim_text": "Exact bounded claim.",
                    "cite_key": "smith2024deep",
                },
            ),
            heading_citation_instructions={
                heading: "No citation authority." for heading in headings
            },
            canonical_fact_sheet=_minimal_cfs(),
            citation_anchors=anchors,
        )

    assert len(llm.calls) == 2


def _minimal_cfs() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "bound_labels": {
            "dataset": "controlled_synthetic_iscas85",
            "evaluator_schema": "trojnet_iscas85_v1",
            "evaluator_id": "trojnet_iscas85",
            "benchmark_tokens": ("iscas85",),
            "circuit_tokens": ("c1355",),
        },
        "conditions": (
            {"id": "primary", "role": "primary"},
            {"id": "raw_cc1", "role": "comparator"},
        ),
        "seeds": (0, 1, 2),
        "circuit_families": ("c1355",),
        "variant_ids": ("c1355_ht1",),
        "variants_per_family": 1,
        "counts": {
            "observations": 1,
            "observations_per_condition": 1,
            "observations_per_condition_seed": 1,
            "invocations": 2,
        },
        "scale": {"n_total_min": 196, "n_total_max": 2480},
        "runtime": {
            "device": "cpu",
            "python": "3.11",
            "packages": {"torch": "2.12.1"},
            "torch_deterministic_algorithms": True,
            "torch_num_threads": 1,
        },
        "metric_keys": ("auprc",),
        "primary_metric": {
            "key": "auprc",
            "condition": "primary",
            "aggregation": "mean",
            "observation_set": "all",
            "value": 1,
        },
        "condition_aggregates": (),
        "per_seed_aggregates": (),
        "derived_facts": {},
        "observation_rows": (
            {
                "condition": "primary",
                "seed": 0,
                "circuit_variant": "c1355_ht1",
                "metrics": {"auprc": 1},
            },
        ),
    }


def _assert_fixture_canonical_binding(report: dict[str, Any]) -> None:
    assert report["canonical_experiment_evidence_path"] == (
        "canonical_experiment_evidence.json"
    )
    assert isinstance(report["canonical_experiment_evidence_sha256"], str)
    assert len(report["canonical_experiment_evidence_sha256"]) == 64


def _write_draft(run_dir: Path) -> None:
    stage_dir = run_dir / "stage-17"
    stage_dir.mkdir(parents=True)
    draft = "## Title\n\nExample\n\n## Method\n\nBody.\n"
    (stage_dir / "paper_draft.md").write_text(
        draft,
        encoding="utf-8",
    )
    (stage_dir / "paper_structure_report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "valid": True,
                "source_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "section_count": 2,
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    (stage_dir / "experiment_fact_closure_report.json").write_text("{}", encoding="utf-8")
    (stage_dir / "citation_closure_report.json").write_text("{}", encoding="utf-8")


def _config() -> Any:
    return SimpleNamespace(research=SimpleNamespace(topic="test topic"))


def test_stage17_structure_report_rejects_checked_in_duplicate_fixture(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "stage17_duplicate_headings.md"
    report = _validate_stage17_manuscript_structure(
        fixture.read_text(encoding="utf-8"),
        stage_dir=tmp_path,
    )

    assert report["valid"] is False
    assert {issue["code"] for issue in report["issues"]} == {
        "duplicate_heading_path"
    }
    assert json.loads(
        (tmp_path / "paper_structure_report.json").read_text(encoding="utf-8")
    ) == report


def test_stage17_structure_report_accepts_unique_heading_paths(tmp_path: Path) -> None:
    report = _validate_stage17_manuscript_structure(
        "## Title\n\nExample\n\n## Method\n\nBody.\n",
        stage_dir=tmp_path,
    )

    assert report["valid"] is True
    assert report["section_count"] == 2
    assert report["issues"] == []


def test_all_three_stage17_calls_receive_the_section_output_contract() -> None:
    llm = _SequentialLLM(
        [
            "## Title\n\nExample\n\n## Abstract\n\nAbstract.\n\n"
            "## Introduction\n\nIntroduction.\n\n## Related Work\n\nPrior work.",
            "## Method\n\nMethod.\n\n## Experiments\n\nExperiments.",
            "## Results\n\nResults.\n\n## Discussion\n\nDiscussion.\n\n"
            "## Limitations\n\nLimitations.\n\n## Conclusion\n\nConclusion.",
        ]
    )

    _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
    )

    assert len(llm.calls) == 3
    for call, system in zip(llm.calls, llm.systems, strict=True):
        prompt = "\n".join(message["content"] for message in call)
        assert "SECTION OUTPUT CONTRACT" in prompt
        assert "Output only the sections requested in this call" in prompt
        assert system.rstrip().endswith(
            "Do not emit a title/preamble outside the requested `##` sections."
        )


def test_stage17_writer_prompts_use_only_section_scoped_citation_authority() -> None:
    class _CitationMandatePromptManager(_PromptManagerStub):
        def for_stage(self, *_args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                system=(
                    "GENERAL CITATION MANDATE: CITE ORIGINAL PAPERS and "
                    "MUST cite baseline methods. "
                    f"SYSTEM_OUTLINE={kwargs['outline']}"
                ),
                user="review the paper",
                json_mode=False,
                max_tokens=8192,
            )

    llm = _SequentialLLM(
        [
            "## Title\n\nExample\n\n## Abstract\n\nAbstract.\n\n"
            "## Introduction\n\nIntroduction.\n\n"
            "## Related Work\n\nPrior work [smith2024deep].",
            "## Method\n\nMethod.\n\n## Experiments\n\nExperiments.",
            "## Results\n\nResults.\n\n## Discussion\n\nDiscussion.\n\n"
            "## Limitations\n\nLimitations.\n\n## Conclusion\n\nConclusion.",
        ]
    )

    _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _CitationMandatePromptManager()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="Outline poison [foreign2024paper] and \\cite{foreign2024paper}.",
        citation_repair_claims=(
            {
                "section": "Related Work",
                "claim_text": "Bounded evidence claim.",
                "cite_key": "smith2024deep",
            },
        ),
        part_citation_instructions={
            "part-1": (
                "FINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
                "- CLAIM planned-claim-001 (section: Related Work)\n"
                "  Required citation key: [smith2024deep]\n"
                "  Retained abstract evidence:\n"
                "  - ev-1: \"Bounded evidence.\"\n"
                "CITATION RULES:\n"
                "- Cite every required key above at least once using exact [cite_key] syntax."
            ),
            "part-2": (
                "FINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
                "No citation authority is assigned to this writing part.\n"
                "CITATION RULES:\n- Do not use any citation marker in this part."
            ),
            "part-3": (
                "FINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
                "No citation authority is assigned to this writing part.\n"
                "CITATION RULES:\n- Do not use any citation marker in this part."
            ),
        },
    )

    prompts = ["\n".join(message["content"] for message in call) for call in llm.calls]
    assert "Required citation key: [smith2024deep]" in prompts[0]
    assert "foreign2024paper" not in prompts[0]
    for prompt in prompts[1:]:
        assert "smith2024deep" not in prompt
        assert "foreign2024paper" not in prompt
        assert "Cite every required key above" not in prompt
        assert "No citation authority is assigned to this writing part." in prompt
        assert "Do not use any citation marker in this part." in prompt
        assert "MUST cite at least 3-5" not in prompt
        assert "CITE 3-5 papers here!" not in prompt
    for system in llm.systems:
        assert "GENERAL CITATION MANDATE" in system
        assert "foreign2024paper" not in system
        assert "SECTION-SCOPED CITATION OVERRIDE" in system
        assert system.index("SECTION-SCOPED CITATION OVERRIDE") > system.index(
            "GENERAL CITATION MANDATE"
        )
        assert "override every earlier general citation requirement" in system


def test_heading_authority_is_exact_for_related_work_only() -> None:
    plan = {
        "claims": [
            {
                "claim_id": f"planned-claim-{index:03d}",
                "claim_text": f"Bounded claim {index}.",
                "section_path": ["Related Work"],
                "planned_citations": [
                    {
                        "cite_key": f"author{index}2024work",
                        "evidence_excerpt_ids": [f"ev-{index}"],
                    }
                ],
            }
            for index in range(1, 20)
        ]
    }
    cards = [
        {
            "cite_key": f"author{index}2024work",
            "extraction_status": "success",
            "evidence_excerpts": [
                {
                    "excerpt_id": f"ev-{index}",
                    "excerpt_text": f"Evidence {index}.",
                }
            ],
        }
        for index in range(1, 20)
    ]

    authority = build_heading_citation_writer_instructions_from_authority(
        plan,
        cards,
        heading_names=("Introduction", "Related Work", "Method"),
    )

    assert all(f"author{index}2024work" not in authority["Introduction"] for index in range(1, 20))
    assert all(f"author{index}2024work" in authority["Related Work"] for index in range(1, 20))
    assert all(f"author{index}2024work" not in authority["Method"] for index in range(1, 20))
    assert "Cite every required key above" not in authority["Introduction"]
    assert "Cite every required key above" not in authority["Method"]


def test_stage17_heading_writer_isolates_related_work_authority() -> None:
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.",
            "## Related Work\n\nPrior work [smith2024deep].",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )
    authority = {
        heading: "No citation authority is assigned to this writing part."
        for heading in (
            "Abstract", "Introduction", "Method", "Experiments", "Results",
            "Discussion", "Limitations", "Conclusion",
        )
    }
    authority["Related Work"] = (
        "FINAL CITATION PLAN (THE ONLY CITATION AUTHORITY):\n"
        "Required citation key: [smith2024deep]\n"
        "Retained abstract evidence: Evidence.\n"
        "CITATION RULES:\n- Cite every required key above at least once."
    )

    _write_paper_sections(
        llm=cast(Any, llm), pm=cast(Any, _PromptManagerStub()),
        preamble="", topic_constraint="", exp_metrics_instruction="",
        citation_instruction="", outline="Poison [foreign2024paper].",
        citation_repair_claims=(
            {"section": "Related Work", "claim_text": "Bounded claim.", "cite_key": "smith2024deep"},
        ),
        heading_citation_instructions=authority,
    )

    prompts = ["\n".join(message["content"] for message in call) for call in llm.calls]
    assert len(prompts) == 4
    assert "smith2024deep" not in prompts[0]
    assert "smith2024deep" in prompts[1]
    assert "smith2024deep" not in prompts[2]
    assert "smith2024deep" not in prompts[3]
    assert all("foreign2024paper" not in prompt for prompt in prompts)


def test_stage17_repair_removes_foreign_citations_before_regeneration() -> None:
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
            "## Introduction\n\nI.\n\n## Related Work\n\nR. [smith2024deep]",
            "## Method\n\nM. [foreign2024paper] and \\cite{foreign2024paper}.\n\n"
            "## Results\n\nWrong section.",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )

    _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
        citation_repair_claims=(
            {
                "section": "Related Work",
                "claim_text": "Bounded evidence claim.",
                "cite_key": "smith2024deep",
            },
        ),
        part_citation_instructions={
            "part-1": "Related Work authority [smith2024deep].",
            "part-2": "No citation authority is assigned to this writing part.",
            "part-3": "No citation authority is assigned to this writing part.",
        },
    )

    repair_prompt = "\n".join(message["content"] for message in llm.calls[2])
    assert "foreign2024paper" not in repair_prompt
    assert "Remove every unauthorized citation marker." in repair_prompt
    assert "None. Do not add citation markers." in repair_prompt


def test_stage17_section_authority_overrides_real_ml_system_citation_mandates() -> None:
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
            "## Introduction\n\nI.\n\n## Related Work\n\nR.",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )

    _write_paper_sections(
        llm=cast(Any, llm),
        pm=PromptManager(domain="ml"),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="Outline [foreign2024paper].",
        part_citation_instructions={
            part: "No citation authority is assigned to this writing part."
            for part in ("part-1", "part-2", "part-3")
        },
    )

    assert "CITE ORIGINAL PAPERS" in llm.systems[1]
    for system in llm.systems:
        assert "foreign2024paper" not in system
        assert system.index("SECTION-SCOPED CITATION OVERRIDE") > system.index(
            "CITE ORIGINAL PAPERS"
        )
        assert "No citation authority is assigned to this writing part." in system


def test_stage17_part_contract_rejects_extra_major_section() -> None:
    text = (
        "## A Paper Title\n\nTitle body.\n\n## Abstract\n\nAbstract.\n\n"
        "## Introduction\n\nIntro.\n\n## Related Work\n\nPrior.\n\n"
        "## Method\n\nNot owned by part 1.\n"
    )
    violations = _validate_paper_part_sections(
        text,
        expected_major_sections=("Abstract", "Introduction", "Related Work"),
        title_slot=True,
    )
    assert violations == ("section_part_major_sequence_mismatch",)


def test_stage17_part_contract_rejects_reserved_title_slot() -> None:
    text = (
        "## Abstract\n\nNot a title.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\nR.\n"
    )
    assert "section_part_title_invalid" in _validate_paper_part_sections(
        text,
        expected_major_sections=("Abstract", "Introduction", "Related Work"),
        title_slot=True,
    )


def test_stage17_part_contract_rejects_out_of_order_sections() -> None:
    text = "## Experiments\n\nE.\n\n## Method\n\nM.\n"
    assert _validate_paper_part_sections(
        text,
        expected_major_sections=("Method", "Experiments"),
        title_slot=False,
    ) == ("section_part_major_sequence_mismatch",)


def test_stage17_part_contract_uses_commonmark_for_fenced_heading() -> None:
    text = (
        "## Method\n\n```markdown\n## Discussion\n```\n\n"
        "## Experiments\n\nSetup.\n"
    )
    assert _validate_paper_part_sections(
        text,
        expected_major_sections=("Method", "Experiments"),
        title_slot=False,
    ) == ()


def test_stage17_hep_conclusions_contract_is_distinct() -> None:
    text = "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Conclusions\n\nC.\n"
    assert _validate_paper_part_sections(
        text,
        expected_major_sections=("Results", "Discussion", "Conclusions"),
        title_slot=False,
    ) == ()
    assert "section_part_major_sequence_mismatch" in _validate_paper_part_sections(
        text,
        expected_major_sections=("Results", "Discussion", "Limitations", "Conclusion"),
        title_slot=False,
    )


def test_stage17_part_contract_regenerates_once_and_records_attempts(
    tmp_path: Path,
) -> None:
    llm = _SequentialLLM(
        [
            "## Method\n\nWrong part.",
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.\n\n## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )
    draft = _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
        stage_dir=tmp_path,
    )
    assert "## Conclusion" in draft
    assert len(llm.calls) == 4
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    assert len(report["parts"][0]["attempts"]) == 2
    assert report["parts"][0]["attempts"][0]["valid"] is False
    assert report["parts"][0]["attempts"][1]["valid"] is True


def test_stage17_cfs_prompts_are_split_and_section_scoped(tmp_path: Path) -> None:
    llm = _SequentialLLM(
        [
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.",
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )
    headings = (
        "Abstract",
        "Introduction",
        "Related Work",
        "Method",
        "Experiments",
        "Results",
        "Discussion",
        "Limitations",
        "Conclusion",
    )
    draft = _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="PREAMBLE_WITH_FOREIGN_FACT 999 runs",
        topic_constraint="TOPIC_WITH_FOREIGN_FACT 888 seeds",
        exp_metrics_instruction="LEGACY_GLOBAL_METRIC auprc=0.123",
        citation_instruction="",
        outline="OUTLINE_WITH_FOREIGN_FACT 777 observations",
        stage_dir=tmp_path,
        heading_citation_instructions={
            heading: "No citation authority assigned to this heading."
            for heading in headings
        },
        canonical_fact_sheet=_minimal_cfs(),
    )

    assert "## Conclusion" in draft
    assert len(llm.calls) == 4
    prompts = ["\n".join(item["content"] for item in call) for call in llm.calls]
    assert all("LEGACY_GLOBAL_METRIC" not in prompt for prompt in prompts)
    assert all("FOREIGN_FACT" not in prompt for prompt in prompts)
    assert "pipeline_validation" in prompts[0]
    assert '"seeds"' in prompts[0]
    assert '"counts"' in prompts[0]
    assert '"runtime"' not in prompts[0]
    assert '"primary_metric"' not in prompts[0]
    assert '"runtime"' in prompts[1]
    assert '"primary_metric"' not in prompts[1]
    assert '"primary_metric"' in prompts[2]
    assert "projection mode=" in prompts[2]
    assert '"runtime"' not in prompts[3]
    assert "projection mode=" not in prompts[3]
    assert '"seeds"' in prompts[3]
    assert '"variant_ids"' in prompts[3]


def test_stage17_structure_repair_inherits_group_cfs(tmp_path: Path) -> None:
    llm = _SequentialLLM(
        [
            "## Method\n\nWrong group.",
            "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n## Introduction\n\nI.\n\n## Related Work\n\nR.",
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.",
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )
    headings = (
        "Abstract", "Introduction", "Related Work", "Method", "Experiments",
        "Results", "Discussion", "Limitations", "Conclusion",
    )
    _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="",
        stage_dir=tmp_path,
        heading_citation_instructions={heading: "No citation authority." for heading in headings},
        canonical_fact_sheet=_minimal_cfs(),
    )
    repair_prompt = "\n".join(item["content"] for item in llm.calls[1])
    assert "canonical_fact_sheet" in repair_prompt
    assert "pipeline_validation" in repair_prompt


def test_stage17_citation_repair_is_deterministic_and_uses_no_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = (
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\nBounded prior work.\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )
    llm = _SequentialLLM(
        ["## Related Work\n\nBounded prior work [smith2024deep]."]
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._paper_writing.build_experiment_fact_closure_report",
        lambda *_args, **_kwargs: {"manuscript_numeric_values": []},
    )
    repaired = _repair_heading_citation_closure(
        llm=cast(Any, llm),
        run_dir=tmp_path,
        stage_dir=tmp_path,
        paper_text=paper,
        evidence=cast(Any, SimpleNamespace()),
        claims=(
            {
                "section": "Related Work",
                "claim_text": "Bounded prior work.",
                "cite_key": "smith2024deep",
            },
        ),
        heading_citation_instructions={
            "Related Work": "Required citation key: [smith2024deep]"
        },
        system="system",
        canonical_fact_sheet=_minimal_cfs(),
    )
    assert "[smith2024deep]" in repaired
    assert llm.calls == []


def test_stage17_missing_citation_is_inserted_deterministically_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = (
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\nExact bounded claim.\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )
    llm = _SequentialLLM([])
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._paper_writing.build_experiment_fact_closure_report",
        lambda *_args, **_kwargs: {"manuscript_numeric_values": []},
    )

    repaired = _repair_heading_citation_closure(
        llm=cast(Any, llm), run_dir=tmp_path, stage_dir=tmp_path,
        paper_text=paper, evidence=cast(Any, SimpleNamespace()),
        claims=({"section": "Related Work", "claim_text": "Exact bounded claim.",
                 "cite_key": "smith2024deep"},),
        heading_citation_instructions={"Related Work": "Required: [smith2024deep]"},
        system="system", canonical_fact_sheet=_minimal_cfs(),
    )

    assert "Exact bounded claim [smith2024deep]." in repaired
    assert llm.calls == []


def test_stage17_existing_marker_cannot_move_across_sentences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper = (
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\n"
        "Exact bounded claim. Different sentence [smith2024deep].\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage_impls._paper_writing.build_experiment_fact_closure_report",
        lambda *_args, **_kwargs: {"manuscript_numeric_values": []},
    )

    with pytest.raises(ValueError, match="anchor"):
        _repair_heading_citation_closure(
            llm=cast(Any, _SequentialLLM([])), run_dir=tmp_path, stage_dir=tmp_path,
            paper_text=paper, evidence=cast(Any, SimpleNamespace()),
            claims=({"section": "Related Work", "claim_text": "Exact bounded claim.",
                     "cite_key": "smith2024deep"},),
            heading_citation_instructions={"Related Work": "Required: [smith2024deep]"},
            system="system", canonical_fact_sheet=_minimal_cfs(),
        )


def test_stage17_duplicate_claim_sentence_is_not_a_repair_anchor(
    tmp_path: Path,
) -> None:
    paper = (
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\n"
        "Repeated bounded claim. Repeated bounded claim.\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )

    with pytest.raises(ValueError, match="not unique"):
        _repair_heading_citation_closure(
            llm=cast(Any, _SequentialLLM([])), run_dir=tmp_path, stage_dir=tmp_path,
            paper_text=paper, evidence=cast(Any, SimpleNamespace()),
            claims=({"section": "Related Work", "claim_text": "Repeated bounded claim.",
                     "cite_key": "smith2024deep"},),
            heading_citation_instructions={"Related Work": "Required: [smith2024deep]"},
            system="system", canonical_fact_sheet=_minimal_cfs(),
        )


def _citation_anchor_paper(sentence: str) -> str:
    return (
        "## Title\n\nPaper.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\n"
        f"{sentence}\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )


@pytest.mark.parametrize(
    ("claim_text", "paper_sentence"),
    [
        ("A  bounded claim.", "A bounded claim."),
        ("A\tbounded claim.", "A bounded claim."),
        ("A\N{NO-BREAK SPACE}bounded claim.", "A bounded claim."),
        ("A bounded claim!", "A bounded claim."),
    ],
    ids=["double-space", "tab", "nbsp", "punctuation"],
)
@pytest.mark.parametrize("existing_marker", [False, True], ids=["missing", "existing"])
def test_stage17_citation_repair_requires_exact_anchor_bytes(
    tmp_path: Path,
    claim_text: str,
    paper_sentence: str,
    existing_marker: bool,
) -> None:
    if existing_marker:
        paper_sentence = paper_sentence[:-1] + " [smith2024deep]."
    paper = _citation_anchor_paper(paper_sentence)

    with pytest.raises(ValueError, match="anchor"):
        _repair_heading_citation_closure(
            llm=cast(Any, _SequentialLLM([])), run_dir=tmp_path, stage_dir=tmp_path,
            paper_text=paper, evidence=cast(Any, SimpleNamespace()),
            claims=({"section": "Related Work", "claim_text": claim_text,
                     "cite_key": "smith2024deep"},),
            heading_citation_instructions={"Related Work": "Required: [smith2024deep]"},
            system="system", canonical_fact_sheet=_minimal_cfs(),
        )

    for name in (
        "citation_heading_repair_log.json",
        "paper_draft.md",
        "experiment_fact_closure_report.json",
        "citation_closure_report.json",
    ):
        assert not (tmp_path / name).exists()


@pytest.mark.parametrize("existing_marker", [False, True], ids=["missing", "existing"])
def test_stage17_citation_repair_accepts_exact_anchor_bytes(
    tmp_path: Path, existing_marker: bool
) -> None:
    sentence = "Exact bounded claim."
    if existing_marker:
        sentence = "Exact bounded claim [smith2024deep]."
    repaired = _repair_heading_citation_closure(
        llm=cast(Any, _SequentialLLM([])), run_dir=tmp_path, stage_dir=tmp_path,
        paper_text=_citation_anchor_paper(sentence),
        evidence=cast(Any, SimpleNamespace()),
        claims=({"section": "Related Work", "claim_text": "Exact bounded claim.",
                 "cite_key": "smith2024deep"},),
        heading_citation_instructions={"Related Work": "Required: [smith2024deep]"},
        system="system", canonical_fact_sheet=_minimal_cfs(),
    )

    assert "Exact bounded claim [smith2024deep]." in repaired


def test_stage17_citation_repair_rejects_non_marker_numeric_change(
) -> None:
    with pytest.raises(ValueError, match="marker-only"):
        _assert_marker_only_citation_delta(
            "Prior work reported 91.5%.",
            "Prior work reported 92.5% [smith2024deep].",
        )


def test_stage17_citation_repair_rejects_noncitation_bracket_replacement() -> None:
    with pytest.raises(ValueError, match="marker-only"):
        _assert_marker_only_citation_delta(
            "Claim [important context].",
            "Claim [smith2024deep].",
        )


@pytest.mark.parametrize(
    ("before", "after"),
    [("Claim.\n", "Claim.\n\n"), ("Claim.\n\n", "Claim.\n")],
)
def test_stage17_marker_only_delta_preserves_trailing_newlines(
    before: str, after: str
) -> None:
    with pytest.raises(ValueError, match="marker-only"):
        _assert_marker_only_citation_delta(before, after)


def test_stage17_full_paper_response_uses_isolated_bounded_repair(
    tmp_path: Path,
) -> None:
    full_paper = (
        "## Paper Title\n\nT.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\nR.\n\n"
        "## Method\n\nM.\n\n## Experiments\n\nE.\n\n"
        "## Results\n\nX.\n\n## Discussion\n\nD.\n\n"
        "## Limitations\n\nL.\n\n## Conclusion\n\nC."
    )
    repaired_part = (
        "## Paper Title\n\nT.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n"
        "## Related Work\n\nBounded evidence [smith2024deep]."
    )
    llm = _SequentialLLM(
        [
            full_paper,
            repaired_part,
            "## Method\n\nM.\n\n## Experiments\n\nE.",
            "## Results\n\nR.\n\n## Discussion\n\nD.\n\n"
            "## Limitations\n\nL.\n\n## Conclusion\n\nC.",
        ]
    )
    draft = _write_paper_sections(
        llm=cast(Any, llm),
        pm=cast(Any, _PromptManagerStub()),
        preamble="PREAMBLE_SENTINEL",
        topic_constraint="",
        exp_metrics_instruction="",
        citation_instruction="",
        outline="OUTLINE_SENTINEL",
        stage_dir=tmp_path,
        citation_repair_claims=(
            {
                "section": "Related Work",
                "claim_text": "Bounded evidence claim.",
                "cite_key": "smith2024deep",
            },
        ),
    )

    assert repaired_part in draft
    assert len(llm.calls) == 4
    repair_prompt = "\n".join(message["content"] for message in llm.calls[1])
    assert "PREAMBLE_SENTINEL" not in repair_prompt
    assert "OUTLINE_SENTINEL" not in repair_prompt
    assert "Bounded evidence claim." in repair_prompt
    assert "[smith2024deep]" in repair_prompt
    assert full_paper in repair_prompt
    assert "Never output a complete paper" in llm.systems[1]
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    assert report["_diagnostic"] is True
    assert report["parts"][0]["attempts"][0]["observed_major_sections"][-1] == (
        "Conclusion"
    )


def test_stage17_full_paper_repair_is_not_deterministically_trimmed(
    tmp_path: Path,
) -> None:
    full_paper = (
        "## Paper Title\n\nT.\n\n## Abstract\n\nA.\n\n"
        "## Introduction\n\nI.\n\n## Related Work\n\nR.\n\n"
        "## Method\n\nM.\n\n## Results\n\nR."
    )
    llm = _SequentialLLM([full_paper, full_paper])
    with pytest.raises(PaperSectionContractError, match="part-1"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
        )
    assert len(llm.calls) == 2
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    assert report["parts"][0]["attempts"][-1]["valid"] is False
    assert "Method" in report["parts"][0]["attempts"][-1][
        "observed_major_sections"
    ]


def test_stage17_part_contract_fails_after_one_regeneration(
    tmp_path: Path,
) -> None:
    llm = _SequentialLLM(
        ["## Method\n\nWrong part.", "## Experiments\n\nStill wrong."]
    )
    with pytest.raises(PaperSectionContractError, match="part-1"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
        )
    assert len(llm.calls) == 2
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    assert len(report["parts"]) == 1
    assert report["parts"][0]["attempts"][-1]["valid"] is False


def test_stage17_initial_transport_failure_stops_before_later_parts(
    tmp_path: Path,
) -> None:
    class FailingLLM:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, _messages: object, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            raise RuntimeError("transport unavailable")

    llm = FailingLLM()
    with pytest.raises(PaperSectionContractError, match="part-1"):
        _write_paper_sections(
            llm=cast(Any, llm),
            pm=cast(Any, _PromptManagerStub()),
            preamble="",
            topic_constraint="",
            exp_metrics_instruction="",
            citation_instruction="",
            outline="",
            stage_dir=tmp_path,
        )
    assert llm.calls == 2
    report = json.loads(
        (tmp_path / "section_generation_report.json").read_text(encoding="utf-8")
    )
    assert len(report["parts"]) == 1
    assert report["parts"][0]["part"] == "part-1"
    assert report["parts"][0]["attempts"][0]["violations"] == [
        "section_part_transport_error:RuntimeError"
    ]


def test_stage18_prompt_contract_is_last_and_valid_output_passes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    reviews = """## Reviewer A

### Strengths
Clear scope.

### Weaknesses
Limited evidence.

### Actionable Revisions
1. Add uncertainty estimates.
"""
    llm = _SequentialLLM([reviews])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status == StageStatus.DONE
    prompt = "\n".join(message["content"] for message in llm.calls[0])
    assert prompt.rstrip().endswith("Do not emit any other markdown heading at any level.")
    report = json.loads(
        (stage_dir / "review_structure_report.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is True
    assert report["comment_count"] == 1
    _assert_fixture_canonical_binding(report)


def test_stage18_uses_one_canonical_snapshot_and_binds_review_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    shadow_draft = run_dir / "stage-17_v99"
    shadow_draft.mkdir()
    (shadow_draft / "paper_draft.md").write_text(
        "POISON SHADOW DRAFT", encoding="utf-8"
    )
    (shadow_draft / "draft_quality.json").write_text(
        '{"overall_warnings":["Canonical detection_f1 is 0.99"]}',
        encoding="utf-8",
    )
    poison_dir = run_dir / "stage-12" / "runs"
    poison_dir.mkdir(parents=True)
    (poison_dir / "poison.json").write_text(
        '{"metrics":{"poison_metric":999}}', encoding="utf-8"
    )
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="a" * 64,
        selected_result={"result_set_type": "stage12_baseline"},
        metric_observations={"safe_metric": ("0.5",)},
        structured_results={"metrics": {"safe_metric": "0.5"}},
        summary=MappingProxyType(
            {
                "metrics_summary": MappingProxyType(
                    {"safe_metric": MappingProxyType({"mean": "0.5"})}
                )
            }
        ),
        analysis_text="Canonical analysis.\n",
    )
    calls = 0

    def load_once(_run_dir: Path) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return evidence

    class CapturingPrompts(_PromptManagerStub):
        experiment_evidence = ""
        draft = ""

        def for_stage(self, *_args: object, **kwargs: object) -> SimpleNamespace:
            self.experiment_evidence = str(kwargs["experiment_evidence"])
            self.draft = str(kwargs["draft"])
            return super().for_stage(*_args, **kwargs)

    monkeypatch.setattr(_review_publish, "load_canonical_experiment_evidence", load_once)
    monkeypatch.setattr(
        _review_publish, "build_canonical_fact_sheet", lambda _evidence: None
    )
    prompts = CapturingPrompts()
    llm = _SequentialLLM(
        [
            "## Reviewer A\n\n### Strengths\nClear scope.\n\n"
            "### Weaknesses\nLimited evidence.\n\n"
            "### Actionable Revisions\n1. Add uncertainty estimates.\n"
        ]
    )

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, prompts),
    )

    assert result.status is StageStatus.DONE
    assert calls == 1
    assert "safe_metric" in prompts.experiment_evidence
    assert "poison_metric" not in prompts.experiment_evidence
    assert "POISON SHADOW DRAFT" not in prompts.draft
    assert all("detection_f1 is 0.99" not in message["content"] for message in llm.calls[0])
    report = json.loads(
        (stage_dir / "review_structure_report.json").read_text(encoding="utf-8")
    )
    assert report["canonical_experiment_evidence_path"] == evidence.manifest_path
    assert report["canonical_experiment_evidence_sha256"] == evidence.manifest_sha256


def test_stage18_invalid_canonical_evidence_cleans_outputs_before_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    for name in ("reviews.md", "review_structure_report.json"):
        (stage_dir / name).write_text("stale\n", encoding="utf-8")

    def invalid_evidence(_run_dir: Path) -> None:
        raise _review_publish.CanonicalExperimentEvidenceError("invalid snapshot")

    monkeypatch.setattr(
        _review_publish, "load_canonical_experiment_evidence", invalid_evidence
    )
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []
    assert not (stage_dir / "reviews.md").exists()
    assert not (stage_dir / "review_structure_report.json").exists()


def test_stage18_rejects_symlinked_stage17_directory_before_llm(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    external_stage17 = tmp_path / "external-stage-17"
    external_stage17.mkdir()
    (external_stage17 / "paper_draft.md").write_text(
        "## Title\n\nExternal draft.\n", encoding="utf-8"
    )
    (run_dir / "stage-17").symlink_to(external_stage17, target_is_directory=True)
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []


def test_stage18_rejects_symlinked_stage17_draft_before_llm(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    external_draft = tmp_path / "external-draft.md"
    external_draft.write_text("## Title\n\nExternal draft.\n", encoding="utf-8")
    draft_path = run_dir / "stage-17" / "paper_draft.md"
    draft_path.unlink()
    draft_path.symlink_to(external_draft)
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []


def test_stage18_rejects_non_utf8_stage17_draft_before_llm(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    (run_dir / "stage-17" / "paper_draft.md").write_bytes(b"\xff\xfe")
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []


def test_stage18_rejects_closure_for_different_draft_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    changed = "## Title\n\nChanged after the draft snapshot.\n"

    def mutate_then_report(_run_dir: Path, *_args: object, **_kwargs: object) -> dict[str, str]:
        draft_path = run_dir / "stage-17" / "paper_draft.md"
        draft_path.write_text(changed, encoding="utf-8")
        return {"paper_sha256": hashlib.sha256(changed.encode("utf-8")).hexdigest()}

    monkeypatch.setattr(
        _review_publish,
        "validate_experiment_fact_closure_report",
        mutate_then_report,
    )
    monkeypatch.setattr(
        _review_publish,
        "validate_citation_closure_report",
        mutate_then_report,
    )
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []
    assert "differs from closure-bound paper" in (result.error or "")


def test_stage18_accessor_lock_error_returns_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    monkeypatch.setattr(
        _review_publish,
        "load_canonical_experiment_evidence",
        lambda _run_dir: (_ for _ in ()).throw(
            RuntimeError("canonical_evidence_generation_locked")
        ),
    )
    llm = _SequentialLLM(["unexpected"])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == []
    assert "canonical_evidence_generation_locked" in (result.error or "")


def test_stage18_passes_one_snapshot_to_both_closure_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    evidence = SimpleNamespace(
        manifest_path="canonical_experiment_evidence.json",
        manifest_sha256="e" * 64,
        selected_result={},
        metric_observations={},
        structured_results={},
        summary={},
        analysis_text="",
    )
    fact_snapshots: list[object] = []
    citation_snapshots: list[object] = []

    monkeypatch.setattr(
        _review_publish, "load_canonical_experiment_evidence", lambda _run_dir: evidence
    )
    monkeypatch.setattr(
        _review_publish, "build_canonical_fact_sheet", lambda _evidence: None
    )

    def fact_closure(
        _run_dir: Path, *, evidence: object | None = None
    ) -> dict[str, str]:
        fact_snapshots.append(evidence)
        draft_bytes = (run_dir / "stage-17" / "paper_draft.md").read_bytes()
        return {
            "paper_sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "canonical_experiment_evidence_path": "canonical_experiment_evidence.json",
            "canonical_experiment_evidence_sha256": "e" * 64,
        }

    def citation_closure(
        _run_dir: Path, _config: object, *, evidence: object | None = None
    ) -> dict[str, str]:
        citation_snapshots.append(evidence)
        draft_bytes = (run_dir / "stage-17" / "paper_draft.md").read_bytes()
        return {
            "paper_sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "structure_report_sha256": hashlib.sha256(
                (run_dir / "stage-17" / "paper_structure_report.json").read_bytes()
            ).hexdigest(),
            "experiment_fact_closure_report_sha256": hashlib.sha256(
                (run_dir / "stage-17" / "experiment_fact_closure_report.json").read_bytes()
            ).hexdigest(),
        }

    monkeypatch.setattr(
        _review_publish, "validate_experiment_fact_closure_report", fact_closure
    )
    monkeypatch.setattr(
        _review_publish, "validate_citation_closure_report", citation_closure
    )
    llm = _SequentialLLM(
        [
            "## Reviewer A\n\n### Strengths\nClear scope.\n\n"
            "### Weaknesses\nLimited evidence.\n\n"
            "### Actionable Revisions\n1. Add uncertainty estimates.\n"
        ]
    )

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.DONE
    assert fact_snapshots == [evidence]
    assert citation_snapshots == [evidence]


def test_stage18_repair_transport_failure_cleans_unbound_artifacts(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    excessive = """## Reviewer A

### Strengths
Clear scope.

### Weaknesses
Limited evidence.

### Actionable Revisions
1. Ensure at least 20 relevant references.
"""

    class RepairTransportFailure:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, _messages: object, **_kwargs: object) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transport unavailable")
            return SimpleNamespace(content=excessive)

    llm = RepairTransportFailure()
    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert result.artifacts == ()
    assert llm.calls == 2
    assert not (stage_dir / "reviews.md").exists()
    assert not (stage_dir / "review_structure_report.json").exists()


def test_stage18_evidence_renderer_has_no_legacy_source_selection() -> None:
    source = inspect.getsource(_review_publish._collect_experiment_evidence)
    for forbidden in (
        "run_dir",
        "_read_prior_artifact",
        "refinement_log",
        "stage-*/runs",
        ".glob(",
    ):
        assert forbidden not in source

    stage_source = inspect.getsource(_review_publish._execute_peer_review)
    for forbidden in (
        "_find_prior_file",
        "draft_quality.json",
        "_get_evolution_overlay",
        "_read_prior_artifact",
    ):
        assert forbidden not in stage_source

    draft_source = inspect.getsource(_review_publish._read_bound_stage17_draft)
    for forbidden in (
        "_find_prior_file",
        "_read_prior_artifact",
        ".glob(",
        ".rglob(",
        ".iterdir(",
        "os.walk",
    ):
        assert forbidden not in draft_source


def test_stage18_unknown_subsection_fixture_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    fixture = Path(__file__).parent / "fixtures" / "stage18_reviews_unknown_subsection.md"
    llm = _SequentialLLM([fixture.read_text(encoding="utf-8")])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status == StageStatus.FAILED
    report = json.loads(
        (stage_dir / "review_structure_report.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is False
    assert report["source_reviews_sha256"] == hashlib.sha256(
        fixture.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    _assert_fixture_canonical_binding(report)
    assert "unknown_review_subsection" in {
        issue["code"] for issue in report["issues"]
    }


def test_stage18_no_llm_fallback_obeys_the_contract(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
    )

    assert result.status == StageStatus.DONE
    report = json.loads(
        (stage_dir / "review_structure_report.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is True
    assert report["comment_count"] == 4
    _assert_fixture_canonical_binding(report)


def test_stage18_repairs_citation_requirement_above_effective_target(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    excessive = """## Reviewer A

### Strengths
Clear scope.

### Weaknesses
Limited evidence.

### Actionable Revisions
1. Ensure at least 20 relevant references.
"""
    repaired = excessive.replace("20", "15")
    llm = _SequentialLLM([excessive, repaired])
    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )
    assert result.status == StageStatus.DONE
    assert len(llm.calls) == 2
    assert "effective target of 15" in llm.calls[1][0]["content"]
    report = json.loads((stage_dir / "review_structure_report.json").read_text())
    _assert_fixture_canonical_binding(report)


def test_stage18_fails_when_citation_requirement_remains_above_target(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    excessive = """## Reviewer A

### Strengths
Clear scope.

### Weaknesses
Limited evidence.

### Actionable Revisions
1. Include at least twenty five unique citations.
"""
    llm = _SequentialLLM([excessive, excessive])
    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )
    assert result.status == StageStatus.FAILED
    report = json.loads((stage_dir / "review_structure_report.json").read_text())
    assert report["issues"][0]["code"] == "citation_count_policy_exceeded"
    _assert_fixture_canonical_binding(report)


def test_stage18_repair_structure_failure_binds_canonical_report(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    stage_dir = run_dir / "stage-18"
    stage_dir.mkdir(parents=True)
    _write_draft(run_dir)
    excessive = """## Reviewer A

### Strengths
Clear scope.

### Weaknesses
Limited evidence.

### Actionable Revisions
1. Ensure at least 20 relevant references.
"""
    malformed_repair = """## Reviewer A

### Strengths
Clear scope.

### Required Revisions
1. Add uncertainty estimates.
"""
    llm = _SequentialLLM([excessive, malformed_repair])

    result = _execute_peer_review(
        stage_dir,
        run_dir,
        cast(Any, _config()),
        cast(Any, None),
        llm=cast(Any, llm),
        prompts=cast(Any, _PromptManagerStub()),
    )

    assert result.status is StageStatus.FAILED
    assert len(llm.calls) == 2
    report = json.loads((stage_dir / "review_structure_report.json").read_text())
    assert report["valid"] is False
    _assert_fixture_canonical_binding(report)
    assert "unknown_review_subsection" in {
        issue["code"] for issue in report["issues"]
    }


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("At least fifteen unique citations are required.", 14),
        ("Ensure at least twenty five relevant references.", 20),
        ("Increase references to 30.", 15),
        ("Raise the citation count above twenty-five.", 20),
        ("30 references are required.", 15),
        ("Twenty five or more sources should be included.", 20),
    ],
)
def test_citation_count_detector_does_not_swallow_qualifiers(
    text: str, target: int
) -> None:
    ledger = SimpleNamespace(
        comments=(
            SimpleNamespace(
                category="actionable_revision",
                exact_text=text,
                comment_id="comment-001",
            ),
        )
    )
    assert _citation_count_policy_violations(ledger, target) == ["comment-001"]
