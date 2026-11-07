import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agentco.agents.replies import PMReply
from agentco.agents.triage import TriageDecision, run_triage
from agentco.learning.recurrence import area_recurrences, record_recurrence
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

    # the fake memory matches loosely, so the known fact also comes back as a playbook
    assert d.playbooks == ("the parser module handles tabs",)
    assert replace(d, playbooks=()) == TriageDecision(
        Role.JUNIOR, True, False, None, "small local change"
    )
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
        "root_cause": [],
        "playbooks": ["the parser module handles tabs"],
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

    assert replace(d, playbooks=()) == TriageDecision(
        Role.JUNIOR, True, False, None, "small local change"
    )


async def test_new_design_without_architect_escalates(store, loop_cfg):
    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design", size="l", risk="medium")
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.escalate, d.fast_lane) == (Role.SENIOR, "new", False)
    assert d.reason == (
        "a new design inside a sub-task or PR round; "
        "a human decides. Triage: small local change"
    )


async def test_human_ok_lets_new_design_through_as_senior(store, loop_cfg):
    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design", size="l")
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, human_ok=True)

    assert (d.route, d.escalate, d.fast_lane) == (Role.SENIOR, None, False)


async def test_new_design_with_architect_routes_to_architect(store, loop_cfg):
    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design", size="xs"), known=False
    )

    d = await run_triage(
        asker, loop_cfg, gate, store, task, ISSUE, allow_architect=True
    )

    assert (d.route, d.escalate, d.fast_lane, d.require_invariant) == (
        Role.ARCHITECT,
        None,
        False,
        False,
    )
    assert store.latest_artifact(task.id, ArtifactKind.TRIAGE).body["route"] == (
        "architect"
    )


async def test_allow_architect_only_affects_new_design(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(), known=False)

    d = await run_triage(
        asker, loop_cfg, gate, store, task, ISSUE, allow_architect=True
    )

    assert (d.route, d.escalate) == (Role.JUNIOR, "new")


async def test_known_ok_skips_unknown_area_but_not_new_design(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(), known=False)
    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, known_ok=True)
    assert (d.route, d.escalate) == (Role.JUNIOR, None)

    _, task, _, asker, gate = await setup(
        store, loop_cfg, triage_json(route="new_design"), known=False
    )
    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, known_ok=True)
    assert d.escalate == "new"


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
            {
                "outcome": "send_back",
                "findings_to_fix": [lead_finding(p) for p in paths],
            },
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


async def add_playbook(backend, text):
    await backend.add(
        "acme__rnd",
        Scope.RND,
        MemoryItem("p", f"Playbook (origin librarian): {text}", "t", datetime.now(UTC)),
    )


async def hot_setup(store, loop_cfg, reply):
    """A task whose one send-back is the 2nd recurrence in src/calc."""
    runtime, task, backend, asker, gate = await setup(store, loop_cfg, reply)
    record_recurrence(store, "acme", 99, ["src/calc/x.py"], "pr-review-1")  # 1st
    task = to_round(store, task.id, [["src/calc/add.py"]])
    record_recurrence(store, "acme", task.id, ["src/calc/add.py"], "pr-review-2")
    return runtime, task, backend, asker, gate


async def test_second_recurrence_forces_senior_with_invariant_and_playbooks(
    store, loop_cfg
):
    _, task, backend, asker, gate = await hot_setup(store, loop_cfg, triage_json())
    await add_playbook(backend, "src/calc: normalise inputs before compare")

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.route is Role.SENIOR and d.require_invariant and not d.fast_lane
    assert d.root_cause == ("src/calc",)
    assert any("normalise inputs" in p for p in d.playbooks)
    assert d.reason.endswith(
        " Root cause: src/calc came back 2 times; state one invariant."
    )
    [artifact] = store.artifacts(task.id, ArtifactKind.TRIAGE)
    assert artifact.body["root_cause"] == ["src/calc"]
    assert artifact.body["route"] == "senior"
    assert any("normalise inputs" in p for p in artifact.body["playbooks"])


async def test_first_recurrence_keeps_plan4_behaviour(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(size="m"))
    task = to_round(store, task.id, [["src/calc/add.py"]])
    record_recurrence(store, "acme", task.id, ["src/calc/add.py"], "pr-review-2")

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.require_invariant, d.root_cause) == (Role.JUNIOR, False, ())
    assert "Root cause" not in d.reason


async def test_threshold_is_configurable(store, loop_cfg):
    loop_cfg.thresholds.root_cause_after = 3
    _, task, _, asker, gate = await hot_setup(store, loop_cfg, triage_json(size="m"))

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.root_cause) == (Role.JUNIOR, ())


async def test_root_cause_overrides_a_new_design_route(store, loop_cfg):
    reply = triage_json(route="new_design", size="l")
    _, task, _, asker, gate = await hot_setup(store, loop_cfg, reply)

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE, human_ok=True)

    assert (d.route, d.require_invariant, d.root_cause) == (
        Role.SENIOR,
        True,
        ("src/calc",),
    )


async def test_other_modules_do_not_make_this_area_hot(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json(size="m"))
    record_recurrence(store, "acme", 98, ["src/other/x.py"], "pr-review-1")
    record_recurrence(store, "acme", 99, ["src/other/y.py"], "pr-review-2")
    task = to_round(store, task.id, [["src/calc/add.py"]])

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert (d.route, d.root_cause) == (Role.JUNIOR, ())


async def test_within_task_repeat_is_counted_once_per_round(store, loop_cfg):
    _, task, _, asker, gate = await setup(store, loop_cfg, triage_json())
    task = to_round(store, task.id, [["src/a/x.py"], ["src/a/x.py"]])

    await run_triage(asker, loop_cfg, gate, store, task, ISSUE)
    assert area_recurrences(store, "acme", ["src/a"]) == {"src/a": 1}
    again = Asker(
        FakeRuntime({Role.TRIAGE: [triage_json()]}),
        store,
        task.id,
        loop_cfg.price_per_request_usd,
    )
    await run_triage(again, loop_cfg, gate, store, task, ISSUE)
    assert area_recurrences(store, "acme", ["src/a"]) == {"src/a": 1}


async def test_pr_send_back_paths_count_like_internal_ones(store, loop_cfg):
    _, task, _, asker, gate = await hot_setup(store, loop_cfg, triage_json())
    store.put_artifact(
        task.id,
        ArtifactKind.LEAD,
        Role.LEAD,
        {
            "outcome": "send_back",
            "source": "pr",
            "findings_to_fix": [lead_finding("src/calc/mul.py")],
        },
    )

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert d.root_cause == ("src/calc",)


async def test_playbooks_are_looked_up_without_a_send_back(store, loop_cfg):
    _, task, backend, asker, gate = await setup(store, loop_cfg, triage_json())
    await add_playbook(backend, "tabs: split on whitespace once")

    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)

    assert any("split on whitespace once" in p for p in d.playbooks)
    assert d.root_cause == () and d.require_invariant is False
