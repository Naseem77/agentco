import os
import secrets

import pytest

from agentco.config import TenantConfig


@pytest.fixture
def cfg() -> TenantConfig:
    """Config for integration tests: random tenant on the FalkorDB from env."""
    host = os.environ.get("FALKORDB_HOST")
    if not host or not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("integration test needs FALKORDB_HOST and OPENAI_API_KEY")
    return TenantConfig(
        tenant=f"itest{secrets.token_hex(3)}",
        falkordb_host=host,
        falkordb_port=int(os.environ.get("FALKORDB_PORT", "6379")),
        roles={},
    )
