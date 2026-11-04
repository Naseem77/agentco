from datetime import UTC, datetime

import pytest

from agentco.memory.backend import MemoryItem
from agentco.memory.graphiti_backend import build_graphiti_backend
from agentco.memory.scopes import Scope

pytestmark = pytest.mark.integration


def item(body: str) -> MemoryItem:
    return MemoryItem(
        name="itest",
        body=body,
        source_description="integration test",
        reference_time=datetime.now(UTC),
    )


async def test_add_then_search_stays_in_graph(cfg):
    b = build_graphiti_backend(cfg)
    rnd, qa = f"{cfg.tenant}__rnd", f"{cfg.tenant}__qa"
    await b.ensure_graph(rnd)
    await b.ensure_graph(qa)
    await b.add(
        rnd, Scope.RND, item("Review rule: always debounce graph switch in GraphView")
    )
    found = await b.search([rnd], "graph switch", 5)
    assert found and all(f.graph == rnd for f in found)
    assert await b.search([qa], "graph switch", 5) == []
    both = await b.search([rnd, qa], "graph switch", 5)
    assert both and all(f.graph == rnd for f in both)
