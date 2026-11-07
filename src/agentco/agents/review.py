"""Blind review: the reviewer sees the issue and a diff, never the engineer's account."""

import asyncio

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import Finding, PMReply, issue_text
from agentco.config import TenantConfig
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
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
        tools=memory_tools(gate.for_role(Role.REVIEWER)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    return await asker.ask(call, ReviewReply)
