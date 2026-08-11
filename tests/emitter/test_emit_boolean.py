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


# -- Zero-token TERM operands of REQUIRE/ANDMAYBE/ANDNOT ---------------------
#
# An operand whose analyzer consumes every token drops out, and the surviving
# operand becomes the whole query, exactly as the same policy already works
# for And/Or children. This matches whoosh, which drops such a term at syntax
# level so the binary operator has nothing left to apply and the survivor
# remains: verified against a live oracle, where all six operand orders below
# return [1]. Phrases are not covered here, because whoosh raises ValueError
# when searching an empty phrase, so a zero-token PHRASE cannot be
# oracle-checked the same way.


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("invoice REQUIRE content:!!!", id="require-zero-token-filter"),
        pytest.param("content:!!! REQUIRE invoice", id="require-zero-token-scored"),
        pytest.param("invoice ANDMAYBE content:!!!", id="andmaybe-zero-token-optional"),
        pytest.param("content:!!! ANDMAYBE invoice", id="andmaybe-zero-token-required"),
        pytest.param("invoice ANDNOT content:!!!", id="andnot-zero-token-negative"),
        pytest.param("content:!!! ANDNOT invoice", id="andnot-zero-token-positive"),
    ],
)
def test_zero_token_operand_leaves_the_survivor(tindex, ereg, parse, query):
    node = parse(query)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- DIVERGENCES.md entry 23: NOT of a zero-token term matches every
# document here; whoosh normalizes Not(NullQuery) to NullQuery and matches
# none.


def test_not_zero_token_term_matches_everything(tindex, ereg, parse):
    node = parse("NOT content:!!!")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4, 5]


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

    Docs 3 and 4 (whitespace-only / punctuation-only ``body`` values) pin
    DIVERGENCES.md entry 20's extended scope: the non-fast fallback
    (``_exists_query``'s ``regex_query(".*")`` against the field's term
    dictionary) means "has at least one indexed term", not "the stored
    field value is non-empty". Both values are indexed as *some* raw text
    (``doc.add_text`` runs, the field is technically "populated"), but
    tantivy's default tokenizer produces zero tokens for either, so no term
    ever reaches the dictionary for that document and the field reads as
    absent, same as doc 2's outright-missing value.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("body", stored=True)  # non-fast TEXT field
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, body in [(1, "has a body"), (2, ""), (3, "!!!"), (4, "   ")]:
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
        pytest.param(
            False,
            [2, 3, 4],
            id="exists-false-matches-doc-without-body-and-untokenizable-values",
        ),
    ],
)
def test_boolean_exists_non_fast_text_target(value, expected):
    index, schema, registry = _non_fast_text_target_fixture()
    node = ast.Term(field="has_body", text=value)
    q = emit_(node, index=index, schema=schema, registry=registry)
    assert search_ids(index, q) == expected


# -- BOOLEAN_EXISTS targeting a non-fast KEYWORD field (end-to-end) ---------


def _non_fast_keyword_target_fixture():
    """A standalone tantivy index/registry: a BOOLEAN_EXISTS field whose
    exists_target is a non-fast KEYWORD field, actually searched.

    Companion to ``_non_fast_text_target_fixture``: KEYWORD was already
    handled by ``_exists_query``'s term-scan fallback at emit time before
    this field's execution strategy was resolved once at registry
    construction, but ``FieldRegistry`` only accepted fast=True or kind=TEXT
    exists_target values, so this exact shape (non-fast KEYWORD) used to be
    *rejected* at construction despite being fully executable at emit time.
    It is now explicitly accepted.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("label", stored=True)  # non-fast KEYWORD field
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, label in [(1, "urgent"), (2, ""), (3, "!!!"), (4, "   ")]:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        if label:
            doc.add_text("label", label)
        w.add_document(doc)
    w.commit()
    index.reload()

    registry = FieldRegistry(
        [
            FieldSpec("label", FieldKind.KEYWORD),
            FieldSpec("has_label", FieldKind.BOOLEAN_EXISTS, exists_target="label"),
        ]
    )
    return index, schema, registry


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(True, [1], id="exists-true-matches-doc-with-label"),
        pytest.param(
            False,
            [2, 3, 4],
            id="exists-false-matches-doc-without-label-and-untokenizable-values",
        ),
    ],
)
def test_boolean_exists_non_fast_keyword_target(value, expected):
    index, schema, registry = _non_fast_keyword_target_fixture()
    node = ast.Term(field="has_label", text=value)
    q = emit_(node, index=index, schema=schema, registry=registry)
    assert search_ids(index, q) == expected


def test_registry_rejects_non_fast_non_text_non_keyword_exists_target():
    """A non-fast, non-TEXT, non-KEYWORD exists_target (e.g. a non-fast U64
    field) has no way to answer 'exists' at all: regex_query only matches a
    text/string field, and exists_query requires a fast field. This is now
    rejected at registry construction rather than surfacing later as an
    UnsupportedQueryError (or an opaque tantivy error) at emit/search time.
    """
    with pytest.raises(ValueError, match="has_pages"):
        FieldRegistry(
            [
                FieldSpec("page_count", FieldKind.U64),  # not fast, not TEXT/KEYWORD
                FieldSpec("has_pages", FieldKind.BOOLEAN_EXISTS, exists_target="page_count"),
            ]
        )


# -- Every(field) and BOOLEAN_EXISTS agree on the same field -----------------


def test_every_field_and_boolean_exists_agree(tindex, ereg):
    """``field:*`` (Every(field)) and a BOOLEAN_EXISTS field targeting the
    *same* field return the exact same document set, because both go
    through ``_exists_query`` reading the single strategy the registry
    resolved for that field (``ereg``'s ``has_tag_kw`` targets ``tag``
    itself), not two independently-derived answers.
    """
    every_q = emit_ast(ast.Every(field="tag"), tindex, ereg)
    boolean_exists_q = emit_ast(ast.Term(field="has_tag_kw", text=True), tindex, ereg)
    assert search_ids(tindex[0], every_q) == search_ids(tindex[0], boolean_exists_q) == [1, 2, 4]


def test_every_field_and_boolean_exists_agree_across_targets(tindex, ereg):
    """The same "has tags" condition, reached through two different targets
    with two different resolved strategies (``has_tag`` -> the fast U64
    ``tag_id`` presence marker -> FAST_FIELD, ``tag:*`` -> the non-fast
    KEYWORD ``tag`` field -> TERM_SCAN), still agrees on the document set:
    both answer the same real-world question about the same docs.
    """
    every_q = emit_ast(ast.Every(field="tag"), tindex, ereg)
    boolean_exists_q = emit_ast(ast.Term(field="has_tag", text=True), tindex, ereg)
    assert search_ids(tindex[0], every_q) == search_ids(tindex[0], boolean_exists_q) == [1, 2, 4]


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
