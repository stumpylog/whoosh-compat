"""Hypothesis properties of ``analyze()``'s ``rewrite_leaf`` hook over the
grammar-aware query strategy: a hook that keeps each leaf changes nothing,
the original leaf keeps its own context inside a replacement, and a hooked
result needs no further analysis.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import settings

from tests.differential.oracle import ORACLE_REGISTRY
from tests.differential.oracle import V2_FIELDS
from tests.differential.oracle import compat_raw_parse
from tests.differential.strategies import query_text
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import Or
from whoosh_compat.ast import Phrase
from whoosh_compat.ast import Term
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import Multitoken

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 8, 4, 10, 30, tzinfo=BERLIN)

MODES = [
    pytest.param(Multitoken.AND, id="default-and"),
    pytest.param(Multitoken.OR, id="default-or"),
]


def _raw(q: str) -> Node:
    # compat_raw_parse: a freshly parsed, not-yet-normalized tree, so the
    # hook's leaves are the ones analyze() finds after its own normalize.
    node, _diagnostics = compat_raw_parse(q, ORACLE_REGISTRY, V2_FIELDS, BERLIN, BASE)
    return node


def _keep(leaf: Term | Phrase) -> Node:
    return leaf


def _with_empty_companion(leaf: Term | Phrase) -> Node:
    # The companion analyzes to zero tokens, so the replacement's Or survives
    # normalization and only collapses during analysis: the leaf is analyzed
    # while it sits inside that Or, and must still resolve in its own context.
    return Or(children=(leaf, Term(field=FieldRef("title"), text="")))


def _with_title_companion(leaf: Term | Phrase) -> Node:
    return Or(children=(leaf, Term(field=FieldRef("title"), text=f"{leaf.text} extra")))


def _drop_or_negate_or_widen(leaf: Term | Phrase) -> Node:
    text = str(leaf.text)
    if len(text) % 2 == 1:
        return Nothing()
    if len(text) % 3 == 0:
        return Not(child=leaf)
    return _with_title_companion(leaf)


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param(_keep, id="keep"),
        pytest.param(_with_empty_companion, id="empty-companion"),
    ],
)
@pytest.mark.parametrize("mode", MODES)
@given(query_text(max_leaves=6))
@settings(max_examples=300, deadline=None)
def test_a_hook_that_adds_nothing_matches_plain_analysis(
    hook: Callable[[Term | Phrase], Node], mode: Multitoken, q: str
) -> None:
    # "keep" returns every leaf as is; "empty-companion" places each leaf
    # inside a replacement, where it must still resolve in its own context.
    raw = _raw(q)
    assert analyze(raw, ORACLE_REGISTRY, default_mode=mode, rewrite_leaf=hook) == analyze(
        raw, ORACLE_REGISTRY, default_mode=mode
    ), f"query: {q!r}"


@pytest.mark.parametrize("mode", MODES)
@given(query_text(max_leaves=6))
@settings(max_examples=300, deadline=None)
def test_a_hooked_result_needs_no_further_analysis(mode: Multitoken, q: str) -> None:
    once = analyze(_raw(q), ORACLE_REGISTRY, default_mode=mode, rewrite_leaf=_with_title_companion)
    assert analyze(once, ORACLE_REGISTRY, default_mode=mode) == once, f"query: {q!r}"


@pytest.mark.parametrize("mode", MODES)
@given(query_text(max_leaves=6))
@settings(max_examples=300, deadline=None)
def test_hooked_analysis_is_insensitive_to_pre_normalization(mode: Multitoken, q: str) -> None:
    raw = _raw(q)
    assert analyze(
        raw, ORACLE_REGISTRY, default_mode=mode, rewrite_leaf=_drop_or_negate_or_widen
    ) == analyze(
        normalize(raw), ORACLE_REGISTRY, default_mode=mode, rewrite_leaf=_drop_or_negate_or_widen
    ), f"query: {q!r}"
