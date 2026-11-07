from collections import deque
from collections.abc import Callable, Mapping, Sequence

from agentco.llm.runtime import AgentCall, AgentResult
from agentco.roles import Role

Step = str | AgentResult | Callable[[AgentCall], str | AgentResult]


class FakeRuntime:
    """Scripted AgentRuntime for tests: each role consumes its steps in order."""

    def __init__(
        self, script: Mapping[Role, Sequence[Step]], premium_requests: float = 1.0
    ) -> None:
        self._script = {Role(role): deque(steps) for role, steps in script.items()}
        self._premium_requests = premium_requests
        self.calls: list[AgentCall] = []

    async def run(self, call: AgentCall) -> AgentResult:
        self.calls.append(call)
        steps = self._script.get(call.role)
        if not steps:
            raise AssertionError(f"no scripted reply for {call.role}")
        step = steps.popleft()
        if callable(step):
            step = step(call)
        if isinstance(step, AgentResult):
            return step
        return AgentResult(step, self._premium_requests)
