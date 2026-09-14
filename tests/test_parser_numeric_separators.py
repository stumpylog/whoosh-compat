"""Numeric date values: a colon separates clock units only, a day and an
hour written fused together are read that way only when the whole date is
fused, and once a space has separated two units the hour must follow a space
or a "T" and a dotted day cannot follow a spaced year.

Without these rules the numeric grammar accepted ":", "." and the empty
string between any two of its units, so the digits of a clock time after a
year and month filled the day and then the hour: "2026-08 15:00" and
"2026-08 15.00" read as 15 August at 00:00. See DIVERGENCES.md entry 63.
The month-plus-time reading "2026-08 15:00" gets instead is rejected on its
own terms; those cells live in test_parser_time_on_period.py.
"""

from datetime import UTC
from datetime import datetime

import pytest

import whoosh_compat as wc
from tests.date_values import DATE_FIELD_PARAMS
from tests.date_values import QUOTED_PARAMS
from tests.date_values import QUOTING_PARAMS
from tests.date_values import parse_date_query
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef


def _assert_unreadable(result: wc.ParseResult, value: str) -> None:
    assert len(result.diagnostics) == 1, result.diagnostics
    diag = result.diagnostics[0]
    assert diag.kind is DiagnosticKind.BAD_DATE
    assert diag.raw_value == value
    assert diag.message == f"{value!r} is not a recognizable date"
    assert diag.suggestion is None


@pytest.mark.parametrize("quoting", QUOTING_PARAMS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026T10:30", id="year-t-then-clock"),
        pytest.param("2026-08-10:15:00", id="colon-before-the-hour"),
        pytest.param("2026:08:10", id="colon-between-date-units"),
        pytest.param("2026-0815", id="fused-day-after-a-separated-month"),
        # The T-separated twin of "2026-08 15:00": it used to read as
        # 15 August at 00:00. The numeric year-month element needs
        # whitespace after the month, so this one is unreadable rather than
        # a month plus a time.
        pytest.param("2026-08T15:00", id="year-month-t-then-clock"),
        # A fused hour after a separated day: it used to read as 10 August
        # at 15:00, although only a fully fused date reads a fused hour.
        pytest.param("2026-08-1015", id="fused-hour-after-a-separated-day"),
        # A colon between a year and a month: it used to read as all of
        # August.
        pytest.param("2026:08", id="colon-before-the-month"),
    ],
)
@pytest.mark.parametrize("field", DATE_FIELD_PARAMS)
def test_a_misread_single_token_value_is_a_bad_date(field: str, value: str, quoting: str) -> None:
    _assert_unreadable(parse_date_query(f"{field}:" + quoting.format(value)), value)


@pytest.mark.parametrize("quoting", QUOTED_PARAMS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-08 1500", id="fused-clock-after-a-year-month"),
        pytest.param("2026 1230", id="fused-month-and-day-after-a-year"),
        pytest.param("2026-08-10 15:00:00:123456", id="colon-before-the-microsecond"),
        # A fused day and hour after a spaced year: it used to read as
        # 15 August, a clock time "08:15" read as a month and a day.
        pytest.param("2026 0815", id="fused-month-and-day-after-a-spaced-year"),
        # A dotted clock time after a spaced date: each used to read as a day
        # and an hour (15 August at 00:00) or as a month and a day (30
        # December, 15 August), the dot twins of "2026-08 15:00" and
        # "2026 12:30".
        pytest.param("2026-08 15.00", id="dotted-clock-after-a-year-month"),
        pytest.param("2026 08 15.00", id="dotted-clock-after-a-spaced-year-month"),
        pytest.param("2026/08 15.00", id="dotted-clock-after-a-slashed-year-month"),
        pytest.param("2026.08 15.00", id="dotted-clock-after-a-dotted-year-month"),
        pytest.param("2026 12.30", id="dotted-month-and-day-after-a-spaced-year"),
        pytest.param("2026 08.15", id="dotted-clock-after-a-spaced-year"),
        # Once a space has separated two units, the hour must follow a space
        # or a "T", and a dotted day may not follow a spaced year, so these
        # mixed spellings are unreadable too; each used to read as 10 August
        # at 15:00.
        pytest.param("2026-08 10-15", id="dash-before-the-hour-after-a-spaced-day"),
        pytest.param("2026-08 10.15:00", id="dot-before-the-hour-after-a-spaced-day"),
        pytest.param("2026 08.10 15:00", id="dotted-day-after-a-spaced-year"),
    ],
)
def test_a_misread_spaced_value_is_a_bad_date_when_quoted(value: str, quoting: str) -> None:
    _assert_unreadable(parse_date_query("added:" + quoting.format(value)), value)


@pytest.mark.parametrize(
    ("query", "lo", "hi", "term"),
    [
        pytest.param(
            "added:2026-08 1500",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            "1500",
            id="fused-clock-after-a-year-month",
        ),
        pytest.param(
            "added:2026 1230",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "1230",
            id="fused-month-and-day-after-a-year",
        ),
        pytest.param(
            "added:2026-08-10 15:00:00:123456",
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
            "15:00:00:123456",
            id="colon-before-the-microsecond",
        ),
        pytest.param(
            "added:2026-08 15.00",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            "15.00",
            id="dotted-clock-after-a-year-month",
        ),
        pytest.param(
            "added:2026 12.30",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "12.30",
            id="dotted-month-and-day-after-a-year",
        ),
    ],
)
def test_an_unquoted_unreadable_remainder_stays_a_term(
    query: str, lo: datetime, hi: datetime, term: str
) -> None:
    """Unquoted, no longer run parses in full, so the unquoted-run rule
    declines and the date keeps its first word, the remainder a search term,
    as any other non-date word would (DIVERGENCES.md entries 61 and 63).
    """
    result = parse_date_query(query)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.And)
    date, leftover = result.ast.children
    assert isinstance(date, ast.DateRange)
    assert (date.lo, date.hi) == (lo, hi)
    assert leftover == ast.Term(field=FieldRef("content"), text=term)


@pytest.mark.parametrize(
    ("query", "value"),
    [
        pytest.param("added:['2026-08 15.00' TO now]", "2026-08 15.00", id="lower-bound"),
        pytest.param("added:[now TO '2026 12.30']", "2026 12.30", id="upper-bound"),
    ],
)
def test_a_dotted_clock_after_a_spaced_date_is_a_bad_date_as_a_bound(
    query: str, value: str
) -> None:
    _assert_unreadable(parse_date_query(query), value)


@pytest.mark.parametrize(
    ("query", "lo", "hi", "incl_hi"),
    [
        pytest.param(
            "added:'2026-08-10 1500'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="separated-date-then-fused-clock",
        ),
        pytest.param(
            "added:'2026-08-10T1500'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="t-then-fused-clock",
        ),
        pytest.param(
            "added:2026-08-10T1500",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="t-then-fused-clock-unquoted",
        ),
        pytest.param(
            "added:'20260810 1500'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="fused-date-then-fused-clock",
        ),
        pytest.param(
            "added:202608101500",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="fully-fused",
        ),
        pytest.param(
            "added:'2026-08-10 15:00:00.123456'",
            datetime(2026, 8, 10, 15, 0, 0, 123456, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 0, 0, 123456, tzinfo=UTC),
            True,
            id="clock-with-microseconds",
        ),
        pytest.param(
            "added:'2026-08-10T15:00:00'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 0, 1, tzinfo=UTC),
            False,
            id="t-then-clock-with-seconds",
        ),
        pytest.param(
            "added:'2026-08 15'",
            datetime(2026, 8, 15, tzinfo=UTC),
            datetime(2026, 8, 16, tzinfo=UTC),
            False,
            id="year-month-then-spaced-day",
        ),
        pytest.param(
            "added:2026-08-10",
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
            False,
            id="separated-date",
        ),
        pytest.param(
            "added:2026-08",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            False,
            id="separated-year-month",
        ),
        pytest.param(
            "added:'2026-08-10 15.00'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="separated-date-then-dotted-clock",
        ),
        pytest.param(
            "added:'2026.08.15 10.30'",
            datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
            datetime(2026, 8, 15, 10, 31, tzinfo=UTC),
            False,
            id="dotted-date-then-dotted-clock",
        ),
        pytest.param(
            "added:'2026 08 10 15.00'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="spaced-date-then-dotted-clock",
        ),
        # The named-date grammar's year-month also reads "2026 08", but only
        # on text the numeric grammar declines, so these spaced values keep
        # the numeric reading.
        pytest.param(
            "added:'2026 08'",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            False,
            id="spaced-year-month",
        ),
        pytest.param(
            "added:'2026 08 10'",
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
            False,
            id="spaced-date",
        ),
        pytest.param(
            "added:'2026 08 10 15:00'",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            False,
            id="spaced-date-then-clock",
        ),
        pytest.param(
            "added:['2026 08' TO now]",
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
            True,
            id="spaced-year-month-as-a-bound",
        ),
        pytest.param(
            "added:2026-08-10-15",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 16, 0, tzinfo=UTC),
            False,
            id="dash-before-the-hour",
        ),
        pytest.param(
            "added:'2026.08.15.10.30'",
            datetime(2026, 8, 15, 10, 30, tzinfo=UTC),
            datetime(2026, 8, 15, 10, 31, tzinfo=UTC),
            False,
            id="dot-before-every-unit",
        ),
        pytest.param(
            "added:['2026-08-10 15.00' TO now]",
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
            True,
            id="separated-date-then-dotted-clock-as-a-bound",
        ),
        pytest.param(
            "created:'2026-08-10 1500'",
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
            False,
            id="date-field-floors-to-the-day",
        ),
    ],
)
def test_a_well_formed_numeric_value_still_resolves(
    query: str, lo: datetime, hi: datetime, incl_hi: bool
) -> None:
    result = parse_date_query(query)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.DateRange)
    assert (result.ast.lo, result.ast.hi, result.ast.incl_hi) == (lo, hi, incl_hi)
