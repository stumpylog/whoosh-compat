# tests/test_parser_fields.py
import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef


def parse(q, reg, **kw):
    return wc.parse(q, registry=reg, default_fields=["content", "title"], **kw).ast


def test_u64(reg):
    assert parse("asn:123", reg) == ast.Term(field=FieldRef("asn"), text=123)


def test_u64_bad(reg):
    r = wc.parse("asn:xyz", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf) and r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    # The diagnostic must carry a real span pointing at "xyz" (offsets 4-7 in
    # "asn:xyz"), not None/None: a host turning this into an HTTP 400 needs
    # somewhere to point the user at, same as BAD_DATE diagnostics already do.
    assert r.diagnostics[0].startchar == 4
    assert r.diagnostics[0].endchar == 7
    # A host that wants a typed exception (field, raw value) rather than
    # regex-parsing the rendered message needs these carried structurally.
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "xyz"


def test_numeric_range_bad_bound_diagnostic_carries_field_and_raw_value(reg):
    r = wc.parse("asn:[xyz TO 20]", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf) and r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "xyz"


def test_bool_words(reg):
    for word in ("t", "TRUE", "yes", "1"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=True)
    for word in ("f", "false", "NO", "0"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=False)
    assert parse("has_tag:banana", reg) == ast.Term(
        field=FieldRef("has_tag"), text=True
    )  # truthy fallback


def test_json_subpath(reg):
    assert parse("notes.user:alice", reg) == ast.Term(field=FieldRef("notes", "user"), text="alice")


def test_json_unregistered_subpath_demotes(reg):
    t = parse("notes.body:x", reg)
    assert not isinstance(t, ast.Term) or t.field != FieldRef("notes", "body")


def test_numeric_range(reg):
    assert parse("asn:[10 TO 20]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=20, incl_lo=True, incl_hi=True
    )


def test_numeric_range_open(reg):
    assert parse("asn:[10 TO]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=None, incl_lo=True, incl_hi=True
    )


def test_field_boosts(reg):
    t = wc.parse(
        "aaa title:bbb",
        registry=reg,
        default_fields=["content", "title"],
        field_boosts={"title": 2.0},
    ).ast
    # expansion copy of 'aaa' into title is boosted; explicit title:bbb is NOT
    or_group = t.children[0]
    assert ast.Boosted(ast.Term(field=FieldRef("title"), text="aaa"), 2.0) in or_group.children
    assert t.children[1] == ast.Term(field=FieldRef("title"), text="bbb")
