import asyncio
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from script_runner import (
    BUILD,
    E2E,
    FILES,
    FIXED,
    FULL,
    LINT,
    NEW_TEST,
    ORIGINAL,
    RED_CMD,
    TESTS_CMD,
    ScriptRunner,
    contract,
    git,
)

from agentco.agents.prompts import load_prompt
from agentco.config import LoopLimits
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.roles import Role
from agentco.sandbox.runner import CommandResult
from agentco.sandbox.worktree import commit_all, create_worktree
from agentco.workflow import preflight as preflight_module
from agentco.workflow.preflight import (
    CheckResult,
    Preflight,
    PreflightReport,
    RestoreError,
    red_copy_path,
)

RULES_OK = '{"violations": []}'


@dataclass
class Rig:
    preflight: Preflight
    runtime: FakeRuntime
    backend: InMemoryBackend


def make_preflight(
    runner,
    store,
    *,
    replies: list[str] | None = None,
    e2e: str | None = None,
    limits: LoopLimits | None = None,
    fmt: str | None = None,
) -> Rig:
    runtime = FakeRuntime({Role.REVIEWER: replies or []})
    task = store.create_task("acme/app", "t", "")
    backend = InMemoryBackend()
    return Rig(
        Preflight(
            repo="acme/app",
            contract=contract(e2e).model_copy(update={"format": fmt}),
            runner=runner,
            limits=limits or LoopLimits(),
            memory=MemoryGate("acme", backend).for_role(Role.REVIEWER),
            asker=Asker(runtime, store, task.id, 0.04),
            reviewer_model="rev-model",
        ),
        runtime,
        backend,
    )


def add_test(wt, rel: str = "tests/test_new.py", text: str = NEW_TEST) -> None:
    (wt.path / rel).write_text(text)


def fix(wt, rel: str = "src/calc.py") -> None:
    (wt.path / rel).write_text(FIXED)


def fixed_worktree(make_repo, with_test: bool = True):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    if with_test:
        add_test(wt)
    fix(wt)
    commit_all(wt, "fix")
    return root, wt


async def go(rig: Rig, wt, **kw) -> PreflightReport:
    args = {"fix_since": None, "final": False, "lead_comments": []}
    return await rig.preflight.run(wt, "main", **{**args, **kw})


def names(rep: PreflightReport) -> list[tuple[str, bool]]:
    return [(c.name, c.ok) for c in rep.checks]


async def test_all_checks_run_and_failures_collected(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={LINT: 1, TESTS_CMD: 1})
    rep = await go(make_preflight(runner, store), wt)
    assert names(rep) == [
        ("rebase", True),
        ("size", True),
        ("rules", True),
        ("lint", False),
        ("build", True),
        ("tests", False),
        ("red", False),
    ]
    assert rep.ok is False and rep.conflict is False
    assert [c.name for c in rep.failures()] == ["lint", "tests", "red"]
    # the red check builds its own copy of the commit
    assert sorted(runner.calls) == sorted([LINT, BUILD, BUILD, TESTS_CMD, RED_CMD])
    lint = rep.checks[3]
    assert lint.detail == "\n".join(f"line {i}" for i in range(40, 100))


async def test_format_check_runs_when_the_contract_has_one(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1, "fmt-check": 1})
    rep = await go(make_preflight(runner, store, fmt="fmt-check"), wt)
    assert ("format", False) in names(rep)
    assert [c.name for c in rep.failures()] == ["format"]


async def test_all_green_report(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rep = await go(make_preflight(runner, store), wt)
    assert rep.ok is True and rep.failures() == []
    assert rep.as_dict() == {
        "ok": True,
        "conflict": False,
        "checks": [
            {"name": "rebase", "ok": True, "detail": ""},
            {"name": "size", "ok": True, "detail": "4 changed lines against main"},
            {"name": "rules", "ok": True, "detail": "no review rules apply"},
            {"name": "lint", "ok": True, "detail": ""},
            {"name": "build", "ok": True, "detail": ""},
            {"name": "tests", "ok": True, "detail": ""},
            {
                "name": "red",
                "ok": True,
                "detail": "new tests fail without the fix (exit 1)",
            },
        ],
    }


async def test_new_test_that_passes_without_fix_is_flagged(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(default=0)
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert not red.ok
    assert red.detail == "tests pass without the fix: tests/test_new.py"
    assert runner.calc_at_call[RED_CMD] == ORIGINAL  # the fix was reverted for it
    assert runner.calc_at_call[TESTS_CMD] == FIXED


async def test_red_ok_when_new_tests_fail_without_fix(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 2})
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert (red.ok, red.detail) == (True, "new tests fail without the fix (exit 2)")


async def test_red_removes_added_source_file_and_restores_it(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    fix(wt)
    (wt.path / "src/extra.py").write_text("def extra():\n    return 1\n")
    commit_all(wt, "fix")
    seen: dict[str, bool] = {}

    class Spy(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            seen[command] = (cwd / "src/extra.py").exists()
            return super().run(command, cwd, timeout_s)

    await go(make_preflight(Spy(), store), wt)
    assert seen[TESTS_CMD] is True and seen[RED_CMD] is False
    assert (wt.path / "src/extra.py").read_text() == "def extra():\n    return 1\n"
    assert git(wt.path, "status", "--porcelain") == ""


async def test_red_check_restores_worktree_even_on_crash(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    head = git(wt.path, "rev-parse", "HEAD")
    runner = ScriptRunner(raise_on=RED_CMD)
    with pytest.raises(RuntimeError, match="runner crashed"):
        await go(make_preflight(runner, store), wt)
    assert runner.calc_at_call[RED_CMD] == ORIGINAL  # it crashed mid-revert
    assert git(wt.path, "status", "--porcelain") == ""
    assert git(wt.path, "rev-parse", "HEAD") == head
    assert (wt.path / "src/calc.py").read_text() == FIXED


class BlockingRunner(ScriptRunner):
    """Blocks inside the red command until released."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, command, cwd, timeout_s):
        if command == RED_CMD:
            self.entered.set()
            self.release.wait(10)
        return super().run(command, cwd, timeout_s)


async def start_blocked_red(make_repo, store, runner):
    _, wt = fixed_worktree(make_repo)
    task = asyncio.create_task(go(make_preflight(runner, store), wt))
    while not runner.entered.is_set():
        await asyncio.sleep(0.01)
    assert (wt.path / "src/calc.py").read_text() == FIXED  # reverted in a copy
    assert (red_copy_path(wt) / "src/calc.py").read_text() == ORIGINAL
    return wt, task


async def test_cancel_during_red_restores_the_fix(make_repo, store):
    runner = BlockingRunner()
    wt, task = await start_blocked_red(make_repo, store, runner)
    head = git(wt.path, "rev-parse", "HEAD")
    task.cancel()
    runner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (wt.path / "src/calc.py").read_text() == FIXED
    assert git(wt.path, "status", "--porcelain") == ""
    assert git(wt.path, "rev-parse", "HEAD") == head
    assert not any(red_copy_path(wt).iterdir())


async def test_second_cancel_cannot_interrupt_the_restore(
    make_repo, store, monkeypatch
):
    real_restore = preflight_module._restore

    def slow_restore(wt):
        threading.Event().wait(0.3)
        real_restore(wt)

    monkeypatch.setattr(preflight_module, "_restore", slow_restore)
    runner = BlockingRunner()
    wt, task = await start_blocked_red(make_repo, store, runner)
    task.cancel()
    runner.release.set()
    await asyncio.sleep(0.1)  # the restore is now running in its thread
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (wt.path / "src/calc.py").read_text() == FIXED
    assert git(wt.path, "status", "--porcelain") == ""


async def test_failed_restore_is_reported_as_possibly_reverted(
    make_repo, store, monkeypatch
):
    def broken_restore(wt):
        raise OSError("disk gone")

    monkeypatch.setattr(preflight_module, "_restore", broken_restore)
    _, wt = fixed_worktree(make_repo)
    with pytest.raises(RestoreError) as e:
        await go(make_preflight(ScriptRunner(codes={RED_CMD: 1}), store), wt)
    assert str(e.value) == (
        f"could not restore the worktree at {wt.path}; it may be left reverted "
        "to the state without the fix: disk gone"
    )


async def test_command_artifacts_do_not_break_red(make_repo, store):
    _, wt = fixed_worktree(make_repo)

    class Artifacts(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            if command == BUILD:
                (cwd / "dist.txt").write_text("build output")
            return super().run(command, cwd, timeout_s)

    rep = await go(make_preflight(Artifacts(codes={RED_CMD: 1}), store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert (red.ok, red.detail) == (True, "new tests fail without the fix (exit 1)")
    assert git(wt.path, "status", "--porcelain") == ""
    assert (wt.path / "src/calc.py").read_text() == FIXED


async def test_red_keeps_ignored_dependency_dirs(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    exclude = Path(git(wt.path, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else wt.path / exclude
    exclude.write_text(exclude.read_text() + "node_modules/\n")
    (wt.path / "node_modules").mkdir()
    (wt.path / "node_modules/dep.js").write_text("x")
    await go(make_preflight(ScriptRunner(codes={RED_CMD: 1}), store), wt)
    assert (wt.path / "node_modules/dep.js").read_text() == "x"


async def test_uncommitted_work_fails_fast_before_any_command(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    (wt.path / "notes.txt").write_text("keep me")
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt)
    assert [(c.name, c.ok, c.detail) for c in rep.checks] == [
        ("rebase", True, ""),
        ("clean", False, "uncommitted changes in the worktree: commit first"),
    ]
    assert runner.calls == []
    assert (wt.path / "notes.txt").read_text() == "keep me"


@pytest.mark.parametrize("code", [124, 127])
async def test_red_inconclusive_when_command_cannot_run(make_repo, store, code):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: code})
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert not red.ok
    assert red.detail.splitlines()[0] == (
        f"could not run the new tests without the fix (exit {code})"
    )


async def test_conflict_fails_fast(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    fix(wt)
    commit_all(wt, "fix")
    (root / "src/calc.py").write_text(ORIGINAL.replace("a + b", "a - b"))
    git(root, "commit", "-am", "other")
    head = git(wt.path, "rev-parse", "HEAD")
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt)
    assert rep == PreflightReport(
        ok=False,
        checks=(CheckResult("rebase", False, "conflict rebasing onto main"),),
        conflict=True,
    )
    assert runner.calls == []
    assert git(wt.path, "rev-parse", "HEAD") == head  # rebase was aborted
    assert git(wt.path, "status", "--porcelain") == ""


async def test_rebases_onto_moved_base(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    fix(wt)
    commit_all(wt, "fix")
    (root / "README.md").write_text("hi\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "docs")
    rep = await go(make_preflight(ScriptRunner(codes={RED_CMD: 1}), store), wt)
    assert rep.ok and rep.checks[0] == CheckResult("rebase", True, "")
    assert (wt.path / "README.md").exists()


async def test_fix_round_size_guard(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    since = git(wt.path, "rev-parse", "HEAD")
    (wt.path / "src/big.py").write_text("".join(f"X{i} = {i}\n" for i in range(200)))
    commit_all(wt, "more")
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt, fix_since=since)
    assert rep.ok is False and rep.conflict is False
    assert names(rep) == [("rebase", True), ("size", False)]
    assert rep.checks[1].detail == (
        f"200 changed lines since {since} exceeds the limit of 150; keep fixes small"
    )
    assert runner.calls == []


async def test_fix_round_measures_only_the_fix(make_repo, store):
    _, wt = fixed_worktree(make_repo)  # 4 lines vs main, well under 150
    since = git(wt.path, "rev-parse", "HEAD")
    (wt.path / "src/more.py").write_text("".join(f"Y{i} = 1\n" for i in range(100)))
    commit_all(wt, "more")
    limits = LoopLimits(max_changed_lines=30, fix_max_changed_lines=150)
    rep = await go(
        make_preflight(ScriptRunner(), store, limits=limits), wt, fix_since=since
    )
    assert dict(names(rep))["size"] is True
    assert rep.checks[1].detail == f"100 changed lines since {since}"


async def test_first_round_size_guard(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner()
    limits = LoopLimits(max_changed_lines=3)
    rep = await go(make_preflight(runner, store, limits=limits), wt)
    assert names(rep) == [("rebase", True), ("size", False)]
    assert rep.checks[1].detail == (
        "4 changed lines against main exceeds the limit of 3; keep fixes small"
    )
    assert runner.calls == []


async def test_rules_llm_only_when_rules_exist(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    rig = make_preflight(ScriptRunner(codes={RED_CMD: 1}), store)
    rep = await go(rig, wt)
    assert rig.runtime.calls == []
    assert rep.checks[2] == CheckResult("rules", True, "no review rules apply")


async def test_rules_violation_from_memory_fails_with_blind_inputs(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    reply = '{"violations": ["no reordering in src/calc.py:2: keep a + b"]}'
    rig = make_preflight(ScriptRunner(codes={RED_CMD: 1}), store, replies=[reply])
    from datetime import UTC, datetime

    await rig.backend.add(
        graph_name("acme", Scope.RND),
        Scope.RND,
        MemoryItem(
            "r", "ReviewRule: keep operand order in src/calc.py", "t", datetime.now(UTC)
        ),
    )
    rep = await go(rig, wt, lead_comments=["use shorter names"])
    assert rep.checks[2] == CheckResult(
        "rules", False, "no reordering in src/calc.py:2: keep a + b"
    )
    assert rep.ok is False
    [call] = rig.runtime.calls
    assert call.role is Role.REVIEWER and call.model == "rev-model"
    assert call.workdir is None and call.edits is False
    assert call.system.startswith(load_prompt(Role.REVIEWER, "rules"))
    assert (
        "- ReviewRule: keep operand order in src/calc.py\n- use shorter names"
        in call.prompt
    )
    assert "+    return b + a" in call.prompt  # the diff
    assert call.max_turns == 40 and call.timeout_s == 1200


async def test_rules_from_lead_comments_alone_trigger_one_call(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    rig = make_preflight(
        ScriptRunner(codes={RED_CMD: 1}), store, replies=['{"violations": []}']
    )
    rep = await go(rig, wt, lead_comments=["add a docstring"])
    assert len(rig.runtime.calls) == 1
    assert rep.checks[2] == CheckResult("rules", True, "checked 1 rule(s)")
    assert rep.ok


async def test_final_runs_full_suite_and_e2e(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rep = await go(make_preflight(runner, store, e2e=E2E), wt, final=True)
    assert rep.ok
    assert sorted(runner.calls) == sorted([LINT, BUILD, BUILD, FULL, RED_CMD, E2E])
    assert runner.calls[-1] == E2E
    assert names(rep)[-2:] == [("red", True), ("e2e", True)]


async def test_non_final_skips_e2e_and_full_suite(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    await go(make_preflight(runner, store, e2e=E2E), wt)
    assert sorted(runner.calls) == sorted([LINT, BUILD, BUILD, TESTS_CMD, RED_CMD])


async def test_final_without_e2e_configured_skips_it(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rep = await go(make_preflight(runner, store), wt, final=True)
    assert [c.name for c in rep.checks][-1] == "red"
    assert E2E not in runner.calls


async def test_e2e_failure_is_reported_with_the_others(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1, E2E: 1, BUILD: 1})
    rep = await go(make_preflight(runner, store, e2e=E2E), wt, final=True)
    assert [c.name for c in rep.failures()] == ["build", "e2e"]


async def test_no_new_test_fails_without_reason(make_repo, store):
    _, wt = fixed_worktree(make_repo, with_test=False)
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert (red.ok, red.detail) == (False, "no new test")
    assert sorted(runner.calls) == sorted([LINT, BUILD, "pytest tests/test_add.py"])


async def test_no_test_reason_passes_red(make_repo, store):
    _, wt = fixed_worktree(make_repo, with_test=False)
    rep = await go(
        make_preflight(ScriptRunner(), store), wt, no_test_reason="docs only"
    )
    red = {c.name: c for c in rep.checks}["red"]
    assert (red.ok, red.detail) == (True, "no new test: docs only")
    assert rep.ok


async def test_test_only_change_has_no_fix_to_revert(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    commit_all(wt, "test")
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert (red.ok, red.detail) == (True, "test-only change: no fix to revert")
    # the tests check ran them once
    assert sorted(runner.calls) == sorted([LINT, BUILD, RED_CMD])


TEST_ONLY = "test-only change: regression tests for a merged fix"


async def red_of(rig, wt, **kw):
    rep = await go(rig, wt, **kw)
    return {c.name: c for c in rep.checks}["red"]


async def test_red_check_test_only_passes_only_when_flagged(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    commit_all(wt, "tests")

    flagged = await red_of(make_preflight(ScriptRunner(), store), wt, test_only=True)
    plain = await red_of(make_preflight(ScriptRunner(), store), wt)

    assert (flagged.ok, flagged.detail) == (True, TEST_ONLY)
    assert (plain.ok, plain.detail) == (True, "test-only change: no fix to revert")


async def test_test_only_passes_when_the_tests_are_removed_but_not_for_normal_tasks(
    make_repo, store
):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    (wt.path / "tests/test_add.py").unlink()
    commit_all(wt, "drop")

    flagged = await red_of(make_preflight(ScriptRunner(), store), wt, test_only=True)
    plain = await red_of(make_preflight(ScriptRunner(), store), wt)

    assert (flagged.ok, flagged.detail) == (True, TEST_ONLY)
    assert (plain.ok, plain.detail) == (False, "no new test")


async def test_test_only_does_not_weaken_red_when_a_source_file_changed(
    make_repo, store
):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 0})

    red = await red_of(make_preflight(runner, store), wt, test_only=True)

    assert (red.ok, red.detail) == (
        False,
        "tests pass without the fix: tests/test_new.py",
    )
    assert runner.calc_at_call[RED_CMD] == ORIGINAL


async def test_test_only_without_tests_needs_a_reason_like_any_task(make_repo, store):
    _, wt = fixed_worktree(make_repo, with_test=False)

    bare = await red_of(make_preflight(ScriptRunner(), store), wt, test_only=True)
    reasoned = await red_of(
        make_preflight(ScriptRunner(), store),
        wt,
        test_only=True,
        no_test_reason="docs only",
    )

    assert (bare.ok, bare.detail) == (False, "no new test")
    assert (reasoned.ok, reasoned.detail) == (True, "no new test: docs only")


async def test_test_only_with_a_non_test_file_alone_still_fails(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    (wt.path / "README.md").write_text("docs\n")
    commit_all(wt, "docs")

    red = await red_of(make_preflight(ScriptRunner(), store), wt, test_only=True)

    assert (red.ok, red.detail) == (False, "no new test")


async def test_test_only_with_no_changes_at_all_does_not_pass(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")

    red = await red_of(make_preflight(ScriptRunner(), store), wt, test_only=True)

    assert (red.ok, red.detail) == (False, "no new test")


async def test_prepare_runs_once_per_worktree(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rig = make_preflight(runner, store)
    await go(rig, wt)
    await go(rig, wt)
    assert runner.prepared == [wt.path, red_copy_path(wt)]


async def test_prepare_failure_is_a_clear_preflight_failure(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(prepare_error="devcontainer up failed (exit 1): no docker")
    rig = make_preflight(runner, store)
    rep = await go(rig, wt)
    assert rep.ok is False and rep.conflict is False
    assert names(rep) == [
        ("rebase", True),
        ("size", True),
        ("rules", True),
        ("prepare", False),
    ]
    assert rep.checks[-1].detail == "devcontainer up failed (exit 1): no docker"
    assert runner.calls == []
    runner.prepare_error = None  # a failed prepare is retried next round
    rep = await go(rig, wt)
    assert runner.prepared == [wt.path, red_copy_path(wt)] and runner.calls != []


async def test_runner_without_prepare_is_fine(make_repo, store):
    _, wt = fixed_worktree(make_repo)

    class Host:
        def run(self, command, cwd, timeout_s):
            return CommandResult(
                command, 1 if command == RED_CMD else 0, "", 0.0, False
            )

    rep = await go(make_preflight(Host(), store), wt)
    assert rep.ok


async def test_timeout_detail_mentions_the_limit(make_repo, store):
    _, wt = fixed_worktree(make_repo)

    class Slow(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            result = super().run(command, cwd, timeout_s)
            if command == LINT:
                return CommandResult(command, 124, "partial", 0.0, True)
            return result

    limits = LoopLimits(command_timeout_s=30)
    rep = await go(make_preflight(Slow(), store, limits=limits), wt)
    assert rep.checks[3] == CheckResult("lint", False, "timed out after 30s\npartial")


class LintLeak(ScriptRunner):
    def run(self, command, cwd, timeout_s):
        if command == LINT:
            (cwd / "lint-report.txt").write_text("report")
            (cwd / "reports").mkdir(exist_ok=True)
            (cwd / "reports/out.xml").write_text("<x/>")
        if command == E2E:
            (cwd / "e2e-output.txt").write_text("shots")
        return super().run(command, cwd, timeout_s)


async def test_no_test_path_leaves_the_worktree_clean(make_repo, store):
    _, wt = fixed_worktree(make_repo, with_test=False)
    rep = await go(make_preflight(LintLeak(), store), wt)
    assert {c.name: c.detail for c in rep.checks}["red"] == "no new test"
    assert git(wt.path, "status", "--porcelain", "--untracked-files=all") == ""


async def test_test_only_path_leaves_the_worktree_clean(make_repo, store):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    commit_all(wt, "test")
    await go(make_preflight(LintLeak(), store), wt)
    assert git(wt.path, "status", "--porcelain", "--untracked-files=all") == ""


async def test_final_e2e_output_is_removed(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = LintLeak(codes={RED_CMD: 1})
    rep = await go(make_preflight(runner, store, e2e=E2E), wt, final=True)
    assert rep.ok
    assert git(wt.path, "status", "--porcelain", "--untracked-files=all") == ""


async def test_clean_check_sees_untracked_files_despite_git_config(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    git(wt.path, "config", "status.showUntrackedFiles", "no")
    (wt.path / "notes.txt").write_text("keep me")
    runner = ScriptRunner()
    rep = await go(make_preflight(runner, store), wt)
    assert [(c.name, c.ok) for c in rep.checks] == [("rebase", True), ("clean", False)]
    assert runner.calls == []


async def test_exit_5_no_tests_collected_is_not_a_red_proof(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    rep = await go(make_preflight(ScriptRunner(codes={RED_CMD: 5}), store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert not red.ok
    assert red.detail.splitlines()[0] == (
        "new tests were not collected (exit 5): cannot prove they fail without the fix"
    )


async def add_rule(
    rig: Rig, text: str = "ReviewRule: keep operand order in src/calc.py"
):
    from datetime import UTC, datetime

    await rig.backend.add(
        graph_name("acme", Scope.RND),
        Scope.RND,
        MemoryItem("r", text, "t", datetime.now(UTC)),
    )


async def test_cache_hit_skips_runner(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rig = make_preflight(runner, store, replies=[RULES_OK, RULES_OK])
    await add_rule(rig)
    first = await go(rig, wt)
    second = await go(rig, wt)
    assert first.ok and second == first
    assert sorted(runner.calls) == sorted([LINT, BUILD, BUILD, TESTS_CMD, RED_CMD])
    assert len(rig.runtime.calls) == 1  # the rules check was cached too


async def test_cache_misses_after_new_commit(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rig = make_preflight(runner, store, replies=[RULES_OK, RULES_OK])
    await add_rule(rig)
    await go(rig, wt)
    first = len(runner.calls)
    (wt.path / "src/calc.py").write_text(FIXED + "# tweak\n")
    commit_all(wt, "tweak")
    await go(rig, wt)
    assert len(runner.calls) == 2 * first == 10  # every command ran again
    for command in (LINT, RED_CMD):
        assert runner.calls.count(command) == 2, command
    assert len(rig.runtime.calls) == 2


async def test_cache_misses_after_base_change(make_repo, store):
    root, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    rig = make_preflight(runner, store, replies=[RULES_OK, RULES_OK])
    await add_rule(rig)
    await go(rig, wt)
    tree = git(wt.path, "rev-parse", "HEAD^{tree}")
    git(root, "commit", "--allow-empty", "-m", "base moved, same content")
    await go(rig, wt)
    assert git(wt.path, "rev-parse", "HEAD^{tree}") == tree  # only the base moved
    for command in (LINT, TESTS_CMD, RED_CMD):
        assert runner.calls.count(command) == 2, command
    assert runner.calls.count(BUILD) == 4  # the worktree and the red copy
    assert len(rig.runtime.calls) == 2


async def test_failed_result_not_cached(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={LINT: 1, RED_CMD: 1})
    rig = make_preflight(runner, store)
    await go(rig, wt)
    rep = await go(rig, wt)
    assert [c.name for c in rep.failures()] == ["lint"]
    assert runner.calls.count(LINT) == 2
    assert runner.calls.count(BUILD) == 2  # once in the worktree, once in the copy
    assert runner.calls.count(TESTS_CMD) == 1 and runner.calls.count(RED_CMD) == 1


async def test_rules_pass_covers_a_smaller_rule_set(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    rig = make_preflight(ScriptRunner(codes={RED_CMD: 1}), store, replies=[RULES_OK])
    await add_rule(rig)
    await go(rig, wt, lead_comments=["add a docstring"])
    rep = await go(rig, wt, lead_comments=[])
    assert rep.ok and len(rig.runtime.calls) == 1


async def test_rules_cache_misses_for_a_new_lead_comment(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    rig = make_preflight(
        ScriptRunner(codes={RED_CMD: 1}), store, replies=[RULES_OK, RULES_OK]
    )
    await add_rule(rig)
    await go(rig, wt)
    await go(rig, wt, lead_comments=["add a docstring"])
    assert len(rig.runtime.calls) == 2


async def test_red_cache_depends_on_the_no_test_reason(make_repo, store):
    _, wt = fixed_worktree(make_repo, with_test=False)
    rig = make_preflight(ScriptRunner(), store)
    first = await red_of(rig, wt, no_test_reason="docs only")
    second = await red_of(rig, wt)
    assert (first.ok, second.ok) == (True, False)


class Timed(ScriptRunner):
    """Sleeps in the slow commands and records when each one ran, and where."""

    def __init__(self, slow: set[str], **kw):
        super().__init__(**kw)
        self.slow = slow
        self.spans: list[tuple[str, Path, float, float]] = []

    def run(self, command, cwd, timeout_s):
        start = time.monotonic()
        if command in self.slow:
            time.sleep(0.3)
        result = super().run(command, cwd, timeout_s)
        self.spans.append((command, cwd, start, time.monotonic()))
        return result

    def span(self, command: str, cwd: Path) -> tuple[float, float]:
        [found] = [(s, e) for c, p, s, e in self.spans if c == command and p == cwd]
        return found


def overlap(*spans: tuple[float, float]) -> bool:
    return max(s for s, _ in spans) < min(e for _, e in spans)


async def test_checks_run_concurrently(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = Timed({LINT, BUILD, TESTS_CMD, RED_CMD}, codes={RED_CMD: 1})
    rig = make_preflight(runner, store, replies=[RULES_OK])
    await add_rule(rig)
    rules: list[tuple[float, float]] = []
    ask = rig.runtime.run

    async def slow_rules(call):
        start = time.monotonic()
        await asyncio.sleep(0.3)
        try:
            return await ask(call)
        finally:
            rules.append((start, time.monotonic()))

    rig.runtime.run = slow_rules
    rep = await go(rig, wt)
    assert rep.ok
    lint, build = runner.span(LINT, wt.path), runner.span(BUILD, wt.path)
    assert overlap(rules[0], lint, build)
    first = (rules[0], lint, build)
    assert max(e for _, e in first) - min(s for s, _ in first) < 0.6
    tests = runner.span(TESTS_CMD, wt.path)
    copy = red_copy_path(wt)
    red = (runner.span(BUILD, copy)[0], runner.span(RED_CMD, copy)[1])
    assert overlap(tests, red)  # the red check works in its copy meanwhile
    assert tests[0] >= build[1]  # tests wait for a passing build


async def test_build_failure_skips_tests_and_red(make_repo, store):
    _, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={BUILD: 1, LINT: 1})
    rep = await go(make_preflight(runner, store), wt)
    assert names(rep) == [
        ("rebase", True),
        ("size", True),
        ("rules", True),
        ("lint", False),
        ("build", False),
    ]
    assert sorted(runner.calls) == sorted([LINT, BUILD])


def tree_bytes(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(path).parts
    }


@pytest.mark.parametrize("crash", [False, True])
async def test_red_runs_in_copy_and_worktree_untouched(make_repo, store, crash):
    root, wt = fixed_worktree(make_repo)
    before = tree_bytes(wt.path)
    seen: dict[str, object] = {}

    class Spy(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            if command == RED_CMD:
                seen["cwd"] = cwd
                seen["worktree"] = (wt.path / "src/calc.py").read_text()
                seen["copy"] = (cwd / "src/calc.py").read_text()
                seen["new test"] = (cwd / "tests/test_new.py").is_file()
            return super().run(command, cwd, timeout_s)

    runner = Spy(codes={RED_CMD: 1}, raise_on=RED_CMD if crash else None)
    if crash:
        with pytest.raises(RuntimeError, match="runner crashed"):
            await go(make_preflight(runner, store), wt)
    else:
        rep = await go(make_preflight(runner, store), wt)
        assert rep.ok
    copy = seen["cwd"]
    assert isinstance(copy, Path)
    assert copy.is_relative_to(root / ".agentco" / "red")
    assert not copy.is_relative_to(wt.path)
    assert (seen["worktree"], seen["copy"], seen["new test"]) == (
        FIXED,
        ORIGINAL,
        True,
    )
    assert tree_bytes(wt.path) == before
    assert git(wt.path, "status", "--porcelain", "--untracked-files=all") == ""
    assert not copy.exists() or not any(copy.iterdir())
    # the copy was built (it has no dependencies of its own) before the revert
    assert runner.calls.count(BUILD) == 2


async def test_red_copy_build_failure_is_not_a_red_proof(make_repo, store):
    _, wt = fixed_worktree(make_repo)

    class CopyBuildFails(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            if command == BUILD and cwd != wt.path:
                return CommandResult(command, 1, "no deps", 0.0, False)
            return super().run(command, cwd, timeout_s)

    runner = CopyBuildFails(codes={RED_CMD: 1})
    rep = await go(make_preflight(runner, store), wt)
    red = {c.name: c for c in rep.checks}["red"]
    assert not red.ok
    assert red.detail.splitlines()[0] == (
        "could not build a copy to run the new tests without the fix (exit 1)"
    )
    assert RED_CMD not in runner.calls


async def test_red_copy_is_prepared_like_the_worktree(make_repo, store):
    root, wt = fixed_worktree(make_repo)
    runner = ScriptRunner(codes={RED_CMD: 1})
    await go(make_preflight(runner, store), wt)
    assert runner.prepared == [wt.path, root / ".agentco" / "red" / wt.path.name]
    assert red_copy_path(wt) == root / ".agentco" / "red" / wt.path.name


async def test_red_copy_reverts_deleted_and_executable_sources(make_repo, store):
    root = make_repo({**FILES, "src/run.sh": "#!/bin/sh\necho hi\n"})
    git(root, "update-index", "--chmod=+x", "src/run.sh")
    git(root, "commit", "-m", "exec")
    wt = create_worktree(root, 1, "main")
    add_test(wt)
    fix(wt)
    (wt.path / "src/run.sh").unlink()
    commit_all(wt, "fix")
    seen: dict[str, object] = {}

    class Spy(ScriptRunner):
        def run(self, command, cwd, timeout_s):
            if command == RED_CMD:
                script = cwd / "src/run.sh"
                seen["script"] = script.read_text() if script.exists() else None
                seen["exec"] = script.exists() and os.access(script, os.X_OK)
            return super().run(command, cwd, timeout_s)

    await go(make_preflight(Spy(codes={RED_CMD: 1}), store), wt)
    assert seen == {"script": "#!/bin/sh\necho hi\n", "exec": True}
    assert not (wt.path / "src/run.sh").exists()
