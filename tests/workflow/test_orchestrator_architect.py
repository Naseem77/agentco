import pytest
from orch_helpers import PM_JSON, adr_json, happy, triage_json

from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.store.task_store import TaskStore
from agentco.workflow.orchestrator import Orchestrator, StepFailed
from agentco.workflow.states import TaskState
from agentco.workflow.tree import ADR_SOURCE, adr_children


def design_script(*adrs: str, triage: str | None = None) -> dict:
    return {
        Role.PM: [PM_JSON],
        Role.TRIAGE: [triage or triage_json("new_design", "l")],
        Role.ARCHITECT: list(adrs) or [adr_json(2)],
    }


def make_child(rig, ref: str = "task:1:adr:1:0") -> int:
    child = rig.store.create_task(
        "acme", "sub", ADR_SOURCE, source_ref=ref, parent_id=rig.task_id
    )
    rig.store.put_artifact(
        child.id, ArtifactKind.REQUEST, Role.ARCHITECT, {"body": "x"}
    )
    return child.id


async def test_new_design_goes_to_architect_then_waits_for_human(make_rig):
    rig = await make_rig(design_script(), seed=False)

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.transitions()[-3:] == ["triage", "architect", "awaiting_human"]
    esc = rig.escalations()[-1]
    assert (esc["category"], esc["from_state"], esc["step"]) == (
        "new",
        "architect",
        "adr",
    )
    assert esc["reason"] == "ADR: Plugin registry (2 sub-tasks)"
    assert esc["options"] == ["approve", "answer: change the design", "reject"]
    assert f"agentco adr {rig.task_id}" in esc["question"]
    assert rig.store.children(rig.task_id) == []
    assert task.assignee is Role.ARCHITECT
    assert rig.store.latest_artifact(rig.task_id, ArtifactKind.ADR) is not None


async def test_approve_spawns_children_and_waits(make_rig):
    rig = await make_rig(design_script())
    await rig.run()
    rig.decide("approve")

    task = await rig.run()

    assert task.state is TaskState.WAITING_CHILDREN
    assert len(adr_children(rig.store, task)) == 2
    assert rig.edges()[-1] == ("awaiting_human", "waiting_children")
    assert rig.github.created == []


@pytest.mark.parametrize(
    "case", ["no_decision", "answer", "reject", "budget_approve", "stale_decision"]
)
async def test_no_children_without_adr_approval(make_rig, case):
    rig = await make_rig(design_script(adr_json(2), adr_json(2, "Second draft")))
    if case == "stale_decision":
        rig.decide("approve")  # a decision from before the escalation
    task = await rig.run()
    assert task.state is TaskState.AWAITING_HUMAN

    if case == "answer":
        rig.decide("answer", "smaller please")
    elif case == "reject":
        rig.decide("reject")
    elif case == "budget_approve":
        rig.store.put_artifact(
            rig.task_id,
            ArtifactKind.ESCALATION,
            Role.LEAD,
            {
                "from_state": "architect",
                "category": "stuck",
                "reason": "budget",
                "options": [],
                "question": "",
            },
        )
        rig.decide("approve")
    task = await rig.run()

    assert rig.store.children(rig.task_id) == []
    assert task.state is (
        TaskState.CANCELLED if case == "reject" else TaskState.AWAITING_HUMAN
    )


async def test_answer_reruns_architect_with_note(make_rig):
    rig = await make_rig(design_script(adr_json(1), adr_json(3, "Revised")))
    await rig.run()
    rig.decide("answer", "use entry points")

    task = await rig.run()

    first, second = rig.calls(Role.ARCHITECT)
    assert "Human guidance" not in first.prompt
    assert "Human guidance:\nuse entry points" in second.prompt
    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.escalations()[-1]["reason"] == "ADR: Revised (3 sub-tasks)"
    rig.decide("approve")
    task = await rig.run()
    assert len(adr_children(rig.store, task)) == 3
    assert len(rig.calls(Role.ARCHITECT)) == 2


async def test_children_come_from_the_adr_the_human_saw(make_rig):
    rig = await make_rig(design_script(adr_json(2)))
    await rig.run()
    later = adr_json(3, "Sneaky")
    import json

    rig.store.put_artifact(
        rig.task_id,
        ArtifactKind.ADR,
        Role.ARCHITECT,
        {"adr": json.loads(later), "human_note": ""},
    )
    rig.decide("approve")

    task = await rig.run()

    kids = adr_children(rig.store, task)
    assert [k.title for k in kids] == [
        "Plugin registry step 0",
        "Plugin registry step 1",
    ]


async def test_resume_after_crash_spawns_once(make_rig, monkeypatch):
    rig = await make_rig(design_script())
    await rig.run()
    rig.decide("approve")
    original = TaskStore.transition
    state = {"armed": True}

    def flaky(self, task_id, to, actor, reason):
        if to is TaskState.WAITING_CHILDREN and state["armed"]:
            state["armed"] = False
            raise RuntimeError("crash")
        return original(self, task_id, to, actor, reason)

    monkeypatch.setattr(TaskStore, "transition", flaky)
    with pytest.raises(RuntimeError):
        await rig.run()
    assert len(rig.store.children(rig.task_id)) == 2

    task = await rig.run()

    assert task.state is TaskState.WAITING_CHILDREN
    assert len(rig.store.children(rig.task_id)) == 2
    for kid in rig.store.children(rig.task_id):
        assert len(rig.store.artifacts(kid.id, ArtifactKind.REQUEST)) == 1


async def test_crash_after_adr_reuses_it(make_rig, monkeypatch):
    rig = await make_rig(design_script(adr_json(2)))
    original = TaskStore.transition

    def flaky(self, task_id, to, actor, reason):
        if to is TaskState.AWAITING_HUMAN:
            monkeypatch.setattr(TaskStore, "transition", original)
            raise RuntimeError("crash")
        return original(self, task_id, to, actor, reason)

    monkeypatch.setattr(TaskStore, "transition", flaky)
    with pytest.raises(RuntimeError):
        await rig.run()

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert len(rig.calls(Role.ARCHITECT)) == 1


async def test_child_never_routes_to_architect(make_rig):
    rig = await make_rig(design_script(), seed=False)
    child = make_child(rig)

    task = await Orchestrator(rig.ctx).run(child)

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.calls(Role.ARCHITECT) == []
    edges = [a.detail["to"] for a in rig.store.audit(child) if a.action == "transition"]
    assert "architect" not in edges
    esc = rig.store.latest_artifact(child, ArtifactKind.ESCALATION)
    assert (esc.body["category"], esc.body["from_state"]) == ("new", "triage")
    assert rig.store.children(child) == []


async def test_child_skips_unknown_area_check(make_rig):
    rig = await make_rig(happy(), seed=False)
    child = make_child(rig)

    task = await Orchestrator(rig.ctx).run(child)

    edges = [a.detail["to"] for a in rig.store.audit(child) if a.action == "transition"]
    assert edges[:3] == ["pm", "triage", "engineering"]
    assert task.assignee is Role.JUNIOR
    assert all(
        e.body["from_state"] != "triage"
        for e in rig.store.artifacts(child, ArtifactKind.ESCALATION)
    )


async def test_pr_round_new_design_escalates(make_rig):
    rig = await make_rig(design_script())
    rig.store.put_artifact(
        rig.task_id, ArtifactKind.PR, Role.LEAD, {"number": 7, "url": "u"}
    )

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.calls(Role.ARCHITECT) == []
    assert rig.escalations()[-1]["from_state"] == "triage"


async def test_task_with_an_existing_worktree_never_becomes_an_adr_parent(make_rig):
    rig = await make_rig(design_script())
    (rig.ctx.repo_root / ".agentco" / "worktrees" / str(rig.task_id)).mkdir(
        parents=True
    )

    task = await rig.run()

    assert task.state is TaskState.AWAITING_HUMAN
    assert rig.calls(Role.ARCHITECT) == []
    assert rig.escalations()[-1]["from_state"] == "triage"


async def test_child_without_its_request_is_not_run_on_its_title_alone(make_rig):
    rig = await make_rig(happy(), seed=False)
    child = rig.store.create_task(  # the spawn crashed before writing the REQUEST
        "acme", "sub", ADR_SOURCE, source_ref="task:1:adr:1:0", parent_id=rig.task_id
    )

    with pytest.raises(StepFailed, match="no request"):
        await Orchestrator(rig.ctx).run(child.id)

    assert rig.calls(Role.PM) == []
    assert rig.store.get(child.id).state is TaskState.INTAKE
