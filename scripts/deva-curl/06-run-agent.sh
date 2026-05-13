#!/usr/bin/env bash
# Submit an agent run — queues a job, returns job_id immediately.
# Poll with ./07-job-status.sh <job_id>.
#
# Usage:
#   ./06-run-agent.sh                              # hello-bot, input='hello world'
#   ./06-run-agent.sh my-agent '{"input": "foo"}'  # custom agent + input

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"
readonly INPUT_JSON="${2:-{\"input\": \"hello world\"}}"

body=$(python3 -c "
import json, sys
print(json.dumps({'input': json.loads('''${INPUT_JSON}''')}))
")

echo "→ POST ${MDK_BASE}/api/v1/agents/${NAME}/runs"
echo "  input: ${INPUT_JSON}"
echo
mdk_curl_json POST "/api/v1/agents/${NAME}/runs" "${body}" | python3 -m json.tool
