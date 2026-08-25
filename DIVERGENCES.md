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
2. Wildcard/prefix patterns are normalized via `pattern_normalizer` before
   matching - case-folded, and on a stemmed field also offered as their
   stem, see the alternatives paragraph below (real whoosh matched raw index
   terms, so `Entwä*` with a capital E failed there too, this is a fix, not
   a regression). The normalization covers every *term*
   character of the pattern, literal runs and bracket-class bodies alike
   (`title:BILL[I]NG*` matches whatever `title:BILLING*` matches), matching
   real whoosh's model of folding the whole pattern text before handing it
   to fnmatch.

   The normalizer may also answer with *several* forms of a fragment
   (`FieldSpec.pattern_normalizer` returns `str | Sequence[str]`), and the
   emitter then matches a term satisfying any of them. That widens the same
   divergence rather than adding a new one, and exists because a host whose
   index is stemmed cannot pick one form: English Snowball substitutes
   rather than truncates, so `company*` needs the stem (`compani`) to reach
   the indexed term while `copy*` needs the run as typed to reach
   `copyright`, and 3.5% of a 4,977-word vocabulary stems to something that
   is not a prefix of itself. Real whoosh, matching raw index terms, found
   neither. Equal forms collapse to one regex branch, so a host that returns
   one form (or none at all) gets byte-identical output to before.

   Three qualifications, all inside a bracket class, and all erring toward
   reading the pattern as typed:

   - A `pattern_normalizer` that expands a single character into several
     (paperless-ngx supplies `ascii_fold(text.lower())`, which maps `ß` ->
     `ss` and `æ` -> `ae`) is not applied inside a class, because a class
     matches exactly one character and a range endpoint of `[ss-z]` is not a
     range at all. Those characters are left as typed, so `[aß]` keeps
     looking for a literal `ß` that a folded index will not contain; the
     pattern under-matches rather than silently meaning something else.
     Outside a class the same character folds normally, since a literal run
     has room for the expansion.
   - A normalizer offering *several* alternatives for a character, or none
     at all, is likewise not applied inside a class, for the same reason: a
     class position matches exactly one character, so an alternation cannot
     live there, and adding or dropping members would change the body's
     length, which every fnmatch offset in the translation is taken against.
     Only a single one-character alternative applies. A stemmer leaves
     single characters alone, so this qualification costs a realistic host
     nothing. What the class then matches is whatever the character as
     typed matches, which is something *other* than the answer offered
     rather than reliably less than it, and the three cases differ (all
     measured):

     - Several alternatives *including* the typed character: narrower, only
       the typed form is accepted. `glob_to_regex("[ab]", lambda t: (t,
       t.upper()))` is `"[ab]"`.
     - Several alternatives *not* including it, or one multi-character
       form: **disjoint**, not narrower. `glob_to_regex("[a]", lambda _t:
       ("x", "y"))` is `"[a]"`, which matches `a` — a character neither
       offered form matches. Same for `lambda _t: ("xy",)`.
     - No alternatives at all: **wider**. "No term can match this fragment"
       is honored outside a class (`glob_to_regex("ab", lambda _t: ())` is
       `None`) but not inside one, where the character stays as typed, so
       `glob_to_regex("[ab]", lambda _t: ())` is `"[ab]"` and still matches
       `a` or `b`.

     Pinned in `tests/emitter/test_pattern_alternates.py`. What none of the
     three does is silently mean a different valid class.
   - The class's extent is found *before* the fold, so a character the
     normalizer maps onto `[` or `]` (`ascii_fold` maps the fullwidth `［`
     and `］` that way) can neither open a class nor close one; it is only
     ever an ordinary member of a class the user opened with a real `[`.
     The whole-pattern-text fold the oracle performs would let it do both:
     `title:[a］*` and `title:［a]*` are the literal text `[a]` followed by
     anything here, where the oracle reads the class `{a}` followed by
     anything. Class delimiters are syntax rather than term characters, so
     they are read from what the user actually typed. The result
     under-matches or reads literally; it never silently becomes a
     different valid class.

     One step further than "delimiters", but the same rule and the same
     cause: fnmatch skips a leading `!` *before* applying its "a `]` in
     first position is an ordinary member" rule, so a fold that produces
     that `!` can move where the oracle thinks the class ends. `title:[！]`
     (fullwidth `！`, which `ascii_fold` maps to `!`) is read here as the
     class `[！]`, whose body folds to exactly `!`, which is fnmatch's
     negated-empty class and matches any single character; the oracle folds
     first, reads `[!]`, finds no closing `]` at all, and takes the whole
     thing as the literal text `[!]`. It is the only shape *measured* to
     diverge on a fold-created `!`: exhaustively over every pattern up to
     length 4 in the glob alphabet plus the fullwidth `！－＼` (111,113
     patterns) and over 299,761 random patterns of length 5-8, that shape
     excluded, there were zero disagreements with the oracle. Everywhere
     those sweeps reach, a fold-created `!` negates the class in both
     readings alike, fnmatch's "a `-` directly after the negation marker is
     a literal member" offset rule included, which `title:[！--a]` (the
     negated range `-` through `a`) exercises. Longer patterns and wider
     alphabets are unmeasured; the mechanism (a leading `!` shifting where
     fnmatch's leading-`]` exemption applies) is confined to a class's first
     two body characters, which is why the short-pattern sweeps are
     believed to be representative rather than merely lucky.

   A normalizer mapping a character onto `-` or `\` (`ascii_fold` maps the
   en/em dashes and the fullwidth `－`/`＼`) is *not* a qualification: it is
   allowed to apply, because the oracle's whole-text fold does exactly the
   same thing, and agreeing with fnmatch is this translation's contract.
   `title:[a–z]*` with an en dash is the range `[a-z]` in both. Measured
   over every pattern up to length 4 in an alphabet of the fullwidth and
   ASCII class characters under the host's real `ascii_fold(str.lower)`:
   with the fullwidth delimiters excluded, 16,104 patterns and zero
   disagreements with the oracle; with them included, every one of the 459
   disagreements involves `［` or `］`, i.e. the qualification above and
   nothing else. Every emitted regex compiled in tantivy in both runs.
3. A boost on a natural-date keyword survives; paperless-ngx v2's own
   pre-parse rewrite turned it into a stray search term.

   **Correction, and a retracted claim.** This entry used to read
   "date-node boosts are preserved (whoosh silently dropped them)", citing
   the hardcoded `self.boost = 1.0` in whoosh's `DateTimeNode.__init__` and
   `DateRangeNode.__init__` (`whoosh/qparser/dateparse.py`). That
   hardcoding is real but **dead**: both classes set `has_boost = True`,
   and `BoostPlugin.do_boost` runs as a filter at priority 510, after
   `DateParserPlugin.do_dates` at 110, so it writes the typed boost back
   onto the date node afterwards. Measured against the pinned oracle,
   real whoosh **preserves** the boost on every single-value date
   spelling: `created:2020^2`, `created:jan^2` and `modified:now^2` all
   parse to a node with `boost=2.0`, and all three compare structurally
   EQUAL to whoosh-compat's tree. There is no divergence there, and there
   never was one.

   A boost after a *bracketed* date range does get lost, but on **both**
   sides equally, so it is not a divergence either: whoosh's
   `syntax.RangeNode` leaves `has_boost` at its `False` default, so
   `BoostPlugin.clean_boost` (filter priority 0, i.e. before the date
   filter runs at all) demotes the `^2` to an ordinary `WordNode` and it
   leaves the query as a stray search term. whoosh-compat's fork has the
   same node kinds at the same filter priorities and does the same thing;
   `created:[2020 TO 2021]^2` differs between the two sides only in the
   timezone of its bounds, which is entry 12.

   What is left, and what this entry now documents, is narrower and is not
   a whoosh property at all: paperless-ngx v2's
   `rewrite_natural_date_keywords` substitutes `added:yesterday` (and its
   seven siblings: `today`, `this month`, `previous month`, `previous
   week`, `previous quarter`, `this year`, `previous year`) for a literal
   bracket range by plain string replacement *before* whoosh parses the
   query. The boost then lands after a `RangeNode` rather than a word
   node, and falls out as a stray term by the mechanism above:
   `added:yesterday^2` searches for the literal text `^2` across the
   default fields in the v2 pipeline, while whoosh-compat, which never
   rewrites the keyword, binds the boost to the resulting `DateRange`.
   Real whoosh *without* that rewrite keeps the boost here too
   (measured), so this is a v2-pipeline divergence, not a whoosh one.

   Test references: `tests/differential/corpus_paperless.txt`'s
   `added:yesterday^2` line and its matching `tests/differential/allowlist.py`
   entry, whose pattern is derived from `oracle.NATURAL_DATE_KEYWORDS` (the
   same list the rewrite itself uses) rather than from any general
   date-boost shape.
4. Stopwords are not removed (a policy choice: whoosh-compat takes no
   position on stopwords, it uses whatever tokens the host's `analyzer`
   returns); this affects ranking and makes stopwords searchable, not
   matching correctness under implicit AND.

   Test references: `tests/differential/test_analyzer_boundary.py`'s
   `test_stopwords_are_a_documented_host_analyzer_divergence`, which
   confirms directly (bypassing the parser, which cannot observe this: see
   that module's docstring) that whoosh's `StandardAnalyzer` drops a bare
   English stopword to zero tokens while paperless-ngx's actual host chains
   (`lower_fold`/`stem_fold`, neither of which filters stopwords) keep it as
   a real, searchable one-token term.
5. Text-field ranges are parseable but unsupported at emit time (a current
   limitation: tantivy-py has no programmatic text-range API); they worked
   in whoosh. Machine-identifiable via `Diagnostic.divergence == 5`. Applies
   only to TEXT/KEYWORD ranges: a JSON-subpath range reports entry 30
   instead, and a BOOLEAN_EXISTS range reports no entry at all, because
   neither ever worked in whoosh either.
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
    case that hit the identical bypass, and since narrowed to exclude the
    bound-less spellings `added:[TO]`/`added:[TO}`: with no bound string
    there is nothing for the missing override to convert, and those
    compare EQUAL. The result-level twin in
    `tests/emitter/result_allowlist.py` has always required a digit
    inside the brackets; a digit is too strict for the AST layer, since a
    month-name bound like `added:[dec to feb]` is tz-converted too and
    does diverge); confirmed to *not* change actual
    search results for this project's small acceptance fixture in
    `tests/emitter/test_acceptance_e2e.py::test_scenario_equal[lowercase-to-open-range]`
    (see that test module's docstring for why an AST-level divergence
    doesn't always imply a different final result set).

    Narrowed a second time: a range whose *both* bounds are
    pure `PlusMinus` relative offsets (`created:[-1yr to -0yr]`, any
    unit/granularity) compares EQUAL, not divergent, and is no longer
    claimed. The tz-reversal override this entry's bug is about only
    changes a bound whoosh's `LocalDateParser` would otherwise shift from
    local wall-clock time to UTC; a relative offset is an arithmetic delta
    off `basedate` on both sides regardless, so the missing override
    changes nothing when every bound is one. Either bound being a named
    keyword (`now`, `today`, a month name) or an absolute date still
    diverges (measured) and stays claimed. A single-bound range whose only
    bound is `now` or a relative offset is a different case entirely: it
    crashes the oracle outright before a comparison is possible, so it was
    split out to its own entry, 55, with `DivergenceKind.ORACLE_ERROR`,
    checked ahead of this entry's pattern.

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
    fold check to also test for `"["`, so a trailing-star-with-bracket
    pattern stays a `Wildcard` instead of losing its class body. Whoosh
    performs this fold at two independent sites
    (`WildcardPlugin.do_wildcards` and `Wildcard.normalize()`, ported as
    `QueryParser.wildcard_query`); here both call one
    `parser/plugins.py:folds_to_prefix`, so the `"["` test cannot come back
    at one site and not the other.

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

14. **The dot-inclusive fieldname tagger that lets JSON dotted-path fields
    (`notes.user`, `custom_fields.value`, ...) resolve also tags any other
    unregistered dotted run the same way, diverging on every such value
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

    The mechanism is the two tagger regexes, not the three dotted names
    this project's corpus and generators happen to use, so the allowlist
    claim is no longer scoped to those names. *Any* dotted run immediately
    followed by a colon is cut in a different place by each side, whether
    or not the dotted name resolves to anything anywhere. Measured for a
    name registered nowhere at all: `title:ab.cd:9 OR x` parses to the
    single fielded value `title:'ab.cd:9'` on whoosh-compat's side (its
    tagger takes `ab.cd:` as one rejected candidate and folds it back onto
    the `9`), but to `title:ab` AND a multifield-expanded `cd:9` on the
    oracle's (its tagger cannot reach past the dot, so it only ever
    matches `cd:`, leaving `ab.` attached to the `title:` prefix). The
    same cut happens for a bare `ab.cd:ef`, a parenthesized `(ab.cd:ef)`,
    a multi-dot `ab.cd.ef:gh`, and a dotted run following an unknown
    field's own colon (`zzz:ab.cd:ef`, which entry 15's regex used to
    claim under its own, wrong reason).

    Two boundaries on that claim, both measured. A dotted run with no
    colon after it is not a field candidate on either side (`title:ab.cd`,
    `9.90`, both EQUAL, consistent with entry 46's "a dot never splits a
    token"). And a dotted run inside a quoted value is claimed by a quote
    plugin before either fieldname tagger sees inside it, so neither side
    cuts there at all: `title:'a.b:c'`, `'a.b:c'`, their double-quoted
    spellings, and a colon-fielded fragment inside a phrase
    (`created"type:a.b:asn"a`) all compare EQUAL. The allowlist pattern is
    therefore anchored on the whole value (a value boundary, then at most
    one leading field prefix) rather than on "a colon precedes the dotted
    run", which would reach inside such a phrase.

    Test references: `tests/differential/allowlist.py`'s two `custom_fields\.`
    / `notes\.` entries (AST-level: neither side's tree matches, by
    construction); `tests/emitter/test_acceptance_e2e.py::test_notes_user_json_subpath_has_no_v2_analogue`
    (result-level: demonstrates concretely that `notes.user:alice` matches
    doc 1 through the JSON-subpath emitter but nothing at all through a
    whoosh oracle index with a plain-TEXT `notes` field, even when that
    index's `notes` field is populated with the same underlying data
    flattened to plain text); `tests/differential/test_allowlist_xref.py`'s
    `test_entry_14_claims_dotted_name_colon_shapes` and
    `test_entry_14_does_not_claim_dotless_or_quoted_shapes`;
    `tests/differential/corpus_docs.txt`'s `title:ab.cd:9 OR x` /
    `ab.cd:ef` lines.

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
    Corpus lines for this correctly-fielded pathway:
    `content:foo OR title:multi-word` and `(0) OR (title:00-000)`
    (`tests/differential/corpus_paperless.txt`).

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
    (entry 29's demotion; the field must declare no default subpath, or the
    bare name resolves and never demotes, see entry 20) can itself analyze
    to more than one token.
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
    single-character value İ (U+0130) is the one value that survives
    `minsize=2` while being a single codepoint, because it is the only
    character in Unicode whose `str.lower()` expands to two codepoints
    (pinned by a derivation test), so `zzz:İ` and `attrs:İ` genuinely
    split into two surviving tokens and diverge.

    How the allowlist decides "survives as 2+ tokens" is worth stating
    precisely, because this entry used to describe it as a crude
    two-character proxy that was explicitly not reliable, and that
    description is now stale. It is field-kind-aware and modelled on the
    real analyzers rather than on an enumerated separator character
    class. For a TEXT field it is whoosh's own tokenizer regex
    (`\w+(\.?\w+)*`, so a single interior dot glues two runs into one
    token, see entry 46) followed by both of `StopFilter`'s rules:
    `minsize=2` and whoosh's live `STOP_WORDS` set, the latter derived
    from `whoosh.analysis.STOP_WORDS` at import time rather than
    hand-copied, so `ab/the`, `901+and` and `zzz:the` are correctly not
    claimed. For a comma_values KEYWORD field there is no two-character
    rule at all and no dot-gluing: the split is a literal comma, applied
    by `CommaValuesPlugin` at the parser level, and a piece of any length
    counts (`tag:'0,00' OR x`). The İ exception above is the one place the
    model names a specific character. What remains an approximation is
    the *query-grammar* boundary work around the value (which characters
    can end a bare value, which are literal text inside an explicitly
    fielded one), not the analyzer simulation itself.

    Two surviving *pieces* are still not two surviving *tokens*: a value
    whose pieces are all the same word (`path:ïð9-ïð9`, `ab-ab`,
    `ab-ab-ab`, case-insensitively) analyzes to a single distinct token,
    so ANDing and ORing it come out the same and the two sides compare
    EQUAL. A single dot does not split a value at all (`ab.cd`,
    `title:foo.bar`, see entry 46), so a dotted spelling is never this
    divergence either. And the separator has to match the field kind: a
    TEXT field's analyzer splits on both a dash and a comma, but an
    *unquoted* comma value on a comma_values KEYWORD field is split
    identically at parse time by both sides (`tag:ab,cd OR tag:x` is
    EQUAL) and only its quoted spelling diverges, by entry 17's
    comma-quote-literal mechanism meeting this entry's `Or` context. The
    allowlist entries now test all three conditions; before the
    pre-release staleness sweep they tested none of them, and the fielded
    `OR` entry in particular discarded four out of five of the
    comparisons it claimed.

    Three neighbouring shapes were re-attributed while sweeping the
    unknown-field-colon cell, and the boundary between them is worth
    stating because two of them used to be claimed here under this
    entry's reason string while diverging for a different cause entirely.
    A dotted run followed by a colon (`zzz:ab.cd:ef`) is entry 14's
    tagger-regex cut, and two consecutive rejected field-name candidates
    (`zzz:and:9`, `zzz:a:b`, `ab:cd:ef`) are entry 57's discarded-candidate
    whoosh bug: in both, the two sides disagree about the value's *text*
    before any combinator question arises, so this entry's paperwork
    described the wrong cause. Both entries sit earlier in `ALLOW` and
    claim those shapes now. A third, `zzz:title:cd`, compares EQUAL and is
    no longer claimed at all: a recognized field name stops the
    rejected-candidate run on both sides, so nothing demotes as one
    multi-token blob.

    What *is* this entry's own mechanism, and was under-claimed rather
    than mis-claimed, is a single-quoted unknown-field value whose
    comma_values pieces contain characters that would be value boundaries
    outside the quotes: an interior colon, whitespace, a paren
    (`zzz:'a:b,c'`, `zzz:'a b,c'`, `zzz:'a(b,c'`). Inside the quotes those
    are literal text the KEYWORD field's comma split sees, both sides
    reach the `tag` field with exactly the same two pieces, and the whole
    tree is identical apart from that branch's `And` versus `Or`. The
    comma-less spelling `zzz:'a:b'` has one piece and compares EQUAL,
    confirming the comma split, not the colon, is what makes the
    difference.

    Corpus lines: the two `tests/differential/corpus_paperless.txt` lines
    named above, plus the two entry-15 KNOWN DIVERGENCE blocks in
    `tests/differential/corpus_realworld.txt`: the unknown-field-demoted
    group (`dat:'-1 year to now'`,
    `type: A OR type: B OR custom_field_name >= 2025-01-01`,
    `document_type:[Receipt]`, `tag: 11-33 Mirka`) and the nine real
    user values the field-kind-aware rebuild newly claims (`02091-C-71`,
    `02091-C-712`, `02091-C-71a`, `02091-C-76hallo`, `9,90`,
    `test 12,34 some use`, `वर्तमान`, `वर्तमान क्षण की धन्यता`,
    `ASN>1593902`), plus `tests/differential/corpus_docs.txt`'s
    `zzz:'a:b,c'` / `zzz:'a b,c'` lines for the quoted comma_values
    pathway.

    Test references: `tests/emitter/result_allowlist.py`'s unfielded/
    `OR`-nested dashed-word and bare-JSON-value entries;
    `tests/emitter/test_acceptance_property.py`'s
    `test_multitoken_default_or_context_is_a_result_level_divergence`;
    `tests/differential/test_allowlist_xref.py`'s
    `test_entry_15_claims_genuine_divergences` and
    `test_entry_15_does_not_claim_agreeing_shapes`.

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
    unlike a typical differential-only divergence). That convergence is
    context-dependent, though, and only holds where both sides combine
    the split tokens the same way: inside a user-written `OR`,
    whoosh-compat's still-unsplit literal resolves `Multitoken.DEFAULT`
    against the enclosing `Or` while whoosh's already-split pair keeps
    the parser's fixed AND default, so `tag:'ab,cd' OR tag:x` does
    diverge at this layer. That shape is claimed by entry 15's fielded-
    inside-`OR` allowlist entry, whose scope was widened to name it during
    the pre-release staleness sweep; the mechanism is this entry's
    literal, the reason it survives to the comparison is entry 15's. The design choice itself, and
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

    The separator is `-`, `.` or `/`, not a space. A space used to be in
    the allowlist entry's separator class, which turned "a bare
    separated-ISO date" into "a four-digit run followed by anything" and
    swept in shapes this entry's reason is false for: `added:'2020 5pm'`
    and `created:0125 0` (a year and an unrelated bare term) both compare
    EQUAL, and `added:'2020 12:30'` diverges for entry 21's
    month:day-versus-time-of-day mechanism, not for this one. Entry 21 now
    carries its own allowlist entry for that shape.

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

19. **Multi-word natural-date keywords parse *unquoted*
    (`created:previous month`), which no whoosh grammar accepts
    (design).** whoosh-compat's date grammar adds keywords whoosh does not
    have (`previous week`/`month`/`quarter`/`year`, plus whoosh's own
    `this month`/`this year`), and accepts all six as a bare value on a
    date field as well as as a quoted one: `DateParserPlugin`'s
    `do_date_phrases` filter joins the two words (and a trailing time of
    day, if any) back into a single value before the grammar sees them, so
    `created:previous month` resolves exactly like
    `created:"previous month"`. In whoosh a value always ends at the first
    space, so the unquoted spelling would be a date attempt on `previous`
    plus a stray default-field term `month`. Real paperless-ngx v2 worked
    around that with an *app-level* regex preprocessing pass in
    `DelayedFullTextQuery` (`rewrite_natural_date_keywords`, `index.py`)
    that rewrote the phrase into an explicit bracket range before whoosh
    ever saw the string; a string rewrite cannot tell a value from the
    inside of a quoted phrase, so `title:"see created:previous month
    notes"` was corrupted by it. Owning the widening in the grammar, where
    quoting is already known, removes that failure mode rather than
    narrowing it.

    The `do_date_phrases` join runs at filter priority 101, ahead of entry
    61's rule at 102, and that ordering is what keeps a bare phrase safe:
    by the time entry 61 looks for a run to reject, `added:previous month`
    is already one joined value the grammar accepts, so there is no
    two-word run left there for it to see, and it parses clean (measured).

    The two entries do meet, though, and the joined value is where. A
    joined phrase is an ordinary word node, so it can serve as the *head*
    of a run entry 61 then extends over the words that follow it.
    Measured, basedate 2026-08-04 10:30 Europe/Berlin:

    ```
    added:previous month          -> clean
    added:previous month to now   -> BAD_DATE('previous month to now')
    added:previous month 3 pm     -> BAD_DATE('previous month 3 pm')
    added:"previous month to now" -> clean
    ```

    So this entry's promise, that the unquoted spelling reaches the grammar
    as the quoted spelling would, holds for a phrase alone and for a phrase
    plus a trailing time of day *as the grammar reads it* (see the two
    outcomes below), but not once the joined value is only the start of a
    longer date expression: `added:"previous month 3 pm"` parses while
    `added:previous month 3 pm` is rejected. That asymmetry is entry 61's
    rule doing exactly what it exists to do, and it lands here too: an
    unquoted run that reads as one date value in full is the silent-wrong
    class, and quoting is the repair. It is recorded here rather than only
    there so a reader of this entry is not told the widening covers more
    than it does.

    The widening is confined to those six phrases on an explicitly named
    date field. Nothing else about a date value becomes whitespace-greedy:
    `created:previous week AND title:foo`, `created:previous week invoice`,
    `title:previous month` (a TEXT field) and `created:this week` (not a
    keyword in any spelling) are all unchanged.

    A time of day *trailing* one of the six is joined on, so that the
    unquoted spelling reaches the grammar as the value the quoted spelling
    would. What the grammar then does with that value depends on the
    keyword, and rejection is only one of the two outcomes. `previous week`
    and `previous quarter` resolve to a span, so entry 52 rejects the
    combination: `created:previous week 3pm` is a BAD_DATE, exactly as
    `created:"previous week 3pm"` is. The other four (`previous month`,
    `previous year`, `this month`, `this year`) resolve to a calendar unit
    and *accept* the time, which narrows the range to that time of day on
    the period's first and last day: parsed in Europe/Berlin,
    `added:previous month noon` is
    `2026-07-01T10:00Z .. 2026-07-31T10:00:00.000001Z` (noon local, which
    is 10:00Z at that zone's UTC+2 summer offset), not the whole month.
    Both outcomes are a change from paperless-ngx v2, whose rewrite
    matched the phrase alone: there, `added:previous month noon` was the
    full-month range **plus a free-text `noon` term**, and that free-text
    term is now gone. This is the same class of v2 divergence as the
    rejecting branch, accepted for the same reason (the unquoted spelling
    must mean what the quoted one means), and it is recorded here so the
    accepting half is not a surprise.

    A time *leading* the phrase is not joined, and
    the asymmetry is deliberate: a field prefix binds the next date
    expression, and in `created:3pm previous week` it finds `3pm`, a
    complete value, and stops. The phrase after it was never combined with
    the time, so there is no incoherent combination for entry 52 to
    reject; it stays free text, which is also what released paperless-ngx
    v2 did with that spelling (its quoting shim only fired on a phrase
    directly following a date-field prefix). Quoting is what forces the
    two into one value, so only `created:"3pm previous week"` is rejected.

    One spelling inconsistency this leaves, noted rather than fixed:
    `added:("previous month")` joins but `added:(previous month)` does
    not. Inside a parenthesized group the field name has already been
    propagated to *both* words by the time the join runs, and the join
    requires the words after the head to carry no field name of their own
    (the guard that keeps `added:previous title:month` apart). This is
    v2-parity-neutral, since the host rewrite it replaces also required
    the phrase to directly follow `field:`, so it is left alone rather
    than widened further.

    Test references: `tests/test_parser_date_phrases.py` (the whole file);
    `tests/test_parser_period_keywords.py`'s
    `test_period_keyword_with_a_time_is_a_bad_date`,
    `test_unquoted_leading_time_does_not_reach_the_phrase` and
    `test_calendar_unit_keyword_still_takes_a_time` (unquoted cases);
    `tests/emitter/test_acceptance_e2e.py`'s
    `test_created_previous_month_unquoted_needs_no_app_level_rewrite`;
    `README.md`'s date syntax row.

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

    One spelling is carved out of that allowlist entry: the standalone
    token `*:*` (a `*`-named field). Both sides read it as an unfielded
    match-all `Every(field=None)`, so it compares EQUAL and claiming it
    only discarded the comparison. It is not merely unclaimed: conjoined
    with a term whose analyzer drops every token it becomes entry 23's
    match-all face, which now has its own allowlist entry. The carve-out
    is deliberately exactly `*:*` and nothing wider: `**:*` is a genuine
    instance of *this* entry's divergence (measured) and stays claimed.

    That carve-out's *neighbourhood* had a blind spot, closed by a dedicated
    adjacent allowlist entry: `*:**` (and `**:**`). whoosh's `FieldsPlugin`
    reads the leading `*:` plus the first `*` as the unfielded match-all and
    leaves a second bare `*` behind, which multifield-expands to a literal
    `Wildcard("*")` per default field while whoosh-compat builds
    `Every(field)`: this entry's own divergence, reached through a star that
    the general entry-20 pattern cannot see (it requires the `*` to start at
    the string start or follow whitespace/`(`/`:`, and this one follows
    another `*`). It was measurably divergent and claimed by nothing, i.e. a
    published-library CI flake waiting for the right fuzz draw. The new claim
    is scoped by exhaustive measurement over every `*`/`:` string up to
    length 5 in five syntactic contexts: exactly two trailing stars diverge,
    while `*:***` compares EQUAL and is deliberately excluded, and the
    left-anchored boundary left `:*:**`, `**:`, `**::`, `**:*:` and `**:::`
    (all measurably divergent, all reached through different mechanisms)
    unclaimed and reported rather than swept in; that gap is closed below.
    Corpus lines: `tests/differential/corpus_docs.txt`'s `*:**` and `**:**`
    lines.

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

    The remaining five shapes named above as unclaimed are now claimed too,
    split into two mechanisms per direct measurement rather than swept into
    one broad pattern:

    `**:`, `**::` and `**:::` (a star-run then a colon-run, with *no*
    trailing star) reach the exact same Every(field)-vs-Wildcard('*')
    leaf-type divergence as `*:**`/`**:**` above, just through a token shape
    the `\*+:\*\*(?!\*)` regex cannot see (it requires exactly two trailing
    stars). `:*:**` and `**:*:` are a different mechanism entirely: whoosh's
    own grammar binds a *leading* bare `:` to a single specific schema field
    (a literal `Term`, e.g. `tag::`), while whoosh-compat's grammar treats
    the same bare `:` as an unfielded term and multifield-expands it into an
    `Or` of one `Term` per default field, a different node shape, not merely
    a leaf-type swap. Whether that leading-bare-`:` binding generalizes past
    these two exact strings was not measured, so its allowlist pattern is
    deliberately literal rather than generalized.

    Corpus lines: `tests/differential/corpus_docs.txt`'s `**:`, `**::`,
    `**:::`, `:*:**` and `**:*:` lines.

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
    non-fast JSON subpath still has no strategy and raises `QueryError`
    (`DiagnosticKind.EXISTS_REQUIRES_FAST`, `Cause.MISCONFIGURED`) naming
    the dotted form the query used.

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

    A bare JSON field name (no subpath) *whose spec declares no default
    subpath* is addressed via the same
    `field:*` -> `Every(field)` path when the query is exactly the
    existence check, even though the same bare name demotes to a text
    search for any other term/pattern: the parser's
    `FieldsPlugin.do_fieldnames` carves the "*"-alone shape out of that
    demotion before it applies, using the same `text == "*"` detection
    `QueryParser.wildcard_query` already uses for the general case here.

    A JSON field that *does* declare a default subpath
    (`SubpathSpec(default=True)`, 0.1.0) is outside this paragraph
    entirely, in both directions: its bare name resolves like any ordinary
    field, so nothing about it is demoted and the carve-out is never
    consulted for it. `notes:*` is then an ordinary recognized-field
    existence check on `FieldRef("notes", "note")`, which for a *fast* JSON
    field narrows the answer from "any subpath has a value"
    (`exists_query(name, json_subpaths=True)`) to the default subpath's own
    column, and for a non-fast one is the same `EXISTS_REQUIRES_FAST` /
    `Cause.MISCONFIGURED` refusal as before, differing only in naming
    `'notes.note'` where it used to name `'notes'`. Both are exactly what a
    host-side `notes:` -> `notes.note:` query-text rewrite produced, which
    is the point of the feature. See entry 30 for the other shapes a
    defaulted bare name now reaches.
    Test references: `tests/test_parser_fields.py`'s
    `test_json_bare_field_name_bare_star_is_existence_not_demoted` and
    `test_json_subpath_bare_star_unaffected_by_bare_name_carve_out`;
    `tests/emitter/test_kind_matrix.py`'s
    `test_json_bare_field_bare_star_existence`;
    `tests/test_default_subpath.py`'s
    `test_bare_star_existence_targets_the_default_subpath` and
    `test_bare_star_on_a_field_without_a_default_is_unchanged`;
    `tests/emitter/test_emit_default_subpath.py`'s
    `test_bare_star_existence_narrows_to_the_default_subpath`.

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

    The `12:30` spelling is not the whole shape. Measured cell by cell
    over every `HH:MM` pair, the divergence is exactly "the pair can be
    read as a calendar month and a valid day of that month": a left half
    of `01`..`12` and a right half that is a real day number for that
    month. `added:'2020 23:59'` (no month 23), `added:'2020 12:00'` (no
    day 0), `added:'2020 04:31'` (April has 30 days) and
    `added:'2021 02:29'` (2021 is not a leap year) all compare EQUAL,
    because no calendar reading is available and both sides fall back to
    the time of day.

    Test references: `tests/test_parser_dates.py`'s year-plus-time case;
    `tests/differential/allowlist.py`'s year-plus-`month:day` entry. That
    entry is new as of the pre-release staleness sweep: before it, entry
    18's separator class included a space, so entry 18's entry claimed
    this shape first and recorded its own "numerically correct on both
    sides" reason for it, which is provably false here.

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
    divergence, since the underlying behavior is this same entry. "TEXT
    field" is now a registry-derived requirement rather than a
    four-name KEYWORD carve-out: measured across the whole oracle
    registry, `NOT <field>:the` and `NOT <field>:a` diverge for every
    TEXT field and for no other kind, so the U64/BOOLEAN_EXISTS/JSON and
    unknown-field spellings (`NOT (id:0)`, `NOT attrs:9`, `NOT zzz:the`)
    that the old generic `\w+:` also claimed were all discarded
    comparisons that would have passed.

    **The match-all face, found by the pre-release staleness sweep, now
    fixed.** The same analysis-ordering asymmetry used to be
    reachable with no `NOT` (and no `ANDNOT`/`ANDMAYBE`/`REQUIRE`) at all,
    through an unfielded match-all:

    ```
    *:* title:the       (before the fix) whoosh-compat: Nothing()    real whoosh: Every()
    *:* AND title:the   (before the fix) whoosh-compat: Nothing()    real whoosh: Every()
    ```

    Each half agreed on its own. `*:*` parses to `Every(field=None)` on
    both sides and compares EQUAL; `title:the` is `Nothing()` on both sides
    and compares EQUAL. Only the conjunction diverged, and for the reason
    this entry already gives, just in a new position: `analyze()` began by
    calling `normalize()`, whose `And` rule drops an unfielded `Every` as
    the identity element, leaving a bare `Term` that the analysis pass then
    drops to zero tokens, so the whole query became `Nothing()`. Real
    whoosh analyzes at *parse* time, so the null child is already gone by
    the time `And.normalize()` runs and it is left with `And([Every()])`
    -> `Every()`.

    Unlike the rest of this entry, this face was **fixed, not documented as
    accepted**: the "keep the uniform, timing-independent
    policy" reasoning above applies to bare `NOT`/`ANDNOT`/`ANDMAYBE`/
    `REQUIRE`, where whoosh-compat's own behavior is at least internally
    consistent regardless of when the emptiness was discovered. This face
    was different: whoosh-compat's behavior here depended on an
    implementation accident (`analyze()`'s own leading `normalize()` pass
    discarding the `Every`'s protection before analysis ever got a chance
    to apply the *same* "newly-emptied sibling doesn't poison" rule the
    `ANDNOT`/`ANDMAYBE`/`REQUIRE` extension below already implements
    correctly for its own operands), not a considered design choice, so it
    did not meet the bar the rest of this entry sets for "accept the
    disagreement". The fix: `normalize()` itself no longer drops an
    unfielded `Every` from an `And` while any surviving sibling still
    holds a fielded, not-yet-analyzed `Term`/`Phrase`
    (`whoosh_compat.ast._can_still_empty_during_analysis`), so the main
    analysis pass's existing survivor rule can protect it if that sibling
    empties out, or correctly re-apply the AND-identity simplification if
    it doesn't. The unconditional drop still happens, just later:
    `analyze()`'s own post-analysis pass runs `normalize` in a private
    `_post_analysis` mode where nothing is left to discover, producing the
    same canonical shape whoosh's `And.normalize()` does.

    The AND-identity drop's soundness is a property of *when* it runs, so
    it belongs to `normalize()`'s rule rather than to a caller's choice of
    entry point. An earlier form of this fix instead left `normalize()`
    destructive and gave the pipeline's own call sites (`parse()`,
    `emit()`, `analyze()`'s leading pass) a private protecting variant.
    That moved the same bug one layer out rather than fixing it: any
    caller who ran the public `normalize()` themselves before calling
    `analyze()` still lost the `Every` before analysis could see the
    sibling's fate, so `analyze(x)` and `analyze(normalize(x))` disagreed,
    breaking `analyze()`'s documented normalization-insensitivity. With
    the rule in `normalize()`, the two are the same function and the
    property holds by construction. Only trees where the drop was already
    premature change shape: an unfielded leaf is never analyzed at all
    (`_leaf_tokens` returns it untouched), so it cannot empty out and the
    drop still fires for it, and any already-analyzed sibling is settled
    the same way.

    Both `whoosh_compat.parse()` (whose result is documented as
    normalized-but-not-yet-analyzed, analysis happening later, at emit
    time) and `TantivyEmitter.emit()` normalize before `analyze()` ever
    runs, which is why the rule has to hold at that point for the real
    `parse()` -> `emit()` API and not just inside `analyze()` (proven with
    a real search, not just an AST comparison: see the test reference
    below).

    `*:* title:the` and `*:* AND title:the` now compare EQUAL, along with
    the structural variants entry 23's old AST-level allowlist entry also
    claimed (`*:* title:the title:foo`, `*:* OR title:the`, `*:* NOT
    title:the`, `title:the *:*`, `(*:*) AND (title:the)`: all measured
    EQUAL after the fix). One spelling that regex also matched is
    unaffected and still genuinely diverges for an unrelated reason: a
    *quoted* zero-token value (`*:* title:"the"`) is entry 24's mechanism
    (a real empty-words `Phrase` object in whoosh vs. whoosh-compat
    dropping the phrase), not this one, and now falls through to entry
    24's own (broadened above) claim instead. That old
    allowlist entry is removed; its `tests/differential/corpus_docs.txt`
    corpus line is kept, repurposed into a live "CONFIRMED PARITY"
    comparison pinning the fix instead of deleted.

    The `Every` has to be the *unfielded* one, because only that one is the
    `And` identity. A fielded match-all was never affected: `has_tag:*
    title:the` and `id:* title:the` compared EQUAL before the fix too.
    (`title:* title:the` does diverge, but for entry 20's
    `Every`-versus-`Wildcard` reason, not this one, and is unaffected by
    this fix.)

    **How far this reached in practice, before the fix.** The divergence
    needed an analyzer that drops *every* token of the conjoined term,
    which made it a property of the host's analyzer rather than of
    whoosh-compat. It was genuinely reachable for a host running a
    Whoosh-style `StandardAnalyzer` with a stopword filter. It was
    effectively unreachable for the motivating consumer: paperless-ngx's
    analyzer chain is `simple -> remove_long(129) -> lowercase ->
    ascii_fold` (optionally followed by a stemmer), with **no stopword
    filter at all**, so its only token-dropping filter is `remove_long`,
    and hitting this would have required an unfielded match-all ANDed with
    a term every one of whose tokens exceeds 129 characters. Fixed anyway,
    since a host running a stock Whoosh-style analyzer is not a hypothetical
    user of this library.

    This face was invisible until the pre-release staleness sweep, for a
    documented reason worth recording: `tests/emitter/result_allowlist.py`'s
    result-level entry-23 row has always named it ("a zero-token term/phrase
    combined with `NOT`/`ANDNOT`/`ANDMAYBE`/`REQUIRE` **or a bare `*`
    (Every)**"), but the AST-layer allowlist had no such alternative, and
    entry 20's own regex over-claimed the standalone `*:*` token, so the
    differential fuzzer skipped every instance instead of comparing it. The
    result-level allowlist entry's "bare `*`" alternative is a loosely
    scoped, skip-only (not strict-xfail) match at that layer, kept as-is
    for now: narrowing it to exclude specifically the now-fixed shape
    without also losing coverage of the still-genuinely-divergent
    `NOT`/`ANDNOT`/`ANDMAYBE`/`REQUIRE` shapes it shares a pattern with was
    explicitly noted as impractical when that entry was written, and
    remains a real, but non-urgent (it costs coverage, not a false
    assertion), follow-up.

    Test references: `tests/test_analyze.py`'s
    `test_and_unfielded_every_survives_a_newly_zero_token_sibling` and its
    neighboring control cases (a fielded `Every`, a real surviving sibling,
    a genuinely pre-existing `Nothing`, direct `normalize()` calls pinning
    both sides of the new rule, and
    `test_analyze_is_insensitive_to_a_prior_direct_normalize`, which sweeps
    the shape through every combinator that can wrap it); and, proving
    the fix reaches the real public API rather than just the AST-level
    `analyze()` call the harness exercises directly,
    `tests/emitter/test_acceptance_property.py`'s `SCENARIOS_EQUAL` entry
    `entry23-match-all-face`, which runs `whoosh_compat.parse()`
    -> `emit()` against a live tantivy index built with a real
    stopword-dropping analyzer and asserts the matched-document-id set.
    The parenthesized spelling gets its own scenario,
    `test_parenthesized_match_all_face_matches_everything`, since real
    whoosh's own search of that exact tree raises rather than returning a
    result to compare against, so that test asserts only the
    whoosh-compat side; the AST-level comparison for the same spelling
    runs and agrees, from a `tests/differential/corpus_docs.txt` corpus
    line.

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

    **The orphaned-separator shape, reached with no `NOT` and no `Every`
    at all.** The same analysis-time-drop versus structural-poison
    mechanism is reachable through a *separator* that has no word to
    attach to, with no `NOT`/`ANDNOT`/`ANDMAYBE`/`REQUIRE` keyword and no
    match-all anywhere in the query. Real whoosh absorbs a comma into the
    word immediately preceding it, but when nothing word-like precedes it
    (a `]` or a `}` does, or whitespace does, or the query opens with it)
    there is no word to absorb into, and the comma becomes a clause of its
    own that contributes no terms. Measured directly against the pinned
    oracle:

    ```
    created:[TO],created:[TO]     whoosh: And([DateRange, Or([]), DateRange])  -> matches nothing
    created:[TO] created:[TO]     whoosh: And([DateRange, DateRange])          -> matches everything
    content:doc , content:doc     whoosh: And([Term, Or([]), Term])            -> matches nothing
    content:doc,content:doc       whoosh: And([Term, Term])                    -> matches the term
    ,content:doc                  whoosh: And([Or([]), Term])                  -> matches nothing
    ```

    The empty clause is a real conjunct in whoosh's tree, so it poisons the
    whole `And` regardless of what its siblings match. whoosh-compat drops
    the contributing-nothing clause during its analysis pass and lets the
    siblings stand alone, which is the identical "uniform, timing-
    independent drop" policy the rest of this entry keeps, just reached
    through punctuation rather than through a word whose analyzer happens
    to consume it. Nothing new is decided here; the same predictability
    reasoning applies, and whoosh-compat's behavior is unchanged.

    Worth stating explicitly, since it is the boundary of the shape: a
    comma **attached** to a preceding word (`content:doc,content:doc`,
    `tag:a,b`, `tag_id:1,2`, a trailing `content:doc,`) is absorbed
    harmlessly and both sides agree on the tree *and* on the result. Only
    the orphaned spelling diverges, which is why
    `tests/emitter/result_allowlist.py`'s entry for it is scoped to a comma
    preceded by a bracket, a brace, whitespace, or the start of the query
    rather than to commas generally. The existing result-level entry-23
    pattern does not reach this shape at all: it requires a
    `NOT`/`ANDNOT`/`ANDMAYBE`/`REQUIRE` keyword or a bare `*` plus a
    `field:zeroTokenWord` spelling, and the clause that empties out here is
    bare punctuation, not a word any analyzer could be asked about. The
    divergence is result-level only: the AST-level comparison for
    `created:[TO],created:[TO]` runs and agrees, pinned by a
    `tests/differential/corpus_realworld.txt` corpus line.

    Test references: `tests/emitter/result_allowlist.py`'s orphaned-comma
    entry; `tests/emitter/test_acceptance_property.py`'s
    `test_orphaned_comma_clause_is_a_result_level_divergence`, which
    asserts the empty oracle id set against whoosh-compat's full one and
    isolates the comma by also asserting that both the single unbounded
    range and the whitespace-joined pair agree on every document.

    **Consequence for anything that reads polarity: read it before
    analysis, not after.** The survivor rule above is polarity-blind by
    construction, and it has to be (which side dropped is exactly what it
    refuses to care about). So an `AndNot` whose *positive* side analyzed
    to nothing leaves its `negative` side standing alone as an ordinary
    positive node, with nothing in the resulting tree recording that the
    user had excluded it. That is right for matching (the whole point of
    the rule) and wrong for any consumer asking "what did this query ask
    FOR?": `whoosh_compat.ast.free_text_tokens()` used to walk the analyzed
    tree, and so returned `('secret',)` for `the ANDNOT secret` when `the`
    was a stopword, contradicting its own first documented rule and handing
    a host a term to search for that the user had explicitly excluded. It
    now walks the normalized-but-unanalyzed tree and analyzes each
    contributing leaf on its own, so polarity comes from the query as
    written and no analysis outcome can reintroduce a negated term *into a
    tree it is given as parsed*. That qualifier is the whole guarantee: a
    caller who analyzes the tree itself before calling still gets the old
    answer, because the collapse has already happened and no later walk can
    see it. `free_text_tokens()` documents that as a precondition on its
    `node` parameter (and notes that `analyzed=False` on such a tree returns
    analyzed text, having no raw text left to return) rather than guarding
    on it, since an analyzed tree is structurally indistinguishable from any
    other valid tree. Any future API answering a question about polarity,
    intent, or "which words did the user type" must take the same route and
    carry the same precondition: the analyzed tree cannot answer those
    questions, and it is not going to be changed so that it can, since that
    would mean giving up the timing-independence this entry chose. Test
    references: `tests/test_free_text_tokens.py`'s
    `test_negated_terms_never_survive_an_analysis_collapse` and
    `test_an_already_analyzed_tree_cannot_answer_the_polarity_question`.

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

    Scoped to registered TEXT fields, like entry 23's own allowlist entry
    and for the same measured reason. The regex used to write a generic
    `\w+:` field with no kind restriction, contradicting both
    `KEYWORD_FIELDS_PATTERN`'s own rationale in the same module (whoosh's
    KEYWORD analyzer does no stopword or minsize filtering, so a
    stopword-shaped KEYWORD value is not zero-token) and the result-level
    twin, which does exclude them: `tag_id:"in by x"`,
    `viewer_id:"to a x 9"` and `type_id:"0"` all compare EQUAL.

    Test references: `tests/differential/allowlist.py`'s all-zero-token
    quoted-phrase entry; `tests/differential/strategies.py`'s
    `ZERO_TOKEN_WORDS` (the same verified-zero-token vocabulary used to
    generate both this case and entry 23's).

    An unfielded spelling (`"to"`, no field prefix) was found unclaimed by
    this entry's own regex, reported at first as possibly the same root
    cause as entry 23's "match-all face" `normalize()`-before-`analyze()`
    ordering bug, since both surface as "a zero-token thing survives in
    whoosh but not whoosh-compat". Measured directly and confirmed
    unrelated: that bug is specifically about `normalize()` discarding an
    *unfielded `Every`* as the `And` identity before analysis can protect
    a sibling
    that later empties out; nothing here involves an `Every` at all. The
    unfielded phrase multifield-expands to one `Phrase` per default field,
    each TEXT-field one of which analyzes to zero tokens by this entry's
    own already-described mechanism, one field at a time, with no
    ordering interaction. This entry's allowlist regex simply hadn't been
    broadened past the fielded case it was originally written from; it now
    covers both.

25. **A bare (non-bracketed) "now"-relative date offset is a
    whoosh-compat-only feature (design, found by the grammar-aware
    fuzzer).** `created:now-7d` parses to a real `DateRange` in
    whoosh-compat: this is documented directly in README.md's syntax
    table, which lists `created:now-7d` as a bare example, not just
    something usable inside a bracketed range's bounds. Real whoosh has no
    `now±<n><unit>` grammar at all (entry 53), bare or bracketed; a bare
    value in this shape fails to parse as a date on the real-whoosh side
    and falls back to `NullQuery`. Confirmed directly against the pinned
    oracle: `oracle_parse("created:now-7d", ...)` returns `NullQuery`. Not
    a bug on either side: whoosh-compat's single-value date grammar simply
    accepts a syntax real whoosh has nowhere at all.

    Correction: an earlier version of this entry additionally claimed
    `created:-3mos` (a bare *word-unit* offset, not a `now±` one) shared
    this divergence. That was wrong, and is corrected here rather than
    left standing: real whoosh's "simple"/plusdate grammar *does*
    recognize a bare `-<n><unit>` offset with no `now` prefix (`-3mos`,
    `-2yrs`, `-7d`, `-1y2mo3w`, etc.) as a single-value date, identically
    to whoosh-compat -- verified directly against the pinned oracle: the
    parsed, normalized trees are structurally equal for `-2yrs`,
    `-10mins`, `-30secs`, `-5hrs`, `-7d`, `-1y2mo3w`, `-999yrs`, `'-3mos'`
    and `-0d`. Only the `now±` spelling is whoosh-compat-only; the
    allowlist regex (`tests/differential/allowlist.py`) is scoped to
    `now[+-]` accordingly, not to any bare `-\d`.

    Correction: an earlier version of this entry additionally claimed
    `oracle_parse("created:[now-7d TO now]", ...)` "parses the identical
    relative-offset text correctly as a range bound." That was wrong, and
    is corrected here rather than left standing: real whoosh has no
    `now±<n><unit>` grammar at all (entry 53), so as a *range bound* too,
    `created:[now-7d TO now]` silently drops the `-7d` offset and resolves
    to a degenerate zero-width `now`-to-`now` range (confirmed directly
    against the pinned oracle). Only the *spaced* relative-offset spelling
    (`created:['-7 days' TO now]`) parses as a range bound on the
    real-whoosh side; `now-7d` does not parse correctly in either
    position.

    A relative offset written with a space (`created:-1 week`, unquoted)
    fails to parse as a single token on whoosh-compat's side too (it splits
    at the whitespace like any other unquoted multi-word value, producing a
    `BAD_DATE` diagnostic for the `-1` piece and an ordinary multifield term
    search for `week`): that shape is already excluded from comparison by
    the DIVERGENCES.md entry 6 diagnostics check, not by this entry.

    Test references: `tests/differential/allowlist.py`'s bare-relative-
    date-offset entry; `tests/differential/strategies.py`'s
    `DATE_RELATIVE` (the relative-offset vocabulary the grammar-aware
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
    (e.g. `(title:0)`, a single character below `StandardAnalyzer`'s
    `minsize=2`): both parse to the same "this operand resolves to
    nothing" shape by the time `ast.normalize()` runs. The value has to be
    *fielded* and *parenthesized* for that: an unparenthesized
    `title:0 ANDNOT title:bar` operand is handled by entry 23's
    analysis-time survivor rule instead and compares EQUAL, and an
    unfielded `(0)` multifield-expands rather than resolving to nothing,
    so it compares EQUAL too (both measured). Real whoosh's
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
    ANDNOT/ANDMAYBE/REQUIRE-with-a-resolves-to-nothing-operand entry. That
    entry's pattern used to be the bare alternation
    `\bANDNOT\b|\bANDMAYBE\b|\bREQUIRE\b`, which claimed every query
    mentioning one of the three operators without testing the zero-token
    condition its own reason names. Since the fuzzers *skip* a claimed
    shape rather than inverting it, and since these operators are
    generated freely, that silently discarded roughly half of all
    `ANDNOT`/`ANDMAYBE`/`REQUIRE` comparisons (all of which pass) and
    shadowed entries 15, 33, 37, 38 and 39, which are ordered after it,
    for any query that happened to use one. It now requires an operand
    that already resolves to nothing: a literal empty group, however
    nested, or a parenthesized zero-token value on a TEXT field.
    `tests/test_syntax.py`'s
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
    (see entry 20, the bare-`field:*` existence special case's
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
    reproduced. whoosh-compat instead reports a
    `DiagnosticKind.PATTERN_ON_NUMERIC` diagnostic and an `ErrorLeaf`, the
    same shape `BAD_NUMBER`/`BAD_DATE` already use for other
    invalid-input-on-parse cases, so a host can surface it as a 400
    instead of a wildcard that quietly means something else, or later
    dies at tantivy search time (`regex_query` doesn't work against a
    numeric field at all). Machine-identifiable via `Diagnostic.divergence
    == 29`.

    A BOOLEAN_EXISTS field (e.g. `has_tag`) has the same silent-mangle
    defect on real whoosh (`has_tag:t*` executes leniently, mangled to
    `Term('has_tag', True)`) and gets the same treatment here for the same
    reason (reported as `DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS`, also
    `Diagnostic.divergence == 29`): this synthetic field also has no
    tantivy schema column of its
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

    whoosh-compat reports a `DiagnosticKind.PATTERN_ON_SUBPATH` diagnostic
    and `ErrorLeaf`, the same shape entry 29 uses, from the same
    `_wildcard_kind_diagnostic` check in `parser/default.py`, extended to
    also fire when a `Prefix`/`Wildcard` ref resolves to a JSON subpath
    (independent of the U64 check: a JSON field's own kind is never U64).
    A hand-built `Prefix`/`Wildcard` node that bypasses the parser (so it
    never reaches the parse-time diagnostic) is refused a second time at
    emit, by `TantivyEmitter._reject_pattern_incompatible_kind` in
    `emitters/tantivy_.py`, which raises `QueryError`
    (`DiagnosticKind.AST_PATTERN_ON_KIND`) before any `Query.regex_query`
    call is built, mirroring entry 5's text-range emit-time backstop.
    Machine-identifiable via `Diagnostic.divergence == 30`.

    A lexicographic range on a JSON subpath (`notes.user:[a TO b]`) reports
    this entry too, not entry 5, for the same underlying reason: there is
    no tantivy-py API scoped to a subpath, so the range has nothing to
    build against either. It differs only in when it is reported. A range
    parses cleanly and is refused at emit time by `visit_termrange`
    (`DiagnosticKind.TEXT_RANGE`, stamped `divergence == 30` once the
    resolved field turns out to be a subpath), whereas a pattern is caught
    at parse time. Entry 5 stays scoped to the TEXT/KEYWORD ranges that
    worked in whoosh; a subpath range never did, because whoosh has no JSON
    field concept to have supported it.

    Both halves of this entry are reachable through a *bare* JSON field
    name when its spec declares a default subpath
    (`SubpathSpec(default=True)`, 0.1.0), because the bare name then
    resolves to a subpath like any dotted one: `notes:fo*` reports
    `PATTERN_ON_SUBPATH` at parse time and `notes:[a TO b]` builds a
    `TermRange` on `FieldRef("notes", "note")` that `visit_termrange`
    refuses at emit. Without a default, both of those spellings are
    unrecognized field prefixes and demote to a silent default-field text
    search instead (entry 20), so declaring a default converts two silently
    wrong searches into honest diagnostics. That is the same answer a
    host-side `notes:` -> `notes.note:` query-text rewrite produced, so it
    is not a new refusal for a host migrating off one, but it is a real
    behavior change for anyone adopting a default without such a rewrite.

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
    `prefix-star`/`wildcard`/`bracket-class` cells; and, for the range
    case, `tests/emitter/test_emit_ranges.py`'s
    `test_text_range_divergence_varies_by_field_kind` (`json-subpath`).

31. **A query nested past 200 levels -- whether by parentheses or by a chain
    of non-merging operators -- is diagnosed at parse time instead of
    crashing with an uncontrolled `RecursionError` (issue
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
    an `ErrorLeaf` for the excess nesting instead, so the ordinary
    pathological shapes (a deep pile of parens, a long operator chain) keep
    `parse()`'s "never raises for query input" invariant intact. The cap is
    not a total-depth guarantee: it bounds what each individual flat group
    contributes, so a query that nests groups which each stay under the cap
    still compounds them (20 paren levels around a 50-operator `ANDNOT`
    chain apiece builds ~1000 group levels), and `GroupNode.query()` still
    recurses once per level, so that shape still exhausts the interpreter's
    limit internally exactly as real whoosh does. That residue is
    long-standing, is not what this entry claims to diverge on, and is not
    something a cap can close; the backstop for it is the exception boundary
    in `parse()`, so the caller sees a `QueryParserError` (a library defect,
    routed to a 500) rather than whoosh's bare `RecursionError` -- see
    ARCHITECTURE.md's "what the caps do not cover".

    Parentheses are not the only source of depth, so the cap is not enforced
    only on them. `InfixOperator.replace_self()` builds one new group per
    non-merging infix operator, so a flat, paren-free chain of `ANDNOT` /
    `ANDMAYBE` / `REQUIRE` nests one level per operator ("a ANDNOT b ANDNOT
    c" -> "((a ANDNOT b) ANDNOT c)"), which real whoosh also builds and also
    `RecursionError`s on, at ~991 operands here before the cap was extended.
    `OperatorsPlugin.do_operators` therefore counts those operators in each
    flat group and reports the same `TOO_DEEP` diagnostic at 200 or more.
    Merging operators (`AND` / `OR`) merge side-by-side groups into one flat
    group and build no hierarchy, so a chain of them of any length parses
    normally, as does a chain of prefix `NOT`s, each of which wraps a single
    node without nesting.

    The two cap sites collapse differently, which is visible in what
    survives. `do_groups` treats only the over-deep bracket region as
    overflow and keeps everything outside it, so `a OR (((...300 deep...)))`
    still searches for `a`. `do_operators` replaces the whole flat group
    holding the over-long chain with the single `TOO_DEEP` leaf, so any
    sibling content in that same group goes with it. Both produce the same
    diagnostic and the same hard failure at `emit()`, so nothing is silently
    mis-answered either way, but the operator side is the blunter of the two.

    Not carried through the differential-triage allowlist/corpus triple:
    the corpus generators (`tests/differential/strategies.py`) have no
    mechanism that produces 200+ levels of paren nesting or 200+ operator
    chains, so there is no
    oracle-comparison test this could ever apply to; the divergence is
    exercised directly instead (see the test references below).

    Test references: `tests/test_parser_basics.py`'s
    `test_paren_nesting_below_cap_has_no_diagnostic` and
    `test_paren_nesting_beyond_cap_reports_diagnostic_instead_of_raising`;
    all of `tests/test_parse_depth_limits.py` (the operator-built case, plus
    the merging-operator and below-the-cap non-regressions);
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
    regexes (both layers) cover that spelling alongside the padded ones.

    A padded value that strips to something *true*-ish is likewise not
    part of this divergence, and the AST-layer allowlist regex used to
    claim it anyway: `has_type:'  true'`, `has_type:'true  '` and even
    `has_type:'  xyz  '` read True on both sides (whoosh by its non-empty
    fallthrough, whoosh-compat because the stripped text is neither empty
    nor one of the four falses) and compare EQUAL, making the entry's own
    reason string false for about half of what it claimed. The regex now
    requires the padded value to strip to the empty string or to one of
    `f`/`false`/`no`/`0`, which is exactly the set that can disagree.

    Before this, whoosh-compat's rule
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
    truthiness rule entry 33 documents for the unquoted/single-quoted form),
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

    The out-of-range value has to reach a numeric parse for the divergence
    to exist. A *double-quoted* spelling (`id:"2147483648"`) never does:
    `PhrasePlugin` claims it on both sides and both build a phrase, so the
    two compare EQUAL. The allowlist entry's optional quote is therefore
    the single quote only; it used to admit the double quote as well and
    discarded those comparisons for nothing.

    Test references: `tests/differential/allowlist.py`'s U64-field/large-value
    entry; `tests/differential/strategies.py`'s `_numeric_atom` and
    `_WHOOSH32_FIELD_MAX` (`whoosh32_max`/`whoosh32_overflow`/`u64_max`
    shapes, computed per field from `_SCHEMA` rather than assumed uniform,
    after `id:4294967295` was initially, incorrectly, expected to match);
    `tests/differential/corpus_docs.txt`'s
    `asn:4294967296` (unsigned field) and `id:2147483648` (signed field)
    lines.

40. **`NOT` of a group that recursively collapses to empty (nested empty
    groups, a boost/paren wrapper around one, or a nested `NOT` that itself
    collapses to empty) matches no documents here, but matches every
    document in real whoosh at nesting depth two or deeper (whoosh-bug, not
    reproduced; found by the acceptance-layer result property,
    `tests/emitter/test_acceptance_property.py`).**
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
    `Not(Nothing) -> Every` rule (entry 23). The same collapse applies when
    the nesting is itself another `NOT` rather than a bare paren: `NOT ((NOT
    ()))` also parses to a bare `ast.Nothing()`, since the inner `NOT ()`
    already collapses to nothing before the outer `NOT` ever sees an
    operand, confirmed directly against a live tantivy index (0 documents
    matched).

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

    `NOT ((NOT ()))` reaches the identical mechanism from one more layer of
    indirection, confirmed directly against `Wrapper.query`
    (`whoosh/qparser/syntax.py`) and `CompoundQuery.__len__`
    (`whoosh/query/compound.py`): the inner `NOT ()`'s own
    `Wrapper.query` sees its operand (`And([])`) test *falsy* (`CompoundQuery`
    defines `__len__` as `len(self.subqueries)`, so a zero-subquery compound
    is falsy even though it is a real, non-`None` object), and `Wrapper.query`'s
    `if q:` guard conflates that falsy-but-real object with "no operand at
    all", silently returning `None` instead of `Not(And([]))`. That `None`
    is dropped by the enclosing group's `is not None` check the same way an
    actually-empty child would be, leaving the same `And([And([])])`
    survivor that a single level of redundant parens around an empty group
    produces on its own; the outer `NOT` then wraps that survivor exactly
    like the `NOT (())` case above, producing the identical
    `And([Not(And([And([]) ...]))])` tree and the identical "matches every
    document" execution. `((NOT ()))` and `(NOT ())` (paren-wrapping with
    *no* outer `NOT`) do not reach this at all and agree with whoosh-compat
    (0 documents on both sides, confirmed directly): the divergence needs a
    `NOT` sitting immediately outside the collapsing structure, not merely
    parentheses around one.

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
    `NOT\s*\((?=[^)]*\()(?:[\s()0-9.^]|NOT\b)*\)` entry (the `NOT\b`
    alternative was added alongside the `NOT ((NOT ()))` shape, so the
    original whitespace/parens/digits/dot/caret-only character class still
    matches once a nested `NOT` token, not just nested parens, is what
    collapses to empty); `tests/emitter/test_acceptance_property.py`'s
    `test_not_of_nested_empty_group_is_a_result_level_divergence` and the
    generated-query property (seeded with `NOT ((())^0.5)` and
    `NOT ((NOT ()))`).

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
    looks like a defect in whoosh's own `NumericRange`/`NUMERIC.sortable_to_bytes`
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
    entirely (`visit_termrange` raises `QueryError`, entry 5),
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
    plumbing oversight, not intended query semantics. (This used to cite
    entry 3's "boost drop" as a parallel oversight in the same node class;
    that claim was retracted, see entry 3.)
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
    absorbed under the tz-bypass paperwork; scoped to a range that writes
    at least one bound, since a bound-less spelling like `added:[TO}` has
    no bound for the typed exclusivity to apply to and measurably compares
    EQUAL);
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

    The no-separator `T`-fused spelling (`added:"2026T10"`, entry 50's
    double-quoted sibling cell) takes exactly the same path: whoosh
    wraps it in a `PhraseNode` whose fallback `Phrase` raises the same
    no-positions `QueryError` at search time (measured), while
    whoosh-compat's `T`-separator grammar parses it into the
    month-period `DateRange`. The allowlist regexes accept `T` directly
    after the year for this reason.

    Test references: `tests/differential/allowlist.py`'s
    double-quoted-date entry; `tests/emitter/result_allowlist.py`'s
    matching entry (ordered before the dashed-token entry-15 pattern so
    the spelling cites this divergence, not the multitoken one);
    `tests/differential/corpus_paperless.txt`'s `created:"2020-01-01"`
    and `added:"2026T10"` lines; `tests/emitter/test_acceptance_e2e.py`'s
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
    `_NullQuery`) and is claimed by entry 49 instead.

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

49. **A bare unquoted `T`-separated datetime value carrying a clock
    time (`added:2026-08-04T10:30:00`, i.e. one whose colons make the
    tokenizer split it) truncated to a month-precision date on BOTH
    sides; whoosh-compat now rejects it as a bad date instead (superseded
    by entry 54, which generalizes the rule to any half-consumed date
    value).** The colon is what makes this entry's shape: the same
    spelling without one (`added:2026-08-04T10`) is never split, parses
    cleanly to an hour-precision `DateRange` here and to `_NullQuery` in
    whoosh, which is entry 48's compat-favorable shape without the
    quotes, not this truncation. The unquoted sibling of entry 48's
    single-quoted spelling: the query tokenizer splits
    `added:2026-08-04T10:30:00` at its colons before any date grammar
    runs, so the date parser on each side sees only `2026-08-04T10` (or
    `2026-08-04t10`) plus stray trailing words (`30`, `00`). Measured
    against the pinned oracle: both parsers used to consume the same
    `2026-08-` prefix, resolve it to the same August-2026 month period,
    and keep the same surviving leftover tokens (the `04T10` chunk is
    dropped or retained identically on both sides depending on the
    spelling), so the two sides AGREED on interpretation. The no-day
    spelling (`added:2026-08T10:30`) truncated one unit further by the
    same mechanism: the `T`-fused `08T10` chunk falls off the date parse,
    and both sides resolved the surviving `2026-` prefix to the same
    year-precision window with the same `08t10`/`30` leftover tokens
    (measured). Real whoosh still does exactly that.

    What whoosh-compat does now: entry 54 stopped its grammar from
    reading the dangling separator at the end of a cut-off value
    (`2026-08-`, `2026-`) as the end of a shorter, whole date, so the
    truncated fragment no longer parses at all and the value reports
    `Diagnostic(kind=BAD_DATE)` instead. The interpretation-level
    agreement described above is therefore gone by choice: the two sides
    no longer agree, because agreeing meant answering a query nobody
    asked (the month or year around a timestamp, ANDed with pieces of
    that same timestamp as free text) with no diagnostic to say so. The
    two structural mechanisms that used to make the trees differ anyway
    (whoosh's timezone-naive `NumericRange`, the same defect family entry
    12 documents for range bounds; and entry 15's multitoken AND-vs-OR
    over the leftover time tokens) are no longer reachable for this
    shape, since whoosh-compat produces no comparable tree at all.

    Result level: real whoosh returns NOTHING for this spelling on
    realistic document sets (the query is an `And` of the month window
    with the leftover time tokens, and documents rarely contain bare
    `30`/`00` text tokens) and says nothing about why; when a document
    *does* contain some of the stray tokens it can even return a hit,
    from a window the user never named. whoosh-compat returns a
    `BAD_DATE` diagnostic, which a host turns into a 400 naming the field
    and the offending value. Users wanting the RFC3339 value honored
    should use the quoted (entry 48) or bracketed-range spellings, which
    is what paperless-ngx generates; that advice is unchanged, it is now
    enforced instead of merely recommended.

    Test references: `tests/differential/allowlist.py`'s bare
    T-separated-value entry (ordered before the entry-18 bare-ISO entry,
    whose fully-parses-numerically-correct prose does not describe the
    truncation, and requiring no quote, complementing entry 48's
    quote-required entry; its pattern is kept, and now cites entry 54,
    for any future query shape it matches that does NOT diagnose);
    `tests/emitter/result_allowlist.py`'s matching entry (ordered before
    the entry-15 dashed-token pattern);
    `tests/differential/corpus_paperless.txt`'s
    `added:2026-08-04T10:30:00`, `added:2026-08-04T10:30:00Z` and
    `added:2026-08T10:30` lines (all three now take the entry-6
    diagnostic skip, counted by
    `test_diagnostic_skip_count_matches_corpus`);
    `tests/test_parser_dates.py`'s
    `test_bare_unquoted_t_value_is_rejected_not_truncated`;
    `tests/emitter/test_acceptance_property.py`'s
    `test_bare_rfc3339_value_is_a_result_level_divergence`.

50. **A no-separator `T`-fused datetime value (`added:2026T10`, bare or
    single-quoted, with or without a colon-split day token) parses to a
    working `DateRange`; real whoosh parses it to `_NullQuery`, matching
    nothing (whoosh-bug, not reproduced).** The dash-less corner of the
    RFC3339 `T` extension, a divergence face that exists only because
    whoosh-compat's grammar accepts `T` as a separator at all: whoosh's
    grammar cannot read `2026T10` in any way (no `T`, and its field
    self-parse also fails on the embedded letter), so its fallback chain
    bottoms out in `_NullQuery` (measured for the bare, lowercase-`t`
    and single-quoted spellings alike), while whoosh-compat reads
    year-`T`-month and returns the month the user plausibly meant. For
    the inner-colon spelling (`added:2026T10:30`) the tokenizer splits
    at the colon and whoosh-compat's date parser joins the adjacent
    tokens into a single day-precision `2026-10-30` reading, still
    against whoosh's `_NullQuery`. Unlike entry 49's dashed spellings,
    whoosh reads nothing at all here rather than a truncated period, so
    there is no half-consumed value for entry 54's rule to reject either
    (that rule needs a dangling separator, and `2026T10` ends on a date
    component): the outcome is the compat-favorable shape of entry 48.
    Entry 49's own truncation is no longer matched for parity, see
    entry 54.

    Sibling cells, each already ending in a documented outcome: the
    double-quoted spelling is entry 45's search-time Phrase crash (that
    entry's regexes now accept the `T`-fused form); a value whose tail
    survives tokenization but fails the date grammar
    (`added:2026T10:30:00Z`, `created:9999T13`) becomes a parse-time
    BAD_DATE diagnostic, entry 6's uniform rule. The allowlist entries
    are ordered before the entry-15 unknown-field-demotion pattern,
    which would otherwise mis-claim the inner-colon spelling by reading
    `2026T10` as an unknown field named `2026T10` with value `30`.
    The colon-less spelling matters beyond theory: the differential
    fuzzer's word alphabet can generate it (`\w`-only, no colon
    needed), so before this entry it was an unclaimed divergence
    waiting to fail loudly.

    Test references: `tests/differential/allowlist.py`'s
    no-separator-T entry; `tests/emitter/result_allowlist.py`'s
    matching entry; `tests/differential/corpus_paperless.txt`'s
    `created:2026T10`, `added:2026T10:30`, `added:'2026T10'` and
    `added:"2026T10"` lines; `tests/test_parser_dates.py`'s
    `test_no_separator_t_value_parses_as_year_t_month`;
    `tests/emitter/test_acceptance_property.py`'s
    `test_no_separator_t_value_is_a_result_level_divergence`.

51. **A range whose start is a bare time of day and whose end is already a
    concrete instant (`added:"noon to now"`, `added:[noon TO now]`,
    `added:[noon TO -1 week]`) resolves normally; real whoosh crashes with
    `AttributeError` on every such query (whoosh-bug, not reproduced).**
    Whoosh's range disambiguation resolves a date-less start against the
    end's date, and to decide whether copying the end's month/day would
    invert the range it compares `start.floor().time()` with
    `end.ceil().time()`. `ceil()` is an `adatetime` method: it exists only
    while a bound is still ambiguous. The `now` keyword (and anything else
    that resolves straight to a concrete `datetime`, such as a `-1 week`
    offset) reaches that line fully specified, so whoosh calls `ceil()` on a
    plain `datetime` and raises `AttributeError: 'datetime.datetime' object
    has no attribute 'ceil'` out of the parser. Confirmed directly against
    the real package for all three spellings above.

    That is a defect, not intended semantics, and this fork's bar is that
    confirmed whoosh bugs are not reproduced. The query itself is perfectly
    answerable -- a time-of-day lower bound with a concrete upper bound is
    an ordinary, meaningful range, and "noon today until now" is plainly
    what it asks for -- so returning a diagnostic for it would reproduce the
    *effect* of the bug (the query still doesn't work) while only changing
    the failure mode from a crash to a 400. Instead `parser/times.py`'s
    `timespan.disambiguated` reads both sides through the module-level
    `floor()`/`ceil()` helpers, which pass an already-concrete `datetime`
    straight through, so the comparison works for either kind of bound and
    the range resolves.

    Resolved semantics, with `now` = 2026-08-19 15:30 UTC:
    `added:[noon TO now]` -> 2026-08-19 12:00 through 15:30 inclusive;
    `added:"noon to now"` -> the identical inclusive 15:30 upper bound (the
    quoted `to` form and its bracketed sibling agree, since `now` is an
    exact instant, not an ambiguous period end: see `_text_to_node`'s
    exactness check, module docstring);
    `added:[noon TO -1 week]` -> 2026-08-12 12:00 through 2026-08-12 15:30.
    When `now` falls *earlier* in the day than the lower bound (a pre-noon
    `now` for `noon to now`), whoosh's own overnight rule for time-only
    bounds carries the upper bound past midnight rather than inverting the
    range -- the same rule already visible in `added:[3pm to 10am]`. The
    range is well-formed (lower bound below upper) in every case.

    One resolved shape is recorded here rather than endorsed. With a
    pre-noon `now` (01:41), `added:[noon TO -1 week]` resolves to
    2025-08-19 12:00 through 2026-08-12 01:41, roughly a year wide: `noon`
    cannot take the end's month/day, so it takes the basedate's, which lands
    after the end, and upstream's year-borrowing branch then pulls the start
    back a year. Both branches are verbatim upstream code, but real whoosh
    crashes before reaching them on this input, so nothing external says
    whether that is the intended reading -- and the fix makes it live,
    user-facing behavior where it used to be an unreachable crash. It is
    noted so that a pre-noon user getting back a year of documents with no
    diagnostic is a documented outcome rather than a surprise; it is not
    claimed to be correct, and a future task may reasonably narrow it.

    Only this one asymmetric shape is affected: a range whose bounds are
    both still ambiguous (`added:[noon to 3am]`, `added:[3pm to 10am]`)
    never took the crashing path and resolves exactly as before. There is
    no differential-corpus coverage and no allowlist entry, and there cannot
    be: the oracle harness parses through real whoosh, which raises on these
    inputs, so there is no oracle value to compare against.

    Test references: `tests/test_parser_period_keywords.py`'s
    `test_time_of_day_lower_bound_against_a_concrete_upper_bound_resolves`,
    `test_time_of_day_lower_bound_before_noon_carries_past_midnight` and
    `test_time_of_day_lower_bound_against_a_relative_upper_bound_resolves`;
    the year-borrow above is pinned as characterization only (explicitly not
    a semantics assertion) by the same file's
    `test_characterize_predawn_relative_upper_bound_year_borrow`.

52. **A period keyword written together with a time of day
    (`added:"previous week 3pm"`, `added:"3pm previous week"`,
    `added:"previous quarter noon"`) is diagnosed as a BAD_DATE, in both
    word orders.** ("Together with" means bound into one date value, which
    quoting always does; see the last paragraph for the unquoted
    spellings.) `previous week` and `previous quarter` are whoosh-compat
    grammar additions (entry 19's family of new keywords) and, unlike every
    other date element, they resolve directly to a fully-built `timespan`: a
    calendar week or quarter doesn't align with any single `adatetime` unit,
    so it can't be expressed as one. The date grammar's merging pass (`Bag`,
    via `parser/times.py`'s `fill_in`) then has nothing coherent to do when
    a time of day appears alongside one, and the two word orders used to
    disagree about it: "previous week 3pm" fed the `timespan` into the pass
    that expects per-unit attributes and raised `AttributeError: 'timespan'
    object has no attribute 'month'` out of `parse()`, while
    "3pm previous week" silently discarded the time and returned the whole
    week -- a wider range than the user asked for, with no diagnostic to say
    so. Both are now rejected with the same `Diagnostic(kind=BAD_DATE)`, on
    the semantic ground that a period names a *span*, and a time of day on a
    span names nothing.

    This is deliberately narrow and does not touch time handling elsewhere
    in the grammar. Ordinary date keywords combine with a time correctly in
    either order and still do: `added:"3pm yesterday"` and
    `added:"yesterday 3pm"` both give 15:00-16:00. So do the
    `adatetime`-valued members of the "previous ..." family
    (`added:"previous month 3pm"`, `added:"previous year 3pm"`), which are
    month- and year-precision `adatetime`s rather than spans and merge with
    a time the ordinary way. A bare period keyword (`added:"previous week"`)
    is entirely unaffected. Real whoosh has no equivalent behavior to
    diverge from here -- it has no `previous week` or `previous quarter` at
    all (entry 19) -- so this constrains only whoosh-compat's own
    extension. The rejection is on the *value*, so it does not depend on
    quoting: entry 19 accepts the phrases unquoted, and a time trailing one
    is bound into the same value, so `added:previous week 3pm` is diagnosed
    exactly like `added:"previous week 3pm"` (before entry 19 it was
    rejected too, but for the unrelated reason that a bare `previous` is
    not a date). That cuts both ways, and the unquoted spelling inherits
    the *acceptance* above just as faithfully: `added:previous month 3pm`
    is the same narrowed range as `added:"previous month 3pm"`, since only
    the span-valued keywords reject at all. The *leading*-time spelling is the one place quoting
    matters, and not as an exception to this rule: unquoted,
    `added:3pm previous week` never combines the two at all (`added:` binds
    `3pm` and stops, leaving "previous week" free text, as released
    paperless-ngx v2 did), so there is no combination here to reject. See
    entry 19 for why that binding rule, rather than word-order symmetry, is
    the one being followed.

    Test references: `tests/test_parser_period_keywords.py` (whole file);
    `tests/test_times.py`'s
    `test_fill_in_rejects_merging_a_timespan_with_other_units` and
    `test_fill_in_timespan_basedate_passthrough`.

53. **A reversed relative date range (`added:[now+1h TO now-1h]`) swaps its
    bounds instead of pushing the upper bound into the next day.** Checked
    against real whoosh first, and confirmed the hard way, since whoosh
    itself has no `now±<n><unit>` grammar (that combined token is a
    whoosh-compat-only extension, entry 25) for a query string to check
    directly: whoosh's own bare offset syntax that whoosh-compat inherits
    "for free" (entry 25's note; e.g. `-1h`/`+1h` without a `now` prefix) is
    the real thing to check, and real whoosh reproduces the exact same
    ~22-hour day-bump on `added:[+1h TO -1h]` that whoosh-compat used to
    produce on the `now±offset` spelling (confirmed directly against a
    `whoosh.qparser.dateparse.DateParserPlugin` instance: `[-1h TO +1h]`
    gives a 2-hour span, `[+1h TO -1h]` gives ~22 hours, from the same
    basedate).

    Despite that parity, this is treated as a defect rather than a
    convention to match, and diverged from. The day-bump exists for a
    genuine, different purpose: disambiguating a *bare, ambiguous* time of
    day with no date attached (`added:[9pm TO 5am]`, an overnight-shift
    reading that is a real, useful convention — confirmed separately
    against real whoosh, which resolves it to a sensible ~9-hour span). But
    `timespan.disambiguated()` (`parser/times.py`) applies the same
    same-day/time-reversed check uniformly to that case AND to the `now`/
    `now±offset`/bare `±offset` family, whose grammar elements (`PlusMinus`,
    the bare `now` regex) return a plain `datetime` directly rather than an
    `adatetime`, and which happen to land on the same calendar day. For
    that family, "the user wrote the bounds backwards" is overwhelmingly
    the more likely reading than "wrap to tomorrow" — nobody has a working
    query that depends on a written-backwards 2-hour window silently
    becoming a 22-hour one, and there is no diagnostic to warn them it
    happened. That is exactly the "silent wrong answer with no diagnostic"
    shape the project's own parity rule treats as a defect rather than a
    convention, so this fork's `timespan.disambiguated()` now checks
    whether both original bounds were already plain `datetime` instances
    (as opposed to `adatetime`, whether or not the `adatetime` happens to
    be fully specified) before choosing between the two branches: plain-
    `datetime`-on-both-sides swaps (matching the existing, already-whoosh-
    matching swap used for a same-shape reversed *absolute* range, e.g.
    `added:[2020-01-01 TO 2019-01-01]`), anything else still day-bumps.

    That representational check is narrower than "any pair of unambiguous
    instants": an explicit, fully-specified absolute datetime
    (`added:[20200101210000 TO 20200101050000]`) or an RFC3339 bound
    (`added:[2020-01-01T21:00:00 TO 2020-01-01T05:00:00]`) is still an
    `adatetime` object even when every unit is set, so a reversed range in
    either of those spellings, or a reversed range mixing one such bound
    with a `now`-family one, still day-bumps rather than swapping (measured
    at basedate 2026-08-04 10:30 Europe/Berlin:
    `added:[20200101210000 TO 20200101050000]` and
    `added:[2020-01-01T21:00:00 TO 2020-01-01T05:00:00]` both give an 8-hour
    day-bumped span; `added:['2026-08-04T10:00:00Z' TO now-3h]` gives
    19h30m; `added:[now+1h TO '2026-08-04T09:00:00Z']` gives 23h30m). That
    remains whoosh-parity (real whoosh day-bumps those shapes too, by the
    same `adatetime`-vs-`datetime` split) and is out of this task's scope;
    only the `now`/offset family, the shape the brief measured, is fixed
    here. Real whoosh's own reversed-instant-range behavior is left unfixed
    in `../whoosh`; only this fork's copy in `parser/times.py` changes.

    `tests/differential/corpus_paperless.txt`'s `added:[now+1h TO now-1h]`
    line already exercises this exact query shape, but does not
    independently discriminate this divergence: whoosh's grammar cannot
    parse the combined `now±offset` token at all (per above), so
    `oracle_parse` silently drops the offset on *both* bounds and returns a
    degenerate zero-width `now`-to-`now` range regardless of which order
    they're written in or what this fix does; the line already mismatches
    the oracle for that unrelated reason and is already covered end-to-end
    by the broad entry-12 allowlist pattern (the tz-reversal wiring defect,
    which matches every bracketed range on a DATE/DATETIME field). Adding a
    second, entry-53-citing allowlist pattern for the same line would be
    unreachable dead code, since entry 12's pattern is checked first and
    already matches; the direct-instantiation check against
    `whoosh.qparser.dateparse.DateParserPlugin` above, not this corpus
    line, is what actually establishes whoosh's own reversed-instant-range
    behavior. No new pattern was added to the differential AST-level
    allowlist module for this entry, deliberately; this paragraph
    documents that choice, not an oversight.

    Test references: `tests/test_parser_dates.py`'s
    `test_reversed_relative_range_swaps_like_the_absolute_case`.

54. **A date value the grammar can only half-consume is rejected
    (`Diagnostic(kind=BAD_DATE)`) instead of silently parsing the
    fragment before the cut (whoosh-bug, not reproduced).** The shape
    that motivated this is a bare, unquoted RFC3339 timestamp,
    `added:2005-01-01T00:00:00Z`. The colons in it are field separators
    in the whoosh grammar, so the tokenizer splits the value before any
    date parsing happens and the date field is left holding the fragment
    `2005-01-`. That split is correct and deliberately unchanged: it is
    the same rule that makes `added:"-1 week"` and `added:"next monday"`
    need their quotes, and the quoted (`added:"2005-01-01T00:00:00Z"`,
    entry 48) and bracketed (`added:[2005-01-01T00:00:00Z TO ...]`)
    spellings, which keep the colons out of the tokenizer's way, both
    parse the whole timestamp and must keep doing so. What was wrong is
    what happened *after* the split.

    The mechanism, in `Sequence.parse` (`parser/dateparse.py`): whoosh
    advances the sequence's position past a separator *before* trying the
    element that should follow it, and a progressive sequence that then
    fails on that element still reports having consumed the separator
    that led nowhere. The only progressive sequence in the module is the
    `simple` numeric grammar (separator class `[- .:/T]*`), so a fragment
    cut off mid-token reads as a shorter, whole date: `2005-01-` becomes
    "all of January 2005", `2005-` becomes "all of 2005", `2005-01-01T`
    becomes "all of that day". The leftovers of the timestamp
    (`00`, `00z`) survive as ordinary free-text terms and are ANDed onto
    the query. The user gets a query they did not write, an empty result
    set, and no diagnostic at all, while `added:now-3days` or
    `added:"3 days ago"` (values the grammar cannot start to parse) are
    rejected outright with `BAD_DATE`.

    Checked against real whoosh first, and it degrades identically
    (measured against the pinned oracle at `../whoosh`, basedate
    2026-08-04 Europe/Berlin): `added:2005-01-01T00:00:00Z` gives a
    January-2005 `NumericRange` ANDed with `content:00 AND content:00z`
    (and the same per-field pair on every other default field), and the
    fragments reproduce the swallow standalone as well:
    `added:2005-01-` gives 2005-01-01 through 2005-01-31, `added:2005-`
    gives all of 2005, `added:2005-01-01T` gives that single day. Its
    `Sequence.parse` is the same code this fork started from. Diverged
    from anyway, under the rule that parity never means reproducing a
    clear whoosh bug: broken parsing that yields a silently wrong query
    with no diagnostic is a defect, not a convention, and no working
    query depends on a truncated value quietly widening into a month.

    The rule this fork implements: a *non-whitespace* separator is
    consumed only provisionally, and the sequence's reported end position
    advances past it only once the element after it also matches. A
    fragment ending in a dangling `-`/`:`/`/`/`.`/`T` therefore no longer
    matches to the end of its text, `ToEnd` rejects it, and the value
    diagnoses `BAD_DATE` naming the field and the offending text.

    What `raw_value` carries, updated by entry 58: this paragraph
    originally documented `raw_value` as **the fragment the tokenizer
    handed the date field, not the text the user typed** (for
    `added:2005-01-01T00:00:00Z`, `2005-01-`, because the colons split the
    value before any date parsing happens, as described above). Entry 58
    widens `raw_value` to cover this exact kind of contiguous leftover
    fragment (for this same query it is now the full
    `2005-01-01T00:00:00Z`, with narrower scope limits of its own -- see
    that entry). The advice this paragraph is really making holds
    regardless of that widening, since it was never specific to the old
    narrower behavior: a host should say what it wants the user to do
    (quote the timestamp, per entry 48) in its own words
    rather than presenting `raw_value` as if it were a self-explanatory
    error message.

    The boundary, deliberately drawn where it is. A remainder that is a
    *clean token boundary* is still a term, not part of the value:
    `added:2005-01-01 invoice` and `created:2020 invoice` keep meaning
    "that date, and the word invoice", because the value there consumed
    its own text exactly and the remainder is whitespace-separated rather
    than glued on. Both named examples still mean exactly that, but the
    general claim needs one qualifier, added by entry 61: a
    whitespace-separated remainder stays a term unless the value and the
    remainder *together* parse as one complete date value, in which case
    entry 61 rejects the whole run and asks the user to quote it
    (`created:2020 august 4` does; `created:2020 invoice` does not, which
    is why that example is unaffected). What this paragraph says about
    *dangling separators* is untouched by that qualifier, and so is the
    rest of this paragraph about whitespace inside the grammar: entry 61
    is a filter running before the grammar sees a value, not a change to
    how the grammar itself ends one.

    Whitespace separators are for the same reason left on
    whoosh's behavior exactly: `simple`'s own `(?=(\s|$))` guard already
    treats whitespace as a valid end of a date value, so a trailing space
    inside a quoted value (`added:'2005-01-01 '`) still parses, and
    (more importantly) a numeric run followed by a space and a
    non-numeric word still *declines* the `simple` alternative so the
    named-month grammar gets its turn: rolling whitespace back too would
    have made `created:'2020 august 4'` and `added:'2020 5pm'` match
    `simple` for their leading year alone and, because `Choice` takes the
    first alternative that matches anything at all and never backtracks,
    never reach the alternative that can read them (both are pinned by
    existing tests, which is how this was caught). A value with a
    *leading* space (`added:' 2005-01-01'`) was already rejected before
    this entry existed; the trailing-space asymmetry is whoosh's and is
    left alone. Spellings with no dangling separator are untouched:
    `added:2005-01-01T00` (hour precision) and `added:2026T10` (entry 50)
    still parse, since their text ends on a date component.

    `tests/differential/corpus_paperless.txt`'s three bare-timestamp
    lines are entry 49's, and that entry (the specific, previously
    "deliberate parity" case this rule reverses) is amended in place
    rather than left contradicting this one. Its
    `tests/differential/allowlist.py` and
    `tests/emitter/result_allowlist.py` entries keep their patterns and
    now cite this entry as well: with whoosh-compat diagnosing, those
    corpus lines take the entry-6 diagnostic skip instead of reaching the
    strict-xfail mismatch assertion (the pinned skip count in
    `tests/differential/test_differential.py`'s
    `test_diagnostic_skip_count_matches_corpus` rose from 8 to 11
    accordingly), but the patterns still stand for any future query shape
    they match that does not diagnose.

    Test references: `tests/test_parser_dates.py`'s
    `test_bare_unquoted_timestamp_is_rejected_not_half_consumed`,
    `test_dangling_separator_is_a_bad_date`,
    `test_bare_unquoted_t_value_is_rejected_not_truncated`,
    `test_timestamp_spellings_the_grammar_can_consume_whole_still_parse`
    and `test_a_whitespace_separated_term_after_a_date_is_still_a_term`.

55. **A single-bound bracketed range whose only bound is `now` or a
    relative offset crashes real whoosh outright (whoosh-bug, not
    reproduced).** `created:[ TO -7 years]` is not a hypothetical shape: it
    is the maintainer-endorsed spelling from paperless-ngx#13482 for an
    empty date bound, and it raises before real whoosh ever builds a query
    object:

    ```
    AttributeError: 'datetime.datetime' object has no attribute 'disambiguated'
    ```

    The mechanism, in `DateParserPlugin.range_to_dt`
    (`whoosh/qparser/dateparse.py`): when exactly one bound is present, the
    branch that resolves it unconditionally calls
    `end.disambiguated(self.basedate)` (or the equivalent for a
    present-only `start`). That call is safe for every bound spelling that
    resolves to an `adatetime`/`timespan`: an absolute date, a bare
    month/year, `today`, `yesterday`, all measured directly and none crash
    alone. It is not safe for a bound resolving to a plain
    `datetime.datetime`, which has no `.disambiguated()`, and two bound
    spellings do exactly that: the literal keyword `now`, and any
    `PlusMinus`-shaped relative offset (`-7 years`, `+1 week`, any
    unit/granularity), since `PlusMinus.props_to_date` returns `dt + delta`
    directly with no `adatetime` wrapping. `range_to_dt`'s two-bounds
    branch avoids this entirely: when both bounds are present it wraps them
    in `timespan(start, end)` first, which does not have this problem, so
    the crash is specific to the single-bound path.

    whoosh-compat's `DateParserPlugin.range_to_node` parses every one of
    these cleanly, producing an ordinary open-ended `DateRange` with no
    diagnostic: not reproduced, since a genuine parsing defect on plausible
    input is not intended whoosh semantics to preserve.

    This entry's own claim must be checked before entry 12's broader
    bracketed-range pattern, which would otherwise also match this shape
    and assert a `MISMATCH` comparison the oracle never gets far enough to
    produce; `tests/differential/allowlist.py` orders it first for exactly
    that reason.

    A related but distinct shape, `created:[TO today]` (no leading space
    before `TO`), also crashed real whoosh, but through an entirely
    different mechanism: a `RangePlugin` tokenizer ambiguity, not this
    entry's `.disambiguated()` wiring defect. Originally found to affect
    whoosh-compat identically; now fixed on whoosh-compat's side instead,
    see entry 56.

    Test references: `tests/differential/corpus_realworld.txt`'s
    `created:[ TO -7 years]` line and its matching
    `tests/differential/allowlist.py` `DivergenceKind.ORACLE_ERROR` entry.
    Entry 12's own text and allowlist pattern also changed alongside this
    one: a range whose *both* bounds are relative offsets
    (`created:[-1yr to -0yr]`) compares EQUAL and is no longer claimed by
    entry 12, since a relative offset is computed identically on both sides
    regardless of the tz-reversal bug entry 12 describes; see entry 12's
    own text and `tests/differential/corpus_realworld.txt`'s "CONFIRMED
    PARITY" lines.

56. **A range's start bound is mis-tokenized as the literal text `TO` when
    a bound-less range's opening bracket sits directly against the `TO`
    separator and the following bound value also begins with a "to"-shaped
    word (whoosh bug, fixed in whoosh-compat).** whoosh's (and,
    until now, whoosh-compat's identically-forked) `RangePlugin.expr`
    recognizes the `TO` separator with `[^\]}]+?(?=[Tt][Oo])` for the start
    bound followed by a bare literal `[Tt][Oo]`, with no word-boundary
    requirement anywhere. For `created:[TO today]` (no space between `[`
    and `TO`), the regex's greedily-attempted, internally-non-greedy start
    group finds its *own* shortest satisfying match by walking three
    characters into the string ("TO ", including the space before "today"),
    because the lookahead `(?=[Tt][Oo])` is satisfied by the "to" that
    starts "today" itself. That consumes the real separator as the start
    bound's text and leaves "day" behind as the end bound, rather than
    recognizing the range as bound-less (empty start) with "today" as the
    end. Confirmed against the pinned oracle: real whoosh crashes outright
    on this shape for a DATE field, `Exception("'TO' is not a parseable
    date")` (`whoosh/fields.py`'s `_parse_datestring`, called on the
    captured text `"TO"`), and silently misparses a non-crashing field type
    the same way (`title:[total 5]` parses to a `Range` object with an
    empty start and end `"tal 5"` in real whoosh, `total`'s own leading
    "to" mistaken for the separator with no `TO` token anywhere else in the
    string at all). Neither is intended whoosh semantics: a genuinely
    ambiguous regex with no distinguishing signal for "user typed `TO` as
    the separator" versus "the separator symbol happens to reappear inside
    an adjacent word" is a defect, not a design choice, so whoosh-compat
    does not reproduce it.

    The fix, in whoosh-compat's own forked `RangePlugin.expr`
    (`src/whoosh_compat/parser/plugins.py`): wrap the separator recognition
    in `\b`, in both the start-bound lookahead and the literal match that
    follows it, so `TO`/`to` is only recognized as the separator token at
    an actual word boundary, never mid-word. Verified empirically across a
    battery of shapes (ordinary numeric/month/date ranges, quoted bounds,
    bound-less ranges, a start value that itself contains "to" as a
    substring without being a boundary match, `into`/`town`/`tomorrow`
    bound values) that every previously-correct spelling parses
    identically: the fix only changes behavior for the specific ambiguous
    shapes above. `title:[total 5]` (no genuine `TO` anywhere) is no longer
    tagged as a range at all after the fix, rather than the garbage
    empty-start/`"tal 5"`-end `Range` object whoosh (and, before this fix,
    whoosh-compat too) built. Before the fix, whoosh-compat's own garbage
    `TermRange` for this shape fell under entry 5's "text-field ranges are
    unsupported at emit time" refusal; after the fix there is no `Range`
    node at all to refuse, since the query resolves as ordinary multifield
    term matching instead, outside entry 5's territory entirely.

    Two allowlist claims cover the blast radius the corpus actually
    exercises: a `DivergenceKind.ORACLE_ERROR` claim for the DATE-field
    crash (`created:[TO today]`, matching entry 55's classification for the
    same reason: the oracle never produces a query object to compare), and
    a `DivergenceKind.MISMATCH` claim for the non-crashing, silently-wrong
    case on other field kinds (`title:[total 5]`, `title:[into TO 5]`).
    Both are scoped to the literal "to"-prefixed vocabulary measured
    (`today`, `tomorrow`, `total`, `into`), not generalized to every English
    word beginning with those two letters, since only those were confirmed
    against the oracle.

    Test references: `tests/test_plugins_unit.py`'s
    `test_range_tagger_to_separator_requires_word_boundary` and
    `test_range_tagger_no_to_at_all_is_not_a_range`;
    `tests/differential/corpus_realworld.txt`'s `created:[TO today]` line
    and `tests/differential/corpus_docs.txt`'s `title:[total 5]` /
    `title:[into TO 5]` lines, with their matching
    `tests/differential/allowlist.py` entries.

57. **A second (or later) rejected field-name candidate in a row silently
    discards the earlier one's text instead of merging it in, and any
    merge at all leaves the surviving node's span shorter than its text
    (whoosh bug, fixed in whoosh-compat).** `FieldsPlugin.do_fieldnames`
    (`src/whoosh_compat/parser/plugins.py`, forked verbatim from whoosh's
    identical loop) cleans up `FieldnameNode`s the low-level tagger created
    for a `word:` -shaped run that turned out not to name a registered
    field, folding each rejected candidate's `.original` text back onto
    whatever comes after it. The loop tracked only a single
    `prev_field_node` reference: when a second rejected candidate followed
    a first one with nothing recognized in between, assigning the new node
    to `prev_field_node` simply overwrote the old reference, and the first
    candidate's `.original` was never read again by anything. This is not
    hypothetical: an unquoted value containing two colons where neither
    interior segment is a real field name reaches exactly this path.

    Measured directly (`aa:bb:cc`, no field named `aa` or `bb` in the
    schema): the tagger emits `FieldnameNode("aa", "aa:")`,
    `FieldnameNode("bb", "bb:")`, `WordNode("cc")`. Real whoosh's
    `do_fieldnames` reduces this to the single word `"bb:cc"` per default
    field (confirmed against the pinned oracle: `content:bb AND
    content:cc`, and the same pair on every other default text field) with
    `"aa:"` gone with no trace anywhere in the resulting query, not even
    folded into a longer literal term. There is no reading under which the
    user's `"aa:"` was ever consulted. whoosh-compat now instead accumulates
    every rejected candidate's `.original` text, in order, before folding
    it onto the next node, so `aa:bb:cc` keeps meaning the literal text the
    user typed (`"aa:bb:cc"` per default field, further tokenized by each
    field's own analyzer downstream exactly as `"bb:cc"` was before).

    Nothing about the bug depends on what the segments say, so the
    allowlist claim is scoped to the mechanism rather than to that one
    measured spelling: two `\w+:` runs back to back where neither names a
    registered field. Measured, all of `zzz:and:9` (a stopword segment),
    `zzz:the:the`, `zzz:a:the`, `zzz:a:b`, `zzz:9:9`, `ab:ab:ab`,
    `zzz:ab:cd` and `ab:cd:ef` reach the oracle with the first candidate's
    text gone, exactly as `aa:bb:cc` does. Several of them were previously
    claimed under entry 15's reason string, which describes a combinator
    difference these queries do not exhibit: the two sides disagree about
    the value's text first, before any combinator question arises. Three
    exclusions, also measured: a recognized field name in either position
    stops the candidate run on both sides (`title:ab:cd`, `zzz:title:cd`,
    both EQUAL); a dot anywhere in the run makes the two taggers cut in
    different places before this bug can apply, which is entry 14's
    mechanism (`zzz:ab.cd:ef`); and a quoted or bracketed continuation
    after the second colon is claimed by a quote or range plugin before
    either tagger's cut matters.

    The companion span bug is present even with only a *single* rejected
    candidate, and shares the same root cause (this fold-in never touched
    `startchar`): `FieldsPlugin.do_fieldnames`'s merge step reassigns the
    surviving node's `.text` to the concatenation but leaves `.startchar`
    pointing at the surviving node's own original position, so
    `endchar - startchar` no longer matches `len(text)`. This is the same
    merge code behind the leftover fragment DIVERGENCES.md entry 54's own
    "What `raw_value` carries" paragraph discusses beside a `BAD_DATE`
    diagnostic: measured directly here, for `added:2005-01-01T00:00:00Z`
    that leftover was `Term(startchar=23, endchar=26, text='00:00Z')`, a
    3-character span holding 6 characters of text. That specific example is
    fixed by this same change, not a separate date-only fix. Now both bugs
    are fixed together: the merge widens
    `startchar` back to the earliest rejected candidate's own start, so a
    node's span always covers exactly its own text.

    This is a general-purpose parser fix, not scoped to dates or to any
    particular field kind: it changes what `FieldsPlugin.do_fieldnames`
    reconstructs for *any* unquoted value containing consecutive
    unrecognized `word:` runs, on any field (or none). Verified against the
    full test suite and differential corpus that no currently-passing
    comparison flips: every corpus query that reaches a `BAD_DATE`
    diagnostic already takes the entry-6 diagnostic skip regardless of the
    leftover term's exact text (`ast.Node.text` differences there are moot,
    since the whole comparison is skipped), and the only diagnostic-free
    corpus line touching a single rejected candidate (`**:::`, entry 20's
    neighborhood) only has its span change, which `ast.Node` excludes from
    equality by design (`compare=False`).

    Test references: `tests/test_plugins_unit.py`'s
    `test_do_fieldnames_demoted_span_widens_to_cover_merged_text`,
    `test_do_fieldnames_consecutive_demoted_candidates_keep_all_text`, and
    `test_do_fieldnames_consecutive_demoted_candidates_at_end_of_group`;
    `tests/differential/corpus_docs.txt`'s `aa:bb:cc` / `zzz:and:9` lines,
    with their matching `tests/differential/allowlist.py` entry;
    `tests/differential/test_allowlist_xref.py`'s
    `test_entry_57_claims_consecutive_rejected_field_candidates` and
    `test_entry_57_does_not_claim_single_or_recognized_candidates`.

58. **A `BAD_DATE` diagnostic's `raw_value` now reports the full
    contiguous value the user typed, not just the fragment the tokenizer
    cut it to before the date grammar ran (whoosh-compat improvement, no
    whoosh equivalent).** Supersedes entry 54's "What `raw_value` carries"
    paragraph. Before this entry, `raw_value` was exactly the text
    `_error` (`src/whoosh_compat/parser/dateparse.py`) received: for
    `added:2005-01-01T00:00:00Z` that was `2005-01-` (measured there),
    because the tokenizer's colon-boundary detection had already cut the
    value before any date parsing happened, and the rest of the timestamp
    reached `do_dates` as a separate, unfielded sibling node the date
    grammar never saw. A host quoting that back at a user was quoting
    something nobody wrote, exactly the problem entry 54 already
    described.

    `do_dates`'s new `_widen_bad_date_error` (immediately after
    `text_to_node`/`range_to_node` produces a `DateErrorNode`) looks ahead
    at the group for text immediately (no gap: `sib.startchar == end`)
    after the rejected fragment, via the new `_leftover_fragment_text`
    helper, and folds each one's text into the diagnostic's
    `message`/`raw_value`, widening `startchar`/`endchar` (both the
    `Diagnostic`'s own copy and the resulting `ast.ErrorLeaf` node's) to
    match. For `added:2005-01-01T00:00:00Z`, `raw_value` is now the full
    `2005-01-01T00:00:00Z`.

    `_leftover_fragment_text` recognizes two shapes, not one, and the
    filter ordering this relies on is load-bearing, not incidental:
    `FILTER_MULTIFIELD` and `FILTER_DATES` share a filter priority (110),
    with `MultifieldPlugin` always winning that tie (it is registered
    before `DateParserPlugin` inside `MultifieldParser.__init__`), so by
    the time `do_dates` runs, an originally-unfielded leftover sibling has
    *already* been rewritten by `do_multifield` into an `OrGroup` of one
    same-text, same-span `copy.copy` of itself per default field.

    `do_dates` must keep running AFTER `do_multifield`, not before: an
    UNFIELDED value on a DATE default field (`wc.parse("yesterday",
    default_fields=["content", "added"], ...)` with `added` a DATETIME
    field) depends on `do_multifield` running first to assign it the
    `added` fieldname before `do_dates` can recognize it as a date at all
    (`do_dates` only ever looks at a node's OWN fieldname, which an
    unfielded node has none of until multifield assigns one). Reordered
    the other way, `do_dates` would see the still-unfielded node, find no
    fieldname to resolve a spec from, and skip it entirely, so
    multifield's later expansion would produce a literal `Term`/`TermRange`
    on a DATETIME field instead of a `DateRange` -- a silent divergence
    from real whoosh (verified against the oracle: whoosh produces the
    `DateRange`), and one nothing in the existing suite exercised, since
    nothing put a DATE/DATETIME field in `default_fields` before this
    entry. The filter priorities therefore stay exactly as they were
    (`do_dates` still runs after `do_multifield`); `_leftover_fragment_text`
    is what was taught to also recognize the multifield-rewritten
    `OrGroup` shape instead, detected structurally (every child a
    `WordNode` with identical text/span, differing only in fieldname/boost)
    rather than by field membership, so a genuine multi-term `OrGroup` the
    user actually wrote is never mistaken for one.
    `test_unfielded_value_still_resolves_as_a_date_with_a_date_default_field`
    pins this case.

    This entry's own widening deliberately does **not** cross whitespace.
    A date value ends at the
    first space by this grammar's own design (`DateParserPlugin` never
    supports whoosh's "free" undelimited multi-word mode, see the class
    docstring), so `week` in `added:-1 week` was never part of the
    attempted value that this entry widens the report of, and when this
    entry was written there appeared to be no principled stopping point
    for how many trailing whitespace-separated words to absorb once that
    boundary is crossed (`added:-1 week invoice` -- is `invoice` part of
    the value too?).

    Both halves of that are superseded by entry 61, which supplies the
    stopping point: full consumption by the date grammar. It answers the
    question above directly, and the answer is no: `invoice` is not part
    of the value, because `-1 week invoice` does not parse in full while
    `-1 week` does. The resolution reached is also the opposite of the one
    this entry anticipated. The "scoped, narrower version of whoosh's free
    mode" imagined here would have made these shapes *parse
    successfully*; what shipped instead **rejects** them, so
    `added:-1 week` is now a `BAD_DATE` naming `-1 week` in full and
    telling the user to quote it, rather than a `-1` diagnostic with
    `week` surviving as a term. This entry's widening mechanism is
    unchanged by that; entry 61 simply reaches the whitespace-separated
    shapes first, so the ones described here as out of reach are no longer
    the ones that arrive.

    Also does not widen past a wildcard/prefix leftover:
    `added:2005-01-01T00:00:00Z*` still reports only `2005-01-`, since
    `WildcardPlugin.do_wildcards` (priority 50) has already turned the
    trailing `*`-bearing fragment into a `WildcardNode`/`PrefixNode` by the
    time `do_dates` runs, which `_leftover_fragment_text` does not
    recognize (deliberately: a wildcard pattern is not plain leftover
    text). Narrower than the shape this entry set out to fix, but not
    incorrect: the diagnostic still reports a real, if partial, prefix of
    what the user typed, same as before this entry for that one spelling.

    Widening only applies when the rejected value's own `raw_value` is
    EXACTLY the source text at its node's own span: a bare, unquoted
    `WordNode`. `do_dates` states this as a positive precondition
    (`type(node) is syntax.WordNode`) rather than excluding specific node
    kinds one at a time, because two different kinds fail it for two
    different reasons, and a blacklist approach only ever excludes the
    ones already found:

    * a `range_to_node` error (a bracketed range whose bound(s) fail to
      parse) has a `raw_value` that is a single BOUND string (`qqq` out of
      `[qqq TO zzz]`), not a slice of the source text at the error node's
      own span at all; gluing a following contiguous sibling's text onto
      it produces a string that appears nowhere in the query
      (`added:[qqq TO zzz]foo` would report `raw_value='qqqfoo'`);
    * a quoted `PhraseNode` (`added:"qqq"foo`) has its surrounding quotes
      stripped from `.text`, but its span still includes them, so
      `raw_value` is a slice of the source text with a two-character
      offset the naive concatenation does not account for (same failure
      mode, `raw_value='qqqfoo'` instead of `'qqq'`).

    Both are pinned:
    `test_range_error_raw_value_is_not_glued_to_a_trailing_leftover` and
    `test_quoted_value_error_raw_value_is_not_glued_to_a_trailing_leftover`.

    A third scope limit, this one specific to a DATE/DATETIME field in
    `default_fields`: the fielded and unfielded spellings of the same
    value widen differently. `added:2005-01-01T00:00:00Z` (explicitly
    fielded) widens as described above. The bare `2005-01-01T00:00:00Z`
    (relying on `added` being a default field) does not: `do_dates`
    recurses into the `OrGroup` `do_multifield` already built for it
    before `do_dates` runs, and the sibling lookup inside that recursion
    only ever sees the `OrGroup`'s own children (all sharing one
    startchar), never a leftover outside it. `raw_value` stays `2005-01-`
    for the unfielded spelling, exactly the pre-entry-58 behavior. Still
    correct (an unwidened `raw_value` is still a genuine source-text
    slice, just a shorter one), just narrower than the fielded case.

    Widening a fielded value on a DATE default field also introduces a
    new, and so far undeduplicated, overlap: the leftover sibling is
    ITSELF attempted as a date under a DATE default field (the same
    `OrGroup` recursion above tries to date-parse its `added` copy), so it
    can raise its own `BAD_DATE` diagnostic with a span nested entirely
    inside the widened diagnostic's span (`(6, 26)` containing `(14,
    26)` for `added:2005-01-01T00:00:00Z` with `added` in
    `default_fields`). Before this entry the two diagnostics' spans were
    adjacent and disjoint; a host that highlights every diagnostic span in
    a query will now double-highlight that overlapping range. No query
    semantics change (both diagnostics already existed; only their spans
    now overlap instead of touching), so this is left as a known, mild
    display wrinkle rather than fixed here: suppressing the
    now-partially-redundant inner diagnostic would need do_dates to know
    about a WIDER diagnostic that hasn't been computed yet at the point it
    processes the inner one, a larger restructuring than this entry's
    scope.

    This is purely a diagnostic-reporting change: the leftover node(s)
    folded into `raw_value` are left exactly where they already were in
    the tree (still an ordinary term on whatever default field(s) apply),
    so what the query actually searches for is completely unchanged, only
    what the diagnostic reports about the rejected value widens.
    Consequently this needs no differential-triage paperwork (allowlist
    entry, corpus line): `Diagnostic`/`raw_value` has no whoosh equivalent
    to diverge from, and the AST shape the differential harness compares
    is unaffected (the entry-6 diagnostic skip already treats any
    `BAD_DATE`-bearing query as unconditionally out of structural
    comparison, regardless of what the diagnostic inside it says).

    Test references: `tests/test_parser_dates.py`'s
    `test_bad_date_raw_value_widens_to_cover_a_contiguous_leftover_fragment`
    (the `OrGroup`-shaped leftover, the only shape reachable through
    `whoosh_compat.parse()`),
    `test_bad_date_raw_value_widens_via_a_bare_wordnode_leftover` (the
    other shape, only reachable through a plain, no-default-field
    `QueryParser`),
    `test_bad_date_raw_value_does_not_widen_past_a_wildcard_leftover`,
    `test_whitespace_separated_value_is_rejected_as_a_whole` (the
    whitespace boundary, as entry 61 leaves it),
    `test_unfielded_value_still_resolves_as_a_date_with_a_date_default_field`
    (the multifield-ordering regression above),
    `test_range_error_raw_value_is_not_glued_to_a_trailing_leftover` and
    `test_quoted_value_error_raw_value_is_not_glued_to_a_trailing_leftover`
    (the scope-limiting precondition above), and the updated
    `test_bare_unquoted_t_value_is_rejected_not_truncated` (entry 54's own
    pinned example, now asserting the widened value).

59. **The oracle's `StopFilter(minsize=2)` drops short tokens the real host
    analyzer keeps, which can make the two sides disagree on token *count*
    itself, not just on how a combinator resolves it (design).** Real
    whoosh's `StandardAnalyzer`, which `tests/differential/oracle.py`'s
    `_analyze` uses for every TEXT field the oracle compares against, chains
    a `StopFilter` with its default `minsize=2`: any token shorter than two
    characters is dropped outright, stopword or not. Paperless-ngx's actual
    production chains, `lower_fold`/`stem_fold` in
    `tests/emitter/conftest.py`, have no such filter and keep a token of any
    length. For most values this never matters, since most tokens are two
    characters or longer on both sides. It matters for a value whose
    analysis produces an interior piece shorter than that: the `02091-C-71`
    family (`02091-C-71`, `02091-C-712`, `02091-C-71a`, `02091-C-76hallo`,
    each splitting on the dash into a one-character middle piece the oracle
    drops and the host keeps), `200[1-9]`'s bracket-class body, `9,90` read
    as a comma-decimal TEXT value, and two Devanagari segmentations
    (`वर्तमान`, `वर्तमान क्षण की धन्यता`) all analyze to a different number of
    surviving tokens on the two sides.

    This is a different mechanism from entry 15's, not a restatement of it.
    Entry 15 assumes both sides tokenize a value into the *same* count and
    diverge only on whether the surrounding group combines those tokens with
    AND or OR. Here the token count itself differs before any combinator is
    even reached, so there is no shared tree shape for a combinator choice
    to diverge over in the first place. The classification is design, not
    whoosh-bug: whoosh-compat is not wrong to keep the short token, because
    it is following the real host analyzer, the thing it exists to be
    faithful to. It is the oracle's `StandardAnalyzer`, with its baked-in
    `minsize=2`, that is the less representative stand-in for what
    paperless-ngx's production index actually does with these values, the
    same reasoning entry 4 already applies to whoosh's baked-in stopword
    filter.

    `9,90` deserves a note so the two facts about it are not read as one.
    A *different*, now-resolved concern already covers this exact value at
    the AST-comparison layer, as a corpus line
    (`tests/differential/corpus_realworld.txt`) claimed by entry 15's
    allowlist regex and pinned by
    `tests/differential/test_allowlist_xref.py`'s `bare-comma-keyword-path`
    case, though entry 15's own prose does not single the value out: on a
    comma-values KEYWORD field, an unquoted `9,90` is split at parse time by
    the `CommaValuesPlugin`/oracle's own comma handling, which is a
    query-level, AST-combinator mechanism. The mismatch
    this entry documents is unrelated: read as a TEXT value, `9,90`'s comma
    separates a piece too short to survive the oracle's `minsize=2` but not
    the host's, so the two sides disagree on token count. Both facts are
    real and both are documented, in their own entries, for their own
    mechanisms; neither is a restatement of the other.

    Test references: `tests/differential/test_analyzer_boundary.py`'s
    `test_lower_fold_token_count_matches_whoosh` and
    `test_stem_fold_token_count_matches_whoosh`, run against the
    `_XFAIL_MULTITOKEN_BOUNDARY`-marked entries in that module's
    `REPRESENTATIVE_VALUES`: `interior-1char-dash-piece`,
    `interior-1char-dash-piece-2`, `interior-1char-dash-piece-3`,
    `interior-1char-dash-piece-4`, `bracket-class-no-wildcard`,
    `comma-decimal`, `devanagari-single-word` and `devanagari-phrase`.

60. **An unquoted term containing an unambiguous single-character bracket
    range and no `*`/`?` is diagnosed at parse time instead of silently
    searched as a literal that essentially never matches (design, not a
    whoosh bug being declined; the unsupported-pattern diagnostic, extended
    to a shape entries 29/30 don't cover).**
    Real whoosh's `WildcardPlugin` (`qparser/plugins.py`) only tags text as
    a wildcard when it contains `*` or one of a handful of `?`-like
    characters (`WildcardPlugin.expr = "(?P<text>[*%s])" % qmarks`); `[` is
    never one of the trigger characters, so `title:200[1-9]` lexes as an
    ordinary term on both whoosh and whoosh-compat, and `query.Wildcard`
    never even gets constructed. `Wildcard.normalize()` (`query/terms.py`)
    shows the same asymmetry from the other side: it folds a wildcard node
    back down to a plain `Term` whenever neither `*` nor `?` appears in the
    text, even though its own `SPECIAL_CHARS = frozenset("*?[")` lists `[`
    as a pattern character elsewhere in the same class. Both are
    consistent-with-itself parity behavior on whoosh's side, not a defect:
    whoosh simply never treats a bracket class as meaningful unless it
    rides along with a genuine wildcard character.

    The consequence for a user is what makes this worth diverging over.
    `title:200[1-9]` is searched as the literal nine-character string
    `"200[1-9]"`, which essentially no real document contains, so the query
    silently matches nothing instead of raising any kind of error. That is
    a strictly more dangerous failure mode than the wrong-field-kind
    mangling entries 29 and 30 already refuse: those still search
    *something* recognizable, where this searches for a string a user
    never meant to type. whoosh-compat reports a
    `DiagnosticKind.SINGLE_CHAR_BRACKET_RANGE` diagnostic and an
    `ErrorLeaf` instead, from a new, narrowly-scoped check
    (`QueryParser._single_char_bracket_range_diagnostic` in
    `parser/default.py`) wired into `term_query`'s final fallthrough (the
    branch reached for an ordinary, unquoted `Term` on a TEXT/KEYWORD/
    unknown field), not into `wildcard_query`'s `_wildcard_kind_diagnostic`:
    the two checks fire on different triggering conditions (field *kind*
    for a genuine wildcard pattern, vs. text *shape* for something that
    isn't a wildcard at all) and stay independent. Machine-identifiable via
    `Diagnostic.divergence == 60`.

    The rule is deliberately narrow: only an unquoted term whose text
    contains a bracket class `[X-Y]` where `X` and `Y` are each exactly one
    character, and that contains neither `*` nor `?` anywhere, is
    diagnosed. A multi-character range (`title:invoice[2020-2021]`) is not
    ambiguous with any wildcard syntax and keeps its current literal-term
    behavior untouched. Any bracket text already combined with a wildcard
    character (`title:200[1-9]*`, `title:*200[1-9]`) reaches
    `wildcard_query` instead, is tagged a genuine `Wildcard`, and is
    unaffected; the diagnostic's own message points a user at exactly this
    spelling as the fix. A double-quoted value (`title:"200[1-9]"`) never
    reaches `term_query` at all, since double-quoting produces a `Phrase`
    node on both whoosh and whoosh-compat; single-quoting does not change
    node type the same way, so a single-quoted value
    (`title:'200[1-9]'`) stays a `Term` and is still diagnosed, with the
    message still pointing at double-quoting (not single) as the literal
    escape hatch.

    Entry 59 separately documents an *analysis-time* token-count divergence
    that also happens to use `200[1-9]` as an example value: after this
    entry, that concern is moot for the exact query text `title:200[1-9]`,
    since the query never reaches analysis at all now, but the underlying
    analyzer-fidelity difference entry 59 describes is real independent of
    this entry and stays documented for the raw-value comparison
    `tests/differential/test_analyzer_boundary.py` runs directly against
    the analyzer callables (bypassing the parser entirely, so it is
    unaffected by this entry's parse-time check).

    Test references: `tests/test_parser_fields.py`'s
    `test_single_char_bracket_range_term_is_diagnosed` (a bare bracket
    range, a term with more than one such range, and the single-quoted
    spelling) and `test_single_char_bracket_range_diagnostic_is_narrowly_scoped`
    (literal brackets with no range, a bracketed single word, a
    double-quoted phrase, a multi-character range, and both wildcard-combined
    spellings, none of which diagnose); `tests/test_parser_basics.py`'s
    `test_single_char_bracket_range_in_term_position_is_diagnosed`;
    `tests/differential/corpus_realworld.txt`'s `title:200[1-9]` (the exact
    query from the source report), which now skips via the existing entry 6
    diagnostics-present check instead of comparing structurally equal, and
    is pinned by `tests/differential/test_differential.py`'s
    `test_diagnostic_skip_count_matches_corpus`.

61. **An unquoted multi-word date value on an explicitly named date field
    is rejected (`Diagnostic(kind=BAD_DATE)`) instead of silently
    truncating to its first word (whoosh-bug, not reproduced).**
    `created:december 2019` is one date value in any reading a user would
    give it. The whoosh grammar ends a value at the first space, so the
    date field receives `december` alone, which resolves against the base
    date to December of the *current* year, and `2019` is left behind as
    an ordinary default-field term ANDed onto the query. None of that is
    reported: the query runs, and returns documents from the wrong year
    that happen to mention 2019.

    Measured against the pinned oracle (basedate 2026-08-04 10:30
    Europe/Berlin), real whoosh degrades exactly that way, in each of the
    three shapes this rule covers:

    ```
    created:december 2019 -> created:[Dec 2026] AND (content:2019 OR ...)
    created:2020 to 2021  -> created:[2020]     AND (tag:to) AND (...2021)
    created:2020 august 4 -> created:[2020]     AND (...august) AND (tag:4)
    ```

    (The leftover terms are shown on a KEYWORD default field, since on a
    TEXT one the oracle's analyzer removes `to` as a stopword and `4` as a
    short token before the query is built, per entry 59. The date bound is
    the part that matters, and it is the same either way.) In none of the
    three does whoosh raise, warn, or report anything; each is a working
    query for something the user did not ask for.

    Diverged from anyway, under the rule entry 54 applies to the
    half-consumed case: a silently wrong query with no diagnostic is a
    defect, not a convention. Unlike entry 54's shape, every spelling this
    rule rejects already has a working spelling that means what the user
    meant, so the diagnostic can name the fix rather than only the
    problem: `created:"december 2019"`, `created:[2020 TO 2021]` and
    `created:"2020 august 4"` all parse to the range described, and the
    message says so (`'december 2019' is a date value written without
    quotes; quote it as created:"december 2019"`). `kind` stays
    `BAD_DATE` rather than becoming a new kind, because `kind` is the
    machine-stable half of the contract a host branches on and a host that
    already routes BAD_DATE to an invalid-date response needs no change to
    route this; the distinction lives in the message.

    The stopping rule, which is what makes this implementable at all: **a
    joined candidate is rejected only if the grammar consumes it in
    full.** `DateParserPlugin.do_unquoted_date_values` (filter priority
    `FILTER_UNQUOTED_DATE_VALUES = 102`) takes a bare `WordNode` carrying
    an explicit DATE/DATETIME field name, collects the plain unfielded
    word siblings following it (anything else, an operator, a group, a
    wildcard, a phrase, a word with its own field name, ends the run),
    joins them longest-first with single spaces, and replaces the whole
    run with the diagnostic for the first candidate that both parses and
    ends exactly at the end of its own text. If no candidate is consumed
    in full, nothing changes: the query keeps whatever it already meant.
    The lookahead is capped (15 words) purely as a budget, with no
    correctness claim of its own; `test_grammar_never_exceeds_lookahead_cap`
    is what keeps that number honest against the longest run the grammar
    can actually use.

    That is the principled stopping point entry 58 said did not exist, and
    it answers that entry's own question directly: in
    `added:-1 week invoice`, `invoice` is not part of the value, because
    `-1 week invoice` does not parse while `-1 week` does. The resolution
    is also the opposite of the one entry 58 anticipated. It expected a
    narrowed version of whoosh's free undelimited mode that would make
    these shapes *parse*; what shipped rejects them and tells the user to
    quote, on the ground that a query which cannot be read one way without
    guessing should not be guessed at.

    Explicitly fielded values only, for the reason `do_date_phrases` gives
    for the same restriction (entry 19): reaching a value through the
    *default* field would claim far more of the query than "the user wrote
    `added:` in front of it", and with a DATE field among the default
    fields every adjacent pair of words in the query would become a
    candidate. The cost of that exclusion is named rather than hidden: a
    plain `QueryParser` whose default field is itself a date field keeps
    the silent truncation this entry closes everywhere else, pinned by
    `test_date_default_field_is_deliberately_excluded`.

    Ordering against entry 19 is load-bearing, not incidental. Entry 19's
    `do_date_phrases` joins its six multi-word keywords at priority 101,
    ahead of this rule at 102, so by the time this filter runs
    `added:previous month` is already a single value the grammar accepts,
    not a two-word run to reject. Reversed, the two rules would contradict
    each other and the keyword widening entry 19 exists to provide would
    be unreachable.

    Ordering protects the *bare* phrase, and nothing more than that. A
    joined phrase is a perfectly ordinary word node, so it is also a
    legitimate head for this rule, which then extends the run over
    whatever plain words follow it. Measured, basedate 2026-08-04 10:30
    Europe/Berlin: `added:previous month` is clean, while
    `added:previous month to now` and `added:previous month 3 pm` are both
    rejected here, naming the whole run. Quoting repairs both
    (`added:"previous month to now"` and `added:"previous month 3 pm"`
    parse), which is the asymmetry this entry accepts everywhere else: the
    unquoted spelling of a complete multi-word date value is rejected, the
    quoted one is the way to write it. Pinned by the
    `joined-keyword-phrase-then-more-words` row of
    `test_unquoted_date_rejection_cell_matrix` and by
    `corpus_realworld.txt`'s `created:previous month to now` line.

    **The boundary against entry 54, and what enforces it.** Two word
    nodes can be adjacent with no whitespace between them, because a plain
    word is not tagged at all: it is whatever interstitial text is left
    between two tagger matches. In a bare RFC3339 timestamp the field-name
    expression (`[\w.]+:`) matches `04T10:` inside the value, and word
    characters do not include `-`, so that candidate starts right after
    the last dash and the text before it, `2026-08-`, is closed off as its
    own word. `do_fieldnames` then rejects `04T10:` and `30:` as unknown
    fields and chains them back onto the word after them (entry 57), which
    is why the colons themselves survive inside a single node. Measured,
    `added:2026-08-04T10:30:00` reaches this filter as `('2026-08-', 6 to
    14)` immediately abutting `('04T10:30:00', 14 to 25)`, a boundary one
    character past a dash rather than at any colon; the same chaining
    leaves `10:30` whole in `created:december 2019 10:30`, so a colon is
    not what splits a value here. The
    grammar accepts the space-joined form `2026-08- 04T10:30:00` as a full
    parse (measured), so with nothing to stop it this rule would swallow
    bare RFC3339 timestamps wholesale and quote back at the user a value
    containing a space nobody typed. The private helper
    `_whitespace_separated` is the guard: it truncates the collected run
    at the first word abutting its predecessor, so this entry claims only
    runs whose words were written with whitespace between them. Everything
    the tokenizer cut out of a single written-together value stays entry
    54's to reject and entry 58's to report, with `raw_value` widened by
    contiguity rather than joined with spaces. That is the whole of the
    boundary: whitespace on this side, contiguity on the other, and no
    query shape belongs to both. It is pinned directly by the
    `abutting-colon-split` row of `test_unquoted_date_rejection_cell_matrix`
    rather than only indirectly by the entry 54 and 58 regression tests
    continuing to pass.

    **The accepted regression.** A word that abuts a date value and
    happens to complete a longer date value with it is now rejected where
    it used to be an ordinary search term. `created:2020 august 4`,
    meaning "documents from 2020 that mention august 4", worked before and
    now errors, because `2020 august 4` is a valid date value; so do
    `created:2020 12` and `created:today 3pm`. The correction is to make
    the two halves explicit: `created:2020 AND august 4` gives back the
    2020 range plus the two terms, and `created:today AND 3pm` the day
    plus the term. Accepted deliberately: a user should be explicit when a
    search term abuts a date value, and the alternative is keeping a
    silently wrong query for the far more common case where the run really
    was one value. The regression needs the run to parse *in full*, which
    is narrower than it sounds: `created:2020 august` is untouched,
    because the grammar does not accept a year followed by a bare month
    name as a complete value (measured), and so is `created:2020 invoice`
    (entry 54's boundary paragraph), where the remainder is not part of
    any date.

    Test references: `tests/test_parser_dates.py`'s
    `test_unquoted_date_rejection_cell_matrix` (the kind/spelling
    exhaustiveness matrix, including the abutting colon-split row above),
    `test_date_default_field_is_deliberately_excluded`,
    `test_whitespace_separated_value_is_rejected_as_a_whole` (entry 58's
    question, answered), `test_grammar_never_exceeds_lookahead_cap` (the
    lookahead budget),
    `test_a_whitespace_separated_term_after_a_date_is_still_a_term` and
    `test_now_followed_by_unquoted_offset_words_reads_as_now_plus_free_text`
    (runs that do not parse in full and so are left alone).
    `tests/differential/corpus_realworld.txt`'s `created:december 2019`,
    `created:2020 to 2021`, `created:2020 august 4` and
    `created:previous month to now` lines each now skip via the existing
    entry 6 diagnostics-present check instead of comparing structurally
    equal, since this entry's rule is a parse-time diagnostic rather than
    a MISMATCH-shaped divergence: no `allowlist.py` entry is needed for
    them, only the corpus lines themselves and the updated count in
    `test_diagnostic_skip_count_matches_corpus`. The fifth corpus line,
    `created:2020 august`, is the accepted regression's own boundary case
    named above and is pinned precisely because it does not diagnose and
    still compares equal to the oracle.
