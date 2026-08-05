"""Differential parity harness: whoosh-compat vs the real whoosh (v2 schema)
oracle, over a corpus of real paperless query strings.
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.differential.allowlist import allowed_reason
from tests.differential.oracle import (
    V2_FIELDS,
    analyze_ast,
    compat_raw_parse,
    oracle_parse,
    to_ast,
    unmapped_reason,
)
from whoosh_compat.ast import normalize

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)

CORPORA = [
    l
    for f in ("corpus_paperless.txt", "corpus_docs.txt")
    for l in (pathlib.Path(__file__).parent / f).read_text(encoding="utf-8").splitlines()
    if l.strip() and not l.startswith("#")
]


@pytest.mark.parametrize("q", CORPORA)
def test_matches_oracle(q, oracle_reg):
    oracle_query = oracle_parse(q, BASE, BERLIN)
    expected = to_ast(oracle_query, oracle_reg)
    if expected is None:
        pytest.skip(unmapped_reason(oracle_query))
    reason = allowed_reason(q)
    if reason is not None:
        pytest.skip(reason)
    raw_ast, diagnostics = compat_raw_parse(q, oracle_reg, V2_FIELDS, BERLIN, BASE)
    if diagnostics:
        # DIVERGENCES #6: any parse producing a diagnostic yields a
        # structured ErrorLeaf on the whoosh-compat side vs whoosh's
        # untyped error_query()/NullQuery-with-.error -- these never
        # compare structurally equal, by construction, for *any* invalid
        # date/number, not just the specific strings the static corpus
        # happens to exercise (see test_hypothesis.py, which discovers
        # many more via fuzzing).
        pytest.skip("DIVERGENCES #6: ErrorLeaf vs error_query (parse diagnostic present)")
    got = normalize(analyze_ast(raw_ast, oracle_reg))
    assert got == normalize(expected), f"query: {q!r}"
