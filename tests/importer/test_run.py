from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers import FIXTURES, GRAPH_AT_H1, REPO

from agentco.code.graph import InMemoryCodeGraph, ScopedCodeGraph, code_graph_for
from agentco.code.parse import parse_source
from agentco.github.client import FakeGitHub
from agentco.importer.locate import SymbolLocator
from agentco.importer.run import ImportOptions, ImportReport, run_import
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import AccessDenied, MemoryGate, ScopedMemory
from agentco.roles import Role
from agentco.store.task_store import TaskStore

NOW = datetime(2026, 11, 4, tzinfo=UTC)
CURSOR_KEY = f"import:acme:{REPO}:cursor"


def names(backend: InMemoryBackend, graph: str) -> list[str]:
    return [item.name for item in backend.items(graph)]


@dataclass
class World:
    gh: FakeGitHub
    backend: InMemoryBackend
    gate: MemoryGate
    rnd: ScopedMemory
    product: ScopedMemory
    codegraph: InMemoryCodeGraph
    code: ScopedCodeGraph
    locator: SymbolLocator
    store: TaskStore

    async def run(self, **overrides) -> ImportReport:
        opts = {
            k: overrides.pop(k) for k in ("since", "limit", "dry_run") if k in overrides
        }
        args = {
            "rnd": self.rnd,
            "product": self.product,
            "now": NOW,
            "code": self.code,
        } | overrides
        return await run_import(
            REPO,
            self.gh,
            self.store,
            args["rnd"],
            args["product"],
            self.locator,
            args["code"],
            ImportOptions(**opts),
            args["now"],
        )


@pytest.fixture
async def world(store):
    backend = InMemoryBackend()
    gate = MemoryGate("acme", backend)
    codegraph = InMemoryCodeGraph()
    code = code_graph_for("acme", Role.LIBRARIAN, codegraph)
    await code.upsert_file(REPO, parse_source("src/app/graph.py", GRAPH_AT_H1))
    locator = SymbolLocator(
        REPO, code, Path("/checkout"), lambda checkout, commit, path: GRAPH_AT_H1
    )
    return World(
        gh=FakeGitHub.from_fixtures(FIXTURES),
        backend=backend,
        gate=gate,
        rnd=gate.for_role(Role.LIBRARIAN),
        product=gate.for_role(Role.PM),
        codegraph=codegraph,
        code=code,
        locator=locator,
        store=store,
    )


ALL_RND = [
    "issue:acme-org/web#1",
    "pr:acme-org/web#3",
    "review:acme-org/web:101",
    "review:acme-org/web:106",
    "issue:acme-org/web#2",
]


async def test_full_import_writes_rnd_and_product_only(world):
    r = await world.run()
    assert (r.issues, r.features, r.pulls, r.reviews, r.written, r.complete) == (
        2,
        1,
        1,
        2,
        6,
        True,
    )
    assert r.planned == 6
    assert names(world.backend, "acme__rnd") == ALL_RND
    assert names(world.backend, "acme__product") == ["feature:acme-org/web#2"]
    assert world.backend.items("acme__qa") == []
    assert world.backend.items("acme__company") == []
    assert r.skipped == Counter(bot=3, pr_author=1, not_team=1, orphan_reply=1)
    assert r.already == 0


async def test_review_linked_to_function(world):
    r = await world.run()
    assert r.linked == 1
    assert world.codegraph.links("acme__rnd") == {
        ("review:acme-org/web:101", "src/app/graph.py", "GraphView.switch")
    }


async def test_without_code_graph_nothing_is_linked(world):
    r = await world.run(code=None)
    assert (r.written, r.linked) == (6, 0)
    assert world.codegraph.links("acme__rnd") == set()


async def test_rerun_writes_nothing(world):
    await world.run()
    r = await world.run()
    assert (r.written, r.planned, r.complete) == (0, 0, True)
    assert r.already == 1  # PR #3: the pulls endpoint has no `since`
    assert len(world.backend.items("acme__rnd")) == 5


async def test_rerun_without_cursor_skips_every_marked_item(world):
    await world.run(since=datetime(2020, 1, 1, tzinfo=UTC))
    r = await world.run(since=datetime(2020, 1, 1, tzinfo=UTC))
    assert (r.written, r.planned, r.already) == (0, 0, 6)
    assert len(world.backend.items("acme__rnd")) == 5
    assert len(world.backend.items("acme__product")) == 1


async def test_dry_run_writes_nothing(world):
    r = await world.run(dry_run=True)
    assert (r.planned, r.written) == (6, 0)
    assert (r.issues, r.features, r.pulls, r.reviews) == (2, 1, 1, 2)
    assert world.backend.items("acme__rnd") == []
    assert world.backend.items("acme__product") == []
    assert world.store.get_meta(CURSOR_KEY) is None
    assert world.store.get_meta(f"import:acme:{REPO}:issue:1") is None
    assert not [c for c in world.gh.calls if c[0] == "pull_files"]
    assert world.codegraph.links("acme__rnd") == set()


async def test_limit_takes_oldest_and_does_not_advance_cursor(world):
    r = await world.run(limit=2)
    assert (r.written, r.complete) == (2, False)
    assert names(world.backend, "acme__rnd") == [
        "issue:acme-org/web#1",
        "pr:acme-org/web#3",
    ]
    assert world.store.get_meta(CURSOR_KEY) is None
    r2 = await world.run()
    assert (r2.written, r2.complete) == (4, True)
    assert names(world.backend, "acme__rnd") == ALL_RND
    assert names(world.backend, "acme__product") == ["feature:acme-org/web#2"]


async def test_limit_equal_to_pending_is_complete(world):
    r = await world.run(limit=6)
    assert (r.written, r.complete) == (6, True)
    assert world.store.get_meta(CURSOR_KEY) == NOW.isoformat()


async def test_crash_resumes_without_duplicates(world):
    original = world.backend.add
    calls = 0

    async def flaky(graph, scope, item):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("boom")
        await original(graph, scope, item)

    world.backend.add = flaky
    with pytest.raises(RuntimeError):
        await world.run()
    assert world.store.get_meta(CURSOR_KEY) is None
    world.backend.add = original
    r = await world.run()
    assert r.written == 4
    assert names(world.backend, "acme__rnd") == ALL_RND
    assert names(world.backend, "acme__product") == ["feature:acme-org/web#2"]


async def test_since_filters_by_created_and_keeps_cursor(world):
    r = await world.run(since=datetime(2026, 1, 5, tzinfo=UTC))
    assert r.written == 3
    assert names(world.backend, "acme__rnd") == [
        "review:acme-org/web:106",
        "issue:acme-org/web#2",
    ]
    assert names(world.backend, "acme__product") == ["feature:acme-org/web#2"]
    assert world.store.get_meta(CURSOR_KEY) is None
    assert ("issues", datetime(2026, 1, 5, tzinfo=UTC)) in world.gh.calls


async def test_cursor_after_complete_run_is_used_as_since(world):
    await world.run(now=datetime(2026, 2, 1, tzinfo=UTC))
    assert world.store.get_meta(CURSOR_KEY) == "2026-02-01T00:00:00+00:00"
    world.gh.calls.clear()
    await world.run()
    assert ("issues", datetime(2026, 2, 1, tzinfo=UTC)) in world.gh.calls
    assert ("review_comments", datetime(2026, 2, 1, tzinfo=UTC)) in world.gh.calls


async def test_explicit_since_wins_over_cursor(world):
    world.store.set_meta(CURSOR_KEY, "2026-02-01T00:00:00+00:00")
    await world.run(since=datetime(2026, 1, 5, tzinfo=UTC))
    assert ("issues", datetime(2026, 1, 5, tzinfo=UTC)) in world.gh.calls


async def test_markers_are_set_for_every_written_item(world):
    await world.run()
    for kind, ident in [
        ("issue", "1"),
        ("issue", "2"),
        ("feature", "2"),
        ("pr", "3"),
        ("review", "101"),
        ("review", "106"),
    ]:
        assert world.store.get_meta(f"import:acme:{REPO}:{kind}:{ident}") == "done"


async def test_wrong_role_cannot_import(world):
    with pytest.raises(AccessDenied):
        await world.run(rnd=world.gate.for_role(Role.QA))
    assert world.backend.items("acme__rnd") == []
    assert world.store.get_meta(CURSOR_KEY) is None


async def test_failed_link_does_not_duplicate_the_episode_on_rerun(world):
    original = world.codegraph.link_episode
    calls = 0

    async def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("link boom")
        return await original(*args, **kwargs)

    world.codegraph.link_episode = flaky
    review_key = f"import:acme:{REPO}:review:101"
    with pytest.raises(RuntimeError, match="link boom"):
        await world.run()
    assert world.store.get_meta(review_key) == "added"
    assert world.codegraph.links("acme__rnd") == set()
    assert world.store.get_meta(CURSOR_KEY) is None

    r = await world.run()
    assert world.store.get_meta(review_key) == "done"
    assert names(world.backend, "acme__rnd").count("review:acme-org/web:101") == 1
    assert sorted(names(world.backend, "acme__rnd")) == sorted(ALL_RND)
    assert world.codegraph.links("acme__rnd") == {
        ("review:acme-org/web:101", "src/app/graph.py", "GraphView.switch")
    }
    assert (r.written, r.relinked, r.linked) == (3, 1, 1)
    assert r.reviews == 2


async def test_legacy_marker_value_counts_as_done(world):
    world.store.set_meta(f"import:acme:{REPO}:review:101", "1")
    r = await world.run(since=datetime(2020, 1, 1, tzinfo=UTC))
    assert (r.written, r.relinked, r.already) == (5, 0, 1)
    assert "review:acme-org/web:101" not in names(world.backend, "acme__rnd")
    assert world.codegraph.links("acme__rnd") == set()


async def test_dry_run_does_not_touch_added_markers(world):
    key = f"import:acme:{REPO}:review:101"
    world.store.set_meta(key, "added")
    r = await world.run(dry_run=True)
    assert (r.reviews, r.written, r.already) == (2, 0, 0)
    assert world.store.get_meta(key) == "added"
    assert world.backend.items("acme__rnd") == []
    assert world.codegraph.links("acme__rnd") == set()


async def test_human_review_on_bot_pr_is_imported_but_bot_pr_is_not(world):
    comment = replace(
        next(c for c in world.gh.review_comments(REPO) if c.id == 101),
        id=201,
        pr_number=5,
        created_at=datetime(2026, 1, 4, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 1, 4, 9, 30, tzinfo=UTC),
    )
    world.gh._comments.append(comment)
    r = await world.run()
    rnd_names = names(world.backend, "acme__rnd")
    assert "review:acme-org/web:201" in rnd_names
    assert "pr:acme-org/web#5" not in rnd_names
    assert (r.reviews, r.pulls, r.written) == (3, 1, 7)
    assert r.skipped == Counter(bot=3, pr_author=1, not_team=1, orphan_reply=1)


@pytest.mark.parametrize("limit", [0, -1])
async def test_limit_below_one_is_rejected(world, limit):
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await world.run(limit=limit)
    assert world.backend.items("acme__rnd") == []
    assert world.gh.calls == []
