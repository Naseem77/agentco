import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import openai
import redis.exceptions
import typer

from agentco.code.falkor_graph import build_falkor_code_graph
from agentco.code.graph import CodeGraphBackend, code_graph_for
from agentco.code.indexer import index_repo
from agentco.config import ConfigError, TenantConfig, family_warnings, load_config
from agentco.github.client import validate_repo
from agentco.memory.backend import MemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.memory.graphiti_backend import build_graphiti_backend
from agentco.memory.scopes import Scope, graph_name
from agentco.memory.seed import seed_company
from agentco.roles import Role
from agentco.store.task_store import TaskStore

# Seam: CLI tests replace this with an in-memory backend.
make_backend: Callable[[TenantConfig], MemoryBackend] = build_graphiti_backend

# Seam: CLI tests replace this with an in-memory code graph.
make_code_graph: Callable[[TenantConfig], CodeGraphBackend] = build_falkor_code_graph

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


def _build_backend(cfg: TenantConfig) -> MemoryBackend:
    try:
        return make_backend(cfg)
    except openai.OpenAIError as e:
        typer.echo(f"cannot create LLM client: {' '.join(str(e).split())}")
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
def task_new(title: str, config: Path = ConfigOption) -> None:
    """Create a task from the CLI and print its id."""
    cfg = _load(config)
    store = TaskStore(cfg.database_url)
    store.create_all()
    typer.echo(store.create_task(cfg.tenant, title, source="cli").id)


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
