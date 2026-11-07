import json

from orch_helpers import RULES_OK, engineer, happy, triage_json
from script_runner import FIXED, LINT, ScriptRunner, git

from agentco.learning.followup import FOLLOWUP_SOURCE
from agentco.roles import Role
from agentco.sandbox.runner import CommandResult
from agentco.store.models import ArtifactKind
from agentco.workflow.orchestrator import Orchestrator
from agentco.workflow.states import TaskState

TEST_FILE = "tests/test_regression.py"
TEST_TEXT = "def test_regression():\n    assert True\n"
RED_DETAIL = "test-only change: regression tests for a merged fix"
NOTHING_TO_FIT = json.dumps(
    {"summary": "tests already fit", "files_touched": [], "tests_added": []}
)


def followup_task(rig, test_files, *, source=FOLLOWUP_SOURCE) -> int:
    task = rig.store.create_task("acme", "QA regression tests for #7: x", source)
    rig.store.put_artifact(
        task.id,
        ArtifactKind.REQUEST,
        Role.HUMAN,
        {"body": "Add QA's regression tests for task #7.", "test_files": test_files},
    )
    return task.id


def script_for(*junior):
    script = happy()
    script[Role.JUNIOR] = list(junior)
    return script


def red_of(rig, task_id, index=0) -> dict:
    artifact = rig.store.artifacts(task_id, ArtifactKind.PREFLIGHT)[index]
    return next(c for c in artifact.body["checks"] if c["name"] == "red")


def worktree_of(rig, task_id):
    return rig.repo / ".agentco/worktrees" / str(task_id)


async def test_followup_task_runs_normal_loop_to_pr_open(make_rig):
    seen = {}

    def inspect(call):
        seen["text"] = (call.workdir / TEST_FILE).read_text()
        seen["log"] = git(call.workdir, "log", "-1", "--format=%s")
        seen["status"] = git(call.workdir, "status", "--porcelain")
        return NOTHING_TO_FIT

    rig = await make_rig(script_for(inspect))
    task_id = followup_task(rig, {TEST_FILE: TEST_TEXT})

    task = await Orchestrator(rig.ctx).run(task_id)

    assert seen == {
        "text": TEST_TEXT,
        "log": "agentco: QA regression tests",
        "status": "",
    }
    assert task.state is TaskState.PR_OPEN
    [created] = rig.github.created
    assert created["head"] == f"agentco/task-{task_id}"
    pr = rig.store.latest_artifact(task_id, ArtifactKind.PR)
    assert pr is not None and pr.body == {"number": 1, "url": created["url"]}
    assert [r["detail"] for r in (red_of(rig, task_id),)] == [RED_DETAIL]
    pushed = git(
        rig.repo,
        "--git-dir",
        str(rig.origin),
        "show",
        f"agentco/task-{task_id}:{TEST_FILE}",
    )
    assert pushed == TEST_TEXT.strip()


async def test_the_engineer_may_make_the_tests_fit(make_rig):
    def fit(call):
        path = call.workdir / TEST_FILE
        path.write_text(path.read_text() + "\n\ndef test_extra():\n    assert True\n")
        return engineer(1, files={TEST_FILE: path.read_text()})(call)

    rig = await make_rig(script_for(fit))
    task_id = followup_task(rig, {TEST_FILE: TEST_TEXT})

    task = await Orchestrator(rig.ctx).run(task_id)

    assert task.state is TaskState.PR_OPEN
    assert "test_extra" in (worktree_of(rig, task_id) / TEST_FILE).read_text()
    assert red_of(rig, task_id)["detail"] == RED_DETAIL


async def test_a_normal_task_never_gets_the_test_only_pass(make_rig):
    rig = await make_rig(script_for(engineer(1, files={TEST_FILE: TEST_TEXT})))
    task_id = followup_task(rig, {}, source="cli")

    await Orchestrator(rig.ctx).run(task_id)

    assert red_of(rig, task_id)["detail"] == "test-only change: no fix to revert"
    assert rig.store.get(task_id).state is TaskState.PR_OPEN


async def test_a_followup_that_also_changes_source_keeps_the_red_check(make_rig):
    bare = [engineer(i, files={"src/calc.py": FIXED + f"# v{i}\n"}) for i in (1, 2)]
    script = script_for(*bare)
    script[Role.SENIOR] = bare
    script[Role.TRIAGE] = [triage_json()] * 2
    script[Role.REVIEWER] = [RULES_OK] * 6
    rig = await make_rig(script, max_rounds=1)
    task_id = followup_task(rig, {TEST_FILE: TEST_TEXT})

    await Orchestrator(rig.ctx).run(task_id)

    red = red_of(rig, task_id)
    assert (red["ok"], red["detail"]) == (
        False,
        f"tests pass without the fix: {TEST_FILE}",
    )


async def test_unsafe_test_files_in_the_request_are_not_written(make_rig, tmp_path):
    seen = {}

    def inspect(call):
        seen["files"] = sorted(
            str(p.relative_to(call.workdir))
            for p in (call.workdir / "tests").rglob("*.py")
        )
        seen["src"] = (call.workdir / "src/calc.py").read_text()
        return NOTHING_TO_FIT

    rig = await make_rig(script_for(inspect))
    task_id = followup_task(
        rig,
        {
            TEST_FILE: TEST_TEXT,
            "../test_up.py": "x",
            "src/calc.py": "BOOM = 1\n",
            "tests/../src/test_calc.py": "x",
        },
    )

    await Orchestrator(rig.ctx).run(task_id)

    assert seen["files"] == ["tests/test_add.py", TEST_FILE]
    assert "BOOM" not in seen["src"]
    assert not (rig.repo / ".agentco/worktrees/test_up.py").exists()


async def test_a_request_without_usable_tests_escalates_before_any_engineer_runs(
    make_rig,
):
    rig = await make_rig(script_for())
    task_id = followup_task(rig, {"../test_up.py": "x"})

    task = await Orchestrator(rig.ctx).run(task_id)

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.calls(Role.JUNIOR) == []
    [escalation] = [
        a.body for a in rig.store.artifacts(task_id, ArtifactKind.ESCALATION)
    ]
    assert (escalation["category"], escalation["reason"]) == (
        "stuck",
        "the QA test files in the request are missing or unsafe",
    )


class LintFailsOnce(ScriptRunner):
    def run(self, command, cwd, timeout_s):
        result = super().run(command, cwd, timeout_s)
        if command == LINT and self.calls.count(LINT) == 1:
            return CommandResult(command, 1, "lint: unused import", 0.0, False)
        return result


async def test_the_qa_tests_are_seeded_once_not_on_every_retry(make_rig):
    seen = {}

    def tweak(call):
        path = call.workdir / TEST_FILE
        path.write_text(TEST_TEXT + "# engineer tweak\n")
        return engineer(1, files={TEST_FILE: path.read_text()})(call)

    def retry(call):
        seen["retry"] = (call.workdir / TEST_FILE).read_text()
        path = call.workdir / TEST_FILE
        path.write_text(path.read_text() + "# second tweak\n")
        return engineer(2, files={TEST_FILE: path.read_text()})(call)

    rig = await make_rig(script_for(tweak, retry), runner=LintFailsOnce())
    task_id = followup_task(rig, {TEST_FILE: TEST_TEXT})

    task = await Orchestrator(rig.ctx).run(task_id)

    assert task.state is TaskState.PR_OPEN
    assert seen["retry"] == TEST_TEXT + "# engineer tweak\n"
    assert (
        (worktree_of(rig, task_id) / TEST_FILE).read_text().endswith("# second tweak\n")
    )


async def test_a_retry_after_a_failed_preflight_must_change_something(make_rig):
    rig = await make_rig(
        script_for(lambda call: NOTHING_TO_FIT, lambda call: NOTHING_TO_FIT),
        runner=LintFailsOnce(),
    )
    task_id = followup_task(rig, {TEST_FILE: TEST_TEXT})

    task = await Orchestrator(rig.ctx).run(task_id)

    assert task.state is TaskState.AWAITING_HUMAN
    [escalation] = [
        a.body for a in rig.store.artifacts(task_id, ArtifactKind.ESCALATION)
    ]
    assert escalation["reason"] == "engineer made no changes"
