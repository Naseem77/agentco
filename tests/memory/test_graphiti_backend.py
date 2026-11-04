import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from graphiti_core.nodes import EpisodeType

from agentco.config import TenantConfig
from agentco.memory import graphiti_backend
from agentco.memory.backend import MemoryItem
from agentco.memory.entities import ENTITY_TYPES
from agentco.memory.graphiti_backend import GraphitiBackend, build_graphiti_backend
from agentco.memory.scopes import Scope

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def edge(fact: str, group_id: str, uuid: str = "u") -> SimpleNamespace:
    return SimpleNamespace(
        fact=fact, group_id=group_id, uuid=uuid, valid_at=NOW, invalid_at=None
    )


def fake_graphiti(edges_by_graph: dict[str, list]) -> MagicMock:
    """Graphiti double: one driver per database.

    Like graphiti_core's @handle_multiple_group_ids, search_ routes a single
    group_id to that graph's database regardless of the driver= argument.
    """
    drivers: dict[str, MagicMock] = {}

    def clone(database: str) -> MagicMock:
        driver = drivers.setdefault(database, MagicMock(name=database))
        driver.database = database
        driver.build_indices_and_constraints = AsyncMock()
        return driver

    async def search_(query, config, group_ids, driver=None):
        assert len(group_ids) == 1
        return SimpleNamespace(edges=list(edges_by_graph.get(group_ids[0], [])))

    graphiti = MagicMock()
    graphiti.driver.clone.side_effect = clone
    graphiti.add_episode = AsyncMock()
    graphiti.search_ = AsyncMock(side_effect=search_)
    graphiti.drivers = drivers
    return graphiti


def item() -> MemoryItem:
    return MemoryItem(
        name="n", body="body", source_description="src", reference_time=NOW
    )


async def test_add_passes_group_and_scope_entity_types():
    graphiti = fake_graphiti({})
    await GraphitiBackend(graphiti).add("acme__qa", Scope.QA, item())
    graphiti.add_episode.assert_awaited_once_with(
        name="n",
        episode_body="body",
        source_description="src",
        reference_time=NOW,
        source=EpisodeType.text,
        group_id="acme__qa",
        entity_types=ENTITY_TYPES[Scope.QA],
    )


async def test_ensure_graph_builds_indices_on_that_graphs_database():
    graphiti = fake_graphiti({})
    await GraphitiBackend(graphiti).ensure_graph("acme__rnd")
    graphiti.driver.clone.assert_called_with(database="acme__rnd")
    graphiti.drivers["acme__rnd"].build_indices_and_constraints.assert_awaited_once()


async def test_search_queries_one_graph_per_call():
    graphiti = fake_graphiti(
        {
            "acme__rnd": [edge("rnd fact", "acme__rnd")],
            "acme__qa": [edge("qa fact", "acme__qa")],
        }
    )
    found = await GraphitiBackend(graphiti).search(
        ["acme__rnd", "acme__qa"], "fact", 10
    )
    assert {(f.text, f.graph) for f in found} == {
        ("rnd fact", "acme__rnd"),
        ("qa fact", "acme__qa"),
    }
    assert sorted(c.kwargs["group_ids"] for c in graphiti.search_.await_args_list) == [
        ["acme__qa"],
        ["acme__rnd"],
    ]
    for call in graphiti.search_.await_args_list:
        assert call.args[0] == "fact" or call.kwargs.get("query") == "fact"
        assert len(call.kwargs["group_ids"]) == 1
        assert call.kwargs["config"].limit == 10


async def test_search_maps_fact_fields():
    invalid = datetime(2026, 2, 1, tzinfo=UTC)
    e = edge("f", "acme__rnd", uuid="abc")
    e.invalid_at = invalid
    [fact] = await GraphitiBackend(fake_graphiti({"acme__rnd": [e]})).search(
        ["acme__rnd"], "q", 5
    )
    assert (fact.text, fact.graph, fact.uuid) == ("f", "acme__rnd", "abc")
    assert (fact.valid_at, fact.invalid_at) == (NOW, invalid)


async def test_search_interleaves_graphs_and_truncates_to_limit():
    graphiti = fake_graphiti(
        {
            "g1": [edge(f"a{i}", "g1", f"a{i}") for i in range(5)],
            "g2": [edge(f"b{i}", "g2", f"b{i}") for i in range(5)],
        }
    )
    found = await GraphitiBackend(graphiti).search(["g1", "g2"], "q", 4)
    assert [f.uuid for f in found] == ["a0", "b0", "a1", "b1"]


async def test_search_dedupes_graphs_and_handles_empty():
    graphiti = fake_graphiti({"g1": [edge("x", "g1")]})
    backend = GraphitiBackend(graphiti)
    assert await backend.search([], "q", 5) == []
    assert len(await backend.search(["g1", "g1"], "q", 5)) == 1
    assert graphiti.search_.await_count == 1


async def test_search_does_not_share_config_between_calls():
    graphiti = fake_graphiti({"g1": []})
    backend = GraphitiBackend(graphiti)
    await asyncio.gather(backend.search(["g1"], "q", 3), backend.search(["g1"], "q", 7))
    limits = sorted(c.kwargs["config"].limit for c in graphiti.search_.await_args_list)
    assert limits == [3, 7]


def test_build_graphiti_backend_uses_cfg_host_and_port(monkeypatch):
    # The real FalkorDriver connects in its constructor, so stub both classes.
    driver_cls, graphiti_cls = MagicMock(), MagicMock()
    monkeypatch.setattr(graphiti_backend, "FalkorDriver", driver_cls)
    monkeypatch.setattr(graphiti_backend, "Graphiti", graphiti_cls)
    cfg = TenantConfig(
        tenant="acme", falkordb_host="db.example", falkordb_port=6400, roles={}
    )
    backend = build_graphiti_backend(cfg)
    driver_cls.assert_called_once_with(host="db.example", port=6400)
    graphiti_cls.assert_called_once_with(graph_driver=driver_cls.return_value)
    assert backend._graphiti is graphiti_cls.return_value
