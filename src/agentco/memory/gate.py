from collections.abc import Iterable
from dataclasses import dataclass

from agentco.memory.backend import Fact, MemoryBackend, MemoryItem
from agentco.memory.scopes import ROLE_ACCESS, Scope, graph_name
from agentco.roles import Role


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class ScopedMemory:
    role: Role
    tenant: str
    backend: MemoryBackend

    @property
    def readable(self) -> frozenset[Scope]:
        return ROLE_ACCESS[self.role].read

    async def search(
        self, query: str, scopes: Iterable[Scope] | None = None, limit: int = 10
    ) -> list[Fact]:
        requested = (
            list(self.readable) if scopes is None else [Scope(s) for s in scopes]
        )
        for scope in requested:
            if scope not in self.readable:
                raise AccessDenied(
                    f"role {self.role.value} may not read scope {scope.value}"
                )
        if not requested:
            return []
        graphs = [graph_name(self.tenant, scope) for scope in dict.fromkeys(requested)]
        return await self.backend.search(graphs, query, limit)

    async def add(self, scope: Scope, item: MemoryItem) -> None:
        scope = Scope(scope)
        if scope not in ROLE_ACCESS[self.role].write:
            raise AccessDenied(
                f"role {self.role.value} may not write scope {scope.value}"
            )
        await self.backend.add(graph_name(self.tenant, scope), scope, item)


class MemoryGate:
    def __init__(self, tenant: str, backend: MemoryBackend) -> None:
        graph_name(tenant, Scope.COMPANY)  # validates the tenant name
        self.tenant = tenant
        self.backend = backend

    def for_role(self, role: Role) -> ScopedMemory:
        return ScopedMemory(Role(role), self.tenant, self.backend)
