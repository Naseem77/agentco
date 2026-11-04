# agentco

A software company made of AI agents: PM, triage, junior/senior engineers, architect, blind reviewer, isolated QA, team lead and librarian — sharing memory in FalkorDB via Graphiti. Work runs locally; the pull request is opened last, only after the team lead approves.

- Design: [docs/superpowers/specs/2026-11-04-agent-company-design.md](docs/superpowers/specs/2026-11-04-agent-company-design.md)
- Plan 1 (foundation): [docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md](docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md)
- Plan 2 (importer): [docs/superpowers/plans/2026-11-04-agentco-plan-2-importer.md](docs/superpowers/plans/2026-11-04-agentco-plan-2-importer.md)

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

## Tests

```bash
uv run pytest -q                  # unit tests; no Docker or network needed
uv run pytest -m integration      # needs FalkorDB on localhost:6379 and OPENAI_API_KEY
uv run ruff check src tests
```
