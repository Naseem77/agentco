from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from agentco.roles import Role
from agentco.store.models import ArtifactKind, AuditEntry, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import BACK_TO_FIRST_FLOOR, TaskState


@dataclass(frozen=True)
class TaskMetrics:
    task_id: int
    title: str
    state: TaskState
    assignee: Role | None
    reassigned: bool
    rounds: int
    pr_rounds: int
    review_findings: int
    fix_round_findings: int
    pr_findings: int
    pr_real: int
    escalations: dict[str, int]
    premium_requests: float
    cost_usd: float
    duration_h: float | None
    recurrences: int


@dataclass(frozen=True)
class MetricsReport:
    tasks: list[TaskMetrics]
    totals: dict[str, float]
    stale_rules: list[str]


def _transitions(audit: list[AuditEntry]) -> list[tuple[str, str]]:
    return [
        (e.detail.get("from", ""), e.detail.get("to", ""))
        for e in audit
        if e.action == "transition"
    ]


def _count(body: dict, key: str) -> int:
    value = body.get(key)
    return len(value) if isinstance(value, list) else 0


def _duration_h(audit: list[AuditEntry]) -> float | None:
    ends = [
        e.at
        for e in audit
        if e.action == "transition"
        and e.detail.get("to") in (TaskState.MERGED, TaskState.CANCELLED)
    ]
    if not ends:
        return None
    return (ends[-1] - audit[0].at).total_seconds() / 3600


def _task_metrics(
    store: TaskStore, task: TaskRecord, audit: list[AuditEntry]
) -> TaskMetrics:
    back = {(a.value, b.value) for a, b in BACK_TO_FIRST_FLOOR}
    transitions = _transitions(audit)
    reviews = store.artifacts(task.id, ArtifactKind.REVIEW)
    pr_reviews = store.artifacts(task.id, ArtifactKind.PR_REVIEW)
    classifications = [c for a in pr_reviews for c in a.body.get("classifications", [])]
    escalations = Counter(
        str(a.body.get("category", ""))
        for a in store.artifacts(task.id, ArtifactKind.ESCALATION)
    )
    return TaskMetrics(
        task_id=task.id,
        title=task.title,
        state=task.state,
        assignee=task.assignee,
        reassigned=task.reassigned,
        rounds=sum(1 for t in transitions if t in back),
        pr_rounds=transitions.count((TaskState.PR_OPEN.value, TaskState.TRIAGE.value)),
        review_findings=sum(_count(a.body, "findings") for a in reviews),
        fix_round_findings=sum(
            _count(a.body, "findings") for a in reviews if a.round > 0
        ),
        pr_findings=sum(
            _count(a.body, "findings")
            for a in store.artifacts(task.id, ArtifactKind.PR_FEEDBACK)
        ),
        pr_real=sum(1 for c in classifications if c.get("category") == "real_bug"),
        escalations=dict(escalations),
        premium_requests=float(
            sum(e.detail.get("requests", 0) for e in audit if e.action == "cost")
        ),
        cost_usd=task.cost_usd,
        duration_h=_duration_h(audit),
        recurrences=sum(_count(a.body, "recurrences") for a in pr_reviews),
    )


def task_metrics(store: TaskStore, task_id: int) -> TaskMetrics:
    return _task_metrics(store, store.get(task_id), store.audit(task_id))


def _mean(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _totals(tasks: list[TaskMetrics], prs: int) -> dict[str, float]:
    merged = [t for t in tasks if t.state is TaskState.MERGED]
    durations = [t.duration_h for t in tasks if t.duration_h is not None]
    return {
        "tasks": float(len(tasks)),
        "merged": float(len(merged)),
        "cancelled": float(sum(t.state is TaskState.CANCELLED for t in tasks)),
        "avg_rounds": _mean([t.rounds for t in tasks]),
        "avg_pr_rounds": _mean([t.pr_rounds for t in tasks]),
        "pr_comments_per_pr": sum(t.pr_findings for t in tasks) / prs if prs else 0.0,
        "escaped_real_bugs": float(sum(t.pr_real for t in tasks)),
        "fix_round_findings": float(sum(t.fix_round_findings for t in tasks)),
        "escalations": float(sum(sum(t.escalations.values()) for t in tasks)),
        "premium_requests": sum(t.premium_requests for t in tasks),
        "cost_usd": sum(t.cost_usd for t in tasks),
        "median_duration_h": float(median(durations)) if durations else 0.0,
        "pct_junior": (
            100 * sum(t.assignee is Role.JUNIOR for t in merged) / len(merged)
            if merged
            else 0.0
        ),
        "recurrences": float(sum(t.recurrences for t in tasks)),
    }


def _stale_rules(store: TaskStore, tenant: str, stale_after: int) -> list[str]:
    prefix = f"librarian:{tenant}:override:"
    stale = []
    for key, value in store.meta_with_prefix(prefix).items():
        try:
            count = int(value)
        except ValueError:
            continue
        if count >= stale_after:
            stale.append((-count, key.removeprefix(prefix)))
    return [fingerprint for _, fingerprint in sorted(stale)]


def metrics_report(
    store: TaskStore,
    tenant: str,
    stale_after: int,
    since: datetime | None = None,
) -> MetricsReport:
    tasks: list[TaskMetrics] = []
    prs = 0
    for record in store.list_tasks(tenant):
        audit = store.audit(record.id)
        if since is not None and (not audit or audit[0].at <= since):
            continue
        tasks.append(_task_metrics(store, record, audit))
        prs += store.latest_artifact(record.id, ArtifactKind.PR) is not None
    return MetricsReport(
        tasks=tasks,
        totals=_totals(tasks, prs),
        stale_rules=_stale_rules(store, tenant, stale_after),
    )
