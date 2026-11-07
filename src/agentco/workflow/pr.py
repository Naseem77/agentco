from agentco.agents.lead import LeadDecision
from agentco.agents.qa import QAReply
from agentco.agents.replies import PMReply
from agentco.store.models import TaskRecord
from agentco.workflow.preflight import PreflightReport

TITLE_LIMIT = 72
HUMAN_MERGES = "Ready to merge — a human merges this PR. agentco never merges."


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def pr_title(task: TaskRecord, issue: PMReply) -> str:
    suffix = f" (agentco #{task.id})"
    summary = _one_line(issue.summary)
    room = TITLE_LIMIT - len(suffix)
    if len(summary) > room:
        summary = summary[: max(room - 1, 0)].rstrip() + "…"
    return summary + suffix


def _lead_section(decision: LeadDecision) -> list[str]:
    lines = ["## Lead review", "", _one_line(decision.summary) or "Approved."]
    if decision.comments:
        lines += ["", *(f"- {_one_line(c)}" for c in decision.comments)]
    return lines


def _qa_section(qa: QAReply | None) -> list[str]:
    if qa is None:
        return ["## QA", "", "QA was not run."]
    lines = [
        "## QA",
        "",
        "| Neighbour case | Result | Evidence |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {_cell(row.case)} | {row.result} | {_cell(row.evidence)} |"
        for row in qa.neighbour_matrix
    ]
    return lines


def _preflight_section(preflight: PreflightReport) -> list[str]:
    lines = ["## Pre-flight", ""]
    lines += [
        f"- {'✅' if check.ok else '❌'} {_one_line(check.name)}"
        for check in preflight.checks
    ]
    return lines


def pr_body(
    task: TaskRecord,
    issue: PMReply,
    decision: LeadDecision,
    qa: QAReply | None,
    preflight: PreflightReport,
    source_issue: int | None,
) -> str:
    blocks = [
        ["## Summary", "", _one_line(issue.summary)],
        [
            "## Acceptance criteria",
            "",
            *(f"- [ ] {_one_line(c)}" for c in issue.acceptance_criteria),
        ],
        _lead_section(decision),
        _qa_section(qa),
        _preflight_section(preflight),
    ]
    if source_issue is not None:
        blocks.append([f"Closes #{source_issue}"])
    blocks.append([HUMAN_MERGES])
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"
