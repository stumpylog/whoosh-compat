"""Tests for fields.py: FieldSpec and FieldRegistry."""

import dataclasses

import pytest

from whoosh_compat.fields import ExistsStrategy
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import resolve_exists_strategy

# ============================================================================
# Test resolve(): canonical names and aliases
# ============================================================================


def test_resolve_by_canonical_name():
    """resolve() finds a spec by its canonical name."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.resolve("title") is spec


def test_resolve_by_alias():
    """resolve() finds a spec by its alias."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading", "subject"))
    registry = FieldRegistry([spec])
    assert registry.resolve("heading") is spec
    assert registry.resolve("subject") is spec


def test_resolve_unknown_returns_none():
    """resolve() returns None for unknown names."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.resolve("unknown") is None


# ============================================================================
# Test resolve_json(): dotted path resolution
# ============================================================================


def test_resolve_json_simple_path():
    """resolve_json() splits on first dot and validates subpath."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user", "admin"),
    )
    registry = FieldRegistry([spec])
    result = registry.resolve_json("notes.user")
    assert result == (spec, "user")


def test_resolve_json_multipart_subpath():
    """resolve_json() validates only first dot; subpath can have more dots."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user.name", "admin.role"),
    )
    registry = FieldRegistry([spec])
    result = registry.resolve_json("notes.user.name")
    assert result == (spec, "user.name")


def test_resolve_json_invalid_subpath():
    """resolve_json() returns None if subpath not in spec.subpaths."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user",),
    )
    registry = FieldRegistry([spec])
    assert registry.resolve_json("notes.body") is None


def test_resolve_json_non_json_spec():
    """resolve_json() returns None if spec.kind is not JSON."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert registry.resolve_json("title.user") is None


def test_resolve_json_no_dot():
    """resolve_json() returns None if path has no dot."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user",),
    )
    registry = FieldRegistry([spec])
    assert registry.resolve_json("notes") is None


# ============================================================================
# Test __contains__: in operator
# ============================================================================


def test_contains_canonical_name():
    """in operator returns True for canonical name."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert "title" in registry


def test_contains_alias():
    """in operator returns True for alias."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    registry = FieldRegistry([spec])
    assert "heading" in registry


def test_contains_dotted_json_path():
    """in operator returns True for valid dotted JSON path."""
    spec = FieldSpec(
        name="notes",
        kind=FieldKind.JSON,
        subpaths=("user",),
    )
    registry = FieldRegistry([spec])
    assert "notes.user" in registry


def test_contains_invalid_path():
    """in operator returns False for invalid path."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    registry = FieldRegistry([spec])
    assert "unknown" not in registry


# ============================================================================
# Test __iter__: iteration over specs
# ============================================================================


def test_iter_specs():
    """Iteration yields FieldSpec objects in insertion order."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="date", kind=FieldKind.DATE, date_only=True)
    spec3 = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec1, spec2, spec3])

    specs = list(registry)
    assert specs == [spec1, spec2, spec3]


def test_iter_deduplicates():
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


def test_validation_duplicate_canonical_names():
    """ValueError if two specs have the same canonical name."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="title", kind=FieldKind.KEYWORD)
    with pytest.raises(ValueError, match="title"):
        FieldRegistry([spec1, spec2])


def test_validation_alias_collides_with_canonical():
    """ValueError if an alias collides with another spec's canonical name."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT)
    spec2 = FieldSpec(name="body", kind=FieldKind.TEXT, aliases=("title",))
    with pytest.raises(ValueError, match="body"):
        FieldRegistry([spec1, spec2])


def test_validation_duplicate_aliases():
    """ValueError if two specs have the same alias."""
    spec1 = FieldSpec(name="title", kind=FieldKind.TEXT, aliases=("heading",))
    spec2 = FieldSpec(name="subject", kind=FieldKind.TEXT, aliases=("heading",))
    with pytest.raises(ValueError, match="subject|title"):
        FieldRegistry([spec1, spec2])


# ============================================================================
# Validation: BOOLEAN_EXISTS
# ============================================================================


def test_validation_boolean_exists_requires_target():
    """ValueError if BOOLEAN_EXISTS spec lacks exists_target."""
    spec = FieldSpec(name="has_attachment", kind=FieldKind.BOOLEAN_EXISTS)
    with pytest.raises(ValueError, match="has_attachment"):
        FieldRegistry([spec])


def test_validation_boolean_exists_target_must_exist():
    """ValueError if exists_target does not reference a registered spec."""
    spec = FieldSpec(
        name="has_attachment",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="attachment",
    )
    with pytest.raises(ValueError, match="has_attachment"):
        FieldRegistry([spec])


def test_validation_boolean_exists_target_unsupported_kind_rejected():
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


def test_validation_boolean_exists_target_text_is_valid():
    """BOOLEAN_EXISTS can target a non-fast TEXT field."""
    spec1 = FieldSpec(name="attachment", kind=FieldKind.TEXT)
    spec2 = FieldSpec(
        name="has_attachment",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="attachment",
    )
    registry = FieldRegistry([spec1, spec2])
    assert registry.resolve("has_attachment") is spec2


def test_validation_boolean_exists_target_keyword_is_valid():
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
    assert registry.resolve("has_tag") is spec2


def test_validation_boolean_exists_target_fast_is_valid():
    """BOOLEAN_EXISTS can target a fast=True field."""
    spec1 = FieldSpec(name="flag", kind=FieldKind.KEYWORD, fast=True)
    spec2 = FieldSpec(
        name="has_flag",
        kind=FieldKind.BOOLEAN_EXISTS,
        exists_target="flag",
    )
    registry = FieldRegistry([spec1, spec2])
    assert registry.resolve("has_flag") is spec2


# ============================================================================
# ExistsStrategy resolution
# ============================================================================


@pytest.mark.parametrize(
    "kind, fast, expected",
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
def test_resolve_exists_strategy_function(kind, fast, expected):
    """The pure kind/fast -> strategy resolution function used by both
    registry validation and (via ``FieldRegistry.exists_strategy``)
    emission.
    """
    assert resolve_exists_strategy(kind, fast) is expected


def test_registry_exists_strategy_accessor():
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


def test_every_and_boolean_exists_share_resolved_strategy():
    """A field's exists strategy, as resolved for a bare ``field:*``
    (``Every``), and as resolved for a BOOLEAN_EXISTS field targeting it,
    are the exact same registry-computed value, by construction.
    """
    target = FieldSpec(name="tag", kind=FieldKind.KEYWORD)
    has_tag = FieldSpec(name="has_tag", kind=FieldKind.BOOLEAN_EXISTS, exists_target="tag")
    registry = FieldRegistry([target, has_tag])

    every_field_strategy = registry.exists_strategy(registry.resolve("tag"))
    boolean_exists_target_strategy = registry.exists_strategy(
        registry.resolve(has_tag.exists_target)
    )
    assert every_field_strategy is boolean_exists_target_strategy is ExistsStrategy.TERM_SCAN


# ============================================================================
# Validation: JSON
# ============================================================================


def test_validation_json_requires_subpaths():
    """ValueError if JSON spec has empty subpaths."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=())
    with pytest.raises(ValueError, match="notes"):
        FieldRegistry([spec])


def test_validation_json_with_subpaths_is_valid():
    """JSON spec with non-empty subpaths is valid."""
    spec = FieldSpec(name="notes", kind=FieldKind.JSON, subpaths=("user",))
    registry = FieldRegistry([spec])
    assert registry.resolve("notes") is spec


# ============================================================================
# Validation: comma_values
# ============================================================================


def test_validation_comma_values_on_keyword():
    """comma_values=True is valid on KEYWORD."""
    spec = FieldSpec(name="tags", kind=FieldKind.KEYWORD, comma_values=True)
    registry = FieldRegistry([spec])
    assert registry.resolve("tags") is spec


def test_validation_comma_values_on_u64():
    """comma_values=True is valid on U64."""
    spec = FieldSpec(name="ids", kind=FieldKind.U64, comma_values=True)
    registry = FieldRegistry([spec])
    assert registry.resolve("ids") is spec


def test_validation_comma_values_on_text():
    """comma_values=True is valid on TEXT."""
    spec = FieldSpec(name="keywords", kind=FieldKind.TEXT, comma_values=True)
    registry = FieldRegistry([spec])
    assert registry.resolve("keywords") is spec


def test_validation_comma_values_on_date_invalid():
    """ValueError if comma_values=True on DATE."""
    spec = FieldSpec(name="dates", kind=FieldKind.DATE, comma_values=True)
    with pytest.raises(ValueError, match="dates"):
        FieldRegistry([spec])


def test_validation_comma_values_on_json_invalid():
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


def test_validation_date_only_on_date_true():
    """date_only=True on DATE is valid."""
    spec = FieldSpec(name="created", kind=FieldKind.DATE, date_only=True)
    registry = FieldRegistry([spec])
    resolved = registry.resolve("created")
    assert resolved.date_only is True


def test_validation_date_only_on_date_false_normalized():
    """DATE spec with date_only=False is normalized to date_only=True."""
    spec = FieldSpec(name="created", kind=FieldKind.DATE, date_only=False)
    registry = FieldRegistry([spec])
    resolved = registry.resolve("created")
    assert resolved.date_only is True


def test_validation_date_only_on_non_date_invalid():
    """ValueError if date_only=True on non-DATE kind."""
    spec = FieldSpec(name="tags", kind=FieldKind.KEYWORD, date_only=True)
    with pytest.raises(ValueError, match="tags"):
        FieldRegistry([spec])


# ============================================================================
# Integration: Multiple specs with various validations
# ============================================================================


def test_registry_with_multiple_specs():
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

    assert registry.resolve("title") is specs[0]
    assert registry.resolve("categories") is specs[1]
    assert registry.resolve("created") is specs[2]
    assert "metadata.author" in registry
    assert registry.resolve("has_attachment") is specs[4]


# ============================================================================
# Edge cases
# ============================================================================


def test_empty_registry():
    """Empty registry works."""
    registry = FieldRegistry([])
    assert list(registry) == []
    assert registry.resolve("anything") is None
    assert "anything" not in registry


def test_field_spec_frozen():
    """FieldSpec is frozen (immutable)."""
    spec = FieldSpec(name="title", kind=FieldKind.TEXT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "new_title"
