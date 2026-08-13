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
    field = draw(st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS))
    n = draw(st.integers(min_value=1, max_value=4))
    all_zero = draw(st.booleans())
    if all_zero:
        parts = draw(st.lists(st.sampled_from(ZERO_TOKEN_WORDS), min_size=n, max_size=n))
    else:
        parts = draw(st.lists(phrase_word, min_size=n, max_size=n))
    text = " ".join(parts)
    slop = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=3)))
    slop_s = f"~{slop}" if slop is not None else ""
    return f'{field}:"{text}"{slop_s}'


@st.composite
def _wildcard_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(TEXT_FIELDS + KEYWORD_FIELDS))
    base = draw(wildcard_base)
    shape = draw(
        st.sampled_from(
            ["trailing", "leading", "infix", "bracket_trailing", "bracket_infix", "question"]
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
def _json_atom(draw: st.DrawFn) -> str:
    path = draw(st.sampled_from(JSON_SUBPATHS))
    value = draw(plain_word)
    return f"{path}:{value}"


@st.composite
def _bool_exists_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(BOOL_EXISTS_FIELDS))
    value = draw(st.sampled_from(["true", "false"]))
    return f"{field}:{value}"


_every_atom = st.sampled_from(["*", "*:*"] + [f"{f}:*" for f in TEXT_FIELDS + KEYWORD_FIELDS])

# issue #10: a literal empty group, so _extend's combinators below can place
# it (and nest it, and pair it with other leaves) anywhere in the tree, same
# as any other awkward leaf this module generates.
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
    ]
    if KEYWORD_FIELDS:
        strategies.append(_comma_atom())
    if JSON_SUBPATHS:
        strategies.append(_json_atom())
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
    boosted = st.tuples(children, st.sampled_from(_BOOSTS)).map(lambda t: f"({t[0]})^{t[1]}")
    binary = st.tuples(children, st.sampled_from(_BINARY_OPS), children).map(
        lambda t: f"({t[0]}) {t[1]} ({t[2]})"
    )
    implicit_and = st.tuples(children, children).map(lambda t: f"{t[0]} {t[1]}")
    return st.one_of(group, negated, boosted, binary, implicit_and)


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

    Only meaningful on a *raw* (pre-normalize) tree: normalize()/analyze_ast()
    already remove these leaves, which is exactly the behavior this metric
    exists to help the fuzzer find more of (see module docstring).
    """

    count = 0

    def analyzer_for(field: FieldRef | None) -> Callable[[str], list[str]] | None:
        spec = reg.resolve(field) if field else None
        return spec.analyzer if spec is not None else None

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
