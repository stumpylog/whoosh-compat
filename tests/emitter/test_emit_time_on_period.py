"""Real searches for date values that pair a time of day with a whole
period (DIVERGENCES.md entry 62): such a value searches nothing, and
emit() refuses it with the parse-time diagnostic, while a time on a
specific day still finds its document.
"""

import pytest

from whoosh_compat import parse as _parse
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


def _assert_bad_date_at_emit(query: str, tindex: TIndex, ereg: FieldRegistry) -> None:
    """``query`` diagnoses BAD_DATE at parse time, and emit() refuses it with
    that same kind of diagnostic.
    """
    result = _parse(query, registry=ereg, default_fields=["content"])
    assert [d.kind for d in result.diagnostics] == [DiagnosticKind.BAD_DATE]
    with pytest.raises(QueryError) as exc:
        emit_ast(result.ast, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.BAD_DATE


@pytest.mark.parametrize(
    "query",
    [
        # Used to search 2020-03-01 10:00 to 2020-03-31 10:01 and find doc 1.
        pytest.param("added:'march 2020 10:00'", id="month-name-and-year"),
        # Used to search the whole of 2020 pinned to 10:00 and find docs 1, 4.
        pytest.param('added:"2020 10:00"', id="bare-year"),
        pytest.param("added:march 2020 10:00", id="unquoted"),
    ],
)
def test_a_time_on_a_period_fails_at_emit(query: str, tindex: TIndex, ereg: FieldRegistry) -> None:
    _assert_bad_date_at_emit(query, tindex, ereg)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:'2020-03-15 10:00'", id="numeric-date"),
        pytest.param("added:'march 15 2020 10:00'", id="month-name-day-and-year"),
    ],
)
def test_a_time_on_a_named_day_still_finds_its_document(
    query: str, tindex: TIndex, ereg: FieldRegistry
) -> None:
    result = _parse(query, registry=ereg, default_fields=["content"])
    assert not result.diagnostics
    assert search_ids(tindex[0], emit_ast(result.ast, tindex, ereg)) == [1]


@pytest.mark.parametrize(
    "query",
    [
        # Used to search 10 March 2020 00:00-01:00: the clock read as a day
        # and an hour.
        pytest.param("added:'2020-03 10:00'", id="year-month-then-clock"),
        # Used to search 15 March 2020 10:00 and find doc 1.
        pytest.param("added:'2020-03-15:10:00'", id="colon-before-the-hour"),
        # Used to search all of 15 March 2020 and find doc 1.
        pytest.param("added:'2020:03:15'", id="colon-between-date-units"),
    ],
)
def test_a_misread_numeric_value_fails_at_emit(
    query: str, tindex: TIndex, ereg: FieldRegistry
) -> None:
    _assert_bad_date_at_emit(query, tindex, ereg)


def test_an_unquoted_fused_clock_after_a_year_month_keeps_the_month_and_the_term(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    """``added:2020-03 1000`` is March 2020 AND the term ``1000``: the date
    half alone finds doc 1, and the leftover term is ANDed on, so a term the
    document does not contain removes it while one it does contain keeps it.
    """

    def ids(query: str) -> list[int]:
        result = _parse(query, registry=ereg, default_fields=["content"])
        assert not result.diagnostics, result.diagnostics
        return search_ids(tindex[0], emit_ast(result.ast, tindex, ereg))

    assert ids("added:2020-03") == [1]
    assert ids("added:2020-03 invoice") == [1]
    assert ids("added:2020-03 1000") == []
