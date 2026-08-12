# Copyright 2011 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

"""Forked from whoosh's ``qparser/syntax.py``.

Where the original built ``whoosh`` query trees, these nodes
build :class:`whoosh_compat.ast.Node` trees instead. Field/kind-specific
decisions (range kind inference, tokenization, term construction) are
delegated to methods on the ``parser`` object passed into ``query()``
(``term_query``, ``range_query``, ``report``, ...), implemented in
:mod:`whoosh_compat.parser.default`. This module only concerns itself with
turning the parsed syntax tree into calls against that API and assembling
the boolean/grouping structure of the resulting AST.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from typing import Any

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.parser.common import attach


class SyntaxNode:
    """Base class for nodes that make up the abstract syntax tree (AST) of a
    parsed user query string. The AST is an intermediate step, generated
    from the query string, then converted into a :class:`whoosh_compat.ast.Node`
    tree by calling the ``query()`` method on the nodes.

    Instances have the following required attributes:

    ``has_fieldname``
        True if this node has a ``fieldname`` attribute.
    ``has_text``
        True if this node has a ``text`` attribute
    ``has_boost``
        True if this node has a ``boost`` attribute.
    ``startchar``
        The character position in the original text at which this node started.
    ``endchar``
        The character position in the original text at which this node ended.
    """

    has_fieldname = False
    has_text = False
    has_boost = False
    fieldname: str | None = None
    startchar: int | None = None
    endchar: int | None = None
    _parent: weakref.ReferenceType[SyntaxNode] | None = None

    def __repr__(self) -> str:
        r = "<"
        if self.has_fieldname:
            r += f"{self.fieldname!r}:"
        r += self.r()
        if self.has_boost and self.boost != 1.0:
            r += f" ^{self.boost}"
        r += ">"
        return r

    def r(self) -> str:
        """Returns a basic representation of this node. The base class's
        ``__repr__`` method calls this, then does the extra busy work of adding
        fieldname and boost where appropriate.
        """

        return f"{self.__class__.__name__} {self.__dict__!r}"

    def apply(self, fn: Callable[[SyntaxNode], SyntaxNode]) -> SyntaxNode:
        return self

    def accept(self, fn: Callable[[SyntaxNode], SyntaxNode]) -> SyntaxNode:
        def fn_wrapper(n: SyntaxNode) -> SyntaxNode:
            return fn(n.apply(fn_wrapper))

        return fn_wrapper(self)

    def query(self, parser: Any) -> ast.Node | None:
        """Returns a :class:`whoosh_compat.ast.Node` instance corresponding to
        this syntax tree node.
        """

        raise NotImplementedError(self.__class__.__name__)

    def is_ws(self) -> bool:
        """Returns True if this node is ignorable whitespace."""

        return False

    def is_text(self) -> bool:
        return False

    def set_fieldname(self, name: str | None, override: bool = False) -> SyntaxNode | None:
        """Sets the fieldname associated with this node. If ``override`` is
        False (the default), the fieldname will only be replaced if this node
        does not already have a fieldname set.

        For nodes that don't have a fieldname, this is a no-op.
        """

        if not self.has_fieldname:
            return None

        if self.fieldname is None or override:
            self.fieldname = name
        return self

    def set_boost(self, boost: float) -> SyntaxNode | None:
        """Sets the boost associated with this node.

        For nodes that don't have a boost, this is a no-op.
        """

        if not self.has_boost:
            return None
        self.boost = boost
        return self

    def set_range(self, startchar: int | None, endchar: int | None) -> SyntaxNode:
        """Sets the character range associated with this node."""

        self.startchar = startchar
        self.endchar = endchar
        return self

    # Navigation methods

    def parent(self) -> SyntaxNode | None:
        if self._parent:
            return self._parent()
        return None

    def next_sibling(self) -> SyntaxNode | None:
        p = self.parent()
        if p is not None:
            return p.node_after(self)
        return None

    def prev_sibling(self) -> SyntaxNode | None:
        p = self.parent()
        if p is not None:
            return p.node_before(self)
        return None

    def node_before(self, n: SyntaxNode) -> SyntaxNode | None:
        return None

    def node_after(self, n: SyntaxNode) -> SyntaxNode | None:
        return None

    def bake(self, parent: SyntaxNode) -> None:
        self._parent = weakref.ref(parent)


class MarkerNode(SyntaxNode):
    """Base class for nodes that only exist to mark places in the tree."""

    def r(self) -> str:
        return self.__class__.__name__


class Whitespace(MarkerNode):
    """Abstract syntax tree node for ignorable whitespace."""

    def r(self) -> str:
        return " "

    def is_ws(self) -> bool:
        return True


class FieldnameNode(SyntaxNode):
    """Abstract syntax tree node for field name assignments."""

    has_fieldname = True

    def __init__(self, fieldname: str | None, original: str) -> None:
        self.fieldname = fieldname
        self.original = original

    def __repr__(self) -> str:
        return f"<{self.fieldname!r}:>"


class GroupNode(SyntaxNode):
    """Base class for abstract syntax tree node types that group together
    sub-nodes.

    Instances have the following attributes:

    ``merging``
        True if side-by-side instances of this group can be merged into a
        single group.

    Subclasses override :meth:`_build` to say how the query nodes returned by
    the subnodes should be combined into a single :class:`whoosh_compat.ast.Node`
    (e.g. ``ast.And(children=subs)``).

    This class implements a number of list methods for operating on the
    subnodes.
    """

    has_boost = True
    merging = True

    def __init__(self, nodes: Sequence[SyntaxNode] | None = None,
                 boost: float = 1.0, **kwargs: Any) -> None:
        self.nodes: list[SyntaxNode] = list(nodes) if nodes else []
        self.boost = boost
        self.kwargs = kwargs

    def r(self) -> str:
        return f"{self.__class__.__name__} {', '.join(repr(n) for n in self.nodes)}"

    @property
    def startchar(self) -> int | None:  # type: ignore[override]
        if not self.nodes:
            return None
        return self.nodes[0].startchar

    @property
    def endchar(self) -> int | None:  # type: ignore[override]
        if not self.nodes:
            return None
        return self.nodes[-1].endchar

    def apply(self, fn: Callable[[SyntaxNode], SyntaxNode]) -> GroupNode:
        return self.__class__([fn(node) for node in self.nodes],
                               boost=self.boost, **self.kwargs)

    def _build(self, subs: tuple[ast.Node, ...]) -> ast.Node:
        """Combine this group's (non-empty) query children into a single
        :class:`whoosh_compat.ast.Node`. Subclasses must override this.
        """

        raise NotImplementedError(self.__class__.__name__)

    def query(self, parser: Any) -> ast.Node | None:
        subs: list[ast.Node] = []
        for node in self.nodes:
            subq = node.query(parser)
            if subq is not None:
                subs.append(subq)

        q: ast.Node
        if not subs:
            q = ast.Nothing()
        else:
            q = self._build(tuple(subs))

        if self.boost != 1.0:
            q = ast.Boosted(q, self.boost)

        return attach(q, self)

    def empty_copy(self) -> GroupNode:
        """Returns an empty copy of this group.

        This is used in the common pattern where a filter creates an new
        group and then adds nodes from the input group to it if they meet
        certain criteria, then returns the new group::

            def remove_whitespace(parser, group):
                newgroup = group.empty_copy()
                for node in group:
                    if not node.is_ws():
                        newgroup.append(node)
                return newgroup
        """

        c = self.__class__(**self.kwargs)
        if self.has_boost:
            c.boost = self.boost
        if self.has_fieldname:
            c.fieldname = self.fieldname  # type: ignore[attr-defined]
        if self.has_text:
            c.text = self.text  # type: ignore[attr-defined]
        return c

    def set_fieldname(self, name: str | None, override: bool = False) -> GroupNode:
        SyntaxNode.set_fieldname(self, name, override=override)
        for node in self.nodes:
            node.set_fieldname(name, override=override)
        return self

    def set_range(self, startchar: int | None, endchar: int | None) -> GroupNode:
        for node in self.nodes:
            node.set_range(startchar, endchar)
        return self

    # List-like methods

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __iter__(self) -> Iterator[SyntaxNode]:
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def __getitem__(self, n: Any) -> Any:
        return self.nodes.__getitem__(n)

    def __setitem__(self, n: Any, v: Any) -> None:
        self.nodes.__setitem__(n, v)

    def __delitem__(self, n: Any) -> None:
        self.nodes.__delitem__(n)

    def insert(self, n: int, v: SyntaxNode) -> None:
        self.nodes.insert(n, v)

    def append(self, v: SyntaxNode) -> None:
        self.nodes.append(v)

    def extend(self, vs: Sequence[SyntaxNode]) -> None:
        self.nodes.extend(vs)

    def pop(self, *args: Any, **kwargs: Any) -> SyntaxNode:
        return self.nodes.pop(*args, **kwargs)

    def reverse(self) -> None:
        self.nodes.reverse()

    def index(self, v: SyntaxNode) -> int:
        return self.nodes.index(v)

    # Navigation methods

    def bake(self, parent: SyntaxNode) -> None:
        SyntaxNode.bake(self, parent)
        for node in self.nodes:
            node.bake(self)

    def node_before(self, n: SyntaxNode) -> SyntaxNode | None:
        try:
            i = self.nodes.index(n)
        except ValueError:
            return None
        if i > 0:
            return self.nodes[i - 1]
        return None

    def node_after(self, n: SyntaxNode) -> SyntaxNode | None:
        try:
            i = self.nodes.index(n)
        except ValueError:
            return None
        if i < len(self.nodes) - 2:
            return self.nodes[i + 1]
        return None


class BinaryGroup(GroupNode):
    """Intermediate base class for group nodes that have exactly two
    subnodes and combine them via a two-argument AST constructor (subclasses
    override :meth:`_build2`).
    """

    merging = False
    has_boost = False

    def _build2(self, qa: ast.Node, qb: ast.Node) -> ast.Node:
        raise NotImplementedError(self.__class__.__name__)

    def query(self, parser: Any) -> ast.Node | None:
        assert len(self.nodes) == 2

        qa = self.nodes[0].query(parser)
        qb = self.nodes[1].query(parser)
        q: ast.Node | None
        if qa is None and qb is None:
            q = ast.Nothing()
        elif qa is None:
            q = qb
        elif qb is None:
            q = qa
        else:
            q = self._build2(qa, qb)

        return attach(q, self)


class Wrapper(GroupNode):
    """Intermediate base class for nodes that wrap a single sub-node
    (subclasses override :meth:`_build1`).
    """

    merging = False

    def _build1(self, q: ast.Node) -> ast.Node:
        raise NotImplementedError(self.__class__.__name__)

    def query(self, parser: Any) -> ast.Node | None:
        # A wrapper can end up with no child at all when a neighbouring
        # operator consumes the text it would have wrapped ("NOT AND x").
        # There is nothing to build from, so contribute nothing.
        if not self.nodes:
            return None
        q = self.nodes[0].query(parser)
        if q is not None:
            return attach(self._build1(q), self)
        return None


class ErrorNode(SyntaxNode):
    def __init__(self, message: str, node: SyntaxNode | None = None) -> None:
        self.message = message
        self.node = node

    def r(self) -> str:
        return f"ERR {self.node!r} {self.message!r}"

    @property
    def startchar(self) -> int | None:  # type: ignore[override]
        return self.node.startchar if self.node is not None else None

    @property
    def endchar(self) -> int | None:  # type: ignore[override]
        return self.node.endchar if self.node is not None else None

    def query(self, parser: Any) -> ast.Node:
        diagnostic = Diagnostic(
            message=self.message,
            kind=DiagnosticKind.UNKNOWN,
            startchar=self.startchar,
            endchar=self.endchar,
        )
        parser.report(diagnostic)
        leaf = ast.ErrorLeaf(diagnostic=diagnostic)
        return attach(leaf, self)


class AndGroup(GroupNode):
    """Syntax node for AND groups; builds :class:`whoosh_compat.ast.And`."""

    def _build(self, subs: tuple[ast.Node, ...]) -> ast.Node:
        return ast.And(children=subs)


class OrGroup(GroupNode):
    """Syntax node for OR groups; builds :class:`whoosh_compat.ast.Or`."""

    def _build(self, subs: tuple[ast.Node, ...]) -> ast.Node:
        return ast.Or(children=subs)

    @classmethod
    def factory(cls, scale: float = 1.0) -> type[OrGroup]:
        """Create an OrGroup subclass.

        The ``scale`` parameter is accepted for compatibility with whoosh
        parsers that supply it (whoosh used scale for scoring), but it is
        silently discarded: the AST's Or node does not carry group-level
        scoring/weighting.
        """
        class ScaledOrGroup(OrGroup):
            def __init__(self, nodes: Sequence[SyntaxNode] | None = None,
                         **kwargs: Any) -> None:
                kwargs.pop("scale", None)
                super().__init__(nodes=nodes, scale=scale, **kwargs)

        return ScaledOrGroup


class DisMaxGroup(GroupNode):
    """Syntax node for DisMax groups.

    whoosh-compat's AST has no dedicated DisjunctionMax node, and none of
    its parser plugins produce ``DisMaxGroup`` by default. The class is
    kept (rather than deleted) so forked plugin/parser code that references
    it continues to work; its ``query()`` degrades to a plain
    :class:`whoosh_compat.ast.Or` of its children.
    """

    def _build(self, subs: tuple[ast.Node, ...]) -> ast.Node:
        return ast.Or(children=subs)


class AndNotGroup(BinaryGroup):
    def _build2(self, qa: ast.Node, qb: ast.Node) -> ast.Node:
        return ast.AndNot(positive=qa, negative=qb)


class AndMaybeGroup(BinaryGroup):
    def _build2(self, qa: ast.Node, qb: ast.Node) -> ast.Node:
        return ast.AndMaybe(required=qa, optional=qb)


class RequireGroup(BinaryGroup):
    def _build2(self, qa: ast.Node, qb: ast.Node) -> ast.Node:
        return ast.Require(scored=qa, filter_only=qb)


class NotGroup(Wrapper):
    def _build1(self, q: ast.Node) -> ast.Node:
        return ast.Not(child=q)


class RangeNode(SyntaxNode):
    """Syntax node for range queries.

    Kind-specific conversion (term/numeric/date range) is not decided here;
    it's delegated to ``parser.range_query()`` (``QueryParser.range_query``,
    :mod:`whoosh_compat.parser.default`), which has access to the schema/field
    spec needed to pick the right :class:`whoosh_compat.ast.Node` subclass.
    """

    has_fieldname = True

    def __init__(self, start: Any, end: Any, startexcl: bool, endexcl: bool) -> None:
        self.start = start
        self.end = end
        self.startexcl = startexcl
        self.endexcl = endexcl
        self.boost = 1.0
        self.fieldname: str | None = None
        self.kwargs: dict[str, Any] = {}

    def r(self) -> str:
        b1 = "{" if self.startexcl else "["
        b2 = "}" if self.endexcl else "]"
        return f"{b1}{self.start!r} {self.end!r}{b2}"

    def query(self, parser: Any) -> ast.Node:
        fieldname = self.fieldname or getattr(parser, "fieldname", None)
        q = parser.range_query(fieldname, self.start, self.end,
                                self.startexcl, self.endexcl,
                                boost=self.boost, node=self)
        return attach(q, self)


class TextNode(SyntaxNode):
    """Intermediate base class for basic nodes that search for text, such as
    term queries, wildcards, prefixes, etc.

    Instances have the following attributes:

    ``tokenize``
        If True and the subclass does not override ``query()``, the node's text
        will be tokenized before constructing the query
    ``removestops``
        If True and the subclass does not override ``query()``, and the field's
        analyzer has a stop word filter, stop words will be removed from the
        text before constructing the query.
    """

    has_fieldname = True
    has_text = True
    has_boost = True
    tokenize = False
    removestops = False

    def __init__(self, text: Any) -> None:
        self.fieldname: str | None = None
        self.text = text
        self.boost = 1.0

    def r(self) -> str:
        return f"{self.__class__.__name__} {self.text!r}"

    def is_text(self) -> bool:
        return True

    def query(self, parser: Any) -> ast.Node:
        fieldname = self.fieldname or getattr(parser, "fieldname", None)
        q = parser.term_query(fieldname, self.text, boost=self.boost,
                               tokenize=self.tokenize,
                               removestops=self.removestops,
                               startchar=self.startchar, endchar=self.endchar)
        return attach(q, self)


class WordNode(TextNode):
    """Syntax node for term queries."""

    tokenize = True
    removestops = True

    def r(self) -> str:
        return repr(self.text)


# Operators

class Operator(SyntaxNode):
    """Base class for PrefixOperator, PostfixOperator, and InfixOperator.

    Operators work by moving the nodes they apply to (e.g. for prefix operator,
    the previous node, for infix operator, the nodes on either side, etc.) into
    a group node. The group provides the code for what to do with the nodes.
    """

    def __init__(self, text: str, grouptype: type[GroupNode],
                 leftassoc: bool = True) -> None:
        """
        :param text: the text of the operator in the query string.
        :param grouptype: the type of group to create in place of the operator
            and the node(s) it operates on.
        :param leftassoc: for infix operators, whether the operator is left
            associative. use ``leftassoc=False`` for right-associative infix
            operators.
        """

        self.text = text
        self.grouptype = grouptype
        self.leftassoc = leftassoc

    def r(self) -> str:
        return f"OP {self.text!r}"

    def replace_self(self, parser: Any, group: GroupNode, position: int) -> int:
        """Called with the parser, a group, and the position at which the
        operator occurs in that group. Should return a group with the operator
        replaced by whatever effect the operator has (e.g. for an infix op,
        replace the op and the nodes on either side with a sub-group).
        """

        raise NotImplementedError


class PrefixOperator(Operator):
    def replace_self(self, parser: Any, group: GroupNode, position: int) -> int:
        length = len(group)
        del group[position]
        if position < length - 1:
            group[position] = self.grouptype([group[position]])
        return position


class PostfixOperator(Operator):
    def replace_self(self, parser: Any, group: GroupNode, position: int) -> int:
        del group[position]
        if position > 0:
            group[position - 1] = self.grouptype([group[position - 1]])
        return position


class InfixOperator(Operator):
    def replace_self(self, parser: Any, group: GroupNode, position: int) -> int:
        la = self.leftassoc
        gtype = self.grouptype
        merging = gtype.merging

        if position > 0 and position < len(group) - 1:
            left = group[position - 1]
            right = group[position + 1]

            # The first two clauses check whether the "strong" side is already
            # a group of the type we are going to create. If it is, we just
            # append the "weak" side to the "strong" side instead of creating
            # a new group inside the existing one. This is necessary because
            # we can quickly run into Python's recursion limit otherwise.
            if merging and la and isinstance(left, gtype):
                left.append(right)
                del group[position:position + 2]
            elif merging and not la and isinstance(right, gtype):
                right.insert(0, left)
                del group[position - 1:position + 1]
                return position - 1
            else:
                # Replace the operator and the two surrounding objects
                group[position - 1:position + 2] = [gtype([left, right])]
        else:
            del group[position]

        return position


# Functions

def to_word(n: FieldnameNode) -> WordNode:
    node = WordNode(n.original)
    node.startchar = n.startchar
    node.endchar = n.endchar
    return node
