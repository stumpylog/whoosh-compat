"""whoosh-compat: parse whoosh-style query strings into a backend-neutral AST.

The public surface is small and deliberately shaped like a drop-in for code
that used to build ``whoosh`` query objects directly:

* :func:`parse`: turn a query string into a :class:`ParseResult` (a
  normalized :class:`whoosh_compat.ast.Node` tree plus any diagnostics
  collected while parsing).
* :mod:`whoosh_compat.ast`: the backend-neutral query AST.
* :mod:`whoosh_compat.fields`: :class:`FieldSpec`/:class:`FieldRegistry`,
  used to describe the schema the parser should parse queries against.

``PARSE_KINDS`` and ``EMIT_KINDS`` are also exported so a host never needs
its own hardcoded partition of :class:`DiagnosticKind` by phase.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import tzinfo

from whoosh_compat import ast
from whoosh_compat.ast import analyze
from whoosh_compat.ast import free_text_tokens
from whoosh_compat.errors import EMIT_KINDS
from whoosh_compat.errors import PARSE_KINDS
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError
from whoosh_compat.errors import QueryParserError
from whoosh_compat.errors import WhooshCompatError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken
from whoosh_compat.fields import PatternNormalizer
from whoosh_compat.fields import SubpathSpec
from whoosh_compat.parser.dateparse import DateParserPlugin
from whoosh_compat.parser.default import MultifieldParser

__version__ = importlib.metadata.version("whoosh-compat")

__all__ = [
    "EMIT_KINDS",
    "PARSE_KINDS",
    "Cause",
    "Diagnostic",
    "DiagnosticKind",
    "FieldKind",
    "FieldRef",
    "FieldRegistry",
    "FieldSpec",
    "Multitoken",
    "ParseResult",
    "PatternNormalizer",
    "QueryError",
    "QueryParserError",
    "SubpathSpec",
    "WhooshCompatError",
    "analyze",
    "ast",
    "free_text_tokens",
    "parse",
]


@dataclass(frozen=True, slots=True)
class ParseResult:
    """The result of parsing a query string."""

    ast: ast.Node
    diagnostics: tuple[Diagnostic, ...]


def parse(
    query: str,
    *,
    registry: FieldRegistry,
    default_fields: Sequence[str],
    field_boosts: Mapping[str, float] | None = None,
    tz: tzinfo | None = None,
    basedate: datetime | None = None,
) -> ParseResult:
    """Parse ``query`` against ``registry`` and return a :class:`ParseResult`.

    Builds a fresh :class:`~whoosh_compat.parser.default.MultifieldParser`
    for every call (so the function is safe to call concurrently from
    multiple threads), attaches a date-parsing plugin when the registry has
    DATE/DATETIME fields and one is available, and normalizes the resulting
    AST via :func:`whoosh_compat.ast.normalize` before returning it.

    :param query: the query string to parse.
    :param registry: describes the known fields and their kinds/aliases.
    :param default_fields: the fields searched by unfielded terms.
    :param field_boosts: optional per-field boost multipliers, applied only
        to the per-field copies created by unfielded-term expansion (not to
        explicitly-fielded terms).
    :param tz: timezone used to interpret relative/local dates.
    :param basedate: the "now" used to resolve relative dates; defaults to
        the current time in ``tz`` when a date plugin is active.

    :raises ValueError: if ``default_fields`` is empty, ``default_fields``
        names a field ``registry`` doesn't know, a ``field_boosts`` key
        doesn't resolve to a known field, or ``basedate`` is a naive
        (tz-less) datetime. These are host configuration mistakes: the
        registry's own philosophy is that a misconfiguration raises at
        construction, not at query time, and this extends the same bar to
        configuration passed here. The naive-``basedate`` rejection is
        unconditional (it does not depend on whether the registry has any
        DATE/DATETIME fields, even though only those attach the date
        plugin that would read it): whether bad configuration raises must
        not vary with unrelated registry contents. An alias resolves
        normally in both ``default_fields`` and ``field_boosts`` (a
        ``field_boosts`` key is canonicalized to its field's own name
        before use); it is not one of the rejected cases.

    :raises QueryParserError: if the parse pipeline fails unexpectedly.
        Never raised for bad query *input* (that becomes diagnostics); it
        means a defect in this library, and hosts route it to a monitorable
        500. Any other exception escaping the pipeline is converted to one,
        chained as ``__cause__``, so this is the only exception type a
        caller passing ordinary query strings has to be ready for. The
        conversion covers the parse pipeline itself (tagging, filtering,
        ``normalize()``); the argument validation and parser/plugin
        construction above it are deliberately outside it, so a
        configuration mistake still surfaces as the ``ValueError``
        documented above rather than being relabelled a library defect.
    """

    if not default_fields:
        raise ValueError("default_fields must not be empty")
    if basedate is not None and basedate.tzinfo is None:
        # Mirrors DateParserPlugin.__init__'s own check, but hoisted here
        # so the rejection is unconditional: without it, a registry with no
        # DATE/DATETIME fields (where the plugin is never attached) would
        # silently accept and ignore the same misconfiguration.
        raise ValueError(
            "basedate must be timezone-aware (a naive datetime is ambiguous: attach a tzinfo)"
        )
    canonical_fields = []
    for name in default_fields:
        ref = registry.make_ref(name)
        if ref is None:
            raise ValueError(f"default_fields names unknown field {name!r}")
        # Canonicalized (not left as whatever alias/dotted form the caller
        # typed) so a field_boosts key, also canonicalized below, looks up
        # correctly in MultifieldPlugin.do_multifield, which keys its lookup
        # by these same strings.
        canonical_fields.append(str(ref))

    resolved_boosts: dict[str, float] | None = None
    if field_boosts:
        resolved_boosts = {}
        for key, boost in field_boosts.items():
            ref = registry.make_ref(key)
            if ref is None:
                raise ValueError(f"field_boosts key {key!r} does not name a known field")
            resolved_boosts[str(ref)] = boost

    parser = MultifieldParser(canonical_fields, registry, fieldboosts=resolved_boosts)

    if any(spec.kind in (FieldKind.DATE, FieldKind.DATETIME) for spec in registry):
        resolved_tz = tz or UTC
        parser.add_plugin(DateParserPlugin(basedate or datetime.now(resolved_tz), resolved_tz))

    try:
        node = parser.parse(query)
        # _normalize_before_analysis, not plain normalize(): DIVERGENCES.md
        # entry 23's match-all face. ParseResult.ast is
        # documented as normalized-but-not-yet-analyzed, and analysis (at
        # emit time) can still discover an And-sibling term is zero-token;
        # plain normalize() would drop an unfielded Every as the AND
        # identity right here, before analysis ever gets a chance to see
        # whether that sibling survives, discarding the Every for good.
        node = ast._normalize_before_analysis(node)
    except QueryParserError:
        raise
    except Exception as exc:
        # The never-raises invariant is a contract hosts build on, so it
        # needs an enforcement mechanism, not just discipline at every site
        # that can fail. Anything escaping the tag/filter/query pipeline
        # that isn't already a QueryParserError is by definition
        # unanticipated, and the shape review keeps finding is an
        # interpreter-level error (RecursionError, from hierarchy the depth
        # caps structurally cannot see; see ARCHITECTURE.md's "what the caps
        # do not cover") reaching a caller that was told to expect only
        # diagnostics.
        #
        # Deliberately QueryParserError and *not* a Diagnostic: a diagnostic
        # means "your query is bad", which hosts route to a 400, and
        # reporting an unknown internal failure that way would blame the
        # user for a library bug and hide it from monitoring.
        # QueryParserError already means "a defect in this library" and is
        # routed to a monitorable 500. The original is chained so the real
        # defect stays diagnosable from the traceback.
        #
        # KeyboardInterrupt/SystemExit are BaseExceptions and so are not
        # caught here: neither one is a parse failure.
        raise QueryParserError(
            f"parsing failed with an unexpected {type(exc).__name__}; "
            "this is a defect in whoosh-compat, not a bad query"
        ) from exc
    return ParseResult(ast=node, diagnostics=tuple(parser.diagnostics))
