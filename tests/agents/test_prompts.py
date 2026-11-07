import pytest

from agentco.agents.prompts import load_prompt
from agentco.roles import Role

PROMPTS = [
    (Role.PM, None),
    (Role.TRIAGE, None),
    (Role.JUNIOR, None),
    (Role.SENIOR, None),
    (Role.REVIEWER, None),
    (Role.REVIEWER, "light"),
    (Role.REVIEWER, "rules"),
    (Role.QA, None),
    (Role.LEAD, None),
]


@pytest.mark.parametrize("role,variant", PROMPTS)
def test_every_role_prompt_loads(role, variant):
    text = load_prompt(role, variant)
    assert text.strip()
    assert "JSON" in text


@pytest.mark.parametrize(
    "role,variant",
    [
        (Role.QA, None),
        (Role.REVIEWER, None),
        (Role.REVIEWER, "light"),
        (Role.REVIEWER, "rules"),
    ],
)
def test_isolated_roles_state_they_are_blind(role, variant):
    assert "must not ask for the engineer's reasoning" in load_prompt(role, variant)


def test_missing_prompt_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt(Role.ARCHITECT)
