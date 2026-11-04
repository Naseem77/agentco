from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from agentco.roles import Role
from agentco.store.models import (
    AuditEntry,
    AuditRow,
    Base,
    MetaRow,
    TaskRecord,
    TaskRow,
    utcnow,
)
from agentco.workflow.states import (
    ALLOWED_TRANSITIONS,
    BACK_TO_FIRST_FLOOR,
    InvalidTransition,
    TaskState,
)


def _record(row: TaskRow) -> TaskRecord:
    return TaskRecord(
        id=row.id,
        tenant=row.tenant,
        parent_id=row.parent_id,
        title=row.title,
        source=row.source,
        source_ref=row.source_ref,
        state=TaskState(row.state),
        assignee=Role(row.assignee) if row.assignee else None,
        round=row.round,
        reassigned=row.reassigned,
        cost_usd=row.cost_usd,
    )


class TaskStore:
    def __init__(self, url: str) -> None:
        db = make_url(url)
        if db.get_backend_name() == "sqlite" and db.database not in (
            None,
            "",
            ":memory:",
        ):
            Path(db.database).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(url)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self._engine)

    def _task(self, session: Session, task_id: int) -> TaskRow:
        row = session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"unknown task {task_id}")
        return row

    @staticmethod
    def _audit(
        session: Session,
        task_id: int,
        actor: Role | None,
        action: str,
        detail: dict,
        cost_usd: float = 0.0,
    ) -> None:
        session.add(
            AuditRow(
                task_id=task_id,
                actor=actor.value if actor else None,
                action=action,
                detail=detail,
                cost_usd=cost_usd,
            )
        )

    def create_task(
        self,
        tenant: str,
        title: str,
        source: str,
        source_ref: str | None = None,
        parent_id: int | None = None,
    ) -> TaskRecord:
        with self._sessions.begin() as session:
            row = TaskRow(
                tenant=tenant,
                parent_id=parent_id,
                title=title,
                source=source,
                source_ref=source_ref,
                state=TaskState.INTAKE.value,
                round=0,
                reassigned=False,
                cost_usd=0.0,
            )
            session.add(row)
            session.flush()
            self._audit(
                session, row.id, None, "created", {"title": title, "source": source}
            )
            return _record(row)

    def get(self, task_id: int) -> TaskRecord:
        with self._sessions() as session:
            return _record(self._task(session, task_id))

    def transition(
        self, task_id: int, to: TaskState, actor: Role, reason: str
    ) -> TaskRecord:
        with self._sessions.begin() as session:
            row = self._task(session, task_id)
            current = TaskState(row.state)
            if to not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> {to.value} is not allowed")
            if (current, to) in BACK_TO_FIRST_FLOOR:
                row.round += 1
            row.state = to.value
            row.updated_at = utcnow()
            self._audit(
                session,
                task_id,
                actor,
                "transition",
                {"from": current.value, "to": to.value, "reason": reason},
            )
            return _record(row)

    def assign(self, task_id: int, role: Role) -> TaskRecord:
        with self._sessions.begin() as session:
            row = self._task(session, task_id)
            previous = row.assignee
            if previous is not None and previous != role.value:
                row.reassigned = True
            row.assignee = role.value
            row.updated_at = utcnow()
            self._audit(
                session, task_id, role, "assign", {"from": previous, "to": role.value}
            )
            return _record(row)

    def record_cost(
        self, task_id: int, actor: Role, usd: float, detail: dict
    ) -> TaskRecord:
        if usd < 0:
            raise ValueError("usd must be non-negative")
        with self._sessions.begin() as session:
            row = self._task(session, task_id)
            row.cost_usd += usd
            row.updated_at = utcnow()
            self._audit(session, task_id, actor, "cost", detail, usd)
            return _record(row)

    def audit(self, task_id: int) -> list[AuditEntry]:
        with self._sessions() as session:
            rows = session.scalars(
                select(AuditRow)
                .where(AuditRow.task_id == task_id)
                .order_by(AuditRow.id)
            )
            return [
                AuditEntry(
                    at=r.at,
                    actor=Role(r.actor) if r.actor else None,
                    action=r.action,
                    detail=r.detail,
                    cost_usd=r.cost_usd,
                )
                for r in rows
            ]

    def get_meta(self, key: str) -> str | None:
        with self._sessions() as session:
            row = session.get(MetaRow, key)
            return row.value if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._sessions.begin() as session:
            session.merge(MetaRow(key=key, value=value))
