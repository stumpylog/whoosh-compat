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
from typing import Any, TextIO

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
        return dataclasses.replace(q, startchar=stxnode.startchar,
                                    endchar=stxnode.endchar)

    try:
        q.startchar = stxnode.startchar
        q.endchar = stxnode.endchar
    except AttributeError:
        raise AttributeError(
            f"Can't set attribute on {q.__class__.__name__}"
        )
    return q


def print_debug(level: int, msg: str, out: TextIO = sys.stderr) -> None:
    if level:
        out.write(f"{' ' * (level - 1)}{msg}\n")
