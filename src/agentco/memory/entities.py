from typing import Literal

from pydantic import BaseModel

from agentco.memory.scopes import Scope


class Policy(BaseModel):
    """A company-wide policy that every agent must follow."""


class Convention(BaseModel):
    """A working convention or style agreement shared across the company."""


class EscalationRule(BaseModel):
    """A rule describing when an agent must escalate to a human."""

    category: Literal["critical", "new", "stuck"] | None = None


class HumanDecision(BaseModel):
    """A decision made by a human that agents must respect."""


class Feature(BaseModel):
    """A product feature, existing or planned."""


class Spec(BaseModel):
    """A written specification describing how a feature should behave."""


class AcceptanceCriterion(BaseModel):
    """A testable condition that must hold for a feature or spec to be accepted."""


class Customer(BaseModel):
    """A customer or customer segment of the product."""


class Feedback(BaseModel):
    """Feedback or a request raised by a customer or user."""


class Module(BaseModel):
    """A module or package of the codebase."""

    path: str | None = None


class File(BaseModel):
    """A source file in the codebase."""

    path: str | None = None


class Function(BaseModel):
    """A function or method in the codebase."""


class Class(BaseModel):
    """A class or type definition in the codebase."""


class Issue(BaseModel):
    """A GitHub issue describing work to be done or a bug to fix."""

    number: int | None = None


class PullRequest(BaseModel):
    """A GitHub pull request proposing a code change."""

    number: int | None = None


class ReviewComment(BaseModel):
    """A comment left by a reviewer on a pull request."""


class ReviewRule(BaseModel):
    """A rule that reviewers apply when checking code changes."""

    module_path: str | None = None
    origin: Literal["lead", "human", "seed"] | None = None


class Learning(BaseModel):
    """A lesson learned from past engineering work."""


class Playbook(BaseModel):
    """A step-by-step procedure for handling a recurring engineering situation."""


class Risk(BaseModel):
    """A known risk or fragile area in the codebase."""


class Decision(BaseModel):
    """An engineering or architectural decision and its rationale."""


class AgentProfile(BaseModel):
    """A profile of an agent's observed strengths, weaknesses, and track record."""


class TestCase(BaseModel):
    """A test case that verifies a behavior of the product."""


class TestRun(BaseModel):
    """A single execution of a test case or suite and its outcome."""


class FlakyTest(BaseModel):
    """A test that passes and fails inconsistently without code changes."""


class EscapedBug(BaseModel):
    """A bug that escaped testing and was found after release."""


class QALearning(BaseModel):
    """A lesson learned from past QA work."""


class NeighbourCase(BaseModel):
    """A test scenario adjacent to a known bug that should also be checked."""

    bug_class: str | None = None


ENTITY_TYPES: dict[Scope, dict[str, type[BaseModel]]] = {
    Scope.COMPANY: {
        m.__name__: m for m in (Policy, Convention, EscalationRule, HumanDecision)
    },
    Scope.PRODUCT: {
        m.__name__: m for m in (Feature, Spec, AcceptanceCriterion, Customer, Feedback)
    },
    Scope.RND: {
        m.__name__: m
        for m in (
            Module,
            File,
            Function,
            Class,
            Issue,
            PullRequest,
            ReviewComment,
            ReviewRule,
            Learning,
            Playbook,
            Risk,
            Decision,
            AgentProfile,
        )
    },
    Scope.QA: {
        m.__name__: m
        for m in (TestCase, TestRun, FlakyTest, EscapedBug, QALearning, NeighbourCase)
    },
}
