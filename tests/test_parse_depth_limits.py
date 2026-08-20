"""Depth caps for hierarchy the parser constructs, from any source."""

from __future__ import annotations

from datetime import UTC

import pytest

from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry
from whoosh_compat import FieldSpec
from whoosh_compat import parse
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: [t.lower()])],
    )


@pytest.mark.parametrize("op", ["ANDMAYBE", "ANDNOT", "REQUIRE"])
def test_flat_operator_chain_reports_too_deep_instead_of_raising(
    registry: FieldRegistry,
    op: str,
) -> None:
    """A paren-free chain of non-merging operators builds one hierarchy level
    per operator. The bracket-stack cap cannot see it, so before this fix the
    recursive descent in do_operators raised RecursionError out of parse(),
    violating the never-raises invariant.
    """
    q = f" {op} ".join(["a"] * 1000)

    result = parse(q, registry=registry, default_fields=["content"], tz=UTC)

    kinds = [d.kind for d in result.diagnostics]
    assert DiagnosticKind.TOO_DEEP in kinds
    assert all(d.cause is Cause.INVALID_INPUT for d in result.diagnostics)


@pytest.mark.parametrize("op", ["AND", "OR"])
def test_merging_operators_are_not_capped(registry: FieldRegistry, op: str) -> None:
    """AND/OR merge into a single flat group, so they build no hierarchy and
    must not trip the cap however long the chain is.
    """
    q = f" {op} ".join(["a"] * 1000)

    result = parse(q, registry=registry, default_fields=["content"], tz=UTC)

    assert not result.diagnostics


def test_operator_chain_below_the_cap_still_parses(registry: FieldRegistry) -> None:
    result = parse(
        "a ANDNOT b ANDNOT c",
        registry=registry,
        default_fields=["content"],
        tz=UTC,
    )
    assert not result.diagnostics


def test_flat_prefix_nots_are_not_capped(registry: FieldRegistry) -> None:
    """Prefix NOT builds a NotGroup per operator, but as siblings: each wraps
    one node and none nests inside another, so 200+ of them add depth 1 in
    total. They are deliberately excluded from do_operators' count, which
    keys on InfixOperator rather than Operator. Widening that predicate to
    all Operators (NotGroup is a Wrapper, so merging is False) would make
    this query report TOO_DEEP, which is why this test exists.
    """
    q = " AND ".join(["NOT a"] * 250)

    result = parse(q, registry=registry, default_fields=["content"], tz=UTC)

    assert not result.diagnostics


@pytest.mark.parametrize(
    ("operands", "expect_too_deep"),
    [(200, False), (201, True)],
)
def test_andnot_chain_cap_boundary(
    registry: FieldRegistry,
    operands: int,
    expect_too_deep: bool,
) -> None:
    """The cap trips at _MAX_GROUP_NESTING_DEPTH (200) operators, i.e. 201
    operands, and one operator below that still parses clean.
    """
    q = " ANDNOT ".join(["a"] * operands)

    result = parse(q, registry=registry, default_fields=["content"], tz=UTC)

    kinds = [d.kind for d in result.diagnostics]
    assert (DiagnosticKind.TOO_DEEP in kinds) is expect_too_deep
    if not expect_too_deep:
        assert not result.diagnostics
