"""End-to-end searches for ``analyze()``'s ``rewrite_leaf`` hook: a host
widens parsed leaves with a companion clause on another field, and the
matched documents show each original leaf kept its multi-token context and
zero-token drop behavior.

Documents (see conftest.py): 1 "Billing 2020", 2 "Billing 2019", 3
"Wärrantyplan", 4 "Report 2020", 5 "Miscellaneous Doc". Content: doc 2
holds shopname and product1, doc 4 holds shopname, product1 and product2.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from whoosh_compat import ast
from whoosh_compat import parse as _parse
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import TIndex
from .conftest import lower_fold
from .conftest import search_ids

TITLE = FieldRef("title")


def _search(
    qs: str,
    registry: FieldRegistry,
    tindex: TIndex,
    hook: Callable[[ast.Term | ast.Phrase], ast.Node] | None,
) -> list[int]:
    parsed = _parse(qs, registry=registry, default_fields=["content"])
    assert not parsed.diagnostics, parsed.diagnostics
    tree = ast.analyze(parsed.ast, registry, rewrite_leaf=hook)
    return search_ids(tindex[0], emit_(tree, index=tindex[0], registry=registry))


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
) -> None:
    # The widened set differs from the naive one: loosening the split term
    # from AND to OR would let doc 2 (shopname without product2) match.
    assert _search(qs, ereg, tindex, None) == plain
    assert _search(qs, ereg, tindex, _companion_for("shopname-product2", "wärrantyplan")) == widened
    naive_hook = _companion_for("shopname-product2", "wärrantyplan", copy_leaf=True)
    assert _search(qs, ereg, tindex, naive_hook) == naive


def test_a_zero_token_leaf_under_not_excludes_its_companion(
    ereg: FieldRegistry, tindex: TIndex
) -> None:
    # A regression guard for the drop path rather than evidence for context:
    # Or drops the empty original either way, so a copied leaf gives the
    # same set here.
    qs = "content:shopname AND NOT content:'!!'"
    assert _search(qs, ereg, tindex, None) == [2, 4]
    assert _search(qs, ereg, tindex, _companion_for("!!", "billing")) == [4]


def test_a_removed_leaf_leaves_its_sibling_standing(ereg: FieldRegistry, tindex: TIndex) -> None:
    def remove_product2(leaf: ast.Term | ast.Phrase) -> ast.Node:
        return ast.Nothing() if leaf.text == "product2" else leaf

    qs = "content:shopname AND content:product2"
    assert _search(qs, ereg, tindex, None) == [4]
    assert _search(qs, ereg, tindex, remove_product2) == [2, 4]


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
    qs: str, plain: list[int], widened: list[int], short_reg: FieldRegistry, tindex: TIndex
) -> None:
    assert _search(qs, short_reg, tindex, None) == plain
    assert (
        _search(qs, short_reg, tindex, _companion_for("averyveryverylongword", "billing"))
        == widened
    )
