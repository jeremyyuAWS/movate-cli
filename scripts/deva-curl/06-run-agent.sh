#!/usr/bin/env bash
# Submit an agent run.
#
# Two modes — pick based on what you need:
#
#   Default (async): queues a job, returns job_id. Poll with
#   ./07-job-status.sh <job_id> until status flips to success/error.
#   Use this for production-shape traffic where the client can wait
#   asynchronously.
#
#   wait=true (inline): runs the agent inline at the API endpoint,
#   returns the full RunView (run_id, output, metrics, status) in
#   one HTTP response. Required for wizard-created agents (the
#   worker pod doesn't have those bundles yet; see BACKLOG item
#   109). Trade-off: HTTP connection stays open for the duration
#   of the run.
#
# Usage:
#   ./06-run-agent.sh                                      # hello-bot, async
#   ./06-run-agent.sh my-agent '{"input": "foo"}'          # custom, async
#   ./06-run-agent.sh my-agent '{"input": "foo"}' wait     # inline + real LLM
#   ./06-run-agent.sh my-agent '{"input": "foo"}' wait mock # inline + mock provider

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"
# Default input on next line — can't use ${2:-{...}} because bash's
# parameter expansion treats unescaped `}` as ending the substitution.
if [[ -z "${2:-}" ]]; then
    readonly _INPUT_JSON='{"input": "hello world"}'
else
    readonly _INPUT_JSON="$2"
fi
readonly MODE="${3:-async}"  # "async" | "wait"
readonly PROVIDER="${4:-real}"  # "real" | "mock" (only meaningful with wait)

# Build the body using jq if available (handles arbitrary quoting
# correctly), or a small Python fallback. Both produce the same
# JSON; jq's shell-quoting story is just more robust for inputs
# with embedded spaces or quotes.
mock_bool=$([[ "${PROVIDER}" == "mock" ]] && echo "true" || echo "false")
if command -v jq >/dev/null 2>&1; then
    body=$(jq -nc --argjson input "${_INPUT_JSON}" --argjson mock "${mock_bool}" \
        '{input: $input, mock: $mock}')
else
    body=$(MDK_BODY_INPUT="${_INPUT_JSON}" python3 -c '
import json, os
print(json.dumps({
    "input": json.loads(os.environ["MDK_BODY_INPUT"]),
    "mock": '"${mock_bool}"' == "true"
}))
')
fi

query=""
[[ "${MODE}" == "wait" ]] && query="?wait=true"

echo "→ POST ${MDK_BASE}/api/v1/agents/${NAME}/runs${query}"
echo "  input: ${_INPUT_JSON}"
echo "  mode:  ${MODE}${query:+ (inline)}"
[[ "${MODE}" == "wait" ]] && echo "  provider: ${PROVIDER}"
echo
mdk_curl_json POST "/api/v1/agents/${NAME}/runs${query}" "${body}" | python3 -m json.tool
