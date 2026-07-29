"""Private bounded HTTP/1.1 transport for inactive structured Stage 23.

The public helpers accept injected byte-stream callables.  Tests and the
inactive private executor therefore exercise exact wire bytes without making a
real provider or API call.  No SDK, generic verifier, LLMClient.chat(), proxy,
redirect, fallback, cache, or hidden retry is used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
import math
import re
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import quote
from urllib.parse import unquote, urlsplit

from researchclaw.pipeline import stage23_structured_authority as authority


MAX_REQUEST_HEAD_BYTES = 16_384
MAX_RESPONSE_HEAD_BYTES = 16_384
MAX_RESPONSE_HEADER_LINE_BYTES = 8_192
METADATA_ENTITY_CAP = {
    "crossref-doi": 1_048_576,
    "arxiv-id": 1_048_576,
    "openalex-title": 2_097_152,
}
RELEVANCE_ENTITY_CAP = 262_144
CONNECT_TIMEOUT_SECONDS = 5
READ_INACTIVITY_TIMEOUT_SECONDS = 10
TOTAL_DEADLINE_SECONDS = 15
WIRE_POLICY_ID = "stage23-openai-chat-completions-bearer-v1"
ALLOWED_RELEVANCE_PROVIDERS = frozenset(
    {
        "openai",
        "openrouter",
        "deepseek",
        "novita",
        "minimax",
        "openai-compatible",
    }
)


class PureTransportError(OSError):
    """No response status, head, or body byte was observed."""

    def __init__(self, message: str, *, error_class: str = "provider_connection") -> None:
        super().__init__(message)
        if error_class not in {
            "provider_dns",
            "provider_connection",
            "provider_tls",
            "provider_timeout",
        }:
            raise ValueError("pure transport classification is invalid")
        self.error_class = error_class


class Stage23TransportError(RuntimeError):
    """A bounded provider operation failed closed."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "provider_malformed",
        response_sha256: str | None = None,
        raw_head_prefix_size: int = 0,
        head_overflow: bool = False,
        captured_entity_size: int = 0,
        captured_entity: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.response_sha256 = response_sha256
        self.raw_head_prefix_size = raw_head_prefix_size
        self.head_overflow = head_overflow
        self.captured_entity_size = captured_entity_size
        self.captured_entity = captured_entity


@dataclass(frozen=True)
class WireRequest:
    method: str
    origin: str
    target: str
    headers: tuple[tuple[str, str], ...]
    entity: bytes
    request_sha256: str
    canonical_transcript: bytes
    credential_identity: object | None = None
    _authorization_value: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ParsedHTTPResponse:
    status: int
    content_type: str
    content_encoding: str | None
    content_length: str | None
    transfer_encoding: str | None
    entity: bytes
    response_sha256: str


@dataclass(frozen=True)
class TimedByteChunk:
    """Fake-stream event carrying the exact private transport deadlines."""

    data: bytes
    connect_elapsed: float
    inactivity_elapsed: float
    total_elapsed: float


@dataclass(frozen=True, init=False, eq=False)
class Stage23RelevanceTransportSpec:
    provider: str
    model: str
    base_url: str
    origin: str
    endpoint_path: str
    wire_api: str
    wire_policy_id: str
    credential_identity: object
    _credential: bytes

    def __init__(
        self,
        *,
        authority_token: object,
        provider: str,
        model: str,
        base_url: str,
        origin: str,
        endpoint_path: str,
        credential: bytes,
        credential_identity: object,
    ) -> None:
        if authority_token is not _SPEC_AUTHORITY:
            raise TypeError("Stage23RelevanceTransportSpec construction is private")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "endpoint_path", endpoint_path)
        object.__setattr__(self, "wire_api", "chat_completions")
        object.__setattr__(self, "wire_policy_id", WIRE_POLICY_ID)
        object.__setattr__(self, "credential_identity", credential_identity)
        object.__setattr__(self, "_credential", credential)

    @classmethod
    def issue_for_test(
        cls,
        *,
        provider: str,
        model: str,
        base_url: str,
        credential: bytes,
    ) -> "Stage23RelevanceTransportSpec":
        """Issue a held fake-credential spec; this method performs no I/O."""

        return issue_relevance_transport_spec(
            provider=provider,
            model=model,
            base_url=base_url,
            wire_api="chat_completions",
            fallback_models=(),
            bridge_enabled=False,
            extra_headers={},
            fallback_url="",
            anthropic_adapter=None,
            credential=credential,
        )


_SPEC_AUTHORITY = object()
_HTTP_STATUS_RE = re.compile(rb"HTTP/1\.[01] ([0-9]{3}) [\x20-\x7e]*\r\n\Z")
_HEADER_NAME_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


def _canonical_request_transcript(
    method: str,
    origin: str,
    target: str,
    headers: Sequence[tuple[str, str]],
    entity: bytes,
) -> bytes:
    return (
        method.encode("ascii")
        + b"\n"
        + origin.encode("ascii")
        + b"\n"
        + target.encode("ascii")
        + b"\n"
        + b"".join(
            name.encode("ascii") + b": " + value.encode("ascii") + b"\n"
            for name, value in headers
        )
        + b"\n"
        + entity
    )


def _wire_request(
    *,
    method: str,
    origin: str,
    target: str,
    headers: tuple[tuple[str, str], ...],
    entity: bytes,
    credential_identity: object | None = None,
    authorization_value: bytes | None = None,
) -> WireRequest:
    transcript = _canonical_request_transcript(
        method, origin, target, headers, entity
    )
    request_head_size = (
        len(method.encode("ascii"))
        + 1
        + len(target.encode("ascii"))
        + len(b" HTTP/1.1\r\n")
        + sum(
            len(name.encode("ascii")) + 2 + len(value.encode("ascii")) + 2
            for name, value in headers
        )
        + (
            len(b"Authorization: ")
            + len(authorization_value)
            + 2
            if authorization_value is not None
            else 0
        )
        + 2
    )
    if request_head_size > MAX_REQUEST_HEAD_BYTES:
        raise Stage23TransportError(
            "request head exceeds 16384 bytes",
            error_class="provider_header_invalid",
        )
    return WireRequest(
        method,
        origin,
        target,
        headers,
        entity,
        hashlib.sha256(transcript).hexdigest(),
        transcript,
        credential_identity,
        authorization_value,
    )


def _encode_component(value: str) -> str:
    return quote(value, safe="-._~", encoding="utf-8", errors="strict")


def build_metadata_request(route: str, identity: str) -> WireRequest:
    if route == "crossref-doi":
        origin = "https://api.crossref.org"
        target = f"/works/{_encode_component(authority.normalize_doi(identity))}"
        accept = "application/json"
    elif route == "arxiv-id":
        origin = "https://export.arxiv.org"
        normalized, _, _ = authority.normalize_arxiv_request(identity)
        target = (
            f"/api/query?id_list={_encode_component(normalized)}"
            "&start=0&max_results=2"
        )
        accept = "application/atom+xml"
    elif route == "openalex-title":
        origin = "https://api.openalex.org"
        key = authority.title_match_key(identity)
        target = (
            f"/works?search={_encode_component(key)}"
            "&select=id%2Cdoi%2Ctitle%2Cpublication_year%2Cids&per-page=200"
        )
        accept = "application/json"
    else:
        raise Stage23TransportError("metadata route is not admitted")
    authority_name = origin.removeprefix("https://")
    headers = (
        ("Host", authority_name),
        ("User-Agent", "AutoResearchClaw-Stage23/1"),
        ("Accept", accept),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    )
    return _wire_request(
        method="GET",
        origin=origin,
        target=target,
        headers=headers,
        entity=b"",
    )


class _BoundedStream:
    def __init__(self, chunks: Iterable[bytes | TimedByteChunk]) -> None:
        self._iterator = iter(chunks)
        self._pending = b""
        self._empty_events = 0
        self.material_observed = False
        self.interrupted = False

    def _next_bytes(self) -> bytes | None:
        while True:
            try:
                event = next(self._iterator)
            except StopIteration:
                return None
            except Exception:
                if self.material_observed:
                    self.interrupted = True
                    return None
                raise
            if type(event) is TimedByteChunk:
                for elapsed in (
                    event.connect_elapsed,
                    event.inactivity_elapsed,
                    event.total_elapsed,
                ):
                    if (
                        type(elapsed) not in {int, float}
                        or not math.isfinite(elapsed)
                        or elapsed < 0
                    ):
                        raise Stage23TransportError(
                            "fake transport timing is invalid"
                        )
                violation: str | None = None
                if event.connect_elapsed > CONNECT_TIMEOUT_SECONDS:
                    violation = "connect timeout"
                elif event.inactivity_elapsed > READ_INACTIVITY_TIMEOUT_SECONDS:
                    violation = "read inactivity timeout"
                elif event.total_elapsed > TOTAL_DEADLINE_SECONDS:
                    violation = "total deadline timeout"
                if violation is not None:
                    if self.material_observed:
                        self.interrupted = True
                        return None
                    raise PureTransportError(
                        violation, error_class="provider_timeout"
                    )
                chunk = event.data
            else:
                chunk = event
            if type(chunk) is not bytes:
                raise Stage23TransportError("response stream yielded non-bytes")
            if not chunk:
                self._empty_events += 1
                if self._empty_events > 1024:
                    if self.material_observed:
                        self.interrupted = True
                        return None
                    raise PureTransportError(
                        "read inactivity timeout", error_class="provider_timeout"
                    )
                continue
            self._empty_events = 0
            self.material_observed = True
            return chunk

    def read_head(self, *, body_prefetch_cap: int) -> bytes:
        captured = bytearray()
        while len(captured) < MAX_RESPONSE_HEAD_BYTES + 1:
            chunk = self._pending or self._next_bytes()
            self._pending = b""
            if chunk is None:
                break
            remaining = MAX_RESPONSE_HEAD_BYTES + 1 - len(captured)
            before = len(captured)
            captured.extend(chunk[:remaining])
            terminator = captured.find(b"\r\n\r\n")
            if terminator >= 0:
                head_end = terminator + 4
                chunk_body_start = max(0, head_end - before)
                self._pending = chunk[
                    chunk_body_start : chunk_body_start + body_prefetch_cap
                ]
                return bytes(captured[:head_end])
        return bytes(captured)

    def read_body(self, absolute_cap: int) -> bytes:
        captured = bytearray()
        while len(captured) < absolute_cap:
            chunk = self._pending or self._next_bytes()
            self._pending = b""
            if chunk is None:
                break
            remaining = absolute_cap - len(captured)
            captured.extend(chunk[:remaining])
        return bytes(captured)


def _collect_bounded(
    chunks: Iterable[bytes | TimedByteChunk],
    *,
    absolute_cap: int,
) -> tuple[bytes, bool]:
    """Compatibility helper for bounded non-HTTP collectors."""

    captured = bytearray()
    stream = _BoundedStream(chunks)
    while len(captured) < absolute_cap:
        chunk = stream._next_bytes()
        if chunk is None:
            break
        captured.extend(chunk[: absolute_cap - len(captured)])
    return bytes(captured), stream.interrupted


def _invalid_head_digest(
    prefix: bytes,
    *,
    status: str,
    overflow: bool,
) -> str:
    transcript = (
        status.encode("ascii")
        + b"\ninvalid-head\nraw-head-prefix-sha256:"
        + hashlib.sha256(prefix).hexdigest().encode("ascii")
        + b"\nraw-head-prefix-size:"
        + str(len(prefix)).encode("ascii")
        + b"\nhead-overflow:"
        + (b"true" if overflow else b"false")
        + b"\nselected-fields:unavailable\n\n"
    )
    return hashlib.sha256(transcript).hexdigest()


def _raise_invalid_head(
    prefix: bytes,
    message: str,
    *,
    status: str = "000",
    overflow: bool = False,
) -> None:
    raise Stage23TransportError(
        message,
        error_class="provider_header_invalid",
        response_sha256=_invalid_head_digest(
            prefix, status=status, overflow=overflow
        ),
        raw_head_prefix_size=len(prefix),
        head_overflow=overflow,
    )


def _status_or_000(prefix: bytes) -> str:
    end = prefix.find(b"\r\n")
    if end < 0:
        return "000"
    match = _HTTP_STATUS_RE.fullmatch(prefix[: end + 2])
    return match.group(1).decode("ascii") if match is not None else "000"


def _parse_content_type(value: str, admitted: Sequence[str]) -> str:
    parts = [part.strip() for part in value.split(";")]
    media = parts[0].lower()
    if media not in {item.lower() for item in admitted}:
        raise Stage23TransportError(
            "response content type is not admitted",
            error_class="provider_content_type",
        )
    if len(parts) > 2 or (
        len(parts) == 2
        and parts[1].lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise Stage23TransportError(
            "response content type parameters are invalid",
            error_class="provider_content_type",
        )
    return value.strip()


def parse_http_response(
    chunks: Iterable[bytes],
    *,
    entity_cap: int,
    accepted_media_types: Sequence[str],
) -> ParsedHTTPResponse:
    if type(entity_cap) is not int or entity_cap <= 0:
        raise Stage23TransportError("entity cap is invalid")
    # The bounded collector retains enough bytes for the exact 16385-byte head
    # overflow prefix and at most entity_cap+1 entity bytes.
    stream = _BoundedStream(chunks)
    wire_cap = 6 * (entity_cap + 1) + 5
    try:
        head = stream.read_head(body_prefetch_cap=wire_cap)
    except (PureTransportError, Stage23TransportError):
        raise
    except Exception:
        raise Stage23TransportError(
            "transport stream system failure",
            error_class="provider_connection",
        ) from None
    if not head:
        raise PureTransportError("no response material")
    terminator = head.find(b"\r\n\r\n")
    if terminator < 0:
        prefix = head[: MAX_RESPONSE_HEAD_BYTES + 1]
        overflow = len(prefix) == MAX_RESPONSE_HEAD_BYTES + 1
        _raise_invalid_head(
            prefix,
            "response head is incomplete or oversized",
            status=_status_or_000(prefix),
            overflow=overflow,
        )
    head_end = terminator + 4
    if head_end > MAX_RESPONSE_HEAD_BYTES:
        _raise_invalid_head(
            head[: MAX_RESPONSE_HEAD_BYTES + 1],
            "response head exceeds 16384 bytes",
            status=_status_or_000(head),
            overflow=True,
        )
    lines = head.split(b"\r\n")
    if any(len(line) > MAX_RESPONSE_HEADER_LINE_BYTES for line in lines[:-2]):
        first = next(line for line in lines[:-2] if len(line) > 8192)
        prefix_size = min(head.find(first) + 8193, 16385)
        _raise_invalid_head(
            head[:prefix_size],
            "response header line exceeds 8192 bytes",
            status=_status_or_000(head),
        )
    status_line = lines[0] + b"\r\n"
    status_match = _HTTP_STATUS_RE.fullmatch(status_line)
    status_text = (
        status_match.group(1).decode("ascii") if status_match is not None else "000"
    )
    if status_match is None:
        _raise_invalid_head(status_line, "response status line is invalid")
    status = int(status_text)
    selected: dict[str, str] = {}
    all_names: set[str] = set()
    line_cursor = len(status_line)
    for line in lines[1:-2]:
        abort_prefix = head[: line_cursor + len(line) + 2]
        line_cursor += len(line) + 2
        if b":" not in line:
            _raise_invalid_head(
                abort_prefix,
                "response header line is malformed",
                status=status_text,
            )
        name_bytes, value_bytes = line.split(b":", 1)
        if _HEADER_NAME_RE.fullmatch(name_bytes) is None:
            _raise_invalid_head(
                abort_prefix,
                "response header name is malformed",
                status=status_text,
            )
        try:
            name = name_bytes.decode("ascii").lower()
            value = value_bytes.decode("ascii").strip(" \t")
        except UnicodeDecodeError:
            _raise_invalid_head(
                abort_prefix,
                "response header is not ASCII",
                status=status_text,
            )
        if any(byte < 0x20 and byte != 0x09 or byte == 0x7F for byte in value_bytes):
            _raise_invalid_head(
                abort_prefix,
                "response header value contains a control byte",
                status=status_text,
            )
        if name in all_names:
            _raise_invalid_head(
                abort_prefix,
                "duplicate response header",
                status=status_text,
            )
        all_names.add(name)
        if name in {
            "content-type",
            "content-encoding",
            "content-length",
            "transfer-encoding",
        }:
            selected[name] = value
    content_encoding = selected.get("content-encoding")
    transfer = selected.get("transfer-encoding")
    content_length = selected.get("content-length")
    if transfer is not None and (
        transfer.lower() != "chunked" or content_length is not None
    ):
        _raise_invalid_head(
            head,
            "response transfer framing is invalid",
            status=status_text,
        )
    if transfer is None and content_length is None:
        _raise_invalid_head(
            head,
            "response has no canonical framing",
            status=status_text,
        )
    if content_length is not None and re.fullmatch(
        r"0|[1-9][0-9]*", content_length
    ) is None:
        _raise_invalid_head(
            head,
            "Content-Length is not canonical",
            status=status_text,
        )
    body_wire = stream.read_body(wire_cap)
    stream_interrupted = stream.interrupted
    if content_encoding is not None and content_encoding.lower() != "identity":
        captured = body_wire[: entity_cap + 1]
        raise Stage23TransportError(
            "response content encoding is invalid",
            error_class="provider_content_encoding",
            response_sha256=_normal_response_digest(
                status, selected, captured
            ),
            captured_entity_size=len(captured),
            captured_entity=captured,
        )
    if transfer is not None:
        try:
            body = _dechunk(body_wire, entity_cap=entity_cap)
        except Stage23TransportError as exc:
            if exc.response_sha256 is None:
                exc.response_sha256 = _normal_response_digest(
                    status, selected, exc.captured_entity
                )
            raise
    else:
        body = body_wire[: entity_cap + 1]
        assert content_length is not None
        cap_text = str(entity_cap)
        oversized_length = len(content_length) > len(cap_text) or (
            len(content_length) == len(cap_text)
            and content_length > cap_text
        )
        if oversized_length:
            captured = body[: entity_cap + 1]
            raise Stage23TransportError(
                "response entity exceeds cap",
                error_class="provider_oversized",
                response_sha256=_normal_response_digest(
                    status, selected, captured
                ),
                captured_entity_size=len(captured),
            )
        expected_length = int(content_length)
        if len(body) != expected_length:
            raise Stage23TransportError(
                "response entity is truncated",
                error_class="provider_truncated",
                response_sha256=_normal_response_digest(status, selected, body),
                captured_entity_size=len(body),
            )
    digest = _normal_response_digest(status, selected, body)
    if stream_interrupted:
        raise Stage23TransportError(
            "response stream was interrupted after material",
            error_class="provider_truncated",
            response_sha256=digest,
            captured_entity_size=len(body),
            captured_entity=body,
        )
    if status != 200:
        error_class = "provider_redirect" if 300 <= status <= 399 else "provider_http_status"
        raise Stage23TransportError(
            "provider HTTP status is not 200",
            error_class=error_class,
            response_sha256=digest,
            captured_entity_size=len(body),
        )
    content_type = selected.get("content-type")
    if content_type is None:
        raise Stage23TransportError(
            "response Content-Type is absent",
            error_class="provider_content_type",
            response_sha256=digest,
            captured_entity_size=len(body),
        )
    try:
        parsed_type = _parse_content_type(content_type, accepted_media_types)
    except Stage23TransportError as exc:
        exc.response_sha256 = digest
        exc.captured_entity_size = len(body)
        raise
    return ParsedHTTPResponse(
        status,
        parsed_type,
        content_encoding,
        content_length,
        transfer,
        body,
        digest,
    )


def _normal_response_digest(
    status: int,
    selected: Mapping[str, str],
    body: bytes,
) -> str:
    transcript = (
        f"{status:03d}\n".encode("ascii")
        + b"Content-Type: "
        + selected.get("content-type", "<absent>").encode("ascii", "strict")
        + b"\nContent-Encoding: "
        + selected.get("content-encoding", "<absent>").encode("ascii", "strict")
        + b"\nContent-Length: "
        + selected.get("content-length", "<absent>").encode("ascii", "strict")
        + b"\nTransfer-Encoding: "
        + selected.get("transfer-encoding", "<absent>").encode("ascii", "strict")
        + b"\n\n"
        + body
    )
    return hashlib.sha256(transcript).hexdigest()


def _dechunk(value: bytes, *, entity_cap: int) -> bytes:
    position = 0
    result = bytearray()
    while True:
        end = value.find(b"\r\n", position)
        if end < 0:
            raise Stage23TransportError(
                "chunk-size line is truncated",
                error_class="provider_truncated",
                captured_entity_size=len(result),
                captured_entity=bytes(result),
            )
        size_line = value[position:end]
        if b";" in size_line or re.fullmatch(rb"0|[1-9A-Fa-f][0-9A-Fa-f]*", size_line) is None:
            raise Stage23TransportError(
                "chunk size is invalid",
                error_class="provider_header_invalid",
                captured_entity_size=len(result),
                captured_entity=bytes(result),
            )
        size = int(size_line, 16)
        position = end + 2
        if size == 0:
            if value[position:] != b"\r\n":
                raise Stage23TransportError(
                    "chunk trailer is nonempty or truncated",
                    error_class="provider_header_invalid",
                    captured_entity_size=len(result),
                    captured_entity=bytes(result),
                )
            return bytes(result)
        if position + size + 2 > len(value):
            available = value[position : min(len(value), position + size)]
            result.extend(available[: max(0, entity_cap + 1 - len(result))])
            raise Stage23TransportError(
                "chunk body is truncated",
                error_class="provider_truncated",
                captured_entity_size=len(result),
                captured_entity=bytes(result),
            )
        result.extend(
            value[position : position + size][
                : max(0, entity_cap + 1 - len(result))
            ]
        )
        if len(result) > entity_cap:
            raise Stage23TransportError(
                "response entity exceeds cap",
                error_class="provider_oversized",
                captured_entity_size=len(result),
                captured_entity=bytes(result),
            )
        position += size
        if value[position : position + 2] != b"\r\n":
            raise Stage23TransportError(
                "chunk delimiter is invalid",
                error_class="provider_header_invalid",
                captured_entity_size=len(result),
                captured_entity=bytes(result),
            )
        position += 2


def verify_metadata(
    *,
    cite_key: str,
    entry: Mapping[str, object],
    outbound: Callable[[WireRequest], Iterable[bytes]],
) -> dict[str, object]:
    route, endpoint, identity = authority.select_route(entry)
    request = build_metadata_request(route, identity)
    try:
        chunks = outbound(request)
    except PureTransportError:
        raise
    except Exception:
        raise Stage23TransportError(
            "metadata pure transport failed",
            error_class="provider_connection",
        ) from None
    admitted = {
        "crossref-doi": (
            "application/json",
            "application/vnd.crossref-api-message+json",
        ),
        "arxiv-id": ("application/atom+xml", "application/xml"),
        "openalex-title": ("application/json",),
    }[route]
    response = parse_http_response(
        chunks,
        entity_cap=METADATA_ENTITY_CAP[route],
        accepted_media_types=admitted,
    )
    try:
        if route == "crossref-doi":
            metadata = authority.parse_crossref_response(
                response.entity,
                requested_doi=identity,
                held_title=str(entry.get("title") or ""),
            )
        elif route == "arxiv-id":
            metadata = authority.parse_arxiv_response(
                response.entity,
                requested=identity,
                held_title=str(entry.get("title") or ""),
            )
        else:
            metadata = authority.parse_openalex_response(
                response.entity,
                held_title=str(entry.get("title") or ""),
            )
    except authority.StructuredStage23AuthorityError as exc:
        detail = str(exc).lower()
        if "ambiguous" in detail:
            error_class = "provider_ambiguous"
        elif "title" in detail:
            error_class = "provider_malformed"
        elif any(
            marker in detail
            for marker in (
                "identity mismatch",
                "base identity mismatch",
                "version identity mismatch",
                "exact identity mismatch",
            )
        ):
            error_class = "provider_identity_mismatch"
        else:
            error_class = "provider_malformed"
        raise Stage23TransportError(
            "metadata identity projection rejected",
            error_class=error_class,
            response_sha256=response.response_sha256,
            captured_entity_size=len(response.entity),
        ) from exc
    return {
        "cite_key": cite_key,
        "route_class": route,
        "endpoint_class": endpoint,
        "request_sha256": request.request_sha256,
        "response_sha256": response.response_sha256,
        "outbound_count": 1,
        "status": "verified",
        "metadata": metadata,
    }


def _strict_base_url(base_url: object) -> tuple[str, str]:
    if type(base_url) is not str or not base_url.startswith("https://"):
        raise Stage23TransportError("structured relevance base URL is invalid")
    if (
        base_url.endswith("/")
        or "?" in base_url
        or "#" in base_url
        or "\\" in base_url
        or "@" in base_url.removeprefix("https://").split("/", 1)[0]
    ):
        raise Stage23TransportError("structured relevance base URL is invalid")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        raw_ascii = base_url.encode("ascii")
    except (UnicodeEncodeError, ValueError) as exc:
        raise Stage23TransportError(
            "structured relevance base URL is invalid"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.hostname != parsed.hostname.lower()
        or parsed.netloc.endswith(":")
        or any(byte < 0x21 or byte > 0x7E for byte in raw_ascii)
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise Stage23TransportError("structured relevance base URL is invalid")
    authority_name = parsed.netloc
    prefix = parsed.path.removeprefix("/")
    separator = "/" if parsed.path else ""
    segments = prefix.split("/") if separator else []
    for segment in segments:
        if re.search(r"%(?![0-9A-Fa-f]{2})", segment):
            raise Stage23TransportError("structured relevance base URL is invalid")
        decoded = unquote(segment)
        if (
            not segment
            or decoded in {".", ".."}
            or "/" in decoded
            or "\\" in decoded
        ):
            raise Stage23TransportError("structured relevance base URL is invalid")
    if (
        not authority_name
        or authority_name.lower() != authority_name
        or any(
            token in base_url.lower()
            for token in ("%2f", "%5c", "/chat/completions")
        )
    ):
        raise Stage23TransportError("structured relevance base URL is invalid")
    origin = f"https://{authority_name}"
    endpoint = f"/{prefix}/chat/completions" if separator else "/chat/completions"
    return origin, endpoint


def issue_relevance_transport_spec(
    *,
    provider: object,
    model: object,
    base_url: object,
    wire_api: object,
    fallback_models: object,
    bridge_enabled: object,
    extra_headers: object,
    fallback_url: object,
    anthropic_adapter: object,
    credential: object,
) -> Stage23RelevanceTransportSpec:
    if provider not in ALLOWED_RELEVANCE_PROVIDERS:
        raise Stage23TransportError("structured relevance provider is not admitted")
    if wire_api != "chat_completions":
        raise Stage23TransportError("structured relevance wire API is not admitted")
    if type(fallback_models) is not tuple or fallback_models:
        raise Stage23TransportError("structured relevance fallback models forbidden")
    if bridge_enabled is not False:
        raise Stage23TransportError("structured relevance bridge is forbidden")
    if type(extra_headers) is not dict or extra_headers or fallback_url != "":
        raise Stage23TransportError("structured relevance alternate headers forbidden")
    if anthropic_adapter is not None:
        raise Stage23TransportError("structured relevance Anthropic adapter forbidden")
    if type(model) is not str:
        raise Stage23TransportError("structured relevance model is invalid")
    try:
        model_bytes = model.encode("ascii")
    except UnicodeEncodeError as exc:
        raise Stage23TransportError("structured relevance model is invalid") from exc
    if not 1 <= len(model_bytes) <= 256 or any(
        byte < 0x21 or byte > 0x7E for byte in model_bytes
    ):
        raise Stage23TransportError("structured relevance model is invalid")
    if type(credential) is not bytes or not 1 <= len(credential) <= 4096 or any(
        byte < 0x21 or byte > 0x7E for byte in credential
    ):
        raise Stage23TransportError("structured relevance credential is invalid")
    origin, endpoint = _strict_base_url(base_url)
    credential_identity = object()
    return Stage23RelevanceTransportSpec(
        authority_token=_SPEC_AUTHORITY,
        provider=provider,
        model=model,
        base_url=base_url,
        origin=origin,
        endpoint_path=endpoint,
        credential=bytes(credential),
        credential_identity=credential_identity,
    )


_SYSTEM_MESSAGE = (
    "Score each ordered citation for relevance to the bounded governed abstract "
    "claims. Return only the required JSON object."
)


def _build_relevance_request(
    *,
    spec: Stage23RelevanceTransportSpec,
    paper: Mapping[str, object],
    claim_projection: Mapping[str, object],
    cited_keys: Sequence[str],
    metadata: Sequence[Mapping[str, object]],
) -> WireRequest:
    keys = authority.validate_cited_keys(tuple(cited_keys))
    if len(metadata) != len(keys):
        raise Stage23TransportError("relevance metadata closure mismatch")
    for key, item in zip(keys, metadata, strict=True):
        authority.validate_citation_projection(item, expected_key=key)
    user = {
        "schema_version": 1,
        "task": "stage23_paper_relevance",
        "paper": dict(paper),
        "claim_projection": dict(claim_projection),
        "cited_keys": list(keys),
        "metadata": [dict(item) for item in metadata],
    }
    user_bytes = authority.canonical_json_bytes(user)
    projection_bytes = authority.canonical_json_bytes(claim_projection)
    if len(projection_bytes) > 16_384:
        raise Stage23TransportError("bounded claim projection exceeds 16384 bytes")
    entity_value = {
        "model": spec.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_MESSAGE},
            {"role": "user", "content": user_bytes.decode("utf-8")},
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    entity = authority.canonical_json_bytes(entity_value)
    if len(entity) > 131_072:
        raise Stage23TransportError("relevance request entity exceeds cap")
    host = spec.origin.removeprefix("https://")
    headers = (
        ("Host", host),
        ("User-Agent", "AutoResearchClaw-Stage23/1"),
        ("Accept", "application/json"),
        ("Accept-Encoding", "identity"),
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(entity))),
        ("Connection", "close"),
    )
    authorization = b"Bearer " + spec._credential
    return _wire_request(
        method="POST",
        origin=spec.origin,
        target=spec.endpoint_path,
        headers=headers,
        entity=entity,
        credential_identity=spec.credential_identity,
        authorization_value=authorization,
    )


def _parse_relevance_response(
    content: bytes,
    *,
    model: str,
    cited_keys: Sequence[str],
) -> list[dict[str, str]]:
    envelope = authority.strict_json_object(content, "relevance response")
    if envelope.get("model") != model:
        raise authority.StructuredStage23AuthorityError(
            "relevance response model mismatch"
        )
    choices = envelope.get("choices")
    if type(choices) is not list or len(choices) != 1:
        raise authority.StructuredStage23AuthorityError(
            "relevance choice closure mismatch"
        )
    choice = choices[0]
    if type(choice) is not dict or choice.get("finish_reason") != "stop":
        raise authority.StructuredStage23AuthorityError(
            "relevance finish reason mismatch"
        )
    message = choice.get("message")
    if (
        type(message) is not dict
        or set(message) != {"role", "content"}
        or message.get("role") != "assistant"
        or type(message.get("content")) is not str
    ):
        raise authority.StructuredStage23AuthorityError(
            "relevance assistant message mismatch"
        )
    result = authority.strict_json_object(
        message["content"].encode("utf-8"), "relevance content"
    )
    if tuple(result) != ("scores",) or type(result["scores"]) is not list:
        raise authority.StructuredStage23AuthorityError(
            "relevance content fields mismatch"
        )
    scores: list[dict[str, str]] = []
    for item in result["scores"]:
        if type(item) is not dict or tuple(item) != ("cite_key", "score"):
            raise authority.StructuredStage23AuthorityError(
                "relevance score fields mismatch"
            )
        scores.append({"cite_key": item["cite_key"], "score": item["score"]})
    candidate = {
        "status": "complete",
        "semantic_calls": 1,
        "outbound_attempts": 1,
        "request_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "retry_fingerprint": None,
        "scores": scores,
        "error_class": None,
    }
    authority.validate_relevance(candidate, cited_keys)
    return scores


def execute_relevance(
    *,
    spec: Stage23RelevanceTransportSpec,
    paper: Mapping[str, object],
    claim_projection: Mapping[str, object],
    cited_keys: Sequence[str],
    metadata: Sequence[Mapping[str, object]],
    outbound: Callable[[WireRequest], Iterable[bytes]],
) -> dict[str, object]:
    if type(spec) is not Stage23RelevanceTransportSpec:
        raise Stage23TransportError("relevance transport spec identity is invalid")
    request = _build_relevance_request(
        spec=spec,
        paper=paper,
        claim_projection=claim_projection,
        cited_keys=cited_keys,
        metadata=metadata,
    )
    attempts = 0
    for ordinal in (1, 2):
        attempts += 1
        try:
            chunks = outbound(request)
            response = parse_http_response(
                chunks,
                entity_cap=RELEVANCE_ENTITY_CAP,
                accepted_media_types=("application/json",),
            )
        except PureTransportError:
            if ordinal == 1:
                continue
            return {
                "status": "unavailable",
                "semantic_calls": 1,
                "outbound_attempts": attempts,
                "request_sha256": request.request_sha256,
                "response_sha256": None,
                "retry_fingerprint": request.request_sha256,
                "scores": [],
                "error_class": "pure_transport_exhausted",
            }
        except Stage23TransportError as exc:
            if exc.response_sha256 is None:
                # A non-PureTransportError without response material is a
                # policy/system failure, not a persisted relevance state.
                raise
            return {
                "status": "failed",
                "semantic_calls": 1,
                "outbound_attempts": attempts,
                "request_sha256": request.request_sha256,
                "response_sha256": exc.response_sha256,
                "retry_fingerprint": (
                    request.request_sha256 if attempts == 2 else None
                ),
                "scores": [],
                "error_class": "relevance_semantic_failure",
            }
        except Exception:
            raise Stage23TransportError(
                "relevance outbound system failure"
            ) from None
        try:
            scores = _parse_relevance_response(
                response.entity,
                model=spec.model,
                cited_keys=cited_keys,
            )
        except authority.StructuredStage23AuthorityError:
            return {
                "status": "failed",
                "semantic_calls": 1,
                "outbound_attempts": attempts,
                "request_sha256": request.request_sha256,
                "response_sha256": response.response_sha256,
                "retry_fingerprint": (
                    request.request_sha256 if attempts == 2 else None
                ),
                "scores": [],
                "error_class": "relevance_semantic_failure",
            }
        result = {
            "status": "complete",
            "semantic_calls": 1,
            "outbound_attempts": attempts,
            "request_sha256": request.request_sha256,
            "response_sha256": response.response_sha256,
            "retry_fingerprint": request.request_sha256 if attempts == 2 else None,
            "scores": scores,
            "error_class": None,
        }
        return authority.validate_relevance(result, cited_keys)
    raise Stage23TransportError("relevance outbound bound violated")
