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

"""Forked from whoosh's ``qparser/default.py``.

``QueryParser`` is the hand-written, plugin-driven parser that turns a query
string into a :class:`whoosh_compat.parser.syntax.SyntaxNode` tree (via
``tag()``/``filterize()``, both ported verbatim from whoosh) and then into a
:class:`whoosh_compat.ast.Node` tree (via ``.query(self)``).

Unlike whoosh, this parser has no ``schema``/analysis machinery: it is
handed a :class:`~whoosh_compat.fields.FieldRegistry` instead, and produces
raw-text AST leaves; tokenization/analysis is entirely the concern of
whatever backend later consumes the AST ("emit-time" analysis, per the
project design). The field-kind-specific decisions whoosh used to delegate to
``Schema``/``FieldType`` objects (numeric coercion, boolean parsing, range
kind inference, wildcard/prefix rewriting) are implemented here instead, as
methods called by the ``syntax`` nodes: :meth:`QueryParser.term_query`,
:meth:`QueryParser.range_query`, :meth:`QueryParser.wildcard_query`,
:meth:`QueryParser.prefix_query`, and :meth:`QueryParser.report`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryParserError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.parser import syntax
from whoosh_compat.parser.common import print_debug
from whoosh_compat.parser.plugins import MultifieldPlugin
from whoosh_compat.parser.plugins import Plugin

# Matches wildcard text that is plain *literal* text followed by exactly one
# trailing "*": rewritten to a Prefix query.
#
# "[" is excluded alongside "*"/"?" because a bracket character class is glob
# syntax too, and a Prefix node's text is a literal (the tantivy emitter
# regex-escapes it). Real whoosh's ``Wildcard.normalize()`` only checks for
# "*"/"?" here and so folds ``202[0-3]*`` down to ``Prefix('202[0-3]')``,
# silently reinterpreting the class as literal characters: a query that then
# matches (essentially) nothing. That is a whoosh bug, not an intended
# semantic: the very same class sets ``SPECIAL_CHARS = frozenset("*?[")``
# for prefix-seeking, so "[" is known to be special everywhere *except* this
# fold. whoosh-compat keeps such patterns tagged as Wildcard, where the
# emitter's fnmatch-faithful glob translation handles the class properly.
# See paperless-ngx#13568.
_TRAILING_STAR_RE = re.compile(r"^[^*?\[]+\*$")


class QueryParser:
    """A hand-written query parser built on modular plug-ins. The default
    configuration implements a powerful fielded query language similar to
    Lucene's.

    You can use the ``plugins`` argument when creating the object to override
    the default list of plug-ins, and/or use ``add_plugin()`` and/or
    ``remove_plugin_class()`` to change the plug-ins included in the parser.
    """

    def __init__(
        self,
        fieldname: str | None,
        registry: FieldRegistry,
        *,
        plugins: Sequence[Plugin | type[Plugin]] | None = None,
        group: type[syntax.GroupNode] = syntax.AndGroup,
    ) -> None:
        """
        :param fieldname: the default field: the parser uses this as the
            field for any terms without an explicit field.
        :param registry: a :class:`~whoosh_compat.fields.FieldRegistry`
            describing the known fields and their kinds/aliases.
        :param plugins: a list of plugins to use. ``WhitespacePlugin`` is
            automatically included, do not put it in this list. This
            overrides the default list of plugins. Classes in the list will
            be automatically instantiated.
        :param group: the default grouping. ``AndGroup`` makes terms required
            by default. ``OrGroup`` makes terms optional by default.
        """

        self.fieldname = fieldname
        self.registry = registry
        self.group = group
        self.plugins: list[Plugin] = []
        self.diagnostics: list[Diagnostic] = []

        if plugins is None:
            plugins = self.default_set()
        self._add_ws_plugin()
        self.add_plugins(plugins)

    def default_set(self) -> list[Plugin]:
        """Returns the default list of plugins to use."""

        from whoosh_compat.parser import plugins as plugins_mod

        pins: list[Plugin] = [
            plugins_mod.WhitespacePlugin(),
            plugins_mod.SingleQuotePlugin(),
            plugins_mod.FieldsPlugin(),
            plugins_mod.WildcardPlugin(),
            plugins_mod.PhrasePlugin(),
            plugins_mod.RangePlugin(),
            plugins_mod.GroupPlugin(),
            plugins_mod.OperatorsPlugin(),
            plugins_mod.BoostPlugin(),
            plugins_mod.EveryPlugin(),
        ]

        alias_map = {spec.name: spec.aliases for spec in self.registry if spec.aliases}
        if alias_map:
            pins.append(plugins_mod.FieldAliasPlugin(alias_map))
        pins.append(plugins_mod.CommaValuesPlugin())

        return pins

    def add_plugins(self, pins: Sequence[Plugin | type[Plugin]]) -> None:
        """Adds the given list of plugins to the list of plugins in this
        parser.
        """

        for pin in pins:
            self.add_plugin(pin)

    def add_plugin(self, pin: Plugin | type[Plugin]) -> None:
        """Adds the given plugin to the list of plugins in this parser."""

        if isinstance(pin, type):
            pin = pin()
        self.plugins.append(pin)

    def _add_ws_plugin(self) -> None:
        from whoosh_compat.parser.plugins import WhitespacePlugin

        self.add_plugin(WhitespacePlugin())

    def remove_plugin(self, pi: Plugin) -> None:
        """Removes the given plugin object from the list of plugins in this
        parser.
        """

        self.plugins.remove(pi)

    def remove_plugin_class(self, cls: type[Plugin]) -> None:
        """Removes any plugins of the given class from this parser."""

        self.plugins = [pi for pi in self.plugins if not isinstance(pi, cls)]

    def replace_plugin(self, plugin: Plugin) -> None:
        """Removes any plugins of the class of the given plugin and then adds
        it. This is a convenience method to keep from having to call
        ``remove_plugin_class`` followed by ``add_plugin`` each time you want
        to reconfigure a default plugin.
        """

        self.remove_plugin_class(plugin.__class__)
        self.add_plugin(plugin)

    def _priorized(self, methodname: str) -> list:
        # methodname is "taggers" or "filters". Returns a priorized list of
        # tagger objects or filter functions.
        items_and_priorities = []
        for plugin in self.plugins:
            # Call either .taggers() or .filters() on the plugin
            method = getattr(plugin, methodname)
            items_and_priorities.extend(method(self))
        # Sort the list by priority (lower priority runs first)
        items_and_priorities.sort(key=lambda x: x[1])
        # Return the sorted list without the priorities
        return [item for item, _ in items_and_priorities]

    def taggers(self) -> list:
        """Returns a priorized list of tagger objects provided by the
        parser's currently configured plugins.
        """

        return self._priorized("taggers")

    def filters(self) -> list:
        """Returns a priorized list of filter functions provided by the
        parser's currently configured plugins.
        """

        return self._priorized("filters")

    def tag(self, text: str, pos: int = 0, debug: int = 0) -> syntax.GroupNode:
        """Returns a group of syntax nodes corresponding to the given text,
        created by matching the Taggers provided by the parser's plugins.

        :param text: the text to tag.
        :param pos: the position in the text to start tagging at.
        """

        # The list of output tags
        stack: list[syntax.SyntaxNode] = []
        # End position of the previous match
        prev = pos
        # Priorized list of taggers provided by the parser's plugins
        taggers = self.taggers()
        if debug:
            print_debug(debug, f"Taggers: {taggers!r}")

        # Define a function that will make a WordNode from the "interstitial"
        # text between matches
        def inter(startchar: int, endchar: int) -> syntax.WordNode:
            n = syntax.WordNode(text[startchar:endchar])
            n.startchar = startchar
            n.endchar = endchar
            return n

        while pos < len(text):
            node = None
            # Try each tagger to see if it matches at the current position
            for tagger in taggers:
                node = tagger.match(self, text, pos)
                if node is not None:
                    if node.endchar <= pos:
                        raise QueryParserError(
                            f"Token {tagger!r} did not move cursor forward. ({text!r}, {pos})"
                        )
                    if prev < pos:
                        tween = inter(prev, pos)
                        if debug:
                            print_debug(debug, f"Tween: {tween!r}")
                        stack.append(tween)

                    if debug:
                        print_debug(debug, f"Tagger: {tagger!r} at {pos}: {node!r}")
                    stack.append(node)
                    prev = pos = node.endchar
                    break

            if not node:
                # No taggers matched, move forward
                pos += 1

        # If there's unmatched text left over on the end, put it in a WordNode
        if prev < len(text):
            stack.append(inter(prev, len(text)))

        # Wrap the list of nodes in a group node
        group = self.group(stack)
        if debug:
            print_debug(debug, f"Tagged group: {group!r}")
        return group

    def filterize(self, nodes: syntax.GroupNode, debug: int = 0) -> syntax.GroupNode:
        """Takes a group of nodes and runs the filters provided by the
        parser's plugins.
        """

        # Call each filter in the priorized list of plugin filters
        if debug:
            print_debug(debug, f"Pre-filtered group: {nodes!r}")
        for f in self.filters():
            if debug:
                print_debug(debug, f"..Applying: {f!r}")
            nodes = f(self, nodes)
            if debug:
                print_debug(debug, f"..Result: {nodes!r}")
            if nodes is None:
                raise QueryParserError(f"Filter {f!r} did not return anything")
        return nodes

    def process(self, text: str, pos: int = 0, debug: int = 0) -> syntax.GroupNode:
        """Returns a group of syntax nodes corresponding to the given text,
        tagged by the plugin Taggers and filtered by the plugin filters.

        :param text: the text to tag.
        :param pos: the position in the text to start tagging at.
        """

        nodes = self.tag(text, pos=pos, debug=debug)
        nodes = self.filterize(nodes, debug=debug)
        return nodes

    def parse(self, text: str, debug: int = 0) -> ast.Node:
        """Parses the input string and returns a :class:`whoosh_compat.ast.Node`
        tree.

        :param text: the string to parse.
        """

        nodes = self.process(text, debug=debug)
        if debug:
            print_debug(debug, f"Syntax tree: {nodes!r}")

        q = nodes.query(self)
        if q is None:
            q = ast.Nothing()
        if debug:
            print_debug(debug, f"Query: {q!r}")
        return q

    # -- Field-kind-driven query construction hooks -------------------------
    #
    # These replace whoosh's schema/``FieldType``-driven ``term_query`` and
    # range-kind-inference machinery. They are called from
    # :mod:`whoosh_compat.parser.syntax` and
    # :mod:`whoosh_compat.parser.plugins` node ``query()`` methods.

    def report(self, diagnostic: Diagnostic) -> None:
        """Records a diagnostic produced while building the AST."""

        self.diagnostics.append(diagnostic)

    def term_query(
        self, fieldname: str | None, text: str, boost: float = 1.0, **kw: object
    ) -> ast.Node:
        """Returns the AST node for a single term in the query string.

        Field-kind-specific coercion (U64 -> int, BOOLEAN_EXISTS -> bool) is
        applied when ``fieldname`` resolves to a known
        :class:`~whoosh_compat.fields.FieldSpec`. Unknown/dotted (JSON
        subpath) fields fall through to a plain text :class:`~whoosh_compat.ast.Term`.
        """

        spec = self.registry.resolve(fieldname) if fieldname else None
        if fieldname and spec is None:
            # Either a dotted JSON subpath, or a literal that reached here
            # because it wasn't a known field (FieldsPlugin already demoted
            # unknown plain fieldnames back to text): either way, keep the
            # (possibly dotted) fieldname text as-is.
            node: ast.Node = ast.Term(field=fieldname, text=text)
            return ast.Boosted(node, boost) if boost != 1.0 else node

        if spec:
            fieldname = spec.name  # canonicalize alias
            if spec.kind is FieldKind.U64:
                try:
                    node = ast.Term(field=fieldname, text=int(text))
                except ValueError:
                    d = Diagnostic(
                        message=f"{text!r} is not a valid number for {fieldname}",
                        kind=DiagnosticKind.BAD_NUMBER,
                        startchar=kw.get("startchar"),  # type: ignore[arg-type]
                        endchar=kw.get("endchar"),  # type: ignore[arg-type]
                        field=fieldname,
                        raw_value=text,
                    )
                    self.report(d)
                    return ast.ErrorLeaf(diagnostic=d)
                return ast.Boosted(node, boost) if boost != 1.0 else node

            if spec.kind is FieldKind.BOOLEAN_EXISTS:
                truthy = text.lower() not in ("f", "false", "no", "0")
                node = ast.Term(field=fieldname, text=truthy)
                return ast.Boosted(node, boost) if boost != 1.0 else node

            # DATE/DATETIME leaves are normally converted by DateParserPlugin
            # before reaching here; if one arrives anyway (plugin disabled or
            # not wired up yet), fall through to a plain text term below.

        node = ast.Term(field=fieldname, text=text)
        return ast.Boosted(node, boost) if boost != 1.0 else node

    def _coerce_range_bound(
        self, text: str | None, fieldname: str, node: object
    ) -> tuple[int | None, ast.ErrorLeaf | None]:
        if text is None:
            return None, None
        try:
            return int(text), None
        except ValueError:
            d = Diagnostic(
                message=f"{text!r} is not a valid number for {fieldname}",
                kind=DiagnosticKind.BAD_NUMBER,
                startchar=getattr(node, "startchar", None),
                endchar=getattr(node, "endchar", None),
                field=fieldname,
                raw_value=text,
            )
            self.report(d)
            return None, ast.ErrorLeaf(diagnostic=d)

    def range_query(
        self,
        fieldname: str | None,
        start: str | None,
        end: str | None,
        startexcl: bool,
        endexcl: bool,
        boost: float = 1.0,
        node: object = None,
    ) -> ast.Node:
        """Returns the AST node for a range query."""

        spec = self.registry.resolve(fieldname) if fieldname else None
        if spec is not None:
            fieldname = spec.name
            if spec.kind is FieldKind.U64:
                lo, lo_err = self._coerce_range_bound(start, fieldname, node)
                if lo_err is not None:
                    return lo_err
                hi, hi_err = self._coerce_range_bound(end, fieldname, node)
                if hi_err is not None:
                    return hi_err
                result: ast.Node = ast.NumericRange(
                    field=fieldname, lo=lo, hi=hi,
                    incl_lo=not startexcl, incl_hi=not endexcl,
                )
                return ast.Boosted(result, boost) if boost != 1.0 else result
            # DATE/DATETIME ranges are normally converted by
            # DateParserPlugin before reaching here; if one arrives anyway,
            # fall back to a plain TermRange below.

        result = ast.TermRange(
            field=fieldname, lo=start, hi=end,
            incl_lo=not startexcl, incl_hi=not endexcl,
        )
        return ast.Boosted(result, boost) if boost != 1.0 else result

    def wildcard_query(
        self, fieldname: str | None, text: str, boost: float = 1.0
    ) -> ast.Node:
        """Returns the AST node for a wildcard query, applying whoosh's
        wildcard-simplification rules:

        * No ``*``/``?`` characters at all -> plain term query.
        * The text is exactly ``"*"`` -> :class:`~whoosh_compat.ast.Every`.
        * Plain *literal* text (no ``*``/``?``/``[``) followed by a single
          trailing ``*`` -> :class:`~whoosh_compat.ast.Prefix`.
        * Otherwise -> :class:`~whoosh_compat.ast.Wildcard`.

        Note that this is only ever reached for text ``WildcardPlugin``
        actually tagged as a wildcard, i.e. text containing ``*`` or a
        question mark: a bracket-only ``202[0-3]`` lexes as an ordinary
        term on both sides, matching whoosh. See ``_TRAILING_STAR_RE`` for
        why ``[`` blocks the Prefix fold.
        """

        if not any(ch in text for ch in "*?"):
            return self.term_query(fieldname, text, boost=boost)

        node: ast.Node
        if text == "*":
            node = ast.Every(field=fieldname)
        elif _TRAILING_STAR_RE.match(text):
            node = ast.Prefix(field=fieldname, text=text[:-1])
        else:
            node = ast.Wildcard(field=fieldname, pattern=text)

        return ast.Boosted(node, boost) if boost != 1.0 else node

    def prefix_query(
        self, fieldname: str | None, text: str, boost: float = 1.0
    ) -> ast.Node:
        """Returns the AST node for a prefix query."""

        node: ast.Node = ast.Prefix(field=fieldname, text=text)
        return ast.Boosted(node, boost) if boost != 1.0 else node


class MultifieldParser(QueryParser):
    """A :class:`QueryParser` configured to search in multiple fields.

    Instead of assigning unfielded clauses to a single default field, this
    parser transforms them into an OR clause that searches a list of fields.
    For example, if the list of multi-fields is "f1", "f2" and the query
    string is "hello there", the class will parse
    "(f1:hello OR f2:hello) (f1:there OR f2:there)".

    ``fieldboosts`` applies *only* to the per-field copies created by this
    expansion: an explicitly-fielded term (``f1:hello``) is never boosted
    by ``fieldboosts``, matching whoosh's ``MultifieldPlugin`` semantics.
    """

    def __init__(
        self,
        fieldnames: Sequence[str],
        registry: FieldRegistry,
        *,
        fieldboosts: Mapping[str, float] | None = None,
        plugins: Sequence[Plugin | type[Plugin]] | None = None,
        group: type[syntax.GroupNode] = syntax.AndGroup,
    ) -> None:
        super().__init__(None, registry, plugins=plugins, group=group)
        mfp = MultifieldPlugin(
            list(fieldnames),
            fieldboosts=dict(fieldboosts) if fieldboosts else None,
        )
        self.add_plugin(mfp)
