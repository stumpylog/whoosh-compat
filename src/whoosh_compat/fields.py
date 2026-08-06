"""Field definitions and registry for whoosh-compat."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from enum import auto


class FieldKind(Enum):
    """Kinds of fields in the schema."""

    TEXT = auto()
    KEYWORD = auto()
    U64 = auto()
    DATE = auto()
    DATETIME = auto()
    BOOLEAN_EXISTS = auto()
    JSON = auto()


class Multitoken(Enum):
    """How to handle multi-token field values."""

    DEFAULT = auto()
    AND = auto()
    OR = auto()
    PHRASE = auto()
    FIRST = auto()


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Specification for a single field in the schema."""

    name: str
    kind: FieldKind
    aliases: tuple[str, ...] = ()
    comma_values: bool = False
    analyzer: Callable[[str], list[str]] | None = None
    pattern_normalizer: Callable[[str], str] | None = None
    multitoken: Multitoken = Multitoken.DEFAULT
    exists_target: str | None = None
    subpaths: tuple[str, ...] = ()
    date_only: bool = False
    fast: bool = False


class FieldRegistry:
    """Registry of field specifications with validation."""

    def __init__(self, specs: Iterable[FieldSpec]) -> None:
        """Initialize registry with field specs, validating constraints.

        Args:
            specs: Iterable of FieldSpec objects.

        Raises:
            ValueError: If any validation rule is violated.
        """
        self._specs: list[FieldSpec] = []
        self._by_name: dict[str, FieldSpec] = {}
        specs_list = list(specs)

        # First pass: normalize DATE specs and collect all specs
        normalized_specs = []
        for spec in specs_list:
            if spec.kind == FieldKind.DATE and not spec.date_only:
                # Normalize: DATE with date_only=False -> date_only=True
                spec = dataclasses.replace(spec, date_only=True)
            normalized_specs.append(spec)

        # Second pass: validate and build indices
        for spec in normalized_specs:
            # Validate: date_only only on DATE
            if spec.date_only and spec.kind != FieldKind.DATE:
                raise ValueError(f"Field '{spec.name}': date_only=True only valid on DATE kind")

            # Validate: JSON requires non-empty subpaths
            if spec.kind == FieldKind.JSON and not spec.subpaths:
                raise ValueError(f"Field '{spec.name}': JSON kind requires non-empty subpaths")

            # Validate: comma_values only on KEYWORD, U64, TEXT
            if spec.comma_values and spec.kind not in (
                FieldKind.KEYWORD,
                FieldKind.U64,
                FieldKind.TEXT,
            ):
                raise ValueError(
                    f"Field '{spec.name}': comma_values=True only valid on "
                    f"KEYWORD, U64, or TEXT kinds"
                )

            # Validate: BOOLEAN_EXISTS requires exists_target
            if spec.kind == FieldKind.BOOLEAN_EXISTS and spec.exists_target is None:
                raise ValueError(f"Field '{spec.name}': BOOLEAN_EXISTS requires exists_target")

            # Check for duplicate canonical names
            if spec.name in self._by_name:
                raise ValueError(f"Field '{spec.name}': duplicate canonical name")

            # Check for alias collision with canonical names
            for alias in spec.aliases:
                if alias in self._by_name:
                    raise ValueError(
                        f"Field '{spec.name}': alias '{alias}' collides with "
                        f"existing canonical name or alias"
                    )

            # Register the spec by canonical name
            self._by_name[spec.name] = spec

            # Register by aliases
            for alias in spec.aliases:
                self._by_name[alias] = spec

            self._specs.append(spec)

        # Third pass: validate BOOLEAN_EXISTS targets (now all specs are registered)
        for spec in self._specs:
            if spec.kind == FieldKind.BOOLEAN_EXISTS:
                # Second pass above already rejected BOOLEAN_EXISTS specs
                # with exists_target=None.
                assert spec.exists_target is not None
                target_spec = self.resolve(spec.exists_target)
                if target_spec is None:
                    raise ValueError(
                        f"Field '{spec.name}': exists_target '{spec.exists_target}' "
                        f"does not reference a registered spec"
                    )
                # Validate: target must be fast=True or kind=TEXT
                if not (target_spec.fast or target_spec.kind == FieldKind.TEXT):
                    raise ValueError(
                        f"Field '{spec.name}': exists_target '{spec.exists_target}' "
                        f"must be fast=True or kind=TEXT"
                    )

    def resolve(self, name: str) -> FieldSpec | None:
        """Resolve a field spec by canonical name or alias.

        Args:
            name: The canonical name or alias to resolve.

        Returns:
            The FieldSpec, or None if not found.
        """
        return self._by_name.get(name)

    def resolve_json(self, dotted: str) -> tuple[FieldSpec, str] | None:
        """Resolve a JSON field by dotted path.

        Splits on the first dot only. Returns (spec, subpath) if spec.kind is JSON
        and subpath is in spec.subpaths, else None.

        Args:
            dotted: Dotted path like "notes.user" or "metadata.author.name".

        Returns:
            Tuple of (FieldSpec, subpath) if valid, else None.
        """
        if "." not in dotted:
            return None

        # Split on first dot only
        name, subpath = dotted.split(".", 1)
        spec = self.resolve(name)
        if spec is None or spec.kind != FieldKind.JSON:
            return None

        # Check if subpath is registered
        if subpath not in spec.subpaths:
            return None

        return (spec, subpath)

    def __contains__(self, name: str) -> bool:
        """Check if a name is a canonical name, alias, or valid dotted path.

        Args:
            name: The name to check.

        Returns:
            True if name is found as canonical, alias, or valid dotted JSON path.
        """
        # Check canonical name or alias
        if name in self._by_name:
            return True

        # Check if it's a valid dotted JSON path
        return self.resolve_json(name) is not None

    def __iter__(self):
        """Iterate over FieldSpec objects in insertion order (deduplicated)."""
        return iter(self._specs)
