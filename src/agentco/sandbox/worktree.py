"""Per-task git worktrees and the neutral snapshot QA works from.

All functions are synchronous and shell out to git; async callers wrap them in
`asyncio.to_thread`. Git is invoked hermetically: no GPG signing, no user hooks,
no prompts, and a fixed agentco identity.
"""

import io
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

AUTHOR_NAME = "agentco"
AUTHOR_EMAIL = "agentco@localhost"
SNAPSHOT_MESSAGE = "agentco: snapshot"

_GIT = [
    "git",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    f"user.name={AUTHOR_NAME}",
    "-c",
    f"user.email={AUTHOR_EMAIL}",
]


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base: str


def branch_name(task_id: int) -> str:
    return f"agentco/task-{task_id}"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            [*_GIT, *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e


def _fail(args: tuple[str, ...], proc: subprocess.CompletedProcess[bytes]) -> GitError:
    detail = proc.stderr.decode(errors="replace").strip()
    return GitError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")


def _git(cwd: Path, *args: str) -> str:
    proc = _run(cwd, *args)
    if proc.returncode != 0:
        raise _fail(args, proc)
    return proc.stdout.decode(errors="replace")


def refresh_main(repo_root: Path, main: str) -> str:
    remotes = _git(repo_root, "remote").split()
    if "origin" not in remotes:
        return main
    _git(repo_root, "fetch", "origin", main)
    return f"origin/{main}"


def _exclude_agentco_dir(repo_root: Path) -> None:
    exclude = Path(_git(repo_root, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = repo_root / exclude
    existing = exclude.read_text() if exclude.exists() else ""
    if ".agentco/" in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(f"{existing}{prefix}.agentco/\n")


def create_worktree(repo_root: Path, task_id: int, base_ref: str) -> Worktree:
    path = repo_root / ".agentco" / "worktrees" / str(task_id)
    branch = branch_name(task_id)
    wt = Worktree(path=path, branch=branch, base=base_ref)
    if path.exists():
        return wt
    _exclude_agentco_dir(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = (
        _run(
            repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        ).returncode
        == 0
    )
    if branch_exists:
        _git(repo_root, "worktree", "add", str(path), branch)
    else:
        _git(
            repo_root,
            "worktree",
            "add",
            "--no-track",
            "-b",
            branch,
            str(path),
            base_ref,
        )
    return wt


def commit_all(wt: Worktree, message: str) -> str | None:
    _git(wt.path, "add", "-A")
    if not _git(wt.path, "status", "--porcelain").strip():
        return None
    _git(wt.path, "commit", "-m", message)
    return _git(wt.path, "rev-parse", "HEAD").strip()


def diff(wt: Worktree, against: str) -> str:
    return _git(wt.path, "diff", f"{against}...HEAD")


def changed_files(wt: Worktree, against: str) -> list[str]:
    out = _git(wt.path, "diff", "--no-renames", "--name-only", f"{against}...HEAD")
    return out.splitlines()


def changed_lines(wt: Worktree, against: str) -> int:
    out = _git(wt.path, "diff", "--no-renames", "--numstat", f"{against}...HEAD")
    total = 0
    for line in out.splitlines():
        added, deleted, _ = line.split("\t", 2)
        if added != "-":  # binary files report "-"
            total += int(added) + int(deleted)
    return total


def rebase_onto(wt: Worktree, base_ref: str) -> bool:
    _git(wt.path, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    proc = _run(wt.path, "rebase", base_ref)
    if proc.returncode == 0:
        return True
    abort = _run(wt.path, "rebase", "--abort")
    if abort.returncode != 0:
        raise _fail(("rebase", "--abort"), abort)
    return False


def make_snapshot(wt: Worktree, dest: Path) -> Path:
    """Export HEAD into a fresh single-commit repo (D4: QA never sees history).

    The repo contract's `build` must work from a fresh checkout, because the
    snapshot has no installed dependencies.
    """
    if dest.exists() and any(dest.iterdir()):
        raise GitError(f"snapshot destination {dest} is not empty")
    archive = _run(wt.path, "archive", "--format=tar", "HEAD")
    if archive.returncode != 0:
        raise _fail(("archive", "HEAD"), archive)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    _git(dest, "init", "-b", "main")
    _git(dest, "add", "-A")
    _git(dest, "commit", "--allow-empty", "-m", SNAPSHOT_MESSAGE)
    return dest


def push_branch(wt: Worktree) -> None:
    _git(wt.path, "push", "-u", "origin", wt.branch)


def remove_worktree(repo_root: Path, path: Path) -> None:
    _git(repo_root, "worktree", "remove", "--force", str(path))
