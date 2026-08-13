"""Whoosh-compatible query-language parser.

The parser is a fork of whoosh's plugin-driven ``qparser``: tokenizing/tagging
lives in :mod:`whoosh_compat.parser.taggers`/:mod:`whoosh_compat.parser.text`,
the intermediate syntax tree in :mod:`whoosh_compat.parser.syntax`, the
plugins that drive tagging/filtering in :mod:`whoosh_compat.parser.plugins`,
and the top-level parser classes in :mod:`whoosh_compat.parser.default`.

.. warning::
   This entire module is **internal** and not part of the stable API. While
   the classes exported here (``QueryParser``, ``MultifieldParser``) are
   importable and usable, they are subject to change without notice between
   whoosh-compat releases. For new code, use the top-level
   :func:`whoosh_compat.parse` function instead.
"""

from whoosh_compat.parser.default import MultifieldParser
from whoosh_compat.parser.default import QueryParser

__all__ = ["MultifieldParser", "QueryParser"]
