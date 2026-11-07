"""Classify a PR feedback round: the lead judges, code applies the stop rule (spec §6)."""

from dataclasses import dataclass, replace
from typing import Literal

from pydantic import Field

from agentco.agents.lead import Classified, LeadReply
from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import Finding
from agentco.config import TenantConfig
from agentco.intake.fence import fence_untrusted, one_line
from agentco.intake.findings import PRFinding
from agentco.llm.replies import Asker
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore

READY_REASON = "remaining PR feedback is not a real problem"
READY_OPTIONS = (
    "merge it yourself on GitHub, then approve",
    "answer: what to fix",
    "reject",
)
TRADEOFF_OPTIONS = ("fix", "keep as is")
MAX_PROMPT_FINDINGS = 50
_CATEGORIES = ("real_bug", "edge_case", "tradeoff", "covered_style")


class PRLeadReply(LeadReply):
    conflicts: list[str] = Field(default_factory=list)
    new_pattern: str = ""


@dataclass(frozen=True)
class Preclassified:
    decided: tuple[Classified, ...]
    known: tuple[str, ...]
    pending: tuple[PRFinding, ...]
    recurrences: tuple[str, ...]


@dataclass(frozen=True)
class PRRoundDecision:
    outcome: Literal["reenter", "escalate", "none"]
    category: str
    findings_to_fix: tuple[Finding, ...]
    follow_ups: tuple[Finding, ...]
    known: tuple[str, ...]
    recurrences: tuple[str, ...]
    classifications: tuple[Classified, ...]
    reason: str
    summary: str
    question: str
    options: tuple[str, ...]
    carried: tuple[str, ...] = ()
    """Ids of findings NOT handled this round: tradeoffs that were not put to a human
    because a real bug, a conflict or a new pattern took the round, and pending findings
    beyond MAX_PROMPT_FINDINGS. They are in no `classifications` entry, so the consumer
    must store them in no PR_REVIEW artifact and set no marker for them. They then come
    back as pending findings next round, once the task is back in PR_OPEN."""


def prior_categories(store: TaskStore, task_id: int) -> dict[str, str]:
    """Fingerprint -> the latest category this task's earlier PR rounds gave it."""
    seen: dict[str, str] = {}
    for artifact in store.artifacts(task_id, ArtifactKind.PR_REVIEW):
        for item in artifact.body.get("classifications", []):
            if (
                isinstance(item, dict)
                and item.get("fingerprint")
                and item.get("category")
            ):
                seen[item["fingerprint"]] = item["category"]
    return seen


def preclassify(findings: list[PRFinding], prior: dict[str, str]) -> Preclassified:
    decided: list[Classified] = []
    known: list[str] = []
    pending: list[PRFinding] = []
    recurrences: dict[str, None] = {}
    for f in findings:
        before = prior.get(f.fingerprint)
        if before == "real_bug":
            recurrences[f.fingerprint] = None
        if f.source == "check":
            decided.append(
                Classified(finding_id=f.id, category="real_bug", note="CI check failed")
            )
        elif before == "real_bug":
            decided.append(
                Classified(
                    finding_id=f.id,
                    category="real_bug",
                    note="reported before as a real bug; the fix did not hold",
                )
            )
        elif before is not None:
            known.append(f.id)
        else:
            pending.append(f)
    return Preclassified(
        tuple(decided), tuple(known), tuple(pending), tuple(recurrences)
    )


def decide_pr_round(
    findings: list[PRFinding], pre: Preclassified, reply: PRLeadReply | None
) -> PRRoundDecision:
    known = set(pre.known)
    categories = {c.finding_id: c.category for c in pre.decided}
    pending = {f.id for f in pre.pending}
    if reply is not None:
        categories.update(
            (c.finding_id, c.category)
            for c in reply.classifications
            if c.finding_id in pending
        )
    notes = {c.finding_id: c.note for c in pre.decided}
    if reply is not None:
        notes.update(
            (c.finding_id, c.note)
            for c in reply.classifications
            if c.finding_id in pending
        )

    all_open = [f for f in findings if f.id not in known]
    # A tradeoff is a human's call; if another outcome takes this round, ask it next round.
    # When every open finding is a tradeoff and a conflict escalates, that escalation is the
    # round in which the human sees them, so they are consumed with it, not carried.
    escalating = reply is not None and bool(reply.conflicts or reply.new_pattern)
    non_tradeoff = any(categories.get(f.id, "real_bug") != "tradeoff" for f in all_open)
    deferred = non_tradeoff and (
        escalating
        or any(categories.get(f.id, "real_bug") == "real_bug" for f in all_open)
    )
    carried_ids = tuple(
        f.id for f in all_open if deferred and categories.get(f.id) == "tradeoff"
    )
    open_findings = [f for f in all_open if f.id not in carried_ids]
    # A pending finding the lead did not classify counts as a real bug.
    effective = {f.id: categories.get(f.id, "real_bug") for f in open_findings}
    classifications = tuple(
        Classified(
            finding_id=f.id,
            category=effective[f.id],
            note=notes.get(f.id, "not classified by the lead; counted as a real bug"),
        )
        for f in open_findings
    )
    by_category = {
        category: [f for f in open_findings if effective[f.id] == category]
        for category in _CATEGORIES
    }
    follow_ups = tuple(f.as_finding() for f in by_category["edge_case"])
    summary = reply.summary if reply else ""

    def outcome(
        kind: Literal["reenter", "escalate", "none"],
        category: str,
        reason: str,
        *,
        findings_to_fix: tuple[Finding, ...] = (),
        question: str = "",
        options: tuple[str, ...] = (),
    ) -> PRRoundDecision:
        return PRRoundDecision(
            outcome=kind,
            category=category,
            findings_to_fix=findings_to_fix,
            follow_ups=follow_ups,
            known=pre.known,
            recurrences=pre.recurrences,
            classifications=classifications,
            reason=reason,
            summary=summary,
            question=question,
            options=options,
            carried=carried_ids,
        )

    # Checked before "nothing open": a conflict among carried tradeoffs still needs a human.
    if (
        (open_findings or carried_ids)
        and reply is not None
        and (reply.conflicts or reply.new_pattern)
    ):
        if reply.conflicts:
            reason = "reviewers conflict: " + "; ".join(reply.conflicts)
        else:
            reason = f"feedback asks for something new: {reply.new_pattern}"
        return outcome(
            "escalate",
            "new",
            reason,
            question=reply.question or reason,
            options=tuple(reply.options),
        )

    if not open_findings:
        return outcome("none", "", "no new PR feedback to act on")

    if by_category["real_bug"]:
        to_fix = (*by_category["real_bug"], *by_category["covered_style"])
        return outcome(
            "reenter",
            "",
            "real bugs: " + ", ".join(f.id for f in by_category["real_bug"]),
            findings_to_fix=tuple(f.as_finding() for f in to_fix),
        )

    if by_category["tradeoff"]:
        titles = "; ".join(f.as_finding().title for f in by_category["tradeoff"])
        return outcome(
            "escalate",
            "tradeoff",
            "tradeoff needs a human decision",
            question=(reply.question if reply else "") or titles,
            options=(tuple(reply.options) if reply else ()) or TRADEOFF_OPTIONS,
        )

    return outcome(
        "escalate",
        "ready_to_merge",
        READY_REASON,
        question=(reply.question if reply else "")
        or "The remaining PR feedback is not a real problem. Merge it, name what to fix, or reject?",
        options=READY_OPTIONS,
    )


def _where(f: PRFinding) -> str:
    if not f.path:
        return ""
    path = one_line(f.path)
    return f" at {path}:{f.line}" if f.line is not None else f" at {path}"


def _finding_block(f: PRFinding) -> str:
    head = (
        f"[{f.id}] {f.source} by {one_line(f.author)} "
        f"({one_line(f.association)}){_where(f)}"
    )
    return f"{head}\n{fence_untrusted(f.body)}"


def _context(store: TaskStore, task: TaskRecord) -> list[str]:
    sections = []
    issue = store.latest_artifact(task.id, ArtifactKind.ISSUE)
    if issue is not None:
        criteria = "\n".join(
            f"- {c}" for c in issue.body.get("acceptance_criteria", [])
        )
        sections.append(
            f"Issue: {issue.body.get('summary', '')}\nAcceptance criteria:\n{criteria}"
        )
    lead = store.latest_artifact(task.id, ArtifactKind.LEAD)
    if lead is not None and lead.body.get("summary"):
        sections.append(f"Lead summary of the last round: {lead.body['summary']}")
    engineer = store.latest_artifact(task.id, ArtifactKind.ENGINEER)
    if engineer is not None:
        files = ", ".join(engineer.body.get("files_touched", [])) or "(none)"
        sections.append(
            f"Engineer notes: {engineer.body.get('summary', '')}\n"
            f"Files touched: {files}"
        )
    return sections


async def classify_round(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    findings: list[PRFinding],
) -> PRRoundDecision:
    pre = preclassify(findings, prior_categories(store, task.id))
    reply: PRLeadReply | None = None
    overflow = pre.pending[MAX_PROMPT_FINDINGS:]
    if overflow:
        shown = {f.id for f in overflow}
        findings = [f for f in findings if f.id not in shown]
        pre = replace(pre, pending=pre.pending[:MAX_PROMPT_FINDINGS])
    if pre.pending:
        sections = [
            *_context(store, task),
            "New PR feedback to classify (by its id):\n\n"
            + "\n\n".join(_finding_block(f) for f in pre.pending),
        ]
        call = AgentCall(
            role=Role.LEAD,
            model=role_model(cfg, Role.LEAD).model,
            system=load_prompt(Role.LEAD, "pr"),
            prompt="\n\n".join(sections),
            tools=memory_tools(gate.for_role(Role.LEAD)),
            max_turns=cfg.loop.agent_max_turns,
            timeout_s=cfg.loop.agent_timeout_s,
        )
        reply = await asker.ask(call, PRLeadReply)
    decision = decide_pr_round(findings, pre, reply)
    if not overflow:
        return decision
    carried = (*decision.carried, *(f.id for f in overflow))
    return replace(decision, carried=carried)
