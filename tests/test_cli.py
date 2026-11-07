import asyncio
import dataclasses
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import openai
import pytest
import redis.exceptions
import yaml
from graphiti_core.llm_client.errors import (
    EmptyResponseError,
    RateLimitError,
    RefusalError,
)
from sqlalchemy import update
from typer.testing import CliRunner

from agentco import cli
from agentco.code.graph import InMemoryCodeGraph
from agentco.github.client import FakeGitHub, GhCli, GitHubError
from agentco.github.models import GhPull, GhReviewComment, GhUser
from agentco.learning.librarian import LearnReport
from agentco.llm.fake import FakeRuntime
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.sandbox.worktree import GitError
from agentco.store.models import ArtifactKind, AuditRow
from agentco.store.task_store import TaskStore
from agentco.workflow.preflight import RestoreError
from agentco.workflow.states import TaskState

# The orchestrator rig lives next to its tests and imports its siblings by bare name.
sys.path.insert(0, str(Path(__file__).parent / "workflow"))
from orch_helpers import (
    LEAD_OK,
    PM_JSON,
    QA_OK,
    REVIEW_OK,
    RULES_OK,
    SEED_FACT,
    STUCK,
    engineer,
    happy,
    triage_json,
)
from script_runner import FILES, RED_CMD, ScriptRunner

runner = CliRunner()
GITHUB_FIXTURES = Path(__file__).parent / "github" / "fixtures"


class FailingBackend(InMemoryBackend):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def ensure_graph(self, graph):
        raise self._error

    async def add(self, graph, scope, item):
        raise self._error


def _write_config(tmp_path):
    models = {
        "pm": "m-pm",
        "triage": "m-triage",
        "architect": "m-arch",
        "senior": "m-senior",
        "junior": "m-junior",
        "reviewer": "m-rev",
        "qa": "m-qa",
        "lead": "m-lead",
        "librarian": "m-lib",
    }
    path = tmp_path / "agentco.config.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "tenant": "acme",
                "database_url": f"sqlite:///{tmp_path}/state.db",
                "roles": {
                    r: {"model": m, "family": f"fam-{m}"} for r, m in models.items()
                },
            }
        )
    )
    return path


class _TaskRunSeams:
    """What `task run` gets from the make_* seams; tests script it with `script()`."""

    def __init__(self) -> None:
        self.runtime = FakeRuntime({})
        self.github = FakeGitHub([], [], [], {})
        self.runner = ScriptRunner(codes={RED_CMD: 1})
        self.runner_args: list[tuple] = []

    def script(self, script, premium: float = 1.0) -> FakeRuntime:
        self.runtime = FakeRuntime(script, premium_requests=premium)
        return self.runtime

    def command_runner(self, contract, root, host):
        self.runner_args.append((contract, root, host))
        return self.runner


@pytest.fixture
def env(tmp_path, monkeypatch):
    backend = InMemoryBackend()
    monkeypatch.setattr(cli, "make_backend", lambda cfg: backend)
    code_graph = InMemoryCodeGraph()
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: code_graph)
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    run_state = _TaskRunSeams()
    monkeypatch.setattr(cli, "make_runtime", lambda cfg: run_state.runtime)
    monkeypatch.setattr(
        cli,
        "make_command_runner",
        lambda contract, root, host: run_state.command_runner(contract, root, host),
    )
    monkeypatch.setattr(cli, "make_github_writer", lambda: run_state.github)
    config = _write_config(tmp_path)

    def run(*args):
        return runner.invoke(cli.app, [*args, "--config", str(config)])

    run.backend = backend
    run.seams = run_state
    run.code_graph = code_graph
    run.store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    return run


def test_init_then_search_as_qa(env):
    first = env("init")
    assert first.exit_code == 0
    assert first.stdout.splitlines()[-1] == "seeded"
    result = env("memory", "search", "neighbours", "--as", "qa")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines
    assert {line.split("\t")[0] for line in lines} == {"acme__company"}
    assert all("neighbours" in line.split("\t")[1] for line in lines)


def test_init_creates_all_four_graphs(env):
    env("init")
    assert sorted(env.backend._graphs) == [
        "acme__company",
        "acme__product",
        "acme__qa",
        "acme__rnd",
    ]


def test_init_twice_says_already_seeded(env):
    env("init")
    second = env("init")
    assert second.exit_code == 0
    assert second.stdout.splitlines()[-1] == "already seeded"
    assert len(env.backend.items("acme__company")) == 18


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("x"),
        redis.exceptions.ConnectionError("x"),
        redis.exceptions.TimeoutError("x"),
    ],
)
def test_init_backend_down_exits_2_and_does_not_mark_seeded(
    tmp_path, monkeypatch, error
):
    monkeypatch.setattr(cli, "make_backend", lambda cfg: FailingBackend(error))
    config = _write_config(tmp_path)
    result = runner.invoke(cli.app, ["init", "--config", str(config)])
    assert result.exit_code == 2
    assert result.stdout.splitlines()[-1] == "cannot reach FalkorDB at localhost:6379"
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("seed_version") is None


def _raise(error):
    def factory(cfg):
        raise error

    return factory


class SearchFailingBackend(InMemoryBackend):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def search(self, graphs, query, limit):
        raise self._error


DOWN_ERRORS = [
    ConnectionError("x"),
    redis.exceptions.ConnectionError("x"),
    redis.exceptions.TimeoutError("x"),
]
LLM_ERROR = openai.OpenAIError("Missing credentials.\nSet OPENAI_API_KEY.")
LLM_LINE = "cannot create LLM client: Missing credentials. Set OPENAI_API_KEY."


@pytest.mark.parametrize("error", DOWN_ERRORS)
def test_init_backend_construction_down_exits_2(tmp_path, monkeypatch, error):
    monkeypatch.setattr(cli, "make_backend", _raise(error))
    config = _write_config(tmp_path)
    result = runner.invoke(cli.app, ["init", "--config", str(config)])
    assert result.exit_code == 2
    assert result.stdout.splitlines()[-1] == "cannot reach FalkorDB at localhost:6379"
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("seed_version") is None


@pytest.mark.parametrize("error", DOWN_ERRORS)
def test_search_backend_construction_down_exits_2(tmp_path, monkeypatch, error):
    monkeypatch.setattr(cli, "make_backend", _raise(error))
    config = _write_config(tmp_path)
    result = runner.invoke(
        cli.app, ["memory", "search", "x", "--as", "qa", "--config", str(config)]
    )
    assert result.exit_code == 2
    assert result.stdout.splitlines() == ["cannot reach FalkorDB at localhost:6379"]


@pytest.mark.parametrize("error", DOWN_ERRORS)
def test_search_backend_down_exits_2(tmp_path, monkeypatch, error):
    monkeypatch.setattr(cli, "make_backend", lambda cfg: SearchFailingBackend(error))
    config = _write_config(tmp_path)
    result = runner.invoke(
        cli.app, ["memory", "search", "x", "--as", "qa", "--config", str(config)]
    )
    assert result.exit_code == 2
    assert result.stdout.splitlines() == ["cannot reach FalkorDB at localhost:6379"]


def test_init_bad_config_exits_1(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("- not a mapping\n")
    result = runner.invoke(cli.app, ["init", "--config", str(bad)])
    assert result.exit_code == 1
    assert "must be a YAML mapping" in result.stdout


def test_init_prints_family_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_backend", lambda cfg: InMemoryBackend())
    config = _write_config(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["roles"]["qa"]["family"] = raw["roles"]["junior"]["family"]
    config.write_text(yaml.safe_dump(raw))
    result = runner.invoke(cli.app, ["init", "--config", str(config)])
    assert result.exit_code == 0
    assert any(
        line.startswith("qa is in the same family")
        for line in result.stdout.splitlines()
    )


def test_search_unknown_role_is_usage_error(env):
    result = env("memory", "search", "x", "--as", "wizard")
    assert result.exit_code == 2


def test_task_new_prints_id(env):
    result = env("task", "new", "Graph switch flickers")
    assert result.exit_code == 0
    task_id = result.stdout.strip()
    assert task_id.isdigit()
    task = env.store.get(int(task_id))
    assert (task.title, task.source, task.tenant) == (
        "Graph switch flickers",
        "cli",
        "acme",
    )


def test_init_llm_client_error_exits_1_and_does_not_mark_seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_backend", _raise(LLM_ERROR))
    config = _write_config(tmp_path)
    result = runner.invoke(cli.app, ["init", "--config", str(config)])
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [LLM_LINE]
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("seed_version") is None


def test_search_llm_client_error_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_backend", _raise(LLM_ERROR))
    config = _write_config(tmp_path)
    result = runner.invoke(
        cli.app, ["memory", "search", "x", "--as", "qa", "--config", str(config)]
    )
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [LLM_LINE]


def _git_repo(root, files):
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    for name, content in files.items():
        (root / name).write_text(content)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_index_cli(env, tmp_path):
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    r = env("index", "--repo", "o/r", "--repo-path", str(root))
    assert r.exit_code == 0
    assert r.stdout.splitlines() == [
        "indexed 1, unchanged 0, deleted 0, skipped 0, calls 0"
    ]
    r = env("index", "--repo", "o/r", "--repo-path", str(root))
    assert r.stdout.splitlines() == [
        "indexed 0, unchanged 1, deleted 0, skipped 0, calls 0"
    ]
    assert list(env.code_graph._repos) == [("acme__rnd", "o/r")]


def test_index_cli_not_a_repo_exits_1(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = env("index", "--repo", "o/r", "--repo-path", str(plain))
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [f"{plain} is not a git repository"]


def test_index_cli_bad_repo_name_exits_1(env, tmp_path):
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    r = env("index", "--repo", "nope", "--repo-path", str(root))
    assert r.exit_code == 1
    assert r.stdout.splitlines() == ["repo must look like owner/name, got 'nope'"]
    assert env.code_graph.upserts == 0


@pytest.mark.parametrize("error", DOWN_ERRORS)
def test_index_cli_falkordb_down_exits_2(tmp_path, monkeypatch, error):
    monkeypatch.setattr(cli, "make_code_graph", _raise(error))
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    config = _write_config(tmp_path)
    result = runner.invoke(
        cli.app,
        ["index", "--repo", "o/r", "--repo-path", str(root), "--config", str(config)],
    )
    assert result.exit_code == 2
    assert result.stdout.splitlines() == ["cannot reach FalkorDB at localhost:6379"]


class FailingGitHub:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def issues(self, repo, since=None):
        raise self._error


FALLBACK_LINE = (
    "{n} episodes linked against current code because their commits are not in the "
    "checkout; fetch PR heads for exact links (see README)"
)


class Boom(AssertionError):
    pass


def _never(cfg):
    raise Boom("a dry run must not build this")


def test_dry_run_needs_no_openai_key_and_builds_no_backend(env, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "make_backend", _never)
    monkeypatch.setattr(cli, "make_code_graph", _never)
    r = env("import", "--repo", "acme-org/web", "--dry-run")
    assert r.exception is None
    assert r.exit_code == 0
    assert r.stdout.splitlines()[0] == "issues: 2"
    assert env.backend._graphs == {}
    assert env.code_graph.upserts == 0


def test_dry_run_with_repo_path_builds_nothing_and_does_not_index(
    env, tmp_path, monkeypatch
):
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    monkeypatch.setattr(cli, "make_backend", _never)
    monkeypatch.setattr(cli, "make_code_graph", _never)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root), "--dry-run")
    assert r.exception is None
    assert r.exit_code == 0


def test_dry_run_reports_link_only_retries_separately(env, tmp_path):
    assert env("import", "--repo", "acme-org/web").exit_code == 0  # 101 stays added
    r = env("import", "--repo", "acme-org/web", "--dry-run")
    assert r.exit_code == 0
    assert r.stdout.splitlines()[-2:] == [
        "episodes to write: 0 (each runs one Graphiti LLM extraction)",
        "link-only retries: 1",
    ]


def test_real_import_ensures_the_code_schema_but_dry_run_does_not(env, monkeypatch):
    schemas = []

    async def record(graph):
        schemas.append(graph)

    monkeypatch.setattr(env.code_graph, "ensure_schema", record)
    assert env("import", "--repo", "acme-org/web", "--dry-run").exit_code == 0
    assert schemas == []
    assert env("import", "--repo", "acme-org/web").exit_code == 0
    assert schemas == ["acme__rnd"]


def test_import_dry_run_prints_counts_and_writes_nothing(env):
    env("init")
    r = env("import", "--repo", "acme-org/web", "--dry-run")
    assert r.exit_code == 0
    assert r.stdout.splitlines() == [
        "issues: 2",
        "features: 1",
        "pull requests: 1",
        "review comments: 2",
        "already imported: 0",
        "skipped: bot=3 not_team=1 orphan_reply=1 pr_author=1",
        "episodes to write: 6 (each runs one Graphiti LLM extraction)",
    ]
    assert env.backend.items("acme__rnd") == []
    assert env.backend.items("acme__product") == []
    assert env.store.get_meta("import:acme:acme-org/web:cursor") is None


def test_import_dry_run_with_nothing_skipped_prints_none(env, monkeypatch):
    empty = FakeGitHub([], [], [], {})
    monkeypatch.setattr(cli, "make_github", lambda: empty)
    r = env("import", "--repo", "acme-org/web", "--dry-run")
    assert r.stdout.splitlines() == [
        "issues: 0",
        "features: 0",
        "pull requests: 0",
        "review comments: 0",
        "already imported: 0",
        "skipped: none",
        "episodes to write: 0 (each runs one Graphiti LLM extraction)",
    ]


def test_import_then_rerun(env):
    env("init")
    first = env("import", "--repo", "acme-org/web")
    assert first.exit_code == 0
    assert first.stdout.splitlines() == [
        "1 review episodes await a code graph; import or index with --repo-path to link them",
        "imported 6 episodes (0 linked to code)",
    ]
    second = env("import", "--repo", "acme-org/web")
    assert second.stdout.splitlines()[-1] == "imported 0 episodes (0 linked to code)"
    assert len(env.backend.items("acme__rnd")) == 5
    assert len(env.backend.items("acme__product")) == 1


def test_import_limit_message(env):
    env("init")
    r = env("import", "--repo", "acme-org/web", "--limit", "2")
    assert r.exit_code == 0
    assert r.stdout.splitlines()[-1] == (
        "stopped at --limit 2: imported 2 of 6 episodes; run again to continue"
    )
    assert env.store.get_meta("import:acme:acme-org/web:cursor") is None


def test_import_creates_graphs_and_state_without_init(env):
    r = env("import", "--repo", "acme-org/web")
    assert r.exit_code == 0
    assert sorted(env.backend._graphs) == ["acme__product", "acme__rnd"]


def test_import_since_is_midnight_utc(env, monkeypatch):
    seen = []
    gh = FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    original = gh.issues
    monkeypatch.setattr(
        gh,
        "issues",
        lambda repo, since=None: seen.append(since) or original(repo, since),
    )
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    r = env("import", "--repo", "acme-org/web", "--since", "2026-01-02", "--dry-run")
    assert r.exit_code == 0
    assert seen == [datetime(2026, 1, 2, tzinfo=UTC)]


def test_import_with_repo_path_indexes_first_and_links(env, tmp_path):
    graph_py = (
        "class GraphView:\n"
        "    def load(self, g):\n"
        "        return g\n"
        "\n"
        "    def switch(self, g):\n"
        "        self.load(g)\n"
    )
    root = tmp_path / "web"
    (root / "src" / "app").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    (root / "src" / "app" / "graph.py").write_text(graph_py)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert r.exit_code == 0
    assert r.stdout.splitlines() == [
        "indexed 1, unchanged 0, deleted 0, skipped 0, calls 1",
        FALLBACK_LINE.format(n=1),
        "imported 6 episodes (1 linked to code)",
    ]
    assert len(env.code_graph.links("acme__rnd")) == 1


def test_import_rerun_after_failed_link_reports_relinked(env, tmp_path):
    graph_py = (
        "class GraphView:\n"
        "    def load(self, g):\n"
        "        return g\n"
        "\n"
        "    def switch(self, g):\n"
        "        self.load(g)\n"
    )
    root = tmp_path / "web"
    (root / "src" / "app").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    (root / "src" / "app" / "graph.py").write_text(graph_py)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    original = env.code_graph.link_episode
    calls = 0

    async def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("link down")
        return await original(*args, **kwargs)

    env.code_graph.link_episode = flaky
    first = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert first.exit_code == 2
    second = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert second.exit_code == 0
    assert second.stdout.splitlines()[-3:] == [
        "relinked 1 episodes",
        FALLBACK_LINE.format(n=1),
        "imported 3 episodes (1 linked to code)",
    ]


def test_import_dry_run_with_repo_path_does_not_index(env, tmp_path):
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root), "--dry-run")
    assert r.exit_code == 0
    assert r.stdout.splitlines()[0] == "issues: 2"
    assert env.code_graph.upserts == 0
    assert env.backend._graphs == {}


def _commit(root, message):
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            message,
        ],
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


GRAPH_V1 = (
    "class GraphView:\n"
    "    def load(self, g):\n"
    "        return g\n"
    "\n"
    "    def switch(self, g):\n"
    "        self.load(g)\n"
)
GRAPH_V2 = (
    "class GraphView:\n"
    "    def load(self, g):\n"
    "        return g\n"
    "\n"
    "    def render(self, g):\n"
    "        return g\n"
    "\n"
    "    def switch(self, g):\n"
    "        self.load(g)\n"
)


def _comments_made_on(gh, commit):
    gh._comments = [
        dataclasses.replace(c, original_commit_id=commit) for c in gh._comments
    ]


def _two_commit_repo(tmp_path):
    root = tmp_path / "web"
    (root / "src" / "app").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    graph = root / "src" / "app" / "graph.py"
    graph.write_text(GRAPH_V1)
    first = _commit(root, "v1")
    graph.write_text(GRAPH_V2)
    _commit(root, "v2")
    return root, first


def test_import_links_to_the_symbol_at_the_comments_own_commit(
    env, tmp_path, monkeypatch
):
    root, first = _two_commit_repo(tmp_path)
    gh = FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    _comments_made_on(gh, first)
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert r.exit_code == 0
    # line 6 is `render` in today's file but `switch` at the comment's commit
    assert r.stdout.splitlines() == [
        "indexed 1, unchanged 0, deleted 0, skipped 0, calls 1",
        "imported 6 episodes (1 linked to code)",
    ]
    ((_, path, qualname),) = env.code_graph.links("acme__rnd")
    assert (path, qualname) == ("src/app/graph.py", "GraphView.switch")


def test_import_falls_back_to_current_code_when_the_commit_is_missing(
    env, tmp_path, monkeypatch
):
    root, _ = _two_commit_repo(tmp_path)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert r.exit_code == 0
    assert r.stdout.splitlines()[1:] == [
        FALLBACK_LINE.format(n=1),
        "imported 6 episodes (1 linked to code)",
    ]
    ((_, path, qualname),) = env.code_graph.links("acme__rnd")
    assert (path, qualname) == ("src/app/graph.py", "GraphView.render")


def test_import_does_not_guess_for_an_outdated_comment_with_a_missing_commit(
    env, tmp_path, monkeypatch
):
    root, _ = _two_commit_repo(tmp_path)
    gh = FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    gh._comments = [dataclasses.replace(c, line=None) for c in gh._comments]
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert r.exit_code == 0
    assert r.stdout.splitlines()[1:] == ["imported 6 episodes (0 linked to code)"]
    assert env.code_graph.links("acme__rnd") == set()


def test_reviews_imported_without_code_are_linked_by_a_later_repo_path_run(
    env, tmp_path
):
    first = env("import", "--repo", "acme-org/web")
    assert first.exit_code == 0
    assert first.stdout.splitlines()[0] == (
        "1 review episodes await a code graph; "
        "import or index with --repo-path to link them"
    )
    assert env.store.get_meta("import:acme:acme-org/web:cursor") is None
    root, _ = _two_commit_repo(tmp_path)
    before = len(env.backend.items("acme__rnd"))
    second = env("import", "--repo", "acme-org/web", "--repo-path", str(root))
    assert second.exit_code == 0
    assert second.stdout.splitlines() == [
        "indexed 1, unchanged 0, deleted 0, skipped 0, calls 1",
        "relinked 1 episodes",
        FALLBACK_LINE.format(n=1),
        "imported 0 episodes (1 linked to code)",
    ]
    assert len(env.backend.items("acme__rnd")) == before
    assert env.store.get_meta("import:acme:acme-org/web:cursor") is not None


def test_import_repo_path_subdirectory_uses_the_repo_root(env, tmp_path):
    root, _ = _two_commit_repo(tmp_path)
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(root / "src"))
    assert r.exit_code == 0
    assert r.stdout.splitlines()[0] == (
        "indexed 1, unchanged 0, deleted 0, skipped 0, calls 1"
    )
    assert sorted(path for _, path, _ in env.code_graph.links("acme__rnd")) == [
        "src/app/graph.py"
    ]


def test_import_dry_run_repo_path_not_a_repo_exits_1_and_writes_nothing(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(plain), "--dry-run")
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [f"{plain} is not a git repository"]


def test_import_repo_path_not_a_repo_exits_1(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = env("import", "--repo", "acme-org/web", "--repo-path", str(plain))
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [f"{plain} is not a git repository"]
    assert env.backend._graphs == {}


def test_import_github_error_exits_3_and_writes_nothing(env, monkeypatch):
    error = GitHubError("gh api failed: HTTP 404: Not Found")
    monkeypatch.setattr(cli, "make_github", lambda: FailingGitHub(error))
    r = env("import", "--repo", "acme-org/missing")
    assert r.exit_code == 3
    assert r.stdout.splitlines()[-1] == "gh api failed: HTTP 404: Not Found"
    assert env.backend.items("acme__rnd") == []
    assert env.store.get_meta("import:acme:acme-org/missing:cursor") is None


def test_import_github_missing_at_construction_exits_3(env, monkeypatch):

    def missing_gh():
        raise GitHubError("gh CLI not found")

    monkeypatch.setattr(cli, "make_github", missing_gh)
    r = env("import", "--repo", "acme-org/web")
    assert r.exit_code == 3
    assert r.stdout.splitlines() == ["gh CLI not found"]


@pytest.mark.parametrize("error", DOWN_ERRORS)
def test_import_falkordb_down_exits_2_and_keeps_cursor_unset(
    tmp_path, monkeypatch, error
):
    monkeypatch.setattr(cli, "make_backend", lambda cfg: FailingBackend(error))
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: InMemoryCodeGraph())
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    config = _write_config(tmp_path)
    r = runner.invoke(
        cli.app, ["import", "--repo", "acme-org/web", "--config", str(config)]
    )
    assert r.exit_code == 2
    assert r.stdout.splitlines() == ["cannot reach FalkorDB at localhost:6379"]
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("import:acme:acme-org/web:cursor") is None


def test_import_llm_client_error_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_backend", _raise(LLM_ERROR))
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: InMemoryCodeGraph())
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    config = _write_config(tmp_path)
    r = runner.invoke(
        cli.app, ["import", "--repo", "acme-org/web", "--config", str(config)]
    )
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [LLM_LINE]


def _openai_auth_error() -> openai.AuthenticationError:
    response = httpx.Response(401, request=httpx.Request("POST", "https://x/v1"))
    return openai.AuthenticationError(
        "Incorrect API key.\nCheck it.", response=response, body=None
    )


@pytest.mark.parametrize(
    "error",
    [
        _openai_auth_error(),
        openai.APIConnectionError(request=httpx.Request("POST", "https://x/v1")),
    ],
)
def test_import_openai_error_mid_run_exits_1_and_keeps_cursor_unset(
    tmp_path, monkeypatch, error
):
    class AddFailing(InMemoryBackend):
        async def add(self, graph, scope, item):
            raise error

    monkeypatch.setattr(cli, "make_backend", lambda cfg: AddFailing())
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: InMemoryCodeGraph())
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    config = _write_config(tmp_path)
    r = runner.invoke(
        cli.app, ["import", "--repo", "acme-org/web", "--config", str(config)]
    )
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [
        f"LLM request failed: {' '.join(str(error).split())}"
    ]
    assert isinstance(r.exception, SystemExit)
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("import:acme:acme-org/web:cursor") is None


def test_import_cursor_not_advanced_when_a_write_fails(tmp_path, monkeypatch):
    class AddFailing(InMemoryBackend):
        async def add(self, graph, scope, item):
            raise ConnectionError("x")

    monkeypatch.setattr(cli, "make_backend", lambda cfg: AddFailing())
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: InMemoryCodeGraph())
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    config = _write_config(tmp_path)
    r = runner.invoke(
        cli.app, ["import", "--repo", "acme-org/web", "--config", str(config)]
    )
    assert r.exit_code == 2
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("import:acme:acme-org/web:cursor") is None


def test_import_cursor_is_captured_before_fetching(env, monkeypatch):
    start = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    ticks = iter(start.replace(minute=m) for m in range(60))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    class ClockedGitHub(FakeGitHub):
        def issues(self, repo, since=None):
            next(ticks)  # time passes while the run fetches and writes
            return super().issues(repo, since)

    gh = ClockedGitHub.from_fixtures(GITHUB_FIXTURES)
    # no linkable review, so none waits for a code graph and the cursor may move
    gh._comments = [dataclasses.replace(c, subject_type="file") for c in gh._comments]
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    monkeypatch.setattr(cli, "datetime", Clock)
    assert env("import", "--repo", "acme-org/web").exit_code == 0
    assert env.store.get_meta("import:acme:acme-org/web:cursor") == start.isoformat()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--repo", "nope"], "repo must look like owner/name, got 'nope'"),
        (
            ["--repo", "o/r", "--since", "yesterday"],
            "--since must be a date like 2026-01-31, got 'yesterday'",
        ),
        (["--repo", "o/r", "--limit", "0"], "--limit must be at least 1, got 0"),
        (["--repo", "o/r", "--limit", "-3"], "--limit must be at least 1, got -3"),
        (["--repo", "o/r", "--limit", "abc"], "--limit must be an integer, got 'abc'"),
    ],
)
def test_import_bad_arguments_exit_1(env, args, message):
    r = env("import", *args)
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [message]
    assert env.backend.items("acme__rnd") == []


def _import_with(tmp_path, monkeypatch, backend=None, github=None):
    monkeypatch.setattr(cli, "make_backend", lambda cfg: backend or InMemoryBackend())
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: InMemoryCodeGraph())
    monkeypatch.setattr(
        cli,
        "make_github",
        lambda: github or FakeGitHub.from_fixtures(GITHUB_FIXTURES),
    )
    config = _write_config(tmp_path)
    return runner.invoke(
        cli.app, ["import", "--repo", "acme-org/web", "--config", str(config)]
    )


def test_import_falkordb_response_error_exits_2(tmp_path, monkeypatch):
    error = redis.exceptions.ResponseError("Invalid graph operation\non empty key")
    r = _import_with(tmp_path, monkeypatch, backend=FailingBackend(error))
    assert r.exit_code == 2
    assert r.stdout.splitlines() == [
        "FalkorDB error: Invalid graph operation on empty key"
    ]
    assert isinstance(r.exception, SystemExit)


def test_init_falkordb_response_error_exits_2(tmp_path, monkeypatch):
    error = redis.exceptions.ResponseError("bad query")
    monkeypatch.setattr(cli, "make_backend", lambda cfg: FailingBackend(error))
    r = runner.invoke(cli.app, ["init", "--config", str(_write_config(tmp_path))])
    assert r.exit_code == 2
    assert r.stdout.splitlines()[-1] == "FalkorDB error: bad query"


def test_index_falkordb_response_error_exits_2(tmp_path, monkeypatch):
    class Failing(InMemoryCodeGraph):
        async def ensure_schema(self, graph):
            raise redis.exceptions.ResponseError("bad index")

    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: Failing())
    root = _git_repo(tmp_path / "repo", {"a.py": "def f(): pass\n"})
    config = _write_config(tmp_path)
    r = runner.invoke(
        cli.app,
        ["index", "--repo", "o/r", "--repo-path", str(root), "--config", str(config)],
    )
    assert r.exit_code == 2
    assert r.stdout.splitlines() == ["FalkorDB error: bad index"]


GRAPHITI_ERRORS = [
    RateLimitError("Rate limit exceeded.\nPlease retry"),
    RefusalError("The model refused"),
    EmptyResponseError("empty"),
]


@pytest.mark.parametrize("error", GRAPHITI_ERRORS, ids=lambda e: type(e).__name__)
def test_import_graphiti_llm_errors_exit_1_and_keep_cursor_unset(
    tmp_path, monkeypatch, error
):
    class AddFailing(InMemoryBackend):
        async def add(self, graph, scope, item):
            raise error

    r = _import_with(tmp_path, monkeypatch, backend=AddFailing())
    assert r.exit_code == 1
    assert r.stdout.splitlines() == [
        f"LLM request failed: {' '.join(str(error).split())}"
    ]
    assert isinstance(r.exception, SystemExit)
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("import:acme:acme-org/web:cursor") is None


def _gh_runner(**overrides):
    def run(argv):
        endpoint = argv[-1]
        for name, text in overrides.items():
            if name in endpoint:
                return text
        for name in ("issues", "pulls/comments", "pulls"):
            if f"/{name}?" in endpoint:
                return FIXTURE_TEXT[
                    "pull_comments.json" if name == "pulls/comments" else f"{name}.json"
                ]
        raise AssertionError(endpoint)

    return run


FIXTURE_TEXT = {p.name: p.read_text() for p in GITHUB_FIXTURES.glob("*.json")}


@pytest.mark.parametrize(
    ("overrides", "line"),
    [
        (
            {"/issues?": '[[{"title": "no number"}]]'},
            "malformed GitHub issue: KeyError: 'number'",
        ),
        (
            {"/issues?": '{"message": "oops"}'},
            "gh api returned unexpected JSON: expected a list of pages",
        ),
    ],
)
def test_import_malformed_gh_json_exits_3_with_one_line(
    tmp_path, monkeypatch, overrides, line
):
    github = cli_github(_gh_runner(**overrides))
    r = _import_with(tmp_path, monkeypatch, github=github)
    assert r.exit_code == 3
    assert r.stdout.splitlines() == [line]
    assert isinstance(r.exception, SystemExit)
    store = TaskStore(f"sqlite:///{tmp_path}/state.db")
    assert store.get_meta("import:acme:acme-org/web:cursor") is None


def cli_github(run):
    return GhCli(runner=run)


CONTRACT = {
    "build": "build",
    "test": "pytest-all",
    "e2e": None,
    "lint": "lint",
    "sandbox": {"devcontainer": ".devcontainer"},
    "test_files": "pytest {files}",
}


@pytest.fixture
def repo(env, make_repo):
    """A contract repo whose area is already known to the `rnd` graph."""
    asyncio.run(
        env.backend.add(
            "acme__rnd",
            Scope.RND,
            MemoryItem("calc", SEED_FACT, "test", datetime.now(UTC)),
        )
    )
    return make_repo({**FILES, ".agentco.yml": yaml.safe_dump(CONTRACT)})


def _run_task(env, repo, *args):
    return env("task", "run", *args, "--repo", "acme/app", "--repo-path", str(repo))


def _new_task(env, title="Make add commutative-safe") -> int:
    return int(env("task", "new", title).stdout)


NONE_LESSON = json.dumps({"lessons": [{"kind": "none", "text": ""}]})


def _over_budget(env):
    """PM and triage cost more than the whole budget, so the task escalates."""
    env.seams.script(
        {
            Role.PM: [PM_JSON, PM_JSON],
            Role.TRIAGE: [triage_json(), triage_json()],
            Role.LIBRARIAN: [NONE_LESSON] * 2,
        },
        premium=100,
    )


def _calls(env) -> list[Role]:
    return [c.role for c in env.seams.runtime.calls]


def _decisions(env, task_id):
    return [a.body for a in env.store.artifacts(task_id, ArtifactKind.HUMAN_DECISION)]


def test_task_new_body_is_stored_as_the_request(env):
    task_id = _new_task(env, "Fix add")
    assert env.store.latest_artifact(task_id, ArtifactKind.REQUEST).body == {
        "body": "Fix add"
    }
    result = env("task", "new", "Fix mul", "--body", "mul drops the sign")
    request = env.store.latest_artifact(int(result.stdout), ArtifactKind.REQUEST)
    assert (request.role, request.body) == (Role.HUMAN, {"body": "mul drops the sign"})


def test_task_run_happy_path_prints_pr_open(env, repo):
    env.seams.script(happy())
    task_id = _new_task(env)

    result = _run_task(env, repo, str(task_id))

    assert (result.exit_code, result.stdout) == (
        0,
        "task 1: pr_open https://github.com/acme/app/pull/1\n",
    )
    assert env.store.get(task_id).state is TaskState.PR_OPEN
    assert [(c["repo"], c["base"]) for c in env.seams.github.created] == [
        ("acme/app", "main")
    ]
    assert env.store.latest_artifact(task_id, ArtifactKind.TARGET).body == {
        "repo": "acme/app",
        "main": "main",
    }
    contract, root, host = env.seams.runner_args[0]
    assert (contract.test, root, host) == ("pytest-all", repo.resolve(), False)


def test_task_run_host_flag_reaches_the_command_runner(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id), "--host")
    assert [host for _, _, host in env.seams.runner_args] == [True]


def test_task_run_is_resumable_and_records_the_target_once(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    first = _run_task(env, repo, str(task_id))
    second = _run_task(env, repo, str(task_id))
    assert (first.exit_code, first.stdout) == (0, "task 1: awaiting_human\n")
    assert (second.exit_code, second.stdout) == (0, "task 1: awaiting_human\n")
    assert _calls(env) == [Role.PM, Role.TRIAGE]
    assert len(env.store.artifacts(task_id, ArtifactKind.TARGET)) == 1


def test_task_run_step_failed_exit_4(env, repo):
    env.seams.script({Role.PM: [STUCK]})
    task_id = _new_task(env)

    result = _run_task(env, repo, str(task_id))

    assert result.exit_code == 4
    assert result.stdout == "task 1: failed: pm: SECRET_REASON_XYZ\n"
    assert env.store.get(task_id).state is TaskState.PM


def test_task_run_github_unavailable_exit_3(env, repo, monkeypatch):
    def missing():
        raise GitHubError("gh not found")

    monkeypatch.setattr(cli, "make_github_writer", missing)
    task_id = _new_task(env)
    result = _run_task(env, repo, str(task_id))
    assert (result.exit_code, result.stdout) == (3, "gh not found\n")
    assert env.store.get(task_id).state is TaskState.INTAKE


def test_task_run_falkordb_down_exit_2(env, repo, monkeypatch):
    def down(cfg):
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(cli, "make_backend", down)
    task_id = _new_task(env)
    result = _run_task(env, repo, str(task_id))
    assert (result.exit_code, result.stdout) == (
        2,
        "cannot reach FalkorDB at localhost:6379\n",
    )


def test_task_run_falkordb_dying_mid_run_exit_2(env, repo, monkeypatch):
    class Dying(InMemoryBackend):
        async def search(self, graphs, query, limit):
            raise redis.exceptions.ConnectionError("gone")

    monkeypatch.setattr(cli, "make_backend", lambda cfg: Dying())
    env.seams.script({Role.PM: [PM_JSON], Role.TRIAGE: [triage_json()]})
    task_id = _new_task(env)
    result = _run_task(env, repo, str(task_id))
    assert (result.exit_code, result.stdout) == (
        2,
        "cannot reach FalkorDB at localhost:6379\n",
    )


class EnterExitRuntime(FakeRuntime):
    def __init__(self, script):
        super().__init__(script)
        self.events = []

    async def __aenter__(self):
        self.events.append("start")
        return self

    async def __aexit__(self, *exc):
        self.events.append("stop")


def test_task_run_starts_and_stops_a_runtime_that_supports_it(env, repo):
    runtime = EnterExitRuntime({Role.PM: [STUCK]})
    env.seams.runtime = runtime
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    assert runtime.events == ["start", "stop"]
    assert [c.role for c in runtime.calls] == [Role.PM]


def test_task_run_all_runs_only_tasks_that_can_progress(env, repo):
    _over_budget(env)
    first = _new_task(env, "First")
    _run_task(env, repo, str(first))
    second = _new_task(env, "Second")
    cancelled = _new_task(env, "Cancelled")
    env.store.transition(cancelled, TaskState.CANCELLED, Role.HUMAN, "x")

    result = _run_task(env, repo, "--all")

    assert (result.exit_code, result.stdout) == (0, "task 2: awaiting_human\n")
    assert _calls(env) == [Role.PM, Role.TRIAGE, Role.PM, Role.TRIAGE]
    assert env.store.get(first).state is TaskState.AWAITING_HUMAN
    assert env.store.get(second).state is TaskState.AWAITING_HUMAN


def test_task_run_all_runs_every_task_and_reports_each(env, repo):
    _over_budget(env)
    _new_task(env, "First")
    _new_task(env, "Second")
    result = _run_task(env, repo, "--all")
    assert (result.exit_code, result.stdout) == (
        0,
        "task 1: awaiting_human\ntask 2: awaiting_human\n",
    )


def test_task_run_all_with_nothing_to_run(env, repo):
    result = _run_task(env, repo, "--all")
    assert (result.exit_code, result.stdout) == (0, "no tasks to run\n")


def test_task_run_all_exits_4_when_any_task_fails_but_runs_the_rest(env, repo):
    env.seams.script(
        {Role.PM: [STUCK, PM_JSON], Role.TRIAGE: [triage_json()]}, premium=100
    )
    _new_task(env, "Broken")
    _new_task(env, "Fine")
    result = _run_task(env, repo, "--all")
    assert result.exit_code == 4
    assert result.stdout == (
        "task 1: failed: pm: SECRET_REASON_XYZ\ntask 2: awaiting_human\n"
    )


def test_task_run_needs_exactly_one_of_id_or_all(env, repo):
    task_id = _new_task(env)
    for args in ([], [str(task_id), "--all"]):
        result = _run_task(env, repo, *args)
        assert (result.exit_code, result.stdout) == (
            1,
            "give exactly one of a task id or --all\n",
        )


def test_task_run_unknown_task_exit_1(env, repo):
    result = _run_task(env, repo, "99")
    assert (result.exit_code, result.stdout) == (1, "task 99 not found\n")


def test_task_run_checkout_without_a_repo_contract_exit_1(env, make_repo):
    bare = make_repo(FILES, name="nocontract")
    task_id = _new_task(env)
    result = _run_task(env, bare, str(task_id))
    assert result.exit_code == 1
    assert result.stdout == f"repo contract {bare.resolve()}/.agentco.yml not found\n"


def test_task_run_bad_repo_name_exit_1(env, repo):
    task_id = _new_task(env)
    result = env(
        "task", "run", str(task_id), "--repo", "nope", "--repo-path", str(repo)
    )
    assert (result.exit_code, result.stdout) == (
        1,
        "repo must look like owner/name, got 'nope'\n",
    )


def test_inbox_lists_escalations(env, repo):
    _over_budget(env)
    waiting = _new_task(env, "Fix add")
    _run_task(env, repo, str(waiting))
    _new_task(env, "Not escalated yet")

    result = env("inbox")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "id\ttitle\tcategory\tfrom_state\treason\toptions\tcost",
        "1\tFix add\tstuck\tengineering\tbudget\tadd budget; reject\t$8.00",
    ]


def test_inbox_when_empty(env):
    result = env("inbox")
    assert (result.exit_code, result.stdout) == (0, "nothing is waiting for a human\n")


def test_decide_requires_awaiting_human(env):
    task_id = _new_task(env)
    result = env("decide", str(task_id), "approve")
    assert (result.exit_code, result.stdout) == (
        1,
        "task 1 is intake, not awaiting_human\n",
    )
    assert _decisions(env, task_id) == []
    assert env.backend._graphs == {}


def test_decide_unknown_task_exit_1(env):
    result = env("decide", "99", "approve")
    assert (result.exit_code, result.stdout) == (1, "task 99 not found\n")


def test_decide_answer_requires_note(env):
    task_id = _new_task(env)
    for args in (["answer"], ["answer", "--note", "  "]):
        result = env("decide", str(task_id), *args)
        assert (result.exit_code, result.stdout) == (
            1,
            "answer needs --note with your answer\n",
        )


def test_decide_rejects_a_bad_budget(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    result = env("decide", str(task_id), "approve", "--add-budget", "0")
    assert (result.exit_code, result.stdout) == (
        1,
        "--add-budget must be a positive amount, got 0.0\n",
    )
    assert _decisions(env, task_id) == []


def test_decide_writes_company_memory_as_human(env, repo):
    _over_budget(env)
    task_id = _new_task(env, "Fix add")
    _run_task(env, repo, str(task_id))

    result = env("decide", str(task_id), "answer", "--note", "ship it small")

    assert result.exit_code == 0
    assert result.stdout == "task 1: answer recorded; run `agentco task run 1`\n"
    assert _decisions(env, task_id) == [{"decision": "answer", "note": "ship it small"}]
    [item] = env.backend.items("acme__company")
    assert item.name == "human-decision:1:1"
    assert item.body == (
        "Human decision (answer) on task 1 'Fix add': the team escalated from "
        "engineering (stuck) because: budget. Note: ship it small"
    )
    assert env.store.get(task_id).state is TaskState.AWAITING_HUMAN


def test_decide_with_falkordb_down_changes_nothing(env, repo, monkeypatch):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))

    def down(cfg):
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(cli, "make_backend", down)
    result = env("decide", str(task_id), "reject")

    assert (result.exit_code, result.stdout) == (
        2,
        "cannot reach FalkorDB at localhost:6379\n",
    )
    assert _decisions(env, task_id) == []
    assert env.store.get(task_id).state is TaskState.AWAITING_HUMAN


def test_decide_reject_cancels(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))

    result = env("decide", str(task_id), "reject", "--note", "not now")

    assert (result.exit_code, result.stdout) == (0, "task 1: cancelled\n")
    assert env.store.get(task_id).state is TaskState.CANCELLED
    assert _decisions(env, task_id) == [{"decision": "reject", "note": "not now"}]
    assert env.store.get_meta("task:1:extra_budget") is None
    again = _run_task(env, repo, str(task_id))
    assert (again.exit_code, again.stdout) == (0, "task 1: cancelled\n")
    assert env.seams.github.created == []


def test_decide_add_budget_then_run_resumes(env, repo):
    script = happy()
    script[Role.PM] = [PM_JSON]
    script[Role.TRIAGE] = [triage_json()]
    script[Role.LIBRARIAN] = [NONE_LESSON]
    env.seams.script(script, premium=100)
    task_id = _new_task(env)
    assert _run_task(env, repo, str(task_id)).stdout == "task 1: awaiting_human\n"

    result = env("decide", str(task_id), "approve", "--add-budget", "100")

    assert result.exit_code == 0
    assert env.store.get_meta("task:1:extra_budget") == "100.0"
    assert env.store.get(task_id).state is TaskState.AWAITING_HUMAN
    resumed = _run_task(env, repo, "--all")
    assert (resumed.exit_code, resumed.stdout) == (
        0,
        "task 1: pr_open https://github.com/acme/app/pull/1\n",
    )
    assert len(env.seams.github.created) == 1
    assert _calls(env).count(Role.PM) == 1


def test_decide_add_budget_accumulates(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    env("decide", str(task_id), "approve", "--add-budget", "1.5")
    env("decide", str(task_id), "approve", "--add-budget", "2")
    assert env.store.get_meta("task:1:extra_budget") == "3.5"
    assert [d["decision"] for d in _decisions(env, task_id)] == ["approve", "approve"]
    assert [i.name for i in env.backend.items("acme__company")] == [
        "human-decision:1:1",
        "human-decision:1:2",
    ]


@pytest.mark.parametrize(
    "error",
    [GitError("fetch failed: offline"), RestoreError("could not restore")],
    ids=["git", "restore"],
)
def test_task_run_git_failure_is_a_failed_line_with_exit_3(
    env, repo, monkeypatch, error
):
    def broken(*args):
        raise error

    monkeypatch.setattr("agentco.workflow.orchestrator.create_worktree", broken)
    env.seams.script({Role.PM: [PM_JSON], Role.TRIAGE: [triage_json()]}, premium=0.01)
    task_id = _new_task(env)

    result = _run_task(env, repo, str(task_id))

    assert result.exit_code == 3
    assert result.stdout == f"task 1: failed: {error}\n"
    assert isinstance(result.exception, SystemExit)
    assert env.store.get(task_id).state is TaskState.ENGINEERING


def test_task_run_all_keeps_other_tasks_lines_when_git_fails(env, repo, monkeypatch):
    def broken(root, task_id, base):
        raise GitError("worktree add failed")

    monkeypatch.setattr("agentco.workflow.orchestrator.create_worktree", broken)
    env.seams.script(
        {Role.PM: [PM_JSON, STUCK], Role.TRIAGE: [triage_json()]}, premium=0.01
    )
    _new_task(env, "Broken git")
    _new_task(env, "Broken pm")

    result = _run_task(env, repo, "--all")

    assert result.exit_code == 3
    assert result.stdout == (
        "task 1: failed: worktree add failed\ntask 2: failed: pm: SECRET_REASON_XYZ\n"
    )


def _intake_gh(env, monkeypatch, **kw) -> FakeGitHub:
    gh = FakeGitHub([], [], [], {}, **kw)
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    return gh


def _pr_task(env, gh, number=7, repo="acme/app") -> int:
    store = env.store
    store.create_all()
    task = store.create_task("acme", "fix tabs", "issue", "5")
    store.put_artifact(task.id, ArtifactKind.TARGET, Role.PM, {"repo": repo})
    for state in (
        TaskState.PM,
        TaskState.TRIAGE,
        TaskState.ENGINEERING,
        TaskState.PREFLIGHT,
        TaskState.INTERNAL_REVIEW,
        TaskState.LEAD_DECISION,
        TaskState.PR_OPEN,
    ):
        store.transition(task.id, state, Role.LEAD, "test")
    url = f"https://github.com/{repo}/pull/{number}"
    store.put_artifact(
        task.id, ArtifactKind.PR, Role.LEAD, {"number": number, "url": url}
    )
    now = datetime.now(UTC)
    gh.upsert_pull(
        GhPull(
            number=number,
            title="t",
            body="b",
            author=GhUser("agentco", False),
            state="open",
            merged_at=None,
            created_at=now,
            updated_at=now,
            base_sha="base",
            head_sha="sha1",
            labels=(),
            url=url,
            closes_issues=(),
        )
    )
    return task.id


def _comment(id, number=7, body="crash on empty list") -> GhReviewComment:
    at = datetime.now(UTC) + timedelta(hours=1)
    return GhReviewComment(
        id=id,
        pr_number=number,
        author=GhUser("alice", False),
        association="MEMBER",
        body=body,
        path="a.py",
        line=3,
        original_line=3,
        side="RIGHT",
        subject_type="line",
        commit_id="sha1",
        original_commit_id="sha1",
        diff_hunk="",
        in_reply_to=None,
        created_at=at,
        updated_at=at,
        url=f"https://github.com/acme/app/pull/{number}#discussion_r{id}",
    )


def _lead_says(*classes) -> str:
    return json.dumps(
        {
            "confidence": 0.9,
            "classifications": [
                {"finding_id": i, "category": c, "note": "n"} for i, c in classes
            ],
            "summary": "decided",
        }
    )


def _intake(env, *args):
    return env("intake", "--repo", "acme/app", *args)


def test_intake_reenters_a_real_bug_and_prints_it(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    gh.add_comment(_comment(1))
    env.seams.script({Role.LEAD: [_lead_says(("c1", "real_bug"))]})

    result = _intake(env)

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        f"#{task_id} back to triage (round 1): 1 finding(s)",
        "0 known, 0 deferred",
    ]
    assert env.store.get(task_id).state is TaskState.TRIAGE
    assert gh.created == [] and gh.edited == []


def test_intake_twice_is_idempotent(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    gh.add_comment(_comment(1))
    env.seams.script({Role.LEAD: [_lead_says(("c1", "real_bug"))]})
    _intake(env)
    lead_calls = len([c for c in env.seams.runtime.calls if c.role is Role.LEAD])

    second = _intake(env)

    assert (second.exit_code, second.stdout) == (0, "0 known, 0 deferred\n")
    assert env.store.get(task_id).round == 1
    assert len(env.seams.runtime.calls) == lead_calls


def test_intake_rejects_a_bad_repo_exit_1(env):
    result = env("intake", "--repo", "not a repo")
    assert result.exit_code == 1
    assert result.stdout == "repo must look like owner/name, got 'not a repo'\n"


def test_intake_github_failure_exit_3(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch, fail_reads=True)
    task_id = _pr_task(env, gh)
    result = _intake(env)
    assert result.exit_code == 3
    assert "gh read failed" in result.stdout
    assert env.store.get(task_id).state is TaskState.PR_OPEN


def test_intake_agent_failure_exit_4(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    gh.add_comment(_comment(1))
    env.seams.script({Role.LEAD: [STUCK]})

    result = _intake(env)

    assert result.exit_code == 4
    assert result.stdout.splitlines()[-1].startswith("error: ")
    assert env.store.get(task_id).state is TaskState.PR_OPEN


def test_intake_prints_merged_and_closed(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    merged = _pr_task(env, gh, number=7)
    closed = _pr_task(env, gh, number=8)
    for n, at in ((7, datetime.now(UTC)), (8, None)):
        pull = gh.get_pull("acme/app", n)
        gh.upsert_pull(dataclasses.replace(pull, state="closed", merged_at=at))

    result = _intake(env)

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        f"#{merged} merged",
        f"#{closed} closed",
        "0 known, 0 deferred",
    ]


def test_intake_prints_a_task_that_needs_a_human(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    gh.add_comment(_comment(1, body="rename this var"))
    env.seams.script({Role.LEAD: [_lead_says(("c1", "tradeoff"))]})

    result = _intake(env)

    assert result.exit_code == 0, result.stdout
    assert f"#{task_id} needs a human: " in result.stdout
    assert env.store.get(task_id).state is TaskState.AWAITING_HUMAN
    assert env.store.latest_artifact(task_id, ArtifactKind.ESCALATION).body.get(
        "open_pr"
    ) in (None, False)


def test_intake_falkordb_down_exit_2(env, monkeypatch):
    def down(cfg):
        raise redis.exceptions.ConnectionError("refused")

    gh = _intake_gh(env, monkeypatch)
    _pr_task(env, gh)
    gh.add_comment(_comment(1))
    monkeypatch.setattr(cli, "make_backend", down)
    result = _intake(env)
    assert (result.exit_code, result.stdout) == (
        2,
        "cannot reach FalkorDB at localhost:6379\n",
    )


def test_fake_github_fail_reads_blocks_every_read():
    gh = FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    gh.fail_reads = True
    reads = [
        lambda: gh.issues("acme/app"),
        lambda: gh.pulls("acme/app"),
        lambda: gh.review_comments("acme/app"),
        lambda: gh.pull_files("acme/app", 1),
        lambda: gh.get_pull("acme/app", 1),
        lambda: gh.pull_reviews("acme/app", 1),
        lambda: gh.check_runs("acme/app", "sha"),
    ]
    for read in reads:
        with pytest.raises(GitHubError, match="simulated failure"):
            read()


# -- the Librarian after task run, decide and intake --------------------------------

_FINDING = {
    "id": "f1",
    "severity": "major",
    "kind": "bug",
    "path": "src/calc.py",
    "title": "Operand order lost",
    "detail": "swapping breaks subtraction callers",
}
REVIEW_FINDING = json.dumps({"findings": [_FINDING], "summary": "one bug"})
LEAD_SEND_BACK = json.dumps(
    {
        "confidence": 0.9,
        "classifications": [{"finding_id": "f1", "category": "real_bug", "note": "x"}],
        "summary": "fix it",
        "comments": ["Keep add free of side effects"],
    }
)


def _send_back_once() -> dict:
    script = happy()
    script[Role.TRIAGE] = [triage_json()] * 2
    script[Role.JUNIOR] = [engineer(1), engineer(2)]
    script[Role.REVIEWER] = [RULES_OK, REVIEW_FINDING, RULES_OK, REVIEW_OK, RULES_OK]
    script[Role.QA] = [QA_OK, QA_OK]
    script[Role.LEAD] = [LEAD_SEND_BACK, LEAD_OK]
    return script


def _lessons(env, graph="acme__rnd") -> list[str]:
    return [i.body for i in env.backend.items(graph)]


def test_task_run_then_learn_rerun_adds_nothing(env, repo):
    env.seams.script(_send_back_once())
    task_id = _new_task(env)

    ran = _run_task(env, repo, str(task_id))

    assert (ran.exit_code, ran.stdout.count("warning")) == (0, 0), ran.stdout
    learned = _lessons(env)
    assert any("Keep add free of side effects" in body for body in learned)
    assert f"task:{task_id}:lead:" in " ".join(learned)
    assert env.store.get_meta("librarian:acme:closed:1") is None

    again = env("learn", str(task_id))

    assert (again.exit_code, again.stdout) == (0, "#1: 0 added, 0 duplicate\n")
    assert _lessons(env) == learned
    assert env("learn", "--all").stdout == "nothing to learn\n"


def test_learn_all_runs_every_task_that_needs_it(env, repo):
    env.seams.script({Role.PM: [PM_JSON]})
    waiting = _new_task(env)
    done = _pr_task(env, env.seams.github)
    env.store.put_artifact(
        done,
        ArtifactKind.LEAD,
        Role.LEAD,
        {"comments": ["Never mutate the input list"], "outcome": "approve"},
    )

    result = env("learn", "--all")

    assert (result.exit_code, result.stdout) == (0, f"#{done}: 1 added, 0 duplicate\n")
    assert waiting != done
    assert env("learn", "--all").stdout == "nothing to learn\n"


def test_learn_needs_exactly_one_of_id_or_all(env):
    both = env("learn", "1", "--all")
    neither = env("learn")
    assert (both.exit_code, neither.exit_code) == (1, 1)
    assert "exactly one" in both.stdout and "exactly one" in neither.stdout


def test_learn_unknown_task_exit_1(env):
    result = env("learn", "99")
    assert (result.exit_code, result.stdout) == (1, "task 99 not found\n")


def test_learn_falkordb_down_exit_2(env, monkeypatch):
    task_id = _new_task(env)
    env.store.transition(task_id, TaskState.CANCELLED, Role.HUMAN, "test")

    def down(cfg):
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(cli, "make_backend", down)
    result = env("learn", str(task_id))
    assert (result.exit_code, result.stdout) == (
        2,
        "cannot reach FalkorDB at localhost:6379\n",
    )


def test_decide_reject_runs_close_out(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    env.seams.script({Role.LIBRARIAN: [NONE_LESSON]})

    result = env("decide", str(task_id), "reject", "--note", "not now")

    assert (result.exit_code, result.stdout) == (0, "task 1: cancelled\n")
    assert [c.role for c in env.seams.runtime.calls] == [Role.LIBRARIAN]
    assert env.store.get_meta("librarian:acme:closed:1") == "done"
    assert any("AgentProfile" in body for body in _lessons(env))
    assert env("learn", "--all").stdout == "nothing to learn\n"


def test_decide_answer_learns_from_the_decision(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    reply = json.dumps(
        {
            "lessons": [
                {
                    "kind": "EscalationRule",
                    "text": "Spending over budget always needs a human",
                    "category": "stuck",
                }
            ]
        }
    )
    env.seams.script({Role.LIBRARIAN: [reply]})

    result = env("decide", str(task_id), "answer", "--note", "go on")

    assert result.exit_code == 0, result.stdout
    assert any(
        "Spending over budget always needs a human" in body
        for body in _lessons(env, "acme__company")
    )
    assert env.store.get(task_id).state is TaskState.AWAITING_HUMAN


def test_librarian_invalid_reply_is_warning_not_failure(env, repo):
    _over_budget(env)
    task_id = _new_task(env)
    _run_task(env, repo, str(task_id))
    env.seams.script({Role.LIBRARIAN: ["not json", "still not", "nope"]})

    result = env("decide", str(task_id), "reject", "--note", "not now")

    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == "task 1: cancelled"
    assert lines[1].startswith(f"warning: librarian #{task_id}: ")
    assert env.store.get(task_id).state is TaskState.CANCELLED
    assert len(_decisions(env, task_id)) == 1
    assert env.store.get_meta(f"librarian:acme:closed:{task_id}") == "done"
    decision = env.store.latest_artifact(task_id, ArtifactKind.HUMAN_DECISION)
    assert env.store.get_meta(f"librarian:acme:artifact:{decision.id}") is None


def _merge_pull(gh, number=7):
    pull = gh.get_pull("acme/app", number)
    gh.upsert_pull(
        dataclasses.replace(pull, state="closed", merged_at=datetime.now(UTC))
    )


def test_intake_merge_runs_close_out_and_followup(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    env.store.put_artifact(
        task_id,
        ArtifactKind.QA,
        Role.QA,
        {
            "passed": True,
            "bug_class": "tabs",
            "tests_run": [],
            "failures": [],
            "new_tests": ["tests/test_tabs_qa.py"],
            "new_test_sources": {"tests/test_tabs_qa.py": "def test_tabs(): ...\n"},
        },
    )
    _merge_pull(gh)

    result = _intake(env)

    assert (result.exit_code, result.stdout) == (
        0,
        f"#{task_id} merged\n0 known, 0 deferred\n",
    )
    assert env.store.get_meta(f"librarian:acme:closed:{task_id}") == "done"
    [followup] = [
        t for t in env.store.list_tasks("acme", [TaskState.INTAKE]) if t.id != task_id
    ]
    assert followup.source == "qa-followup"
    request = env.store.latest_artifact(followup.id, ArtifactKind.REQUEST)
    assert request.body["test_files"] == {
        "tests/test_tabs_qa.py": "def test_tabs(): ...\n"
    }
    assert any("fix tabs" in body for body in _lessons(env))

    second = _intake(env)
    assert second.exit_code == 0
    assert len(env.store.list_tasks("acme", [TaskState.INTAKE])) == 1


def test_intake_librarian_failure_does_not_change_the_exit_code(env, monkeypatch):
    gh = _intake_gh(env, monkeypatch)
    task_id = _pr_task(env, gh)
    _merge_pull(gh)
    monkeypatch.setattr(cli.Librarian, "learn_task", _report_boom)

    result = _intake(env)

    assert result.exit_code == 0, result.stdout
    assert f"warning: librarian #{task_id}: boom" in result.stdout


async def _report_boom(self, task_id, now=None):
    return LearnReport(task_id, errors=["boom"])


def test_task_run_all_runs_the_librarian_for_every_task_that_finished(
    env, repo, monkeypatch
):
    seen = []

    async def record(self, task_id, now=None):
        seen.append(task_id)
        return LearnReport(task_id)

    monkeypatch.setattr(cli.Librarian, "learn_task", record)
    env.seams.script(
        {Role.PM: [PM_JSON] * 3, Role.TRIAGE: [triage_json()] * 3}, premium=100
    )
    ids = [_new_task(env, f"task {n}") for n in range(2)]
    stuck = _new_task(env, "stuck")

    result = _run_task(env, repo, "--all")

    assert result.exit_code == 0, result.stdout
    assert sorted(seen) == sorted([*ids, stuck])


def _seed_metrics(store):
    store.create_all()
    tid = store.create_task("acme", "Fix flicker", "cli").id
    store.assign(tid, Role.JUNIOR)
    for state in (
        TaskState.PM,
        TaskState.TRIAGE,
        TaskState.ENGINEERING,
        TaskState.PREFLIGHT,
        TaskState.INTERNAL_REVIEW,
        TaskState.LEAD_DECISION,
        TaskState.PR_OPEN,
    ):
        store.transition(tid, state, Role.LEAD, "t")
    store.put_artifact(tid, ArtifactKind.PR, Role.LEAD, {"number": 7, "url": "u"})
    store.put_artifact(
        tid,
        ArtifactKind.PR_FEEDBACK,
        Role.LEAD,
        {"pr": 7, "findings": [{"id": "c1"}, {"id": "c2"}]},
    )
    store.record_cost(tid, Role.JUNIOR, 0.5, {"model": "m", "requests": 4})
    store.set_meta("librarian:acme:override:abc123", "3")
    store.set_meta("librarian:acme:override:low", "2")
    return tid


def test_cli_metrics_json(env):
    tid = _seed_metrics(env.store)
    result = env("metrics", "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["stale_rules"] == ["abc123"]
    assert data["totals"]["tasks"] == 1
    assert data["totals"]["pr_comments_per_pr"] == 2
    assert data["totals"]["premium_requests"] == 4
    (task,) = data["tasks"]
    assert (task["task_id"], task["title"], task["state"]) == (
        tid,
        "Fix flicker",
        "pr_open",
    )
    assert (task["assignee"], task["pr_findings"], task["cost_usd"]) == (
        "junior",
        2,
        0.5,
    )


def test_cli_metrics_table(env):
    _seed_metrics(env.store)
    result = env("metrics")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[:3] == ["tasks: 1", "merged: 0", "cancelled: 0"]
    assert "pr_comments_per_pr: 2" in lines
    assert "stale rules: abc123" in lines
    blank = lines.index("")
    assert lines[blank + 1].split() == [
        "id", "state", "assignee", "rounds", "pr_rounds", "pr_findings", "pr_real",
        "escalations", "requests", "cost_usd", "hours", "title",
    ]  # fmt: skip
    assert lines[blank + 2].split() == [
        "1", "pr_open", "junior", "0", "0", "2", "0", "0", "4", "0.50", "-",
        "Fix", "flicker",
    ]  # fmt: skip


def test_cli_metrics_needs_no_falkordb(env, monkeypatch):
    def boom(cfg):
        raise AssertionError("metrics must not build a memory backend")

    monkeypatch.setattr(cli, "make_backend", boom)
    monkeypatch.setattr(cli, "make_code_graph", boom)
    monkeypatch.setattr(cli, "make_github", boom)
    result = env("metrics", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["totals"]["tasks"] == 0


def test_cli_metrics_since_days(env):
    _seed_metrics(env.store)
    assert (
        json.loads(env("metrics", "--json", "--since", "1").stdout)["totals"]["tasks"]
        == 1
    )
    # a task that started a week ago falls outside --since 1
    old = env.store.create_task("acme", "old", "cli").id
    with env.store._sessions.begin() as session:
        session.execute(
            update(AuditRow)
            .where(AuditRow.task_id == old)
            .values(at=datetime.now(UTC) - timedelta(days=7))
        )
    titles = [
        t["title"]
        for t in json.loads(env("metrics", "--json", "--since", "1").stdout)["tasks"]
    ]
    assert titles == ["Fix flicker"]


@pytest.mark.parametrize("value", ["0", "-3", "abc", "1.5"])
def test_cli_metrics_bad_since_exits_1(env, value):
    result = env("metrics", "--since", value)
    assert result.exit_code == 1
    assert result.stdout.splitlines()[0].startswith("--since must be")


def test_cli_metrics_bad_config_exits_1(tmp_path):
    result = runner.invoke(
        cli.app, ["metrics", "--config", str(tmp_path / "missing.yml")]
    )
    assert result.exit_code == 1
