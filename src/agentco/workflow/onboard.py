"""Onboarding: the Architect proposes `.agentco.yml`, a human approves, agentco writes it.

The proposal is a small task of its own (source `onboard`) that never reaches
GitHub. Approving it writes the file to the local checkout, and only when no such
file exists; agentco never commits or pushes it. Each call takes one step, and every
step is safe to repeat after a crash.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentco.agents.architect import ContractProposal, propose_contract
from agentco.config import TenantConfig
from agentco.llm.replies import AgentStuck, Asker, ReplyInvalid
from agentco.llm.runtime import AgentRuntime
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import CONTRACT_FILE, RepoContractError, load_repo_contract
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.escalation import write_escalation
from agentco.workflow.states import TaskState

ONBOARD_SOURCE = "onboard"
WRITTEN_REASON = "contract written locally; commit it yourself"
_TERMINAL = (TaskState.MERGED, TaskState.CANCELLED)
_STEP = "contract"

Status = Literal["exists", "proposed", "waiting", "written", "kept", "rejected"]


@dataclass
class OnboardResult:
    task_id: int | None
    status: Status
    contract_yaml: str = ""


def _latest_task(store: TaskStore, tenant: str, repo: str) -> TaskRecord | None:
    tasks = [
        t
        for t in store.list_tasks(tenant)
        if t.source == ONBOARD_SOURCE and t.source_ref == repo
    ]
    return tasks[-1] if tasks else None


def _id(artifact: Artifact | None) -> int:
    return artifact.id if artifact else 0


def _rejected_key(task: TaskRecord) -> str:
    return f"onboard:{task.tenant}:{task.id}:rejected"


def _pending_yaml(store: TaskStore, task: TaskRecord) -> str:
    contract = store.latest_artifact(task.id, ArtifactKind.CONTRACT)
    return contract.body["contract_yaml"] if contract else ""


def _write_contract(repo_root: Path, text: str) -> Literal["written", "kept"]:
    """Create the file exclusively: an existing file is never touched."""
    path = repo_root / CONTRACT_FILE
    text = text if text.endswith("\n") else text + "\n"
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        try:
            same = path.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        # the same text means a crash after our own write, not someone else's file
        return "written" if same else "kept"
    try:
        load_repo_contract(repo_root)
    except RepoContractError:
        path.unlink()
        raise
    return "written"


async def onboard_repo(
    cfg: TenantConfig,
    store: TaskStore,
    runtime: AgentRuntime,
    gate: MemoryGate,
    repo: str,
    repo_root: Path,
) -> OnboardResult:
    task = _latest_task(store, cfg.tenant, repo)
    open_task = task is not None and task.state not in _TERMINAL
    if os.path.lexists(repo_root / CONTRACT_FILE) and not open_task:
        return OnboardResult(None, "exists")
    # a rejection is reported once; asking again starts a fresh proposal
    if (
        task is not None
        and task.state is TaskState.CANCELLED
        and store.get_meta(_rejected_key(task)) is None
    ):
        store.set_meta(_rejected_key(task), "1")
        return OnboardResult(task.id, "rejected")
    if task is None or task.state in _TERMINAL:
        task = store.create_task(
            cfg.tenant, f"Onboard {repo}", ONBOARD_SOURCE, source_ref=repo
        )
    flow = _Flow(cfg, store, runtime, gate, repo, repo_root)
    if task.state is TaskState.INTAKE:
        task = store.transition(
            task.id, TaskState.ARCHITECT, Role.ARCHITECT, "onboarding"
        )
    if task.state is TaskState.ARCHITECT:
        return await flow.propose(task, "")
    if task.state is TaskState.AWAITING_HUMAN:
        return await flow.resume(task)
    raise ValueError(f"onboarding task {task.id} is {task.state.value}")


@dataclass
class _Flow:
    cfg: TenantConfig
    store: TaskStore
    runtime: AgentRuntime
    gate: MemoryGate
    repo: str
    repo_root: Path

    async def propose(self, task: TaskRecord, note: str) -> OnboardResult:
        store = self.store
        floor = max(
            _id(store.latest_artifact(task.id, ArtifactKind.ESCALATION)),
            _id(store.latest_artifact(task.id, ArtifactKind.HUMAN_DECISION)),
        )
        contract = store.latest_artifact(task.id, ArtifactKind.CONTRACT)
        # a proposal saved before a crash is escalated as it is, not asked for again
        if contract is None or contract.id < floor:
            asker = Asker(self.runtime, store, task.id, self.cfg.price_per_request_usd)
            try:
                await propose_contract(
                    asker,
                    self.cfg,
                    self.gate,
                    task,
                    self.repo_root,
                    human_note=note,
                )
            except (ReplyInvalid, AgentStuck) as exc:
                write_escalation(
                    store,
                    task,
                    "stuck",
                    " ".join(str(exc).split()),
                    options=("approve: try again", "answer: guidance", "reject"),
                    actor=Role.ARCHITECT,
                    step=_STEP,
                )
                return OnboardResult(task.id, "waiting")
        text = _pending_yaml(store, task)
        write_escalation(
            store,
            task,
            "new",
            f"proposed {CONTRACT_FILE} for {self.repo}",
            question=(
                f"Approve this {CONTRACT_FILE}? See it with "
                f"`agentco onboard --repo {self.repo} --repo-path <path>`."
            ),
            options=("approve", "answer: change the contract", "reject"),
            actor=Role.ARCHITECT,
            step=_STEP,
        )
        return OnboardResult(task.id, "proposed", text)

    async def resume(self, task: TaskRecord) -> OnboardResult:
        store = self.store
        escalation = store.latest_artifact(task.id, ArtifactKind.ESCALATION)
        decision = store.latest_artifact(task.id, ArtifactKind.HUMAN_DECISION)
        if escalation is None or decision is None or decision.id < escalation.id:
            return OnboardResult(task.id, "waiting", _pending_yaml(store, task))
        choice = decision.body.get("decision")
        if choice == "reject":
            store.transition(task.id, TaskState.CANCELLED, Role.HUMAN, "rejected")
            store.set_meta(_rejected_key(task), "1")
            return OnboardResult(task.id, "rejected")
        offered = (
            escalation.body.get("category") == "new"
            and escalation.body.get("step") == _STEP
        )
        if choice == "approve" and offered:
            return self._approve(task, escalation)
        if choice not in ("approve", "answer"):
            return OnboardResult(task.id, "waiting", _pending_yaml(store, task))
        task = store.transition(
            task.id, TaskState.ARCHITECT, Role.HUMAN, f"human {choice}"
        )
        note = str(decision.body.get("note", "")) if choice == "answer" else ""
        return await self.propose(task, note)

    def _approve(self, task: TaskRecord, escalation: Artifact) -> OnboardResult:
        """Write what the human saw: the latest proposal older than the escalation."""
        seen = [
            a
            for a in self.store.artifacts(task.id, ArtifactKind.CONTRACT)
            if a.id < escalation.id
        ]
        if not seen:
            raise ValueError(f"onboarding task {task.id} has no proposal to write")
        proposal = ContractProposal.model_validate(seen[-1].body)
        status = _write_contract(self.repo_root, proposal.contract_yaml)
        reason = (
            WRITTEN_REASON
            if status == "written"
            else f"{CONTRACT_FILE} already exists; kept it, wrote nothing"
        )
        self.store.transition(task.id, TaskState.MERGED, Role.HUMAN, reason)
        return OnboardResult(task.id, status, proposal.contract_yaml)
