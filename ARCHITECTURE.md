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
typed, frozen AST (raw, unanalyzed text)
    │  (whoosh_compat.ast)
    ▼
normalize()
    │
    ▼
analyze()               ← tantivy-emitter-specific, see below
    │
    ▼
emitter visitor (purely structural)
    │  (whoosh_compat.emitters)
    ▼
tantivy.Query
```

`whoosh_compat.parse()` (`src/whoosh_compat/__init__.py`) drives the first
three stages and returns a `ParseResult(ast, diagnostics)`: the tree it
hands back still carries raw, unanalyzed `Term`/`Phrase` text (the "AST
carries raw text" contract, unchanged by anything below). Turning that
`ast.Node` into something a search backend can execute is a separate,
explicit step: `whoosh_compat.emitters.tantivy_.emit()`. That keeps the AST
usable by any future backend that doesn't exist yet.

`whoosh_compat.ast.analyze()` is the explicit pipeline stage that resolves
per-field token analysis: it rewrites a multi-token `Term`/`Phrase` leaf into
the `And`/`Or`/`Phrase` shape its field's `Multitoken` policy calls for,
drops a leaf that analyzes to zero tokens (and any group that thereby
empties), re-normalizes, and returns a plain, already-normalized
`ast.Node`. `TantivyEmitter.emit()` calls it (`analyze(normalize(node),
registry, default_mode=Multitoken.AND)`) before visiting, which is *this
emitter's own choice*, not part of the generic `Emitter` protocol
(`emitters/base.py`): a hypothetical future backend that defers token
analysis to its own server could call `emit()`-equivalent logic without ever
calling `analyze()`. Once `analyze()` has run, `TantivyEmitter` is a purely
structural visitor: no `visit_*` method tokenizes text, decides whether a
subtree drops out of an enclosing group, or tracks what group a term is
nested inside; every such decision was already made, once, by `analyze()`.
See `ast.analyze`'s own docstring for the full worked-out rules (multitoken
resolution, the `default_mode` parameter, idempotence, and how DIVERGENCES.md
entry 23's "NOT of a zero-token term" divergence falls out of this pipeline's
ordering rather than being special-cased).

A `FieldRegistry` (`whoosh_compat.fields`) is threaded through the parser,
`analyze()`, and the emitter: it's the seam where the host application tells
this library what fields exist, what kind of data each one holds, and how to
tokenize query text so it matches what's actually indexed.

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
  ast.py                 # AST dataclasses + Visitor[T] + normalize() + analyze()
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
  What's downstream of a successful date parse is new (see §4). One thing
  *upstream* of it is new too: `DateParserPlugin.do_date_phrases`, a filter
  running just after fieldname assignment that joins an unquoted multi-word
  date keyword (`added:previous month`) back into a single value node before
  the grammar sees it. In Whoosh a value always ends at the first space, so
  this is a deliberate widening, confined to the six known phrases (plus a
  trailing time of day) on an explicitly named date field so that no other
  date value becomes whitespace-greedy; `DIVERGENCES.md` entry 19 records
  what it does and does not cover.

**`ast.py`**: frozen dataclasses (`Term`, `And`, `Or`, `Not`, `AndNot`,
`AndMaybe`, `Require`, `Phrase`, `Prefix`, `Wildcard`, `TermRange`,
`NumericRange`, `DateRange`, `Every`, `Nothing`, `Boosted`, `ErrorLeaf`), a
`Visitor[T]` base class that dispatches `visit_<lowercase-classname>`, a
module-level `normalize()` that flattens nested same-type groups, propagates
`Nothing`/`Every` through boolean combinators, dedupes siblings, and merges
boost multipliers, and a module-level `analyze()` (§1) that resolves
per-field token analysis into the tree's own structure. This is the
library's stability contract: emitters (present and future) depend only on
`ast.py` and `fields.py`, never on `parser/`. `Term.analyzed` and
`Phrase.words`/`Phrase.analyzed` are part of that contract too: they carry
`analyze()`'s own output representation (an analyzed leaf's tokens
explicitly, rather than a joined string an emitter would have to re-split),
and are deliberately excluded from equality/hashing the same way
`startchar`/`endchar` are, since they are analysis provenance, not
independent semantic content. One deliberate carve-out: the
duplicate-sibling dedupe (shared by `normalize()` and `analyze()`'s group
rebuild, which routes through the same `_dedupe`) keys on node equality
*plus* `Phrase.words` and the `analyzed` flag, because both genuinely are
result-bearing there: the emitter builds its positional `phrase_query`
from `words` (a shingle-style analyzer whose tokens contain spaces can
produce two equal-comparing phrases with different word tuples and
different match sets; real whoosh's own `Phrase.__eq__` compares the word
lists and keeps both), and an unanalyzed leaf, unlike its equal-comparing
analyzed twin, would still be tokenized by a later `analyze()` pass.

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

`FieldSpec.subpaths` (only meaningful for JSON-kind fields) is
`Mapping[str, SubpathSpec]`: each registered subpath maps to a
`SubpathSpec`, a near-trivial dataclass carrying one flag, `default`, whose
container shape was frozen ahead of per-subpath typing (a numeric- or
date-typed subpath), which is still a separate, later change. Construction also
accepts the older `tuple[str, ...]` form as sugar for "every one of these
subpaths, with the trivial default `SubpathSpec`"; `FieldSpec.__post_init__`
normalizes a tuple into the mapping form immediately, so the value actually
stored on a constructed instance, and read everywhere else in the library
(`FieldRegistry.resolve`'s `ref.json_path not in spec.subpaths`,
`make_ref`'s `subpath in spec.subpaths`, the JSON-path-support probe in
`emitters/tantivy_.py`), is always the mapping. Anything that is neither a
tuple nor a `Mapping` is rejected there, rather than passed to `dict()`:
`dict()` reads a sequence of two-character strings as key/value pairs, so
the plausible mis-spelling `subpaths=['ab', 'cd']` used to construct,
validate, and register as `{'a': 'b', 'c': 'd'}`, leaving the real subpaths
permanently unaddressable and every query against one degrading to
default-field noise with no error anywhere.

`SubpathSpec(default=True)` marks the one subpath a *bare* mention of the
parent JSON field means: with it, `make_ref("notes")` resolves to
`FieldRef("notes", "note")` rather than `None`, and
`is_bare_json_field("notes")` is False, since the name is no longer
unresolvable. An explicitly typed subpath still wins; the default only
decides the bare name. This exists so a host that wants `notes:` to mean
`notes.note:` does not have to rewrite the raw query string before parsing:
such a rewrite cannot see quotes, so it also corrupts
`content:"payment notes: none"`, where the same characters are ordinary
text. It is opt-in per field because a JSON field with no privileged
subpath (a custom-field bag, say) is better left unresolvable.
`FieldRegistry.__init__` rejects more than one default per spec, and checks
every subpath *value* is really a `SubpathSpec` (load-bearing now that the
type carries a flag: anything else either has no `.default` or has a truthy
attribute of its own that would silently decide what a bare mention means).

`FieldRegistry.__init__`
validates the mapping's keys: an empty subpath string, a subpath containing
whitespace, `:`, or `"` (or any other character outside the fieldname tagger's
expression, `r"(?P<text>[\w.]+|[*]):"`, which can only ever produce word
characters and dots; the same whitelist applies to every canonical name
and alias, since a field no query text can address is a misconfiguration
trap regardless of which part of the dotted route carries the bad
character), and a registered canonical name or alias that exactly
matches `<jsonfield>.<subpath>` for a registered subpath, where
`<jsonfield>` is the JSON field's canonical name or any of its aliases
(each alias makes `<alias>.<subpath>:` a supported query route too, and a
collision would permanently shadow it under `make_ref`'s exact-match-first
rule) are all rejected at construction, regardless of registration order.

Every AST leaf that carries a field (`Term`, `Phrase`, `Prefix`, `Wildcard`,
`TermRange`, `NumericRange`, `DateRange`, `Every`) holds a `FieldRef`, not a
raw field-name string. `FieldRegistry.make_ref(raw: str) -> FieldRef | None`
is the single place a dotted parser-level fieldname (`"notes.user"`) is
interpreted: it resolves an alias to its canonical name and decides, once,
whether the name addresses a plain field or a registered JSON field's
subpath, returning `None` for a name that resolves as neither, and also for
a bare JSON field name addressed without a subpath *and declaring no default
subpath* (`FieldsPlugin` demotes
either case back to text before it can reach an AST leaf, except for one
carve-out: a bare JSON field name followed by a lone `*` is the
existence-check special case, not a term to demote, and still reaches
`Every(FieldRef(name))` via `FieldRegistry.is_bare_json_field`; see
DIVERGENCES.md entry 20). A declared default subpath moves the field out of
that demotion path entirely: `notes:` is then an ordinary recognized field
prefix, so the carve-out is never consulted for it and `notes:*` is an
ordinary recognized-field existence check. Everything that follows from
declaring a default follows from that one resolution change, and three
shapes are worth naming: `notes:*` narrows from the whole-field question
`exists_query(name, json_subpaths=True)` to the default subpath's own
column (result-changing on a fast JSON field; on a non-fast one it is the
same `EXISTS_REQUIRES_FAST` refusal either way, naming the dotted form
instead of the bare one), `notes:fo*` becomes a parse-time
`PATTERN_ON_SUBPATH` diagnostic, and `notes:[a TO b]` an emit-time
`TEXT_RANGE` refusal, the latter two where the bare name previously demoted
to a silent default-field text search (DIVERGENCES.md entries 20 and 30).
All three are exactly what a `notes:` → `notes.note:` rewrite in the host
would have produced. Once a `FieldRef` exists, `FieldRegistry.resolve(ref) -> ResolvedField
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
`_emit_json_term`, `_emit_json_phrase`, the range/prefix/wildcard builders)
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

**Errors/diagnostics flow (`errors.py`)**: `Diagnostic` is a frozen,
keyword-only dataclass (`kind`, `cause`, `message`, `startchar`, `endchar`,
`field`, `field_kind`, `raw_value`, `divergence`). `DiagnosticKind` is a
19-member enum, partitioned by `PARSE_KINDS`/`EMIT_KINDS` (`errors.py` is
the authority; hosts branching exhaustively on the kind should read the
enum itself). Every member maps to a `Cause` (`INVALID_INPUT`/
`UNSUPPORTED`/`MISCONFIGURED`/`INTERNAL`) via `cause_for()`, which a host
uses for coarse routing without knowing every `DiagnosticKind`. Because
`cause_for()` is a lookup keyed only on `kind`, one kind has exactly one
cause: two raise sites that need different causes need different kinds.
That is why a registry/schema mismatch reports `SCHEMA_FIELD_MISSING`
(`MISCONFIGURED`) rather than sharing `BACKEND_REJECTED` (`INTERNAL`) with
`emit`'s bare-`ValueError` backstop. Every leaf that queries a resolved
field wraps its tantivy call in `TantivyEmitter._reporting_schema_drift`,
which confirms the condition with `_field_in_schema` (a `.*` regex probe,
the one construction that builds against a field of any kind and fails only
on a missing name) and re-raises anything else, so the backstop's remaining
`ValueError`s really are defects in this library. Drift is a property of the
field, not of the leaf spelling, so the whole leaf axis agrees; a host
routing on `cause` never sees `content:x` and `content:x*` land on opposite
sides of the 400/500 line for one broken deployment. `_exists_query` is the
one path that probes *up front* rather than on failure, because tantivy's
`exists_query` takes no schema and validates nothing at build time: there is
no build-time failure to catch, and without the probe a drifted field would
build a well-formed query that dies in the searcher, outside `emit`'s
`QueryError` contract.

`field` and
`raw_value` default to `None` and are populated wherever a `Diagnostic` is
constructed against a known field (`DateParserPlugin._error()` in
`dateparse.py`, and the `BAD_NUMBER` and pattern-diagnostic sites
(`_wildcard_kind_diagnostic`, which now reports `PATTERN_ON_NUMERIC`,
`PATTERN_ON_BOOLEAN_EXISTS`, or `PATTERN_ON_SUBPATH` depending on the
field's kind) in `default.py`'s `QueryParser`): `field` is a `FieldRef`
naming the field the diagnostic concerns (always the canonical name, since
a `Diagnostic` is only ever built once a field has resolved to a spec),
`raw_value` is the offending text as the user typed it. `divergence`, when
set, is the `DIVERGENCES.md` entry number the diagnostic corresponds to, so
a host can cross-reference without reading prose. A host that wants a typed
exception (e.g. paperless-ngx's `InvalidDateQuery(field, value)`) reads
`kind`/`cause`/`field`/`raw_value` directly instead of regex-parsing
`message`, which stays human-readable and can change wording without
notice; a host that just wants the field's display name calls
`str(diag.field)`. Parsing never raises for bad input:
`QueryParser` accumulates `Diagnostic`s onto `self.diagnostics` as it goes
(see `default.py`'s `report()`), reset at the start of each `parse()` call so
one instance's diagnostics never leak from one query into the next (not
thread-safe across concurrent calls on the same instance, see the class
docstring), and bad fragments become
`ast.ErrorLeaf(diagnostic)` nodes in the tree rather than raising. This
mirrors Whoosh's own leniency, where an unparseable date became a null query
rather than an exception. `whoosh_compat.parse()` surfaces the accumulated
list as `ParseResult.diagnostics`, which a caller should check before
emitting (paperless-ngx, for example, maps a non-empty diagnostics list to
an HTTP 400). Emitting, by contrast, *does* raise: a single `QueryError`,
always carrying the `Diagnostic` describing why (`err.diagnostic`), whether
the cause is an `ErrorLeaf` reaching `emit()` or a construct that's
parseable but genuinely inexecutable against tantivy (text-field
`TermRange`, see §4). `QueryError` inherits `WhooshCompatError`.

(`tests/differential/allowlist.py`'s `DivergenceKind` enum is unrelated to
`Diagnostic.divergence`: "divergence" now names three distinct concepts in
this repo, the differential-testing allowlist classification, the
`DIVERGENCES.md` entry numbers, and the `Diagnostic.divergence` field that
cross-references the latter.)

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
step later deliberately (see §6 for why): `parse()`'s `Term`/`Phrase` AST
nodes carry raw, unanalyzed text, and `analyze()` (§1) is what calls
`FieldSpec.analyzer` on that text, once, structurally, before
`TantivyEmitter` ever visits the tree. A single-token result becomes a
`Term` a `visit_term` call turns straight into a `term_query`; the
multi-token case is governed by `FieldSpec.multitoken` (`Multitoken.DEFAULT`
resolves to whichever boolean group the term's *position in the tree*
actually sits inside, computed once by `analyze()`'s own top-down context
pass, not tracked via any per-visit emitter state; see `DIVERGENCES.md`
entry 15 for how this differs subtly from Whoosh's own fixed-default-group
behavior); a zero-token result (the analyzer dropped everything, e.g. an
all-stopword value) drops the term from its enclosing group rather than
producing an unsatisfiable clause, via `analyze()`'s own re-normalization,
not a per-visit drop check.

**Polarity is a pre-`analyze()` property.** `analyze()`'s zero-token drop is
deliberately blind to *which* operand dropped (DIVERGENCES.md entry 23's
uniform survivor rule), so an `AndNot` whose *positive* side analyzed away
leaves its negative side standing alone as an ordinary positive node. The
analyzed tree therefore cannot be asked "did the user exclude this?", and
any API answering a question about intent rather than about matching must
walk the normalized-but-unanalyzed tree and analyze whichever leaves it
decides to keep, one leaf at a time. `ast.free_text_tokens()` is the one
such API today and does exactly that (`_leaf_analyzed_texts`); it is the
model for any future one. The corollary binds the *caller* too: such an API
can only answer for the tree it is handed, so passing one that has already
been through `analyze()` silently forfeits the guarantee, the collapse
having already happened where no later walk can see it.
`free_text_tokens()` states that as a precondition on its `node` parameter
rather than guarding on it: an analyzed tree is structurally
indistinguishable from any other valid tree, and the `analyzed` flags it
carries are `compare=False` provenance, not an input contract.

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
the *term* characters of `Wildcard`/`Prefix` patterns: character-level only
(lowercase, ASCII-fold), **never stemming or tokenization**. Term characters
means whole literal runs and, one character at a time, the body of a bracket
character class (`BILL[I]NG*` folds to `bill[i]ng.*`: a class member is an
index character exactly as a literal run's characters are). The per-character
application inside a class is deliberate: a normalizer may expand one
character into several (`ascii_fold` maps `ß` -> `ss`), which a range endpoint
cannot survive and a class cannot express, so such characters are left exactly
as typed rather than corrupting the class. See `_normalize_class_body` in the
tantivy emitter. The two
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

**`date_only` fields ceil their exclusive upper bound.** `_to_utc()`
collapses a `date_only` (DATE) field's bounds to UTC-midnight calendar
days; only the date matters, no timezone offset is applied. Truncating an
exclusive upper bound (the half-open ceiling shape above) *down* to its own
day's midnight would move it backwards whenever the untruncated value
carried time-of-day precision, either emptying the range or dropping the
named end day. `_to_utc` ceils such a bound *up* to the next day's midnight
instead when it isn't already day-aligned; the lo bound, and any
both-inclusive exact instant, keep truncating down. See DIVERGENCES.md
entry 32.

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

**Date-range bounds are clamped to tantivy's representable window.**
tantivy's `DateTime` is an i64 nanosecond count, giving a representable
window of roughly [1677-09-21T00:12:43Z, 2262-04-11T23:47:16Z]; a range
bound outside it is converted with *silent* i64 overflow, wrapping modulo
2^64 nanoseconds (measured on the pinned tantivy-py: a [3771, 3773) year
range matched a 2019 document, and `created:[2018 TO 9998]` matched
nothing while whoosh matched everything). `visit_daterange`
(`emitters/tantivy_.py`) therefore clamps bounds into the window before
`range_query`: a clamped edge becomes inclusive (every instant it stands
in for is unrepresentable anyway), and a range lying entirely outside the
window emits a match-nothing query. This restores whoosh's own correct
handling of far-past/far-future years, so it carries no DIVERGENCES.md
entry; it is tracked as a carve-out (see the carve-out-retirement skill)
because the constants' whole-second inward rounding is only safe while
tantivy-py truncates datetimes to whole seconds, which a future release
must re-verify. Index-time dates are the host's responsibility: an
out-of-window date (or a sub-second instant inside the sliver just above
the true minimum, which second-truncation pushes below it) is already
stored wrapped, and no query-side handling can repair that.

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
`whoosh_compat.parse()` always returns a `ParseResult`, never reporting bad
*query* input by raising. The invariant is scoped to the query path:
a *configuration* mistake still raises eagerly, both in
`FieldRegistry.__init__` and in `parse()` itself, which raises `ValueError`
for an empty or unknown `default_fields` and for a naive `basedate`.
Malformed dates, numbers, or other field-kind-specific parse failures
become `Diagnostic`s plus `ErrorLeaf` AST nodes.
On the query path the one exception `parse()` can
raise is `QueryParserError`, and it never means the query was bad: it is the
backstop's report of a defect in this library, and the compounding nesting
shape described below is the known way to reach it from a query string. Only
`emit()` on a tree containing an `ErrorLeaf` raises `QueryError`, by which
point the diagnostics list already told the caller not to do that.

**Query nesting is capped, so pathological input can't turn "never raises"
into a `RecursionError`.** A well-formed but absurdly deep query (thousands
of nested parens around a single term) is legitimate input by this
invariant's own rule, yet every stage that walks the resulting tree
recursively (the tag/filter pipeline's own filters, `GroupNode.query()`, and
`ast.normalize()` before it became iterative) costs Python call-stack frames
proportional to depth. Left unbounded, `parse()` would eventually
`RecursionError` instead of returning a `ParseResult`, which is exactly the
invariant this section opens with breaking. Two stages construct hierarchy,
and each bounds *its own* contribution at `_MAX_GROUP_NESTING_DEPTH` (200),
as early as it can see the depth coming. Neither bounds total depth: both
caps are per group, and they can still compound along a nesting path — see
"what the caps do not cover" below.

`GroupPlugin.do_groups` (`parser/plugins.py`) turns flat `(`/`)` markers into
real tree hierarchy: past 200 unclosed levels, further nesting is
tracked as an opaque, uncounted overflow region instead of being
materialized into hierarchy, and collapsed to a single `Diagnostic(kind=
TOO_DEEP)` plus `ErrorLeaf` once its matching close paren is seen (or at the
end of input, for unbalanced parens).

`OperatorsPlugin.do_operators` builds hierarchy too, and the bracket stack
cannot see it: `InfixOperator.replace_self()` nests one new group per
*non-merging* infix operator (`ANDNOT`, `ANDMAYBE`, `REQUIRE`), so a flat,
paren-free `a ANDNOT b ANDNOT c ...` chain is as deep as it is long, and the
filter's own descent into every level used to make ~991 operands a
`RecursionError` out of `parse()`. It therefore counts those operators in
each flat group before rearranging anything and, at 200 or more, collapses
the group to the same `TOO_DEEP` leaf (`_too_deep_node()`, shared with
`do_groups`) instead of materializing the levels. Merging operators
(`AND`/`OR`) absorb their neighbour into a single flat group and add no
depth, so a chain of them is unaffected at any length; prefix/postfix
operators (`NOT`) wrap one node each without nesting and are likewise not
counted. `do_operators`' descent into subgroups is an explicit work stack
rather than recursion, so hierarchy reaching it from any source cannot
exhaust frames on the way down.

**What the caps do not cover.** Both caps count within a single flat group,
so a query that nests groups which *each* stay under the cap can still build
a tree deeper than 200: 20 levels of parens, each holding a 50-operator
`ANDNOT` chain, is ~1000 levels of `AndNotGroup` from under 10KB of input, and
`GroupNode.query()`/`BinaryGroup.query()` (`parser/syntax.py`) still recurse
once per level, so that input `RecursionError`s internally. This is
long-standing behaviour, not something the operator cap introduced or
regressed, and the paren cap has the same shape of hole. Closing it is not a
matter of a third cap: the backstop for it is the exception boundary in
`parse()` (`__init__.py`), which turns any unexpected exception out of the
parse pipeline into a `QueryParserError` (chaining the original as
`__cause__`) instead of letting a bare `RecursionError` reach the caller, so
the compounding shape above surfaces as a `QueryParserError`, pinned by
`tests/test_parse_never_raises.py`. What the caps buy is that the *ordinary*
pathological shapes (a long chain, a deep pile of parens) never reach that
backstop at all: they get a real `TOO_DEEP` diagnostic instead.

200 was chosen with a wide safety
margin: confirmed directly against both the pinned real-whoosh oracle and
this parser's own tag/filter pipeline with the cap removed, both start
`RecursionError`-ing on bare `(`-nesting somewhere between depth 950 and
1000 (Python's default recursion limit), so 200 leaves roughly 5x headroom
for the several other recursive filters (`do_wildcards`, `do_boost`,
`do_fieldnames`, `do_multifield`, `do_aliases`, `do_comma_values`) a
still-legal, still-under-the-cap tree passes through afterward, while
comfortably exceeding any nesting a real query would use. `do_operators`
is no longer one of them - its descent is the explicit work stack
described above - which only widens the margin.
`ast.normalize()`'s traversal was also converted from recursive to
iterative (an explicit work stack) independently of the cap, since a
hand-built AST that bypasses the parser entirely (constructed directly
against `whoosh_compat.ast`, never going through `GroupPlugin`) has no
parse-time cap to rely on, and previously cost two stack frames per nesting
level on top of whatever the parser itself had already used, roughly
halving the tolerated depth versus the raw parse.

That iterative traversal had a gap a work stack alone cannot see: its
duplicate-sibling dedupe step (`_dedupe`) put each sibling into a `set`,
which hashes it with the frozen dataclasses' generated `__hash__` -
recursing through that sibling's *own* subtree in native Python frames no
matter how the traversal that called it is shaped. A sibling that is
itself deep rather than wide (a long `Not` chain standing next to an
ordinary term) could still `RecursionError` there, contradicting the
"an explicit work stack ... so a pathologically deep or wide tree costs
heap, not Python call-stack frames" guarantee `normalize()`'s own
docstring already made. This is an invariant repair, not the closing of a
live host-facing hole. `parse()`'s own depth caps keep every parsed tree
well under the depth this needs, so it was never reachable that way, and
`TantivyEmitter.emit()` (`emitters/tantivy_.py`) was never exposed to it
either: its `try` around `ast.analyze(ast.normalize(node), ...)` already
caught `RecursionError` by name and converted it to the same
`QueryError(AST_INVALID_SHAPE)` its own still-recursive `visit_*` chain
(`visit_not` calling `self.visit(node.child)`, and so on) converts a
too-deep hand-built tree to anyway - so a caller going through `emit()`
saw a `QueryError` for this shape before the fix and still does after it;
nothing changed there. The only place this fix changes observable
behavior is a caller invoking `ast.normalize()`/`ast.analyze()` directly
on a hand-built tree and using the result for something other than
`emit()`. `_dedupe` now computes its key with its own iterative,
memoized traversal (matching the same node-equality semantics, including
the `Phrase.words`/`analyzed` extension described on its docstring, and
the NaN/negative-zero quirks `Boosted.boost` raises for a string-based
key - see `_encode_field`'s docstring, including why that key
deliberately does *not* attempt numeric-tower canonicalization: doing so
once traded a reachable, silent data-loss bug - two distinct large
integers on a U64/ASN field rounding to the same `float` and one query
branch being silently dropped - for closing an unreachable one) instead
of relying on `__hash__`. That same traversal also has to tolerate a
node object referenced by more than one parent (a DAG, not just a tree):
nothing `normalize()`/`parse()` ever produce shares a node this way, but
nothing stops a caller from building one, and an early version of the
memory fix below evicted a shared child's key as soon as its *first*
parent read it, raising `KeyError` for its second parent - order-
dependent on which parent happened to be visited first, not on the
tree's actual shape. `_structural_key` now discovers the full reachable
node set up front and tracks, per node, how many distinct parents still
need to read it before its entry may be evicted.

The cap bounds recursion depth, not CPU time: parse time is still
quadratic in the length of a long unmatched word-character run (the
fieldname tagger's regex scans to end-of-input at each successive tag
position), measured at ~15s for a 40KB pathological query and ~34s for
60KB - order-of-magnitude, from one developer machine, like the figures
below; the durable claim is the ~4x-per-doubling curve. This fork's
tagger regex is upstream's plus `.` inside the field name (`[\w.]+:`
against `\w+:`, so dotted JSON subpaths tag), which adds no `:` to the run
being scanned and leaves the scan's character alone; the parity claim
rests on measuring the oracle rather than on the regexes being identical.
Real whoosh shows that same curve (measured ~1.1s at 10KB and ~4.0s at
20KB, against whoosh-compat's ~1.0s and ~3.9s). That cost is inherited
deliberately (a rewritten tagger regex would risk parity for a purely
adversarial input shape); the README's
host-contract section tells hosts to cap query length at their own
boundary instead.

*Other* super-linear costs on the same user-reachable path were not
inherited from whoosh and are fixed rather than tolerated, since none had a
parity argument protecting it. Wall-clock figures below are order-of-
magnitude, from one developer machine; the durable claim in each case is the
growth curve, not the seconds.

`glob_to_regex` was quadratic in the pattern length two independent ways,
and both are gone. It rescanned to the end of the pattern for every `[` with
no closing `]` (16K brackets cost ~6-12s, growing 4x per doubling): the
pattern's last `]` is now found once with `str.rfind` and passed into
`_translate_class`, which turns "is there a close from here" into a
comparison and leaves only the forward search that succeeds, and that one is
paid for once in total because the caller resumes past the class it
consumed. Separately, `_translate_class` rebuilt the entire pattern string
around each folded class body, so a pattern of many *closed* classes cost
O(classes x length) with no unmatched bracket involved (`[a]` x 50K plus a
200KB literal tail, which joins no class at all, cost ~11s; the tail's
influence is the signature of a per-class whole-string copy). The class is
now cut out and folded as its own slice, at fnmatch's offsets minus the
class start, so a class costs its own length and the same input is ~0.1s.
That second quadratic arrived with the per-character class-body fold and was
found by review of the first fix.

`DateParserPlugin`'s RFC3339 `Z` gate was a fork-only regex,
`^(?P<body>.*T.*)Z$`, whose two open-ended runs made the engine try every
pair of `T` positions before failing (a 200K-`T` bound cost minutes, where
real whoosh, which has no such gate, pays nothing); the leading run now
excludes `T`, which pins the split at the first one and matches the same
language.

All three are rewrites of *how* the answer is computed, not caps on what is
accepted: a length cap would have changed which queries work, and the
accepted language of each is pinned unchanged by differential checks against
the previous implementations (and, for the glob, against the
`fnmatch.translate` oracle that defines it).

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

One known caveat, inherited verbatim from the forked whoosh code and
confirmed identical in the pinned oracle: a `Wildcard`/`Prefix` leaf's
span covers only the wildcard *marker* character, not the merged pattern
(`inv*` yields `Prefix(startchar=3, endchar=4)`, just the `*`), because
`WildcardPlugin.do_wildcards` concatenates adjacent text nodes' text into
the wildcard node without ever widening its char range. A host using
spans for error highlighting should expect single-character spans for
these two leaf types (and for the `PATTERN_ON_NUMERIC`/
`PATTERN_ON_BOOLEAN_EXISTS`/`PATTERN_ON_SUBPATH` diagnostics built from
them; their `raw_value` does carry the full pattern as typed).

## 5. Extension points

**A new emitter.** Implement `ast.Visitor[T]` (subclass it and provide
`visit_<name>` methods for whichever node types matter to your backend;
unhandled types fall through to `generic_visit`, which raises
`NotImplementedError`) plus a small module-level `emit(node, ...)` function
following `emitters/tantivy_.py`'s shape. The AST and `FieldRegistry` are
the entire contract: a Meilisearch or Elasticsearch emitter needs no
changes to `parser/` or `ast.py`. Calling `ast.analyze()` before visiting is
optional, not required by the `Emitter` protocol (`emitters/base.py`): a
backend whose own query engine tokenizes server-side can skip it and visit
the raw, unanalyzed tree directly, the same way `TantivyEmitter.emit()`
chooses to call it and a hypothetically different emitter could choose not
to.

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
probes whether the installed `tantivy-py`'s `Query.term_query` can resolve a
JSON subpath directly, cached once per `FieldRegistry` (a
`weakref.WeakKeyDictionary`, module-level in `emitters/tantivy_.py`), not
once per emitter instance: `emit()` builds a fresh `TantivyEmitter` on every
call, so a cache on `self` never survived past that one call, and the real
probe query ran on every single `emit()` against any registry with a JSON
field. If unsupported, JSON-subpath term emission falls back to a strictly
escaped, single-leaf `index.parse_query()` call instead of the
fully-programmatic construction used everywhere else. This carve-out is
self-retiring: once
[tantivy-py#716](https://github.com/quickwit-oss/tantivy-py/pull/716) lands
and ships, the probe starts succeeding and the fallback branch simply stops
being taken. No code change is required in this library.
The probe itself queries `self.schema` (a specific index's schema), not
just the installed `tantivy-py` version in the abstract, so the cached
answer can go stale for **the same** `FieldRegistry` reused across
different `tantivy.Index`/schema generations (e.g. after a reindex) if
that changes the probe's outcome. The cache is not re-keyed per index:
`tantivy.Schema`/`tantivy.Index` are not weak-referenceable (confirmed
directly), so there is nothing to key on, and on tantivy-py 0.26 the
probe answers `False` for any real JSON schema regardless of content,
making the cached value invariant across index generations today; this
becomes a live staleness risk only once tantivy-py#716 ships and a
schema-content-dependent `True`/`False` split becomes possible.

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
   (this library's `normalize()` has its own dedicated unit tests).
   whoosh-compat's own side runs its raw parsed tree through the real
   `ast.analyze()` (the same function `TantivyEmitter.emit()` calls) before
   the structural comparison, so the harness has no second, hand-synchronized
   implementation of what analysis does to keep in sync with production
   behavior. Every allowed divergence must match an entry in
   `tests/differential/allowlist.py`,
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
