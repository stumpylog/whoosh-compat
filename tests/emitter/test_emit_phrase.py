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
