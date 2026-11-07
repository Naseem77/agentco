from agentco.workflow.states import (
    ALLOWED_TRANSITIONS,
    BACK_TO_FIRST_FLOOR,
    TaskState,
)


def test_lead_cannot_skip_to_merged():
    assert TaskState.MERGED not in ALLOWED_TRANSITIONS[TaskState.LEAD_DECISION]


def test_pr_only_opens_from_lead_or_human():
    sources = {s for s, tos in ALLOWED_TRANSITIONS.items() if TaskState.PR_OPEN in tos}
    assert sources == {TaskState.LEAD_DECISION, TaskState.AWAITING_HUMAN}


def test_terminal_states_have_no_exits():
    assert ALLOWED_TRANSITIONS[TaskState.MERGED] == frozenset()
    assert ALLOWED_TRANSITIONS[TaskState.CANCELLED] == frozenset()


def test_every_non_terminal_state_can_be_cancelled():
    terminal = {TaskState.MERGED, TaskState.CANCELLED}
    for state in TaskState:
        if state not in terminal:
            assert TaskState.CANCELLED in ALLOWED_TRANSITIONS[state]


def test_every_state_has_an_entry():
    assert set(ALLOWED_TRANSITIONS) == set(TaskState)


def test_back_to_first_floor_edges_are_allowed():
    for src, dst in BACK_TO_FIRST_FLOOR:
        assert dst in ALLOWED_TRANSITIONS[src]
    assert BACK_TO_FIRST_FLOOR == {
        (TaskState.LEAD_DECISION, TaskState.TRIAGE),
        (TaskState.PR_OPEN, TaskState.TRIAGE),
        (TaskState.PREFLIGHT, TaskState.TRIAGE),
    }
