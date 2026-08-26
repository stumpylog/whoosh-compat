"""Grammar-aware Hypothesis strategy for whoosh-compat's query language.

Existing property tests (``test_hypothesis.py``'s ``words``/``atom``/``clause``
strategy) only ever compose ``field:word`` atoms with AND/OR/ANDNOT: they
cannot generate wildcards, ranges, quoted phrases, boosts, date keywords,
comma lists, or JSON subpaths, and they cannot generate a value that
analyzes to zero tokens. Every serious defect found in this project recently
was a *composition* gap (a feature that worked fine tested alone, but broke
once nested inside another feature), so this module's job is specifically to
nest the full supported surface arbitrarily deep, and to place "awkward"
values, most importantly ones whose analyzer consumes every token, in every
structural position: as a group child, as either operand of every binary
operator, under NOT, under a boost, and at depth.

The grammar covered here mirrors README.md's syntax table exactly: no
invented syntax the library doesn't support. Field names are drawn from
``tests/differential/oracle.py``'s ``ORACLE_REGISTRY`` (the same registry the
differential harness already parses against), plus the JSON-subpath-shaped
dotted names (``notes.user`` etc.) that ``allowlist.py`` already documents as
an out-of-scope-for-the-oracle divergence (DIVERGENCES.md entry 14): they are
included here because generating them is exactly what deliverable 1 asks
for, and the existing allowlist entry already accounts for the comparison
being skipped.

Zero-token placement: whoosh's ``StandardAnalyzer`` (the oracle registry's
TEXT-field analyzer, see ``oracle._analyze``) drops both its default
stopword list and any token shorter than ``minsize=2``. ``_ZERO_TOKEN_WORDS``
below is verified once, at import time, to analyze to zero tokens under that
exact analyzer, so every query the strategy builds using one of these words
in a TEXT-field position is guaranteed (not just likely) to exercise the
drop path, wherever in the tree it lands.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable
from collections.abc import Iterable

from hypothesis import strategies as st

from tests.differential.oracle import _SCHEMA
from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import _analyze
from whoosh_compat import ast
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

# --------------------------------------------------------------------------
# Field vocabulary, drawn from the oracle registry so every generated query
# is meaningful against the same schema the parity property already checks.
# --------------------------------------------------------------------------

TEXT_FIELDS: tuple[str, ...] = tuple(s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.TEXT)
KEYWORD_FIELDS: tuple[str, ...] = tuple(
    s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.KEYWORD
)
NUM_FIELDS: tuple[str, ...] = tuple(s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.U64)
DATE_FIELDS: tuple[str, ...] = tuple(
    s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.DATETIME
)
BOOL_EXISTS_FIELDS: tuple[str, ...] = tuple(
    s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.BOOLEAN_EXISTS
)
# Not registered in ORACLE_REGISTRY (v2 whoosh has no JSON concept at all,
# see DIVERGENCES.md entry 14): allowlist.py already skips the structural
# comparison for exactly these dotted names.
JSON_SUBPATHS: tuple[str, ...] = (
    "notes.user",
    "notes.note",
    "custom_fields.value",
    "custom_fields.name",
)
# Bare (subpath-less) canonical names of every registered JSON field, for
# exercising the "bare JSON field name demotes to a text search" shape
# (DIVERGENCES.md entries 14/29, FieldRegistry.is_bare_json_field).
JSON_FIELDS: tuple[str, ...] = tuple(s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.JSON)
# "attrs" (unlike JSON_SUBPATHS's notes.*/custom_fields.* names) is a
# genuinely registered JSON field in ORACLE_REGISTRY, added specifically so
# this module can generate real JSON-subpath pattern/existence/quoted-value
# queries (DIVERGENCES.md entry 14's oracle.py comment): the oracle still has
# no matching field at all, so every query against it structurally diverges
# the same way notes.user/custom_fields.value already do, just via a field
# that (unlike those two) isn't also a plain-TEXT field on the oracle side.
REGISTERED_JSON_SUBPATHS: tuple[str, ...] = tuple(
    f"{spec.name}.{subpath}"
    for spec in ORACLE_REGISTRY
    if spec.kind is FieldKind.JSON
    for subpath in spec.subpaths
)
# A date_only field (DIVERGENCES.md entry 37): real v2 whoosh has no
# date-vs-datetime distinction at all, so this field exists purely so the
# generator can reach "time-bearing value on a date-only field" shapes.
DATE_ONLY_FIELDS: tuple[str, ...] = tuple(
    s.name for s in ORACLE_REGISTRY if s.kind is FieldKind.DATE
)

# Verified (not assumed) to analyze to zero tokens under the oracle's own
# StandardAnalyzer: common stopwords, plus single characters shorter than
# its default minsize=2.
_ZERO_TOKEN_CANDIDATES = (
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "in",
    "is",
    "it",
    "by",
    "0",
    "1",
    "9",
    "x",
)
ZERO_TOKEN_WORDS: tuple[str, ...] = tuple(w for w in _ZERO_TOKEN_CANDIDATES if _analyze(w) == [])
assert ZERO_TOKEN_WORDS, "the analyzer contract this module exploits no longer holds"

# The multi-word keywords are listed in their QUOTED spelling only, and
# deliberately so: do not "complete" this list with the bare spellings that
# DIVERGENCES.md entry 19 also accepts. The generator composes these into
# larger queries, including inside quoted phrase values, and the oracle's
# _rewrite_natural_date_keywords is a quote-blind regex: on a generated
# `title:"x created:previous month y"` it rewrites the phrase's interior
# while whoosh-compat correctly leaves the phrase alone. That is a real,
# expected divergence, so adding the bare spellings here would produce
# recurring fuzz failures whose only remedy is an allowlist entry
# suppressing the very corruption entry 19 exists to remove. The bare
# spellings are compared against the oracle by the fifteen static corpus
# lines in corpus_paperless.txt/corpus_docs.txt, where the rewrite is
# legitimate because the phrase really is the field's value.
DATE_KEYWORDS: tuple[str, ...] = (
    "today",
    "yesterday",
    "'previous week'",
    "'previous month'",
    "'previous quarter'",
    "'previous year'",
    "'this month'",
    "'this year'",
)
DATE_RELATIVE: tuple[str, ...] = (
    "now-7d",
    "now+1h",
    "now-1h",
    "-1 week",
    "-3mos",
    "-999yrs",
    "-1y",
)

# --------------------------------------------------------------------------
# Word-level building blocks
# --------------------------------------------------------------------------

_word_chars = st.characters(whitelist_categories=("Ll", "Lu", "Nd"))
words = st.text(alphabet=_word_chars, min_size=1, max_size=8)
dashed_word = st.builds(lambda a, b: f"{a}-{b}", words, words)
plain_word = st.one_of(words, dashed_word)
# 3-5 piece dash chains ("ab-cd-ef"), issue #57's "multi-piece dash chains"
# item. Deliberately NOT folded into dashed_word/plain_word above: those
# feed 5 other atoms (_term_atom's unfielded branch, every JSON atom), and
# widening them would change what those atoms already generate; kept as
# its own pool for a dedicated leaf instead, same reasoning as the
# comma-clause and dash-negation atoms below.
#
# Each piece is filtered through _analyze (a zero-token piece, e.g. a bare
# "0", isn't ruled out by `words`' own min_size=1 the way phrase_word's
# min_size=2 mostly does): a chain with an embedded zero-token piece nested
# under NOT surfaced 85 unclaimed divergences in an initial sweep, the same
# zero-token-survivor-count mechanism the comma-clause stopword fix above
# already closed once this sweep-and-verify session.
_dash_chain_word = words.filter(lambda w: _analyze(w) != [])
_multi_dash_word = st.lists(_dash_chain_word, min_size=3, max_size=5).map(
    lambda parts: "-".join(parts)
)

# Wildcard/prefix pattern literals stay lowercase-ASCII-only: any uppercase
# or non-ASCII letter in a wildcard/prefix pattern hits DIVERGENCES.md
# entry 2 (whoosh case-folds such patterns into the AST at parse time,
# whoosh-compat only at emit time, see allowlist.py's entry-2 comment), a
# real but already-documented, single-purpose divergence this grammar
# generator isn't trying to re-discover on every run; keeping this
# generator's own alphabet case/ASCII-stable sidesteps it so the property
# stays focused on composition (this module's actual job, see the module
# docstring) instead of repeatedly re-tripping entry 2.
_wildcard_chars = st.characters(whitelist_categories=("Ll", "Nd"))
wildcard_base = st.text(alphabet=_wildcard_chars, min_size=1, max_size=8)

# Phrase content words, deliberately >= 2 characters and never dashed: a
# length-1 word (StandardAnalyzer's minsize=2) or a dashed word whose
# halves are each short enough to tokenize away (e.g. "0-0" -> ["0", "0"],
# both dropped) can make an *entire* multi-word phrase silently degrade to
# zero tokens by accident, the same DIVERGENCES.md entry 24 shape as the
# dedicated all-zero-token phrase atom below, but with no tractable static
# allowlist regex (it would have to simulate tokenization on arbitrary
# generated text to detect it). The dedicated zero-token phrase path
# already covers that shape deliberately and matchably; this pool exists
# so the "ordinary" phrase path doesn't also stumble into it by accident.
_phrase_word_chars = st.characters(whitelist_categories=("Ll", "Lu", "Nd"))
phrase_word = st.text(alphabet=_phrase_word_chars, min_size=2, max_size=8)


# --------------------------------------------------------------------------
# Leaf (atom) generators. Every atom renders directly to a query-string
# fragment; kind is a short label used for structural-type-diversity
# bookkeeping in tests, not consumed by the parser.
# --------------------------------------------------------------------------


@st.composite
def _term_atom(draw: st.DrawFn) -> str:
    field = draw(st.one_of(st.none(), st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS)))
    text = draw(plain_word)
    return f"{field}:{text}" if field else text


@st.composite
def _zero_token_atom(draw: st.DrawFn) -> str:
    # Only TEXT fields drop tokens (the oracle's KEYWORD analyzer has no
    # stopword/minsize filtering, see oracle.py's _analyze_keyword* comment).
    field = draw(st.sampled_from(TEXT_FIELDS))
    text = draw(st.sampled_from(ZERO_TOKEN_WORDS))
    return f"{field}:{text}"


@st.composite
def _phrase_atom(draw: st.DrawFn) -> str:
    # The field is optional: an unfielded quoted phrase ('"electricity
    # bill"') is what users type far more often than a fielded one, is
    # multifield-expanded rather than single-field, and was unreachable
    # here until corpus_realworld.txt showed it up (measured EQUAL).
    field = draw(st.one_of(st.none(), st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS)))
    n = draw(st.integers(min_value=1, max_value=4))
    # An all-zero-token phrase stays FIELDED. Unfielded, it is a genuine
    # unclaimed divergence (measured: '"to"' leaves whoosh with an empty
    # Phrase per default TEXT field and whoosh-compat with only the KEYWORD
    # field's surviving phrase), unlike its fielded sibling, which entry 24
    # already claims; see the pre-release real-world-corpus report.
    all_zero = field is not None and draw(st.booleans())
    if all_zero:
        parts = draw(st.lists(st.sampled_from(ZERO_TOKEN_WORDS), min_size=n, max_size=n))
    else:
        parts = draw(st.lists(phrase_word, min_size=n, max_size=n))
    text = " ".join(parts)
    slop = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=3)))
    slop_s = f"~{slop}" if slop is not None else ""
    prefix = f"{field}:" if field else ""
    return f'{prefix}"{text}"{slop_s}'


@st.composite
def _wildcard_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS))
    base = draw(wildcard_base)
    shape = draw(
        st.sampled_from(
            [
                "trailing",
                "leading",
                "infix",
                "bracket_trailing",
                "bracket_infix",
                "question",
                # Shapes the real-world corpus (corpus_realworld.txt) showed
                # users actually type and this generator could not produce:
                # a wrapped star ("tag:*kassel*"), a bracket character class
                # with no star at all ("title:200[1-9]") and one behind a
                # leading star ("title:*202[0-4]"). The last two are the
                # exact shapes paperless-ngx#13568 was filed about, and all
                # three measurably compare EQUAL against the oracle, so
                # generating them widens composition coverage without
                # widening the divergence surface.
                "wrapped",
                "bracket_bare",
                "bracket_leading_star",
            ]
        )
    )
    if shape == "trailing":
        pattern = f"{base}*"
    elif shape == "leading":
        pattern = f"*{base}"
    elif shape == "infix":
        pattern = f"{base}*{base}"
    elif shape == "bracket_trailing":
        lo = draw(st.integers(min_value=0, max_value=5))
        hi = draw(st.integers(min_value=lo, max_value=9))
        # DIVERGENCES.md entry 13: a trailing star after a bracket class is
        # a real whoosh bug (Wildcard.normalize()/do_wildcards only test for
        # "*"/"?" before folding to Prefix, silently dropping the class);
        # allowlist.py's entry-13 pattern is scoped broadly enough to cover
        # every field/value this shape can generate, not just the one
        # corpus string it was originally written against.
        pattern = f"{base}[{lo}-{hi}]*"
    elif shape == "bracket_infix":
        lo = draw(st.integers(min_value=0, max_value=5))
        hi = draw(st.integers(min_value=lo, max_value=9))
        pattern = f"{base}[{lo}-{hi}]{base}"
    elif shape == "wrapped":
        pattern = f"*{base}*"
    elif shape == "bracket_bare":
        lo = draw(st.integers(min_value=0, max_value=5))
        hi = draw(st.integers(min_value=lo, max_value=9))
        pattern = f"{base}[{lo}-{hi}]"
    elif shape == "bracket_leading_star":
        lo = draw(st.integers(min_value=0, max_value=5))
        hi = draw(st.integers(min_value=lo, max_value=9))
        pattern = f"*{base}[{lo}-{hi}]"
    else:
        pattern = f"{base}?{base}"
    return f"{field}:{pattern}"


@st.composite
def _numeric_range_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(NUM_FIELDS))
    lo = draw(st.integers(min_value=0, max_value=100_000))
    hi = draw(st.integers(min_value=lo, max_value=lo + 100_000))
    open_lo = draw(st.booleans())
    open_hi = draw(st.booleans())
    ob = draw(st.sampled_from(["[", "{"]))
    cb = draw(st.sampled_from(["]", "}"]))
    # A genuinely open bound must have *no* whitespace where the value
    # would go (RangePlugin's regex only treats the bound as absent, i.e.
    # None, when its capture group doesn't match at all; a bound that
    # matches as an empty/whitespace-only string is a different, much
    # stranger case real whoosh treats inconsistently and this generator
    # isn't trying to explore, see README's own "asn:[100 to]" open-range
    # example, which has no space where the missing bound would be).
    lo_s = "" if open_lo else f"{lo} "
    hi_s = "" if open_hi else f" {hi}"
    return f"{field}:{ob}{lo_s}TO{hi_s}{cb}"


@st.composite
def _date_bare_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(DATE_FIELDS))
    shape = draw(st.sampled_from(["year", "compact", "separated", "keyword", "relative"]))
    if shape == "year":
        year = draw(st.integers(min_value=1, max_value=9999))
        value = f"{year:04d}"
    elif shape == "compact":
        year = draw(st.integers(min_value=1900, max_value=2100))
        month = draw(st.integers(min_value=1, max_value=12))
        day = draw(st.integers(min_value=1, max_value=28))
        value = f"{year:04d}{month:02d}{day:02d}"
    elif shape == "separated":
        # DIVERGENCES.md entry 18: a bare separated-ISO date structurally
        # diverges even though both sides agree on the value; already
        # allowlisted broadly by field-prefix + digit-separator shape.
        year = draw(st.integers(min_value=1900, max_value=2100))
        month = draw(st.integers(min_value=1, max_value=12))
        day = draw(st.integers(min_value=1, max_value=28))
        value = f"{year:04d}-{month:02d}-{day:02d}"
    elif shape == "keyword":
        value = draw(st.sampled_from(DATE_KEYWORDS))
    else:
        value = draw(st.sampled_from(DATE_RELATIVE))
    return f"{field}:{value}"


@st.composite
def _date_range_atom(draw: st.DrawFn) -> str:
    # DIVERGENCES.md entry 12 (whoosh-bug): every bracketed range on a
    # DATE/DATETIME field is already allowlisted broadly (the tz-reversal
    # wiring defect in real whoosh/paperless-v2), so any shape generated
    # here stays a documented, attributed skip rather than a new failure.
    field = draw(st.sampled_from(DATE_FIELDS))
    lo = draw(st.one_of(st.just(""), st.integers(min_value=1, max_value=9999).map(str)))
    hi = draw(st.one_of(st.just(""), st.integers(min_value=1, max_value=9999).map(str)))
    ob = draw(st.sampled_from(["[", "{"]))
    cb = draw(st.sampled_from(["]", "}"]))
    # Same no-whitespace-when-absent rationale as _numeric_range_atom above.
    lo_s = "" if lo == "" else f"{lo} "
    hi_s = "" if hi == "" else f" {hi}"
    return f"{field}:{ob}{lo_s}TO{hi_s}{cb}"


@st.composite
def _comma_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(KEYWORD_FIELDS))
    n = draw(st.integers(min_value=2, max_value=4))
    parts = draw(st.lists(words, min_size=n, max_size=n))
    quoted = draw(st.booleans())
    joined = ",".join(parts)
    text = f"'{joined}'" if quoted else joined
    return f"{field}:{text}"


@st.composite
def _field_group_atom(draw: st.DrawFn) -> str:
    """A field applied to a parenthesised group of words (``tag:(MK 2022
    Kassel)``) and the same value written with a space after the colon
    (``tag: taxes``).

    Both are common in the real-world corpus (corpus_realworld.txt) and
    neither was reachable before: every other atom here writes
    ``field:value`` with no space and no group. Measured EQUAL against the
    oracle for the shapes the corpus carries.
    """

    field = draw(st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS))
    if draw(st.booleans()):
        n = draw(st.integers(min_value=2, max_value=3))
        # phrase_word (>= 2 characters, never dashed), not plain_word: a
        # group of values that all analyze to zero tokens reaches the
        # entry-23/24 collapse through a "field:(" spelling the claiming
        # regexes key on "field:<word>" and therefore miss (measured:
        # "NOT (NOT checksum:(x x))" diverges unclaimed while
        # "NOT (NOT (checksum:x checksum:x))" is claimed). That mechanism
        # is already generated by the fielded zero-token atoms; this atom
        # exists for the grouping shape, so it stays out of it.
        parts = draw(st.lists(phrase_word, min_size=n, max_size=n))
        return f"{field}:({' '.join(parts)})"
    return f"{field}: {draw(phrase_word)}"


@st.composite
def _comma_clause_field_term(draw: st.DrawFn) -> str:
    # phrase_word (>= 2 characters, never dashed), not plain_word: a
    # zero-token value (a bare "0", or a dashed "0-0" whose halves both
    # tokenize away) under NOT and comma-joined with another instance of
    # itself hits the same already-claimed shape as its whitespace sibling
    # (measured: "NOT title:0-0 title:0-0" is already allowed) but through
    # a comma the existing allowlist pattern doesn't recognize as an
    # equivalent separator. phrase_word sidesteps generating that
    # shape here entirely, the same reasoning _field_group_atom documents.
    #
    # phrase_word's own minsize=2 only rules out the minsize half of
    # StandardAnalyzer's drop rule, not the stopword half: it can still
    # draw one of whoosh's own STOP_WORDS ("will", "the", "is", ...),
    # which analyzes to zero tokens on a TEXT field exactly like a
    # too-short word does (measured, code review caught this: "NOT
    # title:will,title:will" is an unclaimed divergence, whoosh-compat
    # produces Every where real whoosh produces Nothing). The `.filter()`
    # below re-verifies the same "does this survive the field's own
    # analyzer" question `_analyze` already answers for ZERO_TOKEN_WORDS
    # above, rather than hand-duplicating the stopword list and risking
    # the exact same kind of gap a second time.
    field = draw(st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS))
    text = draw(phrase_word.filter(lambda w: _analyze(w) != []))
    return f"{field}:{text}"


# Deliberately FIELDED clauses only (never _term_atom()'s unfielded
# branch): an unfielded word directly followed by more comma-glued
# content extends DIVERGENCES.md entry 15's Multitoken.DEFAULT chain
# past what its allowlist regex currently matches (measured: "foo,bar"
# alone is claimed, but "foo,bar,path_id:[TO]" is not, even though it's
# the same mechanism, just a shape the regex wasn't scoped for).
# Extending that regex is its own, separately scoped piece of work; this
# generator sticks to the fielded-clause-list shape the real-world
# corpus actually shows (``created:[-1 week to now],added:[-1 month to
# now]``), which needs no allowlist change at all.
#
# Built once at module load, like _every_atom/_empty_group_atom above:
# _comma_clause_atom previously called a function returning a fresh
# st.one_of(...) on every single draw instead of reusing one strategy
# object, rebuilding the whole pool's strategy graph thousands of times
# per run for no benefit (code review caught this).
_COMMA_CLAUSE_POOL: st.SearchStrategy[str] = st.one_of(
    *(
        [_comma_clause_field_term(), _date_range_atom()]
        + ([_numeric_range_atom()] if NUM_FIELDS else [])
    )
)


@st.composite
def _comma_clause_atom(draw: st.DrawFn) -> str:
    """Whole fielded clauses joined by a bare comma, no surrounding
    whitespace (``created:[-1 week to now],added:[-1 month to now]``),
    issue #57's "comma separators and comma-joined clause lists" item.

    Deliberately a dedicated leaf, not an ``_extend`` combinator, and
    deliberately fielded-clauses-only: see ``_COMMA_CLAUSE_POOL``'s
    comment for both scoping decisions and the sweeps that motivated
    them. Measured clean (0 unclaimed divergences) over a 6000-draw sweep
    restricted to this leaf.
    """

    n = draw(st.integers(min_value=2, max_value=3))
    parts = draw(st.lists(_COMMA_CLAUSE_POOL, min_size=n, max_size=n))
    return ",".join(parts)


@st.composite
def _dash_chain_atom(draw: st.DrawFn) -> str:
    """A 3-5 piece dash chain (``ab-cd-ef``), fielded or bare, issue #57's
    "multi-piece dash chains" item: the existing ``dashed_word`` only ever
    produces exactly 2 pieces.

    Both fielded and unfielded, unlike the comma/dash-negation atoms above:
    entry 15's own allowlist comment already generalizes its dash-chain
    match to "one-or-more separators, not exactly one", and this was
    verified directly rather than trusted (``title:ab-cd-ef`` compares
    EQUAL; the unfielded ``ab-cd-ef``/``ab-cd-ef-gh-ij`` are already
    claimed). Measured clean (0 unclaimed divergences) over a 6000-draw
    sweep restricted to this leaf.
    """

    field = draw(st.one_of(st.none(), st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS)))
    text = draw(_multi_dash_word)
    return f"{field}:{text}" if field else text


@st.composite
def _json_atom(draw: st.DrawFn) -> str:
    path = draw(st.sampled_from(JSON_SUBPATHS))
    value = draw(plain_word)
    return f"{path}:{value}"


@st.composite
def _bool_exists_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(BOOL_EXISTS_FIELDS))
    value = draw(st.sampled_from(["true", "false"]))
    return f"{field}:{value}"


# Fielded clauses only, the same restriction and for the same reason as
# _COMMA_CLAUSE_POOL: "-" directly abutting an UNFIELDED value defeats
# entry 15's left-boundary requirement (measured: "-title:2024" and
# even "-title:ab-cd", a fielded *multi-token* value, compare EQUAL;
# only the unfielded "-ab-cd" diverges, already documented). Every
# atom drawn here always carries its own field prefix, so the
# entry-15 unfielded/unknown-field collision never applies.
#
# Built once at module load, same reason as _COMMA_CLAUSE_POOL above.
_DASH_NEGATED_POOL: st.SearchStrategy[str] = st.one_of(
    *(
        [_comma_clause_field_term(), _wildcard_atom(), _date_bare_atom()]
        + ([_numeric_range_atom()] if NUM_FIELDS else [])
        + ([_bool_exists_atom()] if BOOL_EXISTS_FIELDS else [])
    )
)


@st.composite
def _dash_negated_atom(draw: st.DrawFn) -> str:
    """A ``-``-prefixed negated fielded clause (``-title:2024``), issue
    #57's "-" prefix negation item. Real usage (corpus_realworld.txt,
    paperless-ngx#13568) and this library's own README document it as
    equivalent to ``NOT field:value``.

    Deliberately a dedicated leaf, not an ``_extend`` combinator, and
    deliberately fielded-clauses-only: see ``_DASH_NEGATED_POOL``'s
    comment for the scoping reason. Measured clean (0 unclaimed
    divergences) over a 6000-draw sweep restricted to this leaf.
    """

    return f"-{draw(_DASH_NEGATED_POOL)}"


# Punctuation characters that can lead a bare term without turning it into
# some other construct. Excludes the two quote characters (they start a
# phrase/quoted value and are deliberately left unclaimed by entry 15's
# allowlist regex, see DIVERGENCES.md entry 15) and the two wildcard
# characters "?" and "*" (WildcardPlugin routes them into a pattern node,
# a different shape entirely). "." is included here but the atom below is
# bare-values-only, which keeps it away from ".title:ab-cd": a leading dot
# before a REGISTERED field name is entry 14's dot-inclusive fieldname
# tagger, still unclaimed, and generating it would be a CI failure rather
# than a finding.
_LEADING_PUNCTUATION = "-+><!@#$%&=|~;./"


@st.composite
def _leading_punct_atom(draw: st.DrawFn) -> str:
    """A bare value behind a leading punctuation character (``-ab-cd``,
    ``>ab-cd``), the shape entry 15's allowlist regex used to require
    start-of-string, whitespace or "(" in front of.

    The punctuation is not part of any token the analyzer emits, so a
    multi-token value here diverges exactly as its bare spelling does and
    is claimed by the same entry; a single-token value agrees on both
    sides. Deliberately UNFIELDED: the fielded row is not uniformly safe,
    since a leading "." before a registered field name is entry 14's
    mechanism and stays unclaimed. Measured clean (0 unclaimed
    divergences) over a 6000-draw sweep restricted to this leaf.
    """

    punct = draw(st.sampled_from(_LEADING_PUNCTUATION))
    text = draw(st.one_of(plain_word, _multi_dash_word))
    return f"{punct}{text}"


# --------------------------------------------------------------------------
# Additional leaves, added to close vocabulary gaps a full-library review
# found: shapes that were reachable *in principle* by the grammar but had no
# generator support, so a real defect in each of them survived both manual
# review and the existing fuzzer. Same treatment as every atom above: each
# one is a leaf `_leaves()` mixes in, so `_extend`'s combinators nest it
# arbitrarily deep, pair it with other leaves under every binary operator,
# and place it under NOT/boost, not just generate it standalone.
# --------------------------------------------------------------------------

_U64_MAX = 2**64 - 1
# Real v2 whoosh's NUMERIC fields (oracle_schema()) all use the library
# default bits=32 (none of asn/id/*_id/num_notes/etc pass bits=64):
# confirmed directly (`_SCHEMA["asn"].bits == 32`). whoosh-compat's U64 kind
# instead assumes the full 64-bit domain tantivy's actual v3 u64 columns
# support. DIVERGENCES.md entry 39 documents the resulting divergence for
# any value at/above each field's real-whoosh ceiling. That ceiling isn't
# uniform, though: a NUMERIC field's real 32-bit domain depends on its own
# `signed` flag (`asn`/`num_notes`/`custom_field_count` pass
# `signed=False`, so their true max is 2**32 - 1; every other NUMERIC field
# in oracle_schema() leaves `signed` at its library default of True, so
# their true max is only 2**31 - 1), confirmed by inspecting `_SCHEMA`
# directly rather than assuming a single shared boundary (the first version
# of this atom did, and "id:4294967295" turned out to still mismatch,
# since "id" is signed).
_WHOOSH32_FIELD_MAX: dict[str, int] = {
    name: (2 ** (_SCHEMA[name].bits - 1) - 1)
    if _SCHEMA[name].signed
    else (2 ** _SCHEMA[name].bits - 1)
    for name in NUM_FIELDS
}


@st.composite
def _numeric_atom(draw: st.DrawFn) -> str:
    """Bare/single-/double-quoted numeric values on a U64 field, including
    negative and out-of-u64-domain magnitudes, the exact u64 domain
    boundaries (0 and 2**64 - 1), and each field's own real-whoosh 32-bit
    NUMERIC boundary (its exact max, still valid on both sides, and one past
    it, the minimal reproduction of DIVERGENCES.md entry 39).

    Most shapes here are parseable text that whoosh-compat's ``_parse_u64``
    diagnoses as ``BAD_NUMBER`` at parse time (``parser/default.py``), which
    the differential harness skips uniformly (DIVERGENCES.md entry 6): the
    point of generating them is reachability/crash-safety
    (``test_parse_never_raises``, ``test_normalize_is_total_and_idempotent``),
    not a new oracle-comparison outcome. ``u64_max``/``whoosh32_overflow``
    are the exception: both are valid, undiagnosed u64 values that still
    structurally diverge from the oracle (entry 39), since real whoosh's
    32-bit field can never hold either one. Quoting matters: a single-quoted
    value still reaches ``term_query`` (an ``ast.Term``, exactly like an
    unquoted value), while a double-quoted value reaches ``visit_phrase``'s
    own, separately implemented U64 domain check.
    """

    field = draw(st.sampled_from(NUM_FIELDS))
    shape = draw(
        st.sampled_from(
            [
                "valid",
                "negative",
                "oob_small",
                "oob_huge",
                "boundary_lo",
                "u64_max",
                "whoosh32_max",
                "whoosh32_overflow",
            ]
        )
    )
    if shape == "valid":
        text = str(draw(st.integers(min_value=0, max_value=100_000)))
    elif shape == "negative":
        text = f"-{draw(st.integers(min_value=1, max_value=100_000))}"
    elif shape == "oob_small":
        # Just past the u64 ceiling: the domain check's off-by-one boundary.
        text = str(_U64_MAX + draw(st.integers(min_value=1, max_value=10)))
    elif shape == "oob_huge":
        text = str(_U64_MAX + draw(st.integers(min_value=100_000, max_value=10**12)))
    elif shape == "boundary_lo":
        text = "0"
    elif shape == "u64_max":
        text = str(_U64_MAX)
    elif shape == "whoosh32_max":
        text = str(_WHOOSH32_FIELD_MAX[field])
    else:
        text = str(_WHOOSH32_FIELD_MAX[field] + 1)
    quote = draw(st.sampled_from(["bare", "single", "double"]))
    if quote == "single":
        value = f"'{text}'"
    elif quote == "double":
        value = f'"{text}"'
    else:
        value = text
    return f"{field}:{value}"


@st.composite
def _bool_exists_quoted_atom(draw: st.DrawFn) -> str:
    """Single-quoted (``Term``) BOOLEAN_EXISTS values not already covered by
    the plain ``_bool_exists_atom`` above: empty, whitespace-padded (both of
    which DIVERGENCES.md entry 33 documents for the non-empty/padded case),
    and a pattern value (diagnosed, DIVERGENCES.md entry 29).
    """

    field = draw(st.sampled_from(BOOL_EXISTS_FIELDS))
    shape = draw(
        st.sampled_from(["empty", "pad_leading", "pad_trailing", "case_variant", "pattern"])
    )
    if shape == "empty":
        return f"{field}:''"
    if shape == "pad_leading":
        v = draw(st.sampled_from(["true", "false"]))
        return f"{field}:'  {v}'"
    if shape == "pad_trailing":
        v = draw(st.sampled_from(["true", "false"]))
        return f"{field}:'{v}  '"
    if shape == "case_variant":
        v = draw(st.sampled_from(["T", "F", "Yes", "No"]))
        return f"{field}:'{v}'"
    return f"{field}:t*"


@st.composite
def _bool_exists_double_quoted_atom(draw: st.DrawFn) -> str:
    """Double-quoted (``Phrase``) BOOLEAN_EXISTS values: a distinct AST node
    type from the single-quoted ``Term`` shapes above (real whoosh's
    ``BOOLEAN`` field has no analyzer at all, so *any* ``PhrasePlugin`` value
    on one of these fields crashes the oracle at parse time, confirmed
    directly; see DIVERGENCES.md entry 38, ``DivergenceKind.ORACLE_ERROR``).
    """

    field = draw(st.sampled_from(BOOL_EXISTS_FIELDS))
    value = draw(st.sampled_from(["", "true", "false", "  false  ", "t*"]))
    return f'{field}:"{value}"'


@st.composite
def _bare_json_atom(draw: st.DrawFn) -> str:
    """A registered JSON field addressed with no subpath at all
    (DIVERGENCES.md entries 14/29): demotes to an ordinary text search."""

    field = draw(st.sampled_from(JSON_FIELDS))
    text = draw(plain_word)
    return f"{field}:{text}"


@st.composite
def _json_registered_atom(draw: st.DrawFn) -> str:
    """Plain/quoted term values on a *genuinely registered* JSON subpath
    (``attrs.user`` etc, unlike ``_json_atom``'s unregistered ``notes.user``
    style names): still structurally diverges from the oracle (real whoosh
    has no JSON field type at all), same mechanism as DIVERGENCES.md entry
    14, just reached through a field that (unlike ``notes``/``custom_fields``)
    isn't also a plain-TEXT field on the oracle side.
    """

    path = draw(st.sampled_from(REGISTERED_JSON_SUBPATHS))
    quote = draw(st.sampled_from(["bare", "single", "double"]))
    text = draw(plain_word)
    if quote == "single":
        value = f"'{text}'"
    elif quote == "double":
        value = f'"{text}"'
    else:
        value = text
    return f"{path}:{value}"


@st.composite
def _json_pattern_atom(draw: st.DrawFn) -> str:
    """Wildcard/prefix/existence values on a JSON subpath
    (DIVERGENCES.md entry 30: diagnosed at parse time, other than the bare
    ``*`` existence-check special case)."""

    path = draw(st.sampled_from(REGISTERED_JSON_SUBPATHS))
    base = draw(wildcard_base)
    shape = draw(st.sampled_from(["trailing", "infix_question", "wrapped_star", "bare_star"]))
    if shape == "trailing":
        pattern = f"{base}*"
    elif shape == "infix_question":
        pattern = f"{base}?{base}"
    elif shape == "wrapped_star":
        pattern = f"*{base}*"
    else:
        pattern = "*"
    return f"{path}:{pattern}"


@st.composite
def _degenerate_wildcard_atom(draw: st.DrawFn) -> str:
    """A trailing-star pattern whose bracket character class is reversed
    (``[z-a]``) or empty (``[]``): both fold to a ``Wildcard`` on
    whoosh-compat's side and a ``Prefix`` that keeps the literal bracket text
    (not a real character class) on real whoosh's, the same
    DIVERGENCES.md entry 13 whoosh-bug the existing ``bracket_trailing``
    shape in ``_wildcard_atom`` documents, just with a degenerate class body
    and, deliberately, an unfielded (multifield-expanded) form: the existing
    entry 13 allowlist pattern required an explicit ``field:`` prefix, which
    an unfielded pattern like this one never has, and was broadened here to
    also match it (verified directly: the root cause, real whoosh's
    ``SPECIAL_CHARS``/fold-check omitting ``"["``, applies identically
    whether or not the pattern is fielded).
    """

    field = draw(st.one_of(st.none(), st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS)))
    base = draw(wildcard_base)
    cls = draw(st.sampled_from(["[z-a]", "[]"]))
    pattern = f"{base}{cls}*"
    return f"{field}:{pattern}" if field else pattern


@st.composite
def _date_only_atom(draw: st.DrawFn) -> str:
    """Time-bearing values on a ``date_only`` field (DIVERGENCES.md entries
    32 and 37): a bare/quoted single value or a bracket range, always with an
    explicit time-of-day component. ``date_only`` has no whoosh equivalent at
    all (entry 37), so every shape here structurally diverges from the
    oracle by construction, the same as ``JSON_SUBPATHS``/``REGISTERED_JSON_SUBPATHS``
    above; it exists so the generator can reach this field-kind/value-shape
    combination for crash-safety and normalize()-idempotence purposes.
    """

    field = draw(st.sampled_from(DATE_ONLY_FIELDS))
    year = draw(st.integers(min_value=1900, max_value=2100))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    shape = draw(st.sampled_from(["bare", "range"]))
    if shape == "bare":
        return f"{field}:'{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}'"
    day2 = min(day + 1, 28)
    return (
        f"{field}:[{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
        f" TO {year:04d}-{month:02d}-{day2:02d} {hour:02d}:{minute:02d}]"
    )


_every_atom = st.sampled_from(["*", "*:*"] + [f"{f}:*" for f in TEXT_FIELDS + KEYWORD_FIELDS])

# A literal empty group, so _extend's combinators below can place it (and
# nest it, and pair it with other leaves) anywhere in the tree, same as any
# other awkward leaf this module generates.
_empty_group_atom = st.just("()")


def _leaves() -> st.SearchStrategy[str]:
    strategies: list[st.SearchStrategy[str]] = [
        _term_atom(),
        _zero_token_atom(),
        _phrase_atom(),
        _wildcard_atom(),
        _numeric_range_atom(),
        _date_bare_atom(),
        _date_range_atom(),
        _bool_exists_atom(),
        _every_atom,
        _empty_group_atom,
        _degenerate_wildcard_atom(),
        _field_group_atom(),
        _comma_clause_atom(),
        _dash_negated_atom(),
        _dash_chain_atom(),
        _leading_punct_atom(),
    ]
    if NUM_FIELDS:
        strategies.append(_numeric_atom())
    if BOOL_EXISTS_FIELDS:
        strategies.append(_bool_exists_quoted_atom())
        strategies.append(_bool_exists_double_quoted_atom())
    if KEYWORD_FIELDS:
        strategies.append(_comma_atom())
    if JSON_SUBPATHS:
        strategies.append(_json_atom())
    if JSON_FIELDS:
        strategies.append(_bare_json_atom())
    if REGISTERED_JSON_SUBPATHS:
        strategies.append(_json_registered_atom())
        strategies.append(_json_pattern_atom())
    if DATE_ONLY_FIELDS:
        strategies.append(_date_only_atom())
    return st.one_of(*strategies)


# --------------------------------------------------------------------------
# Recursive composition: this is the part that actually nests things inside
# each other, the primary job this module exists to do (see module
# docstring). Every combinator wraps *already-built* subexpressions, so
# depth accumulates naturally with hypothesis's own st.recursive machinery,
# and an awkward leaf (e.g. a zero-token term) can end up at any depth, as a
# group child, as either operand of any binary operator, under NOT, or under
# a boost, purely because st.recursive draws each operand independently from
# the same growing pool.
# --------------------------------------------------------------------------

_BOOSTS = ("0.5", "1.5", "2", "2.0", "10")
_BINARY_OPS = ("AND", "OR", "ANDNOT", "ANDMAYBE", "REQUIRE")


def _extend(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    group = children.map(lambda c: f"({c})")
    negated = children.map(lambda c: f"NOT ({c})")
    # Unparenthesised NOT ("NOT tag:x"), which binds differently from the
    # parenthesised form above whenever the child is compound, and which
    # the real-world corpus (corpus_realworld.txt) is full of. Measured
    # clean over a 3000-draw sweep.
    #
    # Its "-" sibling ("-tag:x") is deliberately NOT generated, though the
    # corpus carries it and issue #13568's reporter fixed a broken query by
    # rewriting "NOT x" as "-x": measured, "-" directly abutting a value
    # defeats the entry-15 allowlist pattern's left boundary (it requires
    # start-of-string, whitespace or "(" before the value), so "-ab-cd" is
    # a genuine, currently unclaimed entry-15 divergence while "ab-cd" and
    # "NOT ab-cd" are claimed. Generating it days before a release would
    # only turn a documented gap into a CI flake; see the pre-release
    # real-world-corpus report for the escalation.
    bare_negated = children.map(lambda c: f"NOT {c}")
    boosted = st.tuples(children, st.sampled_from(_BOOSTS)).map(lambda t: f"({t[0]})^{t[1]}")
    binary = st.tuples(children, st.sampled_from(_BINARY_OPS), children).map(
        lambda t: f"({t[0]}) {t[1]} ({t[2]})"
    )
    implicit_and = st.tuples(children, children).map(lambda t: f"{t[0]} {t[1]}")
    # The same five operators with UNPARENTHESISED operands, so precedence
    # and associativity are decided by the parser instead of pre-resolved
    # here. Its parenthesised sibling above settles every regrouping
    # decision before the parser sees it, which also seals each awkward leaf
    # (zero-token term, empty group, comma clause) inside its own group so
    # it never has to survive a regrouping. Only implicit AND ("a b")
    # reached unparenthesised composition before this. Measured clean over a
    # 6000-draw AST sweep against the oracle and a 3000-example result-level
    # sweep against live whoosh and tantivy indexes.
    unparen_binary = st.tuples(children, st.sampled_from(_BINARY_OPS), children).map(
        lambda t: f"{t[0]} {t[1]} {t[2]}"
    )
    return st.one_of(group, negated, bare_negated, boosted, binary, implicit_and, unparen_binary)


def query_text(max_leaves: int = 8) -> st.SearchStrategy[str]:
    """A grammar-aware strategy over the whole supported query language,
    nested to (roughly) ``max_leaves`` structural pieces.

    Raise ``max_leaves`` for a deliberately deeper/wider local soak; the
    value committed for CI stays modest so the suite runs fast (see
    ``test_hypothesis.py``).
    """

    return st.recursive(_leaves(), _extend, max_leaves=max_leaves)


# --------------------------------------------------------------------------
# Structural-richness metrics, computed from the *parsed AST* rather than
# tracked through the generator: this way every metric reflects what the
# parser actually built (seeded @example() corpus strings get scored
# identically to generated ones), not what the generator merely intended.
# --------------------------------------------------------------------------


def _node_children(node: ast.Node) -> list[ast.Node]:
    kids: list[ast.Node] = []
    for f in dataclasses.fields(node):
        value = getattr(node, f.name)
        if isinstance(value, ast.Node):
            kids.append(value)
        elif isinstance(value, tuple):
            kids.extend(v for v in value if isinstance(v, ast.Node))
    return kids


def ast_shape(node: ast.Node) -> tuple[int, int, int]:
    """Return ``(node_count, max_depth, distinct_type_count)`` for ``node``."""

    type_names: set[str] = set()

    def walk(n: ast.Node, depth: int) -> tuple[int, int]:
        type_names.add(type(n).__name__)
        kids = _node_children(n)
        count = 1
        max_depth = depth
        for kid in kids:
            kid_count, kid_depth = walk(kid, depth + 1)
            count += kid_count
            max_depth = max(max_depth, kid_depth)
        return count, max_depth

    total, depth = walk(node, 1)
    return total, depth, len(type_names)


def zero_token_leaf_count(node: ast.Node, reg: FieldRegistry) -> int:
    """Count Term/Phrase leaves in ``node`` whose analyzer drops every token.

    Only meaningful on a *raw* (pre-normalize) tree: normalize()/analyze()
    already remove these leaves, which is exactly the behavior this metric
    exists to help the fuzzer find more of (see module docstring).
    """

    count = 0

    def analyzer_for(field: FieldRef | None) -> Callable[[str], list[str]] | None:
        resolved = reg.resolve(field) if field else None
        return resolved.spec.analyzer if resolved is not None else None

    def walk(n: ast.Node) -> None:
        nonlocal count
        if isinstance(n, ast.Term) and isinstance(n.text, str):
            analyzer = analyzer_for(n.field)
            if analyzer is not None and not analyzer(n.text):
                count += 1
        elif isinstance(n, ast.Phrase):
            analyzer = analyzer_for(n.field)
            tokens = analyzer(n.text) if analyzer is not None else n.text.split()
            if not tokens:
                count += 1
        for kid in _node_children(n):
            walk(kid)

    walk(node)
    return count


def seed_corpus(*paths: str) -> Iterable[str]:
    """Yield non-comment, non-blank lines from differential corpus files.

    Used to build ``@example()`` seeds without duplicating the corpus.
    """

    here = pathlib.Path(__file__).parent
    for name in paths:
        text = (here / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield stripped
