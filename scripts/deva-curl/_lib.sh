# Sourced by every script in this dir. Reads .env (the gitignored
# file with the bearer), bails with a clear error if missing.
#
# Not standalone — `source _lib.sh` from another script.

set -euo pipefail

readonly _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${_script_dir}/.env" ]]; then
    cat >&2 <<EOM
✗ Missing ${_script_dir}/.env

One-time setup:
    cp ${_script_dir}/.env.example ${_script_dir}/.env
    \$EDITOR ${_script_dir}/.env   # paste the MDK_TOKEN value sent to you

Then re-run this script.
EOM
    exit 1
fi

# shellcheck disable=SC1091
source "${_script_dir}/.env"

if [[ -z "${MDK_TOKEN:-}" ]] || [[ "${MDK_TOKEN}" == "mvt_live_PASTE_THE_BEARER_HERE" ]]; then
    echo "✗ MDK_TOKEN is not set in .env — paste the bearer first" >&2
    exit 1
fi

# Helpers — used by every endpoint wrapper.
mdk_curl_json() {
    # mdk_curl_json METHOD PATH [JSON_BODY]
    # All requests go through this so headers/auth/error formatting
    # are consistent. Returns the response body to stdout; status
    # code goes to stderr as "← HTTP N" so the operator can see it
    # without it polluting the JSON output (pipe to jq cleanly).
    local method="${1:?method required}"
    local path="${2:?path required}"
    local body="${3:-}"

    local args=(
        -sS
        -X "${method}"
        -H "Authorization: Bearer ${MDK_TOKEN}"
        -H "Accept: application/json"
        --max-time 60
        -w "\n← HTTP %{http_code} (%{time_total}s)\n"
    )
    if [[ -n "${body}" ]]; then
        args+=(
            -H "Content-Type: application/json"
            -d "${body}"
        )
    fi

    local response
    response="$(curl "${args[@]}" "${MDK_BASE}${path}")"
    # Split body from the status footer; print body to stdout, status to stderr.
    local body_part="${response%$'\n'← HTTP*}"
    local status_part="← HTTP${response#*← HTTP}"
    echo "${body_part}"
    echo "${status_part}" >&2
}

mdk_curl_form() {
    # mdk_curl_form METHOD PATH FORM_FIELD_FILE_ARGS...
    # For multipart/form-data uploads (POST /api/v1/agents with files).
    local method="${1:?method required}"
    local path="${2:?path required}"
    shift 2

    local args=(
        -sS
        -X "${method}"
        -H "Authorization: Bearer ${MDK_TOKEN}"
        -H "Accept: application/json"
        --max-time 60
        -w "\n← HTTP %{http_code} (%{time_total}s)\n"
    )
    while [[ $# -gt 0 ]]; do
        args+=("-F" "$1")
        shift
    done

    curl "${args[@]}" "${MDK_BASE}${path}"
}
