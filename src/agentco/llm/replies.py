import json
import re
from dataclasses import dataclass, replace

from pydantic import BaseModel, ConfigDict, ValidationError

from agentco.llm.runtime import AgentCall, AgentRuntime
from agentco.store.task_store import TaskStore

_FENCE = re.compile(r"^[ \t]*```[A-Za-z]*[ \t]*$", re.MULTILINE)


class Reply(BaseModel):
    """Base class for every role reply."""

    model_config = ConfigDict(extra="ignore")


class ReplyInvalid(RuntimeError):
    pass


class AgentStuck(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _last_object(text: str) -> str:
    last: str | None = None
    pos = 0
    while (start := text.find("{", pos)) != -1:
        end = _balanced_end(text, start)
        if end is None:
            pos = start + 1
        else:
            last = text[start : end + 1]
            pos = end + 1
    if last is None:
        raise ValueError("no JSON object found in reply")
    return last


def parse_reply[T: Reply](text: str, reply_type: type[T]) -> T:
    """Validate the last balanced top-level {...} of text as reply_type."""
    return reply_type.model_validate_json(_last_object(_FENCE.sub("", text)))


@dataclass
class Asker:
    runtime: AgentRuntime
    store: TaskStore
    task_id: int
    price_per_request_usd: float

    async def ask[T: Reply](self, call: AgentCall, reply_type: type[T]) -> T:
        schema = json.dumps(reply_type.model_json_schema())
        call = replace(
            call,
            system=(
                f"{call.system}\n\nReply with only one JSON object matching "
                f"this JSON schema:\n{schema}"
            ),
        )
        error: Exception | None = None
        for attempt in range(2):
            attempt_call = call
            if error is not None:
                attempt_call = replace(
                    call,
                    prompt=(
                        f"{call.prompt}\n\nYour previous reply was invalid: "
                        f"{error}. Reply again with only the JSON object."
                    ),
                )
            result = await self.runtime.run(attempt_call)
            self.store.record_cost(
                self.task_id,
                call.role,
                result.premium_requests * self.price_per_request_usd,
                {
                    "model": call.model,
                    "requests": result.premium_requests,
                    "reply": reply_type.__name__,
                },
            )
            if result.stuck:
                raise AgentStuck(result.stuck_reason)
            try:
                return parse_reply(result.text, reply_type)
            except (ValueError, ValidationError) as exc:
                error = exc
        raise ReplyInvalid(
            f"{call.role} reply is not a valid {reply_type.__name__}: {error}"
        )
