"""Whoosh-compatible query-language parser.

The parser is a fork of whoosh's plugin-driven ``qparser``: tokenizing/tagging
lives in :mod:`whoosh_compat.parser.taggers`/:mod:`whoosh_compat.parser.text`,
the intermediate syntax tree in :mod:`whoosh_compat.parser.syntax`, the
plugins that drive tagging/filtering in :mod:`whoosh_compat.parser.plugins`,
and the top-level parser classes in :mod:`whoosh_compat.parser.default`.
"""

from whoosh_compat.parser.default import MultifieldParser, QueryParser

__all__ = ["MultifieldParser", "QueryParser"]
