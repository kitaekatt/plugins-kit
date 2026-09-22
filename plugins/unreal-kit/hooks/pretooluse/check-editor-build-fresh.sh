#!/bin/sh
# check-editor-build-fresh.sh -- PreToolUse hook for mcp__unreal-engine__*
#
# The foreground path only checks the tool name and current marker. Detection
# runs detached so an advisory check cannot add latency to an MCP call. Keep
# this script compatible with bash 3.2 and direct zsh execution: it is written
# as portable sh and does not use BASH_REMATCH or BASH_SOURCE.

set -u

INPUT=$(cat)
JSON_ONE_LINE=$(printf '%s' "$INPUT" | tr '\n' ' ')

if ! printf '%s' "$JSON_ONE_LINE" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"mcp__unreal-engine__'; then
    exit 0
fi

# The hook payload uses a JSON string. Project paths do not contain a quote;
# decode the escaped separators used by Windows JSON producers after extraction.
CWD_ESCAPED=$(printf '%s' "$JSON_ONE_LINE" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p')
CWD=$(printf '%s' "$CWD_ESCAPED" | sed 's/\\\\/\\/g')
[ -n "$CWD" ] || exit 0

MARKER="$CWD/.local-data/plugins-kit/unreal-kit/editor-stale.flag"
LOG_DIR="$CWD/.local-data/plugins-kit/unreal-kit"
LOG="$LOG_DIR/editor-stale-detector.log"

if [ -f "$MARKER" ]; then
    cat <<'HOOKEOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Unreal editor build is stale -- rebuild the editor before saving any asset. UnrealEditor-BuildSettings.dll is older than Engine/Build/Build.version, so saves will be stamped with Changelist=0 and rejected by the cooker as 'empty engine version'.","additionalContext":"Editor requires rebuild before assets can be safely saved. UnrealEditor-BuildSettings.dll mtime is older than Engine/Build/Build.version, which means the running editor was launched without rebuilding after the latest sync. Asset saves through MCP will stamp Summary.SavedByEngineVersion with Changelist=0 and be rejected by the cooker as 'empty engine version'. Run a build before any save tool call."}}
HOOKEOF
fi

# Start the detector with all inherited descriptors detached. Its stdout and
# stderr are retained in the project-ephemeral log so wrapper and import
# failures remain diagnosable after the hook returns.
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || exit 0
DETECTOR="$SCRIPT_DIR/detect-editor-stale.py"
[ -f "$DETECTOR" ] || exit 0

mkdir -p "$LOG_DIR" 2>/dev/null || exit 0
(
    exec </dev/null >>"$LOG" 2>&1

    _OS=$(uname -s 2>/dev/null || echo unknown)
    case "$_OS" in
        MINGW*|MSYS*|CYGWIN*) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/python.exe" ;;
        *) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/bin/python3" ;;
    esac

    _DETECT_PY=
    if [ -x "$_STANDALONE_PY" ]; then
        _DETECT_PY="$_STANDALONE_PY"
    elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]; then
        _BOOTSTRAP_DIR=$(CDPATH= cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P) || _BOOTSTRAP_DIR=
        _STANDALONE_DIR=$(CDPATH= cd "${HOME:-}/.local/share/python-standalone" 2>/dev/null && pwd -P) || _STANDALONE_DIR=
        case "$_BOOTSTRAP_DIR" in
            "$_STANDALONE_DIR"/*) _DETECT_PY="$BOOTSTRAP_PYTHON" ;;
        esac
    fi
    if [ -z "$_DETECT_PY" ]; then
        printf '%s\n' '[check-editor-build-fresh] UNKNOWN: no approved detector interpreter was found'
        exit 0
    fi

    printf '%s' "$INPUT" | "$_DETECT_PY" "$DETECTOR"
    _STATUS=$?
    if [ "$_STATUS" -ne 0 ]; then
        printf '[check-editor-build-fresh] detector exited with status %s\n' "$_STATUS"
    fi
) &

exit 0
