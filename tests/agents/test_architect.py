import json

import pytest
import yaml
from pydantic import ValidationError

from agentco.agents.architect import (
    MAX_SUB_TASKS,
    ADRReply,
    ContractProposal,
    SubTask,
    adr_markdown,
    propose_contract,
    run_architect,
)
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import ArtifactKind

ISSUE = PMReply(
    summary="Add a plugin system",
    why="third parties want extensions",
    acceptance_criteria=["a plugin can register a command"],
)


def sub(title: str, *, criteria: list[str] | None = None) -> dict:
    return {
        "title": title,
        "acceptance_criteria": criteria or [f"{title} works"],
        "size": "s",
        "why": "needed",
    }


def adr_dict(n: int = 2) -> dict:
    return {
        "title": "Plugin registry",
        "context": "no extension point exists",
        "decision": "add a registry loaded from entry points",
        "consequences": "plugins run in-process",
        "alternatives": ["subprocess plugins"],
        "new_dependencies": [],
        "sub_tasks": [sub(f"step {i}") for i in range(n)],
    }


def adr_json(n: int = 2) -> str:
    return json.dumps(adr_dict(n))


def make(store, loop_cfg, steps):
    runtime = FakeRuntime({Role.ARCHITECT: steps})
    task = store.create_task("acme", "plugins", "cli")
    asker = Asker(runtime, store, task.id, loop_cfg.price_per_request_usd)
    return runtime, task, asker, MemoryGate("acme", InMemoryBackend())


async def test_run_architect_stores_adr_and_uses_architect_model(
    store, loop_cfg, tmp_path
):
    rt, task, asker, gate = make(store, loop_cfg, [adr_json(2)])

    adr, art = await run_architect(
        asker, loop_cfg, gate, store, task, ISSUE, repo_root=tmp_path
    )

    [call] = rt.calls
    assert call.model == loop_cfg.roles[Role.ARCHITECT].model and call.edits is False
    assert call.workdir == tmp_path
    assert art.kind is ArtifactKind.ADR and art.role is Role.ARCHITECT
    assert len(art.body["adr"]["sub_tasks"]) == 2
    assert art.body["human_note"] == ""
    assert adr.title == "Plugin registry"
    assert "memory_add" not in {t.name for t in call.tools}
    assert "memory_search" in {t.name for t in call.tools}
    assert store.latest_artifact(task.id, ArtifactKind.ADR) == art


def test_adr_reply_rejects_duplicate_titles_and_too_many_subtasks():
    data = adr_dict(2)
    data["sub_tasks"][1]["title"] = data["sub_tasks"][0]["title"].upper()
    with pytest.raises(ValidationError):
        ADRReply.model_validate(data)
    with pytest.raises(ValidationError):
        ADRReply.model_validate(adr_dict(MAX_SUB_TASKS + 1))
    assert len(ADRReply.model_validate(adr_dict(MAX_SUB_TASKS)).sub_tasks) == 8


def test_adr_reply_needs_one_subtask_with_criteria():
    with pytest.raises(ValidationError):
        ADRReply.model_validate({**adr_dict(1), "sub_tasks": []})
    with pytest.raises(ValidationError):
        SubTask.model_validate(sub("x", criteria=[]) | {"acceptance_criteria": []})
    with pytest.raises(ValidationError):
        SubTask.model_validate(sub("t" * 121))


async def test_human_note_reaches_prompt(store, loop_cfg, tmp_path):
    rt, task, asker, gate = make(store, loop_cfg, [adr_json(1), adr_json(1)])

    await run_architect(asker, loop_cfg, gate, store, task, ISSUE, repo_root=tmp_path)
    _, art = await run_architect(
        asker,
        loop_cfg,
        gate,
        store,
        task,
        ISSUE,
        repo_root=tmp_path,
        human_note="use entry points, not config",
    )

    assert "Human guidance" not in rt.calls[0].prompt
    assert "Human guidance:\nuse entry points, not config" in rt.calls[1].prompt
    assert rt.calls[1].prompt.startswith("Issue: Add a plugin system")
    assert art.body["human_note"] == "use entry points, not config"


def test_adr_markdown_lists_subtasks():
    text = adr_markdown(ADRReply.model_validate(adr_dict(2)))
    assert text.startswith("# ADR: Plugin registry")
    for heading in (
        "## Context",
        "## Decision",
        "## Consequences",
        "## Alternatives",
        "## New dependencies",
        "## Sub-tasks",
    ):
        assert heading in text
    assert "1. step 0 (s)" in text and "2. step 1 (s)" in text
    assert "step 0 works" in text


def test_architect_prompt_loads():
    assert "sub-task" in load_prompt(Role.ARCHITECT)


CONTRACT = {
    "build": "make build",
    "test": "pytest",
    "e2e": None,
    "lint": "ruff check",
    "sandbox": {"devcontainer": ".devcontainer"},
}


def contract_json(contract: dict | None = None, rationale: str = "from the Makefile"):
    return json.dumps(
        {
            "contract_yaml": yaml.safe_dump(contract or CONTRACT),
            "rationale": rationale,
        }
    )


async def test_propose_contract_is_read_only_and_stores_artifact(
    store, loop_cfg, tmp_path
):
    rt, task, asker, gate = make(store, loop_cfg, [contract_json()])

    proposal = await propose_contract(asker, loop_cfg, gate, task, tmp_path)

    [call] = rt.calls
    assert call.model == loop_cfg.roles[Role.ARCHITECT].model
    assert call.edits is False and call.workdir == tmp_path
    assert call.system.startswith(load_prompt(Role.ARCHITECT, "contract"))
    assert "memory_add" not in {t.name for t in call.tools}
    art = store.latest_artifact(task.id, ArtifactKind.CONTRACT)
    assert art.role is Role.ARCHITECT
    assert art.body == {
        "contract_yaml": proposal.contract_yaml,
        "rationale": "from the Makefile",
    }


async def test_propose_contract_passes_note_and_previous_draft(
    store, loop_cfg, tmp_path
):
    rt, task, asker, gate = make(
        store, loop_cfg, [contract_json(), contract_json({**CONTRACT, "test": "tox"})]
    )

    first = await propose_contract(asker, loop_cfg, gate, task, tmp_path)
    await propose_contract(
        asker, loop_cfg, gate, task, tmp_path, human_note="we use tox"
    )

    assert "Human guidance" not in rt.calls[0].prompt
    assert "Human guidance:\nwe use tox" in rt.calls[1].prompt
    assert first.contract_yaml in rt.calls[1].prompt


@pytest.mark.parametrize(
    "contract_yaml",
    [
        "build: [unclosed",
        "- a\n- b\n",
        yaml.safe_dump({**CONTRACT, "surprise": 1}),
        yaml.safe_dump({k: v for k, v in CONTRACT.items() if k != "e2e"}),
        yaml.safe_dump({**CONTRACT, "sandbox": {}}),
        yaml.safe_dump({**CONTRACT, "test": "make a && make b"}),
        yaml.safe_dump({**CONTRACT, "build": "make $TARGET"}),
        yaml.safe_dump({**CONTRACT, "lint": "ruff check > out"}),
        yaml.safe_dump({**CONTRACT, "setup": "pip install -e . ; true"}),
        yaml.safe_dump({**CONTRACT, "test_files": "pytest {files} | cat"}),
        yaml.safe_dump({**CONTRACT, "env_passthrough": ["GH_TOKEN"]}),
        yaml.safe_dump({**CONTRACT, "sandbox": {"devcontainer": "/etc"}}),
        yaml.safe_dump({**CONTRACT, "sandbox": {"devcontainer": "../x"}}),
        "build: x\n" * 1000,
    ],
)
def test_contract_proposal_rejects_unsafe_or_invalid_yaml(contract_yaml):
    with pytest.raises(ValidationError):
        ContractProposal(contract_yaml=contract_yaml, rationale="r")


def test_contract_proposal_accepts_a_valid_contract():
    data = {**CONTRACT, "setup": "pip install -e .", "test_files": "pytest {files}"}
    proposal = ContractProposal(contract_yaml=yaml.safe_dump(data), rationale="r")
    assert yaml.safe_load(proposal.contract_yaml)["setup"] == "pip install -e ."


async def test_invalid_contract_reply_is_retried_once(store, loop_cfg, tmp_path):
    bad = json.dumps({"contract_yaml": "build: x", "rationale": "r"})
    rt, task, asker, gate = make(store, loop_cfg, [bad, contract_json()])

    await propose_contract(asker, loop_cfg, gate, task, tmp_path)

    assert len(rt.calls) == 2
    assert len(store.artifacts(task.id, ArtifactKind.CONTRACT)) == 1


def test_architect_contract_prompt_loads():
    text = load_prompt(Role.ARCHITECT, "contract")
    assert ".agentco.yml" in text and "JSON" in text
