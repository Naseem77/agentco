import inspect
import json
import subprocess

import pytest

from agentco.agents.prompts import load_prompt
from agentco.agents.replies import Finding, PMReply
from agentco.agents.review import (
    ReviewReply,
    review_input,
    run_fast_review,
    run_review,
)
from agentco.llm.copilot import permission_allowed
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.sandbox.worktree import commit_all, create_worktree, head_sha

ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
    product_notes="Keep spaces working",
)
FINDING = Finding(
    id="F1",
    severity="major",
    kind="bug",
    path="a.py",
    line=3,
    title="Tab dropped",
    detail="A trailing tab is dropped.",
)
REPLY = json.dumps({"findings": [FINDING.model_dump()], "summary": "one bug"})


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class Env:
    def __init__(self, make_repo, store, loop_cfg, steps):
        self.repo = make_repo({"a.py": "x = 1\n"})
        self.wt = create_worktree(self.repo, 1, "main")
        self.runtime = FakeRuntime({Role.REVIEWER: steps})
        self.task = store.create_task("acme", "tabs", "cli")
        self.cfg = loop_cfg
        self.gate = MemoryGate("acme", InMemoryBackend())
        self.asker = Asker(self.runtime, store, self.task.id, 0.04)

    def commit(self, text: str, message: str = "agentco: change") -> str:
        (self.wt.path / "a.py").write_text(text)
        return commit_all(self.wt, message)

    async def review(self, *, fix_since=None, findings=(), fast_lane=False):
        return await run_review(
            self.asker,
            self.cfg,
            self.gate,
            self.wt,
            "main",
            ISSUE,
            fix_since=fix_since,
            findings_addressed=list(findings),
            fast_lane=fast_lane,
        )


def test_review_input_is_issue_then_diff():
    assert review_input(ISSUE, "DIFF-BODY", []) == (
        "Issue: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n- Given a tab, when parsed, then no error\n\n"
        "Product notes: Keep spaces working\n\n"
        "Diff:\nDIFF-BODY"
    )


def test_review_input_lists_findings_a_fix_addresses():
    text = review_input(ISSUE, "DIFF-BODY", [FINDING])

    assert text.endswith(
        "The diff below is a fix for these earlier findings. Review it as new "
        "code: confirm they are fixed and look for bugs the fix adds.\n"
        "- [major/bug] F1 a.py:3: Tab dropped\n  A trailing tab is dropped.\n\n"
        "Diff:\nDIFF-BODY"
    )


def test_review_input_takes_no_engineer_reply():
    assert list(inspect.signature(review_input).parameters) == [
        "issue",
        "diff_text",
        "findings_addressed",
    ]


async def test_review_sees_base_diff_with_memory_search_only(
    make_repo, store, loop_cfg
):
    env = Env(make_repo, store, loop_cfg, [REPLY])
    env.commit("x = 2\n")

    result = await env.review()

    assert result == ReviewReply(findings=[FINDING], summary="one bug")
    [call] = env.runtime.calls
    assert call.role is Role.REVIEWER and call.model == "m-reviewer"
    assert call.system.startswith(load_prompt(Role.REVIEWER))
    assert call.prompt == review_input(
        ISSUE, git(env.wt.path, "diff", "main...HEAD"), []
    )
    assert "-x = 1\n+x = 2\n" in call.prompt
    assert call.workdir == env.wt.path and call.edits is False
    assert call.commands == () and call.command_prefixes == ()
    assert [tool.name for tool in call.tools] == ["memory_search"]
    assert (call.max_turns, call.timeout_s) == (40, 1200)


async def test_fix_round_reviews_fix_diff_only(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [REPLY])
    env.commit("x = 2\n")
    first = head_sha(env.wt)
    env.commit("x = 3\n")

    await env.review(fix_since=first, findings=[FINDING])

    [call] = env.runtime.calls
    assert call.prompt == review_input(
        ISSUE, git(env.wt.path, "diff", f"{first}...HEAD"), [FINDING]
    )
    assert "-x = 2\n+x = 3\n" in call.prompt
    assert "x = 1" not in call.prompt


async def test_fast_lane_uses_light_prompt(make_repo, store, loop_cfg):
    env = Env(make_repo, store, loop_cfg, [REPLY])
    env.commit("x = 2\n")

    await env.review(fast_lane=True)

    [call] = env.runtime.calls
    assert call.system.startswith(load_prompt(Role.REVIEWER, "light"))
    assert call.workdir == env.wt.path and call.edits is False


@pytest.mark.parametrize("fast_lane", [False, True])
async def test_review_call_is_read_only_with_worktree(
    make_repo, store, loop_cfg, fast_lane
):
    env = Env(make_repo, store, loop_cfg, [REPLY])
    env.commit("x = 2\n")

    await env.review(fast_lane=fast_lane)

    [call] = env.runtime.calls
    assert call.workdir == env.wt.path
    assert call.edits is False
    assert call.commands == () and call.command_prefixes == ()
    assert permission_allowed("read", call, path="a.py")[0] is True
    assert permission_allowed("write", call, path="a.py")[0] is False


@pytest.mark.parametrize("variant", [None, "light", "fast"])
def test_reviewer_prompts_say_verify_in_the_code(variant):
    text = load_prompt(Role.REVIEWER, variant)
    assert (
        "You can read any file in the repository to confirm a finding before you "
        "report it; report only findings you verified in the code."
    ) in text


async def test_fast_review_sees_whole_diff_impact_lessons_and_prior(
    make_repo, store, loop_cfg
):
    env = Env(make_repo, store, loop_cfg, [REPLY])
    env.commit("x = 2\n")
    env.commit("x = 3\n")

    result = await run_fast_review(
        env.asker,
        env.cfg,
        env.gate,
        env.wt,
        "main",
        ISSUE,
        impact="Impact of the diff: a.py::f callers b.py::g",
        lessons=["Close temp files on error"],
        prior=[FINDING],
    )

    assert result == ReviewReply(findings=[FINDING], summary="one bug")
    [call] = env.runtime.calls
    assert call.system.startswith(load_prompt(Role.REVIEWER, "fast"))
    assert "-x = 1\n+x = 3\n" in call.prompt
    assert "a.py::f callers b.py::g" in call.prompt
    assert "- Close temp files on error" in call.prompt
    assert "F1" in call.prompt and "Tab dropped" in call.prompt
    assert call.edits is False
