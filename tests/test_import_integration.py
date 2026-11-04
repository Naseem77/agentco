import asyncio
import os
import shutil
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from agentco import cli
from agentco.code.falkor_graph import build_falkor_code_graph

pytestmark = pytest.mark.integration

runner = CliRunner()
ROLES = [
    "pm",
    "triage",
    "architect",
    "senior",
    "junior",
    "reviewer",
    "qa",
    "lead",
    "librarian",
]


async def _delete_tenant_graphs(cfg) -> None:
    db = build_falkor_code_graph(cfg).db
    try:
        for name in await db.list_graphs():
            name = name.decode() if isinstance(name, bytes) else name
            if name.startswith(f"{cfg.tenant}__"):
                await db.select_graph(name).delete()
    finally:
        await db.aclose()


@pytest.fixture
def tenant_graphs(cfg):
    """The test's `<tenant>__*` graphs are removed even when the test fails."""
    yield cfg
    asyncio.run(_delete_tenant_graphs(cfg))


def write_config(tmp_path, cfg):
    path = tmp_path / "agentco.config.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "tenant": cfg.tenant,
                "database_url": f"sqlite:///{tmp_path}/state.db",
                "falkordb_host": cfg.falkordb_host,
                "falkordb_port": cfg.falkordb_port,
                "roles": {r: {"model": f"m-{r}", "family": f"fam-{r}"} for r in ROLES},
            }
        )
    )
    return path


def invoke(*args, config):
    return runner.invoke(cli.app, [*args, "--config", str(config)])


def dry_run_already(config, repo) -> int:
    r = invoke("import", "--repo", repo, "--dry-run", config=config)
    assert r.exit_code == 0
    line = next(x for x in r.stdout.splitlines() if x.startswith("already imported: "))
    return int(line.removeprefix("already imported: "))


def test_import_smoke(tenant_graphs, tmp_path):
    cfg = tenant_graphs
    # A small repo with a few PRs + human review comments; it must yield more than
    # 2 episodes, since the run below is capped with --limit 2.
    repo = os.environ.get("AGENTCO_IT_REPO")
    if (
        not repo
        or not shutil.which("gh")
        or subprocess.run(
            ["gh", "auth", "status"], capture_output=True, check=False
        ).returncode
        != 0
    ):
        pytest.skip("needs AGENTCO_IT_REPO and gh auth")
    config = write_config(tmp_path, cfg)
    before = dry_run_already(config, repo)
    r = invoke("import", "--repo", repo, "--limit", "2", config=config)
    assert r.exit_code == 0 and "imported 2 of" in r.stdout
    assert dry_run_already(config, repo) == before + 2
    out = invoke(
        "memory", "search", "pull request", "--as", "triage", config=config
    ).stdout
    assert f"{cfg.tenant}__rnd" in out
