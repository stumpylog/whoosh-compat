"""Whoosh's relative-date unit-word abbreviations: yrs/mos/wks/hrs/mins/secs.

Real whoosh's ``English.units`` table (parser/dateparse.py) recognizes each
of these abbreviated unit words as a synonym for the corresponding full unit
(years/months/weeks/hours/minutes/seconds) inside a ``now[+-]<n><unit>``
relative offset. The differential corpus (tests/differential/corpus_paperless.txt)
carries several of these spellings as *bracketed range bounds*
(``created:[-999yrs to now]``), and those already pass the oracle comparison
there -- but nothing in this repo pinned, in one place, that each abbreviation
resolves to the *correct* offset (as opposed to, say, "wks" silently being
read as "weeks" but "mos" being misread as "minutes"), nor that the bare,
non-bracketed spelling (``added:-2yrs``, a whoosh-compat extension with no
whoosh equivalent -- see DIVERGENCES.md entry 25 / the allowlist entry citing
it) does the same. This is the direct, human-readable pin for both.
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
        pytest.param("yrs", 2, timedelta(days=365 * 2), id="years"),
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
    value = f"-{n}{unit}"
    query = f"added:'{value}'" if quoted else f"added:{value}"
    r = dparse(query, reg)
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    # "yrs" is a calendar-year offset (365-or-366-day arithmetic), not a
    # fixed 365-day delta, so it is checked separately below; every other
    # unit here is a fixed-width timedelta and can be checked directly.
    if unit != "yrs":
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
