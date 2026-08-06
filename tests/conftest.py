import pytest

from whoosh_compat.fields import FieldKind
from whoosh_compat.fields import FieldRegistry
from whoosh_compat.fields import FieldSpec


@pytest.fixture
def reg():
    return FieldRegistry([
        FieldSpec("content", FieldKind.TEXT),
        FieldSpec("title", FieldKind.TEXT),
        FieldSpec("document_type", FieldKind.TEXT, aliases=("type",)),
        FieldSpec("tag", FieldKind.KEYWORD, comma_values=True),
        FieldSpec("tag_id", FieldKind.U64, comma_values=True, fast=True),
        FieldSpec("asn", FieldKind.U64, fast=True),
        FieldSpec("created", FieldKind.DATE, date_only=True, fast=True),
        FieldSpec("added", FieldKind.DATETIME, fast=True),
        FieldSpec("has_tag", FieldKind.BOOLEAN_EXISTS, exists_target="tag_id"),
        FieldSpec("notes", FieldKind.JSON, subpaths=("note", "user")),
    ])
