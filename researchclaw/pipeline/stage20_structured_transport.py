"""Strict two-semantic/four-outbound transport for structured Stage 20."""

from __future__ import annotations

import hashlib
import http.client
import urllib.error
from dataclasses import asdict, dataclass
from typing import Mapping

from researchclaw.llm.client import LLMClient, LLMResponse
from researchclaw.pipeline.canonical_experiment_evidence import (
    canonical_authority_json_text,
)
from researchclaw.pipeline.stage20_structured_authority import (
    StructuredStage20AuthorityError,
    parse_quality_response,
)


SEMANTIC_CALL_LIMIT = 2
OUTBOUND_ATTEMPT_LIMIT_PER_CALL = 2
TOTAL_OUTBOUND_ATTEMPT_LIMIT = 4
STRUCTURED_STAGE20_MAX_TOKENS = 1024


class StructuredStage20TransportError(RuntimeError):
    """The frozen structured Stage 20 quality transport failed."""

    diagnostics: tuple["TransportDiagnostic", ...] = ()


@dataclass(frozen=True)
class TransportDiagnostic:
    semantic_call_ordinal: int
    call_role: str
    outbound_attempt_ordinal: int
    request_fingerprint: str
    outcome: str
    transport_class: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_quality_prompt(
    paper: bytes,
    *,
    quality_threshold: str,
    repair_category: str | None = None,
) -> bytes:
    """Build a prompt containing only the replayed immutable paper projection."""

    try:
        paper_text = paper.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredStage20TransportError(
            "structured paper is not UTF-8"
        ) from exc
    payload: dict[str, object] = {
        "schema_version": 1,
        "task": "quality_gate",
        "immutable_paper": paper_text,
        "quality_threshold": quality_threshold,
        "required_response_fields": [
            "score_1_to_10",
            "verdict",
            "strengths",
            "weaknesses",
            "required_actions",
        ],
        "allowed_verdicts": ["proceed", "revise", "reject"],
    }
    if repair_category is not None:
        payload["repair_category"] = repair_category
        payload["task"] = "quality_gate_repair"
    return canonical_authority_json_text(payload).encode("utf-8")


def execute_bounded_quality_calls(
    *,
    paper: bytes,
    quality_threshold: str,
    llm: LLMClient,
) -> tuple[Mapping[str, object], tuple[TransportDiagnostic, ...]]:
    """Run at most two semantic calls and at most four outbound attempts."""

    if llm.config.fallback_url or llm.config.fallback_models:
        raise StructuredStage20TransportError(
            "structured Stage 20 forbids endpoint and model fallback"
        )
    model = llm.config.primary_model
    base_url = llm.config.base_url
    api_key = llm.config.api_key
    fallback_api_key = llm.config.fallback_api_key
    extra_headers = tuple(sorted(llm.config.extra_headers.items()))
    user_agent = llm.config.user_agent
    timeout_sec = llm.config.timeout_sec
    wire_api = getattr(llm.config, "wire_api", "chat_completions")
    anthropic_adapter = getattr(llm, "_anthropic", None)
    anthropic_base_url = (
        getattr(anthropic_adapter, "base_url", None)
        if anthropic_adapter is not None
        else None
    )
    anthropic_api_key = (
        getattr(anthropic_adapter, "api_key", None)
        if anthropic_adapter is not None
        else None
    )
    anthropic_timeout = (
        getattr(anthropic_adapter, "timeout_sec", None)
        if anthropic_adapter is not None
        else None
    )
    if anthropic_adapter is not None and (
        not isinstance(anthropic_base_url, str) or not anthropic_base_url
    ):
        raise StructuredStage20TransportError(
            "structured Stage 20 Anthropic endpoint is invalid"
        )
    endpoint = (
        f"{anthropic_base_url.rstrip('/')}/v1/messages"
        if anthropic_adapter is not None
        else llm._endpoint_url(base_url)
    )
    diagnostics: list[TransportDiagnostic] = []
    total_outbound = 0
    repair_category: str | None = None
    for semantic_ordinal in range(1, SEMANTIC_CALL_LIMIT + 1):
        role = "initial" if semantic_ordinal == 1 else "repair"
        prompt = build_quality_prompt(
            paper,
            quality_threshold=quality_threshold,
            repair_category=repair_category if semantic_ordinal == 2 else None,
        )
        fingerprint = _fingerprint(
            model=model,
            endpoint=endpoint,
            wire_api=str(wire_api),
            prompt=prompt,
        )
        for attempt in range(1, OUTBOUND_ATTEMPT_LIMIT_PER_CALL + 1):
            if total_outbound >= TOTAL_OUTBOUND_ATTEMPT_LIMIT:
                error = StructuredStage20TransportError(
                    "structured Stage 20 outbound limit exceeded"
                )
                error.diagnostics = tuple(diagnostics)
                raise error
            total_outbound += 1
            try:
                _require_frozen_request(
                    llm,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    fallback_api_key=fallback_api_key,
                    extra_headers=extra_headers,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    wire_api=wire_api,
                    endpoint=endpoint,
                    anthropic_adapter=anthropic_adapter,
                    anthropic_base_url=anthropic_base_url,
                    anthropic_api_key=anthropic_api_key,
                    anthropic_timeout=anthropic_timeout,
                )
            except StructuredStage20TransportError as exc:
                exc.diagnostics = tuple(diagnostics)
                raise
            messages = [{"role": "user", "content": prompt.decode("utf-8")}]
            try:
                response = llm._raw_call(
                    model,
                    messages,
                    STRUCTURED_STAGE20_MAX_TOKENS,
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
                        role,
                        attempt,
                        fingerprint,
                        "transport_error",
                        transport_class,
                    )
                )
                if retryable and attempt == 1:
                    continue
                error = StructuredStage20TransportError(
                    f"structured Stage 20 {role} transport failed"
                )
                error.diagnostics = tuple(diagnostics)
                raise error from exc
            try:
                content = _response_content(response, expected_model=model)
                parsed = parse_quality_response(content)
            except (StructuredStage20AuthorityError, StructuredStage20TransportError) as exc:
                repair_category = _semantic_category(exc)
                diagnostics.append(
                    TransportDiagnostic(
                        semantic_ordinal,
                        role,
                        attempt,
                        fingerprint,
                        "semantic_rejected",
                        repair_category,
                    )
                )
                if semantic_ordinal == 1:
                    break
                error = StructuredStage20TransportError(
                    "structured Stage 20 repair response is invalid"
                )
                error.diagnostics = tuple(diagnostics)
                raise error from exc
            diagnostics.append(
                TransportDiagnostic(
                    semantic_ordinal,
                    role,
                    attempt,
                    fingerprint,
                    "accepted",
                    "response",
                )
            )
            return parsed, tuple(diagnostics)
    error = StructuredStage20TransportError(
        "structured Stage 20 semantic call limit exhausted"
    )
    error.diagnostics = tuple(diagnostics)
    raise error


def _fingerprint(
    *, model: str, endpoint: str, wire_api: str, prompt: bytes
) -> str:
    return hashlib.sha256(
        b"\0".join(
            (
                model.encode("utf-8"),
                endpoint.encode("utf-8"),
                wire_api.encode("utf-8"),
                prompt,
                b"json_mode=true",
                b"exact_json_instruction=true",
                b"temperature=0",
                str(STRUCTURED_STAGE20_MAX_TOKENS).encode("ascii"),
            )
        )
    ).hexdigest()


def _require_frozen_request(
    llm: LLMClient,
    *,
    model: str,
    base_url: str,
    api_key: str,
    fallback_api_key: str,
    extra_headers: tuple[tuple[str, str], ...],
    user_agent: str,
    timeout_sec: int,
    wire_api: object,
    endpoint: str,
    anthropic_adapter: object,
    anthropic_base_url: object,
    anthropic_api_key: object,
    anthropic_timeout: object,
) -> None:
    current_endpoint = (
        f"{anthropic_base_url.rstrip('/')}/v1/messages"
        if anthropic_adapter is not None and isinstance(anthropic_base_url, str)
        else llm._endpoint_url(llm.config.base_url)
    )
    if (
        llm.config.primary_model != model
        or llm.config.base_url != base_url
        or llm.config.api_key != api_key
        or llm.config.fallback_api_key != fallback_api_key
        or tuple(sorted(llm.config.extra_headers.items())) != extra_headers
        or llm.config.user_agent != user_agent
        or llm.config.timeout_sec != timeout_sec
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
            anthropic_adapter is not None
            and getattr(anthropic_adapter, "api_key", None)
            != anthropic_api_key
        )
        or (
            anthropic_adapter is not None
            and getattr(anthropic_adapter, "timeout_sec", None)
            != anthropic_timeout
        )
        or current_endpoint != endpoint
    ):
        raise StructuredStage20TransportError(
            "structured Stage 20 frozen request changed"
        )


def _response_content(response: object, *, expected_model: str) -> bytes:
    if not isinstance(response, LLMResponse):
        raise StructuredStage20TransportError("provider response type is invalid")
    if response.model != expected_model:
        raise StructuredStage20TransportError(
            "provider response changed model identity"
        )
    if response.truncated or response.finish_reason == "length":
        raise StructuredStage20TransportError("provider response is truncated")
    if not isinstance(response.content, str) or not response.content.strip():
        raise StructuredStage20TransportError("provider response content is empty")
    return response.content.encode("utf-8")


def _semantic_category(exc: Exception) -> str:
    message = str(exc)
    if "truncated" in message:
        return "truncated_response"
    if "empty" in message:
        return "empty_response"
    if "duplicate" in message:
        return "duplicate_key"
    if "fields" in message:
        return "schema_fields"
    if "verdict" in message:
        return "invalid_verdict"
    if "score" in message or "nonfinite" in message:
        return "invalid_score"
    return "malformed_response"


def _retryable_without_content(exc: Exception) -> tuple[bool, str]:
    if (
        getattr(exc, "_researchclaw_response_content_received", False)
        is True
    ):
        return False, "response_content"
    for attribute in ("partial", "content", "body", "data"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (bytes, bytearray, memoryview, str)) and len(value):
            return False, "response_content"
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
