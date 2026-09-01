#!/usr/bin/env bash
# bootstrap-reset-cooldown — clear bootstrap session-start cooldown(s)
#
# The bootstrap SessionStart hook throttles its per-project cooldown to avoid
# re-running expensive checks every time you re-enter the same project. After a
# bootstrap.json change (or to force a re-check for any reason) the cooldown
# can be cleared with this command.
#
# Usage:
#   bootstrap-reset-cooldown                     reset cooldown for current project (CWD)
#   bootstrap-reset-cooldown --all               reset cooldown for every project
#   bootstrap-reset-cooldown --project <dir>     reset cooldown for an explicit project dir
#   bootstrap-reset-cooldown --status            list cooldowns and ages, no writes
#   bootstrap-reset-cooldown --clear-alerts      also clear bootstrap_alert.json
#   bootstrap-reset-cooldown --clear-alerts --force  also delete undelivered pass output
#   bootstrap-reset-cooldown -h | --help         show this help
#
# Resolves the bootstrap data dir under ~/.claude/plugins/data/<marketplace>/bootstrap.
# Defaults the marketplace to plugins-kit; override with BOOTSTRAP_MARKETPLACE.

set -uo pipefail

MARKETPLACE="${BOOTSTRAP_MARKETPLACE:-plugins-kit}"
PLUGIN_DATA="${HOME}/.claude/plugins/data/${MARKETPLACE}/bootstrap"
COOLDOWN_DIR="$PLUGIN_DATA/cooldowns"
GUARD_FILE="$PLUGIN_DATA/last_session_id"
LEGACY_COOLDOWN_FILE="$PLUGIN_DATA/last_run_epoch"
ALERT_FILE="$PLUGIN_DATA/bootstrap_alert.json"
PENDING_FILE="$PLUGIN_DATA/bootstrap_display.pending"

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

hash_path() {
    local p="$1"
    if command -v sha1sum >/dev/null 2>&1; then
        printf '%s' "$p" | sha1sum | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$p" | shasum -a 1 | awk '{print $1}'
    else
        echo "_global_"
    fi
}

reset_one() {
    local project_dir="$1"
    local key
    key=$(hash_path "$project_dir")
    local f="$COOLDOWN_DIR/last_run_epoch.$key"
    if [ -f "$f" ]; then
        rm -f "$f"
        echo "reset cooldown for $project_dir"
    else
        echo "no cooldown to reset for $project_dir"
    fi
    # Also clear the session_id guard so the next launch isn't skipped by it.
    if [ -f "$GUARD_FILE" ]; then
        rm -f "$GUARD_FILE"
    fi
}

reset_all() {
    if [ -d "$COOLDOWN_DIR" ]; then
        local n
        n=$(find "$COOLDOWN_DIR" -maxdepth 1 -name 'last_run_epoch.*' -type f 2>/dev/null | wc -l | tr -d ' ')
        rm -f "$COOLDOWN_DIR"/last_run_epoch.* 2>/dev/null
        echo "reset $n per-project cooldown(s)"
    else
        echo "no cooldown directory at $COOLDOWN_DIR"
    fi
    # Pre-per-project legacy cooldown file
    if [ -f "$LEGACY_COOLDOWN_FILE" ]; then
        rm -f "$LEGACY_COOLDOWN_FILE"
        echo "reset legacy global cooldown"
    fi
    if [ -f "$GUARD_FILE" ]; then
        rm -f "$GUARD_FILE"
        echo "cleared session_id guard"
    fi
}

clear_alerts() {
    local cleared=0
    if [ -f "$ALERT_FILE" ]; then
        rm -f "$ALERT_FILE"
        echo "cleared $ALERT_FILE"
        cleared=1
    fi
    # The pending file is the ONLY channel any pass has to the user, and the
    # shell's pre-Python failure paths write nothing else. Deleting it
    # unconditionally between a pass and the next prompt silently discards
    # that pass's verdict -- including, in the worst case, the message saying
    # bootstrap could not run at all. Clearing an ALERT is the stated purpose;
    # destroying an undelivered message is collateral. So it goes only with
    # --force, and says what it is withholding otherwise.
    if [ -f "$PENDING_FILE" ]; then
        if [ "$FORCE" -eq 1 ]; then
            rm -f "$PENDING_FILE"
            echo "cleared $PENDING_FILE"
            cleared=1
        else
            echo "kept $PENDING_FILE (undelivered pass output; --force to delete)"
        fi
    fi
    if [ $cleared -eq 0 ]; then
        echo "no alerts to clear"
    fi
}

print_status() {
    if [ ! -d "$COOLDOWN_DIR" ]; then
        echo "no cooldowns recorded ($COOLDOWN_DIR does not exist)"
        return
    fi
    local now
    now=$(date +%s 2>/dev/null || echo 0)
    local found=0
    for f in "$COOLDOWN_DIR"/last_run_epoch.*; do
        [ -f "$f" ] || continue
        found=1
        local key="${f##*/last_run_epoch.}"
        local ts
        ts=$(cat "$f" 2>/dev/null || echo 0)
        local age=$((now - ts))
        printf '  %s  age=%ss  ts=%s\n' "$key" "$age" "$ts"
    done
    if [ $found -eq 0 ]; then
        echo "no per-project cooldowns recorded"
    else
        echo
        echo "(project_dir is hashed — use --project <dir> or rerun bootstrap to identify)"
    fi
    if [ -f "$LEGACY_COOLDOWN_FILE" ]; then
        echo "legacy global cooldown still present at $LEGACY_COOLDOWN_FILE"
    fi
}

# --- Parse args ---
MODE="current"
EXPLICIT_DIR=""
DO_CLEAR_ALERTS=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --all) MODE="all" ;;
        --status) MODE="status" ;;
        --project)
            shift
            [ $# -gt 0 ] || { echo "--project requires a directory argument" >&2; exit 2; }
            EXPLICIT_DIR="$1"
            MODE="explicit"
            ;;
        --clear-alerts) DO_CLEAR_ALERTS=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "$MODE" in
    current)  reset_one "$PWD" ;;
    explicit) reset_one "$EXPLICIT_DIR" ;;
    all)      reset_all ;;
    status)   print_status ;;
esac

if [ -n "$DO_CLEAR_ALERTS" ]; then
    clear_alerts
fi

exit 0
