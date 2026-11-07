import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentco.agents.prompts import load_prompt
from agentco.agents.qa import (
    FAST_LANE_BUG_CLASS,
    NeighbourRow,
    QAFailure,
    QAReply,
    qa_input,
    run_qa,
)
from agentco.agents.replies import PMReply
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker, ReplyInvalid
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.repo_contract import RepoContract, Sandbox
from agentco.roles import Role
from agentco.sandbox.runner import CommandResult, ContainerError
from agentco.sandbox.worktree import commit_all, create_worktree

ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
)
PASSING = {
    "passed": True,
    "bug_class": "tab handling",
    "tests_run": ["pytest"],
    "neighbour_matrix": [{"case": "empty", "result": "pass", "evidence": "ok"}],
}


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def contract(**overrides) -> RepoContract:
    fields = {
        "build": "make build",
        "test": "make test",
        "e2e": None,
        "lint": "make lint",
        "sandbox": Sandbox(devcontainer=".devcontainer"),
        **overrides,
    }
    return RepoContract(**fields)


class FakeRunner:
    def __init__(self, exit_codes: dict[str, int] | None = None):
        self.exit_codes = exit_codes or {}
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        assert (cwd / ".git").is_dir()  # runs only inside the snapshot repo
        self.calls.append((command, cwd))
        code = self.exit_codes.get(command, 0)
        return CommandResult(command, code, f"out of {command}", 0.1, False)


class PreparingRunner(FakeRunner):
    def __init__(self, error: ContainerError | None = None):
        super().__init__()
        self.error = error
        self.prepared: list[Path] = []

    def prepare(self, cwd: Path) -> None:
        self.prepared.append(cwd)
        if self.error:
            raise self.error


class Env:
    def __init__(self, make_repo, store, loop_cfg, steps=()):
        self.repo = make_repo(
            {
                "a.py": "def f():\n    return 1\n",
                "tests/test_a.py": (
                    "from a import f\n\n\ndef test_f():\n    assert f() == 1\n"
                ),
            }
        )
        self.wt = create_worktree(self.repo, 1, "main")
        (self.wt.path / "a.py").write_text("def f():\n    return 2\n")
        commit_all(self.wt, "agentco: change")
        self.runtime = FakeRuntime({Role.QA: list(steps)})
        self.task = store.create_task("acme", "tabs", "cli")
        self.cfg = loop_cfg
        self.backend = InMemoryBackend()
        self.gate = MemoryGate("acme", self.backend)
        self.asker = Asker(self.runtime, store, self.task.id, 0.04)

    async def qa(self, runner, repo_contract=None, *, round_=1, fast_lane=False):
        return await run_qa(
            self.asker,
            self.cfg,
            self.gate,
            self.wt,
            "main",
            repo_contract or contract(),
            runner,
            round_,
            ISSUE,
            fast_lane=fast_lane,
        )


def test_qa_input_lists_neighbour_cases():
    assert qa_input(ISSUE, ["empty input", "two tabs"]) == (
        "Issue: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n- Given a tab, when parsed, then no error\n\n"
        "Known neighbour cases from memory:\n- empty input\n- two tabs"
    )


def test_qa_input_without_neighbours_is_just_the_issue():
    assert qa_input(ISSUE, []) == (
        "Issue: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n- Given a tab, when parsed, then no error"
    )


async def test_qa_runs_build_and_tests_in_snapshot_then_asks_llm(
    make_repo, store, loop_cfg
):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    runner = FakeRunner()

    result = await env.qa(runner, contract(e2e="make e2e"))

    snapshot = env.wt.path.parent / "1-qa-r1"
    assert runner.calls == [("make build", snapshot), ("make test", snapshot)]
    assert result == QAReply(
        passed=True,
        bug_class="tab handling",
        tests_run=["pytest"],
        neighbour_matrix=[NeighbourRow(case="empty", result="pass", evidence="ok")],
    )
    [call] = env.runtime.calls
    assert call.role is Role.QA and call.model == "m-qa"
    assert call.system.startswith(load_prompt(Role.QA))
    assert call.prompt == qa_input(ISSUE, [])
    assert call.workdir == snapshot and call.edits is True
    assert call.commands == ("make build", "make test", "make e2e")
    assert call.command_prefixes == ()
    assert [tool.name for tool in call.tools] == ["memory_search", "memory_add"]
    assert (call.max_turns, call.timeout_s) == (40, 1200)


async def test_affected_tests_command_runs_in_snapshot(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    runner = FakeRunner()

    await env.qa(runner, contract(test_files="make test FILES={files}"))

    snapshot = env.wt.path.parent / "1-qa-r1"
    assert runner.calls == [
        ("make build", snapshot),
        ("make test FILES=tests/test_a.py", snapshot),
    ]
    assert env.runtime.calls[0].command_prefixes == ("make test FILES=",)


async def test_qa_build_failure_overrides_passed(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    runner = FakeRunner({"make build": 2})

    result = await env.qa(runner)

    assert [command for command, _ in runner.calls] == ["make build"]
    assert result.passed is False
    assert result.failures == [
        QAFailure(
            title="build failed",
            steps="make build",
            expected="exit code 0",
            actual="exit code 2\nout of make build",
        )
    ]
    assert len(env.runtime.calls) == 1


async def test_qa_test_failure_overrides_passed_and_keeps_llm_failures(
    make_repo, store, loop_cfg
):
    llm_failure = {"title": "t", "steps": "s", "expected": "e", "actual": "a"}
    reply = {**PASSING, "failures": [llm_failure]}
    env = Env(make_repo, store, loop_cfg, [json.dumps(reply)])

    result = await env.qa(FakeRunner({"make test": 1}))

    assert result.passed is False
    assert result.failures == [
        QAFailure(
            title="tests failed",
            steps="make test",
            expected="exit code 0",
            actual="exit code 1\nout of make test",
        ),
        QAFailure(title="t", steps="s", expected="e", actual="a"),
    ]


async def test_fast_lane_qa_is_deterministic_only(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg)
    runner = FakeRunner()

    result = await env.qa(runner, fast_lane=True)

    assert env.runtime.calls == []
    assert [command for command, _ in runner.calls] == ["make build", "make test"]
    assert result == QAReply(
        passed=True,
        bug_class=FAST_LANE_BUG_CLASS,
        tests_run=["make build", "make test"],
        neighbour_matrix=[],
    )


async def test_fast_lane_qa_fails_on_failing_tests(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg)

    result = await env.qa(FakeRunner({"make test": 1}), fast_lane=True)

    assert env.runtime.calls == []
    assert result.passed is False
    assert result.tests_run == ["make build", "make test"]
    assert [failure.title for failure in result.failures] == ["tests failed"]


async def test_snapshot_is_one_commit_and_removed_after_qa(make_repo, store, loop_cfg):
    seen = {}

    def inspect_snapshot(call):
        seen["exists"] = call.workdir.is_dir()
        seen["commits"] = git(call.workdir, "rev-list", "--count", "HEAD").strip()
        seen["a"] = (call.workdir / "a.py").read_text()
        return json.dumps(PASSING)

    env = Env(make_repo, store, loop_cfg, [inspect_snapshot])

    await env.qa(FakeRunner(), round_=3)

    assert seen == {"exists": True, "commits": "1", "a": "def f():\n    return 2\n"}
    assert not (env.wt.path.parent / "1-qa-r3").exists()
    assert git(env.wt.path, "log", "-1", "--format=%s").strip() == "agentco: change"


async def test_snapshot_removed_when_llm_reply_is_invalid(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, ["not json", "still not json"])

    with pytest.raises(ReplyInvalid):
        await env.qa(FakeRunner())

    assert not (env.wt.path.parent / "1-qa-r1").exists()


async def test_stale_snapshot_from_a_crashed_run_is_replaced(
    make_repo, store, loop_cfg
):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    stale = env.wt.path.parent / "1-qa-r1"
    stale.mkdir()
    (stale / "leftover.txt").write_text("old")

    await env.qa(FakeRunner())

    assert not stale.exists()


async def test_neighbour_cases_come_from_qa_memory_only(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    now = datetime.now(UTC)
    for scope, body in [
        (Scope.QA, "tabs neighbour: two tabs in a row"),
        (Scope.PRODUCT, "tabs product fact"),
        (Scope.RND, "tabs rnd architect note"),
    ]:
        await env.backend.add(
            graph_name("acme", scope), scope, MemoryItem("n", body, "t", now)
        )

    await env.qa(FakeRunner())

    assert env.runtime.calls[0].prompt == qa_input(
        ISSUE, ["tabs neighbour: two tabs in a row"]
    )


async def test_container_prepare_runs_on_the_snapshot(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    runner = PreparingRunner()

    await env.qa(runner)

    assert runner.prepared == [env.wt.path.parent / "1-qa-r1"]


async def test_container_prepare_failure_fails_qa_without_running_commands(
    make_repo, store, loop_cfg
):
    env = Env(make_repo, store, loop_cfg, [json.dumps(PASSING)])
    runner = PreparingRunner(ContainerError("devcontainer up failed"))

    result = await env.qa(runner)

    assert runner.calls == []
    assert result.passed is False
    assert result.failures == [
        QAFailure(
            title="sandbox setup failed",
            steps="prepare",
            expected="exit code 0",
            actual="devcontainer up failed",
        )
    ]


def qa_reply(*paths: str, **extra) -> str:
    return json.dumps({**PASSING, "new_tests": list(paths), **extra})


async def test_run_qa_captures_new_tests_before_snapshot_removed(
    make_repo, store, loop_cfg
):
    text = "def test_neighbour():\n    assert True\n"

    def write_tests(call):
        (call.workdir / "tests/test_neighbour.py").write_text(text)
        return qa_reply("tests/test_neighbour.py")

    env = Env(make_repo, store, loop_cfg, [write_tests])

    qa = await env.qa(FakeRunner())

    assert qa.new_tests == ["tests/test_neighbour.py"]
    assert qa.new_test_sources == {"tests/test_neighbour.py": text}
    assert not (env.wt.path.parent / "1-qa-r1").exists()
    assert not (env.wt.path / "tests/test_neighbour.py").exists()


async def test_new_test_sources_skip_unsafe_paths(make_repo, store, loop_cfg, tmp_path):
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n")
    good = "def test_ok():\n    pass\n"

    def write_tests(call):
        root = call.workdir
        (root / "tests/test_ok.py").write_text(good)
        (root / "tests/test_link.py").symlink_to(outside)
        (root / "tests/linked").symlink_to(tmp_path)
        (root / "helper.py").write_text("x = 1\n")  # not a test path
        (tmp_path / "test_escape.py").write_text("def test_x():\n    pass\n")
        return qa_reply(
            "../test_escape.py",
            "tests/../../test_escape.py",
            str(tmp_path / "test_escape.py"),
            "tests/test_link.py",
            "tests/linked/secret.py",
            "helper.py",
            "tests/test_missing.py",
            "tests/test_ok.py",
        )

    env = Env(make_repo, store, loop_cfg, [write_tests])

    qa = await env.qa(FakeRunner())

    assert qa.new_test_sources == {"tests/test_ok.py": good}


async def test_new_test_sources_are_capped_in_count_and_size(
    make_repo, store, loop_cfg
):
    names = [f"tests/test_n{i}.py" for i in range(12)]

    def write_tests(call):
        for name in names:
            (call.workdir / name).write_text(f"def test_{name[-4:-3]}():\n    pass\n")
        (call.workdir / "tests/test_big.py").write_text("x" * 20_001)
        (call.workdir / "tests/test_edge.py").write_text("y" * 20_000)
        return qa_reply("tests/test_big.py", "tests/test_edge.py", *names)

    env = Env(make_repo, store, loop_cfg, [write_tests])

    qa = await env.qa(FakeRunner())

    assert list(qa.new_test_sources) == ["tests/test_edge.py", *names[:9]]
    assert qa.new_test_sources["tests/test_edge.py"] == "y" * 20_000


async def test_the_model_cannot_supply_new_test_sources(make_repo, store, loop_cfg):
    forged = {"tests/test_evil.py": "import os\n"}
    env = Env(
        make_repo,
        store,
        loop_cfg,
        [qa_reply(new_test_sources=forged)],
    )

    qa = await env.qa(FakeRunner())

    assert qa.new_test_sources == {}


def test_new_test_sources_is_hidden_from_the_model_schema():
    assert "new_test_sources" not in QAReply.model_json_schema()["properties"]


async def test_fast_lane_qa_has_no_new_test_sources(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg)

    qa = await env.qa(FakeRunner(), fast_lane=True)

    assert qa.new_test_sources == {}
