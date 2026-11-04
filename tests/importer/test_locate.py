import subprocess
from dataclasses import replace
from pathlib import Path

from helpers import GRAPH_AT_H1, REPO, comment_101, pr3, thread_106

from agentco.code.graph import InMemoryCodeGraph, ScopedCodeGraph, code_graph_for
from agentco.code.parse import parse_source
from agentco.importer.locate import SymbolLocator, git_show
from agentco.roles import Role


def _code() -> ScopedCodeGraph:
    return code_graph_for("acme", Role.LIBRARIAN, InMemoryCodeGraph())


async def test_locate_uses_the_comment_commit_not_current_code():
    code = _code()
    today = b"class GraphView:\n    def other(self):\n" + b"        pass\n" * 10
    await code.upsert_file(REPO, parse_source("src/app/graph.py", today))
    shown = []

    def show(checkout, commit, path):
        shown.append((checkout, commit, path))
        return GRAPH_AT_H1

    loc = SymbolLocator(REPO, code, Path("/checkout"), show)
    found = await loc.locate(comment_101(), pr3())
    assert found.qualname == "GraphView.switch"
    assert shown == [(Path("/checkout"), "h1", "src/app/graph.py")]
    today_symbol = await code.enclosing(REPO, "src/app/graph.py", 6)
    assert today_symbol.qualname == "GraphView.other"


async def test_parses_are_cached_per_commit_and_path():
    shown = []

    def show(checkout, commit, path):
        shown.append((commit, path))
        return GRAPH_AT_H1

    loc = SymbolLocator(REPO, None, Path("/checkout"), show)
    await loc.locate(comment_101(), pr3())
    await loc.locate(replace(comment_101(), id=999, original_line=2), pr3())
    assert shown == [("h1", "src/app/graph.py")]


async def test_left_side_uses_base_commit():
    shown = []

    def show(checkout, commit, path):
        shown.append((commit, path))
        return GRAPH_AT_H1

    loc = SymbolLocator(REPO, None, Path("/checkout"), show)
    left = replace(comment_101(), side="LEFT")
    assert (await loc.locate(left, pr3())).qualname == "GraphView.switch"
    assert shown == [("b1", "src/app/graph.py")]


async def test_original_line_wins_over_line():
    loc = SymbolLocator(REPO, None, Path("/c"), lambda *_: GRAPH_AT_H1)
    moved = replace(comment_101(), original_line=3, line=6)
    assert (await loc.locate(moved, pr3())).qualname == "GraphView.load"
    only_line = replace(comment_101(), original_line=None, line=3)
    assert (await loc.locate(only_line, pr3())).qualname == "GraphView.load"


async def test_falls_back_to_index_when_commit_missing():
    code = _code()
    await code.upsert_file(REPO, parse_source("src/app/graph.py", GRAPH_AT_H1))
    calls = []

    def show(checkout, commit, path):
        calls.append(commit)
        return GRAPH_AT_H1 if commit == "never" else None

    loc = SymbolLocator(REPO, code, Path("/checkout"), show)
    assert (await loc.locate(comment_101(), pr3())).qualname == "GraphView.switch"
    assert calls == ["h1"]
    line_gone = replace(comment_101(), line=None, original_line=6)
    assert (await loc.locate(line_gone, pr3())).qualname == "GraphView.switch"


async def test_index_fallback_without_checkout_does_not_call_show():
    code = _code()
    await code.upsert_file(REPO, parse_source("src/app/graph.py", GRAPH_AT_H1))

    def show(*_):
        raise AssertionError("show must not be called without a checkout")

    loc = SymbolLocator(REPO, code, None, show)
    assert (await loc.locate(comment_101(), pr3())).qualname == "GraphView.switch"


async def test_no_checkout_no_index_is_none():
    assert await SymbolLocator(REPO, None, None).locate(comment_101(), pr3()) is None


async def test_checkout_missing_commit_and_no_index_is_none():
    loc = SymbolLocator(REPO, None, Path("/c"), lambda *_: None)
    assert await loc.locate(comment_101(), pr3()) is None


async def test_file_level_and_unsupported_are_none():
    code = _code()
    await code.upsert_file(REPO, parse_source("src/app/graph.py", GRAPH_AT_H1))

    def show(*_):
        raise AssertionError("show must not be called")

    loc = SymbolLocator(REPO, code, Path("/c"), show)
    assert await loc.locate(thread_106().root, pr3()) is None
    markdown = replace(comment_101(), path="docs/README.md")
    assert await loc.locate(markdown, pr3()) is None
    no_line = replace(comment_101(), line=None, original_line=None)
    assert await loc.locate(no_line, pr3()) is None


async def test_line_outside_any_symbol_is_none():
    source = b"x = 1\n\n\ndef f():\n    pass\n"
    loc = SymbolLocator(REPO, None, Path("/c"), lambda *_: source)
    assert await loc.locate(replace(comment_101(), original_line=2), pr3()) is None
    assert (
        await loc.locate(replace(comment_101(), original_line=5), pr3())
    ).name == "f"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_git_show_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_bytes(b"one = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_bytes(b"two = 2\n")
    _git(repo, "commit", "-q", "-am", "second")
    assert git_show(repo, first, "a.py") == b"one = 1\n"
    assert git_show(repo, "HEAD", "a.py") == b"two = 2\n"
    assert git_show(repo, "0" * 40, "a.py") is None
    assert git_show(repo, first, "missing.py") is None
    assert git_show(repo, "--output=x", "a.py") is None
    assert git_show(tmp_path / "nope", first, "a.py") is None
