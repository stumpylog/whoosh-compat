"""End-to-end emission for a JSON field that declares a default subpath.

A default subpath changes what ``make_ref`` resolves a bare JSON field name
to, and every emit-side consequence follows from that one change: the
emitter never sees the bare name at all, only the ``FieldRef`` carrying the
subpath. The shapes worth pinning against a live index:

* ``notes:foo`` used to be demoted to a text search over the default fields
  (matching whatever documents happened to contain "foo" in content/title).
  It now searches the ``notes.note`` subpath.
* ``attrs:*`` used to be a whole-field existence check
  (``exists_query(name, json_subpaths=True)``, "any subpath has a value").
  On a *fast* JSON field it now checks the default subpath's own column:
  doc 5 has ``attrs.user`` but no ``attrs.note``, so the two answers
  genuinely differ. On a *non-fast* one (``notes``, the shape paperless
  actually has) both are the same ``EXISTS_REQUIRES_FAST`` refusal and only
  the field name in the message changes.
* ``notes:[a TO b]`` reaches DIVERGENCES.md entry 30's emit-time range
  refusal through the bare name, where it used to demote to text.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
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


def test_non_fast_bare_star_only_changes_the_name_in_the_message(
    tindex: TIndex,
    ereg: FieldRegistry,
    dreg: FieldRegistry,
    dparse: Callable[[str], ast.Node],
    parse: Callable[[str], ast.Node],
) -> None:
    """``notes`` is the non-fast JSON field, the shape a host actually has.

    Both with and without a default, ``notes:*`` is refused identically
    (same kind, same cause, so the same HTTP status for a host); only the
    field name in the message changes, from the bare name to the dotted
    default, which is what the query-text rewrite this feature replaces
    already produced. The narrowing below is only observable once a JSON
    field is fast.
    """
    for node, registry, named in (
        (parse("notes:*"), ereg, "notes"),
        (dparse("notes:*"), dreg, "notes.note"),
    ):
        with pytest.raises(QueryError) as excinfo:
            emit_ast(node, tindex, registry)
        assert excinfo.value.diagnostic.kind is DiagnosticKind.EXISTS_REQUIRES_FAST
        assert excinfo.value.diagnostic.cause is Cause.MISCONFIGURED
        assert f"'{named}'" in str(excinfo.value)


def test_range_on_a_defaulted_bare_name_is_refused_at_emit(
    tindex: TIndex,
    dreg: FieldRegistry,
    dparse: Callable[[str], ast.Node],
) -> None:
    # DIVERGENCES.md entry 30: a lexicographic range on a subpath parses
    # cleanly and is refused by visit_termrange. A defaulted bare name
    # reaches it, where without a default the same spelling demoted to a
    # silent default-field text search.
    with pytest.raises(QueryError) as excinfo:
        emit_ast(dparse("notes:[a TO b]"), tindex, dreg)
    assert excinfo.value.diagnostic.kind is DiagnosticKind.TEXT_RANGE
    assert excinfo.value.diagnostic.divergence == 30


def test_bare_star_existence_narrows_to_the_default_subpath(
    tindex: TIndex,
    ereg: FieldRegistry,
    dreg: FieldRegistry,
    dparse: Callable[[str], ast.Node],
    parse: Callable[[str], ast.Node],
) -> None:
    """The one result-changing emit-side consequence of declaring a default.

    Docs 1, 4 and 5 have a value under "attrs"; only 1 and 4 have one under
    "attrs.note" (doc 5 carries "user" alone). Without a default,
    ``attrs:*`` asks the whole-field question and matches all three; with
    one, it asks about the default subpath's own column and matches two.
    """
    without_default = emit_ast(parse("attrs:*"), tindex, ereg)
    assert search_ids(tindex[0], without_default) == [1, 4, 5]

    with_default = emit_ast(dparse("attrs:*"), tindex, dreg)
    assert search_ids(tindex[0], with_default) == [1, 4]
