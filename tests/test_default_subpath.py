"""Parse-level behavior of a JSON field that declares a default subpath.

A JSON field with no default subpath is unresolvable when addressed bare
(``notes:foo``), which forces a host that wants ``notes:`` to mean
``notes.note:`` to rewrite the raw query string before parsing. That rewrite
is quote-blind, so it also mangles ``content:"payment notes: none"``, where
those characters are ordinary text and not a field prefix at all. Declaring
one subpath the default moves the rewrite inside the real, quote-aware
parser.

These tests pin the two consumers of ``FieldRegistry.is_bare_json_field``,
both of which change behavior for a defaulted field without being touched:
``FieldsPlugin.do_fieldnames`` (via ``__contains__``, the demotion decision)
and ``ast.free_text_tokens``. They also pin the shapes where a defaulted
bare name now yields a diagnostic (a pattern) or a refusable AST (a range)
instead of the silent default-field text search it used to demote to;
DIVERGENCES.md entries 20 and 30 record that.
"""

from __future__ import annotations

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.ast import free_text_tokens
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import SubpathSpec


def _words(text: str) -> list[str]:
    return [t for t in text.lower().split() if t]


@pytest.fixture
def dreg() -> FieldRegistry:
    """``notes`` declares ``note`` its default subpath; ``cf`` declares none."""
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT, analyzer=_words),
            FieldSpec("title", FieldKind.TEXT, analyzer=_words),
            FieldSpec(
                "notes",
                FieldKind.JSON,
                subpaths={"note": SubpathSpec(default=True), "user": SubpathSpec()},
            ),
            FieldSpec("cf", FieldKind.JSON, subpaths=("a", "b")),
        ]
    )


def parse(q: str, reg: FieldRegistry) -> wc.ParseResult:
    return wc.parse(q, registry=reg, default_fields=["content", "title"])


def test_bare_name_is_not_demoted_and_carries_the_default_subpath(
    dreg: FieldRegistry,
) -> None:
    # The whole point: notes:foo now means notes.note:foo, decided by the
    # parser rather than by a regex over the raw query string.
    result = parse("notes:foo", dreg)
    assert not result.diagnostics
    assert result.ast == ast.Term(field=FieldRef("notes", "note"), text="foo")


def test_explicit_subpath_still_wins_when_parsed(dreg: FieldRegistry) -> None:
    result = parse("notes.user:alice", dreg)
    assert not result.diagnostics
    assert result.ast == ast.Term(field=FieldRef("notes", "user"), text="alice")


def test_a_json_field_without_a_default_still_demotes(dreg: FieldRegistry) -> None:
    # Unchanged for the no-default case: cf:foo is demoted back to text.
    result = parse("cf:foo", dreg)
    assert not result.diagnostics
    node = result.ast
    assert not isinstance(node, ast.Term) or node.field != FieldRef("cf")


def test_the_phrase_the_host_regex_used_to_corrupt(dreg: FieldRegistry) -> None:
    # "Notes:" is an ordinary form label. The host's pre-parse rewrite
    # turned this phrase's contents into notes.note:, so the phrase silently
    # matched nothing; the parser knows it is inside quotes.
    result = parse('content:"payment notes: none"', dreg)
    assert not result.diagnostics
    assert result.ast == ast.Phrase(field=FieldRef("content"), text="payment notes: none")


def test_bare_star_existence_targets_the_default_subpath(dreg: FieldRegistry) -> None:
    # do_fieldnames' bare-JSON carve-out is not reached at all now: the
    # field is recognized outright (``in registry``), so notes:* is an
    # ordinary recognized-field existence check. It therefore narrows from
    # "the whole notes field has any value" to "notes.note has a value" --
    # the same narrowing the host's notes: -> notes.note: rewrite produced.
    result = parse("notes:*", dreg)
    assert not result.diagnostics
    assert result.ast == ast.Every(field=FieldRef("notes", "note"))


def test_bare_star_on_a_field_without_a_default_is_unchanged(dreg: FieldRegistry) -> None:
    # The carve-out still does its job for a JSON field with no default.
    result = parse("cf:*", dreg)
    assert not result.diagnostics
    assert result.ast == ast.Every(field=FieldRef("cf"))


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("notes:fo*", id="trailing-star-prefix-fold"),
        pytest.param("notes:f?o", id="question-mark-wildcard"),
    ],
)
def test_pattern_on_a_defaulted_bare_name_is_diagnosed(dreg: FieldRegistry, query: str) -> None:
    # A defaulted bare name resolves to a subpath, so a pattern on it is
    # DIVERGENCES.md entry 30's parse-time refusal (tantivy-py has no API to
    # scope a pattern query to a subpath), where without a default the same
    # spelling was an unrecognized prefix that demoted to a silent
    # default-field text search. An honest diagnostic replaces a wrong
    # search; it is what a host-side notes: -> notes.note: rewrite already
    # produced.
    result = parse(query, dreg)
    assert isinstance(result.ast, ast.ErrorLeaf)
    assert [d.kind for d in result.diagnostics] == [DiagnosticKind.PATTERN_ON_SUBPATH]
    assert result.diagnostics[0].divergence == 30


def test_pattern_on_a_bare_name_without_a_default_still_demotes(dreg: FieldRegistry) -> None:
    # The contrast that makes the test above meaningful.
    result = parse("cf:fo*", dreg)
    assert not result.diagnostics
    # The whole "cf:fo" run is prefix text over the default fields.
    assert result.ast == ast.Or(
        children=(
            ast.Prefix(field=FieldRef("content"), text="cf:fo"),
            ast.Prefix(field=FieldRef("title"), text="cf:fo"),
        )
    )


def test_range_on_a_defaulted_bare_name_becomes_a_subpath_range(dreg: FieldRegistry) -> None:
    # Parses cleanly, like any lexicographic range; entry 30 refuses it at
    # emit time instead (see tests/emitter/test_emit_default_subpath.py).
    # Without a default this was an unrecognized prefix and the whole thing
    # demoted to default-field text.
    result = parse("notes:[a TO b]", dreg)
    assert not result.diagnostics
    assert result.ast == ast.TermRange(
        field=FieldRef("notes", "note"), lo="a", hi="b", incl_lo=True, incl_hi=True
    )


def test_free_text_tokens_ignores_a_defaulted_bare_json_leaf(dreg: FieldRegistry) -> None:
    # A defaulted bare mention is a subpath leaf, and subpath leaves never
    # contribute free text; only the content word does.
    result = parse("notes:secret report", dreg)
    assert not result.diagnostics
    assert free_text_tokens(result.ast, registry=dreg, fields=("content", "title")) == ("report",)


def test_free_text_tokens_still_refuses_a_defaulted_json_field(dreg: FieldRegistry) -> None:
    # Asking for a JSON field's free text is a host configuration error
    # either way. With a default it trips the subpath branch rather than the
    # bare-JSON one, so the message must name what the bare name *resolves
    # to* ("notes" -> "notes.note") instead of calling "notes" a subpath,
    # which it is not.
    result = parse("report", dreg)
    with pytest.raises(ValueError, match=r"'notes', which resolves to JSON subpath 'notes\.note'"):
        free_text_tokens(result.ast, registry=dreg, fields=("notes",))
    with pytest.raises(ValueError, match="a JSON field"):
        free_text_tokens(result.ast, registry=dreg, fields=("cf",))
