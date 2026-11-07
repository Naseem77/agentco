from datetime import UTC, datetime

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply
from agentco.config import TenantConfig
from agentco.llm.replies import Asker
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.backend import MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore


def _episode_body(task_id: int, issue: PMReply) -> str:
    lines = [
        f"Issue for task {task_id}: {issue.summary}",
        f"Why: {issue.why}",
        "Acceptance criteria:",
        *(f"- {criterion}" for criterion in issue.acceptance_criteria),
    ]
    if issue.product_notes:
        lines.append(f"Product notes: {issue.product_notes}")
    return "\n".join(lines)


async def run_pm(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    request: str,
) -> PMReply:
    memory = gate.for_role(Role.PM)
    call = AgentCall(
        role=Role.PM,
        model=role_model(cfg, Role.PM).model,
        system=load_prompt(Role.PM),
        prompt=f"Task {task.id}: {task.title}\n\nRequest:\n{request}",
        tools=memory_tools(memory, Scope.PRODUCT),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    issue = await asker.ask(call, PMReply)
    await memory.add(
        Scope.PRODUCT,
        MemoryItem(
            f"task-{task.id}-issue",
            _episode_body(task.id, issue),
            f"agent:{Role.PM.value}",
            datetime.now(UTC),
        ),
    )
    store.put_artifact(task.id, ArtifactKind.ISSUE, Role.PM, issue.model_dump())
    return issue
