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
