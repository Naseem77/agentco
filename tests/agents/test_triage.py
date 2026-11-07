import json
from datetime import UTC, datetime

import pytest

from agentco.agents.replies import PMReply
from agentco.agents.triage import TriageDecision, run_triage
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState

ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
)


def triage_json(route="junior", size="xs", risk="low", reason="small local change"):
    return json.dumps({"route": route, "size": size, "risk": risk, "reason": reason})


async def setup(store, loop_cfg, reply, *, known=True):
    runtime = FakeRuntime({Role.TRIAGE: [reply]})
    task = store.create_task("acme", "tabs", "cli")
    backend = InMemoryBackend()
    if known:
        await backend.add(
            "acme__rnd",
            Scope.RND,
            MemoryItem("n", "the parser module handles tabs", "t", datetime.now(UTC)),
        )
    asker = Asker(runtime, store, task.id, loop_cfg.price_per_request_usd)
    return runtime, task, backend, asker, MemoryGate("acme", backend)


async def test_known_junior_small_low_risk_takes_fast_lane(store, loop_cfg):
    runtime, task, _, asker, gate = await setup(store, loop_cfg, triage_json())

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d == TriageDecision(Role.JUNIOR, True, False, None, "small local change")
    [call] = runtime.calls
    assert (call.role, call.model, call.tools) == (Role.TRIAGE, "m-triage", ())
    assert call.prompt == (
        "Summary: Parser must accept tabs\n\n"
        "Why: Users paste tab-separated data\n\n"
        "Acceptance criteria:\n- Given a tab, when parsed, then no error\n\n"
        "Product notes: (none)"
    )
    [artifact] = store.artifacts(task.id, ArtifactKind.TRIAGE)
    assert artifact.role == Role.TRIAGE
    assert artifact.body == {
        "route": "junior",
        "fast_lane": True,
        "require_invariant": False,
        "escalate": None,
        "reason": "small local change",
        "reply": {
            "route": "junior",
            "size": "xs",
            "risk": "low",
            "reason": "small local change",
        },
    }


async def test_unknown_area_escalates_new(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(), known=False)

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.escalate == "new"
    assert d.fast_lane is False
    assert d.reason == "unknown area: 0 rnd facts, need 1; a human decides first"
    [artifact] = store.artifacts(task.id, ArtifactKind.TRIAGE)
    assert artifact.body["escalate"] == "new"


async def test_known_min_facts_threshold_is_configurable(store, loop_cfg):
    loop_cfg.thresholds.known_min_facts = 2
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json())

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.escalate == "new"
    assert d.reason == "unknown area: 1 rnd facts, need 2; a human decides first"


async def test_unknown_area_is_judged_on_rnd_only(store, loop_cfg):
    _, task, backend, asker, gate = await setup(
        store, loop_cfg, triage_json(), known=False
    )
    await backend.add(
        "acme__product",
        Scope.PRODUCT,
        MemoryItem("n", "tabs product fact", "t", datetime.now(UTC)),
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.escalate == "new"


async def test_human_ok_overrides_unknown(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(), known=False)

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, human_ok=True)

    assert d == TriageDecision(Role.JUNIOR, True, False, None, "small local change")


async def test_new_design_escalates_until_plan_5(store, loop_cfg):
    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design", size="l", risk="medium")
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.escalate, d.fast_lane) == (Role.SENIOR, "new", False)
    assert d.reason == (
        "new design needs the Architect (Plan 5); "
        "a human decides for now. Triage: small local change"
    )


async def test_human_ok_lets_new_design_through_as_senior(store, loop_cfg):
    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design", size="l")
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, human_ok=True)

    assert (d.route, d.escalate, d.fast_lane) == (Role.SENIOR, None, False)


def lead_finding(path):
    return {
        "id": f"f-{path}",
        "severity": "major",
        "kind": "bug",
        "path": path,
        "line": None,
        "title": "t",
        "detail": "d",
    }


def to_round(store, task_id, paths_by_round):
    """Walk the task through send-backs, leaving one LEAD artifact per round."""
    path_to_lead = (
        TaskState.ENGINEERING,
        TaskState.PREFLIGHT,
        TaskState.INTERNAL_REVIEW,
        TaskState.LEAD_DECISION,
    )
    store.transition(task_id, TaskState.PM, Role.PM, "go")
    store.transition(task_id, TaskState.TRIAGE, Role.PM, "go")
    for paths in paths_by_round:
        for state in path_to_lead:
            store.transition(task_id, state, Role.PM, "go")
        store.put_artifact(
            task_id,
            ArtifactKind.LEAD,
            Role.LEAD,
            {"findings_to_fix": [lead_finding(p) for p in paths]},
        )
        store.transition(task_id, TaskState.TRIAGE, Role.LEAD, "send back")
    return store.get(task_id)


async def test_repeat_area_forces_senior_with_invariant(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json())
    task = to_round(store, task.id, [["src/a.py", "src/b.py"], ["src/a.py"]])
    assert task.round == 2

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.require_invariant, d.fast_lane, d.escalate) == (
        Role.SENIOR,
        True,
        False,
        None,
    )
    assert d.reason == (
        "small local change Repeat area (src/a.py): fix causes with one rule."
    )


async def test_distinct_paths_across_rounds_are_not_a_repeat(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(size="m"))
    task = to_round(store, task.id, [["src/a.py"], ["src/b.py"]])

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.require_invariant) == (Role.JUNIOR, False)


async def test_same_path_twice_in_one_round_is_not_a_repeat(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(size="m"))
    task = to_round(store, task.id, [["src/a.py", "src/a.py"]])

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.require_invariant) == (Role.JUNIOR, False)


async def test_empty_paths_are_never_a_repeat(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(size="m"))
    task = to_round(store, task.id, [[""], [""]])

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.require_invariant is False


@pytest.mark.parametrize(
    ("reply", "later_round", "fast_lane_cfg", "expected"),
    [
        (triage_json(size="xs"), False, True, True),
        (triage_json(size="s"), False, True, True),
        (triage_json(size="m"), False, True, False),
        (triage_json(risk="medium"), False, True, False),
        (triage_json(route="senior"), False, True, False),
        (triage_json(), True, True, False),
        (triage_json(), False, False, False),
    ],
    ids=["xs", "s", "too-big", "risky", "senior", "later-round", "lane-disabled"],
)
async def test_fast_lane_only_for_small_low_risk_known_first_round(
    store, loop_cfg, reply, later_round, fast_lane_cfg, expected
):
    loop_cfg.loop.fast_lane = fast_lane_cfg
    _, task, _, asker, gate = await setup(store, loop_cfg, reply)
    if later_round:
        task = to_round(store, task.id, [["src/a.py"]])
        assert task.round == 1

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.fast_lane is expected
