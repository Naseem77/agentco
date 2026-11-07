"""Team lead: classifies findings, then code applies the decision order (spec §6)."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from pydantic import BaseModel, Field

from agentco.agents.architect import ADRReply, adr_markdown
from agentco.agents.checks import CheckOutcome
from agentco.agents.engineer import EngineerReply
from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.qa import QAReply
from agentco.agents.replies import Finding, PMReply, issue_text
from agentco.agents.review import ReviewReply, finding_line
from agentco.config import TenantConfig
from agentco.llm.replies import AgentStuck, Asker, Reply, ReplyInvalid
from agentco.llm.runtime import AgentCall
from agentco.llm.tools import memory_tools
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.sandbox.affected import is_test_path
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.preflight import PreflightReport

CRITICAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "security": (r"auth|permission|crypto|secret|token|sanitiz",),
    "data": (r"migrations/|schema|\.sql$",),
    "infra": (r"^\.github/workflows/|Dockerfile|compose|\.tf$|deploy",),
    "money": (r"payment|billing|license",),
}
FAST_LANE_SUMMARY = "fast lane: rule-based approval"
ROOT_CAUSE_QUESTION = (
    "Root cause required: does the invariant remove the cause, not just this symptom?"
)
MISSING_INVARIANT = Finding(
    id="root-cause",
    severity="major",
    kind="bug",
    title="State one invariant",
    detail=(
        "This area came back again: state the one rule that removes the cause, "
        "apply it, and test it."
    ),
)

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_DIFF_FILE_LINE = re.compile(
    r"^(?:rename (?:from|to) (.+)|(?:\+\+\+ b|--- a)/(.+?)\t?)$", re.MULTILINE
)


def diff_paths(diff_text: str) -> list[str]:
    """Every old and new path a diff touches, renames included."""
    paths = {p for header in _DIFF_HEADER.findall(diff_text) for p in header}
    for groups in _DIFF_FILE_LINE.findall(diff_text):
        paths.update(p for p in groups if p)
    return sorted(paths)


def critical_hits(paths: list[str], qa: QAReply | None) -> list[str]:
    hits = [
        f"{category}: {path}"
        for category, patterns in CRITICAL_PATTERNS.items()
        for path in paths
        if any(re.search(pattern, path, re.IGNORECASE) for pattern in patterns)
    ]
    if qa is not None and qa.regression_of_escaped_bug:
        hits.append("escaped-bug regression")
    return hits


async def new_hits(
    gate: MemoryGate, paths: list[str], new_dependencies: list[str]
) -> list[str]:
    hits = [f"new dependency: {dependency}" for dependency in new_dependencies]
    memory = gate.for_role(Role.LEAD)
    for path in paths:
        if is_test_path(path):
            continue
        if not await memory.search(path, [Scope.RND]):
            hits.append(f"unknown area: {path}")
    return hits


class Classified(BaseModel):
    finding_id: str
    category: Literal["real_bug", "edge_case", "tradeoff", "covered_style"]
    note: str


class LeadReply(Reply):
    confidence: float
    classifications: list[Classified]
    summary: str
    comments: list[str] = Field(default_factory=list)
    question: str = ""
    options: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class LeadDecision:
    outcome: Literal["approve", "send_back", "escalate"]
    category: str
    findings_to_fix: tuple[Finding, ...]
    follow_ups: tuple[Finding, ...]
    reason: str
    summary: str
    comments: tuple[str, ...]
    question: str
    options: tuple[str, ...]


def _qa_findings(qa: QAReply) -> list[Finding]:
    if not qa.failures:
        return [
            Finding(
                id="qa-1",
                severity="major",
                kind="bug",
                title="QA failed without details",
                detail="QA reported a failure but listed no failing cases.",
            )
        ]
    return [
        Finding(
            id=f"qa-{number}",
            severity="major",
            kind="bug",
            title=failure.title,
            detail=(
                f"Steps: {failure.steps}\nExpected: {failure.expected}\n"
                f"Actual: {failure.actual}"
            ),
        )
        for number, failure in enumerate(qa.failures, 1)
    ]


def decide(
    reply: LeadReply | None,
    review: ReviewReply | None,
    qa: QAReply | None,
    errors: tuple[str, ...],
    critical: list[str],
    new: list[str],
    min_confidence: float,
) -> LeadDecision:
    categories = (
        {c.finding_id: c.category for c in reply.classifications} if reply else {}
    )
    findings = review.findings if review else []
    # A finding the lead did not classify counts as a real bug.
    by_category = {
        category: [f for f in findings if categories.get(f.id, "real_bug") == category]
        for category in ("real_bug", "edge_case", "tradeoff", "covered_style")
    }
    follow_ups = tuple(by_category["edge_case"])
    summary = reply.summary if reply else ""

    def outcome(
        kind: Literal["approve", "send_back", "escalate"],
        category: str,
        reason: str,
        **fields,
    ) -> LeadDecision:
        return LeadDecision(
            outcome=kind,
            category=category,
            findings_to_fix=fields.get("findings_to_fix", ()),
            follow_ups=follow_ups,
            reason=reason,
            summary=summary,
            comments=tuple(reply.comments) if reply and kind == "send_back" else (),
            question=fields.get("question", ""),
            options=fields.get("options", ()),
        )

    if critical:
        return outcome(
            "escalate", "critical", "critical change: " + ", ".join(critical)
        )
    if new:
        return outcome("escalate", "new", "new or unknown: " + ", ".join(new))

    qa_failed = qa is not None and not qa.passed
    reasons = []
    if qa_failed:
        reasons.append("QA failed")
    if by_category["real_bug"]:
        reasons.append("real bugs: " + ", ".join(f.id for f in by_category["real_bug"]))
    reasons.extend(f"check error: {error}" for error in errors)
    if reasons:
        to_fix = [*by_category["real_bug"], *by_category["covered_style"]]
        if qa is not None and qa_failed:
            to_fix.extend(_qa_findings(qa))
        return outcome(
            "send_back", "", "; ".join(reasons), findings_to_fix=tuple(to_fix)
        )

    if by_category["tradeoff"]:
        titles = "; ".join(f.title for f in by_category["tradeoff"])
        return outcome(
            "escalate",
            "tradeoff",
            "tradeoff needs a human decision",
            question=(reply.question if reply else "") or titles,
            options=tuple(reply.options) if reply else (),
        )

    confidence = reply.confidence if reply else 0.0
    if confidence < min_confidence:
        return outcome(
            "escalate",
            "not_confident",
            f"lead confidence {confidence:g} is below {min_confidence:g}",
        )
    return outcome("approve", "", "all checks passed and the lead is confident")


def _preflight_lines(preflight: PreflightReport) -> str:
    lines = [
        f"- {check.name}: {'ok' if check.ok else 'FAILED'} ({check.detail})"
        for check in preflight.checks
    ]
    if preflight.conflict:
        lines.append("- rebase conflict")
    return "\n".join(lines)


def _qa_section(qa: QAReply | None) -> str:
    if qa is None:
        return "QA: unavailable"
    lines = [
        f"QA: {'passed' if qa.passed else 'failed'} (bug class: {qa.bug_class})",
        "Tests run: " + (", ".join(qa.tests_run) or "(none)"),
    ]
    lines.extend(
        f"- {row.case}: {row.result} ({row.evidence})" for row in qa.neighbour_matrix
    )
    if qa.failures:
        lines.append("Failures:")
        lines.extend(f"- {f.title}: {f.steps} -> {f.actual}" for f in qa.failures)
    if qa.new_tests:
        lines.append("New tests: " + ", ".join(qa.new_tests))
    if qa.regression_of_escaped_bug:
        lines.append("This is a regression of a bug that escaped before.")
    return "\n".join(lines)


def _review_section(review: ReviewReply | None) -> str:
    if review is None:
        return "Blind review: unavailable"
    lines = [f"Blind review: {review.summary}"]
    if review.findings:
        lines.append("Findings (classify each by its id):")
        lines.extend(finding_line(finding) for finding in review.findings)
    return "\n".join(lines)


def _engineer_section(engineer: EngineerReply) -> str:
    lines = [
        f"Engineer notes: {engineer.summary}",
        "Files touched: " + (", ".join(engineer.files_touched) or "(none)"),
        "Tests added: " + (", ".join(engineer.tests_added) or "(none)"),
    ]
    if engineer.no_test_reason:
        lines.append(f"No-test reason: {engineer.no_test_reason}")
    if engineer.invariant:
        lines.append(f"Invariant: {engineer.invariant}")
    if engineer.new_dependencies:
        lines.append("New dependencies: " + ", ".join(engineer.new_dependencies))
    lines.extend(f"Follow-up: {f.title} ({f.why})" for f in engineer.follow_ups)
    return "\n".join(lines)


def lead_input(
    issue: PMReply,
    diff_text: str,
    engineer: EngineerReply,
    preflight: PreflightReport,
    checks: CheckOutcome,
    changed_lines: int,
    critical: list[str],
    new: list[str],
    require_invariant: bool = False,
) -> str:
    sections = [
        issue_text(issue),
        _engineer_section(engineer),
        f"Pre-flight:\n{_preflight_lines(preflight)}",
        _qa_section(checks.qa),
        _review_section(checks.review),
    ]
    if checks.errors:
        sections.append(
            "Check errors:\n" + "\n".join(f"- {error}" for error in checks.errors)
        )
    if critical:
        sections.append("Critical changes:\n" + "\n".join(f"- {h}" for h in critical))
    if new:
        sections.append("New or unknown:\n" + "\n".join(f"- {h}" for h in new))
    if require_invariant:
        sections.append(ROOT_CAUSE_QUESTION)
    sections.append(f"Changed lines: {changed_lines}\n\nDiff:\n{diff_text}")
    return "\n\n".join(sections)


def _fast_lane_eligible(
    cfg: TenantConfig,
    checks: CheckOutcome,
    changed_lines: int,
    critical: list[str],
    new: list[str],
) -> bool:
    return (
        not critical
        and not new
        and not checks.errors
        and checks.qa is not None
        and checks.qa.passed
        and checks.review is not None
        and all(f.severity == "nit" for f in checks.review.findings)
        and changed_lines <= cfg.loop.fast_lane_max_changed_lines
    )


_CHORES = (
    re.compile(r"^(run|re-?run|execute)\b.*\b(tests?|lint|linter|build|ci|suite)\b"),
    re.compile(r"^(add|write) (more )?tests?$"),
)


def _normalized_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", title.casefold()).split())


def _is_chore(title: str) -> bool:
    normalized = _normalized_title(title)
    return any(chore.search(normalized) for chore in _CHORES)


def _file_follow_ups(store: TaskStore, task: TaskRecord, titles: Iterable[str]) -> None:
    """File each new, non-chore title once per task; titles match loosely."""
    source_ref = f"task:{task.id}"
    filed = {
        _normalized_title(t.title)
        for t in store.list_tasks(task.tenant)
        if t.source == "followup" and t.source_ref == source_ref
    }
    for title in titles:
        title = title.strip()
        key = _normalized_title(title)
        if not key or key in filed or _is_chore(title):
            continue
        store.create_task(task.tenant, title, "followup", source_ref)
        filed.add(key)


async def run_lead(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    *,
    issue: PMReply,
    diff_text: str,
    engineer: EngineerReply,
    preflight: PreflightReport,
    checks: CheckOutcome,
    changed_lines: int,
    fast_lane: bool,
    require_invariant: bool = False,
) -> LeadDecision:
    paths = sorted({*diff_paths(diff_text), *engineer.files_touched})
    critical = critical_hits(paths, checks.qa)
    new = await new_hits(gate, paths, engineer.new_dependencies)

    if require_invariant and not engineer.invariant.strip():
        decision = LeadDecision(
            outcome="send_back",
            category="",
            findings_to_fix=(MISSING_INVARIANT,),
            follow_ups=(),
            reason="root cause: the engineer stated no invariant",
            summary="",
            comments=(),
            question="",
            options=(),
        )
    elif (
        fast_lane
        and not require_invariant
        and _fast_lane_eligible(cfg, checks, changed_lines, critical, new)
    ):
        decision = LeadDecision(
            outcome="approve",
            category="",
            findings_to_fix=(),
            follow_ups=(),
            reason="fast lane: no critical or new changes, checks passed",
            summary=FAST_LANE_SUMMARY,
            comments=(),
            question="",
            options=(),
        )
    else:
        call = AgentCall(
            role=Role.LEAD,
            model=role_model(cfg, Role.LEAD).model,
            system=load_prompt(Role.LEAD),
            prompt=lead_input(
                issue,
                diff_text,
                engineer,
                preflight,
                checks,
                changed_lines,
                critical,
                new,
                require_invariant,
            ),
            tools=memory_tools(gate.for_role(Role.LEAD)),
            max_turns=cfg.loop.agent_max_turns,
            timeout_s=cfg.loop.agent_timeout_s,
        )
        try:
            reply = await asker.ask(call, LeadReply)
        except (ReplyInvalid, AgentStuck):
            if not (critical or new):
                raise
            reply = None
        decision = decide(
            reply,
            checks.review,
            checks.qa,
            checks.errors,
            critical,
            new,
            cfg.thresholds.lead_min_confidence,
        )
        if reply is None:
            decision = replace(decision, reason="lead unavailable")

    if decision.outcome == "approve":
        _file_follow_ups(
            store,
            task,
            [
                *(f.title for f in decision.follow_ups),
                *(f.title for f in engineer.follow_ups),
            ],
        )

    body = asdict(decision)
    body["findings_to_fix"] = [f.model_dump() for f in decision.findings_to_fix]
    body["follow_ups"] = [f.model_dump() for f in decision.follow_ups]
    store.put_artifact(task.id, ArtifactKind.LEAD, Role.LEAD, body)
    return decision


@dataclass(frozen=True)
class ChildSummary:
    id: int
    title: str
    state: str
    pr_url: str
    summary: str


class ParentLeadReply(Reply):
    approve: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    summary: str


def parent_input(
    issue: PMReply | None,
    adr: ADRReply,
    children: Sequence[ChildSummary],
    human_note: str,
) -> str:
    sections = [
        adr_markdown(adr),
        issue_text(issue) if issue else "(the parent has no issue text)",
    ]
    lines = [
        f"- #{c.id} {c.title} [{c.state}]"
        + (f" {c.pr_url}" if c.pr_url else "")
        + (f"\n  lead summary: {c.summary}" if c.summary else "")
        for c in children
    ]
    sections.append("Sub-tasks:\n" + ("\n".join(lines) or "(none)"))
    if human_note.strip():
        sections.append(f"Human guidance:\n{human_note.strip()}")
    return "\n\n".join(sections)


async def review_parent(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    parent: TaskRecord,
    *,
    adr: ADRReply,
    children: Sequence[ChildSummary],
    human_note: str = "",
) -> ParentLeadReply:
    """Do the merged sub-tasks together deliver the ADR and the parent's criteria?"""
    issue_artifact = store.latest_artifact(parent.id, ArtifactKind.ISSUE)
    issue = PMReply.model_validate(issue_artifact.body) if issue_artifact else None
    call = AgentCall(
        role=Role.LEAD,
        model=role_model(cfg, Role.LEAD).model,
        system=load_prompt(Role.LEAD, "parent"),
        prompt=parent_input(issue, adr, children, human_note),
        tools=memory_tools(gate.for_role(Role.LEAD)),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    reply = await asker.ask(call, ParentLeadReply)
    approved = reply.approve and reply.confidence >= cfg.thresholds.lead_min_confidence
    decision = LeadDecision(
        outcome="approve" if approved else "escalate",
        category="" if approved else "tradeoff",
        findings_to_fix=(),
        follow_ups=(),
        reason=reply.reason,
        summary=reply.summary,
        comments=(),
        question="",
        options=(),
    )
    body = asdict(decision) | {"source": "parent"}
    store.put_artifact(parent.id, ArtifactKind.LEAD, Role.LEAD, body)
    return reply
