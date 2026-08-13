"""Span-preservation tests for ast.normalize().

normalize() must never silently drop startchar/endchar when it rebuilds a
node: a rebuilt node whose structure is unchanged (Not, AndNot, AndMaybe,
Require, Boosted when not collapsing to something else) carries the
original node's own span, and a flattened/merged And/Or carries the union
(min startchar, max endchar) of whatever ended up as its children. See
tests/test_normalize.py for the non-span normalization rules this extends,
and test_parser_basics.py / here for end-to-end confirmation against real
parse() output.
"""

import pytest

import whoosh_compat as wc
from whoosh_compat.ast import And
from whoosh_compat.ast import AndMaybe
from whoosh_compat.ast import AndNot
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import Every
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import Or
from whoosh_compat.ast import Require
from whoosh_compat.ast import Term
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldRegistry


def T(x: str, start: int | None = None, end: int | None = None) -> Term:
    return Term(field=None, text=x, startchar=start, endchar=end)


# -- structure-unchanged wrappers carry the original node's own span -------


class TestWrapperSpanCarried:
    def test_not_carries_own_span(self) -> None:
        t = Not(T("a", 0, 1), startchar=0, endchar=5)
        result = normalize(t)
        assert result == Not(T("a", 0, 1))
        assert (result.startchar, result.endchar) == (0, 5)

    def test_andnot_carries_own_span(self) -> None:
        t = AndNot(positive=T("a", 0, 1), negative=T("b", 5, 6), startchar=0, endchar=6)
        result = normalize(t)
        assert (result.startchar, result.endchar) == (0, 6)

    def test_andnot_negative_nothing_keeps_positive_own_span(self) -> None:
        # Collapses to `positive` verbatim: positive's own (smaller/different)
        # span is kept, not overridden with the wrapper's wider span.
        t = AndNot(positive=T("a", 0, 1), negative=Nothing(), startchar=0, endchar=10)
        result = normalize(t)
        assert result == T("a", 0, 1)
        assert (result.startchar, result.endchar) == (0, 1)

    def test_andmaybe_carries_own_span(self) -> None:
        t = AndMaybe(required=T("a", 0, 1), optional=T("b", 5, 6), startchar=0, endchar=6)
        result = normalize(t)
        assert (result.startchar, result.endchar) == (0, 6)

    def test_andmaybe_optional_nothing_keeps_required_own_span(self) -> None:
        t = AndMaybe(required=T("a", 0, 1), optional=Nothing(), startchar=0, endchar=10)
        result = normalize(t)
        assert result == T("a", 0, 1)
        assert (result.startchar, result.endchar) == (0, 1)

    def test_require_carries_own_span(self) -> None:
        t = Require(scored=T("a", 0, 1), filter_only=T("b", 5, 6), startchar=0, endchar=6)
        result = normalize(t)
        assert (result.startchar, result.endchar) == (0, 6)

    def test_boosted_carries_own_span(self) -> None:
        t = Boosted(T("a", 2, 5), boost=2.0, startchar=2, endchar=5)
        result = normalize(t)
        assert result == Boosted(T("a", 2, 5), boost=2.0)
        assert (result.startchar, result.endchar) == (2, 5)

    def test_boosted_merge_keeps_outer_span(self) -> None:
        inner = Boosted(T("a", 2, 5), boost=0.5, startchar=2, endchar=5)
        outer = Boosted(inner, boost=4.0, startchar=2, endchar=5)
        result = normalize(outer)
        # merged boost 0.5 * 4.0 == 2.0, still boosted
        assert result == Boosted(T("a", 2, 5), boost=2.0)
        assert (result.startchar, result.endchar) == (2, 5)

    def test_boosted_merge_to_one_keeps_child_own_span(self) -> None:
        inner = Boosted(T("a", 2, 5), boost=0.5, startchar=2, endchar=5)
        outer = Boosted(inner, boost=2.0, startchar=2, endchar=5)
        result = normalize(outer)
        assert result == T("a", 2, 5)
        assert (result.startchar, result.endchar) == (2, 5)


# -- collapse-to-marker cases (Nothing()/Every()) carry the original span --


class TestCollapseSpanCarried:
    def test_not_nothing_to_every_carries_span(self) -> None:
        t = Not(Nothing(), startchar=0, endchar=10)
        result = normalize(t)
        assert result == Every()
        assert (result.startchar, result.endchar) == (0, 10)

    def test_andnot_positive_nothing_carries_span(self) -> None:
        t = AndNot(positive=Nothing(), negative=T("a", 5, 6), startchar=0, endchar=10)
        result = normalize(t)
        assert result == Nothing()
        assert (result.startchar, result.endchar) == (0, 10)

    def test_boosted_child_nothing_carries_span(self) -> None:
        t = Boosted(Nothing(), boost=2.0, startchar=0, endchar=10)
        result = normalize(t)
        assert result == Nothing()
        assert (result.startchar, result.endchar) == (0, 10)


# -- And/Or flatten/merge: union of (min startchar, max endchar) across ----
# -- whatever ended up as children ------------------------------------------


class TestAndOrUnionSpan:
    def test_and_union_across_flattened_children(self) -> None:
        t = And(children=(T("a", 0, 1), And(children=(T("b", 5, 6), T("c", 10, 11)))))
        result = normalize(t)
        assert isinstance(result, And)
        assert (result.startchar, result.endchar) == (0, 11)

    def test_or_union_across_flattened_children(self) -> None:
        t = Or(children=(T("a", 0, 1), Or(children=(T("b", 5, 6), T("c", 10, 11)))))
        result = normalize(t)
        assert isinstance(result, Or)
        assert (result.startchar, result.endchar) == (0, 11)

    def test_and_union_skips_children_with_no_span(self) -> None:
        # A hand-built child with no span at all doesn't poison the union;
        # it's just skipped for min/max purposes.
        t = And(children=(T("a", 0, 1), T("b"), T("c", 10, 11)))
        result = normalize(t)
        assert isinstance(result, And)
        assert (result.startchar, result.endchar) == (0, 11)

    def test_and_union_all_none_is_none(self) -> None:
        # If none of the children carry a span at all, the union is None,
        # not an accidental 0 or a crash.
        t = And(children=(T("a"), T("b"), T("c")))
        result = normalize(t)
        assert isinstance(result, And)
        assert (result.startchar, result.endchar) == (None, None)

    def test_and_dedup_drops_span_of_dropped_duplicate(self) -> None:
        # Two structurally-equal Terms (span excluded from equality) at
        # different source positions: dedup keeps only the first, so the
        # second's (wider) span does not widen the union.
        t = And(children=(T("a", 0, 1), T("a", 50, 51)))
        result = normalize(t)
        assert result == T("a")
        assert (result.startchar, result.endchar) == (0, 1)


# -- end-to-end acceptance: real parse() output, sliced against source text -


def test_and_or_spans_slice_expected_substrings(reg: FieldRegistry) -> None:
    query = "title:foo AND (bar OR title:z)"
    node: Node = wc.parse(query, registry=reg, default_fields=["content", "title"]).ast

    assert isinstance(node, And)
    assert (node.startchar, node.endchar) == (6, 29)
    assert query[node.startchar : node.endchar] == "foo AND (bar OR title:z"

    or_child = next(c for c in node.children if isinstance(c, Or))
    assert (or_child.startchar, or_child.endchar) == (15, 29)
    assert query[or_child.startchar : or_child.endchar] == "bar OR title:z"


def test_boosted_term_span_slices_to_bare_term_text(reg: FieldRegistry) -> None:
    query = "title:abc^2"
    node = wc.parse(query, registry=reg, default_fields=["content", "title"]).ast

    assert isinstance(node, Boosted)
    assert isinstance(node.child, Term)
    # Matches the established convention for a plain (unboosted) Term's own
    # span elsewhere in this codebase (see test_parser_fields.py /
    # test_syntax.py): the field prefix ("title:") is excluded, and here the
    # trailing "^2" boost suffix is excluded too (BoostPlugin strips the
    # boost token out of the syntax tree before query() ever sees it).
    assert query[node.child.startchar : node.child.endchar] == "abc"
    # The Boosted wrapper's own span covers exactly the same source range as
    # its child: boosting a clause consumes no extra characters.
    assert (node.startchar, node.endchar) == (node.child.startchar, node.child.endchar)


@pytest.mark.parametrize(
    ("query", "expected_substring"),
    [
        pytest.param("bar^3", "bar", id="bare-term-boost"),
        pytest.param("title:bar^3", "bar", id="fielded-term-boost"),
        pytest.param("(bar AND baz)^2", "bar AND baz", id="group-boost"),
    ],
)
def test_boosted_span_matches_child_span(
    reg: FieldRegistry, query: str, expected_substring: str
) -> None:
    node = wc.parse(query, registry=reg, default_fields=["content", "title"]).ast
    assert isinstance(node, Boosted)
    assert node.startchar is not None
    assert node.endchar is not None
    assert query[node.startchar : node.endchar] == expected_substring
    assert (node.child.startchar, node.child.endchar) == (node.startchar, node.endchar)
