"""Command runners for the gate commands (build/test/lint/e2e).

Agents' own `bash` calls run on the host, limited by the permission policy;
only gate commands go through a `ContainerRunner`. The repo contract's `build`
must work from a fresh checkout, since QA snapshots have no installed
dependencies.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentco.repo_contract import RepoContract, RepoContractError

TIMEOUT_EXIT = 124
NOT_FOUND_EXIT = 127
OUTPUT_LINES = 200
DEFAULT_PREPARE_TIMEOUT_S = 900

ExecFn = Callable[[list[str], Path, float], tuple[int, str]]


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    output: str  # last 200 lines
    duration_s: float
    timed_out: bool


class CommandRunner(Protocol):
    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult: ...


def _tail(text: str) -> str:
    lines = text.splitlines(keepends=True)
    return "".join(lines[-OUTPUT_LINES:])


def _exec(argv: list[str], cwd: Path, timeout_s: float) -> tuple[int, str]:
    """Run argv, merging stderr into stdout; exit 124 on timeout, 127 if not found."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        return NOT_FOUND_EXIT, f"{argv[0]}: {e}"
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        # Kill the whole group: stray grandchildren would hold the pipe open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()  # includes output captured before the timeout
        code = TIMEOUT_EXIT
    return code, out.decode(errors="replace")


class HostRunner:
    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        start = time.monotonic()
        code, output = _exec(["sh", "-lc", command], cwd, timeout_s)
        return CommandResult(
            command=command,
            exit_code=code,
            output=_tail(output),
            duration_s=time.monotonic() - start,
            timed_out=code == TIMEOUT_EXIT,
        )


class ContainerRunner:
    """Runs gate commands via `docker compose run` or `devcontainer exec`.

    `exec_` takes (argv, cwd, timeout_s) and returns (exit_code, output); exit
    124 means timeout. It is injectable so unit tests never start a container.
    """

    def __init__(
        self, contract: RepoContract, repo_root: Path, exec_: ExecFn | None = None
    ) -> None:
        self._contract = contract
        self._repo_root = repo_root
        self._exec = exec_ or _exec

    def container_argv(self, command: str, cwd: Path) -> list[str]:
        sandbox = self._contract.sandbox
        if sandbox.compose is not None:
            if sandbox.service is None:
                raise RepoContractError(
                    "sandbox.service is required when sandbox.compose is set"
                )
            return [
                "docker",
                "compose",
                "-f",
                str(self._repo_root / sandbox.compose),
                "run",
                "--rm",
                "-v",
                f"{cwd}:/workspace",
                "-w",
                "/workspace",
                sandbox.service,
                "sh",
                "-lc",
                command,
            ]
        return [
            "devcontainer",
            "exec",
            "--workspace-folder",
            str(cwd),
            "sh",
            "-lc",
            command,
        ]

    def prepare(self, cwd: Path, timeout_s: float = DEFAULT_PREPARE_TIMEOUT_S) -> None:
        if self._contract.sandbox.devcontainer is None:
            return
        argv = ["devcontainer", "up", "--workspace-folder", str(cwd)]
        code, output = self._exec(argv, cwd, timeout_s)
        if code != 0:
            raise ContainerError(
                f"devcontainer up failed (exit {code}): {_tail(output).strip()}"
            )

    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        argv = self.container_argv(command, cwd)
        start = time.monotonic()
        code, output = self._exec(argv, cwd, timeout_s)
        return CommandResult(
            command=command,
            exit_code=code,
            output=_tail(output),
            duration_s=time.monotonic() - start,
            timed_out=code == TIMEOUT_EXIT,
        )


def runner_for(contract: RepoContract, repo_root: Path, host: bool) -> CommandRunner:
    if host:
        return HostRunner()
    return ContainerRunner(contract, repo_root)
