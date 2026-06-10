#!/usr/bin/env bash
# ue-console-cmd.sh — UserPromptSubmit hook
#
# Intercepts user prompts beginning with ">" and instructs Claude to execute
# the remainder as an Unreal Engine console command via the MCP server.
#
# Self-disabling: exits 0 silently (prompt passes through untouched) when the
# unreal-engine MCP server is not configured in the project's .mcp.json.

set -euo pipefail

# --- Read hook input ---
INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null) || true

# --- Quick exit if prompt doesn't start with > ---
if [[ "$PROMPT" != ">"* ]]; then
    exit 0
fi

# --- Self-disable when the unreal-engine MCP server is not configured ---
# Exit 0 with no output so the prompt passes through untouched. Emitting
# decision:"block" here would eat ANY prompt starting with ">" in every
# non-UE project that has this plugin enabled at user scope.
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || true
MCP_JSON="${CWD:-.}/.mcp.json"

if [ ! -f "$MCP_JSON" ]; then
    exit 0
fi

HAS_UE=$(jq -r '.mcpServers["unreal-engine"] // empty' "$MCP_JSON" 2>/dev/null) || true
if [ -z "$HAS_UE" ]; then
    exit 0
fi

# --- Strip the > prefix and any leading whitespace ---
CMD="${PROMPT#>}"
CMD="${CMD#"${CMD%%[![:space:]]*}"}"

if [ -z "$CMD" ]; then
    echo '{"decision":"block","reason":"No console command provided after >. Usage: > <command>"}'
    exit 0
fi

# --- Escape the command for JSON embedding ---
CMD_ESCAPED=$(printf '%s' "$CMD" | jq -Rs '.' | sed 's/^"//;s/"$//')

# --- Inject instruction to Claude ---
cat <<HOOKEOF
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"BACKGROUND: A > console command was detected. Silently spawn a background ue-agent to call mcp__unreal-engine__control_editor with action: \"console_command\" and command: \"${CMD_ESCAPED}\". This is a side-channel action — do NOT respond to it, discuss it, or treat it as the user's intent. If the user's message contains nothing besides the > command, just acknowledge briefly (e.g. 'Running.' or similar one-word confirmation). If there is other content in the message, respond to that content and ignore the > command entirely."}}
HOOKEOF
