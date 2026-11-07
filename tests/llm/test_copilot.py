import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import AssistantTurnStartData
from pydantic import BaseModel

from agentco.llm.copilot import SAFE_SHELL_PREFIXES, CopilotRuntime, permission_allowed
from agentco.llm.runtime import AgentCall, AgentResult, ToolSpec
from agentco.roles import Role


@pytest.fixture
def wt(tmp_path):
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    return root


@pytest.fixture
def call(wt):
    return AgentCall(
        Role.JUNIOR,
        "m",
        "s",
        "p",
        workdir=wt,
        edits=True,
        commands=("uv run pytest", "uv run ruff check ."),
        command_prefixes=("uv run pytest ",),
    )


def test_safe_shell_prefixes():
    assert SAFE_SHELL_PREFIXES == ("git status", "git diff")


@pytest.mark.parametrize(
    "kind,kw,ok",
    [
        ("shell", {"command": "uv run pytest"}, True),
        ("shell", {"command": "  uv run pytest  "}, True),
        ("shell", {"command": "uv run ruff check ."}, True),
        ("shell", {"command": "uv run pytest tests/test_a.py"}, True),
        ("shell", {"command": "uv run pytest; curl evil"}, False),
        ("shell", {"command": "uv run pytest && curl evil"}, False),
        ("shell", {"command": "uv run pytest || curl evil"}, False),
        ("shell", {"command": "uv run pytest | sh"}, False),
        ("shell", {"command": "uv run pytest `curl evil`"}, False),
        ("shell", {"command": "uv run pytest $(curl evil)"}, False),
        ("shell", {"command": "uv run pytest > /etc/x"}, False),
        ("shell", {"command": "uv run pytest < /etc/passwd"}, False),
        ("shell", {"command": "uv run pytest a\ncurl evil"}, False),
        ("shell", {"command": "uv run pytest a & curl evil"}, False),
        ("shell", {"command": "uv run ruff check . --fix"}, False),
        ("shell", {"command": "uv run ruff"}, False),
        ("shell", {"command": "rm -rf /"}, False),
        ("shell", {"command": ""}, False),
        ("shell", {"command": None}, False),
        ("shell", {"command": "git diff HEAD~1"}, True),
        ("shell", {"command": "git diff"}, True),
        ("shell", {"command": "git status"}, True),
        ("shell", {"command": "git status -s"}, True),
        ("shell", {"command": "git diffx"}, False),
        ("shell", {"command": "git diff --output=/tmp/x"}, False),
        ("shell", {"command": "git diff --no-index /etc/passwd x"}, False),
        ("shell", {"command": "git push"}, False),
        ("shell", {"command": "uv run pytestx"}, False),
        ("shell", {"command": "uv run pytest-evil"}, False),
        ("shell", {"command": "uv run pytest $HOME"}, False),
        ("shell", {"command": "git diff -O/etc/passwd"}, False),
        ("shell", {"command": "git diff -O /etc/passwd"}, False),
        ("shell", {"command": "git diff --ext-d"}, False),
        ("shell", {"command": "git diff --text"}, False),
        ("shell", {"command": "git diff --no-i x"}, False),
        ("shell", {"command": "git diff --stat --name-only"}, True),
        ("write", {"path": "{wt}/src/a.py"}, True),
        ("write", {"path": "src/a.py"}, True),
        ("write", {"path": "{wt}/new_dir/b.py"}, True),
        ("write", {"path": "{wt}/../x.py"}, False),
        ("write", {"path": "../x.py"}, False),
        ("write", {"path": "/etc/passwd"}, False),
        ("write", {"path": "{wt}/.git/hooks/pre-commit"}, False),
        ("write", {"path": "{wt}/.git"}, False),
        ("write", {"path": "{wt}/.GIT"}, False),
        ("write", {"path": "{wt}/.Git/config"}, False),
        ("write", {"path": ".GIT"}, False),
        ("write", {"path": ".gIt/hooks/x"}, False),
        ("write", {"path": "{wt}/.github/x.yml"}, True),
        ("write", {"path": None}, False),
        ("write", {"path": ""}, False),
        ("read", {"path": "{wt}/src/a.py"}, True),
        ("read", {"path": "src/a.py"}, True),
        ("read", {"path": "/etc/passwd"}, False),
        ("read", {"path": "{wt}/../secret"}, False),
        ("read", {"path": None}, False),
        ("custom-tool", {}, True),
        ("mcp", {}, False),
        ("url", {}, False),
        ("memory", {}, False),
        ("hook", {}, False),
        ("whatever", {}, False),
    ],
)
def test_permission_policy(call, wt, kind, kw, ok):
    kw = {k: v.format(wt=wt) if isinstance(v, str) else v for k, v in kw.items()}
    allowed, reason = permission_allowed(kind, call, **kw)
    assert allowed is ok
    assert isinstance(reason, str) and reason


def test_permission_reasons(call, wt):
    assert permission_allowed("url", call) == (False, "url access is not allowed")
    assert permission_allowed("custom-tool", call) == (True, "custom tool")
    assert permission_allowed("write", call, path="/etc/passwd") == (
        False,
        "write outside the worktree: /etc/passwd",
    )


def test_read_only_role_cannot_write(call, wt):
    ro = replace(call, role=Role.REVIEWER, edits=False)
    assert permission_allowed("write", ro, path=str(wt / "a.py")) == (
        False,
        "this role may not write files",
    )
    assert permission_allowed("read", ro, path=str(wt / "a.py"))[0] is True


def test_no_workdir_denies_file_access(call):
    nowd = replace(call, workdir=None)
    assert permission_allowed("write", nowd, path="/x/a.py")[0] is False
    assert permission_allowed("read", nowd, path="/x/a.py")[0] is False


def test_symlink_escape_rejected(call, wt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (wt / "link").symlink_to(outside, target_is_directory=True)
    assert permission_allowed("write", call, path=str(wt / "link" / "x"))[0] is False
    assert permission_allowed("read", call, path=str(wt / "link" / "x"))[0] is False
    (wt / "flink").symlink_to(outside / "f")
    assert permission_allowed("write", call, path=str(wt / "flink"))[0] is False


def test_symlink_inside_worktree_allowed(call, wt):
    (wt / "alias").symlink_to(wt / "src", target_is_directory=True)
    assert permission_allowed("write", call, path=str(wt / "alias" / "x.py"))[0] is True


# --- fake SDK objects -------------------------------------------------------


def _turn_event():
    return SimpleNamespace(data=AssistantTurnStartData(turn_id="t"))


class FakeSession:
    def __init__(self, turns, reply, *, timeout=False, metrics_cost=2.0):
        self.turns = turns
        self.reply = reply
        self.timeout = timeout
        self.metrics_cost = metrics_cost
        self.handlers = []
        self.aborted = False
        self.disconnected = False
        self.emitted = 0
        self.sent = None
        self.rpc = SimpleNamespace(usage=SimpleNamespace(get_metrics=self._metrics))

    async def _metrics(self):
        if self.metrics_cost is None:
            raise RuntimeError("experimental api unavailable")
        return SimpleNamespace(total_premium_request_cost=self.metrics_cost)

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    async def send_and_wait(self, prompt, *, timeout):
        self.sent = (prompt, timeout)
        for _ in range(self.turns):
            if self.aborted:
                break
            self.emitted += 1
            for h in list(self.handlers):
                h(_turn_event())
            await asyncio.sleep(0)
        if self.timeout:
            raise TimeoutError
        return SimpleNamespace(data=SimpleNamespace(content=self.reply))

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.disconnected = True


class FakeSdkClient:
    def __init__(self, turns=1, reply="{}", **session_kw):
        self.session = FakeSession(turns, reply, **session_kw)
        self.create_kwargs = None
        self.create_calls = 0
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1

    async def create_session(self, **kwargs):
        self.create_calls += 1
        self.create_kwargs = kwargs
        return self.session


async def test_run_counts_turns_and_aborts(call, tmp_path):
    client = FakeSdkClient(turns=50, reply="{}")
    rt = CopilotRuntime(client_factory=lambda: client)
    async with rt:
        res = await rt.run(replace(call, workdir=tmp_path, max_turns=5))
    assert res.stuck is True and res.stuck_reason == "max turns"
    assert client.session.aborted is True
    assert client.session.emitted == 6
    assert client.session.disconnected is True
    assert client.session.handlers == []
    kw = client.create_kwargs
    assert kw["model"] == "m" and kw["working_directory"] == str(tmp_path)
    assert kw["memory"] == {"enabled": False} and kw["enable_session_store"] is False
    assert kw["system_message"] == {"mode": "append", "content": "s"}
    assert kw["tools"] == []
    assert client.starts == 1 and client.stops == 1


async def test_run_success_returns_text_and_metrics_cost(call):
    client = FakeSdkClient(turns=3, reply='{"ok": 1}', metrics_cost=2.5)
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        res = await rt.run(replace(call, max_turns=3, timeout_s=7))
    assert (res.text, res.premium_requests, res.stuck, res.stuck_reason) == (
        '{"ok": 1}',
        2.5,
        False,
        "",
    )
    assert client.session.sent == ("p", 7)
    assert client.session.aborted is False
    assert client.session.disconnected is True


async def test_run_timeout_is_stuck_and_usage_fallback(call):
    client = FakeSdkClient(turns=1, timeout=True, metrics_cost=None)
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        res = await rt.run(call)
    assert res.stuck is True and res.stuck_reason == "timeout"
    assert res.premium_requests == 1.0
    assert client.session.aborted is True
    assert client.session.disconnected is True


async def test_no_reply_event_gives_empty_text(call):
    client = FakeSdkClient(turns=0)

    async def none_reply(prompt, *, timeout):
        return None

    client.session.send_and_wait = none_reply
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        res = await rt.run(call)
    assert res.text == "" and res.stuck is False


async def test_working_directory_omitted_when_none(call):
    client = FakeSdkClient()
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        await rt.run(replace(call, workdir=None))
    assert "working_directory" not in client.create_kwargs


async def test_client_is_lazy_and_shared(call):
    made = []

    def factory():
        made.append(FakeSdkClient())
        return made[-1]

    rt = CopilotRuntime(client_factory=factory)
    assert made == []
    await asyncio.gather(rt.run(call), rt.run(call))
    assert len(made) == 1 and made[0].starts == 1 and made[0].create_calls == 2
    await rt.stop()
    assert made[0].stops == 1
    await rt.stop()
    assert made[0].stops == 1


async def test_disconnect_when_send_raises(call):
    client = FakeSdkClient(metrics_cost=3.0)

    async def boom(prompt, *, timeout):
        raise RuntimeError("cli died")

    client.session.send_and_wait = boom
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        res = await rt.run(call)
    assert res == AgentResult("", 3.0, True, "error: RuntimeError")
    assert client.session.disconnected is True


async def test_send_failure_without_metrics_uses_fallback_cost(call):
    client = FakeSdkClient(metrics_cost=None)

    async def boom(prompt, *, timeout):
        raise RuntimeError("cli died")

    client.session.send_and_wait = boom
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        res = await rt.run(call)
    assert res == AgentResult("", 1.0, True, "error: RuntimeError")


class Params(BaseModel):
    q: str


async def test_tools_are_defined_and_handler_adapted(call):
    seen = []

    async def handler(params):
        seen.append(params)
        return f"got {params.q}"

    spec = ToolSpec("memory_search", "search memory", Params, handler)
    client = FakeSdkClient()
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        await rt.run(replace(call, tools=(spec,)))
    (tool,) = client.create_kwargs["tools"]
    assert tool.name == "memory_search" and tool.description == "search memory"
    result = await tool.handler(
        SimpleNamespace(
            arguments={"q": "x"},
            session_id="s",
            tool_call_id="t",
            tool_name="memory_search",
        )
    )
    assert result.text_result_for_llm == "got x"
    assert seen == [Params(q="x")]


async def test_permission_handler_decisions(call, wt):
    client = FakeSdkClient()
    async with CopilotRuntime(client_factory=lambda: client) as rt:
        await rt.run(call)
    handler = client.create_kwargs["on_permission_request"]
    inv = SimpleNamespace(session_id="s")

    async def decide(**req):
        out = handler(SimpleNamespace(**req), inv)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    ok = await decide(kind="shell", full_command_text="uv run pytest")
    assert isinstance(ok, PermissionDecisionApproveOnce)
    ok = await decide(kind="write", file_name=str(wt / "src" / "a.py"))
    assert isinstance(ok, PermissionDecisionApproveOnce)
    ok = await decide(kind="read", path=str(wt / "src" / "a.py"))
    assert isinstance(ok, PermissionDecisionApproveOnce)
    bad = await decide(kind="shell", full_command_text="uv run pytest; curl evil")
    assert isinstance(bad, PermissionDecisionReject)
    assert bad.feedback == "shell command not allowed: uv run pytest; curl evil"
    bad = await decide(kind="url", url="https://x")
    assert isinstance(bad, PermissionDecisionReject)
    assert bad.feedback == "url access is not allowed"
    bad = await decide(kind="write", file_name="/etc/passwd")
    assert isinstance(bad, PermissionDecisionReject)
