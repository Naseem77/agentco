from typing import Literal

from pydantic import BaseModel, Field

from agentco.llm.replies import Reply


class PMReply(Reply):
    summary: str
    why: str
    acceptance_criteria: list[str] = Field(min_length=1)
    product_notes: str = ""


class TriageReply(Reply):
    route: Literal["junior", "senior", "new_design"]
    size: Literal["xs", "s", "m", "l", "xl"]
    risk: Literal["low", "medium", "high"]
    reason: str


class Finding(BaseModel):
    id: str
    severity: Literal["blocker", "major", "minor", "nit"]
    kind: Literal["bug", "edge_case", "tradeoff", "style"]
    path: str = ""
    line: int | None = None
    title: str
    detail: str


def issue_text(issue: PMReply) -> str:
    criteria = "\n".join(f"- {criterion}" for criterion in issue.acceptance_criteria)
    sections = [
        f"Issue: {issue.summary}",
        f"Why: {issue.why}",
        f"Acceptance criteria:\n{criteria}",
    ]
    if issue.product_notes:
        sections.append(f"Product notes: {issue.product_notes}")
    return "\n\n".join(sections)
