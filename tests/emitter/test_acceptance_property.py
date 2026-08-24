"""Result-level property test: does whoosh-compat's full parse -> emit ->
tantivy-search pipeline return the same matched-document-id set as real
whoosh's parse -> search pipeline, for the *generated* query language
(``tests/differential/strategies.py``), not just the hand-named scenarios
``test_acceptance_e2e.py`` already covers?

``tests/differential`` answers "does the parsed AST shape agree with real
whoosh?" That AST-level comparison catches most regressions cheaply (no
index-building, no search execution), but DIVERGENCES.md entry 16 names a
real, still-open risk: an allowlisted AST-level divergence (the tree
genuinely differs) can still happen to produce identical search results on
one fixture's data while producing *different* results on another (entry
16's own example: "dates near a local-midnight boundary"). This module is
the harness that actually checks results, not just trees, against the wider
generated query surface, so that risk gets test pressure instead of staying
a documented-but-unverified possibility.

Design, mirroring test_acceptance_e2e.py's shape:

- ``windex_prop``/``tindex_prop`` (module-scoped fixtures below) build a
  real whoosh index and a real tantivy index ONCE, from the same
  :data:`DOCS_PROP` fixture rows, not once per generated example (building
  either index per Hypothesis example would be far too slow to run in the
  committed suite).
- The dual index reuses ``tests/differential/oracle.py``'s
  ``oracle_schema()`` (whoosh side) and ``ORACLE_REGISTRY`` (whoosh-compat
  side) directly rather than inventing a third schema: this is also what
  lets the generator (``tests/differential/strategies.py``'s
  ``query_text()``) be reused completely as-is, since its field-name pools
  are drawn from exactly this registry already. The tantivy schema mirrors
  ``ORACLE_REGISTRY`` field-for-field (see ``_build_tantivy_schema``), so
  every field name the generator can produce resolves identically on both
  sides.
- :data:`DOCS_PROP` is a *different* fixture from ``test_acceptance_e2e.py``'s
  ``DOCS`` (``conftest.py``), deliberately, because DIVERGENCES.md entry 16
  explains that fixture's dates are never close enough to a local-midnight
  boundary, or its wildcard patterns never uppercase, for the two AST-level
  divergences it names (entries 2 and 12) to become result-changing there.
  :data:`DOCS_PROP` instead specifically includes:
  (a) ``created``/``modified``/``added`` values whose local Europe/Berlin
  calendar day (and, for one row, calendar year) differs from their UTC
  calendar day, to actually exercise entry 16's own named risk;
  (b) content mixing hyphenation, an apostrophe, a diacritic and stopwords,
  so tokenization-edge behavior is exercised by more than one fixture; and
  (c) a populated ``release_date`` (date_only) and ``attrs`` (JSON) value on
  every row: both are whoosh-compat-only concepts (DIVERGENCES.md entries 37
  and 14) with no v2 whoosh schema counterpart at all, so ``windex_prop``
  never uses this data; it exists purely so generated queries against those
  fields have something real to search against, on whoosh-compat's side.

Skip rule (mirroring the AST-level layer's own "entry 6" diagnostic skip,
`tests/differential/test_differential.py`): a
generated query is skipped, not compared, when whoosh-compat itself reports
a parse diagnostic (nothing coherent to search with), when whoosh-compat's
own emit() raises a documented `QueryError`
(ARCHITECTURE.md's "kind dispatch is a closed matrix": a parseable AST can
still be a legitimate emit-time error), or when the real-whoosh oracle
raises either while parsing or while actually searching (no oracle result
to compare against at all, broader than the AST-level grammar fuzzer's
parse-only try/except in `tests/differential/test_hypothesis.py`, since this
layer runs a real search, which can fail in whoosh for shapes that parse
fine, e.g. a Phrase query against a positionless KEYWORD field). Every query
that survives all of those either gets an equal-document-id-set assertion,
or, if `result_allowlist.allowed_result_reason` matches, is skipped as a
known, already-proven-elsewhere divergence: see `_acceptance_property`'s own
comment for why this layer skips an allowlisted generated query rather than
asserting strict inequality against effectively random data, unlike the
AST-comparison layer's structural strict-xfail. The strict-xfail proof
itself lives at the hand-picked-scenario layer just above the generator
wiring in this module.

Soak run: the committed example count (``_MAX_EXAMPLES`` below) is much
smaller than the differential layer's (300), since every example here
executes a real tantivy search *and* a real whoosh search against live
indexes, not just two parses. For a deeper local soak (mirroring
``strategies.py``'s own "raise ``max_leaves`` for a local soak" convention),
set the ``WHOOSH_COMPAT_ACCEPTANCE_SOAK_EXAMPLES`` environment variable,
e.g.::

    WHOOSH_COMPAT_ACCEPTANCE_SOAK_EXAMPLES=2000 uv run pytest \\
        tests/emitter/test_acceptance_property.py -q
"""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import tantivy
from hypothesis import example
from hypothesis import given
from hypothesis import settings
from whoosh.filedb.filestore import RamStorage
from whoosh.index import Index as WhooshIndex

from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import oracle_parse
from tests.differential.oracle import oracle_schema
from tests.differential.strategies import query_text
from tests.emitter.conftest import TIndex
from tests.emitter.conftest import search_ids
from tests.emitter.conftest import whoosh_search_ids
from tests.emitter.result_allowlist import allowed_result_reason
from whoosh_compat import parse as wc_parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2020, 4, 15, 10, 30, tzinfo=BERLIN)

# ORACLE_REGISTRY was built for AST-shape comparison only (tests/differential),
# never for real emission: several non-TEXT/KEYWORD fields (created/modified/
# added, and the two whoosh-compat-only fields attrs/release_date) are not
# marked fast=True there. resolve_exists_strategy (fields.py) has no way to
# answer "exists" for a non-fast field of any kind other than TEXT/KEYWORD
# (which fall back to a term-dictionary scan instead), so a bare field:*
# generated by the grammar-aware generator (an Every() node) raises
# QueryError at emit time for exactly those fields, a genuine
# fixture-configuration gap, not a parity divergence to report. _PROP_REGISTRY
# is ORACLE_REGISTRY's specs with fast=True added for every kind that needs
# it to support existence checks (U64/DATE/DATETIME/JSON); field names and
# kinds are untouched, so the generator (tied to ORACLE_REGISTRY's field
# pools) still produces exactly the same query strings. Used for whoosh-compat
# parsing/emission and to build the mirrored tantivy schema; the real-whoosh
# side (windex_prop, oracle_schema()) has no concept of "fast" at all and is
# unaffected either way.
_FAST_NEEDED_KINDS = (FieldKind.U64, FieldKind.DATE, FieldKind.DATETIME, FieldKind.JSON)
_PROP_REGISTRY = FieldRegistry(
    [
        dataclasses.replace(spec, fast=True) if spec.kind in _FAST_NEEDED_KINDS else spec
        for spec in ORACLE_REGISTRY
    ]
)


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Fixture rows. See the module docstring for why each row exists.
#
# "created"/"modified"/"added" are entered as Europe/Berlin-local, aware
# datetimes and converted to naive UTC at index-build time for both engines
# (mirroring conftest.py's windex/tindex convention: v2 whoosh stores
# UTC-naive, and the tantivy side's DateRange emission also converts to
# naive UTC immediately before the range_query call, ARCHITECTURE.md's
# "Timezone contract").
# --------------------------------------------------------------------------

DOCS_PROP: list[dict[str, object]] = [
    {
        # Ordinary weekday, mid-afternoon: no local/UTC calendar-day
        # boundary anywhere near it, the control row.
        "id": 1,
        "title": "Quarterly Invoice Report",
        "content": "invoice total amount due payment",
        "tag": ["billing", "urgent"],
        "type": "invoice",
        "correspondent": "Acme Corp",
        "asn": 100,
        "created": datetime(2020, 6, 10, 14, 30, tzinfo=BERLIN),
        "modified": datetime(2020, 6, 10, 14, 30, tzinfo=BERLIN),
        "added": datetime(2020, 6, 10, 14, 30, tzinfo=BERLIN),
        "release_date": date(2020, 6, 10),
        "attrs": {"user": "alice", "note": "checked"},
    },
    {
        # Summer (UTC+2): local 00:15 on the 15th is UTC 22:15 on the
        # 14th, a genuine local-vs-UTC calendar-day boundary crossing.
        "id": 2,
        "title": "Late Night Submission",
        "content": "report submitted overnight",
        "tag": ["report"],
        "type": "report",
        "correspondent": "Acme Corp",
        "asn": 101,
        "created": datetime(2020, 6, 15, 0, 15, tzinfo=BERLIN),
        "modified": datetime(2020, 6, 15, 0, 15, tzinfo=BERLIN),
        "added": datetime(2020, 6, 15, 0, 15, tzinfo=BERLIN),
        "release_date": date(2020, 6, 15),
        "attrs": {"user": "bob", "note": "late"},
    },
    {
        # Winter (UTC+1): local 23:45 on Dec 31 is UTC 00:45 on Jan 1,
        # crossing a calendar day AND a calendar year boundary.
        "id": 3,
        "title": "New Year Eve Report",
        "content": "final report before year end",
        "tag": ["billing"],
        "type": "report",
        "correspondent": "Beta LLC",
        "asn": 102,
        "created": datetime(2020, 12, 31, 23, 45, tzinfo=BERLIN),
        "modified": datetime(2020, 12, 31, 23, 45, tzinfo=BERLIN),
        "added": datetime(2020, 12, 31, 23, 45, tzinfo=BERLIN),
        "release_date": date(2020, 12, 31),
        "attrs": {"user": "alice", "note": "year end"},
    },
    {
        # Tokenization-edge content (hyphenation, an apostrophe, a
        # diacritic, stopwords) plus another local-midnight boundary
        # (winter, crossing a calendar month this time).
        "id": 4,
        "title": "Wärranty-Plan O'Brien",
        "content": "the plan is a co-operative warranty of the engine",
        "tag": ["warranty"],
        "type": "plan",
        "correspondent": "O'Brien & Co",
        "asn": 103,
        "created": datetime(2019, 3, 1, 0, 5, tzinfo=BERLIN),
        "modified": datetime(2019, 3, 1, 0, 5, tzinfo=BERLIN),
        "added": datetime(2019, 3, 1, 0, 5, tzinfo=BERLIN),
        "release_date": date(2019, 3, 1),
        "attrs": {"user": "carol", "note": "wärranty note"},
    },
]


# --------------------------------------------------------------------------
# Dual-index construction: built once (module scope), not once per example.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def windex_prop() -> WhooshIndex:
    schema = oracle_schema()
    names = set(schema.names())
    ix = RamStorage().create_index(schema)
    writer = ix.writer()
    for row in DOCS_PROP:
        fields: dict[str, object] = {}
        for key, value in row.items():
            if key not in names:
                continue
            if key == "tag":
                fields[key] = ",".join(value)  # type: ignore[arg-type]
            elif isinstance(value, datetime):
                fields[key] = _to_naive_utc(value)
            elif key == "release_date":
                continue  # not in oracle_schema(): see module docstring
            else:
                fields[key] = value
        # Every BOOLEAN_EXISTS target this fixture leaves unpopulated
        # (correspondent_id/type_id/tag_id/path_id/custom_field_count/
        # owner_id are never set on any DOCS_PROP row, see the module
        # docstring: they're outside this property's entry-16 focus) must
        # still be written explicitly as False here, not left absent.
        # whoosh-compat's BOOLEAN_EXISTS resolves via fast-field presence
        # (fields.py's resolve_exists_strategy), so "the id column has no
        # value for this doc" always means an unambiguous False; real
        # whoosh's plain BOOLEAN field instead indexes nothing at all for
        # an omitted field, and a Term(field, False) query then matches
        # NEITHER true nor false docs that never got a value written
        # (confirmed directly), a fixture-population gap, not a parity
        # divergence, the same convention conftest.py's own windex fixture
        # already follows (every doc always sets has_tag explicitly).
        for has_field in (
            "has_correspondent",
            "has_tag",
            "has_type",
            "has_path",
            "has_custom_fields",
            "has_owner",
        ):
            fields[has_field] = False
        writer.add_document(**fields)
    writer.commit()
    return ix


def _build_tantivy_schema(registry: FieldRegistry) -> tantivy.Schema:
    """Mirror ``registry`` field-for-field into a tantivy schema.

    A ``BOOLEAN_EXISTS`` field has no tantivy field of its own: it always
    resolves through its ``exists_target``'s own (already-mirrored) field.
    """

    sb = tantivy.SchemaBuilder()
    for spec in registry:
        if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            sb.add_text_field(spec.name, stored=True, fast=spec.fast)
        elif spec.kind is FieldKind.U64:
            sb.add_unsigned_field(spec.name, stored=True, indexed=True, fast=spec.fast)
        elif spec.kind in (FieldKind.DATE, FieldKind.DATETIME):
            sb.add_date_field(spec.name, stored=True, indexed=True, fast=spec.fast)
        elif spec.kind is FieldKind.JSON:
            sb.add_json_field(spec.name, stored=True, fast=spec.fast)  # type: ignore[call-arg]
        elif spec.kind is FieldKind.BOOLEAN_EXISTS:
            continue
        else:  # pragma: no cover - closed FieldKind enum, see ARCHITECTURE.md
            raise AssertionError(spec.kind)
    return sb.build()


@pytest.fixture(scope="module")
def tindex_prop() -> TIndex:
    schema = _build_tantivy_schema(_PROP_REGISTRY)
    index = tantivy.Index(schema)
    w = index.writer()
    for row in DOCS_PROP:
        doc = tantivy.Document()
        doc.add_unsigned("id", row["id"])  # type: ignore[arg-type]
        doc.add_text("title", row["title"])  # type: ignore[arg-type]
        doc.add_text("content", row["content"])  # type: ignore[arg-type]
        for t in row["tag"]:  # type: ignore[attr-defined]
            doc.add_text("tag", t)
        doc.add_text("type", row["type"])  # type: ignore[arg-type]
        doc.add_text("correspondent", row["correspondent"])  # type: ignore[arg-type]
        doc.add_unsigned("asn", row["asn"])  # type: ignore[arg-type]
        doc.add_date("created", _to_naive_utc(row["created"]))  # type: ignore[arg-type]
        doc.add_date("modified", _to_naive_utc(row["modified"]))  # type: ignore[arg-type]
        doc.add_date("added", _to_naive_utc(row["added"]))  # type: ignore[arg-type]
        release_date = row["release_date"]
        assert isinstance(release_date, date)
        doc.add_date(
            "release_date", datetime(release_date.year, release_date.month, release_date.day)
        )
        doc.add_json("attrs", row["attrs"])
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


def tantivy_search_ids_prop(tindex_prop: TIndex, q: str) -> list[int]:
    result = wc_parse(
        q, registry=_PROP_REGISTRY, default_fields=V2_FIELDS, basedate=BASE, tz=BERLIN
    )
    query = emit_(result.ast, index=tindex_prop[0], registry=_PROP_REGISTRY)
    return search_ids(tindex_prop[0], query)


def whoosh_search_ids_prop(windex_prop: WhooshIndex, q: str) -> list[int]:
    return whoosh_search_ids(windex_prop, q, BASE, BERLIN)


# --------------------------------------------------------------------------
# Step 1: hand-picked scenarios proving the dual-index machinery itself
# works, before wiring in the generator (this repo's TDD convention).
# --------------------------------------------------------------------------

SCENARIOS_EQUAL = [
    pytest.param("content:invoice", [1], id="term-invoice"),
    pytest.param("tag:billing", [1, 3], id="keyword-tag-billing"),
    pytest.param("asn:[100 TO 101]", [1, 2], id="numeric-range"),
    pytest.param("correspondent:Acme", [1, 2], id="term-correspondent"),
    pytest.param("content:cooperative", [], id="hyphen-split-tokenizes-two-words-not-one"),
    pytest.param("content:co", [4], id="hyphen-split-first-half-matches"),
    # DIVERGENCES.md entry 23's match-all face, through the real public
    # pipeline (wc.parse() -> emit() -> a live tantivy search), not just
    # an AST comparison: "the" is a StandardAnalyzer stopword, so this
    # must match every document on both sides. Both spellings run here
    # because wc.parse() and TantivyEmitter.emit() each call
    # ast.normalize() on the tree before analysis ever runs, so the rule
    # that keeps the unfielded Every alive has to hold in normalize()
    # itself for the real API, not only inside analyze().
    pytest.param("*:* title:the", [1, 2, 3, 4], id="entry23-match-all-face"),
]


@pytest.mark.parametrize(("q", "expected"), SCENARIOS_EQUAL)
def test_scenario_equal(
    windex_prop: WhooshIndex, tindex_prop: TIndex, q: str, expected: list[int]
) -> None:
    assert whoosh_search_ids_prop(windex_prop, q) == expected
    assert tantivy_search_ids_prop(tindex_prop, q) == expected


def test_parenthesized_match_all_face_matches_everything(tindex_prop: TIndex) -> None:
    """DIVERGENCES.md entry 23's match-all face in its parenthesized
    spelling, which is the one a caller who normalized the tree themselves
    before analyzing it used to get wrong (the unfielded ``Every`` dropped
    as the AND identity before analysis could learn the sibling was a
    stopword, leaving "matches nothing").

    Only the whoosh-compat side is asserted here, unlike
    ``entry23-match-all-face`` above: real whoosh parses this spelling into
    a tree containing a literal ``And([])`` (the parenthesized zero-token
    term), and *searching* it raises ``ValueError`` out of
    ``CompoundQuery.estimate_size``'s ``min()`` over no subqueries. That is
    a defect in the oracle, not a behavior to reproduce, so there is no
    oracle result set to compare against. The AST-level comparison for this
    exact query does run, and agrees, from
    ``tests/differential/corpus_docs.txt``.
    """
    assert tantivy_search_ids_prop(tindex_prop, "(*:*) AND (title:the)") == [1, 2, 3, 4]


def test_created_range_near_local_midnight_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 12, proven at the result level (entry 16's own
    named risk): doc 2's ``created`` is Europe/Berlin-local 2020-06-15
    00:15, i.e. UTC 2020-06-14 22:15.

    whoosh-compat correctly parses a bracketed separated-ISO range bound
    (``range_to_node``) into a same-day, tz-converted range: local
    2020-06-15 00:00:00 to 2020-06-16 00:00:00, i.e. UTC 2020-06-14 22:00 to
    2020-06-15 22:00, which includes doc 2's timestamp and only doc 2's.

    Confirmed directly (not assumed) that real whoosh's own range parsing
    for this exact shape degrades much further than DIVERGENCES.md entry
    12's tz-bypass description alone would suggest: ``range_to_dt``'s
    grammar-ordering limitation (the same one entry 18 documents for bare
    values) fails to recognize the separated-ISO bound as a full date at
    all for a *range*, and falls back to a bare year, producing
    ``DateRange(2020-01-01T00:00 TO 2020-12-31T23:59:59.999999)``, the
    entire year 2020, which matches docs 1, 2 and 3 (every 2020 row) and
    excludes only doc 4 (2019). Both behaviors are real; this test asserts
    what each side actually does, not a hypothesis about the bug's shape.
    """

    q = "created:[2020-06-15 TO 2020-06-15]"
    assert whoosh_search_ids_prop(windex_prop, q) == [1, 2, 3]
    assert tantivy_search_ids_prop(tindex_prop, q) == [2]
    assert allowed_result_reason(q) is not None


def test_attrs_and_release_date_are_result_level_divergences(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entries 14/37, proven at the result level: real whoosh
    has neither field registered, so its FieldsPlugin demotes the fieldname
    to an ordinary unfielded term searched across V2_FIELDS instead of a
    JSON-subpath/date-only query, a different search entirely, not just a
    differently-shaped tree for the same search.
    """

    q1 = "attrs.user:alice"
    assert allowed_result_reason(q1) is not None
    assert tantivy_search_ids_prop(tindex_prop, q1) == [1, 3]
    # Real whoosh: "attrs" and "user" become two unfielded, multifield-
    # expanded, implicitly-ANDed words; neither appears literally in any
    # V2_FIELDS content, so it matches nothing.
    assert whoosh_search_ids_prop(windex_prop, q1) == []

    q2 = "release_date:'2020-06-10 00:00'"
    assert allowed_result_reason(q2) is not None
    assert tantivy_search_ids_prop(tindex_prop, q2) == [1]
    assert whoosh_search_ids_prop(windex_prop, q2) == []


def test_padded_boolean_exists_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 33, proven at the result level: every
    ``has_correspondent`` value in this fixture is explicitly False
    (``windex_prop``/``tindex_prop`` never populate ``correspondent_id``),
    so a query that reads True on one side and False on the other produces
    maximally different results: everyone, or no one.
    """

    q = "has_correspondent:'  false'"
    assert allowed_result_reason(q) is not None
    # whoosh-compat: strips before the trues/falses check, reads False,
    # matches every document (has_correspondent is False for all of them).
    assert tantivy_search_ids_prop(tindex_prop, q) == [1, 2, 3, 4]
    # Real whoosh: BOOLEAN._obj_to_bool's unstripped check falls through to
    # bool(qstring), True for this non-empty padded string, matches none.
    assert whoosh_search_ids_prop(windex_prop, q) == []


def test_not_of_nested_empty_group_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 40: NOT of a group that recursively collapses to
    empty matches nothing in whoosh-compat at every nesting depth (the
    deliberate empty-group-drops-out parser rule, see ``GroupNode.query()``
    in ``parser/syntax.py``), but real whoosh's own AST for the equivalent
    shape is depth-dependent: a single level ("NOT ()") also collapses to
    "matches nothing" (both sides agree, so that shape is deliberately
    excluded from the allowlist regex), but two or more levels of nesting
    keep a real ``Not(And([And([])]))`` tree whose *execution* matches
    every document. The nesting can be a bare paren ("NOT (())") or a
    second, inner "NOT" that itself collapses to empty ("NOT ((NOT ()))"):
    both reach the identical ``Wrapper.query``/``CompoundQuery.__len__``
    mechanism (a falsy-but-non-``None`` empty ``And([])`` misread as "no
    operand at all" by real whoosh's ``if q:`` truthiness check), so both
    diverge the same way. Paren-only wrapping with no outer "NOT" at all
    ("((NOT ()))", "(NOT ())") does not reach this mechanism and agrees
    with whoosh-compat instead.

    This shape was previously invisible to ``tests/differential``: real
    whoosh's tree for the depth>=2 case has no oracle-side null subquery
    for ``oracle.to_ast``'s Not/And branches to collapse the same way, so
    ``to_ast`` returns ``None`` ("oracle-unmappable") and the comparison is
    skipped there entirely; only a real dual-index search (this property)
    can see the two sides return different document sets at all.
    """

    q_agrees = "NOT ()"
    assert allowed_result_reason(q_agrees) is None
    assert whoosh_search_ids_prop(windex_prop, q_agrees) == []
    assert tantivy_search_ids_prop(tindex_prop, q_agrees) == []

    q_agrees_paren_only = "((NOT ()))"
    assert allowed_result_reason(q_agrees_paren_only) is None
    assert whoosh_search_ids_prop(windex_prop, q_agrees_paren_only) == []
    assert tantivy_search_ids_prop(tindex_prop, q_agrees_paren_only) == []

    q_diverges = "NOT ((())^0.5)"
    assert allowed_result_reason(q_diverges) is not None
    assert whoosh_search_ids_prop(windex_prop, q_diverges) == [1, 2, 3, 4]
    assert tantivy_search_ids_prop(tindex_prop, q_diverges) == []

    q_diverges_nested_not = "NOT ((NOT ()))"
    assert allowed_result_reason(q_diverges_nested_not) is not None
    assert whoosh_search_ids_prop(windex_prop, q_diverges_nested_not) == [1, 2, 3, 4]
    assert tantivy_search_ids_prop(tindex_prop, q_diverges_nested_not) == []


def test_quoted_rfc3339_value_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 48: a quoted T-separated datetime value
    parses to _NullQuery in whoosh (its grammar has no T separator and
    the fallback chain bottoms out empty), matching nothing, while
    whoosh-compat parses the correct DateRange and returns the document
    the user meant. The Z spelling additionally proves the UTC-designator
    semantics end to end: doc 1's created instant is 14:30 Berlin, i.e.
    12:30 UTC, and only the absolute-UTC reading matches it.
    """

    q_local = "created:'2019-03-01T00:05:00'"
    assert allowed_result_reason(q_local) is not None
    assert whoosh_search_ids_prop(windex_prop, q_local) == []
    assert tantivy_search_ids_prop(tindex_prop, q_local) == [4]

    q_utc = "created:'2020-06-10T12:30:00Z'"
    assert allowed_result_reason(q_utc) is not None
    assert whoosh_search_ids_prop(windex_prop, q_utc) == []
    assert tantivy_search_ids_prop(tindex_prop, q_utc) == [1]


def test_no_separator_t_value_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 50: a no-separator T-fused value is
    unreadable by whoosh's grammar (_NullQuery, matches nothing) while
    whoosh-compat reads year-T-month and returns every document in that
    window: docs 1 and 2 both carry June-2020 "added" instants.
    """

    for q in ("added:2020T06", "added:'2020T06'"):
        assert allowed_result_reason(q) is not None
        assert whoosh_search_ids_prop(windex_prop, q) == []
        assert tantivy_search_ids_prop(tindex_prop, q) == [1, 2]


def test_bare_rfc3339_value_is_a_result_level_divergence() -> None:
    """DIVERGENCES.md entry 54, superseding entry 49: the bare unquoted
    T-separated spelling is cut mid-token by the colon tokenizer. Real
    whoosh swallows the dangling separator, searches the truncated
    month/year window ANDed with the stray time tokens, and returns
    nothing at all here -- silently, for a document that IS inside every
    window the fragment names. whoosh-compat refuses to build a query for
    it and reports BAD_DATE instead, so the user learns the spelling is
    wrong rather than being told there are no matching documents.

    This test builds its own one-document dual index (the same
    dedicated-corpus convention as test_acceptance_e2e.py's issue-13568
    scenario) rather than using the shared DOCS_PROP fixture, whose
    documents carry no bare-number tokens: the content here contains one
    of the stray tokens ("30"), which is what real whoosh's per-field AND
    (needing "30" and "00" both) still fails to match on.
    """

    added = datetime(2020, 6, 15, 14, 0, tzinfo=BERLIN)
    naive = datetime(2020, 6, 15, 12, 0)
    assert _to_naive_utc(added) == naive

    wix = RamStorage().create_index(oracle_schema())
    writer = wix.writer()
    writer.add_document(
        id=7,
        title="Meeting Notes",
        content="meeting agenda item 30 review",
        added=naive,
        created=naive,
        modified=naive,
        has_correspondent=False,
        has_tag=False,
        has_type=False,
        has_path=False,
        has_custom_fields=False,
        has_owner=False,
    )
    writer.commit()

    schema = _build_tantivy_schema(_PROP_REGISTRY)
    tix = tantivy.Index(schema)
    tw = tix.writer()
    doc = tantivy.Document()
    doc.add_unsigned("id", 7)
    doc.add_text("title", "Meeting Notes")
    doc.add_text("content", "meeting agenda item 30 review")
    for date_field in ("added", "created", "modified"):
        doc.add_date(date_field, naive)
    tw.add_document(doc)
    tw.commit()
    tix.reload()

    def tantivy_ids(q: str) -> list[int]:
        result = wc_parse(
            q, registry=_PROP_REGISTRY, default_fields=V2_FIELDS, basedate=BASE, tz=BERLIN
        )
        assert not result.diagnostics
        return search_ids(tix, emit_(result.ast, index=tix, registry=_PROP_REGISTRY))

    # The no-day spelling exercises the year-window face of the same
    # entry: the fragment left behind is "2020-"-shaped rather than
    # "2020-06-"-shaped (measured: added:2020-06T10:30 diagnoses on
    # "2020-", the other two on "2020-06-"), and whoosh reads it as the
    # whole of 2020, which the document also sits inside.
    for q in (
        "added:2020-06-15T10:30:00",
        "added:2020-06-15T10:30:00Z",
        "added:2020-06T10:30",
    ):
        assert allowed_result_reason(q) is not None
        assert whoosh_search_ids(wix, q, BASE, BERLIN) == []
        diagnostics = wc_parse(
            q, registry=_PROP_REGISTRY, default_fields=V2_FIELDS, basedate=BASE, tz=BERLIN
        ).diagnostics
        assert [d.kind for d in diagnostics] == [DiagnosticKind.BAD_DATE]

    # Control: the same instant in the spelling entry 48 recommends (and
    # the one paperless-ngx generates) is honored, so the rejection above
    # is about the unquoted spelling being cut in half, not about the
    # value or the document.
    assert tantivy_ids("added:'2020-06-15T14:00:00'") == [7]


def test_not_under_andnot_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 47: whoosh's root-only Not composes
    incoherently as an AndNot operand. For the deep-fuzz finding query
    the positive side alone matches doc 4 and the negative
    (``NOT (0)``) alone matches every document, so set algebra gives the
    empty set, which tantivy computes; whoosh returns the positive side
    unchanged, ignoring the all-matching negative. The parsed ASTs are
    identical on both sides, so only a real dual-index search can see
    this divergence at all.
    """

    q = "(NOT (created:'this year')) ANDNOT (NOT (0))"
    assert allowed_result_reason(q) is not None
    assert whoosh_search_ids_prop(windex_prop, q) == [4]
    assert tantivy_search_ids_prop(tindex_prop, q) == []
    # The components, agreeing on both sides, isolating the composition.
    assert whoosh_search_ids_prop(windex_prop, "NOT (created:'this year')") == [4]
    assert tantivy_search_ids_prop(tindex_prop, "NOT (created:'this year')") == [4]
    assert whoosh_search_ids_prop(windex_prop, "NOT (0)") == [1, 2, 3, 4]
    assert tantivy_search_ids_prop(tindex_prop, "NOT (0)") == [1, 2, 3, 4]


def test_andnot_zero_token_positive_matches_everything_here(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 23, extended to ANDNOT/ANDMAYBE/REQUIRE: a
    *parenthesized* zero-token positive operand ("the" is a stopword,
    dropped by title's StandardAnalyzer) leaves the negative operand
    standing alone in whoosh-compat (``_lone_operand``'s uniform,
    timing-independent policy), but real whoosh's parenthesized-operand
    tree keeps a live ``AndNot(And([]), ...)`` whose empty-And positive side
    matches zero documents, dragging the whole compound to zero regardless
    of the negative side. Unlike the bare (unparenthesized) form, which
    whoosh-compat and real whoosh already agree on
    (``test_zero_token_operand_leaves_the_survivor``,
    ``tests/emitter/test_emit_boolean.py``).
    """

    q = "(title:the) ANDNOT (content:invoice)"
    assert allowed_result_reason(q) is not None
    # whoosh-compat: positive drops, so _lone_operand returns the negative
    # operand's own (non-negated) query standing alone, exactly as if the
    # ANDNOT and its dropped positive side had never been typed: matches
    # whichever documents contain "invoice" (doc 1), not its exclusion.
    assert tantivy_search_ids_prop(tindex_prop, q) == [1]
    # Real whoosh: the parenthesized, now-empty positive side is
    # unsatisfiable, so the whole AndNot matches nothing.
    assert whoosh_search_ids_prop(windex_prop, q) == []


def test_numeric_range_small_bounds_is_a_whoosh_bug(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 41: a real-whoosh ``NumericRange`` with a
    small-magnitude bound matches every document regardless of any
    document's actual field value, confirmed by bisecting a single-value
    range directly against a live oracle index (this fixture's ``asn``
    values are 100-103; ``N <= 10`` breaks, ``N >= 20`` is fine). No
    document here has ``asn`` anywhere near either bound tested. The
    allowlist entry for this divergence is broad (any bracketed numeric
    range, see ``result_allowlist.py``'s entry-41 comment for why), so a
    "fine" bound below is verified directly here rather than by checking
    ``allowed_result_reason`` is ``None`` for it.
    """

    q_broken = "asn:[5 TO 5]"
    assert allowed_result_reason(q_broken) is not None
    assert tantivy_search_ids_prop(tindex_prop, q_broken) == []
    assert whoosh_search_ids_prop(windex_prop, q_broken) == [1, 2, 3, 4]

    q_fine = "asn:[100 TO 100]"
    assert tantivy_search_ids_prop(tindex_prop, q_fine) == [1]
    assert whoosh_search_ids_prop(windex_prop, q_fine) == [1]


def test_numeric_range_exclusive_bound_is_a_whoosh_bug(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 41's second confirmed manifestation: an
    ordinary-looking bracketed ``NumericRange`` can also match every
    document once its upper bound crosses 127 (``2**7 - 1``), regardless of
    exclusivity (doc 1's ``asn`` is exactly 100, so an exclusive lower
    bound of 100 should exclude it either way).
    """

    q_broken = "asn:{100 TO 127]"
    assert allowed_result_reason(q_broken) is not None
    assert tantivy_search_ids_prop(tindex_prop, q_broken) == [2, 3, 4]
    assert whoosh_search_ids_prop(windex_prop, q_broken) == [1, 2, 3, 4]

    q_fine = "asn:{100 TO 101]"
    assert tantivy_search_ids_prop(tindex_prop, q_fine) == [2]
    assert whoosh_search_ids_prop(windex_prop, q_fine) == [2]


def test_multitoken_default_or_context_is_a_result_level_divergence(
    windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    """DIVERGENCES.md entry 15, confirmed at the result level: an unfielded
    (hence multifield-expanded into a top-level ``Or``) two-token dashed
    word combines its tokens with OR in whoosh-compat
    (``Multitoken.DEFAULT`` follows the syntactic enclosing group) but with
    AND in real whoosh (always follows the parser's fixed default group).
    Doc 3's title contains "Year" but not "00".
    """

    q = "00-YEAR"
    assert allowed_result_reason(q) is not None
    assert tantivy_search_ids_prop(tindex_prop, q) == [3]
    assert whoosh_search_ids_prop(windex_prop, q) == []

    # Second trigger pathway: a bare registered-JSON-field value (entry 29's
    # demotion) that itself analyzes to two surviving tokens ("attrs",
    # "end"), reaching the identical mechanism.
    q2 = "attrs:END"
    assert allowed_result_reason(q2) is not None
    assert tantivy_search_ids_prop(tindex_prop, q2) == [3]
    assert whoosh_search_ids_prop(windex_prop, q2) == []


# --------------------------------------------------------------------------
# Step 2: the grammar-aware generator (tests/differential/strategies.py),
# reused as-is, run through both full pipelines.
# --------------------------------------------------------------------------

_DEFAULT_MAX_EXAMPLES = 40
_MAX_EXAMPLES = int(os.environ.get("WHOOSH_COMPAT_ACCEPTANCE_SOAK_EXAMPLES", _DEFAULT_MAX_EXAMPLES))

# Explicit seeds: the divergences this property was built to prove exist,
# always exercised regardless of what the random search happens to draw.
_SEED_QUERIES = (
    "created:[2020-06-15 TO 2020-06-15]",
    "created:[2020-12-31 TO 2021-01-01]",
    # A far-future bare year: found by a deep fuzz soak matching a wrong
    # document through tantivy's i64-nanosecond date wrap before the
    # emitter's window clamp existed; seeded so the fix is exercised
    # deterministically, not only by random draw.
    "created:3772",
    # A NOT operand under ANDNOT: whoosh's root-only Not composes
    # incoherently there (DIVERGENCES.md entry 47, found by a deep fuzz
    # soak at the result level; the parsed ASTs are identical).
    "(NOT (created:'this year')) ANDNOT (NOT (0))",
    "attrs.user:alice",
    "release_date:'2020-06-10 00:00'",
    "tag:billing",
    "content:invoice",
    "asn:[100 TO 103]",
    "has_correspondent:'  false'",
    "NOT ((())^0.5)",
    "NOT ()",
    # A nested NOT rather than a bare paren collapsing to empty
    # (DIVERGENCES.md entry 40's extension): found by a hypothesis fuzzer
    # soak, sitting only in the local example database until seeded here.
    "NOT ((NOT ()))",
    "(title:the) ANDNOT (content:invoice)",
    "asn:[5 TO 5]",
    "asn:{100 TO 127]",
    "((title:the)^0.5) ANDNOT ((NOT (0)))",
    "00-YEAR",
    "NOT (*:* title:the)",
    "attrs:END",
)


def _acceptance_property(q: str, windex_prop: WhooshIndex, tindex_prop: TIndex) -> None:
    result = wc_parse(
        q, registry=_PROP_REGISTRY, default_fields=V2_FIELDS, basedate=BASE, tz=BERLIN
    )
    if result.diagnostics:
        # DIVERGENCES.md entry 6: an unparseable date/number leaves nothing
        # coherent to search with on whoosh-compat's side.
        return
    try:
        oracle_parse(q, BASE, BERLIN)
    except Exception:  # noqa: BLE001 - see test_hypothesis.py's identical rationale
        # The oracle makes no "never raises" promise the way whoosh-compat's
        # diagnostics-not-exceptions contract does; there is no oracle
        # result to compare against for a shape it cannot parse at all.
        return

    try:
        got = tantivy_search_ids_prop(tindex_prop, q)
    except QueryError:
        # ARCHITECTURE.md's "kind dispatch is a closed matrix": a parseable
        # AST can still be a documented emit-time error (e.g. a text-field
        # TermRange), the third of the three legitimate outcomes alongside a
        # parse diagnostic and an executed search; neither is a parity bug
        # to report here.
        return

    try:
        expected = whoosh_search_ids_prop(windex_prop, q)
    except Exception:  # noqa: BLE001
        # Real whoosh can also fail *after* a query object was built
        # successfully, while actually matching/searching (e.g. a Phrase
        # query against a KEYWORD field, which stores no term positions;
        # or an all-stopword Phrase's empty word list reaching whoosh's own
        # span-matcher construction, DIVERGENCES.md entry 24's mechanism
        # surfacing as a genuine oracle crash rather than a comparable
        # result here). Same rationale as the oracle_parse try/except
        # above: there is no oracle result to compare against.
        return

    if allowed_result_reason(q) is None:
        assert got == expected, f"query: {q!r}"
    # else: an allowlisted, known-divergent query shape. Unlike the
    # AST-comparison layer's structural strict-xfail (tests/differential's
    # "the trees still differ" assertion, which depends only on shape), this
    # layer's divergences are frequently *value*-dependent (e.g. entry 14's
    # demoted-unfielded-search mismatch only shows up when the generated
    # term text would actually hit something; a generated zero-token/
    # stopword value can make both sides coincidentally return the same
    # empty set, as "attrs.user:0" does). A generated example therefore
    # cannot reliably prove the divergence is still live the way a fixed
    # string can, so this property skips an allowlisted shape rather than
    # asserting strict inequality against effectively random data. The
    # strict-xfail proof that each entry's divergence is real still exists,
    # just at the hand-picked-scenario layer above (test_scenario_equal's
    # siblings: test_created_range_near_local_midnight_is_a_result_level_divergence,
    # test_attrs_and_release_date_are_result_level_divergences), the same
    # role SCENARIOS_EQUAL/those tests play for test_acceptance_e2e.py's
    # own documented divergences.


@given(q=query_text(max_leaves=5))
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
def test_generated_query_matches_oracle_results(
    q: str, windex_prop: WhooshIndex, tindex_prop: TIndex
) -> None:
    _acceptance_property(q, windex_prop, tindex_prop)


for _q in _SEED_QUERIES:
    test_generated_query_matches_oracle_results = example(q=_q)(
        test_generated_query_matches_oracle_results
    )
del _q
