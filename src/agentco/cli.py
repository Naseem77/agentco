import asyncio
import dataclasses
import json
import math
import os
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import openai
import redis.exceptions
import typer
from graphiti_core.llm_client.errors import (
    EmptyResponseError,
    RateLimitError,
    RefusalError,
)

from agentco.code.falkor_graph import build_falkor_code_graph
from agentco.code.graph import CodeGraphBackend, code_graph_for
from agentco.code.indexer import NotAGitRepo, index_repo, repo_root
from agentco.config import ConfigError, TenantConfig, family_warnings, load_config
from agentco.github.client import GhCli, GitHub, GitHubWriter, validate_repo
from agentco.github.errors import GitHubError
from agentco.importer.locate import SymbolLocator
from agentco.importer.run import ImportOptions, ImportReport, run_import
from agentco.intake.run import IntakeReport, run_intake
from agentco.learning.librarian import LearnReport, Librarian
from agentco.learning.metrics import MetricsReport, metrics_report
from agentco.llm.copilot import CopilotRuntime
from agentco.llm.runtime import AgentRuntime
from agentco.memory.backend import Fact, MemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.graphiti_backend import build_graphiti_backend
from agentco.memory.scopes import Scope, graph_name
from agentco.memory.seed import seed_company
from agentco.repo_contract import (
    RepoContract,
    RepoContractMissing,
    load_repo_contract,
)
from agentco.roles import Role
from agentco.sandbox.pool import WorktreePool, run_setup
from agentco.sandbox.runner import CommandRunner, runner_for
from agentco.sandbox.worktree import GitError, refresh_main
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.webhook import IntakeTrigger, make_server
from agentco.workflow.orchestrator import StepFailed, TaskContext, run_many
from agentco.workflow.preflight import RestoreError
from agentco.workflow.states import TaskState

# Seam: CLI tests replace this with an in-memory backend.
make_backend: Callable[[TenantConfig], MemoryBackend] = build_graphiti_backend

# Seam: CLI tests replace this with an in-memory code graph.
make_code_graph: Callable[[TenantConfig], CodeGraphBackend] = build_falkor_code_graph

# Seam: CLI tests replace this with an in-memory GitHub.
make_github: Callable[[], GitHub] = GhCli

# Seam: CLI tests replace this with a scripted runtime.
make_runtime: Callable[[TenantConfig], AgentRuntime] = lambda cfg: CopilotRuntime()

# Seam: CLI tests replace this with a scripted command runner.
make_command_runner: Callable[[RepoContract, Path, bool], CommandRunner] = runner_for

# Seam: CLI tests replace this with an in-memory GitHub writer.
make_github_writer: Callable[[], GitHubWriter] = GhCli

app = typer.Typer(no_args_is_help=True, help="A company of AI agents.")
memory_app = typer.Typer(no_args_is_help=True, help="Query company memory.")
task_app = typer.Typer(no_args_is_help=True, help="Manage tasks.")
app.add_typer(memory_app, name="memory")
pool_app = typer.Typer(no_args_is_help=True, help="Warm worktrees.")
app.add_typer(task_app, name="task")
app.add_typer(pool_app, name="pool")

ConfigOption = typer.Option(
    Path("agentco.config.yml"), "--config", help="Path to the tenant config."
)


def _load(config: Path) -> TenantConfig:
    try:
        return load_config(config)
    except ConfigError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e


@contextmanager
def _falkordb_guard(cfg: TenantConfig) -> Iterator[None]:
    try:
        yield
    except (
        ConnectionError,
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
    ) as e:
        typer.echo(f"cannot reach FalkorDB at {cfg.falkordb_host}:{cfg.falkordb_port}")
        raise typer.Exit(2) from e
    except redis.exceptions.ResponseError as e:
        typer.echo(f"FalkorDB error: {_one_line(e)}")
        raise typer.Exit(2) from e


def _one_line(error: object) -> str:
    return " ".join(str(error).split())


# Raised by the OpenAI client and by Graphiti's own LLM wrappers while extracting.
LLM_ERRORS = (openai.OpenAIError, RateLimitError, RefusalError, EmptyResponseError)


class _DryRunBackend:
    """Stands in for the memory backend: a dry run reads markers and writes nothing."""

    async def add(self, graph: str, scope: Scope, item: MemoryItem) -> None:
        raise AssertionError("a dry run must not write memory")

    async def search(self, graphs: list[str], query: str, limit: int) -> list[Fact]:
        raise AssertionError("a dry run must not search memory")

    async def ensure_graph(self, graph: str) -> None:
        raise AssertionError("a dry run must not create graphs")


def _build_backend(cfg: TenantConfig) -> MemoryBackend:
    try:
        return make_backend(cfg)
    except openai.OpenAIError as e:
        typer.echo(f"cannot create LLM client: {_one_line(e)}")
        raise typer.Exit(1) from e


@app.command()
def init(config: Path = ConfigOption) -> None:
    """Create local state, the four graphs, and seed company memory."""
    cfg = _load(config)
    for line in family_warnings(cfg):
        typer.echo(line)
    store = TaskStore(cfg.database_url)
    store.create_all()

    async def _run() -> bool:
        backend = _build_backend(cfg)
        for scope in Scope:
            await backend.ensure_graph(graph_name(cfg.tenant, scope))
        mem = MemoryGate(cfg.tenant, backend).for_role(Role.HUMAN)
        return await seed_company(mem, store, datetime.now(UTC))

    with _falkordb_guard(cfg):
        seeded = asyncio.run(_run())
    typer.echo("seeded" if seeded else "already seeded")


@memory_app.command("search")
def memory_search(
    query: str,
    role: Annotated[Role, typer.Option("--as", help="Role to search as.")],
    config: Path = ConfigOption,
) -> None:
    """Search the graphs a role may read; prints graph<TAB>fact per result."""
    cfg = _load(config)
    with _falkordb_guard(cfg):
        mem = MemoryGate(cfg.tenant, _build_backend(cfg)).for_role(role)
        facts = asyncio.run(mem.search(query))
    for fact in facts:
        typer.echo(f"{fact.graph}\t{fact.text}")


@task_app.command("new")
def task_new(
    title: str,
    body: Annotated[
        str | None,
        typer.Option("--body", help="What to do, in full; defaults to the title."),
    ] = None,
    config: Path = ConfigOption,
) -> None:
    """Create a task from the CLI and print its id."""
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    task = store.create_task(cfg.tenant, title, source="cli")
    store.put_artifact(
        task.id, ArtifactKind.REQUEST, Role.HUMAN, {"body": body or title}
    )
    typer.echo(task.id)


@app.command()
def index(
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo as OWNER/NAME.")],
    repo_path: Annotated[
        Path, typer.Option("--repo-path", help="Local checkout to index.")
    ],
    config: Path = ConfigOption,
) -> None:
    """Index a checkout's code structure into the code graph (incremental).

    Note: skipped counts working-tree files and deleted counts graph removals, so a file can be in both.
    """
    cfg = _load(config)
    try:
        validate_repo(repo)
        with _falkordb_guard(cfg):
            code = code_graph_for(cfg.tenant, Role.LIBRARIAN, make_code_graph(cfg))
            report = asyncio.run(index_repo(repo, repo_path, code))
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    typer.echo(
        f"indexed {report.indexed}, unchanged {report.unchanged}, "
        f"deleted {report.deleted}, skipped {report.skipped}, calls {report.calls}"
    )


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(message)
    return typer.Exit(code)


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        return datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise _fail(f"--since must be a date like 2026-01-31, got {since!r}") from None


def _parse_limit(limit: str | None) -> int | None:
    if limit is None:
        return None
    try:
        value = int(limit)
    except ValueError:
        raise _fail(f"--limit must be an integer, got {limit!r}") from None
    if value < 1:
        raise _fail(f"--limit must be at least 1, got {value}")
    return value


def _dry_run_lines(report: ImportReport) -> list[str]:
    skipped = " ".join(f"{k}={v}" for k, v in sorted(report.skipped.items()))
    lines = [
        f"issues: {report.issues}",
        f"features: {report.features}",
        f"pull requests: {report.pulls}",
        f"review comments: {report.reviews}",
        f"already imported: {report.already}",
        f"skipped: {skipped or 'none'}",
        f"episodes to write: {report.to_write} (each runs one Graphiti LLM extraction)",
    ]
    if report.link_only:
        lines.append(f"link-only retries: {report.link_only}")
    return lines


def _result_lines(report: ImportReport, max_items: int | None) -> list[str]:
    lines = []
    if report.relinked:
        lines.append(f"relinked {report.relinked} episodes")
    if report.linked_by_fallback:
        lines.append(
            f"{report.linked_by_fallback} episodes linked against current code because "
            "their commits are not in the checkout; fetch PR heads for exact links "
            "(see README)"
        )
    if report.awaiting_code:
        lines.append(
            f"{report.awaiting_code} review episodes await a code graph; "
            "import or index with --repo-path to link them"
        )
    if report.complete:
        lines.append(
            f"imported {report.written} episodes ({report.linked} linked to code)"
        )
    else:
        lines.append(
            f"stopped at --limit {max_items}: imported {report.written} "
            f"of {report.to_write} episodes; run again to continue"
        )
    return lines


@app.command("import")
def import_(
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo as OWNER/NAME.")],
    repo_path: Annotated[
        Path | None,
        typer.Option("--repo-path", help="Local checkout: index it and link comments."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only import items created on/after YYYY-MM-DD."),
    ] = None,
    limit: Annotated[
        str | None,
        typer.Option("--limit", help="Import at most N items this run (N >= 1)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count what would be imported; write nothing."),
    ] = False,
    config: Path = ConfigOption,
) -> None:
    """Import a repo's GitHub issues, PRs and review comments into memory (resumable)."""
    try:
        validate_repo(repo)
    except ValueError as e:
        raise _fail(str(e)) from e
    since_at = _parse_since(since)
    max_items = _parse_limit(limit)
    try:
        checkout = repo_root(repo_path) if repo_path is not None else None
    except NotAGitRepo as e:
        raise _fail(str(e)) from e
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    # Taken before any fetch so items created during the run are seen next time.
    started_at = datetime.now(UTC)

    async def _run() -> ImportReport:
        if dry_run:
            gate = MemoryGate(cfg.tenant, _DryRunBackend())
            code = None
        else:
            backend = _build_backend(cfg)
            gate = MemoryGate(cfg.tenant, backend)
            await backend.ensure_graph(graph_name(cfg.tenant, Scope.RND))
            await backend.ensure_graph(graph_name(cfg.tenant, Scope.PRODUCT))
            code = code_graph_for(cfg.tenant, Role.LIBRARIAN, make_code_graph(cfg))
            await code.ensure_schema()
            if checkout is not None:
                done = await index_repo(repo, checkout, code)
                typer.echo(
                    f"indexed {done.indexed}, unchanged {done.unchanged}, "
                    f"deleted {done.deleted}, skipped {done.skipped}, calls {done.calls}"
                )
        return await run_import(
            repo,
            make_github(),
            store,
            gate.for_role(Role.LIBRARIAN),
            gate.for_role(Role.PM),
            SymbolLocator(repo, code, checkout),
            code,
            ImportOptions(since=since_at, limit=max_items, dry_run=dry_run),
            started_at,
        )

    try:
        with _falkordb_guard(cfg):
            report = asyncio.run(_run())
    except NotAGitRepo as e:
        raise _fail(str(e)) from e
    except GitHubError as e:
        raise _fail(str(e), 3) from e
    except LLM_ERRORS as e:
        raise _fail(f"LLM request failed: {_one_line(e)}") from e
    lines = _dry_run_lines(report) if dry_run else _result_lines(report, max_items)
    for line in lines:
        typer.echo(line)


# States `task run --all` picks up; AWAITING_HUMAN only once a decision is waiting.
_RUNNABLE = (
    TaskState.INTAKE,
    TaskState.PM,
    TaskState.TRIAGE,
    TaskState.ENGINEERING,
    TaskState.PREFLIGHT,
    TaskState.INTERNAL_REVIEW,
    TaskState.LEAD_DECISION,
)


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ANSWER = "answer"


def _task_of(store: TaskStore, cfg: TenantConfig, task_id: int) -> TaskRecord:
    try:
        task = store.get(task_id)
    except KeyError:
        task = None
    if task is None or task.tenant != cfg.tenant:
        raise _fail(f"task {task_id} not found")
    return task


def _has_waiting_decision(store: TaskStore, task: TaskRecord) -> bool:
    escalation = store.latest_artifact(task.id, ArtifactKind.ESCALATION)
    decision = store.latest_artifact(task.id, ArtifactKind.HUMAN_DECISION)
    return decision is not None and (escalation is None or decision.id > escalation.id)


def _tasks_to_run(
    store: TaskStore, cfg: TenantConfig, task_id: int | None
) -> list[TaskRecord]:
    if task_id is not None:
        return [_task_of(store, cfg, task_id)]
    states = (*_RUNNABLE, TaskState.AWAITING_HUMAN)
    return [
        t
        for t in store.list_tasks(cfg.tenant, states)
        if t.state is not TaskState.AWAITING_HUMAN or _has_waiting_decision(store, t)
    ]


def _remember_target(store: TaskStore, task_id: int, repo: str, main: str) -> None:
    target = {"repo": repo, "main": main}
    latest = store.latest_artifact(task_id, ArtifactKind.TARGET)
    if latest is None or latest.body != target:
        store.put_artifact(task_id, ArtifactKind.TARGET, Role.HUMAN, target)


@asynccontextmanager
async def _started(runtime: AgentRuntime) -> AsyncIterator[AgentRuntime]:
    async with AsyncExitStack() as stack:
        if isinstance(runtime, AbstractAsyncContextManager):
            runtime = await stack.enter_async_context(runtime)
        yield runtime


def _best_effort_github() -> GitHub | None:
    """The Librarian reads review threads when it can; its absence is not an error."""
    try:
        return make_github()
    except GitHubError:
        return None


async def _learn(
    cfg: TenantConfig,
    store: TaskStore,
    gate: MemoryGate,
    runtime: AgentRuntime,
    gh: GitHub | None,
    task_ids: Iterable[int],
    *,
    only_needed: bool = False,
) -> list[LearnReport]:
    """Run the Librarian per task; what it cannot do becomes a report error, not a crash."""
    librarian = Librarian(cfg, store, gate, runtime, gh)
    reports = []
    for task_id in task_ids:
        if only_needed and not librarian.needs_learning(task_id):
            continue
        try:
            reports.append(await librarian.learn_task(task_id))
        except LLM_ERRORS as e:
            reports.append(
                LearnReport(task_id, errors=[f"LLM request failed: {_one_line(e)}"])
            )
    return reports


def _echo_warnings(reports: Iterable[LearnReport]) -> None:
    for report in reports:
        for error in report.errors:
            typer.echo(f"warning: librarian #{report.task_id}: {_one_line(error)}")


def _learn_from(
    cfg: TenantConfig,
    store: TaskStore,
    task_ids: list[int],
    gh: GitHub | None = None,
    *,
    only_needed: bool = False,
) -> list[LearnReport]:
    """Run the Librarian outside `task run` and `intake`; its warnings are printed."""

    async def _run() -> list[LearnReport]:
        async with _started(make_runtime(cfg)) as runtime:
            gate = MemoryGate(cfg.tenant, _build_backend(cfg))
            return await _learn(
                cfg, store, gate, runtime, gh, task_ids, only_needed=only_needed
            )

    with _falkordb_guard(cfg):
        reports = asyncio.run(_run())
    _echo_warnings(reports)
    return reports


def _learn_after_decision(cfg: TenantConfig, store: TaskStore, task_id: int) -> None:
    """The decision is already saved: whatever goes wrong here is a warning, not an exit code."""
    try:
        _learn_from(cfg, store, [task_id], _best_effort_github())
    except typer.Exit:
        typer.echo(
            f"warning: librarian #{task_id}: skipped; run `agentco learn {task_id}`"
        )
    except Exception as e:  # noqa: BLE001
        typer.echo(f"warning: librarian #{task_id}: {_one_line(e)}")


def _final_line(store: TaskStore, task: TaskRecord) -> str:
    line = f"task {task.id}: {task.state.value}"
    pr = store.latest_artifact(task.id, ArtifactKind.PR)
    if task.state is TaskState.PR_OPEN and pr is not None:
        line += f" {pr.body['url']}"
    return line


@task_app.command("run")
def task_run(
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo as OWNER/NAME.")],
    task_id: Annotated[
        int | None, typer.Argument(help="Task id; omit with --all.")
    ] = None,
    all_tasks: Annotated[
        bool, typer.Option("--all", help="Run every task that can make progress.")
    ] = False,
    repo_path: Annotated[
        Path, typer.Option("--repo-path", help="Local checkout to work in.")
    ] = Path("."),
    main: Annotated[str, typer.Option("--main", help="The base branch.")] = "main",
    host: Annotated[
        bool,
        typer.Option(
            "--host", help="Run repo commands on this machine, not in the sandbox."
        ),
    ] = False,
    config: Path = ConfigOption,
) -> None:
    """Run a task until it opens a PR, needs a human, or is cancelled (resumes)."""
    if (task_id is not None) == all_tasks:
        raise _fail("give exactly one of a task id or --all")
    try:
        validate_repo(repo)
        checkout = repo_root(repo_path)
        contract = load_repo_contract(checkout)
    except (ValueError, NotAGitRepo, RepoContractMissing) as e:
        raise _fail(str(e)) from e
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    tasks = _tasks_to_run(store, cfg, task_id)
    if not tasks:
        typer.echo("no tasks to run")
        return
    for task in tasks:
        _remember_target(store, task.id, repo, main)

    pool = (
        WorktreePool(checkout, cfg.pool.size)
        if cfg.pool.size > 0 and contract.setup
        else None
    )

    async def _run() -> tuple[list[TaskRecord | BaseException], list[LearnReport]]:
        async with _started(make_runtime(cfg)) as runtime:
            gate = MemoryGate(cfg.tenant, _build_backend(cfg))
            ctx = TaskContext(
                cfg=cfg,
                store=store,
                runtime=runtime,
                gate=gate,
                github=make_github_writer(),
                repo=repo,
                repo_root=checkout,
                contract=contract,
                runner=make_command_runner(contract, checkout, host),
                main=main,
                pool=pool,
            )
            outcomes = await run_many([(ctx, t.id) for t in tasks], cfg.concurrency)
            finished = [r.id for r in outcomes if isinstance(r, TaskRecord)]
            learned = await _learn(
                cfg, store, gate, runtime, _best_effort_github(), finished
            )
            return outcomes, learned

    try:
        with _falkordb_guard(cfg):
            results, learned = asyncio.run(_run())
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, StepFailed | GitHubError | GitError | RestoreError
                ):
                    raise result
    except GitHubError as e:
        raise _fail(str(e), 3) from e
    except LLM_ERRORS as e:
        raise _fail(f"LLM request failed: {_one_line(e)}") from e
    code = 0
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, TaskRecord):
            typer.echo(_final_line(store, result))
            continue
        typer.echo(f"task {task.id}: failed: {_one_line(result)}")
        code = code or (4 if isinstance(result, StepFailed) else 3)
    _echo_warnings(learned)
    if code:
        raise typer.Exit(code)


@pool_app.command("fill")
def pool_fill(
    repo_path: Annotated[
        Path, typer.Option("--repo-path", help="Local checkout to keep warm.")
    ] = Path("."),
    main: Annotated[str, typer.Option("--main", help="The base branch.")] = "main",
    host: Annotated[
        bool,
        typer.Option("--host", help="Run setup on this machine, not in the sandbox."),
    ] = False,
    config: Path = ConfigOption,
) -> None:
    """Prepare worktrees ahead of time by running the contract's `setup`."""
    cfg = _load(config)
    if cfg.pool.size == 0:
        typer.echo("pool disabled")
        return
    try:
        checkout = repo_root(repo_path)
        contract = load_repo_contract(checkout)
    except (ValueError, NotAGitRepo, RepoContractMissing) as e:
        raise _fail(str(e)) from e
    if not contract.setup:
        typer.echo("no setup command; nothing to warm")
        return
    runner = make_command_runner(contract, checkout, host)
    pool = WorktreePool(checkout, cfg.pool.size)

    def prepare(path: Path) -> float:
        return run_setup(runner, contract, path, cfg.pool.setup_timeout_s)

    try:
        base = refresh_main(checkout, main)
        added = pool.fill(base, prepare)
    except GitError as e:
        raise _fail(str(e), 3) from e
    typer.echo(f"pool: added {added}")
    for failure in pool.failures:
        typer.echo(f"setup failed: {_one_line(failure)}")
    if pool.failures:
        raise typer.Exit(4)


def _escalation_of(store: TaskStore, task_id: int) -> dict:
    escalation = store.latest_artifact(task_id, ArtifactKind.ESCALATION)
    return escalation.body if escalation else {}


def _pr_findings(store: TaskStore, task_id: int) -> int:
    lead = store.latest_artifact(task_id, ArtifactKind.LEAD)
    if lead is None or lead.body.get("source") != "pr":
        return 0
    return len(lead.body.get("findings_to_fix", []))


def _intake_lines(store: TaskStore, report: IntakeReport) -> list[str]:
    lines = [f"#{n} merged" for n in report.merged]
    lines += [f"#{n} closed" for n in report.closed]
    stopped = {n for n, _ in report.escalated}
    for n in report.reentered:
        if n in stopped:
            continue
        k = _pr_findings(store, n)
        lines.append(
            f"#{n} back to triage (round {store.get(n).round}): {k} finding(s)"
        )
    lines += [f"#{n} needs a human: {category}" for n, category in report.escalated]
    lines.append(f"{report.known} known, {len(report.deferred)} deferred")
    lines += [f"error: {_one_line(e)}" for e in report.errors]
    return lines


def _intake_once(
    cfg: TenantConfig, store: TaskStore, repo: str
) -> tuple[IntakeReport, list[LearnReport]]:
    async def _run() -> tuple[IntakeReport, list[LearnReport]]:
        async with _started(make_runtime(cfg)) as runtime:
            gate = MemoryGate(cfg.tenant, _build_backend(cfg))
            gh = make_github()
            report = await run_intake(cfg, store, gh, runtime, gate, repo)
            learned = await _learn(cfg, store, gate, runtime, gh, report.touched)
            return report, learned

    return asyncio.run(_run())


@app.command()
def intake(
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo as OWNER/NAME.")],
    config: Path = ConfigOption,
) -> None:
    """Read feedback on open PRs and send real findings back to the team (re-runnable)."""
    try:
        validate_repo(repo)
    except ValueError as e:
        raise _fail(str(e)) from e
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()

    try:
        with _falkordb_guard(cfg):
            report, learned = _intake_once(cfg, store, repo)
    except GitHubError as e:
        raise _fail(str(e), 3) from e
    except LLM_ERRORS as e:
        raise _fail(f"LLM request failed: {_one_line(e)}") from e
    if report.busy:
        typer.echo(f"another intake for {repo} is running; skipped")
        return
    for line in _intake_lines(store, report):
        typer.echo(line)
    _echo_warnings(learned)
    if report.github_failed:
        raise typer.Exit(3)
    if report.errors:
        raise typer.Exit(4)


@app.command()
def serve(
    repo: Annotated[str, typer.Option("--repo", help="GitHub repo as OWNER/NAME.")],
    host: Annotated[str, typer.Option("--host", help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8787,
    config: Path = ConfigOption,
) -> None:
    """Receive signed GitHub webhooks and run the intake for the repo (one at a time)."""
    try:
        validate_repo(repo)
    except ValueError as e:
        raise _fail(str(e)) from e
    cfg = _load(config)
    secret = os.environ.get(cfg.webhook.secret_env, "")
    if not secret:
        raise _fail(
            f"set the webhook secret in ${cfg.webhook.secret_env} (it is never read from the config)"
        )
    store = TaskStore(cfg.database_url)
    store.create_all()

    def run_once() -> None:
        with _falkordb_guard(cfg):
            report, learned = _intake_once(cfg, store, repo)
        if report.busy:
            typer.echo(f"another intake for {repo} is running; skipped")
            return
        for line in _intake_lines(store, report):
            typer.echo(line)
        _echo_warnings(learned)

    trigger = IntakeTrigger(run_once, cfg.webhook.debounce_s)
    server = make_server(
        host,
        port,
        tenant=cfg.tenant,
        store=store,
        repo=repo,
        secret=secret.encode(),
        max_body=cfg.webhook.max_body_bytes,
        trigger=trigger,
    )
    bound_host, bound_port = server.server_address[:2]
    typer.echo(f"listening on http://{bound_host}:{bound_port}/webhook for {repo}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        trigger.close()


@app.command()
def inbox(config: Path = ConfigOption) -> None:
    """List the tasks waiting for a human decision."""
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    waiting = store.list_tasks(cfg.tenant, [TaskState.AWAITING_HUMAN])
    if not waiting:
        typer.echo("nothing is waiting for a human")
        return
    typer.echo("id\ttitle\tcategory\tfrom_state\treason\toptions\tcost")
    for task in waiting:
        body = _escalation_of(store, task.id)
        fields = [
            str(task.id),
            task.title,
            str(body.get("category", "")),
            str(body.get("from_state", "")),
            str(body.get("reason", "")),
            "; ".join(str(o) for o in body.get("options", [])),
            f"${task.cost_usd:.2f}",
        ]
        typer.echo("\t".join(_one_line(f) for f in fields))


def _decision_text(task: TaskRecord, decision: Decision, note: str, body: dict) -> str:
    reason = body.get("reason", "")
    text = (
        f"Human decision ({decision.value}) on task {task.id} '{task.title}': "
        f"the team escalated from {body.get('from_state', 'unknown')} "
        f"({body.get('category', 'unknown')}) because: {reason}."
    )
    return f"{text} Note: {note}" if note else text


@app.command()
def decide(
    task_id: int,
    decision: Decision,
    note: Annotated[
        str, typer.Option("--note", help="Your guidance; required for answer.")
    ] = "",
    add_budget: Annotated[
        float | None,
        typer.Option("--add-budget", help="Extra USD this task may spend."),
    ] = None,
    config: Path = ConfigOption,
) -> None:
    """Answer an escalated task; `reject` cancels it, the rest resume on `task run`."""
    if decision is Decision.ANSWER and not note.strip():
        raise _fail("answer needs --note with your answer")
    if add_budget is not None and not (math.isfinite(add_budget) and add_budget > 0):
        raise _fail(f"--add-budget must be a positive amount, got {add_budget}")
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    task = _task_of(store, cfg, task_id)
    if task.state is not TaskState.AWAITING_HUMAN:
        raise _fail(f"task {task_id} is {task.state.value}, not awaiting_human")
    earlier = store.artifacts(task_id, ArtifactKind.HUMAN_DECISION)
    item = MemoryItem(
        f"human-decision:{task_id}:{len(earlier) + 1}",
        _decision_text(task, decision, note, _escalation_of(store, task_id)),
        "human decision",
        datetime.now(UTC),
    )
    # Memory first: if FalkorDB is down nothing about the task has changed.
    with _falkordb_guard(cfg):
        mem = MemoryGate(cfg.tenant, _build_backend(cfg)).for_role(Role.HUMAN)
        asyncio.run(mem.add(Scope.COMPANY, item))
    store.put_artifact(
        task_id,
        ArtifactKind.HUMAN_DECISION,
        Role.HUMAN,
        {"decision": decision.value, "note": note},
    )
    if decision is Decision.REJECT:
        store.transition(
            task_id, TaskState.CANCELLED, Role.HUMAN, "rejected by a human"
        )
        typer.echo(f"task {task_id}: cancelled")
        _learn_after_decision(cfg, store, task_id)
        return
    if add_budget is not None:
        key = f"task:{task_id}:extra_budget"
        store.set_meta(key, str(float(store.get_meta(key) or 0) + add_budget))
    typer.echo(
        f"task {task_id}: {decision.value} recorded; run `agentco task run {task_id}`"
    )
    _learn_after_decision(cfg, store, task_id)


@app.command()
def learn(
    task_id: Annotated[
        int | None, typer.Argument(help="Task id; omit with --all.")
    ] = None,
    all_tasks: Annotated[
        bool,
        typer.Option("--all", help="Every task the Librarian has not finished with."),
    ] = False,
    config: Path = ConfigOption,
) -> None:
    """Run the Librarian again (re-runnable): it writes only what is not in memory yet."""
    if (task_id is not None) == all_tasks:
        raise _fail("give exactly one of a task id or --all")
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    if task_id is not None:
        ids = [_task_of(store, cfg, task_id).id]
    else:
        ids = [t.id for t in store.list_tasks(cfg.tenant)]
    try:
        reports = _learn_from(cfg, store, ids, make_github(), only_needed=all_tasks)
    except GitHubError as e:
        raise _fail(str(e), 3) from e
    if not reports:
        typer.echo("nothing to learn")
    for report in reports:
        typer.echo(
            f"#{report.task_id}: {report.added} added, {report.duplicates} duplicate"
        )


def _metrics_since(days: str | None) -> datetime | None:
    if days is None:
        return None
    try:
        value = int(days)
    except ValueError:
        raise _fail(f"--since must be a number of days, got {days!r}") from None
    if value < 1:
        raise _fail(f"--since must be at least 1 day, got {value}")
    return datetime.now(UTC) - timedelta(days=value)


def _metrics_lines(report: MetricsReport) -> list[str]:
    lines = [
        f"{name}: {value:g}" if value == int(value) else f"{name}: {value:.2f}"
        for name, value in report.totals.items()
    ]
    if report.stale_rules:
        lines.append(f"stale rules: {', '.join(report.stale_rules)}")
    header = ["id", "state", "assignee", "rounds", "pr_rounds", "pr_findings"]
    header += ["pr_real", "escalations", "requests", "cost_usd", "hours", "title"]
    rows = [header]
    for t in report.tasks:
        rows.append(
            [
                str(t.task_id),
                t.state.value,
                t.assignee.value if t.assignee else "-",
                str(t.rounds),
                str(t.pr_rounds),
                str(t.pr_findings),
                str(t.pr_real),
                str(sum(t.escalations.values())),
                f"{t.premium_requests:g}",
                f"{t.cost_usd:.2f}",
                "-" if t.duration_h is None else f"{t.duration_h:.1f}",
                t.title,
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header) - 1)]
    lines.append("")
    for row in rows:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])]
        lines.append("  ".join([*cells, row[-1]]))
    return lines


@app.command()
def metrics(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only tasks started in the last DAYS days."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    config: Path = ConfigOption,
) -> None:
    """Show how the loop is doing, derived from the audit log (no services needed)."""
    cfg = _load(config)
    since_at = _metrics_since(since)
    store = TaskStore(cfg.database_url)
    store.create_all()
    report = metrics_report(
        store, cfg.tenant, cfg.thresholds.stale_after, since=since_at
    )
    if as_json:
        typer.echo(json.dumps(dataclasses.asdict(report), indent=2))
        return
    for line in _metrics_lines(report):
        typer.echo(line)
