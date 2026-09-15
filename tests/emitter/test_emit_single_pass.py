"""``emit()`` normalizes and analyzes its input in one pass: one normalize
over the whole tree (``analyze()``'s own leading one), then one analysis
walk, whether or not a ``rewrite_leaf`` hook is passed. Counted, not timed:
a redundant whole-tree pass costs as much as the real one on a large
query, so the count is what matters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import search_ids


def _count_normalize_passes(monkeypatch: pytest.MonkeyPatch) -> list[ast.Node]:
    """Record the root of every whole-tree normalize pass from now on.

    Installed after ``parse()``, which normalizes its own result.
    """
    calls: list[ast.Node] = []
    real = ast._normalize_impl

    def counting(node: ast.Node, **kwargs: Any) -> ast.Node:
        calls.append(node)
        return real(node, **kwargs)

    monkeypatch.setattr(ast, "_normalize_impl", counting)
    return calls


def _widen(leaf: ast.Term | ast.Phrase) -> ast.Node:
    return ast.Or(children=(leaf, ast.Term(field=FieldRef("title"), text="report")))


@pytest.mark.parametrize(
    ("hook", "expected_passes", "expected_ids"),
    [
        pytest.param(None, 1, [3], id="no-hook"),
        # One more pass per replacement the hook returns: each is normalized
        # before it is analyzed in its leaf's place. Two leaves, two passes.
        pytest.param(_widen, 3, [3, 4], id="hook-widening-two-leaves"),
    ],
)
def test_emit_normalizes_the_tree_once(
    hook: Callable[[ast.Term | ast.Phrase], ast.Node] | None,
    expected_passes: int,
    expected_ids: list[int],
    ereg: FieldRegistry,
    tindex: TIndex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _parse("content:basement AND content:plan", registry=ereg, default_fields=["content"])
    calls = _count_normalize_passes(monkeypatch)
    query = emit_(parsed.ast, index=tindex[0], registry=ereg, rewrite_leaf=hook)
    assert len(calls) == expected_passes
    assert search_ids(tindex[0], query) == expected_ids
