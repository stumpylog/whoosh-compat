"""ast.Fuzzy emitter support: field-kind dispatch, distance domain
validation, normalization, and real-search behavior.
"""

from __future__ import annotations

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import PatternNormalizer

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


def _content_index(docs: dict[int, str], *, raw_tag: bool = False) -> TIndex:
    """Build an index of `id` plus a default-tokenized `content` field
    holding each document's text. With raw_tag, the same text also goes
    into a raw-tokenized `tag` field.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    if raw_tag:
        sb.add_text_field("tag", stored=True, tokenizer_name="raw")
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, text in docs.items():
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("content", text)
        if raw_tag:
            doc.add_text("tag", text)
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture(scope="module")
def fuzzy_tindex() -> TIndex:
    """A small, purpose-built index with an exact term, a one-edit-distance
    typo of it, an unrelated document, and a short prefix-only document.
    Distinct from tindex/ereg's shared DOCS, which has no typo pairs to
    exercise real edit-distance matching against."""
    return _content_index({1: "tokyo", 2: "tokio", 3: "unrelated", 4: "tok"})


@pytest.fixture
def fuzzy_ereg() -> FieldRegistry:
    return FieldRegistry([FieldSpec("content", FieldKind.TEXT)])


# -- Kind dispatch: TEXT and KEYWORD succeed (real searches on both are
# pinned by test_fuzzy_on_text_and_keyword_fields_matches_a_real_document
# at the end of this file), every other FieldKind member fails with
# AST_KIND_NOT_IMPLEMENTED, including both JSON shapes (bare and subpath):
# tantivy-py 0.26.0's fuzzy_term_query rejects a dotted "field.subpath"
# name as an unknown field, and a bare JSON field wants a JSON value
# argument, not a term string. The limit is tantivy-py's binding; tantivy's
# own FuzzyTermQuery accepts a JSON path term. --


UNSUPPORTED_KIND_FIELDS = [
    pytest.param(FieldRef("asn"), FieldKind.U64, id="u64"),
    pytest.param(FieldRef("created"), FieldKind.DATE, id="date"),
    pytest.param(FieldRef("added"), FieldKind.DATETIME, id="datetime"),
    pytest.param(FieldRef("has_tag"), FieldKind.BOOLEAN_EXISTS, id="boolean-exists"),
    pytest.param(FieldRef("notes"), FieldKind.JSON, id="json-bare"),
    pytest.param(FieldRef("notes", "note"), FieldKind.JSON, id="json-subpath"),
]


@pytest.mark.parametrize(("field_ref", "kind"), UNSUPPORTED_KIND_FIELDS)
def test_fuzzy_fails_on_every_other_field_kind(
    field_ref: FieldRef, kind: FieldKind, tindex: TIndex, ereg: FieldRegistry
) -> None:
    resolved = ereg.resolve(field_ref)
    assert resolved is not None
    assert resolved.spec.kind is kind
    node = ast.Fuzzy(field=field_ref, text="x", distance=1)
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.AST_KIND_NOT_IMPLEMENTED
    assert exc.value.diagnostic.cause is Cause.INTERNAL


def test_fuzzy_fails_on_every_other_field_kind_covers_every_unsupported_kind() -> None:
    """Guards against a future FieldKind member being added without a row
    in UNSUPPORTED_KIND_FIELDS: it would otherwise pass silently
    under-covered instead of failing the suite. Each row's kind is checked
    against the field it names by the test above.
    """
    covered_kinds = {p.values[1] for p in UNSUPPORTED_KIND_FIELDS}
    assert covered_kinds == set(FieldKind) - {FieldKind.TEXT, FieldKind.KEYWORD}


def test_fuzzy_fails_on_unknown_field(tindex: TIndex, ereg: FieldRegistry) -> None:
    node = ast.Fuzzy(field=FieldRef("nonexistent"), text="x", distance=1)
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.AST_UNKNOWN_FIELD


# -- Distance domain: 0 and 2 are the boundary values a real search
# accepts. tantivy builds a fuzzy query for any u8 distance, but only
# compiles one for 0 through 2 when the search runs, so 3 would otherwise
# emit cleanly and fail later with a bare ValueError from the searcher.
# Out-of-range values fail at emit() with AST_BAD_NUMBER, carrying
# node/raw_value like every other AST_BAD_NUMBER call site. --


@pytest.mark.parametrize(
    ("distance", "expected_ids"),
    [
        pytest.param(0, [1], id="exact-only"),
        # "tokio" is one substitution from "tokyo".
        pytest.param(1, [1, 2], id="one-edit-typo"),
        # "tok" is two deletions from "tokyo": reached only at distance 2.
        pytest.param(2, [1, 2, 4], id="ceiling-two-deletions"),
    ],
)
def test_fuzzy_distance_selects_which_edits_match(
    distance: int, expected_ids: list[int], fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=distance)
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == expected_ids


@pytest.mark.parametrize(
    "distance",
    [
        pytest.param(-1, id="below-floor"),
        pytest.param(3, id="above-search-ceiling"),
        pytest.param(256, id="above-u8"),
        # bool is an int subclass: without an explicit check True would
        # silently mean distance 1.
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="str"),
    ],
)
@pytest.mark.parametrize(
    "field", [pytest.param("content", id="text"), pytest.param("tag", id="keyword")]
)
def test_fuzzy_rejects_invalid_distance(
    field: str, distance: object, tindex: TIndex, ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(
        field=FieldRef(field),
        text="billing",
        distance=distance,  # type: ignore[arg-type]
    )
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_BAD_NUMBER
    assert d.cause is Cause.INTERNAL
    assert d.raw_value == str(distance)


# -- text and prefix types: a caller-built value of the wrong type is an
# AST_INVALID_SHAPE naming the attribute, not a BACKEND_REJECTED blaming
# tantivy-py for a value this library never should have passed it. --


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("text", 123, id="text-int"),
        pytest.param("text", b"tokyo", id="text-bytes"),
        pytest.param("text", None, id="text-none"),
        # bool subclasses int, but the reverse does not hold: 1 is not a
        # bool, so prefix=1 is a type error rather than a truthy prefix.
        pytest.param("prefix", 1, id="prefix-int"),
        pytest.param("prefix", "yes", id="prefix-str"),
        pytest.param("prefix", None, id="prefix-none"),
    ],
)
def test_fuzzy_rejects_wrongly_typed_text_or_prefix(
    attribute: str, value: object, tindex: TIndex, ereg: FieldRegistry
) -> None:
    fields: dict[str, object] = {"text": "billing", "prefix": False, attribute: value}
    node = ast.Fuzzy(field=FieldRef("content"), distance=1, **fields)  # type: ignore[arg-type]
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert d.cause is Cause.INTERNAL
    assert f"fuzzy {attribute} must be" in d.message


# -- Core query construction: no pattern_normalizer uses
# raw text; the normalizer's empty-forms answer is Query.empty_query(), not
# an exception (matching visit_prefix's identical case). Schema drift on a
# Fuzzy field, with and without a pattern_normalizer, is pinned in
# test_schema_drift.py's leaf sweep. --


def test_fuzzy_case_mismatch_with_no_normalizer_consumes_the_edit_budget(
    fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    """fuzzy_ereg's "content" field has no pattern_normalizer, so a query's
    "Tokyo" reaches tantivy exactly as given while the index holds the
    lowercase "tokyo"/"tokio". At distance=1 that alone matches id 1
    ("tokyo", one case-only edit away) but not id 2 ("tokio", a genuine
    typo, now two edits away): the case difference already spent the whole
    budget. A case-normalized query with the same distance (see the
    one-edit-typo case of test_fuzzy_distance_selects_which_edits_match)
    matches both.
    """
    node = ast.Fuzzy(field=FieldRef("content"), text="Tokyo", distance=1)
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [1]


def test_fuzzy_pattern_normalizer_offering_no_form_matches_nothing(tindex: TIndex) -> None:
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=lambda _: ())]
    )
    node = ast.Fuzzy(field=FieldRef("content"), text="anything", distance=1)
    query = emit_ast(node, tindex, registry)
    assert search_ids(tindex[0], query) == []


# -- Blank text: empty or whitespace-only text matches nothing, whether the
# caller wrote it or the field's pattern_normalizer produced it. parse()
# itself produces empty terms from user input (`''`, `content:''`), so a
# host mirroring a parsed Term into a Fuzzy companion must not get an
# error for it. Passed through to tantivy, a blank term would match every
# one-character term at distance 1, and every term in the field with
# prefix=True; short_term_tindex holds a one-character term so that
# degenerate match would show up here. --


@pytest.fixture(scope="module")
def short_term_tindex() -> TIndex:
    return _content_index({1: "a", 2: "tokyo", 3: "zz"}, raw_tag=True)


@pytest.fixture
def short_term_reg() -> FieldRegistry:
    return FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT), FieldSpec("tag", FieldKind.KEYWORD)]
    )


@pytest.mark.parametrize(
    ("distance", "prefix"),
    [
        pytest.param(0, False, id="distance-0"),
        pytest.param(1, False, id="distance-1"),
        pytest.param(0, True, id="prefix-distance-0"),
        pytest.param(1, True, id="prefix-distance-1"),
    ],
)
@pytest.mark.parametrize(
    "text",
    [pytest.param("", id="empty"), pytest.param(" ", id="space"), pytest.param("\t", id="tab")],
)
@pytest.mark.parametrize(
    "field", [pytest.param("content", id="text"), pytest.param("tag", id="keyword")]
)
def test_fuzzy_blank_text_matches_nothing(
    field: str,
    text: str,
    distance: int,
    prefix: bool,
    short_term_tindex: TIndex,
    short_term_reg: FieldRegistry,
) -> None:
    node = ast.Fuzzy(field=FieldRef(field), text=text, distance=distance, prefix=prefix)
    query = emit_ast(node, short_term_tindex, short_term_reg)
    assert search_ids(short_term_tindex[0], query) == []


@pytest.mark.parametrize(
    "normalized",
    [pytest.param("", id="empty"), pytest.param(" ", id="space")],
)
def test_fuzzy_text_normalized_to_blank_matches_nothing(
    normalized: str, short_term_tindex: TIndex
) -> None:
    # Non-empty text a normalizer strips away entirely (punctuation, say)
    # must not reach tantivy as a blank term either.
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=lambda _: normalized)]
    )
    node = ast.Fuzzy(field=FieldRef("content"), text="--", distance=1, prefix=True)
    query = emit_ast(node, short_term_tindex, registry)
    assert search_ids(short_term_tindex[0], query) == []


def test_fuzzy_prefix_with_text_no_longer_than_distance_matches_every_term(
    short_term_tindex: TIndex, short_term_reg: FieldRegistry
) -> None:
    # Documented, not rejected (see README): with prefix=True, the empty
    # prefix of every term is within `distance` edits of any text that
    # short, so every term matches. A host building a companion clause is
    # expected to skip such short words itself.
    node = ast.Fuzzy(field=FieldRef("content"), text="x", distance=1, prefix=True)
    query = emit_ast(node, short_term_tindex, short_term_reg)
    assert search_ids(short_term_tindex[0], query) == [1, 2, 3]


def test_fuzzy_empty_normalized_form_is_dropped_from_the_alternatives(
    fuzzy_tindex: TIndex,
) -> None:
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=lambda t: ("", t))]
    )
    node = ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=0, prefix=True)
    query = emit_ast(node, fuzzy_tindex, registry)
    assert search_ids(fuzzy_tindex[0], query) == [1]


# -- transposition_cost_one: hardcoded True at the emitter (never exposed
# on ast.Fuzzy itself, see its docstring), so this real-search test is the
# only thing pinning that choice. A single adjacent-character transposition
# ("table" -> "talbe", swapping the 'b'/'l' pair, adjacent by construction)
# costs 1 edit under transposition_cost_one=True and 2 edits under plain
# Levenshtein: a distance=1 fuzzy query on the untransposed word matches the
# transposed document only under the True behavior this emitter hardcodes.
# --


@pytest.fixture(scope="module")
def transposition_tindex() -> TIndex:
    return _content_index({1: "table", 2: "talbe"})


def test_fuzzy_transposition_cost_one_matches_adjacent_swap(
    transposition_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(field=FieldRef("content"), text="table", distance=1)
    query = emit_ast(node, transposition_tindex, fuzzy_ereg)
    # id 1 ("table") matches at distance 0; id 2 ("talbe") matches only
    # because the adjacent transposition is charged 1 edit, not 2.
    assert search_ids(transposition_tindex[0], query) == [1, 2]


# -- prefix reaches the constructed query, verified via real search: it
# changes whether "tok" matches documents it is only a PREFIX of, not equal
# to. (distance is pinned the same way by
# test_fuzzy_distance_selects_which_edits_match above.) --


def test_fuzzy_prefix_false_requires_the_whole_term(
    fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(field=FieldRef("content"), text="tok", distance=0, prefix=False)
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [4]  # only the exact "tok" document


def test_fuzzy_prefix_true_matches_documents_starting_with_the_term(
    fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(field=FieldRef("content"), text="tok", distance=0, prefix=True)
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [1, 2, 4]  # tokyo, tokio, tok


# -- pattern_normalizer with several forms: a document matching only one of
# the offered forms is still found. Uses a synthetic normalizer (not real
# stemming) so the test does not depend on stemmer behavior, only on the
# combination mechanism. --


@pytest.fixture(scope="module")
def two_form_tindex() -> TIndex:
    return _content_index({10: "grey", 11: "gray"})


@pytest.mark.parametrize(
    ("pattern_normalizer", "expected_ids"),
    [
        pytest.param(str, [10], id="one-form"),
        pytest.param(lambda t: (t, t.replace("e", "a")), [10, 11], id="two-forms"),
    ],
)
def test_fuzzy_normalizer_forms_select_the_matching_spellings(
    pattern_normalizer: PatternNormalizer, expected_ids: list[int], two_form_tindex: TIndex
) -> None:
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=pattern_normalizer)]
    )
    node = ast.Fuzzy(field=FieldRef("content"), text="grey", distance=0)
    query = emit_ast(node, two_form_tindex, registry)
    assert search_ids(two_form_tindex[0], query) == expected_ids


# -- Scoring: matching several normalizer forms is worth no more than
# matching one. The normalizer offers a folded and a truncated form;
# "invoice" reaches both, "invo" only the truncated one. --


def _scores(index: tantivy.Index, query: tantivy.Query) -> dict[int, float]:
    searcher = index.searcher()
    return {
        searcher.doc(addr).to_dict()["id"][0]: round(score, 3)
        for score, addr in searcher.search(query, limit=10).hits
    }


@pytest.fixture(scope="module")
def form_overlap_tindex() -> TIndex:
    return _content_index({1: "invoice", 2: "invo"})


def _truncating_normalizer(text: str) -> tuple[str, str]:
    folded = text.lower()
    return (folded, folded[:-1])


def test_fuzzy_multiple_matching_forms_do_not_double_the_score(
    form_overlap_tindex: TIndex,
) -> None:
    registry = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=_truncating_normalizer)]
    )
    node = ast.Fuzzy(field=FieldRef("content"), text="invoce", distance=1, prefix=True)
    query = emit_ast(node, form_overlap_tindex, registry)
    single_form_query = tantivy.Query.fuzzy_term_query(
        form_overlap_tindex[1],
        "content",
        "invoc",
        distance=1,
        prefix=True,
        transposition_cost_one=True,
    )
    # Every score equals the single-form query's, including id 1's, which a
    # Should boolean would double by matching both forms.
    assert _scores(form_overlap_tindex[0], query) == _scores(
        form_overlap_tindex[0], single_form_query
    )


# -- Structural composition: Boosted/Not/AndNot with a
# Fuzzy operand all work "for free" via the existing generic visitor
# methods. Pinned with real search results, not just "no exception". --


def test_boosted_fuzzy_still_matches(fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry) -> None:
    node = ast.Boosted(
        child=ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=1), boost=2.0
    )
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [1, 2]


def test_not_fuzzy_excludes_matches(fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry) -> None:
    # A genuine ast.Not, not AndNot(Every, ...): visit_not builds a single
    # all-negative MustNot clause and is the one case that exercises
    # _pad_if_all_negative (the "All-MustNot boolean groups need padding"
    # invariant CLAUDE.md calls out). AndNot(Every, Fuzzy) below has a
    # Must clause too and never touches that padding path at all, so it is
    # not a substitute for this test.
    node = ast.Not(child=ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=1))
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [3, 4]  # everything except tokyo/tokio


def test_andnot_with_every_positive_and_fuzzy_negative_excludes_matches(
    fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.AndNot(
        positive=ast.Every(),
        negative=ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=1),
    )
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [3, 4]  # everything except tokyo/tokio


def test_andnot_with_fuzzy_positive_side(fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry) -> None:
    node = ast.AndNot(
        positive=ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=1),
        negative=ast.Term(field=FieldRef("content"), text="tokio"),
    )
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [1]  # tokyo, with tokio excluded


# -- End-to-end: Or(Term(...), Fuzzy(...)), the shape a host builds when
# it adds a typo-tolerant companion clause alongside an exact match. --


def test_or_of_term_and_fuzzy_matches_both_exact_and_typo(
    fuzzy_tindex: TIndex, fuzzy_ereg: FieldRegistry
) -> None:
    node = ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="unrelated"),
            ast.Fuzzy(field=FieldRef("content"), text="tokyo", distance=1),
        )
    )
    query = emit_ast(node, fuzzy_tindex, fuzzy_ereg)
    assert search_ids(fuzzy_tindex[0], query) == [1, 2, 3]


# -- Supported kinds on the shared fixtures: every other real-search
# assertion in this file runs against a purpose-built TEXT index. Here a
# one-edit typo is searched against tindex/ereg's shared TEXT ("content")
# and KEYWORD ("tag") fields, pinning that Fuzzy does a real,
# document-matching search on both supported kinds, not just that emit
# does not raise. --


@pytest.mark.parametrize(
    ("field", "text", "expected_ids"),
    [
        # Doc 1's content holds "invoice".
        pytest.param("content", "invoise", [1], id="text"),
        # Docs 1 and 2 are tagged "billing".
        pytest.param("tag", "billng", [1, 2], id="keyword"),
    ],
)
def test_fuzzy_on_text_and_keyword_fields_matches_a_real_document(
    field: str, text: str, expected_ids: list[int], tindex: TIndex, ereg: FieldRegistry
) -> None:
    node = ast.Fuzzy(field=FieldRef(field), text=text, distance=1)
    query = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], query) == expected_ids
