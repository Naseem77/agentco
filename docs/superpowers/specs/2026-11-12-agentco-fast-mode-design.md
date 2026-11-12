# agentco fast mode — design

## Intent

When the user hands agentco an issue or feature, the PR it opens should pass
CodeRabbit / Copilot review the first time, with no review → fix → new-comment
ping-pong. Speed matters as much: a task should take about what a single agent
plus one review takes (~10–20 min), not the 40–60 min of the company pipeline.

## Evidence (Plan 6 benchmark, 3 issues)

- Writing code was 12% of agentco v2 time; pre-flight 46%, review+QA 30%, lead 9%.
- Plain agent rounds 3/1/3, agentco v2 3/2/3: the company pipeline did not cut ping-pong.
- An outside reviewer found 1–3 blocking bugs in every v2 diff that agentco's own
  reviewer, QA and lead approved. All were design / edge-case bugs (cross-filesystem
  temp files, half-done work on cancel, missing error handling), not test failures.
- The importer drops every bot review comment (`importer/episodes.py`), so CodeRabbit /
  Copilot comments — the actual ping-pong — never reach memory.

## Goals

1. All review ping-pong happens locally before any PR exists.
2. One strong engineer with full context; one strong reviewer that reviews like
   CodeRabbit / Copilot, reports every issue at once, and reviews the whole diff each time.
3. Each round, a live impact check from the code graph tells engineer and reviewer which
   callers and tests the change (and each finding) touches.
4. Past bot review comments are imported into memory and given to the engineer before coding.
5. Deterministic checks (lint, build, full tests) run once, before the PR.

## Non-goals

- Removing the company pipeline (it stays the default, `pipeline: company`).
- Running the real CodeRabbit CLI (needs a login and takes 7–30 min/review); possible later.
- Opening GitHub PRs while testing.

## Anti-goals

- Opening a PR that still has blocking findings (that recreates the ping-pong).
- A fast mode slower than ~2× the plain agent.

## Approaches considered

- **A. Fast pipeline inside agentco (chosen by the user).** Reuses worktrees, memory, code
  graph, importer; drops PM/triage/QA/lead LLM calls and per-round pre-flight.
- B. Keep the company pipeline, add bot comments + impact: least code, keeps 88% overhead.
- C. Plain agent + external review wrapper: fast to build, no shared memory/graph.

## Decided

- `loop.pipeline: fast` switches a tenant to fast mode; PM and triage become
  deterministic (issue text becomes the brief; the senior engineer is assigned).
- Mid-round pre-flight is skipped; QA is skipped; the lead is a rule: blocker/major findings
  send work back (all findings in one batch) up to `loop.fast_review_rounds` (default 2)
  times, then escalate to a human instead of opening a PR. Clean review → final pre-flight
  (lint, build, full tests, no LLM rules check) → PR.
- Impact check (my call): a live in-memory overlay of the worktree's code graph, built the
  same way `affected_tests` already does, so unmerged edits never pollute the shared graph.
- Bot comments from logins in `intake.review_bots` (Copilot + `coderabbitai[bot]`) are
  imported as review episodes.

## Left to the builder

Exact wording beyond the prompt copy in the plan; how artifacts are written, following
existing orchestrator patterns.
