"""The Architect: a new design becomes a structured ADR with sub-tasks."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply, issue_text
from agentco.config import TenantConfig
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore

MAX_SUB_TASKS = 8


class SubTask(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    acceptance_criteria: list[str] = Field(min_length=1)
    size: Literal["xs", "s", "m", "l", "xl"]
    why: str = ""


class ADRReply(Reply):
    title: str = Field(min_length=1, max_length=120)
    context: str
    decision: str
    consequences: str
    alternatives: list[str] = Field(default_factory=list)
    new_dependencies: list[str] = Field(default_factory=list)
    sub_tasks: list[SubTask] = Field(min_length=1, max_length=MAX_SUB_TASKS)

    @model_validator(mode="after")
    def _unique_titles(self) -> "ADRReply":
        titles = [s.title.strip().casefold() for s in self.sub_tasks]
        if len(set(titles)) != len(titles):
            raise ValueError("sub-task titles must be unique")
        return self


async def run_architect(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    issue: PMReply,
    *,
    repo_root: Path,
    human_note: str = "",
) -> tuple[ADRReply, Artifact]:
    prompt = issue_text(issue)
    if human_note.strip():
        prompt += f"\n\nHuman guidance:\n{human_note.strip()}"
    call = AgentCall(
        role=Role.ARCHITECT,
        model=role_model(cfg, Role.ARCHITECT).model,
        system=load_prompt(Role.ARCHITECT),
        prompt=prompt,
        workdir=repo_root,
        edits=False,
        tools=memory_tools(gate.for_role(Role.ARCHITECT)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    reply = await asker.ask(call, ADRReply)
    artifact = store.put_artifact(
        task.id,
        ArtifactKind.ADR,
        Role.ARCHITECT,
        {"adr": reply.model_dump(), "human_note": human_note},
    )
    return reply, artifact


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "(none)"


def adr_markdown(adr: ADRReply) -> str:
    tasks = []
    for number, sub in enumerate(adr.sub_tasks, 1):
        criteria = "\n".join(f"   - {c}" for c in sub.acceptance_criteria)
        tasks.append(f"{number}. {sub.title} ({sub.size})\n{criteria}")
    return (
        f"# ADR: {adr.title}\n\n"
        f"## Context\n{adr.context}\n\n"
        f"## Decision\n{adr.decision}\n\n"
        f"## Consequences\n{adr.consequences}\n\n"
        f"## Alternatives\n{_bullets(adr.alternatives)}\n\n"
        f"## New dependencies\n{_bullets(adr.new_dependencies)}\n\n"
        f"## Sub-tasks\n" + "\n".join(tasks) + "\n"
    )
