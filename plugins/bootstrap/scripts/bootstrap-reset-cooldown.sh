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
# Resolves the bootstrap data dir(s) under
# ${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}/<marketplace>/bootstrap.
# With BOOTSTRAP_MARKETPLACE unset, acts on EVERY marketplace directory found
# under the data root rather than assuming plugins-kit -- the levers are
# installed into ~/.local/bin as copies/symlinks that cannot derive their own
# marketplace from $0. Set BOOTSTRAP_MARKETPLACE to scope to one marketplace.

set -uo pipefail

# CLAUDE_BOOTSTRAP_DATA_ROOT redirects everything bootstrap owns to an
# alternate tree for the lifetime of one session (set by the
# claude-plugin-test launcher; unset everywhere else). Mirrors
# session-bootstrap.sh's BOOTSTRAP_DATA_ROOT derivation exactly so this lever
# targets the same tree the engine was pointed at.
BOOTSTRAP_DATA_ROOT="${CLAUDE_BOOTSTRAP_DATA_ROOT:-${HOME}/.claude/plugins/data}"
MARKETPLACE="${BOOTSTRAP_MARKETPLACE:-}"

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

# Normalize a --project argument to the same form session-bootstrap.sh hashes
# for $PWD: an absolute path, no trailing slash. The hook hashes the LOGICAL
# $PWD verbatim (no symlink resolution -- see session-bootstrap.sh, the
# _PROJECT_KEY block), so this uses the logical `pwd`, never `pwd -P`: a
# project reached through a symlink component (macOS /tmp -> /private/tmp, a
# symlinked dev root) would otherwise hash to a different key than the stamp
# the hook wrote. Hashing the argument verbatim would likewise miss a stamp
# keyed by a relative or trailing-slash spelling of the same directory.
resolve_project_dir() {
    local dir="$1"
    ( cd "$dir" 2>/dev/null && pwd )
}

# One bootstrap data dir per line: either the single MARKETPLACE-scoped dir
# (BOOTSTRAP_MARKETPLACE set), or every <data root>/*/bootstrap directory
# found (unset) -- falling back to the plugins-kit default when the data root
# doesn't exist yet, so --status still has something sensible to report.
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

reset_one() {
    local project_dir="$1"
    local key
    key=$(hash_path "$project_dir")
    local any_found=0
    local pd
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        local f="$pd/cooldowns/last_run_epoch.$key"
        if [ -f "$f" ]; then
            rm -f "$f"
            echo "reset cooldown for $project_dir ($(basename "$(dirname "$pd")"))"
            any_found=1
        fi
        # Also clear the session_id guard so the next launch isn't skipped by it.
        [ -f "$pd/last_session_id" ] && rm -f "$pd/last_session_id"
    done < <(plugin_data_dirs)
    if [ "$any_found" -eq 0 ]; then
        echo "no cooldown to reset for $project_dir"
    fi
}

reset_all() {
    local pd
    local any_dir=0
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        any_dir=1
        local cooldown_dir="$pd/cooldowns"
        if [ -d "$cooldown_dir" ]; then
            local n
            n=$(find "$cooldown_dir" -maxdepth 1 -name 'last_run_epoch.*' -type f 2>/dev/null | wc -l | tr -d ' ')
            rm -f "$cooldown_dir"/last_run_epoch.* 2>/dev/null
            echo "reset $n per-project cooldown(s) ($(basename "$(dirname "$pd")"))"
        fi
        # Pre-per-project legacy cooldown file
        if [ -f "$pd/last_run_epoch" ]; then
            rm -f "$pd/last_run_epoch"
            echo "reset legacy global cooldown ($(basename "$(dirname "$pd")"))"
        fi
        if [ -f "$pd/last_session_id" ]; then
            rm -f "$pd/last_session_id"
            echo "cleared session_id guard ($(basename "$(dirname "$pd")"))"
        fi
    done < <(plugin_data_dirs)
    if [ "$any_dir" -eq 0 ]; then
        echo "no cooldown directory found under $BOOTSTRAP_DATA_ROOT"
    fi
}

clear_alerts() {
    local cleared=0
    local pd
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        local alert_file="$pd/bootstrap_alert.json"
        local pending_file="$pd/bootstrap_display.pending"
        if [ -f "$alert_file" ]; then
            rm -f "$alert_file"
            echo "cleared $alert_file"
            cleared=1
        fi
        # The pending file is the ONLY channel any pass has to the user, and the
        # shell's pre-Python failure paths write nothing else. Deleting it
        # unconditionally between a pass and the next prompt silently discards
        # that pass's verdict -- including, in the worst case, the message saying
        # bootstrap could not run at all. Clearing an ALERT is the stated purpose;
        # destroying an undelivered message is collateral. So it goes only with
        # --force, and says what it is withholding otherwise.
        if [ -f "$pending_file" ]; then
            if [ "$FORCE" -eq 1 ]; then
                rm -f "$pending_file"
                echo "cleared $pending_file"
                cleared=1
            else
                echo "kept $pending_file (undelivered pass output; --force to delete)"
            fi
        fi
    done < <(plugin_data_dirs)
    if [ "$cleared" -eq 0 ]; then
        echo "no alerts to clear"
    fi
}

print_status() {
    local pd
    local any_dir=0
    local now
    now=$(date +%s 2>/dev/null || echo 0)
    while IFS= read -r pd; do
        [ -n "$pd" ] || continue
        local mkt
        mkt="$(basename "$(dirname "$pd")")"
        local cooldown_dir="$pd/cooldowns"
        if [ ! -d "$cooldown_dir" ]; then
            echo "$mkt: no cooldowns recorded ($cooldown_dir does not exist)"
            continue
        fi
        any_dir=1
        local found=0
        for f in "$cooldown_dir"/last_run_epoch.*; do
            [ -f "$f" ] || continue
            found=1
            local key="${f##*/last_run_epoch.}"
            local ts
            ts=$(cat "$f" 2>/dev/null || echo 0)
            local age=$((now - ts))
            printf '  %s  %s  age=%ss  ts=%s\n' "$mkt" "$key" "$age" "$ts"
        done
        if [ "$found" -eq 0 ]; then
            echo "$mkt: no per-project cooldowns recorded"
        fi
        if [ -f "$pd/last_run_epoch" ]; then
            echo "$mkt: legacy global cooldown still present at $pd/last_run_epoch"
        fi
    done < <(plugin_data_dirs)
    if [ "$any_dir" -eq 1 ]; then
        echo
        echo "(project_dir is hashed — use --project <dir> or rerun bootstrap to identify)"
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
    explicit)
        RESOLVED_DIR="$(resolve_project_dir "$EXPLICIT_DIR")"
        if [ -z "$RESOLVED_DIR" ]; then
            echo "--project $EXPLICIT_DIR: no such directory" >&2
            exit 2
        fi
        reset_one "$RESOLVED_DIR"
        ;;
    all)      reset_all ;;
    status)   print_status ;;
esac

if [ -n "$DO_CLEAR_ALERTS" ]; then
    clear_alerts
fi

exit 0
