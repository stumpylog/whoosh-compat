# tests/test_parser_fields.py
import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry


def parse(q: str, reg: FieldRegistry) -> ast.Node:
    return wc.parse(q, registry=reg, default_fields=["content", "title"]).ast


def test_u64(reg: FieldRegistry) -> None:
    assert parse("asn:123", reg) == ast.Term(field=FieldRef("asn"), text=123)


def test_u64_bad(reg: FieldRegistry) -> None:
    r = wc.parse("asn:xyz", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    # The diagnostic must carry a real span pointing at "xyz" (offsets 4-7 in
    # "asn:xyz"), not None/None: a host turning this into an HTTP 400 needs
    # somewhere to point the user at, same as BAD_DATE diagnostics already do.
    assert r.diagnostics[0].startchar == 4
    assert r.diagnostics[0].endchar == 7
    # A host that wants a typed exception (field, raw value) rather than
    # regex-parsing the rendered message needs these carried structurally.
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "xyz"


def test_numeric_range_bad_bound_diagnostic_carries_field_and_raw_value(reg: FieldRegistry) -> None:
    r = wc.parse("asn:[xyz TO 20]", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "xyz"


def test_u64_negative_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    # -5 converts fine as a Python int but is outside u64's domain; letting
    # it through used to raise a bare ValueError at tantivy-py's u64
    # extraction at emit time instead of a parse-time diagnostic.
    r = wc.parse("asn:-5", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "-5"
    assert r.diagnostics[0].startchar == 4
    assert r.diagnostics[0].endchar == 6


def test_u64_too_large_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    too_large = str(2**64)
    r = wc.parse(f"asn:{too_large}", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == too_large


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="lower-boundary"),
        pytest.param(2**64 - 1, id="upper-boundary"),
    ],
)
def test_u64_boundary_values_still_parse(reg: FieldRegistry, value: int) -> None:
    assert parse(f"asn:{value}", reg) == ast.Term(field=FieldRef("asn"), text=value)


def test_u64_negative_range_bound_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    r = wc.parse("asn:[-5 TO 20]", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "-5"


def test_u64_too_large_range_bound_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    too_large = str(2**64)
    r = wc.parse(f"asn:[0 TO {too_large}]", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == too_large


def test_u64_boundary_range_bounds_still_parse(reg: FieldRegistry) -> None:
    r = parse(f"asn:[0 TO {2**64 - 1}]", reg)
    assert r == ast.NumericRange(
        field=FieldRef("asn"), lo=0, hi=2**64 - 1, incl_lo=True, incl_hi=True
    )


# -- issue #17: a wildcard/prefix pattern on a numeric field is diagnosed
# -- at parse time rather than failing at search time. Real whoosh silently
# -- drops the wildcard character and searches the (mangled) literal prefix
# -- instead (verified against the oracle: `type_id:1*` parses to
# -- `Term('type_id', <bytes for int 1>)`), which is a whoosh defect, not
# -- intended semantics, so it is not reproduced here.


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("asn:1*", id="trailing-star-prefix-fold"),
        pytest.param("asn:1?", id="question-mark-wildcard"),
        pytest.param("asn:1[2-3]*", id="bracket-class-wildcard"),
        pytest.param("asn:*1", id="leading-star-wildcard"),
    ],
)
def test_wildcard_on_u64_field_is_diagnosed(reg: FieldRegistry, query: str) -> None:
    r = wc.parse(query, registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics
    assert r.diagnostics[0].kind is DiagnosticKind.UNKNOWN
    assert r.diagnostics[0].field == FieldRef("asn")
    # "1*" arrives here as "1": do_wildcards folds a trailing-star-only
    # wildcard into a literal Prefix *before* query()/prefix_query ever run
    # (the fold strips the "*", same as it would for a supported field kind).
    assert r.diagnostics[0].raw_value in ("1", "1?", "1[2-3]*", "*1")


def test_bare_star_on_u64_field_is_still_an_existence_match(reg: FieldRegistry) -> None:
    # The "*"-alone case (issue #16, Every/existence) is unaffected: this
    # entry is about a genuine wildcard *pattern*, not the bare-star
    # simplification.
    assert parse("asn:*", reg) == ast.Every(field=FieldRef("asn"))


def test_bool_words(reg: FieldRegistry) -> None:
    for word in ("t", "TRUE", "yes", "1"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=True)
    for word in ("f", "false", "NO", "0"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=False)
    assert parse("has_tag:banana", reg) == ast.Term(
        field=FieldRef("has_tag"), text=True
    )  # truthy fallback


def test_bool_word_truthy_check_strips_whitespace(reg: FieldRegistry) -> None:
    # issue #24: term_query's own truthy coercion didn't strip whitespace,
    # while the emitter's _is_truthy (used for hand-built AST nodes) did:
    # a parsed "  false  " (quoted, so whitespace survives into the term
    # text) incorrectly parsed as truthy, disagreeing with what emit()
    # would say about the identical text handed to it directly. Made to
    # agree by stripping in both places.
    assert parse("has_tag:'  false  '", reg) == ast.Term(field=FieldRef("has_tag"), text=False)
    assert parse("has_tag:'  yes  '", reg) == ast.Term(field=FieldRef("has_tag"), text=True)


def test_json_subpath(reg: FieldRegistry) -> None:
    assert parse("notes.user:alice", reg) == ast.Term(field=FieldRef("notes", "user"), text="alice")


def test_json_unregistered_subpath_demotes(reg: FieldRegistry) -> None:
    t = parse("notes.body:x", reg)
    assert not isinstance(t, ast.Term) or t.field != FieldRef("notes", "body")


def test_json_bare_field_name_demotes(reg: FieldRegistry) -> None:
    # issue #11: notes:foo (a JSON field addressed with no subpath) used to
    # parse cleanly to Term(field='notes', text='foo') and then raise
    # QueryEmitError at emit(), violating "parsing clean means emitting is
    # safe". Demoted the same way an unknown field is: no diagnostic, no
    # field='notes' anywhere in the result.
    t = parse("notes:foo", reg)
    assert not isinstance(t, ast.Term) or t.field != FieldRef("notes")


def test_numeric_range(reg: FieldRegistry) -> None:
    assert parse("asn:[10 TO 20]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=20, incl_lo=True, incl_hi=True
    )


def test_numeric_range_open(reg: FieldRegistry) -> None:
    assert parse("asn:[10 TO]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=None, incl_lo=True, incl_hi=True
    )


# -- issue #16: a quoted star on a numeric or boolean field is an
# -- existence match, matching the unquoted field:* form -------------------
#
# A *single*-quoted value goes through the ordinary term path (WordNode ->
# term_query) at parse time, same as an unquoted one, so it can produce the
# exact same ast.Every(field) node. A *double*-quoted value is always an
# ast.Phrase at parse time (analysis is emit-time, see ARCHITECTURE.md), so
# its equivalence to the unquoted form is necessarily an emit-time /
# search-result question instead: covered by
# tests/emitter/test_emit_terms.py's quoted-star tests.


@pytest.mark.parametrize(
    ("field", "query"),
    [
        pytest.param("asn", "asn:'*'", id="u64"),
        pytest.param("has_tag", "has_tag:'*'", id="boolean-exists"),
    ],
)
def test_single_quoted_star_matches_unquoted_ast(
    reg: FieldRegistry, field: str, query: str
) -> None:
    assert parse(query, reg) == parse(f"{field}:*", reg) == ast.Every(field=FieldRef(field))


@pytest.mark.parametrize(
    ("field", "query"),
    [
        pytest.param("asn", 'asn:"*"', id="u64"),
        pytest.param("has_tag", 'has_tag:"*"', id="boolean-exists"),
    ],
)
def test_double_quoted_star_stays_a_phrase_at_parse_time(
    reg: FieldRegistry, field: str, query: str
) -> None:
    assert parse(query, reg) == ast.Phrase(field=FieldRef(field), text="*", slop=1)


def test_field_boosts(reg: FieldRegistry) -> None:
    t = wc.parse(
        "aaa title:bbb",
        registry=reg,
        default_fields=["content", "title"],
        field_boosts={"title": 2.0},
    ).ast
    assert isinstance(t, ast.And)
    # expansion copy of 'aaa' into title is boosted; explicit title:bbb is NOT
    or_group = t.children[0]
    assert isinstance(or_group, ast.Or)
    assert ast.Boosted(ast.Term(field=FieldRef("title"), text="aaa"), 2.0) in or_group.children
    assert t.children[1] == ast.Term(field=FieldRef("title"), text="bbb")
