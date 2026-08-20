import pytest

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


def T(x: str) -> Term:
    return Term(field=None, text=x)


# Rule 1: normalize children first (post-order). A nested Nothing deep in a
# child group must be normalized before the parent applies its own rules.
class TestRule1NormalizeChildrenFirst:
    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            # Or(And(a, Nothing)) -> Or(Nothing) -> Nothing (all dropped)
            pytest.param(
                Or(children=(And(children=(T("a"), Nothing())),)),
                Nothing(),
                id="nested-and-nothing-propagates-up",
            ),
            # Not(Nothing()) normalizes to Every(), then Every() dropped from And
            pytest.param(
                And(children=(Not(Nothing()), T("a"))),
                T("a"),
                id="deeply-nested-not-nothing",
            ),
        ],
    )
    def test_children_normalized_first(self, tree: Node, expected: Node) -> None:
        assert normalize(tree) == expected


# Rule 2: flatten nested same-type groups; Boosted is a barrier except
# Boosted(Boosted(x, a), b) which merges (that's rule 8's territory too).
class TestRule2Flatten:
    def test_flatten_and(self) -> None:
        t = And(children=(And(children=(T("a"), T("b"))), T("c")))
        assert normalize(t) == And(children=(T("a"), T("b"), T("c")))

    def test_flatten_or(self) -> None:
        t = Or(children=(Or(children=(T("a"), T("b"))), T("c")))
        assert normalize(t) == Or(children=(T("a"), T("b"), T("c")))

    def test_boosted_and_does_not_flatten(self) -> None:
        inner = And(children=(T("a"), T("b")))
        t = And(children=(Boosted(child=inner, boost=2.0), T("c")))
        assert normalize(t) == And(children=(Boosted(child=inner, boost=2.0), T("c")))

    def test_boosted_boosted_merges(self) -> None:
        t = Boosted(child=Boosted(child=T("a"), boost=2.0), boost=3.0)
        assert normalize(t) == Boosted(child=T("a"), boost=6.0)


# Rule 3: Nothing propagation across all combinator types.
class TestRule3NothingPropagation:
    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            pytest.param(
                And(children=(T("a"), Nothing())),
                Nothing(),
                id="and-with-nothing-child-becomes-nothing",
            ),
            pytest.param(Or(children=(T("a"), Nothing())), T("a"), id="or-drops-nothing-child"),
            pytest.param(
                Or(children=(Nothing(), Nothing())), Nothing(), id="or-all-nothing-becomes-nothing"
            ),
            pytest.param(Not(Nothing()), Every(), id="not-nothing-becomes-every"),
            pytest.param(
                AndNot(positive=T("a"), negative=Nothing()),
                T("a"),
                id="andnot-negative-nothing-drops-to-positive",
            ),
            pytest.param(
                AndNot(positive=Nothing(), negative=T("b")),
                Nothing(),
                id="andnot-positive-nothing-becomes-nothing",
            ),
            pytest.param(
                AndMaybe(required=Nothing(), optional=T("b")),
                Nothing(),
                id="andmaybe-required-nothing-becomes-nothing",
            ),
            pytest.param(
                AndMaybe(required=T("a"), optional=Nothing()),
                T("a"),
                id="andmaybe-optional-nothing-drops-to-required",
            ),
            pytest.param(
                Require(scored=Nothing(), filter_only=T("b")),
                Nothing(),
                id="require-scored-nothing-becomes-nothing",
            ),
            pytest.param(
                Require(scored=T("a"), filter_only=Nothing()),
                Nothing(),
                id="require-filter-only-nothing-becomes-nothing",
            ),
            pytest.param(
                Boosted(child=Nothing(), boost=2.0), Nothing(), id="boosted-nothing-becomes-nothing"
            ),
        ],
    )
    def test_nothing_propagation(self, tree: Node, expected: Node) -> None:
        assert normalize(tree) == expected


# Rule 4: single-child group unwraps (after flatten/dedupe/absorption).
class TestRule4SingleChildUnwrap:
    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            pytest.param(And(children=(T("a"),)), T("a"), id="and-single-child-unwraps"),
            pytest.param(Or(children=(T("a"),)), T("a"), id="or-single-child-unwraps"),
        ],
    )
    def test_single_child_unwrap(self, tree: Node, expected: Node) -> None:
        assert normalize(tree) == expected


# Rule 5: duplicate sibling dedupe, preserving first-seen order.
class TestRule5Dedupe:
    def test_dedupe(self) -> None:
        assert normalize(Or(children=(T("a"), T("a")))) == T("a")

    def test_dedupe_preserves_first_seen_order(self) -> None:
        t = And(children=(T("b"), T("a"), T("b")))
        assert normalize(t) == And(children=(T("b"), T("a")))

    def test_dedupe_keeps_analyzed_phrases_with_distinct_words(self) -> None:
        # Phrase.words is excluded from equality/hashing (analysis
        # provenance, like spans), so two analyzed phrases whose DIFFERENT
        # token tuples space-join to the same text compare equal. Possible
        # only with an analyzer whose tokens contain spaces (shingle-style,
        # explicitly supported); the emitter builds the positional
        # phrase_query from words, so deduping one away silently drops its
        # distinct match set. Real whoosh compares the word lists and
        # keeps both (measured); dedupe must key on words, not just
        # node equality.
        from whoosh_compat.ast import Phrase
        from whoosh_compat.fields import FieldRef

        p1 = Phrase(field=FieldRef("t"), text="a b c", words=("a b", "c"), analyzed=True)
        p2 = Phrase(field=FieldRef("t"), text="a b c", words=("a", "b c"), analyzed=True)
        assert p1 == p2  # the equality contract itself is unchanged
        result = normalize(Or(children=(p1, p2)))
        assert isinstance(result, Or)
        assert len(result.children) == 2

    def test_dedupe_keeps_mixed_analyzed_flag_terms(self) -> None:
        # The analyzed flag's sibling cell: an analyzed Term and an
        # unanalyzed one with the same multi-word text compare equal, but
        # the unanalyzed one would still be tokenized (split into an
        # And/Or of tokens) by a later analyze() pass, so merging the
        # pair silently picks one of two different downstream meanings.
        # Only reachable from a hand-built mixed-flag tree (the pipeline
        # never mixes flags), which the AST contract permits.
        from whoosh_compat.ast import Term as TermNode
        from whoosh_compat.fields import FieldRef

        t1 = TermNode(field=FieldRef("t"), text="foo bar", analyzed=True)
        t2 = TermNode(field=FieldRef("t"), text="foo bar", analyzed=False)
        assert t1 == t2
        result = normalize(Or(children=(t1, t2)))
        assert isinstance(result, Or)
        assert len(result.children) == 2

    def test_dedupe_still_merges_genuinely_identical_analyzed_phrases(self) -> None:
        from whoosh_compat.ast import Phrase
        from whoosh_compat.fields import FieldRef

        p1 = Phrase(field=FieldRef("t"), text="a b", words=("a", "b"), analyzed=True)
        p2 = Phrase(field=FieldRef("t"), text="a b", words=("a", "b"), analyzed=True)
        assert normalize(Or(children=(p1, p2))) == p1


# Rule 6: Every() absorption in Or, dropping in And.
class TestRule6EveryAbsorption:
    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            pytest.param(Or(children=(Every(), T("a"))), Every(), id="every-absorbs-or"),
            pytest.param(
                And(children=(Every(), T("a"))), T("a"), id="every-dropped-from-and-leaves-one"
            ),
            pytest.param(
                And(children=(Every(), Every())), Every(), id="every-dropped-from-and-leaves-zero"
            ),
        ],
    )
    def test_every_absorption(self, tree: Node, expected: Node) -> None:
        assert normalize(tree) == expected


# Rule 7: empty groups collapse to Nothing().
class TestRule7EmptyGroupToNothing:
    @pytest.mark.parametrize(
        "tree",
        [
            pytest.param(And(children=()), id="empty-and"),
            pytest.param(Or(children=()), id="empty-or"),
        ],
    )
    def test_empty_group_collapses(self, tree: Node) -> None:
        assert normalize(tree) == Nothing()


# Rule 8: Boosted(x, 1.0) strips to x.
class TestRule8BoostOneStrips:
    def test_boost_of_one_strips(self) -> None:
        assert normalize(Boosted(child=T("a"), boost=1.0)) == T("a")

    def test_boost_merge_results_in_one_strips(self) -> None:
        # Boosted(Boosted(x, 0.5), 2.0) -> merged boost 1.0 -> strips entirely
        t = Boosted(child=Boosted(child=T("a"), boost=0.5), boost=2.0)
        assert normalize(t) == T("a")


# normalize() must be iterative, not recursive: a hand-built tree can be far
# deeper than anything the parser's own nesting cap would ever allow through
# (that cap only bounds *parenthesization* depth reached via parse(), see
# tests/test_parser_basics.py), so this exercises the traversal mechanism
# itself, independent of the parse-time cap.
class TestIterativeNormalizeDeepTree:
    def test_deep_not_chain_does_not_raise_recursion_error(self) -> None:
        depth = 5000
        tree: Node = T("a")
        for _ in range(depth):
            tree = Not(child=tree)
        result = normalize(tree)
        # depth is even, so the Not chain cancels down to a bare Every()...
        # actually: Not(Nothing()) -> Every() is the only self-annihilating
        # rule; a chain of plain Not(Term) wrappers doesn't collapse at all,
        # it just nests. Confirm the traversal completes and the result is
        # still a Not chain of the same depth, unmangled.
        count = 0
        node = result
        while isinstance(node, Not):
            count += 1
            node = node.child
        assert count == depth
        assert node == T("a")

    def test_deep_and_chain_does_not_raise_recursion_error(self) -> None:
        depth = 5000
        tree: Node = T("z")
        for i in range(depth):
            tree = And(children=(T(str(i)), tree))
        result = normalize(tree)
        # A right-nested chain of And(x, And(y, And(...))) flattens fully
        # under rule 2, so this also exercises the flatten/dedupe logic at
        # depth, not just raw traversal.
        assert isinstance(result, And)
        assert len(result.children) == depth + 1

    def test_deep_chain_as_sibling_does_not_raise_recursion_error(self) -> None:
        # The previous two tests are wide (many shallow siblings); this one
        # is the orthogonal shape: a single 2000-deep Not chain sitting
        # *beside* another sibling in an And. normalize()'s own traversal
        # is iterative and handles this fine, but _dedupe puts each sibling
        # into a set, and a frozen dataclass's generated __hash__ recurses
        # through the whole subtree in native Python frames to compute it -
        # so hashing the deep sibling alone can blow the recursion limit,
        # independent of normalize()'s own traversal being iterative.
        depth = 2000
        deep: Node = T("a")
        for _ in range(depth):
            deep = Not(child=deep)
        tree = And(children=(deep, T("b")))
        result = normalize(tree)
        assert isinstance(result, And)
        assert len(result.children) == 2
        assert result.children[1] == T("b")
        count = 0
        node = result.children[0]
        while isinstance(node, Not):
            count += 1
            node = node.child
        assert count == depth
        assert node == T("a")
