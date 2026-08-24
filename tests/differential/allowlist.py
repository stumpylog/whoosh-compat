"""Intended divergences between whoosh-compat and the real whoosh (v2)
oracle. Each entry documents a query pattern that is *expected* to fail the
structural parity comparison, with a reference to the corresponding numbered
entry in DIVERGENCES.md.

Reference-string prefix convention (never bake a clear whoosh bug into
whoosh-compat for parity's sake):

* ``DIVERGENCES.md entry N``: see that numbered entry in DIVERGENCES.md.
* ``whoosh-bug:``: the oracle's behavior is a confirmed defect in real
  whoosh/paperless-v2 (broken parsing, dropped data, wiring that silently
  no-ops); whoosh-compat keeps its own correct behavior and does NOT
  reproduce the bug.
* ``design:``: a deliberate whoosh-compat design choice/new feature with no
  whoosh equivalent to match (not a bug on either side).
* ``out-of-scope:``: the query exercises something entirely outside
  whoosh-compat's current query surface (e.g. a JSON dotted path, a
  whoosh-compat-only concept with no counterpart in the real-whoosh schema
  being compared against); there is no meaningful oracle comparison to make.

Every reason string MUST also cite an explicit ``DIVERGENCES.md entry N``
somewhere in its text (even a ``whoosh-bug:``/``design:``/``out-of-scope:``
one), matching the prefix's own numbered entry: ``tests/differential/
test_allowlist_xref.py`` mechanically checks this citation exists and names
a real DIVERGENCES.md entry, in both directions (every allowlist entry cites
a real DIVERGENCES.md entry, and every DIVERGENCES.md entry that itself
claims a matching allowlist entry or corpus line actually has one). A
divergence introduced without this paperwork fails that check instead of
silently drifting out of sync.

Strict-xfail semantics (``tests/differential/test_differential.py``'s
``test_matches_oracle``): a query matching one of these entries is not just
skipped. The comparison still runs, and the test asserts the specific
divergent outcome this entry documents actually happened; an allowlisted
query whose comparison unexpectedly succeeds (the trees now match, or the
oracle stops raising) fails the suite, naming the stale entry, rather than
silently continuing to skip a divergence that no longer exists. This is what
each entry's :class:`DivergenceKind` selects between:

* :attr:`DivergenceKind.MISMATCH`: both sides parse cleanly (no
  whoosh-compat diagnostic) and the compared trees are expected to differ.
  The test asserts ``got != expected``. This is the default for nearly
  every entry below.
* :attr:`DivergenceKind.ORACLE_ERROR`: real whoosh itself raises while
  parsing this shape, so there is no oracle query object to compare at all.
  The test asserts the oracle still raises. Two corpus lines exercise
  entries of this kind today: ``has_tag:"true"`` in
  ``tests/differential/corpus_docs.txt`` (the double-quoted-value-on-BOOLEAN
  crash, DIVERGENCES.md entry 38) and ``created:[ TO -7 years]`` in
  ``tests/differential/corpus_realworld.txt`` (the single-bound
  now/relative-offset range crash, DIVERGENCES.md entry 55), so
  ``test_matches_oracle``'s ORACLE_ERROR branch is live code, not a
  provision for the future. Other oracle-crashing shapes (e.g. the
  "NOT NOT alpha" consecutive-bare-NOTs crash documented in DIVERGENCES.md
  entry 35 and the double-quoted-``"*"``-on-BOOLEAN crash in entry 28)
  deliberately have no differential corpus line: see those entries' own
  "no allowlist/corpus triple" notes.

A third outcome, a whoosh-compat parse *diagnostic* (DIVERGENCES.md entry
6: ``Term``/``Phrase`` values whoosh-compat can't parse, e.g. an invalid
date or number, become a structured ``ErrorLeaf`` instead of whoosh's
untyped ``error_query``/``NullQuery``), is deliberately NOT a
:class:`DivergenceKind` here. ``test_matches_oracle`` checks for a
whoosh-compat diagnostic before it ever consults this module's per-entry
:class:`DivergenceKind`, and skips uniformly (not a strict-xfail assertion)
whenever one is present, regardless of which reason (if any) also matched:
a diagnostic means the raw AST contains ``ErrorLeaf`` nodes standing in for
unparseable input, which makes a structural mismatch assertion meaningless
(there's nothing coherent to compare) whether or not the query also happens
to match one of the patterns below. Several entries here (e.g. the unquoted
multi-word natural-date-keyword one) match corpus queries that, in
practice, *always* hit the diagnostic path today rather than reaching a
MISMATCH assertion; that's fine; the entry's pattern still needs to exist to
allowlist any future query shape it matches that does NOT produce a
diagnostic. This diagnostic skip is still counted, not just silently taken:
``test_differential.py``'s ``test_diagnostic_skip_count_matches_corpus``
pins the total number of corpus queries that take it, so a parser change
that starts (or stops) diagnosing a shape is visible as a count change
instead of draining or padding the corpus silently.
"""

from __future__ import annotations

import enum
import re

from whoosh.analysis import STOP_WORDS

from tests.differential.oracle import NATURAL_DATE_KEYWORDS
from tests.differential.oracle import ORACLE_REGISTRY
from whoosh_compat.fields import FieldKind

# Every registered TEXT/KEYWORD field name (canonical and alias), longest
# first so a longer name is never shadowed by a shorter prefix inside a
# regex alternation. These are exactly the kinds whose analyzer can split
# a value into multiple tokens, i.e. the fields that can exhibit the
# entry-15 Multitoken.DEFAULT divergence. Derived from the registry, never
# hand-enumerated: the fuzzers draw their field vocabulary from the same
# registry (strategies.py), so a hand-written list here is guaranteed to
# drift into a latent CI flake the moment a field is added or renamed
# (which is exactly how tag_id/custom_fields_id/viewer_id were once
# missed). test_allowlist_xref.py pins the coverage field by field.
_ANALYZER_SPLIT_FIELDS = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD)
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# A single word StandardAnalyzer analyzes to zero tokens: any of whoosh's
# own STOP_WORDS (case-insensitively, since LowercaseFilter runs before the
# stop filter) or any single word character (minsize=2 drops it). Derived
# from whoosh's live STOP_WORDS set, never hand-enumerated: an earlier
# hand-written ten-word list here silently covered less than a third of the
# real set, and the fuzzer word alphabets can draw any of them (a latent
# flake until the first unlucky draw). Longest-first so a named stopword
# matches itself rather than a shorter prefix; each use site appends its
# own boundary lookahead so a real word merely STARTING with a stopword
# ("thermal", "20th") is not mistaken for one. Shared with
# tests/emitter/result_allowlist.py's entry-23 result-level regex.
# test_allowlist_xref.py pins the coverage word by word.
ZERO_TOKEN_WORD = (
    "(?i:"
    + "|".join(re.escape(w) for w in sorted(STOP_WORDS, key=lambda w: (-len(w), w)))
    + r"|\w)"
)

# Every registered DATETIME field name (and alias), for scoping
# date-range entries. Same no-drift derivation rationale as above.
# Deliberately DATETIME only, matching strategies.DATE_FIELDS: the one
# DATE-kind field (release_date) is a whoosh-compat-only concept with no
# column in the real v2 schema, so its queries belong to entry 37's
# no-oracle-analogue paperwork, not to date-range mechanism entries.
DATE_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.DATETIME
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered comma_values field name (and alias): the fields
# CommaValuesPlugin splits at parse time, i.e. the entry-36 domain.
# Same no-drift derivation rationale as above (entry 36's regex was once
# scoped to 'tag' alone, silently missing its comma siblings).
COMMA_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.comma_values
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered TEXT field name (and alias), for entries scoped to
# analyzer-splitting TEXT values specifically.
TEXT_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.TEXT
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered BOOLEAN_EXISTS field name (and alias): the entry-33
# domain. Same no-drift derivation rationale as above (has_path was once
# missing from a hand-written list of these).
BOOL_EXISTS_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.BOOLEAN_EXISTS
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered NON-JSON field name and alias, for the unknown-field
# exclusion lookahead (a name in this set is KNOWN, so the unknown-field
# demotion mechanism cannot apply to it). JSON-kind names are deliberately
# NOT excluded: a bare JSON field name (attrs:foo) demotes to text exactly
# like an unknown field on both sides, which is entry 15's documented
# second trigger pathway and must stay claimable by this alternative.
# is_shared is appended at the use site: it is deliberately unregistered
# (entry 42) but has its own dedicated entry, which must claim it instead
# of the unknown-field alternative.
REGISTERED_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is not FieldKind.JSON
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered KEYWORD field name (and alias), for excluding them from
# zero-token-word entries: whoosh's KEYWORD analyzer only splits on commas,
# with no stopword/minsize filtering, so a stopword-shaped KEYWORD value is
# NOT zero-token. Derived from the registry for the same no-drift reason
# as _ANALYZER_SPLIT_FIELDS above. Shared with
# tests/emitter/result_allowlist.py.
KEYWORD_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.KEYWORD
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Entry 15's two allowlist regexes below (and only those two) share this
# field-kind-aware model of "does this value survive its field's own
# analyzer as 2+ distinct tokens", derived from whoosh's real tokenizer
# rule instead of an enumerated separator character class. See
# DIVERGENCES.md entry 15 for the Multitoken.DEFAULT mechanism itself, and
# the entry-15 regex comments below for how each piece is used.
#
# A TEXT field's StandardAnalyzer token is whoosh's own
# `\w+(\.?\w+)*` (a run of word characters, where a single interior dot
# glues two runs into one token, matching DIVERGENCES.md entry 46's "a dot
# never splits" finding), then LowercaseFilter, then StopFilter(minsize=2,
# stoplist=STOP_WORDS) drops anything under 2 characters AND anything that
# is (after lowercasing) one of whoosh's own stopwords. The İ exception
# (U+0130, the one character whose str.lower() expands to two codepoints)
# survives minsize after lowercasing even though it is a single codepoint
# here; already established by this entry's own history.
#
# The end of a StandardAnalyzer token, as a zero-width assertion: a word
# run ends where the next character is neither a word character nor a dot
# that itself glues another word run on (whoosh's `\w+(\.?\w+)*` makes
# "the.x" ONE token, not the stopword "the" followed by "x", while a
# trailing dot with no word run after it is not part of the token at all).
_TEXT_TOKEN_END = r"(?!\w|\.\w)"
# One of whoosh's own stopwords spelled out as a whole token. Derived from
# the live STOP_WORDS set for the same no-drift reason as ZERO_TOKEN_WORD
# above, and ordered longest-first so a named stopword matches itself
# rather than a shorter prefix of itself.
_TEXT_STOPWORD_WORD = (
    "(?i:" + "|".join(re.escape(w) for w in sorted(STOP_WORDS, key=lambda w: (-len(w), w))) + ")"
)
_TEXT_STOPWORD = rf"{_TEXT_STOPWORD_WORD}{_TEXT_TOKEN_END}"
_TEXT_SURVIVOR = rf"(?:\w+(?:\.\w+)+|(?!{_TEXT_STOPWORD})\w{{2,}}|İ)"
# A comma_values KEYWORD field splits on a literal comma ONLY, at the
# PARSER level (CommaValuesPlugin), not via the field's content analyzer
# at all -- there is no minimum token length and no dot-gluing rule (a
# comma_values field never even sees "9.90" as two pieces, since there is
# no comma to split on; whoosh's own analyzer keeps it as one glued token
# regardless of field kind).
# A comma_values piece is therefore everything up to the next comma, not
# just a run of word characters: "tag:'abc-ab,x'" splits into "abc-ab"
# and "x", two distinct surviving values, and modelling the piece as
# `\w+` would stop at "abc" and miss the split entirely. What the piece
# may NOT contain are the characters that end a value at the
# query-grammar level before the field's own splitting ever runs:
# whitespace and parens (a bare value's boundaries), either quote (the
# value's own delimiters; a DOUBLE-quoted value is a Phrase node, which
# has no Multitoken.DEFAULT question at all, so admitting `"` would claim
# the agreeing shape `"ab,cd"`), `*`/`?` (WildcardPlugin claims the value
# first) and brackets (range syntax), all for the reasons spelled out for
# `_BARE_FILLER` below. Colon is excluded for the same reason it is
# excluded there, and re-admitted by `_FIELDED_KEYWORD_SURVIVOR` where
# the value already carries an explicit field prefix.
_KEYWORD_SURVIVOR = r"""[^,\s():'"*?\[\]{}]+"""
_FIELDED_KEYWORD_SURVIVOR = r"""[^,\s()'"*?\[\]{}]+"""
_KEYWORD_FILLER = r","
# One whole token StopFilter drops, usable as filler between two TEXT
# survivors: a stopword, or a single isolated word character (shorter than
# minsize=2), excluding the İ exception (İ alone must always be a
# survivor, never droppable filler). Both branches end on a real token
# boundary, so a dot-glued token is never eaten one piece at a time: the
# filler runs below are possessive, and without `_TEXT_TOKEN_END` the
# single-character branch would irrevocably consume "a", "." and "a" out
# of "a.a" before `_TEXT_SURVIVOR` ever got the chance to match the glued
# token as one unit (measured: "zzz:a.a", "zzz:1.9").
_TEXT_DROPPED_TOKEN = rf"(?:{_TEXT_STOPWORD}|(?!İ)\w{_TEXT_TOKEN_END})"
# Filler between two TEXT survivors: any run of characters that is not a
# word character, whitespace, paren, colon, `*`/`?`, or a bracket -- OR a
# whole token StopFilter drops. Colon is excluded because it is a real field-value
# boundary at the query-grammar level, not a literal character within one
# bare value's text. `*`/`?` are excluded because they trigger
# WildcardPlugin at the query-grammar level (the value becomes a
# WildcardNode/PrefixNode, never reaching the field's content analyzer as
# literal text -- measured: "produ*name" would otherwise falsely chain as
# two TEXT survivors "produ"/"name", but the divergence there, if any, is
# entry 2's wildcard-casing mechanism, not this one). Brackets are
# excluded because they are range-syntax delimiters, not literal
# characters within a BARE (unfielded) value's own text -- measured:
# without this exclusion, a query like "created:[2020-01-01 TO
# 2020-12-31]" spuriously chain-matches "2020-12-31]" as if it were a bare
# value fragment, when the actual (correct) divergence there is entry 12's
# date-range tz mechanism; this file's own reason strings must describe
# the actual cause of a divergence, not a coincidentally-true one.
_BARE_FILLER = rf"(?:[^\w\s():'*?\[\]{{}}]+|{_TEXT_DROPPED_TOKEN})"
# Filler between two TEXT survivors of an EXPLICITLY FIELDED value
# ("title:abc:ab", "title:'hello:90'"). Identical to `_BARE_FILLER` except
# that colon is allowed: the field prefix has already been consumed, so an
# interior colon is literal text handed to the field's analyzer, never the
# query-grammar field-value boundary it would be at the start of a BARE
# value. Brackets stay excluded for the same range-syntax reason as
# `_BARE_FILLER`.
_FIELDED_FILLER = rf"(?:[^\w\s()'*?\[\]{{}}]+|{_TEXT_DROPPED_TOKEN})"
# Filler for the unknown-field-colon alternative: colon IS allowed here
# (do_fieldnames merges consecutive rejected field-name candidates into
# one literal string, so an interior colon is just more literal text, not
# a query boundary -- measured: "dat:'-1 year to now'" and
# "attrs.user:alice"-style chains depend on this). Brackets are NOT
# excluded here (unlike _BARE_FILLER): a bracket can legitimately appear
# in literal demoted text ("document_type:[Receipt]", a live corpus line,
# has no "to" inside its brackets and must stay claimed); the separate
# range-lookahead exclusion below (`_RANGE_LOOKAHEAD`) is what excludes
# the genuinely-a-range case instead.
_UF_FILLER = rf"(?:[^\w\s()'*?]+|{_TEXT_DROPPED_TOKEN})"
# Same as _UF_FILLER but also excludes the single-quote character: used
# for the interior of a single-quoted unknown-field value, where the
# closing quote must terminate the match rather than being consumed as
# filler.
_UF_QUOTED_FILLER = rf"(?:[^\w()'*?]+|{_TEXT_DROPPED_TOKEN})"


def _survivor_chain(survivor: str, filler: str) -> str:
    """A run of 2+ non-identical SURVIVOR tokens separated by FILLER, with
    optional TRAILING filler so a dropped short piece at the end does not
    block reaching the value's true boundary (measured: "वर्तमान" --
    Devanagari, tokenizing to वर/तम/न -- needs the trailing 1-character
    "न" piece consumable as filler to reach the end of the word at all).

    Deliberately NO leading filler tolerance, unlike `_survivor_tail`
    below: a possessive leading filler would greedily (and, being
    possessive, irrevocably) consume the first character of what should
    instead be read as part of the FIRST survivor itself (measured:
    adding leading filler here broke "a.b-cd", misreading it as filler
    "a" followed by a failed match, instead of the correct dot-glued
    survivor "a.b" followed by "cd").
    """

    return rf"{survivor}(?:{filler}++{survivor})+(?:{filler}++)?+"


def _survivor_tail(survivor: str, filler: str) -> str:
    """1+ MORE non-identical survivors, for use immediately after a
    prefix that has ALREADY consumed one survivor of its own (the
    unknown-field-colon alternative's field-name-shaped prefix, which
    itself satisfies `\\w{2,}` and thus counts as the first survivor).
    Optional leading filler here is safe (unlike in `_survivor_chain`)
    because the prefix has already been matched by the time this runs, so
    there is no earlier survivor for a possessive leading filler to
    accidentally eat into. It cannot eat into the FOLLOWING survivor
    either, but only because every filler branch that consumes word
    characters ends on `_TEXT_TOKEN_END`: without that, the single
    word-character branch would walk a dot-glued token one piece at a
    time ("a", ".", "a" out of "a.a") and, being possessive, never give
    those characters back.

    Only valid where the prefix's colon is itself a genuine analyzer split
    point, which is true for TEXT (StandardAnalyzer splits the merged
    unknown-field blob on the colon) but not for comma_values KEYWORD
    (only a literal comma splits a token, never colon): counting the
    prefix as a KEYWORD survivor wrongly admits a colon-only value with no
    comma at all (measured: "zzz:x"). Use `_survivor_chain` on the value
    alone for a KEYWORD alternative that follows this kind of prefix.

    NOT interchangeable with `_survivor_chain`: using `_survivor_chain`
    for a value that follows an already-consumed prefix requires 2 MORE
    survivors from scratch within just that tail (wrong: measured, this
    mis-rejects "dat:'-1 year to now'", since `_survivor_chain` has no
    leading-filler tolerance to skip the non-survivor "-1 " lead-in
    before finding "year" as its first candidate survivor -- only
    `.search()` finds a match there, not the `.match()`-equivalent
    anchored consumption this whole pattern needs).
    """

    return rf"(?:{filler}++)?+{survivor}(?:{filler}++{survivor})*+(?:{filler}++)?+"


def _tail_not_all_identical_to_prefix(filler: str, prefix_group_name: str) -> str:
    """A zero-width assertion (used inside a negative lookahead placed
    immediately after the unknown-field-colon alternative's already-consumed
    `\\w{2,}` field-name-shaped prefix, captured under `prefix_group_name`)
    matching iff the WHOLE tail ahead is nothing but repeats of that SAME
    case-insensitively identical token (covers "ab:ab", "ab:ab-ab": per
    `_survivor_tail`'s own docstring, the prefix already counts as the
    first survivor, so a tail that only repeats the prefix rather than
    adding a genuinely different token never reaches 2 DISTINCT survivors;
    ANDing and ORing one distinct token repeated compare equal, so these
    do not diverge).

    Sibling of `_survivor_not_all_identical`, for the branches that follow
    `_survivor_tail` rather than `_survivor_chain`: the first "survivor" here
    was already captured by the prefix, not by this fragment, so this checks
    a backreference instead of taking its own `survivor` pattern. `group_name`
    naming rules are the same as `_survivor_not_all_identical`'s (unique per
    use site; see its docstring).

    Each repeat's boundary is `_TEXT_TOKEN_END`, not a plain `(?!\\w)`: a
    dot-glued repeat ("ab.ab") is one distinct token by StandardAnalyzer's
    own tokenizer, never equal to the lone prefix "ab", and must NOT be
    swallowed as "the prefix again with a dot as filler" (measured: a
    plain `(?!\\w)` wrongly treats "ab:ab.ab" as "ab" repeated, when real
    whoosh sees "ab" and "ab.ab" as two distinct survivors that do
    diverge).
    """

    return (
        rf"(?i:(?:{filler}++)?+(?P={prefix_group_name}){_TEXT_TOKEN_END}"
        rf"(?:{filler}++(?P={prefix_group_name}){_TEXT_TOKEN_END})*"
        rf"(?:{filler}++)?+'?(?=[\s)]|$))"
    )


def _survivor_not_all_identical(survivor: str, filler: str, group_name: str) -> str:
    """A zero-width assertion (used inside a negative lookahead) matching
    iff the WHOLE bounded value ahead is nothing but repeats of one
    case-insensitively identical survivor token (covers "ab-ab",
    "AB-ab", "ab-ab-ab": ANDing and ORing one distinct token compare
    equal, so these do not diverge). `group_name` must be unique per use
    site within the same compiled pattern: backreference numbers/names
    are global across a whole compiled regex, not local to a fragment, so
    reusing a name (or relying on numeric \\1) across two fragments
    combined into one pattern silently checks the WRONG group's captured
    text. The trailing `(?!\\w)` after each backreference use stops it
    from matching just a PREFIX of a longer, genuinely-different token
    (measured: without it, "9,90" was wrongly flagged as "all identical"
    because the backreference for "9" matched only the leading "9" of the
    following "90"). The FIRST, capturing occurrence needs the same
    guard, for the mirror-image reason: without it the capture itself can
    backtrack to a strict prefix of the real token, the leftover
    character is then absorbed by the filler's single-word-character
    branch, and a genuinely two-token chain reads as "all identical" and
    is wrongly rejected (measured: "abc-ab", where the capture backtracked
    to "ab" and let "c" through as filler; note the asymmetry, "ab-abc"
    never had the problem, since there the capture's greedy first attempt
    is already the whole token).
    """

    return (
        rf"(?i:(?P<{group_name}>{survivor})(?!\w)"
        rf"(?:{filler}++(?P={group_name})(?!\w))*"
        rf"(?:{filler}++)?+'?(?=[\s)]|$))"
    )


# A zero-width assertion, placed immediately after a range's opening
# bracket, that the range writes at least one bound. It fails for the
# bound-less spellings ("[TO]", "{ to ]", "[]") and succeeds for every
# range with a real bound on either side ("[2020 TO]", "[TO 2021]",
# "[dec to feb]"). Both date-range entries below use it: a range with no
# bounds has neither a bound to tz-convert (entry 12) nor a bound for the
# typed exclusivity to apply to (entry 44), and measurably compares EQUAL,
# so claiming it discarded the comparison for no reason.
_NON_EMPTY_RANGE = r"(?!\s*(?i:to)?\s*[\]}])"

# A zero-width assertion that what follows is NOT a genuine bracketed
# range expression ("[2020-01-01 TO 2020-12-31]"), used by entry 15's
# unknown-field-colon alternative so a real range is left to entry 12's
# date-range mechanism instead of being claimed here. A bracket with no
# "to" inside it ("document_type:[Receipt]", a live corpus line) is not a
# range and stays claimable.
#
# Written with a tempered, possessive run rather than the obvious pair of
# lazy `[^\]}]*?` quantifiers: the lazy spelling re-scans overlapping
# stretches whenever the bracket is never closed, which is measurably
# quadratic ("zzz:[" followed by 16000 repeats of "a to " took upward of
# 15s, varying with hardware, versus well under a tenth of a second here).
# The tempered branch stops exactly at the first "to" and the second run
# scans once to the first closing bracket, so a match attempt is linear
# in the input and cannot be made to backtrack at all.
_RANGE_LOOKAHEAD = r"(?![\[{](?:(?!\b(?i:to)\b)[^\]}])*+\b(?i:to)\b[^\]}]*+[\]}])"

# A single PlusMinus-shaped relative date offset ("-1yr", "-2 yrs",
# "-999 yrs", "+1 week"), built from whoosh's own unit vocabulary
# (whoosh.qparser.dateparse.English.setup's PlusMinus(...) call: each
# alternation below is one of that call's literal unit-word strings, longest
# alternative first as whoosh itself orders them) rather than approximated,
# since both entries below depend on distinguishing this shape precisely
# from an absolute/named-keyword bound.
_REL_UNIT = (
    r"(?:years|year|yrs|yr|ys|y"
    r"|months|month|mons|mon|mos|mo"
    r"|weeks|week|wks|wk|ws|w"
    r"|days|day|dys|dy|ds|d"
    r"|hours|hour|hrs|hr|hs|h"
    r"|minutes|minute|mins|min|ms|m"
    r"|seconds|second|secs|sec|s)"
)
_REL_BOUND = rf"[+-]\s*(?:\d+\s*{_REL_UNIT}\s*)+"


class DivergenceKind(enum.Enum):
    """Which strict-xfail assertion an allowlist entry's matched query
    should satisfy; see this module's docstring for the full taxonomy.
    """

    MISMATCH = "mismatch"
    ORACLE_ERROR = "oracle_error"


# (pattern, DIVERGENCES reference + short reason, strict-xfail taxonomy kind)
ALLOW: list[tuple[re.Pattern[str], str, DivergenceKind]] = [
    # #3: a boost written on one of the natural-date keywords paperless-ngx
    # v2 rewrites away before whoosh ever sees the query. The v2 pipeline
    # (oracle._rewrite_natural_date_keywords, a clone of the real thing)
    # substitutes "added:yesterday" for a literal bracket range, so the "^2"
    # then sits after a whoosh syntax.RangeNode, whose has_boost is False:
    # BoostPlugin.clean_boost (filter priority 0) demotes the BoostNode to a
    # plain WordNode and the boost leaves the query as a stray search term.
    # whoosh-compat never rewrites the keyword, so the boost lands on a
    # word node and binds to the resulting date node.
    #
    # Deliberately NOT scoped to date boosts in general any more: measured,
    # real whoosh PRESERVES a boost on every single-value date spelling
    # ("created:2020^2", "created:jan^2", "modified:now^2" all keep
    # boost=2.0 and compare EQUAL), because DateTimeNode/DateRangeNode set
    # has_boost = True and BoostPlugin.do_boost (priority 510) runs after
    # DateParserPlugin.do_dates (110), overwriting the constructors' dead
    # self.boost = 1.0; and a boost after a *bracketed* date range is
    # demoted to a stray term on BOTH sides, so it is not a divergence
    # either. See DIVERGENCES.md entry 3.
    (
        re.compile(
            r"\b(?:added|created|modified)\s*:\s*[\"']?"
            r"(?i:" + "|".join(NATURAL_DATE_KEYWORDS) + r")"
            r"[\"']?\^\d"
        ),
        (
            "DIVERGENCES.md entry 3: a boost on a natural-date keyword the v2"
            " pipeline rewrites into a bracket range before whoosh parses it,"
            " where whoosh's boost-less RangeNode drops it to a stray term"
        ),
        DivergenceKind.MISMATCH,
    ),
    # #6: unparseable dates/numbers become a structured ErrorLeaf(diagnostic)
    # in whoosh-compat vs whoosh's untyped error_query()/NullQuery-with-.error.
    # This isn't a single-pattern allowlist entry: any parse that reports a
    # diagnostic falls under this divergence, so the test files themselves
    # skip whenever ``result.diagnostics`` is non-empty rather than matching
    # a query pattern here (a fixed regex here couldn't keep up with
    # hypothesis-fuzzed invalid dates/numbers).
    # #2: wildcard/prefix patterns are case-folded via pattern_normalizer in
    # whoosh-compat, but only at *emit* time (TantivyEmitter, see
    # ARCHITECTURE.md's analyzer-contract paragraph): the parsed AST always
    # keeps the pattern's original case (Wildcard.pattern/Prefix.text are
    # never touched at parse time). Real whoosh instead folds case into the
    # AST itself: field.process_text(text, tokenize=False) still runs the
    # field's LowercaseFilter even though tokenize=False skips the
    # tokenizer (filters aren't skipped, see
    # whoosh.analysis.tokenizers.RegexTokenizer.__call__), so any wildcard/
    # prefix pattern containing an uppercase letter parses to a
    # lowercased Wildcard/Prefix node on the oracle side but an
    # unmodified-case one on whoosh-compat's. This was originally scoped to
    # the single literal corpus string "Wär*"; broadened here (grammar-fuzz
    # discovered via `title:A*`) to the general shape, since the root cause
    # (parse-time AST case is never folded by whoosh-compat, only
    # emit-time text is) applies identically to every field/pattern, not
    # just that one string. The field prefix is optional (`(?:\w+:)?`) so
    # this also covers an unfielded, multifield-expanded pattern like the
    # original bare "Wär*" corpus line, which has no ":" at all. The token
    # scan excludes quote characters and requires a clean token boundary,
    # so quoted-phrase content ('title:"Foo Bar?"', where the "?" is
    # analyzed away identically on both sides and the trees compare EQUAL)
    # no longer matches this entry: an overmatch there is never a real
    # entry-2 divergence, only lost fuzz coverage plus a spurious
    # strict-xfail if such a line ever landed in the corpus. Residual
    # approximation: a standalone pattern token in the MIDDLE of a
    # multi-word quoted phrase ('title:"Foo A* Bar"') still matches;
    # tightening that away needs quote-context tracking a regex cannot do.
    # design (DIVERGENCES.md entry 43, entry 2's range sibling): whoosh
    # case-folds bracket-range bounds into the AST (RangePlugin runs the
    # analyzer chain with tokenize=False over each bound); whoosh-compat's
    # TermRange keeps the case the user typed. Ordered BEFORE the entry-2
    # pattern entry so a range spelling like "title:[A* TO B]" matches this
    # reason instead of being absorbed under the Wildcard/Prefix paperwork
    # (the strict-xfail still fires either way; the point is citing the
    # divergence the query actually exhibits).
    (
        # Requires a real separator token (a bracketed non-range token
        # like title:[ABC] parses as an ordinary term on both sides and
        # must not be claimed): a case-insensitive to-token, since whoosh
        # recognizes to/To/tO as the separator too (measured), delimited
        # by whitespace or a bracket on each side, since open-ended
        # spellings put the separator against a bracket ([A TO], [to B]).
        # Excludes date fields (their bracketed ranges parse to DateRange
        # nodes on both sides and belong to entries 12/44), and hunts the
        # uppercase in the text on either side of the separator, so a
        # bound whose only uppercase is a TO-shaped run (bTO, aTO) is
        # claimed too.
        #
        # Both branches are written as a tempered, possessive scan to a
        # SINGLE occurrence (the earliest uppercase letter / earliest
        # qualifying "to") rather than the more obvious pair of greedy
        # `[^\]}]*` runs either side of the required character: existence
        # of a later, valid pairing implies existence at the earliest one
        # too (an uppercase letter before some qualifying "to" is also
        # before every "to" after it; a qualifying "to" before some
        # uppercase letter is also before that same letter no matter which
        # "to" is picked first), so stopping at the first candidate loses
        # no matches. The naive greedy pair is measurably quadratic on an
        # unclosed bracket ("zzz:[" followed by thousands of "a to "
        # repeats): the outer `[^\]}]*` backtracks through every "to" in
        # the input, and for each one the inner run re-scans to the end
        # looking for an uppercase letter that is never there, same
        # mechanism as entry 15's own lookahead (`_RANGE_LOOKAHEAD` above).
        re.compile(
            rf"\b(?!(?:{DATE_FIELDS_PATTERN}):)\w+:[\[{{]"
            r"(?:(?:(?![A-Z])[^\]}])*+[A-Z]"
            r"(?:(?!(?<=\s)(?i:TO)(?=[\s\]}]))[^\]}])*+(?<=\s)(?i:TO)(?=[\s\]}])"
            r"|(?:(?!(?<=[\s\[{])(?i:TO)(?=[\s\]}]))[^\]}])*+"
            r"(?<=[\s\[{])(?i:TO)(?=[\s\]}])[^\]}]*[A-Z])"
            r"[^\]}]*[\]}]"
        ),
        (
            "DIVERGENCES.md entry 43: whoosh case-folds TermRange bounds into"
            " the AST (analyzer chain with tokenize=False per bound);"
            " whoosh-compat keeps the raw case, and a text TermRange is"
            " unsupported at emit anyway (entry 5), so this is AST-level only"
        ),
        DivergenceKind.MISMATCH,
    ),
    (
        # Second alternative: a fielded pattern token directly abutting a
        # quote (title:Foo*'x', merged into one Wildcard by both parsers).
        # Anchored on the colon so quoted-phrase CONTENT (title:"Foo Bar?",
        # where the colon is followed by the quote, not the token) still
        # cannot match.
        re.compile(
            r"\b(?:\w+:)?(?=[^\s()\"']*[A-Z])(?=[^\s()\"']*[*?])[^\s()\"']+(?=[\s()]|$)"
            r"|\b\w+:(?=[^\s()\"']*[A-Z])(?=[^\s()\"']*[*?])[^\s()\"']+(?=[\"'])"
        ),
        (
            "DIVERGENCES.md entry 2: wildcard/prefix pattern case is folded into"
            " the AST by whoosh (LowercaseFilter runs even with tokenize=False)"
            " but only at emit time by whoosh-compat (FieldSpec.pattern_normalizer)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # DIVERGENCES.md entry 17 (design) documents that whoosh-compat's
    # CommaValuesPlugin treats a *quoted* comma-values field value as a
    # literal, unlike real whoosh (whose KEYWORD(commas=True) analyzer always
    # splits on commas at analysis time, quoted or not). There is no
    # allowlist entry for it here, though: verified directly (re-checked
    # while adding this module's strict-xfail taxonomy) that the raw,
    # pre-analysis parse trees genuinely differ (whoosh-compat keeps
    # "foo,bar" as one Term; the oracle already has two), but
    # oracle.analyze_ast forward-analyzes both sides through the same
    # comma-splitting KEYWORD analyzer before comparison (mirroring
    # TantivyEmitter's own emit-time re-analysis, see DIVERGENCES.md entry
    # 16), which collapses that difference before the comparison this test
    # module runs ever sees it: "tag:'foo,bar'" structurally matches at this
    # comparison layer, even though it is still a real, separately
    # unit-tested divergence in the raw parser output and worth keeping
    # documented. Allowlisting it here would make it a stale entry by
    # construction (the strict-xfail assertion would fail immediately,
    # since the trees actually match), so it stays out of ALLOW and is
    # compared normally.
    # design: JSON dotted-path fields (custom_fields.value, notes.user, ...)
    # aren't registered in the v2 oracle schema/registry (v2 whoosh has no
    # JSON subpath concept at all), so on *both* sides the field is
    # "unknown": but the two parsers tag an unknown dotted name
    # differently, so the resulting trees genuinely diverge structurally
    # rather than being simply unmappable (to_ast maps the oracle's tree
    # fine; it's just shaped differently). whoosh-compat's own FieldsPlugin
    # tagger regex is deliberately dot-inclusive (`[\w.]+:` vs whoosh's
    # `\w+:`, see plugins.FieldsPlugin: this is what makes
    # `notes.user:` resolvable as a JSON subpath *when* a JSON FieldSpec is
    # registered), so it greedily tags the *whole* "custom_fields.value:" run
    # as one candidate fieldname token; whoosh's non-dot-aware tagger only
    # ever matches "value:" (the run *after* the last dot), leaving
    # "custom_fields." as plain text merged differently by each side's
    # unknown-field demotion logic. Confirmed via oracle.compat_raw_parse:
    # both sides produce a real (non-None) tree, they just don't structurally
    # match. This is an inherent consequence of whoosh-compat's JSON-subpath
    # tagger feature existing at all, not a bug on either side.
    (
        re.compile(r"\bcustom_fields\.(value|name)\b"),
        (
            "DIVERGENCES.md entry 14 (design): dot-inclusive FieldsPlugin"
            " tagger ([\\w.]+: vs whoosh's \\w+:) tags an unregistered dotted"
            " name differently than whoosh's tagger even though neither side"
            " has the field registered"
        ),
        DivergenceKind.MISMATCH,
    ),
    (
        re.compile(r"\bnotes\.(user|note)\b"),
        (
            "DIVERGENCES.md entry 14 (design): dot-inclusive FieldsPlugin"
            " tagger ([\\w.]+: vs whoosh's \\w+:) tags an unregistered dotted"
            " name differently than whoosh's tagger even though neither side"
            " has the field registered"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 14, extended): "attrs" is a JSON field
    # genuinely registered in ORACLE_REGISTRY (added specifically so
    # strategies.py can generate real JSON-subpath pattern/existence/quoted-
    # value queries, see oracle.py's comment next to the FieldSpec), but real
    # v2 whoosh has no such field at all, so a query addressing it always
    # structurally diverges from the oracle, same mechanism as the
    # notes./custom_fields. entries above. The regex covers both the
    # bare-JSON-name case ("attrs:foo", which actually still matches the
    # oracle: FieldRegistry.make_ref demotes it identically to a genuinely
    # unregistered field, verified directly) implicitly not matching here
    # since it has no dot, and every "attrs.<subpath>:" shape, which does.
    (
        re.compile(r"\battrs\.(user|note|value|name)\b"),
        (
            "DIVERGENCES.md entry 14 (design, extended): dot-inclusive"
            " FieldsPlugin tagger tags 'attrs.<subpath>:' differently than"
            " whoosh's non-dot-aware tagger; 'attrs' is registered as a JSON"
            " field only on whoosh-compat's side (added purely to reach"
            " generator vocabulary), real whoosh has no such field at all"
        ),
        DivergenceKind.MISMATCH,
    ),
    # NOTE: DIVERGENCES.md entry 8 ("attached -foo searches for foo") describes
    # a divergence between whoosh-compat and paperless-ngx's live tantivy-based
    # search (not one between whoosh-compat and real whoosh): it does
    # not apply here. Verified directly: a bare leading "-foo" with nothing
    # before it never triggers either parser's NOT-prefix operator (that
    # requires a preceding term to attach to); both whoosh and whoosh-compat
    # treat it as an ordinary literal word, multifield-expanded like any
    # other unfielded term (down to the same per-field analyzer quirk: "-"
    # survives inside the KEYWORD `tag` field's token but is stripped by
    # TEXT fields' word-boundary tokenizer, on *both* sides identically).
    # An earlier version of this allowlist wrongly carried a "#8" entry here
    # by analogy without verifying it against the actual v2 oracle; removed
    # after confirming "-foo" compares and passes structurally.
    # whoosh-bug (DIVERGENCES.md entry 44): a date range typed with an
    # exclusive bracket. whoosh's DateRangeNode never forwards
    # startexcl/endexcl (always inclusive-both, a plumbing oversight);
    # whoosh-compat honors the typed
    # brackets on exact bounds. Ordered BEFORE the entry-12 date-range
    # entry so the exclusive spelling cites this divergence rather than
    # being absorbed under the tz-bypass paperwork (both are MISMATCH kind;
    # the ordering only affects citation accuracy). Scoped to a bracketed
    # range on a registered date field where either bracket is the
    # exclusive one AND at least one bound is actually written: a
    # bound-less range ("added:[TO}", "added:{TO]") has no bound to be
    # exclusive OF, and measurably compares EQUAL, so claiming it only
    # threw the comparison away (_NON_EMPTY_RANGE below).
    (
        re.compile(
            rf"\b(?:{DATE_FIELDS_PATTERN}):"
            rf"(?:\{{{_NON_EMPTY_RANGE}[^\]}}]*[\]}}]|\[{_NON_EMPTY_RANGE}[^\]}}]*\}})"
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 44): whoosh's DateRangeNode"
            " drops typed {}/exclusivity flags (always inclusive-both);"
            " whoosh-compat honors them on exact bounds"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 55, ORACLE_ERROR): a bracketed range
    # with exactly one bound present, where that bound is the literal
    # keyword "now" or a PlusMinus relative offset ("-7 years", "+1 week"),
    # crashes real whoosh before a query object is ever built:
    # DateParserPlugin.range_to_dt (whoosh/qparser/dateparse.py) calls
    # end.disambiguated(self.basedate) unconditionally whenever only one
    # bound is present; that method exists on the adatetime/timespan objects
    # every OTHER bound spelling produces (an absolute date, a bare
    # month/year, "today", "yesterday"; none of those crash alone, measured
    # directly), but PlusMinus.props_to_date and the "now" keyword both
    # return a plain datetime.datetime, which has no .disambiguated(). Must
    # be checked before entry 12's own broader bracketed-range pattern
    # below, which would otherwise also match this shape and wrongly assert
    # a MISMATCH comparison the oracle never gets far enough to produce.
    # whoosh-compat parses every one of these cleanly (an open-ended
    # DateRange, no diagnostic): not reproduced. This is real, plausible
    # syntax, not a corner case invented for coverage: it is the
    # maintainer-endorsed spelling from paperless-ngx#13482 for an empty
    # date bound.
    (
        re.compile(
            rf"\b(?:created|modified|added):\["
            rf"(?:\s*(?i:to)\s*(?:now|{_REL_BOUND})\s*\]"
            rf"|(?:now|{_REL_BOUND})\s*(?i:to)\s*\])"
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 55): a single-bound range whose"
            " only bound is 'now' or a relative offset crashes real whoosh's"
            " range_to_dt (calls .disambiguated() on a plain datetime.datetime,"
            " which lacks it); whoosh-compat parses it cleanly"
        ),
        DivergenceKind.ORACLE_ERROR,
    ),
    # whoosh-bug (DIVERGENCES.md entry 56, ORACLE_ERROR): a bound-less date
    # range whose opening bracket sits directly against "TO" (no leading
    # space), immediately followed by a recognized "to"-prefixed date
    # keyword ("today", "tomorrow"), crashes real whoosh's RangePlugin
    # tokenizer: its start-bound regex has no word-boundary requirement, so
    # it walks into the keyword's own leading "to" looking for a separator,
    # consuming "TO " as the (bogus) start-bound text and leaving a
    # truncated fragment ("day"/"morrow") as the end. whoosh-compat's own
    # forked RangePlugin.expr now requires a word boundary around the
    # separator (src/whoosh_compat/parser/plugins.py) and parses this
    # cleanly. Scoped to exactly the two recognized date keywords measured:
    # "total"/"into" as the bound word instead produce a whoosh-compat
    # parse *diagnostic* (unrecognized date text), which takes the entry-6
    # diagnostic skip uniformly and needs no claim here.
    (
        re.compile(r"\b(?:created|modified|added):\[(?i:to)\s+(?i:to(?:day|morrow))\s*\]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 56): a bound-less date range"
            " glued directly to 'TO' and followed by a to-prefixed date"
            " keyword ('today'/'tomorrow') crashes real whoosh's RangePlugin"
            " tokenizer (no word-boundary around the separator); whoosh-compat's"
            " own forked copy requires one and parses it cleanly"
        ),
        DivergenceKind.ORACLE_ERROR,
    ),
    # whoosh-bug (DIVERGENCES.md entry 56, MISMATCH): the same RangePlugin
    # tokenizer ambiguity as the ORACLE_ERROR entry directly above, but on a
    # non-date field, where real whoosh does not crash: it silently
    # misparses instead, since its start-bound regex has no word-boundary
    # requirement. "title:[total 5]" (no "TO" anywhere in the string at
    # all) reads whoosh's own embedded "to" inside "total" as the
    # separator, producing a Range with an empty start and end "tal 5";
    # "title:[into TO 5]" similarly stops the start bound at "in" (the "to"
    # inside "into" satisfies the lookahead) rather than the whole word.
    # whoosh-compat's word-boundary-hardened regex no longer does either:
    # "title:[total 5]" is not tagged as a range at all (falls through to
    # ordinary term parsing) and "title:[into TO 5]" resolves the full
    # "into"/"5" bounds. Scoped to exactly the two words measured
    # ("total", "into"); not generalized to every word beginning with
    # those letters.
    (
        re.compile(r"\btitle:\[(?:total\s|into\s+(?i:to)\s)"),
        (
            "whoosh-bug (DIVERGENCES.md entry 56): a range value that itself"
            " begins with a to-prefixed word ('total', 'into') is silently"
            " misparsed by real whoosh's word-boundary-free RangePlugin regex;"
            " whoosh-compat's own forked copy requires a word boundary and"
            " parses it correctly (or declines to treat it as a range at all)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 12): real whoosh's range-bound date parsing
    # (DateParserPlugin.range_to_dt) calls
    # `self.dateparser.get_parser().date_from(...)`, the *grammar object's*
    # date_from, not `LocalDateParser.date_from`, so LocalDateParser's
    # timezone-reversal override (reverse_timezone_offset) never actually
    # runs for bracketed ranges in the real v2 system, only for
    # single/keyword values (DateParserPlugin.text_to_dt, which *does* call
    # `self.dateparser.date_from`). Naive range bounds are therefore taken as
    # literal UTC with no local-tz shift applied at all. This is a confirmed
    # defect in paperless-ngx v2's LocalDateParser wiring (the override
    # silently no-ops for exactly the code path (ranges) it was written
    # to cover), not an intended whoosh design choice, so whoosh-compat does
    # NOT reproduce it: DateParserPlugin.range_to_node applies the same tz
    # conversion uniformly to both single values and range bounds. Covers
    # every bracketed range on a DATE/DATETIME field in the corpus that has
    # at least one bound to convert. The
    # opening bracket is "[" or "{" (inclusive/exclusive): the root cause
    # (range_to_dt's missing ToEnd/override wiring) doesn't care which one
    # was typed, only that it's a range at all; broadened from "[" only
    # after the grammar-aware fuzzer generated an exclusive-bracket range
    # ("created:{TO 1000]") that hit the identical bypass. A fully open
    # range ("added:[TO]") is deliberately NOT claimed: with no bound
    # string there is nothing for the missing override to convert, and the
    # two sides measurably compare EQUAL. The result-level twin
    # (tests/emitter/result_allowlist.py) has always required a digit
    # inside the brackets for the same reason; a digit is too strict here
    # (a month-name bound like "added:[dec to feb]" is tz-converted too,
    # and does diverge), so this uses _NON_EMPTY_RANGE instead.
    #
    # A second carve-out: a range whose BOTH bounds are pure
    # PlusMinus relative offsets ("[-1yr to -0yr]", "[-2 yrs to -1 yrs]",
    # any unit/granularity, measured directly) compares EQUAL, not
    # divergent. The tz-reversal override this entry's bug is about only
    # changes the result for a bound whoosh's LocalDateParser would
    # otherwise shift from local wall-clock time to UTC; a relative offset
    # is computed as an arithmetic delta off basedate on both sides
    # (real whoosh's PlusMinus.props_to_date does `dt + delta` directly, no
    # local/naive distinction to bypass), so the missing override changes
    # nothing when every bound is one. The instant either bound is instead a
    # named keyword ("now", "today", a month name) or an absolute date, the
    # divergence reappears (measured: "[-1 week to now]", "[now to now]",
    # "[today to now]" all diverge); only the both-relative-offset shape is
    # excluded. Exclusive-bracket forms are unaffected by this carve-out:
    # they never reach this pattern at all, entry 44's pattern above already
    # claims them first.
    (
        re.compile(
            rf"\b(?:created|modified|added):[\[{{]"
            rf"(?!{_REL_BOUND}(?i:to)\s*{_REL_BOUND}\s*[\]}}])"
            rf"{_NON_EMPTY_RANGE}"
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 12): LocalDateParser's"
            " tz-reversal override doesn't reach range bounds (range_to_dt"
            " uses the bare grammar's date_from, not the override);"
            " whoosh-compat applies tz conversion uniformly instead of"
            " reproducing the bug"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated
    # datetime value ('2026-08-04T10:30:00', with or without the Z
    # designator). whoosh's grammar has no T separator and its fallback
    # chain bottoms out in _NullQuery (matches nothing); whoosh-compat
    # parses the correct DateRange. Ordered BEFORE the entry-18 bare-ISO
    # entry, whose regex also matches these strings but whose
    # numerically-correct field-self-parse mechanism does not describe
    # the _NullQuery outcome. The quote is REQUIRED: the bare unquoted
    # spelling colon-tokenizes differently in whoosh (a partial
    # NumericRange plus leftover terms, not _NullQuery), so this reason
    # string would be false for it.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'\d{{4}}-\d\d-\d\d[Tt]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a correct DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 54, superseding DIVERGENCES.md
    # entry 49): the BARE unquoted sibling of the entry-48 spelling.
    # Colon tokenization splits the value before any date grammar runs,
    # leaving the date field a fragment cut off mid-token ("2026-08-").
    # Real whoosh swallows the dangling separator and reads the fragment
    # as a whole, shorter date (the August-2026 month window), ANDing the
    # rest of the timestamp on as free text with no diagnostic;
    # whoosh-compat rejects the half-consumed value as BAD_DATE. Every
    # COLON-BEARING query this pattern matches today therefore takes the
    # entry-6 diagnostic skip before this entry's DivergenceKind is ever
    # consulted. The pattern deliberately also admits the colon-less
    # spelling ("added:2026-08-04T10", a T-fused value with no clock
    # time), which nothing splits and which therefore diagnoses nothing:
    # it parses to an hour-precision DateRange against whoosh's
    # _NullQuery, entry 48's compat-favorable shape without the quotes.
    # The reason string covers both faces because either can reach it (no
    # corpus line uses the colon-less spelling today, so which face fires
    # is currently theoretical, but a wrong reason is not). Ordered BEFORE the
    # entry-18 bare-ISO entry, whose fully-parses-numerically-correct
    # prose describes neither the truncation nor the rejection. No quote
    # after the colon: the quoted spellings belong to entries 48 (single)
    # and 45 (double). The day group is optional: the no-day spelling
    # ("2026-08T10:30") leaves the fragment "2026-" by the same
    # mechanism, which whoosh reads as the whole YEAR.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):\d{{4}}-\d\d(?:-\d\d)?[Tt]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 54, superseding DIVERGENCES.md"
            " entry 49): a bare unquoted T-separated datetime value. With a"
            " clock time, its colons cut the value mid-token: whoosh silently"
            " reads the fragment as a shorter whole date and ANDs the"
            " remainder on as text, whoosh-compat diagnoses BAD_DATE. Without"
            " one there is nothing to cut: whoosh reads the whole value as"
            " _NullQuery, whoosh-compat as the DateRange meant (entry 48's"
            " shape, unquoted)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 50): a NO-separator T-fused value
    # ("2026T10", bare or single-quoted, optionally with a colon-split
    # day token as in "2026T10:30"). whoosh's grammar cannot read it at
    # all and bottoms out in _NullQuery; whoosh-compat's T-separator
    # grammar reads year-T-month (joining a colon-split trailing token
    # into a day). T directly after the year keeps this disjoint from
    # entries 48/49 (which require dashes); the double-quoted spelling
    # belongs to entry 45's crash cell. Ordered before the entry-15
    # unknown-field-demotion pattern, which would otherwise mis-claim
    # the inner-colon spelling by reading "2026T10" as an unknown FIELD
    # named 2026T10 with value 30.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}[Tt]\d"),
        (
            "whoosh-bug (DIVERGENCES.md entry 50): a no-separator T-fused"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a year-T-month DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 18): a bare (non-bracketed) separated-ISO
    # date value on a date field ("created:2020-01-01", "created:2020-01")
    # is numerically correct on both sides but structurally different: real
    # whoosh's DateParserPlugin.text_to_dt fails to fully parse it (the same
    # grammar-ordering limitation entry 12 describes for range bounds), but
    # ErrorNode.query() falls back to running the original node's own
    # query() method anyway, which for an ordinary fielded term reaches
    # DATETIME.parse_query's field-level self-parse (fields.py) instead:
    # numerically correct, but a query.NumericRange, not the
    # DateTimeNode/DateRangeNode shape a successful text_to_dt would have
    # produced. whoosh-compat has no equivalent field-level fallback
    # architecture (DateParserPlugin's grammar is the only date-parsing
    # path, see parser/dateparse.py's module docstring); after fixing the
    # bundle Choice's alternative order, its single grammar path parses
    # these directly into a DateRange. Scoped to a bare value only (no "[" right
    # after the colon, optionally single-quoted) so it doesn't also swallow
    # the bracketed-range corpus lines above (those are covered by the
    # broader "\[" entry regardless).
    #
    # The separator class is "-", "." and "/" only. A SPACE is deliberately
    # NOT a separator here, even though the date grammar accepts one: a
    # four-digit run followed by a space is followed by *anything*, not by
    # an ISO date part, and the shapes that pulls in are either simply
    # equal ("added:'2020 5pm'", "created:0125 0", both measured EQUAL) or
    # divergent for an entirely different reason ("added:'2020 12:30'",
    # which is entry 21's month:day-vs-time-of-day reading and now has its
    # own entry directly below), for which this entry's "numerically
    # correct on both sides" reason is provably false.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}[-./]\d"),
        (
            "DIVERGENCES.md entry 18: bare separated-ISO date value parses"
            " correctly on both sides but via a different mechanism/AST"
            " shape (whoosh's ErrorNode-falls-back-to-field.parse_query vs"
            " whoosh-compat's single DateParserPlugin grammar path)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # DIVERGENCES.md entry 21: a year, whitespace, then a colon-separated
    # pair that can be read as a calendar month:day. whoosh-compat reads
    # the pair as month and day of that year ("added:'2020 12:30'" ->
    # 30 Dec 2020); real whoosh reads it as a time of day on EVERY day of
    # the year (2020-01-01 11:30 .. 2020-12-31 11:30:59). Entry 21 had no
    # entry of its own until this sweep: entry 18's space-separator
    # alternative claimed the shape first and recorded its own (here
    # false) "numerically correct on both sides" reason for it.
    #
    # Scoped by what the divergence actually needs, measured cell by cell
    # over every hour x minute pair: a two-digit left half in 01..12 (a
    # readable month) and a right half that is a valid day of THAT month.
    # A left half of 00 or 13..23, or a right half of 00 or 32..59,
    # compares EQUAL (no calendar reading is available, so both sides fall
    # back to the time of day). The month-length arms below are exact for
    # 30- and 31-day months; February admits 29 unconditionally rather
    # than deriving leap years from the year digits, so "…'2021 02:29'"
    # (EQUAL) is the one residual over-claim, a single spelling per
    # non-leap year, kept because a leap-year-aware regex here would be
    # far less legible than the divergence it guards.
    (
        re.compile(
            rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}\s+"
            r"(?:(?:0[13578]|1[02]):(?:0[1-9]|[12]\d|3[01])"
            r"|(?:0[469]|11):(?:0[1-9]|[12]\d|30)"
            r"|02:(?:0[1-9]|1\d|2\d))"
            r"(?!\d)"
        ),
        (
            "DIVERGENCES.md entry 21: a year followed by a colon-separated"
            " month:day pair reads as a calendar date in whoosh-compat but as"
            " a time of day on every day of that year in whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 45): the double-quoted sibling of
    # the entry-18 spellings. The ErrorNode fallback wraps a PhraseNode
    # here, so whoosh parses query.Phrase('created', ['2020-01-01']),
    # which raises QueryError at search time (a DATETIME field has no
    # positions); whoosh-compat parses the quoted value into the correct
    # day/month-period DateRange. Both sides parse cleanly, so the AST
    # comparison is an ordinary MISMATCH (Phrase vs DateRange).
    (
        re.compile(rf'\b(?:{DATE_FIELDS_PATTERN}):"\d{{4}}(?:[-. /]|[Tt])\d[^"]*"'),
        (
            "whoosh-bug (DIVERGENCES.md entry 45): double-quoted"
            " separated-ISO date parses to a search-time-crashing Phrase in"
            " whoosh but a correct day/month-period DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # NOTE: the unquoted multi-word date keywords (`created:previous month`)
    # used to be allowlisted here, because whoosh-compat's grammar only
    # recognized them quoted while the oracle harness replicated paperless's
    # app-level rewrite. whoosh-compat's date grammar now joins those six
    # phrases itself (DateParserPlugin.do_date_phrases), so both sides agree
    # and there is nothing left to allowlist: the remaining difference is
    # against *stock* whoosh, which cannot parse them in any spelling and so
    # is not reachable through this harness (DIVERGENCES.md entry 19).
    # DIVERGENCES.md entry 23's "match-all face" (an unfielded "*:*" ANDed
    # with a zero-token term) used to have its own allowlist entry here.
    # Fixed by a change to whoosh_compat.ast's analyze()/normalize()
    # ordering (see entry 23's own text for the mechanism and the fix), so
    # every shape that entry claimed now compares EQUAL and the entry is
    # removed rather than left stale. See
    # tests/differential/corpus_docs.txt's "CONFIRMED PARITY" line.
    # design (DIVERGENCES.md entry 20, the "*:*"-carve-out's blind spot):
    # a standalone token of the shape <star-run> ":" "**", i.e. "*:**" and
    # "**:**". Real whoosh's FieldsPlugin consumes the "*:" + the first "*"
    # as the unfielded match-all "*:*" and leaves a SECOND bare "*" behind,
    # which multifield-expands to a literal Wildcard("*") per default field
    # while whoosh-compat builds Every(field) per default field: exactly
    # this entry's Every-vs-Wildcard divergence, just reached through a
    # token the entry-20 regex below cannot see. That regex needs the "*"
    # to start at the string start or after whitespace/"("/":"; here the
    # surviving star is preceded by another star, so nothing matched and
    # the shape was a genuine, unclaimed divergence a single unlucky fuzz
    # draw could have failed CI with.
    #
    # Scoped by measurement over every string of "*"/":" up to length 5, in
    # five contexts each (bare, parenthesized, with a leading sibling, with
    # a trailing sibling, boosted). EXACTLY two trailing stars diverge:
    # "*:***" (three) measurably compares EQUAL, so the trailing negative
    # lookahead is required, not decorative, and "*:*" itself (one star,
    # Every(field=None) on both sides, EQUAL) cannot match this pattern at
    # all. Deliberately anchored to a whitespace/paren/start boundary on the
    # left: ":*:**" also diverges but through a leading-colon token this
    # claim does not describe, and is left unclaimed and reported rather
    # than swept in here (as are "**:", "**::", "**:*:", "**:::").
    (
        re.compile(r"(?:^|(?<=[\s(]))\*+:\*\*(?!\*)(?:\^[\d.]+)?(?=$|[\s)])"),
        (
            "DIVERGENCES.md entry 20: a '*:**'/'**:**' token leaves a second"
            " bare '*' after whoosh's own '*:*' match-all, which expands to a"
            " literal Wildcard('*') per default field in whoosh vs"
            " Every(field) in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 20, group 1): a star-run
    # followed by a colon-run and NOTHING else, e.g. "**:", "**::", "**:::".
    # Measured directly against the oracle: both sides parse to
    # And[Or(<per-default-field>), Term(tag, <same residual text>)], and the
    # ONLY difference is the Or's leaf type, exactly entry 20's Every-vs-
    # Wildcard mechanism, just reached through a token with zero trailing
    # stars after the colon (the pattern directly above requires exactly two
    # trailing stars and so cannot see this shape). The trailing negative
    # lookahead excludes "**:*" (a single trailing star): that shape is a
    # bare-"*"-after-field-colon token claimed by the entry-20 regex further
    # below instead, a different residual (Term(tag, ':') vs no residual
    # term at all), not remeasured here.
    (
        re.compile(r"(?:^|(?<=[\s(]))\*\*:+(?!\*)(?:\^[\d.]+)?(?=$|[\s)])"),
        (
            "DIVERGENCES.md entry 20: a '**:'/'**::'/'**:::' token (star-run"
            " then colon-run, no trailing star) leaves the same"
            " Every(field)-vs-Wildcard('*') leaf-type divergence as the"
            " '*:**'/'**:**' shape above, reached through a token with no"
            " trailing star instead"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 20, group 2): a leading bare
    # ":" followed by "*:**"/"*:*:" ("":*:**"" / ""**:*:""). Measured
    # directly: whoosh's own grammar binds a leading bare ":" to a single
    # specific schema field (a literal Term, e.g. "tag::") rather than
    # multifield-expanding it, while whoosh-compat's grammar treats the same
    # bare ":" as an unfielded term and multifield-expands it into an Or of
    # one Term per default field. That is a different node *shape* (one
    # Term vs. a 7-way Or), not merely a leaf-type swap, so it is kept as
    # its own narrow, exact-token claim rather than folded into either
    # star-run entry above: whether whoosh's single-field binding generalizes
    # to other leading-bare-":" shapes was not measured, so the pattern is
    # deliberately literal rather than generalized.
    (
        re.compile(r"(?:^|(?<=[\s(]))(?::\*:\*\*|\*\*:\*:)(?:\^[\d.]+)?(?=$|[\s)])"),
        (
            "DIVERGENCES.md entry 20: a ':*:**'/'**:*:' token's leading bare"
            " ':' binds to a single schema field in whoosh (a literal Term)"
            " but multifield-expands to an Or of Term per default field in"
            " whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 57): an unquoted value with two
    # consecutive colon-fieldname-looking segments where neither names a
    # real field ("aa:bb:cc": the tagger produces FieldnameNode("aa"),
    # FieldnameNode("bb"), WordNode("cc"), and both fieldnames are
    # rejected). Real whoosh's do_fieldnames keeps only the most recently
    # rejected candidate, so "aa:" is discarded with no trace and the
    # oracle reads this as the two words "bb"/"cc" per default field;
    # whoosh-compat's fixed do_fieldnames accumulates every rejected
    # candidate's text in order, so the value stays the literal "aa:bb:cc"
    # the user typed. Scoped to exactly this measured shape (two
    # non-word-boundary-prefixed lowercase segments, no digits, no
    # existing field name): not generalized past what a query with
    # zero registered fields matching either segment produces.
    (
        re.compile(r"(?:^|(?<=[\s(]))aa:bb:cc(?:\^[\d.]+)?(?=$|[\s)])"),
        (
            "whoosh-bug (DIVERGENCES.md entry 57): a second consecutive"
            " rejected field-name candidate ('aa:bb:cc') has its earlier"
            " candidate's text ('aa:') silently discarded by real whoosh's"
            " do_fieldnames; whoosh-compat's fixed copy accumulates it"
            " instead, keeping the literal text the user typed"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design: a bare "*" wildcard on a field (`title:*`) whoosh-compat
    # simplifies to Every(field) (see QueryParser.wildcard_query's
    # docstring: "the text is exactly '*' -> Every"); real whoosh's
    # WildcardPlugin builds a literal Wildcard('title', '*') query object
    # instead (functionally equivalent: both match every document with a
    # value in the field: but a different AST shape). whoosh-compat's
    # Every(field) shape is deliberate: see DIVERGENCES's emitter table
    # (Every(field) -> fast-field exists_query or a regex(".*") fallback for
    # non-fast TEXT, both cheaper than a literal wildcard scan).
    # A standalone "*" (bounded by whitespace/parens/start-end, optionally
    # preceded by "field:") triggers the same simplification whether or not
    # it's fielded: an entirely bare "*" multifield-expands to one Every()
    # per default field, same shape as the fielded case, just without the
    # ":". Broadened from a colon-only match (which missed the bare form,
    # found by the grammar-aware fuzzer) without also catching "*" used as
    # a wildcard character inside a larger pattern like "produ*name" or a
    # trailing-star fold like "abc*" (neither of those has a "*" bounded by
    # whitespace/parens/start-end on both sides).
    #
    # One spelling is carved back out by the two negative lookbehinds: the
    # standalone token "*:*" (a "*"-named field, optionally boosted).
    # Both sides read it as an unfielded match-all Every(field=None) and it
    # compares EQUAL, so claiming it only discarded the comparison. It is
    # not simply unclaimed, though: ANDed with a term whose analyzer drops
    # every token it becomes the entry-23 match-all divergence, claimed by
    # the entry directly above this one. The carve-out is deliberately
    # exactly "*:*" and not "any star-named field": "**:*" is a genuine
    # entry-20 divergence (measured) and stays claimed.
    (
        re.compile(
            r"(?:^|(?<=[\s(:]))(?<!^\*:)(?<![\s(]\*:)"
            r"\*(?:\^[\d.]+)?(?=$|[\s)])"
        ),
        (
            "DIVERGENCES.md entry 20: bare field:* (or unfielded *) simplifies to"
            " Every(field) in whoosh-compat vs a literal Wildcard('*') in whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 13): real whoosh's WildcardPlugin.do_wildcards
    # (and query.terms.Wildcard.normalize(), same root cause) only tests a
    # trailing-star pattern for "*"/"?" before folding it to a Prefix:
    # despite SPECIAL_CHARS = "*?[" including "[": so a pattern like
    # "202[0-3]*" folds to Prefix('title', '202[0-3]') and silently loses the
    # character class instead of staying a Wildcard. whoosh-compat fixed the
    # fold check (parser/plugins.py's folds_to_prefix, shared by both sites
    # that perform this fold: do_wildcards and QueryParser.wildcard_query) to
    # also test for "[", so it keeps the full Wildcard pattern. Not reproduced:
    # whoosh-compat's own trailing-star-with-bracket corpus line
    # (title:202[0-3]*) is intentionally allowlisted here rather than
    # matched against the (buggy) oracle tree. Broadened from the single
    # literal corpus string to the general shape (any field/value with a
    # bracket class immediately followed by a trailing "*"): the root cause
    # is a check that's missing for every field/pattern, not just this one
    # corpus line, as the grammar-aware fuzzer confirmed (title:0[0-0]*).
    (
        # The field prefix is optional ((?:\w+:)?): broadened after the
        # expanded generator's dedicated degenerate/reversed-class atom
        # (strategies._degenerate_wildcard_atom) produced an *unfielded*,
        # multifield-expanded instance of this same pattern
        # ("x[z-a]*", no "field:" text anywhere in the query at all): the
        # root cause (real whoosh's SPECIAL_CHARS/fold-check omitting "[")
        # applies identically whether or not the pattern is fielded, and
        # verified directly against the oracle both ways.
        re.compile(r"\b(?:\w+:)?[^\s()]*\[[^\]]*\]\*(?:\^[\d.]+)?(?:\s|$|\))"),
        "whoosh-bug (DIVERGENCES.md entry 13): Wildcard.normalize() bracket fold drops the character class on a trailing-star pattern",
        DivergenceKind.MISMATCH,
    ),
    # design (extends DIVERGENCES.md entry 23, found by the grammar-aware
    # fuzzer, see test_hypothesis.py): NOT of a term/phrase whose value is
    # empty after the field's own token-dropping analysis (a stopword, or a
    # token shorter than StandardAnalyzer's minsize=2) reaches the exact
    # same divergence entry 23 already documents at emit time, but at the
    # differential AST-comparison layer instead: oracle.analyze_ast's own
    # "a dropped Term/Phrase vanishes from its parent group" rule (see that
    # function's docstring) turns the NOT's now-empty child into an empty
    # And(), which whoosh_compat.ast.normalize()'s pre-existing
    # Not(Nothing) -> Every rule then upgrades to Every(): whoosh-compat's
    # comparison tree becomes "matches everything", while real whoosh's
    # Not(NullQuery).normalize() stays NullQuery ("matches nothing"), same
    # as entry 23. This entry is scoped to a NOT wrapping (possibly
    # through empty-group and nested-NOT noise) a known-zero-token-word
    # value on any field (the ZERO_TOKEN_WORD fragment, derived from
    # whoosh's own STOP_WORDS set at the top of this module); the broader
    # co-occurrence entry directly below catches the same family in
    # arbitrary scaffolding this prefix cannot reach.
    # The value may be a CHAIN of zero-token pieces joined by the
    # characters StandardAnalyzer splits on (dash/comma/slash): every
    # piece analyzes away, so the-of, the,x, and a-b are zero-token too
    # (measured diverging), while a chain containing one surviving piece
    # (the-invoice) is rescued by it and compares equal, which the
    # per-piece boundary rejects via backtracking exhaustion.
    # The field is required to be a registered TEXT field
    # (TEXT_FIELDS_PATTERN, derived from the registry so it cannot drift as
    # fields are added or renamed), because TEXT is exactly the kind whose
    # analyzer can drop every token: measured across the whole registry,
    # "NOT <field>:the" and "NOT <field>:a" diverge for every TEXT field and
    # for no other kind. An earlier version wrote any \w+ with a
    # four-name KEYWORD lookahead carved out, which also claimed the
    # U64/BOOLEAN_EXISTS/JSON/unknown-field spellings ("NOT (id:0)",
    # "NOT attrs:9", "NOT zzz:the"), all of which compare EQUAL - the
    # zero-token proxy simply does not apply to a field whose value never
    # reaches a stopword/minsize analyzer. The value alternation
    # also includes a bare "\w" (matches exactly one word character): any
    # single-character value is zero-token too (StandardAnalyzer's
    # minsize=2 drops it), and the grammar-aware fuzzer's generic term
    # atom (not just its dedicated zero-token atom) produces single-
    # character words often enough by chance ("title:2", not just the
    # curated word list) that enumerating specific single characters here
    # would be a losing game; the "\w" alternative, ordered last so the
    # named stopwords still match themselves rather than just their first
    # letter, covers all of them at once. The registered KEYWORD fields
    # fall outside TEXT_FIELDS_PATTERN for a reason worth naming: whoosh's
    # KEYWORD analyzer only splits on commas, with no stopword/minsize
    # filtering, so a single-character KEYWORD value is *not* zero-token
    # and a NOT of one is a real comparison, not this divergence.
    (
        re.compile(
            # The prefix tolerates empty-group and nested-NOT noise
            # between the NOT and the fielded value ("NOT (() title:the)",
            # "0 NOT (NOT (() title:the))", found by a deep fuzz soak):
            # each unit is an open paren, a complete empty group, or a
            # further NOT keyword, so a real intervening term still blocks
            # the match.
            r"\bNOT\s*(?:\(\)\s*|\(\s*|NOT\s+)*"
            rf"(?:{TEXT_FIELDS_PATTERN}):"
            rf"{ZERO_TOKEN_WORD}(?:[-,/]{ZERO_TOKEN_WORD})*[-,/]?(?![\w.,/-])"
        ),
        (
            "DIVERGENCES.md entry 23: NOT of a zero-token term/phrase reaches the"
            " same emit-time divergence at the AST-comparison layer"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entries 23 and 40 jointly): the composed
    # family of the two entries above: a NOT anywhere in the query, an
    # empty group anywhere, and a zero-token word anywhere, in arbitrary
    # scaffolding ("((0) OR (NOT ((()) AND (title:the))))", found by a
    # deep fuzz soak after three narrower spellings of the same family
    # each leaked past the prefix-based entry-23 regex). The three
    # co-occurrence lookaheads claim the whole family at once instead of
    # chasing spellings. The zero-token word must not be an (uppercase)
    # operator keyword, so the corpus line "NOT ()" (single-level, both
    # sides agree, entry 40's own excluded shape) stays unclaimed; per
    # the strict-xfail convention, any corpus line matching this entry
    # must be chosen to genuinely diverge.
    # The zero-token word must be written on a registered TEXT field, the
    # same scoping the entry-23 entry above carries and for the same
    # measured reason: an unfielded or non-TEXT zero-token word does not
    # produce the divergence ("NOT ((()) 0)", "NOT (() 0)" both compare
    # EQUAL), so claiming it only discarded the comparison.
    (
        # Anchored at the string start: every clause here is a `.*`
        # existence lookahead ("does NOT/an empty group/a zero-token
        # fielded value occur somewhere ahead"), whose truth at any given
        # start position implies its truth at position 0 too (a hit later
        # in the string is still a hit when searched for from the very
        # beginning). `allowed_reason`/`allowed_entry`/`allowed` only ever
        # ask whether this pattern matched at all, never where, so
        # anchoring changes nothing observable. Left unanchored, a query
        # with no "NOT" anywhere (the common case on an adversarial,
        # never-closes bracket input) makes `.search()` retry these `.*`
        # scans from every position in the string in turn, each one
        # itself linear: quadratic overall. Anchoring makes the match
        # attempt run exactly once.
        #
        # The equivalence depends on the query string having no embedded
        # newline: `.` (no DOTALL) cannot cross one, so a required clause
        # sitting after a `\n` with irrelevant text before it would be
        # visible searching from a later start position but invisible
        # from position 0, breaking "a hit anywhere is a hit at the
        # start". Every query source this module ever sees is newline-free
        # by construction (corpus files are read via `.splitlines()`, and
        # every hypothesis text strategy in `strategies.py` draws only
        # letter/digit characters), so this holds today; it would need
        # re-checking if either source ever admitted a literal `\n`.
        re.compile(
            r"\A(?=.*\bNOT\b)(?=.*\(\))"
            rf"(?=.*\b(?:{TEXT_FIELDS_PATTERN}):['\"]?"
            rf"{ZERO_TOKEN_WORD}(?![\w.,/-]))"
        ),
        (
            "DIVERGENCES.md entry 23 (with entry 40's empty-group rule): a"
            " NOT, an empty group, and a zero-token word co-occurring in"
            " arbitrary scaffolding reach the composed"
            " collapsed-empty/zero-token divergence family"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 24, found by the grammar-aware fuzzer): a
    # quoted phrase whose entire content analyzes to zero tokens (every word
    # is a stopword or shorter than minsize=2) parses on the oracle side to
    # a real (non-None) whoosh.query.Phrase object with an empty words list
    # (whoosh tokenizes phrase content at *parse* time, see
    # PhrasePlugin.PhraseNode.query in whoosh/qparser/plugins.py), which
    # oracle.to_ast maps faithfully to ast.Phrase(text=""); whoosh-compat
    # defers phrase analysis to emit time (ARCHITECTURE.md's analyzer
    # contract), so oracle.analyze_ast's own _analyzed_phrase helper
    # correctly drops the whole phrase (mirrors what TantivyEmitter would
    # do), collapsing the enclosing group to Nothing() instead. Scoped to a
    # double-quoted phrase whose entire content is one or more known
    # zero-token words (the shared ZERO_TOKEN_WORD fragment, same
    # derivation as the entry-23 allowlist entry above) on a registered
    # TEXT field, the same registry-derived scoping entry 23's entry
    # carries above. An earlier version wrote a generic \w+ field name with
    # no kind restriction at all, contradicting KEYWORD_FIELDS_PATTERN's own
    # rationale in this module and the result-level twin, which does
    # exclude them: measured, tag_id:"in by x", viewer_id:"to a x 9" and
    # type_id:"0" all compare EQUAL, and only the TEXT fields diverge.
    # Each word is either a named stopword or a bare
    # single character (any single char is zero-token too, StandardAnalyzer's
    # minsize=2 drops it): the trailing lookahead `(?=[\s"])` on every
    # alternative requires the match to actually end there, so a real
    # (non-zero-token) word that merely *starts with* a stopword or a
    # digit, e.g. "thermal" or "20th", is not mistaken for one, the same
    # false-positive risk fixed for entry 23 above.
    #
    # An unfielded spelling reaches the identical mechanism: the phrase
    # multifield-expands to one Phrase per default field, and every
    # TEXT-field one of those analyzes to zero tokens the same way the
    # fielded case does, so whoosh's raw (unnormalized) tree keeps them as
    # literal empty-words Or siblings while whoosh-compat drops each to
    # Nothing() and the Or filters them out. This was previously unclaimed,
    # not because it is a different mechanism (entry 23's match-all
    # ordering bug is not involved here at all: verified directly, see
    # DIVERGENCES.md entry 24's own note), but because this regex was
    # written from the fielded case and never broadened to the unfielded
    # one. Anchored to string-start/whitespace/open-paren on the
    # left (matching this module's other unfielded-token conventions, e.g.
    # the entry-20 patterns above) since there is no field-name colon to
    # anchor on instead.
    (
        re.compile(
            rf"\b(?:{TEXT_FIELDS_PATTERN}):"
            rf'"{ZERO_TOKEN_WORD}(?=[\s"])'
            rf'(?:\s+{ZERO_TOKEN_WORD}(?=[\s"]))*"'
            rf'|(?:^|(?<=[\s(]))"{ZERO_TOKEN_WORD}(?=[\s"])'
            rf'(?:\s+{ZERO_TOKEN_WORD}(?=[\s"]))*"'
        ),
        (
            "DIVERGENCES.md entry 24: an all-zero-token quoted phrase parses to"
            " a real empty-words Phrase object in whoosh (tokenized at parse"
            " time) but is dropped entirely by whoosh-compat's emit-time"
            " analysis"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 25, found by the grammar-aware fuzzer): a
    # bare (non-bracketed) "now"-relative date offset ("created:now-7d")
    # parses to a real DateRange in whoosh-compat (README's syntax table
    # documents this directly: "created:now-7d" is listed as a bare
    # example, not just a range bound), but real whoosh has no
    # `now±<n><unit>` grammar at all (entry 53): a bare value in this shape
    # fails to parse as a date on the oracle side and falls back to
    # NullQuery. Confirmed directly: oracle_parse("created:now-7d", ...) ->
    # NullQuery, while oracle_parse("created:[now-7d TO now]", ...) also
    # fails to honor the offset (entry 25's own correction). A
    # whoosh-compat feature with no whoosh equivalent, not a bug either
    # side. Scoped to a value starting with "now" immediately followed by a
    # sign. NOT scoped to a bare "-" immediately followed by a digit
    # (e.g. "created:-3mos", "created:-2yrs"): those spellings are *not* a
    # whoosh-compat-only extension -- real whoosh parses them to the exact
    # same DateRange whoosh-compat does (verified directly against the
    # pinned oracle: `to_ast`/`normalize`-equal trees for -2yrs, -10mins,
    # -30secs, -5hrs, -7d, -1y2mo3w, -999yrs, '-3mos', -0d), so widening
    # this entry to cover them would suppress a comparison that actually
    # passes. A relative offset written with a space (e.g.
    # "created:-1 week", unquoted) fails to parse as a single token on both
    # sides and is skipped separately via the DIVERGENCES.md entry 6
    # diagnostics check, not by this entry.
    (
        re.compile(r"\b(?:created|modified|added):'?now[+-]"),
        (
            "DIVERGENCES.md entry 25: a bare 'now'-relative date offset parses"
            " as a DateRange in whoosh-compat (a documented feature/extension)"
            " but whoosh has no now±<n><unit> grammar at all"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 27, found by the property-based fuzzer
    # during the empty-group-drop work): ANDNOT/ANDMAYBE/REQUIRE whose positive/
    # required/scored side analyzes to zero tokens, nested inside further
    # grouping alongside a sibling clause, poisons the whole enclosing And
    # on whoosh-compat's side but real whoosh drops it. Both sides agree
    # the degenerate AndNot/AndMaybe/Require itself resolves to "match
    # nothing"; the divergence is only in whether that Nothing propagates
    # through an *enclosing* And (whoosh-compat's ast.normalize() "Nothing
    # propagates through And" rule, deliberately kept as-is)
    # or gets dropped from it (real whoosh's And.normalize(), which drops a
    # NullQuery child instead of poisoning). No corpus line uses ANDNOT/
    # ANDMAYBE/REQUIRE at all (grep-verified), so this only affects the
    # hypothesis fuzzers, which generate these operators freely.
    #
    # Scoped to a query that pairs one of the three operators with an
    # operand that already resolves to nothing BEFORE analysis: a literal
    # empty group (however nested) or a parenthesized zero-token value on a
    # TEXT field. That is the condition the mechanism above actually needs,
    # and it is not implied by the operator alone. This entry used to be
    # the bare keyword alternation, which claimed every query mentioning
    # ANDNOT/ANDMAYBE/REQUIRE at all: roughly half of those comparisons are
    # EQUAL ("(title:foo) ANDNOT (title:bar)"), and because the fuzzers
    # skip a claimed shape rather than inverting it, that silently threw
    # away the single largest block of differential coverage in this
    # module. It also shadowed entries 15/33/37/38/39, all of which are
    # ordered after it, whenever their shapes happened to share a query
    # with one of these operators.
    #
    # Measured boundary: "title:foo (() ANDNOT title:bar)" and
    # "title:foo ((title:the) ANDNOT title:bar)" diverge;
    # "title:foo (title:0 ANDNOT title:bar)" (an UNparenthesized zero-token
    # operand, which the analysis-time survivor rule handles instead, see
    # entry 23) and "title:foo ((0) ANDNOT title:bar)" (unfielded, so it
    # multifield-expands rather than resolving to nothing) compare EQUAL.
    (
        # Anchored for the same reason as the entry-23/40 composed pattern
        # above: both clauses here are `.*` existence lookaheads too, so
        # matching only at the string start (rather than letting
        # `.search()` retry from every position) changes nothing
        # observable and turns an O(n^2) unclosed-bracket cost into O(n).
        re.compile(
            r"\A(?=.*\b(?:ANDNOT|ANDMAYBE|REQUIRE)\b)"
            r"(?=.*(?:\((?:\s|\(|\))*\)"
            rf"|\(\s*(?:{TEXT_FIELDS_PATTERN}):"
            rf"{ZERO_TOKEN_WORD}(?:[-,/]{ZERO_TOKEN_WORD})*[-,/]?"
            r"(?![\w.,/-])\s*\)))"
        ),
        (
            "DIVERGENCES.md entry 27: ANDNOT/ANDMAYBE/REQUIRE with a"
            " zero-token positive/required/scored side poisons an enclosing"
            " And on whoosh-compat's side (Nothing-propagation algebra) but"
            " is dropped on whoosh's"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 33): a whitespace-padded quoted value on a
    # BOOLEAN_EXISTS field reads False in whoosh-compat (strips before the
    # trues/falses membership check) but True in real whoosh
    # (BOOLEAN._obj_to_bool checks the *unstripped* text, then falls through
    # to bool(qstring), which is True for any non-empty string). Scoped to a
    # single-quoted BOOLEAN_EXISTS value that has leading or trailing
    # whitespace inside the quotes; a quoted empty value ("''") is
    # deliberately excluded, since that shape no longer diverges (both
    # sides now agree it's False) and is compared normally instead. Field
    # alternation broadened from "has_tag" only to every registered
    # BOOLEAN_EXISTS field after the expanded generator's
    # _bool_exists_quoted_atom (which, unlike the old has_tag-only manual
    # corpus line, draws from all of BOOL_EXISTS_FIELDS) found the identical
    # mismatch on "has_correspondent" and confirmed directly it applies
    # uniformly to has_type/has_path/has_custom_fields/has_owner too (the
    # acceptance-layer result property's grammar-aware generator, which
    # also draws from the same BOOL_EXISTS_FIELDS pool, later found the
    # same mismatch reachable on "has_path" specifically, now listed in
    # the alternation below like its siblings): the
    # root cause (term_query's strip-before-check vs BOOLEAN._obj_to_bool's
    # unstripped-then-bool(qstring) fallback) is the same code path for
    # every BOOLEAN_EXISTS field, not something specific to has_tag.
    #
    # The padded value must also STRIP TO SOMETHING FALSE-ISH, i.e. to the
    # empty string or to one of the four falses whoosh-compat's
    # BOOLEAN_EXISTS coercion recognizes (parser/default.py: "f", "false",
    # "no", "0", case-folded). That is the only way the two sides can
    # disagree: whoosh reads True for any non-empty unstripped text, and
    # whoosh-compat reads True for anything that survives stripping and is
    # not one of the falses, so a padded TRUE-ish value agrees on both
    # sides. The earlier regex claimed any padded value at all, which made
    # its own reason string ("reads False here, True in whoosh") provably
    # false for roughly half of what it claimed ("has_type:'  true'",
    # "has_type:'  xyz  '", both measured EQUAL).
    (
        re.compile(
            rf"\b(?:{BOOL_EXISTS_FIELDS_PATTERN}):"
            r"'(?:\s+(?i:false|no|f|0)\s*|\s*(?i:false|no|f|0)\s+|\s+)'"
        ),
        (
            "DIVERGENCES.md entry 33: a whitespace-padded quoted"
            " BOOLEAN_EXISTS value reads False in whoosh-compat (stripped"
            " before the trues/falses check) but True in whoosh"
            " (unstripped check falls through to bool(qstring))"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 36): a comma-values field boost
    # (`tag:alpha,beta^2`) attaches to the whole split group in
    # whoosh-compat, since CommaValuesPlugin's comma split
    # (FILTER_COMMA_VALUES, priority 105) runs before BoostPlugin binds the
    # boost to the preceding node (FILTER_BOOSTS_POST, priority 510):
    # `Boosted(And(tag:alpha, tag:beta), 2.0)`. Real whoosh has no
    # comma-splitting parser plugin at all (see entry 17 above): its
    # KEYWORD(commas=True) analyzer splits on commas at analysis time, long
    # after the boost already bound to the single, still-unsplit term, so
    # each split term carries its own copy of the boost instead:
    # `And(Boosted(tag:alpha, 2.0), Boosted(tag:beta, 2.0))`. Matched
    # documents and summed relevance scoring are identical either way
    # (verified both algebraically and against a live tantivy index; see
    # the DIVERGENCES.md entry), so this is an AST-shape-only divergence,
    # not a whoosh bug. Scoped to any comma-values field's value followed
    # immediately by a boost (the field alternation is derived from the
    # registry's comma_values flags; a hand-written 'tag'-only scope once
    # silently missed tag_id/custom_fields_id/viewer_id).
    (
        re.compile(rf"\b(?:{COMMA_FIELDS_PATTERN}):[^\s()]*,[^\s()]*\^\d"),
        (
            "DIVERGENCES.md entry 36: a comma-values field boost attaches to"
            " the whole split AndGroup in whoosh-compat but to each split"
            " term individually in whoosh, since the comma split happens at"
            " parse time here vs. analysis time there"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 46): entry 36's analyzer-split sibling.
    # A boosted TEXT value the analyzer splits (title:foo-bar^2) attaches
    # the boost to the whole split group in whoosh-compat (analyze() splits
    # inside the already-bound Boosted wrapper) but to each split term in
    # whoosh (its analyzer splits after the boost bound to the unsplit
    # term). Matched documents identical either way; AST-shape only.
    (
        # Separator class is dash/comma/slash, the characters
        # StandardAnalyzer actually splits a TEXT value on (measured). A
        # single dot between word characters is deliberately EXCLUDED:
        # the analyzer keeps foo.bar as one token, so a dotted-only
        # boosted value never splits, never diverges, and claiming it
        # would make the first dotted corpus line fail as a stale entry.
        re.compile(rf"\b(?:{TEXT_FIELDS_PATTERN}):[^\s()]*\w[-,/]\w[^\s()]*\^\d"),
        (
            "DIVERGENCES.md entry 46: a boost on an analyzer-split TEXT"
            " value attaches to the whole split group in whoosh-compat but"
            " to each split term individually in whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 37): "release_date" is a date_only field
    # registered only in ORACLE_REGISTRY (added purely so the generator can
    # reach "time-bearing value on a date-only field" vocabulary,
    # strategies._date_only_atom): real v2 whoosh has no date-vs-datetime
    # distinction and no such field, so every query against it structurally
    # diverges from the oracle's default-multifield-unknown-field expansion,
    # the same "whoosh-compat-only concept" shape as entry 14's JSON fields.
    # Scoped to the field name itself (any value): confirmed directly that a
    # bare date, a time-bearing bare value, and a time-bearing range all
    # mismatch identically, since the oracle never recognizes the field at
    # all regardless of what value follows it.
    (
        re.compile(r"\brelease_date:"),
        (
            "DIVERGENCES.md entry 37 (design): date_only is a"
            " whoosh-compat-only concept with no whoosh equivalent; the"
            " field itself is unregistered on the real v2 whoosh schema"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 38): real whoosh's BOOLEAN field has
    # no analyzer at all (whoosh.fields.BOOLEAN.__init__ never sets one), but
    # PhrasePlugin.PhraseNode.query unconditionally tries to tokenize a
    # double-quoted value through the field's analyzer before building a
    # Phrase query; confirmed directly that this raises
    # "<class 'whoosh.fields.BOOLEAN'> field has no analyzer" for *every*
    # double-quoted value on a BOOLEAN_EXISTS field (empty, valid, padded,
    # even a pattern-shaped value), not just a specific one. whoosh-compat
    # has no equivalent crash: a double-quoted value on one of these fields
    # parses to an ordinary ast.Phrase and is coerced to a boolean at emit
    # time (visit_phrase), same as documented for entry 8. Not reproduced:
    # a real whoosh limitation, not intended semantics.
    (
        re.compile(
            r"\b(?:has_correspondent|has_tag|has_type|has_path|has_custom_fields|has_owner):\""
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 38): BOOLEAN fields have no"
            " analyzer in real whoosh, so PhrasePlugin crashes while"
            " tokenizing any double-quoted value on one; whoosh-compat"
            " parses it as an ordinary Phrase and coerces at emit time"
            " instead of reproducing the crash"
        ),
        DivergenceKind.ORACLE_ERROR,
    ),
    # design (DIVERGENCES.md entry 39, found by the expanded generator's
    # _numeric_atom): real v2 whoosh's NUMERIC fields all default to
    # bits=32 (oracle_schema() never passes bits= explicitly), so a value at
    # or above each field's real 32-bit ceiling silently fails to parse on
    # the oracle side, while whoosh-compat's U64 kind validates against the
    # full 64-bit domain (tantivy's actual column type). That ceiling isn't
    # uniform: asn/num_notes/custom_field_count pass signed=False (real max
    # 2**32 - 1 = 4294967295), while id/correspondent_id/type_id/path_id/
    # owner_id/page_count leave signed at its library default of True (real
    # max only 2**31 - 1 = 2147483647); confirmed directly per field via
    # _SCHEMA rather than assumed. Scoped to the literal values the
    # generator actually produces one past each boundary (2**32 for the
    # unsigned fields, 2**31 for the signed ones), plus 2**64 - 1 (the u64
    # domain's own ceiling, always out of range regardless of signedness);
    # confirmed directly that each field's own exact max (4294967295 for the
    # unsigned three, 2147483647 for the rest) is NOT included here since it
    # parses identically on both sides. The optional quote is the SINGLE
    # quote only: a double-quoted numeric ("id:\"2147483648\"") is a
    # PhrasePlugin phrase on both sides, never reaches either field's
    # numeric parse, and measurably compares EQUAL, so the double-quote
    # alternative this pattern used to carry only discarded comparisons.
    (
        re.compile(
            r"\b(?:asn|num_notes|custom_field_count):'?4294967296\b"
            r"|\b(?:id|correspondent_id|type_id|path_id|owner_id|page_count):"
            r"'?2147483648\b"
            r"|\b(?:id|asn|correspondent_id|type_id|path_id|owner_id|num_notes"
            r"|custom_field_count|page_count):'?18446744073709551615\b"
        ),
        (
            "DIVERGENCES.md entry 39 (design): whoosh-compat's U64 domain is"
            " the full 64-bit range (tantivy's actual column type); real v2"
            " whoosh's NUMERIC fields default to bits=32 (and, for most of"
            " them, signed=True, halving the usable range again) and"
            " silently fail to parse a value at or above that real ceiling"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 15, now confirmed at the AST-comparison
    # layer too, not just result-level): analyze() resolves a DEFAULT-
    # multitoken field's combinator from the term's *actual* enclosing group
    # (AND vs OR), which real whoosh never does (it always uses the parser's
    # fixed default group, AND here). Multifield expansion of an unfielded
    # (or demoted-unknown-field) value always builds a fresh Or(field1:...,
    # field2:..., ...) at the point the value appears, so every default
    # field's own per-field combinator resolves against that Or
    # unconditionally, regardless of what encloses the expansion elsewhere in
    # the query: any such value that survives its own field's analyzer as
    # two or more tokens (for at least one default TEXT/KEYWORD field)
    # diverges. Two independent textual shapes reach this: a bare (unfielded)
    # word containing an internal separator between two surviving runs
    # (StandardAnalyzer splits on the separator; each half has to clear
    # both of StopFilter's rules, minsize=2 and the stopword list, to
    # survive), and
    # an unregistered ("unknown") field name followed by a colon and a value,
    # which whoosh-compat's FieldsPlugin merges into one literal string
    # (fieldname included) the same way real whoosh's own unknown-field
    # demotion does, tokenizing on the colon boundary the same way a dash or
    # dot would. Both alternatives require each side of the internal
    # separator to survive StopFilter, confirmed directly
    # ("zzz:x"/"a:foobar", where the one-character half is dropped by
    # StandardAnalyzer's minsize=2 and only one token survives per field,
    # do NOT diverge; "ab/the", "901+and" and "zzz:the", where the other
    # half is a stopword, do not either). One measured exception to
    # minsize: İ (U+0130, the only character in Unicode whose str.lower()
    # expands to two codepoints, pinned by test_allowlist_xref's
    # derivation test), whose single-character value survives minsize
    # after lowercasing and genuinely diverges ("zzz:İ", found by a deep
    # fuzz soak), so it is admitted alongside the two-character forms.
    # The stopword half of StopFilter is simulated from whoosh's live
    # STOP_WORDS set (see `_TEXT_STOPWORD` above), not approximated: a
    # dropped stopword is neither a survivor nor a valid unknown-field
    # prefix, and is usable as filler between two real survivors the same
    # way a sub-minsize piece is ("dat:'-1 year to now'" depends on the
    # stopword "to" being droppable filler between "year" and "now").
    # Known field names/aliases are excluded from the unknown-field
    # alternative so an explicitly, correctly fielded value (which only
    # diverges when nested inside a genuine user-written OR, a narrower,
    # context-dependent case covered by the next entry) isn't wrongly
    # swept in here.
    # Two further measured corrections to the bare-value alternative's
    # scope. The separator is a DASH only: a single dot does not split a
    # value at all ("ab.cd" measured EQUAL, matching entry 46's own
    # "title:foo.bar stays one token" finding), so the dot alternative
    # claimed nothing but agreeing shapes. And the two halves must not be
    # the same token: "ab-ab" (and any all-identical chain, "ab-ab-ab")
    # analyzes to a single distinct token, so ANDing and ORing it come out
    # the same and the two sides compare EQUAL. The leading negative
    # lookahead rejects exactly the all-identical chains, case-insensitively
    # ("AB-ab" is one token too, since the analyzer lowercases first),
    # while "ab-cd-ab" still matches, having two distinct tokens. The
    # chain is one-or-more separators, not exactly one: a three-piece
    # value ("ab-cd-ab", "ab-cd-ef") diverges by the same mechanism and
    # was previously unclaimed altogether, a latent hole the narrowing
    # work surfaced (the current generators only ever emit a single dash,
    # so nothing had reached it).
    # The bare alternative's optional surrounding quotes are BALANCED (a
    # conditional on the leading quote's group, not two independent `'?`
    # optionals). A whitespace-delimited fragment sitting INSIDE some
    # other, explicitly fielded quoted value would otherwise satisfy the
    # bare alternative's start anchor and swallow that value's closing
    # quote as its own trailing one, claiming an agreeing shape:
    # measured, "title:'foo bar-baz'" and "tag:'foo bar,baz'" compare
    # EQUAL but were claimed through the fragment "bar-baz'". Requiring
    # the quotes to balance leaves an interior fragment facing an
    # unconsumable closing quote where its value boundary must be, which
    # is exactly the signal that it is not a value of its own. The single
    # quote is excluded from the bare and unknown-field fillers for the
    # same reason (it is the value's delimiter, never literal text in
    # it).
    (
        re.compile(
            r"(?:^|(?<=[\s(]))(?P<e15bq>')?"
            rf"(?:(?!{_survivor_not_all_identical(_TEXT_SURVIVOR, _BARE_FILLER, 'e15bts')})"
            rf"{_survivor_chain(_TEXT_SURVIVOR, _BARE_FILLER)}"
            rf"|(?!{_survivor_not_all_identical(_KEYWORD_SURVIVOR, _KEYWORD_FILLER, 'e15bks')})"
            rf"{_survivor_chain(_KEYWORD_SURVIVOR, _KEYWORD_FILLER)})"
            r"(?(e15bq)'|)(?=[\s)]|$)"
            rf"|(?:^|(?<=[\s(]))\b(?!(?:{REGISTERED_FIELDS_PATTERN}|is_shared)\b)"
            rf"(?!{_TEXT_STOPWORD})(?P<e15ufpfx>\w{{2,}}):"
            rf"{_RANGE_LOOKAHEAD}"
            r'(?!")'
            r"(?:"
            rf"'(?:(?!{_tail_not_all_identical_to_prefix(_UF_QUOTED_FILLER, 'e15ufpfx')})"
            rf"{_survivor_tail(_TEXT_SURVIVOR, _UF_QUOTED_FILLER)}"
            rf"|{_survivor_chain(_KEYWORD_SURVIVOR, _KEYWORD_FILLER)})'(?=[\s)]|$)"
            r"|"
            rf"(?:(?!{_tail_not_all_identical_to_prefix(_UF_FILLER, 'e15ufpfx')})"
            rf"{_survivor_tail(_TEXT_SURVIVOR, _UF_FILLER)}"
            rf"|{_survivor_chain(_KEYWORD_SURVIVOR, _KEYWORD_FILLER)})"
            r"(?=[\s)]|$)"
            r")"
        ),
        (
            "DIVERGENCES.md entry 15: an unfielded or unknown-field-demoted"
            " value that survives at least one default field's own"
            " analyzer as 2+ distinct tokens (TEXT: StandardAnalyzer's"
            " tokenizer plus minsize 2; comma_values KEYWORD: comma split,"
            " no minimum length) resolves Multitoken.DEFAULT against the"
            " multifield expansion's Or context in whoosh-compat, but"
            " against whoosh's fixed AND default in real whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 15, the correctly-fielded sibling of the
    # previous entry): a KNOWN TEXT/KEYWORD field's multi-token value
    # sitting inside a user-written OR resolves Multitoken.DEFAULT against
    # that Or context in whoosh-compat but against whoosh's fixed AND
    # default in real whoosh. A degenerate parenthesized wrapper does not
    # shield the term: analyze() normalizes its input first (making it
    # insensitive to whether the caller pre-normalized), so the singleton
    # group "(title:00-000)" collapses and the term resolves against the
    # enclosing OR, exactly as the production emitter always has (the
    # emitter normalizes before analyzing; only the harness's raw-tree
    # path used to see the un-collapsed wrapper and miss this shape).
    # Scope: the query must contain an OR, and a known TEXT/KEYWORD field
    # (the kinds whose analyzer can split a value) with a value containing
    # an internal [-.,] separator between two word runs. The runs may be
    # single characters: unlike StandardAnalyzer's minsize=2 (the previous
    # entry's approximation for TEXT fields), the KEYWORD analyzers split
    # on commas without a minimum token length, so a quoted "tag:'0,00'"
    # genuinely yields two surviving tokens. This can overmatch a query
    # whose fielded value sits in an AND context elsewhere in the same
    # OR-bearing query, or a TEXT-field value whose 1-char half gets
    # dropped by minsize; per the strict-xfail convention, corpus lines
    # matching this entry must be chosen to genuinely diverge.
    #
    # Three measured scope corrections. (1) The separator/field-kind pairing
    # is not free: a TEXT field splits on a dash or a comma
    # ("title:ab-cd OR x", "title:ab,cd OR x", both diverge), but an
    # UNQUOTED comma value on a comma_values KEYWORD field is split
    # identically at parse time by both sides ("tag:ab,cd OR tag:x" is
    # EQUAL) and only its QUOTED spelling diverges, because whoosh-compat
    # keeps a quoted comma value as one literal (entry 17) while whoosh's
    # KEYWORD analyzer splits it regardless. (2) A dot never splits
    # anything ("title:foo.bar OR x" is EQUAL, see entry 46). (3) The
    # value's pieces must not all be the same token: ANDing and ORing one
    # distinct token coincide, so "title:ab-ab OR x" and "tag:'a,a' OR x"
    # compare EQUAL. The earlier pattern tested none of these and threw
    # away four out of five of the comparisons it claimed.
    #
    # This entry's values are always EXPLICITLY fielded, which is where it
    # parts company with the previous entry's filler and survivor
    # definitions. An interior colon here is literal text the field's own
    # analyzer sees ("title:abc:ab", "title:'hello:90'" both diverge),
    # never the field-value boundary it is at the start of a BARE value,
    # so this entry uses the colon-admitting `_FIELDED_FILLER` and
    # `_FIELDED_KEYWORD_SURVIVOR` instead.
    (
        re.compile(
            r"^(?=.*\bOR\b)(?=.*(?:"
            rf"\b(?:{TEXT_FIELDS_PATTERN}):'?"
            rf"(?:(?!{_survivor_not_all_identical(_TEXT_SURVIVOR, _FIELDED_FILLER, 'e15sts')})"
            rf"{_survivor_chain(_TEXT_SURVIVOR, _FIELDED_FILLER)})"
            rf"|\b(?:{KEYWORD_FIELDS_PATTERN}):'"
            rf"(?:(?!{_survivor_not_all_identical(_FIELDED_KEYWORD_SURVIVOR, _KEYWORD_FILLER, 'e15sks')})"
            rf"{_survivor_chain(_FIELDED_KEYWORD_SURVIVOR, _KEYWORD_FILLER)})"
            r"))"
        ),
        (
            "DIVERGENCES.md entry 15: a known TEXT/KEYWORD field's"
            " multi-token value inside a user-written OR resolves"
            " Multitoken.DEFAULT against the enclosing Or context in"
            " whoosh-compat, but against whoosh's fixed AND default in real"
            " whoosh; a singleton paren wrapper does not shield the term,"
            " since analyze() normalizes before resolving context. The"
            " value must be unquoted or single-quoted: a double-quoted"
            " value becomes a Phrase node, which has no Multitoken.DEFAULT"
            " combinator question at all"
        ),
        DivergenceKind.MISMATCH,
    ),
    # out-of-scope (DIVERGENCES.md entry 42): the v2 whoosh schema's
    # is_shared BOOLEAN column is deliberately not a registered field
    # (paperless's tantivy backend does permission filtering outside
    # whoosh-compat, and its public search surface does not expose
    # is_shared), so the oracle parses is_shared:<value> as a typed
    # boolean Term while whoosh-compat demotes it as an unknown field.
    # This is a known-to-oracle-only shape, distinct from entry 15's
    # both-sides-unknown multitoken class, whose regex above excludes
    # is_shared for exactly that reason.
    (
        re.compile(r"\bis_shared:"),
        (
            "DIVERGENCES.md entry 42: is_shared is deliberately not a"
            " registered field (v2's BOOLEAN permission-bookkeeping column;"
            " paperless's tantivy backend filters permissions outside"
            " whoosh-compat), so the oracle parses a typed boolean Term"
            " while whoosh-compat demotes the unknown field to text"
        ),
        DivergenceKind.MISMATCH,
    ),
]


def allowed_reason(query: str) -> str | None:
    """The reference string of the first allowlist entry matching ``query``,
    or ``None`` if no entry matches (i.e. the query must be compared).

    Returning the specific matched reason (rather than a bare bool) lets
    callers report a distinct, auditable skip reason per query instead of a
    single catch-all "allowlisted" message.
    """

    for pattern, reason, _kind in ALLOW:
        if pattern.search(query):
            return reason
    return None


def allowed_entry(query: str) -> tuple[str, DivergenceKind] | None:
    """Like :func:`allowed_reason`, but also returns the matched entry's
    :class:`DivergenceKind`, for callers implementing this module's
    strict-xfail semantics (``test_differential.py``'s ``test_matches_oracle``).
    Returns ``None`` if no entry matches.
    """

    for pattern, reason, kind in ALLOW:
        if pattern.search(query):
            return reason, kind
    return None


def allowed(query: str) -> bool:
    """True if ``query`` matches an intended-divergence pattern and should be
    skipped rather than compared structurally.
    """

    return allowed_reason(query) is not None
