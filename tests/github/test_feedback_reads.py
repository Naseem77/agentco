import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agentco.github.client import FakeGitHub, GhCli
from agentco.github.errors import GitHubError
from agentco.github.models import (
    GhReview,
    GhReviewComment,
    GhUser,
    parse_check_run,
    parse_pull,
    parse_review,
)

NOW = datetime(2026, 1, 9, 12, 0, tzinfo=UTC)

PULL_JSON = {
    "number": 12,
    "title": "Fix it",
    "body": "Fixes #3",
    "user": {"login": "agentco", "type": "User"},
    "state": "open",
    "merged_at": None,
    "labels": [],
    "created_at": "2026-01-09T09:00:00Z",
    "updated_at": "2026-01-09T10:00:00Z",
    "base": {"sha": "b1"},
    "head": {"sha": "h1"},
    "html_url": "https://github.com/acme/app/pull/12",
}
REVIEW_JSON = {
    "id": 77,
    "user": {"login": "alice", "type": "User"},
    "author_association": "MEMBER",
    "state": "CHANGES_REQUESTED",
    "body": "Please add a test",
    "submitted_at": "2026-01-09T11:00:00Z",
    "html_url": "https://github.com/acme/app/pull/12#pullrequestreview-77",
}
CHECK_JSON = {
    "id": 9,
    "name": "tests",
    "head_sha": "abc",
    "status": "completed",
    "conclusion": "failure",
    "html_url": "https://github.com/acme/app/runs/9",
    "completed_at": "2026-01-09T11:30:00Z",
}


def runner_returning(payload):
    seen = []

    def run(argv, input=None):
        seen.append(argv)
        return json.dumps(payload)

    return run, seen


def make_comment(id=1, pr_number=1):
    return GhReviewComment(
        id=id,
        pr_number=pr_number,
        author=GhUser("alice", False),
        association="MEMBER",
        body="nit",
        path="a.py",
        line=3,
        original_line=3,
        side="RIGHT",
        subject_type="line",
        commit_id="h1",
        original_commit_id="h1",
        diff_hunk="",
        in_reply_to=None,
        created_at=NOW,
        updated_at=NOW,
        url="https://github.com/acme/app/pull/1#discussion_r1",
    )


def make_review(id=1, pr_number=1):
    return parse_review({**REVIEW_JSON, "id": id}, pr_number)


def test_get_pull_single_object_no_paginate():
    run, seen = runner_returning(PULL_JSON)
    pull = GhCli(runner=run).get_pull("acme/app", 12)
    assert (pull.number, pull.head_sha, pull.closes_issues) == (12, "h1", (3,))
    assert seen == [["gh", "api", "repos/acme/app/pulls/12"]]


def test_get_pull_non_object_is_github_error():
    run, _ = runner_returning([PULL_JSON])
    with pytest.raises(GitHubError) as err:
        GhCli(runner=run).get_pull("acme/app", 12)
    assert str(err.value) == "gh api returned unexpected JSON: expected an object"


def test_get_pull_invalid_json_is_github_error():
    cli = GhCli(runner=lambda argv, input=None: "not json")
    with pytest.raises(GitHubError) as err:
        cli.get_pull("acme/app", 12)
    assert str(err.value).startswith("gh api returned invalid JSON")


def test_get_pull_malformed_is_github_error():
    run, _ = runner_returning({"number": 12})
    with pytest.raises(GitHubError) as err:
        GhCli(runner=run).get_pull("acme/app", 12)
    assert str(err.value).startswith("malformed GitHub pull request: KeyError")


def test_pull_reviews_argv_and_parse():
    run, seen = runner_returning([[REVIEW_JSON]])
    [review] = GhCli(runner=run).pull_reviews("acme/app", 12)
    assert seen == [
        ["gh", "api", "--paginate", "--slurp", "repos/acme/app/pulls/12/reviews"]
    ]
    assert review == GhReview(
        id=77,
        pr_number=12,
        author=GhUser("alice", False),
        association="MEMBER",
        state="CHANGES_REQUESTED",
        body="Please add a test",
        submitted_at=datetime(2026, 1, 9, 11, 0, tzinfo=UTC),
        url="https://github.com/acme/app/pull/12#pullrequestreview-77",
    )


def test_pull_reviews_parses_ghost_author_and_null_body():
    raw = {**REVIEW_JSON, "user": None, "body": None, "submitted_at": None}
    del raw["author_association"]
    run, _ = runner_returning([[raw]])
    [review] = GhCli(runner=run).pull_reviews("acme/app", 12)
    assert review.author == GhUser("ghost", False)
    assert (review.body, review.submitted_at, review.association) == ("", None, "NONE")


def test_pull_reviews_malformed_item_is_github_error():
    run, _ = runner_returning([[{"id": 1}]])
    with pytest.raises(GitHubError) as err:
        GhCli(runner=run).pull_reviews("acme/app", 12)
    assert str(err.value).startswith("malformed GitHub review: KeyError")


def test_check_runs_flattens_object_pages():
    run, seen = runner_returning(
        [
            {"total_count": 2, "check_runs": [CHECK_JSON]},
            {
                "total_count": 2,
                "check_runs": [{**CHECK_JSON, "id": 10, "name": "lint"}],
            },
        ]
    )
    runs = GhCli(runner=run).check_runs("acme/app", "abc")
    assert [(c.id, c.name, c.conclusion) for c in runs] == [
        (9, "tests", "failure"),
        (10, "lint", "failure"),
    ]
    assert seen == [
        ["gh", "api", "--paginate", "--slurp", "repos/acme/app/commits/abc/check-runs"]
    ]


@pytest.mark.parametrize(
    "payload",
    [[[CHECK_JSON]], [{"total_count": 0}], {"check_runs": []}, [{"check_runs": {}}]],
)
def test_check_runs_bad_page_is_github_error(payload):
    run, _ = runner_returning(payload)
    with pytest.raises(GitHubError):
        GhCli(runner=run).check_runs("acme/app", "abc")


def test_check_run_in_progress_has_no_conclusion():
    raw = {**CHECK_JSON, "status": "in_progress", "conclusion": None}
    del raw["completed_at"]
    check = parse_check_run(raw)
    assert (check.status, check.conclusion, check.completed_at) == (
        "in_progress",
        None,
        None,
    )


def test_malformed_check_run_is_github_error():
    with pytest.raises(GitHubError) as err:
        parse_check_run({"id": 1})
    assert str(err.value).startswith("malformed GitHub check run: KeyError")


@pytest.mark.parametrize("sha", ["", "../x", "a/b", "a b", "..", "a?b"])
def test_check_runs_rejects_unsafe_sha_before_running(sha):
    run, seen = runner_returning([])
    with pytest.raises(ValueError):
        GhCli(runner=run).check_runs("acme/app", sha)
    assert seen == []


def test_feedback_reads_validate_repo_before_running():
    run, seen = runner_returning({})
    cli = GhCli(runner=run)
    for call in (
        lambda: cli.get_pull("../x", 1),
        lambda: cli.pull_reviews("../x", 1),
        lambda: cli.check_runs("../x", "abc"),
    ):
        with pytest.raises(ValueError):
            call()
    assert seen == []


def test_fake_created_pull_is_readable_and_mutable():
    gh = FakeGitHub([], [], [], {})
    ref = gh.create_pull("acme/app", "agentco/task-1", "main", "t", "b")
    pull = gh.get_pull("acme/app", ref.number)
    assert (pull.state, pull.title, pull.body, pull.head_sha, pull.url) == (
        "open",
        "t",
        "b",
        "fake-sha",
        ref.url,
    )
    gh.upsert_pull(replace(pull, state="closed", merged_at=NOW))
    assert gh.get_pull("acme/app", ref.number).merged_at == NOW
    assert [p.number for p in gh.pulls("acme/app")] == [ref.number]


def test_fake_get_pull_unknown_number_is_github_error():
    with pytest.raises(GitHubError) as err:
        FakeGitHub([], [], [], {}).get_pull("acme/app", 99)
    assert str(err.value) == "pull request #99 not found"


def test_fake_upsert_pull_replaces_by_number():
    gh = FakeGitHub([], [parse_pull(PULL_JSON)], [], {})
    gh.upsert_pull(replace(gh.get_pull("acme/app", 12), title="New"))
    assert [(p.number, p.title) for p in gh.pulls("acme/app")] == [(12, "New")]


def test_fake_reviews_checks_and_comments_mutators_and_call_log():
    gh = FakeGitHub([], [], [], {})
    gh.add_review(make_review(1, pr_number=4))
    gh.add_review(make_review(2, pr_number=4))
    gh.add_review(make_review(3, pr_number=5))
    gh.add_comment(make_comment(8, pr_number=4))
    check = parse_check_run(CHECK_JSON)
    gh.set_checks("abc", [check])
    assert [r.id for r in gh.pull_reviews("acme/app", 4)] == [1, 2]
    assert gh.pull_reviews("acme/app", 6) == []
    assert gh.check_runs("acme/app", "abc") == [check]
    assert gh.check_runs("acme/app", "zzz") == []
    assert [c.id for c in gh.review_comments("acme/app")] == [8]
    assert gh.calls == [
        ("pull_reviews", 4),
        ("pull_reviews", 6),
        ("check_runs", "abc"),
        ("check_runs", "zzz"),
        ("review_comments", None),
    ]


def test_fake_constructor_seeds_reviews_and_checks():
    review = make_review(1, pr_number=4)
    check = parse_check_run(CHECK_JSON)
    gh = FakeGitHub([], [], [], {}, reviews={4: [review]}, check_runs={"abc": [check]})
    assert gh.pull_reviews("acme/app", 4) == [review]
    assert gh.check_runs("acme/app", "abc") == [check]


def test_fake_get_pull_is_logged():
    gh = FakeGitHub([], [parse_pull(PULL_JSON)], [], {})
    gh.get_pull("acme/app", 12)
    assert gh.calls == [("get_pull", 12)]


def test_fake_reads_do_not_share_internal_lists():
    gh = FakeGitHub([], [], [], {})
    gh.add_review(make_review(1, pr_number=4))
    gh.pull_reviews("acme/app", 4).clear()
    assert [r.id for r in gh.pull_reviews("acme/app", 4)] == [1]


def test_fake_create_pull_numbers_after_seeded_pulls():
    gh = FakeGitHub([], [parse_pull(PULL_JSON)], [], {})
    ref = gh.create_pull("acme/app", "agentco/task-1", "main", "t", "b")
    assert ref.number == 13
    assert [p.number for p in gh.pulls("acme/app")] == [12, 13]
