"""Episode copy and review-thread selection for the GitHub history import.

Graphiti extracts entities from the episode body only, so each body's first line
says what the episode is.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC
from posixpath import dirname

from agentco.code.parse import Symbol
from agentco.github.models import GhIssue, GhPull, GhReviewComment
from agentco.memory.backend import MemoryItem

TEAM_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
FEATURE_LABELS = frozenset(
    {"enhancement", "feature", "feature request", "feature-request"}
)
TEXT_LIMIT = 2000
HUNK_LINES = 15
_REPLY_LIMIT = 5
_REPLY_TEXT_LIMIT = 500
_FILE_LIMIT = 50
_TRUNCATED = " …[truncated]"


def module_of(path: str) -> str:
    return dirname(path) or "."


@dataclass(frozen=True)
class ReviewThread:
    root: GhReviewComment
    replies: tuple[GhReviewComment, ...]


@dataclass(frozen=True)
class ThreadSelection:
    threads: list[ReviewThread]
    skipped: Counter[str]


def review_threads(
    comments: Iterable[GhReviewComment], pulls: Mapping[int, GhPull]
) -> ThreadSelection:
    """Human team-member review threads; counts why everything else was left out."""
    by_id = {c.id: c for c in comments}
    skipped: Counter[str] = Counter()
    roots: dict[int, GhReviewComment] = {}
    skipped_roots: set[int] = set()
    for c in by_id.values():
        if c.in_reply_to is not None:
            continue
        pr = pulls.get(c.pr_number)
        if pr is None:
            reason = "unknown_pr"
        elif c.author.is_bot:
            reason = "bot"
        elif c.author.login == pr.author.login:
            reason = "pr_author"
        elif c.association not in TEAM_ASSOCIATIONS:
            reason = "not_team"
        else:
            roots[c.id] = c
            continue
        skipped[reason] += 1
        skipped_roots.add(c.id)

    replies: dict[int, list[GhReviewComment]] = {root_id: [] for root_id in roots}
    for c in by_id.values():
        if c.in_reply_to is None:
            continue
        root_id = _root_of(c, by_id)
        if root_id is None:
            skipped["orphan_reply"] += 1
        elif root_id in skipped_roots:
            skipped["skipped_root"] += 1
        elif root_id in roots and not c.author.is_bot:
            replies[root_id].append(c)
        # bot replies under a kept root are dropped silently

    threads = [
        ReviewThread(
            root, tuple(sorted(replies[root.id], key=lambda r: (r.created_at, r.id)))
        )
        for root in roots.values()
    ]
    threads.sort(key=lambda t: (t.root.created_at, t.root.id))
    return ThreadSelection(threads, skipped)


def _root_of(
    comment: GhReviewComment, by_id: Mapping[int, GhReviewComment]
) -> int | None:
    """Id of the thread root a reply belongs to, or None when it is not in the input."""
    seen = {comment.id}
    current = comment
    while current.in_reply_to is not None:
        parent = by_id.get(current.in_reply_to)
        if parent is None or parent.id in seen:
            return None
        seen.add(parent.id)
        current = parent
    return current.id


def is_feature(issue: GhIssue) -> bool:
    return any(label.lower() in FEATURE_LABELS for label in issue.labels)


def _cut(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


def _indent(text: str) -> str:
    """Indent every line so untrusted text cannot start a line at column 0."""
    return "\n".join(f"  {line}" for line in text.splitlines())


def _labels(issue: GhIssue) -> str:
    return ", ".join(issue.labels) or "none"


def _with_body(head: str, body: str) -> str:
    body = body.strip()
    return f"{head}\n\n{_indent(_cut(body))}" if body else head


def issue_item(repo: str, issue: GhIssue) -> MemoryItem:
    state = issue.state + (f", {issue.state_reason}" if issue.state_reason else "")
    head = (
        f"GitHub issue #{issue.number} in {repo}: {issue.title} "
        f"(state: {state}; labels: {_labels(issue)}; opened by @{issue.author.login})."
    )
    return MemoryItem(
        name=f"issue:{repo}#{issue.number}",
        body=_with_body(head, issue.body),
        source_description=f"GitHub issue {issue.url}",
        reference_time=issue.created_at,
    )


def feature_item(repo: str, issue: GhIssue) -> MemoryItem:
    head = (
        f"Feature request from GitHub issue #{issue.number} in {repo}: {issue.title} "
        f"(state: {issue.state}; labels: {_labels(issue)})."
    )
    return MemoryItem(
        name=f"feature:{repo}#{issue.number}",
        body=_with_body(head, issue.body),
        source_description=f"GitHub issue {issue.url}",
        reference_time=issue.created_at,
    )


def _pull_status(pr: GhPull) -> str:
    if pr.merged_at is not None:
        merged = pr.merged_at
        if merged.tzinfo is not None:
            merged = merged.astimezone(UTC)
        return f"merged {merged.date().isoformat()}"
    return "closed without merging" if pr.state == "closed" else "open"


def pull_item(repo: str, pr: GhPull, files: list[str]) -> MemoryItem:
    status = _pull_status(pr)
    if pr.closes_issues:
        status += "; closes issues " + ", ".join(f"#{n}" for n in pr.closes_issues)
    modules = ", ".join(dict.fromkeys(module_of(f) for f in files)) or "none"
    shown = ", ".join(files[:_FILE_LIMIT]) or "none"
    if len(files) > _FILE_LIMIT:
        shown += f" (+{len(files) - _FILE_LIMIT} more)"
    head = (
        f"Pull request #{pr.number} in {repo} by @{pr.author.login}: {pr.title} "
        f"({status}).\nModules touched: {modules}\nFiles: {shown}"
    )
    return MemoryItem(
        name=f"pr:{repo}#{pr.number}",
        body=_with_body(head, pr.body),
        source_description=f"GitHub pull request {pr.url}",
        reference_time=pr.created_at,
    )


def _code_line(root: GhReviewComment, symbol: Symbol | None) -> str:
    line = root.original_line or root.line
    if root.subject_type == "file" or line is None:
        return f"Code: {root.path} (file-level)"
    if symbol is None:
        return f"Code: {root.path}:{line}"
    return f"Code: {root.path}:{line} in {symbol.kind.lower()} {symbol.qualname}"


def review_item(
    repo: str, thread: ReviewThread, pr: GhPull, symbol: Symbol | None
) -> MemoryItem:
    root = thread.root
    lines = [
        (
            f"Review rule from human reviewer @{root.author.login} "
            f"({root.association}) on pull request #{pr.number} in {repo} "
            f"(origin=human, module_path={module_of(root.path)})."
        ),
        _code_line(root, symbol),
        f"Comment: {_indent(_cut(root.body)).lstrip()}",
    ]
    if thread.replies:
        lines.append("Discussion:")
        lines.extend(
            f"@{r.author.login}: {_cut(' '.join(r.body.split()), _REPLY_TEXT_LIMIT)}"
            for r in thread.replies[:_REPLY_LIMIT]
        )
    hunk = root.diff_hunk.splitlines()[-HUNK_LINES:]
    if hunk:
        lines.append(f"Diff hunk (last {HUNK_LINES} lines):")
        lines.extend(hunk)
    return MemoryItem(
        name=f"review:{repo}:{root.id}",
        body="\n".join(lines),
        source_description=f"GitHub review comment {root.url}",
        reference_time=root.created_at,
    )
