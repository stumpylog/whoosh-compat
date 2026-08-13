"""JSON subpath term emission (feature detection + parse_query carve-out).

Installed tantivy-py's ``Query.term_query`` does exact-name field lookup, so
it cannot address a JSON subpath (``notes.user``) even though the *parser*
happily produces a dotted ``Term`` for one (``FieldsPlugin`` + ``FieldRegistry
.resolve_json``, see ``parser/plugins.py``). Until quickwit-oss/tantivy-py#716
lands, the emitter falls back to ``index.parse_query`` for JSON subpath terms
only. These tests exercise both branches (whichever the installed tantivy-py
actually supports) plus, explicitly, the escaping-fallback code path.
"""

from collections.abc import Callable

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import TantivyEmitter
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("alice", [1], id="matches-owning-doc"),
        pytest.param("bob", [4], id="matches-different-doc"),
        # Doc 5's notes.user is the raw string a"b\c: proves the parse_query
        # fallback's quote/backslash escaping round-trips.
        pytest.param('a"b\\c', [5], id="quote-and-backslash-round-trip"),
    ],
)
def test_json_subpath_term(
    tindex: TIndex, ereg: FieldRegistry, text: str, expected: list[int]
) -> None:
    node = ast.Term(field=FieldRef("notes", "user"), text=text)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_json_subpath_term_parsed(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    q = emit_ast(parse("notes.user:alice"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_term_as_group_child(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Regression: a JSON subpath term as a direct child of an And group used
    # to raise QueryEmitError("unknown field 'notes.user'") because
    # _group_child called self._resolve(child.field) directly instead of
    # going through the JSON-subpath-aware dispatch visit_term already uses.
    # Doc 1 has notes.user == "alice" and content contains "invoice".
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("notes", "user"), text="alice"),
            ast.Term(field=FieldRef("content"), text="invoice"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_zero_token_term_dropped_as_group_child(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # The zero-token drop policy must apply to JSON subpath terms exactly
    # like ordinary TEXT/KEYWORD terms: a JSON subpath term that analyzes to
    # zero tokens is dropped from the enclosing group rather than raising or
    # matching nothing.
    node = ast.And(
        children=(
            ast.Term(field=FieldRef("notes", "user"), text=""),
            ast.Term(field=FieldRef("content"), text="invoice"),
        )
    )
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_multitoken_and(tindex: TIndex, ereg: FieldRegistry) -> None:
    # A JSON subpath value that analyzes to multiple tokens must follow the
    # same multitoken policy as TEXT fields: emit_json's term_query path
    # reuses _text_term_query rather than duplicating its handling (see
    # module docstring / emitters/tantivy_.py). Doc 1's notes.note is
    # "check this" (two tokens); AND resolution requires both, so only doc 1
    # matches, mirroring test_emit_terms.py's test_multitoken_and.
    node = ast.Term(field=FieldRef("notes", "note"), text="check this")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_matches_index_parse_query_directly(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # Parity: emitting notes.user:alice must find exactly what a hand-rolled
    # index.parse_query call for the same subpath finds, regardless of
    # whether the emitter took the term_query or parse_query branch.
    index, _schema = tindex
    node = ast.Term(field=FieldRef("notes", "user"), text="alice")
    q = emit_ast(node, tindex, ereg)
    reference = index.parse_query('notes.user:"alice"', default_field_names=["notes"])
    assert search_ids(index, q) == search_ids(index, reference)


def test_json_subpath_zero_tokens_matches_nothing(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Term(field=FieldRef("notes", "user"), text="")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_dotted_field_that_is_not_a_json_subpath_falls_back_to_resolve(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # visit_term's `if resolved is not None` skip branch: a field name
    # containing "." that resolve_json() doesn't recognize (unlike
    # test_json_subpath_unknown_subpath_falls_back_to_plain_field, this
    # constructs the AST directly so it reaches the emitter rather than
    # being demoted by the parser first).
    node = ast.Term(field=FieldRef("notes", "bogus"), text="x")
    with pytest.raises(QueryEmitError, match="unknown field"):
        emit_ast(node, tindex, ereg)


def test_json_subpath_parse_query_fallback_honors_multitoken_first(tindex: TIndex) -> None:
    # Regression: the older-tantivy JSON fallback (index.parse_query) used
    # to discard the analyzed tokens entirely and quote the raw text
    # verbatim, ignoring the field's configured Multitoken policy. With
    # Multitoken.FIRST, only the first analyzed token should be searched:
    # a value with a trailing garbage token must still match a document
    # whose notes.user is exactly the first token, which a raw-text quoted
    # phrase query ("alice extra garbage") would not.
    ereg_first = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("note", "user"),
                analyzer=lambda t: t.lower().split(),
                multitoken=Multitoken.FIRST,
            ),
        ]
    )
    node = ast.Term(field=FieldRef("notes", "user"), text="alice extra garbage")
    q = emit_ast(node, tindex, ereg_first)
    assert search_ids(tindex[0], q) == [1]


def test_json_subpath_parse_query_fallback_honors_multitoken_and_or() -> None:
    # DIVERGENCES.md entry 22 (updated): promoting analysis to its own
    # pipeline stage (ast.analyze()) means a multi-token AND/OR-mode Term
    # value is already resolved into separate single-token Terms, each
    # addressing the JSON subpath independently, before TantivyEmitter ever
    # visits it: the parse_query fallback now gets one call per surviving
    # token, combined by the ordinary boolean-query machinery, giving true
    # AND/OR semantics instead of collapsing to one quoted, phrase-shaped
    # leaf. This is pinned with a *reversed* word order against the index
    # (query "alice bob" against an indexed "bob alice"): a phrase-shaped
    # collapse would never match this (wrong order), but true AND (or OR)
    # over independent single-token term queries does.
    index, _schema, reg = _transposed_json_fixture()
    and_reg = FieldRegistry(
        [
            FieldSpec(
                spec.name,
                spec.kind,
                subpaths=spec.subpaths,
                analyzer=spec.analyzer,
                multitoken=Multitoken.AND,
            )
            for spec in reg
        ]
    )
    emitter = TantivyEmitter(index=index, registry=and_reg)
    assert emitter._json_paths_supported() is False, (
        "this test pins the parse_query fallback; it requires the installed "
        "tantivy-py to not support JSON-subpath term_query/phrase_query"
    )
    and_node = ast.Term(field=FieldRef("notes", "user"), text="alice bob")
    and_q = emitter.emit(and_node)
    assert search_ids(index, and_q) == [1], (
        "Multitoken.AND must match regardless of word order (both tokens "
        "present), not collapse to a phrase-shaped single-leaf match"
    )

    or_reg = FieldRegistry(
        [
            FieldSpec(
                spec.name,
                spec.kind,
                subpaths=spec.subpaths,
                analyzer=spec.analyzer,
                multitoken=Multitoken.OR,
            )
            for spec in reg
        ]
    )
    or_emitter = TantivyEmitter(index=index, registry=or_reg)
    or_node = ast.Term(field=FieldRef("notes", "user"), text="alice nonexistent")
    or_q = or_emitter.emit(or_node)
    assert search_ids(index, or_q) == [1], (
        "Multitoken.OR must match on any surviving token ('alice' alone is "
        "present), not require the whole joined text to match as a phrase"
    )


def test_json_paths_supported_false_when_no_json_field_registered(tindex: TIndex) -> None:
    # _json_paths_supported()'s probe loop finds no JSON+subpaths field, so
    # probe_path stays None and the result is cached as False without ever
    # calling tantivy.Query.term_query.
    ereg_no_json = FieldRegistry([FieldSpec("content", FieldKind.TEXT)])
    emitter = TantivyEmitter(index=tindex[0], registry=ereg_no_json)
    assert emitter._json_paths_supported() is False


def test_json_paths_supported_is_cached_across_calls(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Second call on the same emitter instance hits the cached-result branch
    # (`if self._json_paths_ok is None` is False the second time) instead of
    # re-probing.
    emitter = TantivyEmitter(index=tindex[0], registry=ereg)
    first = emitter._json_paths_supported()
    second = emitter._json_paths_supported()
    assert first == second


@pytest.mark.parametrize(
    ("qs", "expected"),
    [
        pytest.param("attrs.user:*", [1, 4, 5], id="user-subpath-star"),
        pytest.param("attrs.note:*", [1, 4], id="note-subpath-star-excludes-user-only-doc"),
    ],
)
def test_json_subpath_exists_fast_field_scoped_to_subpath(
    tindex: TIndex,
    ereg: FieldRegistry,
    parse: Callable[[str], ast.Node],
    qs: str,
    expected: list[int],
) -> None:
    # issue #29: a fast JSON field's `field.subpath:*` must check only that
    # subpath's own fast column, not "does any subpath of this field have a
    # value". Doc 5 has attrs.user but no attrs.note, so the two subpaths
    # must emit genuinely different (not byte-identical) queries and
    # therefore different match sets.
    q = emit_ast(parse(qs), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_json_subpath_exists_whole_field_control_unchanged(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # Control: the non-subpath, whole-field existence strategy (any subpath
    # has a value) is unchanged by the subpath-aware fix above. A bare JSON
    # field name is not reachable query syntax on its own (make_ref demotes
    # it, see fields.py), so this is exercised the way it actually is
    # reachable: directly via Every(FieldRef("attrs")), same as
    # has_attrs's BOOLEAN_EXISTS redirect (ereg's "has_attrs" field) would
    # resolve to. Docs 1, 4, and 5 all have some "attrs" value; docs 2 and 3
    # have none.
    node = ast.Every(field=FieldRef("attrs"))
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1, 4, 5]


def test_json_subpath_exists_nonfast_raises_naming_dotted_form(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # issue #29's second symptom: a non-fast JSON field has no way to answer
    # "exists" for a subpath, but the error message must name the
    # subpath-qualified form the user actually typed ("notes.user:*"), not
    # the bare field name ("notes:*"), which is not reachable syntax at all.
    node = parse("notes.user:*")
    with pytest.raises(UnsupportedQueryError, match=r"notes\.user"):
        emit_ast(node, tindex, ereg)


def test_json_subpath_exists_multilevel_fast_field(tindex: TIndex) -> None:
    # issue #29 explicitly calls out verifying multi-level subpaths
    # (a.b.c), not just single-level, against a real tantivy index.
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_json_field("data", stored=True, fast=True)  # type: ignore[call-arg]
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for doc_id, payload in [
        (1, {"a": {"b": {"c": "value"}}}),
        (2, {"a": {"b": {"other": "value"}}}),
        (3, {}),
    ]:
        doc = tantivy.Document()
        doc.add_unsigned("id", doc_id)
        doc.add_json("data", payload)
        w.add_document(doc)
    w.commit()
    index.reload()

    reg = FieldRegistry(
        [FieldSpec("data", FieldKind.JSON, subpaths=("a.b.c", "a.b.other"), fast=True)]
    )
    node = ast.Every(field=FieldRef("data", "a.b.c"))
    q = emit_ast(node, (index, schema), reg)
    assert search_ids(index, q) == [1]


def test_json_subpath_unknown_subpath_falls_back_to_plain_field(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # "notes.bogus" isn't in the registry's subpaths for "notes": the
    # FieldsPlugin/registry demote it back to an unfielded term against the
    # default field, same as whoosh treats any other unknown field.
    node = parse("notes.bogus:alice")
    # Exact demotion shape: the unrecognized "notes.bogus:" fieldname prefix
    # is merged back onto the word as plain text and searched as an
    # unfielded term against the parse fixture's default field ("content").
    assert node == ast.Term(field=FieldRef("content"), text="notes.bogus:alice")


# -- issue #34: a quoted phrase on a JSON subpath must map whoosh slop and
# -- ignore Multitoken exactly like a plain-field Phrase does, on both the
# -- parse_query fallback branch and the (not-yet-shipped) term_query/
# -- phrase_query "supported" branch. -----------------------------------


def _transposed_json_fixture() -> tuple[tantivy.Index, tantivy.Schema, FieldRegistry]:
    """A standalone JSON-field index whose ``notes.user`` is "bob alice"
    (transposed relative to a query phrase "alice bob"), for pinning the
    whoosh-slop-to-tantivy-slop mapping the same way
    ``test_emit_phrase.py``'s plain-field slop tests do.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_json_field("notes", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_json("notes", {"user": "bob alice"})
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
            ),
        ]
    )
    return ix, schema, reg


def test_json_subpath_phrase_fallback_slop_is_silently_ignored(tindex: TIndex) -> None:
    # DIVERGENCES.md entry 22: index.parse_query's single quoted-leaf carve-
    # out has no way to express slop (verified directly: appending a "~N"
    # suffix to the query string does not change the resulting PhraseQuery's
    # slop from 0). A transposed pair therefore never matches through the
    # fallback branch, however wide the requested whoosh slop is; this pins
    # that limitation rather than silently regressing it back to "match".
    index, _schema, reg = _transposed_json_fixture()
    emitter = TantivyEmitter(index=index, registry=reg)
    assert emitter._json_paths_supported() is False, (
        "this test pins the parse_query fallback; it requires the installed "
        "tantivy-py to not support JSON-subpath term_query/phrase_query"
    )
    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice bob", slop=99)
    q = emitter.emit(node)
    assert search_ids(index, q) == [], (
        "the fallback path cannot express slop at all: even a very wide "
        "whoosh slop must not recover a transposed-word match"
    )


@pytest.mark.parametrize(
    "multitoken",
    [
        pytest.param(Multitoken.FIRST, id="first"),
        pytest.param(Multitoken.AND, id="and"),
        pytest.param(Multitoken.OR, id="or"),
        pytest.param(Multitoken.PHRASE, id="phrase-mode"),
    ],
)
def test_json_subpath_phrase_fallback_ignores_multitoken(multitoken: Multitoken) -> None:
    # A quoted Phrase node's words are the phrase, not independent tokens
    # subject to the field's Multitoken policy (that policy is a Term-only
    # concern): no Multitoken mode, including FIRST, should collapse this to
    # a single-term match. The doc's notes.user has "alice" as its second
    # word but never "bogus": a Multitoken.FIRST collapse to a bare "alice"
    # term query would wrongly match, so this discriminates real phrase
    # semantics from a term-query collapse.
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_json_field("notes", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_json("notes", {"user": "bob alice"})
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
                multitoken=multitoken,
            ),
        ]
    )
    emitter = TantivyEmitter(index=ix, registry=reg)
    assert emitter._json_paths_supported() is False
    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice bogus", slop=1)
    q = emitter.emit(node)
    assert search_ids(ix, q) == [], (
        "a non-empty match means the phrase collapsed to a term-level "
        "Multitoken combination instead of staying a real phrase query"
    )


def _force_json_paths_supported(emitter: TantivyEmitter) -> None:
    """Force the emitter's cached ``_json_paths_supported()`` probe to
    ``True``, for exercising the "supported" branch of JSON-subpath emission
    on a pinned tantivy-py (0.26) that never actually probes ``True`` for a
    genuine JSON field (tantivy-py#716 hasn't shipped).

    The "supported" branch's own code (``TantivyEmitter._emit_json_phrase``)
    does nothing JSON-specific once it takes this branch: it just calls
    ``tantivy.Query.phrase_query``/``term_query`` with ``resolved.dotted_name``
    as a plain field name string. A schema field literally named
    ``"notes.user"`` (a dot is a legal character in a tantivy field name,
    verified directly) exercises the exact same call with a real index and a
    real search, without depending on genuine JSON-path support that this
    installed tantivy-py doesn't have yet.
    """
    emitter._json_paths_ok = True


def test_json_subpath_phrase_supported_branch_maps_slop(tindex: TIndex) -> None:
    # Result-level pin for the term_query/phrase_query "supported" branch
    # (see _force_json_paths_supported): once tantivy-py#716 ships and
    # _json_paths_supported() starts returning True for real, a quoted
    # phrase on a JSON subpath must match a transposed document exactly as
    # the same phrase would on a plain TEXT field, via the same
    # max(slop - 1, 0) mapping.
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("notes.user", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_text("notes.user", "bob alice")
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
            ),
        ]
    )
    emitter = TantivyEmitter(index=ix, registry=reg)
    _force_json_paths_supported(emitter)

    too_narrow = emitter.emit(ast.Phrase(field=FieldRef("notes", "user"), text="alice bob", slop=1))
    assert search_ids(ix, too_narrow) == [], (
        "whoosh slop=1 (adjacent) must not match a transposed pair"
    )

    wide_enough = emitter.emit(
        ast.Phrase(field=FieldRef("notes", "user"), text="alice bob", slop=99)
    )
    assert search_ids(ix, wide_enough) == [1], (
        "whoosh slop=99 must map to a wide enough tantivy slop to match the "
        "transposed pair, same as it would on a plain TEXT field"
    )


def test_json_subpath_phrase_supported_branch_ignores_multitoken_first(tindex: TIndex) -> None:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("notes.user", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_text("notes.user", "bob alice")
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
                multitoken=Multitoken.FIRST,
            ),
        ]
    )
    emitter = TantivyEmitter(index=ix, registry=reg)
    _force_json_paths_supported(emitter)

    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice bogus", slop=1)
    q = emitter.emit(node)
    assert search_ids(ix, q) == [], (
        "Multitoken.FIRST must not collapse the phrase to a bare 'alice' "
        "term query even on the supported branch"
    )
