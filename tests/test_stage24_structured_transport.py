"""B5-A4 frozen Stage 24 private transport contracts."""

from __future__ import annotations

import hashlib
import json
import ssl

import pytest

from researchclaw.pipeline import stage24_structured_transport as transport


def _input(role: str, client_binding_sha256: str = "a" * 64) -> dict[str, object]:
    common = {
        "schema_version": 2 if role != "generic_support_assessment" else 3,
        "policy_version": {
            "citation_assessment": "citation_assessment_v2",
            "generic_support_assessment": "generic_support_structured_v3",
            "resolution_assessment": "resolution_assessment_v2",
        }[role],
        "assessment_role": role,
        "client_binding_sha256": client_binding_sha256,
        "critic_model": "critic",
    }
    if role == "citation_assessment":
        common.update(
            {
                "canonical_manifest_sha256": "b" * 64,
                "paper_sha256": "c" * 64,
                "obligation_id": "obl-" + "d" * 64,
                "byte_start": 0,
                "byte_end": 1,
                "source_sha256": "e" * 64,
                "instance_id": "instance-1",
                "cite_key": "source2026",
                "stage23_verification_record_sha256": "f" * 64,
                "evidence_records": [],
            }
        )
    return common


def _response(model: str, content: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            content, separators=(",", ":"), ensure_ascii=False
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _http(entity: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(entity)}\r\n\r\n".encode()
        + entity
    )


def _client(
    binding: transport.UnderlyingClientBinding,
) -> transport.Stage24AssessmentTransport:
    return transport.Stage24AssessmentTransport(
        binding,
        credential_source_identity=object(),
        credential_identity=object(),
        credential_bytes=b"fixture-credential",
    )


def _active(
    binding: transport.UnderlyingClientBinding,
) -> transport.ActiveAssessmentClient:
    registry = transport._issue_client_registry(
        owner_identity=object(),
        run_identity=(1, 2),
        semantic_config_sha256="a" * 64,
        factories={"citation_assessment": _client},
    )
    return registry.activate(
        role="citation_assessment",
        binding=binding,
    )


def test_transport_constants_role_order_caps_and_provider_set_are_exact() -> None:
    assert transport.ROLE_ORDER == (
        "citation_assessment",
        "generic_support_assessment",
        "resolution_assessment",
    )
    assert transport.ALLOWED_PROVIDERS == frozenset(
        {
            "openai",
            "openrouter",
            "deepseek",
            "novita",
            "minimax",
            "openai-compatible",
        }
    )
    assert transport.MAX_SEMANTIC_CALLS == 268
    assert transport.MAX_OUTBOUND_ATTEMPTS == 536
    assert transport.MAX_TOKENS == 2048


def test_zero_role_is_exact_not_required_without_registration() -> None:
    touched: list[str] = []
    role_map = transport.build_role_map(
        counts={
            "citation_assessment": 0,
            "generic_support_assessment": 0,
            "resolution_assessment": 0,
        },
        provider="openai",
        base_url="https://api.example.test/v1",
        wire_api="chat_completions",
        semantic_config_sha256="a" * 64,
        models={
            "writer_model": "writer",
            "citation_assessment": "citation",
            "generic_support_assessment": "generic",
            "resolution_assessment": "resolution",
        },
        register=lambda *_args: touched.append("registered"),
    )
    assert tuple(role_map) == transport.ROLE_ORDER
    assert all(
        value == {"state": "not_required", "assessment_count": 0}
        for value in role_map.values()
    )
    assert touched == []


def test_transport_serializer_is_insertion_order_unicode_and_strict() -> None:
    assert transport.transport_json_bytes({"z": "µ", "a": 1}) == (
        b'{"z":"\xc2\xb5","a":1}'
    )
    assert transport.transport_json_bytes({"a": True}) == b'{"a":true}'
    with pytest.raises(transport.Stage24TransportError):
        transport.transport_json_bytes({"a": 1.5})
    with pytest.raises(transport.Stage24TransportError):
        transport.transport_json_bytes({"a": "\ud800"})


def test_exact_request_prompt_envelope_and_caps() -> None:
    binding = transport.issue_underlying_client_binding(
        semantic_config_sha256="a" * 64,
        provider="openai",
        base_url="https://api.example.test/v1",
        wire_api="chat_completions",
        model="critic",
    )
    binding = transport.issue_role_client_binding(
        role="citation_assessment", underlying=binding
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )
    assert request.entity_value["max_tokens"] == 2048
    assert request.entity_value["temperature"] == 0
    assert request.entity_value["stream"] is False
    assert request.headers[1] == (
        "User-Agent",
        "AutoResearchClaw-Stage24/1",
    )
    assert tuple(request.user_envelope) == (
        "schema_version",
        "task",
        "assessment_role",
        "assessment_input",
        "bound_context",
        "response_contract",
    )
    assert len(request.user_json) <= 98_304
    assert len(request.entity) <= 131_072


def test_registry_construction_is_private_and_authorization_order_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError):
        transport.Stage24ClientRegistry(
            authority_token=object(),
            owner_identity=object(),
            run_identity=(1, 2),
            semantic_config_sha256="a" * 64,
            factories={},
        )

    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )
    observed: list[tuple[tuple[str, str], ...]] = []

    def outbound(_client, value):
        observed.append(value.headers)
        return _http(
            _response(
                "critic",
                {"verdict": "supported", "reason": "Exact support."},
            )
        )

    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        outbound,
    )
    transport.execute_assessment(
        request,
        role="citation_assessment",
        binding=binding,
        active_client=_active(binding),
        semantic_call_ordinal=1,
    )
    assert tuple(name for name, _value in observed[0]) == (
        "Host",
        "User-Agent",
        "Accept",
        "Accept-Encoding",
        "Content-Type",
        "Content-Length",
        "Authorization",
        "Connection",
    )
    assert b"Authorization" not in request.canonical_transcript


def test_assessment_input_cap_counts_global_canonical_lf() -> None:
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    value = _input(
        "citation_assessment",
        binding.client_binding_sha256,
    )
    value["padding"] = ""
    base_size = len(
        __import__(
            "researchclaw.pipeline.stage24_structured_authority",
            fromlist=["global_canonical_json_bytes"],
        ).global_canonical_json_bytes(value)
    )
    value["padding"] = "x" * (32_768 - base_size)
    assert len(
        __import__(
            "researchclaw.pipeline.stage24_structured_authority",
            fromlist=["global_canonical_json_bytes"],
        ).global_canonical_json_bytes(value)
    ) == 32_768
    transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=value,
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )
    value["padding"] += "x"
    with pytest.raises(transport.Stage24TransportError):
        transport.build_assessment_request(
            role="citation_assessment",
            binding=binding,
            assessment_input=value,
            bound_context={"manuscript_context": "x", "retained_excerpts": []},
        )


def test_provider_envelope_and_v1_receipt_are_strict() -> None:
    parsed = transport.parse_provider_response(
        _response("critic", {"verdict": "supported", "reason": "Exact support."}),
        role="citation_assessment",
        requested_model="critic",
    )
    assert parsed.decision == {
        "verdict": "supported",
        "reason": "Exact support.",
    }
    receipt = transport.build_transport_receipt(
        role="citation_assessment",
        client_binding_sha256="a" * 64,
        semantic_call_ordinal=1,
        outbound_attempts=1,
        request_sha256="b" * 64,
        response_sha256="c" * 64,
        origin="https://api.example.test",
        target="/v1/chat/completions",
        decision=parsed.decision,
    )
    assert tuple(receipt) == transport.TRANSPORT_RECEIPT_ROOTS
    assert len(receipt) == 15
    assert receipt["retry_fingerprint"] is None
    assert receipt["response_content_sha256"] == hashlib.sha256(
        transport.transport_json_bytes(parsed.decision)
    ).hexdigest()

    bad = json.loads(_response("critic", {"verdict": "supported", "reason": "ok"}))
    bad["choices"][0]["message"]["reasoning_content"] = "hidden"
    with pytest.raises(transport.Stage24TransportError):
        transport.parse_provider_response(
            json.dumps(bad, separators=(",", ":")).encode(),
            role="citation_assessment",
            requested_model="critic",
        )


def test_only_zero_material_closed_transport_failure_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    binding = transport.issue_underlying_client_binding(
        semantic_config_sha256="a" * 64,
        provider="openai",
        base_url="https://api.example.test/v1",
        wire_api="chat_completions",
        model="critic",
    )
    binding = transport.issue_role_client_binding(
        role="citation_assessment", underlying=binding
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    entity = _response(
        "critic", {"verdict": "supported", "reason": "Exact support."}
    )

    raw_response = _http(entity)

    class Socket:
        def __init__(self):
            self.responses = [raw_response, b""]

        def settimeout(self, _value):
            pass

        def sendall(self, value):
            assert b"Authorization: Bearer fixture-credential\r\n" in value
            assert value.endswith(request.entity)

        def recv(self, _size):
            return self.responses.pop(0)

        def close(self):
            pass

    def connect(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("connect timeout before response material")
        return Socket()

    class Context:
        def wrap_socket(self, value, *, server_hostname):
            assert server_hostname == "api.example.test"
            return value

    monkeypatch.setattr(transport.socket, "create_connection", connect)
    monkeypatch.setattr(
        transport.ssl,
        "create_default_context",
        lambda: Context(),
    )
    outcome = transport.execute_assessment(
        request,
        role="citation_assessment",
        binding=binding,
        active_client=_active(binding),
        semantic_call_ordinal=1,
    )
    assert len(calls) == 2
    assert outcome.receipt["outbound_attempts"] == 2
    assert outcome.receipt["retry_fingerprint"] == request.request_sha256


def test_certificate_validation_failure_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    class Socket:
        def close(self):
            pass

    class Context:
        def wrap_socket(self, _value, *, server_hostname):
            calls.append(1)
            raise ssl.SSLCertVerificationError("certificate mismatch")

    monkeypatch.setattr(
        transport.socket,
        "create_connection",
        lambda *_args, **_kwargs: Socket(),
    )
    monkeypatch.setattr(
        transport.ssl,
        "create_default_context",
        lambda: Context(),
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1]


@pytest.mark.parametrize("failure", (TimeoutError, ConnectionResetError))
def test_first_recv_zero_material_timeout_or_reset_retries_once(
    failure,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )
    entity = _response(
        "critic", {"verdict": "supported", "reason": "Exact support."}
    )

    class Socket:
        def __init__(self, attempt):
            self.attempt = attempt
            self.responses = [_http(entity), b""]

        def settimeout(self, _value):
            pass

        def sendall(self, _value):
            pass

        def recv(self, _size):
            if self.attempt == 1:
                raise failure("zero HTTP response bytes")
            return self.responses.pop(0)

        def close(self):
            pass

    def connect(*_args, **_kwargs):
        attempts.append(1)
        return Socket(len(attempts))

    class Context:
        def wrap_socket(self, value, *, server_hostname):
            return value

    monkeypatch.setattr(transport.socket, "create_connection", connect)
    monkeypatch.setattr(
        transport.ssl,
        "create_default_context",
        lambda: Context(),
    )
    outcome = transport.execute_assessment(
        request,
        role="citation_assessment",
        binding=binding,
        active_client=_active(binding),
        semantic_call_ordinal=1,
    )
    assert attempts == [1, 1]
    assert outcome.receipt["outbound_attempts"] == 2


def test_tls_handshake_io_failure_retries_but_protocol_error_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    class Socket:
        def close(self):
            pass

    calls: list[int] = []

    class Context:
        def wrap_socket(self, value, *, server_hostname):
            calls.append(1)
            raise ssl.SSLWantReadError("TLS handshake I/O")

    monkeypatch.setattr(
        transport.socket,
        "create_connection",
        lambda *_args, **_kwargs: Socket(),
    )
    monkeypatch.setattr(
        transport.ssl,
        "create_default_context",
        lambda: Context(),
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1, 1]

    calls.clear()

    class PolicyContext:
        def wrap_socket(self, value, *, server_hostname):
            calls.append(1)
            raise ssl.SSLError("unsupported protocol")

    monkeypatch.setattr(
        transport.ssl,
        "create_default_context",
        lambda: PolicyContext(),
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1]


def test_partial_response_material_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    def outbound(_client, _request):
        calls.append(1)
        raise transport.Stage24TransportError(
            "response material already observed"
        )

    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        outbound,
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1]


def test_direct_callable_timeout_never_authorizes_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    binding = transport.issue_role_client_binding(
        role="citation_assessment",
        underlying=transport.issue_underlying_client_binding(
            semantic_config_sha256="a" * 64,
            provider="openai",
            base_url="https://api.example.test/v1",
            wire_api="chat_completions",
            model="critic",
        ),
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    def outbound(_client, _request):
        calls.append(1)
        raise TimeoutError("caller cannot prove response-material state")

    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        outbound,
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1]


@pytest.mark.parametrize(
    "error_class",
    ("certificate_validation", "provider_policy", "remote_protocol_error"),
)
def test_nonclosed_or_uncertain_failure_never_retries(
    error_class: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    binding = transport.issue_underlying_client_binding(
        semantic_config_sha256="a" * 64,
        provider="openai",
        base_url="https://api.example.test/v1",
        wire_api="chat_completions",
        model="critic",
    )
    binding = transport.issue_role_client_binding(
        role="citation_assessment", underlying=binding
    )
    request = transport.build_assessment_request(
        role="citation_assessment",
        binding=binding,
        assessment_input=_input(
            "citation_assessment",
            binding.client_binding_sha256,
        ),
        bound_context={"manuscript_context": "x", "retained_excerpts": []},
    )

    def outbound(_client, _request):
        calls.append(1)
        raise transport.PureTransportError(
            error_class, zero_response_material=error_class != "remote_protocol_error"
        )

    monkeypatch.setattr(
        transport.Stage24AssessmentTransport,
        "exchange",
        outbound,
    )
    with pytest.raises(transport.Stage24TransportError):
        transport.execute_assessment(
            request,
            role="citation_assessment",
            binding=binding,
            active_client=_active(binding),
            semantic_call_ordinal=1,
        )
    assert calls == [1]
