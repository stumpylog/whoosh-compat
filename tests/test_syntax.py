from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic, DiagnosticKind
from whoosh_compat.parser import syntax
from whoosh_compat.parser.common import attach


class StubParser:
    fieldname: str | None = None

    def __init__(self) -> None:
        self.reports: list[Diagnostic] = []
        self.range_calls: list[tuple[Any, ...]] = []

    def term_query(self, fieldname: Any, text: Any, boost: float = 1.0,
                    **kw: Any) -> ast.Node:
        n = ast.Term(field=fieldname, text=text)
        return ast.Boosted(n, boost) if boost != 1.0 else n

    def range_query(self, fieldname: Any, start: Any, end: Any,
                     startexcl: bool, endexcl: bool, boost: float = 1.0,
                     node: Any = None) -> ast.Node:
        self.range_calls.append((fieldname, start, end, startexcl, endexcl,
                                  boost, node))
        return ast.TermRange(field=fieldname, lo=start, hi=end,
                              incl_lo=not startexcl, incl_hi=not endexcl)

    def report(self, diagnostic: Diagnostic) -> None:
        self.reports.append(diagnostic)


# --- Verbatim brief snippets ---------------------------------------------

def test_andgroup_builds_ast_and() -> None:
    g = syntax.AndGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.And(children=(ast.Term(field=None, text="a"),
                                   ast.Term(field=None, text="b")))


def test_notgroup() -> None:
    g = syntax.NotGroup([syntax.WordNode("a")])
    assert g.query(StubParser()) == ast.Not(ast.Term(field=None, text="a"))


def test_empty_group_is_nothing() -> None:
    assert syntax.AndGroup([]).query(StubParser()) == ast.Nothing()


# --- Analogous group coverage ---------------------------------------------

def test_orgroup_builds_ast_or() -> None:
    g = syntax.OrGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.Or(children=(ast.Term(field=None, text="a"),
                                  ast.Term(field=None, text="b")))


def test_dismaxgroup_maps_to_or() -> None:
    # v1 has no dedicated DisjunctionMax AST node, so DisMaxGroup degrades
    # to a plain Or of its children (documented in syntax.py).
    g = syntax.DisMaxGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.Or(children=(ast.Term(field=None, text="a"),
                                  ast.Term(field=None, text="b")))


def test_andnotgroup() -> None:
    g = syntax.AndNotGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.AndNot(positive=ast.Term(field=None, text="a"),
                            negative=ast.Term(field=None, text="b"))


def test_andmaybegroup() -> None:
    g = syntax.AndMaybeGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.AndMaybe(required=ast.Term(field=None, text="a"),
                              optional=ast.Term(field=None, text="b"))


def test_requiregroup() -> None:
    g = syntax.RequireGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == ast.Require(scored=ast.Term(field=None, text="a"),
                             filter_only=ast.Term(field=None, text="b"))


def test_group_boost_wraps_in_boosted() -> None:
    g = syntax.AndGroup([syntax.WordNode("a"), syntax.WordNode("b")],
                         boost=2.5)
    q = g.query(StubParser())
    assert q == ast.Boosted(
        ast.And(children=(ast.Term(field=None, text="a"),
                           ast.Term(field=None, text="b"))),
        2.5,
    )


def test_group_boost_of_one_does_not_wrap() -> None:
    g = syntax.AndGroup([syntax.WordNode("a")], boost=1.0)
    q = g.query(StubParser())
    assert q == ast.And(children=(ast.Term(field=None, text="a"),))


# --- attach() ---------------------------------------------------------

def test_attach_copies_span_onto_frozen_node() -> None:
    term = ast.Term(field=None, text="a")
    assert term.startchar is None and term.endchar is None

    node = syntax.WordNode("a")
    node.set_range(3, 4)

    attached = attach(term, node)

    # A new instance is returned (frozen dataclass -> dataclasses.replace).
    assert attached is not term
    assert attached == ast.Term(field=None, text="a", startchar=3, endchar=4)
    assert dataclasses.is_dataclass(attached)
    with pytest.raises(dataclasses.FrozenInstanceError):
        attached.startchar = 99  # type: ignore[misc]


def test_attach_none_passthrough() -> None:
    node = syntax.WordNode("a")
    node.set_range(0, 1)
    assert attach(None, node) is None


# --- RangeNode --------------------------------------------------------

def test_rangenode_calls_parser_range_query() -> None:
    node = syntax.RangeNode("a", "z", False, True)
    node.fieldname = "content"
    node.set_range(0, 10)
    parser = StubParser()

    q = node.query(parser)

    assert parser.range_calls == [("content", "a", "z", False, True, 1.0, node)]
    assert q == ast.TermRange(field="content", lo="a", hi="z", incl_lo=True,
                               incl_hi=False, startchar=0, endchar=10)


def test_rangenode_falls_back_to_parser_fieldname() -> None:
    node = syntax.RangeNode("a", "z", False, False)
    parser = StubParser()
    parser.fieldname = "body"

    node.query(parser)

    assert parser.range_calls[0][0] == "body"


# --- TextNode / WordNode ------------------------------------------------

def test_wordnode_uses_own_fieldname_over_parsers() -> None:
    node = syntax.WordNode("dog")
    node.fieldname = "title"
    parser = StubParser()
    parser.fieldname = "body"

    q = node.query(parser)

    assert q == ast.Term(field="title", text="dog")


def test_wordnode_passes_tokenize_and_removestops_flags() -> None:
    calls: list[dict[str, Any]] = []

    class RecordingParser(StubParser):
        def term_query(self, fieldname: Any, text: Any, boost: float = 1.0,
                        **kw: Any) -> ast.Node:
            calls.append({"fieldname": fieldname, "text": text,
                          "boost": boost, **kw})
            return ast.Term(field=fieldname, text=text)

    syntax.WordNode("dog").query(RecordingParser())

    assert calls == [{"fieldname": None, "text": "dog", "boost": 1.0,
                       "tokenize": True, "removestops": True}]


# --- ErrorNode ----------------------------------------------------------

def test_errornode_reports_diagnostic_and_returns_errorleaf() -> None:
    inner = syntax.WordNode("a")
    inner.set_range(2, 3)
    err = syntax.ErrorNode("bad thing", inner)
    parser = StubParser()

    q = err.query(parser)

    assert len(parser.reports) == 1
    diagnostic = parser.reports[0]
    assert diagnostic.message == "bad thing"
    assert diagnostic.kind == DiagnosticKind.UNKNOWN
    assert isinstance(q, ast.ErrorLeaf)
    assert q.diagnostic == diagnostic


# --- Operators (structural, no ast interaction) -------------------------

def test_infix_operator_replaces_with_group() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    group = syntax.AndGroup([a, syntax.Whitespace(), b])
    op = syntax.InfixOperator("AND", syntax.AndGroup)
    pos = op.replace_self(None, group, 1)
    assert pos == 1
    assert len(group) == 1
    assert isinstance(group[0], syntax.AndGroup)
    assert list(group[0]) == [a, b]


def test_prefix_operator_wraps_next_node() -> None:
    a = syntax.WordNode("a")
    group = syntax.AndGroup([syntax.Whitespace(), a])
    op = syntax.PrefixOperator("NOT", syntax.NotGroup)
    op.replace_self(None, group, 0)
    assert len(group) == 1
    assert isinstance(group[0], syntax.NotGroup)


def test_postfix_operator_wraps_prev_node() -> None:
    a = syntax.WordNode("a")
    group = syntax.AndGroup([a, syntax.Whitespace()])
    op = syntax.PostfixOperator("!", syntax.NotGroup)
    op.replace_self(None, group, 1)
    assert len(group) == 1
    assert isinstance(group[0], syntax.NotGroup)
