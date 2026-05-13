#!/usr/bin/env bash
# Eval history — drives the agent-profile "evals over time" chart.
#
# Usage:
#   ./11-eval-history.sh              # all evals across all agents
#   ./11-eval-history.sh hello-bot    # one agent's history

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly AGENT="${1:-}"
query=""
[[ -n "${AGENT}" ]] && query="?agent=${AGENT}"

echo "→ GET ${MDK_BASE}/api/v1/evals${query}"
mdk_curl_json GET "/api/v1/evals${query}" | python3 -m json.tool
