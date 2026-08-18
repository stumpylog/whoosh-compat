"""Tests for :func:`whoosh_compat.ast.free_text_tokens`.

The helper answers "which free-text word tokens does this query contain?"
for consumers that build a secondary clause from the query's text (the
motivating case: paperless-ngx's fuzzy blend, which re-parses a plain word
string through tantivy's own parser and must never receive whoosh grammar).
The subtle rules all live here: negated subtrees contribute nothing,
multifield expansion dedupes back to one token, only TEXT/KEYWORD leaves on
the requested fields count, and tokens are the analyzer's output.
"""

from __future__ import annotations

import pytest

import whoosh_compat as wc
from whoosh_compat.ast import free_text_tokens
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec


def tokens(
    query: str, reg: FieldRegistry, fields: tuple[str, ...] = ("content", "title")
) -> tuple[str, ...]:
    result = wc.parse(query, registry=reg, default_fields=["content", "title"])
    assert not result.diagnostics
    return free_text_tokens(result.ast, registry=reg, fields=fields)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("invocie", ("invocie",), id="unfielded-word-dedupes-multifield-expansion"),
        pytest.param("invocie report", ("invocie", "report"), id="two-words-keep-order"),
        pytest.param("alpha beta alpha", ("alpha", "beta"), id="repeated-word-dedupes"),
        pytest.param(
            "added:'2026-08-04' invocie",
            ("invocie",),
            id="date-grammar-contributes-nothing",
        ),
        pytest.param("asn:5 report", ("report",), id="numeric-field-contributes-nothing"),
        pytest.param(
            "created:[2020 TO 2021] report",
            ("report",),
            id="date-range-contributes-nothing",
        ),
        pytest.param("report NOT draft", ("report",), id="not-subtree-excluded"),
        pytest.param("report ANDNOT draft", ("report",), id="andnot-negative-side-excluded"),
        pytest.param(
            "report ANDMAYBE bonus",
            ("report", "bonus"),
            id="andmaybe-both-sides-included",
        ),
        pytest.param(
            "report REQUIRE bonus",
            ("report", "bonus"),
            id="require-both-sides-included",
        ),
        # The conftest registry's fields have no analyzer, and the default
        # analysis treats the phrase text as one token; a splitting
        # analyzer yields per-word tokens instead (see
        # test_analyzer_output_is_what_counts). Both are "the analyzer's
        # output verbatim", the helper never re-splits.
        pytest.param('"tax report"', ("tax report",), id="phrase-contributes-its-words"),
        pytest.param("rep*", (), id="wildcard-pattern-excluded"),
        pytest.param("repo?t", (), id="question-pattern-excluded"),
        pytest.param("title:report^2", ("report",), id="boost-is-transparent"),
        pytest.param(
            "document_type:contract report",
            ("report",),
            id="fielded-term-on-unlisted-field-excluded",
        ),
        pytest.param(
            "tag:urgent report",
            ("report",),
            id="keyword-field-not-in-fields-excluded",
        ),
        pytest.param(
            "notes.note:secret report",
            ("report",),
            id="json-subpath-term-excluded",
        ),
        pytest.param("*", (), id="every-contributes-nothing"),
        pytest.param(
            "(report OR invocie) NOT (draft memo)", ("report", "invocie"), id="nested-groups"
        ),
    ],
)
def test_free_text_tokens(reg: FieldRegistry, query: str, expected: tuple[str, ...]) -> None:
    assert tokens(query, reg) == expected


def test_listed_keyword_field_contributes(reg: FieldRegistry) -> None:
    # KEYWORD is a free-text kind: when its field IS requested, its
    # analyzed value counts.
    assert tokens("tag:urgent report", reg, fields=("content", "title", "tag")) == (
        "urgent",
        "report",
    )


def test_fields_accept_aliases(reg: FieldRegistry) -> None:
    # "type" is an alias of document_type; requesting either spelling
    # selects the same field.
    assert tokens("type:contract", reg, fields=("type",)) == ("contract",)
    assert tokens("document_type:contract", reg, fields=("type",)) == ("contract",)


def test_analyzer_output_is_what_counts() -> None:
    # A field analyzer that lowercases and drops short tokens shows up in
    # the result: tokens are analysis output, not raw query text.
    def analyzer(text: str) -> list[str]:
        return [t.lower() for t in text.split() if len(t) > 2]

    reg = FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=analyzer),
            FieldSpec("title", FieldKind.TEXT, analyzer=analyzer),
        ]
    )
    result = wc.parse("The Big Ox", registry=reg, default_fields=["content", "title"])
    assert not result.diagnostics
    assert free_text_tokens(result.ast, registry=reg, fields=("content", "title")) == (
        "the",
        "big",
    )


@pytest.mark.parametrize(
    ("bad_field", "message_match"),
    [
        pytest.param("nope", "unknown field", id="unknown-name"),
        pytest.param("asn", "not a free-text", id="numeric-kind"),
        pytest.param("added", "not a free-text", id="datetime-kind"),
        pytest.param("has_tag", "not a free-text", id="boolean-exists-kind"),
        pytest.param("notes", "JSON field", id="bare-json-field"),
        pytest.param("notes.note", "JSON subpath", id="json-subpath"),
    ],
)
def test_ineligible_field_raises(reg: FieldRegistry, bad_field: str, message_match: str) -> None:
    # Host configuration error: same eager-raise philosophy as parse().
    # Each ineligible shape names what it actually is; only a genuinely
    # unregistered name is called "unknown".
    result = wc.parse("report", registry=reg, default_fields=["content"])
    with pytest.raises(ValueError, match=message_match):
        free_text_tokens(result.ast, registry=reg, fields=(bad_field,))


def test_empty_fields_raises(reg: FieldRegistry) -> None:
    result = wc.parse("report", registry=reg, default_fields=["content"])
    with pytest.raises(ValueError, match="fields"):
        free_text_tokens(result.ast, registry=reg, fields=())
