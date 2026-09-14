"""The time-on-a-period diagnostic message (DIVERGENCES.md entry 62), written
once for every test that checks it.
"""

from whoosh_compat.errors import Diagnostic


def time_on_period_message(value: str, unit: str) -> str:
    """The message for ``value``, which pairs a time of day with a whole
    ``unit`` ("month", "year", "week", "quarter").
    """
    return f"{value!r} pairs a time of day with a whole {unit}; name a day, or drop the time"


def is_time_on_period(diag: Diagnostic) -> bool:
    """Whether ``diag`` is a time-on-a-period rejection, whatever its value
    and unit.
    """
    return " pairs a time of day with a whole " in diag.message
