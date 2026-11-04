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
`gh api --slurp`), authenticated with `gh auth login`.

```bash
uv run agentco index --repo owner/name --repo-path .                 # code structure → code graph
uv run agentco import --repo owner/name --repo-path . --dry-run      # counts only, writes nothing
uv run agentco import --repo owner/name --repo-path .                # the real import
```

`index` stores the checkout's functions, classes and call edges (git-tracked files only) in
the tenant's `rnd` graph and is incremental: unchanged files are not re-parsed. Its counts
`skipped` (working-tree files that were not indexed) and `deleted` (graph entries removed)
can overlap, so a file may appear in both.

`import` reads issues, pull requests and review comments through `gh`, oldest first, and
writes each one as an episode: issues and PRs to `rnd`, feature requests also to `product`.
With `--repo-path` it runs `index` first and links review comments to the function they were
written against. **Each episode is one Graphiti extraction (several LLM calls)**, so run
`--dry-run` first and bound the work with `--limit N` (at least 1) or `--since YYYY-MM-DD`.
Re-running is safe: imported items are remembered, and an interrupted or `--limit`ed run
resumes with the next-oldest items. Review threads started by a bot, by the PR's own author or by a
non-member are not imported.

Exit codes: 1 bad arguments, config or LLM client (e.g. `OPENAI_API_KEY` missing), 2 FalkorDB
unreachable, 3 `gh` missing, not logged in, or the repo not found.

## Tests

```bash
uv run pytest -q                  # unit tests; no Docker or network needed
uv run pytest -m integration      # needs FalkorDB on localhost:6379 and OPENAI_API_KEY
uv run ruff check src tests
```
