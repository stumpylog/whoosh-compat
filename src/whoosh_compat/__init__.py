"""whoosh-compat: parse whoosh-style query strings into a backend-neutral AST.

The public surface is small and deliberately shaped like a drop-in for code
that used to build ``whoosh`` query objects directly:

* :func:`parse`: turn a query string into a :class:`ParseResult` (a
  normalized :class:`whoosh_compat.ast.Node` tree plus any diagnostics
  collected while parsing).
* :mod:`whoosh_compat.ast`: the backend-neutral query AST.
* :mod:`whoosh_compat.fields`: :class:`FieldSpec`/:class:`FieldRegistry`,
  used to describe the schema the parser should parse queries against.
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
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import QueryParserError
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.errors import WhooshCompatError
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec
from whoosh_compat.fields import Multitoken
from whoosh_compat.parser.dateparse import DateParserPlugin
from whoosh_compat.parser.default import MultifieldParser

__version__ = importlib.metadata.version("whoosh-compat")

__all__ = [
    "Diagnostic",
    "DiagnosticKind",
    "FieldKind",
    "FieldRef",
    "FieldRegistry",
    "FieldSpec",
    "Multitoken",
    "ParseResult",
    "QueryEmitError",
    "QueryParserError",
    "UnsupportedQueryError",
    "WhooshCompatError",
    "ast",
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
    """

    parser = MultifieldParser(list(default_fields), registry, fieldboosts=field_boosts)

    if any(spec.kind in (FieldKind.DATE, FieldKind.DATETIME) for spec in registry):
        resolved_tz = tz or UTC
        parser.add_plugin(DateParserPlugin(basedate or datetime.now(resolved_tz), resolved_tz))

    node = parser.parse(query)
    node = ast.normalize(node)
    return ParseResult(ast=node, diagnostics=tuple(parser.diagnostics))
