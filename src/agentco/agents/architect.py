"""The Architect: a new design becomes a structured ADR with sub-tasks."""

import posixpath
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply, issue_text
from agentco.config import TenantConfig
from agentco.llm.copilot import permission_allowed
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import RepoContract
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


MAX_CONTRACT_CHARS = 4000


def _shell_policy_error(label: str, command: str) -> str | None:
    command = command.strip()
    call = AgentCall(Role.ARCHITECT, "", "", "", commands=(command,))
    allowed, _ = permission_allowed("shell", call, command=command)
    if not allowed:
        return f"{label} must be one plain command, without shell operators"
    return None


def _contract_problems(contract: RepoContract) -> list[str]:
    commands = {
        "build": contract.build,
        "test": contract.test,
        "e2e": contract.e2e,
        "lint": contract.lint,
        "setup": contract.setup,
        "test_files": contract.test_files
        and contract.test_files.replace("{files}", "a.py"),
    }
    problems = [
        message
        for label, command in commands.items()
        if command and (message := _shell_policy_error(label, command))
    ]
    if contract.env_passthrough:
        problems.append("env_passthrough must stay empty; a human adds it by hand")
    path = contract.sandbox.devcontainer or contract.sandbox.compose or ""
    if path.startswith("/") or ".." in posixpath.normpath(path).split("/"):
        problems.append("sandbox paths must stay inside the repository")
    return problems


class ContractProposal(Reply):
    contract_yaml: str = Field(max_length=MAX_CONTRACT_CHARS)
    rationale: str

    @model_validator(mode="after")
    def _valid_contract(self) -> "ContractProposal":
        try:
            raw = yaml.safe_load(self.contract_yaml)
        except yaml.YAMLError as exc:
            raise ValueError(f"contract_yaml is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("contract_yaml must be a YAML mapping")  # noqa: TRY004
        try:
            contract = RepoContract.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(
                f"contract_yaml is not a valid repo contract: {exc}"
            ) from exc
        if problems := _contract_problems(contract):
            raise ValueError("; ".join(problems))
        return self


async def propose_contract(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    task: TaskRecord,
    repo_root: Path,
    *,
    human_note: str = "",
) -> ContractProposal:
    prompt = f"Propose the .agentco.yml for this repository.\nTask: {task.title}"
    previous = asker.store.latest_artifact(task.id, ArtifactKind.CONTRACT)
    if previous is not None:
        prompt += f"\n\nYour previous draft:\n{previous.body['contract_yaml']}"
    if human_note.strip():
        prompt += f"\n\nHuman guidance:\n{human_note.strip()}"
    call = AgentCall(
        role=Role.ARCHITECT,
        model=role_model(cfg, Role.ARCHITECT).model,
        system=load_prompt(Role.ARCHITECT, "contract"),
        prompt=prompt,
        workdir=repo_root,
        edits=False,
        tools=memory_tools(gate.for_role(Role.ARCHITECT)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    reply = await asker.ask(call, ContractProposal)
    asker.store.put_artifact(
        task.id,
        ArtifactKind.CONTRACT,
        Role.ARCHITECT,
        {"contract_yaml": reply.contract_yaml, "rationale": reply.rationale},
    )
    return reply
