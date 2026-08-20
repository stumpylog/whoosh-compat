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
