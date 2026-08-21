"""Oracle harness: parse queries through the real ``whoosh`` package (the
v2 paperless schema/parser) and translate the resulting whoosh query tree
into a :mod:`whoosh_compat.ast` tree so it can be compared against
whoosh-compat's own parser output.

The v2 schema/parser configuration below is cloned from paperless-ngx
``src/documents/index.py`` as of commit ``aed9abe48^`` (the last commit
before the v2->v3/tantivy migration): ``get_schema()`` -> :func:`oracle_schema`,
``DelayedFullTextQuery._get_query`` 's ``MultifieldParser`` + ``DateParserPlugin``
+ ``LocalDateParser`` configuration -> :func:`oracle_parse`.
"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import tzinfo as tzinfo_t
from typing import cast

import whoosh.query as wq
from whoosh.analysis import StandardAnalyzer
from whoosh.fields import BOOLEAN
from whoosh.fields import DATETIME
from whoosh.fields import KEYWORD
from whoosh.fields import NUMERIC
from whoosh.fields import TEXT
from whoosh.fields import Schema
from whoosh.qparser import MultifieldParser
from whoosh.qparser.dateparse import DateParserPlugin
from whoosh.qparser.dateparse import English
from whoosh.query import qcore as wq_core
from whoosh.util.times import timespan

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser.dateparse import DateParserPlugin as CompatDateParserPlugin
from whoosh_compat.parser.default import MultifieldParser as CompatMultifieldParser

# --------------------------------------------------------------------------
# v2 schema clone (paperless-ngx aed9abe48^:src/documents/index.py:get_schema)
# --------------------------------------------------------------------------


def oracle_schema() -> Schema:
    """A clone of paperless-ngx v2's ``get_schema()``."""

    return Schema(
        id=NUMERIC(stored=True, unique=True),
        title=TEXT(sortable=True),
        content=TEXT(),
        asn=NUMERIC(sortable=True, signed=False),
        correspondent=TEXT(sortable=True),
        correspondent_id=NUMERIC(),
        has_correspondent=BOOLEAN(),
        tag=KEYWORD(commas=True, scorable=True, lowercase=True),
        tag_id=KEYWORD(commas=True, scorable=True),
        has_tag=BOOLEAN(),
        type=TEXT(sortable=True),
        type_id=NUMERIC(),
        has_type=BOOLEAN(),
        created=DATETIME(sortable=True),
        modified=DATETIME(sortable=True),
        added=DATETIME(sortable=True),
        path=TEXT(sortable=True),
        path_id=NUMERIC(),
        has_path=BOOLEAN(),
        notes=TEXT(),
        num_notes=NUMERIC(sortable=True, signed=False),
        custom_fields=TEXT(),
        custom_field_count=NUMERIC(sortable=True, signed=False),
        has_custom_fields=BOOLEAN(),
        custom_fields_id=KEYWORD(commas=True),
        owner=TEXT(),
        owner_id=NUMERIC(),
        has_owner=BOOLEAN(),
        viewer_id=KEYWORD(commas=True),
        checksum=TEXT(),
        page_count=NUMERIC(sortable=True),
        original_filename=TEXT(sortable=True),
        is_shared=BOOLEAN(),
    )


# The default fields DelayedFullTextQuery._get_query() searches (v2).
V2_FIELDS: list[str] = [
    "content",
    "title",
    "correspondent",
    "tag",
    "type",
    "notes",
    "custom_fields",
]

_SCHEMA = oracle_schema()
_STANDARD_ANALYZER = StandardAnalyzer()


def _analyze(text: str) -> list[str]:
    """Tokenize ``text`` with whoosh's own StandardAnalyzer.

    Used as the oracle registry's TEXT-field analyzer, so whoosh-compat's
    :func:`~whoosh_compat.parser.default` term text, the real whoosh index's
    analyzed tokens, and :func:`whoosh_compat.ast.analyze`'s own forward
    analysis of whoosh-compat's parsed tree are all produced by the *same*
    code.
    """

    return [t.text for t in _STANDARD_ANALYZER(text)]


# KEYWORD fields do *not* use StandardAnalyzer: whoosh.fields.KEYWORD's own
# analyzer just splits on commas (when commas=True), strips whitespace, and
# optionally lowercases; it does not tokenize on word boundaries, so
# punctuation-only values like "-" survive as a single token (unlike a TEXT
# field, whose StandardAnalyzer drops them). Using the same analyzers the
# real KEYWORD field types would use keeps a KEYWORD field's multifield-OR
# branch from silently disappearing (or not) on both sides for the same
# reason.
_KEYWORD_ANALYZER_LOWER = KEYWORD(commas=True, lowercase=True).analyzer
_KEYWORD_ANALYZER = KEYWORD(commas=True).analyzer


def _analyze_keyword_lower(text: str) -> list[str]:
    return [t.text for t in _KEYWORD_ANALYZER_LOWER(text)]


def _analyze_keyword(text: str) -> list[str]:
    return [t.text for t in _KEYWORD_ANALYZER(text)]


def _make_oracle_registry() -> FieldRegistry:
    """A :class:`FieldRegistry` describing the same fields/kinds as
    :func:`oracle_schema`, for use by whoosh-compat's own parser
    (``wc.parse``) in the differential tests: *not* consumed by
    ``oracle_parse``, which only touches the real whoosh ``Schema``.

    ``created``/``modified``/``added`` are registered as ``DATETIME`` (not
    ``DATE``/``date_only``) because that's what the real v2 whoosh schema
    calls them (``DATETIME(sortable=True)`` for all three): whoosh's own
    ``LocalDateParser`` converts *all* of them uniformly through the local
    timezone, so matching that requires the same field kind on our side.

    ``is_shared`` (a ``BOOLEAN`` column in :func:`oracle_schema`) is
    deliberately NOT registered: a vestigial v2 field whose only reader
    (the "shared by me" filter's server-built criterion,
    paperless-ngx#4859) moved to the ORM in paperless-ngx#7507, leaving
    it written but read by nothing; paperless's tantivy backend filters
    permissions entirely outside whoosh-compat, its new search schema
    dropped the field, and no ``FieldKind`` expresses a plain stored
    boolean. A query addressing it
    is an unknown field on this side and a typed boolean term on the
    oracle side: DIVERGENCES.md entry 42, with its own allowlist entry
    and corpus line.
    """

    return FieldRegistry(
        [
            FieldSpec("id", FieldKind.U64),
            FieldSpec("title", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("content", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("asn", FieldKind.U64),
            FieldSpec("correspondent", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("correspondent_id", FieldKind.U64, fast=True),
            FieldSpec(
                "has_correspondent",
                FieldKind.BOOLEAN_EXISTS,
                exists_target="correspondent_id",
            ),
            FieldSpec("tag", FieldKind.KEYWORD, comma_values=True, analyzer=_analyze_keyword_lower),
            FieldSpec(
                "tag_id",
                FieldKind.KEYWORD,
                comma_values=True,
                analyzer=_analyze_keyword,
                fast=True,
            ),
            FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
            FieldSpec("type", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("type_id", FieldKind.U64, fast=True),
            FieldSpec("has_type", FieldKind.BOOLEAN_EXISTS, exists_target="type_id"),
            FieldSpec("created", FieldKind.DATETIME),
            FieldSpec("modified", FieldKind.DATETIME),
            FieldSpec("added", FieldKind.DATETIME),
            FieldSpec("path", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("path_id", FieldKind.U64, fast=True),
            FieldSpec("has_path", FieldKind.BOOLEAN_EXISTS, exists_target="path_id"),
            FieldSpec("notes", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("num_notes", FieldKind.U64),
            FieldSpec("custom_fields", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("custom_field_count", FieldKind.U64, fast=True),
            FieldSpec(
                "has_custom_fields",
                FieldKind.BOOLEAN_EXISTS,
                exists_target="custom_field_count",
            ),
            FieldSpec(
                "custom_fields_id",
                FieldKind.KEYWORD,
                comma_values=True,
                analyzer=_analyze_keyword,
            ),
            FieldSpec("owner", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("owner_id", FieldKind.U64, fast=True),
            FieldSpec("has_owner", FieldKind.BOOLEAN_EXISTS, exists_target="owner_id"),
            FieldSpec("viewer_id", FieldKind.KEYWORD, comma_values=True, analyzer=_analyze_keyword),
            FieldSpec("checksum", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("page_count", FieldKind.U64),
            FieldSpec("original_filename", FieldKind.TEXT, analyzer=_analyze),
            # The two fields below have no counterpart at all in
            # oracle_schema()/V2_FIELDS: real v2 whoosh has neither a JSON
            # field type nor a date-only/date-vs-datetime distinction, so any
            # query addressing either of these always structurally diverges
            # from the oracle (DIVERGENCES.md entries 14 and 37). They exist
            # purely so the differential generator (tests/differential/
            # strategies.py) can reach JSON subpath pattern/existence
            # generator vocabulary and date_only time-bearing-value generator
            # vocabulary at all; neither is wired into V2_FIELDS, so their
            # presence doesn't change how any other field's default-multifield
            # expansion behaves.
            FieldSpec(
                "attrs",
                FieldKind.JSON,
                subpaths=("user", "note", "value", "name"),
                analyzer=_analyze,
            ),
            FieldSpec("release_date", FieldKind.DATE, date_only=True),
        ]
    )


ORACLE_REGISTRY = _make_oracle_registry()


# --------------------------------------------------------------------------
# v2 parser config (paperless-ngx aed9abe48^:src/documents/index.py:
# DelayedFullTextQuery._get_query / LocalDateParser)
# --------------------------------------------------------------------------


class LocalDateParser(English):
    """Clone of paperless-ngx v2's ``LocalDateParser``.

    The original converted a naive result back to UTC using Django's
    ``get_current_timezone()``; here ``tz`` is injected explicitly since
    there's no Django app in this harness.
    """

    def __init__(self, tz: tzinfo_t) -> None:
        super().__init__()
        self.tz = tz

    def reverse_timezone_offset(self, d: datetime) -> datetime:
        return d.replace(tzinfo=self.tz).astimezone(UTC)

    def date_from(self, *args: object, **kwargs: object) -> datetime | timespan | None:  # type: ignore[override]
        d = super().date_from(*args, **kwargs)  # type: ignore[arg-type]
        if isinstance(d, timespan):
            d.start = self.reverse_timezone_offset(d.start)
            d.end = self.reverse_timezone_offset(d.end)
        elif isinstance(d, datetime):
            d = self.reverse_timezone_offset(d)
        return d


# The exact keyword vocabulary paperless-ngx v2's own
# rewrite_natural_date_keywords substitutes away before whoosh sees the
# query. Exported (rather than inlined into _KEYWORD_RE below) so
# tests/differential/allowlist.py's DIVERGENCES.md entry-3 pattern can
# derive from the same list instead of hand-repeating it: that entry's
# divergence exists only for values this rewrite consumes, so a keyword
# added here and missed there would silently stop claiming a divergent
# shape.
NATURAL_DATE_KEYWORDS = (
    "today",
    "yesterday",
    "this month",
    "previous month",
    "previous week",
    "previous quarter",
    "this year",
    "previous year",
)

_KEYWORD_RE = re.compile(
    r"(\b(?:added|created|modified))\s*:\s*[\"']?"
    r"(" + "|".join(NATURAL_DATE_KEYWORDS) + r")[\"']?",
    re.IGNORECASE,
)


def _rewrite_natural_date_keywords(q: str, basedate: datetime, tz: tzinfo_t) -> str:
    """Clone of paperless-ngx v2's ``rewrite_natural_date_keywords``.

    ``DelayedFullTextQuery._get_query`` calls this *before* handing the query
    to whoosh's ``MultifieldParser``: it rewrites e.g. ``created:"previous
    week"`` into an explicit local-tz-converted-to-UTC bracket range
    (``created:[20260316000000 TO 20260322235959]``, second precision,
    inclusive end) via plain string substitution, entirely bypassing
    whoosh's/``LocalDateParser``'s own date grammar for these particular
    keywords. Real whoosh's stock ``English`` grammar (see
    ``English.setup()``) only understands ``today``/``yesterday``/``this
    month``/``this year`` natively: it has no ``previous week``/``previous
    month``/``previous quarter``/``previous year`` support at all, so
    without this rewrite those keywords would always fail to parse as
    dates upstream of this rewrite too.
    """

    local_now = basedate.astimezone(tz)
    today = local_now.date()

    def repl(m: re.Match[str]) -> str:
        field = m.group(1)
        keyword = m.group(2).lower()

        if keyword == "today":
            start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
            end = datetime.combine(today, datetime.max.time(), tzinfo=tz)
        elif keyword == "yesterday":
            yesterday = today - timedelta(days=1)
            start = datetime.combine(yesterday, datetime.min.time(), tzinfo=tz)
            end = datetime.combine(yesterday, datetime.max.time(), tzinfo=tz)
        elif keyword == "this month":
            start = datetime(local_now.year, local_now.month, 1, tzinfo=tz)
            start = _add_months(start, 0)
            end = _add_months(start, 1) - timedelta(seconds=1)
        elif keyword == "previous month":
            this_month_start = datetime(local_now.year, local_now.month, 1, tzinfo=tz)
            start = _add_months(this_month_start, -1)
            end = this_month_start - timedelta(seconds=1)
        elif keyword == "this year":
            start = datetime(local_now.year, 1, 1, tzinfo=tz)
            end = datetime(local_now.year, 12, 31, 23, 59, 59, tzinfo=tz)
        elif keyword == "previous week":
            days_since_monday = local_now.weekday()
            this_week_start = datetime.combine(
                today - timedelta(days=days_since_monday), datetime.min.time(), tzinfo=tz
            )
            start = this_week_start - timedelta(days=7)
            end = this_week_start - timedelta(seconds=1)
        elif keyword == "previous quarter":
            current_quarter = (local_now.month - 1) // 3 + 1
            this_quarter_start_month = (current_quarter - 1) * 3 + 1
            this_quarter_start = datetime(local_now.year, this_quarter_start_month, 1, tzinfo=tz)
            start = _add_months(this_quarter_start, -3)
            end = this_quarter_start - timedelta(seconds=1)
        elif keyword == "previous year":
            start = datetime(local_now.year - 1, 1, 1, tzinfo=tz)
            end = datetime(local_now.year - 1, 12, 31, 23, 59, 59, tzinfo=tz)
        else:  # pragma: no cover - regex only matches known keywords
            raise AssertionError(keyword)

        start_str = start.astimezone(UTC).strftime("%Y%m%d%H%M%S")
        end_str = end.astimezone(UTC).strftime("%Y%m%d%H%M%S")
        return f"{field}:[{start_str} TO {end_str}]"

    return _KEYWORD_RE.sub(repl, q)


def _add_months(d: datetime, months: int) -> datetime:
    month0 = d.month - 1 + months
    year = d.year + month0 // 12
    month = month0 % 12 + 1
    return d.replace(year=year, month=month)


def oracle_parse(q: str, basedate: datetime, tz: tzinfo_t) -> wq.Query:
    """Parse ``q`` with the real whoosh ``MultifieldParser`` configured like
    paperless-ngx v2's ``DelayedFullTextQuery._get_query``: including its
    ``rewrite_natural_date_keywords`` preprocessing step.
    """

    q = _rewrite_natural_date_keywords(q, basedate, tz)
    qp = MultifieldParser(V2_FIELDS, _SCHEMA)
    qp.add_plugin(
        DateParserPlugin(
            basedate=basedate,
            dateparser=LocalDateParser(tz),
        ),
    )
    return qp.parse(q, normalize=False)


# --------------------------------------------------------------------------
# whoosh.query.Query -> whoosh_compat.ast
# --------------------------------------------------------------------------


def _is_null(q: wq.Query) -> bool:
    return isinstance(q, wq_core._NullQuery)


def _decode_date_term(fieldname: str, text: object) -> datetime:
    field = _SCHEMA[fieldname]
    naive = field.from_bytes(text)  # type: ignore[arg-type]
    return naive.replace(tzinfo=UTC)


def _adjust_date_hi(hi: datetime | None, incl_hi: bool) -> tuple[datetime | None, bool]:
    """Whoosh represents an ambiguous/period match's end as
    ``adatetime.ceil()`` (the period's last microsecond, e.g.
    ``...T23:59:59.999999Z``) with an inclusive upper bound. whoosh-compat
    instead represents that same period as a half-open range: the ceiling
    plus one microsecond as an *exclusive* upper bound (see
    ``whoosh_compat.parser.dateparse`` module docstring, paperless#13381).
    An exact instant the user actually typed keeps ``incl_hi=True``.
    """

    if hi is None or not incl_hi:
        return hi, incl_hi
    if hi.microsecond == 999_999:
        return hi + timedelta(microseconds=1), False
    return hi, incl_hi


def _field_ref(name: str | None) -> FieldRef | None:
    """Wrap a real-whoosh fieldname string as a plain :class:`FieldRef`.

    Real whoosh has no JSON field concept, so every fieldname coming out of
    a whoosh query object is always a plain (non-subpath) reference.
    """
    return FieldRef(name) if name is not None else None


def _is_recursively_empty(q: wq.Query) -> bool:
    """True for a compound whose every leaf is itself an empty compound.

    These are the only ``None`` results :func:`_to_ast_node` produces that
    an enclosing ``And``/``Or`` may legitimately *drop*: real whoosh's own
    ``And.normalize()``/``Or.normalize()`` drop an empty compound child
    rather than annihilating the parent (verified directly), so dropping
    it here matches whoosh, and whoosh-compat's parser drops an empty
    group before it ever becomes a live node. Every other ``None`` means
    "this harness cannot represent that subtree", which is a reason to
    skip the whole comparison, not to quietly delete a clause real whoosh
    kept: see :func:`_map_group_children`.
    """
    return isinstance(q, wq.And | wq.Or) and all(_is_recursively_empty(c) for c in q.subqueries)


def _map_group_children(q: wq.Query, reg: FieldRegistry) -> list[ast.Node] | None:
    """Map an ``And``/``Or``'s children, or ``None`` if any child is
    unmappable.

    Dropping an unmappable child instead would silently compare a tree the
    oracle does not actually have. Found by the pre-release staleness
    sweep: ``((content:a[4-4]a) ANDMAYBE (created:0330)) OR ((0-0) AND (0))``
    parses (with ``normalize=False``) to an ``Or`` whose first child is an
    ``AndMaybe`` with an empty *required* side. :func:`_to_ast_node`
    declines to map that ``AndMaybe`` at all, and the ``Or`` used to drop
    it, so the "expected" tree lost a whole branch and the comparison
    reported a mismatch that neither parser is responsible for. Such a
    query is now skipped as oracle-unmappable, which is what it always
    was.
    """
    subs: list[ast.Node] = []
    for child in q.subqueries:
        mapped = _to_ast(child, reg)
        if mapped is None:
            if _is_recursively_empty(child):
                continue
            return None
        subs.append(mapped)
    return subs


def _to_ast_node(q: wq.Query, reg: FieldRegistry) -> ast.Node | None:
    if isinstance(q, wq.Term):
        fieldname = q.fieldname
        ref = _field_ref(fieldname)
        resolved = reg.resolve(ref) if ref is not None else None
        if resolved is not None and resolved.spec.kind in (FieldKind.DATE, FieldKind.DATETIME):
            # A single exact instant: whoosh's DateTimeNode.query() encodes
            # it as a Term of the field's to_bytes() representation instead
            # of a DateRange (see whoosh.qparser.dateparse.DateTimeNode).
            dt = _decode_date_term(fieldname, q.text)
            return ast.DateRange(
                field=FieldRef(fieldname), lo=dt, hi=dt, incl_lo=True, incl_hi=True
            )
        if resolved is not None and resolved.spec.kind is FieldKind.U64:
            # NUMERIC fields self-encode term text into sortable byte keys
            # too (whoosh.fields.NUMERIC.to_bytes); decode back to int.
            n = _SCHEMA[fieldname].from_bytes(q.text)
            return ast.Term(field=ref, text=int(n))
        text: object = q.text
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return ast.Term(field=ref, text=cast("str | int | bool", text))

    if isinstance(q, wq.And):
        and_subs = _map_group_children(q, reg)
        if and_subs is None:
            return None
        # An empty And (whoosh's own raw parse of an empty group, e.g. the
        # "()" in "foo ()") has no meaningful mapping on its own: real
        # whoosh's And.normalize() drops a NullQuery/empty-compound child
        # from an enclosing compound rather than annihilating it (verified
        # directly: And([Term, And([])]).normalize() == Term), matching
        # whoosh-compat's own parser, which now drops an empty group before
        # it ever becomes a live AST node. Building
        # ast.And(children=()) here instead would resurrect the same
        # annihilation this fix is about, purely as a harness artifact.
        if not and_subs:
            return None
        return ast.And(children=tuple(and_subs))

    if isinstance(q, wq.Or):
        or_subs = _map_group_children(q, reg)
        if or_subs is None:
            return None
        if not or_subs:  # see the And branch above
            return None
        return ast.Or(children=tuple(or_subs))

    if isinstance(q, wq.Not):
        inner = _to_ast(q.query, reg)
        if inner is None:
            return None
        return ast.Not(child=inner)

    if isinstance(q, wq.AndNot):
        positive = _to_ast(q.subqueries[0], reg)
        negative = _to_ast(q.subqueries[1], reg)
        if positive is None:
            return None
        return ast.AndNot(positive=positive, negative=negative or ast.Nothing())

    if isinstance(q, wq.AndMaybe):
        required = _to_ast(q.subqueries[0], reg)
        optional = _to_ast(q.subqueries[1], reg)
        if required is None:
            return None
        return ast.AndMaybe(required=required, optional=optional or ast.Nothing())

    if isinstance(q, wq.Require):
        scored = _to_ast(q.subqueries[0], reg)
        filter_only = _to_ast(q.subqueries[1], reg)
        if scored is None or filter_only is None:
            return None
        return ast.Require(scored=scored, filter_only=filter_only)

    if isinstance(q, wq.Phrase):
        # Carry the oracle's own word list, not just the joined text:
        # ast.normalize()'s duplicate-sibling dedupe keys on Phrase.words
        # (two equal-comparing phrases with different word tuples have
        # different positional match sets, and whoosh keeps both), so a
        # words=None projection here would let the expected-side normalize
        # dedupe phrases whoosh itself kept distinct, a false mismatch
        # where whoosh-compat is the more faithful side.
        return ast.Phrase(
            field=_field_ref(q.fieldname),
            text=" ".join(q.words),
            words=tuple(q.words),
            slop=q.slop,
        )

    if isinstance(q, wq.Prefix):
        return ast.Prefix(field=_field_ref(q.fieldname), text=q.text)

    if isinstance(q, wq.Wildcard):
        return ast.Wildcard(field=_field_ref(q.fieldname), pattern=q.text)

    if isinstance(q, wq.TermRange):
        return ast.TermRange(
            field=_field_ref(q.fieldname),
            lo=q.start,
            hi=q.end,
            incl_lo=not q.startexcl,
            incl_hi=not q.endexcl,
        )

    if isinstance(q, wq.DateRange):
        lo, hi = q.startdate, q.enddate
        incl_lo, incl_hi = not q.startexcl, not q.endexcl
        hi, incl_hi = _adjust_date_hi(hi, incl_hi)
        if lo is not None:
            lo = lo if lo.tzinfo is not None else lo.replace(tzinfo=UTC)
        if hi is not None:
            hi = hi if hi.tzinfo is not None else hi.replace(tzinfo=UTC)
        return ast.DateRange(
            field=FieldRef(q.fieldname), lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi
        )

    if isinstance(q, wq.NumericRange):
        return ast.NumericRange(
            field=FieldRef(q.fieldname),
            lo=q.start,
            hi=q.end,
            incl_lo=not q.startexcl,
            incl_hi=not q.endexcl,
        )

    if isinstance(q, wq.Every):
        return ast.Every(field=_field_ref(q.fieldname))

    if _is_null(q):
        return ast.Nothing()

    return None


def _to_ast(q: wq.Query, reg: FieldRegistry) -> ast.Node | None:
    node = _to_ast_node(q, reg)
    if node is None:
        return None
    boost = getattr(q, "boost", 1.0)
    if boost is not None and boost != 1.0:
        node = ast.Boosted(child=node, boost=boost)
    return node


def to_ast(q: wq.Query, reg: FieldRegistry) -> ast.Node | None:
    """Map a real whoosh ``Query`` tree to a whoosh-compat ``ast.Node`` tree.

    Returns ``None`` for query types with no meaningful mapping (the
    corpus test skips these: see :func:`unmapped_reason` for *why*, used
    to give each such skip a distinct, auditable reason rather than a
    catch-all "unmappable").
    """

    return _to_ast(q, reg)


def unmapped_reason(q: wq.Query) -> str:
    """A specific, auditable reason :func:`to_ast` returned ``None`` for
    ``q``: which concrete whoosh query type had no mapping. Every
    whoosh_compat.ast node type (Term/And/Or/Not/AndNot/AndMaybe/Require/
    Phrase/Prefix/Wildcard/TermRange/NumericRange/DateRange/Every/
    _NullQuery) *is* handled by :func:`_to_ast_node`, so in practice this
    only fires for whoosh query types genuinely outside that table (e.g.
    ``FuzzyTerm``, ``Sequence``, ``Regex``: plugins this library doesn't
    implement; see the README's syntax table) or a boosted wrapper around
    one of those.
    """

    return f"oracle-unmappable: whoosh query type {type(q).__name__!r} has no ast.Node mapping"


def compat_raw_parse(
    q: str, reg: FieldRegistry, default_fields: list[str], tz: tzinfo_t, basedate: datetime
) -> tuple[ast.Node, tuple[Diagnostic, ...]]:
    """Parse ``q`` with whoosh-compat's parser *without* the top-level
    :func:`whoosh_compat.ast.normalize` pass that ``whoosh_compat.parse()``
    (the public API) applies internally.

    Mirrors the oracle side's ``parser.parse(text, normalize=False)`` (see
    module docstring): the differential comparison's own
    :func:`whoosh_compat.ast.analyze` call needs to analyze each raw
    ``Term`` *before* any structural normalization runs, or a redundant
    parenthesized single term that analyzes to zero tokens (e.g.
    ``(title:0)``: ``0`` is shorter than StandardAnalyzer's default
    ``minsize=2``) gets pre-collapsed by ``whoosh_compat.parse()``'s
    internal normalize into a bare ``Term`` indistinguishable from an
    unparenthesized one. whoosh's own (also-unnormalized) tree keeps the
    structure that turns into an empty ``And([])`` in that case: comparable,
    post-analysis, only if our side is *also* still unnormalized when the
    0-token drop happens. :func:`~whoosh_compat.ast.analyze` still ends with
    its own :func:`~whoosh_compat.ast.normalize` call, exactly mirroring the
    oracle comparison.
    """

    parser = CompatMultifieldParser(list(default_fields), reg)
    if any(spec.kind in (FieldKind.DATE, FieldKind.DATETIME) for spec in reg):
        parser.add_plugin(CompatDateParserPlugin(basedate, tz))
    node = parser.parse(q)
    return node, tuple(parser.diagnostics)
