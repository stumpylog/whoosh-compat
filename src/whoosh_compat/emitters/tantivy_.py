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

import dataclasses
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
from whoosh_compat.fields import Multitoken
from whoosh_compat.fields import ResolvedField

_FALSY_TEXT = ("f", "false", "no", "0")
_U64_MAX = 2**64 - 1


def _is_truthy(value: object) -> bool:
    """Truthiness for BOOLEAN_EXISTS term text.

    Mirrors the parser's own coercion rule (``parser/default.py``): the
    strings ``f``/``false``/``no``/``0`` (case-insensitive) are falsy, an
    empty string (after stripping) is also falsy, and everything else is
    truthy. By the time a ``Term`` reaches this emitter its text has usually
    already been coerced to ``bool`` by the parser, but this function also
    accepts a raw string so directly-constructed AST nodes (as used in
    tests) behave the same way.
    """
    if isinstance(value, str):
        stripped = value.strip().lower()
        return bool(stripped) and stripped not in _FALSY_TEXT
    return bool(value)


# Identity sentinel for "this bracket class matches nothing" (an empty
# class, e.g. after a reversed range removes itself). The VALUE is the
# lookahead fragment CPython's fnmatch.translate emits for the same case,
# but it must never reach tantivy (whose regex engine has no lookahead) and
# must be recognized by IDENTITY, never by substring: a legitimate class
# body can spell the same four characters ("a[(?!)]b" means "a, then one of
# ( ? ! ), then b") and translates to the valid fragment "[(?!)]".
_EMPTY_CLASS = "(?!)"


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
        # "[]": an empty range never matches. Return the identity sentinel
        # (see _EMPTY_CLASS above), which glob_to_regex turns into its own
        # out-of-band None result.
        return _EMPTY_CLASS, j + 1
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


def glob_to_regex(pattern: str, normalizer: Callable[[str], str] | None) -> str | None:
    """Translate an fnmatch-style glob into a tantivy regex, or ``None``
    when the glob provably matches nothing (it contains an empty bracket
    class, whose empty language poisons the whole concatenation). The
    ``None`` signal is out-of-band on purpose: fnmatch's own spelling of
    the same fact is a lookahead fragment tantivy's regex engine cannot
    parse, and recognizing it inside the finished regex by substring would
    false-positive on a legitimate class body spelling the same characters
    (``a[(?!)]b``).

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
            elif fragment is _EMPTY_CLASS:
                # Identity check, deliberately not a value/substring check
                # (see _EMPTY_CLASS): the whole glob can never match.
                return None
            else:
                flush()
                out.append(fragment)
        else:
            literal.append(c)
    flush()
    return "".join(out)


# tantivy's DateTime is an i64 nanosecond count, giving a representable
# window of roughly [1677-09-21T00:12:43.145Z, 2262-04-11T23:47:16.854Z].
# A range bound outside it is converted with SILENT i64 overflow, wrapping
# modulo 2**64 ns: measured on the pinned tantivy-py, a [3771, 3773) year
# range matched a 2019 document and a [2018, 9999) range matched nothing.
# Bounds are clamped into the window before range_query (see
# visit_daterange); the constants are rounded inward to whole seconds,
# which loses nothing since tantivy-py 0.26 truncates datetimes to whole
# seconds anyway (see _to_naive_utc's note below). Index-time dates are the
# host's responsibility: a document indexed with an out-of-window date
# (or a sub-second instant in the sliver just above the true minimum,
# which second-truncation pushes below it) is already stored wrapped,
# which no query-side handling can repair. See ARCHITECTURE.md's
# date-window paragraph and the carve-out-retirement skill's table row
# for the re-verification condition on tantivy-py bumps.
# Naive UTC by the same contract as _to_naive_utc's output, which these
# are compared against.
_TANTIVY_DATE_MIN = datetime(1677, 9, 21, 0, 12, 44)  # noqa: DTZ001
_TANTIVY_DATE_MAX = datetime(2262, 4, 11, 23, 47, 16)  # noqa: DTZ001


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
        # Cache for _json_paths_supported(): None means "not probed yet".
        self._json_paths_ok: bool | None = None

    def emit(self, node: ast.Node) -> tantivy.Query:
        """Normalize, analyze, and emit ``node``, guaranteeing the documented
        exception contract.

        Running :func:`ast.analyze` here, after :func:`ast.normalize` and
        before visiting, is this tantivy emitter's own choice, not part of
        the generic :class:`~whoosh_compat.emitters.base.Emitter` protocol:
        a hypothetical future backend that defers token analysis to its own
        server could legitimately skip this stage. Once analysis has run,
        this class is a purely structural visitor: no ``visit_*`` method
        below tokenizes anything, decides whether a subtree drops out of an
        enclosing group, or tracks what group a term is nested inside.
        ``default_mode=Multitoken.AND`` matches this library's own default
        top-level group (DIVERGENCES.md entries 7 and 15).

        Most invalid-input shapes get a specific, well-messaged
        ``QueryEmitError``/``UnsupportedQueryError`` from the ``visit_*``
        method that first notices them. This is the backstop for the rest:
        a hand-built (not just parsed) AST can violate the type contract in
        ways no single ``visit_*`` method specifically checks for, and the
        underlying call then raises one of several bare exceptions instead
        of one of the two documented types. Four shapes are converted here:

        * A non-numeric ``Boosted.boost`` or other badly-typed leaf value
          (a kind every field kind can carry, or a future tantivy-py call
          this doesn't yet special-case): a bare ``ValueError``, ``TypeError``
          or ``AttributeError`` from the underlying tantivy-py call.
        * A ``None`` (or otherwise non-node) value standing in for a child
          node, either caught by ``ast.normalize``/``ast.analyze`` while
          walking the tree (a bare ``AttributeError``) or, once past both,
          by ``ast.Visitor.generic_visit`` finding no ``visit_*`` method for
          the value's type (a bare ``NotImplementedError``).
        * A chain deep enough to exhaust the interpreter's recursion limit
          (a bare ``RecursionError``): this emitter's traversal, like
          ``ast.normalize``/``ast.analyze``, walks one Python stack frame
          per nesting level.

        Converting here, once, keeps every individual ``visit_*`` method
        free to just let its own tantivy-py calls raise naturally rather
        than needing its own try/except for cases already covered by this
        backstop. ``ast.normalize``/``ast.analyze`` are called inside the
        same try so a hand-built tree that fails during either stage (not
        just during visiting) still gets the documented contract.
        """
        try:
            analyzed = ast.analyze(ast.normalize(node), self.registry, default_mode=Multitoken.AND)
            return self.visit(analyzed)
        except (
            ValueError,
            TypeError,
            AttributeError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            raise QueryEmitError(f"cannot emit query: {exc}") from exc

    # -- helpers -----------------------------------------------------

    def _resolve(self, field: FieldRef | None) -> ResolvedField:
        if field is None:
            raise QueryEmitError("cannot emit an unfielded term")
        resolved = self.registry.resolve(field)
        if resolved is None:
            raise QueryEmitError(f"unknown field {str(field)!r}")
        return resolved

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
                    probe_path = f"{spec.name}.{next(iter(spec.subpaths))}"
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
        ``_emit_json_phrase`` below, which carries an explicit slop
        (DIVERGENCES.md entry 22).

        By the time a ``Term`` reaches this method, :func:`ast.analyze` has
        already run: a multi-token JSON-subpath value was already resolved
        into an ``And``/``Or``/``Phrase`` of single-token ``Term`` nodes per
        the field's ``Multitoken`` policy, so ``text`` here is always
        exactly one already-analyzed token, and this method has no analysis
        or multitoken decision left to make; it only decides *how* to query
        for that one token. When the installed tantivy-py cannot address a
        JSON subpath via ``term_query`` (see ``_json_paths_supported``), it
        falls back to ``index.parse_query``, the only route currently able
        to reach a JSON subpath at all, escaping backslashes/quotes so the
        token round-trips through the query-string grammar. Because
        analysis already happened structurally, an AND/OR-mode multi-token
        JSON value now reaches this fallback as separate single-token calls
        combined by the ordinary ``visit_and``/``visit_or`` boolean
        combinators, giving true AND/OR semantics on the fallback path too,
        not the single-quoted-leaf collapse this fallback used to be limited
        to (DIVERGENCES.md entry 22, updated).
        """
        full = resolved.dotted_name
        token = str(text)

        if self._json_paths_supported():
            return tantivy.Query.term_query(self.schema, full, token)

        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        return self.index.parse_query(
            f'{full}:"{escaped}"', default_field_names=[resolved.spec.name]
        )

    def _emit_json_phrase(self, resolved: ResolvedField, node: ast.Phrase) -> tantivy.Query:
        """Emit a phrase query for a JSON subpath (``resolved.dotted_name``).

        A separate helper from ``_emit_json_term`` rather than a shared one:
        a Phrase node's semantics genuinely diverge from a Term's here, not
        just in the value passed through (it carries slop, and never
        consults ``Multitoken``: a phrase's words are the phrase, not
        independent tokens a combinator picks among).

        ``node.words`` is already the analyzed token tuple (:func:`ast.analyze`
        never leaves a multi-token TEXT/KEYWORD ``Phrase`` unanalyzed by the
        time this is reached), so this method does no tokenization of its
        own. Falls back to ``index.parse_query`` on the same terms as
        ``_emit_json_term`` when the installed tantivy-py can't address a
        JSON subpath directly (see ``_json_paths_supported``). That single
        quoted-leaf carve-out has no query-string syntax tantivy-py 0.26
        honors for slop (verified directly: appending ``~N`` to the quoted
        phrase does not change the resulting query's slop away from 0), so
        an explicit whoosh slop is silently unsupported/ignored there
        (DIVERGENCES.md entry 22).
        """
        full = resolved.dotted_name
        words = node.words if node.words is not None else (str(node.text),)

        if self._json_paths_supported():
            if len(words) == 1:
                # tantivy rejects a single-word phrase query; a term query is
                # the exact equivalent anyway (mirrors the plain-field path).
                return tantivy.Query.term_query(self.schema, full, words[0])
            mapped_slop = max(node.slop - 1, 0)
            w: list[str | tuple[int, str]] = list(words)
            return tantivy.Query.phrase_query(self.schema, full, w, slop=mapped_slop)

        query_text = " ".join(words)
        escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
        return self.index.parse_query(
            f'{full}:"{escaped}"', default_field_names=[resolved.spec.name]
        )

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
        "does any subpath of this field have a value". The
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
            # value.
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
        # syntax to begin with.
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
            # BOOLEAN_EXISTS branches.
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
            # ast.analyze() has already run by the time a Term reaches this
            # visitor (see emit()'s docstring): a TEXT/KEYWORD Term here
            # always carries exactly one already-analyzed token (a
            # zero-token value was dropped to Nothing() before emission, a
            # multi-token value was already resolved into And/Or/Phrase per
            # its field's Multitoken policy). This method has no tokenizing
            # or drop decision left to make.
            return tantivy.Query.term_query(self.schema, spec.name, str(node.text))

        if spec.kind is FieldKind.U64:
            try:
                value = int(node.text)
            except (TypeError, ValueError) as exc:
                raise QueryEmitError(
                    f"{node.text!r} is not a valid number for {spec.name!r}"
                ) from exc
            return tantivy.Query.term_query(self.schema, spec.name, value)

        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            # FieldRegistry's third pass validates exists_target only as
            # far as: it is registered (by canonical name OR alias), it is
            # not itself BOOLEAN_EXISTS, and it resolves to an
            # ExistsStrategy. It may therefore be an alias, and it may be a
            # fast JSON field (ExistsStrategy.FAST_JSON_FIELD); both shapes
            # emit correctly through _exists_query's strategy dispatch, so
            # nothing here may assume a plain non-JSON canonical name.
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
                # method's own plain-field TEXT/KEYWORD branch below.
                return self._emit_json_phrase(json_resolved, node)
            # Falls through to _resolve below, which raises "unknown field"
            # for an invalid subpath reference instead of silently treating
            # it as a plain field.

        resolved = self._resolve(node.field)
        spec = resolved.spec

        if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            # ast.analyze() has already tokenized this Phrase by the time it
            # reaches this visitor: node.words carries the surviving tokens
            # directly (a zero-token phrase was dropped to Nothing() before
            # emission; a one-token phrase deliberately STAYS a Phrase,
            # matching whoosh's PhrasePlugin, and is mapped to the exactly
            # equivalent term query just below). This method does no
            # tokenizing of its own.
            words = node.words if node.words is not None else (str(node.text),)
            if len(words) == 1:
                # tantivy rejects a single-word phrase query; a term query is
                # the exact equivalent anyway.
                return tantivy.Query.term_query(self.schema, spec.name, words[0])
            # whoosh's slop counts *positions spanned* (slop=1 means
            # adjacent); tantivy's counts *gaps allowed* (slop=0 adjacent).
            slop = max(node.slop - 1, 0)
            w: list[str | tuple[int, str]] = list(words)
            return tantivy.Query.phrase_query(self.schema, spec.name, w, slop=slop)

        if spec.kind is FieldKind.U64:
            if node.text == "*":
                # Matches whoosh's NUMERIC.parse_query "*" -> existence
                # special case, same as a quoted term.
                return self._exists_query(resolved)
            try:
                value = int(node.text)
            except (TypeError, ValueError) as exc:
                raise QueryEmitError(
                    f"{node.text!r} is not a valid number for {spec.name!r}"
                ) from exc
            if not (0 <= value <= _U64_MAX):
                # Parsed input can no longer carry an out-of-domain u64 value
                # here (the parse-time domain check now
                # also covers the double-quoted/Phrase spelling), but a
                # hand-built ast.Phrase bypasses the parser entirely, so this
                # is a backstop for that case, same rule as term/range.
                raise QueryEmitError(f"{node.text!r} is not a valid number for {spec.name!r}")
            return tantivy.Query.term_query(self.schema, spec.name, value)

        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            # FieldRegistry's third pass validates exists_target only as
            # far as: it is registered (by canonical name OR alias), it is
            # not itself BOOLEAN_EXISTS, and it resolves to an
            # ExistsStrategy. It may therefore be an alias, and it may be a
            # fast JSON field (ExistsStrategy.FAST_JSON_FIELD); both shapes
            # emit correctly through _exists_query's strategy dispatch, so
            # nothing here may assume a plain non-JSON canonical name.
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
        if regex is None:
            # The glob provably matches nothing (empty bracket class).
            return tantivy.Query.empty_query()
        return tantivy.Query.regex_query(self.schema, spec.name, regex)

    def _reject_pattern_incompatible_kind(self, resolved: ResolvedField) -> None:
        """Backstop for a hand-built ``Prefix``/``Wildcard`` node whose
        field can't support a pattern query, bypassing the parse-time
        diagnostic in ``parser/default.py``'s ``_wildcard_kind_diagnostic``
        (DIVERGENCES.md entry 30 for the JSON-subpath case, entry 29 for
        the BOOLEAN_EXISTS case).

        The dispatch is closed over the kind axis: only TEXT and KEYWORD
        (whose index terms are the analyzed strings a glob-derived regex
        meaningfully runs against) fall through to ``Query.regex_query``;
        every other cell raises with a message naming the field, rather
        than letting the query reach tantivy, which either raises its own
        backend-internal ``ValueError`` or, worse, *accepts* the regex
        against a non-text column's encoded term bytes (numeric, date,
        and JSON columns all do) and silently matches nothing.

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
        * A bare JSON field (no subpath). ``QueryEmitError``, mirroring
          ``visit_term``/``visit_phrase``'s identical cell: the ref is
          malformed addressing (a JSON field is only queryable through a
          subpath), not a backend limitation.
        * Any other kind (U64, DATE, DATETIME, and every future
          ``FieldKind`` member until explicitly classified here).
          Reachable only from a hand-built node: query text gets the
          parse-time ``UNSUPPORTED_PATTERN`` diagnostic first.
        """
        spec = resolved.spec
        if resolved.is_subpath:
            raise UnsupportedQueryError(
                f"wildcard/prefix patterns are not supported on JSON subpath "
                f"{resolved.dotted_name!r} (DIVERGENCES.md entry 30)"
            )
        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            raise UnsupportedQueryError(
                f"wildcard/prefix patterns are not supported on boolean-exists "
                f"field {spec.name!r} (DIVERGENCES.md entry 29)"
            )
        if spec.kind is FieldKind.JSON:
            raise QueryEmitError(
                f"field {spec.name!r} is a JSON field; pattern queries must "
                f"address a subpath (e.g. {spec.name}.<subpath>)"
            )
        if spec.kind is not FieldKind.TEXT and spec.kind is not FieldKind.KEYWORD:
            raise UnsupportedQueryError(
                f"pattern emission for field kind {spec.kind.name} is not implemented"
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

        # Clamp into tantivy's representable window (see _TANTIVY_DATE_MIN
        # above): a bound past either edge would otherwise wrap modulo
        # 2**64 nanoseconds and match an arbitrary wrong document set. A
        # range lying entirely outside the window can match nothing (no
        # representable instant is inside it); a bound clamped to a window
        # edge becomes inclusive, since the edge stands in for every
        # unrepresentable instant beyond it. whoosh handles these same
        # years correctly, so clamping is what keeps result parity.
        if (lo is not None and lo > _TANTIVY_DATE_MAX) or (
            hi is not None and hi < _TANTIVY_DATE_MIN
        ):
            return tantivy.Query.empty_query()
        if lo is not None and lo < _TANTIVY_DATE_MIN:
            lo = _TANTIVY_DATE_MIN
            node = dataclasses.replace(node, incl_lo=True)
        if hi is not None and hi > _TANTIVY_DATE_MAX:
            hi = _TANTIVY_DATE_MAX
            node = dataclasses.replace(node, incl_hi=True)
        return self._range_query(resolved, tantivy.FieldType.Date, lo, hi, node)

    # -- boolean combinators --------------------------------------------

    def visit_and(self, node: ast.And) -> tantivy.Query:
        # ast.analyze() (via ast.normalize(), which it calls internally
        # before returning) has already dropped any zero-token child and
        # collapsed a fully-emptied And to Nothing() before emission, so
        # every child reaching this visitor is a real, surviving node: no
        # per-child drop check or group-context tracking is needed here.
        children = [self.visit(c) for c in node.children]
        if not children:
            return tantivy.Query.empty_query()
        if len(children) == 1:
            return children[0]
        clauses = [(tantivy.Occur.Must, c) for c in children]
        return _boolean_query(clauses)

    def visit_or(self, node: ast.Or) -> tantivy.Query:
        children = [self.visit(c) for c in node.children]
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
        # ast.analyze() already resolved DIVERGENCES.md entry 23's
        # zero-token-operand rule (an operand that newly dropped to nothing
        # during analysis leaves its sibling standing alone) structurally:
        # by construction, neither node.positive nor node.negative is ever
        # itself a bare Nothing() here (normalize()'s AndNot rule collapses
        # the whole node instead, whenever one genuinely is), so this method
        # only has the ordinary two-clause query left to build.
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
    registry: FieldRegistry,
) -> tantivy.Query:
    """Emit a ``tantivy.Query`` for ``node`` against ``registry``.

    The host contract for safely calling this function has two parts, and
    both must hold, not just the first:

    1. The :class:`~whoosh_compat.errors.Diagnostic` list on the
       :class:`~whoosh_compat.ParseResult` that produced ``node`` must be
       empty. A non-empty list means the tree contains at least one
       ``ErrorLeaf``, and calling ``emit()`` on that raises
       ``QueryEmitError``.
    2. Even with an empty diagnostics list, ``emit()`` can still raise
       ``UnsupportedQueryError`` for a query shape that parses cleanly but
       has no way to execute against tantivy today. The canonical example is
       a text-field range (``title:[a TO b]``): whoosh supported this, but
       tantivy-py has no programmatic text-range API (DIVERGENCES.md entry
       5), so it parses with ``diagnostics == ()`` and only fails once
       ``emit()`` is called.

    A host like paperless-ngx should map *both* of these to an HTTP 400:
    checking ``ParseResult.diagnostics`` alone is not sufficient, since it
    says nothing about the second failure mode. Do not read "diagnostics is
    empty" as "emitting is guaranteed to succeed."

    ``emit()`` always runs ``ast.normalize()`` and then ``ast.analyze()`` on
    its input first; the result reflects that normal, analyzed form, not
    necessarily the literal tree passed in. This makes ``emit(t)`` and
    ``emit(analyze(normalize(t), registry))`` agree by construction: a
    hand-built tree containing a literal empty And/Or group or a
    ``Nothing()`` sibling reaches the same matched-document set either way,
    since both call sites go through the identical normalize-then-analyze
    pipeline before anything is visited (the parser itself never produces
    such a tree, since it always normalizes before ``parse()`` returns, so
    only a hand-built AST passed straight to ``emit()`` could observe a
    difference if this weren't true). Running both stages here closes that
    gap without changing behavior for the ``parse()`` -> ``emit()`` path,
    since renormalizing or re-analyzing an already-normalized, already-
    analyzed tree is a no-op (``ast.analyze()``'s docstring explains why
    that holds by construction, not by convention).

    Raises:
        QueryEmitError: ``node`` (or a value it contains) cannot be turned
            into a valid query: an unresolvable field, a value that fails a
            field kind's domain check, a hand-built tree with a ``None`` (or
            otherwise non-node) value standing in for a required child, a
            hand-built tree deep enough to exhaust the interpreter's
            recursion limit, or any other shape that would otherwise raise a
            bare ``ValueError``, ``TypeError``, ``AttributeError``,
            ``NotImplementedError`` or ``RecursionError`` while building the
            query.
        UnsupportedQueryError: ``node`` describes a query shape this emitter
            deliberately does not support (a text range, a wildcard/prefix
            pattern on an incompatible field, an ``exists`` check on a
            non-fast field with no way to answer it). A sibling of
            ``QueryEmitError`` under the common ``WhooshCompatError`` base,
            not a subclass of it.
    """
    return TantivyEmitter(index=index, registry=registry).emit(node)
