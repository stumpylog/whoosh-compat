"""Date / numeric / term range emission."""

from datetime import UTC
from datetime import datetime

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError

from .conftest import emit_ast
from .conftest import search_ids


def utc(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


@pytest.mark.parametrize(
    "lo, hi, incl_lo, incl_hi, expected",
    [
        pytest.param(
            utc(2020, 1, 1), utc(2021, 1, 1), True, False, [1, 4], id="both-bounds-incl-lo-excl-hi"
        ),
        pytest.param(utc(2020, 1, 1), None, True, True, [1, 4], id="open-upper-bound"),
        pytest.param(None, utc(2019, 1, 1), True, False, [3], id="open-lower-bound"),
        # doc 2 is exactly 2019-06-01; excluding the lower bound drops it.
        pytest.param(
            utc(2019, 6, 1),
            utc(2020, 1, 1),
            False,
            False,
            [],
            id="exclusive-lower-drops-boundary-doc",
        ),
        pytest.param(
            utc(2019, 6, 1),
            utc(2020, 1, 1),
            True,
            False,
            [2],
            id="inclusive-lower-keeps-boundary-doc",
        ),
    ],
)
def test_date_range(tindex, ereg, lo, hi, incl_lo, incl_hi, expected):
    node = ast.DateRange(field="created", lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_date_range_parsed(tindex, ereg, parse):
    q = emit_ast(parse("created:[2020-01-01 TO 2020-12-31]"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


@pytest.mark.parametrize(
    "lo, hi, incl_lo, incl_hi, expected",
    [
        pytest.param(101, 103, True, False, [2, 3], id="excl-hi"),
        pytest.param(101, 103, True, True, [2, 3, 4], id="incl-hi"),
        pytest.param(102, None, True, True, [3, 4], id="open-upper"),
    ],
)
def test_numeric_range(tindex, ereg, lo, hi, incl_lo, incl_hi, expected):
    node = ast.NumericRange(field="asn", lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_text_range_raises(tindex, ereg):
    node = ast.TermRange(field="title", lo="a", hi="z", incl_lo=True, incl_hi=True)
    with pytest.raises(UnsupportedQueryError, match="text ranges"):
        emit_ast(node, tindex, ereg)


def test_date_range_naive_bounds_pass_through(tindex, ereg):
    # _to_naive_utc()'s passthrough branch: bounds that are already naive
    # (no tzinfo) are used as-is rather than converted.
    node = ast.DateRange(
        field="created",
        lo=datetime(2020, 1, 1),
        hi=datetime(2021, 1, 1),
        incl_lo=True,
        incl_hi=False,
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_range_query_needs_at_least_one_bound(tindex, ereg):
    node = ast.NumericRange(field="asn", lo=None, hi=None, incl_lo=True, incl_hi=True)
    with pytest.raises(QueryEmitError, match="at least one bound"):
        emit_ast(node, tindex, ereg)
