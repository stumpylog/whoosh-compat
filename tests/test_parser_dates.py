import time
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser import dateparse as dp
from whoosh_compat.parser import syntax
from whoosh_compat.parser.dateparse import DateErrorNode
from whoosh_compat.parser.dateparse import DateParserPlugin
from whoosh_compat.parser.dateparse import DateRangeSyntaxNode
from whoosh_compat.parser.dateparse import English
from whoosh_compat.parser.default import QueryParser
from whoosh_compat.parser.times import adatetime
from whoosh_compat.parser.times import timespan

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)


def dparse(q: str, reg: FieldRegistry) -> wc.ParseResult:
    return wc.parse(q, registry=reg, default_fields=["content"], tz=BERLIN, basedate=BASE)


def _nodes(node: ast.Node) -> Iterator[ast.Node]:
    """``node`` and, recursively, the children of any group node under it."""

    yield node
    for child in getattr(node, "children", ()):
        yield from _nodes(child)


def _plugin_for_cap_test() -> DateParserPlugin:
    return DateParserPlugin(BASE, BERLIN)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "12 december 2019 3pm to 12 december 2020 3pm",
            id="compact-meridiem-two-sided",
        ),
        pytest.param(
            "12 december 2019 3 pm to 12 december 2020 3 pm",
            id="spaced-meridiem-two-sided",
        ),
        pytest.param("12 december 2019 3 pm", id="spaced-meridiem-one-sided"),
        pytest.param("-1 week to +2 months", id="relative-two-sided"),
        pytest.param("last tuesday to next friday", id="keyword-two-sided"),
    ],
)
def test_grammar_never_exceeds_lookahead_cap(text: str) -> None:
    """The lookahead cap is a safety bound with no derivation behind it, so
    it needs a test rather than an argument.

    This proves a lower bound only: the cap cannot be smaller than the
    longest sample below without one of these cases failing. It does not
    prove an upper bound; it samples known grammar shapes, it does not
    search the grammar for its longest expression. Both meridiem spellings
    are included because that is what makes the lower bound meaningful:
    Time12 allows a space before am/pm, so "3 pm" costs two word nodes
    where "3pm" costs one, and an earlier cap derived from the compact
    spelling alone was too small by two per bound.

    Anyone extending the date grammar with a spelling longer than what is
    sampled here must add that spelling as a new case, or the cap can
    silently stop covering the grammar without this test noticing.
    """
    plugin = _plugin_for_cap_test()
    assert plugin._fully_parses(text), "sample is not a full parse, fix the sample"
    assert len(text.split()) <= plugin._UNQUOTED_LOOKAHEAD + 1


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:2020", id="bare-year-implies-year-range"),
        pytest.param("created:[2020 TO 2020]", id="explicit-same-year-range-is-equivalent"),
    ],
)
def test_year_precision(reg: FieldRegistry, query: str) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r == ast.DateRange(
        field=FieldRef("created"),
        lo=datetime(2020, 1, 1, tzinfo=UTC),
        hi=datetime(2021, 1, 1, tzinfo=UTC),
        incl_lo=True,
        incl_hi=False,
    )


def test_open_upper(reg: FieldRegistry) -> None:
    r = dparse("created:[2020 TO]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert r.hi is None


def test_yesterday_keyword(reg: FieldRegistry) -> None:
    r = dparse("added:yesterday", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_month(reg: FieldRegistry) -> None:
    r = dparse("added:'previous month'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_now_compact(reg: FieldRegistry) -> None:
    r = dparse("added:[now-7d TO now]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert (BASE.astimezone(UTC) - r.lo).days == 7
    # The upper bound is what makes this a seven-day window rather than the
    # instant seven days ago: a lower bound alone cannot tell the two apart.
    assert r.hi == BASE.astimezone(UTC)


def test_whoosh_plusminus(reg: FieldRegistry) -> None:
    r = dparse("added:'-1 week'", reg).ast
    assert isinstance(r, ast.DateRange)


def test_reversed_relative_range_swaps_like_the_absolute_case(reg: FieldRegistry) -> None:
    # DIVERGENCES.md entry 53. Both bounds are the now/offset family
    # (basedate +/- an hour), which resolves to a plain datetime rather
    # than an adatetime, same as an absolute range whose bounds are typed
    # backwards (see test_backwards_swap_carries_exactness_with_the_value
    # below): a reversed order here is the user writing the endpoints in
    # the wrong order, not an "overnight span" (that reading only applies
    # to a genuinely ambiguous bare time of day, e.g. "9pm to 5am"). The
    # forward and backward spellings must therefore produce the identical
    # 2-hour span, not a ~22-hour one.
    forward = dparse("added:[now-1h TO now+1h]", reg).ast
    backward = dparse("added:[now+1h TO now-1h]", reg).ast
    assert isinstance(forward, ast.DateRange)
    assert isinstance(backward, ast.DateRange)
    assert forward.lo == backward.lo
    assert forward.hi == backward.hi
    assert forward.hi is not None
    assert forward.lo is not None
    assert forward.hi - forward.lo == timedelta(hours=2)


def test_bad_date_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:notadate", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    # A host mapping this to a typed exception (e.g. InvalidDateQuery(field,
    # value)) needs the field name and offending text structurally, not by
    # regex-parsing the rendered message.
    assert res.diagnostics[0].field == FieldRef("added")
    assert res.diagnostics[0].raw_value == "notadate"


# --- A date value the grammar can only half-consume (DIVERGENCES entry 54) --


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:2005-01-01T00:00:00Z", id="rfc3339-utc"),
        pytest.param("added:2005-01-01T00:00:00", id="rfc3339-no-zone"),
        pytest.param("added:2005-01-01T12:30", id="rfc3339-to-the-minute"),
    ],
)
def test_bare_unquoted_timestamp_is_rejected_not_half_consumed(
    reg: FieldRegistry, query: str
) -> None:
    # DIVERGENCES.md entry 54. The colons make the grammar split the value
    # (colons separate a field name from its value, the same rule that makes
    # added:"-1 week" need its quotes), which leaves the date field holding
    # the cut-off fragment "2005-01-". Real whoosh reads that fragment as
    # "all of January 2005" -- the trailing "-" is swallowed as a separator
    # that leads nowhere -- and ANDs the rest of the timestamp onto the query
    # as loose text, so the user gets a silently wrong query and no
    # diagnostic at all. A value that only half-parses is a bad date here.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    assert not any(isinstance(n, ast.DateRange) for n in _nodes(res.ast))


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:2005-", id="year-then-dangling-dash"),
        pytest.param("added:2005-01-", id="month-then-dangling-dash"),
        pytest.param("added:2005-01-01T", id="day-then-dangling-t"),
        pytest.param("added:2005.01.", id="dotted-then-dangling-dot"),
        pytest.param("added:2005/01/", id="slashed-then-dangling-slash"),
    ],
)
def test_dangling_separator_is_a_bad_date(reg: FieldRegistry, query: str) -> None:
    # The same rule from the other side, for every non-whitespace separator
    # in the "simple" grammar's class: a separator with no date component
    # after it means the value was cut mid-token, not that the component is
    # optional. Whitespace is deliberately not in this list -- it is a clean
    # token boundary, and "simple"'s own (?=(\s|$)) guard already treats it
    # as a valid end of a value (see the boundary discussion in
    # DIVERGENCES.md entry 54).
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


@pytest.mark.parametrize(
    ("query", "lo", "hi"),
    [
        pytest.param(
            'added:"2005-01-01T00:00:00Z"',
            datetime(2005, 1, 1, tzinfo=UTC),
            datetime(2005, 1, 1, 0, 0, 1, tzinfo=UTC),
            id="quoted-value",
        ),
        pytest.param(
            "added:[2005-01-01T00:00:00Z to 2006-01-01T00:00:00Z]",
            datetime(2005, 1, 1, tzinfo=UTC),
            datetime(2006, 1, 1, 0, 0, 1, tzinfo=UTC),
            id="bracketed-bounds",
        ),
    ],
)
def test_timestamp_spellings_the_grammar_can_consume_whole_still_parse(
    reg: FieldRegistry, query: str, lo: datetime, hi: datetime
) -> None:
    # Pinned alongside the rejection above so a future change cannot "fix"
    # the bare spelling by loosening these: quoting (or bracketing) is what
    # keeps the colons out of the field-splitting grammar's way, and both
    # must keep producing a whole-timestamp range.
    res = dparse(query, reg)
    assert not res.diagnostics
    r = res.ast
    assert isinstance(r, ast.DateRange)
    assert (r.lo, r.hi) == (lo, hi)


def test_double_quoted_range_bound_is_a_bad_date_not_a_widening(reg: FieldRegistry) -> None:
    # A user who has just learned "double-quote a timestamp to protect it"
    # (test_timestamp_spellings_the_grammar_can_consume_whole_still_parse
    # above) will reasonably try the same thing inside a bracketed range
    # and gets a rejection instead. That asymmetry is inherited from real
    # whoosh, not introduced here: RangePlugin's tagging regex
    # (parser/plugins.py, ported verbatim from whoosh/qparser/plugins.py)
    # only ever strips SINGLE quotes from a bound
    # ("('[^']*?'\\s+)"/"(\\s+'[^']*?')"); a double-quoted bound keeps its
    # literal `"` characters and is handed to the date grammar as
    # '"2005-01-01T00:00:00Z"', which the grammar cannot read as a date
    # (leading `"` matches none of its start productions) any more than
    # whoosh-compat's grammar can. Confirmed directly against real whoosh
    # (../whoosh, DateParserPlugin/QueryParser): the same query there also
    # fails to build a query object (`int('"200')` inside
    # Field._parse_datestring), it just surfaces as an uncaught
    # ValueError instead of a structured diagnostic -- whoosh-compat's
    # BAD_DATE is a strictly friendlier report of the identical rejection,
    # not a new restriction. Widening to accept double-quoted bounds would
    # therefore be a pure whoosh-compat addition with no whoosh precedent,
    # weighed against here and declined: matching whoosh's syntax for
    # "what a bracket bound may look like" keeps one quoting rule instead
    # of two, and the loud BAD_DATE (not a silent wrong answer) is exactly
    # the failure mode the "don't reproduce a whoosh bug" carve-out does
    # NOT apply to, since this isn't a bug, just a documented quirk of
    # where each quote character is meaningful. paperless-ngx documents
    # this asymmetry explicitly on the host side.
    res = dparse('added:["2005-01-01T00:00:00Z" to "2006-01-01T00:00:00Z"]', reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    assert not any(isinstance(n, ast.DateRange) for n in _nodes(res.ast))


def test_single_quoted_range_bound_works_like_an_unquoted_one(reg: FieldRegistry) -> None:
    # The other half of the same RangePlugin regex: SINGLE quotes ARE
    # stripped from a bound before it reaches the date grammar, so
    # `added:['-1 week' to now]` (single-quoted, needed only to protect the
    # embedded space -- an unquoted "-1 week" would otherwise split on
    # whitespace like any other range bound) parses exactly like its
    # unquoted equivalent. This is whoosh's own escape hatch for a
    # space-containing bound, distinct from -- and not a substitute for --
    # the double-quote convention used everywhere else in the query
    # language; see the rejection test above for the quote character that
    # does NOT get this treatment inside brackets.
    quoted = dparse("added:['-1 week' to now]", reg)
    unquoted = dparse("added:[-1 week to now]", reg)
    assert not quoted.diagnostics
    assert not unquoted.diagnostics
    assert quoted.ast == unquoted.ast


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:2005-01-01T00:00:00Z", id="rfc3339-utc"),
        pytest.param("added:2005-01-01T00:00:00", id="rfc3339-no-zone"),
        pytest.param("added:2005-01-01T12:30", id="rfc3339-to-the-minute"),
    ],
)
def test_bad_date_raw_value_widens_to_cover_a_contiguous_leftover_fragment(
    reg: FieldRegistry, query: str
) -> None:
    """DIVERGENCES.md entry 58: the colon-fragmented shape entry 54 rejects
    reports the FULL value the user typed, not just the fragment the date
    grammar itself saw. The leftover text is contiguous (no whitespace gap)
    with the rejected fragment, so it is folded into the diagnostic's
    raw_value/span; the leftover node itself is untouched (still a plain
    term in the tree, exactly as before this fix), only what the
    diagnostic reports changes.

    See test_whitespace_separated_value_is_rejected_as_a_whole below for
    the boundary this widening deliberately does NOT cross, and for the
    separate rule (DIVERGENCES.md entry 61) that claims the
    whitespace-separated shapes instead.
    """
    res = dparse(query, reg)
    assert res.diagnostics
    d = res.diagnostics[0]
    assert d.kind is DiagnosticKind.BAD_DATE
    expected = query[len("added:") :]
    assert d.raw_value == expected
    assert query[d.startchar : d.endchar] == expected
    # The resulting ErrorLeaf's own span (not just the Diagnostic's) widens
    # the same way: a host reading the tree instead of the diagnostic list
    # must see the same, consistent span.
    (leaf,) = [n for n in _nodes(res.ast) if isinstance(n, ast.ErrorLeaf)]
    assert (leaf.startchar, leaf.endchar) == (d.startchar, d.endchar)
    # No AST semantic change: the leftover text this widening folds into
    # the diagnostic still stays exactly where it already was in the tree,
    # an ordinary default-field Term, unaffected by what the diagnostic
    # reports.
    (term,) = [n for n in _nodes(res.ast) if isinstance(n, ast.Term)]
    assert isinstance(term.text, str)
    assert query.endswith(term.text)


def test_bad_date_raw_value_widens_via_a_bare_wordnode_leftover(reg: FieldRegistry) -> None:
    """The other of the two shapes _leftover_fragment_text recognizes (see
    DIVERGENCES.md entry 58): a bare, still-unfielded WordNode, which
    whoosh_compat.parse() never produces (it always builds a
    MultifieldParser, so even a single default field goes through
    MultifieldPlugin's rewrite -- see the test above, which exercises that
    OrGroup shape instead). A plain QueryParser with NO default fieldname
    (so an unfielded leftover is never itself attempted as a date, and
    never reaches MultifieldPlugin, which is MultifieldParser-only) is the
    only way to reach this branch.
    """
    parser = QueryParser(None, reg)
    parser.add_plugin(DateParserPlugin(BASE, BERLIN))
    node = ast.normalize(parser.parse("added:2005-01-01T00:00:00Z"))
    assert isinstance(node, ast.And)
    (leaf,) = [n for n in node.children if isinstance(n, ast.ErrorLeaf)]
    assert leaf.diagnostic.raw_value == "2005-01-01T00:00:00Z"
    (term,) = [n for n in node.children if isinstance(n, ast.Term)]
    assert term.field is None
    assert term.text == "01T00:00:00Z"


def test_bad_date_raw_value_does_not_widen_past_a_wildcard_leftover(
    reg: FieldRegistry,
) -> None:
    """Documented scope limit (DIVERGENCES.md entry 58): a leftover that
    WildcardPlugin has already turned into a WildcardNode/PrefixNode
    (priority 50, before do_dates) is not plain leftover text, so
    _leftover_fragment_text does not recognize it and raw_value stays
    exactly the tokenizer's original fragment, same as before entry 58.
    """
    res = dparse("added:2005-01-01T00:00:00Z*", reg)
    assert res.diagnostics
    assert res.diagnostics[0].raw_value == "2005-01-"


def test_unfielded_value_still_resolves_as_a_date_with_a_date_default_field(
    reg: FieldRegistry,
) -> None:
    """Guards the priority ordering _widen_bad_date_error's sibling lookup
    relies on (DIVERGENCES.md entry 58): do_dates and MultifieldPlugin's
    do_multifield share a filter priority, with do_multifield always
    winning the tie (registered first in MultifieldParser.__init__), so an
    unfielded value on a DATE default field is ALREADY multifield-expanded
    into a per-field OrGroup by the time do_dates runs -- do_dates must
    still recognize and date-parse its "added" copy, not silently skip an
    unfielded node and let a literal Term reach a DATETIME field instead
    (which whoosh-compat's own emitter refuses to emit at all).

    Reordering do_dates to run BEFORE multifield expansion (to simplify
    the sibling lookup above) breaks this case silently: do_dates only
    ever looks at a node's OWN fieldname, and an unfielded node has none
    until multifield assigns one.
    """
    res = wc.parse(
        "yesterday", registry=reg, default_fields=["content", "added"], tz=BERLIN, basedate=BASE
    )
    assert not res.diagnostics
    assert isinstance(res.ast, ast.Or)
    kinds = {type(c) for c in res.ast.children}
    assert kinds == {ast.Term, ast.DateRange}


def test_bad_date_raw_value_only_widens_the_explicitly_fielded_spelling(
    reg: FieldRegistry,
) -> None:
    """CHARACTERIZATION, not asserting this is the ideal outcome (see
    DIVERGENCES.md entry 58's third scope-limit paragraph): with a
    DATE/DATETIME field in default_fields, the explicitly-fielded and
    unfielded spellings of the same colon-fragmented value widen
    differently. do_dates recurses into the OrGroup MultifieldPlugin
    already built for the unfielded spelling; the sibling lookup inside
    that recursion never sees anything outside the OrGroup, so the
    unfielded spelling keeps reporting only the tokenizer's original
    fragment while the explicitly-fielded one reports the full value.

    The explicitly-fielded spelling also reports a SECOND diagnostic on
    "added": the leftover sibling is itself attempted as a date (it is
    also a copy inside an OrGroup, since it is unfielded), producing its
    own narrower diagnostic whose span nests entirely inside the widened
    one's -- the overlap this same paragraph documents.
    """
    added_ref = FieldRef("added")
    fielded = wc.parse(
        "added:2005-01-01T00:00:00Z",
        registry=reg,
        default_fields=["content", "added"],
        tz=BERLIN,
        basedate=BASE,
    )
    unfielded = wc.parse(
        "2005-01-01T00:00:00Z",
        registry=reg,
        default_fields=["content", "added"],
        tz=BERLIN,
        basedate=BASE,
    )
    fielded_values = {d.raw_value for d in fielded.diagnostics if d.field == added_ref}
    unfielded_values = {d.raw_value for d in unfielded.diagnostics if d.field == added_ref}
    assert fielded_values == {"2005-01-01T00:00:00Z", "01T00:00:00Z"}
    assert "2005-01-01T00:00:00Z" not in unfielded_values


def test_whitespace_separated_value_is_rejected_as_a_whole(
    reg: FieldRegistry,
) -> None:
    """The boundary entry 58 declined to cross, and entry 61's rule for
    crossing it: raw_value covers the whole run when the grammar consumes a
    joined candidate in full, and stops exactly there. "added:-1 week
    invoice" answers entry 58's own question, since "-1 week invoice" does
    not parse while "-1 week" does, so "invoice" stays an ordinary term.
    """
    res = dparse("added:-1 week invoice", reg)
    assert res.diagnostics
    d = res.diagnostics[0]
    assert d.raw_value == "-1 week"
    assert d.startchar == len("added:")
    assert d.endchar == len("added:-1 week")
    assert any(isinstance(n, ast.Term) and n.text == "invoice" for n in _nodes(res.ast))


@pytest.mark.parametrize(
    ("query", "fires", "residual", "note"),
    [
        pytest.param(
            "added:december 2019",
            True,
            None,
            "DATETIME fires like DATE date_only",
            id="datetime-field",
        ),
        pytest.param(
            "title:december 2019",
            False,
            None,
            "non-date field is never a candidate",
            id="non-date-field",
        ),
        pytest.param(
            "december 2019",
            False,
            None,
            "no explicit field, deliberately excluded",
            id="default-field-multifield",
        ),
        pytest.param(
            "created:2020,2021",
            False,
            None,
            "single token, no run to join",
            id="comma-no-space",
        ),
        pytest.param(
            "created:2020, 2021",
            False,
            None,
            "candidate with a comma does not parse",
            id="comma-with-space",
        ),
        pytest.param(
            "created:december title:2019",
            False,
            None,
            "next sibling carries its own field",
            id="next-sibling-fielded",
        ),
        pytest.param(
            "created:december AND 2019",
            False,
            None,
            "an operator ends the run",
            id="operator-between",
        ),
        pytest.param(
            "created:december 20*",
            False,
            None,
            "a prefix node is not a plain word",
            id="wildcard-next",
        ),
        pytest.param(
            "(created:december 2019)",
            True,
            None,
            "recursion reaches nested groups",
            id="inside-group",
        ),
        pytest.param(
            "created:december 2019^2",
            True,
            None,
            "boost ends the run after its word",
            id="boosted",
        ),
        pytest.param(
            "added:2026-08-04T10:30:00",
            False,
            "2026-08-04T10:30:00",
            "colon-split fragments abut, so the run is truncated and the rule declines,"
            " leaving the value to the entries 54/58 rejection it already had",
            id="abutting-colon-split",
        ),
        pytest.param(
            "added:previous month to now",
            True,
            None,
            "a joined keyword phrase is a HEAD like any other word, so words"
            " following it are still a run this rule can claim",
            id="joined-keyword-phrase-then-more-words",
        ),
        pytest.param(
            "added:2026-08-04 T10:30:00",
            True,
            None,
            "whitespace before the T fragment, so the two nodes do not abut and"
            " the run survives _whitespace_separated",
            id="spaced-t-fragment",
        ),
        pytest.param(
            "created:december 2019 10:30",
            True,
            None,
            "a clock time trailing a month-and-year run; its colons live inside a"
            " single node, so they never break the run",
            id="date-then-clock-time",
        ),
        pytest.param(
            "added:10:30 december 2019",
            True,
            None,
            "the same run with the clock time as the HEAD instead of the tail",
            id="clock-time-then-date",
        ),
    ],
)
def test_unquoted_date_rejection_cell_matrix(
    reg: FieldRegistry, query: str, fires: bool, residual: str | None, note: str
) -> None:
    """Every (node type, field kind, value spelling) cell the rule can reach,
    each ending in exactly one outcome. Extend this, never carve exceptions
    out of it: a rule scoped by node type or field kind that lands in one
    cell and misses its siblings is this codebase's dominant defect class.

    ``fires`` is read through a proxy (a BAD_DATE whose ``raw_value`` carries
    a space), which is sound only because this rule is the only producer of a
    spaced ``raw_value``. ``residual`` is the other half of the outcome: where
    a row declines *and* another rule is meant to reject the same value, it
    names that value, so "did not fire" cannot quietly become "nothing
    diagnosed it at all".
    """
    res = dparse(query, reg)
    unquoted = [
        d
        for d in res.diagnostics
        if d.kind is DiagnosticKind.BAD_DATE and " " in (d.raw_value or "")
    ]
    assert bool(unquoted) is fires, note
    if residual is not None:
        surviving = [
            d
            for d in res.diagnostics
            if d.kind is DiagnosticKind.BAD_DATE and d.raw_value == residual
        ]
        assert surviving, note
        assert surviving[0].message == f"{residual!r} is not a recognizable date"


def _parse_with_date_default_field(query: str, reg: FieldRegistry) -> wc.ParseResult:
    """A plain QueryParser (not a MultifieldParser) whose default field is
    itself a DATE/DATETIME field, mirroring
    test_bare_date_keyword_under_query_parser_default_date_field's
    construction rather than wc.parse's MultifieldParser one.
    """
    parser = QueryParser("added", reg)
    parser.add_plugin(DateParserPlugin(BASE, BERLIN))
    node = ast.normalize(parser.parse(query))
    return wc.ParseResult(ast=node, diagnostics=tuple(parser.diagnostics))


def test_date_default_field_is_deliberately_excluded(reg: FieldRegistry) -> None:
    """A plain parser whose default field is itself a DATE field keeps the
    truncating behavior: do_dates resolves an unfielded node through
    parser.fieldname, but do_unquoted_date_values deliberately does not,
    for the reason do_date_phrases gives for the same restriction. With a
    date default field every adjacent word pair in the query would become a
    join candidate, so the rule would reject far more than it repairs.

    A scope exclusion with a named cost, pinned so it cannot drift: the
    silent truncation this rule closes elsewhere still happens here.
    """
    res = _parse_with_date_default_field("december 2019", reg)
    assert not [
        d
        for d in res.diagnostics
        if d.kind is DiagnosticKind.BAD_DATE and " " in (d.raw_value or "")
    ]


@pytest.mark.parametrize(
    ("query", "text"),
    [
        pytest.param("added:2005-01-01 invoice", "invoice", id="day-precision-then-a-word"),
        pytest.param("created:2020 invoice", "invoice", id="year-precision-then-a-word"),
    ],
)
def test_a_whitespace_separated_term_after_a_date_is_still_a_term(
    reg: FieldRegistry, query: str, text: str
) -> None:
    # The boundary the rule above must not cross: here the remainder is a
    # separate token, not the tail of a cut-in-half value, and the date
    # itself consumed its own text exactly. This has always meant "documents
    # from that day that also mention invoice" and must keep meaning it.
    res = dparse(query, reg)
    assert not res.diagnostics
    r = res.ast
    assert isinstance(r, ast.And)
    assert isinstance(r.children[0], ast.DateRange)
    assert isinstance(r.children[1], ast.Term)
    assert r.children[1].text == text


def test_datetime_boost_preserved(reg: FieldRegistry) -> None:
    r = dparse("added:2020^2.0", reg).ast
    assert isinstance(r, ast.Boosted)
    assert r.boost == 2.0


# --- Daynames grammar (parser/dateparse.py:625-637) ------------------------


@pytest.mark.parametrize(
    ("query", "expected_date"),
    [
        # BASE is 2026-08-04, a Tuesday.
        pytest.param("added:'next monday'", datetime(2026, 8, 10), id="next-monday"),
        pytest.param("added:'last friday'", datetime(2026, 7, 31), id="last-friday"),
        pytest.param(
            "added:'next tuesday'", datetime(2026, 8, 11), id="next-same-weekday-wraps-a-week"
        ),
    ],
)
def test_dayname_keywords(reg: FieldRegistry, query: str, expected_date: datetime) -> None:
    # A weekday keyword names a whole day, so both bounds are pinned: the
    # lower bound alone is equally consistent with the instant that day
    # starts at.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(
        expected_date.year, expected_date.month, expected_date.day, tzinfo=BERLIN
    ).astimezone(UTC)
    assert r.hi == (
        datetime(expected_date.year, expected_date.month, expected_date.day, tzinfo=BERLIN)
        + timedelta(days=1)
    ).astimezone(UTC)
    assert not r.incl_hi


# --- Time12 grammar (parser/dateparse.py:647-657) ---------------------------


@pytest.mark.parametrize(
    ("query", "lo_local", "hi_local"),
    [
        pytest.param(
            "added:'3pm'",
            datetime(2026, 8, 4, 15, 0, 0),
            datetime(2026, 8, 4, 16, 0, 0),
            id="3pm-bare-hour",
        ),
        pytest.param(
            "added:'12am'",
            datetime(2026, 8, 4, 0, 0, 0),
            datetime(2026, 8, 4, 1, 0, 0),
            id="12am-is-midnight-hour",
        ),
        pytest.param(
            "added:'12pm'",
            datetime(2026, 8, 4, 12, 0, 0),
            datetime(2026, 8, 4, 13, 0, 0),
            id="12pm-is-noon-hour",
        ),
        pytest.param(
            "added:'5:30am'",
            datetime(2026, 8, 4, 5, 30, 0),
            datetime(2026, 8, 4, 5, 31, 0),
            id="5-30am-with-minutes",
        ),
        pytest.param(
            "added:'5:30:15pm'",
            datetime(2026, 8, 4, 17, 30, 15),
            datetime(2026, 8, 4, 17, 30, 16),
            id="5-30-15pm-with-seconds",
        ),
    ],
)
def test_time12_keywords(
    reg: FieldRegistry, query: str, lo_local: datetime, hi_local: datetime
) -> None:
    # A time of day is a window as wide as the precision typed, not an
    # instant: a bare hour spans the hour, "5:30am" the minute it names and
    # "5:30:15pm" the second. The three widths are why each param carries its
    # own upper bound instead of the test deriving one -- and why asserting
    # only the lower bound could not tell any of them from an instant. The
    # date comes from the basedate (2026-08-04), which is what "a time with
    # no date" resolves against.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == lo_local.replace(tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == hi_local.replace(tzinfo=BERLIN).astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


# --- Other relative-calendar keywords ---------------------------------------


def test_previous_year(reg: FieldRegistry) -> None:
    r = dparse("added:'previous year'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2025, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_week(reg: FieldRegistry) -> None:
    # BASE 2026-08-04 is a Tuesday; this week's Monday is 2026-08-03, so
    # "previous week" is 2026-07-27 through (excl) 2026-08-03.
    r = dparse("added:'previous week'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 27, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 3, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_previous_quarter(reg: FieldRegistry) -> None:
    # BASE month is August (Q3), so the previous quarter is Q2: Apr-Jun.
    r = dparse("added:'previous quarter'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 4, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_this_year(reg: FieldRegistry) -> None:
    r = dparse("added:'this year'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2027, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_this_month(reg: FieldRegistry) -> None:
    r = dparse("added:'this month'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 9, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_today(reg: FieldRegistry) -> None:
    r = dparse("added:today", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 5, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_now_keyword(reg: FieldRegistry) -> None:
    r = dparse("added:now", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == BASE.astimezone(UTC)
    assert r.incl_lo
    assert r.incl_hi


def test_now_followed_by_unquoted_offset_words_reads_as_now_plus_free_text(
    reg: FieldRegistry,
) -> None:
    # A trap the quoting rules above exist to avoid: unlike a quoted or
    # bracketed relative offset, "now - 3 days" (unquoted, space-separated)
    # never reaches the offset grammar at all. Whitespace ends the value at
    # "now" (a zero-width instant, see test_now_keyword), and "-", "3",
    # "days" become three ordinary free-text terms ANDed alongside it, not
    # part of the date. No diagnostic is raised anywhere in this.
    r = dparse("added:now - 3 days", reg)
    assert not r.diagnostics
    node = r.ast
    assert isinstance(node, ast.And)
    assert len(node.children) == 4
    date_children = [c for c in node.children if isinstance(c, ast.DateRange)]
    assert len(date_children) == 1
    assert date_children[0].lo == date_children[0].hi == BASE.astimezone(UTC)
    term_texts = {c.text for c in node.children if isinstance(c, ast.Term)}
    assert term_texts == {"-", "3", "days"}


@pytest.mark.parametrize(
    ("query", "hour", "minute", "second"),
    [
        pytest.param("added:midnight", 0, 0, 0, id="midnight"),
        pytest.param("added:noon", 12, 0, 0, id="noon"),
    ],
)
def test_midnight_noon(reg: FieldRegistry, query: str, hour: int, minute: int, second: int) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.hour, lo_local.minute, lo_local.second) == (hour, minute, second)
    # midnight/noon disambiguate to a zero-width (start == end) timespan, an
    # exact instant, not an ambiguous period: both bounds inclusive, not a
    # half-open range one microsecond wide (see cb3a4b1).
    assert r.lo == r.hi
    assert r.incl_lo
    assert r.incl_hi


def test_tomorrow(reg: FieldRegistry) -> None:
    r = dparse("added:tomorrow", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 5, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 6, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


# --- dmy/mdy/ymd/ydm and month-name sequences (English.setup's self.dmy) ---


@pytest.mark.parametrize(
    ("query", "lo", "hi"),
    [
        pytest.param(
            "created:'4 august 2020'",
            datetime(2020, 8, 4),
            datetime(2020, 8, 5),
            id="day-month-year",
        ),
        pytest.param(
            "created:'august 4 2020'",
            datetime(2020, 8, 4),
            datetime(2020, 8, 5),
            id="month-day-year",
        ),
        pytest.param(
            "created:'2020 august 4'",
            datetime(2020, 8, 4),
            datetime(2020, 8, 5),
            id="year-month-day",
        ),
        pytest.param(
            "created:'2020 4 august'",
            datetime(2020, 8, 4),
            datetime(2020, 8, 5),
            id="year-day-month",
        ),
        pytest.param(
            "created:'4 august'",
            datetime(2026, 8, 4),
            datetime(2026, 8, 5),
            id="day-month-no-year-uses-basedate",
        ),
        pytest.param(
            "created:'august 4'",
            datetime(2026, 8, 4),
            datetime(2026, 8, 5),
            id="month-day-no-year-uses-basedate",
        ),
        pytest.param(
            "created:'august 2020'",
            datetime(2020, 8, 1),
            datetime(2020, 9, 1),
            id="month-year",
        ),
    ],
)
def test_named_month_sequences(reg: FieldRegistry, query: str, lo: datetime, hi: datetime) -> None:
    # Both bounds per param, because the widths differ and because the old
    # "lo is not None" shape passed for any parse that returned a range at
    # all, including one that read the components in the wrong order. The
    # four full-date spellings name the same day whatever the component
    # order; the two without a year take the basedate's year (2026); the
    # month+year spelling is a whole month. "created" is date_only, so its
    # bounds are plain UTC midnights with no local-tz conversion (the same
    # convention as test_month_alone below).
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == lo.replace(tzinfo=UTC)
    assert r.hi == hi.replace(tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_month_alone(reg: FieldRegistry) -> None:
    # "august" alone with no day/year -> the whole month, in the basedate's
    # year (2026).
    r = dparse("created:august", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 1, tzinfo=UTC)
    assert r.hi == datetime(2026, 9, 1, tzinfo=UTC)
    assert not r.incl_hi


# --- Compact numeric "simple" sequence (DateParser.__init__'s self.simple) -


def test_compact_numeric_datetime(reg: FieldRegistry) -> None:
    # 8 digits is the whole calendar day, not the instant at its start: both
    # bounds are pinned so a narrowing of this form to a single instant fails
    # here rather than only shortening the upper bound silently. Contrast the
    # 14-digit spelling below, which really is one instant.
    r = dparse("added:'20200304'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 4, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2020, 3, 5, tzinfo=BERLIN).astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_compact_numeric_datetime_progressive_partial(reg: FieldRegistry) -> None:
    # progressive=True: a prefix of the simple sequence (year+month only)
    # still matches, and spans the whole month it names. Both bounds are
    # pinned for the same reason as the 8-digit day above: a lower bound
    # alone cannot tell a month-wide window from the instant it starts at.
    r = dparse("added:'202003'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2020, 4, 1, tzinfo=BERLIN).astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_compact_numeric_datetime_full_width_is_a_single_second_instant(
    reg: FieldRegistry,
) -> None:
    # The full-width (14-digit) spelling of the same "simple" sequence
    # (year+month+day+hour+minute+second, no separators) is a single
    # second-precision instant, not the whole day the 8-digit prefix above
    # resolves to.
    r = dparse("added:'20050304153000'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2005, 3, 4, 15, 30, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == r.lo + timedelta(seconds=1)
    assert r.incl_lo
    assert not r.incl_hi


# --- plusdate / nowcompact (Combo/PlusMinus grammar) -----------------------


def test_plusdate_years_months_weeks_combo(reg: FieldRegistry) -> None:
    r = dparse("added:'-1y2mo3w'", reg).ast
    assert isinstance(r, ast.DateRange)


# --- Dashed/space/dot/slash-separated ISO dates (English.__init__'s "simple"
# sequence; see module docstring / DIVERGENCES.md for the bug this covers:
# the "bundle" Choice used to try the "datetime" Bag before "simple", which
# partial-matched a bare year and starved "simple" of the input, so any
# separated single-value date failed to parse at all) -----------------------


@pytest.mark.parametrize(
    ("query", "lo", "hi"),
    [
        pytest.param(
            "created:2020-01-01",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="bare-dashed-day",
        ),
        pytest.param(
            "created:'2020-01-01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="quoted-dashed-day",
        ),
        pytest.param(
            "created:2020-01",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 2, 1, tzinfo=UTC),
            id="bare-dashed-month",
        ),
        pytest.param(
            "created:'2020-01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 2, 1, tzinfo=UTC),
            id="quoted-dashed-month",
        ),
        pytest.param(
            "created:'2020 01 01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="space-separated-day",
        ),
        pytest.param(
            "created:'2020.01.01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="dot-separated-day",
        ),
        pytest.param(
            "created:'2020/01/01'",
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 2, tzinfo=UTC),
            id="slash-separated-day",
        ),
    ],
)
def test_separated_iso_date_precision(
    reg: FieldRegistry, query: str, lo: datetime, hi: datetime
) -> None:
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange), r
    assert r.lo == lo
    assert r.hi == hi
    assert not r.incl_hi


# --- RFC3339 "T"/"Z" datetime separator (paperless-ngx PR #13010 back-compat;
# DateParser.__init__'s "simple" sequence now also accepts "T", and
# DateParserPlugin._split_rfc3339_utc strips a trailing "Z") ---------------


def test_rfc3339_t_separator_without_z_still_uses_local_tz(reg: FieldRegistry) -> None:
    # No "Z": "T" is just an accepted separator (like space/dash/dot/slash
    # already were), the value is still local wall-clock time in BERLIN.
    r = dparse("added:'2026-08-04T10:30:00'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN).astimezone(UTC)
    assert r.incl_lo


def test_rfc3339_t_z_is_an_absolute_utc_instant_not_local(reg: FieldRegistry) -> None:
    # With "Z": the value names an absolute UTC instant and must NOT be
    # reinterpreted as BERLIN wall-clock time and shifted again -- this is
    # the exact bug _split_rfc3339_utc's "tz" override on _to_utc exists to
    # avoid. BASE/BERLIN is UTC+2 in August, so a naive local-tz
    # misinterpretation of the same digits would be 2 hours off.
    r = dparse("added:'2026-08-04T10:30:00Z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.incl_lo


def test_rfc3339_lowercase_t_and_z(reg: FieldRegistry) -> None:
    # Sequence's separator regex is compiled with re.IGNORECASE; the "Z"
    # designator check is explicitly case-insensitive too.
    r = dparse("added:'2026-08-04t10:30:00z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("added:2026-08-04T10:30:00", id="full-date"),
        pytest.param("added:2026-08T10:30", id="no-day"),
    ],
)
def test_bare_unquoted_t_value_is_rejected_not_truncated(
    reg: FieldRegistry,
    query: str,
) -> None:
    # DIVERGENCES.md entry 54, superseding entry 49. Without quotes the
    # tokenizer splits the value at its colons before the date grammar
    # runs, leaving the date field a fragment cut off after a separator,
    # mid-token. Real whoosh swallows that dangling separator and reads the
    # fragment as a whole, shorter date (the August-2026 month window, or
    # the 2026 year window), then ANDs the rest of the timestamp on as free
    # text; entry 49 used to keep that truncation for parity. It is a
    # silently wrong query with no diagnostic, so the fragment is now a bad
    # date instead. The quoted spelling (entry 48,
    # test_rfc3339_t_z_is_an_absolute_utc_instant_not_local above) and the
    # bracketed-range spelling are the ones that honor the full value.
    #
    # raw_value itself is the FULL value the user typed here, not the
    # tokenizer's cut-off fragment: DIVERGENCES.md entry 58 widens the
    # diagnostic to cover the immediately-adjacent leftover text once the
    # tokenizer's own fragment fails to parse (see
    # test_bad_date_raw_value_widens_to_cover_a_contiguous_leftover_fragment
    # above for that mechanism in isolation).
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    assert res.diagnostics[0].raw_value == query[len("added:") :]
    assert not any(isinstance(n, ast.DateRange) for n in _nodes(res.ast))


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        pytest.param(
            "added:2026T10",
            datetime(2026, 10, 1, tzinfo=BERLIN),
            datetime(2026, 11, 1, tzinfo=BERLIN),
            id="bare-year-t-month",
        ),
        pytest.param(
            "added:'2026T10'",
            datetime(2026, 10, 1, tzinfo=BERLIN),
            datetime(2026, 11, 1, tzinfo=BERLIN),
            id="quoted-year-t-month",
        ),
        pytest.param(
            "added:2026T10:30",
            datetime(2026, 10, 30, tzinfo=BERLIN),
            datetime(2026, 10, 31, tzinfo=BERLIN),
            id="colon-split-day-joins-year-t-month",
        ),
    ],
)
def test_no_separator_t_value_parses_as_year_t_month(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # DIVERGENCES.md entry 50: with T in the separator class, a dash-less
    # "2026T10" reads as year-T-month; a colon-split trailing token
    # ("2026T10:30") is joined by the date parser into a day-precision
    # reading. Real whoosh cannot read these at all (_NullQuery, matches
    # nothing), so whoosh-compat's reading is the compat-favorable side
    # of a documented divergence, not parity.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.astimezone(UTC)
    assert r.hi == expected_hi.astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_rfc3339_range_bounds_with_z(reg: FieldRegistry) -> None:
    # The motivating case (paperless-ngx issue/PR #13010): a bracketed range
    # of RFC3339 "Z" datetimes, unquoted -- no colon-tokenizing ambiguity
    # inside range bounds, unlike a bare unquoted single value.
    r = dparse("created:[2026-01-01T00:00:00Z TO 2026-06-01T00:00:00Z]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 1, 1, tzinfo=UTC)
    # "created" is date_only=True. Both bounds carry a microsecond field
    # left unspecified by the grammar (there is no ".NNNNNN" in the query
    # text), which is itself ambiguous (see is_ambiguous/adatetime), so
    # both bounds go through the joint-disambiguation "period" path rather
    # than the exact-instant path; the hi side's exclusive ceiling then
    # rounds its ambiguous microsecond up, landing one calendar day past
    # its own date once date_only-collapsed (pre-existing _to_utc ceiling
    # behavior, not new: see test_date_only_whole_day_value_still_ceils_exactly_once).
    assert r.hi == datetime(2026, 6, 2, tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        pytest.param(
            "added:[2021-06-01T00:00:00Z TO 2020]",
            datetime(2020, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2021, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="backwards-z-start-swapped-keeps-utc",
        ),
        pytest.param(
            "added:[2021 TO 2020-06-01T00:00:00Z]",
            datetime(2020, 6, 1, tzinfo=UTC),
            datetime(2022, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            id="backwards-z-end-swapped-keeps-utc",
        ),
        # The next three ranges mix a "Z" bound with a year-AMBIGUOUS local
        # bound. The joint step then mutates the ambiguous bound's year
        # (borrowing it from the other side) WITHOUT swapping (times.py's
        # backwards-swap line is unreachable unless both bounds carry
        # explicit years), so bounds_swapped must stay False and each tz
        # must stay with its positional bound; mistaking the borrow's
        # value change for a swap shifts the Z bound onto local time.
        pytest.param(
            "added:[feb TO 2027-06-01T00:00:00Z]",
            datetime(2027, 2, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="ambiguous-local-start-borrows-year-no-false-swap",
        ),
        pytest.param(
            "added:[dec TO 2026-08-04T10:30:00Z]",
            datetime(2025, 12, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC),
            id="ambiguous-local-start-borrows-prior-year-no-false-swap",
        ),
        pytest.param(
            "added:[2026-08-04T10:30:00Z TO feb]",
            datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
            datetime(2027, 3, 1, tzinfo=BERLIN).astimezone(UTC),
            id="ambiguous-local-end-borrows-next-year-no-false-swap",
        ),
        # A year-only local START with the SAME explicit year as the "Z"
        # bound: the equal-years month/day fill copies the end's date onto
        # the start, mutating its value with no swap possible (the filled
        # dates are equal, never out of order), so bounds_swapped must
        # stay False and positional tz must hold. The mirrored end-side
        # fill takes its date from the basedate instead and CAN go out of
        # order (both variants pinned below).
        pytest.param(
            "added:[2027 TO 2027-06-01T00:00:00Z]",
            datetime(2027, 6, 1, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 6, 1, 0, 0, 1, tzinfo=UTC),
            id="equal-year-start-filled-from-z-end-no-false-swap",
        ),
        pytest.param(
            "added:[2026 TO 2026-08-04T10:30:00Z]",
            datetime(2026, 8, 4, tzinfo=BERLIN).astimezone(UTC),
            datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC),
            id="equal-year-start-filled-from-z-end-basedate-year-no-false-swap",
        ),
        pytest.param(
            "added:[2027-06-01T00:00:00Z TO 2027]",
            datetime(2027, 6, 1, tzinfo=UTC),
            datetime(2027, 8, 5, tzinfo=BERLIN).astimezone(UTC),
            id="equal-year-end-filled-from-basedate-in-order",
        ),
        pytest.param(
            "added:[2027-09-15T00:00:00Z TO 2027]",
            datetime(2027, 8, 4, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="equal-year-end-filled-from-basedate-genuine-swap",
        ),
        # A year-only start with a DIFFERENT explicit year: no fill
        # mutates it (the equal-years precondition fails), the backwards
        # orientation is a genuine swap (bounds_swapped True), and the
        # tzs must follow the swapped values.
        pytest.param(
            "added:[2027 TO 2026-06-01T00:00:00Z]",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2028, 1, 1, tzinfo=BERLIN).astimezone(UTC),
            id="year-only-start-different-year-genuine-swap",
        ),
        # A year+time local start ("2027 10pm": explicit year, NO month or
        # day, explicit hour) is the one date-missing shape whose
        # equal-years fill takes the basedate branch (its floor time
        # exceeds the Z end's ceil time), which can land the filled date
        # after the Z bound and trigger a genuine swap. The tzs must
        # follow that swap even though the start bound was mutated first.
        pytest.param(
            "added:[2027 10pm TO 2027-06-01T00:00:00Z]",
            datetime(2027, 6, 1, tzinfo=UTC),
            datetime(2027, 8, 4, 23, 0, tzinfo=BERLIN).astimezone(UTC),
            id="year-time-start-basedate-filled-genuine-swap",
        ),
        pytest.param(
            "added:[2027 10pm TO 2027-09-15T00:00:00Z]",
            datetime(2027, 8, 4, 22, 0, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="year-time-start-basedate-filled-in-order",
        ),
        pytest.param(
            "added:[2027-09-15T00:00:00Z TO 2027 10pm]",
            datetime(2027, 8, 4, 22, 0, tzinfo=BERLIN).astimezone(UTC),
            datetime(2027, 9, 15, 0, 0, 1, tzinfo=UTC),
            id="year-time-end-basedate-filled-genuine-swap",
        ),
    ],
)
def test_rfc3339_mixed_tz_bounds_survive_a_backwards_swap(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # A backwards range with two explicit years swaps its bounds in the
    # joint-disambiguation step; each bound's timezone (UTC for a
    # "Z"-suffixed RFC3339 bound, the query's local tz otherwise) must
    # follow its VALUE through the swap, or the Z bound gets reinterpreted
    # as local wall-clock time and silently shifted, the exact bug the
    # designator handling exists to prevent.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo
    assert r.hi == expected_hi


def test_backwards_swap_carries_exactness_with_the_value(reg: FieldRegistry) -> None:
    # Mirrors test_rfc3339_mixed_tz_bounds_survive_a_backwards_swap, but for
    # start_exact/end_exact rather than start_tz/end_tz: the joint
    # disambiguation step's genuine-swap branch (times.py's timespan
    # .disambiguated()) swaps the resolved VALUES, so after a swap
    # hi_naive is really the originally-typed start bound's value and vice
    # versa. start_tz/end_tz already follow that swap; start_exact/
    # end_exact did not, so whichever side's exactness decides the
    # exclusive-ceiling adjustment (hi_naive + 1 microsecond, "not
    # end_exact") was answering with the WRONG bound's exactness after a
    # swap.
    #
    # "20200615120000123456" is a fully-specified (exact) compact
    # datetime; "2019" is a year-only (ambiguous, not exact) bound. The
    # forward spelling never swaps (2019 already sorts before 2020), so it
    # is the ground truth: the exact end bound keeps the bracket's typed
    # inclusivity (no "}" -> incl_hi=True) and no ceiling adjustment
    # applies. The reversed spelling swaps (2020 sorts after 2019, a
    # genuinely backwards range), landing the exact value at hi_naive; it
    # must produce the exact same incl_hi/hi, not silently apply the
    # ceiling adjustment meant for an ambiguous bound.
    # NOTE: with "[...]" brackets on both sides, incl_lo is a
    # non-discriminating assertion here: the ambiguous bound ("2019") is
    # always the one that lands at lo (2019 sorts below 2020 either way,
    # swap or no swap), and an ambiguous lo is forced incl_lo=True
    # unconditionally (dateparse.py: "if lo_naive is not None and not
    # start_exact: incl_lo = True"), so this assertion would pass even
    # without the fix. Only end_exact/incl_hi is genuinely pinned by this
    # test; see test_backwards_swap_carries_start_exactness_with_the_value
    # below for the start_exact/incl_lo half, which needs an exclusive
    # "{" bracket and the exact value on alternating sides to discriminate
    # at all.
    forward = dparse("created:[2019 TO 20200615120000123456]", reg).ast
    reversed_ = dparse("created:[20200615120000123456 TO 2019]", reg).ast
    assert isinstance(forward, ast.DateRange)
    assert isinstance(reversed_, ast.DateRange)
    assert forward.incl_hi is True
    assert forward.hi == datetime(2020, 6, 15, tzinfo=UTC)
    assert reversed_.incl_hi == forward.incl_hi
    assert reversed_.hi == forward.hi
    assert reversed_.lo == forward.lo
    assert reversed_.incl_lo == forward.incl_lo


def test_backwards_swap_carries_start_exactness_with_the_value(reg: FieldRegistry) -> None:
    # The start_exact/incl_lo half of
    # test_backwards_swap_carries_exactness_with_the_value, which only
    # pins end_exact/incl_hi (see its NOTE): incl_lo only reveals a
    # start_exact bug for a bound the code doesn't force True outright,
    # which requires BOTH an exact lo bound (start_exact True skips the
    # "ambiguous lo is always inclusive" forcing) AND an exclusive "{"
    # bracket (an inclusive "[" produces incl_lo=True either way, since
    # True is also the un-forced default for an untyped exclusion flag).
    #
    # "20190615120000123456" is exact; "2020" is year-only (ambiguous).
    # The forward spelling (exact TO ambiguous) never swaps (2019 sorts
    # before 2020): the exact lo bound keeps the bracket's typed
    # exclusivity ("{" -> incl_lo=False). The reversed spelling
    # (ambiguous TO exact, with "{" on the same, now-ambiguous-typed
    # side) swaps (2020 sorts after 2019), landing the exact value back
    # at lo_naive; it must produce the same incl_lo=False, not the
    # "ambiguous lo forces incl_lo=True" answer that applied to the
    # ORIGINAL (pre-swap) ambiguous-typed start bound.
    forward = dparse("created:{20190615120000123456 TO 2020}", reg).ast
    reversed_ = dparse("created:{2020 TO 20190615120000123456}", reg).ast
    assert isinstance(forward, ast.DateRange)
    assert isinstance(reversed_, ast.DateRange)
    assert forward.incl_lo is False
    assert forward.lo == datetime(2019, 6, 15, tzinfo=UTC)
    assert reversed_.incl_lo == forward.incl_lo
    assert reversed_.lo == forward.lo
    assert reversed_.hi == forward.hi
    assert reversed_.incl_hi == forward.incl_hi


def test_rfc3339_range_open_lo_bound_with_z(reg: FieldRegistry) -> None:
    # A "Z" bound as the sole (lo) side of an open-ended range: no second
    # bound to combine with, so this exercises the individual (non-joint)
    # bound-resolution path with the tz override applied.
    r = dparse("added:[2026-08-04T10:30:00Z TO]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.hi is None


def test_rfc3339_range_open_hi_bound_with_z(reg: FieldRegistry) -> None:
    r = dparse("added:[TO 2026-08-04T10:30:00Z]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is None
    # Unlike the lo-side test above, an hi-only bound is ambiguous (no
    # ".NNNNNN" microsecond in the query text) rather than exact, so it
    # takes the exclusive-ceiling path: the ceiling rounds the unspecified
    # microsecond up to 999999 and then adds one, carrying into the next
    # whole second (pre-existing adatetime.ceil()/fill_in behavior, not new
    # -- see test_rfc3339_range_bounds_with_z's comment for the same
    # mechanism at date_only granularity).
    assert r.hi == datetime(2026, 8, 4, 10, 30, 1, tzinfo=UTC)


def test_rfc3339_range_mixes_z_and_non_z_bound(reg: FieldRegistry) -> None:
    # A "Z" bound and a plain local (day-precision, unambiguous-enough to
    # avoid the microsecond-ambiguity ceiling exercised above) bound in the
    # same range each keep their own tz treatment (start_tz/end_tz in
    # _range_to_node are per-bound, not shared).
    r = dparse("added:[2026-08-04T10:30:00Z TO 2026-08-10]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    assert r.hi == datetime(2026, 8, 11, tzinfo=BERLIN).astimezone(UTC)


def test_rfc3339_date_only_field_z_designator_is_a_no_op(reg: FieldRegistry) -> None:
    # "created" is date_only=True: _to_utc never applies any tz (local or
    # UTC) to a date_only field, so "Z" makes no observable difference
    # there, but it must still parse rather than error.
    r = dparse("created:'2026-08-04T00:00:00Z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 4, tzinfo=UTC)
    assert r.hi == datetime(2026, 8, 5, tzinfo=UTC)
    assert not r.incl_hi


def test_trailing_z_without_preceding_t_is_not_treated_as_utc_designator(
    reg: FieldRegistry,
) -> None:
    # The RFC3339 "Z" gate requires a "T" earlier in the string (mirrors
    # paperless's own ``_bound_datetimes``'s ``if "T" in token:`` gate): a
    # bare trailing "z"/"Z" with no "T" is not this designator at all, and
    # is simply an unrecognizable date (there is no separate "z" grammar
    # element either).
    res = dparse("added:'20200304Z'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


# --- Range queries combining two date expressions (range_to_node) ---------


def test_range_both_bounds_named_dates(reg: FieldRegistry) -> None:
    r = dparse("created:[2020-01-01 TO 2020-12-31]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 1, 1, tzinfo=UTC)
    assert r.hi == datetime(2021, 1, 1, tzinfo=UTC)


def test_range_bounds_do_not_collapse_to_year(reg: FieldRegistry) -> None:
    # Companion to test_range_both_bounds_named_dates: that test's answer
    # coincidentally matches what the pre-fix "collapse to bare year" bug
    # (DIVERGENCES.md, range_to_node not enforcing full-text consumption on
    # a bound) would also produce (both bounds fall in 2020, so the whole
    # year happens to contain them). This case doesn't coincide: the buggy
    # behavior would silently produce the whole of 2020, not June 15-20.
    r = dparse("created:[2020-06-15 TO 2020-06-20]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 6, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 6, 21, tzinfo=UTC)


# --- date_only exclusive-upper-bound ceiling ------------------------------


def test_date_only_time_bearing_single_value_ceils_hi_to_next_day(reg: FieldRegistry) -> None:
    # "created" is date_only=True. A time-bearing value with any
    # sub-day precision left ambiguous (minutes here, seconds unspecified)
    # produces a half-open period; date_only truncation must not let the
    # exclusive hi bound fall back to (or before) lo's own day.
    r = dparse("created:'2020-03-15 15:30'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_date_only_range_time_bearing_end_bound_includes_named_end_day(
    reg: FieldRegistry,
) -> None:
    r = dparse("created:[2020-03-01 TO '2020-03-15 12:00']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 1, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert not r.incl_hi


def test_date_only_range_time_bearing_start_bound_still_truncates_down(
    reg: FieldRegistry,
) -> None:
    # The lo bound already truncates down correctly; the ceiling fix must
    # not touch it.
    r = dparse("created:['2020-03-15 15:00' TO 2020-11-30]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 12, 1, tzinfo=UTC)


def test_date_only_same_day_range_times_on_both_ends_is_not_empty(reg: FieldRegistry) -> None:
    r = dparse("created:['2020-03-15 00:00' TO '2020-03-15 18:00']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)
    assert r.lo < r.hi


def test_date_only_whole_day_value_still_ceils_exactly_once(reg: FieldRegistry) -> None:
    # Regression guard: a value with no time-of-day component (already
    # day-aligned) must not get an extra day tacked on.
    r = dparse("created:2020-03-15", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 3, 15, tzinfo=UTC)
    assert r.hi == datetime(2020, 3, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:noon", id="degenerate-instant-noon"),
        pytest.param("created:'3pm'", id="hour-precision-period-3pm"),
    ],
)
def test_date_only_noon_and_3pm_consistently_cover_their_day(
    reg: FieldRegistry, query: str
) -> None:
    # Both a degenerate exact-instant value (noon) and an hour-precision
    # period value (3pm) must resolve to a range that covers the same
    # calendar day on a date_only field: before the fix, 3pm's half-open
    # ceiling collapsed to an empty [midnight, midnight) range while noon's
    # both-inclusive shape happened to still work.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    basedate_local = BASE.astimezone(BERLIN)
    expected_day = datetime(
        basedate_local.year, basedate_local.month, basedate_local.day, tzinfo=UTC
    )
    assert r.lo == expected_day
    assert r.hi is not None
    # The range must be non-empty (would-be-matching-document shape) under
    # the (lo, hi, incl_lo, incl_hi) semantics.
    assert r.lo < r.hi or (r.lo == r.hi and r.incl_lo and r.incl_hi)


def test_range_open_lower(reg: FieldRegistry) -> None:
    r = dparse("created:[TO 2020]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is None
    assert r.hi is not None


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        # BASE is 2026-08-04. Whoosh reads a bracketed range's two bounds
        # jointly (timespan.disambiguated's range heuristics), so a
        # backward-reading range spans the boundary instead of inverting.
        pytest.param(
            "added:[dec to feb]",
            datetime(2025, 12, 1, tzinfo=BERLIN),
            datetime(2026, 3, 1, tzinfo=BERLIN),
            id="backward-months-borrow-previous-year",
        ),
        pytest.param(
            "added:[dec 25 to jan 5]",
            datetime(2025, 12, 25, tzinfo=BERLIN),
            datetime(2026, 1, 6, tzinfo=BERLIN),
            id="year-boundary-crossing-days",
        ),
        pytest.param(
            "added:[noon to 3am]",
            datetime(2026, 8, 4, 12, 0, tzinfo=BERLIN),
            datetime(2026, 8, 5, 4, 0, tzinfo=BERLIN),
            id="overnight-time-range-pushes-end-to-next-day",
        ),
        pytest.param(
            "added:[3pm to 10am]",
            datetime(2026, 8, 4, 15, 0, tzinfo=BERLIN),
            datetime(2026, 8, 5, 11, 0, tzinfo=BERLIN),
            id="overnight-pm-to-am",
        ),
        pytest.param(
            "added:[feb to may]",
            datetime(2026, 2, 1, tzinfo=BERLIN),
            datetime(2026, 6, 1, tzinfo=BERLIN),
            id="forward-months-both-basedate-year",
        ),
        pytest.param(
            "added:[2021 to 2020]",
            datetime(2020, 1, 1, tzinfo=BERLIN),
            datetime(2022, 1, 1, tzinfo=BERLIN),
            id="backward-years-swap-to-cover-both",
        ),
    ],
)
def test_range_joint_disambiguation(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # Expected bounds measured directly against the pinned whoosh oracle
    # (whoosh's inclusive last-microsecond ceiling, expressed here in this
    # library's half-open exclusive-upper form).
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.astimezone(UTC)
    assert r.hi == expected_hi.astimezone(UTC)
    assert r.incl_lo
    assert not r.incl_hi


def test_range_joint_disambiguation_date_only_field(reg: FieldRegistry) -> None:
    # The date_only sibling cell: "created" collapses to UTC calendar days,
    # but the joint reading of the two bounds must happen first.
    r = dparse("created:[dec to feb]", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2025, 12, 1, tzinfo=UTC)
    assert r.hi == datetime(2026, 3, 1, tzinfo=UTC)


def test_range_both_sides_are_periods_cannot_combine(reg: FieldRegistry) -> None:
    # Both sides resolve to an already-disambiguated timespan ("previous
    # week"/"previous month"), which can't be nested inside another
    # timespan(): exercises range_to_node's non-combine branch for both
    # raw_start and raw_end.
    r = dparse("added:['previous month' TO 'this month']", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 7, 1, tzinfo=BERLIN).astimezone(UTC)
    # The upper side is the other half of the non-combine branch: it takes
    # "this month"'s end, not its start, so the range spans both months.
    assert r.hi == datetime(2026, 9, 1, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


@pytest.mark.parametrize(
    ("query", "expected_lo", "expected_hi"),
    [
        # A year plus a colon-separated time is ambiguous. The separated-date
        # grammar alternative accepts ":" as a separator and is tried first,
        # so this reads as a calendar day, not a time of day. See
        # DIVERGENCES.md entry 21.
        # Berlin is UTC+1 in December, so the day starts at 23:00 the day before.
        pytest.param(
            "added:'2020 12:30'",
            datetime(2020, 12, 29, 23, 0),
            datetime(2020, 12, 30, 23, 0),
            id="year-plus-time-is-a-date",
        ),
        # A time the separated-date alternative cannot match still reads as a
        # time on every day of the year, matching whoosh.
        pytest.param(
            "added:'2020 5pm'",
            datetime(2020, 1, 1, 16, 0),
            datetime(2020, 12, 31, 17, 0),
            id="year-plus-meridiem-is-a-time",
        ),
    ],
)
def test_year_followed_by_time(
    reg: FieldRegistry, query: str, expected_lo: datetime, expected_hi: datetime
) -> None:
    # The upper bound is what tells the two readings apart, so it is pinned
    # per param rather than left to the lower bound alone: the date reading
    # is one calendar day wide, while the time reading spans the whole year
    # with its edges on the 5pm hour (measured: 2020-01-01 16:00Z through
    # 2020-12-31 17:00Z, i.e. 5pm local on the year's first day to 6pm local
    # on its last). A lower bound alone is equally consistent with an
    # instant, which is neither reading.
    r = dparse(query, reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == expected_lo.replace(tzinfo=UTC)
    assert r.hi == expected_hi.replace(tzinfo=UTC)
    assert r.incl_lo
    assert not r.incl_hi


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:9999", id="max-year-single"),
        pytest.param("created:99991231", id="max-year-compact-day"),
        pytest.param("created:'dec 9999'", id="max-year-named-month"),
        pytest.param("created:[2020 TO 9999]", id="max-year-range-end"),
        pytest.param("created:0000", id="zero-year-single"),
        pytest.param("created:00000101", id="zero-year-compact-day"),
        pytest.param("created:[0000 TO 2020]", id="zero-year-range-start"),
    ],
)
def test_years_outside_the_representable_range_diagnose(reg: FieldRegistry, query: str) -> None:
    # Year 0 has no datetime representation, and year 9999's exclusive
    # ceiling would land past datetime.max. Both blow up in the arithmetic
    # rather than the grammar, so they need catching: parsing reports bad
    # input through diagnostics, it does not raise.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


@pytest.mark.parametrize(
    ("query", "bad_bound"),
    [
        pytest.param("created:[2020 TO 9999]", "9999", id="end-bound-overflow-named-correctly"),
        pytest.param("created:[0000 TO 2020]", "0000", id="start-bound-underflow-named-correctly"),
    ],
)
def test_range_out_of_range_diagnostic_names_the_failing_bound(
    reg: FieldRegistry, query: str, bad_bound: str
) -> None:
    # Regression: range_to_node's exception handler used to always report
    # `node.start or node.end`, so a range failing on its END bound (e.g.
    # year 9999's exclusive ceiling overflowing datetime.max) incorrectly
    # named the START bound in the diagnostic message.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert bad_bound in res.diagnostics[0].message
    assert res.diagnostics[0].field == FieldRef("created")
    assert res.diagnostics[0].raw_value == bad_bound


@pytest.mark.parametrize(
    ("query", "bad_bound"),
    [
        pytest.param("added:[qqq TO zzz]foo", "qqq", id="both-bounds-bad-trailing-text"),
        pytest.param("added:[2020 TO qqq]foo", "qqq", id="end-bound-bad-trailing-text"),
    ],
)
def test_range_error_raw_value_is_not_glued_to_a_trailing_leftover(
    reg: FieldRegistry, query: str, bad_bound: str
) -> None:
    """DIVERGENCES.md entry 58's widening only applies to a bare, unquoted
    WordNode value, not a range_to_node error: a range error's raw_value
    is a single BOUND string, not a slice of the source text at the error
    node's own span, so gluing a following sibling's text onto it (as the
    word-value widening does) would produce a string that appears nowhere
    in the query and does not match the error's own span.
    """
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].raw_value == bad_bound
    assert any(isinstance(n, ast.Term) and n.text == "foo" for n in _nodes(res.ast))


def test_quoted_value_error_raw_value_is_not_glued_to_a_trailing_leftover(
    reg: FieldRegistry,
) -> None:
    """The same shape as the range case above, for a double-quoted value:
    a PhraseNode's .text has its surrounding quotes stripped, but its span
    still includes them, so it is not a slice of the source text at its
    own span either. added:"qqq"foo must report raw_value='qqq' (matching
    its own 'qqq' span, quotes excluded), not 'qqqfoo' (which would match
    neither the query text at that span nor anything the user typed).
    """
    res = dparse('added:"qqq"foo', reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].raw_value == "qqq"
    assert any(isinstance(n, ast.Term) and n.text == "foo" for n in _nodes(res.ast))


@pytest.mark.parametrize(
    ("query", "bad_bound"),
    [
        pytest.param("added:[2020 to 0001]", "0001", id="failing-end-swapped-to-lo"),
        pytest.param("added:[9999 to 2020]", "9999", id="failing-start-swapped-to-hi"),
        pytest.param("added:[now to 0001]", "0001", id="exact-start-swapped-with-bad-end"),
        pytest.param(
            "added:['9999-12-31' to 9999]",
            "9999",
            id="innocent-max-ceiling-start-not-blamed",
            # No swap here: the failing value is genuinely the end bound's
            # exclusive ceiling. The start bound's own ceiling also sits at
            # datetime.max, so a probe applying hi-side arithmetic to both
            # bounds indiscriminately would blame the innocent start; the
            # side-aware rule keeps the positional attribution because the
            # positional bound fails its own side's arithmetic too.
        ),
        pytest.param(
            "added:[9999 to 0001]",
            "9999",
            # The first failing site is the hi-side exclusive ceiling, and
            # its overflowing value genuinely came from the start bound's
            # text (the swap put 9999 at hi): the re-attribution branch
            # fires because 9999 fails hi-side arithmetic on its own while
            # 0001 (which only fails as a lo) survives it.
            id="both-bad-names-first-failing-value",
        ),
        pytest.param(
            "added:[0001 to 9999]",
            "9999",
            # Forward sibling of the case above: same first-failing-site
            # answer whichever side each bad year was typed on.
            id="both-bad-forward-names-first-failing-value",
        ),
        pytest.param(
            "created:{'9999-12-31' TO '9999-12-31 23:59:59.999999'}",
            "9999-12-31 23:59:59.999999",
            id="exclusive-bracket-exact-end-stays-blamed",
            # No swap. The end bound is exact, but the user's exclusive
            # bracket makes date_only ceil it up a day, overflowing: the
            # probe must model that same typed exclusivity, or the end
            # bound looks healthy and the innocent max-ceiling start gets
            # blamed instead.
        ),
    ],
)
def test_range_diagnostic_names_failing_bound_after_joint_swap(
    reg: FieldRegistry, query: str, bad_bound: str
) -> None:
    # The joint-disambiguation step swaps a backwards range with two
    # explicit years (whoosh's own range heuristic), after which the lo/hi
    # conversion failures no longer line up with the bounds' textual
    # positions: the year-1 end bound becomes lo (its Berlin-to-UTC
    # conversion underflows datetime.min), the year-9999 start bound
    # becomes hi (its exclusive ceiling overflows datetime.max). The
    # diagnostic must name the bound whose VALUE failed, not whichever
    # bound sat at that side of the brackets.
    res = dparse(query, reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].raw_value == bad_bound
    assert bad_bound in res.diagnostics[0].message


def test_swapped_exact_max_instant_does_not_spuriously_overflow(reg: FieldRegistry) -> None:
    # Sibling of "innocent-max-ceiling-start-not-blamed" above, but for an
    # EXACT (fully microsecond-specified) start instant rather than an
    # ambiguous whole-day one: 9999-12-31 23:59:59.999999 (exact) genuinely
    # swaps with the year-only "9999" end bound here (unlike the ambiguous
    # '9999-12-31' sibling, which does not swap), landing the exact value
    # at hi_naive.
    #
    # Before start_exact/end_exact followed the swap (see
    # test_backwards_swap_carries_exactness_with_the_value), this used
    # end_exact's STALE, pre-swap value (raw_end = "9999", ambiguous, so
    # False) to decide whether hi_naive needed the exclusive-ceiling
    # +1-microsecond bump. hi_naive was actually the exact start value by
    # then, which needs no such bump; adding one to
    # 9999-12-31 23:59:59.999999 overflowed past datetime.max and raised a
    # bogus BAD_DATE blaming "9999", even though every value the user typed
    # was individually representable. Now that end_exact follows the swap,
    # the exact value is recognized as exact and no adjustment applies.
    res = dparse("added:['9999-12-31 23:59:59.999999' to 9999]", reg)
    assert not res.diagnostics
    assert isinstance(res.ast, ast.DateRange)
    assert res.ast.incl_hi is True
    assert res.ast.hi == datetime(9999, 12, 31, 22, 59, 59, 999999, tzinfo=UTC)


def test_range_bad_start_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:[notadate TO 2020]", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    assert res.diagnostics[0].raw_value == "notadate"


def test_range_bad_end_diagnostic(reg: FieldRegistry) -> None:
    res = dparse("added:[2020 TO notadate]", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE
    assert res.diagnostics[0].field == FieldRef("added")
    # The end bound is the offending text here, not the start bound: the
    # diagnostic's raw_value must track whichever bound the message names.
    assert res.diagnostics[0].raw_value == "notadate"


# --- torange Combo grammar (free "X to Y" text inside one field value) -----


def test_torange_combo_basic(reg: FieldRegistry) -> None:
    r = dparse("added:'3pm to 5pm'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    assert r.hi is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert lo_local.hour == 15
    # The upper side is the end of the 5pm hour, not its start: each side of
    # a "to" combo is itself an hour-wide window, and the range takes the
    # far edge of the second one.
    assert r.hi.astimezone(BERLIN).hour == 18
    assert not r.incl_hi


def test_torange_combo_first_side_fails_whole_thing_fails(reg: FieldRegistry) -> None:
    # Combo.parse's `if at is None: return (None, None)`: the first bundle
    # doesn't match at all, so neither the "to" combo nor any dmy/bundle
    # alternative in the outer Choice matches either.
    res = dparse("added:'notadate to 2020'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_sequence_fill_in_type_error_rejected(reg: FieldRegistry) -> None:
    # Combining day=31 with month=february (2020, a leap year -> Feb has 29
    # days) inside the dmy Sequence raises TimeError from fill_in()'s
    # adatetime(**args) construction (parser/dateparse.py:233-236); Sequence
    # swallows it and reports failure, so no dmy/mdy/... alternative
    # matches and the whole field value is an unparseable date.
    res = dparse("created:'31 february 2020'", reg)
    assert res.diagnostics
    assert res.diagnostics[0].kind is DiagnosticKind.BAD_DATE


def test_bag_time_and_date_both_match_in_one_value(reg: FieldRegistry) -> None:
    # English.setup's `self.datetime = Bag((self.time, self.dmy))`: both
    # sub-elements matching in the same value hits the `onceper and
    # all(seen)` early-break (parser/dateparse.py:416-417).
    r = dparse("added:'3pm 4 august 2020'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo is not None
    lo_local = r.lo.astimezone(BERLIN)
    assert (lo_local.year, lo_local.month, lo_local.day, lo_local.hour) == (2020, 8, 4, 15)


def test_dateparser_parse_method_direct() -> None:
    # DateParser.parse() (distinct from .date_from(), which is what
    # DateParserPlugin actually calls) is unused by whoosh-compat's own
    # code: same as upstream whoosh, where it's likewise dead from the
    # plugin's perspective (only .date_from() is called there too). It's
    # still a documented, non-trivial public method of the grammar classes,
    # so it's covered here directly rather than deleted.
    parser = English()
    result, pos = parser.parse("2020", datetime(2026, 1, 1))
    # Unlike DateParserPlugin (which turns a period into an exclusive
    # ceil+1us upper bound), DateParser.parse() returns times.py's raw
    # disambiguated() result: an inclusive floor/ceil timespan.
    assert result == timespan(
        datetime(2020, 1, 1, 0, 0, 0, 0), datetime(2020, 12, 31, 23, 59, 59, 999999)
    )
    assert pos == 4


# --- Low-level grammar-class internals (unreachable via whoosh_compat.parse
# because DateParserPlugin only ever calls .date_from(), never .parse(),
# and never triggers debug tracing or repr()) ------------------------------


def test_dateparse_internals_repr_and_debug() -> None:
    props = dp.Props(year=2020)
    assert repr(props) == repr({"year": 2020})
    with pytest.raises(AttributeError):
        _ = props.nope

    seq = dp.Sequence((dp.Regex("x"),), name="s")
    assert repr(seq) == "Sequence<s>[<'x'>]"

    assert repr(dp.Regex("abc")) == "<'abc'>"
    assert repr(dp.ToEnd(dp.Regex("abc"))) == "ToEnd(<'abc'>)"

    # print_debug's body only runs when level > 0; DateParserPlugin never
    # passes a positive debug level, so this exercises it directly.
    dp.print_debug(1, "msg %s", "x")


def test_parserbase_date_from_defaults_dt_to_now() -> None:
    # ParserBase.date_from() (inherited by Sequence/Combo/Choice/Bag/Regex/
    # ToEnd, none of which override it) falls back to datetime.now() when
    # no dt is given. Production code (DateParserPlugin) always passes an
    # explicit basedate through DateParser.date_from(), so this default is
    # only reachable by calling a low-level element directly.
    regex = dp.Regex(r"(?P<year>[0-9]{4})", lambda p, dt: dt)
    result = regex.date_from("2020")
    assert result is not None


def test_regex_props_to_date_default_uses_all_units() -> None:
    regex = dp.Regex(r"(?P<year>[0-9]{4})")
    result, _ = regex.parse("2020", datetime(2026, 1, 1))
    assert result == adatetime(year=2020)


def test_dateparser_parse_exact_datetime_skips_disambiguation() -> None:
    # DateParser.parse()'s `if isinstance(d, (adatetime, timespan))` branch
    # is skipped when the grammar already resolves to an exact datetime
    # ("now"), rather than an ambiguous adatetime/timespan.
    parser = English()
    dt = datetime(2026, 1, 1, 10, 30)
    result, _ = parser.parse("now", dt)
    assert result == dt


def test_dateparser_date_from_defaults_and_toend_false() -> None:
    parser = English()
    # basedate=None -> falls back to datetime.utcnow() internally.
    result = parser.date_from("2020")
    assert result is not None

    # toend=False: trailing garbage after a valid match is tolerated.
    result2 = parser.date_from("2020 garbage", datetime(2026, 1, 1), toend=False)
    assert result2 is not None


def test_daterangesyntaxnode_and_dateerrornode_r() -> None:
    node = DateRangeSyntaxNode("added", datetime(2020, 1, 1), datetime(2020, 1, 2), True, False)
    assert node.r() == f"DateRange {datetime(2020, 1, 1)!r}-{datetime(2020, 1, 2)!r}"

    diag = Diagnostic(
        message="bad",
        kind=DiagnosticKind.BAD_DATE,
        cause=Cause.INVALID_INPUT,
        startchar=0,
        endchar=1,
    )
    err = DateErrorNode(diag)
    assert err.r() == "DateError 'bad'"


def test_bare_date_keyword_under_query_parser_default_date_field(reg: FieldRegistry) -> None:
    # do_dates() only date-parses a node with a fieldname. An unfielded
    # term's fieldname is None until QueryParser.fieldname's default-field
    # fallback fills it in; do_dates() used to skip that fallback, so a bare
    # date keyword under a single-field QueryParser with a date default
    # field fell through as an ordinary text term instead of a date range.
    # MultifieldParser (what whoosh_compat.parse() always builds) has no
    # single default fieldname (it passes None and expands unfielded terms
    # into a per-field OR instead), so this path is only reachable through
    # the QueryParser API directly.
    parser = QueryParser("created", reg)
    parser.add_plugin(DateParserPlugin(BASE, BERLIN))
    node = ast.normalize(parser.parse("yesterday"))
    assert node == ast.DateRange(
        field=FieldRef("created"),
        lo=datetime(2026, 8, 3, tzinfo=UTC),
        hi=datetime(2026, 8, 4, tzinfo=UTC),
        incl_lo=True,
        incl_hi=False,
    )


def test_naive_basedate_rejected_by_plugin_construction() -> None:
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        DateParserPlugin(naive, BERLIN)


def test_naive_basedate_rejected_via_parse(reg: FieldRegistry) -> None:
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        wc.parse(
            "created:today", registry=reg, default_fields=["content"], tz=BERLIN, basedate=naive
        )


def test_naive_basedate_rejected_even_without_date_fields() -> None:
    # The rejection is parse()'s own contract, not a side effect of
    # DateParserPlugin construction: a registry with no DATE/DATETIME
    # fields (where the plugin is never attached) must reject the same
    # host misconfiguration instead of silently accepting and ignoring
    # it, so whether bad config raises cannot depend on unrelated
    # registry contents.
    text_only = FieldRegistry([FieldSpec("content", FieldKind.TEXT)])
    naive = datetime(2026, 8, 4, 10, 30)  # no tzinfo
    with pytest.raises(ValueError, match="aware"):
        wc.parse("foo", registry=text_only, default_fields=["content"], basedate=naive)


def test_aware_basedate_with_same_wall_clock_still_works(reg: FieldRegistry) -> None:
    # Pins the fixed behavior without manipulating the process timezone
    # (time.tzset() isn't available on Windows): an aware basedate resolves
    # against its own tzinfo regardless of the machine's local zone, since
    # "aware" is now a hard requirement rather than something silently
    # reinterpreted in the host's local zone.
    aware = datetime(2026, 8, 4, 23, 0, tzinfo=BERLIN)
    r = wc.parse(
        "added:yesterday", registry=reg, default_fields=["content"], tz=BERLIN, basedate=aware
    ).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2026, 8, 3, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 8, 4, 0, 0, tzinfo=BERLIN).astimezone(UTC)
    assert not r.incl_hi


def test_do_dates_leaves_non_range_non_text_date_field_node_untouched(reg: FieldRegistry) -> None:
    # do_dates()'s trailing `else: continue` (has_fieldname but neither a
    # RangeNode nor has_text) has no real-world producer in the current
    # plugin set: FieldnameNode/RangeNode/TextNode are the only
    # has_fieldname=True node types, and FieldnameNode never survives as a
    # leaf. Exercised directly against a minimal stub node.
    class FieldOnlyNode(syntax.SyntaxNode):
        has_fieldname = True

        def __init__(self, fieldname: str) -> None:
            self.fieldname = fieldname

    plugin = DateParserPlugin(BASE, BERLIN)

    class StubParser:
        registry = reg

    group = syntax.AndGroup([FieldOnlyNode("added")])
    result = plugin.do_dates(StubParser(), group)
    assert result[0] is group[0]  # untouched, not replaced


def test_range_exclusive_brackets_honored_for_exact_bounds(reg: FieldRegistry) -> None:
    # DIVERGENCES.md entry 44: an exact bound ("now", a concrete instant)
    # keeps the bracket exclusivity the user typed, where real whoosh's
    # DateRangeNode silently drops the flags (always inclusive-both, the
    # same plumbing oversight class as entry 3's boost drop). The
    # ambiguous-bound counterpart directly below pins the inverse rule.
    r = dparse("added:{now TO now}", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == BASE.astimezone(UTC)
    assert r.hi == BASE.astimezone(UTC)
    assert r.incl_lo is False
    assert r.incl_hi is False
    inclusive = dparse("added:[now TO now]", reg).ast
    assert isinstance(inclusive, ast.DateRange)
    assert inclusive.incl_lo is True
    assert inclusive.incl_hi is True


def test_range_exclusive_bounds_ignored_for_ambiguous_bounds(reg: FieldRegistry) -> None:
    # "2020"/"2021" are ambiguous (year-only) bounds, not exact instants, so
    # range_to_node forces incl_lo=True regardless of the query's "{" exclusive
    # marker (parser/dateparse.py:999-1000): only incl_hi is
    # forced-exclusive (it already carries the "+1us" half-open adjustment).
    r = dparse("added:{2020 TO 2021}", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.incl_lo is True
    assert r.incl_hi is False


# --- RFC3339 "Z" recognition is linear in the value length ----------------


def test_rfc3339_z_check_is_linear_in_value_length() -> None:
    """The "Z" gate looks for a "T" somewhere before a trailing "Z"; the
    regex spelling of that used to backtrack quadratically over a run of
    "T"s (50 K "T"s cost 15.6 s), which any authenticated user can send as
    a date bound.

    Sized so the guard cannot be flaky rather than so it is quick. Measured
    on the author's machine: this shape cost 231 s before the fix and 2.6 ms
    after (the accepting case below, 0.2 ms), against the 2 s budget, so the
    room is ~760x on the passing side and two orders of magnitude on the
    failing one. Absolute seconds are hardware-specific; the durable claim
    is the shape, quadratic-in-"T"s before and linear after. A ratio between
    two input sizes was rejected: at these costs both timings are
    noise-dominated and the ratio would measure nothing.
    """
    plugin = DateParserPlugin(BASE, BERLIN)
    text = "T" * 200_000
    start = time.perf_counter()
    body, is_utc = plugin._split_rfc3339_utc(text)
    elapsed = time.perf_counter() - start
    # No trailing "Z", so this is not the designator and the text is
    # returned untouched: checked, not just timed, so a "fast" version that
    # stopped recognizing anything would fail here.
    assert (body, is_utc) == (text, False)
    assert elapsed < 2.0


def test_rfc3339_z_designator_still_recognized_on_a_long_value() -> None:
    # The accepting half of the same shape: a real (if absurdly long) "T"-run
    # ending in the designator is still split, and still cheaply.
    plugin = DateParserPlugin(BASE, BERLIN)
    body = "T" * 200_000
    start = time.perf_counter()
    got, is_utc = plugin._split_rfc3339_utc(body + "Z")
    elapsed = time.perf_counter() - start
    assert (got, is_utc) == (body, True)
    assert elapsed < 2.0


def test_to_span_split_is_linear_in_value_length() -> None:
    """The two-sided "A to B" splitter used to look for its separator with a
    quantifier-based regex (``(?:\\s+|\\s*,\\s*)to(?:\\s+|\\s*,\\s*)``) matched
    with ``.split()``. Against a long run of separator characters containing
    no "to" at all, that pattern's leading run got re-attempted at every
    offset within the run, and each attempt re-scanned the remaining run
    before failing: quadratic in the input length (measured: 11.6 s for
    50,000 spaces), the same class of user-controlled-input DoS as the
    "Z"-designator check above, just a different pattern shape.

    Sized and budgeted the same way as the "Z" tests above: large enough
    that the shape (quadratic before, linear after) cannot be flaky noise.
    """
    plugin = DateParserPlugin(BASE, BERLIN)
    text = " " * 200_000
    start = time.perf_counter()
    body, start_utc, end_utc = plugin._split_span_rfc3339_utc(text)
    elapsed = time.perf_counter() - start
    # No "to" at all, so this falls back to the single-value path untouched.
    assert (body, start_utc, end_utc) == (text, False, False)
    assert elapsed < 2.0


def test_to_span_split_still_recognized_on_long_values() -> None:
    # The accepting half of the same shape: a real two-sided span still
    # splits correctly (each side's own "Z" recognized independently), and
    # still cheaply, even with absurdly long bounds on both sides.
    plugin = DateParserPlugin(BASE, BERLIN)
    left = "T" * 100_000
    right = "T" * 100_000
    start = time.perf_counter()
    body, start_utc, end_utc = plugin._split_span_rfc3339_utc(f"{left}Z to {right}")
    elapsed = time.perf_counter() - start
    assert (body, start_utc, end_utc) == (f"{left} to {right}", True, False)
    assert elapsed < 2.0


# -- Quoted vs bracketed relative-span exactness agree (bug fix, no --------
# -- DIVERGENCES.md entry: whoosh-compat now agrees with whoosh, see --------
# -- tests/differential/corpus_realworld.txt's "CONFIRMED PARITY" line) ----


def test_quoted_relative_span_exact_end_matches_bracketed_sibling(
    reg: FieldRegistry,
) -> None:
    """``created:'-1 year to now'`` and ``created:[-1 year TO now]`` name the
    same interval and must produce the same upper bound: an exact instant
    the user actually typed ("now") keeps the inclusive treatment, not the
    half-open exclusive-ceiling adjustment meant for an ambiguous period end.

    Before the fix, text_to_node's timespan branch applied the +1-microsecond
    exclusive adjustment to ANY multi-value span (whenever start != end),
    with no check for whether the specific end value was already exact,
    unlike range_to_node's per-bound start_exact/end_exact gate.
    """
    quoted = dparse("created:'-1 year to now'", reg).ast
    bracketed = dparse("created:[-1 year TO now]", reg).ast
    assert isinstance(quoted, ast.DateRange)
    assert isinstance(bracketed, ast.DateRange)
    assert quoted.hi == bracketed.hi
    assert quoted.incl_hi == bracketed.incl_hi
    assert quoted.incl_hi is True
    assert quoted.lo == bracketed.lo
    assert quoted.incl_lo == bracketed.incl_lo


def test_quoted_relative_span_ambiguous_end_still_gets_ceiling_adjustment(
    reg: FieldRegistry,
) -> None:
    """Control: when the end bound genuinely IS ambiguous (a bare year, not
    an exact instant like "now"), the quoted span must still apply the
    half-open exclusive-ceiling adjustment, matching its bracketed sibling.
    Confirms the fix is a real exactness check, not "always inclusive".
    """
    quoted = dparse("created:'2019 to 2020'", reg).ast
    bracketed = dparse("created:[2019 TO 2020]", reg).ast
    assert isinstance(quoted, ast.DateRange)
    assert isinstance(bracketed, ast.DateRange)
    assert quoted.hi == bracketed.hi
    assert quoted.incl_hi == bracketed.incl_hi
    assert quoted.incl_hi is False


def test_quoted_relative_span_z_designator_does_not_leak_to_the_other_bound(
    reg: FieldRegistry,
) -> None:
    """A quoted two-sided span mixing an RFC3339 "Z"-suffixed bound with a
    plain local one must give each bound its own tz treatment, the same way
    _range_to_node already does for a bracketed range (see
    test_rfc3339_range_mixes_z_and_non_z_bound). Before the fix,
    _text_to_node computed a single force_utc flag over the whole quoted
    text, so "now" here was silently reinterpreted as an already-UTC wall
    clock time instead of shifted from local Europe/Berlin.

    "now to 2020-01-01T00:00:00Z" is a backwards-typed span (2020 sorts
    before "now"), so joint disambiguation swaps it to lo=2020, hi=now:
    this also exercises the tz swap following bounds_swapped, alongside
    the exactness swap test_quoted_relative_span_exactness_follows_a_backwards_swap
    already covers.
    """
    r = dparse("added:'now to 2020-01-01T00:00:00Z'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 1, 1, tzinfo=UTC)
    assert r.hi == BASE.astimezone(UTC)
    assert r.incl_lo is True
    assert r.incl_hi is True


def test_quoted_relative_span_z_designator_on_the_other_side(reg: FieldRegistry) -> None:
    """Same shape, Z on the other (chronologically-first) bound: the plain
    local "now" end must not be pulled into UTC either.
    """
    r = dparse("added:'2020-01-01T00:00:00Z to now'", reg).ast
    assert isinstance(r, ast.DateRange)
    assert r.lo == datetime(2020, 1, 1, tzinfo=UTC)
    assert r.hi == BASE.astimezone(UTC)
    assert r.incl_lo is True
    assert r.incl_hi is True


@pytest.mark.parametrize(
    ("quoted_query", "bracketed_query"),
    [
        pytest.param(
            "created:'now to 2020'",
            "created:[now TO 2020]",
            id="exact-typed-first-ambiguous-second",
        ),
        pytest.param(
            "created:'2027 to -1 year'",
            "created:[2027 TO -1 year]",
            id="ambiguous-typed-first-exact-second",
        ),
    ],
)
def test_quoted_relative_span_exactness_follows_a_backwards_swap(
    reg: FieldRegistry, quoted_query: str, bracketed_query: str
) -> None:
    """A backwards two-sided span ("now to 2020": the exact value typed
    first sorts AFTER the ambiguous one, so joint disambiguation swaps
    which value lands at lo vs hi) must still agree with its bracketed
    sibling: whichever value ends up at hi, that value's OWN exactness (not
    positional "first typed" exactness) decides the ceiling adjustment,
    mirroring range_to_node's own bounds_swapped handling.
    """
    quoted = dparse(quoted_query, reg).ast
    bracketed = dparse(bracketed_query, reg).ast
    assert isinstance(quoted, ast.DateRange)
    assert isinstance(bracketed, ast.DateRange)
    assert quoted.hi == bracketed.hi
    assert quoted.incl_hi == bracketed.incl_hi
    assert quoted.lo == bracketed.lo
    assert quoted.incl_lo == bracketed.incl_lo


# Today's diagnostics for shapes entry 61 must not disturb, measured before
# the rule existed. An empty list means the query parses cleanly.
_BASELINE_DIAGNOSTICS: dict[str, list[str]] = {
    "created:2020 invoice": [],
    "added:now-3days": ["now-3days"],
    "added:previous month": [],
    'created:"december 2019"': [],
    "created:'december 2019'": [],
    "created:[2020 TO 2021]": [],
}


@pytest.mark.parametrize(
    ("query", "raw_value"),
    [
        pytest.param("created:december 2019", "december 2019", id="named-month-year"),
        pytest.param("created:2020 to 2021", "2020 to 2021", id="two-sided-year-range"),
        pytest.param("created:2020 august 4", "2020 august 4", id="year-month-day"),
        pytest.param("added:-1 week", "-1 week", id="relative-offset"),
        pytest.param("added:12 december 2019", "12 december 2019", id="day-month-year"),
        pytest.param("created:-1 year to now", "-1 year to now", id="relative-range"),
    ],
)
def test_unquoted_multiword_date_value_is_rejected(
    reg: FieldRegistry, query: str, raw_value: str
) -> None:
    """An unquoted date value the grammar can consume in full is rejected
    naming the whole run, rather than truncating to its first token and
    letting the remainder become free-text terms (DIVERGENCES.md entry 61).
    """
    res = dparse(query, reg)
    assert len(res.diagnostics) == 1
    d = res.diagnostics[0]
    assert d.kind is DiagnosticKind.BAD_DATE
    assert d.raw_value == raw_value
    # The span covers the entire run, from the value's first character to
    # the last joined word's last character.
    assert d.startchar == query.index(":") + 1
    assert d.endchar == len(query)
    # No leftover term survives from inside the rejected run.
    assert not [n for n in _nodes(res.ast) if isinstance(n, ast.Term)]


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("created:2020 invoice", id="remainder-is-not-a-date"),
        pytest.param("added:now-3days", id="prefix-stops-mid-token"),
        pytest.param("added:previous month", id="entry-19-keyword-phrase-still-works"),
        pytest.param('created:"december 2019"', id="double-quoted-already-correct"),
        pytest.param("created:'december 2019'", id="single-quoted-already-correct"),
        pytest.param("created:[2020 TO 2021]", id="bracketed-already-correct"),
    ],
)
def test_unquoted_date_rejection_leaves_other_shapes_alone(reg: FieldRegistry, query: str) -> None:
    """The rule fires only when a joined candidate is consumed in full.
    Everything else parses exactly as it did before entry 61.
    """
    res = dparse(query, reg)
    assert [d.raw_value for d in res.diagnostics] == _BASELINE_DIAGNOSTICS[query]
