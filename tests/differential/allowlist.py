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

Every reason string MUST also cite an explicit ``DIVERGENCES.md entry N``
somewhere in its text (even a ``whoosh-bug:``/``design:``/``out-of-scope:``
one), matching the prefix's own numbered entry: ``tests/differential/
test_allowlist_xref.py`` mechanically checks this citation exists and names
a real DIVERGENCES.md entry, in both directions (every allowlist entry cites
a real DIVERGENCES.md entry, and every DIVERGENCES.md entry that itself
claims a matching allowlist entry or corpus line actually has one). A
divergence introduced without this paperwork fails that check instead of
silently drifting out of sync.

Strict-xfail semantics (``tests/differential/test_differential.py``'s
``test_matches_oracle``): a query matching one of these entries is not just
skipped. The comparison still runs, and the test asserts the specific
divergent outcome this entry documents actually happened; an allowlisted
query whose comparison unexpectedly succeeds (the trees now match, or the
oracle stops raising) fails the suite, naming the stale entry, rather than
silently continuing to skip a divergence that no longer exists. This is what
each entry's :class:`DivergenceKind` selects between:

* :attr:`DivergenceKind.MISMATCH`: both sides parse cleanly (no
  whoosh-compat diagnostic) and the compared trees are expected to differ.
  The test asserts ``got != expected``. This is the default for nearly
  every entry below.
* :attr:`DivergenceKind.ORACLE_ERROR`: real whoosh itself raises while
  parsing this shape, so there is no oracle query object to compare at all.
  The test asserts the oracle still raises. One corpus line exercises an
  entry of this kind today: ``has_tag:"true"`` in
  ``tests/differential/corpus_docs.txt`` (the double-quoted-value-on-BOOLEAN
  crash, DIVERGENCES.md entry 38), so ``test_matches_oracle``'s
  ORACLE_ERROR branch is live code, not a provision for the future. Other
  oracle-crashing shapes (e.g. the "NOT NOT alpha" consecutive-bare-NOTs
  crash documented in DIVERGENCES.md entry 35 and the
  double-quoted-``"*"``-on-BOOLEAN crash in entry 28) deliberately have no
  differential corpus line: see those entries' own "no allowlist/corpus
  triple" notes.

A third outcome, a whoosh-compat parse *diagnostic* (DIVERGENCES.md entry
6: ``Term``/``Phrase`` values whoosh-compat can't parse, e.g. an invalid
date or number, become a structured ``ErrorLeaf`` instead of whoosh's
untyped ``error_query``/``NullQuery``), is deliberately NOT a
:class:`DivergenceKind` here. ``test_matches_oracle`` checks for a
whoosh-compat diagnostic before it ever consults this module's per-entry
:class:`DivergenceKind`, and skips uniformly (not a strict-xfail assertion)
whenever one is present, regardless of which reason (if any) also matched:
a diagnostic means the raw AST contains ``ErrorLeaf`` nodes standing in for
unparseable input, which makes a structural mismatch assertion meaningless
(there's nothing coherent to compare) whether or not the query also happens
to match one of the patterns below. Several entries here (e.g. the unquoted
multi-word natural-date-keyword one) match corpus queries that, in
practice, *always* hit the diagnostic path today rather than reaching a
MISMATCH assertion; that's fine; the entry's pattern still needs to exist to
allowlist any future query shape it matches that does NOT produce a
diagnostic. This diagnostic skip is still counted, not just silently taken:
``test_differential.py``'s ``test_diagnostic_skip_count_matches_corpus``
pins the total number of corpus queries that take it, so a parser change
that starts (or stops) diagnosing a shape is visible as a count change
instead of draining or padding the corpus silently.
"""

from __future__ import annotations

import enum
import re

from whoosh.analysis import STOP_WORDS

from tests.differential.oracle import ORACLE_REGISTRY
from whoosh_compat.fields import FieldKind

# Every registered TEXT/KEYWORD field name (canonical and alias), longest
# first so a longer name is never shadowed by a shorter prefix inside a
# regex alternation. These are exactly the kinds whose analyzer can split
# a value into multiple tokens, i.e. the fields that can exhibit the
# entry-15 Multitoken.DEFAULT divergence. Derived from the registry, never
# hand-enumerated: the fuzzers draw their field vocabulary from the same
# registry (strategies.py), so a hand-written list here is guaranteed to
# drift into a latent CI flake the moment a field is added or renamed
# (which is exactly how tag_id/custom_fields_id/viewer_id were once
# missed). test_allowlist_xref.py pins the coverage field by field.
_ANALYZER_SPLIT_FIELDS = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD)
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# A single word StandardAnalyzer analyzes to zero tokens: any of whoosh's
# own STOP_WORDS (case-insensitively, since LowercaseFilter runs before the
# stop filter) or any single word character (minsize=2 drops it). Derived
# from whoosh's live STOP_WORDS set, never hand-enumerated: an earlier
# hand-written ten-word list here silently covered less than a third of the
# real set, and the fuzzer word alphabets can draw any of them (a latent
# flake until the first unlucky draw). Longest-first so a named stopword
# matches itself rather than a shorter prefix; each use site appends its
# own boundary lookahead so a real word merely STARTING with a stopword
# ("thermal", "20th") is not mistaken for one. Shared with
# tests/emitter/result_allowlist.py's entry-23 result-level regex.
# test_allowlist_xref.py pins the coverage word by word.
ZERO_TOKEN_WORD = (
    "(?i:"
    + "|".join(re.escape(w) for w in sorted(STOP_WORDS, key=lambda w: (-len(w), w)))
    + r"|\w)"
)

# Every registered DATETIME field name (and alias), for scoping
# date-range entries. Same no-drift derivation rationale as above.
# Deliberately DATETIME only, matching strategies.DATE_FIELDS: the one
# DATE-kind field (release_date) is a whoosh-compat-only concept with no
# column in the real v2 schema, so its queries belong to entry 37's
# no-oracle-analogue paperwork, not to date-range mechanism entries.
DATE_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.DATETIME
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered comma_values field name (and alias): the fields
# CommaValuesPlugin splits at parse time, i.e. the entry-36 domain.
# Same no-drift derivation rationale as above (entry 36's regex was once
# scoped to 'tag' alone, silently missing its comma siblings).
COMMA_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.comma_values
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered TEXT field name (and alias), for entries scoped to
# analyzer-splitting TEXT values specifically.
TEXT_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.TEXT
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered BOOLEAN_EXISTS field name (and alias): the entry-33
# domain. Same no-drift derivation rationale as above (has_path was once
# missing from a hand-written list of these).
BOOL_EXISTS_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.BOOLEAN_EXISTS
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered NON-JSON field name and alias, for the unknown-field
# exclusion lookahead (a name in this set is KNOWN, so the unknown-field
# demotion mechanism cannot apply to it). JSON-kind names are deliberately
# NOT excluded: a bare JSON field name (attrs:foo) demotes to text exactly
# like an unknown field on both sides, which is entry 15's documented
# second trigger pathway and must stay claimable by this alternative.
# is_shared is appended at the use site: it is deliberately unregistered
# (entry 42) but has its own dedicated entry, which must claim it instead
# of the unknown-field alternative.
REGISTERED_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is not FieldKind.JSON
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)

# Every registered KEYWORD field name (and alias), for excluding them from
# zero-token-word entries: whoosh's KEYWORD analyzer only splits on commas,
# with no stopword/minsize filtering, so a stopword-shaped KEYWORD value is
# NOT zero-token. Derived from the registry for the same no-drift reason
# as _ANALYZER_SPLIT_FIELDS above. Shared with
# tests/emitter/result_allowlist.py.
KEYWORD_FIELDS_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        (
            name
            for spec in ORACLE_REGISTRY
            if spec.kind is FieldKind.KEYWORD
            for name in (spec.name, *spec.aliases)
        ),
        key=len,
        reverse=True,
    )
)


class DivergenceKind(enum.Enum):
    """Which strict-xfail assertion an allowlist entry's matched query
    should satisfy; see this module's docstring for the full taxonomy.
    """

    MISMATCH = "mismatch"
    ORACLE_ERROR = "oracle_error"


# (pattern, DIVERGENCES reference + short reason, strict-xfail taxonomy kind)
ALLOW: list[tuple[re.Pattern[str], str, DivergenceKind]] = [
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
        DivergenceKind.MISMATCH,
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
    # original bare "Wär*" corpus line, which has no ":" at all. The token
    # scan excludes quote characters and requires a clean token boundary,
    # so quoted-phrase content ('title:"Foo Bar?"', where the "?" is
    # analyzed away identically on both sides and the trees compare EQUAL)
    # no longer matches this entry: an overmatch there is never a real
    # entry-2 divergence, only lost fuzz coverage plus a spurious
    # strict-xfail if such a line ever landed in the corpus. Residual
    # approximation: a standalone pattern token in the MIDDLE of a
    # multi-word quoted phrase ('title:"Foo A* Bar"') still matches;
    # tightening that away needs quote-context tracking a regex cannot do.
    # design (DIVERGENCES.md entry 43, entry 2's range sibling): whoosh
    # case-folds bracket-range bounds into the AST (RangePlugin runs the
    # analyzer chain with tokenize=False over each bound); whoosh-compat's
    # TermRange keeps the case the user typed. Ordered BEFORE the entry-2
    # pattern entry so a range spelling like "title:[A* TO B]" matches this
    # reason instead of being absorbed under the Wildcard/Prefix paperwork
    # (the strict-xfail still fires either way; the point is citing the
    # divergence the query actually exhibits).
    (
        # Requires a real separator token (a bracketed non-range token
        # like title:[ABC] parses as an ordinary term on both sides and
        # must not be claimed): a case-insensitive to-token, since whoosh
        # recognizes to/To/tO as the separator too (measured), delimited
        # by whitespace or a bracket on each side, since open-ended
        # spellings put the separator against a bracket ([A TO], [to B]).
        # Excludes date fields (their bracketed ranges parse to DateRange
        # nodes on both sides and belong to entries 12/44), and hunts the
        # uppercase in the text on either side of the separator, so a
        # bound whose only uppercase is a TO-shaped run (bTO, aTO) is
        # claimed too.
        re.compile(
            rf"\b(?!(?:{DATE_FIELDS_PATTERN}):)\w+:[\[{{]"
            r"(?:[^\]}]*[A-Z][^\]}]*(?<=\s)(?i:TO)(?=[\s\]}])"
            r"|[^\]}]*(?<=[\s\[{])(?i:TO)(?=[\s\]}])[^\]}]*[A-Z])"
            r"[^\]}]*[\]}]"
        ),
        (
            "DIVERGENCES.md entry 43: whoosh case-folds TermRange bounds into"
            " the AST (analyzer chain with tokenize=False per bound);"
            " whoosh-compat keeps the raw case, and a text TermRange is"
            " unsupported at emit anyway (entry 5), so this is AST-level only"
        ),
        DivergenceKind.MISMATCH,
    ),
    (
        # Second alternative: a fielded pattern token directly abutting a
        # quote (title:Foo*'x', merged into one Wildcard by both parsers).
        # Anchored on the colon so quoted-phrase CONTENT (title:"Foo Bar?",
        # where the colon is followed by the quote, not the token) still
        # cannot match.
        re.compile(
            r"\b(?:\w+:)?(?=[^\s()\"']*[A-Z])(?=[^\s()\"']*[*?])[^\s()\"']+(?=[\s()]|$)"
            r"|\b\w+:(?=[^\s()\"']*[A-Z])(?=[^\s()\"']*[*?])[^\s()\"']+(?=[\"'])"
        ),
        (
            "DIVERGENCES.md entry 2: wildcard/prefix pattern case is folded into"
            " the AST by whoosh (LowercaseFilter runs even with tokenize=False)"
            " but only at emit time by whoosh-compat (FieldSpec.pattern_normalizer)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # DIVERGENCES.md entry 17 (design) documents that whoosh-compat's
    # CommaValuesPlugin treats a *quoted* comma-values field value as a
    # literal, unlike real whoosh (whose KEYWORD(commas=True) analyzer always
    # splits on commas at analysis time, quoted or not). There is no
    # allowlist entry for it here, though: verified directly (re-checked
    # while adding this module's strict-xfail taxonomy) that the raw,
    # pre-analysis parse trees genuinely differ (whoosh-compat keeps
    # "foo,bar" as one Term; the oracle already has two), but
    # oracle.analyze_ast forward-analyzes both sides through the same
    # comma-splitting KEYWORD analyzer before comparison (mirroring
    # TantivyEmitter's own emit-time re-analysis, see DIVERGENCES.md entry
    # 16), which collapses that difference before the comparison this test
    # module runs ever sees it: "tag:'foo,bar'" structurally matches at this
    # comparison layer, even though it is still a real, separately
    # unit-tested divergence in the raw parser output and worth keeping
    # documented. Allowlisting it here would make it a stale entry by
    # construction (the strict-xfail assertion would fail immediately,
    # since the trees actually match), so it stays out of ALLOW and is
    # compared normally.
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
        DivergenceKind.MISMATCH,
    ),
    (
        re.compile(r"\bnotes\.(user|note)\b"),
        (
            "DIVERGENCES.md entry 14 (design): dot-inclusive FieldsPlugin"
            " tagger ([\\w.]+: vs whoosh's \\w+:) tags an unregistered dotted"
            " name differently than whoosh's tagger even though neither side"
            " has the field registered"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 14, extended): "attrs" is a JSON field
    # genuinely registered in ORACLE_REGISTRY (added specifically so
    # strategies.py can generate real JSON-subpath pattern/existence/quoted-
    # value queries, see oracle.py's comment next to the FieldSpec), but real
    # v2 whoosh has no such field at all, so a query addressing it always
    # structurally diverges from the oracle, same mechanism as the
    # notes./custom_fields. entries above. The regex covers both the
    # bare-JSON-name case ("attrs:foo", which actually still matches the
    # oracle: FieldRegistry.make_ref demotes it identically to a genuinely
    # unregistered field, verified directly) implicitly not matching here
    # since it has no dot, and every "attrs.<subpath>:" shape, which does.
    (
        re.compile(r"\battrs\.(user|note|value|name)\b"),
        (
            "DIVERGENCES.md entry 14 (design, extended): dot-inclusive"
            " FieldsPlugin tagger tags 'attrs.<subpath>:' differently than"
            " whoosh's non-dot-aware tagger; 'attrs' is registered as a JSON"
            " field only on whoosh-compat's side (added purely to reach"
            " generator vocabulary), real whoosh has no such field at all"
        ),
        DivergenceKind.MISMATCH,
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
    # whoosh-bug (DIVERGENCES.md entry 44): a date range typed with an
    # exclusive bracket. whoosh's DateRangeNode never forwards
    # startexcl/endexcl (always inclusive-both, the same plumbing oversight
    # class as entry 3's boost drop); whoosh-compat honors the typed
    # brackets on exact bounds. Ordered BEFORE the entry-12 date-range
    # entry so the exclusive spelling cites this divergence rather than
    # being absorbed under the tz-bypass paperwork (both are MISMATCH kind;
    # the ordering only affects citation accuracy). Scoped to a bracketed
    # range on a registered date field where either bracket is the
    # exclusive one.
    (
        re.compile(
            rf"\b(?:{DATE_FIELDS_PATTERN}):"
            r"(?:\{[^\]}]*[\]}]|\[[^\]}]*\})"
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 44): whoosh's DateRangeNode"
            " drops typed {}/exclusivity flags (always inclusive-both);"
            " whoosh-compat honors them on exact bounds"
        ),
        DivergenceKind.MISMATCH,
    ),
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
            "whoosh-bug (DIVERGENCES.md entry 12): LocalDateParser's"
            " tz-reversal override doesn't reach range bounds (range_to_dt"
            " uses the bare grammar's date_from, not the override);"
            " whoosh-compat applies tz conversion uniformly instead of"
            " reproducing the bug"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated
    # datetime value ('2026-08-04T10:30:00', with or without the Z
    # designator). whoosh's grammar has no T separator and its fallback
    # chain bottoms out in _NullQuery (matches nothing); whoosh-compat
    # parses the correct DateRange. Ordered BEFORE the entry-18 bare-ISO
    # entry, whose regex also matches these strings but whose
    # numerically-correct field-self-parse mechanism does not describe
    # the _NullQuery outcome. The quote is REQUIRED: the bare unquoted
    # spelling colon-tokenizes differently in whoosh (a partial
    # NumericRange plus leftover terms, not _NullQuery), so this reason
    # string would be false for it.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'\d{{4}}-\d\d-\d\d[Tt]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a correct DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 54, superseding DIVERGENCES.md
    # entry 49): the BARE unquoted sibling of the entry-48 spelling.
    # Colon tokenization splits the value before any date grammar runs,
    # leaving the date field a fragment cut off mid-token ("2026-08-").
    # Real whoosh swallows the dangling separator and reads the fragment
    # as a whole, shorter date (the August-2026 month window), ANDing the
    # rest of the timestamp on as free text with no diagnostic;
    # whoosh-compat rejects the half-consumed value as BAD_DATE. Every
    # COLON-BEARING query this pattern matches today therefore takes the
    # entry-6 diagnostic skip before this entry's DivergenceKind is ever
    # consulted. The pattern deliberately also admits the colon-less
    # spelling ("added:2026-08-04T10", a T-fused value with no clock
    # time), which nothing splits and which therefore diagnoses nothing:
    # it parses to an hour-precision DateRange against whoosh's
    # _NullQuery, entry 48's compat-favorable shape without the quotes.
    # The reason string covers both faces because either can reach it (no
    # corpus line uses the colon-less spelling today, so which face fires
    # is currently theoretical, but a wrong reason is not). Ordered BEFORE the
    # entry-18 bare-ISO entry, whose fully-parses-numerically-correct
    # prose describes neither the truncation nor the rejection. No quote
    # after the colon: the quoted spellings belong to entries 48 (single)
    # and 45 (double). The day group is optional: the no-day spelling
    # ("2026-08T10:30") leaves the fragment "2026-" by the same
    # mechanism, which whoosh reads as the whole YEAR.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):\d{{4}}-\d\d(?:-\d\d)?[Tt]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 54, superseding DIVERGENCES.md"
            " entry 49): a bare unquoted T-separated datetime value. With a"
            " clock time, its colons cut the value mid-token: whoosh silently"
            " reads the fragment as a shorter whole date and ANDs the"
            " remainder on as text, whoosh-compat diagnoses BAD_DATE. Without"
            " one there is nothing to cut: whoosh reads the whole value as"
            " _NullQuery, whoosh-compat as the DateRange meant (entry 48's"
            " shape, unquoted)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 50): a NO-separator T-fused value
    # ("2026T10", bare or single-quoted, optionally with a colon-split
    # day token as in "2026T10:30"). whoosh's grammar cannot read it at
    # all and bottoms out in _NullQuery; whoosh-compat's T-separator
    # grammar reads year-T-month (joining a colon-split trailing token
    # into a day). T directly after the year keeps this disjoint from
    # entries 48/49 (which require dashes); the double-quoted spelling
    # belongs to entry 45's crash cell. Ordered before the entry-15
    # unknown-field-demotion pattern, which would otherwise mis-claim
    # the inner-colon spelling by reading "2026T10" as an unknown FIELD
    # named 2026T10 with value 30.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}[Tt]\d"),
        (
            "whoosh-bug (DIVERGENCES.md entry 50): a no-separator T-fused"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a year-T-month DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
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
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}[-. /]\d"),
        (
            "DIVERGENCES.md entry 18: bare separated-ISO date value parses"
            " correctly on both sides but via a different mechanism/AST"
            " shape (whoosh's ErrorNode-falls-back-to-field.parse_query vs"
            " whoosh-compat's single DateParserPlugin grammar path)"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 45): the double-quoted sibling of
    # the entry-18 spellings. The ErrorNode fallback wraps a PhraseNode
    # here, so whoosh parses query.Phrase('created', ['2020-01-01']),
    # which raises QueryError at search time (a DATETIME field has no
    # positions); whoosh-compat parses the quoted value into the correct
    # day/month-period DateRange. Both sides parse cleanly, so the AST
    # comparison is an ordinary MISMATCH (Phrase vs DateRange).
    (
        re.compile(rf'\b(?:{DATE_FIELDS_PATTERN}):"\d{{4}}(?:[-. /]|[Tt])\d[^"]*"'),
        (
            "whoosh-bug (DIVERGENCES.md entry 45): double-quoted"
            " separated-ISO date parses to a search-time-crashing Phrase in"
            " whoosh but a correct day/month-period DateRange in whoosh-compat"
        ),
        DivergenceKind.MISMATCH,
    ),
    # NOTE: the unquoted multi-word date keywords (`created:previous month`)
    # used to be allowlisted here, because whoosh-compat's grammar only
    # recognized them quoted while the oracle harness replicated paperless's
    # app-level rewrite. whoosh-compat's date grammar now joins those six
    # phrases itself (DateParserPlugin.do_date_phrases), so both sides agree
    # and there is nothing left to allowlist: the remaining difference is
    # against *stock* whoosh, which cannot parse them in any spelling and so
    # is not reachable through this harness (DIVERGENCES.md entry 19).
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
        re.compile(r"(?:^|(?<=[\s(:]))\*(?:\^[\d.]+)?(?=$|[\s)])"),
        (
            "DIVERGENCES.md entry 20: bare field:* (or unfielded *) simplifies to"
            " Every(field) in whoosh-compat vs a literal Wildcard('*') in whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 13): real whoosh's WildcardPlugin.do_wildcards
    # (and query.terms.Wildcard.normalize(), same root cause) only tests a
    # trailing-star pattern for "*"/"?" before folding it to a Prefix:
    # despite SPECIAL_CHARS = "*?[" including "[": so a pattern like
    # "202[0-3]*" folds to Prefix('title', '202[0-3]') and silently loses the
    # character class instead of staying a Wildcard. whoosh-compat fixed the
    # fold check (parser/plugins.py's folds_to_prefix, shared by both sites
    # that perform this fold: do_wildcards and QueryParser.wildcard_query) to
    # also test for "[", so it keeps the full Wildcard pattern. Not reproduced:
    # whoosh-compat's own trailing-star-with-bracket corpus line
    # (title:202[0-3]*) is intentionally allowlisted here rather than
    # matched against the (buggy) oracle tree. Broadened from the single
    # literal corpus string to the general shape (any field/value with a
    # bracket class immediately followed by a trailing "*"): the root cause
    # is a check that's missing for every field/pattern, not just this one
    # corpus line, as the grammar-aware fuzzer confirmed (title:0[0-0]*).
    (
        # The field prefix is optional ((?:\w+:)?): broadened after the
        # expanded generator's dedicated degenerate/reversed-class atom
        # (strategies._degenerate_wildcard_atom) produced an *unfielded*,
        # multifield-expanded instance of this same pattern
        # ("x[z-a]*", no "field:" text anywhere in the query at all): the
        # root cause (real whoosh's SPECIAL_CHARS/fold-check omitting "[")
        # applies identically whether or not the pattern is fielded, and
        # verified directly against the oracle both ways.
        re.compile(r"\b(?:\w+:)?[^\s()]*\[[^\]]*\]\*(?:\^[\d.]+)?(?:\s|$|\))"),
        "whoosh-bug (DIVERGENCES.md entry 13): Wildcard.normalize() bracket fold drops the character class on a trailing-star pattern",
        DivergenceKind.MISMATCH,
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
    # as entry 23. This entry is scoped to a NOT wrapping (possibly
    # through empty-group and nested-NOT noise) a known-zero-token-word
    # value on any field (the ZERO_TOKEN_WORD fragment, derived from
    # whoosh's own STOP_WORDS set at the top of this module); the broader
    # co-occurrence entry directly below catches the same family in
    # arbitrary scaffolding this prefix cannot reach.
    # The value may be a CHAIN of zero-token pieces joined by the
    # characters StandardAnalyzer splits on (dash/comma/slash): every
    # piece analyzes away, so the-of, the,x, and a-b are zero-token too
    # (measured diverging), while a chain containing one surviving piece
    # (the-invoice) is rescued by it and compares equal, which the
    # per-piece boundary rejects via backtracking exhaustion.
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
            # The prefix tolerates empty-group and nested-NOT noise
            # between the NOT and the fielded value ("NOT (() title:the)",
            # "0 NOT (NOT (() title:the))", found by a deep fuzz soak):
            # each unit is an open paren, a complete empty group, or a
            # further NOT keyword, so a real intervening term still blocks
            # the match.
            r"\bNOT\s*(?:\(\)\s*|\(\s*|NOT\s+)*"
            rf"(?!(?:{KEYWORD_FIELDS_PATTERN}):)\w+:"
            rf"{ZERO_TOKEN_WORD}(?:[-,/]{ZERO_TOKEN_WORD})*[-,/]?(?![\w.,/-])"
        ),
        (
            "DIVERGENCES.md entry 23: NOT of a zero-token term/phrase reaches the"
            " same emit-time divergence at the AST-comparison layer"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entries 23 and 40 jointly): the composed
    # family of the two entries above: a NOT anywhere in the query, an
    # empty group anywhere, and a zero-token word anywhere, in arbitrary
    # scaffolding ("((0) OR (NOT ((()) AND (title:the))))", found by a
    # deep fuzz soak after three narrower spellings of the same family
    # each leaked past the prefix-based entry-23 regex). The three
    # co-occurrence lookaheads claim the whole family at once instead of
    # chasing spellings. The zero-token word must not be an (uppercase)
    # operator keyword, so the corpus line "NOT ()" (single-level, both
    # sides agree, entry 40's own excluded shape) stays unclaimed; per
    # the strict-xfail convention, any corpus line matching this entry
    # must be chosen to genuinely diverge.
    (
        re.compile(
            r"(?=.*\bNOT\b)(?=.*\(\))"
            rf"(?=.*(?:[\s(:]|^)(?!(?:NOT|AND|OR|ANDNOT|ANDMAYBE|REQUIRE|TO)\b)"
            rf"{ZERO_TOKEN_WORD}(?![\w.,/-]))"
        ),
        (
            "DIVERGENCES.md entry 23 (with entry 40's empty-group rule): a"
            " NOT, an empty group, and a zero-token word co-occurring in"
            " arbitrary scaffolding reach the composed"
            " collapsed-empty/zero-token divergence family"
        ),
        DivergenceKind.MISMATCH,
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
    # zero-token words (the shared ZERO_TOKEN_WORD fragment, same
    # derivation as the entry-23 allowlist entry above). The
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
            rf'"{ZERO_TOKEN_WORD}(?=[\s"])'
            rf'(?:\s+{ZERO_TOKEN_WORD}(?=[\s"]))*"'
        ),
        (
            "DIVERGENCES.md entry 24: an all-zero-token quoted phrase parses to"
            " a real empty-words Phrase object in whoosh (tokenized at parse"
            " time) but is dropped entirely by whoosh-compat's emit-time"
            " analysis"
        ),
        DivergenceKind.MISMATCH,
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
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 27, found by the property-based fuzzer
    # during the empty-group-drop work): ANDNOT/ANDMAYBE/REQUIRE whose positive/
    # required/scored side analyzes to zero tokens, nested inside further
    # grouping alongside a sibling clause, poisons the whole enclosing And
    # on whoosh-compat's side but real whoosh drops it. Both sides agree
    # the degenerate AndNot/AndMaybe/Require itself resolves to "match
    # nothing"; the divergence is only in whether that Nothing propagates
    # through an *enclosing* And (whoosh-compat's ast.normalize() "Nothing
    # propagates through And" rule, deliberately kept as-is)
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
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 33): a whitespace-padded quoted value on a
    # BOOLEAN_EXISTS field reads False in whoosh-compat (strips before the
    # trues/falses membership check) but True in real whoosh
    # (BOOLEAN._obj_to_bool checks the *unstripped* text, then falls through
    # to bool(qstring), which is True for any non-empty string). Scoped to a
    # single-quoted BOOLEAN_EXISTS value that has leading or trailing
    # whitespace inside the quotes; a quoted empty value ("''") is
    # deliberately excluded, since that shape no longer diverges (both
    # sides now agree it's False) and is compared normally instead. Field
    # alternation broadened from "has_tag" only to every registered
    # BOOLEAN_EXISTS field after the expanded generator's
    # _bool_exists_quoted_atom (which, unlike the old has_tag-only manual
    # corpus line, draws from all of BOOL_EXISTS_FIELDS) found the identical
    # mismatch on "has_correspondent" and confirmed directly it applies
    # uniformly to has_type/has_path/has_custom_fields/has_owner too (the
    # acceptance-layer result property's grammar-aware generator, which
    # also draws from the same BOOL_EXISTS_FIELDS pool, later found the
    # same mismatch reachable on "has_path" specifically, now listed in
    # the alternation below like its siblings): the
    # root cause (term_query's strip-before-check vs BOOLEAN._obj_to_bool's
    # unstripped-then-bool(qstring) fallback) is the same code path for
    # every BOOLEAN_EXISTS field, not something specific to has_tag.
    (
        re.compile(
            rf"\b(?:{BOOL_EXISTS_FIELDS_PATTERN}):"
            r"(?:'\s+\S.*'|'.*\S\s+'|'\s+')"
        ),
        (
            "DIVERGENCES.md entry 33: a whitespace-padded quoted"
            " BOOLEAN_EXISTS value reads False in whoosh-compat (stripped"
            " before the trues/falses check) but True in whoosh"
            " (unstripped check falls through to bool(qstring))"
        ),
        DivergenceKind.MISMATCH,
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
    # not a whoosh bug. Scoped to any comma-values field's value followed
    # immediately by a boost (the field alternation is derived from the
    # registry's comma_values flags; a hand-written 'tag'-only scope once
    # silently missed tag_id/custom_fields_id/viewer_id).
    (
        re.compile(rf"\b(?:{COMMA_FIELDS_PATTERN}):[^\s()]*,[^\s()]*\^\d"),
        (
            "DIVERGENCES.md entry 36: a comma-values field boost attaches to"
            " the whole split AndGroup in whoosh-compat but to each split"
            " term individually in whoosh, since the comma split happens at"
            " parse time here vs. analysis time there"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 46): entry 36's analyzer-split sibling.
    # A boosted TEXT value the analyzer splits (title:foo-bar^2) attaches
    # the boost to the whole split group in whoosh-compat (analyze() splits
    # inside the already-bound Boosted wrapper) but to each split term in
    # whoosh (its analyzer splits after the boost bound to the unsplit
    # term). Matched documents identical either way; AST-shape only.
    (
        # Separator class is dash/comma/slash, the characters
        # StandardAnalyzer actually splits a TEXT value on (measured). A
        # single dot between word characters is deliberately EXCLUDED:
        # the analyzer keeps foo.bar as one token, so a dotted-only
        # boosted value never splits, never diverges, and claiming it
        # would make the first dotted corpus line fail as a stale entry.
        re.compile(rf"\b(?:{TEXT_FIELDS_PATTERN}):[^\s()]*\w[-,/]\w[^\s()]*\^\d"),
        (
            "DIVERGENCES.md entry 46: a boost on an analyzer-split TEXT"
            " value attaches to the whole split group in whoosh-compat but"
            " to each split term individually in whoosh"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 37): "release_date" is a date_only field
    # registered only in ORACLE_REGISTRY (added purely so the generator can
    # reach "time-bearing value on a date-only field" vocabulary,
    # strategies._date_only_atom): real v2 whoosh has no date-vs-datetime
    # distinction and no such field, so every query against it structurally
    # diverges from the oracle's default-multifield-unknown-field expansion,
    # the same "whoosh-compat-only concept" shape as entry 14's JSON fields.
    # Scoped to the field name itself (any value): confirmed directly that a
    # bare date, a time-bearing bare value, and a time-bearing range all
    # mismatch identically, since the oracle never recognizes the field at
    # all regardless of what value follows it.
    (
        re.compile(r"\brelease_date:"),
        (
            "DIVERGENCES.md entry 37 (design): date_only is a"
            " whoosh-compat-only concept with no whoosh equivalent; the"
            " field itself is unregistered on the real v2 whoosh schema"
        ),
        DivergenceKind.MISMATCH,
    ),
    # whoosh-bug (DIVERGENCES.md entry 38): real whoosh's BOOLEAN field has
    # no analyzer at all (whoosh.fields.BOOLEAN.__init__ never sets one), but
    # PhrasePlugin.PhraseNode.query unconditionally tries to tokenize a
    # double-quoted value through the field's analyzer before building a
    # Phrase query; confirmed directly that this raises
    # "<class 'whoosh.fields.BOOLEAN'> field has no analyzer" for *every*
    # double-quoted value on a BOOLEAN_EXISTS field (empty, valid, padded,
    # even a pattern-shaped value), not just a specific one. whoosh-compat
    # has no equivalent crash: a double-quoted value on one of these fields
    # parses to an ordinary ast.Phrase and is coerced to a boolean at emit
    # time (visit_phrase), same as documented for entry 8. Not reproduced:
    # a real whoosh limitation, not intended semantics.
    (
        re.compile(
            r"\b(?:has_correspondent|has_tag|has_type|has_path|has_custom_fields|has_owner):\""
        ),
        (
            "whoosh-bug (DIVERGENCES.md entry 38): BOOLEAN fields have no"
            " analyzer in real whoosh, so PhrasePlugin crashes while"
            " tokenizing any double-quoted value on one; whoosh-compat"
            " parses it as an ordinary Phrase and coerces at emit time"
            " instead of reproducing the crash"
        ),
        DivergenceKind.ORACLE_ERROR,
    ),
    # design (DIVERGENCES.md entry 39, found by the expanded generator's
    # _numeric_atom): real v2 whoosh's NUMERIC fields all default to
    # bits=32 (oracle_schema() never passes bits= explicitly), so a value at
    # or above each field's real 32-bit ceiling silently fails to parse on
    # the oracle side, while whoosh-compat's U64 kind validates against the
    # full 64-bit domain (tantivy's actual column type). That ceiling isn't
    # uniform: asn/num_notes/custom_field_count pass signed=False (real max
    # 2**32 - 1 = 4294967295), while id/correspondent_id/type_id/path_id/
    # owner_id/page_count leave signed at its library default of True (real
    # max only 2**31 - 1 = 2147483647); confirmed directly per field via
    # _SCHEMA rather than assumed. Scoped to the literal values the
    # generator actually produces one past each boundary (2**32 for the
    # unsigned fields, 2**31 for the signed ones), plus 2**64 - 1 (the u64
    # domain's own ceiling, always out of range regardless of signedness);
    # confirmed directly that each field's own exact max (4294967295 for the
    # unsigned three, 2147483647 for the rest) is NOT included here since it
    # parses identically on both sides.
    (
        re.compile(
            r"\b(?:asn|num_notes|custom_field_count):'?\"?4294967296\b"
            r"|\b(?:id|correspondent_id|type_id|path_id|owner_id|page_count):"
            r"'?\"?2147483648\b"
            r"|\b(?:id|asn|correspondent_id|type_id|path_id|owner_id|num_notes"
            r"|custom_field_count|page_count):'?\"?18446744073709551615\b"
        ),
        (
            "DIVERGENCES.md entry 39 (design): whoosh-compat's U64 domain is"
            " the full 64-bit range (tantivy's actual column type); real v2"
            " whoosh's NUMERIC fields default to bits=32 (and, for most of"
            " them, signed=True, halving the usable range again) and"
            " silently fail to parse a value at or above that real ceiling"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 15, now confirmed at the AST-comparison
    # layer too, not just result-level): analyze() resolves a DEFAULT-
    # multitoken field's combinator from the term's *actual* enclosing group
    # (AND vs OR), which real whoosh never does (it always uses the parser's
    # fixed default group, AND here). Multifield expansion of an unfielded
    # (or demoted-unknown-field) value always builds a fresh Or(field1:...,
    # field2:..., ...) at the point the value appears, so every default
    # field's own per-field combinator resolves against that Or
    # unconditionally, regardless of what encloses the expansion elsewhere in
    # the query: any such value that survives its own field's analyzer as
    # two or more tokens (for at least one default TEXT/KEYWORD field)
    # diverges. Two independent textual shapes reach this: a bare (unfielded)
    # word containing an internal separator between two >=2-character runs
    # (StandardAnalyzer splits on the separator; each half needs to be at
    # least minsize=2 to plausibly survive, though this is a length
    # approximation, not an exact stopword-aware simulation, see below), and
    # an unregistered ("unknown") field name followed by a colon and a value,
    # which whoosh-compat's FieldsPlugin merges into one literal string
    # (fieldname included) the same way real whoosh's own unknown-field
    # demotion does, tokenizing on the colon boundary the same way a dash or
    # dot would. Both alternatives require each side of the internal
    # separator to be at least two characters, confirmed directly
    # ("zzz:x"/"a:foobar", where the one-character half is dropped by
    # StandardAnalyzer's minsize=2 and only one token survives per field, do
    # NOT diverge) as a practical proxy for "plausibly survives analysis".
    # One measured exception to the two-character proxy: İ (U+0130, the
    # only character in Unicode whose str.lower() expands to two
    # codepoints, pinned by test_allowlist_xref's derivation test), whose
    # single-character value survives minsize after lowercasing and
    # genuinely diverges ("zzz:İ", found by a deep fuzz soak), so it is
    # admitted alongside the two-character forms. The proxy remains one,
    # not a byte-for-byte simulation of StandardAnalyzer's stopword list;
    # the differential-triage skill's normal iterate-on-fuzzer-findings
    # workflow applies if a future fuzz run finds a shape (e.g. an actual
    # English stopword landing on one side) this approximation misses.
    # Known field names/aliases are excluded from the unknown-field
    # alternative so an explicitly, correctly fielded value (which only
    # diverges when nested inside a genuine user-written OR, a narrower,
    # context-dependent case covered by the next entry) isn't wrongly
    # swept in here.
    (
        re.compile(
            r"(?:^|(?<=[\s(]))(?:\w{2,}|İ)[-.](?:\w{2,}|İ)(?=[\s)]|$)"
            rf"|\b(?!(?:{REGISTERED_FIELDS_PATTERN}|is_shared)\b)"
            r"\w{2,}:(?:[^\s():]{2,}|İ)"
        ),
        (
            "DIVERGENCES.md entry 15: an unfielded or unknown-field-demoted"
            " value that survives its field's analyzer as 2+ tokens resolves"
            " Multitoken.DEFAULT against the multifield expansion's Or"
            " context in whoosh-compat, but against whoosh's fixed AND"
            " default in real whoosh, now confirmed reachable at the AST-"
            " comparison layer via analyze()"
        ),
        DivergenceKind.MISMATCH,
    ),
    # design (DIVERGENCES.md entry 15, the correctly-fielded sibling of the
    # previous entry): a KNOWN TEXT/KEYWORD field's multi-token value
    # sitting inside a user-written OR resolves Multitoken.DEFAULT against
    # that Or context in whoosh-compat but against whoosh's fixed AND
    # default in real whoosh. A degenerate parenthesized wrapper does not
    # shield the term: analyze() normalizes its input first (making it
    # insensitive to whether the caller pre-normalized), so the singleton
    # group "(title:00-000)" collapses and the term resolves against the
    # enclosing OR, exactly as the production emitter always has (the
    # emitter normalizes before analyzing; only the harness's raw-tree
    # path used to see the un-collapsed wrapper and miss this shape).
    # Scope: the query must contain an OR, and a known TEXT/KEYWORD field
    # (the kinds whose analyzer can split a value) with a value containing
    # an internal [-.,] separator between two word runs. The runs may be
    # single characters: unlike StandardAnalyzer's minsize=2 (the previous
    # entry's approximation for TEXT fields), the KEYWORD analyzers split
    # on commas without a minimum token length, so a quoted "tag:'0,00'"
    # genuinely yields two surviving tokens. This can overmatch a query
    # whose fielded value sits in an AND context elsewhere in the same
    # OR-bearing query, or a TEXT-field value whose 1-char half gets
    # dropped by minsize; per the strict-xfail convention, corpus lines
    # matching this entry must be chosen to genuinely diverge.
    (
        re.compile(
            r"^(?=.*\bOR\b)"
            rf"(?=.*\b(?:{_ANALYZER_SPLIT_FIELDS})"
            r":['\"]?\w+[-.,]\w+)"
        ),
        (
            "DIVERGENCES.md entry 15: a known TEXT/KEYWORD field's"
            " multi-token value inside a user-written OR resolves"
            " Multitoken.DEFAULT against the enclosing Or context in"
            " whoosh-compat, but against whoosh's fixed AND default in real"
            " whoosh; a singleton paren wrapper does not shield the term,"
            " since analyze() normalizes before resolving context"
        ),
        DivergenceKind.MISMATCH,
    ),
    # out-of-scope (DIVERGENCES.md entry 42): the v2 whoosh schema's
    # is_shared BOOLEAN column is deliberately not a registered field
    # (paperless's tantivy backend does permission filtering outside
    # whoosh-compat, and its public search surface does not expose
    # is_shared), so the oracle parses is_shared:<value> as a typed
    # boolean Term while whoosh-compat demotes it as an unknown field.
    # This is a known-to-oracle-only shape, distinct from entry 15's
    # both-sides-unknown multitoken class, whose regex above excludes
    # is_shared for exactly that reason.
    (
        re.compile(r"\bis_shared:"),
        (
            "DIVERGENCES.md entry 42: is_shared is deliberately not a"
            " registered field (v2's BOOLEAN permission-bookkeeping column;"
            " paperless's tantivy backend filters permissions outside"
            " whoosh-compat), so the oracle parses a typed boolean Term"
            " while whoosh-compat demotes the unknown field to text"
        ),
        DivergenceKind.MISMATCH,
    ),
]


def allowed_reason(query: str) -> str | None:
    """The reference string of the first allowlist entry matching ``query``,
    or ``None`` if no entry matches (i.e. the query must be compared).

    Returning the specific matched reason (rather than a bare bool) lets
    callers report a distinct, auditable skip reason per query instead of a
    single catch-all "allowlisted" message.
    """

    for pattern, reason, _kind in ALLOW:
        if pattern.search(query):
            return reason
    return None


def allowed_entry(query: str) -> tuple[str, DivergenceKind] | None:
    """Like :func:`allowed_reason`, but also returns the matched entry's
    :class:`DivergenceKind`, for callers implementing this module's
    strict-xfail semantics (``test_differential.py``'s ``test_matches_oracle``).
    Returns ``None`` if no entry matches.
    """

    for pattern, reason, kind in ALLOW:
        if pattern.search(query):
            return reason, kind
    return None


def allowed(query: str) -> bool:
    """True if ``query`` matches an intended-divergence pattern and should be
    skipped rather than compared structurally.
    """

    return allowed_reason(query) is not None
