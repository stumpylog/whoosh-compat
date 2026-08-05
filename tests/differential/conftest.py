import pytest

from tests.differential.oracle import ORACLE_REGISTRY


@pytest.fixture
def oracle_reg():
    return ORACLE_REGISTRY
