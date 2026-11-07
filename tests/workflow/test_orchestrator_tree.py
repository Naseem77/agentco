import json
from dataclasses import dataclass

import pytest
from orch_helpers import PM_JSON, Rig, adr_json, triage_json

from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState
from agentco.workflow.tree import adr_children

PARENT_OK = json.dumps(
    {"approve": True, "confidence": 0.9, "reason": "covered", "summary": "whole"}
)
PARENT_LOW = json.dumps(
    {"approve": True, "confidence": 0.3, "reason": "not sure", "summary": "?"}
)
PARENT_NO = json.dumps(
    {
        "approve": False,
        "confidence": 0.9,
        "reason": "criterion 2 missing",
        "summary": "?",
    }
)
_PATH = [
    TaskState.PM,
    TaskState.TRIAGE,
    TaskState.ENGINEERING,
    TaskState.PREFLIGHT,
    TaskState.INTERNAL_REVIEW,
    TaskState.LEAD_DECISION,
    TaskState.PR_OPEN,
]


@dataclass
class Tree:
    rig: Rig

    @property
    def store(self):
        return self.rig.store

    @property
    def runtime(self):
        return self.rig.runtime

    def kids(self):
        return adr_children(self.store, self.store.get(self.rig.task_id))

    def escalations(self):
        return self.rig.escalations()

    def set_state(self, task_id: int, state: str) -> None:
        if state == "cancelled":
            self.store.transition(task_id, TaskState.CANCELLED, Role.HUMAN, "t")
            return
        steps = _PATH + ([TaskState.MERGED] if state == "merged" else [])
        steps = (
            steps[: steps.index(TaskState(state)) + 1] if state != "merged" else steps
        )
        for step in steps:
            self.store.transition(task_id, step, Role.HUMAN, "t")

    def finish_children(self, *states: str) -> None:
        for kid, state in zip(self.kids(), states, strict=True):
            self.set_state(kid.id, state)

    async def run_parent(self):
        return await self.rig.run()


@pytest.fixture
async def tree(make_rig):
    rig = await make_rig(
        {
            Role.PM: [PM_JSON],
            Role.TRIAGE: [triage_json("new_design", "l")],
            Role.ARCHITECT: [adr_json(2)],
            Role.LEAD: [PARENT_OK, PARENT_OK, PARENT_OK],
        }
    )
    await rig.run()
    rig.decide("approve")
    task = await rig.run()
    assert task.state is TaskState.WAITING_CHILDREN
    return Tree(rig)


def lead_calls(tree):
    return tree.rig.calls(Role.LEAD)


async def test_parent_waits_without_llm_while_children_run(tree):
    tree.set_state(tree.kids()[0].id, "merged")

    task = await tree.run_parent()

    assert task.state is TaskState.WAITING_CHILDREN
    assert lead_calls(tree) == []
    escalations = len(tree.escalations())
    await tree.run_parent()
    assert len(tree.escalations()) == escalations


async def test_all_merged_then_lead_approves_parent(tree):
    tree.finish_children("merged", "merged")

    task = await tree.run_parent()

    assert task.state is TaskState.MERGED
    lead = tree.store.latest_artifact(task.id, ArtifactKind.LEAD)
    assert lead.body["source"] == "parent" and lead.body["outcome"] == "approve"
    assert tree.rig.github.created == []
    assert len(lead_calls(tree)) == 1
    assert "[merged]" in lead_calls(tree)[0].prompt


async def test_cancelled_child_escalates_parent_once(tree):
    tree.finish_children("merged", "cancelled")

    await tree.run_parent()
    task = await tree.run_parent()

    assert task.state is TaskState.AWAITING_HUMAN
    assert [e["step"] for e in tree.escalations() if "step" in e] == [
        "adr",
        "child_failed",
    ]
    assert tree.escalations()[-1]["from_state"] == "waiting_children"
    assert f"#{tree.kids()[1].id}" in tree.escalations()[-1]["reason"]
    assert lead_calls(tree) == []


async def test_accepting_failed_child_then_parent_review(tree):
    tree.finish_children("merged", "cancelled")
    await tree.run_parent()
    tree.rig.decide("approve")

    task = await tree.run_parent()

    assert task.state is TaskState.MERGED
    assert len(lead_calls(tree)) == 1
    assert ("awaiting_human", "waiting_children") in tree.rig.edges()
    assert [e["step"] for e in tree.escalations() if "step" in e].count(
        "child_failed"
    ) == 1


async def test_second_cancelled_child_escalates_again(tree):
    tree.finish_children("cancelled", "merged")
    await tree.run_parent()
    tree.rig.decide("approve")
    task = await tree.run_parent()
    assert task.state is TaskState.MERGED


@pytest.mark.parametrize("reply", [PARENT_LOW, PARENT_NO])
async def test_low_confidence_parent_review_goes_to_human(tree, reply):
    tree.runtime._script[Role.LEAD].clear()
    tree.runtime._script[Role.LEAD].append(reply)
    tree.finish_children("merged", "merged")

    task = await tree.run_parent()

    assert task.state is TaskState.AWAITING_HUMAN
    esc = tree.escalations()[-1]
    assert (esc["category"], esc["step"], esc["from_state"]) == (
        "tradeoff",
        "parent_review",
        "waiting_children",
    )
    assert esc["reason"] in ("not sure", "criterion 2 missing")
    lead = tree.store.latest_artifact(task.id, ArtifactKind.LEAD)
    assert lead.body["outcome"] == "escalate"


async def test_parent_review_human_approve_merges(tree):
    tree.runtime._script[Role.LEAD].clear()
    tree.runtime._script[Role.LEAD].append(PARENT_NO)
    tree.finish_children("merged", "merged")
    await tree.run_parent()
    tree.rig.decide("approve")

    task = await tree.run_parent()

    assert task.state is TaskState.MERGED
    assert len(lead_calls(tree)) == 1


async def test_parent_review_answer_reaches_next_review(tree):
    tree.runtime._script[Role.LEAD].clear()
    tree.runtime._script[Role.LEAD].extend([PARENT_NO, PARENT_OK])
    tree.finish_children("merged", "merged")
    await tree.run_parent()
    tree.rig.decide("answer", "criterion 2 is out of scope")

    task = await tree.run_parent()

    assert task.state is TaskState.MERGED
    first, second = lead_calls(tree)
    assert "Human guidance" not in first.prompt
    assert "Human guidance:\ncriterion 2 is out of scope" in second.prompt


async def test_reject_parent_cancels_open_children_only(tree):
    a, b = tree.kids()
    tree.set_state(a.id, "merged")
    tree.set_state(b.id, "engineering")
    tree.runtime._script[Role.LEAD].clear()
    tree.runtime._script[Role.LEAD].append(PARENT_NO)
    # a human rejects a parent stuck in front of them while b is still open
    tree.store.transition(tree.rig.task_id, TaskState.AWAITING_HUMAN, Role.HUMAN, "t")
    tree.store.put_artifact(
        tree.rig.task_id,
        ArtifactKind.ESCALATION,
        Role.HUMAN,
        {"from_state": "waiting_children", "category": "stuck", "reason": "x"},
    )
    tree.rig.decide("reject")

    task = await tree.run_parent()

    assert task.state is TaskState.CANCELLED
    assert tree.store.get(a.id).state is TaskState.MERGED
    assert tree.store.get(b.id).state is TaskState.CANCELLED


async def test_followup_children_do_not_block_parent(tree):
    parent_id = tree.rig.task_id
    tree.store.create_task(
        "acme", "edge", "followup", source_ref=f"task:{parent_id}", parent_id=parent_id
    )
    tree.finish_children("merged", "merged")

    task = await tree.run_parent()

    assert task.state is TaskState.MERGED
