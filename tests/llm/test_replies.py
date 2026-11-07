import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import AgentStuck, Asker, Reply, ReplyInvalid, parse_reply
from agentco.llm.runtime import AgentCall, AgentResult
from agentco.roles import Role


class Ok(Reply):
    answer: int


async def test_ask_parses_fenced_json_and_records_cost(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime(
        {Role.PM: ['thinking...\n```json\n{"answer": 4}\n```']}, premium_requests=2
    )
    got = await Asker(rt, store, t.id, 0.04).ask(
        AgentCall(Role.PM, "m-pm", "sys", "2+2"), Ok
    )
    assert got.answer == 4
    assert store.get(t.id).cost_usd == pytest.approx(0.08)
    assert rt.calls[0].system == (
        "sys\n\nReply with only one JSON object matching this JSON schema:\n"
        + json.dumps(Ok.model_json_schema())
    )
    assert rt.calls[0].prompt == "2+2"


async def test_cost_detail_is_recorded(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime({Role.PM: ['{"answer": 1}']}, premium_requests=2)
    await Asker(rt, store, t.id, 0.04).ask(AgentCall(Role.PM, "m-pm", "s", "p"), Ok)
    [entry] = [e for e in store.audit(t.id) if e.action == "cost"]
    assert entry.actor == Role.PM
    assert entry.detail == {"model": "m-pm", "requests": 2, "reply": "Ok"}


async def test_invalid_reply_retried_once_then_fails(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime({Role.PM: ["nope", '{"answer": "x"}']})
    with pytest.raises(ReplyInvalid):
        await Asker(rt, store, t.id, 0.04).ask(AgentCall(Role.PM, "m", "s", "p"), Ok)
    assert len(rt.calls) == 2
    assert rt.calls[0].prompt == "p"
    assert rt.calls[1].prompt.startswith("p\n\nYour previous reply was invalid: ")
    assert rt.calls[1].prompt.endswith(". Reply again with only the JSON object.")
    assert store.get(t.id).cost_usd == pytest.approx(0.08)


async def test_invalid_then_valid_succeeds(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime({Role.PM: ["nope", '{"answer": 7}']})
    got = await Asker(rt, store, t.id, 0.04).ask(AgentCall(Role.PM, "m", "s", "p"), Ok)
    assert got.answer == 7
    assert store.get(t.id).cost_usd == pytest.approx(0.08)


async def test_stuck_raises_after_billing(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime(
        {Role.JUNIOR: [AgentResult("", 3, stuck=True, stuck_reason="max turns")]}
    )
    with pytest.raises(AgentStuck) as exc:
        await Asker(rt, store, t.id, 0.04).ask(
            AgentCall(Role.JUNIOR, "m", "s", "p"), Ok
        )
    assert exc.value.reason == "max turns"
    assert len(rt.calls) == 1
    assert store.get(t.id).cost_usd == pytest.approx(0.12)


def test_parse_reply_takes_last_balanced_object():
    text = 'draft {"answer": 1} final {"answer": 2, "note": "}{"}'
    assert parse_reply(text, Ok).answer == 2


def test_parse_reply_ignores_extra_keys_and_nested_braces():
    assert parse_reply('{"answer": 3, "x": {"y": {"z": 1}}}', Ok).answer == 3


def test_parse_reply_bare_fence_without_language():
    assert parse_reply('```\n{"answer": 5}\n```', Ok).answer == 5


def test_parse_reply_without_object_raises_value_error():
    with pytest.raises(ValueError):
        parse_reply("no json here", Ok)


def test_parse_reply_unbalanced_raises_value_error():
    with pytest.raises(ValueError):
        parse_reply('{"answer": 1', Ok)


def test_parse_reply_wrong_type_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_reply('{"answer": "x"}', Ok)


async def test_fake_runtime_consumes_per_role_in_order():
    rt = FakeRuntime({Role.PM: ["a", "b"], Role.QA: ["c"]}, premium_requests=1.5)
    results = [
        await rt.run(AgentCall(Role.PM, "m", "s", "p")),
        await rt.run(AgentCall(Role.QA, "m", "s", "p")),
        await rt.run(AgentCall(Role.PM, "m", "s", "p")),
    ]
    assert [r.text for r in results] == ["a", "c", "b"]
    assert [r.premium_requests for r in results] == [1.5, 1.5, 1.5]
    assert [c.role for c in rt.calls] == [Role.PM, Role.QA, Role.PM]


async def test_fake_runtime_callable_step_can_edit_workdir(tmp_path):
    def work(call: AgentCall) -> str:
        (call.workdir / "a.txt").write_text("hi")
        return "done"

    rt = FakeRuntime({Role.JUNIOR: [work]})
    result = await rt.run(AgentCall(Role.JUNIOR, "m", "s", "p", workdir=tmp_path))
    assert result.text == "done"
    assert Path(tmp_path / "a.txt").read_text() == "hi"


async def test_fake_runtime_callable_may_return_result():
    stuck = AgentResult("", 2, stuck=True, stuck_reason="r")
    rt = FakeRuntime({Role.JUNIOR: [lambda call: stuck]})
    assert await rt.run(AgentCall(Role.JUNIOR, "m", "s", "p")) is stuck


async def test_fake_runtime_exhausted_script_raises():
    rt = FakeRuntime({Role.PM: ["a"]})
    await rt.run(AgentCall(Role.PM, "m", "s", "p"))
    with pytest.raises(AssertionError) as exc:
        await rt.run(AgentCall(Role.PM, "m", "s", "p"))
    assert str(exc.value) == f"no scripted reply for {Role.PM}"
    with pytest.raises(AssertionError) as exc:
        await rt.run(AgentCall(Role.QA, "m", "s", "p"))
    assert str(exc.value) == f"no scripted reply for {Role.QA}"
