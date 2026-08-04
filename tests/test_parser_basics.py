# tests/test_parser_basics.py
import whoosh_compat as wc
from whoosh_compat import ast


def parse(q, reg, **kw):
    return wc.parse(q, registry=reg, default_fields=["content", "title"], **kw).ast


def test_implicit_and(reg):
    assert parse("aaa bbb", reg) == ast.And(children=(
        ast.Or(children=(ast.Term(field="content", text="aaa"), ast.Term(field="title", text="aaa"))),
        ast.Or(children=(ast.Term(field="content", text="bbb"), ast.Term(field="title", text="bbb"))),
    ))

def test_explicit_or(reg):
    t = parse("title:aaa OR title:bbb", reg)
    assert t == ast.Or(children=(ast.Term(field="title", text="aaa"), ast.Term(field="title", text="bbb")))

def test_lowercase_and_is_text(reg):
    t = parse("title:aaa and title:bbb", reg)
    assert isinstance(t, ast.And) and len(t.children) == 3  # 'and' is a term

def test_not_group_parens(reg):
    t = parse("title:a AND (NOT title:b AND NOT title:c)", reg)
    # parens flatten under normalize(), matching whoosh (see task-9 ruling)
    assert t == ast.And(children=(ast.Term(field="title", text="a"),
                                  ast.Not(ast.Term(field="title", text="b")),
                                  ast.Not(ast.Term(field="title", text="c"))))

def test_comma_values(reg):
    assert parse("tag:foo,bar", reg) == ast.And(children=(
        ast.Term(field="tag", text="foo"), ast.Term(field="tag", text="bar")))

def test_quoted_comma_not_expanded(reg):
    assert parse("tag:'foo,bar'", reg) == ast.Term(field="tag", text="foo,bar")

def test_alias(reg):
    assert parse("type:invoice", reg) == ast.Term(field="document_type", text="invoice")

def test_unknown_field_demotes(reg):
    t = parse("http://example.com", reg)
    # url stays one text term (analysis is emit-time) searched across default fields
    assert "http" not in [getattr(c, "field", None) for c in getattr(t, "children", (t,))]

def test_phrase(reg):
    assert parse('title:"exact words"', reg) == ast.Phrase(field="title", text="exact words", slop=1)

def test_phrase_slop(reg):
    assert parse('title:"exact words"~3', reg) == ast.Phrase(field="title", text="exact words", slop=3)

def test_wildcard(reg):
    assert parse("title:produ*name", reg) == ast.Wildcard(field="title", pattern="produ*name")

def test_trailing_star_prefix(reg):
    assert parse("title:produ*", reg) == ast.Prefix(field="title", text="produ")

def test_field_star_every(reg):
    assert parse("title:*", reg) == ast.Every(field="title")

def test_boost(reg):
    assert parse("title:aaa^2.5", reg) == ast.Boosted(ast.Term(field="title", text="aaa"), 2.5)

def test_andnot_andmaybe_require(reg):
    assert parse("title:a ANDNOT title:b", reg) == ast.AndNot(ast.Term(field="title", text="a"), ast.Term(field="title", text="b"))
    assert parse("title:a ANDMAYBE title:b", reg) == ast.AndMaybe(ast.Term(field="title", text="a"), ast.Term(field="title", text="b"))
    assert parse("title:a REQUIRE title:b", reg) == ast.Require(ast.Term(field="title", text="a"), ast.Term(field="title", text="b"))

def test_dangling_minus_tolerated(reg):
    t = parse("title:a - title:b", reg)   # '-' becomes a bare term, not an error
    assert isinstance(t, ast.And)
