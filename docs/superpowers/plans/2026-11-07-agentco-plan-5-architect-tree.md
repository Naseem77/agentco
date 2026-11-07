# Agent Company — Plan 5: Architect, ADRs, task tree, root cause, warm pool, webhooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** when triage sees a *new design*, the **Architect** writes a structured ADR (context, decision, consequences, sub-tasks). A human approves it in the inbox. Only after that approval does agentco create one **child task** per sub-task. Each child goes through the normal loop with its own rounds, review, QA, lead and PR. The parent waits until all children are merged; then the lead approves the parent as a whole, and the approved ADR becomes an `rnd` `Decision`. When the same area comes back a **second time**, the fix goes to the senior, who must state one invariant. The lead checks that the invariant is there, and the merged invariant becomes the `Playbook` that later fixes in that area start from. Repos without `.agentco.yml` get one **proposed by the Architect** and approved by a human. A **warm worktree pool** runs the contract's new `setup` command ahead of time. `agentco serve` receives signed GitHub **webhooks** and runs the existing intake, at most one at a time per repo. Polling still works.

**Architecture:**
- **`agentco.agents.architect`** has `ADRReply`, `run_architect`, `ContractProposal` and `propose_contract`, plus the prompts `architect.md` and `architect-contract.md`. The Architect reads the repo and the `rnd`/`product`/`company` memory, but it never writes memory. The `Decision` fact is written by the Librarian, and only after a human approves.
- **`agentco.workflow.tree`** creates children idempotently (`spawn_children`), finds them (`adr_children`), and cancels them (`cancel_open_children`). The orchestrator gains an `ARCHITECT` step and a new `WAITING_CHILDREN` state. A waiting parent does not spin: when nothing has changed, the run returns, exactly like `AWAITING_HUMAN`.
- **`agentco.learning.recurrence`** keeps per-module recurrence counters in `meta` and looks up the playbooks for an area. Triage reads them to switch on root-cause mode.
- **`agentco.workflow.onboard`** runs the contract proposal as a small task of its own (source `onboard`). Approving it writes the file to the local checkout; agentco never commits or pushes it.
- **`agentco.sandbox.pool`** keeps prepared worktrees under `.agentco/pool/`. A claim is an atomic marker rename, and a claimed entry is never returned to the pool.
- **`agentco.webhook`** is a stdlib `ThreadingHTTPServer` receiver with an HMAC check, an event allow-list, delivery dedupe, and a coalescing trigger in front of `run_intake`. A store **lease** makes a second intake for the same repo skip, whether it comes from the webhook, cron or the CLI.

**Tech Stack:** Python 3.12, uv, pydantic 2, SQLAlchemy (existing store, plus one new `leases` table), typer, PyYAML, the stdlib `http.server`/`hmac`/`hashlib` (no new dependency), and pytest + pytest-asyncio (`asyncio_mode=auto`).

**Spec:**
- `docs/superpowers/specs/2026-11-04-agent-company-design.md`: §3/§4 (the Architect role: strongest model, reads `rnd`/`product`/`company`, writes the `rnd` Decision), §5 (new design → Architect → ADR → sub-tasks → Junior/Senior; "Root cause, not symptom"), §6 (what counts as "new"), §8 (`rnd` `Decision`/`Playbook`), §14 (Architect-tier work, the task tree, the repo contract).
- `docs/superpowers/specs/2026-11-04-agentco-build-decisions.md`: D7, D9 (webhooks), D10, D11, D12, D13(6) (warm pool). D13(7), parallel tasks, already exists (`run_many`, `TenantConfig.concurrency`) and is not rebuilt here.

## Global Constraints

The constraints of Plans 1–4 all still hold: graph names `{tenant}__{scope}`, GitHub only through `gh`, `asyncio.to_thread` for subprocesses called from async code, exit codes 1/2/3/4, the commit trailer, untrusted PR text fenced, every Librarian write with provenance, and every intake/Librarian step idempotent. In addition:

- **Unit tests:** no network, no real LLM or Copilot, no real `gh`, no Docker, and no sockets except a server bound to `127.0.0.1:0` inside the webhook tests (Task 11). Use `FakeRuntime`, `FakeGitHub`, `InMemoryBackend`, `ScriptRunner`, temporary git repos with a local bare `origin`, and `tests/workflow/orch_helpers.build_rig`.
- **FalkorDB:** never run anything against the FalkorDB on `localhost:6379`.
- **Human gate:** no child task exists until a `HUMAN_DECISION` `approve` is newer than an `ESCALATION` with `step == "adr"`. Children come from the ADR that the escalation was about: the latest `ADR` artifact older than that escalation. No other code path creates `ADR_SOURCE` tasks.
- **Tree shape:** the tree is one level deep. A child (`parent_id` set) or a task with an open PR never goes to the Architect. The parent's children are exactly its `ADR_SOURCE` children; follow-up tasks that also carry `parent_id` (Plan 4) never block a parent.
- **GitHub writes:** unchanged from Plan 4. Each child opens its own PR through the existing `_open_pr`. Parents and onboarding tasks never touch GitHub. The proposed `.agentco.yml` is written only to the local checkout and only if no such file exists; agentco never commits or pushes it.
- **Pool hygiene:** a pool entry is created from one `main` commit, prepared once, claimed by at most one task (an atomic `os.rename` of its marker) and never reused. On claim, the entry is checked out on `agentco/task-<id>` at the task's base, `git reset --hard` and `git clean -fd` are run, and `git status --porcelain` must be empty. Ignored files kept by the clean exist only because of the setup that ran on the same commit.
- **Webhooks:** the body is read only after the `Content-Length` check (`≤ webhook.max_body_bytes`). The signature is checked with `hmac.compare_digest` before any JSON parsing. Only the five allowed events are accepted (plus `ping`). Only the configured repo is accepted. A delivery is handled at most once (meta `webhook:{tenant}:{delivery}`). There is at most one running and one pending intake per repo per process, and the lease (Task 10) gives one per repo across processes. The secret comes from an environment variable, never from the YAML. `serve` refuses to start without it.
- **The user's lessons are requirements:** *Fix the cause with one rule* (Tasks 6–7), and *every child is reviewed as new code* (children run the full Plan 3/4 loop; the parent's whole-tree check is an extra step, not a replacement).

## Review Focus

- **Child tasks duplicated on crash or rerun.** A crash between `create_task`, the marker, the `REQUEST` artifact and the parent's transition creates a second child, or a child with no request. Test: Task 2 `test_spawn_children_crash_then_rerun_creates_each_child_once` (crash at every point) and Task 3 `test_resume_after_crash_spawns_once`.
- **The parent is stuck forever when a child is cancelled or its PR is closed.** The parent waits silently, `task run --all` never picks it up, or Plan 4 follow-ups (which also have `parent_id`) block it. Tests: Task 4 `test_cancelled_child_escalates_parent_once` and `test_followup_children_do_not_block_parent`; Task 5 `test_run_all_picks_up_waiting_parent`.
- **ADR sub-tasks bypass human approval.** Children appear on `answer`, on `reject`, on approval of a budget escalation from `architect`, on a decision older than the escalation, or from a child that routes itself to the Architect. Tests: Task 3 `test_no_children_without_adr_approval` (parametrized) and `test_child_never_routes_to_architect`.
- **A pool worktree leaks state.** An entry is claimed twice, is left on a stale `main`, keeps untracked files, sits on the wrong branch after a crash, or keeps an old task branch alive. Test: Task 9 `test_claim_is_clean_on_task_branch`, `test_entry_claimed_once`, `test_stale_entries_dropped`, `test_crash_mid_claim_recovers`.
- **A webhook is forged, replayed or flooded.** A bad or missing signature, an oversized body, an unknown event or another repo triggers an intake; a replayed delivery id triggers twice; a flood starts parallel intakes; cron and `serve` run at the same time. Tests: Task 11 `test_rejections` (parametrized), `test_duplicate_delivery_ignored`, `test_flood_coalesces`; Task 10 `test_second_intake_is_busy`.

## Not in this plan

- Ordering between sub-tasks (`depends_on`) and trees more than one level deep. The ADR prompt asks for sub-tasks that can each merge on their own; `concurrency` limits overlap.
- Sending a cross-cutting root cause to the Architect (see Contradictions 2). Retiring stale Playbooks.
- Hosted, multi-tenant or multi-repo webhook serving; TLS (put a tunnel or reverse proxy in front); creating tasks from `issues` events.
- Opening the onboarding contract as a PR. Returning used worktrees to the pool.

## File Structure

```
src/agentco/store/models.py            + ArtifactKind.ADR, CONTRACT; LeaseRow
src/agentco/store/task_store.py        + children, try_lease, release_lease
src/agentco/workflow/states.py         + WAITING_CHILDREN; edges (see Tasks 2, 3, 8)
src/agentco/agents/architect.py        SubTask, ADRReply, MAX_SUB_TASKS, run_architect, adr_markdown,
                                       ContractProposal, propose_contract
src/agentco/agents/prompts/architect.md  architect-contract.md  lead-parent.md
src/agentco/agents/triage.py           ~ new_design → ARCHITECT, root-cause mode, playbooks
src/agentco/agents/engineer.py         ~ EngineerBrief.playbooks
src/agentco/agents/lead.py             + ParentLeadReply, ChildSummary, review_parent; ~ run_lead(require_invariant)
src/agentco/workflow/escalation.py     ~ write_escalation(step=)
src/agentco/workflow/tree.py           ADR_SOURCE, child_ref, request_text, spawn_children, adr_children,
                                       cancel_open_children
src/agentco/workflow/orchestrator.py   ~ ARCHITECT step, ADR resume, WAITING_CHILDREN, parent review, pool
src/agentco/workflow/onboard.py        ONBOARD_SOURCE, OnboardResult, onboard_repo
src/agentco/learning/recurrence.py     record_recurrence, area_recurrences, area_playbooks
src/agentco/learning/lessons.py        + "Decision" kind, lessons_from_adr
src/agentco/learning/librarian.py      ~ ADR in LEARNED_KINDS
src/agentco/learning/metrics.py        ~ trees, pool totals
src/agentco/intake/run.py              ~ records PR recurrences; intake lease; IntakeReport.busy
src/agentco/repo_contract.py           + RepoContract.setup
src/agentco/config.py                  + Thresholds.root_cause_after, PoolConfig, WebhookConfig
src/agentco/sandbox/pool.py            PoolEntry, WorktreePool
src/agentco/webhook.py                 verify_signature, check_delivery, IntakeTrigger, make_server
src/agentco/cli.py                     + adr, onboard, pool fill, serve; ~ task run, inbox, decide, intake
tests/agents/test_architect.py  tests/workflow/test_tree.py  tests/workflow/test_orchestrator_architect.py
tests/workflow/test_orchestrator_tree.py  tests/workflow/test_onboard.py  tests/learning/test_recurrence.py
tests/sandbox/test_pool.py  tests/test_webhook.py  tests/store/test_leases.py
tests/{agents/test_triage,agents/test_lead,learning/test_librarian,learning/test_metrics,intake/test_intake_run,
       workflow/test_states,test_cli,test_config,test_repo_contract}.py  + new cases
```

## PR grouping

Each PR is one feature. Merge them in order on top of `main` (04b6a15); every PR keeps the full unit suite green.

| PR | Branch | Tasks | Feature |
|---|---|---|---|
| 1 | `feat/architect-tree` | 1, 2, 3, 4, 5 | Architect + ADR, the human gate, the task tree, the parent's whole review, trees in the CLI/Librarian/metrics |
| 2 | `feat/root-cause` | 6, 7 | Per-area recurrence counters, root-cause mode on the 2nd recurrence, playbooks in the brief, the lead's invariant check |
| 3 | `feat/onboard-pool` | 8, 9 | Repo-contract onboarding, the warm worktree pool |
| 4 | `feat/webhooks` | 10, 11 | The intake lease, `agentco serve`, README |

---

### Task 1: The Architect and its ADR

**Files:**
- Create: `src/agentco/agents/architect.py`, `src/agentco/agents/prompts/architect.md`
- Modify: `src/agentco/store/models.py`
- Test: `tests/agents/test_architect.py`, `tests/agents/test_prompts.py` (one case)

**Interfaces:**
- Consumes: `Reply`, `Asker.ask`, `ReplyInvalid`, `AgentStuck`, `AgentCall`, `role_model`, `load_prompt`, `memory_tools`, `MemoryGate.for_role`, `PMReply`, `issue_text`, `TaskStore.put_artifact`, `Role.ARCHITECT`, `cfg.loop.agent_max_turns/agent_timeout_s`.
- Produces:
  - `ArtifactKind.ADR = "adr"`.
  - `MAX_SUB_TASKS = 8`.
  - `SubTask(BaseModel)`: `title: str` (1–120 characters), `acceptance_criteria: list[str]` (at least 1), `size: Literal["xs","s","m","l","xl"]`, `why: str = ""`.
  - `ADRReply(Reply)`: `title: str` (1–120), `context: str`, `decision: str`, `consequences: str`, `alternatives: list[str] = []`, `new_dependencies: list[str] = []`, `sub_tasks: list[SubTask]` (1..`MAX_SUB_TASKS`). A validator rejects duplicate sub-task titles (compared case-insensitively).
  - `async def run_architect(asker, cfg, gate, store, task: TaskRecord, issue: PMReply, *, repo_root: Path, human_note: str = "") -> tuple[ADRReply, Artifact]`. The call is `AgentCall(role=ARCHITECT, model=role_model(cfg, ARCHITECT).model, system=load_prompt(Role.ARCHITECT), prompt=issue_text(issue) + note block, workdir=repo_root, edits=False, tools=memory_tools(gate.for_role(Role.ARCHITECT)))`. It gets read-only memory with no write scope. It stores `ADR` `{"adr": reply.model_dump(), "human_note": human_note}` with role `ARCHITECT`. A non-empty `human_note` goes into the prompt under `Human guidance:`.
  - `adr_markdown(adr: ADRReply) -> str`: `# ADR: title`, `## Context`, `## Decision`, `## Consequences`, `## Alternatives`, `## New dependencies`, and `## Sub-tasks` (numbered: title, size, criteria).
  - `architect.md`: the role (§4). Every sub-task must be mergeable on its own against `main` and small enough for one PR. Name new dependencies and patterns explicitly. Reply with JSON only.

- [ ] **Step 1: Write the failing tests**

```python
async def test_run_architect_stores_adr_and_uses_architect_model(store, loop_cfg, tmp_path):
    rt = FakeRuntime({Role.ARCHITECT: [adr_json(n=2)]})
    adr, art = await run_architect(asker(rt, store, tid), loop_cfg, gate, store, task, ISSUE, repo_root=tmp_path)
    [call] = rt.calls
    assert call.model == loop_cfg.roles[Role.ARCHITECT].model and call.edits is False
    assert art.kind is ArtifactKind.ADR and len(art.body["adr"]["sub_tasks"]) == 2
    assert "memory_add" not in {t.name for t in call.tools}

def test_adr_reply_rejects_duplicate_titles_and_too_many_subtasks(): ...
def test_adr_reply_needs_one_subtask_with_criteria(): ...
async def test_human_note_reaches_prompt(store, loop_cfg, tmp_path): ...
def test_adr_markdown_lists_subtasks(): ...
def test_architect_prompt_loads(): assert "sub-task" in load_prompt(Role.ARCHITECT)
```

- [ ] **Step 2: Run** `uv run pytest tests/agents/test_architect.py tests/agents/test_prompts.py -v`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: architect agent writes a structured ADR`

---

### Task 2: The task tree — state, idempotent children

**Files:**
- Create: `src/agentco/workflow/tree.py`
- Modify: `src/agentco/workflow/states.py`, `src/agentco/store/task_store.py`
- Test: `tests/workflow/test_tree.py`, `tests/workflow/test_states.py`

**Interfaces:**
- Consumes: `ADRReply`, `SubTask`, `ArtifactKind.ADR` (Task 1), `TaskStore.create_task/get/put_artifact/artifacts/get_meta/set_meta/transition`, `InvalidTransition`, and the Plan 4 `followup.py` reuse-the-existing-child pattern.
- Produces:
  - `TaskState.WAITING_CHILDREN = "waiting_children"`. New edges: `AWAITING_HUMAN → WAITING_CHILDREN`, and `WAITING_CHILDREN → {MERGED, AWAITING_HUMAN}`. `CANCELLED` from any non-terminal state stays automatic. Not a back-to-first-floor edge.
  - `TaskStore.children(parent_id: int) -> list[TaskRecord]`, ordered by id.
  - `ADR_SOURCE = "adr"`; `child_ref(parent_id: int, adr_id: int, index: int) -> str` → `f"task:{parent_id}:adr:{adr_id}:{index}"`.
  - `request_text(parent: TaskRecord, adr: ADRReply, sub: SubTask) -> str`: `Part of #<parent>: <adr.title>`, the decision (clipped to 1500 characters), the sub-task's criteria, `Size hint: <size>` (advisory; triage re-sizes) and `why`.
  - `spawn_children(store, parent: TaskRecord, adr: Artifact) -> list[TaskRecord]`, in index order. For each index `i`:
    1. If the meta `tree:{tenant}:{adr.id}:{i}` exists, use the task it names.
    2. Otherwise, reuse the child of `store.children(parent.id)` with `source == ADR_SOURCE` and `source_ref == child_ref(...)`, or call `create_task(tenant, sub.title, ADR_SOURCE, source_ref=child_ref(...), parent_id=parent.id)`.
    3. Set the marker.
    4. If the child has no `REQUEST`, put `REQUEST {"body": request_text(...)}` (role `ARCHITECT`).

    It never transitions the parent. An artifact that is not an `ADR` raises `ValueError`.
  - `adr_children(store, parent: TaskRecord) -> list[TaskRecord]`: the `ADR_SOURCE` children of `parent`.
  - `cancel_open_children(store, parent_id: int, actor: Role, reason: str) -> list[int]`: cancels the `ADR_SOURCE` children that are not in `PR_OPEN`/`MERGED`/`CANCELLED`. An `InvalidTransition` (a concurrent change) is skipped. Returns the ids it cancelled.

- [ ] **Step 1: Write the failing tests**

```python
def test_spawn_children_creates_one_child_per_subtask_with_request(store):
    parent, adr = parent_with_adr(store, n=3)
    kids = spawn_children(store, parent, adr)
    assert [k.parent_id for k in kids] == [parent.id] * 3 and all(k.source == ADR_SOURCE for k in kids)
    assert all(store.latest_artifact(k.id, ArtifactKind.REQUEST) for k in kids)

@pytest.mark.parametrize("crash_after", ["create_task", "set_meta", "put_artifact"])
def test_spawn_children_crash_then_rerun_creates_each_child_once(store, monkeypatch, crash_after):
    # make the named TaskStore method raise once, on the 2nd child; rerun spawn_children
    # -> exactly 3 ADR_SOURCE children, each with exactly one REQUEST, refs 0..2
def test_spawn_children_is_a_noop_on_rerun(store): ...
def test_adr_children_ignores_followups(store):
    parent, adr = parent_with_adr(store, n=1); spawn_children(store, parent, adr)
    store.create_task("acme", "edge", "followup", source_ref=f"task:{parent.id}", parent_id=parent.id)
    assert [k.source for k in adr_children(store, parent)] == [ADR_SOURCE]
def test_cancel_open_children_skips_pr_open_and_terminal(store): ...
def test_waiting_children_edges():
    assert TaskState.WAITING_CHILDREN in ALLOWED_TRANSITIONS[TaskState.AWAITING_HUMAN]
    assert {TaskState.MERGED, TaskState.AWAITING_HUMAN, TaskState.CANCELLED} <= ALLOWED_TRANSITIONS[TaskState.WAITING_CHILDREN]
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_tree.py tests/workflow/test_states.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above. Find the existing child before `create_task`, as `create_qa_followup` does.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: task tree with crash-safe child creation`

---

### Task 3: Triage → Architect → human gate → children

**Files:**
- Modify: `src/agentco/agents/triage.py`, `src/agentco/workflow/escalation.py`, `src/agentco/workflow/orchestrator.py`, `src/agentco/workflow/states.py`
- Test: `tests/workflow/test_orchestrator_architect.py`, `tests/agents/test_triage.py`

**Interfaces:**
- Consumes: `run_architect`, `ADRReply` (Task 1); `WAITING_CHILDREN`, `ADR_SOURCE`, `spawn_children` (Task 2); `run_triage`, `TriageDecision`, `write_escalation`, `_Run._escalate/_resume/_human_ok/_human_input/_is_pr_round/_escalate_if_over_budget/_go/_set`, `StepFailed`.
- Produces:
  - `run_triage(..., human_ok: bool = False, allow_architect: bool = False)`. When `reply.route == "new_design"` and `allow_architect` is true, the result is `route=Role.ARCHITECT`, `escalate=None`, `fast_lane=False`, and the unknown-area check is skipped (the ADR is itself a "new" escalation). When the route is `new_design` and `allow_architect` is false, the task is escalated as `"new"` unless `human_ok`. The reason becomes `"a new design inside a sub-task or PR round; a human decides. Triage: …"`, which replaces the Plan 3 "(Plan 5)" text.
  - `write_escalation(..., step: str | None = None)` stores `body["step"]` when it is set. `_Run._escalate` passes `step` through.
  - `_EDGES[AWAITING_HUMAN]` gains `ARCHITECT`.
  - Orchestrator:
    - `_triage` passes `allow_architect = task.parent_id is None and not self._is_pr_round()`. A route of `ARCHITECT` → `assign(ARCHITECT)` → `_go(ARCHITECT, TRIAGE, reason)`.
    - `_human_ok()` is also true when `task.source == ADR_SOURCE` (the human already approved the design).
  - `_Run._architect(task)`, the handler for `ARCHITECT`:
    1. Run the budget check.
    2. Call `run_architect(..., repo_root=ctx.repo_root, human_note=<note when _human_input() origin is "architect">)`.
    3. Escalate `"new"`, reason `f"ADR: {adr.title} ({n} sub-tasks)"`, `question="Approve this design and its sub-tasks? See `agentco adr <id>`."`, `options=("approve", "answer: change the design", "reject")`, `step="adr"`, `actor=Role.ARCHITECT`.

    `ReplyInvalid`/`AgentStuck` → escalate `"stuck"`.
  - `_resume`, for `origin == "architect"`:
    - `approve` when `step == "adr"` → `_approve_adr(escalation)`.
    - `answer`, or `approve` of any other step (budget, stuck) → `_go(ARCHITECT, HUMAN, ...)`.
    - `reject` stays the generic → `CANCELLED`.
  - `_Run._approve_adr(escalation)`:
    1. `adr` = the latest `ADR` artifact with `id < escalation.id`; if there is none → `StepFailed`.
    2. `spawn_children(store, task, adr)`.
    3. `_set("adr", str(adr.id))`.
    4. `_go(WAITING_CHILDREN, HUMAN, f"ADR approved: {n} sub-tasks")`.

    Until Task 4 the loop returns at `WAITING_CHILDREN`, because that state has no handler.

- [ ] **Step 1: Write the failing tests** (`build_rig` with a triage `new_design` script)

```python
async def test_new_design_goes_to_architect_then_waits_for_human(rig):
    task = await rig.run()
    assert rig.transitions()[-3:] == ["triage", "architect", "awaiting_human"]
    esc = rig.escalations()[-1]
    assert (esc["category"], esc["from_state"], esc["step"]) == ("new", "architect", "adr")
    assert rig.store.children(rig.task_id) == []

async def test_approve_spawns_children_and_waits(rig):
    await rig.run(); rig.decide("approve"); task = await rig.run()
    assert task.state is TaskState.WAITING_CHILDREN and len(adr_children(rig.store, task)) == 2

@pytest.mark.parametrize("case", ["no_decision", "answer", "reject", "budget_approve", "stale_decision"])
async def test_no_children_without_adr_approval(rig, case): ...   # each: store.children(id) == []
async def test_answer_reruns_architect_with_note(rig): ...         # 2nd ARCHITECT prompt contains the note
async def test_children_come_from_the_adr_the_human_saw(rig): ...  # an ADR stored after the escalation is not used
async def test_resume_after_crash_spawns_once(rig, monkeypatch): ...  # transition raises once -> rerun -> 2 children
async def test_child_never_routes_to_architect(rig): ...  # child with new_design -> escalate "new", no ARCHITECT edge
async def test_child_skips_unknown_area_check(rig): ...   # seed=False: an ADR child still goes to ENGINEERING
async def test_pr_round_new_design_escalates(rig): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_orchestrator_architect.py tests/agents/test_triage.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: architect step with a human-approved ADR spawning sub-tasks`

---

### Task 4: The parent waits, then the lead approves the whole

**Files:**
- Create: `src/agentco/agents/prompts/lead-parent.md`
- Modify: `src/agentco/agents/lead.py`, `src/agentco/workflow/orchestrator.py`
- Test: `tests/workflow/test_orchestrator_tree.py`, `tests/agents/test_lead.py`

**Interfaces:**
- Consumes: `adr_children`, `cancel_open_children` (Task 2); `ADRReply`, `adr_markdown` (Task 1); `_approve_adr`, `write_escalation(step=)` (Task 3); `LeadDecision`, `memory_tools`, `load_prompt(role, variant)`, `cfg.thresholds.lead_min_confidence`.
- Produces:
  - `@dataclass(frozen=True) ChildSummary`: `id: int`, `title: str`, `state: str`, `pr_url: str`, `summary: str`. The summary is the latest `LEAD` `summary`, or `""`.
  - `ParentLeadReply(Reply)`: `approve: bool`, `confidence: float` (0–1), `reason: str`, `summary: str`.
  - `async def review_parent(asker, cfg, gate, store, parent: TaskRecord, *, adr: ADRReply, children: Sequence[ChildSummary], human_note: str = "") -> ParentLeadReply`. It uses the LEAD model, the `load_prompt(Role.LEAD, "parent")` prompt and read-only memory tools. The question it answers: do the merged children together deliver the ADR's decision and the parent's acceptance criteria? It stores a `LEAD` artifact `asdict(LeadDecision(outcome="approve" | "escalate", category="" | "tradeoff", findings_to_fix=(), follow_ups=(), reason, summary, comments=(), question="", options=())) | {"source": "parent"}`, which keeps the Plan 4 `LEAD` shape, so `lessons_from_lead` and `_lead_from` still read it.
  - The `_Run.loop` handles `WAITING_CHILDREN` the way it handles `AWAITING_HUMAN`: `if not await self._children(task): return task`.
  - `_Run._children(task) -> bool`. `accepted` = the JSON list in the meta `task:{id}:accepted`.
    1. Any child is `CANCELLED` and not in `accepted` → escalate `"stuck"`, reason `"sub-task(s) #a, #b cancelled"`, `options=("approve: continue without them", "reject: cancel the whole task")`, `step="child_failed"`. Return `True`.
    2. Every child is `MERGED`, or is `CANCELLED` and in `accepted` → budget check, then `review_parent`. The parent goes to `MERGED` (`"all sub-tasks merged; lead approved the whole"`) when `approve` and `confidence >= lead_min_confidence`; otherwise escalate `"tradeoff"` with the lead's reason, `step="parent_review"`. Return `True`.
    3. Otherwise return `False` (still waiting, no LLM call, no write).
  - `_resume`, for `origin == "waiting_children"`:
    - `step == "child_failed"` with approve/answer → add the cancelled children to `accepted`, then `_go(WAITING_CHILDREN, HUMAN, ...)`.
    - `step == "parent_review"`: `approve` → `MERGED`; `answer` → `WAITING_CHILDREN`, and the next `review_parent` gets the note.
  - On `reject`, the generic `CANCELLED` is followed by `cancel_open_children(store, id, Role.HUMAN, "parent rejected")` (Task 5 adds the same call to `agentco decide`).

- [ ] **Step 1: Write the failing tests** (a parent in `WAITING_CHILDREN` with two children made by `spawn_children`; child states are set directly through `store.transition` chains)

```python
async def test_parent_waits_without_llm_while_children_run(tree):
    task = await tree.run_parent()
    assert task.state is TaskState.WAITING_CHILDREN and tree.runtime.calls == []

async def test_all_merged_then_lead_approves_parent(tree):
    tree.finish_children("merged"); task = await tree.run_parent()
    assert task.state is TaskState.MERGED
    assert tree.store.latest_artifact(task.id, ArtifactKind.LEAD).body["source"] == "parent"

async def test_cancelled_child_escalates_parent_once(tree):  # e.g. its PR was closed -> intake cancelled it
    tree.finish_children("merged", "cancelled")
    await tree.run_parent(); await tree.run_parent()
    assert [e["step"] for e in tree.escalations()] == ["child_failed"]
async def test_accepting_failed_child_then_parent_review(tree): ...
async def test_low_confidence_parent_review_goes_to_human(tree): ...
async def test_parent_review_answer_reaches_next_review(tree): ...
async def test_reject_parent_cancels_open_children_only(tree): ...  # PR_OPEN child left, ENGINEERING child cancelled
async def test_followup_children_do_not_block_parent(tree): ...     # a non-terminal "followup" child with parent_id
def test_parent_prompt_loads(): assert load_prompt(Role.LEAD, "parent")
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_orchestrator_tree.py tests/agents/test_lead.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: parent task waits for its children and gets a whole-tree lead review`

---

### Task 5: Trees in the CLI, the Librarian and metrics

**Files:**
- Modify: `src/agentco/cli.py`, `src/agentco/learning/lessons.py`, `src/agentco/learning/librarian.py`, `src/agentco/learning/metrics.py`
- Test: `tests/test_cli.py`, `tests/learning/test_lessons.py`, `tests/learning/test_librarian.py`, `tests/learning/test_metrics.py`

**Interfaces:**
- Consumes: Tasks 1–4; `_RUNNABLE`, `_tasks_to_run`, `inbox`, `decide`, `_learn_after_decision`, `Lesson`, `LessonKind`, `KIND_SCOPE`, `artifact_source`, `clip`, `MAX_TEXT`, `LEARNED_KINDS`, `Librarian._deterministic_lessons`, `TaskMetrics`, `_totals`, `metrics_report`.
- Produces:
  - `_RUNNABLE` gains `WAITING_CHILDREN`, so `task run --all` also checks waiting parents. Children are ordinary runnable tasks, and `run_many(..., cfg.concurrency)` runs them side by side.
  - `inbox` gets an extra last column, `parent` (`#<id>` or empty).
  - `agentco adr <task_id>` prints `adr_markdown` of the task's latest ADR. With no ADR it exits 1.
  - `decide <id> reject` on a task with `ADR_SOURCE` children also calls `cancel_open_children` and prints the ids it cancelled.
  - `LessonKind` gains `"Decision"`, so `KIND_SCOPE["Decision"] == Scope.RND` through `ENTITY_TYPES`.
  - `lessons_from_adr(adr: Artifact, artifacts: list[Artifact]) -> list[Lesson] | None`:
    - No later `HUMAN_DECISION` → `None` (the artifact stays unmarked and is retried).
    - The first later decision is `approve` → one `Lesson(kind="Decision", text=clip(f"{title}: {decision} Consequences: {consequences}", MAX_TEXT), source=artifact_source(adr), modules=(), origin="human")`.
    - The first later decision is anything else → `[]`.
  - `LEARNED_KINDS` gains `ArtifactKind.ADR`, which `_deterministic_lessons` routes to `lessons_from_adr`. `_learn_after_decision` (already called by `decide`) therefore writes the `Decision` right after the approval.
  - `TaskMetrics.is_parent: bool` is true when the task has an `ADR_SOURCE` child. `_totals` computes the per-task averages, `merged` and `pct_junior` over non-parent tasks only, and adds `"trees"` and `"trees_done"` (parents in `MERGED`).

- [ ] **Step 1: Write the failing tests**

```python
def test_run_all_picks_up_waiting_parent(cli_env):
    parent = make_waiting_parent(cli_env.store, children=("merged", "cancelled"))
    res = cli_env.invoke(["task", "run", "--all", "--repo", "acme/app", "--repo-path", str(cli_env.repo)])
    assert cli_env.store.get(parent).state is TaskState.AWAITING_HUMAN   # escalated child_failed

def test_inbox_shows_parent_column(cli_env): ...
def test_adr_command_prints_markdown_and_exits_1_without_adr(cli_env): ...
def test_decide_reject_parent_cancels_open_children(cli_env): ...
def test_lessons_from_adr_waits_for_decision_then_writes_decision():
    assert lessons_from_adr(adr, [adr]) is None
    [lesson] = lessons_from_adr(adr, [adr, approve_after])
    assert lesson.kind == "Decision" and KIND_SCOPE[lesson.kind] is Scope.RND and lesson.origin == "human"
def test_rejected_adr_writes_nothing(): ...
async def test_librarian_writes_decision_once_after_approval(store, backend): ...  # rerun -> no duplicate
def test_metrics_trees_excluded_from_averages(store): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_cli.py tests/learning -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: task trees in run --all, inbox, decide, Librarian and metrics`

---

### Task 6: Per-area recurrence counters

**Files:**
- Create: `src/agentco/learning/recurrence.py`
- Modify: `src/agentco/config.py`, `src/agentco/intake/run.py`
- Test: `tests/learning/test_recurrence.py`, `tests/intake/test_intake_run.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `module_of` (`importer/episodes.py`), `TaskStore.set_meta/meta_with_prefix`, `ScopedMemory.search`, `Fact.text`, `Scope.RND`, `_Intake._reenter` and the `PR_REVIEW` body (`recurrences`, `classifications[].finding_id/fingerprint`, `decision.findings_to_fix[].id/path`).
- Produces:
  - `Thresholds.root_cause_after: int = Field(2, ge=1)`.
  - `record_recurrence(store, tenant: str, task_id: int, paths: Iterable[str], key: str) -> list[str]`. For each distinct `module_of(path)` of a non-empty path, it sets the meta `recur:{tenant}:{module}:{task_id}:{key}` = `"1"`. It is idempotent (the same key gives the same meta) and returns the modules.
  - `area_recurrences(store, tenant: str, modules: Iterable[str]) -> dict[str, int]`: the number of distinct `{task_id}:{key}` entries under each module. A module's prefix ends with `:`, so `src/a` does not count `src/ab`.
  - `async def area_playbooks(memory: ScopedMemory, queries: Iterable[str], limit: int = 3) -> list[str]`. It searches `f"Playbook {q}"` in `[Scope.RND]` for each non-empty query, keeps the first `limit` distinct `Fact.text` values, and returns `[]` on an empty query list.
  - `_Intake._reenter` calls `record_recurrence(..., paths=<findings_to_fix paths whose fingerprint is in review.body["recurrences"]>, key=f"pr-review-{review.id}")` before the `PR_OPEN → TRIAGE` transition. A rerun of the same review records nothing new.

- [ ] **Step 1: Write the failing tests**

```python
def test_counts_distinct_events_per_module(store):
    record_recurrence(store, "acme", 1, ["src/calc/add.py"], "round-1")
    record_recurrence(store, "acme", 1, ["src/calc/add.py"], "round-1")      # same event
    record_recurrence(store, "acme", 2, ["src/calc/mul.py", ""], "pr-review-9")
    assert area_recurrences(store, "acme", ["src/calc", "src/cal"]) == {"src/calc": 2, "src/cal": 0}

def test_other_tenant_not_counted(store): ...
async def test_area_playbooks_dedupes_and_limits(): ...
async def test_pr_recurrence_recorded_once(env): ...   # Plan 4 recurring real bug; intake rerun -> count stays 1
def test_root_cause_after_default_and_validation(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/learning/test_recurrence.py tests/intake/test_intake_run.py tests/test_config.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: count recurrences per area`

---

### Task 7: Root-cause mode on the 2nd recurrence

**Files:**
- Modify: `src/agentco/agents/triage.py`, `src/agentco/agents/engineer.py`, `src/agentco/agents/lead.py`, `src/agentco/workflow/orchestrator.py`, `src/agentco/agents/prompts/{senior,lead,triage}.md`
- Test: `tests/agents/test_triage.py`, `tests/agents/test_engineer.py`, `tests/agents/test_lead.py`, `tests/workflow/test_orchestrator.py`, `tests/learning/test_librarian.py`

**Interfaces:**
- Consumes: `record_recurrence`, `area_recurrences`, `area_playbooks`, `Thresholds.root_cause_after` (Task 6); `_repeat_paths`, `TriageDecision`, `EngineerBrief`, `run_engineer`, `EngineerFailed`, `run_lead`, `LeadDecision`, `lead_input`, `Finding`, `_Run._brief/_lead`, `module_of`.
- Produces:
  - `TriageDecision` gains `root_cause: tuple[str, ...] = ()` and `playbooks: tuple[str, ...] = ()`. Both appear as lists in the `TRIAGE` artifact.
  - `_send_back_paths(store, task_id) -> list[str]`: the `findings_to_fix` paths of the latest `LEAD` artifact with `outcome == "send_back"`. This covers internal and PR send-backs alike.
  - `run_triage` additionally does the following:
    1. Record `_repeat_paths(...)` with `key=f"round-{task.round}"`.
    2. Compute `hot` = the modules of `_send_back_paths` with `area_recurrences(...) >= cfg.thresholds.root_cause_after`.
    3. If `hot` is not empty: `route=SENIOR` (this also overrides `ARCHITECT`), `require_invariant=True`, `root_cause=hot`, and the reason gets `" Root cause: <modules> came back <n> times; state one invariant."`.
    4. In every case, `playbooks = await area_playbooks(gate.for_role(Role.TRIAGE), [*modules of send-back paths, issue.summary])`.

    The Plan 3 within-task `_repeat_paths` → `require_invariant` behaviour is unchanged.
  - `EngineerBrief.playbooks: tuple[str, ...] = ()`. The engineer prompt renders it as `Playbooks for this area (start from these):` followed by one bullet each. `_brief` fills it from the `TRIAGE` artifact.
  - `run_lead(..., require_invariant: bool = False)`. When it is true and `engineer.invariant.strip()` is empty, the lead makes no LLM call and returns `send_back` with `Finding(id="root-cause", severity="major", kind="bug", title="State one invariant", detail="This area came back again: state the one rule that removes the cause, apply it, and test it.")`. When it is true, the fast lane is off and `lead_input` adds `Root cause required: does the invariant remove the cause, not just this symptom?`. `_lead` passes the `TRIAGE` `require_invariant`. (`run_engineer` already refuses an empty invariant; this is the lead's own check for the same rule.)
  - Prompt lines: `senior.md` says to start from the listed playbooks and that the invariant is one rule; `triage.md` says that a repeat area is handled by code.

- [ ] **Step 1: Write the failing tests**

```python
async def test_second_recurrence_forces_senior_with_invariant_and_playbooks(store, loop_cfg, backend):
    record_recurrence(store, "acme", 99, ["src/calc/x.py"], "pr-review-1")        # 1st, other task
    task = task_with_send_back(store, path="src/calc/add.py")                       # 2nd
    await backend.add(rnd, Scope.RND, item("Playbook src/calc: normalise inputs before compare"))
    d = await run_triage(asker(junior_reply), loop_cfg, gate, store, task, ISSUE)
    assert d.route is Role.SENIOR and d.require_invariant and d.root_cause == ("src/calc",)
    assert any("normalise inputs" in p for p in d.playbooks)

async def test_first_recurrence_keeps_plan4_behaviour(...): ...   # count 1 -> route as triage says
async def test_root_cause_overrides_architect(...): ...
def test_brief_renders_playbooks(): ...
async def test_lead_sends_back_missing_invariant_without_llm(store, loop_cfg): ...
async def test_lead_prompt_mentions_root_cause_when_required(...): ...
async def test_merged_root_cause_task_writes_area_playbook(store, backend): ...  # Librarian close-out pins modules
```

- [ ] **Step 2: Run** `uv run pytest tests/agents tests/workflow/test_orchestrator.py tests/learning/test_librarian.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: root-cause mode on a second recurrence in an area`

---

### Task 8: Repo-contract onboarding

**Files:**
- Create: `src/agentco/workflow/onboard.py`, `src/agentco/agents/prompts/architect-contract.md`
- Modify: `src/agentco/agents/architect.py`, `src/agentco/store/models.py`, `src/agentco/workflow/states.py`, `src/agentco/workflow/orchestrator.py`, `src/agentco/cli.py`
- Test: `tests/workflow/test_onboard.py`, `tests/agents/test_architect.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `CONTRACT_FILE`, `RepoContract`, `load_repo_contract`, `RepoContractMissing`; `write_escalation(step=)` (Task 3); `Asker`, `role_model`, `load_prompt(Role.ARCHITECT, "contract")`, `_tasks_to_run`, `_has_waiting_decision`, the `task run` contract loading.
- Produces:
  - `ArtifactKind.CONTRACT = "contract"`; `_EDGES[INTAKE]` gains `ARCHITECT`.
  - `ContractProposal(Reply)`: `contract_yaml: str` (at most 4000 characters), `rationale: str`. A validator requires `yaml.safe_load` to give a mapping that passes `RepoContract.model_validate`.
  - `async def propose_contract(asker, cfg, gate, task, repo_root: Path, *, human_note: str = "") -> ContractProposal`. It is read-only (`workdir=repo_root`, `edits=False`) and stores `CONTRACT {"contract_yaml", "rationale"}`.
  - `ONBOARD_SOURCE = "onboard"`.
  - `@dataclass OnboardResult`: `task_id: int | None`, `status: Literal["exists", "proposed", "waiting", "written", "kept", "rejected"]`, `contract_yaml: str = ""`.
  - `async def onboard_repo(cfg, store, runtime, gate, repo: str, repo_root: Path) -> OnboardResult`. Each run takes one step:
    1. The file exists and there is no open onboarding task → `exists`.
    2. Find the latest `ONBOARD_SOURCE` task with `source_ref == repo`, or create one (`INTAKE → ARCHITECT`). It is never created twice: the lookup comes before `create_task`.
    3. `ARCHITECT` → `propose_contract`, then escalate `"new"` (`step="contract"`, actor `ARCHITECT`) → `proposed`.
    4. `AWAITING_HUMAN` with no newer decision → `waiting`.
    5. `approve`:
       - The file is written with `open(path, "x")`. `FileExistsError` → `kept`, and nothing is overwritten.
       - Otherwise, `load_repo_contract` validates the written file → `written`.
       - Both cases end with `AWAITING_HUMAN → MERGED`, reason `"contract written locally; commit it yourself"`.
    6. `answer` → `ARCHITECT`, then propose again with the note.
    7. The task is `CANCELLED` → `rejected`.
  - The orchestrator `_architect` raises `StepFailed(ARCHITECT, "onboarding runs via agentco onboard")` for an `ONBOARD_SOURCE` task, and `_tasks_to_run` skips those tasks.
  - CLI `agentco onboard --repo O/N --repo-path P` prints the status, plus the YAML when it is `proposed`/`written`. `task run`'s "missing contract" error now ends with `run agentco onboard --repo …`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_onboard_proposes_then_waits_then_writes(env):
    r1 = await onboard_repo(*env.args)
    assert r1.status == "proposed" and not (env.repo / ".agentco.yml").exists()
    assert (await onboard_repo(*env.args)).status == "waiting"
    env.decide(r1.task_id, "approve")
    r3 = await onboard_repo(*env.args)
    assert r3.status == "written" and load_repo_contract(env.repo).test == "pytest"
    assert env.store.get(r1.task_id).state is TaskState.MERGED and env.git_status() == "?? .agentco.yml"

async def test_existing_file_is_never_overwritten(env): ...   # created between proposal and approval -> kept
async def test_answer_reproposes_with_note(env): ...
async def test_invalid_yaml_reply_is_retried_or_escalated(env): ...
async def test_rerun_never_creates_second_onboard_task(env): ...
def test_task_run_all_skips_onboard_tasks(cli_env): ...
def test_cli_onboard_exists(cli_env): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_onboard.py tests/agents/test_architect.py tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: architect proposes a repo contract for unonboarded repos`

---

### Task 9: Warm worktree pool

**Files:**
- Create: `src/agentco/sandbox/pool.py`
- Modify: `src/agentco/repo_contract.py`, `src/agentco/config.py`, `src/agentco/workflow/orchestrator.py`, `src/agentco/learning/metrics.py`, `src/agentco/cli.py`
- Test: `tests/sandbox/test_pool.py`, `tests/workflow/test_orchestrator.py`, `tests/test_repo_contract.py`, `tests/learning/test_metrics.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Worktree`, `branch_name`, `create_worktree`, `_git`, `_run`, `_record_pushed`, `_exclude_agentco_dir`, `GitError` (`sandbox/worktree.py`); `CommandRunner.run`, `CommandResult`; `TaskContext`, `_Run._worktree/_is_pr_round/_meta/_set`, `_repo_lock`; `make_command_runner`.
- Produces:
  - `RepoContract.setup: str | None = None`.
  - `PoolConfig(extra="forbid")`: `size: int = Field(2, ge=0, le=8)` (`0` disables), `setup_timeout_s: float = Field(1800, gt=0)`. `TenantConfig.pool: PoolConfig = PoolConfig()`.
  - `@dataclass(frozen=True) PoolEntry`: `path: Path`, `sha: str`, `setup_s: float`.
  - `WorktreePool(repo_root: Path, size: int)`, with entries `.agentco/pool/<name>/` and markers `.agentco/pool/<name>.ready.json` (`{"sha", "setup_s"}`) or `<name>.task-<id>.json` (claimed). The methods:
    - `fill(main_ref: str, prepare: Callable[[Path], float]) -> int`:
      1. Run `recover()`.
      2. Drop ready entries whose sha is not `rev-parse main_ref`: first rename the marker to `.dead.json` (atomic), then remove the worktree and the marker.
      3. Add entries up to `size`: `git worktree add --detach <path> <sha>`, then `prepare(path)` (it runs `setup` and returns the seconds), and require `git status --porcelain` to be empty.
      4. Only then write the ready marker (write to a temporary name, then rename).

      A failed entry is removed. Returns the number of entries added.
    - `claim(task_id: int, base_ref: str) -> tuple[Worktree, PoolEntry] | None`:
      1. Pick a ready entry whose sha is `rev-parse base_ref`.
      2. `os.rename` its marker to `.task-<id>.json`. A `FileNotFoundError` means the entry was lost to another claim, so try the next one.
      3. `git checkout -B agentco/task-<id> <sha>`, then `reset --hard`, then `clean -fd`, then check that the status is clean.
      4. `_record_pushed(repo_root, branch, None)`.

      Returns `None` when no entry matches.
    - `owned_by(task_id: int) -> Worktree | None`: the entry with the `.task-<id>.json` marker, after making sure HEAD is on `agentco/task-<id>`. This finishes a claim that crashed after the marker rename.
    - `recover() -> None`: removes entry directories that have no marker (a fill that crashed) and markers whose directory is missing. It never touches claimed entries that are still there.
  - `TaskContext.pool: WorktreePool | None = None` (the last field).
  - `_Run._worktree` order:
    1. `pool.owned_by(id)`.
    2. `create_worktree` when its default path exists, the local branch exists or `_is_pr_round()`.
    3. `pool.claim(id, base)`; a hit sets the meta `task:{id}:setup = "pool"` and the meta `pool:{tenant}:hit:{id} = setup_s`.
    4. `create_worktree`. When `contract.setup` is set and the meta `task:{id}:setup` is missing, run it through `ctx.runner` with a `pool.setup_timeout_s` timeout. A non-zero exit → `StepFailed(state, "setup failed: …")`. Success sets `task:{id}:setup = "done"` and `pool:{tenant}:miss:{id} = <seconds>`.

    All of this runs under `_repo_lock`.
  - Metrics totals gain `pool_hits`, `pool_misses` and `setup_saved_s`, read from those meta prefixes.
  - CLI `agentco pool fill --repo-path P [--main main] [--host]`. When `size == 0` it prints "pool disabled"; when there is no `setup` it prints "no setup command; nothing to warm". Both exit 0. Otherwise it fills and prints the count. `task run` passes a `WorktreePool` when `size > 0` and `contract.setup` is set.

- [ ] **Step 1: Write the failing tests** (real temporary git repos; `prepare` writes an ignored `deps/` dir and returns `1.5`)

```python
def test_claim_is_clean_on_task_branch(make_repo):
    pool = WorktreePool(repo, 1); pool.fill("main", prepare)
    (entry_dir(pool) / "stray.txt").write_text("x")                     # tampering / leftover
    wt, entry = pool.claim(7, "main")
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agentco/task-7"
    assert not (wt.path / "stray.txt").exists() and (wt.path / "deps").exists()
    assert git(wt.path, "status", "--porcelain") == "" and entry.setup_s == 1.5

def test_entry_claimed_once(make_repo): ...           # two claims, one entry -> second gets None
def test_stale_entries_dropped(make_repo): ...        # commit on main -> fill drops old, claim at new sha only
def test_crash_mid_claim_recovers(make_repo, monkeypatch): ...  # checkout fails after rename -> owned_by finishes claim
def test_failed_or_dirty_prepare_leaves_no_entry(make_repo): ...
def test_recover_removes_half_filled_entry(make_repo): ...
async def test_pool_hit_skips_setup_and_miss_runs_it(rig): ...  # ScriptRunner sees "make setup" only on the miss
async def test_pr_round_never_claims(rig): ...
def test_contract_setup_optional(): ...
def test_metrics_pool_totals(store): ...
def test_cli_pool_fill_disabled_and_no_setup(cli_env): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/sandbox/test_pool.py tests/workflow/test_orchestrator.py tests/test_repo_contract.py tests/learning/test_metrics.py tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: warm worktree pool with crash-safe claims`

---

### Task 10: One intake per repo — the store lease

**Files:**
- Modify: `src/agentco/store/models.py`, `src/agentco/store/task_store.py`, `src/agentco/intake/run.py`, `src/agentco/cli.py`
- Test: `tests/store/test_leases.py`, `tests/intake/test_intake_run.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Base`, `UTCDateTime`, `TaskStore._sessions`, `run_intake`, `IntakeReport`, `_intake_lines`.
- Produces:
  - `LeaseRow` (table `leases`): `key: str` PK, `owner: str`, `expires_at: UTCDateTime`. `create_all` creates it on existing databases.
  - `TaskStore.try_lease(key: str, owner: str, ttl_s: float, now: datetime | None = None) -> bool`. In one transaction, it deletes the row for `key` if it has expired or belongs to `owner`, then inserts the new row. An `IntegrityError` means `False`.
  - `TaskStore.release_lease(key: str, owner: str) -> None`.
  - `INTAKE_LEASE_TTL_S = 3600`. `run_intake` takes `intake:{tenant}:{repo}` with owner `f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"` before reading anything. If the lease is busy, it returns `IntakeReport(repo, busy=True)` without moving the cursor. It releases the lease in `finally`.
  - `IntakeReport.busy: bool = False`. The CLI `intake` prints `"another intake for <repo> is running; skipped"` and exits 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_lease_exclusive_until_expiry(store):
    assert store.try_lease("k", "a", 60, now=T0) and not store.try_lease("k", "b", 60, now=T0)
    assert store.try_lease("k", "b", 60, now=T0 + timedelta(seconds=61))

def test_release_only_by_owner(store): ...
async def test_second_intake_is_busy(env):
    assert env.store.try_lease(f"intake:acme:acme/app", "other", 60)
    rep = await run_intake(env.cfg, env.store, env.gh, env.rt, env.gate, "acme/app")
    assert rep.busy and env.gh.calls == [] and env.store.get_meta("intake:acme:acme/app:cursor") is None
async def test_lease_released_after_error(env): ...
def test_cli_intake_busy_exit_0(cli_env): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/store/test_leases.py tests/intake/test_intake_run.py tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `fix: one intake per repo at a time via a store lease`

---

### Task 11: `agentco serve` — signed GitHub webhooks; README

**Files:**
- Create: `src/agentco/webhook.py`
- Modify: `src/agentco/config.py`, `src/agentco/cli.py`, `README.md`
- Test: `tests/test_webhook.py`, `tests/test_cli.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `run_intake`, `IntakeReport.busy` (Task 10); the body of the `intake` command; `_learn`, `_intake_lines`, `_falkordb_guard`, `validate_repo`, `TaskStore.get_meta/set_meta`.
- Produces:
  - `WebhookConfig(extra="forbid")`: `secret_env: str = "AGENTCO_WEBHOOK_SECRET"`, `max_body_bytes: int = Field(1_048_576, gt=0, le=26_214_400)`, `debounce_s: float = Field(2.0, ge=0)`. `TenantConfig.webhook: WebhookConfig = WebhookConfig()`.
  - `ACCEPTED_EVENTS = frozenset({"issues", "pull_request", "pull_request_review", "pull_request_review_comment", "check_run"})`.
  - `verify_signature(secret: bytes, body: bytes, header: str | None) -> bool`. It needs `sha256=<64 hex>` and uses `hmac.compare_digest`.
  - `class WebhookRejected(Exception)` with `status: int` and `reason: str`.
  - `@dataclass(frozen=True) Delivery`: `event: str`, `delivery_id: str`, `repo: str`.
  - `check_delivery(headers: Mapping[str, str], body: bytes, *, secret: bytes, repo: str) -> Delivery | None`. The checks run in this order:
    1. A bad signature → 401.
    2. No `X-GitHub-Event` or `X-GitHub-Delivery` → 400.
    3. `ping` → `None`.
    4. An event outside `ACCEPTED_EVENTS` → 400.
    5. JSON that does not parse or is not an object → 400.
    6. `repository.full_name` is not `repo` (case-insensitive) → 400.
  - `IntakeTrigger(run_once: Callable[[], object], debounce_s: float)` has one worker thread. `request()` coalesces: one running and at most one pending. `wait_idle(timeout: float) -> bool` and `close()`. An exception from `run_once` is printed to stderr, and the worker keeps going.
  - `make_server(host: str, port: int, *, tenant: str, store: TaskStore, repo: str, secret: bytes, max_body: int, trigger: IntakeTrigger) -> ThreadingHTTPServer`. The handler:
    - only `POST /webhook`, otherwise 404, and `GET /healthz` → 200;
    - a socket timeout of 10 s;
    - `Content-Length` missing → 411, over `max_body` → 413, both before reading;
    - after `check_delivery`, a meta `webhook:{tenant}:{delivery_id}` that already exists → 200 `duplicate`;
    - otherwise it sets the meta, calls `trigger.request()` and answers 202;
    - it never logs the body.
  - `_intake_once(cfg, store, repo) -> tuple[IntakeReport, list[LearnReport]]`, extracted from `intake` (which now calls it).
  - CLI `agentco serve --repo O/N [--host 127.0.0.1] [--port 8787]`. When `os.environ[cfg.webhook.secret_env]` is missing or empty, it exits 1. `run_once` calls `_intake_once` and prints `_intake_lines`. Ctrl-C closes the server and the trigger.
  - README: Plan 5 sections for the Architect/ADR flow (`agentco adr`, `decide`), task trees in `task run --all` and `inbox`, root-cause mode, `agentco onboard`, `pool.size` / `setup` / `agentco pool fill`, and `agentco serve`: the webhook URL, the secret env, the five events, polling still works, and serving only through a tunnel or proxy.

- [ ] **Step 1: Write the failing tests** (`make_server(..., port=0)` on `127.0.0.1`, `http.client` against it, a counting fake `run_once`)

```python
def sign(body, secret=b"s"): return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

@pytest.mark.parametrize("case,status", [("no_sig", 401), ("bad_sig", 401), ("oversize", 413),
    ("no_length", 411), ("unknown_event", 400), ("other_repo", 400), ("bad_json", 400), ("wrong_path", 404)])
def test_rejections(server, case, status):
    assert post(server, **CASES[case]).status == status and server.trigger_calls == 0

def test_valid_event_triggers_intake(server): ...
def test_ping_is_ok_without_intake(server): ...
def test_duplicate_delivery_ignored(server):
    assert post(server, delivery="d1").status == 202 and post(server, delivery="d1").status == 200
    server.trigger.wait_idle(5); assert server.trigger_calls == 1

def test_flood_coalesces():
    gate = threading.Event(); calls = []
    t = IntakeTrigger(lambda: (calls.append(1), gate.wait(5)), debounce_s=0)
    for _ in range(50): t.request()
    gate.set(); assert t.wait_idle(5) and len(calls) == 2; t.close()

def test_run_once_exception_keeps_worker_alive(): ...
def test_verify_signature_constant_time_and_format(): ...
def test_cli_serve_refuses_without_secret(cli_env, monkeypatch): ...
def test_webhook_config_defaults(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_webhook.py tests/test_cli.py tests/test_config.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above, plus the README.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: agentco serve receives signed GitHub webhooks`

---

## Contradictions resolved

1. **"The parent is done"** (§14), but there is no `DONE` state. → The parent ends in `MERGED` (all of its work is merged and the lead approved it). A new terminal state would have to be added to about ten terminal-state checks in metrics, the Librarian, intake and the CLI. Metrics report parents separately (Task 5) so they don't inflate per-PR numbers.
2. **§5: "a senior *or architect* states one invariant"** on a recurrence. → Senior only. A recurrence is seen at a send-back, when the task already has commits or a PR; turning that into a new ADR tree is unsafe. Cross-cutting recurrences stay listed in `root_cause` for the lead and the human.
3. **§14: the Architect onboards a repo** vs **"GitHub is touched only at the end"**. → The proposal is a "new" escalation. On approval it is written to the local checkout only (never overwriting), and the human commits it.
4. **§5: "sub-tasks re-sized → Junior/Senior"**. → Children go through PM and triage as usual. The ADR's size is a hint in the `REQUEST`, and triage decides.
5. **§6: "an unknown area is new"** vs ADR children. → An approved ADR counts as the human's "OK" for its children (`_human_ok`); otherwise every child would escalate again.
6. **D9: webhooks are for hosted mode.** → Plan 5 ships a local single-repo receiver that feeds the same intake path. Hosted, multi-tenant serving is out of scope.
7. **D13(7): parallel tasks** is listed as future work, but `run_many` + `concurrency` already exist. → Nothing to build; children use it.
8. **Plan 4 follow-ups also set `parent_id`.** → A parent's children are exactly its `ADR_SOURCE` tasks.
9. **"Recurrence"**: Plan 4 counts within one task's PR rounds, but §5 means an *area*. → Per-module meta counters are fed by both PR recurrences (intake) and within-task repeats (triage). Root cause starts at `thresholds.root_cause_after` (2).
10. **`issues` webhooks.** The brief asks to create tasks from issues "if that matches existing CLI behaviour". No current command creates tasks from issues (`task new` is manual, and `import` only fills memory). → `issues` events are accepted and trigger intake only.

## Self-review

- **Spec coverage:**
  - §3/§4 Architect → Tasks 1, 3.
  - §5 new design → ADR → sub-tasks → Tasks 1–3; root cause → Tasks 6–7.
  - §6 "new" (ADR always new, new dependencies listed in the ADR) → Task 3.
  - §8 `rnd` `Decision` → Task 5; `Playbook` → Task 7 (Plan 4's close-out writes it, Plan 5 reads it).
  - §14 Architect-tier work and the tree → Tasks 2–5; the repo contract → Task 8.
  - D9 → Tasks 10–11. D13(6) → Task 9. D13(7) → existing. D11 reply contract → Tasks 1, 4, 8. D12 fakes → throughout.
- **Step scan:** every task has the steps failing test → run, expect FAIL → implement → run the full suite and ruff → commit. `...` test bodies are written from the test name and the Interfaces block; every behaviour they check is specified there.
- **Interface consistency:** each cross-task name is defined in exactly one Interfaces block:
  - Task 1: `ADRReply`/`SubTask`/`run_architect`/`adr_markdown`/`ArtifactKind.ADR`.
  - Task 2: `WAITING_CHILDREN`/`ADR_SOURCE`/`spawn_children`/`adr_children`/`cancel_open_children`/`TaskStore.children`.
  - Task 3: `write_escalation(step=)`/`run_triage(allow_architect=)`/`_approve_adr`.
  - Task 4: `review_parent`/`ParentLeadReply`/`ChildSummary`.
  - Task 5: `"Decision"`/`lessons_from_adr`/`TaskMetrics.is_parent`.
  - Task 6: `record_recurrence`/`area_recurrences`/`area_playbooks`/`root_cause_after`.
  - Task 7: `TriageDecision.root_cause/playbooks`/`EngineerBrief.playbooks`/`run_lead(require_invariant=)`.
  - Task 8: `ContractProposal`/`propose_contract`/`ArtifactKind.CONTRACT`/`ONBOARD_SOURCE`/`onboard_repo`.
  - Task 9: `RepoContract.setup`/`PoolConfig`/`WorktreePool`/`TaskContext.pool`.
  - Task 10: `try_lease`/`release_lease`/`IntakeReport.busy`.
  - Task 11: `WebhookConfig`/`check_delivery`/`IntakeTrigger`/`make_server`/`_intake_once`.

  Existing names (`write_escalation`, `run_triage`, `run_lead`, `create_worktree`, `run_intake`, `_RUNNABLE`, `LEARNED_KINDS`) are used with their current signatures plus only the keyword arguments listed.
- **Review Focus:** each of the five failure modes is pinned to named tests in its owning task (2/3, 4/5, 3, 9, 10/11).
- **Proportion:** 11 tasks in 4 PRs, about Plan 4's size. The largest are Tasks 3, 4 and 9, which are where the review-focus risks are.
- **Unit-test safety:** no Docker, `gh`, Copilot or FalkorDB. The only socket is the webhook test server on `127.0.0.1:0`.
