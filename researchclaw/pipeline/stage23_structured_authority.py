"""Strict code-owned authority helpers for inactive structured Stage 23.

This module deliberately contains no filesystem or network operations.  It
normalizes provider identities, parses bounded provider projections, validates
the exact report/relevance/manifest schemas, and derives the frozen outcome
matrix from Section 18.13.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import io
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Mapping, Sequence


class StructuredStage23AuthorityError(RuntimeError):
    """A structured Stage 23 authority value is malformed or ambiguous."""


STRUCTURED_STAGE23_ARTIFACTS = (
    "verification_report.json",
    "references_verified.bib",
    "paper_final_verified.md",
    "stage23_verification_manifest.json",
)
STRUCTURED_STAGE23_EVIDENCE_REFS = tuple(
    f"stage-23/{name}" for name in STRUCTURED_STAGE23_ARTIFACTS
)

REPORT_FIELDS = (
    "schema_version",
    "publication_stage_id",
    "paper",
    "bibliography",
    "cited_count",
    "cited_keys",
    "claim_scope",
    "provider_policy",
    "relevance_policy",
    "citations",
    "relevance",
    "outcome",
    "generated",
)
RELEVANCE_FIELDS = (
    "status",
    "semantic_calls",
    "outbound_attempts",
    "request_sha256",
    "response_sha256",
    "retry_fingerprint",
    "scores",
    "error_class",
)
MANIFEST_FIELDS = (
    "schema_version",
    "publication_stage_id",
    "publication_mode",
    "structured_capability_schema_version",
    "structured_capability_snapshot",
    "generation_binding_sha256",
    "canonical_experiment_evidence",
    "cfs",
    "source_stage19_manifest",
    "source_stage20_manifest",
    "source_stage21_manifest",
    "source_stage22_manifest",
    "source_paper",
    "source_bibliography",
    "source_latex",
    "quality_outcome",
    "degradation_signal",
    "claim_scope",
    "verification_policy",
    "outcome",
    "verification_report",
    "verified_bibliography",
    "verified_paper",
    "output_count",
    "outputs",
    "generated",
)
CITATION_FIELDS = (
    "cite_key",
    "route_class",
    "endpoint_class",
    "request_sha256",
    "response_sha256",
    "outbound_count",
    "status",
    "metadata",
)
METADATA_FIELDS = ("title", "doi", "arxiv_id", "year", "source")
FILE_REF_FIELDS = ("path", "sha256", "size")
OUTPUT_FIELDS = ("role", "logical_name", "path", "sha256", "size")

PROVIDER_POLICY = {
    "route_policy": "doi-else-arxiv-else-title-v1",
    "max_outbound_per_citation": 1,
    "fallback": False,
    "retry": False,
    "cache": False,
}
RELEVANCE_POLICY = {
    "max_semantic_calls": 1,
    "max_outbound_attempts": 2,
    "retry_class": "pure-transport-before-response-only",
    "threshold": "0.500000",
}
VERIFICATION_POLICY = {
    "route_policy": "doi-else-arxiv-else-title-v1",
    "min_cited_count": 1,
    "max_cited_count": 32,
    "max_provider_outbound_per_citation": 1,
    "provider_fallback": False,
    "provider_retry": False,
    "provider_cache": False,
    "relevance_max_semantic_calls": 1,
    "relevance_max_outbound_attempts": 2,
    "relevance_retry_class": "pure-transport-before-response-only",
    "relevance_threshold": "0.500000",
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:a-z0-9]+\Z")
_ARXIV_NEW_RE = re.compile(r"([0-9]{4}\.[0-9]{4,5})(v[1-9][0-9]*)?\Z")
_ARXIV_OLD_RE = re.compile(
    r"([a-z][a-z0-9-]*(?:\.[a-z]{2})?/[0-9]{7})(v[1-9][0-9]*)?\Z"
)
_SCORE_RE = re.compile(r"[01]\.[0-9]{6}\Z")
_CITE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}\Z")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise StructuredStage23AuthorityError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise StructuredStage23AuthorityError(f"nonfinite JSON constant: {value}")


def strict_json_value(content: bytes, label: str) -> object:
    if type(content) is not bytes or content.startswith(b"\xef\xbb\xbf"):
        raise StructuredStage23AuthorityError(f"{label} is not strict UTF-8 JSON")
    try:
        text = content.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StructuredStage23AuthorityError(f"{label} is malformed JSON") from exc
    _validate_json_bounds(value, depth=0, aggregate=[0])
    return value


def strict_json_object(content: bytes, label: str) -> dict[str, object]:
    value = strict_json_value(content, label)
    if type(value) is not dict:
        raise StructuredStage23AuthorityError(f"{label} is not an object")
    return value


def _validate_json_bounds(value: object, *, depth: int, aggregate: list[int]) -> None:
    if depth > 16:
        raise StructuredStage23AuthorityError("JSON depth exceeds 16")
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise StructuredStage23AuthorityError("JSON string is not scalar UTF-8") from exc
        if size > 16_384:
            raise StructuredStage23AuthorityError("JSON string exceeds 16384 bytes")
        return
    if type(value) in (dict, list):
        aggregate[0] += 1
        if aggregate[0] > 4096:
            raise StructuredStage23AuthorityError(
                "JSON aggregate container count exceeds 4096"
            )
        if type(value) is dict:
            for key in value:
                _validate_json_bounds(
                    key, depth=depth + 1, aggregate=aggregate
                )
        for item in value.values() if type(value) is dict else value:
            _validate_json_bounds(item, depth=depth + 1, aggregate=aggregate)
        return
    if type(value) is float and not math.isfinite(value):
        raise StructuredStage23AuthorityError("JSON number is nonfinite")
    if value is not None and type(value) not in (bool, int, float):
        raise StructuredStage23AuthorityError("unsupported JSON value")


def require_true_int(value: object, expected: int, label: str) -> int:
    if type(value) is not int or value != expected:
        raise StructuredStage23AuthorityError(f"{label} must be true integer {expected}")
    return value


def _year(value: object, label: str = "year") -> int:
    if type(value) is not int or not 1000 <= value <= 2999:
        raise StructuredStage23AuthorityError(f"{label} is invalid")
    return value


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise StructuredStage23AuthorityError(f"{label} is not lowercase SHA-256")
    return value


_UNICODE_WHITE_SPACE = frozenset(
    {
        "\u0009",
        "\u000a",
        "\u000b",
        "\u000c",
        "\u000d",
        "\u0020",
        "\u0085",
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
    }
)


def _trim_unicode_white_space(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and value[start] in _UNICODE_WHITE_SPACE:
        start += 1
    while end > start and value[end - 1] in _UNICODE_WHITE_SPACE:
        end -= 1
    return value[start:end]


def _collapse_unicode_white_space(value: str) -> str:
    output: list[str] = []
    pending_space = False
    for char in value:
        if char in _UNICODE_WHITE_SPACE:
            pending_space = bool(output)
            continue
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(char)
    return "".join(output)


def normalize_doi(value: object) -> str:
    if type(value) is not str:
        raise StructuredStage23AuthorityError("DOI must be one string")
    normalized = unicodedata.normalize(
        "NFKC", _trim_unicode_white_space(value)
    )
    lowered = normalized.lower()
    for prefix in ("doi:", "https://doi.org/", "https://dx.doi.org/"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    try:
        encoded = normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StructuredStage23AuthorityError("DOI must be ASCII") from exc
    lowered = encoded.decode("ascii").lower()
    if (
        not 7 <= len(encoded) <= 255
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in lowered)
        or "%" in lowered
        or _DOI_RE.fullmatch(lowered) is None
    ):
        raise StructuredStage23AuthorityError("DOI identity is invalid")
    return lowered


def normalize_arxiv_request(value: object) -> tuple[str, str, str | None]:
    if type(value) is not str:
        raise StructuredStage23AuthorityError("arXiv ID must be one string")
    normalized = unicodedata.normalize(
        "NFKC", _trim_unicode_white_space(value)
    )
    lowered = normalized.lower()
    pdf_prefix = "https://arxiv.org/pdf/"
    removed_pdf_prefix = lowered.startswith(pdf_prefix)
    for prefix in ("arxiv:", "https://arxiv.org/abs/", pdf_prefix):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            lowered = normalized.lower()
            break
    if removed_pdf_prefix and lowered.endswith(".pdf"):
        lowered = lowered[:-4]
    try:
        encoded = lowered.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StructuredStage23AuthorityError("arXiv ID must be ASCII") from exc
    if len(encoded) > 64:
        raise StructuredStage23AuthorityError("arXiv ID exceeds 64 bytes")
    match = _ARXIV_NEW_RE.fullmatch(lowered) or _ARXIV_OLD_RE.fullmatch(lowered)
    if match is None:
        raise StructuredStage23AuthorityError("arXiv ID is invalid")
    return lowered, match.group(1), match.group(2)


def match_arxiv_identity(requested: object, response_identifier: object) -> str:
    request, request_base, request_version = normalize_arxiv_request(requested)
    if type(response_identifier) is not str:
        raise StructuredStage23AuthorityError(
            "arXiv response identifier must be one string"
        )
    response = response_identifier
    try:
        encoded = response.encode("ascii")
    except UnicodeEncodeError as exc:
        raise StructuredStage23AuthorityError(
            "arXiv response identifier must be ASCII"
        ) from exc
    if not response or len(encoded) > 64:
        raise StructuredStage23AuthorityError(
            "arXiv response identifier length is invalid"
        )
    match = _ARXIV_NEW_RE.fullmatch(response) or _ARXIV_OLD_RE.fullmatch(response)
    if match is None:
        raise StructuredStage23AuthorityError(
            "arXiv response identifier is not exact lowercase grammar"
        )
    response_base, response_version = match.group(1), match.group(2)
    if response_version is None:
        raise StructuredStage23AuthorityError("arXiv response must be versioned")
    if request_base != response_base:
        raise StructuredStage23AuthorityError("arXiv base identity mismatch")
    if request_version is not None and request_version != response_version:
        raise StructuredStage23AuthorityError("arXiv version identity mismatch")
    if request_version is not None and request != response:
        raise StructuredStage23AuthorityError("arXiv exact identity mismatch")
    return response


def title_match_key(value: object) -> str:
    if type(value) is not str:
        raise StructuredStage23AuthorityError("title must be one string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StructuredStage23AuthorityError("title is not scalar UTF-8") from exc
    if len(raw) > 4096:
        raise StructuredStage23AuthorityError("title exceeds 4096 bytes")
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char) == "Cc"
        and char not in _UNICODE_WHITE_SPACE
        for char in normalized
    ):
        raise StructuredStage23AuthorityError("title contains a control")
    mapped = "".join(
        (
            " "
            if char in _UNICODE_WHITE_SPACE
            or unicodedata.category(char).startswith("P")
            else char
        )
        for char in normalized
    )
    result = _collapse_unicode_white_space(mapped).casefold()
    if (
        not result
        or len(result) > 1024
        or len(result.encode("utf-8")) > 4096
    ):
        raise StructuredStage23AuthorityError("title match key is invalid")
    return result


def display_title(value: object) -> str:
    if type(value) is not str:
        raise StructuredStage23AuthorityError("provider title must be one string")
    normalized = unicodedata.normalize("NFKC", value)
    if any(
        (
            unicodedata.category(char) == "Cc"
            and char not in _UNICODE_WHITE_SPACE
        )
        or ord(char) == 0x7F
        for char in normalized
    ):
        raise StructuredStage23AuthorityError("provider title contains a control")
    result = _collapse_unicode_white_space(normalized)
    try:
        encoded = result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StructuredStage23AuthorityError(
            "provider title is not scalar UTF-8"
        ) from exc
    if not result or len(result) > 1024 or len(encoded) > 4096:
        raise StructuredStage23AuthorityError("provider title is invalid")
    return result


def select_route(entry: Mapping[str, object]) -> tuple[str, str, str]:
    if not isinstance(entry, Mapping):
        raise StructuredStage23AuthorityError("citation entry is invalid")
    doi = entry.get("doi")
    if type(doi) is str and _trim_unicode_white_space(doi):
        return "crossref-doi", "crossref", normalize_doi(doi)
    arxiv = entry.get("arxiv") or entry.get("arxiv_id") or entry.get("eprint")
    if type(arxiv) is str and _trim_unicode_white_space(arxiv):
        normalized, _, _ = normalize_arxiv_request(arxiv)
        return "arxiv-id", "arxiv", normalized
    title = entry.get("title")
    if type(title) is str and _trim_unicode_white_space(title):
        return "openalex-title", "openalex", title_match_key(title)
    raise StructuredStage23AuthorityError("citation has no deterministic route")


def parse_crossref_response(
    content: bytes,
    *,
    requested_doi: str,
    held_title: str,
) -> dict[str, object]:
    value = strict_json_object(content, "Crossref response")
    if value.get("status") != "ok" or value.get("message-type") != "work":
        raise StructuredStage23AuthorityError("Crossref provider envelope mismatch")
    message = value.get("message")
    if type(message) is not dict:
        raise StructuredStage23AuthorityError("Crossref message is invalid")
    if normalize_doi(message.get("DOI")) != normalize_doi(requested_doi):
        raise StructuredStage23AuthorityError("Crossref DOI identity mismatch")
    titles = message.get("title")
    if type(titles) is not list or len(titles) != 1 or type(titles[0]) is not str:
        raise StructuredStage23AuthorityError("Crossref title projection is invalid")
    title = display_title(titles[0])
    if title_match_key(title) != title_match_key(held_title):
        raise StructuredStage23AuthorityError("Crossref title identity mismatch")
    selected = None
    for field in ("published-print", "published-online", "issued"):
        if field in message:
            selected = message[field]
            break
    if type(selected) is not dict or set(selected) != {"date-parts"}:
        raise StructuredStage23AuthorityError("Crossref date projection is invalid")
    parts = selected.get("date-parts")
    if (
        type(parts) is not list
        or len(parts) != 1
        or type(parts[0]) is not list
        or not parts[0]
    ):
        raise StructuredStage23AuthorityError("Crossref date-parts is invalid")
    year = _year(parts[0][0], "Crossref year")
    return {
        "title": title,
        "doi": normalize_doi(requested_doi),
        "arxiv_id": None,
        "year": year,
        "source": "crossref",
    }


_ATOM = "http://www.w3.org/2005/Atom"
_ALLOWED_XML_NAMESPACES = {
    _ATOM,
    "http://a9.com/-/spec/opensearch/1.1/",
    "http://arxiv.org/schemas/atom",
    "http://www.w3.org/XML/1998/namespace",
}


def parse_arxiv_response(
    content: bytes,
    *,
    requested: str,
    held_title: str,
) -> dict[str, object]:
    if (
        type(content) is not bytes
        or content.startswith(b"\xef\xbb\xbf")
        or b"<!DOCTYPE" in content.upper()
        or b"<!ENTITY" in content.upper()
        or len(content) > 1_048_576
    ):
        raise StructuredStage23AuthorityError("arXiv XML envelope is invalid")
    namespace_bindings: dict[str, str] = {}
    try:
        for _event, binding in ET.iterparse(
            io.BytesIO(content), events=("start-ns",)
        ):
            prefix, namespace = binding
            if prefix in namespace_bindings:
                raise StructuredStage23AuthorityError(
                    "arXiv XML namespace rebinding is forbidden"
                )
            if namespace not in _ALLOWED_XML_NAMESPACES:
                raise StructuredStage23AuthorityError(
                    "arXiv XML namespace declaration is not admitted"
                )
            namespace_bindings[prefix] = namespace
    except ET.ParseError as exc:
        raise StructuredStage23AuthorityError("arXiv XML is malformed") from exc
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise StructuredStage23AuthorityError("arXiv XML is malformed") from exc
    elements = list(root.iter())
    if len(elements) > 4096:
        raise StructuredStage23AuthorityError("arXiv XML has too many elements")
    def check_depth(element: ET.Element, depth: int) -> None:
        if depth > 16:
            raise StructuredStage23AuthorityError("arXiv XML depth exceeds 16")
        for child in element:
            check_depth(child, depth + 1)

    check_depth(root, 0)
    aggregate = 0
    for element in elements:
        tag = element.tag
        if type(tag) is not str or not tag.startswith("{") or "}" not in tag:
            raise StructuredStage23AuthorityError("arXiv element lacks namespace")
        namespace = tag[1 : tag.index("}")]
        if namespace not in _ALLOWED_XML_NAMESPACES:
            raise StructuredStage23AuthorityError("arXiv namespace is not admitted")
        local_name = tag[tag.index("}") + 1 :]
        if local_name.lower() == "error":
            raise StructuredStage23AuthorityError("arXiv error entry is forbidden")
        for text in (element.text or "", element.tail or ""):
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise StructuredStage23AuthorityError(
                    "arXiv text is invalid"
                ) from exc
            if size > 16_384:
                raise StructuredStage23AuthorityError(
                    "arXiv text node exceeds cap"
                )
            aggregate += size
    if aggregate > 1_048_576:
        raise StructuredStage23AuthorityError("arXiv aggregate text exceeds cap")
    if root.tag != f"{{{_ATOM}}}feed":
        raise StructuredStage23AuthorityError("arXiv root is not Atom feed")
    entries = root.findall(f"{{{_ATOM}}}entry")
    if len(entries) != 1:
        raise StructuredStage23AuthorityError("arXiv feed is ambiguous")
    entry = entries[0]
    consumed: dict[str, str] = {}
    for name in ("id", "title", "published"):
        matches = entry.findall(f"{{{_ATOM}}}{name}")
        if len(matches) != 1 or type(matches[0].text) is not str:
            raise StructuredStage23AuthorityError(f"arXiv {name} is invalid")
        consumed[name] = matches[0].text.strip()
    identifier_url = consumed["id"]
    prefix = next(
        (
            candidate
            for candidate in (
                "http://arxiv.org/abs/",
                "https://arxiv.org/abs/",
            )
            if identifier_url.startswith(candidate)
        ),
        None,
    )
    if prefix is None:
        raise StructuredStage23AuthorityError("arXiv response URL is invalid")
    identifier = identifier_url[len(prefix) :]
    if (
        not identifier
        or any(mark in identifier for mark in ("%", "\\", "//", "?", "#"))
        or identifier in {".", ".."}
    ):
        raise StructuredStage23AuthorityError("arXiv response path is invalid")
    matched = match_arxiv_identity(requested, identifier)
    title = display_title(consumed["title"])
    if title_match_key(title) != title_match_key(held_title):
        raise StructuredStage23AuthorityError("arXiv title identity mismatch")
    match = re.match(r"([0-9]{4})-", consumed["published"])
    if match is None:
        raise StructuredStage23AuthorityError("arXiv published year is invalid")
    year = _year(int(match.group(1)), "arXiv year")
    return {
        "title": title,
        "doi": None,
        "arxiv_id": matched,
        "year": year,
        "source": "arxiv",
    }


def parse_openalex_response(
    content: bytes,
    *,
    held_title: str,
) -> dict[str, object]:
    value = strict_json_object(content, "OpenAlex response")
    meta = value.get("meta")
    results = value.get("results")
    if type(meta) is not dict or type(results) is not list:
        raise StructuredStage23AuthorityError("OpenAlex envelope is malformed")
    count = meta.get("count")
    if (
        type(count) is not int
        or not 0 <= count <= 200
        or count != len(results)
    ):
        raise StructuredStage23AuthorityError(
            "OpenAlex complete candidate set is not proven"
        )
    key = title_match_key(held_title)
    matches: list[tuple[str, int]] = []
    for row in results:
        if type(row) is not dict:
            raise StructuredStage23AuthorityError("OpenAlex result is malformed")
        for required in ("id", "doi", "title", "publication_year", "ids"):
            if required not in row:
                raise StructuredStage23AuthorityError(
                    "OpenAlex consumed projection is missing"
                )
        if type(row["id"]) is not str or type(row["ids"]) is not dict:
            raise StructuredStage23AuthorityError("OpenAlex identity is malformed")
        title = display_title(row["title"])
        year = _year(row["publication_year"], "OpenAlex year")
        if title_match_key(title) == key:
            matches.append((title, year))
    if len(matches) != 1:
        raise StructuredStage23AuthorityError("OpenAlex title result is ambiguous")
    title, year = matches[0]
    return {
        "title": title,
        "doi": None,
        "arxiv_id": None,
        "year": year,
        "source": "openalex",
    }


def validate_cited_keys(value: object) -> tuple[str, ...]:
    if type(value) not in (list, tuple):
        raise StructuredStage23AuthorityError("cited keys are not an array")
    keys = tuple(value)
    if (
        not 1 <= len(keys) <= 32
        or any(type(key) is not str or _CITE_KEY_RE.fullmatch(key) is None for key in keys)
        or keys != tuple(sorted(set(keys), key=lambda item: item.encode("utf-8")))
    ):
        raise StructuredStage23AuthorityError(
            "cited-key closure must be unique sorted count 1..32"
        )
    return keys


def validate_relevance(
    value: Mapping[str, object],
    cited_keys: Sequence[str],
) -> dict[str, object]:
    keys = validate_cited_keys(tuple(cited_keys))
    if type(value) is not dict or tuple(value) != RELEVANCE_FIELDS:
        raise StructuredStage23AuthorityError("relevance fields mismatch")
    status = value["status"]
    semantic = value["semantic_calls"]
    outbound = value["outbound_attempts"]
    request_sha = value["request_sha256"]
    response_sha = value["response_sha256"]
    retry = value["retry_fingerprint"]
    scores = value["scores"]
    error_class = value["error_class"]
    if status not in {"complete", "failed", "unavailable", "missing"}:
        raise StructuredStage23AuthorityError("relevance status is invalid")
    if type(semantic) is not int or type(outbound) is not int:
        raise StructuredStage23AuthorityError("relevance counters must be true ints")
    if type(scores) is not list:
        raise StructuredStage23AuthorityError("relevance scores are invalid")
    if status == "missing":
        expected = (0, 0, None, None, None, [], "relevance_missing")
    elif status == "unavailable":
        expected = (
            1,
            outbound,
            request_sha,
            None,
            request_sha if outbound == 2 else None,
            [],
            "pure_transport_exhausted",
        )
        if outbound not in {1, 2}:
            raise StructuredStage23AuthorityError("unavailable outbound count invalid")
        _sha(request_sha, "relevance request")
    elif status == "failed":
        expected = (
            1,
            outbound,
            request_sha,
            response_sha,
            request_sha if outbound == 2 else None,
            [],
            "relevance_semantic_failure",
        )
        if outbound not in {1, 2}:
            raise StructuredStage23AuthorityError("failed outbound count invalid")
        _sha(request_sha, "relevance request")
        _sha(response_sha, "relevance response")
    else:
        expected = (
            1,
            outbound,
            request_sha,
            response_sha,
            request_sha if outbound == 2 else None,
            scores,
            None,
        )
        if outbound not in {1, 2}:
            raise StructuredStage23AuthorityError("complete outbound count invalid")
        _sha(request_sha, "relevance request")
        _sha(response_sha, "relevance response")
        parsed_keys: list[str] = []
        for item in scores:
            if type(item) is not dict or tuple(item) != ("cite_key", "score"):
                raise StructuredStage23AuthorityError("relevance score fields mismatch")
            parsed_keys.append(item["cite_key"])  # type: ignore[arg-type]
            score = item["score"]
            if type(score) is not str or _SCORE_RE.fullmatch(score) is None:
                raise StructuredStage23AuthorityError("relevance score is not canonical")
            try:
                decimal = Decimal(score)
            except InvalidOperation as exc:
                raise StructuredStage23AuthorityError("relevance score is invalid") from exc
            if not decimal.is_finite() or not Decimal("0") <= decimal <= Decimal("1"):
                raise StructuredStage23AuthorityError("relevance score is out of range")
        if tuple(parsed_keys) != keys:
            raise StructuredStage23AuthorityError("relevance score closure mismatch")
    actual = (
        semantic,
        outbound,
        request_sha,
        response_sha,
        retry,
        scores,
        error_class,
    )
    if actual != expected:
        raise StructuredStage23AuthorityError("relevance state fields mismatch")
    return dict(value)


def relevance_fixture(
    status: str,
    *,
    cited_keys: Sequence[str],
    score: str | None = None,
) -> dict[str, object]:
    keys = validate_cited_keys(tuple(cited_keys))
    if status == "complete":
        value = {
            "status": status,
            "semantic_calls": 1,
            "outbound_attempts": 1,
            "request_sha256": "a" * 64,
            "response_sha256": "b" * 64,
            "retry_fingerprint": None,
            "scores": [
                {"cite_key": key, "score": score or "0.500000"} for key in keys
            ],
            "error_class": None,
        }
    elif status == "missing":
        value = {
            "status": status,
            "semantic_calls": 0,
            "outbound_attempts": 0,
            "request_sha256": None,
            "response_sha256": None,
            "retry_fingerprint": None,
            "scores": [],
            "error_class": "relevance_missing",
        }
    elif status == "failed":
        value = {
            "status": status,
            "semantic_calls": 1,
            "outbound_attempts": 1,
            "request_sha256": "a" * 64,
            "response_sha256": "b" * 64,
            "retry_fingerprint": None,
            "scores": [],
            "error_class": "relevance_semantic_failure",
        }
    elif status == "unavailable":
        value = {
            "status": status,
            "semantic_calls": 1,
            "outbound_attempts": 1,
            "request_sha256": "a" * 64,
            "response_sha256": None,
            "retry_fingerprint": None,
            "scores": [],
            "error_class": "pure_transport_exhausted",
        }
    else:
        raise StructuredStage23AuthorityError("invalid relevance fixture state")
    return validate_relevance(value, keys)


def derive_outcome(
    *,
    claim_scope: str,
    upstream_quality: str,
    relevance: Mapping[str, object],
    cited_keys: Sequence[str],
) -> tuple[bool, str | None]:
    if claim_scope not in {"pipeline_validation", "exploratory", "research_release"}:
        raise StructuredStage23AuthorityError("claim scope is invalid")
    if upstream_quality not in {"passed", "degraded"}:
        raise StructuredStage23AuthorityError("upstream quality is invalid")
    parsed = validate_relevance(relevance, cited_keys)
    favorable = parsed["status"] == "complete" and all(
        Decimal(item["score"]) >= Decimal("0.500000")  # type: ignore[arg-type]
        for item in parsed["scores"]  # type: ignore[union-attr]
    )
    if not favorable and claim_scope == "research_release":
        return False, None
    outcome = "degraded" if upstream_quality == "degraded" or not favorable else "passed"
    return True, outcome


def validate_file_ref(
    value: object,
    *,
    expected_path: str | None = None,
) -> dict[str, object]:
    if type(value) is not dict or tuple(value) != FILE_REF_FIELDS:
        raise StructuredStage23AuthorityError("FileRef fields mismatch")
    path = value["path"]
    if (
        type(path) is not str
        or not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise StructuredStage23AuthorityError("FileRef path is unsafe")
    if expected_path is not None and path != expected_path:
        raise StructuredStage23AuthorityError("FileRef path mismatch")
    _sha(value["sha256"], "FileRef sha256")
    if type(value["size"]) is not int or value["size"] <= 0:
        raise StructuredStage23AuthorityError("FileRef size is invalid")
    return dict(value)


def validate_citation_projection(
    value: object,
    *,
    expected_key: str | None = None,
) -> dict[str, object]:
    if type(value) is not dict or tuple(value) != CITATION_FIELDS:
        raise StructuredStage23AuthorityError("citation projection fields mismatch")
    cite_key = value["cite_key"]
    if type(cite_key) is not str or (
        expected_key is not None and cite_key != expected_key
    ):
        raise StructuredStage23AuthorityError("citation projection key mismatch")
    route = value["route_class"]
    endpoint = value["endpoint_class"]
    route_endpoint = {
        "crossref-doi": "crossref",
        "arxiv-id": "arxiv",
        "openalex-title": "openalex",
    }
    if route not in route_endpoint or endpoint != route_endpoint[route]:
        raise StructuredStage23AuthorityError("citation route mismatch")
    _sha(value["request_sha256"], "citation request")
    _sha(value["response_sha256"], "citation response")
    require_true_int(value["outbound_count"], 1, "citation outbound_count")
    if value["status"] != "verified":
        raise StructuredStage23AuthorityError("citation is not verified")
    metadata = value["metadata"]
    if type(metadata) is not dict or tuple(metadata) != METADATA_FIELDS:
        raise StructuredStage23AuthorityError("citation metadata fields mismatch")
    title = display_title(metadata["title"])
    _year(metadata["year"])
    if metadata["source"] != endpoint:
        raise StructuredStage23AuthorityError("citation metadata source mismatch")
    if route == "crossref-doi":
        normalize_doi(metadata["doi"])
        if metadata["arxiv_id"] is not None:
            raise StructuredStage23AuthorityError("Crossref arXiv ID must be null")
    elif route == "arxiv-id":
        if metadata["doi"] is not None:
            raise StructuredStage23AuthorityError("arXiv DOI must be null")
        normalized, _, version = normalize_arxiv_request(metadata["arxiv_id"])
        if normalized != metadata["arxiv_id"] or version is None:
            raise StructuredStage23AuthorityError("arXiv metadata ID mismatch")
    elif metadata["doi"] is not None or metadata["arxiv_id"] is not None:
        raise StructuredStage23AuthorityError("OpenAlex identifiers must be null")
    parsed = dict(value)
    parsed["metadata"] = {**metadata, "title": title}
    return parsed


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_verification_report_v2(content: bytes) -> dict[str, object]:
    value = strict_json_object(content, "Stage 23 verification report")
    if tuple(value) != REPORT_FIELDS:
        raise StructuredStage23AuthorityError("report root fields/order mismatch")
    require_true_int(value["schema_version"], 2, "report schema_version")
    if value["publication_stage_id"] != "stage23":
        raise StructuredStage23AuthorityError("report stage ID mismatch")
    validate_file_ref(
        value["paper"], expected_path="stage-22/paper_final.md"
    )
    validate_file_ref(
        value["bibliography"], expected_path="stage-22/references.bib"
    )
    keys = validate_cited_keys(value["cited_keys"])
    if type(value["cited_count"]) is not int or value["cited_count"] != len(keys):
        raise StructuredStage23AuthorityError("report cited count mismatch")
    scope = value["claim_scope"]
    if scope not in {"pipeline_validation", "exploratory", "research_release"}:
        raise StructuredStage23AuthorityError("report claim scope mismatch")
    if value["provider_policy"] != PROVIDER_POLICY:
        raise StructuredStage23AuthorityError("report provider policy mismatch")
    if value["relevance_policy"] != RELEVANCE_POLICY:
        raise StructuredStage23AuthorityError("report relevance policy mismatch")
    citations = value["citations"]
    if type(citations) is not list or len(citations) != len(keys):
        raise StructuredStage23AuthorityError("report citation closure mismatch")
    parsed_citations = [
        validate_citation_projection(item, expected_key=key)
        for key, item in zip(keys, citations, strict=True)
    ]
    relevance = validate_relevance(value["relevance"], keys)
    if value["outcome"] not in {"passed", "degraded"}:
        raise StructuredStage23AuthorityError("report outcome mismatch")
    if type(value["generated"]) is not str or not value["generated"]:
        raise StructuredStage23AuthorityError("report generated mismatch")
    if canonical_json_bytes(value) != content:
        raise StructuredStage23AuthorityError("report is not canonical JSON")
    parsed = dict(value)
    parsed["citations"] = parsed_citations
    parsed["relevance"] = relevance
    return parsed


def parse_verification_manifest_v2(content: bytes) -> dict[str, object]:
    value = strict_json_object(content, "Stage 23 verification manifest")
    if tuple(value) != MANIFEST_FIELDS:
        raise StructuredStage23AuthorityError("manifest root fields/order mismatch")
    require_true_int(value["schema_version"], 2, "manifest schema_version")
    if (
        value["publication_stage_id"] != "stage23"
        or value["publication_mode"] != "structured-scientific-claim-v1"
    ):
        raise StructuredStage23AuthorityError("manifest publication identity mismatch")
    require_true_int(
        value["structured_capability_schema_version"],
        1,
        "manifest capability schema",
    )
    if value["structured_capability_snapshot"] != {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }:
        raise StructuredStage23AuthorityError("manifest capability snapshot mismatch")
    _sha(value["generation_binding_sha256"], "manifest generation binding")
    paths = {
        "canonical_experiment_evidence": "canonical_experiment_evidence.json",
        "source_stage19_manifest": (
            "stage-19/scientific_claim_authority_manifest.json"
        ),
        "source_stage20_manifest": "stage-20/quality_gate_manifest.json",
        "source_stage21_manifest": "stage-21/bundle_index.json",
        "source_stage22_manifest": "stage-22/stage22_export_manifest.json",
        "source_paper": "stage-22/paper_final.md",
        "source_bibliography": "stage-22/references.bib",
        "source_latex": "stage-22/paper.tex",
        "verification_report": "stage-23/verification_report.json",
        "verified_bibliography": "stage-23/references_verified.bib",
        "verified_paper": "stage-23/paper_final_verified.md",
    }
    for field, path in paths.items():
        validate_file_ref(value[field], expected_path=path)
    cfs = value["cfs"]
    if (
        type(cfs) is not dict
        or tuple(cfs) != ("schema_version", "sha256")
    ):
        raise StructuredStage23AuthorityError("manifest CFS fields mismatch")
    require_true_int(cfs["schema_version"], 1, "manifest CFS schema")
    _sha(cfs["sha256"], "manifest CFS sha256")
    quality = value["quality_outcome"]
    if quality not in {"passed", "degraded"}:
        raise StructuredStage23AuthorityError("manifest quality outcome mismatch")
    if quality == "passed":
        if value["degradation_signal"] is not None:
            raise StructuredStage23AuthorityError(
                "passed manifest has degradation signal"
            )
    else:
        validate_file_ref(value["degradation_signal"])
    if value["claim_scope"] not in {
        "pipeline_validation",
        "exploratory",
        "research_release",
    }:
        raise StructuredStage23AuthorityError("manifest claim scope mismatch")
    if value["verification_policy"] != VERIFICATION_POLICY:
        raise StructuredStage23AuthorityError("manifest policy mismatch")
    if value["outcome"] not in {"passed", "degraded"}:
        raise StructuredStage23AuthorityError("manifest outcome mismatch")
    require_true_int(value["output_count"], 3, "manifest output_count")
    outputs = value["outputs"]
    roles = (
        ("verification_report", "stage-23/verification_report.json"),
        ("verified_bibliography", "stage-23/references_verified.bib"),
        ("verified_paper", "stage-23/paper_final_verified.md"),
    )
    if type(outputs) is not list or len(outputs) != 3:
        raise StructuredStage23AuthorityError("manifest outputs closure mismatch")
    for row, (role, path) in zip(outputs, roles, strict=True):
        if type(row) is not dict or tuple(row) != OUTPUT_FIELDS:
            raise StructuredStage23AuthorityError("manifest output fields mismatch")
        if (
            row["role"] != role
            or row["logical_name"] is not None
            or row["path"] != path
        ):
            raise StructuredStage23AuthorityError("manifest output order mismatch")
        validate_file_ref(
            {
                "path": row["path"],
                "sha256": row["sha256"],
                "size": row["size"],
            },
            expected_path=path,
        )
    if type(value["generated"]) is not str or not value["generated"]:
        raise StructuredStage23AuthorityError("manifest generated mismatch")
    if canonical_json_bytes(value) != content:
        raise StructuredStage23AuthorityError("manifest is not canonical JSON")
    return dict(value)
