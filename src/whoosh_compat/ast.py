"""Query AST nodes and visitor pattern for whoosh-compat."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Generic
from typing import TypeAlias
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


@dataclass(frozen=True, slots=True)
class Fuzzy(Node):
    """Fuzzy (edit-distance) term query.

    Emit-only: never produced by parse() (no fuzzy syntax is registered in
    this library's parser plugin set), always hand-built by a caller, either
    in a tree passed to emit() or placed around a parsed leaf through
    analyze()'s rewrite_leaf hook. Unlike Term/Phrase/Prefix/Wildcard,
    `field` is required, not optional: there is no defined "expand across
    default search fields" behavior for a fuzzy leaf. A caller wanting fuzzy
    matching across several fields builds an Or of several explicitly
    fielded Fuzzy nodes.

    `distance` and `prefix` map directly onto
    `tantivy.Query.fuzzy_term_query`'s `distance`/`prefix` parameters.
    `prefix=True` matches every indexed term that *starts with* something
    within `distance` edits of `text` ("tok" matches "tokyo"), so it
    widens the match. It is not whoosh's `FuzzyTerm.prefixlength`, which
    narrows it by requiring the first N characters to match exactly.
    `transposition_cost_one` is not exposed here: it is hardcoded `True`
    at the emitter (tantivy's own default, and the more typo-forgiving
    behavior), see emitters/tantivy_.py's `visit_fuzzy`.
    """

    field: FieldRef
    text: str
    distance: int = 1
    prefix: bool = False


# Tags an interned child reference inside a dedupe key. Private, so no
# field value a caller builds can ever equal a reference.
_REF = object()

# Atom types that are always hashable and equal to themselves, checked by
# exact type so a subclass with its own __eq__ still gets the full checks.
_PLAIN_ATOMS: frozenset[type] = frozenset({str, int, bool, type(None)})

_COMPARE_FIELDS: dict[type, tuple[str, ...]] = {}


def _compare_fields(cls: type) -> tuple[str, ...]:
    """The ``compare=True`` field names of ``cls``, in declaration order,
    cached per class (class metadata only, never nodes)."""
    names = _COMPARE_FIELDS.get(cls)
    if names is None:
        names = tuple(f.name for f in dataclass_fields(cls) if f.compare)
        _COMPARE_FIELDS[cls] = names
    return names


def _field_ref_key(value: FieldRef) -> tuple[object, ...] | None:
    """The key for a ``FieldRef`` built entirely from plain strings,
    shared by :func:`_atom_key` and :func:`_plain_term_key`: ``(FieldRef,
    value)``. Safe with no ``repr`` check because such a ``FieldRef``
    prints the same exactly when it compares equal (both fields are
    ``str``), unlike an arbitrary value. Returns ``None`` when ``name`` or
    ``json_path`` might not be a plain ``str`` (nothing stops a caller from
    building a ``FieldRef`` with another type there, including something
    unhashable), so the general path below can fall back correctly.
    """
    if type(value.name) is str and (value.json_path is None or type(value.json_path) is str):
        return (FieldRef, value)
    return None


def _atom_key(value: object) -> object:
    """The key of one field value that is neither a ``Node`` nor a tuple.

    ``(type, value, repr(value))`` when the value can stand for itself in a
    set: it hashes, it equals itself, and it prints. Anything else (NaN of
    any numeric type, an unhashable value, a value whose checks or ``repr``
    raise) gets a fresh ``object()``, equal only to itself, so it never
    matches another sibling. Keeping the type in the key keeps ``1``,
    ``True`` and ``1.0`` apart even though they compare ``==``: as ``Term``
    text they search for different terms. The ``repr`` check keeps two
    equal-but-differently-printed values of the same type apart too (two
    ``Decimal``s with different exponents, the same instant in two time
    zones, a ``datetime`` differing only in ``fold``, ``-0.0`` versus
    ``0.0``): ``analyze()`` searches ``str(value)``, so merging them would
    silently drop whichever branch's spelling did not survive. A ``str``/
    ``int``/``bool``/``None`` or a ``FieldRef`` of plain strings never needs
    this check (see :func:`_field_ref_key`): for those, equal values always
    print the same.
    """
    kind = type(value)
    if kind in _PLAIN_ATOMS:
        return (kind, value)
    if isinstance(value, FieldRef) and kind is FieldRef:
        ref_key = _field_ref_key(value)
        if ref_key is not None:
            return ref_key
    try:
        hash(value)
        reflexive = bool(value == value)  # noqa: PLR0124 - NaN-like values are not equal to themselves
        printed = repr(value)
    except Exception:  # noqa: BLE001 - an arbitrary caller value's hash/==/repr can raise anything
        return object()
    return (kind, value, printed) if reflexive else object()


class _Interner:
    """Assigns each distinct node structure a small ``int`` for
    :func:`_dedupe`, once per public ``normalize()``/``analyze()`` call.

    Two nodes get the same ``int`` exactly when they are the same object,
    or the same type with equal ``compare=True`` fields, where node-valued
    fields compare by their own ``int``, tuples element by element (at any
    nesting), and every other value by :func:`_atom_key`. ``Phrase`` also
    compares ``words`` and ``analyzed``, and ``Term`` compares
    ``analyzed``: both are excluded from node equality, but both change
    what a leaf searches for (the emitter builds a phrase query from
    ``words``, and an unanalyzed leaf is still tokenized by a later
    ``analyze()``), so merging two leaves that differ in them would
    silently drop a branch. This applies at every depth.

    This is node equality except in the safe direction: it never merges
    two siblings ``==`` keeps apart, and it keeps apart some that ``==``
    merges (different ``words``/``analyzed``, values of different types
    that compare equal, values that print differently, NaN, unhashable
    values). Keeping a redundant sibling costs a duplicate clause; merging
    a distinct one loses results. Two values match only when they have the
    same type, compare equal and print the same (``repr``): a ``str``/
    ``int``/``bool``/``None`` or a ``FieldRef`` built from plain strings
    skips the print check, since for those equal values can never print
    differently, but anything else can (two ``Decimal``s with different
    exponents, the same instant in two time zones, a ``datetime`` differing
    only in ``fold``, ``-0.0`` versus ``0.0``), and ``analyze()`` searches
    ``str(value)``, so merging two such values would silently drop whichever
    spelling did not survive. NaN cannot simply follow ``==``: whether two
    nodes holding the same NaN object compare equal depends on the
    interpreter's generated ``__eq__`` (a field tuple compare with an
    identity shortcut on 3.11/3.12, a direct field compare without one on
    3.13/3.14), so a NaN never matches anything here. The same object is
    still one structure, since the memo below is keyed by identity.

    Assumes each value type's ``==`` is an equivalence relation consistent
    with its hash, as every type the built-in nodes declare is. Only nodes
    and tuples are looked into; any other container is one value. A custom
    ``__eq__`` on a ``Node`` subclass is not consulted. An atom's ``value ==
    value`` self-check above is not the only ``==`` call its key can trigger:
    ``_by_key``'s own lookup compares a new key against an existing one with
    the same hash, which calls the atom's ``__eq__`` against a *different*
    keyable value of the same type. A value whose ``==`` raises there
    (passing the self-check but not a comparison against another instance)
    propagates out of :func:`normalize`/:func:`analyze` uncaught, the same
    as any other caller-code exception during those calls.

    Why not put the nodes themselves in a set: the dataclasses' generated
    ``__hash__`` recurses through the whole subtree in Python frames, so a
    deep sibling would ``RecursionError`` inside the set operation even
    though ``normalize()`` walks iteratively. Here a key tuple holds only
    its children's ``int``s (tagged with ``_REF``), so hashing a key never
    descends the tree and no key grows with subtree size, and each node is
    keyed once per call however many ancestors dedupe it. Keying runs on an
    explicit work stack, and a node shared by several parents (a hand-built
    DAG) is keyed once and read by all of them.

    The memo is keyed by ``id()``; ``_held`` keeps every keyed node alive
    for the interner's lifetime so no ``id`` is reused by another object
    meanwhile. That also keeps a call's intermediate trees alive until it
    returns, which raises peak memory on wide trees (measured up to about
    2x, small in absolute terms). One interner must not outlive the public
    call that created it.
    """

    __slots__ = ("_by_key", "_held", "_ids")

    def __init__(self) -> None:
        self._by_key: dict[tuple[object, ...], int] = {}
        self._ids: dict[int, int] = {}
        self._held: list[Node] = []

    def key(self, root: Node) -> int:
        """Return ``root``'s interned ``int``, keying any not-yet-keyed
        node beneath it first."""
        known = self._ids.get(id(root))
        if known is not None:
            return known
        stack: list[tuple[Node, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if id(node) in self._ids:
                continue
            key = _plain_term_key(node) if type(node) is Term else None
            if key is None:
                if not expanded:
                    pending = [c for c in _nested_nodes(node) if id(c) not in self._ids]
                    if pending:
                        stack.append((node, True))
                        stack.extend((c, False) for c in pending)
                        continue
                key = self._general_key(node)
            number = self._by_key.setdefault(key, len(self._by_key))
            self._ids[id(node)] = number
            self._held.append(node)
        return self._ids[id(root)]

    def _general_key(self, node: Node) -> tuple[object, ...]:
        parts: list[object] = [type(node)]
        for value in _keyed_values(node):
            parts.append(self._value_key(value))
        return tuple(parts)

    def _value_key(self, value: object) -> object:
        kind = type(value)
        if kind in _PLAIN_ATOMS:
            return (kind, value)
        if isinstance(value, Node):
            return (_REF, self._ids[id(value)])
        if isinstance(value, tuple):
            return (tuple, kind, *(self._value_key(v) for v in value))
        return _atom_key(value)


def _plain_term_key(node: Term) -> tuple[object, ...] | None:
    """The key :meth:`_Interner._general_key` would build for ``node``,
    built directly for the overwhelmingly common ``Term`` shape (``str``
    text, ``bool`` flag, no field or a ``FieldRef`` of plain strings), or
    ``None`` for any other shape, which then takes the general path.

    A field that is a ``FieldRef`` but not of plain strings (for instance
    one whose ``json_path`` is an unhashable list, which nothing stops a
    caller from building) is deliberately excluded here even though
    :func:`_atom_key` could still key it: that call would raise or fall
    back to a fresh, per-call ``object()``, and this function must return
    exactly what :meth:`_Interner._general_key` would, never something
    merely close to it.
    """
    text = node.text
    analyzed = node.analyzed
    if type(text) is not str or type(analyzed) is not bool:
        return None
    field = node.field
    if field is None:
        field_key: tuple[object, ...] = (type(None), None)
    else:
        ref_key = _field_ref_key(field) if type(field) is FieldRef else None
        if ref_key is None:
            return None
        field_key = ref_key
    return (Term, field_key, (str, text), (bool, analyzed))


def _keyed_values(node: Node) -> tuple[object, ...]:
    """Every field value :meth:`_Interner._general_key` reads for ``node``:
    its own ``compare=True`` fields, plus ``Phrase.words``/``analyzed`` and
    ``Term.analyzed``, which are excluded from node equality but still
    change what a leaf searches for (see :class:`_Interner`'s docstring).
    Shared with :func:`_nested_nodes` so discovery walks exactly the values
    keying reads: a ``Node`` sitting in one of the extra fields must be
    keyed before the key that reads it is built, or ``_value_key`` raises
    ``KeyError``.
    """
    values: list[object] = [getattr(node, name) for name in _compare_fields(type(node))]
    if isinstance(node, Phrase):
        values.append(node.words)
        values.append(node.analyzed)
    elif isinstance(node, Term):
        values.append(node.analyzed)
    return tuple(values)


def _nested_nodes(node: Node) -> list[Node]:
    """Every ``Node`` held by ``node``'s keyed field values (see
    :func:`_keyed_values`), directly or inside tuples at any nesting depth."""
    found: list[Node] = []
    stack: list[object] = list(_keyed_values(node))
    while stack:
        value = stack.pop()
        if isinstance(value, Node):
            found.append(value)
        elif isinstance(value, tuple):
            stack.extend(value)
    return found


def _dedupe(nodes: tuple[Node, ...], *, interner: _Interner) -> tuple[Node, ...]:
    """Remove duplicate nodes, preserving first-seen order.

    Duplicate means interchangeable as a search, decided by ``interner``
    (see :class:`_Interner` for exactly what counts). The same object
    listed twice is caught first by identity, the cheap common case.
    """
    seen: set[int] = set()
    seen_ids: set[int] = set()
    result: list[Node] = []
    for n in nodes:
        if id(n) in seen_ids:
            continue
        seen_ids.add(id(n))
        number = interner.key(n)
        if number not in seen:
            seen.add(number)
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


def _can_still_empty_during_analysis(node: Node) -> bool:
    """Whether analysis could still turn ``node``'s subtree into
    ``Nothing()`` that was not already ``Nothing()``.

    True exactly when the subtree holds a ``Term``/``Phrase`` that is both
    fielded and not yet analyzed: :func:`analyze` only ever rewrites those
    two leaf kinds, only when ``analyzed`` is False, and only when
    :func:`_leaf_tokens` can resolve their field (an unfielded leaf is
    returned untouched, so it can never empty out). A field whose kind is
    outside the analyzable set answers True here too, since this function
    has no registry to consult; that is the safe direction, costing only
    an ``And`` node that stays spelled out rather than collapsing to its
    sibling.

    :func:`analyze`'s ``rewrite_leaf`` hook can empty any ``Term``/``Phrase``,
    and is deliberately not accounted for here: removing an already-analyzed
    or unfielded leaf beside an unfielded ``Every`` in an ``And`` leaves
    ``Nothing()``, the consequence :func:`analyze`'s docstring and
    ARCHITECTURE.md document and
    ``test_removing_a_leaf_beside_an_unfielded_every`` pins.

    Walked iteratively, like every other traversal in this module, so a
    pathologically deep tree costs heap rather than Python call frames.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if (
            isinstance(current, (Term, Phrase))
            and not current.analyzed
            and current.field is not None
        ):
            return True
        stack.extend(_child_nodes(current))
    return False


def _normalize_one(
    node: Node,
    children: tuple[Node, ...],
    *,
    interner: _Interner,
    _post_analysis: bool = False,
) -> Node:
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

    ``_post_analysis`` selects how the ``And`` rule treats an unfielded
    ``Every`` sibling (DIVERGENCES.md entry 23's match-all face). Dropping
    it as the AND identity is only sound once no sibling can still empty
    out: if one later analyzes to zero tokens, the ``Every`` is what should
    have been left standing alone, and by then it is gone. So the default
    (every caller upstream of analysis, ``normalize()``'s own public entry
    point included) drops it only when
    :func:`_can_still_empty_during_analysis` says no sibling is still in
    play. ``_post_analysis=True`` is for the passes :func:`analyze` runs on
    a tree it has already resolved, where nothing is left to discover and
    the drop is unconditional.

    ``interner`` keys the And/Or children for duplicate removal; it is the
    one interner of the public call this step belongs to (see
    :class:`_Interner`).
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
        if had_every and (
            _post_analysis
            or not any(
                _can_still_empty_during_analysis(c)
                for c in flat
                if not (isinstance(c, Every) and c.field is None)
            )
        ):
            flat = [c for c in flat if not (isinstance(c, Every) and c.field is None)]
        flat = list(_dedupe(tuple(flat), interner=interner))
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
        flat = list(_dedupe(tuple(flat), interner=interner))
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


def _normalize_impl(node: Node, *, _post_analysis: bool, interner: _Interner) -> Node:
    """Shared traversal behind :func:`normalize` and :func:`analyze`'s own
    post-analysis pass: an explicit work stack, keyed by
    node identity, so a pathologically deep or wide tree costs heap, not
    Python call-stack frames (a naive recursive postorder walk here used to
    roughly halve the query nesting depth ``parse()`` could tolerate before
    ``RecursionError``, since every parenthesized level already cost frames
    in the parser itself before ever reaching this function). The actual
    per-node rules live in :func:`_normalize_one`; this function only
    handles the postorder scheduling and threads ``_post_analysis`` and
    ``interner`` through unchanged.
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
            memo[id(current)] = _normalize_one(
                current, normalized_kids, interner=interner, _post_analysis=_post_analysis
            )
        else:
            stack.append((current, True))
            for k in kids:
                stack.append((k, False))
    return memo[id(node)]


def normalize(node: Node) -> Node:
    """Normalize an AST node into canonical form (pure, bottom-up).

    Applies flattening of nested same-type groups, Nothing/Every
    propagation, duplicate-sibling dedupe, empty-group collapse, single-child
    unwrap, and boost merging/stripping.

    Safe to call on a tree at any pipeline stage, analyzed or not, and safe
    to call before :func:`analyze` (DIVERGENCES.md entry 23's match-all
    face): the one rule whose soundness depends on analysis having already
    run, dropping an unfielded ``Every`` as the AND identity, fires only
    once :func:`_can_still_empty_during_analysis` shows no sibling can
    still empty out and leave that ``Every`` standing alone. Nothing
    :func:`analyze` needs is ever discarded here, so
    ``analyze(normalize(x)) == analyze(x)``.

    Args:
        node: The AST node to normalize.

    Returns:
        The normalized node.
    """

    return _normalize_impl(node, _post_analysis=False, interner=_Interner())


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
    tokens = (
        ([value] if value else [])
        if spec.analyzer is None
        else _analyzer_tokens(spec.analyzer, spec.name, value)
    )
    return tokens, spec


def _analyzer_tokens(analyzer: Callable[[str], list[str]], name: str, value: str) -> list[str]:
    """``analyzer(value)`` as a list, checked to be tokens of str.

    The analyzer is host code, and what it returns is never checked
    anywhere else: a non-str token reaches the emitter as a ``Term`` whose
    text is not text, and a bare ``str`` would be split into characters.
    Either one searches for terms nobody asked for and silently matches
    nothing, so a broken result raises here, naming the field.
    """
    result: object = analyzer(value)
    expected = f"analyzer for field {name!r} must return a list of str tokens"
    if isinstance(result, str) or not isinstance(result, Iterable):
        raise TypeError(f"{expected}, got {type(result).__name__}")
    tokens = list(result)
    for token in tokens:
        if not isinstance(token, str):
            raise TypeError(f"{expected}, got a token of type {type(token).__name__}")
    return tokens


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
    node: Node,
    children: tuple[Node, ...],
    registry: FieldRegistry,
    ctx: Multitoken,
    *,
    interner: _Interner,
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
    ``Nothing``, ``ErrorLeaf``, the range types, ``Wildcard``, ``Prefix``,
    ``Fuzzy``) falls through unchanged, since analysis never has anything to
    do for those kinds.
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
        return _normalize_one(node, kept, interner=interner, _post_analysis=True)
    if isinstance(node, (Or, Not, AndNot, AndMaybe, Require, Boosted)):
        return _normalize_one(node, children, interner=interner, _post_analysis=True)
    return node


# The type of analyze()'s rewrite_leaf hook, for the private helpers only:
# analyze() spells it out so the alias never appears in its public signature.
_RewriteLeaf: TypeAlias = Callable[[Term | Phrase], Node]


def _analyze_walk(
    node: Node,
    registry: FieldRegistry,
    default_mode: Multitoken,
    rewrite_leaf: _RewriteLeaf | None,
    pin: tuple[Term | Phrase, Node] | None,
    *,
    interner: _Interner,
) -> Node:
    """:func:`analyze`'s single bottom-up pass over an already-normalized
    ``node``, returning its analyzed replacement ahead of the final
    post-analysis normalize.

    ``rewrite_leaf`` is the host hook :func:`analyze` documents, applied to
    each ``Term``/``Phrase`` once that leaf's own analysis is known (see
    :func:`_apply_rewrite`). ``pin`` is ``None`` at the top level. Inside a
    replacement it is ``(leaf, analysis)``: wherever that same leaf object
    appears, it resolves to ``analysis``, the result it already got in its
    original position, instead of being analyzed again in the replacement's
    context.
    """

    # Mirrors normalize()'s own memoized work-stack traversal, except the
    # per-node combine step is _analyze_combine (leaf rewriting plus
    # interleaved normalization) instead of _normalize_one alone. The
    # Multitoken context (AND/OR) applicable to a DEFAULT-configured term's
    # position travels WITH each work item rather than living in a separate
    # id-keyed side table: a frozen node object legitimately aliased at two
    # tree positions with different enclosing combinators (value semantics
    # invite object reuse) then gets one analysis per (object, context) pair
    # instead of whichever context a traversal recorded last. And/Or set
    # their children's context to their own combinator; every other
    # combinator (Not/AndNot/AndMaybe/Require/Boosted) passes its own
    # context through unchanged, since none of them are themselves a
    # combining group a term could inherit AND/OR-ness from.
    memo: dict[tuple[int, Multitoken], Node] = {}
    work: list[tuple[Node, Multitoken, bool]] = [(node, default_mode, False)]
    while work:
        current, ctx, children_ready = work.pop()
        key = (id(current), ctx)
        if key in memo:
            # Already resolved at another position under the same context
            # (one node object aliased in the tree). Resolving it again
            # would give the same node and call rewrite_leaf a second time.
            # A children-ready item never gets here: its key was absent when
            # it was pushed, and only its own descendants, which cannot
            # include itself, run before it pops again.
            continue
        if pin is not None and current is pin[0]:
            memo[key] = pin[1]
            continue
        kids = _child_nodes(current)
        if isinstance(current, And):
            child_ctx = Multitoken.AND
        elif isinstance(current, Or):
            child_ctx = Multitoken.OR
        else:
            child_ctx = ctx
        if children_ready or not kids:
            analyzed_kids = tuple(memo[(id(k), child_ctx)] for k in kids)
            result = _analyze_combine(current, analyzed_kids, registry, ctx, interner=interner)
            if rewrite_leaf is not None and isinstance(current, (Term, Phrase)):
                result = _apply_rewrite(
                    current, result, registry, ctx, rewrite_leaf, interner=interner
                )
            memo[key] = result
        else:
            work.append((current, ctx, True))
            for k in kids:
                work.append((k, child_ctx, False))
    return memo[(id(node), default_mode)]


def _apply_rewrite(
    leaf: Term | Phrase,
    analyzed: Node,
    registry: FieldRegistry,
    ctx: Multitoken,
    rewrite_leaf: _RewriteLeaf,
    *,
    interner: _Interner,
) -> Node:
    """Call the host's ``rewrite_leaf`` hook for one leaf whose own analysis,
    ``analyzed``, is already known, and return what takes the leaf's place.

    Returning the leaf itself keeps ``analyzed``. Anything else is
    normalized and then analyzed in the leaf's context ``ctx`` by a nested,
    hook-free :func:`_analyze_walk` that pins the leaf to ``analyzed``. The
    nested walk never calls the hook, so a replacement containing the leaf
    cannot recurse, and it adds one Python call level however deep the
    replacement is. The parent sees the replacement's outcome in the leaf's
    place, so one that ends up ``Nothing()`` counts as newly dropped
    (DIVERGENCES.md entry 23), exactly as a zero-token leaf does.
    """
    replacement = rewrite_leaf(leaf)
    if replacement is leaf:
        return analyzed
    if not isinstance(replacement, Node):
        raise TypeError(f"rewrite_leaf must return an ast.Node, got {type(replacement).__name__}")
    normalized = _normalize_impl(replacement, _post_analysis=False, interner=interner)
    return _analyze_walk(normalized, registry, ctx, None, (leaf, analyzed), interner=interner)


def analyze(
    node: Node,
    registry: FieldRegistry,
    *,
    default_mode: Multitoken = Multitoken.AND,
    rewrite_leaf: Callable[[Term | Phrase], Node] | None = None,
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
    A field analyzer that returns anything but tokens of ``str`` (a bare
    ``str`` included) raises ``TypeError`` naming the field.

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

    ``rewrite_leaf`` lets a host replace ``Term``/``Phrase`` leaves from
    inside this pass, typically wrapping one as ``Or(leaf, companion)`` to
    search a companion field alongside it. It is called with every ``Term``
    and ``Phrase`` in the normalized input, whatever its field kind and
    whether or not it is already analyzed, after that leaf's own analysis
    has run (so a raising field analyzer raises first). It is never called
    with any other leaf type, nor with a node inside a replacement it
    returned. It is called once per leaf object and enclosing context: a
    leaf object aliased at two positions under the same context is called
    once, and both positions get the same result. Call order is
    unspecified. It is called for a leaf under a negation (``NOT``, or
    ``AndNot``'s negative side) too. Normalization never builds a new
    ``Term`` or ``Phrase``, so whether or not the input was normalized
    first, the leaf every call receives is the input tree's own object (an
    equal leaf that normalization dedupes away simply gets no call). A host
    that wants to skip a negated leaf can therefore pre-scan the tree it
    passes in for leaves under a negation and compare by identity (``is``).

    Returning the leaf itself keeps ordinary analysis. Any other ``Node`` is
    a replacement: it is normalized and then analyzed in the leaf's
    position, against the same ``registry``. Wherever the same leaf object
    appears inside it, that leaf stands for its own analysis in its original
    context, so ``Or(leaf, companion)`` keeps a multi-token leaf combined the
    way its enclosing group says, not the way the new ``Or`` would. An equal
    copy of the leaf gets no such treatment, and one placed before the leaf
    in the same group replaces it when normalization dedupes the pair. Every
    other raw leaf in the replacement is analyzed normally, a
    ``Multitoken.DEFAULT`` one taking its context from its nearest group in
    the normalized replacement, or from the leaf's position when there is
    none. A replacement that ends up empty, a bare ``Nothing()`` included,
    drops out of its enclosing group exactly as a zero-token leaf does
    (DIVERGENCES.md entry 23). The result is fully analyzed, so a later
    plain :func:`analyze` of it, including the one ``emit()`` runs, changes
    nothing. Running the hook a second time over its own output would see
    split tokens and companions rather than the original leaves, so a host
    with several rewrites combines them into one hook.

    An exception raised by the hook propagates unchanged, and a return value
    that is not a ``Node`` raises ``TypeError``. A host calls this function
    itself, so a field analyzer that raises surfaces here as its own
    exception, not as the ``QueryError`` ``emit()`` would have wrapped it
    in. The hook does not change which leaves normalization treats as able
    to empty: removing an already-analyzed or unfielded leaf beside an
    unfielded ``Every`` in an ``And`` leaves ``Nothing()``, because
    normalization has already dropped that ``Every`` as the AND identity,
    and the result is the same whether or not the caller normalized first.

    Traverses iteratively, mirroring :func:`normalize`'s own explicit work
    stack, so a pathologically deep tree costs heap rather than Python
    call-stack frames, exactly like every other stage in this pipeline that
    walks a parsed tree.

    Args:
        node: The AST node to analyze. Normally already normalized (the
            pipeline normalizes ahead of calling this), but the result
            never depends on whether it was: this function's own leading
            pass is :func:`normalize` itself, which is idempotent and
            discards nothing analysis still needs, so
            ``analyze(normalize(x)) == analyze(x)``. A
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
        rewrite_leaf: Optional hook replacing ``Term``/``Phrase`` leaves
            during this pass, as described above. ``None``, the default,
            is plain analysis.

    Returns:
        A plain ``ast.Node`` tree, already normalized, with every
        TEXT/KEYWORD leaf's analysis fully resolved.

    Raises:
        TypeError: ``rewrite_leaf`` returned something that is not a
            ``Node``.
    """

    # Normalize first: see the Args docstring above for why this is
    # load-bearing (the entry-23/entry-27 distinction), not just tidiness.
    # Exactly what normalize() does, the same pass any caller may already
    # have run on this tree, which is why this function's result cannot
    # depend on whether they did (see the Args docstring's insensitivity
    # note). It shares one interner with every later step of this call, so
    # each node is keyed for duplicate removal once.
    interner = _Interner()
    node = _normalize_impl(node, _post_analysis=False, interner=interner)

    walked = _analyze_walk(node, registry, default_mode, rewrite_leaf, None, interner=interner)

    # _post_analysis=True: every leaf's fate is settled by now, so the
    # unfielded-Every AND-identity drop normalize() holds back before
    # analysis (DIVERGENCES.md entry 23's match-all face) is finally
    # unconditional here, giving the canonical shape whoosh's own
    # And.normalize() produces.
    return _normalize_impl(walked, _post_analysis=True, interner=interner)


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
      it (the analyzer/pattern_normalizer seam). A ``Fuzzy`` leaf is the
      same case: its text is a match specification, not a word, so it
      contributes nothing either.
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
        # Not, Prefix/Wildcard, Fuzzy, ranges, Every, Nothing, ErrorLeaf:
        # contribute nothing, deliberately (see the docstring's rules).
    return tuple(out)
