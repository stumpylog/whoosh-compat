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


def _parse(registry: FieldRegistry, q: str, basedate: datetime | None = None):
    return parse(q, registry=registry, default_fields=["content"], tz=UTC, basedate=basedate)


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
    ],
)
def test_period_keyword_with_a_time_is_a_bad_date(registry: FieldRegistry, q: str) -> None:
    """A period keyword denotes a span, so a time-of-day on it is meaningless.

    Two of these previously raised AttributeError out of parse(); one silently
    ignored the time, giving the two word orders different results.
    """
    result = _parse(registry, q)
    assert [d.kind for d in result.diagnostics] == [DiagnosticKind.BAD_DATE]


@pytest.mark.parametrize("q", ['added:"noon to now"', "added:[noon TO now]"])
def test_time_of_day_lower_bound_against_a_concrete_upper_bound_resolves(
    registry: FieldRegistry, q: str
) -> None:
    """ "noon to now" names an answerable span, so it resolves rather than
    diagnosing. Whoosh crashes here with AttributeError (it calls ceil() on
    the already-concrete "now"); this fork does not reproduce that bug.

    Both spellings are pinned: the bracketed form is the one that used to
    crash without being listed in the task brief.
    """
    result = _parse(registry, q, basedate=AFTERNOON)
    assert not result.diagnostics
    assert result.ast.lo == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    # The quoted "to" range takes the exclusive +1us upper bound; the
    # bracketed range keeps the inclusive bound the user typed.
    if result.ast.incl_hi:
        assert result.ast.hi == AFTERNOON
    else:
        assert result.ast.hi == datetime(2026, 8, 19, 15, 30, 0, 1, tzinfo=UTC)
    assert result.ast.lo < result.ast.hi


@pytest.mark.parametrize("q", ['added:"noon to now"', "added:[noon TO now]"])
def test_time_of_day_lower_bound_before_noon_carries_past_midnight(
    registry: FieldRegistry, q: str
) -> None:
    """With a "now" earlier in the day than the lower bound, the range reads
    forward overnight rather than inverting -- whoosh's own rule for
    time-only bounds, which "[3pm to 10am]" already relies on.
    """
    result = _parse(registry, q, basedate=PREDAWN)
    assert not result.diagnostics
    assert result.ast.lo == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert result.ast.hi.date() == datetime(2026, 8, 20, tzinfo=UTC).date()
    assert result.ast.lo < result.ast.hi


def test_time_of_day_lower_bound_against_a_relative_upper_bound_resolves(
    registry: FieldRegistry,
) -> None:
    """The same shape with a "-1 week" offset upper bound, which also arrives
    already concrete and so hit the same crash.
    """
    result = _parse(registry, "added:[noon TO -1 week]", basedate=AFTERNOON)
    assert not result.diagnostics
    assert result.ast.lo == datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    assert result.ast.hi == datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    assert result.ast.lo < result.ast.hi


@pytest.mark.parametrize("q", ['added:"3pm yesterday"', 'added:"yesterday 3pm"'])
def test_ordinary_date_keywords_still_take_a_time_in_either_order(
    registry: FieldRegistry, q: str
) -> None:
    result = _parse(registry, q)
    assert not result.diagnostics
    assert result.ast.lo.hour == 15
    assert result.ast.hi.hour == 16


def test_bare_period_keyword_is_unaffected(registry: FieldRegistry) -> None:
    result = _parse(registry, 'added:"previous week"')
    assert not result.diagnostics
