"""A host-supplied ``analyzer`` or ``pattern_normalizer`` that breaks its
return contract fails the query as ``AST_INVALID_SHAPE``, naming the field.

Both callables are host code in the field registry. Their output used to
decide the outcome by accident: a ``pattern_normalizer`` returning a non-str
form got ``AST_INVALID_SHAPE`` or ``BACKEND_REJECTED`` depending on which
Python exception fired first, and an ``analyzer`` returning non-str tokens
(or a bare ``str``, split into characters) searched for the wrong terms and
silently matched nothing. Every leaf that calls either one is swept here, and
an iterable of str that is not a list or tuple keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator

import pytest

from whoosh_compat import ast
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
from .conftest import search_ids

START, END = 6, 13
CONTENT = FieldRef("content")

BAD_RETURNS = [
    pytest.param(None, id="none"),
    pytest.param(5, id="int"),
    pytest.param(b"inv", id="bytes"),
    pytest.param((None,), id="sequence-holding-none"),
    pytest.param(("inv", 5), id="sequence-holding-an-int"),
    pytest.param([b"inv"], id="sequence-holding-bytes"),
]

# Doc 1's content is "invoice total amount"; no other document has a word
# starting "inv".
PATTERN_LEAVES = [
    pytest.param(ast.Prefix(field=CONTENT, text="INV", startchar=START, endchar=END), id="prefix"),
    pytest.param(
        ast.Wildcard(field=CONTENT, pattern="INV?ICE", startchar=START, endchar=END),
        id="wildcard",
    ),
    pytest.param(
        ast.Fuzzy(field=CONTENT, text="INVOISE", startchar=START, endchar=END), id="fuzzy"
    ),
]


def _normalizer_registry(normalizer: Callable[[str], object]) -> FieldRegistry:
    return FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, pattern_normalizer=normalizer)]  # type: ignore[arg-type]
    )


def _assert_normalizer_contract_error(exc: pytest.ExceptionInfo[QueryError]) -> None:
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert diagnostic.cause is Cause.INTERNAL
    assert diagnostic.field == CONTENT
    assert diagnostic.field_kind is FieldKind.TEXT
    assert (diagnostic.startchar, diagnostic.endchar) == (START, END)
    assert "pattern_normalizer" in diagnostic.message


@pytest.mark.parametrize("bad", BAD_RETURNS)
@pytest.mark.parametrize("node", PATTERN_LEAVES)
def test_a_pattern_normalizer_returning_a_non_str_form_fails(
    node: ast.Node, bad: object, tindex: TIndex
) -> None:
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, _normalizer_registry(lambda _: bad))
    _assert_normalizer_contract_error(exc)


@pytest.mark.parametrize("bad", BAD_RETURNS)
def test_a_pattern_normalizer_returning_a_non_str_form_for_a_class_member_fails(
    bad: object, tindex: TIndex
) -> None:
    """A bracket class body is normalized one character at a time, a
    separate call from the literal runs around it. A bad answer there must
    fail too, not be skipped as a member that did not fold.
    """
    node = ast.Wildcard(field=CONTENT, pattern="[I]NVOICE", startchar=START, endchar=END)
    registry = _normalizer_registry(lambda s: bad if len(s) == 1 else s.lower())
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, registry)
    _assert_normalizer_contract_error(exc)


@pytest.mark.parametrize(
    "node",
    [
        *PATTERN_LEAVES,
        pytest.param(ast.Wildcard(field=CONTENT, pattern="[I]NVOICE"), id="wildcard-class-body"),
    ],
)
def test_a_pattern_normalizer_returning_an_iterator_of_str_still_searches(
    node: ast.Node, tindex: TIndex
) -> None:
    def one_form(text: str) -> Iterator[str]:
        yield text.lower()

    query = emit_ast(node, tindex, _normalizer_registry(one_form))
    assert search_ids(tindex[0], query) == [1]


BAD_TOKENS = [
    pytest.param(lambda _: None, id="none"),
    pytest.param(lambda _: 5, id="int"),
    pytest.param(lambda text: text.lower(), id="bare-str"),
    pytest.param(lambda _: [None], id="list-holding-none"),
    pytest.param(lambda _: [5], id="list-holding-an-int"),
    pytest.param(lambda text: [text.lower().encode()], id="list-holding-bytes"),
    pytest.param(lambda text: [text.lower(), None], id="list-holding-a-str-and-none"),
]

ANALYZED_LEAVES = [
    pytest.param(ast.Term(field=CONTENT, text="invoice"), "content", id="term"),
    pytest.param(ast.Phrase(field=CONTENT, text="invoice total"), "content", id="phrase"),
    pytest.param(ast.Term(field=FieldRef("notes", "note"), text="check"), "notes", id="json-term"),
    pytest.param(
        ast.Phrase(field=FieldRef("notes", "note"), text="check this"), "notes", id="json-phrase"
    ),
]


def _analyzer_registry(analyzer: Callable[[str], object]) -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=analyzer),  # type: ignore[arg-type]
            FieldSpec("notes", FieldKind.JSON, subpaths=("note",), analyzer=analyzer),  # type: ignore[arg-type]
        ]
    )


@pytest.mark.parametrize("analyzer", BAD_TOKENS)
@pytest.mark.parametrize(("node", "field"), ANALYZED_LEAVES)
def test_an_analyzer_returning_anything_but_str_tokens_fails(
    node: ast.Node, field: str, analyzer: Callable[[str], object], tindex: TIndex
) -> None:
    with pytest.raises(QueryError) as exc:
        emit_ast(node, tindex, _analyzer_registry(analyzer))
    diagnostic = exc.value.diagnostic
    assert diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert diagnostic.cause is Cause.INTERNAL
    assert "analyzer" in diagnostic.message
    assert repr(field) in diagnostic.message


@pytest.mark.parametrize("analyzer", BAD_TOKENS)
def test_analyze_raises_type_error_for_a_broken_analyzer(
    analyzer: Callable[[str], object],
) -> None:
    """A host calling ``analyze()`` directly gets the same check, as a
    ``TypeError`` naming the field.
    """
    with pytest.raises(TypeError, match="analyzer for field 'content'"):
        ast.analyze(ast.Term(field=CONTENT, text="invoice"), _analyzer_registry(analyzer))


@pytest.mark.parametrize(("node", "field"), ANALYZED_LEAVES)
def test_an_analyzer_returning_an_iterator_of_str_still_searches(
    node: ast.Node, field: str, tindex: TIndex
) -> None:
    def tokens(text: str) -> Iterator[str]:
        yield from lower_fold(text)

    query = emit_ast(node, tindex, _analyzer_registry(tokens))
    assert search_ids(tindex[0], query) == [1]
