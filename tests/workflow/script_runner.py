"""Shared fixtures for the pre-flight and orchestrator tests: a scripted command runner."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agentco.repo_contract import RepoContract, Sandbox
from agentco.sandbox.runner import CommandResult, ContainerError

ORIGINAL = "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
FIXED = "def add(a, b):\n    return b + a\n\ndef mul(a, b):\n    return a * b\n"
FILES = {
    "src/calc.py": ORIGINAL,
    "tests/test_add.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
}
NEW_TEST = "def test_new():\n    assert add(2, 1) == 3\n"
LINT, BUILD, FULL, E2E = "lint", "build", "pytest-all", "e2e"
RED_CMD = "pytest tests/test_new.py"
TESTS_CMD = "pytest tests/test_add.py tests/test_new.py"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@dataclass
class ScriptRunner:
    """Records every command with the state of src/calc.py when it ran."""

    codes: dict[str, int] = field(default_factory=dict)
    default: int = 0
    raise_on: str | None = None
    prepare_error: str | None = None
    calls: list[str] = field(default_factory=list)
    calc_at_call: dict[str, str | None] = field(default_factory=dict)
    prepared: list[Path] = field(default_factory=list)

    def prepare(self, cwd: Path) -> None:
        if self.prepare_error:
            raise ContainerError(self.prepare_error)
        self.prepared.append(cwd)

    def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        self.calls.append(command)
        calc = cwd / "src/calc.py"
        self.calc_at_call[command] = calc.read_text() if calc.exists() else None
        if command == self.raise_on:
            raise RuntimeError("runner crashed")
        code = self.codes.get(command, self.default)
        output = "\n".join(f"line {i}" for i in range(100)) if code else ""
        return CommandResult(command, code, output, 0.0, code == 124)


def contract(e2e: str | None = None) -> RepoContract:
    return RepoContract(
        build=BUILD,
        test=FULL,
        e2e=e2e,
        lint=LINT,
        sandbox=Sandbox(devcontainer=".devcontainer"),
        test_files="pytest {files}",
    )
