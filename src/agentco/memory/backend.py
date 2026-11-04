from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agentco.memory.scopes import Scope


@dataclass(frozen=True)
class MemoryItem:
    name: str
    body: str
    source_description: str
    reference_time: datetime


@dataclass(frozen=True)
class Fact:
    text: str
    graph: str
    uuid: str
    valid_at: datetime | None
    invalid_at: datetime | None


class MemoryBackend(Protocol):
    async def add(self, graph: str, scope: Scope, item: MemoryItem) -> None: ...

    async def search(self, graphs: list[str], query: str, limit: int) -> list[Fact]: ...

    async def ensure_graph(self, graph: str) -> None: ...


class InMemoryBackend:
    def __init__(self) -> None:
        self._graphs: dict[str, list[MemoryItem]] = {}

    async def ensure_graph(self, graph: str) -> None:
        self._graphs.setdefault(graph, [])

    async def add(self, graph: str, scope: Scope, item: MemoryItem) -> None:
        self._graphs.setdefault(graph, []).append(item)

    async def search(self, graphs: list[str], query: str, limit: int) -> list[Fact]:
        words = query.lower().split()
        facts: list[Fact] = []
        for graph in graphs:
            for index, stored in enumerate(self._graphs.get(graph, [])):
                if any(word in stored.body.lower() for word in words):
                    facts.append(
                        Fact(
                            text=stored.body,
                            graph=graph,
                            uuid=f"{graph}:{index}",
                            valid_at=stored.reference_time,
                            invalid_at=None,
                        )
                    )
        return facts[:limit]

    def items(self, graph: str) -> list[MemoryItem]:
        return list(self._graphs.get(graph, []))
