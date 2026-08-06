from datetime import datetime

import pytest

from whoosh_compat.parser.times import TimeError
from whoosh_compat.parser.times import adatetime
from whoosh_compat.parser.times import ceil
from whoosh_compat.parser.times import fill_in
from whoosh_compat.parser.times import fix
from whoosh_compat.parser.times import floor
from whoosh_compat.parser.times import has_no_date
from whoosh_compat.parser.times import has_no_time
from whoosh_compat.parser.times import is_ambiguous
from whoosh_compat.parser.times import is_void
from whoosh_compat.parser.times import relative_days
from whoosh_compat.parser.times import timespan


def test_adatetime_floor_ceil():
    at = adatetime(year=2020, month=3)
    assert at.floor() == datetime(2020, 3, 1, 0, 0, 0, 0)
    assert at.ceil() == datetime(2020, 3, 31, 23, 59, 59, 999999)


def test_timespan_disambiguate():
    ts = timespan(adatetime(year=2020), adatetime(year=2021))
    ts = ts.disambiguated(datetime(2026, 8, 4))
    assert ts.start == datetime(2020, 1, 1, 0, 0, 0, 0)


@pytest.mark.parametrize("current_wday, wday, dir, expected", [
    pytest.param(6, 1, 1, 2, id="next-weekday-forward"),
    pytest.param(1, 6, -1, -2, id="last-weekday-backward"),
    pytest.param(2, 2, 1, 7, id="same-weekday-next-wraps-a-week"),
    pytest.param(2, 2, -1, -7, id="same-weekday-last-wraps-a-week"),
])
def test_relative_days(current_wday, wday, dir, expected):
    assert relative_days(current_wday, wday, dir) == expected


@pytest.mark.parametrize("kwargs, message", [
    pytest.param({"month": 0}, "month must be in 1..12", id="month-too-low"),
    pytest.param({"month": 13}, "month must be in 1..12", id="month-too-high"),
    pytest.param({"day": 0}, "day must be greater than 1", id="day-too-low"),
    pytest.param({"year": 2021, "month": 2, "day": 29}, "day is out of range for month",
                 id="day-out-of-range-for-month"),
    pytest.param({"hour": 24}, "hour must be in 0..23", id="hour-too-high"),
    pytest.param({"hour": -1}, "hour must be in 0..23", id="hour-too-low"),
    pytest.param({"minute": 60}, "minute must be in 0..59", id="minute-too-high"),
    pytest.param({"second": 60}, "second must be in 0..59", id="second-too-high"),
    pytest.param({"microsecond": 1_000_000}, "microsecond must be in 0..999999",
                 id="microsecond-too-high"),
    pytest.param({"microsecond": -1}, "microsecond must be in 0..999999",
                 id="microsecond-too-low"),
])
def test_adatetime_invalid_components_raise(kwargs, message):
    with pytest.raises(TimeError, match=message):
        adatetime(**kwargs)


def test_adatetime_eq_against_datetime():
    fully_specified = adatetime(2020, 3, 4, 5, 6, 7, 8)
    assert fully_specified == datetime(2020, 3, 4, 5, 6, 7, 8)
    assert adatetime(year=2020) != datetime(2020, 3, 4, 5, 6, 7, 8)
    assert adatetime(year=2020) != "not a date"


def test_adatetime_repr_and_tuple():
    at = adatetime(year=2020, month=3, day=4)
    assert at.tuple() == (2020, 3, 4, None, None, None, None)
    assert repr(at) == "adatetime(2020, 3, 4, None, None, None, None)"


def test_adatetime_date():
    at = adatetime(year=2020, month=3, day=4)
    assert at.date() == datetime(2020, 3, 4).date()


def test_adatetime_copy_is_independent():
    at = adatetime(year=2020, month=3)
    copied = at.copy()
    copied.month = 9
    assert at.month == 3
    assert copied.month == 9


def test_adatetime_replace():
    at = adatetime(year=2009, month=10, day=31)
    replaced = at.replace(year=2010)
    assert replaced.tuple() == (2010, 10, 31, None, None, None, None)
    assert at.year == 2009  # original untouched


def test_adatetime_replace_unknown_key_raises():
    at = adatetime(year=2009)
    with pytest.raises(KeyError):
        at.replace(century=20)


def test_adatetime_eq_same_class():
    assert adatetime(year=2020, month=3) == adatetime(year=2020, month=3)
    assert adatetime(year=2020, month=3) != adatetime(year=2020, month=4)


def test_adatetime_floor_ceil_all_fields_specified():
    # Exercises the branches where each unit is already non-None (no
    # default substitution needed) for both floor() and ceil().
    at = adatetime(2020, 3, 4, 5, 6, 7, 8)
    assert at.floor() == datetime(2020, 3, 4, 5, 6, 7, 8)
    assert at.ceil() == datetime(2020, 3, 4, 5, 6, 7, 8)


def test_adatetime_floor_ceil_no_year_raises():
    at = adatetime(month=3, day=4)
    with pytest.raises(ValueError):
        at.floor()
    with pytest.raises(ValueError):
        at.ceil()


def test_adatetime_disambiguated_fully_specified_returns_datetime():
    at = adatetime(2020, 3, 4, 5, 6, 7, 8)
    result = at.disambiguated(datetime(2026, 1, 1))
    assert result == datetime(2020, 3, 4, 5, 6, 7, 8)


def test_adatetime_disambiguated_ambiguous_returns_timespan():
    at = adatetime(year=2020, month=3)
    result = at.disambiguated(datetime(2026, 1, 1))
    assert isinstance(result, timespan)
    assert result.start == datetime(2020, 3, 1, 0, 0, 0, 0)
    assert result.end == datetime(2020, 3, 31, 23, 59, 59, 999999)


@pytest.mark.parametrize("start, end", [
    pytest.param("not-a-date", datetime(2020, 1, 1), id="bad-start"),
    pytest.param(datetime(2020, 1, 1), "not-a-date", id="bad-end"),
])
def test_timespan_init_rejects_non_datetime(start, end):
    with pytest.raises(TimeError):
        timespan(start, end)


def test_timespan_eq_and_repr():
    a = timespan(datetime(2020, 1, 1), datetime(2020, 1, 2))
    b = timespan(datetime(2020, 1, 1), datetime(2020, 1, 2))
    assert a == b
    assert a != "not a timespan"
    assert repr(a) == (
        "timespan(datetime.datetime(2020, 1, 1, 0, 0), "
        "datetime.datetime(2020, 1, 2, 0, 0))"
    )


def test_timespan_disambiguate_time_only_uses_basedate():
    # Both sides have no date component ("3am to 5pm"), so basedate supplies
    # year/month/day for each side (times.py:301-306).
    start = adatetime(hour=3)
    end = adatetime(hour=17)
    ts = timespan(start, end).disambiguated(datetime(2026, 8, 4))
    assert ts.start.date() == datetime(2026, 8, 4).date()
    assert ts.end.date() == datetime(2026, 8, 4).date()


def test_timespan_disambiguate_no_year_either_side_uses_basedate_year():
    # Neither side has a year, but at least one has a month/day so
    # has_no_date() is False: both years fall back to the basedate
    # (times.py:314-316).
    ts = timespan(adatetime(month=3), adatetime(month=6)).disambiguated(datetime(2026, 1, 1))
    assert ts.start.year == 2026 and ts.end.year == 2026


def test_timespan_disambiguate_end_year_missing_uses_max_of_start_and_basedate_year():
    # end.year = max(start.year, basedate.year) (times.py:320-321).
    ts = timespan(adatetime(year=2020, month=1), adatetime(month=6)).disambiguated(
        datetime(2026, 1, 1))
    assert ts.start.year == 2020
    assert ts.end.year == 2026


def test_timespan_disambiguate_start_year_missing_uses_end_year():
    ts = timespan(adatetime(month=1), adatetime(year=2020, month=6)).disambiguated(
        datetime(2026, 1, 1))
    assert ts.start.year == 2020


def test_timespan_disambiguate_end_dm_copied_to_start():
    # End has month/day, start doesn't, and start's time-of-day isn't after
    # end's: so start borrows end's month/day (times.py:342-344).
    ts = timespan(adatetime(year=2020, hour=1), adatetime(year=2020, month=6, day=15, hour=5)
                  ).disambiguated(datetime(2026, 1, 1))
    assert ts.start.month == 6 and ts.start.day == 15


def test_timespan_disambiguate_end_dm_falls_back_to_basedate_when_time_inverted():
    # End has month/day, start doesn't, but start's time is later than end's
    #: so start falls back to the basedate's month/day (times.py:339-341).
    ts = timespan(adatetime(year=2020, hour=20), adatetime(year=2020, month=6, day=15, hour=5)
                  ).disambiguated(datetime(2026, 3, 9))
    assert ts.start.month == 3 and ts.start.day == 9


def test_timespan_disambiguate_start_dm_copied_to_end_uses_basedate():
    # Start has month/day, end doesn't: end borrows the basedate's
    # month/day (times.py:345-347). Basedate shares start's month/day so the
    # resulting order stays start <= end and no swap kicks in.
    ts = timespan(adatetime(year=2020, month=3, day=9, hour=1), adatetime(year=2020, hour=5)
                  ).disambiguated(datetime(2026, 3, 9))
    assert ts.end.month == 3 and ts.end.day == 9


def test_timespan_disambiguate_swaps_out_of_order_explicit_years():
    # Both years explicit but start > end: swap rather than shift a year.
    ts = timespan(adatetime(year=2021, month=1), adatetime(year=2020, month=6)
                  ).disambiguated(datetime(2026, 1, 1))
    assert ts.start < ts.end


def test_timespan_disambiguate_reduces_ambiguous_start_year():
    ts = timespan(adatetime(month=11), adatetime(year=2020, month=1)).disambiguated(
        datetime(2026, 1, 1))
    assert ts.start <= ts.end


def test_timespan_disambiguate_increases_ambiguous_end_year():
    ts = timespan(adatetime(year=2020, month=11), adatetime(month=1)).disambiguated(
        datetime(2026, 1, 1))
    assert ts.start <= ts.end


def test_timespan_disambiguate_increases_ambiguous_end_year_past_start():
    # end.year is ambiguous and gets filled to start.year via max(), but the
    # resulting month/day still puts end before start: so end.year is
    # bumped by one more (times.py:360-361), not swapped.
    ts = timespan(adatetime(year=2025, month=6, day=1), adatetime(month=1, day=1)
                  ).disambiguated(datetime(2020, 1, 1))
    assert ts.start < ts.end
    assert ts.end.year == 2026


def test_timespan_disambiguate_same_day_end_time_before_start_rolls_over():
    # "3pm to 5am" on the same disambiguated day should push the end to the
    # next day (times.py:369-371).
    ts = timespan(adatetime(hour=15), adatetime(hour=5)).disambiguated(datetime(2026, 8, 4))
    assert ts.end.date() == datetime(2026, 8, 5).date()


@pytest.mark.parametrize("value, expected", [
    pytest.param(datetime(2020, 1, 1), True, id="datetime-passthrough-floor"),
])
def test_floor_ceil_passthrough_for_datetime(value, expected):
    assert floor(value) is value
    assert ceil(value) is value


def test_fill_in_datetime_passthrough():
    dt = datetime(2020, 1, 1)
    assert fill_in(dt, datetime(2026, 1, 1)) is dt


def test_fill_in_timespan_basedate_passthrough():
    # A handful of grammar elements ("previous week"/"previous quarter")
    # resolve to an already-disambiguated timespan used as the basedate.
    ts = timespan(datetime(2020, 1, 1), datetime(2020, 1, 7))
    assert fill_in(adatetime(year=2021), ts) is ts


def test_fill_in_fills_missing_units_from_basedate():
    result = fill_in(adatetime(month=6), datetime(2026, 8, 4, 10, 30, 0, 0))
    assert result == datetime(2026, 6, 4, 10, 30, 0, 0)


@pytest.mark.parametrize("at, expected", [
    pytest.param(adatetime(), True, id="fully-ambiguous-has-no-date"),
    pytest.param(adatetime(hour=3), True, id="time-only-has-no-date"),
    pytest.param(adatetime(year=2020), False, id="year-set-has-date"),
    pytest.param(datetime(2020, 1, 1), False, id="datetime-always-has-date"),
])
def test_has_no_date(at, expected):
    assert has_no_date(at) is expected


@pytest.mark.parametrize("at, expected", [
    pytest.param(adatetime(), True, id="fully-ambiguous-has-no-time"),
    pytest.param(adatetime(year=2020), True, id="date-only-has-no-time"),
    pytest.param(adatetime(hour=3), False, id="hour-set-has-time"),
    pytest.param(datetime(2020, 1, 1), False, id="datetime-always-has-time"),
])
def test_has_no_time(at, expected):
    assert has_no_time(at) is expected


@pytest.mark.parametrize("at, expected", [
    pytest.param(adatetime(year=2020), True, id="partial-adatetime-is-ambiguous"),
    pytest.param(adatetime(2020, 1, 1, 0, 0, 0, 0), False, id="full-adatetime-not-ambiguous"),
    pytest.param(datetime(2020, 1, 1), False, id="datetime-never-ambiguous"),
])
def test_is_ambiguous(at, expected):
    assert is_ambiguous(at) is expected


@pytest.mark.parametrize("at, expected", [
    pytest.param(adatetime(), True, id="empty-adatetime-is-void"),
    pytest.param(adatetime(year=2020), False, id="partial-adatetime-not-void"),
    pytest.param(datetime(2020, 1, 1), False, id="datetime-never-void"),
    pytest.param(timespan(datetime(2020, 1, 1), datetime(2020, 1, 2)), False,
                 id="timespan-never-void"),
])
def test_is_void(at, expected):
    assert is_void(at) is expected


def test_fix_ambiguous_returns_unchanged():
    at = adatetime(year=2020)
    assert fix(at) is at


def test_fix_datetime_returns_unchanged():
    dt = datetime(2020, 1, 1)
    assert fix(dt) is dt


def test_fix_fully_specified_returns_datetime():
    at = adatetime(2020, 3, 4, 5, 6, 7, 8)
    result = fix(at)
    assert result == datetime(2020, 3, 4, 5, 6, 7, 8)
    assert isinstance(result, datetime)
