from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_lease_exclusive_until_expiry(store):
    assert store.try_lease("k", "a", 60, now=T0)
    assert not store.try_lease("k", "b", 60, now=T0)
    assert not store.try_lease("k", "b", 60, now=T0 + timedelta(seconds=59))
    assert store.try_lease("k", "b", 60, now=T0 + timedelta(seconds=61))


def test_same_owner_can_renew(store):
    assert store.try_lease("k", "a", 60, now=T0)
    assert store.try_lease("k", "a", 60, now=T0 + timedelta(seconds=1))
    assert not store.try_lease("k", "b", 60, now=T0 + timedelta(seconds=2))


def test_leases_are_per_key(store):
    assert store.try_lease("k1", "a", 60) and store.try_lease("k2", "b", 60)


def test_release_only_by_owner(store):
    assert store.try_lease("k", "a", 60, now=T0)
    store.release_lease("k", "b")
    assert not store.try_lease("k", "b", 60, now=T0)
    store.release_lease("k", "a")
    assert store.try_lease("k", "b", 60, now=T0)


def test_release_of_unknown_lease_is_a_noop(store):
    store.release_lease("nope", "a")
