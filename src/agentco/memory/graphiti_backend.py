import asyncio
import os
from itertools import chain, zip_longest

from graphiti_core import Graphiti
from graphiti_core.cross_encoder import OpenAIRerankerClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client import LLMConfig, OpenAIClient
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF

from agentco.config import TenantConfig
from agentco.memory.backend import Fact, MemoryItem
from agentco.memory.entities import ENTITY_TYPES
from agentco.memory.scopes import Scope


class GraphitiBackend:
    """MemoryBackend over Graphiti/FalkorDB.

    Every graph is its own FalkorDB database. Graphiti routes a single group_id
    to that graph itself: add_episode switches to the group_id database on a
    per-call clone, and search_ is wrapped by @handle_multiple_group_ids, which
    for one group_id clones the driver onto that group's database and overrides
    any driver= passed in. So we search one graph per call (a single group_id)
    and merge the per-graph results ourselves. The driver= we pass is redundant
    (the wrapper replaces it) and is kept only for the case where the wrapper
    sees the graph already is the driver's default database.
    """

    def __init__(self, graphiti: Graphiti) -> None:
        self._graphiti = graphiti
        self._drivers: dict[str, GraphDriver] = {}

    def _driver_for(self, graph: str) -> GraphDriver:
        if graph not in self._drivers:
            self._drivers[graph] = self._graphiti.driver.clone(database=graph)
        return self._drivers[graph]

    async def ensure_graph(self, graph: str) -> None:
        await self._driver_for(graph).build_indices_and_constraints()

    async def add(self, graph: str, scope: Scope, item: MemoryItem) -> None:
        await self._graphiti.add_episode(
            name=item.name,
            episode_body=item.body,
            source_description=item.source_description,
            reference_time=item.reference_time,
            source=EpisodeType.text,
            group_id=graph,
            entity_types=ENTITY_TYPES[scope],
        )

    async def search(self, graphs: list[str], query: str, limit: int) -> list[Fact]:
        unique = list(dict.fromkeys(graphs))
        per_graph = await asyncio.gather(
            *(self._search_graph(graph, query, limit) for graph in unique)
        )
        # Scores from separate databases are not comparable, so take the best
        # hit of each graph in turn rather than letting one graph crowd out the rest.
        interleaved = (
            fact
            for fact in chain.from_iterable(zip_longest(*per_graph))
            if fact is not None
        )
        return list(interleaved)[:limit]

    async def _search_graph(self, graph: str, query: str, limit: int) -> list[Fact]:
        # Graphiti.search() mutates a module-level shared SearchConfig.limit, so
        # use search_() with a private copy of the config.
        results = await self._graphiti.search_(
            query,
            config=EDGE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit}),
            group_ids=[graph],
            driver=self._driver_for(graph),
        )
        return [
            Fact(
                text=e.fact,
                graph=e.group_id,
                uuid=e.uuid,
                valid_at=e.valid_at,
                invalid_at=e.invalid_at,
            )
            for e in results.edges
        ]


def build_graphiti_backend(cfg: TenantConfig) -> GraphitiBackend:
    # Graphiti's own defaults include retired models, so always pass ours.
    models = cfg.memory_models
    model = os.environ.get("AGENTCO_GRAPHITI_MODEL") or models.model
    small_model = os.environ.get("AGENTCO_GRAPHITI_SMALL_MODEL") or models.small_model
    driver = FalkorDriver(host=cfg.falkordb_host, port=cfg.falkordb_port)
    return GraphitiBackend(
        Graphiti(
            graph_driver=driver,
            llm_client=OpenAIClient(LLMConfig(model=model, small_model=small_model)),
            embedder=OpenAIEmbedder(
                OpenAIEmbedderConfig(embedding_model=models.embedder)
            ),
            cross_encoder=OpenAIRerankerClient(config=LLMConfig(model=small_model)),
        )
    )
