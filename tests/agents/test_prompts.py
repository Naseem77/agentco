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
    (Role.LEAD, "pr"),
    (Role.ARCHITECT, None),
    (Role.LEAD, "parent"),
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
        load_prompt(Role.HUMAN)


@pytest.mark.parametrize("variant", [None, "light"])
def test_reviewer_prompts_name_every_finding_field(variant):
    text = load_prompt(Role.REVIEWER, variant)
    for field in ("`id`", "`path`", "`title`", "`detail`", "`kind`", "`severity`"):
        assert field in text


def test_qa_prompt_names_every_failure_field():
    text = load_prompt(Role.QA)
    for field in ("`title`", "`steps`", "`expected`", "`actual`"):
        assert field in text
