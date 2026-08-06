import pytest

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


def test_or_all_children_dropped_is_empty(tindex, ereg):
    node = ast.Or(children=(
        ast.Term(field="content", text="!!!"),
        ast.Term(field="content", text="???"),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_or_single_surviving_child_returned_unwrapped(tindex, ereg):
    node = ast.Or(children=(
        ast.Term(field="content", text="invoice"),
        ast.Term(field="content", text="!!!"),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


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


@pytest.mark.parametrize("value, expected", [
    # Docs 3 and 5 both have no tags at all.
    pytest.param(False, [3, 5], id="exists-false-matches-docs-without-tags"),
    pytest.param(True, [1, 2, 4], id="exists-true-matches-docs-with-tags"),
])
def test_boolean_exists(tindex, ereg, value, expected):
    node = ast.Term(field="has_tag", text=value)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


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


def test_boosted_group_child_with_tokens_is_wrapped(tindex, ereg):
    # _group_child's Boosted branch when the inner term has real tokens (not
    # dropped): the boost_query wrapping itself, as opposed to the
    # zero-token-drop case covered in test_emit_terms.py.
    node = ast.And(children=(
        ast.Term(field="content", text="invoice"),
        ast.Boosted(child=ast.Term(field="content", text="total"), boost=2.0),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_non_text_term_as_direct_group_child(tindex, ereg):
    # _group_child's `if spec.kind in (TEXT, KEYWORD)` skip branch: a U64
    # term as a direct child of an And group (the zero-token-drop check only
    # applies to TEXT/KEYWORD; other kinds fall straight through to visit()).
    node = ast.And(children=(
        ast.Term(field="asn", text=100),
        ast.Term(field="content", text="invoice"),
    ))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]
