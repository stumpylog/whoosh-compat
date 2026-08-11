"""Phrase emission (including the 1- and 0-token degenerate cases)."""

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import emit_ast
from .conftest import search_ids


@pytest.mark.parametrize(
    "text, slop, expected",
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
    node = ast.Phrase(field="content", text=text, slop=slop)
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
            ast.Term(field="content", text="invoice"),
            ast.Phrase(field="content", text="!!!", slop=1),
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
    "slop, expected",
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
    index, schema, registry = _gapped_index_fixture()
    node = ast.Phrase(field="content", text="alpha beta", slop=slop)
    q = emit_(node, index=index, schema=schema, registry=registry)
    assert search_ids(index, q) == expected
