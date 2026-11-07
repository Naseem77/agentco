import re

from agentco.agents.lead import LeadDecision
from agentco.agents.qa import QAReply
from agentco.agents.replies import PMReply
from agentco.store.models import TaskRecord
from agentco.workflow.preflight import PreflightReport

TITLE_LIMIT = 72
HUMAN_MERGES = "Ready to merge — a human merges this PR. agentco never merges."


BODY_LIMIT = 60000
_ZWSP = "\u200b"
# Only a keyword that precedes a reference acts on GitHub; plain "fix" stays readable.
_CLOSING = re.compile(
    r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?=:?\s+(?:\S*#\d|https?://))"
)


def _defang(text: str) -> str:
    text = _CLOSING.sub(lambda m: m.group(1)[0] + _ZWSP + m.group(1)[1:], text)
    text = text.replace("@", "@" + _ZWSP)
    return re.sub(r"#(?=\d)", "#" + _ZWSP, text)


def _one_line(text: str) -> str:
    return _defang(" ".join(text.split()))


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def pr_title(task: TaskRecord, issue: PMReply) -> str:
    suffix = f"agentco #{task.id}"
    summary = _one_line(issue.summary)
    if not summary:
        title = suffix
    else:
        suffix = f" ({suffix})"
        room = TITLE_LIMIT - len(suffix)
        if len(summary) > room:
            summary = summary[: max(room - 1, 0)].rstrip() + "…"
        title = summary + suffix
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT - 1] + "…"
    return title


def _lead_section(decision: LeadDecision) -> list[str]:
    lines = ["## Lead review", "", _one_line(decision.summary) or "Approved."]
    if decision.comments:
        lines += ["", *(f"- {_one_line(c)}" for c in decision.comments)]
    return lines


def _qa_section(qa: QAReply | None, max_rows: int | None = None) -> list[str]:
    if qa is None:
        return ["## QA", "", "QA was not run."]
    lines = [
        "## QA",
        "",
        "| Neighbour case | Result | Evidence |",
        "| --- | --- | --- |",
    ]
    rows = qa.neighbour_matrix
    shown = rows if max_rows is None else rows[:max_rows]
    lines += [
        f"| {_cell(row.case)} | {row.result} | {_cell(row.evidence)} |" for row in shown
    ]
    if len(shown) < len(rows):
        lines += ["", f"… truncated ({len(rows) - len(shown)} more rows)"]
    return lines


def _preflight_section(preflight: PreflightReport) -> list[str]:
    lines = ["## Pre-flight", ""]
    lines += [
        f"- {'✅' if check.ok else '❌'} {_one_line(check.name)}"
        for check in preflight.checks
    ]
    return lines


def _render(blocks: list[list[str]]) -> str:
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def pr_body(
    task: TaskRecord,
    issue: PMReply,
    decision: LeadDecision,
    qa: QAReply | None,
    preflight: PreflightReport,
    source_issue: int | None,
) -> str:
    def build(max_rows: int | None) -> str:
        blocks = [
            ["## Summary", "", _one_line(issue.summary)],
            [
                "## Acceptance criteria",
                "",
                *(f"- [ ] {_one_line(c)}" for c in issue.acceptance_criteria),
            ],
            _lead_section(decision),
            _qa_section(qa, max_rows),
            _preflight_section(preflight),
        ]
        if source_issue is not None:
            blocks.append([f"Closes #{source_issue}"])
        blocks.append([HUMAN_MERGES])
        return _render(blocks)

    body = build(None)
    rows = 0 if qa is None else len(qa.neighbour_matrix)
    while len(body) > BODY_LIMIT and rows > 0:
        rows //= 2
        body = build(rows)
    return body
