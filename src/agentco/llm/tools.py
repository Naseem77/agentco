from datetime import UTC, datetime

from pydantic import BaseModel

from agentco.llm.runtime import ToolSpec
from agentco.memory.backend import MemoryItem
from agentco.memory.gate import AccessDenied, ScopedMemory
from agentco.memory.scopes import Scope


class SearchParams(BaseModel):
    query: str
    scopes: list[str] | None = None


class AddParams(BaseModel):
    name: str
    body: str


def memory_tools(
    memory: ScopedMemory, write_scope: Scope | None = None
) -> tuple[ToolSpec, ...]:
    """Memory tools for one role; failures come back as text, never raised."""

    async def search(params: BaseModel) -> str:
        assert isinstance(params, SearchParams)
        try:
            scopes = (
                None if params.scopes is None else [Scope(s) for s in params.scopes]
            )
            facts = await memory.search(params.query, scopes)
        except AccessDenied as exc:
            return f"denied: {exc}"
        except ValueError as exc:
            return f"error: {exc}"
        if not facts:
            return "no facts"
        return "\n".join(f"- [{fact.graph}] {fact.text}" for fact in facts)

    tools = [
        ToolSpec(
            "memory_search",
            "Search the company memory graphs your role may read. "
            "Optionally restrict to scopes (company, product, rnd, qa).",
            SearchParams,
            search,
        )
    ]
    if write_scope is not None:

        async def add(params: BaseModel) -> str:
            assert isinstance(params, AddParams)
            item = MemoryItem(
                params.name,
                params.body,
                f"agent:{memory.role.value}",
                datetime.now(UTC),
            )
            try:
                await memory.add(write_scope, item)
            except AccessDenied as exc:
                return f"denied: {exc}"
            return "added"

        tools.append(
            ToolSpec(
                "memory_add",
                f"Save one self-contained fact to the {write_scope.value} memory graph.",
                AddParams,
                add,
            )
        )
    return tuple(tools)
