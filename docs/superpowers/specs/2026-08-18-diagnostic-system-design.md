# Structured diagnostics across parse and emit

Date: 2026-08-18
Status: approved for planning

## Problem

`whoosh-compat` reports query failures through two unrelated shapes.

Parse failures are structured values: `parse()` never raises for bad query
input, it accumulates `Diagnostic` records into `ParseResult.diagnostics`,
each carrying a machine-stable `kind`, the offending `FieldRef`, the raw
value, and an exact source span.

Emit failures are bare exceptions: `QueryEmitError` and
`UnsupportedQueryError` carry a prose message and nothing else.
`UnsupportedQueryError` has no structured payload at all, and
`QueryEmitError.diagnostic` is optional and populated at exactly one of its
raise sites.

The README tells hosts that both mean the same thing (an HTTP 400) and that
both must be caught. So a host gets an enum for a malformed date and a
string for a text-field range, for outcomes it will render identically.

Three specific consequences:

1. **The `kind` enum is coarser than the messages it carries.**
   `parser/default.py:558-566` selects between three distinct causes for
   `DiagnosticKind.UNSUPPORTED_PATTERN` (numeric field, JSON subpath,
   boolean-exists) and collapses all three onto one member. A host that
   wants to distinguish them must either match on prose or re-resolve
   `Diagnostic.field` through the registry to recover something the library
   already knew and discarded.

2. **Emit-time failures carry no position.** Every AST node carries
   `startchar`/`endchar` (`ast.py:32-33`), preserved through `normalize()`,
   but no emit-time raise reads them. A host can underline the offending
   token for a bad date and cannot for an unknown field.

3. **Library documentation is encoded as English inside messages.** Three
   user-reachable messages embed `DIVERGENCES.md` references
   (`emitters/tantivy_.py:755`, `:760`, `:773`), and one embeds registry
   configuration advice (`:534-537`). The README instructs hosts to strip
   and rewrite these, which in practice means a regex over exception text
   the library explicitly refuses to keep stable.

The parse/emit distinction itself is sound and is preserved. What is wrong
is that the two phases use different *payload types* for the same event.

## Goals

- One structured payload type for every query failure, whatever the phase.
- One exception type for hosts to catch, with a payload that is always
  present.
- Distinctions currently readable only in prose become fields: which field
  kind, which divergence, whose fault.
- Emit-time failures carry source spans.
- `Diagnostic.message` becomes purely a developer/log string with no
  semantic load.

## Non-goals

- No change to query semantics. Nothing about which documents a query
  matches changes, so this introduces no divergence from whoosh and needs
  no allowlist, `DIVERGENCES.md` divergence entry, or corpus line.
- No change to `parse()`'s accumulate-everything behavior. It continues to
  return every problem it found rather than failing on the first.
- No change to `QueryParserError`, which signals an internal invariant
  violation (a bug in this library) rather than a problem with the query.
- No backward-compatibility shims. paperless-ngx's integration is not yet
  complete, so the old names are removed rather than deprecated.

## Design

### Diagnostic

```python
class Phase(Enum):
    PARSE = auto()
    EMIT = auto()


class Cause(Enum):
    INVALID_INPUT = auto()   # the query text is wrong; the user must change it
    UNSUPPORTED = auto()     # the query is well-formed; this backend cannot run it
    MISCONFIGURED = auto()   # the registry/schema, not the query, is the obstacle
    INTERNAL = auto()        # the backend rejected something we believed valid


@dataclass(frozen=True, kw_only=True, slots=True)
class Diagnostic:
    kind: DiagnosticKind
    cause: Cause
    phase: Phase
    message: str
    startchar: int | None = None
    endchar: int | None = None
    field: FieldRef | None = None
    field_kind: FieldKind | None = None
    raw_value: str | None = None
    divergence: int | None = None
```

`kw_only=True` because the field list has grown once and will again;
positional construction would make every future addition a breaking change.
All existing construction sites already pass keywords.

Severity remains fatal-only, permanently, as documented today: a
`Diagnostic` always means the query it concerns cannot be emitted. `Cause`
is not a severity tier. Every cause is fatal to the query; they differ in
who can act on it, not in how bad it is.

`Cause.MISCONFIGURED` exists for one condition today: `field:*` on a
non-fast field (`emitters/tantivy_.py:534-537`). This is neither the user's
error nor a permanent backend limitation, it is the operator's registry
declaration. A host may reasonably alert on it rather than return a 400,
and that decision is invisible in the current prose.

`divergence` is `int | None`. No condition maps to more than one
`DIVERGENCES.md` entry; the plural-looking comment at
`emitters/tantivy_.py:714` describes two separate branches, not one
condition with two entries.

### Exceptions

```
WhooshCompatError                 # base, unchanged
├── QueryError(diagnostic)        # NEW: replaces both emit-time types
└── QueryParserError              # unchanged: internal invariant violation
```

```python
class QueryError(WhooshCompatError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
```

`QueryEmitError` and `UnsupportedQueryError` are deleted, along with their
entries in `__init__.py`'s `__all__`.

The invalid-vs-unsupported distinction those two classes encoded moves to
`Diagnostic.cause`, where it is both finer-grained and impossible to forget
to handle. A host writes one `except` clause.

`QueryParserError` is deliberately untouched and stays outside this scheme.
It means a tagger or filter plugin violated a pipeline invariant, which is
a defect in this library, not a query a host should render an error for.

`Cause.INTERNAL` is not the same thing and does not overlap with it.
`INTERNAL` covers exactly one condition: the backend rejected a query this
library constructed and believed valid (`emitters/tantivy_.py:359` wrapping
a `tantivy` exception). The query reached the backend and came back
refused, so it is still a query outcome and still a `QueryError`.
`QueryParserError` fires earlier and never involves the backend at all.

### Kind inventory

`UNSUPPORTED_PATTERN` splits into three members. Emit-only conditions gain
members. Where parse and emit detect the genuinely same condition, they
share a kind and differ in `phase`.

| Kind | Phases | Cause | Divergence |
|---|---|---|---|
| `BAD_DATE` | parse, emit | `INVALID_INPUT` | |
| `BAD_NUMBER` | parse, emit | `INVALID_INPUT` | |
| `TOO_DEEP` | parse | `INVALID_INPUT` | |
| `PATTERN_ON_NUMERIC` | parse, emit | `UNSUPPORTED` | |
| `PATTERN_ON_SUBPATH` | parse, emit | `UNSUPPORTED` | 30 |
| `PATTERN_ON_BOOLEAN_EXISTS` | parse, emit | `UNSUPPORTED` | 29 |
| `TEXT_RANGE` | emit | `UNSUPPORTED` | 5 |
| `JSON_NEEDS_SUBPATH` | emit | `INVALID_INPUT` | |
| `UNKNOWN_FIELD` | emit | `INVALID_INPUT` | |
| `UNFIELDED_TERM` | emit | `INVALID_INPUT` | |
| `EXISTS_REQUIRES_FAST` | emit | `MISCONFIGURED` | |
| `KIND_NOT_IMPLEMENTED` | emit | `UNSUPPORTED` | |
| `BACKEND_REJECTED` | emit | `INTERNAL` | |

The three `PATTERN_ON_*` kinds firing in both phases is intentional and is
the clearest payoff. Today a wildcard on a JSON subpath produces
`UNSUPPORTED_PATTERN` if the parser catches it and an unrelated
`UnsupportedQueryError` message if the emitter does, for one user-visible
condition. Same kind, different `phase`, is the honest encoding.

### Raise and report helpers

Emit gets one helper, so cause selection stops being hand-picked per site:

```python
_CAUSE: Mapping[DiagnosticKind, Cause] = {...}
_DIVERGENCE: Mapping[DiagnosticKind, int] = {...}

def _fail(kind, *, node=None, resolved=None, raw_value=None, message) -> NoReturn:
    ...  # fills phase=EMIT, cause/divergence from the tables,
         # startchar/endchar from node, field/field_kind from resolved
```

This is what fixes today's arbitrary class selection, where
JSON-needs-subpath raises `QueryEmitError` but pattern-on-subpath raises
`UnsupportedQueryError` for no principled reason.

`parser/syntax.py`'s `ErrorNode` currently defaults `kind` to
`UNSUPPORTED_PATTERN` (`:469`). That default is never used, since its only
non-date construction site passes `TOO_DEEP` explicitly
(`parser/plugins.py:435`). With `UNSUPPORTED_PATTERN` gone, `kind` becomes
a required argument.

### Messages

`Diagnostic.message` stays a hand-written developer string, and its
docstring states plainly that it is log/debug output with no stability
guarantee and must never be parsed.

Two categories of content come out of the prose because they are structured
now: `DIVERGENCES.md` references (three sites) and registry-configuration
advice (one site). Documentation cross-references in *comments and
docstrings* are unaffected; only user-reachable message strings are in
scope.

## Testing

Test-driven, per the project convention: the failing test lands first and
its failure is confirmed for the expected reason before implementation.

Existing surface to migrate: 62 `match=` assertions and roughly 76
references to the two removed exception names across 14 test files. The
work is mechanical but not small, and assertions move from message text to
`kind`/`cause`/`phase`.

New guards:

- Every `DiagnosticKind` member appears in the `_CAUSE` table. This is the
  exhaustiveness check that keeps a new kind from silently defaulting.
- No `Diagnostic.message` and no `QueryError` message contains
  `"DIVERGENCES"`, guarding the prose cleanup against regression.
- Emit-time diagnostics for node-scoped failures carry non-`None`
  `startchar`/`endchar`.

`tests/emitter/test_kind_matrix.py` is the existing leaf-type by field-kind
by spelling exhaustiveness matrix and is the primary vehicle here. Every
cell asserts on `kind`, `cause`, and `phase`, never on message text. Per
the project's sweep convention, no cell is exempted: each ends in a
parse-time diagnostic, a documented emit-time `QueryError`, or a real
search.

## Documentation

- `README.md`: the host-contract section is rewritten. The "catch **both**"
  warning is deleted, since there is one exception type. The "error
  messages are written for the host" paragraph is re-aimed at the
  structured contract: branch on `kind` and `cause`, treat `message` as
  log output.
- `ARCHITECTURE.md`: the diagnostic/error surface description is updated.
- `DIVERGENCES.md`: entries 5, 29 and 30 note that they are now
  machine-identifiable via `Diagnostic.divergence`.
- `fields.py:579-589`: a docstring describing the two-part host contract by
  the old exception names is updated.
- `CHANGELOG.md`: a breaking-change entry.

## Open items

Resolved during implementation, not blocking this design:

- Whether `KIND_NOT_IMPLEMENTED` is reachable at all. The three fallthrough
  raises it covers (`emitters/tantivy_.py:608`, `:687`, `:768`) may be
  defensive dead code, in which case they belong under `QueryParserError`
  as an internal invariant rather than in the kind table.
- Whether `UNFIELDED_TERM` (`:365`) is reachable after `normalize()`, with
  the same consequence.
