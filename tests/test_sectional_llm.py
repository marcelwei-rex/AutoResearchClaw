from __future__ import annotations

import json
from typing import Any, Callable
from decimal import Decimal

import pytest

from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline.manuscript_sections import parse_manuscript
from researchclaw.pipeline.sectional_llm import (
    LLMSectionalRevisionProvider,
    _parse_json_object,
)
from researchclaw.pipeline.sectional_revision import extract_review_ledger
from researchclaw.pipeline.sectional_validation import SectionValidationContext


DRAFT = """## Method

The baseline score was 0.475 using \\cite{smith2024}.

## Results

SECRET_OTHER_SECTION must never enter the Method writer prompt.
"""

REVIEWS = """## Reviewer A

### Actionable Revisions
1. Clarify how the baseline score is reported.
"""


class _FakeLLM:
    def __init__(self, responder: Callable[[dict[str, Any], dict[str, Any]], object]):
        self.responder = responder
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[0]["content"])
        content = self.responder(payload, kwargs)
        if isinstance(content, LLMResponse):
            return content
        if not isinstance(content, str):
            content = json.dumps(content)
        return LLMResponse(content=content, model=str(kwargs["model"]))


def _inputs():
    document = parse_manuscript(DRAFT)
    ledger = extract_review_ledger(REVIEWS, source_path="stage-18/reviews.md")
    method = next(section for section in document.sections if section.title == "Method")
    comment = ledger.comments[0]
    context = SectionValidationContext(
        document=document,
        section_id=method.section_id,
        attempt=1,
        allowed_citation_keys=frozenset({"smith2024"}),
        grounded_numeric_values=(Decimal("0.475"),),
        required_comment_ids=(comment.comment_id,),
    )
    return document, ledger, method, comment, context


def _provider(fake: _FakeLLM) -> LLMSectionalRevisionProvider:
    return LLMSectionalRevisionProvider(
        llm=fake,  # type: ignore[arg-type]
        writer_model="writer-model",
        critic_model="critic-model",
    )


def _grounded_provider(fake: _FakeLLM) -> LLMSectionalRevisionProvider:
    cfs = {
        "schema_version": 1,
        "claim_scope": "pipeline_validation",
        "dataset_origin": "synthetic",
        "bound_labels": {"dataset": "ISCAS-85"},
        "conditions": (
            {"id": "proposed", "role": "primary"},
            {"id": "baseline", "role": "comparator"},
        ),
        "seeds": (0, 1, 2),
        "circuit_families": ("c1355",),
        "variant_ids": ("c1355_v1",),
        "variants_per_family": 1,
        "counts": {
            "observations": 6,
            "observations_per_condition": 3,
            "observations_per_condition_seed": 1,
            "invocations": 2,
        },
        "metric_keys": ("auprc",),
        "primary_metric": {
            "condition": "proposed",
            "key": "auprc",
            "direction": "maximize",
            "aggregation": "mean",
            "value": Decimal("0.665"),
        },
        "condition_aggregates": (
            {
                "condition": "proposed",
                "n_seeds": 3,
                "metrics": {
                    "auprc": {
                        "mean": Decimal("0.665"),
                        "std": Decimal("0.01"),
                        "min": Decimal("0.65"),
                        "max": Decimal("0.68"),
                    }
                },
            },
        ),
        "per_seed_aggregates": (),
        "scale": {"n_total_min": 100, "n_total_max": 100},
        "runtime": {
            "device": "cpu",
            "python": "3.11",
            "packages": {"torch": "2.1.0"},
            "torch_deterministic_algorithms": True,
            "torch_num_threads": 1,
        },
        "derived_facts": {},
        "provenance": {"observation_identities_sha256": "a" * 64},
        "observation_rows": (),
    }
    return LLMSectionalRevisionProvider(
        llm=fake,  # type: ignore[arg-type]
        writer_model="writer-model",
        critic_model="critic-model",
        canonical_fact_sheet=cfs,
    )


def test_planner_uses_explicit_writer_model_and_strict_json() -> None:
    document, ledger, method, comment, _ = _inputs()

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        assert kwargs["model"] == "writer-model"
        assert kwargs["json_mode"] is True
        assert kwargs["temperature"] == 0
        assert len(payload["sections"]) == 2
        return {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [
                {
                    "comment_id": comment.comment_id,
                    "target_section_ids": [method.section_id],
                    "disposition": "assigned",
                    "reason": None,
                }
            ],
        }

    plan = _provider(_FakeLLM(respond)).build_plan(ledger=ledger, document=document)
    assert plan["assignments"][0]["comment_id"] == comment.comment_id


def test_planner_receives_full_cfs_without_projection_and_out_of_scope_rule() -> None:
    document, ledger, method, comment, _ = _inputs()

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        context = payload["canonical_grounding_context"]
        assert '"runtime"' in context
        assert '"condition_aggregates"' in context
        assert "projection mode=" not in context
        assert "not_actionable_with_reason" in payload["grounding_policy"]
        return {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [
                {
                    "comment_id": comment.comment_id,
                    "target_section_ids": [],
                    "disposition": "not_actionable_with_reason",
                    "reason": "Ten seeds exceed the canonical three-seed contract.",
                }
            ],
        }

    result = _grounded_provider(_FakeLLM(respond)).build_plan(
        ledger=ledger, document=document
    )
    assert result["assignments"][0]["disposition"] == "not_actionable_with_reason"


def test_writer_and_critic_receive_only_target_section_cfs_view() -> None:
    _, _, method, comment, context = _inputs()
    calls: list[dict[str, Any]] = []

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        calls.append(payload)
        grounding = payload["canonical_grounding_context"]
        assert '"runtime"' in grounding
        assert '"condition_aggregates"' not in grounding
        assert "0.665" not in grounding
        if "grounded_numeric_values" in payload:
            assert payload["grounded_numeric_values"] == []
        assert "not introduce experiments" in payload["grounding_policy"]
        if "deterministic_validator_codes" in payload:
            return {
                "schema_version": 1,
                "comment_id": comment.comment_id,
                "section_id": method.section_id,
                "attempt_id": "attempt-1",
                "verdict": "resolved",
                "reason": "The bounded clarification is present.",
            }
        return {
            "schema_version": 1,
            "section_id": method.section_id,
            "revised_body": method.body,
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "not_addressed",
                    "reason": "The requested extra experiment is outside the CFS.",
                }
            ],
        }

    provider = _grounded_provider(_FakeLLM(respond))
    provider.propose(
        section=method, comments=(comment,), attempt=1, context=context
    )
    provider.assess(
        comment=comment,
        section=method,
        original_body=method.body,
        revised_body=method.body,
        attempt_id="attempt-1",
        validator_codes=(),
    )
    assert len(calls) == 2


def test_generic_provider_preserves_exact_legacy_system_prompts() -> None:
    document, ledger, method, comment, context = _inputs()
    systems: list[str] = []

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        systems.append(str(kwargs["system"]))
        assert "canonical_grounding_context" not in payload
        assert "grounding_policy" not in payload
        if "sections" in payload:
            return {
                "schema_version": 1,
                "planner_version": 1,
                "source_paper_sha256": document.source_sha256,
                "source_reviews_sha256": ledger.source_reviews_sha256,
                "section_model_version": 1,
                "assignments": [
                    {
                        "comment_id": comment.comment_id,
                        "target_section_ids": [method.section_id],
                        "disposition": "assigned",
                        "reason": None,
                    }
                ],
            }
        if "deterministic_validator_codes" in payload:
            return {
                "schema_version": 1,
                "comment_id": comment.comment_id,
                "section_id": method.section_id,
                "attempt_id": "attempt-1",
                "verdict": "resolved",
                "reason": "Resolved.",
            }
        return {
            "schema_version": 1,
            "section_id": method.section_id,
            "revised_body": method.body,
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "not_addressed",
                    "reason": "Unchanged.",
                }
            ],
        }

    provider = _provider(_FakeLLM(respond))
    provider.build_plan(ledger=ledger, document=document)
    provider.propose(section=method, comments=(comment,), attempt=1, context=context)
    provider.assess(
        comment=comment,
        section=method,
        original_body=method.body,
        revised_body=method.body,
        attempt_id="attempt-1",
        validator_codes=(),
    )

    assert systems == [
        "You are a bounded manuscript revision planner.\n"
        "Map every review comment ID exactly once to existing section IDs, or mark it\n"
        "unresolved/not_actionable_with_reason. Never invent IDs, headings, or evidence.\n"
        "Return only the requested JSON object.",
        "You revise exactly one existing manuscript section body.\n"
        "Do not emit headings, preambles, JSON commentary, new citations, or new numeric\n"
        "claims outside the supplied allowlists. Address only the supplied review\n"
        "comments. Return only the requested JSON object.",
        "You are an independent manuscript-resolution critic.\n"
        "Assess one review comment against one original/revised section pair and the\n"
        "deterministic validator result. Do not rewrite text. Return only the requested\n"
        "JSON object.",
    ]


def test_results_writer_receives_results_numeric_authority() -> None:
    document, _, _, comment, context = _inputs()
    results = next(section for section in document.sections if section.title == "Results")

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        assert "0.665" in payload["grounded_numeric_values"]
        return {
            "schema_version": 1,
            "section_id": results.section_id,
            "revised_body": results.body,
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "not_addressed",
                    "reason": "No bounded change required.",
                }
            ],
        }

    results_context = SectionValidationContext(
        document=document,
        section_id=results.section_id,
        attempt=1,
        allowed_citation_keys=context.allowed_citation_keys,
        grounded_numeric_values=(Decimal("0.475"),),
        required_comment_ids=(comment.comment_id,),
    )
    _grounded_provider(_FakeLLM(respond)).propose(
        section=results,
        comments=(comment,),
        attempt=1,
        context=results_context,
    )


def test_planner_rejects_unknown_top_level_fields() -> None:
    document, ledger, _, _, _ = _inputs()
    fake = _FakeLLM(
        lambda payload, kwargs: {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [],
            "unexpected": True,
        }
    )
    provider = _provider(fake)
    with pytest.raises(RuntimeError, match="planner schema_error after 2 calls"):
        provider.build_plan(ledger=ledger, document=document)
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "response",
    (
        '{"schema_version": 1} trailing prose',
        "```json\n{\"schema_version\": 1}\n```junk",
        "[]",
    ),
)
def test_json_parser_rejects_non_bounded_responses(response: str) -> None:
    with pytest.raises(RuntimeError):
        _parse_json_object(response)


def test_json_parser_accepts_one_complete_fenced_object() -> None:
    assert _parse_json_object("```json\n{\"schema_version\": 1}\n```") == {
        "schema_version": 1
    }


def test_provider_rejects_truncated_but_valid_json_response() -> None:
    document, ledger, _, _, _ = _inputs()
    fake = _FakeLLM(
        lambda payload, kwargs: LLMResponse(
            content=json.dumps(
                {
                    "schema_version": 1,
                    "planner_version": 1,
                    "source_paper_sha256": document.source_sha256,
                    "source_reviews_sha256": ledger.source_reviews_sha256,
                    "section_model_version": 1,
                    "assignments": [],
                }
            ),
            model="writer-model",
            truncated=True,
        )
    )
    with pytest.raises(RuntimeError, match="truncated"):
        _provider(fake).build_plan(ledger=ledger, document=document)


def test_empty_planner_response_gets_one_bounded_repair() -> None:
    document, ledger, method, comment, _ = _inputs()
    responses: list[object] = [
        LLMResponse(content="", model="writer-model", finish_reason="length"),
        {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [
                {
                    "comment_id": comment.comment_id,
                    "target_section_ids": [method.section_id],
                    "disposition": "assigned",
                    "reason": None,
                }
            ],
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))
    provider = _provider(fake)

    plan = provider.build_plan(ledger=ledger, document=document)

    assert plan["assignments"][0]["comment_id"] == comment.comment_id
    assert len(fake.calls) == 2
    assert [row["error_category"] for row in provider.diagnostic_records] == [
        "empty_response",
        "success",
    ]
    assert all(row["role"] == "planner" for row in provider.diagnostic_records)


def test_second_empty_planner_response_fails_without_third_call() -> None:
    document, ledger, _, _, _ = _inputs()
    fake = _FakeLLM(
        lambda payload, kwargs: LLMResponse(
            content="",
            model="writer-model",
            finish_reason="length",
        )
    )
    provider = _provider(fake)

    with pytest.raises(RuntimeError, match="planner.*empty_response"):
        provider.build_plan(ledger=ledger, document=document)

    assert len(fake.calls) == 2
    assert len(provider.diagnostic_records) == 2


def test_truncated_planner_response_gets_one_bounded_repair() -> None:
    document, ledger, _, _, _ = _inputs()
    responses: list[object] = [
        LLMResponse(
            content='{"schema_version": 1',
            model="writer-model",
            finish_reason="length",
            truncated=True,
        ),
        {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [],
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))
    provider = _provider(fake)

    provider.build_plan(ledger=ledger, document=document)

    assert len(fake.calls) == 2
    assert [row["error_category"] for row in provider.diagnostic_records] == [
        "truncated_response",
        "success",
    ]


def test_second_malformed_planner_response_fails_without_third_call() -> None:
    document, ledger, _, _, _ = _inputs()
    fake = _FakeLLM(lambda payload, kwargs: "not json")
    provider = _provider(fake)

    with pytest.raises(RuntimeError, match="planner malformed_response"):
        provider.build_plan(ledger=ledger, document=document)

    assert len(fake.calls) == 2
    assert len(provider.diagnostic_records) == 2


def test_planner_root_schema_error_gets_one_bounded_repair() -> None:
    document, ledger, _, _, _ = _inputs()
    responses: list[object] = [
        {"schema_version": 1, "unexpected": True},
        {
            "schema_version": 1,
            "planner_version": 1,
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "section_model_version": 1,
            "assignments": [],
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))
    provider = _provider(fake)

    provider.build_plan(ledger=ledger, document=document)

    assert len(fake.calls) == 2
    assert [row["error_category"] for row in provider.diagnostic_records] == [
        "schema_error",
        "success",
    ]


def test_writer_receives_only_one_section_and_accounts_for_every_comment() -> None:
    _, _, method, comment, context = _inputs()

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        encoded = json.dumps(payload)
        assert "SECRET_OTHER_SECTION" not in encoded
        assert kwargs["model"] == "writer-model"
        return {
            "schema_version": 1,
            "section_id": method.section_id,
            "revised_body": (
                "\nThe baseline score was 0.475 using \\cite{smith2024}; "
                "this wording clarifies its reporting.\n"
            ),
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "addressed",
                    "reason": "The requested reporting clarification was added.",
                }
            ],
        }

    proposal = _provider(_FakeLLM(respond)).propose(
        section=method,
        comments=(comment,),
        attempt=1,
        context=context,
    )
    assert proposal.resolution_comment_ids == (comment.comment_id,)


def test_writer_rejects_missing_comment_resolution() -> None:
    _, _, method, comment, context = _inputs()
    fake = _FakeLLM(
        lambda payload, kwargs: {
            "schema_version": 1,
            "section_id": method.section_id,
            "revised_body": method.body,
            "resolutions": [],
        }
    )
    with pytest.raises(RuntimeError, match="every assigned comment"):
        _provider(fake).propose(
            section=method,
            comments=(comment,),
            attempt=1,
            context=context,
        )


def test_empty_writer_response_gets_one_bounded_repair() -> None:
    _, _, method, comment, context = _inputs()
    responses: list[object] = [
        "",
        {
            "schema_version": 1,
            "section_id": method.section_id,
            "revised_body": method.body + "\nClarified.\n",
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "addressed",
                    "reason": "Clarified.",
                }
            ],
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))
    provider = _provider(fake)

    provider.propose(
        section=method,
        comments=(comment,),
        attempt=1,
        context=context,
    )

    assert len(fake.calls) == 2
    records = provider.diagnostic_records
    assert [row["error_category"] for row in records] == [
        "empty_response",
        "success",
    ]
    assert all(row["role"] == "writer" for row in records)
    assert all(row["section_id"] == method.section_id for row in records)


def test_writer_repair_with_foreign_identity_is_rejected() -> None:
    _, _, method, comment, context = _inputs()
    responses: list[object] = [
        "",
        {
            "schema_version": 1,
            "section_id": "foreign-section",
            "revised_body": method.body,
            "resolutions": [
                {
                    "comment_id": comment.comment_id,
                    "writer_status": "addressed",
                    "reason": "Wrong section identity.",
                }
            ],
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))

    with pytest.raises(RuntimeError, match="section_id mismatches"):
        _provider(fake).propose(
            section=method,
            comments=(comment,),
            attempt=1,
            context=context,
        )
    assert len(fake.calls) == 2


def test_critic_uses_isolated_model_and_exact_identity() -> None:
    _, _, method, comment, _ = _inputs()

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> object:
        assert kwargs["model"] == "critic-model"
        assert len(payload["comment"]["exact_text"]) > 0
        return {
            "schema_version": 1,
            "comment_id": comment.comment_id,
            "section_id": method.section_id,
            "attempt_id": "attempt-1",
            "verdict": "resolved",
            "reason": "The revised sentence directly addresses the comment.",
        }

    fake = _FakeLLM(respond)
    assessment = _provider(fake).assess(
        comment=comment,
        section=method,
        original_body=method.body,
        revised_body=method.body + "\nClarified.\n",
        attempt_id="attempt-1",
        validator_codes=(),
    )
    assert assessment.context_isolated is True
    assert assessment.critic_model == "critic-model"
    assert fake.calls[0][0][0]["role"] == "user"


def test_critic_rejects_identity_claim_from_another_attempt() -> None:
    _, _, method, comment, _ = _inputs()
    fake = _FakeLLM(
        lambda payload, kwargs: {
            "schema_version": 1,
            "comment_id": comment.comment_id,
            "section_id": method.section_id,
            "attempt_id": "wrong-attempt",
            "verdict": "resolved",
            "reason": "Invalid identity claim.",
        }
    )
    with pytest.raises(RuntimeError, match="identity mismatches"):
        _provider(fake).assess(
            comment=comment,
            section=method,
            original_body=method.body,
            revised_body=method.body + "\nClarified.\n",
            attempt_id="attempt-1",
            validator_codes=(),
        )


def test_empty_critic_response_gets_one_bounded_repair() -> None:
    _, _, method, comment, _ = _inputs()
    responses: list[object] = [
        "",
        {
            "schema_version": 1,
            "comment_id": comment.comment_id,
            "section_id": method.section_id,
            "attempt_id": "attempt-1",
            "verdict": "resolved",
            "reason": "Resolved.",
        },
    ]
    fake = _FakeLLM(lambda payload, kwargs: responses.pop(0))
    provider = _provider(fake)

    provider.assess(
        comment=comment,
        section=method,
        original_body=method.body,
        revised_body=method.body + "\nClarified.\n",
        attempt_id="attempt-1",
        validator_codes=(),
    )

    assert len(fake.calls) == 2
    records = provider.diagnostic_records
    assert [row["error_category"] for row in records] == [
        "empty_response",
        "success",
    ]
    assert all(row["role"] == "critic" for row in records)
    assert all(row["comment_id"] == comment.comment_id for row in records)


def test_provider_rejects_same_writer_and_critic_model() -> None:
    fake = _FakeLLM(lambda payload, kwargs: {})
    with pytest.raises(ValueError, match="must differ"):
        LLMSectionalRevisionProvider(
            llm=fake,  # type: ignore[arg-type]
            writer_model="same-model",
            critic_model="same-model",
        )
