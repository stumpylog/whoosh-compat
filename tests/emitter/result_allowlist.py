"""Result-level divergence allowlist for the acceptance property
(``test_acceptance_property.py``): documents query patterns where whoosh and
whoosh-compat both parse and search successfully, but the returned
document-id sets are *expected* to differ.

This is the result-level sibling of ``tests/differential/allowlist.py``,
built once that AST-level module's discipline (a regex keyed by a reference
prefix, matched against the query string, each citing a numbered
``DIVERGENCES.md`` entry) had somewhere to point that also proved the
divergence survives all the way to a live search, not just a parsed tree.

Deliberately narrower taxonomy than the AST-level module's
:class:`~tests.differential.allowlist.DivergenceKind`: that module needs
both ``MISMATCH`` (both sides parse, trees differ) and ``ORACLE_ERROR``
(the oracle itself raises while parsing) because the AST-comparison property
never skips a query on its own, it always runs *something* structurally.
The acceptance property here is built the other way around
(``test_acceptance_property.py``'s module docstring): it skips outright,
before ever consulting this module, on either a whoosh-compat parse
diagnostic, a documented whoosh-compat emit-time error, or a real-whoosh
parse/search exception, exactly mirroring the AST layer's uniform "entry 6"
diagnostic skip plus the grammar fuzzer's try/except around ``oracle_parse``.
Every query that reaches this module's ``allowed_result_reason`` therefore
already parsed and searched successfully on *both* sides. Unlike the
AST-comparison layer, this module's divergences are frequently
*value*-dependent (whether a demoted unfielded search happens to hit
something, whether a boolean field's default happens to be false for every
document, exactly which small integer trips a whoosh numeric-range defect),
so a generated example matching one of these patterns is skipped rather than
strict-xfail-asserted the way the AST layer's allowlist entries are: see
``test_acceptance_property.py``'s ``_acceptance_property`` for the full
reasoning. The strict "these two really differ" proof for each entry instead
lives in a hand-picked scenario test, one per entry, in
``test_acceptance_property.py``. A single flat list (no kind enum) is the
whole taxonomy this layer needs, since there is only one thing an entry ever
means here ("known, already-proven-elsewhere divergence, skip the generated
comparison"); if a future need arises to distinguish more outcomes, add a
kind field then, a one-member enum today would just be ceremony.
"""

from __future__ import annotations

import re

from tests.differential.allowlist import BOOL_EXISTS_FIELDS_PATTERN
from tests.differential.allowlist import DATE_FIELDS_PATTERN
from tests.differential.allowlist import KEYWORD_FIELDS_PATTERN
from tests.differential.allowlist import REGISTERED_FIELDS_PATTERN
from tests.differential.allowlist import ZERO_TOKEN_WORD

# (pattern, DIVERGENCES.md reference + short reason)
RESULT_ALLOW: list[tuple[re.Pattern[str], str]] = [
    # DIVERGENCES.md entries 14/37 (design, extended to the result level):
    # "attrs" (a JSON field) and "release_date" (a date_only field) are
    # whoosh-compat-only concepts with no v2 whoosh analogue at all; the
    # real whoosh oracle schema (oracle.oracle_schema(), used unmodified for
    # this property's windex_prop fixture) has neither field registered.
    # Real whoosh's own FieldsPlugin demotes an unrecognized fieldname to an
    # ordinary unfielded term, multifield-expanded across V2_FIELDS instead
    # (an entirely different search than a JSON-subpath or date-only query),
    # so this isn't just an AST-shape difference the way entries 2/12 turned
    # out to be (DIVERGENCES.md entry 16): it changes what gets searched,
    # confirmed directly against this property's live dual index (see
    # test_attrs_and_release_date_are_result_level_divergences below).
    (
        # Scoped to the dotted subpath form only ("attrs.user:", not bare
        # "attrs:"): a bare JSON field name demotes identically on both
        # sides (FieldRegistry.is_bare_json_field, DIVERGENCES.md entry 29),
        # verified directly that "attrs:0" produces the same (empty) result
        # set on both sides, so it must NOT match here.
        re.compile(r"\battrs\.(user|note|value|name)\b"),
        (
            "DIVERGENCES.md entry 14 (design, result-level): 'attrs.<subpath>'"
            " is a whoosh-compat-only JSON-subpath query with no v2 whoosh"
            " schema entry; real whoosh's non-dot-aware tagger and unknown-field"
            " demotion produce an entirely different unfielded multifield"
            " search instead"
        ),
    ),
    (
        re.compile(r"\brelease_date\b"),
        (
            "DIVERGENCES.md entry 37 (design, result-level): release_date is a"
            " whoosh-compat-only date_only field with no v2 whoosh schema"
            " entry; real whoosh demotes the fieldname to an unfielded"
            " multifield search instead of a date-only query"
        ),
    ),
    # DIVERGENCES.md entry 12 (whoosh-bug, result-level extension): unlike
    # tests/emitter/conftest.py's DOCS fixture (whose created/added values
    # are never close enough to a local-midnight boundary for the bug's
    # absence on whoosh-compat's side to matter, see DIVERGENCES.md entry
    # 16), this property's own fixture (test_acceptance_property.py's
    # DOCS_PROP) deliberately includes documents whose local Europe/Berlin
    # calendar day differs from their UTC calendar day. A bracketed range on
    # created/modified/added therefore now visibly picks up a different
    # document set depending on which side's (buggy real-whoosh vs correct
    # whoosh-compat) timezone handling ran, confirmed directly against the
    # live dual index (see test_created_range_near_local_midnight_is_a_result_level_divergence).
    (
        # At least one digit inside the brackets: a fully open range
        # ("created:[TO]", both bounds absent) is Every(field) on both
        # sides and genuinely does not diverge (verified directly), so it
        # must not match here; every bound the generator actually produces
        # a date from (_date_range_atom) has a digit somewhere between the
        # brackets.
        re.compile(r"\b(?:created|modified|added):[\[{][^\]}]*\d"),
        (
            "DIVERGENCES.md entry 12 (whoosh-bug, result-level extension): a"
            " bracketed date range near a local-midnight boundary now visibly"
            " matches a different document set, since real whoosh's"
            " range_to_dt bug skips the timezone conversion whoosh-compat"
            " applies uniformly"
        ),
    ),
    # DIVERGENCES.md entry 23 (design, result-level extension): a zero-token
    # term/phrase (a stopword, or a token shorter than StandardAnalyzer's
    # minsize=2) combined with NOT/ANDNOT/ANDMAYBE/REQUIRE, or with a bare
    # "*" (Every), collapses differently on the two sides: whoosh-compat's
    # uniform, timing-independent emit-time drop (visit_not's
    # MustNot(empty_query()), _lone_operand's "leave the survivor",
    # ast.normalize()'s parse-time Not(Nothing()) -> Every() rule combined
    # with Every "absorbing" an enclosing And) disagrees with real whoosh's
    # own parse-time/normalize-time handling of the same shape in ways that
    # depend on the surrounding structure (confirmed directly, each a
    # distinct concrete failure while building this property: "NOT
    # (title:the)" matches every document in whoosh-compat, none in real
    # whoosh; "NOT (*:* title:the)" the same, once whoosh-compat's own
    # normalize() has already dropped the harmless "*:*" Every() sibling
    # before NOT ever sees it; "((title:the)^0.5) ANDNOT ((NOT (0)))"
    # matches every document in whoosh-compat via a dropped, boosted,
    # doubly-parenthesized positive operand; "*:* title:the", with no NOT
    # at all, matches *no* documents in whoosh-compat (Every() is absorbed
    # into the And, leaving the zero-token term alone, which then drops to
    # Query.empty_query() at emit time) but matches every document in real
    # whoosh (the parser drops "the" at syntax level, leaving Every() alone
    # as the whole query)). Every one of these is the same underlying
    # "emit-time-uniform vs. parse-time/structure-dependent" tradeoff
    # entry 23 already accepts as deliberate, just reached through a
    # different surrounding structure each time; exactly characterizing
    # which structures reach it via a tightly scoped regex turned out
    # impractical (three narrower, more position-specific attempts each
    # missed a subsequent structural variant), so this entry is scoped
    # loosely instead: a known zero-token word (see
    # strategies.ZERO_TOKEN_WORDS) as a field:value pattern on any
    # non-KEYWORD field, anywhere in a query that also contains
    # NOT/ANDNOT/ANDMAYBE/REQUIRE or a bare "*" anywhere. This also skips a
    # few genuinely-agreeing shapes (a bare, unparenthesized zero-token
    # ANDNOT/ANDMAYBE/REQUIRE operand with no Every involved, already
    # verified to agree with whoosh, see
    # tests/emitter/test_emit_boolean.py's
    # test_zero_token_operand_leaves_the_survivor), which only costs a
    # little generated-example coverage, never a false "these differ"
    # assertion (see this module's docstring on why that tradeoff is safe
    # here specifically).
    (
        re.compile(
            r"(?=.*(?:\bNOT\b|\bANDNOT\b|\bANDMAYBE\b|\bREQUIRE\b"
            r"|(?:^|[\s(:])\*(?:$|[\s)])))"
            rf".*(?!(?:{KEYWORD_FIELDS_PATTERN}):)\b\w+:"
            rf"{ZERO_TOKEN_WORD}\b"
        ),
        (
            "DIVERGENCES.md entry 23 (design, result-level extension,"
            " including its ANDNOT/ANDMAYBE/REQUIRE extension): a zero-token"
            " term/phrase combined with NOT/ANDNOT/ANDMAYBE/REQUIRE or a bare"
            " '*' (Every) collapses differently on the two sides, whoosh-compat"
            " always applying its uniform emit-time drop, real whoosh's own"
            " parse-time/normalize-time handling depending on the surrounding"
            " structure"
        ),
    ),
    # DIVERGENCES.md entry 33 (design, result-level extension): a
    # whitespace-padded quoted BOOLEAN_EXISTS value reads False in
    # whoosh-compat (strips before the trues/falses membership check) but
    # True in real whoosh (BOOLEAN._obj_to_bool checks the *unstripped*
    # text, then falls through to bool(qstring), True for any non-empty
    # string). Confirmed directly against this property's fixture (every
    # DOCS_PROP row's has_* flags are explicitly False, see windex_prop):
    # "has_correspondent:'  false'" matches every document in whoosh-compat
    # (false matches everyone) and none in real whoosh (true matches no
    # one). Same pattern as tests/differential/allowlist.py's entry-33
    # entry, reused here since the mismatch is visible at the result level
    # too, not just in the parsed tree.
    (
        re.compile(
            rf"\b(?:{BOOL_EXISTS_FIELDS_PATTERN}):"
            r"(?:'\s+\S.*'|'.*\S\s+'|'\s+')"
        ),
        (
            "DIVERGENCES.md entry 33 (design, result-level extension): a"
            " whitespace-padded quoted BOOLEAN_EXISTS value reads False in"
            " whoosh-compat (stripped before the trues/falses check) but True"
            " in real whoosh (unstripped check falls through to"
            " bool(qstring))"
        ),
    ),
    # DIVERGENCES.md entry 40 (whoosh-bug, not reproduced): NOT of a group
    # that recursively collapses to empty (nesting depth two or more, e.g.
    # "NOT (())", "NOT ((())^0.5)") parses to a bare Nothing() in
    # whoosh-compat (matches no documents), the same deliberate
    # empty-group-drops-out rule entry 27 documents applied uniformly
    # regardless of depth, but real whoosh's own AST for the equivalent
    # shape isn't uniform by depth: at nesting depth one it also collapses
    # to NullQuery (matches nothing, agreeing with whoosh-compat, so a bare
    # "NOT ()" must NOT match this entry), but at depth two or more it keeps
    # a real Not(And([And([])])) tree whose *execution* (not its AST shape)
    # matches every document. Scoped to a NOT immediately wrapping
    # parenthesized content that is nothing but whitespace, nested parens,
    # digits, dots and carets (an empty group, possibly repeated/boosted,
    # never a real field:value clause) with at least two levels of nesting.
    (
        re.compile(r"\bNOT\s*\((?=[^)]*\()[\s()0-9.^]*\)"),
        (
            "DIVERGENCES.md entry 40 (whoosh-bug, not reproduced): NOT of a"
            " recursively-empty group matches nothing in whoosh-compat"
            " (uniform empty-group-drops-out rule) but matches every document"
            " in real whoosh once nesting depth reaches two or more (an"
            " artifact of how Not/And matchers execute a"
            " structurally-nonempty-but-semantically-empty operand, not a"
            " consistent whoosh design to reproduce)"
        ),
    ),
    # DIVERGENCES.md entry 41 (whoosh-bug, not reproduced): a real-whoosh
    # NumericRange can match every document regardless of any document's
    # actual field value, for reasons that turned out not to reduce to one
    # narrow, regex-describable shape. Directly confirmed trigger shapes
    # (bisected against a live oracle index, four documents with asn
    # 100-103): a single-value range with a small bound (asn:[5 TO 5]:
    # N <= 10 broken, N >= 20 fine); an exclusive-bracket range regardless
    # of magnitude (asn:{100 TO 127]); and, found while trying to
    # characterize the second shape further, a perfectly ordinary
    # *inclusive*-bracket range whose upper bound alone crosses 127
    # (asn:[101 TO 127], no exclusive bracket, no small bound at all).
    # 127 == 2**7 - 1 in every broken case found, consistent with a
    # Lucene-style numeric-trie precision-step tier boundary, but the
    # pattern is reachable through multiple, not-obviously-related bound
    # combinations rather than one identifiable shape, so this entry
    # deliberately gives up on narrowing further and instead treats *any*
    # bracketed range on one of ORACLE_REGISTRY's U64 fields as suspect.
    # Safe to be this broad: this entry is used only to skip a generated
    # example (see this module's docstring), never to strict-xfail-assert
    # against essentially random generated bounds, and whoosh-compat's own
    # numeric range behavior already has independent, targeted regression
    # coverage elsewhere (test_acceptance_e2e.py's "asn:[100 TO 102]"
    # scenario; this module's own small-bound and exclusive-bound scenario
    # tests) that does not depend on this broad skip.
    (
        re.compile(
            r"\b(?:id|asn|correspondent_id|type_id|path_id|num_notes"
            r"|custom_field_count|owner_id|page_count):[\[{]"
        ),
        (
            "DIVERGENCES.md entry 41 (whoosh-bug, not reproduced): a"
            " NumericRange can match every document in real whoosh regardless"
            " of any document's actual field value, for several distinct,"
            " only partially characterized bound shapes; every bracketed"
            " numeric range is treated as suspect rather than narrowed further"
        ),
    ),
    # whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated
    # datetime value. whoosh parses it to _NullQuery (matches nothing);
    # whoosh-compat parses the correct DateRange and returns the intended
    # documents. Ordered before the dashed-token entry-15 pattern, which
    # would otherwise claim the digits inside the quotes under the wrong
    # divergence. The quote is REQUIRED: the bare unquoted spelling
    # colon-tokenizes differently in whoosh (not _NullQuery), so this
    # reason string would be false for it.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'\d{{4}}-\d\d-\d\d[Tt]"),
        (
            "whoosh-bug (DIVERGENCES.md entry 48): a single-quoted T-separated"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a correct DateRange in whoosh-compat"
        ),
    ),
    # design (DIVERGENCES.md entry 49, value-dependent): the BARE unquoted
    # sibling. Both sides truncate to the same month period and AND it
    # with the stray time tokens, so both usually return nothing; when a
    # document inside the window contains one (but not all) of the stray
    # tokens, whoosh-compat's OR over them matches where real whoosh's
    # per-field AND does not. Ordered before the entry-15 dashed-token
    # pattern, whose TEXT-multitoken framing does not describe the date
    # truncation face. The optional day group admits the no-day spelling
    # ("2026-06T10:30"), which truncates to the year window by the same
    # mechanism.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):\d{{4}}-\d\d(?:-\d\d)?[Tt]"),
        (
            "design (DIVERGENCES.md entry 49, value-dependent): a bare"
            " unquoted T-separated datetime value truncates to the same"
            " month period on both sides; results differ only when a"
            " document contains some but not all of the leftover time"
            " tokens (whoosh-compat ORs them, real whoosh ANDs them)"
        ),
    ),
    # whoosh-bug (DIVERGENCES.md entry 50): a no-separator T-fused value
    # ("2026T10", bare or single-quoted, optionally colon-extended).
    # whoosh parses it to _NullQuery and returns nothing; whoosh-compat
    # reads year-T-month and returns every document in that window.
    # T directly after the year keeps this disjoint from entries 48/49;
    # ordered before the entry-15 patterns for the same
    # unknown-field-demotion mis-claim reason as the AST layer.
    (
        re.compile(rf"\b(?:{DATE_FIELDS_PATTERN}):'?\d{{4}}[Tt]\d"),
        (
            "whoosh-bug (DIVERGENCES.md entry 50): a no-separator T-fused"
            " datetime value parses to _NullQuery (matches nothing) in"
            " whoosh but a year-T-month DateRange in whoosh-compat"
        ),
    ),
    # whoosh-bug (DIVERGENCES.md entry 45): a double-quoted separated-ISO
    # date value. whoosh parses it to a Phrase that RAISES QueryError at
    # search time (a DATETIME field has no positions), so there is no
    # oracle result set to compare; whoosh-compat parses the correct
    # day-period DateRange. Ordered before the entry-15 dashed-token
    # pattern below, which would otherwise claim the spelling (its
    # \w+-\w+ alternative matches the digits inside the quotes) under
    # the wrong divergence.
    (
        re.compile(rf'\b(?:{DATE_FIELDS_PATTERN}):"\d{{4}}(?:[-. /]|[Tt])\d[^"]*"'),
        (
            "whoosh-bug (DIVERGENCES.md entry 45): double-quoted"
            " separated-ISO date parses to a search-time-crashing Phrase in"
            " whoosh but a correct day/month-period DateRange in whoosh-compat"
        ),
    ),
    # whoosh-bug (DIVERGENCES.md entry 47): a NOT operand under
    # ANDNOT/ANDMAYBE/REQUIRE. whoosh's Not is root-only (its matcher's
    # own comment); as a binary-query operand the composition produces
    # incoherent results (measured: AndNot(Not(a), Every()) returns a
    # strict subset that no consistent algebra yields, and a Not negative
    # can be ignored entirely), while whoosh-compat computes ordinary set
    # algebra. Scoped, entry-41-style, to any query combining one of the
    # binary keywords with a standalone NOT token: the incoherence's
    # trigger is not characterized further on purpose. \bNOT\b cannot
    # match inside the ANDNOT keyword itself (no word boundary after the
    # D), so a query using only ANDNOT does not match.
    (
        re.compile(r"(?=.*\b(?:ANDNOT|ANDMAYBE|REQUIRE)\b)(?=.*\bNOT\b)"),
        (
            "whoosh-bug (DIVERGENCES.md entry 47): whoosh's root-only Not"
            " composes incoherently as an ANDNOT/ANDMAYBE/REQUIRE operand;"
            " whoosh-compat computes ordinary boolean set algebra"
        ),
    ),
    # DIVERGENCES.md entry 15 (design, confirmed result-level): a multitoken
    # TEXT-field value nested inside a top-level Or (either an unfielded
    # word, always multifield-expanded into an Or, or a fielded value
    # explicitly OR-combined) combines its tokens with OR in whoosh-compat
    # (Multitoken.DEFAULT follows the syntactic enclosing group) but with
    # AND in real whoosh (multitoken_query='default' always follows the
    # parser's fixed default group). Confirmed directly: an unfielded
    # "00-YEAR" matches whoosh-compat's doc 3 (title contains "year" but
    # not "00") but matches nothing in real whoosh (requires both tokens in
    # the same field). Scoped to a dashed two-word value, either bare
    # (unfielded, hence always Or-nested) or fielded and appearing anywhere
    # alongside an explicit "OR" in the query text; and, a second confirmed
    # trigger pathway, a bare (subpath-less) registered-JSON-field value
    # (DIVERGENCES.md entry 29's demotion): "attrs:END" demotes to the
    # literal unfielded text "attrs:END" on both sides, which tokenizes to
    # two surviving tokens ("attrs", "end"), reaching the identical
    # multitoken-OR-vs-AND mismatch (confirmed: matches doc 3, whose
    # content contains "end" but not "attrs", in whoosh-compat only). Not
    # every bare-JSON-demoted value hits this: "attrs:0" tokenizes to just
    # one surviving token ("0" is dropped, shorter than minsize=2), so
    # there's no OR-vs-AND ambiguity and both sides agree; excluding a
    # single-character value from this pattern isn't attempted, since
    # skipping a few additional non-divergent generated examples costs
    # nothing (see this module's docstring).
    (
        # Alternatives, in order: a bare dashed word; a dashed word near
        # an explicit OR; a single-quoted comma value near an explicit OR
        # (the shape _comma_atom emits; an UNQUOTED comma value converges
        # via the parse-time split, measured non-divergent, so only the
        # quoted spelling is claimed); the bare-JSON demotion; and the
        # unknown-field demotion, mirroring the differential twin. An
        # unregistered fieldname demotes on both sides and its colon-split
        # tokens reach the OR-vs-AND mismatch ("notes.user:YEAR" matching
        # doc 3 in whoosh-compat only, found by a deep fuzz soak; for a
        # DOTTED unregistered name whoosh demotes in two pieces via its
        # mid-token tagger, entry 14's mechanism, so the parsed trees
        # differ by more than the combinator, but the RESULT divergence is
        # still the OR-vs-AND one, verified). İ is the one character whose
        # lowercase expands to two codepoints, surviving minsize despite
        # being a single character.
        re.compile(
            r"(?<![:\w])\w+-\w+\b(?!:)"
            r"|\bOR\b.*\b\w+-\w+\b|\b\w+-\w+\b.*\bOR\b"
            r"|\bOR\b[^']*'[^']*,[^']*'|'[^']*,[^']*'[^']*\bOR\b"
            r"|\battrs:(?!\*)\S"
            rf"|\b(?!(?:{REGISTERED_FIELDS_PATTERN}|is_shared)\b)"
            r"\w{2,}:(?:[^\s():]{2,}|İ)"
        ),
        (
            "DIVERGENCES.md entry 15 (design, confirmed result-level): a"
            " multitoken TEXT-field value nested inside a top-level Or combines"
            " its tokens with OR in whoosh-compat (Multitoken.DEFAULT follows"
            " the syntactic enclosing group) but with AND in real whoosh"
            " (multitoken_query='default' follows the parser's fixed default"
            " group)"
        ),
    ),
]


def allowed_result_reason(query: str) -> str | None:
    """The reference string of the first entry matching ``query``, or
    ``None`` if no entry matches (the caller must then assert equal
    document-id sets).
    """

    for pattern, reason in RESULT_ALLOW:
        if pattern.search(query):
            return reason
    return None
