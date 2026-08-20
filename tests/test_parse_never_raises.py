"""``parse()`` raises nothing but ``QueryParserError``, for any query text.

The invariant this file guards is the one the host contract rests on (see
CLAUDE.md): bad *user* input becomes ``Diagnostic``s, never an exception, and
the only exception a caller ever has to be ready for is ``QueryParserError``,
which means "a defect in this library" and which hosts route to a monitorable
500. Deliberately not a ``Diagnostic``: an unknown internal failure reported
as a 400 would blame the user for a library bug and hide it from monitoring.
"""

from __future__ import annotations

from datetime import UTC

import pytest
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

# What the widening buys is *breadth*, not depth: ``max_leaves`` bounds how
# many structural pieces st.recursive draws, so raising the shared strategy's
# committed default (8, kept modest so the differential/emitter suites that
# draw from it stay cheap) mostly buys wider composition of the grammar --
# more feature combinations per query, awkward leaves in more structural
# positions -- for ~2s. It does *not* approach the depths this backstop
# exists for: measured over 300 draws, max_leaves=40 tops out around paren
# depth 7 (vs. 4 at the default), against a cap of 200 and a compounded
# nesting shape that needs ~1000 levels before it recurses out. Depth is
# covered by test_compounded_nesting_becomes_query_parser_error below, which
# constructs that shape directly; raising max_leaves further would only cost
# runtime without ever reaching it.
_WIDE_QUERY_TEXT = query_text(max_leaves=40)


@given(q=_WIDE_QUERY_TEXT)
@settings(max_examples=500, deadline=None)
def test_parse_raises_nothing_but_query_parser_error(q: str) -> None:
    """No generated query escapes ``parse()`` as an exception.

    ``QueryParserError`` would be the one *permitted* escape (it means a
    library defect, never bad user input), but the assertion here is the
    stronger one: over the whole generated grammar the backstop must not fire
    at all. Merely tolerating it would let this test keep passing while a
    regression converted every query in the space into a 500, which is
    exactly the "guard papering over a real bug" mode the backstop is most at
    risk of enabling.
    """
    try:
        parse(q, registry=REGISTRY, default_fields=["content"], tz=UTC)
    except QueryParserError as exc:
        raise AssertionError(
            f"backstop fired for a generated query: {q!r} (cause: {exc.__cause__!r})"
        ) from exc


@pytest.mark.parametrize(
    "q",
    [
        " ANDMAYBE ".join(["a"] * 1000),
        " ANDNOT ".join(["a"] * 1000),
        'added:"previous week 3pm"',
        'added:"noon to now"',
    ],
)
def test_known_crashers_stay_fixed(q: str) -> None:
    """Regression anchors for the three escape routes found in review.

    Each of these once escaped ``parse()`` as an uncaught exception and is
    now fixed at its source, so they must parse without the backstop being
    involved at all.
    """
    parse(q, registry=REGISTRY, default_fields=["content"], tz=UTC)


def test_compounded_nesting_becomes_query_parser_error() -> None:
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
        parse(q, registry=REGISTRY, default_fields=["content"], tz=UTC)


def test_backstop_wraps_an_unexpected_filter_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An arbitrary defect inside the pipeline surfaces as QueryParserError,
    with the original exception preserved as ``__cause__`` so the real bug is
    still diagnosable from the traceback.
    """
    from whoosh_compat.parser import plugins

    def boom(self: object, parser: object, group: object) -> object:
        raise ZeroDivisionError("synthetic filter defect")

    monkeypatch.setattr(plugins.GroupPlugin, "do_groups", boom, raising=True)

    with pytest.raises(QueryParserError) as excinfo:
        parse("a OR b", registry=REGISTRY, default_fields=["content"], tz=UTC)

    assert isinstance(excinfo.value.__cause__, ZeroDivisionError)
