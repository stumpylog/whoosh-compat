import pytest

from whoosh_compat.errors import EMIT_KINDS
from whoosh_compat.errors import PARSE_KINDS
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import QueryError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.errors import WhooshCompatError
from whoosh_compat.errors import cause_for


def test_hierarchy() -> None:
    assert issubclass(UnsupportedQueryError, WhooshCompatError)
    assert issubclass(QueryEmitError, WhooshCompatError)
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
    e = QueryEmitError("cannot emit", diagnostic=d)
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
    """
    overlap = PARSE_KINDS & EMIT_KINDS
    union = PARSE_KINDS | EMIT_KINDS
    assert overlap == frozenset()
    assert union == frozenset(DiagnosticKind)


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
    fault, so it is a 500, not a 400.
    """
    reachable = {
        DiagnosticKind.EXISTS_REQUIRES_FAST,
        DiagnosticKind.TEXT_RANGE,
        DiagnosticKind.PATTERN_TOO_COMPLEX,
    }
    for kind in reachable:
        assert cause_for(kind) is not Cause.INTERNAL
    for kind in EMIT_KINDS - reachable:
        assert cause_for(kind) is Cause.INTERNAL
