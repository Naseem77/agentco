import dataclasses
from datetime import UTC, datetime

import pytest

from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import AccessDenied, MemoryGate, ScopedMemory
from agentco.memory.scopes import Scope
from agentco.roles import Role


def item(body: str) -> MemoryItem:
    return MemoryItem(
        name="n", body=body, source_description="test", reference_time=datetime.now(UTC)
    )


async def test_qa_cannot_read_rnd():
    qa = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    with pytest.raises(AccessDenied, match="qa.*rnd"):
        await qa.search("anything", scopes=[Scope.RND])


async def test_default_scopes_are_role_readable_only():
    backend = InMemoryBackend()
    gate = MemoryGate("acme", backend)
    await gate.for_role(Role.LIBRARIAN).add(
        Scope.RND, item("engineer note: retry loop")
    )
    await gate.for_role(Role.LIBRARIAN).add(Scope.QA, item("qa: retry loop flaky"))
    facts = await gate.for_role(Role.QA).search("retry")
    assert {f.graph for f in facts} == {"acme__qa"}


async def test_empty_scope_list_means_nothing_not_everything():
    qa = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    assert await qa.search("x", scopes=[]) == []


async def test_write_outside_role_denied():
    qa = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    with pytest.raises(AccessDenied):
        await qa.add(Scope.RND, item("x"))


async def test_tenants_never_mix():
    backend = InMemoryBackend()
    await (
        MemoryGate("acme", backend)
        .for_role(Role.LIBRARIAN)
        .add(Scope.RND, item("acme secret"))
    )
    own = await MemoryGate("acme", backend).for_role(Role.LEAD).search("secret")
    assert [f.text for f in own] == ["acme secret"]
    assert (
        await MemoryGate("globex", backend).for_role(Role.LEAD).search("secret") == []
    )


async def test_denied_read_never_reaches_backend():
    class Spy(InMemoryBackend):
        calls = 0

        async def search(self, graphs, query, limit):
            Spy.calls += 1
            return await super().search(graphs, query, limit)

    qa = MemoryGate("acme", Spy()).for_role(Role.QA)
    with pytest.raises(AccessDenied):
        await qa.search("x", scopes=[Scope.QA, Scope.RND])
    assert await qa.search("x", scopes=[]) == []
    assert Spy.calls == 0


async def test_limit_and_case_insensitive_search():
    backend = InMemoryBackend()
    lib = MemoryGate("acme", backend).for_role(Role.LIBRARIAN)
    for i in range(3):
        await lib.add(Scope.COMPANY, item(f"Deploy note {i}"))
    assert len(await lib.search("DEPLOY", limit=2)) == 2
    assert len(backend.items("acme__company")) == 3


def test_invalid_tenant_rejected():
    with pytest.raises(ValueError):
        MemoryGate("Bad Tenant", InMemoryBackend())


async def test_default_search_follows_declared_scope_order():
    seen: list[str] = []

    class Spy(InMemoryBackend):
        async def search(self, graphs, query, limit):
            seen.extend(graphs)
            return []

    await MemoryGate("acme", Spy()).for_role(Role.LEAD).search("x")
    assert seen == ["acme__company", "acme__product", "acme__rnd", "acme__qa"]


def test_scoped_memory_is_sealed():
    mem = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    assert not hasattr(mem, "backend")
    with pytest.raises(TypeError):
        dataclasses.replace(mem, role=Role.LEAD)
    with pytest.raises(AttributeError):
        mem.role = Role.LEAD
    with pytest.raises(AttributeError):
        mem.tenant = "globex"
    assert mem.role is Role.QA and mem.tenant == "acme"


def test_scoped_memory_cannot_be_constructed_directly():
    with pytest.raises(TypeError):
        ScopedMemory(object(), InMemoryBackend(), "acme", Role.LEAD)
