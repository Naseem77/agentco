import pytest

from agentco.learning.followup import (
    FOLLOWUP_SOURCE,
    MAX_TEST_CHARS,
    collect_test_sources,
    create_qa_followup,
    followup_marker,
    write_test_files,
)
from agentco.learning.librarian import Librarian
from agentco.llm.fake import FakeRuntime
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.roles import Role
from agentco.store.models import ArtifactKind as K
from agentco.workflow.states import TaskState

SOURCES = {"tests/test_edge.py": "def test_edge():\n    assert True\n"}
PATH = [
    TaskState.PM,
    TaskState.TRIAGE,
    TaskState.ENGINEERING,
    TaskState.PREFLIGHT,
    TaskState.INTERNAL_REVIEW,
    TaskState.LEAD_DECISION,
    TaskState.PR_OPEN,
]


def task_in(store, final, *, source="cli", title="Fix the parser", sources=SOURCES):
    task = store.create_task("acme", title, source, "12")
    for step in PATH:
        store.transition(task.id, step, Role.LEAD, "t")
    if final is not TaskState.PR_OPEN:
        store.transition(task.id, final, Role.HUMAN, "t")
    if sources is not None:
        store.put_artifact(
            task.id,
            K.QA,
            Role.QA,
            {"passed": True, "new_tests": list(sources), "new_test_sources": sources},
        )
    return store.get(task.id)


def test_followup_created_once_after_merge(store):
    parent = task_in(store, TaskState.MERGED, title="Fix the parser")
    store.put_artifact(
        parent.id, K.TARGET, Role.HUMAN, {"repo": "acme/app", "main": "dev"}
    )

    created = create_qa_followup(store, parent)

    assert created is not None
    assert (created.tenant, created.title, created.source, created.source_ref) == (
        "acme",
        "QA regression tests for #1: Fix the parser",
        FOLLOWUP_SOURCE,
        "task:1",
    )
    assert created.parent_id == parent.id and created.state is TaskState.INTAKE
    [request] = store.artifacts(created.id, K.REQUEST)
    assert request.body == {
        "body": (
            "Add QA's regression tests for the merged fix of task #1. "
            "They must pass on main."
        ),
        "test_files": SOURCES,
    }
    [target] = store.artifacts(created.id, K.TARGET)
    assert target.body == {"repo": "acme/app", "main": "dev"}
    assert store.get_meta(followup_marker("acme", parent.id)) == str(created.id)

    assert create_qa_followup(store, parent) is None
    assert [t.id for t in store.list_tasks("acme", tuple(TaskState))] == [
        parent.id,
        created.id,
    ]


def test_followup_without_a_parent_target_has_no_target(store):
    parent = task_in(store, TaskState.MERGED)
    created = create_qa_followup(store, parent)
    assert store.artifacts(created.id, K.TARGET) == []


def test_followup_title_is_clipped(store):
    parent = task_in(store, TaskState.MERGED, title="x" * 200)
    created = create_qa_followup(store, parent)
    assert created.title == ("QA regression tests for #1: " + "x" * 200)[:72]


@pytest.mark.parametrize(
    ("final", "sources", "source"),
    [
        (TaskState.MERGED, None, "cli"),
        (TaskState.MERGED, {}, "cli"),
        (TaskState.CANCELLED, SOURCES, "cli"),
        (TaskState.PR_OPEN, SOURCES, "cli"),
        (TaskState.MERGED, SOURCES, FOLLOWUP_SOURCE),
    ],
    ids=["no-qa", "no-sources", "cancelled", "pr-open", "followup-of-followup"],
)
def test_no_followup_without_sources_or_when_not_merged(store, final, sources, source):
    parent = task_in(store, final, sources=sources, source=source)

    assert create_qa_followup(store, parent) is None
    assert [t.id for t in store.list_tasks("acme", tuple(TaskState))] == [parent.id]
    assert store.get_meta(followup_marker("acme", parent.id)) is None


def test_a_failed_qa_artifact_has_no_sources(store):
    parent = task_in(store, TaskState.MERGED, sources=None)
    store.put_artifact(parent.id, K.QA, Role.QA, {"error": "qa: boom"})
    assert create_qa_followup(store, parent) is None


def test_only_the_latest_qa_artifact_counts(store):
    parent = task_in(store, TaskState.MERGED)
    store.put_artifact(
        parent.id, K.QA, Role.QA, {"passed": True, "new_test_sources": {}}
    )
    assert create_qa_followup(store, parent) is None


def test_a_crash_between_task_and_request_is_repaired_on_rerun(store):
    parent = task_in(store, TaskState.MERGED)
    half = store.create_task("acme", "half", FOLLOWUP_SOURCE, "task:1", parent_id=1)
    store.set_meta(followup_marker("acme", parent.id), str(half.id))

    assert create_qa_followup(store, parent) is None

    [request] = store.artifacts(half.id, K.REQUEST)
    assert request.body["test_files"] == SOURCES
    assert create_qa_followup(store, parent) is None
    assert len(store.artifacts(half.id, K.REQUEST)) == 1


async def test_the_librarian_creates_the_followup_once_on_merge(loop_cfg, store):
    parent = task_in(store, TaskState.MERGED)
    lib = Librarian(
        loop_cfg, store, MemoryGate("acme", InMemoryBackend()), FakeRuntime({})
    )

    await lib.learn_task(parent.id)
    await lib.learn_task(parent.id)

    followups = [t for t in store.list_tasks("acme", tuple(TaskState)) if t.id != 1]
    assert [(t.source, t.parent_id) for t in followups] == [(FOLLOWUP_SOURCE, 1)]


# -- paths ---------------------------------------------------------------


def tree(root):
    (root / "tests").mkdir(parents=True)
    return root


def test_collect_reads_only_safe_test_files(tmp_path):
    root = tree(tmp_path / "snap")
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n")
    (root / "tests/test_ok.py").write_text("ok\n")
    (root / "tests/test_link.py").symlink_to(outside)
    (root / "tests/linked").symlink_to(tmp_path)
    (root / "tests/test_dir.py").mkdir()
    (root / "tests/test_bin.py").write_bytes(b"\xff\xfe\x00")
    (root / "helper.py").write_text("x\n")
    (tmp_path / "test_up.py").write_text("up\n")

    found = collect_test_sources(
        root,
        [
            "../test_up.py",
            "tests/../../test_up.py",
            str(tmp_path / "test_up.py"),
            "tests/test_link.py",
            "tests/linked/secret.py",
            "tests/test_dir.py",
            "tests/test_bin.py",
            "tests//test_ok.py",
            "./tests/test_ok.py",
            "tests\\test_ok.py",
            "helper.py",
            "tests/test_none.py",
            "",
            "tests/test_ok.py",
        ],
    )

    assert found == {"tests/test_ok.py": "ok\n"}


def test_write_creates_directories_and_returns_what_it_wrote(tmp_path):
    written = write_test_files(
        tmp_path,
        {"tests/unit/test_deep.py": "deep\n", "test_top.py": "top\n"},
    )

    assert written == ["tests/unit/test_deep.py", "test_top.py"]
    assert (tmp_path / "tests/unit/test_deep.py").read_text() == "deep\n"
    assert (tmp_path / "test_top.py").read_text() == "top\n"


def test_write_refuses_escapes_symlinks_and_oversize(tmp_path):
    root = tmp_path / "wt"
    (root / "tests").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "tests/link").symlink_to(outside)
    (root / "tests/test_s.py").symlink_to(outside / "target.py")
    (root / "tests/test_d.py").mkdir()

    written = write_test_files(
        root,
        {
            "../test_up.py": "x",
            "/abs/test_a.py": "x",
            "tests/link/test_out.py": "x",
            "tests/test_s.py": "x",
            "tests/test_d.py": "x",
            "src/helper.py": "x",
            "tests/test_big.py": "y" * (MAX_TEST_CHARS + 1),
            "tests/test_num.py": 5,
            "tests/test_ok.py": "ok",
        },
    )

    assert written == ["tests/test_ok.py"]
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "test_up.py").exists()
    assert not (root / "tests/test_big.py").exists()


def test_write_caps_the_file_count(tmp_path):
    sources = {f"tests/test_{i}.py": "x" for i in range(12)}
    assert write_test_files(tmp_path, sources) == list(sources)[:10]


def test_write_ignores_a_body_that_is_not_a_mapping(tmp_path):
    assert write_test_files(tmp_path, ["tests/test_a.py"]) == []
    assert write_test_files(tmp_path, None) == []
