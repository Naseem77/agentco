import json

import pytest
from orch_helpers import REVIEW_OK, STUCK, build_rig, engineer, issue_of
from script_runner import FULL, RED_CMD, ScriptRunner

from agentco.agents.prompts import load_prompt
from agentco.roles import Role
from agentco.sandbox.runner import CommandResult
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
    assert pre is None  # the final pre-flight passed, so nothing was stored


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


class FullFails(ScriptRunner):
    def run(self, command, cwd, timeout_s):
        if command == FULL:
            self.calls.append(command)
            return CommandResult(command, 1, "full suite failed", 0.0, False)
        return super().run(command, cwd, timeout_s)


async def test_fast_final_preflight_failure_returns_to_the_engineer(make_fast):
    script = {
        Role.SENIOR: [engineer(1), engineer(2)],
        Role.REVIEWER: [REVIEW_OK] * 2,
    }
    runner = FullFails(codes={RED_CMD: 1})
    rig = await make_fast(script, max_rounds=2, runner=runner)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.github.created == []
    assert "Pre-flight failures to make pass" in rig.calls(Role.SENIOR)[1].prompt
