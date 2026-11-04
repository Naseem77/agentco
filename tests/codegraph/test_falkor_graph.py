import re
from types import SimpleNamespace

import pytest

from agentco.code import falkor_graph
from agentco.code.falkor_graph import FalkorCodeGraph, build_falkor_code_graph
from agentco.code.graph import CallEdge
from agentco.code.parse import Symbol, parse_source
from agentco.config import TenantConfig

REPO = "o/r"
PATH = "src/app.py"
SOURCE = b"""class View:
    def load(self):
        return helper()

    def show(self):
        self.load()


def helper():
    pass
"""


class FakeGraph:
    def __init__(self, respond=None, fail=None):
        self.calls: list[tuple[str, dict]] = []
        self._respond = respond or (lambda q, p: [])
        self._fail = fail

    async def query(self, q, params=None):
        self.calls.append((q, params or {}))
        if self._fail is not None:
            self._fail(q)
        return SimpleNamespace(result_set=self._respond(q, params or {}))


class FakeDB:
    def __init__(self, graph: FakeGraph):
        self.graph = graph
        self.selected: list[str] = []

    def select_graph(self, name):
        self.selected.append(name)
        return self.graph


def backend(respond=None, fail=None) -> tuple[FalkorCodeGraph, FakeGraph, FakeDB]:
    graph = FakeGraph(respond, fail)
    db = FakeDB(graph)
    return FalkorCodeGraph(db), graph, db


def find(graph: FakeGraph, *needles: str) -> list[tuple[str, dict]]:
    return [(q, p) for q, p in graph.calls if all(n in q for n in needles)]


def structure():
    fs = parse_source(PATH, SOURCE)
    assert fs is not None
    return fs


def test_build_uses_tenant_host_and_port(monkeypatch):
    seen = {}

    def fake_db(**kwargs):
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(falkor_graph, "FalkorDB", fake_db)
    cfg = TenantConfig(
        tenant="acme", falkordb_host="db.internal", falkordb_port=7777, roles={}
    )
    built = build_falkor_code_graph(cfg)
    assert seen == {"host": "db.internal", "port": 7777}
    assert built.db == "client"


async def test_ensure_schema_creates_three_indexes_on_the_named_graph():
    b, graph, db = backend()
    await b.ensure_schema("acme__rnd")
    assert db.selected == ["acme__rnd"] * 3
    assert [q for q, _ in graph.calls] == [
        "CREATE INDEX FOR (n:Code) ON (n.path)",
        "CREATE INDEX FOR (n:Code) ON (n.repo)",
        "CREATE INDEX FOR (e:Episodic) ON (e.name)",
    ]


async def test_ensure_schema_ignores_only_already_indexed():
    def fail(q):
        raise RuntimeError("Attribute 'path' is already indexed")

    b, graph, _ = backend(fail=fail)
    await b.ensure_schema("g")
    assert len(graph.calls) == 3

    def boom(q):
        raise RuntimeError("connection reset")

    b, graph, _ = backend(fail=boom)
    with pytest.raises(RuntimeError) as exc:
        await b.ensure_schema("g")
    assert str(exc.value) == "connection reset"
    assert len(graph.calls) == 1


async def test_file_hashes_maps_path_to_hash_for_the_repo():
    b, graph, _ = backend(lambda q, p: [["a.py", "h1"], ["b.py", "h2"]])
    assert await b.file_hashes("g", REPO) == {"a.py": "h1", "b.py": "h2"}
    ((q, params),) = graph.calls
    assert ":Code" in q and "$repo" in q
    assert params == {"repo": REPO}


async def test_upsert_file_runs_steps_in_order_and_commits_hash_last():
    b, graph, db = backend()
    fs = structure()
    await b.upsert_file("g", REPO, fs)
    assert db.selected == ["g"] * 8
    steps = [q for q, _ in graph.calls]
    assert len(steps) == 8
    assert "MERGE" in steps[0] and "kind: 'File'" in steps[0]
    assert "DETACH DELETE" in steps[1]
    assert "SET s:Class" in steps[2]
    assert "SET s:Function" in steps[3]
    assert "DELETE r" in steps[4] and "DEFINES" in steps[4]
    assert "CREATE (f)-[:DEFINES]->(s)" in steps[5]
    assert "CREATE (p)-[:DEFINES]->(s)" in steps[6]
    assert "SET f.hash = $hash" in steps[7]


async def test_upsert_file_clears_the_old_hash_before_touching_symbols():
    b, graph, _ = backend()
    await b.upsert_file("g", REPO, structure())
    first, _ = graph.calls[0]
    assert first == (
        "MERGE (f:Code {repo: $repo, path: $path, kind: 'File'}) "
        "SET f:File, f.name = $name, f.language = $language, f.hash = null"
    )


async def test_upsert_file_sets_hash_only_after_symbols_are_written():
    b, graph, _ = backend()
    fs = structure()
    await b.upsert_file("g", REPO, fs)
    hash_calls = [i for i, (_, p) in enumerate(graph.calls) if fs.hash in p.values()]
    assert hash_calls == [len(graph.calls) - 1]


async def test_upsert_file_file_node_params():
    b, graph, _ = backend()
    fs = structure()
    await b.upsert_file("g", REPO, fs)
    _, params = graph.calls[0]
    assert params == {
        "repo": REPO,
        "path": PATH,
        "name": "app.py",
        "language": "python",
    }


async def test_upsert_file_deletes_only_symbols_missing_from_the_new_set():
    b, graph, _ = backend()
    await b.upsert_file("g", REPO, structure())
    q, params = graph.calls[1]
    assert "(s:Code {repo: $repo, path: $path})" in q
    assert params == {
        "repo": REPO,
        "path": PATH,
        "keep": [
            "Class:View",
            "Function:View.load",
            "Function:View.show",
            "Function:helper",
        ],
    }


async def test_upsert_file_symbol_rows_carry_all_properties():
    b, graph, _ = backend()
    await b.upsert_file("g", REPO, structure())
    classes = graph.calls[2][1]["rows"]
    functions = graph.calls[3][1]["rows"]
    assert classes == [
        {
            "qualname": "View",
            "name": "View",
            "kind": "Class",
            "start_line": 1,
            "end_line": 6,
            "calls": [],
        }
    ]
    assert [(r["qualname"], r["calls"]) for r in functions] == [
        ("View.load", ["helper"]),
        ("View.show", ["load"]),
        ("helper", []),
    ]
    assert graph.calls[2][1]["repo"] == REPO and graph.calls[2][1]["path"] == PATH


async def test_upsert_file_links_top_level_to_file_and_nested_to_parent():
    b, graph, _ = backend()
    await b.upsert_file("g", REPO, structure())
    top = graph.calls[5][1]["rows"]
    nested = graph.calls[6][1]["rows"]
    assert top == [{"qualname": "View"}, {"qualname": "helper"}]
    assert nested == [
        {"parent": "View", "qualname": "View.load"},
        {"parent": "View", "qualname": "View.show"},
    ]


async def test_upsert_file_with_no_symbols_skips_symbol_queries():
    b, graph, _ = backend()
    fs = parse_source("empty.py", b"x = 1\n")
    assert fs is not None
    await b.upsert_file("g", REPO, fs)
    steps = [q for q, _ in graph.calls]
    assert len(steps) == 4
    assert graph.calls[1][1]["keep"] == []
    assert "DELETE r" in steps[2]
    assert fs.hash in graph.calls[3][1].values()


async def test_user_values_are_parameters_never_interpolated():
    repo = "o/r' MATCH (n) DETACH DELETE n //"
    path = 'weird"name\\x.py'
    episode = "ep' OR 1=1"
    fs = parse_source(path, b"def fn():\n    other()\n")
    assert fs is not None
    b, graph, _ = backend(respond_by_shape)
    await b.file_hashes("g", repo)
    await b.upsert_file("g", repo, fs)
    await b.replace_calls("g", repo, [CallEdge(path, "fn", path, "fn")])
    await b.call_edges("g", repo)
    await b.symbols_in("g", repo, path)
    await b.all_symbols("g", repo)
    await b.link_episode("g", repo, episode, path, "fn")
    await b.delete_file("g", repo, path)
    for q, params in graph.calls:
        assert params["repo"] == repo
        for text in ("o/r", "DETACH DELETE n //", "weird", "x.py", "OR 1=1", "fn"):
            assert text not in q, q
    assert [p["episode"] for _, p in find(graph, "ABOUT")] == [episode]


def respond_by_shape(q, params):
    if "RETURN f.path, f.hash" in q:
        return [["a.py", "h"]]
    if "RETURN s.path" in q:
        return [["a.py", "Function", "f", "f", 1, 2, []]]
    if "RETURN s.kind" in q:
        return [["Function", "f", "f", 1, 2, []]]
    if "RETURN a.path" in q:
        return [[PATH, "helper", PATH, "helper"]]
    return [[1]]


async def test_every_code_node_pattern_is_labelled_and_repo_scoped():
    b, graph, _ = backend(respond_by_shape)
    await b.upsert_file("g", REPO, structure())
    await b.replace_calls("g", REPO, [CallEdge(PATH, "helper", PATH, "helper")])
    await b.call_edges("g", REPO)
    await b.link_episode("g", REPO, "ep", PATH, "helper")
    await b.delete_file("g", REPO, PATH)
    await b.symbols_in("g", REPO, PATH)
    await b.all_symbols("g", REPO)
    await b.file_hashes("g", REPO)
    checked = 0
    for q, params in graph.calls:
        assert params["repo"] == REPO
        for _, label, props in re.findall(r"\((\w*)(:\w+)?(?: (\{[^}]*\}))?\)", q):
            if not label and not props:
                continue  # a bare reference to an already matched variable
            if label == ":Episodic":
                continue
            checked += 1
            assert label == ":Code", q
            assert "repo: $repo" in props, q
    assert checked == 23


async def test_delete_file_detach_deletes_every_code_node_of_that_file():
    b, graph, _ = backend()
    await b.delete_file("g", REPO, PATH)
    ((q, params),) = graph.calls
    assert q == "MATCH (n:Code {repo: $repo, path: $path}) DETACH DELETE n"
    assert params == {"repo": REPO, "path": PATH}


async def test_symbols_in_rebuilds_symbols_with_tuple_calls():
    rows = [
        ["Class", "View", "View", 1, 6, []],
        ["Function", "View.load", "load", 2, 3, ["helper"]],
    ]
    b, graph, _ = backend(lambda q, p: rows)
    assert await b.symbols_in("g", REPO, PATH) == [
        Symbol("Class", "View", "View", 1, 6, ()),
        Symbol("Function", "load", "View.load", 2, 3, ("helper",)),
    ]
    assert graph.calls[0][1] == {"repo": REPO, "path": PATH}


async def test_all_symbols_groups_by_path_and_keeps_files_without_symbols():
    def respond(q, p):
        if "kind: 'File'" in q:
            return [["a.py", "h"], ["empty.py", "h2"]]
        return [
            ["a.py", "Function", "f", "f", 1, 2, ["g"]],
            ["a.py", "Function", "g", "g", 4, 5, []],
        ]

    b, _, _ = backend(respond)
    assert await b.all_symbols("g", REPO) == {
        "a.py": [
            Symbol("Function", "f", "f", 1, 2, ("g",)),
            Symbol("Function", "g", "g", 4, 5, ()),
        ],
        "empty.py": [],
    }


async def test_replace_calls_clears_repo_edges_before_writing_new_ones():
    b, graph, _ = backend()
    edges = [
        CallEdge("a.py", "f", "b.py", "g"),
        CallEdge("a.py", "f", "b.py", "g"),
        CallEdge("b.py", "g", "b.py", "h"),
    ]
    await b.replace_calls("g", REPO, edges)
    (clear, clear_params), (write, write_params) = graph.calls
    assert "DELETE r" in clear and "[r:CALLS]" in clear
    assert clear_params == {"repo": REPO}
    assert "MERGE (a)-[:CALLS]->(b)" in write
    assert write_params == {
        "repo": REPO,
        "edges": [
            {"cp": "a.py", "c": "f", "tp": "b.py", "t": "g"},
            {"cp": "b.py", "c": "g", "tp": "b.py", "t": "h"},
        ],
    }


async def test_replace_calls_with_no_edges_only_clears():
    b, graph, _ = backend()
    await b.replace_calls("g", REPO, [])
    assert len(graph.calls) == 1 and "DELETE r" in graph.calls[0][0]


async def test_call_edges_are_sorted():
    rows = [["b.py", "g", "b.py", "h"], ["a.py", "f", "b.py", "g"]]
    b, _, _ = backend(lambda q, p: rows)
    assert await b.call_edges("g", REPO) == [
        CallEdge("a.py", "f", "b.py", "g"),
        CallEdge("b.py", "g", "b.py", "h"),
    ]


async def test_link_episode_false_when_nothing_was_linked():
    b, graph, _ = backend(lambda q, p: [[0]])
    assert not await b.link_episode("g", REPO, "ep", PATH, "nope")
    assert len(graph.calls) == 1


async def test_link_episode_merges_about_edge_from_episodic_by_name():
    b, graph, _ = backend(lambda q, p: [[1]])
    assert await b.link_episode("g", REPO, "review:o/r:1", PATH, "View.show")
    ((q, params),) = graph.calls
    assert "(e:Episodic {name: $episode})" in q
    assert "MERGE (e)-[r:ABOUT]->(s)" in q
    assert params == {
        "repo": REPO,
        "path": PATH,
        "qualname": "View.show",
        "episode": "review:o/r:1",
    }
