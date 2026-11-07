"""Pre-flight gate: the cheap, mechanical checks a task passes before review.

Rebase and size fail fast. Every later check runs and every failure is reported,
so the engineer fixes everything in one round. The `red` check proves the new
tests fail without the fix, by reverting the source files on a committed clean
tree and always restoring the worktree afterwards.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from agentco.agents.prompts import load_prompt
from agentco.config import LoopLimits
from agentco.llm.replies import Asker, Reply
from agentco.llm.runtime import AgentCall
from agentco.memory.gate import ScopedMemory
from agentco.memory.scopes import Scope
from agentco.repo_contract import RepoContract
from agentco.roles import Role
from agentco.sandbox.affected import (
    AffectedTests,
    _is_test_name,
    affected_tests,
    is_test_path,
    test_command,
)
from agentco.sandbox.runner import (
    NOT_FOUND_EXIT,
    TIMEOUT_EXIT,
    CommandResult,
    CommandRunner,
    ContainerError,
)
from agentco.sandbox.worktree import (
    Worktree,
    _git,
    _run,
    changed_files,
    changed_lines,
    diff,
    rebase_onto,
)

DETAIL_LINES = 60
NO_TESTS_EXIT = 5  # pytest: no tests collected


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    checks: tuple[CheckResult, ...]
    conflict: bool

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "conflict": self.conflict,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
        }


class RestoreError(RuntimeError):
    pass


class RulesReply(Reply):
    violations: list[str] = Field(default_factory=list)


def _report(checks: list[CheckResult], conflict: bool = False) -> PreflightReport:
    return PreflightReport(
        ok=all(c.ok for c in checks), checks=tuple(checks), conflict=conflict
    )


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-DETAIL_LINES:])


def _restore(wt: Worktree) -> None:
    # No -x: ignored dependency directories must survive.
    _git(wt.path, "reset", "--hard", "HEAD")
    _git(wt.path, "clean", "-fd")


async def _restore_shielded(wt: Worktree) -> None:
    """Restore to completion even when the task is cancelled (again) meanwhile."""
    restore = asyncio.ensure_future(asyncio.to_thread(_restore, wt))
    cancelled = False
    while not restore.done():
        try:
            await asyncio.shield(restore)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - reported below
            break
    error = restore.exception() if restore.done() else None
    if error is not None:
        raise RestoreError(
            f"could not restore the worktree at {wt.path}; it may be left "
            f"reverted to the state without the fix: {error}"
        ) from error
    if cancelled:
        raise asyncio.CancelledError


def _revert_source(wt: Worktree, base: str, sources: list[str]) -> None:
    """Put every changed non-test file back to its state on base."""
    existing = [
        p
        for p in sources
        if _run(wt.path, "cat-file", "-e", f"{base}:{p}").returncode == 0
    ]
    if existing:
        _git(wt.path, "checkout", base, "--", *(f":(literal){p}" for p in existing))
    for p in sources:
        if p not in existing:
            (wt.path / p).unlink(missing_ok=True)


@dataclass
class Preflight:
    repo: str
    contract: RepoContract
    runner: CommandRunner
    limits: LoopLimits
    memory: ScopedMemory
    asker: Asker
    reviewer_model: str
    _prepared: set[Path] = field(default_factory=set, init=False, repr=False)

    async def _command(self, name: str, command: str, wt: Worktree) -> CheckResult:
        result = await self._exec(command, wt)
        if result.exit_code == 0:
            return CheckResult(name, True, "")
        return CheckResult(name, False, self._failure_detail(result))

    async def _exec(self, command: str, wt: Worktree) -> CommandResult:
        return await asyncio.to_thread(
            self.runner.run, command, wt.path, self.limits.command_timeout_s
        )

    def _failure_detail(self, result: CommandResult) -> str:
        detail = _tail(result.output)
        if result.timed_out:
            return f"timed out after {self.limits.command_timeout_s:g}s\n{detail}"
        return detail

    async def run(
        self,
        wt: Worktree,
        base: str,
        *,
        fix_since: str | None,
        final: bool,
        lead_comments: list[str],
        no_test_reason: str = "",
    ) -> PreflightReport:
        if not await asyncio.to_thread(rebase_onto, wt, base):
            return _report(
                [CheckResult("rebase", False, f"conflict rebasing onto {base}")],
                conflict=True,
            )
        checks = [CheckResult("rebase", True, "")]

        status = await asyncio.to_thread(
            _git, wt.path, "status", "--porcelain", "--untracked-files=all"
        )
        if status.strip():
            # the red check's restore would destroy uncommitted work
            checks.append(
                CheckResult(
                    "clean", False, "uncommitted changes in the worktree: commit first"
                )
            )
            return _report(checks)

        try:
            return await self._gate(
                wt, base, checks, fix_since, final, lead_comments, no_test_reason
            )
        finally:
            # the tree was clean, so this only removes what the commands wrote
            await _restore_shielded(wt)

    async def _gate(
        self,
        wt: Worktree,
        base: str,
        checks: list[CheckResult],
        fix_since: str | None,
        final: bool,
        lead_comments: list[str],
        no_test_reason: str,
    ) -> PreflightReport:
        if fix_since is not None:
            lines = await asyncio.to_thread(changed_lines, wt, fix_since)
            limit, scope = self.limits.fix_max_changed_lines, f"since {fix_since}"
        else:
            lines = await asyncio.to_thread(changed_lines, wt, base)
            limit, scope = self.limits.max_changed_lines, f"against {base}"
        if lines > limit:
            checks.append(
                CheckResult(
                    "size",
                    False,
                    f"{lines} changed lines {scope} exceeds the limit of {limit}; "
                    "keep fixes small",
                )
            )
            return _report(checks)
        checks.append(CheckResult("size", True, f"{lines} changed lines {scope}"))

        checks.append(await self._rules(wt, base, lead_comments))

        if not await self._prepare(wt, checks):
            return _report(checks)

        checks.append(await self._command("lint", self.contract.lint, wt))
        checks.append(await self._command("build", self.contract.build, wt))
        if final:
            command = self.contract.test
        else:
            affected = await affected_tests(self.repo, wt, base)
            command = test_command(self.contract, affected)
        checks.append(await self._command("tests", command, wt))
        checks.append(await self._red(wt, base, no_test_reason))
        if final and self.contract.e2e:
            checks.append(await self._command("e2e", self.contract.e2e, wt))
        return _report(checks)

    async def _prepare(self, wt: Worktree, checks: list[CheckResult]) -> bool:
        prepare = getattr(self.runner, "prepare", None)
        if prepare is None or wt.path in self._prepared:
            return True
        try:
            await asyncio.to_thread(prepare, wt.path)
        except ContainerError as e:
            checks.append(CheckResult("prepare", False, str(e)))
            return False
        self._prepared.add(wt.path)
        return True

    async def _rules(
        self, wt: Worktree, base: str, lead_comments: list[str]
    ) -> CheckResult:
        files = await asyncio.to_thread(changed_files, wt, base)
        rules: dict[str, None] = {}
        for path in files:
            facts = await self.memory.search(f"ReviewRule {path}", scopes=[Scope.RND])
            rules.update((f.text, None) for f in facts)
        rules.update((c, None) for c in lead_comments)
        if not rules:
            return CheckResult("rules", True, "no review rules apply")
        patch = await asyncio.to_thread(diff, wt, base)
        listed = "\n".join(f"- {r}" for r in rules)
        prompt = f"## Review rules\n{listed}\n\n## Diff\n{patch}"
        reply = await self.asker.ask(
            AgentCall(
                Role.REVIEWER,
                self.reviewer_model,
                load_prompt(Role.REVIEWER, "rules"),
                prompt,
                workdir=None,
                max_turns=self.limits.agent_max_turns,
                timeout_s=self.limits.agent_timeout_s,
            ),
            RulesReply,
        )
        if reply.violations:
            return CheckResult("rules", False, "\n".join(reply.violations))
        return CheckResult("rules", True, f"checked {len(rules)} rule(s)")

    async def _red(self, wt: Worktree, base: str, no_test_reason: str) -> CheckResult:
        files = await asyncio.to_thread(changed_files, wt, base)
        tests = [p for p in files if is_test_path(p) and (wt.path / p).is_file()]
        if not tests:
            if no_test_reason:
                return CheckResult("red", True, f"no new test: {no_test_reason}")
            return CheckResult("red", False, "no new test")
        sources = [p for p in files if not is_test_path(p)]
        if not sources:
            return CheckResult("red", True, "test-only change: no fix to revert")
        named = [p for p in tests if _is_test_name(p)]
        command = test_command(
            self.contract, AffectedTests(tuple(named), not named, "new tests")
        )
        try:
            # blocking on purpose: cancellation can only land at the command await
            _revert_source(wt, base, sources)
            result = await self._exec(command, wt)
        finally:
            await _restore_shielded(wt)
        if result.exit_code in (TIMEOUT_EXIT, NOT_FOUND_EXIT):
            return CheckResult(
                "red",
                False,
                f"could not run the new tests without the fix "
                f"(exit {result.exit_code})\n{self._failure_detail(result)}",
            )
        if result.exit_code == NO_TESTS_EXIT:
            return CheckResult(
                "red",
                False,
                f"new tests were not collected (exit {NO_TESTS_EXIT}): cannot "
                f"prove they fail without the fix\n{self._failure_detail(result)}",
            )
        if result.exit_code == 0:
            return CheckResult(
                "red", False, f"tests pass without the fix: {', '.join(tests)}"
            )
        return CheckResult(
            "red", True, f"new tests fail without the fix (exit {result.exit_code})"
        )
