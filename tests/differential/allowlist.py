"""Intended divergences between whoosh-compat and the real whoosh (v2)
oracle. Each entry documents a query pattern that is *expected* to fail the
structural parity comparison, with a reference to the corresponding numbered
entry in DIVERGENCES.md.

Reference-string prefix convention (never bake a clear whoosh bug into
whoosh-compat for parity's sake):

* ``DIVERGENCES.md entry N``: see that numbered entry in DIVERGENCES.md.
* ``whoosh-bug:``: the oracle's behavior is a confirmed defect in real
  whoosh/paperless-v2 (broken parsing, dropped data, wiring that silently
  no-ops); whoosh-compat keeps its own correct behavior and does NOT
  reproduce the bug.
* ``design:``: a deliberate whoosh-compat design choice/new feature with no
  whoosh equivalent to match (not a bug on either side).
* ``out-of-scope:``: the query exercises something entirely outside
  whoosh-compat's current query surface (e.g. a JSON dotted path, a
  whoosh-compat-only concept with no counterpart in the real-whoosh schema
  being compared against); there is no meaningful oracle comparison to make.
"""

from __future__ import annotations

import re

# (pattern, DIVERGENCES reference + short reason)
ALLOW: list[tuple[re.Pattern[str], str]] = [
    # #3: date-node boosts are silently dropped by whoosh's DateTimeNode/
    # DateRangeNode.__init__ (both hardcode self.boost = 1.0), while
    # whoosh-compat's DateParserPlugin preserves the typed boost. Scoped to a
    # boost immediately after a created/modified/added clause (bare value or
    # bracket range) specifically: a boost on a non-date term
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
    # whoosh-compat, but only at *emit* time (TantivyEmitter, see
    # ARCHITECTURE.md's analyzer-contract paragraph): the parsed AST always
    # keeps the pattern's original case (Wildcard.pattern/Prefix.text are
    # never touched at parse time). Real whoosh instead folds case into the
    # AST itself: field.process_text(text, tokenize=False) still runs the
    # field's LowercaseFilter even though tokenize=False skips the
    # tokenizer (filters aren't skipped, see
    # whoosh.analysis.tokenizers.RegexTokenizer.__call__), so any wildcard/
    # prefix pattern containing an uppercase letter parses to a
    # lowercased Wildcard/Prefix node on the oracle side but an
    # unmodified-case one on whoosh-compat's. This was originally scoped to
    # the single literal corpus string "Wär*"; broadened here (grammar-fuzz
    # discovered via `title:A*`) to the general shape, since the root cause
    # (parse-time AST case is never folded by whoosh-compat, only
    # emit-time text is) applies identically to every field/pattern, not
    # just that one string. The field prefix is optional (`(?:\w+:)?`) so
    # this also covers an unfielded, multifield-expanded pattern like the
    # original bare "Wär*" corpus line, which has no ":" at all.
    (
        re.compile(r"\b(?:\w+:)?(?=[^\s()]*[A-Z])(?=[^\s()]*[*?])[^\s()]+"),
        (
            "DIVERGENCES.md entry 2: wildcard/prefix pattern case is folded into"
            " the AST by whoosh (LowercaseFilter runs even with tokenize=False)"
            " but only at emit time by whoosh-compat (FieldSpec.pattern_normalizer)"
        ),
    ),
    # design: whoosh-compat's CommaValuesPlugin treats a *quoted* comma-values
    # field value as a literal (SingleQuotePlugin marks it is_quoted); real
    # whoosh has no such plugin at all: its KEYWORD(commas=True) analyzer
    # always splits on commas at analysis time, quoted or not, so
    # "tag:'foo,bar'" still expands to tag:foo AND tag:bar upstream. Not a
    # whoosh bug: whoosh simply never had this feature to begin with.
    (
        re.compile(r"tag:'foo,bar'"),
        "DIVERGENCES.md entry 17: comma_values quote-escape is a whoosh-compat-only feature",
    ),
    # design: JSON dotted-path fields (custom_fields.value, notes.user, ...)
    # aren't registered in the v2 oracle schema/registry (v2 whoosh has no
    # JSON subpath concept at all), so on *both* sides the field is
    # "unknown": but the two parsers tag an unknown dotted name
    # differently, so the resulting trees genuinely diverge structurally
    # rather than being simply unmappable (to_ast maps the oracle's tree
    # fine; it's just shaped differently). whoosh-compat's own FieldsPlugin
    # tagger regex is deliberately dot-inclusive (`[\w.]+:` vs whoosh's
    # `\w+:`, see plugins.FieldsPlugin: this is what makes
    # `notes.user:` resolvable as a JSON subpath *when* a JSON FieldSpec is
    # registered), so it greedily tags the *whole* "custom_fields.value:" run
    # as one candidate fieldname token; whoosh's non-dot-aware tagger only
    # ever matches "value:" (the run *after* the last dot), leaving
    # "custom_fields." as plain text merged differently by each side's
    # unknown-field demotion logic. Confirmed via oracle.compat_raw_parse:
    # both sides produce a real (non-None) tree, they just don't structurally
    # match. This is an inherent consequence of whoosh-compat's JSON-subpath
    # tagger feature existing at all, not a bug on either side.
    (
        re.compile(r"\bcustom_fields\.(value|name)\b"),
        (
            "DIVERGENCES.md entry 14 (design): dot-inclusive FieldsPlugin"
            " tagger ([\\w.]+: vs whoosh's \\w+:) tags an unregistered dotted"
            " name differently than whoosh's tagger even though neither side"
            " has the field registered"
        ),
    ),
    (
        re.compile(r"\bnotes\.(user|note)\b"),
        (
            "DIVERGENCES.md entry 14 (design): dot-inclusive FieldsPlugin"
            " tagger ([\\w.]+: vs whoosh's \\w+:) tags an unregistered dotted"
            " name differently than whoosh's tagger even though neither side"
            " has the field registered"
        ),
    ),
    # NOTE: DIVERGENCES.md entry 8 ("attached -foo searches for foo") describes
    # a divergence between whoosh-compat and paperless-ngx's live tantivy-based
    # search (not one between whoosh-compat and real whoosh): it does
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
    # `self.dateparser.get_parser().date_from(...)`, the *grammar object's*
    # date_from, not `LocalDateParser.date_from`, so LocalDateParser's
    # timezone-reversal override (reverse_timezone_offset) never actually
    # runs for bracketed ranges in the real v2 system, only for
    # single/keyword values (DateParserPlugin.text_to_dt, which *does* call
    # `self.dateparser.date_from`). Naive range bounds are therefore taken as
    # literal UTC with no local-tz shift applied at all. This is a confirmed
    # defect in paperless-ngx v2's LocalDateParser wiring (the override
    # silently no-ops for exactly the code path (ranges) it was written
    # to cover), not an intended whoosh design choice, so whoosh-compat does
    # NOT reproduce it: DateParserPlugin.range_to_node applies the same tz
    # conversion uniformly to both single values and range bounds. Covers
    # every bracketed range on a DATE/DATETIME field in the corpus. The
    # opening bracket is "[" or "{" (inclusive/exclusive): the root cause
    # (range_to_dt's missing ToEnd/override wiring) doesn't care which one
    # was typed, only that it's a range at all; broadened from "[" only
    # after the grammar-aware fuzzer generated an exclusive-bracket range
    # ("created:{TO 1000]") that hit the identical bypass.
    (
        re.compile(r"\b(?:created|modified|added):[\[{]"),
        (
            "whoosh-bug: LocalDateParser's tz-reversal override doesn't reach"
            " range bounds (range_to_dt uses the bare grammar's date_from, not"
            " the override); whoosh-compat applies tz conversion uniformly"
            " instead of reproducing the bug"
        ),
    ),
    # design (DIVERGENCES.md entry 18): a bare (non-bracketed) separated-ISO
    # date value on a date field ("created:2020-01-01", "created:2020-01")
    # is numerically correct on both sides but structurally different: real
    # whoosh's DateParserPlugin.text_to_dt fails to fully parse it (the same
    # grammar-ordering limitation entry 12 describes for range bounds), but
    # ErrorNode.query() falls back to running the original node's own
    # query() method anyway, which for an ordinary fielded term reaches
    # DATETIME.parse_query's field-level self-parse (fields.py) instead:
    # numerically correct, but a query.NumericRange, not the
    # DateTimeNode/DateRangeNode shape a successful text_to_dt would have
    # produced. whoosh-compat has no equivalent field-level fallback
    # architecture (DateParserPlugin's grammar is the only date-parsing
    # path, see parser/dateparse.py's module docstring); after fixing the
    # bundle Choice's alternative order, its single grammar path parses
    # these directly into a DateRange. Scoped to a bare value only (no "[" right
    # after the colon, optionally single-quoted) so it doesn't also swallow
    # the bracketed-range corpus lines above (those are covered by the
    # broader "\[" entry regardless).
    (
        re.compile(r"\b(?:created|modified|added):'?\d{4}[-. /]\d"),
        (
            "DIVERGENCES.md entry 18: bare separated-ISO date value parses"
            " correctly on both sides but via a different mechanism/AST"
            " shape (whoosh's ErrorNode-falls-back-to-field.parse_query vs"
            " whoosh-compat's single DateParserPlugin grammar path)"
        ),
    ),
    # design: whoosh-compat's date grammar adds new keywords (previous week/
    # month/quarter/year) directly to the English grammar (see
    # parser.dateparse module docstring), usable as a single quoted phrase
    # value like whoosh's own multi-word values always require
    # (`created:"previous week"`). Real paperless v2 instead relied on an
    # *app-level* regex preprocessing pass in DelayedFullTextQuery
    # (`rewrite_natural_date_keywords`, index.py) that rewrites e.g.
    # `created:previous week` (unquoted) into an explicit bracket range
    # *before* whoosh ever sees the string; real whoosh's own grammar has no
    # native "previous week" support at all (not a bug, it never claimed to
    # have this feature). That preprocessing hack is paperless-app-specific,
    # not part of whoosh's (or whoosh-compat's) parser proper, so it's out of
    # scope for whoosh-compat's `parse()`: unquoted multi-word keywords
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
            "DIVERGENCES.md entry 19: unquoted multi-word date keywords need"
            " paperless's app-level rewrite_natural_date_keywords"
            " preprocessing, out of whoosh-compat's parser scope"
        ),
    ),
    # design: a bare "*" wildcard on a field (`title:*`) whoosh-compat
    # simplifies to Every(field) (see QueryParser.wildcard_query's
    # docstring: "the text is exactly '*' -> Every"); real whoosh's
    # WildcardPlugin builds a literal Wildcard('title', '*') query object
    # instead (functionally equivalent: both match every document with a
    # value in the field: but a different AST shape). whoosh-compat's
    # Every(field) shape is deliberate: see DIVERGENCES's emitter table
    # (Every(field) -> fast-field exists_query or a regex(".*") fallback for
    # non-fast TEXT, both cheaper than a literal wildcard scan).
    # A standalone "*" (bounded by whitespace/parens/start-end, optionally
    # preceded by "field:") triggers the same simplification whether or not
    # it's fielded: an entirely bare "*" multifield-expands to one Every()
    # per default field, same shape as the fielded case, just without the
    # ":". Broadened from a colon-only match (which missed the bare form,
    # found by the grammar-aware fuzzer) without also catching "*" used as
    # a wildcard character inside a larger pattern like "produ*name" or a
    # trailing-star fold like "abc*" (neither of those has a "*" bounded by
    # whitespace/parens/start-end on both sides).
    (
        re.compile(r"(?:^|(?<=[\s(:]))\*(?=$|[\s)])"),
        (
            "DIVERGENCES.md entry 20: bare field:* (or unfielded *) simplifies to"
            " Every(field) in whoosh-compat vs a literal Wildcard('*') in whoosh"
        ),
    ),
    # whoosh-bug (DIVERGENCES.md entry 13): real whoosh's WildcardPlugin.do_wildcards
    # (and query.terms.Wildcard.normalize(), same root cause) only tests a
    # trailing-star pattern for "*"/"?" before folding it to a Prefix:
    # despite SPECIAL_CHARS = "*?[" including "[": so a pattern like
    # "202[0-3]*" folds to Prefix('title', '202[0-3]') and silently loses the
    # character class instead of staying a Wildcard. whoosh-compat fixed the
    # fold check in both sites that perform it (parser/default.py's
    # _TRAILING_STAR_RE and parser/plugins.py's do_wildcards) to also check
    # for "[", so it keeps the full Wildcard pattern. Not reproduced:
    # whoosh-compat's own trailing-star-with-bracket corpus line
    # (title:202[0-3]*) is intentionally allowlisted here rather than
    # matched against the (buggy) oracle tree. Broadened from the single
    # literal corpus string to the general shape (any field/value with a
    # bracket class immediately followed by a trailing "*"): the root cause
    # is a check that's missing for every field/pattern, not just this one
    # corpus line, as the grammar-aware fuzzer confirmed (title:0[0-0]*).
    (
        re.compile(r"\b\w+:[^\s()]*\[[^\]]*\]\*(?:\s|$|\))"),
        "whoosh-bug (DIVERGENCES.md entry 13): Wildcard.normalize() bracket fold drops the character class on a trailing-star pattern",
    ),
    # design (extends DIVERGENCES.md entry 23, found by the grammar-aware
    # fuzzer, see test_hypothesis.py): NOT of a term/phrase whose value is
    # empty after the field's own token-dropping analysis (a stopword, or a
    # token shorter than StandardAnalyzer's minsize=2) reaches the exact
    # same divergence entry 23 already documents at emit time, but at the
    # differential AST-comparison layer instead: oracle.analyze_ast's own
    # "a dropped Term/Phrase vanishes from its parent group" rule (see that
    # function's docstring) turns the NOT's now-empty child into an empty
    # And(), which whoosh_compat.ast.normalize()'s pre-existing
    # Not(Nothing) -> Every rule then upgrades to Every(): whoosh-compat's
    # comparison tree becomes "matches everything", while real whoosh's
    # Not(NullQuery).normalize() stays NullQuery ("matches nothing"), same
    # as entry 23. Scoped to a NOT directly wrapping a single known-
    # zero-token-word value on any field (see strategies.ZERO_TOKEN_WORDS;
    # kept as a literal list here rather than importing strategies.py, so
    # this module doesn't depend on the fuzzer that discovered the case).
    # The field name itself is left generic (any \w+) rather than
    # enumerated: the mechanism applies to every TEXT field in the
    # registry, and an earlier version of this entry that spelled out only
    # a handful of field names missed "owner" (found by the fuzzer, which
    # samples the oracle registry's full field list). The value alternation
    # also includes a bare "\w" (matches exactly one word character): any
    # single-character value is zero-token too (StandardAnalyzer's
    # minsize=2 drops it), and the grammar-aware fuzzer's generic term
    # atom (not just its dedicated zero-token atom) produces single-
    # character words often enough by chance ("title:2", not just the
    # curated word list) that enumerating specific single characters here
    # would be a losing game; the "\w" alternative, ordered last so the
    # named stopwords still match themselves rather than just their first
    # letter, covers all of them at once. The four registered KEYWORD
    # fields are excluded (negative lookahead): whoosh's KEYWORD analyzer
    # only splits on commas, with no stopword/minsize filtering, so a
    # single-character KEYWORD value is *not* zero-token and a NOT of one
    # is a real comparison, not this divergence.
    (
        re.compile(
            r"\bNOT\s*\(*\s*(?!(?:tag|tag_id|custom_fields_id|viewer_id):)\w+:"
            r"(?:the|a|an|of|to|and|in|is|it|by|\w)(?=\W|$)"
        ),
        (
            "DIVERGENCES.md entry 23: NOT of a zero-token term/phrase reaches the"
            " same emit-time divergence at the AST-comparison layer"
        ),
    ),
    # design (DIVERGENCES.md entry 24, found by the grammar-aware fuzzer): a
    # quoted phrase whose entire content analyzes to zero tokens (every word
    # is a stopword or shorter than minsize=2) parses on the oracle side to
    # a real (non-None) whoosh.query.Phrase object with an empty words list
    # (whoosh tokenizes phrase content at *parse* time, see
    # PhrasePlugin.PhraseNode.query in whoosh/qparser/plugins.py), which
    # oracle.to_ast maps faithfully to ast.Phrase(text=""); whoosh-compat
    # defers phrase analysis to emit time (ARCHITECTURE.md's analyzer
    # contract), so oracle.analyze_ast's own _analyzed_phrase helper
    # correctly drops the whole phrase (mirrors what TantivyEmitter would
    # do), collapsing the enclosing group to Nothing() instead. Scoped to a
    # double-quoted phrase whose entire content is one or more known
    # zero-token words (see strategies.ZERO_TOKEN_WORDS; kept as a literal
    # list here, same rationale as the entry-23 allowlist entry above). The
    # field name is left generic for the same reason entry 23's was
    # broadened above. Each word is either a named stopword or a bare
    # single character (any single char is zero-token too, StandardAnalyzer's
    # minsize=2 drops it): the trailing lookahead `(?=[\s"])` on every
    # alternative requires the match to actually end there, so a real
    # (non-zero-token) word that merely *starts with* a stopword or a
    # digit, e.g. "thermal" or "20th", is not mistaken for one, the same
    # false-positive risk fixed for entry 23 above.
    (
        re.compile(
            r"\b\w+:"
            r'"(?:the|a|an|of|to|and|in|is|it|by|\w)(?=[\s"])'
            r'(?:\s+(?:the|a|an|of|to|and|in|is|it|by|\w)(?=[\s"]))*"'
        ),
        (
            "DIVERGENCES.md entry 24: an all-zero-token quoted phrase parses to"
            " a real empty-words Phrase object in whoosh (tokenized at parse"
            " time) but is dropped entirely by whoosh-compat's emit-time"
            " analysis"
        ),
    ),
    # design (DIVERGENCES.md entry 25, found by the grammar-aware fuzzer): a
    # bare (non-bracketed) relative date offset ("created:now-7d",
    # "created:-3mos") parses to a real DateRange in whoosh-compat (README's
    # syntax table documents this directly: "created:now-7d" is listed as a
    # bare example, not just a range bound), but real whoosh's date grammar
    # only recognizes this relative-offset syntax inside a bracketed range's
    # bounds; a bare value in this shape fails to parse as a date on the
    # oracle side and falls back to NullQuery. Confirmed directly:
    # oracle_parse("created:now-7d", ...) -> NullQuery, while
    # oracle_parse("created:[now-7d TO now]", ...) parses the exact same
    # relative-offset text correctly. A whoosh-compat feature with no whoosh
    # equivalent, not a bug either side. Scoped to a value starting with
    # "now" immediately followed by a sign, or a bare "-" immediately
    # followed by a digit (a relative offset with a *space*, e.g.
    # "created:-1 week", already fails to parse as a single token on both
    # sides and is skipped separately via the DIVERGENCES.md entry 6
    # diagnostics check, not by this entry).
    (
        re.compile(r"\b(?:created|modified|added):'?(?:now[+-]|-\d)"),
        (
            "DIVERGENCES.md entry 25: a bare relative date offset parses as a"
            " DateRange in whoosh-compat (a documented feature/extension) but"
            " whoosh's grammar only supports this syntax inside a bracketed range"
        ),
    ),
    # design (DIVERGENCES.md entry 27, found by the property-based fuzzer
    # while fixing issue #10): ANDNOT/ANDMAYBE/REQUIRE whose positive/
    # required/scored side analyzes to zero tokens, nested inside further
    # grouping alongside a sibling clause, poisons the whole enclosing And
    # on whoosh-compat's side but real whoosh drops it. Both sides agree
    # the degenerate AndNot/AndMaybe/Require itself resolves to "match
    # nothing"; the divergence is only in whether that Nothing propagates
    # through an *enclosing* And (whoosh-compat's ast.normalize() "Nothing
    # propagates through And" rule, deliberately kept as-is per issue #10)
    # or gets dropped from it (real whoosh's And.normalize(), which drops a
    # NullQuery child instead of poisoning). No corpus line uses ANDNOT/
    # ANDMAYBE/REQUIRE at all (grep-verified), so this only affects the
    # hypothesis fuzzers, which generate these operators freely.
    (
        re.compile(r"\bANDNOT\b|\bANDMAYBE\b|\bREQUIRE\b"),
        (
            "DIVERGENCES.md entry 27: ANDNOT/ANDMAYBE/REQUIRE with a"
            " zero-token positive/required/scored side poisons an enclosing"
            " And on whoosh-compat's side (Nothing-propagation algebra) but"
            " is dropped on whoosh's"
        ),
    ),
    # design (DIVERGENCES.md entry 33): a whitespace-padded quoted value on a
    # BOOLEAN_EXISTS field reads False in whoosh-compat (strips before the
    # trues/falses membership check) but True in real whoosh
    # (BOOLEAN._obj_to_bool checks the *unstripped* text, then falls through
    # to bool(qstring), which is True for any non-empty string). Scoped to a
    # single-quoted BOOLEAN_EXISTS value that has leading or trailing
    # whitespace inside the quotes; a quoted empty value ("''") is
    # deliberately excluded, since that shape no longer diverges (both
    # sides now agree it's False) and is compared normally instead.
    (
        re.compile(r"\bhas_tag:'\s+\S.*'|\bhas_tag:'.*\S\s+'"),
        (
            "DIVERGENCES.md entry 33: a whitespace-padded quoted"
            " BOOLEAN_EXISTS value reads False in whoosh-compat (stripped"
            " before the trues/falses check) but True in whoosh"
            " (unstripped check falls through to bool(qstring))"
        ),
    ),
    # design (DIVERGENCES.md entry 36): a comma-values field boost
    # (`tag:alpha,beta^2`) attaches to the whole split group in
    # whoosh-compat, since CommaValuesPlugin's comma split
    # (FILTER_COMMA_VALUES, priority 105) runs before BoostPlugin binds the
    # boost to the preceding node (FILTER_BOOSTS_POST, priority 510):
    # `Boosted(And(tag:alpha, tag:beta), 2.0)`. Real whoosh has no
    # comma-splitting parser plugin at all (see entry 17 above): its
    # KEYWORD(commas=True) analyzer splits on commas at analysis time, long
    # after the boost already bound to the single, still-unsplit term, so
    # each split term carries its own copy of the boost instead:
    # `And(Boosted(tag:alpha, 2.0), Boosted(tag:beta, 2.0))`. Matched
    # documents and summed relevance scoring are identical either way
    # (verified both algebraically and against a live tantivy index; see
    # the DIVERGENCES.md entry), so this is an AST-shape-only divergence,
    # not a whoosh bug. Scoped to a comma-values field's value followed
    # immediately by a boost.
    (
        re.compile(r"\btag:[^\s()]*,[^\s()]*\^\d"),
        (
            "DIVERGENCES.md entry 36: a comma-values field boost attaches to"
            " the whole split AndGroup in whoosh-compat but to each split"
            " term individually in whoosh, since the comma split happens at"
            " parse time here vs. analysis time there"
        ),
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
