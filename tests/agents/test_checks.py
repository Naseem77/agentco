import asyncio
import inspect
import json
import subprocess
from datetime import UTC, datetime

from agentco.agents.checks import CheckOutcome, run_checks
from agentco.agents.engineer import EngineerReply
from agentco.agents.qa import QAReply, qa_input, run_qa
from agentco.agents.replies import PMReply
from agentco.agents.review import ReviewReply, review_input, run_review
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.repo_contract import RepoContract, Sandbox
from agentco.roles import Role
from agentco.sandbox.runner import CommandResult
from agentco.sandbox.worktree import commit_all, create_worktree
from agentco.store.models import ArtifactKind

SENTINEL = "ENGINEER-PRIVATE-REASONING-42"
ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
)
REVIEW_REPLY = json.dumps({"findings": [], "summary": "clean"})
QA_REPLY = json.dumps(
    {"passed": True, "bug_class": "tabs", "tests_run": [], "neighbour_matrix": []}
)


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class OkRunner:
    def run(self, command, cwd, timeout_s):
        return CommandResult(command, 0, "", 0.0, False)


def test_input_builders_take_no_engineer_reply():
    for builder in (review_input, qa_input):
        assert "engineer" not in " ".join(inspect.signature(builder).parameters)
    for runner in (run_review, run_qa):
        parameters = inspect.signature(runner).parameters
        assert "engineer" not in " ".join(parameters)
        assert EngineerReply not in [p.annotation for p in parameters.values()]


async def test_isolation_no_engineer_text_reaches_review_or_qa(
    make_repo, store, loop_cfg
):
    repo = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(repo, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    commit_all(wt, f"agentco: fix {SENTINEL}")
    task = store.create_task("acme", "tabs", "cli")
    engineer = EngineerReply(
        summary=f"I did it because {SENTINEL}", files_touched=["a.py"], tests_added=[]
    )
    store.put_artifact(
        task.id, ArtifactKind.ENGINEER, Role.JUNIOR, engineer.model_dump()
    )
    backend = InMemoryBackend()
    gate = MemoryGate("acme", backend)
    now = datetime.now(UTC)
    await backend.add(
        graph_name("acme", Scope.RND),
        Scope.RND,
        MemoryItem("n", "RND-ARCHITECT-NOTE parser", "t", now),
    )
    seen = {}

    def inspect_qa(call):
        seen["commits"] = git(call.workdir, "rev-list", "--count", "HEAD").strip()
        seen["log"] = git(call.workdir, "log", "--all", "--format=%B")
        return QA_REPLY

    runtime = FakeRuntime({Role.REVIEWER: [REVIEW_REPLY], Role.QA: [inspect_qa]})
    asker = Asker(runtime, store, task.id, 0.04)

    outcome = await run_checks(
        run_review(
            asker, loop_cfg, gate, wt, "main", ISSUE,
            fix_since=None, findings_addressed=[], fast_lane=False,
        ),
        run_qa(
            asker, loop_cfg, gate, wt, "main",
            RepoContract(
                build="b", test="t", e2e=None, lint="l",
                sandbox=Sandbox(devcontainer=".devcontainer"),
            ),
            OkRunner(), 1, ISSUE, fast_lane=False,
        ),
    )  # fmt: skip

    assert outcome.errors == ()
    assert {call.role for call in runtime.calls} == {Role.REVIEWER, Role.QA}
    for call in runtime.calls:
        assert SENTINEL not in call.system + call.prompt
        assert [tool.name for tool in call.tools].count("memory_add") == (
            1 if call.role is Role.QA else 0
        )
    review_call = next(c for c in runtime.calls if c.role is Role.REVIEWER)
    assert review_call.workdir is None
    assert "-x = 1\n+x = 2\n" in review_call.prompt
    assert git(wt.path, "log", "-1", "--format=%s").strip() == (
        f"agentco: fix {SENTINEL}"
    )
    assert seen["commits"] == "1"
    assert SENTINEL not in seen["log"]
    qa_call = next(c for c in runtime.calls if c.role is Role.QA)
    search = next(t for t in qa_call.tools if t.name == "memory_search")
    denied = await search.handler(search.params(query="parser", scopes=["rnd"]))
    assert denied.startswith("denied")
    assert await search.handler(search.params(query="parser")) == "no facts"


async def test_isolation_fast_lane_reviewer_also_blind(make_repo, store, loop_cfg):
    repo = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(repo, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    commit_all(wt, f"agentco: fix {SENTINEL}")
    task = store.create_task("acme", "tabs", "cli")
    runtime = FakeRuntime({Role.REVIEWER: [REVIEW_REPLY]})
    asker = Asker(runtime, store, task.id, 0.04)

    await run_review(
        asker, loop_cfg, MemoryGate("acme", InMemoryBackend()), wt, "main", ISSUE,
        fix_since=None, findings_addressed=[], fast_lane=True,
    )  # fmt: skip

    [call] = runtime.calls
    assert SENTINEL not in call.system + call.prompt
    assert call.workdir is None


async def test_run_checks_returns_both_results():
    async def review():
        return ReviewReply(findings=[], summary="clean")

    async def qa():
        return QAReply(passed=True, bug_class="b", tests_run=[], neighbour_matrix=[])

    outcome = await run_checks(review(), qa())

    assert outcome == CheckOutcome(
        review=ReviewReply(findings=[], summary="clean"),
        qa=QAReply(passed=True, bug_class="b", tests_run=[], neighbour_matrix=[]),
        errors=(),
    )


async def test_one_check_crashing_does_not_cancel_other():
    finished = []

    async def review():
        raise RuntimeError("reviewer exploded")

    async def qa():
        await asyncio.sleep(0.05)
        finished.append("qa")
        return QAReply(passed=True, bug_class="b", tests_run=[], neighbour_matrix=[])

    outcome = await run_checks(review(), qa())

    assert finished == ["qa"]
    assert outcome.review is None
    assert outcome.qa == QAReply(
        passed=True, bug_class="b", tests_run=[], neighbour_matrix=[]
    )
    assert outcome.errors == ("review: RuntimeError: reviewer exploded",)


async def test_qa_crashing_does_not_cancel_review():
    finished = []

    async def review():
        await asyncio.sleep(0.05)
        finished.append("review")
        return ReviewReply(findings=[], summary="clean")

    async def qa():
        raise ValueError("qa exploded")

    outcome = await run_checks(review(), qa())

    assert finished == ["review"]
    assert outcome.review == ReviewReply(findings=[], summary="clean")
    assert outcome.qa is None
    assert outcome.errors == ("qa: ValueError: qa exploded",)


async def test_both_crashing_reports_both_errors():
    async def review():
        raise RuntimeError("r")

    async def qa():
        raise RuntimeError("q")

    outcome = await run_checks(review(), qa())

    assert outcome == CheckOutcome(
        review=None, qa=None, errors=("review: RuntimeError: r", "qa: RuntimeError: q")
    )
