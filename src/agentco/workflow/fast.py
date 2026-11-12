"""Fast pipeline rules: the PM and lead steps without an LLM.

Fast mode keeps two agents per round, a senior engineer and a pre-PR reviewer.
The issue text is the brief, and the lead is a rule over the reviewer's findings.
"""

import re

from agentco.agents.lead import LeadDecision
from agentco.agents.replies import Finding, PMReply
from agentco.agents.review import ReviewReply

BLOCKING = frozenset({"blocker", "major"})
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
_FALLBACK_CRITERION = "Resolve the issue exactly as described."


def issue_from_request(title: str, body: str) -> PMReply:
    """The issue as written is the brief; its bullet lines are the acceptance criteria."""
    text = body.strip()
    criteria = [m.group(1) for line in text.splitlines() if (m := _BULLET.match(line))]
    return PMReply(
        summary=title.strip() or (text.splitlines()[0] if text else ""),
        why=text or title,
        acceptance_criteria=criteria or [_FALLBACK_CRITERION],
        product_notes="",
    )


def _decision(outcome: str, category: str, reason: str, **kw) -> LeadDecision:
    return LeadDecision(
        outcome=outcome,  # type: ignore[arg-type]
        category=category,
        findings_to_fix=kw.get("fix", ()),
        follow_ups=kw.get("follow_ups", ()),
        reason=reason,
        summary=kw.get("summary", reason),
        comments=(),
        question="",
        options=(),
    )


def fast_lead(review: ReviewReply | None, error: str | None) -> LeadDecision:
    """Blocker/major findings go back in one batch; otherwise the work is approved."""
    if review is None:
        return _decision("escalate", "stuck", f"review failed: {error or 'no result'}")
    blocking = [f for f in review.findings if f.severity in BLOCKING]
    if blocking:
        fix: tuple[Finding, ...] = tuple(
            f for f in review.findings if f.severity != "nit"
        )
        return _decision(
            "send_back",
            "fix",
            f"{len(blocking)} blocking finding(s)",
            fix=fix,
            summary=review.summary,
        )
    return _decision(
        "approve",
        "approved",
        "pre-PR review found nothing blocking",
        follow_ups=tuple(review.findings),
        summary=review.summary,
    )
