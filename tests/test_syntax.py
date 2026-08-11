from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.parser import syntax
from whoosh_compat.parser.common import attach


class StubParser:
    fieldname: str | None = None

    def __init__(self) -> None:
        self.reports: list[Diagnostic] = []
        self.range_calls: list[tuple[Any, ...]] = []

    def term_query(self, fieldname: Any, text: Any, boost: float = 1.0, **kw: Any) -> ast.Node:
        n = ast.Term(field=FieldRef(fieldname) if fieldname is not None else None, text=text)
        return ast.Boosted(n, boost) if boost != 1.0 else n

    def range_query(
        self,
        fieldname: Any,
        start: Any,
        end: Any,
        startexcl: bool,
        endexcl: bool,
        boost: float = 1.0,
        node: Any = None,
    ) -> ast.Node:
        self.range_calls.append((fieldname, start, end, startexcl, endexcl, boost, node))
        ref = FieldRef(fieldname) if fieldname is not None else None
        return ast.TermRange(
            field=ref, lo=start, hi=end, incl_lo=not startexcl, incl_hi=not endexcl
        )

    def report(self, diagnostic: Diagnostic) -> None:
        self.reports.append(diagnostic)


# --- Group-node -> AST construction ---------------------------------------


@pytest.mark.parametrize(
    "group_cls, expected",
    [
        pytest.param(
            syntax.AndGroup,
            ast.And(children=(ast.Term(field=None, text="a"), ast.Term(field=None, text="b"))),
            id="andgroup-builds-ast-and",
        ),
        pytest.param(
            syntax.OrGroup,
            ast.Or(children=(ast.Term(field=None, text="a"), ast.Term(field=None, text="b"))),
            id="orgroup-builds-ast-or",
        ),
        pytest.param(
            syntax.DisMaxGroup,
            # whoosh-compat has no dedicated DisjunctionMax AST node, so
            # DisMaxGroup degrades to a plain Or of its children (documented in
            # syntax.py).
            ast.Or(children=(ast.Term(field=None, text="a"), ast.Term(field=None, text="b"))),
            id="dismaxgroup-maps-to-or",
        ),
        pytest.param(
            syntax.AndNotGroup,
            ast.AndNot(
                positive=ast.Term(field=None, text="a"), negative=ast.Term(field=None, text="b")
            ),
            id="andnotgroup-builds-ast-andnot",
        ),
        pytest.param(
            syntax.AndMaybeGroup,
            ast.AndMaybe(
                required=ast.Term(field=None, text="a"), optional=ast.Term(field=None, text="b")
            ),
            id="andmaybegroup-builds-ast-andmaybe",
        ),
        pytest.param(
            syntax.RequireGroup,
            ast.Require(
                scored=ast.Term(field=None, text="a"), filter_only=ast.Term(field=None, text="b")
            ),
            id="requiregroup-builds-ast-require",
        ),
    ],
)
def test_group_builds_ast(group_cls, expected) -> None:
    g = group_cls([syntax.WordNode("a"), syntax.WordNode("b")])
    q = g.query(StubParser())
    assert q == expected


def test_notgroup() -> None:
    g = syntax.NotGroup([syntax.WordNode("a")])
    assert g.query(StubParser()) == ast.Not(ast.Term(field=None, text="a"))


def test_empty_group_is_nothing() -> None:
    assert syntax.AndGroup([]).query(StubParser()) == ast.Nothing()


def test_group_boost_wraps_in_boosted() -> None:
    g = syntax.AndGroup([syntax.WordNode("a"), syntax.WordNode("b")], boost=2.5)
    q = g.query(StubParser())
    assert q == ast.Boosted(
        ast.And(children=(ast.Term(field=None, text="a"), ast.Term(field=None, text="b"))),
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
    assert q == ast.TermRange(
        field=FieldRef("content"),
        lo="a",
        hi="z",
        incl_lo=True,
        incl_hi=False,
        startchar=0,
        endchar=10,
    )


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

    assert q == ast.Term(field=FieldRef("title"), text="dog")


def test_wordnode_passes_tokenize_and_removestops_flags() -> None:
    calls: list[dict[str, Any]] = []

    class RecordingParser(StubParser):
        def term_query(self, fieldname: Any, text: Any, boost: float = 1.0, **kw: Any) -> ast.Node:
            calls.append({"fieldname": fieldname, "text": text, "boost": boost, **kw})
            return ast.Term(field=fieldname, text=text)

    syntax.WordNode("dog").query(RecordingParser())

    assert calls == [
        {
            "fieldname": None,
            "text": "dog",
            "boost": 1.0,
            "tokenize": True,
            "removestops": True,
            "startchar": None,
            "endchar": None,
        }
    ]


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


# --- SyntaxNode base machinery -------------------------------------------


def test_syntaxnode_repr_base_fallback() -> None:
    # SyntaxNode is concrete (not ABC); every shipped node overrides r()
    # and/or __repr__, so the base r() fallback (used if a future subclass
    # forgets to override it) is only reachable by exercising the base
    # class directly.
    node = syntax.SyntaxNode()
    assert node.r() == f"SyntaxNode {node.__dict__!r}"
    assert repr(node) == f"<{node.r()}>"


def test_syntaxnode_repr_includes_fieldname_and_boost() -> None:
    node = syntax.WordNode("dog")
    node.fieldname = "title"
    node.boost = 2.0
    assert repr(node) == "<'title':'dog' ^2.0>"


def test_syntaxnode_repr_omits_boost_when_one() -> None:
    node = syntax.WordNode("dog")
    assert "^" not in repr(node)


def test_syntaxnode_apply_base_returns_self() -> None:
    node = syntax.WordNode("dog")
    assert node.apply(lambda n: syntax.WordNode("cat")) is node


def test_syntaxnode_accept_visits_and_replaces() -> None:
    # accept() runs fn bottom-up; GroupNode.apply() rebuilds a new group
    # from the (possibly replaced) children, so accept() on a group whose
    # child gets replaced returns a new tree containing the replacement.
    inner = syntax.WordNode("a")
    group = syntax.AndGroup([inner])

    def replace_a(n: syntax.SyntaxNode) -> syntax.SyntaxNode:
        if isinstance(n, syntax.WordNode) and n.text == "a":
            return syntax.WordNode("b")
        return n

    result = group.accept(replace_a)
    assert isinstance(result, syntax.AndGroup)
    assert result[0].text == "b"


def test_syntaxnode_set_fieldname_noop_without_fieldname() -> None:
    ws = syntax.Whitespace()
    assert ws.set_fieldname("title") is None


def test_syntaxnode_set_fieldname_keeps_existing_without_override() -> None:
    node = syntax.WordNode("dog")
    node.fieldname = "title"
    node.set_fieldname("body")  # override=False by default
    assert node.fieldname == "title"


def test_syntaxnode_set_fieldname_override_replaces() -> None:
    node = syntax.WordNode("dog")
    node.fieldname = "title"
    node.set_fieldname("body", override=True)
    assert node.fieldname == "body"


def test_syntaxnode_set_boost_noop_without_boost() -> None:
    node = syntax.FieldnameNode("title", "title:")
    assert node.set_boost(2.0) is None


def test_syntaxnode_set_boost() -> None:
    node = syntax.WordNode("dog")
    node.set_boost(3.0)
    assert node.boost == 3.0


def test_syntaxnode_parent_and_siblings() -> None:
    # node_after() has an off-by-one guard (`i < len(nodes) - 2`), so with
    # only two siblings next_sibling() never finds one via this path; use
    # three so both next_sibling()/prev_sibling() see a real neighbor
    # (test_groupnode_node_after_last_element_returns_none pins the bug).
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    c = syntax.WordNode("c")
    group = syntax.AndGroup([a, b, c])
    group.bake(syntax.AndGroup())  # give the group itself a parent chain

    assert a.parent() is group
    assert a.next_sibling() is b
    assert b.prev_sibling() is a
    assert a.prev_sibling() is None
    assert c.next_sibling() is None


def test_syntaxnode_parent_none_when_not_baked() -> None:
    node = syntax.WordNode("a")
    assert node.parent() is None
    assert node.next_sibling() is None
    assert node.prev_sibling() is None


def test_syntaxnode_node_before_after_base_return_none() -> None:
    # The base implementations (used by leaf nodes, which are never
    # themselves a parent) always return None.
    node = syntax.WordNode("a")
    assert node.node_before(node) is None
    assert node.node_after(node) is None


# --- GroupNode ------------------------------------------------------------


def test_groupnode_r_and_startchar_endchar() -> None:
    a = syntax.WordNode("a")
    a.set_range(0, 1)
    b = syntax.WordNode("b")
    b.set_range(2, 3)
    group = syntax.AndGroup([a, b])
    assert group.r() == "AndGroup <None:'a'>, <None:'b'>"
    assert group.startchar == 0
    assert group.endchar == 3


def test_groupnode_startchar_endchar_empty() -> None:
    group = syntax.AndGroup([])
    assert group.startchar is None
    assert group.endchar is None


def test_groupnode_apply_rebuilds_with_boost_and_kwargs() -> None:
    scaled = syntax.OrGroup.factory(scale=2.0)
    group = scaled([syntax.WordNode("a")], boost=1.5)
    rebuilt = group.apply(lambda n: n)
    assert isinstance(rebuilt, scaled)
    assert rebuilt.boost == 1.5
    assert rebuilt is not group


def test_groupnode_query_skips_none_children() -> None:
    # No shipped leaf node returns None from query(), but Wrapper.query()
    # can (when its single child's query() is None), and GroupNode's
    # None-filtering is meant to compose with that. Exercise the filter
    # directly with a stub child so the branch is covered without relying
    # on a specific plugin's grammar.
    class NoneNode(syntax.SyntaxNode):
        def query(self, parser: Any) -> ast.Node | None:
            return None

    group = syntax.AndGroup([NoneNode(), syntax.WordNode("a")])
    q = group.query(StubParser())
    assert q == ast.And(children=(ast.Term(field=None, text="a"),))


def test_groupnode_empty_copy_preserves_fieldname_and_text() -> None:
    class TaggedTextGroup(syntax.GroupNode):
        has_fieldname = True
        has_text = True

        def __init__(self, nodes=None, **kwargs: Any) -> None:
            super().__init__(nodes, **kwargs)
            self.fieldname = None
            self.text = None

    group = TaggedTextGroup([syntax.WordNode("a")])
    group.fieldname = "title"
    group.text = "raw"
    copy = group.empty_copy()
    assert copy.fieldname == "title"
    assert copy.text == "raw"
    assert len(copy) == 0


def test_groupnode_set_range_propagates_to_children() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    group = syntax.AndGroup([a, b])
    group.set_range(5, 9)
    assert a.startchar == 5 and a.endchar == 9
    assert b.startchar == 5 and b.endchar == 9


def test_groupnode_list_protocol() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    c = syntax.WordNode("c")
    group = syntax.AndGroup([a, b])

    assert bool(group) is True
    assert bool(syntax.AndGroup([])) is False
    assert list(iter(group)) == [a, b]
    assert len(group) == 2
    group[0] = c
    assert group[0] is c
    del group[0]
    assert list(group) == [b]
    group.insert(0, a)
    assert list(group) == [a, b]
    group.append(c)
    assert list(group) == [a, b, c]
    group.extend([a])
    assert list(group) == [a, b, c, a]
    assert group.pop() is a
    assert list(group) == [a, b, c]
    group.reverse()
    assert list(group) == [c, b, a]
    assert group.index(b) == 1


def test_groupnode_bake_propagates_to_children() -> None:
    a = syntax.WordNode("a")
    group = syntax.AndGroup([a])
    parent = syntax.AndGroup()
    group.bake(parent)
    assert group.parent() is parent
    assert a.parent() is group


def test_groupnode_node_before_after_not_found_returns_none() -> None:
    a = syntax.WordNode("a")
    group = syntax.AndGroup([a])
    other = syntax.WordNode("z")
    assert group.node_before(other) is None
    assert group.node_after(other) is None


def test_groupnode_node_after_last_element_returns_none() -> None:
    # node_after()'s off-by-one guard (`i < len(nodes) - 2`) means even the
    # second-to-last element reports no next sibling via this path: this
    # locks in that (documented) upstream behavior rather than the "obvious"
    # `i < len(nodes) - 1`.
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    c = syntax.WordNode("c")
    group = syntax.AndGroup([a, b, c])
    assert group.node_after(b) is None
    assert group.node_after(a) is b


# --- BinaryGroup / Wrapper edge branches -----------------------------------


def test_binarygroup_both_none_is_nothing() -> None:
    class NoneNode(syntax.SyntaxNode):
        def query(self, parser: Any) -> ast.Node | None:
            return None

    group = syntax.AndNotGroup([NoneNode(), NoneNode()])
    assert group.query(StubParser()) == ast.Nothing()


def test_binarygroup_left_none_returns_right() -> None:
    class NoneNode(syntax.SyntaxNode):
        def query(self, parser: Any) -> ast.Node | None:
            return None

    group = syntax.AndNotGroup([NoneNode(), syntax.WordNode("b")])
    assert group.query(StubParser()) == ast.Term(field=None, text="b")


def test_binarygroup_right_none_returns_left() -> None:
    class NoneNode(syntax.SyntaxNode):
        def query(self, parser: Any) -> ast.Node | None:
            return None

    group = syntax.AndNotGroup([syntax.WordNode("a"), NoneNode()])
    assert group.query(StubParser()) == ast.Term(field=None, text="a")


def test_wrapper_returns_none_when_child_is_none() -> None:
    class NoneNode(syntax.SyntaxNode):
        def query(self, parser: Any) -> ast.Node | None:
            return None

    group = syntax.NotGroup([NoneNode()])
    assert group.query(StubParser()) is None


def test_wrapper_returns_none_when_it_has_no_child() -> None:
    # A wrapper can be left with nothing to wrap when an operator consumes the
    # text it would have applied to ("NOT AND x"). There is no query to build,
    # so it contributes nothing rather than failing.
    assert syntax.NotGroup([]).query(StubParser()) is None


# --- ErrorNode startchar/endchar without a wrapped node --------------------


def test_errornode_startchar_endchar_without_node() -> None:
    err = syntax.ErrorNode("bad thing")
    assert err.startchar is None
    assert err.endchar is None


# --- OrGroup.factory() ------------------------------------------------------


def test_orgroup_factory_scale_kwarg_survives_construction() -> None:
    scaled_cls = syntax.OrGroup.factory(scale=0.5)
    group = scaled_cls([syntax.WordNode("a")])
    assert group.kwargs.get("scale") == 0.5
    # Passing scale explicitly is popped and replaced by the factory's own.
    group2 = scaled_cls([syntax.WordNode("a")], scale=999)
    assert group2.kwargs.get("scale") == 0.5


# --- TextNode / WordNode r() -------------------------------------------


def test_textnode_r_uses_class_name_and_text() -> None:
    node = syntax.FieldnameNode("title", "title:")  # not a TextNode, sanity
    assert node.original == "title:"


def test_wordnode_r_is_bare_repr() -> None:
    node = syntax.WordNode("dog")
    assert node.r() == repr("dog")


# --- Operator ---------------------------------------------------------


def test_operator_r() -> None:
    op = syntax.InfixOperator("AND", syntax.AndGroup)
    assert op.r() == "OP 'AND'"


def test_infix_operator_at_start_of_group_is_dropped() -> None:
    # position == 0: neither `position > 0` branch applies, so the operator
    # is simply removed.
    b = syntax.WordNode("b")
    group = syntax.AndGroup([syntax.InfixOperator("AND", syntax.AndGroup), b])
    op = group[0]
    op.replace_self(None, group, 0)
    assert list(group) == [b]


def test_infix_operator_at_end_of_group_is_dropped() -> None:
    a = syntax.WordNode("a")
    group = syntax.AndGroup([a, syntax.InfixOperator("AND", syntax.AndGroup)])
    op = group[1]
    op.replace_self(None, group, 1)
    assert list(group) == [a]


def test_infix_operator_merges_into_existing_left_group_when_leftassoc() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    c = syntax.WordNode("c")
    existing = syntax.AndGroup([a, b])
    group = syntax.AndGroup([existing, syntax.InfixOperator("AND", syntax.AndGroup), c])
    op = group[1]
    pos = op.replace_self(None, group, 1)
    assert pos == 1
    assert len(group) == 1
    assert list(group[0]) == [a, b, c]


def test_infix_operator_merges_into_existing_right_group_when_rightassoc() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    c = syntax.WordNode("c")
    existing = syntax.AndGroup([b, c])
    op = syntax.InfixOperator("AND", syntax.AndGroup, leftassoc=False)
    group = syntax.AndGroup([a, op, existing])
    pos = op.replace_self(None, group, 1)
    assert pos == 0
    assert list(group[0]) == [a, b, c]


# --- to_word() --------------------------------------------------------


def test_markernode_r_uses_class_name() -> None:
    class SomeMarker(syntax.MarkerNode):
        pass

    assert SomeMarker().r() == "SomeMarker"


def test_whitespace_r_and_is_ws() -> None:
    ws = syntax.Whitespace()
    assert ws.r() == " "
    assert ws.is_ws() is True
    assert syntax.WordNode("a").is_ws() is False


def test_fieldnamenode_repr() -> None:
    fn = syntax.FieldnameNode("title", "title:")
    assert repr(fn) == "<'title':>"


def test_groupnode_empty_copy_without_boost_or_fieldname() -> None:
    # BinaryGroup has has_boost = False, has_fieldname/has_text = False (the
    # class defaults), so empty_copy() skips both attribute-copy branches.
    group = syntax.AndNotGroup([syntax.WordNode("a"), syntax.WordNode("b")])
    copy = group.empty_copy()
    assert isinstance(copy, syntax.AndNotGroup)
    assert len(copy) == 0


def test_groupnode_set_fieldname_propagates_to_children() -> None:
    a = syntax.WordNode("a")
    b = syntax.WordNode("b")
    group = syntax.AndGroup([a, b])
    group.set_fieldname("title")
    assert a.fieldname == "title"
    assert b.fieldname == "title"


def test_errornode_r() -> None:
    inner = syntax.WordNode("a")
    err = syntax.ErrorNode("bad thing", inner)
    assert err.r() == f"ERR {inner!r} 'bad thing'"


def test_rangenode_r() -> None:
    node = syntax.RangeNode("a", "z", True, False)
    assert node.r() == "{'a' 'z']"
    node2 = syntax.RangeNode("a", "z", False, True)
    assert node2.r() == "['a' 'z'}"


def test_textnode_r() -> None:
    class SomeTextNode(syntax.TextNode):
        pass

    assert SomeTextNode("dog").r() == "SomeTextNode 'dog'"


def test_prefix_operator_at_last_position_is_dropped() -> None:
    # position == length - 1 (no node after the operator): the operator is
    # simply removed rather than wrapping anything.
    a = syntax.WordNode("a")
    group = syntax.AndGroup([a, syntax.PrefixOperator("NOT", syntax.NotGroup)])
    op = group[1]
    op.replace_self(None, group, 1)
    assert list(group) == [a]


def test_postfix_operator_at_first_position_is_dropped() -> None:
    # position == 0 (no node before the operator): the operator is simply
    # removed rather than wrapping anything.
    a = syntax.WordNode("a")
    group = syntax.AndGroup([syntax.PostfixOperator("!", syntax.NotGroup), a])
    op = group[0]
    op.replace_self(None, group, 0)
    assert list(group) == [a]


def test_to_word_converts_fieldnamenode() -> None:
    fn = syntax.FieldnameNode(None, "notafield")
    fn.set_range(0, 9)
    word = syntax.to_word(fn)
    assert isinstance(word, syntax.WordNode)
    assert word.text == "notafield"
    assert word.startchar == 0
    assert word.endchar == 9
