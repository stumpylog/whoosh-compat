from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from whoosh_compat.ast import And
from whoosh_compat.ast import AndMaybe
from whoosh_compat.ast import AndNot
from whoosh_compat.ast import Boosted
from whoosh_compat.ast import DateRange
from whoosh_compat.ast import ErrorLeaf
from whoosh_compat.ast import Every
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
from whoosh_compat.ast import Visitor
from whoosh_compat.ast import Wildcard
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef


# Step 1: Test construction of each node type
class TestNodeConstruction:
    def test_term_construction(self) -> None:
        t = Term(field=FieldRef("title"), text="hello")
        assert t.field == FieldRef("title")
        assert t.text == "hello"
        assert t.startchar is None
        assert t.endchar is None

    def test_term_with_numeric_text(self) -> None:
        t = Term(field=FieldRef("id"), text=42)
        assert t.field == FieldRef("id")
        assert t.text == 42

    def test_term_with_bool_text(self) -> None:
        t = Term(field=FieldRef("flag"), text=True)
        assert t.field == FieldRef("flag")
        assert t.text is True

    def test_term_with_span(self) -> None:
        t = Term(field=FieldRef("title"), text="hello", startchar=0, endchar=5)
        assert t.startchar == 0
        assert t.endchar == 5

    def test_and_construction(self) -> None:
        t1 = Term(field=None, text="a")
        t2 = Term(field=FieldRef("title"), text="b")
        a = And(children=(t1, t2))
        assert len(a.children) == 2
        assert t1.text == "a"
        assert t2.text == "b"

    def test_or_construction(self) -> None:
        o = Or(children=(Term(field=None, text="x"), Term(field=None, text="y")))
        assert len(o.children) == 2

    def test_not_construction(self) -> None:
        child = Term(field=None, text="excluded")
        n = Not(child=child)
        assert child.text == "excluded"
        assert n.child is child

    def test_andnot_construction(self) -> None:
        positive = Term(field=None, text="include")
        negative = Term(field=None, text="exclude")
        an = AndNot(positive=positive, negative=negative)
        assert positive.text == "include"
        assert negative.text == "exclude"
        assert an.positive is positive
        assert an.negative is negative

    def test_andmaybe_construction(self) -> None:
        required = Term(field=None, text="req")
        optional = Term(field=None, text="opt")
        am = AndMaybe(required=required, optional=optional)
        assert required.text == "req"
        assert optional.text == "opt"
        assert am.required is required
        assert am.optional is optional

    def test_require_construction(self) -> None:
        scored = Term(field=None, text="score")
        filter_only = Term(field=None, text="filter")
        r = Require(scored=scored, filter_only=filter_only)
        assert scored.text == "score"
        assert filter_only.text == "filter"
        assert r.scored is scored
        assert r.filter_only is filter_only

    def test_phrase_construction(self) -> None:
        p = Phrase(field=FieldRef("content"), text="hello world")
        assert p.field == FieldRef("content")
        assert p.text == "hello world"
        assert p.slop == 1

    def test_phrase_with_slop(self) -> None:
        p = Phrase(field=FieldRef("content"), text="hello world", slop=5)
        assert p.slop == 5

    def test_prefix_construction(self) -> None:
        pf = Prefix(field=FieldRef("title"), text="auto")
        assert pf.field == FieldRef("title")
        assert pf.text == "auto"

    def test_wildcard_construction(self) -> None:
        w = Wildcard(field=FieldRef("name"), pattern="he*lo")
        assert w.field == FieldRef("name")
        assert w.pattern == "he*lo"

    def test_termrange_construction(self) -> None:
        tr = TermRange(field=FieldRef("tags"), lo="a", hi="z", incl_lo=True, incl_hi=False)
        assert tr.field == FieldRef("tags")
        assert tr.lo == "a"
        assert tr.hi == "z"
        assert tr.incl_lo is True
        assert tr.incl_hi is False

    def test_termrange_with_none(self) -> None:
        tr = TermRange(field=FieldRef("tags"), lo=None, hi="z", incl_lo=False, incl_hi=True)
        assert tr.lo is None
        assert tr.hi == "z"

    def test_numericrange_construction(self) -> None:
        nr = NumericRange(field=FieldRef("age"), lo=18, hi=65, incl_lo=True, incl_hi=True)
        assert nr.field == FieldRef("age")
        assert nr.lo == 18
        assert nr.hi == 65
        assert nr.incl_lo is True
        assert nr.incl_hi is True

    def test_numericrange_with_none(self) -> None:
        nr = NumericRange(field=FieldRef("count"), lo=None, hi=100, incl_lo=False, incl_hi=False)
        assert nr.lo is None
        assert nr.hi == 100

    def test_daterange_construction(self) -> None:
        start = datetime(2020, 1, 1)
        end = datetime(2020, 12, 31)
        dr = DateRange(field=FieldRef("date"), lo=start, hi=end, incl_lo=True, incl_hi=True)
        assert dr.field == FieldRef("date")
        assert dr.lo == start
        assert dr.hi == end

    def test_daterange_with_none(self) -> None:
        dr = DateRange(field=FieldRef("created"), lo=None, hi=None, incl_lo=False, incl_hi=False)
        assert dr.lo is None
        assert dr.hi is None

    def test_every_construction_default(self) -> None:
        e = Every()
        assert e.field is None

    def test_every_construction_with_field(self) -> None:
        e = Every(field=FieldRef("published"))
        assert e.field == FieldRef("published")

    def test_nothing_construction(self) -> None:
        n = Nothing()
        assert isinstance(n, Node)

    def test_boosted_construction(self) -> None:
        child = Term(field=None, text="boost")
        b = Boosted(child=child, boost=2.5)
        assert child.text == "boost"
        assert b.child is child
        assert b.boost == 2.5

    def test_errorleaf_construction(self) -> None:
        diag = Diagnostic(
            message="bad value",
            kind=DiagnosticKind.BAD_DATE,
            cause=Cause.INVALID_INPUT,
            startchar=10,
            endchar=20,
        )
        el = ErrorLeaf(diagnostic=diag)
        assert el.diagnostic is diag
        assert el.diagnostic.message == "bad value"


# Step 2: Test equality and identity
class TestEquality:
    def test_term_equality(self) -> None:
        t1 = Term(field=FieldRef("t"), text="x")
        t2 = Term(field=FieldRef("t"), text="x")
        assert t1 == t2

    def test_term_inequality(self) -> None:
        t1 = Term(field=FieldRef("t"), text="x")
        t2 = Term(field=FieldRef("t"), text="y")
        assert t1 != t2

    def test_and_equality(self) -> None:
        children = (Term(field=None, text="a"), Term(field=None, text="b"))
        a1 = And(children=children)
        a2 = And(children=children)
        assert a1 == a2

    def test_complex_equality(self) -> None:
        t1 = Term(field=FieldRef("f"), text="x")
        t2 = Term(field=FieldRef("f"), text="x")
        b1 = Boosted(child=t1, boost=1.5)
        b2 = Boosted(child=t2, boost=1.5)
        assert b1 == b2


# Step 3: Test frozen instances
class TestFrozen:
    def test_term_frozen(self) -> None:
        t = Term(field=FieldRef("title"), text="hello")
        with pytest.raises(FrozenInstanceError):
            t.field = FieldRef("other")  # type: ignore[misc]

    def test_and_frozen(self) -> None:
        a = And(children=(Term(field=None, text="a"),))
        with pytest.raises(FrozenInstanceError):
            a.children = ()  # type: ignore[misc]

    def test_nothing_frozen(self) -> None:
        n = Nothing()
        with pytest.raises(FrozenInstanceError):
            n.startchar = 10  # type: ignore[misc]


# Step 4: Test visitor dispatch
class Counter(Visitor[int]):
    def visit_term(self, n: Term) -> int:
        return 1

    def visit_and(self, n: And) -> int:
        return sum(self.visit(c) for c in n.children)

    def visit_or(self, n: Or) -> int:
        return sum(self.visit(c) for c in n.children)

    def visit_not(self, n: Not) -> int:
        return self.visit(n.child)

    def visit_boosted(self, n: Boosted) -> int:
        return self.visit(n.child)


class TestVisitor:
    def test_visitor_dispatch_term(self) -> None:
        t = Term(field=None, text="hello")
        assert Counter().visit(t) == 1

    def test_visitor_dispatch_and(self) -> None:
        tree = And(children=(Term(field=None, text="a"), Term(field=FieldRef("title"), text="b")))
        assert Counter().visit(tree) == 2

    def test_visitor_dispatch_nested(self) -> None:
        tree = And(
            children=(
                Term(field=None, text="a"),
                Or(children=(Term(field=None, text="b"), Term(field=None, text="c"))),
            )
        )
        assert Counter().visit(tree) == 3

    def test_visitor_dispatch_with_boost(self) -> None:
        tree = Boosted(
            child=And(children=(Term(field=None, text="a"), Term(field=None, text="b"))), boost=2.0
        )
        assert Counter().visit(tree) == 2

    def test_visitor_missing_falls_to_generic(self) -> None:
        n = Nothing()
        with pytest.raises(NotImplementedError) as exc_info:
            Counter().visit(n)
        assert "Nothing" in str(exc_info.value)

    def test_visitor_missing_phrase(self) -> None:
        p = Phrase(field=FieldRef("content"), text="hello world")
        with pytest.raises(NotImplementedError) as exc_info:
            Counter().visit(p)
        assert "Phrase" in str(exc_info.value)

    def test_visitor_dispatch_walks_mro_for_node_subclass(self) -> None:
        # A Node subclass with no visit_<exact-class-name> method of its own
        # must still dispatch through its nearest ancestor's visit_* method
        # (here, Term's), not fall straight to generic_visit: the previous
        # exact-class-name dispatch treated any such subclass as completely
        # unhandled, turning a legitimate Term specialization into
        # AST_INVALID_SHAPE -> HTTP 500 at the emitter.
        class MyTerm(Term):
            pass

        t = MyTerm(field=None, text="hello")
        assert Counter().visit(t) == 1
