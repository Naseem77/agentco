import os
import sys
from datetime import UTC, datetime

import pytest
import yaml

from agentco.config import CODER_ROLES, RoleModel, TenantConfig
from agentco.github.client import FakeGitHub
from agentco.llm.copilot import CopilotRuntime
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.repo_contract import load_repo_contract
from agentco.roles import Role
from agentco.sandbox.runner import runner_for
from agentco.store.models import ArtifactKind
from agentco.store.task_store import TaskStore
from agentco.workflow.orchestrator import Orchestrator, TaskContext
from agentco.workflow.states import TaskState

pytestmark = pytest.mark.integration

FILES = {
    "src/calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_mul.py": "from src.calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
}


@pytest.fixture
def repo(make_repo):
    contract = {
        "build": f"{sys.executable} -c 'import src.calc'",
        "test": f"{sys.executable} -m pytest -q",
        "e2e": None,
        "lint": "true",
        "sandbox": {"devcontainer": ".devcontainer"},
        "test_files": f"{sys.executable} -m pytest -q {{files}}",
    }
    return make_repo({**FILES, ".agentco.yml": yaml.safe_dump(contract)})


async def test_task_loop_with_the_real_copilot_runtime(repo, tmp_path):
    if os.environ.get("AGENTCO_IT_COPILOT") != "1":
        pytest.skip("needs AGENTCO_IT_COPILOT=1 and a signed-in Copilot CLI")
    coder = os.environ.get("AGENTCO_IT_CODER_MODEL", "claude-sonnet-4.5")
    checker = os.environ.get("AGENTCO_IT_CHECKER_MODEL", "gpt-5")
    cfg = TenantConfig(
        tenant="itest",
        database_url=f"sqlite:///{tmp_path}/state.db",
        roles={
            role: RoleModel(
                model=coder if role in CODER_ROLES else checker,
                family="coder-fam" if role in CODER_ROLES else "checker-fam",
            )
            for role in Role
            if role is not Role.HUMAN
        },
    )
    store = TaskStore(cfg.database_url)
    store.create_all()
    backend = InMemoryBackend()
    await backend.add(
        graph_name(cfg.tenant, Scope.RND),
        Scope.RND,
        MemoryItem(
            "calc",
            "src/calc.py holds the arithmetic helpers add and mul",
            "integration test",
            datetime.now(UTC),
        ),
    )
    task = store.create_task(cfg.tenant, "add() subtracts", source="cli")
    store.put_artifact(
        task.id,
        ArtifactKind.REQUEST,
        Role.HUMAN,
        {"body": "add(2, 3) returns -1 instead of 5; fix it and add a test."},
    )
    contract = load_repo_contract(repo)
    ctx = TaskContext(
        cfg=cfg,
        store=store,
        runtime=CopilotRuntime(),
        gate=MemoryGate(cfg.tenant, backend),
        github=FakeGitHub([], [], [], {}),
        repo="acme/app",
        repo_root=repo,
        contract=contract,
        runner=runner_for(contract, repo, True),
    )

    async with ctx.runtime:
        final = await Orchestrator(ctx).run(task.id)

    assert final.state in (TaskState.PR_OPEN, TaskState.AWAITING_HUMAN)
    assert final.cost_usd > 0
