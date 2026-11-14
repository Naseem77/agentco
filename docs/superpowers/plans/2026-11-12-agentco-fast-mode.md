# agentco fast mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `pipeline: fast` mode where one senior engineer and one CodeRabbit-style reviewer iterate locally (with live code-graph impact and imported bot lessons) until the diff is clean, then run deterministic checks once and open the PR.

**Architecture:** Reuse the existing state machine and its edges; in fast mode the PM, triage and lead handlers become deterministic rules, pre-flight runs only at PR time (no LLM rules check), and INTERNAL_REVIEW runs one whole-diff reviewer fed by an impact report built from an in-memory overlay code graph of the worktree. Only one new edge: ENGINEERING → INTERNAL_REVIEW.

**Tech Stack:** Python 3.12, pydantic, pytest, tree-sitter (existing `code/parse.py`), existing `InMemoryCodeGraph`.

**Spec:** `docs/superpowers/specs/2026-11-12-agentco-fast-mode-design.md`

## Global Constraints

- Default stays `pipeline: company`; every existing test passes unchanged.
- Fast mode makes exactly two LLM calls per round: engineer, reviewer.
- A finding of severity `blocker` or `major` blocks the PR; send-back carries every non-nit finding in one batch.
- Round limit is the existing `budgets.max_rounds` (default 3 = 1 review + 2 fix rounds); past it, escalate to a human, never open the PR.
- The shared code graph is never written with unmerged worktree code.
- CI: `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pytest -q`. Commit locally only; no push, no PR.

## Review Focus

- Reviewer agent fails or returns invalid JSON → escalate "stuck", don't approve (Task 4 test).
- Impact check on a diff that only deletes files or touches non-code files → report says so, never raises (Task 2 test).
- Final pre-flight fails in fast mode → back to engineering with the failures, round counted (Task 4 test).
- Bot reply under a human thread still dropped; bot comment from a bot not in `review_bots` still skipped (Task 1 test).
- Fast-mode PR feedback round (PR_OPEN → TRIAGE) reaches engineering without an LLM triage (Task 4 test).

---

### Task 1: Import review-bot comments as lessons

**Files:**
- Modify: `src/agentco/config.py` (`IntakeConfig.review_bots` default adds `"coderabbitai[bot]"`)
- Modify: `src/agentco/importer/episodes.py` (`review_threads`)
- Modify: `src/agentco/importer/run.py` (pass `frozenset(cfg-bots)` through `ImportOptions`)
- Test: `tests/importer/test_episodes.py`

**Interfaces:**
- Produces: `review_threads(comments, pulls, review_bots: frozenset[str] = frozenset()) -> ThreadSelection`; `ImportOptions.review_bots: frozenset[str] = frozenset()`.

- [ ] Step 1: tests `test_review_bot_root_is_kept` (root by `coderabbitai[bot]`, association `NONE` → 1 thread), `test_other_bot_root_still_skipped` (`dependabot[bot]` → skipped["bot"] == 1), `test_bot_reply_under_kept_root_dropped`.
- [ ] Step 2: run, expect FAIL.
- [ ] Step 3: a root whose author login is in `review_bots` skips the bot / not_team checks (pr_author check still applies). CLI import passes `cfg.intake.review_bots`.
- [ ] Step 4: run tests, PASS. Step 5: commit `feat(importer): keep review-bot comments as lessons`.

### Task 2: Live impact report

**Files:**
- Create: `src/agentco/code/impact.py`
- Test: `tests/codegraph/test_impact.py`

**Interfaces:**
- Consumes: `changed_line_ranges(wt, base)`, `index_repo`, `InMemoryCodeGraph`, `code_graph_for` (as in `sandbox/affected.py`).
- Produces: `async def impact_report(repo: str, wt: Worktree, base: str, max_callers: int = 8) -> str` — markdown: per changed symbol `path::qualname`, its callers (path::qualname, outside-diff callers first), and test files that reach it; `""` when nothing changed; one-line "impact unavailable: …" on any error.

- [ ] Step 1: tests on a tmp git repo: `test_lists_callers_of_changed_function` (b.py calls a.f; change a.f → report contains `a.py::f` and `b.py::g`), `test_reports_tests_reaching_change` (tests/test_a.py calls f → listed), `test_no_change_is_empty`, `test_deleted_file_only_does_not_raise`.
- [ ] Step 2: FAIL. Step 3: implement with an overlay graph (tenant `"impact-overlay"`), callers via reverse `call_edges`, depth 2 for tests. Step 4: PASS. Step 5: commit `feat(code): live impact report for a worktree diff`.

### Task 3: Fast reviewer

**Files:**
- Create: `src/agentco/agents/prompts/reviewer-fast.md`
- Modify: `src/agentco/agents/review.py`
- Test: `tests/agents/test_review.py`

**Interfaces:**
- Produces: `async def run_fast_review(asker, cfg, gate, wt, base, issue: PMReply, *, impact: str, lessons: list[str], prior: list[Finding]) -> ReviewReply` — always diffs against `base`; prompt = issue text, lessons, prior findings ("verify each is fixed"), impact, whole diff.

- [ ] Step 1: test `test_fast_review_sees_whole_diff_impact_and_prior` with `FakeRuntime`: prompt contains the base diff, the impact text, each lesson, each prior finding id; system prompt is `load_prompt(Role.REVIEWER, "fast")`.
- [ ] Step 2: FAIL. Step 3: implement; prompt copy tells the reviewer to act like CodeRabbit + Copilot: correctness, edge cases (empty/None/huge input, concurrency, partial failure, cross-platform/filesystem), error handling, resource cleanup, tests that really test the change, docs/naming; check callers in the impact report; report ALL findings in one pass with severity; no style nits as blockers. Step 4: PASS. Step 5: commit `feat(review): whole-diff fast reviewer`.

### Task 4: Fast pipeline in the orchestrator

**Files:**
- Modify: `src/agentco/config.py` (`LoopLimits.pipeline: Literal["company", "fast"] = "company"`)
- Modify: `src/agentco/workflow/states.py` (ENGINEERING → INTERNAL_REVIEW)
- Modify: `src/agentco/workflow/preflight.py` (`_rules` returns ok "skipped in fast mode" when `limits.pipeline == "fast"`)
- Create: `src/agentco/workflow/fast.py`
- Modify: `src/agentco/workflow/orchestrator.py` (`_pm`, `_triage`, `_engineering`, `_review`, `_lead` branch on `self._fast`)
- Modify: `src/agentco/agents/engineer.py` (`EngineerBrief.impact: str = ""`, rendered as an "Impact of your current diff" section)
- Test: `tests/workflow/test_orchestrator_fast.py`, `tests/workflow/test_fast.py`

**Interfaces:**
- Consumes: Task 2 `impact_report`, Task 3 `run_fast_review`.
- Produces (`workflow/fast.py`): `issue_from_request(title: str, body: str) -> PMReply` (summary=title, why=body or title, acceptance = body bullet lines `- `/`* `/`- [ ] `, else `["Resolve the issue exactly as described."]`); `fast_lead(review: ReviewReply | None, error: str | None) -> LeadDecision` (error/None → escalate "stuck"; any blocker/major → send_back with all non-nit findings; else approve with minor/nit as follow_ups).

Flow: INTAKE → PM (ISSUE artifact from request, no LLM) → TRIAGE (assign SENIOR, fast_lane 0, no LLM) → ENGINEERING (brief.impact from meta) → INTERNAL_REVIEW (impact_report → meta "impact"; run_fast_review; REVIEW artifact) → LEAD_DECISION (`fast_lead`; LEAD artifact; approve → `_open_pr` which runs the final pre-flight; send_back → TRIAGE + `_stop_rules`; escalate → `_escalate`).

- [ ] Step 1: `tests/workflow/test_fast.py`: `issue_from_request` bullets / fallback; `fast_lead` four outcomes.
- [ ] Step 2: `tests/workflow/test_orchestrator_fast.py` (reuse `orch_helpers` rig, `cfg.loop.pipeline="fast"`): `test_fast_happy_path_opens_pr_with_two_llm_calls` (scripted engineer + clean review → PR_OPEN; FakeRuntime saw only engineer + reviewer roles); `test_fast_blocker_sends_back_then_approves` (2nd engineer brief has the finding, 2nd review gets it as prior); `test_fast_round_limit_escalates`; `test_fast_reviewer_error_escalates`; `test_fast_final_preflight_failure_returns_to_engineering`.
- [ ] Step 3: FAIL. Step 4: implement. Step 5: full CI green. Step 6: commit `feat(workflow): fast pipeline`.

### Task 5: Local benchmark (no GitHub PRs)

- [ ] Point `~/agentco-bench/tool` at `feat/fast-mode`; fresh tenant `bench3` (import 42 PRs + index); `loop.pipeline: fast`.
- [ ] Run the 3 bench issues in parallel; record wall time, cost, internal rounds.
- [ ] External strict reviewer (same prompt as v1/v2) on each final diff: approve on first look = zero ping-pong. Blind judge vs plain.
- [ ] Add a fast column to `gen_report.py`; reopen the report; short summary to the user.
