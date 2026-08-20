"""Differential parity harness: whoosh-compat vs the real whoosh (v2 schema)
oracle, over a corpus of real paperless query strings.
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.differential.allowlist import DivergenceKind
from tests.differential.allowlist import allowed_entry
from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import compat_raw_parse
from tests.differential.oracle import oracle_parse
from tests.differential.oracle import to_ast
from tests.differential.oracle import unmapped_reason
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldRegistry

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)

CORPORA = [
    l
    for f in ("corpus_paperless.txt", "corpus_docs.txt")
    for l in (pathlib.Path(__file__).parent / f).read_text(encoding="utf-8").splitlines()
    if l.strip() and not l.startswith("#")
]

# DIVERGENCES.md entry 6: any parse producing a diagnostic yields a
# structured ErrorLeaf on the whoosh-compat side vs whoosh's untyped
# error_query()/NullQuery-with-.error: these never compare structurally
# equal, by construction, for *any* invalid date/number, not just the
# specific strings the static corpus happens to exercise (see
# test_hypothesis.py, which discovers many more via fuzzing). This is a
# separate, uniform rule from the per-entry DivergenceKind taxonomy in
# allowlist.py (see that module's docstring): it is checked first, before
# consulting allowlist.py at all, since a diagnostic-bearing raw AST
# contains ErrorLeaf placeholder nodes that make a structural
# match/mismatch assertion meaningless regardless of whether the query also
# happens to match one of allowlist.py's MISMATCH patterns.
_DIAGNOSTIC_SKIP_REASON = (
    "DIVERGENCES.md entry 6: ErrorLeaf vs error_query (parse diagnostic present)"
)


@pytest.mark.parametrize("q", CORPORA)
def test_matches_oracle(q: str, oracle_reg: FieldRegistry) -> None:
    entry = allowed_entry(q)

    if entry is not None and entry[1] is DivergenceKind.ORACLE_ERROR:
        reason, _kind = entry
        raised = False
        try:
            oracle_parse(q, BASE, BERLIN)
        except Exception:  # noqa: BLE001 - the oracle's crash shape is not enumerable
            raised = True
        assert raised, (
            f"allowlist entry stale: {reason!r} declares DivergenceKind.ORACLE_ERROR"
            f" for query {q!r}, but the oracle no longer raises while parsing it;"
            " re-triage and reclassify or remove the entry per the"
            " differential-triage skill"
        )
        return

    try:
        oracle_query = oracle_parse(q, BASE, BERLIN)
    except Exception:
        if entry is None:
            raise
        reason, _kind = entry
        pytest.fail(
            f"allowlist entry stale: {reason!r} is declared DivergenceKind.MISMATCH"
            f" for query {q!r}, but the oracle now raises while parsing it (no query"
            " to compare); reclassify as DivergenceKind.ORACLE_ERROR or re-triage"
            " per the differential-triage skill"
        )

    expected = to_ast(oracle_query, oracle_reg)
    if expected is None:
        pytest.skip(unmapped_reason(oracle_query))

    raw_ast, diagnostics = compat_raw_parse(q, oracle_reg, V2_FIELDS, BERLIN, BASE)
    if diagnostics:
        pytest.skip(_DIAGNOSTIC_SKIP_REASON)

    got = normalize(analyze(raw_ast, oracle_reg))
    expected_n = normalize(expected)

    if entry is None:
        assert got == expected_n, f"query: {q!r}"
        return

    reason, _kind = entry
    assert got != expected_n, (
        f"allowlist entry stale: {reason!r} no longer diverges for query {q!r}"
        " (the trees now match structurally); fix or remove the allowlist entry"
        " and its DIVERGENCES.md paperwork per the differential-triage skill"
    )


def test_diagnostic_skip_count_matches_corpus() -> None:
    """A standalone recount (not dependent on ``test_matches_oracle``'s
    execution order) of how many corpus queries take the DIVERGENCES.md
    entry 6 diagnostic skip, pinned to a known count.

    ``pytest tests/differential -rs`` already surfaces *which* queries hit
    this skip and why, but a skip reason showing up in ``-rs`` output is
    easy to miss in isolation: a parser change that starts (or stops)
    diagnosing some corpus query shape doesn't fail anything on its own,
    since either direction is still "a skip fired with the documented
    reason". Pinning the total count here means such a change is a visible,
    named test failure instead of a silent shift in how much of the corpus
    the strict-xfail/plain-compare tests above actually exercise.
    """

    count = 0
    for q in CORPORA:
        try:
            oracle_query = oracle_parse(q, BASE, BERLIN)
        except Exception:  # noqa: BLE001, S112 - mirrors test_matches_oracle's own handling
            continue
        if to_ast(oracle_query, ORACLE_REGISTRY) is None:
            continue
        _raw_ast, diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
        if diagnostics:
            count += 1

    # Was 26; dropped to 23 by the RFC3339 "T"/"Z" datetime-separator fix
    # (paperless-ngx PR #13010 back-compat, dateparse.py's
    # ``_split_rfc3339_utc`` / "simple"'s "T"-accepting separator class):
    # three corpus queries containing a "...T...Z" bracketed date-range
    # bound used to fail to parse (BAD_DATE) on the whoosh-compat side and
    # now parse cleanly, so they no longer take this diagnostic skip.
    #
    # Dropped again from 23 to 8 by DateParserPlugin.do_date_phrases: the
    # fifteen corpus lines spelling a multi-word date keyword unquoted
    # (`created:previous month`, ...) used to diagnose BAD_DATE on the bare
    # "previous"/"this" token, and now parse as the phrase they name, so
    # they are compared against the oracle instead of skipped.
    #
    # Rose from 8 to 11 by DIVERGENCES.md entry 54 (the half-consumed date
    # value fix): the three bare unquoted RFC3339 lines
    # (`added:2026-08-04T10:30:00` and siblings) used to parse as the
    # truncated month/year window real whoosh reads them as, and now
    # diagnose BAD_DATE instead of silently searching a period nobody
    # asked for.
    assert count == 11, (
        f"{count} corpus queries now take the DIVERGENCES.md entry 6 diagnostic skip,"
        " expected 11; if this is an intentional parser change (diagnosing a new"
        " shape, or fixing one that used to diagnose), update this pinned count and"
        " say so in the commit message, don't just silently adjust the number"
    )
