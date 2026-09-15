"""Duplicate-sibling removal in ``normalize()``: which siblings count as
duplicates.

Two siblings are duplicates when they are the same object, or the same node
type with equal ``compare=True`` fields, where nested nodes compare the same
way, tuples compare element by element, and any other value must have the
same type, be hashable and equal to itself, and compare ``==``. ``Phrase``
additionally compares ``words`` and ``analyzed``, and ``Term`` compares
``analyzed``, at every depth. Anything that can search differently is kept.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal

import pytest

from whoosh_compat import ast
from whoosh_compat import parse
from whoosh_compat.ast import And
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import DateRange
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import NumericRange
from whoosh_compat.ast import Or
from whoosh_compat.ast import Phrase
from whoosh_compat.ast import Term
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

F = FieldRef("f")
D = FieldRef("d")


def t(text: str = "a") -> Term:
    return Term(field=F, text=text)


X = t("x")


@dataclass(frozen=True, slots=True)
class Tagged(Node):
    """A caller-defined leaf with an arbitrary atom field."""

    tags: object


@dataclass(frozen=True, slots=True)
class Grouped(Node):
    """A caller-defined node holding nodes inside (possibly nested) tuples."""

    items: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Pair(Node):
    """A caller-defined node with a direct node field and a tuple field."""

    head: Node
    items: tuple[object, ...]


def sibling_count(*siblings: Node) -> int:
    result = normalize(Or(children=siblings))
    return len(result.children) if isinstance(result, Or) else 1


def nested(leaf: Node) -> And:
    return And(children=(leaf, X))


SHARED_NAN = float("nan")
SHARED_NAN_BOOST = Boosted(child=t(), boost=SHARED_NAN)
SHARED_NAN_TERM = Term(field=F, text=SHARED_NAN)  # type: ignore[arg-type]

UTC_NOON = datetime(2026, 1, 1, 12, tzinfo=UTC)
PLUS_ONE_1PM = datetime(2026, 1, 1, 13, tzinfo=timezone(timedelta(hours=1)))


def date_range(lo: datetime) -> DateRange:
    return DateRange(field=D, lo=lo, hi=None, incl_lo=True, incl_hi=True)


P_AB_C = Phrase(field=F, text="a b c", words=("a b", "c"), analyzed=True)
P_A_BC = Phrase(field=F, text="a b c", words=("a", "b c"), analyzed=True)


@pytest.mark.parametrize(
    ("siblings", "expected"),
    [
        pytest.param(
            (nested(Term(field=F, text=1)), nested(Term(field=F, text=True))),
            2,
            id="nested-int-and-bool-text-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=2), Boosted(child=t(), boost=2.0)),
            2,
            id="int-and-float-boost-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=float("nan")), Boosted(child=t(), boost=float("nan"))),
            2,
            id="distinct-nan-boosts-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=SHARED_NAN), Boosted(child=t(), boost=SHARED_NAN)),
            2,
            id="distinct-boosts-sharing-one-nan-object-kept",
        ),
        pytest.param(
            (Not(child=SHARED_NAN_BOOST), Not(child=SHARED_NAN_BOOST)),
            2,
            id="parents-sharing-a-composite-nan-child-kept",
        ),
        pytest.param(
            (SHARED_NAN_BOOST, SHARED_NAN_BOOST),
            1,
            id="same-nan-boost-object-twice-merged",
        ),
        pytest.param(
            (
                Term(field=F, text=float("nan")),  # type: ignore[arg-type]
                Term(field=F, text=float("nan")),  # type: ignore[arg-type]
            ),
            2,
            id="terms-with-distinct-nan-text-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=[2]), Boosted(child=t(), boost=[3])),  # type: ignore[arg-type]
            2,
            id="unhashable-boosts-with-different-values-kept",
        ),
        pytest.param(
            (nested(date_range(UTC_NOON)), nested(date_range(PLUS_ONE_1PM))),
            2,
            id="nested-same-instant-in-two-zones-kept",
        ),
        pytest.param(
            (nested(date_range(UTC_NOON)), nested(date_range(UTC_NOON.replace(fold=1)))),
            2,
            id="nested-datetimes-differing-only-in-fold-kept",
        ),
        pytest.param(
            (
                Boosted(child=t(), boost=Decimal("2.0")),  # type: ignore[arg-type]
                Boosted(child=t(), boost=Decimal("2.00")),  # type: ignore[arg-type]
            ),
            2,
            id="equal-decimal-boosts-with-different-exponents-kept",
        ),
        pytest.param(
            (
                nested(Term(field=F, text=Decimal("2.0"))),  # type: ignore[arg-type]
                nested(Term(field=F, text=Decimal("2.00"))),  # type: ignore[arg-type]
            ),
            2,
            id="nested-decimal-text-different-exponents-kept",
        ),
    ],
)
def test_unchanged_dedupe_outcomes(siblings: tuple[Node, ...], expected: int) -> None:
    assert sibling_count(*siblings) == expected


def test_parents_sharing_a_nan_leaf_are_equal_under_eq() -> None:
    # Two parents sharing one leaf object are merged by identity rather than
    # by field values; that is only safe because node equality agrees.
    assert Not(child=SHARED_NAN_TERM) == Not(child=SHARED_NAN_TERM)


@pytest.mark.parametrize(
    ("siblings", "expected"),
    [
        pytest.param(
            (nested(P_AB_C), nested(P_A_BC)),
            2,
            id="nested-phrases-with-different-words-kept",
        ),
        pytest.param(
            (
                nested(Term(field=F, text="foo", analyzed=True)),
                nested(Term(field=F, text="foo", analyzed=False)),
            ),
            2,
            id="nested-terms-with-different-analyzed-kept",
        ),
        pytest.param(
            (Term(field=F, text=1), Term(field=F, text=True)),
            2,
            id="int-and-bool-text-kept",
        ),
        pytest.param(
            (Term(field=F, text=1), Term(field=F, text=1.0)),  # type: ignore[arg-type]
            2,
            id="int-and-float-text-kept",
        ),
        pytest.param(
            (Term(field=F, text="a", analyzed=1), Term(field=F, text="a", analyzed=True)),  # type: ignore[arg-type]
            2,
            id="int-and-bool-analyzed-kept",
        ),
        pytest.param(
            (
                NumericRange(field=F, lo=1, hi=None, incl_lo=True, incl_hi=True),
                NumericRange(field=F, lo=True, hi=None, incl_lo=True, incl_hi=True),
            ),
            2,
            id="int-and-bool-range-bound-kept",
        ),
        pytest.param(
            (Phrase(field=F, text="a b", slop=1), Phrase(field=F, text="a b", slop=True)),
            2,
            id="int-and-bool-slop-kept",
        ),
        pytest.param(
            (Not(child=SHARED_NAN_TERM), Not(child=SHARED_NAN_TERM)),
            1,
            id="parents-sharing-a-nan-leaf-merged",
        ),
        pytest.param(
            (
                Term(field=F, text=SHARED_NAN),  # type: ignore[arg-type]
                Term(field=F, text=SHARED_NAN),  # type: ignore[arg-type]
            ),
            2,
            id="terms-sharing-one-nan-object-kept",
        ),
        pytest.param(
            (
                Boosted(child=t(), boost=Decimal("NaN")),  # type: ignore[arg-type]
                Boosted(child=t(), boost=Decimal("NaN")),  # type: ignore[arg-type]
            ),
            2,
            id="decimal-nan-boosts-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=[2]), Boosted(child=t(), boost=[2])),  # type: ignore[arg-type]
            2,
            id="unhashable-boosts-with-equal-values-kept",
        ),
        pytest.param(
            (Tagged(tags=[1]), Tagged(tags=[1])),
            2,
            id="caller-leaf-with-unhashable-field-kept",
        ),
        pytest.param(
            (Grouped(items=((P_AB_C,),)), Grouped(items=((P_A_BC,),))),
            2,
            id="phrases-with-different-words-in-nested-tuples-kept",
        ),
        pytest.param(
            (Pair(head=t(), items=((X,),)), t("other")),
            2,
            id="node-with-direct-and-nested-tuple-children-keyed",
        ),
        pytest.param(
            (X, Phrase(field=F, text="a b c", words=(X,))),  # type: ignore[arg-type]
            2,
            id="node-nested-in-phrase-words-sibling-first-kept",
        ),
        pytest.param(
            (Phrase(field=F, text="a b c", words=(X,)), X),  # type: ignore[arg-type]
            2,
            id="node-nested-in-phrase-words-phrase-first-kept",
        ),
        pytest.param(
            (Term(field=F, text="a", analyzed=X), X),  # type: ignore[arg-type]
            2,
            id="node-nested-in-term-analyzed-kept",
        ),
        pytest.param(
            (Boosted(child=t(), boost=-0.0), Boosted(child=t(), boost=0.0)),
            2,
            id="negative-and-positive-zero-boost-kept",
        ),
        pytest.param(
            (
                Term(field=F, text=Decimal("2.0")),  # type: ignore[arg-type]
                Term(field=F, text=Decimal("2.00")),  # type: ignore[arg-type]
            ),
            2,
            id="decimal-text-different-exponents-kept",
        ),
        pytest.param(
            (Term(field=F, text=-0.0), Term(field=F, text=0.0)),  # type: ignore[arg-type]
            2,
            id="negative-and-positive-zero-text-kept",
        ),
        pytest.param(
            (
                Term(field=FieldRef("f", ["x"]), text="a"),  # type: ignore[arg-type]
                Term(field=FieldRef("f", ["x"]), text="a"),  # type: ignore[arg-type]
            ),
            2,
            id="term-with-unhashable-json-path-field-kept",
        ),
    ],
)
def test_dedupe_outcomes(siblings: tuple[Node, ...], expected: int) -> None:
    assert sibling_count(*siblings) == expected


# One interner per public call: normalize() and analyze() each build one and
# thread it through every normalize and combine step inside the call, so a
# node is keyed once per call.
REGISTRY = FieldRegistry([FieldSpec("f", FieldKind.TEXT, analyzer=str.split)])
MULTI = And(children=(Term(field=F, text="a b"), Term(field=F, text="c")))


def widen(leaf: Term | Phrase) -> Node:
    return Or(children=(leaf, Term(field=F, text="companion")))


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: normalize(MULTI), id="normalize"),
        pytest.param(lambda: analyze(MULTI, REGISTRY), id="analyze"),
        pytest.param(lambda: analyze(MULTI, REGISTRY, rewrite_leaf=widen), id="analyze-with-hook"),
    ],
)
def test_one_interner_per_public_call(
    monkeypatch: pytest.MonkeyPatch, call: Callable[[], Node]
) -> None:
    created: list[object] = []
    original_init = ast._Interner.__init__

    def counting_init(self: ast._Interner) -> None:
        created.append(self)
        original_init(self)

    monkeypatch.setattr(ast._Interner, "__init__", counting_init)
    call()
    assert len(created) == 1


# Nested groups of one type flatten, so a tree stays deep only when And and
# Or alternate. Keying each subtree once per call keeps normalizing such a
# tree linear in its depth; a key rebuilt per ancestor made it roughly cubic.
def alternating_groups(depth: int) -> Node:
    tree: Node = t("z")
    for i in range(depth):
        group = And if i % 2 else Or
        tree = group(children=(t(f"t{i}"), tree))
    return tree


@pytest.mark.wall_clock
def test_deep_alternating_groups_normalize_quickly() -> None:
    depth = 1000
    tree = alternating_groups(depth)
    start = time.perf_counter()
    result = normalize(tree)
    elapsed = time.perf_counter() - start
    # Walk the result rather than comparing it: == and hash() on a tree
    # this deep recurse.
    levels = 0
    node = result
    while isinstance(node, (And, Or)):
        assert len(node.children) == 2
        levels += 1
        node = node.children[1]
    assert levels == depth
    assert elapsed < 2.0, f"depth-{depth} normalize took {elapsed:.3f}s"


@pytest.mark.wall_clock
def test_deep_alternating_query_parses_and_analyzes_quickly() -> None:
    fields = ["title", "content", "correspondent", "document_type", "tag"]
    registry = FieldRegistry(
        [FieldSpec(name, FieldKind.TEXT, analyzer=lambda s: s.lower().split()) for name in fields]
    )
    # Two parentheses per level keeps 99 levels under the parser's nesting cap.
    levels = 99
    query = "".join(f"a{i} OR (b{i} (" for i in range(levels)) + "z" + "))" * levels
    start = time.perf_counter()
    result = parse(query, registry=registry, default_fields=fields)
    analyze(result.ast, registry)
    elapsed = time.perf_counter() - start
    assert result.diagnostics == ()
    assert elapsed < 3.0, f"{levels}-level parse + analyze took {elapsed:.3f}s"
