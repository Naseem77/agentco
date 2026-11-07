import subprocess
from pathlib import Path

import pytest

from agentco.sandbox.worktree import (
    GitError,
    branch_name,
    changed_files,
    changed_lines,
    commit_all,
    create_worktree,
    diff,
    make_snapshot,
    push_branch,
    rebase_onto,
    refresh_main,
    remove_worktree,
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def commit_on_main(root: Path, rel: str, text: str, message: str) -> None:
    (root / rel).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-m", message)


def test_branch_name():
    assert branch_name(7) == "agentco/task-7"


def test_worktree_is_idempotent_and_branch_named(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    base = refresh_main(root, "main")
    assert base == "origin/main"
    wt = create_worktree(root, 7, base)
    assert wt.branch == "agentco/task-7"
    assert wt.path == root / ".agentco/worktrees/7"
    assert wt.base == "origin/main"
    assert (wt.path / "a.py").read_text() == "x = 1\n"
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "agentco/task-7"
    assert create_worktree(root, 7, base) == wt


def test_create_worktree_excludes_agentco_dir_once(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    create_worktree(root, 1, "main")
    create_worktree(root, 2, "main")
    exclude = (root / ".git/info/exclude").read_text().splitlines()
    assert exclude.count(".agentco/") == 1
    assert git(root, "status", "--porcelain") == ""


def test_create_worktree_does_not_track_base(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 3, "origin/main")
    out = subprocess.run(
        ["git", "config", "--get", "branch.agentco/task-3.merge"],
        cwd=wt.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout == ""


def test_create_worktree_reuses_existing_branch_after_removal(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 4, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    sha = commit_all(wt, "work")
    remove_worktree(root, wt.path)
    assert not wt.path.exists()
    again = create_worktree(root, 4, "main")
    assert again == wt
    assert git(again.path, "rev-parse", "HEAD").strip() == sha


def test_refresh_main_without_origin_returns_main(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    assert refresh_main(repo, "main") == "main"


def test_refresh_main_picks_up_new_origin_commit(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    other = tmp_path / "other"
    git(tmp_path, "clone", str(tmp_path / "repo.git"), str(other))
    (other / "b.py").write_text("y = 1\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=o", "-c", "user.email=o@x", "commit", "-m", "b")
    git(other, "push", "origin", "main")
    base = refresh_main(root, "main")
    assert git(root, "show", f"{base}:b.py") == "y = 1\n"


def test_refresh_main_unknown_branch_raises(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    with pytest.raises(GitError):
        refresh_main(root, "nope")


def test_commit_diff_and_lines(make_repo):
    root = make_repo({"a.py": "x = 1\ny = 2\n"})
    wt = create_worktree(root, 1, "main")
    assert commit_all(wt, "nothing") is None
    (wt.path / "a.py").write_text("x = 1\ny = 3\nz = 4\n")
    sha = commit_all(wt, "feat: change")
    assert sha == git(wt.path, "rev-parse", "HEAD").strip()
    assert git(wt.path, "log", "-1", "--format=%an <%ae>|%s").strip() == (
        "agentco <agentco@localhost>|feat: change"
    )
    assert changed_files(wt, "main") == ["a.py"]
    assert changed_lines(wt, "main") == 3  # 2 added, 1 deleted
    assert "+z = 4" in diff(wt, "main")
    assert commit_all(wt, "again") is None


def test_commit_all_includes_new_files(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "pkg").mkdir()
    (wt.path / "pkg/new.py").write_text("n = 1\n")
    assert commit_all(wt, "add") is not None
    assert changed_files(wt, "main") == ["pkg/new.py"]
    assert changed_lines(wt, "main") == 1


def test_changed_lines_counts_binary_as_zero(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "blob.bin").write_bytes(b"\x00\x01\x02")
    commit_all(wt, "bin")
    assert changed_files(wt, "main") == ["blob.bin"]
    assert changed_lines(wt, "main") == 0


def test_commit_ignores_user_hooks(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    assert commit_all(wt, "hooked") is not None


def test_rebase_onto_clean(make_repo):
    root = make_repo({"a.py": "x = 1\n", "b.py": "y = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    commit_all(wt, "task")
    commit_on_main(root, "b.py", "y = 2\n", "main moves")
    assert rebase_onto(wt, "main") is True
    assert git(wt.path, "log", "--format=%s").splitlines() == [
        "task",
        "main moves",
        "initial",
    ]


def test_rebase_conflict_aborts_cleanly(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("x = task\n")
    commit_all(wt, "task edit")
    head = git(wt.path, "rev-parse", "HEAD")
    commit_on_main(root, "a.py", "x = main\n", "main edit")
    assert rebase_onto(wt, "main") is False
    assert git(wt.path, "status", "--porcelain") == ""
    assert git(wt.path, "rev-parse", "HEAD") == head
    assert (wt.path / "a.py").read_text() == "x = task\n"


def test_rebase_onto_unknown_ref_raises(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    with pytest.raises(GitError):
        rebase_onto(wt, "no-such-ref")


def test_snapshot_has_one_commit_and_no_history(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n", "d/b.py": "y = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    commit_all(wt, "fix: SECRET-ENGINEER-NOTE")
    snap = make_snapshot(wt, tmp_path / "snap")
    assert snap == tmp_path / "snap"
    log = git(snap, "log", "--format=%s|%an <%ae>")
    assert log.strip() == "agentco: snapshot|agentco <agentco@localhost>"
    assert (snap / "a.py").read_text() == "x = 2\n"
    assert (snap / "d/b.py").read_text() == "y = 1\n"
    assert git(snap, "remote").strip() == ""
    assert git(snap, "status", "--porcelain") == ""
    assert git(snap, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert git(snap, "rev-list", "--all", "--count").strip() == "1"
    assert not (snap / ".git/objects/info/alternates").exists()
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=snap,
        check=True,
        capture_output=True,
    ).stdout
    assert b"SECRET-ENGINEER-NOTE" not in objects


def test_snapshot_excludes_uncommitted_and_agentco_files(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "scratch.txt").write_text("uncommitted")
    snap = make_snapshot(wt, tmp_path / "snap")
    assert sorted(p.name for p in snap.iterdir()) == [".git", "a.py"]


def test_snapshot_refuses_non_empty_dest(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    with pytest.raises(GitError):
        make_snapshot(wt, dest)


def test_push_branch_reaches_bare_origin(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 5, "main")
    (wt.path / "a.py").write_text("x = 2\n")
    sha = commit_all(wt, "task")
    push_branch(wt)
    bare = tmp_path / "repo.git"  # --git-dir: safe.bareRepository may be "explicit"
    remote = git(root, "--git-dir", str(bare), "rev-parse", "refs/heads/agentco/task-5")
    assert remote.strip() == sha


def test_push_branch_failure_raises(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 5, "main")
    git(root, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(GitError):
        push_branch(wt)


def test_remove_worktree(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "dirty.txt").write_text("uncommitted")
    remove_worktree(root, wt.path)
    assert not wt.path.exists()
    assert "agentco/task-1" not in git(root, "worktree", "list")
