"""AST -> tantivy.Query emitter.

Builds ``tantivy.Query`` objects programmatically (``Query.term_query``,
``Query.boolean_query``, etc.): never via ``tantivy.parse_query`` /
``index.parse_query`` (that shortcut is reserved for the JSON subpath
carve-out described below, not general query emission).

Installed tantivy-py's ``Query.term_query`` resolves fields by exact name, so
it cannot address a JSON subpath (``notes.user``) even when ``notes`` is a
JSON field: it raises ``ValueError`` as if the field were unknown. Until
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
from whoosh_compat.fields import ExistsStrategy
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken
from whoosh_compat.fields import ResolvedField

_FALSY_TEXT = ("f", "false", "no", "0")
_U64_MAX = 2**64 - 1


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
    when the ``[`` has no matching ``]``: in that case the caller must treat
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
        # Remove empty ranges: invalid in a regex character class.
        for k in range(len(chunks) - 1, 0, -1):
            if chunks[k - 1][-1] > chunks[k][0]:
                chunks[k - 1] = chunks[k - 1][:-1] + chunks[k][1:]
                del chunks[k]
        # Escape backslashes and hyphens for set difference ("--"); hyphens
        # that create ranges must stay unescaped.
        stuff = "-".join(s.replace("\\", r"\\").replace("-", r"\-") for s in chunks)

    if not stuff:
        # "[]": an empty range never matches.
        return "(?!)", j + 1
    if stuff == "!":
        # "[!]": a negated empty range matches any single character.
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
    ``fnmatch.translate``, so fnmatch: not a naive split on ``*``/``?``:
    is the ground truth for what a whoosh wildcard matches. This function
    reproduces fnmatch's translation with two deliberate changes:

    * Literal runs are passed through ``normalizer`` (``spec.pattern_normalizer``,
      identity when ``None``) *before* being regex-escaped, so a pattern can be
      case-folded to line up with the analyzed/indexed term text. Only literal
      runs are normalized: ``*``, ``?`` and bracket-class bodies are pattern
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
            # Collapse runs of "*": "a**b" means the same as "a*b".
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
    ``FieldType.Date``: a tz-aware one raises ``ValueError: Expected DateTime
    type for field ...`` (unlike ``Query.term_query``, which accepts both).
    Parser-produced range bounds are always tz-aware UTC, so convert here.
    Naive input is passed through unchanged (tantivy already reads naive
    datetimes as UTC, matching how documents are indexed).

    Note: tantivy-py 0.26.0 truncates datetimes to whole seconds on both the
    index and the query side; this is consistent and harmless in practice.
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
    of clauses that might end up all-negative: a single top-level ``NOT``,
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
        return tantivy.Query.boolean_query(
            clauses, minimum_number_should_match=minimum_number_should_match
        )
    return tantivy.Query.boolean_query(clauses)


class TantivyEmitter(ast.Visitor["tantivy.Query"]):
    """Emits ``tantivy.Query`` objects from a whoosh-compat AST."""

    def __init__(self, *, index: tantivy.Index, registry: FieldRegistry):
        self.index = index
        self.schema = index.schema
        self.registry = registry
        # Multitoken.DEFAULT term text resolves against the enclosing
        # group's semantics; top level behaves like AND.
        self._group_stack: list[Multitoken] = [Multitoken.AND]
        # Cache for _json_paths_supported(): None means "not probed yet".
        self._json_paths_ok: bool | None = None

    def emit(self, node: ast.Node) -> tantivy.Query:
        """Emit ``node``, guaranteeing the documented exception contract.

        Most invalid-input shapes get a specific, well-messaged
        ``QueryEmitError``/``UnsupportedQueryError`` from the ``visit_*``
        method that first notices them. This is the backstop for the rest:
        a hand-built (not just parsed) AST can carry a value of the wrong
        type or shape for its field's kind in ways no single ``visit_*``
        method specifically checks for (e.g. a non-numeric ``Boosted.boost``,
        which every kind can carry, or a future tantivy-py call this doesn't
        yet special-case); the underlying tantivy-py call then raises a
        bare ``ValueError``/``TypeError``/``AttributeError`` instead of one
        of the two documented types (issue #24). Converting here, once,
        keeps every individual ``visit_*`` method free to just let its own
        tantivy-py calls raise naturally rather than needing its own
        try/except for cases already covered by this backstop.
        """
        try:
            return self.visit(node)
        except (ValueError, TypeError, AttributeError) as exc:
            raise QueryEmitError(f"cannot emit query: {exc}") from exc

    # -- helpers -----------------------------------------------------

    def _resolve(self, field: FieldRef | None) -> ResolvedField:
        if field is None:
            raise QueryEmitError("cannot emit an unfielded term")
        resolved = self.registry.resolve(field)
        if resolved is None:
            raise QueryEmitError(f"unknown field {str(field)!r}")
        return resolved

    def _tokens(self, spec: FieldSpec, text: object) -> list[str]:
        text = str(text)
        if spec.analyzer is None:
            return [text] if text else []
        return list(spec.analyzer(text))

    def _text_term_query(self, resolved: ResolvedField, tokens: list[str]) -> tantivy.Query:
        """Build the query for one already-tokenized TEXT/KEYWORD term.

        Caller guarantees ``tokens`` is non-empty. The tantivy field name
        queried is ``resolved.dotted_name``, so a JSON subpath resolution
        (``"notes.user"``) is handled identically to a plain field
        (``"body"``) without a separate code path.
        """
        spec = resolved.spec
        field_name = resolved.dotted_name

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
            # positions). No slop argument here: this builds a phrase query
            # out of a multi-token *Term* value under Multitoken.PHRASE
            # policy, and whoosh has no slop concept to carry for that (a
            # bare Term never carries one). Distinct from the actual
            # Phrase-node path (visit_phrase / _emit_json_phrase), which
            # maps node.slop via max(node.slop - 1, 0).
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

        Probes with the first JSON-kind field/subpath found in the registry:
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
                # No JSON fields registered: the probe result is moot.
                self._json_paths_ok = False
            else:
                try:
                    tantivy.Query.term_query(self.schema, probe_path, "probe")
                    self._json_paths_ok = True
                except ValueError:
                    self._json_paths_ok = False
        return self._json_paths_ok

    def _emit_json_term(self, resolved: ResolvedField, text: object) -> tantivy.Query:
        """Emit a term query for a JSON subpath (``resolved.dotted_name``).

        For a ``Term`` node's value only; a ``Phrase`` node uses the separate
        ``_emit_json_phrase`` below, which never consults ``spec.multitoken``
        and carries an explicit slop (DIVERGENCES.md entry 22, issue #34).

        Runs ``spec.analyzer`` over the value first, exactly like TEXT/KEYWORD
        terms, so multi-token JSON values follow the same multitoken policy
        (``_text_term_query`` is reused, not duplicated). When the installed
        tantivy-py cannot address JSON subpaths via ``term_query`` (see
        ``_json_paths_supported``), falls back to ``index.parse_query``:
        the only route currently able to reach a JSON subpath: quoting the
        analyzed tokens and escaping backslashes/quotes so they round-trip
        through the query-string grammar.

        The fallback honors ``spec.analyzer``'s output (not the raw text)
        and, for ``Multitoken.FIRST``, searches only the first token. It
        cannot honor AND/OR/PHRASE fully: ``index.parse_query``'s carve-out
        here is deliberately a single quoted leaf (see module docstring),
        with no programmatic way to build a JSON-subpath boolean/phrase
        query the way ``_text_term_query`` does for every other field kind,
        so an AND/OR-mode multi-token value still collapses to one quoted
        (phrase-like) leaf instead of true AND/OR combinator semantics.
        This is a known, narrow limitation of the fallback path itself
        (see DIVERGENCES.md), expected to retire once
        https://github.com/quickwit-oss/tantivy-py/pull/716 ships and
        ``_json_paths_supported()`` starts returning True.
        """
        spec = resolved.spec
        full = resolved.dotted_name
        tokens = self._tokens(spec, text)
        if not tokens:
            return tantivy.Query.empty_query()

        if self._json_paths_supported():
            return self._text_term_query(resolved, tokens)

        mode = spec.multitoken
        if mode is Multitoken.DEFAULT:
            mode = self._group_stack[-1]
        query_text = tokens[0] if mode is Multitoken.FIRST else " ".join(tokens)

        escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
        return self.index.parse_query(f'{full}:"{escaped}"', default_field_names=[spec.name])

    def _emit_json_phrase(self, resolved: ResolvedField, text: object, slop: int) -> tantivy.Query:
        """Emit a phrase query for a JSON subpath (``resolved.dotted_name``).

        A separate helper from ``_emit_json_term`` rather than a shared one
        with an extra parameter: a Phrase node's semantics genuinely diverge
        from a Term's here, not just in the value passed through. It mirrors
        the plain-field phrase path (``visit_phrase``'s TEXT/KEYWORD branch)
        exactly: analyzer tokens, the same ``max(slop - 1, 0)`` whoosh-to-
        tantivy slop mapping, and it never consults ``spec.multitoken``,
        because that policy governs a multi-token bare *Term* value, not a
        quoted phrase's words (a phrase is never "the first word" or "all
        words present in any order").

        Falls back to ``index.parse_query`` on the same terms as
        ``_emit_json_term`` when the installed tantivy-py can't address a
        JSON subpath directly (see ``_json_paths_supported``). That single
        quoted-leaf carve-out has no query-string syntax tantivy-py 0.26
        honors for slop (verified directly: appending ``~N`` to the quoted
        phrase does not change the resulting query's slop away from 0), so
        an explicit whoosh slop is silently unsupported/ignored there
        (DIVERGENCES.md entry 22); the tokens are always joined in full
        (never truncated to the first token), since Multitoken never applies
        to a Phrase node on this branch either.
        """
        spec = resolved.spec
        full = resolved.dotted_name
        tokens = self._tokens(spec, text)
        if not tokens:
            return tantivy.Query.empty_query()

        if self._json_paths_supported():
            if len(tokens) == 1:
                # tantivy rejects a single-word phrase query; a term query is
                # the exact equivalent anyway (mirrors the plain-field path).
                return tantivy.Query.term_query(self.schema, full, tokens[0])
            mapped_slop = max(slop - 1, 0)
            words: list[str | tuple[int, str]] = list(tokens)
            return tantivy.Query.phrase_query(self.schema, full, words, slop=mapped_slop)

        query_text = " ".join(tokens)
        escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
        return self.index.parse_query(f'{full}:"{escaped}"', default_field_names=[spec.name])

    def _drop_check_tokens(self, field: FieldRef | None, text: object) -> list[str] | None:
        """Tokens ``text`` (for ``field``) analyzes to, for the zero-token-
        drop check only. Shared by the ``Term`` and ``Phrase`` branches of
        ``_node_drops``, since both need the identical answer to "does this
        field kind participate in analysis-based dropping at all": TEXT and
        KEYWORD do (plain or JSON subpath), U64/BOOLEAN_EXISTS/DATE never
        do, matching exactly how ``visit_term``/``_emit_json_term`` and
        ``visit_phrase``/``_emit_json_phrase`` dispatch on kind. The Phrase
        branch previously skipped this restriction entirely and tokenized
        every kind, silently dropping e.g. a U64 phrase whose text happened
        to analyze to nothing under an analyzer shared across kinds, even
        though standalone emission of the identical node dispatches on kind
        and never consults those tokens.

        Returns ``None`` when the field's kind is not subject to the drop
        policy (its emission should just run normally, including raising
        for an unresolvable field). Reuses the exact same JSON-subpath
        resolution and tokenization (``_tokens``) that emission uses, so
        this is a read-only preview of what emission will do, not a second
        implementation of it.

        A future explicit analysis pipeline stage that subsumes this
        predicate must preserve the same kind-dispatch rule, and the test
        cases that pin it.
        """
        if field is not None and field.json_path is not None:
            resolved = self.registry.resolve(field)
            if resolved is not None:
                return self._tokens(resolved.spec, text)
            # Not a resolvable JSON subpath (an invalid one, constructed
            # directly rather than by the parser): fall through to
            # ``_resolve`` below, which raises a clear "unknown field"
            # error for it instead of silently treating it as droppable.
        resolved = self._resolve(field)
        if resolved.spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            return self._tokens(resolved.spec, text)
        return None

    def _node_drops(self, node: ast.Node) -> bool:
        """Whether ``node`` would drop entirely from an enclosing And/Or
        group: a zero-token analyzed TEXT/KEYWORD term (plain or JSON
        subpath), a zero-token analyzed TEXT/KEYWORD phrase (plain or JSON
        subpath), a nested And/Or group whose own children all drop, or any
        of those under one or more transparent ``Boosted`` layers. A
        U64/BOOLEAN_EXISTS/DATE term or phrase never drops via analysis,
        regardless of whether its field's spec carries an analyzer.

        A pure predicate: it only tokenizes (``_drop_check_tokens``/
        ``_tokens``), never builds a query, so ``_group_child`` can check
        drop-ness without paying for a full, discarded emission of the
        subtree first (issue #6: the previous approach re-emitted every
        surviving subtree once per enclosing nesting level, doubling work
        per level of alternating And/Or nesting, exponential in depth).
        Every node is still visited/emitted at most once overall: this
        predicate only tokenizes, and ``_group_child`` calls ``self.visit``
        at most once, for the (at most one) top-level call on a given node.
        """
        if isinstance(node, ast.Boosted):
            return self._node_drops(node.child)
        if isinstance(node, ast.Term):
            tokens = self._drop_check_tokens(node.field, node.text)
            return tokens is not None and not tokens
        if isinstance(node, ast.Phrase):
            tokens = self._drop_check_tokens(node.field, node.text)
            return tokens is not None and not tokens
        if isinstance(node, (ast.And, ast.Or)):
            return all(self._node_drops(c) for c in node.children)
        return False

    def _group_child(self, child: ast.Node) -> tantivy.Query | None:
        """Visit a direct child of an And/Or group.

        Returns ``None`` when ``child`` drops entirely (see
        ``_node_drops``): such a node is dropped from its enclosing group
        entirely (whoosh's own behavior when a field's analyzer consumes a
        value completely, e.g. an all-stopword value). Without the
        nested-group case, a group like ``invoice AND ("the" OR "a")``
        under a stopword analyzer would emit the inner ``Or`` as a live
        ``Query.empty_query()`` (see ``visit_or``'s "no children survived"
        branch) which, as a required clause of the outer ``And``, would
        wrongly match nothing instead of just dropping out and leaving
        "invoice" to match on its own.
        """
        if self._node_drops(child):
            return None
        return self.visit(child)

    # -- leaves --------------------------------------------------------

    def visit_nothing(self, node: ast.Nothing) -> tantivy.Query:
        return tantivy.Query.empty_query()

    def _exists_query(self, resolved: ResolvedField) -> tantivy.Query:
        """Build an "exists" query for ``resolved.spec``: does this field
        have a value at all on a given document?

        Shared by ``visit_every`` (a bare ``field:*``) and BOOLEAN_EXISTS
        term emission (``visit_term``, for a field whose ``exists_target``
        is ``resolved.spec``), so the two stay consistent about what
        "exists" means for a given field kind rather than drifting into two
        answers for the same question. Dispatches purely on the strategy
        resolved by ``FieldRegistry`` at construction time; this method
        contains no field-kind or fastness logic of its own. A registry that
        accepted a BOOLEAN_EXISTS spec guarantees its target resolves to a
        strategy, since ``FieldRegistry`` rejects one whose target has none.

        Takes the full ``ResolvedField`` (not a bare spec) per this module's
        general contract, and honors ``resolved.json_path`` for the
        ``FAST_JSON_FIELD`` strategy: a subpath-carrying resolution checks
        only that subpath's fast column (``resolved.dotted_name``), not
        "does any subpath of this field have a value" (issue #29). The
        ``TERM_SCAN`` strategy and the final "no strategy" error both name
        ``resolved.dotted_name`` too, so a non-fast JSON subpath's error
        message names the dotted form the user actually typed rather than
        the bare field name.
        """
        spec = resolved.spec
        strategy = self.registry.exists_strategy(spec)
        if strategy is ExistsStrategy.FAST_FIELD:
            # exists_query is a cheap fast-field presence check.
            return tantivy.Query.exists_query(spec.name)
        if strategy is ExistsStrategy.FAST_JSON_FIELD:
            if resolved.is_subpath:
                # Checking a specific subpath's own fast column: no
                # json_subpaths flag needed, the dotted name already
                # addresses exactly that column (verified against a live
                # tantivy index, including multi-level subpaths).
                return tantivy.Query.exists_query(resolved.dotted_name)
            # Whole-field existence: any subpath having a value counts.
            # A JSON fast field's subpath columns are only checked with
            # json_subpaths=True; without it, exists_query never finds a
            # value (issue #7).
            return tantivy.Query.exists_query(spec.name, json_subpaths=True)
        if strategy is ExistsStrategy.TERM_SCAN:
            # Non-fast TEXT/KEYWORD fields: "has any term at all" via a
            # regex that matches every term in the field's dictionary.
            return tantivy.Query.regex_query(self.schema, spec.name, ".*")
        # No resolved strategy (a non-fast field of any other kind, e.g.
        # U64, DATE, DATETIME, BOOLEAN_EXISTS, a non-fast JSON subpath, ...):
        # regex_query only matches against a tantivy text/string field, so a
        # fallback here would build a query that dies at tantivy search time
        # rather than emit time. Report it clearly instead, naming the
        # dotted form (spec.name for a plain field, "spec.name.json_path"
        # for a subpath) so the message matches what the user actually
        # typed rather than a bare field name that was never reachable
        # syntax to begin with (issue #29's second symptom).
        raise UnsupportedQueryError(
            f"field {resolved.dotted_name!r} ({spec.kind.name}) has no way to"
            f" match 'exists' while non-fast: mark it fast=True to support"
            f" '{resolved.dotted_name}:*'"
        )

    def visit_every(self, node: ast.Every) -> tantivy.Query:
        if node.field is None:
            return tantivy.Query.all_query()
        resolved = self._resolve(node.field)
        if resolved.spec.kind is FieldKind.BOOLEAN_EXISTS:
            # A BOOLEAN_EXISTS field has no physical column of its own to
            # check "exists" against; "existence" only ever means its
            # exists_target's, same redirect as visit_term/visit_phrase's
            # BOOLEAN_EXISTS branches (issue #16).
            resolved = self._resolve(FieldRef(resolved.spec.exists_target))  # type: ignore[arg-type]
        return self._exists_query(resolved)

    def visit_errorleaf(self, node: ast.ErrorLeaf) -> tantivy.Query:
        raise QueryEmitError(
            f"cannot emit query: {node.diagnostic.message}", diagnostic=node.diagnostic
        )

    def visit_term(self, node: ast.Term) -> tantivy.Query:
        if node.field is not None and node.field.json_path is not None:
            resolved = self.registry.resolve(node.field)
            if resolved is not None:
                return self._emit_json_term(resolved, node.text)
            # Falls through to _resolve below, which raises "unknown field"
            # for an invalid subpath reference instead of silently treating
            # it as a plain field.

        resolved = self._resolve(node.field)
        spec = resolved.spec

        if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            tokens = self._tokens(spec, node.text)
            if not tokens:
                # Not inside a group that can drop us: an empty term
                # standalone simply matches nothing.
                #
                # Edge case worth noting explicitly (no code change needed):
                # visit_not wraps this in a bare self.visit(node.child), not
                # _group_child, so `NOT term` for a term whose analyzer drops
                # every token becomes MustNot(empty_query()) here, which
                # matches *every* document (there is nothing for MustNot to
                # exclude). This mirrors the *shape* of ast.normalize()'s
                # Not(Nothing) -> Every rule (see ast.py) but is not evidence
                # of whoosh parity: real whoosh's own Not.normalize() does
                # the opposite (Not(NullQuery) stays NullQuery, i.e. "not
                # nothing" stays "nothing", not "everything"), so this is a
                # confirmed, deliberate divergence from whoosh, not a
                # parity-preserving fallback. See DIVERGENCES.md entry 23.
                return tantivy.Query.empty_query()
            return self._text_term_query(resolved, tokens)

        if spec.kind is FieldKind.U64:
            try:
                value = int(node.text)
            except (TypeError, ValueError) as exc:
                raise QueryEmitError(
                    f"{node.text!r} is not a valid number for {spec.name!r}"
                ) from exc
            return tantivy.Query.term_query(self.schema, spec.name, value)

        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            # exists_target is always a plain (non-JSON) canonical field
            # name: FieldRegistry validates it at construction time.
            target = self._resolve(FieldRef(spec.exists_target))  # type: ignore[arg-type]
            exists = self._exists_query(target)
            if _is_truthy(node.text):
                return exists
            return _boolean_query([(tantivy.Occur.MustNot, exists)])

        if spec.kind is FieldKind.JSON:
            raise QueryEmitError(
                f"field {spec.name!r} is a JSON field; term queries must "
                f"address a subpath (e.g. {spec.name}.<subpath>)"
            )

        raise UnsupportedQueryError(
            f"term emission for field kind {spec.kind.name} is not implemented"
        )

    def visit_phrase(self, node: ast.Phrase) -> tantivy.Query:
        if node.field is not None and node.field.json_path is not None:
            json_resolved = self.registry.resolve(node.field)
            if json_resolved is not None:
                # _emit_json_phrase (not _emit_json_term: a Phrase carries
                # slop and must never consult Multitoken, both unlike a Term,
                # see its docstring) is the JSON-subpath counterpart of this
                # method's own plain-field TEXT/KEYWORD branch below (issue
                # #8: this JSON-subpath dispatch was previously missing from
                # visit_phrase entirely, so a quoted notes.user:"alice"
                # fell through to the plain-field branch and queried the
                # wrong tantivy field name, "notes" instead of "notes.user";
                # issue #34: it then reused _emit_json_term, which dropped
                # slop and wrongly applied Multitoken to phrase text).
                return self._emit_json_phrase(json_resolved, node.text, node.slop)
            # Falls through to _resolve below, which raises "unknown field"
            # for an invalid subpath reference instead of silently treating
            # it as a plain field.

        resolved = self._resolve(node.field)
        spec = resolved.spec

        if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            tokens = self._tokens(spec, node.text)
            if not tokens:
                return tantivy.Query.empty_query()
            if len(tokens) == 1:
                # tantivy rejects a single-word phrase query; a term query is
                # the exact equivalent anyway.
                return tantivy.Query.term_query(self.schema, spec.name, tokens[0])
            # whoosh's slop counts *positions spanned* (slop=1 means
            # adjacent); tantivy's counts *gaps allowed* (slop=0 adjacent).
            slop = max(node.slop - 1, 0)
            words: list[str | tuple[int, str]] = list(tokens)
            return tantivy.Query.phrase_query(self.schema, spec.name, words, slop=slop)

        if spec.kind is FieldKind.U64:
            if node.text == "*":
                # Matches whoosh's NUMERIC.parse_query "*" -> existence
                # special case, same as a quoted term (issue #16).
                return self._exists_query(resolved)
            try:
                value = int(node.text)
            except (TypeError, ValueError) as exc:
                raise QueryEmitError(
                    f"{node.text!r} is not a valid number for {spec.name!r}"
                ) from exc
            if not (0 <= value <= _U64_MAX):
                # Parsed input can no longer carry an out-of-domain u64 value
                # here (issue #9, reopened: the parse-time domain check now
                # also covers the double-quoted/Phrase spelling), but a
                # hand-built ast.Phrase bypasses the parser entirely, so this
                # is a backstop for that case, same rule as term/range.
                raise QueryEmitError(f"{node.text!r} is not a valid number for {spec.name!r}")
            return tantivy.Query.term_query(self.schema, spec.name, value)

        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            # exists_target is always a plain (non-JSON) canonical field
            # name: FieldRegistry validates it at construction time.
            target = self._resolve(FieldRef(spec.exists_target))  # type: ignore[arg-type]
            exists = self._exists_query(target)
            if node.text == "*" or _is_truthy(node.text):
                return exists
            return _boolean_query([(tantivy.Occur.MustNot, exists)])

        if spec.kind is FieldKind.JSON:
            raise QueryEmitError(
                f"field {spec.name!r} is a JSON field; phrase queries must "
                f"address a subpath (e.g. {spec.name}.<subpath>)"
            )

        raise UnsupportedQueryError(
            f"phrase emission for field kind {spec.kind.name} is not implemented"
        )

    def visit_prefix(self, node: ast.Prefix) -> tantivy.Query:
        resolved = self._resolve(node.field)
        self._reject_pattern_incompatible_kind(resolved)
        spec = resolved.spec
        text = str(node.text)
        if spec.pattern_normalizer is not None:
            text = spec.pattern_normalizer(text)
        return tantivy.Query.regex_query(self.schema, spec.name, re.escape(text) + ".*")

    def visit_wildcard(self, node: ast.Wildcard) -> tantivy.Query:
        resolved = self._resolve(node.field)
        self._reject_pattern_incompatible_kind(resolved)
        spec = resolved.spec
        regex = glob_to_regex(str(node.pattern), spec.pattern_normalizer)
        if "(?!)" in regex:
            return tantivy.Query.empty_query()
        return tantivy.Query.regex_query(self.schema, spec.name, regex)

    def _reject_pattern_incompatible_kind(self, resolved: ResolvedField) -> None:
        """Backstop for a hand-built ``Prefix``/``Wildcard`` node whose
        field can't support a pattern query, bypassing the parse-time
        diagnostic in ``parser/default.py``'s ``_wildcard_kind_diagnostic``
        (DIVERGENCES.md entry 30 for the JSON-subpath case, entry 29 for
        the BOOLEAN_EXISTS case).

        Two independent reasons, both raising ``UnsupportedQueryError``
        with a message naming the field rather than letting tantivy-py's
        raw ``ValueError`` leak through ``emit()``'s backstop:

        * A JSON subpath. tantivy stores JSON terms as path-prefixed
          encoded bytes, and there is no tantivy-py API on the pinned
          version that can build a pattern query scoped to one subpath:
          ``Query.regex_query`` against ``resolved.dotted_name`` raises
          ``ValueError`` (not a schema field), and against the bare JSON
          field name it silently matches the whole field's encoded bytes,
          wrong in both directions. Mirrors the text-range backstop in
          ``visit_termrange`` (DIVERGENCES.md entry 5).
        * BOOLEAN_EXISTS. This synthetic field has no schema column of its
          own (``resolved.spec.name`` was never registered with tantivy;
          only its ``exists_target``'s name was), so
          ``Query.regex_query(self.schema, spec.name, ...)`` would raise
          tantivy-py's own "Field ... is not defined in the schema"
          ``ValueError``, a backend-internal message that also
          contradicts the field being queryable at all (``has_tag:true``
          works fine).
        """
        if resolved.is_subpath:
            raise UnsupportedQueryError(
                f"wildcard/prefix patterns are not supported on JSON subpath "
                f"{resolved.dotted_name!r} (DIVERGENCES.md entry 30)"
            )
        if resolved.spec.kind is FieldKind.BOOLEAN_EXISTS:
            raise UnsupportedQueryError(
                f"wildcard/prefix patterns are not supported on boolean-exists "
                f"field {resolved.spec.name!r} (DIVERGENCES.md entry 29)"
            )

    def visit_termrange(self, node: ast.TermRange) -> tantivy.Query:
        raise UnsupportedQueryError("text ranges are not supported (DIVERGENCES.md entry 5)")

    def _range_query(
        self,
        resolved: ResolvedField,
        field_type: tantivy.FieldType,
        lo: int | datetime | None,
        hi: int | datetime | None,
        node: ast.NumericRange | ast.DateRange,
    ) -> tantivy.Query:
        """Shared range_query construction for numeric and date ranges.

        tantivy requires an unbounded side to be *inclusive* (passing
        ``include_* = False`` alongside a ``None`` bound is an error), so the
        node's inclusivity flag is only honored on bounds that actually exist.

        A range open on *both* sides (e.g. ``created:[TO]``, a corner case
        the grammar-aware property fuzzer generated, see
        ``tests/emitter/test_hypothesis_e2e.py``) is a range in name only:
        semantically it means "this field has some value", exactly what
        ``_exists_query`` already answers for a bare ``field:*``
        (``visit_every``) and a BOOLEAN_EXISTS term, so delegate to it
        instead of erroring on a query that parsed without complaint.
        """
        spec = resolved.spec
        if lo is None and hi is None:
            return self._exists_query(resolved)
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
        resolved = self._resolve(node.field)
        spec = resolved.spec
        try:
            lo = None if node.lo is None else int(node.lo)
            hi = None if node.hi is None else int(node.hi)
        except (TypeError, ValueError) as exc:
            raise QueryEmitError(
                f"numeric range bound is not a valid number for {spec.name!r}: {exc}"
            ) from exc
        # Currently numeric fields are U64 only.
        return self._range_query(resolved, tantivy.FieldType.Unsigned, lo, hi, node)

    def visit_daterange(self, node: ast.DateRange) -> tantivy.Query:
        resolved = self._resolve(node.field)
        spec = resolved.spec
        try:
            lo = None if node.lo is None else _to_naive_utc(node.lo)
            hi = None if node.hi is None else _to_naive_utc(node.hi)
        except AttributeError as exc:
            bad = node.lo if node.lo is not None and not hasattr(node.lo, "tzinfo") else node.hi
            raise QueryEmitError(
                f"date range bound {bad!r} is not a valid datetime for {spec.name!r}"
            ) from exc
        return self._range_query(resolved, tantivy.FieldType.Date, lo, hi, node)

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

    def _binary_operands(
        self, left: ast.Node, right: ast.Node
    ) -> tuple[tantivy.Query | None, tantivy.Query | None]:
        """Emit both operands of a binary operator, dropping zero-token ones.

        An operand that analyzes to nothing drops out exactly as it would as
        an And/Or child, and the caller keeps whatever survives. Whoosh
        reaches the same result by a different route: it discards such a term
        while parsing, so the operator never gets built and the surviving
        operand stands alone.
        """
        return self._group_child(left), self._group_child(right)

    def visit_andnot(self, node: ast.AndNot) -> tantivy.Query:
        positive, negative = self._binary_operands(node.positive, node.negative)
        if positive is None or negative is None:
            return self._lone_operand(positive, negative)
        clauses = [(tantivy.Occur.Must, positive), (tantivy.Occur.MustNot, negative)]
        return _boolean_query(clauses)

    def visit_andmaybe(self, node: ast.AndMaybe) -> tantivy.Query:
        required, optional = self._binary_operands(node.required, node.optional)
        if required is None or optional is None:
            return self._lone_operand(required, optional)
        clauses = [(tantivy.Occur.Must, required), (tantivy.Occur.Should, optional)]
        return _boolean_query(clauses)

    def visit_require(self, node: ast.Require) -> tantivy.Query:
        scored, filter_only = self._binary_operands(node.scored, node.filter_only)
        if scored is None or filter_only is None:
            return self._lone_operand(scored, filter_only)
        clauses = [
            (tantivy.Occur.Must, scored),
            (tantivy.Occur.Must, tantivy.Query.const_score_query(filter_only, 0.0)),
        ]
        return _boolean_query(clauses)

    @staticmethod
    def _lone_operand(left: tantivy.Query | None, right: tantivy.Query | None) -> tantivy.Query:
        """The surviving operand, or an empty query when both dropped."""
        survivor = left if left is not None else right
        return survivor if survivor is not None else tantivy.Query.empty_query()

    def visit_boosted(self, node: ast.Boosted) -> tantivy.Query:
        child = self.visit(node.child)
        return tantivy.Query.boost_query(child, node.boost)


def emit(
    node: ast.Node,
    *,
    index: tantivy.Index,
    registry: FieldRegistry,
) -> tantivy.Query:
    """Emit a ``tantivy.Query`` for ``node`` against ``registry``."""
    return TantivyEmitter(index=index, registry=registry).emit(node)
