from datetime import datetime

from agentco.memory.backend import MemoryItem
from agentco.memory.gate import ScopedMemory
from agentco.memory.scopes import Scope
from agentco.store.task_store import TaskStore

SEED_VERSION = "1"

# Spec §9 "Seed policies" (lessons from the bug-fixing experiment; see also §5–§7).
SEED_POLICIES: list[tuple[str, str]] = [
    (
        "every-fix-is-new-code",
        (
            "Every fix is new code, so its own diff must be reviewed and tested "
            "as new code, not just checked for whether the reported problem is gone."
        ),
    ),
    (
        "fix-cause-with-one-rule",
        (
            "Fix the cause, not the symptom: state the one rule (invariant) that "
            "fixes the problem everywhere and apply it."
        ),
    ),
    (
        "test-the-neighbours",
        (
            "Test the neighbours, not just the reported case: run the neighbour "
            "matrix for the bug class, such as during loading, during a switch, "
            "switching back, when the component closes, and two at once."
        ),
    ),
    (
        "blind-review-before-push",
        (
            "Every change gets an independent blind review, which sees only the "
            "code and not the reasoning, before anything is pushed."
        ),
    ),
    (
        "keep-fixes-small",
        (
            "Keep fixes small and in scope, and split rare edge cases out into "
            "their own tasks instead of widening the current fix."
        ),
    ),
    (
        "run-everything-locally",
        (
            "Run everything locally before pushing, and prove that each new test "
            "fails without the fix."
        ),
    ),
    (
        "stop-when-not-real",
        (
            "Stop the review loop when a round's findings contain no real problems, "
            "because reviewers never say done on their own."
        ),
    ),
]

# Spec §6 "Escalation rules — when the human is pulled in" (defaults).
DEFAULT_ESCALATION_RULES: list[tuple[str, str, str]] = [
    (
        "security-sensitive-code",
        "critical",
        (
            "Escalate to the human for security-sensitive code: auth, permissions, "
            "crypto, secrets, and input sanitisation."
        ),
    ),
    (
        "data-changes",
        "critical",
        (
            "Escalate to the human for data changes: migrations, deletes, and "
            "schema changes on production data."
        ),
    ),
    (
        "infrastructure-deployment-ci",
        "critical",
        "Escalate to the human for infrastructure, deployment, and CI config changes.",
    ),
    (
        "breaking-public-api",
        "critical",
        "Escalate to the human when a change breaks a public API or contract.",
    ),
    (
        "payments-billing-legal",
        "critical",
        "Escalate to the human for payments, billing, licensing, and legal text.",
    ),
    (
        "regression-of-escaped-bug",
        "critical",
        (
            "Escalate to the human when QA finds a regression in a "
            "previously-fixed escaped bug."
        ),
    ),
    (
        "no-similar-past-issue",
        "new",
        (
            "Escalate to the human when there is no similar past issue in memory "
            "(similarity below a threshold)."
        ),
    ),
    (
        "module-without-knowledge",
        "new",
        (
            "Escalate to the human when the task touches a module with no "
            "knowledge in the graph yet."
        ),
    ),
    (
        "conflicting-rules",
        "new",
        "Escalate to the human when two review rules or policies conflict.",
    ),
    (
        "new-dependency-or-pattern",
        "new",
        (
            "Escalate to the human when an agent proposes a new dependency or a "
            "new architectural pattern not covered by an ADR."
        ),
    ),
    (
        "stop-rules-or-budget",
        "stuck",
        (
            "Escalate to the human when the stop rules are hit or the cost "
            "budget is exceeded."
        ),
    ),
]


async def seed_company(mem: ScopedMemory, store: TaskStore, now: datetime) -> bool:
    if store.get_meta("seed_version") == SEED_VERSION:
        return False
    for slug, text in SEED_POLICIES:
        await mem.add(
            Scope.COMPANY, MemoryItem(f"policy:{slug}", text, "seed policy", now)
        )
    for slug, category, text in DEFAULT_ESCALATION_RULES:
        await mem.add(
            Scope.COMPANY,
            MemoryItem(
                f"escalation:{slug}", text, f"seed escalation rule ({category})", now
            ),
        )
    store.set_meta("seed_version", SEED_VERSION)
    return True
