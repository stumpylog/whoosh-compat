"""Bidirectional cross-reference check between ``allowlist.py`` and
``DIVERGENCES.md``, the mechanical half of the divergence-paperwork
convention (CLAUDE.md's "a deliberate divergence lands with its paperwork"
rule, and this repository's ``differential-triage`` skill): a divergence
introduced without both pieces should fail the suite instead of silently
drifting out of sync.

Two directions, both required:

* Every ``allowlist.py`` entry's reason string must cite an existing,
  numbered ``DIVERGENCES.md`` entry (``DIVERGENCES.md entry N``, for some
  ``N`` that is actually a numbered entry in the file).
* Every ``DIVERGENCES.md`` entry whose own text claims a matching
  ``allowlist.py`` entry (mentions ``allowlist.py`` at all) must have one:
  some ``ALLOW`` entry actually cites that entry's number back. And every
  ``DIVERGENCES.md`` entry that names a specific corpus query string
  (`` `tests/differential/corpus_X.txt`'s `query` line `` or a `` / ``
  -separated list of such lines) must have that exact line present in the
  named corpus file.

The corpus-line check is deliberately narrow: only the common,
mechanically-recognizable phrasing (a corpus filename immediately followed
by ``'s`` and one or more backtick-quoted literals, ending in "line"/
"lines") is checked, not every prose mention of a corpus file. A prose
sentence like "no line in corpus_paperless.txt uses ANDNOT" is a *negative*
claim (no matching literal to check) and is intentionally not parsed here;
inventing a way to verify negative claims mechanically is not worth the
fragility it would add for what is otherwise a documentation cross-check.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from whoosh.analysis import STOP_WORDS

from tests.differential.allowlist import ALLOW
from tests.differential.allowlist import allowed_entry
from tests.differential.allowlist import allowed_reason
from tests.differential.oracle import ORACLE_REGISTRY
from whoosh_compat.fields import FieldKind

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DIVERGENCES_PATH = _ROOT / "DIVERGENCES.md"
_DIVERGENCES_TEXT = _DIVERGENCES_PATH.read_text(encoding="utf-8")

_CORPUS_DIR = pathlib.Path(__file__).parent
_CORPUS_FILES = ("corpus_paperless.txt", "corpus_docs.txt", "corpus_realworld.txt")
_CORPUS_LINES: dict[str, set[str]] = {
    name: {
        line
        for line in (_CORPUS_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    for name in _CORPUS_FILES
}

# Every numbered top-level entry in DIVERGENCES.md ("1. ...", "12. **...**",
# ...), whether or not it uses the bold-title style entries 12+ adopted.
_ENTRY_START_RE = re.compile(r"^(\d+)\.\s", re.MULTILINE)


def _divergence_entry_numbers() -> set[int]:
    return {int(m.group(1)) for m in _ENTRY_START_RE.finditer(_DIVERGENCES_TEXT)}


def _divergence_entry_bodies() -> dict[int, str]:
    """Map entry number -> its full text, from its own numbered header up to
    (but not including) the next numbered header or end of file.
    """

    starts = list(_ENTRY_START_RE.finditer(_DIVERGENCES_TEXT))
    bodies: dict[int, str] = {}
    for i, m in enumerate(starts):
        number = int(m.group(1))
        end = starts[i + 1].start() if i + 1 < len(starts) else len(_DIVERGENCES_TEXT)
        bodies[number] = _DIVERGENCES_TEXT[m.start() : end]
    return bodies


_CITATION_RE = re.compile(r"DIVERGENCES\.md entry (\d+)")


def test_every_allowlist_entry_cites_an_existing_divergences_entry() -> None:
    known = _divergence_entry_numbers()
    problems = []
    for pattern, reason, _kind in ALLOW:
        cited = [int(n) for n in _CITATION_RE.findall(reason)]
        if not cited:
            problems.append(
                f"{pattern.pattern!r}: reason cites no 'DIVERGENCES.md entry N' at all: {reason!r}"
            )
            continue
        for n in cited:
            if n not in known:
                problems.append(
                    f"{pattern.pattern!r}: cites DIVERGENCES.md entry {n}, which does not exist"
                )
    assert not problems, (
        "allowlist.py entries with missing/invalid DIVERGENCES.md citations:\n"
        + "\n".join(problems)
    )


def test_every_divergences_entry_claiming_an_allowlist_entry_has_one() -> None:
    cited_numbers = {
        n
        for _pattern, reason, _kind in ALLOW
        for n in (int(x) for x in _CITATION_RE.findall(reason))
    }
    problems = []
    for number, body in _divergence_entry_bodies().items():
        # "tests/differential/allowlist.py" specifically, not the bare
        # substring "allowlist.py": tests/emitter/result_allowlist.py (the
        # result-level acceptance property's own allowlist, a sibling
        # module with no connection to this AST-level one) also contains
        # that substring, and a DIVERGENCES.md entry legitimately citing
        # only the result-level module's test references must not be
        # mistaken for a claim about this module.
        if "tests/differential/allowlist.py" not in body:
            continue
        if number not in cited_numbers:
            problems.append(
                f"DIVERGENCES.md entry {number} claims a matching tests/differential/allowlist.py"
                " entry, but no ALLOW entry's reason cites 'DIVERGENCES.md entry"
                f" {number}'"
            )
    assert not problems, "\n".join(problems)


# A corpus filename immediately followed by "'s" (optionally split across a
# line break) and one or more backtick-quoted literals, ending at the word
# "line"/"lines". Requires the first backtick to follow directly (only
# whitespace in between), so prose like "corpus_docs.txt's quoted-star
# section" (no literal immediately named) does not spuriously match.
_CORPUS_CLAIM_RE = re.compile(
    r"`tests/differential/(corpus_\w+\.txt)`'s\s+"
    r"((?:`[^`]+`(?:\s*(?:/|,|and)\s*)?)+)"
    r"\s*lines?\b"
)
_LITERAL_RE = re.compile(r"`([^`]+)`")


def test_every_divergences_corpus_claim_has_a_matching_corpus_line() -> None:
    problems = []
    for m in _CORPUS_CLAIM_RE.finditer(_DIVERGENCES_TEXT):
        corpus_name = m.group(1)
        literals_blob = m.group(2)
        if corpus_name not in _CORPUS_LINES:
            problems.append(f"unknown corpus file cited: {corpus_name!r}")
            continue
        for literal in _LITERAL_RE.findall(literals_blob):
            if literal not in _CORPUS_LINES[corpus_name]:
                problems.append(
                    f"DIVERGENCES.md claims {corpus_name!r} has a line {literal!r}, but no such"
                    " exact line exists in that corpus file"
                )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "field",
    [
        pytest.param(s.name, id=s.name)
        for s in ORACLE_REGISTRY
        if s.kind in (FieldKind.TEXT, FieldKind.KEYWORD)
    ],
)
def test_entry15_fielded_regex_covers_every_analyzer_splitting_field(field: str) -> None:
    """The entry-15 'correctly-fielded inside an OR' allowlist entry must
    recognize EVERY registered TEXT/KEYWORD field, since those are exactly
    the kinds whose analyzer can split a value into multiple tokens, and
    the grammar fuzzer draws its field vocabulary from this same registry
    (strategies.py's TEXT_FIELDS/KEYWORD_FIELDS): a field the regex omits
    is a latent CI flake the moment hypothesis generates a comma or dashed
    value on it inside an OR. Deriving the parametrization from the live
    registry means a newly registered field fails here instead of
    silently shrinking coverage.
    """
    reason = allowed_reason(f"asn:1 OR {field}:'ab,cd'")
    assert reason is not None, f"entry-15 fielded regex does not cover {field!r}"
    assert "entry 15" in reason, f"{field!r} matched a different entry: {reason!r}"


_STOPWORD_PARAMS = [pytest.param(word, id=word) for word in sorted(STOP_WORDS)]


@pytest.mark.parametrize("word", _STOPWORD_PARAMS)
def test_entry23_regex_covers_every_whoosh_stopword(word: str) -> None:
    """The entry-23 zero-token-word alternation must cover whoosh's whole
    STOP_WORDS set, both cases: StandardAnalyzer lowercases before the
    stop filter, so an uppercase spelling is zero-token too, and the
    fuzzers' word alphabets include uppercase letters. Derived from
    whoosh's own list so it cannot drift.
    """
    for spelling in (word, word.upper()):
        reason = allowed_reason(f"NOT (title:{spelling})")
        assert reason is not None, f"entry-23 regex does not cover {spelling!r}"
        assert "entry 23" in reason, f"{spelling!r} matched a different entry: {reason!r}"


@pytest.mark.parametrize("word", _STOPWORD_PARAMS)
def test_entry24_regex_covers_every_whoosh_stopword(word: str) -> None:
    # Both the single-word and multi-word phrase cells, both cases.
    for spelling in (word, word.upper()):
        for phrase in (f'title:"{spelling}"', f'title:"{spelling} {spelling}"'):
            reason = allowed_reason(phrase)
            assert reason is not None, f"entry-24 regex does not cover {phrase!r}"
            assert "entry 24" in reason, f"{phrase!r} matched a different entry: {reason!r}"


@pytest.mark.parametrize("word", _STOPWORD_PARAMS)
def test_result_entry23_regex_covers_every_whoosh_stopword(word: str) -> None:
    # The result-level sibling (tests/emitter/result_allowlist.py) shares
    # the derived ZERO_TOKEN_WORD fragment; this pins that the sharing
    # actually holds at its own entry point, word by word, both cases.
    from tests.emitter.result_allowlist import allowed_result_reason

    for spelling in (word, word.upper()):
        reason = allowed_result_reason(f"NOT title:{spelling}")
        assert reason is not None, f"result entry-23 regex does not cover {spelling!r}"
        assert "entry 23" in reason, f"{spelling!r} matched a different entry: {reason!r}"


@pytest.mark.parametrize(
    ("query", "expected_entry"),
    [
        # entry 43 (range-bound case folding): claimed spellings need a
        # real separator token (case-insensitive to, whitespace- or
        # bracket-delimited) and an uppercase belonging to a bound.
        pytest.param("title:[A* TO B]", 43, id="e43-uppercase-left-bound"),
        pytest.param("title:[a TO bTO]", 43, id="e43-TO-shaped-right-bound"),
        pytest.param("title:[aTO TO b]", 43, id="e43-TO-shaped-left-bound"),
        pytest.param("title:[ABC]", None, id="e43-non-range-bracket-unclaimed"),
        pytest.param("added:[-1 WEEK TO NOW]", 12, id="e43-date-range-stays-entry-12"),
        pytest.param("asn:[2000 TO 2024]", None, id="e43-separator-only-uppercase-unclaimed"),
        pytest.param("title:[abc TO def]", None, id="e43-lowercase-range-unclaimed"),
        # whoosh's separator is case-insensitive (measured), so lowercase
        # 'to' spellings with an uppercase bound diverge identically.
        pytest.param("title:[a to B]", 43, id="e43-lowercase-separator-uppercase-bound"),
        pytest.param("title:[a To b]", None, id="e43-mixed-separator-lowercase-bounds"),
        # Open-ended spellings put the separator against a bracket.
        pytest.param("title:[A TO]", 43, id="e43-open-upper-bound"),
        pytest.param("title:[to B]", 43, id="e43-open-lower-bound"),
        pytest.param("title:[Xy to]", 43, id="e43-open-upper-lowercase-separator"),
        # entry 2 (pattern case folding): quote-abutting fielded patterns
        # are claimed; quoted-phrase content is not.
        pytest.param("title:Foo*'x'", 2, id="e2-pattern-abutting-quote"),
        pytest.param('title:"Foo Bar?"', None, id="e2-quoted-phrase-content-unclaimed"),
        pytest.param("title:A*", 2, id="e2-plain-fielded-pattern"),
        # entry 33 (BOOLEAN_EXISTS padding): whitespace-only counts, the
        # literally empty value does not.
        pytest.param("has_tag:'  '", 33, id="e33-whitespace-only"),
        pytest.param("has_tag:'  false'", 33, id="e33-padded"),
        pytest.param("has_tag:''", None, id="e33-empty-agrees-unclaimed"),
        # entry 23 (zero-token NOT): a dashed value merely starting with a
        # stopword is a real term, not a zero-token one.
        pytest.param("NOT (title:the-invoice)", None, id="e23-stopword-prefix-unclaimed"),
        pytest.param("NOT (title:the)", 23, id="e23-stopword"),
        # Boundary characters that are NOT token continuations must stay
        # claimed (both spellings measured diverging: oracle Nothing vs
        # compat Every).
        pytest.param("NOT title:the^2", 23, id="e23-boosted-stopword"),
        pytest.param("NOT title:the'x'", 23, id="e23-stopword-abutting-quote"),
        pytest.param("NOT title:the.invoice", None, id="e23-dotted-continuation-unclaimed"),
        # A chain of zero-token pieces joined by analyzer-split characters
        # is zero-token too (measured diverging); one surviving piece
        # rescues the chain.
        pytest.param("NOT (title:a-b)", 23, id="e23-zero-token-chain"),
        pytest.param("NOT title:the-of", 23, id="e23-stopword-chain"),
        pytest.param("NOT title:the-invoice", None, id="e23-rescued-chain-unclaimed"),
        # Empty-group and nested-NOT noise between the NOT and the value
        # (deep-fuzz-found spellings) is tolerated; a real intervening
        # term still blocks the claim.
        pytest.param("NOT (() title:the)", 23, id="e23-empty-group-noise"),
        pytest.param("0 NOT (NOT (() title:the))", 23, id="e23-nested-not-noise"),
        pytest.param("NOT (a) title:the", None, id="e23-real-term-blocks"),
        # The co-occurrence family entry: NOT + empty group + zero-token
        # word in arbitrary scaffolding; the agreeing single-level
        # corpus line "NOT ()" must stay unclaimed (the NOT keyword
        # itself does not count as the zero-token word).
        pytest.param("((0) OR (NOT ((()) AND (title:the))))", 23, id="e23-40-family"),
        pytest.param("NOT ()", None, id="e23-40-single-level-unclaimed"),
        pytest.param("title:foo AND ()", None, id="e23-40-no-not-unclaimed"),
        pytest.param("NOT title:the-", 23, id="e23-trailing-separator"),
        # entries 20/13: a trailing boost must not defeat the anchors.
        pytest.param("tag:*^2", 20, id="e20-boosted-bare-star"),
        pytest.param("title:202[0-3]*^2", 13, id="e13-boosted-bracket-fold"),
        # entry 42: fielded spelling only (the whitespace-colon spelling
        # demotes identically on both sides and compares equal).
        pytest.param("is_shared:true", 42, id="e42-fielded"),
        pytest.param("is_shared :x", None, id="e42-nonfielded-unclaimed"),
        # entry 15 demotion pathways: İ is the one single-character value
        # that survives minsize (its lowercase is two codepoints), found
        # by a deep fuzz soak through both the unknown-field and bare-JSON
        # spellings.
        pytest.param("zzz:İ", 15, id="e15-unknown-field-expanding-char"),
        pytest.param("attrs:İ", 15, id="e15-bare-json-expanding-char"),
        pytest.param("İ-ab", 15, id="e15-bare-dashed-expanding-char"),
        pytest.param("zzz:x", None, id="e15-single-char-value-unclaimed"),
        # The pre-release staleness-sweep narrowings. Each claimed
        # spelling was measured diverging and each unclaimed one measured
        # EQUAL (or, where noted, belongs to a different entry).
        # entry 3: only the natural-date keywords the v2 pipeline rewrites
        # away carry the divergence; a general date boost does not.
        pytest.param("added:yesterday^2", 3, id="e3-rewritten-keyword-boost"),
        pytest.param("created:2020^2", None, id="e3-single-value-date-boost-unclaimed"),
        pytest.param("modified:now^2", None, id="e3-now-boost-unclaimed"),
        pytest.param("created:[2020 TO 2021]^2", 12, id="e3-range-boost-is-the-tz-entry"),
        # entries 12/44: a bound-less range has nothing to convert and no
        # bound for the exclusivity to apply to.
        pytest.param("added:[TO}", None, id="e12-44-boundless-range-unclaimed"),
        pytest.param("created:[TO]", None, id="e12-boundless-range-unclaimed"),
        pytest.param("added:[dec to feb]", 12, id="e12-month-name-bounds-claimed"),
        pytest.param("added:{now TO now}", 44, id="e44-exclusive-exact-bounds"),
        # entries 18/21: the space separator belongs to entry 21's
        # month:day reading, not to entry 18's separated-ISO shape.
        pytest.param("created:2020-01-01", 18, id="e18-dashed-iso"),
        pytest.param("added:'2020 5pm'", None, id="e18-space-then-time-unclaimed"),
        pytest.param("added:'2020 12:30'", 21, id="e21-month-day-pair"),
        # entries 23/24: the zero-token proxy only applies to TEXT fields.
        pytest.param("NOT (id:0)", None, id="e23-numeric-field-unclaimed"),
        pytest.param("NOT has_tag:a", None, id="e23-boolean-field-unclaimed"),
        pytest.param('title:"the"', 24, id="e24-text-field-phrase"),
        pytest.param('tag_id:"in by x"', None, id="e24-keyword-field-unclaimed"),
        # entry 27: the operator alone is not the divergence.
        pytest.param("(title:foo) ANDNOT (title:bar)", None, id="e27-plain-andnot-unclaimed"),
        pytest.param("title:foo (() ANDNOT title:bar)", 27, id="e27-empty-group-operand"),
        pytest.param(
            "title:foo ((title:the) ANDNOT title:bar)",
            27,
            id="e27-parenthesized-zero-token-operand",
        ),
        # entry 33: only a padded value that strips to something false-ish.
        pytest.param("has_type:'  true'", None, id="e33-padded-true-unclaimed"),
        pytest.param("has_type:'  xyz  '", None, id="e33-padded-other-unclaimed"),
        # entry 39: a double-quoted numeric is a phrase on both sides.
        pytest.param('id:"2147483648"', None, id="e39-double-quoted-unclaimed"),
        pytest.param("id:'2147483648'", 39, id="e39-single-quoted-claimed"),
        # entry 15: identical pieces are one token; a dot never splits.
        pytest.param("ab-cd", 15, id="e15-distinct-bare-pieces"),
        pytest.param("ab-ab", None, id="e15-identical-bare-pieces-unclaimed"),
        pytest.param("AB-ab", None, id="e15-case-identical-pieces-unclaimed"),
        pytest.param("ab.cd", None, id="e15-dot-never-splits-unclaimed"),
        pytest.param("ab-cd-ab", 15, id="e15-mixed-chain-claimed"),
        pytest.param("title:ab-cd OR title:x", 15, id="e15-text-dash-in-or"),
        pytest.param("tag:ab,cd OR tag:x", None, id="e15-unquoted-keyword-comma-unclaimed"),
        pytest.param("tag_id:'ab,cd' OR tag_id:x", 15, id="e15-quoted-keyword-comma-in-or"),
        pytest.param("title:ab-ab OR title:x", None, id="e15-identical-in-or-unclaimed"),
        # entry 20 / entry 23's match-all face. The standalone "*:*" token
        # agrees on both sides and is carved out of entry 20. Conjoined
        # with a zero-token TEXT term, this used to be entry 23's
        # match-all divergence; now fixed (see entry 23's own text), so
        # these now compare EQUAL and are unclaimed too. A fielded
        # match-all was never the And identity and stays with entry 20.
        pytest.param("*:*", None, id="e20-match-all-token-unclaimed"),
        pytest.param("*:*^2", None, id="e20-boosted-match-all-unclaimed"),
        pytest.param("*:* title:foo", None, id="e20-match-all-live-sibling-unclaimed"),
        pytest.param("**:*", 20, id="e20-star-named-field-still-claimed"),
        pytest.param("title:*", 20, id="e20-fielded-star-still-claimed"),
        pytest.param("*", 20, id="e20-bare-star-still-claimed"),
        pytest.param("*:* title:the", None, id="e23-match-all-face-fixed"),
        pytest.param("title:the *:*", None, id="e23-match-all-face-reversed-fixed"),
        pytest.param("*:* AND title:the", None, id="e23-match-all-face-explicit-and-fixed"),
        pytest.param("has_tag:* title:the", 20, id="e23-fielded-match-all-not-this-face"),
    ],
)
def test_allowlist_regex_scoping(query: str, expected_entry: int | None) -> None:
    """Pins the overmatch/undermatch boundary of the precision-sensitive
    allowlist regexes: each claimed spelling was verified by execution to
    genuinely diverge, to belong to the cited entry's mechanism, or (for
    a few oracle-unmappable spellings, e.g. the empty-group-noise row,
    whose oracle tree to_ast cannot represent) to be skipped identically
    with or without the claim; each unclaimed spelling compares equal or
    belongs elsewhere. A regex edit that widens or narrows past these
    boundaries fails here instead of surfacing as a spurious strict-xfail
    or a silently absorbed regression.
    """
    reason = allowed_reason(query)
    if expected_entry is None:
        assert reason is None or "entry" not in reason, (
            f"{query!r} unexpectedly claimed: {reason!r}"
        )
    else:
        assert reason is not None, f"{query!r} is not claimed by any entry"
        m = re.search(r"entry (\d+)", reason)
        assert m is not None, f"{query!r} reason has no entry citation: {reason!r}"
        assert int(m.group(1)) == expected_entry, (
            f"{query!r} claimed by the wrong entry: {reason!r}"
        )


@pytest.mark.parametrize(
    ("value", "claimed"),
    [
        # Entry 21 diverges exactly when the colon pair can be read as a
        # calendar month and a valid day of that month; measured cell by
        # cell over every HH:MM pair. These spellings cannot be pinned
        # through allowed_reason(), because entry 15's unknown-field
        # alternative independently claims any "<2+ chars>:<2+ chars>" run
        # (it reads "23:59" as an unknown field), so the pattern itself is
        # asserted instead.
        pytest.param("12:30", True, id="december-30"),
        pytest.param("02:29", True, id="february-29-leap-overclaim"),
        pytest.param("11:30", True, id="november-30"),
        pytest.param("23:59", False, id="no-month-23"),
        pytest.param("00:00", False, id="no-month-0"),
        pytest.param("12:00", False, id="no-day-0"),
        pytest.param("04:31", False, id="april-has-30-days"),
        pytest.param("02:30", False, id="february-has-at-most-29"),
        pytest.param("12:60", False, id="no-day-60"),
    ],
)
def test_entry21_claims_exactly_the_readable_month_day_pairs(value: str, claimed: bool) -> None:
    pattern = next(p for p, reason, _kind in ALLOW if "DIVERGENCES.md entry 21" in reason)
    query = f"added:'2020 {value}'"
    assert bool(pattern.search(query)) is claimed, query


def test_the_lowercase_expanding_character_set_is_exactly_i_dot() -> None:
    """The entry-15 value proxies admit the single character İ alongside
    their two-character minimum, because its str.lower() expands to two
    codepoints and survives StandardAnalyzer's minsize. This derives the
    full set from Unicode itself so the hardcoded character cannot drift:
    if a Python upgrade ever adds another expanding character, this fails
    and the regexes gain it deliberately rather than silently missing it.
    """
    import sys

    expanding = {c for c in map(chr, range(0x80, sys.maxunicode + 1)) if len(c.lower()) > 1}
    assert expanding == {"İ"}


@pytest.mark.parametrize(
    "q",
    [
        pytest.param("02091-C-71", id="dash-interior-1char-piece"),
        pytest.param("02091-C-712", id="dash-interior-1char-piece-2"),
        pytest.param("02091-C-71a", id="dash-interior-1char-piece-3"),
        pytest.param("02091-C-76hallo", id="dash-interior-1char-piece-4"),
        pytest.param("9,90", id="bare-comma-keyword-path"),
        pytest.param("test 12,34 some use", id="comma-keyword-path-in-context"),
        pytest.param("ASN>1593902", id="comparison-operator-separator"),
        pytest.param("वर्तमान", id="devanagari-single-word"),
        pytest.param("वर्तमान क्षण की धन्यता", id="devanagari-phrase"),
        pytest.param("'02091-C-71'", id="bare-single-quoted"),
        pytest.param("zzz:foobar", id="unknown-field-colon"),
        pytest.param("zzz:İ", id="unknown-field-colon-i-exception"),
        pytest.param("document_type:[Receipt]", id="unknown-field-bracket-no-range"),
        pytest.param("attrs:İ", id="unknown-field-colon-i-exception-2"),
        pytest.param("foobar:today", id="unknown-field-colon-keyword-value"),
        pytest.param("dat:'-1 year to now'", id="unknown-field-quoted-with-internal-spaces"),
        pytest.param(
            "type: A OR type: B OR custom_field_name >= 2025-01-01",
            id="unknown-field-in-larger-query",
        ),
        pytest.param("tag: 11-33 Mirka", id="unknown-field-dash-value"),
        pytest.param("title:ab-cd OR x", id="fielded-text-dash-in-or"),
        pytest.param("title:'ab-cd' OR x", id="fielded-text-single-quoted-in-or"),
        pytest.param("title:ASN>1593902 OR x", id="fielded-text-comparison-operator-in-or"),
        pytest.param("title:a.b-cd OR x", id="fielded-text-dot-glue-plus-dash-in-or"),
        pytest.param("title:ab--cd OR x", id="fielded-text-double-dash-in-or"),
        pytest.param("tag:'0,00' OR x", id="fielded-keyword-single-quoted-in-or"),
        pytest.param("title:02091-C-71 OR x", id="fielded-text-interior-1char-piece-in-or"),
    ],
)
def test_entry_15_claims_genuine_divergences(q: str) -> None:
    """Every shape here is a genuine, measured divergence per the design
    spec (docs/superpowers/specs/2026-08-24-issue-47-multitoken-boundary-design.md);
    entry 15's two allowlist regexes must claim all of them.
    """

    entry = allowed_entry(q)
    assert entry is not None, f"expected {q!r} to be claimed by an allowlist entry"
    assert "entry 15" in entry[0], f"expected {q!r} claimed by entry 15, got: {entry[0]!r}"


@pytest.mark.parametrize(
    "q",
    [
        pytest.param("200[1-9]", id="bracket-class-not-a-dash-split"),
        pytest.param("ab-ab", id="all-identical-dash-chain"),
        pytest.param("ab-ab-ab", id="all-identical-dash-chain-longer"),
        pytest.param("AB-ab", id="all-identical-dash-chain-case-insensitive"),
        pytest.param("hello", id="plain-single-word"),
        pytest.param("example", id="plain-single-word-2"),
        pytest.param("title", id="plain-single-word-3"),
        pytest.param("a,a", id="all-identical-comma-pair"),
        pytest.param("title:ab-cd", id="known-field-no-or"),
        pytest.param('"ab-cd"', id="bare-double-quoted"),
        pytest.param('title:"ab-cd"', id="known-field-double-quoted-no-or"),
        pytest.param("zzz:[2020 TO 2024]", id="unknown-field-genuine-range"),
        pytest.param('zzz:"ab-cd"', id="unknown-field-double-quoted"),
        pytest.param("zzz:[2020 To 2024]", id="unknown-field-range-mixed-case-to"),
        pytest.param("zzz:[2020 tO 2024]", id="unknown-field-range-mixed-case-to-2"),
        pytest.param("zzz:[2020 TO 2024}", id="unknown-field-range-mismatched-bracket"),
        pytest.param("tag:ab,cd", id="unquoted-keyword-comma-no-or"),
        pytest.param("produ*name", id="wildcard-not-a-dash-split"),
        pytest.param("9.90", id="dot-glued-not-a-comma-split"),
        pytest.param("19?90", id="single-char-wildcard-not-a-dash-split"),
        pytest.param('title:"ab-cd" OR x', id="fielded-text-double-quoted-in-or"),
        pytest.param('tag:"ab,cd" OR x', id="fielded-keyword-double-quoted-in-or"),
        pytest.param('tag:"9,90" OR x', id="fielded-keyword-double-quoted-in-or-2"),
        pytest.param("title:'9,90' OR x", id="fielded-text-single-quoted-comma-in-or"),
        pytest.param("tag:ab,cd OR x", id="fielded-keyword-unquoted-comma-in-or"),
    ],
)
def test_entry_15_does_not_claim_agreeing_shapes(q: str) -> None:
    """Every shape here compares EQUAL to the oracle (measured directly
    during design, see the spec's Corrections 3-8); entry 15's two
    allowlist regexes must NOT claim any of them, or a future differential
    run would strict-xfail-fail on a query that does not actually diverge.
    """

    entry = allowed_entry(q)
    if entry is not None:
        assert "entry 15" not in entry[0], f"{q!r} wrongly claimed by entry 15: {entry[0]!r}"
