"""Phrase emission (including the 1- and 0-token degenerate cases)."""

import contextlib

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import emit_ast
from .conftest import search_ids


@pytest.mark.parametrize(
    ("text", "slop", "expected"),
    [
        # whoosh slop=1 ("adjacent") maps to tantivy slop=0.
        pytest.param("shopname product1", 1, [2, 4], id="adjacent-slop-1-matches-both-docs"),
        # doc 4 is "shopname product1 product2": one intervening token, so it
        # needs whoosh slop=2 (tantivy slop=1).
        pytest.param("shopname product2", 2, [4], id="slop-2-covers-one-intervening-token"),
        pytest.param("shopname product2", 1, [], id="slop-1-too-small-matches-nothing"),
        # Analysis reduces this to one token -> term-query fallback (tantivy
        # rejects a single-word phrase query).
        pytest.param("invoice!!", 1, [1], id="single-token-falls-back-to-term-query"),
        pytest.param("!!!", 1, [], id="zero-tokens-matches-nothing"),
    ],
)
def test_phrase(tindex, ereg, text, slop, expected):
    node = ast.Phrase(field=FieldRef("content"), text=text, slop=slop)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_phrase_parsed(tindex, ereg, parse):
    q = emit_ast(parse('content:"shopname product1"'), tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]


def test_zero_token_phrase_dropped_as_group_child(tindex, ereg):
    # Regression: a zero-token analyzed Phrase (e.g. an all-stopword value)
    # nested inside an And must be dropped from the enclosing group exactly
    # like a zero-token Term already is, not emitted as a live-but-empty
    # query that turns into an unsatisfiable Must clause and kills the
    # whole And. Mirrors real whoosh, which drops the empty phrase clause
    # entirely at parse time (verified against the oracle: QueryParser
    # parses `foo AND "the"` with "the" as a stopword down to just
    # Term('content', 'foo')).
    grp = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Phrase(field=FieldRef("content"), text="!!!", slop=1),
        )
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- index-time position gap: the documented consequence of a positionless
# -- analyzer contract (ARCHITECTURE.md's "analyzer contract" section) -------


def _gapped_index_fixture():
    """A standalone tantivy index whose *index-time* tokenizer drops a
    stopword without renumbering positions, so consecutive query tokens land
    on non-consecutive index positions.

    This is deliberately a real tantivy ``StopWordFilter`` (via
    ``Filter.custom_stopword``), not ``FieldSpec.analyzer`` (the query-time
    callable this project controls): ARCHITECTURE.md's consequence is about a
    host's *index-time* analyzer leaving gaps, which whoosh-compat has no
    say over and no visibility into. Whoosh's own ``StopFilter`` defaults to
    ``renumber=True`` (verified against the pinned oracle,
    ``whoosh.analysis.filters.StopFilter``) and so never produces this gap;
    tantivy's stop word filter has no such renumbering option, which is
    exactly the mismatch being documented.

    Self-contained (not the shared ``tindex``/``ereg`` fixtures) because the
    gap-producing tokenizer has to be registered on the schema's "content"
    field itself, and the shared schema uses tantivy's plain 'default'
    tokenizer.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True, tokenizer_name="gap_stop")
    schema = sb.build()
    index = tantivy.Index(schema)

    gap_stop = (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        .filter(tantivy.Filter.lowercase())
        .filter(tantivy.Filter.custom_stopword(["the"]))
        .build()
    )
    index.register_tokenizer("gap_stop", gap_stop)

    w = index.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    # Indexed positions: alpha=0, ("the" dropped, no renumbering), beta=2.
    doc.add_text("content", "alpha the beta")
    w.add_document(doc)
    w.commit()
    index.reload()

    # Query-time analyzer: a plain lowercase split, standing in for a host
    # whose query-time analysis doesn't (and can't be expected to) know
    # about the index-time stopword filter's position bookkeeping.
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: t.lower().split())]
    )
    return index, schema, registry


@pytest.mark.parametrize(
    ("slop", "expected"),
    [
        # whoosh slop=1 ("adjacent") maps to tantivy slop=0: with the dropped
        # "the" leaving a real gap between positions 0 and 2, "alpha beta"
        # at consecutive query positions under-matches the index.
        pytest.param(1, [], id="default-slop-misses-across-index-time-gap"),
        # Widening slop by exactly the gap width (one dropped token ->
        # whoosh slop=2, tantivy slop=1) recovers the match: the mitigation
        # ARCHITECTURE.md documents for this consequence.
        pytest.param(2, [1], id="slop-widened-by-gap-width-recovers-match"),
    ],
)
def test_phrase_misses_across_index_time_position_gap(slop, expected):
    index, _schema, registry = _gapped_index_fixture()
    node = ast.Phrase(field=FieldRef("content"), text="alpha beta", slop=slop)
    q = emit_(node, index=index, registry=registry)
    assert search_ids(index, q) == expected


# -- DIVERGENCES.md entry 26: slop-1 transposition over-match ---------------
#
# Pins the boundary the issue #18 decision measured against the pinned
# whoosh oracle: the reversed pair "two one" against query phrase
# "one two". Real whoosh's SpanNear2 matcher is ordered=True and never
# matches a reversed pair at any slop; tantivy's slop is an unordered total
# displacement budget, so it starts matching once that budget covers a full
# swap (two positions), which is whoosh slop >= 3 under the slop-1 mapping.


def _reversed_pair_fixture():
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_text("content", "two one")
    w.add_document(doc)
    w.commit()
    index.reload()

    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: t.lower().split())]
    )
    return index, schema, registry


@pytest.mark.parametrize(
    ("slop", "expected"),
    [
        # The parser default and whoosh slop=2 agree with whoosh: no match.
        pytest.param(1, [], id="default-slop-agrees-with-whoosh-no-match"),
        pytest.param(2, [], id="slop-2-agrees-with-whoosh-no-match"),
        # whoosh never matches a reversed pair at any slop (ordered=True);
        # tantivy's unordered total-displacement budget covers the swap
        # once whoosh slop reaches 3 (tantivy slop=2). Over-match, not a bug.
        pytest.param(3, [1], id="slop-3-diverges-tantivy-over-matches-reversed-pair"),
    ],
)
def test_phrase_reversed_pair_slop_boundary(slop, expected):
    index, _schema, registry = _reversed_pair_fixture()
    node = ast.Phrase(field=FieldRef("content"), text="one two", slop=slop)
    q = emit_(node, index=index, registry=registry)
    assert search_ids(index, q) == expected


# -- issue #8: quoted (Phrase) values on non-TEXT/JSON fields need the same
# -- field-kind dispatch visit_term already has, so no raw ValueError from
# -- tantivy-py's type-mismatched term_query/phrase_query escapes emit(). ---


def test_phrase_on_u64_field_matches_unquoted_term(tindex, ereg):
    # asn:"100" (a quoted, single-word value) used to raise ValueError:
    # Expected U64 type for field asn, got '100' (tantivy-py rejects a
    # string against a u64 field's term_query).
    node = ast.Phrase(field=FieldRef("asn"), text="100", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_phrase_on_u64_field_bad_number_raises_query_emit_error(tindex, ereg):
    node = ast.Phrase(field=FieldRef("asn"), text="notanumber", slop=1)
    with pytest.raises(QueryEmitError, match="not a valid number"):
        emit_ast(node, tindex, ereg)


# issue #16: a quoted star on U64/BOOLEAN_EXISTS is an existence match. A
# *double*-quoted star stays an ast.Phrase at parse time (see
# tests/test_parser_fields.py), so its equivalence to the unquoted form is
# proven here, at emit/search-result level.
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param("asn", [1, 2, 3, 4, 5], id="u64-every-doc-has-a-value"),
        pytest.param("has_tag", [1, 2, 4], id="boolean-exists-docs-with-tags"),
    ],
)
def test_quoted_star_phrase_matches_unquoted_star(tindex, ereg, parse, field, expected):
    phrase_node = ast.Phrase(field=FieldRef(field), text="*", slop=1)
    every_node = parse(f"{field}:*")
    assert search_ids(tindex[0], emit_ast(phrase_node, tindex, ereg)) == expected
    assert search_ids(tindex[0], emit_ast(every_node, tindex, ereg)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("true", [1, 2, 4], id="truthy-phrase"),
        pytest.param("false", [3, 5], id="falsy-phrase"),
    ],
)
def test_phrase_on_boolean_exists_field(tindex, ereg, text, expected):
    node = ast.Phrase(field=FieldRef("has_tag"), text=text, slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_phrase_on_json_subpath_single_word_matches_unquoted_term(tindex, ereg):
    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_phrase_on_json_subpath_multi_word_does_not_raise(tindex, ereg):
    # No doc's notes.user is "bob smith"; the point is that this doesn't
    # raise (JSON subpath phrases used to route to the wrong tantivy field
    # name entirely, see the module docstring), not that it matches.
    node = ast.Phrase(field=FieldRef("notes", "user"), text="bob smith", slop=1)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_phrase_on_bare_json_field_raises_query_emit_error(tindex, ereg):
    node = ast.Phrase(field=FieldRef("notes"), text="alice", slop=1)
    with pytest.raises(QueryEmitError, match="JSON field"):
        emit_ast(node, tindex, ereg)


# -- exception contract: every field kind x quoted (Phrase) input either
# -- emits or raises a documented exception type, never a bare ValueError ---


@pytest.mark.parametrize(
    ("field", "text"),
    [
        pytest.param("content", "shopname product1", id="text"),
        pytest.param("tag", "billing", id="keyword"),
        pytest.param("asn", "100", id="u64-valid"),
        pytest.param("asn", "notanumber", id="u64-invalid"),
        pytest.param("has_tag", "true", id="boolean-exists"),
        pytest.param("notes.user", "alice", id="json-subpath"),
    ],
)
def test_phrase_emission_never_raises_undocumented_exception(tindex, ereg, field, text):
    name, _, subpath = field.partition(".")
    ref = FieldRef(name, subpath or None)
    node = ast.Phrase(field=ref, text=text, slop=1)
    # documented exception types: fine.
    with contextlib.suppress(QueryEmitError, UnsupportedQueryError):
        emit_ast(node, tindex, ereg)
