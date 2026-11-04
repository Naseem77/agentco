import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentco.github.client import FakeGitHub, GhCli, GitHubError, validate_repo

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = {p.name: p.read_text() for p in FIXTURE_DIR.glob("*.json")}


def raising(exc):
    def run(*args, **kwargs):
        raise exc

    return run


def completed(returncode=0, stdout="", stderr=""):
    def run(argv, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run


@pytest.mark.parametrize(
    "bad", ["", "acme", "acme/web/x", "a b/c", "../x", "a/..", "./x", "a/b;rm"]
)
def test_validate_repo_rejects(bad):
    with pytest.raises(ValueError):
        validate_repo(bad)


@pytest.mark.parametrize("good", ["acme-org/web", "o/r", "a.b/c_d-e", "acme/.github"])
def test_validate_repo_accepts(good):
    assert validate_repo(good) == good


def test_gh_cli_argv_and_page_flattening():
    seen = []

    def runner(argv):
        seen.append(argv)
        return FIXTURES["issues.json"]

    issues = GhCli(runner).issues(
        "acme-org/web", since=datetime(2026, 1, 5, tzinfo=UTC)
    )
    assert seen == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/acme-org/web/issues?state=all&per_page=100&sort=created&direction=asc&since=2026-01-05T00:00:00Z",
        ]
    ]
    assert [i.number for i in issues] == [1, 2, 4]


def test_gh_cli_issues_without_since_has_no_since_param():
    seen = []
    GhCli(lambda argv: seen.append(argv[-1]) or "[[]]").issues("o/r")
    assert seen == [
        "repos/o/r/issues?state=all&per_page=100&sort=created&direction=asc"
    ]


def test_gh_cli_since_is_rendered_in_utc():
    seen = []
    cli = GhCli(lambda argv: seen.append(argv[-1]) or "[[]]")
    cli.review_comments(
        "o/r", since=datetime(2026, 1, 5, 2, 30, tzinfo=timezone(timedelta(hours=2)))
    )
    assert seen == [
        "repos/o/r/pulls/comments?per_page=100&sort=created&direction=asc&since=2026-01-05T00:30:00Z"
    ]


def test_gh_cli_other_paths():
    seen = []
    cli = GhCli(lambda argv: seen.append(argv[-1]) or "[[]]")
    cli.pulls("o/r")
    cli.review_comments("o/r")
    cli.pull_files("o/r", 3)
    assert seen == [
        "repos/o/r/pulls?state=all&per_page=100&sort=created&direction=asc",
        "repos/o/r/pulls/comments?per_page=100&sort=created&direction=asc",
        "repos/o/r/pulls/3/files?per_page=100",
    ]


def test_gh_cli_parses_pulls_comments_and_files():
    outputs = {
        "pulls?": FIXTURES["pulls.json"],
        "comments?": FIXTURES["pull_comments.json"],
        "files?": FIXTURES["pull_files_3.json"],
    }

    def runner(argv):
        return next(v for k, v in outputs.items() if k in argv[-1])

    cli = GhCli(runner)
    assert [p.number for p in cli.pulls("acme-org/web")] == [5, 3]
    assert [c.id for c in cli.review_comments("acme-org/web")] == [
        101,
        102,
        103,
        104,
        105,
        106,
        107,
    ]
    assert cli.pull_files("acme-org/web", 3) == ["src/app/graph.py", "README.md"]


def test_gh_cli_validates_repo_before_running():
    seen = []
    cli = GhCli(lambda argv: seen.append(argv) or "[[]]")
    for call in (cli.issues, cli.pulls, cli.review_comments):
        with pytest.raises(ValueError):
            call("../x")
    with pytest.raises(ValueError):
        cli.pull_files("../x", 1)
    assert seen == []


def test_gh_cli_invalid_json_is_github_error():
    with pytest.raises(GitHubError, match=r"^gh api returned invalid JSON: "):
        GhCli(lambda argv: "not json").pulls("o/r")


@pytest.mark.parametrize("output", ['{"message": "Not Found"}', "[1]", '[{"a": 1}]'])
def test_gh_cli_unexpected_json_shape_is_github_error(output):
    with pytest.raises(GitHubError) as err:
        GhCli(lambda argv: output).pulls("o/r")
    assert str(err.value) == "gh api returned unexpected JSON: expected a list of pages"


def test_gh_cli_pull_files_without_filename_is_github_error():
    with pytest.raises(GitHubError) as err:
        GhCli(lambda argv: '[[{"name": "x"}]]').pull_files("o/r", 3)
    assert str(err.value) == "malformed GitHub pull file: KeyError: 'filename'"


def test_gh_cli_malformed_item_is_github_error():
    with pytest.raises(GitHubError) as err:
        GhCli(lambda argv: '[[{"number": 1}]]').pulls("o/r")
    assert str(err.value) == "malformed GitHub pull request: KeyError: 'state'"


def test_gh_missing_is_github_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", raising(FileNotFoundError("gh")))
    with pytest.raises(GitHubError) as err:
        GhCli().pulls("o/r")
    assert str(err.value) == (
        "gh CLI not found; install it from https://cli.github.com and run gh auth login"
    )


def test_gh_failure_carries_stderr(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", completed(returncode=1, stderr="HTTP 404: Not Found\n")
    )
    with pytest.raises(GitHubError) as err:
        GhCli().pulls("o/missing")
    assert str(err.value) == "gh api failed: HTTP 404: Not Found"


def test_default_runner_passes_argv_and_returns_stdout(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[[]]", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert GhCli().pulls("o/r") == []
    assert seen == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/o/r/pulls?state=all&per_page=100&sort=created&direction=asc",
        ]
    ]


def test_fake_filters_since_and_records_calls():
    gh = FakeGitHub.from_fixtures(FIXTURE_DIR)
    assert [
        i.number
        for i in gh.issues("acme-org/web", since=datetime(2026, 1, 5, tzinfo=UTC))
    ] == [2]
    assert gh.calls[-1] == ("issues", datetime(2026, 1, 5, tzinfo=UTC))


def test_fake_from_fixtures_content_and_call_log():
    gh = FakeGitHub.from_fixtures(FIXTURE_DIR)
    assert [i.number for i in gh.issues("acme-org/web")] == [1, 2, 4]
    assert [p.number for p in gh.pulls("acme-org/web")] == [5, 3]
    assert [c.id for c in gh.review_comments("acme-org/web")] == [
        101,
        102,
        103,
        104,
        105,
        106,
        107,
    ]
    assert [
        c.id
        for c in gh.review_comments(
            "acme-org/web", since=datetime(2026, 1, 5, tzinfo=UTC)
        )
    ] == [
        106,
        107,
    ]
    assert gh.pull_files("acme-org/web", 3) == ["src/app/graph.py", "README.md"]
    assert gh.pull_files("acme-org/web", 5) == []
    assert gh.calls == [
        ("issues", None),
        ("pulls", None),
        ("review_comments", None),
        ("review_comments", datetime(2026, 1, 5, tzinfo=UTC)),
        ("pull_files", 3),
        ("pull_files", 5),
    ]


def test_fixture_files_are_lists_of_pages():
    for name, text in FIXTURES.items():
        pages = json.loads(text)
        assert all(isinstance(page, list) for page in pages), name
