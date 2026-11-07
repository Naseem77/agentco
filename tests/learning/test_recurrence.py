from datetime import UTC, datetime

from agentco.learning.recurrence import (
    area_playbooks,
    area_recurrences,
    record_recurrence,
)
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role


def test_counts_distinct_events_per_module(store):
    record_recurrence(store, "acme", 1, ["src/calc/add.py"], "round-1")
    record_recurrence(store, "acme", 1, ["src/calc/add.py"], "round-1")  # same event
    modules = record_recurrence(
        store, "acme", 2, ["src/calc/mul.py", ""], "pr-review-9"
    )

    assert modules == ["src/calc"]
    assert area_recurrences(store, "acme", ["src/calc", "src/cal"]) == {
        "src/calc": 2,
        "src/cal": 0,
    }


def test_one_event_touching_two_files_of_a_module_counts_once(store):
    record_recurrence(store, "acme", 1, ["src/a/x.py", "src/a/y.py"], "round-1")

    assert area_recurrences(store, "acme", ["src/a"]) == {"src/a": 1}


def test_other_tenant_not_counted(store):
    record_recurrence(store, "other", 1, ["src/calc/add.py"], "round-1")
    record_recurrence(store, "acme", 1, ["src/calc/add.py"], "round-1")

    assert area_recurrences(store, "acme", ["src/calc"]) == {"src/calc": 1}


def test_a_module_with_a_colon_does_not_leak_into_its_prefix(store):
    record_recurrence(store, "acme", 1, ["src/a:b/x.py"], "round-1")

    assert area_recurrences(store, "acme", ["src/a", "src/a:b"]) == {
        "src/a": 0,
        "src/a:b": 1,
    }


async def memory_with(*texts):
    backend = InMemoryBackend()
    for number, text in enumerate(texts):
        await backend.add(
            "acme__rnd",
            Scope.RND,
            MemoryItem(f"n{number}", text, "t", datetime.now(UTC)),
        )
    return MemoryGate("acme", backend).for_role(Role.TRIAGE)


async def test_area_playbooks_dedupes_and_limits():
    memory = await memory_with(
        "Playbook src/calc: normalise inputs before compare",
        "Playbook src/calc: validate at the boundary",
        "Playbook src/calc: one rounding helper",
        "Playbook src/calc: log every override",
    )

    found = await area_playbooks(memory, ["src/calc", "src/calc", "Playbook"], limit=3)

    assert len(found) == 3 and len(set(found)) == 3
    assert all(text.startswith("Playbook src/calc") for text in found)


async def test_area_playbooks_without_queries_is_empty():
    memory = await memory_with("Playbook src/calc: normalise inputs")

    assert await area_playbooks(memory, []) == []
    assert await area_playbooks(memory, ["", "  "]) == []
