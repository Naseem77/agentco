from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentco.roles import Role
from agentco.workflow.states import TaskState


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC; always returns timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class ArtifactKind(StrEnum):
    REQUEST = "request"
    TARGET = "target"
    ISSUE = "issue"
    TRIAGE = "triage"
    ENGINEER = "engineer"
    PREFLIGHT = "preflight"
    REVIEW = "review"
    QA = "qa"
    LEAD = "lead"
    ESCALATION = "escalation"
    HUMAN_DECISION = "human_decision"
    PR = "pr"


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    title: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String)
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=0)
    reassigned: Mapped[bool] = mapped_column(default=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)  # JSON text
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MetaRow(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


@dataclass
class TaskRecord:
    id: int
    tenant: str
    parent_id: int | None
    title: str
    source: str
    source_ref: str | None
    state: TaskState
    assignee: Role | None
    round: int
    reassigned: bool
    cost_usd: float


@dataclass
class AuditEntry:
    at: datetime
    actor: Role | None
    action: str
    detail: dict = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Artifact:
    id: int
    task_id: int
    round: int
    kind: ArtifactKind
    role: Role
    body: dict
    created_at: datetime
