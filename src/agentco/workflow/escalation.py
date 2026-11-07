"""Hand a task to a human: one ESCALATION artifact, then AWAITING_HUMAN."""

from collections.abc import Sequence

from agentco.config import Budgets, TenantConfig
from agentco.roles import Role
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState


def write_escalation(
    store: TaskStore,
    task: TaskRecord,
    category: str,
    reason: str,
    *,
    options: Sequence[str] = (),
    question: str = "",
    stop: str | None = None,
    from_state: str | None = None,
    open_pr: bool = False,
    actor: Role = Role.LEAD,
) -> TaskRecord:
    """`open_pr` lets a human `approve` open a PR; only lead-approved work sets it."""
    body: dict = {
        "from_state": from_state or task.state.value,
        "category": category,
        "reason": reason,
        "options": list(options),
        "question": question,
    }
    if stop:
        body["stop"] = stop
    if open_pr:
        body["open_pr"] = True
    store.put_artifact(task.id, ArtifactKind.ESCALATION, actor, body)
    if task.state is TaskState.AWAITING_HUMAN:
        return task
    return store.transition(
        task.id, TaskState.AWAITING_HUMAN, actor, f"{category}: {reason}"
    )


def effective_budgets(cfg: TenantConfig, store: TaskStore, task_id: int) -> Budgets:
    """The budgets with what a human granted on this task (`add budget`, one more round)."""
    budgets = cfg.budgets
    extra = float(store.get_meta(f"task:{task_id}:extra_budget") or 0)
    rounds = store.get_meta(f"task:{task_id}:max_rounds")
    return budgets.model_copy(
        update={
            "per_task_usd": budgets.per_task_usd + extra,
            "max_rounds": int(rounds) if rounds else budgets.max_rounds,
        }
    )
