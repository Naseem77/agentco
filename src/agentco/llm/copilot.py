import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import copilot
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import AssistantTurnStartData

from agentco.llm.runtime import AgentCall, AgentResult, ToolSpec

SAFE_SHELL_PREFIXES = ("git status", "git diff")

_SHELL_METACHARS = (";", "&", "|", "`", "$(", "${", ">", "<", "\n", "\r")
# git flags that write outside the worktree or run external programs
_UNSAFE_GIT_FLAGS = ("--output", "--no-index", "--ext-diff", "--textconv")
_FALLBACK_PREMIUM_REQUESTS = 1.0


def _shell_allowed(call: AgentCall, command: str | None) -> tuple[bool, str]:
    denied = (False, f"shell command not allowed: {command or ''}")
    if not command or any(c in command for c in _SHELL_METACHARS):
        return denied
    command = command.strip()
    if command in call.commands:
        return True, "configured command"
    if any(command.startswith(p) for p in call.command_prefixes):
        return True, "configured command prefix"
    for prefix in SAFE_SHELL_PREFIXES:
        if command == prefix or command.startswith(prefix + " "):
            if any(flag in command for flag in _UNSAFE_GIT_FLAGS):
                return denied
            return True, "read-only git command"
    return denied


def _resolve_inside(workdir: Path, path: str | None) -> Path | None:
    """Resolve path (symlinks included) against workdir; None if it escapes."""
    if not path:
        return None
    root = workdir.resolve()
    try:
        resolved = (root / path).resolve()
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_relative_to(root) else None


def permission_allowed(
    kind: str,
    call: AgentCall,
    *,
    command: str | None = None,
    path: str | None = None,
) -> tuple[bool, str]:
    if kind == "custom-tool":
        return True, "custom tool"
    if kind == "shell":
        return _shell_allowed(call, command)
    if kind in ("write", "read"):
        if kind == "write" and not call.edits:
            return False, "this role may not write files"
        if call.workdir is None:
            return False, "no worktree for file access"
        resolved = _resolve_inside(call.workdir, path)
        if resolved is None:
            return False, f"{kind} outside the worktree: {path}"
        if (
            kind == "write"
            and ".git" in resolved.relative_to(call.workdir.resolve()).parts
        ):
            return False, f"write to .git is not allowed: {path}"
        return True, f"{kind} inside the worktree"
    return False, f"{kind} access is not allowed"


def _permission_handler(call: AgentCall) -> Callable[[Any, Any], Any]:
    def handle(request: Any, invocation: Any) -> Any:
        kind = getattr(request, "kind", "")
        allowed, reason = permission_allowed(
            kind,
            call,
            command=getattr(request, "full_command_text", None),
            path=getattr(request, "file_name", None)
            if kind == "write"
            else getattr(request, "path", None),
        )
        if allowed:
            return PermissionDecisionApproveOnce()
        return PermissionDecisionReject(feedback=reason)

    return handle


def _define_tool(spec: ToolSpec) -> Any:
    async def handler(params: Any, invocation: Any) -> str:
        return await spec.handler(params)

    return copilot.define_tool(
        spec.name,
        description=spec.description,
        params_type=spec.params,
        handler=handler,
    )


class CopilotRuntime:
    """AgentRuntime backed by the Copilot SDK; one CLI client shared across calls."""

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or (lambda: copilot.CopilotClient())
        self._client: Any = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._client is None:
                client = self._client_factory()
                await client.start()
                self._client = client

    async def stop(self) -> None:
        async with self._start_lock:
            client, self._client = self._client, None
            if client is not None:
                await client.stop()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def run(self, call: AgentCall) -> AgentResult:
        await self.start()
        kwargs: dict[str, Any] = {
            "model": call.model,
            "system_message": {"mode": "append", "content": call.system},
            "tools": [_define_tool(spec) for spec in call.tools],
            "on_permission_request": _permission_handler(call),
            "memory": {"enabled": False},
            "enable_session_store": False,
        }
        if call.workdir is not None:
            kwargs["working_directory"] = str(call.workdir)
        session = await self._client.create_session(**kwargs)

        turns = 0
        stuck_reason = ""
        aborts: list[asyncio.Task[None]] = []

        def on_event(event: Any) -> None:
            nonlocal turns, stuck_reason
            if not isinstance(getattr(event, "data", None), AssistantTurnStartData):
                return
            turns += 1
            if turns > call.max_turns and not stuck_reason:
                stuck_reason = "max turns"
                aborts.append(asyncio.ensure_future(session.abort()))

        unsubscribe = session.on(on_event)
        text = ""
        try:
            try:
                reply = await session.send_and_wait(call.prompt, timeout=call.timeout_s)
                text = getattr(getattr(reply, "data", None), "content", None) or ""
            except TimeoutError:
                # a timeout only stops the wait, not the agent
                stuck_reason = stuck_reason or "timeout"
                with contextlib.suppress(Exception):
                    await session.abort()
            finally:
                unsubscribe()
                await asyncio.gather(*aborts, return_exceptions=True)
            premium = await self._premium_requests(session)
        finally:
            with contextlib.suppress(Exception):
                await session.disconnect()
        return AgentResult(text, premium, bool(stuck_reason), stuck_reason)

    @staticmethod
    async def _premium_requests(session: Any) -> float:
        try:
            metrics = await session.rpc.usage.get_metrics()
            return float(metrics.total_premium_request_cost)
        except Exception:  # noqa: BLE001 - experimental API, any failure falls back
            return _FALLBACK_PREMIUM_REQUESTS
