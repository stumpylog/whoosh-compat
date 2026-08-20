# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Intentional behavioral differences from real Whoosh are not changelog
material; they are permanently documented, each with its rationale, in
[DIVERGENCES.md](./DIVERGENCES.md).

## [0.2.0] - 2026-08-19

### Breaking

- `QueryEmitError` and `UnsupportedQueryError` are replaced by a single
  `QueryError`, which always carries the `Diagnostic` describing why the
  query could not be emitted (`err.diagnostic`).
- `Diagnostic` is now a keyword-only dataclass, and gains `cause` (a
  `Cause` classifying who can act on the diagnostic: `INVALID_INPUT`,
  `UNSUPPORTED`, `MISCONFIGURED`, or `INTERNAL`), `field_kind`, and
  `divergence` (the `DIVERGENCES.md` entry number, when one applies).
- `DiagnosticKind.UNSUPPORTED_PATTERN` is split into `PATTERN_ON_NUMERIC`,
  `PATTERN_ON_BOOLEAN_EXISTS`, and `PATTERN_ON_SUBPATH`, one per field kind
  a wildcard/prefix pattern cannot be honored against.
- `DiagnosticKind.SCHEMA_FIELD_MISSING` (`Cause.MISCONFIGURED`) is split out
  of `BACKEND_REJECTED`. A query on a field this library's registry knows
  but the tantivy schema does not now reports the mismatch as the
  operator's problem rather than as an internal error. This is the drift a
  host gets when its field table and its schema builder fall out of step,
  so it is a deployment fault, not a library one.
  `BACKEND_REJECTED` keeps `Cause.INTERNAL` and its original meaning:
  tantivy-py refusing a query this emitter built. The split was necessary
  because `cause_for()` is keyed only on `kind`, so one kind cannot carry
  different causes at different raise sites. A host branching on `kind`
  should route the new member; a host branching on `cause` alone sees this
  condition move from `INTERNAL` to `MISCONFIGURED`. Every leaf that
  queries a resolved field reports it uniformly (term, phrase, prefix,
  wildcard, numeric and date range, bare-`*` existence, BOOLEAN_EXISTS, and
  JSON subpaths on both the direct and `parse_query` routes), because drift
  is a property of the field rather than of the spelling that reaches it.
  Only the confirmed missing-field condition is reclassified: any other
  `ValueError` from tantivy-py still reaches `emit`'s backstop as
  `BACKEND_REJECTED`, so a genuine library defect is not hidden behind a
  400.
- `Cause.MISCONFIGURED` is documented as requiring **both** an operator
  alert and an HTTP 400, not an alert alone. Every `MISCONFIGURED` kind is
  reachable from ordinary query text, so a request is always waiting on an
  answer the alert does not give it. No behavior changed, but the previous
  documentation named no status code at all, which left hosts unable to
  route it.

### Fixed (contract)

- A bare `field:*` existence query (and a `BOOLEAN_EXISTS` term resolving
  to a fast target) on a field the registry knows but the index schema does
  not used to build successfully and then raise a bare `ValueError` out of
  the *searcher*, escaping `emit()`'s documented "returns a query or raises
  `QueryError`" contract entirely. tantivy's `exists_query` takes no schema
  and so validates nothing at build time; the schema is now probed up front
  on that path and the drift reported as `SCHEMA_FIELD_MISSING`.

### Added

- The six multi-word date keywords (`previous week`, `previous month`,
  `previous quarter`, `previous year`, `this month`, `this year`) parse
  without quotes on a date field: `added:previous month` now resolves
  exactly like `added:"previous month"`. In whoosh a value ends at the first
  space, so the unquoted spelling used to be a failed date on `previous`
  plus a stray default-field term `month`.
  This exists so a host that wants the unquoted spelling to work no longer
  has to insert the quotes into the raw query string before parsing: such a
  rewrite cannot see quotes, so it corrupts `title:"see added:previous month
  notes"`, where those characters are ordinary phrase text.
  The widening is limited to those six phrases on an explicitly named date
  field, plus a time of day *trailing* one of them, so that an unquoted
  spelling reaches the grammar as the same value the quoted spelling would.
  What the grammar does with that value has two outcomes, not one: the two
  span-valued keywords reject it, so `added:previous week 3pm` is the same
  `BAD_DATE` as `added:"previous week 3pm"` (`DIVERGENCES.md` entry 52),
  where before it was a `BAD_DATE` only by accident of `previous` not being
  a date on its own; the other four are calendar units and accept it,
  narrowing the range to that time of day on the period's first and last
  day, so `added:previous month noon` is
  `2026-07-01T12:00Z .. 2026-07-31T12:00:00.000001Z` rather than the whole
  month plus a free-text `noon` term. A *leading* time is not joined: `added:3pm previous week` stays an
  instant plus two free-text terms, because `added:` binds `3pm` and stops,
  so the phrase and the time were never one value to reject. Nothing else
  about a date value becomes whitespace-greedy:
  `added:previous week AND title:foo`, `added:previous week invoice`, and
  `title:previous month` (a TEXT field) are unchanged (`DIVERGENCES.md`
  entry 19).
- A JSON field can declare one of its subpaths the default:
  `FieldSpec("notes", FieldKind.JSON, subpaths={"user": SubpathSpec(),
  "note": SubpathSpec(default=True)})`. A bare mention of the field then
  resolves to that subpath (`make_ref("notes")` returns
  `FieldRef("notes", "note")`, and `is_bare_json_field("notes")` is
  `False`), while an explicitly typed subpath still wins. `SubpathSpec`
  gains a `default: bool = False` field, and `FieldSpec.default_subpath`
  reports the declared default or `None`. Declaring more than one per spec
  is a `ValueError` at registry construction.
  This exists so a host that wants `notes:` to mean `notes.note:` no longer
  has to rewrite the raw query string before parsing: such a rewrite cannot
  see quotes, so it silently corrupts `content:"payment notes: none"`,
  where those characters are ordinary text and not a field prefix. Opt-in
  per field: with no default declared, a bare JSON field name stays
  unresolvable and demotes to text, exactly as before.
  Adopting a default changes what the bare name does in three ways, all of
  them matching what the host-side rewrite it replaces already produced.
  `notes:*` becomes an existence check on the default subpath's own column
  rather than on the whole field: result-changing on a *fast* JSON field,
  while on a non-fast one it is the same `EXISTS_REQUIRES_FAST` /
  `Cause.MISCONFIGURED` refusal as before, naming `'notes.note'` instead of
  `'notes'`. A wildcard or prefix on the bare name (`notes:fo*`) becomes a
  parse-time `PATTERN_ON_SUBPATH` diagnostic, and a range (`notes:[a TO b]`)
  an emit-time `TEXT_RANGE` `QueryError`, where both previously demoted to a
  silent default-field text search: honest refusals replacing wrong
  searches, but hard errors where there were none (`DIVERGENCES.md`
  entries 20 and 30).
- `parse()` now guarantees the exception type it can raise: the parse
  pipeline is wrapped in a backstop that converts any unexpected exception
  into `QueryParserError`, chaining the original as `__cause__`. It never
  means the query was bad (that is still a `Diagnostic`); it means a defect
  in this library, so a host routes it to a monitorable 500 rather than
  seeing a bare `RecursionError` from a pathologically nested query.
  Configuration mistakes passed to `parse()` still raise `ValueError`
  eagerly, unchanged.
- `free_text_tokens(..., analyzed=False)` returns the raw text each
  contributing leaf was parsed from instead of the field analyzer's output.
  Use it whenever the tokens go back into a parser that analyzes them
  again: analysis is not generally idempotent (a stemmer maps
  `universities` to `univers` and `univers` to `univ`), so re-analyzing
  analyzed output searches a stem the index does not contain. The mode
  never consults the analyzer. Which *nodes* contribute is unchanged
  (negation, patterns, kinds and dedupe are all structural), but the text
  differs in three ways: an all-stopword term still contributes its raw text
  (`the` -> `('the',)`, versus `()` analyzed), a phrase contributes one entry
  rather than one per word (`"tax reports"` -> `('tax reports',)`), and a
  term the analyzer would split contributes one entry (`alpha-beta` ->
  `('alpha-beta',)`). So an entry can contain whitespace and punctuation,
  including characters a re-parse would read as grammar (a colon, a bracket)
  even though the query grammar around them is gone: quote or escape before
  re-parsing. The default is `analyzed=True`, so existing callers are
  unaffected.

### Fixed

- `free_text_tokens()` returned a term the user had **excluded** when the
  other side of the exclusion analyzed to nothing: `the ANDNOT secret`
  yielded `('secret',)` with `the` a stopword, contradicting the function's
  own first documented rule. It walked the tree *after* `analyze()`, and
  `analyze()`'s zero-token drop is deliberately blind to which operand
  dropped (`DIVERGENCES.md` entry 23), so the `AndNot` had already collapsed
  to its negative side standing alone as an ordinary positive node. It now
  walks the normalized-but-unanalyzed tree and analyzes each contributing
  leaf on its own, so polarity comes from the query as written. A host
  building a secondary matching clause from these tokens was showing
  documents the user had asked to exclude. The guarantee is about the tree
  as parsed: `node` now documents a precondition, since a caller who runs
  `analyze()` itself before calling hands over a tree the collapse has
  already happened in.
- `FieldSpec(subpaths=...)` rejects a sequence that is neither a tuple nor a
  mapping instead of passing it to `dict()`. A list of names was read as a
  sequence of key/value pairs, so `subpaths=['ab', 'cd']` silently became
  `{'a': 'b', 'c': 'd'}`: the registry accepted it, the real subpaths were
  permanently unaddressable, and every query against one degraded to
  default-field noise with no error anywhere. Other name lengths raised, but
  in `dict()`'s own vocabulary ("dictionary update sequence element #0 has
  length 3; 2 is required"), naming nothing the caller wrote. Subpath
  *values* are now checked to be `SubpathSpec` too, which matters now that
  the type carries a flag.
- A flat, paren-free chain of non-merging operators (`ANDNOT`, `ANDMAYBE`,
  `REQUIRE`) builds one hierarchy level per operator, which the
  parenthesization cap could not see, so a long chain raised
  `RecursionError` out of `parse()`. The depth cap now covers
  operator-built nesting too, and reports `DiagnosticKind.TOO_DEEP` instead.
- `added:"previous week 3pm"` raised `AttributeError` out of `parse()`
  (a period keyword denotes a span, so a time-of-day on it names nothing,
  and the merging pass got a timespan where it expected a datetime). Both
  word orders now report `DiagnosticKind.BAD_DATE`; previously the reversed
  order silently dropped the time and returned the whole week instead.
- `added:"noon to now"`, a range with a bare time-of-day lower bound and a
  concrete upper bound, is resolved instead of crashing (whoosh itself
  crashes on it; `DIVERGENCES.md` entry 51).
- A wildcard's bracket character class is now case-folded by the field's
  `pattern_normalizer` like the rest of the pattern. `title:BILL[I]NG*`
  compiled to the regex `bill[I]ng.*` and so silently matched nothing while
  `title:BILLING*` matched, breaking the promise in `DIVERGENCES.md` entry
  2. The fold is applied one character at a time, and a character whose
  normalized form is longer than one character (`ascii_fold` maps `ß` ->
  `ss`) is left as typed: a class matches a single character, so expanding
  one inside a class, above all as a range endpoint (`[ss-z]`), would build
  a different query rather than a folded one. Entry 2 records that
  qualification.

### Internal

- The trailing-star-to-`Prefix` fold, which whoosh performs at two
  independent sites, is implemented once as
  `parser.plugins.folds_to_prefix`. Both copies carried the `"["` exclusion
  that fixes paperless-ngx#13568 (`DIVERGENCES.md` entry 13), and sharing
  one implementation is what keeps that true.

## [0.1.0] - 2026-08-18

Initial release.

### Added

- Whoosh query-language parser, forked from Whoosh's own `qparser` so the
  grammar is inherited rather than reimplemented, producing a
  backend-neutral, immutable AST (`whoosh_compat.ast`). Parsing never
  raises for bad query input: malformed dates and numbers become
  structured `Diagnostic`s plus `ErrorLeaf` nodes.
- Field registry (`FieldSpec`/`FieldRegistry`) describing the host schema:
  TEXT, KEYWORD, U64, DATE, DATETIME, JSON (with registered subpaths) and
  BOOLEAN_EXISTS kinds, plus aliases, comma-value lists, per-field
  analyzers and pattern normalizers, and fast-field existence strategies.
- Tantivy emitter (`whoosh_compat.emitters.tantivy_.emit`) constructing
  `tantivy.Query` objects programmatically, never through tantivy's string
  query parser, with documented workarounds for current tantivy-py
  limitations (naive-UTC range bounds, all-`MustNot` boolean padding, the
  JSON-subpath `parse_query` fallback, and clamping to tantivy's
  representable date window).
- Full Whoosh date grammar: ISO and compact forms, natural-language
  keywords (`today`, quoted `"previous month"` and friends), relative
  offsets (`now-7d`, `-1 week`), bracketed ranges with joint
  disambiguation of ambiguous bounds, and an RFC3339 extension accepting
  `T` separators and the `Z` UTC designator.
- `whoosh_compat.free_text_tokens()`: the analyzed free-text word tokens
  of a parsed query, for hosts building secondary text clauses (fuzzy
  blends) that must never receive query grammar.
- Documentation of every intentional divergence from real Whoosh
  (50 numbered entries in `DIVERGENCES.md` at release), enforced by a
  strict-xfail allowlist in the test suite.
- Three-layer test suite: unit tests, differential comparison against a
  pinned real-Whoosh oracle over a corpus of real-world query strings,
  and dual-index end-to-end acceptance tests comparing matched-document
  sets between a real Whoosh index and tantivy, plus grammar-aware
  Hypothesis fuzzing over the whole supported syntax.
