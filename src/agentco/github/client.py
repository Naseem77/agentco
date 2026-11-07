import inspect
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agentco.github.errors import GitHubError
from agentco.github.models import (
    GhCheckRun,
    GhIssue,
    GhPull,
    GhReview,
    GhReviewComment,
    GhUser,
    PullRef,
    linked_issues,
    parse_check_run,
    parse_issue,
    parse_pull,
    parse_review,
    parse_review_comment,
)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GH_MISSING = (
    "gh CLI not found; install it from https://cli.github.com and run gh auth login"
)


def validate_repo(repo: str) -> str:
    if not REPO_RE.match(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ValueError(f"repo must look like owner/name, got {repo!r}")
    return repo


class GitHub(Protocol):
    def issues(self, repo: str, since: datetime | None = None) -> list[GhIssue]: ...

    def pulls(self, repo: str) -> list[GhPull]: ...

    def review_comments(
        self, repo: str, since: datetime | None = None
    ) -> list[GhReviewComment]: ...

    def pull_files(self, repo: str, number: int) -> list[str]: ...

    def get_pull(self, repo: str, number: int) -> GhPull: ...

    def pull_reviews(self, repo: str, number: int) -> list[GhReview]: ...

    def check_runs(self, repo: str, sha: str) -> list[GhCheckRun]: ...


class GitHubWriter(Protocol):
    def create_pull(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef: ...

    def find_open_pull(self, repo: str, head: str) -> PullRef | None: ...

    def edit_pull(self, repo: str, number: int, body: str) -> None: ...


Runner = Callable[[list[str], str | None], str]
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PULL_URL_RE = re.compile(r"^https?://\S+/pull/(\d+)$")


def _subprocess_runner(argv: list[str], input: str | None = None) -> str:
    try:
        stdin = {} if input is None else {"input": input}
        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False, **stdin
        )
    except FileNotFoundError as e:
        raise GitHubError(_GH_MISSING) from e
    if proc.returncode != 0:
        raise GitHubError(f"gh api failed: {proc.stderr.strip()}")
    return proc.stdout


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _since_param(since: datetime) -> str:
    return _utc(since).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(output: str) -> object:
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        raise GitHubError(f"gh api returned invalid JSON: {e}") from e


def _flatten_pages(output: str) -> list[dict]:
    pages = _load_json(output)
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        raise GitHubError("gh api returned unexpected JSON: expected a list of pages")
    return [item for page in pages for item in page]


def _flatten_key(output: str, key: str) -> list[dict]:
    """Flatten `--slurp` pages that are objects holding their items under `key`."""
    pages = _load_json(output)
    if not isinstance(pages, list) or not all(
        isinstance(p, dict) and isinstance(p.get(key), list) for p in pages
    ):
        raise GitHubError(
            f"gh api returned unexpected JSON: expected a list of objects with a {key!r} list"
        )
    return [item for page in pages for item in page[key]]


def _validate_ref(ref: str) -> str:
    if not _REF_RE.match(ref) or ref in (".", ".."):
        raise ValueError(f"commit ref must be a plain sha, got {ref!r}")
    return ref


def _filename(raw: dict) -> str:
    try:
        return raw["filename"]
    except (KeyError, TypeError, IndexError) as e:
        raise GitHubError(f"malformed GitHub pull file: {type(e).__name__}: {e}") from e


def _accepts_input(runner: Callable[..., str]) -> bool:
    params = inspect.signature(runner).parameters.values()
    return any(p.kind is p.VAR_POSITIONAL for p in params) or (
        sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params) >= 2
    )


def _pull_ref(raw: dict) -> PullRef:
    try:
        return PullRef(number=raw["number"], url=raw["url"])
    except (KeyError, TypeError, ValueError) as e:
        raise GitHubError(
            f"malformed GitHub pull request: {type(e).__name__}: {e}"
        ) from e


def _read_only(runner: Callable[..., str]) -> Runner:
    def run(argv: list[str], input: str | None = None) -> str:
        if input is not None:
            raise ValueError("this runner takes no stdin, so it cannot serve writes")
        return runner(argv)

    return run


class GhCli:
    def __init__(self, runner: Callable[..., str] | None = None) -> None:
        if runner is None:
            self._runner: Runner = _subprocess_runner
        elif _accepts_input(runner):
            self._runner = runner
        else:
            self._runner = _read_only(runner)

    def _get(self, path: str) -> list[dict]:
        return _flatten_pages(self._paginated(path))

    def _paginated(self, path: str) -> str:
        return self._runner(["gh", "api", "--paginate", "--slurp", path], None)

    def issues(self, repo: str, since: datetime | None = None) -> list[GhIssue]:
        path = f"repos/{validate_repo(repo)}/issues?state=all&per_page=100&sort=created&direction=asc"
        if since is not None:
            path += f"&since={_since_param(since)}"
        return [i for raw in self._get(path) if (i := parse_issue(raw))]

    def pulls(self, repo: str) -> list[GhPull]:
        path = f"repos/{validate_repo(repo)}/pulls?state=all&per_page=100&sort=created&direction=asc"
        return [parse_pull(raw) for raw in self._get(path)]

    def review_comments(
        self, repo: str, since: datetime | None = None
    ) -> list[GhReviewComment]:
        path = f"repos/{validate_repo(repo)}/pulls/comments?per_page=100&sort=created&direction=asc"
        if since is not None:
            path += f"&since={_since_param(since)}"
        return [parse_review_comment(raw) for raw in self._get(path)]

    def pull_files(self, repo: str, number: int) -> list[str]:
        path = f"repos/{validate_repo(repo)}/pulls/{number}/files?per_page=100"
        return [_filename(raw) for raw in self._get(path)]

    def get_pull(self, repo: str, number: int) -> GhPull:
        path = f"repos/{validate_repo(repo)}/pulls/{int(number)}"
        raw = _load_json(self._runner(["gh", "api", path], None))
        if not isinstance(raw, dict):
            raise GitHubError("gh api returned unexpected JSON: expected an object")
        return parse_pull(raw)

    def pull_reviews(self, repo: str, number: int) -> list[GhReview]:
        path = f"repos/{validate_repo(repo)}/pulls/{int(number)}/reviews"
        return [parse_review(raw, int(number)) for raw in self._get(path)]

    def check_runs(self, repo: str, sha: str) -> list[GhCheckRun]:
        path = f"repos/{validate_repo(repo)}/commits/{_validate_ref(sha)}/check-runs"
        return [
            parse_check_run(raw)
            for raw in _flatten_key(self._paginated(path), "check_runs")
        ]

    def create_pull(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        argv = [
            "gh", "pr", "create", "--repo", validate_repo(repo),
            "--head", head, "--base", base, "--title", title, "--body-file", "-",
        ]  # fmt: skip
        lines = self._runner(argv, body).split()
        match = _PULL_URL_RE.match(lines[-1]) if lines else None
        if match is None:
            raise GitHubError("gh pr create did not print a pull request URL")
        return PullRef(number=int(match.group(1)), url=lines[-1])

    def find_open_pull(self, repo: str, head: str) -> PullRef | None:
        argv = [
            "gh", "pr", "list", "--repo", validate_repo(repo), "--head", head,
            "--state", "open", "--json", "number,url",
        ]  # fmt: skip
        try:
            found = json.loads(self._runner(argv, None))
        except json.JSONDecodeError as e:
            raise GitHubError(f"gh pr list returned invalid JSON: {e}") from e
        if not isinstance(found, list):
            raise GitHubError("gh pr list returned unexpected JSON: expected a list")
        return _pull_ref(found[0]) if found else None

    def edit_pull(self, repo: str, number: int, body: str) -> None:
        argv = [
            "gh", "pr", "edit", str(int(number)), "--repo", validate_repo(repo),
            "--body-file", "-",
        ]  # fmt: skip
        self._runner(argv, body)


class FakeGitHub:
    """In-memory GitHub for tests; filters on `since` like the API does."""

    def __init__(
        self,
        issues: list[GhIssue],
        pulls: list[GhPull],
        comments: list[GhReviewComment],
        files: dict[int, list[str]],
        fail_writes: bool = False,
        reviews: dict[int, list[GhReview]] | None = None,
        check_runs: dict[str, list[GhCheckRun]] | None = None,
    ) -> None:
        self._issues = issues
        self._pulls = pulls
        self._comments = comments
        self._files = files
        self._reviews = {n: list(rs) for n, rs in (reviews or {}).items()}
        self._checks = {sha: list(rs) for sha, rs in (check_runs or {}).items()}
        self.calls: list[tuple[str, object]] = []
        self.fail_writes = fail_writes
        self.created: list[dict] = []
        self.edited: list[dict] = []

    @classmethod
    def from_fixtures(cls, directory: Path) -> "FakeGitHub":
        def load(name: str) -> list[dict]:
            return _flatten_pages((directory / name).read_text())

        issues = [i for raw in load("issues.json") if (i := parse_issue(raw))]
        files = {
            int(path.stem.removeprefix("pull_files_")): [
                _filename(raw) for raw in load(path.name)
            ]
            for path in sorted(directory.glob("pull_files_*.json"))
        }
        return cls(
            issues,
            [parse_pull(raw) for raw in load("pulls.json")],
            [parse_review_comment(raw) for raw in load("pull_comments.json")],
            files,
        )

    def issues(self, repo: str, since: datetime | None = None) -> list[GhIssue]:
        validate_repo(repo)
        self.calls.append(("issues", since))
        return [i for i in self._issues if _updated_since(i.updated_at, since)]

    def pulls(self, repo: str) -> list[GhPull]:
        validate_repo(repo)
        self.calls.append(("pulls", None))
        return list(self._pulls)

    def review_comments(
        self, repo: str, since: datetime | None = None
    ) -> list[GhReviewComment]:
        validate_repo(repo)
        self.calls.append(("review_comments", since))
        return [c for c in self._comments if _updated_since(c.updated_at, since)]

    def pull_files(self, repo: str, number: int) -> list[str]:
        validate_repo(repo)
        self.calls.append(("pull_files", number))
        return list(self._files.get(number, []))

    def get_pull(self, repo: str, number: int) -> GhPull:
        validate_repo(repo)
        self.calls.append(("get_pull", number))
        for pull in self._pulls:
            if pull.number == number:
                return pull
        raise GitHubError(f"pull request #{number} not found")

    def pull_reviews(self, repo: str, number: int) -> list[GhReview]:
        validate_repo(repo)
        self.calls.append(("pull_reviews", number))
        return list(self._reviews.get(number, []))

    def check_runs(self, repo: str, sha: str) -> list[GhCheckRun]:
        validate_repo(repo)
        self.calls.append(("check_runs", sha))
        return list(self._checks.get(sha, []))

    def upsert_pull(self, pull: GhPull) -> None:
        self._pulls = [p for p in self._pulls if p.number != pull.number] + [pull]
        self._pulls.sort(key=lambda p: p.number)

    def add_comment(self, comment: GhReviewComment) -> None:
        self._comments.append(comment)

    def add_review(self, review: GhReview) -> None:
        self._reviews.setdefault(review.pr_number, []).append(review)

    def set_checks(self, sha: str, runs: list[GhCheckRun]) -> None:
        self._checks[sha] = list(runs)

    def create_pull(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRef:
        validate_repo(repo)
        self._check_writable()
        number = max([p.number for p in self._pulls], default=0) + 1
        url = f"https://github.com/{repo}/pull/{number}"
        self.created.append(
            {
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "number": number,
                "url": url,
            }
        )
        now = datetime.now(UTC)
        self.upsert_pull(
            GhPull(
                number=number,
                title=title,
                body=body,
                author=GhUser("agentco", False),
                state="open",
                merged_at=None,
                created_at=now,
                updated_at=now,
                base_sha="fake-base",
                head_sha="fake-sha",
                labels=(),
                url=url,
                closes_issues=linked_issues(body),
            )
        )
        return PullRef(number=number, url=url)

    def find_open_pull(self, repo: str, head: str) -> PullRef | None:
        validate_repo(repo)
        for pull in self.created:
            if pull["repo"] != repo or pull["head"] != head:
                continue
            current = next((p for p in self._pulls if p.number == pull["number"]), None)
            if current is not None and (
                current.state != "open" or current.merged_at is not None
            ):
                continue
            return PullRef(number=pull["number"], url=pull["url"])
        return None

    def edit_pull(self, repo: str, number: int, body: str) -> None:
        validate_repo(repo)
        self._check_writable()
        self.edited.append({"repo": repo, "number": number, "body": body})

    def _check_writable(self) -> None:
        if self.fail_writes:
            raise GitHubError("gh write failed: simulated failure")


def _updated_since(updated_at: datetime, since: datetime | None) -> bool:
    return since is None or _utc(updated_at) >= _utc(since)
