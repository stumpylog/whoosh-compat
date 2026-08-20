"""A time-of-day combined with a period keyword ("previous week", "previous
quarter") is rejected as a bad date, in both word orders.

A period keyword denotes a *span*, so a time-of-day on it names nothing.
Before this was rejected the two word orders disagreed: "previous week 3pm"
raised ``AttributeError`` out of ``parse()`` (the grammar's merging pass got a
``timespan`` where it expected a datetime-like object), while
"3pm previous week" silently dropped the time and returned the whole week.

Ordinary date keywords ("yesterday", "today", ...) do combine with a time
coherently, and must keep doing so in either order.
"""

from datetime import UTC

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


def _parse(registry: FieldRegistry, q: str):
    return parse(q, registry=registry, default_fields=["content"], tz=UTC)


@pytest.mark.parametrize(
    "q",
    [
        'added:"previous week 3pm"',
        'added:"previous quarter noon"',
        'added:"3pm previous week"',
        'added:"noon to now"',
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
