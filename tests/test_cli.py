import dataclasses
import subprocess
from datetime import UTC, datetime
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
from typer.testing import CliRunner

from agentco import cli
from agentco.code.graph import InMemoryCodeGraph
from agentco.github.client import FakeGitHub, GhCli, GitHubError
from agentco.memory.backend import InMemoryBackend
from agentco.store.task_store import TaskStore

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


@pytest.fixture
def env(tmp_path, monkeypatch):
    backend = InMemoryBackend()
    monkeypatch.setattr(cli, "make_backend", lambda cfg: backend)
    code_graph = InMemoryCodeGraph()
    monkeypatch.setattr(cli, "make_code_graph", lambda cfg: code_graph)
    monkeypatch.setattr(
        cli, "make_github", lambda: FakeGitHub.from_fixtures(GITHUB_FIXTURES)
    )
    config = _write_config(tmp_path)

    def run(*args):
        return runner.invoke(cli.app, [*args, "--config", str(config)])

    run.backend = backend
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
