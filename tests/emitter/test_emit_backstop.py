"""``emit()``'s bare-exception backstop, extended to hand-built trees whose
shape (not just a leaf's value) violates the AST's type contract.

DIVERGENCES.md entry 24 already covers ``ValueError``/``TypeError``/
``AttributeError`` escaping from a tantivy-py call for a badly-typed leaf
value (e.g. a non-numeric ``Boosted.boost``). Two further shapes are
possible only via a hand-built tree bypassing the parser (the parser never
produces a ``None`` child or a pathologically deep chain) and previously
escaped ``emit()`` as bare exceptions instead of a ``QueryError``:

* A ``None`` (or otherwise non-node) value where a child node is expected,
  raised by ``ast.Visitor.generic_visit`` as a bare ``NotImplementedError``,
  or, for a top-level ``And``/``Or`` child, as a bare ``AttributeError`` from
  ``ast.normalize`` itself (which runs before the visitor ever sees the
  node).
* A chain deep enough to exhaust the interpreter's recursion limit, raised
  as a bare ``RecursionError``.

Every case here must raise ``QueryError`` carrying an
``AST_INVALID_SHAPE`` diagnostic, never the underlying bare exception: none
of these shapes ever reached tantivy, so none of them is a backend
rejection. A pattern tantivy itself refuses to compile is the separate
``PATTERN_TOO_COMPLEX`` cell below, and is user input rather than a defect.
"""

from __future__ import annotations

import ast as py_ast
import functools
import pathlib
from collections.abc import Callable
from typing import cast

import pytest

from whoosh_compat import ast
from whoosh_compat.emitters import tantivy_
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids

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
def test_hand_built_none_or_non_node_shape_is_ast_invalid_shape(
    node: ast.Node, tindex: TIndex, ereg: FieldRegistry
) -> None:
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert exc.value.diagnostic.cause is Cause.INTERNAL


def test_deep_hand_built_chain_is_ast_invalid_shape(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Matches the depth from the reported reproduction: deep enough to blow
    # the interpreter's default recursion limit well before reaching it.
    def _wrap(n: ast.Node, _: int) -> ast.Node:
        return ast.Not(child=n)

    deep = functools.reduce(_wrap, range(2000), cast("ast.Node", _TERM))
    with pytest.raises(QueryError) as exc:
        emit_ast(deep, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE


def test_no_message_references_project_documentation() -> None:
    """DIVERGENCES references belong in Diagnostic.divergence, not in prose.

    Without this guard the cross-references can be deleted from messages
    and silently not replaced, which is worse than leaving them.
    """
    emitter = pathlib.Path(tantivy_.__file__)
    tree = py_ast.parse(emitter.read_text())

    docstrings = set()
    for node in py_ast.walk(tree):
        if isinstance(node, py_ast.Module | py_ast.ClassDef | py_ast.FunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, py_ast.Expr) and isinstance(first.value, py_ast.Constant):
                docstrings.add(id(first.value))

    # Only literal text that ends up in a runtime string counts. Comments are
    # absent from the AST, and docstrings are prose about the code rather than
    # something a caller ever reads back.
    in_messages = [
        node.value
        for node in py_ast.walk(tree)
        if isinstance(node, py_ast.Constant)
        and isinstance(node.value, str)
        and "DIVERGENCES" in node.value
        and id(node) not in docstrings
    ]
    assert in_messages == [], in_messages


def test_oversized_wildcard_is_unsupported_not_internal(
    ereg: FieldRegistry, tindex: TIndex, parse: Callable[[str], ast.Node]
) -> None:
    """A long wildcard is user input, not a defect.

    tantivy caps a compiled regex at 1000 states, and a pattern someone can
    type reaches that cap. Reporting it as INTERNAL would make a host answer
    an ordinary (if unusual) query with a 500.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("title:a" + "?" * 100), tindex, ereg)
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is DiagnosticKind.PATTERN_TOO_COMPLEX
    assert diagnostic.cause is Cause.UNSUPPORTED
    assert diagnostic.startchar is not None
    assert diagnostic.field == FieldRef("title")


def test_oversized_prefix_is_unsupported_not_internal(
    ereg: FieldRegistry, tindex: TIndex, parse: Callable[[str], ast.Node]
) -> None:
    """The sibling of the wildcard cell: ``visit_prefix`` builds its own
    regex from user text and hits the same tantivy cap.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("title:" + "a" * 1200 + "*"), tindex, ereg)
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is DiagnosticKind.PATTERN_TOO_COMPLEX
    assert diagnostic.cause is Cause.UNSUPPORTED
    assert diagnostic.startchar is not None
    assert diagnostic.field == FieldRef("title")


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("title:a" + "?" * 50, id="wildcard-under-cap"),
        pytest.param("title:" + "a" * 300 + "*", id="prefix-under-cap"),
    ],
)
def test_pattern_under_the_cap_still_emits(
    query: str, ereg: FieldRegistry, tindex: TIndex, parse: Callable[[str], ast.Node]
) -> None:
    """Guards the threshold: these compile fine, so the narrow catch must
    not swallow ordinary patterns.
    """
    index, _ = tindex
    assert search_ids(index, emit_ast(parse(query), tindex, ereg)) == []


def test_unvisitable_node_is_ast_invalid_shape(ereg: FieldRegistry, tindex: TIndex) -> None:
    """A node type no ``visit_*`` method handles never reaches tantivy, so
    it is a caller-built shape defect, not a backend rejection.
    """

    class Bogus(ast.Node):
        pass

    with pytest.raises(QueryError) as exc:
        emit_ast(Bogus(), tindex, ereg)
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert diagnostic.cause is Cause.INTERNAL


def test_query_error_from_a_nested_visitor_passes_through_untouched(
    ereg: FieldRegistry, tindex: TIndex, parse: Callable[[str], ast.Node]
) -> None:
    """``QueryError`` derives from ``Exception``, not ``ValueError``, so a
    diagnostic raised deep inside ``visit()`` must reach the caller with its
    own kind rather than being relabelled ``BACKEND_REJECTED``.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("created:notadate AND title:x"), tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.BAD_DATE
