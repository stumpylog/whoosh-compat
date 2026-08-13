"""``emit()``'s bare-exception backstop, extended to hand-built trees whose
shape (not just a leaf's value) violates the AST's type contract.

DIVERGENCES.md entry 24 already covers ``ValueError``/``TypeError``/
``AttributeError`` escaping from a tantivy-py call for a badly-typed leaf
value (e.g. a non-numeric ``Boosted.boost``). Two further shapes are
possible only via a hand-built tree bypassing the parser (the parser never
produces a ``None`` child or a pathologically deep chain) and previously
escaped ``emit()`` as bare exceptions instead of ``QueryEmitError``:

* A ``None`` (or otherwise non-node) value where a child node is expected,
  raised by ``ast.Visitor.generic_visit`` as a bare ``NotImplementedError``,
  or, for a top-level ``And``/``Or`` child, as a bare ``AttributeError`` from
  ``ast.normalize`` itself (which runs before the visitor ever sees the
  node).
* A chain deep enough to exhaust the interpreter's recursion limit, raised
  as a bare ``RecursionError``.

Every case here must raise ``QueryEmitError``, never the underlying bare
exception.
"""

from __future__ import annotations

import functools
from typing import cast

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast

CONTENT = FieldRef("content")
_TERM = ast.Term(field=CONTENT, text="invoice")


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(ast.Not(child=None), id="not-none-child"),  # type: ignore[arg-type]
        pytest.param(ast.Boosted(child=None, boost=2.0), id="boosted-none-child"),  # type: ignore[arg-type]
        pytest.param(ast.And((None,)), id="and-none-element"),  # type: ignore[arg-type]
        pytest.param(ast.Or((None,)), id="or-none-element"),  # type: ignore[arg-type]
        pytest.param(
            ast.AndNot(positive=None, negative=_TERM),  # type: ignore[arg-type]
            id="andnot-none-positive",
        ),
        pytest.param(
            ast.AndNot(positive=_TERM, negative=None),  # type: ignore[arg-type]
            id="andnot-none-negative",
        ),
        pytest.param(
            ast.AndMaybe(required=None, optional=_TERM),  # type: ignore[arg-type]
            id="andmaybe-none-required",
        ),
        pytest.param(
            ast.AndMaybe(required=_TERM, optional=None),  # type: ignore[arg-type]
            id="andmaybe-none-optional",
        ),
        pytest.param(
            ast.Require(scored=None, filter_only=_TERM),  # type: ignore[arg-type]
            id="require-none-scored",
        ),
        pytest.param(
            ast.Require(scored=_TERM, filter_only=None),  # type: ignore[arg-type]
            id="require-none-filter-only",
        ),
        pytest.param("content:foo", id="bare-string-top-level"),  # type: ignore[arg-type]
    ],
)
def test_hand_built_none_or_non_node_shape_raises_query_emit_error(
    node: ast.Node, tindex: TIndex, ereg: FieldRegistry
) -> None:
    with pytest.raises(QueryEmitError):
        emit_ast(node, tindex, ereg)


def test_deep_hand_built_chain_raises_query_emit_error(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Matches the depth from the reported reproduction: deep enough to blow
    # the interpreter's default recursion limit well before reaching it.
    def _wrap(n: ast.Node, _: int) -> ast.Node:
        return ast.Not(child=n)

    deep = functools.reduce(_wrap, range(2000), cast("ast.Node", _TERM))
    with pytest.raises(QueryEmitError):
        emit_ast(deep, tindex, ereg)
