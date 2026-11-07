import asyncio
import json
import os
from datetime import UTC, datetime

import pytest

from agentco.config import CODER_ROLES, RoleModel, TenantConfig
from agentco.github.client import GhCli, _subprocess_runner
from agentco.llm.fake import FakeRuntime
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState

REPO = os.environ.get("AGENTCO_IT_GH_REPO")
PR = os.environ.get("AGENTCO_IT_PR")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (REPO and PR),
        reason="needs AGENTCO_IT_GH_REPO (owner/name) and AGENTCO_IT_PR (number)",
    ),
]

WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}

LEAD_REPLY = json.dumps(
    {"confidence": 0.9, "classifications": [], "summary": "smoke", "comments": []}
)

TO_PR_OPEN = (
    TaskState.PM,
    TaskState.TRIAGE,
    TaskState.ENGINEERING,
    TaskState.PREFLIGHT,
    TaskState.INTERNAL_REVIEW,
    TaskState.LEAD_DECISION,
    TaskState.PR_OPEN,
)


def _read_only_gh(argv: list[str]) -> str:
    """Real `gh`, but only plain GETs through `gh api`."""
    if argv[:2] != ["gh", "api"] or WRITE_FLAGS & set(argv):
        raise AssertionError(f"integration smoke test must stay read-only: {argv}")
    return _subprocess_runner(argv)


def _artifact_count(store: TaskStore, task_id: int) -> int:
    return sum(len(store.artifacts(task_id, kind)) for kind in ArtifactKind)


def test_run_intake_against_a_real_pr_is_idempotent(tmp_path):
    # run_intake lands with the PR-intake feature; import it late so this module
    # still collects on branches that do not have it.
    from agentco.intake.run import run_intake

    cfg = TenantConfig(
        tenant="itest",
        database_url=f"sqlite:///{tmp_path}/it.db",
        roles={
            role: RoleModel(
                model=f"m-{role.value}",
                family="coder-fam" if role in CODER_ROLES else "checker-fam",
            )
            for role in Role
            if role is not Role.HUMAN
        },
    )
    store = TaskStore(cfg.database_url)
    store.create_all()
    task = store.create_task("itest", "smoke", "cli")
    store.put_artifact(
        task.id, ArtifactKind.TARGET, Role.PM, {"repo": REPO, "main": "main"}
    )
    store.put_artifact(
        task.id,
        ArtifactKind.ISSUE,
        Role.PM,
        {"summary": "smoke", "acceptance_criteria": ["it works"]},
    )
    store.put_artifact(
        task.id,
        ArtifactKind.PR,
        Role.LEAD,
        {"number": int(PR), "url": f"https://github.com/{REPO}/pull/{PR}"},
    )
    for state in TO_PR_OPEN:
        store.transition(task.id, state, Role.LEAD, "smoke")

    gh = GhCli(runner=_read_only_gh)
    gate = MemoryGate("itest", InMemoryBackend())
    runtime = FakeRuntime({Role.LEAD: [LEAD_REPLY] * 5})
    now = datetime.now(UTC)

    first = asyncio.run(run_intake(cfg, store, gh, runtime, gate, REPO, now=now))
    assert first.ok, first.errors
    after_first = _artifact_count(store, task.id)

    second = asyncio.run(run_intake(cfg, store, gh, runtime, gate, REPO, now=now))
    assert second.ok, second.errors
    assert _artifact_count(store, task.id) == after_first
