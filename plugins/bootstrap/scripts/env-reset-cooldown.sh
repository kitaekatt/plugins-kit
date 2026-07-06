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
# Resolves the bootstrap data dir under ~/.claude/plugins/data/<marketplace>/bootstrap.
# Defaults the marketplace to plugins-kit; override with BOOTSTRAP_MARKETPLACE.

set -uo pipefail

MARKETPLACE="${BOOTSTRAP_MARKETPLACE:-plugins-kit}"
PLUGIN_DATA="${HOME}/.claude/plugins/data/${MARKETPLACE}/bootstrap"
STATE_FILE="$PLUGIN_DATA/env_state.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
}

reset_stamp() {
    if [ -f "$STATE_FILE" ]; then
        rm -f "$STATE_FILE"
        echo "reset env stamp ($STATE_FILE)"
    else
        echo "no env stamp to reset at $STATE_FILE"
    fi
    # Clear the per-project bootstrap cooldown (and session-id guard) so the
    # next SessionStart actually runs the pass that includes the env phase.
    bash "$SCRIPT_DIR/bootstrap-reset-cooldown.sh"
}

print_status() {
    if [ -f "$STATE_FILE" ]; then
        echo "env stamp at $STATE_FILE:"
        cat "$STATE_FILE"
        echo
    else
        echo "no env stamp at $STATE_FILE (next session runs the env phase)"
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
