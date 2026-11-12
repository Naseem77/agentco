import json

import pytest
from orch_helpers import REVIEW_OK, STUCK, build_rig, engineer, issue_of
from script_runner import LINT, RED_CMD, ScriptRunner

from agentco.agents.prompts import load_prompt
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState

BLOCKER = {
    "id": "F1",
    "severity": "blocker",
    "kind": "bug",
    "path": "src/calc.py",
    "title": "Breaks subtraction",
    "detail": "BLOCKER_DETAIL_XYZ",
}
REVIEW_BLOCKER = json.dumps({"findings": [BLOCKER], "summary": "one blocker"})


@pytest.fixture
def make_fast(tmp_path, store, loop_cfg, make_repo):
    cfg = loop_cfg.model_copy(
        update={"loop": loop_cfg.loop.model_copy(update={"pipeline": "fast"})}
    )

    async def make(script, **kwargs):
        return await build_rig(tmp_path, store, cfg, make_repo, script, **kwargs)

    return make


async def test_fast_happy_path_uses_only_senior_and_one_review(make_fast):
    rig = await make_fast({Role.SENIOR: [engineer(1)], Role.REVIEWER: [REVIEW_OK]})

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
    assert [c.role for c in rig.runtime.calls] == [Role.SENIOR, Role.REVIEWER]
    [review] = rig.calls(Role.REVIEWER)
    assert review.system.startswith(load_prompt(Role.REVIEWER, "fast"))
    assert "Get it right the first time" in rig.calls(Role.SENIOR)[0].prompt
    assert issue_of(rig).summary == "Make add commutative-safe"
    assert len(rig.github.created) == 1
    pre = rig.store.latest_artifact(rig.task_id, ArtifactKind.PREFLIGHT)
    assert pre is not None and pre.body["ok"] is True  # checked before the review


async def test_fast_blocker_goes_back_once_with_the_findings(make_fast):
    script = {
        Role.SENIOR: [engineer(1), engineer(2)],
        Role.REVIEWER: [REVIEW_BLOCKER, REVIEW_OK],
    }
    rig = await make_fast(script)

    task = await rig.run()

    assert task.state is TaskState.PR_OPEN and task.round == 1
    second = rig.calls(Role.SENIOR)[1].prompt
    assert "BLOCKER_DETAIL_XYZ" in second
    assert "Impact of the diff" in second
    assert "F1" in rig.calls(Role.REVIEWER)[1].prompt  # prior findings re-checked
    lead = rig.store.latest_artifact(rig.task_id, ArtifactKind.LEAD)
    assert lead is not None and lead.body["outcome"] == "approve"


async def test_fast_round_limit_escalates_without_a_pr(make_fast):
    script = {
        Role.SENIOR: [engineer(1), engineer(2)],
        Role.REVIEWER: [REVIEW_BLOCKER] * 2,
    }
    rig = await make_fast(script, max_rounds=1)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert [e["stop"] for e in rig.escalations()] == ["rounds"]
    assert rig.github.created == []


async def test_fast_review_failure_escalates_stuck(make_fast):
    rig = await make_fast({Role.SENIOR: [engineer(1)], Role.REVIEWER: [STUCK]})

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = rig.escalations()
    assert escalation["category"] == "stuck"


async def test_fast_preflight_failure_goes_back_before_the_review(make_fast):
    script = {
        Role.SENIOR: [engineer(1), engineer(2)],
        Role.REVIEWER: [REVIEW_OK],
    }
    runner = ScriptRunner(codes={RED_CMD: 1, LINT: 1})
    rig = await make_fast(script, max_rounds=1, runner=runner)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.transitions()[:5] == [
        "pm",
        "triage",
        "engineering",
        "preflight",
        "engineering",
    ]
    assert "Pre-flight failures to make pass" in rig.calls(Role.SENIOR)[1].prompt
    assert rig.calls(Role.REVIEWER) == []
