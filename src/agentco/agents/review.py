"""Blind review: the reviewer sees the issue and a diff, never the engineer's account."""

import asyncio

from agentco.agents.engineer import contract_commands, test_file_prefixes
from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import Finding, PMReply, issue_text
from agentco.config import TenantConfig
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.worktree import Worktree, diff


class ReviewReply(Reply):
    findings: list[Finding]
    summary: str


def finding_line(finding: Finding) -> str:
    where = ""
    if finding.path:
        where = f" {finding.path}" + (f":{finding.line}" if finding.line else "")
    return (
        f"- [{finding.severity}/{finding.kind}] {finding.id}{where}: "
        f"{finding.title}\n  {finding.detail}"
    )


def review_input(
    issue: PMReply, diff_text: str, findings_addressed: list[Finding]
) -> str:
    sections = [issue_text(issue)]
    if findings_addressed:
        lines = "\n".join(finding_line(f) for f in findings_addressed)
        sections.append(
            "The diff below is a fix for these earlier findings. Review it as new "
            f"code: confirm they are fixed and look for bugs the fix adds.\n{lines}"
        )
    sections.append(f"Diff:\n{diff_text}")
    return "\n\n".join(sections)


async def run_review(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    wt: Worktree,
    base: str,
    issue: PMReply,
    *,
    fix_since: str | None,
    findings_addressed: list[Finding],
    fast_lane: bool,
) -> ReviewReply:
    diff_text = await asyncio.to_thread(diff, wt, fix_since or base)
    call = AgentCall(
        role=Role.REVIEWER,
        model=role_model(cfg, Role.REVIEWER).model,
        system=load_prompt(Role.REVIEWER, "light" if fast_lane else None),
        prompt=review_input(issue, diff_text, findings_addressed),
        workdir=wt.path,  # read-only: no edits, no commands beyond read-only git
        edits=False,
        tools=memory_tools(gate.for_role(Role.REVIEWER)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    return await asker.ask(call, ReviewReply)


def fast_review_input(
    issue: PMReply,
    diff_text: str,
    *,
    impact: str,
    lessons: list[str],
    prior: list[Finding],
) -> str:
    sections = [issue_text(issue)]
    if lessons:
        listed = "\n".join(f"- {lesson}" for lesson in lessons)
        sections.append(f"Review lessons from past PR comments:\n{listed}")
    if prior:
        lines = "\n".join(finding_line(f) for f in prior)
        sections.append(
            "Earlier findings the engineer was asked to fix. Verify each is fixed; "
            f"report any that is not again with the same id:\n{lines}"
        )
    if impact:
        sections.append(impact)
    sections.append(f"Whole diff against the base branch:\n{diff_text}")
    return "\n\n".join(sections)


async def run_fast_review(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    wt: Worktree,
    base: str,
    issue: PMReply,
    *,
    impact: str,
    lessons: list[str],
    prior: list[Finding],
    contract: RepoContract | None = None,
) -> ReviewReply:
    """Fast mode: one review of the whole diff that reports every finding at once.

    With a repo contract the reviewer may run the tests, lint and format check.
    """
    diff_text = await asyncio.to_thread(diff, wt, base)
    call = AgentCall(
        role=Role.REVIEWER,
        model=role_model(cfg, Role.REVIEWER).model,
        system=load_prompt(Role.REVIEWER, "fast"),
        prompt=fast_review_input(
            issue, diff_text, impact=impact, lessons=lessons, prior=prior
        ),
        workdir=wt.path,
        edits=False,
        commands=contract_commands(contract) if contract else (),
        command_prefixes=test_file_prefixes(contract) if contract else (),
        tools=memory_tools(gate.for_role(Role.REVIEWER)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    return await asker.ask(call, ReviewReply)
