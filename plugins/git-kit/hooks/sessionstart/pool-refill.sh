#!/bin/sh
# pool-refill.sh -- SessionStart: start a detached refill of the shared worktree
# pool (scripts/worktree_pool.py). It returns at once. Outside a Git checkout, or
# when git_kit.worktrees.pool is not configured, it does nothing. With no
# interpreter at all it prints one systemMessage warning and exits 0.
# Keep this script portable sh (bash 3.2, zsh and dash).
#
# Interpreter order, as in llm-kit's run-direct-hook.sh: the bootstrap standalone
# install, then BOOTSTRAP_PYTHON. worktrees.py re-execs under git-kit's venv.

set -u

case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW*|MSYS*|CYGWIN*) _PY="${HOME:-}/.local/share/python-standalone/python/python.exe" ;;
    *) _PY="${HOME:-}/.local/share/python-standalone/python/bin/python3" ;;
esac
if [ ! -x "$_PY" ]; then
    _PY="${BOOTSTRAP_PYTHON:-}"
fi
if [ -z "$_PY" ] || [ ! -x "$_PY" ]; then
    printf '%s\n' '{"systemMessage": "git-kit: no Python interpreter was found (the bootstrap standalone install), so the worktree pool refill did not start. Run bootstrap to provision it."}'
    exit 0
fi

_PLUGIN=$(CDPATH= cd "$(dirname "$0")/../.." 2>/dev/null && pwd -P) || {
    printf '%s\n' 'pool-refill.sh: cannot resolve the plugin directory' >&2
    exit 1
}
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 1
exec "$_PY" "$_PLUGIN/scripts/worktrees.py" pool refill --background --quiet-if-off </dev/null
