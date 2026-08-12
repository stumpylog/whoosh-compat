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

    FAST_JSON_FIELD = auto()
    """Like ``FAST_FIELD``, but for a fast JSON field: tantivy's
    ``exists_query`` only checks a JSON field's subpath columns when passed
    ``json_subpaths=True``; without it, nothing is ever found to exist
    (issue #7). A JSON field's "fastness" only ever means its subpaths are
    fast columns, so this is the resolved strategy for every fast JSON
    field, never plain ``FAST_FIELD``.
    """

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

    A fast field uses ``FAST_FIELD`` regardless of kind, except JSON, which
    uses ``FAST_JSON_FIELD`` (tantivy's ``exists_query`` needs
    ``json_subpaths=True`` to see a JSON field's subpath columns at all).
    A non-fast TEXT or KEYWORD field falls back to ``TERM_SCAN``. Every
    other combination (a non-fast field of any other kind) has no way to
    answer "exists".
    """
    if fast:
        if kind is FieldKind.JSON:
            return ExistsStrategy.FAST_JSON_FIELD
        return ExistsStrategy.FAST_FIELD
    if kind in (FieldKind.TEXT, FieldKind.KEYWORD):
        return ExistsStrategy.TERM_SCAN
    return None


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A resolved reference to a field, and, for a JSON field, the subpath
    addressed within it.

    Carries the *canonical* field name: alias resolution happens once, at
    ``FieldRegistry.make_ref``, so a ``FieldRef`` never holds the alias text
    a user typed. AST leaves (``whoosh_compat.ast``) hold this instead of a
    raw field-name string, so a type checker can prove ``ref.json_path`` is
    meaningful at a read site without re-deriving it from string inspection
    (e.g. checking for a literal ``"."``).

    Constructing a ``FieldRef`` directly does not validate it against any
    registry: validation happens at ``FieldRegistry.resolve``, which returns
    ``None`` for a ref naming an unregistered field or an unregistered
    subpath. This keeps ``FieldRef`` a plain, cheap data carrier usable in
    tests without a registry in hand.
    """

    name: str
    json_path: str | None = None

    def __str__(self) -> str:
        """The canonical dotted form: ``"name.json_path"``, or just
        ``"name"`` when this ref does not address a JSON subpath.
        """
        if self.json_path is not None:
            return f"{self.name}.{self.json_path}"
        return self.name


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Specification for a single field in the schema.

    ``analyzer``/``pattern_normalizer``/``multitoken``/``comma_values`` are
    only consulted for kinds that use them (TEXT/KEYWORD, plus JSON for
    ``analyzer``); setting one on a kind that ignores it is permitted, not
    validated against, since a host may reasonably share one ``FieldSpec``
    factory across kinds rather than branch on kind to omit them.
    """

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

            # Validate: canonical name and aliases must be non-empty. An
            # empty name can never be typed in a query.
            if spec.name == "":
                raise ValueError("Field name must not be empty")
            seen_aliases: set[str] = set()
            for alias in spec.aliases:
                if alias == "":
                    raise ValueError(f"Field '{spec.name}': alias must not be empty")
                if alias in seen_aliases:
                    raise ValueError(
                        f"Field '{spec.name}': alias '{alias}' is repeated within this spec"
                    )
                seen_aliases.add(alias)

            # Validate: a JSON-kind field's canonical name (and aliases)
            # must be dot-free. make_ref's exact-match lookup (by raw
            # string, tried first) does give a dotted *non*-JSON name a way
            # to resolve (a registered "field.with.dots" TEXT field, or a
            # dotted alias of one, matches directly and is a deliberately
            # supported, tested shape: see
            # tests/emitter/test_emit_terms.py's dotted-plain-field tests).
            # A dotted *JSON* name is different: make_ref's exact-match
            # branch explicitly excludes JSON kind (a bare JSON reference
            # has no subpath and can't emit, see issue #11's demotion fix),
            # so it falls through to dotted-name splitting instead, which
            # looks up the text *before* the first dot as a field name; for
            # a JSON field whose own canonical name contains a dot, that
            # split point isn't the field's own name, so it never resolves
            # at all, subpaths included. Rejecting at construction is
            # better than a JSON field that silently can never be
            # addressed by any query text.
            if spec.kind is FieldKind.JSON:
                if "." in spec.name:
                    raise ValueError(
                        f"Field '{spec.name}': a JSON field's canonical name must not "
                        f"contain '.' (it would make the field, and all its subpaths, "
                        f"unreachable through any query text)"
                    )
                for alias in spec.aliases:
                    if "." in alias:
                        raise ValueError(
                            f"Field '{spec.name}': a JSON field's alias '{alias}' must not "
                            f"contain '.' (same reachability problem as a dotted canonical "
                            f"name)"
                        )

            # Check for duplicate canonical names (or a collision with an
            # earlier spec's alias, named accurately rather than as a
            # misleading "duplicate canonical name" when that's not what
            # actually collided).
            existing = self._by_name.get(spec.name)
            if existing is not None:
                if existing.name == spec.name:
                    raise ValueError(f"Field '{spec.name}': duplicate canonical name")
                raise ValueError(
                    f"Field '{spec.name}': collides with an alias of field '{existing.name}'"
                )

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
                target_spec = self._by_name.get(spec.exists_target)
                if target_spec is None:
                    raise ValueError(
                        f"Field '{spec.name}': exists_target '{spec.exists_target}' "
                        f"does not reference a registered spec"
                    )
                # Validate: target must not itself be BOOLEAN_EXISTS. A
                # BOOLEAN_EXISTS field has no physical index column of its
                # own to check "exists" against; that's true regardless of
                # its resolved strategy (a fast=True BOOLEAN_EXISTS target
                # resolves FAST_FIELD despite having no real column behind
                # it, since resolve_exists_strategy only looks at kind/fast,
                # not at what physical column that combination implies), so
                # this can never work no matter what the target's own fast
                # flag is. Includes self-reference (a spec targeting itself)
                # as the degenerate case of a cycle.
                if target_spec.kind is FieldKind.BOOLEAN_EXISTS:
                    raise ValueError(
                        f"Field '{spec.name}': exists_target '{spec.exists_target}' is "
                        f"itself BOOLEAN_EXISTS, which has no physical column to check "
                        f"'exists' against; point exists_target at a real field instead"
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

    def resolve(self, ref: FieldRef) -> FieldSpec | None:
        """Resolve a :class:`FieldRef` to its :class:`FieldSpec`.

        The single resolver for both plain fields and JSON subpaths (see
        the module-level design note above ``FieldRef``): a plain ref
        (``json_path is None``) resolves by canonical name, and a JSON
        subpath ref additionally requires ``spec.kind`` be ``JSON`` and
        ``ref.json_path`` be one of ``spec.subpaths``.

        Args:
            ref: The field reference to resolve, normally produced by
                :meth:`make_ref`.

        Returns:
            The FieldSpec, or None if ``ref`` does not name a registered
            field, or names a subpath that field does not have.
        """
        spec = self._by_name.get(ref.name)
        if spec is None:
            return None
        if ref.json_path is not None and (
            spec.kind is not FieldKind.JSON or ref.json_path not in spec.subpaths
        ):
            return None
        return spec

    def make_ref(self, raw: str) -> FieldRef | None:
        """Resolve a raw field-name string from the parser into a
        :class:`FieldRef`, the single place dotted-name interpretation and
        alias canonicalization happen.

        A name that resolves directly (a canonical name or an alias) wins
        outright, even if it also contains a dot: this is what keeps a
        registered plain field whose own name contains a dot (e.g.
        ``"field.with.dots"``) working, including as an exact match, rather
        than being reinterpreted as a JSON subpath lookup. Only when that
        direct lookup misses does a dotted name get a second look as
        ``base.subpath`` against a registered JSON field's ``subpaths``.

        A bare (undotted) name that resolves directly to a JSON-kind spec is
        deliberately *not* recognized here: a JSON field addressed without a
        subpath has no way to emit (``visit_term``/``visit_phrase`` require
        one), so treating it as known here would let ``notes:foo`` parse
        cleanly and then raise at emit time, breaking the "parsing clean
        means emitting is safe" contract. Since ``make_ref`` is also what
        ``FieldsPlugin.do_fieldnames`` calls (via ``__contains__``) to decide
        whether a field prefix is recognized at all, returning ``None`` here
        demotes it the same way an entirely unknown field name already is
        (issue #11): consistent, not stricter, since a known field addressed
        incorrectly demoting more strictly than an unknown one would be
        backwards.

        Args:
            raw: The raw field-name text, as captured by the parser's
                fieldname tagger (already alias-as-typed, not yet
                canonicalized).

        Returns:
            A canonical :class:`FieldRef`, or None if ``raw`` names neither
            a registered field nor a registered JSON subpath.
        """
        spec = self._by_name.get(raw)
        if spec is not None and spec.kind is not FieldKind.JSON:
            return FieldRef(spec.name)

        if "." in raw:
            name, subpath = raw.split(".", 1)
            spec = self._by_name.get(name)
            if spec is not None and spec.kind is FieldKind.JSON and subpath in spec.subpaths:
                return FieldRef(spec.name, subpath)

        return None

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

    def __contains__(self, name: str) -> bool:
        """Check if a name is a canonical name, alias, or valid dotted path.

        Args:
            name: The name to check.

        Returns:
            True if name is found as canonical, alias, or valid dotted JSON path.
        """
        return self.make_ref(name) is not None

    def __iter__(self):
        """Iterate over FieldSpec objects in insertion order (deduplicated)."""
        return iter(self._specs)
