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

Graphiti's OpenAI models are set with `memory_models` (defaults shown; `small_model` is
also the reranker). `AGENTCO_GRAPHITI_MODEL` and `AGENTCO_GRAPHITI_SMALL_MODEL` override
`model` and `small_model` when set.

```yaml
memory_models: { model: gpt-5.5, small_model: gpt-4.1-mini, embedder: text-embedding-3-small }
```

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

## Metrics

```bash
uv run agentco metrics                 # totals, then one row per task
uv run agentco metrics --since 30      # only tasks started in the last 30 days
uv run agentco metrics --json          # the same data as JSON
```

Everything is derived on read from the task store (audit log, artifacts, meta). There is no
extra table, and the command needs neither FalkorDB nor GitHub. It exits 1 only on a bad
config or bad `--since`.

Each number maps to a success metric of spec §9:

| Number | Spec §9 metric | Target |
|---|---|---|
| `pr_comments_per_pr` | human review rounds per PR after it opens (the real ping-pong metric): findings from comments, reviews and failing checks, per PR | **0** |
| `escaped_real_bugs` | escaped bugs: PR findings the lead classified `real_bug` | ↓ |
| `fix_round_findings` | % of bugs introduced by fixes: review findings in rounds after the first (a proxy) | ↓ |
| `avg_rounds`, `avg_pr_rounds` | internal rounds per task; the share of them that came from a PR | ↓ |
| `escalations`, `recurrences` | human pings (escalations to the inbox); findings that came back after a fix | ↓ |
| `pct_junior` | % of tasks done by the junior tier (merged tasks whose final assignee is junior) | ↑ |
| `premium_requests`, `cost_usd`, `median_duration_h` | cost per task; time from creation to merge or cancel | ↓ |

`tasks`, `merged` and `cancelled` count the tasks in the window. `stale rules` lists the
fingerprints of review findings that humans overrode at least `thresholds.stale_after`
times (default 3); retire those rules by hand.

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
env_passthrough: []             # optional: host variables the commands may see
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
your `approve` on a lead escalation or a failed push) is the only way a branch is pushed and a
PR opened, and nothing here ever merges one. An `approve` on a budget or round stop, or after
the lead failed, just lets the team continue; the lead still has to approve.

`inbox` lists each waiting task with the category, the state it came from, why it stopped and
the options. `decide <id> approve|reject|answer` answers it: `answer` needs `--note`,
`reject` cancels the task at once, and `approve` or `answer` take effect on the next
`task run`. `--add-budget USD` raises that task's spending limit, which is how a task that
stopped on its budget continues. Every decision is also written to company memory as a human
decision.

**Security: commands the agents write run on your machine** (`--host`) with a reduced environment:
only `PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `USER`, `LOGNAME` and the variables listed in
`env_passthrough` in `.agentco.yml` are passed on, so API keys and tokens (`OPENAI_API_KEY`,
`GH_TOKEN`, `AWS_*`, ...) are not visible to them. This does not isolate the filesystem or the
network, and the agent's own shell tool is limited only by its permission policy. Use the container
sandbox, and do not feed untrusted text (such as issues from strangers) to the team without it.

Exit codes of `task run`: 1 bad arguments, config, checkout or repo contract, 2 FalkorDB
unreachable, 3 `gh` unavailable or failing or a git operation failed, 4 a step failed and the task stayed where it was
(run it again to retry). `decide` uses 1 and 2.

## Onboard a repo

A repo without `.agentco.yml` can get one proposed by the Architect:

```bash
uv run agentco onboard --repo owner/name --repo-path .   # reads the repo, prints a proposed .agentco.yml
uv run agentco inbox                                     # the proposal waits for you
uv run agentco decide 7 approve                          # or: answer --note "we use tox" | reject
uv run agentco onboard --repo owner/name --repo-path .   # writes the file after your approve
```

Nothing is written until you `approve`. The file is created in the local checkout only if
none exists (an existing `.agentco.yml` is never overwritten), and **agentco never commits or
pushes it**: review the file and commit it yourself. The proposal must be a valid contract
whose commands are each one plain command (no `&&`, `;`, `|`, `$`, redirects) and whose
`env_passthrough` is empty; the Architect gets one retry before the proposal escalates as
`stuck`. `answer` asks for a new draft with your note. Onboarding tasks never run with
`task run`.

## PR feedback

After a PR is open, `agentco intake` reads what people said about it and sends real problems
back to the team:

```bash
agentco intake --repo acme/app
```

It looks at review comments and reviews from your team (owners, members, collaborators), from
review bots you allow-listed in the config, and at failing checks. The lead classifies each
finding: a real bug goes back to triage and the same PR is updated (the branch is pushed with
`--force-with-lease`, the PR body is edited); nits are not worth a round and a tradeoff is
your call. When only nits or tradeoffs are left, you decide: the task stops in
`awaiting_human` and `agentco decide` continues it. A real bug that keeps coming back is bounded by the same round limit as any
task, after which it also stops for you.

Intake never comments on a PR and never merges. It only reads, and a PR you merged or closed
yourself marks its task `merged` or `cancelled`. Running it twice does the same as running it
once. Run it on a timer, then let the team pick the work up:

```cron
*/15 * * * * cd /path/to/repo && agentco intake --repo acme/app && agentco task run --all --repo acme/app
```

Intake prints one line per task (`#3 back to triage (round 1): 2 finding(s)`, `#4 needs a
human: stuck`, `#5 merged`) and a summary (`7 known, 1 deferred`). Exit codes: 1 bad
arguments or config, 2 FalkorDB unreachable, 3 `gh` unavailable or failing, 4 an agent step
failed (a rerun is safe), 0 otherwise.

## What agentco learns

After `task run`, `decide` and `intake`, the Librarian turns what happened into company memory.
It reads only what the task stored, never the chat history:

- the lead's review comments become review rules (`rnd`);
- QA failures become issues (`rnd`); only title, steps, expected and actual, never the tests;
- a real bug found on a PR (not recurring) becomes a risk (`rnd`), an escaped bug and a
  neighbour case to test next time (`qa`);
- your `agentco decide` answers become escalation rules and policies (`company`), one or two at
  most per decision; approving over non-real findings over and over marks a rule stale;
- a merged task adds its invariant, what was solved and how its agents did, plus the human
  review comments on its PR; a cancelled one adds the agent profile only;
- after a merge, QA's regression tests become a follow-up task (`qa-followup`) that adds them
  to the repo through the normal PR path.

Nothing goes to `product`. Every lesson is a plain fact with its origin (`lead`, `human` or
`librarian`) and a provenance line, for example `Source: task:3:lead:12` (task, artifact kind,
artifact id), so you can trace it back. Lessons are deduplicated against what memory already
holds. Warnings (`warning: librarian #3: ...`) never change an exit code.

Everything is idempotent. To rerun the Librarian, for instance after FalkorDB was down or the
model failed:

```bash
agentco learn 3        # one task: prints "#3: 2 added, 1 duplicate"
agentco learn --all    # every task with something not yet learned
```

## Tests

```bash
uv run pytest -q                  # unit tests; no Docker or network needed
uv run pytest -m integration      # needs FalkorDB on localhost:6379 and OPENAI_API_KEY
AGENTCO_IT_COPILOT=1 uv run pytest tests/test_task_loop_integration.py -m integration  # real Copilot agents
uv run ruff check src tests
```

## Webhooks and one intake per repo (`agentco serve`)

<!-- Plan 5, PR 4: keep this section self-contained so it merges cleanly. -->

Polling with `agentco intake` (see [PR feedback](#pr-feedback)) still works and always will.
`agentco serve` adds a push path for one repo: GitHub tells agentco when something happens,
and agentco runs the same intake right away instead of waiting for the next cron tick.

```bash
export AGENTCO_WEBHOOK_SECRET=...            # any long random string
agentco serve --repo acme/app                # 127.0.0.1:8787; --host / --port to change
```

In the repo's **Settings → Webhooks**, add a webhook with:

- **Payload URL:** `https://<your tunnel or proxy>/webhook`
- **Content type:** `application/json`
- **Secret:** the same value as `AGENTCO_WEBHOOK_SECRET`
- **Events:** `issues`, `pull_request`, `pull_request_review`,
  `pull_request_review_comment` and `check_run` (a `ping` is answered too; any other event
  is rejected)

What the receiver does, and what it refuses:

- The secret comes from the environment variable named by `webhook.secret_env` (default
  `AGENTCO_WEBHOOK_SECRET`), never from the config file. `serve` exits 1 if it is not set.
- Every request is checked with the `X-Hub-Signature-256` HMAC before anything is parsed.
  A missing or wrong signature gets 401; an unknown event, a payload for another repo or a
  malformed body gets 400; a missing `Content-Length` 411; a body over
  `webhook.max_body_bytes` (default 1 MiB) 413, without being read.
- A delivery id is handled once: GitHub redelivering it is answered `200 duplicate` and does
  not start an intake. A burst of different deliveries is coalesced into one running intake
  and at most one more after it (`webhook.debounce_s`, default 2 seconds, waits for the burst
  to settle).
- A webhook never creates a task and never carries data into the team: it only asks for an
  intake, which reads GitHub itself.

```yaml
webhook:
  secret_env: AGENTCO_WEBHOOK_SECRET   # name of the environment variable
  max_body_bytes: 1048576
  debounce_s: 2
```

`serve` speaks plain HTTP and binds to `127.0.0.1` by default. Reach it from GitHub only
through a tunnel or a reverse proxy that terminates TLS; do not expose the port directly.

**Only one intake per repo runs at a time**, across processes: `agentco intake`, cron and
`agentco serve` share a lease in the state database (`intake:<tenant>:<repo>`), released when
a run ends; if a process is killed mid-run, its lease expires after one hour. If one is already running, the other skips:
`agentco intake` prints `another intake for acme/app is running; skipped` and exits 0, and the
skipped run does not move the intake cursor, so the next run sees everything.
