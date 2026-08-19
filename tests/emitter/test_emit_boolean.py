import re
import time
from collections.abc import Callable

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat import parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


def test_implicit_and(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="shopname"),
            ast.Term(field=FieldRef("content"), text="product2"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [4]


def test_or_min_should(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Term(field=FieldRef("content"), text="receipt"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_or_all_children_dropped_is_empty(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="!!!"),
            ast.Term(field=FieldRef("content"), text="???"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_or_single_surviving_child_returned_unwrapped(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Term(field=FieldRef("content"), text="!!!"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_not_padded(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Not(child=ast.Term(field=FieldRef("content"), text="invoice"))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 3, 4, 5]


def test_nested_all_negative(tindex: TIndex, ereg: FieldRegistry) -> None:
    # The quickwit-oss/tantivy#3025 shape: a group whose clauses are all
    # MustNot must still be padded so it behaves as "all docs except...",
    # both at the inner And and if it ever bubbles up unpadded.
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("tag"), text="billing"),
            ast.And(
                children=(
                    ast.Not(child=ast.Term(field=FieldRef("title"), text="2019")),
                    ast.Not(child=ast.Term(field=FieldRef("title"), text="2018")),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_andnot(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.AndNot(
        positive=ast.Term(field=FieldRef("content"), text="shopname"),
        negative=ast.Term(field=FieldRef("content"), text="product2"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2]


def test_andmaybe(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.AndMaybe(
        required=ast.Term(field=FieldRef("tag"), text="billing"),
        optional=ast.Term(field=FieldRef("content"), text="invoice"),
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2]


def test_require_filters_not_scores(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Require(
        scored=ast.Term(field=FieldRef("tag"), text="billing"),
        filter_only=ast.Term(field=FieldRef("title"), text="2020"),
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
def test_zero_token_operand_leaves_the_survivor(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node], query: str
) -> None:
    node = parse(query)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- DIVERGENCES.md entry 23: NOT of a zero-token term matches every
# document here; whoosh normalizes Not(NullQuery) to NullQuery and matches
# none.


def test_not_zero_token_term_matches_everything(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    node = parse("NOT content:!!!")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Docs 3 and 5 both have no tags at all.
        pytest.param(False, [3, 5], id="exists-false-matches-docs-without-tags"),
        pytest.param(True, [1, 2, 4], id="exists-true-matches-docs-with-tags"),
    ],
)
def test_boolean_exists(
    tindex: TIndex, ereg: FieldRegistry, value: bool, expected: list[int]
) -> None:
    node = ast.Term(field=FieldRef("has_tag"), text=value)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- DIVERGENCES.md entry 33: BOOLEAN_EXISTS truthiness on quoted values ----
#
# has_tag:'' (quoted empty) now agrees with real whoosh: both sides fall
# through to a falsy result (bool("") is False on whoosh's side; an
# empty-after-strip value is now explicitly falsy on whoosh-compat's).
#
# has_tag:'  false  ' and has_tag:'F ' are the *remaining*, intended
# divergence: real whoosh checks the unstripped lowered text against its
# trues/falses sets and falls through to bool(qstring) for anything else, so
# whitespace-padded text that isn't an exact match reads True on whoosh's
# side (a non-empty string is always truthy). whoosh-compat strips before
# the membership check, so the same padded text reads False. Plain
# has_tag:true/has_tag:false controls are unaffected by either change.


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("has_tag:''", [3, 5], id="quoted-empty-agrees-with-whoosh-false"),
        pytest.param(
            "has_tag:'  false  '",
            [3, 5],
            id="padded-false-diverges-from-whoosh-true",
        ),
        pytest.param(
            "has_tag:'F '",
            [3, 5],
            id="padded-true-looking-diverges-from-whoosh-true",
        ),
        pytest.param("has_tag:true", [1, 2, 4], id="plain-true-control"),
        pytest.param("has_tag:false", [3, 5], id="plain-false-control"),
    ],
)
def test_boolean_exists_quoted_truthiness_shapes(
    tindex: TIndex,
    ereg: FieldRegistry,
    parse: Callable[[str], ast.Node],
    query: str,
    expected: list[int],
) -> None:
    node = parse(query)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- exists checks on a fast JSON field --------------------------
#
# A JSON fast field's subpath columns are only checked by exists_query when
# json_subpaths=True is passed; the default silently checks nothing, so a
# document that has a value reads as absent. Docs 1, 4 and 5 have a value
# under "attrs" (ereg's fast JSON field); docs 2 and 3 have none.


def test_every_field_fast_json(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Every(field=FieldRef("attrs"))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4, 5]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(True, [1, 4, 5], id="exists-true-matches-docs-with-attrs"),
        pytest.param(False, [2, 3], id="exists-false-matches-docs-without-attrs"),
    ],
)
def test_boolean_exists_fast_json(
    tindex: TIndex, ereg: FieldRegistry, value: bool, expected: list[int]
) -> None:
    node = ast.Term(field=FieldRef("has_attrs"), text=value)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- BOOLEAN_EXISTS targeting a non-fast field (end-to-end) -----------------


def _non_fast_text_target_fixture() -> tuple[tantivy.Index, tantivy.Schema, FieldRegistry]:
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
    ("value", "expected"),
    [
        pytest.param(True, [1], id="exists-true-matches-doc-with-body"),
        pytest.param(
            False,
            [2, 3, 4],
            id="exists-false-matches-doc-without-body-and-untokenizable-values",
        ),
    ],
)
def test_boolean_exists_non_fast_text_target(value: bool, expected: list[int]) -> None:
    index, _schema, registry = _non_fast_text_target_fixture()
    node = ast.Term(field=FieldRef("has_body"), text=value)
    q = emit_(node, index=index, registry=registry)
    assert search_ids(index, q) == expected


# -- BOOLEAN_EXISTS targeting a non-fast KEYWORD field (end-to-end) ---------


def _non_fast_keyword_target_fixture() -> tuple[tantivy.Index, tantivy.Schema, FieldRegistry]:
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
    ("value", "expected"),
    [
        pytest.param(True, [1], id="exists-true-matches-doc-with-label"),
        pytest.param(
            False,
            [2, 3, 4],
            id="exists-false-matches-doc-without-label-and-untokenizable-values",
        ),
    ],
)
def test_boolean_exists_non_fast_keyword_target(value: bool, expected: list[int]) -> None:
    index, _schema, registry = _non_fast_keyword_target_fixture()
    node = ast.Term(field=FieldRef("has_label"), text=value)
    q = emit_(node, index=index, registry=registry)
    assert search_ids(index, q) == expected


def test_registry_rejects_non_fast_non_text_non_keyword_exists_target() -> None:
    """A non-fast, non-TEXT, non-KEYWORD exists_target (e.g. a non-fast U64
    field) has no way to answer 'exists' at all: regex_query only matches a
    text/string field, and exists_query requires a fast field. This is now
    rejected at registry construction rather than surfacing later as a
    QueryError (or an opaque tantivy error) at emit/search time.
    """
    with pytest.raises(ValueError, match="has_pages"):
        FieldRegistry(
            [
                FieldSpec("page_count", FieldKind.U64),  # not fast, not TEXT/KEYWORD
                FieldSpec("has_pages", FieldKind.BOOLEAN_EXISTS, exists_target="page_count"),
            ]
        )


# -- Every(field) and BOOLEAN_EXISTS agree on the same field -----------------


def test_every_field_and_boolean_exists_agree(tindex: TIndex, ereg: FieldRegistry) -> None:
    """``field:*`` (Every(field)) and a BOOLEAN_EXISTS field targeting the
    *same* field return the exact same document set, because both go
    through ``_exists_query`` reading the single strategy the registry
    resolved for that field (``ereg``'s ``has_tag_kw`` targets ``tag``
    itself), not two independently-derived answers.
    """
    every_q = emit_ast(ast.Every(field=FieldRef("tag")), tindex, ereg)
    boolean_exists_q = emit_ast(ast.Term(field=FieldRef("has_tag_kw"), text=True), tindex, ereg)
    assert search_ids(tindex[0], every_q) == search_ids(tindex[0], boolean_exists_q) == [1, 2, 4]


def test_every_field_on_the_boolean_exists_field_itself(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # has_tag:* (Every(field="has_tag"), a BOOLEAN_EXISTS field, not one of
    # its targets) used to raise QueryError: BOOLEAN_EXISTS has
    # no resolved exists strategy of its own (it has no physical column;
    # "existence" only ever makes sense via its exists_target). visit_term's
    # BOOLEAN_EXISTS branch already redirects through exists_target;
    # visit_every needs the same redirect for a bare field:* on the
    # BOOLEAN_EXISTS field itself, the "matches the unquoted form"
    # comparison exposed the gap.
    every_q = emit_ast(ast.Every(field=FieldRef("has_tag")), tindex, ereg)
    boolean_exists_q = emit_ast(ast.Term(field=FieldRef("has_tag"), text=True), tindex, ereg)
    assert search_ids(tindex[0], every_q) == search_ids(tindex[0], boolean_exists_q) == [1, 2, 4]


def test_every_field_and_boolean_exists_agree_across_targets(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    """The same "has tags" condition, reached through two different targets
    with two different resolved strategies (``has_tag`` -> the fast U64
    ``tag_id`` presence marker -> FAST_FIELD, ``tag:*`` -> the non-fast
    KEYWORD ``tag`` field -> TERM_SCAN), still agrees on the document set:
    both answer the same real-world question about the same docs.
    """
    every_q = emit_ast(ast.Every(field=FieldRef("tag")), tindex, ereg)
    boolean_exists_q = emit_ast(ast.Term(field=FieldRef("has_tag"), text=True), tindex, ereg)
    assert search_ids(tindex[0], every_q) == search_ids(tindex[0], boolean_exists_q) == [1, 2, 4]


def test_nothing(tindex: TIndex, ereg: FieldRegistry) -> None:
    q = emit_ast(ast.Nothing(), tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_every(tindex: TIndex, ereg: FieldRegistry) -> None:
    q = emit_ast(ast.Every(), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4, 5]


def test_boosted(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Boosted(child=ast.Term(field=FieldRef("content"), text="invoice"), boost=2.0)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_boosted_bad_boost_type_is_backend_rejected(tindex: TIndex, ereg: FieldRegistry) -> None:
    # A safety-net case, not one of the individually-fixed
    # branches: tantivy.Query.boost_query itself raises a bare TypeError
    # for a non-numeric boost (only reachable via a hand-built AST; the
    # parser's own BoostPlugin only ever produces a float). Caught by
    # TantivyEmitter.emit()'s visit-stage (ValueError, TypeError) ->
    # BACKEND_REJECTED conversion: the refusal came from tantivy-py, not
    # from this emitter noticing the bad shape first. The contract it
    # guarantees is "never a bare exception" for shapes that don't have
    # (and don't need) their own specific handling.
    node = ast.Boosted(child=ast.Term(field=FieldRef("content"), text="invoice"), boost="bad")  # type: ignore[arg-type]
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.BACKEND_REJECTED
    assert exc.value.diagnostic.cause is Cause.INTERNAL


def test_boosted_group_child_with_tokens_is_wrapped(tindex: TIndex, ereg: FieldRegistry) -> None:
    # _group_child's Boosted branch when the inner term has real tokens (not
    # dropped): the boost_query wrapping itself, as opposed to the
    # zero-token-drop case covered in test_emit_terms.py.
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Boosted(child=ast.Term(field=FieldRef("content"), text="total"), boost=2.0),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- nested group whose children all drop must itself drop ------------------


def _stopword_registry() -> FieldRegistry:
    """A standalone registry whose "content" analyzer drops "the"/"a" as
    stopwords, reused against the shared `tindex` fixture's real doc content
    (doc 1's content is "invoice total amount").

    Used to reproduce: a nested And/Or group whose every child analyzes to
    zero tokens (an all-stopword group) must itself be dropped from its
    enclosing group, not survive as a live `empty_query()` that wrongly
    requires nothing to be true and kills a sibling required clause.
    """
    stopwords = {"the", "a"}

    def analyzer(text: str) -> list[str]:
        return [t for t in re.split(r"\W+", text.lower()) if t and t not in stopwords]

    return FieldRegistry([FieldSpec("content", FieldKind.TEXT, analyzer=analyzer)])


def test_nested_or_all_dropped_is_dropped_from_and(tindex: TIndex) -> None:
    # Repro from the task: invoice AND ("the" OR "a") must match doc 1, not [].
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Or(
                children=(
                    ast.Term(field=FieldRef("content"), text="the"),
                    ast.Term(field=FieldRef("content"), text="a"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_and_all_dropped_is_dropped_from_or(tindex: TIndex) -> None:
    ereg = _stopword_registry()
    node = ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.And(
                children=(
                    ast.Term(field=FieldRef("content"), text="the"),
                    ast.Term(field=FieldRef("content"), text="a"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_deeply_nested_group_all_dropped_is_dropped(tindex: TIndex) -> None:
    # Arbitrary nesting depth: And(Or(And(the, a))) inside the outer And.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Or(
                children=(
                    ast.And(
                        children=(
                            ast.Term(field=FieldRef("content"), text="the"),
                            ast.Term(field=FieldRef("content"), text="a"),
                        )
                    ),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_group_all_dropped_through_boosted_composes(tindex: TIndex) -> None:
    # The nested-group drop rule must compose with the existing Boosted
    # unwrapping: Boosted(Or(all-dropped)) is still dropped, not turned into
    # a live boost_query(empty_query(), ...) clause.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Boosted(
                child=ast.Or(
                    children=(
                        ast.Term(field=FieldRef("content"), text="the"),
                        ast.Term(field=FieldRef("content"), text="a"),
                    )
                ),
                boost=2.0,
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_nested_group_with_surviving_child_is_not_dropped(tindex: TIndex) -> None:
    # Sanity check on the other side of the fix: a nested group with at
    # least one surviving child must NOT be dropped.
    ereg = _stopword_registry()
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Or(
                children=(
                    ast.Term(field=FieldRef("content"), text="the"),
                    ast.Term(field=FieldRef("content"), text="total"),
                )
            ),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_non_text_term_as_direct_group_child(tindex: TIndex, ereg: FieldRegistry) -> None:
    # _group_child's `if spec.kind in (TEXT, KEYWORD)` skip branch: a U64
    # term as a direct child of an And group (the zero-token-drop check only
    # applies to TEXT/KEYWORD; other kinds fall straight through to visit()).
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("asn"), text=100),
            ast.Term(field=FieldRef("content"), text="invoice"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- emit time must not be exponential in nesting depth for
# -- alternating boolean groups. _group_child used to decide whether a
# -- nested group drops by fully *emitting* its children (discarding the
# -- result) and then emitting the whole subtree again if it survives, so
# -- work doubled per nesting level, which reaches minutes at depth ~20 for
# -- an alternating And/Or tree with a zero-token first child at every level
# -- (that shape prevents the drop check from short-circuiting).


def _alternating_drop_tree(depth: int) -> ast.Node:
    """Alternating And/Or, `depth` levels deep: each level's first child is
    a zero-token term ("the", dropped by _stopword_analyzer below) and the
    second child is the next level down, bottoming out in a real, surviving
    term. Matches the original exponential-blowup repro shape exactly.
    """
    node: ast.Node = ast.Term(field=FieldRef("content"), text="invoice")
    for i in range(depth):
        cls = ast.And if i % 2 == 0 else ast.Or
        node = cls(
            children=(
                ast.Term(field=FieldRef("content"), text="the"),
                node,
            )
        )
    return node


def _stopword_analyzer(calls: list[str], text: str) -> list[str]:
    calls.append(text)
    return [] if text == "the" else [text]


def _alternating_drop_fixture(calls: list[str]) -> tuple[tantivy.Index, FieldRegistry]:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_text("content", "invoice")
    w.add_document(doc)
    w.commit()
    index.reload()

    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: _stopword_analyzer(calls, t))]
    )
    return index, registry


def test_alternating_nesting_emit_is_not_exponential() -> None:
    calls: list[str] = []
    index, registry = _alternating_drop_fixture(calls)
    node = _alternating_drop_tree(40)

    q = emit_(node, index=index, registry=registry)

    assert search_ids(index, q) == [1]
    # Polynomial (measured quadratic: 66/231/496/861 analyzer calls at
    # depth 10/20/30/40), not exponential: the old code doubled work per
    # nesting level, which would put depth 40 in the tens of thousands at
    # least (depth 20 alone measured ~6.5s wall-clock in the issue). 2000
    # comfortably covers the legitimate quadratic count while still
    # catching a regression back to exponential growth.
    assert len(calls) < 2000, f"analyzer called {len(calls)} times for depth 40 (expected < 2000)"


def test_alternating_nesting_emit_is_fast() -> None:
    calls: list[str] = []
    index, registry = _alternating_drop_fixture(calls)
    node = _alternating_drop_tree(40)

    start = time.perf_counter()
    emit_(node, index=index, registry=registry)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"depth-40 emit took {elapsed:.3f}s (expected well under 1s)"


def test_nested_all_drop_group_is_still_dropped_from_parent() -> None:
    # Unaffected by the fix: a nested group whose children all drop is
    # still dropped from its enclosing group, not emitted as a live
    # unsatisfiable clause.
    calls: list[str] = []
    index, registry = _alternating_drop_fixture(calls)
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Or(
                children=(
                    ast.Term(field=FieldRef("content"), text="the"),
                    ast.Term(field=FieldRef("content"), text="the"),
                )
            ),
        )
    )
    q = emit_(node, index=index, registry=registry)
    assert search_ids(index, q) == [1]


# -- EXISTS_REQUIRES_FAST diagnostics carry a source span --------------------


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("notes:*", id="star-json"),
        pytest.param("notes.user:*", id="star-subpath"),
        pytest.param("slow_num:[TO]", id="double-open-inclusive"),
        pytest.param("slow_num:{TO}", id="double-open-exclusive"),
        pytest.param("slow_date:[TO]", id="double-open-date"),
    ],
)
def test_exists_requires_fast_carries_a_span(
    nonfast_reg: FieldRegistry, nonfast_index: TIndex, query: str
) -> None:
    result = parse(query, registry=nonfast_reg, default_fields=["content"])
    assert not result.diagnostics
    with pytest.raises(QueryError) as exc:
        emit_(result.ast, index=nonfast_index[0], registry=nonfast_reg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.EXISTS_REQUIRES_FAST
    assert d.startchar is not None, "span is required for a host-reachable kind"
    assert d.endchar is not None
