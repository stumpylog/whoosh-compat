"""Whoosh's relative-date unit-word abbreviations: yrs/mos/wks/hrs/mins/secs.

Real whoosh's ``English.units`` table (parser/dateparse.py) recognizes each
of these abbreviated unit words as a synonym for the corresponding full unit
(years/months/weeks/hours/minutes/seconds) inside a bare or bracketed
``-<n><unit>`` relative offset, and whoosh-compat parses the *same* offset to
the *same* instant on both sides -- verified directly against the pinned
oracle for every unit below, bare and bracketed alike (see DIVERGENCES.md
entry 25's correction: an earlier claim that a bare ``-<n><unit>`` spelling
like ``added:-2yrs`` was a whoosh-compat-only extension was wrong; only the
``now±<n><unit>`` spelling is). The differential corpus
(tests/differential/corpus_paperless.txt) carries several of these
spellings as *bracketed range bounds* (``created:[-999yrs to now]``), but
those lines are allowlisted under DIVERGENCES.md entry 12 (a real,
unrelated divergence about timezone handling in range bounds) with an
*inverted* assertion (the differential harness checks the trees DIFFER,
not that they match) -- so despite superficially "passing", nothing in the
differential suite actually confirms those bracketed spellings resolve to
the *correct* offset either. Nothing in this repo pinned, in one place, that
each abbreviation resolves to the *correct* offset (as opposed to, say,
"wks" silently being read as "weeks" but "mos" being misread as "minutes").
This is the direct, human-readable pin for that, for the bare (non-bracketed)
spelling, both quoted and unquoted.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldRegistry

BASE = datetime(2026, 8, 4, 10, 30, tzinfo=UTC)


def dparse(q: str, reg: FieldRegistry) -> ast.Node:
    result = wc.parse(q, registry=reg, default_fields=["content"], tz=UTC, basedate=BASE)
    assert not result.diagnostics
    return result.ast


@pytest.mark.parametrize(
    ("unit", "n", "delta"),
    [
        pytest.param("wks", 2, timedelta(weeks=2), id="weeks"),
        pytest.param("hrs", 5, timedelta(hours=5), id="hours"),
        pytest.param("mins", 10, timedelta(minutes=10), id="minutes"),
        pytest.param("secs", 30, timedelta(seconds=30), id="seconds"),
    ],
)
@pytest.mark.parametrize("quoted", [True, False], ids=["quoted", "bare"])
def test_unit_abbreviation_resolves_to_the_correct_offset(
    reg: FieldRegistry,
    unit: str,
    n: int,
    delta: timedelta,
    quoted: bool,
) -> None:
    # "yrs" is excluded here (calendar-year, not fixed-width, arithmetic;
    # see test_years_abbreviation_resolves_two_calendar_years_back below,
    # which already covers it fully) and "mos" likewise (calendar-month
    # arithmetic; see test_months_abbreviation_resolves_three_calendar_
    # months_back below).
    value = f"-{n}{unit}"
    query = f"added:'{value}'" if quoted else f"added:{value}"
    r = dparse(query, reg)
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert BASE - r.lo == delta


@pytest.mark.parametrize("quoted", [True, False], ids=["quoted", "bare"])
def test_years_abbreviation_resolves_two_calendar_years_back(
    reg: FieldRegistry, quoted: bool
) -> None:
    query = "added:'-2yrs'" if quoted else "added:-2yrs"
    r = dparse(query, reg)
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2024, 8, 4, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize("quoted", [True, False], ids=["quoted", "bare"])
def test_months_abbreviation_resolves_three_calendar_months_back(
    reg: FieldRegistry, quoted: bool
) -> None:
    # "mos" is a calendar-month offset (28-31 day arithmetic depending on
    # which months it crosses), not a fixed-width timedelta like the other
    # units, so it is pinned separately from the parametrized case above.
    query = "added:'-3mos'" if quoted else "added:-3mos"
    r = dparse(query, reg)
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 5, 4, 10, 30, tzinfo=UTC)


def test_months_abbreviation_is_distinct_from_minutes_abbreviation(reg: FieldRegistry) -> None:
    # The one pair a typo could plausibly confuse ("mos" vs "mins"): each
    # must resolve to a distinct instant from the other, not collapse onto
    # the same offset.
    mos = dparse("added:-3mos", reg)
    mins = dparse("added:-10mins", reg)
    assert isinstance(mos, ast.DateRange)
    assert isinstance(mins, ast.DateRange)
    assert mos.lo != mins.lo


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:'-1 week'", id="quoted-space-separated"),
        pytest.param("added:'-3 days'", id="quoted-space-separated-days"),
        pytest.param("added:-2yrs", id="bare-word-abbreviation"),
    ],
)
def test_bare_relative_offset_is_a_zero_width_instant_not_a_span(
    reg: FieldRegistry, query: str
) -> None:
    # A relative offset used as a single value (not a range bound) resolves
    # to one instant, both bounds inclusive -- not a half-open span from
    # that instant to now. Quoting only protects the value from whitespace
    # tokenization; it does not turn the value into a range.
    r = dparse(query, reg)
    assert isinstance(r, ast.DateRange)
    assert r.lo == r.hi
    assert r.incl_lo
    assert r.incl_hi
