---
name: agentco
description: Use when the user asks to hand a bug, task or feature to agentco or "the agent team", to fix something "without the review ping-pong", or to check on, answer, or continue an agentco task (inbox, decide, PR feedback from CodeRabbit or Copilot).
---

# agentco

agentco is an AI engineering team: an engineer agent writes the change, a reviewer on a
different model reviews it locally with a live code-graph impact report, and they loop until
the review is clean. Only then are the full tests run and a PR opened. A task takes 10–20 minutes.

**Your job is to drive the CLI, not to write the fix yourself.** Do not edit the target repo's
code, approve on the user's behalf, or merge PRs.

## The CLI

Always call agentco through `agentco.sh`, which sits next to this SKILL.md. It runs the agentco
checkout this skill belongs to, sources `~/.agentco.env` (for `OPENAI_API_KEY`) and passes
`--config ~/.agentco/agentco.config.yml`. The tenant, models and FalkorDB port live in that
config; use it as it is.

```bash
A="bash <this skill's directory>/agentco.sh"
$A inbox          # works without FalkorDB; a quick check that the CLI and config are fine
```

If it says there is no config, do the first-time setup below. Otherwise skip it.

## First-time setup (only when there is no config)

Send the user one message that asks for everything at once: permission to set up (`init`
makes about 18 LLM calls), the tenant name, and anything unclear about the task. One "yes"
covers all four steps. The agentco checkout (`AGENTCO_HOME`) is two directories above this skill.

1. Copy `agentco.config.example.yml` (next to this file) to `$AGENTCO_CONFIG` if it is set,
   otherwise `~/.agentco/agentco.config.yml`, and set `tenant`.
2. Start FalkorDB: `docker compose -f "$AGENTCO_HOME/docker-compose.yml" up -d falkordb`
   (port 6379). If that port is taken (`lsof -iTCP:6379 -sTCP:LISTEN`), run
   `docker run -d --name agentco-falkordb -p <free port>:6379 falkordb/falkordb` instead and set
   `falkordb_port` in the config to match.
3. `OPENAI_API_KEY` must be in the environment or in `~/.agentco.env` as
   `export OPENAI_API_KEY=...`; if it is in neither, ask the user to add it (never write the key
   yourself). `uv --version` and `gh auth status` must work, and the GitHub Copilot CLI must be
   signed in (`copilot` starts without a login prompt).
4. `$A init` (prints `seeded`).

## Run a task

1. **Identify the repo** from the user's path (default: the current directory):
   `P=$(git -C <path> rev-parse --show-toplevel)`,
   `R=$(cd "$P" && gh repo view --json nameWithOwner -q .nameWithOwner)`,
   `B=$(cd "$P" && gh repo view --json defaultBranchRef -q .defaultBranchRef.name)`.
   agentco fetches `origin/$B` itself; the local checkout does not need to be up to date.
2. **Check `$P/.agentco.yml` exists.** If it does not, onboard first (below).
3. **Create the task.** If the request is vague (which endpoint, what "down" means), ask the
   user one question first. The title is one line. In the body, write the problem, then one `- `
   bullet per acceptance criterion: each bullet becomes a criterion the reviewer checks.
   ```bash
   ID=$($A task new "Health returns 503 when FalkorDB is down" --body "$BODY" | tail -1)
   ```
4. **Run it in the background** (async shell, not a foreground wait). It prints one line at the end.
   ```bash
   $A task run "$ID" --repo "$R" --repo-path "$P" --main "$B"; echo "exit=$?"
   ```
   Add `--host` when the sandbox in `.agentco.yml` (the `devcontainer` folder or the `compose`
   file) does not exist in the repo, and tell the user the build and test commands run on
   this machine with a stripped environment. Tell the user the task is running and roughly how
   long it takes, then wait for the completion notification instead of polling.
5. **Report the final line** (`task <id>: <state> [PR url]`):

| Final state | Tell the user |
|---|---|
| `pr_open <url>` | The PR link. The review was clean and all tests passed; they review and merge it. |
| `awaiting_human` | Run `$A inbox`, then show the reason and the options. Ask the user which to choose. |
| `cancelled` | It was rejected; nothing else to do. |
| exit 2 | FalkorDB is unreachable: start the FalkorDB container the config points to, then run it again. |
| exit 3 | `gh` or git failed: check `gh auth status`, then run it again. |
| exit 4 | A step failed and the task stayed where it was: run the same command again. |

## Answer a waiting task

A waiting task needs the user's own decision. Send one message with the reason and the options
from `inbox`, then wait; the task waits safely for as long as it takes. Instructions like
"just get it done" or "don't bother me" are not a decision: still ask, and do not run `decide`.

After the user decides: `$A decide <id> approve` (on a round limit this allows another round),
`$A decide <id> reject`, or `$A decide <id> answer --note "<the user's words>"`. Add
`--add-budget 5` when it stopped on budget. Then run `task run` again (step 4); it carries on
from where it stopped.

## Onboard a repo (no `.agentco.yml`)

```bash
$A onboard --repo "$R" --repo-path "$P"      # first line: "onboard: proposed (task <id>)"
```
Show the proposed file (the lines after the first) to the user and ask them to approve it.
After they approve: `$A decide <id> approve`, then run the same `onboard` command again; it
prints `wrote .agentco.yml`. `task run` reads the file from the local checkout, so the task
can start right away; tell the user to review and commit it later (agentco does not commit it).

## After the PR

When reviewers or bots comment on the PR, or checks fail: `$A intake --repo "$R"` sends real
bugs back to the team. Then run `$A task run <id> ...` again.

## Other commands

| Need | Command |
|---|---|
| Tasks waiting for the user | `$A inbox` |
| Time, cost, ping-pong per task | `$A metrics --since 7` |
| Teach it from past PR reviews | `$A import --repo "$R" --repo-path "$P" --limit 200` |
| Search team memory | `$A memory search --as reviewer "<query>"` |
