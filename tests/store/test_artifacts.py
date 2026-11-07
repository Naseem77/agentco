import pytest

from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState


def to_preflight(store, task_id):
    for s in (
        TaskState.PM,
        TaskState.TRIAGE,
        TaskState.ENGINEERING,
        TaskState.PREFLIGHT,
    ):
        store.transition(task_id, s, Role.PM, "go")


def test_artifacts_carry_round_and_kind(store):
    t = store.create_task("acme", "x", "cli")
    store.put_artifact(t.id, ArtifactKind.REQUEST, Role.HUMAN, {"body": "b"})
    to_preflight(store, t.id)
    store.transition(t.id, TaskState.TRIAGE, Role.PM, "attempts exhausted")
    a = store.put_artifact(t.id, ArtifactKind.PREFLIGHT, Role.SENIOR, {"ok": False})
    assert (a.round, store.get(t.id).round) == (1, 1)
    assert [x.kind for x in store.artifacts(t.id)] == [
        ArtifactKind.REQUEST,
        ArtifactKind.PREFLIGHT,
    ]
    assert store.latest_artifact(t.id, ArtifactKind.REQUEST).body == {"body": "b"}


def test_preflight_to_triage_starts_a_new_round(store):
    t = store.create_task("acme", "x", "cli")
    to_preflight(store, t.id)
    assert store.transition(t.id, TaskState.TRIAGE, Role.PM, "exhausted").round == 1


def test_put_artifact_returns_roundtripped_values_and_audits(store):
    t = store.create_task("acme", "x", "cli")
    a = store.put_artifact(
        t.id, ArtifactKind.HUMAN_DECISION, Role.HUMAN, {"b": 1, "a": [2]}
    )
    assert (a.task_id, a.round, a.kind, a.role) == (
        t.id,
        0,
        ArtifactKind.HUMAN_DECISION,
        Role.HUMAN,
    )
    assert a.body == {"a": [2], "b": 1} and a.created_at.tzinfo is not None
    assert store.artifacts(t.id) == [a]
    entry = store.audit(t.id)[-1]
    assert (entry.action, entry.actor, entry.detail) == (
        "artifact",
        Role.HUMAN,
        {"kind": "human_decision"},
    )


def test_artifact_filters_and_latest(store):
    t = store.create_task("acme", "x", "cli")
    other = store.create_task("acme", "y", "cli")
    first = store.put_artifact(t.id, ArtifactKind.TRIAGE, Role.TRIAGE, {"n": 1})
    store.put_artifact(other.id, ArtifactKind.TRIAGE, Role.TRIAGE, {"n": 99})
    to_preflight(store, t.id)
    store.transition(t.id, TaskState.TRIAGE, Role.PM, "again")
    second = store.put_artifact(t.id, ArtifactKind.TRIAGE, Role.TRIAGE, {"n": 2})
    review = store.put_artifact(t.id, ArtifactKind.REVIEW, Role.REVIEWER, {})
    assert store.artifacts(t.id, kind=ArtifactKind.TRIAGE) == [first, second]
    assert store.artifacts(t.id, round=1) == [second, review]
    assert store.artifacts(t.id, kind=ArtifactKind.TRIAGE, round=0) == [first]
    assert store.artifacts(t.id, kind=ArtifactKind.QA) == []
    assert store.latest_artifact(t.id, ArtifactKind.TRIAGE) == second
    assert store.latest_artifact(t.id, ArtifactKind.QA) is None


def test_put_artifact_unknown_task_raises(store):
    with pytest.raises(KeyError):
        store.put_artifact(404, ArtifactKind.PR, Role.LEAD, {})


def test_list_tasks_filters_tenant_and_state(store):
    a = store.create_task("acme", "a", "cli")
    b = store.create_task("acme", "b", "cli")
    store.create_task("other", "c", "cli")
    store.transition(b.id, TaskState.PM, Role.PM, "go")
    assert [t.id for t in store.list_tasks("acme")] == [a.id, b.id]
    assert [t.id for t in store.list_tasks("acme", {TaskState.PM})] == [b.id]
    assert [
        t.id for t in store.list_tasks("acme", [TaskState.INTAKE, TaskState.PM])
    ] == [
        a.id,
        b.id,
    ]
    assert store.list_tasks("acme", []) == []
    assert store.list_tasks("nobody") == []
