from datetime import UTC, datetime

from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.memory.seed import (
    DEFAULT_ESCALATION_RULES,
    SEED_POLICIES,
    SEED_VERSION,
    seed_company,
)
from agentco.roles import Role

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def _human(backend):
    return MemoryGate("acme", backend).for_role(Role.HUMAN)


def test_seed_catalogue_shape():
    assert [slug for slug, _ in SEED_POLICIES] == [
        "every-fix-is-new-code",
        "fix-cause-with-one-rule",
        "test-the-neighbours",
        "blind-review-before-push",
        "keep-fixes-small",
        "run-everything-locally",
        "stop-when-not-real",
    ]
    categories = [c for _, c, _ in DEFAULT_ESCALATION_RULES]
    assert [categories.count(c) for c in ("critical", "new", "stuck")] == [6, 4, 1]
    assert len(categories) == 11
    slugs = [s for s, _ in SEED_POLICIES] + [s for s, _, _ in DEFAULT_ESCALATION_RULES]
    assert len(set(slugs)) == len(slugs)
    assert all(text.strip() for _, text in SEED_POLICIES)
    assert "neighbours" in dict(SEED_POLICIES)["test-the-neighbours"]
    assert SEED_VERSION == "1"


async def test_seed_writes_policies_and_rules_once(store):
    backend = InMemoryBackend()
    mem = _human(backend)
    assert await seed_company(mem, store, NOW) is True
    assert await seed_company(mem, store, NOW) is False
    items = backend.items("acme__company")
    assert len(items) == 7 + 11
    assert {i.name for i in items} == {f"policy:{s}" for s, _ in SEED_POLICIES} | {
        f"escalation:{s}" for s, _, _ in DEFAULT_ESCALATION_RULES
    }
    assert store.get_meta("seed_version") == SEED_VERSION


async def test_seed_only_touches_company(store):
    backend = InMemoryBackend()
    await seed_company(_human(backend), store, NOW)
    for s in ("product", "rnd", "qa"):
        assert backend.items(f"acme__{s}") == []


async def test_seed_failure_leaves_meta_unset(store):
    class Failing(InMemoryBackend):
        async def add(self, graph, scope, item):
            raise ConnectionError("down")

    try:
        await seed_company(_human(Failing()), store, NOW)
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected ConnectionError")
    assert store.get_meta("seed_version") is None


async def test_seed_bodies_carry_their_type(store):
    backend = InMemoryBackend()
    await seed_company(_human(backend), store, NOW)
    bodies = {i.name: i.body for i in backend.items("acme__company")}
    for slug, text in SEED_POLICIES:
        assert bodies[f"policy:{slug}"] == f"Company policy ({slug}): {text}"
    for slug, category, text in DEFAULT_ESCALATION_RULES:
        assert (
            bodies[f"escalation:{slug}"]
            == f"Escalation rule (category={category}): {text}"
        )


async def test_seed_partial_failure_resumes_without_duplicates(store):
    class FailsOnThird(InMemoryBackend):
        calls = 0

        async def add(self, graph, scope, item):
            FailsOnThird.calls += 1
            if FailsOnThird.calls == 3:
                raise ConnectionError("down")
            await super().add(graph, scope, item)

    backend = FailsOnThird()
    mem = _human(backend)
    try:
        await seed_company(mem, store, NOW)
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected ConnectionError")
    assert len(backend.items("acme__company")) == 2
    assert store.get_meta("seed_version") is None

    assert await seed_company(mem, store, NOW) is True
    names = [i.name for i in backend.items("acme__company")]
    assert len(names) == 7 + 11
    assert len(set(names)) == len(names)
    assert store.get_meta("seed_version") == SEED_VERSION
