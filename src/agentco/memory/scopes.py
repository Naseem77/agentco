import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import NamedTuple

from agentco.roles import Role

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class Scope(StrEnum):
    COMPANY = "company"
    PRODUCT = "product"
    RND = "rnd"
    QA = "qa"


class Access(NamedTuple):
    read: frozenset[Scope]
    write: frozenset[Scope]


def graph_name(tenant: str, scope: Scope) -> str:
    if not TENANT_RE.fullmatch(tenant):
        raise ValueError(
            f"invalid tenant name {tenant!r}: must match {TENANT_RE.pattern}"
        )
    return f"{tenant}__{Scope(scope).value}"


def _access(read: set[Scope], write: set[Scope]) -> Access:
    return Access(frozenset(read), frozenset(write))


_ALL = set(Scope)
_ENGINEERING_READ = {Scope.RND, Scope.PRODUCT, Scope.COMPANY}

ROLE_ACCESS: Mapping[Role, Access] = MappingProxyType(
    {
        Role.PM: _access({Scope.PRODUCT, Scope.COMPANY}, {Scope.PRODUCT}),
        Role.TRIAGE: _access({Scope.RND, Scope.PRODUCT}, set()),
        Role.ARCHITECT: _access(_ENGINEERING_READ, {Scope.RND}),
        Role.SENIOR: _access(_ENGINEERING_READ, set()),
        Role.JUNIOR: _access(_ENGINEERING_READ, set()),
        Role.REVIEWER: _access(_ENGINEERING_READ, set()),
        Role.QA: _access({Scope.QA, Scope.PRODUCT, Scope.COMPANY}, {Scope.QA}),
        Role.LEAD: _access(_ALL, set()),
        Role.LIBRARIAN: _access(_ALL, {Scope.RND, Scope.QA, Scope.COMPANY}),
        Role.HUMAN: _access(_ALL, {Scope.COMPANY, Scope.RND}),
    }
)
