from whoosh_compat import ast

from .conftest import emit_ast, search_ids


def test_implicit_and(tindex, ereg):
    node = ast.And(children=(
        ast.Term(field="content", text="shopname"),
        ast.Term(field="content", text="product2"),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [4]


def test_or_min_should(tindex, ereg):
    node = ast.Or(children=(
        ast.Term(field="content", text="invoice"),
        ast.Term(field="content", text="receipt"),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_not_padded(tindex, ereg):
    node = ast.Not(child=ast.Term(field="content", text="invoice"))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 3, 4, 5]


def test_nested_all_negative(tindex, ereg):
    # The quickwit-oss/tantivy#3025 shape: a group whose clauses are all
    # MustNot must still be padded so it behaves as "all docs except...",
    # both at the inner And and if it ever bubbles up unpadded.
    node = ast.And(children=(
        ast.Term(field="tag", text="steuer"),
        ast.And(children=(
            ast.Not(child=ast.Term(field="title", text="2019")),
            ast.Not(child=ast.Term(field="title", text="2018")),
        )),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_andnot(tindex, ereg):
    node = ast.AndNot(
        positive=ast.Term(field="content", text="shopname"),
        negative=ast.Term(field="content", text="product2"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2]


def test_andmaybe(tindex, ereg):
    node = ast.AndMaybe(
        required=ast.Term(field="tag", text="steuer"),
        optional=ast.Term(field="content", text="invoice"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_require_filters_not_scores(tindex, ereg):
    node = ast.Require(
        scored=ast.Term(field="tag", text="steuer"),
        filter_only=ast.Term(field="title", text="2020"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_boolean_exists_false(tindex, ereg):
    # Docs 3 and 5 both have no tags at all.
    node = ast.Term(field="has_tag", text=False)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [3, 5]


def test_boolean_exists_true(tindex, ereg):
    node = ast.Term(field="has_tag", text=True)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 4]


def test_nothing(tindex, ereg):
    q = emit_ast(ast.Nothing(), tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_every(tindex, ereg):
    q = emit_ast(ast.Every(), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4, 5]


def test_boosted(tindex, ereg):
    node = ast.Boosted(child=ast.Term(field="content", text="invoice"), boost=2.0)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]
