import subprocess

from agentco.config import CHECKER_ROLES, CODER_ROLES
from agentco.roles import Role


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_loop_cfg_models_and_families(loop_cfg, tmp_path):
    assert loop_cfg.tenant == "acme"
    assert loop_cfg.database_url == f"sqlite:///{tmp_path}/loop.db"
    assert set(loop_cfg.roles) == {r for r in Role if r is not Role.HUMAN}
    assert loop_cfg.roles[Role.PM].model == "m-pm"
    coder_families = {loop_cfg.roles[r].family for r in CODER_ROLES}
    assert all(loop_cfg.roles[r].family not in coder_families for r in CHECKER_ROLES)


def test_make_repo_has_commit_and_pushed_origin(make_repo, tmp_path):
    repo = make_repo({"a.txt": "a\n", "d/b.txt": "b\n"})
    assert repo == tmp_path / "repo"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "remote", "get-url", "origin") == str(tmp_path / "repo.git")
    assert git(repo, "rev-parse", "origin/main") == git(repo, "rev-parse", "HEAD")
    bare = f"--git-dir={tmp_path / 'repo.git'}"
    assert git(tmp_path, bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    other = make_repo({"x": "1"}, name="second")
    assert other == tmp_path / "second" and (tmp_path / "second.git").is_dir()
