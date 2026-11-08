# agentco Plan 6: Speed and fewer rounds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut agentco's time per task from ~55–75 min to ~10–15 min without lowering review quality.

**Architecture:** Benchmark profiling showed ~85% of agentco's time is repeated, sequential test runs (pre-flight per attempt, QA re-running build+tests, a second full pre-flight on approval) and extra review rounds. Three independent streams: (A) never run the same check twice and run independent checks concurrently; (B) fewer rounds via engineer shift-left, blocking-only send-backs, gated follow-ups and a deterministic lead auto-approve; (C) QA and reviewer that can actually see/run the code, plus Graphiti model config.

**Tech Stack:** Python 3.12, asyncio, pydantic, pytest, ruff, uv, git worktrees, github-copilot-sdk runtime, Graphiti + FalkorDB.

**Spec:** Benchmark findings (agentco vs plain agent, 3 issues, same model) — this plan's Architecture section and the profiling numbers: pre-flight 1486 s / task, internal_review 2100–2600 s / task (QA round-0 deterministic re-run + sleep), lead 167–696 s, engineering 81–175 s.

## Global Constraints

- Python 3.12; run `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pytest -q` — all must pass (this is CI).
- No new runtime dependencies.
- Behaviour that is not named in a task stays the same; existing tests may be updated only where a task changes the behaviour they pin.
- Never touch FalkorDB on port 6379.

## Review Focus

1. Cache key must change whenever the tree **or** the base changes — a stale "pass" after a rebase is the worst possible bug (A1 test `test_cache_misses_after_base_change`).
2. The red check must never leave the worktree reverted, including on cancellation, now that it runs in a copy (A2 test `test_red_runs_in_copy_and_worktree_untouched`).
3. A QA reply that ran no tests must not count as passed (C1 test `test_qa_without_tests_is_not_passed`).
4. Auto-approve must not fire on any non-nit finding, failed/absent QA, check error, critical/new path or required invariant (B3 parametrized test).
5. Follow-ups must never be filed on send_back rounds, and chores ("run the tests") are never filed (B2 tests).

---

## Stream A — Speed (branch `perf/plan6-speed`)

### Task A1: Check-result cache keyed by tree and base

**Files:**
- Create: `src/agentco/sandbox/check_cache.py`
- Modify: `src/agentco/workflow/preflight.py`
- Test: `tests/sandbox/test_check_cache.py`, `tests/workflow/test_preflight.py`

**Interfaces:**
- Produces: `class CheckCache` with `key(name: str, command: str, tree: str, base: str) -> tuple`, `get(key) -> CheckResult | None`, `put(key, result: CheckResult) -> None` (only `ok=True` results are stored); `def tree_sha(wt: Worktree) -> str` (`git rev-parse HEAD^{tree}`).
- `Preflight` gains field `cache: CheckCache = field(default_factory=CheckCache)`; every command check (lint, build, tests, e2e), `red` and `rules` consult it before running. Rules key's `command` is the sorted joined rule set (so new lead comments miss).

- [ ] Tests: `test_cache_hit_skips_runner` (runner call count stays 1 across two `Preflight.run` on same tree/base), `test_cache_misses_after_new_commit`, `test_cache_misses_after_base_change`, `test_failed_result_not_cached`.
- [ ] Implement; run tests; commit `perf: cache passing pre-flight checks by tree and base`.

### Task A2: Run independent checks concurrently

**Files:** Modify `src/agentco/workflow/preflight.py`; Test `tests/workflow/test_preflight.py`

- [ ] After size check and `_prepare`: run `rules`, `lint`, `build` with `asyncio.gather`. If build fails, skip tests/red (report build failure; lint and rules still reported). Otherwise run `tests` and `red` concurrently; `red` runs in a throwaway copy made with `sandbox.worktree.make_snapshot` under `wt.path.parents[1] / "red"` (never inside the worktree), reverts sources there, runs the new tests, then deletes the copy in `finally`. Call `runner.prepare` on the copy when the runner has it. Check order in the report stays: rebase, size, rules, lint, build, tests, red, e2e.
- [ ] Tests: `test_checks_run_concurrently` (fake runner with `asyncio`/thread barrier or timing: three 0.3 s checks finish in < 0.6 s), `test_red_runs_in_copy_and_worktree_untouched` (worktree files byte-identical after red; copy dir removed, also when the command raises), existing red tests still pass.
- [ ] Commit `perf: run pre-flight checks concurrently; red check in a copy`.

### Task A3: Final pre-flight reuses round results

**Files:** Modify `src/agentco/workflow/orchestrator.py` (`_open_pr`), `preflight.py`; Test `tests/workflow/test_orchestrator*.py` (whichever covers `_open_pr`).

- [ ] `final=True` keeps running the full `contract.test` and e2e, but lint/build/red/rules hit the cache when tree and base are unchanged since the passing round pre-flight. When the round pre-flight's affected-test selection already fell back to the full suite (same command string), the final tests are a cache hit too.
- [ ] Test `test_final_preflight_only_runs_full_suite_when_unchanged`: after a passing round, approval runs exactly one new command (the full suite) and zero rules LLM calls.
- [ ] Commit `perf: final pre-flight reuses unchanged round checks`.

## Stream B — Fewer rounds (branch `perf/plan6-rounds`)

### Task B1: Engineer shift-left

**Files:** Modify `src/agentco/agents/engineer.py`, `prompts/junior.md`, `prompts/senior.md`; Test `tests/agents/test_engineer.py`, `tests/agents/test_prompts.py`

- [ ] `run_engineer` searches `ScopedMemory` (RND scope) for `f"ReviewRule {issue summary}"`-style query (use the task title) and appends a `## Review rules the reviewer will apply` section (deduped, max 15) to the prompt when any are found.
- [ ] Prompt instructs a fixed list in the user prompt: "Commands you may run: …" (exact `_commands(contract)` plus the test-files prefix form `<prefix><test files>`), and in junior.md/senior.md: "Before you reply, run lint and the tests for the files you changed (and your new tests) and fix every failure. Do not reply with failing tests."
- [ ] Tests: `test_engineer_prompt_lists_review_rules`, `test_engineer_prompt_lists_commands`.
- [ ] Commit `feat: engineer sees review rules and runs tests before replying`.

### Task B2: Follow-ups only on approval, deduped, no chores

**Files:** Modify `src/agentco/agents/engineer.py` (stop filing at line ~191), `src/agentco/agents/lead.py` (`_file_follow_ups`, `run_lead`); Test `tests/agents/test_lead.py`, `tests/agents/test_engineer.py`

- [ ] Engineer no longer creates tasks; its `follow_ups` stay in the ENGINEER artifact.
- [ ] `run_lead` files follow-ups **only when outcome == "approve"**: lead edge_case findings + engineer follow-ups (from `engineer.follow_ups`, as titles) + nits (B3). Dedupe by normalized title (casefold, collapse whitespace, strip punctuation) against each other and against existing follow-ups of the task. Drop chores: title matches `^(run|re-?run|execute)\b.*\b(tests?|lint|linter|build|ci|suite)\b` (case-insensitive) or `^(add|write) (more )?tests?$`.
- [ ] Tests: `test_no_followups_on_send_back`, `test_followups_deduped`, `test_chore_followups_dropped`, `test_engineer_does_not_file_followups`.
- [ ] Commit `fix: file follow-ups only on approval, deduped, without chores`.

### Task B3: Blocking-only send-back and deterministic auto-approve

**Files:** Modify `src/agentco/agents/lead.py`; Test `tests/agents/test_lead.py`

- [ ] In `decide`: findings with `severity == "nit"` are never in `findings_to_fix` nor in the "real bugs" reasons; on approve they are filed as **one** follow-up titled `Nits from task {id}` (detail lists them).
- [ ] In `run_lead`, before calling the LLM, add an auto-approve path (outside fast lane too): no `require_invariant`, no critical, no new, no `checks.errors`, `preflight.ok`, `checks.qa` passed with non-empty `tests_run`, and every review finding is `nit` (or none). Outcome approve, reason `"auto: clean checks, only nits"`, summary from review summary. No LLM call.
- [ ] Tests: `test_nits_never_sent_back`, `test_auto_approve_without_llm` (asker not called), parametrized `test_auto_approve_blocked_by` over: minor finding, QA failed, QA no tests_run, check error, critical path, new dependency, require_invariant, preflight not ok.
- [ ] Commit `perf: auto-approve clean checks; nits never block`.

### Task B4: Round limit never hands off a red commit silently

**Files:** Modify `src/agentco/workflow/orchestrator.py` (`_stop_rules`); Test `tests/workflow/…`

- [ ] When escalating at the round limit, the escalation reason appends the failing checks of the latest PREFLIGHT artifact (`"; last pre-flight failed: tests, red"`) and the latest QA failures count, so the human sees the branch is red.
- [ ] Test `test_round_limit_escalation_reports_red_branch`.
- [ ] Commit `fix: round-limit escalation says when the branch is red`.

## Stream C — QA and reviewer that see the code (branch `perf/plan6-checks`)

### Task C1: QA runs tests for real, no duplicate deterministic run

**Files:** Modify `src/agentco/agents/qa.py`, `prompts/qa.md`; Test `tests/agents/test_qa.py`

- [ ] Remove the build + affected-tests run from `run_qa` (pre-flight already ran them on the same HEAD; the orchestrator only reaches review after a passing pre-flight). Fast lane returns `QAReply(passed=True, bug_class=FAST_LANE_BUG_CLASS, tests_run=["pre-flight: build and affected tests"], neighbour_matrix=[])` with no snapshot and no agent call. Delete `_deterministic_checks` and now-unused helpers.
- [ ] QA input lists the exact commands it may run and the test prefix form (`<prefix><paths or pytest args>`). `command_prefixes` also includes `contract.test + " "` so `uv run pytest -q -p no:cacheprovider tests/x.py -k y` is allowed.
- [ ] After the agent replies: if `tests_run` is empty, or every neighbour_matrix row is `n/a`, force `passed=False` with failure `QAFailure(title="QA ran no tests", …)`.
- [ ] qa.md step 1: "Run only the commands listed in the input; no shell operators (`&&`, `|`, `;`, redirects)."
- [ ] Tests: `test_qa_does_not_rerun_build_and_tests`, `test_fast_lane_qa_skips_agent`, `test_qa_prompt_lists_commands`, `test_qa_without_tests_is_not_passed`, `test_qa_all_na_is_not_passed`, and a `permission_allowed` test that the extended prefix admits `<contract.test> tests/x.py`.
- [ ] Commit `fix: QA runs real tests, never re-runs pre-flight, no-tests is not a pass`.

### Task C2: Reviewer gets a read-only worktree

**Files:** Modify `src/agentco/agents/review.py`, `prompts/reviewer.md`, `prompts/reviewer-light.md`; Test `tests/agents/test_review.py`

- [ ] `AgentCall` gets `workdir=wt.path, edits=False`, no commands (read-only git prefixes are already allowed by the runtime). Prompt: "You can read any file in the repository to confirm a finding before you report it; report only findings you verified in the code."
- [ ] Test `test_review_call_is_read_only_with_worktree` (workdir set, edits False, commands empty).
- [ ] Commit `feat: reviewer reads the worktree read-only`.

### Task C3: Graphiti models configurable; tree-sitter pin

**Files:** Modify `src/agentco/config.py`, `src/agentco/memory/graphiti_backend.py`, `pyproject.toml`, `uv.lock`; Test `tests/test_config.py`, `tests/memory/…`

- [ ] `TenantConfig.memory_models: MemoryModels` with `model: str = "gpt-5.5"`, `small_model: str = "gpt-4.1-mini"`, `embedder: str = "text-embedding-3-small"`; `build_graphiti_backend` passes `llm_client=OpenAIClient(LLMConfig(model=…, small_model=…))`, `embedder=OpenAIEmbedder(OpenAIEmbedderConfig(embedding_model=…))`, `cross_encoder=OpenAIRerankerClient(config=LLMConfig(model=small_model))`. Env overrides `AGENTCO_GRAPHITI_MODEL` / `AGENTCO_GRAPHITI_SMALL_MODEL` win when set.
- [ ] `tree-sitter>=0.25,<0.26` in pyproject; `uv lock`.
- [ ] Tests: `test_memory_models_default`, `test_build_graphiti_backend_uses_configured_models` (monkeypatch Graphiti to capture kwargs).
- [ ] Commit `fix: configurable Graphiti models (no gpt-4.1-nano), pin tree-sitter <0.26`.
