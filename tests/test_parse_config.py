"""Host-configuration validation at parse() entry.

The registry's own philosophy is that a misconfiguration raises at
construction, not at query time; these tests cover the same bar applied to
the configuration a host passes to parse() itself: default_fields and
field_boosts.
"""

import pytest

import whoosh_compat as wc
from whoosh_compat import ast
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry


def test_empty_default_fields_raises(reg: FieldRegistry) -> None:
    with pytest.raises(ValueError, match="default_fields"):
        wc.parse("hello", registry=reg, default_fields=[])


def test_unknown_default_field_raises(reg: FieldRegistry) -> None:
    with pytest.raises(ValueError, match="bogus"):
        wc.parse("hello", registry=reg, default_fields=["content", "bogus"])


def test_alias_default_field_is_accepted(reg: FieldRegistry) -> None:
    # An alias is a valid designation, not a host bug: it already resolves
    # correctly downstream (term_query -> field_ref -> make_ref), so this is
    # not one of the cases parse()'s config validation rejects.
    r = wc.parse("hello", registry=reg, default_fields=["type"])
    assert r.ast == ast.Term(field=FieldRef("document_type"), text="hello")


def test_json_subpath_default_field_is_accepted(reg: FieldRegistry) -> None:
    r = wc.parse("hello", registry=reg, default_fields=["notes.user"])
    assert r.ast == ast.Term(field=FieldRef("notes", "user"), text="hello")


def test_unresolvable_field_boost_key_raises(reg: FieldRegistry) -> None:
    with pytest.raises(ValueError, match="bogus"):
        wc.parse(
            "hello",
            registry=reg,
            default_fields=["content"],
            field_boosts={"bogus": 2.0},
        )


def test_field_boost_key_resolves_alias_to_canonical_field(reg: FieldRegistry) -> None:
    # {"type": 2.0} where "type" aliases "document_type": resolves to the
    # canonical field and applies the boost, consistent with alias handling
    # everywhere else in the library (decided in the issue).
    r = wc.parse(
        "hello",
        registry=reg,
        default_fields=["content", "document_type"],
        field_boosts={"type": 2.0},
    )
    assert r.ast == ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="hello"),
            ast.Boosted(ast.Term(field=FieldRef("document_type"), text="hello"), 2.0),
        )
    )


def test_field_boost_key_by_canonical_name_still_works(reg: FieldRegistry) -> None:
    r = wc.parse(
        "hello",
        registry=reg,
        default_fields=["content", "document_type"],
        field_boosts={"document_type": 2.0},
    )
    assert r.ast == ast.Or(
        children=(
            ast.Term(field=FieldRef("content"), text="hello"),
            ast.Boosted(ast.Term(field=FieldRef("document_type"), text="hello"), 2.0),
        )
    )
