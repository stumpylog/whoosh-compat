"""Date / numeric / term range emission."""

from datetime import datetime, timezone

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import UnsupportedQueryError

from .conftest import emit_ast, search_ids


def utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_date_range(tindex, ereg):
    node = ast.DateRange(
        field="created", lo=utc(2020, 1, 1), hi=utc(2021, 1, 1), incl_lo=True, incl_hi=False
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_date_open_upper(tindex, ereg):
    node = ast.DateRange(
        field="created", lo=utc(2020, 1, 1), hi=None, incl_lo=True, incl_hi=True
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_date_open_lower(tindex, ereg):
    node = ast.DateRange(
        field="created", lo=None, hi=utc(2019, 1, 1), incl_lo=True, incl_hi=False
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [3]


def test_date_exclusive_lower(tindex, ereg):
    # doc 2 is exactly 2019-06-01; excluding the lower bound drops it.
    node = ast.DateRange(
        field="created", lo=utc(2019, 6, 1), hi=utc(2020, 1, 1), incl_lo=False, incl_hi=False
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []
    node = ast.DateRange(
        field="created", lo=utc(2019, 6, 1), hi=utc(2020, 1, 1), incl_lo=True, incl_hi=False
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2]


def test_date_range_parsed(tindex, ereg, parse):
    q = emit_ast(parse("created:[2020-01-01 TO 2020-12-31]"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_numeric_range(tindex, ereg):
    node = ast.NumericRange(field="asn", lo=101, hi=103, incl_lo=True, incl_hi=False)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 3]


def test_numeric_range_inclusive(tindex, ereg):
    node = ast.NumericRange(field="asn", lo=101, hi=103, incl_lo=True, incl_hi=True)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 3, 4]


def test_numeric_range_open_upper(tindex, ereg):
    node = ast.NumericRange(field="asn", lo=102, hi=None, incl_lo=True, incl_hi=True)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [3, 4]


def test_text_range_raises(tindex, ereg):
    node = ast.TermRange(field="title", lo="a", hi="z", incl_lo=True, incl_hi=True)
    with pytest.raises(UnsupportedQueryError, match="text ranges"):
        emit_ast(node, tindex, ereg)
