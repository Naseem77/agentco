"""Code graph seam: backend protocol, in-memory fake, rnd-scope role gate, call resolution."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from agentco.code.parse import FileStructure, Symbol, innermost
from agentco.memory.gate import AccessDenied
from agentco.memory.scopes import ROLE_ACCESS, Scope, graph_name
from agentco.roles import Role


@dataclass(frozen=True, order=True)
class CallEdge:
    caller_path: str
    caller: str
    callee_path: str
    callee: str


class CodeGraphBackend(Protocol):
    async def ensure_schema(self, graph: str) -> None: ...

    async def file_hashes(self, graph: str, repo: str) -> dict[str, str]: ...

    async def upsert_file(self, graph: str, repo: str, fs: FileStructure) -> None: ...

    async def delete_file(self, graph: str, repo: str, path: str) -> None: ...

    async def symbols_in(self, graph: str, repo: str, path: str) -> list[Symbol]: ...

    async def all_symbols(self, graph: str, repo: str) -> dict[str, list[Symbol]]: ...

    async def replace_calls(
        self, graph: str, repo: str, edges: list[CallEdge]
    ) -> None: ...

    async def call_edges(self, graph: str, repo: str) -> list[CallEdge]: ...

    async def link_episode(
        self, graph: str, repo: str, episode: str, path: str, qualname: str
    ) -> bool:
        """Link an episode to a symbol; False when the symbol is not in the graph."""
        ...


@dataclass
class _Repo:
    files: dict[str, FileStructure]
    calls: list[CallEdge]
    links: set[tuple[str, str, str]]  # (episode, path, qualname)


class InMemoryCodeGraph:
    def __init__(self) -> None:
        self._repos: dict[tuple[str, str], _Repo] = defaultdict(
            lambda: _Repo({}, [], set())
        )
        self.upserts = 0

    def _repo(self, graph: str, repo: str) -> _Repo:
        return self._repos[(graph, repo)]

    def links(self, graph: str) -> set[tuple[str, str, str]]:
        return {
            link
            for (name, _), repo in self._repos.items()
            if name == graph
            for link in repo.links
        }

    async def ensure_schema(self, graph: str) -> None:
        return None

    async def file_hashes(self, graph: str, repo: str) -> dict[str, str]:
        return {p: fs.hash for p, fs in self._repo(graph, repo).files.items()}

    async def upsert_file(self, graph: str, repo: str, fs: FileStructure) -> None:
        self.upserts += 1
        state = self._repo(graph, repo)
        state.files[fs.path] = fs
        present = {(fs.path, s.qualname) for s in fs.symbols}
        state.links = {
            link
            for link in state.links
            if link[1] != fs.path or (link[1], link[2]) in present
        }
        # edges touching a symbol removed from this file go with it, as in a DB backend
        state.calls = [
            e
            for e in state.calls
            if (e.caller_path != fs.path or (e.caller_path, e.caller) in present)
            and (e.callee_path != fs.path or (e.callee_path, e.callee) in present)
        ]

    async def delete_file(self, graph: str, repo: str, path: str) -> None:
        state = self._repo(graph, repo)
        state.files.pop(path, None)
        state.links = {link for link in state.links if link[1] != path}
        state.calls = [
            e for e in state.calls if path not in (e.caller_path, e.callee_path)
        ]

    async def symbols_in(self, graph: str, repo: str, path: str) -> list[Symbol]:
        fs = self._repo(graph, repo).files.get(path)
        return list(fs.symbols) if fs else []

    async def all_symbols(self, graph: str, repo: str) -> dict[str, list[Symbol]]:
        return {p: list(fs.symbols) for p, fs in self._repo(graph, repo).files.items()}

    async def replace_calls(self, graph: str, repo: str, edges: list[CallEdge]) -> None:
        self._repo(graph, repo).calls = sorted(set(edges))

    async def call_edges(self, graph: str, repo: str) -> list[CallEdge]:
        return sorted(self._repo(graph, repo).calls)

    async def link_episode(
        self, graph: str, repo: str, episode: str, path: str, qualname: str
    ) -> bool:
        state = self._repo(graph, repo)
        fs = state.files.get(path)
        if fs is None or all(s.qualname != qualname for s in fs.symbols):
            return False
        state.links.add((episode, path, qualname))
        return True


_TOKEN = object()


class ScopedCodeGraph:
    """A role's view of the tenant's code graph (rnd scope); obtain via code_graph_for."""

    __slots__ = ("_backend", "_graph", "_role", "_tenant")

    def __init__(
        self, token: object, backend: CodeGraphBackend, tenant: str, role: Role
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("ScopedCodeGraph is created by code_graph_for")
        self._backend = backend
        self._graph = graph_name(tenant, Scope.RND)
        self._tenant = tenant
        self._role = role

    @property
    def role(self) -> Role:
        return self._role

    @property
    def tenant(self) -> str:
        return self._tenant

    def _need_read(self) -> None:
        if Scope.RND not in ROLE_ACCESS[self._role].read:
            raise AccessDenied(
                f"role {self._role.value} may not read scope {Scope.RND.value}"
            )

    def _need_write(self) -> None:
        if Scope.RND not in ROLE_ACCESS[self._role].write:
            raise AccessDenied(
                f"role {self._role.value} may not write scope {Scope.RND.value}"
            )

    async def ensure_schema(self) -> None:
        self._need_write()
        await self._backend.ensure_schema(self._graph)

    async def file_hashes(self, repo: str) -> dict[str, str]:
        self._need_read()
        return await self._backend.file_hashes(self._graph, repo)

    async def upsert_file(self, repo: str, fs: FileStructure) -> None:
        self._need_write()
        await self._backend.upsert_file(self._graph, repo, fs)

    async def delete_file(self, repo: str, path: str) -> None:
        self._need_write()
        await self._backend.delete_file(self._graph, repo, path)

    async def symbols_in(self, repo: str, path: str) -> list[Symbol]:
        self._need_read()
        return await self._backend.symbols_in(self._graph, repo, path)

    async def all_symbols(self, repo: str) -> dict[str, list[Symbol]]:
        self._need_read()
        return await self._backend.all_symbols(self._graph, repo)

    async def replace_calls(self, repo: str, edges: list[CallEdge]) -> None:
        self._need_write()
        await self._backend.replace_calls(self._graph, repo, edges)

    async def call_edges(self, repo: str) -> list[CallEdge]:
        self._need_read()
        return await self._backend.call_edges(self._graph, repo)

    async def link_episode(
        self, repo: str, episode: str, path: str, qualname: str
    ) -> bool:
        self._need_write()
        return await self._backend.link_episode(
            self._graph, repo, episode, path, qualname
        )

    async def enclosing(self, repo: str, path: str, line: int) -> Symbol | None:
        return innermost(await self.symbols_in(repo, path), line)


def code_graph_for(
    tenant: str, role: Role, backend: CodeGraphBackend
) -> ScopedCodeGraph:
    return ScopedCodeGraph(_TOKEN, backend, tenant, Role(role))


def resolve_calls(symbols_by_path: Mapping[str, Sequence[Symbol]]) -> list[CallEdge]:
    """Edges from each function to the function its call names, when unambiguous."""
    by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, symbols in symbols_by_path.items():
        for s in symbols:
            if s.kind == "Function":
                by_name[s.qualname.rsplit(".", 1)[-1]].append((path, s.qualname))

    edges: set[CallEdge] = set()
    for path, symbols in symbols_by_path.items():
        for s in symbols:
            if s.kind != "Function":
                continue
            for name in s.calls:
                candidates = by_name.get(name, [])
                local = [c for c in candidates if c[0] == path]
                if len(local) == 1:
                    target = local[0]
                elif not local and len(candidates) == 1:
                    target = candidates[0]
                else:
                    continue
                edges.add(CallEdge(path, s.qualname, *target))
    return sorted(edges)
