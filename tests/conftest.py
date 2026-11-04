import os
import secrets

import pytest

from agentco.config import TenantConfig
from agentco.store.task_store import TaskStore


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


@pytest.fixture
def falkor_cfg() -> TenantConfig:
    """Like cfg, for tests that need FalkorDB but no LLM key."""
    host = os.environ.get("FALKORDB_HOST")
    if not host:
        pytest.skip("integration test needs FALKORDB_HOST")
    return TenantConfig(
        tenant=f"itest{secrets.token_hex(3)}",
        falkordb_host=host,
        falkordb_port=int(os.environ.get("FALKORDB_PORT", "6379")),
        roles={},
    )


@pytest.fixture
def store(tmp_path):
    s = TaskStore(f"sqlite:///{tmp_path}/s.db")
    s.create_all()
    return s
