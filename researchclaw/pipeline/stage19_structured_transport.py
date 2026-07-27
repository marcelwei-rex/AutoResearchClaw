"""Strict bounded transport for structured Stage 19 ID selection."""

from __future__ import annotations

import hashlib
import http.client
import urllib.error
from dataclasses import dataclass
from typing import Callable, Sequence

from researchclaw.llm.client import LLMClient, LLMResponse
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.scientific_claim_authority import (
    CLAIM_POLICY_ID,
    ScientificClaimGenerationBinding,
    build_scientific_claim_registry,
)
from researchclaw.pipeline.scientific_claim_publication import (
    SECTION_ORDER,
    ScientificClaimSelectionArtifact,
)
from researchclaw.pipeline.sectional_revision import ReviewLedger


SEMANTIC_CALL_LIMIT = 5
OUTBOUND_ATTEMPT_LIMIT_PER_CALL = 2
TOTAL_OUTBOUND_ATTEMPT_LIMIT = 10
STRUCTURED_STAGE19_MAX_TOKENS = 512


class StructuredStage19TransportError(RuntimeError):
    """The bounded Stage 19 provider contract failed."""


@dataclass(frozen=True)
class TransportDiagnostic:
    semantic_call_ordinal: int
    section_id: str
    outbound_attempt_ordinal: int
    request_fingerprint: str
    outcome: str
    transport_class: str


def build_selection_prompts(
    *,
    source_selection: ScientificClaimSelectionArtifact,
    binding: ScientificClaimGenerationBinding,
    ledger: ReviewLedger,
) -> tuple[tuple[str, bytes], ...]:
    """Precompute all five prompts solely from one replayed snapshot."""

    registry = {
        claim.claim_id: claim for claim in build_scientific_claim_registry(binding).claims
    }
    if tuple(section.section_id for section in source_selection.sections) != SECTION_ORDER:
        raise StructuredStage19TransportError("source selection section order mismatch")
    advice = [
        {
            "comment_id": comment.comment_id,
            "reviewer": comment.reviewer,
            "category": comment.category,
            "exact_text": comment.exact_text,
        }
        for comment in ledger.comments
    ]
    prompts: list[tuple[str, bytes]] = []
    for section in source_selection.sections:
        allowed = []
        for claim_id in section.selected_claim_ids:
            claim = registry.get(claim_id)
            if claim is None or claim.section_id != section.section_id:
                raise StructuredStage19TransportError(
                    "source selection claim membership mismatch"
                )
            allowed.append(
                {
                    "claim_id": claim.claim_id,
                    "mandatory": claim.mandatory,
                    "rendered_sentence": claim.rendered_sentence,
                }
            )
        payload = {
            "schema_version": 1,
            "claim_policy_id": CLAIM_POLICY_ID,
            "target_section": section.section_id,
            "source_selection": section.provider_dict(),
            "allowed_claims": allowed,
            "review_advice": advice,
        }
        prompts.append(
            (
                section.section_id,
                canonical_authority_json_text(payload).encode("utf-8"),
            )
        )
    return tuple(prompts)


def execute_bounded_selection_calls(
    prompts: Sequence[tuple[str, bytes]],
    llm: LLMClient,
    *,
    validate: Callable[[str, bytes], None],
) -> tuple[tuple[bytes, ...], tuple[TransportDiagnostic, ...]]:
    """Run exactly five semantic calls with a mechanical total bound of ten."""

    if len(prompts) != SEMANTIC_CALL_LIMIT:
        raise StructuredStage19TransportError("semantic call count must equal 5")
    if llm.config.fallback_url or llm.config.fallback_models:
        raise StructuredStage19TransportError(
            "structured Stage 19 forbids endpoint and model fallback"
        )
    model = llm.config.primary_model
    base_url = llm.config.base_url
    wire_api = getattr(llm.config, "wire_api", "chat_completions")
    anthropic_adapter = getattr(llm, "_anthropic", None)
    anthropic_base_url = (
        getattr(anthropic_adapter, "base_url", None)
        if anthropic_adapter is not None
        else None
    )
    if anthropic_adapter is not None and (
        not isinstance(anthropic_base_url, str) or not anthropic_base_url
    ):
        raise StructuredStage19TransportError(
            "structured Stage 19 Anthropic endpoint is invalid"
        )
    endpoint = (
        f"{anthropic_base_url.rstrip('/')}/v1/messages"
        if anthropic_adapter is not None
        else llm._endpoint_url(base_url)
    )
    responses: list[bytes] = []
    diagnostics: list[TransportDiagnostic] = []
    total = 0
    for semantic_ordinal, (section_id, prompt) in enumerate(prompts, start=1):
        if type(prompt) is not bytes:
            raise StructuredStage19TransportError("prompt must be exact bytes")
        fingerprint = hashlib.sha256(
            b"\0".join(
                (
                    model.encode("utf-8"),
                    endpoint.encode("utf-8"),
                    str(wire_api).encode("utf-8"),
                    prompt,
                    b"json_mode=true",
                    b"exact_json_instruction=true",
                    b"temperature=0",
                    str(STRUCTURED_STAGE19_MAX_TOKENS).encode("ascii"),
                )
            )
        ).hexdigest()
        for attempt in range(1, OUTBOUND_ATTEMPT_LIMIT_PER_CALL + 1):
            if total >= TOTAL_OUTBOUND_ATTEMPT_LIMIT:
                raise StructuredStage19TransportError(
                    "structured Stage 19 outbound limit exceeded"
                )
            total += 1
            if (
                llm.config.primary_model != model
                or llm.config.base_url != base_url
                or getattr(llm.config, "wire_api", "chat_completions") != wire_api
                or llm.config.fallback_url
                or llm.config.fallback_models
                or getattr(llm, "_anthropic", None) is not anthropic_adapter
                or (
                    anthropic_adapter is not None
                    and getattr(anthropic_adapter, "base_url", None)
                    != anthropic_base_url
                )
                or (
                    f"{anthropic_base_url.rstrip('/')}/v1/messages"
                    if anthropic_adapter is not None
                    else llm._endpoint_url(llm.config.base_url)
                )
                != endpoint
            ):
                raise StructuredStage19TransportError(
                    "structured Stage 19 frozen request changed"
                )
            messages = [{"role": "user", "content": prompt.decode("utf-8")}]
            try:
                response = llm._raw_call(
                    model,
                    messages,
                    STRUCTURED_STAGE19_MAX_TOKENS,
                    0,
                    True,
                    allow_endpoint_fallback=False,
                    exact_max_tokens=True,
                    exact_temperature=True,
                    exact_json_instruction=True,
                )
            except Exception as exc:
                retryable, transport_class = _retryable_without_content(exc)
                diagnostics.append(
                    TransportDiagnostic(
                        semantic_ordinal,
                        section_id,
                        attempt,
                        fingerprint,
                        "transport_error",
                        transport_class,
                    )
                )
                if retryable and attempt == 1:
                    continue
                raise StructuredStage19TransportError(
                    f"structured transport failed for {section_id}"
                ) from exc
            content = _validated_response_content(response, expected_model=model)
            try:
                validate(section_id, content)
            except Exception as exc:
                diagnostics.append(
                    TransportDiagnostic(
                        semantic_ordinal,
                        section_id,
                        attempt,
                        fingerprint,
                        "semantic_rejected",
                        "response",
                    )
                )
                raise StructuredStage19TransportError(
                    f"structured selection rejected for {section_id}"
                ) from exc
            diagnostics.append(
                TransportDiagnostic(
                    semantic_ordinal,
                    section_id,
                    attempt,
                    fingerprint,
                    "accepted",
                    "response",
                )
            )
            responses.append(content)
            break
    return tuple(responses), tuple(diagnostics)


def _validated_response_content(
    response: object,
    *,
    expected_model: str,
) -> bytes:
    if not isinstance(response, LLMResponse):
        raise StructuredStage19TransportError("provider response type is invalid")
    if response.model != expected_model or response.truncated:
        raise StructuredStage19TransportError(
            "provider response is truncated or changed model identity"
        )
    if not isinstance(response.content, str) or not response.content:
        raise StructuredStage19TransportError("provider response content is empty")
    return response.content.encode("utf-8")


def _retryable_without_content(exc: Exception) -> tuple[bool, str]:
    if isinstance(exc, urllib.error.HTTPError):
        body = b""
        try:
            body = exc.read()
        except Exception:
            body = b""
        return (not body and (exc.code == 429 or 500 <= exc.code <= 599), "http")
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return True, "timeout_or_connection"
    if isinstance(exc, (ConnectionError, http.client.HTTPException)):
        return True, "connection"
    return False, type(exc).__name__
