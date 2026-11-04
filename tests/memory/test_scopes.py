import pytest

from agentco.memory.scopes import ROLE_ACCESS, Access, Scope, graph_name
from agentco.roles import Role

COMPANY, PRODUCT, RND, QA = Scope.COMPANY, Scope.PRODUCT, Scope.RND, Scope.QA


def access(read, write):
    return Access(frozenset(read), frozenset(write))


def test_graph_name():
    assert graph_name("acme", Scope.RND) == "acme__rnd"


@pytest.mark.parametrize("bad", ["", "Acme", "a_b", "acme__rnd", "a b", "x" * 32])
def test_bad_tenant_rejected(bad):
    with pytest.raises(ValueError):
        graph_name(bad, Scope.RND)


def test_access_table_matches_spec():
    assert ROLE_ACCESS[Role.PM] == access({PRODUCT, COMPANY}, {PRODUCT})
    assert ROLE_ACCESS[Role.TRIAGE] == access({RND, PRODUCT}, set())
    assert ROLE_ACCESS[Role.ARCHITECT] == access({RND, PRODUCT, COMPANY}, {RND})
    for r in (Role.SENIOR, Role.JUNIOR, Role.REVIEWER):
        assert ROLE_ACCESS[r] == access({RND, PRODUCT, COMPANY}, set())
    assert ROLE_ACCESS[Role.QA] == access({QA, PRODUCT, COMPANY}, {QA})
    assert ROLE_ACCESS[Role.LEAD] == access(set(Scope), set())
    assert ROLE_ACCESS[Role.LIBRARIAN] == access(set(Scope), {RND, QA, COMPANY})
    assert ROLE_ACCESS[Role.HUMAN] == access(set(Scope), {COMPANY, RND})


def test_every_role_has_access_entry():
    assert set(ROLE_ACCESS) == set(Role)
