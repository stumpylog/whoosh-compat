# DIVERGENCES

whoosh-compat's parity bar is **whoosh's intended semantics**, not its
defects: where real whoosh (v2) has a confirmed bug, whoosh-compat does not
reproduce it. Where whoosh-compat (v1) makes a deliberate design choice with
no v2 equivalent, or restores behavior that the v2->v3 (tantivy) migration
silently dropped, that's recorded here too rather than left implicit.

Entries 1-11 were identified while designing the library, before any code
existed. Entries 12+ were found later, during triage of the differential
(AST-comparison, `tests/differential/`) and end-to-end acceptance (full
parse -> emit -> search, `tests/emitter/test_acceptance_e2e.py`) test
suites.

## From v2/Whoosh

1. Invalid dates/numbers yield diagnostics the host may turn into HTTP 400 (v2:
   silent empty results).
2. Wildcard/prefix patterns are case-folded via `pattern_normalizer` (v2 matched raw
   index terms, so `Entwä*` with a capital E failed in v2 too — this is a fix).
3. Date-node boosts are preserved (whoosh silently dropped them).
4. Stopwords are not removed (paperless v3 analyzer policy); affects ranking and
   makes stopwords searchable, not matching correctness under implicit AND.
5. Text-field ranges unsupported at emit in v1 (worked in v2).
6. Structured `ErrorLeaf`/diagnostics replace whoosh's `error_query` NullQuery
   wrappers.

## From live v3

7. Implicit AND restored between bare terms (v3 is implicit OR — a silent migration
   regression vs documented behavior).
8. Attached `-foo` searches for `foo` (v3: MustNot → matches nothing).
9. Field boosts apply only to multifield-expansion nodes, not explicitly fielded
   terms (v3 boosts both).
10. Open-ended date ranges are true open bounds (v3: sentinel dates `0001`/`9999`).
11. Nested all-negative boolean groups work (v3 inherits tantivy#3025's zero-hit bug).

## Additional entries (Tasks 11/15 triage)

12. **Date-range tz bypass (whoosh-bug, not reproduced).** Real whoosh's
    `DateParserPlugin.range_to_dt`
    (`whoosh/qparser/dateparse.py`) calls
    `self.dateparser.get_parser().date_from(...)` — the bare grammar
    object's `date_from`, **not** the configured dateparser's own
    `date_from` override — so a `LocalDateParser.date_from` override that
    reverses a local-timezone offset back to UTC (exactly what
    paperless-ngx v2's `LocalDateParser` does, and what the oracle harness
    clones as `oracle.LocalDateParser`) never actually runs for **bracketed
    ranges**, only for single/keyword values
    (`DateParserPlugin.text_to_dt`, which does call
    `self.dateparser.date_from`). Naive range bounds are therefore taken as
    literal UTC in real v2 with no local-tz shift applied at all — a
    wiring defect in v2, not an intended design choice. whoosh-compat's
    `DateParserPlugin.range_to_node`
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

13. **`Wildcard.normalize()` bracket fold drops character classes
    (whoosh-bug, not reproduced).** Real whoosh's `SPECIAL_CHARS` constant
    (`whoosh/query/terms.py`) is `"*?["`, but `Wildcard.normalize()` (same
    file) — and, independently, `WildcardPlugin.do_wildcards`
    (`whoosh/qparser/plugins.py`) — only ever *tests* for `"*"`/`"?"`
    before folding a trailing-star pattern down to a `Prefix`. A pattern
    like `202[0-3]*` (paperless-ngx issue
    [#13568](https://github.com/paperless-ngx/paperless-ngx/issues/13568):
    saved views built around bracket-class year ranges) therefore folds to
    `Prefix('title', '202[0-3]')`, silently destroying the character
    class instead of keeping the full wildcard pattern. whoosh-compat
    fixed the fold check in **both** sites that perform this
    optimization — `parser/default.py`'s `_TRAILING_STAR_RE` and
    `parser/plugins.py`'s `do_wildcards` — to also check for `"["`, so a
    trailing-star-with-bracket pattern stays a `Wildcard` instead of
    losing its class body.

    Test references: `tests/differential/corpus_docs.txt`'s
    `title:202[0-3]*` line plus its matching `tests/differential/allowlist.py`
    entry (`\btitle:202\[0-3\]\*`); `tests/emitter/test_emit_patterns.py`
    (`test_wildcard_emission`'s `character-class-13568` /
    `13568-leading-star-class` cases exercise the emitter's own
    class-preserving behavior directly, independent of the parser fold);
    `tests/emitter/test_acceptance_e2e.py::test_issue_13568_acceptance`
    (the *leading*-star form from the actual issue report, where this bug
    does not trigger at all — see entry 14 below).

14. **JSON dotted-path fields (`notes.user`, `custom_fields.value`, ...)
    are a v1-only concept with no v2 analogue whatsoever (design).** Real
    v2 whoosh has no JSON field type; v2's own `notes`/`custom_fields`
    fields were plain `TEXT()`. There is no query a v2 paperless user could
    type that reaches "the note left by a specific user" the structured way
    v1's JSON subpath (`FieldRegistry.resolve_json`) does. On *both* sides
    the dotted name is technically "unknown" to the v2 schema, but the two
    parsers' fieldname taggers handle an unregistered dotted name
    differently — whoosh-compat's `FieldsPlugin` tagger is deliberately
    dot-inclusive (`[\w.]+:` vs whoosh's `\w+:`) so a *registered* JSON
    field can resolve `notes.user:`, which as a side effect also makes it
    greedily tag the whole dotted run even when unregistered — so the
    resulting (unmapped) trees genuinely differ in shape, not just in
    whether the field resolves.

    Test references: `tests/differential/allowlist.py`'s two `custom_fields\.`
    / `notes\.` entries (AST-level: neither side's tree matches, by
    construction); `tests/emitter/test_acceptance_e2e.py::test_notes_user_json_subpath_has_no_v2_analogue`
    (result-level: demonstrates concretely that `notes.user:alice` matches
    doc 1 through the v1 JSON-subpath emitter but nothing at all through a
    v2-shaped whoosh oracle index, even when that index's `notes` field is
    populated with the same underlying data flattened to plain text).

15. **`Multitoken.DEFAULT` uses position-dependent enclosing-group context;
    real whoosh's `multitoken_query='default'` uses the parser's fixed
    default group (design note, not a bug).** whoosh-compat's
    `Multitoken.DEFAULT` (`src/whoosh_compat/fields.py`) resolves "how do
    multiple tokens from one field value combine" by looking at the
    *actual* enclosing group at the term's position in the parsed tree
    (`TantivyEmitter._group_stack`, `src/whoosh_compat/emitters/tantivy_.py`)
    — an `Or(...)` group's multitoken children combine with OR, an
    `And(...)` group's combine with AND. Real whoosh's default
    (`whoosh/qparser/default.py:191`, `multitoken_query='default'`) instead
    always uses the *parser's* single configured default group class,
    regardless of which group a term happens to sit inside syntactically.
    These agree for the common case (a multitoken term inside the query's
    top-level default group) but can diverge for a multitoken field value
    nested inside an explicit top-level `Or(...)` when the parser's
    configured default group is `And` (paperless v2 and whoosh-compat's own
    default `default_group_and` behavior, DIVERGENCES #7) — whoosh-compat
    would combine that term's tokens with OR (following the enclosing
    group), while real whoosh would still combine them with AND (following
    the parser's fixed default), even though both sides are looking at the
    exact same syntactic position.

    This was not hit by name in this task's corpus (no differential/
    acceptance case currently nests a genuine multitoken field value inside
    a top-level `Or`), but it is a known, understood shape of divergence
    baked into `Multitoken.DEFAULT`'s design rather than an implementation
    defect — do not "fix" it by making the emitter track the parser's
    single default group instead of the syntactic enclosing group if it
    surfaces later; that would just move the divergence rather than remove
    it (whoosh-compat's own position-dependent behavior is arguably more
    intuitive for a hand-written query, since it means "what you see is
    what groups together").

16. **Several AST-level divergences above do not change final search
    results for this project's fixtures (a finding, not a new divergence
    of its own).** Entries 2 (wildcard case-folding order), the
    `tag:'foo,bar'` comma-quote-literal design entry, and entry 12 (date-range
    tz bypass) are all real at the *parsed-AST* level (what
    `tests/differential` compares) but were found, while building
    `tests/emitter/test_acceptance_e2e.py`, to **not** change the final
    doc-id set either backend's search actually returns for the queries in
    this project's fixture:

    - Entry 2: real whoosh's own `field.process_text(text, tokenize=False)`
      still runs the field's LowercaseFilter over an un-tokenized
      wildcard/prefix pattern (filters aren't skipped by `tokenize=False`,
      only the tokenizer step is — see
      `whoosh.analysis.tokenizers.RegexTokenizer.__call__`), so a fielded
      `Entwä*` query against real whoosh also ends up matching the
      lowercased pattern `entwä`, same as whoosh-compat's explicit
      `pattern_normalizer` — both sides match doc 3.
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
    pipelines, not by inspection). This does **not**
    mean entries 2/12/the comma-quote entry are wrong or should be
    removed — they are still real, reproducible AST-level divergences that
    a different fixture (e.g. dates near a local-midnight boundary) could
    absolutely turn into a result-level divergence too; it just means none
    of *this* project's specific test data happens to expose that.
