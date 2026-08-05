from whoosh_compat import ast

from .conftest import emit_ast, search_ids


def test_term(tindex, ereg, parse):
    q = emit_ast(parse("content:invoice"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_multitoken_and(tindex, ereg):
    # A single field value with multiple tokens, combined per Multitoken
    # resolution (DEFAULT -> enclosing group semantics; top level == AND).
    # docs 2 and 4 both contain "shopname" and "product1" in content.
    node = ast.Term(field="content", text="shopname product1")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]


def test_u64(tindex, ereg):
    q = emit_ast(ast.Term(field="asn", text=100), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_zero_token_term_dropped(tindex, ereg):
    grp = ast.And(children=(
        ast.Term(field="content", text="invoice"),
        ast.Term(field="content", text="!!!"),
    ))
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_zero_token_term_standalone_matches_nothing(tindex, ereg):
    q = emit_ast(ast.Term(field="content", text="!!!"), tindex, ereg)
    assert search_ids(tindex[0], q) == []
