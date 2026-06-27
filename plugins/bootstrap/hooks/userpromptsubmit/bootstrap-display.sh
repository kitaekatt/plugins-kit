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
DATA_DIR="${HOME}/.claude/plugins/data/${MARKETPLACE_NAME}/bootstrap"
PENDING="${DATA_DIR}/bootstrap_display.pending"

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
if [ -n "$_BOOT_PY" ] && [ -f "$PLUGIN_ROOT/bootstrap_lib/harvest.py" ]; then
    "$_BOOT_PY" "$PLUGIN_ROOT/bootstrap_lib/harvest.py" \
        --data-dir "$DATA_DIR" \
        --project-dir "$PWD" \
        --marketplace "$MARKETPLACE_NAME" \
        >/dev/null 2>&1 || true
fi

# --- Display relay (unchanged) ---
# The engine writes its display JSON to bootstrap_display.pending; surface it
# once, then rename so it isn't shown again.
[ -f "$PENDING" ] || exit 0
cat "$PENDING"
mv -f "$PENDING" "${DATA_DIR}/bootstrap_display.displayed"
