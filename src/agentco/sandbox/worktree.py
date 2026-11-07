"""Per-task git worktrees and the neutral snapshot QA works from.

All functions are synchronous and shell out to git; async callers wrap them in
`asyncio.to_thread`. Git is invoked hermetically: no GPG signing, no user hooks,
no prompts, and a fixed agentco identity.
"""

import os
import shutil
import subprocess
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


_INHERITED_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _run(
    cwd: Path, *args: str, input: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            [*_GIT, *args],
            cwd=cwd,
            env=env,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
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


_DIFF = (
    "-c",
    "core.quotePath=false",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
)


def diff(wt: Worktree, against: str) -> str:
    return _git(wt.path, *_DIFF, f"{against}...HEAD")


def changed_files(wt: Worktree, against: str) -> list[str]:
    out = _git(wt.path, *_DIFF, "--no-renames", "--name-only", f"{against}...HEAD")
    return out.splitlines()


def changed_lines(wt: Worktree, against: str) -> int:
    out = _git(wt.path, *_DIFF, "--no-renames", "--numstat", f"{against}...HEAD")
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


def _tree_entries(wt: Worktree) -> list[tuple[str, str, str]]:
    out = _git(wt.path, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    entries = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, _kind, sha = meta.split()
        entries.append((mode, sha, path))
    return entries


def _read_blobs(wt: Worktree, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    proc = _run(
        wt.path, "cat-file", "--batch", input="".join(f"{s}\n" for s in shas).encode()
    )
    if proc.returncode != 0:
        raise _fail(("cat-file", "--batch"), proc)
    data, pos, blobs = proc.stdout, 0, {}
    for sha in shas:
        end = data.index(b"\n", pos)
        size = int(data[pos:end].split()[2])
        blobs[sha] = data[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def _safe_rel(path: str) -> Path:
    parts = path.split("/")
    if any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
        raise GitError(f"refusing unsafe path in snapshot: {path!r}")
    return Path(*parts)


def _materialize(wt: Worktree, dest: Path) -> None:
    """Copy HEAD's committed blobs into dest, with no attribute processing.

    Not `git archive`/`checkout-index`: those honour .gitattributes
    (`export-subst` would expand the engineer's commit message into files).
    Symlinks are recreated as symlinks, after all regular files, and a symlink
    that is absolute or resolves outside dest raises GitError.
    """
    entries = _tree_entries(wt)
    blobs = _read_blobs(wt, [sha for mode, sha, _ in entries if mode[0] in "01"])
    links: list[tuple[Path, str]] = []
    for mode, sha, path in entries:
        target = dest / _safe_rel(path)
        if mode == "160000":  # submodule: archive-like empty directory
            target.mkdir(parents=True, exist_ok=True)
        elif mode == "120000":
            links.append((target, blobs[sha].decode(errors="surrogateescape")))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[sha])
            target.chmod(0o755 if mode == "100755" else 0o644)
    root = dest.resolve()
    for link, link_target in links:
        if os.path.isabs(link_target):
            raise GitError(f"snapshot symlink {link.relative_to(dest)} is absolute")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target, link)
    for link, _ in links:  # after all links exist, so chains are resolved physically
        if not Path(os.path.realpath(link)).is_relative_to(root):
            raise GitError(f"snapshot symlink {link.relative_to(dest)} escapes dest")


def make_snapshot(wt: Worktree, dest: Path) -> Path:
    """Export HEAD into a fresh single-commit repo (D4: QA never sees history).

    The repo contract's `build` must work from a fresh checkout, because the
    snapshot has no installed dependencies. Any failure raises GitError and
    leaves dest as it was found (absent, or empty), so a retry works.
    """
    existed = dest.exists()
    if existed and any(dest.iterdir()):
        raise GitError(f"snapshot destination {dest} is not empty")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        _materialize(wt, dest)
        _git(dest, "init", "-b", "main")
        _git(dest, "add", "-A")
        _git(dest, "commit", "--allow-empty", "-m", SNAPSHOT_MESSAGE)
    except (GitError, OSError, ValueError) as e:
        _clean(dest, keep_dir=existed)
        if isinstance(e, GitError):
            raise
        raise GitError(f"snapshot of {wt.path} failed: {e}") from e
    return dest


def _clean(dest: Path, keep_dir: bool) -> None:
    if not keep_dir:
        shutil.rmtree(dest, ignore_errors=True)
        return
    for child in dest.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def push_branch(wt: Worktree) -> None:
    _git(wt.path, "push", "-u", "origin", wt.branch)


def remove_worktree(repo_root: Path, path: Path) -> None:
    _git(repo_root, "worktree", "remove", "--force", str(path))
