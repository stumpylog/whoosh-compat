"""An emit-time failure on a leaf carries that leaf's source positions.

A host that maps a diagnostic back onto the query text (to underline the
offending clause, say) reads ``startchar``/``endchar``. Those must be present
for every leaf-level failure, whichever check raised it: field resolution,
the pattern kind check or the fuzzy kind check. A tree built by hand, or a
companion leaf a host copies a parsed leaf's positions onto, reaches the same
checks.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import cast

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex
from .conftest import emit_ast

START, END = 4, 11
_WHEN = datetime(2026, 8, 1, tzinfo=UTC)

UNKNOWN = FieldRef("nope")


def _leaves(field: FieldRef | None) -> list[tuple[str, ast.Node]]:
    """Every leaf type that resolves a field, on ``field``, at START..END."""
    # Fuzzy, NumericRange and DateRange declare their field required; a None
    # one is the hand-built defect under test.
    required = cast("FieldRef", field)
    return [
        ("term", ast.Term(field=field, text="x", startchar=START, endchar=END)),
        ("phrase", ast.Phrase(field=field, text="x y", startchar=START, endchar=END)),
        ("prefix", ast.Prefix(field=field, text="x", startchar=START, endchar=END)),
        ("wildcard", ast.Wildcard(field=field, pattern="x?", startchar=START, endchar=END)),
        ("fuzzy", ast.Fuzzy(field=required, text="xyz", startchar=START, endchar=END)),
        (
            "termrange",
            ast.TermRange(
                field=field,
                lo="a",
                hi="b",
                incl_lo=True,
                incl_hi=True,
                startchar=START,
                endchar=END,
            ),
        ),
        (
            "numericrange",
            ast.NumericRange(
                field=required,
                lo=1,
                hi=2,
                incl_lo=True,
                incl_hi=True,
                startchar=START,
                endchar=END,
            ),
        ),
        (
            "daterange",
            ast.DateRange(
                field=required,
                lo=_WHEN,
                hi=None,
                incl_lo=True,
                incl_hi=False,
                startchar=START,
                endchar=END,
            ),
        ),
    ]


CASES = [
    *(
        pytest.param(node, DiagnosticKind.AST_UNKNOWN_FIELD, id=f"unknown-field-{name}")
        for name, node in [
            *_leaves(UNKNOWN),
            ("every", ast.Every(field=UNKNOWN, startchar=START, endchar=END)),
        ]
    ),
    # An unfielded Every is the match-all query, not a failure.
    *(
        pytest.param(node, DiagnosticKind.AST_UNFIELDED_TERM, id=f"unfielded-{name}")
        for name, node in _leaves(None)
    ),
    pytest.param(
        ast.Prefix(field=FieldRef("asn"), text="1", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="prefix-on-u64",
    ),
    pytest.param(
        ast.Wildcard(field=FieldRef("asn"), pattern="1?", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="wildcard-on-u64",
    ),
    pytest.param(
        ast.Prefix(field=FieldRef("has_tag"), text="t", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="prefix-on-boolean-exists",
    ),
    pytest.param(
        ast.Wildcard(field=FieldRef("has_tag"), pattern="t?", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="wildcard-on-boolean-exists",
    ),
    pytest.param(
        ast.Prefix(field=FieldRef("notes", "note"), text="x", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="prefix-on-json-subpath",
    ),
    pytest.param(
        ast.Wildcard(field=FieldRef("notes", "note"), pattern="x?", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="wildcard-on-json-subpath",
    ),
    pytest.param(
        ast.Prefix(field=FieldRef("notes"), text="x", startchar=START, endchar=END),
        DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
        id="prefix-on-bare-json",
    ),
    pytest.param(
        ast.Wildcard(field=FieldRef("notes"), pattern="x?", startchar=START, endchar=END),
        DiagnosticKind.AST_JSON_NEEDS_SUBPATH,
        id="wildcard-on-bare-json",
    ),
    pytest.param(
        ast.Prefix(field=FieldRef("created"), text="2026", startchar=START, endchar=END),
        DiagnosticKind.AST_PATTERN_ON_KIND,
        id="prefix-on-date",
    ),
    pytest.param(
        ast.Fuzzy(field=FieldRef("asn"), text="123", startchar=START, endchar=END),
        DiagnosticKind.AST_KIND_NOT_IMPLEMENTED,
        id="fuzzy-on-u64",
    ),
    pytest.param(
        ast.Fuzzy(field=FieldRef("notes", "note"), text="xyz", startchar=START, endchar=END),
        DiagnosticKind.AST_KIND_NOT_IMPLEMENTED,
        id="fuzzy-on-json-subpath",
    ),
]


@pytest.mark.parametrize(("node", "kind"), CASES)
def test_a_leaf_failure_carries_the_leaf_positions(
    node: ast.Node, kind: DiagnosticKind, ereg: FieldRegistry, tindex: TIndex
) -> None:
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is kind
    assert (diagnostic.startchar, diagnostic.endchar) == (START, END)
