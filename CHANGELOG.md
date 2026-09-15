# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Intentional behavioral differences from real Whoosh are not changelog material; they are permanently documented, each with its rationale, in [DIVERGENCES.md](./DIVERGENCES.md).

## [0.3.0]

### Added

- `ast.Fuzzy`: an edit-distance (typo-tolerant) term leaf for hosts building their own companion clauses around a parsed query, for example `Or(Term(...), Fuzzy(...))`. It is emit-only: `parse()` never produces it and there is still no `term~2` query syntax, so a host builds the node itself, either in a tree it passes to `emit()` or around a parsed leaf with the `rewrite_leaf` hook below. It requires an explicit field, supports only TEXT and KEYWORD fields, and matches `text` through the field's `pattern_normalizer` rather than its analyzer. `distance` must be an integer 0, 1 or 2; anything else fails at `emit()` time with a `QueryError`. Empty or whitespace-only `text` matches nothing rather than failing. See README's "Hand-building a `Fuzzy` node for a caller-side companion clause". Additive: nothing `parse()` produces changes.
- `analyze(..., rewrite_leaf=...)`: a hook for hosts that replace `Term`/`Phrase` leaves before emitting, typically wrapping one as `Or(leaf, companion)` to search a companion field or add a `Fuzzy` clause alongside it. It runs inside analysis itself, so the original leaf placed in its replacement keeps the multi-token combination its enclosing group gives it, and a replacement that ends up empty drops out of its group the way a stopword does. The result is fully analyzed and goes to `emit()` as usual. An exception from the hook propagates unchanged, and a return value that is not an `ast.Node` raises `TypeError`. See README's "Rewriting leaves before emit". Additive: `rewrite_leaf=None`, the default, is unchanged behavior.

### Changed

- **Behavior break:** a date value that pairs a time of day with a whole month, year, week or quarter is now rejected with a `BAD_DATE` diagnostic, in every spelling: `added:"this month 15:00"`, `added:previous month 3pm`, `added:3pm previous week`, `added:"august 2026 15:00"`, `added:'2026 23:59'`, and the same values as bracket bounds or `to` range ends, on DATE and DATETIME fields alike. Such a value used to resolve silently to a range pinned to that time on the period's first and last day, which is neither the whole period nor that time on every day; on a DATE field the time was dropped and the whole period searched. The diagnostic names the period and carries no `suggestion`, because quoting does not repair it: a stored query with this shape needs a person to name a day or drop the time, so README's stored-query sweep will list it among the values no quoting can fix. A time on a specific day (`added:"yesterday 15:00"`, `added:"2020-03-15 10:00"`) is unchanged. See DIVERGENCES.md entry 62.
- **Behavior break:** in a numeric date value a colon now separates clock units only, and a day and an hour written fused together are read that way only when the whole date is fused. `added:"2026-08 15:00"` used to be 15 August at 00:00 (the clock time read as a day and an hour) and is now the time-of-day-on-a-month rejection above, as is the space-separated `added:'2026 08 15:00'`, quoted or not. `added:'2026-08-10:15:00'`, `added:'2026:08:10'`, `added:'2026-08 1500'`, `added:'2026 1230'` and `added:'2020 12:30'` (formerly 30 December) are now `BAD_DATE`. Once a space has separated two units, the hour must follow a space or a `T`, and a dotted day cannot follow a spaced year, so a clock time written with a dot is not read as a day and an hour either: `added:'2026-08 15.00'` (formerly 15 August at 00:00) and `added:'2026 12.30'` (formerly 30 December) are `BAD_DATE`, and so are mixed spellings that did name a day and an hour, such as `added:'2026-08 10-15'` and `added:'2026 08.10 15:00'` (formerly 10 August at 15:00). Unquoted, a remainder the date grammar cannot read stays a search term, as any other word would: `added:2026-08 1500` is all of August 2026 AND the term `1500`, and `added:2026-08 15.00` all of August AND `15.00`, where each used to be rejected only because the misread value parsed. Fully fused values (`202608101500`), separated dates with a fused or dotted clock (`2026-08-10 1500`, `2026-08-10 15.00`) and values with no space before the hour (`2026-08-10-15`, `2026.08.15.10.30`) are unchanged. See DIVERGENCES.md entry 63.

### Fixed

- An emit-time diagnostic raised for a leaf now carries that leaf's `startchar`/`endchar` in every case. `AST_UNFIELDED_TERM` and `AST_UNKNOWN_FIELD` on any leaf, `AST_PATTERN_ON_KIND` and `AST_JSON_NEEDS_SUBPATH` on a `Prefix` or `Wildcard`, and `AST_KIND_NOT_IMPLEMENTED` on a `Fuzzy` used to leave both `None`, unlike the other emit-time failures on the same leaves.

## [0.2.0]

### Added

- `Diagnostic.suggestion`: the replacement text for a diagnostic's own `startchar`/`endchar` span, for the cases where a single concrete rewrite of the query text would work. A host applies it as `q[:startchar] + suggestion + q[endchar:]` instead of re-deriving the rule. It is `None` wherever no such rewrite exists, which is most of the time: a malformed date or number has no working spelling, a pattern on a numeric or boolean-exists field cannot be written any other way, and a shape with two fixes that mean different things (`title:200[1-9]`, which can be quoted as literal text or extended with a wildcard) deliberately suggests nothing rather than choosing semantics for the user. Branch on `suggestion is not None`, never on `kind`: the same `kind` carries a suggestion for one query and not another, since `BAD_DATE` covers both `created:december 2019` (quotable) and `created:20231340` (not). The unquoted multi-word date value is the only case that carries one today. Additive: existing fields and every `kind` branch are unchanged.

### Changed

- **Behavior break:** an unquoted multi-word date value (`created:december 2019`, `created:2020 to 2021`) is now rejected with a `BAD_DATE` diagnostic naming the whole value, instead of silently truncating to its first token and reinterpreting the remainder as free-text search terms. The truncating behavior returned wrong documents with no error at all: `created:december 2019` matched December of the current year. Quoted and bracketed spellings (`created:"december 2019"`, `created:[2020 TO 2021]`) were always correct and are unchanged, and remain the way to write these values. Hosts need no code change, since the diagnostic reuses the existing `BAD_DATE` kind. An unquoted multi-word date keyword (`created:previous month`) still parses on its own, but is affected once more words follow it: `created:previous month to now` is rejected the same way, and `created:"previous month to now"` is the spelling that works. See DIVERGENCES.md entry 61.

### Migrating stored queries

Hosts upgrading from 0.1.0, and hosts adopting this library from real
Whoosh, both carry stored queries (saved views, bookmarks, scheduled
searches) that may contain the affected shapes. Under real Whoosh those
queries returned wrong documents silently rather than failing, so an
affected stored query can be of any age and its author was never told.

README's "Adopting the library: sweep stored queries first" gives the
sweep: parse every stored query, rewrite the mechanically fixable ones by
quoting at the span the diagnostic carries, and re-parse to confirm, since
`BAD_DATE` also covers values no quoting can fix.

## [0.1.0] - 2026-08-25

First release.

### Added

- Whoosh query parser (forked from Whoosh's own `qparser`) producing a backend-neutral, immutable AST. Parsing never raises for bad query input: malformed dates and numbers become structured `Diagnostic`s instead.
- Field registry (`FieldSpec`/`FieldRegistry`) describing the host schema: TEXT, KEYWORD, U64, DATE, DATETIME, JSON (with subpaths, including a declared default subpath), and BOOLEAN_EXISTS kinds, plus aliases, per-field analyzers, pattern normalizers, and fast-field existence strategies.
- Tantivy emitter (`whoosh_compat.emitters.tantivy_.emit`) building `tantivy.Query` objects programmatically, never through tantivy's string parser.
- Full Whoosh date grammar: ISO and compact forms, natural-language keywords, relative offsets, bracketed ranges, and an RFC3339 extension.
- `QueryError`/`Diagnostic` error model, with a `Cause` (`INVALID_INPUT`, `UNSUPPORTED`, `MISCONFIGURED`, `INTERNAL`) on every diagnostic so a host can route it to the right HTTP status without inspecting its kind. `PARSE_KINDS`/`EMIT_KINDS` let a host tell parse-time from emit-time diagnostics without enumerating members itself.
- `free_text_tokens()`: the free-text word tokens of a parsed query, for hosts building secondary text clauses (e.g. fuzzy blends) that must never receive query grammar. An `analyzed=False` mode returns raw text instead of analyzer output, for callers who will re-parse it.
- 60 documented intentional divergences from real Whoosh (`DIVERGENCES.md`), enforced by a strict-xfail allowlist in the test suite.
- Three-layer test suite: unit tests, differential comparison against a pinned real-Whoosh oracle, dual-index end-to-end acceptance tests, and grammar-aware Hypothesis fuzzing.
