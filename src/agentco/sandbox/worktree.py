"""Per-task git worktrees and the neutral snapshot QA works from.

All functions are synchronous and shell out to git; async callers wrap them in
`asyncio.to_thread`. Git is invoked hermetically: no GPG signing, no user hooks,
no prompts, and a fixed agentco identity.
"""

import os
import posixpath
import re
import shutil
import subprocess
import unicodedata
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


def head_sha(wt: Worktree) -> str:
    return _git(wt.path, "rev-parse", "HEAD").strip()


def reset_worktree(path: Path, ref: str) -> None:
    """Discard all uncommitted work and return to `ref` (ignored files are kept)."""
    _git(path, "reset", "--hard", ref)
    _git(path, "clean", "-fd")


_DIFF = (
    "-c",
    "core.quotePath=false",
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
)


def diff(wt: Worktree, against: str) -> str:
    return _git(wt.path, *_DIFF, "--no-renames", f"{against}...HEAD")


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


_BLOB_MODES = {"100644", "100755", "120000"}


def _fold(path: str) -> str:
    # macOS APFS is case-insensitive and normalization-insensitive
    return unicodedata.normalize("NFD", path).casefold()


def _validate_entries(entries: list[tuple[str, str, str]]) -> None:
    """Reject trees that could write outside dest or collide on disk.

    Checked up front, before anything is written: a symlink whose lexical
    target is absolute or leaves dest; duplicate or case-colliding paths; and
    any path that lies under another entry (file-under-file, anything under a
    symlink or gitlink).
    """
    seen = {_fold(path) for _, _, path in entries}
    if len(seen) != len(entries):
        raise GitError("refusing snapshot with duplicate or case-colliding paths")
    for _mode, _sha, path in entries:
        parts = path.split("/")
        for i in range(1, len(parts)):
            if _fold("/".join(parts[:i])) in seen:
                raise GitError(f"refusing snapshot: {path!r} lies under another entry")


def _check_inside(root: Path, directory: Path) -> None:
    if not Path(os.path.realpath(directory)).is_relative_to(root):
        raise GitError(f"snapshot path {directory} resolves outside dest")


def _make_parent(root: Path, target: Path) -> None:
    ancestor = target.parent
    while not ancestor.exists() and ancestor != root:
        ancestor = ancestor.parent
    _check_inside(root, ancestor)
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_inside(root, target.parent)


def _materialize(wt: Worktree, dest: Path) -> None:
    """Copy HEAD's committed blobs into dest, with no attribute processing.

    Not `git archive`/`checkout-index`: those honour .gitattributes
    (`export-subst` would expand the engineer's commit message into files).
    Every entry is validated before anything is written. Regular files go
    first, then symlinks (recreated as symlinks; absolute or escaping targets
    raise GitError). Gitlinks (submodules) become empty directories, like
    `git archive`; their content is not in this repo's objects.
    """
    entries = _tree_entries(wt)
    _validate_entries(entries)
    root = dest.resolve()
    links: list[tuple[Path, str, str]] = []
    for mode, _sha, path in entries:
        if mode == "160000" or mode in _BLOB_MODES:
            continue
        raise GitError(f"unsupported mode {mode} for {path!r} in snapshot")
    blobs = _read_blobs(wt, [sha for mode, sha, _ in entries if mode in _BLOB_MODES])
    for mode, sha, path in entries:
        target = dest / _safe_rel(path)
        if mode == "160000":
            _make_parent(root, target)
            target.mkdir(exist_ok=True)
        elif mode == "120000":
            link_target = blobs[sha].decode(errors="surrogateescape")
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), link_target)
            )
            if posixpath.isabs(link_target) or resolved.split("/")[0] == "..":
                raise GitError(f"snapshot symlink {path!r} is absolute or escapes dest")
            links.append((target, link_target, path))
        else:
            _make_parent(root, target)
            target.write_bytes(blobs[sha])
            target.chmod(0o755 if mode == "100755" else 0o644)
    for link, link_target, _ in links:
        _make_parent(root, link)
        os.symlink(link_target, link)
    for link, _, path in links:  # after all links exist, so chains resolve physically
        if not Path(os.path.realpath(link)).is_relative_to(root):
            raise GitError(f"snapshot symlink {path!r} escapes dest")


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
    except Exception as e:
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


def head_ancestor(wt: Worktree, steps: int) -> str:
    """The commit `steps` parents before HEAD."""
    return _git(wt.path, "rev-parse", f"HEAD~{steps}").strip()


def count_commits(wt: Worktree, since: str) -> int:
    """How many commits HEAD has that `since` does not."""
    return int(_git(wt.path, "rev-list", "--count", f"{since}..HEAD"))


def is_ancestor(wt: Worktree, commit: str) -> bool:
    """Whether `commit` is HEAD or one of its ancestors."""
    proc = _run(wt.path, "merge-base", "--is-ancestor", commit, "HEAD")
    return proc.returncode == 0  # 1: not an ancestor; other codes: unknown commit


_TASK_BRANCH = re.compile(r"agentco/task-\d+")


def push_branch(wt: Worktree) -> None:
    """Publish the task branch, replacing what is there if a rebase rewrote it.

    Only agentco's own `agentco/task-<id>` branch may be forced; it is never main.
    The lease names the remote tip seen just now, so a push from anyone else in
    between is still refused.
    """
    if not _TASK_BRANCH.fullmatch(wt.branch):
        raise GitError(f"refusing to push {wt.branch!r}: not an agentco task branch")
    ref = f"refs/heads/{wt.branch}"
    listing = _git(wt.path, "ls-remote", "--heads", "origin", ref).split()
    lease = (
        f"--force-with-lease={ref}:{listing[0]}"
        if listing
        else f"--force-with-lease={ref}:"
    )
    _git(wt.path, "push", "-u", lease, "origin", f"{wt.branch}:{ref}")


def remove_worktree(repo_root: Path, path: Path) -> None:
    _git(repo_root, "worktree", "remove", "--force", str(path))
