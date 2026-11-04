# Agent Company — Plan 1: Foundation (config, memory gate, task store) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local `agentco` CLI that loads tenant + repo config, enforces per-role graph access over Graphiti/FalkorDB, seeds the company policies once, and records tasks, state transitions, audit and cost — the base every agent in later plans runs on.

**Architecture:** Python package `agentco`. Agents never touch storage directly: they get a `ScopedMemory` from a `MemoryGate`, which maps a role to the graphs it may read/write (`{tenant}__{scope}`) and calls a `MemoryBackend` (Graphiti on FalkorDB in production, an in-memory fake in unit tests). Workflow bookkeeping lives in SQL via SQLAlchemy (SQLite locally, Postgres optional).

**Tech Stack:** Python 3.12, uv, graphiti-core[falkordb] 0.30.x, FalkorDB (Docker), SQLAlchemy 2.x, pydantic 2, PyYAML, typer, pytest + pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-11-04-agent-company-design.md` (§3, §4, §5 stop rules, §6 escalation rules, §8, §9 seed policies, §10, §11, §14 repo contract)

## Roadmap (the spec is split into five plans; each ships working software)

1. **Foundation** — this plan.
2. **Importer + code structure** — GitHub history (issues, PRs, review comments incl. human PRs) → graphs; FalkorDB/code-graph (tree-sitter) indexed into `rnd`; comments linked to functions.
3. **Local task loop** — model router, PM/Triage/Junior/Senior agents, worktree sandbox, pre-flight gate (rules, unit/e2e/lint/build, fail-without-fix, size guard), blind review, isolated QA + neighbour matrix, lead decision + finding classification, open PR last.
4. **PR feedback + learning** — intake of PR review comments, finding classification on PR comments, back-to-first-floor on the same PR, Librarian (rules, playbooks, risks, profiles, stale-rule flags), metrics report.
5. **Architect + task tree** — ADR → human approval → child tasks; root-cause step on 2nd recurrence; QA follow-up test PR.

## Global Constraints

- Python `>=3.12`; package name `agentco`; src layout (`src/agentco`).
- Graph names are exactly `{tenant}__{scope}`; scopes are exactly `company`, `product`, `rnd`, `qa`.
- Tenant names match `^[a-z0-9][a-z0-9-]{0,30}$` (no `_`, so `__` can never appear inside a tenant).
- Defaults: `budgets.per_task_usd: 5`, `budgets.max_rounds: 3`.
- Local storage defaults: FalkorDB `localhost:6379`; SQL `sqlite:///.agentco/state.db`.
- Nothing in this plan calls GitHub or any LLM except Graphiti's own extraction in the `integration` tests.
- Unit tests must not need Docker or network; anything that does is marked `@pytest.mark.integration` and skipped when `FALKORDB_HOST`/`OPENAI_API_KEY` are unset.
- Every commit message ends with `Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>`.

## Review Focus

- **`agentco init` run twice** → company policies are not added a second time (seed is recorded in the `meta` table). Test in Task 8.
- **Graph backend down during `init`** → exits non-zero with a message naming host:port, and the seed is *not* marked done, so a rerun seeds. Test in Task 8.
- **QA searches with no scopes given** → gets only `qa`, `product`, `company`, never `rnd`; default is "my readable scopes", never "all". Test in Task 3.
- **Tenant names that could collide** (`acme__rnd`, `Acme`, `a_b`, empty) → rejected at config load. Test in Task 2.
- **Cost exactly at budget** (`5.00` of `5`) → continues; only *exceeding* escalates. Test in Task 6.

## File Structure

```
pyproject.toml                         deps, ruff, pytest markers
docker-compose.yml                     falkordb + postgres for local runs
src/agentco/__init__.py
src/agentco/config.py                  TenantConfig, RoleModel, Budgets, load_config, family_warnings
src/agentco/repo_contract.py           RepoContract, load_repo_contract
src/agentco/roles.py                   Role enum
src/agentco/memory/scopes.py           Scope enum, graph_name, ROLE_ACCESS
src/agentco/memory/backend.py          MemoryItem, Fact, MemoryBackend protocol, InMemoryBackend
src/agentco/memory/gate.py             MemoryGate, ScopedMemory, AccessDenied
src/agentco/memory/entities.py         Graphiti custom entity types per scope
src/agentco/memory/graphiti_backend.py GraphitiBackend, build_graphiti_backend
src/agentco/memory/seed.py             SEED_POLICIES, DEFAULT_ESCALATION_RULES, seed_company
src/agentco/store/models.py            SQLAlchemy Task, AuditEvent, Meta
src/agentco/store/task_store.py        TaskStore
src/agentco/workflow/states.py         TaskState, ALLOWED_TRANSITIONS, BACK_TO_FIRST_FLOOR
src/agentco/workflow/stop_rules.py     StopDecision, apply_stop_rules
src/agentco/cli.py                     typer app: init, memory search, task new
tests/…                                mirrors src
```

---

### Task 1: Project skeleton + tenant config

**Files:**
- Create: `pyproject.toml`, `docker-compose.yml`, `.gitignore`, `src/agentco/__init__.py`, `src/agentco/roles.py`, `src/agentco/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Role(StrEnum)`: `PM, TRIAGE, ARCHITECT, SENIOR, JUNIOR, REVIEWER, QA, LEAD, LIBRARIAN, HUMAN` (values lowercase: `"pm"`, …, `"reviewer"`, …).
  - `RoleModel(BaseModel)`: `model: str`, `family: str`.
  - `Budgets(BaseModel)`: `per_task_usd: float = 5`, `max_rounds: int = 3`.
  - `TenantConfig(BaseModel)`: `tenant: str`, `falkordb_host: str = "localhost"`, `falkordb_port: int = 6379`, `database_url: str = "sqlite:///.agentco/state.db"`, `roles: dict[Role, RoleModel]`, `budgets: Budgets = Budgets()`.
  - `ConfigError(ValueError)`; `load_config(path: Path) -> TenantConfig`.
  - `family_warnings(cfg: TenantConfig) -> list[str]`.

- [ ] **Step 1: Scaffold** — `uv init --package agentco`; add deps `graphiti-core[falkordb]>=0.30,<0.31`, `sqlalchemy>=2`, `pydantic>=2`, `pyyaml`, `typer`; dev deps `pytest`, `pytest-asyncio`, `ruff`. In `pyproject.toml`: `[tool.pytest.ini_options] asyncio_mode = "auto"`, `markers = ["integration: needs FalkorDB + LLM key"]`. `docker-compose.yml`: `falkordb/falkordb:latest` on `6379` and `3000`, `postgres:16` on `5432`. `.gitignore` includes `.agentco/`. `git init`.

- [ ] **Step 2: Write the failing tests**

```python
def test_loads_example_config(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE))      # EXAMPLE = spec §10 yaml + reviewer + a family per role
    assert cfg.tenant == "acme"
    assert cfg.roles[Role.QA].family == "openai"
    assert cfg.budgets.per_task_usd == 5 and cfg.budgets.max_rounds == 3

def test_missing_role_is_error(tmp_path):
    with pytest.raises(ConfigError, match="reviewer"):
        load_config(write(tmp_path, EXAMPLE_WITHOUT_REVIEWER))

def test_checker_same_model_as_coder_is_error(tmp_path):
    with pytest.raises(ConfigError, match="qa.*same model"):      # qa.model == senior.model
        load_config(write(tmp_path, EXAMPLE_QA_EQUALS_SENIOR))

def test_same_family_is_warning_not_error(tmp_path):
    cfg = load_config(write(tmp_path, EXAMPLE_ALL_ANTHROPIC_DIFFERENT_MODELS))
    assert any("qa" in w and "same family" in w for w in family_warnings(cfg))
```

- [ ] **Step 3: Run** `uv run pytest tests/test_config.py -v` — Expected: FAIL (import error).

- [ ] **Step 4: Implement** `load_config` and `family_warnings`. The nine roles other than `HUMAN` are required in `roles`. "Checker" roles are `REVIEWER, QA, LEAD`; "coder" roles are `SENIOR, JUNIOR`. A checker with the same `model` string as a coder → `ConfigError`; the same `family` → one warning per checker (spec §14 "warns once").

- [ ] **Step 5: Run** the tests — PASS; `uv run ruff check .` — clean.

- [ ] **Step 6: Commit** `feat: project skeleton and tenant config`

---

### Task 2: Scopes, graph naming, role access table

**Files:**
- Create: `src/agentco/memory/__init__.py`, `src/agentco/memory/scopes.py`
- Modify: `src/agentco/config.py` (validate `tenant` with `TENANT_RE`)
- Test: `tests/memory/test_scopes.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `Role` (Task 1).
- Produces:
  - `Scope(StrEnum)`: `COMPANY="company", PRODUCT="product", RND="rnd", QA="qa"`.
  - `TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")`.
  - `graph_name(tenant: str, scope: Scope) -> str` — raises `ValueError` on a bad tenant.
  - `Access(NamedTuple)`: `read: frozenset[Scope]`, `write: frozenset[Scope]`.
  - `ROLE_ACCESS: Mapping[Role, Access]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_graph_name():
    assert graph_name("acme", Scope.RND) == "acme__rnd"

@pytest.mark.parametrize("bad", ["", "Acme", "a_b", "acme__rnd", "a b", "x" * 32])
def test_bad_tenant_rejected(bad):
    with pytest.raises(ValueError):
        graph_name(bad, Scope.RND)

def test_access_table_matches_spec():
    A = lambda r, w: Access(frozenset(r), frozenset(w))
    assert ROLE_ACCESS[Role.PM]        == A({PRODUCT, COMPANY}, {PRODUCT})
    assert ROLE_ACCESS[Role.TRIAGE]    == A({RND, PRODUCT}, set())
    assert ROLE_ACCESS[Role.ARCHITECT] == A({RND, PRODUCT, COMPANY}, {RND})
    for r in (Role.SENIOR, Role.JUNIOR, Role.REVIEWER):
        assert ROLE_ACCESS[r] == A({RND, PRODUCT, COMPANY}, set())
    assert ROLE_ACCESS[Role.QA]        == A({QA, PRODUCT, COMPANY}, {QA})
    assert ROLE_ACCESS[Role.LEAD]      == A(set(Scope), set())
    assert ROLE_ACCESS[Role.LIBRARIAN] == A(set(Scope), {RND, QA, COMPANY})
    assert ROLE_ACCESS[Role.HUMAN]     == A(set(Scope), {COMPANY, RND})

def test_every_role_has_access_entry():
    assert set(ROLE_ACCESS) == set(Role)

def test_config_rejects_bad_tenant(tmp_path):          # in tests/test_config.py
    with pytest.raises(ConfigError, match="tenant"):
        load_config(write(tmp_path, EXAMPLE.replace("tenant: acme", "tenant: acme__x")))
```

(Lead writes nothing to graphs: its decisions go to the task store and the Librarian turns its comments into rules — spec §9. Engineers' output goes to git.)

- [ ] **Step 2: Run** `uv run pytest tests -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** `scopes.py`; add a `tenant` validator on `TenantConfig` using `TENANT_RE`.
- [ ] **Step 4: Run** `uv run pytest tests -v` — PASS.
- [ ] **Step 5: Commit** `feat: graph scopes and role access table`

---

### Task 3: Memory backend protocol, in-memory fake, memory gate

**Files:**
- Create: `src/agentco/memory/backend.py`, `src/agentco/memory/gate.py`
- Test: `tests/memory/test_gate.py`

**Interfaces:**
- Consumes: `Scope`, `graph_name`, `ROLE_ACCESS` (Task 2); `Role` (Task 1).
- Produces:
  - `@dataclass(frozen=True) MemoryItem`: `name: str`, `body: str`, `source_description: str`, `reference_time: datetime`.
  - `@dataclass(frozen=True) Fact`: `text: str`, `graph: str`, `uuid: str`, `valid_at: datetime | None`, `invalid_at: datetime | None`.
  - `class MemoryBackend(Protocol)`: `async add(graph: str, scope: Scope, item: MemoryItem) -> None`; `async search(graphs: list[str], query: str, limit: int) -> list[Fact]`; `async ensure_graph(graph: str) -> None`.
  - `InMemoryBackend` — implements the protocol; `search` returns stored items whose `body` contains any query word (case-insensitive), at most `limit`; extra helper `items(graph: str) -> list[MemoryItem]`.
  - `AccessDenied(PermissionError)`.
  - `MemoryGate(tenant: str, backend: MemoryBackend)`; `MemoryGate.for_role(role: Role) -> ScopedMemory`.
  - `ScopedMemory`: `role: Role`, `readable: frozenset[Scope]`, `async search(query: str, scopes: Iterable[Scope] | None = None, limit: int = 10) -> list[Fact]`, `async add(scope: Scope, item: MemoryItem) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_qa_cannot_read_rnd():
    qa = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    with pytest.raises(AccessDenied, match="qa.*rnd"):
        await qa.search("anything", scopes=[Scope.RND])

async def test_default_scopes_are_role_readable_only():
    backend = InMemoryBackend(); gate = MemoryGate("acme", backend)
    await gate.for_role(Role.LIBRARIAN).add(Scope.RND, item("engineer note: retry loop"))
    await gate.for_role(Role.LIBRARIAN).add(Scope.QA, item("qa: retry loop flaky"))
    facts = await gate.for_role(Role.QA).search("retry")
    assert {f.graph for f in facts} == {"acme__qa"}

async def test_empty_scope_list_means_nothing_not_everything():
    qa = MemoryGate("acme", InMemoryBackend()).for_role(Role.QA)
    assert await qa.search("x", scopes=[]) == []

async def test_write_outside_role_denied():
    with pytest.raises(AccessDenied):
        await MemoryGate("acme", InMemoryBackend()).for_role(Role.QA).add(Scope.RND, item("x"))

async def test_tenants_never_mix():
    backend = InMemoryBackend()
    await MemoryGate("acme", backend).for_role(Role.LIBRARIAN).add(Scope.RND, item("acme secret"))
    assert await MemoryGate("globex", backend).for_role(Role.LEAD).search("secret") == []
```

- [ ] **Step 2: Run** `uv run pytest tests/memory/test_gate.py -v` — FAIL.
- [ ] **Step 3: Implement.** `ScopedMemory` checks every requested scope against `ROLE_ACCESS[role]` *before* calling the backend (error message names role and scope); `scopes=None` → `readable`; empty list → return `[]` without calling the backend; the backend only ever receives names built by `graph_name(self.tenant, …)`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: memory gate enforcing per-role graph access`

---

### Task 4: Entity types + Graphiti backend

**Files:**
- Create: `src/agentco/memory/entities.py`, `src/agentco/memory/graphiti_backend.py`
- Test: `tests/memory/test_entities.py`, `tests/memory/test_graphiti_backend_integration.py`

**Interfaces:**
- Consumes: `MemoryBackend`, `MemoryItem`, `Fact` (Task 3); `Scope` (Task 2); `TenantConfig` (Task 1).
- Produces:
  - `ENTITY_TYPES: dict[Scope, dict[str, type[BaseModel]]]`, per spec §8:
    - company: `Policy`, `Convention`, `EscalationRule`, `HumanDecision`
    - product: `Feature`, `Spec`, `AcceptanceCriterion`, `Customer`, `Feedback`
    - rnd: `Module`, `File`, `Function`, `Class`, `Issue`, `PullRequest`, `ReviewComment`, `ReviewRule`, `Learning`, `Playbook`, `Risk`, `Decision`, `AgentProfile`
    - qa: `TestCase`, `TestRun`, `FlakyTest`, `EscapedBug`, `QALearning`, `NeighbourCase`
    - Every model has a one-line docstring (Graphiti uses it during extraction) and only optional fields. Fields to define now: `ReviewRule.module_path: str | None`, `ReviewRule.origin: Literal["lead","human","seed"] | None`; `EscalationRule.category: Literal["critical","new","stuck"] | None`; `Issue.number: int | None`; `PullRequest.number: int | None`; `Module.path: str | None`; `File.path: str | None`; `NeighbourCase.bug_class: str | None`. The rest: docstring only (later plans add fields).
  - `GraphitiBackend(graphiti: Graphiti)` implementing `MemoryBackend`.
  - `build_graphiti_backend(cfg: TenantConfig) -> GraphitiBackend` — `Graphiti(graph_driver=FalkorDriver(host=cfg.falkordb_host, port=cfg.falkordb_port))`.

- [ ] **Step 1: Write the failing tests**

```python
RESERVED = {"uuid", "name", "group_id", "labels", "created_at", "summary", "attributes", "name_embedding"}

def test_entity_types_cover_spec():
    assert set(ENTITY_TYPES[Scope.QA]) == {"TestCase","TestRun","FlakyTest","EscapedBug","QALearning","NeighbourCase"}
    assert {"ReviewRule", "Function", "Playbook", "Risk"} <= set(ENTITY_TYPES[Scope.RND])

def test_no_reserved_field_names():
    for types in ENTITY_TYPES.values():
        for model in types.values():
            assert not RESERVED & set(model.model_fields), model.__name__

def test_every_entity_has_docstring():
    for types in ENTITY_TYPES.values():
        for model in types.values():
            assert (model.__doc__ or "").strip(), model.__name__
```

```python
@pytest.mark.integration
async def test_add_then_search_stays_in_graph(cfg):   # cfg fixture from env, tenant f"itest{random}"
    b = build_graphiti_backend(cfg); rnd, qa = f"{cfg.tenant}__rnd", f"{cfg.tenant}__qa"
    await b.ensure_graph(rnd); await b.ensure_graph(qa)
    await b.add(rnd, Scope.RND, item("Review rule: always debounce graph switch in GraphView"))
    found = await b.search([rnd], "graph switch", 5)
    assert found and all(f.graph == rnd for f in found)
    assert await b.search([qa], "graph switch", 5) == []
```

- [ ] **Step 2: Run** `uv run pytest tests/memory -v` — entity tests FAIL; integration SKIPPED without env.
- [ ] **Step 3: Implement.**
  - `add` → `graphiti.add_episode(name=…, episode_body=…, source_description=…, reference_time=…, source=EpisodeType.text, group_id=graph, entity_types=ENTITY_TYPES[scope])`.
  - `search` → `graphiti.search(query, group_ids=graphs, num_results=limit)`, each edge mapped to `Fact(text=e.fact, graph=e.group_id, uuid=e.uuid, valid_at=e.valid_at, invalid_at=e.invalid_at)`.
  - `ensure_graph` → `graphiti.driver.clone(database=graph).build_indices_and_constraints()`.
- [ ] **Step 4: Run** unit tests — PASS. Then `docker compose up -d falkordb` and `FALKORDB_HOST=localhost OPENAI_API_KEY=… uv run pytest -m integration -v` — PASS.
- [ ] **Step 5: Commit** `feat: Graphiti entity types and FalkorDB backend`

---

### Task 5: Task store, workflow states, audit + cost

**Files:**
- Create: `src/agentco/workflow/__init__.py`, `src/agentco/workflow/states.py`, `src/agentco/store/__init__.py`, `src/agentco/store/models.py`, `src/agentco/store/task_store.py`
- Test: `tests/workflow/test_states.py`, `tests/store/test_task_store.py`

**Interfaces:**
- Consumes: `Role` (Task 1).
- Produces:
  - `TaskState(StrEnum)`: `INTAKE, PM, TRIAGE, ARCHITECT, ENGINEERING, PREFLIGHT, INTERNAL_REVIEW, LEAD_DECISION, AWAITING_HUMAN, PR_OPEN, MERGED, CANCELLED`.
  - `ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]]` — exactly:
    - INTAKE→{PM}; PM→{TRIAGE}; TRIAGE→{ENGINEERING, ARCHITECT, AWAITING_HUMAN}; ARCHITECT→{AWAITING_HUMAN}
    - ENGINEERING→{PREFLIGHT, AWAITING_HUMAN}; PREFLIGHT→{ENGINEERING, INTERNAL_REVIEW}; INTERNAL_REVIEW→{LEAD_DECISION}
    - LEAD_DECISION→{TRIAGE, AWAITING_HUMAN, PR_OPEN}; PR_OPEN→{TRIAGE, AWAITING_HUMAN, MERGED}
    - AWAITING_HUMAN→{TRIAGE, ENGINEERING, PR_OPEN, CANCELLED}
    - plus every non-terminal state→CANCELLED; `MERGED` and `CANCELLED` are terminal (empty set).
  - `BACK_TO_FIRST_FLOOR: frozenset[tuple[TaskState, TaskState]] = {(LEAD_DECISION, TRIAGE), (PR_OPEN, TRIAGE)}` — these increment `round`.
  - `@dataclass TaskRecord`: `id: int`, `tenant: str`, `parent_id: int | None`, `title: str`, `source: str`, `source_ref: str | None`, `state: TaskState`, `assignee: Role | None`, `round: int`, `reassigned: bool`, `cost_usd: float`.
  - `@dataclass AuditEntry`: `at: datetime`, `actor: Role | None`, `action: str`, `detail: dict`, `cost_usd: float`.
  - `InvalidTransition(ValueError)`.
  - `TaskStore(url: str)`: `create_all() -> None`; `create_task(tenant: str, title: str, source: str, source_ref: str | None = None, parent_id: int | None = None) -> TaskRecord`; `get(task_id: int) -> TaskRecord`; `transition(task_id: int, to: TaskState, actor: Role, reason: str) -> TaskRecord`; `assign(task_id: int, role: Role) -> TaskRecord`; `record_cost(task_id: int, actor: Role, usd: float, detail: dict) -> TaskRecord`; `audit(task_id: int) -> list[AuditEntry]`; `get_meta(key: str) -> str | None`; `set_meta(key: str, value: str) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lead_cannot_skip_to_merged():
    assert TaskState.MERGED not in ALLOWED_TRANSITIONS[TaskState.LEAD_DECISION]

def test_pr_only_opens_from_lead_or_human():
    sources = {s for s, tos in ALLOWED_TRANSITIONS.items() if TaskState.PR_OPEN in tos}
    assert sources == {TaskState.LEAD_DECISION, TaskState.AWAITING_HUMAN}

def test_back_to_first_floor_increments_round(store):
    t = walk(store, to=TaskState.LEAD_DECISION)     # helper: INTAKE→PM→TRIAGE→ENGINEERING→PREFLIGHT→INTERNAL_REVIEW→LEAD_DECISION
    t = store.transition(t.id, TaskState.TRIAGE, Role.LEAD, "QA failed")
    assert t.round == 1

def test_preflight_fail_does_not_increment_round(store):
    t = walk(store, to=TaskState.PREFLIGHT)
    assert store.transition(t.id, TaskState.ENGINEERING, Role.JUNIOR, "lint").round == 0

def test_invalid_transition_raises_and_is_not_audited(store):
    t = store.create_task("acme", "x", "cli")
    with pytest.raises(InvalidTransition):
        store.transition(t.id, TaskState.PR_OPEN, Role.LEAD, "skip")
    assert [e.action for e in store.audit(t.id)] == ["created"]

def test_every_transition_and_cost_is_audited(store):
    t = store.create_task("acme", "x", "cli")
    store.transition(t.id, TaskState.PM, Role.PM, "start")
    t = store.record_cost(t.id, Role.PM, 0.12, {"model": "fast-cheap"})
    assert t.cost_usd == pytest.approx(0.12)
    assert [e.action for e in store.audit(t.id)] == ["created", "transition", "cost"]
```

(`store` fixture: `TaskStore(f"sqlite:///{tmp_path}/s.db")` then `create_all()`.)

- [ ] **Step 2: Run** `uv run pytest tests/workflow tests/store -v` — FAIL.
- [ ] **Step 3: Implement.** Tables: `tasks` (fields of `TaskRecord` + `created_at`, `updated_at`), `audit_events` (`id`, `task_id`, `at`, `actor`, `action`, `detail` JSON, `cost_usd`), `meta` (`key` PK, `value`). Each mutating call writes its audit row in the same transaction. `create_task` creates the parent dir of a SQLite file if missing.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: task store with workflow states, audit and cost`

---

### Task 6: Stop rules

**Files:**
- Create: `src/agentco/workflow/stop_rules.py`
- Modify: `src/agentco/store/task_store.py` (add `reassign_to_senior`)
- Test: `tests/workflow/test_stop_rules.py`

**Interfaces:**
- Consumes: `TaskRecord`, `TaskStore` (Task 5), `Budgets` (Task 1), `Role`.
- Produces: `StopDecision(StrEnum)`: `CONTINUE, REASSIGN_SENIOR, ESCALATE_HUMAN`; `apply_stop_rules(task: TaskRecord, budgets: Budgets) -> StopDecision`; `TaskStore.reassign_to_senior(task_id: int) -> TaskRecord` (sets `assignee=SENIOR`, `round=0`, `reassigned=True`; audited as `"reassigned"`).

- [ ] **Step 1: Write the failing tests** (spec §5 stop rules)

```python
B = Budgets()  # 5 USD, 3 rounds
def test_junior_after_3_rounds_goes_to_senior():
    assert apply_stop_rules(rec(Role.JUNIOR, round=3), B) is StopDecision.REASSIGN_SENIOR
def test_reassign_happens_once():
    assert apply_stop_rules(rec(Role.SENIOR, round=3, reassigned=True), B) is StopDecision.ESCALATE_HUMAN
def test_senior_after_3_rounds_goes_to_human():
    assert apply_stop_rules(rec(Role.SENIOR, round=3), B) is StopDecision.ESCALATE_HUMAN
def test_under_limit_continues():
    assert apply_stop_rules(rec(Role.JUNIOR, round=2), B) is StopDecision.CONTINUE
def test_cost_at_budget_continues_over_budget_escalates():
    assert apply_stop_rules(rec(Role.JUNIOR, cost=5.0), B) is StopDecision.CONTINUE
    assert apply_stop_rules(rec(Role.JUNIOR, cost=5.01), B) is StopDecision.ESCALATE_HUMAN
def test_reassign_resets_round(store):
    t = store.assign(store.create_task("acme", "x", "cli").id, Role.JUNIOR)
    t = store.reassign_to_senior(t.id)
    assert (t.assignee, t.round, t.reassigned) == (Role.SENIOR, 0, True)
    assert store.audit(t.id)[-1].action == "reassigned"
```

- [ ] **Step 2: Run** `uv run pytest tests/workflow/test_stop_rules.py -v` — FAIL.
- [ ] **Step 3: Implement.** Cost check first (`cost_usd > per_task_usd` → escalate), then `round >= max_rounds` (junior and not `reassigned` → reassign; otherwise escalate).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: stop rules (rounds, junior→senior, budget)`

---

### Task 7: Repo contract (`.agentco.yml`)

**Files:**
- Create: `src/agentco/repo_contract.py`
- Test: `tests/test_repo_contract.py`

**Interfaces:**
- Produces: `Sandbox(BaseModel)`: `devcontainer: str | None = None`, `compose: str | None = None` (exactly one set); `RepoContract(BaseModel)`: `build: str`, `test: str`, `e2e: str | None` (key required, value may be `null`), `lint: str`, `sandbox: Sandbox`; `load_repo_contract(repo_root: Path) -> RepoContract`; `RepoContractMissing(FileNotFoundError)`; `RepoContractError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_missing_file(tmp_path):
    with pytest.raises(RepoContractMissing):
        load_repo_contract(tmp_path)

def test_e2e_key_required_but_may_be_null(tmp_path):
    write_contract(tmp_path, without="e2e")
    with pytest.raises(RepoContractError, match="e2e"):
        load_repo_contract(tmp_path)
    write_contract(tmp_path, e2e=None)
    assert load_repo_contract(tmp_path).e2e is None

def test_sandbox_needs_exactly_one(tmp_path):
    write_contract(tmp_path, sandbox={"devcontainer": ".devcontainer", "compose": "c.yml"})
    with pytest.raises(RepoContractError, match="sandbox"):
        load_repo_contract(tmp_path)
```

(`e2e: null` is an explicit "this repo has no e2e tests"; Plan 3's pre-flight records it in the audit. My call: the design requires e2e, but some repos have none, and a missing key is more likely a mistake than a choice.)

- [ ] **Step 2: Run** `uv run pytest tests/test_repo_contract.py -v` — FAIL.
- [ ] **Step 3: Implement** with pydantic; wrap `ValidationError` in `RepoContractError`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: .agentco.yml repo contract`

---

### Task 8: Seed company memory + CLI (`init`, `memory search`, `task new`)

**Files:**
- Create: `src/agentco/memory/seed.py`, `src/agentco/cli.py`
- Modify: `pyproject.toml` (`[project.scripts] agentco = "agentco.cli:app"`)
- Test: `tests/memory/test_seed.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `SEED_POLICIES: list[tuple[str, str]]` — `(slug, text)`, exactly these seven (spec §9), text one sentence each taken from the user's lessons (spec §5–§7, §9): `every-fix-is-new-code`, `fix-cause-with-one-rule`, `test-the-neighbours`, `blind-review-before-push`, `keep-fixes-small`, `run-everything-locally`, `stop-when-not-real`.
  - `DEFAULT_ESCALATION_RULES: list[tuple[str, str, str]]` — `(slug, category, text)`, one per bullet in spec §6: 6 `critical`, 4 `new`, 1 `stuck`.
  - `SEED_VERSION = "1"`.
  - `async seed_company(mem: ScopedMemory, store: TaskStore, now: datetime) -> bool` — if `store.get_meta("seed_version") == SEED_VERSION` return `False`; else add each item to `Scope.COMPANY` (names `policy:<slug>` / `escalation:<slug>`), then `set_meta("seed_version", SEED_VERSION)` and return `True`. Meta is set only after every add succeeds. Called with the `HUMAN` role's `ScopedMemory`.
  - `cli.make_backend: Callable[[TenantConfig], MemoryBackend]` (default `build_graphiti_backend`) — the seam CLI tests replace with `InMemoryBackend`.
  - Commands (typer):
    - `agentco init --config PATH` → load config, print each `family_warnings` line, `store.create_all()`, `ensure_graph` for the four graphs, `seed_company`; prints `seeded` or `already seeded`. Backend connection error → prints `cannot reach FalkorDB at {host}:{port}`, exit code 2.
    - `agentco memory search QUERY --as ROLE --config PATH` → one `graph<TAB>fact` line per result.
    - `agentco task new TITLE --config PATH` → creates a task with `source="cli"`, prints its id.

- [ ] **Step 1: Write the failing tests**

```python
async def test_seed_writes_policies_and_rules_once(store):
    backend = InMemoryBackend(); mem = MemoryGate("acme", backend).for_role(Role.HUMAN)
    assert await seed_company(mem, store, NOW) is True
    assert await seed_company(mem, store, NOW) is False
    assert len(backend.items("acme__company")) == 7 + 11

async def test_seed_only_touches_company(store):
    backend = InMemoryBackend()
    await seed_company(MemoryGate("acme", backend).for_role(Role.HUMAN), store, NOW)
    for s in ("product", "rnd", "qa"):
        assert backend.items(f"acme__{s}") == []

def test_init_then_search_as_qa(cli_env):          # CliRunner; make_backend → shared InMemoryBackend
    assert run("init").exit_code == 0
    out = run("memory", "search", "neighbours", "--as", "qa").stdout
    assert "acme__company" in out and "acme__rnd" not in out

def test_init_twice_says_already_seeded(cli_env):
    run("init")
    assert "already seeded" in run("init").stdout

def test_init_backend_down_exits_2_and_does_not_mark_seeded(cli_env_failing_backend):
    r = run("init")
    assert r.exit_code == 2 and "cannot reach FalkorDB at localhost:6379" in r.stdout
    assert store_of(cli_env_failing_backend).get_meta("seed_version") is None

def test_task_new_prints_id(cli_env):
    r = run("task", "new", "Graph switch flickers")
    assert r.exit_code == 0 and r.stdout.strip().isdigit()
```

(`cli_env_failing_backend`: a backend whose `ensure_graph`/`add` raise `ConnectionError`.)

- [ ] **Step 2: Run** `uv run pytest tests/memory/test_seed.py tests/test_cli.py -v` — FAIL.
- [ ] **Step 3: Implement** `seed.py` and `cli.py`. The CLI wraps async calls in `asyncio.run`; `ConnectionError` and `redis.exceptions.ConnectionError` map to exit 2.
- [ ] **Step 4: Run** the full unit suite `uv run pytest -v` and `uv run ruff check .` — PASS, clean.
- [ ] **Step 5: Manual end-to-end** — `docker compose up -d falkordb`; with a real `agentco.config.yml` (tenant `acme`): `uv run agentco init --config agentco.config.yml` → `seeded`; run again → `already seeded`; `uv run agentco memory search "neighbours" --as qa --config agentco.config.yml` → at least one `acme__company` line and no `acme__rnd`. In FalkorDB Browser (`localhost:3000`) the graphs `acme__company`, `acme__product`, `acme__rnd`, `acme__qa` exist.
- [ ] **Step 6: Commit** `feat: seed company policies and agentco CLI`
