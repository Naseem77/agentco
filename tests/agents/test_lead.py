import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from agentco.agents.checks import CheckOutcome
from agentco.agents.engineer import EngineerReply, FollowUp
from agentco.agents.lead import (
    ChildSummary,
    Classified,
    LeadDecision,
    LeadReply,
    critical_hits,
    decide,
    diff_paths,
    new_hits,
    review_parent,
    run_lead,
)
from agentco.agents.prompts import load_prompt
from agentco.agents.qa import QAFailure, QAReply
from agentco.agents.replies import Finding, PMReply
from agentco.agents.review import ReviewReply
from agentco.llm.fake import FakeRuntime
from agentco.llm.replies import Asker, ReplyInvalid
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.gate import MemoryGate
from agentco.memory.scopes import Scope, graph_name
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.preflight import CheckResult, PreflightReport

ISSUE = PMReply(
    summary="Parser must accept tabs",
    why="Users paste tab-separated data",
    acceptance_criteria=["Given a tab, when parsed, then no error"],
)
QA_OK = QAReply(
    passed=True, bug_class="tabs", tests_run=["pytest"], neighbour_matrix=[]
)
QA_FAIL = QAReply(
    passed=False,
    bug_class="tabs",
    tests_run=["pytest"],
    neighbour_matrix=[],
    failures=[
        QAFailure(
            title="tests failed", steps="pytest -q", expected="exit 0", actual="exit 1"
        )
    ],
)
ENGINEER = EngineerReply(
    summary="Accept tabs",
    files_touched=["src/calc.py"],
    tests_added=["tests/test_calc.py"],
    invariant="tabs are whitespace",
    follow_ups=[FollowUp(title="later", why="y")],
)
PREFLIGHT = PreflightReport(
    ok=True, checks=(CheckResult("size", True, "12 lines"),), conflict=False
)
DIFF = (
    "diff --git a/src/calc.py b/src/calc.py\n--- a/src/calc.py\n+++ b/src/calc.py\n"
    "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)


def F(i, kind="bug", sev="major", path=""):
    return Finding(id=i, severity=sev, kind=kind, path=path, title=i, detail="d")


def L(*cls, conf=0.9, **extra):
    return LeadReply(
        confidence=conf,
        classifications=[Classified(finding_id=i, category=c, note="") for i, c in cls],
        summary="s",
        **extra,
    )


def REVIEW(*findings):
    return ReviewReply(findings=list(findings), summary="r")


def ids(findings):
    return [f.id for f in findings]


def test_critical_beats_everything():
    d = decide(
        L(), REVIEW(), QA_OK, (), ["security: src/auth.py"], ["new dependency: x"], 0.7
    )

    assert (d.outcome, d.category) == ("escalate", "critical")
    assert d.reason == "critical change: security: src/auth.py"


def test_new_beats_send_back():
    d = decide(
        L(("a", "real_bug")),
        REVIEW(F("a")),
        QA_FAIL,
        (),
        [],
        ["new dependency: x"],
        0.7,
    )

    assert (d.outcome, d.category) == ("escalate", "new")
    assert d.reason == "new or unknown: new dependency: x"


def test_real_bug_sends_back_with_style_riding_along():
    d = decide(
        L(("a", "real_bug"), ("b", "covered_style")),
        REVIEW(F("a"), F("b", "style", "nit")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert (d.outcome, d.category) == ("send_back", "")
    assert ids(d.findings_to_fix) == ["a", "b"]


def test_send_back_carries_lead_comments_and_summary():
    d = decide(
        L(("a", "real_bug"), comments=["fix a only"]),
        REVIEW(F("a")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert (d.summary, d.comments) == ("s", ("fix a only",))


def test_style_alone_approves():
    d = decide(
        L(("b", "covered_style")),
        REVIEW(F("b", "style", "nit")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert (d.outcome, d.category) == ("approve", "")
    assert d.findings_to_fix == ()
    assert d.summary == "s"


def test_unclassified_finding_is_real_bug():
    d = decide(L(), REVIEW(F("a")), QA_OK, (), [], [], 0.7)

    assert d.outcome == "send_back"
    assert ids(d.findings_to_fix) == ["a"]


def test_missing_reply_counts_every_finding_as_real_bug():
    d = decide(None, REVIEW(F("a")), QA_OK, (), [], [], 0.7)

    assert d.outcome == "send_back"
    assert ids(d.findings_to_fix) == ["a"]


def test_edge_case_becomes_follow_up_not_blocker():
    d = decide(
        L(("e", "edge_case")),
        REVIEW(F("e", "edge_case", "minor")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert d.outcome == "approve"
    assert ids(d.follow_ups) == ["e"]
    assert d.findings_to_fix == ()


def test_edge_case_alongside_real_bug_is_still_a_follow_up():
    d = decide(
        L(("a", "real_bug"), ("e", "edge_case")),
        REVIEW(F("a"), F("e", "edge_case")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert d.outcome == "send_back"
    assert ids(d.findings_to_fix) == ["a"]
    assert ids(d.follow_ups) == ["e"]


def test_tradeoff_escalates_with_options():
    d = decide(
        L(("t", "tradeoff"), question="Which?", options=["A", "B"]),
        REVIEW(F("t", "tradeoff")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert (d.outcome, d.category) == ("escalate", "tradeoff")
    assert (d.question, d.options) == ("Which?", ("A", "B"))


def test_real_bug_beats_tradeoff():
    d = decide(
        L(("a", "real_bug"), ("t", "tradeoff")),
        REVIEW(F("a"), F("t", "tradeoff")),
        QA_OK,
        (),
        [],
        [],
        0.7,
    )

    assert d.outcome == "send_back"


def test_low_confidence_escalates():
    d = decide(L(conf=0.69), REVIEW(), QA_OK, (), [], [], 0.7)

    assert (d.outcome, d.category) == ("escalate", "not_confident")
    assert d.reason == "lead confidence 0.69 is below 0.7"


def test_confidence_at_threshold_approves():
    d = decide(L(conf=0.7), REVIEW(), QA_OK, (), [], [], 0.7)

    assert d.outcome == "approve"


def test_qa_failure_sends_back_with_its_failures_as_findings():
    d = decide(L(), REVIEW(), QA_FAIL, (), [], [], 0.7)

    assert d.outcome == "send_back"
    assert d.reason == "QA failed"
    assert d.findings_to_fix == (
        Finding(
            id="qa-1",
            severity="major",
            kind="bug",
            title="tests failed",
            detail="Steps: pytest -q\nExpected: exit 0\nActual: exit 1",
        ),
    )


def test_check_error_sends_back():
    d = decide(L(), None, QA_OK, ("qa: TimeoutError: slow",), [], [], 0.7)

    assert d.outcome == "send_back"
    assert d.reason == "check error: qa: TimeoutError: slow"
    assert d.findings_to_fix == ()


def test_missing_qa_without_error_does_not_block():
    d = decide(L(), REVIEW(), None, (), [], [], 0.7)

    assert d.outcome == "approve"


@pytest.mark.parametrize(
    "path,hit",
    [
        ("src/auth/login.py", "security: src/auth/login.py"),
        ("db/migrations/001.sql", "data: db/migrations/001.sql"),
        (".github/workflows/ci.yml", "infra: .github/workflows/ci.yml"),
        ("src/billing.py", "money: src/billing.py"),
        ("src/calc.py", None),
        ("docs/.github/workflows/ci.yml", None),
    ],
)
def test_critical_patterns(path, hit):
    assert critical_hits([path], None) == ([hit] if hit else [])


def test_escaped_bug_regression_is_critical():
    qa = QA_OK.model_copy(update={"regression_of_escaped_bug": True})

    assert critical_hits(["src/calc.py"], qa) == ["escaped-bug regression"]


async def gate_with(*bodies: str) -> MemoryGate:
    backend = InMemoryBackend()
    for body in bodies:
        await backend.add(
            graph_name("acme", Scope.RND),
            Scope.RND,
            MemoryItem("n", body, "t", datetime.now(UTC)),
        )
    return MemoryGate("acme", backend)


async def test_new_hits_lists_dependencies_and_paths_without_rnd_facts():
    gate = await gate_with("src/calc.py holds the arithmetic helpers")

    hits = await new_hits(
        gate, ["src/calc.py", "src/fresh.py", "tests/test_fresh.py"], ["left-pad"]
    )

    assert hits == ["new dependency: left-pad", "unknown area: src/fresh.py"]


async def test_new_hits_empty_for_known_paths():
    gate = await gate_with("src/calc.py holds the arithmetic helpers")

    assert await new_hits(gate, ["src/calc.py"], []) == []


class Env:
    def __init__(self, store, loop_cfg, replies, gate):
        self.store = store
        self.cfg = loop_cfg
        self.runtime = FakeRuntime({Role.LEAD: replies})
        self.task = store.create_task("acme", "tabs", "cli")
        self.asker = Asker(
            self.runtime, store, self.task.id, loop_cfg.price_per_request_usd
        )
        self.gate = gate

    async def run(self, **overrides):
        args = {
            "issue": ISSUE,
            "diff_text": DIFF,
            "engineer": ENGINEER,
            "preflight": PREFLIGHT,
            "checks": CheckOutcome(REVIEW(), QA_OK, ()),
            "changed_lines": 2,
            "fast_lane": False,
        }
        args.update(overrides)
        return await run_lead(
            self.asker, self.cfg, self.gate, self.store, self.task, **args
        )


def lead_json(*cls, conf=0.9, **extra):
    return json.dumps(
        {
            "confidence": conf,
            "classifications": [
                {"finding_id": i, "category": c, "note": "n"} for i, c in cls
            ],
            "summary": "ships tabs",
            **extra,
        }
    )


async def test_fast_lane_approves_without_llm(store, loop_cfg):
    env = Env(store, loop_cfg, [], await gate_with("src/calc.py arithmetic"))

    d = await env.run(
        fast_lane=True,
        checks=CheckOutcome(REVIEW(F("n", "style", "nit")), QA_OK, ()),
    )

    assert d == LeadDecision(
        outcome="approve",
        category="",
        findings_to_fix=(),
        follow_ups=(),
        reason="fast lane: no critical or new changes, checks passed",
        summary="fast lane: rule-based approval",
        comments=(),
        question="",
        options=(),
    )
    assert env.runtime.calls == []
    [artifact] = store.artifacts(env.task.id, ArtifactKind.LEAD)
    assert artifact.role is Role.LEAD
    assert artifact.body == json.loads(json.dumps(asdict(d)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"changed_lines": 61},
        {"checks": CheckOutcome(REVIEW(F("a", sev="minor")), QA_OK, ())},
        {"checks": CheckOutcome(REVIEW(), QA_FAIL, ())},
        {"checks": CheckOutcome(None, QA_OK, ("review: X: y",))},
        {"diff_text": DIFF.replace("src/calc.py", "src/auth.py")},
    ],
)
async def test_fast_lane_still_calls_llm_when_a_condition_fails(
    store, loop_cfg, overrides
):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))

    await env.run(fast_lane=True, **overrides)

    assert len(env.runtime.calls) == 1


async def test_fast_lane_with_new_dependency_calls_llm(store, loop_cfg):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))
    engineer = ENGINEER.model_copy(update={"new_dependencies": ["left-pad"]})

    d = await env.run(fast_lane=True, engineer=engineer)

    assert len(env.runtime.calls) == 1
    assert (d.outcome, d.category) == ("escalate", "new")


async def test_lead_sees_everything_and_searches_memory_only(store, loop_cfg):
    reply = lead_json(("a", "edge_case"))
    env = Env(store, loop_cfg, [reply], await gate_with("src/calc.py arithmetic"))
    review = REVIEW(F("a", "edge_case", "minor", path="src/calc.py"))

    d = await env.run(checks=CheckOutcome(review, QA_OK, ()))

    [call] = env.runtime.calls
    assert (call.role, call.model) == (Role.LEAD, "m-lead")
    assert call.system.startswith(load_prompt(Role.LEAD))
    assert [tool.name for tool in call.tools] == ["memory_search"]
    assert call.workdir is None and call.edits is False
    assert call.commands == () and call.command_prefixes == ()
    assert (call.max_turns, call.timeout_s) == (40, 1200)
    for part in (
        "Issue: Parser must accept tabs",
        DIFF,
        "Accept tabs",
        "tabs are whitespace",
        "tests/test_calc.py",
        "size: ok (12 lines)",
        "Blind review: r",
        "- [minor/edge_case] a src/calc.py: a\n  d",
        "QA: passed",
    ):
        assert part in call.prompt
    assert d.outcome == "approve"


async def test_prompt_reports_failures_check_errors_and_hits(store, loop_cfg):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/auth.py auth"))
    diff = DIFF.replace("src/calc.py", "src/auth.py")
    checks = CheckOutcome(None, QA_FAIL, ("review: ReplyInvalid: bad json",))

    await env.run(diff_text=diff, checks=checks)

    [call] = env.runtime.calls
    for part in (
        "QA: failed",
        "- tests failed: pytest -q -> exit 1",
        "Check errors:\n- review: ReplyInvalid: bad json",
        "Blind review: unavailable",
        "Critical changes:\n- security: src/auth.py",
    ):
        assert part in call.prompt


def followups(store, env):
    return [
        t.title
        for t in store.list_tasks("acme")
        if t.source == "followup" and t.source_ref == f"task:{env.task.id}"
    ]


async def test_edge_case_follow_ups_become_tasks(store, loop_cfg):
    env = Env(
        store,
        loop_cfg,
        [lead_json(("e", "edge_case"))],
        await gate_with("src/calc.py arithmetic"),
    )
    review = REVIEW(F("e", "edge_case", "minor"))

    d = await env.run(checks=CheckOutcome(review, QA_OK, ()))

    assert d.outcome == "approve"
    assert ids(d.follow_ups) == ["e"]
    followup = store.list_tasks("acme")[1]
    assert (followup.title, followup.source, followup.source_ref) == (
        "e",
        "followup",
        f"task:{env.task.id}",
    )
    assert followups(store, env) == ["e", "later"]


async def test_follow_up_title_is_not_filed_twice(store, loop_cfg):
    env = Env(store, loop_cfg, [], await gate_with("src/calc.py arithmetic"))
    reply = lead_json(("e", "edge_case"))
    review = REVIEW(F("e", "edge_case", "minor"))
    for _ in range(2):
        env.runtime = FakeRuntime({Role.LEAD: [reply]})
        env.asker = Asker(
            env.runtime, store, env.task.id, loop_cfg.price_per_request_usd
        )
        await env.run(checks=CheckOutcome(review, QA_OK, ()))

    assert [t.title for t in store.list_tasks("acme")] == ["tabs", "e", "later"]


async def test_no_followups_on_send_back(store, loop_cfg):
    env = Env(
        store,
        loop_cfg,
        [lead_json(("a", "real_bug"), ("e", "edge_case"))],
        await gate_with("src/calc.py arithmetic"),
    )
    review = REVIEW(F("a"), F("e", "edge_case", "minor"))

    d = await env.run(checks=CheckOutcome(review, QA_OK, ()))

    assert d.outcome == "send_back"
    assert followups(store, env) == []


async def test_no_followups_on_escalate(store, loop_cfg):
    env = Env(
        store,
        loop_cfg,
        [lead_json(("e", "edge_case"), conf=0.1)],
        await gate_with("src/calc.py arithmetic"),
    )
    review = REVIEW(F("e", "edge_case", "minor"))

    d = await env.run(checks=CheckOutcome(review, QA_OK, ()))

    assert d.outcome == "escalate"
    assert followups(store, env) == []


async def test_followups_deduped(store, loop_cfg):
    env = Env(
        store,
        loop_cfg,
        [lead_json(("e", "edge_case"), ("f", "edge_case"))],
        await gate_with("src/calc.py arithmetic"),
    )
    store.create_task("acme", "Handle  EMPTY input!", "followup", f"task:{env.task.id}")
    review = REVIEW(
        Finding(
            id="e", severity="minor", kind="edge_case", title="Tabs in CSV", detail="d"
        ),
        Finding(
            id="f",
            severity="minor",
            kind="edge_case",
            title="handle empty input",
            detail="d",
        ),
    )
    engineer = ENGINEER.model_copy(
        update={
            "follow_ups": [
                FollowUp(title="tabs in csv.", why="y"),
                FollowUp(title="Unicode  tabs", why="y"),
                FollowUp(title="unicode tabs", why="y"),
            ]
        }
    )

    await env.run(checks=CheckOutcome(review, QA_OK, ()), engineer=engineer)

    assert followups(store, env) == [
        "Handle  EMPTY input!",
        "Tabs in CSV",
        "Unicode  tabs",
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Run the tests",
        "run tests",
        "Re-run the full test suite",
        "rerun lint",
        "Execute the build",
        "Run CI again",
        "Run the linter",
        "Add tests",
        "Write more tests.",
        "add test",
    ],
)
async def test_chore_followups_dropped(store, loop_cfg, title):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))
    engineer = ENGINEER.model_copy(
        update={
            "follow_ups": [
                FollowUp(title=title, why="y"),
                FollowUp(title="Add tests for unicode tabs", why="y"),
            ]
        }
    )

    d = await env.run(engineer=engineer)

    assert d.outcome == "approve"
    assert followups(store, env) == ["Add tests for unicode tabs"]


async def test_artifact_holds_dumped_decision_with_findings_for_triage(store, loop_cfg):
    reply = lead_json(("a", "real_bug"), comments=["fix a"])
    env = Env(store, loop_cfg, [reply], await gate_with("src/calc.py arithmetic"))
    finding = F("a", path="src/calc.py")

    d = await env.run(checks=CheckOutcome(REVIEW(finding), QA_OK, ()))

    assert d.outcome == "send_back"
    [artifact] = store.artifacts(env.task.id, ArtifactKind.LEAD)
    assert artifact.body["findings_to_fix"] == [finding.model_dump()]
    assert artifact.body["comments"] == ["fix a"]
    assert artifact.body["outcome"] == "send_back"
    assert json.loads(json.dumps(artifact.body)) == artifact.body


async def test_threshold_comes_from_config(store, loop_cfg):
    env = Env(
        store,
        loop_cfg,
        [lead_json(conf=0.6)],
        await gate_with("src/calc.py arithmetic"),
    )

    d = await env.run()

    assert (d.outcome, d.category) == ("escalate", "not_confident")


def test_lead_prompt_names_findings_by_id():
    prompt = load_prompt(Role.LEAD)

    assert "`finding_id`" in prompt
    assert "its `id`" in prompt


RENAME_INTO_AUTH = (
    "diff --git a/src/old.py b/src/auth/x.py\nsimilarity index 100%\n"
    "rename from src/old.py\nrename to src/auth/x.py\n"
)
RENAME_OUT_OF_AUTH = (
    "diff --git a/src/auth/x.py b/src/util.py\nsimilarity index 90%\n"
    "rename from src/auth/x.py\nrename to src/util.py\n"
    "--- a/src/auth/x.py\n+++ b/src/util.py\n@@ -1 +1 @@\n-a\n+b\n"
)
RENAME_INTO_WORKFLOW = (
    "diff --git a/ci.yml b/.github/workflows/ci.yml\nsimilarity index 100%\n"
    "rename from ci.yml\nrename to .github/workflows/ci.yml\n"
)


def test_diff_paths_collects_old_and_new_sides_of_a_rename():
    assert diff_paths(RENAME_INTO_AUTH) == ["src/auth/x.py", "src/old.py"]
    assert diff_paths(RENAME_OUT_OF_AUTH) == ["src/auth/x.py", "src/util.py"]


def test_diff_paths_handles_plain_add_and_delete():
    text = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert diff_paths(text) == ["gone.py", "new.py"]


@pytest.mark.parametrize(
    ("diff_text", "hit"),
    [
        (RENAME_INTO_AUTH, "security: src/auth/x.py"),
        (RENAME_OUT_OF_AUTH, "security: src/auth/x.py"),
        (RENAME_INTO_WORKFLOW, "infra: .github/workflows/ci.yml"),
    ],
)
async def test_renames_are_critical_even_when_engineer_hides_them(
    store, loop_cfg, diff_text, hit
):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src arithmetic"))

    d = await env.run(fast_lane=True, diff_text=diff_text)

    assert (d.outcome, d.category) == ("escalate", "critical")
    assert hit in d.reason
    assert len(env.runtime.calls) == 1


async def test_lead_failure_with_critical_hits_escalates(store, loop_cfg):
    env = Env(store, loop_cfg, ["not json"] * 3, await gate_with("src arithmetic"))

    d = await env.run(diff_text=RENAME_INTO_AUTH)

    assert (d.outcome, d.category, d.reason) == (
        "escalate",
        "critical",
        "lead unavailable",
    )
    [artifact] = store.artifacts(env.task.id, ArtifactKind.LEAD)
    assert artifact.body["reason"] == "lead unavailable"


async def test_lead_failure_with_new_hits_escalates(store, loop_cfg):
    env = Env(store, loop_cfg, ["not json"] * 3, await gate_with("src/calc.py x"))
    engineer = ENGINEER.model_copy(update={"new_dependencies": ["left-pad"]})

    d = await env.run(engineer=engineer)

    assert (d.outcome, d.category, d.reason) == (
        "escalate",
        "new",
        "lead unavailable",
    )


async def test_lead_failure_without_hits_still_raises(store, loop_cfg):
    env = Env(store, loop_cfg, ["not json"] * 3, await gate_with("src/calc.py x"))

    with pytest.raises(ReplyInvalid):
        await env.run()


def test_qa_failed_without_details_gets_a_synthetic_finding():
    qa = QA_FAIL.model_copy(update={"failures": []})

    d = decide(L(), REVIEW(), qa, (), [], [], 0.7)

    assert d.outcome == "send_back"
    assert d.findings_to_fix == (
        Finding(
            id="qa-1",
            severity="major",
            kind="bug",
            title="QA failed without details",
            detail="QA reported a failure but listed no failing cases.",
        ),
    )


ROOT_CAUSE_LINE = (
    "Root cause required: does the invariant remove the cause, not just this symptom?"
)


async def test_lead_sends_back_missing_invariant_without_llm(store, loop_cfg):
    env = Env(store, loop_cfg, [], await gate_with("src/calc.py arithmetic"))
    engineer = ENGINEER.model_copy(update={"invariant": "  "})

    d = await env.run(engineer=engineer, fast_lane=True, require_invariant=True)

    assert env.runtime.calls == []
    assert d.outcome == "send_back"
    [finding] = d.findings_to_fix
    assert finding == Finding(
        id="root-cause",
        severity="major",
        kind="bug",
        title="State one invariant",
        detail=(
            "This area came back again: state the one rule that removes the cause, "
            "apply it, and test it."
        ),
    )
    [artifact] = store.artifacts(env.task.id, ArtifactKind.LEAD)
    assert artifact.body["outcome"] == "send_back"
    assert artifact.body["findings_to_fix"][0]["id"] == "root-cause"


async def test_lead_prompt_mentions_root_cause_when_required(store, loop_cfg):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))

    d = await env.run(require_invariant=True)

    [call] = env.runtime.calls
    assert ROOT_CAUSE_LINE in call.prompt
    assert d.outcome == "approve"


async def test_root_cause_turns_the_fast_lane_off(store, loop_cfg):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))

    await env.run(
        fast_lane=True,
        require_invariant=True,
        checks=CheckOutcome(REVIEW(F("n", "style", "nit")), QA_OK, ()),
    )

    assert len(env.runtime.calls) == 1


async def test_lead_prompt_is_unchanged_without_root_cause(store, loop_cfg):
    env = Env(store, loop_cfg, [lead_json()], await gate_with("src/calc.py arithmetic"))

    await env.run()

    assert "Root cause required" not in env.runtime.calls[0].prompt


def test_prompts_mention_playbooks_and_one_rule():
    assert "playbooks" in load_prompt(Role.SENIOR).lower()
    assert "one rule" in load_prompt(Role.SENIOR).lower()
    assert "invariant" in load_prompt(Role.LEAD)
    assert "handled by code" in load_prompt(Role.TRIAGE)


def parent_reply(**kw):
    return json.dumps(
        {"approve": True, "confidence": 0.9, "reason": "covered", "summary": "done"}
        | kw
    )


def _adr():
    from agentco.agents.architect import ADRReply

    return ADRReply.model_validate(
        {
            "title": "Plugin registry",
            "context": "c",
            "decision": "d",
            "consequences": "q",
            "sub_tasks": [
                {"title": "step 0", "acceptance_criteria": ["works"], "size": "s"}
            ],
        }
    )


CHILDREN = [
    ChildSummary(2, "step 0", "merged", "https://x/pull/9", "adds the registry")
]


async def _review_parent(store, loop_cfg, reply, *, repeat=1, **kwargs):
    runtime = FakeRuntime({Role.LEAD: [reply] * repeat})
    task = store.create_task("acme", "tree", "cli")
    store.put_artifact(task.id, ArtifactKind.ISSUE, Role.PM, ISSUE.model_dump())
    asker = Asker(runtime, store, task.id, loop_cfg.price_per_request_usd)
    gate = MemoryGate("acme", InMemoryBackend())
    out = await review_parent(
        asker, loop_cfg, gate, store, task, adr=_adr(), children=CHILDREN, **kwargs
    )
    return runtime, task, out


async def test_review_parent_stores_lead_artifact_in_plan4_shape(store, loop_cfg):
    runtime, task, reply = await _review_parent(store, loop_cfg, parent_reply())

    assert reply.approve and reply.confidence == 0.9
    [call] = runtime.calls
    assert call.role is Role.LEAD and call.model == "m-lead" and call.edits is False
    assert call.system.startswith(load_prompt(Role.LEAD, "parent"))
    assert "# ADR: Plugin registry" in call.prompt
    assert "Parser must accept tabs" in call.prompt
    assert "#2 step 0 [merged] https://x/pull/9" in call.prompt
    assert "lead summary: adds the registry" in call.prompt
    [lead] = store.artifacts(task.id, ArtifactKind.LEAD)
    assert lead.body["source"] == "parent"
    assert lead.body["outcome"] == "approve" and lead.body["category"] == ""
    assert {"findings_to_fix", "follow_ups", "comments", "options"} <= set(lead.body)


async def test_review_parent_low_confidence_or_no_approve_escalates(store, loop_cfg):
    _, task, _ = await _review_parent(
        store, loop_cfg, parent_reply(confidence=0.2, reason="unsure")
    )
    [lead] = store.artifacts(task.id, ArtifactKind.LEAD)
    assert (lead.body["outcome"], lead.body["category"]) == ("escalate", "tradeoff")
    assert lead.body["reason"] == "unsure"

    _, task, _ = await _review_parent(store, loop_cfg, parent_reply(approve=False))
    [lead] = store.artifacts(task.id, ArtifactKind.LEAD)
    assert lead.body["outcome"] == "escalate"


async def test_review_parent_human_note_in_prompt(store, loop_cfg):
    runtime, _, _ = await _review_parent(
        store, loop_cfg, parent_reply(), human_note="criterion 2 is out of scope"
    )
    assert "Human guidance:\ncriterion 2 is out of scope" in runtime.calls[0].prompt


async def test_review_parent_rejects_out_of_range_confidence(store, loop_cfg):
    with pytest.raises(ReplyInvalid):
        await _review_parent(store, loop_cfg, parent_reply(confidence=1.5), repeat=2)
