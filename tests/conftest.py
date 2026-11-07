import os
import secrets
import subprocess
from pathlib import Path

import pytest

from agentco.config import CODER_ROLES, RoleModel, TenantConfig
from agentco.roles import Role
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


@pytest.fixture
def loop_cfg(tmp_path) -> TenantConfig:
    """Unit-test config for the task loop: sqlite under tmp_path, no external services."""
    roles = {
        role: RoleModel(
            model=f"m-{role.value}",
            family="coder-fam" if role in CODER_ROLES else "checker-fam",
        )
        for role in Role
        if role is not Role.HUMAN
    }
    return TenantConfig(
        tenant="acme",
        database_url=f"sqlite:///{tmp_path}/loop.db",
        roles=roles,
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def make_repo(tmp_path):
    """Factory: a repo on `main` with one commit, pushed to a bare `origin`."""

    def make(files: dict[str, str], name: str = "repo") -> Path:
        repo = tmp_path / name
        repo.mkdir()
        origin = tmp_path / f"{name}.git"
        _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "initial")
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-u", "origin", "main")
        return repo

    return make
