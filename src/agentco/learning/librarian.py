"""The Librarian: turns one task's decisions, reviews and outcome into memory.

It runs in-process, right after the work that produced the artifacts, and every step
is guarded by a `meta` marker, so a rerun after a crash writes nothing twice.
"""

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.config import TenantConfig
from agentco.github.client import GitHub, validate_repo
from agentco.github.errors import GitHubError
from agentco.github.models import GhUser
from agentco.importer.episodes import module_of, review_item, review_threads
from agentco.importer.locate import linkable
from agentco.learning.followup import create_qa_followup
from agentco.learning.lessons import (
    MAX_TEXT,
    Lesson,
    LessonWriter,
    artifact_source,
    bump_overrides,
    clip,
    lessons_from_lead,
    lessons_from_pr_review,
    lessons_from_qa,
)
from agentco.llm.replies import AgentStuck, Asker, Reply, ReplyInvalid
from agentco.llm.runtime import AgentCall, AgentRuntime
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState

LEARNED_KINDS = frozenset(
    {
        ArtifactKind.LEAD,
        ArtifactKind.QA,
        ArtifactKind.PR_REVIEW,
        ArtifactKind.HUMAN_DECISION,
    }
)
NO_MODEL = "no model for librarian"
MAX_LESSONS_PER_DECISION = 2
_ESCALATION_CATEGORIES = ("critical", "new", "stuck")
_OVERRIDE_CATEGORIES = frozenset({"tradeoff", "ready_to_merge"})
_NEAREST_FACTS = 5
_FACT_CLIP = 300
_PR_AUTHOR_MASK = "agentco"  # the PR is authored by the user's own gh identity
_PR_URL = re.compile(r"^https://[^/]+/([^/\s]+/[^/\s]+)/pull/\d+")
_FILES_SHOWN = 8
_MALFORMED = (ValidationError, KeyError, TypeError, AttributeError)


class _Unreadable(Exception):
    """An artifact body the deterministic extractors cannot parse."""


class DraftLesson(BaseModel):
    kind: Literal["EscalationRule", "Policy", "none"]
    text: str
    category: str = ""


class LibrarianReply(Reply):
    lessons: list[DraftLesson]

    @field_validator("lessons")
    @classmethod
    def _keep_the_first_two(cls, v: list[DraftLesson]) -> list[DraftLesson]:
        return v[:MAX_LESSONS_PER_DECISION]


@dataclass
class LearnReport:
    task_id: int
    added: int = 0
    duplicates: int = 0
    stale: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def artifact_marker(tenant: str, artifact_id: int) -> str:
    return f"librarian:{tenant}:artifact:{artifact_id}"


def closed_marker(tenant: str, task_id: int) -> str:
    return f"librarian:{tenant}:closed:{task_id}"


def _before(
    artifacts: list[Artifact], kind: ArtifactKind, limit: int
) -> Artifact | None:
    """The latest artifact of a kind older than `limit` (stable across later rounds)."""
    found = [a for a in artifacts if a.kind is kind and a.id < limit]
    return found[-1] if found else None


def _files(engineer: Artifact | None) -> list[str]:
    return (
        [str(f) for f in (engineer.body.get("files_touched") or [])] if engineer else []
    )


def _modules(engineer: Artifact | None) -> list[str]:
    return sorted({module_of(f) for f in _files(engineer)})


class Librarian:
    def __init__(
        self,
        cfg: TenantConfig,
        store: TaskStore,
        gate: MemoryGate,
        runtime: AgentRuntime,
        gh: GitHub | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.gate = gate
        self.runtime = runtime
        self.gh = gh

    def needs_learning(self, task_id: int) -> bool:
        """An unmarked artifact the Librarian learns from, or a finished task not closed out."""
        tenant = self.cfg.tenant
        task = self.store.get(task_id)
        if any(
            a.kind in LEARNED_KINDS
            and self.store.get_meta(artifact_marker(tenant, a.id)) is None
            for a in self.store.artifacts(task_id)
        ):
            return True
        return (
            task.state in (TaskState.MERGED, TaskState.CANCELLED)
            and self.store.get_meta(closed_marker(tenant, task_id)) is None
        )

    async def learn_task(
        self, task_id: int, now: datetime | None = None
    ) -> LearnReport:
        now = now or datetime.now(UTC)
        task = self.store.get(task_id)
        if task.tenant != self.cfg.tenant:
            raise ValueError(f"task {task_id} belongs to tenant {task.tenant}")
        report = LearnReport(task_id)
        writer = LessonWriter(self.store, self.gate, self.cfg.tenant)
        artifacts = self.store.artifacts(task_id)
        for artifact in artifacts:
            if artifact.kind not in LEARNED_KINDS:
                continue
            if self.store.get_meta(artifact_marker(self.cfg.tenant, artifact.id)):
                continue
            await self._learn_artifact(artifact, artifacts, writer, report, now)
        if task.state in (TaskState.MERGED, TaskState.CANCELLED):
            await self._close_out(task, artifacts, writer, report, now)
        return report

    async def _learn_artifact(
        self,
        a: Artifact,
        artifacts: list[Artifact],
        writer: LessonWriter,
        report: LearnReport,
        now: datetime,
    ) -> None:
        marker = artifact_marker(self.cfg.tenant, a.id)
        try:
            lessons = await self._lessons_for(a, artifacts, report)
        except (ReplyInvalid, AgentStuck) as exc:
            report.errors.append(f"{a.kind.value} #{a.id}: {exc}")  # retried next run
            return
        except _Unreadable as exc:
            # The body will never parse; say so once instead of on every run.
            report.errors.append(
                f"{a.kind.value} #{a.id}: unreadable artifact: {exc.__cause__!r}"
            )
            self.store.set_meta(marker, "invalid")
            return
        if lessons is None:
            return
        await self._write(writer, lessons, report, now)
        self.store.set_meta(marker, "done")

    async def _lessons_for(
        self, a: Artifact, artifacts: list[Artifact], report: LearnReport
    ) -> list[Lesson] | None:
        """None: leave the artifact unmarked (the reason is already in the report)."""
        if a.kind is not ArtifactKind.HUMAN_DECISION:
            return self._deterministic_lessons(a, artifacts)
        self._count_overrides(a, artifacts, report)
        if Role.LIBRARIAN not in self.cfg.roles:
            if NO_MODEL not in report.errors:
                report.errors.append(NO_MODEL)
            return None
        return await self._lessons_from_decision(a, artifacts)

    def _deterministic_lessons(
        self, a: Artifact, artifacts: list[Artifact]
    ) -> list[Lesson]:
        try:
            if a.kind is ArtifactKind.LEAD:
                engineer = _before(artifacts, ArtifactKind.ENGINEER, a.id)
                return lessons_from_lead(a, _modules(engineer))
            if a.kind is ArtifactKind.QA:
                return lessons_from_qa(a)
            qa = _before(artifacts, ArtifactKind.QA, a.id)
            bug_class = str(qa.body.get("bug_class") or "") if qa else ""
            return lessons_from_pr_review(a, bug_class)
        except _MALFORMED as exc:
            raise _Unreadable from exc

    def _count_overrides(
        self, decision: Artifact, artifacts: list[Artifact], report: LearnReport
    ) -> None:
        """A human approving over non-real PR findings overrides them once, however often we rerun."""
        escalation = _before(artifacts, ArtifactKind.ESCALATION, decision.id)
        review = _before(artifacts, ArtifactKind.PR_REVIEW, decision.id)
        key = f"librarian:{self.cfg.tenant}:override-counted:{decision.id}"
        if (
            decision.body.get("decision") != "approve"
            or escalation is None
            or review is None
            or escalation.body.get("from_state") != TaskState.PR_OPEN.value
            or escalation.body.get("category") not in _OVERRIDE_CATEGORIES
            or self.store.get_meta(key) is not None
        ):
            return
        fingerprints = [
            c["fingerprint"]
            for c in review.body.get("classifications") or []
            if c.get("category") != "real_bug" and c.get("fingerprint")
        ]
        stale = bump_overrides(
            self.store,
            self.cfg.tenant,
            fingerprints,
            stale_after=self.cfg.thresholds.stale_after,
        )
        self.store.set_meta(key, str(len(fingerprints)))
        for fp in stale:
            if fp not in report.stale:
                report.stale.append(fp)

    async def _lessons_from_decision(
        self, decision: Artifact, artifacts: list[Artifact]
    ) -> list[Lesson]:
        escalation = _before(artifacts, ArtifactKind.ESCALATION, decision.id)
        if escalation is None:
            return []
        memory = self.gate.for_role(Role.LIBRARIAN)
        note = str(decision.body.get("note") or "").strip()
        query = " ".join(
            filter(
                None,
                (
                    str(escalation.body.get("reason") or ""),
                    str(escalation.body.get("question") or ""),
                    note,
                ),
            )
        )
        facts = await memory.search(query, [Scope.COMPANY], limit=_NEAREST_FACTS)
        call = AgentCall(
            role=Role.LIBRARIAN,
            model=role_model(self.cfg, Role.LIBRARIAN).model,
            system=load_prompt(Role.LIBRARIAN),
            prompt=_decision_prompt(escalation, decision, note, facts),
            max_turns=self.cfg.loop.agent_max_turns,
            timeout_s=self.cfg.loop.agent_timeout_s,
        )
        asker = Asker(
            self.runtime, self.store, decision.task_id, self.cfg.price_per_request_usd
        )
        reply = await asker.ask(call, LibrarianReply)
        source = artifact_source(decision)
        lessons = []
        for draft in reply.lessons:
            text = " ".join(draft.text.split())
            if draft.kind == "none" or not text:
                continue
            if (
                draft.kind == "EscalationRule"
                and draft.category in _ESCALATION_CATEGORIES
            ):
                text = f"Escalate as {draft.category}: {text}"
            lessons.append(
                Lesson(
                    kind=draft.kind,
                    text=clip(text, MAX_TEXT),
                    source=source,
                    origin="human",
                )
            )
        return lessons

    async def _write(
        self,
        writer: LessonWriter,
        lessons: list[Lesson],
        report: LearnReport,
        now: datetime,
    ) -> None:
        for lesson in lessons:
            if await writer.write(lesson, now) == "added":
                report.added += 1
            else:
                report.duplicates += 1

    async def _close_out(
        self,
        task: TaskRecord,
        artifacts: list[Artifact],
        writer: LessonWriter,
        report: LearnReport,
        now: datetime,
    ) -> None:
        key = closed_marker(self.cfg.tenant, task.id)
        if self.store.get_meta(key) is not None:
            return
        await self._write(writer, _close_out_lessons(task, artifacts), report, now)
        prs = [a for a in artifacts if a.kind is ArtifactKind.PR]
        if self.gh is not None and prs:
            try:
                await self._learn_review_threads(prs, report)
            except GitHubError as exc:
                report.errors.append(f"review threads: {exc}")  # retried next run
                return
        if task.state is TaskState.MERGED:
            await self._create_qa_followup(task)
        self.store.set_meta(key, "done")

    async def _create_qa_followup(self, task: TaskRecord) -> None:
        await asyncio.to_thread(create_qa_followup, self.store, task)

    async def _learn_review_threads(
        self, prs: list[Artifact], report: LearnReport
    ) -> None:
        """The team's review comments on our PR, under the importer's markers."""
        assert self.gh is not None
        latest = prs[-1]
        number = latest.body.get("number")
        found = _PR_URL.match(str(latest.body.get("url") or ""))
        if not isinstance(number, int) or found is None:
            report.errors.append(f"PR #{latest.id}: no repository or number to read")
            return
        repo = validate_repo(found.group(1))
        since = min(a.created_at for a in prs)
        pull = await asyncio.to_thread(self.gh.get_pull, repo, number)
        comments = await asyncio.to_thread(self.gh.review_comments, repo, since)
        masked = replace(pull, author=GhUser(_PR_AUTHOR_MASK, False))
        selection = review_threads(
            [c for c in comments if c.pr_number == number], {number: masked}
        )
        memory = self.gate.for_role(Role.LIBRARIAN)
        for thread in selection.threads:
            marker = f"import:{self.cfg.tenant}:{repo}:review:{thread.root.id}"
            if self.store.get_meta(marker) is not None:
                report.duplicates += 1
                continue
            await memory.add(Scope.RND, review_item(repo, thread, masked, None))
            # "added" lets `agentco import` link the code later; it never writes it again.
            self.store.set_meta(marker, "added" if linkable(thread.root) else "done")
            report.added += 1


def _close_out_lessons(task: TaskRecord, artifacts: list[Artifact]) -> list[Lesson]:
    record = f"task:{task.id}:record"
    engineers = [a for a in artifacts if a.kind is ArtifactKind.ENGINEER]
    prs = [a for a in artifacts if a.kind is ArtifactKind.PR]
    lessons: list[Lesson] = []
    if task.state is TaskState.MERGED:
        for engineer in reversed(engineers):
            invariant = str(engineer.body.get("invariant") or "").strip()
            if invariant:
                lessons.append(
                    Lesson(
                        kind="Playbook",
                        text=clip(invariant, MAX_TEXT),
                        source=artifact_source(engineer),
                        modules=tuple(_modules(engineer)),
                    )
                )
                break
        lessons.append(_solved(task, artifacts, engineers[-1] if engineers else None))
    lessons.append(_profile(task, artifacts, prs, record))
    return lessons


def _solved(
    task: TaskRecord, artifacts: list[Artifact], engineer: Artifact | None
) -> Lesson:
    issues = [a for a in artifacts if a.kind is ArtifactKind.ISSUE]
    prs = [a for a in artifacts if a.kind is ArtifactKind.PR]
    summary = str(issues[-1].body.get("summary") or "") if issues else ""
    parts = []
    if prs and prs[-1].body.get("number"):
        parts.append(f"PR #{prs[-1].body['number']}")
    files = sorted(set(_files(engineer)))
    if files:
        shown = ", ".join(files[:_FILES_SHOWN])
        more = (
            f" and {len(files) - _FILES_SHOWN} more"
            if len(files) > _FILES_SHOWN
            else ""
        )
        parts.append(f"files: {clip(shown, 200)}{more}")
    where = f" ({'; '.join(parts)})" if parts else ""
    source = artifact_source(prs[-1]) if prs else f"task:{task.id}:record"
    return Lesson(
        kind="Issue",
        text=f"Solved: {clip(summary or task.title, 200)}{where}",
        source=source,
    )


def _profile(
    task: TaskRecord, artifacts: list[Artifact], prs: list[Artifact], source: str
) -> Lesson:
    real = {
        c.get("fingerprint") or c.get("finding_id")
        for a in artifacts
        if a.kind is ArtifactKind.PR_REVIEW
        for c in a.body.get("classifications") or []
        if c.get("category") == "real_bug"
    }
    assignee = task.assignee.value if task.assignee else "none"
    text = (
        f"Task #{task.id} '{clip(task.title, 80)}' ended {task.state.value}: "
        f"assignee {assignee}, reassigned {'yes' if task.reassigned else 'no'}, "
        f"rounds {task.round}, real PR findings {len(real)}, cost ${task.cost_usd:.2f}."
    )
    return Lesson(kind="AgentProfile", text=text, source=source)


def _decision_prompt(
    escalation: Artifact, decision: Artifact, note: str, facts: list
) -> str:
    body = escalation.body
    options = "\n".join(f"- {o}" for o in body.get("options") or []) or "(none)"
    known = (
        "\n".join(f"- {clip(f.text, _FACT_CLIP)}" for f in facts)
        or "(no existing facts)"
    )
    return (
        f"Escalation from state {body.get('from_state') or 'unknown'}, "
        f"category {body.get('category') or 'unknown'}.\n"
        f"Reason: {body.get('reason') or '(none)'}\n"
        f"Question: {body.get('question') or '(none)'}\n"
        f"Options:\n{options}\n\n"
        f"Human decision: {decision.body.get('decision')}\n"
        f"Human note: {note or '(none)'}\n\n"
        f"Existing company facts, nearest first:\n{known}"
    )
