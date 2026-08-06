import pytest

from whoosh_compat.ast import And
from whoosh_compat.ast import AndMaybe
from whoosh_compat.ast import AndNot
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import Every
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import Or
from whoosh_compat.ast import Require
from whoosh_compat.ast import Term
from whoosh_compat.ast import normalize


def T(x):
    return Term(field=None, text=x)


# Rule 1: normalize children first (post-order). A nested Nothing deep in a
# child group must be normalized before the parent applies its own rules.
class TestRule1NormalizeChildrenFirst:
    @pytest.mark.parametrize("tree, expected", [
        # Or(And(a, Nothing)) -> Or(Nothing) -> Nothing (all dropped)
        pytest.param(
            Or(children=(And(children=(T("a"), Nothing())),)), Nothing(),
            id="nested-and-nothing-propagates-up",
        ),
        # Not(Nothing()) normalizes to Every(), then Every() dropped from And
        pytest.param(
            And(children=(Not(Nothing()), T("a"))), T("a"),
            id="deeply-nested-not-nothing",
        ),
    ])
    def test_children_normalized_first(self, tree, expected):
        assert normalize(tree) == expected


# Rule 2: flatten nested same-type groups; Boosted is a barrier except
# Boosted(Boosted(x, a), b) which merges (that's rule 8's territory too).
class TestRule2Flatten:
    def test_flatten_and(self):
        t = And(children=(And(children=(T("a"), T("b"))), T("c")))
        assert normalize(t) == And(children=(T("a"), T("b"), T("c")))

    def test_flatten_or(self):
        t = Or(children=(Or(children=(T("a"), T("b"))), T("c")))
        assert normalize(t) == Or(children=(T("a"), T("b"), T("c")))

    def test_boosted_and_does_not_flatten(self):
        inner = And(children=(T("a"), T("b")))
        t = And(children=(Boosted(child=inner, boost=2.0), T("c")))
        assert normalize(t) == And(
            children=(Boosted(child=inner, boost=2.0), T("c"))
        )

    def test_boosted_boosted_merges(self):
        t = Boosted(child=Boosted(child=T("a"), boost=2.0), boost=3.0)
        assert normalize(t) == Boosted(child=T("a"), boost=6.0)


# Rule 3: Nothing propagation across all combinator types.
class TestRule3NothingPropagation:
    @pytest.mark.parametrize("tree, expected", [
        pytest.param(And(children=(T("a"), Nothing())), Nothing(),
                     id="and-with-nothing-child-becomes-nothing"),
        pytest.param(Or(children=(T("a"), Nothing())), T("a"),
                     id="or-drops-nothing-child"),
        pytest.param(Or(children=(Nothing(), Nothing())), Nothing(),
                     id="or-all-nothing-becomes-nothing"),
        pytest.param(Not(Nothing()), Every(), id="not-nothing-becomes-every"),
        pytest.param(AndNot(positive=T("a"), negative=Nothing()), T("a"),
                     id="andnot-negative-nothing-drops-to-positive"),
        pytest.param(AndNot(positive=Nothing(), negative=T("b")), Nothing(),
                     id="andnot-positive-nothing-becomes-nothing"),
        pytest.param(AndMaybe(required=Nothing(), optional=T("b")), Nothing(),
                     id="andmaybe-required-nothing-becomes-nothing"),
        pytest.param(AndMaybe(required=T("a"), optional=Nothing()), T("a"),
                     id="andmaybe-optional-nothing-drops-to-required"),
        pytest.param(Require(scored=Nothing(), filter_only=T("b")), Nothing(),
                     id="require-scored-nothing-becomes-nothing"),
        pytest.param(Require(scored=T("a"), filter_only=Nothing()), Nothing(),
                     id="require-filter-only-nothing-becomes-nothing"),
        pytest.param(Boosted(child=Nothing(), boost=2.0), Nothing(),
                     id="boosted-nothing-becomes-nothing"),
    ])
    def test_nothing_propagation(self, tree, expected):
        assert normalize(tree) == expected


# Rule 4: single-child group unwraps (after flatten/dedupe/absorption).
class TestRule4SingleChildUnwrap:
    @pytest.mark.parametrize("tree, expected", [
        pytest.param(And(children=(T("a"),)), T("a"), id="and-single-child-unwraps"),
        pytest.param(Or(children=(T("a"),)), T("a"), id="or-single-child-unwraps"),
    ])
    def test_single_child_unwrap(self, tree, expected):
        assert normalize(tree) == expected


# Rule 5: duplicate sibling dedupe, preserving first-seen order.
class TestRule5Dedupe:
    def test_dedupe(self):
        assert normalize(Or(children=(T("a"), T("a")))) == T("a")

    def test_dedupe_preserves_first_seen_order(self):
        t = And(children=(T("b"), T("a"), T("b")))
        assert normalize(t) == And(children=(T("b"), T("a")))


# Rule 6: Every() absorption in Or, dropping in And.
class TestRule6EveryAbsorption:
    @pytest.mark.parametrize("tree, expected", [
        pytest.param(Or(children=(Every(), T("a"))), Every(),
                     id="every-absorbs-or"),
        pytest.param(And(children=(Every(), T("a"))), T("a"),
                     id="every-dropped-from-and-leaves-one"),
        pytest.param(And(children=(Every(), Every())), Every(),
                     id="every-dropped-from-and-leaves-zero"),
    ])
    def test_every_absorption(self, tree, expected):
        assert normalize(tree) == expected


# Rule 7: empty groups collapse to Nothing().
class TestRule7EmptyGroupToNothing:
    @pytest.mark.parametrize("tree", [
        pytest.param(And(children=()), id="empty-and"),
        pytest.param(Or(children=()), id="empty-or"),
    ])
    def test_empty_group_collapses(self, tree):
        assert normalize(tree) == Nothing()


# Rule 8: Boosted(x, 1.0) strips to x.
class TestRule8BoostOneStrips:
    def test_boost_of_one_strips(self):
        assert normalize(Boosted(child=T("a"), boost=1.0)) == T("a")

    def test_boost_merge_results_in_one_strips(self):
        # Boosted(Boosted(x, 0.5), 2.0) -> merged boost 1.0 -> strips entirely
        t = Boosted(child=Boosted(child=T("a"), boost=0.5), boost=2.0)
        assert normalize(t) == T("a")
