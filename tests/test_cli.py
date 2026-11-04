import pytest
import redis.exceptions
import yaml
from typer.testing import CliRunner

from agentco import cli
from agentco.memory.backend import InMemoryBackend
from agentco.store.task_store import TaskStore

runner = CliRunner()


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
    config = _write_config(tmp_path)

    def run(*args):
        return runner.invoke(cli.app, [*args, "--config", str(config)])

    run.backend = backend
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
    "error", [ConnectionError("x"), redis.exceptions.ConnectionError("x")]
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
