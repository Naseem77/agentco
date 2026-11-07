import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentco.agents.qa import NeighbourRow, QAFailure, QAReply
from agentco.config import Thresholds
from agentco.learning.lessons import (
    KIND_SCOPE,
    Lesson,
    LessonWriter,
    bump_overrides,
    lesson_item,
    lesson_key,
    lessons_from_adr,
    lessons_from_lead,
    lessons_from_pr_review,
    lessons_from_qa,
)
from agentco.memory.backend import InMemoryBackend, MemoryItem
from agentco.memory.entities import ENTITY_TYPES
from agentco.memory.gate import AccessDenied, MemoryGate
from agentco.memory.scopes import Scope
from agentco.roles import Role
from agentco.store.models import Artifact, ArtifactKind

NOW = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def gate(backend):
    return MemoryGate("acme", backend)


def all_items(backend):
    return [i for s in Scope for i in backend.items(f"acme__{s.value}")]


def art(kind, body, id=3, task_id=1, role=Role.LEAD):
    return Artifact(
        id=id, task_id=task_id, round=0, kind=kind, role=role, body=body, created_at=NOW
    )


def qa_artifact(failures=None, **kw):
    reply = QAReply(
        confidence=0.9,
        passed=not failures,
        bug_class=kw.pop("bug_class", "off-by-one"),
        tests_run=kw.pop("tests_run", ["pytest tests/x"]),
        neighbour_matrix=kw.pop("neighbour_matrix", []),
        failures=failures or [],
        **kw,
    )
    return art(ArtifactKind.QA, reply.model_dump(), id=5, role=Role.QA)


RULE = Lesson(
    kind="ReviewRule",
    text="Validate input at the API edge",
    source="task:1:lead:3",
    origin="lead",
)


async def test_write_then_rerun_is_duplicate(store, gate, backend):
    w = LessonWriter(store, gate, "acme")
    assert await w.write(RULE, NOW) == "added"
    assert await w.write(RULE, NOW) == "duplicate"
    assert len(backend.items("acme__rnd")) == 1


async def test_rerun_is_decided_by_the_marker_without_searching(store, gate, backend):
    w = LessonWriter(store, gate, "acme")
    await w.write(RULE, NOW)
    assert store.get_meta(lesson_key("acme", RULE)) == "added"
    backend.items("acme__rnd").clear()  # a search would now find nothing and re-add
    assert await w.write(RULE, NOW) == "duplicate"


async def test_crash_after_add_before_marker_is_healed(store, gate, backend):
    # the item is in memory but the marker never got written
    await gate.for_role(Role.LIBRARIAN).add(Scope.RND, lesson_item(RULE, NOW))
    assert store.get_meta(lesson_key("acme", RULE)) is None
    w = LessonWriter(store, gate, "acme")
    assert await w.write(RULE, NOW) == "duplicate"
    assert store.get_meta(lesson_key("acme", RULE)) == "duplicate"
    assert len(backend.items("acme__rnd")) == 1


async def test_existing_fact_counts_as_duplicate(store, gate, backend):
    # a fact the importer or a human wrote, in other case and with digits
    imported = MemoryItem(
        name="imported",
        body="ReviewRule (origin human): VALIDATE input at the API edge, see PR 42",
        source_description="agentco importer",
        reference_time=NOW,
    )
    await gate.for_role(Role.HUMAN).add(Scope.RND, imported)
    w = LessonWriter(store, gate, "acme")
    assert await w.write(RULE, NOW) == "duplicate"
    assert backend.items("acme__rnd") == [imported]


async def test_similar_but_different_fact_is_not_duplicate(store, gate, backend):
    other = MemoryItem(
        name="o",
        body="Validate input at the API edge only for public routes",
        source_description="x",
        reference_time=NOW,
    )
    await gate.for_role(Role.HUMAN).add(Scope.RND, other)
    w = LessonWriter(store, gate, "acme")
    longer = RULE.model_copy(update={"text": "Validate input at the API edge always"})
    assert await w.write(longer, NOW) == "added"
    assert len(backend.items("acme__rnd")) == 2


async def test_same_text_in_other_scope_is_not_a_duplicate(store, gate, backend):
    await gate.for_role(Role.HUMAN).add(
        Scope.COMPANY,
        MemoryItem("p", "Validate input at the API edge", "x", NOW),
    )
    assert await LessonWriter(store, gate, "acme").write(RULE, NOW) == "added"
    assert len(backend.items("acme__rnd")) == 1


async def test_lessons_land_in_their_kind_scope(store, gate, backend):
    w = LessonWriter(store, gate, "acme")
    for kind in ("ReviewRule", "EscalationRule", "Policy", "EscapedBug"):
        lesson = Lesson(kind=kind, text=f"lesson for {kind}", source="task:1:lead:3")
        assert await w.write(lesson, NOW) == "added"
    names = {
        s.value: [i.name.split(":")[0] for i in backend.items(f"acme__{s.value}")]
        for s in Scope
    }
    assert names == {
        "company": ["EscalationRule", "Policy"],
        "product": [],
        "rnd": ["ReviewRule"],
        "qa": ["EscapedBug"],
    }


async def test_every_item_has_provenance(store, gate, backend):
    w = LessonWriter(store, gate, "acme")
    lessons = [
        *lessons_from_lead(
            art(ArtifactKind.LEAD, {"comments": ["Wrap writes in a transaction"]}),
            ["src/db"],
        ),
        *lessons_from_qa(
            qa_artifact([QAFailure(title="t", steps="s", expected="e", actual="a")])
        ),
    ]
    assert len(lessons) == 2
    for lesson in lessons:
        await w.write(lesson, NOW)
    items = all_items(backend)
    assert len(items) == 2
    for item in items:
        assert re.search(r"\nSource: task:1:(lead|qa):\d+$", item.body)
        assert item.source_description.startswith("agentco librarian: task:1:")


def test_lesson_item_shape():
    lesson = Lesson(
        kind="ReviewRule",
        text="Validate input",
        source="task:1:lead:3",
        modules=("src/api", "src/db"),
        origin="lead",
    )
    item = lesson_item(lesson, NOW)
    key_hash = lesson_key("acme", lesson).rsplit(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{16}", key_hash)
    assert item == MemoryItem(
        name=f"ReviewRule:{key_hash}",
        body=(
            "ReviewRule (origin lead): Validate input"
            "\nApplies to: src/api, src/db"
            "\nSource: task:1:lead:3"
        ),
        source_description="agentco librarian: task:1:lead:3",
        reference_time=NOW,
    )


def test_lesson_item_without_modules_has_no_applies_to():
    assert lesson_item(RULE, NOW).body == (
        "ReviewRule (origin lead): Validate input at the API edge"
        "\nSource: task:1:lead:3"
    )


def test_lesson_key_ignores_case_digits_and_spacing_but_not_kind_or_tenant():
    a = Lesson(kind="ReviewRule", text="Use  RETRIES 3 times", source="s")
    b = Lesson(kind="ReviewRule", text="use retries 5 times", source="other")
    assert lesson_key("acme", a) == lesson_key("acme", b)
    assert lesson_key("acme", a).startswith("librarian:acme:lesson:")
    assert lesson_key("acme", a) != lesson_key("other", a)
    assert lesson_key("acme", a) != lesson_key(
        "acme", a.model_copy(update={"kind": "Playbook"})
    )


def test_lesson_text_is_one_line_and_bounded():
    assert Lesson(kind="Policy", text="a\n  b\tc", source="s").text == "a b c"
    assert Lesson(kind="Policy", text="x" * 500, source="s").text == "x" * 500
    with pytest.raises(ValidationError):
        Lesson(kind="Policy", text="x" * 501, source="s")
    with pytest.raises(ValidationError):
        Lesson(kind="Policy", text="  \n ", source="s")
    with pytest.raises(ValidationError):
        Lesson(kind="Nope", text="x", source="s")
    with pytest.raises(ValidationError):
        Lesson(kind="Policy", text="x", source="s", origin="robot")


def test_no_kind_maps_to_product():
    assert Scope.PRODUCT not in KIND_SCOPE.values()


def test_kind_scope_is_derived_from_entity_types():
    assert KIND_SCOPE == {
        "ReviewRule": Scope.RND,
        "Playbook": Scope.RND,
        "Issue": Scope.RND,
        "Risk": Scope.RND,
        "AgentProfile": Scope.RND,
        "EscalationRule": Scope.COMPANY,
        "Policy": Scope.COMPANY,
        "EscapedBug": Scope.QA,
        "NeighbourCase": Scope.QA,
        "Decision": Scope.RND,
    }
    for kind, scope in KIND_SCOPE.items():
        assert kind in ENTITY_TYPES[scope]


async def test_librarian_can_write_every_kind_scope_but_not_product(gate):
    memory = gate.for_role(Role.LIBRARIAN)
    for kind, scope in KIND_SCOPE.items():
        await memory.add(scope, MemoryItem(kind, kind, "x", NOW))
    with pytest.raises(AccessDenied):
        await memory.add(Scope.PRODUCT, MemoryItem("p", "p", "x", NOW))


def test_lessons_from_lead_makes_review_rules_with_origin_lead():
    a = art(
        ArtifactKind.LEAD,
        {"comments": ["Use the shared retry helper", "", "  ", "Keep handlers thin"]},
        id=7,
        task_id=4,
    )
    lessons = lessons_from_lead(a, ["src/api", "src/api", "src/db"])
    assert lessons == [
        Lesson(
            kind="ReviewRule",
            text="Use the shared retry helper",
            source="task:4:lead:7",
            modules=("src/api", "src/db"),
            origin="lead",
        ),
        Lesson(
            kind="ReviewRule",
            text="Keep handlers thin",
            source="task:4:lead:7",
            modules=("src/api", "src/db"),
            origin="lead",
        ),
    ]


def test_lessons_from_lead_without_comments():
    assert lessons_from_lead(art(ArtifactKind.LEAD, {"comments": []}), []) == []
    assert lessons_from_lead(art(ArtifactKind.LEAD, {}), []) == []


def test_lessons_from_lead_clips_long_comment():
    [lesson] = lessons_from_lead(art(ArtifactKind.LEAD, {"comments": ["w " * 400]}), [])
    assert len(lesson.text) <= 500 and lesson.text.endswith("…")


def test_qa_lesson_has_no_qa_reasoning():
    a = qa_artifact(
        [
            QAFailure(
                title="Login 500s", steps="post bad json", expected="400", actual="500"
            )
        ],
        bug_class="SECRET-BUG-CLASS",
        tests_run=["SECRET-TEST"],
        neighbour_matrix=[
            NeighbourRow(case="SECRET-CASE", result="fail", evidence="e")
        ],
    )
    lessons = lessons_from_qa(a)
    texts = " ".join(l.text for l in lessons)
    assert "SECRET" not in texts
    assert all(KIND_SCOPE[l.kind] is Scope.RND for l in lessons)
    assert [(l.kind, l.source, l.origin) for l in lessons] == [
        ("Issue", "task:1:qa:5", "librarian")
    ]
    for part in ("Login 500s", "post bad json", "400", "500"):
        assert part in texts


def test_qa_lessons_one_per_failure_and_none_when_passing():
    failures = [
        QAFailure(title=f"bug {n}", steps="s", expected="e", actual="a") for n in "xy"
    ]
    lessons = lessons_from_qa(qa_artifact(failures))
    assert [l.text.count("bug") for l in lessons] == [1, 1]
    assert lessons_from_qa(qa_artifact()) == []


def test_qa_lesson_stays_within_text_limit_for_huge_failure():
    big = "z" * 3000
    failure = QAFailure(title=big, steps=big, expected=big, actual=big)
    [lesson] = lessons_from_qa(qa_artifact([failure]))
    assert len(lesson.text) <= 500


def pr_review(classifications, fix, recurrences=(), pr=12):
    return art(
        ArtifactKind.PR_REVIEW,
        {
            "pr": pr,
            "consumed": [1],
            "classifications": classifications,
            "recurrences": list(recurrences),
            "decision": {
                "outcome": "reenter",
                "findings_to_fix": fix,
                "follow_ups": [],
            },
        },
        id=9,
        task_id=2,
    )


def cls(fid, category, fp=None):
    return {
        "finding_id": fid,
        "fingerprint": fp or f"fp-{fid}",
        "category": category,
        "note": f"note {fid}",
    }


def fnd(fid, title, path="src/a.py"):
    return {
        "id": fid,
        "severity": "major",
        "kind": "bug",
        "path": path,
        "line": 3,
        "title": title,
        "detail": "d",
    }


def test_pr_real_bug_gives_risk_escaped_bug_and_neighbour_case():
    a = pr_review(
        [cls("c1", "real_bug"), cls("c2", "covered_style"), cls("c3", "edge_case")],
        [fnd("c1", "Null deref in parse"), fnd("c2", "rename var")],
    )
    lessons = lessons_from_pr_review(a, "null-handling")
    assert lessons == [
        Lesson(
            kind="Risk",
            text="Null deref in parse (src/a.py)",
            source="task:2:pr_review:9",
        ),
        Lesson(
            kind="EscapedBug",
            text="Null deref in parse (src/a.py), escaped to PR #12",
            source="task:2:pr_review:9",
        ),
        Lesson(
            kind="NeighbourCase",
            text="bug class null-handling: also test the neighbours of Null deref in parse",
            source="task:2:pr_review:9",
        ),
    ]
    assert [KIND_SCOPE[l.kind] for l in lessons] == [Scope.RND, Scope.QA, Scope.QA]


def test_pr_recurrence_adds_nothing():
    a = pr_review(
        [cls("c1", "real_bug", fp="again"), cls("c2", "real_bug")],
        [fnd("c1", "Same bug again"), fnd("c2", "Fresh bug")],
        recurrences=["again"],
    )
    texts = [l.text for l in lessons_from_pr_review(a, "x")]
    assert len(texts) == 3
    assert all("Fresh bug" in t for t in texts)


def test_pr_without_real_bug_gives_nothing():
    a = pr_review([cls("c1", "known"), cls("c2", "tradeoff")], [])
    assert lessons_from_pr_review(a, "x") == []


def test_pr_real_bug_without_details_falls_back_to_note_and_no_bug_class():
    a = pr_review([cls("k1", "real_bug")], [])
    lessons = lessons_from_pr_review(a, "")
    assert [(l.kind, l.text) for l in lessons] == [
        ("Risk", "note k1"),
        ("EscapedBug", "note k1, escaped to PR #12"),
        ("NeighbourCase", "also test the neighbours of note k1"),
    ]


def test_bump_overrides_reach_stale_after(store):
    assert Thresholds().stale_after == 3
    assert bump_overrides(store, "acme", ["fp1", "fp2"]) == []
    assert bump_overrides(store, "acme", ["fp1"]) == []
    assert bump_overrides(store, "acme", ["fp1", "fp1"]) == ["fp1"]
    assert store.get_meta("librarian:acme:override:fp1") == "3"
    assert store.get_meta("librarian:acme:override:fp2") == "1"
    assert bump_overrides(store, "other", ["fp1"]) == []


def test_bump_overrides_honours_configured_threshold(store):
    assert bump_overrides(store, "acme", ["a"], stale_after=1) == ["a"]
    assert bump_overrides(store, "acme", [], stale_after=1) == []


def test_stale_after_must_be_positive():
    with pytest.raises(ValidationError):
        Thresholds(stale_after=0)


def adr_artifact(id=3, **kw):
    adr = {
        "title": "Plugin registry",
        "decision": "Use a registry.",
        "consequences": "One more module.",
        **kw,
    }
    return art(
        ArtifactKind.ADR, {"adr": adr, "human_note": ""}, id=id, role=Role.ARCHITECT
    )


def decision(id, verdict):
    return art(
        ArtifactKind.HUMAN_DECISION,
        {"decision": verdict, "note": ""},
        id=id,
        role=Role.HUMAN,
    )


def test_lessons_from_adr_waits_for_decision_then_writes_decision():
    adr = adr_artifact()

    assert lessons_from_adr(adr, [adr]) is None
    [lesson] = lessons_from_adr(adr, [adr, decision(5, "approve")])

    assert lesson.kind == "Decision"
    assert KIND_SCOPE[lesson.kind] is Scope.RND
    assert lesson.origin == "human" and lesson.modules == ()
    assert lesson.source == "task:1:adr:3"
    assert lesson.text == (
        "Plugin registry: Use a registry. Consequences: One more module."
    )


def test_rejected_adr_writes_nothing():
    adr = adr_artifact()
    assert lessons_from_adr(adr, [adr, decision(5, "reject")]) == []
    assert lessons_from_adr(adr, [adr, decision(5, "answer")]) == []


def test_only_the_first_later_decision_counts():
    adr = adr_artifact()
    both = [adr, decision(5, "answer"), decision(7, "approve")]
    assert lessons_from_adr(adr, both) == []
    assert lessons_from_adr(adr, [decision(2, "approve"), adr]) is None


def test_long_adr_text_is_clipped_to_the_lesson_limit():
    adr = adr_artifact(decision="d" * 900)
    [lesson] = lessons_from_adr(adr, [adr, decision(5, "approve")])
    assert len(lesson.text) <= 500
