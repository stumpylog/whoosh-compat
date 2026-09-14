"""Properties of date values that combine a date with a time of day.

The example tests pin cells; these search the space between them. A
generated value combines a date part (a keyword, a month name, a bare year,
a named day, or a numeric date written with any separator) with a time part
in either order, under every quoting and both date field kinds, and three
things must hold for all of them:

* parse() raises nothing;
* a time of day the parser accepts is the time the range starts at, and the
  range is at most an hour wide: a time on a whole period is rejected
  (DIVERGENCES.md entry 62), and a clock time is never read as a day and an
  hour (entry 63);
* a diagnostic's suggestion, when it has one, repairs its span (entry 61),
  and a time-on-a-period rejection never has one.
"""

from dataclasses import dataclass
from datetime import timedelta

from hypothesis import example
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from tests.date_messages import is_time_on_period
from tests.date_values import QUOTINGS
from tests.date_values import parse_date_query
from whoosh_compat import ast


@dataclass(frozen=True)
class Clock:
    """A time part and the clock time it names."""

    text: str
    hour: int
    minute: int


@dataclass(frozen=True)
class Case:
    query: str
    field: str
    clock: Clock


CLOCKS = (
    Clock("3pm", 15, 0),
    Clock("3 pm", 15, 0),
    Clock("15:00", 15, 0),
    Clock("15:00:00", 15, 0),
    Clock("09:30", 9, 30),
    Clock("12:30", 12, 30),
    Clock("noon", 12, 0),
    Clock("midnight", 0, 0),
    Clock("1500", 15, 0),
    Clock("15.00", 15, 0),
)

WORD_DATES = (
    "this month",
    "previous month",
    "this year",
    "previous year",
    "previous week",
    "previous quarter",
    "august",
    "august 2026",
    "2026",
    "yesterday",
    "today",
    "next monday",
    "august 10",
    "10 august 2026",
)

NUMERIC_SEPARATORS = ("-", "/", ".", " ", "T", ":", "")


@st.composite
def _value(draw: st.DrawFn) -> tuple[str, Clock]:
    clock = draw(st.sampled_from(CLOCKS))
    # A fused "1500" only means a clock time after a numeric date. Next to
    # a word date it is a four-digit year ("august 1500" is August 1500),
    # which is a correct reading, just not one this case can check.
    if draw(st.booleans()) and clock.text != "1500":
        date = draw(st.sampled_from(WORD_DATES))
        if draw(st.booleans()):
            return f"{date} {clock.text}", clock
        return f"{clock.text} {date}", clock
    first = draw(st.sampled_from(NUMERIC_SEPARATORS))
    date = f"2026{first}08"
    with_day = draw(st.booleans())
    if with_day:
        date += draw(st.sampled_from(NUMERIC_SEPARATORS)) + "10"
    last = draw(st.sampled_from(NUMERIC_SEPARATORS))
    if not with_day and first == "" and last == "":
        # "2026081500" is a complete compact date and hour (YYYYMMDDHH),
        # not a month and a clock time, so it cannot stand for this case.
        last = " "
    if not with_day and clock.text == "15.00" and " " not in first + last:
        # With no space anywhere, "2026-08-15.00" is a date and an hour
        # (15 August, hour 00), written like "2026.08.15.10", so it cannot
        # stand for this case either.
        last = " "
    return f"{date}{last}{clock.text}", clock


@st.composite
def cases(draw: st.DrawFn) -> Case:
    value, clock = draw(_value())
    field = draw(st.sampled_from(("added", "created")))
    spelled = draw(st.sampled_from(tuple(QUOTINGS.values()))).format(value)
    return Case(f"{field}:{spelled}", field, clock)


@given(case=cases())
@settings(max_examples=500, deadline=None)
def test_parse_raises_nothing(case: Case) -> None:
    # Any exception fails the test, QueryParserError included: that one
    # means a library defect, never bad input.
    parse_date_query(case.query)


@given(case=cases())
@settings(max_examples=500, deadline=None)
@example(case=Case('added:"this month 15:00"', "added", Clock("15:00", 15, 0)))
@example(case=Case('added:"2026-08 15:00"', "added", Clock("15:00", 15, 0)))
@example(case=Case("added:'2026-08 1500'", "added", Clock("1500", 15, 0)))
@example(case=Case("added:'2020 12:30'", "added", Clock("12:30", 12, 30)))
@example(case=Case("added:'2026-08 15.00'", "added", Clock("15.00", 15, 0)))
def test_an_accepted_time_of_day_is_the_time_searched(case: Case) -> None:
    result = parse_date_query(case.query)
    # Only a whole query that resolved to one date value says anything
    # about how its time was read. A date_only field floors every value to
    # whole days by design; a diagnostic means nothing was read; an And is
    # an unquoted run that kept a leftover word as a search term.
    if case.field != "added" or result.diagnostics or not isinstance(result.ast, ast.DateRange):
        return
    lo, hi = result.ast.lo, result.ast.hi
    assert lo is not None, case.query
    assert hi is not None, case.query
    assert (lo.hour, lo.minute) == (case.clock.hour, case.clock.minute), case.query
    assert hi - lo <= timedelta(hours=1), case.query


@given(case=cases())
@settings(max_examples=500, deadline=None)
def test_a_suggestion_repairs_its_span(case: Case) -> None:
    result = parse_date_query(case.query)
    for diag in result.diagnostics:
        if is_time_on_period(diag):
            assert diag.suggestion is None, case.query
        if diag.suggestion is None:
            continue
        assert diag.startchar is not None, case.query
        assert diag.endchar is not None, case.query
        fixed = case.query[: diag.startchar] + diag.suggestion + case.query[diag.endchar :]
        assert not parse_date_query(fixed).diagnostics, (case.query, fixed)
