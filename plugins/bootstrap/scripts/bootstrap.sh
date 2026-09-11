#!/usr/bin/env bash
# bootstrap -- report on, or run, the bootstrap provisioning pass
#
# Usage:
#   bootstrap                 report whether a bootstrap pass is running; if
#                             one IS, stay attached and stream it until it
#                             finishes, then exit
#   bootstrap --json          report only, never blocking (the scripting form)
#   bootstrap run             the same, plus START a pass when none is running
#   bootstrap run --verbose   extra flags are passed through to the engine
#   bootstrap -h | --help     show this help
#
# Scoping: acts on the single marketplace that has a bootstrap data dir under
# ${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}. Set
# BOOTSTRAP_MARKETPLACE to choose when there is more than one -- status reports
# on all of them, but a `run` has to name one rather than guess.
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
        sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
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

WRAPPER="$PLUGIN_ROOT/hooks/sessionstart/session-bootstrap.sh"

# --- Resolve an interpreter ---
# Same locations session-bootstrap.sh uses, in the same order, but this lever
# NEVER installs Python: a status probe must not be able to trigger a
# multi-megabyte download, and for `run` the wrapper below does the install
# itself as part of the pass it was going to run anyway.
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
    if "$_c" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" 2>/dev/null; then
        PYTHON="$_c"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # No interpreter yet (a machine whose first pass has not run). `run` still
    # works: the wrapper installs standalone Python as its first act. Status
    # genuinely cannot be answered without one -- and on such a machine the
    # answer is almost certainly "nothing is running" anyway, so say what is
    # actually known rather than guessing it.
    if [ "${1:-}" = "run" ]; then
        shift
        echo "bootstrap: no Python yet; the pass will install one first."
        exec bash "$WRAPPER" --console "$@"
    fi
    echo "bootstrap: no Python 3 found, so the engine lock cannot be read." >&2
    echo "Run 'bootstrap run' -- the pass installs a standalone Python first." >&2
    exit 2
fi

exec "$PYTHON" "$PLUGIN_ROOT/scripts/bootstrap_cli.py" \
    --plugin-root "$PLUGIN_ROOT" "$@"
