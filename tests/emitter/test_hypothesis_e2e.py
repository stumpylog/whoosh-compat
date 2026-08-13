"""End-to-end resilience property: parse + emit against a real in-memory
tantivy index must never raise anything except the documented
:class:`~whoosh_compat.errors.UnsupportedQueryError`.

The differential suite (``tests/differential/test_hypothesis.py``) only ever
calls ``whoosh_compat.parse()``: it never builds a ``tantivy.Query`` or runs
one against an index, so it can't reach bugs specific to *emission*: glob-to-
regex translation, the all-``MustNot`` boolean padding, date-to-naive-UTC
conversion, JSON-subpath escaping. This module closes that gap using the
same shared ``tindex``/``ereg`` fixtures the rest of ``tests/emitter/``
already indexes and parses against (``tests/emitter/conftest.py``), fed by a
grammar generator scoped to that registry's actual field vocabulary (a
different, smaller field set than the differential suite's oracle
registry, see ``ereg``'s docstring), reusing the differential grammar's
word-level building blocks so the two generators agree on what a "word"
looks like.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from tests.differential.strategies import DATE_KEYWORDS
from tests.differential.strategies import DATE_RELATIVE
from tests.differential.strategies import phrase_word
from tests.differential.strategies import plain_word
from tests.differential.strategies import wildcard_base
from whoosh_compat import parse as wc_parse
from whoosh_compat.ast import normalize
from whoosh_compat.emitters.tantivy_ import emit as emit_
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.fields import FieldRegistry

from .conftest import TIndex

BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2020, 4, 15, 10, 30, tzinfo=BERLIN)

_TEXT_FIELDS = ("content", "title")
_KEYWORD_FIELDS = ("tag",)
_NUM_FIELDS = ("tag_id", "asn")
_DATE_FIELDS = ("created", "added")
_BOOL_EXISTS_FIELDS = ("has_tag",)
_JSON_SUBPATHS = ("notes.note", "notes.user")
_DEFAULT_FIELDS = ["content", "title", "tag"]

# lower_fold (ereg's TEXT/KEYWORD analyzer, see conftest.py) only splits on
# non-word characters: unlike the oracle's StandardAnalyzer it has no
# stopword list or minsize filter, so the differential suite's
# ZERO_TOKEN_WORDS vocabulary doesn't apply here. A punctuation/whitespace-
# only value is what actually reduces to zero tokens under lower_fold (the
# same shape tests/emitter/conftest.py's own doc 3/4 fixture rows use for
# exactly this purpose).
_ZERO_TOKEN_LIKE = ("---", "...", "  ", "***", "___")


@st.composite
def _term_atom(draw: st.DrawFn) -> str:
    field = draw(st.one_of(st.none(), st.sampled_from(_TEXT_FIELDS + _KEYWORD_FIELDS)))
    text = draw(plain_word)
    return f"{field}:{text}" if field else text


@st.composite
def _zero_token_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_TEXT_FIELDS))
    text = draw(st.sampled_from(_ZERO_TOKEN_LIKE))
    return f'{field}:"{text}"'


@st.composite
def _phrase_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_TEXT_FIELDS + _KEYWORD_FIELDS))
    n = draw(st.integers(min_value=1, max_value=4))
    parts = draw(st.lists(phrase_word, min_size=n, max_size=n))
    slop = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=3)))
    slop_s = f"~{slop}" if slop is not None else ""
    return f'{field}:"{" ".join(parts)}"{slop_s}'


@st.composite
def _wildcard_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_TEXT_FIELDS + _KEYWORD_FIELDS))
    base = draw(wildcard_base)
    shape = draw(st.sampled_from(["trailing", "leading", "infix", "bracket_trailing", "question"]))
    if shape == "trailing":
        pattern = f"{base}*"
    elif shape == "leading":
        pattern = f"*{base}"
    elif shape == "infix":
        pattern = f"{base}*{base}"
    elif shape == "bracket_trailing":
        lo = draw(st.integers(min_value=0, max_value=5))
        hi = draw(st.integers(min_value=lo, max_value=9))
        pattern = f"{base}[{lo}-{hi}]*"
    else:
        pattern = f"{base}?{base}"
    return f"{field}:{pattern}"


@st.composite
def _numeric_range_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_NUM_FIELDS))
    lo = draw(st.integers(min_value=0, max_value=100_000))
    hi = draw(st.integers(min_value=lo, max_value=lo + 100_000))
    open_lo = draw(st.booleans())
    open_hi = draw(st.booleans())
    ob = draw(st.sampled_from(["[", "{"]))
    cb = draw(st.sampled_from(["]", "}"]))
    lo_s = "" if open_lo else f"{lo} "
    hi_s = "" if open_hi else f" {hi}"
    return f"{field}:{ob}{lo_s}TO{hi_s}{cb}"


@st.composite
def _date_bare_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_DATE_FIELDS))
    shape = draw(st.sampled_from(["year", "compact", "separated", "keyword", "relative"]))
    if shape == "year":
        value = f"{draw(st.integers(min_value=1, max_value=9999)):04d}"
    elif shape == "compact":
        y = draw(st.integers(min_value=1900, max_value=2100))
        m = draw(st.integers(min_value=1, max_value=12))
        d = draw(st.integers(min_value=1, max_value=28))
        value = f"{y:04d}{m:02d}{d:02d}"
    elif shape == "separated":
        y = draw(st.integers(min_value=1900, max_value=2100))
        m = draw(st.integers(min_value=1, max_value=12))
        d = draw(st.integers(min_value=1, max_value=28))
        value = f"{y:04d}-{m:02d}-{d:02d}"
    elif shape == "keyword":
        value = draw(st.sampled_from(DATE_KEYWORDS))
    else:
        value = draw(st.sampled_from(DATE_RELATIVE))
    return f"{field}:{value}"


@st.composite
def _date_range_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_DATE_FIELDS))
    lo = draw(st.one_of(st.just(""), st.integers(min_value=1, max_value=9999).map(str)))
    hi = draw(st.one_of(st.just(""), st.integers(min_value=1, max_value=9999).map(str)))
    ob = draw(st.sampled_from(["[", "{"]))
    cb = draw(st.sampled_from(["]", "}"]))
    lo_s = "" if lo == "" else f"{lo} "
    hi_s = "" if hi == "" else f" {hi}"
    return f"{field}:{ob}{lo_s}TO{hi_s}{cb}"


@st.composite
def _comma_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_KEYWORD_FIELDS))
    n = draw(st.integers(min_value=2, max_value=4))
    parts = draw(st.lists(phrase_word, min_size=n, max_size=n))
    quoted = draw(st.booleans())
    joined = ",".join(parts)
    text = f"'{joined}'" if quoted else joined
    return f"{field}:{text}"


@st.composite
def _json_atom(draw: st.DrawFn) -> str:
    path = draw(st.sampled_from(_JSON_SUBPATHS))
    value = draw(plain_word)
    return f"{path}:{value}"


@st.composite
def _bool_exists_atom(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(_BOOL_EXISTS_FIELDS))
    value = draw(st.sampled_from(["true", "false"]))
    return f"{field}:{value}"


_every_atom = st.sampled_from(["*", "*:*"] + [f"{f}:*" for f in _TEXT_FIELDS + _KEYWORD_FIELDS])

_leaves = st.one_of(
    _term_atom(),
    _zero_token_atom(),
    _phrase_atom(),
    _wildcard_atom(),
    _numeric_range_atom(),
    _date_bare_atom(),
    _date_range_atom(),
    _comma_atom(),
    _json_atom(),
    _bool_exists_atom(),
    _every_atom,
)

_BOOSTS = ("0.5", "1.5", "2", "2.0", "10")
_BINARY_OPS = ("AND", "OR", "ANDNOT", "ANDMAYBE", "REQUIRE")


def _extend(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    group = children.map(lambda c: f"({c})")
    negated = children.map(lambda c: f"NOT ({c})")
    boosted = st.tuples(children, st.sampled_from(_BOOSTS)).map(lambda t: f"({t[0]})^{t[1]}")
    binary = st.tuples(children, st.sampled_from(_BINARY_OPS), children).map(
        lambda t: f"({t[0]}) {t[1]} ({t[2]})"
    )
    implicit_and = st.tuples(children, children).map(lambda t: f"{t[0]} {t[1]}")
    return st.one_of(group, negated, boosted, binary, implicit_and)


def _query_text(max_leaves: int = 6) -> st.SearchStrategy[str]:
    return st.recursive(_leaves, _extend, max_leaves=max_leaves)


@given(q=_query_text(max_leaves=6))
@settings(
    max_examples=300,
    deadline=None,
    # ereg/tindex are read-only, immutable fixtures (a FieldRegistry and a
    # committed in-RAM tantivy index): reusing the same instance across every
    # generated example within one test invocation, rather than rebuilding
    # per example, is exactly what's wanted here, not a source of state
    # leakage between examples.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_emit_never_raises_except_unsupported(q: str, tindex: TIndex, ereg: FieldRegistry) -> None:
    result = wc_parse(q, registry=ereg, default_fields=_DEFAULT_FIELDS, tz=BERLIN, basedate=BASE)
    if result.diagnostics:
        # A diagnostic means the AST contains an ErrorLeaf: emitting one is
        # documented to raise QueryEmitError (ARCHITECTURE.md's "Diagnostics
        # never raise mid-parse" invariant is a *parsing* guarantee; emitting
        # a tree a caller was already told not to emit is a separate,
        # intentional contract). This property is about queries that parsed
        # cleanly, so skip the ones that didn't.
        return
    # The one documented, expected exception: a text-field TermRange
    # (DIVERGENCES.md entry 5, tantivy-py has no programmatic text-range
    # API) is the only construct that's parseable but genuinely
    # inexecutable against tantivy.
    with contextlib.suppress(UnsupportedQueryError):
        emit_(result.ast, index=tindex[0], registry=ereg)


@given(q=_query_text(max_leaves=6))
@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_normalize_idempotent_on_emitter_registry_grammar(q: str, ereg: FieldRegistry) -> None:
    # A second, registry-independent angle on normalize()'s totality/
    # idempotence contract (the primary property lives in
    # tests/differential/test_hypothesis.py, against the oracle registry's
    # larger field vocabulary): this repeats it against ereg's grammar so
    # the JSON-subpath and BOOLEAN_EXISTS node shapes ereg actually emits
    # (neither of which the oracle registry has an equivalent for) are
    # covered by *some* normalize() property too, not just parsed once and
    # left untested.
    result = wc_parse(q, registry=ereg, default_fields=_DEFAULT_FIELDS, tz=BERLIN, basedate=BASE)
    once = normalize(result.ast)
    twice = normalize(once)
    assert once == twice, f"normalize() not idempotent for query: {q!r}"
