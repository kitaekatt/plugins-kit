#!/bin/sh
# run-direct-hook.sh -- launches one of this plugin's /direct hook scripts.
#
# Usage: run-direct-hook.sh <script path relative to this directory>
#
# The hook scripts are stdlib-only and guardrails, not a security boundary (see
# direct_gate.py). Keep this script portable sh (bash 3.2, zsh and dash).
#
# Interpreter order (the forced form, root CLAUDE.md "Python interpreter
# standard"): the deterministic standalone install first, then BOOTSTRAP_PYTHON
# only when it resolves inside the standalone directory. With neither, print a
# systemMessage and exit 0 so a missing interpreter never blocks a prompt or a
# tool call. Reason for the forced form: these hooks run on every prompt and
# every Bash call and must run under one known interpreter each time, as
# presence-kit's prompt hook does.

set -u

_SCRIPT=${1:-}
if [ -z "$_SCRIPT" ]; then
    printf '%s\n' 'run-direct-hook.sh: no script named' >&2
    exit 1
fi

_OS=$(uname -s 2>/dev/null || echo unknown)
case "$_OS" in
    MINGW*|MSYS*|CYGWIN*) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/python.exe" ;;
    *) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/bin/python3" ;;
esac

_PY=
if [ -x "$_STANDALONE_PY" ]; then
    _PY="$_STANDALONE_PY"
elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]; then
    _BOOTSTRAP_DIR=$(CDPATH= cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P) || _BOOTSTRAP_DIR=
    _STANDALONE_DIR=$(CDPATH= cd "${HOME:-}/.local/share/python-standalone" 2>/dev/null && pwd -P) || _STANDALONE_DIR=
    if [ -n "$_BOOTSTRAP_DIR" ] && [ -n "$_STANDALONE_DIR" ]; then
        case "$_BOOTSTRAP_DIR" in
            "$_STANDALONE_DIR"/*) _PY="$BOOTSTRAP_PYTHON" ;;
        esac
    fi
fi

if [ -z "$_PY" ]; then
    printf '%s\n' '{"systemMessage": "llm-kit: no approved interpreter was found (the bootstrap standalone install), so the /direct hook did nothing. Run bootstrap to provision it."}'
    exit 0
fi

_DIR=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || {
    printf '%s\n' 'run-direct-hook.sh: cannot resolve its own directory' >&2
    exit 1
}
if [ ! -f "$_DIR/$_SCRIPT" ]; then
    printf '%s\n' "run-direct-hook.sh: missing $_DIR/$_SCRIPT" >&2
    exit 1
fi

exec "$_PY" "$_DIR/$_SCRIPT"
