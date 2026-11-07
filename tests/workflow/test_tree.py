import pytest

from agentco.agents.architect import ADRReply
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState
from agentco.workflow.tree import (
    ADR_SOURCE,
    adr_children,
    cancel_open_children,
    child_ref,
    request_text,
    spawn_children,
)


def adr_body(n: int) -> dict:
    return {
        "adr": {
            "title": "Plugin registry",
            "context": "ctx",
            "decision": "d" * 2000,
            "consequences": "c",
            "alternatives": [],
            "new_dependencies": [],
            "sub_tasks": [
                {
                    "title": f"step {i}",
                    "acceptance_criteria": [f"crit {i}a", f"crit {i}b"],
                    "size": "s",
                    "why": f"why {i}",
                }
                for i in range(n)
            ],
        },
        "human_note": "",
    }


def parent_with_adr(store: TaskStore, n: int = 3):
    parent = store.create_task("acme", "big feature", "cli")
    adr = store.put_artifact(parent.id, ArtifactKind.ADR, Role.ARCHITECT, adr_body(n))
    return parent, adr


def all_children(store: TaskStore, parent_id: int):
    return [t for t in store.children(parent_id) if t.source == ADR_SOURCE]


def test_spawn_children_creates_one_child_per_subtask_with_request(store):
    parent, adr = parent_with_adr(store, n=3)

    kids = spawn_children(store, parent, adr)

    assert [k.parent_id for k in kids] == [parent.id] * 3
    assert all(k.source == ADR_SOURCE for k in kids)
    assert [k.source_ref for k in kids] == [
        child_ref(parent.id, adr.id, i) for i in range(3)
    ]
    assert [k.title for k in kids] == ["step 0", "step 1", "step 2"]
    for i, kid in enumerate(kids):
        [request] = store.artifacts(kid.id, ArtifactKind.REQUEST)
        body = request.body["body"]
        assert f"Part of #{parent.id}: Plugin registry" in body
        assert f"crit {i}a" in body and "Size hint: s" in body and f"why {i}" in body
        assert "d" * 1500 in body and "d" * 1501 not in body
    assert store.get(parent.id).state is TaskState.INTAKE


def test_request_text_names_parent_and_criteria(store):
    parent, adr = parent_with_adr(store, n=1)
    reply = ADRReply.model_validate(adr.body["adr"])
    text = request_text(parent, reply, reply.sub_tasks[0])
    assert text.startswith(f"Part of #{parent.id}: Plugin registry")


@pytest.mark.parametrize("crash_after", ["create_task", "set_meta", "put_artifact"])
def test_spawn_children_crash_then_rerun_creates_each_child_once(
    store, monkeypatch, crash_after
):
    parent, adr = parent_with_adr(store, n=3)
    original = getattr(TaskStore, crash_after)
    calls = {"count": 0}

    def crashing(self, *args, **kwargs):
        if crash_after == "put_artifact" and args[1] is not ArtifactKind.REQUEST:
            return original(self, *args, **kwargs)
        if crash_after == "set_meta" and not str(args[0]).startswith("tree:"):
            return original(self, *args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 2:
            if crash_after == "create_task":
                original(self, *args, **kwargs)  # the row exists, nothing else does
            raise RuntimeError("boom")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TaskStore, crash_after, crashing)
    with pytest.raises(RuntimeError):
        spawn_children(store, parent, adr)
    monkeypatch.undo()

    kids = spawn_children(store, parent, adr)

    assert len(kids) == 3 and len(all_children(store, parent.id)) == 3
    assert sorted(k.source_ref for k in all_children(store, parent.id)) == [
        child_ref(parent.id, adr.id, i) for i in range(3)
    ]
    for kid in kids:
        assert len(store.artifacts(kid.id, ArtifactKind.REQUEST)) == 1


def test_spawn_children_is_a_noop_on_rerun(store):
    parent, adr = parent_with_adr(store, n=2)
    first = spawn_children(store, parent, adr)
    second = spawn_children(store, parent, adr)
    assert [k.id for k in first] == [k.id for k in second]
    assert len(store.children(parent.id)) == 2
    assert all(len(store.artifacts(k.id, ArtifactKind.REQUEST)) == 1 for k in first)


def test_spawn_children_rejects_non_adr_artifact(store):
    parent, _ = parent_with_adr(store, n=1)
    other = store.put_artifact(parent.id, ArtifactKind.REQUEST, Role.HUMAN, {})
    with pytest.raises(ValueError):
        spawn_children(store, parent, other)


def test_adr_children_ignores_followups(store):
    parent, adr = parent_with_adr(store, n=1)
    spawn_children(store, parent, adr)
    store.create_task(
        "acme", "edge", "followup", source_ref=f"task:{parent.id}", parent_id=parent.id
    )
    assert [k.source for k in adr_children(store, parent)] == [ADR_SOURCE]


def _to_pr_open(store, task_id):
    for state in (
        TaskState.PM,
        TaskState.TRIAGE,
        TaskState.ENGINEERING,
        TaskState.PREFLIGHT,
        TaskState.INTERNAL_REVIEW,
        TaskState.LEAD_DECISION,
        TaskState.PR_OPEN,
    ):
        store.transition(task_id, state, Role.HUMAN, "test")


def test_cancel_open_children_skips_pr_open_and_terminal(store):
    parent, adr = parent_with_adr(store, n=4)
    a, b, c, d = spawn_children(store, parent, adr)
    _to_pr_open(store, b.id)
    store.transition(c.id, TaskState.CANCELLED, Role.HUMAN, "earlier")
    _to_pr_open(store, d.id)
    store.transition(d.id, TaskState.MERGED, Role.HUMAN, "merged")
    followup = store.create_task(
        "acme", "f", "followup", source_ref="x", parent_id=parent.id
    )

    cancelled = cancel_open_children(store, parent.id, Role.HUMAN, "parent rejected")

    assert cancelled == [a.id]
    assert store.get(a.id).state is TaskState.CANCELLED
    assert store.get(b.id).state is TaskState.PR_OPEN
    assert store.get(d.id).state is TaskState.MERGED
    assert store.get(followup.id).state is TaskState.INTAKE
    assert cancel_open_children(store, parent.id, Role.HUMAN, "again") == []
