from pathlib import Path

import pytest

from agentco.repo_contract import RepoContract, Sandbox
from agentco.sandbox.affected import (
    AffectedTests,
    affected_tests,
    changed_line_ranges,
    is_test_path,
    test_command,
)
from agentco.sandbox.worktree import commit_all, create_worktree

FILES = {
    "src/calc.py": "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_add.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "tests/test_mul.py": "from src.calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    "pyproject.toml": "[project]\nname = 'x'\n",
}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def contract(test_files: str | None) -> RepoContract:
    return RepoContract(
        build="b",
        test="uv run pytest",
        e2e=None,
        lint="l",
        sandbox=Sandbox(devcontainer=".devcontainer"),
        test_files=test_files,
    )


async def test_only_callers_of_changed_function(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert got == AffectedTests(
        paths=("tests/test_mul.py",),
        full=False,
        reason="1 affected test file(s) from 1 changed symbol(s)",
    )


async def test_callers_found_transitively(make_repo):
    files = {
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "src/ops.py": "def total(a, b):\n    return add(a, b)\n",
        "tests/test_ops.py": "def test_total():\n    assert total(1, 2) == 3\n",
        "tests/test_other.py": "def test_other():\n    assert True\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "a + b", "b + a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == (("tests/test_ops.py",), False)


async def test_depth_limit_is_respected_by_falling_back_to_full(make_repo):
    files = {
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "src/ops.py": "def total(a, b):\n    return add(a, b)\n",
        "tests/test_ops.py": "def test_total():\n    assert total(1, 2) == 3\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "a + b", "b + a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main", max_depth=1)
    assert (got.paths, got.full) == ((), True)


async def test_unindexed_change_means_full_suite(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "pyproject.toml", "'x'", "'y'")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert got == AffectedTests(
        paths=(), full=True, reason="pyproject.toml has no symbols in the code graph"
    )


async def test_unindexed_change_alongside_indexed_change_is_full(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    edit(wt.path / "pyproject.toml", "'x'", "'y'")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_changed_test_file_is_included(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "tests/test_add.py", "== 3", "== 3  # sum")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == (("tests/test_add.py",), False)


async def test_deleted_test_file_is_not_listed(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    (wt.path / "tests/test_add.py").unlink()
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == (("tests/test_mul.py",), False)


async def test_deleted_source_file_is_full(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    (wt.path / "src/calc.py").unlink()
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_no_callers_means_full_suite(make_repo):
    files = {**FILES, "src/lonely.py": "def lonely():\n    return 1\n"}
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/lonely.py", "return 1", "return 2")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_no_changes_means_full_suite(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_module_level_change_is_full(make_repo):
    files = {**FILES, "src/calc.py": "RATE = 1\n\n" + FILES["src/calc.py"]}
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "RATE = 1", "RATE = 2")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_ambiguous_function_name_is_full(make_repo):
    files = {
        **FILES,
        "src/other.py": "def add(a, b):\n    return a - b\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a + b", "return b + a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_changed_test_support_file_is_full(make_repo):
    files = {**FILES, "tests/conftest.py": "VALUE = 1\n"}
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "tests/conftest.py", "1", "2")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_uncommitted_changes_are_full(make_repo):
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    commit_all(wt, "x")
    edit(wt.path / "src/calc.py", "return a + b", "return b + a")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_task_branch_never_touches_a_shared_graph(make_repo, monkeypatch):
    import agentco.sandbox.affected as mod

    seen = []
    real = mod.InMemoryCodeGraph

    class Spy(real):
        def __init__(self):
            super().__init__()
            seen.append(self)

    monkeypatch.setattr(mod, "InMemoryCodeGraph", Spy)
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    commit_all(wt, "x")
    await affected_tests("acme/app", wt, "main")
    await affected_tests("acme/app", wt, "main")
    assert len(seen) == 2 and seen[0] is not seen[1]


def test_changed_line_ranges(make_repo):
    files = {"a.py": "1\n2\n3\n4\n5\n6\n", "b.txt": "x\n"}
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("1\nTWO\n3\n4\n5\n")  # edit line 2, drop line 6
    (wt.path / "b.txt").write_text("x\ny\nz\n")
    commit_all(wt, "x")
    assert changed_line_ranges(wt, "main") == {
        "a.py": [(2, 2), (5, 5)],
        "b.txt": [(2, 3)],
    }


def test_changed_line_ranges_skips_deleted_and_handles_spaces(make_repo):
    root = make_repo({"gone.py": "x\n", "my file.py": "x\n"})
    wt = create_worktree(root, 1, "main")
    (wt.path / "gone.py").unlink()
    (wt.path / "my file.py").write_text("x\ny\n")
    commit_all(wt, "x")
    assert changed_line_ranges(wt, "main") == {"my file.py": [(2, 2)]}


def test_command_uses_template():
    got = AffectedTests(paths=("tests/test_mul.py",), full=False, reason="r")
    assert test_command(contract("uv run pytest {files}"), got) == (
        "uv run pytest tests/test_mul.py"
    )


def test_command_quotes_paths():
    got = AffectedTests(paths=("tests/a b.py", "tests/c.py"), full=False, reason="r")
    assert test_command(contract("pytest {files}"), got) == (
        "pytest 'tests/a b.py' tests/c.py"
    )


def test_command_full_or_no_template_runs_whole_suite():
    full = AffectedTests(paths=(), full=True, reason="r")
    part = AffectedTests(paths=("tests/x.py",), full=False, reason="r")
    assert test_command(contract("pytest {files}"), full) == "uv run pytest"
    assert test_command(contract(None), part) == "uv run pytest"


@pytest.mark.parametrize(
    "path,ok",
    [
        ("tests/x.py", True),
        ("pkg/test/x.go", True),
        ("web/__tests__/x.ts", True),
        ("src/a.test.ts", True),
        ("src/a.spec.jsx", True),
        ("src/a.test.tsx", True),
        ("src/a.spec.js", True),
        ("src/test_utils_helper.py", True),
        ("src/calc_test.py", True),
        ("pkg/calc_test.go", True),
        ("src/a.py", False),
        ("src/latest/a.py", False),
        ("src/contest.py", False),
        ("src/a.test.py", False),
        ("src/test.py", False),
    ],
)
def test_is_test_path(path, ok):
    assert is_test_path(path) is ok


SUB_FILES = {
    **FILES,
    "src/calc.py": FILES["src/calc.py"] + "\ndef sub(a, b):\n    return a - b\n",
    "tests/test_sub.py": "from src.calc import sub\n\ndef test_sub():\n    assert sub(3, 1) == 2\n",
}


async def test_deleted_last_function_is_full(make_repo):
    root = make_repo(SUB_FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "\ndef sub(a, b):\n    return a - b\n", "")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_deleted_function_between_others_is_full(make_repo):
    root = make_repo(SUB_FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "def mul(a, b):\n    return a * b\n\n", "")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_renamed_function_reached_by_another_test_is_full(make_repo):
    files = {
        **FILES,
        "tests/test_other.py": "from src.calc import multiply\n\ndef test_other():\n    assert multiply(1, 1) == 1\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "def mul(", "def multiply(")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_removed_test_function_is_not_a_removal(make_repo):
    files = {
        **FILES,
        "tests/test_mul.py": FILES["tests/test_mul.py"]
        + "\ndef test_mul_again():\n    assert mul(1, 1) == 1\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(
        wt.path / "tests/test_mul.py",
        "\ndef test_mul_again():\n    assert mul(1, 1) == 1\n",
        "",
    )
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == (("tests/test_mul.py",), False)


async def test_module_level_change_in_test_file_is_full(make_repo):
    files = {
        **FILES,
        "tests/test_add.py": "LIMIT = 1\n\ndef test_add():\n    assert LIMIT == 1\n",
        "tests/test_mul.py": "from tests.test_add import LIMIT\n\ndef test_mul():\n    assert LIMIT == 1\n",
    }
    root = make_repo(files)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "tests/test_add.py", "LIMIT = 1\n", "LIMIT = 2\n")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == ((), True)


async def test_index_failure_falls_back_to_full(make_repo, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("unreadable")

    monkeypatch.setattr("agentco.sandbox.affected.index_repo", boom)
    root = make_repo(FILES)
    wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a")
    commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert got == AffectedTests(
        paths=(),
        full=True,
        reason="code graph unavailable: RuntimeError: unreadable",
    )


def test_command_keeps_other_braces_in_template():
    got = AffectedTests(paths=("tests/a.py",), full=False, reason="r")
    assert test_command(contract("awk '{print}' {files} {x}"), got) == (
        "awk '{print}' tests/a.py {x}"
    )


def test_command_root_level_dash_path_is_not_an_option():
    got = AffectedTests(paths=("-x_test.py", "tests/b.py"), full=False, reason="r")
    assert test_command(contract("pytest {files}"), got) == (
        "pytest ./-x_test.py tests/b.py"
    )
