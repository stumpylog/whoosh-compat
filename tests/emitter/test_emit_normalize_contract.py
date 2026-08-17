"""``emit()`` always normalizes its input first.

Before this contract, ``emit(t)`` and ``emit(normalize(t))`` could return
different matched-document sets for hand-built trees containing literal
empty And/Or groups or ``Nothing()`` siblings: the emitter's ``_node_drops``
predicate and ``normalize()``'s algebra disagreed about what those shapes
mean (``_node_drops`` gave whoosh-style survivor semantics to an empty
group, ``normalize()`` poisons the enclosing group per the AndNot-null
algebra, matching real whoosh). The most alarming case:
``emit(AndNot(positive=And(()), negative=Term(content, "invoice")))`` used to
match the documents containing "invoice", the operand the query asked to
EXCLUDE.

This module pins both minimal repro shapes at the result level (a real
search against a real tantivy index, not just "did emit() raise"), plus a
property test sweeping a small hand-built-AST generator so no other shape in
the same family regresses.
"""

from __future__ import annotations

from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from tests.differential.strategies import plain_word
from whoosh_compat import ast
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids

CONTENT = FieldRef("content")


def test_and_with_empty_or_matches_nothing(tindex: TIndex, ereg: FieldRegistry) -> None:
    # And([Or(()), Term(content, "invoice")]): the empty Or is Nothing()
    # once normalized, which poisons the enclosing And per normalize()'s
    # algebra. Doc 1 ("invoice total amount") must NOT match.
    node = ast.And(children=(ast.Or(children=()), ast.Term(field=CONTENT, text="invoice")))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_and_with_empty_or_agrees_with_normalized(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.And(children=(ast.Or(children=()), ast.Term(field=CONTENT, text="invoice")))
    raw_ids = search_ids(tindex[0], emit_ast(node, tindex, ereg))
    norm_ids = search_ids(tindex[0], emit_ast(normalize(node), tindex, ereg))
    assert raw_ids == norm_ids == []


def test_andnot_with_empty_positive_matches_nothing(tindex: TIndex, ereg: FieldRegistry) -> None:
    # AndNot(positive=And(()), negative=Term(content, "invoice")): a null
    # positive operand collapses the whole AndNot to Nothing() (real
    # whoosh's own AndNot-null rule), not to the negated operand promoted
    # to a positive match. Doc 1 ("invoice total amount") must NOT match:
    # before the fix it was the only doc returned, i.e. the excluded term.
    node = ast.AndNot(
        positive=ast.And(children=()), negative=ast.Term(field=CONTENT, text="invoice")
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_andnot_with_empty_positive_agrees_with_normalized(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    node = ast.AndNot(
        positive=ast.And(children=()), negative=ast.Term(field=CONTENT, text="invoice")
    )
    raw_ids = search_ids(tindex[0], emit_ast(node, tindex, ereg))
    norm_ids = search_ids(tindex[0], emit_ast(normalize(node), tindex, ereg))
    assert raw_ids == norm_ids == []


# --------------------------------------------------------------------------
# Property test: emit(t) and emit(normalize(t)) must always agree on
# matched doc-id sets, for a bounded generator over the node types the
# issue named (And/Or/Not/AndNot/AndMaybe/Require/Term/Nothing), explicitly
# including literal empty-group shapes (the parser itself never produces
# those, see #10, so this is the only layer that exercises them).
# --------------------------------------------------------------------------

_survivor_term = st.builds(lambda w: ast.Term(field=CONTENT, text=w), plain_word)
# lower_fold (ereg's analyzer, see conftest.py) splits on non-word
# characters: a punctuation-only value analyzes to zero tokens, exercising
# the "drops via analysis" path distinct from "literal empty group".
_zero_token_term = st.sampled_from(
    [ast.Term(field=CONTENT, text=w) for w in ("---", "...", "***", "___")]
)
_nothing = st.just(ast.Nothing())

_leaves = st.one_of(_survivor_term, _zero_token_term, _nothing)


def _children(
    node_strategy: st.SearchStrategy[ast.Node],
) -> st.SearchStrategy[tuple[ast.Node, ...]]:
    # min_size=0 is the point: it's what lets And()/Or() come out literally
    # empty, the shape the parser never produces but emit() must still
    # handle consistently with normalize().
    return st.lists(node_strategy, min_size=0, max_size=3).map(tuple)


def _extend(node_strategy: st.SearchStrategy[ast.Node]) -> st.SearchStrategy[ast.Node]:
    return st.one_of(
        _children(node_strategy).map(lambda c: ast.And(children=c)),
        _children(node_strategy).map(lambda c: ast.Or(children=c)),
        node_strategy.map(lambda c: ast.Not(child=c)),
        st.tuples(node_strategy, node_strategy).map(
            lambda t: ast.AndNot(positive=t[0], negative=t[1])
        ),
        st.tuples(node_strategy, node_strategy).map(
            lambda t: ast.AndMaybe(required=t[0], optional=t[1])
        ),
        st.tuples(node_strategy, node_strategy).map(
            lambda t: ast.Require(scored=t[0], filter_only=t[1])
        ),
    )


def _tree(max_leaves: int = 5) -> st.SearchStrategy[ast.Node]:
    return st.recursive(_leaves, _extend, max_leaves=max_leaves)


@given(node=_tree())
@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_emit_agrees_with_emit_of_normalized(
    node: ast.Node, tindex: TIndex, ereg: FieldRegistry
) -> None:
    raw_ids = search_ids(tindex[0], emit_ast(node, tindex, ereg))
    norm_ids = search_ids(tindex[0], emit_ast(normalize(node), tindex, ereg))
    assert raw_ids == norm_ids, f"disagreement for {node!r}: raw={raw_ids} normalized={norm_ids}"
