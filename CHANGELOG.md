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
  of `BACKEND_REJECTED`. A wildcard or prefix query on a field this
  library's registry knows but the tantivy schema does not now reports the
  mismatch as the operator's problem rather than as an internal error.
  `BACKEND_REJECTED` keeps `Cause.INTERNAL` and its original meaning:
  tantivy-py refusing a query this emitter built, which is a defect in this
  library, not a deployment fault. The split was necessary because
  `cause_for()` is keyed only on `kind`, so one kind cannot carry different
  causes at different raise sites. A host branching on `kind` should route
  the new member; a host branching on `cause` alone sees this condition
  move from `INTERNAL` to `MISCONFIGURED`. Note the same drift reached
  through a plain (non-pattern) term still reports `BACKEND_REJECTED`,
  because `emit`'s backstop sees only an exception type and has no field to
  probe.
- `Cause.MISCONFIGURED` is documented as requiring **both** an operator
  alert and an HTTP 400, not an alert alone. Every `MISCONFIGURED` kind is
  reachable from ordinary query text, so a request is always waiting on an
  answer the alert does not give it. No behavior changed, but the previous
  documentation named no status code at all, which left hosts unable to
  route it.

### Added

- `parse()` now guarantees the exception type it can raise: the parse
  pipeline is wrapped in a backstop that converts any unexpected exception
  into `QueryParserError`, chaining the original as `__cause__`. It never
  means the query was bad (that is still a `Diagnostic`); it means a defect
  in this library, so a host routes it to a monitorable 500 rather than
  seeing a bare `RecursionError` from a pathologically nested query.
  Configuration mistakes passed to `parse()` still raise `ValueError`
  eagerly, unchanged.

### Fixed

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
