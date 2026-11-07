"""The warm worktree pool in the orchestrator: claim, setup on a miss, PR rounds."""

import shutil
from pathlib import Path

import pytest
from orch_helpers import (
    LEAD_OK,
    PM_JSON,
    QA_OK,
    REVIEW_OK,
    RULES_OK,
    Rig,
    engineer,
    triage_json,
)
from script_runner import ScriptRunner, contract, git
from test_orchestrator_pr_reentry import CRASH, send_back_from_pr

from agentco.roles import Role
from agentco.sandbox.pool import WorktreePool, run_setup
from agentco.workflow.orchestrator import StepFailed
from agentco.workflow.states import TaskState

SETUP = "make setup"


def script(rounds: int = 1) -> dict[Role, list]:
    return {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage_json()] * rounds,
        Role.JUNIOR: [engineer(v) for v in range(1, rounds + 1)],
        Role.REVIEWER: [RULES_OK, REVIEW_OK, RULES_OK] * rounds,
        Role.QA: [QA_OK] * rounds,
        Role.LEAD: [LEAD_OK] * rounds,
    }


async def pooled_rig(
    make_rig, *, fill: bool, runner: ScriptRunner | None = None
) -> Rig:
    rig = await make_rig(script(), runner=runner)
    rig.ctx.contract = contract().model_copy(update={"setup": SETUP})
    rig.ctx.pool = WorktreePool(rig.repo, 1)
    if fill:
        added = rig.ctx.pool.fill(
            "origin/main",
            lambda p: run_setup(rig.runner, rig.ctx.contract, p, 5),
        )
        assert added == 1
    return rig


def meta(rig: Rig, key: str) -> str | None:
    return rig.store.get_meta(key)


async def test_pool_hit_skips_setup_and_miss_runs_it(make_rig):
    hit = await pooled_rig(make_rig, fill=True)
    assert hit.runner.calls.count(SETUP) == 1  # the fill ran it, ahead of time
    task = await hit.run()
    assert task.state is TaskState.PR_OPEN
    assert hit.runner.calls.count(SETUP) == 1  # and the task did not
    assert meta(hit, "task:1:setup") == "pool"
    assert meta(hit, "pool:acme:hit:1") is not None
    assert meta(hit, "pool:acme:miss:1") is None
    wt = hit.ctx.pool.owned_by(1)
    assert wt is not None and wt.path.parent == hit.repo / ".agentco/pool"
    assert not (hit.repo / ".agentco/worktrees/1").exists()
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-1"


async def test_pool_miss_runs_setup_once_in_a_fresh_worktree(make_rig):
    rig = await pooled_rig(make_rig, fill=False)
    task = await rig.run()
    assert task.state is TaskState.PR_OPEN
    assert rig.runner.calls.count(SETUP) == 1
    assert rig.runner.calls[0] == SETUP  # before any gate command
    assert meta(rig, "task:1:setup") == "done"
    assert meta(rig, "pool:acme:miss:1") is not None
    assert meta(rig, "pool:acme:hit:1") is None
    assert (rig.repo / ".agentco/worktrees/1").exists()


async def test_stale_pool_entry_is_a_miss(make_rig):
    rig = await pooled_rig(make_rig, fill=True)
    (rig.repo / "other.py").write_text("moved\n")
    git(rig.repo, "add", "-A")
    git(rig.repo, "commit", "-m", "upstream")
    git(rig.repo, "push", "origin", "main")
    await rig.run()
    assert meta(rig, "task:1:setup") == "done"
    assert rig.runner.calls.count(SETUP) == 2  # the fill, then the miss


async def test_no_setup_command_means_no_setup_step(make_rig):
    rig = await make_rig(script())
    await rig.run()
    assert SETUP not in rig.runner.calls and meta(rig, "task:1:setup") is None


async def test_setup_failure_stops_the_task_and_is_retried(make_rig):
    runner = ScriptRunner(codes={"pytest tests/test_new.py": 1, SETUP: 2})
    rig = await pooled_rig(make_rig, fill=False, runner=runner)
    with pytest.raises(StepFailed, match="setup failed"):
        await rig.run()
    assert meta(rig, "task:1:setup") is None
    runner.codes[SETUP] = 0
    task = await rig.run()  # the worktree is there, setup still has to happen
    assert task.state is TaskState.PR_OPEN
    assert runner.calls.count(SETUP) == 2 and meta(rig, "task:1:setup") == "done"


async def test_pr_round_never_claims(make_rig):
    rig = await make_rig(script(2))
    rig.ctx.contract = contract().model_copy(update={"setup": SETUP})
    rig.ctx.pool = WorktreePool(rig.repo, 1)
    await rig.run()  # a miss: the pool was empty
    rig.ctx.pool.fill(
        "origin/main", lambda p: run_setup(rig.runner, rig.ctx.contract, p, 5)
    )
    shutil.rmtree(rig.repo / ".agentco/worktrees/1")  # the worktree is gone
    send_back_from_pr(rig, CRASH)
    task = await rig.run()
    assert task.state is TaskState.PR_OPEN
    names = sorted(p.name for p in (rig.repo / ".agentco/pool").iterdir())
    assert any(n.endswith(".ready.json") for n in names)
    assert not any(".task-" in n for n in names)


async def test_pool_claimed_task_resumes_on_its_entry_in_a_pr_round(make_rig):
    rig = await make_rig(script(2))
    rig.ctx.contract = contract().model_copy(update={"setup": SETUP})
    rig.ctx.pool = WorktreePool(rig.repo, 1)
    rig.ctx.pool.fill(
        "origin/main", lambda p: run_setup(rig.runner, rig.ctx.contract, p, 5)
    )
    await rig.run()
    entry = rig.ctx.pool.owned_by(1).path
    send_back_from_pr(rig, CRASH)
    task = await rig.run()
    assert task.state is TaskState.PR_OPEN and task.round == 1
    assert len(rig.github.created) == 1 and len(rig.github.edited) == 1
    assert rig.ctx.pool.owned_by(1).path == entry
    assert git(entry, "log", "--format=%s", "-3").count("\n") >= 1


async def test_crashed_claim_is_finished_by_the_same_task(make_rig):
    rig = await pooled_rig(make_rig, fill=True)
    pool = rig.ctx.pool
    ready = next(m for _, s, m in pool._markers() if s == "ready")
    claimed = ready.with_name(ready.name.replace(".ready.", ".task-1."))
    ready.rename(claimed)  # the marker moved, the checkout never happened
    entry = Path(str(claimed).split(".task-")[0])
    (entry / "stray.txt").write_text("leftover")
    task = await rig.run()
    assert task.state is TaskState.PR_OPEN
    assert not (entry / "stray.txt").exists()
    assert git(entry, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-1"
    assert rig.runner.calls.count(SETUP) == 1
