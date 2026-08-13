# Architecture

This document explains how whoosh-compat turns a Whoosh-style query string
into a `tantivy.Query`, and the design decisions behind each layer. It's
meant to be read standalone by someone who has never seen this library
before.

## 1. Pipeline overview

```
query string
    │
    ▼
forked whoosh parser (taggers + filters, run at named priorities)
    │  (whoosh_compat.parser)
    ▼
typed, frozen AST
    │  (whoosh_compat.ast)
    ▼
normalize()
    │
    ▼
emitter visitor
    │  (whoosh_compat.emitters)
    ▼
tantivy.Query
```

`whoosh_compat.parse()` (`src/whoosh_compat/__init__.py`) drives the first
three stages and returns a `ParseResult(ast, diagnostics)`. Turning that
`ast.Node` into something a search backend can execute is a separate,
explicit step: `whoosh_compat.emitters.tantivy_.emit()`. That keeps the AST
usable by any future backend that doesn't exist yet.

A `FieldRegistry` (`whoosh_compat.fields`) is threaded through both the
parser and the emitter: it's the seam where the host application tells this
library what fields exist, what kind of data each one holds, and (for the
emitter) how to tokenize query text so it matches what's actually indexed.

## 2. Why a fork

Whoosh's query parser (`whoosh.qparser`) is a hand-written, plugin-driven
tagger/filter pipeline: each plugin contributes regex-based *taggers* (which
turn substrings of the query into syntax nodes) and *filters* (which
restructure the resulting node list: grouping, fieldname propagation,
operator binding), each running at a specific numeric priority relative to
the others. The *order* these run in, and the specific regexes each tagger
uses, together define what "lenient Whoosh syntax" actually means in
practice. For example, why `field:` at end-of-input merges into the
following text instead of erroring, or exactly which characters of spacing
around `-`/`+` make them operators versus plain text.

None of that leniency is written down anywhere except the code itself. It
would be impractical (and risky) to reimplement Whoosh's parsing behavior
from a description of it: the actual bug-for-bug (and, more usefully,
feature-for-feature) behavior only exists in the tagger/filter priority
interactions. So this library forks the parsing *pipeline* (the mechanism)
and replaces only the *output* of that pipeline: instead of every syntax
node building a `whoosh.query.Term`/`whoosh.query.And`/etc. object tied to a
`whoosh.fields.Schema`, syntax nodes here build `whoosh_compat.ast` nodes
against a backend-neutral `FieldRegistry`. The tagger/filter mechanics, the
priority ordering, and the natural-language date grammar are preserved
close to verbatim; everything downstream of "a parse succeeded" is new.

## 3. Module map with provenance

```
whoosh_compat/
  __init__.py            # parse(), ParseResult: public API
  fields.py              # FieldSpec, FieldKind, FieldRegistry: host-integration seam
  ast.py                 # AST dataclasses + Visitor[T] + normalize()
  errors.py              # Diagnostic + exception hierarchy
  parser/
    __init__.py
    default.py           # QueryParser/MultifieldParser   (forked: whoosh/qparser/default.py)
    plugins.py           # plugin set                     (forked: whoosh/qparser/plugins.py)
    syntax.py            # syntax nodes                   (forked: whoosh/qparser/syntax.py)
    taggers.py           # regex taggers                  (forked: whoosh/qparser/taggers.py)
    common.py            # tagging/attach helpers          (forked: whoosh/qparser/common.py)
    dateparse.py         # date grammar + plugin            (forked: whoosh/qparser/dateparse.py)
    times.py             # adatetime/timespan                (forked: whoosh/util/times.py)
    text.py              # rcompile                          (forked: whoosh/util/text.py, ~10 lines)
    priorities.py        # named priority constants          (not forked, see module docstring)
  emitters/
    base.py              # Emitter protocol
    tantivy_.py          # TantivyEmitter: the only module that imports tantivy
```

Forked files retain their original Whoosh BSD-2-Clause license header (see
[NOTICE](./NOTICE)): real provenance, kept as-is, distinct from any process
history. The lineage: Matt Chaput's original
[Whoosh](https://github.com/mchaput/whoosh) (also on
[PyPI](https://pypi.org/project/Whoosh/)), forked as
[whoosh-community/whoosh](https://github.com/whoosh-community/whoosh)
(itself now unmaintained), which is the fork this library's parser was
forked from in turn. Within the forked pipeline:

- **`parser/taggers.py`, `parser/text.py`, `parser/common.py`,
  `parser/times.py`**: foundational pieces (regex tagging, `rcompile`,
  tag-list manipulation helpers, the naive-local-time `adatetime`/`timespan`
  model) ported with minimal behavioral change beyond modernization
  (`compat.py` shims inlined, type annotations added, Python 3.11+ only).
- **`parser/syntax.py`**: the intermediate syntax-node tree (`WordNode`,
  `GroupNode`, `FieldnameNode`, etc.) that taggers produce and filters
  restructure before `.query(parser)` turns each node into an
  `whoosh_compat.ast.Node`.
- **`parser/plugins.py`**: the `Plugin` classes (whitespace, quoting,
  fields, wildcard, phrase, range, group, operators, boost, comma-values,
  field-alias, every) that each contribute taggers/filters to the pipeline.
  Only the currently-supported plugin subset ships; see the README's syntax
  table.
- **`parser/default.py`**: `QueryParser`/`MultifieldParser`, the top-level
  driver that assembles a plugin's taggers/filters, runs `tag()` then
  `filterize()`, and holds the field-kind-specific logic (numeric/boolean
  self-parsing, range endpoint parsing, wildcard/prefix rewriting) that
  Whoosh used to delegate to `Schema`/`FieldType` objects. Here it's methods
  on `QueryParser` that consult a `FieldRegistry` instead.
- **`parser/dateparse.py` + `parser/times.py`**: the natural-language date
  grammar (`Sequence`/`Combo`/`Choice`/`Bag`/`Regex` parser-combinator
  elements feeding `adatetime`/`timespan`) ported structurally unchanged.
  What's downstream of a successful date parse is new (see §4).

**`ast.py`**: frozen dataclasses (`Term`, `And`, `Or`, `Not`, `AndNot`,
`AndMaybe`, `Require`, `Phrase`, `Prefix`, `Wildcard`, `TermRange`,
`NumericRange`, `DateRange`, `Every`, `Nothing`, `Boosted`, `ErrorLeaf`), a
`Visitor[T]` base class that dispatches `visit_<lowercase-classname>`, and a
module-level `normalize()` that flattens nested same-type groups, propagates
`Nothing`/`Every` through boolean combinators, dedupes siblings, and merges
boost multipliers. This is the library's stability contract: emitters
(present and future) depend only on `ast.py` and `fields.py`, never on
`parser/`.

**`fields.py`**: `FieldKind` (TEXT, KEYWORD, U64, DATE, DATETIME,
BOOLEAN_EXISTS, JSON), `Multitoken` (how multi-token field values combine:
DEFAULT/AND/OR/PHRASE/FIRST), `FieldSpec` (one field's parse/emit
characteristics: name, kind, aliases, `comma_values`, `analyzer`,
`pattern_normalizer`, `multitoken`, `exists_target`, `subpaths`,
`date_only`, `fast`), `FieldRef` (a typed, canonical reference to a field,
carrying an optional JSON subpath), and `FieldRegistry` (validates and
indexes a collection of specs by canonical name and alias). This is the
host-integration seam: nothing in `parser/` or `emitters/` hard-codes field
names or behavior, it all comes from the registry a caller constructs.

Every AST leaf that carries a field (`Term`, `Phrase`, `Prefix`, `Wildcard`,
`TermRange`, `NumericRange`, `DateRange`, `Every`) holds a `FieldRef`, not a
raw field-name string. `FieldRegistry.make_ref(raw: str) -> FieldRef | None`
is the single place a dotted parser-level fieldname (`"notes.user"`) is
interpreted: it resolves an alias to its canonical name and decides, once,
whether the name addresses a plain field or a registered JSON field's
subpath, returning `None` for a name that resolves as neither (the case
`FieldsPlugin` already demotes back to text before it can reach an AST
leaf). Once a `FieldRef` exists, `FieldRegistry.resolve(ref) -> ResolvedField
| None` is the single resolver for it, plain or JSON subpath alike: nothing
downstream of `make_ref`, including the emitter, inspects a field name for a
literal `.` again. A registered *plain* field whose own name happens to
contain a dot (e.g. `"field.with.dots"`) still resolves directly and
exactly, since `make_ref` tries an exact-name match before ever attempting a
dotted-subpath split; see DIVERGENCES.md entry 14.

`ResolvedField` (`spec: FieldSpec`, `json_path: str | None`, plus the
`is_subpath` and `dotted_name` convenience properties) is `resolve()`'s
return type and the single resolution result: it exists so a call site can
never get from a subpath-carrying `FieldRef` to query-building or
message-building code while losing the subpath along the way. Before this
type existed, `resolve()` returned a bare `FieldSpec` and every consumer had
to separately remember to also read `ref.json_path`; forgetting compiled,
ran, and returned plausible-looking results (this was the shared root cause
behind a family of JSON-subpath bugs: existence checks and pattern/regex
builders that silently queried the whole field, and error messages that
named the field the user typed without its subpath). Emitter helpers that
build queries or messages from a resolved field (`_exists_query`,
`_text_term_query`, `_emit_json_term`, the range/prefix/wildcard builders)
now take a `ResolvedField`, not a bare `FieldSpec`: a helper that cannot yet
honor a subpath has to say so by reading only `.spec`, a visible decision at
the call site instead of a silent drop inside a resolver that never had the
subpath in the first place. Nothing outside `fields.py` reads
`ref.json_path` off an already-resolved field; the only legitimate reads of
`FieldRef.json_path` elsewhere are pre-resolution (deciding whether a ref
addresses a subpath at all, before calling `resolve()`).

**`emitters/`**: `base.py` defines a minimal `Emitter` protocol; the actual
work is `tantivy_.py`'s `TantivyEmitter(ast.Visitor[tantivy.Query])`, whose
module docstring explains its one deliberate exception to "build everything
programmatically" (the JSON-subpath carve-out, §5).

**Errors/diagnostics flow (`errors.py`)**: `Diagnostic(message, kind,
startchar, endchar, field, raw_value)` is a plain data record; `DiagnosticKind`
currently has `BAD_DATE`/`BAD_NUMBER`/`TOO_DEEP`/`UNKNOWN`. `field` and `raw_value`
default to `None` and are populated wherever a `Diagnostic` is constructed
against a known field (`DateParserPlugin._error()` in `dateparse.py`, and
both `BAD_NUMBER` sites in `default.py`'s `QueryParser`): `field` is a
`FieldRef` naming the field the diagnostic concerns (always the canonical
name, since a `Diagnostic` is only ever built once a field has resolved to
a spec), `raw_value` is the offending text as the user typed it. A host
that wants a typed exception (e.g. paperless-ngx's `InvalidDateQuery(field,
value)`) reads these two fields directly instead of regex-parsing
`message`, which stays human-readable and can change wording without
notice; a host that just wants the field's display name calls
`str(diag.field)`. Parsing never raises for bad input:
`QueryParser` accumulates `Diagnostic`s onto `self.diagnostics` as it goes
(see `default.py`'s `report()`), and bad fragments become
`ast.ErrorLeaf(diagnostic)` nodes in the tree rather than raising. This
mirrors Whoosh's own leniency, where an unparseable date became a null query
rather than an exception. `whoosh_compat.parse()` surfaces the accumulated
list as `ParseResult.diagnostics`, which a caller should check before
emitting (paperless-ngx, for example, maps a non-empty diagnostics list to
an HTTP 400). Emitting, by contrast, *does* raise: `QueryEmitError` when
asked to emit an `ErrorLeaf`, `UnsupportedQueryError` for a construct that's
parseable but genuinely inexecutable against tantivy (text-field
`TermRange`, see §4). Both inherit `WhooshCompatError`.

## 4. Key invariants

**All-`MustNot` padding.** A `tantivy.Query.boolean_query` whose clauses are
*all* `Occur.MustNot` matches zero documents instead of "every document
except the excluded ones". This is
[quickwit-oss/tantivy#3025](https://github.com/quickwit-oss/tantivy/issues/3025),
unmerged as of this writing. `_pad_if_all_negative()`
(`emitters/tantivy_.py`) prepends a trivially-true, zero-scoring `Must`
clause (`Query.boost_query(Query.all_query(), 0.0)`) whenever a boolean
group's clauses would otherwise be all-negative. This is applied at *every*
nesting level that constructs boolean clauses (a bare `NOT`, a falsy
`BOOLEAN_EXISTS` term, and any nested group that happens to reduce to only
negative clauses, not just the top level), so a query like
`tag:steuer AND (title:2020 OR (NOT title:2019 AND NOT title:2018))` gets
correct results on stock tantivy 0.26 with no upstream fix required.

**The analyzer contract.** `tantivy.Query.term_query` does **not** tokenize
its input: it builds a term against the literal string passed in. Whoosh,
by contrast, ran the field's analyzer at parse time. This library moves that
step to *emit* time deliberately (see §6 for why): `Term`/`Phrase` AST nodes
carry raw, unanalyzed text, and `TantivyEmitter` calls `FieldSpec.analyzer`
on that text before building term/phrase queries. A single-token result
becomes a `term_query`; the multi-token case is governed by
`FieldSpec.multitoken` (`Multitoken.DEFAULT` resolves to whatever boolean
group the term's emission happens to be nested inside, tracked by
`TantivyEmitter._group_stack`; see `DIVERGENCES.md` entry 15 for how this
differs subtly from Whoosh's own fixed-default-group behavior); a
zero-token result (the analyzer dropped everything, e.g. an all-stopword
value) drops the term from its enclosing group rather than producing an
unsatisfiable clause.

**The analyzer contract carries no positions.** `FieldSpec.analyzer` is typed
`Callable[[str], list[str]] | None`: it returns tokens in order, with no
positional metadata attached to any of them. This is not a parity gap, it
mirrors whoosh's own model exactly. Checked against the pinned oracle:
`whoosh.query.Phrase(fieldname, words, slop=1, boost=1.0, char_ranges=None)`
takes a plain word list that carries no positions either, and
`whoosh.analysis.filters.StopFilter(..., renumber=True)` renumbers surviving
tokens by default, so a stopword removed from the middle of a phrase leaves
no gap for whoosh to reason about in the first place. `str -> list[str]` is
the same contract whoosh already committed to.

That contract has two consequences worth stating plainly. First, if a host's
*index-time* tantivy analyzer leaves a position gap (for example, a stop
word filter that, unlike whoosh's, does not renumber), a phrase query built
from consecutive query-time positions can under-match against the index:
the words are still there and still in order, but the position arithmetic
tantivy's phrase matcher does no longer lines up. The mitigation available
today is `slop`, widened by however many positions the gap spans; there is
no way for `FieldSpec.analyzer` to close the gap itself, since it only ever
sees query-time text and has no visibility into how the host indexed the
field. Second, an analyzer that would want to produce several tokens at one
position, such as synonym expansion, cannot express that either: the return
type is a flat list, so any such tokens get flattened into sequence instead
of standing in parallel. Both are accepted boundaries of today's contract,
not defects in it. Whether to accept a richer, position-carrying token type
later, once a host presents a real query that under-matches because of the
first consequence, is a deliberate product decision and deferred until then.

`FieldSpec.pattern_normalizer` is a second, narrower callable used only for
the literal segments of `Wildcard`/`Prefix` patterns: character-level only
(lowercase, ASCII-fold), **never stemming or tokenization**. The two
callables can't be unified: index terms on a stemmed field are themselves
stems, but a wildcard's literal prefix has to stay literal for the
glob-to-regex translation to mean what the user typed. Running a stemmer
over `entwä` (from `Entwä*`) before pattern-matching would corrupt it. The
practical consequence (inherited honestly from Whoosh itself, which had the
same property) is that a wildcard query against a stemmed field only
matches *unstemmed* index tokens; see the README's "analyzer /
pattern_normalizer seam" section for the concrete example.

**Half-open date-range ceilings.** An ambiguous/period date match (e.g. just
a year, or `previous month`) is represented internally as a `timespan` whose
end is the period's last representable microsecond
(`adatetime.ceil()`, e.g. `2020-12-31T23:59:59.999999`). Rather than keep
that inclusive ceiling as the AST's upper bound, `DateParserPlugin` emits a
**half-open** range: `ceiling + 1 microsecond` as an *exclusive* upper bound
(the exact start of the next period), `incl_hi=False`. This is exact
because `ceil()` always lands on a period's last microsecond, and it avoids
the sentinel-date hacks (`0001-01-01`/`9999-12-31`) that a naive
open-ended-range implementation needs. A date the user typed as an exact
instant (not a period) keeps `incl_hi=True` since there's no rounding
involved.

**Timezone contract.** Everywhere in the AST, `DateRange` bounds are
timezone-aware UTC `datetime`s: parsed local/relative dates are converted
to UTC once, inside `DateParserPlugin` (`parser/dateparse.py`'s `_to_utc`).
The tantivy emitter then converts *back* to **naive** UTC
(`_to_naive_utc`, `emitters/tantivy_.py`) immediately before calling
`Query.range_query`, because `tantivy-py <= 0.26.0` only accepts naive
datetimes there (a tz-aware one raises `ValueError`). The fix,
[tantivy-py#666](https://github.com/quickwit-oss/tantivy-py/pull/666), landed
*after* 0.26.0 was tagged. This conversion is intentionally isolated to the
one call site that needs it; nothing else in the pipeline treats naive and
aware datetimes as interchangeable.

**Kind dispatch is a closed matrix.** A query leaf's behavior is a function
of (leaf node type, field kind, value spelling, JSON subpath or not). The
costliest defects in this codebase's history were rules implemented for one
cell of that matrix and silently missing from siblings, degrading to
TEXT-shaped behavior instead of failing loudly. Every cell must therefore
resolve to exactly one of three outcomes: a parse-time `Diagnostic`, a
documented emit-time error, or an execution that honors the field kind and
subpath. Code that dispatches on field kind or node type handles the full
axis explicitly; an unhandled combination raises rather than falling through.
`tests/emitter/test_kind_matrix.py` enforces this directly: it enumerates
every `FieldKind` member against the leaf spellings reachable from query
text and asserts each cell lands in exactly one of the three outcomes,
deriving its rows from the live `FieldKind` enum so a new member with no
classified cells fails the suite instead of just shrinking coverage
silently.

**Diagnostics never raise mid-parse.** See §3's error-flow paragraph: this
is worth restating as an invariant because it's load-bearing for callers.
`whoosh_compat.parse()` always returns a `ParseResult`, never raises for bad
*query* input (as opposed to bad *registry construction*, which does raise
eagerly, see `FieldRegistry.__init__`). Malformed dates, numbers, or other
field-kind-specific parse failures become `Diagnostic`s plus `ErrorLeaf`
AST nodes. Only `emit()` on a tree containing an `ErrorLeaf` raises
(`QueryEmitError`), by which point the diagnostics list already told the
caller not to do that.

**Query nesting is capped, so pathological input can't turn "never raises"
into a `RecursionError`.** A well-formed but absurdly deep query (thousands
of nested parens around a single term) is legitimate input by this
invariant's own rule, yet every stage that walks the resulting tree
recursively (the tag/filter pipeline's own filters, `GroupNode.query()`, and
`ast.normalize()` before it became iterative) costs Python call-stack frames
proportional to depth. Left unbounded, `parse()` would eventually
`RecursionError` instead of returning a `ParseResult`, which is exactly the
invariant this section opens with breaking. `GroupPlugin.do_groups`
(`parser/plugins.py`) is the one place that turns flat `(`/`)` markers into
real tree hierarchy, so it's also the cheapest and earliest place to bound
it: past `_MAX_GROUP_NESTING_DEPTH` (200) unclosed levels, further nesting is
tracked as an opaque, uncounted overflow region instead of being
materialized into hierarchy, and collapsed to a single `Diagnostic(kind=
TOO_DEEP)` plus `ErrorLeaf` once its matching close paren is seen (or at the
end of input, for unbalanced parens). 200 was chosen with a wide safety
margin: confirmed directly against both the pinned real-whoosh oracle and
this parser's own tag/filter pipeline with the cap removed, both start
`RecursionError`-ing on bare `(`-nesting somewhere between depth 950 and
1000 (Python's default recursion limit), so 200 leaves roughly 5x headroom
for the several other recursive filters (`do_wildcards`, `do_boost`,
`do_fieldnames`, `do_operators`, `do_multifield`, `do_aliases`,
`do_comma_values`) a still-legal, still-under-the-cap tree passes through
afterward, while comfortably exceeding any nesting a real query would use.
`ast.normalize()`'s traversal was also converted from recursive to
iterative (an explicit work stack) independently of the cap, since a
hand-built AST that bypasses the parser entirely (constructed directly
against `whoosh_compat.ast`, never going through `GroupPlugin`) has no
parse-time cap to rely on, and previously cost two stack frames per nesting
level on top of whatever the parser itself had already used, roughly
halving the tolerated depth versus the raw parse.

**Spans are preserved through `normalize()`, not just set at parse time.**
Every `Node` carries an optional `startchar`/`endchar` (character offsets
into the original query string), attached by the parser (`parser/common.py`'s
`attach()`) as each syntax node builds its AST node. A leaf's span excludes
its field prefix (`title:foo` yields `Term(text="foo")` spanning just
`"foo"`) and, for a boosted clause, excludes the trailing `^N` too
(`BoostPlugin` strips the boost token out of the syntax tree before `query()`
ever builds the AST, so a `Boosted` node's span is always identical to its
child's span; `attach()` backfills that span onto the child, since call
sites build `ast.Boosted(child, boost)` before the wrapping span is known).
`ast.normalize()` must not silently drop this metadata while rebuilding
nodes: a rebuilt node whose structure is unchanged (`Not`, `AndNot`,
`AndMaybe`, `Require`, a `Boosted` that isn't collapsing to something else)
carries the pre-normalize node's own span forward; a flattened/merged `And`
or `Or` instead takes the union (min start, max end, skipping any child with
no span at all, `None` if none have one) of whatever ended up as its final
children, since flattening can absorb a nested same-type node's children or
drop some via dedupe/`Nothing`/`Every` filtering. A branch that returns one
of its already-normalized children verbatim (a single-child unwrap, an
`AndNot`/`AndMaybe` side dropping out, a boost merging away to 1.0) leaves
that child's own span alone rather than widening it to the parent's. This
makes spans usable as a genuine subtree-to-source-text mapping on the
post-`normalize()`, post-`parse()` public tree, not just on the raw
pre-normalize parser output.

## 5. Extension points

**A new emitter.** Implement `ast.Visitor[T]` (subclass it and provide
`visit_<name>` methods for whichever node types matter to your backend;
unhandled types fall through to `generic_visit`, which raises
`NotImplementedError`) plus a small module-level `emit(node, ...)` function
following `emitters/tantivy_.py`'s shape. The AST and `FieldRegistry` are
the entire contract: a Meilisearch or Elasticsearch emitter needs no
changes to `parser/` or `ast.py`.

**`FieldSpec` options.** Per-field behavior (aliasing, comma-list
expansion, which analyzer/pattern-normalizer to run, how multi-token values
combine, JSON subpaths, boolean-exists targets, fast-field/date-only
declarations) is entirely data on `FieldSpec`, validated at
`FieldRegistry` construction time (e.g. `BOOLEAN_EXISTS` requires
`exists_target`). Where a field's declared shape decides *how* a query can
be executed, the registry resolves that once, at construction, rather than
leaving it to the emitter: an `exists_target` is resolved to an
`ExistsStrategy` (a fast-field presence check, a fast JSON field's
subpath-aware presence check, or a term-dictionary scan for a non-fast
TEXT/KEYWORD field), and a target that supports neither is rejected there
with a message naming the remedy. `Every(field)` and
`BOOLEAN_EXISTS` then share that single resolved strategy, so the two
cannot drift into different answers to the same question. Extending what
the library can express for a field means adding a
new `FieldSpec` attribute and teaching the parser/emitter to read it, no
plugin-architecture changes required for field-level behavior.

**The JSON `parse_query` carve-out.** `TantivyEmitter._json_paths_supported()`
probes, once per emitter instance, whether the installed `tantivy-py`'s
`Query.term_query` can resolve a JSON subpath directly. If not, JSON-subpath
term emission falls back to a strictly escaped, single-leaf
`index.parse_query()` call instead of the fully-programmatic construction
used everywhere else. This carve-out is self-retiring: once
[tantivy-py#716](https://github.com/quickwit-oss/tantivy-py/pull/716) lands
and ships, the probe starts succeeding and the fallback branch simply stops
being taken. No code change is required in this library.

## 6. Testing strategy

Three layers, each answering a different question:

1. **Unit tests** (`tests/`, excluding `differential/` and `emitter/`):
   parser, `ast.normalize()`, and `FieldSpec`/`FieldRegistry` behavior in
   isolation, including Whoosh's own ported qparser/dateparse test suites
   adapted to this AST.
2. **Differential tests** (`tests/differential/`): "does the parsed AST
   shape agree with real Whoosh?" A pinned real-Whoosh installation (a
   test-only dependency, git-ref pinned, see `pyproject.toml`'s `dev`
   group; this fork carries parser fixes absent from the PyPI release) and
   whoosh-compat parse the same corpus of query strings; oracle-side parsing
   uses `normalize=False` to sidestep Whoosh's own tree-normalization pass
   (this library's `normalize()` has its own dedicated unit tests). Every
   allowed divergence must match an entry in `tests/differential/allowlist.py`,
   and each allowlist entry cross-references a numbered entry in
   `DIVERGENCES.md` explaining *why* the difference is intended rather than
   a bug. The convention there is a regex keyed by a reference prefix (e.g.
   a field-name pattern) matched against the query string, so a whole class
   of queries can share one documented, understood divergence.
3. **End-to-end acceptance tests** (`tests/emitter/test_acceptance_e2e.py`):
   "does the final search result agree?" The same fixture documents are
   indexed twice, once in a real Whoosh index, once in tantivy via
   whoosh-compat, and named query scenarios assert **identical
   matched-document ID sets** (unordered; scores are out of scope). A
   fixture self-test verifies the precondition that scenario terms tokenize
   identically under both analyzer chains, so the comparison isn't
   accidentally testing analyzer drift instead of query-translation
   correctness.

**Why both 2 and 3 exist, not just one:** layer 2 catches AST-shape
regressions early and cheaply (no index-building, no search execution), but
an AST-level difference doesn't necessarily change what a query actually
matches. A divergence can be fully absorbed by what happens downstream
(analysis, tantivy's own query execution). Layer 3 is the layer that answers
the question a user actually cares about. `DIVERGENCES.md` entry 16 and the
`test_acceptance_e2e.py` module docstring document several concrete cases
where a real, allowlisted AST-level divergence was verified (by actually
running both pipelines) to produce the same search results for this
project's fixture, which is a property of the fixture, not a guarantee that
holds for every possible query and dataset.

## See also

[DIVERGENCES.md](./DIVERGENCES.md): the full, numbered list of every
intentional behavioral difference from real Whoosh, or from a naive
string-translation migration to another search engine, with the reasoning
and test references for each.
