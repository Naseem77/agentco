#!/usr/bin/env bash
# Runs the agentco CLI from this checkout with the user's config and secrets.
#   AGENTCO_HOME    agentco checkout (default: the repo this script lives in)
#   AGENTCO_CONFIG  config file      (default: ~/.agentco/agentco.config.yml)
#   AGENTCO_ENV     env file sourced first, e.g. OPENAI_API_KEY (default: ~/.agentco.env)
set -euo pipefail
here="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTCO_HOME="${AGENTCO_HOME:-$(cd "$here/../.." && pwd)}"
AGENTCO_CONFIG="${AGENTCO_CONFIG:-$HOME/.agentco/agentco.config.yml}"
AGENTCO_ENV="${AGENTCO_ENV:-$HOME/.agentco.env}"
if [ ! -f "$AGENTCO_CONFIG" ]; then
  echo "agentco: no config at $AGENTCO_CONFIG (see 'First-time setup' in SKILL.md)" >&2
  exit 1
fi
# shellcheck disable=SC1090
[ -f "$AGENTCO_ENV" ] && source "$AGENTCO_ENV"
exec uv run -q --project "$AGENTCO_HOME" agentco "$@" --config "$AGENTCO_CONFIG"
