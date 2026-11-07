import pytest
from orch_helpers import (
    LEAD_OK,
    PM_JSON,
    QA_FAIL,
    QA_OK,
    REVIEW_OK,
    RULES_OK,
    STUCK,
    engineer,
    happy,
    triage_json,
)
from script_runner import NEW_TEST, git

from agentco.llm.runtime import AgentResult
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState

AUTH = "def login():\n    return True\n"


def critical_script(rounds: int = 1) -> dict:
    return {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * rounds,
        Role.JUNIOR: [
            engineer(
                i,
                files={
                    "src/auth.py": AUTH + f"# v{i}\n",
                    "tests/test_new.py": NEW_TEST,
                },
            )
            for i in range(1, rounds + 1)
        ],
        Role.REVIEWER: [REVIEW_OK] * rounds,
        Role.QA: [QA_OK] * rounds,
        Role.LEAD: [LEAD_OK] * rounds,
    }


async def test_approve_after_a_lead_escalation_opens_the_pr(make_rig):
    rig = await make_rig(critical_script())
    assert (await rig.run()).state is TaskState.AWAITING_HUMAN
    assert rig.github.created == []

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.github.created) == 1
    assert rig.edges()[-1] == ("awaiting_human", "pr_open")
    assert task.round == 0


async def test_reject_cancels_without_a_pr(make_rig):
    rig = await make_rig(critical_script())
    await rig.run()

    rig.decide("reject", "not wanted")
    task = await rig.run()

    assert task.state is TaskState.CANCELLED
    assert rig.github.created == []
    assert rig.transitions()[-1] == "cancelled"


async def test_no_decision_means_run_returns_immediately(make_rig):
    rig = await make_rig(critical_script())
    first = await rig.run()
    calls = len(rig.runtime.calls)

    again = await rig.run()

    assert again.state is TaskState.AWAITING_HUMAN and again == first
    assert len(rig.runtime.calls) == calls
    assert len(rig.escalations()) == 1


@pytest.mark.parametrize("decision", ["answer", "approve"])
async def test_a_human_buys_exactly_one_more_round_after_a_stop_rule(
    make_rig, decision
):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 3,
        Role.JUNIOR: [engineer(1)],
        Role.SENIOR: [engineer(2), engineer(3)],
        Role.REVIEWER: [RULES_OK, REVIEW_OK] * 3,
        Role.QA: [QA_FAIL] * 3,
        Role.LEAD: [LEAD_OK] * 3,
    }
    rig = await make_rig(script, max_rounds=1)
    assert (await rig.run()).state is TaskState.AWAITING_HUMAN
    assert [e["stop"] for e in rig.escalations()] == ["rounds"]
    assert len(rig.calls(Role.SENIOR)) == 1

    rig.decide(decision, "go on")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN and task.round == 2
    assert len(rig.calls(Role.SENIOR)) == 2
    assert "Note from the human: go on" in rig.calls(Role.SENIOR)[1].prompt
    assert [e["stop"] for e in rig.escalations()] == ["rounds", "rounds"]
    assert rig.edges().count(("awaiting_human", "triage")) == 1

    # a second run without a new decision does nothing
    calls = len(rig.runtime.calls)
    await rig.run()
    assert len(rig.runtime.calls) == calls


async def test_unknown_area_then_lead_escalation_both_need_a_human(make_rig):
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [engineer(1)],
        Role.REVIEWER: [REVIEW_OK],
        Role.QA: [QA_OK],
        Role.LEAD: [LEAD_OK],
    }
    rig = await make_rig(script, seed=False)
    await rig.run()
    assert rig.escalations()[-1]["from_state"] == "triage"

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert [(e["from_state"], e["category"]) for e in rig.escalations()] == [
        ("triage", "new"),
        ("lead_decision", "new"),
    ]
    assert rig.github.created == []

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.github.created) == 1


async def test_answer_to_a_lead_escalation_becomes_a_finding_in_the_same_round(
    make_rig,
):
    rig = await make_rig(critical_script(rounds=2))
    await rig.run()

    rig.decide("answer", "use a feature flag")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN and task.round == 0
    second = rig.calls(Role.JUNIOR)[1].prompt
    assert "Fix exactly these findings" in second
    assert "use a feature flag" in second
    assert [e["category"] for e in rig.escalations()] == ["critical", "critical"]


async def test_answer_to_an_engineering_escalation_is_a_note_for_the_engineer(
    make_rig,
):
    script = happy()
    script[Role.JUNIOR] = [STUCK]
    script[Role.SENIOR] = [
        AgentResult("", 1.0, stuck=True, stuck_reason="SENIOR_LOST"),
        engineer(1),
    ]
    rig = await make_rig(script)
    await rig.run()
    assert rig.escalations()[-1]["from_state"] == "engineering"

    rig.decide("answer", "start with calc.py")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert rig.edges().count(("awaiting_human", "engineering")) == 1
    assert "Note from the human: start with calc.py" in rig.calls(Role.SENIOR)[1].prompt
    assert len(rig.calls(Role.TRIAGE)) == 1
    for call in rig.runtime.calls:
        assert (
            "SENIOR_LOST" not in call.prompt and "SECRET_REASON_XYZ" not in call.prompt
        )


async def test_approve_retries_a_failed_open_and_stays_put_when_it_fails_again(
    make_rig,
):
    script = happy()
    script[Role.REVIEWER] = [RULES_OK, REVIEW_OK, RULES_OK, RULES_OK, RULES_OK]
    rig = await make_rig(script)
    rig.github.fail_writes = True
    await rig.run()
    assert [e["from_state"] for e in rig.escalations()] == ["lead_decision"]

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert [e["from_state"] for e in rig.escalations()] == [
        "lead_decision",
        "lead_decision",
    ]
    assert rig.github.created == []

    rig.github.fail_writes = False
    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.github.created) == 1


def calls_after_an_approval(extra: dict) -> dict:
    """Round two, run after a human approves a stop that was not a lead decision."""
    return {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * 2,
        Role.JUNIOR: [engineer(1), engineer(2)],
        Role.REVIEWER: [RULES_OK, REVIEW_OK, RULES_OK, REVIEW_OK, RULES_OK],
        Role.QA: [QA_OK] * 2,
        Role.LEAD: [LEAD_OK] * 2,
        **extra,
    }


async def test_approve_on_a_budget_stop_before_the_lead_does_not_open_a_pr(make_rig):
    script = calls_after_an_approval({Role.QA: [AgentResult(QA_OK, 200.0), QA_OK]})
    rig = await make_rig(script)
    await rig.run()
    assert rig.escalations()[-1]["from_state"] == "lead_decision"
    assert rig.escalations()[-1]["reason"] == "budget"
    assert rig.calls(Role.LEAD) == []

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.github.created == []
    assert rig.edges()[-3:] == [
        ("lead_decision", "awaiting_human"),
        ("awaiting_human", "triage"),
        ("triage", "awaiting_human"),
    ]
    assert git(rig.repo, "--git-dir", str(rig.origin), "branch", "--list") == "* main"
    assert rig.escalations()[-1]["from_state"] == "triage"


async def test_a_budget_top_up_lets_the_stopped_task_through_to_the_lead(make_rig):
    script = calls_after_an_approval({Role.QA: [AgentResult(QA_OK, 200.0), QA_OK]})
    rig = await make_rig(script)
    await rig.run()
    assert len(rig.calls(Role.LEAD)) == 0

    rig.store.set_meta(f"task:{rig.task_id}:extra_budget", "1000.0")
    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.calls(Role.LEAD)) == 1
    assert len(rig.github.created) == 1
    assert [e["reason"] for e in rig.escalations()] == ["budget"]


async def test_a_budget_top_up_resumes_an_engineering_stop(make_rig):
    rig = await make_rig(happy(), premium=100.0)
    await rig.run()
    assert [e["reason"] for e in rig.escalations()] == ["budget"]
    assert rig.calls(Role.JUNIOR) == []

    rig.store.set_meta(f"task:{rig.task_id}:extra_budget", "1000.0")
    rig.decide("answer")
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.calls(Role.JUNIOR)) == 1
    assert [e["reason"] for e in rig.escalations()] == ["budget"]


@pytest.mark.parametrize("decision", ["approve", "answer"])
async def test_a_failed_lead_is_asked_again_never_skipped_by_an_approval(
    make_rig, decision
):
    script = calls_after_an_approval({Role.LEAD: [STUCK, LEAD_OK]})
    rig = await make_rig(script)
    await rig.run()
    assert rig.escalations()[-1]["reason"].startswith("lead failed")
    assert rig.store.latest_artifact(rig.task_id, ArtifactKind.LEAD) is None

    rig.decide(decision)
    task = await rig.run()

    assert task.state is TaskState.PR_OPEN
    assert len(rig.calls(Role.LEAD)) == 2
    assert rig.edges().count(("awaiting_human", "triage")) == 1
    assert len(rig.github.created) == 1
    created_after_lead = rig.store.latest_artifact(rig.task_id, ArtifactKind.LEAD)
    assert created_after_lead is not None and created_after_lead.body["outcome"] == (
        "approve"
    )


async def test_a_missing_lead_artifact_escalates_and_never_leaves_a_pushed_branch(
    make_rig, monkeypatch
):
    rig = await make_rig(critical_script())
    await rig.run()
    assert rig.escalations()[-1]["open_pr"] is True
    real = rig.store.latest_artifact

    def without_lead(task_id, kind):
        return None if kind is ArtifactKind.LEAD else real(task_id, kind)

    monkeypatch.setattr(rig.store, "latest_artifact", without_lead)

    rig.decide("approve")
    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.github.created == []
    assert git(rig.repo, "--git-dir", str(rig.origin), "branch", "--list") == "* main"
    last = rig.escalations()[-1]
    assert last["category"] == "stuck"
    assert last["reason"] == "no lead decision to open a PR from"
    assert "open_pr" not in last

    before = len(rig.escalations())
    again = await rig.run()
    assert again.state is TaskState.AWAITING_HUMAN
    assert len(rig.escalations()) == before


async def test_an_approve_note_is_not_guidance_when_the_final_preflight_fails(
    make_rig,
):
    rig = await make_rig(critical_script(rounds=2), max_rounds=3)
    await rig.run()
    runner = rig.runner
    runner.default = 0
    original = runner.run

    def failing_full_suite(command, cwd, timeout_s):
        if command == "pytest-all" and rig.task().state is TaskState.AWAITING_HUMAN:
            from agentco.sandbox.runner import CommandResult

            runner.calls.append(command)
            return CommandResult(command, 1, "full suite failed", 0.0, False)
        return original(command, cwd, timeout_s)

    runner.run = failing_full_suite

    rig.decide("approve", "ship it, looks great")
    task = await rig.run()

    assert rig.github.created == []
    assert task.state is TaskState.AWAITING_HUMAN
    second = rig.calls(Role.JUNIOR)[1].prompt
    assert "ship it" not in second
    assert "Human guidance" not in second


async def test_a_human_answer_resets_the_preflight_attempts(make_rig):
    from script_runner import LINT, RED_CMD, ScriptRunner

    stuck_senior = AgentResult("", 1.0, stuck=True, stuck_reason="LOST")
    script = {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()],
        Role.JUNIOR: [engineer(1), STUCK],
        Role.SENIOR: [stuck_senior, engineer(2), engineer(3)],
        Role.REVIEWER: [RULES_OK] * 6,
    }
    rig = await make_rig(
        script, max_rounds=1, runner=ScriptRunner(codes={RED_CMD: 1, LINT: 1})
    )
    await rig.run()
    assert rig.escalations()[-1]["from_state"] == "engineering"

    rig.decide("answer", "try the other module")
    await rig.run()

    edges = rig.edges()
    resumed = edges[edges.index(("awaiting_human", "engineering")) :]
    assert resumed[:4] == [
        ("awaiting_human", "engineering"),
        ("engineering", "preflight"),
        ("preflight", "engineering"),
        ("engineering", "preflight"),
    ]
