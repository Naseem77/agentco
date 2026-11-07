import asyncio
import math
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from datetime import UTC, datetime
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
from agentco.sandbox.runner import CommandRunner, runner_for
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.orchestrator import StepFailed, TaskContext, run_many
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
app.add_typer(task_app, name="task")

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

    async def _run() -> list[TaskRecord | BaseException]:
        runtime = make_runtime(cfg)
        async with AsyncExitStack() as stack:
            if isinstance(runtime, AbstractAsyncContextManager):
                runtime = await stack.enter_async_context(runtime)
            ctx = TaskContext(
                cfg=cfg,
                store=store,
                runtime=runtime,
                gate=MemoryGate(cfg.tenant, _build_backend(cfg)),
                github=make_github_writer(),
                repo=repo,
                repo_root=checkout,
                contract=contract,
                runner=make_command_runner(contract, checkout, host),
                main=main,
            )
            return await run_many([(ctx, t.id) for t in tasks], cfg.concurrency)

    try:
        with _falkordb_guard(cfg):
            results = asyncio.run(_run())
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, StepFailed | GitHubError
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
    if code:
        raise typer.Exit(code)


def _escalation_of(store: TaskStore, task_id: int) -> dict:
    escalation = store.latest_artifact(task_id, ArtifactKind.ESCALATION)
    return escalation.body if escalation else {}


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
        return
    if add_budget is not None:
        key = f"task:{task_id}:extra_budget"
        store.set_meta(key, str(float(store.get_meta(key) or 0) + add_budget))
    typer.echo(
        f"task {task_id}: {decision.value} recorded; run `agentco task run {task_id}`"
    )
