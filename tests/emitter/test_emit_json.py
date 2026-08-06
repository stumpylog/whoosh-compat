"""JSON subpath term emission (feature detection + parse_query carve-out).

Installed tantivy-py's ``Query.term_query`` does exact-name field lookup, so
it cannot address a JSON subpath (``notes.user``) even though the *parser*
happily produces a dotted ``Term`` for one (``FieldsPlugin`` + ``FieldRegistry
.resolve_json``, see ``parser/plugins.py``). Until quickwit-oss/tantivy-py#716
lands, the emitter falls back to ``index.parse_query`` for JSON subpath terms
only. These tests exercise both branches (whichever the installed tantivy-py
actually supports) plus, explicitly, the escaping-fallback code path.
"""

import pytest

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import TantivyEmitter
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import emit_ast
from .conftest import search_ids


@pytest.mark.parametrize("text, expected", [
    pytest.param("alice", [1], id="matches-owning-doc"),
    pytest.param("bob", [4], id="matches-different-doc"),
    # Doc 5's notes.user is the raw string a"b\c: proves the parse_query
    # fallback's quote/backslash escaping round-trips.
    pytest.param('a"b\\c', [5], id="quote-and-backslash-round-trip"),
])
def test_json_subpath_term(tindex, ereg, text, expected):
    node = ast.Term(field="notes.user", text=text)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_json_subpath_term_parsed(tindex, ereg, parse):
    q = emit_ast(parse("notes.user:alice"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_multitoken_and(tindex, ereg):
    # A JSON subpath value that analyzes to multiple tokens must follow the
    # same multitoken policy as TEXT fields: emit_json's term_query path
    # reuses _text_term_query rather than duplicating its handling (see
    # module docstring / emitters/tantivy_.py). Doc 1's notes.note is
    # "check this" (two tokens); AND resolution requires both, so only doc 1
    # matches, mirroring test_emit_terms.py's test_multitoken_and.
    node = ast.Term(field="notes.note", text="check this")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_matches_index_parse_query_directly(tindex, ereg):
    # Parity: emitting notes.user:alice must find exactly what a hand-rolled
    # index.parse_query call for the same subpath finds, regardless of
    # whether the emitter took the term_query or parse_query branch.
    index, _schema = tindex
    node = ast.Term(field="notes.user", text="alice")
    q = emit_ast(node, tindex, ereg)
    reference = index.parse_query('notes.user:"alice"', default_field_names=["notes"])
    assert search_ids(index, q) == search_ids(index, reference)


def test_json_subpath_zero_tokens_matches_nothing(tindex, ereg):
    node = ast.Term(field="notes.user", text="")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_dotted_field_that_is_not_a_json_subpath_falls_back_to_resolve(tindex, ereg):
    # visit_term's `if resolved is not None` skip branch: a field name
    # containing "." that resolve_json() doesn't recognize (unlike
    # test_json_subpath_unknown_subpath_falls_back_to_plain_field, this
    # constructs the AST directly so it reaches the emitter rather than
    # being demoted by the parser first).
    from whoosh_compat.errors import QueryEmitError

    node = ast.Term(field="notes.bogus", text="x")
    with pytest.raises(QueryEmitError, match="unknown field"):
        emit_ast(node, tindex, ereg)


def test_json_paths_supported_false_when_no_json_field_registered(tindex):
    # _json_paths_supported()'s probe loop finds no JSON+subpaths field, so
    # probe_path stays None and the result is cached as False without ever
    # calling tantivy.Query.term_query.
    ereg_no_json = FieldRegistry([FieldSpec("content", FieldKind.TEXT)])
    emitter = TantivyEmitter(index=tindex[0], schema=tindex[1], registry=ereg_no_json)
    assert emitter._json_paths_supported() is False


def test_json_paths_supported_is_cached_across_calls(tindex, ereg):
    # Second call on the same emitter instance hits the cached-result branch
    # (`if self._json_paths_ok is None` is False the second time) instead of
    # re-probing.
    emitter = TantivyEmitter(index=tindex[0], schema=tindex[1], registry=ereg)
    first = emitter._json_paths_supported()
    second = emitter._json_paths_supported()
    assert first == second


def test_json_subpath_unknown_subpath_falls_back_to_plain_field(tindex, ereg, parse):
    # "notes.bogus" isn't in the registry's subpaths for "notes": the
    # FieldsPlugin/registry demote it back to an unfielded term against the
    # default field, same as whoosh treats any other unknown field.
    node = parse("notes.bogus:alice")
    # Exact demotion shape: the unrecognized "notes.bogus:" fieldname prefix
    # is merged back onto the word as plain text and searched as an
    # unfielded term against the parse fixture's default field ("content").
    assert node == ast.Term(field="content", text="notes.bogus:alice")
