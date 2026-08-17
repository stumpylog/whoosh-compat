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
from tests.differential.allowlist import allowed_reason
from tests.differential.oracle import ORACLE_REGISTRY
from whoosh_compat.fields import FieldKind

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DIVERGENCES_PATH = _ROOT / "DIVERGENCES.md"
_DIVERGENCES_TEXT = _DIVERGENCES_PATH.read_text(encoding="utf-8")

_CORPUS_DIR = pathlib.Path(__file__).parent
_CORPUS_FILES = ("corpus_paperless.txt", "corpus_docs.txt")
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
    reason = allowed_reason(f"asn:1 OR {field}:'a,b'")
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
    ],
)
def test_allowlist_regex_scoping(query: str, expected_entry: int | None) -> None:
    """Pins the overmatch/undermatch boundary of the precision-sensitive
    allowlist regexes: each claimed spelling was verified by execution to
    genuinely diverge (or, for the date-range row, to belong to the cited
    entry's mechanism), and each unclaimed spelling to compare equal or
    belong elsewhere. A regex edit that widens or narrows past these
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
