# tests/test_parser_basics.py
import time

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
    # class is an ordinary term at tagging time, same as whoosh: not folded
    # *down* from a Wildcard, never tagged as one to begin with. A
    # multi-character range keeps that literal-term behavior all the way
    # through, since it isn't ambiguous with the single-character bracket
    # range shape DIVERGENCES.md entry 60 diagnoses (see
    # test_single_char_bracket_range_in_term_position_is_diagnosed below).
    assert parse("title:invoice[20-30]", reg) == ast.Term(
        field=FieldRef("title"), text="invoice[20-30]"
    )


def test_single_char_bracket_range_in_term_position_is_diagnosed(reg: FieldRegistry) -> None:
    # Unlike a multi-character range, "[0-3]" is an unambiguous
    # single-character bracket range: whoosh itself would search this as
    # the nine-character literal "202[0-3]", which essentially never
    # matches a real document. whoosh-compat diagnoses this shape instead
    # of reproducing the silent no-match (DIVERGENCES.md entry 60); see
    # tests/test_parser_fields.py for the full shape matrix.
    r = wc.parse("title:202[0-3]", registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.SINGLE_CHAR_BRACKET_RANGE


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


# -- A long run of word characters with no ":" used to cost quadratic parse
# -- time: the fieldname tagger's regex rescanned the rest of the run from
# -- every position the tag loop reached.


@pytest.mark.wall_clock
@pytest.mark.parametrize(
    "run",
    [
        pytest.param("".join(chr(0x4E00 + i % 20000) for i in range(32768)), id="cjk-run"),
        pytest.param("abcdefghij" * 3277, id="latin-run"),
        pytest.param("a.b" * 10923, id="dotted-run"),
    ],
)
def test_long_word_run_parses_in_linear_time(reg: FieldRegistry, run: str) -> None:
    """Sized so the guard cannot be flaky rather than so it is quick.
    Measured on the author's machine: about 24 s per shape before the fix
    and 0.3 s after, against the 3 s budget, so one size is enough to tell
    the two apart. Absolute seconds are hardware-specific; the durable
    claim is the shape, quadratic in the run length before and linear
    after, which this guards without measuring it.
    """
    start = time.perf_counter()
    result = wc.parse(run, registry=reg, default_fields=["content", "title"])
    elapsed = time.perf_counter() - start
    # Checked, not just timed: the run is still one term per default field.
    assert result.diagnostics == ()
    assert isinstance(result.ast, ast.Or)
    assert [(c.field, c.text) for c in result.ast.children if isinstance(c, ast.Term)] == [
        (FieldRef("content"), run),
        (FieldRef("title"), run),
    ]
    assert elapsed < 3.0


# -- An opener whose closer never comes (a range bracket, a single quote)
# -- used to cost super-linear parse time: each opener's regex scanned
# -- forward for a closer, and the range one re-tried every later "to".


@pytest.mark.wall_clock
@pytest.mark.parametrize(
    ("unit", "tail", "size"),
    [
        pytest.param("[a to ", "", 16384, id="range-with-to-no-closer"),
        pytest.param("[a ", "", 16384, id="range-bracket"),
        pytest.param("{a ", "", 16384, id="range-brace"),
        pytest.param("['a ", "", 16384, id="range-bracket-and-quote"),
        pytest.param("'a ", "", 16384, id="single-quote"),
        # A closer and a "to" both exist, but in the order that cannot
        # make a range: every bracket is closed by the same "]", and the
        # only "to" comes after it. Twice the size of the rows above,
        # because this is the one shape whose old cost (quadratic, from
        # scanning to that closer) would otherwise fit the budget here.
        pytest.param("[a ", "] to x", 32768, id="range-with-to-after-the-closer"),
    ],
)
def test_an_opener_without_a_closer_parses_in_linear_time(
    reg: FieldRegistry, unit: str, tail: str, size: int
) -> None:
    """Sized so the guard cannot be flaky rather than so it is quick.
    Measured on the author's machine at 16 KB: 4 to 6 s per shape before
    the fix, and about half an hour for the "to" shape, whose cost grew
    with the cube of the length (every later "to" was tried as a bound,
    each scanning to end of input; 4 KB of it cost 30 s). After the fix
    each shape is about a second here, against a budget of 6 s per 16 KB,
    so the pre-fix cost stays several times the budget even on a runner a
    few times slower. 8 KB would not do: the quadratic shapes already fit
    the budget there. Absolute seconds are hardware-specific; the durable claim is
    the shape, super-linear before and linear after, which this guards
    without measuring it.
    """
    query = unit * (size // len(unit)) + tail
    short = wc.parse(unit * 2 + tail, registry=reg, default_fields=["content", "title"])
    start = time.perf_counter()
    result = wc.parse(query, registry=reg, default_fields=["content", "title"])
    elapsed = time.perf_counter() - start
    # Checked, not just timed: an unclosed opener is ordinary text, and
    # repeating it adds no new words, so the long query searches for the
    # same words as two repeats of it.
    assert result.diagnostics == ()
    assert not any(isinstance(n, ast.ErrorLeaf) for n in _flatten(result.ast))
    assert _term_texts(result.ast) == _term_texts(short.ast) != set()
    assert elapsed < 6.0 * size / 16384


def _term_texts(node: ast.Node) -> set[object]:
    return {n.text for n in _flatten(node) if isinstance(n, ast.Term)}


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
