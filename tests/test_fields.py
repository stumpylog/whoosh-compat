"""Tests for fields.py: FieldSpec and FieldRegistry."""

import dataclasses

import pytest

from whoosh_compat.fields import ExistsStrategy
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import resolve_exists_strategy

# ============================================================================
# Test FieldRef: plain data carrier, canonical string form
# ============================================================================


def test_field_ref_str_plain() -> None:
    """str() of a plain FieldRef (no subpath) is just its name."""
    assert str(FieldRef("created")) == "created"


def test_field_ref_str_json_subpath() -> None:
    """str() of a JSON-subpath FieldRef is the canonical dotted form."""
    assert str(FieldRef("notes", "user")) == "notes.user"


def test_field_ref_equality() -> None:
    """FieldRef is a plain, comparable data carrier."""
    assert FieldRef("notes", "user") == FieldRef("notes", "user")
    assert FieldRef("notes", "user") != FieldRef("notes", "admin")
    assert FieldRef("notes") != FieldRef("notes", "user")


# ============================================================================
# Test resolve(): canonical names, aliases, and JSON subpaths
# ============================================================================


def test_resolve_by_canonical_name() -> None:
    """resolve() finds a spec by its canonical name, wrapped in a
    ResolvedField with json_path=None for a plain field.
    """
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("title"))
    assert resolved is not None
    assert resolved.spec is spec
    assert resolved.json_path is None
    assert resolved.is_subpath is False
    assert resolved.dotted_name == "title"


def test_resolve_by_alias() -> None:
    """resolve() finds a spec by its alias.

    A ``FieldRef`` naming an alias directly (rather than the alias having
    already been canonicalized by ``make_ref``) still resolves: ``resolve``
    itself does exact by-name lookup, alias or canonical, since
    ``FieldRegistry`` indexes both under the same table.
    """
    spec = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading", "subject"))
    registry = FieldRegistry([spec])
    heading = registry.resolve(FieldRef("heading"))
    subject = registry.resolve(FieldRef("subject"))
    assert heading is not None
    assert subject is not None
    assert heading.spec is spec
    assert subject.spec is spec


def test_resolve_unknown_returns_none() -> None:
    """resolve() returns None for unknown names."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.resolve(FieldRef("unknown")) is None


def test_resolve_json_subpath() -> None:
    """resolve() returns the JSON field's spec for a valid subpath ref,
    with the subpath carried on the returned ResolvedField.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user", "admin"))
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("notes", "user"))
    assert resolved is not None
    assert resolved.spec is spec
    assert resolved.json_path == "user"
    assert resolved.is_subpath is True
    assert resolved.dotted_name == "notes.user"


def test_resolve_json_invalid_subpath_returns_none() -> None:
    """resolve() returns None when the ref's subpath isn't registered."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    assert registry.resolve(FieldRef("notes", "body")) is None


def test_resolve_subpath_on_non_json_spec_returns_none() -> None:
    """resolve() returns None when a subpath ref targets a non-JSON field."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.resolve(FieldRef("title", "user")) is None


def test_resolve_plain_ref_on_json_spec_returns_the_json_spec() -> None:
    """resolve() on a plain (no-subpath) ref to a JSON field still returns
    that field's spec; whether a *term* can be built against it without a
    subpath is an emitter-level question (see test_emit_terms.py), not a
    registry-resolution one.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("notes"))
    assert resolved is not None
    assert resolved.spec is spec
    assert resolved.json_path is None


# ============================================================================
# Test make_ref(): the parse-boundary raw-string -> FieldRef resolver
# ============================================================================


def test_make_ref_canonical_name() -> None:
    """make_ref() resolves a canonical name to a plain FieldRef."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.make_ref("title") == FieldRef("title")


def test_make_ref_alias_canonicalizes() -> None:
    """make_ref() canonicalizes an alias to the spec's own name."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    registry = FieldRegistry([spec])
    assert registry.make_ref("heading") == FieldRef("title")


def test_make_ref_json_subpath() -> None:
    """make_ref() splits on the first dot and validates the subpath."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user", "admin"))
    registry = FieldRegistry([spec])
    assert registry.make_ref("notes.user") == FieldRef("notes", "user")


def test_make_ref_json_subpath_with_extra_dots() -> None:
    """make_ref() only splits on the first dot; the subpath itself may
    contain further dots, matched verbatim against spec.subpaths.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user.name", "admin.role"))
    registry = FieldRegistry([spec])
    assert registry.make_ref("notes.user.name") == FieldRef("notes", "user.name")


def test_make_ref_json_invalid_subpath_returns_none() -> None:
    """make_ref() returns None for a dotted name whose subpath isn't
    registered, so the existing FieldsPlugin demotion path (unknown field
    text merges back into the following word) is unchanged.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    assert registry.make_ref("notes.body") is None


def test_make_ref_dotted_name_on_non_json_spec_returns_none() -> None:
    """make_ref() returns None for a dotted name whose base isn't a JSON
    field (it isn't reinterpreted as anything else).
    """
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.make_ref("title.user") is None


def test_make_ref_json_field_bare_name_is_unrecognized() -> None:
    """make_ref() on a JSON field's bare name (no dot, no subpath) returns
    None rather than a plain ref (issue #11): a JSON field addressed
    without a subpath has no way to emit, so treating it as known here
    would let e.g. "notes:foo" parse cleanly and then raise at emit time.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    assert registry.make_ref("notes") is None


def test_is_bare_json_field_true_for_json_bare_name_or_alias() -> None:
    """is_bare_json_field() is the narrow query FieldsPlugin.do_fieldnames
    uses (issue #11, reopened) to detect the one shape a bare JSON field
    name is still allowed to reach as a recognized field prefix: a lone
    "*" existence check. It must say True for the canonical name and any
    alias, independent of make_ref()'s own (deliberately stricter) answer.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",), aliases=("n",))
    registry = FieldRegistry([spec])
    assert registry.is_bare_json_field("notes") is True
    assert registry.is_bare_json_field("n") is True


def test_is_bare_json_field_false_for_non_json_or_unknown() -> None:
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.is_bare_json_field("title") is False
    assert registry.is_bare_json_field("nonexistent") is False


def test_make_ref_unknown_returns_none() -> None:
    """make_ref() returns None for a name that resolves neither as a plain
    field nor as a JSON subpath.
    """
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.make_ref("unknown") is None


def test_make_ref_registered_dotted_plain_field_wins_over_json_interpretation() -> None:
    """A registered *plain* field whose own name contains a dot resolves
    directly, exactly, rather than being reinterpreted as ``base.subpath``
    against some other JSON field. This is the canary case a previous
    repair broke: see DIVERGENCES.md and
    tests/emitter/test_emit_terms.py's dotted-plain-field tests for the
    emitter-level behavior this registry-level guarantee supports.
    """
    dotted = FieldSpec(name="field.with.dots", kind=FieldKind.TEXT)
    json_spec = FieldSpec(name="field", kind=FieldKind.JSON, subpaths=("with",))
    registry = FieldRegistry([dotted, json_spec])
    assert registry.make_ref("field.with.dots") == FieldRef("field.with.dots")


# ============================================================================
# Test __contains__: in operator
# ============================================================================


def test_contains_canonical_name() -> None:
    """in operator returns True for canonical name."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert "title" in registry


def test_contains_alias() -> None:
    """in operator returns True for alias."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    registry = FieldRegistry([spec])
    assert "heading" in registry


def test_contains_dotted_json_path() -> None:
    """in operator returns True for valid dotted JSON path."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user",),
    )
    registry = FieldRegistry([spec])
    assert "notes.user" in registry


def test_contains_invalid_path() -> None:
    """in operator returns False for invalid path."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert "unknown" not in registry


# ============================================================================
# Test __iter__: iteration over specs
# ============================================================================


def test_iter_specs() -> None:
    """Iteration yields FieldSpec objects in insertion order."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="date", kind=FieldKind.DATE, date_only=True)
    spec3 = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec1, spec2, spec3])

    specs = list(registry)
    assert specs == [spec1, spec2, spec3]


def test_iter_deduplicates() -> None:
    """Iteration yields unique specs only."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    spec2 = FieldSpec(name="date", kind=FieldKind.DATE, date_only=True)
    registry = FieldRegistry([spec1, spec2])

    specs = list(registry)
    assert len(specs) == 2
    assert spec1 in specs
    assert spec2 in specs


# ============================================================================
# Validation: Duplicate Names and Aliases
# ============================================================================


def test_validation_duplicate_canonical_names() -> None:
    """ValueError if two specs have the same canonical name; message says
    "duplicate canonical name" here, since that's genuinely what collided
    (contrast test_validation_duplicate_canonical_name_message_names_alias_collision,
    where the collision is with an alias instead).
    """
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="title", kind=FieldKind.KEYWORD)
    with pytest.raises(ValueError, match="duplicate canonical name"):
        FieldRegistry([spec1, spec2])


def test_validation_alias_collides_with_canonical() -> None:
    """ValueError if an alias collides with another spec's canonical name."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="body", kind=FieldKind.TEXT, aliases=("title",))
    with pytest.raises(ValueError, match="body"):
        FieldRegistry([spec1, spec2])


def test_validation_duplicate_aliases() -> None:
    """ValueError if two specs have the same alias."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    spec2 = FieldSpec(name="subject", kind=FieldKind.TEXT, aliases=("heading",))
    with pytest.raises(ValueError, match=r"subject|title"):
        FieldRegistry([spec1, spec2])


def test_validation_duplicate_canonical_name_message_names_alias_collision() -> None:
    """issue #21: when a later spec's canonical name collides with an
    *earlier* spec's alias (not its canonical name), the message must say
    so, not claim "duplicate canonical name" (which is what actually
    collided is an alias, a different, more confusing conflict to debug).
    """
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    spec2 = FieldSpec(name="heading", kind=FieldKind.TEXT)
    with pytest.raises(ValueError, match="alias") as excinfo:
        FieldRegistry([spec1, spec2])
    message = str(excinfo.value)
    assert "heading" in message
    assert "title" in message
    assert "duplicate canonical name" not in message


def test_validation_rejects_empty_canonical_name() -> None:
    """issue #21: an empty field name can never be typed in a query."""
    with pytest.raises(ValueError, match="empty"):
        FieldRegistry([FieldSpec(name="", kind=FieldKind.TEXT)])


def test_validation_rejects_empty_alias() -> None:
    with pytest.raises(ValueError, match="empty"):
        FieldRegistry([FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("",))])


def test_validation_rejects_duplicate_aliases_within_one_spec() -> None:
    """issue #21: two identical aliases on the *same* spec (as opposed to
    test_validation_duplicate_aliases's cross-spec case) previously slipped
    past the collision check entirely, since that check only compares
    against specs already registered, not a spec's own aliases tuple.
    """
    with pytest.raises(ValueError, match="heading"):
        FieldRegistry(
            [FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading", "heading"))]
        )


def test_validation_rejects_dotted_json_canonical_name() -> None:
    """issue #21 (narrowed by the follow-up status-update comment): a
    dotted canonical name is only actually unreachable for a JSON-kind
    field. make_ref's exact-match branch excludes JSON specifically (a
    bare JSON reference has no subpath and can't emit, issue #11), so a
    dotted JSON name falls through to dotted-name splitting, which looks
    up the text *before* the first dot as the field name -- never this
    field's own name -- so it (and every one of its subpaths) can never
    resolve through any query text at all.
    """
    with pytest.raises(ValueError, match=r"meta\.data"):
        FieldRegistry([FieldSpec(name="meta.data", kind=FieldKind.JSON, subpaths=("x",))])


def test_validation_rejects_dotted_json_alias() -> None:
    # A clean alias before the dotted one exercises the loop actually
    # scanning past a non-offending entry, not just checking the first.
    with pytest.raises(ValueError, match=r"heading\.sub"):
        FieldRegistry(
            [
                FieldSpec(
                    name="notes",
                    kind=FieldKind.JSON,
                    subpaths=("user",),
                    aliases=("clean", "heading.sub"),
                )
            ]
        )


def test_validation_dotted_plain_canonical_name_is_permitted() -> None:
    """A dotted canonical name on a *non*-JSON field is a deliberately
    supported, tested shape (tests/emitter/test_emit_terms.py's
    dotted-plain-field tests): make_ref's exact-match lookup finds it
    directly, so unlike the JSON case it is genuinely reachable. The
    follow-up status-update comment on issue #21 corrects the original,
    broader "reject all dotted canonical names" decision to this narrower
    JSON-only scope after confirming the plain-field case actually works.
    """
    registry = FieldRegistry([FieldSpec(name="field.with.dots", kind=FieldKind.TEXT)])
    assert registry.make_ref("field.with.dots") == FieldRef("field.with.dots")


def test_validation_dotted_plain_alias_is_permitted() -> None:
    registry = FieldRegistry(
        [FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading.sub",))]
    )
    assert registry.make_ref("heading.sub") == FieldRef("title")


def test_validation_boolean_exists_target_boolean_exists_kind_rejected() -> None:
    """issue #21: a BOOLEAN_EXISTS exists_target can never work, since a
    BOOLEAN_EXISTS field has no physical index column of its own to check
    "exists" against, no matter its fast/kind-resolved strategy. Previously
    only the *non-fast* case was rejected: a fast=True BOOLEAN_EXISTS
    target resolved a FAST_FIELD strategy (resolve_exists_strategy looks
    only at kind/fast, not at what physical column that implies), so it
    slipped through construction despite having no real column behind it.
    """
    spec1 = FieldSpec(name="has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="tag")
    spec2 = FieldSpec(name="tag", kind=FieldKind.TEXT)
    spec3 = FieldSpec(name="has_has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="has_tag")
    with pytest.raises(ValueError, match="has_has_tag"):
        FieldRegistry([spec1, spec2, spec3])


def test_validation_boolean_exists_target_kind_rejected_regardless_of_list_order() -> None:
    """The rejection is a third pass over all fully-registered specs, so it
    must not depend on exists_target being listed *after* the field that
    references it: has_has_tag names has_tag as exists_target here while
    listed *before* it.
    """
    spec3 = FieldSpec(name="has_has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="has_tag")
    spec1 = FieldSpec(name="has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="tag")
    spec2 = FieldSpec(name="tag", kind=FieldKind.TEXT)
    with pytest.raises(ValueError, match="has_has_tag"):
        FieldRegistry([spec3, spec1, spec2])


def test_validation_boolean_exists_self_reference_rejected() -> None:
    with pytest.raises(ValueError, match="has_tag"):
        FieldRegistry(
            [FieldSpec(name="has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="has_tag")]
        )


def test_validation_boolean_exists_fast_self_reference_rejected() -> None:
    """The specific gap the follow-up status update flagged: fast=True on
    a self-targeting BOOLEAN_EXISTS spec resolved a FAST_FIELD strategy
    (fast=True always does, regardless of kind) and so previously
    constructed successfully despite being unable to ever work: a
    BOOLEAN_EXISTS field has no physical column for "fast" to mean
    anything about.
    """
    with pytest.raises(ValueError, match="has_tag"):
        FieldRegistry(
            [
                FieldSpec(
                    name="has_tag",
                    kind=FieldKind.BOOLEAN_EXISTS,
                    exists_target="has_tag",
                    fast=True,
                )
            ]
        )


def test_analyzer_on_kind_that_ignores_it_is_permitted() -> None:
    """issue #21, documented as permitted (not a validation gap): a host
    may reasonably share one FieldSpec factory across kinds, passing an
    analyzer that a non-TEXT/KEYWORD/JSON kind simply never calls.
    """
    FieldRegistry([FieldSpec(name="asn", kind=FieldKind.U64, analyzer=str.split)])


# ============================================================================
# Validation: BOOLEAN_EXISTS
# ============================================================================


def test_validation_boolean_exists_requires_target() -> None:
    """ValueError if BOOLEAN_EXISTS spec lacks exists_target."""
    spec = FieldSpec(name="has_attachment", kind=FieldKind.BOOLEAN_EXISTS)
    with pytest.raises(ValueError, match="has_attachment"):
        FieldRegistry([spec])


def test_validation_boolean_exists_target_must_exist() -> None:
    """ValueError if exists_target does not reference a registered spec."""
    spec = FieldSpec(
        name="has_attachment",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="attachment",
    )
    with pytest.raises(ValueError, match="has_attachment"):
        FieldRegistry([spec])


def test_validation_boolean_exists_target_unsupported_kind_rejected() -> None:
    """ValueError if exists_target has no resolvable 'exists' strategy:
    non-fast and not TEXT/KEYWORD. The error names the target field and the
    two remedies (mark it fast, or change its kind).
    """
    spec1 = FieldSpec(name="page_count", kind=FieldKind.U64)  # not fast, not TEXT/KEYWORD
    spec2 = FieldSpec(
        name="has_pages",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="page_count",
    )
    with pytest.raises(ValueError, match="has_pages") as excinfo:
        FieldRegistry([spec1, spec2])
    message = str(excinfo.value)
    assert "page_count" in message
    assert "fast=True" in message
    assert "TEXT or KEYWORD" in message


def test_validation_boolean_exists_target_text_is_valid() -> None:
    """BOOLEAN_EXISTS can target a non-fast TEXT field."""
    spec1 = FieldSpec(name="attachment", kind=FieldKind.TEXT)
    spec2 = FieldSpec(
        name="has_attachment",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="attachment",
    )
    registry = FieldRegistry([spec1, spec2])
    resolved = registry.resolve(FieldRef("has_attachment"))
    assert resolved is not None
    assert resolved.spec is spec2


def test_validation_boolean_exists_target_keyword_is_valid() -> None:
    """BOOLEAN_EXISTS can target a non-fast KEYWORD field.

    Previously only fast=True or kind=TEXT was accepted at registry
    construction, even though emission already handled non-fast KEYWORD
    via the same term-scan fallback as non-fast TEXT: the accepted set and
    the executable set disagreed. KEYWORD is now explicitly accepted.
    """
    spec1 = FieldSpec(name="tag", kind=FieldKind.KEYWORD)  # not TEXT, not fast
    spec2 = FieldSpec(
        name="has_tag",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="tag",
    )
    registry = FieldRegistry([spec1, spec2])
    resolved = registry.resolve(FieldRef("has_tag"))
    assert resolved is not None
    assert resolved.spec is spec2


def test_validation_boolean_exists_target_fast_is_valid() -> None:
    """BOOLEAN_EXISTS can target a fast=True field."""
    spec1 = FieldSpec(name="flag", kind=FieldKind.KEYWORD, fast=True)
    spec2 = FieldSpec(
        name="has_flag",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="flag",
    )
    registry = FieldRegistry([spec1, spec2])
    resolved = registry.resolve(FieldRef("has_flag"))
    assert resolved is not None
    assert resolved.spec is spec2


# ============================================================================
# ExistsStrategy resolution
# ============================================================================


@pytest.mark.parametrize(
    ("kind", "fast", "expected"),
    [
        pytest.param(FieldKind.TEXT, True, ExistsStrategy.FAST_FIELD, id="fast-text"),
        pytest.param(FieldKind.KEYWORD, True, ExistsStrategy.FAST_FIELD, id="fast-keyword"),
        pytest.param(FieldKind.U64, True, ExistsStrategy.FAST_FIELD, id="fast-u64"),
        pytest.param(FieldKind.DATE, True, ExistsStrategy.FAST_FIELD, id="fast-date"),
        pytest.param(FieldKind.TEXT, False, ExistsStrategy.TERM_SCAN, id="non-fast-text"),
        pytest.param(FieldKind.KEYWORD, False, ExistsStrategy.TERM_SCAN, id="non-fast-keyword"),
        pytest.param(FieldKind.U64, False, None, id="non-fast-u64-unsupported"),
        pytest.param(FieldKind.DATE, False, None, id="non-fast-date-unsupported"),
        pytest.param(FieldKind.DATETIME, False, None, id="non-fast-datetime-unsupported"),
    ],
)
def test_resolve_exists_strategy_function(
    kind: FieldKind, fast: bool, expected: ExistsStrategy | None
) -> None:
    """The pure kind/fast -> strategy resolution function used by both
    registry validation and (via ``FieldRegistry.exists_strategy``)
    emission.
    """
    assert resolve_exists_strategy(kind, fast) is expected


def test_registry_exists_strategy_accessor() -> None:
    """FieldRegistry.exists_strategy() returns the strategy resolved at
    construction time for a registered spec, without re-inspecting kind or
    fastness at call time.
    """
    fast_spec = FieldSpec(name="flag", kind=FieldKind.KEYWORD, fast=True)
    text_spec = FieldSpec(name="body", kind=FieldKind.TEXT)
    unsupported_spec = FieldSpec(name="page_count", kind=FieldKind.U64)
    registry = FieldRegistry([fast_spec, text_spec, unsupported_spec])

    assert registry.exists_strategy(fast_spec) is ExistsStrategy.FAST_FIELD
    assert registry.exists_strategy(text_spec) is ExistsStrategy.TERM_SCAN
    assert registry.exists_strategy(unsupported_spec) is None


def test_every_and_boolean_exists_share_resolved_strategy() -> None:
    """A field's exists strategy, as resolved for a bare ``field:*``
    (``Every``), and as resolved for a BOOLEAN_EXISTS field targeting it,
    are the exact same registry-computed value, by construction.
    """
    target = FieldSpec(name="tag", kind=FieldKind.KEYWORD)
    has_tag = FieldSpec(name="has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="tag")
    registry = FieldRegistry([target, has_tag])

    tag_resolved = registry.resolve(FieldRef("tag"))
    assert tag_resolved is not None
    every_field_strategy = registry.exists_strategy(tag_resolved.spec)
    assert has_tag.exists_target is not None
    target_resolved = registry.resolve(FieldRef(has_tag.exists_target))
    assert target_resolved is not None
    boolean_exists_target_strategy = registry.exists_strategy(target_resolved.spec)
    assert every_field_strategy is boolean_exists_target_strategy is ExistsStrategy.TERM_SCAN


# ============================================================================
# Validation: JSON
# ============================================================================


def test_validation_json_requires_subpaths() -> None:
    """ValueError if JSON spec has empty subpaths."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=())
    with pytest.raises(ValueError, match="notes"):
        FieldRegistry([spec])


def test_validation_json_with_subpaths_is_valid() -> None:
    """JSON spec with non-empty subpaths is valid."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("notes"))
    assert resolved is not None
    assert resolved.spec is spec


# ============================================================================
# Validation: comma_values
# ============================================================================


def test_validation_comma_values_on_keyword() -> None:
    """comma_values=True is valid on KEYWORD."""
    spec = FieldSpec(name="tags", kind=FieldKind.KEYWORD, comma_values=True)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("tags"))
    assert resolved is not None
    assert resolved.spec is spec


def test_validation_comma_values_on_u64() -> None:
    """comma_values=True is valid on U64."""
    spec = FieldSpec(name="ids", kind=FieldKind.U64, comma_values=True)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("ids"))
    assert resolved is not None
    assert resolved.spec is spec


def test_validation_comma_values_on_text() -> None:
    """comma_values=True is valid on TEXT."""
    spec = FieldSpec(name="keywords", kind=FieldKind.TEXT, comma_values=True)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("keywords"))
    assert resolved is not None
    assert resolved.spec is spec


def test_validation_comma_values_on_date_invalid() -> None:
    """ValueError if comma_values=True on DATE."""
    spec = FieldSpec(name="dates", kind=FieldKind.DATE, comma_values=True)
    with pytest.raises(ValueError, match="dates"):
        FieldRegistry([spec])


def test_validation_comma_values_on_json_invalid() -> None:
    """ValueError if comma_values=True on JSON."""
    spec = FieldSpec(
        name="data",
        kind=FieldKind.JSON,
        comma_values=True,
        subpaths=("field",),
    )
    with pytest.raises(ValueError, match="data"):
        FieldRegistry([spec])


# ============================================================================
# Validation: date_only
# ============================================================================


def test_validation_date_only_on_date_true() -> None:
    """date_only=True on DATE is valid."""
    spec = FieldSpec(name="created", kind=FieldKind.DATE, date_only=True)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("created"))
    assert resolved is not None
    assert resolved.spec.date_only is True


def test_validation_date_only_on_date_false_normalized() -> None:
    """DATE spec with date_only=False is normalized to date_only=True."""
    spec = FieldSpec(name="created", kind=FieldKind.DATE, date_only=False)
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("created"))
    assert resolved is not None
    assert resolved.spec.date_only is True


def test_validation_date_only_on_non_date_invalid() -> None:
    """ValueError if date_only=True on non-DATE kind."""
    spec = FieldSpec(name="tags", kind=FieldKind.KEYWORD, date_only=True)
    with pytest.raises(ValueError, match="tags"):
        FieldRegistry([spec])


# ============================================================================
# Integration: Multiple specs with various validations
# ============================================================================


def test_registry_with_multiple_specs() -> None:
    """Registry handles multiple valid specs."""
    specs = [
        FieldSpec(name="title", kind=FieldKind.TEXT),
        FieldSpec(name="tags", kind=FieldKind.KEYWORD, aliases=("categories",)),
        FieldSpec(name="created", kind=FieldKind.DATE, date_only=True),
        FieldSpec(
            name="metadata",
            kind=FieldKind.JSON,
            subpaths=("author", "version"),
        ),
        FieldSpec(
            name="has_attachment",
            kind=FieldKind.BOOLEAN_EXISTS,
            exists_target="title",
        ),
    ]
    registry = FieldRegistry(specs)

    title = registry.resolve(FieldRef("title"))
    categories = registry.resolve(FieldRef("categories"))
    created = registry.resolve(FieldRef("created"))
    has_attachment = registry.resolve(FieldRef("has_attachment"))
    assert title is not None
    assert categories is not None
    assert created is not None
    assert has_attachment is not None
    assert title.spec is specs[0]
    assert categories.spec is specs[1]
    assert created.spec is specs[2]
    assert "metadata.author" in registry
    assert has_attachment.spec is specs[4]


# ============================================================================
# Edge cases
# ============================================================================


def test_empty_registry() -> None:
    """Empty registry works."""
    registry = FieldRegistry([])
    assert list(registry) == []
    assert registry.resolve(FieldRef("anything")) is None
    assert "anything" not in registry


def test_field_spec_frozen() -> None:
    """FieldSpec is frozen (immutable)."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "new_title"  # type: ignore[misc]
