"""Rig for the orchestrator tests: scripted agents, a fake GitHub, a real temp repo."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from script_runner import FILES, FIXED, NEW_TEST, RED_CMD, ScriptRunner, contract

from agentco.agents.replies import PMReply
from agentco.config import Budgets, TenantConfig
from agentco.github.client import FakeGitHub
from agentco.llm.fake import FakeRuntime
from agentco.llm.runtime import AgentCall, AgentResult
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.roles import Role
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.orchestrator import Orchestrator, TaskContext

STUCK = AgentResult("", 1.0, stuck=True, stuck_reason="SECRET_REASON_XYZ")
SEED_FACT = "src/calc.py holds the arithmetic helpers"

PM_JSON = json.dumps(
    {
        "summary": "Make the arithmetic helpers commutative-safe",
        "why": "callers swap operands",
        "acceptance_criteria": ["add(2, 1) == 3"],
    }
)
RULES_OK = json.dumps({"violations": []})
REVIEW_OK = json.dumps({"findings": [], "summary": "looks fine"})
QA_OK = json.dumps(
    {
        "passed": True,
        "bug_class": "ordering",
        "tests_run": ["pytest"],
        "neighbour_matrix": [],
    }
)
QA_FAIL = json.dumps(
    {
        "passed": False,
        "bug_class": "ordering",
        "tests_run": ["pytest"],
        "neighbour_matrix": [],
        "failures": [
            {"title": "mul broke", "steps": "mul(2, 3)", "expected": "6", "actual": "5"}
        ],
    }
)
LEAD_OK = json.dumps({"confidence": 0.9, "classifications": [], "summary": "ship it"})


def triage_json(route: str = "junior", size: str = "m") -> str:
    return json.dumps({"route": route, "size": size, "risk": "low", "reason": "small"})


def engineer(
    version: int = 1,
    *,
    files: dict[str, str] | None = None,
    summary: str = "reordered operands",
    before: Callable[[AgentCall], None] | None = None,
    no_test_reason: str = "",
) -> Callable[[AgentCall], str]:
    """An engineer step that writes real files into the worktree it is given."""
    written = files or {
        "src/calc.py": FIXED + f"# v{version}\n",
        "tests/test_new.py": NEW_TEST,
    }

    def step(call: AgentCall) -> str:
        assert call.workdir is not None
        if before:
            before(call)
        for rel, text in written.items():
            path = call.workdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return json.dumps(
            {
                "summary": summary,
                "files_touched": list(written),
                "tests_added": [p for p in written if p.startswith("tests/")],
                "no_test_reason": no_test_reason,
            }
        )

    return step


def happy() -> dict[Role, list]:
    return {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()],
        Role.JUNIOR: [engineer(1)],
        Role.REVIEWER: [RULES_OK, REVIEW_OK, RULES_OK],
        Role.QA: [QA_OK],
        Role.LEAD: [LEAD_OK],
    }


@dataclass
class Rig:
    ctx: TaskContext
    store: TaskStore
    runtime: Any
    github: FakeGitHub
    runner: ScriptRunner
    backend: InMemoryBackend
    repo: Path
    origin: Path
    task_id: int

    async def run(self) -> TaskRecord:
        return await Orchestrator(self.ctx).run(self.task_id)

    def new_task(
        self,
        title: str = "Another task",
        source: str = "cli",
        source_ref: str | None = None,
    ) -> int:
        return _new_task(self.store, title, source, source_ref)

    def task(self) -> TaskRecord:
        return self.store.get(self.task_id)

    def transitions(self) -> list[str]:
        return [
            a.detail["to"]
            for a in self.store.audit(self.task_id)
            if a.action == "transition"
        ]

    def edges(self) -> list[tuple[str, str]]:
        return [
            (a.detail["from"], a.detail["to"])
            for a in self.store.audit(self.task_id)
            if a.action == "transition"
        ]

    def escalations(self) -> list[dict]:
        return [
            a.body for a in self.store.artifacts(self.task_id, ArtifactKind.ESCALATION)
        ]

    def calls(self, role: Role) -> list[AgentCall]:
        return [c for c in self.runtime.calls if c.role is role]

    def decide(self, decision: str, note: str = "") -> None:
        self.store.put_artifact(
            self.task_id,
            ArtifactKind.HUMAN_DECISION,
            Role.HUMAN,
            {"decision": decision, "note": note},
        )


def _new_task(
    store: TaskStore, title: str, source: str = "cli", source_ref: str | None = None
) -> int:
    task = store.create_task("acme", title, source, source_ref)
    store.put_artifact(
        task.id, ArtifactKind.REQUEST, Role.HUMAN, {"body": f"please: {title}"}
    )
    return task.id


async def build_rig(
    tmp_path: Path,
    store: TaskStore,
    loop_cfg: TenantConfig,
    make_repo: Callable[..., Path],
    script: dict[Role, list],
    *,
    seed: bool = True,
    max_rounds: int | None = None,
    premium: float = 1.0,
    runner: ScriptRunner | None = None,
    github: FakeGitHub | None = None,
    runtime: Any = None,
) -> Rig:
    repo = make_repo(FILES)
    cfg = loop_cfg
    if max_rounds is not None:
        cfg = cfg.model_copy(update={"budgets": Budgets(max_rounds=max_rounds)})
    backend = InMemoryBackend()
    if seed:
        await backend.add(
            graph_name("acme", Scope.RND),
            Scope.RND,
            MemoryItem("calc", SEED_FACT, "test", datetime.now(UTC)),
        )
    runner = runner or ScriptRunner(codes={RED_CMD: 1})
    github = github or FakeGitHub([], [], [], {})
    runtime = runtime or FakeRuntime(script, premium_requests=premium)
    ctx = TaskContext(
        cfg=cfg,
        store=store,
        runtime=runtime,
        gate=MemoryGate("acme", backend),
        github=github,
        repo="acme/app",
        repo_root=repo,
        contract=contract(),
        runner=runner,
    )
    return Rig(
        ctx,
        store,
        runtime,
        github,
        runner,
        backend,
        repo,
        tmp_path / "repo.git",
        _new_task(store, "Make add commutative-safe"),
    )


def issue_of(rig: Rig) -> PMReply:
    artifact = rig.store.latest_artifact(rig.task_id, ArtifactKind.ISSUE)
    assert artifact is not None
    return PMReply.model_validate(artifact.body)


class CountingRuntime:
    """Wraps a runtime; every call takes a moment, so overlapping calls are observable."""

    def __init__(self, inner: FakeRuntime) -> None:
        self.inner = inner
        self.active = 0
        self.peak = 0

    @property
    def calls(self) -> list[AgentCall]:
        return self.inner.calls

    async def run(self, call: AgentCall):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return await self.inner.run(call)
        finally:
            self.active -= 1
