"""The date grammar is built once per process, not once per ``parse()``.

``English()`` compiles roughly 70 regexes, which is about 30% of ``parse()``'s
wall time when it is rebuilt for every query. These tests pin the sharing and,
separately, guard the property that makes sharing safe: a built grammar is
read-only, so concurrent parses cannot interfere through it.
"""

from __future__ import annotations

import threading
from datetime import UTC
from datetime import datetime

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser.dateparse import DateParser
from whoosh_compat.parser.dateparse import DateParserPlugin
from whoosh_compat.parser.dateparse import English

BASE = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def datereg() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT),
            FieldSpec("created", FieldKind.DATE, date_only=True),
            FieldSpec("added", FieldKind.DATETIME),
        ]
    )


def _plugin() -> DateParserPlugin:
    return DateParserPlugin(BASE, UTC)


def test_plugins_share_one_grammar() -> None:
    """Two plugins built with no explicit dateparser share one grammar."""
    assert _plugin().dateparser is _plugin().dateparser


def test_repeated_parses_share_one_grammar(datereg: FieldRegistry) -> None:
    """``parse()`` does not rebuild the grammar per call.

    Recorded by identity rather than by timing so the test cannot go quiet
    on a slow or noisy machine.
    """
    seen: list[int] = []
    original = English.__init__

    # ``__init__`` is inherited from DateParser, so it types as
    # ``Callable[[DateParser], None]`` rather than over English.
    def recording_init(self: DateParser) -> None:
        original(self)
        seen.append(id(self))

    English.__init__ = recording_init  # type: ignore[method-assign]
    try:
        for _ in range(5):
            result = wc.parse(
                "created:2020",
                registry=datereg,
                default_fields=["content"],
                basedate=BASE,
                tz=UTC,
            )
            # Without this the test also passes if the date plugin stops
            # being attached at all: no plugin means no grammar built means
            # ``seen == []``, which would satisfy the assertion below while
            # the date path is silently gone.
            assert isinstance(result.ast, ast.DateRange), result.ast
    finally:
        English.__init__ = original  # type: ignore[method-assign]

    assert len(seen) <= 1, f"grammar rebuilt {len(seen)} times across 5 parses"


def test_explicit_dateparser_is_still_honored() -> None:
    """The ``dateparser=`` escape hatch keeps overriding the shared default."""
    mine = English()
    assert _plugin().dateparser is not mine
    assert DateParserPlugin(BASE, UTC, dateparser=mine).dateparser is mine


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:2020", id="bare-year"),
        pytest.param("created:202003", id="compact-year-month"),
        pytest.param("created:today", id="keyword"),
        pytest.param("added:yesterday", id="keyword-datetime"),
        pytest.param("created:2020-01-01", id="separated"),
        pytest.param("added:'2026-08-04T10:30:00'", id="quoted-rfc3339"),
        pytest.param("added:20260804103000", id="compact-datetime"),
        pytest.param("created:'previous quarter'", id="quoted-period"),
        pytest.param("created:[2020 to 2021]", id="range"),
        pytest.param("added:now-2w", id="relative"),
    ],
)
def test_shared_grammar_parses_each_shape(datereg: FieldRegistry, query: str) -> None:
    """Sharing does not change what any date shape parses to.

    Unquoted RFC3339 (``added:2026-08-04T10:30:00``) is deliberately absent:
    its colons tokenize apart and it is a ``BAD_DATE`` today, quoted or
    compact being the forms that read. That is pre-existing behavior, not
    something grammar sharing touches.
    """
    result = wc.parse(query, registry=datereg, default_fields=["content"], basedate=BASE, tz=UTC)
    assert result.diagnostics == ()


def test_concurrent_parses_agree_with_serial(datereg: FieldRegistry) -> None:
    """Guard on the property that makes sharing safe.

    A built grammar is read-only: ``parse``/``date_from`` never assign to
    ``self`` (per-call state lives on the plugin and on freshly built
    ``Props``), so threads sharing one grammar cannot interfere. This passes
    both before and after the grammar is shared: it is a regression guard on
    that invariant, not a test that drives the change.
    """
    queries = [
        "created:2020",
        "created:today",
        "added:yesterday",
        "created:2020-01-01",
        "added:2026-08-04T10:30:00",
        "created:'previous quarter'",
        "created:[2020 to 2021]",
        "added:now-2w",
    ]

    def run(query: str) -> str:
        return repr(
            wc.parse(
                query,
                registry=datereg,
                default_fields=["content"],
                basedate=BASE,
                tz=UTC,
            ).ast
        )

    expected = {q: run(q) for q in queries}

    results: dict[int, list[tuple[str, str]]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[slot] = [(q, run(q)) for _ in range(20) for q in queries]
        except BaseException as exc:  # noqa: BLE001  (re-raised in the main thread)
            errors.append(exc)

    # daemon=True so a hung worker fails this test loudly rather than
    # blocking interpreter shutdown and turning into a CI job timeout.
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"worker raised: {errors[0]!r}"
    assert len(results) == 8
    for slot, pairs in results.items():
        for query, got in pairs:
            assert got == expected[query], f"thread {slot} diverged on {query!r}"
