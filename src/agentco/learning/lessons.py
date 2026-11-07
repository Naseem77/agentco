"""Lessons the Librarian writes to memory: the model, the deduplicating writer, extractors."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from agentco.agents.qa import QAFailure
from agentco.config import Thresholds
from agentco.intake.findings import normalize_text
from agentco.memory.backend import MemoryItem
from agentco.memory.entities import ENTITY_TYPES
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import Artifact
from agentco.store.task_store import TaskStore

LessonKind = Literal[
    "ReviewRule",
    "EscalationRule",
    "Policy",
    "Playbook",
    "Issue",
    "Risk",
    "AgentProfile",
    "EscapedBug",
    "NeighbourCase",
]
MAX_TEXT = 500
_ELLIPSIS = "…"
_REAL_BUG = "real_bug"


def _kind_scope() -> dict[str, Scope]:
    scopes = {
        kind: scope
        for scope, kinds in ENTITY_TYPES.items()
        for kind in kinds
        if kind in get_args(LessonKind)
    }
    assert set(scopes) == set(get_args(LessonKind)), (
        "lesson kind missing an entity type"
    )
    assert Scope.PRODUCT not in scopes.values(), "the Librarian never writes product"
    return {kind: scopes[kind] for kind in get_args(LessonKind)}


KIND_SCOPE: dict[str, Scope] = _kind_scope()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def clip(text: str, limit: int) -> str:
    text = _one_line(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + _ELLIPSIS


class Lesson(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: LessonKind
    text: str
    source: str
    modules: tuple[str, ...] = ()
    origin: Literal["lead", "human", "librarian"] = "librarian"

    @field_validator("text")
    @classmethod
    def _text_is_one_bounded_line(cls, v: str) -> str:
        v = _one_line(v)
        if not v:
            raise ValueError("lesson text is empty")
        if len(v) > MAX_TEXT:
            raise ValueError(f"lesson text is over {MAX_TEXT} characters")
        return v

    @field_validator("source")
    @classmethod
    def _source_is_one_line(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("modules")
    @classmethod
    def _modules_are_one_line(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_one_line(m) for m in v)


def _digest(lesson: Lesson) -> str:
    material = lesson.kind + normalize_text(lesson.text)
    return hashlib.sha1(material.encode()).hexdigest()[:16]


def lesson_key(tenant: str, lesson: Lesson) -> str:
    return f"librarian:{tenant}:lesson:{_digest(lesson)}"


def lesson_item(lesson: Lesson, now: datetime) -> MemoryItem:
    body = f"{lesson.kind} (origin {lesson.origin}): {lesson.text}"
    if lesson.modules:
        body += f"\nApplies to: {', '.join(lesson.modules)}"
    body += f"\nSource: {lesson.source}"
    return MemoryItem(
        name=f"{lesson.kind}:{_digest(lesson)}",
        body=body,
        source_description=f"agentco librarian: {lesson.source}",
        reference_time=now,
    )


@dataclass
class LessonWriter:
    store: TaskStore
    gate: MemoryGate
    tenant: str

    async def write(
        self, lesson: Lesson, now: datetime
    ) -> Literal["added", "duplicate"]:
        key = lesson_key(self.tenant, lesson)
        if self.store.get_meta(key) is not None:
            return "duplicate"
        scope = KIND_SCOPE[lesson.kind]
        memory = self.gate.for_role(Role.LIBRARIAN)
        wanted = normalize_text(lesson.text)
        facts = await memory.search(lesson.text, scopes=[scope], limit=10)
        if any(wanted in normalize_text(fact.text) for fact in facts):
            self.store.set_meta(key, "duplicate")
            return "duplicate"
        await memory.add(scope, lesson_item(lesson, now))
        self.store.set_meta(key, "added")
        return "added"


def artifact_source(a: Artifact) -> str:
    return f"task:{a.task_id}:{a.kind.value}:{a.id}"


def lessons_from_lead(a: Artifact, modules: Iterable[str]) -> list[Lesson]:
    applies_to = tuple(sorted(set(modules)))
    comments = a.body.get("comments") or []
    return [
        Lesson(
            kind="ReviewRule",
            text=clip(comment, MAX_TEXT),
            source=artifact_source(a),
            modules=applies_to,
            origin="lead",
        )
        for comment in comments
        if isinstance(comment, str) and comment.strip()
    ]


def lessons_from_qa(a: Artifact) -> list[Lesson]:
    """Only what spec §7 lets back-channel to rnd: title, steps, expected, actual."""
    lessons = []
    for raw in a.body.get("failures") or []:
        failure = QAFailure.model_validate(raw)
        text = (
            f"QA failure: {clip(failure.title, 100)}. "
            f"Steps: {clip(failure.steps, 150)}. "
            f"Expected: {clip(failure.expected, 100)}. "
            f"Actual: {clip(failure.actual, 100)}."
        )
        lessons.append(Lesson(kind="Issue", text=text, source=artifact_source(a)))
    return lessons


def lessons_from_pr_review(a: Artifact, bug_class: str) -> list[Lesson]:
    """A real bug found on the PR escaped the internal loop; recurrences add nothing here."""
    recurrences = set(a.body.get("recurrences") or [])
    details = {
        f["id"]: f for f in (a.body.get("decision") or {}).get("findings_to_fix") or []
    }
    pr = a.body.get("pr")
    where = f"PR #{pr}" if pr else "a PR"
    bug_class = _one_line(bug_class)
    lessons = []
    for c in a.body.get("classifications") or []:
        if c.get("category") != _REAL_BUG or c.get("fingerprint") in recurrences:
            continue
        detail = details.get(c.get("finding_id"), {})
        title = clip(detail.get("title") or c.get("note") or c["finding_id"], 150)
        path = _one_line(detail.get("path") or "")
        located = f"{title} ({path})" if path else title
        neighbours = f"also test the neighbours of {title}"
        if bug_class:
            neighbours = f"bug class {clip(bug_class, 100)}: {neighbours}"
        for kind, text in (
            ("Risk", located),
            ("EscapedBug", f"{located}, escaped to {where}"),
            ("NeighbourCase", neighbours),
        ):
            lessons.append(Lesson(kind=kind, text=text, source=artifact_source(a)))
    return lessons


def bump_overrides(
    store: TaskStore,
    tenant: str,
    fingerprints: Iterable[str],
    stale_after: int = Thresholds().stale_after,
) -> list[str]:
    """Count one more human override per fingerprint; return those now stale candidates."""
    stale = []
    for fp in dict.fromkeys(fingerprints):
        key = f"librarian:{tenant}:override:{fp}"
        try:
            count = int(store.get_meta(key) or 0)
        except ValueError:
            count = 0
        count += 1
        store.set_meta(key, str(count))
        if count >= stale_after:
            stale.append(fp)
    return stale
