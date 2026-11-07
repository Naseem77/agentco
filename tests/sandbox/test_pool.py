import subprocess
from pathlib import Path

import pytest

from agentco.sandbox import pool as pool_module
from agentco.sandbox.pool import WorktreePool
from agentco.sandbox.worktree import GitError, create_worktree


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def prepare(path: Path) -> float:
    (path / "deps").mkdir()
    (path / "deps" / "lib.txt").write_text("installed")
    return 1.5


@pytest.fixture
def repo(make_repo) -> Path:
    return make_repo({"a.py": "x = 1\n", ".gitignore": "deps/\n"})


def entry_dirs(pool: WorktreePool) -> list[Path]:
    return pool._entry_dirs()


def markers(pool: WorktreePool, state: str) -> list[Path]:
    return [m for _, s, m in pool._markers() if s == state]


def commit_on_main(root: Path, text: str) -> None:
    (root / "a.py").write_text(text)
    git(root, "commit", "-am", "more")


def test_claim_is_clean_on_task_branch(repo):
    pool = WorktreePool(repo, 1)
    assert pool.fill("main", prepare) == 1
    (entry_dirs(pool)[0] / "stray.txt").write_text("x")  # a leftover
    (entry_dirs(pool)[0] / "a.py").write_text("tampered\n")
    wt, entry = pool.claim(7, "main")
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-7"
    assert wt.branch == "agentco/task-7" and wt.path == entry.path
    assert not (wt.path / "stray.txt").exists()
    assert (wt.path / "a.py").read_text() == "x = 1\n"
    assert (wt.path / "deps" / "lib.txt").exists()
    assert git(wt.path, "status", "--porcelain") == ""
    assert entry.setup_s == 1.5
    assert git(wt.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")
    assert not markers(pool, "ready") and len(markers(pool, "task-7")) == 1


def test_claim_is_clean_even_if_untracked_path_blocks_checkout(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    (entry_dirs(pool)[0] / "dir").mkdir()
    (entry_dirs(pool)[0] / "dir" / "f").write_text("x")
    wt, _ = pool.claim(1, "main")
    assert git(wt.path, "status", "--porcelain") == ""


def test_entry_claimed_once(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    assert pool.claim(1, "main") is not None
    assert pool.claim(2, "main") is None
    assert pool.owned_by(2) is None
    assert pool.claim(1, "main") is None  # the same task cannot take a second one


def test_claim_races_lost_marker_moves_on(repo, monkeypatch):
    pool = WorktreePool(repo, 2)
    pool.fill("main", prepare)
    real = pool_module.os.rename
    lost = []

    def rename(src, dst):
        if not lost:
            lost.append(src)
            Path(src).unlink()  # another claimer renamed it first
            raise FileNotFoundError(src)
        return real(src, dst)

    monkeypatch.setattr(pool_module.os, "rename", rename)
    wt, _ = pool.claim(3, "main")
    assert wt.path.name not in str(lost[0])
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-3"


def test_stale_entries_dropped(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    old = entry_dirs(pool)[0]
    commit_on_main(repo, "x = 2\n")
    assert pool.claim(1, "main") is None  # only entries at main's current commit
    assert pool.fill("main", prepare) == 1
    assert not old.exists() and len(entry_dirs(pool)) == 1
    wt, _ = pool.claim(1, "main")
    assert (wt.path / "a.py").read_text() == "x = 2\n"
    assert git(wt.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")
    assert not markers(pool, "dead")


def test_claimed_entries_survive_fill_and_do_not_count(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    wt, _ = pool.claim(1, "main")
    (wt.path / "work.txt").write_text("task work")
    assert pool.fill("main", prepare) == 1  # a fresh one replaces the claimed one
    assert (wt.path / "work.txt").read_text() == "task work"
    assert len(markers(pool, "task-1")) == 1 and len(markers(pool, "ready")) == 1


def test_crash_mid_claim_recovers(repo, monkeypatch):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    (entry_dirs(pool)[0] / "stray.txt").write_text("x")
    real = pool_module._git

    def failing(cwd, *args):
        if args[:1] == ("checkout",):
            raise GitError("simulated crash")
        return real(cwd, *args)

    monkeypatch.setattr(pool_module, "_git", failing)
    with pytest.raises(GitError):
        pool.claim(5, "main")
    monkeypatch.undo()
    assert pool.claim(6, "main") is None  # the half-claimed entry is not up for grabs
    assert len(markers(pool, "task-5")) == 1
    wt = pool.owned_by(5)
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-5"
    assert not (wt.path / "stray.txt").exists()
    assert git(wt.path, "status", "--porcelain") == ""
    assert (wt.path / "deps").exists()


def test_owned_by_does_not_reset_a_task_in_progress(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    wt, _ = pool.claim(4, "main")
    (wt.path / "a.py").write_text("x = 99\n")
    git(wt.path, "commit", "-am", "work")
    (wt.path / "wip.txt").write_text("uncommitted")
    again = pool.owned_by(4, "origin/main")
    assert again.path == wt.path and again.base == "origin/main"
    assert (again.path / "wip.txt").exists()
    assert git(again.path, "log", "-1", "--format=%s") == "work"
    assert pool.owned_by(5) is None


def test_owned_by_keeps_commits_when_head_was_left_detached(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    wt, _ = pool.claim(4, "main")
    (wt.path / "a.py").write_text("x = 99\n")
    git(wt.path, "commit", "-am", "work")
    tip = git(wt.path, "rev-parse", "HEAD")
    git(
        wt.path, "checkout", "--detach", "HEAD~1"
    )  # e.g. a crash in the middle of a rebase
    again = pool.owned_by(4)
    assert git(again.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-4"
    assert git(again.path, "rev-parse", "HEAD") == tip


def test_claim_leaves_a_task_with_work_of_its_own(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    create_worktree(repo, 8, "main")  # the task branch already exists locally
    assert pool.claim(8, "main") is None
    assert len(markers(pool, "ready")) == 1


def test_claim_leaves_a_task_whose_branch_is_on_the_remote(repo):
    git(repo, "branch", "agentco/task-9", "main")
    git(repo, "push", "origin", "agentco/task-9")
    git(repo, "branch", "-D", "agentco/task-9")
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    assert pool.claim(9, "main") is None
    assert len(markers(pool, "ready")) == 1


def test_claim_forgets_a_stale_pushed_ref(repo):
    git(repo, "update-ref", "refs/agentco/pushed/agentco/task-2", "main")
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    pool.claim(2, "main")
    proc = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/agentco/pushed/agentco/task-2",
        ],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert proc.returncode != 0


def test_failed_or_dirty_prepare_leaves_no_entry(repo):
    pool = WorktreePool(repo, 2)

    def boom(path: Path) -> float:
        raise RuntimeError("setup failed")

    assert pool.fill("main", boom) == 0
    assert "setup failed" in pool.failures[0]
    assert not entry_dirs(pool) and not pool._markers()

    def dirty(path: Path) -> float:
        (path / "untracked.txt").write_text("not ignored")
        return 0.1

    assert pool.fill("main", dirty) == 0
    assert "not clean" in pool.failures[0]
    assert not entry_dirs(pool) and not pool._markers()
    assert git(repo, "worktree", "list").count("\n") == 0  # only the main checkout


def test_recover_removes_half_filled_entry(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    half = pool.dir / "w-half"
    git(repo, "worktree", "add", "--detach", str(half), "main")  # crashed before marker
    orphan = pool.dir / "w-gone.ready.json"
    orphan.write_text('{"sha": "x", "setup_s": 0}')
    pool.recover()
    assert not half.exists() and not orphan.exists()
    assert len(entry_dirs(pool)) == 1 and len(markers(pool, "ready")) == 1
    assert "w-half" not in git(repo, "worktree", "list")


def test_recover_and_fill_leave_foreign_directories_alone(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    foreign = pool.dir / "w-abc123-qa-r1"
    foreign.mkdir()
    (foreign / "f.txt").write_text("x")
    pool.recover()
    pool.fill("main", prepare)
    assert (foreign / "f.txt").exists()


def test_recover_finishes_a_dead_entry_and_spares_claimed_ones(repo):
    pool = WorktreePool(repo, 2)
    pool.fill("main", prepare)
    wt, _ = pool.claim(1, "main")
    ready = markers(pool, "ready")[0]
    dead = ready.with_name(ready.name.replace(".ready.", ".dead."))
    ready.rename(dead)
    pool.recover()
    assert not dead.exists() and not entry_dirs(pool)[1:]
    assert wt.path.exists() and git(wt.path, "rev-parse", "--abbrev-ref", "HEAD")


def test_fill_tops_up_to_size_and_zero_disables(repo):
    pool = WorktreePool(repo, 2)
    assert pool.fill("main", prepare) == 2
    assert pool.fill("main", prepare) == 0
    assert WorktreePool(repo, 0).fill("main", prepare) == 0


def test_fill_skips_while_another_fill_runs(repo):
    pool = WorktreePool(repo, 1)
    with pool._fill_lock() as held:
        assert held
        other = WorktreePool(repo, 1)
        assert other.fill("main", prepare) == 0
        other.recover()  # must not delete what the running fill is building
    assert pool.fill("main", prepare) == 1


def test_pool_dir_is_ignored_by_the_main_checkout(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    assert git(repo, "status", "--porcelain") == ""


def test_claim_with_unknown_base_is_a_miss(repo):
    pool = WorktreePool(repo, 1)
    pool.fill("main", prepare)
    assert pool.claim(1, "nope/main") is None
