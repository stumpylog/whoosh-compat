"""Multi-word date keyword phrases parse without quotes on a date field.

``added:previous month`` resolves exactly like ``added:"previous month"``.
Whoosh's grammar (and this fork's, before this) only ever saw the first
whitespace-delimited token as the field's value, so the phrases were quoted
by the host with a regex that could not see quotes; owning the widening here
removes that class of corruption (DIVERGENCES.md entry 19).

The widening is deliberately limited to the six phrases the date grammar
already knows as quoted values -- plus a time of day *trailing* one of those
phrases, so that an unquoted spelling reaches the grammar as the same value
the quoted spelling would (see ``test_parser_period_keywords.py`` for what
the grammar then does with it, and for why a *leading* time is not joined).
Nothing else about date-field parsing becomes whitespace-greedy.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
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


# -- a trailing time is part of the joined value ---------------------------


@pytest.mark.parametrize("phrase", ["previous month", "previous year", "this month", "this year"])
def test_trailing_time_narrows_the_calendar_unit_phrases(
    registry: FieldRegistry, phrase: str
) -> None:
    """The *accepting* half of the trailing-time join, which is easy to miss
    because the rejecting half (``previous week``/``quarter``, see
    ``test_parser_period_keywords.py``) is the one that produced a crash.

    These four resolve to a calendar unit rather than a span, so the joined
    time narrows the range instead of making it unusable. That is a change
    from paperless v2, whose phrase-only rewrite left the time behind as a
    free-text term next to a full-period range; the value now matches the
    quoted spelling, which is the whole point of the join.
    """
    quoted = dparse(f'added:"{phrase} noon"', registry)
    bare = dparse(f"added:{phrase} noon", registry)

    assert not bare.diagnostics
    assert bare.ast == quoted.ast
    # A single DateRange, not a DateRange AND a leftover "noon" term.
    assert isinstance(bare.ast, ast.DateRange)
    assert bare.ast.lo is not None
    # Asserted in the query's own zone: noon Berlin is 10:00Z at the UTC+2
    # summer offset, so reading the UTC hour here would pin the offset
    # rather than the time of day the user asked for.
    assert bare.ast.lo.astimezone(BERLIN).hour == 12


def test_trailing_time_pins_one_concrete_narrowed_range(registry: FieldRegistry) -> None:
    """One fully spelled-out expectation, so the equality above cannot be
    satisfied by both spellings breaking together.
    """
    r = dparse("added:previous month noon", registry).ast
    assert isinstance(r, ast.DateRange)
    # BASE is 2026-08-05 Europe/Berlin, so "previous month" is July 2026.
    # Converted rather than hardcoded in UTC, but for the record the values
    # are 2026-07-01T10:00Z and 2026-07-31T10:00:00.000001Z (Berlin is
    # UTC+2 in July), which is what DIVERGENCES.md entry 19 quotes.
    assert r.lo == datetime(2026, 7, 1, 12, 0, tzinfo=BERLIN).astimezone(UTC)
    assert r.hi == datetime(2026, 7, 31, 12, 0, tzinfo=BERLIN).astimezone(UTC) + timedelta(
        microseconds=1
    )


def test_boost_after_an_unquoted_phrase_applies_to_the_joined_value(
    registry: FieldRegistry,
) -> None:
    """The boost binds to the joined node, not to a leftover word: boosts are
    applied by a filter running long after this join, so the join carries no
    boost of its own.
    """
    result = dparse("added:previous month^2", registry)
    assert not result.diagnostics
    assert isinstance(result.ast, ast.Boosted)
    assert result.ast.boost == 2.0
    assert isinstance(result.ast.child, ast.DateRange)


def test_phrase_inside_a_parenthesized_group_is_not_joined(registry: FieldRegistry) -> None:
    """CHARACTERIZATION of a documented spelling inconsistency (entry 19):
    ``added:("previous month")`` works but ``added:(previous month)`` does
    not, because inside a group the field name has already been propagated
    onto both words and the join requires the words after the head to carry
    no field name of their own.

    v2-parity-neutral (the host rewrite it replaces also only fired on a
    phrase directly following ``field:``), so it is pinned rather than
    fixed. If a future change makes the bare form join, this test is where
    that shows up.
    """
    grouped = dparse("added:(previous month)", registry)
    assert [d.raw_value for d in grouped.diagnostics] == ["previous", "month"]

    quoted_in_group = dparse('added:("previous month")', registry)
    assert not quoted_in_group.diagnostics
    assert isinstance(quoted_in_group.ast, ast.DateRange)
