import pytest
from code_graph_contract import check_contract

from agentco.code.graph import (
    CallEdge,
    InMemoryCodeGraph,
    ScopedCodeGraph,
    code_graph_for,
    resolve_calls,
)
from agentco.code.parse import Symbol, parse_source
from agentco.memory.gate import AccessDenied
from agentco.roles import Role


def fs(path: str, source: str):
    return parse_source(path, source.encode())


def syms(*specs: tuple[str, list[str]]) -> list[Symbol]:
    return [
        Symbol("Function", name, name, i * 10 + 1, i * 10 + 5, tuple(calls))
        for i, (name, calls) in enumerate(specs)
    ]


async def test_qa_cannot_read_code_graph():
    with pytest.raises(AccessDenied, match="qa.*rnd"):
        await code_graph_for("acme", Role.QA, InMemoryCodeGraph()).symbols_in(
            "o/r", "a.py"
        )


async def test_denial_message_names_role_and_scope():
    with pytest.raises(AccessDenied) as exc:
        await code_graph_for("acme", Role.QA, InMemoryCodeGraph()).all_symbols("o/r")
    assert str(exc.value) == "role qa may not read scope rnd"
    with pytest.raises(AccessDenied) as exc:
        await code_graph_for("acme", Role.JUNIOR, InMemoryCodeGraph()).delete_file(
            "o/r", "a.py"
        )
    assert str(exc.value) == "role junior may not write scope rnd"


async def test_junior_reads_but_cannot_write():
    backend = InMemoryCodeGraph()
    g = code_graph_for("acme", Role.JUNIOR, backend)
    assert await g.symbols_in("o/r", "a.py") == []
    with pytest.raises(AccessDenied):
        await g.upsert_file("o/r", fs("a.py", "def f(): pass"))
    assert backend.upserts == 0


async def test_every_write_method_is_gated():
    g = code_graph_for("acme", Role.JUNIOR, InMemoryCodeGraph())
    with pytest.raises(AccessDenied):
        await g.ensure_schema()
    with pytest.raises(AccessDenied):
        await g.delete_file("o/r", "a.py")
    with pytest.raises(AccessDenied):
        await g.replace_calls("o/r", [])
    with pytest.raises(AccessDenied):
        await g.link_episode("o/r", "e", "a.py", "f")


async def test_every_read_method_is_gated():
    g = code_graph_for("acme", Role.QA, InMemoryCodeGraph())
    for call in (
        g.file_hashes("o/r"),
        g.symbols_in("o/r", "a.py"),
        g.all_symbols("o/r"),
        g.call_edges("o/r"),
        g.enclosing("o/r", "a.py", 1),
    ):
        with pytest.raises(AccessDenied):
            await call


async def test_tenants_and_repos_never_mix():
    b = InMemoryCodeGraph()
    await code_graph_for("acme", Role.LIBRARIAN, b).upsert_file(
        "o/r", fs("a.py", "def f(): pass")
    )
    assert await code_graph_for("globex", Role.LEAD, b).symbols_in("o/r", "a.py") == []
    assert (
        await code_graph_for("acme", Role.LEAD, b).symbols_in("o/other", "a.py") == []
    )
    assert [
        s.qualname
        for s in await code_graph_for("acme", Role.LEAD, b).symbols_in("o/r", "a.py")
    ] == ["f"]


async def test_scoped_graph_is_bound_to_the_tenant_rnd_graph():
    b = InMemoryCodeGraph()
    await code_graph_for("acme", Role.LIBRARIAN, b).upsert_file(
        "o/r", fs("a.py", "def f(): pass")
    )
    assert [s.qualname for s in await b.symbols_in("acme__rnd", "o/r", "a.py")] == ["f"]
    assert await b.symbols_in("acme__qa", "o/r", "a.py") == []


async def test_enclosing_returns_innermost_symbol():
    g = code_graph_for("acme", Role.LIBRARIAN, InMemoryCodeGraph())
    await g.upsert_file(
        "o/r", fs("a.py", "class A:\n    def m(self):\n        pass\n\nx = 1\n")
    )
    found = await g.enclosing("o/r", "a.py", 3)
    assert found is not None and found.qualname == "A.m"
    assert await g.enclosing("o/r", "a.py", 5) is None
    assert await g.enclosing("o/r", "missing.py", 1) is None


def test_scoped_graph_cannot_be_constructed_directly():
    with pytest.raises(TypeError):
        ScopedCodeGraph(object(), InMemoryCodeGraph(), "acme", Role.LEAD)


def test_scoped_graph_rejects_invalid_tenant():
    with pytest.raises(ValueError):
        code_graph_for("Bad Tenant", Role.LEAD, InMemoryCodeGraph())


def test_resolve_calls_prefers_same_file_then_unique_repo_wide():
    edges = resolve_calls(
        {
            "a.py": syms(("run", ["load", "save", "missing"]), ("load", [])),
            "b.py": syms(("load", []), ("save", [])),
            "c.py": syms(("go", ["dup"])),
            "d.py": syms(("dup", [])),
            "e.py": syms(("dup", [])),
        }
    )
    assert edges == [
        CallEdge("a.py", "run", "a.py", "load"),
        CallEdge("a.py", "run", "b.py", "save"),
    ]


def test_resolve_calls_matches_last_segment_and_allows_recursion():
    cls = Symbol("Class", "K", "K", 1, 9)
    method = Symbol("Function", "walk", "K.walk", 2, 8, ("walk", "K"))
    edges = resolve_calls({"a.py": [cls, method]})
    assert edges == [CallEdge("a.py", "K.walk", "a.py", "K.walk")]


def test_resolve_calls_ambiguous_in_caller_file_gets_no_edge():
    a = Symbol("Function", "x", "A.x", 1, 2)
    b = Symbol("Function", "x", "B.x", 3, 4)
    caller = Symbol("Function", "f", "f", 5, 6, ("x",))
    assert (
        resolve_calls(
            {"a.py": [a, b, caller], "b.py": [Symbol("Function", "x", "x", 1, 2)]}
        )
        == []
    )


def test_resolve_calls_is_sorted_and_unique():
    edges = resolve_calls(
        {
            "z.py": syms(("b", ["a", "a"]), ("a", [])),
            "a.py": syms(("c", ["a"])),
        }
    )
    assert edges == [
        CallEdge("a.py", "c", "z.py", "a"),
        CallEdge("z.py", "b", "z.py", "a"),
    ]


async def noop(name: str) -> None:  # the fake links by name; no Episodic node needed
    return None


async def test_in_memory_graph_honours_contract():
    await check_contract(InMemoryCodeGraph(), "acme__rnd", add_episode=noop)


async def test_upsert_counter_counts_calls():
    b = InMemoryCodeGraph()
    await b.upsert_file("g", "o/r", fs("a.py", "def f(): pass"))
    await b.upsert_file("g", "o/r", fs("a.py", "def f(): pass"))
    assert b.upserts == 2


async def test_delete_file_drops_links_and_touching_call_edges():
    b = InMemoryCodeGraph()
    await b.upsert_file("g", "r", fs("a.py", "def f(): pass"))
    await b.upsert_file("g", "r", fs("b.py", "def h(): pass"))
    assert await b.link_episode("g", "r", "ep", "a.py", "f") is True
    assert await b.link_episode("g", "r", "ep", "b.py", "h") is True
    await b.replace_calls(
        "g",
        "r",
        [CallEdge("a.py", "f", "b.py", "h"), CallEdge("b.py", "h", "b.py", "h")],
    )
    await b.delete_file("g", "r", "a.py")
    assert b.links("g") == {("ep", "b.py", "h")}
    assert await b.call_edges("g", "r") == [CallEdge("b.py", "h", "b.py", "h")]
    assert list(await b.all_symbols("g", "r")) == ["b.py"]


async def test_links_are_per_graph():
    b = InMemoryCodeGraph()
    await b.upsert_file("g1", "r", fs("a.py", "def f(): pass"))
    assert await b.link_episode("g1", "r", "ep", "a.py", "f") is True
    assert await b.link_episode("g2", "r", "ep", "a.py", "f") is False
    assert b.links("g1") == {("ep", "a.py", "f")}
    assert b.links("g2") == set()
