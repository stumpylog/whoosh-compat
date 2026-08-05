"""AST -> tantivy.Query emitter.

Builds ``tantivy.Query`` objects programmatically (``Query.term_query``,
``Query.boolean_query``, etc.) -- never via ``tantivy.parse_query`` /
``index.parse_query`` (that shortcut is reserved for the Task 14 JSON
carve-out, not general query emission).

This module imports ``tantivy`` at module scope. It is only imported by
code that actually wants the tantivy backend; ``whoosh_compat`` itself
(the package __init__) does not import it, since tantivy is an optional
dependency.
"""

from __future__ import annotations

import tantivy

from whoosh_compat import ast
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.fields import FieldKind, FieldRegistry, Multitoken

_FALSY_TEXT = ("f", "false", "no", "0")


def _is_truthy(value: object) -> bool:
    """Truthiness for BOOLEAN_EXISTS term text.

    Mirrors the parser's own coercion rule (``parser/default.py``): the
    strings ``f``/``false``/``no``/``0`` (case-insensitive) are falsy,
    everything else is truthy. By the time a ``Term`` reaches this emitter
    its text has usually already been coerced to ``bool`` by the parser, but
    this function also accepts a raw string so directly-constructed AST
    nodes (as used in tests) behave the same way.
    """
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_TEXT
    return bool(value)


def _pad_if_all_negative(
    clauses: list[tuple[tantivy.Occur, tantivy.Query]],
) -> list[tuple[tantivy.Occur, tantivy.Query]]:
    """Prepend a neutral MUST clause if ``clauses`` are all Occur.MustNot.

    Works around quickwit-oss/tantivy#3025: a ``BooleanQuery`` whose clauses
    are *all* ``Occur.MustNot`` matches zero documents instead of "every
    document except the excluded ones" (the fix for this is unmerged
    upstream as of this writing). Prepending a trivially-true, zero-scoring
    MUST clause (``Query.boost_query(Query.all_query(), 0.0)``) restores the
    expected "all except excluded" semantics without affecting scoring.

    Must be applied at every nesting level that builds a boolean query out
    of clauses that might end up all-negative -- a single top-level ``NOT``,
    a falsy ``BOOLEAN_EXISTS`` term, and any nested group that happens to
    reduce to only negative clauses.
    """
    if clauses and all(occur == tantivy.Occur.MustNot for occur, _ in clauses):
        pad = (tantivy.Occur.Must, tantivy.Query.boost_query(tantivy.Query.all_query(), 0.0))
        return [pad, *clauses]
    return list(clauses)


def _boolean_query(
    clauses: list[tuple[tantivy.Occur, tantivy.Query]],
    *,
    minimum_number_should_match: int | None = None,
) -> tantivy.Query:
    """Build a ``boolean_query``, applying the all-negative padding rule.

    Every call site in this module that assembles a clause list should
    funnel through here (rather than calling ``tantivy.Query.boolean_query``
    directly) so the padding rule is applied uniformly.
    """
    clauses = _pad_if_all_negative(clauses)
    if minimum_number_should_match is not None:
        return tantivy.Query.boolean_query(clauses, minimum_number_should_match=minimum_number_should_match)
    return tantivy.Query.boolean_query(clauses)


class TantivyEmitter(ast.Visitor["tantivy.Query"]):
    """Emits ``tantivy.Query`` objects from a whoosh-compat AST."""

    def __init__(self, *, index: tantivy.Index, schema: tantivy.Schema, registry: FieldRegistry):
        self.index = index
        self.schema = schema
        self.registry = registry
        # Multitoken.DEFAULT term text resolves against the enclosing
        # group's semantics; top level behaves like AND.
        self._group_stack: list[Multitoken] = [Multitoken.AND]

    def emit(self, node: ast.Node) -> tantivy.Query:
        return self.visit(node)

    # -- helpers -----------------------------------------------------

    def _resolve(self, field: str | None):
        if field is None:
            raise QueryEmitError("cannot emit an unfielded term")
        spec = self.registry.resolve(field)
        if spec is None:
            raise QueryEmitError(f"unknown field {field!r}")
        return spec

    def _tokens(self, spec, text) -> list[str]:
        text = str(text)
        if spec.analyzer is None:
            return [text] if text else []
        return list(spec.analyzer(text))

    def _text_term_query(self, spec, tokens: list[str]) -> tantivy.Query:
        """Build the query for one already-tokenized TEXT/KEYWORD term.

        Caller guarantees ``tokens`` is non-empty.
        """
        if len(tokens) == 1:
            return tantivy.Query.term_query(self.schema, spec.name, tokens[0])

        mode = spec.multitoken
        if mode is Multitoken.DEFAULT:
            mode = self._group_stack[-1]

        if mode is Multitoken.FIRST:
            return tantivy.Query.term_query(self.schema, spec.name, tokens[0])
        if mode is Multitoken.PHRASE:
            return tantivy.Query.phrase_query(self.schema, spec.name, tokens)

        term_queries = [tantivy.Query.term_query(self.schema, spec.name, t) for t in tokens]
        if mode is Multitoken.OR:
            clauses = [(tantivy.Occur.Should, q) for q in term_queries]
            return _boolean_query(clauses, minimum_number_should_match=1)
        # Multitoken.AND (and the DEFAULT-at-top-level fallback)
        clauses = [(tantivy.Occur.Must, q) for q in term_queries]
        return _boolean_query(clauses)

    def _group_child(self, child: ast.Node) -> tantivy.Query | None:
        """Visit a direct child of an And/Or group.

        Returns ``None`` when the child -- possibly wrapped in one or more
        transparent ``Boosted`` layers -- is a zero-token analyzed TEXT/
        KEYWORD term. This is the brief's "dropped from the group" rule.
        ``Boosted`` is unwrapped recursively (rather than only checking a
        direct ``ast.Term`` child) so ``Boosted(Boosted(Term(...)))`` and
        similar shapes are still dropped correctly instead of turning into
        a live-but-unmatchable ``boost_query(empty_query(), ...)`` clause
        that would wrongly restrict an enclosing And.
        """
        if isinstance(child, ast.Boosted):
            inner = self._group_child(child.child)
            if inner is None:
                return None
            return tantivy.Query.boost_query(inner, child.boost)
        if isinstance(child, ast.Term):
            spec = self._resolve(child.field)
            if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
                tokens = self._tokens(spec, child.text)
                if not tokens:
                    return None
        return self.visit(child)

    # -- leaves --------------------------------------------------------

    def visit_nothing(self, node: ast.Nothing) -> tantivy.Query:
        return tantivy.Query.empty_query()

    def visit_every(self, node: ast.Every) -> tantivy.Query:
        if node.field is not None:
            raise NotImplementedError(
                "Task 13: fielded Every() is not implemented by the terms/booleans emitter"
            )
        return tantivy.Query.all_query()

    def visit_errorleaf(self, node: ast.ErrorLeaf) -> tantivy.Query:
        raise QueryEmitError(
            f"cannot emit query: {node.diagnostic.message}", diagnostic=node.diagnostic
        )

    def visit_term(self, node: ast.Term) -> tantivy.Query:
        spec = self._resolve(node.field)

        if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            tokens = self._tokens(spec, node.text)
            if not tokens:
                # Not inside a group that can drop us -- an empty term
                # standalone simply matches nothing.
                return tantivy.Query.empty_query()
            return self._text_term_query(spec, tokens)

        if spec.kind is FieldKind.U64:
            return tantivy.Query.term_query(self.schema, spec.name, int(node.text))

        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            exists = tantivy.Query.exists_query(spec.exists_target)
            if _is_truthy(node.text):
                return exists
            return _boolean_query([(tantivy.Occur.MustNot, exists)])

        if spec.kind is FieldKind.JSON:
            raise NotImplementedError(
                "Task 14: JSON field Term emission is not implemented by this emitter"
            )

        raise NotImplementedError(
            f"Task 13: Term emission for field kind {spec.kind.name} is not implemented"
        )

    def visit_phrase(self, node: ast.Phrase) -> tantivy.Query:
        raise NotImplementedError("Task 13: Phrase emission is not implemented by this emitter")

    def visit_prefix(self, node: ast.Prefix) -> tantivy.Query:
        raise NotImplementedError("Task 13: Prefix emission is not implemented by this emitter")

    def visit_wildcard(self, node: ast.Wildcard) -> tantivy.Query:
        raise NotImplementedError("Task 13: Wildcard emission is not implemented by this emitter")

    def visit_termrange(self, node: ast.TermRange) -> tantivy.Query:
        raise NotImplementedError("Task 13: TermRange emission is not implemented by this emitter")

    def visit_numericrange(self, node: ast.NumericRange) -> tantivy.Query:
        raise NotImplementedError(
            "Task 13: NumericRange emission is not implemented by this emitter"
        )

    def visit_daterange(self, node: ast.DateRange) -> tantivy.Query:
        raise NotImplementedError("Task 13: DateRange emission is not implemented by this emitter")

    # -- boolean combinators --------------------------------------------

    def visit_and(self, node: ast.And) -> tantivy.Query:
        self._group_stack.append(Multitoken.AND)
        try:
            raw_children = [self._group_child(c) for c in node.children]
        finally:
            self._group_stack.pop()
        children: list[tantivy.Query] = [c for c in raw_children if c is not None]
        if not children:
            return tantivy.Query.empty_query()
        if len(children) == 1:
            return children[0]
        clauses = [(tantivy.Occur.Must, c) for c in children]
        return _boolean_query(clauses)

    def visit_or(self, node: ast.Or) -> tantivy.Query:
        self._group_stack.append(Multitoken.OR)
        try:
            raw_children = [self._group_child(c) for c in node.children]
        finally:
            self._group_stack.pop()
        children: list[tantivy.Query] = [c for c in raw_children if c is not None]
        if not children:
            return tantivy.Query.empty_query()
        if len(children) == 1:
            return children[0]
        clauses = [(tantivy.Occur.Should, c) for c in children]
        return _boolean_query(clauses, minimum_number_should_match=1)

    def visit_not(self, node: ast.Not) -> tantivy.Query:
        child = self.visit(node.child)
        return _boolean_query([(tantivy.Occur.MustNot, child)])

    def visit_andnot(self, node: ast.AndNot) -> tantivy.Query:
        positive = self.visit(node.positive)
        negative = self.visit(node.negative)
        clauses = [(tantivy.Occur.Must, positive), (tantivy.Occur.MustNot, negative)]
        return _boolean_query(clauses)

    def visit_andmaybe(self, node: ast.AndMaybe) -> tantivy.Query:
        required = self.visit(node.required)
        optional = self.visit(node.optional)
        clauses = [(tantivy.Occur.Must, required), (tantivy.Occur.Should, optional)]
        return _boolean_query(clauses)

    def visit_require(self, node: ast.Require) -> tantivy.Query:
        scored = self.visit(node.scored)
        filter_only = self.visit(node.filter_only)
        clauses = [
            (tantivy.Occur.Must, scored),
            (tantivy.Occur.Must, tantivy.Query.const_score_query(filter_only, 0.0)),
        ]
        return _boolean_query(clauses)

    def visit_boosted(self, node: ast.Boosted) -> tantivy.Query:
        child = self.visit(node.child)
        return tantivy.Query.boost_query(child, node.boost)


def emit(
    node: ast.Node,
    *,
    index: tantivy.Index,
    schema: tantivy.Schema,
    registry: FieldRegistry,
) -> tantivy.Query:
    """Emit a ``tantivy.Query`` for ``node`` against ``registry``."""
    return TantivyEmitter(index=index, schema=schema, registry=registry).emit(node)
