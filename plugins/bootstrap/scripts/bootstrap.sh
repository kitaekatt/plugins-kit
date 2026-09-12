#!/usr/bin/env bash
# bootstrap -- report on, run, or reset the bootstrap provisioning pass
#
# Usage:
#   bootstrap                 report whether a bootstrap pass is running; if
#                             one IS, stay attached and stream it until it
#                             finishes, then exit
#   bootstrap --json          report only, never blocking (the scripting form)
#   bootstrap run             apply user/project bootstrap.json and
#                             bootstrap.local.json only; no plugin discovery
#   bootstrap run --verbose   accepted for console compatibility
#   bootstrap reset           clear this project's cooldown so the next session
#                             start runs a real pass
#   bootstrap reset --all     clear every project's cooldown (--status to list,
#                             --project <dir>, --clear-alerts; --help for all)
#   bootstrap -h | --help     show this help
#
# Project layers use the exact working directory; no parent search.
# run refuses while another pass is running, rather than attaching to it.
#
# Scoping: acts on the single marketplace that has a bootstrap data dir under
# ${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}. Set
# BOOTSTRAP_MARKETPLACE to choose when there is more than one -- status reports
# on all of them, but a `run` has to name one rather than guess. `reset` acts
# on every marketplace, like the bootstrap-reset-cooldown lever it delegates to.
#
# This is a THIN shim: it resolves the plugin tree and an interpreter, then
# hands off to scripts/bootstrap_cli.py, which holds all the behavior. It is
# installed into ~/.local/bin as a copy (Windows) or symlink (Unix) by
# session-bootstrap.sh, so it cannot derive the plugin root from $0 and
# discovers it below.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
    -h|--help)
        sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d; s/^# \{0,1\}//'
        exit 0
        ;;
esac

# --- Locate the bootstrap plugin tree ---
# Running from inside the plugin tree (the dev checkout, or the Unix symlink
# whose target resolves back into the cache) needs no discovery.
is_plugin_root() {
    [ -f "$1/hooks/sessionstart/session-bootstrap.sh" ]
}

PLUGIN_ROOT=""
if [ -n "${BOOTSTRAP_PLUGIN_ROOT:-}" ] && is_plugin_root "$BOOTSTRAP_PLUGIN_ROOT"; then
    PLUGIN_ROOT="$BOOTSTRAP_PLUGIN_ROOT"
elif is_plugin_root "$(dirname "$SCRIPT_DIR")"; then
    PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"
else
    # Highest installed CACHE version -- that is the code Claude Code actually
    # loads. Sorted NUMERICALLY per component: lexical order puts 0.98.1 above
    # 0.104.0 and would run a superseded engine. `sort -V` is not assumed (BSD
    # sort on older macOS lacks it).
    _CACHE_ROOT="${HOME}/.claude/plugins/cache"
    _MKTS="${BOOTSTRAP_MARKETPLACE:-}"
    for _cand in $(
        for _d in "$_CACHE_ROOT"/${_MKTS:-*}/bootstrap/*/; do
            [ -f "${_d}hooks/sessionstart/session-bootstrap.sh" ] || continue
            _v="$(basename "${_d%/}")"
            printf '%s\t%s\n' "$_v" "${_d%/}"
        done | sort -t. -k1,1n -k2,2n -k3,3n | cut -f2
    ); do
        PLUGIN_ROOT="$_cand"
    done
    # Fall back to the marketplace clone for a machine that has the
    # marketplace but no cached install yet.
    if [ -z "$PLUGIN_ROOT" ]; then
        for _d in "${HOME}/.claude/plugins/marketplaces"/${_MKTS:-*}/plugins/bootstrap; do
            is_plugin_root "$_d" && PLUGIN_ROOT="$_d" && break
        done
    fi
fi

if [ -z "$PLUGIN_ROOT" ]; then
    echo "bootstrap: no bootstrap plugin tree found under ~/.claude/plugins." >&2
    echo "Install the marketplace's bootstrap plugin, or set BOOTSTRAP_PLUGIN_ROOT." >&2
    exit 2
fi

# --- Resolve an interpreter ---
# Same locations session-bootstrap.sh uses, in the same order, but this lever
# NEVER installs Python: a status probe must not be able to trigger a
# multi-megabyte download, and for `run` the wrapper below does the install
# is owned by Claude's normal lifecycle.
OS="$(uname -s)"
PYTHON=""
if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then
    _CANDIDATES=("${HOME}/.local/share/python-standalone/python/python.exe")
else
    _CANDIDATES=("${HOME}/.local/bin/python3" \
                 "${HOME}/.local/share/python-standalone/python/bin/python3")
fi
for _c in "${_CANDIDATES[@]}" "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
    [ -n "$_c" ] && [ -x "$_c" ] || continue
    if "$_c" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) and sys.version_info[:2] != (3, 14) else 1)" 2>/dev/null; then
        PYTHON="$_c"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # `reset` needs no interpreter either -- it delegates to a pure-bash lever
    # -- so route it here rather than reporting a Python problem it does not
    # have. (The normal path still goes through bootstrap_cli.py, so there is
    # one dispatch, not two.)
    if [ "${1:-}" = "reset" ]; then
        shift
        exec bash "$PLUGIN_ROOT/scripts/bootstrap-reset-cooldown.sh" "$@"
    fi
    echo "bootstrap: no compatible Python found (requires >=3.12, excluding 3.14)." >&2
    echo "Let Claude's normal bootstrap lifecycle provision Python first." >&2
    exit 2
fi

exec "$PYTHON" "$PLUGIN_ROOT/scripts/bootstrap_cli.py" \
    --plugin-root "$PLUGIN_ROOT" "$@"
