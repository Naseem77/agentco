"""Pre-flight gate: the cheap, mechanical checks a task passes before review.

Rebase and size fail fast. Then the rules, lint and build checks run at the
same time, and every failure is reported, so the engineer fixes everything in
one round. Once the build passes, the tests and the `red` check run at the same
time. The `red` check proves the new tests fail without the fix: it reverts the
source files in a throwaway copy of the commit, never in the worktree. A check
that passed on the same tree against the same base commit is not run again.
"""

import asyncio
import os
import posixpath
from collections.abc import Awaitable, Callable
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
from agentco.sandbox.check_cache import CheckCache, base_sha, tree_sha
from agentco.sandbox.runner import (
    NOT_FOUND_EXIT,
    TIMEOUT_EXIT,
    CommandResult,
    CommandRunner,
    ContainerError,
)
from agentco.sandbox.worktree import (
    GitError,
    Worktree,
    _clean,
    _git,
    _make_parent,
    _run,
    _safe_rel,
    changed_files,
    changed_lines,
    diff,
    make_snapshot,
    rebase_onto,
)

DETAIL_LINES = 60
NO_TESTS_EXIT = 5  # pytest: no tests collected
RULE_SEP = "\0"
RED_COPIES = "red"  # next to the worktrees: .agentco/red/<worktree name>


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


async def _all[T](*steps: Awaitable[T]) -> list[T]:
    """Run steps concurrently; the first error is raised only once all have ended,
    so no command is still running in the worktree when it is restored."""
    results = await asyncio.gather(*steps, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results  # type: ignore[return-value]


def red_copy_path(wt: Worktree) -> Path:
    return wt.path.parents[1] / RED_COPIES / wt.path.name


def _clear(copy: Path) -> None:
    # the directory itself stays: a dev container may have it bind-mounted
    if copy.is_dir():
        _clean(copy, keep_dir=True)


def _fresh_copy(wt: Worktree, copy: Path) -> None:
    _clear(copy)  # left over from a crash
    make_snapshot(wt, copy)


def _base_entries(wt: Worktree, base: str) -> dict[str, tuple[str, str]]:
    out = _git(wt.path, "ls-tree", "-r", "-z", "--full-tree", base)
    entries: dict[str, tuple[str, str]] = {}
    for record in out.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            mode, _kind, sha = meta.split()
            entries[path] = (mode, sha)
    return entries


def _revert_source(wt: Worktree, copy: Path, base: str, sources: list[str]) -> None:
    """In the copy, put every changed non-test file back to its state on base.

    Base is read from the worktree's repository (the copy has no history);
    nothing is written to the worktree.
    """
    on_base = _base_entries(wt, base)
    root = copy.resolve()
    for path in sources:
        target = copy / _safe_rel(path)
        _make_parent(root, target)
        if target.is_symlink() or target.is_file():
            target.unlink()
        mode, sha = on_base.get(path, ("", ""))
        if mode not in ("100644", "100755", "120000"):
            continue  # added by the fix (or a submodule): absent without it
        blob = _run(wt.path, "cat-file", "blob", sha)
        if blob.returncode != 0:
            raise GitError(f"cannot read {path!r} on {base}")
        if mode == "120000":
            link = blob.stdout.decode(errors="surrogateescape")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
            if posixpath.isabs(link) or resolved.split("/")[0] == "..":
                raise GitError(f"symlink {path!r} on {base} escapes the copy")
            os.symlink(link, target)
        else:
            target.write_bytes(blob.stdout)
            target.chmod(0o755 if mode == "100755" else 0o644)


@dataclass
class Preflight:
    repo: str
    contract: RepoContract
    runner: CommandRunner
    limits: LoopLimits
    memory: ScopedMemory
    asker: Asker
    reviewer_model: str
    cache: CheckCache = field(default_factory=CheckCache)
    _prepared: set[Path] = field(default_factory=set, init=False, repr=False)

    async def _cached(
        self,
        name: str,
        command: str,
        stamp: tuple[str, str],
        check: Callable[[], Awaitable[CheckResult]],
    ) -> CheckResult:
        key = self.cache.key(name, command, *stamp)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        result = await check()
        self.cache.put(key, result)
        return result

    async def _command(
        self, name: str, command: str, wt: Worktree, stamp: tuple[str, str]
    ) -> CheckResult:
        async def check() -> CheckResult:
            result = await self._exec(command, wt.path)
            if result.exit_code == 0:
                return CheckResult(name, True, "")
            return CheckResult(name, False, self._failure_detail(result))

        return await self._cached(name, command, stamp, check)

    async def _exec(self, command: str, cwd: Path) -> CommandResult:
        return await asyncio.to_thread(
            self.runner.run, command, cwd, self.limits.command_timeout_s
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
        test_only: bool = False,
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
            # the restore after the commands would destroy uncommitted work
            checks.append(
                CheckResult(
                    "clean", False, "uncommitted changes in the worktree: commit first"
                )
            )
            return _report(checks)
        # what every cached result is keyed on: content, and the base it is against
        stamp = (
            await asyncio.to_thread(tree_sha, wt),
            await asyncio.to_thread(base_sha, wt, base),
        )

        try:
            return await self._gate(
                wt,
                base,
                stamp,
                checks,
                fix_since,
                final,
                lead_comments,
                no_test_reason,
                test_only,
            )
        finally:
            # the tree was clean, so this only removes what the commands wrote
            await _restore_shielded(wt)

    async def _gate(
        self,
        wt: Worktree,
        base: str,
        stamp: tuple[str, str],
        checks: list[CheckResult],
        fix_since: str | None,
        final: bool,
        lead_comments: list[str],
        no_test_reason: str,
        test_only: bool,
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

        async def lint_and_build() -> list[CheckResult]:
            prepared: list[CheckResult] = []
            if not await self._prepare(wt, prepared):
                return prepared
            return await _all(
                self._command("lint", self.contract.lint, wt, stamp),
                self._command("build", self.contract.build, wt, stamp),
            )

        rules, commands = await _all(
            self._rules(wt, base, stamp, lead_comments), lint_and_build()
        )
        checks.extend([rules, *commands])  # type: ignore[list-item, misc]
        build = checks[-1]  # or the failed prepare
        if build.name == "prepare":
            return _report(checks)

        if build.ok:  # nothing to test without a build
            if final:
                command = self.contract.test
            else:
                affected = await affected_tests(self.repo, wt, base)
                command = test_command(self.contract, affected)
            checks.extend(
                await _all(
                    self._command("tests", command, wt, stamp),
                    self._cached(
                        "red",
                        RULE_SEP.join(("red", str(test_only), no_test_reason)),
                        stamp,
                        lambda: self._red(wt, base, no_test_reason, test_only),
                    ),
                )
            )
        if final and self.contract.e2e:
            checks.append(await self._command("e2e", self.contract.e2e, wt, stamp))
        return _report(checks)

    async def _prepare(self, wt: Worktree, checks: list[CheckResult]) -> bool:
        error = await self._prepare_path(wt.path)
        if error is not None:
            checks.append(CheckResult("prepare", False, error))
            return False
        return True

    async def _prepare_path(self, path: Path) -> str | None:
        prepare = getattr(self.runner, "prepare", None)
        if prepare is None or path in self._prepared:
            return None
        try:
            await asyncio.to_thread(prepare, path)
        except ContainerError as e:
            return str(e)
        self._prepared.add(path)
        return None

    async def _rules(
        self,
        wt: Worktree,
        base: str,
        stamp: tuple[str, str],
        lead_comments: list[str],
    ) -> CheckResult:
        if self.limits.pipeline == "fast":
            # the pre-PR reviewer already applied the review rules
            return CheckResult("rules", True, "skipped: fast pipeline")
        files = await asyncio.to_thread(changed_files, wt, base)
        rules: dict[str, None] = {}
        for path in files:
            facts = await self.memory.search(f"ReviewRule {path}", scopes=[Scope.RND])
            rules.update((f.text, None) for f in facts)
        rules.update((c, None) for c in lead_comments)
        if not rules:
            return CheckResult("rules", True, "no review rules apply")
        passed = CheckResult("rules", True, f"checked {len(rules)} rule(s)")
        # a diff that kept a larger rule set also keeps every subset of it
        if self.cache.find(
            "rules", *stamp, lambda command: set(rules) <= set(command.split(RULE_SEP))
        ):
            return passed
        result = await self._ask_rules(wt, base, rules)
        self.cache.put(
            self.cache.key("rules", RULE_SEP.join(sorted(rules)), *stamp), result
        )
        return result

    async def _ask_rules(
        self, wt: Worktree, base: str, rules: dict[str, None]
    ) -> CheckResult:
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

    async def _red(
        self, wt: Worktree, base: str, no_test_reason: str, test_only: bool
    ) -> CheckResult:
        files = await asyncio.to_thread(changed_files, wt, base)
        if test_only and files and all(is_test_path(p) for p in files):
            return CheckResult(
                "red", True, "test-only change: regression tests for a merged fix"
            )
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
        copy = red_copy_path(wt)
        try:
            result = await self._red_in_copy(wt, copy, base, sources, command)
        finally:
            await asyncio.to_thread(_clear, copy)
        if isinstance(result, CheckResult):
            return result
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

    async def _red_in_copy(
        self, wt: Worktree, copy: Path, base: str, sources: list[str], command: str
    ) -> CommandResult | CheckResult:
        """Run `command` on a copy of HEAD with the sources reverted to base.

        The copy is built first, as QA's snapshot is: it has none of the
        worktree's installed dependencies.
        """
        cannot = "could not {} a copy to run the new tests without the fix"
        try:
            await asyncio.to_thread(_fresh_copy, wt, copy)
        except GitError as e:
            return CheckResult("red", False, f"{cannot.format('make')}: {e}")
        error = await self._prepare_path(copy)
        if error is not None:
            return CheckResult("red", False, f"{cannot.format('prepare')}: {error}")
        build = await self._exec(self.contract.build, copy)
        if build.exit_code != 0:
            return CheckResult(
                "red",
                False,
                f"{cannot.format('build')} (exit {build.exit_code})\n"
                f"{self._failure_detail(build)}",
            )
        try:
            await asyncio.to_thread(_revert_source, wt, copy, base, sources)
        except (GitError, OSError) as e:
            return CheckResult("red", False, f"{cannot.format('revert')}: {e}")
        return await self._exec(command, copy)
