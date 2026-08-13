from collections.abc import Callable

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
from whoosh_compat.fields import Multitoken

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import search_ids


def test_term(tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]) -> None:
    q = emit_ast(parse("content:invoice"), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_emit_has_no_schema_parameter(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    # issue #27: schema is recoverable from index.schema, so every call site
    # could pass a value that could be derived (and could pass an
    # inconsistent pair); dropped from the public signature.
    node = parse("content:invoice")
    q = emit_(node, index=tindex[0], registry=ereg)
    assert search_ids(tindex[0], q) == [1]
    with pytest.raises(TypeError, match="schema"):
        emit_(node, index=tindex[0], schema=tindex[1], registry=ereg)  # type: ignore[call-arg]


def test_multitoken_and(tindex: TIndex, ereg: FieldRegistry) -> None:
    # A single field value with multiple tokens, combined per Multitoken
    # resolution (DEFAULT -> enclosing group semantics; top level == AND).
    # docs 2 and 4 both contain "shopname" and "product1" in content.
    node = ast.Term(field=FieldRef("content"), text="shopname product1")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]


def test_u64(tindex: TIndex, ereg: FieldRegistry) -> None:
    q = emit_ast(ast.Term(field=FieldRef("asn"), text=100), tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_u64_term_non_numeric_text_raises_query_emit_error(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # issue #24: parsed input can't reach this (term_query diagnoses a bad
    # U64 value at parse time), but a hand-built ast.Term can. visit_phrase's
    # U64 branch already wrapped int(); visit_term's didn't.
    node = ast.Term(field=FieldRef("asn"), text="notanumber")
    with pytest.raises(QueryEmitError, match="notanumber"):
        emit_ast(node, tindex, ereg)


def test_date_kind_term_raises_unsupported_query_error(tindex: TIndex, ereg: FieldRegistry) -> None:
    # issue #24: DATE/DATETIME term emission was never implemented (the
    # parser always converts these via DateParserPlugin first), but a
    # hand-built ast.Term addressing a DATE field bypasses that; it used to
    # raise a bare NotImplementedError.
    node = ast.Term(field=FieldRef("created"), text="2020-01-01")
    with pytest.raises(UnsupportedQueryError, match="DATE"):
        emit_ast(node, tindex, ereg)


def test_zero_token_term_dropped(tindex: TIndex, ereg: FieldRegistry) -> None:
    grp = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Term(field=FieldRef("content"), text="!!!"),
        )
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_zero_token_term_standalone_matches_nothing(tindex: TIndex, ereg: FieldRegistry) -> None:
    q = emit_ast(ast.Term(field=FieldRef("content"), text="!!!"), tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_zero_token_term_dropped_through_boosted(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Regression: a zero-token term wrapped in Boosted must still be
    # dropped from the enclosing And, not turned into a live-but-
    # unmatchable Must clause (boost_query(empty_query(), ...)) that would
    # wrongly zero out the whole group.
    grp = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Boosted(child=ast.Term(field=FieldRef("content"), text="!!!"), boost=2.0),
        )
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_zero_token_term_dropped_through_nested_boosted(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    grp = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Boosted(
                child=ast.Boosted(child=ast.Term(field=FieldRef("content"), text="!!!"), boost=1.5),
                boost=2.0,
            ),
        )
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


def test_all_dropped_through_boosted_is_empty(tindex: TIndex, ereg: FieldRegistry) -> None:
    grp = ast.And(
        children=(ast.Boosted(child=ast.Term(field=FieldRef("content"), text="!!!"), boost=2.0),)
    )
    q = emit_ast(grp, tindex, ereg)
    assert search_ids(tindex[0], q) == []


def test_zero_token_term_dropped_through_boosted_parsed(
    tindex: TIndex, ereg: FieldRegistry, parse: Callable[[str], ast.Node]
) -> None:
    node = parse("invoice !!!^2")
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == [1]


# -- _is_truthy's raw-string branch (BOOLEAN_EXISTS text not yet coerced) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("false", [3, 5], id="str-false-is-falsy"),
        pytest.param("0", [3, 5], id="str-zero-is-falsy"),
        pytest.param("YES", [1, 2, 4], id="str-yes-is-truthy-case-insensitive"),
        pytest.param("  false  ", [3, 5], id="str-falsy-strips-whitespace"),
    ],
)
def test_boolean_exists_raw_string_text(
    tindex: TIndex, ereg: FieldRegistry, text: str, expected: list[int]
) -> None:
    node = ast.Term(field=FieldRef("has_tag"), text=text)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- _resolve() error paths --------------------------------------------------


def test_unfielded_term_raises(tindex: TIndex, ereg: FieldRegistry) -> None:
    with pytest.raises(QueryEmitError, match="unfielded"):
        emit_ast(ast.Term(field=None, text="x"), tindex, ereg)


def test_unknown_field_raises(tindex: TIndex, ereg: FieldRegistry) -> None:
    with pytest.raises(QueryEmitError, match="unknown field"):
        emit_ast(ast.Term(field=FieldRef("nosuchfield"), text="x"), tindex, ereg)


def test_json_field_term_without_subpath_raises(tindex: TIndex, ereg: FieldRegistry) -> None:
    with pytest.raises(QueryEmitError, match="JSON field"):
        emit_ast(ast.Term(field=FieldRef("notes"), text="x"), tindex, ereg)


#: multitoken resolution: FIRST / PHRASE / OR (DEFAULT/AND is covered by
# test_multitoken_and above) -------------------------------------------------


def _multitoken_registry(mode: Multitoken) -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec(
                "content", FieldKind.TEXT, analyzer=lambda t: t.lower().split(), multitoken=mode
            ),
        ]
    )


@pytest.mark.parametrize(
    ("mode", "text", "expected"),
    [
        # FIRST: only the first token is searched: "shopname" alone matches
        # both docs 2 and 4 regardless of what follows it.
        pytest.param(Multitoken.FIRST, "shopname bogus", [2, 4], id="first-uses-only-first-token"),
        # PHRASE: an exact adjacent-token match (tantivy phrase slop=0).
        pytest.param(
            Multitoken.PHRASE, "shopname product1", [2, 4], id="phrase-requires-adjacency"
        ),
        pytest.param(Multitoken.PHRASE, "product1 shopname", [], id="phrase-order-matters"),
        # OR: any token matching is enough: "product2" alone only hits doc 4,
        # but paired with "bogus" (which matches nothing) still hits doc 4 via OR.
        pytest.param(Multitoken.OR, "product2 bogus", [4], id="or-any-token-matches"),
    ],
)
def test_multitoken_modes(tindex: TIndex, mode: Multitoken, text: str, expected: list[int]) -> None:
    ereg = _multitoken_registry(mode)
    node = ast.Term(field=FieldRef("content"), text=text)
    q = emit_ast(node, tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- registered non-JSON field whose name contains a dot --------------------


def _dotted_plain_field_fixture() -> tuple[tantivy.Index, tantivy.Schema, FieldRegistry]:
    """A standalone tantivy index/registry with a plain (non-JSON) TEXT
    field whose name contains a dot, alongside "content". `_term_drop_tokens`
    used to route any field name containing "." straight to `resolve_json`,
    regardless of whether that field was actually registered as JSON: for a
    registered plain field like "field.with.dots" here, `resolve_json`
    returns None (it is not JSON), so the zero-token drop check never ran,
    and the field's own zero-token value survived as a live empty query
    that could wrongly kill an enclosing group.

    Self-contained (not the shared `tindex`/`ereg` fixtures) because a
    dotted field name has to actually exist in the tantivy schema for
    ``Query.term_query`` to accept it, and the shared schema has no such
    field.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    sb.add_text_field("field.with.dots", stored=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, content, dotted in [(1, "invoice total amount", "anything"), (2, "other", "")]:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("content", content)
        if dotted:
            doc.add_text("field.with.dots", dotted)
        w.add_document(doc)
    w.commit()
    index.reload()

    registry = FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: t.lower().split()),
            FieldSpec(
                "field.with.dots",
                FieldKind.TEXT,
                analyzer=lambda t: [w for w in t.lower().split() if w != "!!!"],
            ),
        ]
    )
    return index, schema, registry


def test_dotted_plain_field_zero_token_term_dropped() -> None:
    index, _schema, registry = _dotted_plain_field_fixture()
    grp = ast.And(
        children=(
            ast.Term(field=FieldRef("content"), text="invoice"),
            ast.Term(field=FieldRef("field.with.dots"), text="!!!"),
        )
    )
    q = emit_(grp, index=index, registry=registry)
    assert search_ids(index, q) == [1]


def test_dotted_plain_field_term_with_tokens_still_emits() -> None:
    index, _schema, registry = _dotted_plain_field_fixture()
    q = emit_(
        ast.Term(field=FieldRef("field.with.dots"), text="anything"),
        index=index,
        registry=registry,
    )
    assert search_ids(index, q) == [1]


# -- Prefix without a pattern_normalizer -------------------------------------


def test_prefix_without_normalizer(tindex: TIndex) -> None:
    # visit_prefix's `if spec.pattern_normalizer is not None` skip branch:
    # a registry field with no pattern_normalizer configured at all.
    ereg = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: t.lower().split())]
    )
    q = emit_ast(ast.Prefix(field=FieldRef("content"), text="shopn"), tindex, ereg)
    assert search_ids(tindex[0], q) == [2, 4]
