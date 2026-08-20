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

"""Forked from whoosh's ``qparser/dateparse.py``.

The little English-language date grammar (``Sequence``/``Combo``/``Choice``/
``Bag``/``Regex`` elements feeding an ``adatetime``/``timespan``) is ported
structurally unchanged from whoosh. What differs is everything downstream of
a successful parse:

* Whoosh's ``DateParserPlugin`` built ``whoosh.query.Term``/``DateRange``
  objects directly against a ``schema``, dropping boost in the process
  (DIVERGENCES.md entry 3). Here, :class:`DateParserPlugin` converts Text/Range
  syntax nodes on DATE/DATETIME fields into a single
  :class:`whoosh_compat.ast.DateRange`: an exact instant becomes
  ``DateRange(dt, dt, True, True)``: and preserves boost via
  :class:`whoosh_compat.ast.Boosted`.
* Whoosh's vendored ``whoosh.support.relativedelta`` is replaced by the real
  ``python-dateutil`` (already a core dependency).
* Timezone handling is new: the grammar parses against a *naive local*
  basedate (``basedate.astimezone(tz).replace(tzinfo=None)``); the plugin
  converts results back to aware UTC datetimes at the end, either as
  UTC-midnight calendar days (DATE/``date_only`` fields) or as local instants
  converted to UTC (DATETIME fields): see ``_to_utc``.
* Whoosh represents an ambiguous/period match (e.g. just a year) as a
  ``timespan`` whose ``.end`` is ``adatetime.ceil()`` (the period's last
  microsecond, e.g. ``2020-12-31T23:59:59.999999``). Per paperless#13381,
  this fork instead emits a half-open range: the ceiling plus one
  microsecond as an *exclusive* upper bound (the start of the next period),
  which is exact because ``ceil()`` always lands on a period's last
  microsecond. An exact instant the user actually typed (a plain
  ``datetime``, never a ``timespan``) keeps ``incl_hi=True``.
* Three extensions to ``English``: relative-calendar keywords (``today``,
  ``yesterday``, ``this month``, ``previous month``, ``previous week``,
  ``previous quarter``, ``this year``, ``previous year``, ported from
  paperless's ``_dates.py:_keyword_bounds``; the multi-word ones are also
  accepted unquoted, which whoosh's "a value ends at the first space" rule
  cannot express, see :meth:`DateParserPlugin.do_date_phrases`), a compact
  ``now±<n><unit>`` form (``NowCompact``, alongside the existing ``PlusMinus``/``plusdate``
  element that already gives us whoosh's ``-1 week`` syntax "for free"),
  and an RFC3339 ``T``/``Z`` datetime separator (``simple``'s separator
  character class also accepts ``T``; a trailing ``Z`` is recognized and
  stripped by :meth:`DateParserPlugin._split_rfc3339_utc`, since the
  naive-local grammar below has no tzinfo concept to represent "already
  UTC, no shift" on its own), restoring paperless-ngx's PR #13010
  backward-compatibility behavior for ``created:[2020-01-01T00:00:00Z TO
  ...]``-style bounds.
* Unparseable date text/bounds report a ``Diagnostic(kind=BAD_DATE)`` and
  become an :class:`whoosh_compat.ast.ErrorLeaf`, instead of whoosh's
  ``ErrorNode``/callback mechanism.
"""

from __future__ import annotations

import re
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import tzinfo
from typing import Any
from typing import ClassVar
from typing import cast

from dateutil.relativedelta import relativedelta

from whoosh_compat import ast
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import cause_for
from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRef
from whoosh_compat.fields import FieldSpec
from whoosh_compat.parser import priorities
from whoosh_compat.parser import syntax
from whoosh_compat.parser.common import attach
from whoosh_compat.parser.plugins import Plugin
from whoosh_compat.parser.text import rcompile
from whoosh_compat.parser.times import TimeError
from whoosh_compat.parser.times import adatetime
from whoosh_compat.parser.times import fill_in
from whoosh_compat.parser.times import is_ambiguous
from whoosh_compat.parser.times import is_void
from whoosh_compat.parser.times import relative_days
from whoosh_compat.parser.times import timespan


class DateParseError(Exception):
    """Represents an error in parsing date text."""


def print_debug(level: int, msg: str, *args: Any) -> None:
    if level > 0:
        print(("  " * (level - 1)) + (msg % args))


# Parser element objects

class Props:
    """A dumb little object that just copies a dictionary into attributes so
    I can use dot syntax instead of square bracket string item lookup and
    save a little bit of typing. Used by :class:`Regex`.
    """

    def __init__(self, **args: Any) -> None:
        # NOTE: ``.update()`` (a dict mutation), not ``self.__dict__ =``
        # (an attribute assignment): the latter would route through
        # __setattr__ below and nest the whole dict under a literal
        # "__dict__" key instead of replacing the instance namespace.
        self.__dict__.update(args)

    def __repr__(self) -> str:
        return repr(self.__dict__)

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def __getattr__(self, name: str) -> Any:
        # Props' attributes are whatever named groups the regex happened to
        # capture (set dynamically via ``self.__dict__ = args`` above), so
        # there's no fixed attribute set to declare statically; this makes
        # attribute access on ``Props`` permissive for the ``props_to_date``
        # callbacks below, which is how whoosh's original untyped version
        # behaved.
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value


class ParserBase:
    """Base class for date parser elements."""

    def to_parser(self, e: Any) -> Any:
        if isinstance(e, str):
            return Regex(e)
        else:
            return e

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        raise NotImplementedError

    def date_from(self, text: str, dt: datetime | None = None, pos: int = 0,
                  debug: int = -9999) -> Any:
        if dt is None:
            # Naive local "now", matching the grammar's naive-local-time
            # contract (see DateParserPlugin._local_now).
            dt = datetime.now()  # noqa: DTZ005 (naive local time by contract, see module docstring)

        d, _pos = self.parse(text, dt, pos, debug + 1)
        return d


class MultiBase(ParserBase):
    """Base class for date parser elements such as Sequence and Bag that
    have sub-elements.
    """

    def __init__(self, elements: Any, name: str | None = None) -> None:
        """
        :param elements: the sub-elements to match.
        :param name: a name for this element (for debugging purposes only).
        """

        self.elements = [self.to_parser(e) for e in elements]
        self.name = name

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}<{self.name or ''}>{self.elements!r}"


class Sequence(MultiBase):
    """Merges the dates parsed by a sequence of sub-elements."""

    def __init__(self, elements: Any, sep: str | None = r"(\s+|\s*,\s*)",
                 name: str | None = None, progressive: bool = False) -> None:
        """
        :param elements: the sequence of sub-elements to parse.
        :param sep: a separator regular expression to match between elements,
            or None to not have separators.
        :param name: a name for this element (for debugging purposes only).
        :param progressive: if True, elements after the first do not need to
            match. That is, for elements (a, b, c) and progressive=True, the
            sequence matches like ``a[b[c]]``.
        """

        super().__init__(elements, name)
        self.sep_pattern = sep
        self.sep_expr: re.Pattern[str] | None
        if sep:
            self.sep_expr = rcompile(sep, re.IGNORECASE)
        else:
            self.sep_expr = None
        self.progressive = progressive

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        d: datetime | adatetime = adatetime()
        first = True
        foundall = False
        failed = False

        print_debug(debug, "Seq %s sep=%r text=%r", self.name, self.sep_pattern, text[pos:])
        for e in self.elements:
            print_debug(debug, "Seq %s text=%r", self.name, text[pos:])
            if self.sep_expr and not first:
                print_debug(debug, "Seq %s looking for sep", self.name)
                m = self.sep_expr.match(text, pos)
                if m:
                    pos = m.end()
                else:
                    print_debug(debug, "Seq %s didn't find sep", self.name)
                    break

            print_debug(debug, "Seq %s trying=%r at=%s", self.name, e, pos)

            try:
                at, newpos = e.parse(text, dt, pos=pos, debug=debug + 1)
            except TimeError:
                failed = True
                break

            print_debug(debug, "Seq %s result=%r", self.name, at)
            if not at:
                break
            pos = newpos

            print_debug(debug, "Seq %s adding=%r to=%r", self.name, at, d)
            try:
                d = fill_in(d, at)
            except TimeError:
                print_debug(debug, "Seq %s Error in fill_in", self.name)
                failed = True
                break
            print_debug(debug, "Seq %s filled date=%r", self.name, d)

            first = False
        else:
            foundall = True

        if not failed and (foundall or (not first and self.progressive)):
            print_debug(debug, "Seq %s final=%r", self.name, d)
            return (d, pos)
        else:
            print_debug(debug, "Seq %s failed", self.name)
            return (None, None)


class Combo(Sequence):
    """Parses a sequence of elements in order and combines the dates parsed
    by the sub-elements somehow. The default behavior is to accept two dates
    from the sub-elements and turn them into a range.
    """

    def __init__(self, elements: Any, fn: Any = None, sep: str | None = r"(\s+|\s*,\s*)",
                 min: int = 2, max: int = 2, name: str | None = None) -> None:
        """
        :param elements: the sequence of sub-elements to parse.
        :param fn: a function to run on all dates found. It should return a
            datetime, adatetime, or timespan object. If this argument is None,
            the default behavior accepts two dates and returns a timespan.
        :param sep: a separator regular expression to match between elements,
            or None to not have separators.
        :param min: the minimum number of dates required from the sub-elements.
        :param max: the maximum number of dates allowed from the sub-elements.
        :param name: a name for this element (for debugging purposes only).
        """

        super().__init__(elements, sep=sep, name=name)
        self.fn = fn
        self.min = min
        self.max = max

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        dates: list[Any] = []
        first = True

        print_debug(debug, "Combo %s sep=%r text=%r", self.name, self.sep_pattern, text[pos:])
        for e in self.elements:
            if self.sep_expr and not first:
                print_debug(debug, "Combo %s looking for sep at %r", self.name, text[pos:])
                m = self.sep_expr.match(text, pos)
                if m:
                    pos = m.end()
                else:
                    print_debug(debug, "Combo %s didn't find sep", self.name)
                    return (None, None)

            print_debug(debug, "Combo %s trying=%r", self.name, e)
            at: Any
            newpos: Any
            try:
                at, newpos = e.parse(text, dt, pos, debug + 1)
            except TimeError:
                at, newpos = None, None

            print_debug(debug, "Combo %s result=%r", self.name, at)
            if at is None:
                return (None, None)
            pos = newpos

            first = False
            if is_void(at):
                continue
            if len(dates) == self.max:
                print_debug(debug, "Combo %s length > %s", self.name, self.max)
                return (None, None)
            dates.append(at)

        print_debug(debug, "Combo %s dates=%r", self.name, dates)
        if len(dates) < self.min:
            print_debug(debug, "Combo %s length < %s", self.name, self.min)
            return (None, None)

        return (self.dates_to_timespan(dates), pos)

    def dates_to_timespan(self, dates: Any) -> Any:
        if self.fn:
            return self.fn(dates)
        elif len(dates) == 2:
            return timespan(dates[0], dates[1])
        else:
            raise DateParseError(f"Don't know what to do with {dates!r}")


class Choice(MultiBase):
    """Returns the date from the first of its sub-elements that matches."""

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        print_debug(debug, "Choice %s text=%r", self.name, text[pos:])
        for e in self.elements:
            print_debug(debug, "Choice %s trying=%r", self.name, e)

            try:
                d, newpos = e.parse(text, dt, pos, debug + 1)
            except TimeError:
                d, newpos = None, None
            if d:
                print_debug(debug, "Choice %s matched", self.name)
                return (d, newpos)
        print_debug(debug, "Choice %s no match", self.name)
        return (None, None)


class Bag(MultiBase):
    """Parses its sub-elements in any order and merges the dates."""

    def __init__(self, elements: Any, sep: str = r"(\s+|\s*,\s*)", onceper: bool = True,
                 requireall: bool = False, allof: Any = None, anyof: Any = None,
                 name: str | None = None) -> None:
        """
        :param elements: the sub-elements to parse.
        :param sep: a separator regular expression to match between elements,
            or None to not have separators.
        :param onceper: only allow each element to match once.
        :param requireall: if True, the sub-elements can match in any order,
            but they must all match.
        :param allof: a list of indexes into the list of elements. When this
            argument is not None, this element matches only if all the
            indicated sub-elements match.
        :param anyof: a list of indexes into the list of elements. When this
            argument is not None, this element matches only if any of the
            indicated sub-elements match.
        :param name: a name for this element (for debugging purposes only).
        """

        super().__init__(elements, name)
        self.sep_expr = rcompile(sep, re.IGNORECASE)
        self.onceper = onceper
        self.requireall = requireall
        self.allof = allof
        self.anyof = anyof

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        first = True
        d: datetime | adatetime = adatetime()
        seen = [False] * len(self.elements)

        while True:
            newpos: Any = pos
            print_debug(debug, "Bag %s text=%r", self.name, text[pos:])
            if not first:
                print_debug(debug, "Bag %s looking for sep", self.name)
                m = self.sep_expr.match(text, pos)
                if m:
                    newpos = m.end()
                else:
                    print_debug(debug, "Bag %s didn't find sep", self.name)
                    break

            for i, e in enumerate(self.elements):
                print_debug(debug, "Bag %s trying=%r", self.name, e)

                at: Any
                xpos: Any
                try:
                    at, xpos = e.parse(text, dt, newpos, debug + 1)
                except TimeError:
                    at, xpos = None, None

                print_debug(debug, "Bag %s result=%r", self.name, at)
                if at:
                    if self.onceper and seen[i]:
                        return (None, None)

                    d = fill_in(d, at)
                    newpos = xpos
                    seen[i] = True
                    break
            else:
                break

            pos = newpos
            if self.onceper and all(seen):
                break

            first = False

        if (not any(seen)
                or (self.allof and not all(seen[p] for p in self.allof))
                or (self.anyof and not any(seen[p] for p in self.anyof))
                or (self.requireall and not all(seen))):
            return (None, None)

        print_debug(debug, "Bag %s final=%r", self.name, d)
        return (d, pos)


class ToEnd(ParserBase):
    """Wraps a sub-element and requires that the end of the sub-element's match
    be the end of the text.
    """

    def __init__(self, element: Any) -> None:
        self.element = element

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.element!r})"

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        d: Any
        newpos: Any
        try:
            d, newpos = self.element.parse(text, dt, pos, debug + 1)
        except TimeError:
            d, newpos = None, None

        if d and newpos == len(text):
            return (d, newpos)
        else:
            return (None, None)


class Regex(ParserBase):
    """Matches a regular expression and maps named groups in the pattern to
    datetime attributes using a function or overridden method.

    There are two points at which you can customize the behavior of this class,
    either by supplying functions to the initializer or overriding methods.

    * The ``modify`` function or ``modify_props`` method takes a ``Props``
      object containing the named groups and modifies its values (in place).
    * The ``fn`` function or ``props_to_date`` method takes a ``Props`` object
      and the base datetime and returns an adatetime/datetime.
    """

    fn: Any = None
    modify: Any = None

    def __init__(self, pattern: str, fn: Any = None, modify: Any = None) -> None:
        self.pattern = pattern
        self.expr = rcompile(pattern, re.IGNORECASE)
        self.fn = fn
        self.modify = modify

    def __repr__(self) -> str:
        return f"<{self.pattern!r}>"

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        m = self.expr.match(text, pos)
        if not m:
            return (None, None)

        props = self.extract(m)
        self.modify_props(props)

        try:
            d = self.props_to_date(props, dt)
        except TimeError:
            d = None

        if d:
            return (d, m.end())
        else:
            return (None, None)

    def extract(self, match: re.Match[str]) -> Props:
        d = match.groupdict()
        for key, value in d.items():
            try:  # noqa: SIM105 (matches whoosh's original try/except/pass, kept verbatim)
                d[key] = int(value)  # type: ignore[call-overload]
            except (ValueError, TypeError):
                pass
        return Props(**d)

    def modify_props(self, props: Props) -> None:
        if self.modify:
            self.modify(props)

    def props_to_date(self, props: Props, dt: datetime) -> Any:
        if self.fn:
            return self.fn(props, dt)
        else:
            args = {}
            for key in adatetime.units:
                args[key] = props.get(key)
            return adatetime(**args)


class Month(Regex):
    def __init__(self, *patterns: str) -> None:
        self.patterns = patterns
        self.exprs = [rcompile(pat, re.IGNORECASE) for pat in self.patterns]

        self.pattern = "(?P<month>" + "|".join(f"({pat})" for pat in self.patterns) + ")"
        self.expr = rcompile(self.pattern, re.IGNORECASE)

    def modify_props(self, p: Props) -> None:
        text = p.month
        for i, expr in enumerate(self.exprs):
            m = expr.match(text)
            if m:
                p.month = i + 1
                break


class PlusMinus(Regex):
    def __init__(self, years: str, months: str, weeks: str, days: str, hours: str,
                 minutes: str, seconds: str) -> None:
        rel_years = f"((?P<years>[0-9]+) *({years}))?"
        rel_months = f"((?P<months>[0-9]+) *({months}))?"
        rel_weeks = f"((?P<weeks>[0-9]+) *({weeks}))?"
        rel_days = f"((?P<days>[0-9]+) *({days}))?"
        rel_hours = f"((?P<hours>[0-9]+) *({hours}))?"
        rel_mins = f"((?P<mins>[0-9]+) *({minutes}))?"
        rel_secs = f"((?P<secs>[0-9]+) *({seconds}))?"

        self.pattern = (
            rf"(?P<dir>[+-]) *{rel_years} *{rel_months} *{rel_weeks} *{rel_days} *{rel_hours} *{rel_mins} *{rel_secs}(?=(\W|$))"
        )
        self.expr = rcompile(self.pattern, re.IGNORECASE)

    def props_to_date(self, p: Props, dt: datetime) -> datetime:
        direction = -1 if p.dir == "-" else 1

        delta = relativedelta(years=(p.get("years") or 0) * direction,
                              months=(p.get("months") or 0) * direction,
                              weeks=(p.get("weeks") or 0) * direction,
                              days=(p.get("days") or 0) * direction,
                              hours=(p.get("hours") or 0) * direction,
                              minutes=(p.get("mins") or 0) * direction,
                              seconds=(p.get("secs") or 0) * direction)
        return dt + delta


class NowCompact(Regex):
    """Matches the compact ``now±<n><unit>`` form (``now-7d``, ``now+1h``),
    alongside whoosh's own ``PlusMinus`` (``-1 week``). Units are
    case-sensitive so ``M`` (month) and ``m`` (minute) don't collide; this
    class deliberately does not compile with ``re.IGNORECASE`` like the
    other :class:`Regex` subclasses.
    """

    _UNITS: ClassVar[dict[str, str]] = {
        "y": "years", "M": "months", "w": "weeks", "d": "days",
        "h": "hours", "m": "minutes", "s": "seconds",
    }

    def __init__(self) -> None:
        self.pattern = r"now(?P<sign>[+-])(?P<n>[0-9]+)(?P<unit>[yMwdhms])(?=(\W|$))"
        self.expr = rcompile(self.pattern)
        self.fn = None
        self.modify = None

    def props_to_date(self, p: Props, dt: datetime) -> datetime:
        direction = -1 if p.sign == "-" else 1
        kwargs = {self._UNITS[p.unit]: p.n * direction}
        return dt + relativedelta(**kwargs)


class Daynames(Regex):
    def __init__(self, next: str, last: str, daynames: tuple[str, ...]) -> None:
        self.next_pattern = next
        self.last_pattern = last
        self._dayname_exprs = tuple(rcompile(pat, re.IGNORECASE) for pat in daynames)
        dn_pattern = "|".join(daynames)
        self.pattern = f"(?P<dir>{next}|{last}) +(?P<day>{dn_pattern})(?=(\\W|$))"
        self.expr = rcompile(self.pattern, re.IGNORECASE)

    def props_to_date(self, p: Props, dt: datetime) -> adatetime:
        direction = -1 if re.match(p.dir, self.last_pattern) else 1

        daynum = 0
        for daynum, expr in enumerate(self._dayname_exprs):  # noqa: B007 (daynum is used below, after the loop)
            m = expr.match(p.day)
            if m:
                break
        current_daynum = dt.weekday()
        days_delta = relative_days(current_daynum, daynum, direction)

        d = dt.date() + timedelta(days=days_delta)
        return adatetime(year=d.year, month=d.month, day=d.day)


class Time12(Regex):
    def __init__(self) -> None:
        self.pattern = (r"(?P<hour>[1-9]|10|11|12)(:(?P<mins>[0-5][0-9])"
                        r"(:(?P<secs>[0-5][0-9])(\.(?P<usecs>[0-9]{1,5}))?)?)?"
                        r"\s*(?P<ampm>am|pm)(?=(\W|$))")
        self.expr = rcompile(self.pattern, re.IGNORECASE)

    def props_to_date(self, p: Props, dt: datetime) -> adatetime:
        isam = p.ampm.lower().startswith("a")

        if p.hour == 12:
            hr = 0 if isam else 12
        else:
            hr = p.hour
            if not isam:
                hr += 12

        return adatetime(hour=hr, minute=p.mins, second=p.secs, microsecond=p.usecs)


# Top-level parser classes

class DateParser:
    """Base class for locale-specific parser classes."""

    # Assigned by subclasses' setup() (e.g. English.setup() builds the full
    # grammar and assigns self.all); declared here so get_parser() type-checks.
    all: Any

    day = Regex(r"(?P<day>([123][0-9])|[1-9])(?=(\W|$))(?!=:)",
                lambda p, dt: adatetime(day=p.day))
    year = Regex(r"(?P<year>[0-9]{4})(?=(\W|$))",
                 lambda p, dt: adatetime(year=p.year))
    time24 = Regex(r"(?P<hour>([0-1][0-9])|(2[0-3])):(?P<mins>[0-5][0-9])"
                   r"(:(?P<secs>[0-5][0-9])(\.(?P<usecs>[0-9]{1,5}))?)?"
                   r"(?=(\W|$))",
                   lambda p, dt: adatetime(hour=p.hour, minute=p.mins,
                                           second=p.secs, microsecond=p.usecs))
    time12 = Time12()

    def __init__(self) -> None:
        simple_year = "(?P<year>[0-9]{4})"
        simple_month = "(?P<month>[0-1][0-9])"
        simple_day = "(?P<day>[0-3][0-9])"
        simple_hour = "(?P<hour>([0-1][0-9])|(2[0-3]))"
        simple_minute = "(?P<minute>[0-5][0-9])"
        simple_second = "(?P<second>[0-5][0-9])"
        simple_usec = "(?P<microsecond>[0-9]{6})"

        tup = (simple_year, simple_month, simple_day, simple_hour,
               simple_minute, simple_second, simple_usec)
        # "T" joins the existing space/dash/dot/colon/slash separator class
        # (case-insensitive via Sequence's rcompile(..., re.IGNORECASE), so
        # lowercase "t" is accepted too) so an RFC3339 datetime like
        # "2026-01-01T00:00:00" parses the same shape as the already-working
        # space-separated "2026-01-01 00:00:00": paperless-ngx's PR #13010
        # restored this exact backward-compatibility behavior for the old
        # Whoosh-backed search (see paperless's own
        # ``_translate.py::_bound_datetimes``'s ``if "T" in token:`` branch),
        # which whoosh-compat's DateParserPlugin.date_from below re-derives
        # correctly instead of shelling out to datetime.fromisoformat: a
        # trailing "Z" UTC designator is handled separately (see
        # _split_rfc3339_utc), since this naive grammar has no tzinfo
        # concept of its own to represent "no shift, already UTC" (see the
        # module docstring's "Timezone handling is new" paragraph).
        simple_seq = Sequence(tup, sep="[- .:/T]*", name="simple", progressive=True)
        self.simple = Sequence((simple_seq, r"(?=(\s|$))"), sep="")

        self.setup()

    def setup(self) -> None:
        raise NotImplementedError

    def get_parser(self) -> Any:
        return self.all

    def parse(self, text: str, dt: datetime, pos: int = 0, debug: int = -9999) -> Any:
        parser = self.get_parser()

        d, newpos = parser.parse(text, dt, pos=pos, debug=debug)
        if isinstance(d, (adatetime, timespan)):
            d = d.disambiguated(dt)

        return (d, newpos)

    def date_from(self, text: str, basedate: datetime | None = None, pos: int = 0,
                  debug: int = -9999, toend: bool = True) -> Any:
        if basedate is None:
            # Naive UTC "now": whoosh's own MultiBase.date_from uses
            # datetime.utcnow() for this fallback (unlike ParserBase.date_from
            # above, which uses naive local time), so this preserves that
            # same asymmetry rather than introducing a new one.
            basedate = datetime.now(UTC).replace(tzinfo=None)

        parser = self.get_parser()
        if toend:
            parser = ToEnd(parser)

        d = parser.date_from(text, basedate, pos=pos, debug=debug)
        if isinstance(d, (adatetime, timespan)):
            d = d.disambiguated(basedate)
        return d


class English(DateParser):
    day = Regex(r"(?P<day>([123][0-9])|[1-9])(st|nd|rd|th)?(?=(\W|$))",
                lambda p, dt: adatetime(day=p.day))

    def setup(self) -> None:
        self.plusdate = PlusMinus("years|year|yrs|yr|ys|y",
                                  "months|month|mons|mon|mos|mo",
                                  "weeks|week|wks|wk|ws|w",
                                  "days|day|dys|dy|ds|d",
                                  "hours|hour|hrs|hr|hs|h",
                                  "minutes|minute|mins|min|ms|m",
                                  "seconds|second|secs|sec|s")
        self.nowcompact = NowCompact()

        self.dayname = Daynames("next", "last",
                                ("monday|mon|mo", "tuesday|tues|tue|tu",
                                 "wednesday|wed|we", "thursday|thur|thu|th",
                                 "friday|fri|fr", "saturday|sat|sa",
                                 "sunday|sun|su"))

        midnight = Regex("midnight", lambda p, dt: adatetime(hour=0, minute=0, second=0,
                                                              microsecond=0))
        noon = Regex("noon", lambda p, dt: adatetime(hour=12, minute=0, second=0,
                                                      microsecond=0))
        now = Regex("now", lambda p, dt: dt)

        self.time = Choice((self.time12, self.time24, midnight, noon, now), name="time")

        def tomorrow_to_date(p: Props, dt: datetime) -> adatetime:
            d = dt.date() + timedelta(days=1)
            return adatetime(year=d.year, month=d.month, day=d.day)
        tomorrow = Regex("tomorrow", tomorrow_to_date)

        def yesterday_to_date(p: Props, dt: datetime) -> adatetime:
            d = dt.date() + timedelta(days=-1)
            return adatetime(year=d.year, month=d.month, day=d.day)
        yesterday = Regex("yesterday", yesterday_to_date)

        thisyear = Regex("this year", lambda p, dt: adatetime(year=dt.year))
        thismonth = Regex("this month",
                          lambda p, dt: adatetime(year=dt.year, month=dt.month))
        today = Regex("today",
                      lambda p, dt: adatetime(year=dt.year, month=dt.month, day=dt.day))

        def previous_year_to_date(p: Props, dt: datetime) -> adatetime:
            return adatetime(year=dt.year - 1)
        previous_year = Regex("previous year", previous_year_to_date)

        def previous_month_to_date(p: Props, dt: datetime) -> adatetime:
            # Ported from paperless's ``_keyword_bounds`` (PREVIOUS_MONTH branch):
            # month-precision adatetime -> floor()/ceil() give the first/last
            # instant of that calendar month.
            this_first = dt.date().replace(day=1)
            prev_first = this_first - relativedelta(months=1)
            return adatetime(year=prev_first.year, month=prev_first.month)
        previous_month = Regex("previous month", previous_month_to_date)

        def previous_week_to_date(p: Props, dt: datetime) -> timespan:
            # A calendar week doesn't align with any single adatetime unit
            # (it can span a month/year boundary), so this builds an exact
            # timespan directly rather than relying on floor()/ceil(). The
            # end is set one microsecond short of the exclusive boundary so
            # DateParserPlugin's uniform "ceil + 1us -> exclusive" handling
            # (see module docstring) applies here too.
            this_monday = dt.date() - timedelta(days=dt.weekday())
            start = datetime.combine(this_monday - timedelta(weeks=1), time.min)
            end_excl = datetime.combine(this_monday, time.min)
            return timespan(start, end_excl - timedelta(microseconds=1))
        previous_week = Regex("previous week", previous_week_to_date)

        def previous_quarter_to_date(p: Props, dt: datetime) -> timespan:
            quarter_start = date(dt.year, ((dt.month - 1) // 3) * 3 + 1, 1)
            prev_quarter_start = quarter_start - relativedelta(months=3)
            start = datetime.combine(prev_quarter_start, time.min)
            end_excl = datetime.combine(quarter_start, time.min)
            return timespan(start, end_excl - timedelta(microseconds=1))
        previous_quarter = Regex("previous quarter", previous_quarter_to_date)

        self.month = Month("january|jan", "february|febuary|feb", "march|mar",
                           "april|apr", "may", "june|jun", "july|jul",
                           "august|aug", "september|sept|sep", "october|oct",
                           "november|nov", "december|dec")

        # If you specify a day number you must also specify a month... this
        # Choice captures that constraint

        self.dmy = Choice((Sequence((self.day, self.month, self.year), name="dmy"),
                           Sequence((self.month, self.day, self.year), name="mdy"),
                           Sequence((self.year, self.month, self.day), name="ymd"),
                           Sequence((self.year, self.day, self.month), name="ydm"),
                           Sequence((self.day, self.month), name="dm"),
                           Sequence((self.month, self.day), name="md"),
                           Sequence((self.month, self.year), name="my"),
                           self.month, self.year, self.dayname, tomorrow,
                           yesterday, previous_week, previous_quarter,
                           previous_month, previous_year, thisyear, thismonth,
                           today, now,
                           ), name="date")

        self.datetime = Bag((self.time, self.dmy), name="datetime")
        # "simple" (the compact/separated numeric sequence, e.g. "2020-01-01"
        # or "20200304") is tried *before* "datetime" (the named-month/dayname
        # Bag): Choice.parse returns the first sub-element that matches
        # *anything*, even a partial prefix of the input, and doesn't itself
        # enforce full consumption (that's ToEnd's job, applied by
        # DateParser.date_from around the whole "all" grammar). A separated
        # numeric date like "2020-01-01" satisfies self.dmy's lone `self.year`
        # fallback alternative for just its "2020" prefix (the "-" after it
        # passes self.year's `(?=(\W|$))` lookahead), so if "datetime" were
        # tried first it would "succeed" there, discarding "-01-01" and
        # denying "simple" (which needs to see the whole string, including
        # the separators) any chance to run at all. Trying "simple" first
        # means the compact/separated numeric grammar gets first refusal on
        # digit-led input; "datetime" still runs for anything "simple" can't
        # match at all (named months, day names, keywords, ...).
        self.bundle = Choice((self.nowcompact, self.plusdate, self.simple, self.datetime),
                             name="bundle")
        self.torange = Combo((self.bundle, "to", self.bundle), name="torange")

        self.all = Choice((self.torange, self.bundle), name="all")


# QueryParser plugin

class DateParserPlugin(Plugin):
    """Adds parsing of DATE/DATETIME fields against the :class:`English`
    grammar above, converting matched Text/Range syntax nodes into
    :class:`whoosh_compat.ast.DateRange` nodes.

    >>> parser.add_plugin(DateParserPlugin(datetime.now(tz), tz))
    >>> parser.parse("date:'last tuesday'")

    Unlike whoosh's ``DateParserPlugin``, this class does not support the
    "free" undelimited-date tagging mode (``date:last tuesday`` without
    quotes): paperless v2 never used it (``free=False``), and the
    ambiguity/complexity it adds isn't worth carrying for a feature nothing
    exercises. :meth:`do_date_phrases` is not that mode returning by another
    name: it joins a closed list of six two-word keywords (and a time of day
    trailing one of them), never an open-ended run of date words.
    """

    def __init__(self, basedate: datetime, tz: tzinfo, dateparser: DateParser | None = None) -> None:
        """
        :param basedate: an aware datetime representing "now", against which
            relative/keyword dates are resolved.
        :param tz: the timezone the grammar's calendar math (today,
            previous month, ...) operates in, and that DATETIME field bounds
            are converted through on the way back to UTC.
        :param dateparser: a :class:`DateParser` instance; defaults to a
            fresh :class:`English`.

        :raises ValueError: if ``basedate`` is naive (has no ``tzinfo``).
            The library takes an explicit ``tz`` everywhere else, so a
            naive ``basedate`` is rejected rather than silently interpreted
            in the host machine's local zone; pass an aware datetime
            instead, e.g. ``basedate.replace(tzinfo=tz)`` or
            ``datetime.now(tz)``.
        """

        if basedate.tzinfo is None:
            raise ValueError(
                "basedate must be aware (have a tzinfo), not naive: pass an "
                "aware datetime instead, e.g. basedate.replace(tzinfo=tz) or "
                "datetime.now(tz)"
            )

        self.basedate = basedate
        self.tz = tz
        self.dateparser = dateparser or English()

    def filters(self, parser: Any) -> list[tuple[Any, int]]:
        # Run after FieldsPlugin (100) has assigned field names.
        return [(self.do_date_phrases, priorities.FILTER_DATE_PHRASES),
                (self.do_dates, priorities.FILTER_DATES)]

    # -- time helpers --------------------------------------------------

    def _local_now(self) -> datetime:
        """The "now" the grammar parses against: naive, in ``self.tz``."""

        return self.basedate.astimezone(self.tz).replace(tzinfo=None)

    def _to_utc(self, dt_naive: datetime, date_only: bool, *, ceil: bool = False,
                tz: tzinfo | None = None) -> datetime:
        """Converts a naive local datetime produced by the grammar to an
        aware UTC datetime, per the field's storage semantics.

        DATE (``date_only``) fields collapse to UTC-midnight calendar days
        (paperless ``_date_only_range``): only the calendar date matters, no
        timezone offset is applied. DATETIME fields are treated as local
        instants converted to UTC (paperless ``_datetime_range``).

        ``ceil`` requests round-up-to-next-day instead of truncate-down, and
        only ever changes anything when ``date_only`` and ``dt_naive`` has a
        nonzero time-of-day: it is how an exclusive upper bound
        (``incl_hi=False``) stays a same-day-or-later ceiling instead of
        truncating backwards past its own lo bound. A value already exactly
        at its own midnight is left untouched, since it is
        already day-aligned and rounding it up would over-widen the range by
        an extra day it was never asked to cover. Callers pass ``ceil`` for
        the hi side of an exclusive bound only; the lo side, and any
        both-inclusive exact-instant hi, must keep truncating down.

        ``tz`` overrides ``self.tz`` for this call only: an RFC3339 value
        with an explicit trailing "Z" UTC designator (see
        ``_split_rfc3339_utc``) names an absolute instant that must NOT be
        reinterpreted as a local time and shifted again, so its caller
        passes ``tz=UTC`` here instead (a no-op ``replace``, since the value
        is already the naive wall-clock equivalent of a UTC instant).
        """

        if date_only:
            if ceil and dt_naive.time() != time():
                dt_naive = dt_naive + timedelta(days=1)
            return datetime(dt_naive.year, dt_naive.month, dt_naive.day, tzinfo=UTC)
        return dt_naive.replace(tzinfo=tz or self.tz).astimezone(UTC)

    # RFC3339's trailing UTC designator: a bare "Z" (no numeric UTC offset
    # forms like "+00:00" -- out of scope for this gap, see paperless
    # issue/PR #13010, which only ever emits "Z"). Requires a "T" somewhere
    # earlier in the string so an unrelated trailing "z"/"Z" doesn't get
    # misread as this designator (mirrors paperless's own
    # ``_translate.py::_bound_datetimes``'s ``if "T" in token:`` gate).
    _RFC3339_UTC_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?P<body>.*T.*)Z$", re.IGNORECASE
    )

    def _split_rfc3339_utc(self, text: str) -> tuple[str, bool]:
        """Strip a trailing RFC3339 "Z" UTC designator from ``text``, if
        present, and report whether one was found.

        The grammar (``English``) works entirely in naive datetimes with no
        tzinfo concept of its own (see ``times.py``'s module docstring), so
        "Z" can't be represented by handing it to the grammar directly: it
        has to be recognized and handled here, at the one layer that knows
        about timezones at all. The returned ``bool`` tells callers to pass
        ``tz=UTC`` to ``_to_utc`` for this value instead of ``self.tz``, so
        the value is taken as the absolute instant it names rather than
        reinterpreted as local wall-clock time and shifted again.
        """

        m = self._RFC3339_UTC_RE.match(text)
        if m:
            return m.group("body"), True
        return text, False

    def _error(self, node: syntax.SyntaxNode, text: str, spec: FieldSpec) -> DateErrorNode:
        diagnostic = Diagnostic(
            message=f"{text!r} is not a recognizable date",
            kind=DiagnosticKind.BAD_DATE,
            cause=cause_for(DiagnosticKind.BAD_DATE),
            startchar=node.startchar,
            endchar=node.endchar,
            # DATE/DATETIME fields are never JSON (FieldRegistry rejects
            # date_only/comma_values combinations outside those kinds), so
            # this is always a plain field reference; spec.name is already
            # canonical (aliases resolved by the time a spec is in hand).
            field=FieldRef(spec.name),
            field_kind=spec.kind,
            raw_value=text,
        )
        return DateErrorNode(diagnostic)

    # -- filters ---------------------------------------------------------

    @staticmethod
    def _date_spec(registry: Any, fname: str | None) -> FieldSpec | None:
        """The :class:`FieldSpec` ``fname`` names, if it is a date field."""

        if fname is None:
            return None
        ref = registry.make_ref(fname)
        resolved = registry.resolve(ref) if ref is not None else None
        if resolved is None or resolved.spec.kind not in (FieldKind.DATE, FieldKind.DATETIME):
            return None
        spec: FieldSpec = resolved.spec
        return spec

    # The multi-word keyword phrases English.setup() adds to the grammar,
    # as {first word: allowed second words}. Whoosh's own grammar has no
    # multi-word *value* concept at all (a value ends at the first space),
    # so these are only reachable as a quoted value unless something joins
    # the words back together first: which is what do_date_phrases below
    # does, for these six spellings only. Deliberately not derived from the
    # grammar objects: the grammar is a tree of opaque regex elements with
    # no notion of "how many words is this keyword", and a general
    # whitespace-greedy date value would swallow the token after any date
    # field. See DIVERGENCES.md entry 19.
    _KEYWORD_PHRASES: ClassVar[dict[str, frozenset[str]]] = {
        "previous": frozenset({"week", "month", "quarter", "year"}),
        "this": frozenset({"month", "year"}),
    }

    def _is_time_of_day(self, text: str) -> bool:
        """Whether ``text`` alone is a time of day the grammar recognizes
        ("3pm", "13:45", "noon", "midnight", "now").

        Asks the grammar's own ``time`` element rather than re-deriving a
        notion of "looks like a time" here, so the two can't drift apart.
        Only :class:`English` defines that element: a custom ``DateParser``
        that does not returns False for everything, which degrades
        :meth:`_phrase_run` to "never join a trailing time" (the phrase
        itself still joins). That is a silent behavior difference, not an
        error, so it is called out here: a custom parser wanting the
        trailing-time join must expose a ``time`` element like English's.
        """

        element = getattr(self.dateparser, "time", None)
        if element is None:  # a custom DateParser without English's `time`
            return False
        try:
            at, _ = ToEnd(element).parse(text, self._local_now())
        except (ValueError, OverflowError, TimeError):
            # parse() reports bad input through diagnostics and never
            # raises; a value that blows up here simply isn't a time.
            return False
        return at is not None

    def _phrase_words(self, group: syntax.GroupNode, i: int, limit: int) -> list[int]:
        """Indices of up to ``limit`` plain unfielded word nodes following
        ``group[i]``, separated from it and each other by whitespace only.

        Anything else (an operator, a group, a wildcard/prefix/phrase node,
        a word carrying its own field name) ends the run, so only words that
        would otherwise have become default-field terms are candidates for
        joining onto the date value.
        """

        found: list[int] = []
        j = i + 1
        while len(found) < limit and j < len(group):
            node = group[j]
            if node.is_ws():
                j += 1
                continue
            if type(node) is not syntax.WordNode or node.fieldname is not None:
                break
            found.append(j)
            j += 1
        return found

    def _phrase_run(self, group: syntax.GroupNode, i: int) -> list[int] | None:
        """The indices of the nodes making up an unquoted date keyword
        phrase starting at ``group[i]``, or None if there isn't one.

        The run is the two-word phrase (``previous month``), plus a time of
        day *trailing* it (``previous week 3pm``) if one is there. The
        trailing time is part of the run so that the unquoted spelling
        reaches the grammar as the same value the quoted spelling would; the
        grammar, not this join, then decides what that value means (for the
        span-valued keywords: that a time on a period is an unusable date,
        see DIVERGENCES.md entry 52).

        A time *leading* the phrase is deliberately NOT part of the run,
        even though `added:"3pm previous week"` is rejected. The rule is
        that a field prefix binds the next date expression: in
        ``added:3pm previous week`` it finds "3pm", a complete date value,
        and is satisfied, so "previous week" was never combined with the
        time in the first place and stays ordinary free text (which is what
        paperless v2 did with it, and what this fork must keep doing).
        Quoting is what forces the two into one expression, and only then
        is there an incoherent combination to reject. Do not "fix" this
        into symmetry with the trailing case: the two spellings differ
        because they bind differently, not by oversight.
        """

        # Three words is the longest run that can exist: the two-word phrase
        # plus a trailing time. A fourth word is never part of the value.
        idxs = [i, *self._phrase_words(group, i, limit=2)]
        words = [cast(str, group[j].text).lower() for j in idxs]

        if len(words) < 2 or words[1] not in self._KEYWORD_PHRASES.get(words[0], ()):
            return None

        end = 2
        if len(words) > end and self._is_time_of_day(words[end]):
            end += 1
        return idxs[:end]

    def do_date_phrases(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        """Join an unquoted multi-word date keyword phrase on a date field
        back into a single value node, so ``added:previous month`` resolves
        exactly like ``added:"previous month"``.

        The join is limited to the phrases in ``_KEYWORD_PHRASES`` (plus a
        trailing time of day, see :meth:`_phrase_run`) on a field explicitly
        named in the query: everything else about date-field parsing keeps
        ending at the first space, as whoosh's grammar does.

        One consequence of requiring the *following* words to carry no
        field name of their own (the guard that keeps
        ``added:previous title:month`` two separate values): inside a
        parenthesized group, ``added:(previous month)``, the field name has
        already been propagated onto both words by FieldsPlugin, so the run
        breaks and the phrase is not joined, while
        ``added:("previous month")`` is a single value already and works.
        Left as is rather than widened: the host rewrite this replaces also
        only ever fired on a phrase directly following ``field:``, so
        nothing regresses (DIVERGENCES.md entry 19).
        """

        registry = parser.registry

        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                group[i] = self.do_date_phrases(parser, node)

        i = 0
        while i < len(group):
            head = group[i]
            # An explicitly fielded bare word only: reaching a phrase through
            # the *default* field would mean joining words that carry no date
            # field of their own, a much wider claim on the query than "the
            # user wrote added: in front of it".
            if (type(head) is syntax.WordNode
                    and head.fieldname is not None
                    and self._date_spec(registry, head.fieldname) is not None):
                idxs = self._phrase_run(group, i)
                if idxs is not None:
                    last = group[idxs[-1]]
                    joined = syntax.WordNode(
                        " ".join(cast(str, group[j].text) for j in idxs)
                    )
                    joined.set_fieldname(head.fieldname)
                    # No boost is carried over: BoostPlugin applies one at
                    # FILTER_BOOSTS_POST (510), long after this filter, so
                    # `added:previous month^2` boosts the joined node and
                    # arrives as Boosted(DateRange, 2.0) on its own.
                    joined.startchar = head.startchar
                    joined.endchar = last.endchar
                    group[i:idxs[-1] + 1] = [joined]
            i += 1

        return group

    def do_dates(self, parser: Any, group: syntax.GroupNode) -> syntax.GroupNode:
        registry = parser.registry

        for i, node in enumerate(group):
            if isinstance(node, syntax.GroupNode):
                group[i] = self.do_dates(parser, node)
                continue

            fname = (node.fieldname if node.has_fieldname else None) or parser.fieldname
            spec = self._date_spec(registry, fname)
            if spec is None:
                continue

            new_node: syntax.SyntaxNode
            if isinstance(node, syntax.RangeNode):
                new_node = self.range_to_node(node, spec)
            elif node.has_text:
                new_node = self.text_to_node(node, spec)
            else:
                continue

            new_node.startchar = node.startchar
            new_node.endchar = node.endchar
            group[i] = new_node

        return group

    def text_to_node(self, node: syntax.SyntaxNode, spec: FieldSpec) -> syntax.SyntaxNode:
        text: str = node.text  # type: ignore[attr-defined]
        try:
            return self._text_to_node(node, spec, text)
        except (ValueError, OverflowError):
            # Years at the edges of what datetime can represent (year 0, or
            # year 9999 whose exclusive ceiling lands past datetime.max)
            # fail in the arithmetic rather than in the grammar. Parsing
            # reports bad input through diagnostics, so treat these as an
            # unrecognizable date like any other.
            return self._error(node, text, spec)

    def _text_to_node(
        self, node: syntax.SyntaxNode, spec: FieldSpec, text: str
    ) -> syntax.SyntaxNode:
        local_now = self._local_now()
        parse_text, force_utc = self._split_rfc3339_utc(text)
        result = self.dateparser.date_from(parse_text, local_now)
        if result is None:
            return self._error(node, text, spec)
        value_tz = UTC if force_utc else self.tz

        if isinstance(result, timespan) and result.start != result.end:
            # By construction (see module docstring), a timespan's start/end
            # are always concrete datetimes by the time DateParser.date_from
            # has disambiguated an adatetime: never an ambiguous adatetime
            # itself: so these casts just narrow times.py's more general
            # DateLike union for mypy.
            lo_naive = cast(datetime, result.start)
            hi_naive: datetime | None = cast(datetime, result.end) + timedelta(microseconds=1)
            incl_lo, incl_hi = True, False
        else:
            # Either a plain datetime, or a degenerate (start == end)
            # timespan: text like "midnight"/"noon" disambiguates to an
            # adatetime whose time-of-day is fully specified but whose
            # date is not, which times.py's timespan.disambiguated() fills
            # in from basedate on *both* sides identically (see its
            # has_no_date branch), producing a zero-width timespan rather
            # than a plain datetime. That is still an exact instant, not an
            # ambiguous period, so it gets the same both-inclusive
            # treatment as the plain-datetime case instead of an
            # off-by-one-microsecond half-open range.
            lo_naive = hi_naive = cast(datetime, result.start if isinstance(result, timespan) else result)
            incl_lo = incl_hi = True

        lo = self._to_utc(lo_naive, spec.date_only, tz=value_tz)
        hi = (
            self._to_utc(hi_naive, spec.date_only, ceil=not incl_hi, tz=value_tz)
            if hi_naive is not None
            else None
        )
        boost = node.boost if node.has_boost else 1.0  # type: ignore[attr-defined]
        return DateRangeSyntaxNode(spec.name, lo, hi, incl_lo, incl_hi, boost)

    def range_to_node(self, node: syntax.RangeNode, spec: FieldSpec) -> syntax.SyntaxNode:
        try:
            return self._range_to_node(node, spec)
        except (ValueError, OverflowError):
            # See text_to_node: a bound at the edge of datetime's range
            # fails in the arithmetic, and must diagnose rather than raise.
            # _range_to_node itself catches and attributes failures on a
            # single, identifiable bound (its own date_from() call, or the
            # bound-specific +1-microsecond/timezone conversion steps); this
            # is only a fallback for whatever those local catches don't
            # cover (e.g. the joint start+end disambiguation step, where the
            # failure genuinely can't be attributed to one bound over the
            # other). Prefer naming the end bound there, since a joint
            # disambiguation failure most commonly comes from the exclusive-
            # ceiling arithmetic on the end side (see the +1 microsecond
            # step just below _range_to_node's combine branch).
            return self._error(node, node.end or node.start or "", spec)

    def _range_to_node(self, node: syntax.RangeNode, spec: FieldSpec) -> syntax.SyntaxNode:
        local_now = self._local_now()

        # Wrap the bare grammar in ToEnd (requiring each bound's text to
        # match in full), but do NOT go through the dateparser's own
        # date_from wrapper. The wrapper bundles two things: the same ToEnd
        # full-match requirement (wanted, because the bare grammar accepts
        # whatever prefix the first successful Choice alternative happened
        # to consume, e.g. just the year out of "2020-06-15", silently
        # discarding the rest of the bound instead of diagnosing it), and
        # per-bound disambiguation against basedate (NOT wanted here). A
        # bracketed range's bounds must stay ambiguous until the combine
        # step below, where timespan.disambiguated()'s joint range
        # heuristics read them together, exactly as whoosh's own
        # range_to_dt does: copy the end's year onto a year-less start,
        # borrow end.year - 1 when the range reads backwards across a year
        # boundary, push the end past midnight for an overnight time
        # range. Disambiguating each bound independently first (as the
        # wrapper does) resolves "[dec to feb]" to December and February
        # of the SAME basedate year, an inverted lo > hi range that
        # matches nothing. See text_to_node, which parses single
        # (non-range) values through the wrapper, where the eager
        # per-value disambiguation is correct.
        bound_parser = ToEnd(self.dateparser.get_parser())
        raw_start: Any = None
        raw_end: Any = None
        # Each bound gets its own RFC3339 "Z" check (see text_to_node /
        # _split_rfc3339_utc): a range can freely mix a "Z"-suffixed bound
        # with a plain local one, e.g.
        # ``created:[2020-01-01T00:00:00Z TO 2020-06-01]``.
        start_tz = self.tz
        end_tz = self.tz
        if node.start:
            start_text, start_utc = self._split_rfc3339_utc(node.start)
            if start_utc:
                start_tz = UTC
            try:
                raw_start = bound_parser.date_from(start_text, local_now)
            except (ValueError, OverflowError):
                return self._error(node, node.start, spec)
            if raw_start is None:
                return self._error(node, node.start, spec)
        if node.end:
            end_text, end_utc = self._split_rfc3339_utc(node.end)
            if end_utc:
                end_tz = UTC
            try:
                raw_end = bound_parser.date_from(end_text, local_now)
            except (ValueError, OverflowError):
                return self._error(node, node.end, spec)
            if raw_end is None:
                return self._error(node, node.end, spec)

        # "Exact" means the bound needs no disambiguation at all (a
        # concrete datetime from the grammar, e.g. "now", or a
        # fully-specified adatetime): such a bound names an instant, so it
        # keeps the bracket inclusivity the user typed and skips the
        # half-open +1-microsecond ceiling below. A timespan bound is
        # never exact (it names a period).
        start_exact = isinstance(raw_start, datetime) or (
            isinstance(raw_start, adatetime) and not is_ambiguous(raw_start)
        )
        end_exact = isinstance(raw_end, datetime) or (
            isinstance(raw_end, adatetime) and not is_ambiguous(raw_end)
        )

        lo_naive: datetime | None = None
        hi_naive: datetime | None = None
        incl_lo, incl_hi = not node.startexcl, not node.endexcl

        # timespan() can't nest a timespan inside itself, so bounds that are
        # already a resolved span (e.g. "previous week") are combined
        # separately below instead of via whoosh's combined disambiguation.
        can_combine = (raw_start is not None and raw_end is not None
                       and not isinstance(raw_start, timespan)
                       and not isinstance(raw_end, timespan))
        if can_combine:
            try:
                ts = timespan(raw_start, raw_end).disambiguated(local_now)
            except (ValueError, OverflowError):
                # The joint step sees both bounds at once, so a failure
                # (e.g. a year-0 bound with no datetime representation)
                # must be re-attributed: the bound that also fails on its
                # own is the culprit; if each is individually fine, name
                # the end bound (matching range_to_node's fallback
                # preference).
                try:
                    if isinstance(raw_start, (adatetime, timespan)):
                        raw_start.disambiguated(local_now)
                except (ValueError, OverflowError):
                    return self._error(node, node.start, spec)
                return self._error(node, node.end, spec)
            lo_naive, hi_naive = cast(datetime, ts.start), cast(datetime, ts.end)
            # A mixed-tz range (one RFC3339 "Z" bound, one local): each
            # bound's tz must follow its VALUE through the joint step's
            # backwards-swap, or the Z bound gets reinterpreted as local
            # wall-clock time and silently shifted. The joint step itself
            # reports whether its swap line fired (bounds_swapped, a
            # whoosh-compat addition to the forked timespan): joint
            # disambiguation may also MUTATE a bound before ordering (year
            # borrowing, equal-years month/day fill), so the swap cannot
            # be inferred from the resolved values, only from the flag.
            if ts.bounds_swapped:
                start_tz, end_tz = end_tz, start_tz
        else:
            if raw_start is not None:
                sd = (raw_start.disambiguated(local_now)
                      if isinstance(raw_start, (adatetime, timespan)) else raw_start)
                lo_naive = cast(datetime, sd.start if isinstance(sd, timespan) else sd)
            if raw_end is not None:
                ed = (raw_end.disambiguated(local_now)
                      if isinstance(raw_end, (adatetime, timespan)) else raw_end)
                hi_naive = cast(datetime, ed.end if isinstance(ed, timespan) else ed)

        def _individual(raw: Any) -> datetime | timespan:
            """A bound's own resolution, ignoring the other bound."""
            if isinstance(raw, (adatetime, timespan)):
                return cast("datetime | timespan", raw.disambiguated(local_now))
            return cast(datetime, raw)

        def _fails_as_lo(raw: Any) -> bool:
            """Would this bound's own value fail where the lo bound failed?"""
            try:
                probed = _individual(raw)
                p_lo = cast(datetime, probed.start if isinstance(probed, timespan) else probed)
                self._to_utc(p_lo, spec.date_only)
            except (ValueError, OverflowError):
                return True
            return False

        def _fails_as_hi(raw: Any) -> bool:
            """Would this bound's own value fail where the hi bound failed?

            Mimics the real hi-side arithmetic: an ambiguous bound (an
            individual timespan) gets the half-open exclusive-ceiling
            adjustment; an exact instant keeps the exclusivity the user
            actually typed on the end bracket (``ceil=not incl_hi``, the
            same flag the real failing site used: an exclusive ``}`` on a
            date_only field ceils the bound up a day, which is exactly the
            arithmetic that can overflow).
            """
            try:
                probed = _individual(raw)
                if isinstance(probed, timespan):
                    p_hi = cast(datetime, probed.end) + timedelta(microseconds=1)
                    self._to_utc(p_hi, spec.date_only, ceil=True)
                else:
                    self._to_utc(probed, spec.date_only, ceil=not incl_hi)
            except (ValueError, OverflowError):
                return True
            return False

        def blame(failing_side: str, default_text: str | None) -> str | None:
            """The bound whose typed text produced the failing value.

            Positional attribution (lo failure -> node.start, hi failure ->
            node.end) is wrong exactly when the joint disambiguation step
            swapped a backwards range (times.py's timespan.disambiguated,
            its "just swap the start and end" branch): the value failing at
            one side came from the OTHER side's text. Rather than
            re-deriving the swap decision, re-attribute only on clear
            evidence of it: the OPPOSITE bound fails the failing side's own
            arithmetic on its own while the positional bound survives that
            same arithmetic. Both-fail and neither-fail keep the positional
            default (either the attribution is genuinely ambiguous, or the
            failure is a joint artifact neither bound reproduces alone).
            The arithmetic is side-specific on purpose: probing a lo-side
            bound with hi-side ceiling arithmetic would blame an innocent
            start bound whose own ceiling merely sits at datetime.max.
            """
            if not can_combine:
                return default_text
            if failing_side == "lo":
                if _fails_as_lo(raw_end) and not _fails_as_lo(raw_start):
                    return node.end
            elif _fails_as_hi(raw_start) and not _fails_as_hi(raw_end):
                return node.start
            return default_text

        if hi_naive is not None and not end_exact:
            try:
                hi_naive = hi_naive + timedelta(microseconds=1)
            except OverflowError:
                # The exclusive-ceiling adjustment (e.g. year 9999's last
                # microsecond plus one lands past datetime.max) happens at
                # the hi side, but after a joint swap the hi VALUE may have
                # come from the start bound's text: blame() decides.
                return self._error(node, blame("hi", node.end) or "", spec)
            incl_hi = False
        if lo_naive is not None and not start_exact:
            incl_lo = True

        # start_tz/end_tz (UTC iff that bound had an RFC3339 "Z" designator,
        # self.tz otherwise) follow each bound's VALUE: the joint branch
        # above swaps them alongside a backwards-range value swap, so by
        # this point lo's tz belongs to lo's value and hi's to hi's.
        try:
            lo = (
                self._to_utc(lo_naive, spec.date_only, tz=start_tz)
                if lo_naive is not None
                else None
            )
        except (ValueError, OverflowError):
            return self._error(node, blame("lo", node.start) or "", spec)
        try:
            hi = (
                self._to_utc(hi_naive, spec.date_only, ceil=not incl_hi, tz=end_tz)
                if hi_naive is not None
                else None
            )
        except (ValueError, OverflowError):
            return self._error(node, blame("hi", node.end) or "", spec)
        # An exclusivity flag is meaningless for a bound that isn't there at
        # all (there's nothing to exclude): normalize it to True/inclusive
        # rather than preserving whatever bracket character the user
        # happened to type, matching real whoosh's own behavior (confirmed
        # directly: `created:{ TO ]` and `created:[ TO ]` both parse to an
        # identical, fully-inclusive-shaped range in the oracle regardless
        # of which bracket was used on the absent side).
        if lo is None:
            incl_lo = True
        if hi is None:
            incl_hi = True
        return DateRangeSyntaxNode(spec.name, lo, hi, incl_lo, incl_hi, node.boost)


class DateRangeSyntaxNode(syntax.SyntaxNode):
    """Produced by :class:`DateParserPlugin` in place of a Text/Range node on
    a DATE/DATETIME field. Always builds a single
    :class:`whoosh_compat.ast.DateRange`: an exact instant is represented
    as ``DateRange(dt, dt, True, True)`` rather than a term, per
    DIVERGENCES.md entry 3 (whoosh emits ``query.Term`` for exact instants and drops
    boost; this fork keeps both a uniform range shape and the boost).
    """

    has_fieldname = True
    has_boost = True
    fieldname: str

    def __init__(self, fieldname: str, lo: datetime | None, hi: datetime | None,
                 incl_lo: bool, incl_hi: bool, boost: float = 1.0) -> None:
        self.fieldname = fieldname
        self.lo = lo
        self.hi = hi
        self.incl_lo = incl_lo
        self.incl_hi = incl_hi
        self.boost = boost

    def r(self) -> str:
        return f"DateRange {self.lo!r}-{self.hi!r}"

    def query(self, parser: Any) -> ast.Node:
        # self.fieldname is always spec.name (see DateParserPlugin.do_dates),
        # a plain canonical field name: DATE/DATETIME fields are never JSON.
        node: ast.Node = ast.DateRange(field=FieldRef(self.fieldname), lo=self.lo, hi=self.hi,
                                        incl_lo=self.incl_lo, incl_hi=self.incl_hi)
        if self.boost != 1.0:
            node = ast.Boosted(node, self.boost)
        return attach(node, self)


class DateErrorNode(syntax.SyntaxNode):
    """Produced when date text/bounds fail to parse; reports a
    ``Diagnostic(kind=BAD_DATE)`` and becomes an :class:`~whoosh_compat.ast.ErrorLeaf`.
    """

    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic

    def r(self) -> str:
        return f"DateError {self.diagnostic.message!r}"

    def query(self, parser: Any) -> ast.Node:
        parser.report(self.diagnostic)
        leaf = ast.ErrorLeaf(diagnostic=self.diagnostic)
        return attach(leaf, self)
