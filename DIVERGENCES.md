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
    comment on that corpus line). whoosh-compat's `_range_to_node` parses each
    bound through `ToEnd(self.dateparser.get_parser())`: the same
    full-consumption requirement `text_to_node`'s wrapped `date_from`
    applies, but WITHOUT the wrapper's per-bound disambiguation, which
    must not run before the joint `timespan.disambiguated()` combine step
    (disambiguating each bound independently is what produced the
    inverted `[dec to feb]` ranges; see `_range_to_node`'s own comment).
    A partial match on either bound therefore correctly fails (surfacing
    a `BAD_DATE` diagnostic) rather than silently collapsing to whatever
    coarser precision the first alternative happened to match. Combined with a
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
    *actual* enclosing group at the term's position in the parsed tree: an
    `Or(...)` group's multitoken children combine with OR, an `And(...)`
    group's combine with AND. This resolution is computed once, structurally,
    by `whoosh_compat.ast.analyze()` (a top-down pass over the tree assigns
    each node the `Multitoken` context of its nearest enclosing And/Or, with
    `analyze()`'s own `default_mode` parameter, `Multitoken.AND` by default,
    used for a term with no enclosing group at all); it is not tracked via
    any per-visit emitter state (an earlier version of this mechanism lived
    in a `TantivyEmitter`-internal group-context stack, before analysis
    became its own explicit pipeline stage). Real whoosh's default
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

    This was not hit by name in this project's corpus at the AST-comparison
    layer for a long time (no differential/acceptance case nested a genuine
    multitoken field value inside a top-level `Or`), but it is a known,
    understood shape of divergence baked into `Multitoken.DEFAULT`'s design
    rather than an implementation defect. Do not "fix" it by making
    `analyze()` track the parser's single default group instead of the
    syntactic enclosing group if it surfaces further; that would just move
    the divergence rather than remove it (whoosh-compat's own
    position-dependent behavior is arguably more intuitive for a
    hand-written query, since it means "what you see is what groups
    together"). It has since been confirmed at the AST-comparison layer too,
    not just the result level below: once `analyze()` became the single
    implementation both `TantivyEmitter` and the differential harness call
    (replacing the harness's own separate, hand-synchronized forward-analysis
    model), an unfielded or unknown-field-demoted multi-token value
    (multifield expansion always wraps such a value in a fresh `Or(...)` at
    the point it appears, giving every default field's own combinator an
    unconditional `Or` context to resolve `DEFAULT` against) started
    reaching this exact divergence directly in `tests/differential`, not
    just in the acceptance-layer property described next.
    `tests/differential/allowlist.py`'s entry for this shape covers both
    textual pathways (a bare dashed/dotted word, and an unknown-field-colon
    demotion) that reach it in the current corpus/fuzzer vocabulary.

    The *correctly-fielded* pathway (a known TEXT/KEYWORD field's
    multi-token value written inside an explicit user-typed `OR`, the
    "narrower, context-dependent case" the paragraph above predicts) is
    also confirmed at the AST-comparison layer and has its own adjacent
    allowlist entry. One nuance worth stating: a degenerate parenthesized
    wrapper does not shield the term from the enclosing `OR`. `analyze()`
    normalizes its input before resolving `Multitoken` context (which is
    also what makes it insensitive to whether a caller pre-normalized), so
    in `(0) OR (title:00-000)` the singleton group around the fielded term
    collapses first and the term resolves `DEFAULT` against the `Or`,
    exactly as the production emitter always has (it normalizes before
    analyzing). The differential harness's raw-tree path used to see the
    un-collapsed singleton `And` wrapper and resolve AND context,
    coincidentally agreeing with whoosh for this one spelling while
    production did not; the harness now sees what production sees.
    Corpus lines: `content:foo OR title:multi-word` and
    `(0) OR (title:00-000)` (`tests/differential/corpus_paperless.txt`).

    The acceptance-layer result property (`tests/emitter/test_acceptance_property.py`)
    later found the predicted shape occurring for real, and confirmed it
    reaches the result level, not just a parsed-tree difference: an
    unfielded (hence always multifield-expanded into a top-level `Or`, the
    default group real whoosh's own parser also uses at the top level, so
    this shape does not even need an explicit `OR` in the query text)
    two-token dashed word, `00-YEAR`, against this property's fixture
    (`DOCS_PROP`, whose doc 3 title contains "Year" but not "00"). Real
    whoosh requires both tokens present in the *same* field
    (`And([Term('content', '00'), Term('content', 'year')])` per field,
    confirmed directly), matching nothing here; whoosh-compat's `Or`-context
    `Multitoken.DEFAULT` only requires *either* token, so doc 3's title
    alone (containing "year") satisfies that field's clause, matching doc 3.
    Still not a bug to fix, for the same reason given above; this is the
    entry-16-style confirmation that a documented, deliberate design choice
    can be result-changing on the right data, not evidence the choice
    itself needs revisiting.

    A second, independent trigger pathway to the identical mechanism was
    also confirmed: a bare (subpath-less) registered-JSON-field value
    (entry 29's demotion) can itself analyze to more than one token.
    `attrs:END` demotes to the literal unfielded text `attrs:END` on both
    sides (entry 29), which `StandardAnalyzer` then tokenizes into two
    surviving tokens, `attrs` and `end` (the colon is a token boundary);
    matched against doc 3 (content contains "end" but not "attrs"),
    whoosh-compat's `Or`-context OR-combination matches it, real whoosh's
    AND-combination does not. Not every bare-JSON-demoted value reaches
    this: `attrs:0` tokenizes to only one surviving token (`0` is dropped,
    shorter than `StandardAnalyzer`'s `minsize=2`), so there is no
    OR-vs-AND ambiguity there and both sides agree, confirming the
    mechanism is genuinely about token *count*, not about JSON-field
    demotion specifically. Two refinements found by a deep fuzz soak:
    the *unknown-field demotion* is a third result-level pathway
    (`notes.user:YEAR`: an unregistered, possibly dotted fieldname
    demotes on both sides and its colon-split tokens reach the same
    OR-vs-AND result divergence; at the parsed-AST layer a dotted
    spelling is claimed under entry 14, since whoosh's mid-token tagger
    demotes it in two pieces, but the result-set difference is this
    entry's mechanism, verified against the live dual index); and the
    single-character value İ (U+0130) defeats the allowlists' otherwise
    reliable two-character survives-analysis proxy, because it is the
    only character in Unicode whose `str.lower()` expands to two
    codepoints (pinned by a derivation test), so `zzz:İ` and `attrs:İ`
    genuinely split into two surviving tokens and diverge.

    Test references: `tests/emitter/result_allowlist.py`'s unfielded/
    `OR`-nested dashed-word and bare-JSON-value entries;
    `tests/emitter/test_acceptance_property.py`'s
    `test_multitoken_default_or_context_is_a_result_level_divergence`.

16. **Several AST-level divergences above do not change final search
    results for this project's fixtures (a finding, not a new divergence of
    its own).** Entries 2 (wildcard case-folding order) and 12 (date-range
    tz bypass) are real at the *differential-compared* AST level (what
    `tests/differential/test_differential.py::test_matches_oracle` actually
    compares, the tree produced after `whoosh_compat.ast.analyze()`'s
    forward analysis) but were found, while building
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
    - Entry 12: this project's fixture's `created`/`added` values aren't
      close enough to a day boundary for a timezone shift to change which
      calendar day/year they fall into, so the bug's absence on the
      whoosh-compat side happens not to matter for any query this
      project's corpus currently exercises.

    Entry 17's comma-quote-literal divergence is a related but distinct
    case, worth contrasting with the two above: it does not even survive to
    the differential-compared AST level in the first place (entry 17's own
    text now explains why: `whoosh_compat.ast.analyze()`'s forward analysis
    already splits both sides' comma values identically, for the same
    reason `TantivyEmitter` does, described below), so it was never a
    `tests/differential` divergence to begin with, only a raw-parse-tree
    one and a documented design choice.

    See `tests/emitter/test_acceptance_e2e.py`'s module docstring for the
    specific evidence behind entries 2 and 12 (each case was verified by
    actually running both pipelines, not by inspection). This does not mean
    entries 2/12/17 are wrong or should be removed: entries 2 and 12 are
    still real, reproducible differential-compared-AST-level divergences
    that a different fixture (e.g. dates near a local-midnight boundary)
    could absolutely turn into a result-level divergence too, and entry 17
    is still a real, unit-tested raw-parse-tree divergence; it just means
    none of *this* project's specific test data or comparison layers happen
    to expose these as result-changing.

    The comma-quote-literal mechanism common to entry 17 and this entry:
    `whoosh_compat.ast.analyze()` re-runs the field's own `analyzer` (which
    still splits on commas) over a quoted comma value's Term text before the
    emitter ever visits it, so the parse-time quoted-vs-split distinction
    doesn't survive to search time, the same way it doesn't survive
    `analyze()`'s forward analysis at the differential layer either (the
    same function, not a separately maintained model of it).

17. **`tag:'foo,bar'` comma-quote-literal handling (design, formalizing what
    entries 12/16 above already referred to by description before this
    entry existed).** whoosh-compat's `CommaValuesPlugin` treats a *quoted*
    comma-values field value as a single literal (`SingleQuotePlugin` marks
    it `is_quoted`); real whoosh has no such plugin at all: a
    `KEYWORD(commas=True)` field's analyzer always splits on commas at
    *analysis* time, quoted or not, so `tag:'foo,bar'` still expands to
    `tag:foo AND tag:bar` upstream in real whoosh. This is a whoosh-compat
    feature whoosh never had, not a whoosh bug.

    This divergence is real in the raw, pre-analysis parse tree, but does
    not reach `tests/differential`'s own AST-comparison layer: that
    comparison runs both sides through `whoosh_compat.ast.analyze()` first,
    which forward-analyzes every `Term` through its field's own analyzer
    before comparing (the same function `TantivyEmitter.emit()` calls, the
    same mechanism entry 16 above describes at the result level). Since
    `tag` is
    a `KEYWORD(commas=True)` field, that forward-analysis step splits
    whoosh-compat's still-unsplit `"foo,bar"` term on the comma too,
    collapsing the two sides to the same tree before the comparison this
    project's differential harness runs ever sees a difference (confirmed
    directly: `tag:'foo,bar'` structurally matches under
    `tests/differential/test_differential.py::test_matches_oracle`, so this
    entry deliberately carries no allowlisted skip pattern of its own,
    unlike a typical differential-only divergence). The design choice itself, and
    its result-level irrelevance, are still real and still covered
    directly: `tests/test_parser_fields.py`/`tests/test_plugins_unit.py`
    pin the raw parse-time distinction (`is_quoted`, an unsplit `Term`),
    and `tests/emitter/test_acceptance_e2e.py` covers the result-level
    equivalence entry 16 describes.

    Test references: `tests/differential/corpus_docs.txt`'s `tag:'foo,bar'`
    line (compared normally, not allowlisted); entry 16 above (this
    AST-level divergence doesn't change this project's fixture's actual
    search results, since the emitter re-runs the field's own
    comma-splitting `analyzer` over the quoted literal's text at *emit*
    time anyway).

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
    search for any other term/pattern: the parser's
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

22. **JSON-subpath `index.parse_query` fallback: `AND`/`OR` now honor true
    combinator semantics for a `Term` value (fixed as a structural
    consequence of promoting analysis to its own pipeline stage); `PHRASE`
    and a genuine `Phrase` node remain single-leaf-limited (a real,
    remaining limitation of the fallback path, not the general design).**
    `Multitoken` resolution for a `Term` value now happens once, in
    `whoosh_compat.ast.analyze()`, *before* emission: a multi-token
    `Multitoken.AND`/`OR` value is already rewritten into an `And`/`Or` of
    separate single-token `Term` nodes by the time `TantivyEmitter` ever
    visits it, each addressing the same JSON subpath independently.
    `visit_and`/`visit_or` then combine whatever query each single-token
    `Term` produces, including one built through the `index.parse_query`
    fallback (`TantivyEmitter._emit_json_term`, taken when the installed
    tantivy-py cannot address a JSON subpath directly via `term_query`, see
    the JSON `parse_query` carve-out in `ARCHITECTURE.md` §5): two or more
    separate `index.parse_query`-backed single-token queries, `Must`/
    `Should`-combined by the ordinary boolean-query machinery, give real
    AND ("all tokens present, any order/position") or OR ("any token
    present") semantics, not the single quoted, space-joined,
    phrase-shaped leaf this fallback used to collapse to. Verified directly
    against a live index: `Multitoken.AND` over two tokens present in a
    document's JSON subpath value but in the *reverse* order from the
    query text still matches (a phrase-shaped collapse would not have).
    `Multitoken.FIRST` is unaffected (`analyze()` already resolves it to a
    single token, exactly as before this fix).

    `Multitoken.PHRASE` over a bare `Term` value, and a genuine quoted
    `Phrase` node, are unaffected by this fix and remain limited: both are
    represented as a single `Phrase` AST node (`analyze()` never explodes a
    PHRASE-mode value or a quoted phrase into separate `Term`s, since a
    phrase's words must stay together as one ordered unit, not independent
    combinable clauses), so `TantivyEmitter._emit_json_phrase`'s fallback
    branch still has only one `index.parse_query` call to make, with no way
    to build a JSON-subpath *phrase* query (word order/adjacency, and slop)
    programmatically. `index.parse_query`'s single-leaf call has no
    programmatic way to build that the way `Query.phrase_query` does for
    every other field kind; this remains a structural limitation of the
    carve-out itself, not something the analysis-pipeline refactor could
    also resolve. Once tantivy-py#716 lands and ships,
    `_json_paths_supported()` starts returning `True` and this whole
    fallback branch (including this remaining phrase limitation) stops
    being taken.

    Test references: `tests/emitter/test_emit_json.py`'s
    `test_json_subpath_parse_query_fallback_honors_multitoken_first` (still
    pins `Multitoken.FIRST`) and
    `test_json_subpath_parse_query_fallback_honors_multitoken_and_or`
    (new: pins the AND/OR fix directly, including the reversed-token-order
    case that a phrase-shaped collapse would have missed).

    This remaining limitation is about a `Phrase` node (or a PHRASE-mode
    `Term` value) only. A quoted `Phrase` node on
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

    **A second, separate consequence of this same fallback, worth recording
    ahead of the retirement it's tied to: the fallback currently gives a
    JSON subpath term free, tantivy-native type inference that the future
    programmatic path will not.**
    `_emit_json_term`'s `index.parse_query` call
    (`f'{full}:"{escaped}"'`, line above) hands the analyzed text to
    tantivy's own query-string grammar, which resolves a JSON leaf the way
    tantivy's query parser always does: trying a fast-value (numeric,
    boolean) interpretation *and* a tokenized-text interpretation and OR-ing
    them together. So `attrs.value:100` against a document that stored
    `attrs.value` as the JSON number `100` matches today, and so does the
    same query against a document that stored it as the JSON string
    `"100"`; `attrs.flag:true` matches a JSON boolean `true` the same way.
    None of this is code whoosh-compat wrote; it's free behavior inherited
    from handing the string to `index.parse_query`.

    The `_json_paths_supported()` branch above it builds a plain
    `Query.term_query(schema, full, token)` call with a `str` token: a
    single, explicitly `Str`-typed term, with no equivalent fast-value/text
    union. Once tantivy-py#716 ships and the probe starts returning `True`,
    every JSON subpath term (not just ones that happen to look numeric or
    boolean) moves onto this path automatically, with no code change in
    this library to review or catch the difference: a numeric or boolean
    JSON subpath value (paperless-ngx custom fields values are the
    motivating real-world case) would silently stop matching, with no test
    failure locally to flag it unless the receiving project's own test
    suite happens to cover exactly that shape.

    Verified directly against the tantivy source: the primitive behind this
    is `generate_literals_for_json_object`
    (`src/query/query_parser/query_parser.rs`), which returns more than one
    literal, a fast-value interpretation and a tokenized-text one, and ORs
    them; there is no single-`Term` API that can express that union, so
    this is a structural limitation of the target API, not an
    implementation gap tantivy-py#716 will close on its own. This means
    the fallback may need to survive, scoped narrowly, even after the rest
    of this carve-out retires; see the retirement checklist this entry
    points to. This is documentation only: no code changes here, since the
    decision between "keep a narrow fallback forever" and "teach the
    programmatic branch to replicate the inference deliberately" is a
    larger design call this entry deliberately leaves open rather than
    deciding without a real host to evaluate it against.

    Retirement tracking: `.claude/skills/carve-out-retirement/SKILL.md`'s
    JSON carve-out row now carries the same note: before treating a
    tantivy-py version bump that flips `_json_paths_supported()` to `True`
    as safe to ship, re-verify that non-string (numeric/boolean) JSON
    subpath values still match correctly under the programmatic branch.

23. **`NOT` of a term whose analyzer drops every token matches every
    document here, but matches none in real whoosh (confirmed divergence,
    not fixed).** `whoosh_compat.ast.analyze()` drops a zero-token TEXT/
    KEYWORD `Term`/`Phrase` (e.g. an all-stopword value) to `ast.Nothing()`
    as part of its own analysis pass, leaving `Not(Nothing())`;
    `analyze()` finishes by calling `normalize()`, whose pre-existing
    `Not(Nothing) -> Every()` rule then converts that into "matches
    everything", the *natural* consequence of running token-drop analysis
    ahead of a normalize pass that already had this rule, not a special
    case `analyze()` implements for `NOT` specifically (see `analyze()`'s
    own docstring, which names this exact case explicitly so a future
    implementer doesn't "fix" it by changing drop semantics). Before
    analysis became its own explicit pipeline stage, the identical outcome
    came from a narrower mechanism confined to the emitter itself
    (`visit_term`'s zero-token branch returning `Query.empty_query()`, and
    `visit_not` wrapping that in `MustNot(...)` without going through the
    emitter's own zero-token-drop check, which only applied to direct
    And/Or children); the result was, and still is, `NOT` of such a term
    excludes nothing and therefore matches every document.

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
    the same-shaped result: that rule itself is a deliberate, pre-existing
    design choice with no claim to whoosh parity (whoosh's own rule for the
    equivalent AST-level case is also "stays nothing", the same direction
    as the analysis-driven case documented here). The case documented in
    this entry is purely an *analysis-time* phenomenon: an `ast.Term` that
    is syntactically ordinary (not an `ast.Nothing` node in the tree
    `parse()` produced) but whose configured `analyzer` happens to consume
    its text entirely once `analyze()` runs over it, a fact the earlier
    `normalize()` call in the pipeline (`analyze(normalize(node), ...)`)
    has no visibility into, since normalization runs before analysis, on
    still-raw text. `visit_term`'s docstring used to describe this as an
    an emit-time phenomenon before analysis was promoted to its own
    pipeline stage; it now points here, and to `analyze()`'s own docstring,
    instead.

    Decision: documented, not changed, matching this project's judgment
    call. Changing only the analysis-time case to match whoosh (while
    leaving `ast.normalize()`'s already-established, unrelated
    `Not(Nothing) -> Every` parse-time rule as-is) would make two
    structurally identical situations, a `NOT` whose operand turns out
    empty, behave differently depending purely on *when* the emptiness was
    discovered (parse-time `Nothing` node vs. analysis-time zero-token
    result), which is a timing artifact a query author has no way to
    predict or control. Uniform behavior regardless of when the emptiness
    was discovered, even though it disagrees with whoosh, is more
    predictable than a rule that depends on an implementation detail.

    Test references: `tests/emitter/test_emit_boolean.py`'s
    `test_not_zero_token_term_matches_everything`. The grammar-aware
    property fuzzer (`tests/differential/strategies.py`,
    `test_hypothesis.py::test_fuzz_grammar_matches_oracle`) later found that
    this same divergence also reaches the *differential AST-comparison*
    layer, not just the emitter: the differential harness's own
    forward-analysis step (originally a separate, hand-synchronized
    `oracle.analyze_ast` helper, since replaced by a direct call to the
    real `whoosh_compat.ast.analyze()`, the same function `TantivyEmitter`
    calls) turns a `NOT`'s now-empty child into `Nothing()`, which
    `ast.normalize()`'s pre-existing `Not(Nothing) -> Every` rule then
    upgrades to `Every()`, landing on the same "matches everything" shape
    this entry already describes. Allowlisted in
    `tests/differential/allowlist.py` (a `NOT` directly wrapping a single
    known-zero-token TEXT-field value) rather than treated as a new
    divergence, since the underlying behavior is this same entry.

    The same accepted tradeoff extends to `ANDNOT`/`ANDMAYBE`/`REQUIRE`'s
    positive/required/scored operand, found by the acceptance-layer result
    property (`tests/emitter/test_acceptance_property.py`), not just bare
    `NOT`: `whoosh_compat.ast.analyze()` applies one uniform rule (see its
    own docstring, and the `_analyze_binary_drop` helper it delegates to)
    for all three of `AndNot`/`AndMaybe`/`Require`: a side that newly drops
    to zero tokens *during this analysis pass* lets its sibling stand alone,
    regardless of which side dropped, distinct from a genuinely pre-existing
    `Nothing()` operand (which still follows `ast.normalize()`'s ordinary
    whoosh-matching poison/absorb rule, entry 27's positive/required-null
    handling). Before analysis became its own pipeline stage, the identical
    outcome came from a narrower, emitter-only mechanism confined to
    `visit_andnot`/`visit_andmaybe`/`visit_require`, which shared a helper
    that dropped a zero-token side and let the other stand alone at emit
    time; the result was, and still is, the same "discovered here, not at
    parse time, so `ast.normalize()`'s `AndNot`/`AndMaybe`/`Require` rules
    never get a chance to run for a value that only became empty later"
    mechanism as bare `NOT`. Confirmed directly this also means
    whoosh-compat's behavior depends on nothing about *when* whoosh happened
    to eliminate the operand, unlike real
    whoosh, whose own behavior for this shape turns out to depend on
    parenthesization: `title:the ANDNOT content:foo` (bare, unparenthesized)
    drops `title:the` at the syntax level before `ANDNOT` ever binds,
    leaving `content:foo` standing alone, exactly matching whoosh-compat's
    "leave the survivor" rule and already verified as agreeing
    (`test_zero_token_operand_leaves_the_survivor`); but
    `(title:the) ANDNOT (content:foo)` (each operand explicitly
    parenthesized, the shape `strategies.py`'s `_extend` combinator always
    produces) instead keeps a live `AndNot(And([]), ...)` tree whose
    empty-`And` positive side matches zero documents when actually searched
    (verified directly against a live oracle index), dragging the whole
    `AndNot` down to zero regardless of the negative side, the ordinary
    "AND with an unsatisfiable clause" consequence of entry 27's own
    positive-null rule, not a new one. whoosh-compat's uniform, timing-
    independent policy is kept for the same predictability reason this
    entry already gives; it was not previously known to be reachable
    through `ANDNOT`/`ANDMAYBE`/`REQUIRE`'s parenthesized form specifically
    until the result-level property found it.

    Test references: `tests/emitter/result_allowlist.py`'s
    ANDNOT/ANDMAYBE/REQUIRE-parenthesized-zero-token-operand entry;
    `tests/emitter/test_acceptance_property.py`'s
    `test_andnot_zero_token_positive_matches_everything_here`.

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
    defers all analysis to a pipeline stage after parsing (see
    ARCHITECTURE.md's "analyzer contract" invariant): `parse()`'s
    `ast.Phrase` keeps the raw, unanalyzed text, and
    `whoosh_compat.ast.analyze()` (called both by `TantivyEmitter.emit()`
    and, for the differential harness's purposes, directly by the harness
    itself, the same function either way) drops the phrase from its
    enclosing group entirely once analysis reduces it to zero tokens, the
    same rule already applied to a zero-token plain `Term` (see
    `analyze()`'s docstring). The two sides therefore build structurally
    different trees
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
    (design, found by the property-based fuzzer during the empty-group-drop
    work).**
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
    whoosh crash (whoosh-bug).** Real whoosh's `PhrasePlugin`
    calls `field.process_text()` on every quoted value regardless of field
    type, and `whoosh.fields.BOOLEAN` has no analyzer at all
    (`BOOLEAN.tokenize` raises `Exception("... field has no analyzer")` for
    any input, not something specific to `"*"`): `has_tag:"*"` against the
    real oracle raises a bare `Exception` while parsing, before a query
    object is even built. This is a defect in whoosh's own `BOOLEAN` field
    type, not intended semantics, so it is not reproduced: whoosh-compat's
    `visit_phrase` treats a double-quoted `"*"` on a `BOOLEAN_EXISTS` field
    the same as the single-quoted and unquoted forms, an existence match
    (see entry 27's neighbor, the quoted-star existence special case's
    `_exists_query`/`Every` redirect
    through `exists_target`). The single-quoted form (`has_tag:'*'`) does
    not crash real whoosh (`BOOLEAN.parse_query` special-cases `"*"`
    directly, matching whoosh-compat's own `term_query` fix) and is
    corpus-compared normally.

    Test references: `tests/differential/corpus_docs.txt`'s quoted-star
    section (only the single-quoted and numeric forms are corpus lines, for
    exactly this reason); `tests/emitter/test_emit_phrase.py`'s
    `test_quoted_star_phrase_matches_unquoted_star` and
    `tests/emitter/test_emit_boolean.py`'s
    `test_every_field_on_the_boolean_exists_field_itself` cover the
    double-quoted and bare-star forms directly, without the oracle.

29. **A wildcard/prefix pattern on a numeric field or a BOOLEAN_EXISTS field
    is diagnosed at parse time, not silently mangled (whoosh-bug, not
    reproduced; the unsupported-pattern diagnostic, extended to the
    BOOLEAN_EXISTS case).**
    Real whoosh's `WildcardPlugin`, for a NUMERIC field, silently drops the
    wildcard character(s) and searches whatever's left as a literal exact
    value: confirmed directly against the oracle, `type_id:1*` parses to
    `Term('type_id', <bytes for the int 1>)`, not a rejected query and not
    an actual wildcard search. A user typing `asn:1*` almost certainly
    means "starts with 1", and getting silently narrowed to "is exactly 1"
    with no error is a defect, not intended semantics, so it is not
    reproduced. whoosh-compat instead reports a `DiagnosticKind.UNSUPPORTED_PATTERN`
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
    and the quoted-star existence special case) is unaffected: this entry
    is specifically about a
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
    `tests/differential/corpus_docs.txt`'s unsupported-pattern section (skips via the
    existing entry 6 diagnostics-present check, same as any other
    parse-time diagnostic).

30. **A wildcard/prefix pattern on a JSON subpath is diagnosed at parse
    time rather than silently querying the whole field's encoded bytes
    (tantivy-py gap, not a whoosh divergence).** Unlike entry
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

    whoosh-compat reports the same `DiagnosticKind.UNSUPPORTED_PATTERN` diagnostic and
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
    it keeps routing to the subpath-aware existence check.

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

32. **A `date_only` field's exclusive upper bound rounds up, not down, when
    truncation would otherwise move it backwards.** `_to_utc()`
    (`parser/dateparse.py`) collapses a `date_only` field's bounds to
    UTC-midnight calendar days, since only the calendar date matters for
    such a field. Naively truncating an exclusive upper bound (`incl_hi
    =False`, the half-open ceiling shape described above) down to its own
    day's midnight is wrong whenever the untruncated value carried any
    time-of-day precision: the bound was computed as a ceiling one
    microsecond past some period's end, so truncating it down moves it to
    at or before where it started, either collapsing the range to an empty
    `[midnight, midnight)` or silently dropping the named end day from a
    multi-day range. `_to_utc` now ceils such a bound to the *next* day's
    midnight instead, whenever `ceil=True` (passed only for the hi side of
    an exclusive bound) and the naive datetime has a nonzero time-of-day; a
    bound already exactly at midnight is left alone, since it is already
    day-aligned and rounding it up would over-widen the range by an extra
    day it was never asked to cover. The lo bound, and any both-inclusive
    exact-instant hi (an exact instant like `noon`, which resolves to a
    zero-width timespan rather than a half-open period), keep truncating
    down unchanged.

    This also fixes an internal inconsistency between a degenerate exact
    instant (`created:noon`, both-inclusive, always worked) and an
    hour-precision period value (`created:'3pm'`, half-open, used to
    collapse to an empty range) on the same `date_only` field: both now
    resolve to a range covering the same calendar day.

    Not a real-whoosh divergence: the fork's own `_to_utc` docstring already
    states the `date_only` contract ("only the calendar date matters"), and
    real whoosh's DATE-field concept does not map onto this the same way
    (against the oracle these shapes either return `NullQuery` or collapse
    to a whole-year range via entry 12's partial-match defect), so this is
    whoosh-compat holding itself to its own stated contract rather than
    matching or diverging from whoosh. The differential layer registers
    `created` as DATETIME (`tests/differential/oracle.py`), which
    structurally cannot exercise `date_only` truncation, so this has no
    differential-corpus counterpart; coverage is direct unit/emitter tests
    instead.

    Test references: `tests/test_parser_dates.py`'s
    `test_date_only_time_bearing_single_value_ceils_hi_to_next_day`,
    `test_date_only_range_time_bearing_end_bound_includes_named_end_day`,
    `test_date_only_range_time_bearing_start_bound_still_truncates_down`,
    `test_date_only_same_day_range_times_on_both_ends_is_not_empty`,
    `test_date_only_whole_day_value_still_ceils_exactly_once`, and
    `test_date_only_noon_and_3pm_consistently_cover_their_day` (AST-level);
    `tests/emitter/test_emit_ranges.py`'s
    `test_date_only_time_bearing_single_value_matches_the_named_day`,
    `test_date_only_range_time_bearing_end_bound_includes_named_end_day`,
    `test_date_only_range_time_bearing_start_bound_still_truncates_down`,
    `test_date_only_same_day_range_times_on_both_ends_matches`, and
    `test_date_only_noon_and_3pm_consistently_match_their_day` (result-level,
    real tantivy searches asserting doc-id sets).

33. **A whitespace-padded quoted value on a BOOLEAN_EXISTS field reads False
    in whoosh-compat but True in real whoosh (design, not reproduced).**
    Real whoosh's `BOOLEAN._obj_to_bool` checks the *unstripped* lowered
    query text against its `trues`/`falses` frozensets
    (`t true yes 1` / `f false no 0`) and, for anything that doesn't match
    exactly, falls through to plain `bool(qstring)`: any non-empty string is
    truthy under that fallthrough, so a padded value like
    `has_tag:'  false  '` or `has_tag:'F '` (whitespace after the `f`, so
    the lowered text isn't exactly `"f"`) reads True on the real-whoosh
    side, confirmed directly against the oracle. `QueryParser.term_query`'s
    BOOLEAN_EXISTS branch (`parser/default.py`) strips and lowercases the
    text before the same membership check, so identical padded text reads
    False on whoosh-compat's side instead: stripping first was chosen so a
    hand-typed value with incidental leading/trailing whitespace behaves
    the same as its trimmed form, which is judged the more predictable
    reading for a boolean-shaped field, not something worth chasing parity
    on. The emitter's `_is_truthy` (`emitters/tantivy_.py`) applies the
    identical stripped rule, since a hand-built `ast.Term` that bypasses the
    parser must agree with whatever the parser would have produced for the
    same text (a pre-existing invariant, not new here).

    A quoted *literally empty* value (`has_tag:''`) is not part of this
    divergence: whoosh's `bool("")` fallthrough is False, agreeing with
    whoosh-compat's empty-after-strip falsy rule. A quoted
    *whitespace-only* value (`has_tag:'  '`) IS part of it: whoosh never
    strips, so its fallthrough sees `bool('  ')`, True, while
    whoosh-compat strips down to the empty string, False; the allowlist
    regexes (both layers) cover that spelling alongside the padded ones. Before this, whoosh-compat's rule
    read `has_tag:''` as True (empty string is `not in` the falses tuple),
    which was a genuine bug, not an intended divergence; it is now fixed
    and compared normally against the oracle rather than allowlisted.

    This divergence applies uniformly to every registered `BOOLEAN_EXISTS`
    field, not just `has_tag`: the allowlist entry's field alternation was
    originally scoped to `has_tag` only (the sole field the hand-curated
    corpus lines exercised), and was broadened to all six registered
    `BOOLEAN_EXISTS` fields (`has_correspondent`/`has_tag`/`has_type`/
    `has_path`/`has_custom_fields`/`has_owner`)
    after the expanded generator's `_bool_exists_quoted_atom` (unlike the
    old corpus lines, drawing from every registered BOOLEAN_EXISTS field)
    found the identical mismatch on `has_correspondent` and confirmed
    directly it reproduces on the others as well (`has_path` was confirmed
    last, by the acceptance-layer result property's generator): the root cause
    (`term_query`'s strip-before-check vs `BOOLEAN._obj_to_bool`'s
    unstripped-then-`bool(qstring)` fallback) is the same code path
    regardless of which field it runs on.

    Test references: `tests/test_parser_fields.py`'s
    `test_bool_word_truthy_check_strips_whitespace` and
    `test_bool_word_empty_after_strip_is_falsy`;
    `tests/emitter/test_emit_terms.py`'s
    `test_boolean_exists_raw_string_text`;
    `tests/emitter/test_emit_boolean.py`'s
    `test_boolean_exists_quoted_truthiness_shapes` (result-level, real
    tantivy searches asserting doc-id sets, covering the quoted-empty,
    padded-false, padded-true-looking, and plain true/false control
    shapes); `tests/differential/allowlist.py`'s matching entry;
    `tests/differential/strategies.py`'s `_bool_exists_quoted_atom`; and
    `tests/differential/corpus_docs.txt`'s padded-value lines.

34. **A year at the edge of what `datetime` can represent (`0000`, `9999`) is
    diagnosed rather than silently clamped to whoosh's ceiling (design).**
    Real whoosh executes `created:9999`
    and `created:[2020 TO 9999]` by resolving the year to `datetime.max`
    (`9999-12-31T23:59:59.999999`) as an *inclusive* ceiling, confirmed
    directly against the pinned oracle. whoosh-compat's `DateParserPlugin`
    instead represents an ambiguous/period date as a half-open range whose
    exclusive upper bound is the period's ceiling plus one microsecond (see
    the "half-open date-range ceilings" invariant in `ARCHITECTURE.md`):
    for year 9999 that arithmetic overflows past `datetime.max`, an
    `OverflowError` `text_to_node`/`range_to_node` (`parser/dateparse.py`)
    catch and turn into a `BAD_DATE` `Diagnostic` the same way any other
    unparseable date is reported, rather than special-casing the ceiling
    year to keep it inclusive.

    This is a deliberate choice, not an oversight: whoosh-compat offers a
    true open-ended range (`created:[2020 TO]`, entry 10 above) as the
    correct way to express "from 2020 onward", so a sentinel year that
    exists only to fake an open bound has a real replacement to point users
    at, and the diagnostic message can do exactly that. Year `0000` is
    diagnosed unconditionally regardless of this policy, since no
    `datetime` representation of year 0 exists at all. Confirmed directly
    against the oracle: real whoosh's `DateParserPlugin.text_to_dt` for a
    bare `created:0` value degrades to a silent `NullQuery` rather than
    raising, so this side of the pair is already covered by entry 1's
    general "invalid dates yield diagnostics, real whoosh: silent empty
    results" divergence; this entry exists for the year-9999 half of the
    pair, which entry 1 does not explain on its own.

    An earlier commit message justified keeping the diagnostic by claiming
    "sentinel years are a real habit in stored queries"; that claim was
    checked while writing this entry and has no independent support (the
    paperless-ngx query corpus, `tests/differential/corpus_paperless.txt`,
    contains no year-9999 line, and entry 10's own sentinel mention is about
    a naive string-translation layer's machine-generated queries, not
    anything a user actually types or stores). The rationale above, "this
    diagnostic exists because a real open bound is the better answer", is
    the one that holds up instead.

    Test references: `tests/test_parser_dates.py`'s
    `test_years_outside_the_representable_range_diagnose` and
    `test_range_out_of_range_diagnostic_names_the_failing_bound` already
    pin this diagnostic behavior directly; no new test was needed for this
    entry.

35. **`NOT NOT alpha` (and any other run of two or more consecutive bare
    `NOT`s) parses instead of crashing (whoosh bug, not reproduced).** Real
    whoosh raises a bare `IndexError` while
    parsing `NOT NOT alpha`, `NOT NOT NOT alpha`, and `alpha NOT NOT beta`,
    confirmed directly against the pinned oracle. The cause is
    `Wrapper.query`'s (`whoosh/qparser/syntax.py`) unguarded
    `self.nodes[0]`: a second, inner `NOT` produces a `NotGroup` with no
    child of its own (the outer `NOT` consumed what would have been its
    operand), so by the time the inner wrapper's `query()` runs, its
    `nodes` list is empty and the plain indexing raises. This is a defect
    in whoosh's own grammar wiring, not intended semantics (nothing about
    whoosh's design calls for a double negative to crash instead of
    cancelling out), so it is not reproduced.

    whoosh-compat's `Wrapper.query` (`parser/syntax.py`) already guards this
    exact shape with `if not self.nodes: return None`, which makes the
    empty inner `NotGroup` contribute nothing to its enclosing group instead
    of raising: `NOT NOT alpha` correctly parses as plain `alpha`, `NOT NOT
    NOT alpha` as `NOT alpha`, and `alpha NOT NOT beta` as `alpha AND beta`.
    This was already the right behavior before this entry was written; the
    guard's own comment only mentioned a different shape it also happens to
    cover (`NOT AND x`, an operator consuming the word a wrapper would have
    wrapped), so this divergence went undocumented until now.

    There is no allowlist/corpus triple for this one: real whoosh raises
    before a query object exists, so there is no oracle-side AST to compare
    against and no differential corpus line can exercise it (the harness
    has no mechanism for an oracle-side exception the way it does for a
    `NullQuery`/diagnostic). Not carried through the differential-triage
    allowlist convention for that reason, the same way entry 31 documents
    for the nesting-depth cap.

    Test references: `tests/test_parser_basics.py`'s
    `test_consecutive_bare_nots_parse_instead_of_raising`.

36. **A comma-values field boost (`tag:alpha,beta^2`) attaches to the whole
    split group in whoosh-compat, but to each split term individually in
    real whoosh (design, AST-level only).**
    whoosh-compat's `CommaValuesPlugin.do_comma_values`
    (`parser/plugins.py`) runs at `priorities.FILTER_COMMA_VALUES` (105),
    before `BoostPlugin.do_boost` runs at `FILTER_BOOSTS_POST` (510): the
    comma split happens first, building an `AndGroup` of the two terms, and
    the boost then attaches to that whole group as its single preceding
    node. `tag:alpha,beta^2` therefore parses to
    `Boosted(And(Term('tag','alpha'), Term('tag','beta')), 2.0)`.

    Real whoosh has no comma-splitting parser plugin at all (entry 17 above
    covers this in more detail): a `KEYWORD(commas=True)` field's own
    analyzer splits on commas at *analysis* time, long after `BoostPlugin`
    has already attached the boost to the single, still-unsplit term node.
    Confirmed directly against the pinned oracle:
    `tag:alpha,beta^2` parses to
    `And([Term('tag', 'alpha', boost=2.0), Term('tag', 'beta', boost=2.0)])`,
    the boost riding each split term individually rather than the group as
    a whole.

    This has no result-level consequence for this project's fixtures: the
    matched-document sets are identical either way (both shapes require
    every split term to match, an ordinary AND), and the two scoring shapes
    are algebraically equal under tantivy's boolean-query scoring, which
    sums each `Must` clause's own score: `Boost(c, And(a, b))` scores
    `c * (score(a) + score(b))` for a matching document, while
    `And(Boost(c, a), Boost(c, b))` scores `c*score(a) + c*score(b)`, the
    same value by simple distributivity, for any constant `c` and any
    per-term score. Verified live, not just algebraically: a tantivy index
    with `body: "alpha beta"` scores an identical top hit
    (`1.150728464126587`, exactly double the unboosted `And`'s
    `0.5753642320632935`) for both `Query.boost_query(And(a, b), 2.0)` and
    `And(Query.boost_query(a, 2.0), Query.boost_query(b, 2.0))`.

    The allowlist entry's field alternation is derived from the registry's
    `comma_values` flags (a hand-written `tag`-only scope once silently
    missed the `tag_id`/`custom_fields_id`/`viewer_id` sibling cells; see
    entry 46, this mechanism's analyzer-split sibling, for the broadening
    history).

    Test references: `tests/differential/allowlist.py`'s comma-values-boost
    entry; `tests/differential/corpus_docs.txt`'s
    `tag:alpha,beta^2` line and `tests/differential/corpus_paperless.txt`'s
    `tag_id:alpha,beta^2` line.

37. **A `date_only` field is a whoosh-compat-only concept with no whoosh
    analogue whatsoever (design).** Real v2 whoosh (`whoosh.fields.DATETIME`)
    has no date-vs-datetime distinction at all: every date/time field is a
    `DATETIME`, and a bare date value like `created:2020-03-15` is simply a
    `DATETIME` value whose time-of-day component happens to be unspecified
    (handled as an *ambiguous* period, per whoosh's own `adatetime`/`timespan`
    machinery), never a type-level rejection of a time-bearing value.
    whoosh-compat's `FieldSpec.date_only=True` (`fields.py`) is a real,
    additional constraint with no whoosh counterpart: a `date_only` field's
    range/ceiling handling (DIVERGENCES.md entry 32) and, more basically, its
    acceptance of a bare date literal without a time-of-day component at all
    are whoosh-compat-only behavior.

    `tests/differential/oracle.py`'s `ORACLE_REGISTRY` registers a
    `release_date` field (`FieldKind.DATE, date_only=True`) purely so
    `tests/differential/strategies.py`'s generator (`_date_only_atom`) can
    reach "time-bearing value on a date-only field" vocabulary at all: this
    field has no counterpart in `oracle_schema()`/`V2_FIELDS`, so every query
    addressing it structurally diverges from the oracle's ordinary
    default-multifield-unknown-field expansion, regardless of the value
    (confirmed directly for a bare date, a time-bearing bare value, and a
    time-bearing bracket range: all three mismatch identically), the same
    "whoosh-compat-only concept" shape entry 14 documents for JSON fields.

    Test references: `tests/differential/allowlist.py`'s `release_date:`
    entry; `tests/differential/strategies.py`'s `_date_only_atom` (generator
    reachability); the `date_only` rounding-direction behavior itself is
    exercised at the unit/emitter level by entry 32's own test references,
    not by this differential-comparison entry.

38. **A double-quoted value on a BOOLEAN_EXISTS field crashes real whoosh
    (whoosh bug, not reproduced).** `whoosh.fields.BOOLEAN.__init__` never
    sets an `analyzer` (confirmed directly: `BOOLEAN().analyzer` raises
    `AttributeError`), but `whoosh.qparser.plugins.PhrasePlugin.PhraseNode.query`
    unconditionally calls `fieldobj.process_text(...)`, which needs the
    field's own analyzer to tokenize the quoted text, regardless of whether
    the field kind can even meaningfully hold a multi-word phrase. Confirmed
    directly against the pinned oracle: `has_tag:""`, `has_tag:"true"`,
    `has_tag:"  false  "`, and `has_tag:"t*"` (empty, valid, whitespace-padded,
    and pattern-shaped double-quoted values) all raise
    `Exception: <class 'whoosh.fields.BOOLEAN'> field has no analyzer` while
    parsing, for every registered `BOOLEAN_EXISTS` field
    (`has_correspondent`/`has_tag`/`has_type`/`has_path`/
    `has_custom_fields`/`has_owner`), not just one. This is a genuine whoosh limitation (a `BOOLEAN` field
    simply cannot support a phrase query at all, in any form), not intended
    semantics that a query language would deliberately reject a specific
    value shape for, so whoosh-compat does not reproduce it: a double-quoted
    value on a `BOOLEAN_EXISTS` field parses to an ordinary `ast.Phrase` and
    is coerced to a boolean at emit time (`visit_phrase`, the same
    truthiness rule entry 32 documents for the unquoted/single-quoted form),
    see `tests/emitter/test_emit_phrase.py::test_phrase_on_boolean_exists_field`.

    Test references: `tests/differential/allowlist.py`'s
    double-quoted BOOLEAN_EXISTS entry (all six registered fields,
    `has_path` included, `DivergenceKind.ORACLE_ERROR`); `tests/differential/strategies.py`'s
    `_bool_exists_double_quoted_atom`; `tests/emitter/test_emit_phrase.py::test_phrase_on_boolean_exists_field`
    (result-level: this shape already worked correctly on whoosh-compat's
    side before this entry existed, it was simply never compared against the
    oracle).

39. **The U64 domain accepts the full 64-bit range; real v2 whoosh's
    `NUMERIC` fields are 32-bit, and most of them signed on top of that
    (design, found by the expanded generator vocabulary).**
    `whoosh.fields.NUMERIC.__init__` defaults `bits=32` when not passed
    explicitly, and `oracle_schema()`'s clone of paperless-ngx v2 never
    overrides it for any of `id`/`asn`/`correspondent_id`/`type_id`/
    `path_id`/`num_notes`/`custom_field_count`/`owner_id`/`page_count`:
    confirmed directly, `_SCHEMA["asn"].bits == 32` for every one of them.
    The real per-field ceiling isn't uniform, though, since `NUMERIC` also
    defaults `signed=True`: `asn`/`num_notes`/`custom_field_count` pass
    `signed=False` explicitly (real max `2**32 - 1` = `4294967295`,
    confirmed via `_SCHEMA[name].signed`), but every other field above
    leaves `signed` at its default of `True` (real max only `2**31 - 1` =
    `2147483647`, half the unsigned range, the sign bit consuming the top
    bit of the same 32). Real whoosh therefore silently fails to parse any
    value at or above a given field's own ceiling (`oracle_parse("id:2147483648", ...)`
    -> `NullQuery`, confirmed directly, even though `2147483648` parses fine
    on the *unsigned* `asn` field), even though it is a perfectly valid
    non-negative integer either way. whoosh-compat's `FieldKind.U64` instead
    validates against the full 64-bit domain uniformly, for every U64 field
    regardless of the real schema's per-field signedness (`_parse_u64`,
    `parser/default.py`, `_U64_MAX = 2**64 - 1`), matching tantivy's actual
    u64 column type (the v3 schema this library targets), not v2's narrower,
    per-field 32-bit whoosh fields. This is not a bug on either side: v2's
    schema genuinely had these narrower (and inconsistent) real ranges, and
    v3's tantivy columns genuinely are uniformly 64-bit; whoosh-compat
    deliberately does not shrink its domain check to match the narrower,
    superseded, per-field-inconsistent schema. Confirmed each field's own
    boundary is exact: a field's real maximum (`4294967295` for the three
    unsigned fields, `2147483647` for the rest) parses identically on both
    sides; one past it is the minimal reproduction of the divergence.

    Test references: `tests/differential/allowlist.py`'s U64-field/large-value
    entry; `tests/differential/strategies.py`'s `_numeric_atom` and
    `_WHOOSH32_FIELD_MAX` (`whoosh32_max`/`whoosh32_overflow`/`u64_max`
    shapes, computed per field from `_SCHEMA` rather than assumed uniform,
    after `id:4294967295` was initially, incorrectly, expected to match);
    `tests/differential/corpus_docs.txt`'s
    `asn:4294967296` (unsigned field) and `id:2147483648` (signed field)
    lines.

40. **`NOT` of a group that recursively collapses to empty (nested empty
    groups, or a boost/paren wrapper around one) matches no documents here,
    but matches every document in real whoosh at nesting depth two or
    deeper (whoosh-bug, not reproduced; found by the acceptance-layer
    result property, `tests/emitter/test_acceptance_property.py`).**
    `GroupNode.query()`'s empty-group rule (`parser/syntax.py`) returns
    `None`, not `ast.Nothing()`, for a group whose children all resolve to
    `None` in turn: this is the same deliberate, pre-existing rule entry 27
    documents (restored to stop an empty group from becoming a live
    `Nothing()` node that would then propagate through `normalize()`'s
    And/Not algebra), and it applies uniformly regardless of nesting depth,
    so `NOT (())`, `NOT ((()))` and `NOT ((())^0.5)` all parse to a bare
    `ast.Nothing()` with no `Not` node at all (confirmed directly via
    `compat_raw_parse`): the `NOT` operator has nothing left to bind to by
    the time its operand has recursively collapsed away, so it is dropped
    along with the operand rather than ever reaching `normalize()`'s
    `Not(Nothing) -> Every` rule (entry 23).

    Real whoosh's own behavior for the equivalent shape is not uniform by
    depth, confirmed directly against the pinned oracle: `NOT ()` (a single
    level) also collapses to `_NullQuery` at parse time (matching
    whoosh-compat here), but `NOT (())` and deeper both parse to a real
    `And([Not(And([And([]) ...]))])` tree that, when actually **searched**,
    matches every document (an empty `And([])`, unlike the top-level
    special case, matches nothing when executed, so `Not` of it matches
    everything). This is an artifact of how whoosh's own `Not`/`And`
    matchers handle a structurally-nonempty-but-semantically-empty operand
    at execution time, not a coherent, intentional "NOT of nothing" design
    on whoosh's part (the depth-1 and depth-2+ cases already disagree with
    each other on whoosh's own side), so whoosh-compat does not chase it:
    its own uniform "collapses to nothing at every depth" behavior is more
    predictable, consistent with entry 27's existing precedent of keeping
    the deliberate empty-group-drops-out rule rather than reproducing
    whoosh's transitive per-operator quirks around it.

    This shape was previously invisible to `tests/differential`: real
    whoosh's own `And([])`-nested `Not` tree has no oracle-side null
    subquery for `oracle.to_ast`'s `Not`/`And` branches to collapse
    identically, so `to_ast` returns `None` (`unmapped_reason`'s
    "oracle-unmappable" case) and the comparison is skipped entirely rather
    than compared and passed; the AST-level harness never had a tree to
    disagree over. It took a real dual-index search, not a parsed-tree
    comparison, to notice the two sides actually return different document
    sets for this shape at all, the same "an AST-incomparable or
    AST-agreeing case can still be a result-level divergence" risk entry 16
    names, here in the more extreme direction of AST comparison being
    impossible rather than merely coincidentally agreeing.

    Test references: `tests/emitter/result_allowlist.py`'s
    `NOT\s*\((?=[^)]*\()[\s()0-9.^]*\)` entry;
    `tests/emitter/test_acceptance_property.py`'s
    `test_not_of_nested_empty_group_is_a_result_level_divergence` and the
    generated-query property (seeded with `NOT ((())^0.5)`).

41. **A `NumericRange` with a small-magnitude bound matches every document
    in real whoosh regardless of any document's actual field value
    (whoosh-bug, not reproduced; found by the acceptance-layer result
    property).** Confirmed directly against a live oracle index (four
    documents with `asn` values 100-103, no document with `asn` below 100),
    bisecting a single-value range (`asn:[N TO N]`) across a range of `N`:
    `N` at or below 10 matches every document in the index; `N` at or above
    20 correctly matches only documents whose `asn` equals `N` (i.e.
    nothing, for this fixture); the exact boundary between the two was not
    pinned further than "somewhere between 10 and 20", since the point is
    the defect's existence, not its precise threshold, and chasing that
    threshold further would just be more archaeology of a bug this project
    does not intend to reproduce either way. A bare term query for the same
    small value (`asn:0`) correctly matches nothing, so the defect is
    specific to `NumericRange`, not to how small integers are encoded in
    general; a range whose bounds are both **not** small (`asn:[100 TO
    101]`, `asn:[100 TO 100]`) also correctly matches only in-range
    documents, so the defect is not simply "any bracketed numeric range is
    broken" either, only ranges touching a small-magnitude bound. This
    looks like a defect in whoosh's own `NumericRange`/`sortable_int_to_bytes`
    range-matching machinery, most likely in how it decomposes a range into
    Lucene-style precision-step "shift tiers" for values with few
    significant bits (not investigated further at the whoosh source level,
    since the parity bar this project holds itself to is whoosh's
    *intended* semantics, and matching every document regardless of a
    query's actual bounds is not a coherent intended semantic for a range
    query under any reading). whoosh-compat's own numeric range handling
    already has an existing, oracle-verified regression test for a
    dissimilar-bounds case (`test_acceptance_e2e.py`'s
    `"asn:[100 TO 102]"` scenario) and correctly returns only the documents
    actually in range for every bound tested here, small or otherwise; it
    is not changed to reproduce this defect.

    Two further manifestations were confirmed directly, neither reducible to
    the "small bound" shape above. First, an *exclusive* bracket: `asn:{100
    TO N]` (exclusive lower bound, so doc 1's `asn == 100` should never
    match) correctly excludes doc 1 for `N` up to 120, but for `N` at or
    above 127 matches every document, including doc 1, regardless of any
    document's actual value. Second, while narrowing that down, a plain
    *inclusive* range with no small bound and no exclusivity at all:
    `asn:[101 TO N]` (`101` alone should already exclude doc 1) shows the
    identical break at `N == 127`, one past `126`. `127 == 2**7 - 1` in
    every broken case found here, consistent with a Lucene-style
    numeric-trie precision-step tier boundary, but confirmed reachable
    through at least three distinct, not-obviously-related bound shapes
    (a small single-value range, an exclusive bound, and an ordinary
    inclusive range whose upper bound alone crosses 127), not one
    identifiable pattern; not investigated further at the whoosh source
    level for the same reason the first manifestation wasn't. Given that
    breadth, the allowlist entry for this divergence gives up on narrowing
    to any one reproduction and instead treats every bracketed range on a
    U64 field as suspect. This is safe specifically because the acceptance
    property never strict-xfail-asserts an allowlisted generated example
    (see `tests/emitter/result_allowlist.py`'s module docstring); it would
    not be an acceptable allowlist scope for the AST-comparison layer's
    strict-xfail discipline, where an entry this broad could silently
    swallow an unrelated real regression.

    Test references: `tests/emitter/result_allowlist.py`'s `NumericRange`
    entry; `tests/emitter/test_acceptance_property.py`'s
    `test_numeric_range_small_bounds_is_a_whoosh_bug` and
    `test_numeric_range_exclusive_bound_is_a_whoosh_bug` (each still
    verifies both a broken and an agreeing bound directly, independent of
    the broad allowlist scope).

42. **`is_shared` is not a registered field; real v2 whoosh's schema had a
    `BOOLEAN` `is_shared` column (out-of-scope).** The v2 schema this
    project's oracle clones (`tests/differential/oracle.py`'s
    `oracle_schema()`) includes `is_shared=BOOLEAN()`, written at index
    time (derived from the document's viewer list,
    `is_shared=len(viewer_ids) > 0`). Its history in paperless-ngx: added
    by the "shared by me" filter feature (paperless-ngx#4859), whose
    server-built filter criterion (`query.Term("is_shared", True)`, never
    user-typed query text) was the field's only reader; that reader moved
    to the ORM in paperless-ngx#7507, leaving the column written but
    read by nothing for the rest of the whoosh era. After that, only a
    hand-typed `is_shared:true` query could ever address it, an accident
    of whoosh parsing any schema field rather than a supported feature. paperless-ngx's tantivy
    backend does permission filtering entirely outside whoosh-compat
    (`build_permission_filter()` constructs raw `tantivy.Query` objects),
    and its public search-field surface (`src/documents/search/_fields.py`)
    deliberately does not expose `is_shared`, so whoosh-compat's paperless
    registry (`_make_oracle_registry()`, mirroring that surface) omits it
    on purpose. There is also no `FieldKind` that could express it: the
    registry has `BOOLEAN_EXISTS` (an existence check against a target
    field) but no plain stored-boolean kind, and adding one for a field no
    consumer exposes would be speculative. Consequence: real v2 whoosh
    parses `is_shared:true` as a typed `Term(is_shared, True)`; whoosh-compat
    treats `is_shared` as an unknown field and demotes the text to a
    multifield default-field search, the same treatment any unknown field
    gets. A host that wants the v2 behavior back would add a
    `BOOLEAN_EXISTS`-style or future boolean kind field to its registry;
    nothing in the parser special-cases the name.

    Test references: `tests/differential/allowlist.py`'s `is_shared`
    entry; `tests/differential/corpus_paperless.txt`'s `is_shared:true`
    line. The unknown-field
    allowlist entry for DIVERGENCES.md entry 15 explicitly excludes
    `is_shared` from its unknown-field alternative so this distinct
    divergence (known-to-oracle vs unknown-to-compat) is not silently
    absorbed under entry 15's both-sides-unknown multitoken class.

43. **`TermRange` bounds keep the case the user typed; real whoosh
    case-folds them into the AST (design, entry 2's range sibling).** The
    same mechanism as entry 2, reached through a different node type:
    whoosh's `RangePlugin` runs the field's analyzer chain over each
    bracket-range bound with `tokenize=False` (`LowercaseFilter` still
    applies), so `title:[A* TO B]` parses to `TermRange(lo='a*', hi='b')`
    on the oracle side, while whoosh-compat's `TermRange` carries the raw
    `lo='A*', hi='B'` (no parse-time folding anywhere in whoosh-compat,
    the same principle entry 2 documents for `Wildcard`/`Prefix`
    patterns). Unlike entry 2 there is no emit-time counterpart to do the
    folding later: a text-field `TermRange` is unsupported at emit
    entirely (`visit_termrange` raises `UnsupportedQueryError`, entry 5),
    so the divergence is AST-level only and can never reach a search
    result. Previously this shape was silently absorbed by entry 2's
    allowlist regex (any token with an uppercase letter and a `*`/`?`),
    i.e. asserted-as-expected under paperwork describing a different node
    type; it now has its own entry, ordered before entry 2's so the range
    spelling matches the right reason first.

    Test references: `tests/differential/allowlist.py`'s bracketed-range
    entry (ordered before the entry-2 pattern entry);
    `tests/differential/corpus_paperless.txt`'s `title:[A* TO B]` line.

44. **Exact date-range bounds honor the typed `{`/`}` exclusivity; real
    whoosh's date ranges are always inclusive on both sides (whoosh-bug,
    not reproduced).** whoosh's `DateParserPlugin.range_to_dt` builds a
    `DateRangeNode` that takes no `startexcl`/`endexcl` parameters and
    whose `query()` constructs `query.DateRange` with default inclusive
    bounds, silently discarding the brackets the user typed (verified
    against the pinned oracle: `added:{now TO now}` parses inclusive-both
    in whoosh). whoosh's own `RangePlugin` captures the exclusivity flags
    and its `TermRange` honors them, so the drop is a `DateRangeNode`
    plumbing oversight, exactly parallel to the boost drop entry 3
    documents for the same node class, not intended query semantics.
    whoosh-compat's `_range_to_node` keeps the typed flags for bounds
    classified exact (a concrete datetime such as `now`, or a
    fully-specified instant); ambiguous bounds are period-shaped and get
    the half-open ceiling treatment regardless (the ARCHITECTURE.md
    half-open-ceilings invariant), so exclusivity honoring is only
    observable on exact bounds.

    Result-relevant: `added:{now TO now}` matches the instant in whoosh
    and nothing in whoosh-compat. The generators do draw `{`/`}` brackets
    (`strategies.py`'s `_date_range_atom`), but only around bare-year
    bounds, which are ambiguous and therefore never reach the
    exclusivity-honoring exact-bound path, so the observable divergence is
    pinned by a corpus line rather than fuzz coverage.

    Test references: `tests/differential/allowlist.py`'s
    exclusive-date-bracket entry (ordered before the entry-12 date-range
    entry, so the exclusivity spelling cites this entry rather than being
    absorbed under the tz-bypass paperwork);
    `tests/differential/corpus_paperless.txt`'s `added:{now TO now}` line;
    `tests/test_parser_dates.py`'s
    `test_range_exclusive_brackets_honored_for_exact_bounds` (the direct
    unit pin) and `tests/emitter/test_acceptance_e2e.py`'s
    `test_exclusive_exact_date_range_is_a_documented_divergence` (the
    executed result-level proof, whoosh matching the instant and tantivy
    matching nothing under identity tz conversion).

45. **A double-quoted separated-ISO date value parses to a working
    `DateRange`; real whoosh parses it to a `Phrase` that crashes the
    search (whoosh-bug, not reproduced).** The double-quoted sibling cell
    of entry 18's bare/single-quoted spellings, with a worse downstream:
    for `created:"2020-01-01"`, whoosh's `DateParserPlugin.text_to_dt`
    fails to fully parse the value (the same grammar-ordering limitation
    entries 12 and 18 describe), and `ErrorNode.query()` falls back to
    running the wrapped node's own `query()`. For the bare spelling that
    wrapped node is a term (entry 18's numerically-correct
    `NumericRange` via the field's self-parse); for the double-quoted
    spelling it is a `PhraseNode`, whose fallback builds
    `query.Phrase('created', ['2020-01-01'])`. That Phrase then RAISES at
    search time (`whoosh.query.QueryError: Phrase search: 'created' field
    has no positions`, measured against a live v2-schema index), so a v2
    user typing the quoted spelling got a hard error, not results.
    whoosh-compat's single grammar path parses the quoted value directly
    into the day- or month-period `DateRange` (matching the precision the
    user typed) and returns the documents the user meant.

    Test references: `tests/differential/allowlist.py`'s
    double-quoted-date entry; `tests/emitter/result_allowlist.py`'s
    matching entry (ordered before the dashed-token entry-15 pattern so
    the spelling cites this divergence, not the multitoken one);
    `tests/differential/corpus_paperless.txt`'s `created:"2020-01-01"`
    line; `tests/emitter/test_acceptance_e2e.py`'s
    `test_double_quoted_iso_date_is_a_documented_divergence` (executed:
    whoosh raises QueryError at search time, tantivy returns the matching
    document).

46. **A boost on an analyzer-split TEXT value attaches to the whole split
    group; real whoosh boosts each split term individually (design, entry
    36's analyzer-split sibling).** `title:foo-bar^2` reaches the same
    combining question entry 36 documents for comma values, through
    analysis instead of `CommaValuesPlugin`: whoosh-compat's `analyze()`
    splits the multi-token value inside the already-bound `Boosted`
    wrapper (`Boosted(And(title:foo, title:bar), 2.0)`), while real
    whoosh's field analyzer splits at query-build time, after the boost
    bound to the single unsplit term, so each split term carries its own
    boost copy (`And(Boosted(title:foo, 2.0), Boosted(title:bar, 2.0))`).
    Matched documents are identical either way (the boost algebra is the
    same distribution entry 36 already verified; confirmed end-to-end by
    the equal-results acceptance scenario below, and re-confirmed during
    review across seven boosted-split shapes), so this is an
    AST-shape-only divergence. One interaction worth naming: a boosted
    split value written inside a user-typed `OR` can produce different
    result sets, but that difference reproduces identically with the
    boost removed, i.e. it is entry 15's Or-context `Multitoken.DEFAULT`
    divergence, not this entry's boost placement. The allowlist regex's
    separator class is dash/comma/slash, the characters
    `StandardAnalyzer` actually splits on; a single dot between word
    characters stays one token (measured), so dotted-only spellings are
    deliberately not claimed.

    Entry 36's own allowlist regex was also originally scoped to `tag:`
    alone; it now derives its field alternation from the registry's
    `comma_values` flags, so the once-forgotten `tag_id`/
    `custom_fields_id`/`viewer_id` sibling cells are covered and cannot
    silently drop out again.

    Test references: `tests/differential/allowlist.py`'s boosted
    analyzer-split entry; `tests/differential/corpus_paperless.txt`'s
    `title:foo-bar^2` and `tag_id:alpha,beta^2` lines;
    `tests/emitter/test_acceptance_e2e.py`'s
    `boosted-analyzer-split-value` equal-results scenario.

47. **`ANDNOT` with a `NOT` operand produces incoherent results in real
    whoosh (measured); whoosh-compat computes ordinary boolean set
    algebra (whoosh-bug, not reproduced; result-level only, the parsed
    ASTs are identical on both sides).** whoosh's `query.Not` is a
    root-only construct by its own admission: `Not.matcher`'s source
    comment says it is "usually only called if Not is the root query"
    and that `And`/`Or` do special handling of `Not` subqueries, handling
    the binary queries (`AndNot`/`AndMaybe`/`Require`) do not perform
    (verified in source: all three wrap the child `Not.matcher` directly).
    The incoherence itself was MEASURED on `AndNot`; `AndMaybe`/`Require`
    probes with `Not` operands have so far all agreed with set algebra,
    and those two keywords are covered by the allowlist entry defensively
    via the shared root-only mechanism, not by observed misbehavior.
    Measured against a live index (four docs, `Not(tag:billing)` matching
    docs 2 and 4): `AndNot(Not(billing), Every())` returns doc 2 alone,
    which no consistent reading produces (subtraction gives the empty
    set; ignoring the negative gives docs 2 and 4); the deep-fuzz find
    `(NOT (created:'this year')) ANDNOT (NOT (0))` returns the positive
    side unchanged, ignoring a negative that matches every document;
    while other spellings (`AndNot(Not(billing), Not(billing))`, plain
    positives) subtract correctly. As with entry 41, the point is the
    incoherence's existence, not its precise trigger: chasing the exact
    matcher interaction further would be archaeology of a defect this
    project does not intend to reproduce. whoosh-compat's emitter builds
    compositional boolean queries (`Must`/`MustNot` with all-negative
    padding), and tantivy computes the correct set-algebra answer for
    every probed spelling.

    Result-level only: the parsed and normalized ASTs are identical on
    both sides (verified for the finding query), so no differential
    allowlist entry or corpus line is possible; the paperwork is the
    result-level allowlist entry plus the finding query seeded into the
    acceptance property's explicit examples.

    Test references: `tests/emitter/result_allowlist.py`'s
    NOT-with-binary-operator entry;
    `tests/emitter/test_acceptance_property.py`'s
    `test_not_under_andnot_is_a_result_level_divergence` (the strict
    proof: whoosh returns the positive side unchanged, tantivy computes
    the correct empty subtraction) and its `_SEED_QUERIES` line.

48. **A single-quoted `T`-separated datetime value parses to a working
    `DateRange`; real whoosh parses it to `_NullQuery`, matching nothing
    (whoosh-bug, not reproduced).** The single-value face of the RFC3339
    `T`/`Z` extension (see `parser/dateparse.py`'s module docstring):
    whoosh's `simple` grammar sequence has no `T` in its separator class,
    its `text_to_dt` fails on `'2026-08-04T10:30:00'`, and the fallback
    chain bottoms out in `_NullQuery` (measured: both the plain and
    `Z`-suffixed spellings normalize to `_NullQuery` in the pinned
    oracle), so a v2 user typing the quoted RFC3339 spelling silently got
    zero results. whoosh-compat's grammar accepts `T` as a separator and
    handles a trailing `Z` as the UTC designator it is, returning the
    documents the user meant. Compat-favorable, the same shape as entry
    45's double-quoted crash sibling. This entry covers the single-quoted
    spelling only: the bare unquoted spelling colon-tokenizes differently
    in whoosh (a partial numeric range plus leftover terms, not
    `_NullQuery`) and is not claimed here.

    The bracketed-range face of the same extension needs no entry of its
    own: whoosh partially parses a `T`-bearing bound down to its leading
    year (entry 12's documented partial-bound collapse), so those
    spellings genuinely diverge under entry 12's mechanism and are
    correctly absorbed by its allowlist entry (the three
    `created:[...T...Z TO ...]` corpus lines).

    Test references: `tests/differential/allowlist.py`'s
    T-separated-value entry (ordered before the entry-18 bare-ISO entry,
    whose numerically-correct-fallback mechanism does not describe the
    `_NullQuery` outcome); `tests/emitter/result_allowlist.py`'s matching
    entry; `tests/differential/corpus_paperless.txt`'s
    `added:'2026-08-04T10:30:00'` and `added:'2026-08-04T10:30:00Z'`
    lines; `tests/emitter/test_acceptance_property.py`'s
    `test_quoted_rfc3339_value_is_a_result_level_divergence`.
