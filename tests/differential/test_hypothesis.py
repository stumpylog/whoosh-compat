"""Hypothesis-fuzzed differential parity + a pure crash test."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

import whoosh_compat as wc
from tests.differential.allowlist import allowed
from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import analyze_ast
from tests.differential.oracle import compat_raw_parse
from tests.differential.oracle import oracle_parse
from tests.differential.oracle import to_ast
from whoosh_compat.ast import normalize

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)

words = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1, max_size=8)
fields = st.sampled_from(["", "title:", "tag:", "asn:", "created:", "type:", "zzz:"])
atom = st.builds(lambda f, w: f + w, fields, words)
clause = st.recursive(atom, lambda inner: st.builds(
    lambda a, op, b: f"({a}) {op} ({b})", inner,
    st.sampled_from(["AND", "OR", "ANDNOT"]), inner), max_leaves=6)
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
        # DIVERGENCES.md entry 6: see test_differential.py's identical skip -- any
        # parse producing a diagnostic (e.g. "asn:A", a bad number) yields a
        # structured ErrorLeaf vs whoosh's untyped error_query()/NullQuery.
        return
    got = normalize(analyze_ast(raw_ast, ORACLE_REGISTRY))
    assert got == normalize(expected), f"query: {q!r}"


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
