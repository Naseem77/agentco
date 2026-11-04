import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

_CLOSING_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")


@dataclass(frozen=True)
class GhUser:
    login: str
    is_bot: bool


@dataclass(frozen=True)
class GhIssue:
    number: int
    title: str
    body: str
    author: GhUser
    state: str
    state_reason: str | None
    labels: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    url: str


@dataclass(frozen=True)
class GhPull:
    number: int
    title: str
    body: str
    author: GhUser
    state: str
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    base_sha: str
    head_sha: str
    labels: tuple[str, ...]
    url: str
    closes_issues: tuple[int, ...]


@dataclass(frozen=True)
class GhReviewComment:
    id: int
    pr_number: int
    author: GhUser
    association: str
    body: str
    path: str
    line: int | None
    original_line: int | None
    side: Literal["LEFT", "RIGHT"]
    subject_type: Literal["line", "file"]
    commit_id: str
    original_commit_id: str
    diff_hunk: str
    in_reply_to: int | None
    created_at: datetime
    updated_at: datetime
    url: str


def parse_user(raw: dict | None) -> GhUser:
    if not raw:
        return GhUser("ghost", False)
    login = raw["login"]
    return GhUser(login, raw.get("type") == "Bot" or login.endswith("[bot]"))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _opt_dt(value: str | None) -> datetime | None:
    return _dt(value) if value else None


def _labels(raw: dict) -> tuple[str, ...]:
    return tuple(label["name"] for label in raw.get("labels") or ())


def linked_issues(text: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(m) for m in _CLOSING_RE.findall(text)))


def parse_issue(raw: dict) -> GhIssue | None:
    """None for pull requests, which the issues endpoint also lists."""
    if "pull_request" in raw:
        return None
    return GhIssue(
        number=raw["number"],
        title=raw.get("title") or "",
        body=raw.get("body") or "",
        author=parse_user(raw.get("user")),
        state=raw["state"],
        state_reason=raw.get("state_reason"),
        labels=_labels(raw),
        created_at=_dt(raw["created_at"]),
        updated_at=_dt(raw["updated_at"]),
        closed_at=_opt_dt(raw.get("closed_at")),
        url=raw["html_url"],
    )


def parse_pull(raw: dict) -> GhPull:
    body = raw.get("body") or ""
    return GhPull(
        number=raw["number"],
        title=raw.get("title") or "",
        body=body,
        author=parse_user(raw.get("user")),
        state=raw["state"],
        merged_at=_opt_dt(raw.get("merged_at")),
        created_at=_dt(raw["created_at"]),
        updated_at=_dt(raw["updated_at"]),
        base_sha=raw["base"]["sha"],
        head_sha=raw["head"]["sha"],
        labels=_labels(raw),
        url=raw["html_url"],
        closes_issues=linked_issues(body),
    )


def parse_review_comment(raw: dict) -> GhReviewComment:
    return GhReviewComment(
        id=raw["id"],
        pr_number=int(raw["pull_request_url"].rstrip("/").rsplit("/", 1)[1]),
        author=parse_user(raw.get("user")),
        association=raw.get("author_association") or "NONE",
        body=raw.get("body") or "",
        path=raw["path"],
        line=raw.get("line"),
        original_line=raw.get("original_line"),
        side=raw.get("side") or "RIGHT",
        subject_type=raw.get("subject_type") or "line",
        commit_id=raw.get("commit_id") or "",
        original_commit_id=raw.get("original_commit_id") or "",
        diff_hunk=raw.get("diff_hunk") or "",
        in_reply_to=raw.get("in_reply_to_id"),
        created_at=_dt(raw["created_at"]),
        updated_at=_dt(raw["updated_at"]),
        url=raw["html_url"],
    )
