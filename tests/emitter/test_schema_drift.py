"""Registry/schema drift: a field this library's registry knows that the
tantivy schema does not.

This is the operator's problem, not the query text's and not a defect in
this library, so it reports ``SCHEMA_FIELD_MISSING``/``MISCONFIGURED``. The
condition is a property of the *field*, not of the leaf spelling that
happens to reach it, so every leaf that queries a resolved field must agree:
a host routing on ``cause`` cannot have ``content:x`` and ``content:x*``
land on different sides of the 400/500 line for the same broken deployment.
That totality over the leaf axis is what this module pins.

The discrimination has two sides and both are load-bearing. Reclassifying
too little leaves the common case (a plain term) blaming this library for
the host's configuration. Reclassifying too much is worse: a bare
``ValueError`` from tantivy-py for any *other* reason really is a defect
here, and folding it into ``MISCONFIGURED`` would hide library bugs behind
a 400. ``test_other_value_errors_are_still_internal`` is the guard for the
second direction.
"""

from __future__ import annotations

import datetime
from datetime import UTC

import pytest
import tantivy

from whoosh_compat import ast
from whoosh_compat.emitters import tantivy_
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import emit_ast
from .conftest import lower_fold


@pytest.fixture
def drifted(ereg: FieldRegistry) -> FieldRegistry:
    """``ereg`` plus fields of every kind that the tindex schema lacks.

    Mirrors the real failure mode this guards against: a host's field table
    and its schema builder are separate declarations that fall out of step,
    so the registry gains an entry the index was never built with.
    """
    return FieldRegistry(
        [
            *ereg,
            FieldSpec("ghost", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
            FieldSpec("ghost_kw", FieldKind.KEYWORD, analyzer=lower_fold),
            FieldSpec("ghost_num", FieldKind.U64, fast=True),
            FieldSpec("ghost_date", FieldKind.DATE, date_only=True, fast=True),
            FieldSpec("ghost_json", FieldKind.JSON, subpaths=("user",), fast=True),
            FieldSpec("ghost_nonfast", FieldKind.TEXT, analyzer=lower_fold),
            FieldSpec("has_ghost", FieldKind.BOOLEAN_EXISTS, exists_target="ghost_num"),
        ]
    )


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(ast.Term(field=FieldRef("ghost"), text="alice"), id="term-text"),
        pytest.param(ast.Term(field=FieldRef("ghost_kw"), text="alice"), id="term-keyword"),
        pytest.param(ast.Term(field=FieldRef("ghost_num"), text="7"), id="term-u64"),
        pytest.param(
            ast.Phrase(field=FieldRef("ghost"), text="a b", words=("a", "b")),
            id="phrase-multi-word",
        ),
        pytest.param(
            ast.Phrase(field=FieldRef("ghost"), text="solo", words=("solo",)),
            id="phrase-single-word",
        ),
        pytest.param(ast.Prefix(field=FieldRef("ghost"), text="ali"), id="prefix"),
        pytest.param(ast.Wildcard(field=FieldRef("ghost"), pattern="a?ice"), id="wildcard"),
        pytest.param(
            ast.NumericRange(field=FieldRef("ghost_num"), lo=1, hi=5, incl_lo=True, incl_hi=True),
            id="numeric-range",
        ),
        pytest.param(
            ast.DateRange(
                field=FieldRef("ghost_date"),
                lo=datetime.datetime(2020, 1, 1, tzinfo=UTC),
                hi=datetime.datetime(2020, 12, 31, tzinfo=UTC),
                incl_lo=True,
                incl_hi=True,
            ),
            id="date-range",
        ),
    ],
)
def test_every_leaf_on_a_drifted_field_reports_the_misconfiguration(
    node: ast.Node, tindex: TIndex, drifted: FieldRegistry
) -> None:
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, drifted)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.SCHEMA_FIELD_MISSING
    assert d.cause is Cause.MISCONFIGURED


@pytest.mark.parametrize(
    ("qs", "field"),
    [
        pytest.param("ghost:alice", "ghost", id="bare-term"),
        pytest.param('ghost:"alice smith"', "ghost", id="quoted-phrase"),
        pytest.param("ghost:ali*", "ghost", id="prefix-star"),
        pytest.param("ghost:a?ice", "ghost", id="wildcard"),
        pytest.param("ghost:[a-a]lic*", "ghost", id="bracket-class"),
        pytest.param("ghost_kw:urgent", "ghost_kw", id="keyword-term"),
        pytest.param("ghost_num:7", "ghost_num", id="numeric-term"),
        pytest.param("ghost_num:[1 TO 5]", "ghost_num", id="numeric-range"),
        pytest.param("ghost_date:2020-03-15", "ghost_date", id="date-term"),
        # The existence spellings. tantivy's exists_query takes no schema
        # and validates nothing at build time, so before the probe these
        # built a well-formed query that raised a bare ValueError out of the
        # searcher, escaping emit()'s QueryError contract entirely.
        pytest.param("ghost_num:*", "ghost_num", id="bare-star-fast"),
        pytest.param("has_ghost:true", "ghost_num", id="boolean-exists-term"),
        pytest.param("ghost_nonfast:*", "ghost_nonfast", id="bare-star-term-scan"),
        # JSON subpaths report the subpath-qualified ref, and reach tantivy
        # through whichever of the two subpath routes the installed
        # tantivy-py supports (a direct term/phrase query, or the
        # parse_query fallback); both must classify drift the same way.
        pytest.param(
            "ghost_json.user:*", FieldRef("ghost_json", "user"), id="json-subpath-bare-star"
        ),
        pytest.param(
            "ghost_json.user:alice", FieldRef("ghost_json", "user"), id="json-subpath-term"
        ),
        pytest.param(
            'ghost_json.user:"alice smith"',
            FieldRef("ghost_json", "user"),
            id="json-subpath-phrase",
        ),
    ],
)
def test_drift_from_real_query_text_reports_the_misconfiguration(
    qs: str, field: str | FieldRef, tindex: TIndex, drifted: FieldRegistry
) -> None:
    """The same totality, driven through the parser rather than hand-built
    nodes, so it covers what an actual host request can produce.
    """
    from whoosh_compat import parse as _parse

    result = _parse(qs, registry=drifted, default_fields=["content"])
    assert not result.diagnostics, f"expected a clean parse for {qs!r}, got {result.diagnostics!r}"
    with pytest.raises(QueryError) as exc:
        emit_ast(result.ast, tindex, drifted)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.SCHEMA_FIELD_MISSING
    assert d.cause is Cause.MISCONFIGURED
    assert d.field == (field if isinstance(field, FieldRef) else FieldRef(field))


def test_other_value_errors_are_still_internal(
    tindex: TIndex, ereg: FieldRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard against over-reclassifying.

    A bare ``ValueError`` from tantivy-py on a field that IS in the schema
    is a defect in this library, not the operator's problem. It must reach
    ``emit``'s backstop as ``BACKEND_REJECTED``/``INTERNAL``, so that a real
    bug surfaces as a monitorable 500 rather than being blamed on the
    deployment and answered with a 400.

    Simulated by making the tantivy call fail for a reason that is not a
    missing field, which is the only way to produce the condition without an
    actual tantivy-py bug: the schema probe still finds "content", so the
    drift branch must decline it.

    That hinges on ``_field_in_schema`` probing with ``regex_query`` rather
    than the ``term_query`` monkeypatched below; if it ever switches to
    ``term_query`` the probe would fail too and this test would report
    SCHEMA_FIELD_MISSING instead, failing loudly, but keep the two in step.
    """
    real_term_query = tantivy.Query.term_query

    def exploding_term_query(
        schema: tantivy.Schema, name: str, value: object, **kwargs: object
    ) -> tantivy.Query:
        if name == "content":
            raise ValueError("simulated tantivy-py defect, nothing to do with the schema")
        return real_term_query(schema, name, value, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tantivy.Query, "term_query", staticmethod(exploding_term_query))

    with pytest.raises(QueryError) as exc:
        emit_ast(ast.Term(field=FieldRef("content"), text="invoice"), tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.BACKEND_REJECTED
    assert d.cause is Cause.INTERNAL


def test_pattern_cap_on_a_present_field_is_still_unsupported(
    tindex: TIndex, ereg: FieldRegistry
) -> None:
    """The other over-reclassification direction, on the pattern path: the
    field IS in the schema, so a ValueError from regex_query really is the
    compiled pattern exceeding tantivy's state cap. That is bad input, and
    must stay UNSUPPORTED rather than being swept up as deployment drift by
    the shared probe.
    """
    node = ast.Wildcard(field=FieldRef("content"), pattern="a" + "?" * 400)
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, ereg)
    d = exc.value.diagnostic
    assert d.kind is DiagnosticKind.PATTERN_TOO_COMPLEX
    assert d.cause is Cause.UNSUPPORTED


@pytest.mark.parametrize(
    "kind_and_field",
    [
        pytest.param((FieldKind.TEXT, "content"), id="text"),
        pytest.param((FieldKind.KEYWORD, "tag"), id="keyword"),
        pytest.param((FieldKind.U64, "asn"), id="u64"),
        pytest.param((FieldKind.DATE, "created"), id="date"),
        pytest.param((FieldKind.JSON, "notes"), id="json"),
    ],
)
def test_schema_probe_is_kind_independent(
    kind_and_field: tuple[FieldKind, str], tindex: TIndex, ereg: FieldRegistry
) -> None:
    """The probe must answer "present" for a field of any kind.

    It underpins the whole split, and an earlier version (an empty-string
    term query) only worked on TEXT/KEYWORD: it raised a *type* error on a
    U64, DATE or JSON field and so reported a field that is present as
    missing. That would have turned every genuine tantivy-py rejection on a
    numeric, date or JSON field into a false MISCONFIGURED, which is exactly
    the failure mode `test_other_value_errors_are_still_internal` forbids.
    """
    _, name = kind_and_field
    emitter = tantivy_.TantivyEmitter(index=tindex[0], registry=ereg)
    assert emitter._field_in_schema(name) is True


def test_schema_probe_reports_a_missing_field(tindex: TIndex, ereg: FieldRegistry) -> None:
    emitter = tantivy_.TantivyEmitter(index=tindex[0], registry=ereg)
    assert emitter._field_in_schema("ghost") is False
