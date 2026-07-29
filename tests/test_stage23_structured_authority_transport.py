"""B5-A3 structured Stage 23 authority and fake-byte transport attacks."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json

import pytest

from researchclaw.pipeline import stage23_structured_authority as authority
from researchclaw.pipeline import stage23_structured_transport as transport


def _http(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
    extra: tuple[bytes, ...] = (),
) -> bytes:
    lines = [
        f"HTTP/1.1 {status} Test".encode("ascii"),
        f"Content-Type: {content_type}".encode("ascii"),
        f"Content-Length: {len(body)}".encode("ascii"),
        *extra,
        b"",
        b"",
    ]
    return b"\r\n".join(lines) + body


def _crossref_body(*, doi: str = "10.1000/example") -> bytes:
    return json.dumps(
        {
            "status": "ok",
            "message-type": "work",
            "message": {
                "DOI": doi,
                "title": ["Example title"],
                "published-print": {"date-parts": [[2026]]},
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _openalex_body(results: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"meta": {"count": len(results)}, "results": results},
        separators=(",", ":"),
    ).encode("utf-8")


def _relevance_body(
    *,
    model: str = "model-1",
    scores: tuple[tuple[str, str], ...] = (("Example2026", "0.750000"),),
) -> bytes:
    content = json.dumps(
        {
            "scores": [
                {"cite_key": cite_key, "score": score}
                for cite_key, score in scores
            ]
        },
        separators=(",", ":"),
    )
    return json.dumps(
        {
            "model": model,
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_identity_normalization_and_route_precedence_are_exact() -> None:
    assert authority.normalize_doi(" DOI:10.1000/ABC/Def ") == "10.1000/abc/def"
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.normalize_doi("10.1000/a%2Fb")
    assert authority.normalize_arxiv_request("arXiv:2401.01234v2") == (
        "2401.01234v2",
        "2401.01234",
        "v2",
    )
    assert authority.normalize_arxiv_request(
        "https://arxiv.org/pdf/cs.AI/1234567.pdf"
    ) == ("cs.ai/1234567", "cs.ai/1234567", None)
    assert authority.title_match_key("  A—Title: TEST  ") == "a title test"
    assert authority.select_route(
        {"doi": "10.1000/example", "arxiv": "2401.01234", "title": "Example"}
    ) == ("crossref-doi", "crossref", "10.1000/example")
    assert authority.select_route(
        {"doi": "", "arxiv": "2401.01234", "title": "Example"}
    ) == ("arxiv-id", "arxiv", "2401.01234")
    assert authority.select_route(
        {"doi": "", "arxiv": "", "title": "Example"}
    ) == ("openalex-title", "openalex", "example")


def test_identity_uses_unicode_white_space_not_python_isspace() -> None:
    white_space = (
        "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
        "\u200a\u2028\u2029\u202f\u205f\u3000"
    )
    assert authority.normalize_doi(
        f"{white_space}doi:10.1000/Example{white_space}"
    ) == "10.1000/example"
    assert authority.title_match_key(
        f"{white_space}A{white_space}Title{white_space}"
    ) == "a title"

    for control in ("\u001c", "\u001d", "\u001e", "\u001f"):
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.normalize_doi(f"{control}10.1000/example{control}")
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.normalize_doi(f"10.1000/ex{control}ample")
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.normalize_arxiv_request(f"{control}2401.01234{control}")
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.normalize_arxiv_request(f"2401.01{control}234")
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.title_match_key(f"{control}Example{control}")
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.select_route(
                {
                    "doi": control,
                    "arxiv": "2401.01234",
                    "title": "Example",
                }
            )
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.select_route(
                {
                    "doi": "",
                    "arxiv": control,
                    "title": "Example",
                }
            )


@pytest.mark.parametrize(
    ("requested", "response", "accepted"),
    (
        ("2401.01234v2", "2401.01234v2", True),
        ("2401.01234v2", "2401.01234v3", False),
        ("2401.01234", "2401.01234v7", True),
        ("2401.01234", "2401.01234", False),
        ("cs.ai/1234567v1", "cs.ai/1234567v1", True),
        ("cs.ai/1234567", "cs.ai/1234567v2", True),
        ("cs.ai/1234567", "cs.lg/1234567v2", False),
    ),
)
def test_arxiv_versioned_versionless_identity_matrix(
    requested: str,
    response: str,
    accepted: bool,
) -> None:
    if accepted:
        assert authority.match_arxiv_identity(requested, response) == response
    else:
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.match_arxiv_identity(requested, response)


def test_arxiv_namespace_is_closed_and_response_identity_is_not_dereferenced() -> None:
    good = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>https://arxiv.org/abs/2401.01234v2</id>
  <title>Example title</title><published>2026-01-02T00:00:00Z</published></entry>
</feed>"""
    parsed = authority.parse_arxiv_response(
        good, requested="2401.01234", held_title="Example title"
    )
    assert parsed["arxiv_id"] == "2401.01234v2"
    bad_namespace = good.replace(
        b"</entry>", b'<evil xmlns="https://evil.example">x</evil></entry>'
    )
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.parse_arxiv_response(
            bad_namespace,
            requested="2401.01234",
            held_title="Example title",
        )
    rebound_namespace = good.replace(
        b"<entry>",
        b'<entry xmlns:a="http://arxiv.org/schemas/atom">'
        b'<a:marker xmlns:a="http://www.w3.org/2005/Atom">x</a:marker>',
    )
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.parse_arxiv_response(
            rebound_namespace,
            requested="2401.01234",
            held_title="Example title",
        )
    oversized_tail = good.replace(b"</entry>", b"</entry>" + b"x" * 16385)
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.parse_arxiv_response(
            oversized_tail,
            requested="2401.01234",
            held_title="Example title",
        )
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.match_arxiv_identity("2401.01234", "2401.01234V2")


def test_openalex_requires_complete_mechanically_unique_title_match() -> None:
    row = {
        "id": "https://openalex.org/W1",
        "doi": None,
        "title": "Example title",
        "publication_year": 2026,
        "ids": {},
    }
    parsed = authority.parse_openalex_response(
        _openalex_body([row]),
        held_title="Example title",
    )
    assert parsed["title"] == "Example title"
    with pytest.raises(authority.StructuredStage23AuthorityError, match="ambiguous"):
        authority.parse_openalex_response(
            _openalex_body([row, {**row, "id": "https://openalex.org/W2"}]),
            held_title="Example title",
        )
    mismatch = json.dumps(
        {"meta": {"count": 2}, "results": [row]},
        separators=(",", ":"),
    ).encode()
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.parse_openalex_response(mismatch, held_title="Example title")
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.display_title("Example\u009f title")


def test_strict_json_rejects_duplicate_bool_nan_inf_and_excess_structure() -> None:
    attacks = (
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"\xef\xbb\xbf{}",
    )
    for payload in attacks:
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.strict_json_object(payload, "attack")
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.require_true_int(True, 1, "bool")
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.strict_json_object(
            json.dumps({"x" * 16_385: 0}).encode(), "oversized-key"
        )
    with pytest.raises(authority.StructuredStage23AuthorityError):
        authority.strict_json_object(
            b'{"unused":' + b"9" * 5000 + b"}", "oversized-integer"
        )


def test_cited_closure_rejects_zero_33_duplicate_unsorted_and_malformed() -> None:
    attacks = (
        (),
        tuple(f"K{index:02d}" for index in range(33)),
        ("A", "A"),
        ("B", "A"),
        ("bad key",),
    )
    for cited in attacks:
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.validate_cited_keys(cited)


@pytest.mark.parametrize(
    "mutation",
    (
        {"provider": "anthropic"},
        {"provider": "unknown"},
        {"wire_api": "responses"},
        {"fallback_models": ("fallback",)},
        {"fallback_models": []},
        {"bridge_enabled": True},
        {"extra_headers": {"X-Test": "1"}},
        {"extra_headers": None},
        {"fallback_url": "https://fallback.example"},
        {"fallback_url": None},
        {"anthropic_adapter": object()},
        {"base_url": "http://api.example/v1"},
        {"base_url": "https://api.example/v1/chat/completions"},
        {"base_url": "https://api.example/%2e%2e/v1"},
        {"base_url": "https://bad host/v1"},
        {"base_url": "https://api.example:bad/v1"},
        {"base_url": "https://api.example:/v1"},
        {"credential": b"bad token"},
    ),
)
def test_relevance_provider_wire_fallback_bridge_and_credential_rejections(
    mutation: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "provider": "openai",
        "model": "model-1",
        "base_url": "https://api.example/v1",
        "wire_api": "chat_completions",
        "fallback_models": (),
        "bridge_enabled": False,
        "extra_headers": {},
        "fallback_url": "",
        "anthropic_adapter": None,
        "credential": b"fake-token",
    }
    values.update(mutation)
    with pytest.raises(transport.Stage23TransportError):
        transport.issue_relevance_transport_spec(**values)


def test_relevance_request_repr_and_generic_failure_do_not_leak_credential() -> None:
    secret = b"fake-secret-token"
    spec = transport.issue_relevance_transport_spec(
        provider="openai",
        model="model-1",
        base_url="https://api.example/v1",
        wire_api="chat_completions",
        fallback_models=(),
        bridge_enabled=False,
        extra_headers={},
        fallback_url="",
        anthropic_adapter=None,
        credential=secret,
    )
    request = transport._build_relevance_request(
        spec=spec,
        paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
        claim_projection={"abstract": "Abstract."},
        cited_keys=("K1",),
        metadata=(
            {
                "cite_key": "K1",
                "route_class": "crossref-doi",
                "endpoint_class": "crossref",
                "request_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "outbound_count": 1,
                "status": "verified",
                "metadata": {
                    "title": "Title",
                    "doi": "10.1000/example",
                    "arxiv_id": None,
                    "year": 2026,
                    "source": "crossref",
                },
            },
        ),
    )
    assert secret.decode() not in repr(request)

    def malicious_outbound(value: transport.WireRequest):
        raise RuntimeError(f"provider echoed {value!r} {secret.decode()}")

    with pytest.raises(transport.Stage23TransportError) as error:
        transport.execute_relevance(
            spec=spec,
            paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
            claim_projection={"abstract": "Abstract."},
            cited_keys=("K1",),
            metadata=(
                {
                    "cite_key": "K1",
                    "route_class": "crossref-doi",
                    "endpoint_class": "crossref",
                    "request_sha256": "b" * 64,
                    "response_sha256": "c" * 64,
                    "outbound_count": 1,
                    "status": "verified",
                    "metadata": {
                        "title": "Title",
                        "doi": "10.1000/example",
                        "arxiv_id": None,
                        "year": 2026,
                        "source": "crossref",
                    },
                },
            ),
            outbound=malicious_outbound,
        )
    assert secret.decode() not in str(error.value)


def test_metadata_request_routes_headers_encoding_and_digest_are_exact() -> None:
    crossref = transport.build_metadata_request(
        "crossref-doi", "10.1000/a/b"
    )
    assert crossref.target == "/works/10.1000%2Fa%2Fb"
    assert crossref.headers == (
        ("Host", "api.crossref.org"),
        ("User-Agent", "AutoResearchClaw-Stage23/1"),
        ("Accept", "application/json"),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    )
    openalex = transport.build_metadata_request(
        "openalex-title", "a title / x"
    )
    # OpenAlex receives the code-owned title match key; punctuation is mapped
    # to space before RFC 3986 encoding.
    assert "search=a%20title%20x" in openalex.target
    assert "select=id%2Cdoi%2Ctitle%2Cpublication_year%2Cids" in openalex.target
    assert crossref.request_sha256 == hashlib.sha256(
        crossref.canonical_transcript
    ).hexdigest()


def test_metadata_title_rejection_is_classified_malformed() -> None:
    body = _crossref_body().replace(b"Example title", b"Wrong title")
    with pytest.raises(transport.Stage23TransportError) as error:
        transport.verify_metadata(
            cite_key="Example2026",
            entry={"doi": "10.1000/example", "title": "Example title"},
            outbound=lambda _request: (_http(body),),
        )
    assert error.value.error_class == "provider_malformed"


def test_http_parser_rejects_redirect_te_cl_chunk_extension_trailer_and_truncation() -> None:
    with pytest.raises(transport.Stage23TransportError) as redirect:
        transport.parse_http_response(
            (_http(b"", status=302),),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert redirect.value.response_sha256 is not None
    te_cl = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}"
    )
    with pytest.raises(transport.Stage23TransportError):
        transport.parse_http_response(
            (te_cl,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    chunk_extension = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n2;x=1\r\n{}\r\n0\r\n\r\n"
    )
    with pytest.raises(transport.Stage23TransportError):
        transport.parse_http_response(
            (chunk_extension,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    nonempty_trailer = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\nX: y\r\n\r\n"
    )
    with pytest.raises(transport.Stage23TransportError):
        transport.parse_http_response(
            (nonempty_trailer,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    truncated = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 3\r\n\r\n{}"
    )
    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            (truncated,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert error.value.response_sha256 is not None


def test_conflicting_head_uses_invalid_head_digest_and_does_not_pull_body() -> None:
    head = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n"
    )
    pulls = 0

    def stream():
        nonlocal pulls
        pulls += 1
        yield head
        pulls += 1
        raise AssertionError("invalid head pulled entity body")

    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            stream(),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert pulls == 1
    assert error.value.response_sha256 == transport._invalid_head_digest(
        head, status="200", overflow=False
    )


def test_incomplete_head_digest_uses_already_parsed_status() -> None:
    prefix = b"HTTP/1.1 200 OK\r\nX-Incomplete: yes"
    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            (prefix,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert error.value.response_sha256 == transport._invalid_head_digest(
        prefix, status="200", overflow=False
    )


def test_header_line_overflow_digest_uses_already_parsed_status() -> None:
    prefix = b"HTTP/1.1 200 OK\r\nX-Long: " + b"x" * 8193 + b"\r\n\r\n"
    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            (prefix,),
            entity_cap=16,
            accepted_media_types=("application/json",),
        )
    captured = prefix[: prefix.find(b"X-Long: ") + 8193]
    assert error.value.response_sha256 == transport._invalid_head_digest(
        captured, status="200", overflow=False
    )


def test_head_reader_never_retains_unbounded_single_chunk_remainder() -> None:
    head = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 1000000\r\n\r\n"
    )
    stream = transport._BoundedStream((head + b"x" * 1_000_000,))
    assert stream.read_head(body_prefetch_cap=17) == head
    assert len(stream._pending) == 17
    stream = transport._BoundedStream((head, b"x" * 2_000_001))
    assert stream.read_head(body_prefetch_cap=17) == head
    assert len(stream.read_body(17)) == 17
    assert stream._pending == b""


@pytest.mark.parametrize("control", (b"\x00", b"\x01", b"\x7f"))
def test_http_parser_rejects_control_bytes_in_every_header_value(
    control: bytes,
) -> None:
    malformed = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + b"X-Bad: "
        + control
        + b"\r\nContent-Length: 2\r\n\r\n{}"
    )
    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            (malformed,),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert error.value.error_class == "provider_header_invalid"
    assert error.value.response_sha256 is not None


def test_malformed_chunk_digest_uses_dechunked_prefix_not_wire_boundaries() -> None:
    prefix = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
    )
    attacks = (
        prefix + b"2\r\nab\r\n3\r\nc",
        prefix + b"1\r\na\r\n2\r\nbc\r\n4\r\n",
    )
    digests = []
    for attack in attacks:
        with pytest.raises(transport.Stage23TransportError) as error:
            transport.parse_http_response(
                (attack,),
                entity_cap=1024,
                accepted_media_types=("application/json",),
            )
        digests.append(error.value.response_sha256)
    assert digests[0] == digests[1]


def test_response_material_then_stream_reset_is_not_pure_or_retryable() -> None:
    def interrupted():
        yield b"H"
        raise transport.PureTransportError("reset")

    with pytest.raises(transport.Stage23TransportError) as error:
        transport.parse_http_response(
            interrupted(),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )
    assert error.value.response_sha256 is not None


def test_metadata_iterator_exceptions_before_and_after_material_are_sanitized() -> None:
    entry = {"title": "Example title"}

    def before():
        raise RuntimeError("SECRET-CREDENTIAL")
        yield b""

    with pytest.raises(transport.Stage23TransportError) as first:
        transport.verify_metadata(
            cite_key="Example2026", entry=entry, outbound=lambda _request: before()
        )
    assert "SECRET-CREDENTIAL" not in str(first.value)
    assert first.value.response_sha256 is None

    def after():
        yield b"H"
        raise RuntimeError("SECRET-CREDENTIAL")

    with pytest.raises(transport.Stage23TransportError) as second:
        transport.verify_metadata(
            cite_key="Example2026", entry=entry, outbound=lambda _request: after()
        )
    assert "SECRET-CREDENTIAL" not in str(second.value)
    assert second.value.response_sha256 is not None


def test_fake_stream_binds_exact_timeout_policy_and_empty_stream_is_bounded() -> None:
    with pytest.raises(transport.PureTransportError, match="connect timeout"):
        transport.parse_http_response(
            (
                transport.TimedByteChunk(
                    b"",
                    connect_elapsed=5.000001,
                    inactivity_elapsed=0.0,
                    total_elapsed=5.000001,
                ),
            ),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )

    def empty_forever():
        while True:
            yield b""

    with pytest.raises(transport.PureTransportError, match="inactivity"):
        transport.parse_http_response(
            empty_forever(),
            entity_cap=1024,
            accepted_media_types=("application/json",),
        )


def test_http_head_and_body_caps_capture_first_over_limit_byte() -> None:
    oversized_head = b"HTTP/1.1 200 OK\r\nX:" + b"a" * 16384
    with pytest.raises(transport.Stage23TransportError) as head_error:
        transport.parse_http_response(
            (oversized_head,),
            entity_cap=16,
            accepted_media_types=("application/json",),
        )
    assert head_error.value.raw_head_prefix_size == 16385
    assert head_error.value.head_overflow is True

    body = b"x" * 17
    with pytest.raises(transport.Stage23TransportError) as body_error:
        transport.parse_http_response(
            (_http(body, content_type="application/json"),),
            entity_cap=16,
            accepted_media_types=("application/json",),
        )
    assert body_error.value.captured_entity_size == 17

    huge_length = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + b"Content-Length: "
        + b"9" * 5000
        + b"\r\n\r\n"
        + body
    )
    with pytest.raises(transport.Stage23TransportError) as length_error:
        transport.parse_http_response(
            (huge_length,),
            entity_cap=16,
            accepted_media_types=("application/json",),
        )
    assert length_error.value.error_class == "provider_oversized"
    assert length_error.value.response_sha256 is not None


def test_crossref_metadata_has_one_semantic_and_one_outbound_without_fallback() -> None:
    calls: list[transport.WireRequest] = []

    def outbound(request: transport.WireRequest):
        calls.append(request)
        return (_http(_crossref_body()),)

    result = transport.verify_metadata(
        cite_key="Example2026",
        entry={"doi": "10.1000/example", "title": "Example title"},
        outbound=outbound,
    )
    assert result["status"] == "verified"
    assert result["outbound_count"] == 1
    assert len(calls) == 1


def test_provider_response_failure_never_retries_or_falls_back() -> None:
    calls = 0

    def outbound(_request: transport.WireRequest):
        nonlocal calls
        calls += 1
        return (_http(b"{}", status=429),)

    with pytest.raises(transport.Stage23TransportError):
        transport.verify_metadata(
            cite_key="Example2026",
            entry={"doi": "10.1000/example", "title": "Example title"},
            outbound=outbound,
        )
    assert calls == 1


def _spec() -> transport.Stage23RelevanceTransportSpec:
    return transport.Stage23RelevanceTransportSpec.issue_for_test(
        provider="openai",
        model="model-1",
        base_url="https://api.example/v1",
        credential=b"fake-token",
    )


def _projection() -> dict[str, object]:
    return {
        "schema_version": 1,
        "paper_sha256": "a" * 64,
        "abstract_claim_ids": ["claim-1"],
        "abstract_sentences": ["A governed sentence."],
    }


def _metadata() -> tuple[dict[str, object], ...]:
    return (
        {
            "cite_key": "Example2026",
            "route_class": "crossref-doi",
            "endpoint_class": "crossref",
            "request_sha256": "b" * 64,
            "response_sha256": "c" * 64,
            "outbound_count": 1,
            "status": "verified",
            "metadata": {
                "title": "Example title",
                "doi": "10.1000/example",
                "arxiv_id": None,
                "year": 2026,
                "source": "crossref",
            },
        },
    )


def test_relevance_first_pure_transport_failure_can_retry_identically_once() -> None:
    requests: list[transport.WireRequest] = []

    def outbound(request: transport.WireRequest):
        requests.append(request)
        if len(requests) == 1:
            raise transport.PureTransportError("connect")
        return (_http(_relevance_body()),)

    result = transport.execute_relevance(
        spec=_spec(),
        paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
        claim_projection=_projection(),
        cited_keys=("Example2026",),
        metadata=_metadata(),
        outbound=outbound,
    )
    assert result["status"] == "complete"
    assert result["outbound_attempts"] == 2
    assert requests[0].canonical_transcript == requests[1].canonical_transcript
    assert requests[0].credential_identity is requests[1].credential_identity


def test_relevance_bad_second_response_is_failed_not_unavailable_or_third_attempt() -> None:
    calls = 0

    def outbound(request: transport.WireRequest):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise transport.PureTransportError("connect")
        return (_http(_relevance_body(scores=(("Wrong", "0.7"),))),)

    result = transport.execute_relevance(
        spec=_spec(),
        paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
        claim_projection=_projection(),
        cited_keys=("Example2026",),
        metadata=_metadata(),
        outbound=outbound,
    )
    assert result["status"] == "failed"
    assert result["outbound_attempts"] == 2
    assert result["response_sha256"] is not None
    assert calls == 2


def test_response_material_failure_never_retries() -> None:
    calls = 0

    def outbound(_request: transport.WireRequest):
        nonlocal calls
        calls += 1
        return (_http(b"{}", status=500),)

    result = transport.execute_relevance(
        spec=_spec(),
        paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
        claim_projection=_projection(),
        cited_keys=("Example2026",),
        metadata=_metadata(),
        outbound=outbound,
    )
    assert result["status"] == "failed"
    assert result["outbound_attempts"] == 1
    assert calls == 1


def test_relevance_exact_eight_key_schema_rejects_extra_missing_bool_nan_and_inf() -> None:
    complete = {
        "status": "complete",
        "semantic_calls": 1,
        "outbound_attempts": 1,
        "request_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "retry_fingerprint": None,
        "scores": [{"cite_key": "Example2026", "score": "0.500000"}],
        "error_class": None,
    }
    assert authority.validate_relevance(complete, ("Example2026",)) == complete
    for mutation in (
        {**complete, "extra": 1},
        {key: value for key, value in complete.items() if key != "scores"},
        {**complete, "semantic_calls": True},
        {**complete, "scores": [{"cite_key": "Example2026", "score": "NaN"}]},
        {**complete, "scores": [{"cite_key": "Example2026", "score": "Inf"}]},
    ):
        with pytest.raises(authority.StructuredStage23AuthorityError):
            authority.validate_relevance(mutation, ("Example2026",))


@pytest.mark.parametrize(
    ("scope", "upstream", "state", "score", "publishable", "outcome"),
    (
        ("research_release", "passed", "complete", "0.500000", True, "passed"),
        ("research_release", "degraded", "complete", "0.900000", True, "degraded"),
        ("research_release", "passed", "missing", None, False, None),
        ("research_release", "passed", "failed", None, False, None),
        ("research_release", "passed", "unavailable", None, False, None),
        ("research_release", "passed", "complete", "0.499999", False, None),
        ("pipeline_validation", "passed", "missing", None, True, "degraded"),
        ("pipeline_validation", "passed", "failed", None, True, "degraded"),
        ("exploratory", "passed", "unavailable", None, True, "degraded"),
        ("exploratory", "degraded", "complete", "0.900000", True, "degraded"),
    ),
)
def test_claim_scope_and_four_state_outcome_matrix(
    scope: str,
    upstream: str,
    state: str,
    score: str | None,
    publishable: bool,
    outcome: str | None,
) -> None:
    relevance = authority.relevance_fixture(
        state,
        cited_keys=("Example2026",),
        score=score,
    )
    assert authority.derive_outcome(
        claim_scope=scope,
        upstream_quality=upstream,
        relevance=relevance,
        cited_keys=("Example2026",),
    ) == (publishable, outcome)


def test_report_and_manifest_root_schemas_are_exact_and_ordered() -> None:
    assert len(authority.REPORT_FIELDS) == 13
    assert len(authority.MANIFEST_FIELDS) == 26
    assert authority.RELEVANCE_FIELDS == (
        "status",
        "semantic_calls",
        "outbound_attempts",
        "request_sha256",
        "response_sha256",
        "retry_fingerprint",
        "scores",
        "error_class",
    )
    assert authority.STRUCTURED_STAGE23_ARTIFACTS == (
        "verification_report.json",
        "references_verified.bib",
        "paper_final_verified.md",
        "stage23_verification_manifest.json",
    )
    assert authority.STRUCTURED_STAGE23_EVIDENCE_REFS == tuple(
        f"stage-23/{name}" for name in authority.STRUCTURED_STAGE23_ARTIFACTS
    )


def test_raw_response_and_credential_never_enter_persisted_projection() -> None:
    secret = b"super-secret"
    spec = transport.Stage23RelevanceTransportSpec.issue_for_test(
        provider="openai",
        model="model-1",
        base_url="https://api.example/v1",
        credential=secret,
    )

    def outbound(_request: transport.WireRequest):
        return (_http(_relevance_body()),)

    relevance = transport.execute_relevance(
        spec=spec,
        paper={"path": "stage-22/paper_final.md", "sha256": "a" * 64, "size": 1},
        claim_projection=_projection(),
        cited_keys=("Example2026",),
        metadata=_metadata(),
        outbound=outbound,
    )
    persisted = json.dumps(relevance, separators=(",", ":")).encode()
    assert secret not in persisted
    assert _relevance_body() not in persisted
