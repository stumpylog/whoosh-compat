"""``QueryParser.diagnostics`` must not leak across ``parse()`` calls.

``whoosh_compat.parse()`` builds a fresh ``MultifieldParser`` per call, which
masks this: the accumulator was never reset, so a caller reusing a single
``QueryParser``/``MultifieldParser`` instance across multiple ``parse()``
calls saw diagnostics from an earlier, unrelated query bleed into a later,
clean one.
"""

from whoosh_compat.fields import FieldRegistry
from whoosh_compat.parser.default import MultifieldParser
from whoosh_compat.parser.default import QueryParser


def test_diagnostics_do_not_leak_between_parses(reg: FieldRegistry) -> None:
    qp = MultifieldParser(["content"], reg)
    qp.parse("asn:notanumber")
    assert qp.diagnostics

    qp.parse("alpha")
    assert qp.diagnostics == []


def test_diagnostics_do_not_leak_between_parses_single_field(reg: FieldRegistry) -> None:
    qp = QueryParser("asn", reg)
    qp.parse("notanumber")
    assert qp.diagnostics

    qp.parse("5")
    assert qp.diagnostics == []
