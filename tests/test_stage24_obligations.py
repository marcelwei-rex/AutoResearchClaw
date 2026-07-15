from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError
import json

import pytest

from researchclaw.pipeline.stage24_obligations import (
    ClaimObligationError,
    build_claim_obligation_inventory,
    canonical_obligation_inventory_bytes,
    parse_claim_obligation_inventory,
)


def _paper(body: str, *, newline: str = "\n") -> bytes:
    text = f"# Study\n\n## Results\n\n{body}\n"
    return text.replace("\n", newline).encode("utf-8")


def test_inventory_builds_all_kinds_with_full_identity_and_replays() -> None:
    raw = _paper(
        "Our detector reached 95% F1 and outperformed baseline "
        "[smith2024test]."
    )

    inventory = build_claim_obligation_inventory(raw)

    assert {row.kind for row in inventory} == {
        "numeric_token",
        "citation_instance",
        "comparative_sentence",
        "declarative_sentence",
    }
    assert all(row.obligation_id.startswith("obl-") for row in inventory)
    assert all(len(row.obligation_id) == 68 for row in inventory)
    assert all(
        row.source_sha256
        == hashlib.sha256(raw[row.byte_start : row.byte_end]).hexdigest()
        for row in inventory
    )
    assert list(inventory) == sorted(
        inventory,
        key=lambda row: (
            row.byte_start,
            row.byte_end,
            {
                "numeric_token": 0,
                "citation_instance": 1,
                "comparative_sentence": 2,
                "declarative_sentence": 3,
            }[row.kind],
            row.occurrence_rank,
        ),
    )
    data = canonical_obligation_inventory_bytes(inventory)
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert parse_claim_obligation_inventory(data, paper_bytes=raw) == inventory


def test_inventory_preserves_duplicate_sentence_offsets_and_multibyte_crlf() -> None:
    sentence = "Emoji 😀 result is better than baseline."
    raw = _paper(f"{sentence}\n\n{sentence}", newline="\r\n").rstrip(b"\r\n")

    inventory = build_claim_obligation_inventory(raw)
    comparative = [row for row in inventory if row.kind == "comparative_sentence"]

    assert len(comparative) == 2
    assert comparative[0].byte_start != comparative[1].byte_start
    assert comparative[0].source_sha256 == comparative[1].source_sha256
    assert comparative[0].obligation_id != comparative[1].obligation_id
    assert all(raw[row.byte_start : row.byte_end].decode("utf-8") == sentence for row in comparative)


def test_setext_results_heading_selects_declarative_sentences() -> None:
    raw = b"Study\n=====\n\nEvaluation Results\n------------------\n\nA bounded result remains."

    inventory = build_claim_obligation_inventory(raw)
    rows = [row for row in inventory if row.kind == "declarative_sentence"]

    assert len(rows) == 1
    assert rows[0].kind_payload == {"selected_section_class": "results"}
    assert raw[rows[0].byte_start : rows[0].byte_end] == b"A bounded result remains."


@pytest.mark.parametrize(
    ("parent", "child", "expected_class"),
    (
        ("Results", "Ablation", "results"),
        ("Discussion", "Limitations", "discussion"),
        ("Contributions", "Implementation Details", "contributions"),
    ),
)
def test_selected_section_class_inherits_from_nearest_matching_ancestor(
    parent: str,
    child: str,
    expected_class: str,
) -> None:
    raw = (
        f"# Study\n\n## {parent}\n\n### {child}\n\n"
        "A bounded claim remains.\n"
    ).encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "declarative_sentence"
    ]

    assert len(rows) == 1
    assert rows[0].kind_payload == {"selected_section_class": expected_class}


def test_selected_section_class_does_not_leak_into_sibling_tree() -> None:
    raw = (
        b"# Study\n\n## Results\n\n### Ablation\n\nA result remains.\n\n"
        b"## Method\n\n### Implementation\n\nA method claim remains.\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "declarative_sentence"
    ]

    assert len(rows) == 1
    assert raw[rows[0].byte_start : rows[0].byte_end] == b"A result remains."


def test_nearest_selected_section_alias_overrides_matching_ancestor() -> None:
    raw = (
        b"# Study\n\n## Results\n\n### Discussion\n\nA bounded claim remains.\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "declarative_sentence"
    ]

    assert len(rows) == 1
    assert rows[0].kind_payload == {"selected_section_class": "discussion"}


def test_multi_key_citations_have_exact_key_and_marker_spans() -> None:
    raw = _paper(
        "Prior work [smith2024test; jones2023survey] agrees with "
        "\\cite{lee2022work,kim2021study}."
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert [row.kind_payload["cite_key"] for row in rows] == [
        "smith2024test",
        "jones2023survey",
        "lee2022work",
        "kim2021study",
    ]
    assert [row.kind_payload["key_rank"] for row in rows] == [0, 1, 0, 1]
    for row in rows:
        payload = row.kind_payload
        marker = raw[payload["marker_byte_start"] : payload["marker_byte_end"]]
        assert hashlib.sha256(marker).hexdigest() == payload["marker_sha256"]
        assert raw[row.byte_start : row.byte_end].decode() == payload["cite_key"]


@pytest.mark.parametrize(
    ("source", "lexeme", "unit"),
    (
        ("Value 1,000 is retained.", "1,000", None),
        ("Value .5 is retained.", ".5", None),
        ("Value -1.2e-3 is retained.", "-1.2e-3", None),
        ("Latency is 10ms.", "10", "ms"),
        ("Latency is 10 µs.", "10", " µs"),
        ("Rate is 10%.", "10", "%"),
        ("Count is 1.", "1", None),
    ),
)
def test_numeric_policy_accepts_exact_lexemes(
    source: str,
    lexeme: str,
    unit: str | None,
) -> None:
    rows = [
        row
        for row in build_claim_obligation_inventory(_paper(source))
        if row.kind == "numeric_token" and row.kind_payload["numeric_role"] == "claim_numeric"
    ]

    assert len(rows) == 1
    assert rows[0].kind_payload["number_lexeme"] == lexeme
    assert rows[0].kind_payload["unit_lexeme"] == unit


@pytest.mark.parametrize("source", ("1,,000", "1,00abc", "12,34,567", "1e", "1-2"))
def test_numeric_policy_rejects_malformed_maximal_runs(source: str) -> None:
    with pytest.raises(ClaimObligationError, match="numeric"):
        build_claim_obligation_inventory(_paper(f"Malformed {source} value."))


def test_numeric_policy_marks_protected_contexts_as_metadata() -> None:
    raw = _paper(
        "Use `threshold2024` and [source](https://example.test/v2) with "
        "DOI:10.1234/example.2020 and Figure 3 [smith2024test]."
    )

    rows = [row for row in build_claim_obligation_inventory(raw) if row.kind == "numeric_token"]

    assert rows
    assert all(row.kind_payload["numeric_role"] == "identifier_metadata" for row in rows)


def test_comparison_policy_ignores_protected_inline_and_bibliography_text() -> None:
    raw = _paper(
        "Use `better` and [label](https://better.example) without a comparison.\n\n"
        "@article{test2024, title={A better result}, year={2024}}"
    )

    inventory = build_claim_obligation_inventory(raw)

    assert not [row for row in inventory if row.kind == "comparative_sentence"]
    metadata = [
        row
        for row in inventory
        if row.kind == "numeric_token"
        and row.kind_payload["numeric_role"] == "identifier_metadata"
    ]
    assert metadata


def test_escaped_backticks_remain_visible_comparison_text() -> None:
    raw = b"# Study\n\n## Introduction\n\nThe method is \\`better\\` than baseline.\n"

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1


@pytest.mark.parametrize(
    "body",
    (
        r"The method is \\`better\\` than baseline.",
        "The method is `better\nthan` baseline.",
    ),
)
def test_even_escape_or_multiline_code_span_protects_comparison(body: str) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert rows == []


def test_unmatched_or_different_width_backticks_do_not_hide_comparison() -> None:
    raw = b"# Study\n\n## Introduction\n\nThe method is `better`` than baseline.\n"

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1


@pytest.mark.parametrize(
    "body",
    (
        r"The `code\` better` method remains.",
        r"The `code\\` better` method remains.",
        r"The ``code\`` better`` method remains.",
    ),
)
def test_code_span_closer_ignores_internal_backslash_escape(body: str) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1


@pytest.mark.parametrize(
    "body",
    (
        "The method is `unfinished.\n\nA better result remains.`",
        "- The method is `unfinished.\n- A better result remains.`",
    ),
)
def test_code_delimiters_cannot_pair_across_inline_token_maps(body: str) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1


@pytest.mark.parametrize(
    "body",
    (
        "`[smith2024test]`",
        "```text\n[smith2024test]\n```",
        "    [smith2024test]",
        "[smith2024test](https://example.test)",
        r"\[smith2024test]",
        "[smith2024test]: https://example.test",
        r"\citebanana{smith2024test}",
        r"\citep{smith2024test}",
        r"\citet{smith2024test}",
        r"\cite[smith2024note]{jones2023work}",
        r"\\cite{smith2024test}",
        r"\\cite[smith2024note]{jones2023work}",
    ),
)
def test_citation_inventory_rejects_noneligible_commonmark_and_latex_contexts(
    body: str,
) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows == []


@pytest.mark.parametrize(
    "link",
    (
        "[smith2024test][source]",
        "[smith2024test][]",
        "[smith2024test]",
    ),
)
def test_citation_inventory_rejects_resolved_reference_links(link: str) -> None:
    definition = (
        "[source]: https://example.test\n"
        if link.endswith("[source]")
        else "[smith2024test]: https://example.test\n"
    )
    raw = (
        f"# Study\n\n## Introduction\n\n{link} remains a link.\n\n{definition}"
    ).encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows == []


@pytest.mark.parametrize(
    "body",
    (
        "Prior [smith2024test](not a valid destination) supports this claim.",
        "Prior [smith2024test](<https://example.test supports this claim.",
        "Prior [smith2024test](not\n\na valid destination) supports this claim.",
    ),
)
def test_unconfirmed_inline_link_cannot_hide_bracket_citation(body: str) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows
    assert raw[rows[0].byte_start : rows[0].byte_end] == b"smith2024test"


def test_confirmed_inline_link_is_not_a_citation_obligation() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"Prior [smith2024test](https://example.test) is a link.\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows == []


def test_confirmed_inline_link_may_contain_code_label() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"Prior [`smith2024test`](https://example.test) is a link.\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows == []


def test_invalid_link_real_citation_and_real_link_preserve_source_order() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"[bad2024key](not a valid destination), [real2023cite], and "
        b"[link2022key](https://example.test).\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert [row.kind_payload["cite_key"] for row in rows] == [
        "bad2024key",
        "real2023cite",
    ]


@pytest.mark.parametrize(
    "definition",
    (
        "```text\n[smith2024test]: https://example.test\n```",
        "<div>\n[smith2024test]: https://example.test\n</div>",
        "[smith2024test]:",
        "[smith2024test]: <invalid destination",
    ),
)
def test_unconfirmed_reference_definition_cannot_hide_body_citation(
    definition: str,
) -> None:
    raw = (
        "# Study\n\n## Introduction\n\n"
        "Prior [smith2024test] supports this claim.\n\n"
        f"{definition}\n"
    ).encode()

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows
    assert raw[rows[0].byte_start : rows[0].byte_end] == b"smith2024test"


def test_parser_confirmed_reference_definition_hides_shortcut_link() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"Prior [smith2024test] is a link.\n\n"
        b"[smith2024test]: https://example.test\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "citation_instance"
    ]

    assert rows == []


def test_bibliography_block_produces_no_sentence_or_citation_obligations() -> None:
    raw = _paper(
        "@article{smith2024test, title={A better result}, year={2024}}"
    )

    rows = build_claim_obligation_inventory(raw)

    assert not [
        row
        for row in rows
        if row.kind in {
            "citation_instance",
            "comparative_sentence",
            "declarative_sentence",
        }
    ]


@pytest.mark.parametrize(
    "body",
    (
        "\\foo[\nPrior [smith2024test] supports this claim.",
        "\\foo{\nPrior [smith2024test] supports this claim.",
        "\\foo[\n\nPrior [smith2024test] supports this claim.",
    ),
)
def test_unclosed_latex_group_fails_closed_within_inline_block(body: str) -> None:
    raw = f"# Study\n\n## Introduction\n\n{body}\n".encode()

    with pytest.raises(ClaimObligationError, match="unclosed LaTeX"):
        build_claim_obligation_inventory(raw)


def test_inline_html_tags_are_metadata_but_visible_content_remains_claim_text() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"The <span data-rank=\"2\">better</span> method remains.\n"
    )

    rows = build_claim_obligation_inventory(raw)

    assert len([row for row in rows if row.kind == "comparative_sentence"]) == 1
    numeric = [row for row in rows if row.kind == "numeric_token"]
    assert len(numeric) == 1
    assert numeric[0].kind_payload["numeric_role"] == "identifier_metadata"


def test_escaped_inline_html_remains_visible_comparison_text() -> None:
    raw = b"# Study\n\n## Introduction\n\nThe literal \\<better> marker remains.\n"

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1


def test_real_inline_html_with_quoted_closer_protects_only_tags() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"The <span title=\"higher > lower\">better</span> method remains.\n"
    )

    rows = [
        row
        for row in build_claim_obligation_inventory(raw)
        if row.kind == "comparative_sentence"
    ]

    assert len(rows) == 1
    assert rows[0].kind_payload == {"matched_terms": ["better"]}


def test_html_comment_and_autolink_are_nonclaim_metadata() -> None:
    raw = (
        b"# Study\n\n## Introduction\n\n"
        b"The <!-- better --> marker and <https://higher.example/2024> remain.\n"
    )

    rows = build_claim_obligation_inventory(raw)

    assert not [row for row in rows if row.kind == "comparative_sentence"]
    numeric = [row for row in rows if row.kind == "numeric_token"]
    assert len(numeric) == 1
    assert numeric[0].kind_payload["numeric_role"] == "identifier_metadata"


def test_html_block_is_entirely_nonclaim_context() -> None:
    raw = _paper(
        "<div>\nA better result [smith2024test] reached 2.\n</div>"
    )

    rows = build_claim_obligation_inventory(raw)

    assert not [
        row
        for row in rows
        if row.kind in {
            "citation_instance",
            "comparative_sentence",
            "declarative_sentence",
        }
    ]
    numeric = [row for row in rows if row.kind == "numeric_token"]
    assert len(numeric) == 1
    assert all(
        row.kind_payload["numeric_role"] == "identifier_metadata" for row in numeric
    )


def test_sentence_policy_preserves_abbreviation_and_comparison_term() -> None:
    raw = _paper("The method, i.e. detector A, is better than baseline. Next is stable.")

    inventory = build_claim_obligation_inventory(raw)
    comparative = [row for row in inventory if row.kind == "comparative_sentence"]
    declarative = [row for row in inventory if row.kind == "declarative_sentence"]

    assert len(comparative) == 1
    assert raw[comparative[0].byte_start : comparative[0].byte_end].decode() == (
        "The method, i.e. detector A, is better than baseline."
    )
    assert len(declarative) == 2


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        (b"\xef\xbb\xbf# Study\n", "BOM"),
        (b"# Study\rBody\n", "bare CR"),
        (b"# Study\n\xff", "UTF-8"),
    ),
)
def test_inventory_rejects_noncanonical_source_bytes(raw: bytes, message: str) -> None:
    with pytest.raises(ClaimObligationError, match=message):
        build_claim_obligation_inventory(raw)


@pytest.mark.parametrize(
    "mutation",
    ("id", "span", "kind", "unknown", "duplicate_key"),
)
def test_inventory_replay_rejects_stored_mutation(mutation: str) -> None:
    raw = _paper("The result reached 0.95 and is better.")
    inventory = build_claim_obligation_inventory(raw)
    data = canonical_obligation_inventory_bytes(inventory)
    if mutation == "duplicate_key":
        data = data.replace(
            b'"schema_version":1,',
            b'"schema_version":1,"schema_version":1,',
            1,
        )
    else:
        value = json.loads(data)
        if mutation == "id":
            value[0]["obligation_id"] = "obl-" + "0" * 64
        elif mutation == "span":
            value[0]["byte_end"] += 1
        elif mutation == "kind":
            value[0]["kind"] = "implementation_defined"
        else:
            value[0]["extra"] = True
        data = json.dumps(value).encode()

    with pytest.raises(ClaimObligationError):
        parse_claim_obligation_inventory(data, paper_bytes=raw)


@pytest.mark.parametrize("schema_version", (True, 1.0, "1"))
def test_inventory_replay_rejects_noninteger_schema_version(
    schema_version: object,
) -> None:
    raw = _paper("The result reached 0.95.")
    value = json.loads(
        canonical_obligation_inventory_bytes(build_claim_obligation_inventory(raw))
    )
    value[0]["schema_version"] = schema_version

    with pytest.raises(ClaimObligationError, match="schema version"):
        parse_claim_obligation_inventory(json.dumps(value).encode(), paper_bytes=raw)


@pytest.mark.parametrize("key_rank", (True, 0.0))
def test_inventory_replay_rejects_noninteger_citation_key_rank(
    key_rank: object,
) -> None:
    raw = _paper("Prior work [smith2024test] remains.")
    value = json.loads(
        canonical_obligation_inventory_bytes(build_claim_obligation_inventory(raw))
    )
    citation = next(row for row in value if row["kind"] == "citation_instance")
    citation["kind_payload"]["key_rank"] = key_rank

    with pytest.raises(ClaimObligationError, match="key_rank"):
        parse_claim_obligation_inventory(json.dumps(value).encode(), paper_bytes=raw)


@pytest.mark.parametrize("format_kind", ("pretty", "reordered", "trailing"))
def test_inventory_replay_requires_exact_canonical_json_bytes(format_kind: str) -> None:
    raw = _paper("First result is 1. Second result is 2.")
    inventory = build_claim_obligation_inventory(raw)
    canonical = canonical_obligation_inventory_bytes(inventory)
    if format_kind == "pretty":
        data = (json.dumps(json.loads(canonical), indent=2) + "\n").encode()
    elif format_kind == "reordered":
        value = json.loads(canonical)
        value.reverse()
        data = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    else:
        data = canonical + b" "

    with pytest.raises(ClaimObligationError):
        parse_claim_obligation_inventory(data, paper_bytes=raw)


@pytest.mark.parametrize(
    "mutation",
    ("str", "bom", "crlf", "non_utf8"),
)
def test_inventory_replay_is_strict_bytes_first(mutation: str) -> None:
    raw = _paper("The result reached 1.")
    canonical = canonical_obligation_inventory_bytes(
        build_claim_obligation_inventory(raw)
    )
    data: object
    if mutation == "str":
        data = canonical.decode()
    elif mutation == "bom":
        data = b"\xef\xbb\xbf" + canonical
    elif mutation == "crlf":
        data = canonical.replace(b"\n", b"\r\n")
    else:
        data = canonical + b"\xff"

    with pytest.raises(ClaimObligationError):
        parse_claim_obligation_inventory(data, paper_bytes=raw)  # type: ignore[arg-type]


def test_inventory_rejects_markdown_runtime_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_obligations.package_version",
        lambda _name: "4.1.0",
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_obligations.parse_manuscript",
        lambda *_args, **_kwargs: pytest.fail("parser reached before version gate"),
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_obligations.MarkdownIt",
        lambda *_args, **_kwargs: pytest.fail("MarkdownIt reached before version gate"),
    )

    with pytest.raises(ClaimObligationError, match="version mismatch"):
        build_claim_obligation_inventory(_paper("A result remains."))


def test_inventory_rejects_missing_markdown_runtime_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError("markdown-it-py")

    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_obligations.package_version",
        _missing,
    )
    monkeypatch.setattr(
        "researchclaw.pipeline.stage24_obligations.parse_manuscript",
        lambda *_args, **_kwargs: pytest.fail("parser reached before package gate"),
    )

    with pytest.raises(ClaimObligationError, match="not installed"):
        build_claim_obligation_inventory(_paper("A result remains."))
