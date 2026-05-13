#!/usr/bin/env bash
# Kick off an eval — mock provider for speed. Returns eval_id;
# retrieve scorecard with ./10-eval-scorecard.sh.
#
# Note: the eval endpoint NEEDS the agent to have a dataset file.
# Agents you created via 02-create-agent.sh don't have one. Either
# upload one separately, or wait for the next-sprint endpoint that
# accepts inline datasets.
#
# Usage:
#   ./09-run-eval.sh                       # default: hello-bot, mock=true
#   ./09-run-eval.sh my-agent              # custom agent
#   ./09-run-eval.sh my-agent 0.7 3 false  # agent + gate + runs + use-real-model

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"
readonly GATE="${2:-0.0}"
readonly RUNS="${3:-1}"
readonly MOCK="${4:-true}"

body=$(python3 -c "
import json
print(json.dumps({
    'gate': ${GATE},
    'gate_mode': 'mean',
    'runs': ${RUNS},
    'mock': ${MOCK}
}))
")

echo "→ POST ${MDK_BASE}/api/v1/agents/${NAME}/evals"
echo "  gate=${GATE} runs=${RUNS} mock=${MOCK}"
echo
mdk_curl_json POST "/api/v1/agents/${NAME}/evals" "${body}" | python3 -m json.tool
