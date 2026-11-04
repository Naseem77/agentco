"""Behaviour every CodeGraphBackend must honour; shared by the fake and FalkorDB tests."""

from collections.abc import Awaitable, Callable

from agentco.code.graph import CallEdge, CodeGraphBackend
from agentco.code.parse import parse_source

REPO = "o/r"
PATH = "src/app/graph.py"
UTIL = "src/app/util.py"
UTIL_SRC = b"def show(g):\n    return g\n"

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
