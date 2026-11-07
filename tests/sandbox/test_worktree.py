import subprocess
from pathlib import Path

import pytest

from agentco.sandbox.worktree import (
    GitError,
    Worktree,
    branch_name,
    changed_files,
    changed_lines,
    commit_all,
    count_commits,
    create_worktree,
    diff,
    head_ancestor,
    head_sha,
    is_ancestor,
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


def test_diff_shows_rename_as_delete_and_add(make_repo):
    root = make_repo({"a.py": "x = 1\ny = 2\nz = 3\n"})
    wt = create_worktree(root, 1, "main")
    git(wt.path, "mv", "a.py", "b.py")
    commit_all(wt, "move")
    text = diff(wt, "main")
    assert "rename from" not in text
    assert "diff --git a/a.py b/a.py\ndeleted file mode" in text
    assert "diff --git a/b.py b/b.py\nnew file mode" in text


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


def commit_in_worktree(wt, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        (wt.path / rel).write_text(text)
    commit_all(wt, message)


def test_snapshot_ignores_export_subst_and_export_ignore(make_repo, tmp_path):
    root = make_repo({"a.py": "x=1  $Format:%B$\n", "keep.txt": "keep\n"})
    wt = create_worktree(root, 1, "main")
    commit_in_worktree(
        wt,
        {".gitattributes": "a.py export-subst\nkeep.txt export-ignore\n"},
        "fix: SECRET-REASONING-NOTE",
    )
    snap = make_snapshot(wt, tmp_path / "snap")
    assert (snap / "a.py").read_text() == "x=1  $Format:%B$\n"
    assert (snap / "keep.txt").read_text() == "keep\n"
    assert (snap / ".gitattributes").exists()


def test_snapshot_preserves_modes_and_internal_symlinks(make_repo, tmp_path):
    root = make_repo({"run.sh": "#!/bin/sh\n", "d/b.py": "y = 1\n"})
    (root / "run.sh").chmod(0o755)
    (root / "link.py").symlink_to("d/b.py")
    git(root, "add", "-A")
    git(root, "commit", "-m", "modes")
    wt = create_worktree(root, 1, "main")
    snap = make_snapshot(wt, tmp_path / "snap")
    assert (snap / "run.sh").stat().st_mode & 0o111 != 0
    assert (snap / "d/b.py").stat().st_mode & 0o111 == 0
    assert (snap / "link.py").is_symlink()
    assert (snap / "link.py").readlink() == Path("d/b.py")
    assert git(snap, "status", "--porcelain") == ""


@pytest.mark.parametrize("target", ["/etc/hostname", "../outside.txt"])
def test_snapshot_rejects_escaping_symlink_and_cleans_dest(make_repo, tmp_path, target):
    root = make_repo({"a.py": "x = 1\n"})
    (root / "link").symlink_to(target)
    git(root, "add", "-A")
    git(root, "commit", "-m", "link")
    wt = create_worktree(root, 1, "main")
    dest = tmp_path / "snap"
    with pytest.raises(GitError):
        make_snapshot(wt, dest)
    assert not dest.exists()
    (root / "link").unlink()
    git(root, "add", "-A")
    git(root, "commit", "-m", "drop link")
    wt2 = create_worktree(root, 2, "main")
    assert make_snapshot(wt2, dest) == dest


def test_snapshot_rejects_symlink_chain_escaping_via_dot(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    (root / "y").symlink_to(".")
    (root / "z").symlink_to("y/..")
    git(root, "add", "-A")
    git(root, "commit", "-m", "chain")
    wt = create_worktree(root, 1, "main")
    dest = tmp_path / "snap"
    with pytest.raises(GitError):
        make_snapshot(wt, dest)
    assert not dest.exists()


def test_snapshot_failure_keeps_preexisting_empty_dest_empty(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    (root / "link").symlink_to("/etc/hostname")
    git(root, "add", "-A")
    git(root, "commit", "-m", "link")
    wt = create_worktree(root, 1, "main")
    dest = tmp_path / "snap"
    dest.mkdir()
    with pytest.raises(GitError):
        make_snapshot(wt, dest)
    assert list(dest.iterdir()) == []


def test_changed_files_not_quoted_for_non_ascii_names(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    commit_in_worktree(wt, {"é2.py": "y = 1\n"}, "feat: unicode")
    assert changed_files(wt, "main") == ["é2.py"]


def test_git_calls_ignore_inherited_git_dir(make_repo, monkeypatch, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    other = make_repo({"b.py": "y = 1\n"}, name="other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    wt = create_worktree(root, 1, "main")
    commit_in_worktree(wt, {"c.py": "z = 1\n"}, "feat: c")
    assert changed_files(wt, "main") == ["c.py"]


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


def _remote_sha(root: Path, tmp_path: Path, branch: str) -> str:
    bare = tmp_path / "repo.git"
    return git(
        root, "--git-dir", str(bare), "rev-parse", f"refs/heads/{branch}"
    ).strip()


def test_push_branch_replaces_a_branch_rewritten_by_a_rebase(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 5, "main")
    (wt.path / "b.py").write_text("y = 1\n")
    commit_all(wt, "task")
    push_branch(wt)
    commit_on_main(root, "c.py", "z = 1\n", "upstream")
    git(root, "push", "origin", "main")
    base = refresh_main(root, "main")
    assert rebase_onto(wt, base)

    push_branch(wt)

    assert _remote_sha(root, tmp_path, "agentco/task-5") == head_sha(wt)


def test_push_branch_replaces_a_remote_branch_without_a_tracking_ref(
    make_repo, tmp_path
):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 5, "main")
    (wt.path / "b.py").write_text("y = 1\n")
    commit_all(wt, "task")
    push_branch(wt)
    git(root, "update-ref", "-d", "refs/remotes/origin/agentco/task-5")
    git(wt.path, "commit", "--amend", "-m", "reworded", "--allow-empty")

    push_branch(wt)

    assert _remote_sha(root, tmp_path, "agentco/task-5") == head_sha(wt)


def test_push_branch_never_forces_a_branch_that_is_not_a_task_branch(
    make_repo, tmp_path
):
    root = make_repo({"a.py": "x = 1\n"})
    main_sha = git(root, "rev-parse", "main").strip()
    wt = Worktree(path=root, branch="main", base="main")

    with pytest.raises(GitError) as err:
        push_branch(wt)

    assert str(err.value) == "refusing to push 'main': not an agentco task branch"
    assert _remote_sha(root, tmp_path, "main") == main_sha


def test_commit_counting_helpers(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 3, "main")
    shas = []
    for i in range(3):
        (wt.path / f"f{i}.py").write_text(f"v = {i}\n")
        shas.append(commit_all(wt, f"c{i}"))

    assert count_commits(wt, "main") == 3
    assert count_commits(wt, shas[0]) == 2
    assert head_ancestor(wt, 0) == shas[2]
    assert head_ancestor(wt, 2) == shas[0]
    assert is_ancestor(wt, shas[0]) and is_ancestor(wt, shas[2])
    assert not is_ancestor(wt, "0" * 40)
    git(wt.path, "reset", "--hard", shas[0])
    assert not is_ancestor(wt, shas[2])


def test_remove_worktree(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "dirty.txt").write_text("uncommitted")
    remove_worktree(root, wt.path)
    assert not wt.path.exists()
    assert "agentco/task-1" not in git(root, "worktree", "list")


def _blob(root: Path, text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input=text,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _mktree(root: Path, *entries: tuple[str, str, str, str]) -> str:
    lines = "".join(
        f"{mode} {kind} {sha}\t{name}\n" for mode, kind, sha, name in entries
    )
    return subprocess.run(
        ["git", "mktree"],
        cwd=root,
        input=lines,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _point_branch_at_tree(root: Path, wt, tree: str) -> None:
    commit = git(root, "commit-tree", tree, "-m", "crafted", "-p", "HEAD").strip()
    git(root, "update-ref", f"refs/heads/{wt.branch}", commit)


def _snapshot_must_fail_cleanly(wt, tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    with pytest.raises(GitError):
        make_snapshot(wt, outer / "snap")
    assert list(outer.iterdir()) == []


def test_snapshot_rejects_case_colliding_symlink_dir_without_stray_writes(
    make_repo, tmp_path
):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    up = _blob(root, "..")
    inner = _mktree(root, ("120000", "blob", _blob(root, "x"), "x"))
    tree = _mktree(root, ("120000", "blob", up, "Z"), ("040000", "tree", inner, "z"))
    _point_branch_at_tree(root, wt, tree)
    _snapshot_must_fail_cleanly(wt, tmp_path)


def test_snapshot_rejects_duplicate_symlink_and_dir_without_stray_writes(
    make_repo, tmp_path
):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    inner = _mktree(root, ("120000", "blob", _blob(root, "x"), "b"))
    tree = _mktree(
        root,
        ("120000", "blob", _blob(root, ".."), "a"),
        ("040000", "tree", inner, "a"),
    )
    _point_branch_at_tree(root, wt, tree)
    _snapshot_must_fail_cleanly(wt, tmp_path)


def test_snapshot_rejects_file_under_file(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    inner = _mktree(root, ("100644", "blob", _blob(root, "y"), "b"))
    tree = _mktree(
        root,
        ("100644", "blob", _blob(root, "x"), "a"),
        ("040000", "tree", inner, "a"),
    )
    _point_branch_at_tree(root, wt, tree)
    _snapshot_must_fail_cleanly(wt, tmp_path)


def test_snapshot_rejects_link_under_link(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    inner = _mktree(root, ("120000", "blob", _blob(root, "x"), "b"))
    tree = _mktree(
        root,
        ("120000", "blob", _blob(root, "d"), "a"),
        ("040000", "tree", inner, "a"),
    )
    _point_branch_at_tree(root, wt, tree)
    _snapshot_must_fail_cleanly(wt, tmp_path)


def test_snapshot_rejects_case_colliding_files(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")
    tree = _mktree(
        root,
        ("100644", "blob", _blob(root, "1"), "Readme"),
        ("100644", "blob", _blob(root, "2"), "readme"),
    )
    _point_branch_at_tree(root, wt, tree)
    _snapshot_must_fail_cleanly(wt, tmp_path)


def test_snapshot_handles_gitlink_as_empty_directory(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"})
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
    git(root, "commit", "-m", "add submodule")
    wt = create_worktree(root, 1, "main")
    snap = make_snapshot(wt, tmp_path / "snap")
    assert (snap / "a.py").read_text() == "x = 1\n"
    assert (snap / "sub").is_dir() and list((snap / "sub").iterdir()) == []
    assert git(snap, "log", "--format=%s").strip() == "agentco: snapshot"


def test_snapshot_converts_unexpected_errors_and_cleans_dest(
    make_repo, tmp_path, monkeypatch
):
    root = make_repo({"a.py": "x = 1\n"})
    wt = create_worktree(root, 1, "main")

    def boom(*args, **kwargs):
        raise IndexError("surprise")

    monkeypatch.setattr("agentco.sandbox.worktree._read_blobs", boom)
    dest = tmp_path / "snap"
    with pytest.raises(GitError):
        make_snapshot(wt, dest)
    assert not dest.exists()
