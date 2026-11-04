import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentco.github.errors import GitHubError
from agentco.github.models import (
    GhIssue,
    GhPull,
    GhUser,
    linked_issues,
    parse_issue,
    parse_pull,
    parse_review_comment,
    parse_user,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load(name: str) -> list[list[dict]]:
    return json.loads((FIXTURE_DIR / name).read_text())


def comment(comment_id: int) -> dict:
    return next(
        c for page in load("pull_comments.json") for c in page if c["id"] == comment_id
    )


def raw_issue(**overrides) -> dict:
    raw = {
        "number": 9,
        "title": "t",
        "body": "b",
        "user": {"login": "alice", "type": "User"},
        "state": "open",
        "state_reason": None,
        "labels": [],
        "created_at": "2026-01-01T10:00:00Z",
        "updated_at": "2026-01-01T10:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/o/r/issues/9",
    }
    return raw | overrides


def test_bot_detection():
    assert parse_user({"login": "dependabot[bot]", "type": "Bot"}).is_bot
    assert parse_user({"login": "renovate[bot]", "type": "User"}).is_bot
    assert parse_user({"login": "robot-lover", "type": "Bot"}).is_bot
    assert parse_user({"login": "alice", "type": "User"}) == GhUser("alice", False)
    assert parse_user(None) == GhUser("ghost", False)


def test_issue_endpoint_pull_requests_are_not_issues():
    parsed = [parse_issue(r) for page in load("issues.json") for r in page]
    assert [i.number if i else None for i in parsed] == [1, 2, None, 4]


def test_issue_fixture_fields():
    issues = {
        i.number: i
        for page in load("issues.json")
        for r in page
        if (i := parse_issue(r))
    }
    assert issues[1] == GhIssue(
        number=1,
        title="Graph switch flickers",
        body="Switching graphs flashes the old one for a frame.",
        author=GhUser("alice", False),
        state="closed",
        state_reason="completed",
        labels=("bug",),
        created_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        closed_at=datetime(2026, 1, 3, 12, tzinfo=UTC),
        url="https://github.com/acme-org/web/issues/1",
    )
    assert (issues[2].author, issues[2].state, issues[2].labels) == (
        GhUser("bob", False),
        "open",
        ("enhancement",),
    )
    assert issues[4].author == GhUser("dependabot[bot]", True)


def test_null_body_and_dates():
    i = parse_issue(raw_issue(body=None, title=None, created_at="2026-01-01T10:00:00Z"))
    assert i.body == "" and i.title == ""
    assert i.created_at == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert i.closed_at is None and i.state_reason is None


def test_pull_fixture_fields():
    pulls = {p.number: p for page in load("pulls.json") for p in map(parse_pull, page)}
    assert pulls[3] == GhPull(
        number=3,
        title="Debounce graph switch",
        body="Fixes #1",
        author=GhUser("carol", False),
        state="closed",
        merged_at=datetime(2026, 1, 3, 12, tzinfo=UTC),
        created_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
        updated_at=datetime(2026, 1, 3, 9, tzinfo=UTC),
        base_sha="b1",
        head_sha="h1",
        labels=(),
        url="https://github.com/acme-org/web/pull/3",
        closes_issues=(1,),
    )
    assert pulls[5].author == GhUser("dependabot[bot]", True)
    assert pulls[5].merged_at is None
    assert pulls[5].closes_issues == ()


def test_review_comment_fields():
    c = parse_review_comment(comment(101))
    assert (c.pr_number, c.path, c.original_line, c.side, c.subject_type) == (
        3,
        "src/app/graph.py",
        6,
        "RIGHT",
        "line",
    )
    assert (c.id, c.author, c.association, c.line) == (
        101,
        GhUser("alice", False),
        "MEMBER",
        6,
    )
    assert (c.commit_id, c.original_commit_id, c.in_reply_to) == ("h1", "h1", None)
    assert c.created_at == datetime(2026, 1, 4, 9, tzinfo=UTC)
    assert c.url == "https://github.com/acme-org/web/pull/3#discussion_r101"
    assert c.diff_hunk.startswith("@@ -1,3 +1,8 @@")
    assert parse_review_comment(comment(102)).in_reply_to == 101
    assert parse_review_comment(comment(107)).in_reply_to == 999
    assert parse_review_comment(comment(103)).author == GhUser(
        "copilot-pull-request-reviewer[bot]", True
    )


def test_file_level_review_comment():
    c = parse_review_comment(comment(106))
    assert (c.subject_type, c.line, c.original_line, c.path) == (
        "file",
        None,
        None,
        "src/app/api.py",
    )


def test_review_comment_defaults_for_missing_side_and_subject_type():
    raw = {k: v for k, v in comment(101).items() if k not in ("side", "subject_type")}
    c = parse_review_comment(raw | {"side": None, "subject_type": None, "body": None})
    assert (c.side, c.subject_type, c.body) == ("RIGHT", "line", "")
    c = parse_review_comment(raw)
    assert (c.side, c.subject_type) == ("RIGHT", "line")


def test_linked_issues():
    assert linked_issues("Fixes #1, closes #12 and resolved #7; see #9") == (1, 12, 7)


def test_linked_issues_dedupes_keeps_order_and_ignores_non_keywords():
    assert linked_issues("fixed #4. CLOSE #2\nResolves #4, prefixes #8, Fix #3") == (
        4,
        2,
        3,
    )
    assert linked_issues("") == ()


def _without(raw: dict, key: str) -> dict:
    return {k: v for k, v in raw.items() if k != key}


def _pull_raw() -> dict:
    return load("pulls.json")[0][0]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_without(raw_issue(), "number"), "malformed GitHub issue: KeyError: 'number'"),
        (
            raw_issue(user="alice"),
            "malformed GitHub issue: TypeError: string indices must be integers, not 'str'",
        ),
        (
            raw_issue(created_at="yesterday"),
            "malformed GitHub issue: ValueError: Invalid isoformat string: 'yesterday'",
        ),
        (
            raw_issue(labels=[{"id": 1}]),
            "malformed GitHub issue: KeyError: 'name'",
        ),
        (
            None,
            "malformed GitHub issue: TypeError: argument of type 'NoneType' is not iterable",
        ),
    ],
)
def test_parse_issue_wraps_malformed_json(raw, message):
    with pytest.raises(GitHubError) as err:
        parse_issue(raw)
    assert str(err.value) == message


def test_parse_pull_wraps_malformed_json():
    with pytest.raises(GitHubError) as err:
        parse_pull(_without(_pull_raw(), "base"))
    assert str(err.value) == "malformed GitHub pull request: KeyError: 'base'"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            _without(comment(101), "path"),
            "malformed GitHub review comment: KeyError: 'path'",
        ),
        (
            comment(101) | {"pull_request_url": "nope"},
            "malformed GitHub review comment: IndexError: list index out of range",
        ),
    ],
)
def test_parse_review_comment_wraps_malformed_json(raw, message):
    with pytest.raises(GitHubError) as err:
        parse_review_comment(raw)
    assert str(err.value) == message
