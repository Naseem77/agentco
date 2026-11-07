import json
import subprocess

import pytest

from agentco.agents.engineer import (
    EngineerBrief,
    EngineerFailed,
    EngineerReply,
    FollowUp,
    run_engineer,
)
from agentco.agents.replies import Finding, PMReply
from agentco.config import ConfigError
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.llm.runtime import AgentResult
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import RepoContract, Sandbox
from agentco.roles import Role
from agentco.sandbox.worktree import create_worktree
from agentco.store.models import ArtifactKind

ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
)


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def reply(**overrides) -> str:
    body = {"summary": "s", "files_touched": ["a.py"], "tests_added": []}
    return json.dumps({**body, **overrides})


def contract(**overrides) -> RepoContract:
    fields = {
        "build": "uv build",
        "test": "uv run pytest",
        "e2e": None,
        "lint": "uv run ruff check",
        "sandbox": Sandbox(devcontainer=".devcontainer"),
        **overrides,
    }
    return RepoContract(**fields)


def edit_a(call):
    (call.workdir / "a.py").write_text("x = 2\n")
    return reply()


class Env:
    def __init__(self, make_repo, store, loop_cfg, steps, role=Role.JUNIOR):
        self.role = role
        self.repo = make_repo({"a.py": "x = 1\n"})
        self.wt = create_worktree(self.repo, 1, "main")
        self.runtime = FakeRuntime({role: steps})
        self.task = store.create_task("acme", "tabs", "cli")
        self.store = store
        self.cfg = loop_cfg
        self.gate = MemoryGate("acme", InMemoryBackend())
        self.asker = Asker(self.runtime, store, self.task.id, 0.04)

    async def run(self, brief=None, repo_contract=None):
        return await run_engineer(
            self.asker,
            self.cfg,
            self.gate,
            self.store,
            self.task,
            self.role,
            self.wt,
            repo_contract or contract(),
            brief or EngineerBrief(issue=ISSUE),
        )


async def test_engineer_edits_commits_and_records(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])

    result = await env.run()

    assert result == EngineerReply(summary="s", files_touched=["a.py"], tests_added=[])
    assert git(env.wt.path, "log", "-1", "--format=%s").strip() == (
        "agentco: task 1 round 0"
    )
    assert git(env.wt.path, "status", "--porcelain") == ""
    [call] = env.runtime.calls
    assert call.edits is True and call.workdir == env.wt.path
    assert call.role is Role.JUNIOR and call.model == "m-junior"
    assert call.commands == ("uv build", "uv run pytest", "uv run ruff check")
    assert call.command_prefixes == ()
    assert [t.name for t in call.tools] == ["memory_search"]
    assert (call.max_turns, call.timeout_s) == (40, 1200)
    [artifact] = store.artifacts(env.task.id, ArtifactKind.ENGINEER)
    assert (artifact.role, artifact.body) == (Role.JUNIOR, result.model_dump())


async def test_senior_uses_senior_role_and_model(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a], role=Role.SENIOR)

    await env.run()

    [call] = env.runtime.calls
    assert call.role is Role.SENIOR and call.model == "m-senior"
    [artifact] = store.artifacts(env.task.id, ArtifactKind.ENGINEER)
    assert artifact.role is Role.SENIOR


async def test_commands_and_prefixes_come_from_contract(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])
    repo_contract = contract(e2e="uv run e2e", test_files="uv run pytest {files} -q")

    await env.run(repo_contract=repo_contract)

    call = env.runtime.calls[0]
    assert call.commands == (
        "uv build",
        "uv run pytest",
        "uv run ruff check",
        "uv run e2e",
    )
    assert call.command_prefixes == ("uv run pytest ",)


async def test_round_is_in_commit_message(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])

    await env.run(EngineerBrief(issue=ISSUE, round=2))

    assert git(env.wt.path, "log", "-1", "--format=%s").strip() == (
        "agentco: task 1 round 2"
    )


async def test_commit_message_carries_no_engineer_prose(make_repo, store, loop_cfg):
    def work(call):
        (call.workdir / "a.py").write_text("x = 2\n")
        return reply(summary="SENTINEL-PROSE")

    env = Env(make_repo, store, loop_cfg, [work])

    await env.run()

    assert git(env.wt.path, "log", "-1", "--format=%B").strip() == (
        "agentco: task 1 round 0"
    )


async def test_no_change_fails(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [reply()])

    with pytest.raises(EngineerFailed) as exc:
        await env.run()

    assert exc.value.stuck is False
    assert exc.value.reason == "engineer made no changes"
    assert store.artifacts(env.task.id, ArtifactKind.ENGINEER) == []


async def test_stuck_raises_engineer_failed_stuck(make_repo, store, loop_cfg):
    stuck = AgentResult("", 1.0, stuck=True, stuck_reason="cannot find the parser")
    env = Env(make_repo, store, loop_cfg, [stuck])

    with pytest.raises(EngineerFailed) as exc:
        await env.run()

    assert exc.value.stuck is True
    assert exc.value.reason == "cannot find the parser"
    assert store.artifacts(env.task.id, ArtifactKind.ENGINEER) == []


async def test_invalid_reply_raises_engineer_failed(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, ["not json", "still not json"])

    with pytest.raises(EngineerFailed) as exc:
        await env.run()

    assert exc.value.stuck is False
    assert exc.value.reason.startswith("junior reply is not a valid EngineerReply")
    assert store.artifacts(env.task.id, ArtifactKind.ENGINEER) == []


async def test_missing_invariant_fails_when_required(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])

    with pytest.raises(EngineerFailed) as exc:
        await env.run(EngineerBrief(issue=ISSUE, require_invariant=True))

    assert exc.value.stuck is False
    assert exc.value.reason == "no invariant stated"
    assert store.artifacts(env.task.id, ArtifactKind.ENGINEER) == []


async def test_blank_invariant_fails_when_required(make_repo, store, loop_cfg):
    def work(call):
        edit_a(call)
        return reply(invariant="  \n")

    env = Env(make_repo, store, loop_cfg, [work])

    with pytest.raises(EngineerFailed) as exc:
        await env.run(EngineerBrief(issue=ISSUE, require_invariant=True))

    assert exc.value.reason == "no invariant stated"


async def test_invariant_accepted_and_requested_when_required(
    make_repo, store, loop_cfg
):
    def work(call):
        edit_a(call)
        return reply(invariant="Input is normalised once at the boundary")

    env = Env(make_repo, store, loop_cfg, [work])

    result = await env.run(EngineerBrief(issue=ISSUE, require_invariant=True))

    assert result.invariant == "Input is normalised once at the boundary"
    assert "invariant" in env.runtime.calls[0].prompt.lower()


async def test_invariant_not_requested_by_default(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])

    await env.run()

    assert "invariant" not in env.runtime.calls[0].prompt.lower()


async def test_prompt_contains_only_scoped_findings(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])
    findings = (
        Finding(
            id="f1",
            severity="major",
            kind="bug",
            path="a.py",
            line=3,
            title="Tab crashes the lexer",
            detail="Lexer raises on \\t",
        ),
        Finding(
            id="f2",
            severity="blocker",
            kind="edge_case",
            title="Empty input",
            detail="d2",
        ),
    )
    brief = EngineerBrief(
        issue=ISSUE,
        findings_to_fix=findings,
        preflight_failures=("tests/test_a.py::test_tab FAILED",),
        round=1,
        human_note="Prefer the small fix",
    )

    await env.run(brief)

    assert env.runtime.calls[0].prompt == (
        "Task 1: tabs\n\n"
        "Summary: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n"
        "- Given a tab, when parsed, then no error\n\n"
        "Fix exactly these findings and nothing else; keep the fix small.\n"
        "- [major/bug] f1 a.py:3: Tab crashes the lexer\n"
        "  Lexer raises on \\t\n"
        "- [blocker/edge_case] f2: Empty input\n"
        "  d2\n\n"
        "Pre-flight failures to make pass:\n"
        "- tests/test_a.py::test_tab FAILED\n\n"
        "Note from the human: Prefer the small fix"
    )


async def test_first_round_prompt_has_no_fix_sections(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])

    await env.run()

    assert env.runtime.calls[0].prompt == (
        "Task 1: tabs\n\n"
        "Summary: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n"
        "- Given a tab, when parsed, then no error"
    )


async def test_follow_ups_become_tasks(make_repo, store, loop_cfg):
    def work(call):
        edit_a(call)
        return reply(
            follow_ups=[
                {"title": "Clean up lexer", "why": "dead code"},
                {"title": "Docs for tabs", "why": "undocumented"},
            ]
        )

    env = Env(make_repo, store, loop_cfg, [work])

    result = await env.run()

    assert result.follow_ups == [
        FollowUp(title="Clean up lexer", why="dead code"),
        FollowUp(title="Docs for tabs", why="undocumented"),
    ]
    children = [store.get(i) for i in (2, 3)]
    assert [(t.tenant, t.title, t.source, t.source_ref) for t in children] == [
        ("acme", "Clean up lexer", "followup", "task:1"),
        ("acme", "Docs for tabs", "followup", "task:1"),
    ]


async def test_no_follow_up_tasks_when_engineer_fails(make_repo, store, loop_cfg):
    def work(call):
        return reply(follow_ups=[{"title": "x", "why": "y"}])

    env = Env(make_repo, store, loop_cfg, [work])

    with pytest.raises(EngineerFailed):
        await env.run()

    with pytest.raises(KeyError):
        store.get(2)


async def test_missing_role_model_raises_config_error(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [edit_a])
    del loop_cfg.roles[Role.JUNIOR]

    with pytest.raises(ConfigError) as exc:
        await env.run()

    assert str(exc.value) == "no model for junior"
    assert env.runtime.calls == []
