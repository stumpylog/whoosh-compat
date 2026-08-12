# whoosh-compat

A standalone, typed, Python 3.11+ library that parses the
[Whoosh](https://github.com/whoosh-community/whoosh) query language into a
backend-neutral AST and emits **programmatically constructed**
[`tantivy.Query`](https://github.com/quickwit-oss/tantivy-py) objects: never
via tantivy's own string query parser.

It exists so that applications which used to build Whoosh query objects
directly (or hand-translate Whoosh-style query strings into another engine's
query language) can keep their existing, lenient, natural-language search
syntax while running on [tantivy](https://github.com/quickwit-oss/tantivy).
The motivating case is [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx),
which migrated its search backend from Whoosh to tantivy but kept Whoosh's
query syntax as its user-facing search language. See
[paperless-ngx#13568](https://github.com/paperless-ngx/paperless-ngx/issues/13568)
for a concrete example of a query (`title:202[0-3]*`, a bracket-class
wildcard) that a naive string-translation layer gets wrong.

## Installation

```bash
pip install whoosh-compat[tantivy]
```

The core package (`pip install whoosh-compat`) depends only on
`python-dateutil`. The `tantivy` extra pulls in `tantivy-py`, which is
required to use `whoosh_compat.emitters.tantivy_`. The AST itself
(`whoosh_compat.ast`) and the parser (`whoosh_compat.parse`) have no tantivy
dependency, so a future backend (e.g. Meilisearch) can reuse them.

## Usage

```python
import tantivy

import whoosh_compat as wc
from whoosh_compat.emitters.tantivy_ import emit

# 1. Describe the fields the parser and emitter need to know about.
registry = wc.FieldRegistry(
    [
        wc.FieldSpec("content", wc.FieldKind.TEXT, analyzer=str.split),
        wc.FieldSpec("tag", wc.FieldKind.KEYWORD, comma_values=True, analyzer=str.split),
        wc.FieldSpec("created", wc.FieldKind.DATE, date_only=True, fast=True),
    ]
)

# 2. Parse a whoosh-style query string into a backend-neutral AST.
result = wc.parse(
    "tag:steuer AND created:[2020 TO 2020]",
    registry=registry,
    default_fields=["content"],
)
result.ast  # normalized AST root
result.diagnostics  # tuple[Diagnostic, ...], e.g. invalid dates

# 3. Emit a tantivy.Query against a real index and search it.
query = emit(result.ast, index=index, registry=registry)
searcher.search(query, limit=10)
```

`FieldSpec.analyzer` is how index-time tokenization parity is achieved: it's
a plain `Callable[[str], list[str]]` the host supplies (e.g.
`tantivy.TextAnalyzer.analyze`), called at *emit* time on `Term`/`Phrase`
text. tantivy's `term_query` does not tokenize on its own, so skipping this
step means a query for `"Invoices"` silently fails to match an index that
stored the lowercased token `"invoices"`.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full `FieldSpec`/
`FieldRegistry` shape and how a query string gets from string to
`tantivy.Query`.

## Supported query syntax

Parity target is **Whoosh's intended grammar**, not every Whoosh plugin.
See `ARCHITECTURE.md` for why the parser is a fork rather than a
reimplementation, and `DIVERGENCES.md` for every point where whoosh-compat's
behavior intentionally differs from real Whoosh.

| Syntax | Example | Notes |
|---|---|---|
| Bare terms, implicit AND | `invoice total` | both terms required (Whoosh's own semantics) |
| Boolean operators (uppercase only) | `a AND b`, `a OR b`, `NOT a`, `a ANDNOT b`, `a ANDMAYBE b`, `a REQUIRE b` | lowercase `and`/`or`/`not` are plain text, matching Whoosh's operator regexes; `REQUIRE` is infix like the others |
| Grouping | `(a OR b) AND c` | |
| Fielded terms | `title:invoice` | |
| Field aliases | `type:invoice` → `document_type:invoice` | host-configured on `FieldSpec.aliases` |
| Phrases | `"exact phrase"`, `"a b"~2` | slop follows Whoosh semantics: `1` = adjacent |
| Wildcards | `inv*`, `inv?ice`, `202[0-3]*` | glob syntax including bracket character classes |
| Prefix | `inv*` (no other wildcard chars) | folds to a prefix query |
| Ranges | `created:[2020 TO 2025]`, `asn:[100 to]` | numeric and date; open-ended on either side |
| Boost | `title:invoice^2.0` | |
| Comma value lists | `tag:foo,bar` → `tag:foo AND tag:bar` | per-field opt-in (`FieldSpec.comma_values`); `tag:'foo,bar'` quoting keeps it a single literal |
| Every / exists | `*`, `*:*`, `title:*` | |
| Dates | `created:2020`, `created:today`, `created:'previous month'`, `created:now-7d` | full grammar: ISO/compact forms, natural-language keywords, relative offsets (`now-7d`, `-1 week`); multi-word keywords need quoting, see `DIVERGENCES.md` entry 19 |
| JSON subpaths | `notes.user:alice` | an extension with no equivalent in Whoosh itself (registered per-field via `FieldSpec.subpaths`) |

Not carried over from Whoosh (not currently implemented, kept cheap to add
via the forked plugin architecture): `asn:>100` (`GtLtPlugin`), `term~2`
fuzzy matching, `r"regex"` literal regex queries, `SequencePlugin`,
`-foo`/`+foo` as negation/requirement shorthand (in the whoosh grammar this
library targets, `-foo` was plain text whose analyzer typically dropped the
dash: `NOT` was the only negation operator), and free-date mode (implicit
date parsing in an unfielded-date context; the parser defaults to the
date-parsing plugin for fielded dates when the host calls `parse()` with a
date-aware registry instead).

### Divergences from real Whoosh

**AST-level divergence does not always mean result-level divergence.**
whoosh-compat is tested at two separate layers. See
[`DIVERGENCES.md`](./DIVERGENCES.md) entry 16 for the full explanation, and
`tests/emitter/test_acceptance_e2e.py`'s module docstring for worked
examples: a documented, real difference in the *parsed AST* between
whoosh-compat and real Whoosh (e.g. how a wildcard pattern's case-folding is
sequenced) can still produce the *same final matched-document set*, because
the divergence gets absorbed somewhere downstream (e.g. both sides' text
analyzers end up lowercasing the same way regardless). Read
`DIVERGENCES.md` for what's intentionally different and why; don't assume an
entry there implies a query result bug without checking whether it's one of
the entries called out as AST-only.

### The analyzer / pattern_normalizer seam

Two separate callables on `FieldSpec`, deliberately not unified into one:

- **`analyzer`** (`Callable[[str], list[str]]`): the *full* token-level
  chain (lowercase, ASCII-fold, stemming, stopword removal, whatever the
  host's index-time analyzer does) applied to `Term`/`Phrase` query text at
  emit time, so query tokens match what's actually in the index.
- **`pattern_normalizer`** (`Callable[[str], str]`): a *lighter*,
  character-level-only transform (lowercase + ASCII-fold, no tokenization,
  **never stemming**) applied to the literal segments of `Wildcard`/`Prefix`
  patterns.

These have to be different callables: index terms for a stemmed field are
themselves stems, but running a stemmer over a wildcard's literal prefix
(e.g. the `entwä` in `Entwä*`) would mangle it before the glob/prefix match
even runs. This means **wildcards over a stemmed index inherit the same
caveat Whoosh itself always had**: a wildcard pattern matches raw (folded,
not stemmed) index terms, so `run*` will not match a document whose only
related token was stemmed down to `ran`.

### Timezone handling

`DateRange` bounds inside the AST are always timezone-aware UTC `datetime`s.
The tantivy emitter converts them to **naive UTC** before calling
`Query.range_query` (see `_to_naive_utc` in
`src/whoosh_compat/emitters/tantivy_.py`), because `tantivy-py <= 0.26.0`'s
`range_query` only accepts naive datetimes for `FieldType.Date`. Passing a
tz-aware one raises `ValueError`. This is worked around here rather than
relied on upstream because the fix
([tantivy-py#666](https://github.com/quickwit-oss/tantivy-py/pull/666))
merged *after* 0.26.0 was tagged. Naive input is passed straight through
(tantivy already treats naive datetimes as UTC at index time, matching how
documents are indexed).

### The JSON subpath carve-out

`notes.user:alice`-style dotted-field queries (`FieldKind.JSON`) are the one
place this library emits through `index.parse_query()` instead of
constructing a `tantivy.Query` programmatically: the installed `tantivy-py`'s
`Query.term_query` cannot address a JSON subpath by exact field name: it
raises as if the field didn't exist at all. The emitter feature-detects this
per process and falls back to a strictly escaped/quoted `parse_query` call
for just that one leaf. This carve-out retires itself automatically once
[tantivy-py#716](https://github.com/quickwit-oss/tantivy-py/pull/716) (which
routes `make_term` through JSON path resolution) lands and ships. No code
change is needed here, the feature-detection just starts taking the other
branch.

## Development

Install with dev dependencies (uses [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --group dev
```

Run the checks CI runs:

```bash
uv run ruff check .
uv run mypy src
uv run pytest tests
```

This repo also ships a [`.pre-commit-config.yaml`](./.pre-commit-config.yaml)
covering the cheap, mechanical checks (whitespace, YAML/TOML validity,
codespell, `zizmor` for the GitHub Actions workflow, `ruff check`, keeping
`uv.lock` in sync). Run it with [prek](https://github.com/j178/prek) (a
faster, dependency-free reimplementation of `pre-commit` that reads the
same config file) or `pre-commit` itself:

```bash
uvx prek run --all-files
# or: uvx pre-commit run --all-files
```

### Testing layers

1. **Unit tests** (`tests/`): parser, AST, `normalize()`, and `FieldSpec`/
   `FieldRegistry` behavior in isolation.
2. **Differential tests** (`tests/differential/`): parse the same corpus of
   query strings through both whoosh-compat and a **real, pinned Whoosh**
   (a test-only dependency; see the git-ref pin in `pyproject.toml`'s
   `dev` group, this fork carries parser fixes absent from the PyPI
   release) and compare the resulting trees. Divergences must match an
   explicit allowlist (`tests/differential/allowlist.py`), each entry cross-
   referencing a `DIVERGENCES.md` entry for *why* it's expected.
3. **End-to-end acceptance tests** (`tests/emitter/test_acceptance_e2e.py`):
   the same fixture documents indexed twice (once in a real Whoosh index,
   once in tantivy), full query strings run against both, and the *matched
   document ID sets* compared: result-level parity, not tree shape.

Because layer 2 needs a real Whoosh installation as an oracle, it's pulled
in as a dev-only dependency (pinned by git ref, not the PyPI 2.7.4 release)
rather than a runtime dependency of the library itself.

### Property-based / fuzz testing

`tests/differential/strategies.py` is a grammar-aware [Hypothesis](https://hypothesis.readthedocs.io/)
strategy covering the whole supported query language (README's syntax
table above): nested groups, every operator, wildcards/ranges/phrases/
comma-lists/boosts/JSON subpaths, and deliberately placed zero-token
values (an all-stopword term/phrase, see `strategies.ZERO_TOKEN_WORDS`).
It drives four properties:

- `tests/differential/test_hypothesis.py::test_fuzz_grammar_matches_oracle`:
  the same AST-shape parity check as the static corpus (layer 2 above), but
  over generated, nested queries, guided by `hypothesis.target()` toward
  structurally rich examples (more nodes, deeper nesting, more distinct
  node types, more zero-token leaves buried inside a larger structure).
  Seeded with every static corpus line plus a few strings pulled directly
  from `DIVERGENCES.md` entries, via `@example()`.
- `tests/differential/test_hypothesis.py::test_normalize_is_total_and_idempotent`:
  `normalize(normalize(x)) == normalize(x)` for every freshly parsed AST,
  and `normalize()` never raises.
- `tests/emitter/test_hypothesis_e2e.py::test_emit_never_raises_except_unsupported`:
  parsing a query that produced no diagnostics, then emitting it against a
  real in-memory tantivy index, never raises anything except the
  documented `UnsupportedQueryError`.
- `tests/emitter/test_hypothesis_e2e.py::test_normalize_idempotent_on_emitter_registry_grammar`:
  the same `normalize()` property again, against the emitter registry's
  own (smaller, JSON/BOOLEAN_EXISTS-carrying) field vocabulary.

These run at a modest `max_examples` in CI so the suite stays fast. To run
a longer local soak (recommended before a release, or after touching
`parser/`, `ast.py`, or `emitters/`), raise the example count for a single
run without editing the files:

```bash
uv run python - <<'EOF'
import hypothesis
hypothesis.settings.register_profile("soak", max_examples=5000, deadline=None)
hypothesis.settings.load_profile("soak")
import pytest
raise SystemExit(pytest.main(["tests/differential/test_hypothesis.py", "tests/emitter/test_hypothesis_e2e.py", "-q"]))
EOF
```

or edit the `max_examples=300` values in those two files directly for the
duration of the run. Keep long soaks (thousands of examples) **out of
CI**: they take minutes, not seconds, and are meant for local/pre-release
verification, not every push.

**HypoFuzz** (coverage-guided fuzzing that runs existing Hypothesis
properties under instrumentation) was evaluated for this purpose and is
**not used**: its license (`Zac-HD/hypofuzz`'s `LICENSE`, checked directly)
grants use "for non-commercial purposes only", explicitly requires a
separate paid commercial license for "use within a commercial
organization, including internal tooling or testing" and "use in
continuous integration or development pipelines for commercial products",
and prohibits modification/redistribution without permission. That's
incompatible with this BSD-2-Clause project (whoosh-compat is itself used
by, and expected to be run in CI by, commercial downstream users like
paperless-ngx installs), so it was not added as a dependency. The plain-
Hypothesis soak profile above is the recommended way to get a similar
"run longer, look harder" effect without it.

## License

BSD-2-Clause, see [LICENSE](./LICENSE). This project's parser
(`whoosh_compat/parser/`) is forked from
[whoosh-community/whoosh](https://github.com/whoosh-community/whoosh) (a
fork of Matt Chaput's original [Whoosh](https://github.com/mchaput/whoosh),
also on [PyPI](https://pypi.org/project/Whoosh/); the whoosh-community fork
is itself unmaintained). Forked files retain their original BSD-2-Clause
header. See [NOTICE](./NOTICE).
