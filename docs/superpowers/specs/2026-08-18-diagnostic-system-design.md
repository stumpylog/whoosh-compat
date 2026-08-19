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
| `has_tag:[a TO b]`, `notes.user:[a TO b]` | **emit**: text range (on BOOLEAN_EXISTS and JSON subpath) |
| `title:a` + `?` * 100 | **emit**: tantivy `RegexQueryError`, compiled regex exceeds 1000 states |
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

**A long pattern is reachable, and it is user input, not a defect.**
`title:a` followed by 50 `?` emits fine; 100 fails, so the shortest
counterexample is about 107 characters. It surfaces through the broad
`:352-359` catch, which an earlier draft labelled `cause=INTERNAL`. That
labelling is wrong in the way that matters most: a host following this
spec's own `Cause` semantics would return 500 and alert an operator
because a user typed a long wildcard. The fix is in the design below
(`PATTERN_TOO_COMPLEX`), not a note.

**Only three emit-time conditions are reachable from query text.** Every
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
    INVALID_INPUT = auto()  # the query text is wrong; the user must change it
    UNSUPPORTED = auto()  # the query is well-formed; this backend cannot run it
    MISCONFIGURED = auto()  # the registry/schema, not the query, is the obstacle
    INTERNAL = auto()  # a defect in this library or in a caller-built AST


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

There is deliberately **no `phase` field**, because the parse-time and
emit-time kind sets are **disjoint**: `kind` alone tells you which phase
produced a record.

An earlier draft argued this from causes instead ("emit never originates
`INVALID_INPUT`"), which is true but insufficient. `visit_errorleaf`
re-raises whatever the `ErrorLeaf` carries, and that includes the
`PATTERN_ON_*` kinds, whose cause is `UNSUPPORTED`. So a
`QueryError(cause=UNSUPPORTED)` is ambiguous between a wrapped parse-time
`PATTERN_ON_SUBPATH` and an originated emit-time `TEXT_RANGE`. Causes
partition only the `INVALID_INPUT` half. Disjoint kind sets cover all of
it, and that is what the test guard asserts.

Hosts that want the partition without hardcoding it get an exported
frozenset rather than a per-record field, since it is a property of the
kind, not of the occurrence.

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
| `TEXT_RANGE` | `UNSUPPORTED` | 5 / 30 / none, by field kind (see below) |
| `PATTERN_TOO_COMPLEX` | `UNSUPPORTED` | |

Every reachable emit kind is non-`INTERNAL`, and that is not a coincidence:
once the one user-reachable shape is pulled out of the broad catch, what
remains is by definition a defect. This makes the span guard trivially
scopable (see Spans) and gives hosts a clean rule: `INTERNAL` at emit time
is never the user's fault.

`PATTERN_TOO_COMPLEX` is what keeps a long user wildcard out of `INTERNAL`.
The pattern-emitting sites wrap their own `tantivy.Query.regex_query(...)`
in a narrow `except ValueError`. At that point we know a pattern was being
compiled and the backend refused it, so the label is honest.
`cause=UNSUPPORTED`, because the query is well-formed and merely exceeds
what this backend will compile.

`TEXT_RANGE`'s divergence varies by field kind, which is why it cannot come
from a kind-keyed table. `visit_termrange` (`:772-773`) is the only visitor
that raises without resolving its field, so today every spelling gets entry
5. Measured, it fires on four different kinds:

| Query | Correct divergence |
|---|---|
| `title:[a TO b]` (TEXT/KEYWORD) | 5 |
| `notes.user:[a TO b]` (JSON subpath) | 30 |
| `has_tag:[a TO b]` (BOOLEAN_EXISTS) | none |

`DIVERGENCES.md:27-29` scopes entry 5 to text ranges that *"worked in
whoosh"*. A range on a synthetic BOOLEAN_EXISTS field never worked in
whoosh, and a subpath range is entry 30's territory. Stamping 5 on all
three would ship a wrong cross-reference under the banner of making
divergences machine-readable. `visit_termrange` must therefore resolve its
field, which also fixes `TEXT_RANGE` shipping `field=None, field_kind=None`
today, defeating Goal #3 on one of only three reachable emit kinds.

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
| `AST_INVALID_SHAPE` | `:352-359`, non-backend half |
| `BACKEND_REJECTED` | `:352-359`, backend half |

The last two replace a single `EMIT_FAILED` kind an earlier revision used
for the whole `:352-359` catch. One try block currently wraps both
`ast.analyze(ast.normalize(node))` and `self.visit(...)` and catches five
exception types, which is at least two unrelated failure modes sharing a
label. The split is by stage **and** by type, because splitting by stage
alone mislabels a `RecursionError` raised during `visit()` as a backend
rejection:

- Anything raised by `normalize`/`analyze` is `AST_INVALID_SHAPE`. The
  backend is not involved at that stage.
- From `visit()`: `RecursionError`, `NotImplementedError` and
  `AttributeError` are `AST_INVALID_SHAPE` (a too-deep tree, a missing
  `visit_*` at `ast.py:828`, a `None` child). Only `ValueError` and
  `TypeError` are `BACKEND_REJECTED`, which is where tantivy-py actually
  refuses a query we constructed.

With `PATTERN_TOO_COMPLEX` taking the one user-reachable case,
`BACKEND_REJECTED` has no known reaching query and the name is finally
accurate for what remains. If a query is later found that reaches it, that
is a triage bug, not a host-facing error.

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
# errors.py, NOT the emitter: parse-side sites need it too (see below)
_CAUSE: Mapping[DiagnosticKind, Cause] = {...}


def _fail(kind, *, node=None, resolved=None, raw_value=None, divergence=None, message) -> NoReturn:
    ...  # cause from _CAUSE, startchar/endchar from node,
    # field/field_kind from resolved, divergence passed explicitly
```

**`divergence` is a `_fail` argument, not a table lookup.** A
`Mapping[DiagnosticKind, int]` cannot express either of the two kinds that
need it: `TEXT_RANGE` varies 5 / 30 / none by field kind, and
`AST_PATTERN_ON_KIND` merges sites carrying entry 30 (`:753`), entry 29
(`:758`) and none (`:768`). A kind-keyed table would force both to `None`,
which would *silently lose* the cross-references currently living in the
message strings this spec deletes. The prose-cleanup guard would still
pass. That is a worse outcome than the status quo, and it is only visible
if you check the table's shape against the merged kinds.

**`_CAUSE` lives in `errors.py`, not `emitters/tantivy_.py`.** `cause` is a
required field with no default, so the four parse-side construction sites
must supply it too: `parser/default.py:446`, `:568`,
`parser/dateparse.py:984`, and `parser/syntax.py:487`. The last is
decisive: `ErrorNode.query()` builds its `Diagnostic` from `self.kind` and
so needs `_CAUSE[self.kind]`, and `parser/` must not import from
`emitters/` under ARCHITECTURE's backend-neutrality rule.

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
- **Every reachable emit kind carries non-`None` `startchar`/`endchar`**:
  `EXISTS_REQUIRES_FAST`, `TEXT_RANGE`, `PATTERN_TOO_COMPLEX`. No qualifier
  is needed, because pulling the user-reachable regex case out of the broad
  catch left every remaining node-less site under `cause=INTERNAL`. An
  earlier revision needed a "node-scoped" escape hatch here, which was a
  sign the kind table was wrong rather than the guard.
- **The parse-time and emit-time kind sets are disjoint.** This, not the
  cause partition, is what makes the dropped `phase` field redundant, and
  it is the guard that will actually fail when someone adds a kind.

`tests/emitter/test_kind_matrix.py` is the existing leaf-type by field-kind
by spelling exhaustiveness matrix and is the primary vehicle. Its `Raises`
outcome descriptor is currently `(exc, match: str)`, checked at `:142-148`
via `pytest.raises(outcome.exc, match=outcome.match)`, with 8 cells using
it (`:282`, `:298`, `:402`, `:417`, `:431`, `:437`, `:460`, `:720`). The
descriptor becomes **`(kind, cause, field_kind)`**, and all 8 cells change.

The third element is not optional. `tests/emitter/test_emit_patterns.py:361-391`
(`test_pattern_on_non_text_kind_raises_at_emit`) has six parametrized cells
asserting `match="U64"` / `"DATE"` / `"DATETIME"`, i.e. *which field kind*
hit the backstop. Merging those into one `AST_PATTERN_ON_KIND` collapses
all six to an identical assertion, and collapses them further with the
subpath cell (`:234`) and the BOOLEAN_EXISTS cell (`:286`): eight
discriminating cells reduced to one. `Diagnostic.field_kind` restores
exactly that distinction, and `:768` has `resolved` in scope so `_fail` can
populate it. Shipping a two-tuple would weaken eight cells that
discriminate correctly today, which the project's sweep convention forbids.
Per the
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

`MISCONFIGURED` is verifiably unreachable for this fuzzer: `_every_atom`
(`:182`) builds `field:*` only over `_TEXT_FIELDS + _KEYWORD_FIELDS`, and
`_NUM_FIELDS`/`_DATE_FIELDS` (`:48-49`) are `tag_id`, `asn`, `created`,
`added`, all `fast=True` in the emitter registry. So the tightening cannot
fire on that branch.

**Ordering constraint on the `INTERNAL` branch:** the tightening must land
*after* `PATTERN_TOO_COMPLEX` exists. Until then the fuzzer can generate a
pattern past tantivy's 1000-state limit and land in the broad catch, which
the tightened guard would re-raise. It does not today only because
`max_leaves=6` (`:213`) keeps generated patterns short, which is an
accident of the generator's bounds and not an invariant. Tightening first
plants a nondeterministic failure.

`tests/emitter/test_acceptance_property.py:770` already catches both as a
tuple and needs only the name change.

## Documentation

- `README.md`: host-contract section rewritten. The "catch **both**"
  warning is deleted. The "error messages are written for the host"
  paragraph is re-aimed at the structured contract. `:333`'s fuzzer
  invariant is restated in terms of `Cause.UNSUPPORTED`.
- `ARCHITECTURE.md`: five edits, not one. `:254-258` documents the
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

Rejected because the two classes cannot express `MISCONFIGURED` or
`INTERNAL`, which the reachability probe showed dominate the emit surface.

That rebuttal, however, only defeats the version that drops `Cause`. A
third option survives it: **keep both classes AND add `cause`, deriving the
class mechanically from the cause inside `_fail`** (`UnsupportedQueryError`
iff `cause is UNSUPPORTED`, `QueryEmitError` otherwise). That keeps full
expressiveness, fixes the arbitrary class selection by construction rather
than by deletion, and leaves 80 test references, 5 doc-file mentions and
`test_hypothesis_e2e.py:242` working unmodified. Its migration is only the
20 relevant `match=` assertions.

The single thing the merge buys over it is "a host writes one `except`
clause", and this spec concedes in the Testing section that even that does
not hold for the one consumer in this repo, which must branch on `cause`
anyway.

The merge is still chosen, but for the honest reason: the third option
keeps **two representations of overlapping information that can drift.**
Nothing prevents a future hand-written `raise QueryEmitError(...)` carrying
`cause=UNSUPPORTED`, contradicting its own class. Deriving the class inside
`_fail` contains that only for sites that use `_fail`. One representation
that cannot disagree with itself is worth the migration cost, given
paperless-ngx is not yet on the old surface.

## Open items

Resolved during implementation, not blocking this design:

- Whether any of the seven `AST_*` backstops is truly unreachable even from
  a hand-built AST. **Resolved: keep all seven as distinct kinds.** A
  caller building nodes directly against `ast.py` can produce every one of
  these shapes, and distinct kinds let `test_kind_matrix.py` assert which
  backstop fired without matching on prose. Only a backstop proven
  unreachable even from a hand-built node would move to
  `QueryParserError`, and none is.
- Whether `:352-359`'s catch should be split. **Resolved: narrowed at the
  source *and* split.** The pattern-emitting sites catch their own
  `regex_query` failure as `PATTERN_TOO_COMPLEX`, removing the one
  user-reachable shape; the remainder splits by stage and exception type
  into `AST_INVALID_SHAPE` and `BACKEND_REJECTED`. Narrowing alone would
  have left one `INTERNAL` kind still describing two unrelated failure
  modes, which is the same silent-fallthrough this project's conventions
  single out.

  An earlier revision closed this item as "not split, renamed instead", on
  the strength of an unverified claim that the catch was unreachable from
  query text. It is reachable in about 107 characters. The lesson is
  recorded here rather than quietly fixed: no reachability claim in this
  document should be trusted without a probe behind it, including the ones
  that sound obviously true.
