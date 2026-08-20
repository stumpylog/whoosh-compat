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

"""Forked from whoosh's ``qparser/plugins.py``, trimmed to the plugin subset
this library supports (see the README's syntax table for what's in and out)
and adapted so node ``query()`` methods build :class:`whoosh_compat.ast.Node`
trees: either directly (:class:`EveryPlugin`) or by calling hooks on the
``parser`` object (``term_query``, ``wildcard_query``, ``prefix_query``,
``range_query``, implemented in :mod:`whoosh_compat.parser.default`).

Two extensions beyond stock whoosh live here:

* :class:`CommaValuesPlugin`: splits ``field:a,b,c`` into an ``AndGroup`` of
  single-valued terms for fields whose :class:`~whoosh_compat.fields.FieldSpec`
  has ``comma_values=True``.
* :class:`FieldsPlugin` accepts dotted field names (``notes.user:foo``) and
  only treats them as known fields when ``parser.registry.make_ref(name)``
  resolves them (either as a plain/aliased field or a registered JSON
  subpath); otherwise the ``field:`` text is demoted and merged back into
  the following node exactly as whoosh does for unknown fields.
"""

from __future__ import annotations

import copy
from re import Match
from typing import Any

from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.parser import priorities
from whoosh_compat.parser import syntax
from whoosh_compat.parser.common import attach
from whoosh_compat.parser.taggers import FnTagger
from whoosh_compat.parser.taggers import RegexTagger
from whoosh_compat.parser.taggers import Tagger
from whoosh_compat.parser.text import rcompile

TaggerEntry = tuple[Tagger, int]
FilterFn = Any
FilterEntry = tuple[FilterFn, int]

# Maximum depth of tree hierarchy this parser will materialize. Two stages
# construct hierarchy, and both enforce this cap: GroupPlugin.do_groups (one
# level per unclosed "(") and OperatorsPlugin.do_operators (one level per
# non-merging operator -- ANDNOT/ANDMAYBE/REQUIRE/NOT -- since
# Operator.replace_self() builds a new group for each, while merging groups
# like AND/OR absorb their neighbour into one flat group and so add no depth
# at all). Not a whoosh concept: whoosh (and this parser's own
# tag/filter pipeline, and ast.normalize() before it became iterative) all
# eventually RecursionError on pathologically deep nesting, just at
# interpreter-recursion-limit-dependent depths nobody chose on purpose
# (confirmed directly against both the pinned real-whoosh oracle and this
# parser without this cap: both start RecursionError-ing between depth 950
# and 1000 at the default recursion limit of 1000). 200 leaves close to a
# 5x margin under that floor for every other recursive filter this nested
# structure still passes through (do_wildcards, do_boost, do_fieldnames,
# do_operators, do_multifield, do_aliases, do_comma_values, and finally
# GroupNode.query()), while comfortably exceeding any nesting a real query
# would ever use. See DIVERGENCES.md entry 31 and ARCHITECTURE.md's
# "parsing never raises" invariant.
_MAX_GROUP_NESTING_DEPTH = 200


def _too_deep_node(node: syntax.SyntaxNode | None) -> syntax.ErrorNode:
    """The single leaf that stands in for a region of the query that would
    have exceeded ``_MAX_GROUP_NESTING_DEPTH``. ``node`` is only used to
    position the diagnostic.
    """

    message = (
        "query nesting exceeds the maximum supported depth "
        f"({_MAX_GROUP_NESTING_DEPTH})"
    )
    return syntax.ErrorNode(message, kind=DiagnosticKind.TOO_DEEP, node=node)


class Plugin:
    """Base class for parser plugins."""

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        """Should return a list of ``(Tagger, priority)`` tuples to add to the
        syntax the parser understands. Lower priorities run first.
        """

        return []

    def filters(self, parser: Any) -> list[FilterEntry]:
        """Should return a list of ``(filter_function, priority)`` tuples to
        add to parser. Lower priority numbers run first.

        Filter functions will be called with ``(parser, groupnode)`` and
        should return a group node.
        """

        return []


class TaggingPlugin(Plugin, RegexTagger):
    """A plugin that also acts as a Tagger, to avoid having an extra Tagger
    class for simple cases.

    A TaggingPlugin object should have a ``priority`` attribute and either a
    ``nodetype`` attribute or a ``create()`` method. If the subclass doesn't
    override ``create()``, the base class will call ``self.nodetype`` with the
    Match object's named groups as keyword arguments.
    """

    priority = 0
    nodetype: type[syntax.SyntaxNode]

    def __init__(self, expr: str | None = None) -> None:
        RegexTagger.__init__(self, expr or self.expr)

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        return [(self, self.priority)]

    def filters(self, parser: Any) -> list[FilterEntry]:
        return []

    def create(self, parser: Any, match: Match[str]) -> syntax.SyntaxNode:
        kwargs = {str(k): v for k, v in match.groupdict().items()}
        return self.nodetype(**kwargs)


class WhitespacePlugin(TaggingPlugin):
    """Tags whitespace and removes it at priority 500. Depending on whether
    your plugin's filter wants to see where whitespace was in the original
    query, it should run with priority lower than 500 (before removal of
    whitespace) or higher than 500 (after removal of whitespace).
    """

    nodetype = syntax.Whitespace
    priority = priorities.TAG_WHITESPACE

    def __init__(self, expr: str = r"\s+") -> None:
        TaggingPlugin.__init__(self, expr)

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.remove_whitespace, priorities.FILTER_WHITESPACE_REMOVE)]

    def remove_whitespace(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        newgroup = group.empty_copy()
        for node in group:
            if isinstance(node, syntax.GroupNode):
                newgroup.append(self.remove_whitespace(parser, node))
            elif not node.is_ws():
                newgroup.append(node)
        return newgroup


class SingleQuotePlugin(TaggingPlugin):
    """Adds the ability to specify single "terms" containing spaces by
    enclosing them in single quotes.

    Words produced by this tagger carry ``is_quoted = True`` so that
    :class:`CommaValuesPlugin` knows not to split their text on commas.
    """

    class QuotedWordNode(syntax.WordNode):
        is_quoted = True

    expr = rcompile(r"(^|(?<=\W))'(?P<text>.*?)'(?=\s|\]|[)}]|$)")
    nodetype = QuotedWordNode


class WildcardPlugin(TaggingPlugin):
    # ՞ = Armenian question mark
    # ؟ = Arabic question mark
    # ፧ = Ethiopic question mark
    qmarks = "?՞؟፧"
    expr = rcompile(f"(?P<text>[*{qmarks}])")

    class PrefixNode(syntax.TextNode):
        """Produced by rewriting a trailing-star-only wildcard. Its
        ``query()`` calls ``parser.prefix_query`` rather than the inherited
        ``term_query``: ``QueryParser.prefix_query``
        (:mod:`whoosh_compat.parser.default`) turns this into
        :class:`whoosh_compat.ast.Prefix`.
        """

        def r(self) -> str:
            return f"{self.text!r}*"

        def query(self, parser: Any) -> ast.Node:
            fieldname = self.fieldname or getattr(parser, "fieldname", None)
            q = parser.prefix_query(fieldname, self.text, boost=self.boost,
                                     startchar=self.startchar, endchar=self.endchar)
            return attach(q, self)

    class WildcardNode(syntax.TextNode):
        # Note that this node inherits tokenize = False from TextNode,
        # so the text in this node will not be analyzed... just passed
        # straight to the query.
        #
        # ``query()`` calls ``parser.wildcard_query``
        # (``QueryParser.wildcard_query``, ``parser/default.py``), which is
        # responsible for the no-wildcard-chars -> Term, "*" alone ->
        # Every, and else -> Wildcard rewrites.

        def r(self) -> str:
            return f"Wild {self.text!r}"

        def query(self, parser: Any) -> ast.Node:
            fieldname = self.fieldname or getattr(parser, "fieldname", None)
            q = parser.wildcard_query(fieldname, self.text, boost=self.boost,
                                       startchar=self.startchar, endchar=self.endchar)
            return attach(q, self)

    nodetype = WildcardNode

    def filters(self, parser: Any) -> list[FilterEntry]:
        # Run early, but definitely before multifield plugin
        return [(self.do_wildcards, priorities.FILTER_WILDCARDS)]

    def do_wildcards(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        i = 0
        while i < len(group):
            node = group[i]
            if isinstance(node, self.WildcardNode):
                if i < len(group) - 1 and group[i + 1].is_text():
                    nextnode = group.pop(i + 1)
                    node.text += nextnode.text  # type: ignore[attr-defined]
                if i > 0 and group[i - 1].is_text():
                    prevnode = group.pop(i - 1)
                    node.text = prevnode.text + node.text  # type: ignore[attr-defined]
                else:
                    i += 1
            else:
                if isinstance(node, syntax.GroupNode):
                    self.do_wildcards(parser, node)
                i += 1

        for i in range(len(group)):
            node = group[i]
            if isinstance(node, self.WildcardNode):
                text = node.text
                # "[" blocks the fold alongside the question marks: a
                # PrefixNode's text is a *literal*, so folding "202[0-3]*"
                # into Prefix("202[0-3]") would silently reinterpret the
                # bracket character class as ordinary characters (real
                # whoosh does exactly that: see paperless-ngx#13568 and
                # ``QueryParser.wildcard_query``'s _TRAILING_STAR_RE).
                # Nested rather than combined with `and`: mirrors whoosh's
                # original two-step condition/action shape for this fold.
                if (  # noqa: SIM102 (preserves whoosh control-flow fidelity)
                    len(text) > 1
                    and "[" not in text
                    and not any(qm in text for qm in self.qmarks)
                ):
                    if text.find("*") == len(text) - 1:
                        newnode = self.PrefixNode(text[:-1])
                        newnode.startchar = node.startchar
                        newnode.endchar = node.endchar
                        group[i] = newnode
        return group


class BoostPlugin(TaggingPlugin):
    """Adds the ability to boost clauses of the query using the circumflex."""

    expr = rcompile("\\^(?P<boost>[0-9]*(\\.[0-9]+)?)($|(?=[ \t\r\n)]))")

    class BoostNode(syntax.SyntaxNode):
        def __init__(self, original: str, boost: float) -> None:
            self.original = original
            self.boost = boost

        def r(self) -> str:
            return f"^ {self.boost}"

    def create(self, parser: Any, match: Match[str]) -> syntax.SyntaxNode:
        # Override create so we can grab group 0
        original = match.group(0)
        try:
            boost = float(match.group("boost"))
        except ValueError:
            # The text after the ^ wasn't a valid number, so turn it into a
            # word
            node: syntax.SyntaxNode = syntax.WordNode(original)
        else:
            node = self.BoostNode(original, boost)

        return node

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.clean_boost, priorities.FILTER_BOOSTS_PRE),
                (self.do_boost, priorities.FILTER_BOOSTS_POST)]

    def clean_boost(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """This filter finds any BoostNodes in positions where they can't boost
        the previous node (e.g. at the very beginning, after whitespace, or
        after another BoostNode) and turns them into WordNodes.
        """

        bnode = self.BoostNode
        for i, node in enumerate(group):
            if isinstance(node, bnode):  # noqa: SIM102 (preserves whoosh control-flow fidelity)
                if not i or not group[i - 1].has_boost:
                    group[i] = syntax.to_word(node)  # type: ignore[arg-type]
        return group

    def do_boost(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """This filter finds BoostNodes and applies the boost to the previous
        node.
        """

        newgroup = group.empty_copy()
        for node in group:
            if isinstance(node, syntax.GroupNode):
                node = self.do_boost(parser, node)
            elif isinstance(node, self.BoostNode):
                if newgroup and newgroup[-1].has_boost:
                    # Apply the BoostNode's boost to the previous node
                    newgroup[-1].set_boost(node.boost)
                    # Skip adding the BoostNode to the new group
                    continue
                else:
                    node = syntax.to_word(node)  # type: ignore[arg-type]
            newgroup.append(node)
        return newgroup


class GroupPlugin(Plugin):
    """Adds the ability to group clauses using parentheses."""

    # Marker nodes for open and close bracket

    class OpenBracket(syntax.SyntaxNode):
        def r(self) -> str:
            return "("

    class CloseBracket(syntax.SyntaxNode):
        def r(self) -> str:
            return ")"

    def __init__(self, openexpr: str = "[(]", closeexpr: str = "[)]") -> None:
        self.openexpr = openexpr
        self.closeexpr = closeexpr

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        return [(FnTagger(self.openexpr, self.OpenBracket, "openB"), priorities.TAG_DEFAULT),
                (FnTagger(self.closeexpr, self.CloseBracket, "closeB"), priorities.TAG_DEFAULT)]

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.do_groups, priorities.FILTER_GROUPS)]

    def do_groups(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """This filter finds open and close bracket markers in a flat group
        and uses them to organize the nodes into a hierarchy.

        This is the one place in the whole tag/filter/query pipeline that
        establishes parenthesization depth (every other filter, plus
        ``GroupNode.query()`` afterwards, just recurses into whatever
        hierarchy already exists), so it's also the cheapest and earliest
        place to cap it: once ``stack`` would grow past
        ``_MAX_GROUP_NESTING_DEPTH``, further ``(``/``)`` pairs are treated
        as an opaque, uncounted overflow region (tracked by
        ``overflow_depth`` instead of pushing/popping ``stack``) and
        collapsed into a single diagnostic leaf instead of being
        materialized into real hierarchy that every later stage would have
        to recurse through.
        """

        ob, cb = self.OpenBracket, self.CloseBracket
        # Group hierarchy stack
        stack = [parser.group()]
        # Depth of unclosed "(" seen once at/past the cap, and the bracket
        # that first tipped it over (kept only for diagnostic positioning).
        overflow_depth = 0
        overflow_start: syntax.SyntaxNode | None = None
        for node in group:
            if isinstance(node, ob):
                if overflow_depth or len(stack) >= _MAX_GROUP_NESTING_DEPTH:
                    if overflow_depth == 0:
                        overflow_start = node
                    overflow_depth += 1
                    continue
                # Open bracket: push a new level of hierarchy on the stack
                stack.append(parser.group())
            elif isinstance(node, cb):
                if overflow_depth:
                    overflow_depth -= 1
                    if overflow_depth == 0:
                        stack[-1].append(_too_deep_node(overflow_start))
                        overflow_start = None
                    continue
                # Close bracket: pop the current level of hierarchy and append
                # it to the previous level
                if len(stack) > 1:
                    last = stack.pop()
                    stack[-1].append(last)
            elif overflow_depth:
                # Inside an overflow region: discard content, it will never
                # be part of the materialized tree.
                continue
            else:
                # Anything else: add it to the current level of hierarchy
                stack[-1].append(node)

        if overflow_depth:
            # Unbalanced parens inside the overflow region (more opens than
            # closes): still surface the diagnostic rather than silently
            # dropping the whole thing.
            stack[-1].append(_too_deep_node(overflow_start))

        top = stack[0]
        # If the parens were unbalanced (more opens than closes), just take
        # whatever levels of hierarchy were left on the stack and tack them on
        # the end of the top-level
        if len(stack) > 1:
            for ls in stack[1:]:
                top.extend(ls)

        if len(top) == 1 and isinstance(top[0], syntax.GroupNode):
            boost = top.boost
            top = top[0]
            top.boost = boost

        return top

class EveryPlugin(TaggingPlugin):
    expr = rcompile("[*]:[*]")
    priority = priorities.TAG_EVERY

    def create(self, parser: Any, match: Match[str]) -> syntax.SyntaxNode:
        return self.EveryNode()

    class EveryNode(syntax.SyntaxNode):
        def r(self) -> str:
            return "*:*"

        def query(self, parser: Any) -> ast.Node:
            return attach(ast.Every(field=None), self)


class FieldsPlugin(Plugin):
    """Adds the ability to specify the field of a clause.

    The fieldname expression accepts dotted names (``notes.user:``) so JSON
    subpaths can be addressed directly; whether a dotted (or plain) name is
    actually *known* is decided in :meth:`do_fieldnames` via
    ``parser.registry``.

    Note: unlike most other ``*Plugin`` classes here, this one does not
    inherit :class:`TaggingPlugin`: it builds its own
    :class:`FieldnameTagger` and fully overrides both ``taggers()`` and
    ``filters()``, so there's no shared regex-compiling behavior to reuse.
    """

    class FieldnameTagger(RegexTagger):
        def create(self, parser: Any, match: Match[str]) -> syntax.FieldnameNode:
            return syntax.FieldnameNode(match.group("text"), match.group(0))

    def __init__(self, expr: str = r"(?P<text>[\w.]+|[*]):",
                 remove_unknown: bool = True) -> None:
        """
        :param expr: the regular expression to use for tagging fields.
        :param remove_unknown: if True, converts field specifications for
            fields that aren't in the registry into regular text.
        """

        self.expr = expr
        self.removeunknown = remove_unknown

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        return [(self.FieldnameTagger(self.expr), priorities.TAG_DEFAULT)]

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.do_fieldnames, priorities.FILTER_FIELDNAMES)]

    @staticmethod
    def _fieldname_is_recognized(
        registry: Any, node: syntax.FieldnameNode, group: syntax.GroupNode, i: int
    ) -> bool:
        """Whether ``node`` (at index ``i`` in ``group``) should count as a
        recognized field prefix rather than being demoted to text.

        A plain registry hit (``node.fieldname in registry``) always
        counts. Otherwise, a bare JSON field name (the demotion
        target) gets one narrow carve-out: a lone, unquoted ``*``
        immediately after it is not a term/pattern to demote, it's the
        existence-check special case that
        ``QueryParser.wildcard_query`` already turns into
        :class:`~whoosh_compat.ast.Every` for a *recognized* field
        (``text == "*"``, the same check ``WildcardPlugin`` and
        ``QueryParser.term_query`` use elsewhere for U64/BOOLEAN_EXISTS).
        Reusing that exact node/text shape here, rather than inventing a
        separate detector, is what keeps this carve-out limited to the
        genuine bare-star case and nothing a demoted text search would
        otherwise swallow (e.g. a quoted ``"*"``, or ``*`` followed by more
        text, stay demoted).
        """

        if node.fieldname in registry:
            return True
        if not registry.is_bare_json_field(node.fieldname):
            return False
        if i + 1 >= len(group):
            return False
        nextnode = group[i + 1]
        return isinstance(nextnode, WildcardPlugin.WildcardNode) and nextnode.text == "*"

    def do_fieldnames(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """This filter finds FieldnameNodes in the tree and applies their
        fieldname to the next node.
        """

        fnclass = syntax.FieldnameNode
        registry = getattr(parser, "registry", None)

        if self.removeunknown and registry is not None:
            # Look for field nodes that aren't in the registry (as a plain
            # name, alias, or valid dotted JSON path, or the bare-JSON
            # existence carve-out above) and convert them to text
            newgroup = group.empty_copy()
            prev_field_node = None

            for i, node in enumerate(group):
                if isinstance(node, fnclass) and not self._fieldname_is_recognized(
                    registry, node, group, i
                ):
                    prev_field_node = node
                    continue
                elif prev_field_node:
                    # If prev_field_node is not None, it contains a field node
                    # that appeared before this node but isn't in the
                    # registry, so we'll convert it to text here
                    if node.has_text:
                        node.text = prev_field_node.original + node.text  # type: ignore[attr-defined]
                    else:
                        newgroup.append(syntax.to_word(prev_field_node))
                    prev_field_node = None
                newgroup.append(node)
            if prev_field_node:
                newgroup.append(syntax.to_word(prev_field_node))
            group = newgroup

        newgroup = group.empty_copy()
        # Iterate backwards through the stream, looking for field-able objects
        # with field nodes in front of them
        i = len(group)
        while i > 0:
            i -= 1
            node = group[i]
            if isinstance(node, fnclass):
                # If we see a fieldname node, it must not have been in front
                # of something fieldable, since we would have already removed
                # it (since we're iterating backwards), so convert it to text
                node = syntax.to_word(node)
            elif isinstance(node, syntax.GroupNode):
                node = self.do_fieldnames(parser, node)

            if i > 0 and not node.is_ws() and isinstance(group[i - 1], fnclass):
                node.set_fieldname(group[i - 1].fieldname, override=False)  # type: ignore[union-attr]
                i -= 1

            newgroup.append(node)
        newgroup.reverse()
        return newgroup


class PhrasePlugin(Plugin):
    """Adds the ability to specify phrase queries inside double quotes."""

    class PhraseNode(syntax.TextNode):
        def __init__(self, text: str, textstartchar: int, slop: int = 1) -> None:
            syntax.TextNode.__init__(self, text)
            self.textstartchar = textstartchar
            self.slop = slop

        def r(self) -> str:
            return f"{self.__class__.__name__} {self.text!r}~{self.slop}"

        def query(self, parser: Any) -> ast.Node:
            fieldname = self.fieldname or getattr(parser, "fieldname", None)
            ref = parser.field_ref(fieldname)
            registry = getattr(parser, "registry", None)
            resolved = registry.resolve(ref) if ref is not None and registry is not None else None
            if (
                resolved is not None
                and resolved.spec.kind is FieldKind.U64
                and self.text != "*"
            ):
                # A double-quoted value on a U64 field still needs the same
                # parse-time domain diagnosis the bare/single-quoted/range
                # spellings get: without this, a value
                # that reaches here as a Phrase (raw text, never coerced)
                # sailed through with zero diagnostics and only failed at
                # emit time. ("*" is exempt: it is an existence-match
                # special case handled entirely at emit time.)
                assert ref is not None  # resolve() only returns non-None for a resolved ref
                _, err = parser._parse_u64(self.text, ref, self.startchar, self.endchar)
                if err is not None:
                    return err
            # Analysis is emit-time: just hand the raw phrase text and slop
            # off to the AST; whatever backend consumes the AST is
            # responsible for tokenizing it.
            q: ast.Node = ast.Phrase(field=ref, text=self.text, slop=self.slop)
            if self.boost != 1.0:
                q = ast.Boosted(q, self.boost)
            return attach(q, self)

    class PhraseTagger(RegexTagger):
        def create(self, parser: Any, match: Match[str]) -> PhrasePlugin.PhraseNode:
            text = match.group("text")
            textstartchar = match.start("text")
            slopstr = match.group("slop")
            slop = int(slopstr) if slopstr else 1
            return PhrasePlugin.PhraseNode(text, textstartchar, slop)

    def __init__(self, expr: str = '"(?P<text>.*?)"(~(?P<slop>[1-9][0-9]*))?') -> None:
        self.expr = expr

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        return [(self.PhraseTagger(self.expr), priorities.TAG_DEFAULT)]


class RangePlugin(Plugin):
    """Adds the ability to specify term ranges."""

    expr = rcompile(r"""
    (?P<open>\{|\[)               # Open paren
    (?P<start>
        ('[^']*?'\s+)             # single-quoted
        |                         # or
        ([^\]}]+?(?=[Tt][Oo]))    # everything until "to"
    )?
    [Tt][Oo]                      # "to"
    (?P<end>
        (\s+'[^']*?')             # single-quoted
        |                         # or
        ([^\]}]+?)                # everything until "]" or "}"
    )?
    (?P<close>}|])                # Close paren
    """, verbose=True)

    class RangeTagger(RegexTagger):
        def __init__(self, expr: Any, excl_start: str, excl_end: str) -> None:
            self.expr = expr
            self.excl_start = excl_start
            self.excl_end = excl_end

        def create(self, parser: Any, match: Match[str]) -> syntax.RangeNode:
            start = match.group("start")
            end = match.group("end")
            if start:
                # Strip the space before the "to"
                start = start.rstrip()
                # Strip single quotes
                if start.startswith("'") and start.endswith("'"):
                    start = start[1:-1]
            if end:
                # Strip the space before the "to"
                end = end.lstrip()
                # Strip single quotes
                if end.startswith("'") and end.endswith("'"):
                    end = end[1:-1]
            # What kind of open and close brackets were used?
            startexcl = match.group("open") == self.excl_start
            endexcl = match.group("close") == self.excl_end

            return syntax.RangeNode(start, end, startexcl, endexcl)

    def __init__(self, expr: Any = None, excl_start: str = "{", excl_end: str = "}") -> None:
        self.expr = expr or self.expr
        self.excl_start = excl_start
        self.excl_end = excl_end

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        tagger = self.RangeTagger(self.expr, self.excl_start, self.excl_end)
        return [(tagger, priorities.TAG_RANGE)]


class OperatorsPlugin(Plugin):
    """By default, adds the AND, OR, ANDNOT, ANDMAYBE, and NOT operators to
    the parser syntax. This plugin scans the token stream for subclasses of
    :class:`~whoosh_compat.parser.syntax.Operator` and calls their
    ``make_group``/``replace_self`` methods to allow them to manipulate the
    stream.
    """

    class OpTagger(RegexTagger):
        def __init__(self, expr: Any, grouptype: type[syntax.GroupNode],
                     optype: type[syntax.Operator] = syntax.InfixOperator,
                     leftassoc: bool = True, memo: str = "") -> None:
            RegexTagger.__init__(self, expr)
            self.grouptype = grouptype
            self.optype = optype
            self.leftassoc = leftassoc
            self.memo = memo

        def __repr__(self) -> str:
            return f"<{self.__class__.__name__} {self.expr.pattern!r} ({self.memo})>"

        def create(self, parser: Any, match: Match[str]) -> syntax.Operator:
            return self.optype(match.group(0), self.grouptype, self.leftassoc)

    OpTaggerEntry = tuple["OperatorsPlugin.OpTagger", int]

    def __init__(self, ops: list[OpTaggerEntry] | None = None, clean: bool = False,
                 And: str | None = r"(?<=\s)AND(?=\s)",
                 Or: str | None = r"(?<=\s)OR(?=\s)",
                 AndNot: str | None = r"(?<=\s)ANDNOT(?=\s)",
                 AndMaybe: str | None = r"(?<=\s)ANDMAYBE(?=\s)",
                 Not: str | None = r"(^|(?<=(\s|[()])))NOT(?=\s)",
                 Require: str | None = r"(^|(?<=\s))REQUIRE(?=\s)") -> None:
        ops = list(ops) if ops else []

        if not clean:
            ot = self.OpTagger
            if Not:
                ops.append((ot(Not, syntax.NotGroup, syntax.PrefixOperator,
                                memo="not"), priorities.TAG_OPERATOR))
            if And:
                ops.append((ot(And, syntax.AndGroup, memo="and"), priorities.TAG_OPERATOR))
            if Or:
                ops.append((ot(Or, syntax.OrGroup, memo="or"), priorities.TAG_OPERATOR))
            if AndNot:
                ops.append((ot(AndNot, syntax.AndNotGroup, memo="anot"),
                            priorities.TAG_OPERATOR_ANDNOT))
            if AndMaybe:
                ops.append((ot(AndMaybe, syntax.AndMaybeGroup, memo="amaybe"),
                            priorities.TAG_OPERATOR_ANDNOT))
            if Require:
                ops.append((ot(Require, syntax.RequireGroup, memo="req"),
                            priorities.TAG_OPERATOR))

        self.ops: list[OperatorsPlugin.OpTaggerEntry] = ops

    def taggers(self, parser: Any) -> list[TaggerEntry]:
        return self.ops  # type: ignore[return-value]

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.do_operators, priorities.FILTER_OPERATORS)]

    def do_operators(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """This filter finds PrefixOperator, PostfixOperator, and InfixOperator
        nodes in the tree and calls their logic to rearrange the nodes.

        The descent into subgroups uses an explicit stack rather than
        recursion, so that hierarchy arriving from any source (deeply nested
        parens, or the groups ``_apply_operators`` itself builds) can't
        exhaust the interpreter's frames.
        """

        top = self._apply_operators(parser, group)
        stack = [top]
        while stack:
            g = stack.pop()
            for i, t in enumerate(g):
                if isinstance(t, syntax.GroupNode):
                    g[i] = self._apply_operators(parser, t)
                    stack.append(g[i])

        return top

    def _apply_operators(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """Rearrange the operator nodes of a single group (not its subgroups)."""

        # Each non-merging infix operator becomes one more level of hierarchy
        # in replace_self() below ("a ANDNOT b ANDNOT c" -> ((a ANDNOT b)
        # ANDNOT c)), so a flat, paren-free chain of them builds depth that
        # GroupPlugin.do_groups' bracket stack never saw. Cap it here the same
        # way, by collapsing the whole group to one diagnostic leaf instead of
        # materializing hierarchy every later filter would recurse through.
        # Merging operators (AND/OR) build a single flat group no matter how
        # long the chain, and prefix/postfix operators wrap one node each
        # without nesting, so neither is counted.
        nonmerging = sum(
            1
            for t in group
            if isinstance(t, syntax.InfixOperator) and not t.grouptype.merging
        )
        if nonmerging >= _MAX_GROUP_NESTING_DEPTH:
            return parser.group([_too_deep_node(next(iter(group), None))])

        for tagger, _ in self.ops:
            # Get the operators created by the configured taggers
            optype = tagger.optype
            gtype = tagger.grouptype

            # Left-associative infix operators are replaced left-to-right, and
            # right-associative infix operators are replaced right-to-left.
            # Most of the work is done in the different implementations of
            # Operator.replace_self().
            if tagger.leftassoc:
                i = 0
                while i < len(group):
                    t = group[i]
                    if isinstance(t, optype) and t.grouptype is gtype:
                        i = t.replace_self(parser, group, i)
                    else:
                        i += 1
            else:
                i = len(group) - 1
                while i >= 0:
                    t = group[i]
                    if isinstance(t, optype):
                        i = t.replace_self(parser, group, i)
                    i -= 1

        return group


class MultifieldPlugin(Plugin):
    """Converts any unfielded terms into OR clauses that search for the
    term in a specified list of fields.
    """

    def __init__(self, fieldnames: list[str], fieldboosts: dict[str, float] | None = None,
                 group: type[syntax.GroupNode] = syntax.OrGroup) -> None:
        """
        :param fieldnames: a list of fields to search.
        :param fieldboosts: an optional dictionary mapping field names to
            a boost to use for that field.
        :param group: the group to use to relate the fielded terms to each
            other.
        """

        self.fieldnames = fieldnames
        self.boosts = fieldboosts or {}
        self.group = group

    def filters(self, parser: Any) -> list[FilterEntry]:
        # Run after the fields filter applies explicit fieldnames (at
        # priority 100)
        return [(self.do_multifield, priorities.FILTER_MULTIFIELD)]

    def do_multifield(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                # Recurse inside groups
                group[i] = self.do_multifield(parser, node)
            elif node.has_fieldname and node.fieldname is None:
                # For an unfielded node, create a new group containing fielded
                # versions of the node for each configured "multi" field.
                newnodes = []
                for fname in self.fieldnames:
                    newnode = copy.copy(node)
                    newnode.set_fieldname(fname)
                    newnode.set_boost(self.boosts.get(fname, 1.0))
                    newnodes.append(newnode)
                group[i] = self.group(newnodes)
        return group


class FieldAliasPlugin(Plugin):
    """Adds the ability to use "aliases" of fields in the query string.

    >>> # Allow users to use 'body' or 'text' to refer to the 'content' field
    >>> parser.add_plugin(FieldAliasPlugin({"content": ["body", "text"]}))
    >>> parser.parse("text:hello")
    Term("content", "hello")

    In whoosh-compat, this is normally constructed from the field registry::

        FieldAliasPlugin({spec.name: spec.aliases for spec in registry if spec.aliases})
    """

    def __init__(self, fieldmap: dict[str, tuple[str, ...] | list[str]]) -> None:
        self.fieldmap = fieldmap
        self.reverse: dict[str, str] = {}
        for key, values in fieldmap.items():
            for value in values:
                self.reverse[value] = key

    def filters(self, parser: Any) -> list[FilterEntry]:
        # Run before fields plugin at 100
        return [(self.do_aliases, priorities.FILTER_ALIASES)]

    def do_aliases(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                group[i] = self.do_aliases(parser, node)
            elif node.has_fieldname and node.fieldname is not None:
                fname = node.fieldname
                if fname in self.reverse:
                    node.set_fieldname(self.reverse[fname], override=True)
        return group


class CommaValuesPlugin(Plugin):
    """Splits comma-separated values in a fielded term into an ``AndGroup``
    of single-valued terms, for fields whose
    :class:`~whoosh_compat.fields.FieldSpec` has ``comma_values=True``.

    ``tag:foo,bar`` becomes the equivalent of ``tag:foo AND tag:bar``. Quoted
    text (produced by :class:`SingleQuotePlugin`, which marks its nodes with
    ``is_quoted = True``) is exempt: ``tag:'foo,bar'`` stays a single term.

    Runs after :class:`FieldsPlugin` (100) applies fieldnames to nodes but
    before :class:`MultifieldPlugin` (110) fans out unfielded terms.
    """

    def filters(self, parser: Any) -> list[FilterEntry]:
        return [(self.do_comma_values, priorities.FILTER_COMMA_VALUES)]

    def do_comma_values(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        registry = getattr(parser, "registry", None)
        newgroup = group.empty_copy()

        for node in group:
            if isinstance(node, syntax.GroupNode):
                newgroup.append(self.do_comma_values(parser, node))
                continue

            if (registry is not None
                    and isinstance(node, syntax.WordNode)
                    and node.fieldname is not None
                    and not getattr(node, "is_quoted", False)
                    and "," in node.text):
                ref = registry.make_ref(node.fieldname)
                resolved = registry.resolve(ref) if ref is not None else None
                if resolved is not None and resolved.spec.comma_values:
                    parts = [p for p in node.text.split(",") if p]
                    if parts:
                        words = []
                        for part in parts:
                            w = syntax.WordNode(part)
                            w.set_fieldname(node.fieldname, override=True)
                            w.set_boost(node.boost)
                            w.set_range(node.startchar, node.endchar)
                            words.append(w)
                        newnode = syntax.AndGroup(words)
                        newnode.set_range(node.startchar, node.endchar)
                        newgroup.append(newnode)
                        continue

            newgroup.append(node)

        return newgroup
