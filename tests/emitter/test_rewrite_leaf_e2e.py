"""End-to-end searches for the ``rewrite_leaf`` hook: a host widens parsed
leaves with a companion clause on another field, and the matched documents
show each original leaf kept its multi-token context and zero-token drop
behavior.

Every search runs through both call shapes a host can use: the hook passed
to ``emit()`` directly, and ``analyze()`` with the hook followed by
``emit()``. Both must match the same documents.

Documents (see conftest.py): 1 "Billing 2020", 2 "Billing 2019", 3
"Wärrantyplan", 4 "Report 2020", 5 "Miscellaneous Doc". Content: doc 2
holds shopname and product1, doc 3 holds basement, doc 4 holds shopname,
product1 and product2. Notes users: doc 1 alice, doc 4 bob.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import Cause
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import lower_fold
from .conftest import search_ids

TITLE = FieldRef("title")


@pytest.fixture(
    params=[
        pytest.param(True, id="hook-passed-to-emit"),
        pytest.param(False, id="analyze-then-emit"),
    ]
)
def through_emit(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


def _search(
    qs: str,
    registry: FieldRegistry,
    tindex: TIndex,
    hook: Callable[[ast.Term | ast.Phrase], ast.Node] | None,
    through_emit: bool,
) -> list[int]:
    parsed = _parse(qs, registry=registry, default_fields=["content"])
    assert not parsed.diagnostics, parsed.diagnostics
    if through_emit:
        query = emit_(parsed.ast, index=tindex[0], registry=registry, rewrite_leaf=hook)
    else:
        tree = ast.analyze(parsed.ast, registry, rewrite_leaf=hook)
        query = emit_(tree, index=tindex[0], registry=registry)
    return search_ids(tindex[0], query)


def _companion_for(
    text: str, companion: str, *, copy_leaf: bool = False
) -> Callable[[ast.Term | ast.Phrase], ast.Node]:
    """Wrap each leaf whose raw text is ``text``, on any field, as
    ``Or(leaf, title:companion)``; keep every other leaf.

    ``copy_leaf=True`` is what a host gets by copying the leaf into the
    wrapper instead of placing the leaf itself: the copy is analyzed under
    the wrapper's Or.
    """

    def hook(leaf: ast.Term | ast.Phrase) -> ast.Node:
        if leaf.text != text:
            return leaf
        kept: ast.Node = ast.Term(field=leaf.field, text=str(leaf.text)) if copy_leaf else leaf
        return ast.Or(children=(kept, ast.Term(field=TITLE, text=companion)))

    return hook


@pytest.mark.parametrize(
    ("qs", "plain", "widened", "naive"),
    [
        pytest.param("content:shopname-product2", [4], [3, 4], [2, 3, 4], id="split-term-alone"),
        pytest.param(
            "content:shopname-product2 AND content:product1",
            [4],
            [4],
            [2, 4],
            id="split-term-in-and",
        ),
    ],
)
def test_a_split_term_keeps_requiring_every_token(
    qs: str,
    plain: list[int],
    widened: list[int],
    naive: list[int],
    ereg: FieldRegistry,
    tindex: TIndex,
    through_emit: bool,
) -> None:
    # The widened set differs from the naive one: loosening the split term
    # from AND to OR would let doc 2 (shopname without product2) match.
    hook = _companion_for("shopname-product2", "wärrantyplan")
    naive_hook = _companion_for("shopname-product2", "wärrantyplan", copy_leaf=True)
    assert _search(qs, ereg, tindex, None, through_emit) == plain
    assert _search(qs, ereg, tindex, hook, through_emit) == widened
    assert _search(qs, ereg, tindex, naive_hook, through_emit) == naive


def test_a_split_term_in_an_or_keeps_its_or_context(
    ereg: FieldRegistry, tindex: TIndex, through_emit: bool
) -> None:
    # Under an OR the split term resolves to either token (doc 2 has only
    # shopname). The companion adds doc 1 ("Billing 2020"). Were the pinned
    # leaf analyzed as AND instead, doc 2 would be lost: neither the
    # companion (docs 1 and 4) nor basement (doc 3) brings it back.
    qs = "content:shopname-product2 OR content:basement"
    hook = _companion_for("shopname-product2", "2020")
    assert _search(qs, ereg, tindex, None, through_emit) == [2, 3, 4]
    assert _search(qs, ereg, tindex, hook, through_emit) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("qs", "text", "companion", "plain", "widened"),
    [
        pytest.param("content:basement", "basement", "report", [3], [3, 4], id="term-at-root"),
        pytest.param(
            'content:"product1 product2"',
            "product1 product2",
            "wärrantyplan",
            [4],
            [3, 4],
            id="phrase",
        ),
        pytest.param("notes.user:alice", "alice", "report", [1], [1, 4], id="json-subpath"),
        pytest.param(
            "content:product1 ANDNOT content:basement",
            "basement",
            "report",
            [2, 4],
            [2],
            id="andnot-negative-side",
        ),
    ],
)
def test_a_widened_leaf_searches_its_companion(
    qs: str,
    text: str,
    companion: str,
    plain: list[int],
    widened: list[int],
    ereg: FieldRegistry,
    tindex: TIndex,
    through_emit: bool,
) -> None:
    # On AndNot's negative side the companion widens what is excluded: doc
    # 4 ("Report 2020") drops out.
    assert _search(qs, ereg, tindex, None, through_emit) == plain
    assert _search(qs, ereg, tindex, _companion_for(text, companion), through_emit) == widened


def test_a_zero_token_leaf_under_not_excludes_its_companion(
    ereg: FieldRegistry, tindex: TIndex, through_emit: bool
) -> None:
    # A regression guard for the drop path rather than evidence for context:
    # Or drops the empty original either way, so a copied leaf gives the
    # same set here.
    qs = "content:shopname AND NOT content:'!!'"
    assert _search(qs, ereg, tindex, None, through_emit) == [2, 4]
    assert _search(qs, ereg, tindex, _companion_for("!!", "billing"), through_emit) == [4]


def test_a_removed_leaf_leaves_its_sibling_standing(
    ereg: FieldRegistry, tindex: TIndex, through_emit: bool
) -> None:
    def remove_product2(leaf: ast.Term | ast.Phrase) -> ast.Node:
        return ast.Nothing() if leaf.text == "product2" else leaf

    qs = "content:shopname AND content:product2"
    assert _search(qs, ereg, tindex, None, through_emit) == [4]
    assert _search(qs, ereg, tindex, remove_product2, through_emit) == [2, 4]


def _short_words(text: str) -> list[str]:
    """lower_fold, then drop any token longer than 12 characters, the way a
    host's length filter drops an over-long run.
    """
    return [token for token in lower_fold(text) if len(token) <= 12]


@pytest.fixture
def short_reg() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec(
                "content", FieldKind.TEXT, analyzer=_short_words, pattern_normalizer=str.lower
            ),
            FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold, pattern_normalizer=str.lower),
        ]
    )


@pytest.mark.parametrize(
    ("qs", "plain", "widened"),
    [
        pytest.param("content:averyveryverylongword", [], [1, 2], id="alone"),
        pytest.param(
            "content:shopname AND NOT content:averyveryverylongword",
            [2, 4],
            [4],
            id="under-not",
        ),
    ],
)
def test_a_leaf_the_analyzer_drops_still_widens(
    qs: str,
    plain: list[int],
    widened: list[int],
    short_reg: FieldRegistry,
    tindex: TIndex,
    through_emit: bool,
) -> None:
    hook = _companion_for("averyveryverylongword", "billing")
    assert _search(qs, short_reg, tindex, None, through_emit) == plain
    assert _search(qs, short_reg, tindex, hook, through_emit) == widened


# -- Errors through emit(): the hook and the field analyzers run inside
# -- emit()'s input-stage backstop, so the same exception types convert.


class _HostError(ValueError):
    """A host's own exception type that happens to subclass ValueError."""


def _raising(exc: BaseException) -> Callable[[ast.Term | ast.Phrase], ast.Node]:
    def hook(leaf: ast.Term | ast.Phrase) -> ast.Node:
        raise exc

    return hook


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("hook failed"), id="value-error"),
        pytest.param(TypeError("hook failed"), id="type-error"),
        pytest.param(_HostError("hook failed"), id="host-subclass-of-value-error"),
    ],
)
def test_a_hook_error_of_an_allowlisted_type_becomes_an_internal_query_error(
    exc: Exception, ereg: FieldRegistry, tindex: TIndex
) -> None:
    parsed = _parse("content:basement", registry=ereg, default_fields=["content"])
    with pytest.raises(QueryError) as caught:
        emit_(parsed.ast, index=tindex[0], registry=ereg, rewrite_leaf=_raising(exc))
    assert caught.value.diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert caught.value.diagnostic.cause is Cause.INTERNAL
    assert caught.value.__context__ is exc


def test_a_hook_error_of_another_type_escapes_unchanged(
    ereg: FieldRegistry, tindex: TIndex
) -> None:
    exc = KeyError("hook failed")
    parsed = _parse("content:basement", registry=ereg, default_fields=["content"])
    with pytest.raises(KeyError) as caught:
        emit_(parsed.ast, index=tindex[0], registry=ereg, rewrite_leaf=_raising(exc))
    assert caught.value is exc


def test_a_hook_returning_a_non_node_is_an_internal_query_error(
    ereg: FieldRegistry, tindex: TIndex
) -> None:
    def returns_text(leaf: ast.Term | ast.Phrase) -> ast.Node:
        return "basement"  # type: ignore[return-value]  # the contract violation under test

    parsed = _parse("content:basement", registry=ereg, default_fields=["content"])
    with pytest.raises(QueryError) as caught:
        emit_(parsed.ast, index=tindex[0], registry=ereg, rewrite_leaf=returns_text)
    assert caught.value.diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert caught.value.diagnostic.cause is Cause.INTERNAL
    assert "rewrite_leaf must return an ast.Node, got str" in caught.value.diagnostic.message


def test_a_malformed_replacement_fails_like_a_hand_built_tree(
    ereg: FieldRegistry, tindex: TIndex
) -> None:
    # Analysis passes the bad boost through; tantivy-py refuses it while the
    # query is built, as it would for the same hand-built tree.
    def bad_boost(leaf: ast.Term | ast.Phrase) -> ast.Node:
        return ast.Boosted(child=leaf, boost="x")  # type: ignore[arg-type]  # under test

    parsed = _parse("content:basement", registry=ereg, default_fields=["content"])
    with pytest.raises(QueryError) as caught:
        emit_(parsed.ast, index=tindex[0], registry=ereg, rewrite_leaf=bad_boost)
    assert caught.value.diagnostic.kind is DiagnosticKind.BACKEND_REJECTED
    assert caught.value.diagnostic.cause is Cause.INTERNAL


def test_a_field_analyzer_error_through_emit_is_an_internal_query_error(
    tindex: TIndex,
) -> None:
    exc = ValueError("analyzer failed")

    def refusing(text: str) -> list[str]:
        raise exc

    reg = FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=refusing),
            FieldSpec("title", FieldKind.TEXT, analyzer=lower_fold),
        ]
    )
    parsed = _parse("content:basement", registry=reg, default_fields=["content"])
    with pytest.raises(QueryError) as caught:
        emit_(parsed.ast, index=tindex[0], registry=reg)
    assert caught.value.diagnostic.kind is DiagnosticKind.AST_INVALID_SHAPE
    assert caught.value.diagnostic.cause is Cause.INTERNAL
    assert caught.value.__context__ is exc
