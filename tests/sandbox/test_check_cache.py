import subprocess

from agentco.sandbox.check_cache import CheckCache, base_sha, tree_sha
from agentco.sandbox.worktree import commit_all, create_worktree
from agentco.workflow.preflight import CheckResult


def git(cwd, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_put_then_get_returns_the_passing_result():
    cache = CheckCache()
    key = cache.key("lint", "ruff check", "tree1", "base1")
    result = CheckResult("lint", True, "")
    cache.put(key, result)
    assert cache.get(key) == result


def test_failures_are_never_stored():
    cache = CheckCache()
    key = cache.key("lint", "ruff check", "tree1", "base1")
    cache.put(key, CheckResult("lint", False, "E501"))
    assert cache.get(key) is None


def test_every_part_of_the_key_matters():
    cache = CheckCache()
    cache.put(cache.key("lint", "ruff", "t", "b"), CheckResult("lint", True, ""))
    assert cache.get(cache.key("build", "ruff", "t", "b")) is None
    assert cache.get(cache.key("lint", "ruff --fix", "t", "b")) is None
    assert cache.get(cache.key("lint", "ruff", "t2", "b")) is None
    assert cache.get(cache.key("lint", "ruff", "t", "b2")) is None


def test_find_matches_a_passing_command_on_the_same_tree_and_base():
    cache = CheckCache()
    ok = CheckResult("rules", True, "checked 2 rule(s)")
    cache.put(cache.key("rules", "a\0b", "t", "b"), ok)
    assert cache.find("rules", "t", "b", lambda c: c == "a\0b") == ok
    assert cache.find("rules", "t", "b", lambda c: c == "x") is None
    assert cache.find("rules", "t2", "b", lambda c: True) is None


def test_tree_sha_follows_content_not_commits(make_repo):
    root = make_repo({"a.txt": "a\n"})
    wt = create_worktree(root, 1, "main")
    before = tree_sha(wt)
    assert before == git(wt.path, "rev-parse", "HEAD^{tree}")
    git(
        wt.path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-m",
        "e",
        "--allow-empty",
    )
    assert tree_sha(wt) == before
    (wt.path / "a.txt").write_text("b\n")
    commit_all(wt, "change")
    assert tree_sha(wt) != before


def test_base_sha_resolves_a_ref_to_its_commit(make_repo):
    root = make_repo({"a.txt": "a\n"})
    wt = create_worktree(root, 1, "main")
    assert base_sha(wt, "main") == git(root, "rev-parse", "main")
