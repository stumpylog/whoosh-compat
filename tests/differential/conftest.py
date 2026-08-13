import pytest

from tests.differential.oracle import ORACLE_REGISTRY
from whoosh_compat.fields import FieldRegistry


@pytest.fixture
def oracle_reg() -> FieldRegistry:
    return ORACLE_REGISTRY
