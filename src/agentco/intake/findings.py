import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentco.agents.replies import Finding
from agentco.config import IntakeConfig
from agentco.github.client import GitHub
from agentco.github.models import GhReviewComment
from agentco.importer.episodes import TEAM_ASSOCIATIONS
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState

MAX_BODY = 2000
_TITLE_MAX = 80
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")
_DIGITS_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")
_TERMINAL = (TaskState.MERGED, TaskState.CANCELLED)


def normalize_text(body: str) -> str:
    """Complaint text with code fences, URLs, digits and whitespace runs collapsed."""
    text = _CODE_FENCE_RE.sub(" ", body.lower())
    text = _URL_RE.sub(" ", text)
    text = _DIGITS_RE.sub("", text)
    normalized = _SPACE_RE.sub(" ", text).strip()
    if not normalized:
        # Fence-, URL- or digit-only: the text that was stripped is all there is.
        normalized = _SPACE_RE.sub(" ", body).strip()
    return normalized


def fingerprint(path: str | None, body: str) -> str:
    """Stable id of a complaint: the same text on the same file, however it is dressed up."""
    return hashlib.sha1(f"{path or ''}\n{normalize_text(body)}".encode()).hexdigest()[
        :12
    ]


def _check_fingerprint(name: str) -> str:
    """Check names are identifiers: `tests (3.11)` and `tests (3.12)` are different jobs."""
    return hashlib.sha1(f"\ncheck:{name}".encode()).hexdigest()[:12]


class PRFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source: Literal["comment", "review", "check"]
    author: str
    is_bot: bool
    association: str
    path: str | None = None
    line: int | None = None
    body: str = Field(max_length=MAX_BODY)
    url: str
    created_at: datetime
    fingerprint: str
    review_state: str | None = None

    def marker_key(self, tenant: str, repo: str) -> str:
        return f"intake:{tenant}:{repo}:{self.source}:{self.id[1:]}"

    def as_finding(self) -> Finding:
        major = self.source == "check" or self.review_state == "CHANGES_REQUESTED"
        first_line = next(iter(self.body.strip().splitlines()), "")
        return Finding(
            id=self.id,
            severity="major" if major else "minor",
            kind="bug",
            path=self.path or "",
            line=self.line,
            title=first_line[:_TITLE_MAX],
            detail=self.body,
        )


@dataclass(frozen=True)
class TrackedPull:
    task: TaskRecord
    number: int
    url: str
    opened_at: datetime


def tracked_pulls(store: TaskStore, tenant: str, repo: str) -> list[TrackedPull]:
    states = [s for s in TaskState if s not in _TERMINAL]
    found = []
    for task in store.list_tasks(tenant, states):
        pr = store.latest_artifact(task.id, ArtifactKind.PR)
        target = store.latest_artifact(task.id, ArtifactKind.TARGET)
        if pr is None or target is None or target.body.get("repo") != repo:
            continue
        number, url = pr.body.get("number"), pr.body.get("url")
        if not isinstance(number, int) or not isinstance(url, str):
            continue
        found.append(TrackedPull(task, number, url, pr.created_at))
    return found


def counts(f: PRFinding, team_bots: Collection[str]) -> bool:
    """Only team humans and allow-listed review bots can raise a finding."""
    if f.author in team_bots:
        return True
    return f.association in TEAM_ASSOCIATIONS and not f.is_bot


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _comment_finding(c: GhReviewComment) -> PRFinding:
    body = c.body[:MAX_BODY]
    return PRFinding(
        id=f"c{c.id}",
        source="comment",
        author=c.author.login,
        is_bot=c.author.is_bot,
        association=c.association,
        path=c.path,
        line=c.line if c.line is not None else c.original_line,
        body=body,
        url=c.url,
        created_at=c.created_at,
        fingerprint=fingerprint(c.path, body),
    )


def collect_findings(
    gh: GitHub,
    repo: str,
    tp: TrackedPull,
    since: datetime,
    comments_by_pr: dict[int, list[GhReviewComment]],
    cfg: IntakeConfig,
    head_sha: str,
    *,
    store: TaskStore,
) -> list[PRFinding]:
    """New findings on one PR. Markers are set by the caller once they are stored."""
    found = [
        _comment_finding(c) for c in comments_by_pr.get(tp.number, []) if c.body.strip()
    ]
    for review in gh.pull_reviews(repo, tp.number):
        if (
            review.state in ("DISMISSED", "APPROVED")
            or not review.body.strip()
            or review.submitted_at is None
            or _aware(review.submitted_at) < _aware(since)
        ):
            continue
        body = review.body[:MAX_BODY]
        found.append(
            PRFinding(
                id=f"r{review.id}",
                source="review",
                author=review.author.login,
                is_bot=review.author.is_bot,
                association=review.association,
                body=body,
                url=review.url,
                created_at=review.submitted_at,
                fingerprint=fingerprint(None, body),
                review_state=review.state,
            )
        )
    found = [f for f in found if counts(f, cfg.review_bots)]
    for check in gh.check_runs(repo, head_sha):
        if (
            check.status != "completed"
            or check.conclusion not in cfg.failing_conclusions
        ):
            continue
        found.append(
            PRFinding(
                id=f"k{check.id}",
                source="check",
                author="ci",
                is_bot=False,
                association="CI",
                body=f"CI check {check.name}: {check.conclusion}"[:MAX_BODY],
                url=check.url,
                created_at=check.completed_at or since,
                fingerprint=_check_fingerprint(check.name),
            )
        )
    fresh = [
        f for f in found if store.get_meta(f.marker_key(tp.task.tenant, repo)) is None
    ]
    return sorted(fresh, key=lambda f: (_aware(f.created_at), f.id))
