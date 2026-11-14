"""Resumable, idempotent import of a repo's GitHub history into tenant memory.

Each written item gets a marker in the task store, so a crashed or `--limit`ed run
continues where it stopped and never writes an episode twice. Review episodes use a
two-phase marker: "added" right after the memory write, "done" after the code link.
A run that finds "added" only retries the (idempotent) link. Any other marker value
means done. The cursor only moves after a run that finished everything it found.
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
from agentco.importer.locate import SymbolLocator, linkable
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
    review_bots: frozenset[str] = frozenset()


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
    relinked: int = 0  # link-only retries finished this run
    linked_by_fallback: int = 0  # links resolved against today's index, not the commit
    link_only: int = 0  # planned retries of episodes an earlier run already added
    awaiting_code: int = 0  # reviews still "added": no code graph to link them to yet

    @property
    def planned(self) -> int:
        return self.issues + self.features + self.pulls + self.reviews

    @property
    def to_write(self) -> int:
        """Planned episodes that need a Graphiti extraction (not link-only retries)."""
        return self.planned - self.link_only


@dataclass(frozen=True)
class _Outcome:
    linked: int = 0
    by_fallback: bool = False
    final: bool = (
        True  # False keeps a review's marker at "added" so a later run links it
    )


@dataclass(frozen=True)
class _Pending:
    created_at: datetime
    kind: int
    ident: int
    marker: str
    # writes the episode (or only retries its code link)
    write: Callable[[], Awaitable[_Outcome]]
    link_only: bool = False

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
    """Import issues, features, PRs and review threads, oldest first.

    `limit` (None = unlimited, otherwise >= 1) caps the items handled this run; link-only
    retries count toward it but are reported as `relinked`, not `written`.
    Bot-authored issues/PRs are skipped, but a human team-member review on a bot-authored
    PR is still imported (threads resolve against every fetched PR); only the PR episode
    itself is left out.

    A review episode written while no code graph could link it (none passed, or it has no
    indexed files and there is no checkout) keeps its "added" marker; a later run with
    code available only links it and never writes it again. Until then such retries are
    counted in `awaiting_code`: they use no `limit` slot, and the cursor stays put so the
    next run fetches them again.
    """
    tenant = rnd.tenant
    if not rnd.tenant == product.tenant == (code.tenant if code else rnd.tenant):
        raise ValueError("rnd, product and code must belong to one tenant")
    cursor_key = f"import:{tenant}:{repo}:cursor"
    if opts.limit is not None and opts.limit < 1:
        raise ValueError("limit must be at least 1")
    report = ImportReport()

    # Dry runs never touch the code graph, so they assume linking would work.
    can_link = opts.dry_run or (
        code is not None
        and (locator.has_checkout or bool(await code.file_hashes(repo)))
    )

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
        write: Callable[[bool], Awaitable[_Outcome]],
    ) -> None:
        key = marker(kind_name, ident)
        state = store.get_meta(key)
        link_only = state == "added"
        if state is not None and not link_only:
            report.already += 1
            return
        if link_only and not can_link:
            report.awaiting_code += 1
            return
        pending.append(
            _Pending(
                _utc(created_at),
                kind,
                ident,
                key,
                lambda: write(link_only),
                link_only,
            )
        )
        if kind == _ISSUE:
            report.issues += 1
        elif kind == _FEATURE:
            report.features += 1
        elif kind == _PR:
            report.pulls += 1
        else:
            report.reviews += 1
        if link_only:
            report.link_only += 1

    def add_to(scope_memory: ScopedMemory, scope: Scope, item: MemoryItem):
        async def write(link_only: bool) -> _Outcome:
            await scope_memory.add(scope, item)
            return _Outcome()

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

        async def write_pr(link_only: bool, pr=pr) -> _Outcome:
            files = await asyncio.to_thread(gh.pull_files, repo, pr.number)
            await rnd.add(Scope.RND, pull_item(repo, pr, files))
            return _Outcome()

        plan(pr.created_at, _PR, "pr", pr.number, write_pr)

    pulls_by_number = {p.number: p for p in pulls}
    selection = review_threads(comments, pulls_by_number, opts.review_bots)
    report.skipped.update(selection.skipped)
    for thread in selection.threads:
        if not created_ok(thread.root.created_at):
            continue
        pr = pulls_by_number[thread.root.pr_number]

        async def write_review(link_only: bool, thread=thread, pr=pr) -> _Outcome:
            located = await locator.resolve(thread.root, pr)
            symbol = located.symbol
            item = review_item(repo, thread, pr, symbol)
            if not link_only:
                await rnd.add(Scope.RND, item)
                store.set_meta(marker("review", thread.root.id), "added")
            if code is None or not can_link:
                return _Outcome(final=not linkable(thread.root))
            if symbol is None:
                return _Outcome()
            linked = await code.link_episode(
                repo, item.name, thread.root.path, symbol.qualname
            )
            return _Outcome(int(linked), by_fallback=linked and located.by_fallback)

        plan(thread.root.created_at, _REVIEW, "review", thread.root.id, write_review)

    if opts.dry_run:
        return report

    pending.sort(key=lambda p: p.order)
    cut = opts.limit is not None and len(pending) > opts.limit
    for work in pending[: opts.limit]:
        outcome = await work.write()
        report.linked += outcome.linked
        report.linked_by_fallback += int(outcome.by_fallback)
        if outcome.final:
            store.set_meta(work.marker, "done")
        else:
            report.awaiting_code += 1
        if not work.link_only:
            report.written += 1
        elif outcome.final:
            report.relinked += 1

    report.complete = not cut
    if report.complete and opts.since is None and not report.awaiting_code:
        store.set_meta(cursor_key, _utc(now).isoformat())
    return report
