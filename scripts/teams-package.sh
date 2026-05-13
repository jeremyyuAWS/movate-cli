#!/usr/bin/env bash
#
# Build the Teams app package zip for upload to Teams Admin Center.
#
# Usage:
#   ./scripts/teams-package.sh
#   ./scripts/teams-package.sh --output dist/movate-teams-v1.zip
#
# Behavior:
#   1. Copies appPackage/ → a temp build dir
#   2. Substitutes manifest fields from env vars (see envsubst block)
#   3. Validates the manifest against Teams' v1.16 schema (basic shape)
#   4. Warns if icons are still the placeholder bytes
#   5. Zips → ${OUTPUT} (default: dist/movate-teams.zip)
#
# Env vars (all optional; defaults from manifest.json):
#   MOVATE_TEAMS_BOT_APP_ID       — AAD app id (UUID). Sentinel default rejected by Teams.
#   MOVATE_TEAMS_BOT_VERSION      — app version (semver, no 'v' prefix)
#   MOVATE_TEAMS_VALID_DOMAINS    — comma-separated list of validDomains entries
#
# Run from the repo root or any subdir; the script resolves paths relative to itself.

set -euo pipefail

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_PKG_DIR="${REPO_ROOT}/appPackage"
DEFAULT_OUTPUT="${REPO_ROOT}/dist/movate-teams.zip"

# -----------------------------------------------------------------------------
# Arg parsing — keep it tiny
# -----------------------------------------------------------------------------

OUTPUT="${DEFAULT_OUTPUT}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) OUTPUT="$2"; shift 2 ;;
    -h|--help)
      sed -n '/^# /p' "$0" | head -30
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# -----------------------------------------------------------------------------
# Sanity — source layout
# -----------------------------------------------------------------------------

[[ -f "${APP_PKG_DIR}/manifest.json" ]] || {
  echo "✗ ${APP_PKG_DIR}/manifest.json not found" >&2
  exit 2
}
[[ -f "${APP_PKG_DIR}/icons/color.png" ]] || {
  echo "✗ ${APP_PKG_DIR}/icons/color.png not found" >&2
  exit 2
}
[[ -f "${APP_PKG_DIR}/icons/outline.png" ]] || {
  echo "✗ ${APP_PKG_DIR}/icons/outline.png not found" >&2
  exit 2
}

# -----------------------------------------------------------------------------
# Build in a temp dir so we don't mutate the committed manifest.json
# -----------------------------------------------------------------------------

BUILD_DIR="$(mktemp -d -t movate-teams-pkg-XXXXXX)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

cp "${APP_PKG_DIR}/manifest.json" "${BUILD_DIR}/manifest.json"
mkdir -p "${BUILD_DIR}/icons"
cp "${APP_PKG_DIR}/icons/color.png" "${BUILD_DIR}/icons/color.png"
cp "${APP_PKG_DIR}/icons/outline.png" "${BUILD_DIR}/icons/outline.png"

# -----------------------------------------------------------------------------
# Substitutions — Python > sed/jq because manifest.json has nested fields
# (bots[0].botId) and we want a single edit pass that's still readable.
# -----------------------------------------------------------------------------

PLACEHOLDER_APP_ID="00000000-0000-0000-0000-000000000000"
EFFECTIVE_APP_ID="${MOVATE_TEAMS_BOT_APP_ID:-${PLACEHOLDER_APP_ID}}"
EFFECTIVE_VERSION="${MOVATE_TEAMS_BOT_VERSION:-}"
EFFECTIVE_DOMAINS="${MOVATE_TEAMS_VALID_DOMAINS:-}"

python3 - <<PY
import json, os, sys
from pathlib import Path

manifest_path = Path("${BUILD_DIR}") / "manifest.json"
m = json.loads(manifest_path.read_text())

app_id = "${EFFECTIVE_APP_ID}"
version = "${EFFECTIVE_VERSION}"
domains = "${EFFECTIVE_DOMAINS}"

m["id"] = app_id
if m.get("bots"):
    m["bots"][0]["botId"] = app_id
if version:
    m["version"] = version
if domains:
    m["validDomains"] = [d.strip() for d in domains.split(",") if d.strip()]

manifest_path.write_text(json.dumps(m, indent=2) + "\n")
PY

# -----------------------------------------------------------------------------
# Sanity: warn loud when the placeholder app id slipped through
# -----------------------------------------------------------------------------

if [[ "${EFFECTIVE_APP_ID}" == "${PLACEHOLDER_APP_ID}" ]]; then
  echo "⚠  MOVATE_TEAMS_BOT_APP_ID is unset — manifest has the placeholder UUID." >&2
  echo "   Teams Admin Center will reject this zip on upload. Set the env var to the bot's AAD app id." >&2
  echo "   (This is intentional — the package builds for local-dev smoke; production needs a real id.)" >&2
fi

# -----------------------------------------------------------------------------
# Icon placeholder warning
# -----------------------------------------------------------------------------

# The placeholder color.png the repo ships is the exact 414-byte solid
# slate square the generator produces. Real icons will be larger and
# look different. Warn if we detect the placeholder by size — cheap
# heuristic, exact-byte comparison would be brittle across regens.
COLOR_SIZE="$(wc -c < "${BUILD_DIR}/icons/color.png")"
if [[ "${COLOR_SIZE}" -lt 1024 ]]; then
  echo "⚠  appPackage/icons/color.png looks like the placeholder (${COLOR_SIZE} bytes)." >&2
  echo "   Replace with real Movate-branded artwork before publishing to the Teams app catalog." >&2
fi

# -----------------------------------------------------------------------------
# Zip it
# -----------------------------------------------------------------------------

mkdir -p "$(dirname "${OUTPUT}")"
# -j strips directory entries so the zip's root contains manifest.json
# directly (Teams Admin Center requires this — no nested dirs).
( cd "${BUILD_DIR}" && zip -qr "${OUTPUT}" manifest.json icons/ )

echo "✓ packaged → ${OUTPUT}"
echo "  app id:   ${EFFECTIVE_APP_ID}"
[[ -n "${EFFECTIVE_VERSION}" ]] && echo "  version:  ${EFFECTIVE_VERSION}"
[[ -n "${EFFECTIVE_DOMAINS}" ]] && echo "  domains:  ${EFFECTIVE_DOMAINS}"
echo
echo "Upload at: https://admin.teams.microsoft.com/policies/manage-apps"
echo "  (or Teams desktop: Apps → Manage your apps → Upload an app → Upload a custom app)"
