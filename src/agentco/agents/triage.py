from dataclasses import asdict, dataclass

from agentco.agents.models import role_model
from agentco.agents.prompts import load_prompt
from agentco.agents.replies import PMReply, TriageReply
from agentco.config import TenantConfig
from agentco.importer.episodes import module_of
from agentco.learning.recurrence import (
    area_playbooks,
    area_recurrences,
    record_recurrence,
)
from agentco.llm.replies import Asker
from agentco.llm.runtime import AgentCall
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore

_SMALL = frozenset({"xs", "s"})


@dataclass(frozen=True)
class TriageDecision:
    route: Role
    fast_lane: bool
    require_invariant: bool
    escalate: str | None
    reason: str
    root_cause: tuple[str, ...] = ()
    playbooks: tuple[str, ...] = ()


def _issue_prompt(issue: PMReply) -> str:
    criteria = "\n".join(f"- {criterion}" for criterion in issue.acceptance_criteria)
    return (
        f"Summary: {issue.summary}\n\nWhy: {issue.why}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Product notes: {issue.product_notes or '(none)'}"
    )


def _repeat_paths(store: TaskStore, task_id: int) -> list[str]:
    """Paths that earlier send-backs flagged in at least two different rounds."""
    rounds: dict[str, set[int]] = {}
    for artifact in store.artifacts(task_id, ArtifactKind.LEAD):
        for finding in artifact.body.get("findings_to_fix", []):
            path = finding.get("path", "")
            if path:
                rounds.setdefault(path, set()).add(artifact.round)
    return sorted(path for path, seen in rounds.items() if len(seen) >= 2)


def _send_back_paths(store: TaskStore, task_id: int) -> list[str]:
    """Paths of the latest send-back, whether it came from the lead or from the PR."""
    sent_back = [
        a
        for a in store.artifacts(task_id, ArtifactKind.LEAD)
        if a.body.get("outcome") == "send_back"
    ]
    if not sent_back:
        return []
    findings = sent_back[-1].body.get("findings_to_fix", [])
    return [f["path"] for f in findings if f.get("path")]


async def run_triage(
    asker: Asker,
    cfg: TenantConfig,
    gate: MemoryGate,
    store: TaskStore,
    task: TaskRecord,
    issue: PMReply,
    *,
    human_ok: bool = False,
) -> TriageDecision:
    call = AgentCall(
        role=Role.TRIAGE,
        model=role_model(cfg, Role.TRIAGE).model,
        system=load_prompt(Role.TRIAGE),
        prompt=_issue_prompt(issue),
        max_turns=cfg.loop.agent_max_turns,
        timeout_s=cfg.loop.agent_timeout_s,
    )
    reply = await asker.ask(call, TriageReply)

    route = Role.JUNIOR if reply.route == "junior" else Role.SENIOR
    reason = reply.reason
    escalate: str | None = None

    query = f"{issue.summary} {' '.join(issue.acceptance_criteria)}"
    facts = await gate.for_role(Role.TRIAGE).search(query, [Scope.RND])
    if len(facts) < cfg.thresholds.known_min_facts and not human_ok:
        escalate = "new"
        reason = (
            f"unknown area: {len(facts)} rnd facts, "
            f"need {cfg.thresholds.known_min_facts}; a human decides first"
        )
    elif reply.route == "new_design" and not human_ok:
        escalate = "new"
        reason = (
            "new design needs the Architect (Plan 5); "
            f"a human decides for now. Triage: {reply.reason}"
        )

    repeated = _repeat_paths(store, task.id)
    require_invariant = bool(repeated)
    if repeated:
        route = Role.SENIOR
        reason = (
            f"{reason} Repeat area ({', '.join(repeated)}): fix causes with one rule."
        )

    # An area is hot once it has come back `root_cause_after` times, across tasks.
    record_recurrence(store, task.tenant, task.id, repeated, f"round-{task.round}")
    back_paths = _send_back_paths(store, task.id)
    back_modules = sorted({module_of(path) for path in back_paths})
    counts = area_recurrences(store, task.tenant, back_modules)
    hot = tuple(
        module
        for module in back_modules
        if counts[module] >= cfg.thresholds.root_cause_after
    )
    if hot:
        route = Role.SENIOR
        require_invariant = True
        reason = (
            f"{reason} Root cause: {', '.join(hot)} came back "
            f"{max(counts[module] for module in hot)} times; state one invariant."
        )
    playbooks = await area_playbooks(
        gate.for_role(Role.TRIAGE), [*back_modules, issue.summary]
    )

    fast_lane = (
        cfg.loop.fast_lane
        and reply.size in _SMALL
        and reply.risk == "low"
        and route is Role.JUNIOR
        and task.round == 0
        and escalate is None
    )
    decision = TriageDecision(
        route,
        fast_lane,
        require_invariant,
        escalate,
        reason,
        hot,
        tuple(playbooks),
    )
    store.put_artifact(
        task.id,
        ArtifactKind.TRIAGE,
        Role.TRIAGE,
        {
            **asdict(decision),
            "route": route.value,
            "reply": reply.model_dump(),
        },
    )
    return decision
