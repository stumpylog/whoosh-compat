"""Query AST nodes and visitor pattern for whoosh-compat."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Generic
from typing import TypeVar

from whoosh_compat.errors import Diagnostic
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken

T = TypeVar("T")


@dataclass(frozen=True, kw_only=True, slots=True)
class Node:
    """Base class for all AST nodes. startchar and endchar are keyword-only.

    They are excluded from equality/hashing (``compare=False``): two nodes
    that differ only in source-text position are considered equal. This
    keeps position metadata purely informational (for diagnostics) without
    forcing every AST comparison in tests/consumers to also track parser
    source-offset bookkeeping.
    """

    startchar: int | None = dataclass_field(default=None, compare=False)
    endchar: int | None = dataclass_field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Term(Node):
    """A single term query.

    ``analyzed`` is ``False`` for every ``Term`` produced by ``parse()``:
    ``text`` there is raw, unanalyzed query text (see the "analysis happens
    at emit time" invariant in ARCHITECTURE.md). :func:`analyze` sets it to
    ``True`` on any ``Term`` it constructs as an analysis result (whether a
    single surviving token from a TEXT/KEYWORD field, or unchanged because
    the field's kind never analyzes at all): once ``analyzed`` is ``True``,
    a second :func:`analyze` pass treats this node as an opaque leaf and
    never re-runs the field's analyzer over ``text`` again, no matter what
    characters ``text`` happens to contain. This is what makes
    ``analyze(analyze(x)) == analyze(x)`` hold *by construction* rather than
    by luck: a naive design that re-split an analyzed, space-joined string
    would silently corrupt tokens from a shingle/ngram-style analyzer whose
    own output tokens themselves contain spaces.

    Excluded from equality/hashing (``compare=False``), like ``startchar``/
    ``endchar``: it is analysis *provenance* bookkeeping, not semantic
    content, so an analyzed ``Term`` compares equal to an unanalyzed one
    carrying the same ``field``/``text`` (this is what lets the differential
    test harness compare whoosh-compat's post-:func:`analyze` tree directly
    against a plain, never-analyzed tree built from the oracle's own query
    objects).
    """

    field: FieldRef | None
    text: str | int | bool
    analyzed: bool = dataclass_field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class And(Node):
    """Intersection (AND) of child nodes."""

    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Or(Node):
    """Union (OR) of child nodes."""

    children: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Not(Node):
    """Negation (NOT) of a child node."""

    child: Node


@dataclass(frozen=True, slots=True)
class AndNot(Node):
    """Require positive, exclude negative."""

    positive: Node
    negative: Node


@dataclass(frozen=True, slots=True)
class AndMaybe(Node):
    """Require required, optionally include optional."""

    required: Node
    optional: Node


@dataclass(frozen=True, slots=True)
class Require(Node):
    """Score with scored, filter with filter_only."""

    scored: Node
    filter_only: Node


@dataclass(frozen=True, slots=True)
class Phrase(Node):
    """Phrase query with optional slop.

    ``text`` is the raw, unanalyzed phrase text as ``parse()`` produced it
    (quotes stripped, nothing else done to it); every ``Phrase`` from
    ``parse()`` has ``words is None`` and ``analyzed is False``.
    :func:`analyze` runs the field's analyzer over ``text`` and, for any
    surviving result (including a single token: a one-word phrase stays a
    ``Phrase``, matching whoosh's ``PhrasePlugin``, see
    :func:`_analyze_phrase`), replaces ``words`` with that explicit tuple
    and sets ``analyzed`` to ``True``, leaving ``text`` as informational
    only from that point on (a zero-token result is dropped, see
    :func:`analyze`'s docstring). Carrying the tokens as an explicit tuple,
    rather than a re-joined string an emitter would have to split again, is
    what makes :func:`analyze` idempotent by construction (amendment 1 of
    the analysis-pipeline design): a second pass sees ``analyzed=True`` and
    returns this node unchanged, never re-running the analyzer or
    re-splitting anything, so an analyzer whose own tokens contain spaces
    (a shingle/ngram style analyzer) cannot be corrupted by a join/split
    round trip.

    ``words`` and ``analyzed`` are excluded from equality/hashing
    (``compare=False``), like ``startchar``/``endchar``: they are analysis
    representation/provenance, not independent semantic content. ``text`` is
    kept in sync (space-joined tokens once analyzed) and stays the
    comparable field, so an analyzed ``Phrase`` still compares equal to a
    plain, never-analyzed one carrying the same joined ``text`` (this is
    what lets the differential test harness compare whoosh-compat's post-
    :func:`analyze` tree directly against a plain tree built from the
    oracle's own query objects, which has no ``words`` concept at all).
    """

    field: FieldRef | None
    text: str
    slop: int = 1
    words: tuple[str, ...] | None = dataclass_field(default=None, compare=False)
    analyzed: bool = dataclass_field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class Prefix(Node):
    """Prefix query."""

    field: FieldRef | None
    text: str


@dataclass(frozen=True, slots=True)
class Wildcard(Node):
    """Wildcard pattern query."""

    field: FieldRef | None
    pattern: str


@dataclass(frozen=True, slots=True)
class TermRange(Node):
    """Range query on term values."""

    field: FieldRef | None
    lo: str | None
    hi: str | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True, slots=True)
class NumericRange(Node):
    """Range query on numeric values."""

    field: FieldRef
    lo: int | None
    hi: int | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True, slots=True)
class DateRange(Node):
    """Range query on date values."""

    field: FieldRef
    lo: datetime | None
    hi: datetime | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True, slots=True)
class Every(Node):
    """Match all documents (optionally in a field)."""

    field: FieldRef | None = None


@dataclass(frozen=True, slots=True)
class Nothing(Node):
    """Match no documents."""


@dataclass(frozen=True, slots=True)
class Boosted(Node):
    """Apply a boost factor to a child node."""

    child: Node
    boost: float


@dataclass(frozen=True, slots=True)
class ErrorLeaf(Node):
    """Represent a parse error in the tree."""

    diagnostic: Diagnostic


def _dedupe(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
    """Remove duplicate nodes, preserving first-seen order.

    Duplicate means semantically interchangeable, which is ALMOST node
    equality: ``Phrase.words`` is excluded from ``__eq__``/``__hash__``
    (analysis provenance, like spans), but for a phrase it is genuinely
    result-bearing, since the emitter builds the positional
    ``phrase_query`` from ``words``, not ``text``. An analyzer whose
    tokens contain spaces (shingle-style) can therefore produce two
    equal-comparing phrases with different word tuples and different
    match sets; real whoosh's own ``Phrase.__eq__`` compares the word
    lists and keeps both. The ``analyzed`` flag is result-bearing the
    same way, one cell over: an analyzed ``Term``/``Phrase`` and an
    unanalyzed one with the same text compare equal, but the unanalyzed
    sibling would still be tokenized (and possibly split or dropped) by
    a later ``analyze()`` pass, so merging a mixed-flag pair silently
    picks one of two different downstream meanings. Pipeline-produced
    trees never mix flags (parse yields all-unanalyzed, analyze yields
    all-analyzed), so that half only guards hand-built trees. The dedupe
    key therefore extends node equality with ``(words, analyzed)``,
    leaving the equality contract itself unchanged.
    """
    seen: set[tuple[Node, tuple[str, ...] | None, bool | None]] = set()
    result: list[Node] = []
    for n in nodes:
        key: tuple[Node, tuple[str, ...] | None, bool | None]
        if isinstance(n, Phrase):
            key = (n, n.words, n.analyzed)
        elif isinstance(n, Term):
            key = (n, None, n.analyzed)
        else:
            key = (n, None, None)
        if key not in seen:
            seen.add(key)
            result.append(n)
    return tuple(result)


def _span_union(nodes: tuple[Node, ...]) -> tuple[int | None, int | None]:
    """Returns the (min startchar, max endchar) spanning all of ``nodes``.

    A node whose own span is unset (``startchar is None``) is skipped for
    this purpose rather than treated as "spans everything" or "spans
    nothing": it contributes no information either way. If none of
    ``nodes`` carry a span at all (e.g. an entirely hand-built subtree that
    never set one), the result is ``(None, None)``.
    """

    starts = [n.startchar for n in nodes if n.startchar is not None]
    ends = [n.endchar for n in nodes if n.endchar is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _child_nodes(node: Node) -> tuple[Node, ...]:
    """Returns the immediate child nodes ``normalize`` needs normalized
    before it can apply ``node``'s own rule, in the same order the
    recursive implementation used to visit them. Leaf types (and And/Or
    with no children) return ``()``.
    """

    if isinstance(node, (And, Or)):
        return node.children
    if isinstance(node, Not):
        return (node.child,)
    if isinstance(node, AndNot):
        return (node.positive, node.negative)
    if isinstance(node, AndMaybe):
        return (node.required, node.optional)
    if isinstance(node, Require):
        return (node.scored, node.filter_only)
    if isinstance(node, Boosted):
        return (node.child,)
    return ()


def _normalize_one(node: Node, children: tuple[Node, ...]) -> Node:
    """Applies ``node``'s own normalization rule given its *already
    normalized* children (``children``, in the same order ``_child_nodes``
    returned them). Pure combination step, no traversal: this is the part
    ``normalize``'s recursive predecessor did after its recursive calls
    returned.

    Span handling: whenever this rebuilds a node as a fresh
    instance representing the *same* subtree ``node`` stood for (unchanged
    structure, or a collapse to a ``Nothing``/``Every`` marker), the new
    instance carries ``node``'s own ``startchar``/``endchar``. When a
    branch instead returns one of the already-normalized ``children``
    verbatim (a single-child unwrap, or an And/Or/AndNot/AndMaybe/Require
    side dropping out), that child's own span is left alone rather than
    widened to ``node``'s span, matching how normalize already treats a
    fully-collapsed single-child And/Or. And/Or's flatten/merge case is the
    one exception: since children may have been absorbed from a nested
    same-type node (or dropped via dedupe/Nothing/Every filtering), the
    rebuilt node's span is instead the union (see ``_span_union``) of
    whatever ended up as its final children, not ``node``'s own span.
    """

    if isinstance(node, And):
        if not children:
            return Nothing()  # rule 7: empty group -> Nothing
        start, end = _span_union(children)
        if any(isinstance(c, Nothing) for c in children):
            return Nothing(startchar=start, endchar=end)  # rule 3: Nothing propagates through And
        flat: list[Node] = []
        for child in children:
            if isinstance(child, And):
                flat.extend(child.children)
            else:
                flat.append(child)
        had_every = any(isinstance(c, Every) and c.field is None for c in flat)
        flat = [c for c in flat if not (isinstance(c, Every) and c.field is None)]
        flat = list(_dedupe(tuple(flat)))
        if not flat:
            return (
                Every(startchar=start, endchar=end)
                if had_every
                else Nothing(startchar=start, endchar=end)
            )
        if len(flat) == 1:
            return flat[0]
        flat_start, flat_end = _span_union(tuple(flat))
        return And(children=tuple(flat), startchar=flat_start, endchar=flat_end)

    if isinstance(node, Or):
        if not children:
            return Nothing()  # rule 7: empty group -> Nothing
        start, end = _span_union(children)
        flat = []
        for child in children:
            if isinstance(child, Or):
                flat.extend(child.children)
            else:
                flat.append(child)
        if any(isinstance(c, Every) and c.field is None for c in flat):
            return Every(startchar=start, endchar=end)  # rule 6: Every absorbs Or siblings
        flat = [c for c in flat if not isinstance(c, Nothing)]
        flat = list(_dedupe(tuple(flat)))
        if not flat:
            return Nothing(startchar=start, endchar=end)
        if len(flat) == 1:
            return flat[0]
        flat_start, flat_end = _span_union(tuple(flat))
        return Or(children=tuple(flat), startchar=flat_start, endchar=flat_end)

    if isinstance(node, Not):
        (child,) = children
        if isinstance(child, Nothing):
            return Every(startchar=node.startchar, endchar=node.endchar)
        return Not(child=child, startchar=node.startchar, endchar=node.endchar)

    if isinstance(node, AndNot):
        positive, negative = children
        if isinstance(positive, Nothing):
            return Nothing(startchar=node.startchar, endchar=node.endchar)
        if isinstance(negative, Nothing):
            return positive
        return AndNot(
            positive=positive, negative=negative, startchar=node.startchar, endchar=node.endchar
        )

    if isinstance(node, AndMaybe):
        required, optional = children
        if isinstance(required, Nothing):
            return Nothing(startchar=node.startchar, endchar=node.endchar)
        if isinstance(optional, Nothing):
            return required
        return AndMaybe(
            required=required, optional=optional, startchar=node.startchar, endchar=node.endchar
        )

    if isinstance(node, Require):
        scored, filter_only = children
        if isinstance(scored, Nothing) or isinstance(filter_only, Nothing):
            return Nothing(startchar=node.startchar, endchar=node.endchar)
        return Require(
            scored=scored, filter_only=filter_only, startchar=node.startchar, endchar=node.endchar
        )

    if isinstance(node, Boosted):
        (child,) = children
        boost = node.boost
        if isinstance(child, Nothing):
            return Nothing(startchar=node.startchar, endchar=node.endchar)
        if isinstance(child, Boosted):
            boost = child.boost * boost
            child = child.child
        if boost == 1.0:
            return child
        return Boosted(child=child, boost=boost, startchar=node.startchar, endchar=node.endchar)

    return node


def normalize(node: Node) -> Node:
    """Normalize an AST node into canonical form (pure, bottom-up).

    Applies flattening of nested same-type groups, Nothing/Every
    propagation, duplicate-sibling dedupe, empty-group collapse, single-child
    unwrap, and boost merging/stripping.

    Traverses iteratively (an explicit work stack, keyed by node identity)
    rather than recursively, so a pathologically deep or wide tree costs
    heap, not Python call-stack frames: a naive recursive postorder walk
    here used to roughly halve the query nesting depth ``parse()`` could
    tolerate before ``RecursionError``, since every parenthesized level
    already cost frames in the parser itself before ever reaching this
    function. The actual per-node rules live in
    :func:`_normalize_one`; this function only handles the postorder
    scheduling.

    Args:
        node: The AST node to normalize.

    Returns:
        The normalized node.
    """

    # memo maps id(original node) -> its normalized replacement, once known.
    # Keyed by identity rather than structural equality: two structurally
    # equal but distinct node objects are recomputed independently (cheap,
    # and correct either way), avoiding any assumption that equal nodes are
    # interchangeable during the walk itself.
    memo: dict[int, Node] = {}
    # Each stack entry is (node, children_are_memoized). A node is pushed
    # once as (node, False); if it has children, they're pushed (each as
    # (child, False)) followed by re-pushing (node, True) so the node is
    # revisited only after all its children have been normalized.
    stack: list[tuple[Node, bool]] = [(node, False)]
    while stack:
        current, children_ready = stack.pop()
        kids = _child_nodes(current)
        if children_ready or not kids:
            normalized_kids = tuple(memo[id(k)] for k in kids)
            memo[id(current)] = _normalize_one(current, normalized_kids)
        else:
            stack.append((current, True))
            for k in kids:
                stack.append((k, False))
    return memo[id(node)]


def _leaf_tokens(
    field: FieldRef | None, text: object, registry: FieldRegistry
) -> tuple[list[str], FieldSpec] | None:
    """Tokens ``text`` (addressed at ``field``) analyzes to, plus the spec
    that produced them, or ``None`` when ``field``'s kind is not subject to
    analysis at all.

    This is the closed-matrix kind-dispatch rule stated once, here: only
    TEXT/KEYWORD fields, plain or addressed through a registered JSON
    field's subpath, ever get tokenized or become eligible for zero-token
    dropping. U64/DATE/DATETIME/BOOLEAN_EXISTS terms, and a JSON-kind field
    addressed without a subpath (which cannot reach a ``Term``/``Phrase``
    leaf at all, see ``FieldRegistry.make_ref``), are returned unanalyzed
    regardless of whether their spec happens to carry an ``analyzer``.

    Returns ``None`` (rather than raising) for an unresolvable field: an
    unfielded leaf, or one naming a field/subpath the registry doesn't
    know. That is not this function's job to diagnose; leaving the node
    untouched here means the emitter's own field resolution raises the
    documented error at emit time, exactly as it would for a raw,
    never-analyzed tree.
    """
    if field is None:
        return None
    resolved = registry.resolve(field)
    if resolved is None:
        return None
    spec = resolved.spec
    if field.json_path is None and spec.kind not in (FieldKind.TEXT, FieldKind.KEYWORD):
        return None
    value = str(text)
    tokens = ([value] if value else []) if spec.analyzer is None else list(spec.analyzer(value))
    return tokens, spec


def _analyze_term(node: Term, registry: FieldRegistry, ctx: Multitoken) -> Node:
    """Rewrite one raw ``Term`` leaf, or return it unchanged.

    ``node.analyzed`` guards re-entry: an already-analyzed ``Term`` is
    returned as-is, never re-tokenized (see ``Term``'s docstring). A
    zero-token result becomes ``Nothing()``; a single-token result stays a
    ``Term`` (marked analyzed); a multi-token result becomes ``And``/``Or``/
    ``Phrase`` per ``spec.multitoken`` (``ctx``, the enclosing group's
    combinator, when the field is configured ``Multitoken.DEFAULT``).
    """
    if node.analyzed:
        return node
    found = _leaf_tokens(node.field, node.text, registry)
    if found is None:
        return node
    tokens, spec = found
    span = {"startchar": node.startchar, "endchar": node.endchar}
    field = node.field
    if not tokens:
        return Nothing(**span)
    if len(tokens) == 1:
        return Term(field=field, text=tokens[0], analyzed=True, **span)

    mode = spec.multitoken
    if mode is Multitoken.DEFAULT:
        mode = ctx
    if mode is Multitoken.FIRST:
        return Term(field=field, text=tokens[0], analyzed=True, **span)
    if mode is Multitoken.PHRASE:
        return Phrase(
            field=field, text=" ".join(tokens), words=tuple(tokens), analyzed=True, slop=1, **span
        )
    children = tuple(Term(field=field, text=t, analyzed=True, **span) for t in tokens)
    if mode is Multitoken.OR:
        return Or(children=children, **span)
    return And(children=children, **span)  # Multitoken.AND, and the DEFAULT-at-top-level case


def _analyze_phrase(node: Phrase, registry: FieldRegistry) -> Node:
    """Rewrite one raw ``Phrase`` leaf, or return it unchanged.

    Never consults ``Multitoken``: a quoted phrase's words are the phrase,
    not independent tokens a combinator picks among (unlike a multi-token
    bare ``Term`` value). A zero-token result becomes ``Nothing()`` (matching
    the enclosing-group-drop rule every other zero-token leaf gets); a
    surviving result of any length (including exactly one token) stays a
    ``Phrase``, matching real whoosh's own ``PhrasePlugin``, which always
    builds a ``Phrase`` query object regardless of word count and never
    self-collapses a one-word phrase into a plain term at the AST level
    (only the *emitter*, a backend execution detail, treats a one-word
    phrase query and an equivalent term query as interchangeable; see
    ``visit_phrase``). ``text`` is set to the analyzed, space-joined tokens
    (matching ``text``'s meaning on an unanalyzed ``Phrase``, and how a real
    whoosh ``Phrase`` query's own words read); the emitter never reads it
    once ``words`` is populated, so this is informational only, not a
    join a future analyzer pass could ever be asked to re-split (idempotence
    still comes entirely from the ``analyzed`` flag guard above, not from
    what ``text`` happens to contain).
    """
    if node.analyzed:
        return node
    found = _leaf_tokens(node.field, node.text, registry)
    if found is None:
        return node
    tokens, _spec = found
    span = {"startchar": node.startchar, "endchar": node.endchar}
    field = node.field
    if not tokens:
        return Nothing(**span)
    return Phrase(
        field=field,
        text=" ".join(tokens),
        words=tuple(tokens),
        analyzed=True,
        slop=node.slop,
        **span,
    )


def _analyze_binary_drop(
    node: AndNot | AndMaybe | Require,
    left_attr: str,
    right_attr: str,
    new_left: Node,
    new_right: Node,
) -> Node | None:
    """DIVERGENCES.md entry 23's uniform "zero-token operand" rule for
    ``AndNot``/``AndMaybe``/``Require``, applied once here for all three
    instead of duplicated per node type.

    A side that *newly* collapsed to ``Nothing()`` during this analysis pass
    (as opposed to one that already was ``Nothing()`` in the input tree,
    which is a genuine, pre-existing empty operand that whoosh's own
    ``normalize()`` algebra poisons the combinator for, see DIVERGENCES.md
    entry 27) simply drops out, leaving the other side standing alone: not
    the whoosh-matching poison/absorb rule ``_normalize_one`` would apply.
    This is the same "the survivor stands alone regardless of which side
    dropped" rule the pre-refactor emitter's ``_lone_operand`` implemented
    at emit time; it now runs once, structurally, during analysis instead.

    Returns ``None`` when neither side newly dropped, telling the caller to
    fall back to the ordinary ``_normalize_one`` rule (this rule does not
    apply to a genuinely pre-existing ``Nothing()`` operand).
    """
    orig_left = getattr(node, left_attr)
    orig_right = getattr(node, right_attr)
    left_dropped = isinstance(new_left, Nothing) and not isinstance(orig_left, Nothing)
    right_dropped = isinstance(new_right, Nothing) and not isinstance(orig_right, Nothing)
    if not (left_dropped or right_dropped):
        return None
    if left_dropped and right_dropped:
        return Nothing(startchar=node.startchar, endchar=node.endchar)
    return new_right if left_dropped else new_left


def _analyze_combine(
    node: Node, children: tuple[Node, ...], registry: FieldRegistry, ctx: Multitoken
) -> Node:
    """Combine one node with its already-analyzed-and-normalized
    ``children`` into ``node``'s replacement, dispatching on ``node``'s own
    type. This is :func:`analyze`'s per-node step, run bottom-up: ``Term``/
    ``Phrase`` leaves are rewritten by analysis; every other node with
    children is rebuilt from the new ones and immediately collapsed the same
    way :func:`normalize` would (reusing :func:`_normalize_one` directly for
    every combinator except ``AndNot``/``AndMaybe``/``Require``, which get
    DIVERGENCES.md entry 23's uniform survivor rule first, see
    :func:`_analyze_binary_drop`, falling back to the ordinary algebra when
    that rule doesn't apply). Interleaving the collapse into the same walk
    (rather than a separate pass afterward) is what lets
    :func:`_analyze_binary_drop` tell a genuinely pre-existing ``Nothing()``
    operand apart from one that only became empty during this very pass:
    by the time a parent is combined, any child subtree that fully emptied
    out (however deeply nested) has already collapsed to a literal
    ``Nothing()``. A leaf with no children of its own (``Every``,
    ``Nothing``, ``ErrorLeaf``, the range types, ``Wildcard``, ``Prefix``)
    falls through unchanged, since analysis never has anything to do for
    those kinds.
    """
    if isinstance(node, Term):
        return _analyze_term(node, registry, ctx)
    if isinstance(node, Phrase):
        return _analyze_phrase(node, registry)
    if isinstance(node, (AndNot, AndMaybe, Require)):
        attrs = {AndNot: ("positive", "negative"), AndMaybe: ("required", "optional")}.get(
            type(node), ("scored", "filter_only")
        )
        left, right = children
        override = _analyze_binary_drop(node, attrs[0], attrs[1], left, right)
        if override is not None:
            return override
    if isinstance(node, And):
        # normalize()'s own And rule poisons the whole group on *any*
        # Nothing() child (rule 3: real whoosh's "an impossible clause makes
        # the whole conjunction impossible" algebra, e.g. a genuinely empty
        # range). That is the wrong rule for a child that only became
        # Nothing() *here*, during analysis, because its own field's
        # analyzer consumed every token (an all-stopword value): whoosh's
        # actual behavior for that case is to drop the value as though it
        # was never typed, not to make the whole enclosing And impossible,
        # the same "discovered here, not a genuine impossibility" distinction
        # ``_analyze_binary_drop`` draws for AndNot/AndMaybe/Require. So a
        # newly-dropped child is filtered out of the list before the
        # ordinary algebra ever sees it; a genuinely pre-existing Nothing()
        # (one that was already in ``node.children`` before this pass)
        # still poisons, unchanged. ``Or`` needs no matching override:
        # normalize()'s own Or rule already drops any Nothing() child,
        # newly-dropped or not, which is already the correct behavior here.
        kept = tuple(
            new
            for orig, new in zip(node.children, children, strict=True)
            if not (isinstance(new, Nothing) and not isinstance(orig, Nothing))
        )
        return _normalize_one(node, kept)
    if isinstance(node, (Or, Not, AndNot, AndMaybe, Require, Boosted)):
        return _normalize_one(node, children)
    return node


def analyze(
    node: Node, registry: FieldRegistry, *, default_mode: Multitoken = Multitoken.AND
) -> Node:
    """Resolve every TEXT/KEYWORD ``Term``/``Phrase`` leaf's field analysis,
    turning a raw, unanalyzed tree into one an emitter can visit as a purely
    structural tree, with no token analysis or drop decisions of its own.

    This is a tantivy-emitter-agnostic pipeline stage, not tantivy-specific
    itself: a multi-token ``Term`` value becomes ``And``/``Or``/``Phrase``
    per the field's resolved ``Multitoken`` mode; a zero-token result (all
    stopwords, or shorter than the analyzer's minimum size) drops out of its
    enclosing group entirely, the same way an empty parenthesized group
    already does at parse time; a quoted ``Phrase`` is tokenized the same
    way, staying a ``Phrase`` for any surviving token count including one
    (matching real whoosh's ``PhrasePlugin``, which never self-collapses a
    one-word phrase; see :func:`_analyze_phrase`'s docstring). Fields
    outside the TEXT/KEYWORD/JSON-subpath kinds (U64, DATE, DATETIME,
    BOOLEAN_EXISTS, a bare JSON field) are never analyzed or dropped,
    matching the closed kind-dispatch matrix ARCHITECTURE.md documents.

    ``default_mode`` resolves ``Multitoken.DEFAULT`` for a term with no
    enclosing And/Or group to inherit from (a single top-level multi-token
    term). Every other ``Multitoken.DEFAULT`` term instead follows its
    nearest enclosing group's own combinator (an ``Or`` context resolves to
    OR, an ``And`` context to AND), matching DIVERGENCES.md entry 15's
    documented, position-dependent design (deliberately not whoosh's own
    fixed-parser-default-group behavior). ``Not``/``AndNot``/``AndMaybe``/
    ``Require``/``Boosted`` are transparent to this context: a term inside
    ``NOT (foo bar)`` with no other enclosing group still resolves against
    ``default_mode``, not against a group that isn't actually there.

    A ``NOT`` of a term that analyzes to zero tokens is a deliberately
    named case, not an accident: analysis drops the term, leaving
    ``Not(Nothing())``, which :func:`normalize`'s pre-existing
    ``Not(Nothing) -> Every()`` rule then turns into "matches everything",
    reproducing DIVERGENCES.md entry 23's documented divergence from real
    whoosh (whose own ``Not(NullQuery)`` stays ``NullQuery``) as the natural
    consequence of this pipeline's ordering, not as special-cased behavior
    to preserve. ``AndNot``/``AndMaybe``/``Require`` get one further,
    explicit rule (also entry 23): an operand that newly drops to zero
    tokens during this analysis pass lets its sibling stand alone, uniformly
    regardless of which side dropped; this differs from a genuinely
    pre-existing empty operand (entry 27's poison/absorb algebra, which
    still applies unchanged) purely by *when* the emptiness was discovered,
    a distinction :func:`_analyze_binary_drop` draws directly.

    Idempotent by construction (:func:`Term`/:func:`Phrase`'s ``analyzed``
    flag), not by relying on the host's ``analyzer`` callable happening to
    be a fixed point on its own output: ``analyze(analyze(x), registry) ==
    analyze(x, registry)`` for any ``x`` and ``registry``, since a node
    already marked analyzed is never re-tokenized or re-split.

    Traverses iteratively, mirroring :func:`normalize`'s own explicit work
    stack, so a pathologically deep tree costs heap rather than Python
    call-stack frames, exactly like every other stage in this pipeline that
    walks a parsed tree.

    Args:
        node: The AST node to analyze. Normally already normalized (the
            pipeline calls this as ``analyze(normalize(node), ...)``); a
            not-yet-normalized tree still analyzes correctly, since this
            function begins by normalizing its input (making that promise
            true by construction: a pre-existing ``Nothing`` wrapped in a
            group is collapsed to a literal ``Nothing`` *before* the
            analysis pass, so the newly-dropped-vs-pre-existing
            distinction the entry-23/entry-27 rules turn on never sees a
            group collapse of its own making) and ends by normalizing its
            own result.
        registry: Describes the known fields, their kinds, and their
            analyzers/``multitoken`` policy.
        default_mode: The ``Multitoken`` mode a ``Multitoken.DEFAULT``-
            configured field's term resolves to when it has no enclosing
            And/Or group to inherit from.

    Returns:
        A plain ``ast.Node`` tree, already normalized, with every
        TEXT/KEYWORD leaf's analysis fully resolved.
    """

    # Normalize first: see the Args docstring above for why this is
    # load-bearing (the entry-23/entry-27 distinction), not just tidiness.
    node = normalize(node)

    # Single bottom-up pass, mirroring normalize()'s own memoized
    # work-stack traversal, except the per-node combine step is
    # _analyze_combine (leaf rewriting plus interleaved normalization)
    # instead of _normalize_one alone. The Multitoken context (AND/OR)
    # applicable to a DEFAULT-configured term's position travels WITH each
    # work item rather than living in a separate id-keyed side table: a
    # frozen node object legitimately aliased at two tree positions with
    # different enclosing combinators (value semantics invite object
    # reuse) then gets one analysis per (object, context) pair instead of
    # whichever context a traversal recorded last. And/Or set their
    # children's context to their own combinator; every other combinator
    # (Not/AndNot/AndMaybe/Require/Boosted) passes its own context through
    # unchanged, since none of them are themselves a combining group a
    # term could inherit AND/OR-ness from.
    memo: dict[tuple[int, Multitoken], Node] = {}
    work: list[tuple[Node, Multitoken, bool]] = [(node, default_mode, False)]
    while work:
        current, ctx, children_ready = work.pop()
        kids = _child_nodes(current)
        if isinstance(current, And):
            child_ctx = Multitoken.AND
        elif isinstance(current, Or):
            child_ctx = Multitoken.OR
        else:
            child_ctx = ctx
        if children_ready or not kids:
            analyzed_kids = tuple(memo[(id(k), child_ctx)] for k in kids)
            memo[(id(current), ctx)] = _analyze_combine(current, analyzed_kids, registry, ctx)
        else:
            work.append((current, ctx, True))
            for k in kids:
                work.append((k, child_ctx, False))

    return normalize(memo[(id(node), default_mode)])


class Visitor(Generic[T]):
    """Base visitor for traversing AST nodes."""

    def visit(self, node: Node) -> T:
        """Dispatch to the appropriate visit_* method based on node type.

        Args:
            node: The AST node to visit.

        Returns:
            The result of the visit method.
        """
        method_name = "visit_" + type(node).__name__.lower()
        method = getattr(self, method_name, self.generic_visit)
        return method(node)

    def generic_visit(self, node: Node) -> T:
        """Called for nodes without a specific visit_* method.

        Args:
            node: The AST node being visited.

        Raises:
            NotImplementedError: Always raised to indicate the node type is not handled.
        """
        raise NotImplementedError(f"No visitor method for {type(node).__name__}")


def free_text_tokens(
    node: Node,
    *,
    registry: FieldRegistry,
    fields: Sequence[str],
) -> tuple[str, ...]:
    """Collect the free-text word tokens of ``node``, in first-appearance
    order, deduplicated.

    Answers "which plain words does this query search for?" for consumers
    building a secondary text clause from an already-parsed query (the
    motivating case: a fuzzy-matching blend that re-parses a word string
    through a backend's own query parser and must never receive query
    grammar). Only ``Term``/``Phrase`` leaves on the requested ``fields``
    contribute, and what they contribute is the field analyzer's output,
    verbatim (the tree is passed through :func:`normalize` and
    :func:`analyze` first, both no-ops on already-processed trees). No
    query GRAMMAR survives into the result: no field prefixes, ranges,
    brackets, quotes or patterns. Token text itself is whatever the
    analyzer emits, never re-split here (an analyzer whose tokens contain
    spaces, e.g. shingle-style or the identity default, passes them
    through intact; re-splitting would corrupt exactly the analyzers the
    ``analyze()`` docstring warns about).

    Structural rules, chosen so the tokens reflect what the query asks FOR
    rather than everything it mentions:

    * ``Not`` subtrees and ``AndNot`` negative sides contribute nothing: a
      term the user excluded must not resurface in a matching clause.
    * ``AndMaybe`` and ``Require`` contribute both sides (both express
      positive intent, whether or not they score).
    * ``Boosted`` is transparent; ``And``/``Or`` recurse.
    * Pattern leaves (``Prefix``/``Wildcard``) contribute nothing even on a
      requested field: a pattern is not a word, and analysis never ran on
      it (the analyzer/pattern_normalizer seam).
    * Range/``Every``/``Nothing``/``ErrorLeaf`` leaves and JSON-subpath
      terms contribute nothing.
    * A word the multifield expansion copied onto several default fields
      counts once (dedupe is by token text).

    Args:
        node: the AST to collect from (typically ``ParseResult.ast``).
        registry: resolves field names and provides analyzers.
        fields: the field names (aliases allowed) whose leaves count as
            free text. Must be non-empty, and every name must resolve to a
            plain (non-subpath) TEXT or KEYWORD field; anything else is a
            host configuration error.

    Raises:
        ValueError: ``fields`` is empty, names an unknown field or subpath,
            or names a field whose kind is not TEXT/KEYWORD. Host
            configuration mistakes raise eagerly, same as ``parse()``.
    """

    if not fields:
        raise ValueError("fields must not be empty")
    wanted: set[str] = set()
    for name in fields:
        ref = registry.make_ref(name)
        if ref is None:
            if registry.is_bare_json_field(name):
                # Known JSON field, but only its subpaths are addressable,
                # and those are not free-text fields either; distinguish it
                # from a genuinely unknown name.
                raise ValueError(
                    f"fields names {name!r}, a JSON field, which is not a"
                    " free-text (TEXT/KEYWORD) field"
                )
            raise ValueError(f"fields names unknown field {name!r}")
        if ref.json_path is not None:
            raise ValueError(
                f"fields names JSON subpath {name!r}, which is not a free-text (TEXT/KEYWORD) field"
            )
        resolved = registry.resolve(ref)
        if resolved is None or resolved.spec.kind not in (FieldKind.TEXT, FieldKind.KEYWORD):
            raise ValueError(
                f"fields names {name!r}, which is not a free-text (TEXT/KEYWORD) field"
            )
        wanted.add(ref.name)

    def is_wanted(ref: FieldRef | None) -> bool:
        return ref is not None and ref.json_path is None and ref.name in wanted

    out: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            out.append(text)

    # Iterative left-to-right preorder (matching normalize()'s heap-not-
    # stack-frames rationale for pathologically deep trees): children are
    # pushed reversed so pops keep textual order.
    stack: list[Node] = [analyze(normalize(node), registry)]
    while stack:
        current = stack.pop()
        if isinstance(current, (And, Or)):
            stack.extend(reversed(current.children))
        elif isinstance(current, Boosted):
            stack.append(current.child)
        elif isinstance(current, AndNot):
            stack.append(current.positive)
        elif isinstance(current, AndMaybe):
            stack.append(current.optional)
            stack.append(current.required)
        elif isinstance(current, Require):
            stack.append(current.filter_only)
            stack.append(current.scored)
        elif isinstance(current, Term):
            # A non-str text (a numeric or boolean term value) is never
            # free text, whatever field it sits on.
            if is_wanted(current.field) and isinstance(current.text, str):
                add(current.text)
        elif isinstance(current, Phrase) and is_wanted(current.field) and current.words:
            for word in current.words:
                add(word)
        # Not, Prefix/Wildcard, ranges, Every, Nothing, ErrorLeaf:
        # contribute nothing, deliberately (see the docstring's rules).
    return tuple(out)
