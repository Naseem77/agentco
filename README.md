# agentco

A software company made of AI agents: PM, triage, junior/senior engineers, architect, blind reviewer, isolated QA, team lead and librarian — sharing memory in FalkorDB via Graphiti. Work runs locally; the pull request is opened last, only after the team lead approves.

- Design: [docs/superpowers/specs/2026-11-04-agent-company-design.md](docs/superpowers/specs/2026-11-04-agent-company-design.md)
- Plan 1 (foundation): [docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md](docs/superpowers/plans/2026-11-04-agentco-plan-1-foundation.md)

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

## Tests

```bash
uv run pytest -q                  # unit tests; no Docker or network needed
uv run pytest -m integration      # needs FalkorDB on localhost:6379 and OPENAI_API_KEY
uv run ruff check src tests
```
