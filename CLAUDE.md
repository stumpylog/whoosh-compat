# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

`ARCHITECTURE.md` (pipeline, module provenance, invariants) and `DIVERGENCES.md`
(the numbered list of every intentional behavioral difference from real Whoosh)
are the authoritative design docs. Read the relevant sections before changing
parser or emitter behavior — most non-obvious code here is explained in one of
them, and both are expected to be updated alongside behavior changes.

## Commands

```bash
uv sync --group dev              # install (uv-managed; the venv is .venv)

uv run ruff check .              # lint
uv run ruff format --check .     # format check (parser/ forks are excluded, see pyproject)
uv run mypy src                  # type check
uv run pytest tests              # full suite
uv run pytest tests --cov --cov-branch --cov-report=term-missing

uv run pytest tests/differential -rs          # differential layer; -rs shows attributed skips
uv run pytest tests/emitter/test_emit_ranges.py::test_name   # single test
uvx prek run --all-files         # pre-commit hooks (or `uvx pre-commit run --all-files`)
```

CI additionally runs `pytest tests/emitter` against `tantivy~=0.26.0`
(the `tantivy-pin` job) because that is paperless-ngx's pin — emitter changes
must work on that version, not just the lockfile's.

## Architecture in brief

`parse()` → forked whoosh tagger/filter pipeline → frozen AST → `normalize()`
→ emitter visitor → `tantivy.Query`. `parse()` and `emit()` are deliberately
separate steps, and `whoosh_compat.ast` / `whoosh_compat.fields` are the entire
contract between them.

- `parser/` is a **fork** of whoosh's `qparser` (plus `util/times.py`), kept
  close to verbatim so it stays diffable against upstream; only the *output* of
  the pipeline was replaced (AST nodes instead of `whoosh.query` objects).
  These files are excluded from `ruff format` on purpose — do not reformat them,
  and keep edits minimal and localized. Forked files keep their original
  BSD-2-Clause headers.
- `parser/priorities.py` is not forked: it centralizes the tagger/filter
  priority constants that whoosh inlined per plugin. Ordering here is
  load-bearing for leniency semantics.
- `emitters/tantivy_.py` is the only module that may import `tantivy`.
  Everything else (AST, parser, fields) stays backend-neutral so a future
  emitter needs no changes upstream of it.
- Queries are always built programmatically. The single exception is the JSON
  subpath fallback through `index.parse_query()` — do not add another.

## Invariants worth knowing before editing

- **Parsing never raises for bad query input.** Bad dates/numbers become
  `Diagnostic`s plus `ErrorLeaf` nodes; only `emit()` raises (`QueryEmitError`,
  `UnsupportedQueryError`). Registry construction *does* raise eagerly.
- **`analyzer` vs `pattern_normalizer` are two different callables on purpose**
  (full token chain vs. character-level only for wildcard/prefix literals).
  Don't unify them; see README's "analyzer / pattern_normalizer seam".
- **Analysis happens at emit time, not parse time** — `Term`/`Phrase` nodes
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
AST-level divergence does not imply a result-level one — check before treating
one as a bug.

Two project skills cover the recurring judgment calls; invoke them rather than
improvising:

- `differential-triage` — any differential/corpus mismatch. The parity bar is
  whoosh's *intended* semantics, never its defects; never change whoosh-compat
  to reproduce a whoosh bug, never delete or weaken a corpus line to make tests
  pass, and every non-fix classification needs all three of: allowlist entry,
  `DIVERGENCES.md` entry, retained corpus line.
- `carve-out-retirement` — any tantivy/tantivy-py version bump or removal of a
  compatibility workaround. The three carve-outs (JSON `parse_query` fallback,
  `_to_naive_utc`, all-MustNot padding) have independent retirement conditions.

The real `whoosh` package is a **test-only** dependency, git-ref pinned in the
`dev` group (the fork carries parser fixes absent from PyPI 2.7.4). It must
never become a runtime dependency.

## Downstream

paperless-ngx is the motivating consumer. Its tantivy pin (`~=0.26.0`) and its
query corpus (`tests/differential/corpus_paperless.txt`) constrain what can
change here; raising the tantivy floor requires coordinating downstream first.
