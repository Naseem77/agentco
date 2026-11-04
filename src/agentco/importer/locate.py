"""Resolve a review comment to the symbol it was written against."""

import asyncio
import subprocess
from collections.abc import Callable
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


class SymbolLocator:
    """Finds the function or class a comment points at, using the comment's own commit."""

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

    async def locate(self, comment: GhReviewComment, pr: GhPull) -> Symbol | None:
        line = comment.original_line or comment.line
        if comment.subject_type == "file" or line is None:
            return None
        if language_for(comment.path) is None:
            return None
        if self._checkout is not None:
            commit = (
                pr.base_sha if comment.side == "LEFT" else comment.original_commit_id
            )
            structure = await self._structure_at(commit, comment.path)
            if structure is not None:
                return innermost(structure.symbols, line)
        if self._code is None:
            return None
        return await self._code.enclosing(
            self._repo, comment.path, comment.line or line
        )

    async def _structure_at(self, commit: str, path: str) -> FileStructure | None:
        key = (commit, path)
        if key not in self._parsed:
            assert self._checkout is not None
            data = await asyncio.to_thread(self._show, self._checkout, commit, path)
            self._parsed[key] = None if data is None else parse_source(path, data)
        return self._parsed[key]
