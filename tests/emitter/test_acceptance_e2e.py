"""End-to-end acceptance suite: full query string -> whoosh oracle search
results vs full query string -> whoosh-compat parse -> emit -> tantivy search
results.

This is deliberately a *different* kind of test than ``tests/differential``:
the differential suite compares *parsed ASTs* between whoosh-compat and real
whoosh, never touching an actual index. This module
instead runs each query string through *both full pipelines end to end*:
real whoosh against an in-RAM ``windex`` (:func:`whoosh_search_ids`,
``tests/emitter/conftest.py``) and whoosh-compat's parser + tantivy emitter
against the shared ``tindex`` fixture (:func:`search_ids`): and compares
the resulting doc-id sets.

A useful, repeatedly-confirmed finding from writing this suite: several
divergences that are real and visible *at the AST level* (comparing parsed
trees, as the differential suite does) turn out NOT to change the final
*search results* for any of this fixture's queries, because both backends'
independent normalization paths still converge on the same effective
query by the time something actually executes against an index. Wildcard
case-folding (DIVERGENCES.md entry 2) is the clearest example: real whoosh's own
``field.process_text(..., tokenize=False)`` call still runs the field's
LowercaseFilter over an un-tokenized wildcard/prefix pattern (filters, unlike
tokenizers, are not skipped by ``tokenize=False``: see
``whoosh.analysis.tokenizers.RegexTokenizer.__call__``), so a fielded
``Wär*`` query against real whoosh *also* ends up matching against the
lowercased pattern ``wär``, exactly like whoosh-compat's explicit
``pattern_normalizer`` does: both sides match doc 3, not just the tantivy
side, which is easy to get wrong by assuming whoosh's own wildcard matching
is raw/case-sensitive. This module's expectations are the ones actually
observed running against live whoosh/tantivy indexes; see the per-scenario
comments below for the specific evidence.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import tantivy
from whoosh.analysis import CharsetFilter
from whoosh.analysis import StandardAnalyzer
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import NUMERIC
from whoosh.fields import TEXT
from whoosh.fields import Schema
from whoosh.filedb.filestore import RamStorage
from whoosh.index import Index as WhooshIndex
from whoosh.lang.porter import stem as porter_stem
from whoosh.qparser import QueryParser
from whoosh.support.charset import accent_map

from tests.differential.oracle import oracle_schema
from whoosh_compat import parse as wc_parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import lower_fold
from .conftest import search_ids
from .conftest import whoosh_search_ids

BERLIN = ZoneInfo("Europe/Berlin")
# "created:previous month"/"created:[now-7d TO now]"/etc need a fixed,
# injected "now" to be deterministic; 2020-04-15 also makes "previous month"
# land exactly on doc 1's March 2020 "created" date (see SCENARIOS_EQUAL).
BASE = datetime(2020, 4, 15, 10, 30, tzinfo=BERLIN)


# The multifield default fields an unfielded term searches: the tantivy
# fixture's counterpart of the oracle's V2_FIELDS (content/title/correspondent/
# tag/type/notes/custom_fields), narrowed to the TEXT/KEYWORD fields `ereg`
# actually registers. Using a single-field default (as the shared `parse`
# fixture in conftest.py does, fine for that file's single-field checks)
# would make unfielded multi-field scenarios like "Wär*" (which only
# doc 3's *title* (not content) contains, see DOCS) incomparable to the
# oracle's genuinely-multifield V2_FIELDS search.
DEFAULT_FIELDS = ["content", "title", "tag"]


def tantivy_search_ids(
    tindex: TIndex,
    ereg: FieldRegistry,
    q: str,
    basedate: datetime = BASE,
    tz: ZoneInfo = BERLIN,
) -> list[int]:
    """The tantivy-side counterpart of ``whoosh_search_ids``: parse ``q``
    with whoosh-compat, emit a ``tantivy.Query``, and search ``tindex``.
    """

    result = wc_parse(q, registry=ereg, default_fields=DEFAULT_FIELDS, basedate=basedate, tz=tz)
    query = emit_(result.ast, index=tindex[0], registry=ereg)
    return search_ids(tindex[0], query)


# ---------------------------------------------------------------------------
# Fixture self-test: the *raw text* fed to whoosh's own StandardAnalyzer and
# to the emitter registry's TEXT-field analyzer (`lower_fold`,
# tests/emitter/conftest.py) must tokenize identically: otherwise an
# equality assertion between the two pipelines below could pass or fail for
# the wrong reason (a tokenization mismatch masquerading as a search-result
# mismatch, or vice versa). `lower_fold` doesn't drop stopwords/short tokens
# the way StandardAnalyzer's StopFilter/minsize do, so this only holds for
# ordinary content words: exactly what the SCENARIOS below use.
# ---------------------------------------------------------------------------

_SCENARIO_WORDS = [
    "shopname",
    "product1",
    "product2",
    "billing",
    "urgent",
    "Billing",
    "Wärrantyplan",
    "invoice",
    "total",
    "amount",
    "receipt",
    "plan",
    "warranty",
    "basement",
    "report",
    "alice",
    "bob",
    "foo",
    "2020",
]


def test_00_analyzer_parity_precondition(ereg: FieldRegistry) -> None:
    whoosh_analyzer = StandardAnalyzer()
    resolved = ereg.resolve(FieldRef("content"))
    assert resolved is not None
    spec = resolved.spec
    assert spec.analyzer is not None
    our_analyzer = spec.analyzer
    for word in _SCENARIO_WORDS:
        whoosh_tokens = [t.text for t in whoosh_analyzer(word)]
        our_tokens = our_analyzer(word)
        assert whoosh_tokens == our_tokens, word


# ---------------------------------------------------------------------------
# Step 1/2: full-pipeline scenarios that must produce EQUAL doc-id sets.
#
# All ids below were derived by actually running both pipelines against the
# `windex`/`tindex` fixtures (DOCS, tests/emitter/conftest.py): not by
# eyeballing the fixture. Any case where the actual result surprised a naive
# reading of the fixture is called out explicitly in a comment.
# ---------------------------------------------------------------------------

SCENARIOS_EQUAL = [
    pytest.param(
        "tag:billing AND (title:2020 OR (NOT title:2019 AND NOT title:2018 AND created:[2020 TO 2020]))",
        [1],
        id="tag-and-title-or-date-range",
    ),
    pytest.param("shopname product1", [2, 4], id="implicit-and-two-terms"),
    pytest.param(
        "Wär*",
        [3],
        id="capitalized-wildcard-diacritic",
        # NOT a whoosh-vs-whoosh-compat divergence at the search-result
        # level (see module docstring): real whoosh's own field analyzer
        # lowercases the un-tokenized wildcard pattern text too, so both
        # sides match doc 3. DIVERGENCES.md entry 2 is real, but purely at the
        # *parsed-AST* level (whoosh-compat's pattern_normalizer runs at
        # parse time; whoosh's lowercasing happens implicitly, later,
        # inside query-object construction): see test_differential.py's
        # allowlist entry for that comparison.
    ),
    pytest.param(
        'created:"previous month"',
        [1],
        id="quoted-natural-date-keyword",
        # basedate is 2020-04-15 (BASE); "previous month" == March 2020,
        # which is doc 1's `created` date. Quoted so whoosh-compat's own
        # grammar (which requires quoting for multi-word date keywords,
        # unlike the app-level string-rewrite the real oracle harness
        # replicates: see allowlist.py's "design:" entry) parses it at
        # all; see test_created_previous_month_unquoted_is_a_documented_divergence
        # for the unquoted form.
    ),
    pytest.param("tag:billing,urgent", [1], id="keyword-comma-list-unquoted"),
    pytest.param(
        "tag:'billing,urgent'",
        [1],
        id="keyword-comma-list-quoted",
        # NOT a search-result divergence, despite allowlist.py documenting
        # a real *parse-time* one here (design: whoosh-compat's
        # CommaValuesPlugin keeps a quoted comma value as a single Term at
        # parse time; real whoosh's KEYWORD(commas=True) analyzer always
        # splits on commas). At *emit* time whoosh-compat still runs the
        # field's own analyzer (`lower_fold`, which also splits on commas)
        # over that single Term's text (see TantivyEmitter._text_term_query),
        # so the quote-vs-split distinction doesn't survive to search time
        # either: both sides end up querying tag:billing AND tag:urgent
        # and match doc 1.
    ),
    pytest.param(
        "has_tag:false",
        [3, 5],
        id="has-tag-false",
        # Doc 5 (added for the JSON-subpath emitter suite) also has no tags,
        # so it belongs in the has_tag:false result alongside doc 3.
    ),
    pytest.param("created:[2020 to]", [1, 4], id="lowercase-to-open-range"),
    pytest.param(
        "created:[jun to mar]",
        [1, 2],
        id="backward-month-range-joint-disambiguation",
        # basedate is 2020-04-15 (BASE): the bounds are read jointly
        # (June 2019 through March 2020, borrowing the previous year for
        # the backward-reading start), catching doc 2 (2019-06-01) and
        # doc 1 (2020-03-15) but not doc 5 (2019-03-01, before the
        # borrowed start). Read independently against the basedate
        # instead, both bounds would land in 2020, an inverted lo > hi
        # range matching nothing.
    ),
    pytest.param("added:[now-7d TO now]", [], id="basedate-relative-bracket-range"),
    pytest.param('added:"-1 week"', [], id="basedate-relative-quoted-keyword"),
    pytest.param("title:*", [1, 2, 3, 4, 5], id="bare-star-every-doc-with-title"),
    pytest.param("-foo shopname", [], id="attached-dash-foo-plus-shopname"),
    pytest.param("asn:[100 TO 102]", [1, 2, 3], id="numeric-range"),
]


@pytest.mark.parametrize(("q", "expected"), SCENARIOS_EQUAL)
def test_scenario_equal(
    windex: WhooshIndex, tindex: TIndex, ereg: FieldRegistry, q: str, expected: list[int]
) -> None:
    assert whoosh_search_ids(windex, q, BASE, BERLIN) == expected
    assert tantivy_search_ids(tindex, ereg, q) == expected


# ---------------------------------------------------------------------------
# Genuine documented divergences: assert each side explicitly rather than
# equality.
# ---------------------------------------------------------------------------


def test_created_previous_month_unquoted_is_a_documented_divergence(
    windex: WhooshIndex, tindex: TIndex, ereg: FieldRegistry
) -> None:
    """design (allowlist.py): an *unquoted* multi-word natural-date keyword
     needs paperless-ngx's app-level ``rewrite_natural_date_keywords`` string
     rewrite (which the oracle harness replicates, ``oracle._rewrite_natural_date_keywords``)
    : real whoosh's own grammar has no native "previous month" support at
     all. whoosh-compat's date grammar only recognizes the multi-word
     keywords when quoted (see test_scenario_equal's quoted counterpart);
     unquoted, "previous" is parsed as an ordinary (invalid) date attempt.
    """

    q = "created:previous month"
    assert whoosh_search_ids(windex, q, BASE, BERLIN) == [1]

    result = wc_parse(q, registry=ereg, default_fields=DEFAULT_FIELDS, basedate=BASE, tz=BERLIN)
    assert result.diagnostics, "expected a diagnostic for the unparseable 'previous' date token"
    with pytest.raises(QueryEmitError):
        emit_(result.ast, index=tindex[0], registry=ereg)


def test_notes_user_json_subpath_has_no_v2_analogue(
    windex: WhooshIndex, tindex: TIndex, ereg: FieldRegistry
) -> None:
    """design (DIVERGENCES JSON-subpath entry): ``notes.user:`` is a
    whoosh-compat-only JSON dotted-path concept
    (:meth:`FieldRegistry.resolve_json`). Real whoosh has no JSON field type
    at all: the paperless-ngx v2 schema this suite's oracle clones had its
    own ``notes`` field as plain ``TEXT()``: so there is no query real
    whoosh users could type that reaches "the note written by a specific
    user" the way whoosh-compat's JSON subpath does. ``windex``'s ``notes``
    field (see conftest.py) is filled with a whitespace-joined dump of the
    note dict's values purely to give the field *something* to search, not
    to reproduce a real-whoosh behavior that never existed; unsurprisingly,
    that naive representation doesn't
    happen to satisfy whoosh's own multifield-AND expansion of
    "notes.user:alice" (which requires the literal token "notes" *and* the
    tokens "user"+"alice" together in one field), so it matches nothing.
    """

    q = "notes.user:alice"
    assert whoosh_search_ids(windex, q, BASE, BERLIN) == []
    assert tantivy_search_ids(tindex, ereg, q) == [1]


# ---------------------------------------------------------------------------
# Requirement C: paperless-ngx issue #13568 acceptance scenario.
#
# The exact real-world query from the issue, run against a small dedicated
# corpus (not the shared DOCS/tindex fixtures, to avoid disturbing every
# other emitter test's hardcoded "all docs" expectations). Char classes are
# genuine v2 fnmatch behavior (whoosh's own
# Wildcard query type has always compiled its pattern with
# ``fnmatch.translate``), and the query's only star is a *leading* one
# (title:*2024), so whoosh's Wildcard.normalize() trailing-star-fold bug
# (DIVERGENCES.md entry 13) never triggers: parity should hold, and does.
# ---------------------------------------------------------------------------

# Quoted verbatim from the report, including its tag names ("steuer",
# "valentin"): this is a citation of the real-world issue, not a fixture
# invention, so it is intentionally not genericized like the rest of this
# suite's German test vocabulary.
ISSUE_13568_QUERY = (
    "tag:steuer AND tag:valentin AND "
    "(title:*2024 OR (NOT title:*202[0-3] AND NOT title:*201[0-9] AND created:2024))"
)

# (id, title, tags, created): designed so every clause of the query is
# exercised by at least one doc. The tags match the citation above verbatim
# (required for the query to mean anything); the titles are this suite's
# own invented fixture data and carry no such constraint, so they're
# ordinary generic English:
#   1: tag match + title:*2024              -> included (first OR branch)
#   2: tag match, title *ends* 202[0-3]      -> excluded (char-class NOT fails)
#   3: tag match, no matching title suffix,
#      created:2024                         -> included (second OR branch)
#   4: title:*2024 but missing "valentin"    -> excluded (tag AND fails)
#   5: tag match, title *ends* 201[0-9]      -> excluded (char-class NOT fails)
DOCS_13568 = [
    (1, "Report 2024", ["steuer", "valentin"], "2024-03-01"),
    (2, "Report 2022", ["steuer", "valentin"], "2022-03-01"),
    (3, "Report Reference", ["steuer", "valentin"], "2024-05-01"),
    (4, "Report 2024", ["steuer"], "2024-01-01"),
    (5, "Report 2015", ["steuer", "valentin"], "2015-01-01"),
]


@pytest.fixture(scope="module")
def windex_13568() -> WhooshIndex:
    schema = oracle_schema()
    ix = RamStorage().create_index(schema)
    w = ix.writer()
    for id_, title, tags, created in DOCS_13568:
        w.add_document(
            id=id_,
            title=title,
            content=title,
            tag=",".join(tags),
            has_tag=bool(tags),
            created=datetime.fromisoformat(created),
        )
    w.commit()
    return ix


@pytest.fixture(scope="module")
def tindex_13568() -> TIndex:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("title", stored=True)
    sb.add_text_field("tag", stored=True)
    sb.add_unsigned_field("tag_id", indexed=True, fast=True)
    sb.add_date_field("created", stored=True, indexed=True, fast=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, title, tags, created in DOCS_13568:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("title", title)
        for t in tags:
            doc.add_text("tag", t)
            doc.add_unsigned("tag_id", 1)
        doc.add_date("created", datetime.fromisoformat(created))
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def ereg_13568() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec(
                "tag",
                FieldKind.KEYWORD,
                analyzer=lower_fold,
                pattern_normalizer=str.lower,
                comma_values=True,
            ),
            FieldSpec("tag_id", FieldKind.U64, comma_values=True, fast=True),
            FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
        ]
    )


def test_issue_13568_acceptance(
    windex_13568: WhooshIndex, tindex_13568: TIndex, ereg_13568: FieldRegistry
) -> None:
    expected = [1, 3]
    assert whoosh_search_ids(windex_13568, ISSUE_13568_QUERY, BASE, BERLIN) == expected

    result = wc_parse(
        ISSUE_13568_QUERY,
        registry=ereg_13568,
        default_fields=["title"],
        basedate=BASE,
        tz=BERLIN,
    )
    assert not result.diagnostics
    query = emit_(result.ast, index=tindex_13568[0], registry=ereg_13568)
    assert search_ids(tindex_13568[0], query) == expected


# ---------------------------------------------------------------------------
# Requirement D: folding+stemming analyzer contract proof.
#
# The analyzer/pattern_normalizer split (fields.py's FieldSpec) exists
# specifically so a *full* token-level analysis chain (lowercase + fold +
# stem, used for Term/Phrase queries) never leaks into wildcard/prefix
# pattern matching, which only ever gets character-level transforms
# (lowercase + fold, never stemming). This uses real whoosh's
# `StemmingAnalyzer() | CharsetFilter(accent_map)` on the oracle side and an
# equivalent pair of plain Python functions (built on the *same* underlying
# porter stemmer/accent map real whoosh uses, so both sides stem/fold
# identically) on the whoosh-compat/tantivy side. Both indexes are local to
# this module (not the shared DOCS/tindex fixtures).
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _stem_fold_analyzer(text: str) -> list[str]:
    """analyzer: full token pipeline (lowercase -> stem -> fold), used for
    Term/Phrase queries. Mirrors real whoosh's actual filter order for
    `StemmingAnalyzer() | CharsetFilter(accent_map)` (stemming runs on the
    lowercased surface form; accent-folding runs last): verified directly
    against a live whoosh index.
    """
    return [porter_stem(w.lower()).translate(accent_map) for w in _WORD_RE.findall(text)]


def _fold_lower(text: str) -> str:
    """pattern_normalizer: character-level transforms ONLY (lowercase +
    accent-fold): NEVER stemming, per the analyzer/pattern_normalizer
    contract (fields.py's FieldSpec docstring).
    """
    return text.lower().translate(accent_map)


# (id, content): "jump"/"jumps"/"jumping" all stem to the same root
# ("jump") under the real Porter algorithm; "runs" and "running" do NOT
# (they stem to "run" and "runn" respectively: Porter is a suffix-rule
# stemmer, not a lemmatizer, so it doesn't unify irregular/gerund forms the
# way a dictionary-based stemmer might). That asymmetry is exactly what
# demonstrates the stemmed-wildcard caveat below.
DOCS_STEM = [
    (1, "The system is jumping between states"),
    (2, "It jumps rapidly"),
    (3, "The dog runs and running fast"),
    (4, "Wärranty of the engine is completed"),
]


WhooshStemIndex = tuple[WhooshIndex, Schema]


@pytest.fixture(scope="module")
def windex_stem() -> WhooshStemIndex:
    analyzer = StemmingAnalyzer() | CharsetFilter(accent_map)
    schema = Schema(id=NUMERIC(stored=True, unique=True), content=TEXT(analyzer=analyzer))
    ix = RamStorage().create_index(schema)
    w = ix.writer()
    for id_, text in DOCS_STEM:
        w.add_document(id=id_, content=text)
    w.commit()
    return ix, schema


def _windex_stem_ids(windex_stem: WhooshStemIndex, q_str: str) -> list[int]:
    ix, schema = windex_stem
    q = QueryParser("content", schema).parse(q_str)
    with ix.searcher() as s:
        return sorted(hit["id"] for hit in s.search(q, limit=None))


@pytest.fixture(scope="module")
def tindex_stem() -> TIndex:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, text in DOCS_STEM:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        # Pre-stem/fold at index time with the exact same function used as
        # this registry's `analyzer` below, then let tantivy's plain
        # 'default' tokenizer (lowercase + split on non-word chars) index
        # the already-stemmed words: a no-op on already-lowercase,
        # already-split tokens, so no separate tokenizer registration is
        # needed to keep the two sides consistent.
        doc.add_text("content", " ".join(_stem_fold_analyzer(text)))
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def ereg_stem() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec(
                "content",
                FieldKind.TEXT,
                analyzer=_stem_fold_analyzer,
                pattern_normalizer=_fold_lower,
            ),
        ]
    )


def _tindex_stem_ids(tindex_stem: TIndex, ereg_stem: FieldRegistry, q_str: str) -> list[int]:
    result = wc_parse(q_str, registry=ereg_stem, default_fields=["content"])
    query = emit_(result.ast, index=tindex_stem[0], registry=ereg_stem)
    return search_ids(tindex_stem[0], query)


def test_stemming_term_query_matches_other_inflected_forms(
    windex_stem: WhooshStemIndex, tindex_stem: TIndex, ereg_stem: FieldRegistry
) -> None:
    # "jumping" stems to "jump", matching docs 1 ("jumping") and 2 ("jumps"),
    # both of which also stem to "jump": proof point 1: stemmed term
    # queries match other morphological forms of the same word.
    q = "content:jumping"
    expected = [1, 2]
    assert _windex_stem_ids(windex_stem, q) == expected
    assert _tindex_stem_ids(tindex_stem, ereg_stem, q) == expected


def test_folded_wildcard_matches_diacritic_variant(
    windex_stem: WhooshStemIndex, tindex_stem: TIndex, ereg_stem: FieldRegistry
) -> None:
    # pattern_normalizer folds "Wär" -> "war" (character-level only, no
    # stemming attempted on a wildcard fragment): proof point 2: folded
    # wildcard matches.
    q = "content:Wär*"
    expected = [4]
    assert _windex_stem_ids(windex_stem, q) == expected
    assert _tindex_stem_ids(tindex_stem, ereg_stem, q) == expected


def test_stemmed_wildcard_caveat_diverges(
    windex_stem: WhooshStemIndex, tindex_stem: TIndex, ereg_stem: FieldRegistry
) -> None:
    """Proof point 3: a wildcard over a stemmed index matches INDEX TOKENS,
    not surface forms: querying "running*" can miss a doc whose token was
    stemmed down to something that isn't a prefix match for the literal
    query text. Doc 3 ("running") is indexed as the stem "runn"
    (`_stem_fold_analyzer`).

    whoosh-compat's `pattern_normalizer` never stems (by design, see the
    FieldSpec docstring), so the emitted prefix stays the literal "running",
    which is *not* a prefix of the indexed token "runn", and the query
    matches nothing.

    Real whoosh, by contrast, still runs a wildcard/prefix pattern through
    `field.process_text(text, tokenize=False)`, and a StemFilter is a filter
    like LowercaseFilter, not skipped by `tokenize=False`, so whoosh's
    own tagger *does* stem the pattern text too, "running" -> "runn", which
    then happens to line up exactly with the stored stem and matches. This
    is real whoosh incidentally getting the "right" answer via a mechanism
    whoosh-compat deliberately does not replicate (stemming a wildcard
    fragment is unsound in general, the truncated pattern is not
    guaranteed to be a valid prefix of every morphologically-related stem;
    it happened to work here only because Porter's suffix stripping on
    "running" produces a string that is *also* a valid stem prefix).
    v2 whoosh had this same "stems the wildcard pattern" property wherever
    it stemmed at all: this is a pre-existing property being carried
    forward, not something whoosh-compat is choosing to newly diverge on.
    """

    q = "content:running*"
    assert _windex_stem_ids(windex_stem, q) == [3]
    assert _tindex_stem_ids(tindex_stem, ereg_stem, q) == []
