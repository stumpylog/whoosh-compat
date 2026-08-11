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
    `created|modified|added:\[` entry (covers every bracketed range on a
    DATE/DATETIME field in the differential corpus,
    `tests/differential/corpus_*.txt`); confirmed to *not* change actual
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
    entry (`\btitle:202\[0-3\]\*`); `tests/emitter/test_emit_patterns.py`
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
    whoosh-compat's JSON subpath (`FieldRegistry.resolve_json`) does. On
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

    Test references: `tests/emitter/test_emit_boolean.py`'s
    `test_boolean_exists_non_fast_text_target` (docs 3/4, punctuation-only
    and whitespace-only `body` values).

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
    `test_not_zero_token_term_matches_everything`.

