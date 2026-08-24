"""Unit tests for :func:`whoosh_compat.ast.analyze`, the explicit analysis
pipeline stage between ``normalize()`` and an emitter's structural visit.

Worked examples covering the shapes the design was reasoned through: a
single-token TEXT term (structural no-op), a multi-token TEXT term under
each :class:`Multitoken` mode, a zero-token term, a JSON-subpath term (plain
and multi-token), a quoted Phrase, and a kind-restricted (U64/DATE/
BOOLEAN_EXISTS) term/phrase that analysis must never touch.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

import whoosh_compat as wc
from whoosh_compat.ast import And
from whoosh_compat.ast import AndMaybe
from whoosh_compat.ast import AndNot
from whoosh_compat.ast import Every
from whoosh_compat.ast import Node
from whoosh_compat.ast import Not
from whoosh_compat.ast import Nothing
from whoosh_compat.ast import Or
from whoosh_compat.ast import Phrase
from whoosh_compat.ast import Require
from whoosh_compat.ast import Term
from whoosh_compat.ast import analyze
from whoosh_compat.ast import normalize
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken

CONTENT = FieldRef("content")
TAG = FieldRef("tag")
ASN = FieldRef("asn")
CREATED = FieldRef("created")
HAS_TAG = FieldRef("has_tag")
JSON_USER = FieldRef("attrs", "user")


def word_split(text: str) -> list[str]:
    return text.split()


REG = FieldRegistry(
    [
        FieldSpec("content", FieldKind.TEXT, analyzer=word_split),
        FieldSpec("tag", FieldKind.KEYWORD, analyzer=word_split),
        FieldSpec("asn", FieldKind.U64, fast=True),
        FieldSpec("created", FieldKind.DATE, date_only=True),
        FieldSpec("tag_id", FieldKind.U64, fast=True),
        FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
        FieldSpec("attrs", FieldKind.JSON, subpaths=("user",), analyzer=word_split),
    ]
)


def test_analyze_is_exported_publicly() -> None:
    assert wc.analyze is analyze


def test_single_token_term_is_a_structural_no_op() -> None:
    node = Term(field=CONTENT, text="hello")
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="hello")
    assert isinstance(result, Term)
    assert result.analyzed is True


def test_multi_token_term_and_mode() -> None:
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, REG)
    assert result == And(
        children=(Term(field=CONTENT, text="hello"), Term(field=CONTENT, text="world"))
    )


def test_multi_token_term_or_mode_via_field_config() -> None:
    reg = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=word_split, multitoken=Multitoken.OR)]
    )
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, reg)
    assert result == Or(
        children=(Term(field=CONTENT, text="hello"), Term(field=CONTENT, text="world"))
    )


def test_multi_token_term_phrase_mode_via_field_config() -> None:
    reg = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=word_split, multitoken=Multitoken.PHRASE)]
    )
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, reg)
    assert isinstance(result, Phrase)
    assert result.words == ("hello", "world")
    assert result.slop == 1


def test_multi_token_term_first_mode_via_field_config() -> None:
    reg = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=word_split, multitoken=Multitoken.FIRST)]
    )
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, reg)
    assert result == Term(field=CONTENT, text="hello")


def test_zero_token_term_becomes_nothing() -> None:
    node = Term(field=CONTENT, text="")
    result = analyze(node, REG)
    assert result == Nothing()


def test_json_subpath_single_token_term() -> None:
    node = Term(field=JSON_USER, text="alice")
    result = analyze(node, REG)
    assert result == Term(field=JSON_USER, text="alice")


def test_json_subpath_multi_token_term_default_and() -> None:
    node = Term(field=JSON_USER, text="alice bob")
    result = analyze(node, REG)
    assert result == And(
        children=(Term(field=JSON_USER, text="alice"), Term(field=JSON_USER, text="bob"))
    )


def test_json_subpath_multi_token_term_phrase_mode_via_field_config() -> None:
    # A JSON subpath inherits the parent JSON field's own analyzer and
    # Multitoken policy, the same as a plain TEXT/KEYWORD field: this is
    # the JSON-subpath sibling of
    # test_multi_token_term_phrase_mode_via_field_config above, pinning
    # that FieldSpec.multitoken is genuinely consulted here, not silently
    # ignored in favor of always falling back to the enclosing group's
    # combinator (see FieldSpec's docstring).
    reg = FieldRegistry(
        [
            FieldSpec(
                "attrs",
                FieldKind.JSON,
                subpaths=("user",),
                analyzer=word_split,
                multitoken=Multitoken.PHRASE,
            )
        ]
    )
    node = Term(field=JSON_USER, text="alice bob")
    result = analyze(node, reg)
    assert isinstance(result, Phrase)
    assert result.field == JSON_USER
    assert result.words == ("alice", "bob")
    assert result.slop == 1


def test_quoted_phrase_multi_word() -> None:
    node = Phrase(field=CONTENT, text="hello world", slop=1)
    result = analyze(node, REG)
    assert isinstance(result, Phrase)
    assert result.words == ("hello", "world")
    assert result.text == "hello world"
    assert result.slop == 1


def test_quoted_phrase_single_word_stays_a_phrase() -> None:
    # Matches real whoosh's own PhrasePlugin, which always builds a Phrase
    # query object regardless of word count and never self-collapses a
    # one-word phrase to a term at the AST level (only the emitter, a
    # backend execution detail, treats the two as interchangeable queries).
    node = Phrase(field=CONTENT, text="hello", slop=1)
    result = analyze(node, REG)
    assert isinstance(result, Phrase)
    assert result.words == ("hello",)
    assert result.text == "hello"


def test_quoted_phrase_zero_words_drops() -> None:
    node = Phrase(field=CONTENT, text="", slop=1)
    result = analyze(node, REG)
    assert result == Nothing()


# -- kind-restricted fields: never analyzed or dropped ----------------------


def test_u64_term_never_analyzed() -> None:
    node = Term(field=ASN, text="100")
    result = analyze(node, REG)
    assert result == Term(field=ASN, text="100")
    assert result.analyzed is False


def test_date_term_never_analyzed() -> None:
    node = Term(field=CREATED, text="2020-01-01")
    result = analyze(node, REG)
    assert result == node
    assert result.analyzed is False


def test_boolean_exists_term_never_analyzed() -> None:
    node = Term(field=HAS_TAG, text=True)
    result = analyze(node, REG)
    assert result == node


def test_u64_phrase_never_analyzed_even_if_shorter_than_minsize() -> None:
    # A U64 phrase must never be dropped by the analysis pass, even though
    # the field's own analyzer (shared across kinds in this fixture) would
    # tokenize its text to zero survivors; kind-dispatch excludes it before
    # any tokens are ever computed for it.
    reg = FieldRegistry([FieldSpec("asn", FieldKind.U64, analyzer=lambda t: [])])
    node = Phrase(field=ASN, text="100", slop=1)
    result = analyze(node, reg)
    assert result == node


# -- default_mode parameter --------------------------------------------------


def test_default_mode_and_for_bare_top_level_term() -> None:
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, REG, default_mode=Multitoken.AND)
    assert isinstance(result, And)


def test_default_mode_or_for_bare_top_level_term() -> None:
    node = Term(field=CONTENT, text="hello world")
    result = analyze(node, REG, default_mode=Multitoken.OR)
    assert isinstance(result, Or)


def test_default_mode_applies_only_with_no_enclosing_group() -> None:
    # A term nested inside an explicit Or resolves DEFAULT against that Or,
    # not against default_mode, even when default_mode says AND. The
    # resulting Or(hello, world) flattens directly into the enclosing Or
    # (normalize()'s same-type flattening), rather than nesting, so "hello"
    # and "world" show up as direct siblings, not wrapped in a sub-Or.
    node = Or(children=(Term(field=CONTENT, text="hello world"), Term(field=TAG, text="x")))
    result = analyze(node, REG, default_mode=Multitoken.AND)
    assert isinstance(result, Or)
    texts = {(c.field, c.text) for c in result.children if isinstance(c, Term)}  # type: ignore[union-attr]
    assert (CONTENT, "hello") in texts
    assert (CONTENT, "world") in texts


def test_default_mode_transparent_through_not_and_boosted() -> None:
    # NOT/Boosted are not combining groups themselves: a term inside NOT
    # (foo bar) with nothing else enclosing it still resolves against
    # default_mode, not against some group that isn't actually there.
    node = Not(child=Term(field=CONTENT, text="hello world"))
    result = analyze(node, REG, default_mode=Multitoken.OR)
    assert isinstance(result, Not)
    assert isinstance(result.child, Or)


# -- idempotence by construction ---------------------------------------------


def _shingle_analyzer(text: str) -> list[str]:
    """A deliberately non-idempotent-if-done-wrong analyzer: each output
    token is a two-word shingle containing a literal space. A naive
    analyze() that rejoined tokens with a space and relied on a second pass
    re-splitting them would corrupt these (a real token boundary and the
    join separator become indistinguishable).
    """
    words = text.split()
    if len(words) < 2:
        return words
    return [f"{a} {b}" for a, b in pairwise(words)]


def test_analyze_is_idempotent_with_a_shingle_analyzer() -> None:
    reg = FieldRegistry([FieldSpec("content", FieldKind.TEXT, analyzer=_shingle_analyzer)])
    node = Term(field=CONTENT, text="alpha beta gamma delta")
    once = analyze(node, reg)
    twice = analyze(once, reg)
    assert once == twice
    # And by construction, not by luck: re-running analyze() must not call
    # the analyzer at all on already-analyzed leaves, so the shingle tokens
    # ("alpha beta", "beta gamma", "gamma delta") survive completely intact
    # rather than being rejoined and re-split into something else.
    assert isinstance(once, And)
    texts = {c.text for c in once.children if isinstance(c, Term)}  # type: ignore[union-attr]
    assert texts == {"alpha beta", "beta gamma", "gamma delta"}


def test_analyze_is_idempotent_for_a_quoted_phrase_with_shingle_analyzer() -> None:
    reg = FieldRegistry([FieldSpec("content", FieldKind.TEXT, analyzer=_shingle_analyzer)])
    node = Phrase(field=CONTENT, text="alpha beta gamma", slop=1)
    once = analyze(node, reg)
    twice = analyze(once, reg)
    assert once == twice
    assert isinstance(once, Phrase)
    assert once.words == ("alpha beta", "beta gamma")


def test_analyze_is_idempotent_for_zero_token_and_kind_restricted_leaves() -> None:
    node = And(
        children=(
            Term(field=CONTENT, text=""),
            Term(field=ASN, text="100"),
            Phrase(field=CONTENT, text="hello world", slop=1),
        )
    )
    once = analyze(node, REG)
    twice = analyze(once, REG)
    assert once == twice


# -- DIVERGENCES.md entry 23: NOT of a zero-token term -----------------------


def test_not_of_zero_token_term_matches_everything_via_normalize_not_nothing() -> None:
    """DIVERGENCES.md entry 23: analyze() drops a zero-token term's leaf to
    Nothing(), leaving Not(Nothing()); normalize()'s pre-existing
    Not(Nothing) -> Every() rule (unrelated to analysis, it already existed
    for an explicit parse-time Nothing) then takes it from there, reproducing
    "matches everything" as the natural consequence of this pipeline's
    ordering, not as a special case analyze() itself implements.
    """
    node = Not(child=Term(field=CONTENT, text=""))
    result = analyze(node, REG)
    assert result == Every()


def test_andnot_zero_token_positive_leaves_negative_standing_alone() -> None:
    """DIVERGENCES.md entry 23's extension to AndNot/AndMaybe/Require: an
    operand that newly drops to zero tokens during analysis lets its sibling
    stand alone, unlike a genuinely pre-existing Nothing() operand (which
    still poisons per DIVERGENCES.md entry 27's whoosh-matching algebra,
    covered separately in tests/test_normalize.py).
    """
    node = AndNot(positive=Term(field=CONTENT, text=""), negative=Term(field=CONTENT, text="foo"))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="foo")


def test_andnot_zero_token_negative_leaves_positive_standing_alone() -> None:
    node = AndNot(positive=Term(field=CONTENT, text="foo"), negative=Term(field=CONTENT, text=""))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="foo")


def test_andmaybe_zero_token_required_leaves_optional_standing_alone() -> None:
    node = AndMaybe(required=Term(field=CONTENT, text=""), optional=Term(field=CONTENT, text="foo"))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="foo")


def test_require_zero_token_scored_leaves_filter_only_standing_alone() -> None:
    node = Require(scored=Term(field=CONTENT, text=""), filter_only=Term(field=CONTENT, text="foo"))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="foo")


def test_andnot_genuinely_pre_existing_nothing_still_poisons() -> None:
    # A literal, pre-existing Nothing() (not one that appeared only through
    # analysis) still follows the ordinary whoosh-matching poison rule,
    # unaffected by the entry-23 override.
    node = AndNot(positive=Nothing(), negative=Term(field=CONTENT, text="foo"))
    result = analyze(node, REG)
    assert result == Nothing()


# -- And poisons only on a genuinely pre-existing Nothing --------------------


def test_and_drops_a_newly_zero_token_child_without_poisoning() -> None:
    node = And(children=(Term(field=CONTENT, text="invoice"), Term(field=CONTENT, text="")))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="invoice")


def test_and_still_poisons_on_a_genuinely_pre_existing_nothing() -> None:
    node = And(children=(Term(field=CONTENT, text="invoice"), Nothing()))
    result = analyze(node, REG)
    assert result == Nothing()


def test_group_wrapped_pre_existing_nothing_poisons_like_the_literal() -> None:
    # The unnormalized sibling of the two literal-Nothing cases above: a
    # pre-existing Nothing WRAPPED in a group (the shape normalize() would
    # have collapsed) must classify identically, so the docstring's promise
    # that a not-yet-normalized tree still analyzes correctly holds. The
    # group collapses to a fresh Nothing during the analysis pass itself,
    # which must not be mistaken for a newly-dropped zero-token child.
    node = And(children=(Term(field=CONTENT, text="invoice"), And(children=(Nothing(),))))
    assert analyze(node, REG) == Nothing()
    assert analyze(node, REG) == analyze(normalize(node), REG)


def test_andnot_group_wrapped_pre_existing_nothing_poisons_not_inverts() -> None:
    # Same shape on AndNot's positive side: misclassifying the collapsed
    # group as newly-dropped would apply the entry-23 survivor rule and
    # promote the NEGATIVE side to stand alone, matching exactly the
    # documents the query excluded.
    node = AndNot(
        positive=And(children=(Nothing(),)),
        negative=Term(field=CONTENT, text="foo"),
    )
    assert analyze(node, REG) == Nothing()
    assert analyze(node, REG) == analyze(normalize(node), REG)


def test_distinct_phrase_tokenizations_survive_analysis_side_by_side() -> None:
    # An analyzer whose tokens contain spaces (shingle-style) can tokenize
    # two different phrase texts into DIFFERENT word tuples that join to
    # the same string. The two phrases carry distinct positional
    # constraints (the emitter builds phrase_query from words), and real
    # whoosh keeps both in an Or (its Phrase.__eq__ compares word lists);
    # analysis plus normalization must not dedupe one away.
    shingles = FieldRegistry(
        [
            FieldSpec(
                "sh",
                FieldKind.TEXT,
                analyzer=lambda t: [w.replace("-", " ") for w in t.split()],
            )
        ]
    )
    ref = FieldRef("sh")
    tree = Or(
        children=(
            Phrase(field=ref, text="a-b c"),
            Phrase(field=ref, text="a b-c"),
        )
    )
    result = analyze(tree, shingles)
    assert isinstance(result, Or)
    words = sorted(
        p.words for p in result.children if isinstance(p, Phrase) and p.words is not None
    )
    assert words == [("a", "b c"), ("a b", "c")]


def test_aliased_node_context_follows_each_position() -> None:
    # One frozen Term object legitimately reused at two tree positions
    # (value semantics invite object sharing): each occurrence must
    # resolve Multitoken.DEFAULT from its OWN enclosing combinator, not
    # from whichever position a traversal happened to record last. The
    # control tree is structurally identical but with distinct objects.
    shared = Term(field=CONTENT, text="multi word")
    aliased = And(children=(shared, Or(children=(shared, Term(field=CONTENT, text="x")))))
    control = And(
        children=(
            Term(field=CONTENT, text="multi word"),
            Or(children=(Term(field=CONTENT, text="multi word"), Term(field=CONTENT, text="x"))),
        )
    )
    assert analyze(aliased, REG) == analyze(control, REG)


def test_or_drops_a_newly_zero_token_child() -> None:
    node = Or(children=(Term(field=CONTENT, text="invoice"), Term(field=CONTENT, text="")))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="invoice")


# -- DIVERGENCES.md entry 23's match-all face: an unfielded Every survives -
# -- a sibling that only empties out during analysis, the same way a ------
# -- fielded Every already does -------------------------------------------


def test_and_unfielded_every_survives_a_newly_zero_token_sibling() -> None:
    """DIVERGENCES.md entry 23's match-all face. Real whoosh analyzes at
    parse time, so a stopword-shaped term is already ``Nothing``-equivalent
    before its own ``And.normalize()`` ever runs, and that rule drops a
    null child from an enclosing compound rather than annihilating the
    parent (matching ``test_and_drops_a_newly_zero_token_child_without_poisoning``
    above, the same "newly empty, don't poison" rule) -- leaving
    ``Every()`` standing alone. whoosh-compat must agree: the unfielded
    ``Every`` is not a genuinely pre-existing ``Nothing``, so it must not
    poison, and it must not simply vanish either, the way
    ``_normalize_one``'s AND-identity rule does when it runs on a
    not-yet-analyzed sibling.
    """
    node = And(children=(Every(), Term(field=CONTENT, text="")))
    result = analyze(node, REG)
    assert result == Every()


def test_and_fielded_every_already_survived_a_newly_zero_token_sibling() -> None:
    """Control: the fielded case already worked before this fix (a fielded
    ``Every`` is never subject to the unfielded-only AND-identity drop), so
    it must keep working identically afterward.
    """
    node = And(children=(Every(field=TAG), Term(field=CONTENT, text="")))
    result = analyze(node, REG)
    assert result == Every(field=TAG)


def test_and_unfielded_every_still_absorbed_when_a_real_term_survives() -> None:
    """The unfielded-Every-as-AND-identity simplification is still correct,
    and must still fire, when nothing empties out: ``Every() AND invoice``
    is just ``invoice`` (matches real whoosh, which drops the redundant
    match-all through ordinary And flattening for a sibling that was never
    in question). Only a sibling that empties out *during this analysis
    pass* must protect the Every; an ordinary surviving term must not.
    """
    node = And(children=(Every(), Term(field=CONTENT, text="invoice")))
    result = analyze(node, REG)
    assert result == Term(field=CONTENT, text="invoice")


def test_and_unfielded_every_still_poisoned_by_pre_existing_nothing() -> None:
    """A genuinely pre-existing ``Nothing`` (not one that only appeared
    during this analysis pass) must still poison the And even with an
    unfielded Every present, matching ordinary And algebra and
    ``test_and_still_poisons_on_a_genuinely_pre_existing_nothing`` above.
    """
    node = And(children=(Every(), Nothing()))
    result = analyze(node, REG)
    assert result == Nothing()


def test_direct_normalize_keeps_the_every_while_a_sibling_can_still_empty() -> None:
    """Plain, direct ``normalize()`` must keep the unfielded Every when any
    surviving sibling is a fielded leaf analysis has not resolved yet, since
    that leaf may still empty out and leave the Every standing alone. The
    AND-identity drop is only sound once nothing below can still vanish;
    applying it earlier is what made ``analyze()`` disagree with
    ``analyze(normalize(...))``.
    """
    node = And(children=(Every(), Term(field=CONTENT, text="")))
    result = normalize(node)
    assert result == And(children=(Every(), Term(field=CONTENT, text="")))


def test_direct_normalize_drops_the_every_once_siblings_are_analyzed() -> None:
    """The AND-identity drop still fires, unchanged, once no sibling can
    empty out any more: an already-analyzed sibling is settled, so
    ``Every() AND invoice`` canonicalizes to ``invoice`` exactly as before.
    """
    node = And(children=(Every(), Term(field=CONTENT, text="invoice", analyzed=True)))
    result = normalize(node)
    assert result == Term(field=CONTENT, text="invoice", analyzed=True)


def test_direct_normalize_drops_the_every_for_an_unanalyzable_sibling() -> None:
    """An unfielded leaf is never subject to analysis at all (analysis only
    resolves leaves whose field the registry can resolve), so it can never
    empty out and the AND-identity drop stays sound for it, keeping
    ``normalize()``'s behavior for a hand-built unfielded tree unchanged.
    """
    node = And(children=(Every(), Term(field=None, text="invoice")))
    result = normalize(node)
    assert result == Term(field=None, text="invoice")


# -- analyze() is insensitive to a prior direct normalize() ---------------


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(
            And(children=(Every(), Term(field=CONTENT, text=""))),
            id="and-every-with-zero-token-sibling",
        ),
        pytest.param(
            And(
                children=(
                    Every(),
                    Term(field=CONTENT, text=""),
                    Term(field=CONTENT, text="invoice"),
                )
            ),
            id="and-every-with-zero-token-and-surviving-siblings",
        ),
        pytest.param(
            And(children=(And(children=(Every(), Term(field=CONTENT, text=""))),)),
            id="nested-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            Not(child=And(children=(Every(), Term(field=CONTENT, text="")))),
            id="not-of-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            Or(
                children=(
                    And(children=(Every(), Term(field=CONTENT, text=""))),
                    Term(field=CONTENT, text="invoice"),
                )
            ),
            id="or-of-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            AndNot(
                positive=And(children=(Every(), Term(field=CONTENT, text=""))),
                negative=Term(field=CONTENT, text="invoice"),
            ),
            id="andnot-positive-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            AndMaybe(
                required=And(children=(Every(), Term(field=CONTENT, text=""))),
                optional=Term(field=CONTENT, text="invoice"),
            ),
            id="andmaybe-required-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            Require(
                scored=And(children=(Every(), Term(field=CONTENT, text=""))),
                filter_only=Term(field=CONTENT, text="invoice"),
            ),
            id="require-scored-and-every-with-zero-token-sibling",
        ),
        pytest.param(
            Or(children=(Every(), Term(field=CONTENT, text=""))),
            id="or-every-with-zero-token-sibling",
        ),
        pytest.param(
            And(children=(Every(), Phrase(field=CONTENT, text="", words=()))),
            id="and-every-with-zero-token-phrase-sibling",
        ),
        pytest.param(
            And(children=(Every(field=TAG), Term(field=CONTENT, text=""))),
            id="and-fielded-every-with-zero-token-sibling",
        ),
        pytest.param(
            And(children=(Every(), Nothing())),
            id="and-every-with-pre-existing-nothing",
        ),
    ],
)
def test_analyze_is_insensitive_to_a_prior_direct_normalize(node: Node) -> None:
    """``analyze()``'s contract: a not-yet-normalized tree analyzes to the
    same result as its already-normalized form. A caller that runs plain
    ``normalize()`` itself before calling ``analyze()`` must not get a
    different answer from one that hands ``analyze()`` the raw tree, which
    requires ``normalize()`` to never discard something analysis still
    needs (here, an unfielded ``Every`` whose sibling turns out to be
    zero-token).
    """
    assert analyze(node, REG) == analyze(normalize(node), REG)


# -- spans --------------------------------------------------------------


def test_analyzed_multi_token_term_carries_the_original_span() -> None:
    node = Term(field=CONTENT, text="hello world", startchar=5, endchar=16)
    result = analyze(node, REG)
    assert result.startchar == 5
    assert result.endchar == 16
    assert isinstance(result, And)
    for child in result.children:
        assert child.startchar == 5
        assert child.endchar == 16


def test_normalize_applied_by_analyze_is_a_no_op_on_an_already_analyzed_tree() -> None:
    node = Term(field=CONTENT, text="hello world")
    once = analyze(node, REG)
    assert normalize(once) == once
