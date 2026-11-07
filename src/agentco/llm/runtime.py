from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from agentco.roles import Role


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[str]]


@dataclass(frozen=True)
class AgentCall:
    role: Role
    model: str
    system: str
    prompt: str
    workdir: Path | None = None
    edits: bool = False
    commands: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    max_turns: int = 40
    timeout_s: float = 1200


@dataclass(frozen=True)
class AgentResult:
    text: str
    premium_requests: float
    stuck: bool = False
    stuck_reason: str = ""


class AgentRuntime(Protocol):
    async def run(self, call: AgentCall) -> AgentResult: ...
