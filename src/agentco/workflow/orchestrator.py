"""The task loop: drives one task through the state machine, in process.

Every step re-reads the task and every state change is a compare-and-set
transition, so a crashed run resumes where it stopped and two runs of one task
cannot both advance it. Nothing here merges. A PR is opened only from a lead
approval or a human `approve`; everything else that needs a person goes to
AWAITING_HUMAN with an ESCALATION artifact.
"""

import asyncio
import json
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter

from agentco.agents.architect import ADRReply, run_architect
from agentco.agents.checks import CheckOutcome, run_checks
from agentco.agents.engineer import (
    EngineerBrief,
    EngineerFailed,
    EngineerReply,
    run_engineer,
)
from agentco.agents.lead import ChildSummary, LeadDecision, review_parent, run_lead
from agentco.agents.models import role_model
from agentco.agents.pm import run_pm
from agentco.agents.qa import QAReply, run_qa
from agentco.agents.replies import Finding, PMReply
from agentco.agents.review import ReviewReply, run_review
from agentco.agents.triage import run_triage
from agentco.config import Budgets, TenantConfig
from agentco.github.client import GitHubWriter
from agentco.github.errors import GitHubError
from agentco.github.models import PullRef
from agentco.learning.followup import FOLLOWUP_SOURCE, write_test_files
from agentco.llm.replies import AgentStuck, Asker, ReplyInvalid
from agentco.llm.runtime import AgentRuntime
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.pool import PoolSetupFailed, WorktreePool, run_setup
from agentco.sandbox.runner import CommandRunner
from agentco.sandbox.worktree import (
    GitError,
    Worktree,
    _run,
    branch_name,
    changed_lines,
    commit_all,
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
from agentco.workflow.escalation import effective_budgets, write_escalation
from agentco.workflow.onboard import ONBOARD_SOURCE
from agentco.workflow.pr import pr_body, pr_title
from agentco.workflow.preflight import CheckResult, Preflight, PreflightReport
from agentco.workflow.states import InvalidTransition, TaskState
from agentco.workflow.stop_rules import StopDecision, apply_stop_rules
from agentco.workflow.tree import (
    ADR_SOURCE,
    adr_children,
    cancel_open_children,
    spawn_children,
)

_S = TaskState
_DONE = frozenset({_S.PR_OPEN, _S.MERGED, _S.CANCELLED})
_MAX_STEPS = 500
_BUDGET_OPTIONS = ["add budget", "reject"]
_ROUND_OPTIONS = ["answer: one more round", "reject"]
_NO_LEAD = "no lead decision to open a PR from"
_NO_QA_TESTS = "the QA test files in the request are missing or unsafe"


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
    pool: WorktreePool | None = None


class Orchestrator:
    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx

    async def run(self, task_id: int) -> TaskRecord:
        """Advance a task until it needs a human, opens its PR, or is cancelled."""
        try:
            return await _Run(self.ctx, task_id).loop()
        except InvalidTransition:
            # intake may see a human merge or close the PR while a step is running
            task = self.ctx.store.get(task_id)
            if task.state in (TaskState.MERGED, TaskState.CANCELLED):
                return task
            raise


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


_REPO_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _repo_lock(repo_root: Path) -> asyncio.Lock:
    """One lock per checkout (and event loop), shared by every run on it."""
    locks = _REPO_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(Path(repo_root).resolve(), asyncio.Lock())


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
        # an onboarding task is not code work: its approval would write a file
        if self.task().source == ONBOARD_SOURCE:
            raise StepFailed(self.task().state, "onboarding runs via agentco onboard")
        handlers = {
            _S.INTAKE: self._intake,
            _S.PM: self._pm,
            _S.TRIAGE: self._triage,
            _S.ARCHITECT: self._architect,
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
            if task.state is _S.WAITING_CHILDREN:
                if not await self._children(task):
                    return task
                continue
            if task.state is _S.PR_OPEN and self._answer_interrupted():
                await self._start_answer_round()
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
        return effective_budgets(self.ctx.cfg, self.store, self.task_id)

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
        step: str | None = None,
    ) -> None:
        write_escalation(
            self.store,
            self.task(),
            category,
            reason,
            options=options,
            question=question,
            stop=stop,
            from_state=from_state,
            open_pr=open_pr,
            actor=actor,
            step=step,
        )

    def _is_pr_round(self) -> bool:
        return self._latest(ArtifactKind.PR) is not None

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
        async with _repo_lock(self.ctx.repo_root):
            return await self._acquire_worktree(base)

    async def _acquire_worktree(self, base: str) -> Worktree:
        """The task's worktree: its pool entry, else a plain one (setup runs there).

        Only a task that is starting fresh may claim a pool entry. One whose
        worktree, local branch or PR exists already is resuming, and an entry
        starts from `main`, not from that work.
        """
        ctx, pool = self.ctx, self.ctx.pool
        if pool is not None:
            owned = await asyncio.to_thread(pool.owned_by, self.task_id, base)
            if owned is not None:
                await self._note_pool_hit(pool)
                return owned
        if pool is not None and not await self._resuming():
            claimed = await asyncio.to_thread(pool.claim, self.task_id, base)
            if claimed is not None:
                wt, entry = claimed
                self._set("setup", "pool")
                self.store.set_meta(self._pool_key("hit"), str(entry.setup_s))
                return wt
        wt = await asyncio.to_thread(create_worktree, ctx.repo_root, self.task_id, base)
        await self._setup(wt)
        return wt

    async def _resuming(self) -> bool:
        default_path = self.ctx.repo_root / ".agentco" / "worktrees" / str(self.task_id)
        return (
            default_path.exists()
            or self._is_pr_round()
            or await asyncio.to_thread(self._local_branch_exists)
        )

    def _pool_key(self, kind: str) -> str:
        return f"pool:{self.ctx.cfg.tenant}:{kind}:{self.task_id}"

    def _local_branch_exists(self) -> bool:
        ref = f"refs/heads/{branch_name(self.task_id)}"
        proc = _run(self.ctx.repo_root, "rev-parse", "--verify", "--quiet", ref)
        return proc.returncode == 0

    async def _note_pool_hit(self, pool: WorktreePool) -> None:
        """Record a claim whose bookkeeping a crash cut off."""
        if self._meta("setup") is not None:
            return
        entry = await asyncio.to_thread(pool.entry_of, self.task_id)
        self._set("setup", "pool")
        if entry is not None:
            self.store.set_meta(self._pool_key("hit"), str(entry.setup_s))

    async def _setup(self, wt: Worktree) -> None:
        setup = self.ctx.contract.setup
        if setup is None or self._meta("setup") == "done":
            return  # "pool" here means the claimed entry is gone: set up this one
        timeout = self.ctx.cfg.pool.setup_timeout_s
        try:
            seconds = await asyncio.to_thread(
                run_setup, self.ctx.runner, self.ctx.contract, wt.path, timeout
            )
        except PoolSetupFailed as e:
            raise StepFailed(self.task().state, f"setup failed: {e}") from e
        self._set("setup", "done")
        self.store.set_meta(self._pool_key("miss"), str(seconds))

    async def _refresh_base(self) -> str:
        async with _repo_lock(self.ctx.repo_root):
            base = await asyncio.to_thread(
                refresh_main, self.ctx.repo_root, self.ctx.main
            )
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
                known_ok=task.source == ADR_SOURCE,  # the human approved the design
                allow_architect=task.parent_id is None and not self._is_pr_round(),
            )
        except (ReplyInvalid, AgentStuck) as exc:
            await self._escalate("stuck", _agent_error(exc), actor=Role.TRIAGE)
            return
        if decision.escalate:
            await self._escalate(decision.escalate, decision.reason, actor=Role.TRIAGE)
            return
        if decision.route is Role.ARCHITECT:
            self.store.assign(self.task_id, Role.ARCHITECT)
            self._go(_S.ARCHITECT, Role.TRIAGE, decision.reason)
            return
        if not task.reassigned:
            self.store.assign(self.task_id, decision.route)
        # review feedback on an open PR is always reviewed as new code
        fast_lane = decision.fast_lane and not self._is_pr_round()
        self._set("fast_lane", "1" if fast_lane else "0")
        self._go(_S.ENGINEERING, Role.TRIAGE, decision.reason)

    async def _architect(self, task: TaskRecord) -> None:
        if await self._escalate_if_over_budget(task):
            return
        floor = _id(self._latest(ArtifactKind.HUMAN_DECISION))
        adr = self._latest(ArtifactKind.ADR)
        if adr is None or adr.id < floor:
            human = self._human_input()
            note = human[0] if human and human[1] == _S.ARCHITECT.value else ""
            try:
                _, adr = await run_architect(
                    self.asker,
                    self.ctx.cfg,
                    self.ctx.gate,
                    self.store,
                    task,
                    self._issue(),
                    repo_root=self.ctx.repo_root,
                    human_note=note,
                )
            except (ReplyInvalid, AgentStuck) as exc:
                await self._escalate("stuck", _agent_error(exc), actor=Role.ARCHITECT)
                return
        # an ADR written before a crash is escalated as it is, not rewritten
        sub_tasks = adr.body["adr"]["sub_tasks"]
        await self._escalate(
            "new",
            f"ADR: {adr.body['adr']['title']} ({len(sub_tasks)} sub-tasks)",
            question=(
                f"Approve this design and its sub-tasks? See `agentco adr {task.id}`."
            ),
            options=("approve", "answer: change the design", "reject"),
            actor=Role.ARCHITECT,
            step="adr",
        )

    async def _approve_adr(self, escalation: Artifact) -> None:
        """Children come from the ADR the human saw: the latest one before the escalation."""
        adrs = [
            a
            for a in self.store.artifacts(self.task_id, ArtifactKind.ADR)
            if a.id < escalation.id
        ]
        if not adrs:
            raise StepFailed(self.task().state, "no ADR to approve")
        spawned = spawn_children(self.store, self.task(), adrs[-1])
        self._set("adr", str(adrs[-1].id))
        self._go(
            _S.WAITING_CHILDREN, Role.HUMAN, f"ADR approved: {len(spawned)} sub-tasks"
        )

    def _accepted(self) -> set[int]:
        return set(json.loads(self._meta("accepted") or "[]"))

    def _child_summary(self, child: TaskRecord) -> ChildSummary:
        pr = self.store.latest_artifact(child.id, ArtifactKind.PR)
        lead = self.store.latest_artifact(child.id, ArtifactKind.LEAD)
        return ChildSummary(
            child.id,
            child.title,
            child.state.value,
            str(pr.body.get("url", "")) if pr else "",
            str(lead.body.get("summary", "")) if lead else "",
        )

    def _parent_note(self) -> str:
        """A human's answer to the last whole-tree review, until the next escalation."""
        escalation = self._latest(ArtifactKind.ESCALATION)
        decision = self._latest(ArtifactKind.HUMAN_DECISION)
        if (
            escalation is None
            or decision is None
            or decision.id < escalation.id
            or escalation.body.get("step") != "parent_review"
        ):
            return ""
        return str(decision.body.get("note", "")).strip()

    async def _children(self, task: TaskRecord) -> bool:
        """Act on the children of a waiting parent; False while there is nothing to do."""
        kids = adr_children(self.store, task)
        accepted = self._accepted()
        failed = [k for k in kids if k.state is _S.CANCELLED and k.id not in accepted]
        if failed:
            await self._escalate(
                "stuck",
                "sub-task(s) " + ", ".join(f"#{k.id}" for k in failed) + " cancelled",
                options=(
                    "approve: continue without them",
                    "reject: cancel the whole task",
                ),
                step="child_failed",
            )
            return True
        done = all(
            k.state is _S.MERGED or (k.state is _S.CANCELLED and k.id in accepted)
            for k in kids
        )
        if not kids or not done:
            return False
        if await self._escalate_if_over_budget(task):
            return True
        adr_id = self._meta("adr")
        adr = next(
            (
                a
                for a in self.store.artifacts(self.task_id, ArtifactKind.ADR)
                if str(a.id) == adr_id
            ),
            None,
        )
        if adr is None:
            raise StepFailed(_S.WAITING_CHILDREN, "no approved ADR to review against")
        try:
            reply = await review_parent(
                self.asker,
                self.ctx.cfg,
                self.ctx.gate,
                self.store,
                task,
                adr=ADRReply.model_validate(adr.body["adr"]),
                children=[self._child_summary(k) for k in kids],
                human_note=self._parent_note(),
            )
        except (ReplyInvalid, AgentStuck) as exc:
            await self._escalate(
                "stuck", f"lead failed: {_agent_error(exc)}", step="parent_review"
            )
            return True
        if (
            reply.approve
            and reply.confidence >= self.ctx.cfg.thresholds.lead_min_confidence
        ):
            self._go(
                _S.MERGED, Role.LEAD, "all sub-tasks merged; lead approved the whole"
            )
        else:
            await self._escalate(
                "tradeoff",
                reply.reason,
                question="Do the merged sub-tasks deliver the design?",
                options=("approve", "answer: what is missing", "reject"),
                step="parent_review",
            )
        return True

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
        if origin == _S.LEAD_DECISION.value and decision.body["decision"] != "answer":
            return (
                None  # an approve of the lead's work is not guidance for the next round
            )
        return note, origin

    def _brief(self, task: TaskRecord) -> EngineerBrief:
        """Built from lead, QA/review (via the lead), pre-flight and human input only."""
        findings: list[Finding] = []
        untrusted: frozenset[str] = frozenset()
        lead = self._latest(ArtifactKind.LEAD)
        if lead and lead.body.get("outcome") == "send_back":
            findings = list(_lead_from(lead.body).findings_to_fix)
            if lead.body.get("source") == "pr":
                untrusted = frozenset(f.id for f in findings)
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
            elif origin != "pr_open":  # a PR answer is already a LEAD finding
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
            require_invariant=self._require_invariant(),
            round=task.round,
            human_note=human_note,
            untrusted_finding_ids=untrusted,
            playbooks=tuple(triage.body.get("playbooks") or ()) if triage else (),
            tests_precommitted=(
                task.source == FOLLOWUP_SOURCE
                and task.round == 0
                and not findings
                and not failures
            ),
        )

    async def _engineering(self, task: TaskRecord) -> None:
        if await self._escalate_if_over_budget(task):
            return
        wt = await self._worktree()
        role = task.assignee or Role.JUNIOR
        if not await self._seed_qa_tests(task, wt, role):
            return
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

    async def _seed_qa_tests(self, task: TaskRecord, wt: Worktree, role: Role) -> bool:
        """Commit QA's tests into the follow-up task's branch, once, before the engineer."""
        if (
            task.source != FOLLOWUP_SOURCE
            or task.round != 0
            or self._meta("qa-tests") is not None
        ):
            return True
        request = self._latest(ArtifactKind.REQUEST)
        sources = request.body.get("test_files") if request else None
        written = await asyncio.to_thread(write_test_files, wt.path, sources)
        if not written:
            await self._escalate("stuck", _NO_QA_TESTS, actor=role)
            return False
        await asyncio.to_thread(commit_all, wt, "agentco: QA regression tests")
        self._set("qa-tests", "done")
        return True

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
                no_test_reason=self._no_test_reason(),
                test_only=self.task().source == FOLLOWUP_SOURCE,
            )
        except (ReplyInvalid, AgentStuck) as exc:
            failed = CheckResult(
                "rules", False, f"rules check failed: {_agent_error(exc)}"
            )
            return PreflightReport(False, (failed,), False), True
        return report, False

    def _no_test_reason(self) -> str:
        engineer = self._latest(ArtifactKind.ENGINEER)
        return str(engineer.body.get("no_test_reason", "")).strip() if engineer else ""

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
        floor = max(
            _id(self._latest(ArtifactKind.TRIAGE)),
            _id(self._latest(ArtifactKind.HUMAN_DECISION)),
        )
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

    def _require_invariant(self) -> bool:
        triage = self._latest(ArtifactKind.TRIAGE)
        return bool(triage and triage.body.get("require_invariant"))

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
                require_invariant=self._require_invariant(),
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
            opened = self._latest(ArtifactKind.PR)
            if opened is not None:
                pull = await self._update_pr(wt, opened, body)
                if pull is None:
                    return
            else:
                async with _repo_lock(self.ctx.repo_root):
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

    async def _update_pr(
        self, wt: Worktree, opened: Artifact, body: str
    ) -> PullRef | None:
        """Push the rewritten branch and edit the PR; None when the PR is gone.

        A PR that was already opened is never created again: a human closed it
        or it is not ours any more, so a person decides.
        """
        number = opened.body["number"]
        pull = await asyncio.to_thread(
            self.ctx.github.find_open_pull, self.ctx.repo, wt.branch
        )
        if pull is None or pull.number != number:
            await self._escalate(
                "stuck",
                f"PR #{number} is no longer open",
                options=["reject"],
                from_state=_S.LEAD_DECISION.value,
            )
            return None
        async with _repo_lock(self.ctx.repo_root):
            await asyncio.to_thread(push_branch, wt)
        await asyncio.to_thread(
            self.ctx.github.edit_pull, self.ctx.repo, pull.number, body
        )
        return pull

    def _feedback_titles(self) -> list[str]:
        """Titles of the PR/human findings sent back since the PR was last updated."""
        floor = _id(self._latest(ArtifactKind.PR))
        titles: list[str] = []
        for lead in self.store.artifacts(self.task_id, ArtifactKind.LEAD):
            if lead.id < floor or lead.body.get("source") not in ("pr", "human"):
                continue
            for finding in lead.body.get("findings_to_fix", []):
                if finding["title"] not in titles:
                    titles.append(finding["title"])
        return titles

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
        titles = self._feedback_titles() if self._is_pr_round() else []
        return pr_body(
            task,
            self._issue(),
            _lead_from(lead.body),
            self._checks().qa,
            report,
            self._source_issue(task),
            feedback=(task.round, titles) if titles else None,
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
            cancel_open_children(
                self.store, self.task_id, Role.HUMAN, "parent rejected"
            )
            return True
        if choice not in ("approve", "answer") or escalation is None:
            return False
        if origin == _S.ARCHITECT.value:
            if choice == "approve" and escalation.body.get("step") == "adr":
                await self._approve_adr(escalation)
            else:
                self._go(_S.ARCHITECT, Role.HUMAN, f"human {choice}")
        elif origin == _S.WAITING_CHILDREN.value:
            self._resume_parent(choice, escalation)
        elif origin == "pr_open":
            await self._resume_pr(choice, decision)
        elif escalation.body.get("stop") == "rounds":
            self._set("max_rounds", str(self._budgets().max_rounds + 1))
            self._go(_S.TRIAGE, Role.HUMAN, "one more round granted")
        elif escalation.body.get("open_pr") and choice == "approve":
            await self._open_pr()
        elif origin == "engineering":
            self._go(_S.ENGINEERING, Role.HUMAN, f"human {choice}")
        else:
            self._go(_S.TRIAGE, Role.HUMAN, f"human {choice}")
        return True

    def _resume_parent(self, choice: str, escalation: Artifact) -> None:
        step = escalation.body.get("step")
        if step == "child_failed":
            cancelled = {
                k.id
                for k in adr_children(self.store, self.task())
                if k.state is _S.CANCELLED
            }
            self._set("accepted", json.dumps(sorted(self._accepted() | cancelled)))
        if step == "parent_review" and choice == "approve":
            self._go(_S.MERGED, Role.HUMAN, "human approved the whole")
        else:
            self._go(_S.WAITING_CHILDREN, Role.HUMAN, f"human {choice}")

    async def _resume_pr(self, choice: str, decision: Artifact) -> None:
        """A human answered an escalation raised while the PR was open."""
        if choice == "approve":
            self._go(_S.PR_OPEN, Role.HUMAN, "human approved")
            return
        lead = self._latest(ArtifactKind.LEAD)
        if lead is None or lead.body.get("human_decision") != decision.id:
            note = str(decision.body.get("note", "")).strip()
            finding = Finding(
                id=f"h{decision.id}",
                severity="major",
                kind="bug",
                title=note[:80] or "Human guidance",
                detail=note or "Human guidance",
            )
            send_back = LeadDecision(
                outcome="send_back",
                category="",
                findings_to_fix=(finding,),
                follow_ups=(),
                reason="human guidance on the open PR",
                summary="",
                comments=(),
                question="",
                options=(),
            )
            body = TypeAdapter(LeadDecision).dump_python(send_back, mode="json")
            self.store.put_artifact(
                self.task_id,
                ArtifactKind.LEAD,
                Role.HUMAN,
                body | {"source": "human", "human_decision": decision.id},
            )
        if self.task().state is _S.AWAITING_HUMAN:
            self._go(_S.PR_OPEN, Role.HUMAN, "human answered: back to work")
        await self._start_answer_round()

    def _answer_interrupted(self) -> bool:
        """A human's send-back stored and the PR reopened, but the round not begun.

        Any later PR or escalation artifact means that round already ran.
        """
        lead = self._latest(ArtifactKind.LEAD)
        return (
            lead is not None
            and lead.body.get("source") == "human"
            and lead.id > _id(self._latest(ArtifactKind.PR))
            and lead.id > _id(self._latest(ArtifactKind.ESCALATION))
        )

    async def _start_answer_round(self) -> None:
        self._go(_S.TRIAGE, Role.HUMAN, "human guidance on the PR")
        await self._stop_rules()
