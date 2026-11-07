"""Team lead: classifies findings, then code applies the decision order (spec §6)."""

import re
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import BaseModel, Field

from agentco.agents.checks import CheckOutcome
from agentco.agents.engineer import EngineerReply
from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.qa import QAReply
from agentco.agents.replies import Finding, PMReply, issue_text
from agentco.agents.review import ReviewReply, _finding_line
from agentco.config import TenantConfig
from agentco.llm.replies import Asker, Reply
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

_DIFF_PATH = re.compile(r"^diff --git a/(.+) b/\1$", re.MULTILINE)


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
        lines.extend(_finding_line(finding) for finding in review.findings)
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


def _file_follow_ups(
    store: TaskStore, task: TaskRecord, follow_ups: tuple[Finding, ...]
) -> None:
    source_ref = f"task:{task.id}"
    filed = {
        t.title
        for t in store.list_tasks(task.tenant)
        if t.source == "followup" and t.source_ref == source_ref
    }
    for finding in follow_ups:
        if finding.title not in filed:
            store.create_task(task.tenant, finding.title, "followup", source_ref)
            filed.add(finding.title)


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
) -> LeadDecision:
    paths = sorted({*_DIFF_PATH.findall(diff_text), *engineer.files_touched})
    critical = critical_hits(paths, checks.qa)
    new = await new_hits(gate, paths, engineer.new_dependencies)

    if fast_lane and _fast_lane_eligible(cfg, checks, changed_lines, critical, new):
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
            ),
            tools=memory_tools(gate.for_role(Role.LEAD)),
            max_turns=cfg.loop.agent_max_turns,
            timeout_s=cfg.loop.agent_timeout_s,
        )
        reply = await asker.ask(call, LeadReply)
        decision = decide(
            reply,
            checks.review,
            checks.qa,
            checks.errors,
            critical,
            new,
            cfg.thresholds.lead_min_confidence,
        )
        _file_follow_ups(store, task, decision.follow_ups)

    body = asdict(decision)
    body["findings_to_fix"] = [f.model_dump() for f in decision.findings_to_fix]
    body["follow_ups"] = [f.model_dump() for f in decision.follow_ups]
    store.put_artifact(task.id, ArtifactKind.LEAD, Role.LEAD, body)
    return decision
