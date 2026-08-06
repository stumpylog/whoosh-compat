"""Wildcard / Prefix / Every(field) emission.

Every expected doc-id list below was derived by running Python's
``fnmatch.fnmatch()`` (whoosh's own glob engine: ``query.Wildcard`` compiles
its pattern with ``fnmatch.translate``) over the fixture's *analyzed* tokens,
not by eyeballing the raw document text. See the module-level token map.
"""

import fnmatch

import pytest

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import glob_to_regex

from .conftest import emit_ast
from .conftest import search_ids

# The analyzed (== indexed) tokens for each fixture doc, per field. Used by
# ``fnmatch_ids`` below to keep the expected values honest.
TITLE_TOKENS = {
    1: ["steuer", "2020"],
    2: ["steuer", "2019"],
    3: ["entwässerungsplan"],
    4: ["report", "2020"],
}
CONTENT_TOKENS = {
    1: ["invoice", "total", "amount"],
    2: ["receipt", "shopname", "product1"],
    3: ["plan", "entwasserung", "basement"],
    4: ["shopname", "product1", "product2"],
}


def fnmatch_ids(tokens, pattern):
    """The doc ids fnmatch (the ground-truth oracle) says ``pattern`` hits."""
    pattern = pattern.lower()  # == the fields' pattern_normalizer
    return sorted(
        doc_id
        for doc_id, toks in tokens.items()
        if any(fnmatch.fnmatch(t, pattern) for t in toks)
    )


# -- glob_to_regex unit-level behavior ---------------------------------------


@pytest.mark.parametrize("pattern, normalizer, expected", [
    pytest.param("produ*1", None, "produ.*1", id="star-becomes-dot-star"),
    pytest.param("a?b", None, "a.b", id="question-mark-becomes-dot"),
    pytest.param("Entwä*", str.lower, "entwä.*", id="normalizer-applied-to-literal-run"),
    # A literal "." must not become "any character".
    pytest.param("a.b*", None, "a\\.b.*", id="literal-metachar-escaped"),
    pytest.param("202[0-3]", None, "202[0-3]", id="character-class-passthrough"),
    # fnmatch spells negation "[!...]"; the regex dialect spells it "[^...]".
    pytest.param("202[!0-3]", None, "202[^0-3]", id="negated-class-fnmatch-to-regex-dialect"),
    # fnmatch.translate("202[0-3") == "(?s:202\\[0\\-3)\\z": the unmatched
    # "[" is an ordinary character, not a class opener.
    pytest.param("202[0-3", None, "202\\[0\\-3", id="unclosed-bracket-is-literal"),
    # Only literal runs go through the normalizer; the class body is passed
    # through as class syntax.
    pytest.param("AB[0-3]*", str.lower, "ab[0-3].*", id="class-body-not-normalized-or-escaped-away"),
    # A "*" immediately following a class collapses runs the same as bare
    # "**" (the `while pattern[i]=="*"` collapse only fires after `flush()`,
    # not right after a class: this is really exercising the "**" collapse
    # itself with a class in front of it).
    pytest.param("a**b", None, "a.*b", id="collapses-runs-of-star"),
    # fnmatch's bracket parser treats a "]" right after "[" (or "[!") as an
    # ordinary member of the class, not the closer.
    pytest.param("[]a]", None, "[]a]", id="leading-bracket-is-literal-member"),
    pytest.param("[!]a]", None, "[^]a]", id="negated-leading-bracket-is-literal-member"),
    # A trailing "-" with nothing after it inside the class is appended to
    # the previous chunk rather than starting a new (empty) one.
    pytest.param("[a-]", None, "[a\\-]", id="trailing-hyphen-appends-to-previous-chunk"),
    # Multiple "-"-separated chunks where a later chunk's start sorts before
    # the previous chunk's end collapse into one chunk (an actual [9-1]
    # range is invalid in a regex; CPython's fnmatch treats it as a literal
    # sequence instead by merging the empty range away).
    pytest.param("[9-1-5-3]", None, "[\\-]", id="empty-range-chunks-are-merged-away"),
])
def test_glob_to_regex(pattern, normalizer, expected):
    assert glob_to_regex(pattern, normalizer) == expected


def test_glob_to_regex_escapes_class_internal_set_operators(tindex, ereg):
    # Python's `re` accepts "[a[]"; Rust's regex crate (tantivy) reads "[",
    # "&" and "~" inside a class as nested-class / set-operator syntax and
    # rejects the pattern. Escaping them keeps both engines happy and does
    # not change the matched language.
    assert glob_to_regex("[a[]", None) == "[a\\[]"
    assert glob_to_regex("[a&]", None) == "[a\\&]"
    # ...and tantivy must actually accept the result.
    q = emit_ast(ast.Wildcard(field="title", pattern="[a[]"), tindex, ereg)
    assert search_ids(tindex[0], q) == []


# -- wildcard emission -------------------------------------------------------


@pytest.mark.parametrize("field, tokens, pattern, expected", [
    pytest.param("title", TITLE_TOKENS, "Entwä*", [3], id="diacritic-prefix-star"),
    pytest.param("content", CONTENT_TOKENS, "produ*1", [2, 4], id="infix-star"),
    pytest.param("content", CONTENT_TOKENS, "product?", [2, 4], id="question-mark"),
    # paperless-ngx issue #13568: saved views use "202[0-3]"-style bracket
    # classes. Constructed as a Wildcard node directly (whoosh's
    # WildcardPlugin only *tags* text containing "*"/"?", so bare
    # "202[0-3]" would lex as a Term) to exercise the emitter's raw class
    # handling.
    pytest.param("title", TITLE_TOKENS, "202[0-3]", [1, 4], id="character-class-13568"),
    pytest.param("title", TITLE_TOKENS, "201[!0-8]", [2], id="negated-character-class"),
    # An unmatched "[" is a literal character; no fixture token contains one,
    # so this matches nothing: in contrast to the closed-class form above,
    # which matches [1, 4].
    pytest.param("title", TITLE_TOKENS, "202[0-3", [], id="unclosed-bracket-is-literal"),
    # The exact wildcard shape from paperless-ngx issue #13568.
    pytest.param("title", TITLE_TOKENS, "*202[0-3]", [1, 4], id="13568-leading-star-class"),
    pytest.param("title", TITLE_TOKENS, "*201[0-9]", [2], id="13568-negated"),
])
def test_wildcard_emission(tindex, ereg, field, tokens, pattern, expected):
    # Keep the fnmatch oracle result honest alongside the emitter's result.
    assert fnmatch_ids(tokens, pattern) == expected
    q = emit_ast(ast.Wildcard(field=field, pattern=pattern), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- prefix emission ---------------------------------------------------------


def test_prefix(tindex, ereg):
    expected = fnmatch_ids(CONTENT_TOKENS, "shopn*")
    assert expected == [2, 4]
    q = emit_ast(ast.Prefix(field="content", text="shopn"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_prefix_normalizes_and_escapes(tindex, ereg):
    # Prefix text is a *literal*: it is normalized then regex-escaped, so a
    # "[" in it matches only a real "[" (nothing in the fixture).
    q = emit_ast(ast.Prefix(field="title", text="Entwä"), tindex, ereg)
    assert search_ids(tindex[0], q) == [3]
    q = emit_ast(ast.Prefix(field="title", text="202[0-3]"), tindex, ereg)
    assert search_ids(tindex[0], q) == []


# -- Every(field) ------------------------------------------------------------


@pytest.mark.parametrize("field, expected", [
    pytest.param(None, [1, 2, 3, 4, 5], id="unfielded-matches-all-docs"),
    pytest.param("asn", [1, 2, 3, 4, 5], id="fast-field-matches-all-docs"),
    # 'tag' is a non-fast KEYWORD field -> regex ".*" fallback; doc 3 has no
    # tags at all, so it is excluded.
    pytest.param("tag", [1, 2, 4], id="non-fast-text-field-excludes-doc-without-value"),
])
def test_every_field(tindex, ereg, field, expected):
    q = emit_ast(ast.Every(field=field), tindex, ereg)
    assert search_ids(tindex[0], q) == expected
