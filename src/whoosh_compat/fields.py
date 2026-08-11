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


class ExistsStrategy(Enum):
    """How to execute an "exists" check (bare ``field:*`` or BOOLEAN_EXISTS)
    against a given field.

    Resolved once, at ``FieldRegistry`` construction, from a field's
    ``fast``/``kind`` combination, and stored on the ``FieldSpec`` so
    emission dispatches on the resolved strategy instead of re-deriving it
    from field capability, keeping ``Every(field)`` and BOOLEAN_EXISTS in
    agreement by construction.
    """

    FAST_FIELD = auto()
    """A cheap fast-field presence check (tantivy's ``exists_query``)."""

    TERM_SCAN = auto()
    """"Has at least one indexed term", via a ``regex_query(".*")`` sweep of
    the field's term dictionary. Only meaningful for TEXT/KEYWORD fields, and
    only "has at least one indexed term", not "the stored value is
    non-empty": a whitespace-only or punctuation-only value that the field's
    analyzer reduces to zero tokens reads as absent (DIVERGENCES.md entry
    20).
    """


def resolve_exists_strategy(kind: FieldKind, fast: bool) -> ExistsStrategy | None:
    """Resolve the "exists" execution strategy for a field, or ``None`` if
    the field's kind/fastness combination cannot support one at all.

    A fast field always uses ``FAST_FIELD`` regardless of kind, since
    tantivy's ``exists_query`` works on any fast field. A non-fast TEXT or
    KEYWORD field falls back to ``TERM_SCAN``. Every other combination
    (a non-fast field of any other kind) has no way to answer "exists".
    """
    if fast:
        return ExistsStrategy.FAST_FIELD
    if kind in (FieldKind.TEXT, FieldKind.KEYWORD):
        return ExistsStrategy.TERM_SCAN
    return None


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
        # Each canonical field name's resolved "exists" execution strategy,
        # computed once here from kind/fast rather than re-derived at emit
        # time. See ``exists_strategy()``.
        self._exists_strategies: dict[str, ExistsStrategy | None] = {}
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

            # Resolve and store this field's own "exists" execution
            # strategy once, up front, so BOOLEAN_EXISTS validation below
            # and emission later both read the same answer instead of
            # re-deriving it from kind/fast independently.
            self._exists_strategies[spec.name] = resolve_exists_strategy(spec.kind, spec.fast)

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
                # Validate: target must resolve to a supported "exists"
                # strategy (fast=True of any kind, or non-fast TEXT/KEYWORD).
                if self.exists_strategy(target_spec) is None:
                    raise ValueError(
                        f"Field '{spec.name}': exists_target '{spec.exists_target}' "
                        f"(kind={target_spec.kind.name}, fast={target_spec.fast}) has "
                        f"no way to answer 'exists': mark it fast=True, or change its "
                        f"kind to TEXT or KEYWORD"
                    )

    def resolve(self, name: str) -> FieldSpec | None:
        """Resolve a field spec by canonical name or alias.

        Args:
            name: The canonical name or alias to resolve.

        Returns:
            The FieldSpec, or None if not found.
        """
        return self._by_name.get(name)

    def exists_strategy(self, spec: FieldSpec) -> ExistsStrategy | None:
        """Return ``spec``'s resolved "exists" execution strategy.

        Resolved once, at registry construction, from ``spec.kind`` and
        ``spec.fast`` (see ``resolve_exists_strategy``); ``None`` means the
        field has no way to answer "exists" while non-fast and not
        TEXT/KEYWORD. Shared by ``Every(field)`` and BOOLEAN_EXISTS emission
        so the two agree by construction rather than by parallel capability
        checks.

        Args:
            spec: A ``FieldSpec`` registered in this registry.

        Returns:
            The resolved ``ExistsStrategy``, or ``None`` if unsupported.
        """
        return self._exists_strategies.get(spec.name)

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
