"""Wildcard / Prefix / Every(field) emission.

Every expected doc-id list below was derived by running Python's
``fnmatch.fnmatch()`` (whoosh's own glob engine: ``query.Wildcard`` compiles
its pattern with ``fnmatch.translate``) over the fixture's *analyzed* tokens,
not by eyeballing the raw document text. See the module-level token map.
"""

import fnmatch
from collections.abc import Callable

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters.tantivy_ import glob_to_regex
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import emit_
from .conftest import emit_ast
from .conftest import search_ids

# The analyzed (== indexed) tokens for each fixture doc, per field. Used by
# ``fnmatch_ids`` below to keep the expected values honest.
TITLE_TOKENS = {
    1: ["billing", "2020"],
    2: ["billing", "2019"],
    3: ["wärrantyplan"],
    4: ["report", "2020"],
}
CONTENT_TOKENS = {
    1: ["invoice", "total", "amount"],
    2: ["receipt", "shopname", "product1"],
    3: ["plan", "warranty", "basement"],
    4: ["shopname", "product1", "product2"],
}


def fnmatch_ids(tokens: dict[int, list[str]], pattern: str) -> list[int]:
    """The doc ids fnmatch (the ground-truth oracle) says ``pattern`` hits."""
    pattern = pattern.lower()  # == the fields' pattern_normalizer
    return sorted(
        doc_id for doc_id, toks in tokens.items() if any(fnmatch.fnmatch(t, pattern) for t in toks)
    )


# -- glob_to_regex unit-level behavior ---------------------------------------


@pytest.mark.parametrize(
    ("pattern", "normalizer", "expected"),
    [
        pytest.param("produ*1", None, "produ.*1", id="star-becomes-dot-star"),
        pytest.param("a?b", None, "a.b", id="question-mark-becomes-dot"),
        pytest.param("Wär*", str.lower, "wär.*", id="normalizer-applied-to-literal-run"),
        # A literal "." must not become "any character".
        pytest.param("a.b*", None, "a\\.b.*", id="literal-metachar-escaped"),
        pytest.param("202[0-3]", None, "202[0-3]", id="character-class-passthrough"),
        # fnmatch spells negation "[!...]"; the regex dialect spells it "[^...]".
        pytest.param("202[!0-3]", None, "202[^0-3]", id="negated-class-fnmatch-to-regex-dialect"),
        # fnmatch.translate("202[0-3") == "(?s:202\\[0\\-3)\\z": the unmatched
        # "[" is an ordinary character, not a class opener.
        pytest.param("202[0-3", None, "202\\[0\\-3", id="unclosed-bracket-is-literal"),
        # Only literal runs go through the normalizer; the class body is passed
        # through as class syntax.
        pytest.param(
            "AB[0-3]*", str.lower, "ab[0-3].*", id="class-body-not-normalized-or-escaped-away"
        ),
        # A "*" immediately following a class collapses runs the same as bare
        # "**" (the `while pattern[i]=="*"` collapse only fires after `flush()`,
        # not right after a class: this is really exercising the "**" collapse
        # itself with a class in front of it).
        pytest.param("a**b", None, "a.*b", id="collapses-runs-of-star"),
        # fnmatch's bracket parser treats a "]" right after "[" (or "[!") as an
        # ordinary member of the class, not the closer.
        pytest.param("[]a]", None, "[]a]", id="leading-bracket-is-literal-member"),
        pytest.param("[!]a]", None, "[^]a]", id="negated-leading-bracket-is-literal-member"),
        # A trailing "-" with nothing after it inside the class is appended to
        # the previous chunk rather than starting a new (empty) one.
        pytest.param("[a-]", None, "[a\\-]", id="trailing-hyphen-appends-to-previous-chunk"),
        # Multiple "-"-separated chunks where a later chunk's start sorts before
        # the previous chunk's end collapse into one chunk (an actual [9-1]
        # range is invalid in a regex; CPython's fnmatch treats it as a literal
        # sequence instead by merging the empty range away).
        pytest.param("[9-1-5-3]", None, "[\\-]", id="empty-range-chunks-are-merged-away"),
    ],
)
def test_glob_to_regex(
    pattern: str, normalizer: Callable[[str], str] | None, expected: str
) -> None:
    assert glob_to_regex(pattern, normalizer) == expected


def test_glob_to_regex_escapes_class_internal_set_operators(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # Python's `re` accepts "[a[]"; Rust's regex crate (tantivy) reads "[",
    # "&" and "~" inside a class as nested-class / set-operator syntax and
    # rejects the pattern. Escaping them keeps both engines happy and does
    # not change the matched language.
    assert glob_to_regex("[a[]", None) == "[a\\[]"
    assert glob_to_regex("[a&]", None) == "[a\\&]"
    # ...and tantivy must actually accept the result.
    q = emit_ast(ast.Wildcard(field=FieldRef("title"), pattern="[a[]"), tindex, ereg)
    assert search_ids(tindex[0], q) == []


# -- wildcard emission -------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "tokens", "pattern", "expected"),
    [
        pytest.param("title", TITLE_TOKENS, "Wär*", [3], id="diacritic-prefix-star"),
        pytest.param("content", CONTENT_TOKENS, "produ*1", [2, 4], id="infix-star"),
        pytest.param("content", CONTENT_TOKENS, "product?", [2, 4], id="question-mark"),
        # paperless-ngx issue #13568: saved views use "202[0-3]"-style bracket
        # classes. Constructed as a Wildcard node directly (whoosh's
        # WildcardPlugin only *tags* text containing "*"/"?", so bare
        # "202[0-3]" would lex as a Term) to exercise the emitter's raw class
        # handling.
        pytest.param("title", TITLE_TOKENS, "202[0-3]", [1, 4], id="character-class-13568"),
        pytest.param("title", TITLE_TOKENS, "201[!0-8]", [2], id="negated-character-class"),
        # An unmatched "[" is a literal character; no fixture token contains one,
        # so this matches nothing: in contrast to the closed-class form above,
        # which matches [1, 4].
        pytest.param("title", TITLE_TOKENS, "202[0-3", [], id="unclosed-bracket-is-literal"),
        # The exact wildcard shape from paperless-ngx issue #13568.
        pytest.param("title", TITLE_TOKENS, "*202[0-3]", [1, 4], id="13568-leading-star-class"),
        pytest.param("title", TITLE_TOKENS, "*201[0-9]", [2], id="13568-negated"),
        # Reversed character range; produces empty regex pattern (?!) which
        # tantivy doesn't support. Should emit a query matching nothing, not raise.
        pytest.param("title", TITLE_TOKENS, "x[z-a]*", [], id="reversed-char-class-raises"),
        # Other empty-class forms.
        pytest.param("title", TITLE_TOKENS, "[]", [], id="empty-bracket-class"),
    ],
)
def test_wildcard_emission(
    tindex: TIndex,
    ereg: FieldRegistry,
    field: str,
    tokens: dict[int, list[str]],
    pattern: str,
    expected: list[int],
) -> None:
    # Keep the fnmatch oracle result honest alongside the emitter's result.
    assert fnmatch_ids(tokens, pattern) == expected
    q = emit_ast(ast.Wildcard(field=FieldRef(field), pattern=pattern), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- prefix emission ---------------------------------------------------------


def test_prefix(tindex: TIndex, ereg: FieldRegistry) -> None:
    expected = fnmatch_ids(CONTENT_TOKENS, "shopn*")
    assert expected == [2, 4]
    q = emit_ast(ast.Prefix(field=FieldRef("content"), text="shopn"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


def test_prefix_normalizes_and_escapes(tindex: TIndex, ereg: FieldRegistry) -> None:
    # Prefix text is a *literal*: it is normalized then regex-escaped, so a
    # "[" in it matches only a real "[" (nothing in the fixture).
    q = emit_ast(ast.Prefix(field=FieldRef("title"), text="Wär"), tindex, ereg)
    assert search_ids(tindex[0], q) == [3]
    q = emit_ast(ast.Prefix(field=FieldRef("title"), text="202[0-3]"), tindex, ereg)
    assert search_ids(tindex[0], q) == []


# -- Every(field) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param(None, [1, 2, 3, 4, 5], id="unfielded-matches-all-docs"),
        pytest.param("asn", [1, 2, 3, 4, 5], id="fast-field-matches-all-docs"),
        # 'tag' is a non-fast KEYWORD field -> regex ".*" fallback; doc 3 has no
        # tags at all, so it is excluded.
        pytest.param("tag", [1, 2, 4], id="non-fast-text-field-excludes-doc-without-value"),
    ],
)
def test_every_field(
    tindex: TIndex, ereg: FieldRegistry, field: str | None, expected: list[int]
) -> None:
    ref = FieldRef(field) if field is not None else None
    q = emit_ast(ast.Every(field=ref), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- JSON subpath pattern backstop -------------------------------


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(
            ast.Prefix(field=FieldRef("notes", "user"), text="ali"),
            id="prefix-non-fast-subpath",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("notes", "user"), pattern="a?ice"),
            id="wildcard-non-fast-subpath",
        ),
        pytest.param(
            ast.Prefix(field=FieldRef("attrs", "user"), text="ali"),
            id="prefix-fast-subpath",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("attrs", "user"), pattern="a?ice"),
            id="wildcard-fast-subpath",
        ),
    ],
)
def test_pattern_on_json_subpath_raises_at_emit(
    tindex: TIndex, ereg: FieldRegistry, node: ast.Prefix | ast.Wildcard
) -> None:
    # A hand-built Prefix/Wildcard node bypasses the parser's parse-time
    # diagnostic entirely, so this is the backstop that catches it before
    # it can reach the silent-wrong-results regex query that used to be
    # built here: resolved.dotted_name is only ever read for
    # the error message, never handed to Query.regex_query.
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_PATTERN_ON_KIND
    assert d.divergence == 30
    # Discriminates the four cases from each other: without this they all
    # assert the same two values and stop proving which cell they came
    # from. The ref keeps its subpath, so the fast and non-fast rows are
    # distinguishable on the record alone.
    assert d.field == node.field
    assert d.field_kind is FieldKind.JSON


def test_pattern_on_plain_json_field_no_subpath_still_works(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # Control: the JSON-subpath backstop must not fire for an ordinary
    # (non-JSON) field's pattern query, proving the plain-field pattern
    # path is unaffected by the new check.
    expected = fnmatch_ids(CONTENT_TOKENS, "shopn*")
    assert expected == [2, 4]
    q = emit_ast(ast.Prefix(field=FieldRef("content"), text="shopn"), tindex, ereg)
    assert search_ids(tindex[0], q) == expected


# -- BOOLEAN_EXISTS pattern backstop ------------------


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(
            ast.Prefix(field=FieldRef("has_tag"), text="t"),
            id="prefix-fast-target",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("has_tag"), pattern="tr?e"),
            id="wildcard-fast-target",
        ),
        pytest.param(
            ast.Prefix(field=FieldRef("has_tag_kw"), text="t"),
            id="prefix-nonfast-target",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("has_tag_kw"), pattern="tr?e"),
            id="wildcard-nonfast-target",
        ),
    ],
)
def test_pattern_on_boolean_exists_field_raises_at_emit(
    tindex: TIndex, ereg: FieldRegistry, node: ast.Prefix | ast.Wildcard
) -> None:
    # A hand-built Prefix/Wildcard node bypasses the parser's parse-time
    # diagnostic entirely, so this is the backstop that catches it before
    # it can reach tantivy's raw, backend-internal "Field ... is not
    # defined in the schema" ValueError (BOOLEAN_EXISTS has no schema
    # column of its own): same shape as the JSON-subpath backstop above.
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_PATTERN_ON_KIND
    assert d.divergence == 29
    # Same discrimination as the JSON-subpath group above: the fast and
    # non-fast exists_target rows differ only in the field they name.
    assert d.field == node.field
    assert d.field_kind is FieldKind.BOOLEAN_EXISTS


def _raw_keyword_index() -> tantivy.Index:
    # A raw-tokenizer field so punctuation-bearing terms ("a!b") survive
    # indexing verbatim: the class-body characters under test would be
    # stripped by the 'default' tokenizer of the shared tindex fixture.
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("kw", stored=True, tokenizer_name="raw")
    index = tantivy.Index(sb.build())
    w = index.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_text("kw", "a!b")
    w.add_document(doc)
    w.commit()
    index.reload()
    return index


def test_wildcard_class_containing_regex_metachars_still_matches() -> None:
    # fnmatch ground truth: "a[(?!)]b" means "a, then one of ( ? ! ), then
    # b", matching "a!b". The never-matches signal must be out-of-band
    # (glob_to_regex returning None), never a substring scan of the
    # finished regex, which this legitimate class body would false-positive
    # into a silent match-nothing query.
    assert fnmatch.fnmatchcase("a!b", "a[(?!)]b")
    index = _raw_keyword_index()
    reg = FieldRegistry([FieldSpec("kw", FieldKind.KEYWORD)])
    q = emit_(ast.Wildcard(field=FieldRef("kw"), pattern="a[(?!)]b"), index=index, registry=reg)
    assert search_ids(index, q) == [1]


def test_wildcard_empty_class_still_matches_nothing() -> None:
    # Control: a genuinely empty class (a reversed range removes itself,
    # fnmatch semantics) must keep matching zero documents, now via the
    # out-of-band None signal instead of the substring scan.
    assert glob_to_regex("a[z-a]b", None) is None
    index = _raw_keyword_index()
    reg = FieldRegistry([FieldSpec("kw", FieldKind.KEYWORD)])
    q = emit_(ast.Wildcard(field=FieldRef("kw"), pattern="a[z-a]b"), index=index, registry=reg)
    assert search_ids(index, q) == []


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(
            ast.Prefix(field=FieldRef("notes"), text="al"),
            id="prefix-bare-json",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("notes"), pattern="al*"),
            id="wildcard-bare-json",
        ),
    ],
)
def test_pattern_on_bare_json_field_raises_at_emit(
    tindex: TIndex, ereg: FieldRegistry, node: ast.Prefix | ast.Wildcard
) -> None:
    # The bare-JSON sibling of the subpath backstop above, mirroring
    # visit_term/visit_phrase's identical cell: a hand-built pattern node
    # addressing a JSON field with no subpath must raise, not fall through
    # to Query.regex_query against the JSON column's path-prefixed encoded
    # term bytes (which tantivy accepts and which silently matches nothing
    # for values that exist).
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_JSON_NEEDS_SUBPATH
    # The bare field, with no subpath invented for it.
    assert d.field == FieldRef("notes")
    assert d.field_kind is FieldKind.JSON


@pytest.mark.parametrize(
    ("node", "field_kind"),
    [
        pytest.param(ast.Prefix(field=FieldRef("asn"), text="10"), FieldKind.U64, id="prefix-u64"),
        pytest.param(
            ast.Wildcard(field=FieldRef("asn"), pattern="10*"), FieldKind.U64, id="wildcard-u64"
        ),
        pytest.param(
            ast.Prefix(field=FieldRef("created"), text="20"), FieldKind.DATE, id="prefix-date"
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("created"), pattern="20*"),
            FieldKind.DATE,
            id="wildcard-date",
        ),
        pytest.param(
            ast.Prefix(field=FieldRef("added"), text="20"),
            FieldKind.DATETIME,
            id="prefix-datetime",
        ),
        pytest.param(
            ast.Wildcard(field=FieldRef("added"), pattern="20*"),
            FieldKind.DATETIME,
            id="wildcard-datetime",
        ),
    ],
)
def test_pattern_on_non_text_kind_raises_at_emit(
    tindex: TIndex, ereg: FieldRegistry, node: ast.Node, field_kind: FieldKind
) -> None:
    # The remaining kind-axis siblings: tantivy accepts a regex query
    # against a numeric or date column's encoded term bytes and silently
    # matches nothing, so every non-TEXT/KEYWORD kind must end in a
    # documented emit-time error instead. Query text can't reach these
    # cells (the parser's PATTERN_ON_NUMERIC/PATTERN_ON_BOOLEAN_EXISTS/
    # PATTERN_ON_SUBPATH diagnostic fires first);
    # only a hand-built node can, which is exactly why the emit-time
    # dispatch has to be closed over the kind axis on its own.
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_PATTERN_ON_KIND
    assert d.field_kind is field_kind


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(ast.Prefix(field=FieldRef("ghost"), text="ali"), id="prefix"),
        pytest.param(ast.Wildcard(field=FieldRef("ghost"), pattern="a?ice"), id="wildcard"),
    ],
)
def test_pattern_on_field_absent_from_schema_reports_the_mismatch(
    tindex: TIndex, ereg: FieldRegistry, node: ast.Node
) -> None:
    # A field the whoosh-compat registry knows and the tantivy schema does
    # not is an operator/deployment mismatch, never a property of the query
    # text. tantivy-py signals it with the same bare ValueError it uses for
    # a regex that busts the state cap, so the pattern path has to tell the
    # two apart: reporting PATTERN_TOO_COMPLEX/UNSUPPORTED here would mask
    # a broken deployment as a permanently unsupported query.
    #
    # The bar is the sibling cell: a plain term query on the same broken
    # field already reports the mismatch correctly through emit()'s
    # backstop, so the pattern visitors must land on the same kind and
    # cause rather than inventing their own.
    broken = FieldRegistry([*ereg, FieldSpec("ghost", FieldKind.TEXT)])
    with pytest.raises(QueryError) as term_exc:
        emit_ast(ast.Term(field=FieldRef("ghost"), text="alice"), tindex, broken)
    term_d = term_exc.value.diagnostic
    assert term_d.kind is DiagnosticKind.BACKEND_REJECTED
    assert term_d.cause is Cause.INTERNAL

    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, broken)
    d = exc.value.diagnostic
    assert d.kind is term_d.kind
    assert d.cause is term_d.cause
    assert d.field == FieldRef("ghost")
    assert d.field_kind is FieldKind.TEXT


def test_pattern_over_the_cap_on_a_real_field_is_still_unsupported(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    # The other side of the split above: the field IS in the schema, so a
    # ValueError from regex_query really is the compiled pattern exceeding
    # tantivy's 1000-state cap, which is bad input and stays a 400-shaped
    # UNSUPPORTED rather than being reclassified as a deployment fault.
    node = ast.Wildcard(field=FieldRef("content"), pattern="a" + "?" * 400)
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.PATTERN_TOO_COMPLEX
    assert d.cause is Cause.UNSUPPORTED
    assert d.field == FieldRef("content")


def test_every_field_non_fast_non_text_raises(tindex: TIndex, ereg: FieldRegistry) -> None:
    # 'asn' is registered as a fast U64 field in ereg, so build a second
    # registry with it as non-fast instead: the regex(".*") fallback
    # visit_every otherwise applies to any non-fast field only actually
    # works against a tantivy text/string field. A non-fast U64 field would
    # build a query that dies at tantivy search time; this must be caught
    # and reported clearly at emit time instead, naming the fix (make the
    # field fast).
    non_fast_registry = FieldRegistry(
        [FieldSpec("asn", FieldKind.U64, fast=False)]
        + [spec for spec in ereg if spec.name != "asn"]
    )
    with pytest.raises(QueryError) as exc:
        emit_ast(ast.Every(field=FieldRef("asn")), tindex, non_fast_registry)
    assert exc.value.diagnostic.kind is DiagnosticKind.EXISTS_REQUIRES_FAST
