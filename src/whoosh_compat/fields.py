"""Field definitions and registry for whoosh-compat."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from enum import auto
from types import MappingProxyType
from typing import Any
from typing import cast

# The character class of the fieldname tagger's expression
# (parser/plugins.py's FieldsPlugin.expr, r"(?P<text>[\w.]+|[*]):"): the
# alphabet a registered canonical name, alias, or JSON subpath must stay
# inside to be addressable from query text at all. See
# FieldRegistry.__init__'s name/alias and subpath validation.
_TAGGER_REACHABLE = re.compile(r"[\w.]+")

#: The type of ``FieldSpec.pattern_normalizer``: a fragment of a
#: wildcard/prefix pattern in, and *one or more* forms of it out, any of
#: which a term may match.
#:
#: Returning a bare ``str`` means exactly one form and is the plain
#: case-folding contract this hook has always had. Returning a sequence
#: means the emitter should accept a term matching *any* of the forms, ORed
#: together; that is what lets a pattern reach both the run as typed and its
#: stem, which no single string can do (English Snowball substitutes rather
#: than truncates: "company" -> "compani", so a prefix of one is not a
#: prefix of the other). An empty sequence means "no term can match this
#: fragment", and the whole pattern then matches nothing.
#:
#: A bare ``str`` is accepted rather than rejected on purpose: ``str`` *is*
#: a ``Sequence[str]``, so a normalizer written against the old contract
#: type-checks against the new one and would otherwise be read, silently, as
#: one alternative per character.
PatternNormalizer = Callable[[str], "str | Sequence[str]"]


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
   . A JSON field's "fastness" only ever means its subpaths are
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
class SubpathSpec:
    """Per-subpath specification for one entry of a JSON field's
    ``subpaths``.

    Still nearly trivial: a subpath inherits the parent JSON field's
    analyzer and text semantics, exactly as it did when ``subpaths`` was a
    bare ``tuple[str, ...]``. The one thing it carries is ``default``.

    ``default=True`` marks this subpath as the one a *bare* mention of the
    parent JSON field means: with it, ``make_ref("notes")`` resolves to
    ``FieldRef("notes", "note")`` instead of ``None``, and the field stops
    being a bare (unresolvable) JSON field. At most one subpath per
    ``FieldSpec`` may declare it; ``FieldRegistry.__init__`` rejects more.
    Without it, a bare JSON field name stays unresolvable, which is still
    the right answer for a JSON field with no privileged subpath (a host's
    custom-field bag, say, where ``cf:`` means nothing in particular).

    This is the growth the container shape was frozen for; per-subpath
    *typing* (numeric, date, or boolean subpaths) remains a separate, later
    change and is not implemented here.
    """

    default: bool = False


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Specification for a single field in the schema.

    ``analyzer``/``multitoken`` are only consulted for kinds that use them:
    TEXT/KEYWORD, plus JSON, whose subpaths are tokenized exactly like a
    TEXT/KEYWORD field's value and so honor both the same way (a JSON
    subpath's multi-token value is combined per this same spec's
    ``multitoken``, falling back to the enclosing group's combinator on
    ``Multitoken.DEFAULT``, identically to a plain field; see
    ``_leaf_tokens``/``_analyze_term`` in ``ast.py``). ``pattern_normalizer``
    is narrower: TEXT/KEYWORD only, never JSON, even through a subpath.
    A JSON subpath's wildcard/prefix query is refused outright
    (``AST_PATTERN_ON_KIND``, DIVERGENCES.md entry 30; a parse-time
    ``PATTERN_ON_SUBPATH`` diagnostic catches ordinary query text before
    that), so ``spec.pattern_normalizer`` is never even read for one; see
    ``TantivyEmitter._reject_pattern_incompatible_kind``. It may return
    either one form of a pattern fragment or several alternatives; see
    :data:`PatternNormalizer` for that contract.
    Setting one of these three on a kind that ignores it is permitted, not
    validated against, since a host may reasonably share one ``FieldSpec``
    factory across kinds rather than branch on kind to omit them.
    ``comma_values`` is the exception: ``FieldRegistry.__init__`` raises
    ``ValueError`` for ``comma_values=True`` on any kind other than
    KEYWORD, U64, or TEXT (a comma in a date or boolean value is a parse
    problem, not a list), so a shared factory must branch on kind for this
    one flag.

    ``subpaths`` accepts either its canonical form, a
    ``Mapping[str, SubpathSpec]``, or a ``tuple[str, ...]`` as sugar for "all
    of these subpaths, with the trivial default ``SubpathSpec``" (the tuple
    form therefore cannot declare a default subpath; use the mapping form
    for that). Any other type, a list most plausibly, is rejected outright
    rather than passed to ``dict()``, which would read a list of names as a
    sequence of key/value pairs. ``__post_init__`` normalizes either
    accepted input into the stored form: a
    read-only *snapshot* (``MappingProxyType`` over a private copy), never
    the tuple or the caller's own mapping object. The copy is what keeps
    the "registry validates eagerly" contract honest: a host that retains
    a reference to the mapping it passed in cannot mutate subpaths into
    the spec after ``FieldRegistry`` construction has validated them, and
    the proxy rejects direct ``spec.subpaths[...] = ...`` assignment too.
    ``subpaths`` is excluded from the generated ``__hash__`` (mappings are
    unhashable) but still participates in ``__eq__``; equal specs hash
    equal, so the frozen spec is usable as a set member or dict key.
    """

    name: str
    kind: FieldKind
    aliases: tuple[str, ...] = ()
    comma_values: bool = False
    analyzer: Callable[[str], list[str]] | None = None
    pattern_normalizer: PatternNormalizer | None = None
    multitoken: Multitoken = Multitoken.DEFAULT
    exists_target: str | None = None
    subpaths: Mapping[str, SubpathSpec] | tuple[str, ...] = field(default=(), hash=False)
    date_only: bool = False
    fast: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.subpaths, tuple):
            # A dict comprehension silently collapses a repeated key, so a
            # duplicate in the tuple sugar form has to be caught here, before
            # normalization erases the fact that it was ever repeated: by the
            # time FieldRegistry.__init__ sees this spec, subpaths is already
            # the deduplicated mapping and the duplication is unrecoverable.
            seen: set[str] = set()
            for path in self.subpaths:
                if path in seen:
                    raise ValueError(
                        f"Field '{self.name}': subpath '{path}' is repeated in the "
                        f"subpaths tuple; remove the duplicate"
                    )
                seen.add(path)
            normalized = {path: SubpathSpec() for path in self.subpaths}
        elif isinstance(self.subpaths, Mapping):
            # Copy, don't alias: the caller may retain (and later mutate)
            # the mapping object it passed in, and validation has to bind
            # to what was true at construction time.
            normalized = dict(self.subpaths)
        else:
            # Anything else (most plausibly a list, the tuple sugar spelled
            # with the wrong brackets) is rejected here rather than fed to
            # dict(). dict() accepts a sequence of pairs, so a list of
            # two-character subpath names silently became a mapping of
            # their halves: ['ab', 'cd'] -> {'a': 'b', 'c': 'd'}. The
            # registry then validated and accepted that, the real subpaths
            # were permanently unaddressable, and every query against one
            # degraded to default-field noise with no error anywhere. A
            # list of any other name length raised, but out of dict() and
            # in dict()'s own vocabulary ("dictionary update sequence
            # element #0 has length 3; 2 is required"), which names nothing
            # the caller wrote.
            raise ValueError(  # noqa: TRY004 (registry misconfiguration is ValueError throughout)
                f"Field '{self.name}': subpaths must be a tuple or mapping, not "
                f"{type(self.subpaths).__name__} (a list of names silently becomes "
                f"a mapping of their halves; use a tuple)"
            )
        # Frozen dataclass: normalize through object.__setattr__ rather
        # than plain assignment. Every reader elsewhere sees only this
        # read-only snapshot, never the tuple sugar or the caller's own
        # mapping object.
        object.__setattr__(self, "subpaths", MappingProxyType(normalized))

    @property
    def default_subpath(self) -> str | None:
        """The subpath a bare mention of this field means, or ``None``.

        The one subpath whose ``SubpathSpec.default`` is True (see
        :class:`SubpathSpec`), or ``None`` when no subpath declares it, in
        which case a bare mention of this field resolves to nothing at all
        (``FieldRegistry.make_ref`` returns ``None`` and
        ``FieldRegistry.is_bare_json_field`` returns True).
        Only meaningful on a *registered* spec. A ``FieldSpec`` on its own
        is normalized but not validated: ``__post_init__`` settles the
        container's shape and nothing else, while everything about the
        subpaths themselves is checked by ``FieldRegistry.__init__``, the
        only supported way to put a spec into use. So on a spec that has
        never been registered this property answers from unvalidated data
        and inherits exactly the two failure modes registration would have
        rejected: several subpaths declaring ``default=True`` returns the
        first in iteration order rather than raising, and a subpath mapped
        to something that is not a ``SubpathSpec`` at all raises
        ``AttributeError`` from the ``.default`` read (e.g.
        ``FieldSpec("n", FieldKind.JSON, subpaths={"a": "x"})``). Both are
        ``ValueError`` at registry construction; neither is reachable
        through a registry, which is where this property is read from
        (``make_ref``/``is_bare_json_field``).
        """
        subpaths = cast("Mapping[str, SubpathSpec]", self.subpaths)
        for path, subspec in subpaths.items():
            if subspec.default:
                return path
        return None

    # The mappingproxy stored above is not picklable and breaks the
    # default slots-dataclass __reduce_ex__ path (pickle AND copy.deepcopy
    # both route through it), so state is exported with subpaths as a
    # plain dict and re-wrapped in a fresh read-only proxy on restore. The
    # round-trip preserves both equality and the mutation protection.
    # (dataclasses.asdict remains unsupported: it deep-copies field values
    # directly, bypassing this hook entirely, and a bare mappingproxy
    # cannot be deep-copied; nothing in this library or its known hosts
    # calls asdict on a spec.)
    def __getstate__(self) -> dict[str, Any]:
        state = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        state["subpaths"] = dict(cast("Mapping[str, SubpathSpec]", self.subpaths))
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        for name, value in state.items():
            if name == "subpaths":
                value = MappingProxyType(dict(value))
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ResolvedField:
    """The result of resolving a :class:`FieldRef` against a
    :class:`FieldRegistry`: the field's :class:`FieldSpec` together with the
    JSON subpath (if any) the ref addressed.

    ``FieldRegistry.resolve`` is the single place a :class:`FieldRef` turns
    into something query-building or message-building code can act on. This
    type exists so the subpath travels with that result instead of being
    read separately off the original ref (easy to forget, and the shared
    root cause behind several JSON-subpath bugs): a helper that receives a
    ``ResolvedField`` has the subpath in hand whether or not it goes on to
    use it, and a helper that genuinely can't honor a subpath has to say so
    by reading only ``.spec``, a visible decision at the call site rather
    than a silent drop inside a resolver that never had the subpath to
    begin with.
    """

    spec: FieldSpec
    json_path: str | None = None

    @property
    def is_subpath(self) -> bool:
        """Whether this resolution addresses a JSON subpath."""
        return self.json_path is not None

    @property
    def dotted_name(self) -> str:
        """The tantivy-facing field name: ``"attrs.user"`` for a JSON
        subpath, or just ``spec.name`` for a plain field.
        """
        if self.json_path is not None:
            return f"{self.spec.name}.{self.json_path}"
        return self.spec.name


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

        # First pass: validate and build indices
        for spec in specs_list:
            # Validate: date_only is required on DATE, and only on DATE.
            # DATE has exactly one supported granularity (whole calendar
            # days; see dateparse.py's _to_utc), so date_only=True is not
            # optional configuration here, it is a statement of that fact
            # the caller must make explicitly, and date_only=False (the
            # dataclass default, so the common case for an otherwise
            # unremarkable DATE spec) does not describe a real, different
            # granularity DATE could instead have. A DATE spec silently
            # rewritten from date_only=False to date_only=True via
            # dataclasses.replace() used to paper over that (and, since
            # every other kind is rejected for date_only=True just below,
            # the flag could never actually vary on a *registered* spec:
            # always exactly spec.kind is FieldKind.DATE). Besides carrying
            # no information, the rewrite replaced the object the caller
            # passed in with a copy for every ordinary DATE spec, breaking
            # `list(registry)[0] is my_spec`-style identity.
            if spec.kind == FieldKind.DATE and not spec.date_only:
                raise ValueError(
                    f"Field '{spec.name}': DATE requires date_only=True (DATE "
                    f"has exactly one supported granularity, whole calendar "
                    f"days; use DATETIME for a field with a time component)"
                )
            if spec.date_only and spec.kind != FieldKind.DATE:
                raise ValueError(f"Field '{spec.name}': date_only=True only valid on DATE kind")

            # Validate: JSON requires non-empty subpaths
            if spec.kind == FieldKind.JSON and not spec.subpaths:
                raise ValueError(f"Field '{spec.name}': JSON kind requires non-empty subpaths")

            # Validate: each subpath string must itself be non-empty and
            # addressable through the fieldname tagger. An empty subpath
            # constructs and resolves (make_ref("attrs.") -> FieldRef("attrs",
            # json_path="")) but no query text can ever type it, so a Term
            # built against it silently matches nothing. Any character
            # outside the fieldname tagger's own alphabet has the same
            # problem: the tagger's expression (FieldsPlugin.expr,
            # r"(?P<text>[\w.]+|[*]):") only ever captures word characters
            # and dots, so a subpath containing anything else (whitespace,
            # ':', '"', '-', '/', ...) is unreachable from any query text
            # and every query addressing it silently degrades to
            # default-field text noise. Validation is a whitelist over that
            # same character class rather than an enumerated blacklist, so
            # it cannot drift out of sync with the tagger regex. A
            # hand-built FieldRef carrying an unreachable subpath would
            # also feed it unescaped into the JSON parse_query fallback
            # string.
            if spec.kind == FieldKind.JSON:
                subpath_specs = cast("Mapping[str, SubpathSpec]", spec.subpaths)
                # Validate: every value is a real SubpathSpec. Load-bearing
                # now that SubpathSpec carries `default`: anything else
                # either has no `.default` at all (an AttributeError from
                # inside resolution, far from the misconfigured spec) or,
                # worse, a truthy attribute of its own that would silently
                # decide what a bare mention of this field means. Checked
                # before `default` is read anywhere below.
                for subpath, subspec in subpath_specs.items():
                    if not isinstance(subspec, SubpathSpec):
                        raise ValueError(  # noqa: TRY004 (registry misconfiguration is ValueError throughout)
                            f"Field '{spec.name}': subpath '{subpath}' maps to "
                            f"{type(subspec).__name__}, not a SubpathSpec"
                        )
                # Validate: at most one subpath may claim to be the default.
                # Several is not a preference to resolve by iteration order
                # (which would make the meaning of a bare `notes:` depend on
                # dict insertion order); it is a host configuration mistake,
                # so it raises here, eagerly, like every other registry
                # validation.
                defaults = [path for path, sub in subpath_specs.items() if sub.default]
                if len(defaults) > 1:
                    named = ", ".join(repr(path) for path in defaults)
                    raise ValueError(
                        f"Field '{spec.name}': at most one subpath may be the "
                        f"default, but {named} all declare default=True"
                    )
                for subpath in spec.subpaths:
                    if subpath == "":
                        raise ValueError(
                            f"Field '{spec.name}': a JSON field's subpath must not be "
                            f"empty (no query text can ever address it)"
                        )
                    if not _TAGGER_REACHABLE.fullmatch(subpath):
                        raise ValueError(
                            f"Field '{spec.name}': subpath '{subpath}' contains a "
                            f"character the fieldname tagger can never produce "
                            f"(only word characters and '.' are addressable from "
                            f"query text); remove it from the subpath"
                        )

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

            # Validate: canonical name and aliases must be non-empty and
            # addressable through the fieldname tagger, the same whitelist
            # the subpath validation below applies (an empty name, or one
            # containing any character outside the tagger's [\w.] alphabet,
            # can never be typed in a query: every query addressing it
            # would silently degrade to default-field text noise with no
            # diagnostic). A name reachable only through default_fields/
            # field_boosts configuration but never query text is a trap,
            # not a feature, so the rejection is unconditional.
            if spec.name == "":
                raise ValueError("Field name must not be empty")
            if not _TAGGER_REACHABLE.fullmatch(spec.name):
                raise ValueError(
                    f"Field '{spec.name}': name contains a character the fieldname "
                    f"tagger can never produce (only word characters and '.' are "
                    f"addressable from query text)"
                )
            seen_aliases: set[str] = set()
            for alias in spec.aliases:
                if alias == "":
                    raise ValueError(f"Field '{spec.name}': alias must not be empty")
                if not _TAGGER_REACHABLE.fullmatch(alias):
                    raise ValueError(
                        f"Field '{spec.name}': alias '{alias}' contains a character "
                        f"the fieldname tagger can never produce (only word "
                        f"characters and '.' are addressable from query text)"
                    )
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
            # has no subpath and can't emit, see FieldsPlugin's demotion),
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

        # Second pass: validate BOOLEAN_EXISTS targets (now all specs are registered)
        for spec in self._specs:
            if spec.kind == FieldKind.BOOLEAN_EXISTS:
                # The first pass above already rejected BOOLEAN_EXISTS specs
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

        # Third pass: reject a registered canonical name or alias that
        # exactly matches "<jsonfield>.<subpath>" for any registered JSON
        # field's subpath, where <jsonfield> is the JSON field's canonical
        # name OR any of its aliases (an alias makes "<alias>.<subpath>:" a
        # supported query route too: make_ref resolves the subpath through
        # the alias). This is the mirror image of the dotted-JSON-name
        # rejection above: make_ref resolves an exact match before ever
        # attempting a dotted-subpath split, so a plain field or alias
        # spelled exactly like a registered subpath's dotted form would
        # permanently steal every "<jsonfield>.<subpath>:" query from that
        # subpath, with no diagnostic. Runs over the fully-registered
        # self._by_name, so it catches the collision regardless of whether
        # the JSON field or the colliding plain name/alias was registered
        # first.
        for spec in self._specs:
            if spec.kind is not FieldKind.JSON:
                continue
            for subpath in spec.subpaths:
                for route_name in (spec.name, *spec.aliases):
                    dotted = f"{route_name}.{subpath}"
                    colliding = self._by_name.get(dotted)
                    if colliding is None:
                        continue
                    collision_desc = (
                        f"canonical name '{dotted}'"
                        if colliding.name == dotted
                        else f"alias '{dotted}'"
                    )
                    raise ValueError(
                        f"Field '{spec.name}': subpath '{subpath}' is shadowed by "
                        f"field '{colliding.name}''s {collision_desc} (make_ref "
                        f"resolves an exact match before ever trying a "
                        f"dotted-subpath split, so '{dotted}:' would always resolve "
                        f"to '{colliding.name}', never this subpath); rename "
                        f"'{colliding.name}' or its colliding name"
                    )

    def resolve(self, ref: FieldRef) -> ResolvedField | None:
        """Resolve a :class:`FieldRef` to a :class:`ResolvedField`.

        The single resolver for both plain fields and JSON subpaths (see
        the module-level design note above ``FieldRef``): a plain ref
        (``json_path is None``) resolves by canonical name, and a JSON
        subpath ref additionally requires ``spec.kind`` be ``JSON`` and
        ``ref.json_path`` be one of ``spec.subpaths``. The returned
        ``ResolvedField`` carries ``ref.json_path`` forward alongside the
        spec, so nothing downstream of this call needs to go back to the
        original ``FieldRef`` to recover it.

        Args:
            ref: The field reference to resolve, normally produced by
                :meth:`make_ref`.

        Returns:
            The resolved field, or None if ``ref`` does not name a
            registered field, or names a subpath that field does not have.
        """
        spec = self._by_name.get(ref.name)
        if spec is None:
            return None
        if ref.json_path is not None and (
            spec.kind is not FieldKind.JSON or ref.json_path not in spec.subpaths
        ):
            return None
        return ResolvedField(spec, ref.json_path)

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
        deliberately *not* recognized here **unless the spec declares a
        default subpath** (``SubpathSpec(default=True)``, see
        :attr:`FieldSpec.default_subpath`), in which case the bare name
        resolves to that subpath: ``make_ref("notes")`` returns
        ``FieldRef("notes", "note")``. That is the whole point of declaring
        one, and it exists so a host does not have to rewrite ``notes:`` to
        ``notes.note:`` in the raw query string before parsing: such a
        rewrite cannot see quotes, so it also corrupts
        ``content:"payment notes: none"``, where the same characters are
        ordinary text and not a field prefix at all. An explicitly typed
        subpath still wins: a default only decides what the *bare* name
        means. The reasoning below applies to a JSON field with no declared
        default.

        A JSON field addressed without a subpath has no way to emit
        (``visit_term``/``visit_phrase`` require one), so treating it as
        known here would let ``notes:foo`` parse cleanly and then raise at
        emit time. Note that "clean parse" is
        *not*, on its own, a guarantee that emitting will succeed: a
        text-field range (``title:[a TO b]``) also parses with no
        diagnostics and then raises ``QueryError`` at emit time
        (DIVERGENCES.md entry 5). The real host contract has two parts, both
        documented on :func:`whoosh_compat.emitters.tantivy_.emit` and in the
        README: check ``ParseResult.diagnostics`` before emitting, *and*
        expect emit() to still raise ``QueryError`` for a handful
        of parseable-but-inexecutable shapes. Returning ``None`` here for a
        bare JSON field name closes off one such shape at parse time instead
        of leaving it to that second check; it doesn't mean every other
        shape is closed off the same way. Since ``make_ref`` is also what
        ``FieldsPlugin.do_fieldnames`` calls (via ``__contains__``) to decide
        whether a field prefix is recognized at all, returning ``None`` here
        demotes it the same way an entirely unknown field name already is:
        consistent, not stricter, since a known field addressed incorrectly
        demoting more strictly than an unknown one would be backwards.

        Args:
            raw: The raw field-name text, as captured by the parser's
                fieldname tagger (already alias-as-typed, not yet
                canonicalized).

        Returns:
            A canonical :class:`FieldRef`, or None if ``raw`` names neither
            a registered field nor a registered JSON subpath, nor a JSON
            field with a declared default subpath.
        """
        spec = self._by_name.get(raw)
        if spec is not None:
            if spec.kind is not FieldKind.JSON:
                return FieldRef(spec.name)
            default_subpath = spec.default_subpath
            if default_subpath is not None:
                return FieldRef(spec.name, default_subpath)

        if "." in raw:
            name, subpath = raw.split(".", 1)
            spec = self._by_name.get(name)
            if spec is not None and spec.kind is FieldKind.JSON and subpath in spec.subpaths:
                return FieldRef(spec.name, subpath)

        return None

    def is_bare_json_field(self, raw: str) -> bool:
        """Whether ``raw`` resolves directly (as a canonical name or alias)
        to a JSON-kind spec that is addressed without a subpath *and has no
        way to supply one*, i.e. declares no default subpath.

        A JSON field that declares a default subpath (see
        :class:`SubpathSpec`) is not this shape: ``make_ref`` resolves its
        bare name to that subpath, so it is an ordinary recognized field and
        this returns False. Only a JSON field with no default is "bare" in
        the sense this method means, that is, unresolvable.

        A narrower, separate query from :meth:`make_ref`: ``make_ref``
        deliberately returns ``None`` for this exact shape, so a
        bare JSON field name demotes to a text search when addressed with a
        real term or pattern. But one bare-JSON shape is not a term or
        pattern at all: a lone ``*`` is the existence-check special case
        (mirrored for U64/BOOLEAN_EXISTS in
        ``QueryParser.term_query``, DIVERGENCES.md entries 20 and 29), and
        the emitter still fully supports it for a bare JSON field
        (``visit_every`` needs no subpath). ``FieldsPlugin.do_fieldnames``
        uses this method to detect that one case before demotion applies,
        so ``field:*`` on a bare JSON field can still reach
        :class:`~whoosh_compat.ast.Every` instead of being swallowed by the
        general demotion.

        Args:
            raw: The raw field-name text, as captured by the parser's
                fieldname tagger (already alias-as-typed, not yet
                canonicalized).

        Returns:
            True if ``raw`` names a registered JSON field directly (not via
            a dotted subpath lookup) that declares no default subpath.
        """
        spec = self._by_name.get(raw)
        return spec is not None and spec.kind is FieldKind.JSON and spec.default_subpath is None

    def exists_strategy(self, spec: FieldSpec) -> ExistsStrategy | None:
        """Return ``spec``'s resolved "exists" execution strategy.

        Resolved once, at registry construction, from ``spec.kind`` and
        ``spec.fast`` (see ``resolve_exists_strategy``); ``None`` means the
        field has no way to answer "exists" while non-fast and not
        TEXT/KEYWORD. Shared by ``Every(field)`` and BOOLEAN_EXISTS emission
        so the two agree by construction rather than by parallel capability
        checks.

        Validates ``spec`` against this registry by *identity*, not merely
        by name: keying purely on ``spec.name`` would make a foreign spec
        (one this registry never registered) return ``None``,
        indistinguishable from "this registry's own field of that name has
        no way to answer 'exists'", and would let a spec from a
        *different* registry that happens to share a name silently borrow
        this registry's strategy for that name instead of being refused.

        Args:
            spec: A ``FieldSpec`` registered in this registry.

        Returns:
            The resolved ``ExistsStrategy``, or ``None`` if unsupported.

        Raises:
            ValueError: ``spec`` is not the exact object this registry
                registered under ``spec.name`` (an unregistered name, or a
                different spec object that happens to share one).
        """
        if self._by_name.get(spec.name) is not spec:
            raise ValueError(
                f"exists_strategy() called with a FieldSpec named {spec.name!r} "
                f"that is not registered in this registry"
            )
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
