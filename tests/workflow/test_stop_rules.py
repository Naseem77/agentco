import pytest

from agentco.config import Budgets
from agentco.roles import Role
from agentco.store.models import TaskRecord
from agentco.workflow.states import TaskState
from agentco.workflow.stop_rules import StopDecision, apply_stop_rules

B = Budgets()  # 5 USD, 3 rounds


def rec(assignee, round=0, reassigned=False, cost=0.0) -> TaskRecord:
    return TaskRecord(
        id=1,
        tenant="acme",
        parent_id=None,
        title="x",
        source="cli",
        source_ref=None,
        state=TaskState.ENGINEERING,
        assignee=assignee,
        round=round,
        reassigned=reassigned,
        cost_usd=cost,
    )


def test_junior_after_3_rounds_goes_to_senior():
    assert (
        apply_stop_rules(rec(Role.JUNIOR, round=3), B) is StopDecision.REASSIGN_SENIOR
    )


def test_reassign_happens_once():
    task = rec(Role.SENIOR, round=3, reassigned=True)
    assert apply_stop_rules(task, B) is StopDecision.ESCALATE_HUMAN


def test_junior_already_reassigned_goes_to_human():
    task = rec(Role.JUNIOR, round=3, reassigned=True)
    assert apply_stop_rules(task, B) is StopDecision.ESCALATE_HUMAN


def test_senior_after_3_rounds_goes_to_human():
    assert apply_stop_rules(rec(Role.SENIOR, round=3), B) is StopDecision.ESCALATE_HUMAN


def test_under_limit_continues():
    assert apply_stop_rules(rec(Role.JUNIOR, round=2), B) is StopDecision.CONTINUE


def test_cost_at_budget_continues_over_budget_escalates():
    assert apply_stop_rules(rec(Role.JUNIOR, cost=5.0), B) is StopDecision.CONTINUE
    assert (
        apply_stop_rules(rec(Role.JUNIOR, cost=5.01), B) is StopDecision.ESCALATE_HUMAN
    )


def test_over_budget_escalates_even_when_junior_could_be_reassigned():
    task = rec(Role.JUNIOR, round=3, cost=5.01)
    assert apply_stop_rules(task, B) is StopDecision.ESCALATE_HUMAN


def test_reassign_resets_round(store):
    t = store.assign(store.create_task("acme", "x", "cli").id, Role.JUNIOR)
    t = store.reassign_to_senior(t.id)
    assert (t.assignee, t.round, t.reassigned) == (Role.SENIOR, 0, True)
    assert store.audit(t.id)[-1].action == "reassigned"


def test_reassign_twice_raises_and_writes_no_audit(store):
    t = store.assign(store.create_task("acme", "x", "cli").id, Role.JUNIOR)
    store.reassign_to_senior(t.id)
    audit_len = len(store.audit(t.id))
    with pytest.raises(ValueError, match="once"):
        store.reassign_to_senior(t.id)
    assert len(store.audit(t.id)) == audit_len


def test_reassign_unknown_task_raises_key_error(store):
    with pytest.raises(KeyError):
        store.reassign_to_senior(999)
