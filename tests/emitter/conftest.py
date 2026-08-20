from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import tzinfo
from typing import NamedTuple

import pytest
import tantivy
from whoosh.filedb.filestore import RamStorage
from whoosh.index import Index as WhooshIndex
from whoosh.lang.snowball.english import EnglishStemmer

from tests.differential.oracle import oracle_parse
from tests.differential.oracle import oracle_schema
from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

TIndex = tuple[tantivy.Index, tantivy.Schema]

_english_stemmer = EnglishStemmer()


class Doc(NamedTuple):
    id: int
    title: str
    content: str
    tags: list[str]
    asn: int
    created_iso: str
    added_iso: str
    notes: dict[str, str] | None


DOCS: list[Doc] = [
    Doc(
        1,
        "Billing 2020",
        "invoice total amount",
        ["billing", "urgent"],
        100,
        "2020-03-15",
        "2020-03-15T10:00:00Z",
        {"note": "check this", "user": "alice"},
    ),
    Doc(
        2,
        "Billing 2019",
        "receipt shopname product1",
        ["billing"],
        101,
        "2019-06-01",
        "2019-06-01T09:00:00Z",
        None,
    ),
    Doc(
        3,
        "Wärrantyplan",
        "plan warranty basement",
        [],
        102,
        "2018-03-23",
        "2018-03-23T08:00:00Z",
        None,
    ),
    Doc(
        4,
        "Report 2020",
        "shopname product1 product2",
        ["report"],
        103,
        "2020-11-30",
        "2020-11-30T12:00:00Z",
        {"note": "final", "user": "bob"},
    ),
    # Doc 5 exists solely to exercise JSON-subpath emission (test_emit_json.py):
    # a "user" value containing both a double-quote and a backslash, to prove
    # the parse_query fallback's escaping round-trips. Its other fields are
    # deliberately chosen to fall outside every bound/pattern asserted by the
    # rest of the emitter suite (see conftest module docstring note below):
    # except where a doc without tags/an "every doc" query genuinely must
    # include it; those expectations were updated accordingly.
    Doc(
        5,
        "Miscellaneous Doc",
        "assorted filler content only",
        [],
        99,
        "2019-03-01",
        "2019-03-01T00:00:00Z",
        {"user": 'a"b\\c'},
    ),
]


def lower_fold(text: str) -> list[str]:
    """Approximate tantivy's 'default' tokenizer: split on non-word chars, lowercase.

    Applied as the ``analyzer`` for TEXT/KEYWORD fields in the emitter test
    registry so tokenization here matches what tantivy's own 'default'
    tokenizer chain (simple tokenizer + lowercase filter) does at index time.

    Note: tantivy's 'default' chain does *not* ASCII-fold: "Wärrantyplan"
    is indexed as the single token "wärrantyplan", umlaut intact. This
    analyzer must not fold either, or emitted term text would silently stop
    matching what is in the index (verified against a live tantivy index).
    """
    lowered = unicodedata.normalize("NFC", text.lower())
    return [t for t in re.split(r"\W+", lowered, flags=re.UNICODE) if t]


@pytest.fixture(scope="session")
def tindex() -> TIndex:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("title", stored=True)  # 'default' tokenizer: simple+lowercase
    sb.add_text_field("content", stored=True)
    sb.add_text_field("tag", stored=True)
    sb.add_unsigned_field("asn", stored=True, indexed=True, fast=True)
    sb.add_unsigned_field("tag_id", indexed=True, fast=True)
    sb.add_date_field("created", stored=True, indexed=True, fast=True)
    sb.add_date_field("added", stored=True, indexed=True, fast=True)
    sb.add_json_field("notes", stored=True)
    sb.add_json_field("attrs", stored=True, fast=True)  # type: ignore[call-arg]  # stub gap
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, title, content, tags, asn, created, added, notes in DOCS:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("title", title)
        doc.add_text("content", content)
        for t in tags:
            doc.add_text("tag", t)
            doc.add_unsigned("tag_id", 1)  # presence marker; value unused, only existence matters
        doc.add_unsigned("asn", asn)
        doc.add_date("created", datetime.fromisoformat(created))
        doc.add_date("added", datetime.fromisoformat(added))
        if notes:
            doc.add_json("notes", notes)
            doc.add_json("attrs", notes)  # fast counterpart of "notes", same values
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def ereg() -> FieldRegistry:
    """Field registry mirroring tests/conftest.py's ``reg``, tuned for tantivy.

    Analyzer/pattern_normalizer match tantivy's 'default' tokenizer so
    emitted term text lines up with what's actually indexed.
    """
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec(
                "tag",
                FieldKind.KEYWORD,
                analyzer=lower_fold,
                pattern_normalizer=lambda s: s.lower(),
                comma_values=True,
            ),
            FieldSpec("tag_id", FieldKind.U64, comma_values=True, fast=True),
            FieldSpec("asn", FieldKind.U64, fast=True),
            FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
            FieldSpec("added", FieldKind.DATETIME, fast=True),
            FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
            FieldSpec("has_tag_kw", FieldKind.BOOLEAN_EXISTS, exists_target="tag"),
            FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user")),
            FieldSpec("attrs", FieldKind.JSON, subpaths=("note", "user"), fast=True),
            FieldSpec("has_attrs", FieldKind.BOOLEAN_EXISTS, exists_target="attrs"),
        ]
    )


@pytest.fixture(scope="session")
def nonfast_index() -> TIndex:
    """Tantivy schema/index whose numeric, date and JSON fields are all
    non-fast, the counterpart to ``tindex``'s all-fast ``asn``/``created``/
    ``added``/``attrs``. Exists to exercise ``EXISTS_REQUIRES_FAST`` for
    field kinds other than the JSON one ``tindex``/``ereg`` already cover
    via the non-fast ``notes`` field.
    """
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True)
    sb.add_json_field("notes", stored=True)
    sb.add_unsigned_field("slow_num", stored=True, indexed=True)
    sb.add_date_field("slow_date", stored=True, indexed=True)
    schema = sb.build()
    index = tantivy.Index(schema)
    w = index.writer()
    for id_, _title, content, _tags, _asn, _created, _added, notes in DOCS:
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("content", content)
        if notes:
            doc.add_json("notes", notes)
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def nonfast_reg() -> FieldRegistry:
    """Registry counterpart to ``nonfast_index``: ``slow_num``/``slow_date``
    are U64/DATE fields with no fast column, alongside the already
    non-fast ``notes`` JSON field mirroring ``ereg``'s.
    """
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user")),
            FieldSpec("slow_num", FieldKind.U64),
            FieldSpec("slow_date", FieldKind.DATE, date_only=True),
        ]
    )


@pytest.fixture
def parse(ereg: FieldRegistry) -> Callable[[str], ast.Node]:
    """``parse()`` bound to the emitter registry, returning just the AST."""

    def _p(query_string: str) -> ast.Node:
        return _parse(query_string, registry=ereg, default_fields=["content"]).ast

    return _p


def emit_ast(node: ast.Node, tindex: TIndex, ereg: FieldRegistry) -> tantivy.Query:
    return emit_(node, index=tindex[0], registry=ereg)


def search_ids(index: tantivy.Index, q: tantivy.Query, limit: int = 10) -> list[int]:
    s = index.searcher()
    return sorted(
        hit_doc["id"][0]
        for _, addr in s.search(q, limit).hits
        for hit_doc in [s.doc(addr).to_dict()]
    )


@pytest.fixture(scope="session")
def windex() -> WhooshIndex:
    """In-RAM real-whoosh index (v2 paperless schema, ``oracle.oracle_schema``)
    holding the same ``DOCS`` fixture rows as ``tindex``: the oracle
    counterpart used by the e2e acceptance suite (``test_acceptance_e2e.py``)
    to prove whoosh-compat's parse -> emit -> tantivy-search pipeline agrees
    with real whoosh's parse -> search pipeline on full query strings (not
    just parsed ASTs, which is what ``tests/differential`` already covers).

    Field value shaping mirrors what paperless-ngx v2's own indexing code did:
    ``tag`` is a comma-joined string (``KEYWORD(commas=True)`` splits it back
    apart at analysis time), ``has_tag`` is a plain boolean presence flag,
    ``created``/``added`` are naive datetimes (v2 stored everything as
    UTC-naive). ``notes`` (a JSON object on the tantivy/v3 side) has no
    v2 equivalent (v2's ``notes`` field was plain ``TEXT()``); it is rendered
    here as a whitespace-joined dump of the dict's values purely so the field
    is populated with *something* searchable, not to reproduce any particular
    real-whoosh behavior (see DIVERGENCES.md's JSON-subpath entry,
    ``notes.user:`` style queries are a whoosh-compat-only concept with no
    real-whoosh analogue at all).
    """

    schema = oracle_schema()
    storage = RamStorage()
    ix = storage.create_index(schema)
    writer = ix.writer()
    for id_, title, content, tags, asn, created, added, notes in DOCS:
        fields = {
            "id": id_,
            "title": title,
            "content": content,
            "tag": ",".join(tags),
            "asn": asn,
            "has_tag": bool(tags),
            "created": datetime.fromisoformat(created),
            "added": datetime.fromisoformat(added).astimezone(UTC).replace(tzinfo=None),
        }
        if notes:
            fields["notes"] = " ".join(str(v) for v in notes.values())
        writer.add_document(**fields)
    writer.commit()
    return ix


def whoosh_search_ids(windex: WhooshIndex, q_str: str, basedate: datetime, tz: tzinfo) -> list[int]:
    """Parse ``q_str`` through the real whoosh v2 oracle parser
    (``oracle.oracle_parse``) and return the sorted ``id`` list it matches
    against ``windex``.
    """

    query = oracle_parse(q_str, basedate, tz)
    with windex.searcher() as searcher:
        return sorted(hit["id"] for hit in searcher.search(query, limit=None))


# -- Stemmed index/registry pair ---------------------------------------------
#
# ``tindex`` above builds its text fields with tantivy's 'default' tokenizer
# (simple + lowercase, NO stemming), which is exactly right for the rest of
# the emitter suite but cannot show what a stemmed index does to a pattern:
# against an unstemmed index "company*" and "copy*" both already work. This
# second purpose-built pair (same reason ``nonfast_index`` exists) indexes
# through ``tantivy.Filter.stemmer("English")`` and pairs with a registry
# whose ``pattern_normalizer`` stems too, mirroring how ``ereg`` pairs with
# ``tindex``.

STEM_COMPANY = 1
STEM_COMPANIES = 2
STEM_COPYRIGHT = 3
STEM_COPIES = 4

#: id -> indexed content for ``stemmed_index``. Chosen because English
#: Snowball's y->i step *substitutes* rather than truncates: "company" and
#: "companies" both stem to "compani" (so a pattern typed as the user spells
#: it misses them), while "copyright" keeps its literal spelling (so a
#: pattern that stems misses it). No single normalized string reaches both.
STEM_DOCS: dict[int, str] = {
    STEM_COMPANY: "company",
    STEM_COMPANIES: "companies",
    STEM_COPYRIGHT: "copyright",
    STEM_COPIES: "copies",
}


def english_stem(text: str) -> str:
    """Real-whoosh's English Snowball stemmer, one word in, one word out.

    Whoosh's implementation is used rather than a hand table so the fixture
    cannot drift from the stemmer the differential oracle itself would apply;
    ``test_pattern_alternates.py`` pins it against the terms tantivy's own
    Rust Snowball actually put in the index.
    """
    return _english_stemmer.stem(text)


def stem_fold(text: str) -> list[str]:
    """Analyzer counterpart of ``stemmed_index``'s tokenizer chain."""
    return [english_stem(token) for token in lower_fold(text)]


def stem_alternates(text: str) -> tuple[str, ...]:
    """``pattern_normalizer`` for ``stemmed_reg``: the folded run and its stem.

    Both forms, not a choice between them, which is the whole point of the
    alternatives contract (``whoosh_compat.PatternNormalizer``). Returned
    with the duplicate still in it when the stemmer is a no-op: collapsing
    that is the emitter's job, and this fixture would hide the bug if it did
    it here.
    """
    folded = text.lower()
    return (folded, english_stem(folded))


@pytest.fixture(scope="session")
def stemmed_index() -> TIndex:
    sb = tantivy.SchemaBuilder()
    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    sb.add_text_field("content", stored=True, tokenizer_name="en_stem_test")
    schema = sb.build()
    index = tantivy.Index(schema)
    index.register_tokenizer(
        "en_stem_test",
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        .filter(tantivy.Filter.lowercase())
        .filter(tantivy.Filter.stemmer("English"))
        .build(),
    )
    w = index.writer()
    for id_, content in STEM_DOCS.items():
        doc = tantivy.Document()
        doc.add_unsigned("id", id_)
        doc.add_text("content", content)
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def stemmed_reg() -> FieldRegistry:
    """Registry counterpart to ``stemmed_index``."""
    return FieldRegistry(
        [
            FieldSpec(
                "content",
                FieldKind.TEXT,
                analyzer=stem_fold,
                pattern_normalizer=stem_alternates,
            ),
        ]
    )
