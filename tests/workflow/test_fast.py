from agentco.agents.replies import Finding
from agentco.agents.review import ReviewReply
from agentco.workflow.fast import fast_lead, issue_from_request


def _f(id_: str, severity: str, kind: str = "bug") -> Finding:
    return Finding(id=id_, severity=severity, kind=kind, title=id_, detail="d")


def test_issue_bullets_become_acceptance_criteria():
    body = "Context line.\n\n- [ ] keeps spaces\n* handles tabs\n1. no crash\n"
    issue = issue_from_request("Parser tabs", body)
    assert issue.summary == "Parser tabs"
    assert issue.why == body.strip()
    assert issue.acceptance_criteria == ["keeps spaces", "handles tabs", "no crash"]


def test_issue_without_bullets_gets_fallback_criterion():
    issue = issue_from_request("Fix it", "")
    assert issue.why == "Fix it"
    assert issue.acceptance_criteria == ["Resolve the issue exactly as described."]


def test_missing_review_escalates():
    d = fast_lead(None, "review: timeout")
    assert (d.outcome, d.category) == ("escalate", "stuck")
    assert "timeout" in d.reason


def test_blocking_sends_back_every_non_nit():
    review = ReviewReply(
        findings=[_f("F1", "major"), _f("F2", "minor"), _f("F3", "nit")], summary="s"
    )
    d = fast_lead(review, None)
    assert d.outcome == "send_back"
    assert [f.id for f in d.findings_to_fix] == ["F1", "F2"]


def test_minor_bug_alone_is_sent_back():
    review = ReviewReply(findings=[_f("F1", "minor"), _f("F2", "nit")], summary="s")
    d = fast_lead(review, None)
    assert d.outcome == "send_back"
    assert [f.id for f in d.findings_to_fix] == ["F1"]


def test_minor_tradeoff_and_nit_are_approved():
    review = ReviewReply(
        findings=[_f("F1", "minor", "tradeoff"), _f("F2", "nit")], summary="s"
    )
    d = fast_lead(review, None)
    assert d.outcome == "approve"
    assert [f.id for f in d.follow_ups] == ["F1", "F2"]
    assert d.findings_to_fix == ()
