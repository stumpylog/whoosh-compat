"""Property test: duplicate-sibling removal agrees with a reference
equality written out directly, on generated sibling lists that share
objects, carry NaN and other non-reflexive or unhashable values, mix value
types that compare ``==``, and nest nodes inside tuples.

``ref_eq``/``val_eq`` restate the documented rule recursively (fine for the
small trees generated here). Removal keeps the first of each run of
duplicates, so the expected result is every sibling that no earlier sibling
equals.
"""

from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from whoosh_compat import ast
from whoosh_compat.ast import And
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import Or
from whoosh_compat.ast import Phrase
from whoosh_compat.ast import Term
from whoosh_compat.fields import FieldRef

F = FieldRef("f")


@dataclass(frozen=True, slots=True)
class Box(Node):
    """A caller-defined leaf with an arbitrary atom field."""

    v: object


@dataclass(frozen=True, slots=True)
class Mixed(Node):
    """A caller-defined node mixing nodes and atoms in nested tuples."""

    items: tuple[object, ...]


def dedupe(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
    return ast._dedupe(nodes, interner=ast._Interner())


def keyable(value: object) -> bool:
    try:
        hash(value)
        reflexive = bool(value == value)  # noqa: PLR0124 - NaN-like values are not equal to themselves
        repr(value)
    except Exception:  # noqa: BLE001 - an arbitrary value's hash/==/repr can raise anything
        return False
    return reflexive


def val_eq(x: object, y: object) -> bool:
    if isinstance(x, Node) or isinstance(y, Node):
        return isinstance(x, Node) and isinstance(y, Node) and ref_eq(x, y)
    if isinstance(x, tuple) or isinstance(y, tuple):
        if not (isinstance(x, tuple) and isinstance(y, tuple)):
            return False
        return (
            type(x) is type(y)
            and len(x) == len(y)
            and all(val_eq(a, b) for a, b in zip(x, y, strict=True))
        )
    return type(x) is type(y) and keyable(x) and keyable(y) and bool(x == y) and repr(x) == repr(y)


def ref_eq(a: Node, b: Node) -> bool:
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    names = [f.name for f in dataclass_fields(a) if f.compare]
    if isinstance(a, Phrase):
        names += ["words", "analyzed"]
    elif isinstance(a, Term):
        names.append("analyzed")
    return all(val_eq(getattr(a, name), getattr(b, name)) for name in names)


def expected_dedupe(nodes: tuple[Node, ...]) -> list[Node]:
    return [n for i, n in enumerate(nodes) if not any(ref_eq(m, n) for m in nodes[:i])]


def assert_matches_reference(nodes: tuple[Node, ...]) -> None:
    result = dedupe(nodes)
    expected = expected_dedupe(nodes)
    assert len(result) == len(expected)
    assert all(r is e for r, e in zip(result, expected, strict=True))


SHARED_NAN = float("nan")
UTC_NOON = datetime(2026, 1, 1, 12, tzinfo=UTC)

atoms = st.one_of(
    st.sampled_from(["a", "b", "a b"]),
    st.sampled_from([0, 1, 2]),
    st.booleans(),
    st.sampled_from([0.0, -0.0, 1.0, 2.0, SHARED_NAN]),
    st.builds(float, st.just("nan")),
    st.sampled_from([Decimal("2.0"), Decimal("2.00")]),
    st.builds(Decimal, st.just("NaN")),
    st.sampled_from(
        [
            UTC_NOON,
            datetime(2026, 1, 1, 13, tzinfo=timezone(timedelta(hours=1))),
            UTC_NOON.replace(fold=1),
        ]
    ),
    st.builds(lambda: [1]),
)

# A plain Term, drawn separately from the recursive `trees` strategy below,
# for embedding as a Node value inside Phrase.words/Term.analyzed: both are
# excluded from node equality, so a Node placed there is discovered only
# through the extra keying rule for those two fields, not the general
# compare=True walk (see _keyed_values in the library).
node_in_field = st.builds(
    Term, field=st.just(F), text=st.sampled_from(["a", "b"]), analyzed=st.booleans()
)

leaves = st.one_of(
    st.builds(
        Term,
        field=st.just(F),
        text=st.one_of(st.sampled_from(["a", "b"]), atoms),
        analyzed=st.one_of(st.booleans(), st.sampled_from([0, 1]), node_in_field),
    ),
    st.builds(
        Phrase,
        field=st.just(F),
        text=st.just("a b c"),
        words=st.one_of(
            st.sampled_from([None, (), ("a b", "c"), ("a", "b c")]),
            st.builds(lambda n: (n,), node_in_field),
        ),
        analyzed=st.booleans(),
    ),
    st.builds(Box, v=atoms),
    st.just(Nothing()),
)


def extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    return st.one_of(
        st.builds(lambda cs: And(children=tuple(cs)), st.lists(children, min_size=1, max_size=3)),
        st.builds(lambda cs: Or(children=tuple(cs)), st.lists(children, min_size=1, max_size=3)),
        st.builds(Not, child=children),
        st.builds(Boosted, child=children, boost=atoms),
        st.builds(Mixed, items=st.tuples(children, st.one_of(atoms, st.tuples(children, atoms)))),
    )


trees = st.recursive(leaves, extend, max_leaves=8)


@st.composite
def sibling_lists(draw: st.DrawFn) -> tuple[Node, ...]:
    pool = draw(st.lists(trees, min_size=1, max_size=4))
    shared = st.sampled_from(pool)
    sibling = st.one_of(
        shared,
        st.builds(Not, child=shared),
        st.builds(lambda a, b: And(children=(a, b)), shared, shared),
        trees,
    )
    return tuple(draw(st.lists(sibling, min_size=1, max_size=6)))


@settings(deadline=None)
@given(sibling_lists())
def test_dedupe_matches_reference_equality(nodes: tuple[Node, ...]) -> None:
    assert_matches_reference(nodes)


SHARED_LEAF = Term(field=F, text="a")
P_AB_C = Phrase(field=F, text="a b c", words=("a b", "c"), analyzed=True)
P_A_BC = Phrase(field=F, text="a b c", words=("a", "b c"), analyzed=True)


@pytest.mark.parametrize(
    "nodes",
    [
        # A child reference must never equal an atom in the same position,
        # whatever integer the child happens to be interned as.
        pytest.param(
            (Mixed(items=(SHARED_LEAF, 0)), Mixed(items=(0, SHARED_LEAF))),
            id="node-and-int-swapped-in-a-tuple",
        ),
        pytest.param(
            (Box(v=SHARED_LEAF), Box(v=("n", 0))),
            id="node-versus-tagged-tuple-atom",
        ),
        pytest.param(
            (Mixed(items=((P_AB_C,),)), Mixed(items=((P_A_BC,),))),
            id="phrase-words-inside-nested-tuples",
        ),
        pytest.param(
            (Box(v=frozenset({1})), Box(v=frozenset({1}))),
            id="equal-frozensets-merge",
        ),
        pytest.param(
            (Box(v=SHARED_NAN), Box(v=SHARED_NAN)),
            id="same-nan-object-in-two-boxes",
        ),
    ],
)
def test_dedupe_matches_reference_equality_on_fixed_cases(nodes: tuple[Node, ...]) -> None:
    assert_matches_reference(nodes)
