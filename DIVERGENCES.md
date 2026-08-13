# DIVERGENCES

whoosh-compat's parity bar is whoosh's intended semantics, not its defects:
where real whoosh has a confirmed bug, whoosh-compat does not reproduce it.
Where whoosh-compat makes a deliberate design choice with no whoosh
equivalent, or restores documented behavior that a naive string-translation
migration to another search engine would otherwise silently drop, that is
recorded here too rather than left implicit.

Entries 1-11 were identified while designing the library, before any code
existed. Entries 12+ were found later, during triage of the differential
(AST-comparison, `tests/differential/`) and end-to-end acceptance (full
parse, emit, search, `tests/emitter/test_acceptance_e2e.py`) test suites.

## From whoosh

1. Invalid dates/numbers yield diagnostics the host may turn into an HTTP
   400 response (real whoosh: silent empty results).
2. Wildcard/prefix patterns are case-folded via `pattern_normalizer` (real
   whoosh matched raw index terms, so `Entwä*` with a capital E failed
   there too, this is a fix, not a regression).
3. Date-node boosts are preserved (whoosh silently dropped them).
4. Stopwords are not removed (a policy choice: whoosh-compat takes no
   position on stopwords, it uses whatever tokens the host's `analyzer`
   returns); this affects ranking and makes stopwords searchable, not
   matching correctness under implicit AND.
5. Text-field ranges are parseable but unsupported at emit time (a current
   limitation: tantivy-py has no programmatic text-range API); they worked
   in whoosh.
6. Structured `ErrorLeaf`/diagnostics replace whoosh's `error_query`
   NullQuery wrappers.

## From a naive string-translation migration

The entries below describe divergences from what a naive
query-string-to-query-string translation layer would produce if it fed
whoosh-style queries straight into another search engine's own string
parser (the approach paperless-ngx's production code used, and that this
library exists to replace, before adopting whoosh-compat's typed
parse-then-emit pipeline).

7. Implicit AND is restored between bare terms (a naive translation ends up
   implicit OR once handed to a downstream string parser, a silent
   regression versus whoosh's documented behavior).
8. Attached `-foo` searches for `foo` (a naive translation lets a
   downstream string parser read it as a MustNot, which then matches
   nothing).
9. Field boosts apply only to multifield-expansion nodes, not explicitly
   fielded terms (a naive translation boosts both).
10. Open-ended date ranges are true open bounds (a naive translation needs
    sentinel dates like `0001`/`9999` to fake an open bound).
11. Nested all-negative boolean groups work (a downstream string parser
    that inherits [quickwit-oss/tantivy#3025](https://github.com/quickwit-oss/tantivy/issues/3025)
    returns zero hits for these instead).

## Additional entries (from differential and acceptance testing)

12. **Date-range timezone bypass (whoosh bug, not reproduced).** Real
    whoosh's `DateParserPlugin.range_to_dt`
    (`whoosh/qparser/dateparse.py`) calls
    `self.dateparser.get_parser().date_from(...)`, the bare grammar
    object's `date_from`, not the configured dateparser's own `date_from`
    override. So a `LocalDateParser.date_from` override that reverses a
    local-timezone offset back to UTC (exactly what paperless-ngx's own
    `LocalDateParser` does, and what the oracle harness clones as
    `oracle.LocalDateParser`) never actually runs for bracketed ranges,
    only for single/keyword values (`DateParserPlugin.text_to_dt`, which
    does call `self.dateparser.date_from`). Naive range bounds are
    therefore taken as literal UTC in real whoosh with no local-tz shift
    applied at all, a wiring defect, not an intended design choice.
    whoosh-compat's `DateParserPlugin.range_to_node`
    (`src/whoosh_compat/parser/dateparse.py`) applies the same tz
    conversion uniformly to both single values and range bounds instead of
    reproducing the bug.

    Test references: `tests/differential/allowlist.py`'s
    `created|modified|added:[\[{]` entry (covers every bracketed range,
    inclusive or exclusive open bracket, on a DATE/DATETIME field in the
    differential corpus, `tests/differential/corpus_*.txt`, broadened from
    `[`-only after the grammar-aware fuzzer generated an exclusive-bracket
    case that hit the identical bypass); confirmed to *not* change actual
    search results for this project's small acceptance fixture in
    `tests/emitter/test_acceptance_e2e.py::test_scenario_equal[lowercase-to-open-range]`
    (see that test module's docstring for why an AST-level divergence
    doesn't always imply a different final result set).

    The same missing-`ToEnd` root cause has a second, distinct symptom
    beyond the tz bypass: `range_to_dt` accepts whatever prefix of a bound
    string the first successful `Choice` alternative happens to consume,
    instead of requiring the whole bound to match. For a separated bound
    like `2020-06-15`, the `bundle` grammar's `datetime` Bag partial-matches
    just the bare year `2020` (its `dmy` sub-choice's lone `self.year`
    fallback, since the following `-` satisfies `year`'s
    `(?=(\W|$))` lookahead) and `range_to_dt` silently accepts that partial
    match instead of failing, so `created:[2020-06-15 TO 2020-06-20]`
    collapses to the whole of 2020 in real whoosh (confirmed directly
    against the oracle, see `tests/differential/corpus_paperless.txt`'s
    comment on that corpus line). whoosh-compat's `range_to_node` now calls
    `self.dateparser.date_from` (the `ToEnd`-wrapped form `text_to_node`
    already used) instead of the bare grammar object's `date_from`, so a
    partial match on either bound correctly fails (surfacing a `BAD_DATE`
    diagnostic) rather than silently collapsing to whatever coarser
    precision the first alternative happened to match. Combined with a
    separate whoosh-compat bug fix to the `bundle` Choice's alternative
    order (`simple` is now tried before `datetime`, so a partial `dmy`
    year-only match can no longer starve the separated-numeric grammar of
    input it needs; see `parser/dateparse.py`'s `English.__init__`), a
    separated bound like `2020-06-15` now matches `simple` in full on the
    first try, so this case parses correctly rather than erroring: see
    `tests/test_parser_dates.py::test_range_bounds_do_not_collapse_to_year`.

13. **`Wildcard.normalize()` bracket fold drops character classes (whoosh
    bug, not reproduced).** Real whoosh's `SPECIAL_CHARS` constant
    (`whoosh/query/terms.py`) is `"*?["`, but `Wildcard.normalize()` (same
    file), and independently `WildcardPlugin.do_wildcards`
    (`whoosh/qparser/plugins.py`), only ever *test* for `"*"`/`"?"` before
    folding a trailing-star pattern down to a `Prefix`. A pattern like
    `202[0-3]*` (paperless-ngx issue
    [#13568](https://github.com/paperless-ngx/paperless-ngx/issues/13568):
    saved views built around bracket-class year ranges) therefore folds to
    `Prefix('title', '202[0-3]')`, silently destroying the character class
    instead of keeping the full wildcard pattern. whoosh-compat fixed the
    fold check in both sites that perform this optimization,
    `parser/default.py`'s `_TRAILING_STAR_RE` and `parser/plugins.py`'s
    `do_wildcards`, to also check for `"["`, so a trailing-star-with-bracket
    pattern stays a `Wildcard` instead of losing its class body.

    Test references: `tests/differential/corpus_docs.txt`'s
    `title:202[0-3]*` line plus its matching `tests/differential/allowlist.py`
    entry, broadened from that one literal string to the general
    field/value shape after the grammar-aware fuzzer generated other
    bracket-then-trailing-star patterns (e.g. `title:0[0-0]*`) that hit the
    same root cause; `tests/emitter/test_emit_patterns.py`
    (`test_wildcard_emission`'s `character-class-13568` /
    `13568-leading-star-class` cases exercise the emitter's own
    class-preserving behavior directly, independent of the parser fold);
    `tests/emitter/test_acceptance_e2e.py::test_issue_13568_acceptance`
    (the *leading*-star form from the actual issue report, where this bug
    does not trigger at all, see entry 14 below).

14. **JSON dotted-path fields (`notes.user`, `custom_fields.value`, ...)
    are a whoosh-compat-only concept with no whoosh analogue whatsoever
    (design).** Real whoosh has no JSON field type; the paperless-ngx
    schema this library's oracle clones had `notes`/`custom_fields` as
    plain `TEXT()` fields instead. There is no query a whoosh user could
    type that reaches "the note left by a specific user" the structured way
    whoosh-compat's JSON subpath (`FieldRegistry.make_ref`/`resolve`, which
    produce and resolve a `FieldRef` carrying the subpath) does. On
    both sides the dotted name is technically "unknown" to a plain-TEXT
    schema, but the two parsers' fieldname taggers handle an unregistered
    dotted name differently: whoosh-compat's `FieldsPlugin` tagger is
    deliberately dot-inclusive (`[\w.]+:` vs whoosh's `\w+:`) so a
    *registered* JSON field can resolve `notes.user:`, which as a side
    effect also makes it greedily tag the whole dotted run even when
    unregistered, so the resulting (unmapped) trees genuinely differ in
    shape, not just in whether the field resolves.

    Test references: `tests/differential/allowlist.py`'s two `custom_fields\.`
    / `notes\.` entries (AST-level: neither side's tree matches, by
    construction); `tests/emitter/test_acceptance_e2e.py::test_notes_user_json_subpath_has_no_v2_analogue`
    (result-level: demonstrates concretely that `notes.user:alice` matches
    doc 1 through the JSON-subpath emitter but nothing at all through a
    whoosh oracle index with a plain-TEXT `notes` field, even when that
    index's `notes` field is populated with the same underlying data
    flattened to plain text).

15. **`Multitoken.DEFAULT` uses position-dependent enclosing-group context;
    real whoosh's `multitoken_query='default'` uses the parser's fixed
    default group (design note, not a bug).** whoosh-compat's
    `Multitoken.DEFAULT` (`src/whoosh_compat/fields.py`) resolves "how do
    multiple tokens from one field value combine" by looking at the
    *actual* enclosing group at the term's position in the parsed tree
    (`TantivyEmitter._group_stack`, `src/whoosh_compat/emitters/tantivy_.py`):
    an `Or(...)` group's multitoken children combine with OR, an
    `And(...)` group's combine with AND. Real whoosh's default
    (`whoosh/qparser/default.py:191`, `multitoken_query='default'`) instead
    always uses the *parser's* single configured default group class,
    regardless of which group a term happens to sit inside syntactically.
    These agree for the common case (a multitoken term inside the query's
    top-level default group) but can diverge for a multitoken field value
    nested inside an explicit top-level `Or(...)` when the parser's
    configured default group is `And` (whoosh-compat's own default,
    matching entry 7's implicit-AND restoration): whoosh-compat would
    combine that term's tokens with OR (following the enclosing group),
    while real whoosh would still combine them with AND (following the
    parser's fixed default), even though both sides are looking at the
    exact same syntactic position.

    This was not hit by name in this project's corpus (no differential/
    acceptance case currently nests a genuine multitoken field value inside
    a top-level `Or`), but it is a known, understood shape of divergence
    baked into `Multitoken.DEFAULT`'s design rather than an implementation
    defect. Do not "fix" it by making the emitter track the parser's single
    default group instead of the syntactic enclosing group if it surfaces
    later; that would just move the divergence rather than remove it
    (whoosh-compat's own position-dependent behavior is arguably more
    intuitive for a hand-written query, since it means "what you see is
    what groups together").

16. **Several AST-level divergences above do not change final search
    results for this project's fixtures (a finding, not a new divergence of
    its own).** Entries 2 (wildcard case-folding order), entry 17
    (the `tag:'foo,bar'` comma-quote-literal design entry), and entry 12
    (date-range tz bypass) are all real at the *parsed-AST* level (what
    `tests/differential` compares) but were found, while building
    `tests/emitter/test_acceptance_e2e.py`, to not change the final doc-id
    set either backend's search actually returns for the queries in this
    project's fixture:

    - Entry 2: real whoosh's own `field.process_text(text, tokenize=False)`
      still runs the field's LowercaseFilter over an un-tokenized
      wildcard/prefix pattern (filters aren't skipped by `tokenize=False`,
      only the tokenizer step is, see
      `whoosh.analysis.tokenizers.RegexTokenizer.__call__`), so a fielded
      `Entwä*` query against real whoosh also ends up matching the
      lowercased pattern `entwä`, same as whoosh-compat's explicit
      `pattern_normalizer`, both sides match doc 3.
    - The comma-quote-literal entry: whoosh-compat's emitter re-runs the
      field's own `analyzer` (which still splits on commas) over a quoted
      comma value's Term text at *emit* time
      (`TantivyEmitter._text_term_query`), so the parse-time
      quoted-vs-split distinction doesn't survive to search time either.
    - Entry 12: this project's fixture's `created`/`added` values aren't
      close enough to a day boundary for a timezone shift to change which
      calendar day/year they fall into, so the bug's absence on the
      whoosh-compat side happens not to matter for any query this
      project's corpus currently exercises.

    See `tests/emitter/test_acceptance_e2e.py`'s module docstring for the
    specific evidence (each case was verified by actually running both
    pipelines, not by inspection). This does not mean entries 2/12/17 are
    wrong or should be removed: they are still real, reproducible
    AST-level divergences that a different fixture (e.g. dates
    near a local-midnight boundary) could absolutely turn into a
    result-level divergence too; it just means none of *this* project's
    specific test data happens to expose that.

17. **`tag:'foo,bar'` comma-quote-literal handling (design, formalizing what
    entries 12/16 above already referred to by description before this
    entry existed).** whoosh-compat's `CommaValuesPlugin` treats a *quoted*
    comma-values field value as a single literal (`SingleQuotePlugin` marks
    it `is_quoted`); real whoosh has no such plugin at all: a
    `KEYWORD(commas=True)` field's analyzer always splits on commas at
    *analysis* time, quoted or not, so `tag:'foo,bar'` still expands to
    `tag:foo AND tag:bar` upstream in real whoosh. This is a whoosh-compat
    feature whoosh never had, not a whoosh bug.

    Test references: `tests/differential/allowlist.py`'s `tag:'foo,bar'`
    entry; `tests/differential/corpus_docs.txt`'s `tag:'foo,bar'` line;
    entry 16 above (this AST-level divergence doesn't change this
    project's fixture's actual search results, since the emitter re-runs
    the field's own comma-splitting `analyzer` over the quoted literal's
    text at *emit* time anyway).

18. **Bare (non-bracketed) separated-ISO date field values structurally
    diverge from whoosh, even once numerically correct on both sides
    (design).** Fixing the `bundle` Choice's alternative order (it tried the
    `datetime` Bag before `simple`, so a separated date like `2020-01-01`
    never reached `simple`, the only alternative built to handle
    separators; see `parser/dateparse.py`'s `English.__init__`) makes
    whoosh-compat's single date grammar parse `created:2020-01-01`
    correctly and directly, producing a `DateRange` node the same way it
    handles every other date value.

    Real whoosh takes a different route to the same numeric answer:
    `DateParserPlugin.text_to_dt` has the *same* grammar-ordering
    limitation (confirmed directly: `LocalDateParser(tz).date_from
    ("2020-01-01", basedate)` returns `None`, same partial-year-match
    problem entry 12 describes for ranges), so it wraps the term node in an
    `ErrorNode`. But `ErrorNode.query()` (`whoosh/qparser/syntax.py`) does
    not give up: it calls the *original* wrapped node's own `.query()`
    method regardless, which for an ordinary fielded term is
    `FieldsPlugin`'s normal path straight to `DATETIME.parse_query`
    (`whoosh/fields.py`), whoosh's field-level self-parse that strips
    separators and slices the resulting digit string positionally. That
    fallback computes the numerically correct day/month-precision range,
    just as a `query.NumericRange`, not the `DateTimeNode`/`DateRangeNode`
    shape `text_to_dt` would have produced had its own grammar succeeded.
    `query.error_query` only *annotates* an `.error` attribute onto that
    working query; it does not replace it.

    whoosh-compat has no equivalent two-path architecture to replicate:
    its `DateParserPlugin` is the *only* date-parsing mechanism (see
    `parser/dateparse.py`'s module docstring), with no field-level
    self-parse fallback behind it, so once the ordering bug is fixed its single grammar
    path simply parses these values directly. The end value is the same
    (both sides agree on the day/month the value denotes), but the AST
    shape genuinely differs (`DateRange` vs. `NumericRange`), so this is a
    real, allowlisted AST-level divergence, not a whoosh bug to avoid
    reproducing.

    The "both sides agree on the value" part holds for zero-padded values,
    which is what the corpus covers. It does not extend to every string the
    allowlist regex can match: whoosh's fallback slices the separator-
    stripped digits by position, so an unpadded `created:2020-1-1` becomes
    `202011`, read as November 2020, while whoosh-compat reports it as an
    unrecognizable date. The harness never compares that case (a
    diagnostic skips it first), so the allowlist entry stays broad rather
    than enumerating padding variants.

    Test references: `tests/differential/allowlist.py`'s bare
    separated-ISO-date entry; `tests/differential/corpus_paperless.txt`'s
    `created:2020-01-01` / `created:2020-01` / `created:'2020.01.01'`
    lines; `tests/test_parser_dates.py::test_separated_iso_date_precision`
    (direct unit coverage of whoosh-compat's own corrected value, decoupled
    from the oracle comparison).

19. **Unquoted multi-word natural-date keywords (`created:previous month`)
    are out of whoosh-compat's parser scope (design).** whoosh-compat's
    date grammar adds new keywords (`previous week`/`month`/`quarter`/
    `year`) directly to the English grammar, usable as a single quoted
    phrase value like whoosh's own multi-word values always require
    (`created:"previous week"`). Real paperless-ngx v2 instead relied on an
    *app-level* regex preprocessing pass in `DelayedFullTextQuery`
    (`rewrite_natural_date_keywords`, `index.py`) that rewrites e.g.
    `created:previous week` (unquoted) into an explicit bracket range
    *before* whoosh ever sees the string; real whoosh's own grammar has no
    native "previous week" support at all (not a whoosh bug: it never
    claimed to have this feature). That preprocessing hack is
    paperless-app-specific, not part of whoosh's (or whoosh-compat's)
    parser proper, so it is out of scope for whoosh-compat's `parse()`:
    unquoted multi-word keywords behave like any other unquoted
    multi-word value (split at the first whitespace, one token per field).
    The README's syntax table documents only the quoted form
    (`created:'previous month'`) for exactly this reason.

    Test references: `tests/differential/allowlist.py`'s
    `previous (?:week|month|quarter|year)|this (?:month|year)` entry (the
    oracle harness replicates the app-level rewrite, so only the
    *unquoted* form is allowlisted; the quoted form is directly compared
    and passes); `README.md`'s date syntax row.

20. **A bare `field:*` wildcard simplifies to `Every(field)` rather than a
    literal `Wildcard('*')` (design).** whoosh-compat's
    `QueryParser.wildcard_query` treats the text being exactly `"*"` as a
    special case that builds `ast.Every(field)` instead of an
    `ast.Wildcard(field, "*")`; real whoosh's `WildcardPlugin` always
    builds a literal `Wildcard` query object, with no such simplification.
    Functionally equivalent (both match every document with a value in the
    field), but a different AST shape, and a deliberately cheaper emitted
    query on the tantivy side (`Every(field)` -> a fast-field
    `exists_query`, or a `regex(".*")` fallback for non-fast TEXT/KEYWORD
    fields, both cheaper than a literal wildcard scan; see
    `emitters/tantivy_.py`'s `visit_every`).

    Test references: `tests/differential/allowlist.py`'s `:\*(?:\s|$)`
    entry; `tests/emitter/test_emit_patterns.py`'s `test_every_field`.

    This "has any term at all" strategy (the non-fast TEXT/KEYWORD
    `regex_query(".*")` fallback in `_exists_query`) is shared by
    BOOLEAN_EXISTS term emission (`visit_term`'s `BOOLEAN_EXISTS` branch,
    for a field whose `exists_target` is non-fast TEXT/KEYWORD, not just
    `Every`/bare `field:*`), so it carries the same consequence there:
    "exists" means "has at least one indexed term", not "the stored field
    value is non-empty". A whitespace-only or punctuation-only value that
    the target field's own tokenizer reduces to zero terms reads as absent
    for existence purposes, on both the `field:*` and BOOLEAN_EXISTS paths,
    since both go through the same `_exists_query` helper. This is correct
    and intentional, consistent with how `Every(field)` already behaved
    before BOOLEAN_EXISTS started sharing the strategy, not a new or
    separate divergence.

    The strategy used for a given field (`FAST_FIELD` via `exists_query`, or
    `TERM_SCAN` via the `regex_query(".*")` fallback) is resolved once, at
    `FieldRegistry` construction, from the field's `kind`/`fast` combination
    (`fields.py`'s `resolve_exists_strategy`/`ExistsStrategy`), and stored on
    the registry rather than re-derived by `_exists_query` at emit time;
    `Every(field)` and a BOOLEAN_EXISTS field targeting that same field read
    the exact same registry-resolved strategy, so they cannot drift apart. A
    non-fast, non-TEXT, non-KEYWORD `exists_target` (nothing left that can
    answer "exists" at all) is rejected at registry construction, not left
    to fail at search time.

    A fast JSON field's existence check is subpath-scoped when the query
    addresses one: `attrs.user:*` checks only `attrs.user`'s own fast column
    (`resolved.dotted_name`), distinct from the whole-field `attrs:*`, which
    checks whether any subpath has a value at all (`json_subpaths=True`); a
    non-fast JSON subpath still has no strategy and raises
    `UnsupportedQueryError` naming the dotted form the query used.

    Test references: `tests/emitter/test_emit_boolean.py`'s
    `test_boolean_exists_non_fast_text_target` (docs 3/4, punctuation-only
    and whitespace-only `body` values),
    `test_boolean_exists_non_fast_keyword_target` (the same shape for a
    non-fast KEYWORD target),
    `test_registry_rejects_non_fast_non_text_non_keyword_exists_target`,
    `test_every_field_and_boolean_exists_agree`, and
    `test_every_field_and_boolean_exists_agree_across_targets`
    (`Every`/BOOLEAN_EXISTS agreement on the same and on differently-typed
    targets); `tests/test_fields.py`'s
    `test_validation_boolean_exists_target_unsupported_kind_rejected` and
    `test_validation_boolean_exists_target_keyword_is_valid`.

    A bare JSON field name (no subpath) is addressed via the same
    `field:*` -> `Every(field)` path when the query is exactly the
    existence check, even though the same bare name demotes to a text
    search for any other term/pattern (issue #11): the parser's
    `FieldsPlugin.do_fieldnames` carves the "*"-alone shape out of that
    demotion before it applies, using the same `text == "*"` detection
    `QueryParser.wildcard_query` already uses for the general case here.
    Test references: `tests/test_parser_fields.py`'s
    `test_json_bare_field_name_bare_star_is_existence_not_demoted` and
    `test_json_subpath_bare_star_unaffected_by_bare_name_carve_out`;
    `tests/emitter/test_kind_matrix.py`'s
    `test_json_bare_field_bare_star_existence`.

21. **A year followed by a colon-separated time reads as a calendar date
    (design).** Value text like `added:'2020 12:30'` is ambiguous: the
    trailing digits can be read as a time of day, or as the month and day
    of a separator-separated calendar date. Real whoosh reads it as a
    time, producing "12:30 on every day of 2020". whoosh-compat's date
    grammar tries its separated-date alternative first (the ordering that
    makes `created:2020-01-01` parse at all, see entry 18), and that
    alternative accepts `:` among its separators, so the same text reads
    as 30 December 2020.

    Only this shape is affected: a year plus a time that carries an
    explicit meridiem or an unambiguous marker (`added:'2020 5pm'`) still
    reads as a time on both sides, because the separated-date alternative
    cannot match it. Forms with no time component are unaffected.

    Test references: `tests/test_parser_dates.py`'s year-plus-time case.

22. **JSON-subpath `index.parse_query` fallback cannot honor
    `Multitoken.AND`/`OR`/`PHRASE` combinator semantics, only `FIRST` and
    single-leaf matching (a known limitation of the fallback path, not the
    general design).** `TantivyEmitter._emit_json_term`
    (`emitters/tantivy_.py`) runs `spec.analyzer` over a JSON subpath
    term's value and, when the installed tantivy-py can address a JSON
    subpath directly (`_json_paths_supported()`), reuses
    `_text_term_query` exactly like an ordinary TEXT/KEYWORD term: every
    `Multitoken` mode works identically to a plain field. When it cannot
    (as of tantivy-py 0.26, the version this project currently runs against;
    see the JSON `parse_query` carve-out in `ARCHITECTURE.md` §5), the
    fallback still runs `spec.analyzer` and honors `Multitoken.FIRST`
    (searching only the first token), but `AND`/`OR`/`PHRASE`/
    DEFAULT-resolved-to-AND-or-OR all collapse to one quoted, space-joined
    leaf through `index.parse_query`, which behaves like a phrase match,
    not true AND ("all tokens present, any order/position") or OR ("any
    token present") semantics. This is a structural limitation of the
    carve-out itself: `index.parse_query`'s single-leaf call has no
    programmatic way to build a JSON-subpath boolean query the way
    `_text_term_query` does for every other field kind. It was fixed to at
    least stop discarding the analyzed tokens entirely (previously the
    fallback quoted the *raw, unanalyzed* text verbatim, ignoring
    `spec.analyzer` and every `Multitoken` mode including `FIRST`), but
    full AND/OR/PHRASE parity is not achievable without the JSON subpath
    `term_query`/`phrase_query` support `_json_paths_supported()` probes
    for. Once tantivy-py#716 lands and ships, `_json_paths_supported()`
    starts returning `True` and this whole fallback branch (including this
    limitation) stops being taken.

    Test references: `tests/emitter/test_emit_json.py`'s
    `test_json_subpath_parse_query_fallback_honors_multitoken_first`.

    This limitation is about `Term` values only. A quoted `Phrase` node on
    a JSON subpath (`TantivyEmitter._emit_json_phrase`) never consults
    `Multitoken` at all, on either branch (a phrase's words are the phrase,
    not independent tokens to combine), and additionally cannot carry an
    explicit whoosh slop through the `index.parse_query` fallback branch:
    that single quoted-leaf call has no query-string syntax tantivy-py 0.26
    honors for slop (verified directly against a live index: appending
    `~N` to the quoted phrase text does not change the resulting query's
    slop away from `0`), so a widened slop silently has no effect there,
    unlike the `_json_paths_supported()` branch, which maps slop exactly
    like a plain-field phrase (`max(node.slop - 1, 0)`). Once
    tantivy-py#716 ships, the fallback branch stops being taken and this
    slop limitation goes away along with the rest of this entry.

    Test references: `tests/emitter/test_emit_json.py`'s
    `test_json_subpath_phrase_fallback_slop_is_silently_ignored`,
    `test_json_subpath_phrase_fallback_ignores_multitoken`, and
    `test_json_subpath_phrase_supported_branch_maps_slop`;
    `tests/emitter/test_kind_matrix.py`'s
    `test_json_subpath_phrase_slop_and_multitoken`.

23. **`NOT` of a term whose analyzer drops every token matches every
    document here, but matches none in real whoosh (confirmed divergence,
    not fixed).** `visit_term`'s zero-token TEXT/KEYWORD branch
    (`emitters/tantivy_.py`) returns `Query.empty_query()` for a term that
    analyzes to zero tokens (e.g. an all-stopword value). `visit_not`
    wraps whatever `self.visit(node.child)` returns in `MustNot(...)`
    without going through `_group_child`'s zero-token-drop check (that
    check only applies to direct And/Or children), so `NOT` of such a term
    becomes `MustNot(empty_query())`, which excludes nothing and therefore
    matches every document.

    Real whoosh does the opposite. Verified directly against the pinned
    oracle: `query.Not(NullQuery).normalize()` is `NullQuery` (real
    whoosh's `Not.normalize()`, `whoosh/query/wrappers.py`, returns the
    singleton `NullQuery` unchanged when its child normalizes to
    `NullQuery`, rather than treating "not nothing" as "everything"), and
    searching that normalized query returns zero hits. This is the exact
    opposite of whoosh-compat's emit-time behavior for the equivalent
    shape.

    This is *not* the same case as `ast.normalize()`'s own
    `Not(Nothing) -> Every` rule (`ast.py`), even though the two produce
    the same-shaped result: that rule fires at parse/normalize time for an
    explicit `ast.Nothing` node reaching a `Not`, and is itself a
    deliberate, pre-existing design choice with no claim to whoosh parity
    (whoosh's own rule for the equivalent AST-level case is also
    "stays nothing", the same direction as the term-analyzer case
    documented here). The case documented in this entry is purely an
    *emit-time* phenomenon: an `ast.Term` that is syntactically ordinary
    (not an `ast.Nothing` node) but whose configured `analyzer` happens to
    consume its text entirely once emission runs `_tokens` over it, a fact
    `ast.normalize()` has no visibility into since it runs before
    analysis. Left undocumented before this entry, `visit_term`'s
    docstring described the emit-time behavior as consistent with
    `ast.normalize()`'s rule without flagging that neither one actually
    matches real whoosh; the docstring now points here instead.

    Decision: documented, not changed, matching this project's judgment
    call. Changing only the emit-time case to match whoosh (while leaving
    `ast.normalize()`'s already-established, unrelated `Not(Nothing) ->
    Every` parse-time rule as-is) would make two structurally identical
    situations, a `NOT` whose operand turns out empty, behave differently
    depending purely on *when* the emptiness was discovered (parse-time
    Nothing node vs. emit-time zero-token analysis), which is a timing
    artifact a query author has no way to predict or control. Uniform
    emit-time behavior, even though it disagrees with whoosh, is more
    predictable than a rule that depends on an implementation detail.

    Test references: `tests/emitter/test_emit_boolean.py`'s
    `test_not_zero_token_term_matches_everything`. The grammar-aware
    property fuzzer (`tests/differential/strategies.py`,
    `test_hypothesis.py::test_fuzz_grammar_matches_oracle`) later found that
    this same divergence also reaches the *differential AST-comparison*
    layer, not just the emitter: `oracle.analyze_ast`'s own token-dropping
    rule turns a `NOT`'s now-empty child into an empty `And()`, which
    `ast.normalize()`'s pre-existing `Not(Nothing) -> Every` rule then
    upgrades to `Every()`, landing on the same "matches everything" shape
    this entry already describes, just reached through the test harness's
    forward-analysis step instead of `TantivyEmitter`. Allowlisted in
    `tests/differential/allowlist.py` (a `NOT` directly wrapping a single
    known-zero-token TEXT-field value) rather than treated as a new
    divergence, since the underlying behavior is this same entry.

24. **An all-zero-token quoted phrase parses to a real empty-words `Phrase`
    object in real whoosh, but is dropped entirely by whoosh-compat's
    emit-time analysis (design, found by the grammar-aware fuzzer).** Real
    whoosh's `PhrasePlugin.PhraseNode.query()`
    (`whoosh/qparser/plugins.py`) tokenizes a quoted phrase's text at
    *parse* time, via the field's own analyzer, and builds a
    `whoosh.query.Phrase(fieldname, words, ...)` regardless of how many
    words come out the other end, including zero (e.g. `title:"the"`, a
    single stopword): the result is a real, non-null `Phrase` query object
    whose `words` list just happens to be empty. whoosh-compat instead
    defers all analysis to emit time (see ARCHITECTURE.md's "analyzer
    contract" invariant): `ast.Phrase` keeps the raw, unanalyzed text, and
    `TantivyEmitter` (and, for the differential harness's purposes,
    `oracle.analyze_ast`'s `_analyzed_phrase` helper, which models the same
    behavior for comparison) drops the phrase from its enclosing group
    entirely once analysis reduces it to zero tokens, the same rule already
    applied to a zero-token plain `Term` (see `oracle.analyze_ast`'s
    docstring). The two sides therefore build structurally different trees
    for the exact same input: an oracle `ast.Phrase(field, text="")` versus
    whoosh-compat's node vanishing (its enclosing group normalizing to
    `Nothing()` if nothing else survives). This is the same underlying
    parse-time-vs-emit-time-analysis design already responsible for several
    other entries in this document, just not previously exercised by name
    for a *phrase* (only single terms) until the grammar-aware fuzzer
    generated an all-stopword phrase.

    Test references: `tests/differential/allowlist.py`'s all-zero-token
    quoted-phrase entry; `tests/differential/strategies.py`'s
    `ZERO_TOKEN_WORDS` (the same verified-zero-token vocabulary used to
    generate both this case and entry 23's).

25. **A bare (non-bracketed) relative date offset is a whoosh-compat-only
    feature (design, found by the grammar-aware fuzzer).** `created:now-7d`
    and `created:-3mos` parse to a real `DateRange` in whoosh-compat: this
    is documented directly in README.md's syntax table, which lists
    `created:now-7d` as a bare example, not just something usable inside a
    bracketed range's bounds. Real whoosh's date grammar only recognizes
    this relative-offset syntax (`now-7d`, `-1 week`, etc.) as a *range
    bound*; a bare, non-bracketed value in this shape fails to parse as a
    date on the real-whoosh side and falls back to `NullQuery`. Confirmed
    directly against the pinned oracle:
    `oracle_parse("created:now-7d", ...)` returns `NullQuery`, while
    `oracle_parse("created:[now-7d TO now]", ...)` parses the identical
    relative-offset text correctly as a range bound. Not a bug on either
    side: whoosh-compat's single-value date grammar simply accepts a syntax
    real whoosh's does too, just only in the other position.

    A relative offset written with a space (`created:-1 week`, unquoted)
    fails to parse as a single token on whoosh-compat's side too (it splits
    at the whitespace like any other unquoted multi-word value, producing a
    `BAD_DATE` diagnostic for the `-1` piece and an ordinary multifield term
    search for `week`): that shape is already excluded from comparison by
    the DIVERGENCES.md entry 6 diagnostics check, not by this entry.

    Test references: `tests/differential/allowlist.py`'s bare-relative-
    date-offset entry; `tests/differential/strategies.py`'s
    `_DATE_RELATIVE` (the relative-offset vocabulary the grammar-aware
    fuzzer draws bare date values from).

26. **Phrase slop diverges from `~2` upward (design, mapping left
    unchanged).** whoosh's slop counts *positions spanned* (adjacent is
    slop=1); tantivy's counts *gaps allowed* (adjacent is slop=0), so the
    mapping is `tantivy_slop = whoosh_slop - 1` (`emitters/tantivy_.py`'s
    `visit_phrase`). That mapping is exact for the parser default (slop=1)
    and for two-word phrases in forward order at slop=2, which is what a
    caller who never types `~N` gets. Two independent divergences appear
    only once a query explicitly widens slop with `~N`:

    - **Reversed pair over-match, `N >= 3`.** Real whoosh's phrase matcher
      (`SpanNear2(..., ordered=True)`, `whoosh/query/positional.py:243`)
      never matches a transposed pair at any slop. tantivy's slop is an
      unordered total-displacement budget, so a reversed two-word phrase
      starts matching once that budget covers a full swap: whoosh slop 3
      maps to tantivy slop 2, which is enough. Confirmed against the pinned
      oracle: query phrase `"one two"`, document `"two one"`, whoosh never
      matches at any slop, tantivy matches starting at whoosh slop 3.
    - **Three-or-more-word under-match, `N >= 2`.** whoosh's ordered
      matcher enforces slop as a *per-adjacent-gap* limit; tantivy's is a
      *total* budget shared across the whole phrase. For a 3+ word phrase
      where each adjacent gap uses the full whoosh allowance, tantivy's
      total budget runs out before whoosh's would. Verified: query phrase
      `"one two three"` against document `"one x two y three"`, whoosh
      slop=2 matches (each adjacent gap is within limit 2), tantivy needs
      slop=3 (total displacement 3) to match the same document; the mapped
      tantivy slop=1 does not.

    Not reproduced at the parser default or at whoosh slop=2 on a two-word
    phrase, so no user who omits `~N` is affected, and no line in
    `tests/differential/corpus_paperless.txt` uses `~` at all. Changing the
    mapping to close either gap would put the currently-exact default and
    two-word-slop-2 cases at risk to fix a shape nobody in the motivating
    consumer's corpus types; left as a documented divergence instead.

    Test references: `tests/emitter/test_emit_phrase.py`'s
    `test_phrase_reversed_pair_slop_boundary` (pins the reversed-pair
    over-match boundary and confirms the default and slop=2 still agree
    with whoosh).

27. **A degenerate `ANDNOT`/`ANDMAYBE`/`REQUIRE` operand poisons an
    enclosing `And` on whoosh-compat's side but is dropped on whoosh's
    (design, found by the property-based fuzzer while fixing issue #10).**
    Issue #10 restored the rule that an empty group (`()`) drops out of the
    tree at parse time instead of becoming a live `Nothing()`. Once a
    literal empty group can appear as an `ANDNOT`/`ANDMAYBE`/`REQUIRE`
    operand, so can a group whose only content analyzes to zero tokens
    (e.g. `(0)`, a single character below `StandardAnalyzer`'s
    `minsize=2`): both parse to the same "this operand resolves to
    nothing" shape by the time `ast.normalize()` runs. Real whoosh's
    `AndNot`/`AndMaybe`/`Require.normalize()` (`whoosh/query/compound.py`)
    already implement the correct per-operator rule for that shape
    (`AndNot`/`AndMaybe`: positive/required null -> `NullQuery`, negative/
    optional null -> the other side; `Require`: either side null ->
    `NullQuery`), which whoosh-compat's own `ast.normalize()` also
    implements identically (see its `AndNot`/`AndMaybe`/`Require`
    branches). The divergence is one level up: real whoosh's
    `And.normalize()` drops a `NullQuery` child from an enclosing `And`
    (verified directly: `And([Term('tag', '0'), And([])]).normalize() ==
    Term('tag', '0')`), while whoosh-compat's `ast.normalize()` And rule
    poisons instead (`any(isinstance(c, Nothing) ...) -> Nothing()`). This
    is the same kind of deliberate divergence as entry 23's `Not(Nothing)
    -> Every` (there, confirmed as "not a parity-preserving fallback"):
    `tests/test_normalize.py` already pins the asymmetry directly (`And`
    poisons on a `Nothing` child, `Or` drops one, `or-drops-nothing-child`),
    which only makes sense as an intentional choice (AND with a provably
    impossible clause is itself impossible; OR with one clause impossible
    still has the others), not an oversight. Issue #10 explicitly keeps
    this algebra rather than reproducing whoosh's fully transitive
    drop-not-poison behavior for every compound type; this entry documents
    the one new place (a literal empty group as an `ANDNOT`/`ANDMAYBE`/
    `REQUIRE` operand) where that existing, deliberate rule now applies.

    No line in `tests/differential/corpus_paperless.txt` or
    `corpus_docs.txt` uses `ANDNOT`/`ANDMAYBE`/`REQUIRE` at all
    (grep-verified), so this is only reachable through the property-based
    fuzzers (`tests/differential/test_hypothesis.py`), which generate these
    operators freely with arbitrary short (down to one character) words.

    Test references: `tests/differential/allowlist.py`'s
    `\bANDNOT\b|\bANDMAYBE\b|\bREQUIRE\b` entry; `tests/test_syntax.py`'s
    `test_binarygroup_left_none_becomes_nothing_positive` and
    `test_binarygroup_right_none_becomes_nothing_negative` pin the
    corrected (and now real-whoosh-matching) per-node behavior at the
    parser level directly.

28. **A double-quoted `"*"` on a BOOLEAN_EXISTS field is not reproduced as a
    whoosh crash (whoosh-bug, issue #16).** Real whoosh's `PhrasePlugin`
    calls `field.process_text()` on every quoted value regardless of field
    type, and `whoosh.fields.BOOLEAN` has no analyzer at all
    (`BOOLEAN.tokenize` raises `Exception("... field has no analyzer")` for
    any input, not something specific to `"*"`): `has_tag:"*"` against the
    real oracle raises a bare `Exception` while parsing, before a query
    object is even built. This is a defect in whoosh's own `BOOLEAN` field
    type, not intended semantics, so it is not reproduced: whoosh-compat's
    `visit_phrase` treats a double-quoted `"*"` on a `BOOLEAN_EXISTS` field
    the same as the single-quoted and unquoted forms, an existence match
    (see entry 27's neighbor, issue #16's `_exists_query`/`Every` redirect
    through `exists_target`). The single-quoted form (`has_tag:'*'`) does
    not crash real whoosh (`BOOLEAN.parse_query` special-cases `"*"`
    directly, matching whoosh-compat's own `term_query` fix) and is
    corpus-compared normally.

    Test references: `tests/differential/corpus_docs.txt`'s issue #16
    section (only the single-quoted and numeric forms are corpus lines, for
    exactly this reason); `tests/emitter/test_emit_phrase.py`'s
    `test_quoted_star_phrase_matches_unquoted_star` and
    `tests/emitter/test_emit_boolean.py`'s
    `test_every_field_on_the_boolean_exists_field_itself` cover the
    double-quoted and bare-star forms directly, without the oracle.

29. **A wildcard/prefix pattern on a numeric field or a BOOLEAN_EXISTS field
    is diagnosed at parse time, not silently mangled (whoosh-bug, not
    reproduced; issue #17, reopened for the BOOLEAN_EXISTS case).**
    Real whoosh's `WildcardPlugin`, for a NUMERIC field, silently drops the
    wildcard character(s) and searches whatever's left as a literal exact
    value: confirmed directly against the oracle, `type_id:1*` parses to
    `Term('type_id', <bytes for the int 1>)`, not a rejected query and not
    an actual wildcard search. A user typing `asn:1*` almost certainly
    means "starts with 1", and getting silently narrowed to "is exactly 1"
    with no error is a defect, not intended semantics, so it is not
    reproduced. whoosh-compat instead reports a `DiagnosticKind.UNKNOWN`
    diagnostic and an `ErrorLeaf`, the same shape `BAD_NUMBER`/`BAD_DATE`
    already use for other invalid-input-on-parse cases, so a host can
    surface it as a 400 instead of a wildcard that quietly means something
    else, or later dies at tantivy search time (`regex_query` doesn't work
    against a numeric field at all).

    A BOOLEAN_EXISTS field (e.g. `has_tag`) has the same silent-mangle
    defect on real whoosh (`has_tag:t*` executes leniently, mangled to
    `Term('has_tag', True)`) and gets the same treatment here for the same
    reason: this synthetic field also has no tantivy schema column of its
    own (it redirects to its `exists_target`'s), so letting a
    `Prefix`/`Wildcard` node reach `emit()` would fail there instead,
    either with an undocumented-shape error or, for a hand-built AST node
    bypassing the parser, tantivy-py's raw "Field ... is not defined in the
    schema" `ValueError` leaking through unchanged. The diagnostic fires
    only for a genuine pattern; `_wildcard_kind_diagnostic` is scoped to run
    after the `text == "*"` existence-match special case has already
    resolved to `Every`, so `has_tag:*` is unaffected. The emitter has a
    matching backstop, `TantivyEmitter._reject_pattern_incompatible_kind`
    (shared with entry 30's JSON-subpath case), for a hand-built AST node
    that bypasses the parser.

    A bare `field:*` (the "*"-alone existence-match special case, entry 20
    and issue #16) is unaffected: this entry is specifically about a
    genuine wildcard *pattern* (`?`, multiple/leading `*`, or a bracket
    class), checked only after the "*"-alone case has already been
    handled.

    Test references: `tests/test_parser_fields.py`'s
    `test_wildcard_on_u64_field_is_diagnosed` (trailing-star prefix fold,
    `?`, a bracket-class wildcard, and a leading star),
    `test_bare_star_on_u64_field_is_still_an_existence_match`,
    `test_wildcard_on_boolean_exists_field_is_diagnosed`, and
    `test_bare_star_on_boolean_exists_field_is_still_an_existence_match`;
    `tests/emitter/test_emit_patterns.py`'s
    `test_pattern_on_boolean_exists_field_raises_at_emit` (fast and
    non-fast `exists_target`, both `Prefix` and `Wildcard`);
    `tests/emitter/test_kind_matrix.py`'s `boolean-exists-fast`/
    `boolean-exists-nonfast` `prefix-star`/`wildcard`/`bracket-class` cells;
    `tests/differential/corpus_docs.txt`'s issue #17 section (skips via the
    existing entry 6 diagnostics-present check, same as any other
    parse-time diagnostic).

30. **A wildcard/prefix pattern on a JSON subpath is diagnosed at parse
    time rather than silently querying the whole field's encoded bytes
    (tantivy-py gap, not a whoosh divergence; issue #30).** Unlike entry
    29's U64 case, this is not a whoosh defect being declined: whoosh has
    no JSON field concept at all, so there is no whoosh behavior to
    compare against. tantivy stores JSON terms as path-prefixed encoded
    bytes (`path\x00<type-byte>value`), and there is no tantivy-py API on
    the pinned version (0.26.0) that can build a pattern query scoped to
    one subpath: `Query.regex_query(schema, 'notes.user', ...)` raises
    `ValueError: Field 'notes.user' is not defined in the schema` (the
    dotted subpath form is rejected outright), and building the query
    against the bare JSON field name instead compiles but matches wrong in
    both directions, silently: `notes.user:ali*` misses a document with
    `notes.user == 'alice'` (the anchored regex can never match the
    path-prefixed bytes `user\x00salice`), while `notes.note:user*`
    spuriously matches a document that merely has *a* `user` subpath
    (the pattern matches the encoded path bytes of an unrelated subpath),
    both confirmed directly against the pinned tantivy-py rather than
    assumed.

    whoosh-compat reports the same `DiagnosticKind.UNKNOWN` diagnostic and
    `ErrorLeaf` shape as entry 29, from the same `_wildcard_kind_diagnostic`
    check in `parser/default.py`, extended to also fire when a `Prefix`/
    `Wildcard` ref resolves to a JSON subpath (independent of the U64
    check: a JSON field's own kind is never U64). A hand-built `Prefix`/
    `Wildcard` node that bypasses the parser (so it never reaches the
    parse-time diagnostic) is refused a second time at emit, by
    `TantivyEmitter._reject_pattern_incompatible_kind` in `emitters/tantivy_.py`,
    which raises `UnsupportedQueryError` before any `Query.regex_query`
    call is built, mirroring entry 5's text-range emit-time backstop.

    A bare `field.subpath:*` (the "*"-alone existence-match special case,
    entries 20 and 29) is unaffected: it never reaches
    `_wildcard_kind_diagnostic` at parse time (handled by the earlier
    `text == "*"` branch in `wildcard_query`) and is emitted via
    `visit_every`/`_exists_query`, not `visit_prefix`/`visit_wildcard`, so
    it keeps routing to the subpath-aware existence check from issue #29.

    If a future tantivy-py version gains a way to scope a pattern query to
    a JSON subpath, this can be revisited as a self-retiring carve-out with
    a probe, the same shape as `TantivyEmitter._json_paths_supported()`
    (ARCHITECTURE.md's "JSON `parse_query` carve-out" section); it should
    not be emulated against the current API in the meantime.

    Test references: `tests/test_parser_fields.py`'s
    `test_wildcard_on_json_subpath_is_diagnosed` (trailing-star prefix
    fold, `?`, and a bracket-class wildcard), `test_bare_star_on_json_subpath_is_still_an_existence_match`,
    and `test_wildcard_on_json_plain_field_no_subpath_is_unaffected`;
    `tests/emitter/test_emit_patterns.py`'s
    `test_pattern_on_json_subpath_raises_at_emit` (non-fast and fast
    subpaths, both `Prefix` and `Wildcard`) and
    `test_pattern_on_plain_json_field_no_subpath_still_works`;
    `tests/emitter/test_kind_matrix.py`'s `json-nonfast`/`json-fast`
    `prefix-star`/`wildcard`/`bracket-class` cells.

31. **A query nested past 200 parenthesization levels is diagnosed at parse
    time instead of crashing with an uncontrolled `RecursionError` (issue
    #31).** Real whoosh has no nesting cap at all: it recurses through its
    own query-building traversal the same way this parser's fork did, and
    both were confirmed directly to start `RecursionError`-ing on bare
    paren-nesting somewhere between depth 950 and 1000 (Python's default
    recursion limit of 1000). That crash is a whoosh defect, not intended
    semantics (nothing about whoosh's design calls for a query to fail with
    a bare interpreter error instead of a parser diagnostic), so it is not
    reproduced here: whoosh-compat caps nesting at
    `_MAX_GROUP_NESTING_DEPTH` (200, `parser/plugins.py`), well under either
    interpreter's crash floor, and reports `Diagnostic(kind=TOO_DEEP)` plus
    an `ErrorLeaf` for the excess nesting instead, keeping `parse()`'s
    "never raises for query input" invariant intact regardless of the
    interpreter's recursion limit.

    Not carried through the differential-triage allowlist/corpus triple:
    the corpus generators (`tests/differential/strategies.py`) have no
    mechanism that produces 200+ levels of paren nesting, so there is no
    oracle-comparison test this could ever apply to; the divergence is
    exercised directly instead (see the test references below).

    Test references: `tests/test_parser_basics.py`'s
    `test_paren_nesting_below_cap_has_no_diagnostic` and
    `test_paren_nesting_beyond_cap_reports_diagnostic_instead_of_raising`;
    `tests/test_normalize.py`'s `TestIterativeNormalizeDeepTree` (covers
    `ast.normalize()`'s traversal directly, via a hand-built tree that
    bypasses the parser's cap entirely).
