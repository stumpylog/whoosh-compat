from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.errors import WhooshCompatError


def test_hierarchy():
    assert issubclass(UnsupportedQueryError, WhooshCompatError)
    assert issubclass(QueryEmitError, WhooshCompatError)

def test_diagnostic_frozen():
    d = Diagnostic("bad date 'x'", DiagnosticKind.BAD_DATE, 5, 9)
    assert d.startchar == 5
    e = QueryEmitError("cannot emit", diagnostic=d)
    assert e.diagnostic is d
