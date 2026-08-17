# tests/test_parser_basics.py
import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry


def parse(q: str, reg: FieldRegistry) -> ast.Node:
    return wc.parse(q, registry=reg, default_fields=["content", "title"]).ast


def test_implicit_and(reg: FieldRegistry) -> None:
    assert parse("aaa bbb", reg) == ast.And(
        children=(
            ast.Or(
                children=(
                    ast.Term(field=FieldRef("content"), text="aaa"),
                    ast.Term(field=FieldRef("title"), text="aaa"),
                )
            ),
            ast.Or(
                children=(
                    ast.Term(field=FieldRef("content"), text="bbb"),
                    ast.Term(field=FieldRef("title"), text="bbb"),
                )
            ),
        )
    )


def test_explicit_or(reg: FieldRegistry) -> None:
    t = parse("title:aaa OR title:bbb", reg)
    assert t == ast.Or(
        children=(
            ast.Term(field=FieldRef("title"), text="aaa"),
            ast.Term(field=FieldRef("title"), text="bbb"),
        )
    )


def test_lowercase_and_is_text(reg: FieldRegistry) -> None:
    t = parse("title:aaa and title:bbb", reg)
    assert isinstance(t, ast.And)
    assert len(t.children) == 3


def test_not_group_parens(reg: FieldRegistry) -> None:
    t = parse("title:a AND (NOT title:b AND NOT title:c)", reg)
    # parens flatten under normalize(), matching whoosh (see task-9 ruling)
    assert t == ast.And(
        children=(
            ast.Term(field=FieldRef("title"), text="a"),
            ast.Not(ast.Term(field=FieldRef("title"), text="b")),
            ast.Not(ast.Term(field=FieldRef("title"), text="c")),
        )
    )


def test_comma_values(reg: FieldRegistry) -> None:
    assert parse("tag:foo,bar", reg) == ast.And(
        children=(
            ast.Term(field=FieldRef("tag"), text="foo"),
            ast.Term(field=FieldRef("tag"), text="bar"),
        )
    )


def test_quoted_comma_not_expanded(reg: FieldRegistry) -> None:
    assert parse("tag:'foo,bar'", reg) == ast.Term(field=FieldRef("tag"), text="foo,bar")


def test_alias(reg: FieldRegistry) -> None:
    assert parse("type:invoice", reg) == ast.Term(field=FieldRef("document_type"), text="invoice")


def test_unknown_field_demotes(reg: FieldRegistry) -> None:
    t = parse("http://example.com", reg)
    # url stays one text term (analysis is emit-time) searched across default fields
    assert "http" not in [getattr(c, "field", None) for c in getattr(t, "children", (t,))]


def test_phrase(reg: FieldRegistry) -> None:
    assert parse('title:"exact words"', reg) == ast.Phrase(
        field=FieldRef("title"), text="exact words", slop=1
    )


def test_phrase_slop(reg: FieldRegistry) -> None:
    assert parse('title:"exact words"~3', reg) == ast.Phrase(
        field=FieldRef("title"), text="exact words", slop=3
    )


def test_wildcard(reg: FieldRegistry) -> None:
    assert parse("title:produ*name", reg) == ast.Wildcard(
        field=FieldRef("title"), pattern="produ*name"
    )


def test_trailing_star_prefix(reg: FieldRegistry) -> None:
    assert parse("title:produ*", reg) == ast.Prefix(field=FieldRef("title"), text="produ")


def test_bracket_class_blocks_prefix_fold(reg: FieldRegistry) -> None:
    # paperless-ngx#13568. Real whoosh folds this to Prefix('202[0-3]'): a
    # *literal* prefix: silently reinterpreting the character class as
    # ordinary text. whoosh-compat keeps it a Wildcard so the class survives.
    assert parse("title:202[0-3]*", reg) == ast.Wildcard(
        field=FieldRef("title"), pattern="202[0-3]*"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param("*202[0-3]", id="leading-star-class"),
        pytest.param("a[b]?c", id="class-plus-question-mark"),
        pytest.param("[0-9]*", id="leading-class-trailing-star"),
        pytest.param("202[0-3]*", id="trailing-class-trailing-star"),
    ],
)
def test_bracket_class_wildcard_never_folds_to_term(reg: FieldRegistry, pattern: str) -> None:
    # Any wildcard-tagged text containing "[" stays a pattern node, never a
    # plain Term (and never a literal-text Prefix).
    assert parse(f"title:{pattern}", reg) == ast.Wildcard(field=FieldRef("title"), pattern=pattern)


def test_bracket_only_text_is_a_term(reg: FieldRegistry) -> None:
    # WildcardPlugin only tags text containing "*"/"?", so a bare bracket
    # class is an ordinary term: same as whoosh. (Not folded *down* from a
    # Wildcard; never tagged as one to begin with.)
    assert parse("title:202[0-3]", reg) == ast.Term(field=FieldRef("title"), text="202[0-3]")


def test_field_star_every(reg: FieldRegistry) -> None:
    assert parse("title:*", reg) == ast.Every(field=FieldRef("title"))


def test_boost(reg: FieldRegistry) -> None:
    assert parse("title:aaa^2.5", reg) == ast.Boosted(
        ast.Term(field=FieldRef("title"), text="aaa"), 2.5
    )


def test_andnot_andmaybe_require(reg: FieldRegistry) -> None:
    assert parse("title:a ANDNOT title:b", reg) == ast.AndNot(
        ast.Term(field=FieldRef("title"), text="a"), ast.Term(field=FieldRef("title"), text="b")
    )
    assert parse("title:a ANDMAYBE title:b", reg) == ast.AndMaybe(
        ast.Term(field=FieldRef("title"), text="a"), ast.Term(field=FieldRef("title"), text="b")
    )
    assert parse("title:a REQUIRE title:b", reg) == ast.Require(
        ast.Term(field=FieldRef("title"), text="a"), ast.Term(field=FieldRef("title"), text="b")
    )


def test_dangling_minus_tolerated(reg: FieldRegistry) -> None:
    t = parse("title:a - title:b", reg)  # '-' becomes a bare term, not an error
    assert isinstance(t, ast.And)


# -- empty groups: dropped at parse time, never entering the
# -- tree, rather than becoming a live Nothing() that then propagates -------


def test_empty_group_dropped_matches_bare_term(reg: FieldRegistry) -> None:
    assert parse("foo ()", reg) == parse("foo", reg)


def test_not_of_empty_group_matches_nothing(reg: FieldRegistry) -> None:
    assert parse("NOT ()", reg) == ast.Nothing()


def test_nested_and_repeated_empty_groups_behave_consistently(reg: FieldRegistry) -> None:
    assert parse("foo (() ())", reg) == parse("foo", reg)
    assert parse("(())", reg) == ast.Nothing()


# -- consecutive bare NOTs (DIVERGENCES.md entry 35): real whoosh raises a
# -- bare IndexError for these shapes (Wrapper.query indexing an empty
# -- NotGroup); whoosh-compat's own empty-nodes guard already makes the
# -- inner, childless NOT contribute nothing instead --------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param(
            "NOT NOT title:alpha",
            ast.Term(field=FieldRef("title"), text="alpha"),
            id="double-not-cancels",
        ),
        pytest.param(
            "NOT NOT NOT title:alpha",
            ast.Not(ast.Term(field=FieldRef("title"), text="alpha")),
            id="triple-not-is-single-not",
        ),
        pytest.param(
            "title:alpha NOT NOT title:beta",
            ast.And(
                children=(
                    ast.Term(field=FieldRef("title"), text="alpha"),
                    ast.Term(field=FieldRef("title"), text="beta"),
                )
            ),
            id="double-not-mid-query-cancels",
        ),
    ],
)
def test_consecutive_bare_nots_parse_instead_of_raising(
    reg: FieldRegistry, query: str, expected: ast.Node
) -> None:
    res = wc.parse(query, registry=reg, default_fields=["content", "title"])
    assert not res.diagnostics
    assert res.ast == expected


# -- pathological parenthesis nesting: parsing never raises for
# -- query input, even input that would blow the interpreter's recursion
# -- limit if the parser and normalize() traversed it recursively. A query
# -- past the nesting cap gets a diagnostic instead of a RecursionError.


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(50, id="healthy-depth-below-cap"),
    ],
)
def test_paren_nesting_below_cap_has_no_diagnostic(reg: FieldRegistry, depth: int) -> None:
    query = "(" * depth + "content:a" + ")" * depth
    result = wc.parse(query, registry=reg, default_fields=["content", "title"])
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(500, id="depth-that-crashed-normalize-pre-fix"),
        pytest.param(5000, id="depth-well-beyond-cap"),
    ],
)
def test_paren_nesting_beyond_cap_reports_diagnostic_instead_of_raising(
    reg: FieldRegistry, depth: int
) -> None:
    query = "(" * depth + "content:a" + ")" * depth
    result = wc.parse(query, registry=reg, default_fields=["content", "title"])
    assert result.diagnostics != ()
    assert any(d.kind is DiagnosticKind.TOO_DEEP for d in result.diagnostics)
    assert any(isinstance(n, ast.ErrorLeaf) for n in _flatten(result.ast))


def _flatten(node: ast.Node) -> list[ast.Node]:
    """Collects a node and every descendant reachable through the AST's
    various child-holding attributes, for assertions that just need to know
    whether an ErrorLeaf is present *somewhere* in the tree.
    """

    out = [node]
    for attr in ("children",):
        val = getattr(node, attr, None)
        if val is not None:
            for child in val:
                out.extend(_flatten(child))
    for attr in ("child", "positive", "negative", "required", "optional", "scored", "filter_only"):
        val = getattr(node, attr, None)
        if isinstance(val, ast.Node):
            out.extend(_flatten(val))
    return out
