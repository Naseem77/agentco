# agentco

A software company made of AI agents: PM, triage, junior/senior engineers, architect, blind reviewer, isolated QA, team lead and librarian — sharing memory in FalkorDB via Graphiti. Work runs locally; the pull request is opened last, only after the team lead approves.

- Design: [docs/superpowers/specs/2026-11-04-agent-company-design.md](docs/superpowers/specs/2026-11-04-agent-company-design.md)
- Plan 1 (foundation): [docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md](docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md)
- Plan 2 (importer): [docs/superpowers/plans/2026-11-04-agentco-plan-2-importer.md](docs/superpowers/plans/2026-11-04-agentco-plan-2-importer.md)
- Plan 3 (task loop): [docs/superpowers/plans/2026-11-07-agentco-plan-3-task-loop.md](docs/superpowers/plans/2026-11-07-agentco-plan-3-task-loop.md)

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
docker compose up -d falkordb      # FalkorDB on localhost:6379
export OPENAI_API_KEY=...          # Graphiti uses it for extraction and embeddings
```

Create `agentco.config.yml` (the default for `--config`); relative sqlite paths in
`database_url` are resolved against this file's directory:

```yaml
tenant: acme
roles:
  pm:        { model: fast-cheap,     family: anthropic }
  triage:    { model: fast-cheap,     family: anthropic }
  architect: { model: strongest,      family: anthropic }
  senior:    { model: claude-strong,  family: anthropic }
  junior:    { model: claude-cheaper, family: anthropic }
  reviewer:  { model: gpt-reviewer,   family: openai }
  qa:        { model: gpt-family,     family: openai }
  lead:      { model: gemini-family,  family: google }
  librarian: { model: fast-cheap,     family: anthropic }
```

All roles are required, unknown keys are rejected, and reviewer/qa/lead must use a
different model than senior/junior. See the
[design spec §10](docs/superpowers/specs/2026-11-04-agent-company-design.md) for every option.

```bash
uv run agentco init                                   # one-time
uv run agentco memory search --as qa "neighbours"
uv run agentco task new "Graph switch flickers"
```

`agentco init` creates the local state DB and the four graphs, then seeds company memory
with the seed policies and default escalation rules. Seeding makes about 18 LLM
extraction calls and is one-time; an interrupted run resumes where it stopped.
`memory search --as <role>` only searches the graphs that role may read.

## Importing a repo

Prerequisites: the `init` setup above (FalkorDB running, `OPENAI_API_KEY` set for Graphiti),
`git`, and the [GitHub CLI](https://cli.github.com) **2.48 or newer** (needed for
`gh api --slurp`), authenticated with `gh auth login`. A `--dry-run` needs neither FalkorDB
nor `OPENAI_API_KEY`, only `gh`.

```bash
uv run agentco index --repo owner/name --repo-path .                 # code structure → code graph
uv run agentco import --repo owner/name --repo-path . --dry-run      # counts only, writes nothing
uv run agentco import --repo owner/name --repo-path .                # the real import
```

`index` stores the checkout's functions, classes and call edges (git-tracked files only) in
the tenant's `rnd` graph and is incremental: unchanged files are not re-parsed. Its counts
`skipped` (working-tree files that were not indexed) and `deleted` (graph entries removed)
can overlap, so a file may appear in both. `--repo-path` may be any directory inside the
checkout; it is resolved to the repository root, and a directory outside a git repository
is rejected (exit 1).

`import` reads issues, pull requests and review comments through `gh`, oldest first, and
writes each one as an episode: issues and PRs to `rnd`, feature requests also to `product`.
**Each episode is one Graphiti extraction (several LLM calls)**, so run `--dry-run` first
and bound the work with `--limit N` (at least 1) or `--since YYYY-MM-DD`.

`--dry-run` imports nothing: it contacts only GitHub, creates no graphs, does not index and
builds neither the memory backend nor the code graph. It reads the "already imported"
markers from the local state database (`database_url`), which it creates if missing. It
reports the episodes that would be written (each one LLM extraction) and, separately, any
link-only retries (episodes already written whose code link is still pending; no LLM call).

Re-running is safe: imported items are remembered, and an interrupted or `--limit`ed run
resumes with the next-oldest items. Review threads started by a bot, by the PR's own author
or by a non-member are not imported, and neither are issues or PRs authored by bots.
`--since` only filters by creation date; it never sets the resume cursor, so a run with
`--since` does not make later plain runs skip anything.

### Linking review comments to code

With `--repo-path`, `import` **indexes the checkout first and only then contacts GitHub**.
If `gh` fails (exit 3), the code graph has already been updated; that is harmless, since
indexing is incremental.

Each review comment is linked to the function or class it was written against, using the
file as it was at the comment's own commit:

- **Commit-accurate**: when the commit is in the checkout, the link is exact even if the
  code has moved since. Review commits of PRs that were merged or force-pushed are often
  missing from a clone; fetch the PR heads first:
  `git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'`
- **Best effort**: when the commit is missing, a comment on a line that still exists is
  resolved against the *current* code, which can point at a different function if the file
  changed. The run then prints `N episodes linked against current code because their
  commits are not in the checkout`. A comment whose line no longer exists (outdated) and
  whose commit is missing is not linked at all rather than guessed.

Reviews imported while no code graph was available (no `--repo-path` and an empty index)
stay pending: the next `import --repo-path ...` (or one after `agentco index`) links them
without writing the episodes again, and prints `relinked N episodes` (only when N > 0). The
resume cursor is held back while such reviews are pending, and the run reports
`N review episodes await a code graph`.

Exit codes: 1 bad arguments, config, a non-git `--repo-path` or an LLM error (e.g.
`OPENAI_API_KEY` missing, rate limit, refusal), 2 FalkorDB unreachable or rejecting a
query, 3 `gh` missing, not logged in, the repo not found, or `gh` returning unexpected JSON.

## Run a task

Prerequisites: `init` done, FalkorDB and `OPENAI_API_KEY` as above, the
GitHub Copilot CLI signed in (the agents run through the Copilot SDK), `git` and `gh`. The
target repo needs a `.agentco.yml` at its root that tells the team how to build and test it:

```yaml
build: make build
test: make test
e2e: null                       # or a command; the key is required
lint: make lint
sandbox: { devcontainer: .devcontainer }   # or { compose: docker-compose.yml, service: app }
test_files: pytest {files}      # optional: run only the tests a change affects
```

```bash
uv run agentco task new "Graph switch flickers" --body "Switching graphs flickers for a frame..."
uv run agentco task run 1 --repo owner/name --repo-path .   # or --all
uv run agentco inbox                                        # tasks waiting for you
uv run agentco decide 1 approve                             # or: reject | answer --note "..."
uv run agentco task run 1 --repo owner/name --repo-path .   # carries on from your decision
```

`task run` works on a branch in its own git worktree and prints the final state per task. It
stops when the task needs you (`awaiting_human`), opens its pull request (`pr_open`) or is
cancelled, and it resumes where it stopped when run again. `--all` runs every task that can
make progress, `cfg.concurrency` at a time; `--host` runs the repo's build and test commands
on this machine instead of in the sandbox.

**A PR opens only after approval, and a human always merges.** The team lead's approval (or
your `approve` on an escalated task) is the only way a branch is pushed and a PR opened, and
nothing here ever merges one.

`inbox` lists each waiting task with the category, the state it came from, why it stopped and
the options. `decide <id> approve|reject|answer` answers it: `answer` needs `--note`,
`reject` cancels the task at once, and `approve` or `answer` take effect on the next
`task run`. `--add-budget USD` raises that task's spending limit, which is how a task that
stopped on its budget continues. Every decision is also written to company memory as a human
decision.

Exit codes of `task run`: 1 bad arguments, config, checkout or repo contract, 2 FalkorDB
unreachable, 3 `gh` unavailable or failing, 4 a step failed and the task stayed where it was
(run it again to retry). `decide` uses 1 and 2.

## Tests

```bash
uv run pytest -q                  # unit tests; no Docker or network needed
uv run pytest -m integration      # needs FalkorDB on localhost:6379 and OPENAI_API_KEY
AGENTCO_IT_COPILOT=1 uv run pytest tests/test_task_loop_integration.py -m integration  # real Copilot agents
uv run ruff check src tests
```
