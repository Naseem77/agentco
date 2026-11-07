from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from agentco.learning.metrics import metrics_report, task_metrics
from agentco.roles import Role
from agentco.store.models import ArtifactKind, AuditRow
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState

S = TaskState
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def walk(store: TaskStore, tid: int, *states: TaskState) -> None:
    for state in states:
        store.transition(tid, state, Role.LEAD, "t")


FIRST_FLOOR = (S.PM, S.TRIAGE, S.ENGINEERING, S.PREFLIGHT, S.INTERNAL_REVIEW)


def review(*ids: str) -> dict:
    return {
        "findings": [
            {"id": i, "severity": "minor", "kind": "bug", "title": i, "detail": "d"}
            for i in ids
        ],
        "summary": "s",
    }


def pr_review(*categories: str, recurrences: tuple[str, ...] = ()) -> dict:
    return {
        "pr": 7,
        "consumed": [],
        "classifications": [
            {"finding_id": f"c{n}", "fingerprint": f"fp{n}", "category": c, "note": ""}
            for n, c in enumerate(categories)
        ],
        "recurrences": list(recurrences),
        "decision": {},
    }


def stamp(store: TaskStore, tid: int, *times: datetime) -> None:
    """Give a task's audit entries (in order) fixed timestamps."""
    with store._sessions.begin() as session:
        ids = session.scalars(
            select(AuditRow.id).where(AuditRow.task_id == tid).order_by(AuditRow.id)
        ).all()
        assert len(ids) == len(times)
        for audit_id, at in zip(ids, times, strict=True):
            session.execute(
                update(AuditRow).where(AuditRow.id == audit_id).values(at=at)
            )


def busy_task(store: TaskStore) -> int:
    """2 internal rounds, 1 PR round, 3 PR findings (1 real), 1 escalation."""
    tid = store.create_task("acme", "busy", "cli").id
    store.assign(tid, Role.JUNIOR)
    walk(store, tid, *FIRST_FLOOR, S.LEAD_DECISION)
    store.put_artifact(tid, ArtifactKind.REVIEW, Role.REVIEWER, review("a", "b"))
    walk(store, tid, S.TRIAGE)  # round 1
    walk(store, tid, S.ENGINEERING, S.PREFLIGHT, S.INTERNAL_REVIEW, S.LEAD_DECISION)
    store.put_artifact(tid, ArtifactKind.REVIEW, Role.REVIEWER, review("c"))
    walk(store, tid, S.TRIAGE)  # round 2
    walk(store, tid, S.ENGINEERING, S.PREFLIGHT, S.INTERNAL_REVIEW, S.LEAD_DECISION)
    walk(store, tid, S.PR_OPEN)
    store.put_artifact(
        tid,
        ArtifactKind.PR_FEEDBACK,
        Role.LEAD,
        {
            "pr": 7,
            "head_sha": "x",
            "findings": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
        },
    )
    store.put_artifact(
        tid,
        ArtifactKind.PR_REVIEW,
        Role.LEAD,
        pr_review("real_bug", "covered_style", "edge_case", recurrences=("fp0",)),
    )
    walk(store, tid, S.TRIAGE)  # round 3, the PR round
    store.put_artifact(
        tid,
        ArtifactKind.ESCALATION,
        Role.LEAD,
        {"from_state": "triage", "category": "tradeoff", "reason": "r"},
    )
    store.record_cost(tid, Role.JUNIOR, 0.08, {"model": "m", "requests": 2})
    store.record_cost(tid, Role.LEAD, 0.12, {"model": "m", "requests": 3})
    return tid


def test_metrics_from_audit_and_artifacts(store):
    tid = busy_task(store)
    m = task_metrics(store, tid)
    assert (m.rounds, m.pr_rounds, m.pr_findings, m.pr_real, m.premium_requests) == (
        3,
        1,
        3,
        1,
        5.0,
    )
    assert m.escalations == {"tradeoff": 1}
    assert (m.review_findings, m.fix_round_findings) == (3, 1)
    assert m.recurrences == 1
    assert m.cost_usd == 0.2
    assert (m.title, m.state, m.assignee, m.reassigned) == (
        "busy",
        S.TRIAGE,
        Role.JUNIOR,
        False,
    )
    assert m.duration_h is None


def test_empty_task_has_zeroes(store):
    tid = store.create_task("acme", "idle", "cli").id
    m = task_metrics(store, tid)
    assert (m.rounds, m.pr_rounds, m.review_findings, m.pr_findings) == (0, 0, 0, 0)
    assert (m.pr_real, m.recurrences, m.premium_requests) == (0, 0, 0.0)
    assert m.escalations == {}
    assert m.duration_h is None


def test_duration_runs_from_first_audit_to_terminal_transition(store):
    tid = store.create_task("acme", "t", "cli").id
    walk(store, tid, S.CANCELLED)
    stamp(store, tid, T0, T0 + timedelta(hours=1, minutes=30))
    assert task_metrics(store, tid).duration_h == 1.5


def finished(store, title, role, hours, *, pr_findings=0, start=T0):
    tid = store.create_task("acme", title, "cli").id
    store.assign(tid, Role.JUNIOR)
    walk(store, tid, *FIRST_FLOOR, S.LEAD_DECISION, S.PR_OPEN)
    store.put_artifact(tid, ArtifactKind.PR, Role.LEAD, {"number": tid, "url": "u"})
    if pr_findings:
        store.put_artifact(
            tid,
            ArtifactKind.PR_FEEDBACK,
            Role.LEAD,
            {"pr": tid, "findings": [{"id": f"c{n}"} for n in range(pr_findings)]},
        )
    if role is Role.SENIOR:
        store.assign(tid, Role.SENIOR)
    walk(store, tid, S.MERGED)
    count = len(store.audit(tid))
    stamp(
        store,
        tid,
        *[start] * (count - 1),
        start + timedelta(hours=hours),
    )
    return tid


def test_report_totals_and_since_filter(store):
    old = finished(store, "old", Role.JUNIOR, 2, pr_findings=2, start=T0)
    finished(store, "new", Role.SENIOR, 4, pr_findings=0, start=T0 + timedelta(days=10))
    open_tid = store.create_task("acme", "open", "cli").id
    stamp(store, open_tid, T0 + timedelta(days=10))
    cancelled = store.create_task("acme", "gone", "cli").id
    walk(store, cancelled, S.CANCELLED)
    stamp(store, cancelled, T0 + timedelta(days=1), T0 + timedelta(days=1, hours=6))
    other = store.create_task("other", "foreign", "cli").id
    assert other  # a different tenant never shows up

    report = metrics_report(store, "acme", stale_after=3)
    assert [t.title for t in report.tasks] == ["old", "new", "open", "gone"]
    assert report.totals == {
        "tasks": 4.0,
        "merged": 2.0,
        "cancelled": 1.0,
        "avg_rounds": 0.0,
        "avg_pr_rounds": 0.0,
        "pr_comments_per_pr": 1.0,  # 2 findings over 2 PRs
        "escaped_real_bugs": 0.0,
        "fix_round_findings": 0.0,
        "escalations": 0.0,
        "premium_requests": 0.0,
        "cost_usd": 0.0,
        "median_duration_h": 4.0,
        "pct_junior": 50.0,  # 1 of 2 merged tasks was finished by a junior
        "recurrences": 0.0,
    }
    assert report.stale_rules == []
    assert old in {t.task_id for t in report.tasks}

    recent = metrics_report(store, "acme", stale_after=3, since=T0 + timedelta(days=5))
    assert [t.title for t in recent.tasks] == ["new", "open"]
    assert recent.totals["tasks"] == 2.0
    assert recent.totals["pr_comments_per_pr"] == 0.0
    assert recent.totals["pct_junior"] == 0.0


def test_report_sums_loop_numbers(store):
    busy_task(store)
    totals = metrics_report(store, "acme", stale_after=3).totals
    assert totals["avg_rounds"] == 3.0
    assert totals["avg_pr_rounds"] == 1.0
    assert totals["escaped_real_bugs"] == 1.0
    assert totals["fix_round_findings"] == 1.0
    assert totals["escalations"] == 1.0
    assert totals["premium_requests"] == 5.0
    assert totals["cost_usd"] == 0.2
    assert totals["recurrences"] == 1.0
    assert totals["median_duration_h"] == 0.0
    assert totals["pr_comments_per_pr"] == 0.0  # the busy task has no PR artifact


def test_empty_report(store):
    report = metrics_report(store, "acme", stale_after=3)
    assert report.tasks == []
    assert report.totals["tasks"] == 0.0
    assert report.totals["avg_rounds"] == 0.0
    assert report.totals["pct_junior"] == 0.0


def test_stale_rules_listed(store):
    store.set_meta("librarian:acme:override:aaa", "3")
    store.set_meta("librarian:acme:override:bbb", "2")
    store.set_meta("librarian:acme:override:ccc", "5")
    store.set_meta("librarian:other:override:ddd", "9")
    store.set_meta("librarian:acme:lesson:eee", "added")
    report = metrics_report(store, "acme", stale_after=3)
    assert report.stale_rules == ["ccc", "aaa"]
    assert metrics_report(store, "acme", stale_after=2).stale_rules == [
        "ccc",
        "aaa",
        "bbb",
    ]
