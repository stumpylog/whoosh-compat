"""Phrase emission (including the 1- and 0-token degenerate cases)."""

import pytest

from whoosh_compat import ast

from .conftest import emit_ast
from .conftest import search_ids


@pytest.mark.parametrize(
    "text, slop, expected",
    [
        # whoosh slop=1 ("adjacent") maps to tantivy slop=0.
        pytest.param("shopname product1", 1, [2, 4], id="adjacent-slop-1-matches-both-docs"),
        # doc 4 is "shopname product1 product2": one intervening token, so it
        # needs whoosh slop=2 (tantivy slop=1).
        pytest.param("shopname product2", 2, [4], id="slop-2-covers-one-intervening-token"),
        pytest.param("shopname product2", 1, [], id="slop-1-too-small-matches-nothing"),
        # Analysis reduces this to one token -> term-query fallback (tantivy
        # rejects a single-word phrase query).
        pytest.param("invoice!!", 1, [1], id="single-token-falls-back-to-term-query"),
        pytest.param("!!!", 1, [], id="zero-tokens-matches-nothing"),
    ],
)
def test_phrase(tindex, ereg, text, slop, expected):
    node = ast.Phrase(field="content", text=text, slop=slop)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_phrase_parsed(tindex, ereg, parse):
    q = emit_ast(parse('content:"shopname product1"'), tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]


def test_zero_token_phrase_dropped_as_group_child(tindex, ereg):
    # Regression: a zero-token analyzed Phrase (e.g. an all-stopword value)
    # nested inside an And must be dropped from the enclosing group exactly
    # like a zero-token Term already is, not emitted as a live-but-empty
    # query that turns into an unsatisfiable Must clause and kills the
    # whole And. Mirrors real whoosh, which drops the empty phrase clause
    # entirely at parse time (verified against the oracle: QueryParser
    # parses `foo AND "the"` with "the" as a stopword down to just
    # Term('content', 'foo')).
    grp = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Phrase(field="content", text="!!!", slop=1),
        )
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]
