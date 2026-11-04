"""Resumable, idempotent import of a repo's GitHub history into tenant memory.

Each written item gets a marker in the task store, so a crashed or `--limit`ed run
continues where it stopped and never writes an episode twice. The cursor only moves
after a run that finished everything it found.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agentco.code.graph import ScopedCodeGraph
from agentco.github.client import GitHub
from agentco.importer.episodes import (
    feature_item,
    is_feature,
    issue_item,
    pull_item,
    review_item,
    review_threads,
)
from agentco.importer.locate import SymbolLocator
from agentco.memory.backend import MemoryItem
from agentco.memory.gate import ScopedMemory
from agentco.memory.scopes import Scope
from agentco.store.task_store import TaskStore

_ISSUE, _FEATURE, _PR, _REVIEW = 0, 1, 2, 3


@dataclass(frozen=True)
class ImportOptions:
    since: datetime | None = None
    limit: int | None = None
    dry_run: bool = False


@dataclass
class ImportReport:
    issues: int = 0
    features: int = 0
    pulls: int = 0
    reviews: int = 0
    already: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    written: int = 0
    linked: int = 0
    complete: bool = True

    @property
    def planned(self) -> int:
        return self.issues + self.features + self.pulls + self.reviews


@dataclass(frozen=True)
class _Pending:
    created_at: datetime
    kind: int
    ident: int
    marker: str
    # writes the episode; returns the number of code links made
    write: Callable[[], Awaitable[int]]

    @property
    def order(self) -> tuple[datetime, int, int]:
        return (self.created_at, self.kind, self.ident)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


async def run_import(
    repo: str,
    gh: GitHub,
    store: TaskStore,
    rnd: ScopedMemory,
    product: ScopedMemory,
    locator: SymbolLocator,
    code: ScopedCodeGraph | None,
    opts: ImportOptions,
    now: datetime,
) -> ImportReport:
    tenant = rnd.tenant
    cursor_key = f"import:{tenant}:{repo}:cursor"
    report = ImportReport()

    fetch_since = opts.since
    if fetch_since is None:
        stored = store.get_meta(cursor_key)
        fetch_since = datetime.fromisoformat(stored) if stored else None

    issues = await asyncio.to_thread(gh.issues, repo, fetch_since)
    pulls = await asyncio.to_thread(gh.pulls, repo)
    comments = await asyncio.to_thread(gh.review_comments, repo, fetch_since)

    def created_ok(created_at: datetime) -> bool:
        return opts.since is None or _utc(created_at) >= _utc(opts.since)

    def marker(kind: str, ident: int) -> str:
        return f"import:{tenant}:{repo}:{kind}:{ident}"

    pending: list[_Pending] = []

    def plan(
        created_at: datetime,
        kind: int,
        kind_name: str,
        ident: int,
        write: Callable[[], Awaitable[int]],
    ) -> None:
        key = marker(kind_name, ident)
        if store.get_meta(key) is not None:
            report.already += 1
            return
        pending.append(_Pending(_utc(created_at), kind, ident, key, write))
        if kind == _ISSUE:
            report.issues += 1
        elif kind == _FEATURE:
            report.features += 1
        elif kind == _PR:
            report.pulls += 1
        else:
            report.reviews += 1

    def add_to(scope_memory: ScopedMemory, scope: Scope, item: MemoryItem):
        async def write() -> int:
            await scope_memory.add(scope, item)
            return 0

        return write

    for issue in issues:
        if issue.author.is_bot:
            report.skipped["bot"] += 1
            continue
        if not created_ok(issue.created_at):
            continue
        plan(
            issue.created_at,
            _ISSUE,
            "issue",
            issue.number,
            add_to(rnd, Scope.RND, issue_item(repo, issue)),
        )
        if is_feature(issue):
            plan(
                issue.created_at,
                _FEATURE,
                "feature",
                issue.number,
                add_to(product, Scope.PRODUCT, feature_item(repo, issue)),
            )

    for pr in pulls:
        if pr.author.is_bot:
            report.skipped["bot"] += 1
            continue
        if not created_ok(pr.created_at):
            continue

        async def write_pr(pr=pr) -> int:
            files = await asyncio.to_thread(gh.pull_files, repo, pr.number)
            await rnd.add(Scope.RND, pull_item(repo, pr, files))
            return 0

        plan(pr.created_at, _PR, "pr", pr.number, write_pr)

    pulls_by_number = {p.number: p for p in pulls}
    selection = review_threads(comments, pulls_by_number)
    report.skipped.update(selection.skipped)
    for thread in selection.threads:
        if not created_ok(thread.root.created_at):
            continue
        pr = pulls_by_number[thread.root.pr_number]

        async def write_review(thread=thread, pr=pr) -> int:
            symbol = await locator.locate(thread.root, pr)
            item = review_item(repo, thread, pr, symbol)
            await rnd.add(Scope.RND, item)
            if symbol is None or code is None:
                return 0
            linked = await code.link_episode(
                repo, item.name, thread.root.path, symbol.qualname
            )
            return int(linked)

        plan(thread.root.created_at, _REVIEW, "review", thread.root.id, write_review)

    if opts.dry_run:
        return report

    pending.sort(key=lambda p: p.order)
    cut = opts.limit is not None and len(pending) > opts.limit
    for work in pending[: opts.limit]:
        report.linked += await work.write()
        store.set_meta(work.marker, "done")
        report.written += 1

    report.complete = not cut
    if report.complete and opts.since is None:
        store.set_meta(cursor_key, _utc(now).isoformat())
    return report
