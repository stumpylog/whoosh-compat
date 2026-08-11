import re

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import emit_ast
from .conftest import search_ids


def test_implicit_and(tindex, ereg):
    node = ast.And(
        children=(
            ast.Term(field="content", text="shopname"),
            ast.Term(field="content", text="product2"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [4]


def test_or_min_should(tindex, ereg):
    node = ast.Or(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Term(field="content", text="receipt"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_or_all_children_dropped_is_empty(tindex, ereg):
    node = ast.Or(
        children=(
            ast.Term(field="content", text="!!!"),
            ast.Term(field="content", text="???"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_or_single_surviving_child_returned_unwrapped(tindex, ereg):
    node = ast.Or(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Term(field="content", text="!!!"),
        )
    )
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
    node = ast.And(
        children=(
            ast.Term(field="tag", text="billing"),
            ast.And(
                children=(
                    ast.Not(child=ast.Term(field="title", text="2019")),
                    ast.Not(child=ast.Term(field="title", text="2018")),
                )
            ),
        )
    )
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
        required=ast.Term(field="tag", text="billing"),
        optional=ast.Term(field="content", text="invoice"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_require_filters_not_scores(tindex, ereg):
    node = ast.Require(
        scored=ast.Term(field="tag", text="billing"),
        filter_only=ast.Term(field="title", text="2020"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


@pytest.mark.parametrize(
    "value, expected",
    [
        # Docs 3 and 5 both have no tags at all.
        pytest.param(False, [3, 5], id="exists-false-matches-docs-without-tags"),
        pytest.param(True, [1, 2, 4], id="exists-true-matches-docs-with-tags"),
    ],
)
def test_boolean_exists(tindex, ereg, value, expected):
    node = ast.Term(field="has_tag", text=value)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- BOOLEAN_EXISTS targeting a non-fast field (end-to-end) -----------------


def _non_fast_text_target_fixture():
    """A standalone tantivy index/registry: a BOOLEAN_EXISTS field whose
    exists_target is a non-fast TEXT field, actually searched.

    FieldRegistry validation permits this shape (a non-fast TEXT
    exists_target), but emission used to always build ``exists_query``,
    which tantivy only accepts for fast fields, so a registry like this one
    used to fail at *search* time with "Field body is not a fast field"
    despite being constructed successfully. Self-contained (not the shared
    ``tindex``/``ereg`` fixtures) so it doesn't require adding an unrelated
    field to those.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("body", stored=True)  # non-fast TEXT field
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, body in [(1, "has a body"), (2, "")]:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        if body:
            doc.add_text("body", body)
        w.add_document(doc)
    w.commit()
    index.reload()

    registry = FieldRegistry(
        [
            FieldSpec("body", FieldKind.TEXT),
            FieldSpec("has_body", FieldKind.BOOLEAN_EXISTS, exists_target="body"),
        ]
    )
    return index, schema, registry


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(True, [1], id="exists-true-matches-doc-with-body"),
        pytest.param(False, [2], id="exists-false-matches-doc-without-body"),
    ],
)
def test_boolean_exists_non_fast_text_target(value, expected):
    index, schema, registry = _non_fast_text_target_fixture()
    node = ast.Term(field="has_body", text=value)
    q = emit_(node, index=index, schema=schema, registry=registry)
    assert search_ids(index, q) == expected


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
    node = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Boosted(child=ast.Term(field="content", text="total"), boost=2.0),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- nested group whose children all drop must itself drop ------------------


def _stopword_registry():
    """A standalone registry whose "content" analyzer drops "the"/"a" as
    stopwords, reused against the shared `tindex` fixture's real doc content
    (doc 1's content is "invoice total amount").

    Used to reproduce: a nested And/Or group whose every child analyzes to
    zero tokens (an all-stopword group) must itself be dropped from its
    enclosing group, not survive as a live `empty_query()` that wrongly
    requires nothing to be true and kills a sibling required clause.
    """
    stopwords = {"the", "a"}

    def analyzer(text):
        return [t for t in re.split(r"\W+", text.lower()) if t and t not in stopwords]

    return FieldRegistry([FieldSpec("content", FieldKind.TEXT, analyzer=analyzer)])


def test_nested_or_all_dropped_is_dropped_from_and(tindex):
    # Repro from the task: invoice AND ("the" OR "a") must match doc 1, not [].
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Or(
                children=(
                    ast.Term(field="content", text="the"),
                    ast.Term(field="content", text="a"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_and_all_dropped_is_dropped_from_or(tindex):
    ereg = _stopword_registry()
    node = ast.Or(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.And(
                children=(
                    ast.Term(field="content", text="the"),
                    ast.Term(field="content", text="a"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_deeply_nested_group_all_dropped_is_dropped(tindex):
    # Arbitrary nesting depth: And(Or(And(the, a))) inside the outer And.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Or(
                children=(
                    ast.And(
                        children=(
                            ast.Term(field="content", text="the"),
                            ast.Term(field="content", text="a"),
                        )
                    ),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_group_all_dropped_through_boosted_composes(tindex):
    # The nested-group drop rule must compose with the existing Boosted
    # unwrapping: Boosted(Or(all-dropped)) is still dropped, not turned into
    # a live boost_query(empty_query(), ...) clause.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Boosted(
                child=ast.Or(
                    children=(
                        ast.Term(field="content", text="the"),
                        ast.Term(field="content", text="a"),
                    )
                ),
                boost=2.0,
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_group_with_surviving_child_is_not_dropped(tindex):
    # Sanity check on the other side of the fix: a nested group with at
    # least one surviving child must NOT be dropped.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field="content", text="invoice"),
            ast.Or(
                children=(
                    ast.Term(field="content", text="the"),
                    ast.Term(field="content", text="total"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_non_text_term_as_direct_group_child(tindex, ereg):
    # _group_child's `if spec.kind in (TEXT, KEYWORD)` skip branch: a U64
    # term as a direct child of an And group (the zero-token-drop check only
    # applies to TEXT/KEYWORD; other kinds fall straight through to visit()).
    node = ast.And(
        children=(
            ast.Term(field="asn", text=100),
            ast.Term(field="content", text="invoice"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]
