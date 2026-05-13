#!/usr/bin/env bash
# Smoke: liveness + readiness. No auth needed.
# Use this first to confirm you can reach the runtime at all.
#
#   ./01-health.sh

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

echo "→ GET ${MDK_BASE}/healthz"
curl -sS "${MDK_BASE}/healthz" | python3 -m json.tool
echo
echo "→ GET ${MDK_BASE}/ready"
curl -sS "${MDK_BASE}/ready" | python3 -m json.tool
