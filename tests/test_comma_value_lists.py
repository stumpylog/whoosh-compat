"""``comma_values`` value-list grammar: what a comma means inside a field's
value, versus between two clauses.

``FieldSpec(comma_values=True)`` is what makes a comma inside a value split
into a conjunctive value list (``tag:foo,bar`` -> ``tag:foo AND tag:bar``,
already pinned by ``test_comma_values`` in test_parser_basics.py). What is
not pinned anywhere yet is the two edges around that feature:

- an *ordinary* (non-``comma_values``) field must not treat an unquoted
  comma as a list separator at all -- it stays part of the literal value.
- a comma immediately before a *known field name* is a clause separator,
  not a value-list delimiter, on both comma_values and ordinary fields
  alike, since ``field:value,other_field:value`` is ambiguous between "one
  value list containing the string 'other_field:value'" and "two clauses"
  and the grammar resolves it as two clauses.
"""

from __future__ import annotations

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry


def parse(q: str, reg: FieldRegistry) -> ast.Node:
    result = wc.parse(q, registry=reg, default_fields=["content"])
    assert not result.diagnostics
    return result.ast


def test_ordinary_field_keeps_an_unquoted_comma_as_literal_text(reg: FieldRegistry) -> None:
    # "title" does not declare comma_values: unlike "tag" (comma_values=True,
    # see test_comma_values), a comma in its value is not a list delimiter.
    t = parse("title:foo,bar", reg)
    assert t == ast.Term(field=FieldRef("title"), text="foo,bar")


def test_comma_before_a_known_field_separates_a_comma_values_clause(
    reg: FieldRegistry,
) -> None:
    # A value-list reading of "tag:foo,added:2020" would demand a tag
    # literally named "added:2020". The grammar instead treats "added:" as
    # the start of a new clause.
    t = parse("tag:foo,added:2020-01-01", reg)
    assert isinstance(t, ast.And)
    assert ast.Term(field=FieldRef("tag"), text="foo") in t.children
    assert any(isinstance(c, ast.DateRange) and c.field == FieldRef("added") for c in t.children)
    # The tag clause must carry only "foo", not a two-element value list:
    # "added:2020-01-01" is a separate clause, not a second tag value.
    tag_terms = [c for c in t.children if isinstance(c, ast.Term) and c.field == FieldRef("tag")]
    assert tag_terms == [ast.Term(field=FieldRef("tag"), text="foo")]


def test_comma_before_a_known_field_separates_an_ordinary_field_clause_too(
    reg: FieldRegistry,
) -> None:
    # Same clause-separator reading on a non-comma_values field: this is not
    # a literal "Alpha,tag:foo" (contrast with the fully-literal case above,
    # which has no field name after the comma to trigger the split).
    t = parse("title:Alpha,tag:foo", reg)
    assert isinstance(t, ast.And)
    assert ast.Term(field=FieldRef("tag"), text="foo") in t.children
    title_terms = [
        c for c in t.children if isinstance(c, ast.Term) and c.field == FieldRef("title")
    ]
    assert len(title_terms) == 1
    assert title_terms[0].text != "Alpha,tag:foo"


def test_single_quoted_comma_values_are_never_split_even_before_a_known_field(
    reg: FieldRegistry,
) -> None:
    # Quoting protects the comma from both readings at once: single value,
    # literal text, not a clause boundary either.
    t = parse("tag:'foo,added:2020-01-01'", reg)
    assert t == ast.Term(field=FieldRef("tag"), text="foo,added:2020-01-01")


def test_double_quoted_comma_values_are_never_split_even_before_a_known_field(
    reg: FieldRegistry,
) -> None:
    # The more common user spelling of "quote the value" takes a different
    # grammar path than the single-quoted case above (a double-quoted value
    # is tagged by PhrasePlugin and becomes a Phrase, not a Term; the
    # single-quoted case above is tagged by SingleQuotePlugin instead), but
    # the outcome the comma_values feature cares about is the same: the
    # comma stays inside one value, and no clause split happens even though
    # "added:" follows it.
    t = parse('tag:"foo,added:2020-01-01"', reg)
    assert t == ast.Phrase(field=FieldRef("tag"), text="foo,added:2020-01-01")
