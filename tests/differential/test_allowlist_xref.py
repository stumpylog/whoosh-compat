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

from tests.differential.allowlist import ALLOW

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
# whitespace in between), so prose like "corpus_docs.txt's issue #16
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
