import re

from agentco.agents.lead import LeadDecision
from agentco.agents.qa import NeighbourRow, QAReply
from agentco.agents.replies import PMReply
from agentco.github.models import _CLOSING_RE
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


def test_pr_body_defangs_llm_closing_keywords_mentions_and_refs():
    nasty = "Fixes #99 cc @bob, resolves https://github.com/o/r/issues/8"
    body = pr_body(
        task(),
        issue(nasty),
        decision(("Closes owner/other#9 ping @org/team",)),
        QaWith(nasty),
        PREFLIGHT,
        3,
    )
    assert _CLOSING_RE.findall(body) == ["3"]
    assert "@bob" not in body and "@org" not in body
    assert not re.search(r"#99|#9\b", body)
    assert not re.search(r"(?i)resolves\s+https", body)
    assert "Closes #3" in body.splitlines()


def QaWith(text):
    return QAReply(
        passed=True,
        bug_class="x",
        tests_run=[],
        neighbour_matrix=[NeighbourRow(case=text, result="pass", evidence=text)],
    )


def test_pr_title_defangs_mentions_but_keeps_task_suffix():
    title = pr_title(task(7), issue("ping @bob about #12"))
    assert "@bob" not in title and "#12" not in title
    assert title.endswith(" (agentco #7)")


def test_title_empty_summary_has_no_leading_space():
    assert pr_title(task(7), issue("  ")) == "agentco #7"


def test_title_never_exceeds_limit_for_huge_task_id():
    assert len(pr_title(task(10**80), issue("Fix it"))) <= 72


def test_pr_body_is_capped_and_truncates_qa_table():
    big = QAReply(
        passed=True,
        bug_class="x",
        tests_run=[],
        neighbour_matrix=[
            NeighbourRow(case=f"case {i}", result="pass", evidence="e" * 500)
            for i in range(400)
        ],
    )
    body = pr_body(task(), issue(), decision(), big, PREFLIGHT, 3)
    assert len(body) <= 60000
    assert "… truncated" in body
    assert "| case 0 |" in body
    assert body.rstrip("\n").splitlines()[-1] == LAST_LINE
