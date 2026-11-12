"""What a task's diff touches: changed symbols, their callers and the tests that reach them.

Built live from a per-call in-memory overlay of the worktree's committed HEAD, so it
always reflects the engineer's latest commit and never writes unmerged code into the
shared graph. Engineer and reviewer read it to check every caller of what changed.
"""

import asyncio
from collections import defaultdict

from agentco.code.graph import InMemoryCodeGraph, code_graph_for
from agentco.code.indexer import index_repo
from agentco.roles import Role
from agentco.sandbox.affected import changed_line_ranges, is_test_path
from agentco.sandbox.worktree import Worktree, changed_files

_OVERLAY_TENANT = "impact-overlay"
_TEST_DEPTH = 3

Node = tuple[str, str]


def _overlaps(start: int, end: int, hunk: tuple[int, int]) -> bool:
    return start <= hunk[1] and hunk[0] <= end


async def impact_report(
    repo: str, wt: Worktree, base: str, max_callers: int = 8
) -> str:
    """Markdown impact of the diff against `base`; "" when nothing changed."""
    try:
        files = await asyncio.to_thread(changed_files, wt, base)
        if not files:
            return ""
        ranges = await asyncio.to_thread(changed_line_ranges, wt, base)
        code = code_graph_for(_OVERLAY_TENANT, Role.LIBRARIAN, InMemoryCodeGraph())
        await index_repo(repo, wt.path, code)
        symbols = await code.all_symbols(repo)
        edges = await code.call_edges(repo)
    except Exception as e:  # noqa: BLE001 - impact is advice; never block the task
        return f"impact unavailable: {type(e).__name__}: {e}"

    callers: dict[Node, set[Node]] = defaultdict(set)
    for e in edges:
        callers[(e.callee_path, e.callee)].add((e.caller_path, e.caller))

    lines: list[str] = []
    for path in sorted(files):
        if not (wt.path / path).is_file():
            lines.append(f"- {path} (deleted): check every importer of this file")
            continue
        hunks = ranges.get(path, [])
        changed = [
            s
            for s in symbols.get(path, [])
            if s.kind == "Function"
            and any(_overlaps(s.start_line, s.end_line, h) for h in hunks)
        ]
        if is_test_path(path):
            continue
        for symbol in changed:
            node = (path, symbol.qualname)
            direct = sorted(callers[node], key=lambda c: (c[0] == path, c))
            seen, frontier = {node}, {node}
            for _ in range(_TEST_DEPTH):
                frontier = {c for n in frontier for c in callers[n]} - seen
                seen |= frontier
            tests = sorted({p for p, _ in seen if is_test_path(p)})
            shown = ", ".join(f"{p}::{q}" for p, q in direct[:max_callers])
            more = len(direct) - max_callers
            lines.append(
                f"- {path}::{symbol.qualname} — callers: {shown or 'none found'}"
                + (f" (+{more} more)" if more > 0 else "")
                + (
                    f"; tests reaching it: {', '.join(tests)}"
                    if tests
                    else "; no test reaches it"
                )
            )
    if not lines:
        return ""
    return "Impact of the diff (from the live code graph):\n" + "\n".join(lines)
