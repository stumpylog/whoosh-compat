"""Tests for :func:`whoosh_compat.ast.free_text_tokens`.

The helper answers "which free-text word tokens does this query contain?"
for consumers that build a secondary clause from the query's text (the
motivating case: paperless-ngx's fuzzy blend, which re-parses a plain word
string through tantivy's own parser and must never receive whoosh grammar).
The subtle rules all live here: negated subtrees contribute nothing,
multifield expansion dedupes back to one token, only TEXT/KEYWORD leaves on
the requested fields count, and tokens are the analyzer's output (or, with
``analyzed=False``, the raw text those leaves were parsed from).
"""

from __future__ import annotations

import pytest

import whoosh_compat as wc
from whoosh_compat.ast import Multitoken
from whoosh_compat.ast import analyze
from whoosh_compat.ast import free_text_tokens
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec


def tokens(
    query: str,
    reg: FieldRegistry,
    fields: tuple[str, ...] = ("content", "title"),
    *,
    analyzed: bool = True,
) -> tuple[str, ...]:
    result = wc.parse(query, registry=reg, default_fields=["content", "title"])
    assert not result.diagnostics
    return free_text_tokens(result.ast, registry=reg, fields=fields, analyzed=analyzed)


def stemming_analyzer(text: str) -> list[str]:
    """Lowercase, drop the stopword ``the``, strip a couple of suffixes.

    Deliberately NOT idempotent (``universities`` -> ``univers`` ->
    ``univer``), which is exactly the property ``analyzed=False`` exists to
    protect a caller from: feeding analyzed output back into a parser that
    analyzes again searches a shorter stem than the index holds.
    """
    out = []
    for raw in text.split():
        word = raw.lower()
        if word == "the":
            continue
        for suffix in ("ities", "ies", "ed", "s"):
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                word = word[: -len(suffix)]
                break
        out.append(word)
    return out


@pytest.fixture
def stem_reg() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=stemming_analyzer),
            FieldSpec("title", FieldKind.TEXT, analyzer=stemming_analyzer),
        ]
    )


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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # The bug: analyze()'s binary-drop (DIVERGENCES.md entry 23) removes
        # a positive side that analyzed to nothing and leaves the NEGATIVE
        # side standing alone as a bare positive, so the excluded term used
        # to come back out of a function whose first rule is that it cannot.
        pytest.param("the ANDNOT secret", (), id="andnot-positive-side-all-stopwords"),
        pytest.param("the NOT secret", (), id="not-with-all-stopword-sibling"),
        pytest.param("(the OR the) ANDNOT secret", (), id="andnot-positive-group-all-stopwords"),
        pytest.param(
            "report AND (the ANDNOT secret)",
            ("report",),
            id="collapsed-andnot-nested-under-and",
        ),
        pytest.param(
            "report ANDNOT (the AND secret)",
            ("report",),
            id="collapsing-inside-the-negative-side",
        ),
        # Still the ordinary case, with an analyzer in play.
        pytest.param("invoices ANDNOT secret", ("invoice",), id="ordinary-andnot"),
    ],
)
def test_negated_terms_never_survive_an_analysis_collapse(
    stem_reg: FieldRegistry, query: str, expected: tuple[str, ...]
) -> None:
    assert tokens(query, stem_reg, analyzed=True) == expected


def test_negation_is_structural_so_it_holds_unanalyzed_too(stem_reg: FieldRegistry) -> None:
    # Polarity comes from the parsed tree, not from analysis, so the raw
    # mode excludes the same subtrees. "the" is positive here and unanalyzed
    # mode does not consult the stopword list, so it contributes its raw
    # text; "secret" is excluded because the user excluded it.
    assert tokens("the ANDNOT secret", stem_reg, analyzed=False) == ("the",)
    assert tokens("invoices ANDNOT secret", stem_reg, analyzed=False) == ("invoices",)


def test_unanalyzed_mode_returns_raw_term_text(stem_reg: FieldRegistry) -> None:
    # The motivating case: stemming is not idempotent, so a caller that
    # re-parses these words through a backend that analyzes them again must
    # start from the raw text, not from a stem.
    assert tokens("universities", stem_reg, analyzed=True) == ("univers",)
    assert tokens("universities", stem_reg, analyzed=False) == ("universities",)
    assert stemming_analyzer("univers") == ["univer"]


def test_unanalyzed_mode_keeps_a_zero_token_leaf(stem_reg: FieldRegistry) -> None:
    # A word the analyzer would drop entirely (a stopword) has raw text, and
    # analyzed=False reports it: the mode's contract is "the text before
    # analysis", so it never runs the analyzer, not even to decide
    # membership.
    assert tokens("the", stem_reg, analyzed=True) == ()
    assert tokens("the", stem_reg, analyzed=False) == ("the",)
    assert tokens("the report", stem_reg, analyzed=False) == ("the", "report")


def test_unanalyzed_mode_dedupes_on_what_it_emits(stem_reg: FieldRegistry) -> None:
    # Two spellings that analyze to one token stay two raw tokens; dedupe
    # applies to the emitted text, whichever mode produced it.
    assert tokens("Report report", stem_reg, analyzed=True) == ("report",)
    assert tokens("Report report", stem_reg, analyzed=False) == ("Report", "report")


def test_unanalyzed_phrase_contributes_its_raw_text_unsplit(stem_reg: FieldRegistry) -> None:
    # Analyzed, a phrase contributes the analyzer's words. Unanalyzed, it
    # contributes the raw phrase text as one entry: re-splitting it here
    # would be this function doing tokenization it explicitly does not do.
    assert tokens('"tax reports"', stem_reg, analyzed=True) == ("tax", "report")
    assert tokens('"tax reports"', stem_reg, analyzed=False) == ("tax reports",)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("rep*", (), id="prefix-pattern"),
        pytest.param("repo?t", (), id="wildcard-pattern"),
        pytest.param("*", (), id="every"),
        pytest.param("asn:5", (), id="numeric-field"),
        pytest.param("created:[2020 TO 2021]", (), id="range"),
    ],
)
def test_unanalyzed_mode_keeps_every_other_structural_rule(
    reg: FieldRegistry, query: str, expected: tuple[str, ...]
) -> None:
    # Only the TEXT of a contributing leaf changes between the modes; which
    # nodes contribute at all does not (except for the zero-token leaf,
    # which analysis alone can identify).
    assert tokens(query, reg, analyzed=False) == expected


def test_multitoken_first_still_takes_only_the_first_token() -> None:
    # Guards the per-leaf analysis path: FIRST is a property of the field's
    # spec, and it must survive analysing a leaf on its own.
    def splitting_analyzer(text: str) -> list[str]:
        return [part for part in text.split("-") if part]

    freg = FieldRegistry(
        [
            FieldSpec(
                "content",
                FieldKind.TEXT,
                analyzer=splitting_analyzer,
                multitoken=Multitoken.FIRST,
            )
        ]
    )
    result = wc.parse("alpha-beta", registry=freg, default_fields=["content"])
    assert not result.diagnostics
    assert free_text_tokens(result.ast, registry=freg, fields=("content",)) == ("alpha",)
    assert free_text_tokens(result.ast, registry=freg, fields=("content",), analyzed=False) == (
        "alpha-beta",
    )


def test_an_already_analyzed_tree_cannot_answer_the_polarity_question(
    stem_reg: FieldRegistry,
) -> None:
    # Pins the documented precondition on ``node`` (and DIVERGENCES.md entry
    # 23's qualifier): the guarantee is about the tree as parsed. A caller
    # who analyzes first hands over a tree where the AndNot has already
    # collapsed onto its negative side, and no later walk can tell that node
    # apart from one the user asked for. Both modes degrade, and the raw
    # mode degrades twice over: it returns *analyzed* text, having no other
    # left to return. Not guarded in code (an analyzed tree is structurally
    # an ordinary tree); pinned here so the precondition is not mistaken for
    # a caveat nobody checked.
    fields = ("content", "title")
    result = wc.parse("the ANDNOT secret", registry=stem_reg, default_fields=["content", "title"])
    collapsed = analyze(normalize(result.ast), stem_reg)
    assert free_text_tokens(collapsed, registry=stem_reg, fields=fields) == ("secret",)
    assert free_text_tokens(collapsed, registry=stem_reg, fields=fields, analyzed=False) == (
        "secret",
    )

    stemmed = wc.parse("universities", registry=stem_reg, default_fields=["content", "title"])
    assert free_text_tokens(
        analyze(normalize(stemmed.ast), stem_reg),
        registry=stem_reg,
        fields=fields,
        analyzed=False,
    ) == ("univers",)
