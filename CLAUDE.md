# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

`ARCHITECTURE.md` (pipeline, module provenance, invariants) and `DIVERGENCES.md`
(the numbered list of every intentional behavioral difference from real Whoosh)
are the authoritative design docs. Read the relevant sections before changing
parser or emitter behavior, since most non-obvious code here is explained in one of
them, and both are expected to be updated alongside behavior changes.

## Commands

```bash
uv sync --group dev              # install (uv-managed; the venv is .venv)

uv run ruff check .              # lint
uv run ruff format --check .     # format check (parser/ forks are excluded, see pyproject)
uv run mypy src                  # type check
uv run mypy tests                # type check the test suite
uv run pytest tests              # full suite
uv run pytest tests --cov --cov-branch --cov-report=term-missing

uv run pytest tests/differential -rs          # differential layer; -rs shows attributed skips
uv run pytest tests/emitter/test_emit_ranges.py::test_name   # single test
uvx prek run --all-files         # pre-commit hooks (or `uvx pre-commit run --all-files`)
```

CI additionally runs `pytest tests/emitter` against `tantivy~=0.26.0`
(the `tantivy-pin` job) because that is paperless-ngx's pin; emitter changes
must work on that version, not just the lockfile's.

## Architecture in brief

`parse()` → forked whoosh tagger/filter pipeline → frozen AST → `normalize()`
→ emitter visitor → `tantivy.Query`. `parse()` and `emit()` are deliberately
separate steps, and `whoosh_compat.ast` / `whoosh_compat.fields` are the entire
contract between them.

- `parser/` is a **fork** of whoosh's `qparser` (plus `util/times.py`), kept
  close to verbatim so it stays diffable against upstream; only the *output* of
  the pipeline was replaced (AST nodes instead of `whoosh.query` objects).
  These files are excluded from `ruff format` on purpose: do not reformat them,
  and keep edits minimal and localized. Forked files keep their original
  BSD-2-Clause headers.
- `parser/priorities.py` is not forked: it centralizes the tagger/filter
  priority constants that whoosh inlined per plugin. Ordering here is
  load-bearing for leniency semantics.
- `emitters/tantivy_.py` is the only module that may import `tantivy`.
  Everything else (AST, parser, fields) stays backend-neutral so a future
  emitter needs no changes upstream of it.
- Queries are always built programmatically. The single exception is the JSON
  subpath fallback through `index.parse_query()`; do not add another.

## Invariants worth knowing before editing

- **Parsing never raises for bad query input.** Bad dates/numbers become
  `Diagnostic`s plus `ErrorLeaf` nodes; on the query path it is `emit()` that
  raises, and its `QueryError` always carries a `Diagnostic`. Registry
  construction *does* raise eagerly.
  Enforced, not merely intended: `parse()` wraps the pipeline in a backstop that
  converts any unexpected exception into `QueryParserError` (chaining the
  original), so the only exception a caller ever handles means "a library
  defect" and hosts route it to a monitorable 500. `emit()`'s backstop is
  deliberately *not* the same blanket `except Exception`: it converts a
  named allowlist (see its docstring), so anything outside that list -
  `_translate_class`'s `AssertionError`, for one - still escapes as itself.
  Don't downgrade it to a
  `Diagnostic` (that would blame the user for a bug and hide it from
  monitoring), and don't treat it as a licence to skip fixing a root cause:
  `tests/test_parse_never_raises.py` is where new escape routes get pinned.
- **`analyzer` vs `pattern_normalizer` are two different callables on purpose**
  (full token chain vs. character-level only for wildcard/prefix literals).
  Don't unify them; see README's "analyzer / pattern_normalizer seam".
- **Analysis happens at emit time, not parse time**: `Term`/`Phrase` nodes
  carry raw text.
- **All-`MustNot` boolean groups need padding** at every nesting level
  (`_pad_if_all_negative`), working around an unfixed tantivy issue.
- **`DateRange` bounds are tz-aware UTC in the AST**, converted to naive UTC
  only at the `range_query` call site. Period dates use a half-open exclusive
  upper bound.

## Working with the test layers

Three layers answer different questions: unit (`tests/`), differential
(`tests/differential/`, AST compared against a pinned real-Whoosh oracle), and
end-to-end acceptance (`tests/emitter/test_acceptance_e2e.py`, matched-document
ID sets compared across a real Whoosh index and tantivy). An allowlisted
AST-level divergence does not imply a result-level one; check before treating
one as a bug.

Two project skills cover the recurring judgment calls; invoke them rather than
improvising:

- `differential-triage`: any differential/corpus mismatch. The parity bar is
  whoosh's *intended* semantics, never its defects; never change whoosh-compat
  to reproduce a whoosh bug, never delete or weaken a corpus line to make tests
  pass, and every non-fix classification needs all three of: allowlist entry,
  `DIVERGENCES.md` entry, retained corpus line.
- `carve-out-retirement`: any tantivy/tantivy-py version bump or removal of a
  compatibility workaround. The four carve-outs (JSON `parse_query` fallback,
  `_to_naive_utc`, all-MustNot padding, the date-window clamp) have
  independent retirement conditions.

The real `whoosh` package is a **test-only** dependency, git-ref pinned in the
`dev` group (the fork carries parser fixes absent from PyPI 2.7.4). It must
never become a runtime dependency.

## Conventions

Non-negotiable, and not all of them are visible from the surrounding code:

- **Test-driven for behavior changes.** Write the failing test, run it, confirm
  it fails for the reason you expect, then implement. Report that evidence when
  handing work back. A behavior claim needs a test that executes a real search
  and asserts document sets, not one that asserts a query object was built
  without raising.
- **Parametrized cases use `pytest.param(..., id="descriptive-name")`.** Never
  bare tuples; the autogenerated ids are unreadable in CI output and make `-k`
  selection guesswork.
- **No em dashes** in any prose: code comments, docstrings, Markdown, commit
  messages. Use commas, colons, parentheses or separate sentences.
- **No project-process vocabulary in shipped artifacts.** Issue or task
  numbers, review or fix-round framing, and planning references do not belong
  in code, comments, docs or commit messages. Comments explain the code;
  commit messages describe the change. Cross-references to `DIVERGENCES.md`
  entries by number are fine, since those are permanent and reader-facing.
- **Stage with explicit paths** (`git add path/to/file`), never `git add -A` or
  `git add .`; unrelated files have been swept into commits that way.
- **Verify claims, especially about whoosh.** The oracle is checked out and
  runnable. Assertions about what whoosh does have been confidently wrong in
  both directions here; measure against it rather than reasoning from memory,
  and say which you did.
- **Sweep the sibling cells.** The dominant recurring defect class in this
  codebase is a rule implemented for one combination of AST leaf type, field
  kind, and value spelling (bare, single-quoted, double-quoted) but forgotten
  for its siblings: the same check landing in `visit_term` but not
  `visit_phrase`, applying to U64 but not BOOLEAN_EXISTS, handling plain
  fields but not JSON subpaths. Any behavior change scoped by node type or
  field kind must enumerate the whole row and column it touches, and every
  cell must end in exactly one of three outcomes: a parse-time diagnostic, a
  documented emit-time error, or a real search that honors the kind and
  subpath. Silent fallthrough to TEXT-shaped behavior is never acceptable for
  a non-TEXT cell. Where the kind/spelling exhaustiveness matrix test exists,
  extend it for every new cell; never carve exceptions out of it.
- **A deliberate divergence lands with its paperwork.** The triple from the
  `differential-triage` skill (allowlist entry, `DIVERGENCES.md` entry,
  retained corpus line) applies when *introducing* an intentional deviation
  from whoosh, not only when triaging a failing test. A change that knowingly
  deviates ships all three in the same commit; a divergence that exists only
  in a commit message or code comment does not count as documented.

## Downstream

paperless-ngx is the motivating consumer. Its tantivy pin (`~=0.26.0`) and its
query corpus (`tests/differential/corpus_paperless.txt`) constrain what can
change here; raising the tantivy floor requires coordinating downstream first.
