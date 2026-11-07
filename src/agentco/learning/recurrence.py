"""Per-area recurrence counters (spec §5: root cause, not symptom).

An area is a module (a directory). Every event is one meta row, so recording the
same event twice changes nothing and a crashed run can simply be repeated.
"""

from collections.abc import Iterable

from agentco.importer.episodes import module_of
from agentco.memory.gate import ScopedMemory
from agentco.memory.scopes import Scope
from agentco.store.task_store import TaskStore


def _prefix(tenant: str, module: str) -> str:
    return f"recur:{tenant}:{module}:"


def record_recurrence(
    store: TaskStore, tenant: str, task_id: int, paths: Iterable[str], key: str
) -> list[str]:
    modules = sorted({module_of(path) for path in paths if path})
    for module in modules:
        store.set_meta(f"{_prefix(tenant, module)}{task_id}:{key}", "1")
    return modules


def area_recurrences(
    store: TaskStore, tenant: str, modules: Iterable[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for module in modules:
        prefix = _prefix(tenant, module)
        # `src/a:b` shares the prefix `src/a:`; its events start with a non-numeric id.
        counts[module] = sum(
            1
            for key in store.meta_with_prefix(prefix)
            if key[len(prefix) :].split(":", 1)[0].isdecimal()
        )
    return counts


async def area_playbooks(
    memory: ScopedMemory, queries: Iterable[str], limit: int = 3
) -> list[str]:
    texts: list[str] = []
    for query in queries:
        if not query.strip() or len(texts) >= limit:
            continue
        for fact in await memory.search(f"Playbook {query}", [Scope.RND]):
            if fact.text not in texts:
                texts.append(fact.text)
    return texts[:limit]
