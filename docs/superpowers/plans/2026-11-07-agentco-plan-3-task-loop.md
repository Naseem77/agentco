# Agent Company — Plan 3: Local task loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentco task run <id>` takes a task from intake to an open GitHub PR on one machine. It runs PM → triage → junior/senior engineer in a git worktree → pre-flight gate → blind review and isolated QA in parallel → lead decision. The PR is pushed and opened **last**, only after the lead approves. Anything critical, new or stuck lands in `agentco inbox`; `agentco decide` records the human's answer, and the next `task run` resumes from there.

**Architecture:**
- **`agentco.llm`** — every model call goes through the `AgentRuntime` protocol: `CopilotRuntime` (one Copilot SDK session per role step, our permission handler, our memory tools) or `FakeRuntime` (scripted, for tests). `Asker.ask()` adds the pydantic JSON reply contract (retried once) and records cost.
- **`agentco.sandbox`** — worktrees, the QA snapshot, commands (host, compose or devcontainer), and affected tests from a per-task in-memory code graph.
- **`agentco.agents`** — one module per role; each builds its role's prompt and its *isolated* inputs.
- **`agentco.workflow`** — `preflight.py` (the gate) and `orchestrator.py` (the state machine on the existing `TaskStore`, in-process and synchronous, D7).
- **GitHub writes** go through `gh` only: push, then `gh pr create` / `gh pr edit`. Nothing ever merges.

**Tech Stack:** Python 3.12, uv, `github-copilot-sdk` 1.0.x (import `copilot`, async), git worktrees, `gh` CLI, pydantic 2, SQLAlchemy (existing store), typer, pytest + pytest-asyncio (`asyncio_mode=auto`).

**Spec:**
- `docs/superpowers/specs/2026-11-04-agent-company-design.md`: §4 roles, §5 lifecycle and stop rules, §6 lead decision and escalation, §7 QA isolation, §10 models, §14 repo contract and parallel work.
- `docs/superpowers/specs/2026-11-04-agentco-build-decisions.md`: D1, D2, D3, D4, D5, D7, D8, D10, D11, D12, D13.

## Copilot SDK facts

Verified 2026-11-07 against the `github_copilot_sdk-1.0.29` wheel source (`copilot/client.py`, `session.py`, `tools.py`, `generated/*.py`), https://github.com/github/copilot-sdk/tree/main/python (README) and https://pypi.org/pypi/github-copilot-sdk/json.

- **Package:** PyPI `github-copilot-sdk`, import `copilot`, Python ≥3.11, deps pydantic≥2.11, httpx, python-dateutil. The CLI runtime binary downloads on first use (`python -m copilot download-runtime`; `COPILOT_CLI_PATH` overrides).
- **Client:** async only. `async with CopilotClient() as client:` or `await client.start()` / `await client.stop()`. Kwargs include `use_logged_in_user=True` (default), `github_token`, `mode="copilot-cli"|"empty"`, `log_level`.
- **Session:** `await client.create_session(...)`, keyword-only. We use `model`, `working_directory`, `system_message={"mode": "append", "content": str}`, `tools=[Tool]`, `on_permission_request`, `memory={"enabled": False}`, `enable_session_store=False`. `available_tools`/`excluded_tools` exist (built-in shell is `"bash"`). `session_limits` (`SessionLimitsConfig`) has **only** `max_ai_credits` — **no max-turns option**, so we count turns ourselves.
- **Send:** `await session.send_and_wait(prompt, *, timeout: float = 60.0)` returns the final `assistant.message` event (`event.data.content`) or `None`, and raises `TimeoutError`. A timeout stops the waiting, **not** the agent → `await session.abort()`. `session.on(handler)` delivers every event (`assistant.turn_start` → `AssistantTurnStartData`) and returns an unsubscribe callable. `await session.disconnect()` ends the session.
- **Tools:** `define_tool(name, *, description=..., params_type=PydanticModel, handler=fn, skip_permission=False)` → `copilot.Tool`; the handler receives `(params, invocation)`.
- **Permissions:** handler `(request, invocation) -> PermissionRequestResult`, sync or async. `request.kind` ∈ `"shell"` (`full_command_text`), `"write"` (`file_name`), `"read"` (`path`), `"custom-tool"`, `"mcp"`, `"url"`, `"memory"`, … Results: `copilot.rpc.PermissionDecisionApproveOnce()`, `PermissionDecisionReject(feedback=...)`.
- **Usage:** `(await session.rpc.usage.get_metrics()).total_premium_request_cost: float` (API group marked **experimental**); `assistant.usage` events carry `AssistantUsageData.cost` (also experimental).

**Not verified — kept behind `AgentRuntime`, exercised only by the integration smoke test:** (1) whether `total_premium_request_cost` is multiplier-weighted requests or dollars — we treat it as requests and fall back to `1.0` per `send_and_wait` on error; (2) whether async tool handlers are awaited (the README example is `async def`; we assume yes); (3) the model ids on the user's plan; (4) keychain auth with a changed `COPILOT_HOME` — we never change it.

## Global Constraints

Plan 1 and Plan 2 constraints all still hold (graph names `{tenant}__{scope}`, tenant regex, GitHub only via `gh`, commit trailer). In addition:

- **Unit tests:** no network, no real LLM or Copilot, no real `gh` and no Docker. Use `FakeRuntime`, `FakeGitHub`, `InMemoryBackend`, `InMemoryCodeGraph`, temporary git repos (with a local bare repo as `origin`) and fake command runners. There is exactly one `integration`-marked smoke test, gated by env vars (Task 13). Implementers never run it.
- **FalkorDB:** never run anything against the FalkorDB on `localhost:6379`. It holds the user's data. No step in this plan starts, stops or queries it.
- **Copilot SDK:** only `src/agentco/llm/copilot.py` may import `copilot`. Every other module depends only on `AgentRuntime`.
- **Isolation:** each role sees only its own inputs, and this is enforced in code, not in prompts. The QA and blind-review input builders take no parameter that could carry engineer notes. QA works in a separate snapshot repo with exactly one commit, `agentco: snapshot`.
- **Memory:** memory is reached only through `MemoryGate.for_role(role)`. The lead, reviewer and engineers write nothing. The PM writes `product`, QA writes `qa`, and `decide` writes `company` as `Role.HUMAN`.
- **GitHub:** writes are `git push`, `gh pr create` and `gh pr edit` only. No code path calls `gh pr merge`, `gh api -X PUT …/merge` or pushes to `main`. A PR is opened only from `LEAD_DECISION` (lead approval) or from `AWAITING_HUMAN` after a human `approve`.
- **Model replies:** every reply is a pydantic model whose JSON schema is appended to the system prompt. An invalid reply is retried once, then raises `ReplyInvalid`, which counts as a failed step. Cost is recorded for **every** runtime call, including invalid and stuck ones, as `premium_requests × cfg.price_per_request_usd`.
- **Subprocesses from async code:** every git, command-runner or gh call made from an `async def` goes through `await asyncio.to_thread(...)`, so parallel tasks really run in parallel.
- **Paths:** worktrees live under `<repo_root>/.agentco/worktrees/`; `create_worktree` adds `.agentco/` to the target repo's `.git/info/exclude`. Branch `agentco/task-<id>`, QA snapshot `<id>-qa-r<round>`.
- **Exit codes:** `1` config / bad argument, `2` FalkorDB unreachable, `3` GitHub (`gh`/push) error, `4` agent step failed (the task stays in its state; rerun `task run`).

## Review Focus

- **QA or the blind reviewer sees engineer reasoning** — anything from `EngineerReply`, commit messages or `rnd` memory reaching a QA call, or `EngineerReply` reaching a reviewer call (prompt, system, tools, workdir history). Test: Task 9 sentinel test (also checks the one-commit snapshot and QA's `rnd` denial).
- **PR opened without approval, or merged** — a PR after `send_back`/`escalate`, `PR_OPEN` after a failed push, or any gh argv containing `merge`. Tests: Task 11 (argv), Task 12 (state machine).
- **Fail-without-fix lies or leaves the worktree dirty** — a new test that passes on the old code is not flagged, or the worktree is not restored (`git status --porcelain` empty, same `HEAD`) after the red check, even when the command raises. Test: Task 6.
- **Permission policy too loose** — a shell command outside `.agentco.yml` (or chained after one), a write outside the worktree (`../`, symlinks), a write by a read-only role, or an `mcp`/`url` request approved. Test: Task 3.
- **Stop rules / cost not enforced** — no one-time senior reassignment after 3 junior rounds, no escalation after 3 senior rounds, budget overrun not stopping, exhausted pre-flight or rebase conflict not counted as a round, stuck junior not handed to a senior. Test: Task 12.

## Not in this plan

- **Plan 4:** PR-feedback intake (`agentco intake`, D9) and re-entry from `PR_OPEN`; the Librarian (lead comments and human decisions → ReviewRules / EscalationRules / Policies, invariants → Playbooks, QA's back-channel issue in `rnd`, the QA regression-test follow-up PR); metrics and learning.
- **Plan 5:** the Architect, ADRs and the task tree. Plan 3 escalates a `new_design` triage to the human inbox (category `new`) instead.
- **Deferred — D13(6) warm worktree pool:** a task's worktree is created once and reused for all its rounds, so dependencies install once per task. A cross-task pool needs stale-dependency handling and eviction; Plan 4's metrics will show whether setup time justifies it.
- **Not here:** webhooks, a UI, hosted sandboxes.

## File Structure

```
src/agentco/config.py              + Thresholds, LoopLimits, price_per_request_usd, concurrency
src/agentco/repo_contract.py       + test_files template, Sandbox.service
src/agentco/workflow/states.py     + PREFLIGHT -> TRIAGE (back to first floor)
src/agentco/store/models.py        + ArtifactRow, Artifact, ArtifactKind
src/agentco/store/task_store.py    + put_artifact, artifacts, latest_artifact, list_tasks
src/agentco/llm/__init__.py
src/agentco/llm/runtime.py         ToolSpec, AgentCall, AgentResult, AgentRuntime
src/agentco/llm/fake.py            FakeRuntime
src/agentco/llm/replies.py         Reply, parse_reply, Asker, ReplyInvalid, AgentStuck
src/agentco/llm/tools.py           memory_tools(ScopedMemory, write_scope)
src/agentco/llm/copilot.py         permission_allowed, CopilotRuntime (only importer of `copilot`)
src/agentco/agents/__init__.py
src/agentco/agents/prompts.py      load_prompt
src/agentco/agents/prompts/*.md    pm, triage, junior, senior, reviewer, reviewer-light, reviewer-rules, qa, lead
src/agentco/agents/replies.py      PMReply, TriageReply, Finding
src/agentco/agents/pm.py, triage.py, engineer.py, review.py, qa.py, lead.py
src/agentco/sandbox/__init__.py
src/agentco/sandbox/worktree.py    Worktree, create/commit/diff/rebase/snapshot/push
src/agentco/sandbox/runner.py      CommandResult, CommandRunner, HostRunner, ContainerRunner, runner_for
src/agentco/sandbox/affected.py    is_test_path, changed_line_ranges, AffectedTests, affected_tests, test_command
src/agentco/workflow/preflight.py  CheckResult, PreflightReport, Preflight
src/agentco/workflow/pr.py         pr_title, pr_body
src/agentco/workflow/orchestrator.py  TaskContext, Orchestrator, run_many
src/agentco/github/client.py       + PullRef, GitHubWriter, GhCli/FakeGitHub write ops
src/agentco/cli.py                 + task run, inbox, decide; task new --body; make_runtime, make_command_runner seams
tests/conftest.py                  + make_repo, loop_cfg fixtures
tests/llm/, tests/sandbox/, tests/agents/, tests/workflow/, tests/github/, tests/test_cli.py
tests/test_task_loop_integration.py
```

## PR grouping

Each PR is one feature. Merge them in order; every PR keeps the full unit suite green.

| PR | Branch | Tasks | Feature |
|---|---|---|---|
| 1 | `feat/agent-runtime` | 1, 2, 3 | Config/store/state additions, `AgentRuntime` + `FakeRuntime` + reply contract, `CopilotRuntime` |
| 2 | `feat/sandbox-preflight` | 4, 5, 6 | Worktrees + QA snapshot + command runners, affected tests, pre-flight gate |
| 3 | `feat/role-agents` | 7, 8, 9, 10 | PM, triage, engineers, blind review + QA (parallel), lead decision |
| 4 | `feat/task-loop` | 11, 12, 13 | gh write ops, orchestrator, `task run` / `inbox` / `decide`, README, smoke test |

---

### Task 1: Config, repo contract, state edge, task artifacts

**Files:**
- Modify: `src/agentco/config.py`, `src/agentco/repo_contract.py`, `src/agentco/workflow/states.py`, `src/agentco/store/models.py`, `src/agentco/store/task_store.py`, `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_repo_contract.py`, `tests/workflow/test_states.py`, `tests/store/test_artifacts.py`

**Interfaces:**
- Consumes: `TenantConfig`, `RepoContract`, `Sandbox`, `ALLOWED_TRANSITIONS`, `BACK_TO_FIRST_FLOOR`, `TaskStore`, `TaskRecord` (all existing).
- Produces:
  - `Thresholds(BaseModel, extra="forbid")`: `known_min_facts: int = 1` (D10, ≥1), `lead_min_confidence: float = 0.7` (0–1).
  - `LoopLimits(BaseModel, extra="forbid")`, every number > 0: `max_changed_lines: int = 400`, `fix_max_changed_lines: int = 150`, `preflight_attempts: int = 2`, `command_timeout_s: float = 900`, `agent_max_turns: int = 40`, `agent_timeout_s: float = 1200`, `fast_lane: bool = True`, `fast_lane_max_changed_lines: int = 60`.
  - New optional `TenantConfig` fields: `price_per_request_usd: float = 0.04` (≥0), `concurrency: int = 1` (≥1), `thresholds: Thresholds = Thresholds()`, `loop: LoopLimits = LoopLimits()`.
  - `RepoContract.test_files: str | None = None`. When set it must contain `{files}`; otherwise `RepoContractError`.
  - `Sandbox.service: str | None = None`. It is only meaningful with `compose`.
  - `ALLOWED_TRANSITIONS[PREFLIGHT]` adds `TRIAGE`.
  - `BACK_TO_FIRST_FLOOR` becomes `{(LEAD_DECISION, TRIAGE), (PR_OPEN, TRIAGE), (PREFLIGHT, TRIAGE)}`.
  - `class ArtifactKind(StrEnum)`: `REQUEST, TARGET, ISSUE, TRIAGE, ENGINEER, PREFLIGHT, REVIEW, QA, LEAD, ESCALATION, HUMAN_DECISION, PR`. Values are lowercase.
  - `ArtifactRow`: table `artifacts`, columns `id`, `task_id` (FK tasks), `round`, `kind`, `role`, `body` (JSON text), `created_at`.
  - `@dataclass(frozen=True) Artifact`: `id: int`, `task_id: int`, `round: int`, `kind: ArtifactKind`, `role: Role`, `body: dict`, `created_at: datetime`.
  - `TaskStore.put_artifact(task_id: int, kind: ArtifactKind, role: Role, body: dict) -> Artifact` (round = the task's current round; also audits `("artifact", {"kind": kind})`); `artifacts(task_id, kind: ArtifactKind | None = None, round: int | None = None) -> list[Artifact]` (id order); `latest_artifact(task_id, kind) -> Artifact | None`; `list_tasks(tenant: str, states: Collection[TaskState] | None = None) -> list[TaskRecord]` (id order).
  - `tests/conftest.py` fixtures: `loop_cfg(tmp_path) -> TenantConfig` (tenant `acme`, sqlite under `tmp_path`, every role `RoleModel(model=f"m-{role}", family=…)`, checkers in a different family); `make_repo(tmp_path)` → factory `(files: dict[str, str], name="repo") -> Path` that creates a repo on `main` with one commit plus a bare `origin` (`tmp_path/f"{name}.git"`) it pushes to.

- [ ] **Step 1: Write the failing tests**

```python
def test_loop_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert (cfg.price_per_request_usd, cfg.concurrency) == (0.04, 1)
    assert cfg.loop.preflight_attempts == 2 and cfg.thresholds.known_min_facts == 1

@pytest.mark.parametrize("extra", [{"concurrency": 0}, {"loop": {"max_changed_lines": 0}},
                                   {"loop": {"bogus": 1}}, {"price_per_request_usd": -1}])
def test_loop_invalid(tmp_path, extra):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, BASE | extra))

def test_test_files_needs_placeholder(tmp_path):
    write_contract(tmp_path, test_files="uv run pytest")
    with pytest.raises(RepoContractError, match="{files}"):
        load_repo_contract(tmp_path)

def test_preflight_can_go_back_to_first_floor():
    assert (TaskState.PREFLIGHT, TaskState.TRIAGE) in BACK_TO_FIRST_FLOOR   # replaces the old exact-set assert

def test_artifacts_carry_round_and_kind(store):
    t = store.create_task("acme", "x", "cli")
    store.put_artifact(t.id, ArtifactKind.REQUEST, Role.HUMAN, {"body": "b"})
    for s in (TaskState.PM, TaskState.TRIAGE, TaskState.ENGINEERING, TaskState.PREFLIGHT):
        store.transition(t.id, s, Role.PM, "go")
    store.transition(t.id, TaskState.TRIAGE, Role.PM, "attempts exhausted")
    a = store.put_artifact(t.id, ArtifactKind.PREFLIGHT, Role.SENIOR, {"ok": False})
    assert (a.round, store.get(t.id).round) == (1, 1)
    assert [x.kind for x in store.artifacts(t.id)] == [ArtifactKind.REQUEST, ArtifactKind.PREFLIGHT]
    assert store.latest_artifact(t.id, ArtifactKind.REQUEST).body == {"body": "b"}

def test_list_tasks_filters_tenant_and_state(store): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_repo_contract.py tests/workflow tests/store -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Update the existing `test_back_to_first_floor_edges_are_allowed` exact-set assert to the three edges. Store `body` with `json.dumps(sort_keys=True)`. `Base.metadata.create_all` creates the new table (no migration tool yet — say so in the docstring). `make_repo` runs `git init -b main` and sets `user.email`/`user.name` per repo.
- [ ] **Step 4: Run** the same tests, then `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: loop config, test_files contract, task artifacts`

---

### Task 2: AgentRuntime protocol, FakeRuntime, reply contract, prompts

**Files:**
- Create: `src/agentco/llm/{__init__,runtime,fake,replies,tools}.py`, `src/agentco/agents/__init__.py`, `src/agentco/agents/prompts.py`, `src/agentco/agents/prompts/{pm,triage,junior,senior,reviewer,reviewer-light,reviewer-rules,qa,lead}.md`
- Test: `tests/llm/test_replies.py`, `tests/llm/test_tools.py`, `tests/agents/test_prompts.py`

**Interfaces:**
- Consumes: `Role`, `TaskStore.record_cost(task_id, actor, usd, detail: dict)`, `ScopedMemory.search(query, scopes=None, limit=10) -> list[Fact]`, `ScopedMemory.add(scope, MemoryItem)`, `AccessDenied`, `Scope`.
- Produces:
  - `@dataclass(frozen=True) ToolSpec`: `name: str`, `description: str`, `params: type[BaseModel]`, `handler: Callable[[BaseModel], Awaitable[str]]`.
  - `@dataclass(frozen=True) AgentCall`: `role: Role`, `model: str`, `system: str`, `prompt: str`, `workdir: Path | None = None`, `edits: bool = False`, `commands: tuple[str, ...] = ()` (exact shell commands allowed), `command_prefixes: tuple[str, ...] = ()`, `tools: tuple[ToolSpec, ...] = ()`, `max_turns: int = 40`, `timeout_s: float = 1200`.
  - `@dataclass(frozen=True) AgentResult`: `text: str`, `premium_requests: float`, `stuck: bool = False`, `stuck_reason: str = ""`.
  - `class AgentRuntime(Protocol)`: `async def run(self, call: AgentCall) -> AgentResult`.
  - `Step = str | AgentResult | Callable[[AgentCall], str | AgentResult]`.
  - `FakeRuntime(script: Mapping[Role, Sequence[Step]], premium_requests: float = 1.0)`: `.calls: list[AgentCall]`; steps are consumed per role, in order; a callable step can edit `call.workdir` (simulated engineer); an exhausted script raises `AssertionError(f"no scripted reply for {role}")`.
  - `class Reply(BaseModel)`: `model_config = ConfigDict(extra="ignore")`. Base class for every role reply.
  - `parse_reply(text: str, reply_type: type[T]) -> T`. It strips a ```` ```json ```` fence, takes the **last** balanced top-level `{…}`, and validates it. It raises `ValueError` or `ValidationError`.
  - `class ReplyInvalid(RuntimeError)`.
  - `class AgentStuck(RuntimeError)`: `reason: str`.
  - `@dataclass Asker`: `runtime: AgentRuntime`, `store: TaskStore`, `task_id: int`, `price_per_request_usd: float`.
  - `async def Asker.ask(self, call: AgentCall, reply_type: type[T]) -> T`.
  - `memory_tools(memory: ScopedMemory, write_scope: Scope | None = None) -> tuple[ToolSpec, ...]`: `memory_search(query: str, scopes: list[str] | None)` returns `- [graph] text` lines or `no facts`; `memory_add(name: str, body: str)` exists only when `write_scope` is given and writes there; `AccessDenied` comes back as tool text `denied: …`, never raised.
  - `load_prompt(role: Role, variant: str | None = None) -> str` reads `agents/prompts/{role}[-{variant}].md` via `importlib.resources`.

- [ ] **Step 1: Write the failing tests**

```python
class Ok(Reply):
    answer: int

async def test_ask_parses_fenced_json_and_records_cost(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime({Role.PM: ['thinking...\n```json\n{"answer": 4}\n```']}, premium_requests=2)
    got = await Asker(rt, store, t.id, 0.04).ask(AgentCall(Role.PM, "m-pm", "sys", "2+2"), Ok)
    assert got.answer == 4
    assert store.get(t.id).cost_usd == pytest.approx(0.08)
    assert '"answer"' in rt.calls[0].system          # schema appended to system

async def test_invalid_reply_retried_once_then_fails(store):
    t = store.create_task("acme", "x", "cli")
    rt = FakeRuntime({Role.PM: ["nope", '{"answer": "x"}']})
    with pytest.raises(ReplyInvalid):
        await Asker(rt, store, t.id, 0.04).ask(AgentCall(Role.PM, "m", "s", "p"), Ok)
    assert len(rt.calls) == 2 and "previous reply was invalid" in rt.calls[1].prompt
    assert store.get(t.id).cost_usd == pytest.approx(0.08)   # both calls billed

async def test_stuck_raises_after_billing(store):
    rt = FakeRuntime({Role.JUNIOR: [AgentResult("", 3, stuck=True, stuck_reason="max turns")]})
    ...  # AgentStuck("max turns"), cost 0.12

async def test_memory_tools_respect_gate():
    gate = MemoryGate("acme", InMemoryBackend())
    names = [t.name for t in memory_tools(gate.for_role(Role.QA), Scope.QA)]
    assert names == ["memory_search", "memory_add"]
    search = memory_tools(gate.for_role(Role.QA))[0]
    assert (await search.handler(search.params(query="x", scopes=["rnd"]))).startswith("denied")

def test_every_role_prompt_loads():
    for role, variant in [(Role.PM, None), (Role.REVIEWER, "light"), (Role.LEAD, None), (Role.REVIEWER, "rules")]:
        assert load_prompt(role, variant).strip()
```

- [ ] **Step 2: Run** `uv run pytest tests/llm tests/agents -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `ask` makes the system prompt `call.system + "\n\nReply with only one JSON object matching this JSON schema:\n" + json.dumps(reply_type.model_json_schema())`.
  - On a parse failure it retries once with `prompt + f"\n\nYour previous reply was invalid: {err}. Reply again with only the JSON object."`.
  - Every call records `record_cost(task_id, call.role, result.premium_requests * price, {"model": call.model, "requests": n, "reply": reply_type.__name__})` **before** parsing or raising.
  - `reviewer-rules.md` is the pre-flight rules-check prompt (Task 6).
  - Prompts are short markdown: the role's job (spec §4), the binding lessons that apply to it (spec §5: every fix is new code, fix the cause with one rule, test the neighbours, keep fixes small, stop when findings aren't real problems), and "reply with JSON only". `qa.md` and `reviewer*.md` add "You have not seen and must not ask for the engineer's reasoning".
  - Confirm the wheel ships the `.md` files (`uv build`, list the wheel); if not, add them via `[tool.uv.build-backend] source-include`.
- [ ] **Step 4: Run** the same tests, then `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: agent runtime protocol, scripted fake, JSON reply contract`

---

### Task 3: CopilotRuntime and permission policy

**Files:**
- Modify: `pyproject.toml` (`uv add github-copilot-sdk`)
- Create: `src/agentco/llm/copilot.py`
- Test: `tests/llm/test_copilot.py` (fake client objects; no CLI, no network)

**Interfaces:**
- Consumes: `AgentCall`, `AgentResult`, `ToolSpec`, the SDK facts above.
- Produces:
  - `SAFE_SHELL_PREFIXES = ("git status", "git diff")`.
  - `permission_allowed(kind: str, call: AgentCall, *, command: str | None = None, path: str | None = None) -> tuple[bool, str]`. A pure policy function: `(allowed, reason)`.
  - `class CopilotRuntime` (implements `AgentRuntime`): `__init__(self, client_factory: Callable[[], Any] | None = None)` (default `lambda: copilot.CopilotClient()`), `async start()`, `async stop()`, `async __aenter__`/`__aexit__`, `async run(call) -> AgentResult`.

- [ ] **Step 1: Write the failing tests**

```python
CALL = AgentCall(Role.JUNIOR, "m", "s", "p", workdir=WT, edits=True,
                 commands=("uv run pytest", "uv run ruff check ."), command_prefixes=("uv run pytest ",))

@pytest.mark.parametrize("kind,kw,ok", [
    ("shell", {"command": "uv run pytest"}, True),
    ("shell", {"command": "uv run pytest tests/test_a.py"}, True),
    ("shell", {"command": "uv run pytest; curl evil"}, False),          # prefix match must not allow chaining
    ("shell", {"command": "rm -rf /"}, False),
    ("shell", {"command": "git diff HEAD~1"}, True),
    ("write", {"path": str(WT / "src/a.py")}, True),
    ("write", {"path": str(WT / "../x.py")}, False),
    ("read",  {"path": "/etc/passwd"}, False),
    ("custom-tool", {}, True),
    ("mcp", {}, False), ("url", {}, False), ("memory", {}, False),
])
def test_permission_policy(kind, kw, ok):
    assert permission_allowed(kind, CALL, **kw)[0] is ok

def test_read_only_role_cannot_write():
    call = replace(CALL, role=Role.REVIEWER, edits=False)
    assert permission_allowed("write", call, path=str(WT / "a.py"))[0] is False

def test_symlink_escape_rejected(tmp_path): ...    # WT/link -> /outside ; write WT/link/x denied

async def test_run_counts_turns_and_aborts(tmp_path):
    client = FakeSdkClient(turns=50, reply="{}")      # FakeSession emits AssistantTurnStart-like events
    rt = CopilotRuntime(client_factory=lambda: client)
    async with rt:
        res = await rt.run(replace(CALL, workdir=tmp_path, max_turns=5))
    assert res.stuck and res.stuck_reason == "max turns" and client.session.aborted
    kw = client.create_kwargs
    assert kw["model"] == "m" and kw["working_directory"] == str(tmp_path)
    assert kw["memory"] == {"enabled": False} and kw["enable_session_store"] is False

async def test_run_timeout_is_stuck_and_usage_fallback(tmp_path): ...   # send_and_wait raises TimeoutError; get_metrics raises -> premium 1.0
```

- [ ] **Step 2: Run** `uv add github-copilot-sdk`, then `uv run pytest tests/llm/test_copilot.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**

  - **Policy.** Shell: allowed only when `command.strip()` is in `call.commands`, or starts with a `command_prefixes` or `SAFE_SHELL_PREFIXES` entry — and always contains none of `;`, `&&`, `||`, `|`, `` ` ``, `$(`, `>`, `<`, newline. Write: `call.edits` and `Path(path).resolve()` inside `call.workdir.resolve()`. Read: resolves inside `workdir`. `custom-tool`: allowed. Everything else: rejected.
  - **`run`.** (1) `create_session(model=call.model, working_directory=str(call.workdir)` (omitted when `None`)`, system_message={"mode": "append", "content": call.system}, tools=[define_tool(s.name, description=s.description, params_type=s.params, handler=_adapt(s.handler)) …], on_permission_request=handler, memory={"enabled": False}, enable_session_store=False)`. (2) `handler` reads `kind`, `full_command_text`, `file_name`, `path` via `getattr` and returns `copilot.rpc.PermissionDecisionApproveOnce()` or `PermissionDecisionReject(feedback=reason)`. (3) `session.on` counts events whose `.data` is a `copilot.session_events.AssistantTurnStartData`; past `max_turns` it schedules `session.abort()` and marks the call stuck `"max turns"`. (4) `send_and_wait(call.prompt, timeout=call.timeout_s)`; `TimeoutError` → `abort()`, stuck `"timeout"`. (5) Premium requests from `get_metrics().total_premium_request_cost`, `1.0` on any exception. (6) `disconnect()` in a `finally`. One client is shared across calls; `start()` is lazy.
  - The test's fake SDK objects are duck-typed, but import the real `AssistantTurnStartData` so the `isinstance` check stays honest.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: Copilot SDK runtime with worktree-scoped permissions`

---

### Task 4: Worktrees, QA snapshot, command runners

**Files:**
- Create: `src/agentco/sandbox/{__init__,worktree,runner}.py`
- Test: `tests/sandbox/test_worktree.py`, `tests/sandbox/test_runner.py`

**Interfaces:**
- Consumes: `RepoContract`, `Sandbox` (Task 1), the `make_repo` fixture.
- Produces (`worktree.py`, all sync and subprocess-based; async callers wrap them in `asyncio.to_thread`):
  - `class GitError(RuntimeError)`.
  - `branch_name(task_id: int) -> str`, which returns `f"agentco/task-{task_id}"`.
  - `@dataclass(frozen=True) Worktree`: `path: Path`, `branch: str`, `base: str`.
  - `refresh_main(repo_root: Path, main: str) -> str`. If an `origin` remote exists it runs `git fetch origin {main}` and returns `f"origin/{main}"`; otherwise it returns `main`.
  - `create_worktree(repo_root, task_id, base_ref) -> Worktree`, at `<repo_root>/.agentco/worktrees/<id>`. It is idempotent (an existing worktree is returned as is, so a resumed run continues the same branch) and adds `.agentco/` to `.git/info/exclude`.
  - `commit_all(wt, message) -> str | None`: `git add -A` + commit with the agentco author. Returns the SHA, or `None` when there is nothing to commit.
  - `diff(wt, against: str) -> str` (`git diff {against}...HEAD`); `changed_files(wt, against) -> list[str]`; `changed_lines(wt, against) -> int` (numstat added + deleted).
  - `rebase_onto(wt, base_ref) -> bool`. On conflict it runs `git rebase --abort` and returns `False`.
  - `make_snapshot(wt, dest: Path) -> Path`. Runs `git archive HEAD`, extracts into `dest`, then `git init -b main`, add, and a single commit `agentco: snapshot` by `agentco <agentco@localhost>`. No remote and no shared objects (D4).
  - `push_branch(wt) -> None` (`git push -u origin {branch}`; failure raises `GitError`); `remove_worktree(repo_root, path)`.
- Produces (`runner.py`):
  - `@dataclass(frozen=True) CommandResult`: `command: str`, `exit_code: int`, `output: str` (last 200 lines), `duration_s: float`, `timed_out: bool`.
  - `class CommandRunner(Protocol)`: `def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult`.
  - `HostRunner`: `subprocess.run(["sh", "-lc", command], cwd=…, timeout=…)`. A timeout gives exit `124`, `timed_out=True`.
  - `ContainerRunner(contract: RepoContract, repo_root: Path, exec_: Callable[[list[str], Path, float], tuple[int, str]] | None = None)`:
    - `container_argv(command, cwd) -> list[str]`: compose → `["docker","compose","-f",<repo_root/compose>,"run","--rm","-v",f"{cwd}:/workspace","-w","/workspace",service,"sh","-lc",command]` (missing `service` → `RepoContractError`); devcontainer → `["devcontainer","exec","--workspace-folder",str(cwd),"sh","-lc",command]`.
    - `prepare(cwd)`: `devcontainer up --workspace-folder cwd`; a no-op for compose.
  - `runner_for(contract, repo_root, host: bool) -> CommandRunner`.

- [ ] **Step 1: Write the failing tests**

```python
def test_worktree_is_idempotent_and_branch_named(make_repo):
    root = make_repo({"a.py": "x = 1\n"})
    base = refresh_main(root, "main")
    assert base == "origin/main"
    wt = create_worktree(root, 7, base)
    assert wt.branch == "agentco/task-7" and wt.path == root / ".agentco/worktrees/7"
    assert create_worktree(root, 7, base) == wt

def test_commit_diff_and_lines(make_repo): ...     # edit a.py, commit_all -> sha; changed_files == ["a.py"]; nothing to commit -> None

def test_rebase_conflict_aborts_cleanly(make_repo):
    # main and task both edit a.py line 1 -> rebase_onto False, `git status --porcelain` empty, HEAD unchanged

def test_snapshot_has_one_commit_and_no_history(make_repo, tmp_path):
    root = make_repo({"a.py": "x = 1\n"}); wt = create_worktree(root, 1, "main")
    (wt.path / "a.py").write_text("x = 2\n"); commit_all(wt, "fix: SECRET-ENGINEER-NOTE")
    snap = make_snapshot(wt, tmp_path / "snap")
    log = git(snap, "log", "--format=%s")
    assert log.strip() == "agentco: snapshot" and (snap / "a.py").read_text() == "x = 2\n"
    assert git(snap, "remote").strip() == ""

def test_push_branch_reaches_bare_origin(make_repo): ...

def test_host_runner_timeout(tmp_path):
    r = HostRunner().run("sleep 5", tmp_path, timeout_s=0.2)
    assert (r.exit_code, r.timed_out) == (124, True)

def test_container_argv_compose_and_devcontainer(tmp_path): ...  # exact argv; compose w/o service -> RepoContractError
```

- [ ] **Step 2: Run** `uv run pytest tests/sandbox -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - All git calls go through `_git(cwd, *args) -> str`, which raises `GitError` with stderr.
  - Snapshots extract with `tarfile` (filter `"data"`).
  - Docstring note: the contract's `build` must work from a fresh checkout, because QA snapshots have no installed dependencies.
  - Agents' own `bash` calls run on the host, limited by the permission policy (Task 3). Only the gate commands go through `ContainerRunner`.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: task worktrees, QA snapshot, command runners`

---

### Task 5: Affected tests from the code graph

**Files:**
- Create: `src/agentco/sandbox/affected.py`
- Test: `tests/sandbox/test_affected.py`

**Interfaces:**
- Consumes:
  - `index_repo(repo, root, code)`, `InMemoryCodeGraph`, `code_graph_for(tenant, Role.LIBRARIAN, backend)`;
  - `ScopedCodeGraph.all_symbols(repo)`, `call_edges(repo)`, `CallEdge`;
  - `Worktree`, `RepoContract.test_files`.
- Produces:
  - `is_test_path(path: str) -> bool`. True for:
    - any path with a `tests/`, `test/` or `__tests__/` segment;
    - file names `test_*.py`, `*_test.py`, `*_test.go`;
    - `*.test.{ts,tsx,js,jsx}` and `*.spec.{ts,tsx,js,jsx}`.
  - `changed_line_ranges(wt: Worktree, base: str) -> dict[str, list[tuple[int, int]]]`. New-side hunks from `git diff -U0 {base}...HEAD`; a pure deletion maps to `(n, n)`.
  - `@dataclass(frozen=True) AffectedTests`: `paths: tuple[str, ...]`, `full: bool`, `reason: str`.
  - `async def affected_tests(repo: str, wt: Worktree, base: str, max_depth: int = 4) -> AffectedTests`:
    1. Index the worktree's committed `HEAD` into a fresh per-task `InMemoryCodeGraph` overlay. The user's FalkorDB `rnd` graph is never written for a task branch; it is keyed to `main` (spec §8 / D13).
    2. Changed symbols are those whose `[start_line, end_line]` overlaps a hunk.
    3. Do a reverse BFS over `call_edges` up to `max_depth`, collecting caller paths where `is_test_path`. Add changed test files themselves.
    4. Return `full=True` when a changed non-test file has no symbols in the graph (unsupported language, config, lockfile), or when the result is empty.
  - `test_command(contract: RepoContract, affected: AffectedTests) -> str`. Returns `contract.test` when `full` or when `contract.test_files is None`; otherwise `contract.test_files.format(files=" ".join(shlex.quote(p) for p in affected.paths))`.

- [ ] **Step 1: Write the failing tests**

```python
FILES = {"src/calc.py": "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n",
         "tests/test_add.py": "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
         "tests/test_mul.py": "from src.calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"}

async def test_only_callers_of_changed_function(make_repo):
    root = make_repo(FILES); wt = create_worktree(root, 1, "main")
    edit(wt.path / "src/calc.py", "return a * b", "return b * a"); commit_all(wt, "x")
    got = await affected_tests("acme/app", wt, "main")
    assert (got.paths, got.full) == (("tests/test_mul.py",), False)

async def test_unindexed_change_means_full_suite(make_repo): ...   # change pyproject.toml -> full=True
async def test_changed_test_file_is_included(make_repo): ...
def test_command_uses_template(): ...   # test_files="uv run pytest {files}" -> "uv run pytest tests/test_mul.py"
@pytest.mark.parametrize("p,ok", [("tests/x.py", True), ("src/a.test.ts", True), ("src/test_utils_helper.py", True), ("src/a.py", False)])
def test_is_test_path(p, ok): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/sandbox/test_affected.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** `index_repo` reads `git ls-files`, so callers must commit first; the engineer step always commits (Task 8). Read the hunk header with `@@ -a,b \+(\d+)(?:,(\d+))? @@`.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: affected tests from a per-task code graph overlay`

---

### Task 6: Pre-flight gate

**Files:**
- Create: `src/agentco/workflow/preflight.py`
- Test: `tests/workflow/test_preflight.py`

**Interfaces:**
- Consumes: `CommandRunner`, `CommandResult`, `Worktree`, `rebase_onto`, `changed_files`, `changed_lines`, `affected_tests`, `test_command`, `is_test_path`, `Asker`, `AgentCall`, `load_prompt(Role.REVIEWER, "rules")`, `ScopedMemory` (as `Role.REVIEWER`), `RepoContract`, `LoopLimits`.
- Produces:
  - `@dataclass(frozen=True) CheckResult`: `name: str`, `ok: bool`, `detail: str`.
  - `@dataclass(frozen=True) PreflightReport`: `ok: bool`, `checks: tuple[CheckResult, ...]`, `conflict: bool`. Also `failures() -> list[CheckResult]` and `as_dict() -> dict`.
  - `class RulesReply(Reply)`: `violations: list[str]`.
  - `@dataclass Preflight`: `repo: str`, `contract: RepoContract`, `runner: CommandRunner`, `limits: LoopLimits`, `memory: ScopedMemory`, `asker: Asker`, `reviewer_model: str`.
  - `async def Preflight.run(self, wt: Worktree, base: str, *, fix_since: str | None, final: bool, lead_comments: list[str], no_test_reason: str = "") -> PreflightReport`.

**Order (the fail-fast checks stop the run; after them every check runs and every failure is reported):**
1. `rebase`: `rebase_onto(wt, base)`. A conflict returns `PreflightReport(False, …, conflict=True)`.
2. `size`: in fix rounds, `changed_lines(wt, fix_since) <= fix_max_changed_lines`; otherwise `changed_lines(wt, base) <= max_changed_lines`. Fail fast ("keep fixes small").
3. `rules`: gather review rules — `memory.search(f"ReviewRule {path}", scopes=[Scope.RND])` for each changed file — plus `lead_comments`. Only if any exist, make one LLM call with the `reviewer-rules` prompt, the diff and the rules. Any violation fails.
4. `lint`, then `build`.
5. `tests`: `test_command(contract, affected)`, or `contract.test` when `final`.
6. `red`: the fail-without-fix check (below).
7. `e2e`: only when `final` and `contract.e2e` is set. D13 runs only affected tests each round; spec §5 runs everything before the PR. This is that "everything", done once.

**The `red` check:**
- New or changed test files are `[p for p in changed_files(wt, base) if is_test_path(p)]`.
- If there are none: fail with "no new test", unless `no_test_reason` is non-empty, in which case pass with the reason.
- Otherwise, on the committed clean tree: `git checkout {base} -- <changed non-test files that exist on base>`, delete added non-test files, run `test_files` (or `contract.test`) for the new tests and expect a **non-zero** exit. A `finally` always runs `git reset --hard HEAD && git clean -fd` (no `-x`, so ignored dependency dirs survive). Tests that pass without the fix fail the check: `"tests pass without the fix: …"`.

- [ ] **Step 1: Write the failing tests**

```python
class ScriptRunner:   # records commands; returns exit codes from a dict keyed by command, default 0
    ...

async def test_all_checks_run_and_failures_collected(make_repo, store): ...  # lint=1, build=0, tests=1 -> both reported, ok False

async def test_new_test_that_passes_without_fix_is_flagged(make_repo, store):
    root = make_repo(FILES); wt = create_worktree(root, 1, "main")
    add_test(wt, "tests/test_new.py"); fix(wt, "src/calc.py"); commit_all(wt, "fix")
    runner = ScriptRunner(default=0)   # new test passes even with the fix reverted
    rep = await make_preflight(runner, store).run(wt, "main", fix_since=None, final=False, lead_comments=[])
    red = {c.name: c for c in rep.checks}["red"]
    assert not red.ok and "without the fix" in red.detail

async def test_red_check_restores_worktree_even_on_crash(make_repo, store):
    head = git(wt.path, "rev-parse", "HEAD")
    runner = ScriptRunner(raise_on="tests/test_new.py")     # runner raises mid-check
    with pytest.raises(RuntimeError):
        await pf.run(wt, "main", fix_since=None, final=False, lead_comments=[])
    assert git(wt.path, "status", "--porcelain") == "" and git(wt.path, "rev-parse", "HEAD") == head
    assert (wt.path / "src/calc.py").read_text() == FIXED

async def test_conflict_fails_fast(make_repo, store): ...      # conflict=True, no commands run
async def test_fix_round_size_guard(make_repo, store): ...     # 200 lines since fix_since -> size fails, nothing else runs
async def test_rules_llm_only_when_rules_exist(make_repo, store): ...   # no facts, no comments -> FakeRuntime has zero calls
async def test_final_runs_full_suite_and_e2e(make_repo, store): ...
async def test_no_test_reason_passes_red(make_repo, store): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_preflight.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Run each command with `await asyncio.to_thread(runner.run, cmd, wt.path, limits.command_timeout_s)`. The `detail` of a failed command is the last 60 lines of output. The rules check uses `AgentCall(Role.REVIEWER, reviewer_model, load_prompt(Role.REVIEWER, "rules"), prompt, workdir=None)`.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: pre-flight gate with fail-without-fix check`

---

### Task 7: PM and triage

**Files:**
- Create: `src/agentco/agents/replies.py`, `src/agentco/agents/pm.py`, `src/agentco/agents/triage.py`
- Test: `tests/agents/test_pm.py`, `tests/agents/test_triage.py`

**Interfaces:**
- Consumes: `Asker`, `AgentCall`, `memory_tools`, `MemoryGate`, `MemoryItem`, `Scope`, `TaskStore.put_artifact`, `TenantConfig`.
- Produces (`replies.py`, all subclasses of `Reply`):
  - `PMReply`: `summary: str`, `why: str`, `acceptance_criteria: list[str]` (min 1), `product_notes: str = ""`.
  - `TriageReply`: `route: Literal["junior","senior","new_design"]`, `size: Literal["xs","s","m","l","xl"]`, `risk: Literal["low","medium","high"]`, `reason: str`.
  - `Finding(BaseModel)`: `id: str`, `severity: Literal["blocker","major","minor","nit"]`, `kind: Literal["bug","edge_case","tradeoff","style"]`, `path: str = ""`, `line: int | None = None`, `title: str`, `detail: str`.
- Produces (`pm.py` / `triage.py`):
  - `async def run_pm(asker, cfg, gate, store, task: TaskRecord, request: str) -> PMReply`.
    - The PM gets memory search over `product` + `company` and writes one `product` episode named `task-{id}-issue`. Its body starts with a self-describing line: `Issue for task {id}: {summary}`.
    - It stores an `ISSUE` artifact holding `model_dump()`.
  - `@dataclass(frozen=True) TriageDecision`: `route: Role` (JUNIOR/SENIOR), `fast_lane: bool`, `require_invariant: bool`, `escalate: str | None` (`"new"` or `None`), `reason: str`.
  - `async def run_triage(asker, cfg, gate, store, task, issue: PMReply, *, human_ok: bool = False) -> TriageDecision`. It makes the LLM call, then applies these rules in order (deterministic code wins over the model):
    1. **Known vs new (D10).** Search `rnd` facts as `Role.TRIAGE` with `f"{issue.summary} {' '.join(issue.acceptance_criteria)}"`. If `len(facts) < thresholds.known_min_facts` and not `human_ok`, escalate `"new"`.
    2. **New design.** `route == "new_design"` and not `human_ok` → escalate `"new"`. The reason names the Architect as Plan 5.
    3. **Repeat area.** If any `path` appears in send-back findings (the `lead` artifacts' `findings_to_fix`) of ≥2 different rounds → `route=SENIOR`, `require_invariant=True` ("fix causes with one rule").
    4. **Fast lane (D13).** `fast_lane = loop.fast_lane and size in {"xs","s"} and risk == "low" and route is JUNIOR and task.round == 0 and not escalate`.
  - It stores a `TRIAGE` artifact.

- [ ] **Step 1: Write the failing tests**

```python
async def test_pm_writes_product_episode_and_issue_artifact(store, loop_cfg): ...
    # FakeRuntime PM reply -> backend has one product episode starting "Issue for task 1:"; ISSUE artifact present;
    # PM AgentCall tools == ("memory_search", "memory_add")

async def test_unknown_area_escalates_new(store, loop_cfg):     # empty rnd graph
    d = await run_triage(asker, loop_cfg, gate, store, task, ISSUE)
    assert d.escalate == "new"

async def test_human_ok_overrides_unknown(store, loop_cfg): ...
async def test_new_design_escalates_until_plan_5(store, loop_cfg): ...
async def test_repeat_area_forces_senior_with_invariant(store, loop_cfg): ...  # two lead artifacts in rounds 0,1 touching src/a.py
async def test_fast_lane_only_for_small_low_risk_known_first_round(store, loop_cfg): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/agents -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Model ids come from `cfg.roles[role].model`; a missing role raises `ConfigError(f"no model for {role}")`. Turn and timeout limits come from `cfg.loop`. The triage prompt contains the issue only, no memory tools; rule 1 is the code's memory lookup.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: PM and triage agents with known/new and fast-lane rules`

---

### Task 8: Engineer (junior / senior)

**Files:**
- Create: `src/agentco/agents/engineer.py`
- Test: `tests/agents/test_engineer.py`

**Interfaces:**
- Consumes: `Asker`, `AgentCall`, `memory_tools` (search only), `Worktree`, `commit_all`, `RepoContract`, `Finding`, `PMReply`, `TaskStore.create_task`.
- Produces:
  - `@dataclass(frozen=True) EngineerBrief`: `issue: PMReply`, `findings_to_fix: tuple[Finding, ...] = ()`, `preflight_failures: tuple[str, ...] = ()`, `require_invariant: bool = False`, `round: int = 0`, `human_note: str = ""`.
  - `FollowUp(BaseModel)`: `title: str`, `why: str`.
  - `EngineerReply(Reply)`: `summary: str`, `files_touched: list[str]`, `tests_added: list[str]`, `no_test_reason: str = ""`, `invariant: str = ""`, `new_dependencies: list[str] = []`, `follow_ups: list[FollowUp] = []`.
  - `class EngineerFailed(RuntimeError)`: `reason: str`, `stuck: bool`.
  - `async def run_engineer(asker, cfg, gate, store, task, role: Role, wt: Worktree, contract: RepoContract, brief: EngineerBrief) -> EngineerReply`. It:
    - builds `AgentCall(role, model, load_prompt(role), prompt, workdir=wt.path, edits=True, commands=<build/test/lint/e2e that are set>, command_prefixes=(test_files.split("{files}")[0],) if contract.test_files else (), tools=memory_tools(gate.for_role(role)))`;
    - the prompt lists **only** the scoped `findings_to_fix` and pre-flight failures, never the whole review ("keep fixes small"), and asks for an invariant when `require_invariant`;
    - after the call runs `commit_all(wt, f"agentco: task {id} round {round}")` (the message carries no engineer prose);
    - raises `EngineerFailed` on `AgentStuck` (stuck=True), on `ReplyInvalid`, when nothing changed, or when `require_invariant` and the invariant is blank;
    - creates one task per follow-up: `create_task(tenant, f.title, "followup", source_ref=f"task:{id}")`;
    - stores an `ENGINEER` artifact.

- [ ] **Step 1: Write the failing tests**

```python
async def test_engineer_edits_commits_and_records(make_repo, store, loop_cfg):
    def work(call):  # scripted engineer edits the worktree
        (call.workdir / "a.py").write_text("x = 2\n"); return json.dumps({"summary": "s", "files_touched": ["a.py"], "tests_added": []})
    ...
    assert git(wt.path, "log", "-1", "--format=%s") == "agentco: task 1 round 0"
    call = rt.calls[0]
    assert call.edits and call.workdir == wt.path and "uv run pytest" in call.commands
    assert [t.name for t in call.tools] == ["memory_search"]

async def test_no_change_fails(make_repo, store, loop_cfg): ...
async def test_stuck_raises_engineer_failed_stuck(make_repo, store, loop_cfg): ...
async def test_missing_invariant_fails_when_required(make_repo, store, loop_cfg): ...
async def test_prompt_contains_only_scoped_findings(make_repo, store, loop_cfg): ...
async def test_follow_ups_become_tasks(make_repo, store, loop_cfg): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/agents/test_engineer.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above. Wrap `commit_all` in `asyncio.to_thread`.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: junior and senior engineer step in the task worktree`

---

### Task 9: Blind review and isolated QA, in parallel

**Files:**
- Create: `src/agentco/agents/review.py`, `src/agentco/agents/qa.py`, `src/agentco/agents/checks.py`
- Test: `tests/agents/test_review.py`, `tests/agents/test_qa.py`, `tests/agents/test_checks.py`

**Interfaces:**
- Consumes: `Asker`, `AgentCall`, `memory_tools`, `Worktree`, `make_snapshot`, `diff`, `CommandRunner`, `affected_tests`, `test_command`, `Finding`, `PMReply`.
- Produces:
  - `ReviewReply(Reply)`: `findings: list[Finding]`, `summary: str`.
  - `NeighbourRow(BaseModel)`: `case: str`, `result: Literal["pass","fail","n/a"]`, `evidence: str`.
  - `QAFailure(BaseModel)`: `title: str`, `steps: str`, `expected: str`, `actual: str`.
  - `QAReply(Reply)`: `passed: bool`, `bug_class: str`, `tests_run: list[str]`, `neighbour_matrix: list[NeighbourRow]`, `failures: list[QAFailure] = []`, `regression_of_escaped_bug: bool = False`, `new_tests: list[str] = []`.
  - `review_input(issue: PMReply, diff_text: str, findings_addressed: list[Finding]) -> str`.
    - This is a pure function, and its signature has **no** engineer-reply parameter.
    - In fix rounds `diff_text` is the fix diff (`diff(wt, fix_since)`), reviewed as new code (spec §5).
  - `async def run_review(asker, cfg, gate, wt, base, issue, *, fix_since: str | None, findings_addressed, fast_lane: bool) -> ReviewReply`.
    - It uses the `reviewer` or `reviewer-light` prompt, with no workdir, no edits and memory search only (`Role.REVIEWER`).
  - `qa_input(issue: PMReply, neighbour_cases: list[str]) -> str`. Also pure, also no engineer parameter.
  - `async def run_qa(asker, cfg, gate, wt, base, contract, runner, round_: int, issue, *, fast_lane: bool) -> QAReply`. It:
    1. makes the snapshot `<repo_root>/.agentco/worktrees/<id>-qa-r<round>`;
    2. runs `contract.build`, then the affected tests (via `test_command`), in the snapshot with the `runner`;
    3. unless fast lane, makes the QA LLM call: `workdir=snapshot`, `edits=True` (QA writes neighbour tests), contract commands, `memory_tools(gate.for_role(Role.QA), Scope.QA)`;
    4. forces `passed=False` (with a `QAFailure`) when a deterministic step failed;
    5. removes the snapshot in a `finally`.
    - Fast lane is deterministic only: steps 1–2, no LLM.
    - Neighbour cases come from `qa`-scoped memory facts searched with the issue summary.
  - `@dataclass(frozen=True) CheckOutcome`: `review: ReviewReply | None`, `qa: QAReply | None`, `errors: tuple[str, ...]`.
  - `async def run_checks(review_coro, qa_coro) -> CheckOutcome`. It runs `asyncio.gather(..., return_exceptions=True)` (D13): one failure never cancels the other and becomes an `errors` entry.

- [ ] **Step 1: Write the failing tests**

```python
SENTINEL = "ENGINEER-PRIVATE-REASONING-42"

async def test_isolation_no_engineer_text_reaches_review_or_qa(make_repo, store, loop_cfg):
    # engineer reply summary + commit message contain SENTINEL; ENGINEER artifact stored
    await run_checks(run_review(...), run_qa(...))
    for call in rt.calls:
        assert SENTINEL not in call.system + call.prompt
        assert call.workdir is None or SENTINEL not in git(call.workdir, "log", "--format=%B")
    qa_call = next(c for c in rt.calls if c.role is Role.QA)
    assert git(qa_call.workdir, "rev-list", "--count", "HEAD").strip() == "1"   # checked inside the scripted step
    search = next(t for t in qa_call.tools if t.name == "memory_search")
    assert (await search.handler(search.params(query="x", scopes=["rnd"]))).startswith("denied")

def test_input_builders_take_no_engineer_reply():
    assert "engineer" not in " ".join(inspect.signature(review_input).parameters)
    assert "engineer" not in " ".join(inspect.signature(qa_input).parameters)

async def test_fix_round_reviews_fix_diff_only(make_repo, store, loop_cfg): ...
async def test_qa_build_failure_overrides_passed(make_repo, store, loop_cfg): ...
async def test_fast_lane_qa_makes_no_llm_call_and_light_review(make_repo, store, loop_cfg): ...
async def test_one_check_crashing_does_not_cancel_other(): ...
async def test_snapshot_removed_after_qa(make_repo, store, loop_cfg): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/agents -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above. The orchestrator stores the `REVIEW` and `QA` artifacts (Task 12), not these functions.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: blind review and snapshot-isolated QA run in parallel`

---

### Task 10: Lead decision

**Files:**
- Create: `src/agentco/agents/lead.py`
- Test: `tests/agents/test_lead.py`

**Interfaces:**
- Consumes: `Asker`, `AgentCall`, `memory_tools` (search only, `Role.LEAD`), `Finding`, `ReviewReply`, `QAReply`, `CheckOutcome`, `PreflightReport`, `EngineerReply`, `TenantConfig.thresholds`.
- Produces:
  - `CRITICAL_PATTERNS: dict[str, tuple[str, ...]]`, regexes over changed paths: security `auth|permission|crypto|secret|token|sanitiz`; data `migrations/|schema|\.sql$`; infra `^\.github/workflows/|Dockerfile|compose|\.tf$|deploy`; money `payment|billing|license`.
  - `critical_hits(paths: list[str], qa: QAReply | None) -> list[str]`. A QA `regression_of_escaped_bug` adds `"escaped-bug regression"`.
  - `async def new_hits(gate, paths, new_dependencies) -> list[str]`. Returns new dependencies, plus touched paths with zero `rnd` facts.
  - `Classified(BaseModel)`: `finding_id: str`, `category: Literal["real_bug","edge_case","tradeoff","covered_style"]`, `note: str`.
  - `LeadReply(Reply)`: `confidence: float`, `classifications: list[Classified]`, `summary: str`, `comments: list[str] = []`, `question: str = ""`, `options: list[str] = []`.
  - `@dataclass(frozen=True) LeadDecision`: `outcome: Literal["approve","send_back","escalate"]`, `category: str` (`""`, `"critical"`, `"new"`, `"tradeoff"`, `"not_confident"`), `findings_to_fix: tuple[Finding, ...]`, `follow_ups: tuple[Finding, ...]`, `reason: str`, `summary: str`, `comments: tuple[str, ...]`, `question: str`, `options: tuple[str, ...]`.
  - `decide(reply: LeadReply | None, review: ReviewReply | None, qa: QAReply | None, errors: tuple[str, ...], critical: list[str], new: list[str], min_confidence: float) -> LeadDecision`. Pure, applied in **this order** (spec §6):
    1. critical → escalate `critical`;
    2. new → escalate `new`;
    3. QA failed, any `real_bug`, or a check error → `send_back`. `findings_to_fix` = real bugs + covered_style findings (style rides along only on a send-back);
    4. edge cases → `follow_ups` (they don't block);
    5. any tradeoff → escalate `tradeoff` with `options`;
    6. `confidence < min_confidence` → escalate `not_confident`;
    7. otherwise approve.
    - A finding the lead didn't classify counts as `real_bug`.
  - `async def run_lead(asker, cfg, gate, store, task, *, issue, diff_text, engineer: EngineerReply, preflight: PreflightReport, checks: CheckOutcome, changed_lines: int, fast_lane: bool) -> LeadDecision`.
    - The lead sees everything, including engineer notes; the lead is not a blind checker.
    - Fast lane with no critical/new hits, QA passed, no check errors, no findings above `nit` and `changed_lines <= loop.fast_lane_max_changed_lines` → approve **without** an LLM call (summary `"fast lane: rule-based approval"`).
    - Edge-case follow-ups are created as tasks (`source="followup"`).
    - Stores a `LEAD` artifact holding `asdict(decision)`. Comments live in the artifact because `ROLE_ACCESS` gives the lead no memory writes.

- [ ] **Step 1: Write the failing tests**

```python
def F(i, kind="bug", sev="major"): return Finding(id=i, severity=sev, kind=kind, title=i, detail="d")
def L(*cls, conf=0.9): return LeadReply(confidence=conf, classifications=[Classified(finding_id=i, category=c, note="") for i, c in cls], summary="s")

def test_critical_beats_everything():
    d = decide(L(), ReviewReply(findings=[], summary=""), QA_OK, (), ["security: src/auth.py"], [], 0.7)
    assert (d.outcome, d.category) == ("escalate", "critical")

def test_real_bug_sends_back_with_style_riding_along():
    d = decide(L(("a", "real_bug"), ("b", "covered_style")), REVIEW(F("a"), F("b", "style", "nit")), QA_OK, (), [], [], 0.7)
    assert d.outcome == "send_back" and [f.id for f in d.findings_to_fix] == ["a", "b"]

def test_style_alone_approves(): ...
def test_unclassified_finding_is_real_bug(): ...
def test_edge_case_becomes_follow_up_not_blocker(): ...
def test_tradeoff_escalates_with_options(): ...
def test_low_confidence_escalates(): ...
def test_qa_fail_or_check_error_sends_back(): ...
async def test_fast_lane_approves_without_llm(store, loop_cfg): ...
@pytest.mark.parametrize("path,hit", [("src/auth/login.py", True), ("db/migrations/001.sql", True), (".github/workflows/ci.yml", True), ("src/calc.py", False)])
def test_critical_patterns(path, hit): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/agents/test_lead.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: lead decision with finding classification and escalation`

---

### Task 11: gh write operations and PR body

**Files:**
- Modify: `src/agentco/github/client.py`, `src/agentco/github/models.py`
- Create: `src/agentco/workflow/pr.py`
- Test: `tests/github/test_writer.py`, `tests/workflow/test_pr.py`

**Interfaces:**
- Consumes: `GhCli(runner)`, `FakeGitHub`, `GitHubError`, `LeadDecision`, `QAReply`, `PreflightReport`, `PMReply`.
- Produces:
  - `PullRef(BaseModel)`: `number: int`, `url: str`.
  - `class GitHubWriter(Protocol)`: `create_pull(repo, head, base, title, body) -> PullRef`, `find_open_pull(repo, head) -> PullRef | None`, `edit_pull(repo, number, body) -> None`.
  - `GhCli` implements it:
    - `gh pr create --repo R --head H --base B --title T --body-file -`. The body goes through the runner's stdin; extend `Runner` to `Callable[[list[str], str | None], str]` with the input defaulting to `None`, and keep the existing read calls working. The number is parsed from the printed URL.
    - `gh pr list --repo R --head H --state open --json number,url`.
    - `gh pr edit N --repo R --body-file -`.
  - `FakeGitHub(..., fail_writes: bool = False)`: `.created: list[dict]`, `.edited: list[dict]`; `find_open_pull` reads `.created`. When `fail_writes` is set, writes raise `GitHubError`.
  - `pr_title(task: TaskRecord, issue: PMReply) -> str` (`f"{issue.summary} (agentco #{task.id})"`, ≤72 characters).
  - `pr_body(task, issue, decision: LeadDecision, qa: QAReply | None, preflight: PreflightReport, source_issue: int | None) -> str`. Sections:
    - Summary, Acceptance criteria, Lead review (summary + comments), QA (neighbour matrix table), Pre-flight (check list);
    - `Closes #N` when the task came from an issue;
    - the last line, `Ready to merge — a human merges this PR. agentco never merges.`

- [ ] **Step 1: Write the failing tests**

```python
def test_create_pull_argv_and_number():
    seen = []
    gh = GhCli(runner=lambda argv, input=None: seen.append((argv, input)) or "https://github.com/acme/app/pull/12\n")
    ref = gh.create_pull("acme/app", "agentco/task-1", "main", "t", "body")
    assert ref == PullRef(number=12, url="https://github.com/acme/app/pull/12")
    assert seen[0][0][:3] == ["gh", "pr", "create"] and seen[0][1] == "body"

def test_no_merge_anywhere():
    assert not any("merge" in n for n in dir(GhCli) + dir(FakeGitHub) if not n.startswith("_"))
    src = inspect.getsource(agentco.github.client) + inspect.getsource(agentco.workflow.pr)
    assert '"merge"' not in src and "'merge'" not in src

def test_find_open_pull_parses_json(): ...
def test_fake_records_and_fails_writes(): ...
def test_pr_body_closes_issue_and_says_human_merges(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/github tests/workflow/test_pr.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Existing `GhCli` tests pass a one-argument runner. Adapt it in `__init__`: when the runner takes one parameter, wrap it.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: gh PR create/edit (never merge) and PR body`

---

### Task 12: Orchestrator — the task state machine

**Files:**
- Create: `src/agentco/workflow/orchestrator.py`
- Test: `tests/workflow/test_orchestrator.py`, `tests/workflow/test_orchestrator_resume.py`

**Interfaces:**
- Consumes: everything above, plus `TaskStore.transition/assign/reassign_to_senior/get/get_meta`, `apply_stop_rules`, `StopDecision`, `refresh_main`, `create_worktree`, `push_branch`.
- Produces:
  - `class StepFailed(RuntimeError)`: `state: TaskState`, `reason: str`. Raised when an agent step fails in a state that cannot escalate (PM). The CLI maps it to exit 4.
  - `@dataclass TaskContext`: `cfg: TenantConfig`, `store: TaskStore`, `runtime: AgentRuntime`, `gate: MemoryGate`, `github: GitHubWriter`, `repo: str`, `repo_root: Path`, `contract: RepoContract`, `runner: CommandRunner`, `main: str = "main"`.
  - `class Orchestrator`: `__init__(self, ctx: TaskContext)`, `async def run(self, task_id: int) -> TaskRecord`.
  - `async def run_many(ctxs_and_ids: list[tuple[TaskContext, int]], limit: int) -> list[TaskRecord | BaseException]`. Uses an `asyncio.Semaphore(limit)` and `gather(return_exceptions=True)`.

**`run` loop** — dispatch on the state until it reaches `AWAITING_HUMAN` (with no new human decision), `PR_OPEN` or `CANCELLED`:

| State | Handler |
|---|---|
| INTAKE | → PM |
| PM | `run_pm`; `ReplyInvalid`/`AgentStuck` → `StepFailed` (stays in PM) → TRIAGE |
| TRIAGE | budget check; `run_triage(human_ok=…)`; escalate → `_escalate`; else `assign(route)` → ENGINEERING. A senior assignment is used as is (no reassign); the fast lane is kept in meta `task:{id}:fast_lane` |
| ENGINEERING | budget check; create/reuse the worktree; `run_engineer`. `EngineerFailed(stuck)` on a junior → `reassign_to_senior` (once) and retry; on a senior or a second failure → `_escalate("stuck")` → PREFLIGHT |
| PREFLIGHT | run. Pass → INTERNAL_REVIEW. Failed (no conflict) and attempts < `preflight_attempts` → ENGINEERING in the **same round** (existing edge; `preflight_failures` go into the brief). Exhausted or conflict → TRIAGE (round +1, back to first floor) then `_stop_rules()` |
| INTERNAL_REVIEW | `run_checks(run_review, run_qa)`, store REVIEW/QA artifacts → LEAD_DECISION |
| LEAD_DECISION | budget check; `critical_hits`, `new_hits`, `run_lead(changed_lines=changed_lines(wt, base))`. approve → `_open_pr()`; send_back → TRIAGE (round +1) then `_stop_rules()`, with the findings carried to the next engineer brief; escalate → `_escalate(category)` |
| AWAITING_HUMAN | `_resume()` (below) |

- `_stop_rules()`: `apply_stop_rules(task, cfg.budgets)`. `REASSIGN_SENIOR` → `reassign_to_senior`. `ESCALATE_HUMAN` → `_escalate("stuck")`.
- Budget check: over `budgets.per_task_usd + float(get_meta(f"task:{id}:extra_budget") or 0)` → `_escalate("stuck", reason="budget")`. It runs only in TRIAGE, ENGINEERING and LEAD_DECISION, the states with an edge to AWAITING_HUMAN.
- `_escalate(category, reason, options=())`: writes an `ESCALATION` artifact `{"from_state", "category", "reason", "options", "question"}` → AWAITING_HUMAN.
- `_open_pr()`:
  1. final pre-flight (`final=True`, full suite + e2e). On failure → TRIAGE (round +1);
  2. `push_branch`;
  3. `find_open_pull`, or else `create_pull`;
  4. a `PR` artifact `{"number", "url"}` → PR_OPEN.
  - A `GitError`/`GitHubError` → `_escalate("stuck", reason="push/gh failed: …")`. The state never reaches PR_OPEN without a PR. When already in AWAITING_HUMAN (human approve), there is no AWAITING_HUMAN→AWAITING_HUMAN edge: `_escalate` writes the new `ESCALATION` artifact and stays.
- `_resume()`: reads the latest `HUMAN_DECISION` newer than the latest `ESCALATION`. If there is none, return. Otherwise, by `decision` and the escalation's `from_state`:
  - `reject` → CANCELLED;
  - `approve` from `lead_decision` → `_open_pr()`;
  - `approve`/`answer` from `triage` → TRIAGE with `human_ok=True`;
  - from `engineering` → ENGINEERING with `human_note`;
  - `answer` from `lead_decision` → TRIAGE, with the note added as a real-bug finding for the next brief.
  - A stop-rule escalation (`category == "stuck"`) + `answer` buys exactly one more round (meta `task:{id}:max_rounds` + 1, read by a wrapper around `Budgets`).
  - A budget stop needs `--add-budget` (Task 13), or it escalates again immediately.
- Every handler re-reads the task with `store.get` and calls `transition` (compare-and-set), so two `task run`s on one task cannot both advance it.

- [ ] **Step 1: Write the failing tests** (`FakeRuntime` scripts per role, `ScriptRunner` from Task 6 tests moved to `tests/conftest.py`, `FakeGitHub`, `InMemoryBackend` seeded with one `rnd` fact, `make_repo`)

```python
async def test_happy_path_opens_pr_last(ctx, store):
    t = await Orchestrator(ctx).run(task_id)
    assert t.state is TaskState.PR_OPEN
    states = [a.detail.get("to") for a in store.audit(task_id) if a.action == "transition"]
    assert states == ["pm", "triage", "engineering", "preflight", "internal_review", "lead_decision", "pr_open"]
    assert len(ctx.github.created) == 1 and git(origin, "branch", "--list", "agentco/task-1")

async def test_send_back_never_opens_pr_and_reruns_with_scoped_findings(ctx, store): ...
async def test_three_junior_rounds_reassign_senior_then_escalate(ctx, store): ...
async def test_preflight_exhausted_counts_as_round(ctx, store): ...
async def test_rebase_conflict_goes_back_to_triage(ctx, store): ...
async def test_budget_overrun_escalates_stuck(ctx, store): ...
async def test_stuck_junior_handed_to_senior(ctx, store): ...
async def test_push_failure_escalates_not_pr_open(ctx, store):
    ctx.github = FakeGitHub(fail_writes=True)
    t = await Orchestrator(ctx).run(task_id)
    assert t.state is TaskState.AWAITING_HUMAN and ctx.github.created == []
async def test_critical_path_escalates_before_lead_llm(ctx, store): ...
async def test_unknown_area_escalates_new(ctx, store): ...
async def test_pm_invalid_twice_raises_step_failed_state_pm(ctx, store): ...
async def test_run_many_respects_limit(ctx_factory): ...   # max concurrent runtime calls observed == limit
```

`test_orchestrator_resume.py`: approve after a lead escalation → PR_OPEN; reject → CANCELLED; answer after a stop-rule escalation → exactly one more round; no decision → `run` returns immediately, still AWAITING_HUMAN.

- [ ] **Step 2: Run** `uv run pytest tests/workflow -v`. Expected: FAIL.
- [ ] **Step 3: Implement** as above. Engineer findings for round N come from the round N−1 `LEAD` artifact (`findings_to_fix`) and its `human_note`. `fix_since` is the meta `task:{id}:round_start_sha:{round}`, saved when entering ENGINEERING.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: in-process task orchestrator with stop rules and resume`

---

### Task 13: CLI — `task run`, `inbox`, `decide`; README; smoke test

**Files:**
- Modify: `src/agentco/cli.py`, `README.md`, `tests/test_cli.py`
- Create: `tests/test_task_loop_integration.py`

**Interfaces:**
- Consumes: `Orchestrator`, `TaskContext`, `run_many`, `StepFailed`, `CopilotRuntime`, `runner_for`, `load_repo_contract`, `validate_repo`, `TaskStore.list_tasks/latest_artifact/put_artifact`, `HumanDecision`, `MemoryGate`, `_load`, `_falkordb_guard`, `_fail`.
- Produces (new module-level seams, monkeypatched in tests):
  - `make_runtime: Callable[[TenantConfig], AgentRuntime] = lambda cfg: CopilotRuntime()`;
  - `make_command_runner: Callable[[RepoContract, Path, bool], CommandRunner] = runner_for`;
  - `make_github_writer: Callable[[], GitHubWriter] = GhCli`.
- Commands:
  - `task new <title> [--body TEXT]`: stores a `REQUEST` artifact `{"body": body or title}`.
  - `task run [<id> | --all] --repo o/r [--repo-path .] [--main main] [--host]`:
    - stores a `TARGET` artifact `{"repo", "main"}` on the first run;
    - `--all` runs every task in INTAKE…LEAD_DECISION, with `cfg.concurrency`;
    - prints the final state per task;
    - exits 4 on `StepFailed`, 3 on `GitHubError`, 2 when FalkorDB is unreachable.
  - `inbox`: a table of AWAITING_HUMAN tasks — id, title, category, from_state, reason, options, cost.
  - `decide <id> approve|reject|answer [--note TEXT] [--add-budget USD]`:
    - fails (exit 1) unless the task is AWAITING_HUMAN; `answer` needs `--note`;
    - stores a `HUMAN_DECISION` artifact, and writes a `company` episode through `gate.for_role(Role.HUMAN)` (`HumanDecision` entity text) inside `_falkordb_guard`;
    - `--add-budget` adds to meta `task:{id}:extra_budget`;
    - `reject` transitions to CANCELLED immediately; the others wait for the next `task run`.
- README: a "Run a task" section (`task new --body`, `task run`, `inbox`, `decide`), noting that a PR opens only after approval and a human always merges.
- `tests/test_task_loop_integration.py`: `@pytest.mark.integration`, skipped unless `AGENTCO_IT_COPILOT=1`. Real `CopilotRuntime` + `FakeGitHub` + `InMemoryBackend` (one seeded `rnd` fact) + a temp repo with a bare origin, a one-line bug, a failing-test-free suite and a host `.agentco.yml`. Asserts the end state ∈ {PR_OPEN, AWAITING_HUMAN} and `cost_usd > 0`. **Implementers do not run it.**

- [ ] **Step 1: Write the failing tests** (the `env` fixture in `tests/test_cli.py` additionally patches `make_runtime` → `FakeRuntime`, `make_command_runner` → `ScriptRunner`, `make_github_writer` → `FakeGitHub`)

```python
def test_task_run_happy_path_prints_pr_open(env, make_repo): ...
def test_task_run_step_failed_exit_4(env, make_repo): ...
def test_inbox_lists_escalations(env, make_repo): ...
def test_decide_requires_awaiting_human(env): ...            # exit 1
def test_decide_answer_requires_note(env): ...
def test_decide_writes_company_memory_as_human(env, make_repo): ...
def test_decide_reject_cancels(env, make_repo): ...
def test_decide_add_budget_then_run_resumes(env, make_repo): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `asyncio.run(...)` inside the typer command; the runtime is used as `async with` when it supports it. Write the README section and the integration test.
- [ ] **Step 4: Run** `uv run pytest -q` (the integration test is deselected by `addopts`) and `uv run ruff check src tests`. Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat: task run, inbox and decide commands`

---

## Contradictions resolved

1. **The lead writes no memory.** Spec §6 has the lead "write review comments", but `ROLE_ACCESS` gives the lead no writes. → Comments are stored in the `LEAD` artifact. Pre-flight enforces them for this task. The Librarian (Plan 4) turns them into ReviewRules.
2. **The Architect route.** The Architect exists only in Plan 5. → Plan 3 escalates `new_design` to the human inbox with category `new`.
3. **D10 "Issue/Playbook facts".** Facts carry no entity type. → "Known" means at least `known_min_facts` facts in the `rnd` graph.
4. **Indexing the task branch.** §8 indexes the task branch, but the `rnd` code graph is keyed to `main`. → Affected tests come from a per-task in-memory overlay; FalkorDB is never written for a branch.
5. **Which tests run when.** D13 runs affected tests per round; §5 runs all tests + e2e. → Affected tests run every round, and the full suite + e2e run once in the final pre-flight before the PR.
6. **D3 `docker compose run` without a service name.** → An optional `sandbox.service` is added, `devcontainer exec` is used for devcontainers, and `--host` is added. Agents' own shell calls run on the host, limited to contract commands.
7. **D4 "separate worktree".** A worktree shares git history. → QA gets an exported one-commit repo instead.
8. **A rebase conflict is a round (§14), but there was no PREFLIGHT→TRIAGE edge.** → The edge was added, and it counts as back to first floor.
9. **D13 "re-review only the fix diff" vs §5 "every fix is new code; rerun everything".** → The fix diff is reviewed as new code, the full pre-flight reruns, and QA reruns build + affected tests + the neighbour matrix.
10. **PM, PREFLIGHT and INTERNAL_REVIEW cannot reach AWAITING_HUMAN.** → The budget is checked only in the escalation-capable states, and a PM failure exits 4 without a transition.
11. **QA's back-channel issue to `rnd` (§7)** is only a `QA` artifact for now. The Librarian writes it in Plan 4.

## Self-review

- **Spec coverage:** §4 roles → Tasks 7–10. §5 lessons: fix = new code (Task 9), fix causes with one rule (Task 7 invariant), test the neighbours (Task 9), blind review before push (Task 9), small fixes (Tasks 6, 8), run everything locally + red check (Task 6), a human decides (Tasks 10, 12, 13). §6 lead order → Task 10. §7 isolation → Tasks 4, 9. §10 per-role models → Task 7. §14 → Tasks 4, 6, 12. D1/D2 → Task 3; D3/D4 → Task 4; D5 → Task 11; D7/D8 → Tasks 12–13; D10 → Task 7; D11 → Task 2; D12 → fakes throughout; D13 → fast lane (Tasks 7, 9, 10), parallel checks (Task 9), affected tests (Task 5), scoped fixes (Task 8), per-role models (config), parallel tasks (`run_many`, Task 12); warm pool deferred.
- **Interface consistency:** every cross-task name is defined in exactly one Interfaces block — `AgentCall`/`Asker`/`Reply` (2), `Worktree` (4), `Finding` (7), `CheckOutcome` (9), `LeadDecision` (10), `GitHubWriter`/`PullRef` (11), `StepFailed`/`TaskContext` (12).
- **Placeholders:** `...` in test listings marks bodies the implementer writes from the test name and the Interfaces block; every behaviour they test is specified above.
- **Unit-test safety:** no task runs Docker, `gh`, Copilot or FalkorDB; `ContainerRunner` is tested on argv only; the one network test is env-gated and deselected by default.
