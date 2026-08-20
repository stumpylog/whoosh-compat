"""End-to-end emission for a JSON field that declares a default subpath.

A default subpath changes what ``make_ref`` resolves a bare JSON field name
to, and every emit-side consequence follows from that one change: the
emitter never sees the bare name at all, only the ``FieldRef`` carrying the
subpath. Two shapes are worth pinning against a live index because their
*results* change, not just their spelling:

* ``notes:foo`` used to be demoted to a text search over the default fields
  (matching whatever documents happened to contain "foo" in content/title).
  It now searches the ``notes.note`` subpath.
* ``attrs:*`` used to be a whole-field existence check
  (``exists_query(name, json_subpaths=True)``, "any subpath has a value").
  It now checks the default subpath's own column. Doc 5 has ``attrs.user``
  but no ``attrs.note``, so the two answers genuinely differ.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import SubpathSpec

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import lower_fold
from .conftest import search_ids


@pytest.fixture
def dreg() -> FieldRegistry:
    """``ereg``'s JSON fields, with ``note`` declared the default subpath."""
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths={"note": SubpathSpec(default=True), "user": SubpathSpec()},
            ),
            FieldSpec(
                "attrs",
                FieldKind.JSON,
                subpaths={"note": SubpathSpec(default=True), "user": SubpathSpec()},
                fast=True,
            ),
        ]
    )


@pytest.fixture
def dparse(dreg: FieldRegistry) -> Callable[[str], ast.Node]:
    def _p(query_string: str) -> ast.Node:
        result = wc.parse(query_string, registry=dreg, default_fields=["content", "title"])
        assert not result.diagnostics
        return result.ast

    return _p


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Doc 1's notes.note is "check this", doc 4's is "final".
        pytest.param("notes:final", [4], id="bare-name-searches-the-default-subpath"),
        pytest.param("notes.note:final", [4], id="explicit-default-subpath-agrees"),
        # An explicitly typed subpath is unaffected by the default.
        pytest.param("notes.user:alice", [1], id="explicit-other-subpath-still-wins"),
        # ...and the default does not make the *other* subpath's values
        # reachable through the bare name.
        pytest.param("notes:alice", [], id="bare-name-does-not-search-other-subpaths"),
    ],
)
def test_bare_json_name_term_searches_the_default_subpath(
    tindex: TIndex,
    dreg: FieldRegistry,
    dparse: Callable[[str], ast.Node],
    query: str,
    expected: list[int],
) -> None:
    q = emit_ast(dparse(query), tindex, dreg)
    assert search_ids(tindex[0], q) == expected


def test_bare_star_existence_narrows_to_the_default_subpath(
    tindex: TIndex,
    ereg: FieldRegistry,
    dreg: FieldRegistry,
    dparse: Callable[[str], ast.Node],
    parse: Callable[[str], ast.Node],
) -> None:
    """The one emit-side consequence of declaring a default.

    Docs 1, 4 and 5 have a value under "attrs"; only 1 and 4 have one under
    "attrs.note" (doc 5 carries "user" alone). Without a default,
    ``attrs:*`` asks the whole-field question and matches all three; with
    one, it asks about the default subpath's own column and matches two.
    """
    without_default = emit_ast(parse("attrs:*"), tindex, ereg)
    assert search_ids(tindex[0], without_default) == [1, 4, 5]

    with_default = emit_ast(dparse("attrs:*"), tindex, dreg)
    assert search_ids(tindex[0], with_default) == [1, 4]
