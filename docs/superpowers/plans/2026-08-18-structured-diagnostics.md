# Structured Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two unstructured emit-time exception types with a single `QueryError` carrying the same `Diagnostic` record that `parse()` already produces, so hosts branch on an enum instead of parsing prose.

**Architecture:** `Diagnostic` gains `cause`, `field_kind` and `divergence` and becomes the sole failure payload for both phases. `parse()` keeps accumulating diagnostics as values; `emit()` raises `QueryError(diagnostic)`. `DiagnosticKind.UNSUPPORTED_PATTERN` splits three ways, emit-time backstops get an `AST_` prefix under `Cause.INTERNAL`, and the broad emit catch splits so a user-reachable regex-size failure is no longer labelled an internal defect.

**Tech Stack:** Python 3.12+, `uv`, pytest, mypy, ruff, tantivy-py (`~=0.26.0` floor), hypothesis. Real `whoosh` is a test-only oracle.

**Spec:** `docs/superpowers/specs/2026-08-18-diagnostic-system-design.md`

## Global Constraints

- **No em dashes** in any prose: code comments, docstrings, Markdown, commit messages. Use commas, colons, parentheses or separate sentences.
- **No project-process vocabulary** in shipped artifacts. No task numbers, no "review round", no planning references in code, comments, docs or commit messages. Cross-references to `DIVERGENCES.md` entries by number are fine.
- **Stage with explicit paths** (`git add path/to/file`), never `git add -A` or `git add .`.
- **Parametrized cases use `pytest.param(..., id="descriptive-name")`**, never bare tuples.
- **TDD is mandatory.** Write the failing test, run it, confirm it fails for the expected reason, then implement. Report that evidence.
- `src/whoosh_compat/parser/` is a fork of whoosh's `qparser`, excluded from `ruff format`. Do not reformat it; keep edits minimal and localized.
- `emitters/tantivy_.py` is the only module that may import `tantivy`. `errors.py`, `ast.py`, `fields.py` and `parser/` stay backend-neutral.
- Every gate must pass before a task is committed: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run mypy tests`, `uv run pytest tests`.
- Baseline before starting: `1404 passed, 23 skipped`.

## Final DiagnosticKind inventory (18 members)

Copy this verbatim; later tasks reference these exact names.

| Kind | Cause | Phase |
|---|---|---|
| `BAD_DATE` | `INVALID_INPUT` | parse |
| `BAD_NUMBER` | `INVALID_INPUT` | parse |
| `TOO_DEEP` | `INVALID_INPUT` | parse |
| `PATTERN_ON_NUMERIC` | `UNSUPPORTED` | parse |
| `PATTERN_ON_BOOLEAN_EXISTS` | `UNSUPPORTED` | parse |
| `PATTERN_ON_SUBPATH` | `UNSUPPORTED` | parse |
| `EXISTS_REQUIRES_FAST` | `MISCONFIGURED` | emit |
| `TEXT_RANGE` | `UNSUPPORTED` | emit |
| `PATTERN_TOO_COMPLEX` | `UNSUPPORTED` | emit |
| `AST_UNFIELDED_TERM` | `INTERNAL` | emit |
| `AST_UNKNOWN_FIELD` | `INTERNAL` | emit |
| `AST_JSON_NEEDS_SUBPATH` | `INTERNAL` | emit |
| `AST_BAD_NUMBER` | `INTERNAL` | emit |
| `AST_BAD_DATE` | `INTERNAL` | emit |
| `AST_PATTERN_ON_KIND` | `INTERNAL` | emit |
| `AST_KIND_NOT_IMPLEMENTED` | `INTERNAL` | emit |
| `AST_INVALID_SHAPE` | `INTERNAL` | emit |
| `BACKEND_REJECTED` | `INTERNAL` | emit |

## Task assignment

| Task | Agent | Model | Why |
|---|---|---|---|
| 1. `errors.py` foundation | `python-pro` | opus | Defines the contract every other task consumes |
| 2. Parse-side kind split | `python-pro` | opus | Sweep discipline across three construction sites |
| 3. Emit-side `_fail` conversion | `python-pro` | opus | Largest task, 19 raise sites |
| 4. Split the emit catch | `python-pro` | opus | Subtle stage/type split, fixes a mis-caused user error |
| 5. `visit_termrange` resolution | `python-pro` | opus | Divergence correctness per field kind |
| 6. `_exists_query` node threading | `python-pro` | sonnet | Mechanical, 3 call sites |
| 7. Delete old exception names | `python-pro` | sonnet | Mechanical, 80 references |
| 8. Matrix descriptor | `python-pro` | opus | 8 discriminating cells, sweep convention |
| 9. Fuzzer guard tightening | `python-pro` | sonnet | Two-line change, ordering-dependent |
| 10. Documentation sweep | `general-purpose` | sonnet | Prose across 7 files, no code judgment |

Tasks 1-5 are sequential. Task 6 may run parallel with 4-5. Tasks 7-9 require 3 complete. Task 9 requires 4 complete (ordering constraint: the fuzzer can otherwise hit `INTERNAL`). Task 10 requires 7 complete.

---

### Task 1: errors.py foundation

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `src/whoosh_compat/errors.py` (whole file)
- Test: `tests/test_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Cause`, `DiagnosticKind` (18 members above), `Diagnostic` (kw_only, with `cause`/`field_kind`/`divergence`), `QueryError(diagnostic)` with a `.diagnostic` attribute, `cause_for(kind) -> Cause`, `PARSE_KINDS: frozenset[DiagnosticKind]`, `EMIT_KINDS: frozenset[DiagnosticKind]`. `QueryEmitError` and `UnsupportedQueryError` still exist after this task and are removed in Task 7.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_errors.py`:

```python
from whoosh_compat.errors import EMIT_KINDS
from whoosh_compat.errors import PARSE_KINDS
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.errors import cause_for


def test_every_kind_has_a_cause():
    """cause_for() must be total over DiagnosticKind.

    This is the exhaustiveness guard: a new member added without a cause
    entry fails here rather than silently defaulting at a raise site.
    """
    for kind in DiagnosticKind:
        assert isinstance(cause_for(kind), Cause)


def test_parse_and_emit_kind_sets_are_disjoint_and_total():
    """Disjointness is what makes a `phase` field unnecessary: `kind` alone
    identifies which phase produced a Diagnostic.
    """
    assert PARSE_KINDS & EMIT_KINDS == frozenset()
    assert PARSE_KINDS | EMIT_KINDS == frozenset(DiagnosticKind)


def test_diagnostic_is_keyword_only():
    with pytest.raises(TypeError):
        Diagnostic("msg", DiagnosticKind.BAD_DATE, 0, 3)  # type: ignore[misc]


def test_query_error_carries_its_diagnostic():
    d = Diagnostic(
        kind=DiagnosticKind.TEXT_RANGE,
        cause=Cause.UNSUPPORTED,
        message="text ranges are not supported",
        divergence=5,
    )
    err = QueryError(d)
    assert err.diagnostic is d
    assert str(err) == "text ranges are not supported"


def test_internal_emit_kinds_are_never_user_facing():
    """Every emit kind that is reachable from query text is non-INTERNAL.

    Hosts rely on this to route: INTERNAL at emit time is never the user's
    fault, so it is a 500, not a 400.
    """
    reachable = {
        DiagnosticKind.EXISTS_REQUIRES_FAST,
        DiagnosticKind.TEXT_RANGE,
        DiagnosticKind.PATTERN_TOO_COMPLEX,
    }
    for kind in reachable:
        assert cause_for(kind) is not Cause.INTERNAL
    for kind in EMIT_KINDS - reachable:
        assert cause_for(kind) is Cause.INTERNAL
```

Add to `tests/emitter/test_emit_backstop.py` (it will pass trivially until
Task 3 lands, and guards the cleanup from regression afterwards):

```python
def test_no_message_references_project_documentation():
    """DIVERGENCES references belong in Diagnostic.divergence, not in prose.

    Without this guard the cross-references can be deleted from messages
    and silently not replaced, which is worse than leaving them.
    """
    source = pathlib.Path("src/whoosh_compat/emitters/tantivy_.py").read_text()
    in_messages = [
        line
        for line in source.splitlines()
        if "DIVERGENCES" in line and not line.lstrip().startswith("#")
    ]
    assert in_messages == [], in_messages
```

Update the two existing positional constructions to keyword form:
- `tests/test_errors.py:14`
- `tests/test_ast.py:178`

Both currently read `Diagnostic("bad ...", DiagnosticKind.BAD_DATE, N, M)`. Rewrite as:

```python
Diagnostic(
    message="bad date 'x'",
    kind=DiagnosticKind.BAD_DATE,
    cause=Cause.INVALID_INPUT,
    startchar=5,
    endchar=9,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_errors.py -v`
Expected: FAIL with `ImportError: cannot import name 'Cause'`.

- [ ] **Step 3: Rewrite errors.py**

```python
"""Diagnostic and exception hierarchy for whoosh-compat."""

from dataclasses import dataclass
from enum import Enum
from enum import auto

from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef


class Cause(Enum):
    """Who can act on a diagnostic, and whether the query can ever run.

    Not a severity tier: every cause is fatal to the query it concerns.
    A host maps ``INVALID_INPUT``/``UNSUPPORTED`` to a 400,
    ``MISCONFIGURED`` to an operator alert, and ``INTERNAL`` to a 500,
    because ``INTERNAL`` at emit time is never the user's fault.
    """

    INVALID_INPUT = auto()
    UNSUPPORTED = auto()
    MISCONFIGURED = auto()
    INTERNAL = auto()


class DiagnosticKind(Enum):
    """Kinds of diagnostics that can be reported during query processing.

    Member values are a machine-stable contract: a host is expected to
    branch on ``kind`` (for example, mapping ``BAD_DATE`` to a typed
    ``InvalidDateQuery``). ``Diagnostic.message`` carries no such guarantee
    and may reword without notice.

    ``PARSE_KINDS`` and ``EMIT_KINDS`` partition this enum. The partition
    is why ``Diagnostic`` needs no ``phase`` field: ``kind`` alone says
    which half of the pipeline produced a record.

    An ``AST_`` prefix means the condition is unreachable from query text.
    Reaching one means a caller built a node the parser would never
    produce, so it is a defect in the caller, not a bad query.
    """

    # Parse-time.
    BAD_DATE = auto()
    BAD_NUMBER = auto()
    TOO_DEEP = auto()
    PATTERN_ON_NUMERIC = auto()
    PATTERN_ON_BOOLEAN_EXISTS = auto()
    PATTERN_ON_SUBPATH = auto()

    # Emit-time, reachable from query text.
    EXISTS_REQUIRES_FAST = auto()
    TEXT_RANGE = auto()
    PATTERN_TOO_COMPLEX = auto()

    # Emit-time backstops for caller-built ASTs.
    AST_UNFIELDED_TERM = auto()
    AST_UNKNOWN_FIELD = auto()
    AST_JSON_NEEDS_SUBPATH = auto()
    AST_BAD_NUMBER = auto()
    AST_BAD_DATE = auto()
    AST_PATTERN_ON_KIND = auto()
    AST_KIND_NOT_IMPLEMENTED = auto()
    AST_INVALID_SHAPE = auto()
    BACKEND_REJECTED = auto()


PARSE_KINDS = frozenset(
    {
        DiagnosticKind.BAD_DATE,
        DiagnosticKind.BAD_NUMBER,
        DiagnosticKind.TOO_DEEP,
        DiagnosticKind.PATTERN_ON_NUMERIC,
        DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
        DiagnosticKind.PATTERN_ON_SUBPATH,
    }
)

EMIT_KINDS = frozenset(DiagnosticKind) - PARSE_KINDS


_CAUSE: dict[DiagnosticKind, Cause] = {
    DiagnosticKind.BAD_DATE: Cause.INVALID_INPUT,
    DiagnosticKind.BAD_NUMBER: Cause.INVALID_INPUT,
    DiagnosticKind.TOO_DEEP: Cause.INVALID_INPUT,
    DiagnosticKind.PATTERN_ON_NUMERIC: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_ON_SUBPATH: Cause.UNSUPPORTED,
    DiagnosticKind.EXISTS_REQUIRES_FAST: Cause.MISCONFIGURED,
    DiagnosticKind.TEXT_RANGE: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_TOO_COMPLEX: Cause.UNSUPPORTED,
    DiagnosticKind.AST_UNFIELDED_TERM: Cause.INTERNAL,
    DiagnosticKind.AST_UNKNOWN_FIELD: Cause.INTERNAL,
    DiagnosticKind.AST_JSON_NEEDS_SUBPATH: Cause.INTERNAL,
    DiagnosticKind.AST_BAD_NUMBER: Cause.INTERNAL,
    DiagnosticKind.AST_BAD_DATE: Cause.INTERNAL,
    DiagnosticKind.AST_PATTERN_ON_KIND: Cause.INTERNAL,
    DiagnosticKind.AST_KIND_NOT_IMPLEMENTED: Cause.INTERNAL,
    DiagnosticKind.AST_INVALID_SHAPE: Cause.INTERNAL,
    DiagnosticKind.BACKEND_REJECTED: Cause.INTERNAL,
}


def cause_for(kind: DiagnosticKind) -> Cause:
    """The ``Cause`` for ``kind``.

    Lives here rather than in the emitter because the parse-side
    construction sites need it too, and ``parser/`` must not import from
    ``emitters/``.
    """

    return _CAUSE[kind]


@dataclass(frozen=True, kw_only=True, slots=True)
class Diagnostic:
    """A structured record of why a query cannot run.

    Severity is fatal-only, permanently: a ``Diagnostic`` always means the
    query it concerns cannot be emitted. There is no ``severity`` field and
    none will be added; a future informational-only signal (for example,
    reporting that ``analyze()`` dropped a zero-token term) must use a
    separate channel, never ``ParseResult.diagnostics``.

    ``message`` is developer/log output only. It has no stability
    guarantee and must never be parsed. Branch on ``kind`` and ``cause``.

    ``divergence`` is the ``DIVERGENCES.md`` entry number when one applies,
    so a host can cross-reference without reading prose.
    """

    kind: DiagnosticKind
    cause: Cause
    message: str
    startchar: int | None = None
    endchar: int | None = None
    field: FieldRef | None = None
    field_kind: FieldKind | None = None
    raw_value: str | None = None
    divergence: int | None = None


class WhooshCompatError(Exception):
    """Base exception for whoosh-compat."""


class QueryError(WhooshCompatError):
    """Raised by ``emit()`` when a query cannot be turned into a backend query.

    Always carries a ``Diagnostic``. Callers branch on
    ``err.diagnostic.cause``; the exception's own message is the
    diagnostic's message and carries no stability guarantee.
    """

    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


class QueryParserError(WhooshCompatError):
    """Raised when an internal parser pipeline invariant is violated.

    Not raised for bad *user* query input (see the module-level invariant:
    ``parse()`` never raises for that, it accumulates ``Diagnostic``s
    instead). The only raise sites are internal self-checks in
    ``parser/default.py`` (a tagger failed to advance the cursor, a filter
    returned ``None`` where a node was required), both of which indicate a
    bug in a tagger/filter plugin, not something a caller passing ordinary
    query strings should ever expect to catch.

    Distinct from ``Cause.INTERNAL``, which describes a ``Diagnostic``
    about an AST that already exists. This fires during the tagger/filter
    pipeline, before there is one.
    """


class UnsupportedQueryError(WhooshCompatError):
    """Deprecated, removed once every raise site moves to ``QueryError``."""


class QueryEmitError(WhooshCompatError):
    """Deprecated, removed once every raise site moves to ``QueryError``."""

    def __init__(self, msg: str, *, diagnostic: Diagnostic | None = None):
        super().__init__(msg)
        self.diagnostic = diagnostic
```

Note the new `FieldKind` import. `fields.py` must not import `errors.py` or this cycles; confirm with `uv run python -c "import whoosh_compat"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_errors.py tests/test_ast.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full gate**

```bash
uv run ruff check . && uv run mypy src && uv run mypy tests && uv run pytest tests
```
Expected: the suite still passes. Every existing `UNSUPPORTED_PATTERN` reference now fails to resolve, so if `pytest` reports `AttributeError: UNSUPPORTED_PATTERN`, that is Task 2's work; leave those failures and do NOT commit until Step 6 passes. If you cannot get green here, stop and report rather than reverting the enum.

- [ ] **Step 6: Bridge the removed enum member**

`parser/syntax.py:469` and `parser/plugins.py:435` reference `UNSUPPORTED_PATTERN`. To keep this task self-contained and the suite green, temporarily point `syntax.py`'s default at `DiagnosticKind.PATTERN_ON_SUBPATH` and add `cause=cause_for(self.kind)` to the `Diagnostic(...)` at `syntax.py:487`. Task 2 makes `kind` required and removes the default.

- [ ] **Step 7: Commit**

```bash
git add src/whoosh_compat/errors.py src/whoosh_compat/parser/syntax.py tests/test_errors.py tests/test_ast.py
git commit -m "feat: add Cause, QueryError and the split diagnostic kinds"
```

---

### Task 2: Parse-side kind split and field_kind population

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `src/whoosh_compat/parser/default.py:440-455`, `:514-575`
- Modify: `src/whoosh_compat/parser/dateparse.py:983-996` and its 9 call sites
- Modify: `src/whoosh_compat/parser/syntax.py:464-493`
- Modify: `src/whoosh_compat/parser/plugins.py:435`
- Test: `tests/test_parser_fields.py`, `tests/test_parser_dates.py`, `tests/test_syntax.py`

**Interfaces:**
- Consumes: `Cause`, `cause_for`, the split `DiagnosticKind` members from Task 1.
- Produces: parse-time diagnostics that carry `cause` and `field_kind`. `DateParserPlugin._error(node, text, spec)` now takes a `FieldSpec` rather than a `str`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from whoosh_compat import parse
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind


@pytest.mark.parametrize(
    ("query", "kind", "field_kind"),
    [
        pytest.param("asn:1*", DiagnosticKind.PATTERN_ON_NUMERIC, FieldKind.U64, id="numeric"),
        pytest.param(
            "has_tag:t*",
            DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
            FieldKind.BOOLEAN_EXISTS,
            id="boolean-exists",
        ),
        pytest.param(
            "notes.user:fo*",
            DiagnosticKind.PATTERN_ON_SUBPATH,
            FieldKind.JSON,
            id="json-subpath",
        ),
    ],
)
def test_pattern_diagnostics_split_by_field_kind(reg, query, kind, field_kind):
    """UNSUPPORTED_PATTERN used to collapse these three onto one member,
    forcing a host to match on prose or re-resolve the field.
    """
    result = parse(query, registry=reg, default_fields=["content"])
    (d,) = result.diagnostics
    assert d.kind is kind
    assert d.cause is Cause.UNSUPPORTED
    assert d.field_kind is field_kind


@pytest.mark.parametrize(
    ("query", "kind", "field_kind"),
    [
        pytest.param("asn:nope", DiagnosticKind.BAD_NUMBER, FieldKind.U64, id="u64-term"),
        pytest.param("created:nope", DiagnosticKind.BAD_DATE, FieldKind.DATE, id="date-term"),
        pytest.param("added:nope", DiagnosticKind.BAD_DATE, FieldKind.DATETIME, id="datetime-term"),
    ],
)
def test_value_diagnostics_carry_field_kind(reg, query, kind, field_kind):
    result = parse(query, registry=reg, default_fields=["content"])
    (d,) = result.diagnostics
    assert d.kind is kind
    assert d.cause is Cause.INVALID_INPUT
    assert d.field_kind is field_kind


def test_pattern_divergences_are_machine_readable(reg):
    """Entry 29 covers numeric AND boolean-exists; entry 30 covers subpaths."""
    for query, entry in (("asn:1*", 29), ("has_tag:t*", 29), ("notes.user:fo*", 30)):
        (d,) = parse(query, registry=reg, default_fields=["content"]).diagnostics
        assert d.divergence == entry, query
```

`reg` is the existing fixture in `tests/conftest.py`. Confirm it declares `asn` (U64), `created` (DATE), `added` (DATETIME), `has_tag` (BOOLEAN_EXISTS) and `notes` (JSON with a `user` subpath); if any is missing, add it there rather than defining a local registry.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parser_fields.py -k "split_by_field_kind or carry_field_kind or machine_readable" -v`
Expected: FAIL, `AttributeError: PATTERN_ON_NUMERIC` is already resolved by Task 1, so expect assertion failures on `d.field_kind is None` and `d.divergence is None`.

- [ ] **Step 3: Split the wildcard diagnostic**

In `parser/default.py`, replace the body of `_wildcard_kind_diagnostic` (currently `:557-575`) so each branch picks its own kind and divergence:

```python
        resolved = self.registry.resolve(ref) if ref is not None else None
        if resolved is None:
            return None
        if resolved.spec.kind is FieldKind.U64:
            kind = DiagnosticKind.PATTERN_ON_NUMERIC
            divergence = 29
            message = f"wildcard patterns are not supported on numeric field {ref}"
        elif resolved.is_subpath:
            kind = DiagnosticKind.PATTERN_ON_SUBPATH
            divergence = 30
            message = f"wildcard patterns are not supported on a JSON subpath ({ref})"
        elif resolved.spec.kind is FieldKind.BOOLEAN_EXISTS:
            kind = DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS
            divergence = 29
            message = f"wildcard patterns are not supported on boolean-exists field {ref}"
        else:
            return None
        d = Diagnostic(
            message=message,
            kind=kind,
            cause=cause_for(kind),
            startchar=startchar,
            endchar=endchar,
            field=ref,
            field_kind=resolved.spec.kind,
            raw_value=text,
            divergence=divergence,
        )
        self.report(d)
        return ast.ErrorLeaf(diagnostic=d)
```

Keep the branch order exactly as above: the subpath test must stay ahead of the `BOOLEAN_EXISTS` test, matching the current code, because a subpath on a boolean-exists target would otherwise take the wrong branch.

- [ ] **Step 4: Populate cause and field_kind at the remaining parse sites**

`parser/default.py:446` inside `_parse_u64`, which is U64-only by construction:

```python
            d = Diagnostic(
                message=f"{text!r} is not a valid number for {ref}",
                kind=DiagnosticKind.BAD_NUMBER,
                cause=cause_for(DiagnosticKind.BAD_NUMBER),
                startchar=startchar,
                endchar=endchar,
                field=ref,
                field_kind=FieldKind.U64,
                raw_value=text,
            )
```

`parser/dateparse.py:983`, widening the signature from `field: str` to `spec`:

```python
    def _error(self, node: syntax.SyntaxNode, text: str, spec: FieldSpec) -> DateErrorNode:
        diagnostic = Diagnostic(
            message=f"{text!r} is not a recognizable date",
            kind=DiagnosticKind.BAD_DATE,
            cause=cause_for(DiagnosticKind.BAD_DATE),
            startchar=node.startchar,
            endchar=node.endchar,
            # DATE/DATETIME fields are never JSON (FieldRegistry rejects
            # date_only/comma_values combinations outside those kinds), so
            # this is always a plain field reference; spec.name is already
            # canonical (aliases resolved by the time a spec is in hand).
            field=FieldRef(spec.name),
            field_kind=spec.kind,
            raw_value=text,
        )
        return DateErrorNode(diagnostic)
```

Then change all 9 call sites from `self._error(node, X, spec.name)` to `self._error(node, X, spec)`: lines `1042`, `1051`, `1102`, `1143`, `1145`, `1153`, `1155`, `1194`, `1195`.

- [ ] **Step 5: Make ErrorNode.kind required**

In `parser/syntax.py:464-470`, drop the default so a new caller must choose:

```python
    def __init__(
        self,
        message: str,
        kind: DiagnosticKind,
        node: SyntaxNode | None = None,
    ) -> None:
```

`parser/plugins.py:435` already passes `kind=DiagnosticKind.TOO_DEEP` and needs only reordering to keyword form. Three test sites rely on the removed default and must now pass one explicitly: `tests/test_syntax.py:293`, `:651`, `:774`. `tests/test_syntax.py:301` asserts the defaulted value; change it to assert whichever kind that test now passes.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_parser_fields.py tests/test_parser_dates.py tests/test_syntax.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full gate**

```bash
uv run ruff check . && uv run mypy src && uv run mypy tests && uv run pytest tests
```
Expected: `1404 passed` or higher (new tests added), `23 skipped`. The differential layer must not move: `test_diagnostic_skip_count_matches_corpus` pins 23 and gates on truthiness only, so splitting the enum cannot change it. If that count moved, stop and report.

- [ ] **Step 8: Commit**

```bash
git add src/whoosh_compat/parser/default.py src/whoosh_compat/parser/dateparse.py src/whoosh_compat/parser/syntax.py src/whoosh_compat/parser/plugins.py tests/test_parser_fields.py tests/test_parser_dates.py tests/test_syntax.py
git commit -m "feat: split pattern diagnostics by field kind and carry field_kind"
```

---

### Task 3: Emit-side _fail conversion

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `src/whoosh_compat/emitters/tantivy_.py`, all raise sites except `:359`
- Test: `tests/emitter/test_emit_terms.py`, `test_emit_phrase.py`, `test_emit_ranges.py`, `test_emit_json.py`, `test_emit_patterns.py`, `test_emit_boolean.py`, `test_emit_backstop.py`

**Interfaces:**
- Consumes: `QueryError`, `cause_for`, all `DiagnosticKind` members.
- Produces: `_fail(kind, *, node=None, resolved=None, raw_value=None, divergence=None, message) -> NoReturn` on the emitter visitor. Every emit path except `:352-359` now raises `QueryError`.

**Site-to-kind map.** Convert exactly these, one kind each:

| Site | Kind |
|---|---|
| `:365` unfielded term | `AST_UNFIELDED_TERM` |
| `:368` unknown field | `AST_UNKNOWN_FIELD` |
| `:534` exists non-fast | `EXISTS_REQUIRES_FAST` |
| `:553` errorleaf re-raise | (carve-out, see Step 4) |
| `:583`, `:655`, `:664`, `:817` bad number | `AST_BAD_NUMBER` |
| `:603`, `:682`, `:763` JSON needs subpath | `AST_JSON_NEEDS_SUBPATH` |
| `:608`, `:687` kind not implemented | `AST_KIND_NOT_IMPLEMENTED` |
| `:753`, `:758`, `:768` pattern on kind | `AST_PATTERN_ON_KIND` |
| `:773` text range | `TEXT_RANGE` (Task 5 refines) |
| `:831` bad date bound | `AST_BAD_DATE` |

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from whoosh_compat import ast
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldRef


def test_exists_on_non_fast_field_is_misconfigured(ereg, tindex, parse):
    """A non-fast field failing `field:*` is the operator's registry, not
    the user's query, so a host may alert rather than return a 400.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("notes:*"), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.EXISTS_REQUIRES_FAST
    assert d.cause is Cause.MISCONFIGURED


def test_text_range_is_unsupported_with_its_divergence(ereg, tindex, parse):
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("title:[a TO b]"), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.TEXT_RANGE
    assert d.cause is Cause.UNSUPPORTED
    assert d.divergence == 5


def test_hand_built_unknown_field_is_internal(ereg, tindex):
    """Query text cannot reach this: unknown field names are absorbed into
    the default field as free text. Only a hand-built node gets here.
    """
    node = ast.Term(field=FieldRef("nosuchfield"), text="x")
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_UNKNOWN_FIELD
    assert d.cause is Cause.INTERNAL


def test_errorleaf_reraise_keeps_the_parse_diagnostic(ereg, tindex, parse):
    """emit() must re-raise the parse-time record unchanged, not restamp it
    with an emit-side cause.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("asn:notanumber"), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.BAD_NUMBER
    assert d.cause is Cause.INVALID_INPUT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/emitter/test_emit_terms.py -k "misconfigured or unsupported_with_its or is_internal or reraise" -v`
Expected: FAIL with `DID NOT RAISE QueryError` (they raise the old types).

- [ ] **Step 3: Add the _fail helper**

On the emitter visitor class, above the visitors:

```python
    def _fail(
        self,
        kind: DiagnosticKind,
        *,
        message: str,
        node: ast.Node | None = None,
        resolved: ResolvedField | None = None,
        raw_value: str | None = None,
        divergence: int | None = None,
    ) -> NoReturn:
        """Raise a ``QueryError`` carrying a fully populated ``Diagnostic``.

        Single funnel for emit-time failures so the cause is looked up
        rather than hand-picked per site. ``divergence`` is an argument
        rather than a table lookup because it varies by field kind for
        ``TEXT_RANGE`` and spans two entries for ``AST_PATTERN_ON_KIND``.
        """

        raise QueryError(
            Diagnostic(
                kind=kind,
                cause=cause_for(kind),
                message=message,
                startchar=node.startchar if node is not None else None,
                endchar=node.endchar if node is not None else None,
                field=FieldRef(resolved.dotted_name) if resolved is not None else None,
                field_kind=resolved.spec.kind if resolved is not None else None,
                raw_value=raw_value,
                divergence=divergence,
            )
        )
```

- [ ] **Step 4: Carve out visit_errorleaf**

`visit_errorleaf` must NOT route through `_fail`, or the parse-origin cause is overwritten and the phase information is destroyed:

```python
    def visit_errorleaf(self, node: ast.ErrorLeaf) -> tantivy.Query:
        # Re-raises the parse-time record unchanged. Routing this through
        # _fail would restamp an emit-side cause onto a parse diagnostic.
        raise QueryError(node.diagnostic)
```

The old `"cannot emit query: "` prefix disappears. That is intended.

- [ ] **Step 5: Convert the remaining sites**

Work the site-to-kind table above top to bottom. Two representative conversions:

```python
        # was: raise QueryEmitError(f"unknown field {str(field)!r}")
        self._fail(
            DiagnosticKind.AST_UNKNOWN_FIELD,
            message=f"unknown field {str(field)!r}",
        )
```

```python
# was: raise UnsupportedQueryError(f"... (DIVERGENCES.md entry 30)")
self._fail(
    DiagnosticKind.AST_PATTERN_ON_KIND,
    message=(
        f"wildcard/prefix patterns are not supported on JSON subpath {resolved.dotted_name!r}"
    ),
    resolved=resolved,
    divergence=30,
)
```

Strip every `(DIVERGENCES.md entry N)` from message text and pass `divergence=N` instead. Strip the `fast=True` advice from `:534` and pass `resolved=resolved`; `cause=MISCONFIGURED` is what tells a host to check the registry. Pass `node=` wherever a node is in scope.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/emitter -v`
Expected: the four new tests PASS. Many existing tests still fail because they assert the old exception types; that is Task 7. Record the failure count.

- [ ] **Step 7: Commit**

```bash
git add src/whoosh_compat/emitters/tantivy_.py tests/emitter/test_emit_terms.py
git commit -m "feat: route emit failures through a single QueryError funnel"
```

---

### Task 4: Split the emit catch-all

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `src/whoosh_compat/emitters/tantivy_.py:349-359`, and the `regex_query` call site near `:748`
- Test: `tests/emitter/test_emit_backstop.py`

**Interfaces:**
- Consumes: `_fail` from Task 3.
- Produces: `PATTERN_TOO_COMPLEX` raised from the pattern sites; `AST_INVALID_SHAPE` and `BACKEND_REJECTED` replacing the single broad catch.

**Why this task exists:** `title:a` followed by 100 `?` reaches the broad catch through tantivy's 1000-state regex limit. Labelling that `INTERNAL` makes a host return 500 for ordinary user input.

- [ ] **Step 1: Write the failing tests**

```python
def test_oversized_pattern_is_unsupported_not_internal(ereg, tindex, parse):
    """A long wildcard is user input, not a defect. It must not be
    INTERNAL, or a host returns 500 for a query someone typed.
    """
    q = "title:a" + "?" * 100
    with pytest.raises(QueryError) as exc:
        emit_ast(parse(q), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.PATTERN_TOO_COMPLEX
    assert d.cause is Cause.UNSUPPORTED
    assert d.startchar is not None


def test_short_pattern_still_emits(ereg, tindex, parse):
    """Guards the threshold: 50 `?` compiles fine, so the narrow catch must
    not swallow ordinary patterns.
    """
    assert emit_ast(parse("title:a" + "?" * 50), tindex, ereg) is not None


def test_unvisitable_node_is_ast_invalid_shape(ereg, tindex):
    class Bogus(ast.Node):
        pass

    with pytest.raises(QueryError) as exc:
        emit_ast(Bogus(), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert d.cause is Cause.INTERNAL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/emitter/test_emit_backstop.py -k "oversized or unvisitable" -v`
Expected: FAIL. The oversized case currently raises with `kind=AST_INVALID_SHAPE` or the pre-split kind, not `PATTERN_TOO_COMPLEX`.

- [ ] **Step 3: Narrow the pattern site**

Wrap the `regex_query` call (near `:748`):

```python
        try:
            return tantivy.Query.regex_query(self.schema, spec.name, regex)
        except ValueError as exc:
            # tantivy caps compiled regex size (1000 states by default).
            # A long user wildcard lands here, so this is UNSUPPORTED
            # input, not an internal defect.
            self._fail(
                DiagnosticKind.PATTERN_TOO_COMPLEX,
                message=f"pattern is too complex for the backend to compile: {exc}",
                node=node,
                resolved=resolved,
            )
```

`node` and `resolved` must be in scope at that call site; thread them through the helper's signature if they are not.

- [ ] **Step 4: Split the broad catch**

Replace `:349-359` with a two-stage form. Split by stage AND by exception type, because a `RecursionError` raised during `visit()` is a too-deep tree, not a backend rejection:

```python
        try:
            analyzed = ast.analyze(ast.normalize(node), self.registry, default_mode=Multitoken.AND)
        except (
            ValueError,
            TypeError,
            AttributeError,
            NotImplementedError,
            RecursionError,
        ) as exc:
            # The backend is not involved at this stage, so nothing here
            # can be a backend rejection.
            self._fail(
                DiagnosticKind.AST_INVALID_SHAPE,
                message=f"cannot emit query: {exc}",
            )
        try:
            return self.visit(analyzed)
        except (AttributeError, NotImplementedError, RecursionError) as exc:
            # A missing visit_* method, a None child, or a tree too deep to
            # walk. All caller-built defects, none of which reached tantivy.
            self._fail(
                DiagnosticKind.AST_INVALID_SHAPE,
                message=f"cannot emit query: {exc}",
            )
        except (ValueError, TypeError) as exc:
            # tantivy-py refused a query we constructed.
            self._fail(
                DiagnosticKind.BACKEND_REJECTED,
                message=f"cannot emit query: {exc}",
            )
```

`QueryError` inherits from `Exception`, not `ValueError`, so a `_fail` from a nested visitor passes through these handlers untouched. Verify that with the existing backstop tests before moving on.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/emitter/test_emit_backstop.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/whoosh_compat/emitters/tantivy_.py tests/emitter/test_emit_backstop.py
git commit -m "feat: separate oversized patterns and AST defects from backend rejections"
```

---

### Task 5: Resolve the field in visit_termrange

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `src/whoosh_compat/emitters/tantivy_.py:772-773`
- Test: `tests/emitter/test_emit_ranges.py`

**Interfaces:**
- Consumes: `_fail` from Task 3.
- Produces: `TEXT_RANGE` diagnostics carrying `field`, `field_kind` and a per-kind `divergence`.

**Why:** `visit_termrange` is the only visitor that raises without resolving its field, so today every spelling gets entry 5. Measured, it fires on four kinds, and entry 5 is scoped to ranges that *worked in whoosh*.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(
    ("query", "field_kind", "divergence"),
    [
        pytest.param("title:[a TO b]", FieldKind.TEXT, 5, id="text"),
        pytest.param("tag:[a TO b]", FieldKind.KEYWORD, 5, id="keyword"),
        pytest.param("notes.user:[a TO b]", FieldKind.JSON, 30, id="json-subpath"),
        pytest.param("has_tag:[a TO b]", FieldKind.BOOLEAN_EXISTS, None, id="boolean-exists"),
    ],
)
def test_text_range_divergence_varies_by_field_kind(
    ereg, tindex, parse, query, field_kind, divergence
):
    """Entry 5 is scoped to text ranges that worked in whoosh. A range on a
    synthetic boolean-exists field never did, and a subpath range is entry
    30's territory, so stamping 5 on all of them ships a wrong reference.
    """
    with pytest.raises(QueryError) as exc:
        emit_ast(parse(query), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.TEXT_RANGE
    assert d.field_kind is field_kind
    assert d.divergence == divergence
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/emitter/test_emit_ranges.py -k divergence_varies -v`
Expected: FAIL, `d.field_kind is None` for every case.

- [ ] **Step 3: Implement**

```python
    def visit_termrange(self, node: ast.TermRange) -> tantivy.Query:
        resolved = self._resolve(node.field)
        if resolved.is_subpath:
            divergence = 30
        elif resolved.spec.kind in (FieldKind.TEXT, FieldKind.KEYWORD):
            divergence = 5
        else:
            # A range on a synthetic boolean-exists field has no whoosh
            # behavior to diverge from.
            divergence = None
        self._fail(
            DiagnosticKind.TEXT_RANGE,
            message="text ranges are not supported",
            node=node,
            resolved=resolved,
            divergence=divergence,
        )
```

Note this now calls `_resolve`, so a `TermRange` on an unknown field raises `AST_UNKNOWN_FIELD` before `TEXT_RANGE`. That is correct ordering: an unresolvable field is the more specific failure.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/emitter/test_emit_ranges.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/whoosh_compat/emitters/tantivy_.py tests/emitter/test_emit_ranges.py
git commit -m "feat: carry field kind and the correct divergence on text ranges"
```

---

### Task 6: Thread nodes into _exists_query

**Agent:** `python-pro` | **Model:** sonnet

**Files:**
- Modify: `src/whoosh_compat/emitters/tantivy_.py:480` and its three callers
- Test: `tests/emitter/test_emit_boolean.py`

**Interfaces:**
- Consumes: `_fail` from Task 3.
- Produces: `_exists_query(self, resolved, *, node=None)`. `EXISTS_REQUIRES_FAST` diagnostics carry spans.

**Why three callers matter:** miss one and the guard passes for one spelling while failing for another. `_exists_query` is called from `visit_every` (bare `field:*`), BOOLEAN_EXISTS term emission in `visit_term`, and `_range_query`'s double-open delegation at `:797-799`. The last one is how `slow_num:[TO]` reaches this kind.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("notes:*", id="star-json"),
        pytest.param("notes.user:*", id="star-subpath"),
        pytest.param("slow_num:[TO]", id="double-open-inclusive"),
        pytest.param("slow_num:{TO}", id="double-open-exclusive"),
        pytest.param("slow_date:[TO]", id="double-open-date"),
    ],
)
def test_exists_requires_fast_carries_a_span(nonfast_reg, nonfast_index, query):
    result = parse(query, registry=nonfast_reg, default_fields=["content"])
    assert not result.diagnostics
    with pytest.raises(QueryError) as exc:
        emit_(result.ast, index=nonfast_index, registry=nonfast_reg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.EXISTS_REQUIRES_FAST
    assert d.startchar is not None, "span is required for a host-reachable kind"
    assert d.endchar is not None
```

This needs a registry with non-fast `slow_num` (U64) and `slow_date` (DATE) plus a matching tantivy schema. Add `nonfast_reg` and `nonfast_index` fixtures to `tests/emitter/conftest.py` alongside `ereg`/`tindex`, mirroring their construction.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/emitter/test_emit_boolean.py -k carries_a_span -v`
Expected: FAIL with `assert None is not None`, "span is required for a host-reachable kind".

- [ ] **Step 3: Implement**

Change the signature and the `_fail` call:

```python
    def _exists_query(self, resolved: ResolvedField, *, node: ast.Node | None = None) -> tantivy.Query:
```

```python
        self._fail(
            DiagnosticKind.EXISTS_REQUIRES_FAST,
            message=(
                f"field {resolved.dotted_name!r} ({resolved.spec.kind.name}) has no way "
                f"to match 'exists' while non-fast"
            ),
            node=node,
            resolved=resolved,
        )
```

Then update all three callers to pass `node=`:
- `visit_every`, passing its own node
- the BOOLEAN_EXISTS branch in `visit_term`, passing the term node
- `_range_query` at `:797-799`, passing the range node it already holds

The old message named the `field:*` spelling even for a `[TO]` query, advising about a query the user never typed. Dropping that clause is deliberate; `cause=MISCONFIGURED` carries the actionable part.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/emitter/test_emit_boolean.py -v`
Expected: PASS, all five parametrized cases.

- [ ] **Step 5: Commit**

```bash
git add src/whoosh_compat/emitters/tantivy_.py tests/emitter/conftest.py tests/emitter/test_emit_boolean.py
git commit -m "feat: carry source spans on exists-requires-fast diagnostics"
```

---

### Task 7: Delete the old exception names

**Agent:** `python-pro` | **Model:** sonnet

**Files:**
- Modify: `src/whoosh_compat/errors.py` (remove the two deprecated classes)
- Modify: `src/whoosh_compat/__init__.py:28-31`, `:44-60`
- Modify: 13 test files carrying 80 references
- Test: the whole suite

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: a public surface where `QueryError` is the only emit-time exception. `__all__` gains `Cause` and `QueryError`, loses `QueryEmitError` and `UnsupportedQueryError`.

- [ ] **Step 1: Confirm the migration surface**

```bash
rg -o "UnsupportedQueryError|QueryEmitError" tests/ | wc -l   # expect 80
rg -l "UnsupportedQueryError|QueryEmitError" tests/ | wc -l   # expect 13
rg -c "match=" tests/emitter/test_emit_{json,patterns,phrase,ranges,terms}.py  # expect 20 total
```

If these counts differ from 80/13/20, stop and report before editing; the plan's assumptions have drifted.

- [ ] **Step 2: Delete the deprecated classes**

Remove `UnsupportedQueryError` and `QueryEmitError` from `errors.py` entirely.

- [ ] **Step 3: Update the public surface**

In `src/whoosh_compat/__init__.py`, replace the two imports with `Cause`, `QueryError`, `PARSE_KINDS` and `EMIT_KINDS`, and update `__all__` to match. Keep it alphabetized as it is today.

The two frozensets are public on purpose. The spec drops `Diagnostic.phase` because the parse and emit kind sets are disjoint, which means a host that wants the phase must ask which set a kind belongs to. Exporting them is what keeps that from being a hardcoded list on the host side. `__init__.py:1-12` describes the public surface as small, so add one line there noting they exist.

- [ ] **Step 4: Migrate the test references**

For each of the 13 files, replace `pytest.raises(QueryEmitError, match="...")` and `pytest.raises(UnsupportedQueryError, match="...")` with a `QueryError` catch that asserts `kind` and `cause`. Do NOT keep `match=` on message text; that is the coupling this work removes. Pattern:

```python
    # was: with pytest.raises(UnsupportedQueryError, match="text ranges"):
    with pytest.raises(QueryError) as exc:
        emit_ast(parse("title:[a TO b]"), tindex, ereg)
    assert exc.value.diagnostic.kind is DiagnosticKind.TEXT_RANGE
```

`tests/emitter/test_acceptance_property.py:770` catches both as a tuple; collapse it to `QueryError`. Leave `tests/emitter/test_hypothesis_e2e.py:242` alone, Task 9 owns it, but its import must change to `QueryError` for the file to load.

- [ ] **Step 5: Run the full gate**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run mypy tests && uv run pytest tests
```
Expected: green, at or above the 1404 baseline, still 23 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/whoosh_compat/errors.py src/whoosh_compat/__init__.py tests/
git commit -m "refactor!: replace QueryEmitError and UnsupportedQueryError with QueryError"
```

---

### Task 8: Matrix descriptor

**Agent:** `python-pro` | **Model:** opus

**Files:**
- Modify: `tests/emitter/test_kind_matrix.py:94-100`, `:142-148`, and 8 cells at `:282`, `:298`, `:402`, `:417`, `:431`, `:437`, `:460`, `:720`
- Modify: `tests/emitter/test_emit_patterns.py:361-391`

**Interfaces:**
- Consumes: `QueryError`, `DiagnosticKind`, `Cause`, `FieldKind`.
- Produces: `Raises(kind, cause, field_kind=None)` descriptor.

**Why `field_kind` is not optional:** `test_pattern_on_non_text_kind_raises_at_emit` has six cells asserting `match="U64"`/`"DATE"`/`"DATETIME"`. Under one merged `AST_PATTERN_ON_KIND` and one cause, all six collapse to an identical assertion, and collapse further with the subpath and boolean-exists cells: eight discriminating cells reduced to one. The project's sweep convention forbids weakening cells that discriminate correctly today.

- [ ] **Step 1: Rewrite the descriptor**

```python
@dataclasses.dataclass(frozen=True)
class Raises:
    """Outcome 2: a clean parse (no diagnostics) followed by a documented
    emit-time raise.

    Asserts on the structured diagnostic, never on message text: messages
    are log output with no stability guarantee.
    """

    kind: DiagnosticKind
    cause: Cause
    field_kind: FieldKind | None = None
```

- [ ] **Step 2: Rewrite the checker**

Replace `:142-148`:

```python
if isinstance(outcome, Raises):
    assert not r.diagnostics, (
        f"expected a clean parse for {qs!r} (diagnostic deferred to emit), got {r.diagnostics!r}"
    )
    with pytest.raises(QueryError) as exc:
        emit_(r.ast, index=tindex[0], registry=ereg)
    d = exc.value.diagnostic
    assert d.kind is outcome.kind, qs
    assert d.cause is outcome.cause, qs
    if outcome.field_kind is not None:
        assert d.field_kind is outcome.field_kind, qs
    return
```

Also update the `Diag` branch at `:139` from `pytest.raises(QueryEmitError)` to `pytest.raises(QueryError)`.

- [ ] **Step 3: Update the 8 cells**

Each `Raises("...", match="...")` becomes `Raises(DiagnosticKind.X, Cause.Y, FieldKind.Z)`. Derive `X`/`Y` from the inventory table at the top of this plan and `Z` from the field the cell exercises.

- [ ] **Step 4: Restore discrimination in test_emit_patterns.py**

The six cells at `:361-391` currently assert `match="U64"` / `"DATE"` / `"DATETIME"`. Replace with `field_kind` assertions:

```python
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.AST_PATTERN_ON_KIND
    assert d.field_kind is expected_field_kind
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/emitter/test_kind_matrix.py tests/emitter/test_emit_patterns.py -v`
Expected: PASS. Confirm all six pattern cells still discriminate: temporarily change one expected `field_kind` and confirm exactly one test fails.

- [ ] **Step 6: Commit**

```bash
git add tests/emitter/test_kind_matrix.py tests/emitter/test_emit_patterns.py
git commit -m "test: assert structured diagnostics instead of message text in the kind matrix"
```

---

### Task 9: Tighten the fuzzer guard

**Agent:** `python-pro` | **Model:** sonnet

**Files:**
- Modify: `tests/emitter/test_hypothesis_e2e.py:242-243`

**Interfaces:**
- Consumes: `QueryError`, `Cause`.
- Produces: nothing downstream.

**Blocked on Task 4.** Until `PATTERN_TOO_COMPLEX` exists the fuzzer can generate a pattern past tantivy's 1000-state limit and land in `INTERNAL`, which this tightening re-raises. It does not today only because `max_leaves=6` (`:213`) keeps patterns short, which is an accident of the generator's bounds, not an invariant.

- [ ] **Step 1: Implement**

```python
        try:
            emit_(result.ast, index=tindex[0], registry=ereg)
        except QueryError as exc:
            # README documents that the fuzzer never produces anything but
            # documented-unsupported shapes. MISCONFIGURED would mean the
            # test registry is wrong; INTERNAL would mean a real defect.
            if exc.diagnostic.cause is not Cause.UNSUPPORTED:
                raise
```

`MISCONFIGURED` is structurally unreachable for this fuzzer: `_every_atom` (`:182`) builds `field:*` only over `_TEXT_FIELDS + _KEYWORD_FIELDS`, and `_NUM_FIELDS`/`_DATE_FIELDS` (`:48-49`) are `tag_id`, `asn`, `created`, `added`, all `fast=True` in `ereg`.

- [ ] **Step 2: Run with extra examples**

Run: `uv run pytest tests/emitter/test_hypothesis_e2e.py -v -p no:randomly --hypothesis-seed=0`
Expected: PASS. Then run it three more times without a fixed seed to shake out nondeterminism.

- [ ] **Step 3: Commit**

```bash
git add tests/emitter/test_hypothesis_e2e.py
git commit -m "test: fail the fuzzer on any cause but documented-unsupported"
```

---

### Task 10: Documentation sweep

**Agent:** `general-purpose` | **Model:** sonnet

**Files:**
- Modify: `README.md:95-135`, `:333`
- Modify: `ARCHITECTURE.md:254-258`, `:262`, `:279-283`, `:437`, `:513`
- Modify: `CLAUDE.md:58-60`
- Modify: `DIVERGENCES.md` entries 5, 29, 30, plus stale references at `:506`, `:975`, `:1040`, `:1048`, `:1612`
- Modify: `src/whoosh_compat/fields.py:579-589`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the final public surface from Task 7.
- Produces: no code.

- [ ] **Step 1: Rewrite the README host contract**

Delete the "catch **both**" warning at `:102-120`; there is one exception type now. Replace the "Error messages are written for the host" paragraph at `:121-127` with the structured contract: branch on `kind` and `cause`, treat `message` as log output, never parse it. Document the `Cause` routing table (`INVALID_INPUT`/`UNSUPPORTED` to 400, `MISCONFIGURED` to an operator alert, `INTERNAL` to 500). Restate `:333`'s fuzzer invariant in terms of `Cause.UNSUPPORTED`.

- [ ] **Step 2: Fix the five ARCHITECTURE.md references**

`:254-258` documents the **positional** `Diagnostic(message, kind, startchar, endchar, field, raw_value)` signature and lists the old four enum members literally; both are now wrong. `:262` names "the `UNSUPPORTED_PATTERN` site". `:279-283` describes the two exception types. `:437` restates the invariant. `:513` describes `UNSUPPORTED_PATTERN` diagnostics built from Wildcard/Prefix.

Add one sentence noting that `tests/differential/allowlist.py`'s `DivergenceKind` is unrelated to `Diagnostic.divergence`, since "divergence" now names three concepts in this repo.

- [ ] **Step 3: Fix CLAUDE.md**

`:58-60` reads "only `emit()` raises (`QueryEmitError`, `UnsupportedQueryError`)". Replace with `QueryError`.

- [ ] **Step 4: Fix DIVERGENCES.md**

Add to entries 5, 29 and 30 a note that they are machine-identifiable via `Diagnostic.divergence`. For entry 5, record that the reference applies to TEXT and KEYWORD ranges; a JSON subpath range reports entry 30, and a BOOLEAN_EXISTS range reports no entry, because neither worked in whoosh. Then fix the five stale references: `:506`, `:1048`, `:1612` name the removed exception classes; `:975` and `:1040` name `DiagnosticKind.UNSUPPORTED_PATTERN`.

- [ ] **Step 5: Fix the fields.py docstring**

`:579-589` describes the two-part host contract using both removed exception names. Rewrite for `QueryError`, keeping the substantive point that a clean parse does not guarantee a successful emit.

- [ ] **Step 6: Add the CHANGELOG entry**

Under a new `0.2.0` heading, a **Breaking** section: `QueryEmitError` and `UnsupportedQueryError` are replaced by `QueryError`, which always carries a `Diagnostic`; `Diagnostic` is keyword-only and gains `cause`, `field_kind` and `divergence`; `DiagnosticKind.UNSUPPORTED_PATTERN` splits into `PATTERN_ON_NUMERIC`, `PATTERN_ON_BOOLEAN_EXISTS` and `PATTERN_ON_SUBPATH`.

- [ ] **Step 7: Verify no stale references remain**

```bash
rg -n "QueryEmitError|UnsupportedQueryError|UNSUPPORTED_PATTERN" README.md ARCHITECTURE.md CLAUDE.md DIVERGENCES.md CHANGELOG.md src/
rg -n "—" README.md ARCHITECTURE.md CLAUDE.md DIVERGENCES.md CHANGELOG.md
```
Expected: no output from either command.

- [ ] **Step 8: Commit**

```bash
git add README.md ARCHITECTURE.md CLAUDE.md DIVERGENCES.md CHANGELOG.md src/whoosh_compat/fields.py
git commit -m "docs: describe the structured diagnostic contract"
```

---

## Final verification

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy src && uv run mypy tests`
- [ ] `uv run pytest tests` at or above 1404 passed, 23 skipped
- [ ] `uv run pytest tests/differential -rs` shows the same 23 attributed skips
- [ ] `uv pip install "tantivy~=0.26.0" && uv run pytest tests/emitter` passes on paperless-ngx's pin
- [ ] `uvx prek run --all-files`
- [ ] `rg -n "DIVERGENCES" src/whoosh_compat/emitters/tantivy_.py` shows no hit inside a message string
