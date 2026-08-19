"""Diagnostic and exception hierarchy for whoosh-compat."""

from dataclasses import dataclass
from enum import Enum
from enum import auto

from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef


class Cause(Enum):
    """Who can act on a diagnostic, and whether the query can ever run.

    Not a severity tier: every cause is fatal to the query it concerns.
    A host maps ``INVALID_INPUT``/``UNSUPPORTED`` to a 400,
    ``MISCONFIGURED`` to an operator alert, and ``INTERNAL`` to a 500,
    because ``INTERNAL`` at emit time is never the user's fault.
    """

    INVALID_INPUT = auto()
    UNSUPPORTED = auto()
    MISCONFIGURED = auto()
    INTERNAL = auto()


class DiagnosticKind(Enum):
    """Kinds of diagnostics that can be reported during query processing.

    Member values are a machine-stable contract: a host is expected to
    branch on ``kind`` (for example, mapping ``BAD_DATE`` to a typed
    ``InvalidDateQuery``). ``Diagnostic.message`` carries no such guarantee
    and may reword without notice.

    ``PARSE_KINDS`` and ``EMIT_KINDS`` partition this enum. The partition
    is why ``Diagnostic`` needs no ``phase`` field: ``kind`` alone says
    which half of the pipeline produced a record.

    An ``AST_`` prefix means the condition is unreachable from query text.
    Reaching one means a caller built a node the parser would never
    produce, so it is a defect in the caller, not a bad query.
    """

    # Parse-time.
    BAD_DATE = auto()
    BAD_NUMBER = auto()
    TOO_DEEP = auto()
    PATTERN_ON_NUMERIC = auto()
    PATTERN_ON_BOOLEAN_EXISTS = auto()
    PATTERN_ON_SUBPATH = auto()

    # Emit-time, reachable from query text.
    EXISTS_REQUIRES_FAST = auto()
    TEXT_RANGE = auto()
    PATTERN_TOO_COMPLEX = auto()

    # Emit-time backstops for caller-built ASTs.
    AST_UNFIELDED_TERM = auto()
    AST_UNKNOWN_FIELD = auto()
    AST_JSON_NEEDS_SUBPATH = auto()
    AST_BAD_NUMBER = auto()
    AST_BAD_DATE = auto()
    AST_PATTERN_ON_KIND = auto()
    AST_KIND_NOT_IMPLEMENTED = auto()
    AST_INVALID_SHAPE = auto()
    BACKEND_REJECTED = auto()


PARSE_KINDS = frozenset(
    {
        DiagnosticKind.BAD_DATE,
        DiagnosticKind.BAD_NUMBER,
        DiagnosticKind.TOO_DEEP,
        DiagnosticKind.PATTERN_ON_NUMERIC,
        DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
        DiagnosticKind.PATTERN_ON_SUBPATH,
    }
)

EMIT_KINDS = frozenset(DiagnosticKind) - PARSE_KINDS


_CAUSE: dict[DiagnosticKind, Cause] = {
    DiagnosticKind.BAD_DATE: Cause.INVALID_INPUT,
    DiagnosticKind.BAD_NUMBER: Cause.INVALID_INPUT,
    DiagnosticKind.TOO_DEEP: Cause.INVALID_INPUT,
    DiagnosticKind.PATTERN_ON_NUMERIC: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_ON_SUBPATH: Cause.UNSUPPORTED,
    DiagnosticKind.EXISTS_REQUIRES_FAST: Cause.MISCONFIGURED,
    DiagnosticKind.TEXT_RANGE: Cause.UNSUPPORTED,
    DiagnosticKind.PATTERN_TOO_COMPLEX: Cause.UNSUPPORTED,
    DiagnosticKind.AST_UNFIELDED_TERM: Cause.INTERNAL,
    DiagnosticKind.AST_UNKNOWN_FIELD: Cause.INTERNAL,
    DiagnosticKind.AST_JSON_NEEDS_SUBPATH: Cause.INTERNAL,
    DiagnosticKind.AST_BAD_NUMBER: Cause.INTERNAL,
    DiagnosticKind.AST_BAD_DATE: Cause.INTERNAL,
    DiagnosticKind.AST_PATTERN_ON_KIND: Cause.INTERNAL,
    DiagnosticKind.AST_KIND_NOT_IMPLEMENTED: Cause.INTERNAL,
    DiagnosticKind.AST_INVALID_SHAPE: Cause.INTERNAL,
    DiagnosticKind.BACKEND_REJECTED: Cause.INTERNAL,
}


def cause_for(kind: DiagnosticKind) -> Cause:
    """The ``Cause`` for ``kind``.

    Lives here rather than in the emitter because the parse-side
    construction sites need it too, and ``parser/`` must not import from
    ``emitters/``.
    """

    return _CAUSE[kind]


@dataclass(frozen=True, kw_only=True, slots=True)
class Diagnostic:
    """A structured record of why a query cannot run.

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

    ``message`` is developer/log output only. It has no stability
    guarantee and must never be parsed. Branch on ``kind`` and ``cause``.

    ``divergence`` is the ``DIVERGENCES.md`` entry number when one applies,
    so a host can cross-reference without reading prose.
    """

    kind: DiagnosticKind
    cause: Cause
    message: str
    startchar: int | None = None
    endchar: int | None = None
    field: FieldRef | None = None
    field_kind: FieldKind | None = None
    raw_value: str | None = None
    divergence: int | None = None


class WhooshCompatError(Exception):
    """Base exception for whoosh-compat."""


class QueryError(WhooshCompatError):
    """Raised by ``emit()`` when a query cannot be turned into a backend query.

    Always carries a ``Diagnostic``. Callers branch on
    ``err.diagnostic.cause``; the exception's own message is the
    diagnostic's message and carries no stability guarantee.
    """

    def __init__(self, diagnostic: Diagnostic) -> None:
        """Initialize QueryError from the diagnostic describing the failure.

        Args:
            diagnostic: The structured record of why the query cannot run.
        """
        super().__init__(diagnostic.message)
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

    Distinct from ``Cause.INTERNAL``, which describes a ``Diagnostic``
    about an AST that already exists. This fires during the tagger/filter
    pipeline, before there is one.
    """


class UnsupportedQueryError(WhooshCompatError):
    """Deprecated, removed once every raise site moves to ``QueryError``."""


class QueryEmitError(WhooshCompatError):
    """Deprecated, removed once every raise site moves to ``QueryError``."""

    def __init__(self, msg: str, *, diagnostic: Diagnostic | None = None):
        """Initialize QueryEmitError with optional diagnostic.

        Args:
            msg: The error message.
            diagnostic: Optional diagnostic information.
        """
        super().__init__(msg)
        self.diagnostic = diagnostic
