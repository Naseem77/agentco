import os
import subprocess

import pytest

from agentco.code.graph import CallEdge, InMemoryCodeGraph, code_graph_for
from agentco.code.indexer import MAX_FILE_BYTES, IndexReport, NotAGitRepo, index_repo
from agentco.memory.gate import AccessDenied
from agentco.memory.scopes import Scope, graph_name
from agentco.roles import Role

GRAPH = graph_name("acme", Scope.RND)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def git_repo(root, files):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "add", "-A")
    return root


def librarian(backend):
    return code_graph_for("acme", Role.LIBRARIAN, backend)


async def test_first_index_then_noop(tmp_path):
    root = git_repo(
        tmp_path / "repo",
        {
            "a.py": "def f():\n    g()\n",
            "b.ts": "export function g() {}",
            "README.md": "x",
            ".gitignore": "node_modules/\n",
            "node_modules/x.js": "function z(){}",
        },
    )
    backend = InMemoryCodeGraph()
    code = librarian(backend)
    assert await index_repo("o/r", root, code) == IndexReport(
        indexed=2, unchanged=0, deleted=0, skipped=0, calls=1
    )
    assert sorted(await backend.file_hashes(GRAPH, "o/r")) == ["a.py", "b.ts"]
    upserts = backend.upserts
    assert await index_repo("o/r", root, code) == IndexReport(
        indexed=0, unchanged=2, deleted=0, skipped=0, calls=0
    )
    assert backend.upserts == upserts


async def test_only_changed_file_reindexed_and_links_survive(tmp_path):
    root = git_repo(
        tmp_path / "repo",
        {"a.py": "def f():\n    pass\n", "b.py": "def g():\n    pass\n"},
    )
    backend = InMemoryCodeGraph()
    code = librarian(backend)
    await index_repo("o/r", root, code)
    assert await code.link_episode("o/r", "ep1", "a.py", "f") is True

    (root / "a.py").write_text("def f():\n    pass\n\n\ndef h():\n    f()\n")
    report = await index_repo("o/r", root, code)

    assert report == IndexReport(indexed=1, unchanged=1, deleted=0, skipped=0, calls=1)
    assert backend.links(GRAPH) == {("ep1", "a.py", "f")}
    assert await backend.call_edges(GRAPH, "o/r") == [
        CallEdge("a.py", "h", "a.py", "f")
    ]


async def test_removed_file_deleted(tmp_path):
    root = git_repo(
        tmp_path / "repo",
        {"a.py": "def f():\n    g()\n", "b.ts": "export function g() {}"},
    )
    backend = InMemoryCodeGraph()
    code = librarian(backend)
    await index_repo("o/r", root, code)
    assert len(await backend.call_edges(GRAPH, "o/r")) == 1

    git(root, "rm", "-q", "-f", "b.ts")
    report = await index_repo("o/r", root, code)

    assert report == IndexReport(indexed=0, unchanged=1, deleted=1, skipped=0, calls=0)
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["a.py"]
    assert await backend.call_edges(GRAPH, "o/r") == []


async def test_file_deleted_on_disk_but_still_tracked_leaves_graph(tmp_path):
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n", "b.py": "x = 1\n"})
    backend = InMemoryCodeGraph()
    code = librarian(backend)
    await index_repo("o/r", root, code)

    os.remove(root / "a.py")
    report = await index_repo("o/r", root, code)

    assert report == IndexReport(indexed=0, unchanged=1, deleted=1, skipped=1, calls=0)
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["b.py"]


async def test_untracked_files_ignored(tmp_path):
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    (root / "new.py").write_text("def n(): pass\n")
    backend = InMemoryCodeGraph()
    report = await index_repo("o/r", root, librarian(backend))
    assert report == IndexReport(indexed=1, unchanged=0, deleted=0, skipped=0, calls=0)
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["a.py"]


async def test_uncommitted_edits_are_indexed(tmp_path):
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "m")
    (root / "a.py").write_text("def edited(): pass\n")
    backend = InMemoryCodeGraph()
    await index_repo("o/r", root, librarian(backend))
    assert [s.name for s in await backend.symbols_in(GRAPH, "o/r", "a.py")] == [
        "edited"
    ]


async def test_nested_paths_are_slash_separated(tmp_path):
    root = git_repo(tmp_path / "repo", {"pkg/sub/m.py": "def f(): pass\n"})
    backend = InMemoryCodeGraph()
    await index_repo("o/r", root, librarian(backend))
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["pkg/sub/m.py"]


async def test_large_file_skipped(tmp_path):
    big = "x = 1\n" * (MAX_FILE_BYTES // 6 + 1)
    assert len(big.encode()) > MAX_FILE_BYTES
    root = git_repo(tmp_path / "repo", {"big.py": big, "a.py": "def f(): pass\n"})
    backend = InMemoryCodeGraph()
    report = await index_repo("o/r", root, librarian(backend))
    assert report == IndexReport(indexed=1, unchanged=0, deleted=0, skipped=1, calls=0)
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["a.py"]


async def test_file_at_size_limit_is_indexed(tmp_path):
    body = "x = 1\n"
    data = body * (MAX_FILE_BYTES // len(body))
    data += "#" * (MAX_FILE_BYTES - len(data))
    assert len(data.encode()) == MAX_FILE_BYTES
    root = git_repo(tmp_path / "repo", {"edge.py": data})
    report = await index_repo("o/r", root, librarian(InMemoryCodeGraph()))
    assert report == IndexReport(indexed=1, unchanged=0, deleted=0, skipped=0, calls=0)


async def test_symlink_skipped(tmp_path):
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    outside = tmp_path / "outside.py"
    outside.write_text("def secret(): pass\n")
    (root / "link.py").symlink_to(outside)
    git(root, "add", "-A")
    backend = InMemoryCodeGraph()
    report = await index_repo("o/r", root, librarian(backend))
    assert report == IndexReport(indexed=1, unchanged=0, deleted=0, skipped=1, calls=0)
    assert list(await backend.file_hashes(GRAPH, "o/r")) == ["a.py"]


async def test_not_a_git_repo(tmp_path):
    with pytest.raises(NotAGitRepo) as exc:
        await index_repo("o/r", tmp_path, librarian(InMemoryCodeGraph()))
    assert str(exc.value) == f"{tmp_path} is not a git repository"


async def test_role_without_rnd_write_is_denied(tmp_path):
    root = git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    backend = InMemoryCodeGraph()
    with pytest.raises(AccessDenied) as exc:
        await index_repo("o/r", root, code_graph_for("acme", Role.QA, backend))
    assert str(exc.value) == "role qa may not write scope rnd"
    assert backend.upserts == 0
