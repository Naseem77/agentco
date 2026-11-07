import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from agentco.llm.fake import FakeRuntime
from agentco.memory.backend import InMemoryBackend
from agentco.memory.gate import MemoryGate
from agentco.repo_contract import CONTRACT_FILE, load_repo_contract
from agentco.roles import Role
from agentco.store.models import ArtifactKind
from agentco.workflow.onboard import ONBOARD_SOURCE, onboard_repo
from agentco.workflow.orchestrator import Orchestrator, StepFailed, TaskContext
from agentco.workflow.states import TaskState

REPO = "acme/app"
CONTRACT = {
    "build": "make build",
    "test": "pytest",
    "e2e": None,
    "lint": "ruff check",
    "sandbox": {"devcontainer": ".devcontainer"},
}


def proposal(contract: dict | None = None, rationale: str = "from the Makefile") -> str:
    return json.dumps(
        {"contract_yaml": yaml.safe_dump(contract or CONTRACT), "rationale": rationale}
    )


@pytest.fixture
def env(store, loop_cfg, make_repo):
    repo = make_repo({"README.md": "hello\n"})
    runtime = FakeRuntime({Role.ARCHITECT: [proposal(), proposal(), proposal()]})
    gate = MemoryGate("acme", InMemoryBackend())

    def decide(task_id, decision, note=""):
        store.put_artifact(
            task_id,
            ArtifactKind.HUMAN_DECISION,
            Role.HUMAN,
            {"decision": decision, "note": note},
        )

    def git_status():
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    return SimpleNamespace(
        args=(loop_cfg, store, runtime, gate, REPO, repo),
        store=store,
        repo=repo,
        runtime=runtime,
        decide=decide,
        git_status=git_status,
        cfg=loop_cfg,
        gate=gate,
    )


def onboard_tasks(store):
    return [t for t in store.list_tasks("acme") if t.source == ONBOARD_SOURCE]


async def test_onboard_proposes_then_waits_then_writes(env):
    r1 = await onboard_repo(*env.args)
    assert r1.status == "proposed" and not (env.repo / CONTRACT_FILE).exists()
    assert yaml.safe_load(r1.contract_yaml)["test"] == "pytest"
    assert (await onboard_repo(*env.args)).status == "waiting"
    assert not (env.repo / CONTRACT_FILE).exists()
    env.decide(r1.task_id, "approve")

    r3 = await onboard_repo(*env.args)

    assert r3.status == "written" and load_repo_contract(env.repo).test == "pytest"
    assert env.store.get(r1.task_id).state is TaskState.MERGED
    assert env.git_status() == "?? .agentco.yml"
    esc = env.store.latest_artifact(r1.task_id, ArtifactKind.ESCALATION).body
    assert (esc["category"], esc["step"], esc["from_state"]) == (
        "new",
        "contract",
        "architect",
    )
    assert len(env.runtime.calls) == 1


async def test_nothing_is_written_without_an_approve(env):
    r1 = await onboard_repo(*env.args)

    for decision in ("answer", "reject"):
        env.decide(r1.task_id, decision, "note")
        if decision == "reject":
            env.store.transition(r1.task_id, TaskState.CANCELLED, Role.HUMAN, "t")
        await onboard_repo(*env.args)
        assert not (env.repo / CONTRACT_FILE).exists()


async def test_stale_approve_before_the_escalation_does_not_write(env):
    task = env.store.create_task("acme", "Onboard", ONBOARD_SOURCE, REPO)
    env.decide(task.id, "approve")

    r = await onboard_repo(*env.args)

    assert r.status == "proposed" and not (env.repo / CONTRACT_FILE).exists()


async def test_existing_file_is_never_overwritten(env):
    r1 = await onboard_repo(*env.args)
    (env.repo / CONTRACT_FILE).write_text("build: mine\n")
    env.decide(r1.task_id, "approve")

    r = await onboard_repo(*env.args)

    assert r.status == "kept"
    assert (env.repo / CONTRACT_FILE).read_text() == "build: mine\n"
    assert env.store.get(r1.task_id).state is TaskState.MERGED


async def test_existing_file_with_no_onboarding_task_is_exists(env):
    (env.repo / CONTRACT_FILE).write_text("build: mine\n")

    r = await onboard_repo(*env.args)

    assert r.status == "exists" and r.task_id is None
    assert env.runtime.calls == [] and onboard_tasks(env.store) == []


async def test_crash_after_write_is_recognised_on_rerun(env):
    r1 = await onboard_repo(*env.args)
    env.decide(r1.task_id, "approve")
    (env.repo / CONTRACT_FILE).write_text(r1.contract_yaml)

    r = await onboard_repo(*env.args)

    assert r.status == "written"
    assert env.store.get(r1.task_id).state is TaskState.MERGED


async def test_answer_reproposes_with_note(env):
    r1 = await onboard_repo(*env.args)
    env.decide(r1.task_id, "answer", "we use tox")

    r2 = await onboard_repo(*env.args)

    assert r2.status == "proposed" and r2.task_id == r1.task_id
    assert "Human guidance:\nwe use tox" in env.runtime.calls[1].prompt
    assert not (env.repo / CONTRACT_FILE).exists()
    assert (await onboard_repo(*env.args)).status == "waiting"
    assert len(env.store.artifacts(r1.task_id, ArtifactKind.CONTRACT)) == 2


async def test_approve_after_answer_writes_the_latest_proposal(env):
    r1 = await onboard_repo(*env.args)
    env.decide(r1.task_id, "answer", "use tox")
    r2 = await onboard_repo(*env.args)
    env.decide(r2.task_id, "approve")

    r3 = await onboard_repo(*env.args)

    assert r3.status == "written" and r3.contract_yaml == r2.contract_yaml


async def test_invalid_yaml_reply_is_retried_or_escalated(env):
    bad = json.dumps({"contract_yaml": "build: x", "rationale": "r"})
    runtime = FakeRuntime({Role.ARCHITECT: [bad, bad]})
    cfg, store, _, gate, repo_name, repo = env.args

    r = await onboard_repo(cfg, store, runtime, gate, repo_name, repo)

    assert r.status == "waiting" and r.contract_yaml == ""
    esc = store.latest_artifact(r.task_id, ArtifactKind.ESCALATION).body
    assert esc["category"] == "stuck" and esc["step"] == "contract"
    assert store.latest_artifact(r.task_id, ArtifactKind.CONTRACT) is None
    # an approve of a stuck escalation never writes; it asks again
    env.decide(r.task_id, "approve")
    good = FakeRuntime({Role.ARCHITECT: [proposal()]})
    r2 = await onboard_repo(cfg, store, good, gate, repo_name, repo)
    assert r2.status == "proposed" and not (repo / CONTRACT_FILE).exists()


async def test_invalid_first_reply_then_valid_one_is_retried(env):
    bad = json.dumps({"contract_yaml": "build: x", "rationale": "r"})
    cfg, store, _, gate, repo_name, repo = env.args
    runtime = FakeRuntime({Role.ARCHITECT: [bad, proposal()]})

    r = await onboard_repo(cfg, store, runtime, gate, repo_name, repo)

    assert r.status == "proposed" and len(runtime.calls) == 2


async def test_rerun_never_creates_second_onboard_task(env):
    r1 = await onboard_repo(*env.args)
    await onboard_repo(*env.args)
    await onboard_repo(*env.args)

    assert [t.id for t in onboard_tasks(env.store)] == [r1.task_id]
    assert len(env.runtime.calls) == 1


async def test_crash_after_creating_the_task_resumes_it(env):
    task = env.store.create_task("acme", f"Onboard {REPO}", ONBOARD_SOURCE, REPO)

    r = await onboard_repo(*env.args)

    assert r.task_id == task.id and r.status == "proposed"
    assert len(onboard_tasks(env.store)) == 1


async def test_crash_between_proposal_and_escalation_reuses_the_proposal(env):
    r1 = await onboard_repo(*env.args)
    # drop the escalation as if the process died right after the proposal
    task = env.store.get(r1.task_id)
    assert task.state is TaskState.AWAITING_HUMAN
    env2 = env.store.create_task(
        "acme", f"Onboard {REPO}", ONBOARD_SOURCE, "acme/other"
    )
    env.store.transition(env2.id, TaskState.ARCHITECT, Role.ARCHITECT, "t")
    env.store.put_artifact(
        env2.id,
        ArtifactKind.CONTRACT,
        Role.ARCHITECT,
        {"contract_yaml": r1.contract_yaml, "rationale": "r"},
    )
    calls = len(env.runtime.calls)
    cfg, store, runtime, gate, _, repo = env.args

    r = await onboard_repo(cfg, store, runtime, gate, "acme/other", repo)

    assert r.status == "proposed" and r.contract_yaml == r1.contract_yaml
    assert len(runtime.calls) == calls


async def test_rejected_is_reported_once_then_a_new_proposal_may_start(env):
    r1 = await onboard_repo(*env.args)
    env.decide(r1.task_id, "reject")
    env.store.transition(r1.task_id, TaskState.CANCELLED, Role.HUMAN, "rejected")

    r2 = await onboard_repo(*env.args)
    assert r2.status == "rejected" and r2.task_id == r1.task_id
    assert not (env.repo / CONTRACT_FILE).exists()

    r3 = await onboard_repo(*env.args)
    assert r3.status == "proposed" and r3.task_id != r1.task_id


async def test_other_repos_get_their_own_task(env):
    r1 = await onboard_repo(*env.args)
    cfg, store, runtime, gate, _, repo = env.args

    r2 = await onboard_repo(cfg, store, runtime, gate, "acme/other", repo)

    assert r2.task_id != r1.task_id


async def test_orchestrator_refuses_onboard_tasks(env, make_rig_ctx):
    r1 = await onboard_repo(*env.args)
    env.decide(r1.task_id, "approve")

    with pytest.raises(StepFailed, match="agentco onboard"):
        await Orchestrator(make_rig_ctx).run(r1.task_id)

    assert not (env.repo / CONTRACT_FILE).exists()
    assert env.store.get(r1.task_id).state is TaskState.AWAITING_HUMAN


@pytest.fixture
def make_rig_ctx(env):
    cfg, store, runtime, gate, _, repo = env.args
    contract = load_contract_stub()
    return TaskContext(
        cfg=cfg,
        store=store,
        runtime=runtime,
        gate=gate,
        github=None,
        repo=REPO,
        repo_root=repo,
        contract=contract,
        runner=None,
    )


def load_contract_stub():
    from agentco.repo_contract import RepoContract

    return RepoContract.model_validate(CONTRACT)
