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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentco.repo_contract import RepoContract, RepoContractError

TIMEOUT_EXIT = 124
NOT_FOUND_EXIT = 127
KILL_GRACE_S = 2.0
OUTPUT_LINES = 200
DEFAULT_PREPARE_TIMEOUT_S = 900

SAFE_ENV = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME"})
SAFE_ENV_PREFIXES = ("LC_",)

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


def safe_env(extra: Iterable[str] = ()) -> dict[str, str]:
    """The host environment cut down to an allowlist, so LLM-written commands
    cannot read API keys or tokens. `extra` names variables a repo opts into."""
    allowed = SAFE_ENV | set(extra)
    return {
        k: v
        for k, v in os.environ.items()
        if k in allowed or k.startswith(SAFE_ENV_PREFIXES)
    }


def _exec(
    argv: list[str],
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run argv, merging stderr into stdout; exit 124 on timeout, 127 if not found."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
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
        code = TIMEOUT_EXIT
        try:
            out, _ = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired as e:
            # A descendant in another session still holds the pipe: give up on it.
            out = e.output or b""
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait()
    return code, out.decode(errors="replace")


class HostRunner:
    def __init__(self, env_passthrough: Iterable[str] = ()) -> None:
        self._env_passthrough = tuple(env_passthrough)

    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        start = time.monotonic()
        env = safe_env(self._env_passthrough)
        code, output = _exec(["sh", "-lc", command], cwd, timeout_s, env)
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
        return HostRunner(contract.env_passthrough)
    return ContainerRunner(contract, repo_root)
