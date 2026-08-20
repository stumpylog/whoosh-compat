"""``TantivyEmitter._json_paths_supported()``'s probe must run once per
registry, not once per ``emit()`` call.

``emit()`` (the module-level function) builds a fresh ``TantivyEmitter`` per
call, and the probe used to be cached only on that short-lived instance
(``self._json_paths_ok``), so it re-ran the real ``Query.term_query(...,
"probe")`` call on every single emitted query against any registry with a
JSON field -- for a host with more than one such registry (paperless-ngx has
two), that is every search.
"""

from __future__ import annotations

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import emit_ast

_REAL_TERM_QUERY = tantivy.Query.term_query


@pytest.fixture
def count_probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every ``Query.term_query(..., "probe")`` call
    ``_json_paths_supported`` makes, without disturbing any other
    ``term_query`` call (real term emission uses real term text, never the
    literal string ``"probe"``).
    """
    calls: list[str] = []

    def spy(schema: tantivy.Schema, field: str, value: object) -> tantivy.Query:
        if value == "probe":
            calls.append(field)
        return _REAL_TERM_QUERY(schema, field, value)

    monkeypatch.setattr(tantivy.Query, "term_query", staticmethod(spy))
    return calls


def _json_term(field: str, subpath: str, text: str) -> ast.Term:
    return ast.Term(field=FieldRef(field, subpath), text=text)


def test_probe_runs_once_across_multiple_emit_calls_on_one_registry(
    tindex: TIndex, ereg: FieldRegistry, count_probe_calls: list[str]
) -> None:
    node = _json_term("notes", "user", "alice")

    emit_ast(node, tindex, ereg)
    emit_ast(node, tindex, ereg)
    emit_ast(node, tindex, ereg)

    assert len(count_probe_calls) == 1


def test_probe_is_scoped_per_registry_not_shared_globally(
    tindex: TIndex, ereg: FieldRegistry, count_probe_calls: list[str]
) -> None:
    """A second, distinct registry (paperless-ngx runs two) must not reuse
    the first registry's cached probe answer: each registry gets its own
    probe, run once, cached from then on for that registry.
    """
    other_registry = FieldRegistry(
        [FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user"))]
    )
    node = _json_term("notes", "user", "alice")

    emit_ast(node, tindex, ereg)
    emit_ast(node, tindex, other_registry)
    assert len(count_probe_calls) == 2

    emit_ast(node, tindex, ereg)
    emit_ast(node, tindex, other_registry)
    assert len(count_probe_calls) == 2
