# newest-copy.sh -- sourced by session-bootstrap.sh; not executed.
#
# bootstrap_newer_copy <plugin_root>
#
# Prints the root of the newest complete bootstrap copy installed beside
# <plugin_root> when it is strictly newer than <plugin_root>, and prints
# nothing otherwise. Always returns 0.
#
# Why: Claude Code binds ${CLAUDE_PLUGIN_ROOT} when a session starts, so a
# session that was already running when bootstrap updated keeps firing the OLD
# copy's hooks -- on every SessionStart it sees, a subagent's compaction
# included. All copies share one data dir and one bootstrap venv, so an old
# copy's pass and the installed copy's passes undo each other (each re-syncs
# the venv's editable install to its own version). The hook hands such a pass
# to the newest copy instead of running it.
#
# Only the cache layout (<...>/bootstrap/<X.Y.Z>/) takes part: <plugin_root>'s
# directory name must be a plain X.Y.Z version equal to its plugin.json version.
# A dev checkout (<checkout>/plugins/bootstrap) never hands off. A candidate
# must carry the hook, the engine entry point, and a plugin.json whose version
# equals its directory name, so a half-written cache dir is never chosen.
# Versions compare numerically per component (0.104.0 > 0.98.1). Bash 3.2 safe.

# Prints the X.Y.Z version of a cache-layout copy, or returns 1.
_bootstrap_copy_version() {
    local root="$1" v manifest_v
    v="$(basename "$root")"
    [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    manifest_v="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' \
        "$root/.claude-plugin/plugin.json" 2>/dev/null | head -n 1 \
        | sed 's/.*"\([^"]*\)"$/\1/')"
    [ "$manifest_v" = "$v" ] || return 1
    printf '%s\n' "$v"
}

# True when X.Y.Z version $1 is strictly greater than $2.
_bootstrap_version_gt() {
    local a1 a2 a3 b1 b2 b3
    IFS=. read -r a1 a2 a3 <<< "$1"
    IFS=. read -r b1 b2 b3 <<< "$2"
    [ $((10#$a1)) -ne $((10#$b1)) ] && { [ $((10#$a1)) -gt $((10#$b1)) ]; return; }
    [ $((10#$a2)) -ne $((10#$b2)) ] && { [ $((10#$a2)) -gt $((10#$b2)) ]; return; }
    [ $((10#$a3)) -gt $((10#$b3)) ]
}

bootstrap_newer_copy() {
    local root="$1" own best="" best_v cand v
    own="$(_bootstrap_copy_version "$root")" || return 0
    best_v="$own"
    for cand in "$(dirname "$root")"/*/; do
        cand="${cand%/}"
        [ -f "$cand/hooks/sessionstart/session-bootstrap.sh" ] || continue
        [ -f "$cand/engine/bootstrap_engine.py" ] || continue
        v="$(_bootstrap_copy_version "$cand")" || continue
        if _bootstrap_version_gt "$v" "$best_v"; then
            best="$cand"
            best_v="$v"
        fi
    done
    [ -n "$best" ] && printf '%s\n' "$best"
    return 0
}
