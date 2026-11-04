import os
import shutil
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from agentco import cli

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


def test_import_smoke(cfg, tmp_path):
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
