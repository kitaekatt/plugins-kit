#!/bin/sh
# presence-prompt.sh -- UserPromptSubmit hook for presence-kit.
#
# Runs `presence.py prompt` with stdin and stdout passed through; presence.py owns
# the attended gate, the payload parse and every output. Keep this script
# portable sh (bash 3.2, zsh and dash): no bash-only variables, arrays or tests.
#
# Interpreter order (the forced form, root CLAUDE.md "Python interpreter
# standard"): the deterministic standalone install first, then BOOTSTRAP_PYTHON
# only when it resolves inside the standalone directory. With neither, print a
# systemMessage and exit 0 so a missing interpreter never blocks a prompt.
#
# Reason for the forced form: the prompt hook runs on every prompt and must run
# under one known interpreter each time, as unreal-kit's
# hooks/pretooluse/check-editor-build-fresh.sh does; BOOTSTRAP_PYTHON is accepted
# only when it resolves inside the standalone install.

set -u

_OS=$(uname -s 2>/dev/null || echo unknown)
case "$_OS" in
    MINGW*|MSYS*|CYGWIN*) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/python.exe" ;;
    *) _STANDALONE_PY="${HOME:-}/.local/share/python-standalone/python/bin/python3" ;;
esac

_PRESENCE_PY=
if [ -x "$_STANDALONE_PY" ]; then
    _PRESENCE_PY="$_STANDALONE_PY"
elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]; then
    _BOOTSTRAP_DIR=$(CDPATH= cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P) || _BOOTSTRAP_DIR=
    _STANDALONE_DIR=$(CDPATH= cd "${HOME:-}/.local/share/python-standalone" 2>/dev/null && pwd -P) || _STANDALONE_DIR=
    if [ -n "$_BOOTSTRAP_DIR" ] && [ -n "$_STANDALONE_DIR" ]; then
        case "$_BOOTSTRAP_DIR" in
            "$_STANDALONE_DIR"/*) _PRESENCE_PY="$BOOTSTRAP_PYTHON" ;;
        esac
    fi
fi

if [ -z "$_PRESENCE_PY" ]; then
    printf '%s\n' '{"systemMessage": "presence-kit: no approved interpreter was found (the bootstrap standalone install), so the prompt hook did nothing. Run bootstrap to provision it."}'
    exit 0
fi

_SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || {
    printf '%s\n' 'presence-prompt.sh: cannot resolve its own directory' >&2
    exit 1
}
_PLUGIN_ROOT=$(CDPATH= cd "$_SCRIPT_DIR/../.." 2>/dev/null && pwd -P) || {
    printf '%s\n' 'presence-prompt.sh: cannot resolve the plugin root' >&2
    exit 1
}
_PRESENCE="$_PLUGIN_ROOT/scripts/presence.py"
if [ ! -f "$_PRESENCE" ]; then
    printf '%s\n' "presence-prompt.sh: missing $_PRESENCE" >&2
    exit 1
fi

exec "$_PRESENCE_PY" "$_PRESENCE" prompt
