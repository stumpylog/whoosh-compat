from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)


def dparse(q, reg):
    return wc.parse(q, registry=reg, default_fields=["content"], tz=BERLIN, basedate=BASE)


def test_year_precision(reg):
    r = dparse("created:2020", reg).ast
    assert r == ast.DateRange(field="created", lo=datetime(2020, 1, 1, tzinfo=timezone.utc),
                              hi=datetime(2021, 1, 1, tzinfo=timezone.utc), incl_lo=True, incl_hi=False)


def test_range_2020_to_2020(reg):
    r = dparse("created:[2020 TO 2020]", reg).ast
    assert r == ast.DateRange(field="created", lo=datetime(2020, 1, 1, tzinfo=timezone.utc),
                              hi=datetime(2021, 1, 1, tzinfo=timezone.utc), incl_lo=True, incl_hi=False)


def test_open_upper(reg):
    r = dparse("created:[2020 TO]", reg).ast
    assert r.lo is not None and r.hi is None


def test_yesterday_keyword(reg):
    r = dparse("added:yesterday", reg).ast
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(timezone.utc)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(timezone.utc) and not r.incl_hi


def test_previous_month(reg):
    r = dparse("added:'previous month'", reg).ast
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(timezone.utc)
    assert r.hi == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(timezone.utc) and not r.incl_hi


def test_now_compact(reg):
    r = dparse("added:[now-7d TO now]", reg).ast
    assert (BASE.astimezone(timezone.utc) - r.lo).days == 7


def test_whoosh_plusminus(reg):
    r = dparse("added:'-1 week'", reg).ast
    assert isinstance(r, ast.DateRange)


def test_bad_date_diagnostic(reg):
    res = dparse("added:notadate", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_datetime_boost_preserved(reg):
    r = dparse("added:2020^2.0", reg).ast
    assert isinstance(r, ast.Boosted) and r.boost == 2.0
