import re
import unicodedata
from datetime import UTC
from datetime import datetime

import pytest
import tantivy
from whoosh.filedb.filestore import RamStorage

from tests.differential.oracle import oracle_parse
from tests.differential.oracle import oracle_schema
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

DOCS = [  # (id, title, content, tags, asn, created_iso, added_iso, notes)
    (1, "Steuer 2020", "invoice total amount", ["steuer", "wichtig"], 100, "2020-03-15", "2020-03-15T10:00:00Z", {"note": "check this", "user": "alice"}),
    (2, "Steuer 2019", "receipt shopname product1", ["steuer"], 101, "2019-06-01", "2019-06-01T09:00:00Z", None),
    (3, "Entwässerungsplan", "plan entwasserung basement", [], 102, "2018-03-23", "2018-03-23T08:00:00Z", None),
    (4, "Report 2020", "shopname product1 product2", ["report"], 103, "2020-11-30", "2020-11-30T12:00:00Z", {"note": "final", "user": "bob"}),
    # Doc 5 exists solely to exercise JSON-subpath emission (test_emit_json.py):
    # a "user" value containing both a double-quote and a backslash, to prove
    # the parse_query fallback's escaping round-trips. Its other fields are
    # deliberately chosen to fall outside every bound/pattern asserted by the
    # rest of the emitter suite (see conftest module docstring note below) --
    # except where a doc without tags/an "every doc" query genuinely must
    # include it; those expectations were updated accordingly.
    (5, "Miscellaneous Doc", "assorted filler content only", [], 99, "2019-03-01", "2019-03-01T00:00:00Z", {"user": 'a"b\\c'}),
]


def lower_fold(text: str) -> list[str]:
    """Approximate tantivy's 'default' tokenizer: split on non-word chars, lowercase.

    Applied as the ``analyzer`` for TEXT/KEYWORD fields in the emitter test
    registry so tokenization here matches what tantivy's own 'default'
    tokenizer chain (simple tokenizer + lowercase filter) does at index time.

    Note: tantivy's 'default' chain does *not* ASCII-fold -- "Entwässerungsplan"
    is indexed as the single token "entwässerungsplan", umlaut intact. This
    analyzer must not fold either, or emitted term text would silently stop
    matching what is in the index (verified against a live tantivy index).
    """
    lowered = unicodedata.normalize("NFC", text.lower())
    return [t for t in re.split(r"\W+", lowered, flags=re.UNICODE) if t]


@pytest.fixture(scope="session")
def tindex():
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
        w.add_document(doc)
    w.commit()
    index.reload()
    return index, schema


@pytest.fixture
def ereg():
    """Field registry mirroring tests/conftest.py's ``reg``, tuned for tantivy.

    Analyzer/pattern_normalizer match tantivy's 'default' tokenizer so
    emitted term text lines up with what's actually indexed.
    """
    return FieldRegistry([
        FieldSpec("content", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
        FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
        FieldSpec("tag", FieldKind.KEYWORD, analyzer=lower_fold, pattern_normalizer=lambda s: s.lower(), comma_values=True),
        FieldSpec("tag_id", FieldKind.U64, comma_values=True, fast=True),
        FieldSpec("asn", FieldKind.U64, fast=True),
        FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
        FieldSpec("added", FieldKind.DATETIME, fast=True),
        FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
        FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user")),
    ])


@pytest.fixture
def parse(ereg):
    """``parse()`` bound to the emitter registry, returning just the AST."""

    def _p(query_string):
        return _parse(query_string, registry=ereg, default_fields=["content"]).ast

    return _p


def emit_ast(node, tindex, ereg):
    return emit_(node, index=tindex[0], schema=tindex[1], registry=ereg)


def search_ids(index, q, limit=10):
    s = index.searcher()
    return sorted(
        hit_doc["id"][0]
        for _, addr in s.search(q, limit).hits
        for hit_doc in [s.doc(addr).to_dict()]
    )


@pytest.fixture(scope="session")
def windex():
    """In-RAM real-whoosh index (v2 paperless schema, ``oracle.oracle_schema``)
    holding the same ``DOCS`` fixture rows as ``tindex`` -- the oracle
    counterpart used by the e2e acceptance suite (``test_acceptance_e2e.py``)
    to prove whoosh-compat's parse -> emit -> tantivy-search pipeline agrees
    with real whoosh's parse -> search pipeline on full query strings (not
    just parsed ASTs, which is what ``tests/differential`` already covers).

    Field value shaping mirrors what paperless-ngx v2's own indexing code did:
    ``tag`` is a comma-joined string (``KEYWORD(commas=True)`` splits it back
    apart at analysis time), ``has_tag`` is a plain boolean presence flag,
    ``created``/``added`` are naive datetimes (v2 stored everything as
    UTC-naive). ``notes`` -- a JSON object on the tantivy/v3 side  -- has no
    v2 equivalent (v2's ``notes`` field was plain ``TEXT()``); it is rendered
    here as a whitespace-joined dump of the dict's values purely so the field
    is populated with *something* searchable, not to reproduce any particular
    v2 behavior (see DIVERGENCES.md's JSON-subpath entry -- ``notes.user:``
    style queries are a v1-only concept with no v2 analogue at all).
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


def whoosh_search_ids(windex, q_str, basedate, tz):
    """Parse ``q_str`` through the real whoosh v2 oracle parser
    (``oracle.oracle_parse``) and return the sorted ``id`` list it matches
    against ``windex``.
    """

    query = oracle_parse(q_str, basedate, tz)
    with windex.searcher() as searcher:
        return sorted(hit["id"] for hit in searcher.search(query, limit=None))
