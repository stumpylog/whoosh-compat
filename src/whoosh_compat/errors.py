"""Diagnostic and exception hierarchy for whoosh-compat."""

from dataclasses import dataclass
from enum import Enum
from enum import auto

from whoosh_compat.fields import FieldRef


class DiagnosticKind(Enum):
    """Kinds of diagnostics that can be reported during query processing.

    Member values are a machine-stable contract: a host is expected to
    branch on ``kind`` (for example, mapping ``BAD_DATE`` to a typed
    ``InvalidDateQuery``). ``Diagnostic.message`` carries no such guarantee
    and may reword without notice.
    """

    BAD_DATE = auto()
    BAD_NUMBER = auto()
    TOO_DEEP = auto()
    UNSUPPORTED_PATTERN = auto()


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A diagnostic message with optional location information.

    Severity is fatal-only, permanently: a ``Diagnostic`` always means the
    query it concerns cannot be emitted. There is no ``severity`` field and
    none will be added; a future informational-only signal (for example,
    reporting that ``analyze()`` dropped a zero-token term) must use a
    separate channel, never ``ParseResult.diagnostics``. A caller that sees
    any diagnostics for a query should treat that query as un-emittable,
    the same way `whoosh_compat.emitters.tantivy_.emit` treats an
    ``ErrorLeaf`` in the tree: not as a warning to weigh, but as a hard
    stop. See also the two-part host contract documented on
    :func:`whoosh_compat.emitters.tantivy_.emit` and the README.
    """

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
    """Raised when an internal parser pipeline invariant is violated.

    Not raised for bad *user* query input (see the module-level invariant:
    ``parse()`` never raises for that, it accumulates ``Diagnostic``s
    instead). The only raise sites are internal self-checks in
    ``parser/default.py`` (a tagger failed to advance the cursor, a filter
    returned ``None`` where a node was required), both of which indicate a
    bug in a tagger/filter plugin, not something a caller passing ordinary
    query strings should ever expect to catch.
    """
