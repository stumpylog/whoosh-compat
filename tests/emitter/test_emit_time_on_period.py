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
    result = _parse(query, registry=ereg, default_fields=["content"])
    assert [d.kind for d in result.diagnostics] == [DiagnosticKind.BAD_DATE]
    with pytest.raises(QueryError) as exc:
        emit_ast(result.ast, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.BAD_DATE


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
