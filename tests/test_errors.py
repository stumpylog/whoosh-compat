import pytest

from whoosh_compat.errors import EMIT_KINDS
from whoosh_compat.errors import PARSE_KINDS
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.errors import WhooshCompatError
from whoosh_compat.errors import cause_for


def test_hierarchy() -> None:
    assert issubclass(QueryError, WhooshCompatError)


def test_diagnostic_frozen() -> None:
    d = Diagnostic(
        message="bad date 'x'",
        kind=DiagnosticKind.BAD_DATE,
        cause=Cause.INVALID_INPUT,
        startchar=5,
        endchar=9,
    )
    assert d.startchar == 5
    e = QueryError(d)
    assert e.diagnostic is d


def test_every_kind_has_a_cause() -> None:
    """cause_for() must be total over DiagnosticKind.

    This is the exhaustiveness guard: a new member added without a cause
    entry fails here rather than silently defaulting at a raise site.
    """
    for kind in DiagnosticKind:
        assert isinstance(cause_for(kind), Cause)


def test_parse_and_emit_kind_sets_are_disjoint_and_total() -> None:
    """Disjointness is what makes a `phase` field unnecessary: `kind` alone
    identifies which phase produced a Diagnostic.

    The two memberships are spelled out here rather than derived from the
    module's own sets. Deriving them would make this guard vacuous:
    `EMIT_KINDS` is defined as `frozenset(DiagnosticKind) - PARSE_KINDS`,
    so disjointness and totality hold by construction no matter what the
    enum contains. Written as literals, a new `DiagnosticKind` member fails
    here until someone decides which phase produces it.
    """
    parse_kinds = {
        DiagnosticKind.BAD_DATE,
        DiagnosticKind.BAD_NUMBER,
        DiagnosticKind.TOO_DEEP,
        DiagnosticKind.PATTERN_ON_NUMERIC,
        DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
        DiagnosticKind.PATTERN_ON_SUBPATH,
    }
    emit_kinds = {
        DiagnosticKind.EXISTS_REQUIRES_FAST,
        DiagnosticKind.TEXT_RANGE,
        DiagnosticKind.PATTERN_TOO_COMPLEX,
        DiagnosticKind.SCHEMA_FIELD_MISSING,
        DiagnosticKind.AST_UNFIELDED_TERM,
        DiagnosticKind.AST_UNKNOWN_FIELD,
        DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
        DiagnosticKind.AST_BAD_NUMBER,
        DiagnosticKind.AST_BAD_DATE,
        DiagnosticKind.AST_PATTERN_ON_KIND,
        DiagnosticKind.AST_KIND_NOT_IMPLEMENTED,
        DiagnosticKind.AST_INVALID_SHAPE,
        DiagnosticKind.BACKEND_REJECTED,
    }
    assert parse_kinds & emit_kinds == set()
    assert parse_kinds | emit_kinds == set(DiagnosticKind)
    assert parse_kinds == PARSE_KINDS
    assert emit_kinds == EMIT_KINDS


def test_schema_field_missing_is_misconfigured() -> None:
    """A field this library's registry knows and the tantivy schema does not
    is a deployment drift the operator can fix. It is neither a defect in
    this library nor a property of the query text.
    """
    assert cause_for(DiagnosticKind.SCHEMA_FIELD_MISSING) is Cause.MISCONFIGURED


def test_backend_rejected_stays_internal() -> None:
    """The bare ValueError/TypeError path from tantivy-py is a real library
    defect and must not be swept into MISCONFIGURED by the split.

    `cause` is a pure function of `kind` (`cause_for` reads a dict), so the
    only way to re-cause the schema-drift branch was to give it a kind of
    its own. Getting this backwards would relabel a genuine tantivy-py
    rejection of a query this emitter built as the operator's problem,
    routing a library bug to a 400 and hiding it from monitoring.
    """
    assert cause_for(DiagnosticKind.BACKEND_REJECTED) is Cause.INTERNAL


def test_diagnostic_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        Diagnostic("msg", DiagnosticKind.BAD_DATE, 0, 3)  # type: ignore[arg-type,call-arg]


def test_query_error_carries_its_diagnostic() -> None:
    d = Diagnostic(
        kind=DiagnosticKind.TEXT_RANGE,
        cause=Cause.UNSUPPORTED,
        message="text ranges are not supported",
        divergence=5,
    )
    err = QueryError(d)
    assert err.diagnostic is d
    assert str(err) == "text ranges are not supported"


def test_internal_emit_kinds_are_never_user_facing() -> None:
    """Every emit kind that is reachable from query text is non-INTERNAL.

    Hosts rely on this to route: INTERNAL at emit time is never the user's
    fault, so it is a 500, not a 400. A MISCONFIGURED kind is reachable and
    so belongs on the non-INTERNAL side: the operator caused it, but a user
    request is still waiting on an answer.
    """
    reachable = {
        DiagnosticKind.EXISTS_REQUIRES_FAST,
        DiagnosticKind.TEXT_RANGE,
        DiagnosticKind.PATTERN_TOO_COMPLEX,
        DiagnosticKind.SCHEMA_FIELD_MISSING,
    }
    for kind in reachable:
        assert cause_for(kind) is not Cause.INTERNAL
    for kind in EMIT_KINDS - reachable:
        assert cause_for(kind) is Cause.INTERNAL
