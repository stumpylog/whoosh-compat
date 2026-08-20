"""A time-of-day combined with a period keyword ("previous week", "previous
quarter") is rejected as a bad date, in both word orders.

A period keyword denotes a *span*, so a time-of-day on it names nothing.
Before this was rejected the two word orders disagreed: "previous week 3pm"
raised ``AttributeError`` out of ``parse()`` (the grammar's merging pass got a
``timespan`` where it expected a datetime-like object), while
"3pm previous week" silently dropped the time and returned the whole week.

Ordinary date keywords ("yesterday", "today", ...) do combine with a time
coherently, and must keep doing so in either order. So does a range with a
bare time-of-day lower bound and a concrete upper bound ("noon to now"),
which names a perfectly answerable span; whoosh crashes on that one, and this
fork resolves it instead (DIVERGENCES.md entry 51).
"""

from datetime import UTC
from datetime import datetime

import pytest

from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry
from whoosh_compat import FieldSpec
from whoosh_compat import ParseResult
from whoosh_compat import ast
from whoosh_compat import parse
from whoosh_compat.errors import DiagnosticKind


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: [t.lower()]),
            FieldSpec("added", FieldKind.DATETIME, fast=True),
        ]
    )


def _parse(registry: FieldRegistry, q: str, basedate: datetime | None = None) -> ParseResult:
    return parse(q, registry=registry, default_fields=["content"], tz=UTC, basedate=basedate)


def _date_range(registry: FieldRegistry, q: str, basedate: datetime | None = None) -> ast.DateRange:
    """Parse a query that must resolve cleanly to a single ``DateRange``.

    Every bound-checking test below shares the same two preconditions (no
    diagnostics, and a ``DateRange`` at the root), so they live here once
    rather than being restated per test. Narrowing the node type is also what
    lets the ``.lo``/``.hi``/``.incl_*`` reads below type-check: ``ast.Node``
    has no such attributes.
    """
    result = _parse(registry, q, basedate=basedate)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.DateRange)
    return result.ast


# An afternoon "now", so that "noon to now" reads forward within one day.
AFTERNOON = datetime(2026, 8, 19, 15, 30, tzinfo=UTC)
# A "now" before noon, where whoosh's overnight rule for time-only lower
# bounds (the same rule behind "[3pm to 10am]") carries the range past
# midnight instead of inverting it.
PREDAWN = datetime(2026, 8, 19, 1, 41, tzinfo=UTC)


@pytest.mark.parametrize(
    "q",
    [
        'added:"previous week 3pm"',
        'added:"previous quarter noon"',
        'added:"3pm previous week"',
        "added:[previous week 3pm TO 2021]",
        # The same values written without quotes, which the date grammar now
        # accepts (see test_parser_date_phrases.py). Quoting is a spelling,
        # not a semantic: the rejection cannot depend on it, or the unquoted
        # spelling would silently answer a question the quoted one refuses.
        "added:previous week 3pm",
        "added:previous quarter noon",
        "added:3pm previous week",
    ],
)
def test_period_keyword_with_a_time_is_a_bad_date(registry: FieldRegistry, q: str) -> None:
    """A period keyword denotes a span, so a time-of-day on it is meaningless.

    Two of these previously raised AttributeError out of parse(); one silently
    ignored the time, giving the two word orders different results.
    """
    result = _parse(registry, q)
    assert [d.kind for d in result.diagnostics] == [DiagnosticKind.BAD_DATE]


def test_calendar_unit_keyword_still_takes_a_time(registry: FieldRegistry) -> None:
    """The counterweight to the rejection above: only the keywords resolving
    to a span refuse a time. "previous month" resolves to a calendar unit (an
    adatetime), which a time of day narrows coherently. The unquoted spelling
    must land on the same side of that line as the quoted one.

    The resolved bounds (the 15:00-16:00 hour of the first and last day of
    the previous month) are the inherited floor()/ceil() behavior of a
    month-precision adatetime with an hour filled in, pinned here only so
    that "unquoted matches quoted" cannot be satisfied by both spellings
    breaking together.
    """
    quoted = _date_range(registry, 'added:"previous month 3pm"', basedate=AFTERNOON)
    unquoted = _date_range(registry, "added:previous month 3pm", basedate=AFTERNOON)
    assert unquoted == quoted
    assert quoted.lo == datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    assert quoted.hi == datetime(2026, 7, 31, 16, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("q", "expected_hi", "expected_incl_hi"),
    [
        # The quoted "to" range takes the exclusive +1us upper bound; the
        # bracketed range keeps the inclusive bound the user typed. Each
        # spelling pins its own inclusivity rather than the test reading it
        # off the result, so a regression that flipped one spelling cannot
        # be absorbed by the other spelling's expectation.
        pytest.param(
            'added:"noon to now"',
            datetime(2026, 8, 19, 15, 30, 0, 1, tzinfo=UTC),
            False,
            id="quoted-to-range-exclusive-hi",
        ),
        pytest.param(
            "added:[noon TO now]",
            datetime(2026, 8, 19, 15, 30, tzinfo=UTC),
            True,
            id="bracketed-range-inclusive-hi",
        ),
    ],
)
def test_time_of_day_lower_bound_against_a_concrete_upper_bound_resolves(
    registry: FieldRegistry,
    q: str,
    expected_hi: datetime,
    expected_incl_hi: bool,
) -> None:
    """ "noon to now" names an answerable span, so it resolves rather than
    diagnosing. Whoosh crashes here with AttributeError (it calls ceil() on
    the already-concrete "now"); this fork does not reproduce that bug.

    Both spellings are pinned: the bracketed form is the one that used to
    crash without being listed in the task brief.
    """
    rng = _date_range(registry, q, basedate=AFTERNOON)
    assert rng.lo == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert rng.incl_lo is True
    assert rng.hi == expected_hi
    assert rng.incl_hi is expected_incl_hi
    assert rng.lo < rng.hi


@pytest.mark.parametrize("q", ['added:"noon to now"', "added:[noon TO now]"])
def test_time_of_day_lower_bound_before_noon_carries_past_midnight(
    registry: FieldRegistry, q: str
) -> None:
    """With a "now" earlier in the day than the lower bound, the range reads
    forward overnight rather than inverting -- whoosh's own rule for
    time-only bounds, which "[3pm to 10am]" already relies on.
    """
    rng = _date_range(registry, q, basedate=PREDAWN)
    assert rng.hi is not None  # a two-sided range; DateRange bounds are Optional
    assert rng.lo == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert rng.hi.date() == datetime(2026, 8, 20, tzinfo=UTC).date()
    assert rng.lo < rng.hi


def test_time_of_day_lower_bound_against_a_relative_upper_bound_resolves(
    registry: FieldRegistry,
) -> None:
    """The same shape with a "-1 week" offset upper bound, which also arrives
    already concrete and so hit the same crash.
    """
    rng = _date_range(registry, "added:[noon TO -1 week]", basedate=AFTERNOON)
    assert rng.lo == datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    assert rng.hi == datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    assert rng.lo < rng.hi


def test_characterize_predawn_relative_upper_bound_year_borrow(
    registry: FieldRegistry,
) -> None:
    """CHARACTERIZATION, not a semantics assertion: this records what the
    inherited heuristics in ``times.py``'s ``timespan.disambiguated``
    currently do, so that if you change that function this test tells you
    what moved. It does not claim the result is the right answer.

    With a "now" before noon, "noon" cannot take the end's month/day
    (12:00 > 01:41), so it takes the basedate's; that lands after the end,
    so upstream's year-borrowing branch pulls the start back a year and the
    range comes out roughly a year wide. Both branches are verbatim
    upstream code, but real whoosh crashes before reaching them on this
    input, so there is no oracle to say whether it is intended.
    See DIVERGENCES.md entry 51.
    """
    rng = _date_range(registry, "added:[noon TO -1 week]", basedate=PREDAWN)
    assert rng.lo == datetime(2025, 8, 19, 12, 0, tzinfo=UTC)
    assert rng.hi == datetime(2026, 8, 12, 1, 41, tzinfo=UTC)
    # Well-formed (not inverted) even though it is wider than a user
    # plausibly meant -- the property that actually matters here.
    assert rng.lo < rng.hi


@pytest.mark.parametrize("q", ['added:"3pm yesterday"', 'added:"yesterday 3pm"'])
def test_ordinary_date_keywords_still_take_a_time_in_either_order(
    registry: FieldRegistry, q: str
) -> None:
    """The guard on this whole change: an ordinary date keyword combines with
    a time coherently in BOTH word orders, and must keep doing so. Only the
    period keywords (which resolve to a span) are rejected.

    Asserts the full resolved datetimes against a pinned "now", not just the
    hour: rejecting a time on a period keyword must not shift the DATE that
    an ordinary keyword resolves to either.
    """
    rng = _date_range(registry, q, basedate=AFTERNOON)
    # "yesterday" relative to the pinned 2026-08-19 "now", at 15:00-16:00.
    assert rng.lo == datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    assert rng.hi == datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    assert rng.incl_lo is True
    assert rng.incl_hi is False


def test_bare_period_keyword_is_unaffected(registry: FieldRegistry) -> None:
    result = _parse(registry, 'added:"previous week"')
    assert not result.diagnostics
