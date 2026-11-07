from pathlib import Path

import pytest

from agentco.repo_contract import RepoContract, RepoContractError, Sandbox
from agentco.sandbox.runner import (
    CommandResult,
    ContainerError,
    ContainerRunner,
    HostRunner,
    runner_for,
)


def contract(**sandbox) -> RepoContract:
    return RepoContract(
        build="b", test="t", e2e=None, lint="l", sandbox=Sandbox(**sandbox)
    )


def test_host_runner_success_captures_output(tmp_path):
    r = HostRunner().run("echo out; echo err >&2; exit 3", tmp_path, timeout_s=10)
    assert isinstance(r, CommandResult)
    assert r.command == "echo out; echo err >&2; exit 3"
    assert (r.exit_code, r.timed_out) == (3, False)
    assert sorted(r.output.split()) == ["err", "out"]
    assert r.duration_s >= 0


def test_host_runner_uses_cwd(tmp_path):
    r = HostRunner().run("pwd -P", tmp_path, timeout_s=10)
    assert r.output.strip() == str(tmp_path.resolve())


def test_host_runner_timeout(tmp_path):
    r = HostRunner().run("sleep 5", tmp_path, timeout_s=0.2)
    assert (r.exit_code, r.timed_out) == (124, True)
    assert r.duration_s < 4


def test_host_runner_timeout_kills_grandchildren_holding_the_pipe(tmp_path):
    r = HostRunner().run("echo started; (sleep 5; echo late) & wait", tmp_path, 0.3)
    assert (r.exit_code, r.timed_out) == (124, True)
    assert r.output.strip() == "started"
    assert r.duration_s < 4


def test_host_runner_keeps_last_200_lines(tmp_path):
    r = HostRunner().run("seq 1 500", tmp_path, timeout_s=10)
    lines = r.output.splitlines()
    assert (len(lines), lines[0], lines[-1]) == (200, "301", "500")


def test_host_runner_tolerates_non_utf8(tmp_path):
    r = HostRunner().run("printf 'a\\377b'", tmp_path, timeout_s=10)
    assert (r.exit_code, r.output) == (0, "a\ufffdb")


def test_container_argv_compose(tmp_path):
    runner = ContainerRunner(
        contract(compose="ci/compose.yml", service="app"), tmp_path
    )
    cwd = tmp_path / "wt"
    assert runner.container_argv("make test", cwd) == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "ci/compose.yml"),
        "run",
        "--rm",
        "-v",
        f"{cwd}:/workspace",
        "-w",
        "/workspace",
        "app",
        "sh",
        "-lc",
        "make test",
    ]


def test_container_argv_devcontainer(tmp_path):
    runner = ContainerRunner(contract(devcontainer=".devcontainer"), tmp_path)
    cwd = tmp_path / "wt"
    assert runner.container_argv("make test", cwd) == [
        "devcontainer",
        "exec",
        "--workspace-folder",
        str(cwd),
        "sh",
        "-lc",
        "make test",
    ]


def test_compose_without_service_raises(tmp_path):
    runner = ContainerRunner(contract(compose="compose.yml"), tmp_path)
    with pytest.raises(RepoContractError) as e:
        runner.container_argv("make test", tmp_path)
    assert str(e.value) == "sandbox.service is required when sandbox.compose is set"
    with pytest.raises(RepoContractError):
        runner.run("make test", tmp_path, 5)


def test_container_run_delegates_to_exec(tmp_path):
    calls: list[tuple[list[str], Path, float]] = []

    def fake_exec(argv, cwd, timeout_s):
        calls.append((argv, cwd, timeout_s))
        return 1, "boom\n"

    runner = ContainerRunner(
        contract(devcontainer=".devcontainer"), tmp_path, fake_exec
    )
    wt = tmp_path / "wt"
    r = runner.run("make test", wt, 30)
    assert calls == [(runner.container_argv("make test", wt), wt, 30)]
    assert (r.command, r.exit_code, r.output, r.timed_out) == (
        "make test",
        1,
        "boom\n",
        False,
    )


def test_container_run_reports_timeout_on_124(tmp_path):
    runner = ContainerRunner(
        contract(devcontainer="d"), tmp_path, lambda argv, cwd, t: (124, "")
    )
    r = runner.run("x", tmp_path, 1)
    assert (r.exit_code, r.timed_out) == (124, True)


def test_container_run_trims_output_to_200_lines(tmp_path):
    text = "\n".join(str(i) for i in range(1, 501)) + "\n"
    runner = ContainerRunner(
        contract(devcontainer="d"), tmp_path, lambda a, c, t: (0, text)
    )
    lines = runner.run("x", tmp_path, 1).output.splitlines()
    assert (len(lines), lines[0], lines[-1]) == (200, "301", "500")


def test_container_default_exec_missing_binary_is_exit_127(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    runner = ContainerRunner(contract(devcontainer="d"), tmp_path)
    r = runner.run("make test", tmp_path, 5)
    assert (r.exit_code, r.timed_out) == (127, False)
    assert "devcontainer" in r.output


def test_prepare_devcontainer_runs_up(tmp_path):
    calls = []

    def fake_exec(argv, cwd, timeout_s):
        calls.append((argv, cwd))
        return 0, ""

    wt = tmp_path / "wt"
    ContainerRunner(contract(devcontainer="d"), tmp_path, fake_exec).prepare(wt)
    assert calls == [(["devcontainer", "up", "--workspace-folder", str(wt)], wt)]


def test_prepare_devcontainer_failure_raises(tmp_path):
    runner = ContainerRunner(
        contract(devcontainer="d"), tmp_path, lambda a, c, t: (1, "no docker")
    )
    with pytest.raises(ContainerError) as e:
        runner.prepare(tmp_path)
    assert str(e.value) == "devcontainer up failed (exit 1): no docker"


def test_prepare_compose_is_noop(tmp_path):
    def fake_exec(argv, cwd, timeout_s):
        raise AssertionError("compose prepare must not exec")

    ContainerRunner(
        contract(compose="c.yml", service="app"), tmp_path, fake_exec
    ).prepare(tmp_path)


def test_runner_for(tmp_path):
    c = contract(compose="c.yml", service="app")
    assert isinstance(runner_for(c, tmp_path, host=True), HostRunner)
    r = runner_for(c, tmp_path, host=False)
    assert isinstance(r, ContainerRunner)
    assert r.container_argv("x", tmp_path)[:3] == ["docker", "compose", "-f"]
