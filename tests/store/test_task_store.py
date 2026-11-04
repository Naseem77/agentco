import threading

import pytest

from agentco.roles import Role
from agentco.store.task_store import TaskStore
from agentco.workflow.states import InvalidTransition, TaskState

PATH = [
    (TaskState.PM, Role.PM),
    (TaskState.TRIAGE, Role.PM),
    (TaskState.ENGINEERING, Role.TRIAGE),
    (TaskState.PREFLIGHT, Role.JUNIOR),
    (TaskState.INTERNAL_REVIEW, Role.JUNIOR),
    (TaskState.LEAD_DECISION, Role.REVIEWER),
    (TaskState.PR_OPEN, Role.LEAD),
]


def walk(store, to):
    t = store.create_task("acme", "x", "cli")
    for state, actor in PATH:
        t = store.transition(t.id, state, actor, "walk")
        if state == to:
            return t
    raise AssertionError(f"{to} not on path")


def test_create_task_defaults(store):
    t = store.create_task("acme", "title", "github", source_ref="acme/x#1")
    assert t.state == TaskState.INTAKE
    assert (t.round, t.cost_usd, t.reassigned) == (0, 0.0, False)
    assert t.assignee is None and t.parent_id is None
    assert t.source_ref == "acme/x#1"
    assert store.get(t.id) == t


def test_parent_id(store):
    p = store.create_task("acme", "p", "cli")
    c = store.create_task("acme", "c", "cli", parent_id=p.id)
    assert c.parent_id == p.id


def test_get_unknown_raises(store):
    with pytest.raises(KeyError):
        store.get(999)


def test_back_to_first_floor_increments_round(store):
    t = walk(store, to=TaskState.LEAD_DECISION)
    t = store.transition(t.id, TaskState.TRIAGE, Role.LEAD, "QA failed")
    assert t.round == 1


def test_pr_feedback_increments_round(store):
    t = walk(store, to=TaskState.PR_OPEN)
    t = store.transition(t.id, TaskState.TRIAGE, Role.HUMAN, "changes requested")
    assert t.round == 1 and t.state == TaskState.TRIAGE


def test_preflight_fail_does_not_increment_round(store):
    t = walk(store, to=TaskState.PREFLIGHT)
    assert store.transition(t.id, TaskState.ENGINEERING, Role.JUNIOR, "lint").round == 0


def test_invalid_transition_raises_and_is_not_audited(store):
    t = store.create_task("acme", "x", "cli")
    with pytest.raises(InvalidTransition):
        store.transition(t.id, TaskState.PR_OPEN, Role.LEAD, "skip")
    assert store.get(t.id).state == TaskState.INTAKE
    assert [e.action for e in store.audit(t.id)] == ["created"]


def test_transition_unknown_task_raises(store):
    with pytest.raises(KeyError):
        store.transition(999, TaskState.PM, Role.PM, "x")


def test_every_transition_and_cost_is_audited(store):
    t = store.create_task("acme", "x", "cli")
    store.transition(t.id, TaskState.PM, Role.PM, "start")
    t = store.record_cost(t.id, Role.PM, 0.12, {"model": "fast-cheap"})
    assert t.cost_usd == pytest.approx(0.12)
    assert [e.action for e in store.audit(t.id)] == ["created", "transition", "cost"]


def test_audit_entry_contents(store):
    t = store.create_task("acme", "x", "cli")
    store.transition(t.id, TaskState.PM, Role.PM, "start")
    store.record_cost(t.id, Role.PM, 0.5, {"model": "m"})
    _, tr, cost = store.audit(t.id)
    assert tr.actor == Role.PM
    assert tr.detail == {"from": "intake", "to": "pm", "reason": "start"}
    assert cost.cost_usd == pytest.approx(0.5)
    assert cost.detail == {"model": "m"}
    assert tr.at.tzinfo is not None


def test_cost_accumulates(store):
    t = store.create_task("acme", "x", "cli")
    store.record_cost(t.id, Role.PM, 0.1, {})
    t = store.record_cost(t.id, Role.PM, 0.2, {})
    assert t.cost_usd == pytest.approx(0.3)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), float("-inf")])
def test_invalid_cost_rejected_without_audit(store, bad):
    t = store.create_task("acme", "x", "cli")
    with pytest.raises(ValueError) as exc:
        store.record_cost(t.id, Role.PM, bad, {})
    assert str(exc.value) == "usd must be a finite, non-negative number"
    assert store.get(t.id).cost_usd == 0
    assert [e.action for e in store.audit(t.id)] == ["created"]


def test_assign_sets_assignee_and_audits(store):
    t = store.create_task("acme", "x", "cli")
    t = store.assign(t.id, Role.SENIOR)
    assert t.assignee == Role.SENIOR and t.reassigned is False
    assert [e.action for e in store.audit(t.id)] == ["created", "assign"]


def test_assign_changes_assignee_but_not_reassigned_flag(store):
    t = store.create_task("acme", "x", "cli")
    store.assign(t.id, Role.JUNIOR)
    t = store.assign(t.id, Role.SENIOR)
    assert t.assignee == Role.SENIOR and t.reassigned is False
    assert store.get(t.id).reassigned is False


def test_assign_unknown_task_raises_key_error(store):
    with pytest.raises(KeyError):
        store.assign(999, Role.JUNIOR)


def test_meta_roundtrip(store):
    assert store.get_meta("k") is None
    store.set_meta("k", "v1")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"


def test_fresh_nested_sqlite_dir_works_end_to_end(tmp_path):
    s = TaskStore(f"sqlite:///{tmp_path}/new/dir/s.db")
    s.create_all()
    t = s.create_task("acme", "x", "cli")
    assert s.get(t.id).title == "x"
    assert (tmp_path / "new" / "dir" / "s.db").exists()


def test_missing_sqlite_dir_is_created_on_construction(tmp_path):
    TaskStore(f"sqlite:///{tmp_path}/other/s.db")
    assert (tmp_path / "other").is_dir()


def test_in_memory_sqlite_url_is_accepted():
    s = TaskStore("sqlite://")
    s.create_all()


def test_persists_across_instances(tmp_path):
    url = f"sqlite:///{tmp_path}/s.db"
    a = TaskStore(url)
    a.create_all()
    t = a.create_task("acme", "x", "cli")
    assert TaskStore(url).get(t.id).title == "x"


def test_stale_state_transition_loses_compare_and_set(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/s.db"
    first, second = TaskStore(url), TaskStore(url)
    first.create_all()
    t = first.create_task("acme", "x", "cli")
    for state, actor in PATH[:6]:
        first.transition(t.id, state, actor, "walk")
    assert first.get(t.id).state == TaskState.LEAD_DECISION

    # `second` read LEAD_DECISION before `first` moved the task on.
    stale = second.get(t.id).state
    first.transition(t.id, TaskState.TRIAGE, Role.LEAD, "QA failed")
    monkeypatch.setattr(TaskStore, "_current_state", lambda self, s, i: stale)

    with pytest.raises(InvalidTransition):
        second.transition(t.id, TaskState.TRIAGE, Role.LEAD, "QA failed")

    after = first.get(t.id)
    assert (after.state, after.round) == (TaskState.TRIAGE, 1)
    moves = [e for e in first.audit(t.id) if e.action == "transition"]
    assert sum(e.detail["reason"] == "QA failed" for e in moves) == 1


def test_concurrent_record_cost_sums_exactly(tmp_path):
    url = f"sqlite:///{tmp_path}/s.db"
    seed = TaskStore(url)
    seed.create_all()
    t = seed.create_task("acme", "x", "cli")
    threads, per_thread, usd = 8, 10, 0.25
    errors = []

    def work():
        try:
            s = TaskStore(url)
            for _ in range(per_thread):
                s.record_cost(t.id, Role.PM, usd, {})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(threads)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()

    assert errors == []
    assert seed.get(t.id).cost_usd == pytest.approx(threads * per_thread * usd)
    costs = [e for e in seed.audit(t.id) if e.action == "cost"]
    assert len(costs) == threads * per_thread
