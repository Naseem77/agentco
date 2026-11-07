"""Warm worktrees: prepared ahead of time, claimed by one task each, never reused.

An entry is a detached worktree at `.agentco/pool/<name>/` on one `main` commit,
with the repo contract's `setup` already run. Its state is its marker file, and
every state change is an atomic rename of that file:

    <name>.ready.json        {"sha", "setup_s"}: written last, once the entry is clean
    <name>.task-<id>.json    claimed by that task (the same content)
    <name>.dead.json         being dropped (stale sha)

A directory without a marker is a fill that crashed, and a marker without a
directory is a leftover; `recover` removes both. Claiming moves the marker first
and only then prepares the checkout, so a crash in between leaves an entry that
only `owned_by` (the same task, on its next run) can finish, never another task.
"""

import fcntl
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agentco.repo_contract import RepoContract
from agentco.sandbox.runner import CommandRunner
from agentco.sandbox.worktree import (
    GitError,
    Worktree,
    _exclude_agentco_dir,
    _fetch_task_branch,
    _git,
    _record_pushed,
    _run,
    branch_name,
)

_MARKER = re.compile(
    r"(?P<name>[A-Za-z0-9_-]+)\.(?P<state>ready|dead|task-(?P<id>\d+))\.json"
)


@dataclass(frozen=True)
class PoolEntry:
    path: Path
    sha: str
    setup_s: float


class PoolSetupFailed(RuntimeError):
    pass


def run_setup(
    runner: CommandRunner, contract: RepoContract, path: Path, timeout_s: float
) -> float:
    """Run the contract's `setup` in `path`; return the seconds it took."""
    if contract.setup is None:
        return 0.0
    prepare = getattr(runner, "prepare", None)
    started = time.monotonic()
    if prepare is not None:
        prepare(path)
    result = runner.run(contract.setup, path, timeout_s)
    if result.exit_code != 0:
        tail = result.output.strip().splitlines()[-5:]
        reason = "timed out" if result.timed_out else f"exit {result.exit_code}"
        raise PoolSetupFailed(f"{contract.setup!r} {reason}: {' | '.join(tail)}")
    return time.monotonic() - started


def _rev(cwd: Path, ref: str) -> str | None:
    proc = _run(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


class WorktreePool:
    def __init__(self, repo_root: Path, size: int) -> None:
        self.repo_root = Path(repo_root)
        self.size = size
        self.dir = self.repo_root / ".agentco" / "pool"
        self.failures: list[str] = []

    # -- markers -----------------------------------------------------------

    def _markers(self) -> list[tuple[str, str, Path]]:
        """(entry name, state, marker path) for every marker file."""
        found = []
        if self.dir.is_dir():
            for path in sorted(self.dir.iterdir()):
                m = _MARKER.fullmatch(path.name)
                if m and path.is_file():
                    found.append((m["name"], m["state"], path))
        return found

    def _entry_dirs(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(p for p in self.dir.iterdir() if p.is_dir())

    def _marker(self, name: str, state: str) -> Path:
        return self.dir / f"{name}.{state}.json"

    @staticmethod
    def _read(marker: Path) -> tuple[str, float] | None:
        try:
            data = json.loads(marker.read_text())
            return str(data["sha"]), float(data["setup_s"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_ready(self, name: str, sha: str, setup_s: float) -> None:
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps({"sha": sha, "setup_s": setup_s}))
        os.rename(tmp, self._marker(name, "ready"))

    # -- removal -----------------------------------------------------------

    def _remove_entry(self, name: str) -> None:
        path = self.dir / name
        if path.exists():
            _run(self.repo_root, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
        _run(self.repo_root, "worktree", "prune")
        for state in ("ready", "dead"):
            self._marker(name, state).unlink(missing_ok=True)
        (self.dir / f"{name}.tmp").unlink(missing_ok=True)

    # -- fill / recover ----------------------------------------------------

    @contextmanager
    def _fill_lock(self) -> Iterator[bool]:
        """Held while a fill runs, so a second process neither fills at once nor
        mistakes the first one's half-built entry for a crashed one."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def recover(self) -> None:
        with self._fill_lock() as held:
            if held:
                self._recover()

    def _recover(self) -> None:
        markers = self._markers()
        with_marker = {name for name, _, _ in markers}
        for path in self._entry_dirs():
            if path.name not in with_marker:
                self._remove_entry(path.name)
        for name, state, marker in markers:
            if state == "dead":
                self._remove_entry(name)
            elif not (self.dir / name).is_dir():
                marker.unlink(missing_ok=True)
        _run(self.repo_root, "worktree", "prune")

    def fill(self, main_ref: str, prepare: Callable[[Path], float]) -> int:
        self.failures = []
        if self.size <= 0:
            return 0
        sha = _git(self.repo_root, "rev-parse", "--verify", f"{main_ref}^{{commit}}")
        sha = sha.strip()
        _exclude_agentco_dir(self.repo_root)
        with self._fill_lock() as held:
            if not held:
                return 0
            self._recover()
            ready = []
            for name, state, marker in self._markers():
                if state != "ready":
                    continue
                content = self._read(marker)
                if content is None or content[0] != sha:
                    self._drop(name, marker)
                else:
                    ready.append(name)
            added = 0
            while len(ready) < self.size:
                name = f"w-{uuid.uuid4().hex[:10]}"
                if not self._add_entry(name, sha, prepare):
                    break
                ready.append(name)
                added += 1
            return added

    def _drop(self, name: str, marker: Path) -> None:
        try:
            os.rename(marker, self._marker(name, "dead"))
        except FileNotFoundError:
            return  # claimed in the meantime: it is not ours to remove
        self._remove_entry(name)

    def _add_entry(self, name: str, sha: str, prepare: Callable[[Path], float]) -> bool:
        path = self.dir / name
        try:
            _git(self.repo_root, "worktree", "add", "--detach", str(path), sha)
            setup_s = prepare(path)
            if _git(path, "status", "--porcelain").strip():
                raise GitError(f"pool entry {name} is not clean after setup")
            self._write_ready(name, sha, setup_s)
        except Exception as e:  # noqa: BLE001 - `prepare` is arbitrary setup code
            self.failures.append(f"{type(e).__name__}: {e}")
            self._remove_entry(name)
            return False
        return True

    # -- claim -------------------------------------------------------------

    def claim(self, task_id: int, base_ref: str) -> tuple[Worktree, PoolEntry] | None:
        branch = branch_name(task_id)
        if self._local_branch(branch) or _fetch_task_branch(self.repo_root, branch):
            return None  # the task has work of its own: it must resume from it
        sha = _rev(self.repo_root, base_ref)
        if sha is None:
            return None
        taken = self._marker_of(task_id)
        if taken is not None:
            return None  # owned_by finishes an earlier claim
        for name, state, marker in self._markers():
            if state != "ready":
                continue
            content = self._read(marker)
            if content is None or content[0] != sha:
                continue
            try:
                os.rename(marker, self._marker(name, f"task-{task_id}"))
            except FileNotFoundError:
                continue  # another claim got there first
            entry = PoolEntry(self.dir / name, sha, content[1])
            return self._finish_claim(entry, branch, base_ref), entry
        return None

    def owned_by(self, task_id: int, base_ref: str | None = None) -> Worktree | None:
        found = self._marker_of(task_id)
        if found is None:
            return None
        name, marker = found
        path = self.dir / name
        content = self._read(marker)
        if content is None or not path.is_dir():
            return None
        branch = branch_name(task_id)
        entry = PoolEntry(path, content[0], content[1])
        head = _run(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
        if head == branch:
            return Worktree(path, branch, base_ref or entry.sha)
        if self._local_branch(branch):
            # The claim got past creating the branch, so the task may have commits
            # on it (a crash mid-rebase leaves HEAD detached): never reset it.
            _run(path, "rebase", "--abort")
            _git(path, "checkout", branch)
            return Worktree(path, branch, base_ref or entry.sha)
        return self._finish_claim(entry, branch, base_ref)

    def entry_of(self, task_id: int) -> PoolEntry | None:
        found = self._marker_of(task_id)
        content = self._read(found[1]) if found else None
        if found is None or content is None:
            return None
        return PoolEntry(self.dir / found[0], content[0], content[1])

    def _marker_of(self, task_id: int) -> tuple[str, Path] | None:
        for name, state, marker in self._markers():
            if state == f"task-{task_id}":
                return name, marker
        return None

    def _local_branch(self, branch: str) -> bool:
        return _rev(self.repo_root, f"refs/heads/{branch}") is not None

    def _finish_claim(
        self, entry: PoolEntry, branch: str, base_ref: str | None
    ) -> Worktree:
        """Put a claimed entry on its task branch at the entry's commit, clean.

        Cleaning comes before the branch switch, so being on the task branch at all
        means the tree was already clean and a rerun never throws away the task's work.
        """
        path, sha = entry.path, entry.sha
        _git(path, "reset", "--hard", sha)
        _git(path, "clean", "-fd")
        _git(path, "checkout", "-B", branch, sha)
        _git(path, "reset", "--hard", sha)
        _git(path, "clean", "-fd")
        if _git(path, "status", "--porcelain").strip():
            raise GitError(f"claimed pool entry {path.name} is not clean")
        _record_pushed(self.repo_root, branch, None)
        return Worktree(path, branch, base_ref or sha)
