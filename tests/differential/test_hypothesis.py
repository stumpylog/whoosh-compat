"""Hypothesis-fuzzed differential parity + a pure crash test."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hypothesis import example
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis import target

import whoosh_compat as wc
from tests.differential.allowlist import allowed
from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import analyze_ast
from tests.differential.oracle import compat_raw_parse
from tests.differential.oracle import oracle_parse
from tests.differential.oracle import to_ast
from tests.differential.strategies import ast_shape
from tests.differential.strategies import query_text
from tests.differential.strategies import seed_corpus
from tests.differential.strategies import zero_token_leaf_count
from whoosh_compat.ast import normalize

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
def test_fuzz_matches_oracle(q):
    if allowed(q):
        return
    expected = to_ast(oracle_parse(q, BASE, BERLIN), ORACLE_REGISTRY)
    if expected is None:
        return
    raw_ast, diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    if diagnostics:
        # DIVERGENCES.md entry 6: see test_differential.py's identical skip: any
        # parse producing a diagnostic (e.g. "asn:A", a bad number) yields a
        # structured ErrorLeaf vs whoosh's untyped error_query()/NullQuery.
        return
    got = normalize(analyze_ast(raw_ast, ORACLE_REGISTRY))
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
)
_SEED_QUERIES = tuple(
    dict.fromkeys((*seed_corpus("corpus_paperless.txt", "corpus_docs.txt"), *_DIVERGENCE_SEEDS))
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
    got = normalize(analyze_ast(raw_ast, ORACLE_REGISTRY))
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
def test_normalize_is_total_and_idempotent(q):
    # compat_raw_parse (not the public wc.parse()) so this exercises
    # normalize() the way callers actually invoke it: on a freshly parsed,
    # not-yet-normalized tree, not one that has already been through it once.
    raw_ast, _diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    once = normalize(raw_ast)
    twice = normalize(once)
    assert once == twice, f"normalize() not idempotent for query: {q!r}"


@given(st.text(max_size=80))
@settings(max_examples=300, deadline=None)
def test_parse_never_raises(q):
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
def test_parse_never_raises_wild(q):
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
