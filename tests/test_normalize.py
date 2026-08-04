from whoosh_compat.ast import (
    Term, And, Or, Not, AndNot, AndMaybe, Require, Boosted, Every, Nothing,
    normalize,
)


def T(x):
    return Term(field=None, text=x)


# Rule 1: normalize children first (post-order) — a nested Nothing deep in a
# child group must be normalized before the parent applies its own rules.
class TestRule1NormalizeChildrenFirst:
    def test_nested_and_nothing_propagates_up(self):
        # Or(And(a, Nothing)) -> Or(Nothing) -> Nothing (all dropped)
        t = Or(children=(And(children=(T("a"), Nothing())),))
        assert normalize(t) == Nothing()

    def test_deeply_nested_not_nothing(self):
        t = And(children=(Not(Nothing()), T("a")))
        # Not(Nothing()) normalizes to Every(), then Every() dropped from And
        assert normalize(t) == T("a")


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
    def test_nothing_in_and(self):
        assert normalize(And(children=(T("a"), Nothing()))) == Nothing()

    def test_nothing_dropped_from_or(self):
        assert normalize(Or(children=(T("a"), Nothing()))) == T("a")

    def test_or_all_nothing(self):
        assert normalize(Or(children=(Nothing(), Nothing()))) == Nothing()

    def test_not_nothing(self):
        assert normalize(Not(Nothing())) == Every()

    def test_andnot_negative_nothing(self):
        assert normalize(AndNot(positive=T("a"), negative=Nothing())) == T("a")

    def test_andnot_positive_nothing(self):
        assert normalize(AndNot(positive=Nothing(), negative=T("b"))) == Nothing()

    def test_andmaybe_required_nothing(self):
        assert normalize(AndMaybe(required=Nothing(), optional=T("b"))) == Nothing()

    def test_andmaybe_optional_nothing(self):
        assert normalize(AndMaybe(required=T("a"), optional=Nothing())) == T("a")

    def test_require_scored_nothing(self):
        assert normalize(Require(scored=Nothing(), filter_only=T("b"))) == Nothing()

    def test_require_filter_only_nothing(self):
        assert normalize(Require(scored=T("a"), filter_only=Nothing())) == Nothing()

    def test_boosted_nothing(self):
        assert normalize(Boosted(child=Nothing(), boost=2.0)) == Nothing()


# Rule 4: single-child group unwraps (after flatten/dedupe/absorption).
class TestRule4SingleChildUnwrap:
    def test_and_single_child_unwraps(self):
        assert normalize(And(children=(T("a"),))) == T("a")

    def test_or_single_child_unwraps(self):
        assert normalize(Or(children=(T("a"),))) == T("a")


# Rule 5: duplicate sibling dedupe, preserving first-seen order.
class TestRule5Dedupe:
    def test_dedupe(self):
        assert normalize(Or(children=(T("a"), T("a")))) == T("a")

    def test_dedupe_preserves_first_seen_order(self):
        t = And(children=(T("b"), T("a"), T("b")))
        assert normalize(t) == And(children=(T("b"), T("a")))


# Rule 6: Every() absorption in Or, dropping in And.
class TestRule6EveryAbsorption:
    def test_every_absorbs_or(self):
        assert normalize(Or(children=(Every(), T("a")))) == Every()

    def test_every_dropped_from_and_leaves_one(self):
        assert normalize(And(children=(Every(), T("a")))) == T("a")

    def test_every_dropped_from_and_leaves_zero(self):
        assert normalize(And(children=(Every(), Every()))) == Every()


# Rule 7: empty groups collapse to Nothing().
class TestRule7EmptyGroupToNothing:
    def test_empty_and(self):
        assert normalize(And(children=())) == Nothing()

    def test_empty_or(self):
        assert normalize(Or(children=())) == Nothing()


# Rule 8: Boosted(x, 1.0) strips to x.
class TestRule8BoostOneStrips:
    def test_boost_of_one_strips(self):
        assert normalize(Boosted(child=T("a"), boost=1.0)) == T("a")

    def test_boost_merge_results_in_one_strips(self):
        # Boosted(Boosted(x, 0.5), 2.0) -> merged boost 1.0 -> strips entirely
        t = Boosted(child=Boosted(child=T("a"), boost=0.5), boost=2.0)
        assert normalize(t) == T("a")
