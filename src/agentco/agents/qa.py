"""QA: behaviour checks in a one-commit snapshot, blind to the engineer's work."""

import asyncio
import shutil
from pathlib import Path
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
from agentco.sandbox.affected import affected_tests, test_command
from agentco.sandbox.runner import CommandResult, CommandRunner, ContainerError
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


def qa_input(issue: PMReply, neighbour_cases: list[str]) -> str:
    sections = [issue_text(issue)]
    if neighbour_cases:
        cases = "\n".join(f"- {case}" for case in neighbour_cases)
        sections.append(f"Known neighbour cases from memory:\n{cases}")
    return "\n\n".join(sections)


def _failure(title: str, command: str, actual: str) -> QAFailure:
    return QAFailure(
        title=title, steps=command, expected="exit code 0", actual=actual.strip()
    )


def _command_failure(title: str, result: CommandResult, timeout_s: float) -> QAFailure:
    if result.timed_out:
        actual = f"timed out after {timeout_s:g}s\n{result.output}"
    else:
        actual = f"exit code {result.exit_code}\n{result.output}"
    return _failure(title, result.command, actual)


async def _deterministic_checks(
    cfg: TenantConfig,
    wt: Worktree,
    base: str,
    contract: RepoContract,
    runner: CommandRunner,
    snapshot_path: Path,
) -> tuple[list[str], list[QAFailure]]:
    """Build, then the affected tests, in the snapshot. Returns (commands run, failures)."""
    timeout_s = cfg.loop.command_timeout_s
    prepare = getattr(runner, "prepare", None)
    if prepare is not None:
        try:
            await asyncio.to_thread(prepare, snapshot_path)
        except ContainerError as exc:
            return [], [_failure("sandbox setup failed", "prepare", str(exc))]

    ran: list[str] = []
    build = await asyncio.to_thread(
        runner.run, contract.build, snapshot_path, timeout_s
    )
    ran.append(contract.build)
    if build.exit_code != 0:
        return ran, [_command_failure("build failed", build, timeout_s)]

    repo = wt.path.parents[2].name
    affected = await affected_tests(repo, wt, base)
    command = test_command(contract, affected)
    tests = await asyncio.to_thread(runner.run, command, snapshot_path, timeout_s)
    ran.append(command)
    if tests.exit_code != 0:
        return ran, [_command_failure("tests failed", tests, timeout_s)]
    return ran, []


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
    snapshot_dir = wt.path.parents[1] / "qa"  # never inside the worktree pool folder
    snapshot_path = snapshot_dir / f"{wt.path.name}-qa-r{round_}"
    shutil.rmtree(snapshot_path, ignore_errors=True)  # stale crashed run
    try:
        await asyncio.to_thread(make_snapshot, wt, snapshot_path)
        ran, failures = await _deterministic_checks(
            cfg, wt, base, contract, runner, snapshot_path
        )
        if fast_lane:
            return QAReply(
                passed=not failures,
                bug_class=FAST_LANE_BUG_CLASS,
                tests_run=ran,
                neighbour_matrix=[],
                failures=failures,
            )
        memory = gate.for_role(Role.QA)
        facts = await memory.search(issue.summary, [Scope.QA])
        prefixes = (
            (contract.test_files.split("{files}")[0],) if contract.test_files else ()
        )
        commands = (contract.build, contract.test, contract.e2e)
        call = AgentCall(
            role=Role.QA,
            model=role_model(cfg, Role.QA).model,
            system=load_prompt(Role.QA),
            prompt=qa_input(issue, [fact.text for fact in facts]),
            workdir=snapshot_path,
            edits=True,
            commands=tuple(command for command in commands if command),
            command_prefixes=prefixes,
            tools=memory_tools(memory, Scope.QA),
            max_turns=cfg.loop.agent_max_turns,
            timeout_s=cfg.loop.agent_timeout_s,
        )
        reply = await asker.ask(call, QAReply)
        if failures:
            reply = reply.model_copy(
                update={
                    "passed": False,
                    "failures": [*failures, *reply.failures],
                }
            )
        sources = await asyncio.to_thread(
            collect_test_sources, snapshot_path, reply.new_tests
        )
        reply = reply.model_copy(update={"new_test_sources": sources})
        return reply
    finally:
        shutil.rmtree(snapshot_path, ignore_errors=True)
