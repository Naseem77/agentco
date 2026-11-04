from enum import StrEnum

from agentco.config import Budgets
from agentco.roles import Role
from agentco.store.models import TaskRecord


class StopDecision(StrEnum):
    CONTINUE = "continue"
    REASSIGN_SENIOR = "reassign_senior"
    ESCALATE_HUMAN = "escalate_human"


def apply_stop_rules(task: TaskRecord, budgets: Budgets) -> StopDecision:
    """Decide whether a looping task may go on (spec §5). Pure: no I/O."""
    if task.cost_usd > budgets.per_task_usd:
        return StopDecision.ESCALATE_HUMAN
    if task.round >= budgets.max_rounds:
        if task.assignee == Role.JUNIOR and not task.reassigned:
            return StopDecision.REASSIGN_SENIOR
        return StopDecision.ESCALATE_HUMAN
    return StopDecision.CONTINUE
