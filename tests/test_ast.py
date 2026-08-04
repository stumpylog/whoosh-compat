import pytest
from datetime import datetime
from dataclasses import FrozenInstanceError

from whoosh_compat.ast import (
    Node, Term, And, Or, Not, AndNot, AndMaybe, Require, Phrase, Prefix,
    Wildcard, TermRange, NumericRange, DateRange, Every, Nothing, Boosted,
    ErrorLeaf, Visitor
)
from whoosh_compat.errors import Diagnostic, DiagnosticKind


# Step 1: Test construction of each node type
class TestNodeConstruction:
    def test_term_construction(self):
        t = Term(field="title", text="hello")
        assert t.field == "title"
        assert t.text == "hello"
        assert t.startchar is None
        assert t.endchar is None

    def test_term_with_numeric_text(self):
        t = Term(field="id", text=42)
        assert t.field == "id"
        assert t.text == 42

    def test_term_with_bool_text(self):
        t = Term(field="flag", text=True)
        assert t.field == "flag"
        assert t.text is True

    def test_term_with_span(self):
        t = Term(field="title", text="hello", startchar=0, endchar=5)
        assert t.startchar == 0
        assert t.endchar == 5

    def test_and_construction(self):
        a = And(children=(Term(field=None, text="a"), Term(field="title", text="b")))
        assert len(a.children) == 2
        assert a.children[0].text == "a"
        assert a.children[1].text == "b"

    def test_or_construction(self):
        o = Or(children=(Term(field=None, text="x"), Term(field=None, text="y")))
        assert len(o.children) == 2

    def test_not_construction(self):
        n = Not(child=Term(field=None, text="excluded"))
        assert n.child.text == "excluded"

    def test_andnot_construction(self):
        an = AndNot(
            positive=Term(field=None, text="include"),
            negative=Term(field=None, text="exclude")
        )
        assert an.positive.text == "include"
        assert an.negative.text == "exclude"

    def test_andmaybe_construction(self):
        am = AndMaybe(
            required=Term(field=None, text="req"),
            optional=Term(field=None, text="opt")
        )
        assert am.required.text == "req"
        assert am.optional.text == "opt"

    def test_require_construction(self):
        r = Require(
            scored=Term(field=None, text="score"),
            filter_only=Term(field=None, text="filter")
        )
        assert r.scored.text == "score"
        assert r.filter_only.text == "filter"

    def test_phrase_construction(self):
        p = Phrase(field="content", text="hello world")
        assert p.field == "content"
        assert p.text == "hello world"
        assert p.slop == 1

    def test_phrase_with_slop(self):
        p = Phrase(field="content", text="hello world", slop=5)
        assert p.slop == 5

    def test_prefix_construction(self):
        pf = Prefix(field="title", text="auto")
        assert pf.field == "title"
        assert pf.text == "auto"

    def test_wildcard_construction(self):
        w = Wildcard(field="name", pattern="he*lo")
        assert w.field == "name"
        assert w.pattern == "he*lo"

    def test_termrange_construction(self):
        tr = TermRange(field="tags", lo="a", hi="z", incl_lo=True, incl_hi=False)
        assert tr.field == "tags"
        assert tr.lo == "a"
        assert tr.hi == "z"
        assert tr.incl_lo is True
        assert tr.incl_hi is False

    def test_termrange_with_none(self):
        tr = TermRange(field="tags", lo=None, hi="z", incl_lo=False, incl_hi=True)
        assert tr.lo is None
        assert tr.hi == "z"

    def test_numericrange_construction(self):
        nr = NumericRange(field="age", lo=18, hi=65, incl_lo=True, incl_hi=True)
        assert nr.field == "age"
        assert nr.lo == 18
        assert nr.hi == 65
        assert nr.incl_lo is True
        assert nr.incl_hi is True

    def test_numericrange_with_none(self):
        nr = NumericRange(field="count", lo=None, hi=100, incl_lo=False, incl_hi=False)
        assert nr.lo is None
        assert nr.hi == 100

    def test_daterange_construction(self):
        start = datetime(2020, 1, 1)
        end = datetime(2020, 12, 31)
        dr = DateRange(field="date", lo=start, hi=end, incl_lo=True, incl_hi=True)
        assert dr.field == "date"
        assert dr.lo == start
        assert dr.hi == end

    def test_daterange_with_none(self):
        dr = DateRange(field="created", lo=None, hi=None, incl_lo=False, incl_hi=False)
        assert dr.lo is None
        assert dr.hi is None

    def test_every_construction_default(self):
        e = Every()
        assert e.field is None

    def test_every_construction_with_field(self):
        e = Every(field="published")
        assert e.field == "published"

    def test_nothing_construction(self):
        n = Nothing()
        assert isinstance(n, Node)

    def test_boosted_construction(self):
        b = Boosted(child=Term(field=None, text="boost"), boost=2.5)
        assert b.child.text == "boost"
        assert b.boost == 2.5

    def test_errorleaf_construction(self):
        diag = Diagnostic("bad value", DiagnosticKind.BAD_DATE, 10, 20)
        el = ErrorLeaf(diagnostic=diag)
        assert el.diagnostic is diag
        assert el.diagnostic.message == "bad value"


# Step 2: Test equality and identity
class TestEquality:
    def test_term_equality(self):
        t1 = Term(field="t", text="x")
        t2 = Term(field="t", text="x")
        assert t1 == t2

    def test_term_inequality(self):
        t1 = Term(field="t", text="x")
        t2 = Term(field="t", text="y")
        assert t1 != t2

    def test_and_equality(self):
        children = (Term(field=None, text="a"), Term(field=None, text="b"))
        a1 = And(children=children)
        a2 = And(children=children)
        assert a1 == a2

    def test_complex_equality(self):
        t1 = Term(field="f", text="x")
        t2 = Term(field="f", text="x")
        b1 = Boosted(child=t1, boost=1.5)
        b2 = Boosted(child=t2, boost=1.5)
        assert b1 == b2


# Step 3: Test frozen instances
class TestFrozen:
    def test_term_frozen(self):
        t = Term(field="title", text="hello")
        with pytest.raises(FrozenInstanceError):
            t.field = "other"

    def test_and_frozen(self):
        a = And(children=(Term(field=None, text="a"),))
        with pytest.raises(FrozenInstanceError):
            a.children = ()

    def test_nothing_frozen(self):
        n = Nothing()
        with pytest.raises(FrozenInstanceError):
            n.startchar = 10


# Step 4: Test visitor dispatch
class Counter(Visitor[int]):
    def visit_term(self, n):
        return 1

    def visit_and(self, n):
        return sum(self.visit(c) for c in n.children)

    def visit_or(self, n):
        return sum(self.visit(c) for c in n.children)

    def visit_not(self, n):
        return self.visit(n.child)

    def visit_boosted(self, n):
        return self.visit(n.child)


class TestVisitor:
    def test_visitor_dispatch_term(self):
        t = Term(field=None, text="hello")
        assert Counter().visit(t) == 1

    def test_visitor_dispatch_and(self):
        tree = And(children=(Term(field=None, text="a"), Term(field="title", text="b")))
        assert Counter().visit(tree) == 2

    def test_visitor_dispatch_nested(self):
        tree = And(children=(
            Term(field=None, text="a"),
            Or(children=(Term(field=None, text="b"), Term(field=None, text="c")))
        ))
        assert Counter().visit(tree) == 3

    def test_visitor_dispatch_with_boost(self):
        tree = Boosted(
            child=And(children=(Term(field=None, text="a"), Term(field=None, text="b"))),
            boost=2.0
        )
        assert Counter().visit(tree) == 2

    def test_visitor_missing_falls_to_generic(self):
        n = Nothing()
        with pytest.raises(NotImplementedError) as exc_info:
            Counter().visit(n)
        assert "Nothing" in str(exc_info.value)

    def test_visitor_missing_phrase(self):
        p = Phrase(field="content", text="hello world")
        with pytest.raises(NotImplementedError) as exc_info:
            Counter().visit(p)
        assert "Phrase" in str(exc_info.value)
