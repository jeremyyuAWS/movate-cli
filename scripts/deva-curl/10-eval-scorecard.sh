#!/usr/bin/env bash
# Retrieve a completed eval's scorecard. Pass the eval_id returned
# by ./09-run-eval.sh.
#
# Usage:
#   ./10-eval-scorecard.sh <eval_id>

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly EVAL_ID="${1:?missing eval_id — pass the value returned by 09-run-eval.sh}"

echo "→ GET ${MDK_BASE}/api/v1/evals/${EVAL_ID}"
mdk_curl_json GET "/api/v1/evals/${EVAL_ID}" | python3 -m json.tool
