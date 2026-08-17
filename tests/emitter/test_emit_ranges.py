"""Date / numeric / term range emission."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime

import pytest

from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


def utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lo", "hi", "incl_lo", "incl_hi", "expected"),
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
def test_date_range(
    tindex: TIndex,
    ereg: FieldRegistry,
    lo: datetime | None,
    hi: datetime | None,
    incl_lo: bool,
    incl_hi: bool,
    expected: list[int],
) -> None:
    node = ast.DateRange(field=FieldRef("created"), lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_date_range_parsed(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    q = emit_ast(parse("created:[2020-01-01 TO 2020-12-31]"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


# -- date_only exclusive-upper-bound ceiling, result-level ------------------
#
# "created" (tests/emitter/conftest.py's DOCS) is date_only=True, with a
# document created on each of 2018-03-23 (id 3), 2019-03-01 (id 5),
# 2019-06-01 (id 2), 2020-03-15 (id 1), 2020-11-30 (id 4). Before the fix,
# any time-of-day precision on a bound truncated its exclusive upper bound
# down instead of up, silently emptying or shortening these ranges.


def test_date_only_time_bearing_single_value_matches_the_named_day(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # Before the fix: truncated to [2020-03-15T00:00Z, 2020-03-15T00:00Z),
    # an empty range, 0 hits.
    q = emit_ast(parse("created:'2020-03-15 15:30'"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_date_only_range_time_bearing_end_bound_includes_named_end_day(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # Before the fix: hi truncated down to 2020-03-15T00:00Z (incl_hi=False),
    # silently dropping doc 1 (created exactly on that day) from the range.
    q = emit_ast(parse("created:[2018-01-01 TO '2020-03-15 12:00']"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 5]


def test_date_only_range_time_bearing_start_bound_still_truncates_down(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # The lo bound already truncated down correctly; the ceiling fix must
    # not change this shape.
    q = emit_ast(parse("created:['2020-03-15 15:00' TO 2020-11-30]"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_date_only_same_day_range_times_on_both_ends_matches(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # Before the fix: both bounds truncated to the same day's midnight with
    # incl_hi=False, an empty [midnight, midnight) range, 0 hits, even
    # though the query explicitly includes midnight.
    q = emit_ast(parse("created:['2020-03-15 00:00' TO '2020-03-15 18:00']"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_date_only_noon_and_3pm_consistently_match_their_day(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # Regression for the noon-vs-3pm inconsistency the issue calls out:
    # both a degenerate exact-instant value and an hour-precision period
    # value must match a document created that day on a date_only field.
    # basedate is pinned to doc 1's created day (2020-03-15) so "noon"/"3pm"
    # resolve onto it.
    basedate = datetime(2020, 3, 15, 10, 0, tzinfo=UTC)
    for query in ("created:noon", "created:'3pm'"):
        res = _parse(query, registry=ereg, default_fields=["content"], tz=UTC, basedate=basedate)
        q = emit_ast(res.ast, tindex, ereg)
        assert search_ids(tindex[0], q) == [1], query


@pytest.mark.parametrize(
    ("lo", "hi", "incl_lo", "incl_hi", "expected"),
    [
        pytest.param(101, 103, True, False, [2, 3], id="excl-hi"),
        pytest.param(101, 103, True, True, [2, 3, 4], id="incl-hi"),
        pytest.param(102, None, True, True, [3, 4], id="open-upper"),
    ],
)
def test_numeric_range(
    tindex: TIndex,
    ereg: FieldRegistry,
    lo: int | None,
    hi: int | None,
    incl_lo: bool,
    incl_hi: bool,
    expected: list[int],
) -> None:
    node = ast.NumericRange(field=FieldRef("asn"), lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_text_range_raises(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.TermRange(field=FieldRef("title"), lo="a", hi="z", incl_lo=True, incl_hi=True)
    with pytest.raises(UnsupportedQueryError, match="text ranges"):
        emit_ast(node, tindex, ereg)


def test_text_range_pins_the_two_part_host_contract(tindex: TIndex, ereg: FieldRegistry) -> None:
    # DIVERGENCES.md entry 5 / README's "host contract" section: an empty
    # diagnostics list is not, on its own, proof that emit() will succeed.
    # "title:[a TO b]" is a text-field range: it parses clean (no
    # diagnostics, no ErrorLeaf) and only fails once emit() is called, with
    # a typed UnsupportedQueryError rather than a bare exception.
    result = _parse("title:[a TO b]", registry=ereg, default_fields=["content"])
    assert result.diagnostics == ()
    with pytest.raises(UnsupportedQueryError, match="text ranges"):
        emit_ast(result.ast, tindex, ereg)


def test_date_range_naive_bounds_pass_through(tindex: TIndex, ereg: FieldRegistry) -> None:
    # _to_naive_utc()'s passthrough branch: bounds that are already naive
    # (no tzinfo) are used as-is rather than converted.
    node = ast.DateRange(
        field=FieldRef("created"),
        lo=datetime(2020, 1, 1),
        hi=datetime(2021, 1, 1),
        incl_lo=True,
        incl_hi=False,
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4]


def test_range_open_on_both_sides_means_field_exists(tindex: TIndex, ereg: FieldRegistry) -> None:
    # A range with no bounds at all ("asn:[TO]") used to raise QueryEmitError
    # (the ast.NumericRange/DateRange constructors don't forbid this shape,
    # and the grammar-aware property fuzzer found it parses cleanly, see
    # tests/emitter/test_hypothesis_e2e.py): semantically it just means "asn
    # has some value", exactly what visit_every's `field:*` already answers,
    # so _range_query now delegates to the same _exists_query helper instead
    # of erroring on a query nothing told the caller was invalid.
    node = ast.NumericRange(field=FieldRef("asn"), lo=None, hi=None, incl_lo=True, incl_hi=True)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 2, 3, 4, 5]


# -- a hand-built (not just parsed) AST node with a bad bound
# -- must never let a raw exception escape emit(); parsed input can't reach
# -- these shapes (the parser always diagnoses a bad numeric bound), but a
# -- host constructing ast.NumericRange/ast.DateRange directly can.


def test_numeric_range_non_numeric_bound_raises_query_emit_error(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    node = ast.NumericRange(
        field=FieldRef("asn"),
        lo="notanumber",  # type: ignore[arg-type]
        hi=None,
        incl_lo=True,
        incl_hi=True,
    )
    with pytest.raises(QueryEmitError, match="notanumber"):
        emit_ast(node, tindex, ereg)


def test_date_range_non_datetime_bound_raises_query_emit_error(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    node = ast.DateRange(
        field=FieldRef("created"),
        lo="not-a-datetime",  # type: ignore[arg-type]
        hi=None,
        incl_lo=True,
        incl_hi=True,
    )
    with pytest.raises(QueryEmitError, match="not-a-datetime"):
        emit_ast(node, tindex, ereg)
