"""A pattern matches the typed run OR its stem.

Index terms are stemmed; wildcard and prefix patterns are not. Normalizing a
pattern to a *single* string therefore cannot be right, because English
Snowball's y->i step substitutes rather than truncates: "company" -> "compani"
(so the run as typed misses the index term) while "copyright" is its own stem
(so a stemmed run misses it). The two words are the same morphological class,
so no length heuristic separates them; measured over a 4,977-word vocabulary,
177 words (3.5%) stem to something that is not a prefix of themselves.

``FieldSpec.pattern_normalizer`` therefore returns *alternatives*
(``whoosh_compat.PatternNormalizer``) and the emitter ORs them into one
regex. This file pins that end to end against a genuinely stemmed index
(``stemmed_index``/``stemmed_reg`` in ``conftest.py``), plus the contract's
edges: deduplication, the bare-``str`` form, "no alternatives at all", and the
bracket-class length invariant.
"""

import pytest
import tantivy

from whoosh_compat import parse as wc_parse
from whoosh_compat.emitters.tantivy_ import glob_to_regex
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

from .conftest import STEM_COMPANIES
from .conftest import STEM_COMPANY
from .conftest import STEM_COPIES
from .conftest import STEM_COPYRIGHT
from .conftest import STEM_DOCS
from .conftest import TIndex
from .conftest import emit_
from .conftest import english_stem
from .conftest import lower_fold
from .conftest import search_ids
from .conftest import stem_fold


def ids(index: TIndex, registry: FieldRegistry, query: str) -> list[int]:
    """Parse ``query``, emit it, and search: the whole pipeline, by doc id."""
    result = wc_parse(query, registry=registry, default_fields=["content"])
    assert result.diagnostics == ()
    return search_ids(index[0], emit_(result.ast, index=index[0], registry=registry))


# -- The fixture really is stemmed -------------------------------------------


def test_the_stemmed_fixture_is_actually_stemmed(stemmed_index: TIndex) -> None:
    """Guard on the fixture itself, without which every assertion below could
    pass for the wrong reason (``tindex``'s 'default' tokenizer does not stem,
    and against an unstemmed index both patterns already work).

    Also pins whoosh's Python Snowball against the terms tantivy's Rust
    Snowball actually produced, since the registry's normalizer uses the
    former to predict the latter.
    """
    index, schema = stemmed_index
    hits = search_ids(index, tantivy.Query.term_query(schema, "content", "compani"))
    assert hits == [STEM_COMPANY, STEM_COMPANIES], "y->i stemming did not happen"
    assert search_ids(index, tantivy.Query.term_query(schema, "content", "company")) == []
    assert search_ids(index, tantivy.Query.term_query(schema, "content", "copyright")) == [
        STEM_COPYRIGHT
    ]
    for doc_id, word in STEM_DOCS.items():
        hit = search_ids(index, tantivy.Query.term_query(schema, "content", english_stem(word)))
        assert doc_id in hit, f"whoosh stems {word!r} to a term tantivy did not index"


# -- The behaviour the alternatives contract exists for ----------------------


def test_a_pattern_matches_the_typed_run_or_its_stem(
    stemmed_index: TIndex, stemmed_reg: FieldRegistry
) -> None:
    """Both directions, in one test: a fix that trades one for the other fails.

    A single-string normalizer can satisfy either assertion but never both:
    stemming loses "copyright", not stemming loses "companies".
    """
    assert ids(stemmed_index, stemmed_reg, "company*") == [STEM_COMPANY, STEM_COMPANIES]
    assert ids(stemmed_index, stemmed_reg, "copy*") == [STEM_COPYRIGHT, STEM_COPIES]


def test_no_single_normalized_string_reaches_both(stemmed_index: TIndex) -> None:
    """The measurement the alternatives contract replaces, kept executable.

    Under the *old* single-string contract, either choice loses one of the
    two documents the other finds, and the words that force the choice are
    the same morphological class, so no rule over one string separates them.
    This is what "both tests fail today, for different reasons" looks like
    with the reasons pinned side by side.
    """
    typed_only = FieldRegistry(
        [FieldSpec("content", FieldKind.TEXT, analyzer=stem_fold, pattern_normalizer=str.lower)]
    )
    stem_only = FieldRegistry(
        [
            FieldSpec(
                "content",
                FieldKind.TEXT,
                analyzer=stem_fold,
                pattern_normalizer=lambda t: english_stem(t.lower()),
            )
        ]
    )
    # The typed run alone never reaches the stemmed term.
    assert ids(stemmed_index, typed_only, "company*") == []
    assert ids(stemmed_index, typed_only, "copy*") == [STEM_COPYRIGHT]
    # The stem alone reaches it, and loses the compound that kept the
    # literal spelling.
    assert ids(stemmed_index, stem_only, "company*") == [STEM_COMPANY, STEM_COMPANIES]
    assert ids(stemmed_index, stem_only, "copy*") == [STEM_COPIES]


def test_alternates_apply_to_wildcards_not_just_prefixes(
    stemmed_index: TIndex, stemmed_reg: FieldRegistry
) -> None:
    """``visit_wildcard`` goes through ``glob_to_regex``, a different path from
    ``visit_prefix``'s, and needs the widening just as much. Both directions
    again: the run either side of the "?" is normalized independently, so one
    pattern needs the stem and the other needs the literal spelling.
    """
    assert ids(stemmed_index, stemmed_reg, "c?mpany*") == [STEM_COMPANY, STEM_COMPANIES]
    assert ids(stemmed_index, stemmed_reg, "cop?right*") == [STEM_COPYRIGHT]


def test_a_term_that_matches_neither_form_still_misses(
    stemmed_index: TIndex, stemmed_reg: FieldRegistry
) -> None:
    """The widening is a union of two languages, not "match anything"."""
    assert ids(stemmed_index, stemmed_reg, "coma*") == []


# -- The emitted regex -------------------------------------------------------


def test_equal_alternatives_emit_one_branch() -> None:
    """Deduplication is correctness, not tidiness: the equal case is the
    common one (every non-stemming field, and every word the stemmer leaves
    alone), and it must produce the same single regex it always did, not
    ``x|x``.
    """
    assert glob_to_regex("inv*", lambda t: (t.lower(), t.lower())) == "inv.*"
    assert glob_to_regex("inv*", str.lower) == "inv.*"
    assert glob_to_regex("inv*", None) == "inv.*"


def test_distinct_alternatives_become_one_alternation_group() -> None:
    assert glob_to_regex("copy*", lambda t: (t, english_stem(t))) == "(?:copy|copi).*"


def test_each_run_alternates_separately() -> None:
    """Per run, not per pattern: a pattern with several literal runs costs the
    sum of their alternatives, not the product.
    """
    assert (
        glob_to_regex("copy*copies", lambda t: (t, english_stem(t)))
        == "(?:copy|copi).*(?:copies|copi)"
    )


def test_alternatives_are_regex_escaped() -> None:
    """Each branch is escaped individually, so a normalizer cannot inject
    regex syntax through an alternative any more than through a single form.
    """
    assert glob_to_regex("a*", lambda t: (t, t + ".+")) == "(?:a|a\\.\\+).*"


# -- No alternatives at all --------------------------------------------------


def test_no_alternatives_means_the_pattern_matches_nothing() -> None:
    """An empty sequence is a real answer: the normalizer says no term can
    match this fragment, so the whole concatenation can match nothing. That is
    the same out-of-band ``None`` an empty bracket class already returns.
    """
    assert glob_to_regex("inv*", lambda _t: ()) is None
    # Only the run is emptied, but the run is part of the concatenation.
    assert glob_to_regex("a*b", lambda t: () if t == "b" else (t,)) is None


def test_an_empty_string_alternative_is_not_the_same_as_no_alternatives() -> None:
    """One empty form is still one form: it says the run normalizes away, not
    that nothing can match. Unchanged from the single-string contract, where
    a normalizer returning "" did exactly this.
    """
    assert glob_to_regex("inv*", lambda _t: "") == ".*"
    assert glob_to_regex("inv*", lambda _t: ("",)) == ".*"


def test_a_prefix_with_no_alternatives_emits_an_empty_query(
    stemmed_index: TIndex,
) -> None:
    """``visit_prefix`` does not go through ``glob_to_regex``, so it needs its
    own answer for the same fact.
    """
    registry = FieldRegistry(
        [
            FieldSpec(
                "content",
                FieldKind.TEXT,
                analyzer=lower_fold,
                pattern_normalizer=lambda _t: (),
            )
        ]
    )
    assert ids(stemmed_index, registry, "company*") == []


# -- The bracket-class length invariant --------------------------------------


def test_a_class_character_with_several_alternatives_stays_as_typed() -> None:
    """A class position matches exactly one character, so an alternation is
    not expressible there, and adding members would change the body's length,
    which every fnmatch offset in ``_translate_class`` is taken against. The
    character is left as the user typed it, the same answer a multi-character
    fold already gets.
    """
    assert glob_to_regex("[ab]x", lambda t: (t.lower(), t.upper())) == "[ab](?:x|X)"


def test_a_class_character_with_no_alternatives_stays_as_typed() -> None:
    """Dropping the member would change the length too; on the literal path
    the same answer aborts the whole pattern.
    """
    assert glob_to_regex("[ab]", lambda _t: ()) == "[ab]"


def test_a_single_single_character_alternative_still_folds_the_class() -> None:
    """The case that cannot corrupt anything still applies, so
    ``title:BILL[I]NG*`` keeps folding to ``bill[i]ng.*``.
    """
    assert glob_to_regex("BILL[I]NG*", lambda t: (t.lower(),)) == "bill[i]ng.*"


@pytest.mark.parametrize(
    "pattern",
    ["[a-z]x", "[!a-z]x", "[]a]x", "[a\\b]x", "x[0-3]y"],
    ids=["range", "negated", "leading-close", "backslash", "digits"],
)
def test_a_stemming_normalizer_never_corrupts_a_class(pattern: str) -> None:
    """The realistic host normalizer (fold + stem, as alternatives) run over
    class-bearing patterns: the stemmer is a no-op on single characters, so
    every class comes out exactly as the plain single-form fold leaves it.
    """
    folded = glob_to_regex(pattern, str.lower)
    assert glob_to_regex(pattern, lambda t: (t.lower(), english_stem(t.lower()))) == folded


def test_an_expanding_fold_is_screened_per_alternative() -> None:
    """``_CLASS_FOLD_LENGTH_MSG``'s invariant is unchanged by the alternatives
    seam: one output character per input character, enforced by construction,
    with the screening now applied to the alternative rather than to the one
    returned string. A fold that expands ("ß" -> "ss") is therefore skipped
    for that character either way, and wrapping it in a one-element tuple
    changes nothing.
    """

    def expanding_str(text: str) -> str:
        return text.replace("ß", "ss")

    def expanding_alt(text: str) -> tuple[str, ...]:
        return (expanding_str(text),)

    # The member is left as typed, so the class keeps its length and meaning.
    assert glob_to_regex("[aß]x", expanding_alt) == "[aß]x"
    # And the reversed range that leaves behind still collapses to
    # "matches nothing" (U+00DF > "z"), the same as under one string.
    assert glob_to_regex("[ß-z]x", expanding_alt) is None
    for pattern in ("[aß]x", "[ß-z]x", "ßx*", "[!ß]x"):
        assert glob_to_regex(pattern, expanding_alt) == glob_to_regex(pattern, expanding_str)
