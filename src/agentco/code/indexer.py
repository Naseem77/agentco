"""Incremental indexer: git-tracked working-tree files into the code graph."""

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentco.code.graph import ScopedCodeGraph, resolve_calls
from agentco.code.parse import content_hash, language_for, parse_source

MAX_FILE_BYTES = 1_000_000


class NotAGitRepo(ValueError):
    pass


@dataclass(frozen=True)
class IndexReport:
    indexed: int
    unchanged: int
    deleted: int
    skipped: int
    calls: int


def tracked_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise NotAGitRepo("git not found; install git to index a repository") from e
    if result.returncode != 0:
        raise NotAGitRepo(f"{root} is not a git repository")
    names = result.stdout.decode("utf-8", errors="replace").split("\0")
    # Unmerged paths are listed once per index stage.
    return list(dict.fromkeys(name for name in names if name))


class _Unreadable(Exception):
    """The file may still be indexable (permissions, EIO, ...); keep its graph data."""


def _read_indexable(path: Path) -> bytes | None:
    """File bytes; None if missing, not a regular file, or over the size cap.

    Raises _Unreadable for any other OS error.
    """
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise _Unreadable from e
    return data if len(data) <= MAX_FILE_BYTES else None


async def index_repo(repo: str, root: Path, code: ScopedCodeGraph) -> IndexReport:
    tracked = [p for p in tracked_files(root) if language_for(p) is not None]
    await code.ensure_schema()
    known = await code.file_hashes(repo)

    indexed = unchanged = skipped = 0
    present: set[str] = set()
    for path in tracked:
        try:
            data = _read_indexable(root / path)
        except _Unreadable:
            present.add(path)
            skipped += 1
            continue
        if data is None:
            skipped += 1
            continue
        present.add(path)
        if known.get(path) == content_hash(data):
            unchanged += 1
            continue
        fs = parse_source(path, data)
        if fs is None:
            skipped += 1
            present.discard(path)
            continue
        await code.upsert_file(repo, fs)
        indexed += 1

    # Files that vanished, were untracked, or are no longer indexable leave the graph.
    gone = sorted(set(known) - present)
    for path in gone:
        await code.delete_file(repo, path)

    calls = 0
    if indexed or gone:
        edges = resolve_calls(await code.all_symbols(repo))
        await code.replace_calls(repo, edges)
        calls = len(edges)
    return IndexReport(indexed, unchanged, len(gone), skipped, calls)
