from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser import dateparse as dp
from whoosh_compat.parser import syntax
from whoosh_compat.parser.dateparse import DateErrorNode
from whoosh_compat.parser.dateparse import DateParserPlugin
from whoosh_compat.parser.dateparse import DateRangeSyntaxNode
from whoosh_compat.parser.dateparse import English
from whoosh_compat.parser.default import QueryParser
from whoosh_compat.parser.times import adatetime
from whoosh_compat.parser.times import timespan

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)


def dparse(q: str, reg: FieldRegistry) -> wc.ParseResult:
    return wc.parse(q, registry=reg, default_fields=["content"], tz=BERLIN, basedate=BASE)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:2020", id="bare-year-implies-year-range"),
        pytest.param("created:[2020 TO 2020]", id="explicit-same-year-range-is-equivalent"),
    ],
)
def test_year_precision(reg: FieldRegistry, query: str) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r == ast.DateRange(
        field=FieldRef("created"),
        lo=datetime(2020, 1, 1, tzinfo=UTC),
        hi=datetime(2021, 1, 1, tzinfo=UTC),
        incl_lo=True,
        incl_hi=False,
    )


def test_open_upper(reg: FieldRegistry) -> None:
    r = dparse("created:[2020 TO]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert r.hi is None


def test_yesterday_keyword(reg: FieldRegistry) -> None:
    r = dparse("added:yesterday", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_month(reg: FieldRegistry) -> None:
    r = dparse("added:'previous month'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_now_compact(reg: FieldRegistry) -> None:
    r = dparse("added:[now-7d TO now]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert (BASE.astimezone(UTC) - r.lo).days == 7


def test_whoosh_plusminus(reg: FieldRegistry) -> None:
    r = dparse("added:'-1 week'", reg).ast
    assert isinstance(r, ast.DateRange)


def test_bad_date_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:notadate", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    # A host mapping this to a typed exception (e.g. InvalidDateQuery(field,
    # value)) needs the field name and offending text structurally, not by
    # regex-parsing the rendered message.
    assert res.diagnostics[0].field == FieldRef("added")
    assert res.diagnostics[0].raw_value == "notadate"


def test_datetime_boost_preserved(reg: FieldRegistry) -> None:
    r = dparse("added:2020^2.0", reg).ast
    assert isinstance(r, ast.Boosted)
    assert r.boost == 2.0


# --- Daynames grammar (parser/dateparse.py:625-637) ------------------------


@pytest.mark.parametrize(
    ("query", "expected_date"),
    [
        # BASE is 2026-08-04, a Tuesday.
        pytest.param("added:'next monday'", datetime(2026, 8, 10), id="next-monday"),
        pytest.param("added:'last friday'", datetime(2026, 7, 31), id="last-friday"),
        pytest.param(
            "added:'next tuesday'", datetime(2026, 8, 11), id="next-same-weekday-wraps-a-week"
        ),
    ],
)
def test_dayname_keywords(reg: FieldRegistry, query: str, expected_date: datetime) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(
        expected_date.year, expected_date.month, expected_date.day, tzinfo=BERLIN
    ).astimezone(UTC)


# --- Time12 grammar (parser/dateparse.py:647-657) ---------------------------


@pytest.mark.parametrize(
    ("query", "hour", "minute", "second"),
    [
        pytest.param("added:'3pm'", 15, 0, 0, id="3pm-bare-hour"),
        pytest.param("added:'12am'", 0, 0, 0, id="12am-is-midnight-hour"),
        pytest.param("added:'12pm'", 12, 0, 0, id="12pm-is-noon-hour"),
        pytest.param("added:'5:30am'", 5, 30, 0, id="5-30am-with-minutes"),
        pytest.param("added:'5:30:15pm'", 17, 30, 15, id="5-30-15pm-with-seconds"),
    ],
)
def test_time12_keywords(
    reg: FieldRegistry, query: str, hour: int, minute: int, second: int
) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.hour, lo_local.minute, lo_local.second) == (hour, minute, second)


# --- Other relative-calendar keywords ---------------------------------------


def test_previous_year(reg: FieldRegistry) -> None:
    r = dparse("added:'previous year'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2025, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_week(reg: FieldRegistry) -> None:
    # BASE 2026-08-04 is a Tuesday; this week's Monday is 2026-08-03, so
    # "previous week" is 2026-07-27 through (excl) 2026-08-03.
    r = dparse("added:'previous week'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 27, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 3, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_quarter(reg: FieldRegistry) -> None:
    # BASE month is August (Q3), so the previous quarter is Q2: Apr-Jun.
    r = dparse("added:'previous quarter'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 4, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_this_year(reg: FieldRegistry) -> None:
    r = dparse("added:'this year'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)


def test_this_month(reg: FieldRegistry) -> None:
    r = dparse("added:'this month'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC)


def test_today(reg: FieldRegistry) -> None:
    r = dparse("added:today", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, tzinfo=BERLIN).astimezone(UTC)


def test_now_keyword(reg: FieldRegistry) -> None:
    r = dparse("added:now", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == BASE.astimezone(UTC)
    assert r.incl_lo
    assert r.incl_hi


@pytest.mark.parametrize(
    ("query", "hour", "minute", "second"),
    [
        pytest.param("added:midnight", 0, 0, 0, id="midnight"),
        pytest.param("added:noon", 12, 0, 0, id="noon"),
    ],
)
def test_midnight_noon(reg: FieldRegistry, query: str, hour: int, minute: int, second: int) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.hour, lo_local.minute, lo_local.second) == (hour, minute, second)
    # midnight/noon disambiguate to a zero-width (start == end) timespan, an
    # exact instant, not an ambiguous period: both bounds inclusive, not a
    # half-open range one microsecond wide (see cb3a4b1).
    assert r.lo == r.hi
    assert r.incl_lo
    assert r.incl_hi


def test_tomorrow(reg: FieldRegistry) -> None:
    r = dparse("added:tomorrow", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 5, tzinfo=BERLIN).astimezone(UTC)


# --- dmy/mdy/ymd/ydm and month-name sequences (English.setup's self.dmy) ---


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:'4 august 2020'", id="day-month-year"),
        pytest.param("created:'august 4 2020'", id="month-day-year"),
        pytest.param("created:'2020 august 4'", id="year-month-day"),
        pytest.param("created:'2020 4 august'", id="year-day-month"),
        pytest.param("created:'4 august'", id="day-month-no-year-uses-basedate"),
        pytest.param("created:'august 4'", id="month-day-no-year-uses-basedate"),
        pytest.param("created:'august 2020'", id="month-year"),
    ],
)
def test_named_month_sequences(reg: FieldRegistry, query: str) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None


def test_month_alone(reg: FieldRegistry) -> None:
    # "august" alone with no day/year -> the whole month, in the basedate's
    # year (2026).
    r = dparse("created:august", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 1, tzinfo=UTC)
    assert r.hi == datetime(2026, 9, 1, tzinfo=UTC)
    assert not r.incl_hi


# --- Compact numeric "simple" sequence (DateParser.__init__'s self.simple) -


def test_compact_numeric_datetime(reg: FieldRegistry) -> None:
    r = dparse("added:'20200304'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 4, tzinfo=BERLIN).astimezone(UTC)


def test_compact_numeric_datetime_progressive_partial(reg: FieldRegistry) -> None:
    # progressive=True: a prefix of the simple sequence (year+month only)
    # still matches.
    r = dparse("added:'202003'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 1, tzinfo=BERLIN).astimezone(UTC)


# --- plusdate / nowcompact (Combo/PlusMinus grammar) -----------------------


def test_plusdate_years_months_weeks_combo(reg: FieldRegistry) -> None:
    r = dparse("added:'-1y2mo3w'", reg).ast
    assert isinstance(r, ast.DateRange)


# --- Dashed/space/dot/slash-separated ISO dates (English.__init__'s "simple"
# sequence; see module docstring / DIVERGENCES.md for the bug this covers:
# the "bundle" Choice used to try the "datetime" Bag before "simple", which
# partial-matched a bare year and starved "simple" of the input, so any
# separated single-value date failed to parse at all) -----------------------


@pytest.mark.parametrize(
    ("query", "lo", "hi"),
    [
        pytest.param(
            "created:2020-01-01",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="bare-dashed-day",
        ),
        pytest.param(
            "created:'2020-01-01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="quoted-dashed-day",
        ),
        pytest.param(
            "created:2020-01",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 2, 1, tzinfo=UTC),
            id="bare-dashed-month",
        ),
        pytest.param(
            "created:'2020-01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 2, 1, tzinfo=UTC),
            id="quoted-dashed-month",
        ),
        pytest.param(
            "created:'2020 01 01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="space-separated-day",
        ),
        pytest.param(
            "created:'2020.01.01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="dot-separated-day",
        ),
        pytest.param(
            "created:'2020/01/01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="slash-separated-day",
        ),
    ],
)
def test_separated_iso_date_precision(
    reg: FieldRegistry, query: str, lo: datetime, hi: datetime
) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange), r
    assert r.lo == lo
    assert r.hi == hi
    assert not r.incl_hi


# --- RFC3339 "T"/"Z" datetime separator (paperless-ngx PR #13010 back-compat;
# DateParser.__init__'s "simple" sequence now also accepts "T", and
# DateParserPlugin._split_rfc3339_utc strips a trailing "Z") ---------------


def test_rfc3339_t_separator_without_z_still_uses_local_tz(reg: FieldRegistry) -> None:
    # No "Z": "T" is just an accepted separator (like space/dash/dot/slash
    # already were), the value is still local wall-clock time in BERLIN.
    r = dparse("added:'2026-08-04T10:30:00'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN).astimezone(UTC)
    assert r.incl_lo


def test_rfc3339_t_z_is_an_absolute_utc_instant_not_local(reg: FieldRegistry) -> None:
    # With "Z": the value names an absolute UTC instant and must NOT be
    # reinterpreted as BERLIN wall-clock time and shifted again -- this is
    # the exact bug _split_rfc3339_utc's "tz" override on _to_utc exists to
    # avoid. BASE/BERLIN is UTC+2 in August, so a naive local-tz
    # misinterpretation of the same digits would be 2 hours off.
    r = dparse("added:'2026-08-04T10:30:00Z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.incl_lo


def test_rfc3339_lowercase_t_and_z(reg: FieldRegistry) -> None:
    # Sequence's separator regex is compiled with re.IGNORECASE; the "Z"
    # designator check is explicitly case-insensitive too.
    r = dparse("added:'2026-08-04t10:30:00z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi", "expected_leftover"),
    [
        pytest.param(
            "added:2026-08-04T10:30:00",
            datetime(2026, 8, 1, tzinfo=BERLIN),
            datetime(2026, 9, 1, tzinfo=BERLIN),
            "30:00",
            id="full-date-truncates-to-month",
        ),
        pytest.param(
            "added:2026-08T10:30",
            datetime(2026, 1, 1, tzinfo=BERLIN),
            datetime(2027, 1, 1, tzinfo=BERLIN),
            "08T10:30",
            id="no-day-truncates-to-year",
        ),
    ],
)
def test_bare_unquoted_t_value_truncates_with_leftover_terms(
    reg: FieldRegistry,
    query: str,
    expected_lo: datetime,
    expected_hi: datetime,
    expected_leftover: str,
) -> None:
    # DIVERGENCES.md entry 49: without quotes, the tokenizer splits the
    # value at its colons before the date grammar runs, and the grammar
    # then prefix-matches only the complete date units before the
    # T-fused chunk ("2026-08-" -> the month window; "2026-" -> the year
    # window), with the stray remainder surviving as an ordinary term.
    # Real whoosh truncates to the SAME window with the same leftover
    # (measured); keeping that truncation is deliberate parity, not a
    # missed parse. The quoted spelling (entry 48) and the
    # bracketed-range spelling are the ones that honor the full value.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.And)
    dr, leftover = r.children
    assert isinstance(dr, ast.DateRange)
    assert dr.lo == expected_lo.astimezone(UTC)
    assert dr.hi == expected_hi.astimezone(UTC)
    assert dr.incl_lo
    assert not dr.incl_hi
    assert isinstance(leftover, ast.Term)
    assert leftover.field is not None
    assert leftover.field.name == "content"
    assert leftover.text == expected_leftover


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        pytest.param(
            "added:2026T10",
            datetime(2026, 10, 1, tzinfo=BERLIN),
            datetime(2026, 11, 1, tzinfo=BERLIN),
            id="bare-year-t-month",
        ),
        pytest.param(
            "added:'2026T10'",
            datetime(2026, 10, 1, tzinfo=BERLIN),
            datetime(2026, 11, 1, tzinfo=BERLIN),
            id="quoted-year-t-month",
        ),
        pytest.param(
            "added:2026T10:30",
            datetime(2026, 10, 30, tzinfo=BERLIN),
            datetime(2026, 10, 31, tzinfo=BERLIN),
            id="colon-split-day-joins-year-t-month",
        ),
    ],
)
def test_no_separator_t_value_parses_as_year_t_month(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # DIVERGENCES.md entry 50: with T in the separator class, a dash-less
    # "2026T10" reads as year-T-month; a colon-split trailing token
    # ("2026T10:30") is joined by the date parser into a day-precision
    # reading. Real whoosh cannot read these at all (_NullQuery, matches
    # nothing), so whoosh-compat's reading is the compat-favorable side
    # of a documented divergence, not parity.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.astimezone(UTC)
    assert r.hi == expected_hi.astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_rfc3339_range_bounds_with_z(reg: FieldRegistry) -> None:
    # The motivating case (paperless-ngx issue/PR #13010): a bracketed range
    # of RFC3339 "Z" datetimes, unquoted -- no colon-tokenizing ambiguity
    # inside range bounds, unlike a bare unquoted single value.
    r = dparse("created:[2026-01-01T00:00:00Z TO 2026-06-01T00:00:00Z]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 1, 1, tzinfo=UTC)
    # "created" is date_only=True. Both bounds carry a microsecond field
    # left unspecified by the grammar (there is no ".NNNNNN" in the query
    # text), which is itself ambiguous (see is_ambiguous/adatetime), so
    # both bounds go through the joint-disambiguation "period" path rather
    # than the exact-instant path; the hi side's exclusive ceiling then
    # rounds its ambiguous microsecond up, landing one calendar day past
    # its own date once date_only-collapsed (pre-existing _to_utc ceiling
    # behavior, not new: see test_date_only_whole_day_value_still_ceils_exactly_once).
    assert r.hi == datetime(2026, 6, 2, tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        pytest.param(
            "added:[2021-06-01T00:00:00Z TO 2020]",
            datetime(2020, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2021, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="backwards-z-start-swapped-keeps-utc",
        ),
        pytest.param(
            "added:[2021 TO 2020-06-01T00:00:00Z]",
            datetime(2020, 6, 1, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            id="backwards-z-end-swapped-keeps-utc",
        ),
        # The next three ranges mix a "Z" bound with a year-AMBIGUOUS local
        # bound. The joint step then mutates the ambiguous bound's year
        # (borrowing it from the other side) WITHOUT swapping (times.py's
        # backwards-swap line is unreachable unless both bounds carry
        # explicit years), so bounds_swapped must stay False and each tz
        # must stay with its positional bound; mistaking the borrow's
        # value change for a swap shifts the Z bound onto local time.
        pytest.param(
            "added:[feb TO 2027-06-01T00:00:00Z]",
            datetime(2027, 2, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="ambiguous-local-start-borrows-year-no-false-swap",
        ),
        pytest.param(
            "added:[dec TO 2026-08-04T10:30:00Z]",
            datetime(2025, 12, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC),
            id="ambiguous-local-start-borrows-prior-year-no-false-swap",
        ),
        pytest.param(
            "added:[2026-08-04T10:30:00Z TO feb]",
            datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
            datetime(2027, 3, 1, tzinfo=BERLIN).astimezone(UTC),
            id="ambiguous-local-end-borrows-next-year-no-false-swap",
        ),
        # A year-only local START with the SAME explicit year as the "Z"
        # bound: the equal-years month/day fill copies the end's date onto
        # the start, mutating its value with no swap possible (the filled
        # dates are equal, never out of order), so bounds_swapped must
        # stay False and positional tz must hold. The mirrored end-side
        # fill takes its date from the basedate instead and CAN go out of
        # order (both variants pinned below).
        pytest.param(
            "added:[2027 TO 2027-06-01T00:00:00Z]",
            datetime(2027, 6, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="equal-year-start-filled-from-z-end-no-false-swap",
        ),
        pytest.param(
            "added:[2026 TO 2026-08-04T10:30:00Z]",
            datetime(2026, 8, 4, tzinfo=BERLIN).astimezone(UTC),
            datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC),
            id="equal-year-start-filled-from-z-end-basedate-year-no-false-swap",
        ),
        pytest.param(
            "added:[2027-06-01T00:00:00Z TO 2027]",
            datetime(2027, 6, 1, tzinfo=UTC),
            datetime(2027, 8, 5, tzinfo=BERLIN).astimezone(UTC),
            id="equal-year-end-filled-from-basedate-in-order",
        ),
        pytest.param(
            "added:[2027-09-15T00:00:00Z TO 2027]",
            datetime(2027, 8, 4, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="equal-year-end-filled-from-basedate-genuine-swap",
        ),
        # A year-only start with a DIFFERENT explicit year: no fill
        # mutates it (the equal-years precondition fails), the backwards
        # orientation is a genuine swap (bounds_swapped True), and the
        # tzs must follow the swapped values.
        pytest.param(
            "added:[2027 TO 2026-06-01T00:00:00Z]",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2028, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            id="year-only-start-different-year-genuine-swap",
        ),
        # A year+time local start ("2027 10pm": explicit year, NO month or
        # day, explicit hour) is the one date-missing shape whose
        # equal-years fill takes the basedate branch (its floor time
        # exceeds the Z end's ceil time), which can land the filled date
        # after the Z bound and trigger a genuine swap. The tzs must
        # follow that swap even though the start bound was mutated first.
        pytest.param(
            "added:[2027 10pm TO 2027-06-01T00:00:00Z]",
            datetime(2027, 6, 1, tzinfo=UTC),
            datetime(2027, 8, 4, 23, 0, tzinfo=BERLIN).astimezone(UTC),
            id="year-time-start-basedate-filled-genuine-swap",
        ),
        pytest.param(
            "added:[2027 10pm TO 2027-09-15T00:00:00Z]",
            datetime(2027, 8, 4, 22, 0, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="year-time-start-basedate-filled-in-order",
        ),
        pytest.param(
            "added:[2027-09-15T00:00:00Z TO 2027 10pm]",
            datetime(2027, 8, 4, 22, 0, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="year-time-end-basedate-filled-genuine-swap",
        ),
    ],
)
def test_rfc3339_mixed_tz_bounds_survive_a_backwards_swap(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # A backwards range with two explicit years swaps its bounds in the
    # joint-disambiguation step; each bound's timezone (UTC for a
    # "Z"-suffixed RFC3339 bound, the query's local tz otherwise) must
    # follow its VALUE through the swap, or the Z bound gets reinterpreted
    # as local wall-clock time and silently shifted, the exact bug the
    # designator handling exists to prevent.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo
    assert r.hi == expected_hi


def test_rfc3339_range_open_lo_bound_with_z(reg: FieldRegistry) -> None:
    # A "Z" bound as the sole (lo) side of an open-ended range: no second
    # bound to combine with, so this exercises the individual (non-joint)
    # bound-resolution path with the tz override applied.
    r = dparse("added:[2026-08-04T10:30:00Z TO]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.hi is None


def test_rfc3339_range_open_hi_bound_with_z(reg: FieldRegistry) -> None:
    r = dparse("added:[TO 2026-08-04T10:30:00Z]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is None
    # Unlike the lo-side test above, an hi-only bound is ambiguous (no
    # ".NNNNNN" microsecond in the query text) rather than exact, so it
    # takes the exclusive-ceiling path: the ceiling rounds the unspecified
    # microsecond up to 999999 and then adds one, carrying into the next
    # whole second (pre-existing adatetime.ceil()/fill_in behavior, not new
    # -- see test_rfc3339_range_bounds_with_z's comment for the same
    # mechanism at date_only granularity).
    assert r.hi == datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC)


def test_rfc3339_range_mixes_z_and_non_z_bound(reg: FieldRegistry) -> None:
    # A "Z" bound and a plain local (day-precision, unambiguous-enough to
    # avoid the microsecond-ambiguity ceiling exercised above) bound in the
    # same range each keep their own tz treatment (start_tz/end_tz in
    # _range_to_node are per-bound, not shared).
    r = dparse("added:[2026-08-04T10:30:00Z TO 2026-08-10]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.hi == datetime(2026, 8, 11, tzinfo=BERLIN).astimezone(UTC)


def test_rfc3339_date_only_field_z_designator_is_a_no_op(reg: FieldRegistry) -> None:
    # "created" is date_only=True: _to_utc never applies any tz (local or
    # UTC) to a date_only field, so "Z" makes no observable difference
    # there, but it must still parse rather than error.
    r = dparse("created:'2026-08-04T00:00:00Z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, tzinfo=UTC)


def test_trailing_z_without_preceding_t_is_not_treated_as_utc_designator(
    reg: FieldRegistry,
) -> None:
    # The RFC3339 "Z" gate requires a "T" earlier in the string (mirrors
    # paperless's own ``_bound_datetimes``'s ``if "T" in token:`` gate): a
    # bare trailing "z"/"Z" with no "T" is not this designator at all, and
    # is simply an unrecognizable date (there is no separate "z" grammar
    # element either).
    res = dparse("added:'20200304Z'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


# --- Range queries combining two date expressions (range_to_node) ---------


def test_range_both_bounds_named_dates(reg: FieldRegistry) -> None:
    r = dparse("created:[2020-01-01 TO 2020-12-31]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 1, 1, tzinfo=UTC)
    assert r.hi == datetime(2021, 1, 1, tzinfo=UTC)


def test_range_bounds_do_not_collapse_to_year(reg: FieldRegistry) -> None:
    # Companion to test_range_both_bounds_named_dates: that test's answer
    # coincidentally matches what the pre-fix "collapse to bare year" bug
    # (DIVERGENCES.md, range_to_node not enforcing full-text consumption on
    # a bound) would also produce (both bounds fall in 2020, so the whole
    # year happens to contain them). This case doesn't coincide: the buggy
    # behavior would silently produce the whole of 2020, not June 15-20.
    r = dparse("created:[2020-06-15 TO 2020-06-20]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 6, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 6, 21, tzinfo=UTC)


# --- date_only exclusive-upper-bound ceiling ------------------------------


def test_date_only_time_bearing_single_value_ceils_hi_to_next_day(reg: FieldRegistry) -> None:
    # "created" is date_only=True. A time-bearing value with any
    # sub-day precision left ambiguous (minutes here, seconds unspecified)
    # produces a half-open period; date_only truncation must not let the
    # exclusive hi bound fall back to (or before) lo's own day.
    r = dparse("created:'2020-03-15 15:30'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_date_only_range_time_bearing_end_bound_includes_named_end_day(
    reg: FieldRegistry,
) -> None:
    r = dparse("created:[2020-03-01 TO '2020-03-15 12:00']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 1, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert not r.incl_hi


def test_date_only_range_time_bearing_start_bound_still_truncates_down(
    reg: FieldRegistry,
) -> None:
    # The lo bound already truncates down correctly; the ceiling fix must
    # not touch it.
    r = dparse("created:['2020-03-15 15:00' TO 2020-11-30]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 12, 1, tzinfo=UTC)


def test_date_only_same_day_range_times_on_both_ends_is_not_empty(reg: FieldRegistry) -> None:
    r = dparse("created:['2020-03-15 00:00' TO '2020-03-15 18:00']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert r.lo < r.hi


def test_date_only_whole_day_value_still_ceils_exactly_once(reg: FieldRegistry) -> None:
    # Regression guard: a value with no time-of-day component (already
    # day-aligned) must not get an extra day tacked on.
    r = dparse("created:2020-03-15", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:noon", id="degenerate-instant-noon"),
        pytest.param("created:'3pm'", id="hour-precision-period-3pm"),
    ],
)
def test_date_only_noon_and_3pm_consistently_cover_their_day(
    reg: FieldRegistry, query: str
) -> None:
    # Both a degenerate exact-instant value (noon) and an hour-precision
    # period value (3pm) must resolve to a range that covers the same
    # calendar day on a date_only field: before the fix, 3pm's half-open
    # ceiling collapsed to an empty [midnight, midnight) range while noon's
    # both-inclusive shape happened to still work.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    basedate_local = BASE.astimezone(BERLIN)
    expected_day = datetime(
        basedate_local.year, basedate_local.month, basedate_local.day, tzinfo=UTC
    )
    assert r.lo == expected_day
    assert r.hi is not None
    # The range must be non-empty (would-be-matching-document shape) under
    # the (lo, hi, incl_lo, incl_hi) semantics.
    assert r.lo < r.hi or (r.lo == r.hi and r.incl_lo and r.incl_hi)


def test_range_open_lower(reg: FieldRegistry) -> None:
    r = dparse("created:[TO 2020]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is None
    assert r.hi is not None


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        # BASE is 2026-08-04. Whoosh reads a bracketed range's two bounds
        # jointly (timespan.disambiguated's range heuristics), so a
        # backward-reading range spans the boundary instead of inverting.
        pytest.param(
            "added:[dec to feb]",
            datetime(2025, 12, 1, tzinfo=BERLIN),
            datetime(2026, 3, 1, tzinfo=BERLIN),
            id="backward-months-borrow-previous-year",
        ),
        pytest.param(
            "added:[dec 25 to jan 5]",
            datetime(2025, 12, 25, tzinfo=BERLIN),
            datetime(2026, 1, 6, tzinfo=BERLIN),
            id="year-boundary-crossing-days",
        ),
        pytest.param(
            "added:[noon to 3am]",
            datetime(2026, 8, 4, 12, 0, tzinfo=BERLIN),
            datetime(2026, 8, 5, 4, 0, tzinfo=BERLIN),
            id="overnight-time-range-pushes-end-to-next-day",
        ),
        pytest.param(
            "added:[3pm to 10am]",
            datetime(2026, 8, 4, 15, 0, tzinfo=BERLIN),
            datetime(2026, 8, 5, 11, 0, tzinfo=BERLIN),
            id="overnight-pm-to-am",
        ),
        pytest.param(
            "added:[feb to may]",
            datetime(2026, 2, 1, tzinfo=BERLIN),
            datetime(2026, 6, 1, tzinfo=BERLIN),
            id="forward-months-both-basedate-year",
        ),
        pytest.param(
            "added:[2021 to 2020]",
            datetime(2020, 1, 1, tzinfo=BERLIN),
            datetime(2022, 1, 1, tzinfo=BERLIN),
            id="backward-years-swap-to-cover-both",
        ),
    ],
)
def test_range_joint_disambiguation(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # Expected bounds measured directly against the pinned whoosh oracle
    # (whoosh's inclusive last-microsecond ceiling, expressed here in this
    # library's half-open exclusive-upper form).
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.astimezone(UTC)
    assert r.hi == expected_hi.astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_range_joint_disambiguation_date_only_field(reg: FieldRegistry) -> None:
    # The date_only sibling cell: "created" collapses to UTC calendar days,
    # but the joint reading of the two bounds must happen first.
    r = dparse("created:[dec to feb]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2025, 12, 1, tzinfo=UTC)
    assert r.hi == datetime(2026, 3, 1, tzinfo=UTC)


def test_range_both_sides_are_periods_cannot_combine(reg: FieldRegistry) -> None:
    # Both sides resolve to an already-disambiguated timespan ("previous
    # week"/"previous month"), which can't be nested inside another
    # timespan(): exercises range_to_node's non-combine branch for both
    # raw_start and raw_end.
    r = dparse("added:['previous month' TO 'this month']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)


@pytest.mark.parametrize(
    ("query", "expected_lo"),
    [
        # A year plus a colon-separated time is ambiguous. The separated-date
        # grammar alternative accepts ":" as a separator and is tried first,
        # so this reads as a calendar day, not a time of day. See
        # DIVERGENCES.md entry 21.
        # Berlin is UTC+1 in December, so the day starts at 23:00 the day before.
        pytest.param(
            "added:'2020 12:30'", datetime(2020, 12, 29, 23, 0), id="year-plus-time-is-a-date"
        ),
        # A time the separated-date alternative cannot match still reads as a
        # time on every day of the year, matching whoosh.
        pytest.param(
            "added:'2020 5pm'", datetime(2020, 1, 1, 16, 0), id="year-plus-meridiem-is-a-time"
        ),
    ],
)
def test_year_followed_by_time(reg: FieldRegistry, query: str, expected_lo: datetime) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:9999", id="max-year-single"),
        pytest.param("created:99991231", id="max-year-compact-day"),
        pytest.param("created:'dec 9999'", id="max-year-named-month"),
        pytest.param("created:[2020 TO 9999]", id="max-year-range-end"),
        pytest.param("created:0000", id="zero-year-single"),
        pytest.param("created:00000101", id="zero-year-compact-day"),
        pytest.param("created:[0000 TO 2020]", id="zero-year-range-start"),
    ],
)
def test_years_outside_the_representable_range_diagnose(reg: FieldRegistry, query: str) -> None:
    # Year 0 has no datetime representation, and year 9999's exclusive
    # ceiling would land past datetime.max. Both blow up in the arithmetic
    # rather than the grammar, so they need catching: parsing reports bad
    # input through diagnostics, it does not raise.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


@pytest.mark.parametrize(
    ("query", "bad_bound"),
    [
        pytest.param("created:[2020 TO 9999]", "9999", id="end-bound-overflow-named-correctly"),
        pytest.param("created:[0000 TO 2020]", "0000", id="start-bound-underflow-named-correctly"),
    ],
)
def test_range_out_of_range_diagnostic_names_the_failing_bound(
    reg: FieldRegistry, query: str, bad_bound: str
) -> None:
    # Regression: range_to_node's exception handler used to always report
    # `node.start or node.end`, so a range failing on its END bound (e.g.
    # year 9999's exclusive ceiling overflowing datetime.max) incorrectly
    # named the START bound in the diagnostic message.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert bad_bound in res.diagnostics[0].message
    assert res.diagnostics[0].field == FieldRef("created")
    assert res.diagnostics[0].raw_value == bad_bound


@pytest.mark.parametrize(
    ("query", "bad_bound"),
    [
        pytest.param("added:[2020 to 0001]", "0001", id="failing-end-swapped-to-lo"),
        pytest.param("added:[9999 to 2020]", "9999", id="failing-start-swapped-to-hi"),
        pytest.param("added:[now to 0001]", "0001", id="exact-start-swapped-with-bad-end"),
        pytest.param(
            "added:['9999-12-31' to 9999]",
            "9999",
            id="innocent-max-ceiling-start-not-blamed",
            # No swap here: the failing value is genuinely the end bound's
            # exclusive ceiling. The start bound's own ceiling also sits at
            # datetime.max, so a probe applying hi-side arithmetic to both
            # bounds indiscriminately would blame the innocent start; the
            # side-aware rule keeps the positional attribution because the
            # positional bound fails its own side's arithmetic too.
        ),
        pytest.param(
            "added:['9999-12-31 23:59:59.999999' to 9999]",
            "9999",
            id="exact-max-instant-start-not-blamed",
        ),
        pytest.param(
            "added:[9999 to 0001]",
            "9999",
            # The first failing site is the hi-side exclusive ceiling, and
            # its overflowing value genuinely came from the start bound's
            # text (the swap put 9999 at hi): the re-attribution branch
            # fires because 9999 fails hi-side arithmetic on its own while
            # 0001 (which only fails as a lo) survives it.
            id="both-bad-names-first-failing-value",
        ),
        pytest.param(
            "added:[0001 to 9999]",
            "9999",
            # Forward sibling of the case above: same first-failing-site
            # answer whichever side each bad year was typed on.
            id="both-bad-forward-names-first-failing-value",
        ),
        pytest.param(
            "created:{'9999-12-31' TO '9999-12-31 23:59:59.999999'}",
            "9999-12-31 23:59:59.999999",
            id="exclusive-bracket-exact-end-stays-blamed",
            # No swap. The end bound is exact, but the user's exclusive
            # bracket makes date_only ceil it up a day, overflowing: the
            # probe must model that same typed exclusivity, or the end
            # bound looks healthy and the innocent max-ceiling start gets
            # blamed instead.
        ),
    ],
)
def test_range_diagnostic_names_failing_bound_after_joint_swap(
    reg: FieldRegistry, query: str, bad_bound: str
) -> None:
    # The joint-disambiguation step swaps a backwards range with two
    # explicit years (whoosh's own range heuristic), after which the lo/hi
    # conversion failures no longer line up with the bounds' textual
    # positions: the year-1 end bound becomes lo (its Berlin-to-UTC
    # conversion underflows datetime.min), the year-9999 start bound
    # becomes hi (its exclusive ceiling overflows datetime.max). The
    # diagnostic must name the bound whose VALUE failed, not whichever
    # bound sat at that side of the brackets.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].raw_value == bad_bound
    assert bad_bound in res.diagnostics[0].message


def test_range_bad_start_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:[notadate TO 2020]", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    assert res.diagnostics[0].raw_value == "notadate"


def test_range_bad_end_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:[2020 TO notadate]", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    # The end bound is the offending text here, not the start bound: the
    # diagnostic's raw_value must track whichever bound the message names.
    assert res.diagnostics[0].raw_value == "notadate"


# --- torange Combo grammar (free "X to Y" text inside one field value) -----


def test_torange_combo_basic(reg: FieldRegistry) -> None:
    r = dparse("added:'3pm to 5pm'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert lo_local.hour == 15


def test_torange_combo_first_side_fails_whole_thing_fails(reg: FieldRegistry) -> None:
    # Combo.parse's `if at is None: return (None, None)`: the first bundle
    # doesn't match at all, so neither the "to" combo nor any dmy/bundle
    # alternative in the outer Choice matches either.
    res = dparse("added:'notadate to 2020'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_sequence_fill_in_type_error_rejected(reg: FieldRegistry) -> None:
    # Combining day=31 with month=february (2020, a leap year -> Feb has 29
    # days) inside the dmy Sequence raises TimeError from fill_in()'s
    # adatetime(**args) construction (parser/dateparse.py:233-236); Sequence
    # swallows it and reports failure, so no dmy/mdy/... alternative
    # matches and the whole field value is an unparseable date.
    res = dparse("created:'31 february 2020'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_bag_time_and_date_both_match_in_one_value(reg: FieldRegistry) -> None:
    # English.setup's `self.datetime = Bag((self.time, self.dmy))`: both
    # sub-elements matching in the same value hits the `onceper and
    # all(seen)` early-break (parser/dateparse.py:416-417).
    r = dparse("added:'3pm 4 august 2020'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.year, lo_local.month, lo_local.day, lo_local.hour) == (2020, 8, 4, 15)


def test_dateparser_parse_method_direct() -> None:
    # DateParser.parse() (distinct from .date_from(), which is what
    # DateParserPlugin actually calls) is unused by whoosh-compat's own
    # code: same as upstream whoosh, where it's likewise dead from the
    # plugin's perspective (only .date_from() is called there too). It's
    # still a documented, non-trivial public method of the grammar classes,
    # so it's covered here directly rather than deleted.
    parser = English()
    result, pos = parser.parse("2020", datetime(2026, 1, 1))
    # Unlike DateParserPlugin (which turns a period into an exclusive
    # ceil+1us upper bound), DateParser.parse() returns times.py's raw
    # disambiguated() result: an inclusive floor/ceil timespan.
    assert result == timespan(
        datetime(2020, 1, 1, 0, 0, 0, 0), datetime(2020, 12, 31, 23, 59, 59, 999999)
    )
    assert pos == 4


# --- Low-level grammar-class internals (unreachable via whoosh_compat.parse
# because DateParserPlugin only ever calls .date_from(), never .parse(),
# and never triggers debug tracing or repr()) ------------------------------


def test_dateparse_internals_repr_and_debug() -> None:
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
    regex = dp.Regex(r"(?P<year>[0-9]{4})", lambda p, dt: dt)
    result = regex.date_from("2020")
    assert result is not None


def test_regex_props_to_date_default_uses_all_units() -> None:
    regex = dp.Regex(r"(?P<year>[0-9]{4})")
    result, _ = regex.parse("2020", datetime(2026, 1, 1))
    assert result == adatetime(year=2020)


def test_dateparser_parse_exact_datetime_skips_disambiguation() -> None:
    # DateParser.parse()'s `if isinstance(d, (adatetime, timespan))` branch
    # is skipped when the grammar already resolves to an exact datetime
    # ("now"), rather than an ambiguous adatetime/timespan.
    parser = English()
    dt = datetime(2026, 1, 1, 10, 30)
    result, _ = parser.parse("now", dt)
    assert result == dt


def test_dateparser_date_from_defaults_and_toend_false() -> None:
    parser = English()
    # basedate=None -> falls back to datetime.utcnow() internally.
    result = parser.date_from("2020")
    assert result is not None

    # toend=False: trailing garbage after a valid match is tolerated.
    result2 = parser.date_from("2020 garbage", datetime(2026, 1, 1), toend=False)
    assert result2 is not None


def test_daterangesyntaxnode_and_dateerrornode_r() -> None:
    node = DateRangeSyntaxNode("added", datetime(2020, 1, 1), datetime(2020, 1, 2), True, False)
    assert node.r() == f"DateRange {datetime(2020, 1, 1)!r}-{datetime(2020, 1, 2)!r}"

    diag = Diagnostic(message="bad", kind=DiagnosticKind.BAD_DATE, startchar=0, endchar=1)
    err = DateErrorNode(diag)
    assert err.r() == "DateError 'bad'"


def test_bare_date_keyword_under_query_parser_default_date_field(reg: FieldRegistry) -> None:
    # do_dates() only date-parses a node with a fieldname. An unfielded
    # term's fieldname is None until QueryParser.fieldname's default-field
    # fallback fills it in; do_dates() used to skip that fallback, so a bare
    # date keyword under a single-field QueryParser with a date default
    # field fell through as an ordinary text term instead of a date range.
    # MultifieldParser (what whoosh_compat.parse() always builds) has no
    # single default fieldname (it passes None and expands unfielded terms
    # into a per-field OR instead), so this path is only reachable through
    # the QueryParser API directly.
    parser = QueryParser("created", reg)
    parser.add_plugin(DateParserPlugin(BASE, BERLIN))
    node = ast.normalize(parser.parse("yesterday"))
    assert node == ast.DateRange(
        field=FieldRef("created"),
        lo=datetime(2026, 8, 3, tzinfo=UTC),
        hi=datetime(2026, 8, 4, tzinfo=UTC),
        incl_lo=True,
        incl_hi=False,
    )


def test_naive_basedate_rejected_by_plugin_construction() -> None:
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        DateParserPlugin(naive, BERLIN)


def test_naive_basedate_rejected_via_parse(reg: FieldRegistry) -> None:
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        wc.parse(
            "created:today", registry=reg, default_fields=["content"], tz=BERLIN, basedate=naive
        )


def test_naive_basedate_rejected_even_without_date_fields() -> None:
    # The rejection is parse()'s own contract, not a side effect of
    # DateParserPlugin construction: a registry with no DATE/DATETIME
    # fields (where the plugin is never attached) must reject the same
    # host misconfiguration instead of silently accepting and ignoring
    # it, so whether bad config raises cannot depend on unrelated
    # registry contents.
    text_only = FieldRegistry([FieldSpec("content", FieldKind.TEXT)])
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        wc.parse("foo", registry=text_only, default_fields=["content"], basedate=naive)


def test_aware_basedate_with_same_wall_clock_still_works(reg: FieldRegistry) -> None:
    # Pins the fixed behavior without manipulating the process timezone
    # (time.tzset() isn't available on Windows): an aware basedate resolves
    # against its own tzinfo regardless of the machine's local zone, since
    # "aware" is now a hard requirement rather than something silently
    # reinterpreted in the host's local zone.
    aware = datetime(2026, 8, 4, 23, 0, tzinfo=BERLIN)
    r = wc.parse(
        "added:yesterday", registry=reg, default_fields=["content"], tz=BERLIN, basedate=aware
    ).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_do_dates_leaves_non_range_non_text_date_field_node_untouched(reg: FieldRegistry) -> None:
    # do_dates()'s trailing `else: continue` (has_fieldname but neither a
    # RangeNode nor has_text) has no real-world producer in the current
    # plugin set: FieldnameNode/RangeNode/TextNode are the only
    # has_fieldname=True node types, and FieldnameNode never survives as a
    # leaf. Exercised directly against a minimal stub node.
    class FieldOnlyNode(syntax.SyntaxNode):
        has_fieldname = True

        def __init__(self, fieldname: str) -> None:
            self.fieldname = fieldname

    plugin = DateParserPlugin(BASE, BERLIN)

    class StubParser:
        registry = reg

    group = syntax.AndGroup([FieldOnlyNode("added")])
    result = plugin.do_dates(StubParser(), group)
    assert result[0] is group[0]  # untouched, not replaced


def test_range_exclusive_brackets_honored_for_exact_bounds(reg: FieldRegistry) -> None:
    # DIVERGENCES.md entry 44: an exact bound ("now", a concrete instant)
    # keeps the bracket exclusivity the user typed, where real whoosh's
    # DateRangeNode silently drops the flags (always inclusive-both, the
    # same plumbing oversight class as entry 3's boost drop). The
    # ambiguous-bound counterpart directly below pins the inverse rule.
    r = dparse("added:{now TO now}", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == BASE.astimezone(UTC)
    assert r.hi == BASE.astimezone(UTC)
    assert r.incl_lo is False
    assert r.incl_hi is False
    inclusive = dparse("added:[now TO now]", reg).ast
    assert isinstance(inclusive, ast.DateRange)
    assert inclusive.incl_lo is True
    assert inclusive.incl_hi is True


def test_range_exclusive_bounds_ignored_for_ambiguous_bounds(reg: FieldRegistry) -> None:
    # "2020"/"2021" are ambiguous (year-only) bounds, not exact instants, so
    # range_to_node forces incl_lo=True regardless of the query's "{" exclusive
    # marker (parser/dateparse.py:999-1000): only incl_hi is
    # forced-exclusive (it already carries the "+1us" half-open adjustment).
    r = dparse("added:{2020 TO 2021}", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.incl_lo is True
    assert r.incl_hi is False
