# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Intentional behavioral differences from real Whoosh are not changelog
material; they are permanently documented, each with its rationale, in
[DIVERGENCES.md](./DIVERGENCES.md).

## [0.1.0] - 2026-08-25

First release.

### Added

- Whoosh query parser (forked from Whoosh's own `qparser`) producing a
  backend-neutral, immutable AST. Parsing never raises for bad query input:
  malformed dates and numbers become structured `Diagnostic`s instead.
- Field registry (`FieldSpec`/`FieldRegistry`) describing the host schema:
  TEXT, KEYWORD, U64, DATE, DATETIME, JSON (with subpaths, including a
  declared default subpath), and BOOLEAN_EXISTS kinds, plus aliases,
  per-field analyzers, pattern normalizers, and fast-field existence
  strategies.
- Tantivy emitter (`whoosh_compat.emitters.tantivy_.emit`) building
  `tantivy.Query` objects programmatically, never through tantivy's string
  parser.
- Full Whoosh date grammar: ISO and compact forms, natural-language
  keywords, relative offsets, bracketed ranges, and an RFC3339 extension.
- `QueryError`/`Diagnostic` error model, with a `Cause` (`INVALID_INPUT`,
  `UNSUPPORTED`, `MISCONFIGURED`, `INTERNAL`) on every diagnostic so a host
  can route it to the right HTTP status without inspecting its kind.
  `PARSE_KINDS`/`EMIT_KINDS` let a host tell parse-time from emit-time
  diagnostics without enumerating members itself.
- `free_text_tokens()`: the free-text word tokens of a parsed query, for
  hosts building secondary text clauses (e.g. fuzzy blends) that must never
  receive query grammar. An `analyzed=False` mode returns raw text instead
  of analyzer output, for callers who will re-parse it.
- 60 documented intentional divergences from real Whoosh (`DIVERGENCES.md`),
  enforced by a strict-xfail allowlist in the test suite.
- Three-layer test suite: unit tests, differential comparison against a
  pinned real-Whoosh oracle, dual-index end-to-end acceptance tests, and
  grammar-aware Hypothesis fuzzing.
