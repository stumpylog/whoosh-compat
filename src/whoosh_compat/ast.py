"""Query AST nodes and visitor pattern for whoosh-compat."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import fields as dataclass_fields
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


def _dedupe_key_children(node: Node) -> tuple[Node, ...]:
    """Returns the ``Node`` values among ``node``'s own ``compare=True``
    dataclass fields, in field-declaration order: exactly the fields the
    auto-generated ``__eq__``/``__hash__`` actually looks at, whether held
    directly (``Not.child``) or inside a tuple (``And.children``).

    Generic across every ``Node`` subclass (unlike ``_child_nodes``, which
    only covers the container types ``normalize`` itself walks): this also
    descends into leaf-adjacent fields like ``Boosted.child`` for the same
    reason, and is what lets :func:`_structural_key` build a subtree's key
    bottom-up without relying on the dataclasses' own recursive
    ``__hash__``.
    """
    children: list[Node] = []
    for f in dataclass_fields(node):
        if not f.compare:
            continue
        value = getattr(node, f.name)
        if isinstance(value, Node):
            children.append(value)
        elif isinstance(value, tuple):
            children.extend(v for v in value if isinstance(v, Node))
    return tuple(children)


# Every NaN encoded by _encode_field draws a fresh tag from here: a NaN
# never `==`-compares equal to anything, including another NaN with the
# identical value, so no two NaN encodings may ever be allowed to collide
# (see _encode_field's docstring for why this can't just key off id()).
_NAN_TAGS: itertools.count[int] = itertools.count()


def _encode_field(value: object, memo: dict[int, str]) -> str:
    """Encodes one already-normalized field value of a node into a string
    fragment, for :func:`_structural_key` to assemble.

    A ``Node`` value contributes its own already-computed entry from
    ``memo`` (populated bottom-up, never recomputed here); a tuple
    contributes each element's encoding, length-prefixed and joined, so a
    tuple boundary can never be confused with a field-value boundary (a
    field value that happens to contain the joining character does not
    create a false split).

    Anything else - the atomic-leaf fallback - is
    ``f"{type(value).__name__}:{value!r}"`` for almost everything, which
    is enough to distinguish it from a different type with a colliding
    ``repr()`` (e.g. the string ``"1"`` vs the int ``1``), and is exact
    (round-trips losslessly, matches ``==``) for every type actually
    reachable here except one: ``float``, the one place in this AST an
    atomic (non-``Node``) field sits directly on a composite node
    (``Boosted.boost``) rather than being handled by :func:`_leaf_key`'s
    fast path. Two ``float``-specific quirks get this wrong:

    * Two NaN floats, which never compare equal to *anything* via a bare
      ``==`` - not even to themselves, field-for-field, on this
      interpreter, as measured below - both ``repr()`` as ``"nan"`` and
      would wrongly encode as the *same* string, merging two nodes real
      node-equality treats as distinct (over-dedupe: silently drops one -
      the actual correctness risk this module's recursive-``__hash__``
      fix was about, just relocated to a coarser key instead of
      eliminated).
    * ``-0.0`` and ``0.0`` compare equal (``-0.0 == 0.0``) but ``repr()``
      differently, so they would wrongly encode as distinct.

    Both are handled explicitly, for ``float`` only. A NaN draws a fresh
    tag from ``_NAN_TAGS`` every time, so no two NaN encodings ever
    collide, no matter what: this deliberately does *not* key off
    ``id(value)`` the way ``__eq__`` gets to for a top-level "is this
    literally the same node" check (:func:`_dedupe` has its own such
    check, for that reason - see its docstring), because on at least one
    supported interpreter the field-level comparison a NaN participates
    in here does not get that shortcut - measured (via
    ``dis.dis(Boosted.__eq__)``) on CPython 3.14, whose slots-optimized
    dataclass codegen compares ``self.boost == other.boost`` directly (a
    bare ``float.__eq__`` call, no identity fast path) after a
    ``self is other`` prologue that only ever fires for the *whole*
    node, not a field nested inside it - so even the *same* NaN object
    nested inside two otherwise-identical composite nodes compares
    unequal there. This does **not** hold across every interpreter this
    library supports (3.11 through 3.14, measured on all four, not just
    the two endpoints): the split falls between 3.12 and 3.13, not at
    3.14. On CPython 3.11 and 3.12, the generated ``__eq__`` instead
    builds and compares a tuple of the fields, which *does* get
    ``PyObject_RichCompareBool``'s per-element identity shortcut - so the
    same same-object-NaN case compares *equal* on those two versions;
    3.13 and 3.14 both use the direct field-by-field compare described
    above, with no shortcut. An ``id(value)``-based tag would have
    matched 3.11/3.12 but silently over-deduped on 3.13/3.14 (or vice
    versa for a counter, depending which versions' behavior one tried to
    match) - a NaN-bearing key cannot be made to match ``__eq__`` exactly
    on every supported interpreter simultaneously with a design this
    simple. Always-unique per encounter (this function's actual choice)
    never over-dedupes on *any* version: 3.11/3.12's identity-based
    "equal" case just lands in the always-safe under-dedupe direction
    there instead (see :func:`_dedupe`'s docstring for why the merge that
    exact case is entitled to, at the *whole-node* level, is restored
    separately). A zero of either sign is normalized to plain ``0.0``; no
    other type reachable here needs the same scrutiny, but a future
    atomic field added to a composite ``Node`` would.
    """
    if isinstance(value, Node):
        return memo[id(value)]
    if isinstance(value, tuple):
        parts = [_encode_field(v, memo) for v in value]
        return "(" + "|".join(f"{len(p)}:{p}" for p in parts) + ")"
    if isinstance(value, float):
        if math.isnan(value):  # NaN: never `==`-equal to anything, not even itself
            return f"nan:{next(_NAN_TAGS)}"
        if value == 0.0:  # normalize -0.0 to 0.0: they compare equal but repr differently
            value = 0.0
        return f"num:{value!r}"
    return f"{type(value).__name__}:{value!r}"


def _leaf_key(node: Node) -> tuple[object, ...]:
    """Fast path for a node with no ``Node``-valued ``compare=True`` field
    (``_dedupe_key_children(node) == ()``): a plain tuple of its own
    field values, built directly rather than by routing through
    :func:`_structural_key`'s stack/string machinery.

    Safe because nothing in this tuple is itself a ``Node``: there is no
    subtree here for :func:`_structural_key`'s string flattening to
    protect against, no matter how deep this leaf's *siblings* happen to
    be. It is also more exact than :func:`_encode_field`'s fallback would
    be for the same fields, not just faster: the raw values go straight
    into the tuple, so Python's own ``==``/``hash`` resolve numeric-tower
    and NaN equality correctly with no canonicalization needed at all.
    This is the overwhelmingly common case :func:`_dedupe` sees in
    practice (a run of ``Term``/``Phrase``/etc. siblings, not a
    hand-built pathologically deep chain), so skipping the string
    serialization for it matters for the normal case's performance even
    though correctness only requires it for the composite case.
    """
    return (type(node), *(getattr(node, f.name) for f in dataclass_fields(node) if f.compare))


def _structural_key(root: Node) -> str:
    """Computes a string built from the same information the dataclasses'
    generated ``__eq__``/``__hash__`` uses (``compare=True`` fields, at
    every depth), without ever calling either.

    This is *equal for two nodes precisely when their fields are* for
    every case actually reachable here except one, deliberate exception,
    documented on :func:`_encode_field`: an atomic ``int``/``float``/
    ``bool`` value does not get numeric-tower canonicalization (``2`` and
    ``2.0`` encode differently, even though ``2 == 2.0``), because the one
    field this matters for (``Boosted.boost``) also carries a field type
    (``float``) with a real over-dedupe risk (NaN) that a
    canonicalization step aggravated in an earlier version of this
    function - see :func:`_encode_field`'s docstring for the full
    reasoning and why under-dedupe (kept as distinct siblings when
    ``==`` would have merged them - harmless, a redundant clause matches
    the same documents) is the direction this function accepts on the
    rare occasions its key is coarser than ``==``, never the reverse
    (silently dropping a distinct query branch). A hypothetical future
    atomic field type outside what :func:`_encode_field` already handles
    would need the same scrutiny before any exactness claim would hold of
    it.

    It also does *not* hold across two separate calls to this function
    for the same NaN-bearing node: :func:`_encode_field` deliberately
    gives every NaN a fresh, ever-incrementing tag, so
    ``_structural_key(n) == _structural_key(n)`` can be ``False``. This is
    not a hazard nothing relies on, quite the opposite - :func:`_dedupe`
    calls this once per sibling and *does* compare the results across
    those separate calls, via its shared ``seen`` set (that comparison,
    across calls, is precisely how two distinct siblings ever get
    compared to each other at all). What is true, and is the actual
    mechanism that makes this safe rather than a bug, is that each call's
    internal ``memo`` dict (mapping ``id(node) -> str`` for one call's own
    discovery pass) is never shared with another call - a stale lookup
    from a *previous* call's memo is not the failure mode here. The
    reason NaN siblings still behave correctly under this repeated
    cross-call comparison is that ``_NAN_TAGS`` is a single, global,
    ever-incrementing counter every call draws from: two different NaN
    *encounters*, whether in the same call or different ones, always draw
    different tags and so never spuriously compare equal - which is
    exactly "no two NaN nodes ever falsely dedupe," the property this
    module exists to guarantee. A future caller relying on
    ``_structural_key(n) == _structural_key(n)`` being ``True`` across
    two separate calls, for some purpose other than comparing distinct
    siblings against each other, would need to know it is not.

    Traverses iteratively (an explicit work stack, keyed by node identity,
    mirroring :func:`normalize`'s own traversal), so a node that is itself
    deep (e.g. a long ``Not`` chain appearing as one sibling among several)
    costs heap, not Python call-stack frames. This is what a call to
    ``hash(node)`` or ``node in some_set`` does not give you: the
    dataclasses' generated ``__hash__`` recurses through the whole subtree
    in native Python frames to compute a single int, so a sibling deep
    enough on its own can exceed the recursion limit even though nothing
    else in the same traversal is recursive.

    Each node's contribution is built as a single flat, length-prefixed
    string from its own fields plus its already-computed children's
    strings (not their nodes, and not nested containers of them), so no
    step here - construction or, later, hashing/comparing the final
    string - is asked to recurse through the tree's structure again: a
    string's own equality and hashing are computed over its flat
    character content, not over whatever tree shape produced it.

    The tree ``normalize()``/``parse()`` ever produce never shares a node
    object between two different parents, but a hand-built one is under
    no such obligation (nothing prevents ``And(children=(x, Not(child=x)))``
    for the same ``x`` object), so this function is written to tolerate a
    DAG, not just a tree - both for correctness and for memory. A single
    top-down stack visiting each child once, as soon as its parent needs
    it, gets both of those wrong for a shared node: whichever parent is
    processed first can evict the child's ``memo`` entry (see below)
    before a second, not-yet-processed parent reads it, raising
    ``KeyError`` - and the ``KeyError`` is order-dependent on which
    parent happens to be visited first, not on the tree's actual shape,
    which is exactly the kind of latent trap this module exists to
    remove, not add. So this runs in two passes instead:

    1. Discovery: an identity-keyed DFS (also iterative, for the same
       depth reason as everything else here) that visits every reachable
       node exactly once and records, per node, its own distinct children
       (``kids_of``) and, per node, the distinct parents that reference it
       (``parents_of``) - "distinct" meaning by identity, so a node
       referenced twice by the *same* parent (``AndNot(positive=x,
       negative=x)``) counts as one parent, not two.
    2. Combination: a worklist seeded with every node that has zero
       distinct children (the leaves), processed in an order where a node
       is only ever added to the worklist once every one of its distinct
       children has already been combined - guaranteeing, for a shared
       node with multiple parents, that its ``memo`` entry exists no
       matter which of its parents happens to run first, and that it is
       *processed* exactly once even though multiple parents read it,
       unlike a naive stack revisit, which would redo a shared subtree's
       own field-processing loop once per parent.

       That only bounds the number of times this loop *runs*, not the
       size of what it produces. Every node's own contribution still
       embeds its children's full text (by design - see above), so on a
       hand-built DAG where sharing *compounds* across levels (each
       level's node embeds two already-large strings that themselves
       overlap, e.g. ``And(children=(Not(child=n), Boosted(child=n,
       boost=2.0)))`` chained so each level's ``n`` is the previous
       level's whole node), the key string's own length still roughly
       doubles per level - measured: 43 nodes / 1.5M characters at depth
       14, 49 nodes / 5.9M characters at depth 16, 55 nodes / 23.7M
       characters at depth 18, i.e. linear node count but exponential
       string size. This is meaningfully better than the single-pass
       version it replaced (which redid the *work* exponentially too:
       0.12s vs 6.2s at depth 18 for the same input), but it is not
       solved, only the work-duplication half of it is. Not a live
       concern: ``normalize()``/``parse()`` never produce a DAG at all,
       let alone a compounding one, so this only bites a caller who
       hand-builds one on purpose.

    ``memo`` entries are still evicted as early as correctness allows,
    for the same reason as the single-pass version this replaced: a
    level's string embeds its entire subtree's text, so keeping every
    level's copy alive at once, all the way up a deep chain, would cost
    memory quadratic in depth (a still-heap-only, but still real, echo of
    the same "one sibling deep enough defeats the safeguard" shape this
    function exists to avoid on the call-stack side). The difference from
    the single-pass version is *when* eviction is safe: a node is only
    evicted once every one of its distinct parents (not just the first)
    has read it, tracked by a countdown (``remaining_reads``) seeded from
    ``len(parents_of[...])`` and decremented once per parent as that
    parent is combined. For a tree with no sharing at all (every node has
    at most one parent), this reduces to exactly the earlier behavior:
    each node is evicted right after its one and only parent reads it.
    """
    kids_of: dict[int, tuple[Node, ...]] = {}
    node_by_id: dict[int, Node] = {id(root): root}
    parents_of: dict[int, list[Node]] = {}
    unresolved: dict[int, int] = {}

    frontier = [root]
    discovered = {id(root)}
    while frontier:
        current = frontier.pop()
        kids = _dedupe_key_children(current)
        kids_of[id(current)] = kids
        distinct_kids: dict[int, Node] = {}
        for k in kids:
            distinct_kids.setdefault(id(k), k)
        unresolved[id(current)] = len(distinct_kids)
        for kid_id, k in distinct_kids.items():
            parents_of.setdefault(kid_id, []).append(current)
            if kid_id not in discovered:
                discovered.add(kid_id)
                node_by_id[kid_id] = k
                frontier.append(k)

    remaining_reads = {nid: len(parents) for nid, parents in parents_of.items()}
    memo: dict[int, str] = {}
    ready = [n for nid, n in node_by_id.items() if unresolved[nid] == 0]
    while ready:
        current = ready.pop()
        parts = [type(current).__name__]
        for f in dataclass_fields(current):
            if not f.compare:
                continue
            parts.append(_encode_field(getattr(current, f.name), memo))
        memo[id(current)] = "|".join(f"{len(p)}:{p}" for p in parts)
        for kid_id in {id(k) for k in kids_of[id(current)]}:
            remaining_reads[kid_id] -= 1
            if remaining_reads[kid_id] == 0:
                memo.pop(kid_id, None)
        for parent in parents_of.get(id(current), ()):
            unresolved[id(parent)] -= 1
            if unresolved[id(parent)] == 0:
                ready.append(parent)
    return memo[id(root)]


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

    The key is computed via :func:`_structural_key` (or, for a childless
    leaf, the cheaper :func:`_leaf_key`) rather than by putting ``n``
    itself into the set: a plain ``set`` would hash ``n`` with the
    dataclasses' generated (recursive) ``__hash__``, which is exactly the
    recursion :func:`normalize`'s explicit work stack exists to avoid,
    and a node deep enough on its own (not wide - depth, as a single
    sibling) defeats that work stack by recursing inside the ``set``
    operation instead of inside ``normalize``'s own traversal.

    A same-object identity check runs first, ahead of (and independent
    of) that key: this codebase's generated ``__eq__`` short-circuits to
    ``True`` when ``self is other``, before comparing any field - the one
    place real node equality treats two nodes as interchangeable *without
    consulting field values at all*. A structural key built from field
    values alone cannot reproduce that shortcut for a sibling that
    contains a NaN (:func:`_encode_field` deliberately makes every NaN
    encoding unique, since NaN never compares ``==`` to anything else,
    including a second occurrence of the identical float object nested
    inside two otherwise-identical composite nodes - see its docstring),
    so without this check, the exact same node object listed twice as a
    sibling would wrongly be kept as two "distinct" entries whenever it
    contains a NaN anywhere in its subtree. Every other case this check
    also short-circuits (an ordinary duplicate object reference with no
    NaN in it) was already being caught correctly by the key alone; this
    only changes NaN-bearing duplicates, and only in the narrow direction
    of restoring the merge real equality already grants them via
    ``self is other``.
    """
    seen: set[tuple[object, tuple[str, ...] | None, bool | None]] = set()
    seen_ids: set[int] = set()
    result: list[Node] = []
    for n in nodes:
        if id(n) in seen_ids:
            continue
        seen_ids.add(id(n))
        base: object = _leaf_key(n) if not _dedupe_key_children(n) else _structural_key(n)
        key: tuple[object, tuple[str, ...] | None, bool | None]
        if isinstance(n, Phrase):
            key = (base, n.words, n.analyzed)
        elif isinstance(n, Term):
            key = (base, None, n.analyzed)
        else:
            key = (base, None, None)
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

        Walks ``type(node).__mro__`` rather than dispatching on the exact
        concrete class name alone: a ``Node`` subclass with no
        ``visit_<its-own-name>`` method of its own (e.g. a caller-defined
        specialization of ``Term``) still reaches its nearest ancestor's
        visitor method instead of falling straight through to
        ``generic_visit``. Without this, any such subclass -- a
        structurally ordinary, legitimate node -- was indistinguishable
        from a genuinely unhandled shape, converting to
        ``AST_INVALID_SHAPE`` at the emitter (an internal error, HTTP 500)
        rather than being visited normally. The walk stops at (and
        includes) ``Node`` itself; a class not descended from ``Node`` at
        all still falls through to ``generic_visit``, unchanged.

        Args:
            node: The AST node to visit.

        Returns:
            The result of the visit method.
        """
        for cls in type(node).__mro__:
            method = getattr(self, "visit_" + cls.__name__.lower(), None)
            if method is not None:
                return method(node)
            if cls is Node:
                break
        return self.generic_visit(node)

    def generic_visit(self, node: Node) -> T:
        """Called for nodes without a specific visit_* method.

        Args:
            node: The AST node being visited.

        Raises:
            NotImplementedError: Always raised to indicate the node type is not handled.
        """
        raise NotImplementedError(f"No visitor method for {type(node).__name__}")


def _leaf_analyzed_texts(leaf: Term | Phrase, registry: FieldRegistry) -> tuple[str, ...]:
    """The analyzed token texts a single ``Term``/``Phrase`` leaf contributes.

    Runs the very same per-leaf rewrite :func:`analyze` runs (``analyzed``
    re-entry guard, zero-token drop, ``Multitoken`` handling included), one
    leaf at a time, and reads the tokens back out of the result. Analysing
    per leaf rather than whole-tree is what lets :func:`free_text_tokens`
    keep the *pre-analysis* structure, whose polarity analysis deliberately
    destroys (DIVERGENCES.md entry 23).

    The ``Multitoken.AND`` context passed here is not the enclosing group's
    combinator, which a per-leaf call cannot know. It does not need to be:
    context only picks between ``And`` and ``Or`` for a
    ``Multitoken.DEFAULT`` field, and both carry the identical token set.
    The context-independent modes (``FIRST``, ``PHRASE``, explicit
    ``AND``/``OR``) come from the field's own spec and are unaffected.
    """
    analyzed = (
        _analyze_term(leaf, registry, Multitoken.AND)
        if isinstance(leaf, Term)
        else _analyze_phrase(leaf, registry)
    )
    if isinstance(analyzed, Term):
        # A non-str text (a numeric or boolean term value) is never free
        # text, whatever field it sits on.
        return (analyzed.text,) if isinstance(analyzed.text, str) else ()
    if isinstance(analyzed, Phrase):
        return analyzed.words or ()
    if isinstance(analyzed, (And, Or)):
        return tuple(
            child.text
            for child in analyzed.children
            if isinstance(child, Term) and isinstance(child.text, str)
        )
    return ()  # Nothing(): the analyzer consumed every token.


def free_text_tokens(
    node: Node,
    *,
    registry: FieldRegistry,
    fields: Sequence[str],
    analyzed: bool = True,
) -> tuple[str, ...]:
    """Collect the free-text word tokens of ``node``, in first-appearance
    order, deduplicated.

    Answers "which plain words does this query search for?" for consumers
    building a secondary text clause from an already-parsed query (the
    motivating case: a fuzzy-matching blend that re-parses a word string
    through a backend's own query parser and must never receive query
    grammar). Only ``Term``/``Phrase`` leaves on the requested ``fields``
    contribute, and what they contribute is by default the field analyzer's
    output, verbatim (each contributing leaf is analyzed on its own, exactly
    as :func:`analyze` would analyze it, after the tree is passed through
    :func:`normalize`; both are no-ops on already-processed leaves). No
    query GRAMMAR survives into the result: no field prefixes, ranges,
    brackets, quotes or patterns. Token text itself is whatever the
    analyzer emits, never re-split here (an analyzer whose tokens contain
    spaces, e.g. shingle-style or the identity default, passes them
    through intact; re-splitting would corrupt exactly the analyzers the
    ``analyze()`` docstring warns about).

    Structural rules, chosen so the tokens reflect what the query asks FOR
    rather than everything it mentions:

    * ``Not`` subtrees and ``AndNot`` negative sides contribute nothing: a
      term the user excluded must not resurface in a matching clause. This
      holds for every shape of the tree *as parsed*, which is why the walk
      runs on that tree and analyzes leaf by leaf instead of analyzing the
      tree first: whole-tree :func:`analyze` drops an operand whose every
      token the analyzer consumed (DIVERGENCES.md entry 23), so an
      ``AndNot`` whose positive side was all stopwords would collapse to its
      own *negative* side and hand back a bare positive term the user had
      excluded. The rule is therefore conditional on ``node`` being the tree
      as parsed; see ``node``'s precondition below.
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
        node: the AST to collect from. **Must be the tree as parsed**
            (``ParseResult.ast``, or any tree that has not been through
            :func:`analyze`); :func:`normalize` having been applied is fine,
            and is what ``parse()`` already does. Passing an
            already-analyzed tree is not rejected, but it cannot answer the
            questions this function asks, and both modes degrade silently:
            polarity is gone, so a negated term can come back out (that is
            entry 23's collapse, already applied, and no walk can undo it),
            and the raw text is gone, so ``analyzed=False`` returns
            *analyzed* text in flat contradiction of its own name. There is
            no guard because there is nothing reliable to guard on: an
            analyzed tree is structurally a valid tree, and the ``analyzed``
            flags it carries are ``compare=False`` provenance, not a
            trustworthy input contract.
        registry: resolves field names and provides analyzers.
        fields: the field names (aliases allowed) whose leaves count as
            free text. Must be non-empty, and every name must resolve to a
            plain (non-subpath) TEXT or KEYWORD field; anything else is a
            host configuration error.
        analyzed: when ``True`` (the default), a contributing leaf yields
            the field analyzer's output. When ``False``, it yields the raw
            text it was parsed from, and the analyzer is never consulted at
            all. Pass ``False`` when the tokens are going back into a parser
            that will analyze them itself: analysis is not generally
            idempotent (a stemmer maps ``universities`` to ``univers`` and
            ``univers`` to ``univ``), so re-analyzing analyzed output
            searches for something the index does not contain.

            Which NODES contribute is structural and identical in both
            modes, with the single exception of the zero-token leaf below;
            polarity, patterns, kinds and dedupe never vary. What differs is
            the text: three differences a caller sizing its output should
            expect, and one hazard. All four are deliberate consequences of
            "the analyzer is never consulted":

            * A leaf whose analysis would be empty (an all-stopword value)
              still contributes its raw text: ``the`` yields ``('the',)``
              here and ``()`` analyzed. Deciding *membership* by the
              analyzer while refusing its *output* would be a half-analysis
              that this mode's whole contract denies, and it would make the
              result depend on a stopword list the caller opted out of. The
              re-parse downstream applies that list once, in its own index's
              terms, which is where it belongs. This is the one case where
              the two modes disagree about a node rather than about text.
            * A ``Phrase`` contributes its raw text as ONE entry, not one
              per word: ``"tax reports"`` yields ``('tax reports',)`` here
              and ``('tax', 'report')`` analyzed. Splitting it would be this
              function tokenizing, which it does not do in either mode.
            * A ``Term`` whose analyzer splits it contributes ONE entry
              here: ``alpha-beta`` yields ``('alpha-beta',)`` here and
              ``('alpha', 'beta')`` analyzed (whatever the field's
              ``Multitoken`` policy made of it). So an entry in this mode
              can contain whitespace and punctuation, and the count of
              entries is the count of leaves, not of words.
            * Raw text has not been through tokenization, so unlike the
              analyzed mode it can still contain characters (a colon, a
              hyphen, a bracket) that a *re-parse* would read as grammar,
              even though the query grammar around them is gone. A caller
              feeding these to another parser must quote or escape them.

            Dedupe applies to whatever is emitted, so two spellings that
            analyze to one token stay two entries here.

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
            # Named by what it resolves to, not only by what was typed: a
            # JSON field declaring a default subpath reaches here under its
            # bare name (``notes`` -> ``notes.note``), and calling that bare
            # name itself "a JSON subpath" would say something untrue.
            raise ValueError(
                f"fields names {name!r}, which resolves to JSON subpath {str(ref)!r},"
                " not a free-text (TEXT/KEYWORD) field"
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
    # pushed reversed so pops keep textual order. The walk is over the
    # normalized but UNANALYZED tree, so the structural rules above read the
    # polarity the user wrote; analysis happens per contributing leaf, in
    # _leaf_analyzed_texts.
    stack: list[Node] = [normalize(node)]
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
        elif isinstance(current, (Term, Phrase)) and is_wanted(current.field):
            if analyzed:
                for token in _leaf_analyzed_texts(current, registry):
                    add(token)
            elif isinstance(current, Phrase):
                # The phrase's raw text, as one entry: splitting it into
                # words here would be this function tokenizing, which is
                # exactly what the unanalyzed mode was asked not to do.
                add(current.text)
            elif isinstance(current.text, str):
                # A non-str text (a numeric or boolean term value) is never
                # free text, whatever field it sits on.
                add(current.text)
        # Not, Prefix/Wildcard, ranges, Every, Nothing, ErrorLeaf:
        # contribute nothing, deliberately (see the docstring's rules).
    return tuple(out)
