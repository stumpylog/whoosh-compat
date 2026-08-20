"""Multi-word date keyword phrases parse without quotes on a date field.

``added:previous month`` resolves exactly like ``added:"previous month"``.
Whoosh's grammar (and this fork's, before this) only ever saw the first
whitespace-delimited token as the field's value, so the phrases were quoted
by the host with a regex that could not see quotes; owning the widening here
removes that class of corruption (DIVERGENCES.md entry 19).

The widening is deliberately limited to the six phrases the date grammar
already knows as quoted values -- plus, when one of those phrases is present,
an adjacent time of day, so that an unquoted spelling reaches the grammar as
the same value the quoted spelling would (see
``test_parser_period_keywords.py`` for what the grammar then does with it).
Nothing else about date-field parsing becomes whitespace-greedy.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

BERLIN = ZoneInfo("Europe/Berlin")
# A Wednesday, so "previous week" spans a whole distinct Mon-Sun block.
BASE = datetime(2026, 8, 5, 10, 30, tzinfo=BERLIN)

PHRASES = [
    "previous week",
    "previous month",
    "previous quarter",
    "previous year",
    "this month",
    "this year",
]


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT),
            FieldSpec("title", FieldKind.TEXT),
            FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
            FieldSpec("added", FieldKind.DATETIME, fast=True),
        ]
    )


def dparse(q: str, registry: FieldRegistry) -> wc.ParseResult:
    return wc.parse(q, registry=registry, default_fields=["content"], tz=BERLIN, basedate=BASE)


@pytest.mark.parametrize("phrase", PHRASES)
@pytest.mark.parametrize("field", ["added", "created"])
def test_multi_word_date_phrase_parses_unquoted(
    registry: FieldRegistry, field: str, phrase: str
) -> None:
    quoted = dparse(f'{field}:"{phrase}"', registry)
    bare = dparse(f"{field}:{phrase}", registry)

    assert not bare.diagnostics
    assert isinstance(bare.ast, ast.DateRange)
    assert bare.ast == quoted.ast


@pytest.mark.parametrize("phrase", PHRASES)
def test_unquoted_phrase_is_case_insensitive_like_the_quoted_form(
    registry: FieldRegistry, phrase: str
) -> None:
    """The grammar matches its keywords case-insensitively, so the unquoted
    spelling must too: the host's regex it replaces was case-insensitive on
    the phrase as well.
    """
    quoted = dparse(f'added:"{phrase}"', registry)
    bare = dparse(f"added:{phrase.upper()}", registry)

    assert not bare.diagnostics
    assert bare.ast == quoted.ast


def test_unquoted_phrase_spans_the_whole_phrase_in_source(registry: FieldRegistry) -> None:
    """The merged value reports the source span of the full phrase, so a
    later diagnostic or highlight points at what the user actually typed.
    """
    q = "added:previous week 3pm"
    result = dparse(q, registry)
    assert [d.raw_value for d in result.diagnostics] == ["previous week 3pm"]
    # From the start of the value (spans exclude the "added:" prefix
    # everywhere else too) to the end of the last joined word.
    assert [(d.startchar, d.endchar) for d in result.diagnostics] == [(q.index("previous"), len(q))]


# -- the widening stops at the six phrases ---------------------------------


def test_phrase_on_a_text_field_is_not_merged(registry: FieldRegistry) -> None:
    """``title:previous month`` is a TEXT field: no date grammar is involved,
    so it stays two terms (a fielded one and a default-field one), exactly as
    before.
    """
    result = dparse("title:previous month", registry)
    assert not result.diagnostics
    assert result.ast == ast.And(
        children=(
            ast.Term(field=FieldRef("title"), text="previous"),
            ast.Term(field=FieldRef("content"), text="month"),
        )
    )


def test_following_operator_is_not_swallowed(registry: FieldRegistry) -> None:
    """The token after the phrase is still an operator, not part of the date."""
    result = dparse("added:previous week AND title:foo", registry)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.And)
    assert len(result.ast.children) == 2
    assert isinstance(result.ast.children[0], ast.DateRange)
    assert result.ast.children[1] == ast.Term(field=FieldRef("title"), text="foo")


def test_following_ordinary_word_is_not_swallowed(registry: FieldRegistry) -> None:
    """A plain word after the phrase stays a default-field term."""
    result = dparse("added:previous week invoice", registry)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.And)
    assert len(result.ast.children) == 2
    assert isinstance(result.ast.children[0], ast.DateRange)
    assert result.ast.children[1] == ast.Term(field=FieldRef("content"), text="invoice")


@pytest.mark.parametrize(
    "q",
    [
        # "this week"/"this quarter" are not keywords the grammar knows in
        # any spelling, quoted or not.
        pytest.param("added:this week", id="unknown-phrase-tail"),
        # A word that is not one of the six heads.
        pytest.param("added:next month", id="unknown-phrase-head"),
        # A fielded second token belongs to its own field.
        pytest.param("added:previous title:month", id="second-token-is-fielded"),
        # A wildcard is a different node type, not a bare word.
        pytest.param("added:previous mont*", id="second-token-is-a-wildcard"),
        # A parenthesized group is not a bare word either.
        pytest.param("added:previous (month)", id="second-token-is-a-group"),
    ],
)
def test_near_misses_still_diagnose_the_bare_head_token(registry: FieldRegistry, q: str) -> None:
    """Every one of these leaves ``added:<word>`` as its own (unparseable)
    date value, exactly as before this widening.
    """
    result = dparse(q, registry)
    assert [d.raw_value for d in result.diagnostics] == [q.split(":", 1)[1].split(" ")[0]]


def test_quoted_phrase_inside_a_text_phrase_is_untouched(registry: FieldRegistry) -> None:
    """The regression the host's quote-blind rewrite could not avoid: the
    phrase spelling occurring inside an unrelated quoted phrase stays a
    literal phrase on its own field.
    """
    result = dparse('title:"see added:previous month notes"', registry)
    assert not result.diagnostics
    assert result.ast == ast.Phrase(
        field=FieldRef("title"),
        text="see added:previous month notes",
    )
