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

    Used both as the oracle registry's TEXT-field analyzer (so
    whoosh-compat's :func:`~whoosh_compat.parser.default` term text and the
    real whoosh index's analyzed tokens are produced by the *same* code) and
    directly by :func:`analyze_ast`.
    """

    return [t.text for t in _STANDARD_ANALYZER(text)]


# KEYWORD fields do *not* use StandardAnalyzer -- whoosh.fields.KEYWORD's own
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
    (``wc.parse``) in the differential tests -- *not* consumed by
    ``oracle_parse``, which only touches the real whoosh ``Schema``.

    ``created``/``modified``/``added`` are registered as ``DATETIME`` (not
    ``DATE``/``date_only``) because that's what the real v2 whoosh schema
    calls them (``DATETIME(sortable=True)`` for all three) -- whoosh's own
    ``LocalDateParser`` converts *all* of them uniformly through the local
    timezone, so matching that requires the same field kind on our side.
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
            FieldSpec(
                "tag", FieldKind.KEYWORD, comma_values=True, analyzer=_analyze_keyword_lower
            ),
            FieldSpec(
                "tag_id", FieldKind.KEYWORD, comma_values=True, analyzer=_analyze_keyword,
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
                "custom_fields_id", FieldKind.KEYWORD, comma_values=True,
                analyzer=_analyze_keyword,
            ),
            FieldSpec("owner", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("owner_id", FieldKind.U64, fast=True),
            FieldSpec("has_owner", FieldKind.BOOLEAN_EXISTS, exists_target="owner_id"),
            FieldSpec(
                "viewer_id", FieldKind.KEYWORD, comma_values=True, analyzer=_analyze_keyword
            ),
            FieldSpec("checksum", FieldKind.TEXT, analyzer=_analyze),
            FieldSpec("page_count", FieldKind.U64),
            FieldSpec("original_filename", FieldKind.TEXT, analyzer=_analyze),
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

    def date_from(self, *args: object, **kwargs: object):  # type: ignore[override]
        d = super().date_from(*args, **kwargs)  # type: ignore[arg-type]
        if isinstance(d, timespan):
            d.start = self.reverse_timezone_offset(d.start)
            d.end = self.reverse_timezone_offset(d.end)
        elif isinstance(d, datetime):
            d = self.reverse_timezone_offset(d)
        return d


_KEYWORD_RE = re.compile(
    r"(\b(?:added|created|modified))\s*:\s*[\"']?"
    r"(today|yesterday|this month|previous month|previous week|previous quarter"
    r"|this year|previous year)[\"']?",
    re.IGNORECASE,
)


def _rewrite_natural_date_keywords(q: str, basedate: datetime, tz: tzinfo_t) -> str:
    """Clone of paperless-ngx v2's ``rewrite_natural_date_keywords``.

    ``DelayedFullTextQuery._get_query`` calls this *before* handing the query
    to whoosh's ``MultifieldParser`` -- it rewrites e.g. ``created:"previous
    week"`` into an explicit local-tz-converted-to-UTC bracket range
    (``created:[20260316000000 TO 20260322235959]``, second precision,
    inclusive end) via plain string substitution, entirely bypassing
    whoosh's/``LocalDateParser``'s own date grammar for these particular
    keywords. Real whoosh's stock ``English`` grammar (see
    ``English.setup()``) only understands ``today``/``yesterday``/``this
    month``/``this year`` natively -- it has no ``previous week``/``previous
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
            this_quarter_start = datetime(
                local_now.year, this_quarter_start_month, 1, tzinfo=tz
            )
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
    paperless-ngx v2's ``DelayedFullTextQuery._get_query`` -- including its
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


def _to_ast_node(q: wq.Query, reg: FieldRegistry) -> ast.Node | None:
    if isinstance(q, wq.Term):
        fieldname = q.fieldname
        spec = reg.resolve(fieldname) if fieldname else None
        if spec is not None and spec.kind in (FieldKind.DATE, FieldKind.DATETIME):
            # A single exact instant: whoosh's DateTimeNode.query() encodes
            # it as a Term of the field's to_bytes() representation instead
            # of a DateRange (see whoosh.qparser.dateparse.DateTimeNode).
            dt = _decode_date_term(fieldname, q.text)
            return ast.DateRange(field=fieldname, lo=dt, hi=dt, incl_lo=True, incl_hi=True)
        if spec is not None and spec.kind is FieldKind.U64:
            # NUMERIC fields self-encode term text into sortable byte keys
            # too (whoosh.fields.NUMERIC.to_bytes); decode back to int.
            n = _SCHEMA[fieldname].from_bytes(q.text)
            return ast.Term(field=fieldname, text=int(n))
        text: object = q.text
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return ast.Term(field=fieldname, text=cast("str | int | bool", text))

    if isinstance(q, wq.And):
        and_subs = [s for s in (_to_ast(c, reg) for c in q.subqueries) if s is not None]
        return ast.And(children=tuple(and_subs))

    if isinstance(q, wq.Or):
        or_subs = [s for s in (_to_ast(c, reg) for c in q.subqueries) if s is not None]
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
        return ast.Phrase(field=q.fieldname, text=" ".join(q.words), slop=q.slop)

    if isinstance(q, wq.Prefix):
        return ast.Prefix(field=q.fieldname, text=q.text)

    if isinstance(q, wq.Wildcard):
        return ast.Wildcard(field=q.fieldname, pattern=q.text)

    if isinstance(q, wq.TermRange):
        return ast.TermRange(
            field=q.fieldname, lo=q.start, hi=q.end,
            incl_lo=not q.startexcl, incl_hi=not q.endexcl,
        )

    if isinstance(q, wq.DateRange):
        lo, hi = q.startdate, q.enddate
        incl_lo, incl_hi = not q.startexcl, not q.endexcl
        hi, incl_hi = _adjust_date_hi(hi, incl_hi)
        if lo is not None:
            lo = lo if lo.tzinfo is not None else lo.replace(tzinfo=UTC)
        if hi is not None:
            hi = hi if hi.tzinfo is not None else hi.replace(tzinfo=UTC)
        return ast.DateRange(field=q.fieldname, lo=lo, hi=hi, incl_lo=incl_lo, incl_hi=incl_hi)

    if isinstance(q, wq.NumericRange):
        return ast.NumericRange(
            field=q.fieldname, lo=q.start, hi=q.end,
            incl_lo=not q.startexcl, incl_hi=not q.endexcl,
        )

    if isinstance(q, wq.Every):
        return ast.Every(field=q.fieldname)

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
    corpus test skips these -- see :func:`unmapped_reason` for *why*, used
    to give each such skip a distinct, auditable reason rather than a
    catch-all "unmappable").
    """

    return _to_ast(q, reg)


def unmapped_reason(q: wq.Query) -> str:
    """A specific, auditable reason :func:`to_ast` returned ``None`` for
    ``q`` -- which concrete whoosh query type had no mapping. Every type in
    the brief's mapping table (Term/And/Or/Not/AndNot/AndMaybe/Require/
    Phrase/Prefix/Wildcard/TermRange/NumericRange/DateRange/Every/
    _NullQuery) *is* handled by :func:`_to_ast_node`, so in practice this
    only fires for whoosh query types genuinely outside that table (e.g.
    ``FuzzyTerm``, ``Sequence``, ``Regex`` -- v1 plugins not implemented
    per the design doc's "No GtLt / fuzzy-~ / r'regex' / Sequence" note) or
    a boosted wrapper around one of those.
    """

    return f"oracle-unmappable: whoosh query type {type(q).__name__!r} has no ast.Node mapping"


# --------------------------------------------------------------------------
# whoosh_compat.ast -> forward-analyzed whoosh_compat.ast (comparable form)
# --------------------------------------------------------------------------


def _analyzed_term(field: str | None, text: object, reg: FieldRegistry) -> ast.Node | None:
    spec = reg.resolve(field) if field else None
    if spec is None or spec.kind not in (FieldKind.TEXT, FieldKind.KEYWORD):
        return ast.Term(field=field, text=cast("str | int | bool", text))
    if not isinstance(text, str):
        return ast.Term(field=field, text=cast("str | int | bool", text))
    tokens = spec.analyzer(text) if spec.analyzer else [text]
    if not tokens:
        return None
    if len(tokens) == 1:
        return ast.Term(field=field, text=tokens[0])
    return ast.And(children=tuple(ast.Term(field=field, text=t) for t in tokens))


def _analyzed_phrase(node: ast.Phrase, reg: FieldRegistry) -> ast.Node | None:
    spec = reg.resolve(node.field) if node.field else None
    analyzer = spec.analyzer if spec is not None else None
    tokens = analyzer(node.text) if analyzer else node.text.split()
    if not tokens:
        return None
    return ast.Phrase(field=node.field, text=" ".join(tokens), slop=node.slop)


def compat_raw_parse(
    q: str, reg: FieldRegistry, default_fields: list[str], tz: tzinfo_t, basedate: datetime
) -> tuple[ast.Node, tuple[Diagnostic, ...]]:
    """Parse ``q`` with whoosh-compat's parser *without* the top-level
    :func:`whoosh_compat.ast.normalize` pass that ``whoosh_compat.parse()``
    (the public API) applies internally.

    Mirrors the oracle side's ``parser.parse(text, normalize=False)`` (see
    module docstring / brief): :func:`analyze_ast` needs to forward-analyze
    each raw ``Term`` *before* any structural normalization runs, or a
    redundant parenthesized single term that analyzes to zero tokens (e.g.
    ``(title:0)`` -- ``0`` is shorter than StandardAnalyzer's default
    ``minsize=2``) gets pre-collapsed by ``whoosh_compat.parse()``'s
    internal normalize into a bare ``Term`` indistinguishable from an
    unparenthesized one. whoosh's own (also-unnormalized) tree keeps the
    structure that turns into an empty ``And([])`` in that case --
    comparable, post-:func:`analyze_ast`, only if our side is *also* still
    unnormalized when the 0-token drop happens. ``analyze_ast`` still ends
    with its own :func:`~whoosh_compat.ast.normalize` call, exactly
    mirroring the oracle comparison.
    """

    parser = CompatMultifieldParser(list(default_fields), reg)
    if any(spec.kind in (FieldKind.DATE, FieldKind.DATETIME) for spec in reg):
        parser.add_plugin(CompatDateParserPlugin(basedate, tz))
    node = parser.parse(q)
    return node, tuple(parser.diagnostics)


def analyze_ast(node: ast.Node, reg: FieldRegistry) -> ast.Node:
    """Replace each TEXT/KEYWORD ``Term``'s raw text with its analyzed
    tokens (multi-token -> ``And`` of ``Term``s, matching the oracle
    schema's default multitoken policy), then normalize.

    A 0-token analyzed value (a stopword, or a token shorter than
    StandardAnalyzer's ``minsize=2``) is *dropped from its parent group*
    entirely -- not replaced with an explicit :class:`~whoosh_compat.ast.Nothing`
    leaf -- mirroring whoosh's own ``GroupNode``/``Wrapper``/``BinaryGroup``
    ``query()`` methods (``qa is None -> use qb``, etc; see
    ``whoosh.qparser.syntax``): a stopword inside ``foo AND the`` doesn't
    make the *whole* query match nothing, it just disappears as though it
    was never typed. This deliberately differs from
    :func:`whoosh_compat.ast.normalize`'s own rule that an *explicit*
    ``Nothing()`` (e.g. a genuinely empty range) poisons an enclosing
    ``And`` -- that's a different, real "no results" case, not a
    dropped-token case, and conflating the two here would produce
    normalize()-driven false mismatches like ``(title:0) AND (0)`` (a
    single-char token whoosh's default analyzer drops as too short)
    resolving to ``Nothing`` on whoosh-compat's side but to
    ``Term('tag', '0')`` (the multifield OR's one surviving KEYWORD branch)
    on whoosh's, since KEYWORD fields have no minsize filter.
    """

    def go(n: ast.Node) -> ast.Node | None:
        if isinstance(n, ast.Term):
            return _analyzed_term(n.field, n.text, reg)
        if isinstance(n, ast.Phrase):
            return _analyzed_phrase(n, reg)
        if isinstance(n, ast.And):
            # Mirrors whoosh's GroupNode.query(): a plain And/Or group
            # *always* builds a (possibly empty) query object from whatever
            # children survive analysis -- it never itself disappears the
            # way a Wrapper/BinaryGroup does. normalize()'s existing
            # empty-group-> Nothing rule handles the all-dropped case
            # identically on both sides of the comparison.
            subs = [s for s in (go(c) for c in n.children) if s is not None]
            return ast.And(children=tuple(subs))
        if isinstance(n, ast.Or):
            subs = [s for s in (go(c) for c in n.children) if s is not None]
            return ast.Or(children=tuple(subs))
        if isinstance(n, ast.Not):
            child = go(n.child)
            return None if child is None else ast.Not(child=child)
        if isinstance(n, ast.AndNot):
            a, b = go(n.positive), go(n.negative)
            if a is None and b is None:
                return None
            return b if a is None else (a if b is None else ast.AndNot(positive=a, negative=b))
        if isinstance(n, ast.AndMaybe):
            a, b = go(n.required), go(n.optional)
            if a is None and b is None:
                return None
            return b if a is None else (a if b is None else ast.AndMaybe(required=a, optional=b))
        if isinstance(n, ast.Require):
            a, b = go(n.scored), go(n.filter_only)
            if a is None and b is None:
                return None
            return b if a is None else (a if b is None else ast.Require(scored=a, filter_only=b))
        if isinstance(n, ast.Boosted):
            child = go(n.child)
            return None if child is None else ast.Boosted(child=child, boost=n.boost)
        return n

    result = go(node)
    return ast.normalize(result if result is not None else ast.Nothing())
