#!/usr/bin/env bash
# Run the shippability gate on an agent — prompt linter +
# eval cost forecast. Use this before letting the user click
# "Publish" in your wizard.
#
# Response:
#   { passed: bool, errors: [], warnings: [], cost_forecast: {...} }
#
# Usage:
#   ./05-validate-agent.sh                  # default: hello-bot
#   ./05-validate-agent.sh my-agent-name

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"

echo "→ POST ${MDK_BASE}/api/v1/agents/${NAME}/validate"
mdk_curl_json POST "/api/v1/agents/${NAME}/validate" "" | python3 -m json.tool
