from pathlib import Path

from agentco.code.impact import impact_report
from agentco.sandbox.worktree import _git, commit_all, create_worktree

FILES = {
    "src/calc.py": "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
    "src/ops.py": "def total(a, b):\n    return add(a, b)\n",
    "tests/test_ops.py": "def test_total():\n    assert total(1, 2) == 3\n",
    "README.md": "hi\n",
}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


async def test_lists_callers_and_tests_of_changed_function(make_repo):
    wt = create_worktree(make_repo(FILES), 1, "main")
    edit(wt.path / "src/calc.py", "return a + b", "return b + a")
    commit_all(wt, "x")
    report = await impact_report("acme/app", wt, "main")
    assert "src/calc.py::add" in report
    assert "src/ops.py::total" in report
    assert "tests/test_ops.py" in report
    assert "src/calc.py::mul" not in report


async def test_no_change_is_empty(make_repo):
    wt = create_worktree(make_repo(FILES), 1, "main")
    assert await impact_report("acme/app", wt, "main") == ""


async def test_non_code_and_deleted_files_do_not_raise(make_repo):
    wt = create_worktree(make_repo(FILES), 1, "main")
    edit(wt.path / "README.md", "hi", "hello")
    _git(wt.path, "rm", "-q", "src/ops.py")
    commit_all(wt, "x")
    report = await impact_report("acme/app", wt, "main")
    assert "src/ops.py (deleted)" in report
    assert "unavailable" not in report


async def test_failure_degrades_to_one_line(make_repo, monkeypatch):
    wt = create_worktree(make_repo(FILES), 1, "main")
    edit(wt.path / "src/calc.py", "return a + b", "return b + a")
    commit_all(wt, "x")

    async def boom(*_a, **_k):
        raise RuntimeError("parse failed")

    monkeypatch.setattr("agentco.code.impact.index_repo", boom)
    report = await impact_report("acme/app", wt, "main")
    assert report == "impact unavailable: RuntimeError: parse failed"
