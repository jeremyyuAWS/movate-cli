#!/usr/bin/env bash
# List recent jobs, filterable by agent + status. Drives the
# Mova iO agent-profile page's "recent runs" tab.
#
# Usage:
#   ./08-list-jobs.sh                              # all jobs
#   ./08-list-jobs.sh hello-bot                    # one agent
#   ./08-list-jobs.sh hello-bot success            # one agent + status

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly AGENT="${1:-}"
readonly STATUS="${2:-}"

query=""
if [[ -n "${AGENT}" ]]; then
    query="?agent=${AGENT}"
fi
if [[ -n "${STATUS}" ]]; then
    query="${query:+${query}&}status=${STATUS}"
    [[ -z "${query%%\?*}" ]] && query="?${query}"
fi

echo "→ GET ${MDK_BASE}/api/v1/jobs${query}"
mdk_curl_json GET "/api/v1/jobs${query}" | python3 -m json.tool
