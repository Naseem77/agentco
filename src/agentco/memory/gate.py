from collections.abc import Iterable

from agentco.memory.backend import Fact, MemoryBackend, MemoryItem
from agentco.memory.scopes import ROLE_ACCESS, Scope, graph_name
from agentco.roles import Role


class AccessDenied(PermissionError):
    pass


_TOKEN = object()


class ScopedMemory:
    """A role's view of tenant memory; obtain one via MemoryGate.for_role."""

    __slots__ = ("_backend", "_role", "_tenant")

    def __init__(
        self, token: object, backend: MemoryBackend, tenant: str, role: Role
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("ScopedMemory is created by MemoryGate.for_role")
        self._backend = backend
        self._tenant = tenant
        self._role = role

    @property
    def role(self) -> Role:
        return self._role

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def readable(self) -> frozenset[Scope]:
        return ROLE_ACCESS[self.role].read

    async def search(
        self, query: str, scopes: Iterable[Scope] | None = None, limit: int = 10
    ) -> list[Fact]:
        requested = (
            [s for s in Scope if s in self.readable]
            if scopes is None
            else [Scope(s) for s in scopes]
        )
        for scope in requested:
            if scope not in self.readable:
                raise AccessDenied(
                    f"role {self.role.value} may not read scope {scope.value}"
                )
        if not requested:
            return []
        graphs = [graph_name(self._tenant, scope) for scope in dict.fromkeys(requested)]
        return await self._backend.search(graphs, query, limit)

    async def add(self, scope: Scope, item: MemoryItem) -> None:
        scope = Scope(scope)
        if scope not in ROLE_ACCESS[self.role].write:
            raise AccessDenied(
                f"role {self.role.value} may not write scope {scope.value}"
            )
        await self._backend.add(graph_name(self._tenant, scope), scope, item)


class MemoryGate:
    def __init__(self, tenant: str, backend: MemoryBackend) -> None:
        graph_name(tenant, Scope.COMPANY)  # validates the tenant name
        self.tenant = tenant
        self.backend = backend

    def for_role(self, role: Role) -> ScopedMemory:
        return ScopedMemory(_TOKEN, self.backend, self.tenant, Role(role))
