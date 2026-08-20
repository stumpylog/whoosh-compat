"""A leading ``-`` is not a negation operator in this grammar.

Real whoosh's default plugin list has no bare-minus negation plugin either
(``-term`` negation is a query-string convention some other systems use, not
part of whoosh's own default ``QueryParser`` grammar); ``OperatorsPlugin``'s
``NotGroup``/``PrefixOperator`` only fires for the ``NOT`` keyword. A leading
``-`` on an unfielded term is therefore just a character in the term text
(separator stripping, if any, is an analyzer-time decision, not a parse-time
one -- ``Term.analyzed`` stays ``False`` here). A leading ``-`` immediately
before a field name (``-field:value``) does not attach to the field clause at
all: the parser splits it into its own bare, un-negated token.
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


def test_unfielded_leading_dash_is_kept_as_literal_term_text(reg: FieldRegistry) -> None:
    t = parse("-taxes", reg)
    assert isinstance(t, ast.Term)
    assert t.text == "-taxes"


def test_unfielded_leading_dash_does_not_negate_a_conjoined_term(reg: FieldRegistry) -> None:
    t = parse("invoice -taxes", reg)
    assert isinstance(t, ast.And)
    assert not any(isinstance(c, ast.Not) for c in t.children)


def test_fielded_leading_dash_splits_off_as_its_own_positive_token(reg: FieldRegistry) -> None:
    # The "-" detaches entirely from "title:beta" and becomes its own bare
    # token on the default field, AND-ed alongside the (un-negated) field
    # clause -- the negation is dropped, not inverted onto the field clause.
    t = parse("-title:beta", reg)
    assert isinstance(t, ast.And)
    assert not any(isinstance(c, ast.Not) for c in t.children)
    dash_terms = [
        c
        for c in t.children
        if isinstance(c, ast.Term) and c.field == FieldRef("content") and c.text == "-"
    ]
    beta_terms = [
        c
        for c in t.children
        if isinstance(c, ast.Term) and c.field == FieldRef("title") and c.text == "beta"
    ]
    assert len(dash_terms) == 1
    assert len(beta_terms) == 1


def test_fielded_leading_dash_clause_still_conjoins_with_the_rest(reg: FieldRegistry) -> None:
    # Discriminating shape: if the dash-fielded clause were dropped from the
    # query entirely (rather than kept as an ordinary positive requirement),
    # "title:alpha -title:beta" would reduce to just "title:alpha". It does
    # not: the (un-negated) beta requirement is still present as a sibling.
    t = parse("title:alpha -title:beta", reg)
    assert isinstance(t, ast.And)
    field_terms = {
        (c.field, c.text) for c in t.children if isinstance(c, ast.Term) and c.field is not None
    }
    assert (FieldRef("title"), "alpha") in field_terms
    assert (FieldRef("title"), "beta") in field_terms
