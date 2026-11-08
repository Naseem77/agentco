"""QA: behaviour checks in a one-commit snapshot, blind to the engineer's work."""

import asyncio
import shutil
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply, issue_text
from agentco.config import TenantConfig
from agentco.learning.followup import collect_test_sources
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.runner import CommandRunner
from agentco.sandbox.worktree import Worktree, make_snapshot

FAST_LANE_BUG_CLASS = "fast lane: deterministic checks only"


class NeighbourRow(BaseModel):
    case: str
    result: Literal["pass", "fail", "n/a"]
    evidence: str


class QAFailure(BaseModel):
    title: str
    steps: str
    expected: str
    actual: str


class QAReply(Reply):
    passed: bool
    bug_class: str
    tests_run: list[str]
    neighbour_matrix: list[NeighbourRow]
    failures: list[QAFailure] = Field(default_factory=list)
    regression_of_escaped_bug: bool = False
    new_tests: list[str] = Field(default_factory=list)
    # Filled by run_qa from the snapshot; never taken from the model.
    new_test_sources: SkipJsonSchema[dict[str, str]] = Field(default_factory=dict)


def qa_input(
    issue: PMReply,
    neighbour_cases: list[str],
    *,
    commands: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> str:
    sections = [issue_text(issue)]
    if neighbour_cases:
        cases = "\n".join(f"- {case}" for case in neighbour_cases)
        sections.append(f"Known neighbour cases from memory:\n{cases}")
    if commands:
        listed = "\n".join(f"- `{command}`" for command in commands)
        sections.append(f"Commands you may run, exactly as written:\n{listed}")
    if prefixes:
        listed = "\n".join(f"- `{prefix}<paths or pytest args>`" for prefix in prefixes)
        sections.append(
            "Test commands that take arguments "
            f"(append test paths or pytest args):\n{listed}"
        )
    if commands or prefixes:
        sections.append(
            "Run one command per shell call. No shell operators "
            "(`&&`, `|`, `;`, redirects)."
        )
    return "\n\n".join(sections)


def qa_commands(contract: RepoContract) -> tuple[str, ...]:
    commands = (contract.build, contract.test, contract.e2e)
    return tuple(command for command in commands if command)


def qa_command_prefixes(contract: RepoContract) -> tuple[str, ...]:
    prefixes = []
    if contract.test_files:
        prefixes.append(contract.test_files.split("{files}")[0])
    prefixes.append(contract.test + " ")
    return tuple(dict.fromkeys(prefixes))


def _ran_no_tests(reply: QAReply) -> str | None:
    if not reply.tests_run:
        return "tests_run is empty"
    rows = reply.neighbour_matrix
    if rows and all(row.result == "n/a" for row in rows):
        return "every neighbour case is n/a"
    return None


async def run_qa(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    wt: Worktree,
    base: str,
    contract: RepoContract,
    runner: CommandRunner,
    round_: int,
    issue: PMReply,
    *,
    fast_lane: bool,
) -> QAReply:
    # The orchestrator reaches review only after pre-flight passed on this HEAD,
    # so build and affected tests are not run again here.
    if fast_lane:
        return QAReply(
            passed=True,
            bug_class=FAST_LANE_BUG_CLASS,
            tests_run=["pre-flight: build and affected tests"],
            neighbour_matrix=[],
        )
    snapshot_dir = wt.path.parents[1] / "qa"  # never inside the worktree pool folder
    snapshot_path = snapshot_dir / f"{wt.path.name}-qa-r{round_}"
    shutil.rmtree(snapshot_path, ignore_errors=True)  # stale crashed run
    try:
        await asyncio.to_thread(make_snapshot, wt, snapshot_path)
        memory = gate.for_role(Role.QA)
        facts = await memory.search(issue.summary, [Scope.QA])
        commands = qa_commands(contract)
        prefixes = qa_command_prefixes(contract)
        call = AgentCall(
            role=Role.QA,
            model=role_model(cfg, Role.QA).model,
            system=load_prompt(Role.QA),
            prompt=qa_input(
                issue,
                [fact.text for fact in facts],
                commands=commands,
                prefixes=prefixes,
            ),
            workdir=snapshot_path,
            edits=True,
            commands=commands,
            command_prefixes=prefixes,
            tools=memory_tools(memory, Scope.QA),
            max_turns=cfg.loop.agent_max_turns,
            timeout_s=cfg.loop.agent_timeout_s,
        )
        reply = await asker.ask(call, QAReply)
        no_tests = _ran_no_tests(reply)
        if no_tests:
            failure = QAFailure(
                title="QA ran no tests",
                steps="run the listed test commands in the snapshot",
                expected="tests run and neighbour cases checked",
                actual=no_tests,
            )
            reply = reply.model_copy(
                update={"passed": False, "failures": [*reply.failures, failure]}
            )
        sources = await asyncio.to_thread(
            collect_test_sources, snapshot_path, reply.new_tests
        )
        reply = reply.model_copy(update={"new_test_sources": sources})
        return reply
    finally:
        shutil.rmtree(snapshot_path, ignore_errors=True)
