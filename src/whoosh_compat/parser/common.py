# Copyright 2010 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

"""
This module contains common utility objects/functions for the other query
parser modules.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any
from typing import TextIO

from whoosh_compat import ast
from whoosh_compat.errors import QueryParserError

__all__ = ["QueryParserError", "attach", "get_single_text", "print_debug"]


def get_single_text(field: Any, text: str, **kwargs: Any) -> Any:
    """Returns the first token from an analyzer's output.
    """

    for t in field.process_text(text, mode="query", **kwargs):
        return t


def attach(q: Any, stxnode: Any) -> Any:
    """Copy the ``startchar``/``endchar`` span from a syntax node onto a
    query/AST node.

    ``whoosh_compat.ast`` nodes are frozen dataclasses, so they can't be
    mutated in place; for those, this returns a *new* instance built via
    ``dataclasses.replace`` with the span fields set. Non-dataclass query
    objects (if any) are still mutated in place as before.
    """

    if not q:
        return q

    if dataclasses.is_dataclass(q) and not isinstance(q, type):
        result = dataclasses.replace(q, startchar=stxnode.startchar,
                                      endchar=stxnode.endchar)
        return _propagate_boosted_span(result)

    try:
        q.startchar = stxnode.startchar
        q.endchar = stxnode.endchar
    except AttributeError:
        raise AttributeError(  # noqa: B904 (matches whoosh's original re-raise, kept verbatim)
            f"Can't set attribute on {q.__class__.__name__}"
        )
    return q


def _propagate_boosted_span(node: Any) -> Any:
    """Backfills a span-less :class:`~whoosh_compat.ast.Boosted` child with
    the ``Boosted`` node's own (just-attached) span.

    Every call site that builds ``ast.Boosted(child, boost)`` (across
    ``default.py``, ``plugins.py``, ``dateparse.py``, and the ``GroupNode``
    boost-wrap in this module) does so before it knows the span: the
    wrapping syntax node's span is only attached afterwards, via this
    function's caller. That leaves ``child`` permanently span-less once
    ``attach`` replaces the outer node, since ``dataclasses.replace`` only
    touches the top-level instance.

    This is safe because a boosted clause's own span is always exactly its
    child's span: ``BoostPlugin.do_boost`` strips the ``^N`` token out of
    the syntax tree before ``query()`` ever builds the AST (see
    ``BoostPlugin.do_boost``), so boosting never adds characters to the
    span. Recurses through further nested ``Boosted`` layers (e.g. a
    doubly-boosted clause like ``(foo^2)^3``) but stops as soon as it finds
    a child that already has a span, so it never overwrites a span another
    ``attach`` call already set correctly.
    """

    if (isinstance(node, ast.Boosted)
            and node.child.startchar is None
            and node.child.endchar is None):
        child = dataclasses.replace(node.child, startchar=node.startchar,
                                     endchar=node.endchar)
        child = _propagate_boosted_span(child)
        node = dataclasses.replace(node, child=child)
    return node


def print_debug(level: int, msg: str, out: TextIO = sys.stderr) -> None:
    if level:
        out.write(f"{' ' * (level - 1)}{msg}\n")
