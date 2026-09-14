"""Unit tests for ``analyze()``'s ``rewrite_leaf`` hook.

The hook lets a host replace a ``Term``/``Phrase`` leaf, typically with
``Or(leaf, companion)``, from inside analysis itself. These tests pin what
the hook sees, what its return value means, and that a leaf keeps the
multi-token context and zero-token drop behavior it would have had with no
hook at all.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import cast

import pytest
from _pytest.mark import ParameterSet

import whoosh_compat
from whoosh_compat.ast import And
from whoosh_compat.ast import AndMaybe
from whoosh_compat.ast import AndNot
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import DateRange
from whoosh_compat.ast import ErrorLeaf
from whoosh_compat.ast import Every
from whoosh_compat.ast import Fuzzy
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import NumericRange
from whoosh_compat.ast import Or
from whoosh_compat.ast import Phrase
from whoosh_compat.ast import Prefix
from whoosh_compat.ast import Require
from whoosh_compat.ast import Term
from whoosh_compat.ast import TermRange
from whoosh_compat.ast import Wildcard
from whoosh_compat.ast import _child_nodes
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken

CONTENT = FieldRef("content")
SIDE = FieldRef("side")
SIDE_AND = FieldRef("side_and")


def words(text: str) -> list[str]:
    """Split on spaces and hyphens, dropping the stopword ``the``."""
    return [t for t in text.replace("-", " ").split() if t != "the"]


def refuse_bad(text: str) -> list[str]:
    if text == "bad":
        raise ValueError("analyzer refused")
    return text.split()


REG = FieldRegistry(
    [
        FieldSpec("content", FieldKind.TEXT, analyzer=words),
        FieldSpec("tag", FieldKind.KEYWORD, analyzer=words),
        FieldSpec("side", FieldKind.TEXT, analyzer=words),
        FieldSpec("side_and", FieldKind.TEXT, analyzer=words, multitoken=Multitoken.AND),
        FieldSpec("strict", FieldKind.TEXT, analyzer=refuse_bad),
        FieldSpec("asn", FieldKind.U64, fast=True),
        FieldSpec("tag_id", FieldKind.U64, fast=True),
        FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
        FieldSpec("created", FieldKind.DATE, date_only=True),
        FieldSpec("added", FieldKind.DATETIME),
        FieldSpec("attrs", FieldKind.JSON, subpaths=("user",), analyzer=words),
    ]
)


def t(text: str, field: FieldRef | None = CONTENT) -> Term:
    return Term(field=field, text=text)


Q = t("q", SIDE)
A_AND_B = And(children=(t("a"), t("b")))


def wrap_only(target: Node, companion: Node = Q) -> Callable[[Term | Phrase], Node]:
    """A hook that wraps ``target`` (by identity) as ``Or(target, companion)``."""

    def hook(leaf: Term | Phrase) -> Node:
        return Or(children=(leaf, companion)) if leaf is target else leaf

    return hook


def remove_only(target: Node) -> Callable[[Term | Phrase], Node]:
    def hook(leaf: Term | Phrase) -> Node:
        return Nothing() if leaf is target else leaf

    return hook


class Recorder:
    """A hook that records each leaf it is called with, then applies ``inner``."""

    def __init__(self, inner: Callable[[Term | Phrase], Node] = lambda leaf: leaf) -> None:
        self.calls: list[Term | Phrase] = []
        self.inner = inner

    def __call__(self, leaf: Term | Phrase) -> Node:
        self.calls.append(leaf)
        return self.inner(leaf)


# -- the no-op paths -------------------------------------------------------


@pytest.mark.parametrize(
    "tree",
    [
        pytest.param(t("a-b"), id="top-level-multi-token"),
        pytest.param(And(children=(t("a-b"), t("x"))), id="and"),
        pytest.param(Or(children=(t("a-b"), t("x"))), id="or"),
        pytest.param(And(children=(t("the"), t("x"))), id="and-zero-token"),
        pytest.param(AndNot(positive=t("the"), negative=t("x")), id="andnot-zero-token"),
        pytest.param(And(children=(Every(), t("x", None))), id="and-every-unfielded-sibling"),
    ],
)
def test_no_hook_and_identity_hook_match_plain_analysis(tree: Node) -> None:
    plain = analyze(tree, REG)
    assert analyze(tree, REG, rewrite_leaf=None) == plain
    assert analyze(tree, REG, rewrite_leaf=lambda leaf: leaf) == plain


# -- which leaves reach the hook -------------------------------------------


@pytest.mark.parametrize(
    ("leaf", "pinned"),
    [
        pytest.param(t("a-b"), A_AND_B, id="text-term"),
        pytest.param(
            t("a-b", FieldRef("tag")),
            And(children=(t("a", FieldRef("tag")), t("b", FieldRef("tag")))),
            id="keyword-term",
        ),
        pytest.param(
            t("a-b", FieldRef("attrs", "user")),
            And(children=(t("a", FieldRef("attrs", "user")), t("b", FieldRef("attrs", "user")))),
            id="json-subpath-term",
        ),
        pytest.param(t("101", FieldRef("asn")), None, id="u64-term"),
        pytest.param(t("true", FieldRef("has_tag")), None, id="boolean-exists-term"),
        pytest.param(t("2020-03-15", FieldRef("created")), None, id="date-term"),
        pytest.param(t("2020-03-15", FieldRef("added")), None, id="datetime-term"),
        pytest.param(t("a-b", None), None, id="unfielded-term"),
        pytest.param(t("a-b", FieldRef("nope")), None, id="unknown-field-term"),
        pytest.param(
            Phrase(field=CONTENT, text="a-b"), Phrase(field=CONTENT, text="a b"), id="text-phrase"
        ),
        pytest.param(
            Phrase(field=FieldRef("tag"), text="a-b"),
            Phrase(field=FieldRef("tag"), text="a b"),
            id="keyword-phrase",
        ),
        pytest.param(
            Phrase(field=FieldRef("attrs", "user"), text="a-b"),
            Phrase(field=FieldRef("attrs", "user"), text="a b"),
            id="json-subpath-phrase",
        ),
        pytest.param(Phrase(field=FieldRef("asn"), text="101"), None, id="u64-phrase"),
        pytest.param(
            Phrase(field=FieldRef("has_tag"), text="true"), None, id="boolean-exists-phrase"
        ),
        pytest.param(Phrase(field=FieldRef("created"), text="2020-03-15"), None, id="date-phrase"),
        pytest.param(
            Phrase(field=FieldRef("added"), text="2020-03-15"), None, id="datetime-phrase"
        ),
        pytest.param(Phrase(field=None, text="a-b"), None, id="unfielded-phrase"),
        pytest.param(Phrase(field=FieldRef("nope"), text="a-b"), None, id="unknown-field-phrase"),
    ],
)
def test_every_term_and_phrase_reaches_the_hook(leaf: Term | Phrase, pinned: Node | None) -> None:
    # pinned=None: analysis leaves this kind untouched, so the pinned result is
    # the leaf itself.
    hook = Recorder(wrap_only(leaf))
    result = analyze(leaf, REG, rewrite_leaf=hook)
    assert hook.calls == [leaf]
    assert hook.calls[0] is leaf
    assert result == Or(children=(leaf if pinned is None else pinned, Q))


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(Prefix(field=CONTENT, text="inv"), id="prefix"),
        pytest.param(Wildcard(field=CONTENT, pattern="in?oice"), id="wildcard"),
        pytest.param(Fuzzy(field=CONTENT, text="invoice"), id="fuzzy"),
        pytest.param(
            TermRange(field=CONTENT, lo="a", hi="b", incl_lo=True, incl_hi=True), id="term-range"
        ),
        pytest.param(
            NumericRange(field=FieldRef("asn"), lo=1, hi=2, incl_lo=True, incl_hi=True),
            id="numeric-range",
        ),
        pytest.param(
            DateRange(
                field=FieldRef("created"),
                lo=datetime(2020, 1, 1, tzinfo=UTC),
                hi=datetime(2021, 1, 1, tzinfo=UTC),
                incl_lo=True,
                incl_hi=False,
            ),
            id="date-range",
        ),
        pytest.param(Every(), id="every"),
        pytest.param(Every(field=CONTENT), id="fielded-every"),
        pytest.param(Nothing(), id="nothing"),
        pytest.param(
            ErrorLeaf(
                diagnostic=Diagnostic(
                    kind=DiagnosticKind.BAD_DATE, cause=Cause.INVALID_INPUT, message="bad"
                )
            ),
            id="error-leaf",
        ),
    ],
)
def test_other_leaves_never_reach_the_hook(node: Node) -> None:
    hook = Recorder(lambda leaf: Nothing())
    assert analyze(node, REG, rewrite_leaf=hook) == analyze(node, REG)
    assert hook.calls == []


def test_nodes_inside_a_replacement_are_not_hooked_again() -> None:
    leaf = t("a-b")
    hook = Recorder(lambda seen: Or(children=(seen, Q)))
    analyze(leaf, REG, rewrite_leaf=hook)
    assert hook.calls == [leaf]


def test_a_leaf_aliased_under_two_contexts_is_hooked_once_per_context() -> None:
    leaf = t("a-b")
    hook = Recorder()
    analyze(And(children=(Or(children=(leaf, t("y"))), leaf)), REG, rewrite_leaf=hook)
    assert sum(call is leaf for call in hook.calls) == 2


def test_a_leaf_aliased_under_one_context_is_hooked_once() -> None:
    leaf = t("a-b")
    hook = Recorder()
    tree = And(children=(Not(child=leaf), Boosted(child=leaf, boost=2.0)))
    analyze(tree, REG, rewrite_leaf=hook)
    assert hook.calls == [leaf]


def _collect_leaves(node: Node) -> list[Term | Phrase]:
    """Every ``Term``/``Phrase`` in ``node``'s subtree, walked via ``_child_nodes``."""
    found: list[Term | Phrase] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (Term, Phrase)):
            found.append(current)
        stack.extend(_child_nodes(current))
    return found


@pytest.mark.parametrize(
    "q",
    [
        pytest.param("alpha NOT beta", id="not"),
        pytest.param("alpha ANDNOT beta", id="andnot"),
        pytest.param("(alpha OR gamma) AND NOT (beta OR delta)", id="not-of-group"),
    ],
)
def test_hook_receives_the_parsed_tree_s_own_leaf_objects(q: str) -> None:
    # A leaf under a negation still reaches the hook as the input tree's own
    # object, identity-comparable against a pre-scan the host runs on the
    # tree it passes in, including a leaf under NOT/AndNot's negative side.
    result = whoosh_compat.parse(q, registry=REG, default_fields=["content"])
    leaves = _collect_leaves(result.ast)
    hook = Recorder()
    analyze(result.ast, REG, rewrite_leaf=hook)
    assert {id(call) for call in hook.calls} == {id(leaf) for leaf in leaves}


def test_hook_receives_input_leaf_objects_from_a_tree_normalization_reshapes() -> None:
    # Normalization flattens the nested And, unwraps the one-child Or and the
    # 1.0 boost, and dedupes the equal copy, but never builds a new Term or
    # Phrase: every call still receives one of the input's own leaf objects,
    # and the deduplicated copy simply gets no call.
    a, x, phrase, copy = t("a-b"), t("x"), Phrase(field=CONTENT, text="y z"), t("a-b")
    tree = And(
        children=(
            And(children=(a, x)),
            Boosted(child=Or(children=(phrase,)), boost=1.0),
            copy,
        )
    )
    assert normalize(tree) != tree
    hook = Recorder()
    analyze(tree, REG, rewrite_leaf=hook)
    assert len(hook.calls) == 3
    assert {id(call) for call in hook.calls} == {id(a), id(x), id(phrase)}


# -- the pinned leaf keeps its context --------------------------------------

# Under AND the pinned "a-b" analyzes to And(a, b); under OR it analyzes to
# Or(a, b), which then flattens into the replacement's own Or.
AND_SHAPED = Or(children=(A_AND_B, Q))
OR_SHAPED = Or(children=(t("a"), t("b"), Q))
Z = t("z")


def _positions(sibling: Node) -> list[ParameterSet]:
    """The single-node wrappers around a leaf that the context and drop
    tests share, with ``sibling`` as the other operand of each binary one.
    """
    return [
        pytest.param(lambda n: Not(child=n), id="not"),
        pytest.param(lambda n: AndNot(positive=n, negative=sibling), id="andnot-positive"),
        pytest.param(lambda n: AndNot(positive=sibling, negative=n), id="andnot-negative"),
        pytest.param(lambda n: AndMaybe(required=n, optional=sibling), id="andmaybe-required"),
        pytest.param(lambda n: AndMaybe(required=sibling, optional=n), id="andmaybe-optional"),
        pytest.param(lambda n: Require(scored=n, filter_only=sibling), id="require-scored"),
        pytest.param(lambda n: Require(scored=sibling, filter_only=n), id="require-filter-only"),
        pytest.param(lambda n: Boosted(child=n, boost=2.0), id="boosted"),
    ]


@pytest.mark.parametrize(
    "wrapper",
    [
        pytest.param(lambda n: n, id="direct"),
        *_positions(Z),
        pytest.param(lambda n: Not(child=Boosted(child=n, boost=2.0)), id="not-of-boosted"),
    ],
)
@pytest.mark.parametrize(
    ("group", "shape"),
    [
        pytest.param(And, AND_SHAPED, id="in-and"),
        pytest.param(Or, OR_SHAPED, id="in-or"),
    ],
)
def test_pinned_leaf_resolves_in_its_enclosing_group(
    wrapper: Callable[[Node], Node], group: type[And | Or], shape: Node
) -> None:
    leaf = t("a-b")
    tree = group(children=(t("y"), wrapper(leaf)))
    result = analyze(tree, REG, rewrite_leaf=wrap_only(leaf))
    assert result == normalize(group(children=(t("y"), wrapper(shape))))


@pytest.mark.parametrize(
    ("mode", "shape"),
    [
        pytest.param(Multitoken.AND, AND_SHAPED, id="default-and"),
        pytest.param(Multitoken.OR, OR_SHAPED, id="default-or"),
    ],
)
def test_top_level_pinned_leaf_resolves_against_default_mode(mode: Multitoken, shape: Node) -> None:
    leaf = t("a-b")
    assert analyze(leaf, REG, default_mode=mode, rewrite_leaf=wrap_only(leaf)) == shape


@pytest.mark.parametrize(
    ("tree_of", "expected"),
    [
        pytest.param(
            lambda n: And(children=(Not(child=Or(children=(n, t("w")))), t("y"))),
            And(children=(Not(child=Or(children=(t("a"), t("b"), Q, t("w")))), t("y"))),
            id="not-of-or-inside-and",
        ),
        pytest.param(
            lambda n: Or(children=(And(children=(Boosted(child=n, boost=2.0), t("w"))), t("y"))),
            Or(children=(And(children=(Boosted(child=AND_SHAPED, boost=2.0), t("w"))), t("y"))),
            id="boosted-inside-and-inside-or",
        ),
    ],
)
def test_pinned_leaf_context_through_nesting(
    tree_of: Callable[[Node], Node], expected: Node
) -> None:
    leaf = t("a-b")
    assert analyze(tree_of(leaf), REG, rewrite_leaf=wrap_only(leaf)) == expected


def test_an_equal_copy_is_not_pinned() -> None:
    leaf = t("a-b")
    copy = t("a-b")

    def hook(seen: Term | Phrase) -> Node:
        return Or(children=(copy, Q)) if seen is leaf else seen

    result = analyze(And(children=(leaf, t("x"))), REG, rewrite_leaf=hook)
    # The copy resolves under the replacement's own Or, so it loosens.
    assert result == And(children=(OR_SHAPED, t("x")))


@pytest.mark.parametrize(
    ("copy_first", "expected"),
    [
        pytest.param(True, And(children=(OR_SHAPED, t("x"))), id="copy-first-replaces-leaf"),
        pytest.param(False, And(children=(AND_SHAPED, t("x"))), id="leaf-first-stays-pinned"),
    ],
)
def test_normalization_dedupes_an_equal_copy_before_pins_resolve(
    copy_first: bool, expected: Node
) -> None:
    leaf = t("a-b")
    copy = t("a-b")
    pair = (copy, leaf) if copy_first else (leaf, copy)

    def hook(seen: Term | Phrase) -> Node:
        return Or(children=(*pair, Q)) if seen is leaf else seen

    assert analyze(And(children=(leaf, t("x"))), REG, rewrite_leaf=hook) == expected


# -- companions ---------------------------------------------------------------


def test_companion_is_analyzed_per_its_own_multitoken() -> None:
    leaf = t("a-b")
    hook = wrap_only(leaf, t("c-d", SIDE_AND))
    result = analyze(Or(children=(leaf, t("y"))), REG, rewrite_leaf=hook)
    assert result == Or(
        children=(t("a"), t("b"), And(children=(t("c", SIDE_AND), t("d", SIDE_AND))), t("y"))
    )


def test_default_companion_follows_the_replacement_group() -> None:
    leaf = t("a-b")
    result = analyze(
        And(children=(leaf, t("y"))), REG, rewrite_leaf=wrap_only(leaf, t("c-d", SIDE))
    )
    assert result == And(children=(Or(children=(A_AND_B, t("c", SIDE), t("d", SIDE))), t("y")))


def test_a_bare_companion_takes_the_leaf_context() -> None:
    leaf = t("a-b")

    def hook(seen: Term | Phrase) -> Node:
        return t("c-d", SIDE) if seen is leaf else seen

    result = analyze(And(children=(leaf, t("y"))), REG, rewrite_leaf=hook)
    assert result == And(children=(t("c", SIDE), t("d", SIDE), t("y")))


def test_default_companion_keeps_or_after_the_pinned_leaf_drops() -> None:
    leaf = t("the")
    result = analyze(
        And(children=(leaf, t("y"))), REG, rewrite_leaf=wrap_only(leaf, t("c-d", SIDE))
    )
    assert result == And(children=(Or(children=(t("c", SIDE), t("d", SIDE))), t("y")))


# -- drop semantics -------------------------------------------------------------

Y = t("y")

POSITIONS = [
    pytest.param(lambda n: n, id="alone"),
    pytest.param(lambda n: And(children=(n, Y)), id="and"),
    pytest.param(lambda n: Or(children=(n, Y)), id="or"),
    *_positions(Y),
    pytest.param(lambda n: And(children=(Boosted(child=n, boost=2.0), Y)), id="and-of-boosted"),
]

LEAF_KINDS = [
    pytest.param(lambda text: Term(field=CONTENT, text=text), id="term"),
    pytest.param(lambda text: Phrase(field=CONTENT, text=text), id="phrase"),
]


@pytest.mark.parametrize("make", LEAF_KINDS)
@pytest.mark.parametrize("position", POSITIONS)
def test_a_removed_leaf_drops_like_a_zero_token_leaf(
    position: Callable[[Node], Node], make: Callable[[str], Term | Phrase]
) -> None:
    leaf = make("x")
    removed = analyze(position(leaf), REG, rewrite_leaf=remove_only(leaf))
    assert removed == analyze(position(make("the")), REG)


@pytest.mark.parametrize("make", LEAF_KINDS)
@pytest.mark.parametrize("position", POSITIONS)
def test_a_zero_token_pinned_leaf_leaves_its_companion(
    position: Callable[[Node], Node], make: Callable[[str], Term | Phrase]
) -> None:
    leaf = make("the")
    widened = analyze(position(leaf), REG, rewrite_leaf=wrap_only(leaf))
    assert widened == analyze(position(Q), REG)


def test_a_literal_nothing_inside_a_replacement_empties_it_and_it_drops() -> None:
    leaf = t("x")

    def hook(seen: Term | Phrase) -> Node:
        return And(children=(seen, Nothing())) if seen is leaf else seen

    assert analyze(And(children=(leaf, Y)), REG, rewrite_leaf=hook) == Y


@pytest.mark.parametrize(
    ("leaf", "expected"),
    [
        pytest.param(t("x"), Every(), id="raw"),
        pytest.param(
            Term(field=CONTENT, text="x", analyzed=True), Nothing(), id="already-analyzed"
        ),
        pytest.param(t("x", None), Nothing(), id="unfielded"),
    ],
)
def test_removing_a_leaf_beside_an_unfielded_every(leaf: Term, expected: Node) -> None:
    # An unfielded Every inside an And is kept through normalization only
    # while a sibling can still analyze to zero tokens, which a raw, fielded
    # leaf can and an analyzed or unfielded one cannot. A removed leaf follows
    # the same rule, so the outcome does not depend on whether the caller
    # normalized the tree first.
    tree = And(children=(Every(), leaf))
    hook = remove_only(leaf)
    assert analyze(tree, REG, rewrite_leaf=hook) == expected
    assert analyze(normalize(tree), REG, rewrite_leaf=hook) == expected


# -- errors -------------------------------------------------------------------


def test_a_non_node_return_raises_type_error() -> None:
    with pytest.raises(TypeError, match=r"rewrite_leaf.*str"):
        analyze(t("x"), REG, rewrite_leaf=lambda leaf: cast(Node, "oops"))


def test_an_exception_from_the_hook_propagates_unchanged() -> None:
    boom = ValueError("hook failed")

    def hook(leaf: Term | Phrase) -> Node:
        raise boom

    with pytest.raises(ValueError, match="hook failed") as caught:
        analyze(t("x"), REG, rewrite_leaf=hook)
    assert caught.value is boom


def test_the_leaf_is_analyzed_before_the_hook_runs() -> None:
    hook = Recorder(lambda leaf: Nothing())
    with pytest.raises(ValueError, match="analyzer refused"):
        analyze(t("bad", FieldRef("strict")), REG, rewrite_leaf=hook)
    assert hook.calls == []


# -- the result ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tree",
    [
        pytest.param(And(children=(t("a-b"), t("x"))), id="and"),
        pytest.param(Or(children=(t("a-b"), t("x"))), id="or"),
        pytest.param(Not(child=Phrase(field=CONTENT, text="a-b")), id="not-phrase"),
    ],
)
def test_a_hooked_result_is_a_fixed_point_of_plain_analysis(tree: Node) -> None:
    def hook(leaf: Term | Phrase) -> Node:
        return Or(children=(leaf, t(f"{leaf.text}-c", SIDE)))

    once = analyze(tree, REG, rewrite_leaf=hook)
    assert analyze(once, REG) == once


def test_spans_on_rebuilt_containers() -> None:
    leaf = Term(field=CONTENT, text="a-b", startchar=4, endchar=7)
    x = Term(field=CONTENT, text="x", startchar=12, endchar=13)
    tree = Not(child=And(children=(leaf, x), startchar=4, endchar=13), startchar=0, endchar=13)
    result = analyze(tree, REG, rewrite_leaf=wrap_only(leaf))
    assert isinstance(result, Not)
    assert (result.startchar, result.endchar) == (0, 13)
    assert isinstance(result.child, And)
    assert (result.child.startchar, result.child.endchar) == (4, 13)
    widened = result.child.children[0]
    assert isinstance(widened, Or)
    assert (widened.startchar, widened.endchar) == (4, 7)


def test_an_error_leaf_inside_a_replacement_is_kept() -> None:
    leaf = t("x")
    error = ErrorLeaf(
        diagnostic=Diagnostic(
            kind=DiagnosticKind.BAD_DATE, cause=Cause.INVALID_INPUT, message="bad"
        )
    )
    result = analyze(And(children=(leaf, Y)), REG, rewrite_leaf=wrap_only(leaf, error))
    assert result == And(children=(Or(children=(t("x"), error)), Y))


def test_host_built_nodes_keep_the_spans_the_host_gave_them() -> None:
    leaf = Term(field=CONTENT, text="x", startchar=4, endchar=5)
    companion = Term(field=SIDE, text="q", startchar=40, endchar=41)
    result = analyze(leaf, REG, rewrite_leaf=wrap_only(leaf, companion))
    assert isinstance(result, Or)
    kept = result.children[1]
    assert (kept.startchar, kept.endchar) == (40, 41)
    assert (result.startchar, result.endchar) == (4, 41)


# -- depth --------------------------------------------------------------------


def test_a_deep_tree_over_a_hooked_leaf() -> None:
    leaf = t("a-b")
    tree: Node = leaf
    for _ in range(50_000):
        tree = Not(child=tree)
    result = analyze(tree, REG, rewrite_leaf=wrap_only(leaf))
    for _ in range(50_000):
        assert isinstance(result, Not)
        result = result.child
    assert result == AND_SHAPED


def test_a_deep_replacement() -> None:
    leaf = t("a-b")
    deep: Node = Q
    for _ in range(50_000):
        deep = Not(child=deep)
    result = analyze(leaf, REG, rewrite_leaf=wrap_only(leaf, deep))
    assert isinstance(result, Or)
    assert result.children[0] == A_AND_B
