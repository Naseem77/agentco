import asyncio
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import Finding, PMReply
from agentco.config import TenantConfig
from agentco.intake.fence import fence_untrusted, one_line
from agentco.llm.replies import AgentStuck, Asker, Reply, ReplyInvalid
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.worktree import (
    Worktree,
    changed_files,
    commit_all,
    head_sha,
    reset_worktree,
)
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore


@dataclass(frozen=True)
class EngineerBrief:
    issue: PMReply
    findings_to_fix: tuple[Finding, ...] = ()
    preflight_failures: tuple[str, ...] = ()
    require_invariant: bool = False
    round: int = 0
    human_note: str = ""
    untrusted_finding_ids: frozenset[str] = frozenset()
    """Findings quoted from PR comments or checks: rendered as fenced data."""
    tests_precommitted: bool = False
    playbooks: tuple[str, ...] = ()
    impact: str = ""
    """Callers and tests the current diff touches, from the live code graph."""
    pre_pr: bool = False
    """Fast mode: a whole-diff reviewer acting like CodeRabbit / Copilot comes next."""


class FollowUp(BaseModel):
    title: str
    why: str


class EngineerReply(Reply):
    summary: str
    files_touched: list[str]
    tests_added: list[str]
    no_test_reason: str = ""
    invariant: str = ""
    new_dependencies: list[str] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)


class EngineerFailed(RuntimeError):
    def __init__(self, reason: str, stuck: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stuck = stuck


def _finding_lines(finding: Finding) -> str:
    where = ""
    if finding.path:
        where = f" {finding.path}" + (f":{finding.line}" if finding.line else "")
    return (
        f"- [{finding.severity}/{finding.kind}] {finding.id}{where}: "
        f"{finding.title}\n  {finding.detail}"
    )


def _untrusted_lines(finding: Finding) -> str:
    where = ""
    if finding.path:
        where = f" {one_line(finding.path)}" + (
            f":{finding.line}" if finding.line else ""
        )
    head = f"- [{finding.severity}/{finding.kind}] {finding.id}{where}:"
    text = finding.detail
    if not text.startswith(finding.title):
        text = f"{finding.title}\n{text}"
    return f"{head}\n{fence_untrusted(text)}"


MAX_REVIEW_RULES = 15
_PRE_PR = (
    "## Get it right the first time\n"
    "Next, a reviewer that acts like CodeRabbit and GitHub Copilot reviews your whole "
    "diff, and the PR opens only when it finds nothing worth a comment. Before you "
    "finish:\n"
    "- list every case, input and failure the issue names, and make sure each one "
    "has code that handles it and a test that fails without your change;\n"
    "- run the targeted tests, the lint and the format check, and fix what they "
    "report;\n"
    "- check what reviewers flag: empty / None / huge input; error paths and cleanup "
    "of partial work (no stray temp files); races and atomic writes (temp files "
    "beside the target, original file permissions kept); closed files and processes; "
    "new exceptions handled by every caller, including the CLI; cross-platform "
    "paths; docs and config examples for changed behaviour; every caller of a "
    "function you change;\n"
    "- make each test assert the behaviour it names: a glob or mock that can never "
    "match proves nothing."
)


def _prompt(
    task: TaskRecord,
    brief: EngineerBrief,
    contract: RepoContract,
    rules: tuple[str, ...] = (),
) -> str:
    issue = brief.issue
    criteria = "\n".join(f"- {criterion}" for criterion in issue.acceptance_criteria)
    sections = [
        f"Task {task.id}: {task.title}",
        f"Summary: {issue.summary}",
        f"Why: {issue.why}",
        f"Acceptance criteria:\n{criteria}",
    ]
    if issue.product_notes:
        sections.append(f"Product notes: {issue.product_notes}")
    if brief.playbooks:
        lines = "\n".join(f"- {playbook}" for playbook in brief.playbooks)
        sections.append(f"Playbooks for this area (start from these):\n{lines}")
    if brief.findings_to_fix:
        findings = "\n".join(
            _untrusted_lines(f)
            if f.id in brief.untrusted_finding_ids
            else _finding_lines(f)
            for f in brief.findings_to_fix
        )
        sections.append(
            "Fix exactly these findings and nothing else; keep the fix small.\n"
            + findings
        )
    if brief.impact:
        sections.append(
            brief.impact + "\nCheck every caller listed still works with your change."
        )
    if brief.preflight_failures:
        failures = "\n".join(f"- {failure}" for failure in brief.preflight_failures)
        sections.append(f"Pre-flight failures to make pass:\n{failures}")
    if brief.require_invariant:
        sections.append(
            "State in `invariant` the one rule that makes this class of bug "
            "impossible, and apply it everywhere in the code you touch."
        )
    if brief.human_note:
        sections.append(f"Note from the human: {brief.human_note}")
    if brief.pre_pr:
        sections.append(_PRE_PR)
    if rules:
        listed = "\n".join(f"- {rule}" for rule in rules)
        sections.append(f"## Review rules the reviewer will apply\n{listed}")
    allowed = [
        *contract_commands(contract),
        *(f"{p}<test files>" for p in test_file_prefixes(contract)),
    ]
    if allowed:
        listed = "\n".join(f"- {command}" for command in allowed)
        sections.append(
            "Commands you may run (exactly as written, no shell operators):\n" + listed
        )
    return "\n\n".join(sections)


def contract_commands(contract: RepoContract) -> tuple[str, ...]:
    commands = (
        contract.build,
        contract.test,
        contract.lint,
        contract.format,
        contract.e2e,
    )
    return tuple(command for command in commands if command)


def test_file_prefixes(contract: RepoContract) -> tuple[str, ...]:
    return (contract.test_files.split("{files}")[0],) if contract.test_files else ()


async def review_rules(
    gate: MemoryGate, role: Role, task: TaskRecord
) -> tuple[str, ...]:
    facts = await gate.for_role(role).search(
        f"ReviewRule {task.title}", scopes=[Scope.RND], limit=MAX_REVIEW_RULES
    )
    rules = dict.fromkeys(" ".join(f.text.split()) for f in facts)
    return tuple(rule for rule in rules if rule)[:MAX_REVIEW_RULES]


async def _invariant_only_retry(
    wt: Worktree, brief: EngineerBrief, reply: EngineerReply
) -> bool:
    # An earlier round already committed the fix; only the invariant was missing.
    if not (brief.require_invariant and reply.invariant.strip()):
        return False
    return bool(await asyncio.to_thread(changed_files, wt, wt.base))


async def _tests_precommitted(wt: Worktree, brief: EngineerBrief) -> bool:
    # The orchestrator already committed tests the engineer may find need no change.
    return brief.tests_precommitted and bool(
        await asyncio.to_thread(changed_files, wt, wt.base)
    )


async def run_engineer(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    role: Role,
    wt: Worktree,
    contract: RepoContract,
    brief: EngineerBrief,
) -> EngineerReply:
    rules = await review_rules(gate, role, task)
    call = AgentCall(
        role=role,
        model=role_model(cfg, role).model,
        system=load_prompt(role),
        prompt=_prompt(task, brief, contract, rules),
        workdir=wt.path,
        edits=True,
        commands=contract_commands(contract),
        command_prefixes=test_file_prefixes(contract),
        tools=memory_tools(gate.for_role(role)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    head = await asyncio.to_thread(head_sha, wt)
    try:
        reply = await asker.ask(call, EngineerReply)
    except Exception as exc:
        await asyncio.to_thread(reset_worktree, wt.path, head)
        if isinstance(exc, AgentStuck):
            raise EngineerFailed(exc.reason, stuck=True) from exc
        if isinstance(exc, ReplyInvalid):
            raise EngineerFailed(str(exc)) from exc
        raise
    sha = await asyncio.to_thread(
        commit_all, wt, f"agentco: task {task.id} round {brief.round}"
    )
    if sha is None and not (
        await _invariant_only_retry(wt, brief, reply)
        or await _tests_precommitted(wt, brief)
    ):
        raise EngineerFailed("engineer made no changes")
    if brief.require_invariant and not reply.invariant.strip():
        raise EngineerFailed("no invariant stated")
    store.put_artifact(task.id, ArtifactKind.ENGINEER, role, reply.model_dump())
    return reply
