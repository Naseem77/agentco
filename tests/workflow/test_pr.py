from agentco.agents.lead import LeadDecision
from agentco.agents.qa import NeighbourRow, QAReply
from agentco.agents.replies import PMReply
from agentco.roles import Role
from agentco.store.models import TaskRecord
from agentco.workflow.pr import pr_body, pr_title
from agentco.workflow.preflight import CheckResult, PreflightReport

LAST_LINE = "Ready to merge — a human merges this PR. agentco never merges."


def task(id=7):
    return TaskRecord(
        id=id,
        tenant="t",
        parent_id=None,
        title="x",
        source="issue",
        source_ref="3",
        state="PR_OPEN",
        assignee=Role.JUNIOR,
        round=1,
        reassigned=False,
        cost_usd=0.0,
    )


def issue(summary="Fix login redirect"):
    return PMReply(
        summary=summary,
        why="SECRET-WHY",
        acceptance_criteria=["Redirects to /home", "Keeps query string"],
        product_notes="SECRET-NOTES",
    )


def decision(comments=("Tighten the name",)):
    return LeadDecision(
        outcome="approve",
        category="approved",
        findings_to_fix=(),
        follow_ups=(),
        reason="SECRET-REASON",
        summary="Looks good overall.",
        comments=tuple(comments),
        question="",
        options=(),
    )


QA = QAReply(
    passed=True,
    bug_class="redirect",
    tests_run=["pytest tests/test_login.py"],
    neighbour_matrix=[
        NeighbourRow(case="logout | redirect", result="pass", evidence="ok\nline2"),
        NeighbourRow(case="signup", result="n/a", evidence="no code path"),
    ],
)
PREFLIGHT = PreflightReport(
    ok=True,
    checks=(
        CheckResult("lint", True, "SECRET-DETAIL"),
        CheckResult("build", False, "boom"),
    ),
    conflict=False,
)


def test_title_format():
    assert pr_title(task(7), issue()) == "Fix login redirect (agentco #7)"


def test_title_truncated_to_72_characters():
    title = pr_title(task(123), issue("word " * 40))
    assert len(title) == 72
    assert title.endswith("… (agentco #123)")


def test_title_collapses_newlines():
    assert pr_title(task(1), issue("a\nb")) == "a b (agentco #1)"


def test_pr_body_exact():
    assert pr_body(task(), issue(), decision(), QA, PREFLIGHT, 3) == (
        "## Summary\n\nFix login redirect\n\n"
        "## Acceptance criteria\n\n- [ ] Redirects to /home\n- [ ] Keeps query string\n\n"
        "## Lead review\n\nLooks good overall.\n\n- Tighten the name\n\n"
        "## QA\n\n| Neighbour case | Result | Evidence |\n| --- | --- | --- |\n"
        "| logout \\| redirect | pass | ok line2 |\n"
        "| signup | n/a | no code path |\n\n"
        "## Pre-flight\n\n- ✅ lint\n- ❌ build\n\n"
        f"Closes #3\n\n{LAST_LINE}\n"
    )


def test_pr_body_closes_issue_and_says_human_merges():
    body = pr_body(task(), issue(), decision(), QA, PREFLIGHT, 3)
    assert "Closes #3" in body.splitlines()
    assert body.rstrip("\n").splitlines()[-1] == LAST_LINE


def test_pr_body_without_issue_has_no_closes_line():
    body = pr_body(task(), issue(), decision(), QA, PREFLIGHT, None)
    assert "Closes" not in body
    assert body.rstrip("\n").splitlines()[-1] == LAST_LINE


def test_pr_body_without_qa():
    body = pr_body(task(), issue(), decision(()), None, PREFLIGHT, None)
    assert "## QA\n\nQA was not run.\n" in body
    assert "| Neighbour case" not in body
    assert "## Lead review\n\nLooks good overall.\n\n## QA" in body


def test_pr_body_omits_engineer_and_internal_reasoning():
    body = pr_body(task(), issue(), decision(), QA, PREFLIGHT, 3)
    for secret in ("SECRET-WHY", "SECRET-NOTES", "SECRET-REASON", "SECRET-DETAIL"):
        assert secret not in body
