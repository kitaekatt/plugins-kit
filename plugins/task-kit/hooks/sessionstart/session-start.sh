#!/bin/sh
# session-start.sh -- SessionStart hook for task-kit (matchers resume and clear, async).
#
# Keeps this machine's working-session registrations current when a session
# resumes (same session id, new process) or is cleared (new session id, same
# process). The hook input arrives on stdin; the `source` field selects the work.
# A `fork` or any other source does nothing.
#
# FAST PATH. The registrations live only in git: the project's
# <project>/dev/tasks/.sessions/*.yaml and <project>/tmp/.sessions/*.yaml (the
# project is $CLAUDE_PROJECT_DIR, else the hook input's "cwd"). With no
# registration to update this script exits 0 before starting Python:
#   resume: some session file mentions the input's session id;
#   clear:  some session file holds a record (a "session_id:" line).
# The file name of THIS machine's file (machine.py: host name lowercased, "-wsl"
# under WSL, the sessions.host override) cannot be derived in sh exactly, so both
# checks look at every machine's file. That is correct but broader than needed:
# on clear, a project where only ANOTHER machine holds a registration starts
# Python once and finds nothing to do. A session that registered tasks in a
# different project than this one is not seen (the entry has the same scope).
#
# Never fails the session: every path exits 0. The hook is async, so nothing it
# prints reaches anyone; failures go to <data dir>/sessions/session-start-hook.log
# (the one file this script writes there, only on a failure; the data directory
# is ${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/task-kit,
# the rule bootstrap_guard.data_dir uses). The Python entry
# (scripts/task_session_start.py) logs its own errors; this script appends any
# stderr of the entry to the same log.
#
# Keep this script portable sh (bash 3.2, zsh and dash): no bash-only variables,
# arrays or tests.
#
# Interpreter order (the forced form, root CLAUDE.md "Python interpreter
# standard"): the deterministic standalone install first, then BOOTSTRAP_PYTHON
# only when it resolves inside the standalone directory. The entry re-execs into
# the task-kit venv itself, as task.py does.
#
# Reason for the forced form: a Claude Code hook script bootstrap provisions for
# must run under one known interpreter each time, as presence-kit's
# hooks/stop/presence-stop.sh does; BOOTSTRAP_PYTHON is accepted only when it
# resolves inside the standalone install.

set -u

_INPUT=$(cat 2>/dev/null) || _INPUT=
_FLAT=$(printf '%s' "$_INPUT" | tr -d '\n\r')
_SOURCE=$(printf '%s' "$_FLAT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
_SESSION=$(printf '%s' "$_FLAT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
_CWD=$(printf '%s' "$_FLAT" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed 's|\\\\|/|g')

_DATA_ROOT="${CLAUDE_BOOTSTRAP_DATA_ROOT:-${HOME:-}/.claude/plugins/data}"
_DATA_DIR="$_DATA_ROOT/plugins-kit/task-kit"
_PROJECT="${CLAUDE_PROJECT_DIR:-$_CWD}"
[ -n "$_PROJECT" ] && [ -d "$_PROJECT" ] || exit 0

case "$_SOURCE" in
    resume)
        case "$_SESSION" in
            ""|*[!A-Za-z0-9_-]*|[!A-Za-z0-9]*) exit 0 ;;
        esac
        _FOUND=
        for _F in "$_PROJECT"/dev/tasks/.sessions/*.yaml "$_PROJECT"/tmp/.sessions/*.yaml; do
            if [ -f "$_F" ] && grep -q -F -- "$_SESSION" "$_F" 2>/dev/null; then
                _FOUND=1
                break
            fi
        done
        [ -n "$_FOUND" ] || exit 0
        ;;
    clear)
        _FOUND=
        for _F in "$_PROJECT"/dev/tasks/.sessions/*.yaml "$_PROJECT"/tmp/.sessions/*.yaml; do
            if [ -f "$_F" ] && grep -q 'session_id:' "$_F" 2>/dev/null; then
                _FOUND=1
                break
            fi
        done
        [ -n "$_FOUND" ] || exit 0
        ;;
    *)
        exit 0
        ;;
esac

_LOG="$_DATA_DIR/sessions/session-start-hook.log"

_log() {
    mkdir -p "$_DATA_DIR/sessions" 2>/dev/null
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" "$1" >>"$_LOG" 2>/dev/null
    return 0
}

_OS=$(uname -s 2>/dev/null || echo unknown)
case "$_OS" in
    MINGW*|MSYS*|CYGWIN*) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/python.exe" ;;
    *) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/bin/python3" ;;
esac

_TASK_PY=
if [ -x "$_STANDALONE_PY" ]; then
    _TASK_PY="$_STANDALONE_PY"
elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]; then
    _BOOTSTRAP_DIR=$(CDPATH= cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P) || _BOOTSTRAP_DIR=
    _STANDALONE_DIR=$(CDPATH= cd "${HOME:-}/.local/share/python-standalone" 2>/dev/null && pwd -P) || _STANDALONE_DIR=
    if [ -n "$_BOOTSTRAP_DIR" ] && [ -n "$_STANDALONE_DIR" ]; then
        case "$_BOOTSTRAP_DIR" in
            "$_STANDALONE_DIR"/*) _TASK_PY="$BOOTSTRAP_PYTHON" ;;
        esac
    fi
fi

if [ -z "$_TASK_PY" ]; then
    _log "session-start.sh: no approved interpreter was found (the bootstrap standalone install); the $_SOURCE refresh was skipped. Run bootstrap to provision it."
    exit 0
fi

_SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || {
    _log "session-start.sh: cannot resolve its own directory"
    exit 0
}
_PLUGIN_ROOT=$(CDPATH= cd "$_SCRIPT_DIR/../.." 2>/dev/null && pwd -P) || {
    _log "session-start.sh: cannot resolve the plugin root"
    exit 0
}
_ENTRY="$_PLUGIN_ROOT/skills/task/scripts/task_session_start.py"
if [ ! -f "$_ENTRY" ]; then
    _log "session-start.sh: missing $_ENTRY"
    exit 0
fi

# stderr is captured, not redirected into the log, so a quiet run creates no file.
_ERR=$(printf '%s' "$_INPUT" | "$_TASK_PY" "$_ENTRY" 2>&1 >/dev/null)
_STATUS=$?
if [ -n "$_ERR" ]; then
    _log "$_ERR"
fi
if [ "$_STATUS" -ne 0 ]; then
    _log "session-start.sh: $_ENTRY exited $_STATUS"
fi
exit 0
