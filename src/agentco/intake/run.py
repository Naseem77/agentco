"""Intake: read what happened to our open PRs, then move the tasks (spec §6).

Every step stores its result before it is acted on and acts idempotently, so a run
that dies anywhere is finished by the next one. The PR_REVIEW artifact is the
commit point of a feedback round; its `intake:{tenant}:review:{id}` marker says it
has been applied. Intake never comments on, approves or merges a PR.
"""

import asyncio
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import TypeAdapter

from agentco.agents.lead import LeadDecision
from agentco.agents.replies import Finding
from agentco.config import TenantConfig
from agentco.github.client import GitHub
from agentco.github.errors import GitHubError, InvalidRef
from agentco.github.models import GhPull, GhReviewComment
from agentco.intake.classify import PRRoundDecision, classify_round
from agentco.intake.findings import (
    PRFinding,
    TrackedPull,
    collect_findings,
    tracked_pulls,
)
from agentco.llm.replies import AgentStuck, Asker, ReplyInvalid
from agentco.llm.runtime import AgentRuntime
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.escalation import effective_budgets, write_escalation
from agentco.workflow.states import InvalidTransition, TaskState
from agentco.workflow.stop_rules import StopDecision, apply_stop_rules

_S = TaskState
_ROUND_OPTIONS = ("answer: one more round", "reject")
_BUDGET_OPTIONS = ("add budget", "reject")
CURSOR_OVERLAP = timedelta(
    minutes=5
)  # our clock vs GitHub's; markers dedupe the re-read
INTAKE_LEASE_TTL_S = 3600
_DECISION = TypeAdapter(PRRoundDecision)
_LEAD = TypeAdapter(LeadDecision)


@dataclass
class IntakeReport:
    repo: str
    pulls: int = 0
    new_findings: int = 0
    reentered: list[int] = field(default_factory=list)
    escalated: list[tuple[int, str]] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    known: int = 0
    deferred: list[int] = field(default_factory=list)
    touched: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    github_failed: bool = False
    busy: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def touch(self, task_id: int) -> None:
        if task_id not in self.touched:
            self.touched.append(task_id)


def _read_cursor(store: TaskStore, key: str) -> datetime | None:
    raw = store.get_meta(key)
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# -- the small steps; each is idempotent and named so a test can interrupt it -------


def _store_feedback(
    store: TaskStore, task_id: int, number: int, head_sha: str, found: list[PRFinding]
) -> Artifact:
    return store.put_artifact(
        task_id,
        ArtifactKind.PR_FEEDBACK,
        Role.LEAD,
        {
            "pr": number,
            "head_sha": head_sha,
            "findings": [f.model_dump(mode="json") for f in found],
        },
    )


def _mark_feedback(
    store: TaskStore, tenant: str, repo: str, found: list[PRFinding], artifact_id: int
) -> None:
    for f in found:
        store.set_meta(f.marker_key(tenant, repo), f"feedback:{artifact_id}")


def _store_review(
    store: TaskStore,
    task_id: int,
    number: int,
    pending: list[PRFinding],
    decision: PRRoundDecision,
) -> Artifact:
    fingerprints = {f.id: f.fingerprint for f in pending}
    classifications = [
        {
            "finding_id": c.finding_id,
            "fingerprint": fingerprints[c.finding_id],
            "category": c.category,
            "note": c.note,
        }
        for c in decision.classifications
    ]
    classifications += [
        {
            "finding_id": i,
            "fingerprint": fingerprints[i],
            "category": "known",
            "note": "already decided in an earlier round",
        }
        for i in decision.known
    ]
    return store.put_artifact(
        task_id,
        ArtifactKind.PR_REVIEW,
        Role.LEAD,
        {
            "pr": number,
            "consumed": [c["finding_id"] for c in classifications],
            "classifications": classifications,
            "recurrences": list(decision.recurrences),
            "decision": _DECISION.dump_python(decision, mode="json"),
        },
    )


def _store_send_back(store: TaskStore, task_id: int, review: Artifact) -> None:
    decision = review.body["decision"]
    lead = LeadDecision(
        outcome="send_back",
        category="",
        findings_to_fix=tuple(Finding(**f) for f in decision["findings_to_fix"]),
        follow_ups=tuple(Finding(**f) for f in decision["follow_ups"]),
        reason="PR feedback",
        summary=decision["summary"],
        comments=(),
        question="",
        options=(),
    )
    body = _LEAD.dump_python(lead, mode="json") | {
        "source": "pr",
        "pr": review.body["pr"],
        "pr_review": review.id,
    }
    store.put_artifact(task_id, ArtifactKind.LEAD, Role.LEAD, body)


def _move_to_triage(store: TaskStore, task_id: int) -> None:
    store.transition(task_id, _S.TRIAGE, Role.LEAD, "PR feedback to fix")


def _mark_applied(store: TaskStore, tenant: str, review_id: int) -> None:
    store.set_meta(f"intake:{tenant}:review:{review_id}", "applied")


# -- one run ------------------------------------------------------------------------


class _Intake:
    def __init__(
        self,
        cfg: TenantConfig,
        store: TaskStore,
        gh: GitHub,
        runtime: AgentRuntime,
        gate: MemoryGate,
        repo: str,
        report: IntakeReport,
        since: datetime,
    ) -> None:
        self.cfg, self.store, self.gh = cfg, store, gh
        self.runtime, self.gate = runtime, gate
        self.repo, self.report, self.since = repo, report, since
        self.comments: dict[int, list[GhReviewComment]] = {}

    async def pull(self, tp: TrackedPull) -> None:
        task = self.store.get(tp.task.id)
        if task.state in (_S.MERGED, _S.CANCELLED):
            return
        pull = await asyncio.to_thread(self.gh.get_pull, self.repo, tp.number)
        if pull.merged_at is not None:
            self._merged(task)
        elif pull.state == "closed":
            self._closed(task)
        else:
            await self._feedback(tp, pull)

    def _finish(self, task: TaskRecord, to: _S, reason: str, bucket: list[int]) -> None:
        self.store.transition(task.id, to, Role.HUMAN, reason)
        bucket.append(task.id)
        self.report.touch(task.id)

    def _merged(self, task: TaskRecord) -> None:
        if task.state in (_S.PR_OPEN, _S.AWAITING_HUMAN):
            self._finish(task, _S.MERGED, "PR merged by a human", self.report.merged)
        else:
            self._finish(
                task,
                _S.CANCELLED,
                "PR merged by a human mid-round",
                self.report.merged,
            )

    def _closed(self, task: TaskRecord) -> None:
        self._finish(task, _S.CANCELLED, "PR closed by a human", self.report.closed)

    async def _feedback(self, tp: TrackedPull, pull: GhPull) -> None:
        task = self.store.get(tp.task.id)
        await self._record_feedback(tp, task, pull)
        task = self.store.get(task.id)
        review = self._unapplied_review(task.id)
        if review is not None:
            await self._apply(task, review)
            task = self.store.get(task.id)
        pending = self._pending(task.id, pull.head_sha)
        if not pending:
            return
        if task.state is not _S.PR_OPEN:
            self.report.deferred.append(task.id)
            return
        decision = await classify_round(
            Asker(self.runtime, self.store, task.id, self.cfg.price_per_request_usd),
            self.cfg,
            self.gate,
            self.store,
            task,
            pending,
        )
        review = _store_review(self.store, task.id, tp.number, pending, decision)
        self.report.known += len(decision.known)
        self.report.touch(task.id)
        await self._apply(task, review)

    async def _record_feedback(
        self, tp: TrackedPull, task: TaskRecord, pull: GhPull
    ) -> None:
        found = await asyncio.to_thread(
            collect_findings,
            self.gh,
            self.repo,
            tp,
            self.since,
            self.comments,
            self.cfg.intake,
            pull.head_sha,
            store=self.store,
            with_checks=task.state is _S.PR_OPEN,
        )
        stored = {
            f["id"]: artifact.id
            for artifact in self.store.artifacts(task.id, ArtifactKind.PR_FEEDBACK)
            for f in artifact.body["findings"]
        }
        # Stored but never marked: a run died between the two. Mark, do not store twice.
        for f in found:
            if f.id in stored:
                _mark_feedback(self.store, task.tenant, self.repo, [f], stored[f.id])
        fresh = [f for f in found if f.id not in stored]
        if not fresh:
            return
        artifact = _store_feedback(self.store, task.id, tp.number, pull.head_sha, fresh)
        _mark_feedback(self.store, task.tenant, self.repo, fresh, artifact.id)
        self.report.new_findings += len(fresh)
        self.report.touch(task.id)

    def _unapplied_review(self, task_id: int) -> Artifact | None:
        review = self.store.latest_artifact(task_id, ArtifactKind.PR_REVIEW)
        if review is None:
            return None
        tenant = self.store.get(task_id).tenant
        if self.store.get_meta(f"intake:{tenant}:review:{review.id}") is not None:
            return None
        return review

    def _pending(self, task_id: int, head_sha: str) -> list[PRFinding]:
        """Unconsumed findings; a check of a commit that is no longer the head is stale."""
        consumed = {
            i
            for review in self.store.artifacts(task_id, ArtifactKind.PR_REVIEW)
            for i in review.body["consumed"]
        }
        pending: dict[str, PRFinding] = {}
        for artifact in self.store.artifacts(task_id, ArtifactKind.PR_FEEDBACK):
            for raw in artifact.body["findings"]:
                finding = PRFinding.model_validate(raw)
                stale = finding.source == "check" and (
                    artifact.body.get("head_sha") != head_sha
                )
                if finding.id not in consumed and not stale:
                    pending.setdefault(finding.id, finding)
        return list(pending.values())

    # -- apply a PR_REVIEW ----------------------------------------------------------

    async def _apply(self, task: TaskRecord, review: Artifact) -> None:
        decision = review.body["decision"]
        if decision["outcome"] == "reenter":
            self._reenter(task, review, decision)
        elif decision["outcome"] == "escalate":
            self._escalate(
                task,
                review,
                decision["category"],
                decision["reason"],
                options=decision["options"],
                question=decision["question"],
            )
        _mark_applied(self.store, task.tenant, review.id)
        self.report.touch(task.id)

    def _reenter(self, task: TaskRecord, review: Artifact, decision: dict) -> None:
        if not any(
            a.body.get("pr_review") == review.id
            for a in self.store.artifacts(task.id, ArtifactKind.LEAD)
        ):
            _store_send_back(self.store, task.id, review)
        for raw in decision["follow_ups"]:
            marker = f"intake:{task.tenant}:review:{review.id}:followup:{raw['id']}"
            if self.store.get_meta(marker) is None:
                # A crash between create and marker must not create the task twice.
                existing = next(
                    (
                        t
                        for t in self.store.list_tasks(task.tenant)
                        if t.parent_id == task.id
                        and t.source == "followup"
                        and t.title == raw["title"]
                    ),
                    None,
                )
                created = existing or self.store.create_task(
                    task.tenant,
                    raw["title"],
                    "followup",
                    f"task:{task.id}",
                    parent_id=task.id,
                )
                self.store.set_meta(marker, str(created.id))
        if self.store.get(task.id).state is _S.PR_OPEN:
            _move_to_triage(self.store, task.id)
        task = self.store.get(task.id)
        if task.id not in self.report.reentered:
            self.report.reentered.append(task.id)
        if task.state is _S.TRIAGE:
            self._stop_rules(task, review)

    def _stop_rules(self, task: TaskRecord, review: Artifact) -> None:
        budgets = effective_budgets(self.cfg, self.store, task.id)
        decision = apply_stop_rules(task, budgets)
        if decision is StopDecision.REASSIGN_SENIOR:
            self.store.reassign_to_senior(task.id)
        elif decision is StopDecision.ESCALATE_HUMAN:
            if task.cost_usd > budgets.per_task_usd:
                self._escalate(task, review, "stuck", "budget", options=_BUDGET_OPTIONS)
            else:
                self._escalate(
                    task,
                    review,
                    "stuck",
                    "round limit reached on PR feedback",
                    options=_ROUND_OPTIONS,
                    stop="rounds",
                )

    def _escalate(
        self,
        task: TaskRecord,
        review: Artifact,
        category: str,
        reason: str,
        *,
        options: tuple[str, ...] | list[str] = (),
        question: str = "",
        stop: str | None = None,
    ) -> None:
        """Once per review: a later escalation artifact means a run died before the move."""
        latest = self.store.latest_artifact(task.id, ArtifactKind.ESCALATION)
        if latest is not None and latest.id > review.id:
            if task.state is not _S.AWAITING_HUMAN:
                self.store.transition(
                    task.id, _S.AWAITING_HUMAN, Role.LEAD, f"{category}: {reason}"
                )
        else:
            write_escalation(
                self.store,
                task,
                category,
                reason,
                options=options,
                question=question,
                stop=stop,
            )
        self.report.escalated.append((task.id, category))


async def run_intake(
    cfg: TenantConfig,
    store: TaskStore,
    gh: GitHub,
    runtime: AgentRuntime,
    gate: MemoryGate,
    repo: str,
    *,
    now: datetime | None = None,
) -> IntakeReport:
    """One intake per repo at a time, across processes: a busy lease skips the run."""
    lease = f"intake:{cfg.tenant}:{repo}"
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    if not store.try_lease(lease, owner, INTAKE_LEASE_TTL_S):
        return IntakeReport(repo, busy=True)
    try:
        return await _run_intake(cfg, store, gh, runtime, gate, repo, now)
    finally:
        store.release_lease(lease, owner)


async def _run_intake(
    cfg: TenantConfig,
    store: TaskStore,
    gh: GitHub,
    runtime: AgentRuntime,
    gate: MemoryGate,
    repo: str,
    now: datetime | None,
) -> IntakeReport:
    now = now or datetime.now(UTC)
    report = IntakeReport(repo)
    key = f"intake:{cfg.tenant}:{repo}:cursor"
    tracked = tracked_pulls(store, cfg.tenant, repo)
    if not tracked:
        store.set_meta(key, now.isoformat())
        return report
    cursor = _read_cursor(store, key)
    since = cursor - CURSOR_OVERLAP if cursor else min(tp.opened_at for tp in tracked)
    intake = _Intake(cfg, store, gh, runtime, gate, repo, report, since)
    try:
        comments = await asyncio.to_thread(gh.review_comments, repo, since)
    except GitHubError as exc:
        report.errors.append(f"cannot read review comments: {exc}")
        report.github_failed = True
        return report
    for c in comments:
        intake.comments.setdefault(c.pr_number, []).append(c)
    for tp in tracked:
        report.pulls += 1
        try:
            await intake.pull(tp)
        except GitHubError as exc:
            report.errors.append(f"#{tp.task.id} PR #{tp.number}: {exc}")
            report.github_failed = True
        except (ReplyInvalid, AgentStuck) as exc:
            reason = exc.reason if isinstance(exc, AgentStuck) else str(exc)
            report.errors.append(f"#{tp.task.id} agent step failed: {reason}")
        except InvalidTransition:
            report.errors.append(
                f"#{tp.task.id} changed concurrently, retried next run"
            )
        except InvalidRef as exc:
            report.errors.append(f"#{tp.task.id} PR #{tp.number}: {exc}")
    if report.ok:
        store.set_meta(key, now.isoformat())
    return report
