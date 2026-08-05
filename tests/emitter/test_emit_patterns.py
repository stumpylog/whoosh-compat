"""Wildcard / Prefix / Every(field) emission.

Every expected doc-id list below was derived by running Python's
``fnmatch.fnmatch()`` (whoosh's own glob engine -- ``query.Wildcard`` compiles
its pattern with ``fnmatch.translate``) over the fixture's *analyzed* tokens,
not by eyeballing the raw document text. See the module-level token map.
"""

import fnmatch

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import glob_to_regex

from .conftest import emit_ast, search_ids

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


def test_glob_to_regex_star_and_question():
    assert glob_to_regex("produ*1", None) == "produ.*1"
    assert glob_to_regex("a?b", None) == "a.b"


def test_glob_to_regex_normalizes_literals():
    assert glob_to_regex("Entwä*", str.lower) == "entwä.*"


def test_glob_to_regex_escapes_literal_metachars():
    # A literal "." must not become "any character".
    assert glob_to_regex("a.b*", None) == "a\\.b.*"


def test_glob_to_regex_character_class():
    assert glob_to_regex("202[0-3]", None) == "202[0-3]"


def test_glob_to_regex_negated_character_class():
    # fnmatch spells negation "[!...]"; the regex dialect spells it "[^...]".
    assert glob_to_regex("202[!0-3]", None) == "202[^0-3]"


def test_glob_to_regex_unclosed_bracket_is_literal():
    # fnmatch.translate("202[0-3") == "(?s:202\\[0\\-3)\\z" -- the unmatched
    # "[" is an ordinary character, not a class opener.
    assert glob_to_regex("202[0-3", None) == "202\\[0\\-3"


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


def test_glob_to_regex_class_is_not_normalized_or_escaped_away():
    # Only literal runs go through the normalizer; the class body is passed
    # through as class syntax.
    assert glob_to_regex("AB[0-3]*", str.lower) == "ab[0-3].*"


# -- wildcard emission -------------------------------------------------------


def test_wildcard_entwae(tindex, ereg):
    expected = fnmatch_ids(TITLE_TOKENS, "Entwä*")
    assert expected == [3]
    q = emit_ast(ast.Wildcard(field="title", pattern="Entwä*"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_infix(tindex, ereg):
    expected = fnmatch_ids(CONTENT_TOKENS, "produ*1")
    assert expected == [2, 4]
    q = emit_ast(ast.Wildcard(field="content", pattern="produ*1"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_question_mark(tindex, ereg):
    expected = fnmatch_ids(CONTENT_TOKENS, "product?")
    assert expected == [2, 4]
    q = emit_ast(ast.Wildcard(field="content", pattern="product?"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_character_class(tindex, ereg):
    # paperless-ngx issue #13568: saved views use "202[0-3]"-style bracket
    # classes. Constructed as a Wildcard node directly (whoosh's
    # WildcardPlugin only *tags* text containing "*"/"?", so bare
    # "202[0-3]" would lex as a Term) to exercise the emitter's raw class
    # handling.
    expected = fnmatch_ids(TITLE_TOKENS, "202[0-3]")
    assert expected == [1, 4]
    q = emit_ast(ast.Wildcard(field="title", pattern="202[0-3]"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_negated_character_class(tindex, ereg):
    expected = fnmatch_ids(TITLE_TOKENS, "201[!0-8]")
    assert expected == [2]
    q = emit_ast(ast.Wildcard(field="title", pattern="201[!0-8]"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_unclosed_bracket_is_literal(tindex, ereg):
    # An unmatched "[" is a literal character; no fixture token contains one,
    # so this matches nothing -- in contrast to the closed-class form above,
    # which matches [1, 4].
    expected = fnmatch_ids(TITLE_TOKENS, "202[0-3")
    assert expected == []
    q = emit_ast(ast.Wildcard(field="title", pattern="202[0-3"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_13568_leading_star_class(tindex, ereg):
    # The exact wildcard shape from paperless-ngx issue #13568.
    expected = fnmatch_ids(TITLE_TOKENS, "*202[0-3]")
    assert expected == [1, 4]
    q = emit_ast(ast.Wildcard(field="title", pattern="*202[0-3]"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_wildcard_13568_negated(tindex, ereg):
    expected = fnmatch_ids(TITLE_TOKENS, "*201[0-9]")
    assert expected == [2]
    q = emit_ast(ast.Wildcard(field="title", pattern="*201[0-9]"), tindex, ereg)
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


def test_every_unfielded(tindex, ereg):
    q = emit_ast(ast.Every(), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4]


def test_every_field_fast(tindex, ereg):
    q = emit_ast(ast.Every(field="asn"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4]


def test_every_field_text(tindex, ereg):
    # 'tag' is a non-fast KEYWORD field -> regex ".*" fallback; doc 3 has no
    # tags at all, so it is excluded.
    q = emit_ast(ast.Every(field="tag"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 4]
