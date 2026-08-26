# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Intentional behavioral differences from real Whoosh are not changelog material; they are permanently documented, each with its rationale, in [DIVERGENCES.md](./DIVERGENCES.md).

## [0.2.0]

### Changed

- **Behavior break:** an unquoted multi-word date value (`created:december 2019`, `created:2020 to 2021`) is now rejected with a `BAD_DATE` diagnostic naming the whole value, instead of silently truncating to its first token and reinterpreting the remainder as free-text search terms. The truncating behavior returned wrong documents with no error at all: `created:december 2019` matched December of the current year. Quoted and bracketed spellings (`created:"december 2019"`, `created:[2020 TO 2021]`) were always correct and are unchanged, and remain the way to write these values. Hosts need no code change, since the diagnostic reuses the existing `BAD_DATE` kind. An unquoted multi-word date keyword (`created:previous month`) still parses on its own, but is affected once more words follow it: `created:previous month to now` is rejected the same way, and `created:"previous month to now"` is the spelling that works. See DIVERGENCES.md entry 61.

### Migrating stored queries from 0.1.0

Only the shapes described above are affected: a date-fielded value written
without quotes that continues past the part the date grammar resolves. A
host holding stored queries (saved views, bookmarks, scheduled searches)
can rewrite them mechanically rather than surfacing the break to users.

Every affected diagnostic spans exactly the offending value on
`startchar`/`endchar`, so the rewrite is an insert of two quote characters.
Apply the diagnostics of one query in descending `startchar` order, so that
rewriting one value does not shift the spans of the ones before it:

```python
result = whoosh_compat.parse(q, registry=..., default_fields=..., tz=..., basedate=...)
out = q
for d in sorted(result.diagnostics, key=lambda d: -d.startchar):
    if d.kind is DiagnosticKind.BAD_DATE and d.startchar is not None:
        out = f'{out[:d.startchar]}"{out[d.startchar:d.endchar]}"{out[d.endchar:]}'
```

**Re-parse the result and keep it only if it comes back with no
diagnostics.** The rewrite is not safe to apply blindly, because
`BAD_DATE` also covers values that no spelling can fix. `created:last week`
becomes `created:"last" week`, which is still `BAD_DATE`, since whoosh's
date grammar has no `last` keyword and the diagnostic spans only the word
`last`. A stored query that does not come back clean needs a human, not a
rewrite. There is currently no stable field that tells the two cases apart
ahead of time (see issue #68); re-parsing is the reliable check.

Verified rewrites, each parsing clean afterwards:

| stored query | rewritten |
| --- | --- |
| `created:december 2019` | `created:"december 2019"` |
| `created:2020 to 2021` | `created:"2020 to 2021"` |
| `created:previous month to now` | `created:"previous month to now"` |
| `created:december 2019 AND title:invoice` | `created:"december 2019" AND title:invoice` |
| `created:december 2019 OR added:2020 august 4` | `created:"december 2019" OR added:"2020 august 4"` |

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
