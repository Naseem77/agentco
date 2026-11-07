import json
import math
from collections.abc import Collection
from pathlib import Path

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from agentco.roles import Role
from agentco.store.models import (
    Artifact,
    ArtifactKind,
    ArtifactRow,
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


def _artifact(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        task_id=row.task_id,
        round=row.round,
        kind=ArtifactKind(row.kind),
        role=Role(row.role),
        body=json.loads(row.body),
        created_at=row.created_at,
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
        self._engine = create_engine(
            url,
            connect_args={"timeout": 30} if db.get_backend_name() == "sqlite" else {},
        )
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create missing tables. There is no migration tool yet, so a database
        created before a table existed only gains that table, not new columns."""
        Base.metadata.create_all(self._engine)

    def _task(self, session: Session, task_id: int) -> TaskRow:
        row = session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f"unknown task {task_id}")
        return row

    def _current_state(self, session: Session, task_id: int) -> TaskState:
        return TaskState(self._task(session, task_id).state)

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
            current = self._current_state(session, task_id)
            if to not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> {to.value} is not allowed")
            # Compare-and-set on the state we validated against; a concurrent
            # transition makes this match no rows.
            result = session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.state == current.value)
                .values(
                    state=to.value,
                    round=TaskRow.round + int((current, to) in BACK_TO_FIRST_FLOOR),
                    updated_at=utcnow(),
                )
            )
            if result.rowcount == 0:
                raise InvalidTransition(
                    f"task {task_id} is no longer in state {current.value}"
                )
            self._audit(
                session,
                task_id,
                actor,
                "transition",
                {"from": current.value, "to": to.value, "reason": reason},
            )
            session.flush()
            session.expire_all()
            return _record(self._task(session, task_id))

    def list_tasks(
        self, tenant: str, states: Collection[TaskState] | None = None
    ) -> list[TaskRecord]:
        stmt = select(TaskRow).where(TaskRow.tenant == tenant).order_by(TaskRow.id)
        if states is not None:
            stmt = stmt.where(TaskRow.state.in_([s.value for s in states]))
        with self._sessions() as session:
            return [_record(r) for r in session.scalars(stmt)]

    def put_artifact(
        self, task_id: int, kind: ArtifactKind, role: Role, body: dict
    ) -> Artifact:
        with self._sessions.begin() as session:
            task = self._task(session, task_id)
            row = ArtifactRow(
                task_id=task_id,
                round=task.round,
                kind=kind.value,
                role=role.value,
                body=json.dumps(body, sort_keys=True),
            )
            session.add(row)
            self._audit(session, task_id, role, "artifact", {"kind": kind.value})
            session.flush()
            return _artifact(row)

    def artifacts(
        self,
        task_id: int,
        kind: ArtifactKind | None = None,
        round: int | None = None,
    ) -> list[Artifact]:
        stmt = select(ArtifactRow).where(ArtifactRow.task_id == task_id)
        if kind is not None:
            stmt = stmt.where(ArtifactRow.kind == kind.value)
        if round is not None:
            stmt = stmt.where(ArtifactRow.round == round)
        with self._sessions() as session:
            return [
                _artifact(r) for r in session.scalars(stmt.order_by(ArtifactRow.id))
            ]

    def latest_artifact(self, task_id: int, kind: ArtifactKind) -> Artifact | None:
        with self._sessions() as session:
            row = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.task_id == task_id, ArtifactRow.kind == kind.value)
                .order_by(ArtifactRow.id.desc())
                .limit(1)
            ).first()
            return _artifact(row) if row else None

    def assign(self, task_id: int, role: Role) -> TaskRecord:
        with self._sessions.begin() as session:
            row = self._task(session, task_id)
            previous = row.assignee
            row.assignee = role.value
            row.updated_at = utcnow()
            self._audit(
                session, task_id, role, "assign", {"from": previous, "to": role.value}
            )
            return _record(row)

    def reassign_to_senior(self, task_id: int) -> TaskRecord:
        with self._sessions.begin() as session:
            # Atomic once-only guard: of two concurrent callers, one matches no row.
            result = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.reassigned.is_(False),
                    TaskRow.assignee == Role.JUNIOR.value,
                )
                .values(
                    assignee=Role.SENIOR.value,
                    round=0,
                    reassigned=True,
                    updated_at=utcnow(),
                )
            )
            if result.rowcount == 0:
                row = self._task(session, task_id)  # unknown id -> KeyError
                if row.reassigned:
                    raise ValueError(
                        f"task {task_id} was already reassigned; once only"
                    )
                raise ValueError(
                    f"task {task_id} is not junior (assignee: {row.assignee}); "
                    "only a junior task can be handed to senior"
                )
            # System decision (stop rules), not an agent action: no actor.
            self._audit(session, task_id, None, "reassigned", {"to": Role.SENIOR.value})
            session.flush()
            session.expire_all()
            return _record(self._task(session, task_id))

    def record_cost(
        self, task_id: int, actor: Role, usd: float, detail: dict
    ) -> TaskRecord:
        if not math.isfinite(usd) or usd < 0:
            raise ValueError("usd must be a finite, non-negative number")
        with self._sessions.begin() as session:
            result = session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(cost_usd=TaskRow.cost_usd + usd, updated_at=utcnow())
            )
            if result.rowcount == 0:
                raise KeyError(f"unknown task {task_id}")
            self._audit(session, task_id, actor, "cost", detail, usd)
            session.flush()
            session.expire_all()
            return _record(self._task(session, task_id))

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

    def meta_with_prefix(self, prefix: str) -> dict[str, str]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MetaRow)
                .where(MetaRow.key.startswith(prefix, autoescape=True))
                .order_by(MetaRow.key)
            )
            return {r.key: r.value for r in rows}

    def set_meta(self, key: str, value: str) -> None:
        with self._sessions.begin() as session:
            session.merge(MetaRow(key=key, value=value))
