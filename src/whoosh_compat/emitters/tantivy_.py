"""AST -> tantivy.Query emitter.

Builds ``tantivy.Query`` objects programmatically (``Query.term_query``,
``Query.boolean_query``, etc.) -- never via ``tantivy.parse_query`` /
``index.parse_query`` (that shortcut is reserved for the JSON subpath
carve-out described below, not general query emission).

Installed tantivy-py's ``Query.term_query`` resolves fields by exact name, so
it cannot address a JSON subpath (``notes.user``) even when ``notes`` is a
JSON field -- it raises ``ValueError`` as if the field were unknown. Until
https://github.com/quickwit-oss/tantivy-py/pull/716 lands and ships, JSON
subpath terms are emitted via ``index.parse_query`` instead; see
``TantivyEmitter._json_paths_supported``/``_emit_json_term``.

This module imports ``tantivy`` at module scope. It is only imported by
code that actually wants the tantivy backend; ``whoosh_compat`` itself
(the package __init__) does not import it, since tantivy is an optional
dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC
from datetime import datetime

import tantivy

from whoosh_compat import ast
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import Multitoken

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


def _translate_class(pattern: str, i: int, n: int) -> tuple[str | None, int]:
    """Translate the bracket expression starting just after a ``[`` at ``i-1``.

    Returns ``(regex_fragment, next_index)``. ``regex_fragment`` is ``None``
    when the ``[`` has no matching ``]`` -- in that case the caller must treat
    the ``[`` as an ordinary literal character and resume at ``i``.

    This is a direct port of CPython's ``fnmatch.translate`` bracket handling
    (the ``elif c == '['`` branch), which is the semantics whoosh's
    ``query.Wildcard`` inherits by compiling its pattern with
    ``fnmatch.translate``. It is deliberately *not* simplified: the ``!``
    negation, the leading-``]``-is-a-literal-member rule, and the
    hyphen/backslash escaping inside the class all have to line up with
    fnmatch exactly or globs would silently change meaning.
    """
    j = i
    if j < n and pattern[j] == "!":
        j += 1
    if j < n and pattern[j] == "]":
        j += 1
    while j < n and pattern[j] != "]":
        j += 1
    if j >= n:
        return None, i

    stuff = pattern[i:j]
    if "-" not in stuff:
        stuff = stuff.replace("\\", r"\\")
    else:
        chunks = []
        k = i + 2 if pattern[i] == "!" else i + 1
        start = i
        while True:
            k = pattern.find("-", k, j)
            if k < 0:
                break
            chunks.append(pattern[start:k])
            start = k + 1
            k = k + 3
        chunk = pattern[start:j]
        if chunk:
            chunks.append(chunk)
        else:
            chunks[-1] += "-"
        # Remove empty ranges -- invalid in a regex character class.
        for k in range(len(chunks) - 1, 0, -1):
            if chunks[k - 1][-1] > chunks[k][0]:
                chunks[k - 1] = chunks[k - 1][:-1] + chunks[k][1:]
                del chunks[k]
        # Escape backslashes and hyphens for set difference ("--"); hyphens
        # that create ranges must stay unescaped.
        stuff = "-".join(s.replace("\\", r"\\").replace("-", r"\-") for s in chunks)

    if not stuff:
        # "[]" -- an empty range never matches.
        return "(?!)", j + 1
    if stuff == "!":
        # "[!]" -- a negated empty range matches any single character.
        return ".", j + 1
    if stuff[0] == "!":
        stuff = "^" + stuff[1:]
    elif stuff[0] == "^":
        stuff = "\\" + stuff
    # Python's `re` tolerates a bare "[", "&" or "~" inside a class; Rust's
    # regex crate (which tantivy uses) reads them as the start of a nested
    # class / a set-operator and errors out ("unclosed character class").
    # Escaping them is a no-op for the matched language on both engines.
    for ch in "[&~":
        stuff = stuff.replace(ch, "\\" + ch)
    return f"[{stuff}]", j + 1


def glob_to_regex(pattern: str, normalizer: Callable[[str], str] | None) -> str:
    """Translate an fnmatch-style glob into a tantivy regex.

    Whoosh's ``query.Wildcard`` compiles its pattern with
    ``fnmatch.translate``, so fnmatch -- not a naive split on ``*``/``?`` --
    is the ground truth for what a whoosh wildcard matches. This function
    reproduces fnmatch's translation with two deliberate changes:

    * Literal runs are passed through ``normalizer`` (``spec.pattern_normalizer``,
      identity when ``None``) *before* being regex-escaped, so a pattern can be
      case-folded to line up with the analyzed/indexed term text. Only literal
      runs are normalized -- ``*``, ``?`` and bracket-class bodies are pattern
      syntax and are passed through as such.
    * No anchoring. ``fnmatch.translate`` emits ``(?s:...)\\z`` framing, and
      newer CPython versions also emit atomic groups (``(?>.*?foo)``) as a
      backtracking optimization. tantivy's regex engine (regex-automata, via
      tantivy_fst) matches the *whole* term by construction and does not
      support atomic groups, so the framing is dropped and ``*`` is emitted as
      a plain ``.*``.
    """
    normalize: Callable[[str], str] = normalizer if normalizer is not None else (lambda s: s)

    out: list[str] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            out.append(re.escape(normalize("".join(literal))))
            literal.clear()

    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            flush()
            # Collapse runs of "*" -- "a**b" means the same as "a*b".
            while i < n and pattern[i] == "*":
                i += 1
            out.append(".*")
        elif c == "?":
            flush()
            out.append(".")
        elif c == "[":
            fragment, i = _translate_class(pattern, i, n)
            if fragment is None:
                # No closing "]": the "[" is an ordinary character.
                literal.append(c)
            else:
                flush()
                out.append(fragment)
        else:
            literal.append(c)
    flush()
    return "".join(out)


def _to_naive_utc(value: datetime) -> datetime:
    """Naive-UTC form of ``value`` for ``Query.range_query``.

    ``Query.range_query`` (tantivy-py 0.26) only accepts *naive* datetimes for
    ``FieldType.Date`` -- a tz-aware one raises ``ValueError: Expected DateTime
    type for field ...`` (unlike ``Query.term_query``, which accepts both).
    Parser-produced range bounds are always tz-aware UTC, so convert here.
    Naive input is passed through unchanged (tantivy already reads naive
    datetimes as UTC, matching how documents are indexed).
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


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
        # Cache for _json_paths_supported(): None means "not probed yet".
        self._json_paths_ok: bool | None = None

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

    def _text_term_query(
        self, spec, tokens: list[str], *, field_name: str | None = None
    ) -> tantivy.Query:
        """Build the query for one already-tokenized TEXT/KEYWORD term.

        Caller guarantees ``tokens`` is non-empty. ``field_name`` defaults to
        ``spec.name``; JSON subpath terms pass the dotted path (e.g.
        ``"notes.user"``) instead, reusing this method's multitoken handling
        without duplicating it.
        """
        field_name = spec.name if field_name is None else field_name

        if len(tokens) == 1:
            return tantivy.Query.term_query(self.schema, field_name, tokens[0])

        mode = spec.multitoken
        if mode is Multitoken.DEFAULT:
            mode = self._group_stack[-1]

        if mode is Multitoken.FIRST:
            return tantivy.Query.term_query(self.schema, field_name, tokens[0])
        if mode is Multitoken.PHRASE:
            # tantivy-py's phrase_query() takes list[str | tuple[int, str]];
            # lists are invariant so a plain list[str] doesn't satisfy that
            # even though every element here is a bare str (no explicit
            # positions).
            words: list[str | tuple[int, str]] = list(tokens)
            return tantivy.Query.phrase_query(self.schema, field_name, words)

        term_queries = [tantivy.Query.term_query(self.schema, field_name, t) for t in tokens]
        if mode is Multitoken.OR:
            clauses = [(tantivy.Occur.Should, q) for q in term_queries]
            return _boolean_query(clauses, minimum_number_should_match=1)
        # Multitoken.AND (and the DEFAULT-at-top-level fallback)
        clauses = [(tantivy.Occur.Must, q) for q in term_queries]
        return _boolean_query(clauses)

    def _json_paths_supported(self) -> bool:
        """Whether the installed tantivy-py's ``Query.term_query`` can address
        a JSON subpath directly (cached once per emitter instance).

        Probes with the first JSON-kind field/subpath found in the registry --
        JSON path resolution in ``term_query`` is a schema-level capability of
        the installed tantivy-py, not something that varies per field, so one
        probe per emitter instance is representative for all JSON fields.
        Retires itself once https://github.com/quickwit-oss/tantivy-py/pull/716
        ships: the probe starts succeeding and ``_emit_json_term`` stops
        taking the ``parse_query`` branch below.
        """
        if self._json_paths_ok is None:
            probe_path = None
            for spec in self.registry:
                if spec.kind is FieldKind.JSON and spec.subpaths:
                    probe_path = f"{spec.name}.{spec.subpaths[0]}"
                    break
            if probe_path is None:
                # No JSON fields registered -- the probe result is moot.
                self._json_paths_ok = False
            else:
                try:
                    tantivy.Query.term_query(self.schema, probe_path, "probe")
                    self._json_paths_ok = True
                except ValueError:
                    self._json_paths_ok = False
        return self._json_paths_ok

    def _emit_json_term(self, spec, subpath: str, text: object) -> tantivy.Query:
        """Emit a term query for a JSON subpath (``spec.name + "." + subpath``).

        Runs ``spec.analyzer`` over the value first, exactly like TEXT/KEYWORD
        terms, so multi-token JSON values follow the same multitoken policy
        (``_text_term_query`` is reused, not duplicated). When the installed
        tantivy-py cannot address JSON subpaths via ``term_query`` (see
        ``_json_paths_supported``), falls back to ``index.parse_query`` --
        the only route currently able to reach a JSON subpath -- quoting the
        value and escaping backslashes/quotes so it round-trips through the
        query-string grammar.
        """
        full = f"{spec.name}.{subpath}"
        tokens = self._tokens(spec, text)
        if not tokens:
            return tantivy.Query.empty_query()

        if self._json_paths_supported():
            return self._text_term_query(spec, tokens, field_name=full)

        escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
        return self.index.parse_query(f'{full}:"{escaped}"', default_field_names=[spec.name])

    def _group_child(self, child: ast.Node) -> tantivy.Query | None:
        """Visit a direct child of an And/Or group.

        Returns ``None`` when the child -- possibly wrapped in one or more
        transparent ``Boosted`` layers -- is a zero-token analyzed TEXT/
        KEYWORD term: such a term is dropped from its enclosing group
        entirely (whoosh's own behavior when a field's analyzer consumes a
        token completely, e.g. an all-stopword value). ``Boosted`` is
        unwrapped recursively (rather than only checking a
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
        if node.field is None:
            return tantivy.Query.all_query()
        spec = self._resolve(node.field)
        if spec.fast:
            # exists_query is a cheap fast-field presence check, but it only
            # works on fast fields (tantivy errors out otherwise).
            return tantivy.Query.exists_query(spec.name)
        # Non-fast (TEXT/KEYWORD) fields: "has any term at all" via a regex
        # that matches every term in the field's dictionary.
        return tantivy.Query.regex_query(self.schema, spec.name, ".*")

    def visit_errorleaf(self, node: ast.ErrorLeaf) -> tantivy.Query:
        raise QueryEmitError(
            f"cannot emit query: {node.diagnostic.message}", diagnostic=node.diagnostic
        )

    def visit_term(self, node: ast.Term) -> tantivy.Query:
        if node.field is not None and "." in node.field:
            resolved = self.registry.resolve_json(node.field)
            if resolved is not None:
                spec, subpath = resolved
                return self._emit_json_term(spec, subpath, node.text)

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
            raise QueryEmitError(
                f"field {spec.name!r} is a JSON field; term queries must "
                f"address a subpath (e.g. {spec.name}.<subpath>)"
            )

        raise NotImplementedError(
            f"Term emission for field kind {spec.kind.name} is not implemented"
        )

    def visit_phrase(self, node: ast.Phrase) -> tantivy.Query:
        spec = self._resolve(node.field)
        tokens = self._tokens(spec, node.text)
        if not tokens:
            return tantivy.Query.empty_query()
        if len(tokens) == 1:
            # tantivy rejects a single-word phrase query; a term query is the
            # exact equivalent anyway.
            return tantivy.Query.term_query(self.schema, spec.name, tokens[0])
        # whoosh's slop counts *positions spanned* (slop=1 means adjacent);
        # tantivy's counts *gaps allowed* (slop=0 means adjacent).
        slop = max(node.slop - 1, 0)
        words: list[str | tuple[int, str]] = list(tokens)
        return tantivy.Query.phrase_query(self.schema, spec.name, words, slop=slop)

    def visit_prefix(self, node: ast.Prefix) -> tantivy.Query:
        spec = self._resolve(node.field)
        text = str(node.text)
        if spec.pattern_normalizer is not None:
            text = spec.pattern_normalizer(text)
        return tantivy.Query.regex_query(self.schema, spec.name, re.escape(text) + ".*")

    def visit_wildcard(self, node: ast.Wildcard) -> tantivy.Query:
        spec = self._resolve(node.field)
        regex = glob_to_regex(str(node.pattern), spec.pattern_normalizer)
        return tantivy.Query.regex_query(self.schema, spec.name, regex)

    def visit_termrange(self, node: ast.TermRange) -> tantivy.Query:
        raise UnsupportedQueryError("text ranges are not supported (DIVERGENCES.md entry 5)")

    def _range_query(
        self, spec, field_type, lo, hi, node: ast.NumericRange | ast.DateRange
    ) -> tantivy.Query:
        """Shared range_query construction for numeric and date ranges.

        tantivy requires an unbounded side to be *inclusive* (passing
        ``include_* = False`` alongside a ``None`` bound is an error), so the
        node's inclusivity flag is only honored on bounds that actually exist.
        """
        if lo is None and hi is None:
            raise QueryEmitError("range query needs at least one bound")
        return tantivy.Query.range_query(
            self.schema,
            spec.name,
            field_type,
            lower_bound=lo,
            upper_bound=hi,
            include_lower=True if lo is None else node.incl_lo,
            include_upper=True if hi is None else node.incl_hi,
        )

    def visit_numericrange(self, node: ast.NumericRange) -> tantivy.Query:
        spec = self._resolve(node.field)
        lo = None if node.lo is None else int(node.lo)
        hi = None if node.hi is None else int(node.hi)
        # v1 scope: numeric fields are U64 only.
        return self._range_query(spec, tantivy.FieldType.Unsigned, lo, hi, node)

    def visit_daterange(self, node: ast.DateRange) -> tantivy.Query:
        spec = self._resolve(node.field)
        lo = None if node.lo is None else _to_naive_utc(node.lo)
        hi = None if node.hi is None else _to_naive_utc(node.hi)
        return self._range_query(spec, tantivy.FieldType.Date, lo, hi, node)

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
