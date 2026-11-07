"""QA's regression tests become a follow-up task after the fix is merged.

The test files QA writes come from an LLM in a snapshot, so they are untrusted on
both ends: every path is checked when they are read out of the snapshot and again
when they are written into the follow-up task's worktree.
"""

from pathlib import Path, PurePosixPath

from agentco.roles import Role
from agentco.sandbox.affected import is_test_path
from agentco.store.models import ArtifactKind, TaskRecord
from agentco.store.task_store import TaskStore
from agentco.workflow.states import TaskState

FOLLOWUP_SOURCE = "qa-followup"
MAX_TEST_FILES = 10
MAX_TEST_CHARS = 20_000
_TITLE_LIMIT = 72


def followup_marker(tenant: str, task_id: int) -> str:
    return f"qa-followup:{tenant}:{task_id}"


def _safe_target(root: Path, rel: str) -> Path | None:
    """`root/rel` when it is a test path that stays inside `root`, else None."""
    if not rel or "\\" in rel or "\x00" in rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        return None
    if str(pure) != rel or not is_test_path(rel):
        return None
    here = root
    for part in pure.parts:
        here = here / part
        if here.is_symlink():
            return None
    try:
        here.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return here


def collect_test_sources(root: Path, paths: list[str]) -> dict[str, str]:
    """The text of the listed test files under `root`, skipping anything unsafe."""
    found: dict[str, str] = {}
    for rel in paths:
        if len(found) >= MAX_TEST_FILES:
            break
        target = _safe_target(root, rel)
        if target is None or not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) <= MAX_TEST_CHARS:
            found[rel] = text
    return found


def write_test_files(root: Path, sources: object) -> list[str]:
    """Write the safe entries of `sources` under `root`; return the paths written."""
    if not isinstance(sources, dict):
        return []
    written: list[str] = []
    for rel, text in sources.items():
        if len(written) >= MAX_TEST_FILES:
            break
        if not isinstance(rel, str) or not isinstance(text, str):
            continue
        if len(text) > MAX_TEST_CHARS:
            continue
        target = _safe_target(root, rel)
        if target is None or (target.exists() and not target.is_file()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _safe_target(root, rel):  # a parent created above must still be inside
            continue
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


def create_qa_followup(store: TaskStore, task: TaskRecord) -> TaskRecord | None:
    """The follow-up task for a merged task's QA tests; None when there is none to make."""
    if task.state is not TaskState.MERGED or task.source == FOLLOWUP_SOURCE:
        return None
    qa = store.latest_artifact(task.id, ArtifactKind.QA)
    sources = qa.body.get("new_test_sources") if qa else None
    if not sources or not isinstance(sources, dict):
        return None
    key = followup_marker(task.tenant, task.id)
    existing = store.get_meta(key)
    if existing is not None:
        # A crash between the task and its artifacts must not leave an empty task.
        _complete(store, store.get(int(existing)), task, sources)
        return None
    title = f"QA regression tests for #{task.id}: {task.title}"[:_TITLE_LIMIT]
    created = store.create_task(
        task.tenant, title, FOLLOWUP_SOURCE, f"task:{task.id}", parent_id=task.id
    )
    store.set_meta(key, str(created.id))
    _complete(store, created, task, sources)
    return created


def _complete(
    store: TaskStore, created: TaskRecord, parent: TaskRecord, sources: dict
) -> None:
    if store.latest_artifact(created.id, ArtifactKind.REQUEST) is not None:
        return
    target = store.latest_artifact(parent.id, ArtifactKind.TARGET)
    if target is not None:
        store.put_artifact(created.id, ArtifactKind.TARGET, Role.HUMAN, target.body)
    store.put_artifact(
        created.id,
        ArtifactKind.REQUEST,
        Role.HUMAN,
        {
            "body": (
                f"Add QA's regression tests for the merged fix of task #{parent.id}. "
                "They must pass on main."
            ),
            "test_files": sources,
        },
    )
