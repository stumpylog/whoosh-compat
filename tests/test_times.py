from whoosh_compat.parser.times import adatetime, timespan
from datetime import datetime


def test_adatetime_floor_ceil():
    at = adatetime(year=2020, month=3)
    assert at.floor() == datetime(2020, 3, 1, 0, 0, 0, 0)
    assert at.ceil() == datetime(2020, 3, 31, 23, 59, 59, 999999)


def test_timespan_disambiguate():
    ts = timespan(adatetime(year=2020), adatetime(year=2021))
    ts = ts.disambiguated(datetime(2026, 8, 4))
    assert ts.start == datetime(2020, 1, 1, 0, 0, 0, 0)
