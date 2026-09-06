#!/usr/bin/env bash
# env-reset-cooldown — force the next session's env.json pass
#
# The env.json personalization phase is gated by a dedicated stamp
# (env_state.json in bootstrap's data dir) recording the merged-manifest
# sha256, the engine version, and the last result; a clean, unchanged pass
# is skipped. Deleting the stamp forces the phase to run on the next
# SessionStart — the explicit "re-converge my machine" lever. The bootstrap
# per-project cooldown gates the WHOLE SessionStart pass, so it is cleared
# too (via bootstrap-reset-cooldown.sh) — otherwise "next session runs the
# env phase" would not hold inside the cooldown window.
#
# Usage:
#   env-reset-cooldown             delete the env stamp + clear this project's cooldown
#   env-reset-cooldown --status    show the current env stamp, no writes
#   env-reset-cooldown -h | --help show this help
#
# Resolves the bootstrap data dir(s) under
# ${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/<marketplace>/bootstrap.
# With BOOTSTRAP_MARKETPLACE unset, acts on EVERY marketplace directory found
# under the data root rather than assuming plugins-kit. Set
# BOOTSTRAP_MARKETPLACE to scope to one marketplace.

set -uo pipefail

BOOTSTRAP_DATA_ROOT="${CLAUDE_BOOTSTRAP_DATA_ROOT:-${HOME}/.claude/plugins/data}"
MARKETPLACE="${BOOTSTRAP_MARKETPLACE:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
}

# One bootstrap data dir per line -- mirrors bootstrap-reset-cooldown.sh's
# plugin_data_dirs() so both levers agree on which marketplaces exist.
plugin_data_dirs() {
    if [ -n "$MARKETPLACE" ]; then
        printf '%s\n' "$BOOTSTRAP_DATA_ROOT/$MARKETPLACE/bootstrap"
        return
    fi
    local found=0
    for d in "$BOOTSTRAP_DATA_ROOT"/*/bootstrap; do
        [ -d "$d" ] || continue
        found=1
        printf '%s\n' "$d"
    done
    if [ "$found" -eq 0 ]; then
        printf '%s\n' "$BOOTSTRAP_DATA_ROOT/plugins-kit/bootstrap"
    fi
}

reset_stamp() {
    local pd
    local any_reset=0
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        local state_file="$pd/env_state.json"
        if [ -f "$state_file" ]; then
            rm -f "$state_file"
            echo "reset env stamp ($state_file)"
            any_reset=1
        fi
    done < <(plugin_data_dirs)
    if [ "$any_reset" -eq 0 ]; then
        echo "no env stamp to reset under $BOOTSTRAP_DATA_ROOT"
    fi
    # Clear the per-project bootstrap cooldown (and session-id guard) so the
    # next SessionStart actually runs the pass that includes the env phase.
    #
    # The sibling is resolved rather than assumed: this script is invoked both
    # from the plugin tree (where it sits beside bootstrap-reset-cooldown.SH)
    # and via the ~/.local/bin shim (where the sibling is installed WITHOUT the
    # .sh extension). Hardcoding either name breaks the other -- and the shim
    # is the form the docs tell people to run.
    local sibling=""
    for sibling in "$SCRIPT_DIR/bootstrap-reset-cooldown.sh" \
                   "$SCRIPT_DIR/bootstrap-reset-cooldown" \
                   "$(command -v bootstrap-reset-cooldown 2>/dev/null)"; do
        [ -n "$sibling" ] && [ -f "$sibling" ] && break
        sibling=""
    done
    if [ -n "$sibling" ]; then
        bash "$sibling"
    else
        echo "env-reset-cooldown: bootstrap-reset-cooldown not found; the env" >&2
        echo "  stamp is cleared but the per-project cooldown still gates the" >&2
        echo "  pass, so the env phase may not run until it expires." >&2
        return 1
    fi
}

print_status() {
    local pd
    local any_dir=0
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        any_dir=1
        local mkt
        mkt="$(basename "$(dirname "$pd")")"
        local state_file="$pd/env_state.json"
        if [ -f "$state_file" ]; then
            echo "$mkt: env stamp at $state_file:"
            cat "$state_file"
            echo
        else
            echo "$mkt: no env stamp at $state_file (next session runs the env phase)"
        fi
    done < <(plugin_data_dirs)
    if [ "$any_dir" -eq 0 ]; then
        echo "no marketplace data dirs found under $BOOTSTRAP_DATA_ROOT"
    fi
}

# --- Parse args ---
MODE="reset"

while [ $# -gt 0 ]; do
    case "$1" in
        --status) MODE="status" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "$MODE" in
    reset)  reset_stamp ;;
    status) print_status ;;
esac

exit 0
