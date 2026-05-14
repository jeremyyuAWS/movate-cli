#!/usr/bin/env bash
# Soft-delete an agent. Bundle moves to a sibling
# ``.deleted-<name>-<timestamp>/`` on the runtime's filesystem so
# the operator can recover within a 7-day window if it was a
# mistake.
#
# After this returns 200, GET /agents stops listing the agent and
# POST /agents/<name>/runs etc. all 404.
#
# Usage:
#   ./13-delete-agent.sh <name>

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:?missing agent name}"

echo "→ DELETE ${MDK_BASE}/api/v1/agents/${NAME}"
mdk_curl_json DELETE "/api/v1/agents/${NAME}" | python3 -m json.tool
