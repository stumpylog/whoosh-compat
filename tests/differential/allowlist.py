"""Intended divergences between whoosh-compat and the real whoosh (v2)
oracle. Each entry documents a query pattern that is *expected* to fail the
structural parity comparison, with a reference to the corresponding numbered
entry in DIVERGENCES.md.

Reference-string prefix convention (never bake a clear whoosh bug into
whoosh-compat for parity's sake):

* ``DIVERGENCES.md entry N`` -- see that numbered entry in DIVERGENCES.md.
* ``whoosh-bug:`` -- the oracle's behavior is a confirmed defect in real
  whoosh/paperless-v2 (broken parsing, dropped data, wiring that silently
  no-ops); whoosh-compat keeps its own correct behavior and does NOT
  reproduce the bug.
* ``design:`` -- a deliberate whoosh-compat design choice/new feature with no
  whoosh equivalent to match (not a bug on either side).
* ``out-of-scope:`` -- the query exercises something outside whoosh-compat's
  v1 surface entirely (e.g. a v3-only JSON dotted path against the v2
  schema); there is no meaningful oracle comparison to make.
"""

from __future__ import annotations

import re

# (pattern, DIVERGENCES reference + short reason)
ALLOW: list[tuple[re.Pattern[str], str]] = [
    # #3: date-node boosts are silently dropped by whoosh's DateTimeNode/
    # DateRangeNode.__init__ (both hardcode self.boost = 1.0), while
    # whoosh-compat's DateParserPlugin preserves the typed boost. Scoped to a
    # boost immediately after a created/modified/added clause (bare value or
    # bracket range) specifically -- a boost on a non-date term
    # ("title:foo^2") is unaffected by this bug and must still compare.
    (
        re.compile(
            r"\b(?:created|modified|added):"
            r"(?:\[[^\]]*\]|\S+)"
            r"\^\d"
        ),
        "DIVERGENCES.md entry 3: date-node boost preservation",
    ),

    # #6: unparseable dates/numbers become a structured ErrorLeaf(diagnostic)
    # in whoosh-compat vs whoosh's untyped error_query()/NullQuery-with-.error.
    # This isn't a single-pattern allowlist entry: any parse that reports a
    # diagnostic falls under this divergence, so the test files themselves
    # skip whenever ``result.diagnostics`` is non-empty rather than matching
    # a query pattern here (a fixed regex here couldn't keep up with
    # hypothesis-fuzzed invalid dates/numbers).

    # #2: wildcard/prefix patterns are case-folded via pattern_normalizer in
    # whoosh-compat (Entwä* matches Entwässerungsplan); whoosh matched raw
    # (already-lowercased-at-index-time) terms, so a capitalized wildcard
    # pattern like "Entwä*" never matched in v2 either -- this is a fix, not
    # parity.
    (re.compile(r"Entwä\*"), "DIVERGENCES.md entry 2: wildcard pattern normalization"),

    # design: whoosh-compat's CommaValuesPlugin treats a *quoted* comma-values
    # field value as a literal (SingleQuotePlugin marks it is_quoted); real
    # whoosh has no such plugin at all -- its KEYWORD(commas=True) analyzer
    # always splits on commas at analysis time, quoted or not, so
    # "tag:'foo,bar'" still expands to tag:foo AND tag:bar upstream. Not a
    # whoosh bug -- whoosh simply never had this feature to begin with.
    (re.compile(r"tag:'foo,bar'"), "design: comma_values quote-escape is a whoosh-compat-only feature"),

    # design: JSON dotted-path fields (custom_fields.value, notes.user, ...)
    # aren't registered in the v2 oracle schema/registry (v2 whoosh has no
    # JSON subpath concept at all), so on *both* sides the field is
    # "unknown" -- but the two parsers tag an unknown dotted name
    # differently, so the resulting trees genuinely diverge structurally
    # rather than being simply unmappable (to_ast maps the oracle's tree
    # fine; it's just shaped differently). whoosh-compat's own FieldsPlugin
    # tagger regex is deliberately dot-inclusive (`[\w.]+:` vs whoosh's
    # `\w+:`, see plugins.FieldsPlugin -- this is what makes
    # `notes.user:` resolvable as a JSON subpath *when* a JSON FieldSpec is
    # registered), so it greedily tags the *whole* "custom_fields.value:" run
    # as one candidate fieldname token; whoosh's non-dot-aware tagger only
    # ever matches "value:" (the run *after* the last dot), leaving
    # "custom_fields." as plain text merged differently by each side's
    # unknown-field demotion logic. Confirmed via oracle.compat_raw_parse:
    # both sides produce a real (non-None) tree, they just don't structurally
    # match. This is an inherent consequence of the v1 JSON-subpath tagger
    # feature existing at all, not a bug on either side.
    (
        re.compile(r"\bcustom_fields\.(value|name)\b"),
        (
            "design: dot-inclusive FieldsPlugin tagger ([\\w.]+: vs whoosh's"
            " \\w+:) tags an unregistered dotted name differently than whoosh's"
            " tagger even though neither side has the field registered"
        ),
    ),
    (
        re.compile(r"\bnotes\.(user|note)\b"),
        (
            "design: dot-inclusive FieldsPlugin tagger ([\\w.]+: vs whoosh's"
            " \\w+:) tags an unregistered dotted name differently than whoosh's"
            " tagger even though neither side has the field registered"
        ),
    ),

    # NOTE: DIVERGENCES.md entry 8 ("attached -foo searches for foo") describes a
    # *v1-vs-live-v3* (tantivy) divergence, not v1-vs-v2/whoosh -- it does
    # not apply here. Verified directly: a bare leading "-foo" with nothing
    # before it never triggers either parser's NOT-prefix operator (that
    # requires a preceding term to attach to); both whoosh and whoosh-compat
    # treat it as an ordinary literal word, multifield-expanded like any
    # other unfielded term (down to the same per-field analyzer quirk: "-"
    # survives inside the KEYWORD `tag` field's token but is stripped by
    # TEXT fields' word-boundary tokenizer, on *both* sides identically).
    # An earlier version of this allowlist wrongly carried a "#8" entry here
    # by analogy without verifying it against the actual v2 oracle; removed
    # after confirming "-foo" compares and passes structurally.

    # whoosh-bug (DIVERGENCES.md entry 12): real whoosh's range-bound date parsing
    # (DateParserPlugin.range_to_dt) calls
    # `self.dateparser.get_parser().date_from(...)` -- the *grammar object's*
    # date_from, not `LocalDateParser.date_from` -- so LocalDateParser's
    # timezone-reversal override (reverse_timezone_offset) never actually
    # runs for bracketed ranges in the real v2 system, only for
    # single/keyword values (DateParserPlugin.text_to_dt, which *does* call
    # `self.dateparser.date_from`). Naive range bounds are therefore taken as
    # literal UTC with no local-tz shift applied at all. This is a confirmed
    # defect in paperless-ngx v2's LocalDateParser wiring (the override
    # silently no-ops for exactly the code path -- ranges -- it was written
    # to cover), not an intended whoosh design choice, so whoosh-compat does
    # NOT reproduce it: DateParserPlugin.range_to_node applies the same tz
    # conversion uniformly to both single values and range bounds. Covers
    # every bracketed range on a DATE/DATETIME field in the corpus.
    (
        re.compile(r"\b(?:created|modified|added):\["),
        (
            "whoosh-bug: LocalDateParser's tz-reversal override doesn't reach"
            " range bounds (range_to_dt uses the bare grammar's date_from, not"
            " the override); whoosh-compat applies tz conversion uniformly"
            " instead of reproducing the bug"
        ),
    ),

    # design: whoosh-compat's date grammar adds new keywords (previous week/
    # month/quarter/year) directly to the English grammar (see
    # parser.dateparse module docstring), usable as a single quoted phrase
    # value like whoosh's own multi-word values always require
    # (`created:"previous week"`). Real paperless v2 instead relied on an
    # *app-level* regex preprocessing pass in DelayedFullTextQuery
    # (`rewrite_natural_date_keywords`, index.py) that rewrites e.g.
    # `created:previous week` -- unquoted -- into an explicit bracket range
    # *before* whoosh ever sees the string; real whoosh's own grammar has no
    # native "previous week" support at all (not a bug -- it never claimed to
    # have this feature). That preprocessing hack is paperless-app-specific,
    # not part of whoosh's (or whoosh-compat's) parser proper, so it's out of
    # scope for whoosh-compat's `parse()` -- unquoted multi-word keywords
    # behave like any other unquoted multi-word value (split at the first
    # whitespace, one token per field). The oracle harness replicates the
    # app-level rewrite (see oracle._rewrite_natural_date_keywords) so the
    # *quoted* form (`created:"previous week"`) matches; only the unquoted
    # form is allowlisted here.
    (
        re.compile(
            r"\b(?:created|modified|added):"
            r"(?:previous (?:week|month|quarter|year)|this (?:month|year))\b"
        ),
        (
            "design: unquoted multi-word date keywords need paperless's"
            " app-level rewrite_natural_date_keywords preprocessing, out of"
            " whoosh-compat's parser scope"
        ),
    ),

    # design: a bare "*" wildcard on a field (`title:*`) whoosh-compat
    # simplifies to Every(field) (see QueryParser.wildcard_query's
    # docstring: "the text is exactly '*' -> Every"); real whoosh's
    # WildcardPlugin builds a literal Wildcard('title', '*') query object
    # instead (functionally equivalent -- both match every document with a
    # value in the field -- but a different AST shape). whoosh-compat's
    # Every(field) shape is deliberate: see DIVERGENCES's emitter table
    # (Every(field) -> fast-field exists_query or a regex(".*") fallback for
    # non-fast TEXT, both cheaper than a literal wildcard scan).
    (
        re.compile(r":\*(?:\s|$)"),
        (
            "design: bare field:* simplifies to Every(field) in whoosh-compat vs"
            " a literal Wildcard('*') in whoosh"
        ),
    ),

    # whoosh-bug (DIVERGENCES.md entry 13): real whoosh's WildcardPlugin.do_wildcards
    # (and query.terms.Wildcard.normalize(), same root cause) only tests a
    # trailing-star pattern for "*"/"?" before folding it to a Prefix --
    # despite SPECIAL_CHARS = "*?[" including "[" -- so a pattern like
    # "202[0-3]*" folds to Prefix('title', '202[0-3]') and silently loses the
    # character class instead of staying a Wildcard. whoosh-compat fixed the
    # fold check in both sites that perform it (parser/default.py's
    # _TRAILING_STAR_RE and parser/plugins.py's do_wildcards) to also check
    # for "[", so it keeps the full Wildcard pattern. Not reproduced --
    # whoosh-compat's own trailing-star-with-bracket corpus line
    # (title:202[0-3]*) is intentionally allowlisted here rather than
    # matched against the (buggy) oracle tree.
    (
        re.compile(r"\btitle:202\[0-3\]\*"),
        "whoosh-bug (DIVERGENCES.md entry 13): Wildcard.normalize() bracket fold drops the character class on a trailing-star pattern",
    ),
]


def allowed_reason(query: str) -> str | None:
    """The reference string of the first allowlist entry matching ``query``,
    or ``None`` if no entry matches (i.e. the query must be compared).

    Returning the specific matched reason (rather than a bare bool) lets
    callers report a distinct, auditable skip reason per query instead of a
    single catch-all "allowlisted" message.
    """

    for pattern, reason in ALLOW:
        if pattern.search(query):
            return reason
    return None


def allowed(query: str) -> bool:
    """True if ``query`` matches an intended-divergence pattern and should be
    skipped rather than compared structurally.
    """

    return allowed_reason(query) is not None
