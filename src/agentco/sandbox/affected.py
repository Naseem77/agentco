"""Run only the tests a change can reach, selected from the code graph (D13).

The graph is a fresh per-task in-memory overlay built from the worktree's
committed HEAD; the shared `rnd` graph is keyed to main and is never written
for a task branch. Where the call graph cannot trace a change (module-level
code, a function name it cannot tell apart), the tests that import the changed
module run; when the selection is still unsure, the answer is the full suite.
Prose files (Markdown, reStructuredText, AsciiDoc) select no tests. The final
pre-flight before a PR always runs the full suite.
"""

import asyncio
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from posixpath import basename

from agentco.code.graph import InMemoryCodeGraph, code_graph_for
from agentco.code.indexer import index_repo
from agentco.code.parse import Symbol, parse_source
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.worktree import _DIFF, Worktree, _git, _run, changed_files

_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
_TEST_NAME = re.compile(
    r"test_.*\.py|.*_test\.py|.*_test\.go|.*\.(?:test|spec)\.(?:ts|tsx|js|jsx)",
    re.DOTALL,
)
# prose no test reads; .txt is left out because it may be requirements.txt
_DOC_SUFFIXES = (".md", ".rst", ".adoc")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_OVERLAY_TENANT = "overlay"


@dataclass(frozen=True)
class AffectedTests:
    paths: tuple[str, ...]
    full: bool
    reason: str


def _is_test_name(path: str) -> bool:
    return _TEST_NAME.fullmatch(basename(path)) is not None


def is_test_path(path: str) -> bool:
    segments = path.split("/")
    return any(s in _TEST_DIRS for s in segments[:-1]) or _is_test_name(path)


def changed_line_ranges(wt: Worktree, base: str) -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges per file; a pure deletion is the line it follows.

    Deleted and binary files have no new side and are absent; so are paths git
    quotes. Callers must treat a changed file missing here as unknown.
    """
    out = _git(
        wt.path,
        *_DIFF,
        "--no-renames",
        "-U0",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        f"{base}...HEAD",
    )
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    in_header = False
    for line in out.splitlines():
        if line.startswith("diff --git "):
            current, in_header = None, True
        elif in_header and line.startswith("+++ "):
            target = line[4:].removesuffix("\t")  # git tabs names that contain spaces
            current = target[2:] if target.startswith("b/") else None
        elif line.startswith("@@"):
            in_header = False
            match = _HUNK.match(line)
            if match and current is not None:
                start = int(match.group(1))
                count = 1 if match.group(2) is None else int(match.group(2))
                end = start if count == 0 else start + count - 1
                ranges.setdefault(current, []).append((start, end))
    return ranges


def _full(reason: str) -> AffectedTests:
    return AffectedTests(paths=(), full=True, reason=reason)


def _function_spans(symbols: list[Symbol]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(
        (s.start_line, s.end_line) for s in symbols if s.kind == "Function"
    ):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _inside_functions(spans: list[tuple[int, int]], hunk: tuple[int, int]) -> bool:
    return any(start <= hunk[0] and hunk[1] <= end for start, end in spans)


def _is_code(line: str) -> bool:
    text = line.strip()
    return bool(text) and not text.startswith("#")


def _module_code_changed(
    wt: Worktree, base: str, path: str, spans: list[tuple[int, int]]
) -> bool:
    """True when a hunk adds or removes code outside every function.

    Blank lines and comments between functions are not code under test.
    """
    out = _git(wt.path, *_DIFF, "--no-renames", "-U0", f"{base}...HEAD", "--", path)
    start = count = 0
    added: list[str] = []
    removed: list[str] = []

    def outside() -> bool:
        end = start if count == 0 else start + count - 1
        for i, line in enumerate(added):
            if _is_code(line) and not _inside_functions(spans, (start + i, start + i)):
                return True
        return any(map(_is_code, removed)) and not _inside_functions(
            spans, (start, end)
        )

    in_hunk = False
    for line in out.splitlines():
        if line.startswith("@@"):
            if in_hunk and outside():
                return True
            match = _HUNK.match(line)
            if match is None:
                return True
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            added, removed, in_hunk = [], [], True
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
        elif in_hunk and line.startswith("-"):
            removed.append(line[1:])
    return in_hunk and outside()


def _removed_symbols(
    wt: Worktree, merge_base: str, path: str, head: list[Symbol]
) -> list[str]:
    """Base-side symbols of `path` that no longer exist in HEAD (deleted or renamed)."""
    proc = _run(wt.path, "show", f"{merge_base}:{path}")
    if proc.returncode != 0:
        return []  # added in this branch: nothing was removed
    structure = parse_source(path, proc.stdout)
    if structure is None:
        return []
    kept = {s.qualname for s in head}
    gone = [s for s in structure.symbols if s.qualname not in kept]
    if is_test_path(path):  # dropping a test needs no rerun; dropping a helper does
        gone = [s for s in gone if not s.name.lower().startswith("test")]
    return [s.qualname for s in gone]


def _module_names(path: str) -> list[str]:
    """Dotted names `path` can be imported as, with and without a `src.` root."""
    parts = path.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def _imports_module(text: str, module: str) -> bool:
    if re.search(rf"(?<![\w.]){re.escape(module)}(?![\w])", text):
        return True
    if "." not in module:
        return False
    package, name = module.rsplit(".", 1)
    for match in re.finditer(
        rf"^\s*from\s+{re.escape(package)}\s+import\s+(\([^)]*\)|[^\n]*)",
        text,
        re.MULTILINE,
    ):
        if re.search(rf"\b{re.escape(name)}\b", match.group(1)):
            return True
    return False


def _tests_importing(wt: Worktree, path: str, test_files: list[str]) -> set[str]:
    if not path.endswith(".py"):
        return set()
    names = _module_names(path)
    found: set[str] = set()
    for test in test_files:
        try:
            text = (wt.path / test).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(_imports_module(text, name) for name in names):
            found.add(test)
    return found


def _overlaps(symbol: Symbol, hunk: tuple[int, int]) -> bool:
    return symbol.start_line <= hunk[1] and hunk[0] <= symbol.end_line


async def affected_tests(
    repo: str, wt: Worktree, base: str, max_depth: int = 4
) -> AffectedTests:
    dirty = await asyncio.to_thread(
        _git, wt.path, "status", "--porcelain", "--untracked-files=no"
    )
    if dirty.strip():
        return _full("uncommitted changes in the worktree")  # graph reads HEAD's files
    files = await asyncio.to_thread(changed_files, wt, base)
    if not files:
        return _full(f"no changes against {base}")
    ranges = await asyncio.to_thread(changed_line_ranges, wt, base)

    try:
        merge_base = (
            await asyncio.to_thread(_git, wt.path, "merge-base", base, "HEAD")
        ).strip()
        code = code_graph_for(_OVERLAY_TENANT, Role.LIBRARIAN, InMemoryCodeGraph())
        await index_repo(repo, wt.path, code)
        symbols = await code.all_symbols(repo)
        edges = await code.call_edges(repo)
    except Exception as e:  # noqa: BLE001 - any failure must degrade to the full suite
        return _full(f"code graph unavailable: {type(e).__name__}: {e}")

    name_count: dict[str, int] = defaultdict(int)
    for syms in symbols.values():
        for s in syms:
            if s.kind == "Function":
                name_count[s.qualname.rsplit(".", 1)[-1]] += 1

    tests: set[str] = set()
    seeds: set[tuple[str, str]] = set()
    # source files the graph cannot fully trace: run the tests that import them
    by_import: set[str] = set()
    for path in sorted(files):
        if is_test_path(path):
            if not _is_test_name(path):
                return _full(f"{path} is test support, not a test file")
            if (wt.path / path).is_file():  # a deleted test has nothing to run
                tests.add(path)
        elif path.lower().endswith(_DOC_SUFFIXES):
            continue
        elif not symbols.get(path):
            return _full(f"{path} has no symbols in the code graph")
        elif path not in ranges:
            return _full(f"{path} changed without a line range")
        file_ranges = ranges.get(path, [])
        spans = _function_spans(symbols.get(path, []))
        # a test file is run itself, so its module-level code is not code under test;
        # elsewhere an import or constant may reach any function of the module
        whole_module = (
            not _is_test_name(path)
            and any(not _inside_functions(spans, hunk) for hunk in file_ranges)
            and await asyncio.to_thread(
                _module_code_changed, wt, merge_base, path, spans
            )
        )
        if whole_module:
            by_import.add(path)
        if (wt.path / path).is_file():
            removed = await asyncio.to_thread(
                _removed_symbols, wt, merge_base, path, symbols.get(path, [])
            )
            if removed:
                return _full(f"{path} removes or renames {removed[0]}")
        for symbol in symbols.get(path, []):
            if symbol.kind != "Function" or not (
                whole_module or any(_overlaps(symbol, h) for h in file_ranges)
            ):
                continue
            name = symbol.qualname.rsplit(".", 1)[-1]
            if name_count[name] > 1 and not (
                is_test_path(path) and name.startswith("test")
            ):
                if not _is_test_name(path):  # a test file already runs itself
                    by_import.add(path)  # its call edges cannot be told apart by name
                continue
            seeds.add((path, symbol.qualname))

    callers: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        callers[(e.callee_path, e.callee)].append((e.caller_path, e.caller))
    seen = set(seeds)
    frontier = seeds
    for _ in range(max_depth):
        frontier = {c for node in frontier for c in callers[node]} - seen
        seen |= frontier
    tests |= {path for path, _ in seen if is_test_path(path) and _is_test_name(path)}
    if by_import:
        listed = await asyncio.to_thread(_git, wt.path, "ls-files")
        test_files = [p for p in listed.splitlines() if _is_test_name(p)]
        for path in sorted(by_import):
            importing = await asyncio.to_thread(_tests_importing, wt, path, test_files)
            if not importing:
                return _full(f"no test imports {path}, which the graph cannot trace")
            tests |= importing

    if not tests:
        return _full("no test file reaches the changed code")
    return AffectedTests(
        paths=tuple(sorted(tests)),
        full=False,
        reason=f"{len(tests)} affected test file(s) from {len(seeds)} changed symbol(s)",
    )


def test_command(contract: RepoContract, affected: AffectedTests) -> str:
    if affected.full or contract.test_files is None:
        return contract.test
    # a leading "-" would read as an option
    paths = (f"./{p}" if p.startswith("-") else p for p in affected.paths)
    files = " ".join(shlex.quote(p) for p in paths)
    return contract.test_files.replace("{files}", files)


test_command.__test__ = False  # type: ignore[attr-defined]  # not a pytest test when imported
