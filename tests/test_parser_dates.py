from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)


def dparse(q, reg):
    return wc.parse(q, registry=reg, default_fields=["content"], tz=BERLIN, basedate=BASE)


@pytest.mark.parametrize("query", [
    pytest.param("created:2020", id="bare-year-implies-year-range"),
    pytest.param("created:[2020 TO 2020]", id="explicit-same-year-range-is-equivalent"),
])
def test_year_precision(reg, query):
    r = dparse(query, reg).ast
    assert r == ast.DateRange(field="created", lo=datetime(2020, 1, 1, tzinfo=UTC),
                              hi=datetime(2021, 1, 1, tzinfo=UTC), incl_lo=True, incl_hi=False)


def test_open_upper(reg):
    r = dparse("created:[2020 TO]", reg).ast
    assert r.lo is not None and r.hi is None


def test_yesterday_keyword(reg):
    r = dparse("added:yesterday", reg).ast
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(UTC) and not r.incl_hi


def test_previous_month(reg):
    r = dparse("added:'previous month'", reg).ast
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC) and not r.incl_hi


def test_now_compact(reg):
    r = dparse("added:[now-7d TO now]", reg).ast
    assert (BASE.astimezone(UTC) - r.lo).days == 7


def test_whoosh_plusminus(reg):
    r = dparse("added:'-1 week'", reg).ast
    assert isinstance(r, ast.DateRange)


def test_bad_date_diagnostic(reg):
    res = dparse("added:notadate", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_datetime_boost_preserved(reg):
    r = dparse("added:2020^2.0", reg).ast
    assert isinstance(r, ast.Boosted) and r.boost == 2.0


# --- Daynames grammar (parser/dateparse.py:625-637) ------------------------

@pytest.mark.parametrize("query, expected_date", [
    # BASE is 2026-08-04, a Tuesday.
    pytest.param("added:'next monday'", datetime(2026, 8, 10), id="next-monday"),
    pytest.param("added:'last friday'", datetime(2026, 7, 31), id="last-friday"),
    pytest.param("added:'next tuesday'", datetime(2026, 8, 11),
                 id="next-same-weekday-wraps-a-week"),
])
def test_dayname_keywords(reg, query, expected_date):
    r = dparse(query, reg).ast
    assert r.lo == datetime(expected_date.year, expected_date.month, expected_date.day,
                            tzinfo=BERLIN).astimezone(UTC)


# --- Time12 grammar (parser/dateparse.py:647-657) ---------------------------

@pytest.mark.parametrize("query, hour, minute, second", [
    pytest.param("added:'3pm'", 15, 0, 0, id="3pm-bare-hour"),
    pytest.param("added:'12am'", 0, 0, 0, id="12am-is-midnight-hour"),
    pytest.param("added:'12pm'", 12, 0, 0, id="12pm-is-noon-hour"),
    pytest.param("added:'5:30am'", 5, 30, 0, id="5-30am-with-minutes"),
    pytest.param("added:'5:30:15pm'", 17, 30, 15, id="5-30-15pm-with-seconds"),
])
def test_time12_keywords(reg, query, hour, minute, second):
    r = dparse(query, reg).ast
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.hour, lo_local.minute, lo_local.second) == (hour, minute, second)


# --- Other relative-calendar keywords ---------------------------------------

def test_previous_year(reg):
    r = dparse("added:'previous year'", reg).ast
    assert r.lo == datetime(2025, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC) and not r.incl_hi


def test_previous_week(reg):
    # BASE 2026-08-04 is a Tuesday; this week's Monday is 2026-08-03, so
    # "previous week" is 2026-07-27 through (excl) 2026-08-03.
    r = dparse("added:'previous week'", reg).ast
    assert r.lo == datetime(2026, 7, 27, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 3, tzinfo=BERLIN).astimezone(UTC) and not r.incl_hi


def test_previous_quarter(reg):
    # BASE month is August (Q3), so the previous quarter is Q2: Apr-Jun.
    r = dparse("added:'previous quarter'", reg).ast
    assert r.lo == datetime(2026, 4, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC) and not r.incl_hi


def test_this_year(reg):
    r = dparse("added:'this year'", reg).ast
    assert r.lo == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)


def test_this_month(reg):
    r = dparse("added:'this month'", reg).ast
    assert r.lo == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC)


def test_today(reg):
    r = dparse("added:today", reg).ast
    assert r.lo == datetime(2026, 8, 4, tzinfo=BERLIN).astimezone(UTC)


def test_now_keyword(reg):
    r = dparse("added:now", reg).ast
    assert r.lo == BASE.astimezone(UTC)
    assert r.incl_lo and r.incl_hi  # an exact instant, not a period


@pytest.mark.parametrize("query, hour, minute, second", [
    pytest.param("added:midnight", 0, 0, 0, id="midnight"),
    pytest.param("added:noon", 12, 0, 0, id="noon"),
])
def test_midnight_noon(reg, query, hour, minute, second):
    r = dparse(query, reg).ast
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.hour, lo_local.minute, lo_local.second) == (hour, minute, second)


def test_tomorrow(reg):
    r = dparse("added:tomorrow", reg).ast
    assert r.lo == datetime(2026, 8, 5, tzinfo=BERLIN).astimezone(UTC)


# --- dmy/mdy/ymd/ydm and month-name sequences (English.setup's self.dmy) ---

@pytest.mark.parametrize("query", [
    pytest.param("created:'4 august 2020'", id="day-month-year"),
    pytest.param("created:'august 4 2020'", id="month-day-year"),
    pytest.param("created:'2020 august 4'", id="year-month-day"),
    pytest.param("created:'2020 4 august'", id="year-day-month"),
    pytest.param("created:'4 august'", id="day-month-no-year-uses-basedate"),
    pytest.param("created:'august 4'", id="month-day-no-year-uses-basedate"),
    pytest.param("created:'august 2020'", id="month-year"),
])
def test_named_month_sequences(reg, query):
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None


def test_month_alone(reg):
    # "august" alone with no day/year -> the whole month, in the basedate's
    # year (2026).
    r = dparse("created:august", reg).ast
    assert r.lo == datetime(2026, 8, 1, tzinfo=UTC)
    assert r.hi == datetime(2026, 9, 1, tzinfo=UTC) and not r.incl_hi


# --- Compact numeric "simple" sequence (DateParser.__init__'s self.simple) -

def test_compact_numeric_datetime(reg):
    r = dparse("added:'20200304'", reg).ast
    assert r.lo == datetime(2020, 3, 4, tzinfo=BERLIN).astimezone(UTC)


def test_compact_numeric_datetime_progressive_partial(reg):
    # progressive=True: a prefix of the simple sequence (year+month only)
    # still matches.
    r = dparse("added:'202003'", reg).ast
    assert r.lo == datetime(2020, 3, 1, tzinfo=BERLIN).astimezone(UTC)


# --- plusdate / nowcompact (Combo/PlusMinus grammar) -----------------------

def test_plusdate_years_months_weeks_combo(reg):
    r = dparse("added:'-1y2mo3w'", reg).ast
    assert isinstance(r, ast.DateRange)


# --- Range queries combining two date expressions (range_to_node) ---------

def test_range_both_bounds_named_dates(reg):
    r = dparse("created:[2020-01-01 TO 2020-12-31]", reg).ast
    assert r.lo == datetime(2020, 1, 1, tzinfo=UTC)
    assert r.hi == datetime(2021, 1, 1, tzinfo=UTC)


def test_range_open_lower(reg):
    r = dparse("created:[TO 2020]", reg).ast
    assert r.lo is None and r.hi is not None


def test_range_both_sides_are_periods_cannot_combine(reg):
    # Both sides resolve to an already-disambiguated timespan ("previous
    # week"/"previous month"), which can't be nested inside another
    # timespan() -- exercises range_to_node's non-combine branch for both
    # raw_start and raw_end.
    r = dparse("added:['previous month' TO 'this month']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)


def test_range_bad_start_diagnostic(reg):
    res = dparse("added:[notadate TO 2020]", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_range_bad_end_diagnostic(reg):
    res = dparse("added:[2020 TO notadate]", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


# --- torange Combo grammar (free "X to Y" text inside one field value) -----

def test_torange_combo_basic(reg):
    r = dparse("added:'3pm to 5pm'", reg).ast
    assert isinstance(r, ast.DateRange)
    lo_local = r.lo.astimezone(BERLIN)
    assert lo_local.hour == 15


def test_torange_combo_first_side_fails_whole_thing_fails(reg):
    # Combo.parse's `if at is None: return (None, None)` -- the first bundle
    # doesn't match at all, so neither the "to" combo nor any dmy/bundle
    # alternative in the outer Choice matches either.
    res = dparse("added:'notadate to 2020'", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_sequence_fill_in_type_error_rejected(reg):
    # Combining day=31 with month=february (2020, a leap year -> Feb has 29
    # days) inside the dmy Sequence raises TimeError from fill_in()'s
    # adatetime(**args) construction (parser/dateparse.py:233-236); Sequence
    # swallows it and reports failure, so no dmy/mdy/... alternative
    # matches and the whole field value is an unparseable date.
    res = dparse("created:'31 february 2020'", reg)
    assert res.diagnostics and res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_bag_time_and_date_both_match_in_one_value(reg):
    # English.setup's `self.datetime = Bag((self.time, self.dmy))`: both
    # sub-elements matching in the same value hits the `onceper and
    # all(seen)` early-break (parser/dateparse.py:416-417).
    r = dparse("added:'3pm 4 august 2020'", reg).ast
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.year, lo_local.month, lo_local.day, lo_local.hour) == (2020, 8, 4, 15)


def test_dateparser_parse_method_direct() -> None:
    # DateParser.parse() (distinct from .date_from(), which is what
    # DateParserPlugin actually calls) is unused by whoosh-compat's own
    # code -- same as upstream whoosh, where it's likewise dead from the
    # plugin's perspective (only .date_from() is called there too). It's
    # still a documented, non-trivial public method of the grammar classes,
    # so it's covered here directly rather than deleted.
    from whoosh_compat.parser.dateparse import English
    from whoosh_compat.parser.times import timespan

    parser = English()
    result, pos = parser.parse("2020", datetime(2026, 1, 1))
    # Unlike DateParserPlugin (which turns a period into an exclusive
    # ceil+1us upper bound), DateParser.parse() returns times.py's raw
    # disambiguated() result: an inclusive floor/ceil timespan.
    assert result == timespan(datetime(2020, 1, 1, 0, 0, 0, 0),
                               datetime(2020, 12, 31, 23, 59, 59, 999999))
    assert pos == 4


# --- Low-level grammar-class internals (unreachable via whoosh_compat.parse
# because DateParserPlugin only ever calls .date_from(), never .parse(),
# and never triggers debug tracing or repr()) ------------------------------

def test_dateparse_internals_repr_and_debug() -> None:
    from whoosh_compat.parser import dateparse as dp

    props = dp.Props(year=2020)
    assert repr(props) == repr({"year": 2020})
    with pytest.raises(AttributeError):
        _ = props.nope

    seq = dp.Sequence((dp.Regex("x"),), name="s")
    assert repr(seq) == "Sequence<s>[<'x'>]"

    assert repr(dp.Regex("abc")) == "<'abc'>"
    assert repr(dp.ToEnd(dp.Regex("abc"))) == "ToEnd(<'abc'>)"

    # print_debug's body only runs when level > 0; DateParserPlugin never
    # passes a positive debug level, so this exercises it directly.
    dp.print_debug(1, "msg %s", "x")


def test_parserbase_date_from_defaults_dt_to_now() -> None:
    # ParserBase.date_from() (inherited by Sequence/Combo/Choice/Bag/Regex/
    # ToEnd, none of which override it) falls back to datetime.now() when
    # no dt is given. Production code (DateParserPlugin) always passes an
    # explicit basedate through DateParser.date_from(), so this default is
    # only reachable by calling a low-level element directly.
    from whoosh_compat.parser import dateparse as dp

    regex = dp.Regex(r"(?P<year>[0-9]{4})", lambda p, dt: dt)
    result = regex.date_from("2020")
    assert result is not None


def test_regex_props_to_date_default_uses_all_units() -> None:
    from whoosh_compat.parser import dateparse as dp
    from whoosh_compat.parser.times import adatetime

    regex = dp.Regex(r"(?P<year>[0-9]{4})")
    result, _ = regex.parse("2020", datetime(2026, 1, 1))
    assert result == adatetime(year=2020)


def test_dateparser_parse_exact_datetime_skips_disambiguation() -> None:
    # DateParser.parse()'s `if isinstance(d, (adatetime, timespan))` branch
    # is skipped when the grammar already resolves to an exact datetime
    # ("now"), rather than an ambiguous adatetime/timespan.
    from whoosh_compat.parser.dateparse import English

    parser = English()
    dt = datetime(2026, 1, 1, 10, 30)
    result, _ = parser.parse("now", dt)
    assert result == dt


def test_dateparser_date_from_defaults_and_toend_false() -> None:
    from whoosh_compat.parser.dateparse import English

    parser = English()
    # basedate=None -> falls back to datetime.utcnow() internally.
    result = parser.date_from("2020")
    assert result is not None

    # toend=False: trailing garbage after a valid match is tolerated.
    result2 = parser.date_from("2020 garbage", datetime(2026, 1, 1), toend=False)
    assert result2 is not None


def test_daterangesyntaxnode_and_dateerrornode_r() -> None:
    from whoosh_compat.errors import Diagnostic
    from whoosh_compat.errors import DiagnosticKind
    from whoosh_compat.parser.dateparse import DateErrorNode
    from whoosh_compat.parser.dateparse import DateRangeSyntaxNode

    node = DateRangeSyntaxNode("added", datetime(2020, 1, 1), datetime(2020, 1, 2), True, False)
    assert node.r() == f"DateRange {datetime(2020, 1, 1)!r}-{datetime(2020, 1, 2)!r}"

    diag = Diagnostic(message="bad", kind=DiagnosticKind.BAD_DATE, startchar=0, endchar=1)
    err = DateErrorNode(diag)
    assert err.r() == "DateError 'bad'"


def test_do_dates_leaves_non_range_non_text_date_field_node_untouched(reg) -> None:
    # do_dates()'s trailing `else: continue` (has_fieldname but neither a
    # RangeNode nor has_text) has no real-world producer in the current
    # plugin set -- FieldnameNode/RangeNode/TextNode are the only
    # has_fieldname=True node types, and FieldnameNode never survives as a
    # leaf. Exercised directly against a minimal stub node.
    from whoosh_compat.parser import syntax
    from whoosh_compat.parser.dateparse import DateParserPlugin

    class FieldOnlyNode(syntax.SyntaxNode):
        has_fieldname = True

        def __init__(self, fieldname):
            self.fieldname = fieldname

    plugin = DateParserPlugin(BASE, BERLIN)

    class StubParser:
        registry = reg

    group = syntax.AndGroup([FieldOnlyNode("added")])
    result = plugin.do_dates(StubParser(), group)
    assert result[0] is group[0]  # untouched, not replaced


def test_range_exclusive_bounds_ignored_for_ambiguous_bounds(reg):
    # "2020"/"2021" are ambiguous (year-only) bounds, not exact instants, so
    # range_to_node forces incl_lo=True regardless of the query's "{" exclusive
    # marker (parser/dateparse.py:999-1000) -- only incl_hi is
    # forced-exclusive (it already carries the "+1us" half-open adjustment).
    r = dparse("added:{2020 TO 2021}", reg).ast
    assert r.incl_lo is True and r.incl_hi is False
