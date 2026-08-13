"""Query AST nodes and visitor pattern for whoosh-compat."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Generic
from typing import TypeVar

from whoosh_compat.errors import Diagnostic
from whoosh_compat.fields import FieldRef

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

    startchar: int | None = field(default=None, compare=False)
    endchar: int | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Term(Node):
    """A single term query."""

    field: FieldRef | None
    text: str | int | bool


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
    """Phrase query with optional slop."""

    field: FieldRef | None
    text: str
    slop: int = 1


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
    """Remove duplicate nodes, preserving first-seen order."""
    seen: set[Node] = set()
    result: list[Node] = []
    for n in nodes:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return tuple(result)


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
    """

    if isinstance(node, And):
        if not children:
            return Nothing()  # rule 7: empty group -> Nothing
        if any(isinstance(c, Nothing) for c in children):
            return Nothing()  # rule 3: Nothing propagates through And
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
            return Every() if had_every else Nothing()
        if len(flat) == 1:
            return flat[0]
        return And(children=tuple(flat))

    if isinstance(node, Or):
        if not children:
            return Nothing()  # rule 7: empty group -> Nothing
        flat = []
        for child in children:
            if isinstance(child, Or):
                flat.extend(child.children)
            else:
                flat.append(child)
        if any(isinstance(c, Every) and c.field is None for c in flat):
            return Every()  # rule 6: Every absorbs Or siblings
        flat = [c for c in flat if not isinstance(c, Nothing)]
        flat = list(_dedupe(tuple(flat)))
        if not flat:
            return Nothing()
        if len(flat) == 1:
            return flat[0]
        return Or(children=tuple(flat))

    if isinstance(node, Not):
        (child,) = children
        if isinstance(child, Nothing):
            return Every()
        return Not(child=child)

    if isinstance(node, AndNot):
        positive, negative = children
        if isinstance(positive, Nothing):
            return Nothing()
        if isinstance(negative, Nothing):
            return positive
        return AndNot(positive=positive, negative=negative)

    if isinstance(node, AndMaybe):
        required, optional = children
        if isinstance(required, Nothing):
            return Nothing()
        if isinstance(optional, Nothing):
            return required
        return AndMaybe(required=required, optional=optional)

    if isinstance(node, Require):
        scored, filter_only = children
        if isinstance(scored, Nothing) or isinstance(filter_only, Nothing):
            return Nothing()
        return Require(scored=scored, filter_only=filter_only)

    if isinstance(node, Boosted):
        (child,) = children
        boost = node.boost
        if isinstance(child, Nothing):
            return Nothing()
        if isinstance(child, Boosted):
            boost = child.boost * boost
            child = child.child
        if boost == 1.0:
            return child
        return Boosted(child=child, boost=boost)

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
    function (issue #31). The actual per-node rules live in
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
