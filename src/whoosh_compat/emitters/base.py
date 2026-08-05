"""Backend-neutral emitter protocol."""

from __future__ import annotations

from typing import Any, Protocol

from whoosh_compat import ast


class Emitter(Protocol):
    """Something that turns a query AST into a backend-native query object."""

    def emit(self, node: ast.Node) -> Any:
        """Emit a backend-native query object for ``node``."""
        ...
