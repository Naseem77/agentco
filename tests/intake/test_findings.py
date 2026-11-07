from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentco.agents.replies import Finding
from agentco.config import IntakeConfig, TenantConfig
from agentco.github.client import FakeGitHub
from agentco.github.models import GhCheckRun, GhReview, GhReviewComment, GhUser
from agentco.intake.findings import (
    PRFinding,
    TrackedPull,
    collect_findings,
    counts,
    fingerprint,
    tracked_pulls,
)
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.states import TaskState

REPO = "acme/app"
SINCE = datetime(2026, 1, 9, 0, 0, tzinfo=UTC)
AFTER = SINCE + timedelta(hours=1)
COPILOT = "copilot-pull-request-reviewer[bot]"


def C(id, author, association, body, bot=False, path="a.py", line=3, pr=7, at=AFTER):
    return GhReviewComment(
        id=id,
        pr_number=pr,
        author=GhUser(author, bot),
        association=association,
        body=body,
        path=path,
        line=line,
        original_line=line,
        side="RIGHT",
        subject_type="line",
        commit_id="sha1",
        original_commit_id="sha1",
        diff_hunk="",
        in_reply_to=None,
        created_at=at,
        updated_at=at,
        url=f"https://github.com/acme/app/pull/{pr}#discussion_r{id}",
    )


def R(
    id,
    author="alice",
    association="MEMBER",
    state="COMMENTED",
    body="b",
    at=AFTER,
    pr=7,
):
    return GhReview(
        id=id,
        pr_number=pr,
        author=GhUser(author, author.endswith("[bot]")),
        association=association,
        state=state,
        body=body,
        submitted_at=at,
        url=f"https://github.com/acme/app/pull/{pr}#pullrequestreview-{id}",
    )


def K(id, name="tests", status="completed", conclusion="failure", sha="sha1"):
    return GhCheckRun(
        id=id,
        name=name,
        head_sha=sha,
        status=status,
        conclusion=conclusion,
        url=f"https://github.com/acme/app/runs/{id}",
        completed_at=AFTER,
    )


def make_task(store, repo=REPO, number=7, title="t"):
    t = store.create_task("t", title, "cli")
    store.put_artifact(
        t.id, ArtifactKind.TARGET, Role.HUMAN, {"repo": repo, "main": "main"}
    )
    store.put_artifact(
        t.id,
        ArtifactKind.PR,
        Role.LEAD,
        {"number": number, "url": f"https://github.com/{repo}/pull/{number}"},
    )
    return t


@pytest.fixture
def gh():
    return FakeGitHub([], [], [], {})


@pytest.fixture
def tracked(store):
    t = make_task(store)
    pr = store.latest_artifact(t.id, ArtifactKind.PR)
    return TrackedPull(
        task=t,
        number=7,
        url="https://github.com/acme/app/pull/7",
        opened_at=pr.created_at,
    )


def by_pr(gh):
    out: dict[int, list] = {}
    for c in gh.review_comments(REPO, SINCE):
        out.setdefault(c.pr_number, []).append(c)
    return out


def collect(store, gh, tracked, cfg=None, head_sha="sha1", since=SINCE):
    return collect_findings(
        gh,
        REPO,
        tracked,
        since,
        by_pr(gh),
        cfg or IntakeConfig(),
        head_sha,
        store=store,
    )


def test_team_and_allow_listed_bot_count_strangers_and_other_bots_skipped(
    store, tracked, gh
):
    gh.add_comment(C(1, "alice", "MEMBER", "off-by-one in loop"))
    gh.add_comment(C(2, "rando", "NONE", "use rust"))
    gh.add_comment(C(3, COPILOT, "NONE", "null check missing", bot=True))
    gh.add_comment(C(4, "dependabot[bot]", "NONE", "bump", bot=True))
    assert [f.id for f in collect(store, gh, tracked)] == ["c1", "c3"]


def test_collect_reads_only_comments_of_the_tracked_pr(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "mine", pr=7))
    gh.add_comment(C(2, "alice", "MEMBER", "other pr", pr=8))
    assert [f.id for f in collect(store, gh, tracked)] == ["c1"]


def test_comment_finding_fields(store, tracked, gh):
    gh.add_comment(C(5, "alice", "OWNER", "  Off by one\nsecond line", line=9))
    [f] = collect(store, gh, tracked)
    assert f == PRFinding(
        id="c5",
        source="comment",
        author="alice",
        is_bot=False,
        association="OWNER",
        path="a.py",
        line=9,
        body="  Off by one\nsecond line",
        url="https://github.com/acme/app/pull/7#discussion_r5",
        created_at=AFTER,
        fingerprint=fingerprint("a.py", "  Off by one\nsecond line"),
    )


def test_outdated_comment_falls_back_to_original_line(store, tracked, gh):
    c = C(5, "alice", "MEMBER", "x")
    gh.add_comment(replace(c, line=None, original_line=11))
    assert collect(store, gh, tracked)[0].line == 11


def test_replies_count_like_comments(store, tracked, gh):
    gh.add_comment(replace(C(2, "alice", "MEMBER", "still broken"), in_reply_to=1))
    assert [f.id for f in collect(store, gh, tracked)] == ["c2"]


def test_pr_author_comment_counts(store, tracked, gh):
    gh.add_comment(C(1, "me", "OWNER", "self review note"))
    assert [f.id for f in collect(store, gh, tracked)] == ["c1"]


def test_empty_comment_is_skipped(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "  \n"))
    assert collect(store, gh, tracked) == []


def test_failing_check_is_finding_success_and_running_are_not(store, tracked, gh):
    gh.set_checks(
        "sha1",
        [
            K(1, "tests", conclusion="failure"),
            K(2, "lint", conclusion="success"),
            K(3, "build", status="in_progress", conclusion=None),
            K(4, "slow", conclusion="timed_out"),
            K(5, "skipped", conclusion="skipped"),
            K(6, "neutral", conclusion="neutral"),
        ],
    )
    found = collect(store, gh, tracked)
    assert [f.id for f in found] == ["k1", "k4"]
    f = found[0]
    assert (f.source, f.author, f.is_bot, f.association, f.path, f.line) == (
        "check",
        "ci",
        False,
        "CI",
        None,
        None,
    )
    assert f.url == "https://github.com/acme/app/runs/1"
    assert f.fingerprint == fingerprint(None, "check:tests")


def test_checks_read_for_given_head_sha_only(store, tracked, gh):
    gh.set_checks("old", [K(1, sha="old")])
    gh.set_checks("sha1", [K(2)])
    assert [f.id for f in collect(store, gh, tracked)] == ["k2"]
    assert ("check_runs", "sha1") in gh.calls
    assert ("check_runs", "old") not in gh.calls


def test_failing_conclusions_are_configurable(store, tracked, gh):
    gh.set_checks("sha1", [K(1, conclusion="cancelled"), K(2, conclusion="failure")])
    cfg = IntakeConfig(failing_conclusions=["cancelled"])
    assert [f.id for f in collect(store, gh, tracked, cfg)] == ["k1"]


def test_check_without_completed_at_still_collected(store, tracked, gh):
    gh.set_checks("sha1", [replace_check(K(1), completed_at=None)])
    [f] = collect(store, gh, tracked)
    assert f.created_at == SINCE


def replace_check(check, **update):
    return check.model_copy(update=update)


def test_dismissed_and_empty_reviews_skipped(store, tracked, gh):
    gh.add_review(R(1, state="DISMISSED", body="was wrong"))
    gh.add_review(R(2, state="APPROVED", body=""))
    gh.add_review(R(3, state="COMMENTED", body="   "))
    gh.add_review(R(4, state="CHANGES_REQUESTED", body="fix the race"))
    gh.add_review(R(5, state="APPROVED", body="LGTM"))
    assert [f.id for f in collect(store, gh, tracked)] == ["r4", "r5"]


def test_reviews_before_since_or_unsubmitted_are_skipped(store, tracked, gh):
    gh.add_review(R(1, at=SINCE - timedelta(seconds=1)))
    gh.add_review(R(2, at=SINCE))
    gh.add_review(replace_review(R(3), submitted_at=None, state="PENDING"))
    assert [f.id for f in collect(store, gh, tracked)] == ["r2"]


def replace_review(review, **update):
    return review.model_copy(update=update)


def test_review_from_stranger_or_unlisted_bot_skipped(store, tracked, gh):
    gh.add_review(R(1, author="rando", association="NONE"))
    gh.add_review(R(2, author="other[bot]", association="NONE"))
    gh.add_review(R(3, author=COPILOT, association="NONE"))
    assert [f.id for f in collect(store, gh, tracked)] == ["r3"]


def test_review_finding_fields(store, tracked, gh):
    gh.add_review(R(9, state="CHANGES_REQUESTED", body="fix the race"))
    [f] = collect(store, gh, tracked)
    assert (f.source, f.path, f.line, f.fingerprint) == (
        "review",
        None,
        None,
        fingerprint(None, "fix the race"),
    )
    assert f.url == "https://github.com/acme/app/pull/7#pullrequestreview-9"


def test_marked_findings_are_dropped(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "a"))
    gh.add_comment(C(2, "alice", "MEMBER", "b"))
    gh.add_review(R(3))
    gh.set_checks("sha1", [K(4)])
    store.set_meta("intake:t:acme/app:comment:1", "done")
    store.set_meta("intake:t:acme/app:review:3", "done")
    store.set_meta("intake:t:acme/app:check:4", "done")
    assert [f.id for f in collect(store, gh, tracked)] == ["c2"]


def test_marker_is_per_tenant_and_repo(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "a"))
    store.set_meta("intake:other:acme/app:comment:1", "done")
    store.set_meta("intake:t:acme/other:comment:1", "done")
    assert [f.id for f in collect(store, gh, tracked)] == ["c1"]


def test_collect_does_not_set_markers(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "a"))
    collect(store, gh, tracked)
    assert store.get_meta("intake:t:acme/app:comment:1") is None


def test_marker_key():
    f = PRFinding(
        id="k42",
        source="check",
        author="ci",
        is_bot=False,
        association="CI",
        body="x",
        url="u",
        created_at=AFTER,
        fingerprint="f",
    )
    assert f.marker_key("t", REPO) == "intake:t:acme/app:check:42"


def test_findings_sorted_by_created_at_then_id(store, tracked, gh):
    gh.add_comment(C(2, "alice", "MEMBER", "b", at=AFTER + timedelta(minutes=5)))
    gh.add_comment(C(10, "alice", "MEMBER", "a", at=AFTER))
    gh.add_comment(C(3, "alice", "MEMBER", "c", at=AFTER))
    gh.add_review(R(1, at=AFTER))
    assert [f.id for f in collect(store, gh, tracked)] == ["c10", "c3", "r1", "c2"]


def test_body_cut_to_2000(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "x" * 5000))
    gh.add_review(R(2, body="y" * 5000))
    found = collect(store, gh, tracked)
    assert [len(f.body) for f in found] == [2000, 2000]


def test_pr_finding_rejects_body_over_2000():
    with pytest.raises(ValueError):
        PRFinding(
            id="c1",
            source="comment",
            author="a",
            is_bot=False,
            association="MEMBER",
            body="x" * 2001,
            url="u",
            created_at=AFTER,
            fingerprint="f",
        )


def test_fingerprint_ignores_case_digits_and_whitespace():
    assert fingerprint("a.py", "Off by 1  here") == fingerprint("a.py", "off by 2 here")


def test_fingerprint_ignores_code_fences_and_urls():
    a = "Use this:\n```py\nx = 1\n```\nsee https://example.com/a/1"
    b = "use this:\n```js\ny = 2\n```\nsee http://other.org/b"
    assert fingerprint("a.py", a) == fingerprint("a.py", b)


def test_fingerprint_depends_on_path_and_text():
    base = fingerprint("a.py", "missing check")
    assert base != fingerprint("b.py", "missing check")
    assert base != fingerprint("a.py", "different words")
    assert fingerprint(None, "x") == fingerprint("", "x")


def test_fingerprint_shape_and_value():
    import hashlib

    expected = hashlib.sha1(b"a.py\nmissing check").hexdigest()[:12]
    assert fingerprint("a.py", "Missing   Check") == expected


def test_counts_rules():
    def f(author, association, bot=False):
        return PRFinding(
            id="c1",
            source="comment",
            author=author,
            is_bot=bot,
            association=association,
            body="x",
            url="u",
            created_at=AFTER,
            fingerprint="f",
        )

    bots = ["good[bot]"]
    assert counts(f("a", "MEMBER"), bots)
    assert counts(f("a", "OWNER"), bots)
    assert counts(f("a", "COLLABORATOR"), bots)
    assert not counts(f("a", "CONTRIBUTOR"), bots)
    assert not counts(f("a", "NONE"), bots)
    assert not counts(f("a", "MEMBER", bot=True), bots)
    assert counts(f("good[bot]", "NONE", bot=True), bots)
    assert counts(f("good[bot]", "MEMBER", bot=True), bots)
    assert not counts(f("evil[bot]", "NONE", bot=True), bots)


def test_as_finding_maps_severity_and_title(store, tracked, gh):
    gh.add_comment(C(1, "alice", "MEMBER", "first line\nmore", path="a.py", line=4))
    gh.add_review(R(2, state="CHANGES_REQUESTED", body="x" * 200))
    gh.add_review(R(3, state="COMMENTED", body="note"))
    gh.set_checks("sha1", [K(4, name="tests")])
    found = {f.id: f.as_finding() for f in collect(store, gh, tracked)}
    assert found["c1"] == Finding(
        id="c1",
        severity="minor",
        kind="bug",
        path="a.py",
        line=4,
        title="first line",
        detail="first line\nmore",
    )
    assert found["r2"].severity == "major"
    assert found["r2"].title == "x" * 80
    assert (found["r2"].path, found["r2"].line) == ("", None)
    assert found["r3"].severity == "minor"
    assert found["k4"].severity == "major"
    assert found["k4"].kind == "bug"


def test_tracked_pulls_only_open_tasks_of_this_repo(store):
    open_task = make_task(store, number=7)
    other_repo = make_task(store, repo="acme/other", number=8)
    merged = make_task(store, number=9)
    cancelled = make_task(store, number=10)
    awaiting = make_task(store, number=11)
    no_pr = store.create_task("t", "no pr", "cli")
    store.put_artifact(no_pr.id, ArtifactKind.TARGET, Role.HUMAN, {"repo": REPO})
    no_target = store.create_task("t", "no target", "cli")
    store.put_artifact(
        no_target.id, ArtifactKind.PR, Role.LEAD, {"number": 12, "url": "u"}
    )
    foreign = store.create_task("other", "foreign tenant", "cli")
    store.put_artifact(foreign.id, ArtifactKind.TARGET, Role.HUMAN, {"repo": REPO})
    store.put_artifact(
        foreign.id, ArtifactKind.PR, Role.LEAD, {"number": 13, "url": "u"}
    )
    walk(store, merged.id, TaskState.MERGED)
    walk(store, cancelled.id, TaskState.CANCELLED)
    walk(store, awaiting.id, TaskState.AWAITING_HUMAN)

    pulls = tracked_pulls(store, "t", REPO)
    assert [(p.task.id, p.number, p.url) for p in pulls] == [
        (open_task.id, 7, "https://github.com/acme/app/pull/7"),
        (awaiting.id, 11, "https://github.com/acme/app/pull/11"),
    ]
    assert other_repo.id not in [p.task.id for p in pulls]


def test_tracked_pull_uses_latest_pr_artifact_and_its_created_at(store):
    t = make_task(store, number=7)
    newer = store.put_artifact(
        t.id, ArtifactKind.PR, Role.LEAD, {"number": 21, "url": "u21"}
    )
    [p] = tracked_pulls(store, "t", REPO)
    assert (p.number, p.url, p.opened_at) == (21, "u21", newer.created_at)


def test_tracked_pull_uses_latest_target(store):
    t = make_task(store, repo="acme/old")
    store.put_artifact(t.id, ArtifactKind.TARGET, Role.HUMAN, {"repo": REPO})
    assert [p.task.id for p in tracked_pulls(store, "t", REPO)] == [t.id]
    assert tracked_pulls(store, "t", "acme/old") == []


def walk(store, task_id, target):
    path = {
        TaskState.MERGED: [
            TaskState.PM,
            TaskState.TRIAGE,
            TaskState.ENGINEERING,
            TaskState.PREFLIGHT,
            TaskState.INTERNAL_REVIEW,
            TaskState.LEAD_DECISION,
            TaskState.PR_OPEN,
            TaskState.MERGED,
        ],
        TaskState.CANCELLED: [TaskState.CANCELLED],
        TaskState.AWAITING_HUMAN: [
            TaskState.PM,
            TaskState.TRIAGE,
            TaskState.AWAITING_HUMAN,
        ],
    }[target]
    for state in path:
        store.transition(task_id, state, Role.LEAD, "test")


def test_tenant_config_has_intake_defaults():
    cfg = TenantConfig(tenant="acme", roles={})
    assert cfg.intake == IntakeConfig()
