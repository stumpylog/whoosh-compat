"""Query AST nodes and visitor pattern for whoosh-compat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from whoosh_compat.errors import Diagnostic


T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class Node:
    """Base class for all AST nodes. startchar and endchar are keyword-only."""
    startchar: int | None = None
    endchar: int | None = None


@dataclass(frozen=True)
class Term(Node):
    """A single term query."""
    field: str | None
    text: str | int | bool


@dataclass(frozen=True)
class And(Node):
    """Intersection (AND) of child nodes."""
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or(Node):
    """Union (OR) of child nodes."""
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Not(Node):
    """Negation (NOT) of a child node."""
    child: Node


@dataclass(frozen=True)
class AndNot(Node):
    """Require positive, exclude negative."""
    positive: Node
    negative: Node


@dataclass(frozen=True)
class AndMaybe(Node):
    """Require required, optionally include optional."""
    required: Node
    optional: Node


@dataclass(frozen=True)
class Require(Node):
    """Score with scored, filter with filter_only."""
    scored: Node
    filter_only: Node


@dataclass(frozen=True)
class Phrase(Node):
    """Phrase query with optional slop."""
    field: str | None
    text: str
    slop: int = 1


@dataclass(frozen=True)
class Prefix(Node):
    """Prefix query."""
    field: str | None
    text: str


@dataclass(frozen=True)
class Wildcard(Node):
    """Wildcard pattern query."""
    field: str | None
    pattern: str


@dataclass(frozen=True)
class TermRange(Node):
    """Range query on term values."""
    field: str | None
    lo: str | None
    hi: str | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True)
class NumericRange(Node):
    """Range query on numeric values."""
    field: str
    lo: int | None
    hi: int | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True)
class DateRange(Node):
    """Range query on date values."""
    field: str
    lo: datetime | None
    hi: datetime | None
    incl_lo: bool
    incl_hi: bool


@dataclass(frozen=True)
class Every(Node):
    """Match all documents (optionally in a field)."""
    field: str | None = None


@dataclass(frozen=True)
class Nothing(Node):
    """Match no documents."""
    pass


@dataclass(frozen=True)
class Boosted(Node):
    """Apply a boost factor to a child node."""
    child: Node
    boost: float


@dataclass(frozen=True)
class ErrorLeaf(Node):
    """Represent a parse error in the tree."""
    diagnostic: Diagnostic


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
