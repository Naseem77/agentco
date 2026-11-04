"""Behaviour every CodeGraphBackend must honour; shared by the fake and FalkorDB tests."""

from collections.abc import Awaitable, Callable

from agentco.code.graph import CallEdge, CodeGraphBackend
from agentco.code.parse import parse_source

REPO = "o/r"
PATH = "src/app/graph.py"
UTIL = "src/app/util.py"
UTIL_SRC = b"def show(g):\n    return g\n"
OTHER_REPO = "o/other"
A = "src/a.py"
B = "src/b.py"
A_V1 = b"""def f():
    g()


def g():
    h()


def h():
    pass
"""
A_V2 = b"""def f():
    h()


def h():
    pass
"""
B_SRC = b"def u():\n    f()\n"

V1 = b"""class GraphView:
    def load(self, g):
        return helper(g)

    def switch(self, g):
        self.load(g)
"""

V2 = b"""# header

class GraphView:
    def switch(self, g):
        self.show(g)
"""


async def check_contract(
    backend: CodeGraphBackend,
    graph: str,
    add_episode: Callable[[str], Awaitable[object]],
    count_links: Callable[[], Awaitable[int]] | None = None,
) -> None:
    async def links() -> int:
        if count_links is not None:
            return await count_links()
        return len(backend.links(graph))  # type: ignore[attr-defined]

    v1 = parse_source(PATH, V1)
    v2 = parse_source(PATH, V2)
    assert v1 is not None and v2 is not None

    await backend.ensure_schema(graph)
    await backend.upsert_file(graph, REPO, v1)
    assert await backend.file_hashes(graph, REPO) == {PATH: v1.hash}
    got = await backend.symbols_in(graph, REPO, PATH)
    assert sorted(got, key=lambda s: s.start_line) == list(v1.symbols)
    assert [(s.kind, s.qualname, s.start_line, s.end_line, s.calls) for s in got] == [
        ("Class", "GraphView", 1, 6, ()),
        ("Function", "GraphView.load", 2, 3, ("helper",)),
        ("Function", "GraphView.switch", 5, 6, ("load",)),
    ]

    episode = "review:o/r:101"
    await add_episode(episode)
    assert await backend.link_episode(graph, REPO, episode, PATH, "GraphView.switch")
    assert not await backend.link_episode(graph, REPO, episode, PATH, "GraphView.nope")
    assert await links() == 1

    await backend.upsert_file(graph, REPO, v2)
    assert await backend.file_hashes(graph, REPO) == {PATH: v2.hash}
    after = await backend.symbols_in(graph, REPO, PATH)
    assert [(s.kind, s.qualname, s.start_line, s.end_line, s.calls) for s in after] == [
        ("Class", "GraphView", 3, 5, ()),
        ("Function", "GraphView.switch", 4, 5, ("show",)),
    ]
    assert await links() == 1
    assert not await backend.link_episode(graph, REPO, episode, PATH, "GraphView.load")
    assert await links() == 1

    util = parse_source(UTIL, UTIL_SRC)
    assert util is not None
    await backend.upsert_file(graph, REPO, util)
    first = CallEdge(PATH, "GraphView.switch", UTIL, "show")
    second = CallEdge(PATH, "GraphView.switch", PATH, "GraphView.switch")
    await backend.replace_calls(graph, REPO, [first])
    assert await backend.call_edges(graph, REPO) == [first]
    await backend.replace_calls(graph, REPO, [second])
    assert await backend.call_edges(graph, REPO) == [second]
    assert sorted(await backend.all_symbols(graph, REPO)) == [PATH, UTIL]

    await backend.delete_file(graph, REPO, PATH)
    assert await backend.file_hashes(graph, REPO) == {UTIL: util.hash}
    assert await backend.symbols_in(graph, REPO, PATH) == []
    assert await backend.call_edges(graph, REPO) == []
    assert await links() == 0

    await backend.delete_file(graph, REPO, UTIL)
    assert await backend.file_hashes(graph, REPO) == {}
    assert await backend.all_symbols(graph, REPO) == {}

    await check_reupsert_and_isolation(backend, graph)


async def check_reupsert_and_isolation(backend: CodeGraphBackend, graph: str) -> None:
    a1 = parse_source(A, A_V1)
    a2 = parse_source(A, A_V2)
    b = parse_source(B, B_SRC)
    assert a1 is not None and a2 is not None and b is not None

    await backend.upsert_file(graph, REPO, a1)
    await backend.upsert_file(graph, REPO, b)
    f_g = CallEdge(A, "f", A, "g")
    g_h = CallEdge(A, "g", A, "h")
    f_h = CallEdge(A, "f", A, "h")
    u_g = CallEdge(B, "u", A, "g")
    u_h = CallEdge(B, "u", A, "h")
    await backend.replace_calls(graph, REPO, [f_g, g_h, f_h, u_g, u_h])
    assert await backend.call_edges(graph, REPO) == sorted([f_g, g_h, f_h, u_g, u_h])

    # removing g drops edges where g is the caller or the callee, in any file
    await backend.upsert_file(graph, REPO, a2)
    assert await backend.call_edges(graph, REPO) == sorted([f_h, u_h])

    # upserting the same structure again changes nothing
    symbols = await backend.all_symbols(graph, REPO)
    await backend.upsert_file(graph, REPO, a2)
    await backend.upsert_file(graph, REPO, a2)
    assert await backend.all_symbols(graph, REPO) == symbols
    assert [s.qualname for s in symbols[A]] == ["f", "h"]
    assert await backend.file_hashes(graph, REPO) == {A: a2.hash, B: b.hash}
    assert await backend.call_edges(graph, REPO) == sorted([f_h, u_h])

    # a second repo with the same path is independent of the first
    other = parse_source(A, b"def only_here():\n    pass\n")
    assert other is not None
    await backend.upsert_file(graph, OTHER_REPO, other)
    assert await backend.file_hashes(graph, OTHER_REPO) == {A: other.hash}
    assert [s.qualname for s in await backend.symbols_in(graph, OTHER_REPO, A)] == [
        "only_here"
    ]
    assert await backend.call_edges(graph, OTHER_REPO) == []
    assert await backend.symbols_in(graph, REPO, A) == list(a2.symbols)
    assert await backend.file_hashes(graph, REPO) == {A: a2.hash, B: b.hash}

    await backend.delete_file(graph, OTHER_REPO, A)
    assert await backend.all_symbols(graph, OTHER_REPO) == {}
    assert await backend.symbols_in(graph, REPO, A) == list(a2.symbols)
    assert await backend.call_edges(graph, REPO) == sorted([f_h, u_h])

    await backend.delete_file(graph, REPO, A)
    await backend.delete_file(graph, REPO, B)
    assert await backend.all_symbols(graph, REPO) == {}
    assert await backend.call_edges(graph, REPO) == []
