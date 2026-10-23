#!/bin/sh
# databench-ensure.sh -- async task-kit SessionStart hook.
#
# startup, resume and clear ask Databench to ensure its generation service is
# running. Databench owns the regeneration-enabled switch and starts no browser.
# The hook never fails the session. A missing command or a failed ensure appends
# exactly one line to task-kit's SessionStart log.
#
# Keep this script portable sh (bash 3.2, zsh and dash): no bash-only variables,
# arrays or tests.

set -u

_INPUT=$(cat 2>/dev/null) || _INPUT=
_FLAT=$(printf '%s' "$_INPUT" | tr -d '\n\r')
_SOURCE=$(printf '%s' "$_FLAT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

case "$_SOURCE" in
    startup|resume|clear) ;;
    *) exit 0 ;;
esac

_DATA_ROOT="${CLAUDE_BOOTSTRAP_DATA_ROOT:-${HOME:-}/.claude/plugins/data}"
_DATA_DIR="$_DATA_ROOT/plugins-kit/task-kit"
_LOG="$_DATA_DIR/sessions/session-start-hook.log"

_log() {
    mkdir -p "$_DATA_DIR/sessions" 2>/dev/null
    _STAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null) || _STAMP=
    printf '%s %s\n' "$_STAMP" "$1" >>"$_LOG" 2>/dev/null
    return 0
}

if ! command -v databench >/dev/null 2>&1; then
    _log "databench-ensure.sh: databench is not on PATH"
    exit 0
fi

mkdir -p "$_DATA_DIR/sessions" 2>/dev/null
databench ensure >/dev/null 2>>"$_LOG"
_STATUS=$?
if [ "$_STATUS" -ne 0 ]; then
    _log "databench-ensure.sh: databench ensure exited $_STATUS"
fi

exit 0
