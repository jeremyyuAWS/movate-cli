#!/usr/bin/env bash
# List all agents the runtime knows about.
#
#   ./03-list-agents.sh

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

echo "→ GET ${MDK_BASE}/agents"
mdk_curl_json GET "/agents" | python3 -m json.tool
