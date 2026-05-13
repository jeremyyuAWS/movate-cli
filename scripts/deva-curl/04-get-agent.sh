#!/usr/bin/env bash
# Fetch the full profile for one agent. Drives the Mova iO
# agent-profile page.
#
# Usage:
#   ./04-get-agent.sh                  # default: hello-bot
#   ./04-get-agent.sh my-agent-name

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"

echo "→ GET ${MDK_BASE}/api/v1/agents/${NAME}"
mdk_curl_json GET "/api/v1/agents/${NAME}" | python3 -m json.tool
