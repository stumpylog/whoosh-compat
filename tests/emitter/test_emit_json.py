"""JSON subpath term emission (feature detection + parse_query carve-out).

Installed tantivy-py's ``Query.term_query`` does exact-name field lookup, so
it cannot address a JSON subpath (``notes.user``) even though the *parser*
happily produces a dotted ``Term`` for one (``FieldsPlugin`` + ``FieldRegistry
.resolve_json``, see ``parser/plugins.py``). Until quickwit-oss/tantivy-py#716
lands, the emitter falls back to ``index.parse_query`` for JSON subpath terms
only. These tests exercise both branches (whichever the installed tantivy-py
actually supports) plus, explicitly, the escaping-fallback code path.
"""

from whoosh_compat import ast

from .conftest import emit_ast, search_ids


def test_json_subpath_term(tindex, ereg):
    node = ast.Term(field="notes.user", text="alice")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_term_parsed(tindex, ereg, parse):
    q = emit_ast(parse("notes.user:alice"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_term_other_doc(tindex, ereg):
    node = ast.Term(field="notes.user", text="bob")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [4]


def test_json_subpath_value_with_quote_and_backslash(tindex, ereg):
    # Doc 5's notes.user is the raw string a"b\c -- proves the parse_query
    # fallback's quote/backslash escaping round-trips.
    node = ast.Term(field="notes.user", text='a"b\\c')
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [5]


def test_json_subpath_matches_index_parse_query_directly(tindex, ereg):
    # Parity: emitting notes.user:alice must find exactly what a hand-rolled
    # index.parse_query call for the same subpath finds, regardless of
    # whether the emitter took the term_query or parse_query branch.
    index, _schema = tindex
    node = ast.Term(field="notes.user", text="alice")
    q = emit_ast(node, tindex, ereg)
    reference = index.parse_query('notes.user:"alice"', default_field_names=["notes"])
    assert search_ids(index, q) == search_ids(index, reference)


def test_json_subpath_unknown_subpath_falls_back_to_plain_field(tindex, ereg, parse):
    # "notes.bogus" isn't in the registry's subpaths for "notes" -- the
    # FieldsPlugin/registry demote it back to an unfielded term against the
    # default field, same as whoosh treats any other unknown field.
    node = parse("notes.bogus:alice")
    assert isinstance(node, ast.Term)
    assert node.field != "notes.bogus"
