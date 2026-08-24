"""Unit tests for whoosh_compat.parser.plugins that don't go through the
full ``whoosh_compat.parse()`` entry point. These exercise plugin
construction, tagger regexes, and filter functions directly against stub
parsers/nodes, in isolation from the rest of the parsing pipeline.

Full end-to-end parsing behavior is covered by tests/test_parser_basics.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser import plugins
from whoosh_compat.parser import priorities
from whoosh_compat.parser import syntax


class StubParser:
    """Minimal stand-in for a ``whoosh_compat.parser.default.QueryParser``."""

    fieldname: str | None = None

    def __init__(self, registry: FieldRegistry | None = None) -> None:
        self.registry = registry
        self.reports: list[Diagnostic] = []
        self.wildcard_calls: list[tuple[Any, ...]] = []
        self.prefix_calls: list[tuple[Any, ...]] = []

    def group(self) -> syntax.GroupNode:
        return syntax.OrGroup()

    def field_ref(self, fieldname: Any) -> FieldRef | None:
        """Mirrors ``QueryParser.field_ref``: resolve against the stub's own
        registry when given one, otherwise just wrap the raw name.
        """
        if fieldname is None:
            return None
        if self.registry is not None:
            ref = self.registry.make_ref(fieldname)
            if ref is not None:
                return ref
        return FieldRef(fieldname)

    def term_query(self, fieldname: Any, text: Any, boost: float = 1.0, **kw: Any) -> ast.Node:
        n = ast.Term(field=self.field_ref(fieldname), text=text)
        return ast.Boosted(n, boost) if boost != 1.0 else n

    def wildcard_query(self, fieldname: Any, text: Any, boost: float = 1.0, **kw: Any) -> ast.Node:
        self.wildcard_calls.append((fieldname, text, boost))
        return ast.Wildcard(field=self.field_ref(fieldname), pattern=text)

    def prefix_query(self, fieldname: Any, text: Any, boost: float = 1.0, **kw: Any) -> ast.Node:
        self.prefix_calls.append((fieldname, text, boost))
        return ast.Prefix(field=self.field_ref(fieldname), text=text)

    def range_query(
        self,
        fieldname: Any,
        start: Any,
        end: Any,
        startexcl: bool,
        endexcl: bool,
        boost: float = 1.0,
        node: Any = None,
    ) -> ast.Node:
        return ast.TermRange(
            field=self.field_ref(fieldname),
            lo=start,
            hi=end,
            incl_lo=not startexcl,
            incl_hi=not endexcl,
        )

    def report(self, diagnostic: Diagnostic) -> None:
        self.reports.append(diagnostic)


@pytest.fixture
def registry() -> FieldRegistry:
    return FieldRegistry(
        [
            FieldSpec("content", FieldKind.TEXT),
            FieldSpec("title", FieldKind.TEXT),
            FieldSpec("document_type", FieldKind.TEXT, aliases=("type",)),
            FieldSpec("tag", FieldKind.KEYWORD, comma_values=True),
            FieldSpec("asn", FieldKind.U64, fast=True),
            FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user")),
        ]
    )


# --- Plugin construction / taggers()/filters() wiring ----------------------


def test_whitespace_plugin_taggers_and_filters() -> None:
    plugin = plugins.WhitespacePlugin()
    taggers = plugin.taggers(None)
    assert taggers == [(plugin, priorities.TAG_WHITESPACE)]
    filters = plugin.filters(None)
    assert filters == [(plugin.remove_whitespace, priorities.FILTER_WHITESPACE_REMOVE)]


def test_boost_plugin_filters_priorities() -> None:
    plugin = plugins.BoostPlugin()
    fs = dict(plugin.filters(None))
    assert fs[plugin.clean_boost] == priorities.FILTER_BOOSTS_PRE
    assert fs[plugin.do_boost] == priorities.FILTER_BOOSTS_POST


def test_wildcard_plugin_filter_priority() -> None:
    plugin = plugins.WildcardPlugin()
    assert plugin.filters(None) == [(plugin.do_wildcards, priorities.FILTER_WILDCARDS)]


def test_fields_plugin_filter_priority() -> None:
    plugin = plugins.FieldsPlugin()
    assert plugin.filters(None) == [(plugin.do_fieldnames, priorities.FILTER_FIELDNAMES)]


def test_comma_values_plugin_filter_priority() -> None:
    plugin = plugins.CommaValuesPlugin()
    assert plugin.filters(None) == [(plugin.do_comma_values, priorities.FILTER_COMMA_VALUES)]


def test_field_alias_plugin_filter_priority_and_reverse_map() -> None:
    plugin = plugins.FieldAliasPlugin({"document_type": ["type", "kind"]})
    assert plugin.filters(None) == [(plugin.do_aliases, priorities.FILTER_ALIASES)]
    assert plugin.reverse == {"type": "document_type", "kind": "document_type"}


def test_deleted_plugins_are_gone() -> None:
    for name in (
        "PrefixPlugin",
        "RegexPlugin",
        "FuzzyTermPlugin",
        "FunctionPlugin",
        "SequencePlugin",
        "PlusMinusPlugin",
        "GtLtPlugin",
        "CopyFieldPlugin",
        "PseudoFieldPlugin",
    ):
        assert not hasattr(plugins, name)


# --- Tagger regex behavior --------------------------------------------------


def test_whitespace_tagger_matches() -> None:
    tagger = plugins.WhitespacePlugin()
    node = tagger.match(None, "a   b", 1)
    assert isinstance(node, syntax.Whitespace)
    assert (node.startchar, node.endchar) == (1, 4)


def test_single_quote_tagger_marks_is_quoted() -> None:
    tagger = plugins.SingleQuotePlugin()
    node = tagger.match(None, "'foo,bar'", 0)
    assert isinstance(node, syntax.WordNode)
    assert node.text == "foo,bar"
    assert getattr(node, "is_quoted", False) is True


def test_word_node_default_is_not_quoted() -> None:
    assert getattr(syntax.WordNode("x"), "is_quoted", False) is False


def test_wildcard_tagger_matches_star() -> None:
    tagger = plugins.WildcardPlugin()
    node = tagger.match(None, "produ*name", 5)
    assert isinstance(node, plugins.WildcardPlugin.WildcardNode)
    assert node.text == "*"


def test_boost_tagger_matches_number() -> None:
    tagger = plugins.BoostPlugin()
    node = tagger.match(None, "aaa^2.5", 3)
    assert isinstance(node, plugins.BoostPlugin.BoostNode)
    assert node.boost == 2.5


def test_boost_tagger_invalid_number_becomes_word() -> None:
    tagger = plugins.BoostPlugin()
    node = tagger.match(None, "^", 0)
    assert isinstance(node, syntax.WordNode)


def test_every_tagger_matches_star_colon_star() -> None:
    tagger = plugins.EveryPlugin()
    node = tagger.match(None, "*:*", 0)
    assert isinstance(node, plugins.EveryPlugin.EveryNode)


def test_fields_plugin_tagger_matches_dotted_name() -> None:
    plugin = plugins.FieldsPlugin()
    tagger = plugin.FieldnameTagger(plugin.expr)
    node = tagger.match(None, "notes.user:foo", 0)
    assert isinstance(node, syntax.FieldnameNode)
    assert node.fieldname == "notes.user"


def test_range_tagger_matches() -> None:
    plugin = plugins.RangePlugin()
    tagger = plugin.RangeTagger(plugin.expr, plugin.excl_start, plugin.excl_end)
    node = tagger.match(None, "[a TO b]", 0)
    assert isinstance(node, syntax.RangeNode)
    assert (node.start, node.end) == ("a", "b")


@pytest.mark.parametrize(
    ("text", "expected_start", "expected_end"),
    [
        pytest.param("[TO today]", None, "today", id="no-leading-space-bound-word-starts-with-to"),
        pytest.param("[TO tomorrow]", None, "tomorrow", id="bound-word-tomorrow"),
        pytest.param("[TO total]", None, "total", id="bound-word-total"),
        pytest.param("[into TO 5]", "into", "5", id="start-value-contains-to-substring"),
        # Unaffected controls: the leading-space spelling already worked, and
        # ordinary ranges (no "to"-prefixed word adjacent to the separator)
        # must keep working identically.
        pytest.param("[ TO today]", "", "today", id="leading-space-already-worked"),
        pytest.param("[1 TO 5]", "1", "5", id="ordinary-numeric-range"),
        pytest.param("[dec to feb]", "dec", "feb", id="ordinary-month-range"),
        pytest.param("[town TO city]", "town", "city", id="start-value-is-a-to-prefixed-word"),
    ],
)
def test_range_tagger_to_separator_requires_word_boundary(
    text: str, expected_start: str | None, expected_end: str | None
) -> None:
    """A bound value that begins with, or itself is, a "to"-prefixed word
    (``today``, ``total``, ``into``) must not be mistaken for the "TO"
    separator token: the separator is only recognized at a word boundary,
    never mid-word. Regression coverage for the mis-tokenization behind
    ``created:[TO today]`` (no leading space) parsing whoosh-compat's own
    literal "TO" as the start-bound value and "day" as the end.
    """
    plugin = plugins.RangePlugin()
    tagger = plugin.RangeTagger(plugin.expr, plugin.excl_start, plugin.excl_end)
    node = tagger.match(None, text, 0)
    assert isinstance(node, syntax.RangeNode)
    assert (node.start, node.end) == (expected_start, expected_end)


def test_range_tagger_no_to_at_all_is_not_a_range() -> None:
    """``[total 5]`` has no genuine "TO" separator anywhere (only the
    non-word-boundary "to" inside "total"), so it must not be tagged as a
    range at all, rather than silently mis-parsing to a garbage
    start/end split the way the un-hardened regex did (``start=None,
    end='tal 5'``, treating the literal chars "to" inside "total" as the
    separator).
    """
    plugin = plugins.RangePlugin()
    tagger = plugin.RangeTagger(plugin.expr, plugin.excl_start, plugin.excl_end)
    assert tagger.match(None, "[total 5]", 0) is None


def test_phrase_tagger_captures_slop() -> None:
    plugin = plugins.PhrasePlugin()
    tagger = plugin.PhraseTagger(plugin.expr)
    node = tagger.match(None, '"exact words"~3', 0)
    assert isinstance(node, plugins.PhrasePlugin.PhraseNode)
    assert node.text == "exact words"
    assert node.slop == 3


def test_group_plugin_brackets() -> None:
    plugin = plugins.GroupPlugin()
    (opentag, _), (closetag, _) = plugin.taggers(None)
    assert isinstance(opentag.match(None, "(", 0), plugins.GroupPlugin.OpenBracket)
    assert isinstance(closetag.match(None, ")", 0), plugins.GroupPlugin.CloseBracket)


# --- do_fieldnames dotted-name gating --------------------------------------


def test_do_fieldnames_known_plain_field(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("title", "title:")
    word = syntax.WordNode("aaa")
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname == "title"
    assert result[0].text == "aaa"


def test_do_fieldnames_known_dotted_json_path(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("notes.user", "notes.user:")
    word = syntax.WordNode("trenton")
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname == "notes.user"


def test_do_fieldnames_unknown_dotted_subpath_demotes_and_merges(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("notes.bogus", "notes.bogus:")
    word = syntax.WordNode("x")
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname is None
    assert result[0].text == "notes.bogus:x"


def test_do_fieldnames_unknown_field_merges_into_url(registry: FieldRegistry) -> None:
    # "http://example.com" tags as FieldnameNode("http", "http:") followed by
    # a WordNode("//example.com"); since "http" isn't a registered field the
    # colon text must be merged back onto the following node.
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("http", "http:")
    word = syntax.WordNode("//example.com")
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname is None
    assert result[0].text == "http://example.com"


def test_do_fieldnames_no_registry_accepts_everything() -> None:
    parser = StubParser(registry=None)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("nope", "nope:")
    word = syntax.WordNode("x")
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname == "nope"


# --- do_fieldnames span/text consistency for demoted fieldname text --------
#
# The low-level tagger (QueryParser.tag) treats any "word:"-looking run
# anywhere in the query as a candidate field boundary, so an unquoted value
# containing a colon (an RFC3339 timestamp, "http://...") gets split into a
# FieldnameNode candidate plus surrounding text at the tagging stage, before
# any filter runs. do_fieldnames demotes a candidate that isn't a real field
# by prepending its .original back onto the following node's .text -- these
# tests pin two invariants that demotion must hold, both reachable from a
# single unquoted value and both silently broken before this fix (inherited
# verbatim from upstream whoosh's identical do_fieldnames, confirmed against
# the vendored oracle): the merged node's span must widen to cover the
# prepended text, and if the value contains a SECOND colon in a row, the
# first candidate's text must not be discarded entirely.


def test_do_fieldnames_demoted_span_widens_to_cover_merged_text(
    registry: FieldRegistry,
) -> None:
    """A single demoted FieldnameNode merging into the next node must widen
    that node's startchar back to the FieldnameNode's own start, not leave
    it pointing only at the following node's original position.
    """
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn = syntax.FieldnameNode("nope", "nope:")
    fn.startchar, fn.endchar = 0, 5
    word = syntax.WordNode("x")
    word.startchar, word.endchar = 5, 6
    group = syntax.AndGroup([fn, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].text == "nope:x"
    assert (result[0].startchar, result[0].endchar) == (0, 6)


def test_do_fieldnames_consecutive_demoted_candidates_keep_all_text(
    registry: FieldRegistry,
) -> None:
    """Two rejected FieldnameNode candidates in a row (e.g. from
    "2026-08-04T10:30:00Z", where "01T00:" and "00:" both look like field
    boundaries) must both survive into the final merged text, in order and
    with nothing dropped -- not just the immediately preceding one.
    """
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn1 = syntax.FieldnameNode("01T00", "01T00:")
    fn1.startchar, fn1.endchar = 0, 6
    fn2 = syntax.FieldnameNode("00", "00:")
    fn2.startchar, fn2.endchar = 6, 9
    word = syntax.WordNode("00Z")
    word.startchar, word.endchar = 9, 12
    group = syntax.AndGroup([fn1, fn2, word])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].text == "01T00:00:00Z"
    assert (result[0].startchar, result[0].endchar) == (0, 12)


def test_do_fieldnames_consecutive_demoted_candidates_at_end_of_group(
    registry: FieldRegistry,
) -> None:
    """The same accumulation must also happen when the run of rejected
    candidates is the last thing in the group (the final flush path, not
    the merge-into-next-node path).
    """
    parser = StubParser(registry)
    plugin = plugins.FieldsPlugin()
    fn1 = syntax.FieldnameNode("a", "a:")
    fn1.startchar, fn1.endchar = 0, 2
    fn2 = syntax.FieldnameNode("b", "b:")
    fn2.startchar, fn2.endchar = 2, 4
    group = syntax.AndGroup([fn1, fn2])
    result = plugin.do_fieldnames(parser, group)
    assert len(result) == 1
    assert result[0].fieldname is None
    assert result[0].text == "a:b:"
    assert (result[0].startchar, result[0].endchar) == (0, 4)


# --- CommaValuesPlugin -------------------------------------------------------


def test_comma_values_splits_unquoted_field(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.CommaValuesPlugin()
    node = syntax.WordNode("foo,bar")
    node.set_fieldname("tag", override=True)
    group = syntax.AndGroup([node])
    result = plugin.do_comma_values(parser, group)
    assert len(result) == 1
    sub = result[0]
    assert isinstance(sub, syntax.AndGroup)
    assert [n.text for n in sub] == ["foo", "bar"]  # type: ignore[attr-defined]
    assert all(n.fieldname == "tag" for n in sub)


def test_comma_values_leaves_quoted_node_intact(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.CommaValuesPlugin()
    node = plugins.SingleQuotePlugin.QuotedWordNode("foo,bar")
    node.set_fieldname("tag", override=True)
    group = syntax.AndGroup([node])
    result = plugin.do_comma_values(parser, group)
    assert list(result) == [node]


def test_comma_values_ignores_field_without_comma_values(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.CommaValuesPlugin()
    node = syntax.WordNode("a,b")
    node.set_fieldname("title", override=True)
    group = syntax.AndGroup([node])
    result = plugin.do_comma_values(parser, group)
    assert list(result) == [node]


def test_comma_values_drops_empty_parts(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    plugin = plugins.CommaValuesPlugin()
    node = syntax.WordNode("foo,,bar,")
    node.set_fieldname("tag", override=True)
    group = syntax.AndGroup([node])
    result = plugin.do_comma_values(parser, group)
    sub = result[0]
    assert [n.text for n in sub] == ["foo", "bar"]


# --- WildcardPlugin rewrite + node query hooks ------------------------------


def test_do_wildcards_rewrites_trailing_star_to_prefix() -> None:
    plugin = plugins.WildcardPlugin()
    word = syntax.WordNode("produ")
    star = plugins.WildcardPlugin.WildcardNode("*")
    group = syntax.AndGroup([word, star])
    result = plugin.do_wildcards(None, group)
    assert len(result) == 1
    assert isinstance(result[0], plugins.WildcardPlugin.PrefixNode)
    assert result[0].text == "produ"


def test_do_wildcards_keeps_mid_pattern_as_wildcard() -> None:
    plugin = plugins.WildcardPlugin()
    a = syntax.WordNode("produ")
    star = plugins.WildcardPlugin.WildcardNode("*")
    b = syntax.WordNode("name")
    group = syntax.AndGroup([a, star, b])
    result = plugin.do_wildcards(None, group)
    assert len(result) == 1
    assert isinstance(result[0], plugins.WildcardPlugin.WildcardNode)
    assert result[0].text == "produ*name"


def test_wildcard_node_query_calls_parser_hook(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    node = plugins.WildcardPlugin.WildcardNode("produ*name")
    node.set_fieldname("title", override=True)
    node.query(parser)
    assert parser.wildcard_calls == [("title", "produ*name", 1.0)]


def test_prefix_node_query_calls_parser_hook(registry: FieldRegistry) -> None:
    parser = StubParser(registry)
    node = plugins.WildcardPlugin.PrefixNode("produ")
    node.set_fieldname("title", override=True)
    node.query(parser)
    assert parser.prefix_calls == [("title", "produ", 1.0)]


# --- EveryPlugin / PhrasePlugin build AST directly --------------------------


def test_every_node_query_is_direct_ast() -> None:
    node = plugins.EveryPlugin.EveryNode()
    parser = StubParser(None)
    assert node.query(parser) == ast.Every(field=None)


def test_phrase_node_query_is_direct_ast() -> None:
    node = plugins.PhrasePlugin.PhraseNode("exact words", textstartchar=0, slop=3)
    node.set_fieldname("title", override=True)
    parser = StubParser(None)
    assert node.query(parser) == ast.Phrase(field=FieldRef("title"), text="exact words", slop=3)


def test_phrase_node_query_applies_boost() -> None:
    node = plugins.PhrasePlugin.PhraseNode("exact words", textstartchar=0, slop=1)
    node.set_fieldname("title", override=True)
    node.set_boost(2.0)
    parser = StubParser(None)
    q = node.query(parser)
    assert q == ast.Boosted(ast.Phrase(field=FieldRef("title"), text="exact words", slop=1), 2.0)


# --- FieldAliasPlugin --------------------------------------------------------


def test_field_alias_plugin_rewrites_fieldname() -> None:
    plugin = plugins.FieldAliasPlugin({"document_type": ["type"]})
    node = syntax.WordNode("invoice")
    node.set_fieldname("type", override=True)
    group = syntax.AndGroup([node])
    result = plugin.do_aliases(None, group)
    assert result[0].fieldname == "document_type"
