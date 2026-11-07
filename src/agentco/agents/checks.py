import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass

from agentco.agents.qa import QAReply
from agentco.agents.review import ReviewReply


@dataclass(frozen=True)
class CheckOutcome:
    review: ReviewReply | None
    qa: QAReply | None
    errors: tuple[str, ...]


async def run_checks(
    review_coro: Awaitable[ReviewReply], qa_coro: Awaitable[QAReply]
) -> CheckOutcome:
    """Run review and QA concurrently; one failing never cancels the other."""
    review, qa = await asyncio.gather(review_coro, qa_coro, return_exceptions=True)
    errors: list[str] = []
    if isinstance(review, BaseException):
        errors.append(f"review: {type(review).__name__}: {review}")
        review = None
    if isinstance(qa, BaseException):
        errors.append(f"qa: {type(qa).__name__}: {qa}")
        qa = None
    return CheckOutcome(review, qa, tuple(errors))
