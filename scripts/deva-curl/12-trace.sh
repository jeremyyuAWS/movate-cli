#!/usr/bin/env bash
# Trace replay — full timeline of a run (single agent or workflow
# with children). Drives the Mova iO trace-viewer component.
#
# Usage:
#   ./12-trace.sh <run_id>
#
# Get a run_id from ./07-job-status.sh — `result_run_id` field on
# a successful job.

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly RUN_ID="${1:?missing run_id — get it from a successful job's result_run_id}"

echo "→ GET ${MDK_BASE}/api/v1/runs/${RUN_ID}/trace"
mdk_curl_json GET "/api/v1/runs/${RUN_ID}/trace" | python3 -m json.tool
