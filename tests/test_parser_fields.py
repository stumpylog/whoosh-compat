# tests/test_parser_fields.py
import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
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


# -- u64 domain, quoted spellings: a double-quoted value on a U64 field becomes an
# -- ast.Phrase carrying raw text, which the term/range-only domain check
# -- never saw; it sailed through with zero diagnostics and only failed at
# -- emit time. ---------------------------------------------------------


def test_u64_negative_double_quoted_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    r = wc.parse('asn:"-5"', registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "-5"
    assert r.diagnostics[0].startchar == 4
    assert r.diagnostics[0].endchar == 8


def test_u64_too_large_double_quoted_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    too_large = str(2**64)
    r = wc.parse(f'asn:"{too_large}"', registry=reg, default_fields=["content"])
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
def test_u64_double_quoted_boundary_values_still_parse_and_match(
    reg: FieldRegistry, value: int
) -> None:
    r = wc.parse(f'asn:"{value}"', registry=reg, default_fields=["content"])
    assert r.diagnostics == ()
    assert r.ast == ast.Phrase(field=FieldRef("asn"), text=str(value), slop=1)


def test_u64_double_quoted_non_numeric_is_diagnosed_at_parse_time(reg: FieldRegistry) -> None:
    # A non-numeric double-quoted value on a U64 field must also be caught,
    # not just an out-of-domain one, mirroring the bare/single-quoted forms.
    r = wc.parse('asn:"xyz"', registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("asn")
    assert r.diagnostics[0].raw_value == "xyz"


def test_u64_double_quoted_comma_value_is_diagnosed_not_split(reg: FieldRegistry) -> None:
    # A double-quoted value on a comma_values U64 field is never split by
    # CommaValuesPlugin (that plugin only touches syntax.WordNode, and a
    # double-quoted value is a PhrasePlugin.PhraseNode, a different syntax
    # node type entirely), so it reaches the phrase path as a single raw
    # string that isn't a valid u64 either way; confirming it is diagnosed
    # here rather than silently accepted or split rules out a fourth,
    # comma-specific gap in the double-quoted spelling.
    r = wc.parse('tag_id:"1,99999999999999999999"', registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics[0].kind is DiagnosticKind.BAD_NUMBER
    assert r.diagnostics[0].field == FieldRef("tag_id")
    assert r.diagnostics[0].raw_value == "1,99999999999999999999"


# -- a wildcard/prefix pattern on a numeric field is diagnosed
# -- at parse time rather than failing at search time. Real whoosh silently
# -- drops the wildcard character and searches the (mangled) literal prefix
# -- instead (verified against the oracle: `type_id:1*` parses to
# -- `Term('type_id', <bytes for int 1>)`), which is a whoosh defect, not
# -- intended semantics, so it is not reproduced here.


@pytest.mark.parametrize(
    ("query", "raw_value"),
    [
        pytest.param("asn:1*", "1*", id="trailing-star-prefix-fold"),
        pytest.param("asn:1?", "1?", id="question-mark-wildcard"),
        pytest.param("asn:1[2-3]*", "1[2-3]*", id="bracket-class-wildcard"),
        pytest.param("asn:*1", "*1", id="leading-star-wildcard"),
    ],
)
def test_wildcard_on_u64_field_is_diagnosed(reg: FieldRegistry, query: str, raw_value: str) -> None:
    r = wc.parse(query, registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics
    assert r.diagnostics[0].kind is DiagnosticKind.PATTERN_ON_NUMERIC
    assert r.diagnostics[0].field == FieldRef("asn")
    # raw_value carries the pattern exactly as the user typed it, for every
    # spelling: the trailing-star Prefix fold (do_wildcards strips the "*"
    # before prefix_query runs) re-appends it when building the diagnostic,
    # since a host rendering this error would otherwise quote a value
    # containing no wildcard at all.
    assert r.diagnostics[0].raw_value == raw_value


def test_bare_star_on_u64_field_is_still_an_existence_match(reg: FieldRegistry) -> None:
    # The "*"-alone case (the Every/existence special case) is unaffected: this
    # entry is about a genuine wildcard *pattern*, not the bare-star
    # simplification.
    assert parse("asn:*", reg) == ast.Every(field=FieldRef("asn"))


# -- DIVERGENCES.md entry 30: a wildcard/prefix pattern on a JSON subpath is diagnosed at
# -- parse time rather than silently regexing the whole JSON field's
# -- path-prefixed encoded bytes. tantivy-py has no API that can scope a
# -- pattern query to one subpath (verified against 0.26.0: regex_query
# -- rejects a dotted field name outright), so unlike the U64 case above
# -- (a whoosh defect not reproduced) this is a genuine tantivy-py gap, and
# -- the fix is to refuse loudly, same diagnostic shape as entry 29's U64
# -- refusal (DIVERGENCES.md entry 30).


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("notes.user:ali*", id="trailing-star-prefix-fold"),
        pytest.param("notes.user:a?ice", id="question-mark-wildcard"),
        pytest.param("notes.user:al[iy]ce*", id="bracket-class-wildcard"),
    ],
)
def test_wildcard_on_json_subpath_is_diagnosed(reg: FieldRegistry, query: str) -> None:
    r = wc.parse(query, registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics
    assert r.diagnostics[0].kind is DiagnosticKind.PATTERN_ON_SUBPATH
    assert r.diagnostics[0].field == FieldRef("notes", "user")


def test_bare_star_on_json_subpath_is_still_an_existence_match(reg: FieldRegistry) -> None:
    # The "*"-alone case (existence, plain and subpath-aware) is unaffected: this
    # entry is about a genuine wildcard *pattern* on a subpath, not the
    # bare-star simplification (DIVERGENCES.md entries 20 and 29).
    assert parse("notes.user:*", reg) == ast.Every(field=FieldRef("notes", "user"))


def test_wildcard_on_json_plain_field_no_subpath_is_unaffected(reg: FieldRegistry) -> None:
    # Control: a plain (non-JSON) field's pattern path is untouched by the
    # subpath diagnostic.
    assert parse("content:invoi*", reg) == ast.Prefix(field=FieldRef("content"), text="invoi")


# -- a wildcard/prefix pattern on a BOOLEAN_EXISTS
# -- field is diagnosed at parse time too, the same shape as the U64 case
# -- above. Real whoosh executes has_tag:t* leniently, mangled to
# -- Term('has_tag', True) (the same silent-mangle defect class entry 29
# -- documents for numerics), which is a whoosh defect, not intended
# -- semantics, so it is not reproduced here.


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("has_tag:t*", id="trailing-star-prefix-fold"),
        pytest.param("has_tag:tr?e", id="question-mark-wildcard"),
        pytest.param("has_tag:[t-t]rue*", id="bracket-class-wildcard"),
    ],
)
def test_wildcard_on_boolean_exists_field_is_diagnosed(reg: FieldRegistry, query: str) -> None:
    r = wc.parse(query, registry=reg, default_fields=["content"])
    assert isinstance(r.ast, ast.ErrorLeaf)
    assert r.diagnostics
    assert r.diagnostics[0].kind is DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS
    assert r.diagnostics[0].field == FieldRef("has_tag")


def test_bare_star_on_boolean_exists_field_is_still_an_existence_match(
    reg: FieldRegistry,
) -> None:
    # The "*"-alone case (the Every/existence special case) is unaffected: this
    # entry is about a genuine wildcard *pattern*, not the bare-star
    # simplification.
    assert parse("has_tag:*", reg) == ast.Every(field=FieldRef("has_tag"))


def test_bool_words(reg: FieldRegistry) -> None:
    for word in ("t", "TRUE", "yes", "1"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=True)
    for word in ("f", "false", "NO", "0"):
        assert parse(f"has_tag:{word}", reg) == ast.Term(field=FieldRef("has_tag"), text=False)
    assert parse("has_tag:banana", reg) == ast.Term(
        field=FieldRef("has_tag"), text=True
    )  # truthy fallback


def test_bool_word_truthy_check_strips_whitespace(reg: FieldRegistry) -> None:
    # Regression: term_query's own truthy coercion didn't strip whitespace,
    # while the emitter's _is_truthy (used for hand-built AST nodes) did:
    # a parsed "  false  " (quoted, so whitespace survives into the term
    # text) incorrectly parsed as truthy, disagreeing with what emit()
    # would say about the identical text handed to it directly. Made to
    # agree by stripping in both places.
    assert parse("has_tag:'  false  '", reg) == ast.Term(field=FieldRef("has_tag"), text=False)
    assert parse("has_tag:'  yes  '", reg) == ast.Term(field=FieldRef("has_tag"), text=True)


def test_bool_word_empty_after_strip_is_falsy(reg: FieldRegistry) -> None:
    # A quoted empty value strips down to "", which sat outside the falses
    # tuple ("" not in ("f", "false", "no", "0")) and so read as truthy: the
    # only shape where whoosh-compat's stripped rule actually disagreed with
    # whoosh (real whoosh's bool("") fallthrough is also False). Treating an
    # empty-after-strip value as falsy brings this case into agreement.
    assert parse("has_tag:''", reg) == ast.Term(field=FieldRef("has_tag"), text=False)


def test_json_subpath(reg: FieldRegistry) -> None:
    assert parse("notes.user:alice", reg) == ast.Term(field=FieldRef("notes", "user"), text="alice")


def test_json_unregistered_subpath_demotes(reg: FieldRegistry) -> None:
    t = parse("notes.body:x", reg)
    assert not isinstance(t, ast.Term) or t.field != FieldRef("notes", "body")


def test_json_bare_field_name_demotes(reg: FieldRegistry) -> None:
    # Bare-JSON demotion: notes:foo (a JSON field addressed with no subpath) used to
    # parse cleanly to Term(field='notes', text='foo') and then raise
    # QueryEmitError at emit(), violating "parsing clean means emitting is
    # safe". Demoted the same way an unknown field is: no diagnostic, no
    # field='notes' anywhere in the result.
    t = parse("notes:foo", reg)
    assert not isinstance(t, ast.Term) or t.field != FieldRef("notes")


def test_json_bare_field_name_bare_star_is_existence_not_demoted(reg: FieldRegistry) -> None:
    # The demotion above must not swallow the one bare
    # JSON shape that already worked before it, the bare-star existence
    # check. "notes:*" is not a literal term/pattern to demote, it's the
    # same existence special case carved out for U64/BOOLEAN_EXISTS
    # (DIVERGENCES.md entries 20 and 29), so it must still reach
    # Every(FieldRef('notes')) with no diagnostics.
    r = wc.parse("notes:*", registry=reg, default_fields=["content", "title"])
    assert not r.diagnostics
    assert r.ast == ast.Every(field=FieldRef("notes"))


def test_json_subpath_bare_star_unaffected_by_bare_name_carve_out(reg: FieldRegistry) -> None:
    # A subpath existence check ("notes.user:*") was never
    # demoted in the first place: make_ref's dotted-name branch already
    # recognizes it. This pins that the bare-name carve-out above doesn't
    # change that path.
    r = wc.parse("notes.user:*", registry=reg, default_fields=["content", "title"])
    assert not r.diagnostics
    assert r.ast == ast.Every(field=FieldRef("notes", "user"))


def test_numeric_range(reg: FieldRegistry) -> None:
    assert parse("asn:[10 TO 20]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=20, incl_lo=True, incl_hi=True
    )


def test_numeric_range_open(reg: FieldRegistry) -> None:
    assert parse("asn:[10 TO]", reg) == ast.NumericRange(
        field=FieldRef("asn"), lo=10, hi=None, incl_lo=True, incl_hi=True
    )


# -- quoted-star existence: a quoted star on a numeric or boolean field is an
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


@pytest.mark.parametrize(
    ("query", "kind", "field_kind"),
    [
        pytest.param("asn:1*", DiagnosticKind.PATTERN_ON_NUMERIC, FieldKind.U64, id="numeric"),
        pytest.param(
            "has_tag:t*",
            DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
            FieldKind.BOOLEAN_EXISTS,
            id="boolean-exists",
        ),
        pytest.param(
            "notes.user:fo*",
            DiagnosticKind.PATTERN_ON_SUBPATH,
            FieldKind.JSON,
            id="json-subpath",
        ),
    ],
)
def test_pattern_diagnostics_split_by_field_kind(
    reg: FieldRegistry, query: str, kind: DiagnosticKind, field_kind: FieldKind
) -> None:
    """A single collapsed pattern kind forced a host to match on prose or
    re-resolve the field to tell numeric, boolean-exists and subpath apart.
    """
    result = wc.parse(query, registry=reg, default_fields=["content"])
    (d,) = result.diagnostics
    assert d.kind is kind
    assert d.cause is Cause.UNSUPPORTED
    assert d.field_kind is field_kind


@pytest.mark.parametrize(
    ("query", "kind", "field_kind"),
    [
        pytest.param("asn:nope", DiagnosticKind.BAD_NUMBER, FieldKind.U64, id="u64-term"),
        pytest.param("created:nope", DiagnosticKind.BAD_DATE, FieldKind.DATE, id="date-term"),
        pytest.param("added:nope", DiagnosticKind.BAD_DATE, FieldKind.DATETIME, id="datetime-term"),
    ],
)
def test_value_diagnostics_carry_field_kind(
    reg: FieldRegistry, query: str, kind: DiagnosticKind, field_kind: FieldKind
) -> None:
    result = wc.parse(query, registry=reg, default_fields=["content"])
    (d,) = result.diagnostics
    assert d.kind is kind
    assert d.cause is Cause.INVALID_INPUT
    assert d.field_kind is field_kind


@pytest.mark.parametrize(
    ("query", "entry"),
    [
        pytest.param("asn:1*", 29, id="numeric"),
        pytest.param("has_tag:t*", 29, id="boolean-exists"),
        pytest.param("notes.user:fo*", 30, id="json-subpath"),
    ],
)
def test_pattern_divergences_are_machine_readable(
    reg: FieldRegistry, query: str, entry: int
) -> None:
    """Entry 29 covers numeric AND boolean-exists; entry 30 covers subpaths."""
    (d,) = wc.parse(query, registry=reg, default_fields=["content"]).diagnostics
    assert d.divergence == entry
