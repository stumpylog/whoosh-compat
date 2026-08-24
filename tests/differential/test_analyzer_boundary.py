"""Token-stream comparison between whoosh's ``StandardAnalyzer`` (the
differential oracle's own TEXT-field analyzer, see ``oracle.py``'s
``_analyze``) and paperless-ngx's actual host tokenizer chains
(``tests/emitter/conftest.py``'s ``lower_fold``/``stem_fold``).

``tests/differential/`` cannot see any divergence whose cause is analyzer
behavior: ``oracle.ORACLE_REGISTRY`` uses whoosh's own ``StandardAnalyzer``
for *both* the oracle's tree and whoosh-compat's own parsed tree, so a
comparison run through that registry is structurally blind to tokenizer
differences no matter how many queries it runs.

This module compares token *streams* directly instead, bypassing the parser
and oracle entirely: per ``ARCHITECTURE.md``'s "analyzer contract" section, a
value's token *count* (0 / 1 / many) is what drives AST shape downstream
(``analyze()``'s zero-token drop and ``Multitoken.DEFAULT`` grouping), so a
token-count mismatch is exactly the class of divergence that would otherwise
go undetected. Token *content* differences that don't change the count (for
example stemming: "university" vs "univers") are out of scope here; those
are a separate, already-documented divergence surface.

8 of the value classes below are marked ``xfail(strict=True)``, documented as
a permanent, accepted analyzer-fidelity difference: DIVERGENCES.md entry 59.
Whoosh's oracle StandardAnalyzer chains a StopFilter(minsize=2) that drops
tokens under 2 characters; paperless-ngx's real host analyzer chains
(``lower_fold`` and ``stem_fold``) have no such filter, so these 8 values
produce different token *counts* between the oracle and the host. This
module compares token counts directly against the host analyzers, never
through any allowlist mechanism, so fixing entry 15's Multitoken.DEFAULT
boundary regex (a differential-layer test-classification tool, already fixed
elsewhere) could never have changed what a real analyzer callable produces.
The stopword class is triaged separately below: it is DIVERGENCES.md entry 4,
a deliberate policy choice, not a pending finding.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.differential.oracle import _analyze as whoosh_analyze
from tests.emitter.conftest import lower_fold
from tests.emitter.conftest import stem_fold

# Representative values, one per token-count-affecting mechanism identified
# across real-world corpus findings. Each is a *raw value* (the text a Term
# node would carry pre-analysis), not a query string with a field prefix.
#
# The 8 marked xfail(strict=True) below are a permanent, accepted
# analyzer-fidelity divergence from whoosh, documented as DIVERGENCES.md
# entry 59: whoosh's oracle StandardAnalyzer chains StopFilter(minsize=2),
# dropping tokens under 2 characters (interior dash pieces, comma decimals,
# word-internal analyzer splits), while the real host analyzer chains keep
# them. strict=True means an unexpected pass fails the suite loudly instead
# of silently going stale, which would signal a change in host analyzer
# behavior and require investigation.
_XFAIL_MULTITOKEN_BOUNDARY = pytest.mark.xfail(
    strict=True,
    reason="DIVERGENCES.md entry 59: whoosh's StopFilter(minsize=2) vs the real host analyzer",
)

REPRESENTATIVE_VALUES = [
    pytest.param("hello", id="plain-word-sanity-control"),
    pytest.param("example", id="plain-word-sanity-control-2"),
    pytest.param("200[1-9]", id="bracket-class-no-wildcard", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param("02091-C-71", id="interior-1char-dash-piece", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param("02091-C-712", id="interior-1char-dash-piece-2", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param("02091-C-71a", id="interior-1char-dash-piece-3", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param(
        "02091-C-76hallo", id="interior-1char-dash-piece-4", marks=_XFAIL_MULTITOKEN_BOUNDARY
    ),
    pytest.param("9,90", id="comma-decimal", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param("12,34", id="comma-decimal-2"),
    pytest.param("ASN>1593902", id="comparison-operator"),
    pytest.param("200", id="plain-3digit-number"),
    pytest.param("2001", id="plain-4digit-number"),
    pytest.param("university", id="stem-pair-university"),
    pytest.param("universities", id="stem-pair-universities"),
    pytest.param("company", id="stem-pair-company"),
    pytest.param("companies", id="stem-pair-companies"),
    pytest.param("copyright", id="stem-pair-copyright"),
    pytest.param("copies", id="stem-pair-copies"),
    pytest.param("Wärrantyplan", id="diacritic-single-word"),
    pytest.param("वर्तमान", id="devanagari-single-word", marks=_XFAIL_MULTITOKEN_BOUNDARY),
    pytest.param("वर्तमान क्षण की धन्यता", id="devanagari-phrase", marks=_XFAIL_MULTITOKEN_BOUNDARY),
]


@pytest.mark.parametrize("value", REPRESENTATIVE_VALUES)
def test_lower_fold_token_count_matches_whoosh(value: str) -> None:
    """Finding sensor: does whoosh's ``StandardAnalyzer`` token count agree
    with paperless-ngx's plain (unstemmed) host chain for this value?

    An un-``xfail``-marked failure here is a new, untriaged divergence
    class from this module's own investigation; a marked one is a known,
    permanent analyzer-fidelity difference, DIVERGENCES.md entry 59 (see
    ``REPRESENTATIVE_VALUES``' ``_XFAIL_MULTITOKEN_BOUNDARY``).
    """
    whoosh_tokens = whoosh_analyze(value)
    host_tokens = lower_fold(value)
    assert len(whoosh_tokens) == len(host_tokens), (
        f"value={value!r}: whoosh={whoosh_tokens!r} ({len(whoosh_tokens)} tokens) "
        f"vs lower_fold={host_tokens!r} ({len(host_tokens)} tokens)"
    )


@pytest.mark.parametrize("value", REPRESENTATIVE_VALUES)
def test_stem_fold_token_count_matches_whoosh(value: str) -> None:
    """Same sensor as above, against the stemmed host chain
    (``stem_fold``): stemming changes token *content*, not *count*, so this
    should agree everywhere ``lower_fold`` does.
    """
    whoosh_tokens = whoosh_analyze(value)
    host_tokens = stem_fold(value)
    assert len(whoosh_tokens) == len(host_tokens), (
        f"value={value!r}: whoosh={whoosh_tokens!r} ({len(whoosh_tokens)} tokens) "
        f"vs stem_fold={host_tokens!r} ({len(host_tokens)} tokens)"
    )


# --------------------------------------------------------------------------
# Stopwords: triaged, not a pending finding. DIVERGENCES.md entry 4.
# --------------------------------------------------------------------------

STOPWORD_VALUES = [
    pytest.param("a", id="bare-stopword-single-char"),
    pytest.param("to", id="bare-stopword-2char"),
    pytest.param("the", id="bare-stopword-3char"),
    pytest.param("of", id="bare-stopword-of"),
    pytest.param("in", id="bare-stopword-in"),
    pytest.param("and", id="bare-stopword-and"),
]


@pytest.mark.parametrize("value", STOPWORD_VALUES)
@pytest.mark.parametrize("host_analyzer", [lower_fold, stem_fold], ids=["lower_fold", "stem_fold"])
def test_stopwords_are_a_documented_host_analyzer_divergence(
    value: str, host_analyzer: Callable[[str], list[str]]
) -> None:
    """DIVERGENCES.md entry 4: "whoosh-compat takes no position on
    stopwords, it uses whatever tokens the host's ``analyzer`` returns."

    Real whoosh's ``StandardAnalyzer`` bakes in a stopword filter
    unconditionally, dropping the value to zero tokens. Neither
    ``lower_fold`` nor ``stem_fold`` (paperless-ngx's actual host chains,
    see ``tests/emitter/conftest.py``) filters stopwords, so the same value
    survives as a real, searchable one-token term through whoosh-compat.
    This is deliberate: whoosh-compat's ``FieldSpec.analyzer`` contract is
    bring-your-own (ARCHITECTURE.md's "analyzer contract"), and it is a
    host's choice, not this library's, whether to filter stopwords at all.
    A host that wants whoosh's behavior gets it by wiring a stopword filter
    into its own analyzer; whoosh-compat does not impose one.
    """
    assert whoosh_analyze(value) == []
    assert host_analyzer(value) == [value.lower()]
