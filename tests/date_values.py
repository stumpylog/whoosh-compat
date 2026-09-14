"""The registry, "now" and value spellings the date-value tests share
(DIVERGENCES.md entries 62 and 63).
"""

from datetime import UTC
from datetime import datetime

import pytest

import whoosh_compat as wc
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec

BASE = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

# One instance for every test: a FieldRegistry keeps no state beyond what
# its constructor builds.
REGISTRY = FieldRegistry(
    [
        FieldSpec("content", FieldKind.TEXT, analyzer=lambda t: [t.lower()]),
        FieldSpec("added", FieldKind.DATETIME, fast=True),
        FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
    ]
)


def parse_date_query(q: str) -> wc.ParseResult:
    """``q`` parsed against :data:`REGISTRY` in UTC, with "now" at :data:`BASE`."""
    return wc.parse(q, registry=REGISTRY, default_fields=["content"], tz=UTC, basedate=BASE)


# Each way to write a value after "field:", as a format string.
QUOTINGS = {"double-quoted": '"{}"', "single-quoted": "'{}'", "unquoted": "{}"}

QUOTING_PARAMS = [pytest.param(spelling, id=name) for name, spelling in QUOTINGS.items()]

QUOTED_PARAMS = [
    pytest.param(spelling, id=name) for name, spelling in QUOTINGS.items() if name != "unquoted"
]

DATE_FIELD_PARAMS = [
    pytest.param("added", id="datetime"),
    pytest.param("created", id="date"),
]
