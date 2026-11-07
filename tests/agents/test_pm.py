import json
from datetime import UTC, datetime

import pytest

from agentco.agents.pm import run_pm
from agentco.agents.replies import PMReply
from agentco.config import ConfigError
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker, ReplyInvalid
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import ArtifactKind

PM_JSON = json.dumps(
    {
        "summary": "Parser must accept tabs",
        "why": "Users paste tab-separated data",
        "acceptance_criteria": ["Given a tab, when parsed, then no error"],
        "product_notes": "Spaces already work",
    }
)


def make(store, loop_cfg, steps):
    runtime = FakeRuntime({Role.PM: steps})
    task = store.create_task("acme", "tabs", "cli")
    backend = InMemoryBackend()
    asker = Asker(runtime, store, task.id, loop_cfg.price_per_request_usd)
    return runtime, task, backend, asker, MemoryGate("acme", backend)


async def test_pm_writes_product_episode_and_issue_artifact(store, loop_cfg):
    runtime, task, backend, asker, gate = make(store, loop_cfg, [PM_JSON])

    issue = await run_pm(asker, loop_cfg, gate, store, task, "parser chokes on tabs")

    assert issue == PMReply(
        summary="Parser must accept tabs",
        why="Users paste tab-separated data",
        acceptance_criteria=["Given a tab, when parsed, then no error"],
        product_notes="Spaces already work",
    )
    [item] = backend.items("acme__product")
    assert item.name == "task-1-issue"
    assert item.body == (
        "Issue for task 1: Parser must accept tabs\n"
        "Why: Users paste tab-separated data\n"
        "Acceptance criteria:\n"
        "- Given a tab, when parsed, then no error\n"
        "Product notes: Spaces already work"
    )
    assert item.source_description == "agent:pm"
    [artifact] = store.artifacts(task.id, ArtifactKind.ISSUE)
    assert (artifact.role, artifact.body) == (Role.PM, issue.model_dump())
    [call] = runtime.calls
    assert [t.name for t in call.tools] == ["memory_search", "memory_add"]
    assert call.model == "m-pm"
    assert (call.max_turns, call.timeout_s) == (40, 1200)
    assert call.prompt == "Task 1: tabs\n\nRequest:\nparser chokes on tabs"
    assert call.edits is False and call.workdir is None


async def test_pm_search_tool_reads_product_and_company_only(store, loop_cfg):
    runtime, task, backend, asker, gate = make(store, loop_cfg, [PM_JSON])
    for scope, body in (
        (Scope.PRODUCT, "tabs are whitespace"),
        (Scope.COMPANY, "tabs policy"),
        (Scope.RND, "tabs in the lexer"),
    ):
        await backend.add(
            f"acme__{scope.value}",
            scope,
            MemoryItem("n", body, "t", datetime.now(UTC)),
        )

    await run_pm(asker, loop_cfg, gate, store, task, "tabs")

    search = runtime.calls[0].tools[0]
    out = await search.handler(search.params(query="whitespace policy lexer"))
    assert out == "- [acme__company] tabs policy\n- [acme__product] tabs are whitespace"


async def test_pm_invalid_reply_writes_nothing(store, loop_cfg):
    bad = json.dumps({"summary": "s", "why": "w", "acceptance_criteria": []})
    _, task, backend, asker, gate = make(store, loop_cfg, [bad, bad])

    with pytest.raises(ReplyInvalid):
        await run_pm(asker, loop_cfg, gate, store, task, "x")

    assert backend.items("acme__product") == []
    assert store.artifacts(task.id, ArtifactKind.ISSUE) == []


async def test_pm_missing_role_model_raises_config_error(store, loop_cfg):
    runtime, task, _, asker, gate = make(store, loop_cfg, [PM_JSON])
    del loop_cfg.roles[Role.PM]

    with pytest.raises(ConfigError) as exc:
        await run_pm(asker, loop_cfg, gate, store, task, "x")

    assert str(exc.value) == "no model for pm"
    assert runtime.calls == []
