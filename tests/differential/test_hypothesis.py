"""Hypothesis-fuzzed differential parity + a pure crash test."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import tantivy
from hypothesis import example
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis import target

import whoosh_compat as wc
from tests.differential.allowlist import allowed
from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import compat_raw_parse
from tests.differential.oracle import oracle_parse
from tests.differential.oracle import to_ast
from tests.differential.strategies import ast_shape
from tests.differential.strategies import query_text
from tests.differential.strategies import seed_corpus
from tests.differential.strategies import zero_token_leaf_count
from whoosh_compat import ast
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)

words = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1, max_size=8
)
# A separate "dash inside a word" strategy so hyphenated/dashed-ISO-date-like
# atoms (e.g. "created:2020-01-01") are exercised by the parity fuzzer too
# (the plain `words` alphabet above has no way to generate a "-" at all).
# The dash is deliberately confined to *between* two non-empty runs of
# `words`-alphabet characters, never leading: a leading "-" is a dangling
# NOT-operator-prefix concern (a different, already-covered grammar area),
# not something this date-grammar-focused addition is meant to fuzz, and
# generating it here would just produce noisy, unrelated divergences.
dashed_word = st.builds(lambda a, b: f"{a}-{b}", words, words)
fields = st.sampled_from(["", "title:", "tag:", "asn:", "created:", "type:", "zzz:"])
atom = st.builds(lambda f, w: f + w, fields, st.one_of(words, dashed_word))
clause = st.recursive(
    atom,
    lambda inner: st.builds(
        lambda a, op, b: f"({a}) {op} ({b})", inner, st.sampled_from(["AND", "OR", "ANDNOT"]), inner
    ),
    max_leaves=6,
)
query = st.builds(lambda parts: " ".join(parts), st.lists(clause, min_size=1, max_size=4))


@given(query)
@settings(max_examples=300, deadline=None)
@example("created:0001")
def test_fuzz_matches_oracle(q: str) -> None:
    if allowed(q):
        return
    try:
        oracle_query = oracle_parse(q, BASE, BERLIN)
    except Exception:  # noqa: BLE001 - the oracle's own failure modes are not enumerable
        # Same skip as _grammar_fuzz_matches_oracle below (see its comment
        # for the full rationale): the oracle makes no never-raises promise,
        # and this strategy can reach shapes that crash it, e.g.
        # "created:0001" (the fields sample includes "created:" and the
        # words alphabet includes decimal digits), whose year-1 date
        # underflows datetime.min in LocalDateParser's tz-reversal
        # arithmetic. The @example above pins that shape deterministically.
        # whoosh-compat itself handles these without raising; there is
        # simply no oracle result to compare against.
        return
    expected = to_ast(oracle_query, ORACLE_REGISTRY)
    if expected is None:
        return
    raw_ast, diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    if diagnostics:
        # DIVERGENCES.md entry 6: see test_differential.py's identical skip: any
        # parse producing a diagnostic (e.g. "asn:A", a bad number) yields a
        # structured ErrorLeaf vs whoosh's untyped error_query()/NullQuery.
        return
    got = normalize(analyze(raw_ast, ORACLE_REGISTRY))
    assert got == normalize(expected), f"query: {q!r}"


# --------------------------------------------------------------------------
# Grammar-aware parity: the full supported query language (strategies.py),
# nested rather than flat, with `hypothesis.target()` guiding the search
# toward structurally rich examples (see strategies.py's module docstring
# for why nesting/composition is specifically the gap this closes) and
# seeded with the corpus/DIVERGENCES examples known to be interesting.
# --------------------------------------------------------------------------

# Seeds: every static differential corpus line (known-interesting query
# shapes already curated by hand) plus a few strings pulled directly from
# DIVERGENCES.md entries that aren't already corpus lines, so those
# documented divergence shapes are always exercised by this property too,
# not just left to chance generation.
_DIVERGENCE_SEEDS = (
    "created:[2020-06-15 TO 2020-06-20]",  # entry 12: partial-bound collapse
    "title:202[0-3]*",  # entry 13: bracket-class trailing-star fold
    "notes.user:alice",  # entry 14: JSON subpath, no whoosh analogue
    "tag:'foo,bar'",  # entry 17: comma-quote-literal
    "title:*",  # entry 20: bare field:* -> Every(field)
    "added:'2020 12:30'",  # entry 21: year + colon-time ambiguity
    "NOT title:the",  # entry 23: NOT of a zero-token term
    # entry 23 through empty-group and nested-NOT noise (deep-fuzz find).
    "0 NOT (NOT (() title:the))",
)

# Explicit, seeded examples of the generator vocabulary a full-library review
# found unreachable (see strategies.py's new atoms): each shape below is
# proven reachable by construction rather than left to chance draws, per the
# acceptance criteria for widening this fuzzer's vocabulary.
_NEW_VOCABULARY_SEEDS = (
    # Empty groups: bare, nested, repeated, and mixed with a real clause.
    "()",
    "(())",
    "() AND ()",
    "title:foo AND ()",
    "NOT ()",
    # Bare JSON field name (no subpath): demotes to a text search.
    "attrs:foo",
    # Quoted values on non-TEXT fields.
    'asn:"100"',
    "asn:'100'",
    "notes.user:'alice'",
    'notes.user:"alice"',
    # Negative and out-of-u64-range numerics, bare and quoted.
    "asn:-5",
    "asn:18446744073709551616",
    'asn:"-5"',
    'asn:"18446744073709551616"',
    "asn:0",
    "asn:18446744073709551615",
    "asn:4294967295",  # real whoosh's unsigned 32-bit NUMERIC maximum: matches
    "asn:4294967296",  # entry 39: minimal reproduction, mismatches
    "id:2147483647",  # real whoosh's signed 32-bit NUMERIC maximum: matches
    "id:2147483648",  # entry 39: minimal reproduction (signed field), mismatches
    "has_correspondent:'  false'",  # entry 33, broadened past has_tag
    # Reversed and degenerate character classes.
    "x[z-a]*",
    "title:x[z-a]*",
    "title:[]*",
    # JSON subpath pattern and existence shapes, on a genuinely registered
    # JSON field (attrs, unlike the unregistered notes/custom_fields names).
    "attrs.user:pre*",
    "attrs.user:w?ld",
    "attrs.user:*lice*",
    "attrs.user:*",
    # Quoted values on BOOLEAN_EXISTS fields, single- and double-quoted.
    "has_tag:''",
    "has_tag:'  false  '",
    "has_tag:'F '",
    "has_tag:t*",
    'has_tag:""',
    'has_tag:"true"',
    'has_tag:"  false  "',
    'has_tag:"t*"',
    # Time-bearing values on a date-only field.
    "release_date:'2020-03-15 15:30'",
    "release_date:[2020-03-15 15:30 TO 2020-03-16 15:30]",
)
_SEED_QUERIES = tuple(
    dict.fromkeys(
        (
            *seed_corpus("corpus_paperless.txt", "corpus_docs.txt"),
            *_DIVERGENCE_SEEDS,
            *_NEW_VOCABULARY_SEEDS,
        )
    )
)


def _grammar_fuzz_matches_oracle(q: str) -> None:
    raw_ast, diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    count, depth, distinct_types = ast_shape(raw_ast)
    dropped = zero_token_leaf_count(raw_ast, ORACLE_REGISTRY)
    # Hill-climb toward structurally rich examples: more nodes, deeper
    # nesting, more distinct node types in one tree, and more zero-token
    # leaves buried somewhere inside a larger structure (exactly the
    # composition gap described in strategies.py's module docstring: every
    # real defect found so far involved a dropped/zero-token child nested
    # inside something else, not a zero-token value tested alone).
    target(float(count), label="node_count")
    target(float(depth), label="depth")
    target(float(distinct_types), label="distinct_types")
    target(float(dropped), label="dropped_zero_token")

    if allowed(q):
        return
    try:
        oracle_query = oracle_parse(q, BASE, BERLIN)
    except Exception:  # noqa: BLE001 - the oracle's own failure modes are not enumerable
        # The oracle (real whoosh, plus this harness's LocalDateParser clone
        # of paperless-ngx's own tz-reversal override, see oracle.py) makes
        # no promise of never raising for malformed input the way
        # whoosh-compat's diagnostics-not-exceptions contract does (see
        # ARCHITECTURE.md's "Diagnostics never raise mid-parse" invariant):
        # an empty range bound with an exclusive bracket
        # ("created:{ TO 1]") crashes whoosh's own field.parse_range, and a
        # year at the extreme edge of what datetime can represent
        # ("created:0001") overflows the harness's own UTC tz-reversal
        # arithmetic. Neither is a whoosh-compat defect (whoosh-compat
        # handles both without raising, producing either an ErrorLeaf/
        # diagnostic or a valid DateRange); there is simply no oracle
        # result to compare against for shapes the oracle itself cannot
        # parse, so skip rather than fail.
        return
    expected = to_ast(oracle_query, ORACLE_REGISTRY)
    if expected is None:
        return
    if diagnostics:
        # DIVERGENCES.md entry 6, see test_fuzz_matches_oracle above.
        return
    got = normalize(analyze(raw_ast, ORACLE_REGISTRY))
    assert got == normalize(expected), f"query: {q!r}"


test_fuzz_grammar_matches_oracle = given(query_text(max_leaves=6))(
    settings(max_examples=300, deadline=None)(_grammar_fuzz_matches_oracle)
)
for _q in _SEED_QUERIES:
    test_fuzz_grammar_matches_oracle = example(_q)(test_fuzz_grammar_matches_oracle)
del _q


# --------------------------------------------------------------------------
# normalize() resilience: totality (never raises for any parsed AST) and
# idempotence (a second pass is always a no-op). Grammar-aware so it reaches
# every node type the language can produce, not just the ones the simpler
# `words`-based strategy above happens to build.
# --------------------------------------------------------------------------


@given(query_text(max_leaves=6))
@settings(max_examples=300, deadline=None)
def test_normalize_is_total_and_idempotent(q: str) -> None:
    # compat_raw_parse (not the public wc.parse()) so this exercises
    # normalize() the way callers actually invoke it: on a freshly parsed,
    # not-yet-normalized tree, not one that has already been through it once.
    raw_ast, _diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    once = normalize(raw_ast)
    twice = normalize(once)
    assert once == twice, f"normalize() not idempotent for query: {q!r}"


@given(query_text(max_leaves=6))
@settings(max_examples=300, deadline=None)
def test_analyze_is_normalization_insensitive(q: str) -> None:
    # analyze()'s documented contract: a not-yet-normalized tree analyzes
    # to the same result as its normalized form (the docstring's "a
    # not-yet-normalized tree still analyzes correctly"). This pins the
    # whole property, not just the group-wrapped-Nothing shape covered by
    # the direct unit tests in tests/test_analyze.py.
    raw_ast, _diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    assert analyze(raw_ast, ORACLE_REGISTRY) == analyze(normalize(raw_ast), ORACLE_REGISTRY), (
        f"query: {q!r}"
    )


@given(st.text(max_size=80))
@settings(max_examples=300, deadline=None)
def test_parse_never_raises(q: str) -> None:
    wc.parse(
        q,
        registry=ORACLE_REGISTRY,
        default_fields=V2_FIELDS,
        tz=BERLIN,
        basedate=BASE,
    )


# A wilder crash-only input space: full unicode chunks interleaved with a
# booster of the grammar's own metacharacters and field prefixes, so the
# sampler spends time in syntactically interesting territory (unclosed
# groups, dangling operators, half-formed ranges) instead of plain noise.
# Crash-only means no oracle comparison, so broadening the alphabet cannot
# produce divergence false positives: any raise is a real bug.
_meta = st.sampled_from(
    [
        *"()[]{}\"'*?^~:,-+ ",
        "AND",
        "OR",
        "NOT",
        "TO",
        "title:",
        "created:",
        "tag:",
        "notes.user:",
        # Years at the edges of what datetime can represent: their arithmetic
        # (a year-0 floor, a year-9999 exclusive ceiling) is a place parsing has
        # failed before, and the alphabet above cannot reach them on its own.
        "0000",
        "9999",
    ]
)
_wild = st.lists(st.one_of(st.text(max_size=12), _meta), min_size=0, max_size=40).map("".join)


@given(_wild)
@settings(max_examples=300, deadline=None)
def test_parse_never_raises_wild(q: str) -> None:
    # The contract under test: bad input NEVER raises; it surfaces on the
    # ParseResult's diagnostics channel instead.
    result = wc.parse(
        q,
        registry=ORACLE_REGISTRY,
        default_fields=V2_FIELDS,
        tz=BERLIN,
        basedate=BASE,
    )
    assert result.ast is not None or result.diagnostics


# --------------------------------------------------------------------------
# Nesting-depth-and-cost-budget property: a full-library review found that
# the dominant shape that defeats ast.normalize()'s flattening (and that
# emit()'s own iterative rewrite, DIVERGENCES.md's normalize()/emit()
# performance notes, was written to fix) is *alternating* And/Or nesting,
# not a single repeated operator (which normalize() flattens into one wide
# node trivially). This property builds exactly that shape to a controlled
# depth and asserts both a crash-safety property (no bare exception escapes
# parse(), at every depth, including past the parser's own
# _MAX_GROUP_NESTING_DEPTH=200 cap, parser/plugins.py) and a cost-budget
# property (parse-plus-emit stays within a loose, non-flaky time ceiling
# that an exponential-blowup regression would still fail by a wide margin).
# --------------------------------------------------------------------------


def _alternating_query(depth: int) -> str:
    """Build a query string nested ``depth`` levels deep, alternating AND/OR
    at each level: ``(((title:leaf0) AND (tag:leaf1)) OR (tag:leaf2)) AND ...``.

    Deliberately alternating rather than a single repeated operator:
    ``ast.normalize()`` flattens a run of the *same* associative operator
    into one wide node in a single pass, which hides an exponential-blowup
    regression; alternating operators defeat that flattening the same way
    the review that prompted this property described.
    """

    parts = [f"title:leaf{i % 7}" if i % 2 == 0 else f"tag:leaf{i % 7}" for i in range(depth + 1)]
    expr = parts[0]
    for i, part in enumerate(parts[1:]):
        op = "AND" if i % 2 == 0 else "OR"
        expr = f"({expr}) {op} ({part})"
    return expr


# The parser's own nesting-depth cap (parser/plugins.py's
# _MAX_GROUP_NESTING_DEPTH); depths are drawn on both sides of it so this
# property demonstrably exercises the cap itself, not just depths safely
# under it "by luck".
_MAX_GROUP_NESTING_DEPTH = 200


@given(st.integers(min_value=1, max_value=60))
@settings(max_examples=30, deadline=None)
@example(depth=1)
@example(depth=_MAX_GROUP_NESTING_DEPTH - 1)  # just under the cap
@example(depth=_MAX_GROUP_NESTING_DEPTH)  # exactly at the cap
@example(depth=_MAX_GROUP_NESTING_DEPTH + 50)  # comfortably past the cap
def test_alternating_nesting_depth_cost_budget(depth: int) -> None:
    q = _alternating_query(depth)

    start = time.perf_counter()
    result = wc.parse(
        q, registry=ORACLE_REGISTRY, default_fields=V2_FIELDS, tz=BERLIN, basedate=BASE
    )
    elapsed = time.perf_counter() - start

    # Crash-safety: parse() never raises, at any depth, including well past
    # the nesting cap; past the cap the result must carry a diagnostic
    # (DIVERGENCES.md entry 31) rather than a bare success with a silently
    # truncated tree or (the regression this guards against) an unbounded
    # RecursionError/exponential hang.
    assert result.ast is not None
    if depth > _MAX_GROUP_NESTING_DEPTH:
        assert result.diagnostics, (
            f"depth {depth} exceeds the {_MAX_GROUP_NESTING_DEPTH}-level nesting cap"
            " but produced no diagnostic"
        )

    # Cost budget: loose enough not to flake on a slow machine (an ordinary
    # depth-60 alternating parse takes low milliseconds), tight enough that
    # an exponential blowup in normalize()'s flattening or emit()'s subtree
    # handling would blow through it by orders of magnitude rather than
    # merely brushing it.
    assert elapsed < 5.0, f"parse() took {elapsed:.2f}s at depth {depth}, budget is 5.0s"

    if not result.diagnostics:
        emit_start = time.perf_counter()
        _emit_alternating(result.ast)
        emit_elapsed = time.perf_counter() - emit_start
        assert emit_elapsed < 5.0, (
            f"emit() took {emit_elapsed:.2f}s at depth {depth}, budget is 5.0s"
        )


def _emit_alternating(node: ast.Node) -> tantivy.Query:
    return emit_(node, index=_COST_BUDGET_INDEX, registry=_COST_BUDGET_REGISTRY)


def _build_cost_budget_index() -> tantivy.Index:
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("title", stored=True)
    sb.add_text_field("tag", stored=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    doc = tantivy.Document()
    doc.add_text("title", "leaf0 leaf1 leaf2 leaf3 leaf4 leaf5 leaf6")
    doc.add_text("tag", "leaf0 leaf1 leaf2 leaf3 leaf4 leaf5 leaf6")
    w.add_document(doc)
    w.commit()
    index.reload()
    return index


_COST_BUDGET_INDEX = _build_cost_budget_index()
_COST_BUDGET_REGISTRY = FieldRegistry(
    [
        FieldSpec("title", FieldKind.TEXT, analyzer=lambda t: t.split()),
        FieldSpec("tag", FieldKind.TEXT, analyzer=lambda t: t.split()),
    ]
)
