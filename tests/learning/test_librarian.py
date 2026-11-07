import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentco.agents.qa import QAFailure, QAReply
from agentco.config import Thresholds
from agentco.github.client import FakeGitHub
from agentco.github.errors import GitHubError
from agentco.github.models import GhReviewComment, GhUser
from agentco.learning.librarian import (
    NO_MODEL,
    DraftLesson,
    LearnReport,
    Librarian,
    LibrarianReply,
    artifact_marker,
    closed_marker,
)
from agentco.llm.fake import FakeRuntime
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import ArtifactKind as K
from agentco.workflow.states import TaskState

REPO = "acme/app"
NOW = datetime(2026, 11, 7, tzinfo=UTC)
LATER = datetime.now(UTC) + timedelta(hours=1)
PR_URL = f"https://github.com/{REPO}/pull/7"
PATH = [
    TaskState.PM,
    TaskState.TRIAGE,
    TaskState.ENGINEERING,
    TaskState.PREFLIGHT,
    TaskState.INTERNAL_REVIEW,
    TaskState.LEAD_DECISION,
    TaskState.PR_OPEN,
]


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def gate(backend):
    return MemoryGate("acme", backend)


def items(backend, scope):
    return backend.items(f"acme__{scope.value}")


def everything(backend):
    return [i for s in Scope for i in items(backend, s)]


def of_kind(backend, scope, kind):
    return [i for i in items(backend, scope) if i.body.startswith(kind)]


def librarian(cfg, store, gate, script=None, gh=None):
    runtime = FakeRuntime({Role.LIBRARIAN: script or []})
    return Librarian(cfg, store, gate, runtime, gh), runtime


def new_task(store, state=TaskState.INTAKE, title="Fix the parser"):
    task = store.create_task("acme", title, "issue", "12")
    for step in PATH:
        if state is TaskState.INTAKE:
            break
        store.transition(task.id, step, Role.LEAD, "test")
        if step is state:
            break
    if state in (TaskState.MERGED, TaskState.CANCELLED):
        store.transition(task.id, state, Role.HUMAN, "test")
    return store.get(task.id).id


def put(store, tid, kind, body, role=Role.LEAD):
    return store.put_artifact(tid, kind, role, body)


def engineer(store, tid, files=("src/app/a.py",), invariant=""):
    return put(
        store,
        tid,
        K.ENGINEER,
        {"summary": "s", "files_touched": list(files), "invariant": invariant},
        Role.JUNIOR,
    )


def lead(store, tid, comments=("Validate input at the API edge",)):
    return put(store, tid, K.LEAD, {"outcome": "send_back", "comments": list(comments)})


def qa_reply(**kw):
    return QAReply(
        confidence=0.9,
        passed=not kw.get("failures"),
        bug_class=kw.pop("bug_class", "off-by-one"),
        tests_run=[],
        neighbour_matrix=[],
        **kw,
    ).model_dump()


def escalation(store, tid, category="new", from_state="triage", reason="unknown area"):
    return put(
        store,
        tid,
        K.ESCALATION,
        {
            "from_state": from_state,
            "category": category,
            "reason": reason,
            "options": ["approve", "reject"],
            "question": "Go ahead?",
        },
    )


def human(store, tid, decision="answer", note="use the new queue"):
    return put(
        store, tid, K.HUMAN_DECISION, {"decision": decision, "note": note}, Role.HUMAN
    )


def rule_reply(
    text="Hand new-area work to a human first", category="new", kind="EscalationRule"
):
    return json.dumps({"lessons": [{"kind": kind, "text": text, "category": category}]})


NONE = json.dumps({"lessons": [{"kind": "none", "text": ""}]})


def comment(id, author, assoc, pr=7, path="src/app/a.py", line=3, at=None, **kw):
    at = at or LATER
    return GhReviewComment(
        id=id,
        pr_number=pr,
        author=GhUser(author, kw.get("bot", False)),
        association=assoc,
        body=kw.get("body", f"please rename {id}"),
        path=path,
        line=line,
        original_line=line,
        side="RIGHT",
        subject_type="line",
        commit_id="sha",
        original_commit_id="sha",
        diff_hunk="",
        in_reply_to=None,
        created_at=at,
        updated_at=at,
        url=f"https://github.com/{REPO}/pull/{pr}#discussion_r{id}",
    )


def fake_github(*comments, pr_author="naseem"):
    gh = FakeGitHub([], [], list(comments), {})
    gh.create_pull(REPO, "agentco/t", "main", "t", "b")
    pull = gh.get_pull(REPO, 1)
    gh.upsert_pull(replace(pull, number=7, author=GhUser(pr_author, False)))
    return gh


def merged_task(store, gh_pr=True, invariant="One rule: parse once at the edge"):
    tid = new_task(store, TaskState.PR_OPEN)
    put(
        store,
        tid,
        K.ISSUE,
        {
            "summary": "Parser crashes on empty input",
            "why": "w",
            "acceptance_criteria": ["a"],
        },
        Role.PM,
    )
    engineer(store, tid, ("src/app/a.py", "src/app/b.py", "tests/test_a.py"), invariant)
    if gh_pr:
        put(store, tid, K.PR, {"number": 7, "url": PR_URL})
    store.transition(tid, TaskState.MERGED, Role.HUMAN, "merged")
    return tid


async def test_lead_comments_become_review_rules_once(loop_cfg, store, gate, backend):
    tid = new_task(store, TaskState.ENGINEERING)
    engineer(store, tid, ("src/app/a.py", "src/app/b.py", "lib/c.py"))
    lead(store, tid)
    lib, _ = librarian(loop_cfg, store, gate)
    first = await lib.learn_task(tid, NOW)
    second = await lib.learn_task(tid, NOW)
    rules = of_kind(backend, Scope.RND, "ReviewRule")
    assert len(rules) == 1
    assert "origin lead" in rules[0].body
    assert "Applies to: lib, src/app" in rules[0].body
    assert f"Source: task:{tid}:lead:" in rules[0].body
    assert (first.added, first.duplicates, first.errors) == (1, 0, [])
    assert (second.added, second.duplicates, second.errors) == (0, 0, [])


async def test_lead_modules_come_from_the_engineer_it_reviewed(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    engineer(store, tid, ("src/app/a.py",))
    lead(store, tid)
    engineer(store, tid, ("other/z.py",))
    lib, _ = librarian(loop_cfg, store, gate)
    await lib.learn_task(tid, NOW)
    assert "Applies to: src/app\n" in of_kind(backend, Scope.RND, "ReviewRule")[0].body


async def test_qa_failures_go_to_rnd_without_reasoning(loop_cfg, store, gate, backend):
    tid = new_task(store, TaskState.ENGINEERING)
    failure = QAFailure(title="Crash", steps="run x", expected="ok", actual="boom")
    put(
        store,
        tid,
        K.QA,
        qa_reply(failures=[failure], bug_class="secret-class"),
        Role.QA,
    )
    lib, _ = librarian(loop_cfg, store, gate)
    await lib.learn_task(tid, NOW)
    issues = of_kind(backend, Scope.RND, "Issue")
    assert len(issues) == 1 and "QA failure: Crash. Steps: run x." in issues[0].body
    assert not any("secret-class" in i.body for i in items(backend, Scope.RND))


async def test_pr_review_real_bug_lands_in_qa_scope_with_bug_class(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.PR_OPEN)
    put(store, tid, K.QA, qa_reply(bug_class="off-by-one"), Role.QA)
    put(
        store,
        tid,
        K.PR_REVIEW,
        {
            "pr": 7,
            "consumed": ["c1"],
            "classifications": [
                {
                    "finding_id": "c1",
                    "fingerprint": "fp1",
                    "category": "real_bug",
                    "note": "n",
                }
            ],
            "recurrences": [],
            "decision": {
                "findings_to_fix": [
                    {"id": "c1", "title": "Crash on empty", "path": "a.py"}
                ]
            },
        },
    )
    put(store, tid, K.QA, qa_reply(bug_class="later-class"), Role.QA)
    lib, _ = librarian(loop_cfg, store, gate)
    report = await lib.learn_task(tid, NOW)
    qa = [i.body for i in items(backend, Scope.QA)]
    assert report.added == 3 and report.errors == []
    assert any(
        b.startswith("NeighbourCase") and "bug class off-by-one:" in b for b in qa
    )
    assert any(b.startswith("EscapedBug") and "escaped to PR #7" in b for b in qa)
    assert [i.body.split(" ")[0] for i in items(backend, Scope.RND)] == ["Risk"]


async def test_human_decision_becomes_escalation_rule_in_company(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    human(store, tid)
    lib, runtime = librarian(loop_cfg, store, gate, [rule_reply()])
    report = await lib.learn_task(tid, NOW)
    rules = of_kind(backend, Scope.COMPANY, "EscalationRule")
    assert len(rules) == 1
    assert rules[0].body.startswith(
        "EscalationRule (origin human): Escalate as new: Hand new-area work to a human first"
    )
    assert f"Source: task:{tid}:human_decision:" in rules[0].body
    assert report.added == 1
    (call,) = runtime.calls
    assert (call.role, call.model, call.workdir, call.tools, call.edits) == (
        Role.LIBRARIAN,
        "m-librarian",
        None,
        (),
        False,
    )
    for part in (
        "category new",
        "unknown area",
        "Go ahead?",
        "- approve",
        "use the new queue",
    ):
        assert part in call.prompt
    assert "# Role: Librarian" in call.system
    assert store.get(tid).cost_usd > 0


async def test_prompt_lists_the_five_nearest_company_facts(
    loop_cfg, store, gate, backend
):
    for n in range(7):
        await backend.add(
            "acme__company",
            Scope.COMPANY,
            MemoryItem(f"p{n}", f"queue policy {n}", "t", NOW),
        )
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid, reason="queue")
    human(store, tid, note="queue")
    lib, runtime = librarian(loop_cfg, store, gate, [NONE])
    await lib.learn_task(tid, NOW)
    assert [f"- queue policy {n}" in runtime.calls[0].prompt for n in range(7)] == [
        True
    ] * 5 + [False] * 2


async def test_librarian_none_writes_nothing(loop_cfg, store, gate, backend):
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    human(store, tid)
    lib, runtime = librarian(loop_cfg, store, gate, [NONE])
    first = await lib.learn_task(tid, NOW)
    await lib.learn_task(tid, NOW)
    assert everything(backend) == []
    assert (first.added, first.duplicates, first.errors) == (0, 0, [])
    assert len(runtime.calls) == 1  # marked: the rerun does not ask again


async def test_at_most_two_lessons_are_kept(loop_cfg, store, gate, backend):
    drafts = [
        {"kind": "Policy", "text": f"{word} comes first", "category": ""}
        for word in ("Alpha", "Beta", "Gamma")
    ]
    assert len(LibrarianReply.model_validate({"lessons": drafts}).lessons) == 2
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    human(store, tid)
    lib, _ = librarian(loop_cfg, store, gate, [json.dumps({"lessons": drafts})])
    await lib.learn_task(tid, NOW)
    assert [i.body.split("\n")[0] for i in items(backend, Scope.COMPANY)] == [
        "Policy (origin human): Alpha comes first",
        "Policy (origin human): Beta comes first",
    ]


async def test_policy_ignores_category_and_unknown_category_is_not_prefixed(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    human(store, tid)
    reply = json.dumps(
        {
            "lessons": [
                {"kind": "Policy", "text": "Keep it simple", "category": "new"},
                {"kind": "EscalationRule", "text": "Ask first", "category": "bogus"},
            ]
        }
    )
    lib, _ = librarian(loop_cfg, store, gate, [reply])
    await lib.learn_task(tid, NOW)
    assert [i.body.split("\n")[0] for i in items(backend, Scope.COMPANY)] == [
        "Policy (origin human): Keep it simple",
        "EscalationRule (origin human): Ask first",
    ]


async def test_invalid_reply_retried_next_run_and_others_continue(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    decision = human(store, tid)
    lead(store, tid)
    lib, runtime = librarian(
        loop_cfg, store, gate, ["nope", "still nope", rule_reply()]
    )
    first = await lib.learn_task(tid, NOW)
    assert first.added == 1  # the lead rule, despite the invalid decision
    assert len(first.errors) == 1 and first.errors[0].startswith(
        f"human_decision #{decision.id}: "
    )
    assert store.get_meta(artifact_marker("acme", decision.id)) is None
    second = await lib.learn_task(tid, NOW)
    assert (second.added, second.errors) == (1, [])
    assert len(of_kind(backend, Scope.COMPANY, "EscalationRule")) == 1
    assert store.get_meta(artifact_marker("acme", decision.id)) == "done"
    assert len(runtime.calls) == 3


async def test_unreadable_body_is_reported_once_then_marked(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    bad = put(store, tid, K.QA, {"failures": [{"bogus": 1}]}, Role.QA)
    lib, _ = librarian(loop_cfg, store, gate)
    first = await lib.learn_task(tid, NOW)
    second = await lib.learn_task(tid, NOW)
    assert len(first.errors) == 1 and first.errors[0].startswith(
        f"qa #{bad.id}: unreadable"
    )
    assert second.errors == []
    assert store.get_meta(artifact_marker("acme", bad.id)) == "invalid"
    assert everything(backend) == []


async def test_no_librarian_model_leaves_decisions_unmarked(
    loop_cfg, store, gate, backend
):
    roles = {r: m for r, m in loop_cfg.roles.items() if r is not Role.LIBRARIAN}
    cfg = loop_cfg.model_copy(update={"roles": roles})
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid)
    human(store, tid)
    decision = human(store, tid, note="again")
    lead(store, tid)
    lib, runtime = librarian(cfg, store, gate)
    first = await lib.learn_task(tid, NOW)
    assert first.errors == [NO_MODEL] == ["no model for librarian"]
    assert first.added == 1 and runtime.calls == []
    assert store.get_meta(artifact_marker("acme", decision.id)) is None
    again = await lib.learn_task(tid, NOW)
    assert (again.errors, again.added) == ([NO_MODEL], 0)


def pr_review(store, tid, classes):
    return put(
        store,
        tid,
        K.PR_REVIEW,
        {
            "pr": 7,
            "consumed": [],
            "classifications": [
                {"finding_id": f"c{i}", "fingerprint": fp, "category": cat, "note": ""}
                for i, (fp, cat) in enumerate(classes)
            ],
            "recurrences": [],
            "decision": {"findings_to_fix": []},
        },
    )


def approve_over_nits(store, tid, category="ready_to_merge", decision="approve"):
    pr_review(
        store,
        tid,
        [("fp-nit", "covered_style"), ("fp-bug", "real_bug"), ("fp-edge", "edge_case")],
    )
    escalation(store, tid, category=category, from_state="pr_open")
    return human(store, tid, decision=decision, note="")


async def test_approve_over_nits_bumps_overrides_once_per_decision(
    loop_cfg, store, gate
):
    cfg = loop_cfg.model_copy(update={"thresholds": Thresholds(stale_after=2)})
    t1 = new_task(store, TaskState.PR_OPEN)
    approve_over_nits(store, t1)
    lib, runtime = librarian(cfg, store, gate, [NONE, NONE])
    first = await lib.learn_task(t1, NOW)
    await lib.learn_task(t1, NOW)
    key = "librarian:acme:override:"
    assert [store.get_meta(key + fp) for fp in ("fp-nit", "fp-edge", "fp-bug")] == [
        "1",
        "1",
        None,
    ]
    assert first.stale == []
    t2 = new_task(store, TaskState.PR_OPEN)
    approve_over_nits(store, t2, category="tradeoff")
    second = await lib.learn_task(t2, NOW)
    assert second.stale == ["fp-nit", "fp-edge"]
    assert store.get_meta(key + "fp-nit") == "2"
    assert len(runtime.calls) == 2


async def test_override_is_not_double_counted_after_a_failed_reply(
    loop_cfg, store, gate
):
    t = new_task(store, TaskState.PR_OPEN)
    approve_over_nits(store, t)
    lib, _ = librarian(loop_cfg, store, gate, ["x", "y", NONE])
    assert len((await lib.learn_task(t, NOW)).errors) == 1
    await lib.learn_task(t, NOW)
    assert store.get_meta("librarian:acme:override:fp-nit") == "1"


@pytest.mark.parametrize(
    ("category", "from_state", "decision"),
    [
        ("stuck", "pr_open", "approve"),
        ("tradeoff", "lead_decision", "approve"),
        ("tradeoff", "pr_open", "answer"),
        ("tradeoff", "pr_open", "reject"),
    ],
)
async def test_other_decisions_do_not_bump(
    loop_cfg, store, gate, category, from_state, decision
):
    t = new_task(store, TaskState.PR_OPEN)
    pr_review(store, t, [("fp-nit", "covered_style")])
    escalation(store, t, category=category, from_state=from_state)
    human(store, t, decision=decision)
    lib, _ = librarian(loop_cfg, store, gate, [NONE])
    await lib.learn_task(t, NOW)
    assert store.get_meta("librarian:acme:override:fp-nit") is None


async def test_override_still_counts_without_a_librarian_model(loop_cfg, store, gate):
    roles = {r: m for r, m in loop_cfg.roles.items() if r is not Role.LIBRARIAN}
    cfg = loop_cfg.model_copy(update={"roles": roles})
    t = new_task(store, TaskState.PR_OPEN)
    approve_over_nits(store, t)
    lib, _ = librarian(cfg, store, gate)
    await lib.learn_task(t, NOW)
    await lib.learn_task(t, NOW)
    assert store.get_meta("librarian:acme:override:fp-nit") == "1"


async def test_decision_without_an_escalation_asks_nothing(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.ENGINEERING)
    decision = human(store, tid)
    lib, runtime = librarian(loop_cfg, store, gate)
    report = await lib.learn_task(tid, NOW)
    assert runtime.calls == [] and report.errors == []
    assert store.get_meta(artifact_marker("acme", decision.id)) == "done"


async def test_decision_pairs_with_the_escalation_before_it(loop_cfg, store, gate):
    tid = new_task(store, TaskState.ENGINEERING)
    escalation(store, tid, reason="first problem")
    human(store, tid, note="first answer")
    escalation(store, tid, reason="second problem")
    human(store, tid, note="second answer")
    lib, runtime = librarian(loop_cfg, store, gate, [NONE, NONE])
    await lib.learn_task(tid, NOW)
    first, second = (c.prompt for c in runtime.calls)
    assert "first problem" in first and "first answer" in first
    assert "second problem" not in first
    assert "second problem" in second and "second answer" in second


async def test_merged_close_out_writes_playbook_issue_profile_and_threads_once(
    loop_cfg, store, gate, backend
):
    tid = merged_task(store)
    store.assign(tid, Role.SENIOR)
    store.record_cost(tid, Role.SENIOR, 1.234, {})
    pr_review(store, tid, [("fp1", "real_bug"), ("fp2", "covered_style")])
    gh = fake_github(
        comment(101, "alice", "MEMBER"),
        comment(102, "naseem", "OWNER", path="README.md"),
        comment(103, "carol", "NONE"),
        comment(104, "copilot[bot]", "NONE", bot=True),
        comment(105, "alice", "MEMBER", pr=8),
    )
    lib, _ = librarian(loop_cfg, store, gate, gh=gh)
    first = await lib.learn_task(tid, NOW)
    second = await lib.learn_task(tid, NOW)
    rnd = [i.body for i in items(backend, Scope.RND)]
    playbook = [b for b in rnd if b.startswith("Playbook")]
    assert len(playbook) == 1 and "One rule: parse once at the edge" in playbook[0]
    assert "Applies to: src/app, tests" in playbook[0]
    solved = [b for b in rnd if b.startswith("Issue")]
    assert len(solved) == 1
    assert solved[0].split("\n")[0] == (
        "Issue (origin librarian): Solved: Parser crashes on empty input "
        "(PR #7; files: src/app/a.py, src/app/b.py, tests/test_a.py)"
    )
    profile = [b for b in rnd if b.startswith("AgentProfile")]
    assert len(profile) == 1
    assert profile[0].split("\n")[0] == (
        f"AgentProfile (origin librarian): Task #{tid} 'Fix the parser' ended merged: "
        "assignee senior, reassigned no, rounds 0, real PR findings 1, cost $1.23."
    )
    threads = [b for b in rnd if b.startswith("Review rule from human reviewer")]
    assert sorted(b.split("@")[1].split(" ")[0] for b in threads) == ["alice", "naseem"]
    assert first.errors == []
    assert len(rnd) == 6  # risk, playbook, issue, profile, two threads
    assert len(items(backend, Scope.QA)) == 2  # escaped bug, neighbour case
    assert first.added == 8
    assert (second.added, second.duplicates, second.errors) == (0, 0, [])
    assert store.get_meta(closed_marker("acme", tid)) == "done"


async def test_cancelled_close_out_writes_only_the_profile(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.PR_OPEN)
    engineer(store, tid, invariant="never written")
    put(store, tid, K.PR, {"number": 7, "url": PR_URL})
    store.transition(tid, TaskState.CANCELLED, Role.HUMAN, "closed")
    gh = fake_github(comment(101, "alice", "MEMBER"))
    lib, _ = librarian(loop_cfg, store, gate, gh=gh)
    called = []

    async def hook(task):
        called.append(task.id)

    lib._create_qa_followup = hook
    report = await lib.learn_task(tid, NOW)
    bodies = [i.body for i in items(backend, Scope.RND)]
    assert [b.split(" ")[0] for b in bodies] == ["AgentProfile", "Review"]
    assert "ended cancelled" in bodies[0]
    assert called == []
    assert report.added == 2


async def test_without_github_or_pr_no_threads_are_read(loop_cfg, store, gate, backend):
    tid = merged_task(store)
    lib, _ = librarian(loop_cfg, store, gate)
    await lib.learn_task(tid, NOW)
    assert not any(i.body.startswith("Review rule") for i in items(backend, Scope.RND))
    other = merged_task(store, gh_pr=False)
    gh = fake_github(comment(101, "alice", "MEMBER"))
    gh.calls.clear()
    lib2, _ = librarian(loop_cfg, store, gate, gh=gh)
    await lib2.learn_task(other, NOW)
    assert gh.calls == []


async def test_review_thread_marker_shared_with_importer(
    loop_cfg, store, gate, backend
):
    tid = merged_task(store)
    gh = fake_github(
        comment(101, "alice", "MEMBER"),
        comment(102, "alice", "MEMBER", path="README.md", line=None),
        comment(103, "alice", "MEMBER"),
    )
    store.set_meta(
        f"import:acme:{REPO}:review:103", "added"
    )  # importer got there first
    lib, _ = librarian(loop_cfg, store, gate, gh=gh)
    report = await lib.learn_task(tid, NOW)
    threads = [i for i in items(backend, Scope.RND) if i.name.startswith("review:")]
    assert sorted(i.name for i in threads) == [
        f"review:{REPO}:101",
        f"review:{REPO}:102",
    ]
    assert (
        store.get_meta(f"import:acme:{REPO}:review:101") == "added"
    )  # code to link later
    assert store.get_meta(f"import:acme:{REPO}:review:102") == "done"
    assert store.get_meta(f"import:acme:{REPO}:review:103") == "added"  # untouched
    assert report.duplicates == 1


async def test_pr_author_login_is_masked_but_others_stay_filtered(
    loop_cfg, store, gate, backend
):
    tid = merged_task(store)
    gh = fake_github(
        comment(101, "naseem", "OWNER"),  # the user, who is the PR author on GitHub
        comment(102, "dave", "CONTRIBUTOR"),
    )
    lib, _ = librarian(loop_cfg, store, gate, gh=gh)
    await lib.learn_task(tid, NOW)
    names = [i.name for i in items(backend, Scope.RND) if i.name.startswith("review:")]
    assert names == [f"review:{REPO}:101"]


async def test_github_failure_leaves_close_out_open_and_retries(
    loop_cfg, store, gate, backend
):
    tid = merged_task(store)

    class Down(FakeGitHub):
        def get_pull(self, repo, number):
            raise GitHubError("rate limited")

    down = Down([], [], [], {})
    lib, _ = librarian(loop_cfg, store, gate, gh=down)
    first = await lib.learn_task(tid, NOW)
    assert first.errors == ["review threads: rate limited"]
    assert store.get_meta(closed_marker("acme", tid)) is None
    assert first.added == 3  # playbook, issue, profile
    gh = fake_github(comment(101, "alice", "MEMBER"))
    gh.calls.clear()
    lib2, _ = librarian(loop_cfg, store, gate, gh=gh)
    second = await lib2.learn_task(tid, NOW)
    assert (second.added, second.errors) == (1, [])
    assert store.get_meta(closed_marker("acme", tid)) == "done"


async def test_playbook_uses_the_last_non_empty_invariant(
    loop_cfg, store, gate, backend
):
    tid = new_task(store, TaskState.PR_OPEN)
    engineer(store, tid, invariant="Early rule")
    engineer(store, tid, invariant="  ")
    store.transition(tid, TaskState.MERGED, Role.HUMAN, "m")
    lib, _ = librarian(loop_cfg, store, gate)
    await lib.learn_task(tid, NOW)
    assert [i.body.split("\n")[0] for i in of_kind(backend, Scope.RND, "Playbook")] == [
        "Playbook (origin librarian): Early rule"
    ]


async def test_no_invariant_no_playbook(loop_cfg, store, gate, backend):
    tid = merged_task(store, invariant="")
    lib, _ = librarian(loop_cfg, store, gate)
    await lib.learn_task(tid, NOW)
    assert of_kind(backend, Scope.RND, "Playbook") == []


async def test_merged_calls_the_qa_followup_hook_once(loop_cfg, store, gate):
    tid = merged_task(store, gh_pr=False)
    lib, _ = librarian(loop_cfg, store, gate)
    seen = []

    async def hook(task):
        seen.append((task.id, task.state))

    lib._create_qa_followup = hook
    await lib.learn_task(tid, NOW)
    await lib.learn_task(tid, NOW)
    assert seen == [(tid, TaskState.MERGED)]


async def test_never_writes_product(loop_cfg, store, gate, backend):
    tid = merged_task(store)
    escalation(store, tid)
    human(store, tid)
    lead(store, tid)
    put(
        store,
        tid,
        K.QA,
        qa_reply(failures=[QAFailure(title="t", steps="s", expected="e", actual="a")]),
        Role.QA,
    )
    pr_review(store, tid, [("fp", "real_bug")])
    lib, _ = librarian(
        loop_cfg,
        store,
        gate,
        [rule_reply()],
        gh=fake_github(comment(101, "alice", "MEMBER")),
    )
    await lib.learn_task(tid, NOW)
    assert items(backend, Scope.PRODUCT) == []
    assert (
        items(backend, Scope.COMPANY)
        and items(backend, Scope.QA)
        and items(backend, Scope.RND)
    )


async def test_every_written_item_has_provenance(loop_cfg, store, gate, backend):
    tid = merged_task(store)
    escalation(store, tid)
    human(store, tid)
    lead(store, tid)
    lib, _ = librarian(loop_cfg, store, gate, [rule_reply()])
    await lib.learn_task(tid, NOW)
    for item in everything(backend):
        assert "\nSource: task:" in item.body
        assert item.source_description.startswith("agentco librarian: task:")


def test_needs_learning_tracks_markers_and_close_out(loop_cfg, store, gate):
    tid = new_task(store, TaskState.PR_OPEN)
    lib, _ = librarian(loop_cfg, store, gate)
    assert lib.needs_learning(tid) is False  # nothing the Librarian reads yet
    lead(store, tid)
    assert lib.needs_learning(tid) is True
    asyncio.run(lib.learn_task(tid, NOW))
    assert lib.needs_learning(tid) is False
    store.transition(tid, TaskState.MERGED, Role.HUMAN, "m")
    assert lib.needs_learning(tid) is True
    asyncio.run(lib.learn_task(tid, NOW))
    assert lib.needs_learning(tid) is False


async def test_task_of_another_tenant_is_refused(loop_cfg, store, gate):
    other = store.create_task("other", "t", "issue").id
    lib, _ = librarian(loop_cfg, store, gate)
    with pytest.raises(ValueError, match=f"^task {other} belongs to tenant other$"):
        await lib.learn_task(other, NOW)


def test_draft_and_report_defaults():
    assert DraftLesson(kind="Policy", text="x").category == ""
    report = LearnReport(3)
    assert (
        report.task_id,
        report.added,
        report.duplicates,
        report.stale,
        report.errors,
    ) == (
        3,
        0,
        0,
        [],
        [],
    )
