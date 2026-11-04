"""Resolve a review comment to the symbol it was written against."""

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentco.code.graph import ScopedCodeGraph
from agentco.code.indexer import MAX_FILE_BYTES
from agentco.code.parse import (
    FileStructure,
    Symbol,
    innermost,
    language_for,
    parse_source,
)
from agentco.github.models import GhPull, GhReviewComment

GIT_SHOW_TIMEOUT_SECONDS = 10
GitShow = Callable[[Path, str, str], bytes | None]


def git_show(checkout: Path, commit: str, path: str) -> bytes | None:
    """File content at a commit, or None when git cannot produce it."""
    if not commit.strip() or commit.startswith("-"):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "show", f"{commit}:{path}"],
            capture_output=True,
            check=False,
            timeout=GIT_SHOW_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or len(proc.stdout) > MAX_FILE_BYTES:
        return None
    return proc.stdout


def linkable(comment: GhReviewComment) -> bool:
    """Whether the comment points at a line of a language we index."""
    line = comment.original_line or comment.line
    return (
        comment.subject_type != "file"
        and line is not None
        and language_for(comment.path) is not None
    )


@dataclass(frozen=True)
class Located:
    symbol: Symbol | None
    by_fallback: bool = (
        False  # resolved against today's index, not the comment's commit
    )


class SymbolLocator:
    """Finds the function or class a comment points at, using the comment's own commit.

    When that commit is not in the checkout, a comment on a line that still exists
    (`line` set) is resolved against the current index as a best effort; an outdated
    comment (`line` is None) is left unresolved rather than guessed.
    """

    def __init__(
        self,
        repo: str,
        code: ScopedCodeGraph | None,
        checkout: Path | None,
        show: GitShow = git_show,
    ) -> None:
        self._repo = repo
        self._code = code
        self._checkout = checkout
        self._show = show
        self._parsed: dict[tuple[str, str], FileStructure | None] = {}

    @property
    def has_checkout(self) -> bool:
        return self._checkout is not None

    async def locate(self, comment: GhReviewComment, pr: GhPull) -> Symbol | None:
        return (await self.resolve(comment, pr)).symbol

    async def resolve(self, comment: GhReviewComment, pr: GhPull) -> Located:
        if not linkable(comment):
            return Located(None)
        line = comment.original_line or comment.line
        assert line is not None
        if self._checkout is not None:
            commit = (
                pr.base_sha if comment.side == "LEFT" else comment.original_commit_id
            )
            structure = await self._structure_at(commit, comment.path)
            if structure is not None:
                return Located(innermost(structure.symbols, line))
        if self._code is None or comment.line is None:
            return Located(None)
        symbol = await self._code.enclosing(self._repo, comment.path, comment.line)
        return Located(symbol, by_fallback=symbol is not None)

    async def _structure_at(self, commit: str, path: str) -> FileStructure | None:
        key = (commit, path)
        if key not in self._parsed:
            assert self._checkout is not None
            data = await asyncio.to_thread(self._show, self._checkout, commit, path)
            self._parsed[key] = None if data is None else parse_source(path, data)
        return self._parsed[key]
