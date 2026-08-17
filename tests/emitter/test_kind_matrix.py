"""Exhaustiveness matrix over (field kind, leaf spelling, registry
configuration).

The dominant recurring defect class in this codebase is a rule implemented
for one combination of (leaf node type, field kind, value spelling) and
forgotten for its siblings: several fixed and open issues are instances of
exactly this (see the module docstring's issue references below and
DIVERGENCES.md). This module enumerates the matrix instead of leaving it
implicit, so a new sibling gap fails loudly instead of waiting for the next
manual review to notice it.

Every cell must resolve to exactly one of three legal outcomes (see
ARCHITECTURE.md's "closed matrix" invariant):

1. a parse-time ``Diagnostic`` plus ``ast.ErrorLeaf`` (:class:`Diag`),
2. a documented emit-time raise, ``QueryEmitError``/``UnsupportedQueryError``
   (:class:`Raises`), or
3. a real search executed against a live tantivy index that demonstrably
   honors the field's kind and JSON subpath, asserted as a doc-id set
   (:class:`Search`).

A cell that parses cleanly (no diagnostics) and then emits a query that
either raises something undocumented or silently returns the wrong result
set is not a legal outcome for any cell, ever.

Rows are derived from :class:`~whoosh_compat.fields.FieldKind` plus the
registry configurations that are known to change dispatch: JSON with a fast
vs. non-fast subpath, DATE (always ``date_only``) vs. DATETIME, a
``comma_values`` U64 field vs. a plain one, and a BOOLEAN_EXISTS field whose
``exists_target`` is fast vs. non-fast. ``test_every_fieldkind_has_a_row``
below is the enforcement mechanism for the acceptance criterion "adding a
new FieldKind member without classifying its cells fails the suite": it
walks the live ``FieldKind`` enum, not a hand-copied list, so a new member
shows up there as an assertion failure until someone adds rows for it.

Columns are the leaf productions reachable from query text: bare term, single- and
double-quoted, prefix star, ``?`` wildcard, bracket-class wildcard, bare
``field:*``, a bracketed range, a comma list, and a ``^N`` boost. Not every
column is a meaningful, distinct production for every row (a comma list is
only a distinct shape from a bare value on a ``comma_values=True`` field;
BOOLEAN_EXISTS/JSON have no numeric or date bound to range over), so a row
lists only the columns that are syntactically reachable and semantically
distinct for its configuration; ``ROW_COLUMNS`` records, per row, exactly
which columns were classified, and
``test_every_row_classifies_its_applicable_columns`` checks each row's
classified set against that declared set so a silently dropped column also
fails loudly.

Known-bad cells (issues #9, #11 [closed against a prior symptom, kept below
as a regression control], #17, #35) are marked
``pytest.mark.xfail(strict=True)`` with the target outcome this project has
already decided on (see each issue and its DIVERGENCES.md cross-reference);
a fix flips its cell from xfail to pass, and a regression un-flips it
loudly. Issue #34's cells were fixed and flipped to plain (non-xfail)
regression tests; the fallback branch's remaining, permanent slop
limitation is asserted directly rather than marked xfail, since it is
documented (DIVERGENCES.md entry 22), not a bug awaiting a fix.
"""

from __future__ import annotations

import dataclasses

import pytest
import tantivy
from _pytest.mark import ParameterSet

from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken

from .conftest import TIndex

# ---------------------------------------------------------------------------
# Outcome descriptors
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Diag:
    """Outcome 1: a parse-time Diagnostic plus an ErrorLeaf somewhere in the
    tree, and emit() raising QueryEmitError for it.
    """


@dataclasses.dataclass(frozen=True)
class Raises:
    """Outcome 2: a clean parse (no diagnostics) followed by a documented
    emit-time raise.
    """

    exc: type[Exception]
    match: str


@dataclasses.dataclass(frozen=True)
class Search:
    """Outcome 3: a clean parse and a real search whose matched doc-id set
    is asserted exactly.
    """

    ids: list[int]


def _contains_errorleaf(node: ast.Node) -> bool:
    """Whether ``node`` or any of its descendants is an ErrorLeaf.

    Generic over every AST node shape (no per-node-type special casing)
    using dataclasses.fields, so it doesn't need updating when a new node
    type is added.
    """
    if isinstance(node, ast.ErrorLeaf):
        return True
    for f in dataclasses.fields(node):
        value = getattr(node, f.name)
        if isinstance(value, ast.Node) and _contains_errorleaf(value):
            return True
        if isinstance(value, tuple):
            for item in value:
                if isinstance(item, ast.Node) and _contains_errorleaf(item):
                    return True
    return False


def _run(qs: str, ereg: FieldRegistry, tindex: TIndex, outcome: object) -> None:
    r = _parse(qs, registry=ereg, default_fields=["content"])

    if isinstance(outcome, Diag):
        assert r.diagnostics, f"expected a parse-time diagnostic for {qs!r}, got none"
        assert _contains_errorleaf(r.ast), f"expected an ErrorLeaf in the tree for {qs!r}"
        with pytest.raises(QueryEmitError):
            emit_(r.ast, index=tindex[0], registry=ereg)
        return

    if isinstance(outcome, Raises):
        assert not r.diagnostics, (
            f"expected a clean parse for {qs!r} (diagnostic deferred to emit), "
            f"got {r.diagnostics!r}"
        )
        with pytest.raises(outcome.exc, match=outcome.match):
            emit_(r.ast, index=tindex[0], registry=ereg)
        return

    if isinstance(outcome, Search):
        assert not r.diagnostics, f"expected a clean parse for {qs!r}, got {r.diagnostics!r}"
        q = emit_(r.ast, index=tindex[0], registry=ereg)
        s = tindex[0].searcher()
        ids = sorted(s.doc(addr)["id"][0] for _, addr in s.search(q, 10).hits)
        assert ids == outcome.ids, f"{qs!r} matched {ids}, expected {outcome.ids}"
        return

    raise AssertionError(f"unreachable: unknown outcome type {outcome!r}")


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
#
# Uses the shared ``ereg``/``tindex`` fixtures from conftest.py, whose field
# registry already covers every row this matrix needs:
#   content   TEXT
#   tag       KEYWORD, comma_values=True
#   asn       U64, fast, no comma_values
#   tag_id    U64, fast, comma_values=True
#   created   DATE (always date_only), fast
#   added     DATETIME, fast
#   has_tag       BOOLEAN_EXISTS -> tag_id (fast target: FAST_FIELD strategy)
#   has_tag_kw    BOOLEAN_EXISTS -> tag (non-fast target: TERM_SCAN strategy)
#   notes     JSON, non-fast, subpaths=(note, user)
#   attrs     JSON, fast, subpaths=(note, user)
#
# and its DOCS fixture (tests/emitter/conftest.py):
#   1: title "Billing 2020", content "invoice total amount",
#      tags [billing, urgent], asn 100, created 2020-03-15,
#      added 2020-03-15T10:00:00Z, notes {note: "check this", user: "alice"}
#   2: "Billing 2019", "receipt shopname product1", tags [billing], asn 101,
#      created 2019-06-01, added 2019-06-01T09:00:00Z, notes None
#   3: "Wärrantyplan", "plan warranty basement", tags [], asn 102,
#      created 2018-03-23, added 2018-03-23T08:00:00Z, notes None
#   4: "Report 2020", "shopname product1 product2", tags [report], asn 103,
#      created 2020-11-30, added 2020-11-30T12:00:00Z,
#      notes {note: "final", user: "bob"}
#   5: "Miscellaneous Doc", "assorted filler content only", tags [], asn 99,
#      created 2019-03-01, added 2019-03-01T00:00:00Z,
#      notes {user: 'a"b\\c'}   (no "note" key)

# Every FieldKind member must appear as a key here (enforced by
# test_every_fieldkind_has_a_row). Values are the row ids that exercise it.
KIND_ROWS: dict[FieldKind, tuple[str, ...]] = {
    FieldKind.TEXT: ("text",),
    FieldKind.KEYWORD: ("keyword",),
    FieldKind.U64: ("u64-plain", "u64-comma"),
    FieldKind.DATE: ("date",),
    FieldKind.DATETIME: ("datetime",),
    FieldKind.BOOLEAN_EXISTS: ("boolean-exists-fast", "boolean-exists-nonfast"),
    FieldKind.JSON: ("json-nonfast", "json-fast"),
}

# Per-row id, the set of columns this matrix classifies for it. Checked by
# test_every_row_classifies_its_applicable_columns against the columns that
# actually show up in CELLS, so a column silently dropped from a row (typo
# in an id, a forgotten pytest.param) fails the suite instead of just
# shrinking the matrix quietly.
ALL_COLUMNS = frozenset(
    {
        "bare",
        "single-quoted",
        "double-quoted",
        "prefix-star",
        "wildcard",
        "bracket-class",
        "bare-star",
        "bracket-range",
        "comma-list",
        "boosted",
    }
)

ROW_COLUMNS: dict[str, frozenset[str]] = {
    # TEXT/KEYWORD: every column is a meaningful, distinct production.
    "text": ALL_COLUMNS,
    "keyword": ALL_COLUMNS,
    # U64: no bracket-range distinction from "comma-list" is needed beyond
    # what's already covered; comma-list is exercised on the comma_values
    # row instead of the plain one, where it is a distinct shape (an AND
    # of split values) rather than just a malformed-number diagnostic.
    "u64-plain": ALL_COLUMNS,
    "u64-comma": ALL_COLUMNS,
    # DATE/DATETIME: wildcard-ish columns collide with the date grammar
    # (WildcardPlugin never even sees them; DateParserPlugin claims the
    # text first and reports BAD_DATE), and comma_values is not a valid
    # DATE/DATETIME configuration at all (FieldRegistry rejects it), so
    # "comma-list" here just exercises what a literal, unsupported comma in
    # a date value does (still classified, just not distinct "comma list"
    # semantics): both rows classify it, so a future change routing
    # DATETIME comma values differently from DATE ones fails the matrix.
    "date": ALL_COLUMNS,
    "datetime": ALL_COLUMNS,
    # BOOLEAN_EXISTS: no numeric/date bound to range over, and comma_values
    # is not a valid BOOLEAN_EXISTS configuration.
    "boolean-exists-fast": ALL_COLUMNS - {"comma-list"},
    "boolean-exists-nonfast": ALL_COLUMNS - {"comma-list"},
    # JSON subpath: comma_values is not a valid JSON configuration.
    "json-nonfast": ALL_COLUMNS - {"comma-list"},
    "json-fast": ALL_COLUMNS - {"comma-list"},
}


def _param(
    row: str,
    column: str,
    qs: str,
    outcome: object,
    marks: pytest.MarkDecorator | None = None,
) -> ParameterSet:
    assert column in ROW_COLUMNS[row], f"{row}/{column} not declared in ROW_COLUMNS"
    if marks is None:
        return pytest.param(qs, outcome, id=f"{row}-{column}")
    return pytest.param(qs, outcome, id=f"{row}-{column}", marks=marks)


CELLS: list[ParameterSet] = [
    # -- TEXT (content) ----------------------------------------------------
    _param("text", "bare", "content:invoice", Search([1])),
    _param("text", "single-quoted", "content:'invoice'", Search([1])),
    _param("text", "double-quoted", 'content:"invoice"', Search([1])),
    _param("text", "prefix-star", "content:invoi*", Search([1])),
    _param("text", "wildcard", "content:invo?ce", Search([1])),
    _param("text", "bracket-class", "content:[i-i]nvoic*", Search([1])),
    _param("text", "bare-star", "content:*", Search([1, 2, 3, 4, 5])),
    _param(
        "text",
        "bracket-range",
        "content:[invoice TO invoice]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("text", "comma-list", "content:invoice,invoice", Search([1])),
    _param("text", "boosted", "content:invoice^2.0", Search([1])),
    # -- KEYWORD (tag, comma_values=True) -----------------------------------
    _param("keyword", "bare", "tag:urgent", Search([1])),
    _param("keyword", "single-quoted", "tag:'urgent'", Search([1])),
    _param("keyword", "double-quoted", 'tag:"urgent"', Search([1])),
    _param("keyword", "prefix-star", "tag:urg*", Search([1])),
    _param("keyword", "wildcard", "tag:ur?ent", Search([1])),
    _param("keyword", "bracket-class", "tag:[u-u]rgen*", Search([1])),
    _param("keyword", "bare-star", "tag:*", Search([1, 2, 4])),
    _param(
        "keyword",
        "bracket-range",
        "tag:[urgent TO urgent]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("keyword", "comma-list", "tag:billing,urgent", Search([1])),
    _param("keyword", "boosted", "tag:urgent^2.0", Search([1])),
    # -- U64 plain (asn, fast, no comma_values) -----------------------------
    _param("u64-plain", "bare", "asn:100", Search([1])),
    _param("u64-plain", "single-quoted", "asn:'100'", Search([1])),
    _param("u64-plain", "double-quoted", 'asn:"100"', Search([1])),
    _param("u64-plain", "prefix-star", "asn:100*", Diag()),
    _param("u64-plain", "wildcard", "asn:10?", Diag()),
    _param("u64-plain", "bracket-class", "asn:[1-1]*", Diag()),
    _param("u64-plain", "bare-star", "asn:*", Search([1, 2, 3, 4, 5])),
    _param("u64-plain", "bracket-range", "asn:[100 TO 100]", Search([1])),
    # No comma_values on this field: a literal comma in the value is just a
    # malformed number, diagnosed the same as any other non-numeric text.
    _param("u64-plain", "comma-list", "asn:100,100", Diag()),
    _param("u64-plain", "boosted", "asn:100^2.0", Search([1])),
    # -- U64 comma_values (tag_id, fast, presence marker value 1) ----------
    _param("u64-comma", "bare", "tag_id:1", Search([1, 2, 4])),
    _param("u64-comma", "single-quoted", "tag_id:'1'", Search([1, 2, 4])),
    _param("u64-comma", "double-quoted", 'tag_id:"1"', Search([1, 2, 4])),
    _param("u64-comma", "prefix-star", "tag_id:1*", Diag()),
    _param("u64-comma", "wildcard", "tag_id:1?", Diag()),
    _param("u64-comma", "bracket-class", "tag_id:[1-1]*", Diag()),
    _param("u64-comma", "bare-star", "tag_id:*", Search([1, 2, 4])),
    _param("u64-comma", "bracket-range", "tag_id:[1 TO 1]", Search([1, 2, 4])),
    _param("u64-comma", "comma-list", "tag_id:1,1", Search([1, 2, 4])),
    _param("u64-comma", "boosted", "tag_id:1^2.0", Search([1, 2, 4])),
    # -- DATE (created, always date_only, fast) -----------------------------
    _param("date", "bare", "created:2020-03-15", Search([1])),
    _param("date", "single-quoted", "created:'2020-03-15'", Search([1])),
    _param("date", "double-quoted", 'created:"2020-03-15"', Search([1])),
    # A trailing "*" on an otherwise-complete date: WildcardPlugin claims
    # the text (it contains "*"), folds to a Prefix, and the date value
    # underneath it still resolves to the same single day; not a wildcard
    # search over date bytes at all, so this is a legitimate Search cell,
    # not a Diag one.
    _param("date", "prefix-star", "created:2020-03-15*", Search([1])),
    _param("date", "wildcard", "created:20?", Diag()),
    _param("date", "bracket-class", "created:[2-2]*", Diag()),
    # A bare "*" on a DATE field is claimed by DateParserPlugin before
    # WildcardPlugin/EveryPlugin ever see it, and "*" is not a recognizable
    # date, so this diagnoses as BAD_DATE rather than an existence match
    # (DATE/DATETIME have no "field:*" existence shorthand at all; the only
    # way to ask "does this field have a value" for a date field is a
    # both-sides-open bracket range, exercised by _range_query's delegation
    # to _exists_query, see visit_daterange/visit_numericrange's shared
    # helper).
    _param("date", "bare-star", "created:*", Diag()),
    _param("date", "bracket-range", "created:[2020-03-15 TO 2020-03-15]", Search([1])),
    _param("date", "comma-list", "created:2020-03-15,2020-03-15", Diag()),
    _param("date", "boosted", "created:2020-03-15^2.0", Search([1])),
    # -- DATETIME (added, fast, not date_only) -------------------------------
    # A full ISO instant ("...T10:00:00Z") only parses within a quoted value
    # when written as a single field:value leaf (the colons in the
    # time-of-day are otherwise read as new field:value separators by the
    # bare/unquoted tokenizer; unrelated to the date grammar itself). "bare"
    # here uses a bare *date* (no time-of-day), which is a legitimate,
    # distinct, unquoted DATETIME leaf spelling in its own right: it
    # resolves to the whole day as a range, same day-granularity DATE gets,
    # just on a field that can also carry finer precision when quoted. The
    # bracketed-range column below is a different story: no colon
    # ambiguity inside a range's brackets, so "T"/"Z" datetimes parse there
    # unquoted (paperless-ngx PR #13010 back-compat; see dateparse.py's
    # ``_split_rfc3339_utc``).
    _param("datetime", "bare", "added:2020-03-15", Search([1])),
    _param(
        "datetime",
        "single-quoted",
        "added:'2020-03-15 10:00:00'",
        Search([1]),
    ),
    _param(
        "datetime",
        "double-quoted",
        'added:"2020-03-15 10:00:00"',
        Search([1]),
    ),
    _param("datetime", "prefix-star", "added:2020-03-15*", Search([1])),
    _param("datetime", "wildcard", "added:20?", Diag()),
    _param("datetime", "bracket-class", "added:[2-2]*", Diag()),
    # No "field:*" existence shorthand for DATE/DATETIME, same as the "date"
    # row above.
    _param("datetime", "bare-star", "added:*", Diag()),
    _param(
        "datetime",
        "bracket-range",
        "added:[2020-03-15T10:00:00Z TO 2020-03-15T10:00:00Z]",
        Search([1]),
    ),
    _param("datetime", "comma-list", "added:2020-03-15,2020-03-15", Diag()),
    _param("datetime", "boosted", "added:2020-03-15^2.0", Search([1])),
    # -- BOOLEAN_EXISTS, fast target (has_tag -> tag_id) ---------------------
    _param("boolean-exists-fast", "bare", "has_tag:true", Search([1, 2, 4])),
    _param("boolean-exists-fast", "single-quoted", "has_tag:'true'", Search([1, 2, 4])),
    _param("boolean-exists-fast", "double-quoted", 'has_tag:"true"', Search([1, 2, 4])),
    _param("boolean-exists-fast", "prefix-star", "has_tag:true*", Diag()),
    _param("boolean-exists-fast", "wildcard", "has_tag:tr?", Diag()),
    _param("boolean-exists-fast", "bracket-class", "has_tag:[t-t]*", Diag()),
    _param("boolean-exists-fast", "bare-star", "has_tag:*", Search([1, 2, 4])),
    _param(
        "boolean-exists-fast",
        "bracket-range",
        "has_tag:[true TO true]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("boolean-exists-fast", "boosted", "has_tag:true^2.0", Search([1, 2, 4])),
    # -- BOOLEAN_EXISTS, non-fast target (has_tag_kw -> tag, TERM_SCAN) -----
    _param("boolean-exists-nonfast", "bare", "has_tag_kw:true", Search([1, 2, 4])),
    _param("boolean-exists-nonfast", "single-quoted", "has_tag_kw:'true'", Search([1, 2, 4])),
    _param("boolean-exists-nonfast", "double-quoted", 'has_tag_kw:"true"', Search([1, 2, 4])),
    _param("boolean-exists-nonfast", "prefix-star", "has_tag_kw:true*", Diag()),
    _param("boolean-exists-nonfast", "wildcard", "has_tag_kw:tr?", Diag()),
    _param("boolean-exists-nonfast", "bracket-class", "has_tag_kw:[t-t]*", Diag()),
    _param("boolean-exists-nonfast", "bare-star", "has_tag_kw:*", Search([1, 2, 4])),
    _param(
        "boolean-exists-nonfast",
        "bracket-range",
        "has_tag_kw:[true TO true]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("boolean-exists-nonfast", "boosted", "has_tag_kw:true^2.0", Search([1, 2, 4])),
    # -- JSON, non-fast subpath (notes.user / notes.note) -------------------
    _param("json-nonfast", "bare", "notes.user:alice", Search([1])),
    _param("json-nonfast", "single-quoted", "notes.user:'alice'", Search([1])),
    _param("json-nonfast", "double-quoted", 'notes.user:"alice"', Search([1])),
    _param("json-nonfast", "prefix-star", "notes.user:ali*", Diag()),
    _param("json-nonfast", "wildcard", "notes.user:a?ice", Diag()),
    _param("json-nonfast", "bracket-class", "notes.user:[a-a]lic*", Diag()),
    _param(
        "json-nonfast",
        "bare-star",
        "notes.user:*",
        Raises(UnsupportedQueryError, r"notes\.user:\*"),
    ),
    _param(
        "json-nonfast",
        "bracket-range",
        "notes.user:[alice TO alice]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("json-nonfast", "boosted", "notes.user:alice^2.0", Search([1])),
    # -- JSON, fast subpath (attrs.user / attrs.note) ------------------------
    _param("json-fast", "bare", "attrs.user:alice", Search([1])),
    _param("json-fast", "single-quoted", "attrs.user:'alice'", Search([1])),
    _param("json-fast", "double-quoted", 'attrs.user:"alice"', Search([1])),
    _param("json-fast", "prefix-star", "attrs.user:ali*", Diag()),
    _param("json-fast", "wildcard", "attrs.user:a?ice", Diag()),
    _param("json-fast", "bracket-class", "attrs.user:[a-a]lic*", Diag()),
    # attrs.note:* must match only docs that actually have a "note" subpath
    # (1 and 4), not every doc that has any "attrs" value at all (1, 4, 5):
    # doc 5 only has "user", proving the subpath is really being checked.
    _param(
        "json-fast",
        "bare-star",
        "attrs.note:*",
        Search([1, 4]),
    ),
    _param(
        "json-fast",
        "bracket-range",
        "attrs.user:[alice TO alice]",
        Raises(UnsupportedQueryError, "text ranges"),
    ),
    _param("json-fast", "boosted", "attrs.user:alice^2.0", Search([1])),
]


@pytest.mark.parametrize(("qs", "outcome"), CELLS)
def test_kind_matrix_cell(qs: str, outcome: object, ereg: FieldRegistry, tindex: TIndex) -> None:
    _run(qs, ereg, tindex, outcome)


def test_every_fieldkind_has_a_row() -> None:
    """A new FieldKind member with no rows here fails this assertion
    instead of silently having zero matrix coverage (the matrix design's "adding a
    new FieldKind member without classifying its cells fails the suite"
    acceptance criterion).
    """
    missing = set(FieldKind) - set(KIND_ROWS)
    assert not missing, f"FieldKind member(s) with no matrix row: {missing}"
    for kind, rows in KIND_ROWS.items():
        assert rows, f"{kind} has an empty row list"
        for row in rows:
            assert row in ROW_COLUMNS, f"row {row!r} for {kind} has no declared ROW_COLUMNS entry"


def test_every_row_classifies_its_applicable_columns() -> None:
    """Every column declared applicable for a row in ROW_COLUMNS must
    actually appear as a classified cell in CELLS, and vice versa: a typo'd
    id or a forgotten pytest.param silently shrinks the matrix otherwise.
    """
    classified: dict[str, set[str]] = {row: set() for row in ROW_COLUMNS}
    for p in CELLS:
        cell_id = p.id
        assert isinstance(cell_id, str), f"cell {p!r} has no string id"
        # ids are "{row}-{column}"; row itself may contain hyphens
        # (e.g. "u64-plain"), so recover the split from the known row set.
        matched_row = next((r for r in ROW_COLUMNS if cell_id.startswith(r + "-")), None)
        assert matched_row is not None, f"cell id {cell_id!r} does not start with a known row id"
        column = cell_id[len(matched_row) + 1 :]
        classified[matched_row].add(column)

    for row, expected_columns in ROW_COLUMNS.items():
        assert classified[row] == expected_columns, (
            f"row {row!r}: classified columns {classified[row]} do not match "
            f"declared applicable columns {expected_columns}"
        )


# ---------------------------------------------------------------------------
# Additional known-bad cells requiring bespoke registries/indexes
# ---------------------------------------------------------------------------


def test_json_subpath_phrase_slop_and_multitoken(tindex: TIndex) -> None:
    """A quoted phrase on a JSON subpath must map whoosh slop the
    same way a plain-field phrase does, and must never consult
    Multitoken.FIRST (or any other multitoken mode): a Phrase node's text is
    a phrase, not a multitoken term value.

    The installed tantivy-py (0.26, pinned) has no JSON-subpath
    term_query/phrase_query support at all (tantivy-py#716 hasn't shipped),
    so JSON-subpath phrase emission always takes the index.parse_query
    fallback here, and that fallback has no query-string syntax tantivy-py
    honors for slop (verified directly against a live index: appending
    "~N" to the quoted phrase does not change the resulting query's slop
    away from 0). A widened whoosh slop therefore cannot recover a
    transposed-word match through this branch; that limitation is
    documented in DIVERGENCES.md entry 22 rather than silently dropped.
    The corresponding "supported" branch (once tantivy-py#716 ships) is
    pinned separately in test_emit_json.py, which forces that branch on a
    stand-in schema field to prove the max(slop - 1, 0) mapping there.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_json_field("notes", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_json("notes", {"user": "bob alice"})
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
            ),
        ]
    )
    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice bob", slop=99)
    q = emit_(node, index=ix, registry=reg)
    s = ix.searcher()
    ids = sorted(s.doc(a)["id"][0] for _, a in s.search(q, 10).hits)
    assert ids == [], (
        "the index.parse_query fallback cannot express slop at all: even a "
        "very wide whoosh slop must not recover a transposed-word match "
        "(DIVERGENCES.md entry 22); a match here would mean slop is being "
        "silently promised but not delivered"
    )


def test_json_subpath_phrase_ignores_multitoken_first(tindex: TIndex) -> None:
    """Second symptom of the same defect: Multitoken.FIRST must not apply to a
    quoted Phrase node (only to a multi-token Term value); a phrase's words
    are the phrase, not independent tokens to pick "the first" of.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_json_field("notes", stored=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 1)
    doc.add_json("notes", {"user": "bob alice"})
    w.add_document(doc)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=lambda t: [w for w in t.lower().split() if w],
                multitoken=Multitoken.FIRST,
            ),
        ]
    )
    # "alice bogus": the doc has "alice" (as its second word) but not
    # "bogus" at all. A Multitoken.FIRST collapse would search only the
    # query text's first analyzed token ("alice") as a bare term, which the
    # doc does contain, and wrongly match. A true phrase search for "alice
    # bogus" must not match, since "bogus" isn't in the doc at all: this is
    # what discriminates "still a real phrase query" from "silently
    # collapsed to a term query", unlike a doc/query pair where the first
    # token alone happens to be enough either way.
    node = ast.Phrase(field=FieldRef("notes", "user"), text="alice bogus", slop=1)
    q = emit_(node, index=ix, registry=reg)
    s = ix.searcher()
    ids = sorted(s.doc(a)["id"][0] for _, a in s.search(q, 10).hits)
    assert ids == [], (
        "a quoted phrase must still search as a phrase even when the field's "
        "Multitoken mode is FIRST; a non-empty match means the phrase collapsed "
        "to a single-term 'alice' search instead"
    )


def test_u64_phrase_drop_check_does_not_apply_text_policy() -> None:
    """The group-membership zero-token drop check
    (_node_drops/_drop_check_tokens) restricts itself to TEXT/KEYWORD for a
    Term, and now (post-fix) also restricts itself for a Phrase, matching
    how visit_phrase dispatches on kind. Configuring an analyzer on a U64
    spec is explicitly supported (a host may share one FieldSpec factory
    across kinds), so a double-quoted numeric value whose analyzer happens
    to consume its text must not be silently dropped from an enclosing
    And/Or group: standalone emission of the identical node builds a
    correct term query, and group membership must agree with that.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    sb.add_unsigned_field("asn", stored=True, indexed=True, fast=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    for id_, content, asn in [(1, "invoice total amount", 100), (5, "assorted filler", 0)]:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("content", content)
        doc.add_unsigned("asn", asn)
        w.add_document(doc)
    w.commit()
    ix.reload()

    def lower_fold_min2(text: str) -> list[str]:
        return [t for t in text.lower().split() if len(t) >= 2]

    reg = FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lower_fold_min2),
            FieldSpec("asn", FieldKind.U64, fast=True, analyzer=lower_fold_min2),
        ]
    )
    r = _parse('invoice AND asn:"0"', registry=reg, default_fields=["content"])
    assert not r.diagnostics
    q = emit_(r.ast, index=ix, registry=reg)
    s = ix.searcher()
    ids = sorted(s.doc(a)["id"][0] for _, a in s.search(q, 10).hits)
    assert ids == [], (
        "doc 1's asn is 100, not 0: the AND'd asn:\"0\" constraint must still apply "
        "even though the U64 spec's analyzer would tokenize the phrase text '0' down "
        "to zero tokens; getting [1] means the phrase was wrongly dropped from the "
        "enclosing And as if it were a TEXT/KEYWORD zero-token value"
    )

    standalone = _parse('asn:"0"', registry=reg, default_fields=["content"])
    assert not standalone.diagnostics
    standalone_q = emit_(standalone.ast, index=ix, registry=reg)
    standalone_ids = sorted(s.doc(a)["id"][0] for _, a in s.search(standalone_q, 10).hits)
    assert standalone_ids == [5], (
        'asn:"0" emitted standalone must still match doc 5 (asn=0), proving '
        "emission itself is correct and only group membership was ever in question"
    )


def test_boolean_exists_phrase_in_group_is_not_dropped() -> None:
    """Control for the zero-token drop check: a BOOLEAN_EXISTS phrase must never be treated as
    droppable by analysis either (it was never subject to tokenization to
    begin with), pinning the other non-TEXT/KEYWORD kind through the same
    group-membership path.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    sb.add_unsigned_field("tag_id", indexed=True, fast=True)
    schema = sb.build()
    ix = tantivy.Index(schema)
    w = ix.writer()
    doc1 = tantivy.Document()
    doc1.add_unsigned("id", 1)
    doc1.add_text("content", "filler")
    doc1.add_unsigned("tag_id", 1)
    w.add_document(doc1)
    doc2 = tantivy.Document()
    doc2.add_unsigned("id", 2)
    doc2.add_text("content", "filler")
    w.add_document(doc2)
    w.commit()
    ix.reload()

    reg = FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: t.lower().split()),
            FieldSpec("tag_id", FieldKind.U64, fast=True),
            FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
        ]
    )
    r = _parse('filler AND has_tag:"true"', registry=reg, default_fields=["content"])
    assert not r.diagnostics
    q = emit_(r.ast, index=ix, registry=reg)
    s = ix.searcher()
    ids = sorted(s.doc(a)["id"][0] for _, a in s.search(q, 10).hits)
    assert ids == [1]


@pytest.mark.parametrize(
    ("qs", "outcome"),
    [
        pytest.param("attrs:*", Search([1, 4, 5]), id="json-bare-fast-bare-star-is-exists"),
        pytest.param("attrs:foo", Search([]), id="json-bare-fast-real-term-still-demotes"),
        pytest.param(
            "notes:*",
            Raises(UnsupportedQueryError, r"'notes' \(JSON\) has no way to match 'exists'"),
            id="json-bare-nonfast-bare-star-still-raises",
        ),
        pytest.param(
            "attrs.user:*", Search([1, 4, 5]), id="json-subpath-bare-star-unaffected-boundary"
        ),
    ],
)
def test_json_bare_field_bare_star_existence(
    qs: str, outcome: object, ereg: FieldRegistry, tindex: TIndex
) -> None:
    """Bare-JSON demotion: demoting a bare JSON field name addressed with a
    real term (attrs:foo) must not also demote the bare-star existence
    special case (attrs:*), which the emitter still fully supports and
    which worked end to end before the original demotion fix. "notes" (the
    non-fast JSON field) still raises the documented UnsupportedQueryError
    for the same shape (the subpath-unsupported message), and "attrs.user:*" (an actual
    subpath existence check) is unaffected, pinning the boundary this fix's
    dispatch logic sits next to.
    """
    _run(qs, ereg, tindex, outcome)
