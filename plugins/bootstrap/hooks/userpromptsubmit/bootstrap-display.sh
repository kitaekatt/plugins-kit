#!/usr/bin/env bash
# bootstrap-display.sh — UserPromptSubmit hook that surfaces bootstrap results once.
#
# The SessionStart hook fires the engine in the background. The engine writes
# its display JSON to bootstrap_display.pending when done. This hook checks for
# that file on every user prompt (~0ms when idle) and emits it once, then renames
# it to bootstrap_display.displayed so it won't be shown again.
#
# Why UserPromptSubmit (not Stop): UserPromptSubmit supports
# hookSpecificOutput.additionalContext, which injects context to Claude.
# Stop hooks reject hookSpecificOutput via schema validation.
#
# Handshake protocol:
#   .pending   = engine wrote this, needs to be shown
#   .displayed = stop hook read and renamed it, already shown
# If the engine needs to show new content, it writes a new .pending file.

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MARKETPLACE_NAME="$(basename "$(cd "$PLUGIN_ROOT/../.." && pwd)")"
# Honors CLAUDE_BOOTSTRAP_DATA_ROOT for the same reason session-bootstrap.sh does:
# this hook reads the display handshake files the engine wrote, so it must look in
# whichever tree that engine was pointed at.
BOOTSTRAP_DATA_ROOT="${CLAUDE_BOOTSTRAP_DATA_ROOT:-${HOME}/.claude/plugins/data}"
DATA_DIR="${BOOTSTRAP_DATA_ROOT}/${MARKETPLACE_NAME}/bootstrap"

# claude-plugin-test stand-down -- the cached copy yields to the dev tree's copy
# for the whole session. Same rationale as session-bootstrap.sh: both copies load
# and both would otherwise run, and this hook also carries the harvest and the
# SessionStart rescue, so a cached copy left running would relaunch a PRODUCTION
# pass from inside a session meant to be isolated.
if [ -n "${CLAUDE_PLUGIN_TEST:-}" ] && case "$PLUGIN_ROOT" in
    */.claude/plugins/cache/*) true ;; *) false ;; esac; then
    exit 0
fi
PENDING="${DATA_DIR}/bootstrap_display.pending"

# --- Capture hook input (UserPromptSubmit JSON on stdin) ---
# Needed by the SessionStart-missed rescue below to learn this session's id.
# TTY-guarded so a manual terminal invocation doesn't block; read -t caps a
# pathological open-but-silent stdin pipe at 10s (a bare cat would hang until
# the hook timeout); -d '' reads the whole JSON regardless of newlines.
HOOK_INPUT=""
if [ ! -t 0 ]; then
    IFS= read -r -t 10 -d '' HOOK_INPUT 2>/dev/null || true
fi

# --- Single-session update harvest (Part 2) ---
# Claude Code's auto-updater can fetch a NEWER bootstrap mid-session: the new
# code lands on disk and installed_plugins.json is repointed, but the
# SessionStart hook already ran the OLD engine and only re-fires on a fresh
# session. UserPromptSubmit DOES re-fire within the session, so harvest the
# already-fetched new engine here and converge in ONE session. The Python helper
# keeps this near-zero cost when there's no update (two reads + a compare); it
# only detaches a real pass when the installed version is strictly newer than the
# engine_ran_version stamp. Output is discarded so it never lands in the prompt's
# context; the launched engine surfaces through bootstrap_display.pending. Fully
# best-effort -- it must never block or fail the prompt.
#
# Invoke bootstrap's standalone Python by absolute path (NOT bare python/python3,
# which hits Windows Store stubs; NOT `uv run`, whose env resolution would add
# latency to every prompt). It's the same interpreter session-bootstrap.sh runs
# the engine with, guaranteed present once SessionStart has run.
_OS="$(uname -s 2>/dev/null || echo unknown)"
if [[ "$_OS" == MINGW* ]] || [[ "$_OS" == MSYS* ]]; then
    _BOOT_PY="${HOME}/.local/share/python-standalone/python/python.exe"
else
    _BOOT_PY="${HOME}/.local/bin/python3"
fi
[ -x "$_BOOT_PY" ] || _BOOT_PY="$(command -v python3 2>/dev/null || true)"
_run_harvest() {
    [ -n "$_BOOT_PY" ] && [ -f "$PLUGIN_ROOT/bootstrap_lib/harvest.py" ] || return 0
    "$_BOOT_PY" "$PLUGIN_ROOT/bootstrap_lib/harvest.py" \
        --data-dir "$DATA_DIR" \
        --project-dir "$PWD" \
        --marketplace "$MARKETPLACE_NAME" \
        >/dev/null 2>&1 || true
}

# --- SessionStart-missed rescue ---
# On a fresh machine (or after a mid-session plugin install) Claude Code can
# still be syncing the marketplace when SessionStart fires, so bootstrap's
# SessionStart hook isn't registered yet and the provisioning pass never runs
# this session. UserPromptSubmit re-fires every prompt, so detect the miss
# here. Pure bash by necessity: on a fresh machine Python doesn't exist yet --
# session-bootstrap.sh is what installs it.
#
# Detection signal: session-bootstrap.sh touches sessions/<session_id> at ENTRY
# (before its gates), so a marker missing for THIS prompt's session_id means no
# SessionStart pass was invoked for this session. Deliberately NOT the Layer-1
# last_session_id guard stamp: that is a single global slot (a second concurrent
# session overwrites it -- comparing against it ping-pongs rescues between
# sessions forever) and bootstrap-reset-cooldown deletes it (which must re-arm
# the NEXT SessionStart, not fire a mid-session pass on the next prompt).
#
# Launch discipline (the detached subshell; never delays the prompt):
#   1. sleep, then re-check the marker -- a genuinely-firing SessionStart pass
#      (fast-start / claude -p race) touches it within milliseconds of starting,
#      so the rescue stands down. Stand-down runs the harvest this prompt would
#      otherwise have skipped, so a single-prompt session still converges a
#      pending update.
#   2. stand down if ANY per-project cooldown stamp is fresh (<120s): a pass
#      stamped it at entry moments ago -- covers a SessionStart pass that
#      received no stdin (documented mode) and so never wrote a marker.
#   3. atomic one-launch-per-session lock (noclobber create): at most ONE rescue
#      launch per session_id, ever, even across overlapping prompts.
#   4. launch session-bootstrap.sh with the hook JSON piped in, so it writes
#      this session's marker and its normal gates (session guard, per-project
#      cooldown, registry bypass) apply unchanged.
_RESCUE_LAUNCHED=""
_SB="$PLUGIN_ROOT/hooks/sessionstart/session-bootstrap.sh"
_RESCUE_DELAY="${BOOTSTRAP_RESCUE_DELAY:-2}"
if [ -n "$HOOK_INPUT" ] && [ -f "$_SB" ]; then
    # session_id extraction: keep byte-identical to session-bootstrap.sh's
    # _GUARD_SID pipeline (drift test: tests/bootstrap/test_sessionstart_rescue.py).
    _SID=$(echo "$HOOK_INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | grep -o '"[^"]*"$' | tr -d '"' || true)
    _SID=$(printf '%s' "$_SID" | tr -cd 'A-Za-z0-9._-')
    _SESS_MARKER="$DATA_DIR/sessions/$_SID"
    _RESCUE_LOCK="$DATA_DIR/sessions/rescue_launched.$_SID"
    if [ -n "$_SID" ] && [ ! -e "$_SESS_MARKER" ] && [ ! -e "$_RESCUE_LOCK" ]; then
        _RESCUE_LAUNCHED=1
        (
            sleep "$_RESCUE_DELAY"
            if [ -e "$_SESS_MARKER" ]; then
                # A SessionStart pass claimed this session while we slept: it
                # owns provisioning. Run the harvest skipped in the foreground.
                _run_harvest
                exit 0
            fi
            if [ -n "$(find "$DATA_DIR/cooldowns" -type f -newermt '-120 seconds' 2>/dev/null | head -n 1)" ]; then
                # A pass stamped a cooldown moments ago (possibly a no-stdin
                # SessionStart still running): don't race it. Re-evaluated on
                # the next prompt.
                _run_harvest
                exit 0
            fi
            mkdir -p "$DATA_DIR/sessions"
            ( set -C; : > "$_RESCUE_LOCK" ) 2>/dev/null || exit 0
            {
                echo "--- Shell $(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo unknown-time) ---"
                echo "sessionstart-rescue: no SessionStart pass ran for session $_SID; launching session-bootstrap.sh"
            } >> "$DATA_DIR/bootstrap.log"
            printf '%s' "$HOOK_INPUT" | bash "$_SB"
        ) >/dev/null 2>&1 &
    fi
fi

# Run the per-prompt harvest unless the rescue armed this prompt (its subshell
# either launches a full pass or runs the harvest itself on stand-down -- never
# two engine launches from one prompt).
if [ -z "$_RESCUE_LAUNCHED" ]; then
    _run_harvest
fi

# --- Display relay ---
# The engine writes its display JSON to bootstrap_display.pending; surface it
# once, then rename so it isn't shown again.
#
# The pass that WROTE the file and the prompt that READS it are not the same
# moment: a pending file waits on disk until some session's first prompt picks
# it up, so a session that started after a skipped pass can be handed a verdict
# produced long before it. Nothing in the payload said when the pass ran, so
# "Setup issues found" read as a statement about the machine right now.
# bootstrap_lib/display_relay.py stamps the message with its own age (derived
# from the pending file's mtime) before emitting it, and consumes the file
# itself -- so on success we must NOT also cat it.
#
# The plain cat + mv below stays as the fallback, and it is load-bearing rather
# than defensive: on a fresh machine there is no Python yet, and the pending
# file is often the message that says exactly that. Never let the age stamp
# cost the user their bootstrap message.
[ -f "$PENDING" ] || exit 0
if [ -n "$_BOOT_PY" ] && [ -f "$PLUGIN_ROOT/bootstrap_lib/display_relay.py" ]; then
    if "$_BOOT_PY" "$PLUGIN_ROOT/bootstrap_lib/display_relay.py" \
        --data-dir "$DATA_DIR" 2>/dev/null; then
        exit 0
    fi
fi
cat "$PENDING"
mv -f "$PENDING" "${DATA_DIR}/bootstrap_display.displayed"
