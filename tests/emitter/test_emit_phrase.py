"""Phrase emission (including the 1- and 0-token degenerate cases)."""

from whoosh_compat import ast

from .conftest import emit_ast, search_ids


def test_phrase(tindex, ereg):
    # whoosh slop=1 ("adjacent") maps to tantivy slop=0.
    node = ast.Phrase(field="content", text="shopname product1", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]


def test_phrase_slop(tindex, ereg):
    # doc 4 is "shopname product1 product2": one intervening token, so it
    # needs whoosh slop=2 (tantivy slop=1).
    node = ast.Phrase(field="content", text="shopname product2", slop=2)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [4]


def test_phrase_slop_too_small(tindex, ereg):
    node = ast.Phrase(field="content", text="shopname product2", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_phrase_single_token(tindex, ereg):
    # Analysis reduces this to one token -> term-query fallback (tantivy
    # rejects a single-word phrase query).
    node = ast.Phrase(field="content", text="invoice!!", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_phrase_zero_tokens(tindex, ereg):
    node = ast.Phrase(field="content", text="!!!", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_phrase_parsed(tindex, ereg, parse):
    q = emit_ast(parse('content:"shopname product1"'), tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]
