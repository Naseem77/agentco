"""The task loop: drives one task through the state machine, in process.

Every step re-reads the task and every state change is a compare-and-set
transition, so a crashed run resumes where it stopped and two runs of one task
cannot both advance it. Nothing here merges. A PR is opened only from a lead
approval or a human `approve`; everything else that needs a person goes to
AWAITING_HUMAN with an ESCALATION artifact.
"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentco.agents.checks import CheckOutcome, run_checks
from agentco.agents.engineer import (
    EngineerBrief,
    EngineerFailed,
    EngineerReply,
    run_engineer,
)
from agentco.agents.lead import LeadDecision, run_lead
from agentco.agents.models import role_model
from agentco.agents.pm import run_pm
from agentco.agents.qa import QAReply, run_qa
from agentco.agents.replies import Finding, PMReply
from agentco.agents.review import ReviewReply, run_review
from agentco.agents.triage import run_triage
from agentco.config import Budgets, TenantConfig
from agentco.github.client import GitHubWriter
from agentco.github.errors import GitHubError
from agentco.llm.replies import AgentStuck, Asker, ReplyInvalid
from agentco.llm.runtime import AgentRuntime
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.runner import CommandRunner
from agentco.sandbox.worktree import (
    GitError,
    Worktree,
    changed_lines,
    count_commits,
    create_worktree,
    diff,
    head_ancestor,
    head_sha,
    is_ancestor,
    push_branch,
    rebase_onto,
    refresh_main,
)
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.pr import pr_body, pr_title
from agentco.workflow.preflight import CheckResult, Preflight, PreflightReport
from agentco.workflow.states import TaskState
from agentco.workflow.stop_rules import StopDecision, apply_stop_rules

_S = TaskState
_DONE = frozenset({_S.PR_OPEN, _S.MERGED, _S.CANCELLED})
_MAX_STEPS = 500
_BUDGET_OPTIONS = ["add budget", "reject"]
_ROUND_OPTIONS = ["answer: one more round", "reject"]
_NO_LEAD = "no lead decision to open a PR from"


class StepFailed(RuntimeError):
    """A step with no safe place to escalate to failed; the task stays in `state`."""

    def __init__(self, state: TaskState, reason: str) -> None:
        super().__init__(f"{state.value}: {reason}")
        self.state = state
        self.reason = reason


@dataclass
class TaskContext:
    cfg: TenantConfig
    store: TaskStore
    runtime: AgentRuntime
    gate: MemoryGate
    github: GitHubWriter
    repo: str
    repo_root: Path
    contract: RepoContract
    runner: CommandRunner
    main: str = "main"


class Orchestrator:
    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx

    async def run(self, task_id: int) -> TaskRecord:
        """Advance a task until it needs a human, opens its PR, or is cancelled."""
        return await _Run(self.ctx, task_id).loop()


async def run_many(
    jobs: Sequence[tuple[TaskContext, int]], limit: int
) -> list[TaskRecord | BaseException]:
    """Run tasks concurrently, at most `limit` at once; failures are returned."""
    gate = asyncio.Semaphore(limit)

    async def one(ctx: TaskContext, task_id: int) -> TaskRecord:
        async with gate:
            return await Orchestrator(ctx).run(task_id)

    return list(
        await asyncio.gather(
            *(one(ctx, task_id) for ctx, task_id in jobs), return_exceptions=True
        )
    )


def _agent_error(exc: Exception) -> str:
    return exc.reason if isinstance(exc, AgentStuck) else str(exc)


def _lead_from(body: dict) -> LeadDecision:
    return LeadDecision(
        outcome=body["outcome"],
        category=body["category"],
        findings_to_fix=tuple(Finding(**f) for f in body["findings_to_fix"]),
        follow_ups=tuple(Finding(**f) for f in body["follow_ups"]),
        reason=body["reason"],
        summary=body["summary"],
        comments=tuple(body["comments"]),
        question=body["question"],
        options=tuple(body["options"]),
    )


def _report_from(body: dict) -> PreflightReport:
    checks = tuple(CheckResult(c["name"], c["ok"], c["detail"]) for c in body["checks"])
    return PreflightReport(body["ok"], checks, body["conflict"])


def _id(artifact: Artifact | None) -> int:
    return artifact.id if artifact else 0


@dataclass
class _Run:
    ctx: TaskContext
    task_id: int
    asker: Asker = field(init=False)
    preflight: Preflight = field(init=False)

    def __post_init__(self) -> None:
        cfg = self.ctx.cfg
        self.store = self.ctx.store
        self.asker = Asker(
            self.ctx.runtime, self.store, self.task_id, cfg.price_per_request_usd
        )
        self.preflight = Preflight(
            repo=self.ctx.repo,
            contract=self.ctx.contract,
            runner=self.ctx.runner,
            limits=cfg.loop,
            memory=self.ctx.gate.for_role(Role.REVIEWER),
            asker=self.asker,
            reviewer_model=role_model(cfg, Role.REVIEWER).model,
        )

    async def loop(self) -> TaskRecord:
        handlers = {
            _S.INTAKE: self._intake,
            _S.PM: self._pm,
            _S.TRIAGE: self._triage,
            _S.ENGINEERING: self._engineering,
            _S.PREFLIGHT: self._preflight,
            _S.INTERNAL_REVIEW: self._review,
            _S.LEAD_DECISION: self._lead,
        }
        for _ in range(_MAX_STEPS):
            task = self.task()
            if task.state is _S.AWAITING_HUMAN:
                if not await self._resume(task):
                    return task
                continue
            handler = handlers.get(task.state)
            if task.state in _DONE or handler is None:
                return task
            await handler(task)
        raise StepFailed(self.task().state, f"no progress after {_MAX_STEPS} steps")

    # -- shared helpers --------------------------------------------------

    def task(self) -> TaskRecord:
        return self.store.get(self.task_id)

    def _key(self, name: str) -> str:
        return f"task:{self.task_id}:{name}"

    def _meta(self, name: str) -> str | None:
        return self.store.get_meta(self._key(name))

    def _set(self, name: str, value: str) -> None:
        self.store.set_meta(self._key(name), value)

    def _latest(self, kind: ArtifactKind) -> Artifact | None:
        return self.store.latest_artifact(self.task_id, kind)

    def _go(self, to: TaskState, actor: Role, reason: str) -> TaskRecord:
        return self.store.transition(self.task_id, to, actor, reason)

    def _budgets(self) -> Budgets:
        budgets = self.ctx.cfg.budgets
        extra = float(self._meta("extra_budget") or 0)
        rounds = self._meta("max_rounds")
        return budgets.model_copy(
            update={
                "per_task_usd": budgets.per_task_usd + extra,
                "max_rounds": int(rounds) if rounds else budgets.max_rounds,
            }
        )

    def _issue(self) -> PMReply:
        artifact = self._latest(ArtifactKind.ISSUE)
        if artifact is None:
            raise StepFailed(self.task().state, "no issue artifact")
        return PMReply.model_validate(artifact.body)

    async def _escalate(
        self,
        category: str,
        reason: str,
        *,
        options: Sequence[str] = (),
        question: str = "",
        stop: str | None = None,
        from_state: str | None = None,
        open_pr: bool = False,
        actor: Role = Role.LEAD,
    ) -> None:
        task = self.task()
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
            body["open_pr"] = True  # only these escalations let `approve` open a PR
        self.store.put_artifact(self.task_id, ArtifactKind.ESCALATION, actor, body)
        if task.state is not _S.AWAITING_HUMAN:
            self._go(_S.AWAITING_HUMAN, actor, f"{category}: {reason}")

    async def _escalate_if_over_budget(self, task: TaskRecord) -> bool:
        if task.cost_usd <= self._budgets().per_task_usd:
            return False
        await self._escalate("stuck", "budget", options=_BUDGET_OPTIONS)
        return True

    async def _stop_rules(self) -> None:
        """Run after every move back to TRIAGE: may reassign, escalate or go on."""
        task = self.task()
        budgets = self._budgets()
        decision = apply_stop_rules(task, budgets)
        if decision is StopDecision.REASSIGN_SENIOR:
            self.store.reassign_to_senior(self.task_id)
        elif decision is StopDecision.ESCALATE_HUMAN:
            if task.cost_usd > budgets.per_task_usd:
                await self._escalate("stuck", "budget", options=_BUDGET_OPTIONS)
            else:
                await self._escalate(
                    "stuck",
                    f"round limit reached ({budgets.max_rounds})",
                    options=_ROUND_OPTIONS,
                    stop="rounds",
                )

    async def _worktree(self) -> Worktree:
        base = self._meta("base")
        if base is None:
            base = await self._refresh_base()
        return await asyncio.to_thread(
            create_worktree, self.ctx.repo_root, self.task_id, base
        )

    async def _refresh_base(self) -> str:
        base = await asyncio.to_thread(refresh_main, self.ctx.repo_root, self.ctx.main)
        self._set("base", base)
        return base

    async def _sync_branch(self, wt: Worktree, base: str) -> str | None:
        """Rebase onto `base`; return the last-reviewed tip as it is after the rebase.

        The recorded tip names a commit the rebase rewrites, so it is remapped by
        its distance from HEAD and stored only once the rebase has happened.
        """
        since = self._meta("fix_since")
        behind: int | None = None
        if since is not None and await asyncio.to_thread(is_ancestor, wt, since):
            behind = await asyncio.to_thread(count_commits, wt, since)
        if not await asyncio.to_thread(rebase_onto, wt, base):
            return since  # the pre-flight rebase check reports the conflict
        if behind is None:
            return None
        total = await asyncio.to_thread(count_commits, wt, base)
        remapped = await asyncio.to_thread(head_ancestor, wt, min(behind, total))
        self._set("fix_since", remapped)
        return remapped

    # -- states ----------------------------------------------------------

    async def _intake(self, task: TaskRecord) -> None:
        self._go(_S.PM, Role.PM, "intake")

    async def _pm(self, task: TaskRecord) -> None:
        request = self._latest(ArtifactKind.REQUEST)
        text = str(request.body.get("body", "")) if request else ""
        try:
            await run_pm(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                self.store,
                task,
                text or task.title,
            )
        except (ReplyInvalid, AgentStuck) as exc:
            raise StepFailed(_S.PM, _agent_error(exc)) from exc
        self._go(_S.TRIAGE, Role.PM, "issue written")

    def _human_ok(self) -> bool:
        """A human already allowed this unknown or new area to be worked on."""
        escalation = self._latest(ArtifactKind.ESCALATION)
        decision = self._latest(ArtifactKind.HUMAN_DECISION)
        return bool(
            escalation
            and decision
            and decision.id > escalation.id
            and escalation.body.get("from_state") == "triage"
            and escalation.body.get("category") == "new"
            and decision.body.get("decision") in ("approve", "answer")
        )

    async def _triage(self, task: TaskRecord) -> None:
        if await self._escalate_if_over_budget(task):
            return
        try:
            decision = await run_triage(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                self.store,
                task,
                self._issue(),
                human_ok=self._human_ok(),
            )
        except (ReplyInvalid, AgentStuck) as exc:
            await self._escalate("stuck", _agent_error(exc), actor=Role.TRIAGE)
            return
        if decision.escalate:
            await self._escalate(decision.escalate, decision.reason, actor=Role.TRIAGE)
            return
        if not task.reassigned:
            self.store.assign(self.task_id, decision.route)
        self._set("fast_lane", "1" if decision.fast_lane else "0")
        self._go(_S.ENGINEERING, Role.TRIAGE, decision.reason)

    def _human_input(self) -> tuple[str, str] | None:
        """(note, from_state of the escalation answered) not yet seen by an engineer."""
        decision = self._latest(ArtifactKind.HUMAN_DECISION)
        if decision is None or decision.id < _id(self._latest(ArtifactKind.ENGINEER)):
            return None
        note = str(decision.body.get("note", "")).strip()
        if not note or decision.body.get("decision") not in ("approve", "answer"):
            return None
        earlier = [
            e
            for e in self.store.artifacts(self.task_id, ArtifactKind.ESCALATION)
            if e.id < decision.id
        ]
        origin = earlier[-1].body.get("from_state", "") if earlier else ""
        return note, origin

    def _brief(self, task: TaskRecord) -> EngineerBrief:
        """Built from lead, QA/review (via the lead), pre-flight and human input only."""
        findings: list[Finding] = []
        lead = self._latest(ArtifactKind.LEAD)
        if lead and lead.body.get("outcome") == "send_back":
            findings = list(_lead_from(lead.body).findings_to_fix)
        human_note = ""
        human = self._human_input()
        if human:
            note, origin = human
            if origin == "lead_decision":
                findings.append(
                    Finding(
                        id="human-1",
                        severity="major",
                        kind="bug",
                        title="Human guidance",
                        detail=note,
                    )
                )
            else:
                human_note = note
        failures: tuple[str, ...] = ()
        pre = self._latest(ArtifactKind.PREFLIGHT)
        if pre and pre.id > _id(self._latest(ArtifactKind.ENGINEER)):
            report = _report_from(pre.body)
            failures = tuple(f"{c.name}: {c.detail}" for c in report.failures())
        triage = self._latest(ArtifactKind.TRIAGE)
        self._set("findings_addressed", json.dumps([f.model_dump() for f in findings]))
        return EngineerBrief(
            issue=self._issue(),
            findings_to_fix=tuple(findings),
            preflight_failures=failures,
            require_invariant=bool(triage and triage.body.get("require_invariant")),
            round=task.round,
            human_note=human_note,
        )

    async def _engineering(self, task: TaskRecord) -> None:
        if await self._escalate_if_over_budget(task):
            return
        wt = await self._worktree()
        role = task.assignee or Role.JUNIOR
        brief = self._brief(task)
        try:
            await run_engineer(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                self.store,
                task,
                role,
                wt,
                self.ctx.contract,
                brief,
            )
        except EngineerFailed as exc:
            # the reason may carry engineer reasoning: it goes only to the human
            if exc.stuck and role is Role.JUNIOR and not task.reassigned:
                self.store.reassign_to_senior(self.task_id)
            else:
                await self._escalate("stuck", exc.reason, actor=role)
            return
        self._go(_S.PREFLIGHT, role, "engineer committed")

    async def _run_preflight(
        self,
        wt: Worktree,
        base: str,
        *,
        fix_since: str | None,
        final: bool,
        lead_comments: list[str],
    ) -> tuple[PreflightReport, bool]:
        try:
            report = await self.preflight.run(
                wt,
                base,
                fix_since=fix_since,
                final=final,
                lead_comments=lead_comments,
            )
        except (ReplyInvalid, AgentStuck) as exc:
            failed = CheckResult(
                "rules", False, f"rules check failed: {_agent_error(exc)}"
            )
            return PreflightReport(False, (failed,), False), True
        return report, False

    def _lead_comments(self) -> list[str]:
        lead = self._latest(ArtifactKind.LEAD)
        if lead and lead.body.get("outcome") == "send_back":
            return [str(c) for c in lead.body.get("comments", [])]
        return []

    async def _preflight(self, task: TaskRecord) -> None:
        wt = await self._worktree()
        base = await self._refresh_base()
        wt = Worktree(wt.path, wt.branch, base)
        since = await self._sync_branch(wt, base)
        report, agent_error = await self._run_preflight(
            wt,
            base,
            fix_since=since,
            final=False,
            lead_comments=self._lead_comments(),
        )
        self.store.put_artifact(
            self.task_id, ArtifactKind.PREFLIGHT, Role.REVIEWER, report.as_dict()
        )
        role = task.assignee or Role.JUNIOR
        if report.ok:
            self._go(_S.INTERNAL_REVIEW, Role.REVIEWER, "pre-flight passed")
            return
        floor = _id(self._latest(ArtifactKind.TRIAGE))
        attempts = sum(
            1
            for a in self.store.artifacts(self.task_id, ArtifactKind.PREFLIGHT)
            if a.id > floor
        )
        if (
            report.conflict
            or agent_error
            or attempts >= self.ctx.cfg.loop.preflight_attempts
        ):
            self._go(_S.TRIAGE, role, "pre-flight failed: round over")
            await self._stop_rules()
        else:
            self._go(_S.ENGINEERING, role, "pre-flight failed: fix and retry")

    async def _review(self, task: TaskRecord) -> None:
        wt = await self._worktree()
        base = self._meta("base") or wt.base
        issue = self._issue()
        addressed = [
            Finding(**f) for f in json.loads(self._meta("findings_addressed") or "[]")
        ]
        fast_lane = self._meta("fast_lane") == "1"
        outcome = await run_checks(
            run_review(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                wt,
                base,
                issue,
                fix_since=self._meta("fix_since"),
                findings_addressed=addressed,
                fast_lane=fast_lane,
            ),
            run_qa(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                wt,
                base,
                self.ctx.contract,
                self.ctx.runner,
                task.round,
                issue,
                fast_lane=fast_lane,
            ),
        )
        errors = {e.split(":", 1)[0]: e for e in outcome.errors}
        for kind, role, result in (
            (ArtifactKind.REVIEW, Role.REVIEWER, outcome.review),
            (ArtifactKind.QA, Role.QA, outcome.qa),
        ):
            body = (
                result.model_dump()
                if result is not None
                else {"error": errors.get(kind.value, f"{kind.value}: no result")}
            )
            self.store.put_artifact(self.task_id, kind, role, body)
        self._go(_S.LEAD_DECISION, Role.LEAD, "checks done")
        self._set("fix_since", await asyncio.to_thread(head_sha, wt))

    def _checks(self) -> CheckOutcome:
        errors: list[str] = []
        parsed: dict[ArtifactKind, ReviewReply | QAReply | None] = {}
        for kind, model in (
            (ArtifactKind.REVIEW, ReviewReply),
            (ArtifactKind.QA, QAReply),
        ):
            artifact = self._latest(kind)
            if artifact is None or "error" in artifact.body:
                errors.append(
                    artifact.body["error"] if artifact else f"{kind.value}: missing"
                )
                parsed[kind] = None
            else:
                parsed[kind] = model.model_validate(artifact.body)
        return CheckOutcome(
            parsed[ArtifactKind.REVIEW],  # type: ignore[arg-type]
            parsed[ArtifactKind.QA],  # type: ignore[arg-type]
            tuple(errors),
        )

    def _engineer_reply(self) -> EngineerReply:
        artifact = self._latest(ArtifactKind.ENGINEER)
        if artifact is None:
            raise StepFailed(self.task().state, "no engineer artifact")
        return EngineerReply.model_validate(artifact.body)

    async def _lead(self, task: TaskRecord) -> None:
        if await self._escalate_if_over_budget(task):
            return
        wt = await self._worktree()
        base = self._meta("base") or wt.base
        pre = self._latest(ArtifactKind.PREFLIGHT)
        if pre is None:
            raise StepFailed(_S.LEAD_DECISION, "no pre-flight artifact")
        try:
            decision = await run_lead(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                self.store,
                task,
                issue=self._issue(),
                diff_text=await asyncio.to_thread(diff, wt, base),
                engineer=self._engineer_reply(),
                preflight=_report_from(pre.body),
                checks=self._checks(),
                changed_lines=await asyncio.to_thread(changed_lines, wt, base),
                fast_lane=self._meta("fast_lane") == "1",
            )
        except (ReplyInvalid, AgentStuck) as exc:
            await self._escalate("stuck", f"lead failed: {_agent_error(exc)}")
            return
        if decision.outcome == "approve":
            await self._open_pr()
        elif decision.outcome == "send_back":
            self._go(_S.TRIAGE, Role.LEAD, decision.reason)
            await self._stop_rules()
        else:
            await self._escalate(
                decision.category,
                decision.reason,
                options=decision.options,
                question=decision.question,
                open_pr=True,
            )

    async def _open_pr(self) -> None:
        """Push and open (or update) the PR. Only reached from an approval."""
        task = self.task()
        lead = self._current_lead()
        if lead is None:
            await self._escalate("stuck", _NO_LEAD, from_state=_S.LEAD_DECISION.value)
            return
        try:
            wt = await self._worktree()
            base = await self._refresh_base()
            wt = Worktree(wt.path, wt.branch, base)
            await self._sync_branch(wt, base)
            report, _ = await self._run_preflight(
                wt, base, fix_since=None, final=True, lead_comments=[]
            )
            if not report.ok:
                self.store.put_artifact(
                    self.task_id,
                    ArtifactKind.PREFLIGHT,
                    Role.REVIEWER,
                    report.as_dict(),
                )
                self._go(_S.TRIAGE, Role.LEAD, "final pre-flight failed")
                await self._stop_rules()
                return
            body = self._pr_body(task, lead, report)
            await asyncio.to_thread(push_branch, wt)
            pull = await asyncio.to_thread(
                self.ctx.github.find_open_pull, self.ctx.repo, wt.branch
            )
            if pull is None:
                pull = await asyncio.to_thread(
                    self.ctx.github.create_pull,
                    self.ctx.repo,
                    wt.branch,
                    self.ctx.main,
                    pr_title(task, self._issue()),
                    body,
                )
            else:
                await asyncio.to_thread(
                    self.ctx.github.edit_pull, self.ctx.repo, pull.number, body
                )
        except (GitError, GitHubError) as exc:
            await self._escalate(
                "stuck",
                f"push/gh failed: {exc}",
                from_state=_S.LEAD_DECISION.value,
                open_pr=True,
            )
            return
        self.store.put_artifact(
            self.task_id,
            ArtifactKind.PR,
            Role.LEAD,
            {"number": pull.number, "url": pull.url},
        )
        self._go(_S.PR_OPEN, Role.LEAD, f"pull request #{pull.number}")

    def _current_lead(self) -> Artifact | None:
        """The lead's approve/escalate decision on the engineer's latest work."""
        lead = self._latest(ArtifactKind.LEAD)
        if (
            lead is None
            or lead.body.get("outcome") not in ("approve", "escalate")
            or lead.id < _id(self._latest(ArtifactKind.ENGINEER))
        ):
            return None
        return lead

    def _source_issue(self, task: TaskRecord) -> int | None:
        ref = task.source_ref or ""
        return int(ref) if task.source == "issue" and ref.isdecimal() else None

    def _pr_body(
        self, task: TaskRecord, lead: Artifact, report: PreflightReport
    ) -> str:
        return pr_body(
            task,
            self._issue(),
            _lead_from(lead.body),
            self._checks().qa,
            report,
            self._source_issue(task),
        )

    # -- human decisions -------------------------------------------------

    async def _resume(self, task: TaskRecord) -> bool:
        """Act on a human decision made since the last escalation; False if none."""
        escalation = self._latest(ArtifactKind.ESCALATION)
        decision = self._latest(ArtifactKind.HUMAN_DECISION)
        if decision is None or decision.id < _id(escalation):
            return False
        choice = decision.body.get("decision")
        origin = escalation.body.get("from_state") if escalation else None
        if choice == "reject":
            self._go(_S.CANCELLED, Role.HUMAN, "rejected by a human")
            return True
        if choice not in ("approve", "answer") or escalation is None:
            return False
        if escalation.body.get("stop") == "rounds":
            self._set("max_rounds", str(self._budgets().max_rounds + 1))
            self._go(_S.TRIAGE, Role.HUMAN, "one more round granted")
        elif escalation.body.get("open_pr") and choice == "approve":
            await self._open_pr()
        elif origin == "engineering":
            self._go(_S.ENGINEERING, Role.HUMAN, f"human {choice}")
        else:
            self._go(_S.TRIAGE, Role.HUMAN, f"human {choice}")
        return True
