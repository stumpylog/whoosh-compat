"""A date value that pairs a time of day with a whole period is rejected.

A time of day needs a day to fall on. On a whole month, year, week or
quarter it names nothing, and resolving it anyway gave a range pinned to
that time on the period's first and last day: a query that runs and answers
a question nobody asked. The rejection is about what the value means, so it
holds for every spelling of the date and of the time, either word order,
every quoting, both date field kinds, and every position a date value can
take. See DIVERGENCES.md entry 62.
"""

from datetime import UTC
from datetime import datetime

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

BASE = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: [t.lower()]),
            FieldSpec("added", FieldKind.DATETIME, fast=True),
            FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
        ]
    )


def _parse(registry: FieldRegistry, q: str) -> wc.ParseResult:
    return wc.parse(q, registry=registry, default_fields=["content"], tz=UTC, basedate=BASE)


def _message(value: str, unit: str) -> str:
    return f"{value!r} pairs a time of day with a whole {unit}; name a day, or drop the time"


def _assert_time_on_period(result: wc.ParseResult, value: str, unit: str) -> None:
    assert len(result.diagnostics) == 1, result.diagnostics
    diag = result.diagnostics[0]
    assert diag.kind is DiagnosticKind.BAD_DATE
    assert diag.raw_value == value
    assert diag.message == _message(value, unit)
    # Quoting does not repair this value, and naming a day or dropping the
    # time are different queries, so there is no single rewrite to offer.
    assert diag.suggestion is None


PERIODS = [
    pytest.param("this month", "month", id="this-month"),
    pytest.param("previous month", "month", id="previous-month"),
    pytest.param("this year", "year", id="this-year"),
    pytest.param("previous year", "year", id="previous-year"),
    pytest.param("previous week", "week", id="previous-week"),
    pytest.param("previous quarter", "quarter", id="previous-quarter"),
    pytest.param("august", "month", id="month-name"),
    pytest.param("august 2026", "month", id="month-name-and-year"),
    pytest.param("2026", "year", id="bare-year"),
]

TIMES = [
    pytest.param("3pm", id="meridiem"),
    pytest.param("3 pm", id="spaced-meridiem"),
    pytest.param("15:00", id="clock"),
    pytest.param("15:00:00", id="clock-with-seconds"),
    pytest.param("noon", id="noon"),
    pytest.param("midnight", id="midnight"),
]

ORDERS = [
    pytest.param("trailing", id="time-after"),
    pytest.param("leading", id="time-before"),
]

QUOTINGS = [
    pytest.param('"{}"', id="double-quoted"),
    pytest.param("'{}'", id="single-quoted"),
    pytest.param("{}", id="unquoted"),
]

FIELDS = [
    pytest.param("added", id="datetime"),
    pytest.param("created", id="date"),
]

# The one cell the grammar reads as something else: "august 3 pm" is a day
# number followed by a spaced meridiem, so it is August 3rd with a stray
# "pm", not a month plus a time. It is still a bad date, just not this one.
_READS_AS_A_DAY = ("august", "3 pm", "trailing")


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("quoting", QUOTINGS)
@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("time", TIMES)
@pytest.mark.parametrize(("period", "unit"), PERIODS)
def test_a_time_of_day_on_a_period_is_rejected(
    registry: FieldRegistry,
    period: str,
    unit: str,
    time: str,
    order: str,
    quoting: str,
    field: str,
) -> None:
    """Every cell of date spelling x time spelling x word order x quoting x
    field kind ends in exactly one asserted outcome. Extend the lists, never
    carve cells out of them.
    """
    value = f"{period} {time}" if order == "trailing" else f"{time} {period}"
    result = _parse(registry, f"{field}:" + quoting.format(value))

    if (period, time, order) != _READS_AS_A_DAY:
        _assert_time_on_period(result, value, unit)
        return

    assert len(result.diagnostics) == 1, result.diagnostics
    diag = result.diagnostics[0]
    assert diag.kind is DiagnosticKind.BAD_DATE
    if quoting == "{}":
        # Unquoted, the unquoted-run rule names the longest run that does
        # parse, "august 3", and suggests quoting it.
        assert diag.raw_value == "august 3"
        assert diag.suggestion == '"august 3"'
    else:
        assert diag.raw_value == value
        assert diag.message == f"{value!r} is not a recognizable date"
        assert diag.suggestion is None


@pytest.mark.parametrize(
    ("query", "value", "unit"),
    [
        pytest.param("added:[august 3pm TO now]", "august 3pm", "month", id="bracket-start-bound"),
        pytest.param(
            "added:[2025 TO august 2026 15:00]",
            "august 2026 15:00",
            "month",
            id="bracket-end-bound",
        ),
        pytest.param(
            "created:[this month 15:00 TO now]",
            "this month 15:00",
            "month",
            id="bracket-bound-on-a-date-field",
        ),
        pytest.param(
            "added:[2027 10pm TO 2027-06-01T00:00:00Z]",
            "2027 10pm",
            "year",
            id="bracket-start-bound-against-a-utc-end",
        ),
        pytest.param(
            "added:[2027-09-15T00:00:00Z TO 2027 10pm]",
            "2027 10pm",
            "year",
            id="bracket-end-bound-against-a-utc-start",
        ),
        pytest.param(
            'added:"august 3pm to now"', "august 3pm to now", "month", id="to-range-start"
        ),
        pytest.param(
            'added:"2025 to august 3pm"', "2025 to august 3pm", "month", id="to-range-end"
        ),
    ],
)
def test_a_time_on_a_period_is_rejected_in_every_position(
    registry: FieldRegistry, query: str, value: str, unit: str
) -> None:
    """A bracket bound names its own text; a quoted "to" range names the
    whole value, as any bad date in that position does.
    """
    _assert_time_on_period(_parse(registry, query), value, unit)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("previous week to now", id="span-keyword"),
        pytest.param("previous quarter to now", id="other-span-keyword"),
        pytest.param("previous week 3pm to now", id="span-keyword-with-a-time"),
    ],
)
def test_a_span_keyword_cannot_end_a_to_range(registry: FieldRegistry, value: str) -> None:
    """A week or quarter is already a span, and a "to" range cannot take a
    span as an end, with or without a time on it: the value stays a
    BAD_DATE with the generic message. A month or a year can end a "to"
    range, so a time on one of those gets the time-on-a-period message
    (test_a_time_on_a_period_is_rejected_in_every_position).
    """
    result = _parse(registry, f'added:"{value}"')
    assert len(result.diagnostics) == 1, result.diagnostics
    diag = result.diagnostics[0]
    assert diag.kind is DiagnosticKind.BAD_DATE
    assert diag.raw_value == value
    assert diag.message == f"{value!r} is not a recognizable date"


@pytest.mark.parametrize(
    ("query", "lo", "hi"),
    [
        pytest.param(
            'added:"yesterday 15:00"',
            datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 13, 15, 1, tzinfo=UTC),
            id="named-day",
        ),
        pytest.param(
            'added:"next monday 15:00"',
            datetime(2026, 9, 21, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 15, 1, tzinfo=UTC),
            id="named-weekday",
        ),
        pytest.param(
            'added:"august 10 15:00"',
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            id="month-name-then-day",
        ),
        pytest.param(
            'added:"10 august 15:00"',
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            id="day-then-month-name",
        ),
        pytest.param(
            'added:"dec 25 2019 10:30"',
            datetime(2019, 12, 25, 10, 30, tzinfo=UTC),
            datetime(2019, 12, 25, 10, 31, tzinfo=UTC),
            id="full-date-then-clock",
        ),
        pytest.param(
            'added:"2026-08-10 15:00"',
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 15, 1, tzinfo=UTC),
            id="numeric-date-then-clock",
        ),
        pytest.param(
            'added:"august 15"',
            datetime(2026, 8, 15, tzinfo=UTC),
            datetime(2026, 8, 16, tzinfo=UTC),
            id="month-name-then-day-alone",
        ),
        pytest.param(
            'added:"15:00"',
            datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 15, 1, tzinfo=UTC),
            id="bare-clock",
        ),
        pytest.param(
            'added:"this month"',
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 10, 1, tzinfo=UTC),
            id="bare-calendar-period",
        ),
        pytest.param(
            "added:previous week",
            datetime(2026, 9, 7, tzinfo=UTC),
            datetime(2026, 9, 14, tzinfo=UTC),
            id="bare-span-period",
        ),
    ],
)
def test_a_time_on_a_specific_day_still_resolves(
    registry: FieldRegistry, query: str, lo: datetime, hi: datetime
) -> None:
    result = _parse(registry, query)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.DateRange)
    assert (result.ast.lo, result.ast.hi) == (lo, hi)
    assert result.ast.incl_lo
    assert not result.ast.incl_hi
