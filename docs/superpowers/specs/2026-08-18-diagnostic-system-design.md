# Structured diagnostics across parse and emit

Date: 2026-08-18
Status: revised after adversarial review; approved for planning

## Problem

`whoosh-compat` reports query failures through two unrelated shapes.

Parse failures are structured values: `parse()` never raises for bad query
input, it accumulates `Diagnostic` records into `ParseResult.diagnostics`,
each carrying a machine-stable `kind`, the offending `FieldRef`, the raw
value, and an exact source span.

Emit failures are bare exceptions. `UnsupportedQueryError` carries no
structured payload at all, and `QueryEmitError.diagnostic` is optional and
populated at exactly 1 of its 12 raise sites (`emitters/tantivy_.py:553`).

The README tells hosts that both mean the same thing (an HTTP 400) and that
both must be caught. So a host gets an enum for a malformed date and a
string for a text-field range, for outcomes it will render identically.

Three specific consequences:

1. **The `kind` enum is coarser than the messages it carries.**
   `parser/default.py:557-567` selects between three distinct causes for
   `DiagnosticKind.UNSUPPORTED_PATTERN` (numeric field, JSON subpath,
   boolean-exists) and collapses all three onto one member. A host that
   wants to distinguish them must either match on prose or re-resolve
   `Diagnostic.field` through the registry to recover something the library
   already knew and discarded.

2. **Emit-time failures carry no position.** Every AST node carries
   `startchar`/`endchar` (`ast.py:32-33`), preserved through `normalize()`,
   but no emit-time raise reads them.

3. **Library documentation is encoded as English inside messages.** Three
   user-reachable messages embed `DIVERGENCES.md` references
   (`emitters/tantivy_.py:755`, `:760`, `:773`), and one embeds registry
   configuration advice (`:534-537`). The README instructs hosts to strip
   and rewrite these, which in practice means a regex over exception text
   the library explicitly refuses to keep stable.

The class selection between the two exception types is also arbitrary:
`:763` raises `QueryEmitError` for a JSON field needing a subpath while
`:753`, three lines away in the same helper, raises `UnsupportedQueryError`
for a pattern on a subpath. No principle separates them.

The parse/emit distinction itself is sound and is preserved. What is wrong
is that the two phases use different *payload types* for the same event.

## Measured reachability

An earlier draft of this spec assigned emit-time behavior to conditions
that cannot occur. The kind table below is now grounded in a probe that
ran every candidate spelling through `parse()` then `emit()`. Results:

| Query | Outcome |
|---|---|
| `notes:*`, `notes.user:*` | **emit**: exists-requires-fast |
| `slow_num:[TO]`, `slow_date:[TO]`, `slow_num:{TO}` | **emit**: exists-requires-fast |
| `title:[a TO b]`, `content:[TO]`, `notes:[TO]` | **emit**: text range |
| `nosuch:foo`, `notes.bogus:foo`, `notes:foo`, `notes:"a b"`, `notes:fo*` | no error: absorbed into the default field as free text |
| `asn:notanumber`, `asn:"not a number"`, `asn:[abc TO def]` | parse: `BAD_NUMBER` |
| `created:notadate`, `created:[zzz TO yyy]` | parse: `BAD_DATE` |
| `asn:1*`, `notes.user:fo*`, `has_tag:t*` | parse: `UNSUPPORTED_PATTERN` |

`EXISTS_REQUIRES_FAST` is reachable through **two** distinct node shapes,
not one. Besides `field:*` (an `Every` node), a double-open range delegates
to the same helper: `_range_query` (`:797-799`) returns
`self._exists_query(resolved)` when both bounds are `None`, because a range
with no bounds means "this field has some value". So `slow_num:[TO]` on any
non-fast field hits it, on U64 and DATE as well as JSON, in both the
inclusive `[TO]` and exclusive `{TO}` spellings. A first pass at this table
missed that by enumerating value spellings while holding the node type
fixed, which is the sibling-cell failure `CLAUDE.md` warns about.

Note also that the message at `:534-537` hardcodes the `field:*` spelling
("mark it fast=True to support `'slow_num:*'`") even when the user wrote
`slow_num:[TO]`. It names a query the user never typed. That is a further
argument for moving the advice out of prose and into `cause`.

**Only two emit-time conditions are reachable from query text.** Every
other emit raise site is a backstop for a hand-built AST that bypassed the
parser, which the code already documents as such (`:710-712`: "Backstop for
a hand-built `Prefix`/`Wildcard` node"; `:748-750`: "Reachable only from a
hand-built node"; `:658-660`: "a hand-built `ast.Phrase` bypasses the
parser entirely"). `fields.py:575-579` deliberately makes bare JSON field
names unresolvable so they cannot reach emit at all.

This is the single most important input to the design, and it means the
emit-side surface is far smaller than it appears from counting raise sites.

## Goals

- One structured payload type for every query failure, whatever the phase.
- One exception type for hosts to catch, with a payload that is always
  present.
- Distinctions currently readable only in prose become fields: which field
  kind, which divergence, whose fault.
- Emit-time failures carry source spans where a node is in scope.
- `Diagnostic.message` becomes purely a developer/log string with no
  semantic load.
- Backstop conditions are labelled honestly as library/caller defects
  rather than dressed up as query errors.

## Non-goals

- No change to query semantics. Nothing about which documents a query
  matches changes, so this introduces no divergence from whoosh and needs
  no allowlist entry, `DIVERGENCES.md` divergence entry, or corpus line.
  Verified: `tests/differential/test_differential.py:88-91` gates on
  `if diagnostics:` truthiness only and never inspects `kind`;
  `DiagnosticKind` appears nowhere under `tests/differential/`;
  `allowlist.py` keys on regexes over the raw query string. Splitting
  `UNSUPPORTED_PATTERN` cannot move
  `test_diagnostic_skip_count_matches_corpus`'s pinned count of 23.
- No change to `parse()`'s accumulate-everything behavior.
- No change to `QueryParserError`, which signals a parser-pipeline
  invariant violation.
- No backward-compatibility shims. paperless-ngx's integration is not yet
  complete, so old names are removed rather than deprecated.
- `free_text_tokens` is untouched: `ast.py:831-955` never imports, checks
  or constructs `ErrorLeaf`/`Diagnostic`.

## Design

### Diagnostic

```python
class Cause(Enum):
    INVALID_INPUT = auto()   # the query text is wrong; the user must change it
    UNSUPPORTED = auto()     # the query is well-formed; this backend cannot run it
    MISCONFIGURED = auto()   # the registry/schema, not the query, is the obstacle
    INTERNAL = auto()        # a defect in this library or in a caller-built AST


@dataclass(frozen=True, kw_only=True, slots=True)
class Diagnostic:
    kind: DiagnosticKind
    cause: Cause
    message: str
    startchar: int | None = None
    endchar: int | None = None
    field: FieldRef | None = None
    field_kind: FieldKind | None = None
    raw_value: str | None = None
    divergence: int | None = None
```

There is deliberately **no `phase` field.** An earlier draft carried one,
justified by emit re-raising a parse-origin diagnostic at `:552-555`. That
justification is unnecessary: emit never *originates* an `INVALID_INPUT`
diagnostic, so a `QueryError` whose cause is `INVALID_INPUT` is by
construction one that wrapped a parse-time diagnostic. `Cause` subsumes the
distinction at no cost. Otherwise `phase` would be fully redundant with the
channel a host received the record through (`ParseResult.diagnostics` or
`QueryError.diagnostic`).

`kw_only=True` because the field list has grown once and will again.

Severity remains fatal-only, permanently, as documented today: a
`Diagnostic` always means the query it concerns cannot be emitted. `Cause`
is not a severity tier. Every cause is fatal to the query; they differ in
who can act on it, not in how bad it is.

`Cause.MISCONFIGURED` covers one measured condition, `field:*` on a
non-fast field (`:534-537`). It earns a member because the correct host
response differs in kind: this is the operator's registry declaration, not
the user's query, so a host may reasonably alert rather than return a 400.
That is invisible in the current prose.

`Cause.INTERNAL` covers the backstops, and is what makes the reachability
finding actionable: a host can distinguish "your query is unrunnable" from
"someone handed the emitter a malformed AST" without reading prose.

`divergence` is `int | None`. No condition maps to more than one entry.

### Exceptions

```
WhooshCompatError                 # base, unchanged
├── QueryError(diagnostic)        # NEW: replaces both emit-time types
└── QueryParserError              # unchanged: parser-pipeline invariant
```

```python
class QueryError(WhooshCompatError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
```

`QueryEmitError` and `UnsupportedQueryError` are deleted.

The invalid-vs-unsupported distinction those classes encoded moves to
`Diagnostic.cause`, which is strictly more expressive: the two classes
cannot represent `MISCONFIGURED`, the one distinction most worth surfacing.

`QueryParserError` stays outside this scheme and is not merged into
`Cause.INTERNAL`. It fires during the tagger/filter pipeline, before an AST
exists; `INTERNAL` describes a `Diagnostic` about an AST that does exist.

### Kind inventory

Rows are grouped by whether query text can reach them. `UNSUPPORTED_PATTERN`
splits three ways; each of the three retains a divergence entry.

**Parse-time, reachable:**

| Kind | Cause | Divergence |
|---|---|---|
| `BAD_DATE` | `INVALID_INPUT` | |
| `BAD_NUMBER` | `INVALID_INPUT` | |
| `TOO_DEEP` | `INVALID_INPUT` | |
| `PATTERN_ON_NUMERIC` | `UNSUPPORTED` | 29 |
| `PATTERN_ON_BOOLEAN_EXISTS` | `UNSUPPORTED` | 29 |
| `PATTERN_ON_SUBPATH` | `UNSUPPORTED` | 30 |

`DIVERGENCES.md:964-967` covers the numeric *and* BOOLEAN_EXISTS cases
under entry 29 ("A wildcard/prefix pattern on a numeric field or a
BOOLEAN_EXISTS field is diagnosed at parse time"), so both rows carry 29.
An earlier draft left the numeric row blank.

**Emit-time, reachable from query text:**

| Kind | Cause | Divergence |
|---|---|---|
| `EXISTS_REQUIRES_FAST` | `MISCONFIGURED` | |
| `TEXT_RANGE` | `UNSUPPORTED` | 5 |
| `EMIT_FAILED` | `INTERNAL` | |

`EMIT_FAILED` is reachable in principle rather than by a known query: it is
the `:352-359` catch around `analyze(normalize(node))` and `visit()`.

It is deliberately **not** named `BACKEND_REJECTED`. That catch covers
`ValueError`, `TypeError`, `AttributeError`, `NotImplementedError` and
`RecursionError`, and the `emit()` docstring (`:326-340`) enumerates four
shapes, three of which never reach tantivy at all: a `None` child found
while walking, `generic_visit` finding no `visit_*` method (`ast.py:828`),
and `RecursionError` on a too-deep hand-built tree. Logging a Python
recursion limit as "the backend rejected your query" is simply false.
`EMIT_FAILED` claims only what is true: emission did not complete.

**Emit-time backstops, not reachable from query text.** All carry
`cause=INTERNAL`. They keep distinct kinds so the existing matrix tests can
assert which backstop fired without matching on message text:

| Kind | Sites |
|---|---|
| `AST_UNFIELDED_TERM` | `:365` |
| `AST_UNKNOWN_FIELD` | `:368` |
| `AST_JSON_NEEDS_SUBPATH` | `:603`, `:682`, `:763` |
| `AST_BAD_NUMBER` | `:583`, `:655`, `:664`, `:817` |
| `AST_BAD_DATE` | `:831` |
| `AST_PATTERN_ON_KIND` | `:753`, `:758`, `:768` |
| `AST_KIND_NOT_IMPLEMENTED` | `:608`, `:687` |

The `AST_` prefix is the contract: reaching one means a caller built a node
the parser would never produce. This replaces the earlier draft's attempt
to give these emit "phases" alongside their parse counterparts.

`AST_PATTERN_ON_KIND` is deliberately **one** kind covering all three
branches of `_reject_pattern_incompatible_kind`, unlike the parse-time
split. The parse-time split exists because hosts render those; nobody
renders a backstop. This also avoids the trap in the earlier draft, which
claimed an emit phase for `PATTERN_ON_NUMERIC` when `:767` has no numeric
branch at all (U64, DATE and DATETIME all fall through together).

`AST_UNKNOWN_FIELD` is `INTERNAL`, not `INVALID_INPUT`: unknown field names
never survive parsing as field refs, they are absorbed into the default
field as free text (`nosuch:foo` -> `Term(field='content', text='nosuch:foo')`).
Three of its 11 call sites pass a *synthesized* `FieldRef(spec.exists_target)`
(`:549`, `:596`, `:675`), a name the user never typed, which is a further
reason it cannot be a user-facing input error.

### Raise and report helpers

Emit gets one helper, so cause selection stops being hand-picked per site:

```python
_CAUSE: Mapping[DiagnosticKind, Cause] = {...}
_DIVERGENCE: Mapping[DiagnosticKind, int] = {...}

def _fail(kind, *, node=None, resolved=None, raw_value=None, message) -> NoReturn:
    ...  # cause/divergence from the tables, startchar/endchar from node,
         # field/field_kind from resolved
```

`visit_errorleaf` (`:552-555`) does **not** route through `_fail`. It
re-raises an existing parse-origin `Diagnostic` unchanged, and must not
have its cause rewritten to an emit-side value.

`parser/syntax.py`'s `ErrorNode` currently defaults `kind` to
`UNSUPPORTED_PATTERN` (`:469`). With that member gone, `kind` becomes
required. Its only `src/` construction site already passes `TOO_DEEP`
explicitly (`parser/plugins.py:435`), but three test sites rely on the
default (`tests/test_syntax.py:293`, `:651`, `:774`, with `:301` asserting
the defaulted value) and must be updated.

### Spans

`_fail` derives `startchar`/`endchar` from `node` when one is in scope.
Three emit sites have no node available and will carry `None`:

- `_resolve` (`:363`) takes a `FieldRef`, not a node, and is called from 11
  sites. Source of `AST_UNFIELDED_TERM` and `AST_UNKNOWN_FIELD`.
- `_exists_query` (`:480`) takes only a `ResolvedField`. Source of
  `EXISTS_REQUIRES_FAST`.
- The `:359` backstop has no node at all.

Threading a node through `_resolve` and `_exists_query` is in scope for
this work, since `EXISTS_REQUIRES_FAST` is one of only two host-reachable
emit conditions and is exactly the case Problem #2 describes. The other two
are `INTERNAL` backstops where a span has no audience, so they may keep
`None`. The test guard is therefore scoped to reachable kinds, not to all
emit kinds, so it cannot be satisfied vacuously by `_fail`'s `node=None`
default.

`_exists_query` has **three** callers, and all three must pass the node or
the guard fails for one shape while passing for another: `visit_every`
(bare `field:*`), BOOLEAN_EXISTS term emission in `visit_term`, and
`_range_query`'s double-open delegation at `:797-799`. The last is the one
easily missed, and it is the shape that reaches `EXISTS_REQUIRES_FAST` on
U64 and DATE fields.

Caveat worth recording: per `ARCHITECTURE.md:507-514`, a `Wildcard`/`Prefix`
leaf's span covers only the wildcard marker character (`inv*` ->
`Prefix(startchar=3, endchar=4)`), not the whole token. No host UI should
be planned around underlining the full pattern.

### Populating field_kind at parse time

`field_kind` is useless if only the emitter fills it, since the three
`PATTERN_ON_*` kinds are parse-only and are precisely the ones a host wants
to tell apart by kind. All three parse-time construction sites have the
information in hand already:

- `parser/default.py:446` (`BAD_NUMBER`): inside `_parse_u64`, which is
  U64-only by construction, so `field_kind=FieldKind.U64` is a constant.
- `parser/default.py:568` (the `PATTERN_ON_*` split): `resolved` is in
  scope, so `field_kind=resolved.spec.kind`.
- `parser/dateparse.py:984` (`BAD_DATE`): `_error` currently takes
  `field: str` and all 9 of its call sites pass `spec.name` (`:1042`,
  `:1051`, `:1102`, `:1143`, `:1145`, `:1153`, `:1155`, `:1194`, `:1195`).
  Widening the signature to carry the spec, or an explicit kind alongside
  the name, distinguishes DATE from DATETIME exactly. The existing comment
  at `:989-991` already establishes these are never JSON.

Without this, a host resolving `PATTERN_ON_NUMERIC` still has to go back to
the registry, which is the exact round-trip Problem #1 objects to.

### Messages

`Diagnostic.message` stays a hand-written developer string, and its
docstring states plainly that it is log/debug output with no stability
guarantee and must never be parsed.

Two categories of content come out of the prose because they are structured
now: `DIVERGENCES.md` references (three sites) and registry-configuration
advice (one site). Cross-references in comments and docstrings are
unaffected; only user-reachable message strings are in scope.

`QueryError.__init__` passes `diagnostic.message` to `super()`, so
`visit_errorleaf`'s current `f"cannot emit query: {…}"` prefix (`:553-555`)
disappears. This is intended.

## Testing

Test-driven, per the project convention: the failing test lands first and
its failure is confirmed for the expected reason before implementation.

Measured migration surface (the earlier draft's numbers were wrong):

- **80** occurrences of the two removed exception names across **13** test
  files (`rg -o … | wc -l`).
- **20** `match=` assertions actually attached to those two exception types,
  in 5 files (`test_emit_json/patterns/phrase/ranges/terms.py`). The other
  42 of the 62 total `match=` uses are registry `ValueError`, `TimeError`
  and date/config asserts, untouched by this work.
- **2** positional `Diagnostic(...)` constructions broken by `kw_only`:
  `tests/test_errors.py:14`, `tests/test_ast.py:178`.
- **3** `ErrorNode` constructions relying on the removed `kind` default.

New guards:

- Every `DiagnosticKind` member appears in `_CAUSE`. This is the
  exhaustiveness check that keeps a new kind from silently defaulting.
- No `Diagnostic.message` and no `QueryError` message contains
  `"DIVERGENCES"`.
- Emit-time diagnostics for the **reachable** kinds carry non-`None`
  `startchar`/`endchar`.
- No `Cause.INVALID_INPUT` diagnostic originates at emit time, which is the
  invariant that lets `Cause` stand in for the dropped `phase` field.

`tests/emitter/test_kind_matrix.py` is the existing leaf-type by field-kind
by spelling exhaustiveness matrix and is the primary vehicle. Its `Raises`
outcome descriptor is currently `(exc, match: str)`, checked at `:142-148`
via `pytest.raises(outcome.exc, match=outcome.match)`, with 8 cells using
it (`:282`, `:298`, `:402`, `:417`, `:431`, `:437`, `:460`, `:720`). The
descriptor becomes `(kind, cause)` and all 8 cells change. Per the
project's sweep convention no cell is exempted: each ends in a parse-time
diagnostic, a documented emit-time `QueryError`, or a real search.

`tests/emitter/test_hypothesis_e2e.py:242-243` currently suppresses **only**
`UnsupportedQueryError`, encoding the invariant stated at `README.md:333`
that the fuzzer never raises anything else. A mechanical rewrite to
`suppress(QueryError)` would silently swallow that invariant. It must
become an explicit re-raise on any cause other than `UNSUPPORTED`. This is
the one place where "hosts write a single `except` clause" does not apply,
and it is a deliberate tightening, since the fuzzer should also never see
`MISCONFIGURED` or `INTERNAL`.

`tests/emitter/test_acceptance_property.py:770` already catches both as a
tuple and needs only the name change.

## Documentation

- `README.md`: host-contract section rewritten. The "catch **both**"
  warning is deleted. The "error messages are written for the host"
  paragraph is re-aimed at the structured contract. `:333`'s fuzzer
  invariant is restated in terms of `Cause.UNSUPPORTED`.
- `ARCHITECTURE.md`: four edits, not one. `:254-258` documents the
  **positional** `Diagnostic(message, kind, startchar, endchar, field,
  raw_value)` signature and lists the four enum members literally; `:262`
  names "the `UNSUPPORTED_PATTERN` site"; `:279-283` describes the two
  exception types; `:437` restates the invariant; `:513` describes
  `UNSUPPORTED_PATTERN` diagnostics built from Wildcard/Prefix.
- `CLAUDE.md:58-60` names both removed exceptions in the parsing-never-raises
  invariant.
- `DIVERGENCES.md`: entries 5, 29 and 30 gain a note that they are
  machine-identifiable via `Diagnostic.divergence`. Separately, `:506`,
  `:1048` and `:1612` name the removed exception classes and `:975`,
  `:1040` name `DiagnosticKind.UNSUPPORTED_PATTERN`; all five are stale.
- `fields.py:579-589`: a docstring describing the two-part host contract by
  the old exception names.
- `__init__.py`: remove `QueryEmitError`/`UnsupportedQueryError` from
  `__all__` (`:53`, `:56`), add `QueryError` and `Cause`. `Diagnostic` and
  `DiagnosticKind` are already exported (`:44-45`).
- `CHANGELOG.md`: a breaking-change entry.

Naming note, no action required: `tests/differential/allowlist.py` defines
an unrelated `DivergenceKind` enum. With `Diagnostic.divergence` added,
"divergence" now names three distinct concepts in this repo. Worth a
sentence in `ARCHITECTURE.md` rather than a rename.

## Rejected alternative

Keep both exception classes, make `.diagnostic` mandatory on both, and drop
`Cause` entirely on the grounds that the class *is* the cause. This is
cheaper: `Phase` disappears anyway, the 80 test references and the doc
mentions of both class names stay valid, and `test_hypothesis_e2e.py`'s
selective suppression keeps working unmodified.

Rejected because the two classes cannot express `MISCONFIGURED`. A non-fast
field failing `field:*` would collapse back into `UnsupportedQueryError`
alongside genuine backend limitations, which re-hides the one distinction
with a genuinely different host response. The same objection applies to
`INTERNAL`: with only two classes, the backstop conditions that the
reachability probe showed dominate the emit surface would be
indistinguishable from real query failures.

The cost of rejecting it is concentrated in one file
(`test_hypothesis_e2e.py`) and is a two-line change that tightens the
invariant rather than weakening it.

## Open items

Resolved during implementation, not blocking this design:

- Whether any of the seven `AST_*` backstops is truly unreachable even from
  a hand-built AST. **Resolved: keep all seven as distinct kinds.** A
  caller building nodes directly against `ast.py` can produce every one of
  these shapes, and distinct kinds let `test_kind_matrix.py` assert which
  backstop fired without matching on prose. Only a backstop proven
  unreachable even from a hand-built node would move to
  `QueryParserError`, and none is.
- Whether `:352-359`'s catch should be split. **Resolved: not split,
  renamed instead.** The five caught exception types do not separate
  cleanly into "tantivy said no" and "our walk blew up" (tantivy-py raises
  `ValueError`, which the AST walk also raises), so a split would be
  guesswork at the catch site. `EMIT_FAILED` under `cause=INTERNAL` states
  only what is known.
