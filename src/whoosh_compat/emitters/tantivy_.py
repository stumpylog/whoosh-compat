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

import contextlib
import dataclasses
import re
import weakref
from collections.abc import Iterator
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from typing import NoReturn

import tantivy

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.errors import cause_for
from whoosh_compat.fields import ExistsStrategy
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import Multitoken
from whoosh_compat.fields import PatternNormalizer
from whoosh_compat.fields import ResolvedField

_FALSY_TEXT = ("f", "false", "no", "0")
_U64_MAX = 2**64 - 1

# _json_paths_supported()'s probe result, cached per FieldRegistry rather
# than per TantivyEmitter instance: emit() (the module-level function)
# builds a fresh TantivyEmitter for every single call, so a cache living on
# `self` never survives past the one emit() call that populated it, and the
# real term_query("probe") call this guards against ran on every emitted
# query for any registry with a JSON field. A FieldRegistry, by contrast,
# is the long-lived object a host builds once and reuses across every
# emit() call (ARCHITECTURE.md), so caching there makes the probe run once
# per registry for the registry's whole lifetime instead.
# Deliberately *not* a single process-wide flag: whether the installed
# tantivy-py's term_query can resolve a JSON subpath is, in the design this
# probe assumes, a capability of the tantivy-py build alone and so
# genuinely registry-independent -- but the probe itself asks the question
# by querying a specific field drawn from a specific registry against a
# specific index's schema, so a registry whose schema has drifted on that
# probed field would get a false answer for the wrong reason (a missing
# field, not an unsupported dotted path). A single shared flag would let
# that one bad probe poison every *other*, undrifted registry's answer too;
# scoping to the registry confines a bad probe to the registry that caused
# it, same as the pre-fix per-instance cache did within its own one-call
# lifetime.  A `WeakKeyDictionary` is used (rather than an attribute on
# `FieldRegistry` itself) to keep this tantivy-specific concern out of
# `fields.py`, and lets the entry disappear on its own once a registry is
# no longer referenced anywhere else, instead of accumulating for the life
# of the process.
_json_paths_supported_cache: weakref.WeakKeyDictionary[FieldRegistry, bool] = (
    weakref.WeakKeyDictionary()
)


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


def _is_intable(value: object) -> bool:
    """Whether ``int(value)`` succeeds.

    Used only to name the offending bound in a numeric range's diagnostic,
    the same way ``visit_daterange`` re-tests its bounds for a ``tzinfo``
    attribute to name the offending date.
    """
    try:
        int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    return True


# Identity sentinel for "this bracket class matches nothing" (an empty
# class, e.g. after a reversed range removes itself). The VALUE is the
# lookahead fragment CPython's fnmatch.translate emits for the same case,
# but it must never reach tantivy (whose regex engine has no lookahead) and
# must be recognized by IDENTITY, never by substring: a legitimate class
# body can spell the same four characters ("a[(?!)]b" means "a, then one of
# ( ? ! ), then b") and translates to the valid fragment "[(?!)]".
_EMPTY_CLASS = "(?!)"

# _translate_class works at fnmatch's offsets into the class as typed, so the
# per-character fold it applies to the body must not change the length.
_CLASS_FOLD_LENGTH_MSG = "pattern_normalizer changed a bracket class's length"


def _alternatives(normalize: PatternNormalizer, text: str) -> tuple[str, ...]:
    """The forms of ``text`` a term may match, deduplicated, in order.

    ``pattern_normalizer`` returns either one form (a bare ``str``) or
    several (a ``Sequence[str]``); see ``fields.PatternNormalizer``. A bare
    ``str`` is one alternative, *not* one alternative per character: ``str``
    satisfies ``Sequence[str]``, so a normalizer written against the older
    ``Callable[[str], str]`` contract still type-checks against the new one
    and must keep meaning what it always meant.

    Deduplication is correctness, not tidiness. The equal case is the common
    one: every non-stemming field, and every word a stemmer leaves alone,
    yields the same string twice, and the caller must emit one regex branch
    for it rather than "x|x".

    An empty tuple is a real answer and means no term can match this
    fragment; ``_alternation`` turns it into the caller's matches-nothing
    signal.
    """
    result = normalize(text)
    if isinstance(result, str):
        return (result,)
    return tuple(dict.fromkeys(result))


def _alternation(alternatives: Sequence[str]) -> str | None:
    """Regex-escape ``alternatives`` into one fragment, or ``None`` for none.

    One alternative escapes to exactly the fragment this function's
    single-form predecessor emitted, so a normalizer that returns a plain
    string (or none at all) produces a byte-identical regex to before this
    seam existed. Several become one non-capturing alternation group: a
    *local* widening of the one fragment, so a pattern with several literal
    runs costs the sum of their alternatives rather than the product.
    tantivy's regex engine (regex-automata via tantivy_fst) accepts
    ``(?:a|b)``; verified against a live index, not assumed.
    """
    if not alternatives:
        return None
    if len(alternatives) == 1:
        return re.escape(alternatives[0])
    return "(?:" + "|".join(re.escape(alt) for alt in alternatives) + ")"


def _normalize_class_body(body: str, normalize: PatternNormalizer) -> str:
    """``normalize`` applied to a bracket class's body, one character at a time.

    The characters inside a class are index terms exactly like a literal run's
    are: ``title:BILL[I]NG*`` has to fold to ``bill[i]ng.*`` or it matches
    nothing, and real whoosh folds the whole pattern text (class bodies
    included) before handing it to fnmatch. So the body is normalized too, but
    *per character*, and the length is preserved so the caller can keep using
    fnmatch's own offsets into the class it cut out.

    Per character rather than as one string, and skipping any character that
    does not normalize to exactly one alternative of exactly one character,
    because a ``pattern_normalizer`` is not necessarily a pure case fold:
    paperless-ngx supplies ``ascii_fold(text.lower())``, and ascii-folding
    expands single letters into several ("ß" -> "ss", "æ" -> "ae"). Folding a range endpoint
    that way would turn ``[ß-z]`` into ``[ss-z]``: no longer that range, or any
    range. A multi-character fold of a plain class *member* is not the same
    failure but has no good answer either: a class matches exactly one
    character, so "one of a, or the two-character sequence ss" is not
    expressible as a class at all (it would need an alternation, which would
    have to be threaded back out through the whole concatenation). Both cases
    therefore leave the character as the user typed it: that can lose a match
    the host's folded index would have had, which is a bounded and honest
    outcome, where a corrupted class is silently the wrong query.

    A normalizer returning *several* alternatives for a character (a stemmer
    composed with a fold, say) gets the same answer, and for the same reason
    the multi-character case does: a class position matches exactly one
    character, so "one of a, or one of the forms of b" is not expressible as
    a class at all, and widening it into extra class members would change the
    body's length, which every offset below is taken against. An empty
    sequence lands here too: on the literal path it means the fragment can
    never match, but a class member that never matches is just one member
    fewer, and dropping it would again change the length. The one case that
    applies is the one that cannot corrupt anything: exactly one alternative,
    exactly one character long. That is also the case a stemmer produces for
    a single character, which it leaves alone.

    A *single*-character remap onto class syntax ("-" or "\\") is allowed to
    apply, and that is a decision rather than an oversight. It is reachable,
    not exotic: tantivy's ascii_fold (which the host composes into its
    ``pattern_normalizer``) maps the whole dash family and the fullwidth forms
    onto their ASCII counterparts (U+2010..U+2015 and U+FF0D become "-",
    U+FF3C becomes "\\"), so a range whose hyphen is an en dash really does
    become the ASCII range ``[a-z]`` here. Suppressing that would *diverge
    from the oracle*: real whoosh folds the whole pattern text before handing
    it to fnmatch, so fnmatch reads the folded "-" as a range separator too,
    and agreeing with fnmatch is the entire contract of this translation (see
    ``glob_to_regex``).

    Measured under the host's real ``ascii_fold(str.lower)`` over every
    pattern up to length 4 in an alphabet of ASCII and fullwidth class
    characters: with the fullwidth *delimiters* excluded, 16,104 patterns and
    zero disagreements with ``fnmatch.translate(fold(pattern))``; the
    delimiters are a separate, documented qualification (see
    ``_translate_class`` below and DIVERGENCES.md entry 2). Across the whole
    30,940-pattern alphabet tantivy's Rust regex engine refused nothing,
    including the sharp shapes a remap can build ("[]a]", "x[a]b]y",
    "[a\\b]"), so a remap cannot reach PATTERN_TOO_COMPLEX either.
    """
    out: list[str] = []
    for ch in body:
        folded = _alternatives(normalize, ch)
        out.append(folded[0] if len(folded) == 1 and len(folded[0]) == 1 else ch)
    return "".join(out)


def _translate_class(
    pattern: str, i: int, n: int, normalize: PatternNormalizer, last_close: int
) -> tuple[str | None, int]:
    """Translate the bracket expression starting just after a ``[`` at ``i-1``.

    Returns ``(regex_fragment, next_index)``. ``regex_fragment`` is ``None``
    when the ``[`` has no matching ``]``: in that case the caller must treat
    the ``[`` as an ordinary literal character and resume at ``i``.

    ``last_close`` is the index of the pattern's final ``]`` (``-1`` if it has
    none), computed once by the caller: it is what keeps the whole translation
    linear. An unmatched ``[`` used to be answered by scanning forward to the
    end of the pattern, and since the caller then resumes one character later,
    a run of ``[`` cost a full rescan each (12.2 s for 16 K of them, growing
    4x per doubling: a denial of service on a user-supplied wildcard). With
    the pattern's last ``]`` known, "no close exists from here" is an index
    comparison, and the only forward search left is the one that succeeds and
    is paid for once, because the caller resumes past the class it consumed.

    This is a direct port of CPython's ``fnmatch.translate`` bracket handling
    (the ``elif c == '['`` branch), which is the semantics whoosh's
    ``query.Wildcard`` inherits by compiling its pattern with
    ``fnmatch.translate``. It is deliberately *not* simplified: the ``!``
    negation, the leading-``]``-is-a-literal-member rule, and the
    hyphen/backslash escaping inside the class all have to line up with
    fnmatch exactly or globs would silently change meaning.

    The one addition to fnmatch's algorithm is ``normalize``, applied to the
    class body (see ``_normalize_class_body``) *before* the fnmatch logic runs,
    which is where real whoosh's pattern-wide fold would have applied it too.
    Order matters: folding can empty a range that was non-empty unfolded
    (``[Z-a]`` -> ``[z-a]``), and it is fnmatch's own empty-range removal that
    has to see the folded endpoints and collapse it.
    """
    j = i
    if j < n and pattern[j] == "!":
        j += 1
    if j < n and pattern[j] == "]":
        j += 1
    # fnmatch's "scan for the closing ]", with its two leading exemptions (a
    # "!" is negation, and a "]" in first position is a member) intact. The
    # search itself only runs when last_close promises it will find one, so
    # it never walks the tail of a pattern that has no "]" left in it.
    close = pattern.find("]", j) if j <= last_close else -1
    if close < 0:
        return None, i
    j = close

    # Cut the class out of the pattern and fold it. Everything below then
    # works inside this class-sized slice, at fnmatch's indices minus i.
    #
    # Slicing rather than rebuilding the pattern around the folded body is
    # the second half of this function's linearity, and it is the same bug
    # as the scan above wearing different clothes: a full string copy per
    # class made a pattern of many small classes cost O(classes x length)
    # even with the scan fixed ("[a]" x 32,000 spent half a second, and
    # appending an inert tail that joins no class scaled it, which is the
    # signature of a per-class whole-string copy). The fold itself is
    # length-preserving by construction, so the offsets below stay fnmatch's.
    #
    # A leading "!" is negation syntax rather than a term character, so it is
    # left out of the fold and keeps its meaning below.
    #
    # Note the ordering: the class extent (j) is found on the *unfolded* text,
    # so a character the normalizer maps onto "[" or "]" (ascii_fold does map
    # the fullwidth brackets U+FF3B/U+FF3D that way) cannot end this class
    # here, and on glob_to_regex's literal path it cannot open one either,
    # being regex-escaped like any other literal character. The
    # whole-text-fold oracle would let it do both, so this is a deliberate,
    # documented divergence: class delimiters are syntax and are read from
    # what the user actually typed. See DIVERGENCES.md entry 2's second
    # qualification.
    #
    # Known gap, and NOT what the above claims: a normalizer-produced "]" is
    # not an ordinary member on *output*. The escape loop below covers "[&~"
    # but not "]", so a "]" folded out of U+FF3D is emitted bare and closes
    # the emitted class early (a class body of "a", U+FF3D, "b" emits
    # "[a]b]"). Predates this seam and is unchanged by it; recorded here
    # rather than fixed, since fixing it changes emitted regexes and belongs
    # with its own differential.
    stuff = pattern[i:j]
    # Which characters are *term text* is a question about the pattern as
    # typed: the "!" the user wrote is negation syntax, so it stays out of the
    # fold and a normalizer never gets to un-negate a class.
    fold_from = 1 if stuff.startswith("!") else 0
    stuff = stuff[:fold_from] + _normalize_class_body(stuff[fold_from:], normalize)
    if len(stuff) != j - i:  # pragma: no cover - guards an invariant of the fold
        # Load-bearing for correctness rather than tidiness: every offset
        # below is fnmatch's own, taken on the class as typed, so a fold that
        # changed the length would silently shift the hyphen chunking against
        # the text being chunked. _normalize_class_body guarantees this by
        # construction (exactly one output character per input character);
        # this is the seam that would go quiet if a later variant stopped.
        raise AssertionError(_CLASS_FOLD_LENGTH_MSG)

    # Whether the class is *negated* is a different question and takes a
    # different answer: it is read off the folded text, because that is what
    # the "^" rewrite further down reads, and the two have to agree. A
    # normalizer mapping some character onto "!" (ascii_fold maps the
    # fullwidth U+FF01) therefore negates a class the user did not, in both
    # places at once. When these two disagreed, a class of U+FF01, "-", "-",
    # "a" chunked its "-" as an ordinary member here while the rewrite below
    # read the class as negated, emitting "[^-\-a]" (two literal characters)
    # where fnmatch's whole-text fold gives "[^\--a]" (the range "-" through
    # "a"): a different language, silently.
    negated = stuff.startswith("!")

    if "-" not in stuff:
        stuff = stuff.replace("\\", r"\\")
    else:
        chunks = []
        k = 2 if negated else 1
        start = 0
        while True:
            k = stuff.find("-", k)
            if k < 0:
                break
            chunks.append(stuff[start:k])
            start = k + 1
            k = k + 3
        chunk = stuff[start:]
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


def glob_to_regex(pattern: str, normalizer: PatternNormalizer | None) -> str | None:
    """Translate an fnmatch-style glob into a tantivy regex, or ``None``
    when the glob provably matches nothing. Two things say that: an empty
    bracket class, whose empty language poisons the whole concatenation,
    and a normalizer answering a literal run with *no* alternatives, which
    says the same thing about that run. The ``None`` signal is out-of-band
    on purpose: fnmatch's own spelling of the same fact is a lookahead
    fragment tantivy's regex engine cannot parse, and recognizing it inside
    the finished regex by substring would false-positive on a legitimate
    class body spelling the same characters (``a[(?!)]b``).

    Whoosh's ``query.Wildcard`` compiles its pattern with
    ``fnmatch.translate``, so fnmatch: not a naive split on ``*``/``?``:
    is the ground truth for what a whoosh wildcard matches. This function
    reproduces fnmatch's translation with two deliberate changes:

    * Term characters are passed through ``normalizer``
      (``spec.pattern_normalizer``, identity when ``None``) *before* being
      regex-escaped, so a pattern can be case-folded to line up with the
      analyzed/indexed term text. That means whole literal runs and, one
      character at a time, bracket-class bodies (see
      ``_normalize_class_body``); ``*``, ``?`` and the class delimiters
      themselves are syntax and are passed through as such. A run whose
      normalizer offers several alternatives becomes an alternation group
      over them (see ``_alternation``), so a pattern can match a term that
      satisfies the run as typed *or* its stem.
    * No anchoring. ``fnmatch.translate`` emits ``(?s:...)\\z`` framing, and
      newer CPython versions also emit atomic groups (``(?>.*?foo)``) as a
      backtracking optimization. tantivy's regex engine (regex-automata, via
      tantivy_fst) matches the *whole* term by construction and does not
      support atomic groups, so the framing is dropped and ``*`` is emitted as
      a plain ``.*``.
    """
    normalize: PatternNormalizer = normalizer if normalizer is not None else (lambda s: s)

    out: list[str] = []
    literal: list[str] = []
    # Set by flush() when a run has no alternatives at all. Flagged rather
    # than returned because flush() is called from four places and the
    # answer is the same at every one; the loop may keep appending after it
    # is set, since the assembled regex is then discarded unread.
    impossible = False

    def flush() -> None:
        nonlocal impossible
        if literal:
            fragment = _alternation(_alternatives(normalize, "".join(literal)))
            literal.clear()
            if fragment is None:
                impossible = True
            else:
                out.append(fragment)

    i, n = 0, len(pattern)
    # Where the last bracket class could possibly end, computed once here and
    # handed to every _translate_class call (see there for why: it is what
    # makes this a single left-to-right pass instead of a rescan per "["). It
    # is read off the pattern as typed, like the class extents themselves,
    # which is the same deliberate choice documented in _translate_class: a
    # character the normalizer maps onto "]" is a class member, not a close.
    last_close = pattern.rfind("]")

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
            fragment, i = _translate_class(pattern, i, n, normalize, last_close)
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
    if impossible:
        return None
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
        ``QueryError`` (via ``_fail``) from the ``visit_*`` method that
        first notices them. This is the backstop for the rest:
        a hand-built (not just parsed) AST can violate the type contract in
        ways no single ``visit_*`` method specifically checks for, and the
        underlying call then raises one of several bare exceptions instead
        of a documented type. Three shapes are converted here:

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
        backstop.

        The conversion is split by stage, and within the visiting stage by
        exception type, because only one of those cells is a backend
        rejection. ``ast.normalize``/``ast.analyze`` never call tantivy, so
        anything escaping them is a caller-built shape defect
        (``AST_INVALID_SHAPE``). During visiting, an ``AttributeError``,
        ``NotImplementedError`` or ``RecursionError`` likewise never came
        from tantivy (a missing ``visit_*`` method, a ``None`` child, a tree
        too deep to walk), while a bare ``ValueError``/``TypeError`` is
        tantivy-py refusing a query this emitter constructed
        (``BACKEND_REJECTED``). ``QueryError`` derives from ``Exception``
        rather than ``ValueError``, so a ``_fail`` from a nested visitor
        passes through both handlers with its own kind intact.

        That last cell stays genuinely internal because the one condition
        that would otherwise land in it wrongly is peeled off before it gets
        here: every leaf that queries a resolved field wraps its tantivy
        call in ``_reporting_schema_drift``, which reclassifies a *missing
        field* as ``SCHEMA_FIELD_MISSING`` (the operator's problem) and
        re-raises everything else. So a ``ValueError`` reaching this
        backstop has already been shown not to be registry/schema drift,
        which is what makes reporting it as a defect in this library sound.
        """
        try:
            analyzed = ast.analyze(ast.normalize(node), self.registry, default_mode=Multitoken.AND)
        except (
            ValueError,
            TypeError,
            AttributeError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            self._fail(
                DiagnosticKind.AST_INVALID_SHAPE,
                message=f"cannot emit query: {exc}",
            )
        try:
            return self.visit(analyzed)
        except (AttributeError, NotImplementedError, RecursionError) as exc:
            self._fail(
                DiagnosticKind.AST_INVALID_SHAPE,
                message=f"cannot emit query: {exc}",
            )
        except (ValueError, TypeError) as exc:
            self._fail(
                DiagnosticKind.BACKEND_REJECTED,
                message=f"cannot emit query: {exc}",
            )

    # -- helpers -----------------------------------------------------

    def _fail(
        self,
        kind: DiagnosticKind,
        *,
        message: str,
        node: ast.Node | None = None,
        resolved: ResolvedField | None = None,
        raw_value: str | None = None,
        divergence: int | None = None,
    ) -> NoReturn:
        """Raise a ``QueryError`` carrying a fully populated ``Diagnostic``.

        Single funnel for emit-time failures so the cause is looked up
        rather than hand-picked per site. ``divergence`` is an argument
        rather than a table lookup because it varies by field kind for
        ``TEXT_RANGE`` and spans two entries for ``AST_PATTERN_ON_KIND``.

        ``field`` is rebuilt in structured form (canonical name plus
        subpath) rather than from ``resolved.dotted_name``, because
        ``FieldRef("notes.user")`` and ``FieldRef("notes", "user")`` are
        not equal even though they stringify alike: the dotted spelling
        would leave a host reading ``diagnostic.field.json_path`` with the
        subpath at parse time and ``None`` at emit time for the same field.
        """

        raise QueryError(
            Diagnostic(
                kind=kind,
                cause=cause_for(kind),
                message=message,
                startchar=node.startchar if node is not None else None,
                endchar=node.endchar if node is not None else None,
                field=(
                    FieldRef(resolved.spec.name, resolved.json_path)
                    if resolved is not None
                    else None
                ),
                field_kind=resolved.spec.kind if resolved is not None else None,
                raw_value=raw_value,
                divergence=divergence,
            )
        )

    def _resolve(self, field: FieldRef | None) -> ResolvedField:
        if field is None:
            self._fail(
                DiagnosticKind.AST_UNFIELDED_TERM,
                message="cannot emit an unfielded term",
            )
        resolved = self.registry.resolve(field)
        if resolved is None:
            self._fail(
                DiagnosticKind.AST_UNKNOWN_FIELD,
                message=f"unknown field {str(field)!r}",
                raw_value=str(field),
            )
        return resolved

    def _json_paths_supported(self) -> bool:
        """Whether the installed tantivy-py's ``Query.term_query`` can address
        a JSON subpath directly (cached once per ``FieldRegistry``, in
        ``_json_paths_supported_cache``, not per emitter instance: ``emit()``
        builds a fresh ``TantivyEmitter`` on every call, so a cache living on
        ``self`` never survives past that one call).

        Probes with the first JSON-kind field/subpath found in the registry:
        JSON path resolution in ``term_query`` is a schema-level capability of
        the installed tantivy-py, not something that varies per field, so one
        probe per registry is representative for all JSON fields on it.
        Retires itself once https://github.com/quickwit-oss/tantivy-py/pull/716
        ships: the probe starts succeeding and ``_emit_json_term`` stops
        taking the ``parse_query`` branch below.
        """
        cached = _json_paths_supported_cache.get(self.registry)
        if cached is not None:
            return cached
        probe_path = None
        for spec in self.registry:
            if spec.kind is FieldKind.JSON and spec.subpaths:
                probe_path = f"{spec.name}.{next(iter(spec.subpaths))}"
                break
        if probe_path is None:
            # No JSON fields registered: the probe result is moot.
            supported = False
        else:
            try:
                tantivy.Query.term_query(self.schema, probe_path, "probe")
                supported = True
            except ValueError:
                supported = False
        _json_paths_supported_cache[self.registry] = supported
        return supported

    def _emit_json_term(
        self, resolved: ResolvedField, text: object, node: ast.Node
    ) -> tantivy.Query:
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
            # The probe inside targets resolved.spec.name (the JSON field
            # itself), not `full`: a dotted subpath is never a schema field
            # in its own right, so it can only answer the base field's
            # presence, which is exactly the drift condition.
            with self._reporting_schema_drift(resolved, node):
                return tantivy.Query.term_query(self.schema, full, token)

        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        with self._reporting_schema_drift(resolved, node):
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
                with self._reporting_schema_drift(resolved, node):
                    return tantivy.Query.term_query(self.schema, full, words[0])
            mapped_slop = max(node.slop - 1, 0)
            w: list[str | tuple[int, str]] = list(words)
            with self._reporting_schema_drift(resolved, node):
                return tantivy.Query.phrase_query(self.schema, full, w, slop=mapped_slop)

        query_text = " ".join(words)
        escaped = query_text.replace("\\", "\\\\").replace('"', '\\"')
        with self._reporting_schema_drift(resolved, node):
            return self.index.parse_query(
                f'{full}:"{escaped}"', default_field_names=[resolved.spec.name]
            )

    # -- leaves --------------------------------------------------------

    def visit_nothing(self, node: ast.Nothing) -> tantivy.Query:
        return tantivy.Query.empty_query()

    def _exists_query(
        self, resolved: ResolvedField, *, node: ast.Node | None = None
    ) -> tantivy.Query:
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
        # tantivy's exists_query takes no schema and so validates nothing at
        # build time: against a drifted registry the two strategies that use
        # it build a perfectly well-formed query that raises a bare
        # ValueError out of the *searcher*, escaping emit() and its
        # QueryError contract entirely. That is the same "dies at tantivy
        # search time rather than emit time" failure the no-strategy branch
        # below refuses to cause, so the probe runs up front here rather
        # than in a failure path that does not exist.
        if strategy in (
            ExistsStrategy.FAST_FIELD,
            ExistsStrategy.FAST_JSON_FIELD,
        ) and not self._field_in_schema(spec.name):
            self._fail(
                DiagnosticKind.SCHEMA_FIELD_MISSING,
                message=f"field {spec.name!r} is not defined in the index schema",
                node=node,
                resolved=resolved,
            )
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
            # regex that matches every term in the field's dictionary. This
            # one does take a schema, so drift surfaces as a build-time
            # ValueError and the shared wrapper classifies it.
            with self._reporting_schema_drift(resolved, node):
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
        self._fail(
            DiagnosticKind.EXISTS_REQUIRES_FAST,
            message=(
                f"field {resolved.dotted_name!r} ({spec.kind.name}) has no way"
                f" to match 'exists' while non-fast"
            ),
            node=node,
            resolved=resolved,
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
        return self._exists_query(resolved, node=node)

    def visit_errorleaf(self, node: ast.ErrorLeaf) -> tantivy.Query:
        # Re-raises the parse-time record unchanged. Routing this through
        # _fail would restamp an emit-side cause onto a parse diagnostic,
        # destroying the phase information the cause carries.
        raise QueryError(node.diagnostic)

    def visit_term(self, node: ast.Term) -> tantivy.Query:
        if node.field is not None and node.field.json_path is not None:
            resolved = self.registry.resolve(node.field)
            if resolved is not None:
                return self._emit_json_term(resolved, node.text, node)
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
            with self._reporting_schema_drift(resolved, node):
                return tantivy.Query.term_query(self.schema, spec.name, str(node.text))

        if spec.kind is FieldKind.U64:
            try:
                value = int(node.text)
            except (TypeError, ValueError):
                self._fail(
                    DiagnosticKind.AST_BAD_NUMBER,
                    message=f"{node.text!r} is not a valid number for {spec.name!r}",
                    node=node,
                    resolved=resolved,
                    raw_value=str(node.text),
                )
            with self._reporting_schema_drift(resolved, node):
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
            exists = self._exists_query(target, node=node)
            if _is_truthy(node.text):
                return exists
            return _boolean_query([(tantivy.Occur.MustNot, exists)])

        if spec.kind is FieldKind.JSON:
            self._fail(
                DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
                message=(
                    f"field {spec.name!r} is a JSON field; term queries must "
                    f"address a subpath (e.g. {spec.name}.<subpath>)"
                ),
                node=node,
                resolved=resolved,
            )

        self._fail(
            DiagnosticKind.AST_KIND_NOT_IMPLEMENTED,
            message=f"term emission for field kind {spec.kind.name} is not implemented",
            node=node,
            resolved=resolved,
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
                with self._reporting_schema_drift(resolved, node):
                    return tantivy.Query.term_query(self.schema, spec.name, words[0])
            # whoosh's slop counts *positions spanned* (slop=1 means
            # adjacent); tantivy's counts *gaps allowed* (slop=0 adjacent).
            slop = max(node.slop - 1, 0)
            w: list[str | tuple[int, str]] = list(words)
            with self._reporting_schema_drift(resolved, node):
                return tantivy.Query.phrase_query(self.schema, spec.name, w, slop=slop)

        if spec.kind is FieldKind.U64:
            if node.text == "*":
                # Matches whoosh's NUMERIC.parse_query "*" -> existence
                # special case, same as a quoted term.
                return self._exists_query(resolved, node=node)
            try:
                value = int(node.text)
            except (TypeError, ValueError):
                self._fail(
                    DiagnosticKind.AST_BAD_NUMBER,
                    message=f"{node.text!r} is not a valid number for {spec.name!r}",
                    node=node,
                    resolved=resolved,
                    raw_value=str(node.text),
                )
            if not (0 <= value <= _U64_MAX):
                # Parsed input can no longer carry an out-of-domain u64 value
                # here (the parse-time domain check now
                # also covers the double-quoted/Phrase spelling), but a
                # hand-built ast.Phrase bypasses the parser entirely, so this
                # is a backstop for that case, same rule as term/range.
                self._fail(
                    DiagnosticKind.AST_BAD_NUMBER,
                    message=f"{node.text!r} is not a valid number for {spec.name!r}",
                    node=node,
                    resolved=resolved,
                    raw_value=str(node.text),
                )
            with self._reporting_schema_drift(resolved, node):
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
            exists = self._exists_query(target, node=node)
            if node.text == "*" or _is_truthy(node.text):
                return exists
            return _boolean_query([(tantivy.Occur.MustNot, exists)])

        if spec.kind is FieldKind.JSON:
            self._fail(
                DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
                message=(
                    f"field {spec.name!r} is a JSON field; phrase queries must "
                    f"address a subpath (e.g. {spec.name}.<subpath>)"
                ),
                node=node,
                resolved=resolved,
            )

        self._fail(
            DiagnosticKind.AST_KIND_NOT_IMPLEMENTED,
            message=f"phrase emission for field kind {spec.kind.name} is not implemented",
            node=node,
            resolved=resolved,
        )

    def visit_prefix(self, node: ast.Prefix) -> tantivy.Query:
        resolved = self._resolve(node.field)
        self._reject_pattern_incompatible_kind(resolved)
        spec = resolved.spec
        text = str(node.text)
        if spec.pattern_normalizer is None:
            fragment: str | None = re.escape(text)
        else:
            fragment = _alternation(_alternatives(spec.pattern_normalizer, text))
        if fragment is None:
            # The normalizer offered no form this run could take, the same
            # "provably matches nothing" answer glob_to_regex spells as None.
            return tantivy.Query.empty_query()
        return self._regex_query(resolved, fragment + ".*", node)

    def visit_wildcard(self, node: ast.Wildcard) -> tantivy.Query:
        resolved = self._resolve(node.field)
        self._reject_pattern_incompatible_kind(resolved)
        spec = resolved.spec
        regex = glob_to_regex(str(node.pattern), spec.pattern_normalizer)
        if regex is None:
            # The glob provably matches nothing (empty bracket class).
            return tantivy.Query.empty_query()
        return self._regex_query(resolved, regex, node)

    def _regex_query(self, resolved: ResolvedField, regex: str, node: ast.Node) -> tantivy.Query:
        """Build a regex query from a user-derived pattern.

        tantivy caps a compiled regex at 1000 states, and a pattern short
        enough to type by hand can exceed that (a term followed by ~100
        single-character wildcards, or a ~1000-character prefix; a fragment
        with two distinct alternatives roughly halves that last budget,
        measured at 981 characters plain against 491 for a two-branch
        alternation, since each branch compiles its own states). That is
        unusual input, not a defect in this library or in the caller's AST,
        so it must not reach ``emit``'s internal-error backstop: a host
        maps ``INTERNAL`` to a 500, and this deserves a 400.

        Only the two pattern visitors route through here. The fixed ``.*``
        that ``_exists_query`` compiles for a TERM_SCAN field carries no
        user input and cannot hit the cap, so it calls ``regex_query``
        directly under the shared drift wrapper instead of coming through
        this method and risking a bogus ``PATTERN_TOO_COMPLEX``.

        The cap is not the only thing ``Query.regex_query`` reports as a
        bare ``ValueError``, though: a field this library's registry knows
        but the tantivy schema does not raises the same type. That is a
        registry/schema mismatch, an operator's problem and not the query
        text's, so it gets ``SCHEMA_FIELD_MISSING`` (``MISCONFIGURED``)
        rather than being frozen into ``PATTERN_TOO_COMPLEX`` forever or
        blamed on this library as a ``BACKEND_REJECTED`` 500. That split is
        ``_reporting_schema_drift``'s job, shared with every other leaf
        that queries a resolved field, so the paths cannot drift in what
        they consider "missing". The probe runs only in the failure path,
        so the common case stays a single call.

        What is left once the drift case is peeled off really is the state
        cap, which is why this method's own handler can name it directly.
        """
        try:
            with self._reporting_schema_drift(resolved, node):
                return tantivy.Query.regex_query(self.schema, resolved.spec.name, regex)
        except ValueError as exc:
            # Only reached when the drift probe said the field IS present,
            # since _reporting_schema_drift raises QueryError (not a
            # ValueError) when it isn't.
            self._fail(
                DiagnosticKind.PATTERN_TOO_COMPLEX,
                message=f"pattern is too complex for the backend to compile: {exc}",
                node=node,
                resolved=resolved,
            )

    def _field_in_schema(self, name: str) -> bool:
        """Whether ``name`` is a field of the index's tantivy schema.

        ``tantivy.Schema`` exposes no introspection at all on the pinned
        version, so the only available probe is a query construction that
        the missing-field condition rejects and nothing else does. A
        ``.*`` regex query is that construction: it builds against a field
        of *any* kind (tantivy is happy to compile a regex over a numeric
        or date column's encoded term bytes, which is why ``_exists_query``
        can use the same call for a TERM_SCAN existence check) and raises
        ``ValueError`` only when the field name is absent.

        The kind-independence is load-bearing, because every caller passes
        a field whose kind it has not constrained. An empty-string *term*
        query would not do: it succeeds only on a text or string field and
        raises a *type* error on a U64, DATE or JSON one, so it would
        report a field that is present as missing and relabel a genuine
        library defect as the operator's problem.

        ``name`` must be a canonical spec name, never a JSON dotted path: a
        subpath is not a schema field in its own right (``notes.user``
        probes as absent even when ``notes`` is present), so passing one
        would report every JSON subpath query as schema drift.

        ``tests/emitter/test_schema_drift.py``'s
        ``test_other_value_errors_are_still_internal`` monkeypatches
        ``term_query`` to fail *while* this probe keeps succeeding, so it
        depends on this call staying a different tantivy-py entry point
        than the one under test. Changing it to ``term_query`` would break
        that test loudly rather than silently, but change both together.
        """
        try:
            tantivy.Query.regex_query(self.schema, name, ".*")
        except ValueError:
            return False
        return True

    @contextlib.contextmanager
    def _reporting_schema_drift(
        self, resolved: ResolvedField, node: ast.Node | None = None
    ) -> Iterator[None]:
        """Translate a missing-field ``ValueError`` from tantivy-py into
        ``SCHEMA_FIELD_MISSING``, and let every other one through.

        Wraps the individual tantivy call, never a whole visitor body, so
        the only exceptions it can see come from tantivy itself and not
        from this emitter's own value coercion.

        A registry that knows a field the schema does not is drift the
        operator can fix, and it is by far the most likely way this
        condition arises in a real deployment: a host's field table and its
        schema builder are separate declarations that can fall out of step.
        Reaching ``emit``'s generic backstop instead would report it as
        ``BACKEND_REJECTED``/``INTERNAL`` and blame this library for the
        host's configuration.

        The narrowness matters as much as the coverage. Only the condition
        the schema probe actually confirms is reclassified; any other
        ``ValueError`` from the same call is re-raised untouched and still
        reaches the backstop as ``BACKEND_REJECTED``, because tantivy-py
        rejecting a query this emitter built for any *other* reason really
        is a defect here. Swallowing those into ``MISCONFIGURED`` would
        hide library bugs behind a 400, which is worse than the gap this
        closes.
        """
        try:
            yield
        except ValueError as exc:
            if not self._field_in_schema(resolved.spec.name):
                self._fail(
                    DiagnosticKind.SCHEMA_FIELD_MISSING,
                    message=f"cannot emit query: {exc}",
                    node=node,
                    resolved=resolved,
                )
            raise

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
        * A bare JSON field (no subpath). ``AST_JSON_NEEDS_SUBPATH``,
          mirroring ``visit_term``/``visit_phrase``'s identical cell: the
          ref is malformed addressing (a JSON field is only queryable
          through a subpath), not a backend limitation.
        * Any other kind (U64, DATE, DATETIME, and every future
          ``FieldKind`` member until explicitly classified here).
          Reachable only from a hand-built node: query text gets a
          parse-time ``PATTERN_ON_NUMERIC``/``PATTERN_ON_BOOLEAN_EXISTS``/
          ``PATTERN_ON_SUBPATH`` diagnostic first.
        """
        spec = resolved.spec
        if resolved.is_subpath:
            self._fail(
                DiagnosticKind.AST_PATTERN_ON_KIND,
                message=(
                    f"wildcard/prefix patterns are not supported on JSON subpath "
                    f"{resolved.dotted_name!r}"
                ),
                resolved=resolved,
                divergence=30,
            )
        if spec.kind is FieldKind.BOOLEAN_EXISTS:
            self._fail(
                DiagnosticKind.AST_PATTERN_ON_KIND,
                message=(
                    f"wildcard/prefix patterns are not supported on boolean-exists "
                    f"field {spec.name!r}"
                ),
                resolved=resolved,
                divergence=29,
            )
        if spec.kind is FieldKind.JSON:
            self._fail(
                DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
                message=(
                    f"field {spec.name!r} is a JSON field; pattern queries must "
                    f"address a subpath (e.g. {spec.name}.<subpath>)"
                ),
                resolved=resolved,
            )
        if spec.kind is not FieldKind.TEXT and spec.kind is not FieldKind.KEYWORD:
            self._fail(
                DiagnosticKind.AST_PATTERN_ON_KIND,
                message=f"pattern emission for field kind {spec.kind.name} is not implemented",
                resolved=resolved,
            )

    def visit_termrange(self, node: ast.TermRange) -> tantivy.Query:
        """Refuse a lexicographic range, naming the divergence for its kind.

        The field is resolved first, so an unresolvable one reports
        ``AST_UNKNOWN_FIELD``: the more specific failure. The divergence
        then follows the resolved kind, since DIVERGENCES.md entry 5 is
        scoped to the ranges that worked in whoosh (TEXT and KEYWORD);
        a JSON subpath range is entry 30's territory, and a range on a
        synthetic boolean-exists field has no whoosh behavior to diverge
        from at all.
        """
        resolved = self._resolve(node.field)
        divergence: int | None
        if resolved.is_subpath:
            divergence = 30
        elif resolved.spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            divergence = 5
        else:
            divergence = None
        self._fail(
            DiagnosticKind.TEXT_RANGE,
            message="text ranges are not supported",
            node=node,
            resolved=resolved,
            divergence=divergence,
        )

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
            return self._exists_query(resolved, node=node)
        with self._reporting_schema_drift(resolved, node):
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
            bad = node.lo if node.lo is not None and not _is_intable(node.lo) else node.hi
            self._fail(
                DiagnosticKind.AST_BAD_NUMBER,
                message=f"numeric range bound is not a valid number for {spec.name!r}: {exc}",
                node=node,
                resolved=resolved,
                raw_value=repr(bad),
            )
        # Currently numeric fields are U64 only.
        return self._range_query(resolved, tantivy.FieldType.Unsigned, lo, hi, node)

    def visit_daterange(self, node: ast.DateRange) -> tantivy.Query:
        resolved = self._resolve(node.field)
        spec = resolved.spec
        try:
            lo = None if node.lo is None else _to_naive_utc(node.lo)
            hi = None if node.hi is None else _to_naive_utc(node.hi)
        except AttributeError:
            bad = node.lo if node.lo is not None and not hasattr(node.lo, "tzinfo") else node.hi
            self._fail(
                DiagnosticKind.AST_BAD_DATE,
                message=f"date range bound {bad!r} is not a valid datetime for {spec.name!r}",
                node=node,
                resolved=resolved,
                raw_value=repr(bad),
            )

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
       ``ErrorLeaf``, and calling ``emit()`` on that raises ``QueryError``
       carrying that very parse-time ``Diagnostic``.
    2. Even with an empty diagnostics list, ``emit()`` can still raise
       ``QueryError`` for a query shape that parses cleanly but
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
        QueryError: ``node`` cannot be turned into a valid query. The
            attached ``Diagnostic`` says why: its ``kind`` names the
            specific condition and its ``cause`` says who can act on it,
            which is what a host should branch on. This covers a tree
            carrying a parse-time ``ErrorLeaf`` (the parse ``Diagnostic``
            is re-raised unchanged), a query shape this emitter
            deliberately does not support (a text range, a wildcard/prefix
            pattern on an incompatible field), a registry that cannot
            answer an ``exists`` check on a non-fast field, a registry that
            knows a field the index schema does not
            (``SCHEMA_FIELD_MISSING``), and the caller-built-AST backstops
            (an unresolvable field, a value that fails a field kind's
            domain check). It also covers the
            two catch-all backstops: ``AST_INVALID_SHAPE`` for a hand-built
            tree with a ``None`` (or otherwise non-node) value standing in
            for a required child, a node type no visitor handles, or a tree
            deep enough to exhaust the interpreter's recursion limit, and
            ``BACKEND_REJECTED`` for a bare ``ValueError``/``TypeError``
            from tantivy-py refusing a query this emitter built.
    """
    return TantivyEmitter(index=index, registry=registry).emit(node)
