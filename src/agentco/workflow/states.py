from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class TaskState(StrEnum):
    INTAKE = "intake"
    PM = "pm"
    TRIAGE = "triage"
    ARCHITECT = "architect"
    ENGINEERING = "engineering"
    PREFLIGHT = "preflight"
    INTERNAL_REVIEW = "internal_review"
    LEAD_DECISION = "lead_decision"
    AWAITING_HUMAN = "awaiting_human"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    CANCELLED = "cancelled"


class InvalidTransition(ValueError):
    """Raised when a task is moved along an edge the workflow does not allow."""


_S = TaskState
_TERMINAL = frozenset({_S.MERGED, _S.CANCELLED})

_EDGES: dict[TaskState, set[TaskState]] = {
    _S.INTAKE: {_S.PM},
    _S.PM: {_S.TRIAGE},
    _S.TRIAGE: {_S.ENGINEERING, _S.ARCHITECT, _S.AWAITING_HUMAN},
    _S.ARCHITECT: {_S.AWAITING_HUMAN},
    _S.ENGINEERING: {_S.PREFLIGHT, _S.AWAITING_HUMAN},
    _S.PREFLIGHT: {_S.ENGINEERING, _S.INTERNAL_REVIEW, _S.TRIAGE},
    _S.INTERNAL_REVIEW: {_S.LEAD_DECISION},
    _S.LEAD_DECISION: {_S.TRIAGE, _S.AWAITING_HUMAN, _S.PR_OPEN},
    _S.PR_OPEN: {_S.TRIAGE, _S.AWAITING_HUMAN, _S.MERGED},
    _S.AWAITING_HUMAN: {_S.TRIAGE, _S.ENGINEERING, _S.PR_OPEN},
    _S.MERGED: set(),
    _S.CANCELLED: set(),
}

ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        state: frozenset(tos if state in _TERMINAL else tos | {_S.CANCELLED})
        for state, tos in _EDGES.items()
    }
)

# Sending work back to the first floor starts a new round.
BACK_TO_FIRST_FLOOR: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {(_S.LEAD_DECISION, _S.TRIAGE), (_S.PR_OPEN, _S.TRIAGE), (_S.PREFLIGHT, _S.TRIAGE)}
)
