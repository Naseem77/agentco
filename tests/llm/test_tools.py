from datetime import UTC, datetime

from agentco.llm.tools import memory_tools
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.roles import Role


async def test_memory_tools_respect_gate():
    gate = MemoryGate("acme", InMemoryBackend())
    names = [t.name for t in memory_tools(gate.for_role(Role.QA), Scope.QA)]
    assert names == ["memory_search", "memory_add"]
    search = memory_tools(gate.for_role(Role.QA))[0]
    out = await search.handler(search.params(query="x", scopes=["rnd"]))
    assert out == "denied: role qa may not read scope rnd"


def test_search_only_without_write_scope():
    gate = MemoryGate("acme", InMemoryBackend())
    assert [t.name for t in memory_tools(gate.for_role(Role.JUNIOR))] == [
        "memory_search"
    ]


async def test_search_formats_facts_and_no_facts():
    backend = InMemoryBackend()
    gate = MemoryGate("acme", backend)
    await backend.add(
        graph_name("acme", Scope.RND),
        Scope.RND,
        MemoryItem("n", "parser fails on tabs", "t", datetime.now(UTC)),
    )
    search = memory_tools(gate.for_role(Role.JUNIOR))[0]
    assert await search.handler(search.params(query="tabs", scopes=None)) == (
        "- [acme__rnd] parser fails on tabs"
    )
    assert await search.handler(search.params(query="zzz", scopes=None)) == "no facts"


async def test_add_writes_to_write_scope_and_denies_others():
    backend = InMemoryBackend()
    gate = MemoryGate("acme", backend)
    add = memory_tools(gate.for_role(Role.QA), Scope.QA)[1]
    assert (
        await add.handler(add.params(name="n", body="neighbour: empty input"))
        == "added"
    )
    [item] = backend.items("acme__qa")
    assert (item.name, item.body) == ("n", "neighbour: empty input")

    denied = memory_tools(gate.for_role(Role.JUNIOR), Scope.RND)[1]
    out = await denied.handler(denied.params(name="n", body="b"))
    assert out == "denied: role junior may not write scope rnd"
    assert backend.items("acme__rnd") == []


async def test_search_rejects_unknown_scope_as_text():
    gate = MemoryGate("acme", InMemoryBackend())
    search = memory_tools(gate.for_role(Role.PM))[0]
    out = await search.handler(search.params(query="x", scopes=["bogus"]))
    assert out.startswith("error: ")
