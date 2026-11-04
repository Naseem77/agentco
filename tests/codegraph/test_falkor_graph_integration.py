import pytest
from code_graph_contract import check_contract

from agentco.code.falkor_graph import build_falkor_code_graph
from agentco.code.parse import parse_source

pytestmark = pytest.mark.integration


async def count(db_graph, query: str) -> int:
    return (await db_graph.query(query)).result_set[0][0]


async def test_falkor_code_graph_honours_contract(falkor_cfg):
    backend = build_falkor_code_graph(falkor_cfg)
    graph = f"{falkor_cfg.tenant}__rnd"
    db_graph = backend.db.select_graph(graph)
    try:
        await check_contract(
            backend,
            graph,
            add_episode=lambda name: db_graph.query(
                "CREATE (:Episodic {name: $n})", {"n": name}
            ),
            count_links=lambda: count(
                db_graph, "MATCH (:Episodic)-[r:ABOUT]->(:Code) RETURN count(r)"
            ),
        )
    finally:
        await db_graph.delete()


async def test_graphiti_entities_are_never_touched(falkor_cfg):
    backend = build_falkor_code_graph(falkor_cfg)
    graph = f"{falkor_cfg.tenant}__rnd"
    db_graph = backend.db.select_graph(graph)
    repo, path = "o/r", "src/a.py"
    fs = parse_source(path, b"def f():\n    pass\n")
    assert fs is not None
    try:
        await db_graph.query(
            "CREATE (:Entity:File {repo: $repo, path: $path}),"
            " (:Entity:Function {repo: $repo, path: $path, qualname: 'f'})",
            {"repo": repo, "path": path},
        )
        await backend.ensure_schema(graph)
        await backend.upsert_file(graph, repo, fs)
        await backend.delete_file(graph, repo, path)
        assert await count(db_graph, "MATCH (n:Entity) RETURN count(n)") == 2
        assert await count(db_graph, "MATCH (n:Code) RETURN count(n)") == 0
    finally:
        await db_graph.delete()
