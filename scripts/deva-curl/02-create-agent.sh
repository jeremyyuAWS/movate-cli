#!/usr/bin/env bash
# Create an agent from the Mova iO wizard's JSON shape.
#
# Usage:
#   ./02-create-agent.sh                                    # seeds a hello-bot demo agent
#   ./02-create-agent.sh "My Bot" "Reply with JSON {\"output\": <text>}"
#   ./02-create-agent.sh "My Bot" "<prompt>" "openai/gpt-4o-mini-2024-07-18"
#
# Args:
#   $1  name        (default: hello-bot)
#   $2  prompt      (default: simple JSON-echo prompt)
#   $3  ai_model    (default: openai/gpt-4o-mini-2024-07-18)

source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

readonly NAME="${1:-hello-bot}"
readonly PROMPT="${2:-Respond ONLY with valid JSON: {\"output\": \"<echo the input>\"}\n\nInput: {{ input.input }}}"
readonly MODEL="${3:-openai/gpt-4o-mini-2024-07-18}"

# Build the wizard JSON payload. python3 handles the string escaping
# for us; safer than bash quoting against arbitrary prompts.
body=$(python3 -c "
import json, sys
print(json.dumps({
    'name': '${NAME}',
    'agent_provider': 'Movate',
    'agent_type': 'Task Agent',
    'role': 'Assistant',
    'description': 'Demo agent created via deva-curl wrapper',
    'agent_role': 'Concise, technical, JSON-only',
    'agent_goal': 'Echo the input back as JSON',
    'agent_prompt': '''${PROMPT}'''.replace('\\\\n', chr(10)),
    'reference_output': 'Example output for the demo',
    'ai_model': '${MODEL}',
    'ai_foundation': 'Azure'
}))
")

echo "→ POST ${MDK_BASE}/api/v1/agents/from-wizard"
echo "  name: ${NAME}"
echo "  model: ${MODEL}"
echo
mdk_curl_json POST "/api/v1/agents/from-wizard" "${body}" | python3 -m json.tool
