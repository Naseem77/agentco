import json
from pathlib import Path

import pytest
from orch_helpers import (
    LEAD_OK,
    PM_JSON,
    QA_FAIL,
    QA_OK,
    REVIEW_OK,
    RULES_OK,
    STUCK,
    CountingRuntime,
    engineer,
    happy,
    issue_of,
    triage_json,
)
from script_runner import FIXED, LINT, NEW_TEST, RED_CMD, ScriptRunner, git

from agentco.agents.prompts import load_prompt
from agentco.github.client import FakeGitHub
from agentco.llm.fake import FakeRuntime
from agentco.llm.runtime import AgentResult
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.orchestrator import StepFailed, run_many
from agentco.workflow.pr import pr_title
from agentco.workflow.states import TaskState

FINDING = {
    "id": "f1",
    "severity": "major",
    "kind": "bug",
    "path": "src/calc.py",
    "title": "Operand order lost",
    "detail": "swapping breaks subtraction callers",
}
REVIEW_FINDING = json.dumps({"findings": [FINDING], "summary": "one bug"})
LEAD_SEND_BACK = json.dumps(
    {
        "confidence": 0.9,
        "classifications": [{"finding_id": "f1", "category": "real_bug", "note": "x"}],
        "summary": "fix it",
        "comments": ["tighten operand handling"],
    }
)


def advance_main(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "upstream")
    git(repo, "push", "origin", "main")


def everything_said(rig) -> list[str]:
    return [t for c in rig.runtime.calls for t in (c.system, c.prompt)]


async def test_happy_path_opens_pr_last(make_rig):
    rig = await make_rig(happy())

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert rig.transitions() == [
        "pm",
        "triage",
        "engineering",
        "preflight",
        "internal_review",
        "lead_decision",
        "pr_open",
    ]
    [created] = rig.github.created
    assert created["head"] == "agentco/task-1" and created["base"] == "main"
    assert created["title"] == pr_title(task, issue_of(rig))
    branches = git(rig.repo, "--git-dir", str(rig.origin), "branch", "--list")
    assert "agentco/task-1" in branches
    pr = rig.store.latest_artifact(rig.task_id, ArtifactKind.PR)
    assert pr is not None
    assert pr.body == {"number": 1, "url": created["url"]}
    assert rig.github.edited == []


async def test_preflight_runs_before_review_and_cost_is_recorded(make_rig):
    rig = await make_rig(happy())

    await rig.run()

    roles = [c.role for c in rig.runtime.calls]
    assert roles[:4] == [Role.PM, Role.TRIAGE, Role.JUNIOR, Role.REVIEWER]
    assert set(roles[4:6]) == {Role.REVIEWER, Role.QA} and roles[6:] == [
        Role.LEAD,
        Role.REVIEWER,
    ]
    first_reviewer = rig.calls(Role.REVIEWER)[0]
    assert first_reviewer.system.startswith(load_prompt(Role.REVIEWER, "rules"))
    assert len(roles) == 8
    assert rig.task().cost_usd == pytest.approx(0.04 * 8)


async def test_send_back_never_opens_pr_and_reruns_with_scoped_findings(make_rig):
    github = FakeGitHub([], [], [], {})
    seen: list[int] = []
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [
            engineer(
                files={
                    "src/calc.py": FIXED + "# v1\n",
                    "tests/test_new.py": NEW_TEST + "# ROUND_ZERO_ONLY\n",
                },
                summary="ENGINEER_SECRET_SUMMARY",
            ),
            engineer(
                files={"src/calc.py": FIXED + "# v2 fixed\n"},
                before=lambda call: seen.append(len(github.created)),
            ),
        ],
        Role.REVIEWER: [RULES_OK, REVIEW_FINDING, RULES_OK, REVIEW_OK, RULES_OK],
        Role.QA: [QA_OK, QA_OK],
        Role.LEAD: [LEAD_SEND_BACK, LEAD_OK],
    }
    rig = await make_rig(script, github=github)

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN and task.round == 1
    assert ("lead_decision", "triage") in rig.edges()
    assert seen == [0]  # no PR existed while the fix was written
    first, second = rig.calls(Role.JUNIOR)
    assert "Operand order lost" not in first.prompt
    assert "Operand order lost" in second.prompt
    assert "swapping breaks subtraction callers" in second.prompt
    review2 = rig.calls(Role.REVIEWER)[3]
    assert "# v2 fixed" in review2.prompt and "ROUND_ZERO_ONLY" not in review2.prompt
    assert "Operand order lost" in review2.prompt  # findings addressed
    for call in [*rig.calls(Role.REVIEWER), *rig.calls(Role.QA), second]:
        assert "ENGINEER_SECRET_SUMMARY" not in call.prompt
        assert "ENGINEER_SECRET_SUMMARY" not in call.system
    rules2 = rig.calls(Role.REVIEWER)[2]
    assert "tighten operand handling" in rules2.prompt  # lead comments reach pre-flight
    assert len(github.created) == 1


async def test_fix_since_is_recorded_after_the_rebase(make_rig):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [
            engineer(
                files={
                    "src/calc.py": FIXED + "# v1\n",
                    "tests/test_new.py": NEW_TEST + "# ROUND_ZERO_ONLY\n",
                }
            ),
            engineer(
                files={"src/calc.py": FIXED + "# v2 fixed\n"},
                before=lambda call: advance_main(
                    call.workdir.parents[2], {"docs/NOTES.md": "UPSTREAM_ONLY\n"}
                ),
            ),
        ],
        Role.REVIEWER: [RULES_OK, REVIEW_FINDING, RULES_OK, REVIEW_OK, RULES_OK],
        Role.QA: [QA_OK, QA_OK],
        Role.LEAD: [LEAD_SEND_BACK, LEAD_OK],
    }
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    review2 = rig.calls(Role.REVIEWER)[3]
    assert "# v2 fixed" in review2.prompt
    assert "UPSTREAM_ONLY" not in review2.prompt
    assert "ROUND_ZERO_ONLY" not in review2.prompt
    wt = rig.repo / ".agentco/worktrees/1"
    assert (wt / "docs/NOTES.md").read_text() == "UPSTREAM_ONLY\n"  # really rebased


async def test_three_junior_rounds_reassign_senior_then_escalate(make_rig):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 6,
        Role.JUNIOR: [engineer(i) for i in (1, 2, 3)],
        Role.SENIOR: [engineer(i) for i in (4, 5, 6)],
        Role.REVIEWER: [RULES_OK, REVIEW_OK] * 6,
        Role.QA: [QA_FAIL] * 6,
        Role.LEAD: [LEAD_OK] * 6,
    }
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert task.assignee is Role.SENIOR and task.reassigned and task.round == 3
    engineers = [
        c.role for c in rig.runtime.calls if c.role in (Role.JUNIOR, Role.SENIOR)
    ]
    assert engineers == [Role.JUNIOR] * 3 + [Role.SENIOR] * 3
    assert "mul broke" in rig.calls(Role.JUNIOR)[1].prompt
    assert rig.edges().count(("lead_decision", "triage")) == 6
    assert rig.edges()[-1] == ("triage", "awaiting_human")
    assert rig.escalations() == [
        {
            "from_state": "triage",
            "category": "stuck",
            "reason": "round limit reached (3)",
            "options": ["answer: one more round", "reject"],
            "question": "",
            "stop": "rounds",
        }
    ]
    assert rig.github.created == []


async def test_preflight_exhausted_counts_as_a_round(make_rig):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [engineer(1), engineer(2)],
        Role.SENIOR: [engineer(3), engineer(4)],
        Role.REVIEWER: [RULES_OK] * 4,
    }
    rig = await make_rig(
        script,
        max_rounds=1,
        runner=ScriptRunner(codes={RED_CMD: 1, LINT: 1}),
    )

    task = await rig.run()

    assert rig.transitions() == [
        "pm",
        "triage",
        "engineering",
        "preflight",
        "engineering",  # same round: the first attempt may be fixed
        "preflight",
        "triage",  # attempts exhausted: round +1
        "engineering",
        "preflight",
        "engineering",
        "preflight",
        "triage",
        "awaiting_human",
    ]
    assert task.round == 1 and task.reassigned
    brief = rig.calls(Role.JUNIOR)[1].prompt
    assert "Pre-flight failures to make pass" in brief
    assert "lint" in brief and "line 99" in brief
    assert rig.calls(Role.QA) == [] and rig.calls(Role.LEAD) == []
    assert len(rig.store.artifacts(rig.task_id, ArtifactKind.PREFLIGHT)) == 4
    assert rig.escalations()[0]["stop"] == "rounds"


async def test_rebase_conflict_goes_back_to_triage(make_rig):
    def conflicting_main(call):
        advance_main(
            call.workdir.parents[2],
            {"src/calc.py": "def add(a, b):\n    return a + b + 0\n"},
        )

    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [engineer(1, before=conflicting_main)],
        Role.SENIOR: [
            engineer(2, files={"src/calc.py": "def add(a, b):\n    return b\n"})
        ],
    }
    rig = await make_rig(script, max_rounds=1)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.edges().count(("preflight", "triage")) == 2
    last = rig.store.latest_artifact(rig.task_id, ArtifactKind.PREFLIGHT)
    assert last is not None and last.body["conflict"] is True
    assert LINT not in rig.runner.calls  # nothing ran on a conflicting branch
    assert git(rig.repo / ".agentco/worktrees/1", "status", "--porcelain") == ""
    assert rig.github.created == []


async def test_budget_overrun_escalates_stuck(make_rig):
    rig = await make_rig(
        {Role.PM: [PM_JSON], Role.TRIAGE: [triage_json()]}, premium=100
    )

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.escalations() == [
        {
            "from_state": "engineering",
            "category": "stuck",
            "reason": "budget",
            "options": ["add budget", "reject"],
            "question": "",
        }
    ]
    assert task.cost_usd == pytest.approx(8.0)
    assert rig.calls(Role.JUNIOR) == []


async def test_stuck_junior_is_handed_to_a_senior_without_leaking_the_reason(make_rig):
    script = happy()
    script[Role.JUNIOR] = [STUCK]
    script[Role.SENIOR] = [engineer(1)]
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert task.assignee is Role.SENIOR and task.reassigned and task.round == 0
    assert [
        c.role for c in rig.runtime.calls if c.role in (Role.JUNIOR, Role.SENIOR)
    ] == [
        Role.JUNIOR,
        Role.SENIOR,
    ]
    assert rig.escalations() == []
    assert not any("SECRET_REASON_XYZ" in text for text in everything_said(rig))


async def test_stuck_senior_escalates_with_the_reason_only_in_the_escalation(make_rig):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()],
        Role.JUNIOR: [STUCK],
        Role.SENIOR: [AgentResult("", 1.0, stuck=True, stuck_reason="SENIOR_LOST")],
    }
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "engineering"
    assert escalation["category"] == "stuck"
    assert "SENIOR_LOST" in escalation["reason"]
    assert not any("SENIOR_LOST" in text for text in everything_said(rig))


async def test_push_failure_escalates_and_never_opens_a_pr(make_rig):
    rig = await make_rig(happy(), github=FakeGitHub([], [], [], {}, fail_writes=True))

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.github.created == [] and rig.github.edited == []
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "lead_decision"
    assert escalation["category"] == "stuck"
    assert escalation["reason"].startswith("push/gh failed: gh write failed")
    assert rig.store.latest_artifact(rig.task_id, ArtifactKind.PR) is None


async def test_rejected_git_push_escalates_and_never_opens_a_pr(make_rig):
    rig = await make_rig(happy())
    hook = rig.origin / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.github.created == []
    [escalation] = rig.escalations()
    assert escalation["reason"].startswith("push/gh failed:")


async def test_critical_path_escalates_instead_of_approving(make_rig):
    script = happy()
    script[Role.JUNIOR] = [
        engineer(
            files={
                "src/calc.py": FIXED,
                "src/auth.py": "def login():\n    return True\n",
                "tests/test_new.py": NEW_TEST,
            }
        )
    ]
    script[Role.REVIEWER] = [RULES_OK, REVIEW_OK]
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "lead_decision"
    assert escalation["category"] == "critical"
    assert escalation["reason"].startswith("critical change: security: src/auth.py")
    assert rig.github.created == []


async def test_unknown_area_escalates_new(make_rig):
    rig = await make_rig({Role.PM: [PM_JSON], Role.TRIAGE: [triage_json()]}, seed=False)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "triage" and escalation["category"] == "new"
    assert escalation["reason"].startswith("unknown area")
    assert rig.calls(Role.JUNIOR) == []


async def test_triage_stuck_escalates(make_rig):
    rig = await make_rig({Role.PM: [PM_JSON], Role.TRIAGE: [STUCK]})

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "triage" and escalation["category"] == "stuck"
    assert "SECRET_REASON_XYZ" in escalation["reason"]


@pytest.mark.parametrize(
    ("steps", "cost"),
    [(["nope", "still nope"], 0.08), ([STUCK], 0.04)],
    ids=["invalid-twice", "stuck"],
)
async def test_pm_failure_raises_step_failed_and_stays_in_pm(make_rig, steps, cost):
    rig = await make_rig({Role.PM: steps})

    with pytest.raises(StepFailed) as raised:
        await rig.run()

    assert raised.value.state is TaskState.PM
    assert rig.task().state is TaskState.PM
    assert rig.task().cost_usd == pytest.approx(cost)


@pytest.mark.parametrize(
    "steps",
    [["bad", "bad"] * 2, [AgentResult("", 1.0, stuck=True, stuck_reason="r")] * 2],
    ids=["invalid", "stuck"],
)
async def test_preflight_rules_failure_fails_the_round_not_the_loop(make_rig, steps):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [engineer(1)],
        Role.SENIOR: [engineer(2)],
        Role.REVIEWER: steps,
    }
    rig = await make_rig(script, max_rounds=1)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    reports = rig.store.artifacts(rig.task_id, ArtifactKind.PREFLIGHT)
    assert [r.body["ok"] for r in reports] == [False, False]
    assert reports[0].body["checks"][0]["name"] == "rules"
    assert task.cost_usd == pytest.approx(0.04 * len(rig.runtime.calls))
    assert rig.calls(Role.QA) == []


@pytest.mark.parametrize(
    "steps",
    [["bad", "bad"], [AgentResult("", 1.0, stuck=True, stuck_reason="lead stuck")]],
    ids=["invalid", "stuck"],
)
async def test_lead_failure_escalates_stuck(make_rig, steps):
    script = happy()
    script[Role.LEAD] = steps
    rig = await make_rig(script)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["from_state"] == "lead_decision"
    assert escalation["category"] == "stuck"
    assert escalation["reason"].startswith("lead failed")
    assert rig.github.created == []
    assert task.cost_usd == pytest.approx(0.04 * len(rig.runtime.calls))


async def test_second_open_updates_the_same_pr(make_rig, monkeypatch):
    script = happy()
    script[Role.REVIEWER] = [RULES_OK, REVIEW_OK, RULES_OK, RULES_OK]
    script[Role.LEAD] = [LEAD_OK, LEAD_OK]
    rig = await make_rig(script)
    real = rig.store.transition
    crashed: list[bool] = []

    def flaky(task_id, to, actor, reason):
        if to is TaskState.PR_OPEN and not crashed:
            crashed.append(True)
            raise RuntimeError("crash after the PR was created")
        return real(task_id, to, actor, reason)

    monkeypatch.setattr(rig.store, "transition", flaky)

    with pytest.raises(RuntimeError, match="crash after"):
        await rig.run()
    assert len(rig.github.created) == 1
    assert rig.task().state is TaskState.LEAD_DECISION

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.github.created) == 1
    [edited] = rig.github.edited
    assert edited["number"] == 1 and edited["repo"] == "acme/app"
    assert edited["body"] == rig.github.created[0]["body"]
    pr = rig.store.latest_artifact(rig.task_id, ArtifactKind.PR)
    assert pr is not None and pr.body == {
        "number": 1,
        "url": rig.github.created[0]["url"],
    }


async def test_run_many_respects_the_limit(make_rig):
    runtime = CountingRuntime(
        FakeRuntime({Role.PM: [PM_JSON] * 4, Role.TRIAGE: [triage_json()] * 4})
    )
    rig = await make_rig({}, seed=False, runtime=runtime)
    ids = [rig.task_id, *(rig.new_task(f"t{i}") for i in range(3))]

    results = await run_many([(rig.ctx, i) for i in ids], limit=2)

    assert [r.state for r in results] == [TaskState.AWAITING_HUMAN] * 4
    assert [r.id for r in results] == ids
    assert runtime.peak == 2


async def test_run_many_returns_failures_instead_of_raising(make_rig):
    runtime = FakeRuntime({Role.PM: ["x", "y", PM_JSON], Role.TRIAGE: [triage_json()]})
    rig = await make_rig({}, seed=False, runtime=runtime)
    second = rig.new_task("second")

    first_result, second_result = await run_many(
        [(rig.ctx, rig.task_id), (rig.ctx, second)], limit=1
    )

    assert isinstance(first_result, StepFailed)
    assert second_result.state is TaskState.AWAITING_HUMAN
