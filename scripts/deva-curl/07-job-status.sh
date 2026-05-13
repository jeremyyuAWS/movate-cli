#!/usr/bin/env bash
# Poll a job. ./06-run-agent.sh returned the job_id; pass it here.
#
# Usage:
#   ./07-job-status.sh <job_id>

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly JOB_ID="${1:?missing job_id — pass the value returned by 06-run-agent.sh}"

echo "→ GET ${MDK_BASE}/jobs/${JOB_ID}"
mdk_curl_json GET "/jobs/${JOB_ID}" | python3 -m json.tool
