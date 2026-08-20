"""``parse()`` raises nothing but ``QueryParserError``, for any query text.

The invariant this file guards is the one the host contract rests on (see
CLAUDE.md): bad *user* input becomes ``Diagnostic``s, never an exception, and
the only exception a caller ever has to be ready for is ``QueryParserError``,
which means "a defect in this library" and which hosts route to a monitorable
500. Deliberately not a ``Diagnostic``: an unknown internal failure reported
as a 400 would blame the user for a library bug and hide it from monitoring.
"""

from __future__ import annotations

import contextlib
from datetime import UTC

import pytest
from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings

from tests.differential.strategies import query_text
from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry
from whoosh_compat import FieldSpec
from whoosh_compat import parse
from whoosh_compat.errors import QueryParserError

REGISTRY = FieldRegistry(
    [
        FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: [t.lower()]),
        FieldSpec("added", FieldKind.DATETIME, fast=True),
        FieldSpec("asn", FieldKind.U64, fast=True),
    ],
)


@pytest.fixture
def registry() -> FieldRegistry:
    return REGISTRY


# ``query_text``'s committed default (max_leaves=8, kept modest so the shared
# strategy stays cheap for the differential/emitter suites that draw from it)
# generates trees far too shallow to reach the depth-cap and nesting paths
# this backstop exists for. Widened here only, not in the strategy itself.
_DEEP_QUERY_TEXT = query_text(max_leaves=40)


@given(q=_DEEP_QUERY_TEXT)
@settings(
    max_examples=500,
    deadline=None,
    # REGISTRY is a read-only, immutable module-level fixture shared across
    # examples on purpose; nothing here mutates parser-visible state.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_parse_raises_nothing_but_query_parser_error(q: str) -> None:
    # QueryParserError is the one documented escape (a library defect, never
    # bad user input); anything else propagating out of parse() fails the test.
    with contextlib.suppress(QueryParserError):
        parse(q, registry=REGISTRY, default_fields=["content"], tz=UTC)


@pytest.mark.parametrize(
    "q",
    [
        " ANDMAYBE ".join(["a"] * 1000),
        " ANDNOT ".join(["a"] * 1000),
        'added:"previous week 3pm"',
        'added:"noon to now"',
    ],
)
def test_known_crashers_stay_fixed(q: str, registry: FieldRegistry) -> None:
    """Regression anchors for the three escape routes found in review.

    Each of these once escaped ``parse()`` as an uncaught exception and is
    now fixed at its source, so they must parse without the backstop being
    involved at all.
    """
    parse(q, registry=registry, default_fields=["content"], tz=UTC)


def test_compounded_nesting_becomes_query_parser_error(registry: FieldRegistry) -> None:
    """The shape the depth caps structurally cannot see is the backstop's job.

    Both caps count within a single flat group, so groups that each stay
    under the cap still compound: 20 paren levels around a 50-operator
    ``ANDNOT`` chain apiece builds ~1000 ``AndNotGroup`` levels, and
    ``GroupNode.query()``/``BinaryGroup.query()`` recurse once per level.
    That is a long-standing hole no third cap closes (ARCHITECTURE.md, "what
    the caps do not cover"), so the designed answer is that the caller sees
    ``QueryParserError`` rather than a bare ``RecursionError``.
    """
    q = "z"
    for _ in range(20):
        q = "(" + q + " ANDNOT " + " ANDNOT ".join(["a"] * 50) + ")"

    with pytest.raises(QueryParserError):
        parse(q, registry=registry, default_fields=["content"], tz=UTC)


def test_backstop_wraps_an_unexpected_filter_failure(
    registry: FieldRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary defect inside the pipeline surfaces as QueryParserError,
    with the original exception preserved as ``__cause__`` so the real bug is
    still diagnosable from the traceback.
    """
    from whoosh_compat.parser import plugins

    def boom(self: object, parser: object, group: object) -> object:
        raise ZeroDivisionError("synthetic filter defect")

    monkeypatch.setattr(plugins.GroupPlugin, "do_groups", boom, raising=True)

    with pytest.raises(QueryParserError) as excinfo:
        parse("a OR b", registry=registry, default_fields=["content"], tz=UTC)

    assert isinstance(excinfo.value.__cause__, ZeroDivisionError)
