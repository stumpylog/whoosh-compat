"""ast.Fuzzy: a leaf node never produced by parse(), always hand-built by
a caller.

These tests pin, directly, that normalize()/analyze() pass this leaf
through untouched: both fall through generically for any node type they
don't special-case. If a future edit to either
function's dispatch narrows that generic fallthrough, these tests catch
it.
"""

from __future__ import annotations

from whoosh_compat.ast import And
from whoosh_compat.ast import Fuzzy
from whoosh_compat.ast import Term
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec


def test_fuzzy_survives_normalize_unchanged() -> None:
    node = Fuzzy(field=FieldRef("content"), text="tokyo", distance=1, prefix=False)
    assert normalize(node) == node


def test_fuzzy_survives_analyze_unchanged() -> None:
    registry = FieldRegistry([FieldSpec("content", FieldKind.TEXT, analyzer=str.split)])
    node = Fuzzy(field=FieldRef("content"), text="tokyo", distance=1, prefix=False)
    assert analyze(node, registry) == node


def test_fuzzy_is_a_leaf_with_no_child_nodes() -> None:
    # And(Fuzzy, Term) must not be flattened, merged, or otherwise
    # misinterpreted as having Fuzzy's own children: normalize()'s And rule
    # only ever inspects whether a child IS Nothing()/Every(), never
    # whether it recognizes the child's type.
    node = Fuzzy(field=FieldRef("content"), text="tokyo")
    other = Term(field=FieldRef("content"), text="bill")
    combined = And(children=(node, other))
    result = normalize(combined)
    assert isinstance(result, And)
    assert node in result.children
    assert other in result.children


def test_fuzzy_defaults() -> None:
    node = Fuzzy(field=FieldRef("content"), text="tokyo")
    assert node.distance == 1
    assert node.prefix is False


def test_fuzzy_equality_and_hash_follow_dataclass_fields() -> None:
    a = Fuzzy(field=FieldRef("content"), text="tokyo", distance=1)
    b = Fuzzy(field=FieldRef("content"), text="tokyo", distance=1)
    c = Fuzzy(field=FieldRef("content"), text="tokyo", distance=2)
    assert a == b
    assert hash(a) == hash(b)
    assert a != c


def test_fuzzy_startchar_endchar_excluded_from_equality() -> None:
    a = Fuzzy(field=FieldRef("content"), text="tokyo", startchar=0, endchar=5)
    b = Fuzzy(field=FieldRef("content"), text="tokyo")
    assert a == b
