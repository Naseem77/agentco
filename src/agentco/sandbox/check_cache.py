"""Passing pre-flight results, reused while nothing they depend on has changed.

A key holds the check's name, the exact command (or rule set) it ran, the tree
it ran on and the base commit it was measured against. The tree, not the
commit, so a rebase that changes nothing still hits; the base commit, not the
ref, so a moved base never reuses a stale pass. Only passes are stored: a
failure, flaky or not, always runs again.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentco.sandbox.worktree import Worktree, _git

if TYPE_CHECKING:
    from agentco.workflow.preflight import CheckResult

CacheKey = tuple[str, str, str, str]


def tree_sha(wt: Worktree) -> str:
    return _git(wt.path, "rev-parse", "HEAD^{tree}").strip()


def base_sha(wt: Worktree, base: str) -> str:
    return _git(wt.path, "rev-parse", "--verify", f"{base}^{{commit}}").strip()


@dataclass
class CheckCache:
    _results: dict[CacheKey, "CheckResult"] = field(default_factory=dict)

    def key(self, name: str, command: str, tree: str, base: str) -> CacheKey:
        return (name, command, tree, base)

    def get(self, key: CacheKey) -> "CheckResult | None":
        return self._results.get(key)

    def put(self, key: CacheKey, result: "CheckResult") -> None:
        if result.ok:
            self._results[key] = result

    def find(
        self, name: str, tree: str, base: str, match: Callable[[str], bool]
    ) -> "CheckResult | None":
        """A stored pass of `name` on this tree and base whose command matches."""
        for (n, command, t, b), result in self._results.items():
            if (n, t, b) == (name, tree, base) and match(command):
                return result
        return None
