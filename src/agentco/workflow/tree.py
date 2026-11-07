"""The task tree: one level of children, created from an approved ADR.

Every step is safe to repeat. A child is found by its marker (meta), then by its
`source_ref` among the parent's children, and only then created; the REQUEST
artifact is written last, so a crash at any point leaves a task the next run
completes rather than a second child.
"""

from agentco.agents.architect import ADRReply, SubTask
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import InvalidTransition, TaskState

ADR_SOURCE = "adr"
_DECISION_LIMIT = 1500
_NOT_OPEN = frozenset({TaskState.PR_OPEN, TaskState.MERGED, TaskState.CANCELLED})


def child_ref(parent_id: int, adr_id: int, index: int) -> str:
    return f"task:{parent_id}:adr:{adr_id}:{index}"


def _marker(tenant: str, adr_id: int, index: int) -> str:
    return f"tree:{tenant}:{adr_id}:{index}"


def request_text(parent: TaskRecord, adr: ADRReply, sub: SubTask) -> str:
    criteria = "\n".join(f"- {c}" for c in sub.acceptance_criteria)
    lines = [
        f"Part of #{parent.id}: {adr.title}",
        f"Decision: {adr.decision[:_DECISION_LIMIT]}",
        f"Sub-task: {sub.title}",
        f"Acceptance criteria:\n{criteria}",
        f"Size hint: {sub.size}",
    ]
    if sub.why:
        lines.append(f"Why: {sub.why}")
    return "\n\n".join(lines)


def spawn_children(
    store: TaskStore, parent: TaskRecord, adr: Artifact
) -> list[TaskRecord]:
    """One child per sub-task, in order; never transitions the parent."""
    if adr.kind is not ArtifactKind.ADR:
        raise ValueError(f"artifact {adr.id} is a {adr.kind.value}, not an ADR")
    reply = ADRReply.model_validate(adr.body["adr"])
    existing = {
        t.source_ref: t for t in store.children(parent.id) if t.source == ADR_SOURCE
    }
    children = []
    for index, sub in enumerate(reply.sub_tasks):
        ref = child_ref(parent.id, adr.id, index)
        marker = _marker(parent.tenant, adr.id, index)
        marked = store.get_meta(marker)
        child = (
            store.get(int(marked))
            if marked is not None
            else existing.get(ref)
            or store.create_task(
                parent.tenant,
                sub.title,
                ADR_SOURCE,
                source_ref=ref,
                parent_id=parent.id,
            )
        )
        if marked is None:
            store.set_meta(marker, str(child.id))
        if store.latest_artifact(child.id, ArtifactKind.REQUEST) is None:
            store.put_artifact(
                child.id,
                ArtifactKind.REQUEST,
                Role.ARCHITECT,
                {"body": request_text(parent, reply, sub)},
            )
        children.append(child)
    return children


def adr_children(store: TaskStore, parent: TaskRecord) -> list[TaskRecord]:
    return [t for t in store.children(parent.id) if t.source == ADR_SOURCE]


def cancel_open_children(
    store: TaskStore, parent_id: int, actor: Role, reason: str
) -> list[int]:
    """Cancel the ADR children that have no open PR and are not finished."""
    cancelled = []
    for child in store.children(parent_id):
        if child.source != ADR_SOURCE or child.state in _NOT_OPEN:
            continue
        try:
            store.transition(child.id, TaskState.CANCELLED, actor, reason)
        except InvalidTransition:
            continue
        cancelled.append(child.id)
    return cancelled
