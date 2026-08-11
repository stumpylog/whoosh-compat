"""Diagnostic and exception hierarchy for whoosh-compat."""

from dataclasses import dataclass
from enum import Enum
from enum import auto

from whoosh_compat.fields import FieldRef


class DiagnosticKind(Enum):
    """Kinds of diagnostics that can be reported during query processing."""

    BAD_DATE = auto()
    BAD_NUMBER = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A diagnostic message with optional location information."""

    message: str
    kind: DiagnosticKind
    startchar: int | None
    endchar: int | None
    field: FieldRef | None = None
    raw_value: str | None = None


class WhooshCompatError(Exception):
    """Base exception for whoosh-compat."""


class UnsupportedQueryError(WhooshCompatError):
    """Raised when a query feature is not supported."""


class QueryEmitError(WhooshCompatError):
    """Raised when a query cannot be emitted."""

    def __init__(self, msg: str, *, diagnostic: Diagnostic | None = None):
        """Initialize QueryEmitError with optional diagnostic.

        Args:
            msg: The error message.
            diagnostic: Optional diagnostic information.
        """
        super().__init__(msg)
        self.diagnostic = diagnostic


class QueryParserError(WhooshCompatError):
    """Raised when query parsing fails."""
