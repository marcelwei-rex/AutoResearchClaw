"""Bounded LLM adapter for the Stage 19 sectional execution protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.canonical_experiment_evidence import canonical_decimal
from researchclaw.pipeline.manuscript_sections import ManuscriptDocument, ManuscriptSection
from researchclaw.pipeline.sectional_execution import (
    ResolutionAssessment,
    SectionProposal,
)
from researchclaw.pipeline.sectional_revision import ReviewComment, ReviewLedger
from researchclaw.pipeline.sectional_validation import SectionValidationContext


_PLANNER_SYSTEM = """You are a bounded manuscript revision planner.
Map every review comment ID exactly once to existing section IDs, or mark it
unresolved/not_actionable_with_reason. Never invent IDs, headings, or evidence.
Return only the requested JSON object."""

_WRITER_SYSTEM = """You revise exactly one existing manuscript section body.
Do not emit headings, preambles, JSON commentary, new citations, or new numeric
claims outside the supplied allowlists. Address only the supplied review
comments. Return only the requested JSON object."""

_CRITIC_SYSTEM = """You are an independent manuscript-resolution critic.
Assess one review comment against one original/revised section pair and the
deterministic validator result. Do not rewrite text. Return only the requested
JSON object."""

_FORMAT_REPAIR_SUFFIX = """
Your previous response was empty, truncated, malformed, or had the wrong root
fields. Return one complete JSON object matching response_schema exactly. Do
not include prose or Markdown fences."""


@dataclass(frozen=True)
class _WriterResolution:
    comment_id: str
    writer_status: str
    reason: str


class LLMSectionalRevisionProvider:
    """Strict planner/writer/critic adapter with model and context isolation."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        writer_model: str,
        critic_model: str,
    ) -> None:
        self._llm = llm
        self.writer_model = writer_model.strip()
        self.critic_model = critic_model.strip()
        if not self.writer_model or not self.critic_model:
            raise ValueError("writer_model and critic_model are required")
        if self.writer_model == self.critic_model:
            raise ValueError("writer_model and critic_model must differ")
        self._diagnostic_records: list[dict[str, Any]] = []

    @property
    def diagnostic_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(record) for record in self._diagnostic_records)

    def build_plan(
        self,
        *,
        ledger: ReviewLedger,
        document: ManuscriptDocument,
    ) -> object:
        payload = {
            "source_paper_sha256": document.source_sha256,
            "source_reviews_sha256": ledger.source_reviews_sha256,
            "sections": [
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "path": list(section.path),
                }
                for section in document.sections
            ],
            "comments": [
                {
                    "comment_id": comment.comment_id,
                    "reviewer": comment.reviewer,
                    "category": comment.category,
                    "required": comment.required,
                    "exact_text": comment.exact_text,
                }
                for comment in ledger.comments
            ],
            "response_schema": {
                "schema_version": 1,
                "planner_version": 1,
                "source_paper_sha256": document.source_sha256,
                "source_reviews_sha256": ledger.source_reviews_sha256,
                "section_model_version": 1,
                "assignments": [
                    {
                        "comment_id": "existing comment ID",
                        "target_section_ids": ["existing section ID"],
                        "disposition": "assigned|unresolved|not_actionable_with_reason",
                        "reason": None,
                    }
                ],
            },
        }
        response = self._chat_json(
            role="planner",
            model=self.writer_model,
            system=_PLANNER_SYSTEM,
            payload=payload,
            max_tokens=8192,
            expected_keys={
                "schema_version",
                "planner_version",
                "source_paper_sha256",
                "source_reviews_sha256",
                "section_model_version",
                "assignments",
            },
        )
        _expect_keys(
            response,
            {
                "schema_version",
                "planner_version",
                "source_paper_sha256",
                "source_reviews_sha256",
                "section_model_version",
                "assignments",
            },
            "planner response",
        )
        assignments = response["assignments"]
        if not isinstance(assignments, list):
            raise RuntimeError("planner assignments must be a list")
        for index, assignment in enumerate(assignments):
            _expect_keys(
                assignment,
                {"comment_id", "target_section_ids", "disposition", "reason"},
                f"planner assignment {index}",
            )
        return response

    def propose(
        self,
        *,
        section: ManuscriptSection,
        comments: tuple[ReviewComment, ...],
        attempt: int,
        context: SectionValidationContext,
    ) -> SectionProposal:
        payload = {
            "section": {
                "section_id": section.section_id,
                "title": section.title,
                "path": list(section.path),
                "heading_source": section.heading_source,
                "body": section.body,
            },
            "comments": [
                {
                    "comment_id": comment.comment_id,
                    "required": comment.required,
                    "exact_text": comment.exact_text,
                }
                for comment in comments
            ],
            "attempt": attempt,
            "allowed_citation_keys": sorted(context.allowed_citation_keys),
            "grounded_numeric_values": [
                canonical_decimal(value) for value in context.grounded_numeric_values
            ],
            "response_schema": {
                "schema_version": 1,
                "section_id": section.section_id,
                "revised_body": "body only; no heading",
                "resolutions": [
                    {
                        "comment_id": "existing assigned comment ID",
                        "writer_status": "addressed|not_addressed",
                        "reason": "nonempty reason",
                    }
                ],
            },
        }
        response = self._chat_json(
            role="writer",
            model=self.writer_model,
            system=_WRITER_SYSTEM,
            payload=payload,
            max_tokens=16384,
            expected_keys={
                "schema_version",
                "section_id",
                "revised_body",
                "resolutions",
            },
            section_id=section.section_id,
            attempt_id=f"{section.section_id}-attempt-{attempt}",
        )
        _expect_keys(
            response,
            {"schema_version", "section_id", "revised_body", "resolutions"},
            "writer response",
        )
        if response["schema_version"] != 1:
            raise RuntimeError("writer schema_version must be 1")
        if response["section_id"] != section.section_id:
            raise RuntimeError("writer section_id mismatches requested section")
        revised_body = response["revised_body"]
        if not isinstance(revised_body, str):
            raise RuntimeError("writer revised_body must be a string")
        resolutions_raw = response["resolutions"]
        if not isinstance(resolutions_raw, list):
            raise RuntimeError("writer resolutions must be a list")
        known_ids = {comment.comment_id for comment in comments}
        seen: set[str] = set()
        parsed: list[_WriterResolution] = []
        for index, resolution in enumerate(resolutions_raw):
            _expect_keys(
                resolution,
                {"comment_id", "writer_status", "reason"},
                f"writer resolution {index}",
            )
            comment_id = _nonempty_string(resolution["comment_id"], "comment_id")
            if comment_id not in known_ids or comment_id in seen:
                raise RuntimeError("writer resolution IDs must be unique assigned IDs")
            writer_status = _nonempty_string(
                resolution["writer_status"], "writer_status"
            )
            if writer_status not in {"addressed", "not_addressed"}:
                raise RuntimeError("writer_status is invalid")
            parsed.append(
                _WriterResolution(
                    comment_id=comment_id,
                    writer_status=writer_status,
                    reason=_nonempty_string(resolution["reason"], "reason"),
                )
            )
            seen.add(comment_id)
        if seen != known_ids:
            raise RuntimeError("writer response must account for every assigned comment")
        return SectionProposal(
            section_id=section.section_id,
            revised_body=revised_body,
            resolution_comment_ids=tuple(
                item.comment_id for item in parsed if item.writer_status == "addressed"
            ),
        )

    def assess(
        self,
        *,
        comment: ReviewComment,
        section: ManuscriptSection,
        original_body: str,
        revised_body: str,
        attempt_id: str,
        validator_codes: tuple[str, ...],
    ) -> ResolutionAssessment:
        payload = {
            "comment": {
                "comment_id": comment.comment_id,
                "required": comment.required,
                "exact_text": comment.exact_text,
            },
            "section": {
                "section_id": section.section_id,
                "title": section.title,
                "original_body": original_body,
                "revised_body": revised_body,
            },
            "attempt_id": attempt_id,
            "deterministic_validator_codes": list(validator_codes),
            "response_schema": {
                "schema_version": 1,
                "comment_id": comment.comment_id,
                "section_id": section.section_id,
                "attempt_id": attempt_id,
                "verdict": "resolved|unresolved",
                "reason": "nonempty evidence-based reason",
            },
        }
        response = self._chat_json(
            role="critic",
            model=self.critic_model,
            system=_CRITIC_SYSTEM,
            payload=payload,
            max_tokens=2048,
            expected_keys={
                "schema_version",
                "comment_id",
                "section_id",
                "attempt_id",
                "verdict",
                "reason",
            },
            section_id=section.section_id,
            attempt_id=attempt_id,
            comment_id=comment.comment_id,
        )
        _expect_keys(
            response,
            {
                "schema_version",
                "comment_id",
                "section_id",
                "attempt_id",
                "verdict",
                "reason",
            },
            "critic response",
        )
        if response["schema_version"] != 1:
            raise RuntimeError("critic schema_version must be 1")
        if (
            response["comment_id"] != comment.comment_id
            or response["section_id"] != section.section_id
            or response["attempt_id"] != attempt_id
        ):
            raise RuntimeError("critic response identity mismatches assessment input")
        verdict = _nonempty_string(response["verdict"], "verdict")
        if verdict not in {"resolved", "unresolved"}:
            raise RuntimeError("critic verdict is invalid")
        return ResolutionAssessment(
            comment_id=comment.comment_id,
            section_id=section.section_id,
            attempt_id=attempt_id,
            critic_model=self.critic_model,
            context_isolated=True,
            verdict=verdict,
            reason=_nonempty_string(response["reason"], "reason"),
        )

    def _chat_json(
        self,
        *,
        role: str,
        model: str,
        system: str,
        payload: Mapping[str, Any],
        max_tokens: int,
        expected_keys: set[str],
        section_id: str | None = None,
        attempt_id: str | None = None,
        comment_id: str | None = None,
    ) -> dict[str, Any]:
        error_category = "malformed_response"
        for call_index in (1, 2):
            call_system = system if call_index == 1 else system + _FORMAT_REPAIR_SUFFIX
            try:
                response = self._llm.chat(
                    [
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        }
                    ],
                    system=call_system,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=0,
                    json_mode=True,
                    strip_thinking=True,
                )
            except RuntimeError:
                self._record_diagnostic(
                    role=role,
                    model=model,
                    call_index=call_index,
                    max_tokens=max_tokens,
                    error_category="transport_error",
                    section_id=section_id,
                    attempt_id=attempt_id,
                    comment_id=comment_id,
                )
                raise

            parsed: dict[str, Any] | None = None
            content = response.content if isinstance(response.content, str) else ""
            if not content.strip():
                error_category = "empty_response"
            elif response.truncated:
                error_category = "truncated_response"
            else:
                try:
                    parsed = _parse_json_object(content)
                except RuntimeError:
                    error_category = "malformed_response"
                else:
                    if set(parsed) != expected_keys:
                        error_category = "schema_error"
                        parsed = None
                    else:
                        error_category = "success"

            self._record_diagnostic(
                role=role,
                model=model,
                call_index=call_index,
                max_tokens=max_tokens,
                error_category=error_category,
                response=response,
                section_id=section_id,
                attempt_id=attempt_id,
                comment_id=comment_id,
            )
            if parsed is not None:
                return parsed

        raise RuntimeError(
            f"sectional {role} {error_category} after 2 calls"
        )

    def _record_diagnostic(
        self,
        *,
        role: str,
        model: str,
        call_index: int,
        max_tokens: int,
        error_category: str,
        response: object | None = None,
        section_id: str | None = None,
        attempt_id: str | None = None,
        comment_id: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "role": role,
            "model": model,
            "call_index": call_index,
            "max_tokens": max_tokens,
            "finish_reason": str(getattr(response, "finish_reason", "") or ""),
            "truncated": getattr(response, "truncated", False) is True,
            "prompt_tokens": _true_nonnegative_int(
                getattr(response, "prompt_tokens", 0)
            ),
            "completion_tokens": _true_nonnegative_int(
                getattr(response, "completion_tokens", 0)
            ),
            "total_tokens": _true_nonnegative_int(
                getattr(response, "total_tokens", 0)
            ),
            "content_length": len(str(getattr(response, "content", "") or "")),
            "error_category": error_category,
        }
        for key, value in (
            ("section_id", section_id),
            ("attempt_id", attempt_id),
            ("comment_id", comment_id),
        ):
            if value is not None:
                record[key] = value
        self._diagnostic_records.append(record)


def _parse_json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise RuntimeError("LLM response must be text")
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise RuntimeError("unterminated fenced JSON response")
        if lines[0].strip() not in {"```", "```json", "```JSON"}:
            raise RuntimeError("unsupported fenced response language")
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON response: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("LLM response must be a JSON object")
    return value


def _expect_keys(value: object, expected: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise RuntimeError(
            f"{context} fields mismatch; missing={missing}, unknown={unknown}"
        )
    return value


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field} must be a nonempty string")
    return value


def _true_nonnegative_int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0
