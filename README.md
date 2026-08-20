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

## API Stability

The public API boundary, as of 0.1.0:

**Stable API** (guaranteed across minor and patch releases):

- `whoosh_compat.parse()` and `whoosh_compat.ParseResult`
- `whoosh_compat.ast` module (the backend-neutral query tree), including
  `whoosh_compat.free_text_tokens()` (also exported at top level)
- `whoosh_compat.fields` module (`FieldSpec`, `FieldRegistry`, `FieldKind`, etc.)
- `whoosh_compat.errors` module (exception types)
- `whoosh_compat.emitters.tantivy_.emit()` function and the `Emitter` protocol in `whoosh_compat.emitters.base`

**Internal / not stable** (usable, but subject to change without notice between versions):

- `whoosh_compat.parser.*`: the forked whoosh tagger and filter pipeline. The parser is a fork of whoosh's own query parser, kept close to upstream so it stays diffable and easy to maintain. Because it tracks a third-party codebase, its internals and behavior may change between whoosh-compat releases, even minor ones.

The `parser.*` exemption forecloses nothing: it can always be promoted to stable later. In the meantime, if you import directly from `whoosh_compat.parser`, your code may need updates on whoosh-compat releases.

### Module naming: why `emitters.tantivy_`?

The `emitters.tantivy_` module uses a trailing underscore to signal that this is a backend-specific module. This naming is deliberate and permanent at the 0.1.0 release. While a future version could add a lazy re-export (e.g., `emitters.tantivy` without the underscore) to provide an alternative import path, the canonical import is and will remain `from whoosh_compat.emitters.tantivy_ import emit`. The underscore also reinforces that tantivy is an optional dependency: the emitter won't be imported unless you ask for it.

### The host contract

A host embedding this library (mapping failures to an HTTP 400, for
example) needs to check for **two** independent failure modes, not one:

1. **`ParseResult.diagnostics` is non-empty.** `parse()` never raises for
   bad *query* input; a malformed date, an out-of-domain number, or other
   field-kind-specific problem becomes a `Diagnostic` plus an `ErrorLeaf` in
   the tree instead. Calling `emit()` on a tree containing an `ErrorLeaf`
   raises `QueryError`.
2. **`emit()` raises `QueryError`.** This can happen even when
   `diagnostics` is empty: some query shapes parse cleanly but have no way
   to execute against tantivy today. The canonical example is a text-field
   range, `title:[a TO b]`: whoosh supported this, but tantivy-py has no
   programmatic text-range API (`DIVERGENCES.md` entry 5), so the query
   parses with `diagnostics == ()` and only fails once `emit()` is called.
   There is a single exception type now: `QueryError` always carries a
   `Diagnostic` (`err.diagnostic`) describing why.

**An empty `diagnostics` tuple does not, by itself, mean emitting is safe.**
Both checks matter; a host that only looks at `diagnostics` will still see
an uncaught `QueryError` bubble up for shapes like the one above.

**The one exception `parse()` itself can raise is `QueryParserError`, and it
never means the query was bad.** It means a defect in this library: the parse
pipeline is wrapped in a backstop that converts any unexpected exception into
that type, chaining the original as `__cause__`, so a host routes it to a
monitorable 500 instead of seeing (for example) a bare `RecursionError` from a
pathologically nested query. It is deliberately not a `Diagnostic`: reporting
an unknown internal failure as a 400 would blame the user for a bug on this
side and hide it from monitoring. (Misconfiguration passed to `parse()` --
an empty or unknown `default_fields`, a naive `basedate` -- still raises
`ValueError` eagerly, as documented on the function.)

**Branch on `Diagnostic.kind` and `Diagnostic.cause`; treat `message` as
log output, never parse it.** Both `ParseResult.diagnostics` entries and a
caught `QueryError`'s `.diagnostic` are structured records: `kind` is a
stable `DiagnosticKind` member a host can switch on (for example, mapping
`BAD_DATE` to a typed `InvalidDateQuery`), and `cause` is a coarser `Cause`
a host can use for routing without knowing every `DiagnosticKind`:

| `Cause`         | Meaning                                    | Typical host response       |
| --------------- | ------------------------------------------- | ---------------------------- |
| `INVALID_INPUT` | The query text itself is malformed          | HTTP 400                     |
| `UNSUPPORTED`   | The query is well-formed but this backend can't run it | HTTP 400        |
| `MISCONFIGURED` | The registry/schema setup is wrong          | Operator alert **and** HTTP 400 |
| `INTERNAL`      | The AST violates an invariant `parse()` would never produce | HTTP 500 |

`MISCONFIGURED` is the one cause that is two responses rather than one. It
means the registry and the index schema disagree, which only an operator can
fix, so it must raise an alert. But every `MISCONFIGURED` kind is reachable
from ordinary query text (`notes.user:*` for `EXISTS_REQUIRES_FAST`, and any
query naming a field the registry knows and the index schema does not for
`SCHEMA_FIELD_MISSING`), so a request is waiting on an answer that the alert
does not provide. The query cannot run whether or not anyone reads the
alert, and reporting it as a 500 would claim a defect in this library that
isn't there, so the request gets a 400.

`SCHEMA_FIELD_MISSING` is reported uniformly by every leaf that queries a
resolved field (term, phrase, prefix, wildcard, numeric and date range,
bare-`*` existence, and JSON subpaths): the drift is a property of the
field, not of the spelling that reaches it, so `content:x` and `content:x*`
never land on opposite sides of the 400/500 line for the same broken
deployment. Only the confirmed missing-field condition is reclassified; any
other refusal from tantivy-py remains `BACKEND_REJECTED`/`INTERNAL`, so a
genuine defect in this library is never hidden behind a 400.

`Diagnostic.message` (and the `QueryError` exception message, which is the
same string) carries no stability guarantee and may reword without notice.
Everything a host needs to act on is on the record's own fields instead: a
`DIVERGENCES.md` entry number on `divergence`, the field and its kind on
`field`/`field_kind`, the offending literal on `raw_value`, and the span in
the query string on `startchar`/`endchar`.
Treat the message as developer/log output: a host showing errors to end
users should build its own copy from `kind`/`cause`, not display or parse
`message`; the paperless-ngx integration does exactly this.

A `Diagnostic`'s severity is fatal-only, and always will be: there is no
`severity` field, and none is planned. Any diagnostics present means the
query cannot be emitted, full stop; there is no "warning" tier to weigh
differently. A future informational-only signal (for example, reporting
that a zero-token term was silently dropped during analysis) would use a
separate channel, never `ParseResult.diagnostics`.

### Free-text tokens for secondary clauses

`whoosh_compat.free_text_tokens(node, registry=..., fields=...)` answers
"which plain words does this query search for?" for hosts that blend a
secondary text clause alongside the emitted query: the motivating case is
paperless-ngx's fuzzy-matching blend, which re-parses a word string
through tantivy's own query parser and must never receive whoosh grammar.
It returns the analyzed `Term`/`Phrase` tokens on the requested
TEXT/KEYWORD fields, deduplicated in first-appearance order, with the
subtle rules handled here rather than in each host: negated subtrees
(`NOT x`, `ANDNOT`'s negative side) contribute nothing, patterns and
ranges contribute nothing, and a word the multifield expansion copied
onto several default fields counts once. Tokens are the field analyzer's
output verbatim, never re-split; see the function's docstring for the
full contract.

**Cap query length at the host boundary.** Parse time is quadratic in the
length of a long run of word characters containing no `:` (the fieldname
tagger's regex, `[\w.]+:`, scans toward end-of-input and fails at each
successive position). Measured: a 40KB pathological query takes ~6
seconds, 60KB ~14 seconds, ~4x per doubling. This is exact parity with
real whoosh (byte-identical timings against the pinned oracle), inherited
deliberately rather than fixed with a rewritten tagger regex whose subtle
behavior differences would risk parity. The parser's own nesting-depth cap
bounds recursion, not CPU time, so a host accepting untrusted query
strings should enforce its own length limit (a few KB comfortably covers
any human-written query) before calling `parse()`.

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
| RFC3339 datetimes | `created:[2020-01-01T00:00:00Z TO 2020-06-01T00:00:00Z]`, `created:'2020-01-01T00:00:00Z'` | `T` joins the separator class and a trailing `Z` is honored as the UTC designator (an absolute instant, not local time); an extension over Whoosh, which cannot parse these correctly (quoted `T`/`Z` values parse to nothing; range bounds collapse to their leading year), see `DIVERGENCES.md` entries 12 and 48-50 |
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
- `tests/test_parse_never_raises.py::test_parse_raises_nothing_but_query_parser_error`:
  over the same generated grammar (drawn deeper than the shared strategy's
  committed default), `parse()` raises nothing but `QueryParserError`, the
  documented "library defect" type. Sits alongside regression anchors for
  every escape route found so far.
- `tests/emitter/test_hypothesis_e2e.py::test_emit_never_raises_except_unsupported`:
  parsing a query that produced no diagnostics, then emitting it against a
  real in-memory tantivy index, never raises a `QueryError` whose
  `diagnostic.cause` is anything other than `Cause.UNSUPPORTED` (the
  documented case of a construct that parses cleanly but has no way to
  execute against tantivy, such as `DIVERGENCES.md` entry 5).
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
