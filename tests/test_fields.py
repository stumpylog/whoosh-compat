"""Tests for fields.py: FieldSpec and FieldRegistry."""

import dataclasses

import pytest

from whoosh_compat.fields import ExistsStrategy
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import SubpathSpec
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
# Test FieldSpec.subpaths: mapping is canonical, tuple is accepted sugar
# ============================================================================


def test_subpaths_tuple_form_normalizes_to_mapping() -> None:
    """A tuple[str, ...] passed as subpaths is sugar for a mapping of every
    entry to the trivial default SubpathSpec: the stored, canonical form on
    the constructed instance is always the mapping.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user", "note"))
    assert spec.subpaths == {"user": SubpathSpec(), "note": SubpathSpec()}


def test_subpaths_native_mapping_form_is_kept_as_is() -> None:
    """Constructing with the native Mapping[str, SubpathSpec] form directly
    works and round-trips unchanged.
    """
    mapping = {"user": SubpathSpec(), "note": SubpathSpec()}
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=mapping)
    assert spec.subpaths == mapping


def test_subpaths_empty_tuple_normalizes_to_empty_mapping() -> None:
    """An empty tuple (the field's default) normalizes to an empty mapping,
    not left as a tuple.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=())
    assert spec.subpaths == {}


def test_subpaths_default_is_empty_mapping() -> None:
    """Omitting subpaths entirely also normalizes through __post_init__ to
    an empty mapping (the dataclass default is the tuple sugar form).
    """
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    assert spec.subpaths == {}


def test_subpaths_membership_checks_work_against_mapping() -> None:
    """resolve()'s ``ref.json_path not in spec.subpaths`` membership check
    (and make_ref's ``subpath in spec.subpaths``) key off the mapping's
    keys, same as they did against the tuple form.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths={"user": SubpathSpec()})
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("notes", "user"))
    assert resolved is not None
    assert resolved.json_path == "user"
    assert registry.resolve(FieldRef("notes", "missing")) is None
    ref = registry.make_ref("notes.user")
    assert ref == FieldRef("notes", "user")


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
    None rather than a plain ref: a JSON field addressed
    without a subpath has no way to emit, so treating it as known here
    would let e.g. "notes:foo" parse cleanly and then raise at emit time.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    assert registry.make_ref("notes") is None


def test_is_bare_json_field_true_for_json_bare_name_or_alias() -> None:
    """is_bare_json_field() is the narrow query FieldsPlugin.do_fieldnames
    uses to detect the one shape a bare JSON field
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
    """Registry construction validation: when a later spec's canonical name
    collides with an
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
    """An empty field name can never be typed in a query."""
    with pytest.raises(ValueError, match="empty"):
        FieldRegistry([FieldSpec(name="", kind=FieldKind.TEXT)])


def test_validation_rejects_empty_alias() -> None:
    with pytest.raises(ValueError, match="empty"):
        FieldRegistry([FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("",))])


def test_validation_rejects_duplicate_aliases_within_one_spec() -> None:
    """Two identical aliases on the *same* spec (as opposed to
    test_validation_duplicate_aliases's cross-spec case) previously slipped
    past the collision check entirely, since that check only compares
    against specs already registered, not a spec's own aliases tuple.
    """
    with pytest.raises(ValueError, match="heading"):
        FieldRegistry(
            [FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading", "heading"))]
        )


def test_validation_rejects_dotted_json_canonical_name() -> None:
    """A deliberately narrowed validation: a
    dotted canonical name is only actually unreachable for a JSON-kind
    field. make_ref's exact-match branch excludes JSON specifically (a
    bare JSON reference has no subpath and can't emit), so a
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
    narrowing decision corrects the original,
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
    """A BOOLEAN_EXISTS exists_target can never work, since a
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
    """Documented as permitted (not a validation gap): a host
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


def test_validation_rejects_empty_subpath_string() -> None:
    """An empty subpath string constructs and resolves (make_ref("attrs.")
    -> FieldRef("attrs", json_path="")), but no query text can ever type it,
    so a Term built against it silently matches nothing.
    """
    with pytest.raises(ValueError, match="attrs") as excinfo:
        FieldRegistry([FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("",), fast=True)])
    assert "empty" in str(excinfo.value)


def test_validation_rejects_duplicate_subpath_in_tuple_sugar() -> None:
    """A repeated entry in the tuple-sugar form of ``subpaths`` collapses
    silently through the ``{path: SubpathSpec() for
    path in self.subpaths}`` dict comprehension in ``FieldSpec.__post_init__``:
    by the time ``FieldRegistry.__init__`` would see the normalized mapping,
    the duplication is already gone. A ``Mapping[str, SubpathSpec]`` passed
    directly cannot have a literal duplicate key at all (Python dict literals
    reject that structurally), so this check only applies to the tuple sugar
    form, and it has to run in ``FieldSpec.__post_init__`` itself, before
    normalization, to see the duplicate at all.
    """
    with pytest.raises(ValueError, match="user"):
        FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user", "user"))


@pytest.mark.parametrize(
    "subpath",
    [
        pytest.param("has space", id="whitespace"),
        pytest.param("has:colon", id="colon"),
        pytest.param('has"quote', id="double-quote"),
        pytest.param("has-hyphen", id="hyphen"),
        pytest.param("has/slash", id="slash"),
        pytest.param("has#hash", id="hash"),
        pytest.param("has'quote", id="single-quote"),
        pytest.param("has(paren", id="paren"),
    ],
)
def test_validation_rejects_subpath_with_unreachable_character(subpath: str) -> None:
    """The fieldname tagger's expression
    (``FieldsPlugin.expr``, ``r"(?P<text>[\\w.]+|[*]):"``) only ever captures
    word characters and dots, so a subpath containing ANY other character
    can never be typed in any query text: a query addressing it silently
    degrades to default-field text noise with no diagnostic. Validation is
    a whitelist over the tagger's own alphabet, not an enumerated
    blacklist, so it cannot drift out of sync with the tagger regex.
    A hand-built FieldRef carrying an unreachable subpath would also feed
    it unescaped into the JSON parse_query fallback string.
    """
    with pytest.raises(ValueError, match="attrs"):
        FieldRegistry(
            [FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=(subpath,), fast=True)]
        )


@pytest.mark.parametrize(
    "subpath",
    [
        pytest.param("plain", id="plain-word"),
        pytest.param("with_underscore", id="underscore"),
        pytest.param("digits123", id="digits"),
        pytest.param("dotted.inner", id="dotted"),
    ],
)
def test_validation_accepts_tagger_reachable_subpaths(subpath: str) -> None:
    # Controls for the whitelist above: every character class the tagger
    # CAN capture (word characters and dots) must keep constructing.
    reg = FieldRegistry(
        [FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=(subpath,), fast=True)]
    )
    ref = reg.make_ref(f"attrs.{subpath}")
    assert ref is not None
    assert ref.json_path == subpath


@pytest.mark.parametrize(
    "specs",
    [
        pytest.param(
            [
                FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",), fast=True),
                FieldSpec(name="attrs.user", kind=FieldKind.TEXT),
            ],
            id="json-field-registered-first",
        ),
        pytest.param(
            [
                FieldSpec(name="attrs.user", kind=FieldKind.TEXT),
                FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",), fast=True),
            ],
            id="plain-field-registered-first",
        ),
    ],
)
def test_validation_rejects_dotted_plain_name_shadowing_subpath(specs: list[FieldSpec]) -> None:
    """A plain TEXT field whose canonical name is
    exactly "<jsonfield>.<subpath>" would permanently steal every
    "attrs.user:" query from the registered JSON subpath, since make_ref
    resolves an exact match before ever attempting a dotted-subpath split
    (DIVERGENCES.md entry 14). This is the mirror image of the
    dotted-JSON-canonical-name rejection
    (test_validation_rejects_dotted_json_canonical_name), and must be caught
    regardless of which field is registered first.
    """
    with pytest.raises(ValueError, match="attrs") as excinfo:
        FieldRegistry(specs)
    assert "user" in str(excinfo.value)


def test_validation_rejects_alias_shadowing_subpath() -> None:
    """The shadowing collision applies to an alias on an otherwise
    unrelated field, not just a canonical name.
    """
    spec1 = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",), fast=True)
    spec2 = FieldSpec(name="other", kind=FieldKind.TEXT, aliases=("attrs.user",))
    with pytest.raises(ValueError, match="attrs") as excinfo:
        FieldRegistry([spec1, spec2])
    assert "other" in str(excinfo.value)


@pytest.mark.parametrize(
    "collider",
    [
        pytest.param(FieldSpec(name="meta.user", kind=FieldKind.TEXT), id="canonical-name"),
        pytest.param(
            FieldSpec(name="other", kind=FieldKind.TEXT, aliases=("meta.user",)),
            id="alias",
        ),
    ],
)
def test_validation_rejects_shadowing_of_alias_dotted_subpath_route(
    collider: FieldSpec,
) -> None:
    """The alias sibling of the two checks above, on the JSON field's OWN
    side this time: a JSON field's alias makes "<alias>.<subpath>" a
    supported query route too (make_ref resolves the subpath through the
    alias), so a plain field or alias spelled exactly like the
    alias-dotted form steals that route just as surely as the
    canonical-dotted form, and must be rejected the same way.
    """
    json_spec = FieldSpec(
        name="attrs", kind=FieldKind.JSON, subpaths=("user",), aliases=("meta",), fast=True
    )
    with pytest.raises(ValueError, match="attrs") as excinfo:
        FieldRegistry([json_spec, collider])
    assert "user" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("a-b", id="hyphen"),
        pytest.param("a/b", id="slash"),
        pytest.param("a b", id="space"),
        pytest.param("a:b", id="colon"),
        pytest.param('a"b', id="double-quote"),
        pytest.param("*", id="bare-star"),
    ],
)
@pytest.mark.parametrize("where", [pytest.param("name"), pytest.param("alias")])
def test_validation_rejects_tagger_unreachable_names_and_aliases(where: str, bad: str) -> None:
    """The subpath whitelist's sibling cells on the (name | alias |
    subpath) axis: a canonical name or alias containing any character
    outside the fieldname tagger's alphabet can never be typed in query
    text, so every query addressing it silently degrades to default-field
    text noise with no diagnostic. Rejected eagerly, same as subpaths.
    (Such a field would still resolve through make_ref for
    default_fields/field_boosts purposes, but a field that can only ever
    be addressed through host configuration and never through query text
    is a misconfiguration trap, not a feature.)
    """
    if where == "name":
        spec = FieldSpec(name=bad, kind=FieldKind.TEXT)
    else:
        spec = FieldSpec(name="ok", kind=FieldKind.TEXT, aliases=(bad,))
    with pytest.raises(ValueError, match="tagger"):
        FieldRegistry([spec])


@pytest.mark.parametrize(
    "good",
    [
        pytest.param("plain", id="plain-word"),
        pytest.param("with_underscore", id="underscore"),
        pytest.param("digits123", id="digits"),
        pytest.param("field.with.dots", id="dotted-plain-name"),
    ],
)
def test_validation_accepts_tagger_reachable_names(good: str) -> None:
    # Controls, including the deliberately supported dotted PLAIN field
    # name: make_ref tries an exact match before any dotted-subpath split
    # (see fields.py's dotted-JSON-name validation comment and
    # tests/emitter/test_emit_terms.py's dotted-plain-field tests), so
    # such a name resolves directly; only JSON-kind names reject dots, a
    # separate check. DIVERGENCES.md entry 14 documents the adjacent
    # dot-inclusive tagger behavior.
    reg = FieldRegistry([FieldSpec(name=good, kind=FieldKind.TEXT)])
    assert reg.make_ref(good) is not None


def test_subpaths_mapping_is_snapshotted_and_read_only() -> None:
    """The stored subpaths must be a defensive, read-only snapshot: a host
    that keeps a reference to the mapping it passed in must not be able to
    smuggle in, after construction, subpaths that eager registry
    validation would have rejected (the "registry raises eagerly"
    invariant would otherwise be bypassable with no error anywhere).
    """
    mine = {"user": SubpathSpec()}
    spec = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=mine, fast=True)
    reg = FieldRegistry([spec])
    mine[""] = SubpathSpec()  # construction rejects an empty subpath
    assert "" not in spec.subpaths
    assert reg.make_ref("attrs.") is None
    with pytest.raises(TypeError):
        spec.subpaths[""] = SubpathSpec()  # type: ignore[index]


def test_subpaths_tuple_sugar_is_read_only_too() -> None:
    spec = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",))
    with pytest.raises(TypeError):
        spec.subpaths["extra"] = SubpathSpec()  # type: ignore[index]


def test_fieldspec_and_registry_pickle_and_deepcopy() -> None:
    """The read-only subpaths snapshot (a mappingproxy) must not cost the
    spec its copyability: a host passing a registry across process
    boundaries (multiprocessing workers, caches) gets a TypeError from
    pickle otherwise. The round-trip must preserve equality AND the
    read-only protection (the restored subpaths is a fresh proxy, not a
    caller-mutable dict).
    """
    import copy
    import pickle

    spec = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",), fast=True)
    plain = FieldSpec(name="title", kind=FieldKind.TEXT)
    reg = FieldRegistry([spec, plain])

    for original in (spec, plain):
        for restored in (pickle.loads(pickle.dumps(original)), copy.deepcopy(original)):
            assert restored == original
            assert restored.subpaths == original.subpaths
            with pytest.raises(TypeError):
                restored.subpaths["smuggled"] = SubpathSpec()  # type: ignore[index]

    # Every pickle protocol, not just the default: slots dataclasses
    # interact differently with older protocols.
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored_spec = pickle.loads(pickle.dumps(spec, protocol))
        assert restored_spec == spec

    reg2 = pickle.loads(pickle.dumps(reg))
    ref = reg2.make_ref("attrs.user")
    assert ref is not None
    assert reg2.resolve(ref) is not None
    reg3 = copy.deepcopy(reg)
    assert reg3.make_ref("title") is not None


def test_fieldspec_is_hashable() -> None:
    # frozen=True advertises value semantics; a frozen spec that blows up
    # in hash() the moment it carries subpaths is a trap for any future
    # set/dict-key use. Equal specs must hash equal.
    a = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",))
    b = FieldSpec(name="attrs", kind=FieldKind.JSON, subpaths=("user",))
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_alias_dotted_subpath_route_resolves_without_collider() -> None:
    # Control for the rejection above: the alias-dotted route itself is
    # legitimate and must keep resolving to the subpath.
    reg = FieldRegistry(
        [
            FieldSpec(
                name="attrs",
                kind=FieldKind.JSON,
                subpaths=("user",),
                aliases=("meta",),
                fast=True,
            )
        ]
    )
    ref = reg.make_ref("meta.user")
    assert ref is not None
    assert ref.name == "attrs"
    assert ref.json_path == "user"


def test_validation_multi_dot_subpath_still_constructs() -> None:
    """Control: a legitimate multi-level dotted subpath (a subpath string
    that itself contains further dots, e.g. "user.name") is a supported
    shape, not a case the new character-restriction validation should reject.
    Only whitespace, ':', and '"' are unreachable through the fieldname
    tagger; '.' is explicitly part of its expression
    (r"(?P<text>[\\w.]+|[*]):"). See also test_make_ref_json_subpath_with_extra_dots,
    which confirms make_ref resolves such a subpath end to end.
    """
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user.name", "admin.role"))
    registry = FieldRegistry([spec])
    resolved = registry.resolve(FieldRef("notes", "user.name"))
    assert resolved is not None
    assert resolved.json_path == "user.name"


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
